"""Samplers: farthest-point scatter inside a mask and equal-arc-length rows traced on the surface.

All samplers return `(face, bary)` pairs on a surface (`mesh.make_surface` / `sub_surface`), so
positions, normals and weights are read back exactly at the sampled points.
"""
import numpy as np

from . import curves, geodesic, mesh


def vertex_face(surf):
    """A face incident to each vertex and the corner index of the vertex in it."""
    F = surf['F']
    face = -np.ones(len(surf['V']), np.int64)
    corner = np.zeros(len(surf['V']), np.int64)
    for k in range(3):
        face[F[::-1, k]] = np.arange(len(F))[::-1]
        corner[F[::-1, k]] = k
    return face, corner


def vertex_bary(surf, verts):
    """(face, bary) of surface vertices."""
    vf, vc = vertex_face(surf)
    verts = np.asarray(verts, np.int64)
    b = np.zeros((len(verts), 3))
    b[np.arange(len(verts)), vc[verts]] = 1.0
    return vf[verts], b


def at(surf, face, bary):
    """Position and smoothed unit normal at surface points."""
    P = mesh.bary_eval(surf, surf['V'], face, bary)
    return P, mesh.unit(mesh.bary_eval(surf, surf['N'], face, bary))


def mirror_map(V, x0=0.0, axis=0, tol=None):
    """Vertex -> vertex across the plane (nearest to the reflected position within `tol`, else -1).
    Exact partners are found by hashing, the rest by a brute-force search."""
    tol = max(tol if tol is not None else 1e-6, 1e-9)
    R = mesh.reflect(V, x0, axis)
    cell = lambda P: map(tuple, np.round(P / tol).astype(np.int64))       # noqa: E731
    table = {}
    for i, k in enumerate(cell(V)):
        table.setdefault(k, i)
    out = np.array([table.get(k, -1) for k in cell(R)], np.int64)
    for s in np.array_split(np.flatnonzero(out < 0), max(len(np.flatnonzero(out < 0)) // 256, 1)):
        if not len(s):
            continue
        d = ((R[s, None, :] - V[None]) ** 2).sum(-1)
        j = d.argmin(1)
        ok = d[np.arange(len(j)), j] <= tol * tol
        out[s[ok]] = j[ok]
    return out


def scatter(surf, cand, count, spacing=0.0, x0=0.0, symmetric=False, axis=0, seed=0, trials=6):
    """Farthest-point scatter over candidate vertices, the best of `trials` random starts (lowest
    coefficient of variation of the nearest-neighbour surface distance). With `symmetric`, only the
    x >= x0 half is searched and every pick off the mid-plane stands for itself and its mirror image
    (`count` is the total). Returns (vertices, paired flags); build the mirror images with `_mirror`."""
    V = surf['V']
    cand = np.asarray(cand, bool)
    mirror = gap = plane = None
    if symmetric:
        mirror = mirror_map(V, x0, axis, .3 * surf['edge'])
        plane = np.abs(V[:, axis] - x0) <= 1e-9 + 1e-4 * surf['edge']
        mirror[plane] = np.flatnonzero(plane)
        cand = cand & (V[:, axis] >= x0 - 1e-9) & (mirror >= 0)
        gap = np.where(plane, np.inf, 2 * np.abs(V[:, axis] - x0))
    best, best_cv = ([], []), np.inf
    for t in range(max(trials, 1)):
        v, paired = geodesic.farthest_points(surf['g'], cand, count, spacing, mirror=mirror, symmetric_ok=plane,
                                             gap=gap, rng=np.random.default_rng(seed + 7919 * t))
        if len(v) < 3:
            return v, paired
        allv = np.array(v + [int(mirror[x]) for x, p in zip(v, paired) if p])
        d = geodesic.nn_distances(surf['g'], allv, start=2 * surf['edge'])
        d = d[np.isfinite(d)]
        cv = d.std() / max(d.mean(), 1e-12) if len(d) > 2 else np.inf
        if cv < best_cv:
            best, best_cv = (v, paired), cv
        if cv < .08:
            break
    return best


def best_row(surf, chains, purity, thr, step, key_dir=None, min_len=0.0):
    """The chain/sub-interval with the longest run of points whose `purity` >= thr.

    Returns (chain, s0, s1) with the arc positions of the run ends, or None. `key_dir` (3,) orients
    the chain so that it advances along that direction."""
    best = None
    for ch in chains:
        L = curves.length(ch)
        if L < max(min_len, step):
            continue
        if key_dir is not None and np.dot(ch['P1'][-1] - ch['P0'][0], key_dir) < 0:
            ch = curves.reverse(ch)
        s, P, f, b = curves.dense(ch, step)
        ok = np.ones(len(s), bool)
        if thr > 0:
            ok = mesh.bary_eval(surf, purity, f, b) >= thr
        run = curves.longest_run(ok)
        if run is None:
            continue
        i0, i1 = run
        a, z = s[i0], s[i1 - 1]
        if best is None or z - a > best[2] - best[1]:
            best = (ch, a, z)
    return best


def adaptive_row(surf, chains, thr, floor, step, key_dir=None, min_len=0.0, frac=0.4):
    """`best_row`, relaxing the joint purity threshold from `thr` down to `floor` (steps of 0.05)
    until the run covers `frac` of the longest candidate chain, so that a row is not squeezed into a
    sliver where the weights are blended along the whole limb. Returns (best or None, threshold used)."""
    if thr <= 0 or not chains:
        return best_row(surf, chains, surf['attrs']['pur'], 0.0, step, key_dir, min_len), 0.0
    need = frac * max(curves.length(c) for c in chains)
    best, t = None, thr
    while True:
        b = best_row(surf, chains, surf['attrs']['pur'], t, step, key_dir, min_len)
        if b is not None and b[2] - b[1] >= need:
            return b, t
        if b is not None and (best is None or b[2] - b[1] > best[0][2] - best[0][1]):
            best = (b, t)
        if t <= floor + 1e-9:
            break
        t = max(floor, t - .05)
    return best if best else (None, thr)


def adaptive_candidates(pur, thr, floor, need=.35):
    """Vertices usable for scatter: purity >= the highest threshold in [floor, thr] that keeps
    `need` of the vertices. Returns (bool mask, threshold used)."""
    if thr <= 0:
        return np.ones(len(pur), bool), 0.0
    t = thr
    while True:
        ok = pur >= t
        if ok.mean() >= need or t <= floor + 1e-9:
            return ok, t
        t = max(floor, t - .05)


def rows_on_chain(surf, best, n, inclusive, inset=0.0):
    """`n` equally spaced points on the chosen run: (face, bary, arc positions)."""
    ch, a, z = best
    a, z = a + inset, z - inset
    if z <= a:
        return None
    s, P, f, b = curves.resample(ch, a, z, n, inclusive)
    return f, b, s


def signed_midline(surf, lat):
    """Signed surface distance to the mid-plane lat = 0 (positive on the left). The sources are
    the endpoints of every edge that crosses the plane, started at their plane distance."""
    g = surf['g']
    src, dst = g['src'], g['idx']
    cross = (lat[src] * lat[dst] <= 0)
    srcs = np.unique(np.concatenate([src[cross], dst[cross]]))
    d = geodesic.dijkstra(g, srcs, init=np.abs(lat[srcs]))
    d = np.where(np.isfinite(d), d, 1e3)
    return np.where(lat >= 0, d, -d)
