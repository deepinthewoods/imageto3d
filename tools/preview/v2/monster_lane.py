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
     before anything is spent (F7); a record of a PROVEN pre-click failure (`rodin_backend.proven_pre_click`; the wrappers archive
     it themselves as `studio_submission.not_submitted_<time>.json`) that is still in place is archived through
     `rodin_backend.set_aside_pre_click` and the variant is unsubmitted again (Retry / Use enabled views submit again; a record with any click intent stays and stops for review); collect or inspect by `status` / `actions` / `pending_action` (a `free_preview` intent of a record
     whose model is bound and identified is resolved, F1; any other pending click is refused). NO record -> submit (free): the frozen
     files are first copied to `models/<v>/hyper3d/inputs_<generation>/` (read-only, sha256-verified) and THAT folder is uploaded (F7):
     one view -> `hyper3d_single.py submit <snapshot> <out> --view <file>`; 2-4 -> `hyper3d_multiview.py submit <snapshot> <out> --views
     a,b[,c[,d]]` (the frozen order), with the Hyper3D venv python (`$V2_HYPER3D_PYTHON`, else `$HYPER3D_VENV` /
     `%USERPROFILE%/.cache/hyper3d-venv`, `Scripts/python.exe -X utf8`) from the monster root;
  3. `collect --allow-paid --accept-preview` (PAID, 0.5 credits). Every wrapper child gets the click-time gate (DESIGN_PHASE4.md I.8):
     `paid_gate.env_for(root, override_pause, cmd=[python, -X, utf8, monster_lane.py, gate, --root, R, --task-args, <json>])`; a child that
     stops at a closed gate exits 4 (paid_gate.EXIT_GATE_CLOSED) = verdict `waiting` (requeue), never a failure;
  4. `python tools/reduce_monster.py "<M>/<v>" --glb <receipted GLB> --sha256 <its digest>` (Blender) -> models/<v>/reduced/. The
     model reduced is the one the record's export receipt names (`exports.GLB` {path, sha256, bytes}; receipted_glb): its bytes are
     hashed BEFORE the reducer starts and AGAIN after it ended, the reducer itself refuses a file that is not that digest, and its
     materials.json must name that file and digest. Another `glb_*.glb` in the folder is never reduced, and a missing receipted file
     is never replaced by one (final-live review, "reduction can certify the wrong model"). Then every output is validated and
     the generation-bound success record `reduced/v2_reduce_ok.json` (with `source` {file, sha256}) is written (F12; `--redo` only
     for an existing reduced/).
Every child is started with Popen and recorded in `.codex_jobs/_app/monster_lane/<variant key>.children.json` ({driver, children: [{pid,
create, argv}]}) until it exited, so Kill & retry can drain the whole process tree and a slot is never released while a child of the
attempt still runs (F14).
Before every step the gate is checked again. The LAST stdout line is the verdict JSON: {"status": "ok" | "busy" | "waiting" | "failed", ...}.

GATE (identical to conversions): the conversion hold `.codex_jobs/_batch/CONVERT_HOLD` and the conversion drain
`.codex_jobs/_batch/CONVERT_DRAIN` -> wait, never overridden (final-verify new 1; = paid_gate.file_reason); mode `pause` /
`pause-conversions`, or a PAUSE file with `conversions` in its scope for a manual reason -> wait; a Run now confirmed while paused stores
the PAUSE ID it overrode (`override_pause` = the `pause_id` of modes.manual_pause: unique per pause transition, recheck finding 5; ONE id
for ALL independent manual pauses in force, final-verify row 5: a manual PAUSE added after the confirmation of a pause mode is a new id)
and overrides only that (a new pause needs a new confirmation; an ordinary Retry clears Run now and the override, F4); a supervisor_mode.json that exists but
cannot be read -> wait (`held`, `mode_unreadable`), never overridden and Run now is refused until it is readable (nobody can tell
whether conversions are paused: final-live review, "payment without pause confirmation"); mode `auto` /
`views-run` -> only when the user is away (idle >= 300 s) and no "user active" PAUSE -> a per-task Run now overrides this idle gate only.
`gate()['code']`: `held` (hold, or drain with `draining`) | `paused` | `user_present` (None when open). The paid children ask the same
`gate()` through this file's `gate` sub-command right before every credit-capable click.

HOOKS for the orchestrator / actions (DESIGN_PHASE5.md H.1): `tick`, `plan_starts`, `task_argv` / `TASK_ENV`, `finish_outcome`,
`record_failure`, `task_wanted`, `run_now`, `cancel_task`, `retry_task`, `active_reason`, `recorded_children` / `clear_children`,
`lane_status` / `paid_summary`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
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
IDLE_REASONS = modes.IDLE_REASONS  # pipeline_supervisor: the screen-on pauses (not manual)
BUSY_TEXT = 'profile busy'
TASK_ENV = {'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1'}
HOLD_FILE = 'CONVERT_HOLD'         # = services.HOLD_FILE (DESIGN_PHASE4.md I.1); read here without importing services (psutil)
DRAIN_FILE = 'CONVERT_DRAIN'       # = services.DRAIN_FILE (DESIGN_PHASE4.md I.10)
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
CHILDREN_DIR = 'monster_lane'
CREATE_TOLERANCE = 1.0


class ConfirmPaused(guard.Conflict):
    """Run now while conversions are paused by hand (HTTP 409 `confirm`): the server's CHALLENGE. Its body carries `pause_id` (the
    challenge id: one identity for the exact set of pauses in force, each bound to its source and file revision) and `pauses`
    ([{pause_id, kind, reason}]: every pause the user must be shown). The client shows those reasons, then sends `confirm: true` AND
    that `pause_id` (finding 5); the server accepts only while the pauses in force still equal the challenge, and answers a new
    challenge otherwise (final-live review, "undisclosed maintenance override")."""
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


def resolve_task(root: Path, task_id: object, key: object, generation: object) -> dict:
    """The lane row the client DISPLAYED (finding 4): found by its `key`, with the frozen `generation` it was shown for; `task_id` is a
    hint only (a rebuilt database assigns new row numbers, so an id may name another variant's task by now). 409 `changed` when that
    identity is not in the queue any more; 400 without a key / generation."""
    if not isinstance(key, str) or not key or not isinstance(generation, str) or not generation:
        raise ValueError('key and generation are required: the conversion task the client displayed (a row number alone does not '
                         'identify it)')
    q = queue(root)
    task = q.by_key(key)
    if task is None and not q.health_view().get('ok'):
        raise guard.Unreadable('the task queue cannot be read; try again shortly')
    args = task.get('args') if task is not None and isinstance(task.get('args'), dict) else {}
    if task is None or task['lane'] != LANE or args.get('frozen_sha') != generation or not key.endswith(f':{generation}'):
        raise guard.Changed('the conversion task you were shown is not in the queue any more (the queue was rebuilt, or the views were '
                            'chosen again); reload and look again', changes=['task'])
    return task


def task_ident(task: dict) -> dict:
    """{key, generation} of a lane row: what every state change compares in its own transaction (taskqueue.check_identity)."""
    return {'key': task['key'], 'generation': (task.get('args') or {}).get('frozen_sha')}


# A disposition that could not be written to views.json is OUTSTANDING DURABLE WORK (finding 3): the intent is first written to
# `.codex_jobs/_app/monster_lane/<variant key>.disposition.json` {pending: {<generation>: {value, time, error}}}, then views.json; the
# intent is removed only once views.json carries it. While an intent is pending the orchestrator does NOT finish the row, every tick
# retries the write (repair_dispositions), nothing queues that generation again, and the card / item / Queue errors show it.

PENDING_SUFFIX = '.disposition.json'


def pending_path(root: Path, monster: str, variant: str) -> Path:
    return Path(root) / guard.APP / CHILDREN_DIR / f'{variant_key(monster, variant)}{PENDING_SUFFIX}'


def _pending_lock(root: Path, monster: str, variant: str) -> guard.OsLock:
    return guard.os_lock(Path(root) / guard.APP / 'locks' / 'mdisposition' / f'{variant_key(monster, variant)}.lock')


def _read_pending(path: Path) -> dict:
    """{generation: intent} of a pending-dispositions file ({} when absent). A file that cannot be read is `{"*": {error}}`: it blocks
    every generation of the variant (indeterminate), and it is never replaced."""
    try:
        value = mv.read_strict(path, path.name)
    except guard.Unreadable as exc:
        return {'*': {'state': None, 'error': str(exc), 'unreadable': True}}
    if value is None:
        return {}
    pending = value.get('pending')
    if not isinstance(pending, dict) or any(not isinstance(v, dict) for v in pending.values()):
        return {'*': {'state': None, 'error': f'{path.name}: not a pending-dispositions file', 'unreadable': True}}
    return pending


def pending_disposition(root: Path, monster: str, variant: str, generation: str) -> dict | None:
    """The outstanding disposition write of a generation ({state, error, time, ...}), or None."""
    try:
        pending = _read_pending(pending_path(root, monster, variant))
    except ValueError:
        return None
    return pending.get(generation) or pending.get('*')


def _write_pending(path: Path, monster: str, variant: str, pending: dict) -> None:
    if pending:
        guard.write_atomic(path, mv.dump({'schema': 1, 'monster': monster, 'variant': variant, 'pending': pending}))
    else:
        path.unlink(missing_ok=True)


def persist_disposition(root: Path, monster: str, variant: str, generation: str, value: dict) -> dict:
    """Durably record `value` ({state, reason, key}) for a generation (finding 3): the intent file first, then views.json
    (mv.set_disposition, idempotent for the same state), then the intent is dropped. ANY failure raises and leaves the intent (with the
    error) behind; the caller must not finish the queue row then."""
    path = pending_path(root, monster, variant)
    with _pending_lock(root, monster, variant):
        pending = _read_pending(path)
        if '*' in pending:
            raise guard.Unreadable(pending['*']['error'])
        intent = {'state': value.get('state'), 'value': dict(value), 'time': guard.now(), 'error': None}
        pending[generation] = intent
        _write_pending(path, monster, variant, pending)        # the durable intent FIRST
        try:
            out = mv.set_disposition(root, monster, variant, generation, value)
        except BaseException as exc:
            intent['error'] = f'{type(exc).__name__}: {exc}'[:400]
            try:
                _write_pending(path, monster, variant, pending)
            except OSError:
                pass
            raise
        pending.pop(generation, None)
        _write_pending(path, monster, variant, pending)
    return out


def repair_dispositions(root: Path) -> list[str]:
    """HOOK (tick): write every outstanding disposition again. Returns the ones still open (shown in the orchestrator errors)."""
    out = []
    folder = Path(root) / guard.APP / CHILDREN_DIR
    try:
        paths = sorted(folder.glob(f'*{PENDING_SUFFIX}'))
    except OSError:
        return out
    for path in paths:
        try:
            value = mv.read_strict(path, path.name) or {}
            monster, variant = mv.safe_part(value.get('monster'), 'monster'), mv.check_variant(value.get('variant'))
            if path != pending_path(root, monster, variant):
                raise guard.Unreadable(f'{path.name} does not belong to {monster}/{variant}')
            for generation, intent in sorted(_read_pending(path).items()):
                if generation == '*' or not isinstance(intent.get('value'), dict):
                    raise guard.Unreadable(str(intent.get('error') or f'{path.name}: invalid entry'))
                try:
                    persist_disposition(root, monster, variant, generation, intent['value'])
                except Exception as exc:  # noqa: BLE001  (kept; retried at the next tick)
                    out.append(f'{monster}/{variant}: the {intent.get("state")} of its conversion is not recorded yet '
                               f'({type(exc).__name__}: {exc})')
        except Exception as exc:  # noqa: BLE001
            out.append(f'{path.name}: {type(exc).__name__}: {exc}')
    return out


def record_failure(root: Path, task: dict, error: str | None) -> None:
    """HOOK (orchestrator, BEFORE it fails the row): persist `failed` for the task's generation (F4). RAISES when the record cannot be
    written (finding 3): the orchestrator then leaves the row unfinished and tries again at its next pass; the intent stays on disk."""
    gen = _generation_of(task)
    if gen:
        persist_disposition(root, gen[0], gen[1], gen[2], {'state': 'failed', 'reason': str(error or 'failed')[:300], 'key': task.get('key')})


CANCEL_PASSES = 5                  # cancel_task: how often it looks again while the row keeps changing under it


def cancel_task(root: Path, task: dict, detail: str = 'manual') -> bool:
    """HOOK (Queue tab Cancel of a monster row; `task` = resolve_task's row, which may be STALE by now): persist `cancelled` for the
    generation FIRST, then cancel the queued row in a transaction that compares the displayed identity (finding 4).

    A Cancel is never lost and never answers success while the generation stays runnable (last-check review, new blocker + row 3).
    `task` only says what the client was shown; every pass looks the row up again by its EXACT key and acts on what is there NOW:
      - a QUEUED row of that generation (also when the client was shown it cancelled: the queue was rebuilt and the generation queued
        again because its durable disposition was missing) -> the durable `cancelled` is written first, as a COMPARE-AND-SET
        (`mv.set_disposition(..., only_absent=True)`, under the monster lock: a recorded failure is never replaced, final-verify row
        3), then the row is cancelled with the identity compared in the same transaction;
      - a CANCELLED row, or no row at all for a Cancel that was shown a cancelled one (a rebuilt queue that has not been ticked) ->
        the durable `cancelled` is (re)written when the generation has no disposition;
      - anything else (it started, failed, finished): a Cancel that was shown a QUEUED row answers False (only OUR OWN write is taken
        back, and only while no newer disposition was written: the variant's monotonic sequence); a REPEATED Cancel (shown a
        cancelled row) answers 409 `changed` (guard.Changed).
    Success (True) is answered only after a last look finds NO runnable row of the generation, and for a repeated Cancel only when the
    durable disposition says `cancelled` (or the variant has no views.json any more: nothing frozen, nothing runnable); otherwise the
    passes go on, and 409 `changed` is the answer when the state keeps changing. False when the row the client saw queued is not
    queued (and not cancelled) any more."""
    q = queue(root)
    if task['lane'] != LANE:
        raise mv.not_found(f'Unknown monster conversion task {task["id"]}')
    shown = task['state']
    if shown not in ('queued', 'cancelled'):
        return False
    gen = _generation_of(task)
    ident = task_ident(task)
    value = {'state': 'cancelled', 'reason': 'cancelled by the user', 'key': task['key']}
    own: list[dict] = []                                        # our own durable write (taken back when the Cancel loses)

    def take_back() -> None:
        if own:
            mv.set_disposition(root, gen[0], gen[1], gen[2], None, expect_seq=own.pop()['seq'])

    def changed(text: str) -> guard.Changed:
        return guard.Changed(f'{text}; reload and look again', changes=['task'])

    def row_now() -> dict | None:
        row = q.by_key(task['key'])
        if row is None and not q.health_view().get('ok'):
            raise guard.Unreadable('the task queue cannot be read; try again shortly')
        if row is not None and (row['lane'] != LANE or (row.get('args') or {}).get('frozen_sha') != ident['generation']):
            take_back()
            raise changed(f'the task {task["key"]!r} belongs to another choice of views now')
        return row

    for _pass in range(CANCEL_PASSES):
        row = row_now()
        state = row['state'] if row is not None else None
        if state not in ('queued', 'cancelled', None) or (state is None and shown == 'queued'):
            take_back()
            if shown == 'queued' and state is not None:
                return False                                    # it started / failed / finished: the newer state stands
            raise changed('the conversion task you were shown is not in the queue any more' if state is None else
                          f'the conversion is {state} now, not cancelled')
        durable = None                                          # the generation's disposition after the compare-and-set
        if gen:
            wrote = mv.set_disposition(root, gen[0], gen[1], gen[2], value, only_absent=True)   # the durable disposition FIRST
            if wrote.get('written'):
                own[:] = [wrote]
            durable = 'missing' if wrote.get('missing') else 'cancelled' if wrote.get('written') or wrote.get('same') else \
                str((wrote.get('previous') or {}).get('state'))
        if state == 'queued':
            try:
                done = q.cancel(row['id'], detail, manual_gen='manual', expect=ident)
            except taskqueue.Expired as exc:
                take_back()
                raise changed(str(exc)) from None
            if not done:
                continue                                        # the row moved meanwhile: look again
        if shown == 'cancelled' and durable not in ('cancelled', 'missing', None):
            raise changed(f'the conversion of this choice is {durable} now, not cancelled')   # (not runnable either: it has a disposition)
        last = row_now()                                        # NO runnable row may be left (the queue may have been rebuilt)
        if last is None or last['state'] == 'cancelled':
            return True
    take_back()
    raise changed('the conversion task keeps changing')


def shown_outcome(root: Path, task: dict) -> dict:
    """What a failed / cancelled lane row is PUBLISHED with (final-live review, blocker 4): {conversion_seq, disposition, error} from
    ONE read of the variant's views.json. `conversion_seq` = the variant's monotonic counter (mv.disposition_seq), `disposition` =
    the recorded outcome of the row's generation in that same snapshot, `error` = the text to show: the row's own when the row
    carries that very outcome, else the disposition's reason (mv.outcome_error). The row was read at another moment than
    views.json, so a row read before a Retry and a second failure is shown with the SECOND failure and its revision: the Retry the
    user presses clears the failure they were shown, never one they were not. `conversion_seq` None when the variant cannot be
    read (display only: the server refuses a Retry without it)."""
    out = {'conversion_seq': None, 'disposition': None, 'error': task.get('error')}
    gen = _generation_of(task)
    if not gen:
        return out
    try:
        vj = mv.read_views_json(mv.views_dir(mv.mroot_of(root), gen[0], gen[1]))
        seq = mv.disposition_seq(vj)
        try:
            disposition = mv.disposition_of(vj, gen[2])
        except guard.Unreadable:
            return out | {'conversion_seq': seq}
        return {'conversion_seq': seq, 'disposition': disposition,
                'error': mv.outcome_error(disposition, task.get('state'), task.get('error'))}
    except Exception:  # noqa: BLE001
        return out


def shown_seq(root: Path, task: dict) -> int | None:
    """The disposition revision a lane row is DISPLAYED with (queue row / item.conversion `conversion_seq`): shown_outcome's."""
    return shown_outcome(root, task)['conversion_seq']


def retry_task(root: Path, task: dict, seq: object = None) -> bool:
    """HOOK (Retry of a failed / cancelled monster row, also Run now of one; `task` = resolve_task's row; `seq` = the disposition
    revision the client DISPLAYED, `conversion_seq` of the queue row / item.conversion): the generation's disposition is cleared FIRST
    (the plan would cancel the queued row otherwise), then the row is queued again in a transaction that compares the displayed
    identity; Retry also clears Run now and its pause override (taskqueue.retry).

    A stale Retry never erases a newer outcome (post-cut-over review, blocker 4):
      - the disposition is cleared as a COMPARE-AND-SET on the variant's monotonic counter (`mv.set_disposition(..., expect_seq=seq)`,
        under the monster lock): when anything was recorded since the client was shown the row (a newer failure, a Cancel, another
        Retry), nothing is cleared and the answer is 409 `changed`; a Retry of a failed / cancelled row WITHOUT `seq` is refused (400);
      - when the queue transition then loses (the row is not failed / cancelled any more: False; it is another task by now: 409
        `changed`; the queue raised), the disposition this call cleared is PUT BACK (only while no newer one was written), so a
        rebuilt queue restores the row as failed / cancelled and never makes that conversion runnable."""
    q = queue(root)
    if task['lane'] != LANE:
        raise mv.not_found(f'Unknown monster conversion task {task["id"]}')
    gen = _generation_of(task)
    cleared = None
    if gen and task['state'] in ('failed', 'cancelled'):
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise ValueError('conversion_seq is required: the disposition revision of the conversion task the client displayed (queue '
                             'row / item.conversion `conversion_seq`)')
        cleared = mv.set_disposition(root, gen[0], gen[1], gen[2], None, expect_seq=seq)
        if cleared.get('stale'):
            raise guard.Changed('the outcome of this conversion changed since you were shown it (a newer failure, cancel or retry was '
                                'recorded); reload and look again', changes=['task'])

    def put_back() -> None:
        if cleared and cleared.get('written') and isinstance(cleared.get('previous'), dict):
            mv.set_disposition(root, gen[0], gen[1], gen[2], cleared['previous'], expect_seq=cleared['seq'])

    try:
        done = q.retry(task['id'], expect=task_ident(task))
    except taskqueue.Expired as exc:
        put_back()
        raise guard.Changed(f'{exc}; reload and look again', changes=['task']) from None
    except BaseException:
        put_back()
        raise
    if not done:
        put_back()                                              # the row moved on: its recorded outcome stands, never erased
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
    return tree_hold(root, monster, variant)


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
    errors += repair_dispositions(root)                         # outstanding durable work first (finding 3)
    recovered += mv.reconcile_descriptors(root, errors)          # no unguarded / old-named monster view task waits for the runner (row 10)
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
                pending = pending_disposition(root, entry['monster'], entry['variant'], entry['digest'])
                if pending is not None:                         # indeterminate: never reconstructed automatically (finding 3)
                    errors.append(f'{name}: the outcome of its conversion is not recorded in views.json yet '
                                  f'({pending.get("error") or "pending"}); it is not queued again until that is written')
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


def read_drain(root: Path) -> dict | None:
    """The conversion drain (CONVERT_DRAIN, services.py I.10) as a dict, None when absent; an unreadable file still drains. Like the
    hold it closes the lane gate and is never overridden by Run now (final-verify review, new finding 1; = paid_gate.file_reason)."""
    path = Path(root) / '.codex_jobs' / '_batch' / DRAIN_FILE
    if not path.exists():
        return None
    value = mv.read_json(path, None)
    return value if isinstance(value, dict) else {'reason': 'unreadable CONVERT_DRAIN file'}


def pause_id(root: Path) -> str | None:
    """The `pause_id` of the manual pause(s) of conversions now in force (modes.manual_pause: ONE identity for all independent manual
    pauses; None when there is none). A confirmed Run now overrides exactly this set (finding 5; final-verify row 5): every pause
    transition has its own id, also two within one second, and a manual PAUSE added to a pause mode makes a new one."""
    manual = modes.manual_pause(root)
    return manual['pause_id'] if manual else None


def gate(root: Path, args: dict | None = None, idle: float | None = None, idle_known: bool = False) -> dict:
    """Whether a monster conversion step may run now: {open, reason, code, mode, pause, pause_id, pauses, paused, pause_reason, hold,
    drain, idle, away, needs_confirm} (+ `mode_unreadable` when supervisor_mode.json exists but cannot be read: closed with code
    `held`, never overridden by Run now or a consent; `pauses` = every independent manual pause in force as {pause_id, kind,
    reason}: what a Run now confirmation must show). `idle` = the user's idle seconds (None + idle_known=False: read presence.json / GetLastInputInfo).
    `code`: held (CONVERT_HOLD, or CONVERT_DRAIN with `draining: true`; neither is ever overridden) | paused | user_present | None.
    `paused` / `pause_id` / `pause_reason` describe ALL independent manual pauses in force (modes.manual_pause: the pause mode, a
    supervisor pause that is neither the idle rule nor its reflection of the current mode, and every PAUSE file that is not PROVABLY
    the supervisor's mirror of its own pause) whatever else closes the gate; `args.override_pause` (the `pause_id` the user confirmed)
    overrides them only while EVERY pause in force is in it (modes.consent_covers: finding 5, final-verify row 5).

    Post-cut-over review, blocker 1: a PAUSE file is never taken for the supervisor's reflection of the confirmed mode by what it SAYS.
    Only the supervisor's own record (supervisor_pause.json, DESIGN_PHASE4.md I.11) can be that reflection, and a PAUSE is its mirror
    only when the record's `mirror` is the sha256 of the PAUSE bytes. A maintenance PAUSE that a supervisor tick rewrites as `manual
    <mode>` (an older supervisor did) is a NEW unconfirmed pause: the consent given for the mode does not cover it and the gate stays
    closed. The idle rule too is read from the supervisor's record: a PAUSE that only says `user active` is an independent pause.

    The mode file, the PAUSE file and the supervisor's record are one modes.pause_state; the reported reason and the id the consent is
    compared with come from that one set of snapshots (last-check row 5). The `gate` sub-command (the paid child's click-time check)
    evaluates this same function; it takes no lock (it runs under the gate transition lock of its caller)."""
    args = args or {}
    mode_rec, snap, own = modes.pause_state(root)          # ONE set of snapshots: ids, reasons, scope and proof of the same bytes
    mode = mode_rec['mode']
    pause = modes.public_pause(snap)
    hold = read_hold(root)
    drain = read_drain(root)
    manual = modes.manual_pause(root, mode_rec, snap, own)  # ALL independent manual pauses in force, as one identity (row 5)
    key = manual['pause_id'] if manual else None
    if idle is None and not idle_known:
        idle, _source = modes.idle_seconds()
    away = idle is not None and idle >= modes.AWAY_AFTER
    run_now = bool(args.get('run_now'))
    override = modes.consent_covers(args.get('override_pause'), manual)   # every pause in force is one the user confirmed
    out = {'open': True, 'reason': None, 'code': None, 'mode': mode, 'pause': pause, 'supervisor_pause': modes.public_pause(own),
           'pause_id': key, 'pauses': [dict(p) for p in manual['pauses']] if manual else [], 'paused': manual is not None,
           'pause_reason': manual['reason'] if manual else None, 'hold': hold, 'drain': drain, 'idle': idle, 'away': away,
           'needs_confirm': False}
    if hold is not None:                                   # never overridden: the supervisor is (re)starting or has not ticked yet
        return out | {'open': False, 'code': 'held', 'reason': f'conversions are held (CONVERT_HOLD: {hold.get("reason") or "no reason"}); '
                      'they wait until the running supervisor completes a tick'}
    if drain is not None:                                  # a drain closes the gate by itself, never overridden (= paid_gate.file_reason)
        return out | {'open': False, 'code': 'held', 'draining': True,
                      'reason': f'conversions are being drained (CONVERT_DRAIN: {drain.get("reason") or "no reason"}); they wait until '
                                'the drain is over'}
    unreadable = modes.mode_unreadable(mode_rec)           # the mode file exists but does not say which mode is in force
    if unreadable:                                         # never overridden: no consent can name a pause nobody can read
        return out | {'open': False, 'code': 'held', 'mode_unreadable': True,
                      'reason': f'the pause mode cannot be read ({unreadable}); conversions wait until supervisor_mode.json is '
                                'readable again (nobody can tell whether they are paused)'}
    idle_pause = modes.idle_paused(mode_rec, own)          # the supervisor's own record says so (never a PAUSE file's wording)
    if manual is not None and not override:                # the consent must name exactly the set of manual pauses in force now
        return out | {'open': False, 'code': 'paused', 'reason': f'conversions are paused ({manual["reason"]})', 'needs_confirm': True}
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
    At most LIMIT attempts of the lane run at once (a variant whose earlier attempt left a live process, or a process tree that is
    not fully known, counts as active: F14, finding 9); every queued
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
        held = tree_hold(root, gen[0], gen[1]) if gen else None
        if held:
            out['waiting'][task['id']] = held
            continue
        if gen and pending_disposition(root, gen[0], gen[1], gen[2]) is not None:
            out['waiting'][task['id']] = 'the outcome of the last attempt is not recorded yet'
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


def run_now(root: Path, task_id: object, confirm: bool = False, key: object = None, generation: object = None,
            pause_id: object = None, conversion_seq: object = None) -> dict:
    """HOOK (POST /api/v2/queue {op: "run_now", id, key, generation, confirm?, pause_id?}): mark a queued (or failed / cancelled: queued
    again through retry_task) monster conversion to run now. The task is the one the client DISPLAYED (`key` + `generation`; `id` is a
    hint; 409 `changed` when it expired, finding 4) and every state change compares that identity in its own transaction. Run now
    overrides the idle gate. While conversions are paused by hand it needs `confirm: true` AND the `pause_id` the user was shown (409
    `confirm` with the current `pause_id` otherwise, also when the echoed id is not the pause in force any more); the override then holds
    for THAT pause only (finding 5). The 409 `confirm` is the server's CHALLENGE: it carries `pauses` (the exact list of pauses in
    force, ids + reasons) for the confirmation dialog and `pause_id`, the challenge id the client echoes; the request is accepted
    only while the pauses in force still equal that challenge (each id is the pause's source + file revision, so a pause that was
    added, or rewritten with another reason, is a new challenge). While supervisor_mode.json cannot be read Run now is REFUSED (409
    `unreadable`): nothing is marked until the mode is readable. The conversion hold (CONVERT_HOLD) is never overridden: the task is marked and starts when the hold
    lifts (the answer carries a `warning`). Run now of a FAILED / CANCELLED row is a Retry first: it needs the `conversion_seq` the
    client displayed (retry_task: 409 `changed` when a newer outcome was recorded since; post-cut-over blocker 4)."""
    task = resolve_task(root, task_id, key, generation)
    ident, task_id = task_ident(task), task['id']
    q = queue(root)
    g = gate(root, {'run_now': True})
    if g.get('mode_unreadable'):                               # an unconfirmed (or any) Run now is never accepted on an unknown mode
        raise guard.Unreadable(f'Run now is refused: {g["reason"]}')
    override = None
    if g['paused']:
        if not confirm:
            raise ConfirmPaused(f'conversions are paused ({g["pause_reason"]}): run this conversion anyway? (it pays {CREDITS_PER_MODEL} '
                                'Hyper3D credits)', pause_id=g['pause_id'], pauses=g['pauses'])
        if not isinstance(pause_id, str) or pause_id != g['pause_id']:
            raise ConfirmPaused(f'the pause changed since you were asked: conversions are paused ({g["pause_reason"]}) now. Run this '
                                f'conversion anyway? (it pays {CREDITS_PER_MODEL} Hyper3D credits)', pause_id=g['pause_id'],
                                pauses=g['pauses'])
        override = g['pause_id']
    try:
        if task['state'] in ('failed', 'cancelled'):
            check = task_check(root, task)
            if not check['wanted'] or check['damaged']:
                raise ValueError(f'task {task_id} cannot run: {check["why"]}')
            if not retry_task(root, task, conversion_seq):
                raise ValueError(f'task {task_id} is no longer failed or cancelled; reload and look again')
        elif task['state'] != 'queued':
            raise ValueError(f'task {task_id} is {task["state"]}; Run now is for a queued task')
        if not q.mark_run_now(task_id, override, expect=ident):
            raise ValueError(f'task {task_id} is no longer queued')
    except taskqueue.Expired as exc:
        raise guard.Changed(f'{exc}; reload and look again', changes=['task']) from None
    mv.invalidate()
    out = {'ok': True, 'id': task_id, 'key': task['key'], 'generation': ident['generation'], 'state': 'queued', 'run_now': True,
           'override_pause': bool(override), 'pause_id': override}
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


PROVIDER_IDS = ('provider_action_id',)
ACTION_COSTS = ('observed_cost',)


def advance_evidence(record: dict, kind: str) -> str | None:
    """The evidence that a paid action was CHARGED (finding 8), or None: `actions[<kind>].confirmed_at`, which the backend
    (rodin_backend) writes only when it saw the paid stage advance (with `confirmed_by`, `stage_before` / `stage_after`, the balances).
    Nothing else counts: not a cleared `pending_action`, not an observed stage, not the record's status. An unconfirmed click keeps
    `pending_action` and gets a `paid_review` note in the record."""
    actions = record.get('actions') if isinstance(record.get('actions'), dict) else {}
    entry = actions.get(kind) if isinstance(actions.get(kind), dict) else {}
    if not entry.get('confirmed_at'):
        return None
    how = entry.get('confirmed_by')
    return f'confirmed {entry["confirmed_at"]}' + (f' by {how}' if how else '')


def paid_actions(record: dict | None) -> list[dict]:
    """The credit-capable actions a submission record names (F5, finding 8): [{action, status, credits, id, provider_id, evidence,
    model_id}]. `status` confirmed ONLY when the backend recorded `actions[<kind>].confirmed_at` (advance_evidence); otherwise uncertain
    (the click may or may not have happened; a cleared intent alone proves nothing). `credits` = the cost the backend observed for that
    click (`actions[<kind>].observed_cost`, else `observed_cost_<kind>.credits`), None when it was not observed (never assumed). An action
    observed at cost 0 is free and not listed. `id` = `hyper3d:<model id>:<action>` (the ledger identity); `provider_id` = the provider's
    own action id when the backend recorded one."""
    if not isinstance(record, dict):
        return []
    actions = record.get('actions') if isinstance(record.get('actions'), dict) else {}
    model = record.get('model_id') if isinstance(record.get('model_id'), str) and record.get('model_id') else None
    out = []
    for kind in PAID_KINDS:
        if kind not in actions:
            continue
        entry = actions[kind] if isinstance(actions[kind], dict) else {}
        costs = [entry.get(f).get('credits') if isinstance(entry.get(f), dict) else entry.get(f) for f in ACTION_COSTS]
        credits = next((n for n in map(_number, costs) if n is not None), None)
        if credits is None:
            observed = record.get(f'observed_cost_{kind}')
            credits = _number(observed.get('credits')) if isinstance(observed, dict) else None
        if credits == 0:
            continue
        evidence = advance_evidence(record, kind)
        out.append({'action': kind, 'status': 'confirmed' if evidence else 'uncertain', 'credits': credits,
                    'id': f'hyper3d:{model}:{kind}' if model else None,
                    'provider_id': next((str(entry[f]) for f in PROVIDER_IDS if isinstance(entry.get(f), (str, int)) and entry[f]), None),
                    'evidence': evidence, 'model_id': entry.get('model_id') if isinstance(entry.get('model_id'), str) else model})
    committed = _number(record.get('credits_committed'))
    if not out and committed and committed > 0:                  # credits committed without a named paid action (an older record)
        out.append({'action': 'committed', 'status': 'uncertain', 'credits': committed, 'id': f'hyper3d:{model}:committed' if model else None,
                    'provider_id': None, 'evidence': None, 'model_id': model})
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


def _stored_actions(info: object) -> list[dict]:
    """The paid actions an unrepaired write error carries (its evidence: ledger id, provider id, observed amount, status)."""
    actions = info.get('actions') if isinstance(info, dict) else None
    return [a for a in actions if isinstance(a, dict) and isinstance(a.get('action'), str)] if isinstance(actions, list) else []


def _merge_actions(rec_text: str, *lists: list[dict]) -> list[dict]:
    """One action per ledger id; a later list wins, except that a confirmed action is never downgraded."""
    out: dict[str, dict] = {}
    for actions in lists:
        for action in actions:
            ident = action.get('id') or f'record:{rec_text}:{action["action"]}'
            prev = out.get(ident)
            if prev is not None and prev.get('status') == 'confirmed' and action.get('status') != 'confirmed':
                continue
            out[ident] = dict(action) | {'id': ident}
    return list(out.values())


def _ledger_lock(root: Path) -> guard.OsLock:
    return guard.os_lock(Path(root) / guard.APP / 'paid_log.lock')


def _errors_lock(root: Path) -> guard.OsLock:
    """The lock of paid_log_errors.json (last-check review, row 8). EVERY write of that file is a read-merge-write under it; it is
    always taken INSIDE the ledger lock when that is held (order: ledger, then errors), and alone only when the ledger lock itself
    could not be taken."""
    return guard.os_lock(Path(root) / guard.APP / 'paid_log_errors.lock')


def _update_error(root: Path, rec_text: str, change) -> None:
    """Read-merge-write of ONE record's entry of paid_log_errors.json under the errors lock: `change(entry now | None)` returns the
    entry to store, or None to drop it. The other records' entries are the ones just read, so two concurrent failures (or a failure
    and a repair) never overwrite each other (row 8)."""
    with _errors_lock(root):
        errors = _read_errors(root)
        current = errors.get(rec_text)
        new = change(current)
        if new is None:
            if rec_text not in errors:
                return
            errors.pop(rec_text)
        else:
            errors[rec_text] = new
        guard.write_atomic(paid_errors_path(root), mv.dump(errors))


def reconcile_paid(root: Path, monster: str, variant: str, files: list[str], record_path: Path, record: dict | None,
                   stored: list[dict] | None = None) -> str | None:
    """Bring paid_log.jsonl in line with a record (F5), on every run: one entry per provider action id, appended when it is new or its
    status / amount changed (a confirmed entry is final). An older lane's entry (no `id`) for the same record and model covers its
    geometry click. A write error is kept in paid_log_errors.json (shown in the Queue and the item) until a later reconcile succeeds; the
    error text is returned (None = in line).
    Finding 8: the error entry STORES the actions it could not log ({id, provider_id, action, status, credits, model_id, evidence}), so a
    repair does not depend on the record still being at `record_path` (Reconvert moves it): `stored` = those actions; they are logged
    even when the record is gone or belongs to another model by now. Final-verify row 8: the actions stored with the path's error
    entry are merged in by EVERY call (read under the ledger lock), and the entry is cleared only of the ids the ledger carries.
    Last-check row 8: the ERROR FILE is updated under the same ledger lock too (and always as a read-merge-write of this record's
    entry, `_update_error`): a failed ledger write records its repair before the ledger lock is released, so two concurrent failures
    both keep their repair. Only when the ledger lock itself cannot be taken is the repair written under the errors lock alone."""
    rec_text = Path(record_path).as_posix()
    wanted = _merge_actions(rec_text, stored or [], paid_actions(record))

    def failed(exc: BaseException, actions: list[dict]) -> str:
        text = f'the paid log could not be written ({type(exc).__name__}: {exc})'

        def keep(current):
            kept = _merge_actions(rec_text, _stored_actions(current), actions)     # the evidence travels with the repair
            return {'time': guard.now(), 'error': text, 'monster': monster, 'variant': variant, 'views': list(files),
                    'model_id': (record or {}).get('model_id'), 'actions': kept}
        try:
            _update_error(root, rec_text, keep)
        except (OSError, guard.Conflict):
            pass                                               # the verdict and the task row still carry the text
        return text

    try:
        ledger = _ledger_lock(root).__enter__()
    except (OSError, guard.Conflict) as exc:
        return failed(exc, wanted)                             # (no ledger lock: the errors lock alone guards the repair)
    try:
        try:
            # final-verify row 8: EVERY reconciliation (not only repair_accounting) merges the actions stored with an unrepaired write
            # error of this path, read under the ledger lock: an ordinary run after Reconvert moved the record away (record None, or a
            # new model's record at the path) logs them instead of dropping them with the error entry.
            unrepaired = _stored_actions(_read_errors(root).get(rec_text))
            wanted = _merge_actions(rec_text, unrepaired, stored or [], paid_actions(record))
            entries = paid_entries(root)
            last = {e['id']: e for e in entries if isinstance(e.get('id'), str)}
            new = []
            for action in wanted:
                ident = action['id']
                prev = last.get(ident)
                legacy = any(e.get('record') == rec_text and 'id' not in e and e.get('model_id') == action.get('model_id') for e in entries)
                if prev is None and legacy and action['action'] in ('geometry', 'committed'):
                    continue
                if prev is not None and (prev.get('status') == 'confirmed' or
                                         (prev.get('status') == action['status'] and prev.get('credits') == action['credits'])):
                    continue
                new.append({'time': guard.now(), 'kind': 'hyper3d', 'id': ident, 'action': action['action'], 'status': action['status'],
                            'credits': action['credits'], 'monster': monster, 'variant': variant, 'views': list(files),
                            'record': rec_text, 'model_id': action.get('model_id'), 'provider_id': action.get('provider_id'),
                            'evidence': action.get('evidence')})
            if new:
                path = paid_log_path(root)
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, 'a', encoding='utf-8') as handle:
                    for entry in new:
                        handle.write(json.dumps(entry) + '\n')
            # clear only what is ACCOUNTED: a stored action leaves the error once the ledger carries its id (just written, or there
            # already); the entry goes when none is left AND it had evidence at all (stored actions, or the record of the same
            # model at the path). An entry without either stays listed (repair_accounting reports it; never cleared silently).
            in_ledger = {e['id'] for e in paid_entries(root) if isinstance(e.get('id'), str)}
            legacy_ok = {a['id'] for a in wanted if a['id'] not in in_ledger and a['action'] in ('geometry', 'committed') and any(
                e.get('record') == rec_text and 'id' not in e and e.get('model_id') == a.get('model_id') for e in entries)}

            def clear(current):
                if current is None:
                    return None
                info = current if isinstance(current, dict) else {}
                had = _stored_actions(current)                  # (the entry as it is NOW, under the errors lock)
                left = [a for a in _merge_actions(rec_text, had) if a['id'] not in in_ledger and a['id'] not in legacy_ok]
                same_model = record is not None and (info.get('model_id') is None or record.get('model_id') == info.get('model_id'))
                if left:
                    return info | {'actions': left}
                return None if had or same_model else current
            _update_error(root, rec_text, clear)
            return None
        except (OSError, guard.Conflict) as exc:
            return failed(exc, wanted)                         # STILL under the ledger lock (last-check row 8)
    finally:
        ledger.__exit__(None, None, None)


def repair_accounting(root: Path) -> list[str]:
    """HOOK (tick): retry the paid-log reconciliation of every record with an unrepaired write error, from the evidence STORED with the
    error (finding 8) plus the record when it is still the same model's. An error whose record is gone and that carries no stored action
    cannot be resolved: it stays listed (never cleared silently). Returns the errors still open."""
    out = []
    for rec_text, info in list(_read_errors(root).items()):
        if not isinstance(info, dict) or not isinstance(info.get('monster'), str):
            out.append(f'paid log: {rec_text}: {info}')
            continue
        stored = _stored_actions(info)
        record = mv.read_json(Path(rec_text), None)
        record = record if isinstance(record, dict) else None
        if record is not None and stored and info.get('model_id') is not None and record.get('model_id') != info.get('model_id'):
            record = None                                      # another conversion's record sits at that path now (Reconvert)
        if record is None and not stored:
            out.append(f'paid log: {info["monster"]}/{info.get("variant")}: the record {rec_text} is gone and the error carries no '
                       f'stored action; check {PAID_LOG} by hand ({info.get("error")})')
            continue
        error = reconcile_paid(root, info['monster'], str(info.get('variant')), list(info.get('views') or []), Path(rec_text), record,
                               stored=stored)
        if error:
            out.append(f'paid log: {info["monster"]}/{info.get("variant")}: {error}')
    return out


def paid_summary(root: Path) -> dict:
    """{count, credits (confirmed, observed amounts), unknown_amounts (confirmed without an observed amount), uncertain {count, credits},
    recent, errors}: the last entry of every provider action id counts; entries of an older lane (no id) count as confirmed. An action is
    `confirmed` only with evidence that its paid stage advanced (paid_actions, finding 8)."""
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
    """The recorded process (pid + create time) is still running. Without a recorded create time any live process with that pid counts
    (that keeps a slot held; it is never enough to SIGNAL the process: see `_proven`)."""
    if psutil is None or not isinstance(pid, int) or pid <= 0:
        return False
    actual = _create_time(pid)
    if actual is None:
        return False
    return not isinstance(create, (int, float)) or abs(actual - float(create)) <= CREATE_TOLERANCE


def _proven(pid: object, create: object) -> bool:
    """The live process `pid` IS the recorded one: a recorded create time that matches. Evidence for HOLDING a slot; a signal needs
    the verified object itself (`_open_proven`), never a second look-up of the pid."""
    return isinstance(create, (int, float)) and not isinstance(create, bool) and _alive(pid, create)


def _open_proven(pid: object, create: object):
    """The psutil.Process OBJECT of the recorded process (pid + recorded create time), or None (final-live review, blocker 7). The
    pid is opened ONCE and the identity is verified ON THAT OBJECT (its own create time is the recorded one); the caller keeps and
    signals exactly this object. Checking the pid and then opening it again would leave a gap in which the process exits and its
    pid is given to an unrelated one, which the second look-up would return."""
    if psutil is None or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if isinstance(create, bool) or not isinstance(create, (int, float)):
        return None
    try:
        proc = psutil.Process(pid)
        if abs(proc.create_time() - float(create)) > CREATE_TOLERANCE:
            return None
    except psutil.Error:
        return None
    return proc


def _still_same(proc) -> bool:
    """The RETAINED process object still names the process it was verified as (psutil compares the create time it holds with the
    pid's now): False once that process is gone, also when its pid belongs to another process by now."""
    try:
        return bool(proc.is_running())
    except psutil.Error:
        return False


# ---- Windows Job Object (finding 9) ------------------------------------------------------------------------------------------------
# The driver process of an attempt (`monster_lane.py run`) creates a NAMED job object (no limits: JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE is
# off) and assigns ITSELF before it starts any child, so every descendant (collector, browser, reducer, Blender and whatever they start)
# is in the job from its first instruction. The job's name is recorded in the children file; the orchestrator opens it by name while the
# driver lives and KEEPS that handle, so it owns the whole tree through the driver's exit: the job says exactly which processes of the
# attempt still run (grandchildren included) and can terminate them all. Without a job (not Windows, the assignment failed, or the
# orchestrator restarted after the driver died, which makes the name unreachable) the lane falls back to the processes recorded as
# they appeared, and keeps the slot whenever that evidence is incomplete.

JOB_PREFIX = 'Local\\imageto3d-mlane-'
JOB_OBJECT_ASSIGN_PROCESS, JOB_OBJECT_QUERY, JOB_OBJECT_TERMINATE = 0x0001, 0x0004, 0x0008
JOB_PROCESS_ID_LIST = 3                # JOBOBJECTINFOCLASS.JobObjectBasicProcessIdList
ERROR_MORE_DATA = 234
_K32 = None
_JOBS: dict[str, tuple[str, int]] = {}  # children file -> (job name, handle held by THIS process)
_JOBS_LOCK = threading.Lock()


def _k32():
    """kernel32 with the job functions typed (None off Windows)."""
    global _K32
    if os.name != 'nt':
        return None
    if _K32 is None:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateJobObjectW.restype, k.CreateJobObjectW.argtypes = wintypes.HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]
        k.OpenJobObjectW.restype, k.OpenJobObjectW.argtypes = wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k.AssignProcessToJobObject.restype, k.AssignProcessToJobObject.argtypes = wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]
        k.GetCurrentProcess.restype, k.GetCurrentProcess.argtypes = wintypes.HANDLE, []
        k.QueryInformationJobObject.restype = wintypes.BOOL
        k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        k.TerminateJobObject.restype, k.TerminateJobObject.argtypes = wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]
        k.CloseHandle.restype, k.CloseHandle.argtypes = wintypes.BOOL, [wintypes.HANDLE]
        _K32 = k
    return _K32


def job_create(name: str) -> int | None:
    """A new named job object with default limits (nothing is killed when its handles close). None when it cannot be made."""
    k = _k32()
    if k is None:
        return None
    return k.CreateJobObjectW(None, name) or None


def job_assign_self(handle: int) -> bool:
    """Put THIS process into the job: every child it starts from now on is in the job too."""
    k = _k32()
    return bool(k and k.AssignProcessToJobObject(handle, k.GetCurrentProcess()))


def job_open(name: str) -> int | None:
    """A query + terminate handle of a named job that still exists (None otherwise)."""
    k = _k32()
    if k is None or not name:
        return None
    return k.OpenJobObjectW(JOB_OBJECT_QUERY | JOB_OBJECT_TERMINATE, False, name) or None


def job_pids(handle: int) -> list[int] | None:
    """The process ids in the job now (None when the job cannot be queried)."""
    k = _k32()
    if k is None or not handle:
        return None
    import ctypes
    from ctypes import wintypes
    count = 256
    while True:
        class ProcessIdList(ctypes.Structure):
            _fields_ = [('assigned', wintypes.DWORD), ('listed', wintypes.DWORD), ('pids', ctypes.c_size_t * count)]
        info = ProcessIdList()
        if k.QueryInformationJobObject(handle, JOB_PROCESS_ID_LIST, ctypes.byref(info), ctypes.sizeof(info), None):
            return [int(info.pids[i]) for i in range(min(info.listed, count))]
        if ctypes.get_last_error() != ERROR_MORE_DATA or count >= 1 << 16:
            return None
        count *= 4


def job_terminate(handle: int) -> bool:
    k = _k32()
    return bool(k and handle and k.TerminateJobObject(handle, 1))


def job_close(handle: int | None) -> None:
    k = _k32()
    if k is not None and handle:
        k.CloseHandle(handle)


def _job_handle(path: Path, name: object) -> int | None:
    """The handle this process holds for the job a children file names (opened by name on first sight and then kept: ownership through
    the driver's exit). None when the file names no job or it can no longer be opened."""
    key = str(path)
    with _JOBS_LOCK:
        held = _JOBS.get(key)
        if held is not None and held[0] != name:
            job_close(held[1])
            _JOBS.pop(key, None)
            held = None
        if held is not None:
            return held[1]
        if not isinstance(name, str) or not name.startswith(JOB_PREFIX):
            return None
        handle = job_open(name)
        if handle:
            _JOBS[key] = (name, handle)
        return handle


def _forget_job(path: Path) -> None:
    with _JOBS_LOCK:
        held = _JOBS.pop(str(path), None)
    if held is not None:
        job_close(held[1])


def _recorded(value: dict) -> list[dict]:
    """Every process a children file records: the driver, its direct children and every descendant seen ({pid, create, role})."""
    out, seen = [], set()
    groups = ([dict(value['driver']) | {'role': 'driver'}] if isinstance(value.get('driver'), dict) else [],
              [dict(c) | {'role': 'child'} for c in value.get('children') or [] if isinstance(c, dict)],
              [dict(c) | {'role': 'descendant'} for c in value.get('descendants') or [] if isinstance(c, dict)])
    for group in groups:
        for proc in group:
            ident = (proc.get('pid'), proc.get('create'))
            if isinstance(proc.get('pid'), int) and ident not in seen:
                seen.add(ident)
                out.append(proc)
    return out


def _tree_state(path: Path) -> dict:
    """What is known about the process tree of a variant's last attempt (finding 9): {file, job, live: [{pid, create, role}],
    incomplete: reason | None, set_name}.
      - job: this process holds the attempt's Job Object: `live` = exactly the processes still in the job (complete evidence);
      - otherwise `live` = the recorded processes (driver, children, descendants as they appeared) that still run, and `incomplete` says
        why that record cannot be trusted to be the whole tree (a spawn that was never recorded, a tracking error, an unreadable file)
        once the driver that could correct it is gone."""
    try:
        value = mv.read_strict(path, path.name)
    except guard.Unreadable as exc:
        return {'file': True, 'job': False, 'live': [], 'incomplete': str(exc), 'set_name': path.name}
    if value is None:
        _forget_job(path)
        return {'file': False, 'job': False, 'live': [], 'incomplete': None, 'set_name': None}
    name = str(value.get('set_name') or path.name)
    handle = _job_handle(path, value.get('job'))
    if handle is not None:
        pids = job_pids(handle)
        if pids is not None:
            # every process the job lists is live (a job lists no exited process), also one whose create time cannot be read
            live = [{'pid': pid, 'create': _create_time(pid), 'role': 'job'} for pid in pids]
            return {'file': True, 'job': True, 'live': live, 'incomplete': None, 'set_name': name, 'handle': handle}
    recorded = _recorded(value)
    live = [p for p in recorded if _alive(p.get('pid'), p.get('create'))]
    driver = value.get('driver') if isinstance(value.get('driver'), dict) else None
    driver_alive = driver is not None and _proven(driver.get('pid'), driver.get('create'))
    incomplete = None if driver_alive or not value.get('incomplete') else str(value['incomplete'])
    return {'file': True, 'job': False, 'live': live, 'incomplete': incomplete, 'set_name': name}


def tree_state(root: Path, monster: str, variant: str) -> dict:
    return _tree_state(children_path(root, monster, variant))


def _hold_text(path: Path, state: dict) -> str | None:
    if state['live']:
        return f'a process of the last conversion attempt still runs (pid {", ".join(str(p["pid"]) for p in state["live"][:8])})'
    if state['incomplete']:
        return (f'the process tree of the last conversion attempt is not fully known ({state["incomplete"]}); the slot stays held: check '
                f'that nothing of it runs, then remove {path.as_posix()}')
    return None


def tree_hold(root: Path, monster: str, variant: str) -> str | None:
    """Why the variant's last attempt still holds its slot (a live process of its tree, or incomplete evidence about the tree), else
    None. A record whose whole tree is verifiably gone is removed here: `_tree_state` asks the attempt's Job Object first (every live
    process in it, whoever its parent was) and only then the recorded processes, so the record stays, and the survivors are named,
    while anything of the attempt runs (final-verify row 9)."""
    path = children_path(root, monster, variant)
    state = _tree_state(path)
    text = _hold_text(path, state)
    if text is None and state['file']:
        path.unlink(missing_ok=True)
        _forget_job(path)
    return text


def recorded_children(root: Path, monster: str, variant: str) -> dict:
    """{driver: {pid, create} | None, children: [{pid, create, argv}], descendants: [...], job, incomplete} of the variant's last driver
    (empty when none)."""
    value = mv.read_json(children_path(root, monster, variant), None)
    if not isinstance(value, dict):
        return {'driver': None, 'children': [], 'descendants': [], 'job': None, 'incomplete': None}
    return {'driver': value.get('driver') if isinstance(value.get('driver'), dict) else None,
            'children': [c for c in value.get('children') or [] if isinstance(c, dict)],
            'descendants': [c for c in value.get('descendants') or [] if isinstance(c, dict)],
            'job': value.get('job'), 'incomplete': value.get('incomplete')}


def live_children(root: Path, monster: str, variant: str) -> list[dict]:
    """The processes of the variant's last attempt that still run (the Job Object's, else the recorded ones verified by pid + create
    time; the driver itself included)."""
    return tree_state(root, monster, variant)['live']


def clear_children(root: Path, monster: str, variant: str) -> None:
    """After a verified drain: forget the attempt's process record (kept while anything of it still runs or the evidence is incomplete)."""
    tree_hold(root, monster, variant)


def stray_trees(root: Path) -> list[str]:
    """set_names (`monster:<M>/<v>`) of variants whose last attempt still holds its slot (a live process or incomplete evidence about
    its tree): counted as active by the plan (F14, finding 9)."""
    out = []
    folder = Path(root) / guard.APP / CHILDREN_DIR
    try:
        paths = list(folder.glob('*.children.json'))
    except OSError:
        return out
    for path in paths:
        state = _tree_state(path)
        if _hold_text(path, state):
            out.append(str(state['set_name'] or path.name))
    return out


def tree_plan(root: Path, monster: str, variant: str, roots: list[tuple] | tuple = ()) -> dict:
    """Kill & retry, step 1 (finding 9): WHAT may be signalled, decided before any signal. {job: handle | None, procs: [psutil.Process],
    blocking: [text], incomplete: text | None}.
      - with the attempt's Job Object: the job is the tree (nothing is enumerated by parent pid);
      - otherwise only processes whose identity is PROVEN (pid + a recorded create time that matches): the `roots` the caller verified
        and the recorded driver / children / descendants, each with its descendants at this moment. A recorded process that is alive but
        has no recorded create time is `blocking`: nothing at all is signalled then (it may be an unrelated process on a reused pid).
    `procs` are the VERIFIED psutil.Process objects themselves (`_open_proven`: opened once, identity checked on that object; their
    descendants are the objects psutil made for them); `drain_tree` signals exactly these retained objects and never opens a pid
    again (final-live review, blocker 7)."""
    path = children_path(root, monster, variant)
    state = _tree_state(path)
    plan = {'job': state.get('handle') if state['job'] else None, 'procs': [], 'blocking': [], 'incomplete': state['incomplete']}
    if plan['job'] is not None or psutil is None:
        return plan
    procs: dict[int, object] = {}

    def collect(pid, created) -> None:
        if pid == os.getpid():                                  # (never this process: a driver run inside it, as in tests)
            return
        top = _open_proven(pid, created)                        # ONE open; the identity is verified on the object that is kept
        if top is None:
            return
        try:
            for proc in [top] + top.children(recursive=True):
                procs.setdefault(proc.pid, proc)
        except psutil.Error:
            procs.setdefault(top.pid, top)
    for pid, created in roots:
        collect(pid, created)
    value = mv.read_json(path, None)
    for proc in _recorded(value) if isinstance(value, dict) else []:
        if not _alive(proc.get('pid'), proc.get('create')):
            continue
        if not _proven(proc.get('pid'), proc.get('create')):
            plan['blocking'].append(f'the recorded {proc.get("role")} {proc.get("pid")} has no recorded create time: it cannot be told '
                                    'from an unrelated process')
            continue
        collect(proc['pid'], proc['create'])
    own = {pid for pid, _created in roots}
    plan['procs'] = [proc for pid, proc in procs.items() if pid not in own]
    return plan


def drain_tree(root: Path, monster: str, variant: str, plan: dict, wait: float = 10.0) -> dict:
    """Kill & retry, step 2: stop what `tree_plan` named and confirm it is gone. {killed: n, problems: [text]}. The record is removed
    only when the whole tree is verifiably gone; incomplete evidence stays a problem (the caller keeps the slot)."""
    problems: list[str] = []
    killed = 0
    if plan.get('job') is not None:
        before = job_pids(plan['job']) or []
        if before and not job_terminate(plan['job']):
            problems.append('the job object of the attempt could not be terminated')
        deadline = time.monotonic() + wait
        left = job_pids(plan['job'])
        while left and time.monotonic() < deadline:
            time.sleep(0.1)
            left = job_pids(plan['job'])
        if left is None:
            problems.append('the job object of the attempt cannot be queried')
        elif left:
            problems.append(f'process(es) {", ".join(map(str, left[:8]))} of the attempt are still running after the kill')
        killed = len(before) - len(left or [])
    elif psutil is not None:
        tree = list(plan.get('procs') or [])
        for proc in reversed(tree):                             # deepest first (children lists are breadth-first)
            try:
                # the RETAINED verified object is what gets the signal (blocker 7): it is checked once more on itself, so a
                # process that exited meanwhile is skipped also when its pid belongs to another process by now
                if not _still_same(proc):
                    continue
                proc.kill()
            except psutil.NoSuchProcess:
                pass
            except psutil.Error as exc:
                problems.append(f'child {proc.pid} could not be killed ({exc})')
        _gone, alive = psutil.wait_procs(tree, timeout=wait) if tree else ([], [])
        for proc in alive:
            problems.append(f'child {proc.pid} is still running after the kill')
        killed = len(tree) - len(alive)
    if not problems:
        held = tree_hold(root, monster, variant)               # removes the record when everything is gone
        if held:
            problems.append(held)
    return {'killed': killed, 'problems': problems}


class _Tracker:
    """The children file of one driver run (finding 9): the driver, the Job Object it put itself into (`own_process`), every child from
    BEFORE its start (`reserve`) until it exited, and every descendant as it appears (a watcher thread, psutil). `incomplete` names why
    the record may not be the whole tree; the file is removed only when the driver itself sees no descendant left."""
    WATCH_S = 0.5

    def __init__(self, root: Path, monster: str, variant: str, own_process: bool = False) -> None:
        self.path = children_path(root, monster, variant)
        self.set_name = set_name(monster, variant)
        self.driver = {'pid': os.getpid(), 'create': _create_time(os.getpid())}
        self.children: list[dict] = []
        self.descendants: dict[int, dict] = {}
        self.incomplete: str | None = None
        self.job: str | None = None
        self.job_error: str | None = None
        self._handle = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        if own_process:
            self._own_job(variant_key(monster, variant))
        self._write()                                           # an attempt whose tree cannot be recorded does not start (OSError)
        self._thread = None
        if psutil is not None:
            self._thread = threading.Thread(target=self._watch, name='monster-tree', daemon=True)
            self._thread.start()

    def _own_job(self, key: str) -> None:
        name = f'{JOB_PREFIX}{key}-{uuid.uuid4().hex[:12]}'
        handle = job_create(name)
        if handle and job_assign_self(handle):
            self.job, self._handle = name, handle
            return
        job_close(handle)
        self.job_error = 'no job object (not Windows, or this process could not be assigned); the recorded processes are the evidence'

    def _write(self) -> None:
        guard.write_atomic(self.path, mv.dump({'schema': 2, 'set_name': self.set_name, 'driver': self.driver, 'job': self.job,
                                               'job_error': self.job_error, 'children': self.children,
                                               'descendants': list(self.descendants.values()), 'incomplete': self.incomplete}))

    def _job_members(self) -> list[int] | None:
        """The OTHER processes in this driver's Job Object now ([] without a job; None when the job cannot be queried). A grandchild
        whose parent exited is no longer a psutil descendant of the driver, but it is still in the job (final-verify row 9)."""
        if self._handle is None:
            return []
        pids = job_pids(self._handle)
        return None if pids is None else [pid for pid in pids if pid != os.getpid()]

    def _scan(self) -> bool:
        """Record every descendant of this driver that is not recorded yet: its psutil descendants AND every other process of its Job
        Object (which also holds the ones whose parent is gone). True when something was added."""
        if psutil is None:
            return False
        added = False
        procs = {proc.pid: proc for proc in psutil.Process(os.getpid()).children(recursive=True)}
        for pid in self._job_members() or []:
            if pid not in procs:
                try:
                    procs[pid] = psutil.Process(pid)
                except psutil.Error:
                    continue
        for proc in procs.values():
            try:
                if proc.pid not in self.descendants or abs(self.descendants[proc.pid]['create'] - proc.create_time()) > CREATE_TOLERANCE:
                    self.descendants[proc.pid] = {'pid': proc.pid, 'create': proc.create_time(), 'name': proc.name()}
                    added = True
            except psutil.Error:
                continue
        return added

    def _job_survivors(self) -> str | None:
        """Final-verify row 9, asked right before the record may be removed: the Job Object is the authority on what of this attempt
        still runs. Every other live process in it is put on record (pid + create time); returns why the record is NOT complete (the job
        cannot be queried, or a member cannot be identified), else None."""
        for _try in range(2):
            members = self._job_members()
            if members is None:
                return 'the job object of the attempt could not be queried when the driver ended'
            unknown = []
            for pid in members:
                created = _create_time(pid)
                if created is None:
                    unknown.append(pid)
                elif pid not in self.descendants or abs((self.descendants[pid].get('create') or 0) - created) > CREATE_TOLERANCE:
                    self.descendants[pid] = {'pid': pid, 'create': created, 'name': 'job member'}
            if not unknown:
                return None
            time.sleep(0.2)                                     # (a process that is exiting right now leaves the job in a moment)
        return f'process(es) {", ".join(map(str, unknown[:8]))} of the attempt\'s job object could not be identified'

    def _job_empty(self) -> bool:
        """Last-check review, row 9: the FINAL look, immediately before the ownership record is unlinked. True only when the attempt's
        Job Object has NO member besides this driver (or there is no job: the recorded processes were the evidence). A member that is
        there now (a successor a job member spawned before it exited, after `_job_survivors` looked) is put on record and keeps the
        record; a job that cannot be queried now makes the record `incomplete`. An empty job is conclusive: only a member can add a
        member, and this driver starts nothing any more."""
        try:
            members = self._job_members()
        except Exception as exc:  # noqa: BLE001
            self.incomplete = f'the job object of the attempt could not be checked ({type(exc).__name__}: {exc})'
            return False
        if members is None:
            self.incomplete = 'the job object of the attempt could not be queried when the driver ended'
            return False
        if not members:
            return True
        unknown = []
        for pid in members:
            created = _create_time(pid)
            if created is None:
                unknown.append(pid)
            self.descendants[pid] = {'pid': pid, 'create': created, 'name': 'job member'}
        if unknown:                                             # (it may have exited this instant; the record stays and says so)
            self.incomplete = (f'process(es) {", ".join(map(str, unknown[:8]))} were in the attempt\'s job object when the driver '
                               'ended and could not be identified')
        return False

    def _watch(self) -> None:
        while not self._stop.wait(self.WATCH_S):
            try:
                with self._lock:
                    if self._scan():
                        self._write()
            except Exception as exc:  # noqa: BLE001  (the evidence is incomplete from here on: the slot stays held, visibly)
                with self._lock:
                    self.incomplete = f'descendant tracking failed ({type(exc).__name__}: {exc})'
                    try:
                        self._write()
                    except OSError:
                        pass

    def reserve(self, argv: list[str]) -> None:
        """BEFORE a child is started: the file says a spawn is under way. Raises OSError when that cannot be written (no spawn then)."""
        with self._lock:
            before, self.incomplete = self.incomplete, self.incomplete or f'a child was being started ({Path(str(argv[0])).name} ...)'
            try:
                self._write()
            except OSError:
                self.incomplete = before
                raise

    def unreserve(self) -> None:
        with self._lock:
            if self.incomplete and self.incomplete.startswith('a child was being started'):
                self.incomplete = None
            self._write()

    def add(self, proc: subprocess.Popen, argv: list[str]) -> None:
        with self._lock:
            self.children.append({'pid': proc.pid, 'create': _create_time(proc.pid), 'argv': [str(a) for a in argv[:4]],
                                  'started': time.time()})
            if self.incomplete and self.incomplete.startswith('a child was being started'):
                self.incomplete = None
            try:
                self._scan()
            except Exception:  # noqa: BLE001  (the watcher records the error)
                pass
            self._write()

    def remove(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self.children = [c for c in self.children if c['pid'] != proc.pid]
            try:
                self._scan()
            except Exception:  # noqa: BLE001
                pass
            self.descendants = {pid: d for pid, d in self.descendants.items() if _alive(pid, d.get('create'))}
            self._write()

    def close(self) -> None:
        """End of the driver run: the record is removed only when the driver sees that nothing of its tree is left; otherwise it stays
        (with the survivors) and the slot stays held. The attempt's Job Object is asked BEFORE the record may be unlinked (final-verify
        row 9): a process that is still in the job (a grandchild whose short-lived parent exited between two watcher samples) is
        recorded and keeps the record; a job that cannot be queried leaves the record `incomplete`. Last-check row 9: the job is asked
        ONCE MORE, immediately before the unlink (`_job_empty`): a successor that a member spawned during this cleanup keeps the
        record."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._lock:
            try:
                self._scan()
            except Exception as exc:  # noqa: BLE001
                self.incomplete = self.incomplete or f'descendant tracking failed ({type(exc).__name__}: {exc})'
            try:
                problem = self._job_survivors()
            except Exception as exc:  # noqa: BLE001
                problem = f'the job object of the attempt could not be checked ({type(exc).__name__}: {exc})'
            if problem:
                self.incomplete = self.incomplete or problem
            self.children = [c for c in self.children if _alive(c.get('pid'), c.get('create'))]
            self.descendants = {pid: d for pid, d in self.descendants.items() if _alive(pid, d.get('create'))}
            try:
                if not self.children and not self.descendants and not self.incomplete and self._job_empty():
                    self.path.unlink(missing_ok=True)
                else:
                    self._write()
            except OSError:
                pass
        job_close(self._handle)
        self._handle = None


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
    children file from before its start until it exited, and its descendants as they appear (F14, finding 9)."""
    say(f'[monster_lane] $ {" ".join(argv)}')
    if tracker is not None:
        try:
            tracker.reserve(argv)                               # recorded BEFORE the spawn (finding 9): no untracked child
        except OSError as exc:
            return -1, f'the child process could not be recorded ({exc}); it was not started'
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), stdout=None, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                                encoding='utf-8', errors='replace', creationflags=NO_WINDOW, env=env)
    except OSError as exc:
        if tracker is not None:
            try:
                tracker.unreserve()
            except OSError:
                pass
        return -1, f'could not start {argv[0]}: {exc}'
    if tracker is not None:
        try:
            tracker.add(proc, argv)
        except OSError:
            pass                                               # the file still says a spawn was under way: incomplete evidence
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


def snapshot_inputs(out_dir: Path, vdir: Path, frozen: dict, mroot: Path | None = None) -> tuple[Path | None, str | None]:
    """The immutable upload folder of a generation (F7): `hyper3d/inputs_<generation>/` holds exactly the frozen files, read-only, each
    with its frozen sha256 (copied from views/<v>/ and verified; an existing folder is verified again and rebuilt when it differs).
    (folder, None) or (None, problem). `mroot`: the monster root every destination is checked against right before use (resolved
    containment, no link or junction on the way; finding 14)."""
    files = [str(f) for f in frozen.get('files') or []]
    digests = frozen.get('sha256') if isinstance(frozen.get('sha256'), dict) else {}
    snap = snapshot_dir(out_dir, frozen)
    tmp = Path(out_dir) / f'.inputs_{mv.frozen_digest(frozen)}_{uuid.uuid4().hex[:8]}'
    if mroot is not None:
        try:
            mv.contained(mroot, Path(out_dir), snap, tmp, *(snap / f for f in files), *(tmp / f for f in files))
        except ValueError as exc:
            return None, f'refusing the input snapshot: {exc}'
    if snap.is_dir():
        if sorted(os.listdir(snap)) == sorted(files) and all(_sha(snap / f) == digests.get(f) for f in files):
            return snap, None
        mv._rmtree_own(snap)
    problem = mv.frozen_problem(vdir, frozen)
    if problem:
        return None, f'{problem}; choose the views again'
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


def upload_problem(out_dir: Path) -> str | None:
    """None when the FINAL copies the backend uploaded (`hyper3d/upload_inputs/`) still have the sizes and sha256 its record lists
    (`rodin_backend.verify_upload_inputs`; a record without `inputs` is legacy = unknown, judged by inputs_problem alone). Checked on
    every resume before a paid collect instead of trusting the saved metadata; a check that cannot run counts as a difference."""
    try:
        folder = str(HERE.parents[1] / 'hyper3d_pipeline')
        if folder not in sys.path:
            sys.path.insert(0, folder)
        import rodin_backend
        problems = rodin_backend.verify_upload_inputs(Path(out_dir))
    except Exception as exc:  # noqa: BLE001  (fail closed before anything is paid)
        return f'the uploaded inputs could not be verified ({type(exc).__name__}: {exc})'
    if problems:
        return ('the images the backend uploaded are not the ones its record lists (' + '; '.join(map(str, problems[:4])) +
                '); nothing more is paid for this record: check it by hand')
    return None


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


GLB_SHA = re.compile(r'[0-9a-f]{64}')


def receipted_glb(record: dict | None, out_dir: Path) -> tuple[Path | None, str | None, str | None]:
    """(path, sha256, problem) of the high-poly GLB the record's EXPORT RECEIPT names (`exports.GLB` {path, sha256, bytes}, written by
    rodin_backend when the export was downloaded and inspected). The path must be a `glb_*.glb` directly in this variant's hyper3d/
    folder and the receipt must carry the file's sha256: only that file, with those bytes, is ever reduced (final-live review,
    "reduction can certify the wrong model"). (None, None, problem) for a record without a usable receipt."""
    exports = record.get('exports') if isinstance(record, dict) else None
    entry = exports.get('GLB') if isinstance(exports, dict) else None
    if not isinstance(entry, dict):
        return None, None, 'the record has no export receipt (exports.GLB)'
    raw, digest = entry.get('path'), entry.get('sha256')
    if not isinstance(raw, str) or not raw:
        return None, None, 'the export receipt names no file'
    if not isinstance(digest, str) or not GLB_SHA.fullmatch(digest):
        return None, None, 'the export receipt carries no sha256 of the exported GLB'
    out_dir = Path(out_dir)
    path = Path(raw) if Path(raw).is_absolute() else out_dir / raw
    try:
        inside = path.parent.resolve() == out_dir.resolve()
    except OSError:
        inside = False
    if not inside or not path.name.startswith('glb_') or path.suffix.lower() != '.glb':
        return None, None, f'the export receipt names {raw!r}, which is not a glb_*.glb of this variant\'s hyper3d folder'
    return out_dir / path.name, digest, None


def glb_problem(path: Path, digest: str, when: str) -> str | None:
    """None when `path` is a regular file whose bytes have the receipted sha256 NOW (hashed, never taken from a cache)."""
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            return f'the receipted GLB {path.name} is missing {when}'
        actual = mv.sha256_file(path)
    except OSError as exc:
        return f'the receipted GLB {path.name} cannot be read {when} ({exc})'
    if actual != digest:
        return (f'{path.name} is not the model the export receipt names {when} (sha256 {actual[:12]} != receipted {digest[:12]}); '
                'it is not reduced')
    return None


def reduced_source_problem(mdir: Path, glb: Path, digest: str) -> str | None:
    """None when reduced/materials.json (the reducer's LAST write) says it reduced exactly the receipted GLB: `source` = its path
    relative to the model folder and `source_sha256` = the receipted digest (reduce_monster.py writes both when it is given `--glb`
    and `--sha256`)."""
    materials = mv.read_json(Path(mdir) / 'reduced' / 'materials.json', None)
    if not isinstance(materials, dict):
        return 'reduced/materials.json is missing or invalid (the reducer did not finish)'
    want = f'hyper3d/{Path(glb).name}'
    if materials.get('source') != want:
        return f'the reducer reduced {materials.get("source")!r}, not the receipted {want}'
    if materials.get('source_sha256') != digest:
        return 'the reducer did not confirm the sha256 of the receipted GLB (materials.json source_sha256)'
    return None


def set_aside_unsubmitted(out_dir: Path) -> tuple[Path | None, str | None]:
    """Archive a record of a PROVEN pre-click failure (mv.proven_unsubmitted) the way the backend does, BY the backend
    (`rodin_backend.set_aside_pre_click`: under the output lock, as `studio_submission.not_submitted_<time>.json`, and only when the
    record read under that lock still proves that nothing was clicked). (archived path, None); (None, None) when the backend left
    the record where it is (not provably pre-click any more); (None, problem) when it could not be asked."""
    try:
        aside = mv.hyper3d_backend().set_aside_pre_click(Path(out_dir), 'the v2 monster lane found a proven pre-click failure')
    except Exception as exc:  # noqa: BLE001  (Stop: the output lock is busy; anything else: fail closed, nothing is submitted)
        return None, f'{type(exc).__name__}: {exc}'
    return (Path(aside) if aside else None), None


def run_conversion(root: Path, monster: str, variant: str, *, frozen_sha: str | None = None, run_now: bool = False,
                   override_pause: str | None = None, idle: float | None = None, idle_known: bool = False, python: str | None = None,
                   say=None, own_process: bool = False) -> dict:
    """The driver of one attempt (see the module docstring). Returns the verdict; never raises for a pipeline failure. `own_process`
    (the `run` sub-command only): this process IS the driver, so it puts itself into the attempt's Job Object (finding 9); a caller that
    runs the driver inside another process (tests) must not."""
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
        held = tree_hold(root, monster, variant)                # a live process of the previous attempt, or an incomplete record of it
        if held:
            return base | {'status': 'busy', 'step': 'check', 'error': held}
        try:
            tracker = _Tracker(root, monster, variant, own_process=own_process)
        except OSError as exc:
            return base | {'status': 'failed', 'step': 'check', 'error': f'the process record of the attempt could not be written ({exc})'}
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

    def unsafe(step: str) -> dict | None:
        """Finding 14: every destination this driver or its children write (hyper3d/ with its record, input snapshot and the backend's
        upload folder, reduced/ and its success record) resolved against the monster root IMMEDIATELY before use; a link or junction
        anywhere on the way is refused."""
        try:
            mv.contained(mroot, vdir, mdir, out_dir, record_path, snapshot_dir(out_dir, frozen) if frozen else out_dir,
                         out_dir / 'upload_inputs', mdir / 'reduced', mdir / 'reduced' / mv.REDUCE_OK, mdir / 'reduce.log')
        except ValueError as exc:
            return verdict('failed', step, f'refusing to write outside the variant: {exc}')
        return None

    def account(files: list[str]) -> None:
        record = mv.record_of(mdir)
        if record is not None or accounting_error(root, record_path):
            accounting['error'] = reconcile_paid(root, monster, variant, files, record_path, record)

    env = dict(os.environ) | paid_gate.env_for(root, override_pause=bool(run_now or override_pause),
                                               cmd=gate_command(root, args, idle, idle_known))
    frozen = None
    blocked = unsafe('check')
    if blocked:
        return blocked
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
    if mv.proven_unsubmitted(record):
        # a PROVEN pre-click failure the backend left in place (it normally sets the record aside itself): nothing was clicked, so
        # the variant is unsubmitted and this attempt submits again. A record with ANY click intent is not touched (it fails below
        # with its status, for review).
        blocked = unsafe('check')
        if blocked:
            return blocked
        aside, problem = set_aside_unsubmitted(out_dir)
        if problem:
            return verdict('failed', 'check', f'the record of the failed submission could not be set aside ({problem})')
        if aside is not None:
            steps.append(f'unsubmitted record set aside ({aside.name})')
        record = mv.record_of(mdir)                             # None now; a record the backend left in place is judged below
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
        blocked = gated('submit') or unsafe('submit')
        if blocked:
            return blocked
        out_dir.mkdir(parents=True, exist_ok=True)
        snap, problem = snapshot_inputs(out_dir, vdir, frozen, mroot)
        if problem:
            return verdict('failed', 'check', problem)
        blocked = unsafe('submit')                              # again, with the folders that exist now
        if blocked:
            return blocked
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
    glb, glb_sha, receipt_problem = (None, None, None)
    if status == 'exported':
        # ONLY the GLB the export receipt names is reduced. A receipt that cannot be used, or a receipted file that is gone while
        # ANOTHER glb_*.glb lies in the folder, stops here: that other file is never taken for the model that was paid for.
        glb, glb_sha, receipt_problem = receipted_glb(record, out_dir)
        if receipt_problem:
            return verdict('failed', 'check', f'{receipt_problem}; nothing is reduced: inspect {record_path.as_posix()}')
        if not glb.exists() and any(out_dir.glob('glb_*.glb')):
            others = sorted(p.name for p in out_dir.glob('glb_*.glb'))
            return verdict('failed', 'check', f'the receipted GLB {glb.name} is missing, but {", ".join(others[:4])} is in '
                                              f'{out_dir.as_posix()}; that is not the model the receipt names and is not reduced: '
                                              'check it by hand')
    if status == 'submitted' or (status == 'exported' and not glb.exists()):
        pending = record.get('pending_action')
        if pending and not free_preview_resolved(record):
            return verdict('failed', 'collect', f'the record has an unresolved {pending!r} click; inspect '
                                                f'{record_path.as_posix()} (hyper3d_single.py inspect) before resuming')
        problem = upload_problem(out_dir)                       # the final upload copies, re-hashed before anything is paid
        if problem:
            return verdict('failed', 'check', problem)
        if not Path(py).is_file():
            return verdict('failed', 'collect', f'the Hyper3D venv python was not found: {py}')
        blocked = gated('collect') or unsafe('collect')
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
        glb, glb_sha, receipt_problem = receipted_glb(record, out_dir) if status == 'exported' else (None, None, None)
    if status != 'exported':
        why = record.get('why') or record.get('pending_action')
        return verdict('failed', 'collect', f'record status {status!r}' + (f' ({why})' if why else '') +
                       f'; inspect {record_path.as_posix()}')
    if receipt_problem:
        return verdict('failed', 'collect', f'{receipt_problem}; nothing is reduced: inspect {record_path.as_posix()}')

    blocked = gated('reduce') or unsafe('reduce')
    if blocked:
        return blocked
    problem = glb_problem(glb, glb_sha, 'before the reduction')  # the receipted bytes, hashed now
    if problem:
        return verdict('failed', 'reduce', f'{problem}; inspect {record_path.as_posix()}')
    redo = ['--redo'] if (mdir / 'reduced').exists() else []
    code, err = _run([sys.executable, '-X', 'utf8', str(mroot / 'tools' / 'reduce_monster.py'), f'{monster}/{variant}', *redo,
                      '--glb', str(glb), '--sha256', glb_sha], mroot, say, None, tracker)
    steps.append(f'reduce exit {code}')
    blocked = unsafe('reduce')
    if blocked:
        return blocked
    problem = mv.reduced_files_problem(mdir)
    if code != 0 or problem:
        return verdict('failed', 'reduce', stopped_message(err) or problem or
                       f'reduce_monster.py exit code {code}; see {(mdir / "reduce.log").as_posix()}')
    # AFTER: the file is still the receipted model, and the reducer says that file and digest is what it reduced. Only then is
    # the reduction certified (with its source).
    problem = glb_problem(glb, glb_sha, 'after the reduction') or reduced_source_problem(mdir, glb, glb_sha)
    if problem:
        return verdict('failed', 'reduce', f'the reduction is not certified: {problem}')
    try:
        mv.write_reduce_ok(mdir, frozen, source={'file': f'hyper3d/{glb.name}', 'sha256': glb_sha})
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
    run.add_argument('--override-pause', default=None, metavar='PAUSE_ID')
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
                                override_pause=args.override_pause, own_process=True)
    except Exception as exc:  # noqa: BLE001  (the verdict line is always written)
        result = {'status': 'failed', 'step': 'driver', 'error': f'{type(exc).__name__}: {exc}', 'monster': args.monster,
                  'variant': args.variant}
    sys.stdout.flush()
    print(json.dumps(result), flush=True)
    return 0 if result.get('status') == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main())
