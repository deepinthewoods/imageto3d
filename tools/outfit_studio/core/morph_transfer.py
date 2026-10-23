"""Closest points on a triangle mesh and barycentric transfer of per-vertex deltas (SPEC section 3, morph targets). Pure numpy."""
import numpy as np

MIN_DELTA = 1e-5
_BUDGET = 150000       # (points x triangles) per chunk


def _closest_chunk(p, a, b, c):
    """Barycentric closest point of every p (n, 3) on every triangle (m, 3 x3): Ericson, vectorised. -> (n, m, 3) weights."""
    p = p[:, None]
    ab, ac = b - a, c - a
    ap = p - a
    d1, d2 = (ab * ap).sum(-1), (ac * ap).sum(-1)
    bp = p - b
    d3, d4 = (ab * bp).sum(-1), (ac * bp).sum(-1)
    cp = p - c
    d5, d6 = (ab * cp).sum(-1), (ac * cp).sum(-1)
    va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
    with np.errstate(all='ignore'):
        denom = 1. / (va + vb + vc)
        v_in, w_in = vb * denom, vc * denom
        t_ab = d1 / (d1 - d3)
        t_ac = d2 / (d2 - d6)
        t_bc = (d4 - d3) / ((d4 - d3) + (d5 - d6))
    with np.errstate(all='ignore'):
        u = np.stack([1 - v_in - w_in, v_in, w_in], -1)
    bad = ~np.isfinite(u).all(-1)                       # degenerate triangle: fall back to vertex a
    u[bad] = (1, 0, 0)

    def put(mask, vals):
        u[mask] = np.stack(vals, -1)[mask]

    zero, one = np.zeros_like(d1), np.ones_like(d1)
    # later writes win = earlier checks of the reference algorithm
    put((va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0), (zero, 1 - t_bc, t_bc))
    put((vb <= 0) & (d2 >= 0) & (d6 <= 0), (1 - t_ac, zero, t_ac))
    put((d6 >= 0) & (d5 <= d6), (zero, zero, one))
    put((vc <= 0) & (d1 >= 0) & (d3 <= 0), (1 - t_ab, t_ab, zero))
    put((d3 >= 0) & (d4 <= d3), (zero, one, zero))
    put((d1 <= 0) & (d2 <= 0), (one, zero, zero))
    return u


def closest_points(P, V, F):
    """Closest point of every row of P (n, 3) on the triangles F (m, 3) of V. -> (tri (n,), bary (n, 3), dist (n,))."""
    P, V, F = np.asarray(P, np.float64), np.asarray(V, np.float64), np.asarray(F, np.int64)
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    n, m = len(P), len(F)
    tri, bary, dist = np.zeros(n, np.int64), np.zeros((n, 3)), np.zeros(n)
    step = max(1, _BUDGET // max(m, 1))
    for i in range(0, n, step):
        p = P[i:i + step]
        u = _closest_chunk(p, a, b, c)
        q = u[..., 0:1] * a + u[..., 1:2] * b + u[..., 2:3] * c
        d = np.linalg.norm(q - p[:, None], axis=-1)
        k = d.argmin(1)
        rows = np.arange(len(p))
        tri[i:i + step], bary[i:i + step], dist[i:i + step] = k, u[rows, k], d[rows, k]
    return tri, bary, dist


def barycentric_delta(tri, bary, F, delta):
    """Delta of every item vertex = barycentric blend of the per-vertex `delta` (nv, 3) of its closest triangle."""
    return np.einsum('nj,njk->nk', bary, np.asarray(delta, np.float64)[np.asarray(F, np.int64)[tri]])


def transfer_deltas(tri, bary, F, deltas):
    """{key: (nv_base, 3) delta} -> {key: (n, 3)} for the keys whose max |delta| exceeds MIN_DELTA."""
    out = {}
    for name, d in deltas.items():
        t = barycentric_delta(tri, bary, F, d)
        if np.abs(t).max(initial=0.) > MIN_DELTA:
            out[name] = t
    return out
