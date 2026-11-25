#!/usr/bin/env python3
"""The monster conversion lane of the v2 preview app (DESIGN_PHASE5.md section E and H; orchestrator lane `monster_convert`, limit 1).

One queue task per (variant, frozen view list = generation): key `monster_convert:<Monster>/<variant>:<frozen digest 12>`, set_name
`monster:<Monster>/<variant>`, unit `convert`, args {monster, variant, files, frozen_sha, frozen_time, run_now?, override_pause?},
priority 4, max_attempts 1 (a paid step is never retried automatically: a failure waits for Retry, which resumes from the record).

The queue is a rebuildable cache. What must survive a rebuilt queue lives in views.json (phase 5 review F4): `conversion[<generation>]`
= {state: cancelled | failed, reason, key, time}, written BEFORE the queue row is cancelled / failed and cleared by an ordinary Retry (and
by Run now on such a task). `tick` looks every frozen generation up by its EXACT key; a missing row of a generation with a disposition is
restored in that terminal state (never queued), and a queued row of such a generation is never started (the plan cancels it).

The task runs THIS file as its builder (run_task.py wrapper, detached):

    python -X utf8 tools/preview/v2/monster_lane.py run --root <imageto3d root> --monster "<M>" --variant <v> --frozen-sha <12 hex>
           [--run-now] [--override-pause <pause key>]

and the driver walks the steps, each resumable from `models/<v>/hyper3d/studio_submission.json` (never paying twice):
  0. ownership: one driver per variant (`.codex_jobs/_app/locks/mconvert/<variant>.lock`, held for the whole run) and no live process of
     an earlier attempt (the children file, below); otherwise verdict `busy`;
  1. paid accounting is reconciled from the record on EVERY run (F5), also for an exported or reduced record;
  2. a record exists -> its `inputs` (the backend's ordered [{name, sha256, size}]) must equal the frozen generation, else it is refused
     before anything is spent (F7); collect or inspect by `status` / `actions` / `pending_action` (a `free_preview` intent of a record
     whose model is bound and identified is resolved, F1; any other pending click is refused). NO record -> submit (free): the frozen
     files are first copied to `models/<v>/hyper3d/inputs_<generation>/` (read-only, sha256-verified) and THAT folder is uploaded (F7):
     one view -> `hyper3d_single.py submit <snapshot> <out> --view <file>`; 2-4 -> `hyper3d_multiview.py submit <snapshot> <out> --views
     a,b[,c[,d]]` (the frozen order), with the Hyper3D venv python (`$V2_HYPER3D_PYTHON`, else `$HYPER3D_VENV` /
     `%USERPROFILE%/.cache/hyper3d-venv`, `Scripts/python.exe -X utf8`) from the monster root;
  3. `collect --allow-paid --accept-preview` (PAID, 0.5 credits). Every wrapper child gets the click-time gate (DESIGN_PHASE4.md I.8):
     `paid_gate.env_for(root, override_pause, cmd=[python, -X, utf8, monster_lane.py, gate, --root, R, --task-args, <json>])`; a child that
     stops at a closed gate exits 4 (paid_gate.EXIT_GATE_CLOSED) = verdict `waiting` (requeue), never a failure;
  4. `python tools/reduce_monster.py "<M>/<v>"` (Blender) -> models/<v>/reduced/; then every output is validated and the generation-bound
     success record `reduced/v2_reduce_ok.json` is written (F12; `--redo` only for an existing reduced/).
Every child is started with Popen and recorded in `.codex_jobs/_app/monster_lane/<variant key>.children.json` ({driver, children: [{pid,
create, argv}]}) until it exited, so Kill & retry can drain the whole process tree and a slot is never released while a child of the
attempt still runs (F14).
Before every step the gate is checked again. The LAST stdout line is the verdict JSON: {"status": "ok" | "busy" | "waiting" | "failed", ...}.

GATE (identical to conversions): the conversion hold `.codex_jobs/_batch/CONVERT_HOLD` -> wait, never overridden; mode `pause` /
`pause-conversions`, or a PAUSE file with `conversions` in its scope for a manual reason -> wait; a Run now confirmed while paused stores
the PAUSE KEY it overrode (`override_pause` = `pause_key()`: the mode and its `since`, or the PAUSE file's `since` and reason) and overrides
only that pause (a new pause needs a new confirmation; an ordinary Retry clears Run now and the override, F4); mode `auto` / `views-run`
-> only when the user is away (idle >= 300 s) and no "user active" PAUSE -> a per-task Run now overrides this idle gate only.
`gate()['code']`: `held` | `paused` | `user_present` (None when open).

HOOKS for the orchestrator / actions (DESIGN_PHASE5.md H.1): `tick`, `plan_starts`, `task_argv` / `TASK_ENV`, `finish_outcome`,
`record_failure`, `task_wanted`, `run_now`, `cancel_task`, `retry_task`, `active_reason`, `recorded_children` / `clear_children`,
`lane_status` / `paid_summary`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
for _p in (HERE.parents[1], HERE.parent, HERE):            # tools/ (paid_gate), tools/preview, tools/preview/v2
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import guard  # noqa: E402
import modes  # noqa: E402
import monsters_v2 as mv  # noqa: E402
import paid_gate  # noqa: E402
import taskqueue  # noqa: E402

try:
    import psutil  # noqa: E402
except ImportError:  # pragma: no cover
    psutil = None

LANE = 'monster_convert'
LIMIT = 1
PRIORITY = 4
MAX_ATTEMPTS = 1
BUSY_DELAY = 300.0                 # "Hyper3D profile busy" / another driver of the variant: try again after 5 min
WAIT_DELAY = 60.0                  # the gate closed during the run: look again after 1 min
PAID_LOG = 'paid_log.jsonl'
PAID_ERRORS = 'paid_log_errors.json'
CREDITS_PER_MODEL = 0.5            # what the confirmations SAY a conversion costs; never substituted for an unobserved amount (F5)
PAID_KINDS = ('geometry', 'material_generate', 'material_confirm')    # = rodin_backend.COST_KINDS
IDLE_REASONS = ('user active', 'user active (views keep running)')   # pipeline_supervisor: the screen-on pauses (not manual)
BUSY_TEXT = 'profile busy'
TASK_ENV = {'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1'}
HOLD_FILE = 'CONVERT_HOLD'         # = services.HOLD_FILE (DESIGN_PHASE4.md I.1); read here without importing services (psutil)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
CHILDREN_DIR = 'monster_lane'
CREATE_TOLERANCE = 1.0


class ConfirmPaused(guard.Conflict):
    """Run now while conversions are paused by hand: the client asks for confirmation, then sends `confirm: true` (HTTP 409)."""
    code = 'confirm'


# ------------------------------------------------------------------------------------------------------------------ queue

def queue(root: Path) -> taskqueue.TaskQueue:
    return taskqueue.for_root(Path(root))


def set_name(monster: str, variant: str) -> str:
    return f'monster:{monster}/{variant}'


def task_key(monster: str, variant: str, frozen: dict) -> str:
    return f'{LANE}:{monster}/{variant}:{mv.frozen_digest(frozen)}'


def _args(monster: str, variant: str, frozen: dict, run_now: bool = False) -> dict:
    return {'monster': monster, 'variant': variant, 'files': [str(f) for f in frozen.get('files') or []],
            'frozen_sha': mv.frozen_digest(frozen), 'frozen_time': frozen.get('time'), 'run_now': bool(run_now)}


def enqueue(root: Path, monster: str, variant: str, frozen: dict, run_now: bool = False) -> int:
    """Queue the conversion of a frozen view list: an existing row of that EXACT key is returned unchanged (a cancelled / failed one is
    never revived here: that is Retry), and a generation with a persisted disposition is refused (F4)."""
    if LANE not in taskqueue.LANES:
        raise RuntimeError(f'the {LANE} lane is not registered in taskqueue.LANES')
    key = task_key(monster, variant, frozen)
    q = queue(root)
    row = q.by_key(key)
    if row is not None:
        return row['id']
    vj = mv.read_views_json(mv.views_dir(mv.mroot_of(root), monster, variant))
    disp = mv.disposition_of(vj, mv.frozen_digest(frozen))
    if disp is not None:
        raise ValueError(f'the conversion of this choice was {disp["state"]} ({disp.get("reason") or "-"}); use Retry')
    task_id, _created = q.enqueue_once(LANE, key, set_name(monster, variant), 'convert', _args(monster, variant, frozen, run_now),
                                       priority=PRIORITY, max_attempts=MAX_ATTEMPTS)
    return task_id


def lane_tasks(root: Path, state=None, limit: int = 5000) -> list[dict]:
    return queue(root).tasks(state=state, lane=LANE, limit=limit)


def newest_tasks(root: Path) -> dict[str, dict]:
    """{"<monster>/<variant>": newest task of the lane} (display only; decisions use exact keys)."""
    out: dict[str, dict] = {}
    for task in lane_tasks(root):
        name = str(task.get('set_name') or '').removeprefix('monster:')
        out.setdefault(name, task)
    return out


def open_task(root: Path, monster: str, variant: str) -> dict | None:
    for task in queue(root).tasks(state=taskqueue.OPEN, lane=LANE, set_name=set_name(monster, variant), limit=10):
        return task
    return None


def task_check(root: Path, task: dict) -> dict:
    """{wanted, why, damaged}: whether a task of the lane should still run. Not wanted: its views.json no longer has the frozen list it was
    queued for, the variant is approved, or (a QUEUED row) its generation has a persisted disposition or a verified reduction. `damaged`:
    views.json / chosen_lod.json cannot be read, which proves nothing (the task neither starts nor is cancelled, F16)."""
    args = task.get('args') if isinstance(task.get('args'), dict) else {}
    mroot = mv.mroot_of(root)
    try:
        monster, variant = mv.safe_part(args.get('monster'), 'monster'), mv.check_variant(args.get('variant'))
    except ValueError:
        return {'wanted': False, 'why': 'the task names no valid monster / variant', 'damaged': False}
    try:
        vj = mv.read_views_json(mv.views_dir(mroot, monster, variant))
    except guard.Unreadable as exc:
        return {'wanted': False, 'why': str(exc), 'damaged': True}
    if not vj or not vj.get('frozen'):
        return {'wanted': False, 'why': 'the variant has no frozen view list any more', 'damaged': False}
    generation = mv.frozen_digest(vj['frozen'])
    if generation != args.get('frozen_sha'):
        return {'wanted': False, 'why': 'the views were chosen again since this conversion was queued', 'damaged': False}
    mdir = mv.models_dir(mroot, monster, variant)
    try:
        if mv.read_choice(mdir).get('approved') is True:
            return {'wanted': False, 'why': 'the variant is approved', 'damaged': False}
    except guard.Unreadable as exc:
        return {'wanted': False, 'why': str(exc), 'damaged': True}
    if task.get('state') == 'queued':
        disp = mv.disposition_of(vj, generation)
        if disp is not None:
            return {'wanted': False, 'why': f'the conversion of this choice was {disp["state"]} ({disp.get("reason") or "-"}); use Retry',
                    'damaged': False}
        if mv.reduce_status(mdir, generation)['ok']:
            return {'wanted': False, 'why': 'the variant is converted and reduced already', 'damaged': False}
    return {'wanted': True, 'why': None, 'damaged': False}


def task_wanted(root: Path, task: dict) -> tuple[bool, str | None]:
    """(wanted, reason): the lane's approval check (a damaged state counts as not wanted here: Retry / Kill & retry fail closed)."""
    check = task_check(root, task)
    return check['wanted'] and not check['damaged'], check['why']


def task_for(root: Path, monster: str, variant: str, frozen: dict | None) -> dict | None:
    """The row of a generation, by its exact key (None without a freeze or a row)."""
    return queue(root).by_key(task_key(monster, variant, frozen)) if isinstance(frozen, dict) else None


def _generation_of(task: dict) -> tuple[str, str, str] | None:
    args = task.get('args') if isinstance(task.get('args'), dict) else {}
    try:
        monster, variant = mv.safe_part(args.get('monster'), 'monster'), mv.check_variant(args.get('variant'))
    except ValueError:
        return None
    generation = args.get('frozen_sha')
    return (monster, variant, generation) if isinstance(generation, str) and generation else None


def record_failure(root: Path, task: dict, error: str | None) -> None:
    """HOOK (orchestrator, BEFORE it fails the row): persist `failed` for the task's generation (F4)."""
    gen = _generation_of(task)
    if gen:
        mv.set_disposition(root, gen[0], gen[1], gen[2], {'state': 'failed', 'reason': str(error or 'failed')[:300], 'key': task.get('key')})


def cancel_task(root: Path, task_id: int, detail: str = 'manual') -> bool:
    """HOOK (Queue tab Cancel of a monster row): persist `cancelled` for the generation FIRST, then cancel the queued row; when the row was
    no longer queued, the previous disposition is put back. False when it was not queued."""
    q = queue(root)
    task = q.get(task_id)
    if task is None or task['lane'] != LANE:
        raise mv.not_found(f'Unknown monster conversion task {task_id}')
    gen = _generation_of(task)
    previous = None
    if gen and task['state'] == 'queued':
        previous = mv.set_disposition(root, gen[0], gen[1], gen[2], {'state': 'cancelled', 'reason': 'cancelled by the user', 'key': task['key']})
    done = q.cancel(task_id, detail, manual_gen='manual')
    if not done and gen and task['state'] == 'queued':
        mv.set_disposition(root, gen[0], gen[1], gen[2], previous)
    return done


def retry_task(root: Path, task_id: int) -> bool:
    """HOOK (Retry of a failed / cancelled monster row, also Run now of one): the generation's disposition is cleared FIRST (the plan would
    cancel the queued row otherwise), then the row is queued again; Retry also clears Run now and its pause override (taskqueue.retry)."""
    q = queue(root)
    task = q.get(task_id)
    if task is None or task['lane'] != LANE:
        raise mv.not_found(f'Unknown monster conversion task {task_id}')
    gen = _generation_of(task)
    previous = mv.set_disposition(root, gen[0], gen[1], gen[2], None) if gen and task['state'] in ('failed', 'cancelled') else None
    done = q.retry(task_id)
    if not done and previous is not None:
        mv.set_disposition(root, gen[0], gen[1], gen[2], previous)
    return done


def active_reason(root: Path, monster: str, variant: str) -> str | None:
    """Why a conversion of the variant is active now (a queued / running row, a live driver, a live child of an earlier attempt), else None.
    An unreadable queue raises guard.Unreadable: it never means "nothing is running" (F6)."""
    q = queue(root)
    health = q.check()
    if not health.get('ok'):
        raise guard.Unreadable(f'the task queue cannot be read ({health.get("error")}); refusing while a conversion might be active')
    for task in q.tasks(state=taskqueue.OPEN, lane=LANE, set_name=set_name(monster, variant), limit=10):
        return f'a conversion is {task["state"]} (task {task["id"]})'
    if not q.health_view().get('ok'):
        raise guard.Unreadable('the task queue cannot be read; refusing while a conversion might be active')
    if driver_busy(root, monster, variant):
        return 'a conversion driver of this variant is running'
    kids = live_children(root, monster, variant)
    if kids:
        return f'a process of the last conversion attempt still runs (pid {", ".join(str(k["pid"]) for k in kids)})'
    return None


def tick(root: Path) -> dict:
    """HOOK (orchestrator loop, before starting): recover interrupted picks / imports / Reconverts, repair a failed paid-log write,
    auto-freeze the unattended variants, then for every frozen generation look its row up by EXACT key: none + a persisted disposition ->
    the row is restored in that terminal state; none -> queued. Each variant on its own (F16). {frozen, queued, restored, recovered,
    errors}."""
    root = Path(root)
    errors: list[str] = []
    recovered = mv.recover_journals(root) + mv.recover_reconverts(root)
    errors += [r for r in recovered if ' error ' in r]
    errors += repair_accounting(root)
    frozen_now = mv.auto_freeze(root, errors)
    queued, restored = [], []
    if LANE in taskqueue.LANES:
        q = queue(root)
        mroot = mv.mroot_of(root)
        for entry in mv.frozen_variants(root, errors):
            name = f'{entry["monster"]}/{entry["variant"]}'
            try:
                mdir = mv.models_dir(mroot, entry['monster'], entry['variant'])
                if mv.reduce_status(mdir, entry['digest'])['ok'] or mv.read_choice(mdir).get('approved') is True:
                    continue
                key = task_key(entry['monster'], entry['variant'], entry['frozen'])
                row = q.by_key(key)
                if not q.health_view().get('ok'):
                    errors.append(f'{name}: the queue cannot be read')
                    continue
                if row is not None:
                    continue
                disp = mv.disposition_of(entry['views_json'], entry['digest'])
                if disp is not None:
                    q.restore_terminal(LANE, key, set_name(entry['monster'], entry['variant']), 'convert',
                                       _args(entry['monster'], entry['variant'], entry['frozen']), disp['state'],
                                       f'{disp.get("reason") or disp["state"]} (restored from views.json)', PRIORITY, MAX_ATTEMPTS)
                    restored.append(name)
                    continue
                enqueue(root, entry['monster'], entry['variant'], entry['frozen'])
                queued.append(name)
            except Exception as exc:  # noqa: BLE001
                errors.append(f'{name}: {type(exc).__name__}: {exc}')
    if frozen_now or queued or restored or recovered:
        mv.invalidate()
    return {'frozen': frozen_now, 'queued': queued, 'restored': restored, 'recovered': recovered, 'errors': errors}


# ------------------------------------------------------------------------------------------------------------------- gate

def read_hold(root: Path) -> dict | None:
    """The conversion hold (CONVERT_HOLD) as a dict, None when absent; an unreadable file still holds."""
    path = Path(root) / '.codex_jobs' / '_batch' / HOLD_FILE
    if not path.exists():
        return None
    value = mv.read_json(path, None)
    return value if isinstance(value, dict) else {'reason': 'unreadable CONVERT_HOLD file'}


def pause_key(root: Path) -> str | None:
    """The identity of the manual pause of conversions now in force (None when there is none): the mode file's pause mode and `since`,
    else the PAUSE file's `since` and reason (a "user active" PAUSE is the idle gate, not a manual pause). A confirmed Run now overrides
    exactly this pause (F4)."""
    mode = modes.read_mode(root)
    if mode['mode'] in ('pause', 'pause-conversions'):
        return f'mode:{mode["mode"]}:{mode.get("since")}'
    pause = modes.read_pause(root)
    if pause is not None and 'conversions' in (pause.get('scope') or []) and pause.get('reason') not in IDLE_REASONS:
        return f'pause:{pause.get("since")}:{pause.get("reason")}'
    return None


def gate(root: Path, args: dict | None = None, idle: float | None = None, idle_known: bool = False) -> dict:
    """Whether a monster conversion step may run now: {open, reason, code, mode, pause, pause_key, hold, idle, away, needs_confirm}.
    `idle` = the user's idle seconds (None + idle_known=False: read presence.json / GetLastInputInfo). `code`: held | paused | user_present
    | None. `args.override_pause` overrides a manual pause only while it equals pause_key() (the pause the user confirmed)."""
    args = args or {}
    mode = modes.read_mode(root)['mode']
    pause = modes.read_pause(root)
    hold = read_hold(root)
    key = pause_key(root)
    if idle is None and not idle_known:
        idle, _source = modes.idle_seconds()
    away = idle is not None and idle >= modes.AWAY_AFTER
    run_now = bool(args.get('run_now'))
    override = isinstance(args.get('override_pause'), str) and key is not None and args['override_pause'] == key
    out = {'open': True, 'reason': None, 'code': None, 'mode': mode, 'pause': pause, 'pause_key': key, 'hold': hold, 'idle': idle,
           'away': away, 'needs_confirm': False}
    if hold is not None:                                   # never overridden: the supervisor is (re)starting or has not ticked yet
        return out | {'open': False, 'code': 'held', 'reason': f'conversions are held (CONVERT_HOLD: {hold.get("reason") or "no reason"}); '
                      'they wait until the running supervisor completes a tick'}
    paused_conv = pause is not None and 'conversions' in (pause.get('scope') or [])
    idle_pause = paused_conv and pause.get('reason') in IDLE_REASONS
    if mode in ('pause', 'pause-conversions') and not override:
        return out | {'open': False, 'code': 'paused', 'reason': f'conversions are paused (mode {mode})', 'needs_confirm': True}
    if paused_conv and not idle_pause and not override:
        return out | {'open': False, 'code': 'paused', 'reason': f'conversions are paused ({pause.get("reason") or "PAUSE file"})',
                      'needs_confirm': True}
    if mode in ('auto', 'views-run') and not run_now:
        if idle_pause:
            return out | {'open': False, 'code': 'user_present',
                          'reason': 'waiting until you are away (the supervisor pauses conversions while you are at the PC)'}
        if not away:
            return out | {'open': False, 'code': 'user_present', 'reason': 'waiting until you are away (conversions run only when the PC is idle)'}
    return out


def gate_command(root: Path, args: dict, idle: float | None = None, idle_known: bool = False) -> list[str]:
    """The `cmd` of the click-time gate (paid_gate.env_for) for the wrapper children of one attempt: this file's `gate` sub-command."""
    cmd = [sys.executable, '-X', 'utf8', str(HERE / 'monster_lane.py'), 'gate', '--root', str(Path(root)), '--task-args',
           json.dumps({'run_now': bool(args.get('run_now')), 'override_pause': args.get('override_pause')})]
    if idle_known:
        cmd += ['--idle', str(idle)] if idle is not None else ['--idle-unknown']
    return cmd


def plan_starts(root: Path, now: float | None = None, idle: float | None = None, idle_known: bool = False) -> dict:
    """HOOK (orchestrator `_start_monsters`): {start: [task] (0 or 1), cancel: [(task, reason)], waiting: {task id: reason}, active: n}.
    At most LIMIT attempts of the lane run at once (a variant whose earlier attempt left a live child counts as active, F14); every queued
    row of the lane is considered, Run now rows FIRST (F18); a queued task that is no longer wanted is to be cancelled; a damaged one waits;
    the first queued task whose gate is open is started (the gate is per task: Run now opens it for that task only)."""
    q = queue(root)
    active = q.tasks(state=taskqueue.ACTIVE, lane=LANE, limit=100)
    active_variants = {str(t.get('set_name')) for t in active}
    stray = [s for s in stray_trees(root) if s not in active_variants]
    out = {'start': [], 'cancel': [], 'waiting': {}, 'active': len(active) + len(stray)}
    if idle is None and not idle_known:
        idle, _source = modes.idle_seconds()
        idle_known = True
    rows = q.next_queued(LANE, 100000, now)
    rows.sort(key=lambda t: (not (t.get('args') or {}).get('run_now'), t.get('priority') or 0, t['id']))
    for task in rows:
        check = task_check(root, task)
        if check['damaged']:
            out['waiting'][task['id']] = f'the variant cannot be read ({check["why"]})'
            continue
        if not check['wanted']:
            out['cancel'].append((task, check['why']))
            continue
        if out['active'] + len(out['start']) >= LIMIT:
            out['waiting'][task['id']] = 'another monster conversion is running'
            continue
        gen = _generation_of(task)
        if gen and live_children(root, gen[0], gen[1]):
            out['waiting'][task['id']] = 'a process of the previous attempt still runs'
            continue
        g = gate(root, task.get('args'), idle, idle_known)
        if g['open']:
            out['start'].append(task)
        else:
            out['waiting'][task['id']] = g['reason']
    return out


def task_argv(root: Path, task: dict) -> list[str]:
    """HOOK (orchestrator `default_argv` for this lane): the driver command of one attempt."""
    args = task.get('args') or {}
    argv = [sys.executable, '-X', 'utf8', str(HERE / 'monster_lane.py'), 'run', '--root', str(Path(root)),
            '--monster', str(args['monster']), '--variant', str(args['variant']), '--frozen-sha', str(args.get('frozen_sha') or '')]
    if args.get('run_now'):
        argv.append('--run-now')
    if isinstance(args.get('override_pause'), str) and args['override_pause']:
        argv += ['--override-pause', args['override_pause']]
    return argv


def finish_outcome(root: Path, task: dict, result: dict) -> dict:
    """HOOK (orchestrator `_finish` for this lane): {outcome: done | requeue | failed, error, delay}. `done` needs exit 0, the verdict `ok`
    AND the lane's verified reduction of the task's generation (v2_reduce_ok.json, F12); `busy` / `waiting` are requeued (not failures);
    anything else fails (Retry resumes from the record)."""
    last = result.get('last_json') if isinstance(result.get('last_json'), dict) else None
    if result.get('start_error'):
        return {'outcome': 'failed', 'error': f'could not start: {result["start_error"]}', 'delay': 0.0}
    status = (last or {}).get('status')
    if status == 'busy':
        return {'outcome': 'requeue', 'error': (last or {}).get('error') or 'Hyper3D profile busy', 'delay': BUSY_DELAY}
    if status == 'waiting':
        return {'outcome': 'requeue', 'error': (last or {}).get('error') or 'waiting for the gate', 'delay': WAIT_DELAY}
    if status == 'ok' and result.get('exit') == 0:
        gen = _generation_of(task)
        if gen is None:
            return {'outcome': 'failed', 'error': 'the task names no valid monster / variant / generation', 'delay': 0.0}
        mdir = mv.models_dir(mv.mroot_of(root), gen[0], gen[1])
        verified = mv.reduce_status(mdir, gen[2])
        if verified['ok']:
            return {'outcome': 'done', 'error': None, 'delay': 0.0}
        return {'outcome': 'failed', 'error': f'the driver reported ok but the reduction is not verified ({verified["problem"]})', 'delay': 0.0}
    if last is None:
        return {'outcome': 'failed', 'error': f'the driver exited {result.get("exit")} without a verdict on stdout', 'delay': 0.0}
    return {'outcome': 'failed', 'error': str(last.get('error') or f'exit code {result.get("exit")}'), 'delay': 0.0}


def run_now(root: Path, task_id: object, confirm: bool = False) -> dict:
    """HOOK (POST /api/v2/queue {op: "run_now", id, confirm?}): mark a queued (or failed / cancelled: queued again through retry_task)
    monster conversion to run now. It overrides the idle gate; while conversions are paused by hand it needs `confirm: true` (409 `confirm`
    otherwise) and then overrides THAT pause (its pause_key) for this task. The conversion hold (CONVERT_HOLD) is never overridden: the task
    is marked and starts when the hold lifts (the answer carries a `warning`)."""
    if isinstance(task_id, bool) or not isinstance(task_id, int):
        raise ValueError('id (a task number) is required')
    q = queue(root)
    task = q.get(task_id)
    if task is None or task['lane'] != LANE:
        raise mv.not_found(f'Unknown monster conversion task {task_id}')
    g = gate(root, {'run_now': True})
    if not g['open'] and g['needs_confirm'] and not confirm:
        raise ConfirmPaused(f'{g["reason"]}: run this conversion anyway? (it pays {CREDITS_PER_MODEL} Hyper3D credits)')
    if task['state'] in ('failed', 'cancelled'):
        check = task_check(root, task)
        if not check['wanted'] or check['damaged']:
            raise ValueError(f'task {task_id} cannot run: {check["why"]}')
        retry_task(root, task_id)
    elif task['state'] != 'queued':
        raise ValueError(f'task {task_id} is {task["state"]}; Run now is for a queued task')
    override = g['pause_key'] if (confirm and g['needs_confirm']) else None
    if not q.mark_run_now(task_id, override):
        raise ValueError(f'task {task_id} is no longer queued')
    mv.invalidate()
    out = {'ok': True, 'id': task_id, 'state': 'queued', 'run_now': True, 'override_pause': bool(override), 'pause_key': override}
    if g['code'] == 'held':
        out['warning'] = g['reason']
    return out


# ------------------------------------------------------------------------------------------------------------- paid log

def paid_log_path(root: Path) -> Path:
    return Path(root) / guard.APP / PAID_LOG


def paid_errors_path(root: Path) -> Path:
    return Path(root) / guard.APP / PAID_ERRORS


def paid_entries(root: Path) -> list[dict]:
    out = []
    try:
        lines = paid_log_path(root).read_text(encoding='utf-8').splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def paid_actions(record: dict | None) -> list[dict]:
    """The credit-capable actions a submission record names (F5): [{action, status, credits, id}]. `status` confirmed = the backend
    observed the next stage after the click (its pending intent was cleared); uncertain = the intent is still pending (the click may or may
    not have happened). `credits` = the cost observed on the page for that click, None when it was not observed (never assumed). An action
    observed at cost 0 is free and not listed. `id` = `hyper3d:<model id>:<action>` (the provider's stable identity)."""
    if not isinstance(record, dict):
        return []
    actions = record.get('actions') if isinstance(record.get('actions'), dict) else {}
    pending = record.get('pending_action')
    model = record.get('model_id') if isinstance(record.get('model_id'), str) and record.get('model_id') else None
    out = []
    for kind in PAID_KINDS:
        if kind not in actions:
            continue
        observed = record.get(f'observed_cost_{kind}')
        credits = _number(observed.get('credits')) if isinstance(observed, dict) else None
        if credits == 0:
            continue
        out.append({'action': kind, 'status': 'uncertain' if pending == kind else 'confirmed', 'credits': credits,
                    'id': f'hyper3d:{model}:{kind}' if model else None})
    committed = _number(record.get('credits_committed'))
    if not out and committed and committed > 0:                  # credits committed without a named paid action (an older record)
        out.append({'action': 'committed', 'status': 'uncertain', 'credits': committed, 'id': f'hyper3d:{model}:committed' if model else None})
    return out


def has_paid(record: dict | None) -> bool:
    """Any evidence of a paid action (confirmed or not) or committed credits: such a record is never submitted again."""
    if not isinstance(record, dict):
        return False
    actions = record.get('actions') if isinstance(record.get('actions'), dict) else {}
    committed = _number(record.get('credits_committed'))
    return any(k in actions for k in PAID_KINDS) or bool(committed and committed > 0)


def _read_errors(root: Path) -> dict:
    value = mv.read_json(paid_errors_path(root), {})
    return value if isinstance(value, dict) else {'(unreadable)': {'error': f'{PAID_ERRORS} is unreadable'}}


def accounting_error(root: Path, record_path: Path) -> dict | None:
    """The unrepaired paid-log write error of a record ({time, error, ...}) or None."""
    value = _read_errors(root).get(Path(record_path).as_posix())
    return value if isinstance(value, dict) else None


def reconcile_paid(root: Path, monster: str, variant: str, files: list[str], record_path: Path, record: dict | None) -> str | None:
    """Bring paid_log.jsonl in line with a record (F5), on every run: one entry per provider action id, appended when it is new or its
    status / amount changed (a confirmed entry is final). An older lane's entry (no `id`) for the same record and model covers its
    geometry click. A write error is kept in paid_log_errors.json (shown in the Queue and the item) until a later reconcile succeeds; the
    error text is returned (None = in line)."""
    rec_text = Path(record_path).as_posix()
    wanted = paid_actions(record)
    try:
        with guard.os_lock(Path(root) / guard.APP / 'paid_log.lock'):
            entries = paid_entries(root)
            last = {e['id']: e for e in entries if isinstance(e.get('id'), str)}
            legacy = any(e.get('record') == rec_text and 'id' not in e and e.get('model_id') == (record or {}).get('model_id')
                         for e in entries)
            new = []
            for action in wanted:
                ident = action['id'] or f'record:{rec_text}:{action["action"]}'
                prev = last.get(ident)
                if prev is None and legacy and action['action'] in ('geometry', 'committed'):
                    continue
                if prev is not None and (prev.get('status') == 'confirmed' or
                                         (prev.get('status') == action['status'] and prev.get('credits') == action['credits'])):
                    continue
                new.append({'time': guard.now(), 'kind': 'hyper3d', 'id': ident, 'action': action['action'], 'status': action['status'],
                            'credits': action['credits'], 'monster': monster, 'variant': variant, 'views': list(files),
                            'record': rec_text, 'model_id': (record or {}).get('model_id')})
            if new:
                path = paid_log_path(root)
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, 'a', encoding='utf-8') as handle:
                    for entry in new:
                        handle.write(json.dumps(entry) + '\n')
            errors = _read_errors(root)
            if rec_text in errors:
                errors.pop(rec_text)
                guard.write_atomic(paid_errors_path(root), mv.dump(errors))
        return None
    except (OSError, guard.Conflict) as exc:
        text = f'the paid log could not be written ({type(exc).__name__}: {exc})'
        try:
            errors = _read_errors(root)
            errors[rec_text] = {'time': guard.now(), 'error': text, 'monster': monster, 'variant': variant, 'views': list(files)}
            guard.write_atomic(paid_errors_path(root), mv.dump(errors))
        except OSError:
            pass                                               # the verdict and the task row still carry the text
        return text


def repair_accounting(root: Path) -> list[str]:
    """HOOK (tick): retry the paid-log reconciliation of every record with an unrepaired write error. Returns the errors still open."""
    out = []
    for rec_text, info in list(_read_errors(root).items()):
        if not isinstance(info, dict) or not isinstance(info.get('monster'), str):
            out.append(f'paid log: {rec_text}: {info}')
            continue
        record = mv.read_json(Path(rec_text), None)
        error = reconcile_paid(root, info['monster'], str(info.get('variant')), list(info.get('views') or []), Path(rec_text),
                               record if isinstance(record, dict) else None)
        if error:
            out.append(f'paid log: {info["monster"]}/{info.get("variant")}: {error}')
    return out


def paid_summary(root: Path) -> dict:
    """{count, credits (confirmed, observed amounts), unknown_amounts (confirmed without an observed amount), uncertain {count, credits},
    recent, errors}: the last entry of every provider action id counts; entries of an older lane (no id) count as confirmed."""
    entries = [e for e in paid_entries(root) if e.get('kind') == 'hyper3d']
    by_id, legacy = {}, []
    for entry in entries:
        if isinstance(entry.get('id'), str):
            by_id[entry['id']] = entry
        else:
            legacy.append(entry)
    confirmed = [e for e in by_id.values() if e.get('status') == 'confirmed'] + legacy
    uncertain = [e for e in by_id.values() if e.get('status') != 'confirmed']
    known = [_number(e.get('credits')) for e in confirmed]
    return {'count': len(confirmed), 'credits': round(sum(k for k in known if k is not None), 3),
            'unknown_amounts': sum(1 for k in known if k is None),
            'uncertain': {'count': len(uncertain), 'credits': round(sum(_number(e.get('credits')) or 0 for e in uncertain), 3)},
            'recent': entries[-10:], 'errors': [dict(v, record=k) for k, v in _read_errors(root).items() if isinstance(v, dict)]}


def lane_status(root: Path) -> dict:
    """HOOK (GET /api/v2/queue `monsters`): the lane's gate, counts and the paid log summary."""
    q = queue(root)
    totals = {s: n for s in taskqueue.STATES if (n := q.count(s, lanes=[LANE]))}
    return {'lane': LANE, 'limit': LIMIT, 'gate': {k: v for k, v in gate(root).items() if k not in ('pause', 'hold')}, 'totals': totals,
            'paid': paid_summary(root), 'hold': read_hold(root)}


# ------------------------------------------------------------------------------------------------- driver ownership / tree

def variant_key(monster: str, variant: str) -> str:
    return f'{mv.slug(monster)[:40]}--{mv.slug(variant)[:24]}--{mv.canon_digest(monster, variant)}'


def driver_lock_path(root: Path, monster: str, variant: str) -> Path:
    return Path(root) / guard.APP / 'locks' / 'mconvert' / f'{variant_key(monster, variant)}.lock'


def driver_busy(root: Path, monster: str, variant: str) -> bool:
    """Whether a driver of the variant holds its lock now (probed without waiting)."""
    probe = guard.OsLock(driver_lock_path(root, monster, variant), wait=0)
    try:
        probe.__enter__()
    except guard.Busy:
        return True
    probe.__exit__(None, None, None)
    return False


def children_path(root: Path, monster: str, variant: str) -> Path:
    return Path(root) / guard.APP / CHILDREN_DIR / f'{variant_key(monster, variant)}.children.json'


def _create_time(pid: int | None) -> float | None:
    if psutil is None or not pid:
        return None
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def _alive(pid: object, create: object) -> bool:
    """The recorded process (pid + create time) is still running."""
    if psutil is None or not isinstance(pid, int) or pid <= 0:
        return False
    actual = _create_time(pid)
    if actual is None:
        return False
    return not isinstance(create, (int, float)) or abs(actual - float(create)) <= CREATE_TOLERANCE


def recorded_children(root: Path, monster: str, variant: str) -> dict:
    """{driver: {pid, create} | None, children: [{pid, create, argv}]} of the variant's last driver (empty when none)."""
    value = mv.read_json(children_path(root, monster, variant), None)
    if not isinstance(value, dict):
        return {'driver': None, 'children': []}
    kids = [c for c in value.get('children') or [] if isinstance(c, dict)]
    return {'driver': value.get('driver') if isinstance(value.get('driver'), dict) else None, 'children': kids}


def live_children(root: Path, monster: str, variant: str) -> list[dict]:
    """The recorded children of the variant's drivers that still run (verified by pid + create time)."""
    return [c for c in recorded_children(root, monster, variant)['children'] if _alive(c.get('pid'), c.get('create'))]


def clear_children(root: Path, monster: str, variant: str) -> None:
    """After a verified drain: forget the recorded processes (only the dead ones are dropped when some still run)."""
    path = children_path(root, monster, variant)
    data = recorded_children(root, monster, variant)
    alive = [c for c in data['children'] if _alive(c.get('pid'), c.get('create'))]
    if alive:
        guard.write_atomic(path, mv.dump({'driver': data['driver'], 'children': alive}))
    else:
        path.unlink(missing_ok=True)


def stray_trees(root: Path) -> list[str]:
    """set_names (`monster:<M>/<v>`) of variants whose recorded children still run (counted as active by the plan, F14)."""
    out = []
    folder = Path(root) / guard.APP / CHILDREN_DIR
    try:
        paths = list(folder.glob('*.children.json'))
    except OSError:
        return out
    for path in paths:
        value = mv.read_json(path, None)
        if not isinstance(value, dict):
            continue
        if any(_alive(c.get('pid'), c.get('create')) for c in value.get('children') or [] if isinstance(c, dict)):
            out.append(str(value.get('set_name') or path.name))
    return out


class _Tracker:
    """The children file of one driver run: every child is recorded (pid, create time, argv) from right after its start until it exited."""

    def __init__(self, root: Path, monster: str, variant: str) -> None:
        self.path = children_path(root, monster, variant)
        self.set_name = set_name(monster, variant)
        self.driver = {'pid': os.getpid(), 'create': _create_time(os.getpid())}
        self.children: list[dict] = []
        self._write()

    def _write(self) -> None:
        guard.write_atomic(self.path, mv.dump({'set_name': self.set_name, 'driver': self.driver, 'children': self.children}))

    def add(self, proc: subprocess.Popen, argv: list[str]) -> None:
        self.children.append({'pid': proc.pid, 'create': _create_time(proc.pid), 'argv': [str(a) for a in argv[:4]],
                              'started': time.time()})
        self._write()

    def remove(self, proc: subprocess.Popen) -> None:
        self.children = [c for c in self.children if c['pid'] != proc.pid]
        self._write()

    def close(self) -> None:
        if not self.children:
            self.path.unlink(missing_ok=True)


# --------------------------------------------------------------------------------------------------------------- driver

def hyper3d_python() -> str:
    override = os.environ.get('V2_HYPER3D_PYTHON')
    if override:
        return override
    venv = os.environ.get('HYPER3D_VENV') or str(Path(os.environ.get('USERPROFILE') or Path.home()) / '.cache' / 'hyper3d-venv')
    return str(Path(venv) / 'Scripts' / 'python.exe') if os.name == 'nt' else str(Path(venv) / 'bin' / 'python')


def screenshots(out_dir: Path, n: int = 6) -> list[str]:
    try:
        pngs = sorted((p for p in Path(out_dir).iterdir() if p.suffix.lower() == '.png'), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    return [p.as_posix() for p in pngs[:n]]


def stopped_message(stderr: str) -> str | None:
    lines = [l.strip() for l in (stderr or '').splitlines() if l.strip()]
    for line in reversed(lines):
        if line.startswith(('STOPPED:', 'INVALID INPUT:', 'PAID GATE CLOSED:')):
            return line
    return lines[-1][:400] if lines else None


def _run(argv: list[str], cwd: Path, say, env: dict | None = None, tracker: _Tracker | None = None) -> tuple[int, str]:
    """Run one step: stdout goes straight to our stdout (the attempt log), stderr is returned (and echoed). The child is recorded in the
    children file while it runs (F14)."""
    say(f'[monster_lane] $ {" ".join(argv)}')
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), stdout=None, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                                encoding='utf-8', errors='replace', creationflags=NO_WINDOW, env=env)
    except OSError as exc:
        return -1, f'could not start {argv[0]}: {exc}'
    if tracker is not None:
        try:
            tracker.add(proc, argv)
        except OSError:
            pass
    try:
        _out, err = proc.communicate()
    finally:
        if tracker is not None and proc.poll() is not None:
            try:
                tracker.remove(proc)
            except OSError:
                pass
    if err:
        sys.stderr.write(err)
        sys.stderr.flush()
    return proc.returncode, err or ''


def free_preview_resolved(record: dict) -> bool:
    """A `free_preview` intent whose outcome is positively known (F1): the record is `submitted` with a bound, identified model (its id
    and URL), so the free click produced that model. Any other pending click stays protected."""
    model = record.get('model_id')
    return (record.get('pending_action') == 'free_preview' and record.get('status') == 'submitted' and isinstance(model, str) and
            bool(model) and isinstance(record.get('model_url'), str) and model in record['model_url'])


def _sha(path: Path) -> str | None:
    try:
        return mv.sha256_file(path)
    except OSError:
        return None


def snapshot_dir(out_dir: Path, frozen: dict) -> Path:
    return Path(out_dir) / f'inputs_{mv.frozen_digest(frozen)}'


def snapshot_inputs(out_dir: Path, vdir: Path, frozen: dict) -> tuple[Path | None, str | None]:
    """The immutable upload folder of a generation (F7): `hyper3d/inputs_<generation>/` holds exactly the frozen files, read-only, each
    with its frozen sha256 (copied from views/<v>/ and verified; an existing folder is verified again and rebuilt when it differs).
    (folder, None) or (None, problem)."""
    files = [str(f) for f in frozen.get('files') or []]
    digests = frozen.get('sha256') if isinstance(frozen.get('sha256'), dict) else {}
    snap = snapshot_dir(out_dir, frozen)
    if snap.is_dir():
        if sorted(os.listdir(snap)) == sorted(files) and all(_sha(snap / f) == digests.get(f) for f in files):
            return snap, None
        mv._rmtree_own(snap)
    problem = mv.frozen_problem(vdir, frozen)
    if problem:
        return None, f'{problem}; choose the views again'
    tmp = Path(out_dir) / f'.inputs_{mv.frozen_digest(frozen)}_{uuid.uuid4().hex[:8]}'
    tmp.mkdir(parents=True)
    try:
        for name in files:
            shutil.copyfile(Path(vdir) / name, tmp / name)
            if _sha(tmp / name) != digests.get(name):
                raise ValueError(f'{name} changed while it was copied; choose the views again')
            os.chmod(tmp / name, stat.S_IREAD)
        os.rename(tmp, snap)
    except (OSError, ValueError) as exc:
        mv._rmtree_own(tmp)
        return None, str(exc)
    return snap, None


def inputs_problem(record: dict, frozen: dict, snap: Path) -> str | None:
    """None when the submission record was made from exactly the frozen generation (F7): its `inputs` (ordered [{name, sha256, size}],
    written by the backend at submit) equal the frozen files and digests. A record without `inputs` (an older backend) is accepted only
    when it uploaded the frozen names from this generation's verified snapshot."""
    files = [str(f) for f in frozen.get('files') or []]
    digests = frozen.get('sha256') if isinstance(frozen.get('sha256'), dict) else {}
    inputs = record.get('inputs')
    if inputs is not None:
        got = [(i.get('name'), i.get('sha256')) for i in inputs] if isinstance(inputs, list) and all(isinstance(i, dict) for i in inputs) else None
        if got != [(f, digests.get(f)) for f in files]:
            return (f'the submission record was made from other images than the frozen views (record inputs {got}, frozen '
                    f'{[(f, digests.get(f)) for f in files]}); nothing was spent on it by this lane: check it by hand')
        return None
    if record.get('images_uploaded') != files:
        return f'the submission record uploaded {record.get("images_uploaded")!r}, not the frozen views {files}; check it by hand'
    source = record.get('source')
    try:
        same = isinstance(source, str) and Path(source).resolve() == Path(snap).resolve()
    except OSError:
        same = False
    if not same:
        return 'the submission record names no inputs and was not made from the verified snapshot of this choice; check it by hand'
    if not all(_sha(Path(snap) / f) == digests.get(f) for f in files):
        return 'the verified snapshot of this choice changed; check it by hand'
    return None


def run_conversion(root: Path, monster: str, variant: str, *, frozen_sha: str | None = None, run_now: bool = False,
                   override_pause: str | None = None, idle: float | None = None, idle_known: bool = False, python: str | None = None,
                   say=None) -> dict:
    """The driver of one attempt (see the module docstring). Returns the verdict; never raises for a pipeline failure."""
    say = say or (lambda text: print(text, flush=True))
    root, mroot = Path(root), mv.mroot_of(root)
    base = {'monster': monster, 'variant': variant}
    try:
        monster, variant = mv.safe_part(monster, 'monster'), mv.check_variant(variant)
        mv.canonical_variant(mroot, monster, variant)
    except Exception as exc:  # noqa: BLE001  (ValueError, the not-found error of monsters_v2 / catalog)
        return base | {'status': 'failed', 'step': 'check', 'error': str(exc)}
    lock = guard.OsLock(driver_lock_path(root, monster, variant), wait=0)
    try:
        lock.__enter__()
    except guard.Busy:
        return base | {'status': 'busy', 'step': 'check', 'error': 'another conversion driver of this variant is running'}
    try:
        kids = live_children(root, monster, variant)
        if kids:
            return base | {'status': 'busy', 'step': 'check',
                           'error': f'a process of the previous attempt still runs (pid {", ".join(str(k["pid"]) for k in kids)})'}
        tracker = _Tracker(root, monster, variant)
        try:
            return _drive(root, mroot, monster, variant, base, tracker, frozen_sha=frozen_sha, run_now=run_now,
                          override_pause=override_pause, idle=idle, idle_known=idle_known, python=python, say=say)
        finally:
            tracker.close()
    finally:
        lock.__exit__(None, None, None)


def _drive(root: Path, mroot: Path, monster: str, variant: str, base: dict, tracker: _Tracker, *, frozen_sha, run_now, override_pause,
           idle, idle_known, python, say) -> dict:
    vdir, mdir = mv.views_dir(mroot, monster, variant), mv.models_dir(mroot, monster, variant)
    out_dir = mdir / 'hyper3d'
    record_path = out_dir / 'studio_submission.json'
    args = {'run_now': run_now, 'override_pause': override_pause}
    steps: list[str] = []
    accounting: dict = {'error': None}

    def verdict(status: str, step: str, error: str | None = None, **extra) -> dict:
        rec = mv.record_of(mdir)
        paid = paid_actions(rec)
        out = base | {'status': status, 'step': step, 'steps': steps, 'error': error,
                      'record_status': (rec or {}).get('status'), 'paid': any(a['status'] == 'confirmed' for a in paid),
                      'paid_uncertain': [a['action'] for a in paid if a['status'] == 'uncertain'],
                      'accounting_error': accounting['error']} | extra
        if status == 'failed':
            out['screenshots'] = screenshots(out_dir)
            out['record'] = record_path.as_posix() if record_path.is_file() else None
        return out

    def gated(step: str) -> dict | None:
        g = gate(root, args, idle, idle_known)
        return None if g['open'] else verdict('waiting', step, g['reason'])

    def account(files: list[str]) -> None:
        record = mv.record_of(mdir)
        if record is not None or accounting_error(root, record_path):
            accounting['error'] = reconcile_paid(root, monster, variant, files, record_path, record)

    env = dict(os.environ) | paid_gate.env_for(root, override_pause=bool(run_now or override_pause),
                                               cmd=gate_command(root, args, idle, idle_known))
    try:
        vj = mv.read_views_json(vdir)
    except guard.Unreadable as exc:
        return verdict('failed', 'check', str(exc))
    frozen = (vj or {}).get('frozen')
    if not frozen:
        return verdict('failed', 'check', 'the variant has no frozen view list (Use enabled views first)')
    generation = mv.frozen_digest(frozen)
    if frozen_sha and generation != frozen_sha:
        return verdict('failed', 'check', 'the views were chosen again since this conversion was queued')
    files = [str(f) for f in frozen.get('files') or []]
    account(files)                                              # on every resume, also for an exported / reduced record (F5)
    if mv.reduce_status(mdir, generation)['ok']:
        steps.append('reduced already')
        return verdict('ok', 'done')
    script = mroot / 'tools' / ('hyper3d_single.py' if len(files) == 1 else 'hyper3d_multiview.py')
    py = python or hyper3d_python()
    snap = snapshot_dir(out_dir, frozen)

    record = mv.record_of(mdir)
    if record is None:
        if any(out_dir.glob('glb_*.glb')):
            return verdict('failed', 'check', f'{out_dir.as_posix()} has a GLB but no studio_submission.json; inspect it by hand')
        if not 1 <= len(files) <= 4:
            return verdict('failed', 'check', f'{len(files)} frozen views; 1 to 4 are possible')
        problem = mv.frozen_problem(vdir, frozen)
        if problem:
            return verdict('failed', 'check', f'{problem}; choose the views again')
        if not Path(py).is_file():
            return verdict('failed', 'submit', f'the Hyper3D venv python was not found: {py}')
        blocked = gated('submit')
        if blocked:
            return blocked
        out_dir.mkdir(parents=True, exist_ok=True)
        snap, problem = snapshot_inputs(out_dir, vdir, frozen)
        if problem:
            return verdict('failed', 'check', problem)
        plan = ['--view', files[0]] if len(files) == 1 else ['--views', ','.join(files)]
        code, err = _run([py, '-X', 'utf8', str(script), 'submit', str(snap), str(out_dir), *plan], mroot, say, env, tracker)
        steps.append(f'submit exit {code}')
        if code == paid_gate.EXIT_GATE_CLOSED:
            return verdict('waiting', 'submit', stopped_message(err) or 'the paid gate closed')
        if code != 0:
            message = stopped_message(err) or f'submit exit code {code}'
            if code == 3 and BUSY_TEXT in err.lower():
                return verdict('busy', 'submit', message)
            return verdict('failed', 'submit', message)
        record = mv.record_of(mdir)
        if record is None:
            return verdict('failed', 'submit', 'submit finished without a studio_submission.json')

    problem = inputs_problem(record, frozen, snap)
    if problem:
        return verdict('failed', 'check', problem)
    status = record.get('status')
    if status == 'submitted' or (status == 'exported' and not any(out_dir.glob('glb_*.glb'))):
        pending = record.get('pending_action')
        if pending and not free_preview_resolved(record):
            return verdict('failed', 'collect', f'the record has an unresolved {pending!r} click; inspect '
                                                f'{record_path.as_posix()} (hyper3d_single.py inspect) before resuming')
        if not Path(py).is_file():
            return verdict('failed', 'collect', f'the Hyper3D venv python was not found: {py}')
        blocked = gated('collect')
        if blocked:
            return blocked
        code, err = _run([py, '-X', 'utf8', str(script), 'collect', str(out_dir), '--allow-paid', '--accept-preview'], mroot, say, env,
                         tracker)
        steps.append(f'collect exit {code}')
        record = mv.record_of(mdir) or record
        account(files)
        if accounting['error']:
            say(f'[monster_lane] WARNING: {accounting["error"]}')
        if code == paid_gate.EXIT_GATE_CLOSED:
            return verdict('waiting', 'collect', stopped_message(err) or 'the paid gate closed before a paid click')
        if code != 0:
            message = stopped_message(err) or f'collect exit code {code}'
            if code == 3 and BUSY_TEXT in err.lower():
                return verdict('busy', 'collect', message)
            return verdict('failed', 'collect', message)
        status = record.get('status')
    if status != 'exported' or not any(out_dir.glob('glb_*.glb')):
        why = record.get('why') or record.get('pending_action')
        return verdict('failed', 'collect', f'record status {status!r}' + (f' ({why})' if why else '') +
                       f'; inspect {record_path.as_posix()}')

    blocked = gated('reduce')
    if blocked:
        return blocked
    redo = ['--redo'] if (mdir / 'reduced').exists() else []
    code, err = _run([sys.executable, '-X', 'utf8', str(mroot / 'tools' / 'reduce_monster.py'), f'{monster}/{variant}', *redo], mroot, say,
                     None, tracker)
    steps.append(f'reduce exit {code}')
    problem = mv.reduced_files_problem(mdir)
    if code != 0 or problem:
        return verdict('failed', 'reduce', stopped_message(err) or problem or
                       f'reduce_monster.py exit code {code}; see {(mdir / "reduce.log").as_posix()}')
    try:
        mv.write_reduce_ok(mdir, frozen)
    except (OSError, ValueError) as exc:
        return verdict('failed', 'reduce', f'the reduction could not be verified: {exc}')
    return verdict('ok', 'done')


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description='The monster conversion driver of the v2 lane (one attempt) and its gate check.')
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('--root', type=Path, required=True)
    run.add_argument('--monster', required=True)
    run.add_argument('--variant', required=True)
    run.add_argument('--frozen-sha', default='')
    run.add_argument('--run-now', action='store_true')
    run.add_argument('--override-pause', default=None, metavar='PAUSE_KEY')
    check = sub.add_parser('gate', help='exit 0 when a paid step of this task may run now, else 1 with the reason on stdout')
    check.add_argument('--root', type=Path, required=True)
    check.add_argument('--task-args', default='{}')
    check.add_argument('--idle', type=float, default=None)
    check.add_argument('--idle-unknown', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'gate':
        try:
            task_args = json.loads(args.task_args)
            if not isinstance(task_args, dict):
                raise ValueError('not an object')
            g = gate(args.root, task_args, args.idle, args.idle is not None or args.idle_unknown)
        except Exception as exc:  # noqa: BLE001  (a gate that cannot be evaluated is closed)
            print(f'the monster lane gate could not be evaluated ({type(exc).__name__}: {exc})', flush=True)
            return 1
        print('open' if g['open'] else g['reason'], flush=True)
        return 0 if g['open'] else 1
    try:
        result = run_conversion(args.root, args.monster, args.variant, frozen_sha=args.frozen_sha or None, run_now=args.run_now,
                                override_pause=args.override_pause)
    except Exception as exc:  # noqa: BLE001  (the verdict line is always written)
        result = {'status': 'failed', 'step': 'driver', 'error': f'{type(exc).__name__}: {exc}', 'monster': args.monster,
                  'variant': args.variant}
    sys.stdout.flush()
    print(json.dumps(result), flush=True)
    return 0 if result.get('status') == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main())
