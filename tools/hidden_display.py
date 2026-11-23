#!/usr/bin/env python3
"""Private X display for the conversion browsers, so their windows never pop up on the desktop.

    python3 tools/hidden_display.py start --parent <pid>    # prints e.g. ":91", or nothing

Starts Xvfb (no window at all) when installed, else Xephyr (a nested X server whose single
host window is minimized at once), waits for its socket, prints the display name and returns.
A detached watcher kills the server when <pid> exits. Launch the browser with DISPLAY set to
the printed value (Playwright: launchPersistentContext(..., {env: {...process.env, DISPLAY}});
Selenium: Service(..., env=...)). Prints nothing, so the caller keeps the normal desktop
DISPLAY, when PIPELINE_SHOW_WINDOWS=1, without DISPLAY, or without Xvfb/Xephyr.

Minimizing the browser window itself does not work: Firefox stops painting a minimized window
(requestAnimationFrame halts), so Playwright clicks time out and WebGL canvases stay blank.
Inside the private display the browser window stays mapped and renders normally.
"""
import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
SIZE = '1920x1200x24'


def hidden_display(parent=None):
    """Start a private display for the calling Python process; returns ':N' or None.

    Windows has no X server to hide the browser in: always None (the browser window is shown)."""
    if sys.platform == 'win32' or os.environ.get('PIPELINE_SHOW_WINDOWS') == '1':
        return None
    run = subprocess.run([sys.executable, str(HERE), 'start', '--parent', str(parent or os.getpid())],
                         capture_output=True, text=True)
    return run.stdout.strip() or None


def free_display():
    for number in range(90, 200):
        if not Path(f'/tmp/.X{number}-lock').exists() and not Path(f'/tmp/.X11-unix/X{number}').exists():
            return number
    raise SystemExit('no free X display number in 90..199')


def start(parent):
    if sys.platform == 'win32' or os.environ.get('PIPELINE_SHOW_WINDOWS') == '1' or not os.environ.get('DISPLAY'):
        return
    import fcntl
    # parallel workers: serialise picking + starting so two never claim the same display number
    with open('/tmp/hidden_display.lock', 'w') as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        return _start(parent)


def _start(parent):
    number = free_display()
    if shutil.which('Xvfb'):
        command = ['Xvfb', f':{number}', '-screen', '0', SIZE, '-nolisten', 'tcp']
    elif shutil.which('Xephyr'):
        command = ['Xephyr', f':{number}', '-screen', SIZE, '-nolisten', 'tcp', '-br', '-ac']
    else:
        return
    server = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True)
    socket = Path(f'/tmp/.X11-unix/X{number}')
    for _ in range(100):
        if socket.exists() or server.poll() is not None:
            break
        time.sleep(0.05)
    if server.poll() is not None or not socket.exists():
        server.kill()
        return
    if command[0] == 'Xephyr' and shutil.which('xdotool'):
        for _ in range(40):
            windows = subprocess.run(['xdotool', 'search', '--pid', str(server.pid)],
                                     capture_output=True, text=True).stdout.split()
            if windows:
                for window in windows:
                    subprocess.run(['xdotool', 'windowminimize', window], capture_output=True)
                break
            time.sleep(0.05)
    subprocess.Popen([sys.executable, str(HERE), 'watch', '--parent', str(parent), '--server', str(server.pid)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    print(f':{number}')


def watch(parent, server):
    while Path(f'/proc/{parent}').exists() and Path(f'/proc/{server}').exists():
        time.sleep(1)
    try:
        os.kill(server, signal.SIGTERM)
    except ProcessLookupError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('start', 'watch'))
    parser.add_argument('--parent', type=int, required=True)
    parser.add_argument('--server', type=int)
    args = parser.parse_args()
    if args.command == 'start':
        start(args.parent)
    else:
        watch(args.parent, args.server)


if __name__ == '__main__':
    main()
