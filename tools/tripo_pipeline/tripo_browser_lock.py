"""Cross-process locks on the Tripo Studio browser profile (stdlib only; DESIGN_PHASE4 I.3, review_v2_phase4 finding 8).

`tools/tripo_pipeline/studio.mjs` drives ONE persistent Playwright Firefox profile, `$XDG_DATA_HOME/tripo-studio-playwright-firefox`
(else `~/.local/share/...`), shared by EVERY checkout of this project and by direct manual runs. Two locks cover it:

- The checkout lock `<root>/.codex_jobs/_batch/browser_tripo.lock`, which every python caller of studio.mjs in this checkout holds
  around each call (convert_job.py's submit / collect / retry, convert_queue.tripo_balance, the preview app's balance check
  v2 credits.check_tripo, which answers `busy` at once). Kept so those callers keep seeing each other as before.
- The profile reservation (phase 5 review, phase-4 finding 8), keyed by the RESOLVED profile path, not by checkout: the lock file
  `<profile parent>/<profile name>.imageto3d.lock` beside the profile. `tripo_browser_lock(root)` takes it after the checkout lock
  and yields the environment for the studio.mjs child: {IMAGETO3D_TRIPO_PROFILE_LOCK: <lock path>, IMAGETO3D_TRIPO_PROFILE_TOKEN:
  <random token written into the lock file>}.

studio.mjs refuses to open the profile without a reservation it has verified (`verify`: the lock path belongs to the profile it is
about to open, the token is the holder's and the lock is held). Started without one (a direct manual invocation, the preview app's
balance check, an older convert worker), it re-runs itself under this module's `run` command, which takes the reservation, so
every entry point is covered:

    python tools/tripo_pipeline/tripo_browser_lock.py run --profile <dir> [--wait S] -- node tools/tripo_pipeline/studio.mjs ...
    python tools/tripo_pipeline/tripo_browser_lock.py verify --profile <dir> --lock <path> --token <token> [--register-pid N]
                                                                                                         (exit 0 = valid)
    python tools/tripo_pipeline/tripo_browser_lock.py users --profile <dir>     (exit 1 + lines: an orphan still uses it)

A manual run is simply `node tools/tripo_pipeline/studio.mjs balance` (it reserves the profile itself, waiting up to
$IMAGETO3D_TRIPO_PROFILE_WAIT seconds, default 600, then exits 75 "Tripo browser busy").

Browser lifetime (recheck-before-cutover finding 18): the reservation is a byte lock of the python HOLDER, which spawns node and
waits for it; when the holder dies the kernel frees the lock although node / Firefox may still have the profile open. So
studio.mjs registers itself right after `verify` (`verify --register-pid <node pid>` writes `<profile parent>/<profile
name>.imageto3d.pid` = {pid, create_time, registered}; it prints the path) and removes that file only after its command ended
normally (the browser context closed). Every acquirer checks it after taking the byte lock (`profile_users`): a registered node
that still runs (same pid AND create time), or, when it is gone, any Firefox process started with `-profile <this profile>`,
keeps the profile BUSY until it exits (polled like a held lock: `wait` / TripoBrowserBusy, exit 75 from `run`). A pid file whose
process is gone and that no Firefox uses is stale and is removed. Process facts come from psutil (always present on Windows:
portable.py needs it), else /proc on Linux; where neither exists a pid file cannot be checked and counts as busy (fail closed).

Both are the OS byte lock of claim_lock.py (claim_lock.try_lock: msvcrt.locking of the byte at 1<<20 on Windows, fcntl.flock
elsewhere), freed by the kernel when the holder dies; the files are never deleted. Lock order is always checkout lock, then
profile reservation (`run` takes only the reservation), so no two holders wait on each other. The holder writes `pid <pid> since
<time> ...` at the start of each file (informational, plus the token in the profile file).
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time


def _claim_lock():
    """claim_lock.py beside this file, loaded by path so importing this module never puts tools/tripo_pipeline on sys.path
    (its workflow.py / palette.py would shadow other folders' modules)."""
    spec = importlib.util.spec_from_file_location('_tripo_claim_lock', Path(__file__).resolve().parent / 'claim_lock.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_claim = _claim_lock()
try_lock, release_lock = _claim.try_lock, _claim.release_lock

LOCK_NAME = Path('.codex_jobs') / '_batch' / 'browser_tripo.lock'
POLL_S = 1.0
PROFILE_NAME = 'tripo-studio-playwright-firefox'   # as studio.mjs
PROFILE_LOCK_ENV = 'IMAGETO3D_TRIPO_PROFILE_LOCK'
PROFILE_TOKEN_ENV = 'IMAGETO3D_TRIPO_PROFILE_TOKEN'
PROFILE_WAIT_ENV = 'IMAGETO3D_TRIPO_PROFILE_WAIT'  # `run`: seconds to wait for a busy reservation (default PROFILE_WAIT_S)
PROFILE_WAIT_S = 600
EXIT_BUSY = 75                                     # `run`: the reservation stayed busy
BROWSER_NAMES = ('firefox',)                       # process-name prefixes of a browser that may still hold the profile
CREATE_TOLERANCE_S = 1.0                           # a registered pid is the same process when its create time matches this closely


class TripoBrowserBusy(Exception):
    """Another process holds the Tripo browser lock or the profile reservation (raised after `wait` seconds)."""


def lock_path(root) -> Path:
    return Path(root) / LOCK_NAME


def holder(root) -> str | None:
    return _first_line(lock_path(root))


def _first_line(path) -> str | None:
    try:
        return Path(path).read_text(encoding='utf-8', errors='replace').strip().splitlines()[0][:160]
    except (OSError, IndexError):
        return None


def default_profile(environ=None) -> Path:
    """The profile studio.mjs opens: path.join(process.env.XDG_DATA_HOME ?? path.join(os.homedir(), '.local', 'share'), NAME)."""
    env = os.environ if environ is None else environ
    base = env.get('XDG_DATA_HOME')
    if base is None:
        base = os.path.join(os.path.expanduser('~'), '.local', 'share')
    return Path(os.path.join(base, PROFILE_NAME))


def resolved_profile(profile=None) -> Path:
    """The canonical path of a profile directory (symlinks / junctions / case resolved where the OS can)."""
    return Path(os.path.realpath(os.path.abspath(str(profile if profile is not None else default_profile()))))


def profile_lock_path(profile=None) -> Path:
    """The reservation file of a profile: `<parent>/<name>.imageto3d.lock` of its RESOLVED path (one per profile, any checkout)."""
    real = resolved_profile(profile)
    return real.parent / f'{real.name}.imageto3d.lock'


def _same_path(a, b) -> bool:
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def browser_pid_path(profile=None) -> Path:
    """Where studio.mjs registers the node process that opens `profile`: `<parent>/<name>.imageto3d.pid` of its RESOLVED path."""
    real = resolved_profile(profile)
    return real.parent / f'{real.name}.imageto3d.pid'


def _psutil():
    try:
        import psutil
        return psutil
    except ImportError:
        return None


def _create_time(pid):
    """The process's start identity (psutil create_time; else Linux /proc starttime ticks); None when the process is gone;
    'unknown' when this python cannot tell (no psutil, no /proc)."""
    psutil = _psutil()
    if psutil is not None:
        try:
            return float(psutil.Process(int(pid)).create_time())
        except (psutil.NoSuchProcess, ValueError):
            return None
        except psutil.Error:               # access denied: it exists, its start time is not ours to read
            return 'unknown'
    stat = Path(f'/proc/{int(pid)}/stat')
    if not Path('/proc/self/stat').exists():
        return 'unknown'
    try:
        return float(stat.read_text().rsplit(')', 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _processes():
    """[(pid, name, [args])] of the running processes, or None when they cannot be listed here."""
    psutil = _psutil()
    if psutil is not None:
        out = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            out.append((proc.info['pid'], proc.info['name'] or '', list(proc.info['cmdline'] or ())))
        return out
    if not Path('/proc/self/stat').exists():
        return None
    out = []
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                args = [a for a in (entry / 'cmdline').read_bytes().decode('utf-8', 'replace').split('\0') if a]
            except OSError:
                continue
            out.append((int(entry.name), os.path.basename(args[0]) if args else '', args))
    return out


def register_browser(profile, pid) -> Path:
    """Record that process `pid` (studio.mjs's node) is about to open `profile`; atomic. Returns the pid file."""
    path = browser_pid_path(profile)
    created = _create_time(pid)
    if created is None:
        raise ValueError(f'no process {pid}')
    temp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    temp.write_text(json.dumps({'pid': int(pid), 'create_time': None if created == 'unknown' else created,
                                'registered': time.strftime('%Y-%m-%dT%H:%M:%S'), 'profile': str(resolved_profile(profile))}) + '\n',
                    encoding='utf-8')
    os.replace(temp, path)
    return path


def unregister_browser(profile, pid=None) -> bool:
    """Remove the pid file of `profile` (only when it names `pid`, if given). True when a file was removed."""
    path = browser_pid_path(profile)
    if pid is not None:
        try:
            if json.loads(path.read_text(encoding='utf-8')).get('pid') != int(pid):
                return False
        except (OSError, ValueError, AttributeError):
            return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def profile_users(profile=None) -> list:
    """Who still has `profile` open although no holder keeps its reservation (finding 18), as texts; [] = nobody. Looked at only
    when a pid file exists (studio.mjs removes it after a normal end): the registered node when it still runs (same pid and
    create time), else every browser process (BROWSER_NAMES) started with `-profile <this profile>`. A stale pid file (its
    process gone, no such browser) is removed. A pid file that cannot be read or checked here counts as a user (fail closed)."""
    path = browser_pid_path(profile)
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return []
    except OSError as error:
        return [f'unreadable {path.name} ({type(error).__name__})']
    try:
        data = json.loads(text)
        pid = int(data['pid'])
        recorded = data.get('create_time')
    except (ValueError, KeyError, TypeError):
        data, pid, recorded = None, None, None      # half-written or damaged: fall through to the browser scan
    if pid is not None:
        created = _create_time(pid)
        if created == 'unknown':
            return [f'node pid {pid} (registered in {path.name}; this python cannot check it: install psutil or delete the file '
                    f'once no node / Firefox uses the profile)']
        if created is not None and (not isinstance(recorded, (int, float)) or abs(created - recorded) <= CREATE_TOLERANCE_S):
            return [f'node pid {pid} left by a holder that is gone']
    processes = _processes()
    if processes is None:
        return [f'{path.name} exists and the processes cannot be listed here']
    real = resolved_profile(profile)
    users = []
    for other, name, args in processes:
        if not name.lower().startswith(BROWSER_NAMES):
            continue
        if any(flag in ('-profile', '--profile') and _same_path(value, real) for flag, value in zip(args, args[1:])):
            users.append(f'{name} pid {other} still on the profile')
    if not users:
        try:
            if path.read_text(encoding='utf-8') == text:      # not re-registered meanwhile
                path.unlink()
        except OSError:
            pass
    return users


def _drain_users(profile, deadline, announce):
    """After the byte lock is ours: wait until no orphaned node / Firefox uses the profile; TripoBrowserBusy past `deadline`."""
    announced = False
    while True:
        users = profile_users(profile)
        if not users:
            return
        text = f'Tripo browser busy (profile still in use: {"; ".join(users)})'
        if deadline is not None and time.monotonic() >= deadline:
            raise TripoBrowserBusy(text)
        if not announced:
            (announce or (lambda t: print(t, file=sys.stderr, flush=True)))(f'{text}; waiting')
            announced = True
        time.sleep(POLL_S if deadline is None else min(POLL_S, max(0.05, deadline - time.monotonic())))


def _open(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o666), 'r+b')


def _acquire(handle, deadline, describe, announce):
    """Poll try_lock until it succeeds; TripoBrowserBusy past `deadline` (None = forever). `describe()` names the holder."""
    announced = False
    while not try_lock(handle):
        if deadline is not None and time.monotonic() >= deadline:
            raise TripoBrowserBusy(f'Tripo browser busy (held by {describe() or "another process"})')
        if not announced:
            text = f'Tripo browser busy (held by {describe() or "another process"}); waiting'
            (announce or (lambda t: print(t, file=sys.stderr, flush=True)))(text)
            announced = True
        time.sleep(POLL_S if deadline is None else min(POLL_S, max(0.05, deadline - time.monotonic())))


def _write(handle, text):
    handle.seek(0)
    handle.truncate()
    handle.write(text.encode())
    handle.flush()


def _clear(handle):
    try:
        handle.seek(0)
        handle.truncate()
        handle.flush()
    except OSError:
        pass


@contextmanager
def profile_reservation(profile=None, what='studio.mjs', wait=None, announce=None, deadline=None):
    """Hold the reservation of `profile` (default: the one studio.mjs opens) for the block; yields the child environment
    {PROFILE_LOCK_ENV: path, PROFILE_TOKEN_ENV: token}. `wait` / `deadline` as tripo_browser_lock. With the byte lock taken,
    a node / Firefox a dead holder left on the profile is waited for as well (profile_users; finding 18)."""
    path = profile_lock_path(profile)
    if deadline is None and wait is not None:
        deadline = time.monotonic() + float(wait)
    handle = _open(path)
    try:
        _acquire(handle, deadline, lambda: _first_line(path), announce)
        try:
            _drain_users(profile, deadline, announce)
            token = secrets.token_hex(16)
            _write(handle, f'pid {os.getpid()} since {time.strftime("%Y-%m-%dT%H:%M:%S")} token {token} {what}\n')
            yield {PROFILE_LOCK_ENV: str(path), PROFILE_TOKEN_ENV: token}
        finally:
            _clear(handle)
            release_lock(handle)
    finally:
        handle.close()


@contextmanager
def tripo_browser_lock(root, what='studio.mjs', wait=None, announce=None, profile=None):
    """Hold the Tripo browser lock of `root` and then the profile reservation for the block; yields the environment a studio.mjs
    child needs ({IMAGETO3D_TRIPO_PROFILE_LOCK, IMAGETO3D_TRIPO_PROFILE_TOKEN}). `wait`: seconds to poll a busy lock, shared by both
    (None = forever, 0 = fail at once) before TripoBrowserBusy. `announce(text)` is called once per busy lock (default: stderr)."""
    path = lock_path(root)
    deadline = None if wait is None else time.monotonic() + float(wait)
    handle = _open(path)
    try:
        _acquire(handle, deadline, lambda: holder(root), announce)
        try:
            _write(handle, f'pid {os.getpid()} since {time.strftime("%Y-%m-%dT%H:%M:%S")} {what}\n')
            with profile_reservation(profile, what=what, announce=announce, deadline=deadline) as env:
                yield env
        finally:
            _clear(handle)
            release_lock(handle)
    finally:
        handle.close()


def verify(profile, lock, token) -> str | None:
    """None when `lock` / `token` name a reservation of `profile` that is held right now, else why not."""
    if not lock or not token:
        return 'no reservation in the environment'
    expected = profile_lock_path(profile)
    if not _same_path(lock, expected):
        return f'the reservation {lock} is not the one of profile {resolved_profile(profile)} ({expected})'
    try:
        text = Path(lock).read_text(encoding='utf-8', errors='replace')
    except OSError as error:
        return f'cannot read the reservation: {error}'
    if f' token {token} ' not in f' {text} ':
        return 'the reservation is held under another token (a different holder)'
    try:
        handle = _open(Path(lock))
    except OSError as error:
        return f'cannot open the reservation: {error}'
    try:
        if try_lock(handle):              # nobody holds it: the holder that wrote the token is gone
            release_lock(handle)
            return 'the reservation is not held (its holder exited)'
    finally:
        handle.close()
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('run', help='run a command while holding the profile reservation (its env names the reservation)')
    p.add_argument('--profile', help='the profile directory (default: the one studio.mjs opens)')
    p.add_argument('--wait', type=float, help=f'seconds to wait for a busy reservation (default ${PROFILE_WAIT_ENV} or {PROFILE_WAIT_S})')
    p.add_argument('cmd', nargs=argparse.REMAINDER)
    p = sub.add_parser('verify', help='exit 0 when --lock/--token are a held reservation of --profile')
    p.add_argument('--profile')
    p.add_argument('--lock', default='')
    p.add_argument('--token', default='')
    p.add_argument('--register-pid', type=int, help='on success record this process (studio.mjs) as the profile\'s user and '
                                                    'print the pid file path')
    p = sub.add_parser('users', help='list an orphaned node / Firefox still on --profile (exit 0 = none, 1 = some)')
    p.add_argument('--profile')
    args = parser.parse_args(argv)
    if args.command == 'users':
        users = profile_users(args.profile)
        for user in users:
            print(user, flush=True)
        return 1 if users else 0
    if args.command == 'verify':
        problem = verify(args.profile, args.lock, args.token)
        if problem:
            print(problem, flush=True)
            return 1
        if args.register_pid is not None:
            try:
                print(register_browser(args.profile, args.register_pid), flush=True)
            except (OSError, ValueError) as error:
                print(f'cannot register pid {args.register_pid} for the profile: {error}', flush=True)
                return 1
        return 0
    cmd = args.cmd[1:] if args.cmd[:1] == ['--'] else args.cmd
    if not cmd:
        parser.error('run needs a command after --')
    wait = args.wait
    if wait is None:
        try:
            wait = float(os.environ.get(PROFILE_WAIT_ENV, PROFILE_WAIT_S))
        except ValueError:
            wait = PROFILE_WAIT_S
    try:
        with profile_reservation(args.profile, what=f'run {" ".join(cmd)[:120]}', wait=wait) as env:
            return subprocess.call(cmd, env={**os.environ, **env})
    except TripoBrowserBusy as error:
        print(f'{error}; not opening the profile', file=sys.stderr, flush=True)
        return EXIT_BUSY


if __name__ == '__main__':
    raise SystemExit(main())
