"""Service manager of the v2 preview server: adopts and manages the three pipeline daemons (DESIGN_PHASE4.md section A).

    supervisor  python -X utf8 -u tools/pipeline_supervisor.py run
    views       python -u tools/view_batch.py run --parallel <settings.view_parallel>
    convert     python -X utf8 -u tools/convert_queue.py --sets-file .codex_jobs/_batch/convert_sets.txt --worker 1 [--no-tripo]

API (every function takes `root`, so tests use a temp root with fake service scripts under `<root>/tools/`):
    tick(root, settings=None, now=None) -> {"services": {name: status}, "events": [...]}   called every 30 s by the orchestrator thread
    status(root, settings=None, now=None, lines=20, deep=False) -> {"services": {...}, "convert_jobs": [...], "errors": [...]}
    start(root, name, settings=None) / stop(root, name, ...) / restart(root, name, ...)    the Queue tab buttons
    convert_job_running(root) / studio_running(root)                                      for credits.check_tripo and Stop-now gating

Discovery is a psutil scan: python processes whose argv holds the service's script (resolved against the process's cwd and compared with
`<root>/tools/<script>`, so a process of another checkout never matches), plus the service's required arguments. A match is ADOPTED (recorded in
`.codex_jobs/_app/services/<name>.json` as `{pid, create_time, cmdline, adopted, since, ...}`); a service is started only when enabled in the
settings and nothing matches. Two matches = state `duplicate`, an error in the UI, never fixed automatically. Services start detached
(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW) with stdout/stderr appended to `.codex_jobs/_batch/<name>_win.out.log/.err.log`,
so stopping or restarting the web server never stops a service.

Crash restart: an enabled service that vanished (without a stop request) is restarted after 60 s, after a second quick failure after 5 min,
after a third it is HELD ("restart failed 3 times") until Start is pressed. A run that stays up for STABLE_AFTER resets the count. The convert
worker's own idle exit (its log says `exiting`) is not a failure: it is restarted when staged, convert-listed jobs exist.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
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
_LOCK = threading.RLock()


class ServiceError(Exception):
    """A request the manager refuses (unknown service, already running, duplicate instances, convert_job running, ...)."""


@dataclass(frozen=True)
class Service:
    name: str
    script: str                       # relative to the repo root, forward slashes
    need_args: tuple = ()             # arguments that must follow the script (`run`)
    interp: str = 'python'            # the first word of the process name
    log_stems: tuple = ()             # <stem>.out.log / <stem>.err.log under _batch; the first is written by the manager, the rest only displayed
    events_log: str = 'events.log'
    stop_file: str | None = None

    @property
    def script_name(self) -> str:
        return self.script.rsplit('/', 1)[-1]

    def command(self, settings: dict) -> list[str]:
        if self.name == 'supervisor':
            return [sys.executable, '-X', 'utf8', '-u', self.script, 'run']
        if self.name == 'views':
            return [sys.executable, '-u', self.script, 'run', '--parallel', str(settings['view_parallel'])]
        if self.name == 'convert':
            cmd = [sys.executable, '-X', 'utf8', '-u', self.script, '--sets-file', '.codex_jobs/_batch/convert_sets.txt', '--worker', '1']
            return cmd + (['--no-tripo'] if settings['tripo'] == 'off' else [])
        raise ServiceError(f'{self.name} cannot be started')


SERVICES = {
    'supervisor': Service('supervisor', 'tools/pipeline_supervisor.py', ('run',), log_stems=('supervisor_win',), stop_file='supervisor.stop'),
    'views': Service('views', 'tools/view_batch.py', ('run',), log_stems=('views_win', 'view_batch_win8', 'view_batch_win')),
    'convert': Service('convert', 'tools/convert_queue.py', (), log_stems=('convert_win', 'convert_worker1_win', 'convert_worker_w1_win'),
                       events_log='convert_events.log', stop_file='convert_worker.stop'),
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


# ------------------------------------------------------------------------------------------------------------------------ discovery

def _real(path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _match(root, svc: Service, cmd: list[str], cwd: str | None) -> bool:
    if not cmd or not Path(cmd[0].replace('\\', '/')).name.lower().startswith(svc.interp):
        return False
    args = cmd[1:]
    index = next((i for i, a in enumerate(args[:8]) if a.replace('\\', '/').rsplit('/', 1)[-1] == svc.script_name), None)
    if index is None:
        return False
    script = Path(args[index])
    if not script.is_absolute():
        if not cwd:
            return False
        script = Path(cwd) / script
    if _real(script) != _real(Path(root) / svc.script):
        return False
    rest = args[index + 1:]
    if any(a not in rest for a in svc.need_args):
        return False
    if svc.name == 'convert' and '--worker' in rest:   # the app manages worker 1 only; worker N > 1 are the user's own
        value = rest[rest.index('--worker') + 1:][:1]
        if value != ['1']:
            return False
    return True


def scan(root, names=None) -> dict:
    """{name: [instance]} for the services (default all three) and probes named; an instance is {pid, ppid, create_time, cmdline}.
    A child that repeats its parent's command line (a launcher shim) is dropped in favour of the child."""
    table = {**SERVICES, **PROBES}
    wanted = [table[n] for n in (names or SERVICES)]
    found: dict[str, list[dict]] = {s.name: [] for s in wanted}
    me = os.getpid()
    for proc in psutil.process_iter(['pid', 'ppid', 'cmdline', 'create_time']):
        info = proc.info
        cmd = info.get('cmdline') or []
        if info['pid'] == me or not cmd:
            continue
        line = ' '.join(cmd[1:8]).replace('\\', '/')
        candidates = [s for s in wanted if s.script_name in line]
        if not candidates:
            continue
        try:
            cwd = proc.cwd()
        except (psutil.Error, OSError):
            cwd = None
        for svc in candidates:
            if _match(root, svc, cmd, cwd):
                found[svc.name].append({'pid': info['pid'], 'ppid': info.get('ppid') or 0, 'create_time': info['create_time'] or 0.0,
                                        'cmdline': list(cmd)})
    for name, items in found.items():
        found[name] = [i for i in items
                       if not any(o['ppid'] == i['pid'] and o['cmdline'] == i['cmdline'] and o['pid'] != i['pid'] for o in items)]
    return found


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
           'duplicates': []}
    for key in ('fails', 'error', 'user_stopped', 'held', 'next_restart', 'idle_exit'):
        if old and key in old:
            rec[key] = old[key]
    return rec


def start(root, name: str, settings: dict | None = None) -> dict:
    """The Start button: start `name` now (also when it is disabled in the settings, or held). Refuses when an instance already runs.
    Clears held / stopped / failure state. Returns the new status."""
    svc = _service(name)
    settings = settings or app_settings.load(root)
    with _LOCK:
        found = scan(root, [name])[name]
        if found:
            raise ServiceError(f'{name} is already running (pid {", ".join(str(i["pid"]) for i in found)})')
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


def _terminate(root, svc: Service, inst: dict, timeout: float = 10.0) -> None:
    """Stop one process by PID after re-checking that it still is the matched service instance (same create time and command line)."""
    again = [i for i in scan(root, [svc.name])[svc.name] if i['pid'] == inst['pid'] and abs(i['create_time'] - inst['create_time']) < CREATE_TIME_TOLERANCE]
    if not again:
        raise ServiceError(f'process {inst["pid"]} is no longer the {svc.name} service; nothing stopped')
    proc = psutil.Process(inst['pid'])
    proc.terminate()
    try:
        proc.wait(timeout)
    except psutil.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(5)
        except psutil.TimeoutExpired:
            pass
    except psutil.Error:
        pass


def _wait_gone(root, name: str, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while True:
        if not scan(root, [name])[name]:
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.2)


def _restore_pause(root, rec: dict) -> None:
    """A restarting supervisor deletes the PAUSE file on its way out (`release_all`); until the new one rewrites it (its first tick) the
    convert worker would see "not paused" and could start a conversion. Put the old PAUSE file back at once."""
    backup = rec.pop('pause_backup', None)
    path = batch(root) / 'PAUSE'
    if backup is not None and not path.exists():
        guard.write_atomic(path, backup.encode('utf-8'))


def _finish_stop(root, name: str, rec: dict, now: float) -> None:
    """The process is gone after a stop request: record a deliberate stop (or the pending restart)."""
    rec.update(running=False, exited_at=now, stop_requested=None, user_stopped=not rec.get('then_start'), idle_exit=False)
    rec['duplicates'] = []
    write_record(root, name, rec)


def stop(root, name: str, *, wait: float = 5.0, force: bool = False, then_start: bool = False, settings: dict | None = None) -> dict:
    """The Stop button (restart = then_start). Returns {"ok", "pending", "message", "status"}:
      supervisor: `python tools/pipeline_supervisor.py stop` (its own stop file; it resumes frozen processes on exit)
      views:      by PID after re-checking command line and create time; running Codex jobs continue (the next runner re-adopts them)
      convert:    graceful only while a convert_job.py runs: writes convert_worker.stop and returns pending (the worker exits before its next job).
                  With no convert_job.py running it also writes the file, waits up to `wait` s, then (an adopted old worker without the stop-file
                  code) stops it by PID. `force=True` skips the wait; it is refused while a convert_job.py runs.
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
            rec.update(running=False, user_stopped=not then_start, stop_requested=None)
            write_record(root, name, rec)
            if then_start:
                return {'ok': True, 'pending': False, 'message': f'{name} was not running; started', 'status': start(root, name, settings)}
            return {'ok': True, 'pending': False, 'message': f'{name} is not running', 'status': status_one(root, svc, settings)}
        inst = found[0]
        if not (rec.get('pid') == inst['pid'] and abs((rec.get('create_time') or 0) - inst['create_time']) < CREATE_TIME_TOLERANCE):
            rec = _fresh_record(inst, True, rec)
        rec.update(stop_requested=now, then_start=then_start)
        pause_file = batch(root) / 'PAUSE'
        if name == 'supervisor' and then_start and pause_file.exists():
            try:
                rec['pause_backup'] = pause_file.read_text(encoding='utf-8')
            except OSError:
                pass
        write_record(root, name, rec)
        pending, message = False, f'{name} stopped'
        if name == 'supervisor':
            run = subprocess.run([sys.executable, '-X', 'utf8', '-u', svc.script, 'stop'], cwd=str(root), capture_output=True, text=True,
                                 encoding='utf-8', errors='replace', timeout=60, creationflags=NO_WINDOW)
            if run.returncode != 0:
                rec.update(stop_requested=None, then_start=False)
                write_record(root, name, rec)
                raise ServiceError(f'supervisor stop failed: {(run.stderr or run.stdout).strip()[:300]}')
            gone = _wait_gone(root, name, wait)
            pending, message = not gone, ('supervisor stopped' if gone else 'stop requested; the supervisor exits within a few seconds')
        elif name == 'views':
            _terminate(root, svc, inst)
            gone = _wait_gone(root, name, wait)
            pending, message = not gone, ('view runner stopped (running Codex jobs continue)' if gone else 'view runner is still exiting')
        else:
            jobs = convert_job_running(root)
            if jobs:
                if force:
                    rec.update(stop_requested=None, then_start=False)
                    write_record(root, name, rec)
                    raise ServiceError(f'refused: convert_job.py is running (pid {jobs[0]["pid"]}); the worker can only stop gracefully after it')
                guard.write_atomic(batch(root) / svc.stop_file, f'{iso(now)}\n'.encode('utf-8'))
                pending, message = True, 'stop requested; the worker exits after the conversion in flight'
            else:
                guard.write_atomic(batch(root) / svc.stop_file, f'{iso(now)}\n'.encode('utf-8'))
                gone = _wait_gone(root, name, 0 if force else wait)
                message = 'convert worker stopped'
                if not gone:
                    if convert_job_running(root):   # a job started meanwhile: back to the graceful path
                        pending, message = True, 'a conversion just started; the worker exits after it'
                    else:
                        _terminate(root, svc, inst)
                        gone = _wait_gone(root, name, 5.0)
                        pending, message = not gone, 'convert worker stopped by PID (it does not read the stop file)'
                if gone:
                    (batch(root) / svc.stop_file).unlink(missing_ok=True)
        if not pending:
            after = read_record(root, name)
            _restore_pause(root, after)
            _finish_stop(root, name, after, time.time())
            if then_start:
                return {'ok': True, 'pending': False, 'message': message + '; restarting', 'status': start(root, name, settings)}
        return {'ok': True, 'pending': pending, 'message': message, 'status': status_one(root, svc, settings)}


def restart(root, name: str, *, wait: float = 15.0, settings: dict | None = None) -> dict:
    """Stop then start (the supervisor needs this to pick up new code). A pending stop (convert worker mid-job) restarts on its own at exit."""
    return stop(root, name, wait=wait, then_start=True, settings=settings)


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
    return bool(convert_list.pending_jobs(root))


def _tick_one(root, svc: Service, found: list[dict], settings: dict, now: float) -> list[str]:
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
        rec.update(running=True, cmdline=inst['cmdline'], duplicates=[], last_seen=now, exited_at=None, idle_exit=False)
        if now - inst['create_time'] >= STABLE_AFTER and (rec.get('fails') or rec.get('error')):
            rec.update(fails=0, error=None)
        if name == 'convert':
            rec['convert_jobs'] = [j['pid'] for j in convert_job_running(root)]
        write_record(root, name, rec)
        return events
    # nothing runs
    rec['duplicates'] = []
    if rec.get('running'):
        rec.update(running=False, exited_at=now)
        if rec.get('stop_requested'):
            rec.update(user_stopped=not rec.get('then_start'), stop_requested=None, idle_exit=False)
            events.append(f'{name}: stopped')
        elif _idle_exit(root, svc, rec):
            rec.update(idle_exit=True)
            events.append(f'{name}: exited (idle, nothing staged)')
        else:
            last = _last_err_line(root, svc)
            events += _backoff_or_hold(rec, now, 'exited unexpectedly' + (f' ({last})' if last else ''))
    if svc.stop_file and (batch(root) / svc.stop_file).exists() and not rec.get('stop_requested'):
        (batch(root) / svc.stop_file).unlink(missing_ok=True)   # a stop file nobody will read would stop the next worker
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
    if reason:
        _restore_pause(root, rec)
        try:
            inst = _spawn(root, svc, settings)
        except OSError as error:
            events += _backoff_or_hold(rec, now, f'could not start: {error}')
        else:
            rec = _fresh_record(inst, False, {**rec, 'idle_exit': False, 'next_restart': None})
            rec['last_seen'] = now
            events.append(f'{name}: started pid {inst["pid"]} ({reason})')
    write_record(root, name, rec)
    return events


def tick(root, settings: dict | None = None, now: float | None = None) -> dict:
    """One manager pass (the orchestrator calls it every 30 s on the writer server): adopt, restart crashed services, start missing ones.
    Never raises for one service's problem. Returns {"services": {name: status}, "events": [text]}."""
    settings = settings or app_settings.load(root)
    now = time.time() if now is None else now
    events: list[str] = []
    with _LOCK:
        found = scan(root)
        for svc in SERVICES.values():
            try:
                events += _tick_one(root, svc, found[svc.name], settings, now)
            except Exception as error:   # noqa: BLE001
                events.append(f'{svc.name}: manager error {type(error).__name__}: {error}')
    return {'services': {n: status_one(root, s, settings, now) for n, s in SERVICES.items()}, 'events': events}


# ------------------------------------------------------------------------------------------------------------------------ status

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
        message = 'stop requested' if state == 'stopping' else None
    elif rec.get('held'):
        state, message = 'held', rec.get('error') or f'restart failed {MAX_FAILS} times'
    elif rec.get('user_stopped'):
        state, message = 'stopped', 'stopped on request'
    elif rec.get('idle_exit'):
        state, message = 'idle', 'exited because nothing is staged; restarts when work appears'
    elif rec.get('next_restart'):
        state, message = 'crashed', rec.get('error')
    else:
        state, message = 'stopped', None
    logs = log_files(root, svc)
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
        'convert_jobs': rec.get('convert_jobs') if svc.name == 'convert' else None,
        'logs': {'out_file': logs['out'].name, 'err_file': logs['err'].name,
                 'out': tail_lines(logs['out'], lines), 'err': tail_lines(logs['err'], lines),
                 'events': tail_lines(batch(root) / svc.events_log, lines)},
    }


def status(root, settings: dict | None = None, now: float | None = None, lines: int = LOG_LINES, deep: bool = False) -> dict:
    """Snapshot for the Queue tab from the records the last tick wrote (cheap; each recorded pid is re-verified). `deep=True` adds a live
    scan for the running convert_job.py processes."""
    settings = settings or app_settings.load(root)
    now = time.time() if now is None else now
    services = {n: status_one(root, s, settings, now, lines) for n, s in SERVICES.items()}
    jobs = [{'pid': j['pid'], 'since': iso(j['create_time']), 'args': j['cmdline'][2:6]} for j in convert_job_running(root)] if deep else None
    errors = [f'{n}: {s["message"]}' for n, s in services.items() if s['state'] in ('duplicate', 'held')]
    return {'services': services, 'convert_jobs': jobs, 'errors': errors}
