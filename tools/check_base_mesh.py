"""Check the game base bodies (projects/<race>.blend, mesh `base`) for holes, flaps and bits sticking out (2026-11-24, user: "Generally, if
there's flaps or just bits that are sticking out, we want to remove them ... the meshes should be just like a kind of whole without any flappy
bits sticking out").

    python tools/check_base_mesh.py [race ...] [--json out.json]

For each body (all of projects/*.blend with a `base` mesh when no race is named) Blender 5.1.2 dumps the mesh with its own polygon
triangulation (what the exporters use), then:
  open / non-manifold   edges used by 1 / 3+ polygons (should be 0 / 0)
  degenerate            polygons whose triangulation contains a zero-area triangle (the exporter drops it: a crack)
  seen from behind      the body is rendered from 100 directions (z-buffer, 2 mm pixels); on a closed outward-facing body no face is ever
                        seen from behind - one that is is a flap, fin, fold or inside-out face (should be 0)
  outward folds         neighbouring polygons folded more than 150 degrees with the fold sticking OUT (a blade or flap). Real features can
                        do this (elf ears, tapered hand tips, the halfling heel), so these are listed for a look, not counted as errors.
Inward folds (narrow clefts: toe gaps, creases) are only counted.
Fixes: tools/fix_base_holes.py (holes, hidden faces by visibility, flaps via --delete-faces, --move-vertex, --retriangulate-degenerate) and
tools/rebuild_base_bottom.py (gnome bottom). Re-export with tools/preview/export_bases.py --force <race> afterwards."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
BLENDER = r'C:\Users\niall\Tools\blender-5.1.2-windows-x64\blender.exe'
sys.path.insert(0, str(ROOT / 'tools' / 'layers'))      # visibility.py moved here from experiments/shell_slots_20261122 (pipeline v2 phase 2)
import visibility as vis  # noqa: E402

DUMP = r'''
import sys, bpy, numpy as np
out = sys.argv[sys.argv.index('--') + 1]
ob = bpy.data.objects.get('base')
if ob is None:
    raise SystemExit('no base')
me = ob.data
M = ob.matrix_world
V = np.array([list(M @ v.co) for v in me.vertices])
me.calc_loop_triangles()
F = np.array([tuple(lt.vertices) for lt in me.loop_triangles])
O = np.array([lt.polygon_index for lt in me.loop_triangles])
ta = np.array([lt.area for lt in me.loop_triangles])
cnt = {}
for p in me.polygons:
    for ek in p.edge_keys:
        cnt.setdefault(ek, []).append(p.index)
pairs = np.array([(a, b) for ps in cnt.values() for a in ps for b in ps if a < b] or [(0, 0)])
np.savez(out, V=V, F=F, O=O, ta=ta, pairs=pairs, area=np.array([p.area for p in me.polygons]),
         open=sum(len(v) == 1 for v in cnt.values()), nonman=sum(len(v) > 2 for v in cnt.values()))
'''
FOLD_DEG = 150.0
DIRS = 100
PX = 0.002
MIN_BACK_PX = 10


def scan(npz):
    z = np.load(npz)
    V, F, O, area, pairs = z['V'], z['F'], z['O'], z['area'], z['pairs']
    NP = len(area)
    N = vis.face_normals(V, F)
    k = np.arange(DIRS) + 0.5
    phi, th = np.arccos(1 - 2 * k / DIRS), np.pi * (1 + 5 ** 0.5) * k
    D = np.stack([np.cos(th) * np.sin(phi), np.sin(th) * np.sin(phi), np.cos(phi)], 1)
    front, back = np.zeros(NP), np.zeros(NP)
    for d in D:
        img, lo, (dd, r, u) = vis.zbuffer(V, F, d, PX)
        f = img[img >= 0]
        s = N[f] @ dd
        np.add.at(front, O[f[s < 0]], 1)
        np.add.at(back, O[f[s >= 0]], 1)
    ta = np.linalg.norm(np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]), axis=1) / 2
    pn = np.zeros((NP, 3))
    np.add.at(pn, O, N * ta[:, None])
    pn /= np.maximum(np.linalg.norm(pn, axis=1), 1e-20)[:, None]
    pc = np.zeros((NP, 3))
    np.add.at(pc, O, V[F].mean(1) * ta[:, None])
    pc /= np.maximum(np.bincount(O, ta, NP), 1e-20)[:, None]
    behind = [{'poly': int(p), 'at': [round(float(c), 4) for c in pc[p]], 'back_px': int(back[p]), 'front_px': int(front[p])}
              for p in range(NP) if back[p] >= MIN_BACK_PX]
    out_folds, clefts = [], 0
    for a, b in {(int(min(a, b)), int(max(a, b))) for a, b in pairs if a != b}:
        if pn[a] @ pn[b] < np.cos(np.radians(FOLD_DEG)):
            if (pc[b] - pc[a]) @ pn[a] < 0 and (pc[a] - pc[b]) @ pn[b] < 0:
                out_folds.append({'polys': [a, b], 'at': [round(float(c), 4) for c in (pc[a] + pc[b]) / 2],
                                  'fold_deg': round(float(np.degrees(np.arccos(np.clip(pn[a] @ pn[b], -1, 1)))), 1)})
            else:
                clefts += 1
    degenerate = sorted({int(o) for o, t in zip(O, z['ta']) if t < 1e-8})
    return {'open_edges': int(z['open']), 'nonmanifold_edges': int(z['nonman']), 'degenerate_polygons': degenerate,
            'seen_from_behind': behind, 'outward_folds': sorted(out_folds, key=lambda q: q['at']), 'inward_clefts': clefts}


def main():
    races = [a for a in sys.argv[1:] if not a.startswith('--')]
    js = sys.argv[sys.argv.index('--json') + 1] if '--json' in sys.argv else None
    if js in races:
        races.remove(js)
    if not races:                                     # the game bodies = those tools/preview/export_bases.py exports (not old working copies)
        races = sorted(p.stem for p in (ROOT / 'tools' / 'preview' / 'cache' / 'bases').glob('*.json'))
    blends = [ROOT / 'projects' / f'{r}.blend' for r in races]
    rep = {}
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / 'dump.py'
        script.write_text(DUMP)
        for blend in blends:
            npz = Path(tmp) / f'{blend.stem}.npz'
            subprocess.run([BLENDER, '-b', str(blend), '--factory-startup', '-noaudio', '-P', str(script), '--', str(npz)],
                           capture_output=True, text=True)
            if not npz.exists():
                continue
            r = scan(npz)
            rep[blend.stem] = r
            bad = r['open_edges'] or r['nonmanifold_edges'] or r['degenerate_polygons'] or r['seen_from_behind']
            print(f"{blend.stem:16s} {'PROBLEMS' if bad else 'clean   '}  open {r['open_edges']}  non-manifold {r['nonmanifold_edges']}  "
                  f"degenerate {len(r['degenerate_polygons'])}  seen from behind {len(r['seen_from_behind'])}  "
                  f"outward folds {len(r['outward_folds'])}  inward clefts {r['inward_clefts']}")
            for q in r['seen_from_behind'][:6]:
                print('    seen from behind', q)
            for q in r['outward_folds']:
                print('    outward fold (look: real feature or flap?)', q)
    if js:
        Path(js).write_text(json.dumps(rep, indent=1))


if __name__ == '__main__':
    main()
