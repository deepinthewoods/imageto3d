"""Cross-process claim lock of an approved set (stdlib only; used by workflow.py claim and by the preview app v2).

`workflow.py claim <set>` takes an exclusive OS lock on `.codex_jobs/_batch/claim_locks/<set>.lock` for the whole claim,
before it writes the first `.base_views` snapshot file. Anything else that wants to move or rename `approved/<set>` (the
preview app's unapprove / undo) takes the same lock without blocking: `ClaimBusy` means "being claimed", try later.
The lock is an OS byte lock (msvcrt.locking on Windows like pipeline_supervisor.lock_try, fcntl.flock elsewhere), so it
is released by the kernel when its holder dies; the lock file itself is never deleted.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import time

if os.name == 'nt':
    import msvcrt
else:
    import fcntl

LOCK_OFFSET = 1 << 20          # same convention as pipeline_supervisor.py: the pid text at the start stays readable
SUBDIR = Path('.codex_jobs') / '_batch' / 'claim_locks'


class ClaimBusy(Exception):
    """Another process holds the claim lock of this set."""


def lock_path(root: Path, set_name: str) -> Path:
    return Path(root) / SUBDIR / f'{set_name}.lock'


def try_lock(handle) -> bool:
    if os.name == 'nt':
        handle.seek(LOCK_OFFSET)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        finally:
            handle.seek(0)
        return True
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def release_lock(handle) -> None:
    if os.name == 'nt':
        handle.seek(LOCK_OFFSET)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.seek(0)
    else:
        fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def claim_lock(root: Path, set_name: str, wait: float = 0.0):
    """Hold the claim lock of `set_name`; `wait` seconds of polling before ClaimBusy (0 = fail at once)."""
    path = lock_path(root, set_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, 'a+b')
    try:
        deadline = time.monotonic() + wait
        while not try_lock(handle):
            if time.monotonic() >= deadline:
                raise ClaimBusy(f'{set_name} is being claimed')
            time.sleep(0.05)
        try:
            yield
        finally:
            release_lock(handle)
    finally:
        handle.close()
