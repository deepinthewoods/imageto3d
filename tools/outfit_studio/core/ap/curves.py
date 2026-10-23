"""Curves traced on a triangle surface: iso-lines of a vertex field, clipping, arc-length sampling.

A *chain* is a run of segments that each lie inside one face:
`{'P0','P1' (S,3), 'face' (S,), 'B0','B1' (S,3) barycentric ends}` with `P1[i] == P0[i+1]`.
Because every point keeps its (face, barycentric) pair, attributes (weights, normals,
fields) are read exactly at any point of the curve.
"""
import numpy as np

from . import mesh


def isolines(V, F, f, level=0.0, face_ok=None):
    """Chains of the level set f = level, restricted to faces where `face_ok`."""
    f = np.asarray(f, float) - level
    s = f >= 0
    ids = s[F]
    mixed = ids.any(1) & ~ids.all(1)
    if face_ok is not None:
        mixed &= face_ok
    fs = np.flatnonzero(mixed)
    if not len(fs):
        return []
    _, fe = mesh.edge_table(F)
    nodes, seg = {}, []                                  # edge id -> (a, b, t); (node, node, face)
    for face in fs:
        vs = F[face]
        pair = []
        for k in range(3):
            a, b = int(vs[k]), int(vs[(k + 1) % 3])
            if s[a] == s[b]:
                continue
            key = int(fe[face, k])
            if key not in nodes:
                nodes[key] = (a, b, f[a] / (f[a] - f[b]))
            pair.append(key)
        if len(pair) == 2:
            seg.append((pair[0], pair[1], int(face)))
    if not seg:
        return []
    by_node = {}
    for i, (p, q, _) in enumerate(seg):
        by_node.setdefault(p, []).append(i)
        by_node.setdefault(q, []).append(i)
    used = np.zeros(len(seg), bool)

    def walk(cur, node):
        out = []
        while True:
            used[cur] = True
            p, q, _ = seg[cur]
            if p != node:
                p, q = q, p
            out.append((cur, p, q))
            node = q
            nxt = [j for j in by_node[node] if not used[j]]
            if not nxt:
                return out
            cur = nxt[0]

    chains = []
    for n, l in by_node.items():                         # open chains start at a lone end
        if len(l) == 1 and not used[l[0]]:
            chains.append(walk(l[0], n))
    for i in range(len(seg)):                            # what is left are loops
        if not used[i]:
            chains.append(walk(i, seg[i][0]))
    return [_build(c, seg, nodes, V, F) for c in chains]


def _build(chain, seg, nodes, V, F):
    P0, P1, face, B0, B1 = [], [], [], [], []
    for cur, ka, kb in chain:
        fc = seg[cur][2]
        out = []
        for key in (ka, kb):
            va, vb, t = nodes[key]
            bar = np.zeros(3)
            bar[int(np.flatnonzero(F[fc] == va)[0])] = 1 - t
            bar[int(np.flatnonzero(F[fc] == vb)[0])] = t
            out.append((V[va] * (1 - t) + V[vb] * t, bar))
        P0.append(out[0][0])
        P1.append(out[1][0])
        B0.append(out[0][1])
        B1.append(out[1][1])
        face.append(fc)
    return {'P0': np.array(P0), 'P1': np.array(P1), 'face': np.array(face, np.int64),
            'B0': np.array(B0), 'B1': np.array(B1)}


def seg_lengths(ch):
    return np.linalg.norm(ch['P1'] - ch['P0'], axis=1)


def length(ch):
    return float(seg_lengths(ch).sum())


def reverse(ch):
    return {'P0': ch['P1'][::-1], 'P1': ch['P0'][::-1], 'face': ch['face'][::-1],
            'B0': ch['B1'][::-1], 'B1': ch['B0'][::-1]}


def field_at_ends(F, ch, vf):
    """A vertex field evaluated at both ends of every segment."""
    tri = np.asarray(vf)[F[ch['face']]]
    return (tri * ch['B0']).sum(1), (tri * ch['B1']).sum(1)


def clip(F, ch, vf, thr=0.0):
    """Sub-chains of `ch` where the vertex field `vf` >= thr (linear inside each segment)."""
    g0, g1 = field_at_ends(F, ch, vf)
    g0, g1 = g0 - thr, g1 - thr
    S = len(g0)
    keep = (g0 >= 0) | (g1 >= 0)
    d = g1 - g0
    safe = np.where(np.abs(d) < 1e-15, 1.0, d)
    tc = np.clip(-g0 / safe, 0, 1)
    lo = np.where(g0 >= 0, 0.0, np.where(g1 >= 0, tc, 1.0))
    hi = np.where(g1 >= 0, 1.0, np.where(g0 >= 0, tc, 0.0))
    runs, cur = [], []
    for i in range(S):
        if not keep[i] or hi[i] - lo[i] <= 1e-9:
            if cur:
                runs.append(cur)
                cur = []
            continue
        if cur and lo[i] > 1e-9:
            runs.append(cur)
            cur = []
        cur.append(i)
        if hi[i] < 1 - 1e-9:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    closed = np.linalg.norm(ch['P1'][-1] - ch['P0'][0]) < 1e-9
    if closed and len(runs) > 1 and runs[0][0] == 0 and runs[-1][-1] == S - 1 and lo[0] <= 1e-9 \
            and hi[-1] >= 1 - 1e-9:
        runs[0] = runs.pop() + runs[0]                    # a loop's run wraps around its seam
    out = []
    for r in runs:
        i = np.array(r)
        l, h = lo[i][:, None], hi[i][:, None]
        out.append({'P0': ch['P0'][i] + (ch['P1'][i] - ch['P0'][i]) * l,
                    'P1': ch['P0'][i] + (ch['P1'][i] - ch['P0'][i]) * h,
                    'face': ch['face'][i],
                    'B0': ch['B0'][i] + (ch['B1'][i] - ch['B0'][i]) * l,
                    'B1': ch['B0'][i] + (ch['B1'][i] - ch['B0'][i]) * h})
    return out


def sample_at(ch, s):
    """Points at arc lengths `s`: (positions, faces, barycentrics)."""
    L = seg_lengths(ch)
    cum = np.concatenate([[0], np.cumsum(L)])
    s = np.clip(np.asarray(s, float), 0, cum[-1])
    j = np.clip(np.searchsorted(cum, s, side='right') - 1, 0, len(L) - 1)
    t = ((s - cum[j]) / np.maximum(L[j], 1e-15))[:, None]
    return (ch['P0'][j] + (ch['P1'][j] - ch['P0'][j]) * t, ch['face'][j],
            ch['B0'][j] + (ch['B1'][j] - ch['B0'][j]) * t)


def dense(ch, step):
    """Samples every ~`step` along the chain: (arc positions, points, faces, barycentrics)."""
    total = length(ch)
    s = np.linspace(0, total, max(int(np.ceil(total / step)) + 1, 2))
    return (s,) + sample_at(ch, s)


def longest_run(ok):
    """(start, stop) of the longest run of True in a bool array (stop exclusive), or None."""
    ok = np.asarray(ok, bool)
    if not ok.any():
        return None
    d = np.diff(np.concatenate([[0], ok.astype(int), [0]]))
    a, b = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    k = (b - a).argmax()
    return int(a[k]), int(b[k])


def resample(ch, s0, s1, n, inclusive):
    """`n` points at equal arc length between s0 and s1: ends included, or the centres of
    n equal bins. Returns (arc positions, positions, faces, barycentrics)."""
    if n <= 0:
        return (np.zeros(0),) + sample_at(ch, np.zeros(0))
    if inclusive:
        s = np.linspace(s0, s1, n) if n > 1 else np.array([(s0 + s1) / 2])
    else:
        s = s0 + (s1 - s0) * (np.arange(n) + .5) / n
    return (s,) + sample_at(ch, s)
