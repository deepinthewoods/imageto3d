"""Cross-process locks of the Hyper3D runners (stdlib only; no selenium/playwright import, so every entry point can use it).

BrowserLock / open_profile: the Hyper3D Firefox profile admits ONE browser, and the outfit conversion (convert_job.py ->
rodin_backend.py), the monster wrappers (monsters/aaa/tools/hyper3d_*.py subclass rodin_backend.SeleniumRodin), the
preview app's monster lane and the manual runners (native_browser.py, workflow.py) all start separate processes. Every
function that opens the profile in Firefox goes through `open_profile`, which takes the OS byte lock
`<imageto3d root>/.codex_jobs/_batch/browser_hyper3d.lock` BEFORE it checks the profile and launches the browser; the process
that opens the profile holds it until it closes the browser. The kernel frees it if the holder dies; the file is never
deleted. The lock is re-entrant within one process (a module-level depth counter per lock file), so nested users in one
process never deadlock on themselves. A parent (e.g. a preview app lane) never holds it while it waits on a child that
opens the profile: the child would wait for the parent until $HYPER3D_LOCK_WAIT runs out.

OutputLock: one paid-record lock per Hyper3D output folder, `<out_dir>/.record.lock`, held for the whole submit / collect /
preview / inspect / bind command (review_v2_phase4 finding 1: two collectors reading the same record could both confirm
the geometry). A second command on the same output stops at once with Stop('another process is handling this output').
It is re-entrant within one THREAD only, so two threads of one process exclude each other like two processes.
"""
import contextlib
import importlib.util
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_claim_lock():
    """tools/tripo_pipeline/claim_lock.py (try_lock / release_lock: msvcrt byte lock on Windows, flock elsewhere), loaded by
    path so that folder never joins sys.path (its workflow.py/palette.py must not shadow this folder's modules)."""
    spec = importlib.util.spec_from_file_location('claim_lock', HERE.parent / 'tripo_pipeline' / 'claim_lock.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


claim_lock = _load_claim_lock()


class Stop(Exception):
    """A fail-closed stop: nothing wrong happened, a guard or a missing permission halted the run (rodin_backend.Stop)."""


# --------------------------------------------------------------------------- browser (profile) lock

LOCK_FILE = HERE.parents[1] / '.codex_jobs' / '_batch' / 'browser_hyper3d.lock'
LOCK_FILE_ENV = 'HYPER3D_BROWSER_LOCK'       # test hook: a different lock file
LOCK_WAIT_ENV = 'HYPER3D_LOCK_WAIT'          # seconds to wait for a busy browser (0 = fail at once)
LOCK_WAIT_S = 600
LOCK_POLL_S = 1.0
LOCK_BUSY = 'Hyper3D profile busy (another conversion holds the browser)'

_HELD = {}                   # normcased lock path -> [handle, depth]: the locks this PROCESS holds
_GUARD = threading.RLock()


def lock_wait_seconds():
    try:
        return max(0.0, float(os.environ[LOCK_WAIT_ENV]))
    except (KeyError, ValueError):
        return float(LOCK_WAIT_S)


def _key(path):
    return os.path.normcase(os.path.abspath(str(path)))


def _stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


class BrowserLock:
    """Exclusive cross-process lock on the Hyper3D browser profile, re-entrant within this process. `acquire` polls for
    `wait` seconds (default $HYPER3D_LOCK_WAIT or 600) and then raises Stop(LOCK_BUSY); `release` is idempotent per object.
    The holder's pid is written at the start of the file for the waiter's message (informational only: the OS lock is the
    authority)."""

    def __init__(self, path=None, wait=None):
        self.path = Path(path or os.environ.get(LOCK_FILE_ENV) or LOCK_FILE)
        self.wait = lock_wait_seconds() if wait is None else float(wait)
        self.held = False

    def _holder(self):
        try:
            return self.path.read_text(errors='replace').strip().splitlines()[0][:80]
        except (OSError, IndexError):
            return None

    def _take(self, key):
        """One attempt under the guard: join this process's hold, or take the OS lock. True when held."""
        with _GUARD:
            entry = _HELD.get(key)
            if entry:
                entry[1] += 1
                return True
            handle = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666), 'r+b')
            try:
                if not claim_lock.try_lock(handle):
                    handle.close()
                    return False
                handle.seek(0)
                handle.truncate()
                handle.write(f'pid {os.getpid()} since {_stamp()}\n'.encode())
                handle.flush()
            except BaseException:
                handle.close()
                raise
            _HELD[key] = [handle, 1]
            return True

    def acquire(self):
        if self.held:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        key = _key(self.path)
        deadline = time.monotonic() + self.wait
        announced = False
        while not self._take(key):
            if time.monotonic() >= deadline:
                holder = self._holder()
                raise Stop(LOCK_BUSY + (f'; held by {holder}' if holder else '') + f'; waited {self.wait:g} s')
            if not announced:
                print(f'{LOCK_BUSY}; waiting up to {self.wait:g} s ({self.path})', file=sys.stderr, flush=True)
                announced = True
            time.sleep(min(LOCK_POLL_S, max(0.05, deadline - time.monotonic())))
        self.held = True
        return self

    def release(self):
        if not self.held:
            return
        self.held = False
        key = _key(self.path)
        with _GUARD:
            entry = _HELD.get(key)
            if not entry:
                return
            entry[1] -= 1
            if entry[1] > 0:
                return
            handle = _HELD.pop(key)[0]
            try:
                handle.seek(0)
                handle.truncate()
                handle.flush()
                claim_lock.release_lock(handle)
            except OSError:
                pass
            finally:
                handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


def held_by_this_process(path=None):
    """Depth of this process's hold on the browser lock (0 = not held here)."""
    with _GUARD:
        entry = _HELD.get(_key(path or os.environ.get(LOCK_FILE_ENV) or LOCK_FILE))
        return entry[1] if entry else 0


def open_profile(check, launch, wait=None):
    """The one way to open the Hyper3D Firefox profile: take the browser lock, then `check()` (native_browser.check_profile:
    raises ValueError when another Firefox has the profile open), then `launch()` (starts Firefox on it). Returns
    (launch(), lock); the caller releases `lock` after it closed the browser. Any failure releases the lock and re-raises
    (a busy lock: Stop(LOCK_BUSY))."""
    lock = BrowserLock(wait=wait).acquire()
    try:
        check()
        return launch(), lock
    except BaseException:
        lock.release()
        raise


# --------------------------------------------------------------------------- per-output paid-record lock

RECORD_LOCK = '.record.lock'
OUTPUT_BUSY = 'another process is handling this output'
_OUTPUT = threading.local()   # .held: {normcased lock path: [handle, depth]} of this THREAD


def _output_held():
    if not hasattr(_OUTPUT, 'held'):
        _OUTPUT.held = {}
    return _OUTPUT.held


@contextlib.contextmanager
def output_lock(out_dir):
    """Hold `<out_dir>/.record.lock` (the folder must exist). Yields True for the outermost hold of this thread (the
    caller then (re-)reads the record), False for a nested one. A lock held elsewhere -> Stop(OUTPUT_BUSY) at once,
    before anything is read or written."""
    path = Path(out_dir) / RECORD_LOCK
    key = _key(path)
    held = _output_held()
    if key in held:
        held[key][1] += 1
        try:
            yield False
        finally:
            held[key][1] -= 1
        return
    handle = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o666), 'r+b')
    try:
        if not claim_lock.try_lock(handle):
            raise Stop(f'{OUTPUT_BUSY}: {Path(out_dir)}')
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f'pid {os.getpid()} since {_stamp()}\n'.encode())
            handle.flush()
            held[key] = [handle, 1]
            try:
                yield True
            finally:
                held.pop(key, None)
                handle.seek(0)
                handle.truncate()
                handle.flush()
        finally:
            claim_lock.release_lock(handle)
    finally:
        handle.close()
