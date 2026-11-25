"""rebuild_open.py: rebuild only the modules whose layers still have open edges (from the scene arrays), then rewrite the index."""
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path('F:/godot/blender/characters/imageto3d/experiments/shell_slots_20261122')
OUT = HERE / 'out' / 'layers'
SLOT_MODULE = {'torso': 'body_armor', 'outfit': 'outfit', 'lower_arm': 'right_gauntlet_boot', 'lower_leg': 'right_gauntlet_boot'}


def open_edges(a0):
    z = np.load(a0)
    V, F = z['gV'].astype(float), z['gF'].astype(np.int64)
    _, vid = np.unique(np.round(V * 1e6).astype(np.int64), axis=0, return_inverse=True)
    vid = vid.ravel()
    e = np.sort(vid[np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])], 1)
    e = e[e[:, 0] != e[:, 1]]
    _, c = np.unique(e, axis=0, return_counts=True)
    return int((c == 1).sum())


if __name__ == '__main__':
    os.environ['BODY_FIT_GPU'] = '1'
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    sys.path.insert(0, str(HERE))
    import layers as L
    import process_all as pa
    want = set()
    for a0 in OUT.glob('*/scenes/*__a0.npz'):
        if '__bare__' in a0.name or not open_edges(a0):
            continue
        race = a0.parent.parent.name
        slot, sset = a0.name[:-len('__a0.npz')].split('__', 1)
        want.add((race, sset, SLOT_MODULE[slot.split('.')[0]]))
    mods = sorted({m for jb in pa.jobs(None) for m in jb['modules'] if (m.split('/')[3], m.split('/')[1], m.split('/')[4]) in want})
    print(time.strftime('%H:%M:%S'), f'{len(want)} layer groups with open edges -> {len(mods)} modules', flush=True)
    failed = 0
    with mp.Pool(11, initializer=L._setup) as pool:
        for msg in pool.imap_unordered(L._run, [('module', str(pa.ROOT / m)) for m in mods]):
            failed += msg.startswith('FAILED')
            print(time.strftime('%H:%M:%S'), msg[:220], flush=True)
    print(time.strftime('%H:%M:%S'), f'{failed} failed; index: {L.write_index()} layers', flush=True)
