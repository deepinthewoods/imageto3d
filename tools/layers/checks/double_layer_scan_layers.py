"""double_layer_scan_layers.py [races]: the double_layer_scan.py measure (inner wall: garment facing the body with garment just outside it;
stacked: outward garment with garment just outside) on the CURRENT layer outputs (out/layers/<race>/scenes/<slot>__<set>__a0.npz)."""
import json
import os
import sys
from pathlib import Path

import numpy as np

os.environ.setdefault('BODY_FIT_GPU', '1')
sys.path.insert(0, 'F:/godot/blender/characters/imageto3d/experiments/shell_slots_20261122')
import geodiv as gd  # noqa: E402

OUT = Path('F:/godot/blender/characters/imageto3d/experiments/shell_slots_20261122/out/layers')
SP = Path(__file__).parent
WALL_MM = 40.0
SKIN_COLS = np.array([[.86, .55, .45], [.95, .85, .55]])
SLOTS = ('outfit', 'torso', 'lower_arm.r', 'lower_leg.r')
races = sys.argv[1].split(',') if len(sys.argv) > 1 else sorted(p.name for p in OUT.iterdir() if p.is_dir())
res = []
for race in races:
    body = gd.game_body(race)
    for p in sorted((OUT / race / 'scenes').glob('*__a0.npz')):
        stem = p.name[:-len('__a0.npz')]
        if '__bare' in stem or stem.split('__')[0] not in SLOTS:
            continue
        z = np.load(p)
        V, F, C = z['gV'].astype(float), z['gF'].astype(np.int64), z['gC'].astype(float)
        garm = np.abs(C[:, None, :] - SKIN_COLS[None]).sum(2).min(1) > .05
        if garm.sum() < 10:
            continue
        T = V[F]
        cen = T.mean(1)
        nf = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
        area = np.linalg.norm(nf, axis=1) / 2
        nf = nf / np.maximum(2 * area, 1e-18)[:, None]
        _, tri, _ = body.closest(cen)
        nb = body.tri_n[tri]
        dot = (nf * nb).sum(1)
        TG = T[np.flatnonzero(garm)]
        out = {}
        for kind, sel in (('inner_wall', garm & (dot < -.3)), ('stacked', garm & (dot > .3))):
            fi = np.flatnonzero(sel)
            hit = np.zeros(len(fi), bool)
            for k in range(0, len(fi), 256):
                ff = fi[k:k + 256]
                t = gd.ray_first_hit(cen[ff] + nb[ff] * 1e-3, nb[ff], TG[:, 0], TG[:, 1], TG[:, 2])
                hit[k:k + 256] = np.isfinite(t) & (t < WALL_MM / 1000)
            out[kind] = float(area[fi[hit]].sum() / area[garm].sum())
        res.append({'race': race, 'layer': stem, 'faces': len(F), 'inner_wall_frac': round(out['inner_wall'], 4),
                    'stacked_frac': round(out['stacked'], 4)})
    print(race, 'done', flush=True)
(SP / 'double_layers_layers.json').write_text(json.dumps(res, indent=0))
res.sort(key=lambda x: -x['inner_wall_frac'])
print('\nWORST inner walls:')
for x in res[:15]:
    print(f"  {x['inner_wall_frac'] * 100:5.1f} %  {x['race']:14s} {x['layer']}")
for slot in SLOTS:
    iw = np.array([x['inner_wall_frac'] for x in res if x['layer'].startswith(slot + '__')])
    if len(iw):
        print(f'{slot:12s} {len(iw):4d} layers: median {np.median(iw) * 100:4.1f} %, > 5 %: {int((iw > .05).sum())}, > 10 %: {int((iw > .10).sum())}')
