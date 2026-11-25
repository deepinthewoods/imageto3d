"""Mode / pause / presence helpers for the Queue tab header (DESIGN_PHASE4.md section C).

Everything reads or writes files of the pipeline supervisor under `<root>/.codex_jobs/_batch/` and takes `root` so tests use a temp root:
  supervisor_mode.json  {"mode", "since"}  written here (atomic), read by the supervisor every tick
  PAUSE                 {"since", "reason", "scope"}  written by the supervisor only; read here
  supervisor.lock       single-instance lock of the supervisor, probed read-only like `Supervisor.instance()`

The UI presets differ from the supervisor's mode names: Auto = `views-run`, Pause conversions = `pause-conversions`, Pause all = `pause`,
Run everything now = `run`. (The supervisor's own `auto` pauses views too while the user is at the PC; the app never writes it, but a
mode file that holds it is shown as it is.)
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import guard  # noqa: E402

MODES = ('auto', 'pause', 'pause-conversions', 'views-run', 'run')   # = pipeline_supervisor.MODES (a test compares them)
PRESETS = {'auto': 'views-run', 'pause_conversions': 'pause-conversions', 'pause_all': 'pause', 'run_now': 'run'}
PRESET_LABELS = {'views-run': 'Auto', 'pause-conversions': 'Pause conversions', 'pause': 'Pause all', 'run': 'Run everything now',
                 'auto': 'Supervisor auto (images pause too while you are at the PC)'}
PRESENCE_FILE = Path('C:/Users/niall/.claude/presence.json')
PRESENCE_FRESH_S = 30.0
AWAY_AFTER = 300.0   # = pipeline_supervisor.AWAY_AFTER
LOCK_OFFSET = 1 << 20   # = pipeline_supervisor.LOCK_OFFSET


class ModeError(ValueError):
    pass


def batch(root) -> Path:
    return Path(root) / '.codex_jobs' / '_batch'


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default


# ------------------------------------------------------------------------------------------------------------------------ mode

def read_mode(root) -> dict:
    """{"mode", "since", "label"}; a missing / invalid file means the supervisor's default `auto`."""
    raw = read_json(batch(root) / 'supervisor_mode.json', {}) or {}
    mode = raw.get('mode') if isinstance(raw, dict) else None
    mode = mode if mode in MODES else 'auto'
    since = raw.get('since') if isinstance(raw, dict) and isinstance(raw.get('since'), str) else None
    return {'mode': mode, 'since': since, 'label': PRESET_LABELS[mode]}


def write_mode(root, mode: str) -> dict:
    """Write the supervisor's mode file (atomic) the way `pipeline_supervisor.py mode <value>` does; `mode` may also be a preset key
    (`pause_all`, `pause_conversions`, `run_now`, `auto`). Returns read_mode(root)."""
    mode = PRESETS.get(mode, mode)
    if mode not in MODES:
        raise ModeError(f'unknown mode {mode!r}; one of {list(MODES)} or {list(PRESETS)}')
    data = {'mode': mode, 'since': datetime.now().isoformat(timespec='seconds')}
    guard.write_atomic(batch(root) / 'supervisor_mode.json', (json.dumps(data, indent=2) + '\n').encode('utf-8'))
    return read_mode(root)


def read_pause(root) -> dict | None:
    """The PAUSE file as {"since", "reason", "scope": [...]} or None when absent (an unreadable file counts as a pause of everything)."""
    path = batch(root) / 'PAUSE'
    if not path.exists():
        return None
    raw = read_json(path, None)
    if not isinstance(raw, dict):
        return {'since': None, 'reason': 'unreadable PAUSE file', 'scope': ['conversions', 'views']}
    scope = raw.get('scope')
    return {'since': raw.get('since'), 'reason': raw.get('reason'),
            'scope': [str(s) for s in scope] if isinstance(scope, list) else ['conversions', 'views']}   # no scope = both (the old file format)


# ---------------------------------------------------------------------------------------------------------------------- presence

def win_idle_seconds() -> float | None:
    """Seconds since the last keyboard / mouse input (GetLastInputInfo); None when it cannot be read (not Windows, no session)."""
    if os.name != 'nt':
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]
        info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        ctypes.windll.kernel32.GetTickCount.restype = wintypes.DWORD
        return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:  # noqa: BLE001
        return None


def idle_seconds(now: float | None = None, presence_file: Path | None = None) -> tuple[float | None, str]:
    """(idle seconds, source): `presence.json` when written in the last 30 s, else GetLastInputInfo, else (None, 'unknown')."""
    now = time.time() if now is None else now
    try:
        data = json.loads(Path(presence_file or PRESENCE_FILE).read_text(encoding='utf-8-sig'))
        if now - float(data['updated_unix']) < PRESENCE_FRESH_S:
            return float(data['user_idle_seconds']), 'presence.json'
    except (OSError, ValueError, KeyError, TypeError):
        pass
    idle = win_idle_seconds()
    return (idle, 'GetLastInputInfo') if idle is not None else (None, 'unknown')


# ---------------------------------------------------------------------------------------------------------------------- supervisor

def supervisor_running(root) -> tuple[bool, str]:
    """(running, pid text): is the supervisor's single-instance lock held? Probes like `Supervisor.instance()`: the lock file is opened
    read-only, never created, and the probe lock is released again at once (a held lock = a running supervisor)."""
    path = batch(root) / 'supervisor.lock'
    if not path.exists():
        return False, ''
    try:
        handle = path.open()
    except OSError:
        return False, ''
    try:
        running = not guard.try_lock(handle)
        if not running:
            guard.release_lock(handle)
        handle.seek(0)
        try:
            pid = handle.read().strip()
        except OSError:
            pid = ''
    finally:
        handle.close()
    return running, pid


def snapshot(root, now: float | None = None, presence_file: Path | None = None, supervisor=None) -> dict:
    """What the Queue tab header shows: the mode (with its UI label), the PAUSE file, the user's idle time and the supervisor state.
    `supervisor` = (running, pid) injected by callers that already probed."""
    idle, source = idle_seconds(now, presence_file)
    running, pid = supervisor if supervisor is not None else supervisor_running(root)
    return {
        'mode': read_mode(root),
        'pause': read_pause(root),
        'idle_seconds': idle, 'idle_source': source,
        'away': None if idle is None else idle >= AWAY_AFTER,
        'supervisor_running': running, 'supervisor_pid': pid,
        'warning': None if running else 'supervisor not running: the mode has no effect on conversions',
    }
