"""leg_offsets.py <race> <set substring> [module]: per leg, the offset (game leg section centre - generated module section centre, across the
limb, mm) at fractions along the thigh (hip -> knee) and shin (knee -> ankle), from the aligned module as the layer build loads it."""
import os
import sys

import numpy as np

os.environ.setdefault('BODY_FIT_GPU', '1')
sys.path.insert(0, 'F:/godot/blender/characters/imageto3d/experiments/shell_slots_20261122')
import geodiv as gd  # noqa: E402
import layers as L  # noqa: E402
import process_all as pa  # noqa: E402

race, sub = sys.argv[1], sys.argv[2]
module = sys.argv[3] if len(sys.argv) > 3 else 'body_armor'
mods = [m for jb in pa.jobs([race]) for m in jb['modules'] if sub in m.split('/')[1] and m.split('/')[4] == module and m.split('/')[3] == race]
body = L.get_body(race, gd)
ob = L.out_body(body, gd)
pn = body.part_names
for mod in mods:
    m, info = L.prep(pa.ROOT / mod, body, race, gd)
    print(mod, info.get('source_lod'))
    for side in ('r', 'l'):
        th, lg, ft = (pn.index(f'{p}.{side}') for p in ('thigh_stretch', 'leg_stretch', 'foot'))
        root = [i for i, n in enumerate(pn) if n.startswith(('root', 'spine_01', 'pelvis', 'hips'))]
        V = ob.V
        vs = lambda ids: set(ob.F[np.isin(ob.part, ids)].ravel().tolist())  # noqa: E731
        J0 = gd._joint(ob, vs([th]), vs(root))
        J1 = gd._joint(ob, vs([th]), vs([lg]))
        J2 = gd._joint(ob, vs([lg]), vs([ft]))
        legp = [i for i, n in enumerate(pn) if n in (f'thigh_stretch.{side}', f'leg_stretch.{side}', f'knee_cop.{side}')]
        mf = np.isin(m.part, legp)
        gf = np.isin(ob.part, [th, lg])
        row = []
        for seg, (A, B) in (('thigh', (J0, J1)), ('shin', (J1, J2))):
            a = (B - A) / np.linalg.norm(B - A)
            Lg = np.linalg.norm(B - A)
            for fr in (.2, .4, .6, .8) if seg == 'thigh' else (.2, .4, .6):
                gc = gd._section_centre(ob.V, ob.F[gf], A, a, fr * Lg)
                mc = gd._section_centre(m.V, m.F[mf], A, a, fr * Lg)
                if gc is None or mc is None:
                    row.append(f'{seg}{fr:.1f}: -')
                    continue
                o = gc - mc
                o -= (o @ a) * a
                row.append(f'{seg}{fr:.1f}: fwd {-o[1] * 1000:+5.0f} lat {o[0] * 1000:+5.0f}')
        print(f'  {side}: ' + ' | '.join(row))
