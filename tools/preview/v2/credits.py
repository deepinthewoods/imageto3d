"""Credit balances for the Queue tab (DESIGN_PHASE4.md section E.2). Cache: `.codex_jobs/_app/credits.json`.

    snapshot(root)            {"tripo": tripo_state(root), "hyper3d": hyper3d_state(root)}   cheap; safe to call on every poll
    tripo_state(root)         the setting (`off` = "off (no credits this month)") plus the cached balance of the last check
    check_tripo(root, run=)   the "Check balance" button: `node tools/tripo_pipeline/studio.mjs balance` while holding the Tripo browser lock
                              `.codex_jobs/_batch/browser_tripo.lock` (DESIGN_PHASE4.md I.3; taken without waiting: held by anyone = "busy"),
                              and only when no convert_job.py / studio.mjs of this checkout runs (legacy callers without the lock)
    hyper3d_state(root)       newest `balances` entry of any `studio_submission.json` (models/**, monsters pipeline/**/hyper3d/)

The Hyper3D scan is cheap: it lists the candidate files (a depth-limited directory walk, ~0.1 s for 1000 modules) and reads only files whose
mtime is newer than the previous scan; the first scan reads files newest first and stops once a file is older than the best balance found
(a file's mtime cannot be earlier than the balances inside it). Nothing here ever submits or pays anything.

Every write of credits.json is a read-merge-write of ONE key under the cross-process lock `.codex_jobs/_app/credits.lock` (`update_cache`), so a
balance check and a Hyper3D scan never overwrite each other's newly recorded value (phase 4 review F8). A balance check counts only with exit 0
and a numeric `credits`; anything else is an error that keeps the last good balance.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from contextlib import contextmanager
import math
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import guard  # noqa: E402
import services  # noqa: E402
import settings as app_settings  # noqa: E402

OFF_TEXT = 'off (no credits this month)'
SUBMISSION = 'studio_submission.json'
SCAN_DEPTH = 4                 # models/<set>/<job>/<race>/<module>/ and pipeline/<Monster>/models/<n>/hyper3d/ are both 4 directories deep
MTIME_SLACK = 120.0            # seconds: the file's mtime may trail a balance's timestamp by this much without being skipped
_CHECK = threading.Lock()
_SCAN = threading.Lock()


def cache_path(root) -> Path:
    return Path(root) / guard.APP / 'credits.json'


def read_cache(root) -> dict:
    try:
        data = json.loads(cache_path(root).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_cache(root, data: dict) -> None:
    guard.write_atomic(cache_path(root), (json.dumps(data, indent=2) + '\n').encode('utf-8'))


def cache_lock_path(root) -> Path:
    return Path(root) / guard.APP / 'credits.lock'


def update_cache(root, key: str, merge) -> dict:
    """Read credits.json, replace ONLY `key` with `merge(current value of the key)` and write it back, all under the credits lock (F8). Returns
    the new value of the key."""
    with guard.os_lock(cache_lock_path(root)):
        cache = read_cache(root)
        value = merge(cache.get(key) if isinstance(cache.get(key), dict) else None)
        cache[key] = value
        write_cache(root, cache)
    return value


def browser_lock_path(root) -> Path:
    return Path(root) / '.codex_jobs' / '_batch' / 'browser_tripo.lock'


class BrowserBusy(Exception):
    """Another process holds the Tripo browser lock."""


@contextmanager
def tripo_browser(root):
    """Hold `.codex_jobs/_batch/browser_tripo.lock` (DESIGN_PHASE4.md I.3: the cross-process byte lock, claim_lock style, every python caller of
    studio.mjs takes around each call) without waiting: BrowserBusy when anybody holds it."""
    path = browser_lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, 'a+b')
    try:
        if not guard.try_lock(handle):
            raise BrowserBusy('the Tripo browser profile is in use (browser_tripo.lock is held)')
        try:
            yield
        finally:
            guard.release_lock(handle)
    finally:
        handle.close()


def tripo_browser_busy(root) -> bool:
    """A quick probe of the Tripo browser lock (taken and released at once)."""
    try:
        with tripo_browser(root):
            return False
    except BrowserBusy:
        return True
    except OSError:
        return False


def now_iso(now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc).isoformat(timespec='seconds')


# ------------------------------------------------------------------------------------------------------------------------ Tripo

def tripo_state(root) -> dict:
    mode = app_settings.load(root)['tripo']
    cached = read_cache(root).get('tripo') or {}
    return {'mode': mode, 'text': OFF_TEXT if mode == 'off' else 'on (Tripo first)',
            'credits': cached.get('credits'), 'checked_at': cached.get('checked_at'), 'ok': cached.get('ok'), 'error': cached.get('error')}


def _run_balance(root) -> tuple[int, str, str]:
    run = subprocess.run(['node', str(Path(root) / 'tools' / 'tripo_pipeline' / 'studio.mjs'), 'balance'], capture_output=True, text=True,
                         encoding='utf-8', errors='replace', cwd=str(root), timeout=300, creationflags=services.NO_WINDOW)
    return run.returncode, run.stdout or '', run.stderr or ''


def _numeric(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_balance(code: int, out: str):
    """The balance of a `studio.mjs balance` run: only exit 0 with a numeric `credits` on the last stdout line counts (F4 / F8); else None."""
    if code != 0:
        return None
    try:
        credits = json.loads(out.strip().splitlines()[-1])['credits']
    except (IndexError, ValueError, KeyError, TypeError):
        return None
    return credits if _numeric(credits) else None


def check_tripo(root, run=None, now: float | None = None) -> dict:
    """Run the Tripo balance check once. `run(root) -> (returncode, stdout, stderr)` is injectable (tests never start node).
    {"status": "ok", "credits"} | {"status": "busy", "message"} (the browser lock is held, or a conversion / another check uses the profile)
    | {"status": "error", "message"} (the last good balance stays in the cache)."""
    if not _CHECK.acquire(blocking=False):
        return {'status': 'busy', 'message': 'a balance check is already running'}
    try:
        try:
            with tripo_browser(root):
                jobs = services.convert_job_running(root)
                users = jobs or services.studio_running(root)
                if users:
                    what = 'convert_job.py' if jobs else 'studio.mjs'
                    return {'status': 'busy', 'message': f'{what} is running (pid {users[0]["pid"]}) and uses the Tripo browser profile; check again after it'}
                try:
                    code, out, err = (run or _run_balance)(root)
                except (OSError, subprocess.SubprocessError) as error:
                    return _tripo_failed(root, now, f'{type(error).__name__}: {error}')
        except BrowserBusy as exc:
            return {'status': 'busy', 'message': f'{exc}; check again after the conversion'}
        credits = parse_balance(code, out)
        if credits is None:
            tail = ' | '.join((err or out).strip().splitlines()[-2:])
            return _tripo_failed(root, now, f'balance check failed (exit {code}, no numeric balance): {tail}'[:400])
        stamp = now_iso(now)
        update_cache(root, 'tripo', lambda _old: {'credits': credits, 'checked_at': stamp, 'ok': True, 'error': None})
        return {'status': 'ok', 'credits': credits, 'checked_at': stamp}
    finally:
        _CHECK.release()


def _tripo_failed(root, now, message: str) -> dict:
    stamp = now_iso(now)

    def merge(previous):
        previous = previous or {}
        return {'credits': previous.get('credits'), 'checked_at': previous.get('checked_at'), 'ok': False, 'error': message, 'failed_at': stamp}
    update_cache(root, 'tripo', merge)
    return {'status': 'error', 'message': message}


# ---------------------------------------------------------------------------------------------------------------------- Hyper3D

def default_extra_roots(root) -> list[Path]:
    """characters/monsters/aaa/pipeline next to this checkout (the monster conversions' `hyper3d/studio_submission.json`)."""
    return [Path(root).parent / 'monsters' / 'aaa' / 'pipeline']


def submission_files(root, extra_roots=None) -> list[Path]:
    """Every studio_submission.json under models/ and the extra roots, at most SCAN_DEPTH directories deep."""
    bases = [Path(root) / 'models'] + [Path(p) for p in (default_extra_roots(root) if extra_roots is None else extra_roots)]
    found: list[Path] = []

    def walk(directory: str, depth: int) -> None:
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if depth < SCAN_DEPTH:
                                walk(entry.path, depth + 1)
                        elif entry.name == SUBMISSION:
                            found.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            return

    for base in bases:
        if base.is_dir():
            walk(str(base), 0)
    return found


def _parse_time(text) -> float | None:
    try:
        stamp = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()


def _best_balance(path: Path) -> dict | None:
    """The newest entry of the file's `balances` list ({stage, credits, at}) or None."""
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    best = None
    for entry in (data.get('balances') or []) if isinstance(data, dict) else []:
        if not isinstance(entry, dict) or isinstance(entry.get('credits'), bool) or not isinstance(entry.get('credits'), (int, float)):
            continue
        ts = _parse_time(entry.get('at'))
        if ts is not None and (best is None or ts > best['ts']):
            best = {'ts': ts, 'credits': entry['credits'], 'at': entry.get('at'), 'stage': entry.get('stage')}
    return best


def scan_hyper3d(root, extra_roots=None, persist: bool = True) -> dict | None:
    """Update the cached Hyper3D balance from the submission files changed since the last scan; returns the cached entry (or None).
    `persist=False` (a read-only server) computes the same answer without writing credits.json."""
    with _SCAN:
        cache = read_cache(root)
        state = cache.get('hyper3d') if isinstance(cache.get('hyper3d'), dict) else {}
        best = state.get('best') if isinstance(state.get('best'), dict) else None
        high = float(state.get('high_water') or 0.0)
        stamped = []
        for path in submission_files(root, extra_roots):
            try:
                stamped.append((path.stat().st_mtime, path))
            except OSError:
                continue
        stamped.sort(key=lambda item: item[0], reverse=True)
        top = stamped[0][0] if stamped else high
        changed = False
        for mtime, path in stamped:
            if high and mtime <= high:
                break                              # files are newest first: everything left was already scanned
            if best is not None and mtime < best['ts'] - MTIME_SLACK:
                break                              # cannot hold a balance newer than the best known one
            found = _best_balance(path)
            if found and (best is None or found['ts'] > best['ts']):
                best = {**found, 'source': str(path)}
                changed = True
        if persist and (changed or top != high or 'hyper3d' not in cache):
            stamp, found = now_iso(), best

            def merge(current):
                # another writer may have recorded a newer balance meanwhile: keep the newest best and the highest high-water mark
                current = current or {}
                other = current.get('best') if isinstance(current.get('best'), dict) else None
                keep = other if other is not None and (found is None or float(other.get('ts') or 0) > float(found.get('ts') or 0)) else found
                mark = max(float(current.get('high_water') or 0.0), float(top or 0.0))
                return {'best': keep, 'high_water': mark, 'scanned_at': stamp}
            best = update_cache(root, 'hyper3d', merge)['best']
        return best


def hyper3d_state(root, extra_roots=None, scan: bool = True, persist: bool = True) -> dict:
    """{"credits", "at", "stage", "source"} of the newest recorded balance (None values when no submission recorded one)."""
    best = scan_hyper3d(root, extra_roots, persist) if scan else ((read_cache(root).get('hyper3d') or {}).get('best'))
    best = best or {}
    return {'credits': best.get('credits'), 'at': best.get('at'), 'stage': best.get('stage'), 'source': best.get('source')}


def snapshot(root, extra_roots=None, persist: bool = True) -> dict:
    return {'tripo': tripo_state(root), 'hyper3d': hyper3d_state(root, extra_roots, persist=persist)}
