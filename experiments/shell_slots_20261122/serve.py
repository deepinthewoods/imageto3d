"""Shell / slot prototype server (2026-11-23): out/ as static files for the Mix page.

    python experiments/shell_slots_20261122/serve.py [--port 8790]

RETIRED (2026-11-25): result variants. GET /api/variant answers 410 {"status": "error", "error": "variants retired; ..."} and builds nothing (the builder
`geodiv.py variant` was pruned when the layer code moved to tools/layers/); GET /api/queue is always empty. The static pages in out/ are served as before.
"""
import argparse
import json
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / 'out'
RETIRED = 'variants retired; the layer builder moved to tools/layers (python tools/layers/build.py module <module_dir> --lod 0p2)'


def variant(name, key, retry):
    """Variants are retired (2026-11-25): their builder (geodiv.py variant) was pruned with the move to tools/layers. A clear JSON error, no files touched."""
    return 410, {'status': 'error', 'error': RETIRED}


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
            return self.send_json(code, obj)
        if u.path == '/api/queue':
            return self.send_json(200, {'building': None, 'queue': []})
        return super().do_GET()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8790)
    a = ap.parse_args()
    srv = ThreadingHTTPServer(('127.0.0.1', a.port), partial(Handler, directory=str(OUT)))
    print(f'serving {OUT} on http://127.0.0.1:{a.port}/view.html', flush=True)
    srv.serve_forever()


if __name__ == '__main__':
    main()
