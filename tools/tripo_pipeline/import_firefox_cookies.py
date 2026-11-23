#!/usr/bin/env python3
"""Copy only Tripo cookies from the user's ordinary Firefox profile to stdout.

The caller must capture stdout privately. No Google cookies are included.
"""
import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time

# Linux: the snap Firefox profile; Windows (2026-11-09): the ordinary Store-Firefox profile the user also uses for Hyper3D / Google.
# TRIPO_FIREFOX_PROFILE overrides either.
if os.environ.get('TRIPO_FIREFOX_PROFILE'):
    PROFILE = Path(os.environ['TRIPO_FIREFOX_PROFILE']).expanduser()
elif os.name == 'nt':
    PROFILE = Path.home() / '.local/share/hyper3d-workflow-profile'
else:
    PROFILE = Path.home() / 'snap/firefox/common/.mozilla/firefox/kdkuxy50.default'


def main():
    if not PROFILE.is_dir():
        raise SystemExit(f'Ordinary Firefox profile not found: {PROFILE}')
    scratch = Path.home() / '.local/share'
    scratch.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix='tripo-cookie-copy-', dir=scratch)   # holds a copy of the cookie db: always removed again
    try:
        os.chmod(tmp, 0o700)
        for name in ('cookies.sqlite', 'cookies.sqlite-wal', 'cookies.sqlite-shm'):
            source = PROFILE / name
            if source.exists():
                shutil.copy2(source, Path(tmp) / name)
        if not (Path(tmp) / 'cookies.sqlite').exists():
            raise SystemExit(f'No cookies.sqlite in {PROFILE}')
        with contextlib.closing(sqlite3.connect(Path(tmp) / 'cookies.sqlite')) as db:   # `with connect()` alone does not close (Windows keeps the file locked)
            rows = db.execute('''select name,value,host,path,expiry,isSecure,isHttpOnly,sameSite
                                 from moz_cookies where host = '.tripo3d.ai'
                                 or host = 'tripo3d.ai' or host like '%.tripo3d.ai' ''').fetchall()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    now = time.time()
    result = []
    for name, value, host, path, expiry, secure, http_only, same_site in rows:
        expires = expiry / 1000 if expiry > 10**11 else expiry
        if expires and expires <= now:
            continue
        cookie = {'name': name, 'value': value, 'domain': host, 'path': path,
                  'secure': bool(secure), 'httpOnly': bool(http_only)}
        if expires:
            cookie['expires'] = expires
        if same_site == 0 and secure:
            cookie['sameSite'] = 'None'
        elif same_site == 2:
            cookie['sameSite'] = 'Strict'
        else:
            cookie['sameSite'] = 'Lax'
        result.append(cookie)
    print(f'{len(result)} unexpired Tripo cookie(s) from {PROFILE}', file=sys.stderr)   # count only, never the values
    print(json.dumps(result))


if __name__ == '__main__':
    main()
