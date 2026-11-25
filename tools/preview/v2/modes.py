"""Mode / pause / presence helpers for the Queue tab header (DESIGN_PHASE4.md section C).

Everything reads or writes files of the pipeline supervisor under `<root>/.codex_jobs/_batch/` and takes `root` so tests use a temp root:
  supervisor_mode.json  {"mode", "since", "pause_id"}  written here (atomic), read by the supervisor every tick
  PAUSE                 {"since", "reason", "scope"}  written by the supervisor only; read here
  supervisor.lock       single-instance lock of the supervisor, probed read-only like `Supervisor.instance()`

The UI presets differ from the supervisor's mode names: Auto = `views-run`, Pause conversions = `pause-conversions`, Pause all = `pause`,
Run everything now = `run`. (The supervisor's own `auto` pauses views too while the user is at the PC; the app never writes it, but a
mode file that holds it is shown as it is.)

PAUSE IDENTITY (recheck-before-cutover finding 5). Every pause transition has a unique `pause_id`: a mode write of this module stores a
fresh uuid4 in the mode file; a mode file or PAUSE file written by somebody else (the supervisor, its `mode` command) carries none, so its
id is derived from the file itself (`file:` + a digest of its inode, creation / modification time in ns, size and bytes: every rewrite is
a new file, hence a new id, also within one second). `manual_pause(root)` names the manual pause of conversions in force; a confirmed
Run now of the monster lane overrides exactly that `pause_id` and no later pause.

ALL INDEPENDENT PAUSES (final-verify review, row 5). Two manual pauses can be in force at once: the pause MODE and a separate manual
maintenance PAUSE file. `manual_pauses(root)` lists every independent one; the supervisor's own PAUSE file is NOT independent when it
only reflects the mode (`reason: "manual <the current pause mode>"` with the scope the supervisor writes for it and no other field) or
the user's activity (IDLE_REASONS). `manual_pause(root)['pause_id']` identifies the whole SET (one pause: its own id; several: their
ids joined with `+`), and `consent_covers(consent, manual)` is true only when every pause in force is in the confirmed set, so a consent
given for the mode pause does not cover a manual PAUSE added afterwards.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import hashlib
import sys
import time
import uuid

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
PAUSE_MODES = ('pause', 'pause-conversions')   # the modes that pause conversions by hand
ID_JOIN = '+'                                  # joins the ids of several independent manual pauses into one `pause_id`
IDLE_REASONS = ('user active', 'user active (views keep running)')   # pipeline_supervisor: the screen-on pauses (not manual)


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

def file_identity(path: Path) -> str | None:
    """The identity of one WRITE of a file somebody else owns (finding 5): `file:` + a digest of its inode, creation and modification
    time (ns), size and bytes. None when the file is absent; a file that exists but cannot be read gets an id that never repeats (so it
    is never equal to an id a user confirmed)."""
    path = Path(path)
    try:
        st = path.stat()
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return f'unidentified:{uuid.uuid4().hex}'
    basis = f'{st.st_ino}:{getattr(st, "st_ctime_ns", 0)}:{st.st_mtime_ns}:{st.st_size}:'.encode('ascii') + raw
    return 'file:' + hashlib.sha256(basis).hexdigest()[:32]


def read_mode(root) -> dict:
    """{"mode", "since", "label", "pause_id"}; a missing / invalid file means the supervisor's default `auto`. `pause_id` (None unless
    the mode pauses conversions) = the id this module wrote with the mode, else the identity of the file's write (file_identity)."""
    path = batch(root) / 'supervisor_mode.json'
    raw = read_json(path, {}) or {}
    mode = raw.get('mode') if isinstance(raw, dict) else None
    mode = mode if mode in MODES else 'auto'
    since = raw.get('since') if isinstance(raw, dict) and isinstance(raw.get('since'), str) else None
    pause_id = None
    if mode in PAUSE_MODES:
        stored = raw.get('pause_id') if isinstance(raw, dict) else None
        pause_id = stored if isinstance(stored, str) and stored else file_identity(path)
    return {'mode': mode, 'since': since, 'label': PRESET_LABELS[mode], 'pause_id': pause_id}


def write_mode(root, mode: str) -> dict:
    """Write the supervisor's mode file (atomic) the way `pipeline_supervisor.py mode <value>` does; `mode` may also be a preset key
    (`pause_all`, `pause_conversions`, `run_now`, `auto`). EVERY write carries a fresh uuid4 `pause_id` (finding 5): pausing again, even
    with the same mode within the same second, is a new pause. Returns read_mode(root)."""
    mode = PRESETS.get(mode, mode)
    if mode not in MODES:
        raise ModeError(f'unknown mode {mode!r}; one of {list(MODES)} or {list(PRESETS)}')
    data = {'mode': mode, 'since': datetime.now().isoformat(timespec='seconds'), 'pause_id': uuid.uuid4().hex}
    guard.write_atomic(batch(root) / 'supervisor_mode.json', (json.dumps(data, indent=2) + '\n').encode('utf-8'))
    return read_mode(root)


def read_pause(root) -> dict | None:
    """The PAUSE file as {"since", "reason", "scope": [...], "pause_id"} or None when absent (an unreadable file counts as a pause of
    everything). `pause_id` = the file's own `pause_id` when its writer stored one, else the identity of this write (file_identity).
    (This shape is what GET /api/v2/state `status.paused` shows; the fields the supervisor never writes are `pause_extra`.)"""
    path = batch(root) / 'PAUSE'
    if not path.exists():
        return None
    ident = file_identity(path)
    if ident is None:                                         # removed between the two looks
        return None
    raw = read_json(path, None)
    if not isinstance(raw, dict):
        return {'since': None, 'reason': 'unreadable PAUSE file', 'scope': ['conversions', 'views'], 'pause_id': ident}
    scope = raw.get('scope')
    stored = raw.get('pause_id')
    return {'since': raw.get('since'), 'reason': raw.get('reason'),
            'scope': [str(s) for s in scope] if isinstance(scope, list) else ['conversions', 'views'],   # no scope = both (the old file format)
            'pause_id': stored if isinstance(stored, str) and stored else ident}


def pause_extra(root) -> list[str]:
    """The names of the PAUSE file's fields the supervisor never writes (it writes since, reason, scope); [] when the file is absent or
    unreadable. A file that has one is nobody's reflection of the mode (mode_reflection)."""
    raw = read_json(batch(root) / 'PAUSE', None)
    return sorted(str(k) for k in raw if k not in ('since', 'reason', 'scope')) if isinstance(raw, dict) else []


def mode_reflection(mode: dict, pause: dict | None) -> bool:
    """Whether a PAUSE file is the supervisor's reflection of the CURRENT pause mode (pipeline_supervisor `desired_pause` + its tick):
    `reason: "manual <mode>"` for the mode in force, the scope the supervisor writes for that mode and no field the supervisor never
    writes (`pause['extra']`, from pause_extra). Such a file is the same pause as the mode (one consent covers both); any other file
    that pauses conversions for a manual reason is an INDEPENDENT pause (final-verify review, row 5)."""
    if pause is None or mode.get('mode') not in PAUSE_MODES:
        return False
    if pause.get('reason') != f'manual {mode["mode"]}' or pause.get('extra'):
        return False
    want = ['conversions'] if mode['mode'] == 'pause-conversions' else ['conversions', 'views']
    return list(pause.get('scope') or []) == want


def manual_pauses(root, mode: dict | None = None, pause: dict | None = None) -> list[dict]:
    """EVERY independent manual pause of conversions now in force, [{"pause_id", "kind": mode | pause, "reason"}]: the pause mode (its
    pause_id), and a PAUSE file with `conversions` in its scope that is neither the idle gate (a "user active" PAUSE) nor the
    supervisor's reflection of the current pause mode (mode_reflection). `mode` / `pause` = read_mode / read_pause results the caller
    already has."""
    mode = read_mode(root) if mode is None else mode
    pause = read_pause(root) if pause is None else pause
    if pause is not None and 'extra' not in pause:
        pause = pause | {'extra': pause_extra(root)}            # (kept out of read_pause: its shape is the API's `status.paused`)
    out = []
    if mode['mode'] in PAUSE_MODES:
        out.append({'pause_id': mode.get('pause_id') or f'unidentified:{uuid.uuid4().hex}', 'kind': 'mode', 'reason': f'mode {mode["mode"]}'})
    if pause is not None and 'conversions' in (pause.get('scope') or []) and pause.get('reason') not in IDLE_REASONS \
            and not mode_reflection(mode, pause):
        out.append({'pause_id': pause.get('pause_id') or f'unidentified:{uuid.uuid4().hex}', 'kind': 'pause',
                    'reason': str(pause.get('reason') or 'PAUSE file')})
    return out


def manual_pause(root, mode: dict | None = None, pause: dict | None = None) -> dict | None:
    """The MANUAL pause of conversions now in force, {"pause_id", "kind": mode | pause | mode+pause, "reason", "pauses"}, or None. It
    stands for ALL independent manual pauses (manual_pauses): with one, `pause_id` is that pause's own id; with several it is their ids
    joined with `+`, so it changes whenever one of them is added, removed or made again (a consent names exactly the set of pauses the
    user was shown: final-verify review, row 5; `consent_covers` compares it with the pauses in force later)."""
    pauses = manual_pauses(root, mode, pause)
    if not pauses:
        return None
    for entry in pauses:                                        # ids are joined with `+` below: one that contains it is replaced
        if ID_JOIN in entry['pause_id']:
            entry['pause_id'] = 'id:' + hashlib.sha256(entry['pause_id'].encode('utf-8')).hexdigest()[:32]
    if len(pauses) == 1:
        return dict(pauses[0]) | {'pauses': pauses}
    return {'pause_id': ID_JOIN.join(p['pause_id'] for p in pauses), 'kind': '+'.join(p['kind'] for p in pauses),
            'reason': '; '.join(p['reason'] for p in pauses), 'pauses': pauses}


def consent_covers(consent: object, manual: dict | None) -> bool:
    """Whether a confirmed Run now (`consent` = the `pause_id` the user confirmed, kept as the task's `override_pause`) covers the
    manual pauses in force (`manual` = manual_pause(...)): EVERY independent pause in force must be one the user was shown. A pause
    added after the confirmation is not covered (the gate closes again, lane and paid child alike); one that was lifted meanwhile does
    not take the consent for the others away. False without a consent or without a pause."""
    if not isinstance(consent, str) or not consent or manual is None:
        return False
    seen = set(consent.split(ID_JOIN))
    return all(p['pause_id'] in seen for p in manual.get('pauses') or [manual])


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
    """What the Queue tab header shows: the mode (with its UI label), the PAUSE file, `pause_id` (manual_pause), the user's idle time and
    the supervisor state.
    `supervisor` = (running, pid) injected by callers that already probed."""
    idle, source = idle_seconds(now, presence_file)
    running, pid = supervisor if supervisor is not None else supervisor_running(root)
    mode, pause = read_mode(root), read_pause(root)
    manual = manual_pause(root, mode, pause)
    return {
        'mode': mode,
        'pause': pause,
        'pause_id': manual['pause_id'] if manual else None,     # the manual pause of conversions in force (finding 5), else None
        'idle_seconds': idle, 'idle_source': source,
        'away': None if idle is None else idle >= AWAY_AFTER,
        'supervisor_running': running, 'supervisor_pid': pid,
        'warning': None if running else 'supervisor not running: the mode has no effect on conversions',
    }
