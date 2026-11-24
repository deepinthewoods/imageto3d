"""Build result variants without the server (2026-11-23): one at a time, resumable.

    python experiments/shell_slots_20261122/build_variants.py                 the default combinations for every result
    python experiments/shell_slots_20261122/build_variants.py --keys a1-boff-c1-d1-e1 --only gnome

Default keys: a0-boff-c0-d0-e1 (density-aware) and a1-boff-c1-d1-e1 (all checkboxes) for every result, plus a1-bwhole_mirror-c1-d1-e1 for gnome
results; gnome results first. A variant already built by the current geodiv.py / shell_slots.py is skipped, so stopping and re-running carries on.
Writes out/variants/<name>__<key>.* exactly like serve.py's on-request builds; view.html loads them with or without serve.py.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from serve import KEY_RE, OUT, ROOT, VAR, code_mtime  # noqa: E402


def current(name, key):
    js = VAR / f'{name}__{key}.json'
    if not js.is_file():
        return False
    try:
        return json.loads(js.read_text(encoding='utf-8')).get('code_mtime', 0) >= code_mtime() - 1
    except ValueError:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--keys', help='comma-separated variant keys (default: see the module docstring)')
    ap.add_argument('--only', default='', help='only results whose name contains this')
    a = ap.parse_args()
    names = sorted((r['name'] for r in json.loads((OUT / 'index.json').read_text())['results'] if a.only in r['name']), key=lambda n: ('gnome' not in n, n))
    if a.keys:
        jobs = [(n, k) for k in a.keys.split(',') for n in names]
    else:
        jobs = [(n, 'a0-boff-c0-d0-e1') for n in names] + [(n, 'a1-boff-c1-d1-e1') for n in names] + \
               [(n, 'a1-bwhole_mirror-c1-d1-e1') for n in names if 'gnome' in n]
    bad = [k for _, k in jobs if not KEY_RE.match(k)]
    if bad:
        raise SystemExit(f'bad key {bad[0]}')
    todo = [j for j in jobs if not current(*j)]
    print(f'{len(jobs)} variants, {len(jobs) - len(todo)} already current, {len(todo)} to build', flush=True)
    for i, (name, key) in enumerate(todo, 1):
        t0 = time.time()
        r = subprocess.run([sys.executable, str(HERE / 'geodiv.py'), 'variant', name, key], cwd=ROOT, capture_output=True, text=True)
        ok = r.returncode == 0 and current(name, key)
        if not ok:
            (VAR / f'{name}__{key}.failed').write_text(((r.stderr or '') + (r.stdout or ''))[-4000:], encoding='utf-8')
        print(f'[{i}/{len(todo)}] {"built " if ok else "FAILED"} {name} {key} {time.time() - t0:.0f} s', flush=True)


if __name__ == '__main__':
    main()
