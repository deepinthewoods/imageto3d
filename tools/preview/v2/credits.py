"""Credit balances for the Queue tab (DESIGN_PHASE4.md section E.2). Cache: `.codex_jobs/_app/credits.json`.

    snapshot(root)            {"tripo": tripo_state(root), "hyper3d": hyper3d_state(root)}   cheap; safe to call on every poll
    tripo_state(root)         the setting (`off` = "off (no credits this month)") plus the cached balance of the last check
    check_tripo(root, run=)   the "Check balance" button: `node tools/tripo_pipeline/studio.mjs balance`, ONLY when no convert_job.py /
                              studio.mjs of this checkout runs (they use the same browser profile); otherwise {"status": "busy"}
    hyper3d_state(root)       newest `balances` entry of any `studio_submission.json` (models/**, monsters pipeline/**/hyper3d/)

The Hyper3D scan is cheap: it lists the candidate files (a depth-limited directory walk, ~0.1 s for 1000 modules) and reads only files whose
mtime is newer than the previous scan; the first scan reads files newest first and stops once a file is older than the best balance found
(a file's mtime cannot be earlier than the balances inside it). Nothing here ever submits or pays anything.
"""
from __future__ import annotations

from datetime import datetime, timezone
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


def check_tripo(root, run=None, now: float | None = None) -> dict:
    """Run the Tripo balance check once. `run(root) -> (returncode, stdout, stderr)` is injectable (tests never start node).
    {"status": "ok", "credits"} | {"status": "busy", "message"} (a conversion or another check uses the browser profile) | {"status": "error", "message"}."""
    if not _CHECK.acquire(blocking=False):
        return {'status': 'busy', 'message': 'a balance check is already running'}
    try:
        users = services.convert_job_running(root) or services.studio_running(root)
        if users:
            what = 'convert_job.py' if services.convert_job_running(root) else 'studio.mjs'
            return {'status': 'busy', 'message': f'{what} is running (pid {users[0]["pid"]}) and uses the Tripo browser profile; check again after it'}
        try:
            code, out, err = (run or _run_balance)(root)
        except (OSError, subprocess.SubprocessError) as error:
            return _tripo_failed(root, now, f'{type(error).__name__}: {error}')
        try:
            credits = json.loads(out.strip().splitlines()[-1])['credits']
        except (IndexError, ValueError, KeyError, TypeError):
            tail = ' | '.join((err or out).strip().splitlines()[-2:])
            return _tripo_failed(root, now, f'balance check failed (exit {code}): {tail}'[:400])
        cache = read_cache(root)
        cache['tripo'] = {'credits': credits, 'checked_at': now_iso(now), 'ok': True, 'error': None}
        write_cache(root, cache)
        return {'status': 'ok', 'credits': credits, 'checked_at': cache['tripo']['checked_at']}
    finally:
        _CHECK.release()


def _tripo_failed(root, now, message: str) -> dict:
    cache = read_cache(root)
    previous = cache.get('tripo') or {}
    cache['tripo'] = {'credits': previous.get('credits'), 'checked_at': previous.get('checked_at'), 'ok': False, 'error': message,
                      'failed_at': now_iso(now)}
    write_cache(root, cache)
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
            cache['hyper3d'] = {'best': best, 'high_water': top, 'scanned_at': now_iso()}
            write_cache(root, cache)
        return best


def hyper3d_state(root, extra_roots=None, scan: bool = True, persist: bool = True) -> dict:
    """{"credits", "at", "stage", "source"} of the newest recorded balance (None values when no submission recorded one)."""
    best = scan_hyper3d(root, extra_roots, persist) if scan else ((read_cache(root).get('hyper3d') or {}).get('best'))
    best = best or {}
    return {'credits': best.get('credits'), 'at': best.get('at'), 'stage': best.get('stage'), 'source': best.get('source')}


def snapshot(root, extra_roots=None, persist: bool = True) -> dict:
    return {'tripo': tripo_state(root), 'hyper3d': hyper3d_state(root, extra_roots, persist=persist)}
