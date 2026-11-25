#!/usr/bin/env python3
"""The monster conversion lane of the v2 preview app (DESIGN_PHASE5.md section E; orchestrator lane `monster_convert`, limit 1).

One queue task per (variant, frozen view list): key `monster_convert:<Monster>/<variant>:<frozen digest 12>`, set_name
`monster:<Monster>/<variant>`, unit `convert`, args {monster, variant, files, frozen_sha, frozen_time, run_now?, override_pause?},
priority 4, max_attempts 1 (a paid step is never retried automatically: a failure waits for Retry, which resumes from the record).

The task runs THIS file as its builder (run_task.py wrapper, detached):

    python -X utf8 tools/preview/v2/monster_lane.py run --root <imageto3d root> --monster "<M>" --variant <v> --frozen-sha <12 hex>
           [--run-now] [--override-pause]

and the driver walks the steps, each resumable from `models/<v>/hyper3d/studio_submission.json` (never paying twice):
  1. a record exists -> collect or inspect by its `status` / `actions` / `pending_action`; NO record -> submit (free). A record with a
     paid action or `credits_committed > 0` is never submitted again (the backend itself also refuses any recorded click);
  2. submit: one frozen view -> `hyper3d_single.py submit <views> <out> --view <file>`; 2-4 -> `hyper3d_multiview.py submit <views> <out>
     --views a,b[,c[,d]]` (the frozen order), both with the Hyper3D venv python (`$V2_HYPER3D_PYTHON`, else `$HYPER3D_VENV` /
     `%USERPROFILE%/.cache/hyper3d-venv`, `Scripts/python.exe -X utf8`) from the monster root;
  3. `collect --allow-paid --accept-preview` (PAID, 0.5 credits); a record with a paid action gets ONE entry in the imageto3d
     `.codex_jobs/_app/paid_log.jsonl` {time, kind: "hyper3d", monster, variant, views, credits, record, model_id};
  4. `python tools/reduce_monster.py "<M>/<v>"` (Blender) -> models/<v>/reduced/ with 5 LODs (`--redo` only for an incomplete reduced/).
Before every step the gate is checked again. The LAST stdout line is the verdict JSON: {"status": "ok" | "busy" | "waiting" | "failed", ...}:
`busy` = "Hyper3D profile busy" (another conversion holds the browser: requeue after 5 min, not a failure), `waiting` = the gate closed
(requeue after 1 min), `failed` = the record status, the wrapper's STOPPED message and the screenshot paths.

GATE (identical to conversions): the conversion hold `.codex_jobs/_batch/CONVERT_HOLD` (DESIGN_PHASE4.md I.1: the supervisor is being
stopped / restarted or has not ticked yet) -> wait, and nothing overrides it (convert_queue.py / convert_job.py wait for it the same way);
mode `pause` / `pause-conversions`, or a PAUSE file with `conversions` in its scope for a manual reason -> wait (a Run now pressed while
paused, after its confirmation = `override_pause`, overrides this); mode `auto` / `views-run` -> only when the user is away (idle >= 300 s)
and no "user active" PAUSE -> a per-task Run now (`run_now`) overrides this idle gate only. `gate()['code']` names the reason for the UI:
`held` | `paused` | `user_present` (None when open).

HOOKS for the orchestrator (DESIGN_PHASE5.md H.1): `tick` (auto-freeze + queue the frozen variants without a task), `plan_starts`
(which queued task may start now, which must be cancelled), `task_argv` / `TASK_ENV`, `finish_outcome` (done / requeue / failed of a
result), `task_wanted` (the approval check of this lane), `run_now` (Queue op), `lane_status` / `paid_summary` (Queue tab).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import guard  # noqa: E402
import modes  # noqa: E402
import monsters_v2 as mv  # noqa: E402
import taskqueue  # noqa: E402

LANE = 'monster_convert'
LIMIT = 1
PRIORITY = 4
MAX_ATTEMPTS = 1
BUSY_DELAY = 300.0                 # "Hyper3D profile busy": try again after 5 min
WAIT_DELAY = 60.0                  # the gate closed during the run: look again after 1 min
PAID_LOG = 'paid_log.jsonl'
CREDITS_PER_MODEL = 0.5
IDLE_REASONS = ('user active', 'user active (views keep running)')   # pipeline_supervisor: the screen-on pauses (not manual)
BUSY_TEXT = 'profile busy'
TASK_ENV = {'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1'}
HOLD_FILE = 'CONVERT_HOLD'         # = services.HOLD_FILE (DESIGN_PHASE4.md I.1); read here without importing services (psutil)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


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


def enqueue(root: Path, monster: str, variant: str, frozen: dict, run_now: bool = False) -> int:
    """Queue the conversion of a frozen view list (idempotent per key: an existing task of that freeze is returned unchanged)."""
    if LANE not in taskqueue.LANES:
        raise RuntimeError(f'the {LANE} lane is not registered in taskqueue.LANES')
    files = [str(f) for f in frozen.get('files') or []]
    args = {'monster': monster, 'variant': variant, 'files': files, 'frozen_sha': mv.frozen_digest(frozen),
            'frozen_time': frozen.get('time'), 'run_now': bool(run_now)}
    return queue(root).enqueue(LANE, task_key(monster, variant, frozen), set_name(monster, variant), 'convert', args,
                               priority=PRIORITY, max_attempts=MAX_ATTEMPTS)


def lane_tasks(root: Path, state=None, limit: int = 5000) -> list[dict]:
    return queue(root).tasks(state=state, lane=LANE, limit=limit)


def newest_tasks(root: Path) -> dict[str, dict]:
    """{"<monster>/<variant>": newest task of the lane} (one query; rows come newest first)."""
    out: dict[str, dict] = {}
    for task in lane_tasks(root):
        name = str(task.get('set_name') or '').removeprefix('monster:')
        out.setdefault(name, task)
    return out


def open_task(root: Path, monster: str, variant: str) -> dict | None:
    for task in queue(root).tasks(state=taskqueue.OPEN, lane=LANE, set_name=set_name(monster, variant), limit=10):
        return task
    return None


def task_wanted(root: Path, task: dict) -> tuple[bool, str | None]:
    """(wanted, reason): the lane's approval check. A task is wanted while its variant's views.json still has the frozen list it was queued
    for, the variant is not approved and its reduction is not complete."""
    args = task.get('args') if isinstance(task.get('args'), dict) else {}
    mroot = mv.mroot_of(root)
    try:
        monster, variant = mv.safe_part(args.get('monster'), 'monster'), mv.check_variant(args.get('variant'))
    except ValueError:
        return False, 'the task names no valid monster / variant'
    vj = mv.read_views_json(mv.views_dir(mroot, monster, variant))
    if not vj or not vj.get('frozen'):
        return False, 'the variant has no frozen view list any more'
    if mv.frozen_digest(vj['frozen']) != args.get('frozen_sha'):
        return False, 'the views were chosen again since this conversion was queued'
    mdir = mv.models_dir(mroot, monster, variant)
    if mv.read_obj(mdir / mv.CHOICE).get('approved') is True:
        return False, 'the variant is approved'
    if task.get('state') == 'queued' and mv.reduced_complete(mdir):
        return False, 'the variant is converted and reduced already'
    return True, None


def tick(root: Path) -> dict:
    """HOOK (orchestrator loop, before starting): auto-freeze the unattended variants, then queue every frozen variant whose freeze has no
    task yet (the queue is a derived cache of views.json; a failed task stays failed until Retry). {frozen: [...], queued: [...]}"""
    root = Path(root)
    frozen_now = mv.auto_freeze(root)
    queued = []
    if LANE in taskqueue.LANES:
        keys = {t['key'] for t in lane_tasks(root)}
        mroot = mv.mroot_of(root)
        for entry in mv.frozen_variants(root):
            mdir = mv.models_dir(mroot, entry['monster'], entry['variant'])
            if mv.reduced_complete(mdir) or mv.read_obj(mdir / mv.CHOICE).get('approved') is True:
                continue
            if task_key(entry['monster'], entry['variant'], entry['frozen']) in keys:
                continue
            try:
                enqueue(root, entry['monster'], entry['variant'], entry['frozen'])
                queued.append(f'{entry["monster"]}/{entry["variant"]}')
            except taskqueue.QueueError:
                continue
    if frozen_now or queued:
        mv.invalidate()
    return {'frozen': frozen_now, 'queued': queued}


# ------------------------------------------------------------------------------------------------------------------- gate

def read_hold(root: Path) -> dict | None:
    """The conversion hold (CONVERT_HOLD) as a dict, None when absent; an unreadable file still holds."""
    path = Path(root) / '.codex_jobs' / '_batch' / HOLD_FILE
    if not path.exists():
        return None
    value = mv.read_json(path, None)
    return value if isinstance(value, dict) else {'reason': 'unreadable CONVERT_HOLD file'}


def gate(root: Path, args: dict | None = None, idle: float | None = None, idle_known: bool = False) -> dict:
    """Whether a monster conversion step may run now: {open, reason, code, mode, pause, hold, idle, away, needs_confirm}. `idle` = the
    user's idle seconds (None + idle_known=False: read presence.json / GetLastInputInfo). `code`: held | paused | user_present | None."""
    args = args or {}
    mode = modes.read_mode(root)['mode']
    pause = modes.read_pause(root)
    hold = read_hold(root)
    if idle is None and not idle_known:
        idle, _source = modes.idle_seconds()
    away = idle is not None and idle >= modes.AWAY_AFTER
    run_now, override = bool(args.get('run_now')), bool(args.get('override_pause'))
    out = {'open': True, 'reason': None, 'code': None, 'mode': mode, 'pause': pause, 'hold': hold, 'idle': idle, 'away': away,
           'needs_confirm': False}
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


def plan_starts(root: Path, now: float | None = None, idle: float | None = None, idle_known: bool = False) -> dict:
    """HOOK (orchestrator `_start_queued`): {start: [task] (0 or 1), cancel: [(task, reason)], waiting: {task id: reason}, active: n}.
    At most LIMIT attempts of the lane run at once; a queued task whose freeze is gone is to be cancelled; the first queued task whose
    gate is open is started (the gate is per task: Run now opens it for that task only)."""
    q = queue(root)
    active = q.tasks(state=taskqueue.ACTIVE, lane=LANE, limit=100)
    out = {'start': [], 'cancel': [], 'waiting': {}, 'active': len(active)}
    if idle is None and not idle_known:
        idle, _source = modes.idle_seconds()
        idle_known = True
    for task in q.next_queued(LANE, 50, now):
        wanted, why = task_wanted(root, task)
        if not wanted:
            out['cancel'].append((task, why))
            continue
        if len(active) + len(out['start']) >= LIMIT:
            out['waiting'][task['id']] = 'another monster conversion is running'
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
    if args.get('override_pause'):
        argv.append('--override-pause')
    return argv


def finish_outcome(root: Path, task: dict, result: dict) -> dict:
    """HOOK (orchestrator `_finish` for this lane): {outcome: done | requeue | failed, error, delay}. `done` needs exit 0, the verdict
    `ok` AND the five reduced levels on disk; `busy` / `waiting` are requeued (not failures, attempts not counted against a retry);
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
        args = task.get('args') or {}
        mroot = mv.mroot_of(root)
        try:
            mdir = mv.models_dir(mroot, mv.safe_part(args.get('monster'), 'monster'), mv.check_variant(args.get('variant')))
        except ValueError:
            return {'outcome': 'failed', 'error': 'the task names no valid monster / variant', 'delay': 0.0}
        if mv.reduced_complete(mdir):
            return {'outcome': 'done', 'error': None, 'delay': 0.0}
        return {'outcome': 'failed', 'error': 'the driver reported ok but the reduced levels are missing', 'delay': 0.0}
    if last is None:
        return {'outcome': 'failed', 'error': f'the driver exited {result.get("exit")} without a verdict on stdout', 'delay': 0.0}
    return {'outcome': 'failed', 'error': str(last.get('error') or f'exit code {result.get("exit")}'), 'delay': 0.0}


def run_now(root: Path, task_id: object, confirm: bool = False) -> dict:
    """HOOK (POST /api/v2/queue {op: "run_now", id, confirm?}): mark a queued (or failed / cancelled: queued again) monster conversion to
    run now. It overrides the idle gate; while conversions are paused by hand it needs `confirm: true` (409 `confirm` otherwise) and then
    overrides the pause for this task too. The conversion hold (CONVERT_HOLD) is never overridden: the task is marked and starts when the
    hold lifts (the answer carries a `warning`)."""
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
        wanted, why = task_wanted(root, task)
        if not wanted:
            raise ValueError(f'task {task_id} cannot run: {why}')
        q.retry(task_id)
    elif task['state'] != 'queued':
        raise ValueError(f'task {task_id} is {task["state"]}; Run now is for a queued task')
    override = bool(confirm and g['needs_confirm'])
    if not q.mark_run_now(task_id, override):
        raise ValueError(f'task {task_id} is no longer queued')
    mv.invalidate()
    out = {'ok': True, 'id': task_id, 'state': 'queued', 'run_now': True, 'override_pause': override}
    if g['code'] == 'held':
        out['warning'] = g['reason']
    return out


# ------------------------------------------------------------------------------------------------------------- paid log

def paid_log_path(root: Path) -> Path:
    return Path(root) / guard.APP / PAID_LOG


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


def log_paid(root: Path, monster: str, variant: str, files: list[str], record_path: Path, record: dict) -> bool:
    """One entry per paid record (record path + model id); False when it was logged already."""
    rec_text = Path(record_path).as_posix()
    with guard.os_lock(Path(root) / guard.APP / 'paid_log.lock'):
        for entry in paid_entries(root):
            if entry.get('record') == rec_text and entry.get('model_id') == record.get('model_id'):
                return False
        credits = record.get('credits_committed')
        entry = {'time': guard.now(), 'kind': 'hyper3d', 'monster': monster, 'variant': variant, 'views': list(files),
                 'credits': credits if isinstance(credits, (int, float)) and not isinstance(credits, bool) and credits > 0 else CREDITS_PER_MODEL,
                 'record': rec_text, 'model_id': record.get('model_id')}
        path = paid_log_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry) + '\n')
    return True


def paid_summary(root: Path) -> dict:
    entries = [e for e in paid_entries(root) if e.get('kind') == 'hyper3d']
    return {'count': len(entries), 'credits': round(sum(float(e.get('credits') or 0) for e in entries), 3), 'recent': entries[-10:]}


def lane_status(root: Path) -> dict:
    """HOOK (GET /api/v2/queue `monsters`): the lane's gate, counts and the paid log summary."""
    totals: dict[str, int] = {}
    for task in lane_tasks(root):
        totals[task['state']] = totals.get(task['state'], 0) + 1
    return {'lane': LANE, 'limit': LIMIT, 'gate': {k: v for k, v in gate(root).items() if k not in ('pause', 'hold')}, 'totals': totals,
            'paid': paid_summary(root), 'hold': read_hold(root)}


# --------------------------------------------------------------------------------------------------------------- driver

def hyper3d_python() -> str:
    override = os.environ.get('V2_HYPER3D_PYTHON')
    if override:
        return override
    venv = os.environ.get('HYPER3D_VENV') or str(Path(os.environ.get('USERPROFILE') or Path.home()) / '.cache' / 'hyper3d-venv')
    return str(Path(venv) / 'Scripts' / 'python.exe') if os.name == 'nt' else str(Path(venv) / 'bin' / 'python')


def has_paid(record: dict | None) -> bool:
    if not isinstance(record, dict):
        return False
    actions = record.get('actions') if isinstance(record.get('actions'), dict) else {}
    credits = record.get('credits_committed')
    return any(k in actions for k in ('geometry', 'material_generate', 'material_confirm')) or \
        (isinstance(credits, (int, float)) and not isinstance(credits, bool) and credits > 0)


def screenshots(out_dir: Path, n: int = 6) -> list[str]:
    try:
        pngs = sorted((p for p in Path(out_dir).iterdir() if p.suffix.lower() == '.png'), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    return [p.as_posix() for p in pngs[:n]]


def stopped_message(stderr: str) -> str | None:
    lines = [l.strip() for l in (stderr or '').splitlines() if l.strip()]
    for line in reversed(lines):
        if line.startswith(('STOPPED:', 'INVALID INPUT:')):
            return line
    return lines[-1][:400] if lines else None


def _run(argv: list[str], cwd: Path, say) -> tuple[int, str]:
    """Run one step: stdout goes straight to our stdout (the attempt log), stderr is returned (and echoed)."""
    say(f'[monster_lane] $ {" ".join(argv)}')
    try:
        proc = subprocess.run(argv, cwd=str(cwd), stdout=None, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                              text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
    except OSError as exc:
        return -1, f'could not start {argv[0]}: {exc}'
    if proc.stderr:
        sys.stderr.write(proc.stderr)
        sys.stderr.flush()
    return proc.returncode, proc.stderr or ''


def run_conversion(root: Path, monster: str, variant: str, *, frozen_sha: str | None = None, run_now: bool = False,
                   override_pause: bool = False, idle: float | None = None, idle_known: bool = False, python: str | None = None,
                   say=None) -> dict:
    """The driver of one attempt (see the module docstring). Returns the verdict; never raises for a pipeline failure."""
    say = say or (lambda text: print(text, flush=True))
    root, mroot = Path(root), mv.mroot_of(root)
    base = {'monster': monster, 'variant': variant}
    try:
        monster, variant = mv.safe_part(monster, 'monster'), mv.check_variant(variant)
    except ValueError as exc:
        return base | {'status': 'failed', 'step': 'check', 'error': str(exc)}
    vdir, mdir = mv.views_dir(mroot, monster, variant), mv.models_dir(mroot, monster, variant)
    out_dir = mdir / 'hyper3d'
    record_path = out_dir / 'studio_submission.json'
    args = {'run_now': run_now, 'override_pause': override_pause}
    steps: list[str] = []

    def verdict(status: str, step: str, error: str | None = None, **extra) -> dict:
        rec = mv.record_of(mdir)
        out = base | {'status': status, 'step': step, 'steps': steps, 'error': error,
                      'record_status': (rec or {}).get('status'), 'paid': has_paid(rec)} | extra
        if status == 'failed':
            out['screenshots'] = screenshots(out_dir)
            out['record'] = record_path.as_posix() if record_path.is_file() else None
        return out

    def gated(step: str) -> dict | None:
        g = gate(root, args, idle, idle_known)
        return None if g['open'] else verdict('waiting', step, g['reason'])

    vj = mv.read_views_json(vdir)
    frozen = (vj or {}).get('frozen')
    if not frozen:
        return verdict('failed', 'check', 'the variant has no frozen view list (Use enabled views first)')
    if frozen_sha and mv.frozen_digest(frozen) != frozen_sha:
        return verdict('failed', 'check', 'the views were chosen again since this conversion was queued')
    files = [str(f) for f in frozen.get('files') or []]
    if mv.reduced_complete(mdir):
        steps.append('reduced already')
        return verdict('ok', 'done')
    script = mroot / 'tools' / ('hyper3d_single.py' if len(files) == 1 else 'hyper3d_multiview.py')
    py = python or hyper3d_python()

    record = mv.record_of(mdir)
    if record is None:
        if any(out_dir.glob('glb_*.glb')):
            return verdict('failed', 'check', f'{out_dir.as_posix()} has a GLB but no studio_submission.json; inspect it by hand')
        problem = mv.frozen_problem(vdir, frozen)
        if problem:
            return verdict('failed', 'check', f'{problem}; choose the views again')
        if not 1 <= len(files) <= 4:
            return verdict('failed', 'check', f'{len(files)} frozen views; 1 to 4 are possible')
        if not Path(py).is_file():
            return verdict('failed', 'submit', f'the Hyper3D venv python was not found: {py}')
        blocked = gated('submit')
        if blocked:
            return blocked
        plan = ['--view', files[0]] if len(files) == 1 else ['--views', ','.join(files)]
        code, err = _run([py, '-X', 'utf8', str(script), 'submit', str(vdir), str(out_dir), *plan], mroot, say)
        steps.append(f'submit exit {code}')
        if code != 0:
            message = stopped_message(err) or f'submit exit code {code}'
            if code == 3 and BUSY_TEXT in err.lower():
                return verdict('busy', 'submit', message)
            return verdict('failed', 'submit', message)
        record = mv.record_of(mdir)
        if record is None:
            return verdict('failed', 'submit', 'submit finished without a studio_submission.json')

    status = record.get('status')
    if status == 'submitted' or (status == 'exported' and not any(out_dir.glob('glb_*.glb'))):
        if record.get('pending_action'):
            return verdict('failed', 'collect', f'the record has an unresolved {record["pending_action"]!r} click; inspect '
                                                f'{record_path.as_posix()} (hyper3d_single.py inspect) before resuming')
        if not Path(py).is_file():
            return verdict('failed', 'collect', f'the Hyper3D venv python was not found: {py}')
        blocked = gated('collect')
        if blocked:
            return blocked
        code, err = _run([py, '-X', 'utf8', str(script), 'collect', str(out_dir), '--allow-paid', '--accept-preview'], mroot, say)
        steps.append(f'collect exit {code}')
        record = mv.record_of(mdir) or record
        if has_paid(record):
            try:
                if log_paid(root, monster, variant, files, record_path, record):
                    say(f'[monster_lane] paid: logged {record.get("credits_committed") or CREDITS_PER_MODEL} credits')
            except OSError as exc:
                say(f'[monster_lane] WARNING: the paid log could not be written: {exc}')
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
    code, err = _run([sys.executable, '-X', 'utf8', str(mroot / 'tools' / 'reduce_monster.py'), f'{monster}/{variant}', *redo], mroot, say)
    steps.append(f'reduce exit {code}')
    if code != 0 or not mv.reduced_complete(mdir):
        return verdict('failed', 'reduce', stopped_message(err) or f'reduce_monster.py exit code {code}; see {(mdir / "reduce.log").as_posix()}')
    return verdict('ok', 'done')


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description='The monster conversion driver of the v2 lane (one attempt).')
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('--root', type=Path, required=True)
    run.add_argument('--monster', required=True)
    run.add_argument('--variant', required=True)
    run.add_argument('--frozen-sha', default='')
    run.add_argument('--run-now', action='store_true')
    run.add_argument('--override-pause', action='store_true')
    args = parser.parse_args(argv)
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
