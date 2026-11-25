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
    python tools/tripo_pipeline/tripo_browser_lock.py verify --profile <dir> --lock <path> --token <token>   (exit 0 = valid)

A manual run is simply `node tools/tripo_pipeline/studio.mjs balance` (it reserves the profile itself, waiting up to
$IMAGETO3D_TRIPO_PROFILE_WAIT seconds, default 600, then exits 75 "Tripo browser busy").

Both are the OS byte lock of claim_lock.py (claim_lock.try_lock: msvcrt.locking of the byte at 1<<20 on Windows, fcntl.flock
elsewhere), freed by the kernel when the holder dies; the files are never deleted. Lock order is always checkout lock, then
profile reservation (`run` takes only the reservation), so no two holders wait on each other. The holder writes `pid <pid> since
<time> ...` at the start of each file (informational, plus the token in the profile file).
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import importlib.util
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
    {PROFILE_LOCK_ENV: path, PROFILE_TOKEN_ENV: token}. `wait` / `deadline` as tripo_browser_lock."""
    path = profile_lock_path(profile)
    if deadline is None and wait is not None:
        deadline = time.monotonic() + float(wait)
    handle = _open(path)
    try:
        _acquire(handle, deadline, lambda: _first_line(path), announce)
        token = secrets.token_hex(16)
        try:
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
    args = parser.parse_args(argv)
    if args.command == 'verify':
        problem = verify(args.profile, args.lock, args.token)
        if problem:
            print(problem, flush=True)
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
