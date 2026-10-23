"""Triangle-mesh utilities for attachment-point placement (pure numpy, no bpy).

A *surface* is a dict built by `make_surface`: a (subdivided) triangle mesh with its
edge graph, smoothed vertex normals, per-vertex attributes interpolated through the
subdivision and per-face attributes inherited from the source faces.
Positions stay exactly on the source surface (midpoint subdivision), so a point
found on the fine mesh is also a point of the original faceted mesh; only the normals
are smoothed.
"""
import numpy as np

EPS = 1e-12


def unit(a):
    n = np.linalg.norm(a, axis=-1, keepdims=True)
    return a / np.maximum(n, EPS)


def triangulate(V, polys):
    """Triangles of polygons with a mirror-invariant choice of diagonals: a quad is split along
    the shorter diagonal (ties broken by the larger canonical key of |x|, y, z), an n-gon is fanned
    from its canonical vertex. A mirrored polygon therefore triangulates into the mirrored
    triangles, which Blender's tessellation does not promise. Returns (F (m,3), polygon of each)."""
    V = np.asarray(V, float)
    key = np.round(np.column_stack([np.abs(V[:, 0]), V[:, 1], V[:, 2]]), 5)
    canon = lambda ids: max(ids, key=lambda i: tuple(key[i]))          # noqa: E731
    tris, owner = [], []
    for k, poly in enumerate(polys):
        p = list(poly)
        if len(p) == 3:
            t = [p]
        elif len(p) == 4:
            d02, d13 = np.linalg.norm(V[p[0]] - V[p[2]]), np.linalg.norm(V[p[1]] - V[p[3]])
            if abs(d02 - d13) < 1e-6 * max(d02, d13):
                use02 = canon([p[0], p[2]]) == canon(p)
            else:
                use02 = d02 < d13
            t = [[p[0], p[1], p[2]], [p[0], p[2], p[3]]] if use02 else [[p[1], p[2], p[3]], [p[1], p[3], p[0]]]
        else:
            c = p.index(canon(p))
            p = p[c:] + p[:c]
            t = [[p[0], p[i], p[i + 1]] for i in range(1, len(p) - 1)]
        tris += t
        owner += [k] * len(t)
    return np.array(tris, np.int64), np.array(owner, np.int64)


def face_cross(V, F):
    return np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])


def face_areas(V, F):
    return np.linalg.norm(face_cross(V, F), axis=1) * .5


def face_normals(V, F):
    return unit(face_cross(V, F))


def vertex_normals(V, F):
    """Angle-weighted vertex normals (what smooth shading shows)."""
    fn = face_normals(V, F)
    out = np.zeros_like(V)
    for k in range(3):
        a, b, c = V[F[:, k]], V[F[:, (k + 1) % 3]], V[F[:, (k + 2) % 3]]
        u, w = unit(b - a), unit(c - a)
        ang = np.arccos(np.clip((u * w).sum(1), -1, 1))
        for d in range(3):
            out[:, d] += np.bincount(F[:, k], fn[:, d] * ang, len(V))
    return unit(out)


def edge_table(F):
    """(edges (E,2) sorted vertex pairs, face_edges (m,3) edge id of edges (0,1),(1,2),(2,0))."""
    e = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1)
    edges, inv = np.unique(e, axis=0, return_inverse=True)
    return edges, inv.reshape(3, -1).T


def graph(V, F, diag=True):
    """Symmetric CSR edge graph with 3D edge lengths. `diag` also joins the two vertices
    opposite each interior edge (halves the metric error of an edge-only Dijkstra)."""
    edges, fe = edge_table(F)
    if diag:
        m = len(F)
        opp = np.concatenate([F[:, 2], F[:, 0], F[:, 1]])        # vertex opposite edge k of face
        eid = fe.T.reshape(-1)
        order = np.argsort(eid, kind='stable')
        e_s, o_s = eid[order], opp[order]
        same = e_s[1:] == e_s[:-1]
        extra = np.stack([o_s[:-1][same], o_s[1:][same]], 1)
        edges = np.unique(np.sort(np.concatenate([edges, extra]), axis=1), axis=0)
    n = len(V)
    a = np.concatenate([edges[:, 0], edges[:, 1]])
    b = np.concatenate([edges[:, 1], edges[:, 0]])
    o = np.argsort(a, kind='stable')
    a, b = a[o], b[o]
    w = np.linalg.norm(V[a] - V[b], axis=1)
    indptr = np.zeros(n + 1, np.int64)
    np.cumsum(np.bincount(a, minlength=n), out=indptr[1:])
    return {'n': n, 'indptr': indptr, 'idx': b, 'w': w, 'src': a}


def subdivide(V, F, level=1, vattrs=None):
    """Midpoint (4-1) subdivision. Returns (V, F, fparent, vattrs): `fparent` maps every new
    face to its source face and `vattrs` (n,k) arrays are interpolated linearly."""
    vattrs = {k: np.asarray(a, float) for k, a in (vattrs or {}).items()}
    fparent = np.arange(len(F))
    for _ in range(level):
        edges, fe = edge_table(F)
        n = len(V)
        mid = n + fe
        ea, eb = edges[:, 0], edges[:, 1]
        V = np.vstack([V, .5 * (V[ea] + V[eb])])
        vattrs = {k: np.concatenate([a, .5 * (a[ea] + a[eb])]) for k, a in vattrs.items()}
        a, b, c = F.T
        m01, m12, m20 = mid.T
        F = np.concatenate([np.stack([a, m01, m20], 1), np.stack([m01, b, m12], 1),
                            np.stack([m20, m12, c], 1), np.stack([m01, m12, m20], 1)])
        fparent = np.tile(fparent, 4)
    return V, F, fparent, vattrs


def diffuse(g, X, iters):
    """`iters` rounds of neighbour averaging of per-vertex values X along the edge graph."""
    X = np.asarray(X, float)
    deg = np.maximum(np.diff(g['indptr']), 1)
    for _ in range(iters):
        acc = np.zeros_like(X)
        if X.ndim == 1:
            acc = np.bincount(g['src'], X[g['idx']], g['n'])
        else:
            for d in range(X.shape[1]):
                acc[:, d] = np.bincount(g['src'], X[g['idx'], d], g['n'])
        mean = acc / (deg if X.ndim == 1 else deg[:, None])
        X = .5 * (X + mean)
    return X


def mean_edge(V, F):
    edges, _ = edge_table(F)
    return float(np.linalg.norm(V[edges[:, 0]] - V[edges[:, 1]], axis=1).mean())


def level_for(V, F, target_edge):
    """Subdivision levels needed to bring the mean edge down to `target_edge`."""
    return int(max(np.ceil(np.log2(max(mean_edge(V, F) / target_edge, 1))), 0))


def level_cap(n_faces, max_faces):
    """Highest subdivision level that keeps the face count under `max_faces`."""
    return int(max(np.floor(np.log(max(max_faces / max(n_faces, 1), 1)) / np.log(4)), 0))


def make_surface(V, F, face_attrs=None, vert_attrs=None, target_edge=None, level=None,
                 normal_radius=0.0):
    """Subdivide (level, or enough for `target_edge`) and precompute graph and normals.

    `normal_radius` smooths the vertex normals over that surface distance: the original
    angle-weighted normals are interpolated through the subdivision and diffused along the
    edge graph (surface distance, never through space)."""
    V = np.asarray(V, float)
    F = np.asarray(F, np.int64)
    if level is None:
        level = min(level_for(V, F, target_edge), 5) if target_edge else 0
    va = dict(vert_attrs or {})
    va['_n'] = vertex_normals(V, F)
    V2, F2, fparent, va = subdivide(V, F, level, va)
    g = graph(V2, F2)
    n = unit(va.pop('_n'))
    e = mean_edge(V2, F2)
    if normal_radius > 0:
        n = unit(diffuse(g, n, int(round((normal_radius / e) ** 2))))
    surf = {'V': V2, 'F': F2, 'g': g, 'N': n, 'fparent': fparent, 'level': level, 'edge': e,
            'area': face_areas(V2, F2), 'fnormal': face_normals(V2, F2), 'attrs': va,
            'fattrs': {k: np.asarray(a)[fparent] for k, a in (face_attrs or {}).items()}}
    return surf


def sub_surface(surf, face_mask):
    """Restrict a surface to some faces (vertices renumbered; maps back via 'vmap'/'fmap')."""
    fmap = np.flatnonzero(face_mask)
    used = np.unique(surf['F'][fmap])
    remap = -np.ones(len(surf['V']), np.int64)
    remap[used] = np.arange(len(used))
    F = remap[surf['F'][fmap]]
    V = surf['V'][used]
    return {'V': V, 'F': F, 'g': graph(V, F), 'N': surf['N'][used], 'vmap': used, 'fmap': fmap,
            'edge': surf['edge'], 'area': surf['area'][fmap], 'fnormal': surf['fnormal'][fmap],
            'attrs': {k: a[used] for k, a in surf['attrs'].items()},
            'fattrs': {k: a[fmap] for k, a in surf['fattrs'].items()}}


def bary_eval(surf_or_F, attr, face, bary):
    """Interpolate a per-vertex attribute at (face, barycentric) points."""
    F = surf_or_F['F'] if isinstance(surf_or_F, dict) else surf_or_F
    a = np.asarray(attr)[F[face]]                     # (k,3,...)
    return (a * bary.reshape(bary.shape + (1,) * (a.ndim - 2))).sum(1)


def vertex_to_face(F, vals):
    """Mean of per-vertex values over each face's corners."""
    return np.asarray(vals)[F].mean(1)


def components(g):
    """Connected-component label per vertex of a CSR graph."""
    n = g['n']
    lab = -np.ones(n, np.int64)
    cur = 0
    for s in range(n):
        if lab[s] >= 0:
            continue
        stack = [s]
        lab[s] = cur
        while stack:
            u = stack.pop()
            nb = g['idx'][g['indptr'][u]:g['indptr'][u + 1]]
            for v in nb[lab[nb] < 0]:
                lab[v] = cur
                stack.append(v)
        cur += 1
    return lab


def closest_points(V, F, P, chunk=64):
    """Closest point on the triangle soup for each query point (Ericson). Returns
    (points (k,3), face (k,), bary (k,3), dist (k,))."""
    P = np.atleast_2d(np.asarray(P, float))
    A, B, C = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    out_p = np.zeros_like(P)
    out_f = np.zeros(len(P), np.int64)
    out_b = np.zeros((len(P), 3))
    out_d = np.zeros(len(P))
    for s in range(0, len(P), chunk):
        p = P[s:s + chunk, None, :]
        bary = _tri_closest_bary(p, A[None], B[None], C[None])       # (k,m,3)
        q = bary[..., :1] * A + bary[..., 1:2] * B + bary[..., 2:3] * C
        d = np.linalg.norm(q - p, axis=2)
        j = d.argmin(1)
        r = np.arange(len(j))
        out_f[s:s + chunk] = j
        out_b[s:s + chunk] = bary[r, j]
        out_p[s:s + chunk] = q[r, j]
        out_d[s:s + chunk] = d[r, j]
    return out_p, out_f, out_b, out_d


def _tri_closest_bary(p, a, b, c):
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = (ab * ap).sum(-1), (ac * ap).sum(-1)
    bp = p - b
    d3, d4 = (ab * bp).sum(-1), (ac * bp).sum(-1)
    cp = p - c
    d5, d6 = (ab * cp).sum(-1), (ac * cp).sum(-1)
    vc = d1 * d4 - d3 * d2
    vb = d5 * d2 - d1 * d6
    va = d3 * d6 - d5 * d4
    den = np.where(np.abs(va + vb + vc) < EPS, 1.0, va + vb + vc)
    v_in, w_in = vb / den, vc / den
    out = np.stack([1 - v_in - w_in, v_in, w_in], -1)                 # interior default
    div = lambda x, y: x / np.where(np.abs(y) < EPS, 1.0, y)          # noqa: E731

    def put(cond, bary):
        nonlocal out
        out = np.where(cond[..., None], bary, out)

    z, o = np.zeros_like(d1), np.ones_like(d1)
    # later puts override earlier ones: edges BC, AC, AB, then the vertex regions C, B, A
    w = div(d4 - d3, (d4 - d3) + (d5 - d6))
    put((va <= 0) & (d4 - d3 >= 0) & (d5 - d6 >= 0), np.stack([z, 1 - w, w], -1))
    w = div(d2, d2 - d6)
    put((vb <= 0) & (d2 >= 0) & (d6 <= 0), np.stack([1 - w, z, w], -1))
    w = div(d1, d1 - d3)
    put((vc <= 0) & (d1 >= 0) & (d3 <= 0), np.stack([1 - w, w, z], -1))
    put((d6 >= 0) & (d5 <= d6), np.stack([z, z, o], -1))
    put((d3 >= 0) & (d4 <= d3), np.stack([z, o, z], -1))
    put((d1 <= 0) & (d2 <= 0), np.stack([o, z, z], -1))
    return out


def reflect(P, x0=0.0, axis=0):
    """Mirror points or vectors across the plane axis = x0 (vectors: x0 = 0)."""
    P = np.array(P, float)
    P[..., axis] = 2 * x0 - P[..., axis]
    return P


def mirror_error(V, F, x0=0.0, sample=400, seed=0):
    """Max distance of mirrored vertices from the surface: how asymmetric the mesh is."""
    rng = np.random.default_rng(seed)
    ids = rng.choice(len(V), min(sample, len(V)), replace=False)
    _, _, _, d = closest_points(V, F, reflect(V[ids], x0))
    return float(d.max())
