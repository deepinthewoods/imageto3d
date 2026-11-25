"""validate_layers.py [n per race] [seed] [races,]: random mixes of pieces per race at Amount 100 (fill morph): open edges AT the rings (must be 0), true
poke-through (skin in front with garment within 40 mm behind it), and back faces seen (inside showing), as % of visible armour / garment pixels.

Reads the prototype's flat layout (experiments/shell_slots_20261122/out/layers/index.json + <race>/<slot>__<set>.glb, read-only). The skin mask comes from the
GLB's material names ('role:prov', glbread2); the seam rings are the ones layer generation uses (layers.get_body -> slots -> out_body -> rings_of, i.e. the
output body with the part fixes and joint-plane corrections). The rows go to <system temp>/validate_layers_rows.json."""
import json
import random
import sys
import tempfile
from pathlib import Path

import numpy as np

SP = __import__('os').path.dirname(__import__('os').path.abspath(__file__))
sys.path.insert(0, SP)
sys.path.insert(0, 'F:/godot/blender/characters/imageto3d/tools/layers')
import glbread2  # noqa: E402
import geodiv as gd  # noqa: E402
import layers as LY  # noqa: E402
import visibility as vis  # noqa: E402

P = Path('F:/godot/blender/characters/imageto3d/experiments/shell_slots_20261122/out/layers')
RACES = [r for r in (sys.argv[3].split(',') if len(sys.argv) > 3 else []) if r]
NPER = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 3
random.seed(int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 1)
idx = json.loads((P / 'index.json').read_text())['pieces']
px = .004


def assemble(race, sel):
    Vs, Fs, Ss, off = [], [], [], 0
    for slot, s in sel:
        V, F, mats = glbread2.read(P / race / f'{slot}__{s}.glb', 'fill')
        sk = glbread2.skin_mask(mats)
        Vs.append(V)
        Fs.append(F + off)
        Ss.append(sk)
        off += len(V)
    V, F, sk = np.concatenate(Vs), np.concatenate(Fs), np.concatenate(Ss)
    q = np.round(V * 1e6).astype(np.int64)                         # weld (GLB floats are float32)
    _, first, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    return V[first], inv.ravel()[F], sk, q[first]


def depth(img, lo, frame, V, F, N, fsel):
    dd, r, u = frame
    ii, jj = np.nonzero(img >= 0)
    f = fsel[img[ii, jj]]
    p2 = np.c_[lo[0] + (jj + .5) * px, lo[1] + (ii + .5) * px]
    P0 = p2[:, :1] * r + p2[:, 1:] * u
    n = N[f]
    den = n @ dd
    t = ((V[F[f, 0]] - P0) * n).sum(1) / np.where(np.abs(den) > 1e-9, den, 1e-9)
    D = np.full(img.shape, np.inf)
    D[ii, jj] = t
    return D


def measure(V, F, sk):
    N = vis.face_normals(V, F)
    g = np.flatnonzero(~sk)
    tot = poke = back = 0
    for el in (-30, 0, 30):
        for az in range(0, 360, 45):
            a, e = np.radians(az), np.radians(el)
            d = -np.array([np.sin(a) * np.cos(e), -np.cos(a) * np.cos(e), np.sin(e)])
            img, lo, fr = vis.zbuffer(V, F, d, px)
            hi = lo + px * np.array(img.shape[::-1])
            imgg, log_, frg = vis.zbuffer(V, F[g], d, px, lo=lo, hi=hi)
            h, w = min(img.shape[0], imgg.shape[0]), min(img.shape[1], imgg.shape[1])
            img, imgg = img[:h, :w], imgg[:h, :w]
            Da = depth(img, lo, fr, V, F, N, np.arange(len(F)))
            Dg = depth(imgg, log_, frg, V, F, N, g)
            fs = img >= 0
            f = np.where(fs, img, 0)
            front_skin = fs & sk[f]
            behind = Dg - Da
            pk = front_skin & np.isfinite(Dg) & (behind > 0) & (behind < .04)
            facing_away = fs & ((N[f] @ fr[0]) > 0)                     # back face seen
            gar = fs & ~sk[f]
            tot += int(gar.sum() + pk.sum())
            poke += int(pk.sum())
            back += int((facing_away & gar).sum())
    return poke / max(tot, 1) * 100, back / max(tot, 1) * 100


def main():
    rows = []
    for race in (RACES or sorted({p['race'] for p in idx})):
        body = LY.get_body(race, gd)                                  # as layer generation: the race's game body with its part fixes, ...
        S = LY.slots(body, gd)
        ob = LY.out_body(body, gd)                                    # ... the output mesh the seams are cut on, ...
        ringq = {tuple(x) for k in S if k != 'outfit' for R in LY.rings_of(ob, S[k], gd) for x in np.round(R['P'] * 1e6).astype(np.int64)}   # ... and its rings
        have = lambda slot: sorted({p['set'] for p in idx if p['race'] == race and p['slot'] == slot and p['set'] != 'bare'})  # noqa: E731
        combos = []
        for _ in range(NPER):
            c = [('head', 'bare'), ('torso', random.choice(have('torso')))]
            for limb in ('lower_arm', 'lower_leg'):
                if f'{limb}.r' in S:
                    sset = random.choice(have(f'{limb}.r') or ['bare'])
                    c += [(f'{limb}.r', sset), (f'{limb}.l', sset)]
            combos.append(('armour', c))
        for _ in range(max(1, NPER // 2)):
            o = random.choice(have('outfit'))
            c = [('head', 'bare'), ('outfit', o)]
            combos.append(('outfit', c))
        for kind, c in combos:
            V, F, sk, q = assemble(race, c)
            E = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), 1)
            ue, cnt = np.unique(E, axis=0, return_counts=True)
            o = ue[cnt == 1]
            onr = np.array([tuple(x) in ringq for x in q])
            L = np.linalg.norm(V[o[:, 0]] - V[o[:, 1]], axis=1)
            ring_open = float(L[onr[o[:, 0]] & onr[o[:, 1]]].sum())
            poke, back = measure(V, F, sk)
            name = ' + '.join(s[:14] for k, s in c if s != 'bare' and not k.endswith('.l'))
            rows.append((race, kind, ring_open, poke, back, name, c))
            print(f'{race:16s} {kind:6s} ring open {ring_open:.3f} m | poke-through {poke:5.2f} % | inside showing {back:5.2f} % | {name}', flush=True)
    with open(Path(tempfile.gettempdir()) / 'validate_layers_rows.json', 'w', encoding='utf-8') as fh:
        json.dump([(x[0], x[1], x[3], x[4], x[6]) for x in rows], fh)
    r = np.array([(x[2], x[3], x[4]) for x in rows])
    print(f'\n{len(rows)} mixes: ring open total {r[:, 0].sum():.3f} m; poke-through median {np.median(r[:, 1]):.2f} %, max {r[:, 1].max():.2f} %; '
          f'inside showing median {np.median(r[:, 2]):.2f} %, max {r[:, 2].max():.2f} %')


if __name__ == '__main__':
    main()
