"""Process every fitted outfit and armour set of every race into the prototype (2026-11-23, user: "try to process all of these"), in parallel.

    python experiments/shell_slots_20261122/process_all.py [--workers 5] [--races gnome_female,human_male] [--list] [--gpu]

Jobs: every models/<set>/<job>/<race>/outfit, and every body_armor with its own set's right_gauntlet_boot (same job if it has one, else another job
of the set for that race, else the game's bare arms), each module with a fit (fit/align.json) and a reduction. Where a set has several jobs for one
race the newest fit wins. Results go to out/ like the curated ones (index.json under a lock), with geodiv's automatic settings. Resumable: a result
whose record was written by the current geodiv.py / shell_slots.py is skipped. Worker processes each take jobs from a shared queue file; a worker
waits while free memory is below MIN_FREE_GB. Progress: out/process_all.log (one line per result).
"""
import os as _os
for _v in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):      # several workers share the cores
    _os.environ.setdefault(_v, '3')
import argparse
import ctypes
import json
import multiprocessing as mp
import os
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / 'out'
LOG = OUT / 'process_all.log'
MIN_FREE_GB = 2.5
SKIP_SETS = ('chain_shirt',)    # sets not used (user 2026-11-24: chain shirt dropped, chain mail is used instead)


def free_gb():
    class MS(ctypes.Structure):
        _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong), ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong), ('ullTotalVirtual', ctypes.c_ulonglong),
                    ('ullAvailVirtual', ctypes.c_ulonglong), ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
    m = MS()
    m.dwLength = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.ullAvailPhys / 2 ** 30


def ready(p):
    return (p / 'fit' / 'align.json').is_file() and (p / 'reduced' / 'manifest.json').is_file() and any((p / 'reduced').glob('lod_error_0p4.glb'))


def jobs(races=None):
    import geodiv as gd
    cache = ROOT / 'tools' / 'preview' / 'cache' / 'bases'
    all_races = sorted(p.stem for p in cache.glob('*.npz') if not p.stem.endswith('_rig') and (cache / f'{p.stem}_rig.npz').is_file())
    races = [r for r in all_races if not races or r in races]
    out = []
    for race in races:
        best = {}
        for p in ROOT.glob(f'models/*/*/{race}/*'):
            if p.name not in ('outfit', 'body_armor', 'right_gauntlet_boot') or not ready(p):
                continue
            if any(k in p.parts[-4] for k in SKIP_SETS):
                continue
            key = (p.parts[-4], p.name)
            t = (p / 'fit' / 'align.json').stat().st_mtime
            if key not in best or t > best[key][0]:
                best[key] = (t, p)
        for (s, kind), (_, p) in sorted(best.items()):
            if kind == 'outfit':
                out.append({'kind': 'outfit', 'race': race, 'set': s, 'modules': [p.relative_to(ROOT).as_posix()]})
            elif kind == 'body_armor':
                g = p.parent / 'right_gauntlet_boot'
                if not ready(g):
                    g = best.get((s, 'right_gauntlet_boot'), (0, None))[1]
                out.append({'kind': 'armour', 'race': race, 'set': s, 'modules': [p.relative_to(ROOT).as_posix()] + ([g.relative_to(ROOT).as_posix()] if g else [])})
    # names: geodiv's own default names (so curated results are the same entries), the full set name where two jobs would share one
    for j in out:
        s = j['set']
        if j['kind'] == 'outfit':
            j['name'] = f'outfit__{j["race"]}__{s.split("_outfit_")[-1][:28]}'
        else:
            pre = f'{s.split("_")[0]}_{s.split("_")[1]}'
            j['name'] = f'armour__{j["race"]}__{pre}' + (f'__arms_{pre}' if len(j['modules']) > 1 else '__base_arms')
    from collections import Counter
    c = Counter(j['name'] for j in out)
    for j in out:
        if c[j['name']] > 1:
            j['name'] = f'{j["kind"]}__{j["race"]}__{j["set"]}'
    return out


def current(name, code_mtime):
    idx = OUT / 'index.json'
    if not idx.is_file() or not (OUT / f'{name}.glb').is_file():
        return False
    for r in json.loads(idx.read_text()).get('results', []):
        if r['name'] == name:
            return r.get('code_mtime', 0) >= code_mtime - 1
    return False


def log(line):
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(time.strftime('%H:%M:%S ') + line + '\n')


def take(queue_path, lock_path):
    """Pop the next job from the shared queue file (under a lock file)."""
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            time.sleep(0.1)
    try:
        q = json.loads(Path(queue_path).read_text())
        if not q:
            return None
        j = q.pop(0)
        Path(queue_path).write_text(json.dumps(q))
        return j
    finally:
        os.close(fd)
        os.unlink(lock_path)


def _patient_replace(replace):
    """os.replace that retries for a few seconds: on Windows it fails while another process has the target open (out/index.json is read
    by the viewer and by a second run's up-to-date check while workers replace it - one result was lost that way on 2026-11-24)."""
    def rep(src, dst, *a, **k):
        for i in range(40):
            try:
                return replace(src, dst, *a, **k)
            except PermissionError:
                if i == 39:
                    raise
                time.sleep(.25)
    return rep


def worker(wid, queue_path):
    sys.path.insert(0, str(HERE))
    os.replace = _patient_replace(os.replace)
    import geodiv as gd
    lock = str(queue_path) + '.lock'
    while True:
        while free_gb() < MIN_FREE_GB:
            time.sleep(10)
        j = take(queue_path, lock)
        if j is None:
            return
        t0 = time.time()
        try:
            mods = [ROOT / m for m in j['modules']]
            if j['kind'] == 'outfit':
                rec = gd.run_outfit(mods[0], name=j['name'])
            else:
                rec = gd.run_armour(mods[0], mods[1] if len(mods) > 1 else None, name=j['name'])
            w_ = rec.get('watertight', {})
            log(f'w{wid} done {j["name"]} {time.time() - t0:.0f} s {rec["stats"]["faces"]} faces, open {w_.get("open_m")} m, '
                f'non-manifold {w_.get("nonmanifold_edges")} | {rec.get("options", "")}')
        except Exception as e:  # noqa: BLE001
            log(f'w{wid} FAILED {j["name"]} {type(e).__name__}: {e} | ' + traceback.format_exc().strip().splitlines()[-2][:200])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workers', type=int, default=5)
    ap.add_argument('--races', default='')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--force', action='store_true', help='rebuild results that are already current')
    ap.add_argument('--gpu', action='store_true', help='BODY_FIT_GPU=1 for the workers: ray tests, closest points and z-buffers on the GPU '
                    '(tools/tripo_pipeline/gpu_geometry.py; same results, ~4x faster per job)')
    a = ap.parse_args()
    if a.gpu:
        os.environ['BODY_FIT_GPU'] = '1'                  # before geodiv / body_fit are imported here or in the spawned workers
    sys.path.insert(0, str(HERE))
    import geodiv as gd
    js = jobs([r for r in a.races.split(',') if r] or None)
    todo = [j for j in js if a.force or not current(j['name'], gd.CODE_MTIME)]
    # gnome first (the user's main concern), then the rest race by race
    todo.sort(key=lambda j: (j['race'] != 'gnome_female', j['race'], j['kind'], j['name']))
    print(f'{len(js)} results, {len(js) - len(todo)} current, {len(todo)} to build with {a.workers} workers', flush=True)
    if a.list:
        for j in todo:
            print(j['name'], j['modules'])
        return
    qp = OUT / f'process_all_queue_{os.getpid()}.json'          # several runs (e.g. one race again) can go side by side
    qp.write_text(json.dumps(todo))
    log(f'start: {len(todo)} to build, {a.workers} workers' + (', GPU' if a.gpu else ''))
    ps = [mp.Process(target=worker, args=(i, qp)) for i in range(a.workers)]
    for p in ps:
        p.start()
        time.sleep(3)
    for p in ps:
        p.join()
    log('all done')
    print('all done', flush=True)


if __name__ == '__main__':
    main()
