"""flap_faces.py <layer glb> <race> <kx,ky,kz>: garment faces within 15 cm of the knee point, below it at Amount 100, moved > 15 mm by the
morph: their Amount 0 depth (centre + corners, mm) and whether keep_hidden would call them buried."""
import sys
from collections import Counter

import numpy as np

sys.path.insert(0, 'F:/godot/blender/characters/imageto3d/tools/layers')
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
import geodiv as gd  # noqa: E402
import glbread2  # noqa: E402
import layers as L  # noqa: E402

path, race = sys.argv[1], sys.argv[2]
J = np.array([float(x) for x in sys.argv[3].split(',')])
V0, F, mat = glbread2.read(path)
V1, _, _ = glbread2.read(path, morph='fill')
body = L.get_body(race, gd)
S = L.slots(body, gd)
c0, c1 = V0[F].mean(1), V1[F].mean(1)
mv = np.linalg.norm(c1 - c0, axis=1)
sel = (np.linalg.norm(c0 - J, axis=1) < .15) & (c1[:, 2] < J[2] - .03) & (mv > .015) & np.array([not m.startswith('skin') for m in mat])
print('flap faces', int(sel.sum()), Counter(mat[sel]).most_common())
nb = 0
for f in np.flatnonzero(sel):
    ms = body.measure(np.r_[c0[f][None], V0[F[f]]])
    buried = bool((ms['d'] < -.001).all() and not np.isin(ms['part'], S['torso']).any())
    nb += buried
    print(f, mat[f], 'a0 depth mm', np.round(ms['d'] * 1000).astype(int).tolist(), 'buried' if buried else '-', 'moved %.0f' % (mv[f] * 1000))
print('buried', nb, 'of', int(sel.sum()))
