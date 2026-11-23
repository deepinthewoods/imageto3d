"""Small Linux/Windows portability helpers for the Hyper3D runners (locks, directory fsync)."""
import os
import sys

WINDOWS = sys.platform == 'win32'


def lock_nonblocking(stream):
    """Exclusive, non-blocking advisory lock on an open file; raises BlockingIOError (an OSError) when held.
    The lock is released when the file is closed. Linux: flock; Windows: a byte-range lock on byte 0."""
    if WINDOWS:
        import msvcrt
        try:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError(*exc.args) from exc
    else:
        import fcntl
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def fsync_dir(path):
    """Flush a directory entry to disk (Linux only; Windows cannot open directories with os.open)."""
    if WINDOWS:
        return
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

