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
by begin_management, before the supervisor is stopped or restarted, and whenever no supervisor runs; removed by the tick only when it is ours
and the running supervisor wrote `supervisor_tick.json` (its pid, a time after the hold's since). A hold of a dead owner older than
STALE_HOLD_S is replaced by ours; a hold of a live other owner is never touched.

Convert worker stop (I.2): `.codex_jobs/_batch/convert_worker1.stop` = {pid, create_time, request, time} naming the worker process; the worker
acknowledges with `convert_worker1.stopping`, finishes its job (finish + offload) and exits. The manager waits and NEVER kills on a timeout:
the stop stays pending and every tick re-evaluates it. A LEGACY worker (it started before the current convert_queue.py was written, or that
script has no stop support) is stopped by its verified handle only when no convert_job.py of this checkout runs and its last event in
convert_events.log (since it started) is PAUSED / IDLE / LOW DISK, after its last CONVERT: it is sleeping.

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


def write_hold(root, reason: str, now: float | None = None) -> dict:
    """Write OUR conversion hold (atomic): {since, since_unix, reason, owner_pid, owner_create_time}."""
    now = time.time() if now is None else now
    hold = {'since': datetime.fromtimestamp(now).isoformat(timespec='milliseconds'), 'since_unix': now, 'reason': reason,
            'owner_pid': os.getpid(), 'owner_create_time': _my_create_time()}
    guard.write_atomic(hold_path(root), (json.dumps(hold, indent=2) + '\n').encode('utf-8'))
    return hold


def _foreign_live(hold: dict | None) -> bool:
    return bool(hold) and not _hold_is_mine(hold) and _process_alive(hold.get('owner_pid'), hold.get('owner_create_time'))


def assert_hold(root, reason: str, *, renew: bool = False, now: float | None = None) -> str | None:
    """Make sure a conversion hold exists. `renew` (a supervisor stop / restart, a manager start) writes ours with a new `since` unless a LIVE
    other owner holds it. Without `renew` an existing hold stays as it is, except a stale one (dead owner, older than STALE_HOLD_S), which is
    replaced by ours. Returns an event text or None."""
    now = time.time() if now is None else now
    hold = read_hold(root)
    if hold is None:
        write_hold(root, reason, now)
        return f'conversion hold written ({reason})'
    if _foreign_live(hold):
        return None
    if renew:
        write_hold(root, reason, now)
        return f'conversion hold renewed ({reason})'
    if not _hold_is_mine(hold):
        since = _hold_time(hold)
        if since is None or now - since > STALE_HOLD_S:
            write_hold(root, reason, now)
            return f'stale conversion hold of pid {hold.get("owner_pid")} replaced ({reason})'
    return None


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


def release_hold(root, sup: dict | None) -> str | None:
    """Remove the hold when it is ours (or stale) and the running supervisor ticked after its `since` (I.1). Returns an event text or None."""
    hold = read_hold(root)
    if hold is None or _foreign_live(hold):
        return None
    if not _hold_is_mine(hold):
        since = _hold_time(hold)
        if since is None or time.time() - since <= STALE_HOLD_S:
            return None                        # a recent hold of a process that just exited: it may still be part of a by-hand restart
    if supervisor_ticked(root, sup, _hold_time(hold)):
        hold_path(root).unlink(missing_ok=True)
        return f'conversion hold released (supervisor pid {sup["pid"]} completed a tick)'
    return None


def begin_management(root) -> list[str]:
    """The services thread's first step (I.1): our conversion hold exists before any convert worker is started or adopted. A hold of a dead
    owner is replaced (whatever its age: ours keeps the hold continuous), a live other owner's hold is left alone."""
    with _LOCK:
        text = assert_hold(root, 'preview app manager start', renew=True)
    return [text] if text else []


def hold_state(root) -> dict | None:
    """`hold` of the status: the file plus who owns it and what removes it."""
    hold = read_hold(root)
    if hold is None:
        return None
    owner = 'this server' if _hold_is_mine(hold) else ('another live process' if _foreign_live(hold) else 'a process that exited')
    return {**{k: hold.get(k) for k in ('since', 'reason', 'owner_pid')}, 'owner': owner,
            'message': 'conversions wait until the running supervisor completes a tick (needs the supervisor service)'}


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
    rec.update(running=False, exited_at=now, stop_requested=None, user_stopped=not rec.get('then_start'), idle_exit=False, stop_note=None)
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


def _event_time(line: str, now: float) -> float | None:
    """The time of a convert_events.log line (`MM-DD HH:MM:SS ...`, the current year; a date in the future belongs to the previous year)."""
    try:
        stamp = datetime.strptime(f'{datetime.fromtimestamp(now).year}-{line[:14]}', '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None
    if stamp.timestamp() > now + 86400:
        stamp = stamp.replace(year=stamp.year - 1)
    return stamp.timestamp()


def worker_sleeping(root, inst: dict, now: float | None = None, worker: int = MANAGED_WORKER) -> bool:
    """The legacy-stop rule of I.2: the worker's last event (since it started) is PAUSED / IDLE / LOW DISK, after its last CONVERT, and it is
    not an exit announcement (an exiting worker runs finish / offload)."""
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
            return False                          # nothing from this worker since it started (or an unreadable line): not proven asleep
        return body.startswith(SLEEP_EVENTS) and 'exiting' not in body
    return False


def _convert_stop_step(root, svc: Service, inst: dict, rec: dict, now: float) -> tuple[bool, str]:
    """One step of a pending convert stop: (gone, message). Writes / renews the stop file naming the process, stops a sleeping legacy worker
    through its verified handle, never kills anything else."""
    if not _names(worker_stop_path(root), inst):
        write_worker_stop(root, inst, rec.get('stop_request_id') or iso(rec.get('stop_requested')), now)
    if _names(worker_stopping_path(root), inst):
        return False, 'the worker acknowledged the stop; it exits after its current job (finish and offload included)'
    if legacy_worker(root, inst):
        jobs = convert_job_running(root)
        if jobs:
            return False, (f'an older worker without the stop file: it is stopped only while it sleeps; convert_job.py (pid {jobs[0]["pid"]}) '
                           'runs now')
        if not worker_sleeping(root, inst, now):
            return False, 'an older worker without the stop file: waiting until its event log says it sleeps (PAUSED / IDLE / LOW DISK)'
        if convert_job_running(root) or not worker_sleeping(root, inst, time.time()):      # (re-checked right before the signal)
            return False, 'an older worker: it woke up; waiting'
        gone = _terminate(root, svc, inst, timeout=10.0, allow_kill=False)
        return gone, 'the older worker was asleep: stopped by its verified PID' if gone else 'the older worker did not exit yet'
    return False, 'stop requested; waiting for the worker to acknowledge it (convert_worker1.stopping)'


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
                return {'ok': True, 'pending': False, 'message': f'{name} was not running; started', 'status': start(root, name, settings)}
            return {'ok': True, 'pending': False, 'message': f'{name} is not running', 'status': status_one(root, svc, settings)}
        inst = found[0]
        if not (rec.get('pid') == inst['pid'] and abs((rec.get('create_time') or 0) - inst['create_time']) < CREATE_TIME_TOLERANCE):
            rec = _fresh_record(inst, True, rec)
        if name == 'convert' and force and convert_job_running(root):
            raise ServiceError('refused: convert_job.py is running; the worker can only stop gracefully after it')
        rec.update(stop_requested=now, then_start=then_start, stop_request_id=request)
        pause_file = batch(root) / 'PAUSE'
        if name == 'supervisor' and then_start and pause_file.exists():
            try:
                rec['pause_backup'] = pause_file.read_text(encoding='utf-8')
            except OSError:
                pass
        write_record(root, name, rec)
        pending, message = False, f'{name} stopped'
        if name == 'supervisor':
            assert_hold(root, 'supervisor ' + ('restart' if then_start else 'stop'), renew=True, now=now)
            run = subprocess.run([sys.executable, '-X', 'utf8', '-u', svc.script, 'stop'], cwd=str(root), capture_output=True, text=True,
                                 encoding='utf-8', errors='replace', timeout=60, creationflags=NO_WINDOW)
            if run.returncode != 0:
                rec.update(stop_requested=None, then_start=False)
                write_record(root, name, rec)
                raise ServiceError(f'supervisor stop failed: {(run.stderr or run.stdout).strip()[:300]}')
            gone = _wait_gone(root, name, wait, inst)
            pending, message = not gone, ('supervisor stopped' if gone else 'stop requested; the supervisor exits within a few seconds')
        elif name == 'views':
            gone = _terminate(root, svc, inst)
            gone = gone or _wait_gone(root, name, wait, inst)
            pending, message = not gone, ('view runner stopped (running Codex jobs continue)' if gone else 'view runner is still exiting')
        else:
            gone, message = _convert_stop_step(root, svc, inst, rec, now)
            if not gone:
                gone = _wait_gone(root, name, wait, inst)
                if not gone:
                    gone, message = _convert_stop_step(root, svc, inst, rec, time.time())
            if gone:
                message = 'convert worker stopped'
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
            if then_start:
                return {'ok': True, 'pending': False, 'message': message + '; restarting', 'status': start(root, name, settings)}
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
            if rec.get('stop_requested'):
                write_record(root, name, rec)
                try:
                    gone, note = _convert_stop_step(root, svc, inst, rec, now)
                except ServiceError as error:
                    gone, note = False, str(error)
                rec = read_record(root, name)
                rec['stop_note'] = note
                if gone:
                    events.append(f'{name}: {note}')
                    rec.update(running=False, exited_at=now, stop_requested=None, user_stopped=not rec.get('then_start'), idle_exit=False)
                    _clean_worker_files(root)
                    if rec.get('then_start'):
                        events += _start(root, svc, rec, settings, now, 'restart')
                        rec = read_record(root, name)
        write_record(root, name, rec)
        return events
    # nothing runs
    rec['duplicates'] = []
    if rec.get('running'):
        rec.update(running=False, exited_at=now)
        if rec.get('stop_requested'):
            rec.update(user_stopped=not rec.get('then_start'), stop_requested=None, idle_exit=False, stop_note=None)
            events.append(f'{name}: stopped')
        elif _idle_exit(root, svc, rec):
            rec.update(idle_exit=True)
            events.append(f'{name}: exited (idle, nothing staged)')
        else:
            last = _last_err_line(root, svc)
            events += _backoff_or_hold(rec, now, 'exited unexpectedly' + (f' ({last})' if last else ''))
    enabled = bool(settings['services'].get(name, True))
    restart_now = bool(rec.get('then_start'))
    reason = None
    if rec.get('held') or rec.get('user_stopped') or not enabled:
        pass
    elif restart_now:
        reason = 'restart'
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
        write_record(root, name, rec)
        return events
    rec['blocked'] = None
    if reason:
        write_record(root, name, rec)
        events += _start(root, svc, rec, settings, now, reason)
        return events
    write_record(root, name, rec)
    return events


def _start(root, svc: Service, rec: dict, settings: dict, now: float, reason: str) -> list[str]:
    """Spawn `svc` for the tick (crash restart, missing service, pending restart) and record it."""
    _restore_pause(root, rec)
    try:
        inst = _spawn(root, svc, settings)
    except OSError as error:
        events = _backoff_or_hold(rec, now, f'could not start: {error}')
        write_record(root, svc.name, rec)
        return events
    new = _fresh_record(inst, False, {**rec, 'idle_exit': False, 'next_restart': None})
    new['last_seen'] = now
    write_record(root, svc.name, new)
    return [f'{svc.name}: started pid {inst["pid"]} ({reason})']


def tick(root, settings: dict | None = None, now: float | None = None) -> dict:
    """One manager pass (the orchestrator calls it every 30 s on the writer server): adopt, restart crashed services, start missing ones in the
    order supervisor, views, convert (after the supervisor's first tick), drive pending convert stops, keep / release the conversion hold.
    Never raises for one service's problem. Returns {"services": {name: status}, "events": [text]}."""
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
            text = release_hold(root, sup)
            if text:
                events.append(text)
        except Exception as error:  # noqa: BLE001
            events.append(f'hold / stop files: manager error {type(error).__name__}: {error}')
    return {'services': {n: status_one(root, s, settings, now) for n, s in SERVICES.items()}, 'events': events}


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
    return {'services': services, 'convert_jobs': jobs, 'errors': errors, 'hold': hold_state(root), 'settings': health}
