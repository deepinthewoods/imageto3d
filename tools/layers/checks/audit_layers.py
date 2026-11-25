"""audit_layers.py [race ...]: per layer (from the scene arrays): faces turning over anywhere along the slider, duplicate triangles, edges with
more than two faces, open edges (one face). Prints totals and the worst layers."""
import sys
from pathlib import Path

import numpy as np

OUT = Path('F:/godot/blender/characters/imageto3d/experiments/shell_slots_20261122/out/layers')
races = sys.argv[1:] or sorted(p.name for p in OUT.iterdir() if p.is_dir())
rows = []
for race in races:
    for a0 in sorted((OUT / race / 'scenes').glob('*__a0.npz')):
        stem = a0.name[:-len('__a0.npz')]
        z0, z1 = np.load(a0), np.load(a0.with_name(f'{stem}__a100.npz'))
        V0, V1, F = z0['gV'].astype(np.float64), z1['gV'].astype(np.float64), z0['gF'].astype(np.int64)
        e1, e2 = V0[F[:, 1]] - V0[F[:, 0]], V0[F[:, 2]] - V0[F[:, 0]]
        n0 = np.cross(e1, e2)
        a = (n0 * n0).sum(1)
        D = V1 - V0
        d1, d2 = D[F[:, 1]] - D[F[:, 0]], D[F[:, 2]] - D[F[:, 0]]
        b = (n0 * (np.cross(e1, d2) + np.cross(d1, e2))).sum(1)
        c = (n0 * np.cross(d1, d2)).sum(1)
        tm = np.clip(np.where(c > 0, -b / np.where(c > 0, 2 * c, 1.), 1.), 0, 1)
        fmin = np.minimum(a + b + c, a + b * tm + c * tm * tm)
        live = a > 1e-14
        flips = int((live & (fmin <= 0)).sum())
        st = np.sort(F, 1)
        dup = len(st) - len(np.unique(st, axis=0))
        e = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1)
        # vertices are not shared across primitives in the npz? they are (one mesh); weld by position to be safe
        key = np.round(V0 * 1e6).astype(np.int64)
        _, vid = np.unique(key, axis=0, return_inverse=True)
        vid = vid.ravel()
        ew = np.sort(vid[e], 1)
        ew = ew[ew[:, 0] != ew[:, 1]]
        _, cnt = np.unique(ew, axis=0, return_counts=True)
        rows.append((race, stem, len(F), flips, dup, int((cnt > 2).sum()), int((cnt == 1).sum())))
tot = np.array([r[3:] for r in rows]) if rows else np.zeros((0, 4))
print(f'{len(rows)} layers | faces turning over along the slider: {int(tot[:, 0].sum())} in {int((tot[:, 0] > 0).sum())} layers | duplicate '
      f'triangles: {int(tot[:, 1].sum())} in {int((tot[:, 1] > 0).sum())} | edges with >2 faces: {int(tot[:, 2].sum())} in {int((tot[:, 2] > 0).sum())} '
      f'| open edges: {int(tot[:, 3].sum())} in {int((tot[:, 3] > 0).sum())} layers')
for r in sorted(rows, key=lambda r: -(r[3] * 100 + r[4] * 10 + r[5] + r[6]))[:12]:
    if any(r[3:]):
        print('  ', r)
