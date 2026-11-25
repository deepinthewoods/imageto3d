"""Cross-process lock on the Tripo Studio browser profile (stdlib only; DESIGN_PHASE4 I.3, review_v2_phase4 finding 8).

`tools/tripo_pipeline/studio.mjs` drives ONE persistent Playwright Firefox profile (shared by every checkout), so every python
caller of studio.mjs holds `<root>/.codex_jobs/_batch/browser_tripo.lock` around each call: convert_job.py's submit / collect /
retry, convert_queue.tripo_balance and the preview app's balance check (v2 credits.check_tripo, which answers `busy` at once).

The lock is the same OS byte lock as claim_lock.py (claim_lock.try_lock: msvcrt.locking of the byte at 1<<20 on Windows,
fcntl.flock elsewhere), so it is freed by the kernel when its holder dies and the file is never deleted. The holder writes
`pid <pid> since <time> <what>` at the start of the file (informational only).
"""
from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
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


class TripoBrowserBusy(Exception):
    """Another process holds the Tripo browser lock (raised after `wait` seconds)."""


def lock_path(root) -> Path:
    return Path(root) / LOCK_NAME


def holder(root) -> str | None:
    try:
        return lock_path(root).read_text(encoding='utf-8', errors='replace').strip().splitlines()[0][:120]
    except (OSError, IndexError):
        return None


@contextmanager
def tripo_browser_lock(root, what='studio.mjs', wait=None, announce=None):
    """Hold the Tripo browser lock of `root` for the block. `wait`: seconds to poll a busy lock (None = forever, 0 = fail at
    once) before TripoBrowserBusy. `announce(text)` is called once when the lock is busy (default: print to stderr)."""
    path = lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o666), 'r+b')
    try:
        deadline = None if wait is None else time.monotonic() + float(wait)
        announced = False
        while not try_lock(handle):
            if deadline is not None and time.monotonic() >= deadline:
                raise TripoBrowserBusy(f'Tripo browser busy (held by {holder(root) or "another process"})')
            if not announced:
                text = f'Tripo browser busy (held by {holder(root) or "another process"}); waiting'
                (announce or (lambda t: print(t, file=sys.stderr, flush=True)))(text)
                announced = True
            time.sleep(POLL_S if deadline is None else min(POLL_S, max(0.05, deadline - time.monotonic())))
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f'pid {os.getpid()} since {time.strftime("%Y-%m-%dT%H:%M:%S")} {what}\n'.encode())
            handle.flush()
            yield
        finally:
            try:
                handle.seek(0)
                handle.truncate()
                handle.flush()
            except OSError:
                pass
            release_lock(handle)
    finally:
        handle.close()
