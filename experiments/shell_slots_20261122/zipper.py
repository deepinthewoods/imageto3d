"""zip_seams (2026-11-24, user: "scan them for watertightness ... where the different meshes are stitched together, like the thighs, the knees,
where the outfits join with the skin").

The watertightness scan (watertight.py) found no result closed: along the joins two open edges run side by side, 0-15 mm apart (a lip that ends
beside the skin edge instead of on it, skin of one module beside skin of another, a garment edge beside skin that was cut back a little further).
zip_seams bridges such pairs with strips of triangles between their EXISTING vertices: nothing moves, so the shape and the morph targets stay as
they were; only triangles are added. A strip is left out where one of its bridging edges would pass through another triangle of the mesh.
"""
import numpy as np
from scipy import sparse
from scipy.sparse import csgraph
from scipy.spatial import cKDTree

ZIP_GAP_MM = 15.0          # open edges up to this far apart are bridged
ZIP_SAME_LOOP_MM = 40.0    # a partner on the same opening must be at least this far along it (a slit); nearer is the same edge continuing
ZIP_MIN_RUN = 2            # bridged runs have at least this many vertices on the first side
ZIP_PASSES = 3


class _M:
    def __init__(self, V, F):
        self.V, self.F = V, F


def _pt_seg(P, A, B):
    ab = B - A
    t = np.clip(((P - A) * ab).sum(-1) / np.maximum((ab * ab).sum(-1), 1e-30), 0., 1.)
    return np.linalg.norm(P - (A + ab * t[..., None]), axis=-1), t


def _true_runs(flags):
    """Runs of consecutive True in a cyclic bool array (index arrays), and whether the whole loop is True."""
    flags = np.asarray(flags, bool)
    n = len(flags)
    if flags.all():
        return [np.arange(n)], True
    if not flags.any():
        return [], False
    start = int(np.flatnonzero(~flags)[0])
    order = (np.arange(n) + start) % n
    runs, cur = [], []
    for i in order:
        if flags[i]:
            cur.append(i)
        elif cur:
            runs.append(np.array(cur))
            cur = []
    if cur:
        runs.append(np.array(cur))
    return runs, False


def _pav(y):
    """Closest non-decreasing sequence (pool adjacent violators)."""
    vals, wts, cnt = [], [], []
    for v in y:
        vals.append(float(v)); wts.append(1.); cnt.append(1)
        while len(vals) > 1 and vals[-2] > vals[-1]:
            w = wts[-2] + wts[-1]
            vals[-2:] = [(vals[-2] * wts[-2] + vals[-1] * wts[-1]) / w]
            wts[-2:] = [w]
            cnt[-2:] = [cnt[-2] + cnt[-1]]
    out = []
    for v, c in zip(vals, cnt):
        out += [v] * c
    return np.array(out)


def _strip_tris(A, pa, B, pb):
    """Triangles between chain A (whose open edges run A[i] -> A[i+1]) and chain B beside it, listed the same way along the seam (its own open
    edges run B[j+1] -> B[j]); pa / pb non-decreasing positions. Every triangle holds the reverse of the open edge it closes: the winding agrees."""
    tris, i, j = [], 0, 0
    while i < len(A) - 1 or j < len(B) - 1:
        if j >= len(B) - 1 or (i < len(A) - 1 and pa[i + 1] <= pb[j + 1]):
            tris.append((A[i + 1], A[i], B[j]))
            i += 1
        else:
            tris.append((B[j], B[j + 1], A[i]))
            j += 1
    return tris


def _segs_cross_tris(P0, P1, A, B, C, eps=1e-12):
    """(S, T) bool: segment s (ends excluded) passes through the inside of triangle t."""
    D = (P1 - P0)[:, None, :]
    e1, e2 = (B - A)[None], (C - A)[None]
    pv = np.cross(D, e2)
    det = (e1 * pv).sum(-1)
    ok = np.abs(det) > eps
    inv = 1. / np.where(ok, det, 1.)
    tv = P0[:, None, :] - A[None]
    u = (tv * pv).sum(-1) * inv
    qv = np.cross(tv, e1)
    v = (D * qv).sum(-1) * inv
    t = (e2 * qv).sum(-1) * inv
    return ok & (u > 1e-6) & (v > 1e-6) & (u + v < 1 - 1e-6) & (t > 1e-4) & (t < 1 - 1e-4)


def _crossing(seg, V, F, tree, rad):
    """Per bridging edge (x, y): True where it passes through a triangle of F that uses neither x nor y."""
    out = np.zeros(len(seg), bool)
    for s, (x, y) in enumerate(seg):
        mid, half = (V[x] + V[y]) / 2, np.linalg.norm(V[y] - V[x]) / 2
        cand = np.array(tree.query_ball_point(mid, half + rad), np.int64)
        if not len(cand):
            continue
        cand = cand[~np.isin(F[cand], [x, y]).any(1)]
        if len(cand):
            out[s] = _segs_cross_tris(V[[x]], V[[y]], V[F[cand, 0]], V[F[cand, 1]], V[F[cand, 2]]).any()
    return out


def zip_seams(V, F, gap_mm=ZIP_GAP_MM, same_loop_mm=ZIP_SAME_LOOP_MM, passes=ZIP_PASSES, boundary_loops=None):
    """Bridge open edges that run side by side (within gap_mm, opposite ways as on a closed surface) with strips of triangles between their own
    vertices. Returns (new faces (k, 3) int64, notes)."""
    if boundary_loops is None:
        from shell_slots import boundary_loops
    V = np.asarray(V, np.float64)
    F0 = np.asarray(F, np.int64)
    new = []
    notes = {'runs': 0, 'faces': 0, 'refused_crossing': 0, 'refused_same_way_vertices': 0}
    gap = gap_mm / 1000
    for _ in range(passes):
        Fc = np.concatenate([F0, np.array(new, np.int64).reshape(-1, 3)])
        loops = boundary_loops(_M(V, Fc))
        if not loops:
            break
        LA, LB, LL, LK, arc, larc = [], [], [], [], [], []
        for li, lp in enumerate(loops):
            lp = np.asarray(lp, np.int64)
            b = np.roll(lp, -1)
            sl = np.linalg.norm(V[b] - V[lp], axis=1)
            LA.append(lp); LB.append(b); LL.append(np.full(len(lp), li)); LK.append(np.arange(len(lp)))
            arc.append(np.r_[0., np.cumsum(sl)[:-1]])
            larc.append(float(sl.sum()))
        EA, EB, EL, EK, EARC = (np.concatenate(x) for x in (LA, LB, LL, LK, arc))
        elen = np.linalg.norm(V[EB] - V[EA], axis=1)
        bvt, loc = np.unique(np.r_[EA, EB], return_inverse=True)
        la_, lb_ = loc[:len(EA)], loc[len(EA):]
        G = sparse.coo_matrix((np.r_[elen, elen], (np.r_[la_, lb_], np.r_[lb_, la_])), shape=(len(bvt), len(bvt))).tocsr()
        # partner of every loop vertex (start vertex of each open edge): nearest point on an open edge elsewhere
        ts = np.linspace(0, 1, 9)
        S = np.concatenate([V[EA] + (V[EB] - V[EA]) * t for t in ts])
        own = np.tile(np.arange(len(EA)), len(ts))
        tree = cKDTree(S)
        P = V[EA]
        pe = np.full(len(EA), -1)
        pt = np.zeros(len(EA))
        lists = tree.query_ball_point(P, gap + elen.max() / (len(ts) - 1) + 1e-9)
        cand_src = sorted({int(la_[k]) for k, c in enumerate(lists) if c})
        Dg = csgraph.dijkstra(G, directed=False, indices=cand_src, limit=same_loop_mm / 1000) if cand_src else None
        rowof = {s: r for r, s in enumerate(cand_src)}
        for k, cand in enumerate(lists):
            if not cand:
                continue
            cand = np.unique(own[cand])
            cand = cand[(EA[cand] != EA[k]) & (EB[cand] != EA[k])]
            if not len(cand):
                continue
            row = Dg[rowof[int(la_[k])]]
            cand = cand[~(np.isfinite(row[la_[cand]]) | np.isfinite(row[lb_[cand]]))]
            if not len(cand):
                continue
            d, t = _pt_seg(P[k][None], V[EA[cand]], V[EB[cand]])
            j = int(np.argmin(d))
            if d[j] <= gap:
                pe[k], pt[k] = cand[j], t[j]
        prev = np.zeros(len(EA), np.int64)
        for li in range(len(loops)):
            ids = np.flatnonzero(EL == li)
            prev[ids] = np.roll(ids, 1)
        tan = V[EB] - V[EA[prev]]
        has = pe >= 0
        pdir = np.zeros_like(tan)
        pdir[has] = V[EB[pe[has]]] - V[EA[pe[has]]]
        anti = has & ((tan * pdir).sum(1) < 0)
        notes['refused_same_way_vertices'] += int((has & ~anti).sum())
        used = np.zeros(len(EA), bool)
        Fall = Fc
        cent = V[Fall].mean(1)
        rad = float(np.linalg.norm(V[Fall] - cent[:, None], axis=2).max())
        ctree = cKDTree(cent)
        added = 0
        for li in range(len(loops)):
            ids = np.flatnonzero(EL == li)
            runs, _ = _true_runs(anti[ids])
            for run in runs:
                pl = EL[pe[ids[run]]]
                for sub in np.split(run, np.flatnonzero(np.diff(pl)) + 1):
                    if len(sub) < ZIP_MIN_RUN:
                        continue
                    ks = ids[sub]
                    if used[ks[:-1]].any():
                        continue
                    l2 = int(EL[pe[ks[0]]])
                    i2 = np.flatnonzero(EL == l2)
                    n2 = len(i2)
                    e_first, e_last = int(pe[ks[0]]), int(pe[ks[-1]])
                    q0, q1 = int(EK[e_last]), int(EK[e_first])
                    m = ((q1 - q0) % n2) + 1                                 # partner edges: from e_last forward to e_first along loop l2
                    if m >= n2:
                        continue
                    espan = i2[[(q0 + t_) % n2 for t_ in range(m)]]
                    if used[espan].any():
                        continue
                    cum = np.r_[0., np.cumsum(elen[espan])]
                    pos = {int(e): j for j, e in enumerate(espan)}
                    if any(int(pe[k]) not in pos for k in ks):
                        continue                                             # a partner jumps outside the span: not one seam
                    srel = np.array([cum[pos[int(pe[k])]] + pt[k] * elen[pe[k]] for k in ks])
                    srel = -_pav(-srel)                                      # partner positions fall along this run
                    if srel[0] - srel[-1] < 1e-6:
                        continue
                    # chain B: the loop-l2 vertices of the span, listed from the first partner backwards
                    Bc = [int(EB[espan[-1]])] + [int(EA[espan[j]]) for j in reversed(range(m))]
                    relB = np.r_[cum[m], cum[:m][::-1]]
                    A = [int(a) for a in EA[ks]]
                    if len(set(Bc)) < len(Bc) or set(Bc) & set(A):
                        continue
                    ra = np.r_[0., np.cumsum(elen[ks[:-1]])]
                    pb = np.maximum.accumulate(np.interp(-relB, -srel, ra))
                    tris = _strip_tris(A, ra, Bc, pb)
                    if not tris:
                        continue
                    Aset = set(A)
                    seg = np.array([(x, y) for t in tris for x, y in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])) if (x in Aset) != (y in Aset)], np.int64)
                    if len(seg) and _crossing(seg, V, Fall, ctree, rad).any():
                        notes['refused_crossing'] += 1
                        continue
                    new += tris
                    used[ks[:-1]] = True
                    used[espan] = True
                    notes['runs'] += 1
                    added += len(tris)
        if not added:
            break
    notes['faces'] = len(new)
    return np.array(new, np.int64).reshape(-1, 3), notes
