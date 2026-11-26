#!/usr/bin/env python3
"""Supervisor of the two pipeline runners: pauses them while the screen is on, advances sets between review gates.

    python3 tools/pipeline_supervisor.py run [--dry-run]     # the loop (single instance); --dry-run: one pass, changes nothing
    python3 tools/pipeline_supervisor.py status
    python3 tools/pipeline_supervisor.py mode [auto|pause|pause-conversions|views-run|run]   # manual override (stored in supervisor_mode.json); auto = follow the user
    python3 tools/pipeline_supervisor.py accept-drift --note "..." IMAGE...   # reviewed colour drift: stop holding these images
    python tools/pipeline_supervisor.py stop                 # ask a running instance to exit cleanly (works everywhere; the
                                                             # way to stop a hidden Windows process: see "Windows" below)

User gate (every 30 s): the user is AWAY when the screen is locked/blanked (GNOME ScreenSaver GetActive) or there was
no keyboard/mouse input for AWAY_AFTER (5 min, Mutter IdleMonitor GetIdletime; a playing video, which keeps the screen
from locking, does not count as input). Away = run, at the computer = pause (user, 2026-10-27: the screen never
locked overnight, so the old screen-only rule paused all night). Pausing
writes .codex_jobs/_batch/PAUSE (json: since, reason, scope; every write / removal of PAUSE, supervisor_pause.json and
supervisor_mode.json happens under the paid-gate transition lock, gate_transition): convert_queue.py then starts nothing new, view_batch.py
only when the scope lists "views" (a manual `mode pause`, and a user-active pause since the 2026-10-29 user rule;
`mode pause-conversions` keeps view generation running; `mode views-run` (user, 2026-11-14) follows the user for
conversions only, so views generate at any time and conversions run while the user is away), and
convert_job.py starts no paid step. "Finish, then freeze": every descendant of a running convert_job.py OF THIS CHECKOUT
(its script path, resolved against the process's working directory, is <root>/tools/tripo_pipeline/convert_job.py) that is
blender, or a python running a script under tools/tripo_pipeline/ (other than convert_job.py), is SIGSTOPped (re-checked each
tick); network steps (codex image jobs, Hyper3D/rodin_backend, firefox, geckodriver, node studio.mjs, Xvfb) are never frozen.
Frozen pids (with start times) are in paused_pids.json. Resuming (or exiting, or SIGTERM) removes PAUSE and SIGCONTs exactly
the processes recorded there whose pid AND start time still match (nothing else, review_v2_phase4 finding 7). A process whose
resume FAILED stays on record and is resumed again every tick until it runs or is confirmed gone; a failed resume is reported as
`RESUME FAILED`, never as continued (post-cut-over review, blocker 6).

Whose PAUSE (post-cut-over review, blocker 1; DESIGN_PHASE4.md I.11): the supervisor's OWN pause is supervisor_pause.json (since,
reason, scope, pid, mirror), written whenever it pauses and removed when it resumes or exits. PAUSE is only its MIRROR for the
readers of that file, and the supervisor writes, rewrites or removes PAUSE only while PAUSE is its own: the bytes it last wrote
(`mirror` = their sha256), or, when there is no supervisor_pause.json at all (an older supervisor, or the PAUSE the app put back
after a restart), a file of exactly the supervisor's shape (only since / reason / scope, with one of its own reasons). Any other
PAUSE (a manual / maintenance pause) is left exactly as it is, also on resume and on exit: `PAUSE FOREIGN` is logged once.

Every completed screen tick writes .codex_jobs/_batch/supervisor_tick.json {pid, create_time, time (epoch s), at (ISO), mode,
paused}: the preview app removes its CONVERT_HOLD only after a tick of the current supervisor that is newer than the hold
(DESIGN_PHASE4 I.1). On shutdown the supervisor first waits for a running advance pass (a gate's workflow.py claim child runs
under this process's claim lock) and only then releases PAUSE, its single-instance lock and exits; a pass that is not inside a
claim is waited for at most ADVANCE_JOIN_TIMEOUT seconds (finding 9).

Auto-advance (every 3 min), only for sets matching a pattern in .codex_jobs/_batch/advance.json
({"sets": [{"pattern": "ragged_outfit_*", "extra_file": "...", "views_priority": 2}]}):
  Gate 1: all `fronts--<set>--<race>` tasks done and every front passes (framing flags, colour drift, size/RGBA, prompts json,
          design record) -> concepts/<set>/<race> moved to approved/<set>, workflow.py claim, view_batch.py add-views.
          Races already in a claimed job of the set are skipped unless a new front of theirs is in concepts/ (a redone
          gnome), and gnome fronts are gated and claimed on their own
          (state key `<set>#gnome`; add-views gets the entry's `gnome_extra_file` instead of `extra_file`), so a gnome
          front added to a set whose other races moved on long ago neither waits for nor holds up the others.
  Gate 2: every `views--*` task of a processing_views job done and all views pass -> workflow.py ready.
A colour drift a human reviewed and accepted is listed in advance.json `accepted_drift` ([{"sha256": .., "image": ..,
"note": ..}]) and no longer holds that exact image (a regenerated image has a new hash and is checked again).
Anything flagged is reported once as `SUPERVISOR HOLD` and waits for a human. All events go to events.log (SUPERVISOR ...).

Windows (the same file runs on both platforms; every difference sits behind IS_WIN): the user gate is GetLastInputInfo idle
time (away at >= AWAY_AFTER; a locked workstation gets no input, so it counts as away after 5 min); the single-instance lock
is msvcrt.locking on a byte at offset 1<<20 of supervisor.lock (the pid text stays readable for `status`); freezing uses
psutil suspend()/resume() instead of SIGSTOP/SIGCONT (psutil is required); children run with PYTHONUTF8=1 and no console
window. A hidden Windows process cannot catch SIGTERM, so `stop` writes .codex_jobs/_batch/supervisor.stop, which the loop
checks every second and exits through the normal path (PAUSE removed, frozen processes resumed); at startup any process
still recorded in paused_pids.json (a killed supervisor) is resumed.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from PIL import Image

IS_WIN = os.name == 'nt'
if IS_WIN:
    import msvcrt
else:
    import fcntl

ROOT = Path(__file__).resolve().parents[1]
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import paid_gate  # noqa: E402  (tools/paid_gate.py: the gate transition mutex for the PAUSE / mode writes)
import proc_cmdline  # noqa: E402  (tools/proc_cmdline.py: the interpreter entry-point parser)
sys.path.insert(0, str(ROOT / 'tools' / 'tripo_pipeline'))
import views_omitted  # noqa: E402
from claim_lock import ClaimBusy, claim_lock, release_lock, try_lock  # noqa: E402

try:
    import psutil
except ImportError:
    psutil = None

SCREEN_TICK, ADVANCE_TICK = 30, 180
ADVANCE_JOIN_TIMEOUT = 1200   # seconds shutdown waits for an advance pass outside a claim (a claim is always waited for)
TICK_FILE_NAME = 'supervisor_tick.json'
CLAIMED_STAGES = ('processing_views', 'multiview_ready', 'processing', 'completed')
# What a user-active pause stops (user, 2026-10-26: "we can generate the multi-views when the screen is on"); the image
# jobs run remotely and their local checks cost a few CPU-seconds per view. A manual `mode pause` stops both.
# User rule 2026-10-29 (GPU crash under full pipeline load): while the user is at the PC, pause
# image generation too - everything (views + conversions) runs only when the PC is idle.
SCREEN_ON_SCOPE = ('conversions', 'views')
VIEWS_RUN_ACTIVE = 'user active (views keep running)'   # mode views-run: the user-active pause covers conversions only
SIGSTOP, SIGCONT = getattr(signal, 'SIGSTOP', 'stop'), getattr(signal, 'SIGCONT', 'cont')   # Windows has no such signals
STOP_FILE_NAME = 'supervisor.stop'
CLAIM_LOCK_ENV = 'IMAGETO3D_CLAIM_LOCK_HELD'   # <set>: told to the `workflow.py claim` child of a caller that already holds the set's claim lock
CLAIM_LOCK_WAIT = 20.0   # seconds gate 1 waits for the preview app (an unapprove) to release a set's claim lock before it tries again next pass
LOCK_OFFSET = 1 << 20   # Windows: the single-instance lock is this byte, far past the pid text (a locked byte cannot be read)
MODES = ('auto', 'pause', 'pause-conversions', 'views-run', 'run')   # pause-conversions: a manual pause that keeps views running
AWAY_AFTER = 300   # seconds without keyboard/mouse input = away (GNOME's own idle-delay)
VIEWS = views_omitted.VIEWS
GDBUS = ['gdbus', 'call', '--session', '--dest', 'org.gnome.ScreenSaver', '--object-path', '/org/gnome/ScreenSaver',
         '--method', 'org.gnome.ScreenSaver.GetActive']
IDLE_GDBUS = ['gdbus', 'call', '--session', '--dest', 'org.gnome.Mutter.IdleMonitor', '--object-path',
              '/org/gnome/Mutter/IdleMonitor/Core', '--method', 'org.gnome.Mutter.IdleMonitor.GetIdletime']
BODY_RED_NOTE = 'POSE: body_red shifted'   # allowed on right_gauntlet_boot (the one-sided module shifts the red centroid)


def json_bytes(data):
    return (json.dumps(data, indent=2) + '\n').encode('utf-8')


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_bytes(json_bytes(data))
    tmp.replace(path)


OWN_PAUSE_NAME = paid_gate.SUPERVISOR_PAUSE   # supervisor_pause.json: the supervisor's own pause (PAUSE is only its mirror)
OWN_PAUSE_REASONS = ('user active', VIEWS_RUN_ACTIVE, 'manual pause', 'manual pause-conversions')   # what desired_pause() says
PAUSE_SHAPE = {'since', 'reason', 'scope'}    # the fields of a PAUSE the supervisor writes


def read_bytes(path):
    """The bytes of `path`, None when it does not exist (or cannot be read: then it is not ours to touch)."""
    try:
        return path.read_bytes()
    except OSError:
        return None


def pause_is_own(own, pause_bytes):
    """Whether the PAUSE file with these bytes is the supervisor's (see the module docstring, "Whose PAUSE"). `own` = the content
    of supervisor_pause.json, None when that file does not exist."""
    if pause_bytes is None:
        return False
    if isinstance(own, dict):                     # we have a record: ours only when it is exactly what we wrote
        return own.get('mirror') == hashlib.sha256(pause_bytes).hexdigest()
    if own is not None:                           # an unreadable record: nothing is known to be ours
        return False
    try:                                          # no record at all: an older supervisor's file, by its shape
        data = json.loads(pause_bytes.decode('utf-8-sig'))
    except ValueError:
        return False
    return (isinstance(data, dict) and set(data) <= PAUSE_SHAPE and data.get('reason') in OWN_PAUSE_REASONS
            and isinstance(data.get('scope'), list))


GATE_LOCK_TRIES, GATE_LOCK_WAIT = 4, 10.0   # tries x seconds a gate write waits for the transition lock


def gate_transition(root, action, what):
    """Run `action()` (a WRITE or REMOVAL of PAUSE / supervisor_pause.json / supervisor_mode.json) under the paid-gate transition
    lock of `root` (tools/paid_gate.py transition_lock; last-check review row 6): a paid click's final gate check and its
    dispatch hold the same lock, so the gate file never changes between the two. The lock is an OS byte lock that never expires
    (post-cut-over review, blocker 2): a busy one is retried (GATE_LOCK_TRIES), and when it still is busy paid_gate.GateBusy is
    raised and NOTHING is written (the callers report it and try again; a transition that is not serialized with a click in
    flight is never passed off as made)."""
    for _ in range(GATE_LOCK_TRIES):
        try:
            lock = paid_gate.transition_lock(root, wait=GATE_LOCK_WAIT, what=what)
            lock.__enter__()
        except paid_gate.GateBusy:
            continue
        try:
            return action()
        finally:
            lock.__exit__(None, None, None)
    raise paid_gate.GateBusy(f'the paid-gate transition lock stayed busy; not done: {what}')


class advance_lock:
    """The byte lock of .codex_jobs/_batch/advance.lock around every read-modify-write of advance.json (shared with the preview
    app's drift Accept, tools/preview/v2/actions.py; same claim_lock byte convention). Waits up to `wait` seconds."""

    def __init__(self, advance_file, wait=30.0):
        self.path, self.wait, self.handle = Path(advance_file).with_name('advance.lock'), wait, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, 'a+b')
        deadline = time.monotonic() + self.wait
        while not try_lock(handle):
            if time.monotonic() >= deadline:
                handle.close()
                raise TimeoutError(f'{self.path}: another process is writing advance.json')
            time.sleep(0.05)
        self.handle = handle
        return self

    def __exit__(self, *exc):
        handle, self.handle = self.handle, None
        if handle:
            try:
                release_lock(handle)
            finally:
                handle.close()


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default


def norm(text):
    """Forward-slash form of a path (or of paths joined by spaces): task JSONs written on Windows carry backslashes."""
    return text.replace('\\', '/')


def win_idle_seconds():
    """Windows: seconds since the last keyboard/mouse input (GetLastInputInfo), None when it cannot be read."""
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]

    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    ctypes.windll.kernel32.GetTickCount.restype = wintypes.DWORD
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def lock_try(handle):
    """Take the exclusive non-blocking single-instance lock on an open lock file; False when someone holds it."""
    if IS_WIN:
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


def lock_release(handle):
    if IS_WIN:
        handle.seek(LOCK_OFFSET)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.seek(0)
    else:
        fcntl.flock(handle, fcntl.LOCK_UN)


def parse_screen(text):
    """True when the screen is blanked/locked, False when it is on, None when the output is not a GetActive reply."""
    found = re.search(r'\(\s*(true|false)\s*,?\s*\)', text or '')
    return None if not found else found.group(1) == 'true'


def parse_idle(text):
    """Seconds since the last keyboard/mouse input from a GetIdletime reply ("(uint64 1234,)" in ms), None if unreadable."""
    found = re.search(r'\(\s*(?:uint64\s+)?(\d+)\s*,?\s*\)', text or '')
    return None if not found else int(found.group(1)) / 1000


def own_create_time():
    """This process's creation time as psutil reports it (None without psutil)."""
    try:
        return psutil.Process(os.getpid()).create_time() if psutil else None
    except Exception:   # noqa: BLE001
        return None


def process_table():
    """Rows {pid, ppid, comm, cmdline, stopped, start} of every process (psutil if installed, else /proc)."""
    rows = []
    if psutil:
        for proc in psutil.process_iter(['pid', 'ppid', 'name', 'cmdline', 'status', 'create_time']):
            info = proc.info
            rows.append({'pid': info['pid'], 'ppid': info['ppid'] or 0, 'comm': info['name'] or '',
                         'cmdline': info['cmdline'] or [], 'stopped': info['status'] in ('stopped', 'tracing-stop'),
                         'start': info['create_time'] or 0})
        return rows
    if IS_WIN:
        raise RuntimeError('psutil is required on Windows (python -m pip install psutil)')
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f'/proc/{entry}/stat').read_text()
            head, _, tail = stat.rpartition(')')
            fields = tail.split()
            cmdline = Path(f'/proc/{entry}/cmdline').read_bytes().split(b'\0')
        except OSError:
            continue
        rows.append({'pid': int(entry), 'ppid': int(fields[1]), 'comm': head.partition('(')[2],
                     'cmdline': [c.decode(errors='replace') for c in cmdline if c], 'stopped': fields[0] in 'Tt',
                     'start': int(fields[19])})
    return rows


def resume_fully(proc):
    """Windows: resume every thread until its suspend count is zero (one psutil resume() undoes one suspend only)."""
    import ctypes
    kernel = ctypes.windll.kernel32
    proc.resume()
    try:
        threads = proc.threads()
    except (AttributeError, psutil.Error):   # process gone (or a test double without threads)
        return
    for thread in threads:
        handle = kernel.OpenThread(0x0002, False, thread.id)   # THREAD_SUSPEND_RESUME
        if not handle:
            continue
        try:
            for _ in range(64):
                previous = kernel.ResumeThread(handle)
                if previous in (0, 1, 0xFFFFFFFF):   # was running / now running / error
                    break
        finally:
            kernel.CloseHandle(handle)


def script_of(row):
    """The script a python row's interpreter actually runs, '' when none (phase-4 finding 7): its command line is parsed like the
    interpreter does (tools/proc_cmdline.py): options such as `-X utf8 -u` are skipped, `-c` / `-m` / stdin run no script file, and
    an argument after the script (e.g. a checkout path given to another program as data) is never taken for it."""
    script, _ = proc_cmdline.python_script(row.get('cmdline') or [])
    return script or ''


def row_cwd(row):
    """Working directory of a table row's process: row['cwd'] when the row carries one, else asked from the OS for the SAME
    process (start time checked; a reused pid gives None); None when it cannot be read."""
    if 'cwd' in row:
        return row['cwd']
    try:
        if psutil:
            proc = psutil.Process(row['pid'])
            if abs(proc.create_time() - row['start']) >= 1:
                return None
            return proc.cwd()
        if IS_WIN:
            return None
        fields = Path(f'/proc/{row["pid"]}/stat').read_text().rpartition(')')[2].split()
        if int(fields[19]) != row['start']:
            return None
        return os.readlink(f'/proc/{row["pid"]}/cwd')
    except Exception:   # noqa: BLE001  gone, access denied, no such pid
        return None


def norm_path(path):
    return os.path.normcase(os.path.realpath(str(path)))


def script_path(row):
    """The resolved script path of a python row (relative paths against its working directory), None when unknown."""
    script = script_of(row)
    if not script:
        return None
    path = Path(script)
    if not path.is_absolute():
        cwd = row_cwd(row)
        if not cwd:
            return None
        path = Path(cwd) / path
    return norm_path(path)


def heavy_processes(rows, root=None):
    """Local CPU-heavy steps to freeze: descendants of a running convert_job.py that are blender or a python running a
    tools/tripo_pipeline/ script other than convert_job.py. Never hyper3d/node/firefox/geckodriver/Xvfb/codex. With `root`
    only convert_job.py processes of THAT checkout count (their resolved script path is <root>/tools/tripo_pipeline/
    convert_job.py; an unknown path does not count), so another checkout's conversion is never frozen."""
    children = {}
    for row in rows:
        children.setdefault(row['ppid'], []).append(row)
    target = None if root is None else norm_path(Path(root) / 'tools' / 'tripo_pipeline' / 'convert_job.py')
    roots = [r for r in rows if r['comm'].startswith('python') and Path(script_of(r)).name == 'convert_job.py'
             and (target is None or script_path(r) == target)]
    found, seen, stack = [], set(), [r['pid'] for r in roots]
    while stack:
        for child in children.get(stack.pop(), []):
            if child['pid'] in seen:
                continue
            seen.add(child['pid'])
            stack.append(child['pid'])
            script = Path(script_of(child))
            if child['comm'].startswith('blender') or (
                    child['comm'].startswith('python') and 'tripo_pipeline' in script.parts and script.name != 'convert_job.py'):
                found.append(child)
    return found


def same_process(row, record):
    return row['pid'] == record['pid'] and abs(row['start'] - record['start']) < 1


def sha256(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def file_sig(paths):
    digest = hashlib.sha1()
    for path in sorted(paths):
        try:
            stat = path.stat()
            digest.update(f'{path}:{stat.st_size}:{stat.st_mtime_ns};'.encode())
        except OSError:
            digest.update(f'{path}:-;'.encode())
    return digest.hexdigest()


def real_flags(flags, module):
    return [f for f in flags if not (module == 'right_gauntlet_boot' and BODY_RED_NOTE in f)]


class Supervisor:
    def __init__(self, root=ROOT, dry_run=False):
        self.root, self.dry_run = Path(root), dry_run
        self.batch = self.root / '.codex_jobs' / '_batch'
        self.events, self.pause_file = self.batch / 'events.log', self.batch / 'PAUSE'
        self.mode_file, self.pids_file = self.batch / 'supervisor_mode.json', self.batch / 'paused_pids.json'
        self.state_file, self.advance_file = self.batch / 'supervisor_state.json', self.batch / 'advance.json'
        self.lock_file, self.stop_file = self.batch / 'supervisor.lock', self.batch / STOP_FILE_NAME
        self.tick_file = self.batch / TICK_FILE_NAME
        self.own_pause_file = self.batch / OWN_PAUSE_NAME   # this supervisor's own pause; PAUSE is its mirror only while it is ours
        self.claim_active = None   # set whose claim lock this process holds (whole claim: moves, claim child, add-views)
        self.paused = self.own_pause_file.exists() or self.pause_owned()
        self.stop, self.last_error, self.screen_failed = False, {}, False
        self.foreign_pause, self.resume_failures, self.reported_failures = None, [], None
        self.claim_held = None   # set whose claim lock this process holds while its claim child runs
        self.write_lock, self.sizes, self.cache = threading.Lock(), {}, {}
        self.state = read_json(self.state_file, {}) or {}
        self.state.setdefault('holds', {})
        self.state.setdefault('sets', {})
        self.thread = None

    # logging and small helpers
    def emit(self, line):
        stamp = f'{datetime.now():%H:%M:%S}'
        if self.dry_run:
            print(f'{stamp} [dry-run] {line}', flush=True)
            return
        text = f'{stamp} SUPERVISOR {line}'
        print(text, flush=True)
        with self.write_lock, self.events.open('a', encoding='utf-8', newline='\n') as handle:
            handle.write(text + '\n')

    def guard(self, name, func, *args):
        """Run func; log (when the message changes) and swallow any exception: the supervisor never crashes."""
        try:
            return func(*args)
        except Exception as error:   # noqa: BLE001
            message = f'{type(error).__name__}: {error}'[:300]
            if self.last_error.get(name) != message:
                self.last_error[name] = message
                try:
                    self.emit(f'ERROR {name}: {message}')
                except Exception:   # noqa: BLE001
                    pass

    def save_state(self):
        if not self.dry_run:
            with self.write_lock:
                atomic_write(self.state_file, self.state)

    def run_cmd(self, command, timeout=900):
        extra = ({'encoding': 'utf-8', 'errors': 'replace', 'creationflags': 0x08000000}   # CREATE_NO_WINDOW
                 if IS_WIN else {})
        return subprocess.run([str(c) for c in command], capture_output=True, text=True, cwd=self.root, timeout=timeout,
                              env=self.environment(), **extra)

    def environment(self):
        env = dict(os.environ)
        if IS_WIN:
            env['PYTHONUTF8'] = '1'
        else:
            env.setdefault('DBUS_SESSION_BUS_ADDRESS', f'unix:path=/run/user/{os.getuid()}/bus')
        if self.claim_held:
            env[CLAIM_LOCK_ENV] = self.claim_held
        return env

    def mode(self):
        mode = (read_json(self.mode_file, {}) or {}).get('mode', 'auto')
        return mode if mode in MODES else 'auto'

    # screen gate and freezing
    def user_away(self):
        """True when the screen is locked/blanked or idle for AWAY_AFTER, False at the computer, None unknown."""
        return self.away_windows() if IS_WIN else self.away_gnome()

    def away_windows(self):
        """Idle time only: a locked workstation gets no input, so it counts as away after AWAY_AFTER like a blank screen."""
        idle = win_idle_seconds()
        return None if idle is None else idle >= AWAY_AFTER

    def away_gnome(self):
        replies = []
        for command, parse in ((GDBUS, parse_screen), (IDLE_GDBUS, parse_idle)):
            try:
                replies.append(parse(self.run_cmd(command, timeout=10).stdout))
            except (OSError, subprocess.SubprocessError):
                replies.append(None)
        locked, idle = replies
        if locked or (idle is not None and idle >= AWAY_AFTER):
            return True
        return False if idle is not None or locked is False else None

    def desired_pause(self):
        """(paused, reason) for the current mode; keeps the previous state when the screen cannot be read."""
        mode = self.mode()
        if mode not in ('auto', 'views-run'):
            return mode != 'run', f'manual {mode}'
        active = 'user active' if mode == 'auto' else VIEWS_RUN_ACTIVE
        away = self.user_away()
        if away is None:
            if not self.screen_failed:
                self.emit('SCREEN state unreadable (GetLastInputInfo failed); keeping the previous state' if IS_WIN else
                          'SCREEN state unreadable (gdbus GetActive/GetIdletime failed); keeping the previous state')
            self.screen_failed = True
            return self.paused, active if self.paused else 'user away'
        self.screen_failed = False
        return not away, active if not away else 'user away'

    def recorded(self):
        return read_json(self.pids_file, []) or []

    def freeze(self, rows=None):
        """SIGSTOP the running heavy descendants of convert_job.py; returns the newly frozen rows. Each signal is sent only after the
        pid is verified to be the same process (create time) as the table row (send(..., start=))."""
        rows = process_table() if rows is None else rows
        kept = [r for r in self.recorded() if any(same_process(row, r) for row in rows)]
        new = []
        for row in heavy_processes(rows, self.root):
            # Windows suspends are counted and psutil does not always report a suspended process as stopped, so a
            # process already on record is never suspended a second time (2026-11-16: body_fit.py was frozen three
            # times, resumed twice and stayed suspended for six hours)
            if row['stopped'] or any(same_process(row, r) for r in kept):
                continue
            try:
                self.send(row['pid'], SIGSTOP, start=row['start'])
            except OSError:
                continue
            kept.append({'pid': row['pid'], 'start': row['start'], 'comm': row['comm']})
            new.append(row)
        if not self.dry_run and (new or kept != self.recorded()):
            atomic_write(self.pids_file, kept)
        return new

    def resume(self, rows=None):
        """SIGCONT exactly the processes recorded in paused_pids.json whose pid and start time still match (identities this
        supervisor suspended); a stopped process it did not record is never touched. Returns the pids resumed.
        Post-cut-over review, blocker 6: a record leaves paused_pids.json only when its process was resumed or is CONFIRMED GONE
        (no process with that pid, or the pid belongs to a process with another start time). A resume that failed keeps its
        record, so every later tick (and the next start) tries again; `self.resume_failures` = [(record, error text)] of this
        call, and the failed pids are never part of the returned list."""
        rows = process_table() if rows is None else rows
        resumed, kept, failures = [], [], []
        for record in self.recorded():
            if not isinstance(record, dict) or 'pid' not in record or 'start' not in record:
                continue
            row = next((row for row in rows if same_process(row, record)), None)
            if row is None:
                if not self.confirmed_gone(record, rows):   # there, but its identity could not be read: not ours to drop
                    kept.append(record)
                    failures.append((record, 'its identity could not be read'))
                continue
            try:
                self.send(row['pid'], SIGCONT, start=row['start'])
            except OSError as error:
                try:
                    gone = self.confirmed_gone(record, process_table())   # it may have ended just now
                except Exception:   # noqa: BLE001  (no process table: not confirmed)
                    gone = False
                if not gone:
                    kept.append(record)
                    failures.append((record, str(error)[:200]))
                continue
            resumed.append(row['pid'])
        self.resume_failures = failures
        if not self.dry_run:
            if kept:
                atomic_write(self.pids_file, kept)
            else:
                self.pids_file.unlink(missing_ok=True)
        return sorted(resumed)

    @staticmethod
    def confirmed_gone(record, rows):
        """The recorded process no longer exists: no row has its pid, or the pid now belongs to a process with a KNOWN other start
        time. A row with that pid whose start time is unknown (0: the process could not be inspected) does not confirm it."""
        same_pid = [row for row in rows if row['pid'] == record['pid']]
        return not any(same_process(row, record) or not row['start'] for row in same_pid)

    def resume_report(self):
        """' ; N could not be resumed ...' for the RESUME line, and one RESUME FAILED event per change of the failing set."""
        failed = sorted(record['pid'] for record, _ in self.resume_failures)
        if failed != self.reported_failures:
            self.reported_failures = failed
            if failed:
                self.emit('RESUME FAILED ' + ', '.join(f"{record['pid']}:{record.get('comm', '?')} ({error})"
                                                       for record, error in self.resume_failures)
                          + '; kept in paused_pids.json and tried again every tick until resumed or gone')
        return f'; {len(failed)} process(es) could NOT be resumed and stay on record' if failed else ''

    def send(self, pid, sig, start=None):
        """Deliver SIGSTOP / SIGCONT to `pid`. With `start` (the process table's create time of the process meant) the identity is
        verified right before delivery, on the handle that delivers it: a pid reused by another process raises OSError and nothing
        is signalled (phase-4 finding 7)."""
        if IS_WIN:   # SIGSTOP/SIGCONT: suspend/resume the process (psutil errors become OSError, as os.kill raises)
            try:
                proc = psutil.Process(pid)
                if start is not None and abs(proc.create_time() - start) >= 1:
                    raise OSError(f'pid {pid} is no longer the process that was meant (create time changed)')
                if sig == SIGSTOP:
                    proc.suspend()
                else:
                    resume_fully(proc)
            except psutil.Error as error:
                raise OSError(str(error)) from error
            return
        if start is not None:
            if psutil:
                try:
                    proc = psutil.Process(pid)
                    if abs(proc.create_time() - start) >= 1:
                        raise OSError(f'pid {pid} is no longer the process that was meant (create time changed)')
                    proc.send_signal(sig)   # psutil re-checks that the handle's process is still running before it signals
                except psutil.Error as error:
                    raise OSError(str(error)) from error
                return
            fields = Path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()   # OSError when it is gone
            if int(fields[19]) != start:
                raise OSError(f'pid {pid} is no longer the process that was meant (start time changed)')
        os.kill(pid, sig)

    def tick_screen(self):
        paused, reason = self.desired_pause()
        if paused:
            scope = (list(SCREEN_ON_SCOPE) if reason == 'user active'
                     else ['conversions'] if reason in ('manual pause-conversions', VIEWS_RUN_ACTIVE) else ['conversions', 'views'])
            written = True
            try:
                self.write_pause(reason, scope)
            except paid_gate.GateBusy as error:   # a paid click is being dispatched: not written, tried again next tick
                written = False
                self.guard_note('pause-write', f'PAUSE ({reason}) not written yet: {error}')
            if written and not self.paused:
                self.emit(f'PAUSE ({reason}): {" and ".join(scope)} start nothing new; heavy local steps freeze')
            frozen = self.freeze()
            if frozen:
                self.emit('FREEZE ' + ', '.join(f"{r['pid']}:{r['comm']}" for r in frozen))
            self.paused = written or self.paused
            return True
        if self.paused or self.own_pause_file.exists() or self.pids_file.exists() or self.pause_owned():
            was_paused = self.paused or self.own_pause_file.exists() or self.pause_owned()
            try:
                self.remove_pause('supervisor PAUSE removed')
            except paid_gate.GateBusy as error:   # still paused: nothing is resumed before the pause is really gone
                self.guard_note('pause-write', f'PAUSE not removed yet: {error}')
                return True
            resumed = self.resume()
            note = self.resume_report()
            if was_paused or resumed:
                self.emit(f'RESUME ({reason}): PAUSE removed' + (f', continued {len(resumed)} process(es)' if resumed else '') + note)
        self.paused = False
        return True

    def guard_note(self, name, message):
        """Log `message` as an ERROR once per change (like guard, for a failure that is handled)."""
        if self.last_error.get(name) != message:
            self.last_error[name] = message
            self.emit(f'ERROR {name}: {message}')

    def pause_owned(self):
        """PAUSE exists and is this supervisor's own mirror (pause_is_own)."""
        own = read_json(self.own_pause_file, {}) if self.own_pause_file.exists() else None
        return pause_is_own(own, read_bytes(self.pause_file))

    def write_pause(self, reason, scope):
        """Make the supervisor's own pause {reason, scope} current: supervisor_pause.json always, and PAUSE as its mirror unless
        a PAUSE somebody else wrote is there (left untouched; blocker 1). One read-decide-write under the gate transition lock;
        nothing is written when everything already is as wanted. Raises paid_gate.GateBusy when the lock stays busy."""
        def current():
            own = read_json(self.own_pause_file, {}) if self.own_pause_file.exists() else None
            pause = read_bytes(self.pause_file)
            if pause is None and self.pause_file.exists():
                pause = b''                               # unreadable: foreign
            mine = pause is None or pause_is_own(own, pause)
            since = own.get('since') if isinstance(own, dict) else None
            if since is None and mine and pause:
                try:
                    since = (json.loads(pause.decode('utf-8-sig')) or {}).get('since')
                except (ValueError, AttributeError):
                    since = None
            want = {'since': since or datetime.now().isoformat(timespec='seconds'), 'reason': reason, 'scope': scope}
            mirror = hashlib.sha256(json_bytes(want)).hexdigest() if mine else None
            record = dict(want, pid=os.getpid(), mirror=mirror)
            return own, pause, mine, want, record

        own, pause, mine, want, record = current()
        if own == record and (not mine or pause == json_bytes(want)):
            self.note_foreign(None if mine else pause)
            return

        def action():
            own, pause, mine, want, record = current()    # decided again under the lock
            if own != record:
                atomic_write(self.own_pause_file, record)
            if mine and pause != json_bytes(want):
                atomic_write(self.pause_file, want)
            return None if mine else pause
        self.note_foreign(gate_transition(self.root, action, f'supervisor PAUSE ({reason})'))

    def note_foreign(self, pause):
        """Log once that a PAUSE of somebody else is in force and left alone (`pause` = its bytes, None when PAUSE is ours)."""
        digest = hashlib.sha256(pause).hexdigest() if pause is not None else None
        if digest != self.foreign_pause:
            self.foreign_pause = digest
            if digest:
                reason = (read_json(self.pause_file, {}) or {}).get('reason') if self.pause_file.exists() else None
                self.emit(f'PAUSE FOREIGN ({reason or "no reason"}): a PAUSE this supervisor did not write is in force; it is left '
                          'untouched (the supervisor\'s own pause is in supervisor_pause.json)')

    def remove_pause(self, what):
        """End the supervisor's own pause: remove supervisor_pause.json, and PAUSE only when it is our own mirror (a PAUSE of
        somebody else stays; blocker 1). Under the gate transition lock; raises paid_gate.GateBusy when it stays busy."""
        def action():
            own = read_json(self.own_pause_file, {}) if self.own_pause_file.exists() else None
            if pause_is_own(own, read_bytes(self.pause_file)):
                self.pause_file.unlink(missing_ok=True)
            self.own_pause_file.unlink(missing_ok=True)
        if self.own_pause_file.exists() or self.pause_file.exists():
            gate_transition(self.root, action, what)
        if self.pause_file.exists():
            self.note_foreign(read_bytes(self.pause_file) or b'')

    def write_tick(self):
        """supervisor_tick.json after a completed screen tick (the preview app's CONVERT_HOLD release waits for one)."""
        if self.dry_run:
            return
        atomic_write(self.tick_file, {'pid': os.getpid(), 'create_time': own_create_time(), 'time': time.time(),
                                      'at': datetime.now().isoformat(timespec='seconds'), 'mode': self.mode(),
                                      'paused': bool(self.paused)})

    def resume_stale(self):
        resumed = self.resume()
        self.emit(f'RESUME at start: continued {len(resumed)} process(es) left suspended by an earlier instance' + self.resume_report())

    def release_all(self):
        try:
            self.remove_pause('supervisor exit: PAUSE removed')
        except paid_gate.GateBusy as error:   # left in place (a pause too many is safe); the next instance takes it over
            self.guard_note('pause-write', f'PAUSE not removed on exit: {error}')
        resumed = self.resume()
        note = self.resume_report()           # what could not be resumed stays in paused_pids.json for the next start
        if resumed or note:
            self.emit(f'RESUME on exit: continued {len(resumed)} process(es)' + note)

    def gate_promoted(self):
        """User-promoted sets (two-stage approval, 2026-10-28): a set the user moved into approved/ without a
        job record is claimed at once, its views are queued (all four views; the concept art is reference only)
        and the other races' front tasks are created. Gate 1 takes over when those fronts finish."""
        approved = self.root / 'approved'
        if not approved.is_dir():
            return
        for folder in sorted(approved.iterdir()):
            if self.stop:   # shutting down: start no new claim
                return
            if not folder.is_dir() or folder.name.startswith('.') or not re.fullmatch(r'[a-z0-9][a-z0-9_]*', folder.name):
                continue
            if (folder / '.tripo-job.json').exists():
                continue   # already claimed (gate 1 second claim or an earlier run)
            set_id = folder.name
            if (self.root / 'concepts' / set_id).is_dir():
                continue   # a concepts/approved clash, not a promotion: gate 1 holds on it
            key = f'promoted:{set_id}'
            record = self.state['sets'].get(key) or {}
            if record.get('job') or record.get('claim_failed'):
                continue
            if self.dry_run:
                self.emit(f'WOULD CLAIM promoted {set_id}')
                continue
            run = self.run_cmd([sys.executable, self.root / 'tools' / 'tripo_pipeline' / 'workflow.py', 'claim', set_id])
            if run.returncode != 0:
                self.state['sets'][key] = {'claim_failed': True, 'error': self.tail(run)}
                self.save_state()
                self.emit(f'FAILED claim promoted {set_id} (set stays in approved/): {self.tail(run)}')
                continue
            job = Path(json.loads(run.stdout)['path'])
            self.state['sets'][key] = {'job': job.name, 'path': str(job), 'views_added': False, 'promoted': True}
            self.save_state()
            self.emit(f'CLAIMED promoted {set_id} -> {job.name}')
            entry = self.advance_entry(set_id) or {}
            self.add_views(key, entry)
            have = {p_.name for p_ in job.iterdir() if p_.is_dir() and not p_.name.startswith('.')}
            races = sorted(r.name for r in (self.root / 'base_views').iterdir()
                           if r.is_dir() and (r / 'palette.json').is_file() and r.name not in have)
            # the set's rules file goes into the other-race fronts too (plate section colours); gnomes get their own
            gnomes = [r for r in races if r.startswith('gnome')]
            for group, extra in (([r for r in races if r not in gnomes], entry.get('extra_file')),
                                 (gnomes, entry.get('gnome_extra_file'))):
                if not group:
                    continue
                front_cmd = [sys.executable, self.root / 'tools' / 'view_batch.py', 'add-fronts', set_id,
                             '--design-job', str(job), '--races', ','.join(group)]
                if extra:
                    front_cmd += ['--extra-file', self.root / extra]
                frun = self.run_cmd(front_cmd)
                if frun.returncode != 0:
                    self.emit(f'FAILED add-fronts for promoted {set_id}: {self.tail(frun)}')
                else:
                    self.emit(f'QUEUED {len(group)} race fronts for promoted {set_id}')

    # auto-advance
    def advance_entry(self, set_id):
        set_id = set_id.split('#', 1)[0].removeprefix('promoted:')   # state keys: <set>#gnome, promoted:<set>
        for entry in (read_json(self.advance_file, {}) or {}).get('sets', []):
            if fnmatch.fnmatchcase(set_id, entry.get('pattern', '')):
                return entry
        return None

    def task_index(self):
        """{(kind, set_id): [task name]} for fronts-- and views-- tasks of sets that are opted in."""
        groups = {}
        for path in (self.batch / 'tasks').glob('*.json'):
            parts = path.stem.split('--')
            if parts[0] in ('fronts', 'views') and len(parts) >= 3 and self.advance_entry(parts[1]):
                groups.setdefault((parts[0], parts[1]), []).append(path.stem)
        return groups

    def load_task(self, name):
        if name not in self.cache:
            self.cache[name] = (read_json(self.batch / 'tasks' / f'{name}.json'),
                                read_json(self.batch / 'status' / f'{name}.json') or {'state': 'pending'})
        return self.cache[name]

    def status_paths(self, names):
        return [self.batch / 'status' / f'{n}.json' for n in names]

    def hold(self, key, reasons, sig):
        """Report a hold once per (signature, reasons); returns False."""
        value = {'sig': sig, 'reasons': reasons}
        if self.dry_run or self.state['holds'].get(key) != value:
            self.emit(f'HOLD {key}: ' + '; '.join(reasons))
            if not self.dry_run:
                self.state['holds'][key] = value
                self.save_state()
        return False

    def clear_hold(self, key):
        if self.state['holds'].pop(key, None) is not None:
            self.save_state()

    def held_unchanged(self, key, sig):
        return not self.dry_run and (self.state['holds'].get(key) or {}).get('sig') == sig

    def base_size(self, path):
        if path not in self.sizes:
            with Image.open(path) as image:
                self.sizes[path] = image.size
        return self.sizes[path]

    def image_problem(self, path, base):
        try:
            size = self.base_size(base)
            with Image.open(path) as image:
                image.load()
                if image.mode != 'RGBA':
                    return f'{path.name} is {image.mode}, not RGBA'
                if image.size != size:
                    return f'{path.name} is {image.size[0]}x{image.size[1]}, base is {size[0]}x{size[1]}'
        except Exception as error:   # noqa: BLE001
            return f'{path.name} unreadable ({type(error).__name__}: {error})'
        return None

    def colour_problems(self, palette_dir, images):
        if not images:
            return []
        run = self.run_cmd([sys.executable, self.root / 'tools' / 'check_concept_colours.py', '--palette', palette_dir,
                            '--json', *images], timeout=600)
        try:
            results = json.loads(run.stdout)
        except ValueError:
            return [f'colour check failed (exit {run.returncode}): {" | ".join(run.stderr.strip().splitlines()[-2:])}']
        accepted = self.accepted_drift()
        return [f"{Path(r['image']).name} colour DRIFT ({'; '.join(r['problems'])})" for r in results
                if not r['ok'] and sha256(Path(r['image'])) not in accepted]

    def gate_sig(self, paths):
        """Signature of a gate's inputs; an accepted drift added later re-opens the hold."""
        return file_sig(paths) + ':' + hashlib.sha1(' '.join(sorted(self.accepted_drift())).encode()).hexdigest()[:12]

    def accepted_drift(self):
        """sha256 of images whose colour drift a human reviewed and accepted (advance.json `accepted_drift`)."""
        entries = (read_json(self.advance_file, {}) or {}).get('accepted_drift', [])
        return {e['sha256'] for e in entries if isinstance(e, dict) and e.get('sha256')}

    # gate 1
    def front_reasons(self, set_id, race, task, status):
        reasons = []
        folder = self.root / 'concepts' / set_id / race
        base = self.root / 'base_views' / race / 'front.png'
        images = []
        for output in task['outputs']:
            out = Path(norm(output))
            if out.suffix != '.png':
                continue
            module = out.stem.removesuffix('_front')
            if not out.is_file():
                reasons.append(f'{race}: {out.name} missing')
                continue
            images.append(out)
            problem = self.image_problem(out, base)
            if problem:
                reasons.append(f'{race}: {problem}')
            # Concept fronts are one-shot and unchecked (user rule 2026-10-28): framing flags and colour drift
            # no longer gate a set here - drift is acceptable because the views are regenerated from the bases.
            prompts = read_json(folder / f'{module}_prompts.json')
            if not prompts:
                reasons.append(f'{race}: {module}_prompts.json missing, empty or invalid')
            try:
                record = (folder / f'{module}_design_record.md').read_text(encoding='utf-8-sig')
            except OSError:
                record = None
            if record is None or not re.search(r'^\W*Design:', record, re.M):
                reasons.append(f'{race}: {module}_design_record.md missing or without a Design: line')
        return reasons   # colour drift is not checked for one-shot concept fronts (user rule 2026-10-28)

    def claimed_races(self, set_id):
        """Races of `set_id` already in a claimed job (processing_views/ .. completed/): their fronts moved on."""
        key = ('claimed', set_id)
        if key not in self.cache:
            races = set()
            for stage in CLAIMED_STAGES:
                for job in (self.root / stage).glob(f'{set_id}--*'):
                    races |= {p.name for p in job.iterdir() if p.is_dir() and not p.name.startswith('.')}
            self.cache[key] = races
        return self.cache[key]

    def gate_fronts(self, groups):
        for (kind, set_id), names in sorted(groups.items()):
            if self.stop:   # shutting down: start no new claim
                return
            if kind != 'fronts' or not (self.root / 'concepts' / set_id).is_dir():
                continue
            # a done front whose race moved on (not in concepts/ any more, in a claimed job) is history; a new front in
            # concepts/ is gated even when an older version of that race was converted (a redone gnome)
            folder = self.root / 'concepts' / set_id
            names = [n for n in names if (folder / n.split('--')[2]).is_dir()
                     or n.split('--')[2] not in self.claimed_races(set_id)]
            gnome = [n for n in names if n.split('--')[2].startswith('gnome')]
            for key, part in ((set_id, [n for n in names if n not in gnome]), (f'{set_id}#gnome', gnome)):
                if part and key not in self.state['sets']:
                    self.gate_front_group(set_id, key, part)

    def extra_for(self, key, entry):
        return entry.get('gnome_extra_file') if key.endswith('#gnome') else entry.get('extra_file')

    def gate_front_group(self, set_id, key, names):
        entry = self.advance_entry(set_id)
        folder = self.root / 'concepts' / set_id
        loaded = {n: self.load_task(n) for n in names}
        states = {n: s.get('state') for n, (t, s) in loaded.items()}
        sig = self.gate_sig([p for d in folder.iterdir() if d.is_dir() for p in d.iterdir()] + self.status_paths(names))
        failed = sorted(n.split('--')[2] for n, s in states.items() if s == 'failed')
        if failed:
            self.hold(key, [f'fronts task failed: {", ".join(failed)}'], sig)
            return
        if any(s != 'done' for s in states.values()):
            self.clear_hold(key)
            return
        if self.held_unchanged(key, sig):
            return
        reasons = []
        extra = self.extra_for(key, entry)
        if extra and not (self.root / extra).is_file():
            reasons.append(f'extra file {extra} missing')
        for name, (task, status) in sorted(loaded.items()):
            race = name.split('--')[2]
            if not task or not (folder / race).is_dir():
                reasons.append(f'{race}: no concepts folder')
                continue
            reasons += self.front_reasons(set_id, race, task, status)
        if (self.root / 'approved' / set_id).exists():
            self.hold(key, reasons + ['approved/ already has this set'], sig)
        elif reasons:
            self.hold(key, reasons, sig)
        elif self.dry_run:
            self.emit(f'WOULD AUTO-CLAIM {key}: move {len(names)} race folder(s) to approved/, workflow.py claim, '
                      f'add-views --priority {entry.get("views_priority", 2)}')
        else:
            self.claim(set_id, entry, sorted(n.split('--')[2] for n in names), key)
            self.clear_hold(key)

    def claim(self, set_id, entry, races, key=None):
        """Move the race folders into approved/<set>, claim the set and queue its views. The set's claim lock
        (claim_lock.py, the one `workflow.py claim` and the preview app's unapprove use) is held from before the
        first move until the claim returns, so the preview app cannot take approved/<set> away half-claimed; the
        claim child is told (CLAIM_LOCK_ENV) that its caller holds the lock. A set the app is unapproving right now
        (lock busy) stays in concepts/ and is tried again on the next pass."""
        try:
            with claim_lock(self.root, set_id, wait=CLAIM_LOCK_WAIT):
                self.claim_held = self.claim_active = set_id
                try:
                    self.claim_locked(set_id, entry, races, key)
                finally:
                    self.claim_held = self.claim_active = None
        except ClaimBusy:
            self.emit(f'HOLD {set_id}: claim lock busy (the preview app is changing this set); trying again next pass')

    def claim_locked(self, set_id, entry, races, key=None):
        key = key or set_id
        approved = self.root / 'approved' / set_id
        approved.mkdir(parents=True)
        moved = []
        try:
            for race in races:
                (self.root / 'concepts' / set_id / race).rename(approved / race)
                moved.append(race)
        except OSError as error:
            for race in moved:
                (approved / race).rename(self.root / 'concepts' / set_id / race)
            approved.rmdir()
            self.emit(f'HOLD {set_id}: could not move the race folders: {error}')
            return
        try:
            (self.root / 'concepts' / set_id).rmdir()
        except OSError:
            pass
        job, error = None, ''
        try:
            run = self.run_cmd([sys.executable, self.root / 'tools' / 'tripo_pipeline' / 'workflow.py', 'claim', set_id])
            error = self.tail(run)
            if run.returncode == 0:
                job = Path(json.loads(run.stdout)['path'])
        except (ValueError, KeyError, subprocess.SubprocessError) as problem:
            error = error or repr(problem)
        self.claim_held = None   # the add-views child below needs no claim lock
        if job is None:
            self.state['sets'][key] = {'claim_failed': True}
            self.save_state()
            self.emit(f'FAILED claim {key} (folders are in approved/{set_id}): {error}')
            return
        self.state['sets'][key] = {'job': job.name, 'path': str(job), 'views_added': False}
        self.save_state()
        self.add_views(key, entry)

    def add_views(self, key, entry):
        record = self.state['sets'][key]
        command = [sys.executable, self.root / 'tools' / 'view_batch.py', 'add-views', record['path'],
                   '--priority', entry.get('views_priority', 2)]
        extra = self.extra_for(key, entry)
        if extra:
            command += ['--extra-file', self.root / extra]
        run = self.run_cmd(command)
        if run.returncode != 0:
            message = f'add-views {key} -> {record["job"]} failed (will retry): {self.tail(run)}'
            if record.get('error') != message:
                record['error'] = message
                self.save_state()
                self.emit('FAILED ' + message)
            return
        record['views_added'] = True
        record.pop('error', None)
        self.save_state()
        self.emit(f'AUTO-CLAIM {key} -> {record["job"]}')

    @staticmethod
    def tail(run):
        return ' | '.join((run.stderr or run.stdout or '').strip().splitlines()[-3:])

    def retry_views(self):
        for key, record in list(self.state['sets'].items()):
            if self.stop:
                return
            entry = self.advance_entry(key)
            if entry and record.get('path') and not record.get('views_added') and not self.dry_run:
                self.add_views(key, entry)

    # gate 2
    def view_reasons(self, job, tasks):
        reasons = []
        owner = {}
        for task, status in tasks:
            for output in task['outputs']:
                owner[norm(output)] = (task, status)
        for race_dir in sorted(p for p in job.iterdir() if p.is_dir() and not p.name.startswith('.')):
            race, palette = race_dir.name, job / '.base_views' / race_dir.name
            images = []
            for module_dir in sorted(p for p in race_dir.iterdir() if p.is_dir() and not p.name.startswith('.')):
                module = module_dir.name
                try:
                    omitted = (views_omitted.load(module_dir) or {}).get('omitted', [])
                except ValueError as error:
                    reasons.append(f'{race}/{module}: {error}')
                    continue
                wanted = [v for v in VIEWS if v not in omitted]
                actual = {p.stem for p in module_dir.glob('*.png')}
                reasons += [f'{race}/{module}: {v}.png missing' for v in wanted if v not in actual]
                if actual - set(wanted):
                    reasons.append(f'{race}/{module}: unexpected PNGs {sorted(actual - set(wanted))}')
                for view in (v for v in wanted if v in actual):
                    image = module_dir / f'{view}.png'
                    images.append(image)
                    problem = self.image_problem(image, palette / f'{view}.png')
                    if problem:
                        reasons.append(f'{race}/{module}: {problem}')
                    if view == 'front':
                        continue
                    found = next((v for k, v in owner.items() if k.endswith(f'/{race}/{module}/{view}.png')), None)
                    flags = ((found[1].get('checks') or {}).get(image.name)) if found else None
                    if flags is None:
                        reasons.append(f'{race}/{module}: no framing record for {view}.png')
                    elif real_flags(flags, module):
                        reasons.append(f'{race}/{module}: framing {view}.png: {"; ".join(real_flags(flags, module))}')
            drift = self.view_colour_drift(
                palette if (palette / 'palette.json').is_file() else self.root / 'base_views' / race, images)
            if drift:
                reasons += [f'{race}: {p}' for p in drift]
        return reasons

    def view_colour_drift(self, palette_dir, images):
        """colour_problems for views with unattended auto-waiver: a drifted view is recorded in advance.json
        accepted_drift (hash-bound, with a note) and no longer holds the set; a regenerated image has a new
        hash and is checked again. Enabled unless advance.json sets auto_accept_view_drift false."""
        config = read_json(self.advance_file, {}) or {}
        if config.get('auto_accept_view_drift') is False:
            return self.colour_problems(palette_dir, images)
        run = self.run_cmd([sys.executable, self.root / 'tools' / 'check_concept_colours.py', '--palette', palette_dir,
                            '--json', *images], timeout=600)
        try:
            results = json.loads(run.stdout)
        except ValueError:
            return [f'colour check failed (exit {run.returncode}): {" | ".join(run.stderr.strip().splitlines()[-2:])}']
        accepted = self.accepted_drift()
        problems = []
        for r in results:
            if r['ok'] or sha256(Path(r['image'])) in accepted:
                continue
            digest = sha256(Path(r['image']))
            if not self.dry_run:   # a dry run changes nothing, advance.json included
                with advance_lock(self.advance_file):   # re-read under the lock: the app's drift Accept may have written meanwhile
                    current = read_json(self.advance_file, {}) or {}
                    entries = current.setdefault('accepted_drift', [])
                    if digest not in {e.get('sha256') for e in entries if isinstance(e, dict)}:
                        entries.append({'sha256': digest, 'image': r['image'], 'note': 'auto-waived view colour drift (unattended '
                                        'run, known generator hue repaint); framing checks unaffected'})
                        atomic_write(self.advance_file, current)
            accepted.add(digest)
            self.emit(f'ACCEPTED colour drift {Path(r["image"]).name} (auto, hash-bound; see advance.json)')
        return problems

    def gate_views(self, groups):
        stage = self.root / 'processing_views'
        if not stage.is_dir():
            return
        for job in sorted(p for p in stage.iterdir() if p.is_dir()):
            if self.stop:
                return
            set_id = job.name.split('--')[0]
            if not self.advance_entry(set_id) or (read_json(job / '.tripo-job.json', {}) or {}).get('state') != 'generating_other_views':
                continue
            names = [n for n in groups.get(('views', set_id), [])
                     if f'/processing_views/{job.name}/' in norm(' '.join((self.load_task(n)[0] or {}).get('outputs', [])))]
            if not names:
                continue   # views not queued (yet)
            loaded = [self.load_task(n) for n in names]
            states = [s.get('state') for t, s in loaded]
            sig = self.gate_sig([p for d in job.iterdir() if d.is_dir() and not d.name.startswith('.') for m in d.iterdir()
                            if m.is_dir() for p in m.iterdir()] + self.status_paths(names))
            if 'failed' in states:
                bad = sorted(n.split('--', 1)[1] for n, st in zip(names, states) if st == 'failed')
                self.hold(job.name, [f'views task failed: {", ".join(bad)}'], sig)
                continue
            if any(st != 'done' for st in states):
                self.clear_hold(job.name)
                continue
            if self.held_unchanged(job.name, sig):
                continue
            reasons = self.view_reasons(job, loaded)
            if reasons:
                self.hold(job.name, reasons, sig)
            elif self.dry_run:
                self.emit(f'WOULD AUTO-READY {job.name}: workflow.py ready')
            else:
                run = self.run_cmd([sys.executable, self.root / 'tools' / 'tripo_pipeline' / 'workflow.py', 'ready', job.name])
                if run.returncode == 0:
                    self.clear_hold(job.name)
                    self.emit(f'AUTO-READY {job.name}')
                else:
                    self.hold(job.name, [f'workflow.py ready failed: {self.tail(run)}'], sig)

    def advance(self):
        self.cache = {}
        self.guard('promoted', self.gate_promoted)   # runs even with no advance.json sets
        if not (read_json(self.advance_file, {}) or {}).get('sets'):
            return
        groups = self.task_index()
        for name, func in (('retry-views', self.retry_views), ('gate1', lambda: self.gate_fronts(groups)),
                           ('gate2', lambda: self.gate_views(groups))):
            if self.stop:   # shutting down: the remaining gates wait for the next start
                return
            self.guard(name, func)

    # loop
    def acquire(self):
        self.batch.mkdir(parents=True, exist_ok=True)
        handle = self.lock_file.open('a+')
        if not lock_try(handle):
            handle.close()
            return None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        return handle

    def sleep(self, seconds):
        end = time.time() + seconds
        while not self.stop and time.time() < end:
            self.stop = self.stop_file.exists()
            if not self.stop:
                time.sleep(min(1, max(0, end - time.time())))

    def advance_async(self):
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self.guard, args=('advance', self.advance), daemon=True)
            self.thread.start()

    def drain_advance(self, timeout=ADVANCE_JOIN_TIMEOUT, poll=30):
        """Shutdown: wait for a running advance pass before PAUSE and the single-instance lock are released (finding 9). A
        pass inside a claim (moves, the workflow.py claim child that relies on this process's claim lock, add-views) is
        always waited for; anything else at most `timeout` seconds. Returns True when no pass runs any more."""
        thread = self.thread
        if thread is None or not thread.is_alive():
            return True
        self.emit(f'STOPPING: waiting for the running advance pass to finish'
                  f'{" (claim of " + self.claim_active + " in progress)" if self.claim_active else ""}')
        started = time.monotonic()
        while thread.is_alive():
            thread.join(poll)
            if not thread.is_alive():
                break
            if self.claim_active:
                continue   # never exit (and free the claim lock) while a claim runs
            if time.monotonic() - started >= timeout:
                self.emit(f'STOPPING: the advance pass is still running after {timeout} s outside any claim; exiting without it')
                return False
        self.emit(f'STOPPING: advance pass finished after {time.monotonic() - started:.0f} s')
        return True

    def run(self):
        handle = self.acquire()
        if handle is None:
            print('pipeline_supervisor is already running', file=sys.stderr)
            return 1
        for sig in (signal.SIGTERM, signal.SIGINT) + ((signal.SIGBREAK,) if IS_WIN else ()):
            signal.signal(sig, lambda *_: setattr(self, 'stop', True))
        self.stop_file.unlink(missing_ok=True)   # a stop requested before this start is stale
        self.emit(f'START pid {os.getpid()}, mode {self.mode()}')
        if IS_WIN and self.recorded():   # a killed predecessor cannot have resumed what it froze (or its resume failed)
            self.guard('startup-resume', self.resume_stale)
        last_advance = 0
        try:
            while not self.stop:
                if self.guard('screen', self.tick_screen):
                    self.guard('tick-file', self.write_tick)
                if time.time() - last_advance >= ADVANCE_TICK:
                    last_advance = time.time()
                    self.advance_async()
                self.sleep(SCREEN_TICK)
        finally:
            self.stop = True
            self.guard('drain', self.drain_advance)   # a claim child must never outlive our claim lock
            self.guard('exit', self.release_all)
            self.stop_file.unlink(missing_ok=True)
            self.emit('STOP')
            handle.close()
        return 0

    def dry_run_report(self):
        paused, reason = self.desired_pause()
        self.emit(f'mode {self.mode()}: would be {"PAUSED" if paused else "running"} ({reason}); PAUSE file '
                  f'{"present" if self.pause_file.exists() else "absent"}')
        for row in heavy_processes(process_table(), self.root):
            self.emit(f'heavy step {row["pid"]}:{row["comm"]} {" ".join(row["cmdline"][:3])[:100]} '
                      f'(stopped={row["stopped"]}; {"would freeze" if paused else "left running"})')
        self.guard('advance', self.advance)

    def instance(self):
        """(running, pid text) of the single instance, probing the lock without disturbing it."""
        handle = self.lock_file.open() if self.lock_file.exists() else None   # read-only: status creates nothing
        running, pid = False, ''
        if handle:
            if lock_try(handle):
                lock_release(handle)
            else:
                running = True
            handle.seek(0)
            pid = handle.read().strip()
            handle.close()
        return running, pid

    def status(self):
        running, pid = self.instance()
        print(f'supervisor: {"running (pid " + pid + ")" if running else "not running"}')
        away = self.user_away()
        print(f'mode: {self.mode()}; user: ' + {True: 'away (run)', False: 'active (pause)', None: 'unknown'}[away])
        print(f'PAUSE: {json.dumps(read_json(self.pause_file)) if self.pause_file.exists() else "absent"}'
              + (' (not the supervisor\'s: left untouched)' if self.pause_file.exists() and not self.pause_owned() else ''))
        print('supervisor pause: '
              + (json.dumps(read_json(self.own_pause_file)) if self.own_pause_file.exists() else 'absent'))
        rows = process_table()
        records = self.recorded()
        alive = [r for r in records if any(same_process(row, r) for row in rows)]
        print(f'frozen: {len(alive)} alive of {len(records)} recorded {[r["pid"] for r in alive]}')
        for key, value in sorted(self.state['holds'].items()):
            print(f'HOLD {key}: ' + '; '.join(value['reasons']))
        for key, value in sorted(self.state['sets'].items()):
            print(f'claimed {key}: {value}')
        sets = (read_json(self.advance_file, {}) or {}).get('sets', [])
        print('advance.json: ' + (', '.join(e.get('pattern', '?') for e in sets) or 'no sets (nothing auto-advances)'))


def accept_drift(advance_file, images, note):
    with advance_lock(advance_file):
        return _accept_drift(advance_file, images, note)


def _accept_drift(advance_file, images, note):
    data = read_json(advance_file, {}) or {}
    entries = data.setdefault('accepted_drift', [])
    have = {e.get('sha256') for e in entries}
    for image in images:
        digest = sha256(image)
        if digest is None:
            print(f'{image}: unreadable', file=sys.stderr)
            return 1
        if digest not in have:
            entries.append({'sha256': digest, 'image': str(image), 'note': note,
                            'date': datetime.now().isoformat(timespec='seconds')})
            have.add(digest)
            print(f'accepted {image}')
    atomic_write(advance_file, data)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=ROOT)
    sub = parser.add_subparsers(dest='command', required=True)
    runner = sub.add_parser('run')
    runner.add_argument('--dry-run', action='store_true', help='one pass: print what the screen gate and gates 1/2 would do')
    sub.add_parser('status')
    sub.add_parser('stop', help='ask the running instance to exit through its normal path (PAUSE removed, frozen steps resumed)')
    mode = sub.add_parser('mode')
    mode.add_argument('value', nargs='?', choices=MODES)
    accept = sub.add_parser('accept-drift', help='record reviewed colour drift of these exact images in advance.json')
    accept.add_argument('--note', required=True)
    accept.add_argument('images', nargs='+', type=Path)
    args = parser.parse_args(argv)
    if args.command == 'accept-drift':
        return accept_drift(args.root / '.codex_jobs' / '_batch' / 'advance.json', args.images, args.note)
    sup = Supervisor(args.root, dry_run=getattr(args, 'dry_run', False))
    if args.command == 'status':
        sup.status()
    elif args.command == 'stop':
        running, pid = sup.instance()
        if not running:
            print('supervisor: not running')
            return 0
        sup.stop_file.write_text(f'{datetime.now().isoformat(timespec="seconds")}\n', encoding='utf-8')
        print(f'stop requested (pid {pid}); it exits within a second')
    elif args.command == 'mode':
        if args.value:
            try:
                gate_transition(sup.root, lambda: atomic_write(sup.mode_file, {'mode': args.value,
                                                                               'since': datetime.now().isoformat(timespec='seconds')}),
                                f'supervisor mode {args.value}')
            except paid_gate.GateBusy as error:
                print(f'mode NOT changed: {error}', file=sys.stderr)
                print(f'mode: {sup.mode()}')
                return 1
        print(f'mode: {sup.mode()}')
    elif args.dry_run:
        sup.dry_run_report()
    else:
        return sup.run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
