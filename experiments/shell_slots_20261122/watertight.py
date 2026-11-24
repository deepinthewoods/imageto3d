"""Watertightness scan of shell / slot results (2026-11-24, user: "scan them for watertightness ... where the different meshes are stitched
together, like the thighs, the knees, where the outfits join with the skin").

    python experiments/shell_slots_20261122/watertight.py [name-filter] [--json out.json]

Works on the mesh topology, not on renders. The result GLB is welded (positions equal to 1e-6 m), then every open edge (used by one triangle) and
every non-manifold edge (used by three or more) is found. Each open edge is classified by the distance from it to the nearest OTHER open edge that
shares no vertex with it (exact segment-to-segment distance):
  t-junction  < 0.05 mm  the two sides touch but share no vertices (a vertex was moved onto the other side's edge, nothing was split)
  crack       < 3 mm     a seam that was meant to close and did not
  gap         < 15 mm    a visible gap along a join
  free        otherwise  a free edge (a hem, the tucked edge of a lip, an opening with nothing beside it)
and by the kinds of face on both sides (role:prov of the face that owns it and of the face that owns its partner edge)."""
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy import sparse
from scipy.sparse import csgraph
from scipy.spatial import cKDTree

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / 'tools' / 'tripo_pipeline'))
import body_fit as bf  # noqa: E402

SAME_LOOP_MM = 40.0   # an open edge further along the same opening than this (walking along the open edges) can be a partner (a slit);
                      # nearer ones are the same edge continuing (the cut has many short segments)
CLASSES = (('t-junction', 0.00005), ('crack', 0.003), ('gap', 0.015), ('free', np.inf))


def load_glb(path):
    """Welded positions, faces and per-face 'role:prov' material names of a result GLB (no morphs)."""
    g = bf.Glb(str(path))
    mats = [m.get('name', '') for m in g.js.get('materials', [])]
    V, F, R, off = [], [], [], 0
    for k in range(len(g.prims)):
        P = bf.gl_to_bl(g.positions(k))
        T = g.triangles(k)
        V.append(P)
        F.append(T + off)
        R += [mats[g.prims[k]['def'].get('material', 0)]] * len(T)
        off += len(P)
    V, F = np.concatenate(V), np.concatenate(F)
    q = np.round(V / 1e-6).astype(np.int64)
    _, first, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    F = inv.ravel()[F]
    keep = (F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 2] != F[:, 0])
    return V[first], F[keep], np.array(R)[keep]


def edges(F):
    E = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    fi = np.tile(np.arange(len(F)), 3)
    k = np.sort(E, 1)
    _, inv, cnt = np.unique(k, axis=0, return_inverse=True, return_counts=True)
    inv = inv.ravel()
    return E, fi, cnt[inv]


def seg_seg_dist(P0, P1, Q0, Q1):
    """Distance between segments P0P1 and Q0Q1 (row-wise; the minimum over a dense set of points of each against the other, exact for the
    endpoint cases and within 1/64 of an edge length otherwise)."""
    def pt_seg(P, A, B):
        ab = B - A
        t = np.clip(((P - A) * ab).sum(-1) / np.maximum((ab * ab).sum(-1), 1e-30), 0, 1)
        return np.linalg.norm(P - (A + ab * t[..., None]), axis=-1)
    ts = np.linspace(0, 1, 65)[None, :, None]
    Ps = P0[:, None] + (P1 - P0)[:, None] * ts
    Qs = Q0[:, None] + (Q1 - Q0)[:, None] * ts
    a = pt_seg(Ps, Q0[:, None], Q1[:, None]).min(1)
    b = pt_seg(Qs, P0[:, None], P1[:, None]).min(1)
    return np.minimum(a, b)


def scan(V, F, R, reach=0.03):
    E, fi, cnt = edges(F)
    nonman = np.unique(np.sort(E[cnt > 2], 1), axis=0)
    ob = cnt == 1
    Eo, fo = E[ob], fi[ob]
    n = len(Eo)
    L = np.linalg.norm(V[Eo[:, 0]] - V[Eo[:, 1]], axis=1)
    best = np.full(n, np.inf)
    partner = np.full(n, -1)
    if n:
        mid = V[Eo].mean(1)
        tree = cKDTree(mid)
        pairs = tree.query_pairs(reach + L.max(), output_type='ndarray')
        if len(pairs):
            i, j = pairs[:, 0], pairs[:, 1]
            share = (Eo[i, 0, None] == Eo[j]).any(1) | (Eo[i, 1, None] == Eo[j]).any(1)
            i, j = i[~share], j[~share]
            # walking distance along the open edges between the two edges: a partner must be another opening or far along this one
            bvt, loc = np.unique(Eo.ravel(), return_inverse=True)
            loc = loc.reshape(-1, 2)
            G = sparse.coo_matrix((np.r_[L, L], (np.r_[loc[:, 0], loc[:, 1]], np.r_[loc[:, 1], loc[:, 0]])), shape=(len(bvt), len(bvt))).tocsr()
            src = np.unique(loc[i, 0])
            far = np.ones(len(i), bool)
            if len(src):
                Dg = csgraph.dijkstra(G, directed=False, indices=src, limit=SAME_LOOP_MM / 1000)
                row = np.searchsorted(src, loc[i, 0])
                walk = np.minimum(Dg[row, loc[j, 0]], Dg[row, loc[j, 1]])
                far = ~np.isfinite(walk)
            i, j = i[far], j[far]
            d = np.concatenate([seg_seg_dist(V[Eo[i[s:s + 20000], 0]], V[Eo[i[s:s + 20000], 1]], V[Eo[j[s:s + 20000], 0]], V[Eo[j[s:s + 20000], 1]])
                                for s in range(0, len(i), 20000)]) if len(i) else np.zeros(0)
            for a, b in ((i, j), (j, i)):
                o = np.lexsort((d, a))
                a2, b2, d2 = a[o], b[o], d[o]
                first = np.r_[True, a2[1:] != a2[:-1]] if len(a2) else np.zeros(0, bool)
                upd = d2[first] < best[a2[first]]
                best[a2[first][upd]] = d2[first][upd]
                partner[a2[first][upd]] = b2[first][upd]
    cls = np.full(n, 'free', object)
    for name, lim in reversed(CLASSES):
        cls[best < lim] = name
    own = R[fo]
    other = np.where(partner >= 0, R[fo[np.maximum(partner, 0)]], '-')
    return {'E': Eo, 'face': fo, 'len': L, 'dist': best, 'partner': partner, 'cls': cls, 'own': own, 'other': other, 'nonmanifold': nonman}


def kind(rp):
    r, _, p = rp.partition(':')
    return f'{"skin" if r == "skin" else "garment"}:{p}' if rp != '-' else '-'


def summary(s):
    out = {'open_edges': int(len(s['E'])), 'open_m': round(float(s['len'].sum()), 3), 'nonmanifold_edges': int(len(s['nonmanifold']))}
    for c, _ in CLASSES:
        m = s['cls'] == c
        out[c] = {'edges': int(m.sum()), 'm': round(float(s['len'][m].sum()), 3)}
    by = defaultdict(float)
    for c, a, b, l in zip(s['cls'], s['own'], s['other'], s['len']):
        by[f'{c:10s}  {" | ".join(sorted([kind(a), kind(b)])) if c != "free" else kind(a)}'] += l
    out['seams'] = {k: round(v, 3) for k, v in sorted(by.items(), key=lambda x: -x[1])}
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--') and not a.endswith('.json')]
    idx = json.loads((HERE / 'out' / 'index.json').read_text())['results']
    names = [r['name'] for r in idx if not args or args[0] in r['name']]
    js = sys.argv[sys.argv.index('--json') + 1] if '--json' in sys.argv else None
    res = {}
    for nm in names:
        V, F, R = load_glb(HERE / 'out' / f'{nm}.glb')
        sm = summary(scan(V, F, R))
        res[nm] = sm
        print(f'{nm[:66]:66s} open {sm["open_m"]:6.2f} m | t-junction {sm["t-junction"]["m"]:5.2f} crack {sm["crack"]["m"]:5.2f} gap {sm["gap"]["m"]:5.2f} '
              f'free {sm["free"]["m"]:5.2f} m | non-manifold {sm["nonmanifold_edges"]}', flush=True)
        if len(names) <= 3:
            for k, v in list(sm['seams'].items())[:16]:
                print(f'      {v:6.3f} m  {k}')
    if js:
        Path(js).write_text(json.dumps(res, indent=1))


if __name__ == '__main__':
    main()
