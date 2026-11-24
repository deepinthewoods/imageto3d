"""Shell / slot prototype server (2026-11-23): out/ as static files, plus result variants built on request for the viewer's option controls.

    python experiments/shell_slots_20261122/serve.py [--port 8790]

GET /api/variant?name=<result>&key=<a0-boff-c0-d0-e0>[&retry=1]
    -> {"status": "ready", "rec": {...}} | {"status": "queued", "position": n} | {"status": "building", "elapsed": s} | {"status": "failed", "error": ...}
GET /api/queue -> {"building": [name, key] | null, "queue": [[name, key], ...]}
Builds run one at a time (`geodiv.py variant <name> <key>` in a subprocess) into out/variants/<name>__<key>.*; a variant built by an older geodiv.py /
shell_slots.py is rebuilt. The key's letters are the viewer's options: a fold-safe joins, b symmetry method, c spike cleanup, d edge refinement,
e density-aware thresholds.
"""
import argparse
import json
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / 'out'
VAR = OUT / 'variants'
ROOT = HERE.parents[1]
BASE_KEY = 'a0-boff-c0-d0-e0'
KEY_RE = re.compile(r'^a[01]-b(off|sleeve_shift|sleeve_average|sleeve_copy|whole_mirror)-c[01]-d[01]-e[01]$')
lock = threading.Lock()
queue = []
state = {'building': None, 'started': None}


def code_mtime():
    return max((HERE / f).stat().st_mtime for f in ('geodiv.py', 'shell_slots.py'))


def log(*a):
    VAR.mkdir(parents=True, exist_ok=True)
    with open(VAR / 'serve.log', 'a', encoding='utf-8') as f:
        f.write(time.strftime('%H:%M:%S ') + ' '.join(map(str, a)) + '\n')


def worker():
    while True:
        with lock:
            job = queue.pop(0) if queue else None
            state['building'], state['started'] = job, (time.time() if job else None)
        if job is None:
            time.sleep(0.5)
            continue
        name, key = job
        t0 = time.time()
        try:
            r = subprocess.run([sys.executable, str(HERE / 'geodiv.py'), 'variant', name, key], cwd=ROOT, capture_output=True, text=True, timeout=1800)
            ok = r.returncode == 0 and (VAR / f'{name}__{key}.json').is_file()
            if not ok:
                (VAR / f'{name}__{key}.failed').write_text(((r.stderr or '') + (r.stdout or ''))[-4000:], encoding='utf-8')
            log('built' if ok else 'FAILED', name, key, f'{time.time() - t0:.0f} s')
        except Exception as error:  # noqa: BLE001
            VAR.mkdir(parents=True, exist_ok=True)
            (VAR / f'{name}__{key}.failed').write_text(f'{type(error).__name__}: {error}', encoding='utf-8')
            log('FAILED', name, key, error)
        finally:
            with lock:
                state['building'], state['started'] = None, None


def result_names():
    try:
        return {r['name'] for r in json.loads((OUT / 'index.json').read_text())['results']}
    except (OSError, ValueError):
        return set()


def variant(name, key, retry):
    if re.match(r'^a[01]-b[a-z_]+-c[01]-d[01]$', key):          # a page cached from before option e: same options, e off
        key += '-e0'
    if name not in result_names() or not KEY_RE.match(key):
        return 400, {'status': 'error', 'error': 'unknown result or option key'}
    stem = VAR / f'{name}__{key}'
    js, failed = Path(f'{stem}.json'), Path(f'{stem}.failed')
    if retry and failed.is_file():
        failed.unlink()
    if js.is_file():
        rec = json.loads(js.read_text(encoding='utf-8'))
        if rec.get('code_mtime', 0) >= code_mtime() - 1:          # built by the current geodiv.py / shell_slots.py
            return 200, {'status': 'ready', 'rec': rec}
    if failed.is_file() and failed.stat().st_mtime >= code_mtime():
        return 200, {'status': 'failed', 'error': failed.read_text(encoding='utf-8')[-1500:]}
    with lock:
        job = (name, key)
        if state['building'] == job:
            return 200, {'status': 'building', 'elapsed': round(time.time() - state['started'])}
        if job not in queue:
            queue.append(job)
            log('queued', name, key)
        return 200, {'status': 'queued', 'position': queue.index(job) + 1, 'building': state['building']}


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        if self.path.split('?')[0].endswith(('.html', '.json', '.js', '/')):    # the page and the result lists are never taken from the browser cache
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        super().end_headers()

    def send_json(self, code, obj):
        body = json.dumps(obj, default=float).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == '/api/variant':
            q = urllib.parse.parse_qs(u.query)
            code, obj = variant(q.get('name', [''])[0], q.get('key', [''])[0], q.get('retry', ['0'])[0] == '1')
            log('request', q.get('name', [''])[0], q.get('key', [''])[0], '->', obj.get('status'), obj.get('error', '')[:80] if obj.get('status') == 'error' else '')
            return self.send_json(code, obj)
        if u.path == '/api/queue':
            with lock:
                return self.send_json(200, {'building': state['building'], 'queue': list(queue)})
        return super().do_GET()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8790)
    a = ap.parse_args()
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer(('127.0.0.1', a.port), partial(Handler, directory=str(OUT)))
    print(f'serving {OUT} on http://127.0.0.1:{a.port}/view.html', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
