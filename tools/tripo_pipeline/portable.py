"""Linux/Windows helpers shared by the conversion chain (stdlib only, so Blender's python can import it too).

Linux behaviour is exactly what the callers did before (fcntl.flock, /proc/<pid>, pgrep, /usr/local/bin/blender,
libmeshoptimizer.so, <venv>/bin/python); the Windows branches exist since 2026-11-09:

- file_lock(path, exclusive, sidecar): fcntl.flock on Linux. Windows has no advisory shared lock in msvcrt, so shared requests
  take the exclusive lock there (every caller holds it only for a short read/modify/write, nothing nests), and the data file is
  never locked itself (msvcrt locks are mandatory and would block readers): a lock file under .codex_jobs/_batch/record_locks/ carries it.
- pid_alive(pid): /proc on Linux, OpenProcess + GetExitCodeProcess on Windows.
- process_running(pattern): pgrep -f on Linux, a psutil command-line scan on Windows (backslashes read as slashes).
- default_blender(): $BLENDER, else /usr/local/bin/blender on Linux; on Windows the highest `Blender Foundation\\Blender X.Y`
  install, else blender on PATH.
- meshopt_library(): lib/libmeshoptimizer.so or lib/libmeshoptimizer.dll (tools/lowpoly/build_meshoptimizer.sh builds either).
- venv_python_candidates(): interpreters with numpy + scipy for the plain-python steps.
"""
import contextlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

WINDOWS = os.name == 'nt'
ROOT = Path(__file__).resolve().parents[2]


# ----------------------------------------------------------------------------------------------- locks

@contextlib.contextmanager
def file_lock(path, exclusive=True, sidecar=False):
    """Hold a lock on `path` for the with-block (blocking). `sidecar`: `path` is a data file other processes read and write by
    themselves (the job record); on Windows the lock then lives in .codex_jobs/_batch/record_locks/. Without it `path` is a pure lock file and is
    created when missing. Shared locks (exclusive=False) are exclusive on Windows."""
    path = Path(path)
    if not WINDOWS:
        import fcntl
        with open(path, 'r' if sidecar else 'a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        return
    import msvcrt
    # the lock file lives outside the data folder (a job folder gets moved between pipeline stages and must stay clean); the name
    # is the folder + file name, which survives that move
    lock_path = (ROOT / '.codex_jobs' / '_batch' / 'record_locks' / f'{path.resolve().parent.name}__{path.name}.lock') if sidecar else path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if sidecar and not path.exists():
        raise FileNotFoundError(str(path))        # same error flock's open() raised on Linux
    with open(lock_path, 'ab') as handle:
        fd = handle.fileno()
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass


# ----------------------------------------------------------------------------------------------- processes

def pid_alive(pid):
    """True when a process with this pid exists (Windows: and has not exited)."""
    if not pid or pid <= 0:
        return False
    if not WINDOWS:
        return Path(f'/proc/{pid}').exists()
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, int(pid))      # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5                      # ERROR_ACCESS_DENIED: exists, not ours to open
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259                                 # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def process_running(pattern):
    """True when another process's command line contains `pattern` (a plain substring; on Windows `\\` counts as `/`).
    Linux: pgrep -f (a regex, as before)."""
    if not WINDOWS:
        return subprocess.run(['pgrep', '-f', pattern], capture_output=True).returncode == 0
    import psutil
    me = os.getpid()
    for proc in psutil.process_iter(['pid', 'cmdline']):
        if proc.info['pid'] == me:
            continue
        line = ' '.join(proc.info['cmdline'] or ()).replace('\\', '/')
        if pattern in line:
            return True
    return False


# ----------------------------------------------------------------------------------------------- tools

def _version_key(path):
    digits = ''.join(c if c.isdigit() or c == '.' else ' ' for c in path.name).split()
    try:
        return tuple(int(x) for x in digits[0].split('.')) if digits else (0,)
    except ValueError:
        return (0,)


def default_blender():
    """$BLENDER; else Linux /usr/local/bin/blender; Windows: the highest installed Blender Foundation\\Blender X.Y\\blender.exe (the
    Linux conversions ran 5.1, glTF exporter 5.1.x; Windows has 5.0, 4.4, 4.1), else blender on PATH."""
    if os.environ.get('BLENDER'):
        return os.environ['BLENDER']
    if not WINDOWS:
        return '/usr/local/bin/blender'
    roots = [Path(os.environ.get(v, d)) / 'Blender Foundation' for v, d in (('ProgramFiles', r'C:\Program Files'),
                                                                           ('ProgramFiles(x86)', r'C:\Program Files (x86)'))]
    found = sorted((exe for r in roots if r.is_dir() for d in r.glob('Blender *') for exe in [d / 'blender.exe'] if exe.is_file()),
                   key=lambda p: _version_key(p.parent), reverse=True)
    if found:
        return str(found[0])
    return shutil.which('blender') or 'blender'


def meshopt_library(root=ROOT):
    """Path of the meshoptimizer shared library for this OS under tools/lowpoly/lib."""
    return Path(root) / 'tools/lowpoly/lib' / ('libmeshoptimizer.dll' if WINDOWS else 'libmeshoptimizer.so')


def venv_python_candidates(root=ROOT, configured=None):
    """Interpreters to try for the numpy + scipy steps, in order: $SEGMENT_FIRST_PYTHON, `configured` (config.json
    segment_first.venv_python), the characters/.venv beside the imageto3d folder; Windows adds the Python 3.10 install and `python`
    on PATH (never sys.executable: inside Blender that is blender.exe)."""
    root = Path(root)
    out = [os.environ.get('SEGMENT_FIRST_PYTHON'), configured]
    if WINDOWS:
        out.append(str(root.parent / '.venv/Scripts/python.exe'))
        local = os.environ.get('LOCALAPPDATA')
        if local:
            out.append(str(Path(local) / 'Programs/Python/Python310/python.exe'))
        out.append(shutil.which('python'))
    else:
        out.append(str(root.parent / '.venv/bin/python'))
    return [c for c in out if c]


def venv_python_dir(venv):
    """`<venv>/bin/python` on Linux, `<venv>/Scripts/python.exe` on Windows."""
    return str(Path(venv) / ('Scripts/python.exe' if WINDOWS else 'bin/python'))


def backend_python(configured, windows=None):
    """Interpreter named by a backend config.json: on Windows `windows` (its `venv_python_windows`) wins when set; an absolute
    interpreter path (or a name on PATH) is used as is, a venv directory resolves to its bin/python / Scripts/python.exe."""
    configured = windows if (WINDOWS and windows) else configured
    path = Path(configured)
    if path.is_dir():
        return venv_python_dir(path)
    return str(configured)


def local_path(path, root=ROOT):
    """A path recorded in a manifest / job record by a run on another machine or OS (absolute, e.g. `/media/niz/.../imageto3d/base_views/..`)
    mapped onto this checkout: the path itself while it exists, else `<root>/<part after the first imageto3d folder>` when that exists,
    else the path unchanged. None stays None."""
    if not path:
        return path
    p = Path(path)
    if p.exists():
        return p
    parts = str(path).replace('\\', '/').split('/')
    if 'imageto3d' in parts:
        mapped = Path(root).joinpath(*parts[parts.index('imageto3d') + 1:])
        if mapped.exists():
            return mapped
    return p


def utf8_environment():
    """Make child python processes (Blender's included) read and write text as UTF-8 on Windows (their default is the ANSI code page)."""
    if WINDOWS:
        os.environ.setdefault('PYTHONUTF8', '1')
        os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
