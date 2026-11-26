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

ONE SNAPSHOT PER FILE (last-check review, row 5). A pause's identity, reason, scope and extra fields all come from ONE read of its file
(`read_file`: the file is opened once; the identity is the digest of the bytes of that read + the inode / times / size of that same open
handle; the JSON is parsed from those same bytes). `read_mode` / `pause_snapshot` never look at a file twice, and `manual_pauses`
compares consent against exactly that snapshot, so replacing maintenance pause A by B between two looks can never yield "the id of A
with the reason of B" (a consent for A accepted while B is reported).

WRITES (last-check review, row 6): `write_mode` writes supervisor_mode.json under `paid_gate.transition_lock(root)` (retried while
GateBusy), so a pause can never land between a paid click's final gate check and its dispatch. This module writes no PAUSE file.
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
for _p in (HERE.parents[1], HERE.parent, HERE):
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

UNREADABLE = object()          # read_file: the file exists but its bytes are not JSON (or could not be read)
LOCK_WAIT = 30.0               # write_mode: how long it keeps retrying the gate transition mutex
READS: list = []               # test seam: callables run after every read_file (path) - a writer that replaces the file "between reads"


def _identity(st, raw: bytes) -> str:
    basis = f'{st.st_ino}:{getattr(st, "st_ctime_ns", 0)}:{st.st_mtime_ns}:{st.st_size}:'.encode('ascii') + raw
    return 'file:' + hashlib.sha256(basis).hexdigest()[:32]


def read_file(path: Path) -> tuple[str | None, object]:
    """ONE snapshot of a file somebody else may rewrite (row 5): (identity, value). The file is opened ONCE; `identity` = `file:` + a
    digest of that handle's inode, creation / modification time (ns), size and the bytes read from it; `value` = the JSON parsed from
    those same bytes, or UNREADABLE. (None, None) when the file is absent. A file that exists but cannot be read gets an identity that
    never repeats (so it is never equal to an id a user confirmed) and UNREADABLE."""
    path = Path(path)
    try:
        with open(path, 'rb') as handle:
            st = os.fstat(handle.fileno())
            raw = handle.read()
    except FileNotFoundError:
        out = (None, None)
    except OSError:
        out = (f'unidentified:{uuid.uuid4().hex}', UNREADABLE)
    else:
        try:
            value = json.loads(raw.decode('utf-8-sig'))
        except ValueError:
            value = UNREADABLE
        out = (_identity(st, raw), value)
    for hook in list(READS):
        hook(path)
    return out


def file_identity(path: Path) -> str | None:
    """The identity of one WRITE of a file somebody else owns (finding 5), from one read (read_file); None when the file is absent.
    Never combine it with a separate read of the same file: take identity and content together from read_file."""
    return read_file(path)[0]


def read_mode(root) -> dict:
    """{"mode", "since", "label", "pause_id"}; a missing / invalid file means the supervisor's default `auto`. `pause_id` (None unless
    the mode pauses conversions) = the id this module wrote with the mode, else the identity of the file's write; mode and identity come
    from the SAME read (row 5)."""
    ident, raw = read_file(batch(root) / 'supervisor_mode.json')
    raw = raw if isinstance(raw, dict) else {}
    mode = raw.get('mode')
    mode = mode if isinstance(mode, str) and mode in MODES else 'auto'
    since = raw.get('since') if isinstance(raw.get('since'), str) else None
    pause_id = None
    if mode in PAUSE_MODES:
        stored = raw.get('pause_id')
        pause_id = stored if isinstance(stored, str) and stored else ident
    return {'mode': mode, 'since': since, 'label': PRESET_LABELS[mode], 'pause_id': pause_id}


def write_mode(root, mode: str) -> dict:
    """Write the supervisor's mode file (atomic) the way `pipeline_supervisor.py mode <value>` does; `mode` may also be a preset key
    (`pause_all`, `pause_conversions`, `run_now`, `auto`). EVERY write carries a fresh uuid4 `pause_id` (finding 5): pausing again, even
    with the same mode within the same second, is a new pause. The write happens under the gate transition mutex
    (paid_gate.transition_lock; retried while GateBusy for LOCK_WAIT seconds, then ModeError: nothing was written). Returns what was
    written, in read_mode's shape."""
    mode = PRESETS.get(mode, mode)
    if mode not in MODES:
        raise ModeError(f'unknown mode {mode!r}; one of {list(MODES)} or {list(PRESETS)}')
    import paid_gate   # tools/paid_gate.py: the gate transition mutex (imported here: the read-only users of this module need no tools/)
    data = {'mode': mode, 'since': datetime.now().isoformat(timespec='seconds'), 'pause_id': uuid.uuid4().hex}
    deadline = time.monotonic() + LOCK_WAIT
    while True:
        try:
            with paid_gate.transition_lock(root, wait=2.0, what=f'mode {mode}'):
                guard.write_atomic(batch(root) / 'supervisor_mode.json', (json.dumps(data, indent=2) + '\n').encode('utf-8'))
            break
        except paid_gate.GateBusy as exc:
            if time.monotonic() >= deadline:
                raise ModeError(f'the mode was not changed: {exc}') from exc
    return {'mode': mode, 'since': data['since'], 'label': PRESET_LABELS[mode],
            'pause_id': data['pause_id'] if mode in PAUSE_MODES else None}


def pause_snapshot(root) -> dict | None:
    """The PAUSE file from ONE read (row 5): {"since", "reason", "scope": [...], "pause_id", "extra": [...]} or None when absent (an
    unreadable file counts as a pause of everything). `pause_id` = the file's own `pause_id` when its writer stored one, else the
    identity of this read; `extra` = the names of the fields the supervisor never writes (it writes since, reason, scope): a file that
    has one is nobody's reflection of the mode (mode_reflection). Identity, reason, scope and extra describe the same bytes."""
    ident, raw = read_file(batch(root) / 'PAUSE')
    if ident is None:
        return None
    if not isinstance(raw, dict):
        return {'since': None, 'reason': 'unreadable PAUSE file', 'scope': ['conversions', 'views'], 'pause_id': ident, 'extra': []}
    scope = raw.get('scope')
    stored = raw.get('pause_id')
    return {'since': raw.get('since'), 'reason': raw.get('reason'),
            'scope': [str(s) for s in scope] if isinstance(scope, list) else ['conversions', 'views'],   # no scope = both (the old file format)
            'pause_id': stored if isinstance(stored, str) and stored else ident,
            'extra': sorted(str(k) for k in raw if k not in ('since', 'reason', 'scope'))}


def public_pause(pause: dict | None) -> dict | None:
    """A pause_snapshot in the shape GET /api/v2/state `status.paused` shows (without `extra`)."""
    return None if pause is None else {k: v for k, v in pause.items() if k != 'extra'}


def read_pause(root) -> dict | None:
    """The PAUSE file as {"since", "reason", "scope": [...], "pause_id"} or None when absent: pause_snapshot without `extra` (display
    only; whoever decides about consent passes a pause_snapshot to manual_pause, never this plus a second read)."""
    return public_pause(pause_snapshot(root))


def pause_extra(root) -> list[str]:
    """The names of the PAUSE file's fields the supervisor never writes; [] when the file is absent or unreadable. Display only: a
    decision takes `extra` from the same pause_snapshot as the id and the reason."""
    pause = pause_snapshot(root)
    return list(pause['extra']) if pause else []


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
    already has; `pause` must be a pause_snapshot (it carries `extra` from the same read as its id and reason). A `pause` without `extra`
    (read_pause's display shape) is NOT completed from a second look at the file: the file is read again as one snapshot and THAT
    snapshot is used for id, reason, scope and extra alike (row 5)."""
    mode = read_mode(root) if mode is None else mode
    if pause is None or 'extra' not in pause:
        pause = pause_snapshot(root)
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
    mode, pause = read_mode(root), pause_snapshot(root)          # one read per file: id and reason of the same bytes (row 5)
    manual = manual_pause(root, mode, pause)
    return {
        'mode': mode,
        'pause': public_pause(pause),
        'pause_id': manual['pause_id'] if manual else None,     # the manual pause of conversions in force (finding 5), else None
        'idle_seconds': idle, 'idle_source': source,
        'away': None if idle is None else idle >= AWAY_AFTER,
        'supervisor_running': running, 'supervisor_pid': pid,
        'warning': None if running else 'supervisor not running: the mode has no effect on conversions',
    }
