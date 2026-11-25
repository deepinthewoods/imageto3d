"""Service manager of the v2 preview server: adopts and manages the three pipeline daemons (DESIGN_PHASE4.md sections A and I).

    supervisor  python -X utf8 -u tools/pipeline_supervisor.py run
    views       python -u tools/view_batch.py run --parallel <settings.view_parallel>
    convert     python -X utf8 -u tools/convert_queue.py --sets-file .codex_jobs/_batch/convert_sets.txt --worker 1 [--no-tripo]

API (every function takes `root`, so tests use a temp root with fake service scripts under `<root>/tools/`):
    begin_management(root)                 the services thread's first step: writes the conversion hold (I.1) before anything is started / adopted
    tick(root, settings=None, now=None) -> {"services": {name: status}, "events": [...]}   called every 30 s by the orchestrator thread
    status(root, settings=None, now=None, lines=20, deep=False) -> {"services": {...}, "convert_jobs": [...], "errors": [...], "hold", "settings"}
    start(root, name, settings=None) / stop(root, name, ...) / restart(root, name, ...)    the Queue tab buttons
    convert_job_running(root) / studio_running(root)                                      for credits.check_tripo and Stop gating

Discovery (phase 4 review F5) parses every python command line the way the interpreter and the daemon's own argparse would: the interpreter's
options are skipped (`-c` / `-m` / stdin = not a script run), the script is the first operand and must resolve (against the process cwd) to
EXACTLY `<root>/tools/<script>` of this checkout, the sub-command must be the daemon's (`run`; `run --dry-run` is a one-shot pass, not the
daemon), the effective root must be this root (`pipeline_supervisor.py --root X`) and the convert worker must be worker 1 (`--worker N`,
`--worker=N` and argparse prefixes, last one wins, default 1). There is no bounded token scan. A process that names the service script and
sub-command but whose script path cannot be resolved (its cwd is unreadable) is UNVERIFIED: never adopted or signalled, and while one exists
no second instance is started. The supervisor's `supervisor.lock` and the view runner's `view_runner.lock` (I.5) are probed before a start too.
Before any signal the process is re-opened as ONE psutil.Process handle whose create time and command line are checked again; the signal goes
to that handle (psutil refuses a reused pid).

A match is ADOPTED (recorded in `.codex_jobs/_app/services/<name>.json` as `{pid, create_time, cmdline, adopted, since, ...}`); a service is
started only when enabled in the settings, nothing matches and the settings file is usable (settings.health). Two matches = state
`duplicate`, an error in the UI, never fixed automatically. Services start detached (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP |
CREATE_NO_WINDOW) with stdout/stderr appended to `.codex_jobs/_batch/<name>_win.out.log/.err.log`, so the web server never stops a service.
Start order: supervisor, views, then the convert worker only once the supervisor completed a tick (`supervisor_tick.json`).

Conversion hold (DESIGN_PHASE4.md I.1, `.codex_jobs/_batch/CONVERT_HOLD`, {since, since_unix, reason, owner_pid, owner_create_time}): written
by begin_management, before the supervisor is stopped or restarted (naming that supervisor in `retiring`), and whenever no supervisor runs;
removed by the tick only when it is ours, the running supervisor wrote `supervisor_tick.json` (its pid, a time after the hold's since) AND
that supervisor is the intended generation: not a retiring one, no stop of it pending, no legacy worker drain in progress (release_blocker;
DESIGN_PHASE4.md I.9). A hold of a dead owner older than STALE_HOLD_S is replaced by ours; a hold of a live other owner is never touched.

Convert worker stop (I.2): `.codex_jobs/_batch/convert_worker1.stop` = {pid, create_time, request, time} naming the worker process; the worker
acknowledges with `convert_worker1.stopping`, finishes its job (finish + offload) and exits. The manager waits and NEVER kills on a timeout:
the stop stays pending and every tick re-evaluates it. A LEGACY worker (it started before the current convert_queue.py was written, or that
script has no stop support) is drained under the conversion hold (written first), and stopped only through frozen_stop: once no
convert_job.py of this checkout runs and its last event in convert_events.log (since it started) is PAUSED / IDLE / LOW DISK after its last
CONVERT, it is suspended, re-verified idle while frozen and only then terminated. An older view runner (no launch reservation) is stopped the
same way, only while it launches nothing (I.9). A pending stop that ends in a tick is reported in the tick's `completed` with its request id.

Legacy worker drain, second half (DESIGN_PHASE4.md I.10; pre-cut-over review finding 1): stopping intake (the hold) is separate from draining
a job. After the hold the manager writes `.codex_jobs/_batch/CONVERT_DRAIN` = {since, since_unix, reason, owner_pid, worker: {pid,
create_time}}: a convert_job.py that is BLOCKED at its paid gate (it keeps `gate_wait/<pid>.json`; nothing paid in flight) releases its module
lock, marks the module `deferred` and exits 76, recording that in `drain_exits/`; the current convert_queue.py takes no job while the file
exists and never counts exit 76. The drain then waits until no convert_job.py runs and the worker sleeps (frozen_stop as before). An older
worker that keeps taking jobs (it does not know the hold, or counts exit 76 as a failure and tries the next job) is frozen and terminated
while it has NO child and its last event is one after which it only picks the next job (DRAIN_SAFE_EVENTS: never CONVERTED / FINISH /
OFFLOAD / an exit announcement, after which it moves sets). When the drain ends (settle_drain) the failure counts an older worker wrote for
those exits are restored (`<job>.fails` back to max(the lowest count before, now - exits); logged in `convert_drain.log` and the convert
record's `drain_restored`), the job locks naming the terminated worker are removed and CONVERT_DRAIN is deleted. PAUSE is never touched.

Suspension record (phase-4 row 6 residual): frozen_stop writes `.codex_jobs/_app/services/suspensions.json` = {"items": [{pid, create_time,
service, time, terminated}]} BEFORE it suspends a process and removes the entry once the process is resumed or gone. begin_management
(recover_suspensions) resumes every recorded process that still runs as this checkout's service and is still suspended (a manager that
crashed in between), and drops the entries of processes that are gone.

Supervisor stop delivery (finding 7): the supervisor record carries `stop_delivery` = {state: requested -> delivered | failed, ...}. A stop
command that fails, or a pending stop whose `supervisor.stop` file is gone while the same supervisor still runs (after STOP_DELIVERY_GRACE),
is NON-DELIVERY: the stop request is withdrawn, the supervisor is taken out of the hold's `retiring` generations (the hold keeps its
`since`), and the healthy supervisor's next tick releases the hold again.

Restart outcome (finding 20): a restart's `completed` entry (and the stop() result) is terminal only once the replacement is identified
(`replacement` = {pid, create_time}, ok true) or the start failed / is blocked (ok false with the reason).

Crash restart: an enabled service that vanished (without a stop request) is restarted after 60 s, after a second quick failure after 5 min,
after a third it is HELD ("restart failed 3 times") until Start is pressed. A run that stays up for STABLE_AFTER resets the count. The convert
worker's own idle exit (its log says `exiting`) is not a failure: it is restarted when convert_list.eligible_jobs finds work it would take.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import convert_list  # noqa: E402
import guard  # noqa: E402
import modes  # noqa: E402
import settings as app_settings  # noqa: E402

import psutil  # noqa: E402   (required: the manager cannot see processes without it)

DETACHED = (getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
            | getattr(subprocess, 'CREATE_NO_WINDOW', 0))
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
BACKOFF = (60.0, 300.0)       # wait before the 1st / 2nd restart; the 3rd failure holds the service
MAX_FAILS = 3
STABLE_AFTER = 600.0          # a run this long clears the failure count and the error
CREATE_TIME_TOLERANCE = 1.5
LOG_LINES = 20
HOLD_FILE = 'CONVERT_HOLD'
TICK_FILE = 'supervisor_tick.json'
STALE_HOLD_S = 600.0          # a hold whose owner died this long ago may be replaced by a new manager's own hold (I.1)
SLEEP_EVENTS = ('PAUSED', 'IDLE', 'LOW DISK')
MANAGED_WORKER = 1
DRAIN_FILE = 'CONVERT_DRAIN'  # I.10: gate-waiting convert_job.py processes leave (exit DRAIN_EXIT) while it exists; written after the hold
DRAIN_EXIT = 76               # = tools/tripo_pipeline/convert_job.py DRAIN_EXIT
DRAIN_SETTLE = 20.0           # how long a frozen cycling worker's leaving child (at the gate, or just started) is waited for
DRAIN_YOUNG = 10.0            # a child this young has not reached its first gate check yet
# events after which a worker only releases the job and picks the next one (never CONVERTED / FINISH / OFFLOAD / STOP / "exiting": it moves sets)
DRAIN_SAFE_EVENTS = SLEEP_EVENTS + ('CONVERT ', 'FAILED', 'DEFERRED', 'SHARED', 'HOLD', 'BALANCE', 'PARTIAL', 'OWNER')
STOP_DELIVERY_GRACE = 15.0    # a pending supervisor stop whose stop file is gone this long while the supervisor still runs was not delivered
_LOCK = threading.RLock()


class ServiceError(Exception):
    """A request the manager refuses (unknown service, already running, duplicate instances, convert_job running, ...)."""


@dataclass(frozen=True)
class Service:
    name: str
    script: str                       # relative to the repo root, forward slashes
    subcommand: str | None = None     # the daemon's sub-command (`run`); None = the script has none
    interp: str = 'python'            # the first word of the process name
    log_stems: tuple = ()             # <stem>.out.log / <stem>.err.log under _batch; the first is written by the manager, the rest only displayed
    events_log: str = 'events.log'
    lock_file: str | None = None      # the daemon's single-instance lock under _batch (probed before a start)

    @property
    def script_name(self) -> str:
        return self.script.rsplit('/', 1)[-1]

    def command(self, settings: dict) -> list[str]:
        if self.name == 'supervisor':
            return [sys.executable, '-X', 'utf8', '-u', self.script, 'run']
        if self.name == 'views':
            return [sys.executable, '-u', self.script, 'run', '--parallel', str(settings['view_parallel'])]
        if self.name == 'convert':
            cmd = [sys.executable, '-X', 'utf8', '-u', self.script, '--sets-file', '.codex_jobs/_batch/convert_sets.txt',
                   '--worker', str(MANAGED_WORKER)]
            return cmd + (['--no-tripo'] if settings['tripo'] == 'off' else [])
        raise ServiceError(f'{self.name} cannot be started')


SERVICES = {
    'supervisor': Service('supervisor', 'tools/pipeline_supervisor.py', 'run', log_stems=('supervisor_win',), lock_file='supervisor.lock'),
    'views': Service('views', 'tools/view_batch.py', 'run', log_stems=('views_win', 'view_batch_win8', 'view_batch_win'),
                     lock_file='view_runner.lock'),
    'convert': Service('convert', 'tools/convert_queue.py', None, log_stems=('convert_win', 'convert_worker1_win', 'convert_worker_w1_win'),
                       events_log='convert_events.log'),
}
PROBES = {   # processes that are looked for but never started or stopped here
    'convert_job': Service('convert_job', 'tools/tripo_pipeline/convert_job.py'),
    'studio': Service('studio', 'tools/tripo_pipeline/studio.mjs', interp='node'),
}


# ------------------------------------------------------------------------------------------------------------------------ paths

def batch(root) -> Path:
    return Path(root) / '.codex_jobs' / '_batch'


def records_dir(root) -> Path:
    return Path(root) / guard.APP / 'services'


def record_path(root, name: str) -> Path:
    return records_dir(root) / f'{name}.json'


def read_record(root, name: str) -> dict:
    try:
        rec = json.loads(record_path(root, name).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {'name': name}
    return rec if isinstance(rec, dict) else {'name': name}


def write_record(root, name: str, rec: dict) -> None:
    rec['name'] = name
    guard.write_atomic(record_path(root, name), (json.dumps(rec, indent=2) + '\n').encode('utf-8'))


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).isoformat(timespec='seconds') if ts else None


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None


def tail_lines(path: Path, n: int = LOG_LINES, max_bytes: int = 65536) -> list[str]:
    if n <= 0:
        return []
    try:
        with open(path, 'rb') as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return []
    return data.decode('utf-8', errors='replace').splitlines()[-n:]


def log_files(root, svc: Service) -> dict:
    """{"out": path, "err": path} to display: the newest existing of the stems (the first stem when none exists)."""
    out = {}
    for kind in ('out', 'err'):
        files = [batch(root) / f'{stem}.{kind}.log' for stem in svc.log_stems]
        existing = [f for f in files if f.exists()]
        out[kind] = max(existing, key=lambda f: f.stat().st_mtime) if existing else files[0]
    return out


def _my_create_time() -> float | None:
    try:
        return psutil.Process(os.getpid()).create_time()
    except psutil.Error:
        return None


# ------------------------------------------------------------------------------------------------------------------- command lines

def _real(path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


PY_ARG_OPTIONS = ('-X', '-W', '--check-hash-based-pycs')
NODE_ARG_OPTIONS = ('-r', '--require', '--import', '--loader', '--experimental-loader', '--inspect-port', '--title')


def python_script(cmd: list[str]) -> tuple[str | None, list[str]]:
    """(script operand, its arguments) of a python command line, parsed like the interpreter: options (`-u`, `-X utf8`, `-Xutf8`, `-W x`,
    combined `-uB`, ...) are skipped; `-c` / `-m` / `-` (stdin) / `--help`-like long options mean no script is run: (None, [])."""
    i, n = 1, len(cmd)
    while i < n:
        arg = cmd[i]
        if arg == '--':
            i += 1
            break
        if not arg.startswith('-') or arg == '-':
            break
        if arg.startswith('--'):
            if arg in PY_ARG_OPTIONS:
                i += 2
                continue
            if arg.startswith('--check-hash-based-pycs='):
                i += 1
                continue
            return None, []
        body, skip_next = arg[1:], False
        for j, ch in enumerate(body):
            if ch in 'cm':
                return None, []
            if ch in 'XW':
                skip_next = j == len(body) - 1
                break
        i += 2 if skip_next else 1
    if i >= n or cmd[i] == '-':
        return None, []
    return cmd[i], cmd[i + 1:]


def _shared_parser():
    """tools/proc_cmdline.py `python_script`, the parser the supervisor uses (phase 5 review row 7), loaded by path so both sides parse a
    command line the same way; None when it is missing (the copy above is used)."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location('_imageto3d_proc_cmdline', HERE.parents[1] / 'proc_cmdline.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.python_script
    except (OSError, ImportError, AttributeError, SyntaxError):
        return None


_local_python_script = python_script
python_script = _shared_parser() or _local_python_script


def node_script(cmd: list[str]) -> tuple[str | None, list[str]]:
    """(script, its arguments) of a node command line (`-e` / `-p` / `--eval` / `--print` = no script)."""
    i, n = 1, len(cmd)
    while i < n:
        arg = cmd[i]
        if arg == '--':
            i += 1
            break
        if not arg.startswith('-'):
            break
        if arg in ('-e', '-p', '--eval', '--print', '-pe') or arg.startswith(('--eval=', '--print=')):
            return None, []
        i += 2 if arg in NODE_ARG_OPTIONS else 1
    if i >= n:
        return None, []
    return cmd[i], cmd[i + 1:]


def _option_value(args: list[str], name: str, minimum: int = 3) -> tuple[bool, str | None]:
    """(present, value) of the LAST `--name V` / `--name=V` (or an argparse prefix of `--name` at least `minimum` characters long)."""
    found, value = False, None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith('--') and len(arg) >= minimum:
            key, eq, val = arg.partition('=')
            if name.startswith(key) and len(key) >= minimum:
                found = True
                if eq:
                    value = val
                elif i + 1 < len(args):
                    value = args[i + 1]
                    i += 1
                else:
                    value = None
        i += 1
    return found, value


def _first_positional(args: list[str], options_with_values: tuple[str, ...] = ()) -> tuple[int | None, str | None]:
    """(index, value) of the first argument that is not an option (or an option's value)."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith('-'):
            key = arg.partition('=')[0]
            takes = any(o.startswith(key) and len(key) >= 3 for o in options_with_values) and '=' not in arg
            i += 2 if takes else 1
            continue
        return i, arg
    return None, None


def identify(root, svc: Service, cmd: list[str], cwd: str | None) -> str | None:
    """'match' (this checkout's daemon `svc`), 'unverified' (its script name and sub-command, but the script path cannot be resolved: the cwd
    is unknown) or None. The rules are in the module docstring (phase 4 review F5)."""
    if not cmd or not Path(cmd[0].replace('\\', '/')).name.lower().startswith(svc.interp):
        return None
    script, args = (node_script if svc.interp == 'node' else python_script)(cmd)
    if not script or script.replace('\\', '/').rsplit('/', 1)[-1] != svc.script_name:
        return None
    if svc.name == 'supervisor':
        index, sub = _first_positional(args, ('--root',))
        if sub != 'run' or _option_value(args[index + 1:], '--dry-run')[0] or any(a in ('-h', '--help') for a in args):
            return None
        has_root, value = _option_value(args[:index], '--root')
    elif svc.subcommand:
        index, sub = _first_positional(args)
        if sub != svc.subcommand or any(a in ('-h', '--help') for a in args):
            return None
        has_root, value = False, None
    else:
        has_root, value = False, None
    if svc.name == 'convert':
        present, worker = _option_value(args, '--worker')
        try:
            number = int(worker) if present else MANAGED_WORKER
        except (TypeError, ValueError):
            return None
        if number != MANAGED_WORKER:          # the app manages worker 1 only; worker N > 1 are the user's own
            return None
    path = Path(script)
    if not path.is_absolute():
        if not cwd:
            return 'unverified'
        path = Path(cwd) / path
    if _real(path) != _real(Path(root) / svc.script):
        return None
    if has_root:
        if value is None:
            return None
        effective = Path(value)
        if not effective.is_absolute():
            if not cwd:
                return 'unverified'
            effective = Path(cwd) / effective
        if _real(effective) != _real(root):
            return None
    return 'match'


def _match(root, svc: Service, cmd: list[str], cwd: str | None) -> bool:
    return identify(root, svc, cmd, cwd) == 'match'


# ------------------------------------------------------------------------------------------------------------------------ discovery

def scan_full(root, names=None) -> tuple[dict, dict]:
    """({name: [instance]}, {name: [unverified instance]}) for the services (default all three) and probes named; an instance is {pid, ppid,
    create_time, cmdline, cwd}. A child that repeats its parent's command line (a launcher shim) is dropped in favour of the child."""
    table = {**SERVICES, **PROBES}
    wanted = [table[n] for n in (names or SERVICES)]
    found: dict[str, list[dict]] = {s.name: [] for s in wanted}
    maybe: dict[str, list[dict]] = {s.name: [] for s in wanted}
    me = os.getpid()
    for proc in psutil.process_iter(['pid', 'ppid', 'cmdline', 'create_time']):
        info = proc.info
        cmd = info.get('cmdline') or []
        if info['pid'] == me or not cmd:
            continue
        line = ' '.join(cmd).replace('\\', '/')
        candidates = [s for s in wanted if s.script_name in line]
        if not candidates:
            continue
        try:
            cwd = proc.cwd()
        except (psutil.Error, OSError):
            cwd = None
        for svc in candidates:
            verdict = identify(root, svc, cmd, cwd)
            if verdict:
                (found if verdict == 'match' else maybe)[svc.name].append(
                    {'pid': info['pid'], 'ppid': info.get('ppid') or 0, 'create_time': info['create_time'] or 0.0, 'cmdline': list(cmd), 'cwd': cwd})
    for table_ in (found, maybe):
        for name, items in table_.items():
            table_[name] = [i for i in items
                            if not any(o['ppid'] == i['pid'] and o['cmdline'] == i['cmdline'] and o['pid'] != i['pid'] for o in items)]
    return found, maybe


def scan(root, names=None) -> dict:
    """{name: [instance]} of the verified matches (see scan_full)."""
    return scan_full(root, names)[0]


def convert_job_running(root) -> list[dict]:
    """Running `tools/tripo_pipeline/convert_job.py` processes of this checkout (they own the Tripo browser profile)."""
    return scan(root, ['convert_job'])['convert_job']


def studio_running(root) -> list[dict]:
    """Running `node tools/tripo_pipeline/studio.mjs` processes of this checkout (the Tripo browser profile is in use)."""
    return scan(root, ['studio'])['studio']


def _alive(rec: dict) -> bool:
    """The recorded process still exists with the recorded create time (a reused pid does not count)."""
    pid, created = rec.get('pid'), rec.get('create_time')
    if not pid or not created:
        return False
    try:
        proc = psutil.Process(pid)
        return abs(proc.create_time() - created) < CREATE_TIME_TOLERANCE and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _process_alive(pid, created) -> bool:
    """pid runs and (when `created` is known) has that create time."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return False
        return created is None or not isinstance(created, (int, float)) or abs(proc.create_time() - float(created)) < CREATE_TIME_TOLERANCE
    except psutil.Error:
        return False


def verified_handle(root, svc: Service, inst: dict):
    """ONE psutil.Process handle of `inst`, re-checked right now: same create time and still this checkout's `svc` (identify == match).
    None when the pid is gone, reused or no longer the service."""
    try:
        proc = psutil.Process(inst['pid'])
        if abs(proc.create_time() - inst['create_time']) >= CREATE_TIME_TOLERANCE:
            return None
        cmd = proc.cmdline()
        try:
            cwd = proc.cwd()
        except (psutil.Error, OSError):
            cwd = None
        return proc if identify(root, svc, cmd, cwd) == 'match' else None
    except psutil.Error:
        return None


def _lock_held(root, svc: Service) -> bool:
    """The daemon's single-instance lock (supervisor.lock / view_runner.lock, claim_lock-style byte lock) is held by some process."""
    if not svc.lock_file:
        return False
    path = batch(root) / svc.lock_file
    if not path.exists():
        return False
    if svc.name == 'supervisor':
        return modes.supervisor_running(root)[0]
    try:
        handle = path.open('rb')
    except OSError:
        return False
    try:
        if guard.try_lock(handle):
            guard.release_lock(handle)
            return False
        return True
    finally:
        handle.close()


# ------------------------------------------------------------------------------------------------------------------ conversion hold

def hold_path(root) -> Path:
    return batch(root) / HOLD_FILE


def read_hold(root) -> dict | None:
    """The CONVERT_HOLD file as a dict, None when absent ({} fields when it is unreadable: it still holds)."""
    path = hold_path(root)
    if not path.exists():
        return None
    raw = read_json(path)
    return raw if isinstance(raw, dict) else {'reason': 'unreadable CONVERT_HOLD file'}


def _hold_time(hold: dict) -> float | None:
    value = hold.get('since_unix')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return _parse_time(hold.get('since'))


def _parse_time(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def _hold_is_mine(hold: dict) -> bool:
    mine = _my_create_time()
    created = hold.get('owner_create_time')
    return hold.get('owner_pid') == os.getpid() and (mine is None or not isinstance(created, (int, float))
                                                     or abs(float(created) - mine) < CREATE_TIME_TOLERANCE)


MAX_RETIRING = 8


def _ident(inst: dict) -> dict:
    return {'pid': inst.get('pid'), 'create_time': inst.get('create_time')}


def _same_process(a: dict, b: dict) -> bool:
    """Two {pid, create_time} name the same process (a missing create time compares by pid only)."""
    if not isinstance(a, dict) or not isinstance(b, dict) or a.get('pid') != b.get('pid') or not a.get('pid'):
        return False
    ca, cb = a.get('create_time'), b.get('create_time')
    if isinstance(ca, (int, float)) and isinstance(cb, (int, float)) and not isinstance(ca, bool) and not isinstance(cb, bool):
        return abs(float(ca) - float(cb)) < CREATE_TIME_TOLERANCE
    return True


def _retiring(hold: dict | None) -> list[dict]:
    """The supervisor generations a hold must outlive (phase 5 review row 2): [{pid, create_time}] of the supervisors a stop / restart was sent
    to. A tick of one of them never releases the hold."""
    raw = hold.get('retiring') if isinstance(hold, dict) else None
    return [r for r in raw if isinstance(r, dict) and isinstance(r.get('pid'), int)] if isinstance(raw, list) else []


def write_hold(root, reason: str, now: float | None = None, retiring: list[dict] | None = None) -> dict:
    """Write OUR conversion hold (atomic): {since, since_unix, reason, owner_pid, owner_create_time, retiring}."""
    now = time.time() if now is None else now
    hold = {'since': datetime.fromtimestamp(now).isoformat(timespec='milliseconds'), 'since_unix': now, 'reason': reason,
            'owner_pid': os.getpid(), 'owner_create_time': _my_create_time()}
    if retiring:
        hold['retiring'] = retiring[-MAX_RETIRING:]
    guard.write_atomic(hold_path(root), (json.dumps(hold, indent=2) + '\n').encode('utf-8'))
    return hold


def _foreign_live(hold: dict | None) -> bool:
    return bool(hold) and not _hold_is_mine(hold) and _process_alive(hold.get('owner_pid'), hold.get('owner_create_time'))


def assert_hold(root, reason: str, *, renew: bool = False, now: float | None = None, retiring: dict | None = None) -> str | None:
    """Make sure a conversion hold exists. `renew` (a supervisor stop / restart, a manager start) writes ours with a new `since` unless a LIVE
    other owner holds it. Without `renew` an existing hold stays as it is, except a stale one (dead owner, older than STALE_HOLD_S), which is
    replaced by ours. `retiring` (a supervisor instance a stop is about to be sent to) is added to the hold's `retiring` generations; a rewrite
    keeps the live ones of the hold it replaces (release_hold never accepts their ticks). Returns an event text or None."""
    now = time.time() if now is None else now
    hold = read_hold(root)
    keep = [r for r in _retiring(hold) if _process_alive(r.get('pid'), r.get('create_time'))]
    if retiring and not any(_same_process(r, retiring) for r in keep):
        keep.append(_ident(retiring))
    if hold is None:
        write_hold(root, reason, now, keep)
        return f'conversion hold written ({reason})'
    if _foreign_live(hold):
        return None
    if renew or (retiring and not any(_same_process(r, retiring) for r in _retiring(hold))):
        write_hold(root, reason, now, keep)
        return f'conversion hold renewed ({reason})'
    if not _hold_is_mine(hold):
        since = _hold_time(hold)
        if since is None or now - since > STALE_HOLD_S:
            write_hold(root, reason, now, keep)
            return f'stale conversion hold of pid {hold.get("owner_pid")} replaced ({reason})'
    return None


def withdraw_retiring(root, inst: dict) -> bool:
    """Take the supervisor `inst` out of the hold's `retiring` generations (finding 7: its stop was never delivered, so it is still the
    intended generation). The hold keeps its owner, reason and `since`, so a tick that supervisor completed after it releases the hold. A
    live other owner's hold is never touched. True when the hold was rewritten."""
    hold = read_hold(root)
    if hold is None or _foreign_live(hold):
        return False
    before = _retiring(hold)
    keep = [r for r in before if not _same_process(r, inst)]
    if len(keep) == len(before):
        return False
    new = dict(hold)
    if keep:
        new['retiring'] = keep
    else:
        new.pop('retiring', None)
    guard.write_atomic(hold_path(root), (json.dumps(new, indent=2) + '\n').encode('utf-8'))
    return True


def supervisor_stop_file(root) -> Path:
    """`supervisor.stop`: written by `pipeline_supervisor.py stop`, removed by the supervisor at the very end of its exit."""
    return batch(root) / 'supervisor.stop'


def _stop_delivered(root, inst: dict) -> bool:
    """The stop reached the supervisor `inst`: its stop file exists (it exits through its normal path) or the process is already gone."""
    return supervisor_stop_file(root).exists() or not _process_alive(inst['pid'], inst['create_time'])


def _stop_not_delivered(root, rec: dict, inst: dict, why: str, now: float) -> None:
    """Non-delivery of a supervisor stop is established (finding 7): withdraw the request and the `retiring` designation, record why."""
    rec.update(stop_requested=None, then_start=False, stop_note=None,
               stop_delivery={'state': 'failed', 'time': now, 'pid': inst['pid'], 'create_time': inst['create_time'], 'why': why[:300]})
    rec.pop('pause_backup', None)             # the supervisor did not exit: its PAUSE file was never removed
    rec.pop('restart_request', None)
    write_record(root, 'supervisor', rec)
    withdraw_retiring(root, inst)


def supervisor_tick(root) -> dict | None:
    raw = read_json(batch(root) / TICK_FILE)
    return raw if isinstance(raw, dict) else None


def supervisor_ticked(root, sup: dict | None, after: float | None) -> bool:
    """The running supervisor `sup` (a scan instance) completed a tick after `after`: supervisor_tick.json names its pid (and create time,
    when the file carries one) and its `time` is later than `after`."""
    if not sup:
        return False
    tick_ = supervisor_tick(root)
    if not tick_ or tick_.get('pid') != sup['pid']:
        return False
    created = tick_.get('create_time')
    if isinstance(created, (int, float)) and not isinstance(created, bool) and abs(float(created) - sup['create_time']) >= CREATE_TIME_TOLERANCE:
        return False
    stamp = _parse_time(tick_.get('time'))
    return stamp is not None and (after is None or stamp > after)


def release_blocker(root, sup: dict | None, hold: dict | None = None) -> str | None:
    """Why the hold may not be released for the running supervisor `sup` even if it ticked (phase 5 review row 2), else None: `sup` is a
    generation a stop / restart was sent to (the hold's `retiring`), the supervisor's stop is still pending for that very process (the
    restart transition is not complete), or a legacy convert worker is being drained under the hold (I.2)."""
    hold = read_hold(root) if hold is None else hold
    if sup and any(_same_process(r, sup) for r in _retiring(hold)):
        return f'supervisor pid {sup["pid"]} is the outgoing one (a stop was sent to it); waiting for its successor'
    rec = read_record(root, 'supervisor')
    if sup and rec.get('stop_requested') and _same_process(rec, sup):
        return f'the stop of supervisor pid {sup["pid"]} is still pending'
    conv = read_record(root, 'convert')
    if conv.get('stop_requested') and conv.get('drain_hold'):
        return 'an older convert worker is being drained (the hold stays until it is gone)'
    return None


def release_hold(root, sup: dict | None) -> str | None:
    """Remove the hold when it is ours (or stale), the running supervisor ticked after its `since` (I.1) AND that supervisor is the intended
    generation: not one a stop / restart was sent to, with no stop of it pending, and no legacy worker drain needs the hold (release_blocker).
    Returns an event text or None."""
    hold = read_hold(root)
    if hold is None or _foreign_live(hold):
        return None
    if not _hold_is_mine(hold):
        since = _hold_time(hold)
        if since is None or time.time() - since <= STALE_HOLD_S:
            return None                        # a recent hold of a process that just exited: it may still be part of a by-hand restart
    if release_blocker(root, sup, hold):
        return None
    if supervisor_ticked(root, sup, _hold_time(hold)):
        hold_path(root).unlink(missing_ok=True)
        return f'conversion hold released (supervisor pid {sup["pid"]} completed a tick)'
    return None


def begin_management(root) -> list[str]:
    """The services thread's first step (I.1): our conversion hold exists before any convert worker is started or adopted. A hold of a dead
    owner is replaced (whatever its age: ours keeps the hold continuous), a live other owner's hold is left alone."""
    with _LOCK:
        text = assert_hold(root, 'preview app manager start', renew=True)
        events = [text] if text else []
        try:                                   # after the hold: what a manager that crashed left behind (row 6 residual, I.10)
            events += recover_suspensions(root)
            events += settle_drain(root)
        except Exception as error:  # noqa: BLE001  (never keeps the hold from being reported)
            events.append(f'recovery at manager start failed: {type(error).__name__}: {error}')
    return events


def hold_state(root) -> dict | None:
    """`hold` of the status: the file plus who owns it and what removes it."""
    hold = read_hold(root)
    if hold is None:
        return None
    owner = 'this server' if _hold_is_mine(hold) else ('another live process' if _foreign_live(hold) else 'a process that exited')
    retiring = [r for r in _retiring(hold) if _process_alive(r.get('pid'), r.get('create_time'))]
    message = 'conversions wait until the running supervisor completes a tick (needs the supervisor service)'
    if retiring:
        message = (f'conversions wait until a supervisor started after pid {", ".join(str(r["pid"]) for r in retiring)} (a stop was sent to it) '
                   'completes a tick')
    return {**{k: hold.get(k) for k in ('since', 'reason', 'owner_pid')}, 'owner': owner, 'retiring': [r['pid'] for r in retiring],
            'message': message}


# ------------------------------------------------------------------------------------------------------------------------ start / stop

def _spawn(root, svc: Service, settings: dict) -> dict:
    """Start the service detached; returns {pid, create_time, cmdline}. Logs are appended to <name>_win.out.log / .err.log."""
    cmd = svc.command(settings)
    primary = {kind: batch(root) / f'{svc.log_stems[0]}.{kind}.log' for kind in ('out', 'err')}
    batch(root).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env['PYTHONUTF8'] = '1'
    with open(primary['out'], 'ab') as out, open(primary['err'], 'ab') as err:
        out.write(f'=== {datetime.now().isoformat(timespec="seconds")} preview app starts: {" ".join(cmd)}\n'.encode('utf-8'))
        out.flush()
        kwargs = {'creationflags': DETACHED} if os.name == 'nt' else {'start_new_session': True}
        proc = subprocess.Popen(cmd, cwd=str(root), stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, **kwargs)
    try:
        created = psutil.Process(proc.pid).create_time()
    except psutil.Error:
        created = time.time()
    return {'pid': proc.pid, 'create_time': created, 'cmdline': cmd}


def _fresh_record(inst: dict, adopted: bool, old: dict | None = None) -> dict:
    rec = {'pid': inst['pid'], 'create_time': inst['create_time'], 'cmdline': inst['cmdline'], 'adopted': adopted,
           'since': inst['create_time'], 'running': True, 'exited_at': None, 'stop_requested': None, 'then_start': False,
           'duplicates': [], 'blocked': None}
    for key in ('fails', 'error', 'user_stopped', 'held', 'next_restart', 'idle_exit'):
        if old and key in old:
            rec[key] = old[key]
    return rec


def _start_blocker(root, svc: Service, maybe: list[dict] | None = None) -> str | None:
    """Why a new instance must not be started although no verified instance runs (an unverified look-alike, a held instance lock)."""
    if maybe:
        return (f'pid {", ".join(str(i["pid"]) for i in maybe)} runs {svc.script_name} {svc.subcommand or ""}'.rstrip()
                + ' but could not be verified as this checkout\'s service (cwd unreadable); not starting another')
    if _lock_held(root, svc):
        return f'{svc.lock_file} is held: an instance runs that the scan did not match; not starting another'
    return None


def start(root, name: str, settings: dict | None = None) -> dict:
    """The Start button: start `name` now (also when it is disabled in the settings, or held). Refuses when an instance already runs or might
    run. Clears held / stopped / failure state. Returns the new status."""
    svc = _service(name)
    settings = settings or app_settings.load(root)
    with _LOCK:
        found, maybe = scan_full(root, [name])
        if found[name]:
            raise ServiceError(f'{name} is already running (pid {", ".join(str(i["pid"]) for i in found[name])})')
        blocker = _start_blocker(root, svc, maybe[name])
        if blocker:
            raise ServiceError(f'{name}: {blocker}')
        try:
            inst = _spawn(root, svc, settings)
        except OSError as error:
            raise ServiceError(f'{name} could not start: {error}') from error
        rec = _fresh_record(inst, False)
        rec.update(fails=0, error=None, user_stopped=False, held=False, next_restart=None, idle_exit=False)
        write_record(root, name, rec)
    return status_one(root, svc, settings)


def _service(name: str) -> Service:
    if name not in SERVICES:
        raise ServiceError(f'unknown service {name!r}; one of {list(SERVICES)}')
    return SERVICES[name]


def _terminate(root, svc: Service, inst: dict, timeout: float = 10.0, allow_kill: bool = True) -> bool:
    """Stop one process through ONE verified handle (same create time, still this checkout's service; F5). `allow_kill`: a process that
    ignores the terminate is killed after `timeout` (the view runner); without it a timeout leaves it running. True when it is gone."""
    proc = verified_handle(root, svc, inst)
    if proc is None:
        if _process_alive(inst['pid'], None):      # the pid runs, but not as the recorded process / service: never signalled
            raise ServiceError(f'process {inst["pid"]} is no longer the {svc.name} service; nothing stopped')
        return True
    try:
        proc.terminate()
        proc.wait(timeout)
        return True
    except psutil.NoSuchProcess:
        return True
    except psutil.TimeoutExpired:
        if not allow_kill:
            return False
        try:
            if proc.is_running():
                proc.kill()
                proc.wait(5)
        except psutil.NoSuchProcess:
            return True
        except (psutil.TimeoutExpired, psutil.Error):
            pass
        return not proc.is_running()
    except psutil.Error:
        return not proc.is_running()


def _wait_gone(root, name: str, timeout: float, inst: dict | None = None) -> bool:
    end = time.monotonic() + timeout
    while True:
        if inst is not None:
            if not _process_alive(inst['pid'], inst['create_time']):
                return True
        elif not scan(root, [name])[name]:
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.2)


def _restore_pause(root, rec: dict) -> None:
    """A restarting supervisor deletes the PAUSE file on its way out (`release_all`). The conversion hold covers that gap (I.1); the old PAUSE
    file is also put back at once, as before (the new supervisor rewrites it from the mode on its first tick)."""
    backup = rec.pop('pause_backup', None)
    path = batch(root) / 'PAUSE'
    if backup is not None and not path.exists():
        guard.write_atomic(path, backup.encode('utf-8'))


def _finish_stop(root, name: str, rec: dict, now: float) -> None:
    """The process is gone after a stop request: record a deliberate stop (or the pending restart)."""
    rec.update(running=False, exited_at=now, stop_requested=None, user_stopped=not rec.get('then_start'), idle_exit=False, stop_note=None,
               drain_hold=False)
    rec['duplicates'] = []
    write_record(root, name, rec)


# ------------------------------------------------------------------------------------------------------------- convert worker stop (I.2)

def worker_stop_path(root, worker: int = MANAGED_WORKER) -> Path:
    return batch(root) / f'convert_worker{worker}.stop'


def worker_stopping_path(root, worker: int = MANAGED_WORKER) -> Path:
    return batch(root) / f'convert_worker{worker}.stopping'


def write_worker_stop(root, inst: dict, request, now: float, worker: int = MANAGED_WORKER) -> None:
    data = {'pid': inst['pid'], 'create_time': inst['create_time'], 'request': request, 'time': iso(now)}
    guard.write_atomic(worker_stop_path(root, worker), (json.dumps(data, indent=2) + '\n').encode('utf-8'))


def _names(path: Path, inst: dict) -> bool:
    raw = read_json(path)
    if not isinstance(raw, dict) or raw.get('pid') != inst['pid']:
        return False
    created = raw.get('create_time')
    return not isinstance(created, (int, float)) or abs(float(created) - inst['create_time']) < CREATE_TIME_TOLERANCE


def legacy_worker(root, inst: dict) -> bool:
    """A worker that may not know the per-worker stop file: it started before the current convert_queue.py was written, or that file has no
    stop support (`.stopping`)."""
    script = Path(root) / SERVICES['convert'].script
    try:
        mtime = script.stat().st_mtime
        text = script.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return True
    return '.stopping' not in text or inst['create_time'] < mtime


# ------------------------------------------------------------------------------------------------ suspension record (row 6 residual)

def suspensions_path(root) -> Path:
    return records_dir(root) / 'suspensions.json'


def read_suspensions(root) -> list[dict]:
    raw = read_json(suspensions_path(root))
    items = raw.get('items') if isinstance(raw, dict) else None
    return [i for i in items if isinstance(i, dict) and isinstance(i.get('pid'), int)] if isinstance(items, list) else []


def _write_suspensions(root, items: list[dict]) -> None:
    path = suspensions_path(root)
    if not items:
        path.unlink(missing_ok=True)
        return
    guard.write_atomic(path, (json.dumps({'items': items}, indent=2) + '\n').encode('utf-8'))


def _note_suspension(root, service: str, inst: dict, terminated: bool = False) -> None:
    """Persist that `inst` is about to be suspended by this manager (or, `terminated`, that a terminate was sent while it was frozen)."""
    items = [i for i in read_suspensions(root) if not _same_process(i, inst)]
    items.append({'pid': inst['pid'], 'create_time': inst['create_time'], 'service': service, 'time': time.time(), 'terminated': terminated,
                  'owner_pid': os.getpid()})
    _write_suspensions(root, items)


def _clear_suspension(root, inst: dict) -> None:
    try:
        items = read_suspensions(root)
        keep = [i for i in items if not _same_process(i, inst)]
        if len(keep) != len(items):
            _write_suspensions(root, keep)
    except OSError:
        pass


def recover_suspensions(root) -> list[str]:
    """Resume what a crashed manager left suspended (phase-4 row 6 residual): every recorded process that still runs with the recorded create
    time, is still this checkout's service (verified_handle) and is still suspended is resumed; an entry whose process is gone (terminated,
    or exited) is dropped; a pid that now is something else is never touched. Returns event texts."""
    events, keep = [], []
    with _LOCK:
        items = read_suspensions(root)
        for item in items:
            svc = {**SERVICES, **PROBES}.get(item.get('service'))
            inst = {'pid': item['pid'], 'create_time': item.get('create_time') or 0.0}
            if svc is None or not _process_alive(item['pid'], item.get('create_time')):
                continue                                              # gone (terminated or exited), or not a known service: forget it
            proc = verified_handle(root, svc, inst)
            if proc is None:
                events.append(f'{svc.name}: pid {item["pid"]} was recorded as suspended but is no longer this checkout\'s {svc.name}; not touched')
                continue
            try:
                if proc.status() != psutil.STATUS_STOPPED:
                    continue                                          # it runs: nothing to do
                proc.resume()
                events.append(f'{svc.name}: pid {item["pid"]} was left suspended by an earlier manager; resumed'
                              + (' (a stop was already sent to it)' if item.get('terminated') else ''))
            except psutil.NoSuchProcess:
                continue
            except psutil.Error as error:
                keep.append(item)
                events.append(f'{svc.name}: pid {item["pid"]} is still suspended and could not be resumed ({type(error).__name__})')
        if len(keep) != len(items):
            _write_suspensions(root, keep)
    return events


def _event_time(line: str, now: float) -> float | None:
    """The time of a convert_events.log line (`MM-DD HH:MM:SS ...`, the current year; a date in the future belongs to the previous year)."""
    try:
        stamp = datetime.strptime(f'{datetime.fromtimestamp(now).year}-{line[:14]}', '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None
    if stamp.timestamp() > now + 86400:
        stamp = stamp.replace(year=stamp.year - 1)
    return stamp.timestamp()


def worker_last_event(root, inst: dict, now: float | None = None, worker: int = MANAGED_WORKER) -> str | None:
    """The text of the worker's last line in convert_events.log since it started (without the time and the worker tag); None when it wrote
    nothing since then or the last line cannot be read."""
    now = time.time() if now is None else now
    tag = '' if worker == 1 else f'[w{worker}] '
    for line in reversed(tail_lines(batch(root) / SERVICES['convert'].events_log, 200, 262144)):
        body = line[15:] if len(line) > 15 else ''
        if worker == 1 and body.startswith('[w'):
            continue
        if tag and not body.startswith(tag):
            continue
        body = body[len(tag):]
        stamp = _event_time(line, now)
        if stamp is None or stamp < inst['create_time'] - 2:
            return None                           # nothing from this worker since it started (or an unreadable line)
        return body
    return None


def worker_sleeping(root, inst: dict, now: float | None = None, worker: int = MANAGED_WORKER) -> bool:
    """The legacy-stop rule of I.2: the worker's last event (since it started) is PAUSED / IDLE / LOW DISK, after its last CONVERT, and it is
    not an exit announcement (an exiting worker runs finish / offload). Nothing since it started = not proven asleep."""
    body = worker_last_event(root, inst, now, worker)
    return body is not None and body.startswith(SLEEP_EVENTS) and 'exiting' not in body


def frozen_stop(root, svc: Service, inst: dict, check) -> tuple[bool, str | None]:
    """Stop an older daemon that has no drain acknowledgement only when it is PROVEN idle (phase 5 review rows 6 and 17): its verified handle is
    SUSPENDED first, so it cannot wake between the check and the signal; `check(proc)` then inspects the frozen process (its children, the
    files that would show work in flight) and returns a problem text or None. With a problem the process is resumed untouched and (False,
    problem) comes back; without one it is terminated while still frozen (resumed afterwards, so a POSIX SIGTERM is delivered) and waited for.
    The suspension is PERSISTED first (suspensions.json: pid, create time, time; row 6 residual) and the entry is removed once the process
    is resumed or gone, so a manager that crashes in between leaves a record the next manager start resumes (recover_suspensions).
    Returns (gone, problem)."""
    proc = verified_handle(root, svc, inst)
    if proc is None:
        if _process_alive(inst['pid'], None):      # the pid runs, but not as the recorded process / service: never signalled
            raise ServiceError(f'process {inst["pid"]} is no longer the {svc.name} service; nothing stopped')
        return True, None
    try:
        _note_suspension(root, svc.name, inst)
    except OSError as error:
        return False, f'the suspension could not be recorded ({type(error).__name__}: {error}), so it is not suspended; waiting'
    try:
        proc.suspend()
    except psutil.NoSuchProcess:
        _clear_suspension(root, inst)
        return True, None
    except psutil.Error as error:
        _clear_suspension(root, inst)
        return False, f'it could not be suspended to verify that it is idle ({type(error).__name__}); waiting'
    terminated = False
    try:
        try:
            problem = check(proc)
        except psutil.NoSuchProcess:
            return True, None
        except Exception as error:  # noqa: BLE001  (an unverifiable state is never "idle")
            problem = f'its state could not be verified ({type(error).__name__}: {error}); waiting'
        if problem:
            return False, problem
        try:
            try:
                _note_suspension(root, svc.name, inst, terminated=True)
            except OSError:
                pass
            proc.terminate()
            terminated = True
        except psutil.NoSuchProcess:
            return True, None
    finally:
        resumed = True
        try:
            proc.resume()
        except psutil.NoSuchProcess:
            pass
        except psutil.Error:
            resumed = False                      # the record stays: the next manager start tries again
        if resumed:
            _clear_suspension(root, inst)
    try:
        proc.wait(10.0)
        return True, None
    except psutil.NoSuchProcess:
        return True, None
    except psutil.TimeoutExpired:
        return False, 'it was signalled while idle but has not exited yet'
    except psutil.Error:
        return (not proc.is_running()) if terminated else False, None


def _locks_naming(root, pid: int) -> list[str]:
    """Job locks (`convert_locks/<job>.lock`, holder pid as text) that name `pid`: a worker holding one is between claim and conversion."""
    out = []
    for lock in (batch(root) / 'convert_locks').glob('*.lock'):
        try:
            if int(lock.read_text(encoding='utf-8').strip()) == pid:
                out.append(lock.stem)
        except (OSError, ValueError):
            continue
    return out


def _worker_idle_problem(root, proc, inst: dict) -> str | None:
    """The frozen legacy worker is provably asleep: no child process (a convert_job.py, a balance check), no convert_job.py of this checkout,
    no job lock naming it, and its last event since it started is PAUSED / IDLE / LOW DISK after its last CONVERT."""
    kids = proc.children()
    if kids:
        return f'an older worker: it has a child process (pid {kids[0].pid}): it woke up; waiting'
    jobs = convert_job_running(root)
    if jobs:
        return f'an older worker: convert_job.py (pid {jobs[0]["pid"]}) runs; waiting'
    locks = _locks_naming(root, inst['pid'])
    if locks:
        return f'an older worker: it holds the job lock of {locks[0]}; waiting'
    if not worker_sleeping(root, inst, time.time()):
        return 'an older worker: its event log no longer says it sleeps (it woke up); waiting'
    return None


# ------------------------------------------------------------------------------------------------------- conversion drain (I.10)

def drain_path(root) -> Path:
    return batch(root) / DRAIN_FILE


def read_drain(root) -> dict | None:
    """The CONVERT_DRAIN file as a dict, None when absent ({} fields when unreadable: it still drains)."""
    path = drain_path(root)
    if not path.exists():
        return None
    raw = read_json(path)
    return raw if isinstance(raw, dict) else {'reason': 'unreadable CONVERT_DRAIN file'}


def write_drain(root, inst: dict, now: float | None = None) -> dict:
    """Ask the convert_job.py processes blocked at the paid gate to leave (I.10). Written only AFTER the conversion hold."""
    now = time.time() if now is None else now
    drain = {'since': datetime.fromtimestamp(now).isoformat(timespec='milliseconds'), 'since_unix': now, 'reason': 'legacy convert worker drain',
             'owner_pid': os.getpid(), 'owner_create_time': _my_create_time(), 'worker': _ident(inst)}
    guard.write_atomic(drain_path(root), (json.dumps(drain, indent=2) + '\n').encode('utf-8'))
    return drain


def _stale_file(path: Path, age: float = 60.0) -> bool:
    try:
        return time.time() - path.stat().st_mtime > age
    except OSError:
        return False


def gate_waiters(root, clean: bool = True) -> list[dict]:
    """convert_job.py processes of this root that are blocked at the paid gate right now (`gate_wait/<pid>.json`, kept by the process while
    it waits): [{pid, create_time, job, module, step, since}]. With `clean` a file naming a process that no longer runs is removed."""
    out = []
    for path in sorted((batch(root) / 'gate_wait').glob('*.json')):
        raw = read_json(path)
        if not isinstance(raw, dict):
            if clean and _stale_file(path):
                path.unlink(missing_ok=True)
            continue
        if not _process_alive(raw.get('pid'), raw.get('create_time')):
            if clean:
                path.unlink(missing_ok=True)
            continue
        out.append({k: raw.get(k) for k in ('pid', 'create_time', 'job', 'module', 'step', 'since')})
    return out


def drain_exits(root) -> list[dict]:
    """The exits convert_job.py recorded when it left the gate for a drain (`drain_exits/*.json`), each with its `path`."""
    out = []
    for path in sorted((batch(root) / 'drain_exits').glob('*.json')):
        raw = read_json(path)
        if isinstance(raw, dict):
            out.append(raw | {'path': path})
        elif _stale_file(path):
            path.unlink(missing_ok=True)
    return out


def _plain_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def settle_drain_exits(root, now: float | None = None) -> list[dict]:
    """Consume the recorded drain exits whose process is gone and restore the failure counts an OLDER worker wrote for them (it counts any
    non-zero exit; the current convert_queue.py marks its children `parent_aware` and counts nothing). Per job: `<job>.fails` goes back to
    max(the lowest `fails_before` of those exits, the count now - their number), never up, and the file is removed at 0. Returns one entry
    per job {job, exits, counted, modules, fails_from?, fails_to?}; every entry is appended to `convert_drain.log`."""
    now = time.time() if now is None else now
    by_job: dict[str, list[dict]] = {}
    done = []
    for item in drain_exits(root):
        if _process_alive(item.get('pid'), item.get('create_time')):
            continue                                                   # still on its way out
        done.append(item['path'])
        job = item.get('job')
        if isinstance(job, str) and '--' in job and Path(job).name == job:
            by_job.setdefault(job, []).append(item)
    restored = []
    locks = batch(root) / 'convert_locks'
    for job, items in sorted(by_job.items()):
        counted = [i for i in items if not i.get('parent_aware') and _plain_count(i.get('fails_before'))]
        entry = {'job': job, 'exits': len(items), 'counted': len(counted), 'modules': sorted({str(i.get('module')) for i in items})}
        if counted:
            path = locks / f'{job}.fails'
            try:
                current = int(path.read_text(encoding='utf-8').strip())
            except FileNotFoundError:
                current = 0
            except (OSError, ValueError):
                current = None
                entry['problem'] = 'the failure count could not be read; not restored'
            if current is not None:
                target = max(min(i['fails_before'] for i in counted), current - len(counted))
                if target < current:
                    if target <= 0:
                        path.unlink(missing_ok=True)
                    else:
                        guard.write_atomic(path, str(target).encode('utf-8'))
                    entry.update(fails_from=current, fails_to=target)
        restored.append(entry)
    if restored:
        try:
            with open(batch(root) / 'convert_drain.log', 'a', encoding='utf-8') as handle:
                for entry in restored:
                    handle.write(json.dumps({'time': iso(now)} | entry) + '\n')
        except OSError:
            pass
    for path in done:
        path.unlink(missing_ok=True)
    return restored


def settle_drain(root, now: float | None = None) -> list[str]:
    """End a conversion drain that is no longer pending (the drained worker is gone, or the convert record carries no legacy stop any more):
    restore the failure counts of its drain exits (settle_drain_exits), remove the job locks naming the drained worker once it is gone, delete
    CONVERT_DRAIN and record what was restored (`drain_restored` / `drain_finished` of the convert record). While the drain is pending
    nothing is touched. Returns event texts."""
    now = time.time() if now is None else now
    with _LOCK:
        drain = read_drain(root)
        conv = read_record(root, 'convert')
        if drain is not None and conv.get('stop_requested') and conv.get('drain_hold'):
            return []
        folder = batch(root) / 'drain_exits'
        if drain is None and not (folder.is_dir() and any(folder.glob('*.json'))):
            return []
        events = []
        restored = settle_drain_exits(root, now)
        worker = drain.get('worker') if isinstance(drain, dict) and isinstance(drain.get('worker'), dict) else None
        unlocked = []
        if worker and isinstance(worker.get('pid'), int) and not _process_alive(worker['pid'], None):
            for job in _locks_naming(root, worker['pid']):
                (batch(root) / 'convert_locks' / f'{job}.lock').unlink(missing_ok=True)
                unlocked.append(job)
        for entry in restored:
            if 'fails_to' in entry:
                events.append(f'convert: failure count of {entry["job"]} restored {entry["fails_from"]} -> {entry["fails_to"]} '
                              f'({entry["counted"]} drain exit(s) the older worker counted as failures)')
            elif entry.get('problem'):
                events.append(f'convert: drain exit of {entry["job"]}: {entry["problem"]}')
        if unlocked:
            events.append(f'convert: removed the job lock(s) of the stopped older worker: {", ".join(unlocked)}')
        if drain is not None:
            drain_path(root).unlink(missing_ok=True)
            deferred = sum(e['exits'] for e in restored)
            events.append(f'conversion drain finished: CONVERT_DRAIN removed ({deferred} deferred convert_job exit(s))')
        if restored or unlocked or drain is not None:
            conv = read_record(root, 'convert')
            conv.update(drain_restored=restored, drain_unlocked=unlocked, drain_finished=now)
            write_record(root, 'convert', conv)
    return events


def drain_state(root) -> dict | None:
    """`drain` of the status: the CONVERT_DRAIN file plus the convert_job.py processes waiting at the paid gate (they leave by themselves)."""
    drain = read_drain(root)
    if drain is None:
        return None
    worker = drain.get('worker') if isinstance(drain.get('worker'), dict) else {}
    return {'since': drain.get('since'), 'reason': drain.get('reason'), 'worker_pid': worker.get('pid'), 'gate_waiters': gate_waiters(root, clean=False),
            'message': 'an older convert worker is being drained: convert_job.py processes blocked at the paid gate leave (deferred, not failed)'}


def _cycling(root, inst: dict, since: float | None) -> bool:
    """The drained worker keeps taking jobs: a convert_job.py left the gate for this drain although its parent is not a current worker (the
    older worker launched it after intake was stopped, or will count its exit and try the next job)."""
    for item in drain_exits(root):
        stamp = item.get('time')
        if not item.get('parent_aware') and isinstance(stamp, (int, float)) and (since is None or stamp >= since - 1):
            return True
    return False


def _drain_idle_problem(root, proc, inst: dict) -> str | None:
    """The frozen CYCLING legacy worker can be terminated: it has no child (a child that waits at the paid gate, or that just started and
    will reach the gate, is waited for up to DRAIN_SETTLE: frozen, the worker cannot start another) and its last event since it started is
    one after which it only releases the job and picks the next (DRAIN_SAFE_EVENTS; never while it finishes / offloads a set)."""
    deadline = time.monotonic() + DRAIN_SETTLE
    while True:
        kids = proc.children(recursive=True)
        if not kids:
            break
        waiting = {w['pid'] for w in gate_waiters(root)}
        now = time.time()
        young = True
        for kid in kids:
            try:
                young = young and now - kid.create_time() < DRAIN_YOUNG
            except psutil.Error:
                continue
        if not (young or any(k.pid in waiting for k in kids)) or time.monotonic() >= deadline:
            return f'an older worker: it has a child process (pid {kids[0].pid}) that is not leaving the paid gate; waiting'
        time.sleep(0.2)
    body = worker_last_event(root, inst, time.time())
    if body is None or not body.startswith(DRAIN_SAFE_EVENTS) or 'exiting' in body:
        return 'an older worker: its last event is not one after which it only picks the next job (it may be finishing a set); waiting'
    return None


def _convert_stop_step(root, svc: Service, inst: dict, rec: dict, now: float) -> tuple[bool, str]:
    """One step of a pending convert stop: (gone, message). Writes / renews the stop file naming the process. A legacy worker (I.2, phase 5
    review row 6, I.10) is drained in this order: the conversion hold FIRST (intake is stopped: a hold-aware worker starts nothing, and every
    convert_job.py started from the current code waits before its paid steps; the hold stays while the drain is pending, `drain_hold`), THEN
    CONVERT_DRAIN (the convert_job.py processes blocked at the paid gate leave by themselves: deferred, lock released, exit 76), then wait
    until no convert_job.py runs and the worker sleeps, then frozen_stop: suspended, re-verified idle, only then terminated. A worker that
    keeps taking jobs meanwhile (_cycling) is frozen and terminated while it has no child (_drain_idle_problem). Never kills anything else."""
    if not _names(worker_stop_path(root), inst):
        write_worker_stop(root, inst, rec.get('stop_request_id') or iso(rec.get('stop_requested')), now)
    if _names(worker_stopping_path(root), inst):
        return False, 'the worker acknowledged the stop; it exits after its current job (finish and offload included)'
    if legacy_worker(root, inst):
        if not rec.get('drain_hold') or read_hold(root) is None:
            assert_hold(root, 'legacy convert worker drain')
            current = read_record(root, 'convert')
            current['drain_hold'] = rec['drain_hold'] = True
            write_record(root, 'convert', current)
        if read_hold(root) is None:
            return False, 'an older worker: the conversion hold could not be written; it is not stopped'
        drain = read_drain(root)
        if drain is None or not _same_process(drain.get('worker') if isinstance(drain.get('worker'), dict) else {}, inst):
            drain = write_drain(root, inst, now)          # after the hold: intake is stopped first, then the gate-waiters are asked to leave
        waiting = {w['pid'] for w in gate_waiters(root)}
        jobs = convert_job_running(root)
        working = [j for j in jobs if j['pid'] not in waiting]
        if not jobs and worker_sleeping(root, inst, now):
            gone, problem = frozen_stop(root, svc, inst, lambda proc: _worker_idle_problem(root, proc, inst))
            if gone:
                return True, 'the older worker was asleep (verified while suspended): stopped by its verified PID'
            return False, problem or 'the older worker did not exit yet'
        if _cycling(root, inst, _hold_time(drain)):
            gone, problem = frozen_stop(root, svc, inst, lambda proc: _drain_idle_problem(root, proc, inst))
            if gone:
                return True, ('the older worker kept taking jobs during the drain: stopped by its verified PID while it had no child '
                              '(verified while suspended)')
            return False, problem or 'the older worker did not exit yet'
        if working:
            return False, (f'an older worker without the stop file: it is stopped only while it sleeps; convert_job.py (pid {working[0]["pid"]}) '
                           'runs now (conversions are held meanwhile'
                           + (f'; {len(jobs) - len(working)} more wait at the paid gate and leave by themselves' if len(jobs) > len(working) else '')
                           + ')')
        if jobs:
            return False, (f'an older worker without the stop file: {len(jobs)} convert_job.py process(es) blocked at the paid gate are leaving '
                           '(CONVERT_DRAIN: deferred, not failed); conversions are held meanwhile')
        return False, ('an older worker without the stop file: waiting until its event log says it sleeps (PAUSED / IDLE / LOW DISK); '
                       'conversions are held meanwhile')
    return False, 'stop requested; waiting for the worker to acknowledge it (convert_worker1.stopping)'


# ------------------------------------------------------------------------------------------------------------- view runner stop (I.5)

def legacy_runner(root, inst: dict) -> bool:
    """A view runner without the launch reservation of I.5 (an older runner: it started before the current view_batch.py was written, that
    file has no `launching` state, or it does not hold view_runner.lock). Stopping it between a spawn and its status write would lose the job."""
    script = Path(root) / SERVICES['views'].script
    try:
        mtime = script.stat().st_mtime
        text = script.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return True
    return "'launching'" not in text or inst['create_time'] < mtime or not _lock_held(root, SERVICES['views'])


def _recorded_jobs(root) -> set[str]:
    """Codex jobs the view runner's task status files record as running / launching (the ones a new runner adopts)."""
    out = set()
    for path in (batch(root) / 'status').glob('*.json'):
        raw = read_json(path)
        jobs = raw.get('jobs') if isinstance(raw, dict) else None
        if isinstance(raw, dict) and raw.get('state') in ('running', 'launching') and isinstance(jobs, list) and jobs \
                and isinstance(jobs[-1], dict) and isinstance(jobs[-1].get('job'), str):
            out.add(jobs[-1]['job'])
    return out


def _runner_idle_problem(root, proc) -> str | None:
    """The frozen older runner launches nothing right now: every child is a `codex_imagegen.sh <job>` whose job its status files already
    record as running (a new runner adopts it through `.done`), and it has no other child (a framing check of a finishing task)."""
    recorded = None
    for child in proc.children():
        try:
            cmd = child.cmdline()
        except psutil.NoSuchProcess:
            continue
        at = next((i for i, a in enumerate(cmd) if a.replace('\\', '/').endswith('codex_imagegen.sh')), None)
        if at is None or at + 1 >= len(cmd):
            return f'the older view runner is busy (child pid {child.pid} is not a recorded Codex job); waiting'
        recorded = _recorded_jobs(root) if recorded is None else recorded
        if cmd[at + 1] not in recorded:
            return f'the older view runner is launching Codex job {cmd[at + 1]} (not recorded yet); waiting'
    return None


def _views_stop_step(root, svc: Service, inst: dict, now: float) -> tuple[bool, str]:
    """One step of a view runner stop: a current runner (launch reservation, I.5) by its verified handle at once (running Codex jobs continue);
    an older one only through frozen_stop while it launches nothing (phase 5 review row 17)."""
    if not legacy_runner(root, inst):
        gone = _terminate(root, svc, inst)
        return gone, 'view runner stopped (running Codex jobs continue)' if gone else 'view runner is still exiting'
    gone, problem = frozen_stop(root, svc, inst, lambda proc: _runner_idle_problem(root, proc))
    if gone:
        return True, 'older view runner stopped while it launched nothing (its running Codex jobs continue; the next runner adopts them)'
    return False, problem or 'the older view runner did not exit yet'


def _clean_worker_files(root) -> None:
    """Delete per-worker stop / stopping files that name a process which no longer runs (I.2)."""
    for path in list(batch(root).glob('convert_worker*.stop')) + list(batch(root).glob('convert_worker*.stopping')):
        raw = read_json(path)
        if isinstance(raw, dict) and not _process_alive(raw.get('pid'), raw.get('create_time')):
            path.unlink(missing_ok=True)


def stop(root, name: str, *, wait: float = 5.0, force: bool = False, then_start: bool = False, settings: dict | None = None,
         request=None) -> dict:
    """The Stop button (restart = then_start). Returns {"ok", "pending", "message", "status"}:
      supervisor: the conversion hold is written FIRST (I.1), then `python tools/pipeline_supervisor.py stop` (its own stop file)
      views:      by its verified handle (F5); running Codex jobs continue (the next runner re-adopts them)
      convert:    graceful only (I.2): `convert_worker1.stop` names the worker process; waits up to `wait` s for its exit and never kills it on a
                  timeout: the stop stays pending and the tick re-evaluates it. A legacy worker is stopped by its handle only while asleep.
                  `force=True` is refused while a convert_job.py runs (nothing is ever killed mid-conversion).
    Refused (ServiceError) while two instances run: duplicates are the user's to fix."""
    svc = _service(name)
    settings = settings or app_settings.load(root)
    with _LOCK:
        found = scan(root, [name])[name]
        now = time.time()
        rec = read_record(root, name)
        if len(found) > 1:
            raise ServiceError(f'{name}: {len(found)} instances run (pids {", ".join(str(i["pid"]) for i in found)}); stop the extra ones by hand')
        if not found:
            if name == 'supervisor':
                assert_hold(root, 'supervisor stop requested (not running)')
            rec.update(running=False, user_stopped=not then_start, stop_requested=None)
            write_record(root, name, rec)
            if then_start:
                status_ = start(root, name, settings)
                new = read_record(root, name)
                return {'ok': True, 'pending': False, 'message': f'{name} was not running; started as pid {new.get("pid")}', 'status': status_,
                        'replacement': {'pid': new.get('pid'), 'create_time': new.get('create_time')}}
            return {'ok': True, 'pending': False, 'message': f'{name} is not running', 'status': status_one(root, svc, settings)}
        inst = found[0]
        if not (rec.get('pid') == inst['pid'] and abs((rec.get('create_time') or 0) - inst['create_time']) < CREATE_TIME_TOLERANCE):
            rec = _fresh_record(inst, True, rec)
        if name == 'convert' and force and convert_job_running(root):
            raise ServiceError('refused: convert_job.py is running; the worker can only stop gracefully after it')
        rec.update(stop_requested=now, then_start=then_start, stop_request_id=request)
        rec.pop('restart_request', None)
        if name == 'supervisor':                 # finding 7: the delivery of the stop is persisted (requested -> delivered | failed)
            rec['stop_delivery'] = {'state': 'requested', 'time': now, 'pid': inst['pid'], 'create_time': inst['create_time']}
        pause_file = batch(root) / 'PAUSE'
        if name == 'supervisor' and then_start and pause_file.exists():
            try:
                rec['pause_backup'] = pause_file.read_text(encoding='utf-8')
            except OSError:
                pass
        write_record(root, name, rec)
        pending, message = False, f'{name} stopped'
        if name == 'supervisor':
            assert_hold(root, 'supervisor ' + ('restart' if then_start else 'stop'), renew=True, now=now, retiring=inst)
            failure = None
            try:
                run = subprocess.run([sys.executable, '-X', 'utf8', '-u', svc.script, 'stop'], cwd=str(root), capture_output=True, text=True,
                                     encoding='utf-8', errors='replace', timeout=60, creationflags=NO_WINDOW)
                if run.returncode != 0:
                    failure = (run.stderr or run.stdout or '').strip()[:300] or f'exit {run.returncode}'
            except (OSError, subprocess.SubprocessError) as error:
                failure = f'{type(error).__name__}: {error}'[:300]
            if failure is not None and not _stop_delivered(root, inst):
                # non-delivery is established: the command failed, no stop file exists and the supervisor still runs. It stays the intended
                # generation: the request and the `retiring` designation are withdrawn, so its ticks release the hold again (finding 7)
                _stop_not_delivered(root, rec, inst, failure, time.time())
                raise ServiceError(f'supervisor stop failed: {failure} (the stop was not delivered: the supervisor keeps running and the '
                                   'conversion hold is released by its next tick)')
            rec['stop_delivery'] = {'state': 'delivered' if _stop_delivered(root, inst) else 'unconfirmed', 'time': time.time(),
                                    'pid': inst['pid'], 'create_time': inst['create_time'], 'note': failure}
            write_record(root, name, rec)
            gone = _wait_gone(root, name, wait, inst)
            pending, message = not gone, ('supervisor stopped' if gone else 'stop requested; the supervisor exits within a few seconds')
        elif name == 'views':
            gone, message = _views_stop_step(root, svc, inst, now)
            if not gone and _wait_gone(root, name, wait, inst):
                gone, message = True, 'view runner stopped (running Codex jobs continue)'
            pending = not gone
            if pending:
                rec = read_record(root, name)
                rec['stop_note'] = message
                write_record(root, name, rec)
        else:
            gone, message = _convert_stop_step(root, svc, inst, rec, now)
            if not gone:
                gone = _wait_gone(root, name, wait, inst)
                if not gone:
                    gone, message = _convert_stop_step(root, svc, inst, rec, time.time())
            if gone:
                message = f'convert worker stopped: {message}' if 'older worker' in message else 'convert worker stopped'
            pending = not gone
            if pending:
                rec = read_record(root, name)
                rec['stop_note'] = message
                write_record(root, name, rec)
        if not pending:
            if name == 'convert':
                _clean_worker_files(root)
            after = read_record(root, name)
            _restore_pause(root, after)
            _finish_stop(root, name, after, time.time())
            if name == 'convert':
                drained = settle_drain(root)      # a legacy drain that just ended: failure counts restored, CONVERT_DRAIN removed (I.10)
                if drained:
                    message += ' (' + '; '.join(drained) + ')'
            if then_start:
                # finding 20: the outcome is terminal only with the replacement identified; start() raises ServiceError when it fails / is blocked
                status_ = start(root, name, settings)
                new = read_record(root, name)
                return {'ok': True, 'pending': False, 'message': f'{message}; restarted as pid {new.get("pid")}', 'status': status_,
                        'replacement': {'pid': new.get('pid'), 'create_time': new.get('create_time')}}
        return {'ok': True, 'pending': pending, 'message': message, 'status': status_one(root, svc, settings)}


def restart(root, name: str, *, wait: float = 15.0, settings: dict | None = None, request=None) -> dict:
    """Stop then start (the supervisor needs this to pick up new code). A pending stop (convert worker mid-job) restarts on its own at exit."""
    return stop(root, name, wait=wait, then_start=True, settings=settings, request=request)


# ------------------------------------------------------------------------------------------------------------------------ tick

def _backoff_or_hold(rec: dict, now: float, reason: str) -> list[str]:
    """Count a failure (crash, exit or failed start); schedule the restart or hold the service. Returns event texts."""
    fails = int(rec.get('fails') or 0) + 1
    rec['fails'] = fails
    if fails >= MAX_FAILS:
        rec.update(held=True, next_restart=None, error=f'restart failed {MAX_FAILS} times; last: {reason}')
        return [f'{rec["name"]}: held after {MAX_FAILS} failures ({reason})']
    delay = BACKOFF[fails - 1]
    rec.update(next_restart=now + delay, error=f'{reason}; restarting in {int(delay)} s')
    return [f'{rec["name"]}: {reason}; restart in {int(delay)} s']


def _last_err_line(root, svc: Service) -> str:
    for line in reversed(tail_lines(log_files(root, svc)['err'], 5)):
        if line.strip():
            return line.strip()[:200]
    return ''


def _idle_exit(root, svc: Service, rec: dict) -> bool:
    """The convert worker left by itself because nothing is staged (its events log says `exiting`, written since we last saw it alive)."""
    if svc.name != 'convert':
        return False
    path = batch(root) / svc.events_log
    try:
        recent = path.stat().st_mtime >= (rec.get('last_seen') or 0) - 5
    except OSError:
        return False
    return recent and any('exiting' in line for line in tail_lines(path, 3))


def has_work(root) -> bool:
    """Work the convert worker would really take now (F20): a listed staged job under MAX_TRIES failures with a free module."""
    return bool(convert_list.eligible_jobs(root))


def _completed(ctx: dict, name: str, request, then_start: bool, text: str, ok: bool = True, replacement: dict | None = None) -> None:
    """Record the end of a pending stop / restart for the tick's `completed` list (phase 5 review row 18): the service request id the stop
    carried (`stop_request_id`), so the manager can give the request its terminal outcome. A restart ends only with its replacement
    identified (`replacement` = {pid, create_time}, ok true) or with the reason it was not started (ok false); finding 20."""
    ctx.setdefault('completed', []).append({'name': name, 'request': request, 'action': 'restart' if then_start else 'stop', 'ok': bool(ok),
                                            'pending': False, 'text': text, 'replacement': replacement})


def _tick_one(root, svc: Service, found: list[dict], settings: dict, now: float, ctx: dict) -> list[str]:
    events: list[str] = []
    name = svc.name
    rec = read_record(root, name)
    if len(found) > 1:
        dup = [{'pid': i['pid'], 'create_time': i['create_time'], 'cmdline': i['cmdline']} for i in found]
        if [d['pid'] for d in dup] != [d['pid'] for d in rec.get('duplicates') or []]:
            events.append(f'{name}: {len(found)} instances run (pids {", ".join(str(i["pid"]) for i in found)}); not touched')
        rec.update(duplicates=dup, last_seen=now)
        write_record(root, name, rec)
        return events
    if found:
        inst = found[0]
        same = rec.get('pid') == inst['pid'] and abs((rec.get('create_time') or 0) - inst['create_time']) < CREATE_TIME_TOLERANCE
        if not same:
            rec = _fresh_record(inst, True, rec)
            events.append(f'{name}: adopted pid {inst["pid"]}')
            if rec.get('user_stopped') or rec.get('held'):
                rec.update(user_stopped=False, held=False, next_restart=None, error=None)   # someone started it by hand: the stop is over
        rec.update(running=True, cmdline=inst['cmdline'], duplicates=[], last_seen=now, exited_at=None, idle_exit=False, blocked=None)
        if now - inst['create_time'] >= STABLE_AFTER and (rec.get('fails') or rec.get('error')):
            rec.update(fails=0, error=None)
        if name == 'convert':
            rec['convert_jobs'] = [j['pid'] for j in convert_job_running(root)]
        if name == 'supervisor' and rec.get('stop_requested') and not supervisor_stop_file(root).exists() \
                and now - float(rec['stop_requested']) > STOP_DELIVERY_GRACE:
            # finding 7 (reconcile): the stop is pending for this very supervisor, but its stop file is gone and it still runs: the request
            # never reached it (or was removed). It is withdrawn; this supervisor stays the intended generation.
            request, then_start = rec.get('stop_request_id'), bool(rec.get('then_start'))
            _stop_not_delivered(root, rec, inst, 'the stop file is gone and the supervisor still runs', now)
            text = (f'supervisor: the stop was not delivered (pid {inst["pid"]} still runs and no stop request is left); the request is '
                    'withdrawn and its ticks release the conversion hold again')
            events.append(text)
            _completed(ctx, name, request, then_start, text, ok=False)
            rec = read_record(root, name)
        stopped = False
        if name in ('convert', 'views') and rec.get('stop_requested'):
            write_record(root, name, rec)
            try:
                if name == 'convert':
                    gone, note = _convert_stop_step(root, svc, inst, rec, now)
                else:
                    gone, note = _views_stop_step(root, svc, inst, now)
            except ServiceError as error:
                gone, note = False, str(error)
            rec = read_record(root, name)
            rec['stop_note'] = note
            if gone:
                text = f'{name}: {note}'
                events.append(text)
                request, then_start = rec.get('stop_request_id'), bool(rec.get('then_start'))
                rec.update(running=False, exited_at=now, stop_requested=None, user_stopped=not then_start, idle_exit=False, drain_hold=False,
                           stop_note=None)
                if name == 'convert':
                    _clean_worker_files(root)
                if then_start:      # finding 20: the restart is terminal only after the start checks below identified the replacement
                    rec['restart_request'] = {'id': request, 'text': text}
                else:
                    _completed(ctx, name, request, False, text)
                stopped = True
        if not stopped:
            write_record(root, name, rec)
            return events
    # nothing runs (any more)
    rec['duplicates'] = []
    if rec.get('running'):
        rec.update(running=False, exited_at=now)
        if rec.get('stop_requested'):
            request, then_start = rec.get('stop_request_id'), bool(rec.get('then_start'))
            rec.update(user_stopped=not then_start, stop_requested=None, idle_exit=False, stop_note=None, drain_hold=False)
            events.append(f'{name}: stopped')
            if then_start:
                rec['restart_request'] = {'id': request, 'text': f'{name}: stopped'}
            else:
                _completed(ctx, name, request, False, f'{name}: stopped')
        elif _idle_exit(root, svc, rec):
            rec.update(idle_exit=True)
            events.append(f'{name}: exited (idle, nothing staged)')
        else:
            last = _last_err_line(root, svc)
            events += _backoff_or_hold(rec, now, 'exited unexpectedly' + (f' ({last})' if last else ''))
    enabled = bool(settings['services'].get(name, True))
    restart_now = bool(rec.get('then_start'))
    pending_restart = rec.get('restart_request') if isinstance(rec.get('restart_request'), dict) else None
    reason = None
    if restart_now:                # an explicit Restart starts the service like Start does (also when it is disabled or was held)
        reason = 'restart'
        rec.update(held=False, user_stopped=False, next_restart=None)
    elif rec.get('held') or rec.get('user_stopped') or not enabled:
        pass
    elif rec.get('idle_exit'):
        if has_work(root):
            reason = 'work appeared'
    elif rec.get('next_restart'):
        if now >= rec['next_restart']:
            reason = 'restart after failure'
    else:
        reason = 'not running'
    blocked = None
    if reason and ctx.get('settings_error') and reason != 'restart':
        blocked = f'not started: {ctx["settings_error"]}'
    elif reason:
        blocked = _start_blocker(root, svc, ctx.get('maybe', {}).get(name))
        if not blocked and name == 'convert' and reason != 'restart' and not ctx.get('supervisor_ready'):
            blocked = 'waiting for the supervisor to run and complete a tick before the convert worker starts'
    if blocked:
        if blocked != rec.get('blocked'):
            events.append(f'{name}: {blocked}')
        rec['blocked'] = blocked
        if pending_restart and reason == 'restart':       # terminal: the replacement was not started (it starts once the blocker is gone)
            rec.pop('restart_request', None)
            _completed(ctx, name, pending_restart.get('id'), True, f'{pending_restart.get("text")}; restart blocked: {blocked}', ok=False)
        write_record(root, name, rec)
        return events
    rec['blocked'] = None
    if reason:
        rec.pop('restart_request', None)
        write_record(root, name, rec)
        started, inst, error = _start(root, svc, rec, settings, now, reason)
        events += started
        if pending_restart and reason == 'restart':
            if inst is not None:
                _completed(ctx, name, pending_restart.get('id'), True, '; '.join([str(pending_restart.get('text'))] + started),
                           replacement={'pid': inst['pid'], 'create_time': inst['create_time']})
            else:
                _completed(ctx, name, pending_restart.get('id'), True, f'{pending_restart.get("text")}; restart failed: {error}', ok=False)
        return events
    write_record(root, name, rec)
    return events


def _start(root, svc: Service, rec: dict, settings: dict, now: float, reason: str) -> tuple[list[str], dict | None, str | None]:
    """Spawn `svc` for the tick (crash restart, missing service, pending restart) and record it: (events, the new instance {pid,
    create_time, cmdline} or None, the error text when it could not start)."""
    _restore_pause(root, rec)
    try:
        inst = _spawn(root, svc, settings)
    except OSError as error:
        events = _backoff_or_hold(rec, now, f'could not start: {error}')
        write_record(root, svc.name, rec)
        return events, None, f'could not start: {error}'
    new = _fresh_record(inst, False, {**rec, 'idle_exit': False, 'next_restart': None})
    new['last_seen'] = now
    write_record(root, svc.name, new)
    return [f'{svc.name}: started pid {inst["pid"]} ({reason})'], inst, None


def tick(root, settings: dict | None = None, now: float | None = None) -> dict:
    """One manager pass (the orchestrator calls it every 30 s on the writer server): adopt, restart crashed services, start missing ones in the
    order supervisor, views, convert (after the supervisor's first tick), drive pending convert / view runner stops, keep / release the
    conversion hold. Never raises for one service's problem. Returns {"services": {name: status}, "events": [text], "completed": [{name,
    request, action, ok, pending: false, text}]} (`completed`: pending stops / restarts that ended in this pass, with the request id they
    carried)."""
    settings = settings or app_settings.load(root)
    health = app_settings.health(root, reload=False)
    now = time.time() if now is None else now
    events: list[str] = []
    with _LOCK:
        found, maybe = scan_full(root)
        sups = found['supervisor']
        sup = sups[0] if len(sups) == 1 else None
        if sup is None:
            try:
                text = assert_hold(root, 'no supervisor runs')
            except OSError as error:
                text = f'conversion hold could not be written: {error}'
            if text:
                events.append(text)
        ctx = {'maybe': maybe, 'supervisor_ready': supervisor_ticked(root, sup, sup['create_time'] if sup else None),
               'settings_error': health.get('error') if health.get('hold_starts') else None}
        for svc in SERVICES.values():
            try:
                events += _tick_one(root, svc, found[svc.name], settings, now, ctx)
            except Exception as error:   # noqa: BLE001
                events.append(f'{svc.name}: manager error {type(error).__name__}: {error}')
        try:
            _clean_worker_files(root)
            if not read_record(root, 'convert').get('stop_requested'):
                (batch(root) / 'convert_worker.stop').unlink(missing_ok=True)   # the old shared marker: never written any more (F6)
            events += settle_drain(root, now)        # a legacy drain that ended in this pass (I.10); nothing while one is pending
            text = release_hold(root, sup)
            if text:
                events.append(text)
        except Exception as error:  # noqa: BLE001
            events.append(f'hold / stop files: manager error {type(error).__name__}: {error}')
    return {'services': {n: status_one(root, s, settings, now) for n, s in SERVICES.items()}, 'events': events,
            'completed': ctx.get('completed', [])}


# ------------------------------------------------------------------------------------------------------------------------ status

def _tripo_differs(svc: Service, settings: dict, cmdline: list | None) -> bool | None:
    """The running convert worker was started with another Tripo policy than the saved setting (the setting changes the NEXT start only)."""
    if svc.name != 'convert' or not cmdline:
        return None
    return ('--no-tripo' in cmdline) != (settings['tripo'] == 'off')


def status_one(root, svc: Service, settings: dict | None = None, now: float | None = None, lines: int = LOG_LINES) -> dict:
    settings = settings or app_settings.load(root)
    now = time.time() if now is None else now
    rec = read_record(root, svc.name)
    live = _alive(rec) and bool(rec.get('running', True))
    dups = rec.get('duplicates') or []
    if dups:
        state, message = 'duplicate', f'{len(dups)} instances run (pids {", ".join(str(d["pid"]) for d in dups)}); stop the extra ones by hand'
    elif live:
        state = 'stopping' if rec.get('stop_requested') else 'running'
        message = (rec.get('stop_note') or 'stop requested') if state == 'stopping' else None
    elif rec.get('held'):
        state, message = 'held', rec.get('error') or f'restart failed {MAX_FAILS} times'
    elif rec.get('user_stopped'):
        state, message = 'stopped', 'stopped on request'
    elif rec.get('blocked'):
        state, message = 'waiting', rec['blocked']
    elif rec.get('idle_exit'):
        state, message = 'idle', 'exited because nothing is staged; restarts when work appears'
    elif rec.get('next_restart'):
        state, message = 'crashed', rec.get('error')
    else:
        state, message = 'stopped', None
    logs = log_files(root, svc)
    running_cmd = rec.get('cmdline') if live else None
    return {
        'name': svc.name,
        'enabled': bool(settings['services'].get(svc.name, True)),
        'state': state, 'message': message,
        'pid': rec.get('pid') if live else None,
        'uptime': round(now - rec['create_time']) if live and rec.get('create_time') else None,
        'adopted': bool(rec.get('adopted')) if live else None,
        'since': iso(rec.get('create_time')) if live else None,
        'error': rec.get('error'),
        'fails': int(rec.get('fails') or 0),
        'restart_in': max(0, round(rec['next_restart'] - now)) if rec.get('next_restart') and not live else None,
        'stop_pending': bool(rec.get('stop_requested')) and live,
        'duplicates': dups,
        'command': svc.command(settings) if svc.name in SERVICES else None,
        'running_command': running_cmd,
        'command_differs': _tripo_differs(svc, settings, running_cmd),
        'convert_jobs': rec.get('convert_jobs') if svc.name == 'convert' else None,
        'logs': {'out_file': logs['out'].name, 'err_file': logs['err'].name,
                 'out': tail_lines(logs['out'], lines), 'err': tail_lines(logs['err'], lines),
                 'events': tail_lines(batch(root) / svc.events_log, lines)},
    }


def status(root, settings: dict | None = None, now: float | None = None, lines: int = LOG_LINES, deep: bool = False) -> dict:
    """Snapshot for the Queue tab from the records the last tick wrote (cheap; each recorded pid is re-verified). `deep=True` adds a live
    scan for the running convert_job.py processes. `hold` = the conversion hold (I.1), `settings` = settings.health."""
    settings = settings or app_settings.load(root)
    now = time.time() if now is None else now
    services = {n: status_one(root, s, settings, now, lines) for n, s in SERVICES.items()}
    jobs = [{'pid': j['pid'], 'since': iso(j['create_time']), 'args': j['cmdline'][2:6]} for j in convert_job_running(root)] if deep else None
    errors = [f'{n}: {s["message"]}' for n, s in services.items() if s['state'] in ('duplicate', 'held')]
    health = app_settings.health(root, reload=False)
    if health.get('error'):
        errors.append(f'settings: {health["error"]}' + ('; no service is started until it is fixed' if health.get('hold_starts')
                                                        else '; the last valid settings are used'))
    return {'services': services, 'convert_jobs': jobs, 'errors': errors, 'hold': hold_state(root), 'drain': drain_state(root),
            'settings': health}
