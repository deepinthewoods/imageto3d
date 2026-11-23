#!/usr/bin/env python3
"""Windows stand-in for the user gate of tools/pipeline_supervisor.py (which needs GNOME/fcntl): pause the queues while
the user is at the PC (user rule 2026-10-29: everything idles with the user at the PC).

    python tools/win_idle_gate.py            # the loop (single instance)
    python tools/win_idle_gate.py status     # idle time, mode, PAUSE file

Every 30 s: the user is AWAY after AWAY_AFTER (5 min) without keyboard/mouse input (GetLastInputInfo; a locked screen
gets no input, so it is away after 5 min too; a playing video is not input). At the PC -> write .codex_jobs/_batch/PAUSE
{"since", "reason": "user active", "scope": ["conversions", "views"]}, so tools/view_batch.py and convert_queue.py start
nothing new (running jobs finish). Away -> remove that PAUSE. Only pauses with a reason this gate writes are removed;
any other PAUSE (written by hand) is left alone.

The supervisor's manual override in .codex_jobs/_batch/supervisor_mode.json is honoured: "auto" (default) follows the
user, "run" never pauses for the user, "pause" pauses everything, "pause-conversions" pauses conversions only.
Changes are logged to .codex_jobs/_batch/events.log as `WINGATE ...` lines.
"""
import ctypes
import json
import msvcrt
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BATCH = ROOT / '.codex_jobs' / '_batch'
PAUSE, EVENTS = BATCH / 'PAUSE', BATCH / 'events.log'
MODE, LOCK = BATCH / 'supervisor_mode.json', BATCH / 'win_idle_gate.lock'
AWAY_AFTER, TICK = 300, 30
USER, MANUAL = 'user active', 'manual pause (supervisor_mode.json)'
OWNED = (USER, MANUAL)   # PAUSE reasons this gate may remove (the Linux supervisor writes the same "user active")


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]


def idle_seconds():
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    ctypes.windll.kernel32.GetTickCount.restype = wintypes.DWORD
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def mode():
    try:
        return json.loads(MODE.read_text()).get('mode', 'auto')
    except (OSError, ValueError, AttributeError):
        return 'auto'


def current_pause():
    """(reason, scope) of the PAUSE file, or None."""
    if not PAUSE.exists():
        return None
    try:
        data = json.loads(PAUSE.read_text())
        return data.get('reason'), data.get('scope')
    except (OSError, ValueError, AttributeError):
        return 'PAUSE file', None


def emit(line):
    line = f'{datetime.now():%H:%M:%S} WINGATE {line}'
    print(line, flush=True)
    with EVENTS.open('a') as handle:
        handle.write(line + '\n')


def wanted(idle):
    """(reason, scope) the PAUSE file should have now, or None for no pause."""
    current = mode()
    if current == 'pause':
        return MANUAL, ['conversions', 'views']
    if current == 'pause-conversions':
        return MANUAL, ['conversions']
    if current == 'run' or idle >= AWAY_AFTER:
        return None
    return USER, ['conversions', 'views']


def tick():
    idle = idle_seconds()
    want, have = wanted(idle), current_pause()
    if have and have[0] not in OWNED:
        return   # a hand-written pause: not ours to change
    if want and have != want:
        BATCH.mkdir(parents=True, exist_ok=True)
        tmp = PAUSE.with_suffix('.tmp')
        tmp.write_text(json.dumps({'since': datetime.now().isoformat(timespec='seconds'), 'reason': want[0],
                                   'scope': want[1]}, indent=2) + '\n')
        tmp.replace(PAUSE)
        emit(f'PAUSE ({want[0]}, scope {",".join(want[1])}): queues start nothing new')
    elif not want and have:
        PAUSE.unlink(missing_ok=True)
        emit(f'RESUME (idle {idle / 60:.0f} min, mode {mode()}): PAUSE removed')


def main():
    if sys.argv[1:] == ['status']:
        print(json.dumps({'idle_seconds': round(idle_seconds()), 'mode': mode(), 'pause': current_pause()}))
        return
    BATCH.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK, 'a+')
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        sys.exit('win_idle_gate already running')
    emit(f'START (away after {AWAY_AFTER // 60} min without input, mode {mode()})')
    while True:
        try:
            tick()
        except OSError as error:   # a share hiccup must not stop the gate
            print(f'tick failed: {error}', flush=True)
        time.sleep(TICK)


if __name__ == '__main__':
    main()
