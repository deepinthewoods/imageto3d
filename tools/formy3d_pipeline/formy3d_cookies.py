"""Copy only formy3d.com cookies from the user's ordinary Firefox profile (read-only, private temp copy).

Mirrors tools/tripo_pipeline/import_firefox_cookies.py. Cookie values are only ever returned to the caller
(to be added to a Playwright context); this module never prints, logs or writes them anywhere.
"""
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time

PROFILE = (Path(os.environ['FORMY3D_FIREFOX_PROFILE']).expanduser() if os.environ.get('FORMY3D_FIREFOX_PROFILE')
           else Path.home() / 'snap/firefox/common/.mozilla/firefox/kdkuxy50.default')
HOST = 'formy3d.com'


def firefox_cookies(profile=PROFILE, host=HOST):
    """Unexpired cookies for `host` and its subdomains, as Playwright cookie dicts."""
    profile = Path(profile)
    if not profile.is_dir():
        raise FileNotFoundError(f'Ordinary Firefox profile not found: {profile}')
    base = Path.home() / '.local/share'
    with tempfile.TemporaryDirectory(prefix='formy3d-cookie-copy-', dir=base) as tmp:
        os.chmod(tmp, 0o700)
        for name in ('cookies.sqlite', 'cookies.sqlite-wal', 'cookies.sqlite-shm'):
            source = profile / name
            if source.exists():
                shutil.copy2(source, Path(tmp) / name)
        db = sqlite3.connect(Path(tmp) / 'cookies.sqlite')
        try:
            rows = db.execute('''select name,value,host,path,expiry,isSecure,isHttpOnly,sameSite
                                 from moz_cookies where host = ? or host = ? or host like ?''',
                              (host, '.' + host, '%.' + host)).fetchall()
        finally:
            db.close()
    now = time.time()
    result = []
    for name, value, domain, path, expiry, secure, http_only, same_site in rows:
        expires = expiry / 1000 if expiry > 10**11 else expiry
        if expires and expires <= now:
            continue
        cookie = {'name': name, 'value': value, 'domain': domain, 'path': path,
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
    return result


def cookie_names(cookies):
    """Names only, safe to log."""
    return sorted({c['name'] for c in cookies})
