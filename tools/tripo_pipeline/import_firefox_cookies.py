#!/usr/bin/env python3
"""Copy only Tripo cookies from the user's ordinary Firefox profile to stdout.

The caller must capture stdout privately. No Google cookies are included.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

PROFILE = Path(os.environ['TRIPO_FIREFOX_PROFILE']).expanduser() if os.environ.get('TRIPO_FIREFOX_PROFILE') else Path.home() / 'snap/firefox/common/.mozilla/firefox/kdkuxy50.default'


def main():
    if not PROFILE.is_dir():
        raise SystemExit(f'Ordinary Firefox profile not found: {PROFILE}')
    with tempfile.TemporaryDirectory(prefix='tripo-cookie-copy-', dir=Path.home() / '.local/share') as tmp:
        os.chmod(tmp, 0o700)
        for name in ('cookies.sqlite', 'cookies.sqlite-wal', 'cookies.sqlite-shm'):
            source = PROFILE / name
            if source.exists():
                shutil.copy2(source, Path(tmp) / name)
        with sqlite3.connect(Path(tmp) / 'cookies.sqlite') as db:
            rows = db.execute('''select name,value,host,path,expiry,isSecure,isHttpOnly,sameSite
                                 from moz_cookies where host = '.tripo3d.ai'
                                 or host = 'tripo3d.ai' or host like '%.tripo3d.ai' ''').fetchall()
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
    print(json.dumps(result))


if __name__ == '__main__':
    main()
