"""The orchestrator of the v2 preview app (DESIGN_ORCHESTRATOR.md section 2, DESIGN_PHASE3.md sections A and E): runs the queued layer builds.

A thread owned by the one server that holds `.codex_jobs/_app/server.lock` (and, besides, its own `orchestrator.lock`). Every 3 s it
  1. FINISHES active attempts: a result file WITH THE ATTEMPT'S TOKEN -> done / failed (done needs the builder's verdict on stdout AND the
     matching valid publication); a dead process without a result = lost; a Kill & retry request kills the builder, then the wrapper;
  2. RECONCILES the queue with the decisions (the queue is a derived cache): a damaged database is kept aside and rebuilt from the surviving
     wrappers' claim files; interrupted file transactions are recovered; every approved unit without a valid publication and without a live /
     queued task is queued; queued tasks without a matching approval are cancelled; a ready.json whose approval no longer holds is revoked;
  3. STARTS queued tasks while `active < limit` (one pool shared by the `bare` and `layers` lanes; a `layers` task waits while the bare build
     of its race is queued or running and no current bare build exists). Before any attempt the task's approval is checked again (withdrawn ->
     cancelled) and, for layers, the pinned source GLB digest (changed -> failed 'source changed');
and, every 10 minutes and at start, runs the stale scan in its own thread (fingerprints of the published builds -> `.codex_jobs/_app/stale.json`).

ATTEMPTS (phase 3 review F5). Before anything is spawned the attempt is COMMITTED: the row goes queued -> starting (only from queued) with a new
uuid token and attempt-specific paths `logs/<id>-a<n>.log / .err.log / .result.json / .claim.json / .task.json`. The detached wrapper
(`run_task.py <task json> --token <token>`) first writes the claim file {token, wrapper pid + create time}, then adds the builder's pid + create
time. After a crash a `starting` attempt with a claim (or, without one, a process whose command line carries the token) is adopted as running;
only then is it declared lost. Results are accepted only with the attempt's token, and a row is finished only while it still carries it.

Heartbeat (review F10): the attempt's two logs growing. An active attempt whose logs have not grown for `build_stall_minutes` (settings.json,
default 30) is shown as stalled in the queue, with Kill & retry. Pool limit: `layer_workers_present` (default 1) while the user is at the PC,
`layer_workers_away` (default 4) when away (idle >= 300 s: presence.json when fresh, else GetLastInputInfo); 0 while the supervisor mode is
`pause`. Retries: 2 automatic ones (60 s backoff) for a lost task, `inputs changed during build` and transient failures, and only while the
approval still holds; none for invalid input, a missing required slot or a changed source (those need a human).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import serve  # noqa: E402  (tools/preview/serve.py, used as a library)
import actions  # noqa: E402
import builds  # noqa: E402
import catalog  # noqa: E402
import decisions  # noqa: E402
import guard  # noqa: E402
import taskqueue  # noqa: E402

try:
    import psutil  # noqa: E402
except ImportError:      # the conservative fallback below
    psutil = None

REPO = HERE.parents[2]
RUN_TASK = HERE / 'run_task.py'
BUILD_PY = REPO / 'tools' / 'layers' / 'build.py'
PRESENCE_FILE = Path('C:/Users/niall/.claude/presence.json')
PRESENCE_FRESH_S = 30.0
AWAY_IDLE_S = 300.0
INTERVAL = 3.0
STALE_INTERVAL = 600.0
BACKOFF = 60.0
CREATE_TIME_TOLERANCE = 2.0
ADOPT_CAP_S = 6 * 3600.0              # without psutil a pid cannot be proven to be the task's process: trust it for this long at most
DEFAULTS = {'layer_workers_present': 1, 'layer_workers_away': 4, 'build_stall_minutes': 30}
MINIMUM = {'layer_workers_present': 0, 'layer_workers_away': 0, 'build_stall_minutes': 1}
TASK_ENV = {'BODY_FIT_GPU': '1', 'PYTHONUTF8': '1', 'OMP_NUM_THREADS': '1'}
DETACHED = (getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) | getattr(subprocess, 'CREATE_NO_WINDOW', 0))
NO_VERDICT = 'the builder exited 0 without a verdict on stdout'
# failure classification (see the module docstring): invalid input never retries, anything else without a clear verdict is transient
INVALID = re.compile(r'missing input|no garment found|required slot|invalid race|unknown module|argument error|must be one of|module dir not found|'
                     r'invalid input|not a single|missing game body cache|cannot read the inputs|source changed|without a verdict', re.I)
TRANSIENT = re.compile(r'inputs changed during build|PermissionError|timed out|MemoryError|out of memory|device lost|GPU|CUDA|Errno|busy|lost', re.I)


# --------------------------------------------------------------------------------------------------------------------- locks

class FileLock:
    """A single-instance lock file (OS byte lock, released by the OS when the holder dies), like guard.ServerLock."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, 'a+b')
        if not guard.try_lock(handle):
            handle.close()
            return False
        self.handle = handle
        try:
            handle.seek(0)
            handle.truncate(0)
            handle.write(f'{os.getpid()}\n'.encode())
            handle.flush()
        except OSError:
            pass
        return True

    def release(self) -> None:
        handle, self.handle = self.handle, None
        if handle:
            try:
                guard.release_lock(handle)
            except OSError:
                pass
            handle.close()


# ------------------------------------------------------------------------------------------------------ presence / settings

def win_idle_seconds() -> float | None:
    """Seconds since the last keyboard / mouse input (GetLastInputInfo); None when it cannot be read (not Windows, no session)."""
    if os.name != 'nt':
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]
        info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        ctypes.windll.kernel32.GetTickCount.restype = wintypes.DWORD
        return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:  # noqa: BLE001
        return None


def idle_seconds(now: float | None = None) -> float | None:
    """How long the user has been idle: presence.json when it was written in the last 30 s, else GetLastInputInfo, else None (unknown = present)."""
    now = time.time() if now is None else now
    try:
        data = json.loads(PRESENCE_FILE.read_text(encoding='utf-8-sig'))
        if now - float(data['updated_unix']) < PRESENCE_FRESH_S:
            return float(data['user_idle_seconds'])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return win_idle_seconds()


def read_settings(root: Path) -> dict:
    """`.codex_jobs/_app/settings.json`: the worker limits and `build_stall_minutes` (integers; invalid values take the default)."""
    out = dict(DEFAULTS)
    try:
        raw = json.loads((Path(root) / guard.APP / 'settings.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return out
    if isinstance(raw, dict):
        for key in DEFAULTS:
            if isinstance(raw.get(key), int) and not isinstance(raw[key], bool) and raw[key] >= MINIMUM[key]:
                out[key] = raw[key]
    return out


def supervisor_mode(root: Path) -> str | None:
    try:
        raw = json.loads((Path(root) / '.codex_jobs' / '_batch' / 'supervisor_mode.json').read_text(encoding='utf-8-sig'))
        return raw.get('mode') if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None


def pool_status(root: Path, idle=None, mode=None) -> dict:
    """{"limit", "present", "idle", "mode"}: how many layer builds may run at once right now."""
    idle = idle_seconds() if idle is None else idle
    settings = read_settings(root)
    mode = supervisor_mode(root) if mode is None else mode
    present = idle is None or idle < AWAY_IDLE_S
    limit = 0 if mode == 'pause' else settings['layer_workers_present' if present else 'layer_workers_away']
    return {'limit': limit, 'present': present, 'idle': idle, 'mode': mode}


# ------------------------------------------------------------------------------------------------------------------ processes

def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if psutil is not None:
        try:
            return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False
    if os.name == 'nt':
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259     # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def create_time(pid: int | None) -> float | None:
    if psutil is None or not pid:
        return None
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return None


def cmdline(pid: int | None) -> list[str] | None:
    if psutil is None or not pid:
        return None
    try:
        return [str(a) for a in psutil.Process(pid).cmdline()]
    except psutil.Error:
        return None


def identity_ok(pid: int | None, recorded_create: float | None, started: float | None, now: float) -> bool:
    """`pid` is alive and is the recorded process (create time within the tolerance; without one: alive and recent)."""
    if not pid_alive(pid):
        return False
    actual = create_time(pid)
    if recorded_create is not None and actual is not None:
        return abs(recorded_create - actual) <= CREATE_TIME_TOLERANCE
    return (now - (started or 0)) < ADOPT_CAP_S


def same_process(task: dict, now: float) -> bool:
    """The task's recorded wrapper process is still alive and is the same process."""
    return identity_ok(task.get('pid'), task.get('pid_create'), task.get('started'), now)


def find_token_process(token: str | None) -> tuple[int, float | None] | None:
    """(pid, create time) of a live process whose command line carries `token` (a wrapper started just before a crash), else None."""
    if psutil is None or not token:
        return None
    for proc in psutil.process_iter(['pid', 'cmdline', 'create_time']):
        try:
            if token in (proc.info.get('cmdline') or []):
                return proc.info['pid'], proc.info.get('create_time')
        except (psutil.Error, TypeError):
            continue
    return None


def attempt_paths(logs: Path, task_id: int, attempt: int) -> dict[str, Path]:
    """The files of one attempt: `<id>-a<n>.log / .err.log / .result.json / .claim.json / .task.json`."""
    base = f'{task_id}-a{attempt}'
    return {'log': logs / f'{base}.log', 'err': logs / f'{base}.err.log', 'result': logs / f'{base}.result.json',
            'claim': logs / f'{base}.claim.json', 'spec': logs / f'{base}.task.json'}


def paths_of(task: dict) -> dict[str, Path] | None:
    log = task.get('log')
    if not log or not str(log).endswith('.log'):
        return None
    base = str(log)[:-4]
    return {'log': Path(log), 'err': Path(base + '.err.log'), 'result': Path(base + '.result.json'), 'claim': Path(base + '.claim.json'),
            'spec': Path(base + '.task.json')}


def result_path(task: dict) -> Path | None:
    paths = paths_of(task)
    return paths['result'] if paths else None


def _json_file(path: Path | None) -> dict | None:
    if path is None:
        return None
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


last_output = builds.last_output          # the heartbeat: when the attempt's two logs last grew
stalled = builds.is_stalled                # an active attempt whose logs have not grown for build_stall_minutes


def classify(result: dict) -> tuple[bool, str | None, bool]:
    """(ok, error, retryable) of a result record `{exit, last_json, ...}`. Success needs exit 0 AND a builder verdict `ok` / `current` on
    stdout; exit 0 without a verdict is a failure (review F10)."""
    last = result.get('last_json') if isinstance(result.get('last_json'), dict) else None
    if result.get('exit') == 0 and last and last.get('status') in ('ok', 'current'):
        return True, None, False
    if result.get('exit') == 0 and last is None and not result.get('start_error'):
        return False, NO_VERDICT, False
    error = (last or {}).get('error') or result.get('start_error') or (f'unexpected verdict {last.get("status")!r}' if last else None) or \
        f'exit code {result.get("exit")}'
    error = str(error)
    if INVALID.search(error):
        return False, error, False
    retryable = bool((last or {}).get('retryable')) or last is None or bool(TRANSIENT.search(error))
    return False, error, retryable


# ------------------------------------------------------------------------------------------------------------ orchestrator

class SimulatedCrash(BaseException):
    """Tests: raised by a fault hook to stop the orchestrator at an exact point (as a crash would)."""


class Orchestrator:
    """The reconciler. `start()` takes the lock and the thread; tests call `adopt()` / `tick()` / `reconcile()` directly.
    `require_approval` / `reconcile_queue` (default on) can be switched off by tests of the raw queue mechanics."""

    def __init__(self, root: Path, *, argv_for=None, idle=None, interval: float = INTERVAL, stale_interval: float = STALE_INTERVAL,
                 scan_stale: bool = True, require_approval: bool = True, reconcile_queue: bool = True) -> None:
        self.root = Path(root)
        self.queue = taskqueue.for_root(self.root)
        self.argv_for = argv_for or self.default_argv
        self.idle = idle or idle_seconds
        self.interval = interval
        self.stale_interval = stale_interval
        self.scan_stale = scan_stale
        self.require_approval = require_approval
        self.reconcile_queue = reconcile_queue
        self.lock = FileLock(self.root / guard.APP / 'orchestrator.lock')
        self.logs = self.root / guard.APP / 'logs'
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.stale_thread: threading.Thread | None = None
        self.last_stale = 0.0
        self.errors: list[str] = []
        self.notes: list[str] = []
        self.active = False
        self.faults: dict = {}                         # tests: {'claimed' | 'spawned': fn(task)} called at those points of a start
        self._procs: list[subprocess.Popen] = []       # the wrappers this instance started (polled, so none is left un-reaped)

    # ------------------------------------------------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        """Take the orchestrator lock and start the thread. False (and nothing running) when another process holds the lock."""
        if self.active:
            return True
        if not self.lock.acquire():
            return False
        self.active = True
        self.stop_event.clear()
        for step in (self.adopt, self.reconcile):
            try:
                step()
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f'{step.__name__}: {exc}')
        self.thread = threading.Thread(target=self._loop, name='orchestrator', daemon=True)
        self.thread.start()
        return True

    def stop(self, timeout: float = 15.0) -> None:
        """Stop the thread and release the lock. Running tasks are NOT touched (they are detached)."""
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout)
        if self.stale_thread:
            self.stale_thread.join(timeout)
        self.active = False
        self.lock.release()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f'{type(exc).__name__}: {exc}')
                self.errors = self.errors[-20:]
                sys.stderr.write(traceback.format_exc())
            self.stop_event.wait(self.interval)

    def _fault(self, stage: str, task: dict) -> None:
        hook = self.faults.get(stage)
        if hook:
            hook(task)

    # ------------------------------------------------------------------------------------------------------------ adopt
    def adopt(self) -> dict:
        """At start: every starting / running attempt is finished (its result is there), adopted (its wrapper is alive and the same, or a
        `starting` one has a claim / a process with its token) or lost."""
        now = time.time()
        summary = {'adopted': 0, 'finished': 0, 'lost': 0}
        for task in self.queue.tasks(state=taskqueue.ACTIVE):
            outcome = self._check_active(task, now)
            if outcome in ('alive', 'adopted'):
                summary['adopted'] += 1
                self.notes.append(f'adopted task {task["id"]} attempt {task["attempts"]}')
            elif outcome in ('finished', 'killed'):
                summary['finished'] += 1
            elif outcome == 'lost':
                summary['lost'] += 1
        if any(summary.values()):
            catalog.invalidate()
        return summary

    # -------------------------------------------------------------------------------------------------------------- tick
    def tick(self, now: float | None = None) -> dict:
        """One reconcile pass: finish, reconcile, start. Returns {"finished", "started", "limit", "running"}."""
        now = time.time() if now is None else now
        finished = 0
        for task in self.queue.tasks(state=taskqueue.ACTIVE):
            finished += self._check_active(task, now) in ('finished', 'lost', 'killed')
        if self.reconcile_queue:
            try:
                self.reconcile()
            except Exception as exc:  # noqa: BLE001  (one bad set must not stop the builds)
                self.errors = (self.errors + [f'reconcile: {type(exc).__name__}: {exc}'])[-20:]
        pool = pool_status(self.root, self.idle(), None)
        started = self._start_queued(pool['limit'], now)
        if self.scan_stale and now - self.last_stale >= self.stale_interval:
            self.last_stale = now
            self._stale_async()
        if finished or started:
            catalog.invalidate()
        return {'finished': finished, 'started': started, 'limit': pool['limit'], 'running': self.queue.count(taskqueue.ACTIVE)}

    # ------------------------------------------------------------------------------------------------------- active ones
    def _read_result(self, task: dict) -> dict | None:
        """The attempt's result file, only when it carries the attempt's token (a late receipt of another attempt is ignored)."""
        value = _json_file(result_path(task))
        if value is None or value.get('token') != task.get('token'):
            return None
        return value

    def _claim(self, task: dict) -> dict | None:
        paths = paths_of(task)
        value = _json_file(paths['claim']) if paths else None
        if value is None or not task.get('token') or value.get('token') != task.get('token'):
            return None
        return value

    def _check_active(self, task: dict, now: float) -> str:
        """Settle one starting / running attempt: 'finished', 'lost', 'killed', 'adopted' (starting -> running) or 'alive'."""
        result = self._read_result(task)
        if result is not None:
            self._finish(task, result)
            return 'finished'
        if task.get('kill_requested'):
            self._kill(task)
            return 'killed'
        claim = self._claim(task)
        if task['state'] == 'starting':
            if claim and identity_ok(claim.get('wrapper_pid'), claim.get('wrapper_create'), task.get('started'), now):
                pid, created = claim['wrapper_pid'], claim.get('wrapper_create')
            else:
                found = find_token_process(task.get('token'))
                if found is None:
                    self._lost(task)
                    return 'lost'
                pid, created = found
            if self.queue.mark_running(task['id'], task.get('token'), pid, created):
                if claim and claim.get('builder_pid'):
                    self.queue.set_builder(task['id'], task.get('token'), claim['builder_pid'], claim.get('builder_create'))
                return 'adopted'
            return 'alive'
        if claim and claim.get('builder_pid') and claim['builder_pid'] != task.get('builder_pid'):
            self.queue.set_builder(task['id'], task.get('token'), claim['builder_pid'], claim.get('builder_create'))
        if same_process(task, now):
            return 'alive'
        result = self._read_result(task)                       # the wrapper may have written it just before it exited
        if result is not None:
            self._finish(task, result)
            return 'finished'
        self._lost(task)
        return 'lost'

    def _approved(self, task: dict) -> bool:
        return not self.require_approval or actions.task_approved(self.root, task)

    def _publication_problem(self, task: dict, verdict: dict) -> str | None:
        """None when the build the verdict names is the valid publication now (layers: current_<lod>.json of the module; bare: current_bare.json)."""
        build_id = verdict.get('build_id') if isinstance(verdict, dict) else None
        if not isinstance(build_id, str):
            return 'the builder verdict names no build'
        args = task.get('args') or {}
        if task['lane'] == 'bare':
            ptr = builds.bare_pointer(str(args.get('race') or task.get('unit')))
        else:
            if not args.get('module_dir') or args.get('lod') not in decisions.LOD_LEVELS:
                return 'the task names no module / LOD'
            ptr = builds.pointer_at(Path(args['module_dir']), args['lod'])
        if not ptr or ptr.get('build_id') != build_id:
            return f'the builder reported {build_id} but it is not the valid publication'
        return None

    def _finish(self, task: dict, result: dict) -> None:
        ok, error, retryable = classify(result)
        if ok:
            problem = self._publication_problem(task, result.get('last_json'))
            if problem is None:
                self.queue.finish(task['id'], task.get('token'), True, result)
                return
            ok, error, retryable = False, problem, True
        if retryable and task['attempts'] < task['max_attempts'] and self._approved(task):
            self.queue.requeue(task['id'], task.get('token'), error or 'failed', BACKOFF)
        else:
            self.queue.finish(task['id'], task.get('token'), False, result, error)

    def _lost(self, task: dict) -> None:
        """An active attempt whose process is gone and left no result."""
        error = 'lost: the build process died without a result'
        if task['attempts'] < task['max_attempts'] and self._approved(task):
            self.queue.requeue(task['id'], task.get('token'), error, BACKOFF)
        else:
            self.queue.finish(task['id'], task.get('token'), False, None, error)

    def _verified(self, pid: int | None, recorded_create: float | None, must_contain: list[str]) -> bool:
        """`pid` is alive, has the recorded create time and its command line contains every string of `must_contain`."""
        if psutil is None or not pid or not pid_alive(pid):
            return False
        actual = create_time(pid)
        if recorded_create is None or actual is None or abs(actual - recorded_create) > CREATE_TIME_TOLERANCE:
            return False
        line = cmdline(pid) or []
        norm = [os.path.normcase(a) for a in line]
        return all(os.path.normcase(str(m)) in norm for m in must_contain)

    def _kill(self, task: dict) -> None:
        """Kill & retry: the builder first, then the wrapper, each by PID after its command line and create time were checked; then a fresh
        attempt (attempts reset: a manual retry) while the approval holds, else failed."""
        claim = self._claim(task) or {}
        killed = []
        argv = [a for a in (claim.get('argv') or [])[1:]]
        for role, pid, created, marks in (('builder', claim.get('builder_pid') or task.get('builder_pid'),
                                           claim.get('builder_create') if claim.get('builder_pid') else task.get('builder_create'), argv),
                                          ('wrapper', task.get('pid'), task.get('pid_create'), [task.get('token')] if task.get('token') else [str(RUN_TASK)])):
            if self._verified(pid, created, marks):
                try:
                    psutil.Process(pid).kill()
                    killed.append(f'{role} {pid}')
                except psutil.Error:
                    pass
        detail = 'killed by the user (Kill & retry)' + (f': {", ".join(killed)}' if killed else ': no matching process was running')
        if self._approved(task):
            self.queue.requeue(task['id'], task.get('token'), detail, 0.0, reset=True)
        else:
            self.queue.finish(task['id'], task.get('token'), False, None, detail + '; the approval was withdrawn, not retried')
        self.notes.append(f'task {task["id"]}: {detail}')

    # ---------------------------------------------------------------------------------------------------------- reconcile
    def reconcile(self) -> dict:
        """Make the queue agree with the decisions (the queue is a derived cache, review F7 / F9). Idempotent."""
        out = {'repaired': None, 'recovered': [], 'queued': {}, 'cancelled': 0, 'revoked': []}
        health = self.queue.check()
        if health.get('corrupt'):
            out['repaired'] = self.repair_queue()
        out['recovered'] = actions.recover_transactions(self.root)
        derived = catalog.derive_all(self.root)
        for name, info in sorted(derived['sets'].items()):
            if info['decisions_error'] or info.get('recovering'):
                continue
            if info['locked'] and info['stage'] in ('3d', 'aligned', 'completed') and self._needs_work(info):
                with decisions.set_lock(self.root, name):
                    catalog.invalidate()
                    fresh = catalog.derive_one(self.root, name)
                    if fresh and fresh['locked'] and not fresh['decisions_error']:
                        done = actions.ensure_set_tasks(self.root, fresh)
                        if any(v in ('queued', 'retried') for v in done.values()):
                            out['queued'][name] = done
            if info.get('ready') is not None and info['stage'] != 'completed':
                revoked = self._revoke(name)
                if revoked:
                    out['revoked'].append(revoked)
        out['cancelled'] = self._cancel_withdrawn(derived)
        if out['queued'] or out['cancelled'] or out['revoked'] or out['recovered'] or out['repaired']:
            catalog.invalidate()
        return out

    @staticmethod
    def _needs_work(info: dict) -> bool:
        if any(rec['status'] == 'none' for rec in info['builds'].values()):
            return True
        return any(not builds.bare_pointer(race) for race in info['races'])

    def _cancel_withdrawn(self, derived: dict) -> int:
        """Cancel queued tasks whose approval is gone or changed (and queued bare tasks no approved set needs)."""
        cancelled = 0
        for task in self.queue.tasks(state='queued', limit=5000):
            if self._approved_in(derived, task):
                continue
            if self.queue.cancel(task['id'], 'no matching approval (reconcile)'):
                cancelled += 1
        return cancelled

    @staticmethod
    def _approved_in(derived: dict, task: dict) -> bool:
        args = task.get('args') if isinstance(task.get('args'), dict) else {}
        if task['lane'] == 'bare':
            race = args.get('race') or task.get('unit')
            return any(info['locked'] and race in info['races'] for info in derived['sets'].values())
        info = derived['sets'].get(task.get('set_name'))
        if not info or info['decisions_error']:
            return False
        entry = ((info['decisions']['lod_approval'] or {}).get('units') or {}).get(task.get('unit'))
        return isinstance(entry, dict) and entry.get('job') == args.get('job') and entry.get('lod') == args.get('lod') and \
            (not args.get('glb_sha256') or entry.get('glb_sha256') == args.get('glb_sha256'))

    def _revoke(self, name: str) -> str | None:
        """Revoke models/<set>/ready.json when (rechecked under the set lock) its approval no longer holds: renamed, never deleted, event logged."""
        with decisions.set_lock(self.root, name):
            catalog.invalidate()
            fresh = catalog.derive_one(self.root, name)
            if not fresh or fresh.get('ready') is None or fresh['stage'] == 'completed' or fresh.get('recovering'):
                return None
            reason = '; '.join(fresh.get('not_completed') or [f'the set is in {fresh["stage"]}'])[:300]
            revoked = decisions.revoke_ready(self.root, name, reason)
        if revoked:
            self.queue.event('revoke', f'{name}: {revoked}: {reason}')
            serve.log_action(self.root, 'revoke_ready', f'models/{name}/ready.json', f'models/{name}/{revoked}', reason[:200])
            self.notes.append(f'revoked {name}/ready.json: {reason}')
        return revoked

    def repair_queue(self) -> dict:
        """The database is damaged: keep it as `.corrupt-<time>`, start a fresh one, re-adopt every SURVIVING wrapper from its claim file (so
        nothing is started twice); the rest of the work is queued again from the decisions by reconcile."""
        kept = self.queue.repair()
        adopted = 0
        now = time.time()
        for claim_file in sorted(self.logs.glob('*.claim.json')) if self.logs.is_dir() else []:
            claim = _json_file(claim_file)
            task = (claim or {}).get('task') if isinstance((claim or {}).get('task'), dict) else None
            if not claim or not task or not claim.get('token') or _json_file(Path(str(claim_file)[:-len('.claim.json')] + '.result.json')):
                continue
            if not identity_ok(claim.get('wrapper_pid'), claim.get('wrapper_create'), claim.get('claimed'), now):
                continue
            try:
                new_id = self.queue.insert_running(task.get('lane'), task.get('key'), task.get('set'), task.get('unit'), task.get('args') or {},
                                                   int(claim.get('attempt') or 1), claim['token'], claim.get('log'), claim.get('wrapper_pid'),
                                                   claim.get('wrapper_create'), claim.get('claimed'), int(task.get('max_attempts') or 3))
            except (taskqueue.QueueError, ValueError, TypeError):
                continue
            if new_id is not None:
                adopted += 1
                if claim.get('builder_pid'):
                    self.queue.set_builder(new_id, claim['token'], claim['builder_pid'], claim.get('builder_create'))
        note = f'queue database was damaged: kept as {kept.name if kept else "-"}, rebuilt; {adopted} running build(s) re-adopted from their claims'
        self.notes.append(note)
        self.queue.event('repair', note)
        return {'kept': str(kept) if kept else None, 'adopted': adopted}

    # ------------------------------------------------------------------------------------------------------------ starting
    def default_argv(self, task: dict) -> list[str]:
        args = task.get('args') or {}
        if task['lane'] == 'bare':
            return [sys.executable, str(BUILD_PY), 'bare', str(args['race'])]
        argv = [sys.executable, str(BUILD_PY), 'module', str(args['module_dir']), '--lod', str(args['lod'])]
        if isinstance(args.get('glb_sha256'), str):
            argv += ['--expect-source-sha256', args['glb_sha256']]
        return argv

    def _bare_current(self, race: str) -> bool:
        ptr = builds.bare_pointer(race)
        if not ptr:
            return False
        try:
            return ptr.get('fingerprint') == builds.fingerprint_bare(race)
        except OSError:
            return False

    def _start_queued(self, limit: int, now: float) -> int:
        running = self.queue.count(taskqueue.ACTIVE)
        if running >= limit:
            return 0
        open_bare = {t['unit'] for t in self.queue.tasks(state=taskqueue.OPEN, lane='bare')}
        started = 0
        for task in self.queue.next_queued(taskqueue.LANES, 100, now):
            if running >= limit:
                break
            args = task.get('args') or {}
            if task['lane'] == 'layers':
                race = str(args.get('race') or '')
                if race in open_bare and not self._bare_current(race):
                    continue                                   # the bare layers of its race come first
            if not self._approved(task):                       # withdrawn while it waited: never started (review F6)
                self.queue.cancel(task['id'], 'approval withdrawn before start')
                continue
            if task['lane'] == 'layers' and isinstance(args.get('glb_sha256'), str) and args.get('module_dir') and args.get('lod'):
                if builds.cached_sha256(builds.source_glb(Path(args['module_dir']), args['lod'])) != args['glb_sha256']:
                    self.queue.fail_queued(task['id'], 'source changed: the LOD GLB is not the one the 3D approval pinned (send the set back to 3D)')
                    continue
            if self._spawn(task):
                running += 1
                started += 1
        return started

    def _spawn(self, task: dict) -> bool:
        """Commit the attempt (queued -> starting, token, attempt paths), write its task file and start the detached wrapper. A start that fails
        fails the attempt (no retry). False when the row was no longer queued."""
        attempt = task['attempts'] + 1
        while attempt_paths(self.logs, task['id'], attempt)['spec'].exists():     # (a manual retry resets the count: files stay unique)
            attempt += 1
        token = uuid.uuid4().hex
        paths = attempt_paths(self.logs, task['id'], attempt)
        self.logs.mkdir(parents=True, exist_ok=True)
        if not self.queue.claim(task['id'], token, str(paths['log'])):
            return False
        self._fault('claimed', task)
        try:
            for key in ('result', 'claim'):
                paths[key].unlink(missing_ok=True)
            spec = {'id': task['id'], 'attempt': attempt, 'token': token, 'argv': self.argv_for(task), 'env': TASK_ENV, 'log': str(paths['log']),
                    'err': str(paths['err']), 'result': str(paths['result']), 'claim': str(paths['claim']), 'cwd': str(REPO),
                    'task': {'key': task['key'], 'lane': task['lane'], 'set': task.get('set_name'), 'unit': task.get('unit'),
                             'args': task.get('args') or {}, 'max_attempts': task.get('max_attempts')}}
            paths['spec'].write_text(json.dumps(spec), encoding='utf-8')
            kwargs = {'creationflags': DETACHED} if os.name == 'nt' else {'start_new_session': True}
            proc = subprocess.Popen([sys.executable, str(RUN_TASK), str(paths['spec']), '--token', token], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, cwd=str(REPO), **kwargs)
        except Exception as exc:  # noqa: BLE001
            self.queue.finish(task['id'], token, False, None, f'could not start: {type(exc).__name__}: {exc}')
            return False
        self._procs = [p for p in self._procs if p.poll() is None] + [proc]
        self._fault('spawned', task)
        self.queue.mark_running(task['id'], token, proc.pid, create_time(proc.pid))
        return True

    # ------------------------------------------------------------------------------------------------------------ stale scan
    def _stale_async(self) -> None:
        if self.stale_thread and self.stale_thread.is_alive():
            return
        self.stale_thread = threading.Thread(target=self._stale_safe, name='stale-scan', daemon=True)
        self.stale_thread.start()

    def _stale_safe(self) -> None:
        try:
            self.stale_scan()
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f'stale scan: {type(exc).__name__}: {exc}')

    def stale_scan(self) -> dict:
        """Compare the fingerprint of every PUBLISHED build of the sets in 3d / aligned / completed with the current one; writes stale.json."""
        return scan_stale(self.root)


def scan_stale(root: Path) -> dict:
    """The stale scan (also callable without an orchestrator). A unit is stale when its published build's fingerprint is no longer the one
    `build.fingerprint_module` gives now (code, game body, align, LOD file or manifest changed since). Bare layers the same per race."""
    root = Path(root)
    derived = catalog.derive_all(root)
    units: dict[str, dict] = {}
    bare: dict[str, dict] = {}
    races: set[str] = set()
    for name, info in derived['sets'].items():
        if info['stage'] not in ('3d', 'aligned', 'completed') or not info['locked']:
            continue
        for unit in info['units']:
            key = f'{unit["race"]}/{unit["module"]}'
            rec = (info.get('builds') or {}).get(key)
            if unit['status'] != 'reduced' or not rec or rec['status'] not in builds.BUILT:
                continue
            races.add(unit['race'])
            module = builds.module_dir(root, unit['model_set'], unit['job'], unit['race'], unit['module'])
            ptr = builds.pointer_at(module, rec['lod'])
            if not ptr:
                continue
            try:
                current = builds.fingerprint_module(module, rec['lod'])
            except (OSError, ValueError):
                continue
            if ptr.get('fingerprint') != current:
                units[f'{name}/{key}'] = {'build_id': ptr['build_id'], 'lod': rec['lod'], 'fingerprint': ptr.get('fingerprint'), 'current': current}
    for race in sorted(races):
        ptr = builds.bare_pointer(race)
        if ptr:
            try:
                current = builds.fingerprint_bare(race)
            except (OSError, ValueError):
                continue
            if ptr.get('fingerprint') != current:
                bare[race] = {'build_id': ptr['build_id'], 'fingerprint': ptr.get('fingerprint'), 'current': current}
    data = {'time': time.time(), 'units': units, 'bare': bare}
    builds.write_stale(root, data)
    catalog.invalidate()
    return data


# ----------------------------------------------------------------------------------------------------------- queue endpoint

OPEN_PAGE = 1000
RECENT_PAGE = 500


def log_tail(task: dict, size: int = 600, which: str = 'log') -> str:
    paths = paths_of(task)
    path = paths.get(which) if paths else None
    if not path:
        return ''
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - size))
            return handle.read().decode('utf-8', 'replace').lstrip()
    except OSError:
        return ''


def task_view(task: dict, now: float, stall_minutes: int = DEFAULTS['build_stall_minutes']) -> dict:
    started, finished = task.get('started'), task.get('finished')
    shows_log = task['state'] in ('starting', 'running', 'failed', 'done')
    return {'id': task['id'], 'lane': task['lane'], 'set': task['set_name'], 'unit': task['unit'], 'state': builds.shown_state(task, now, stall_minutes),
            'stored_state': task['state'], 'attempts': task['attempts'],
            'max_attempts': task['max_attempts'], 'started': started, 'finished': finished,
            'elapsed': round((finished or now) - started, 1) if started else None, 'error': task.get('error'),
            'log_tail': log_tail(task) if shows_log else '', 'err_tail': log_tail(task, 400, 'err') if task['state'] in ('running', 'failed') else '',
            'key': task['key'], 'stalled': stalled(task, now, stall_minutes), 'last_output': last_output(task) if task['state'] in taskqueue.ACTIVE else None,
            'kill_requested': bool(task.get('kill_requested'))}


def queue_state(root: Path, active: bool, now: float | None = None) -> dict:
    """GET /api/v2/queue: active and queued tasks plus failed / done / cancelled ones of the last 24 h, newest first. `totals` (per state) and
    `workers.running` are SQL counts over the whole table, independent of the page; `page` says how much of each group is shown (review F16)."""
    now = time.time() if now is None else now
    queue = taskqueue.for_root(root)
    since = now - 86400
    finished_states = ('failed', 'done', 'cancelled')
    open_rows = queue.tasks(state=taskqueue.OPEN, limit=OPEN_PAGE)
    recent = queue.tasks(state=finished_states, since=since, limit=RECENT_PAGE)
    totals = queue.totals()
    open_total = sum(totals.get(s, 0) for s in taskqueue.OPEN)
    recent_total = queue.count(finished_states, since)
    tasks = sorted(open_rows + recent, key=lambda t: -t['id'])
    pool = pool_status(root)
    stall = read_settings(root)['build_stall_minutes']
    views = [task_view(t, now, stall) for t in tasks]
    return {'tasks': views,
            'workers': {'limit': pool['limit'], 'running': sum(totals.get(s, 0) for s in taskqueue.ACTIVE), 'present': pool['present'],
                        'mode': pool['mode'], 'idle': pool['idle'], 'orchestrator': bool(active)},
            'totals': totals, 'stalled': sum(v['stalled'] for v in views), 'stall_minutes': stall,
            'page': {'open_limit': OPEN_PAGE, 'open_total': open_total, 'open_shown': len(open_rows), 'recent_limit': RECENT_PAGE,
                     'recent_total': recent_total, 'recent_shown': len(recent), 'recent_since': since,
                     'truncated': open_total > len(open_rows) or recent_total > len(recent)},
            'queue_health': queue.health_view()}
