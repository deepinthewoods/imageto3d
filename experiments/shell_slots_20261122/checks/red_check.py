"""red_check.py <race> <stem substring> [...]: per saved layer (scenes/<stem>__a0/__a100.npz), the layer alone from 24 directions at 4 mm a
pixel: visible garment samples showing their BACK (red in the viewer), and inner/outer garment face pairs passing through each other."""
import os
import sys
from pathlib import Path

import numpy as np

os.environ.setdefault('BODY_FIT_GPU', '1')
sys.path.insert(0, 'F:/godot/blender/characters/imageto3d/experiments/shell_slots_20261122')
import geodiv as gd  # noqa: E402
import layers as L  # noqa: E402
import visibility as vis  # noqa: E402

OUT = Path('F:/godot/blender/characters/imageto3d/experiments/shell_slots_20261122/out/layers')
SKIN_COLS = np.array([[.86, .55, .45], [.95, .85, .55]])
DIRS = vis.view_dirs(rings=(-45, 0, 45), per_ring=8, poles=False)
race, subs = sys.argv[1], sys.argv[2:]
body = gd.game_body(race)
for a0 in sorted((OUT / race / 'scenes').glob('*__a0.npz')):
    stem = a0.name[:-len('__a0.npz')]
    if subs and not any(s in stem for s in subs):
        continue
    row = []
    for tag in ('a0', 'a100'):
        z = np.load(a0.with_name(f'{stem}__{tag}.npz'))
        V, F, C = z['gV'].astype(float), z['gF'].astype(np.int64), z['gC'].astype(float)
        garm = np.abs(C[:, None, :] - SKIN_COLS[None]).sum(2).min(1) > .05
        v = vis.visibility(V, F, px=.004, dirs=DIRS, see_through=False)
        seen = (v['front'] + v['back'])[garm].sum()
        red = v['back'][garm].sum()
        m = type('M', (), {})()
        m.V, m.F, m.role = V, F, np.where(garm, 'primary', gd.SKIN)
        m.prov = np.full(len(F), 'tripo')
        side = L.wall_sides(m, body, gd)
        n = len(L.wall_crossings(V, F, side)[0])
        row.append(f'{tag}: red {int(red):5d} samples ({100 * red / max(seen, 1):.2f} %), crossing pairs {n:4d}')
    print(f'{race} {stem[:48]:48s} ' + ' | '.join(row), flush=True)
