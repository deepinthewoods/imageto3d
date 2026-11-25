#!/usr/bin/env python3
"""Preview app v2 server (phase 1): loopback HTTP, static web/ app, /files/ + /mfiles/ like serve.py, and /api/v2/*.

    python tools/preview/v2/server.py [--port 8766] [--root <project root>] [--open]

serve.py is imported as a library (its scans, caches and file rules); the old site keeps running unchanged on :8765.
Safety rules (DESIGN.md section 6):
* Every route (index, static, files, API) only answers a `Host` that is loopback (127.0.0.1 / localhost / [::1] on our port) or one of the
  tailnet names found at start (`tailscale status --json`) / listed in `.codex_jobs/_app/settings.json` `allowed_hosts`; anything else
  gets 421. Every POST needs the per-run token (`X-Token`, injected into index.html as <meta name="v2-token">) and an `Origin`, when
  sent, that is http(s) on one of those hosts (403 otherwise).
* Path inputs of /, static, /files/ and /mfiles/ are checked as TEXT first (no `..`, no drive, no UNC, no anchored path, no stream
  names) and only then touch the filesystem; forbidden = 403, a permitted path that does not exist = 404.
* One v2 server per root may write (`.codex_jobs/_app/server.lock`); a second one serves reads only and answers POSTs with 503.
"""
from __future__ import annotations

import argparse
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
from urllib.parse import parse_qs, unquote, urlsplit
import webbrowser

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import serve  # noqa: E402  (tools/preview/serve.py, used as a library)
import actions  # noqa: E402
import builds  # noqa: E402
import catalog  # noqa: E402
import decisions  # noqa: E402
import guard  # noqa: E402
import orchestrator  # noqa: E402
import taskqueue  # noqa: E402

WEB = HERE.parent / 'web'
DEFAULT_ROOT = serve.DEFAULT_ROOT
STATIC_TYPES = {'.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.json': 'application/json',
                '.png': 'image/png', '.svg': 'image/svg+xml', '.ico': 'image/x-icon', '.map': 'application/json', '.txt': 'text/plain',
                '.woff': 'font/woff', '.woff2': 'font/woff2', '.wasm': 'application/wasm', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}
FILE_SUFFIXES = ('.png', '.jpg', '.jpeg', '.webp', '.glb')
BODY_LIMIT = 4096
THUMB_LIMIT = 8 * 1024 * 1024
PLACEHOLDER = ('<!doctype html><html><head><meta charset="utf-8"><title>Preview v2</title></head><body>'
               '<p>Preview v2 backend is running. The web app (tools/preview/web/index.html) has not been written yet.</p></body></html>')
META = re.compile(r'<meta\s+name="v2-token"[^>]*>', re.I)
BAD_PATH_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')       # drive letters / alternate streams / wildcards: none belongs in a served path
POST_PATHS = ('/api/v2/action', '/api/v2/lod', '/api/v2/thumbnail', '/api/v2/amount', '/api/v2/queue')


def queue_health(root: Path) -> dict | None:
    """`state.queue_health` (DESIGN_PHASE3.md E5): None while the layer-build queue database is fine, else {ok: false, errors, corrupt, since}
    (the catalog keeps working; the orchestrator keeps the damaged file and rebuilds the queue). A repair is reported once as {ok: true, repaired}."""
    health = taskqueue.for_root(root).health_view()
    if health.get('ok'):
        return {'ok': True, 'repaired': health['repaired']} if health.get('repaired') else None
    return {'ok': False, 'errors': [str(health.get('error'))], 'corrupt': bool(health.get('corrupt')), 'since': health.get('since')}


def inject_token(html: str, token: str) -> str:
    """index.html with `<meta name="v2-token" content="...">` (an existing one is replaced)."""
    tag = f'<meta name="v2-token" content="{token}">'
    if META.search(html):
        return META.sub(tag, html, count=1)
    lower = html.lower()
    for marker, after in (('</head>', False), ('<head>', True)):
        at = lower.find(marker)
        if at >= 0:
            at += len(marker) if after else 0
            return html[:at] + tag + html[at:]
    return tag + html


# ---------------------------------------------------------------------------------------------------------------- hosts

def discover_hosts(root: Path, run=subprocess.run) -> list[str]:
    """Extra Host names to accept: this machine's tailnet name(s) (`tailscale status --json`) and `allowed_hosts` of
    `.codex_jobs/_app/settings.json`. A missing tailscale or file just means none."""
    names: list[str] = []
    try:
        out = run(['tailscale', 'status', '--json'], capture_output=True, text=True, timeout=5, check=False)
        me = (json.loads(out.stdout or '{}').get('Self') or {}) if out.returncode == 0 else {}
        names += [str(me.get('DNSName') or '').rstrip('.'), str(me.get('HostName') or '')]
        names += [str(ip) for ip in me.get('TailscaleIPs') or []]
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        pass
    try:
        configured = json.loads((Path(root) / guard.APP / 'settings.json').read_text(encoding='utf-8-sig')).get('allowed_hosts') or []
        names += [str(n) for n in configured if isinstance(n, str)]
    except (OSError, ValueError, AttributeError):
        pass
    return sorted({n.strip().lower() for n in names if n and n.strip()})


def host_set(port: int, extra: list[str], bind: str = '127.0.0.1') -> frozenset[str]:
    """Every accepted `Host` header value: loopback on our port, the bind address, and each extra name (with and without the port)."""
    hosts = {f'127.0.0.1:{port}', f'localhost:{port}', f'[::1]:{port}', f'{bind}:{port}'}
    for name in extra:
        hosts |= {name.lower(), f'{name.lower()}:{port}'}
    return frozenset(hosts)


# ------------------------------------------------------------------------------------------------------- path containment

def clean_relative(raw: str) -> str | None:
    """The decoded relative path of a URL tail, or None when it is not a plain relative path (traversal, anchored, drive, UNC,
    stream names, control characters, Windows trailing dots/spaces). Pure text: nothing here touches the filesystem."""
    rel = unquote(raw).replace('\\', '/')
    if not rel or BAD_PATH_CHARS.search(rel) or rel.startswith('/'):
        return None
    for part in rel.split('/'):
        if part in ('', '.', '..') or part != part.rstrip(' .'):
            return None
    return rel


def _contained(file: Path, base: Path) -> bool:
    try:
        file.resolve(strict=False).relative_to(base.resolve(strict=True))
    except (ValueError, OSError):
        return False
    return True


def lookup(base: Path, raw: str, suffixes, allow=None) -> tuple[Path | None, int]:
    """(file, 200) | (None, 403 forbidden) | (None, 404 missing) for URL tail `raw` below `base`."""
    rel = clean_relative(raw)
    if rel is None:
        return None, 403
    file = base / rel
    if file.suffix.lower() not in suffixes and not (allow and allow(file)):
        return None, 403
    if not _contained(file, base):
        return None, 403
    if not file.is_file():
        return None, 404
    return file, 200


def _symmap(file: Path) -> bool:
    return file.name in ('symmap.bin', 'symmap.json') and file.parent.name.startswith('lod_') and file.parent.parent.name == 'fitlab'


def files_lookup(root: Path, path: str) -> tuple[Path | None, int]:
    """serve.py's `/files/` rule: image/GLB suffixes (and the fit lab symmetry maps) inside the project root. The bare layers are part of the
    project root too (`<root>/tools/preview/cache/layers/`, phase 3 re-check R13): nothing is ever served from another root."""
    return lookup(root, path.removeprefix('/files/'), FILE_SUFFIXES, _symmap)


def mfiles_lookup(root: Path, path: str) -> tuple[Path | None, int]:
    return lookup(serve.monsters.monsters_root(root), path.removeprefix(serve.monsters.FILE_PREFIX), serve.monsters.SERVED)


def static_lookup(web: Path, path: str) -> tuple[Path | None, int]:
    return lookup(web, path.lstrip('/'), tuple(STATIC_TYPES))


# ------------------------------------------------------------------------------------------------------------- handler

def make_handler(root: Path, token: str, web: Path = WEB, ctx: dict | None = None):
    ctx = ctx if ctx is not None else {'hosts': None, 'read_only': False, 'message': None, 'notes': []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):      # quiet; the old site's actions.jsonl is the audit trail
            pass

        # ------------------------------------------------------------------------------------------------ responses
        def send_json(self, status: int, data: object) -> None:
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            if 'gzip' in (self.headers.get('Accept-Encoding') or '').lower() and len(raw) > 4096:
                body = gzip.compress(raw, 6)
                self.send_header('Content-Encoding', 'gzip')
            else:
                body = raw
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def send_bytes(self, raw: bytes, content_type: str, cache: str | None = None) -> None:
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            if cache:
                self.send_header('Cache-Control', cache)
            self.end_headers()
            self.wfile.write(raw)

        def fail(self, status: int, message: str, **extra) -> None:
            self.send_json(status, {'ok': False, 'error': message} | extra)

        def send_file(self, file: Path, status: int, cache: str | None = None, content_type: str | None = None) -> None:
            if file is None:
                self.send_error(status)
                return
            try:
                data = file.read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_bytes(data, content_type or mimetypes.guess_type(file.name)[0] or 'application/octet-stream', cache)

        # ------------------------------------------------------------------------------------------------- host / origin
        def host_ok(self) -> bool:
            hosts = ctx.get('hosts')
            return hosts is None or (self.headers.get('Host') or '').strip().lower() in hosts

        def origin_ok(self) -> bool:
            origin = self.headers.get('Origin')
            if origin is None:
                return True
            split = urlsplit(origin)
            hosts = ctx.get('hosts')
            return split.scheme in ('http', 'https') and bool(split.netloc) and (hosts is None or split.netloc.lower() in hosts)

        # ------------------------------------------------------------------------------------------------------ GET
        def do_GET(self) -> None:
            if not self.host_ok():
                self.send_error(421, 'Unknown host')
                return
            split = urlsplit(self.path)
            path, query = split.path, parse_qs(split.query)
            if path in ('/', '/index.html'):
                index = web / 'index.html'
                if not os.path.lexists(index):
                    html = PLACEHOLDER
                else:
                    file, _status = static_lookup(web, 'index.html')      # the same containment check as every other static file
                    if file is None:
                        self.send_error(404)
                        return
                    html = file.read_text(encoding='utf-8')
                self.send_bytes(inject_token(html, token).encode(), 'text/html; charset=utf-8', 'no-store')
            elif path == '/api/v2/state':
                data = dict(catalog.state(root))
                data['server'] = {'read_only': bool(ctx.get('read_only')), 'message': ctx.get('message'), 'notes': list(ctx.get('notes') or []),
                                  'orchestrator': bool(ctx.get('orchestrator'))}
                data['queue_health'] = queue_health(root)
                self.send_json(200, data)
            elif path == '/api/v2/layers':
                try:
                    self.send_json(200, catalog.layers_for_race(root, (query.get('race') or [''])[0]))
                except catalog.NotFound as exc:
                    self.fail(404, str(exc))
            elif path == '/api/v2/queue':
                self.send_json(200, orchestrator.queue_state(root, bool(ctx.get('orchestrator'))))
            elif path == '/api/v2/item':
                try:
                    self.send_json(200, catalog.item(root, (query.get('id') or [''])[0]))
                except catalog.NotFound as exc:
                    self.fail(404, str(exc))
                except (ValueError, OSError) as exc:
                    self.fail(400, str(exc))
            elif path.startswith('/api/'):
                self.send_error(404)
            elif path.startswith('/files/'):
                file, status = files_lookup(root, path)
                self.send_file(file, status, 'private, max-age=3600')
            elif path.startswith(serve.monsters.FILE_PREFIX):
                file, status = mfiles_lookup(root, path)
                self.send_file(file, status)
            else:
                file, _status = static_lookup(web, path)                    # static: everything unusable is a plain 404
                self.send_file(file, 404, 'no-cache', None if file is None else
                               STATIC_TYPES[file.suffix.lower()] + ('; charset=utf-8' if file.suffix.lower() in ('.js', '.mjs', '.css', '.html') else ''))

        # ----------------------------------------------------------------------------------------------------- POST
        def authorised(self) -> bool:
            if not secrets.compare_digest((self.headers.get('X-Token') or '').encode(), token.encode()):
                return False
            return self.origin_ok()

        def read_body(self, limit: int) -> bytes:
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise ValueError('Invalid request size') from None
            if size < 1 or size > limit:
                raise ValueError('Invalid request size')
            return self.rfile.read(size)

        def do_POST(self) -> None:
            if not self.host_ok():
                self.send_error(421, 'Unknown host')
                return
            split = urlsplit(self.path)
            if not self.authorised():
                self.fail(403, 'Missing or wrong token, or foreign origin')
                return
            if split.path not in POST_PATHS:
                self.send_error(404)
                return
            if ctx.get('read_only'):
                self.fail(503, ctx.get('message') or 'This server is read-only', code='read_only')
                return
            body = None
            try:
                if split.path == '/api/v2/thumbnail':
                    url = parse_qs(split.query).get('url', [''])[0]
                    result = serve.write_thumb(root, url, self.read_body(THUMB_LIMIT))
                    catalog.invalidate()
                    self.send_json(200, result)
                    return
                body = json.loads(self.read_body(BODY_LIMIT))
                handler = {'/api/v2/action': actions.perform, '/api/v2/lod': actions.set_lod, '/api/v2/amount': actions.set_amount,
                           '/api/v2/queue': actions.queue_op}[split.path]
                self.send_json(200, handler(root, body))
            except guard.Conflict as exc:
                catalog.invalidate()
                extra = {k: v for k, v in (getattr(exc, 'extra', None) or {}).items() if k in ('changes',)}      # e.g. `changes` of 409 changed / stale
                extra |= {'rev': exc.current} if isinstance(exc, decisions.RevConflict) else {}
                self.send_json(409, {'ok': False, 'error': str(exc), 'code': exc.code, 'item': self.current_item(body)} | extra)
            except catalog.NotFound as exc:
                self.fail(404, str(exc))
            except guard.ReadOnly as exc:
                self.fail(503, str(exc), code='read_only')
            except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
                self.fail(400, str(exc))

        def current_item(self, body: object) -> object:
            try:
                return catalog.item(root, body['id'])
            except Exception:
                return None

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    lock: guard.ServerLock | None = None
    orchestrator: orchestrator.Orchestrator | None = None

    def server_close(self) -> None:
        if self.orchestrator:
            self.orchestrator.stop()           # the thread and its lock; running builds are detached and keep going
            self.orchestrator = None
        super().server_close()
        if self.lock:
            self.lock.release()
            self.lock = None


def create_server(root: Path = DEFAULT_ROOT, host: str = '127.0.0.1', port: int = 0, token: str | None = None, web: Path = WEB,
                  extra_hosts: list[str] | None = None, read_only: bool | None = None, run_orchestrator: bool = False) -> Server:
    """`extra_hosts` = additional accepted Host names (None = discover the tailnet name and settings.json; [] = loopback only).
    `read_only` None = take the single-writer lock and serve read-only when another server holds it; True = never write.
    `run_orchestrator` starts the layer-build orchestrator thread, only in the server that holds the writer lock (and its own
    orchestrator.lock: when another process holds that, this server is read-only with a banner)."""
    root = Path(root)
    token = token or secrets.token_urlsafe(18)
    ctx: dict = {'hosts': None, 'read_only': False, 'message': None, 'notes': []}
    server = Server((host, port), make_handler(root, token, Path(web), ctx))
    server.token = token
    server.ctx = ctx
    ctx['hosts'] = host_set(server.server_port, discover_hosts(root) if extra_hosts is None else list(extra_hosts), host)
    lock = guard.ServerLock(root)
    if read_only or not lock.acquire():
        ctx['read_only'] = True
        ctx['message'] = ('Read-only: another preview v2 server holds the writer lock of this project; use that one to change anything'
                          if read_only is None else 'Read-only server')
    else:
        server.lock = lock
        ctx['notes'] = actions.recover_at_start(root)
        if run_orchestrator:
            runner = orchestrator.Orchestrator(root)
            if runner.start():
                server.orchestrator = runner
                ctx['orchestrator'] = True
            else:
                ctx['read_only'] = True
                ctx['message'] = 'Read-only: another process holds the orchestrator lock (.codex_jobs/_app/orchestrator.lock) and runs the builds'
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--read-only', action='store_true')
    parser.add_argument('--no-orchestrator', action='store_true', help='do not run the layer-build orchestrator in this server')
    args = parser.parse_args()
    server = create_server(args.root.resolve(), port=args.port, read_only=True if args.read_only else None, run_orchestrator=not args.no_orchestrator)
    url = f'http://127.0.0.1:{server.server_port}/'
    print(url, '(read-only)' if server.ctx['read_only'] else '', '(orchestrator)' if server.ctx.get('orchestrator') else '', flush=True)
    for note in server.ctx['notes']:
        print('recovery:', note, flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
