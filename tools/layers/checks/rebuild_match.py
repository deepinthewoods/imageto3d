"""rebuild_match.py <races,|all> <set substring> [module (body_armor|outfit|right_gauntlet_boot)] [--lod 0p4] [--job JOBID] [--out DIR] [--workers 6] [--cpu] [--force]

A COMPARISON tool: builds the matching modules' layers with build.build_module, one process per module (several at a time), into SCRATCH folders
DIR/<race>/<set>/<module>/ (default DIR: <system temp>/layers_rebuild_match; --out into models/ is refused). Production rebuilds go through the app (its
layer lane builds the pinned job at the approved LOD); this script never touches a module's own layers/ folder or its pointers.

The requested LOD's file is what is discovered (reduced/lod_error_<lod>.glb). Where a set has several jobs for the race, --job names the one to build, else
the job whose reduced/manifest.json is newest (job id as the tie-break). Prints one line per module; exit 1 when any build failed (or nothing matched)."""
import argparse
import multiprocessing as mp
import os
import sys
import tempfile
import time
from pathlib import Path

LAYERS = Path(__file__).resolve().parents[1]


def _one(job):
    mod, lod, out, gpu, force = job
    sys.path.insert(0, str(LAYERS))
    import build
    try:
        r = build.build_module(mod, lod, out, gpu=gpu, force=force)
    except Exception as error:  # noqa: BLE001
        r = {'status': 'failed', 'error': f'{type(error).__name__}: {error}'}
    return r['status'], f"{r['status']:8s} {mod} {r.get('seconds', '')} s {(r.get('error') or '')[:200]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('races')
    ap.add_argument('sub')
    ap.add_argument('module', nargs='?')
    ap.add_argument('--lod', default='0p4', choices=('0p4', '0p2', '0p1'))
    ap.add_argument('--job')
    ap.add_argument('--out', default=str(Path(tempfile.gettempdir()) / 'layers_rebuild_match'))
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--cpu', action='store_true')
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args()
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    sys.path.insert(0, str(LAYERS))
    import process_all as pa
    out_root = Path(a.out).resolve()
    if out_root == (pa.ROOT / 'models').resolve() or (pa.ROOT / 'models').resolve() in out_root.parents:
        print(f'refusing --out {out_root}: scratch output only (production layers are built through the app)', flush=True)
        return 1
    races = None if a.races == 'all' else a.races.split(',')
    mods = sorted({m for jb in pa.jobs(races, lod=a.lod, job=a.job) for m in jb['modules']
                   if a.sub in m.split('/')[1] and (a.module is None or m.split('/')[4] == a.module) and (races is None or m.split('/')[3] in races)})
    jobs = [(str(pa.ROOT / m), a.lod, str(out_root / m.split('/')[3] / m.split('/')[1] / m.split('/')[4]), not a.cpu, a.force) for m in mods]
    print(time.strftime('%H:%M:%S'), f'{len(mods)} modules -> {out_root}', flush=True)
    failed = 0
    if jobs:
        with mp.Pool(min(a.workers, len(jobs))) as pool:
            for status, msg in pool.imap_unordered(_one, jobs):
                failed += status == 'failed'
                print(time.strftime('%H:%M:%S'), msg, flush=True)
    print(time.strftime('%H:%M:%S'), f'{len(mods) - failed} ok, {failed} failed', flush=True)
    return 1 if failed or not jobs else 0


if __name__ == '__main__':
    sys.exit(main())
