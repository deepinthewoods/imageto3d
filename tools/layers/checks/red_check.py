"""red_check.py <race> <build dir> [stem substring ...]: per saved layer (<build dir>/scenes/<stem>__a0/__a100.npz of a tools/layers/build.py build), the layer alone from 24 directions at 4 mm a
pixel: visible garment samples showing their BACK (red in the viewer), and inner/outer garment face pairs passing through each other.

Garment = every face whose ROLE is not 'skin' (the role / provenance NAMES the scene arrays carry: gRole / gProv, written by layers.write_layer; the
generator's skin, the game body's skin, caps and body strips are not garment). Scenes written before those arrays existed must be rebuilt."""
import os
import sys
from pathlib import Path

import numpy as np

os.environ.setdefault('BODY_FIT_GPU', '1')
sys.path.insert(0, 'F:/godot/blender/characters/imageto3d/tools/layers')
import geodiv as gd  # noqa: E402
import layers as L  # noqa: E402
import visibility as vis  # noqa: E402

DIRS = vis.view_dirs(rings=(-45, 0, 45), per_ring=8, poles=False)
race, build_dir, subs = sys.argv[1], Path(sys.argv[2]), sys.argv[3:]
body = L.get_body(race, gd)
for a0 in sorted((build_dir / 'scenes').glob('*__a0.npz')):
    stem = a0.name[:-len('__a0.npz')]
    if subs and not any(s in stem for s in subs):
        continue
    row = []
    for tag in ('a0', 'a100'):
        z = np.load(a0.with_name(f'{stem}__{tag}.npz'))
        V, F = z['gV'].astype(float), z['gF'].astype(np.int64)
        if 'gRole' not in z.files:
            raise SystemExit(f'{a0.name}: no gRole array (scene written by an older builder): rebuild the layer')
        garm = z['gRole'] != gd.SKIN
        v = vis.visibility(V, F, px=.004, dirs=DIRS, see_through=False)
        seen = (v['front'] + v['back'])[garm].sum()
        red = v['back'][garm].sum()
        m = type('M', (), {})()
        m.V, m.F, m.role = V, F, z['gRole']
        m.prov = z['gProv']
        side = L.wall_sides(m, body, gd)
        n = len(L.wall_crossings(V, F, side)[0])
        row.append(f'{tag}: red {int(red):5d} samples ({100 * red / max(seen, 1):.2f} %), crossing pairs {n:4d}')
    print(f'{race} {stem[:48]:48s} ' + ' | '.join(row), flush=True)
