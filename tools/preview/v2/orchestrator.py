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

A WORKER SLOT is held until both the wrapper and its builder are confirmed gone (re-check R6): a dead wrapper whose builder still runs makes the
row `orphan` (it keeps its slot and is shown with Kill); Kill & retry needs the attempt token the client saw, kills each process only after its
identity was verified, and requeues only once both are gone (a refused or failed kill leaves the row and its slot, with the reason as error).
Whenever the queue database is missing, empty or repaired, the surviving attempts (claim files, and live processes carrying a task file's token
on their command line) are adopted BEFORE reconcile allocates any work (re-check R5). Reconcile isolates each set (a failure is logged and the
pass goes on, R10) and QUARANTINES an active ready.json that can no longer be verified (unreadable decisions, bytes or approval id that are not
the approval's: renamed `ready.json.quarantine-<time>`, bytes kept, event `quarantine`, R4). Bare builds are written to and read from
`<root>/tools/preview/cache/layers/<race>/` (`--out` is always passed, R13).

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
import settings as app_settings  # noqa: E402
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
KILL_WAIT = 10.0                      # seconds Kill & retry waits for a killed process to be gone
ADOPT_CAP_S = 6 * 3600.0              # without psutil a pid cannot be proven to be the task's process: trust it for this long at most
DEFAULTS = {'layer_workers_present': 1, 'layer_workers_away': 4, 'build_stall_minutes': 30}
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
    """The worker limits and `build_stall_minutes` of `.codex_jobs/_app/settings.json`, read through `settings.load` (the only settings reader,
    DESIGN_PHASE4.md H: invalid values take the default)."""
    loaded = app_settings.load(root)
    return {key: loaded[key] for key in DEFAULTS}


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


def token_processes() -> dict[str, tuple[int, float | None]]:
    """{token: (pid, create time)} of every live process whose command line carries `--token <token>` (the wrappers; one process scan)."""
    out: dict[str, tuple[int, float | None]] = {}
    if psutil is None:
        return out
    for proc in psutil.process_iter(['pid', 'cmdline', 'create_time']):
        try:
            line = proc.info.get('cmdline') or []
            if '--token' in line and line.index('--token') + 1 < len(line):
                out[str(line[line.index('--token') + 1])] = (proc.info['pid'], proc.info.get('create_time'))
        except (psutil.Error, TypeError, ValueError):
            continue
    return out


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
        self._claims_scanned: object = 'never'          # the database identity whose emptiness was last answered by a claim scan (R5)
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
        for task in self.queue.tasks(state=taskqueue.ACTIVE, limit=100000):
            outcome = self._check_active(task, now)
            if outcome in ('alive', 'adopted', 'orphaned'):
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
        for task in self.queue.tasks(state=taskqueue.ACTIVE, limit=100000):
            finished += self._check_active(task, now) in ('finished', 'lost', 'killed', 'orphaned', 'kill_failed')
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

    def _builder_alive(self, task: dict, claim: dict | None, now: float) -> bool:
        """The attempt's builder (from the claim, else the row) is still the live recorded process."""
        claim = claim or {}
        pid = claim.get('builder_pid') or task.get('builder_pid')
        created = claim.get('builder_create') if claim.get('builder_pid') else task.get('builder_create')
        return bool(pid) and identity_ok(pid, created, task.get('started'), now)

    def _check_active(self, task: dict, now: float) -> str:
        """Settle one starting / running / orphan attempt: 'finished', 'lost', 'killed', 'kill_failed', 'adopted' (starting -> running),
        'orphaned' (running -> orphan: the wrapper died, its builder lives; the slot stays taken) or 'alive'."""
        result = self._read_result(task)
        if result is not None:
            self._finish(task, result)
            return 'finished'
        if task.get('kill_requested'):
            return self._kill(task)
        claim = self._claim(task)
        if task['state'] == 'starting':
            if claim and identity_ok(claim.get('wrapper_pid'), claim.get('wrapper_create'), task.get('started'), now):
                pid, created = claim['wrapper_pid'], claim.get('wrapper_create')
            else:
                found = find_token_process(task.get('token'))
                if found is None:
                    if self._builder_alive(task, claim, now) and self.queue.mark_orphan(task['id'], task.get('token')):
                        return 'orphaned'                      # the wrapper is gone, its builder is not (R6)
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
        if task['state'] != 'orphan' and same_process(task, now):
            return 'alive'
        result = self._read_result(task)                       # the wrapper may have written it just before it exited
        if result is not None:
            self._finish(task, result)
            return 'finished'
        if self._builder_alive(task, claim, now):              # re-check R6: the slot is held until the builder is gone too
            if task['state'] != 'orphan' and self.queue.mark_orphan(task['id'], task.get('token')):
                self.notes.append(f'task {task["id"]}: its wrapper died while the builder still runs (orphan; the worker slot stays taken)')
                return 'orphaned'
            return 'alive'
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
            ptr = builds.bare_pointer(self.root, str(args.get('race') or task.get('unit')), fresh=True)
        else:
            if not args.get('module_dir') or args.get('lod') not in decisions.LOD_LEVELS:
                return 'the task names no module / LOD'
            ptr = builds.pointer_at(Path(args['module_dir']), args['lod'], fresh=True)
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

    def _terminate(self, pid: int | None, created: float | None, marks: list[str]) -> str:
        """Stop one recorded process: 'gone' (not running, or its pid now belongs to another process), 'killed' (verified, killed and confirmed
        gone), 'unverified' (alive but its identity / command line could not be verified: not touched) or 'survived' (killed, still alive)."""
        if not pid or not pid_alive(pid):
            return 'gone'
        actual = create_time(pid)
        if created is not None and actual is not None and abs(actual - created) > CREATE_TIME_TOLERANCE:
            return 'gone'                                       # the pid was reused: the recorded process is gone
        if not self._verified(pid, created, marks):
            deadline = time.monotonic() + 3.0                   # (a process that is exiting right now cannot be inspected: wait for it)
            while time.monotonic() < deadline and pid_alive(pid):
                time.sleep(0.1)
            if not pid_alive(pid):
                return 'gone'
            if not self._verified(pid, created, marks):
                return 'unverified'
        try:
            proc = psutil.Process(pid)
            proc.kill()
            proc.wait(KILL_WAIT)
        except psutil.NoSuchProcess:
            return 'killed'
        except psutil.Error:
            pass
        if not pid_alive(pid):
            return 'killed'
        later = create_time(pid)
        return 'killed' if later is not None and created is not None and abs(later - created) > CREATE_TIME_TOLERANCE else 'survived'

    def _kill(self, task: dict) -> str:
        """Kill & retry: the builder first, then the wrapper, each by PID after its command line and create time were checked. Only when BOTH are
        confirmed gone is a fresh attempt queued (attempts reset: a manual retry; failed instead when the approval was withdrawn): 'killed'. A
        process that could not be verified or did not die leaves the row as it is, slot included, with the reason as its error ('kill_failed')."""
        claim = self._claim(task) or {}
        killed, problems = [], []
        argv = [a for a in (claim.get('argv') or [])[1:]]
        for role, pid, created, marks in (('builder', claim.get('builder_pid') or task.get('builder_pid'),
                                           claim.get('builder_create') if claim.get('builder_pid') else task.get('builder_create'), argv),
                                          ('wrapper', task.get('pid'), task.get('pid_create'), [task.get('token')] if task.get('token') else [str(RUN_TASK)])):
            outcome = self._terminate(pid, created, marks)
            if outcome == 'killed':
                killed.append(f'{role} {pid}')
            elif outcome != 'gone':
                problems.append(f'{role} {pid} {"could not be verified (not touched)" if outcome == "unverified" else "is still running after the kill"}')
        if problems:
            detail = 'Kill & retry could not stop the attempt: ' + '; '.join(problems) + ' (the worker slot stays taken)'
            self.queue.kill_failed(task['id'], task.get('token'), detail)
            self.notes.append(f'task {task["id"]}: {detail}')
            return 'kill_failed'
        detail = 'killed by the user (Kill & retry)' + (f': {", ".join(killed)}' if killed else ': no matching process was running')
        if self._approved(task):
            self.queue.requeue(task['id'], task.get('token'), detail, 0.0, reset=True)
        else:
            self.queue.finish(task['id'], task.get('token'), False, None, detail + '; the approval was withdrawn, not retried')
        self.notes.append(f'task {task["id"]}: {detail}')
        return 'killed'

    # ---------------------------------------------------------------------------------------------------------- reconcile
    def _db_identity(self) -> tuple | None:
        try:
            st = os.stat(self.queue.path)
        except OSError:
            return None
        return (st.st_ino, st.st_ctime_ns)

    def reconcile(self) -> dict:
        """Make the queue agree with the decisions (the queue is a derived cache, review F7 / F9). Idempotent. A missing, empty or repaired
        database first adopts the surviving attempts (re-check R5); each set is reconciled on its own (a failure is logged, R10)."""
        out = {'repaired': None, 'adopted': 0, 'recovered': [], 'queued': {}, 'cancelled': 0, 'revoked': [], 'quarantined': [], 'errors': []}
        health = self.queue.check()
        if health.get('corrupt'):
            out['repaired'] = self.repair_queue()
        else:
            ident = self._db_identity()
            if ident != self._claims_scanned and self.queue.count() == 0:
                out['adopted'] = self.adopt_claims()
                if out['adopted']:
                    note = f'the queue database was missing or empty: {out["adopted"]} running build(s) re-adopted from their claims'
                    self.notes.append(note)
                    self.queue.event('adopt', note)
                self._claims_scanned = self._db_identity() if out['adopted'] else ident
        out['recovered'] = actions.recover_transactions(self.root)
        derived = catalog.derive_all(self.root)
        for name, info in sorted(derived['sets'].items()):
            try:
                self._reconcile_set(name, info, out)
            except Exception as exc:  # noqa: BLE001  (one bad set never stops the others, re-check R10)
                text = f'reconcile {name}: {type(exc).__name__}: {exc}'
                out['errors'].append(text)
                self.errors = (self.errors + [text])[-20:]
        out['cancelled'] = self._cancel_withdrawn(derived)
        if out['queued'] or out['cancelled'] or out['revoked'] or out['quarantined'] or out['recovered'] or out['repaired'] or out['adopted']:
            catalog.invalidate()
        return out

    def _reconcile_set(self, name: str, info: dict, out: dict) -> None:
        if info.get('recovering'):
            return
        if info['decisions_error']:
            if info.get('ready') is not None:                   # an active manifest nobody can verify any more (R4)
                moved = self._revoke(name, quarantine_reason='the set decision file is unreadable')
                if moved:
                    out['quarantined'].append(moved)
            return
        if info['locked'] and info['stage'] in ('3d', 'aligned', 'completed') and self._needs_work(info):
            with decisions.set_lock(self.root, name):
                catalog.invalidate()
                fresh = catalog.derive_one(self.root, name)
                if fresh and fresh['locked'] and not fresh['decisions_error']:
                    done = actions.ensure_set_tasks(self.root, fresh)
                    if any(v in ('queued', 'retried') for v in done.values()):
                        out['queued'][name] = done
        if info.get('ready') is not None and info['stage'] != 'completed':
            moved = self._revoke(name)
            if moved:
                out['quarantined' if '.quarantine-' in moved else 'revoked'].append(moved)

    def _needs_work(self, info: dict) -> bool:
        if any(rec['status'] in ('none', builds.DAMAGED) for rec in info['builds'].values()):
            return True
        return any(not builds.bare_pointer(self.root, race) and (info.get('bare_builds') or {}).get(race, {}).get('status') != builds.CANCELLED
                   for race in info['races'])

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
        gen = decisions.approval_gen(info['decisions']['lod_approval'])
        return isinstance(entry, dict) and entry.get('job') == args.get('job') and entry.get('lod') == args.get('lod') and \
            bool(entry.get('glb_sha256')) and entry.get('glb_sha256') == args.get('glb_sha256') and task.get('approval_gen') in (None, gen)

    UNVERIFIABLE = ('ready.json is missing or belongs to another approval', 'ready.json is not the manifest this approval wrote')

    def _revoke(self, name: str, quarantine_reason: str | None = None) -> str | None:
        """Take models/<set>/ready.json out of service when (rechecked under the set lock) its approval no longer holds: REVOKED (renamed
        `ready.json.revoked-<time>`) when the approval is simply gone or its builds changed; QUARANTINED (`ready.json.quarantine-<time>`, re-check
        R4) when it cannot be verified: the decisions are unreadable, or the approval exists but the manifest's bytes / approval id are not the
        ones it wrote. Never deleted; queue event `revoke` / `quarantine` and an actions.jsonl entry."""
        with decisions.set_lock(self.root, name):
            catalog.invalidate()
            fresh = catalog.derive_one(self.root, name)
            if not fresh or fresh.get('ready') is None or fresh.get('recovering'):
                return None
            if fresh['decisions_error']:
                kind, reason = 'quarantine', quarantine_reason or f'the set decision file is unreadable: {fresh["decisions_error"]}'
            elif fresh['stage'] == 'completed':
                return None
            else:
                problems = fresh.get('not_completed') or [f'the set is in {fresh["stage"]}']
                unverifiable = bool(fresh['decisions']['aligned_approval']) and any(p.startswith(self.UNVERIFIABLE) for p in problems)
                kind, reason = ('quarantine' if unverifiable else 'revoked'), '; '.join(problems)
            reason = reason[:300]
            moved = decisions.revoke_ready(self.root, name, reason, kind)
        if moved:
            event = 'quarantine' if kind == 'quarantine' else 'revoke'
            self.queue.event(event, f'{name}: {moved}: {reason}')
            serve.log_action(self.root, f'{event}_ready', f'models/{name}/ready.json', f'models/{name}/{moved}', reason[:200])
            self.notes.append(f'{"quarantined" if kind == "quarantine" else "revoked"} {name}/ready.json: {reason}')
        return moved

    def repair_queue(self) -> dict:
        """The database is damaged: keep it as `.corrupt-<time>`, start a fresh one, re-adopt every SURVIVING attempt (adopt_claims, so nothing is
        started twice); the rest of the work is queued again from the decisions by reconcile."""
        kept = self.queue.repair()
        adopted = self.adopt_claims()
        self._claims_scanned = self._db_identity()
        note = f'queue database was damaged: kept as {kept.name if kept else "-"}, rebuilt; {adopted} running build(s) re-adopted from their claims'
        self.notes.append(note)
        self.queue.event('repair', note)
        return {'kept': str(kept) if kept else None, 'adopted': adopted}

    def adopt_claims(self) -> int:
        """Re-insert every attempt that is still alive into a lost / empty / rebuilt queue (re-check R5): each attempt's task file without a
        result; alive = its claim's wrapper is the recorded live process, or (no usable claim yet) a live process carries its token on the command
        line. A claim whose wrapper died while its builder still runs is adopted as `orphan` (its slot stays taken, R6). Returns how many."""
        if not self.logs.is_dir():
            return 0
        now, adopted, tokens = time.time(), 0, None
        for spec_file in sorted(self.logs.glob('*.task.json')):
            base = str(spec_file)[:-len('.task.json')]
            if Path(base + '.result.json').exists():
                continue
            claim = _json_file(Path(base + '.claim.json'))
            spec = _json_file(spec_file) or {}
            source = claim if claim and claim.get('token') and isinstance(claim.get('task'), dict) else None
            orphan = False
            if source is not None and identity_ok(source.get('wrapper_pid'), source.get('wrapper_create'), source.get('claimed'), now):
                pid, created, started = source['wrapper_pid'], source.get('wrapper_create'), source.get('claimed')
            elif source is not None and source.get('builder_pid') and \
                    identity_ok(source['builder_pid'], source.get('builder_create'), source.get('claimed'), now):
                pid, created, started, orphan = source.get('wrapper_pid'), source.get('wrapper_create'), source.get('claimed'), True
            else:
                if not spec.get('token') or not isinstance(spec.get('task'), dict):
                    continue
                tokens = token_processes() if tokens is None else tokens
                found = tokens.get(spec['token'])
                if not found:
                    continue
                source = spec | {'log': spec.get('log')}
                (pid, created), started = found, spec_file.stat().st_mtime
            task = source['task']
            try:
                new_id = self.queue.insert_running(task.get('lane'), task.get('key'), task.get('set'), task.get('unit'), task.get('args') or {},
                                                   int(source.get('attempt') or 1), source['token'], source.get('log'), pid, created, started,
                                                   int(task.get('max_attempts') or 3), approval_gen=task.get('approval_gen'))
            except (taskqueue.QueueError, ValueError, TypeError):
                continue
            if new_id is None:
                continue
            adopted += 1
            if source.get('builder_pid'):
                self.queue.set_builder(new_id, source['token'], source['builder_pid'], source.get('builder_create'))
            if orphan:
                self.queue.mark_orphan(new_id, source['token'])
        return adopted

    # ------------------------------------------------------------------------------------------------------------ starting
    def default_argv(self, task: dict) -> list[str]:
        """The builder command. The output folder is ALWAYS explicit (re-check R13): bare builds go to `<root>/tools/preview/cache/layers/<race>`
        of THIS project root (never the builder's default, the real repository's cache), module builds to `<module_dir>/layers`."""
        args = task.get('args') or {}
        if task['lane'] == 'bare':
            race = str(args['race'])
            return [sys.executable, str(BUILD_PY), 'bare', race, '--out', str(builds.bare_root(self.root) / race)]
        argv = [sys.executable, str(BUILD_PY), 'module', str(args['module_dir']), '--lod', str(args['lod']), '--out', str(Path(args['module_dir']) / 'layers')]
        if isinstance(args.get('glb_sha256'), str):
            argv += ['--expect-source-sha256', args['glb_sha256']]
        return argv

    def _bare_current(self, race: str) -> bool:
        ptr = builds.bare_pointer(self.root, race)
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
                             'args': task.get('args') or {}, 'max_attempts': task.get('max_attempts'), 'approval_gen': task.get('approval_gen')}}
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
        races |= set(info['races'])                       # every live race's bare build, also of a set none of whose units is built yet (R14)
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
        ptr = builds.bare_pointer(root, race)
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


# ------------------------------------------------------------------------------------------------------ services (phase 4)

SERVICE_INTERVAL = 30.0          # seconds between two services.tick passes (the first psutil scan takes ~6 s: never part of the 3 s loop)
SERVICE_ACTIONS = ('start', 'stop', 'restart')
EVENT_KEEP = 100


class ServiceManager:
    """The services thread of the writer server, only with `--manage-services` (DESIGN_PHASE4.md A and H). Every SERVICE_INTERVAL it runs
    `services.tick(root, settings.load(root))` (adopt, crash restart, start missing enabled services) and keeps the last snapshot. Start / Stop /
    Restart requests from the HTTP threads are queued and run HERE, never on an HTTP thread (`services.stop` waits): `request()` answers at once
    with `{ok, pending: true, request}` and the outcome appears in `events` (and the queue's event log, kind `service`)."""

    def __init__(self, root: Path, *, interval: float = SERVICE_INTERVAL, tick=None, settings_loader=None) -> None:
        import services                                    # psutil is needed only when the services are managed
        self.services = services
        self.root = Path(root)
        self.interval = interval
        self._tick = tick or services.tick
        self._settings = settings_loader or app_settings.load
        self.cond = threading.Condition()
        self.requests: list[dict] = []                    # waiting requests, oldest first
        self.running: dict | None = None                  # the request being executed
        self.events: list[dict] = []
        self.statuses: dict | None = None                 # {name: services.status_one(...)} after the last tick / request
        self.last_tick: float | None = None
        self.ticks = 0
        self.errors: list[str] = []
        self._next_id = 1
        self._next_tick = 0.0
        self._wake = False
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    # ------------------------------------------------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name='services', daemon=True)
        self.thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        """Stop the thread (a request being executed finishes first). The services themselves are never stopped by this."""
        self.stop_event.set()
        with self.cond:
            self.cond.notify_all()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout)

    def wake(self) -> None:
        """Run a tick now (after a settings change)."""
        with self.cond:
            self._wake = True
            self.cond.notify_all()

    # -------------------------------------------------------------------------------------------------------- requests
    def request(self, name: object, action: object) -> dict:
        """Queue a Start / Stop / Restart of `name`. A second request for a service that has one waiting or running is refused (409 busy)."""
        if name not in self.services.SERVICES:
            raise ValueError(f'name must be one of {list(self.services.SERVICES)}')
        if action not in SERVICE_ACTIONS:
            raise ValueError(f'action must be one of {list(SERVICE_ACTIONS)}')
        with self.cond:
            busy = [r for r in self.requests + ([self.running] if self.running else []) if r['name'] == name]
            if busy:
                raise guard.Busy(f'{name}: a {busy[0]["action"]} request is already in progress')
            req = {'id': self._next_id, 'name': name, 'action': action, 'time': time.time()}
            self._next_id += 1
            self.requests.append(req)
            self.cond.notify_all()
        return {'ok': True, 'pending': True, 'request': req['id'], 'name': name, 'action': action}

    def _event(self, kind: str, text: str, **extra) -> None:
        self.events = (self.events + [{'time': time.time(), 'kind': kind, 'text': text} | extra])[-EVENT_KEEP:]
        try:
            taskqueue.for_root(self.root).event('service', text[:500])
        except Exception:  # noqa: BLE001
            pass

    def _execute(self, req: dict) -> None:
        name, action = req['name'], req['action']
        settings = self._settings(self.root)
        try:
            if action == 'start':
                self.services.start(self.root, name, settings)
                outcome = {'ok': True, 'pending': False, 'message': f'{name} started'}
            elif action == 'stop':
                outcome = self.services.stop(self.root, name, settings=settings)
            else:
                outcome = self.services.restart(self.root, name, settings=settings)
            ok, pending, message = True, bool(outcome.get('pending')), str(outcome.get('message') or '')
        except self.services.ServiceError as exc:
            ok, pending, message = False, False, str(exc)
        except Exception as exc:  # noqa: BLE001
            ok, pending, message = False, False, f'{type(exc).__name__}: {exc}'
        self._refresh(settings)              # first: whoever sees the outcome event also sees the status it produced
        self._event('request', f'{action} {name}: {message}', request=req['id'], name=name, action=action, ok=ok, pending=pending)

    def _refresh(self, settings: dict | None = None) -> None:
        try:
            self.statuses = self.services.status(self.root, settings or self._settings(self.root), lines=0)['services']
        except Exception as exc:  # noqa: BLE001
            self.errors = (self.errors + [f'status: {type(exc).__name__}: {exc}'])[-20:]

    # ------------------------------------------------------------------------------------------------------------ loop
    def tick(self) -> dict:
        """One manager pass (tests call it directly)."""
        result = self._tick(self.root, self._settings(self.root))
        self.last_tick = time.time()
        self.ticks += 1
        self.statuses = result.get('services')
        for text in result.get('events') or []:
            self._event('tick', str(text))
        return result

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            with self.cond:
                req = self.requests.pop(0) if self.requests else None
                self.running = req
            if req is not None:
                try:
                    self._execute(req)
                finally:
                    with self.cond:
                        self.running = None
                continue
            if self._wake or time.monotonic() >= self._next_tick:
                self._wake = False
                try:
                    self.tick()
                except Exception as exc:  # noqa: BLE001
                    self.errors = (self.errors + [f'tick: {type(exc).__name__}: {exc}'])[-20:]
                    sys.stderr.write(traceback.format_exc())
                self._next_tick = time.monotonic() + self.interval
            with self.cond:
                if not self.requests and not self._wake and not self.stop_event.is_set():
                    self.cond.wait(max(0.05, self._next_tick - time.monotonic()))

    # ------------------------------------------------------------------------------------------------------------ views
    def problems(self) -> list[str]:
        """The header's services problems from the last snapshot ([] before the first tick)."""
        import progress
        return progress.problems(self.statuses or {}) or []

    def view(self, lines: int = 20) -> dict:
        """`services` of GET /api/v2/queue: `services.status` (fresh from the records, with log tails) plus the manager's own state."""
        try:
            status = self.services.status(self.root, self._settings(self.root), lines=lines)
        except Exception as exc:  # noqa: BLE001
            status = {'services': self.statuses or {}, 'convert_jobs': None, 'errors': [f'status: {type(exc).__name__}: {exc}']}
        with self.cond:
            pending = [dict(r) for r in self.requests] + ([dict(self.running) | {'running': True}] if self.running else [])
        return status | {'managed': True, 'scanned': self.last_tick is not None, 'last_tick': self.last_tick, 'interval': self.interval,
                         'pending': pending, 'events': list(self.events[-30:]), 'manager_errors': list(self.errors[-5:]),
                         'problems': self.problems()}


class TripoCheck:
    """The Queue tab's "Check balance" (DESIGN_PHASE4.md E.2 / H): `credits.check_tripo` on a worker thread. Refused (409 busy) while a check
    runs or a `convert_job.py` of this checkout runs (it uses the same browser profile); the result lands in credits.json and `last`."""

    def __init__(self, root: Path, run=None, busy=None) -> None:
        self.root = Path(root)
        self.run = run                                # injectable: run(root) -> (returncode, stdout, stderr); tests never start node
        self.busy = busy                              # injectable: () -> [running convert_job instances]
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.last: dict | None = None
        self.started: float | None = None
        self.finished: float | None = None

    def _users(self) -> list[dict]:
        if self.busy is not None:
            return list(self.busy())
        import services
        return services.convert_job_running(self.root)

    def request(self) -> dict:
        """{ok: true, status: "running", pending: true} once the check started; {ok: false, status: "busy", message} (HTTP 200, the
        credits.check_tripo shape) while a check runs or a convert_job.py uses the browser profile. The result goes to `view()["last"]`."""
        with self.lock:
            if self.thread and self.thread.is_alive():
                return {'ok': False, 'status': 'busy', 'message': 'a balance check is already running'}
            users = self._users()
            if users:
                return {'ok': False, 'status': 'busy',
                        'message': f'convert_job.py is running (pid {users[0].get("pid")}) and uses the Tripo browser profile; check again after it'}
            self.started, self.finished = time.time(), None
            self.thread = threading.Thread(target=self._work, name='tripo-check', daemon=True)
            self.thread.start()
        return {'ok': True, 'status': 'running', 'pending': True}

    def _work(self) -> None:
        import credits
        try:
            self.last = credits.check_tripo(self.root, run=self.run)
        except Exception as exc:  # noqa: BLE001
            self.last = {'status': 'error', 'message': f'{type(exc).__name__}: {exc}'}
        self.finished = time.time()
        try:
            taskqueue.for_root(self.root).event('op', f'check_tripo: {self.last.get("status")} {self.last.get("credits", self.last.get("message", ""))}'[:500])
        except Exception:  # noqa: BLE001
            pass

    def view(self) -> dict:
        return {'running': bool(self.thread and self.thread.is_alive()), 'started': self.started, 'finished': self.finished, 'last': self.last}


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
            'kill_requested': bool(task.get('kill_requested')), 'orphan': task['state'] == 'orphan',
            'token': task.get('token') if task['state'] in taskqueue.ACTIVE else None}


def queue_state(root: Path, active: bool, now: float | None = None) -> dict:
    """GET /api/v2/queue. The rows come in three groups (re-check R16): EVERY active attempt (starting / running / stalled / orphan) first, never
    truncated, newest first; then queued tasks (newest first, at most OPEN_PAGE); then failed / done / cancelled ones of the last 24 h (newest first,
    at most RECENT_PAGE). `totals` (per state) and `workers.running` are SQL counts over the whole table, independent of the page; `page` says how
    much of each group is shown (review F16)."""
    now = time.time() if now is None else now
    queue = taskqueue.for_root(root)
    since = now - 86400
    finished_states = ('failed', 'done', 'cancelled')
    active_rows = queue.tasks(state=taskqueue.ACTIVE, limit=1_000_000)
    queued_rows = queue.tasks(state='queued', limit=OPEN_PAGE)
    open_rows = active_rows + queued_rows
    recent = queue.tasks(state=finished_states, since=since, limit=RECENT_PAGE)
    totals = queue.totals()
    open_total = sum(totals.get(s, 0) for s in taskqueue.OPEN)
    recent_total = queue.count(finished_states, since)
    tasks = sorted(active_rows, key=lambda t: -t['id']) + sorted(queued_rows, key=lambda t: -t['id']) + sorted(recent, key=lambda t: -t['id'])
    pool = pool_status(root)
    stall = read_settings(root)['build_stall_minutes']
    views = [task_view(t, now, stall) for t in tasks]
    return {'tasks': views,
            'workers': {'limit': pool['limit'], 'running': sum(totals.get(s, 0) for s in taskqueue.ACTIVE), 'present': pool['present'],
                        'mode': pool['mode'], 'idle': pool['idle'], 'orchestrator': bool(active)},
            'totals': totals, 'stalled': sum(v['stalled'] for v in views), 'stall_minutes': stall,
            'page': {'open_limit': OPEN_PAGE, 'open_total': open_total, 'open_shown': len(open_rows), 'recent_limit': RECENT_PAGE,
                     'recent_total': recent_total, 'recent_shown': len(recent), 'recent_since': since,
                     'active_total': sum(totals.get(s, 0) for s in taskqueue.ACTIVE), 'active_shown': len(active_rows),
                     'queued_total': totals.get('queued', 0), 'queued_shown': len(queued_rows),
                     'truncated': open_total > len(open_rows) or recent_total > len(recent)},
            'queue_health': queue.health_view()}
