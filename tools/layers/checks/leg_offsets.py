"""leg_offsets.py <race> <set substring> [module] [--lod 0p4|0p2|0p1] [--job JOBID]: per leg, the offset (game leg section centre - generated module section
centre, across the limb, mm) at fractions along the thigh (hip -> knee) and shin (knee -> ankle), from the aligned module as the layer build loads it.

--lod: the reduction level to load; default the LOD of the module's newest build pointer (<module>/layers/current_<lod>.json), else 0p4. Races whose game
body has no legs (the gnome) are skipped with a note, as layers.centre_legs does."""
import argparse
import os
import sys

import numpy as np

os.environ.setdefault('BODY_FIT_GPU', '1')
sys.path.insert(0, 'F:/godot/blender/characters/imageto3d/tools/layers')
import geodiv as gd  # noqa: E402
import layers as L  # noqa: E402
import process_all as pa  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument('race')
ap.add_argument('sub')
ap.add_argument('module', nargs='?', default='body_armor')
ap.add_argument('--lod', choices=('0p4', '0p2', '0p1'))
ap.add_argument('--job')
a = ap.parse_args()
race, sub, module = a.race, a.sub, a.module


def pointer_lod(mod_dir):
    """The LOD of the newest current_<lod>.json of the module's layers folder, or None."""
    ptrs = sorted((p for p in (mod_dir / 'layers').glob('current_*.json') if p.stem != 'current_bare'), key=lambda p: p.stat().st_mtime)
    return ptrs[-1].stem[len('current_'):] if ptrs else None


body = L.get_body(race, gd)
S = L.slots(body, gd)
if not all(k in S for k in ('lower_leg.r', 'lower_leg.l')):
    print(f'{race}: the game body has no legs: nothing to measure')
    sys.exit(0)
ob = L.out_body(body, gd)
pn = body.part_names
lods = {m: (a.lod or pointer_lod(pa.ROOT / m) or '0p4') for m in
        (m for lod in ((a.lod,) if a.lod else ('0p4', '0p2', '0p1')) for jb in pa.jobs([race], lod=lod, job=a.job) for m in jb['modules']
         if sub in m.split('/')[1] and m.split('/')[4] == module and m.split('/')[3] == race)}
for mod, lod in sorted(lods.items()):
    if not (pa.ROOT / mod / 'reduced' / f'lod_error_{lod}.glb').is_file():
        print(mod, f'no lod_error_{lod}.glb')
        continue
    m, info = L.prep(pa.ROOT / mod, body, race, gd, lod)
    print(mod, info.get('source_lod'))
    for side in ('r', 'l'):
        th, lg, ft = (pn.index(f'{p}.{side}') for p in ('thigh_stretch', 'leg_stretch', 'foot'))
        root = [i for i, n in enumerate(pn) if n.startswith(('root', 'spine_01', 'pelvis', 'hips'))]
        if not root or min(int(np.isin(ob.part, ids).sum()) for ids in ([th], [lg], [ft], root)) < 4:      # (centre_legs' guard: parts named, no faces)
            print(f'  {side}: no game leg')
            continue
        vs = lambda ids: set(ob.F[np.isin(ob.part, ids)].ravel().tolist())  # noqa: E731
        J0 = gd._joint(ob, vs([th]), vs(root))
        J1 = gd._joint(ob, vs([th]), vs([lg]))
        J2 = gd._joint(ob, vs([lg]), vs([ft]))
        legp = [i for i, n in enumerate(pn) if n in (f'thigh_stretch.{side}', f'leg_stretch.{side}', f'knee_cop.{side}')]
        mf = np.isin(m.part, legp)
        gf = np.isin(ob.part, [th, lg])
        row = []
        for seg, (A, B) in (('thigh', (J0, J1)), ('shin', (J1, J2))):
            ax = (B - A) / np.linalg.norm(B - A)
            Lg = np.linalg.norm(B - A)
            for fr in (.2, .4, .6, .8) if seg == 'thigh' else (.2, .4, .6):
                gc = gd._section_centre(ob.V, ob.F[gf], A, ax, fr * Lg)
                mc = gd._section_centre(m.V, m.F[mf], A, ax, fr * Lg)
                if gc is None or mc is None:
                    row.append(f'{seg}{fr:.1f}: -')
                    continue
                o = gc - mc
                o -= (o @ ax) * ax
                row.append(f'{seg}{fr:.1f}: fwd {-o[1] * 1000:+5.0f} lat {o[0] * 1000:+5.0f}')
        print(f'  {side}: ' + ' | '.join(row))
