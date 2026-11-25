"""Body-mask prototype, core library (2026-11-04). EXPERIMENT: nothing outside experiments/body_mask_20261104 is written.

Idea: keep the (aligned) garment as it is and solve poke-through on the BODY side: hide base-body regions that are covered, tuck the
visible body near the garment under it, optionally push the garment only by the residual, and give the user per-region inflate sliders.

Reuses tools/tripo_pipeline/body_fit.py (Body, Surface, RayGrid, PokeScene, fit_push, ...) and align_body.py (Rig).
"""
import os
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'tripo_pipeline'))
import body_fit as bf          # noqa: E402
import align_body as ab        # noqa: E402
from scipy import sparse       # noqa: E402
from scipy.spatial import cKDTree   # noqa: E402

REGIONS = ['torso', 'upper_arm', 'forearm', 'hand', 'thigh', 'shin', 'foot']
EXTRA_REGIONS = ['head']                      # not user-facing (slider 0 unless given)
ALL_REGIONS = REGIONS + EXTRA_REGIONS
REGION_OF_PART = {'spine_01.x': 'torso', 'root.x': 'torso', 'neck.x': 'torso', 'head.x': 'head',
                  'shoulder.l': 'upper_arm', 'shoulder.r': 'upper_arm', 'arm_stretch.l': 'upper_arm', 'arm_stretch.r': 'upper_arm',
                  'forearm_stretch.l': 'forearm', 'forearm_stretch.r': 'forearm', 'hand.l': 'hand', 'hand.r': 'hand',
                  'thigh_stretch.l': 'thigh', 'thigh_stretch.r': 'thigh', 'leg_stretch.l': 'shin', 'leg_stretch.r': 'shin',
                  'foot.l': 'foot', 'foot.r': 'foot'}

PARAMS = dict(k=2, ndir=96, margin_pct=0.4, tuck_max_mm=25., escape_thresh=0.02, under_reach_m=0.15, garment_frac=0.30,
              ring=1, rounds=3, kappa=0.004, smooth_iters=400, ray_eps=1e-3, cell=0.03,
              inflate_mm={'torso': 6., 'upper_arm': 4.}, inflate_sigma_pct=3.0, pose_exposed=True)


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


# ----------------------------------------------------------------------------------------------- open body (masked / tucked / posed)

class MaskedBody(bf.Body):
    """The FULL (tucked / posed) sub-divided base as a closed-ish body for body_fit's machinery, plus an `active` triangle mask (False = hidden).
    Sign of the signed distance = generalized winding number of the full mesh (body_fit's reference test, robust against the overlapping limb parts;
    an open mesh has no usable inside). A point whose closest triangle is hidden is not constrained at all (d = +1 m): nothing hidden may push the
    garment. Reverse test (poke) samples are taken from active triangles only (see poke_of). With active = all and no tuck this is the plain Body."""

    def __init__(self, V, F, part, part_names, H, o, active):
        V = np.asarray(V, np.float64)
        F = np.asarray(F, np.int64)
        a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
        keep = np.linalg.norm(np.cross(b - a, c - a), axis=1) > 1e-12       # Body drops degenerate triangles itself: do it here so the masks stay aligned
        part, active = np.asarray(part)[keep], np.asarray(active, bool)[keep]
        super().__init__(V, F[keep], part, part_names)
        assert len(self.F) == int(keep.sum()), 'degenerate sub-triangle dropped'
        self.H, self.o = H, np.asarray(o, np.float64)
        self.active = active

    def measure(self, P, inside_test=None):
        dist, tri, bary = self.closest(P)
        inside = self.winding(P) > .5
        n = np.einsum('ki,kij->kj', bary, self.vn[self.F[tri]])
        ln = np.linalg.norm(n, axis=1, keepdims=True)
        n = np.where(ln > 1e-9, n / np.maximum(ln, 1e-30), self.tri_n[tri])
        d = np.where(inside, -dist, dist)
        d[~self.active[tri]] = 1.0
        return {'d': d, 'n': n, 'part': self.part[tri], 'tri': tri}


# ----------------------------------------------------------------------------------------------- 1. subdivided base

def template(k):
    """Barycentric corner coordinates (4^k, 3 corners, 3 parent weights) of the recursive 4-split children, parent orientation kept.
    Child order: (corner0, corner1, corner2, centre) recursively."""
    tris = np.eye(3)[None]
    for _ in range(k):
        out = []
        for p0, p1, p2 in tris:
            m01, m12, m20 = (p0 + p1) / 2, (p1 + p2) / 2, (p2 + p0) / 2
            out += [[p0, m01, m20], [m01, p1, m12], [m20, m12, p2], [m01, m12, m20]]
        tris = np.array(out)
    return tris


class SubBody:
    """Conforming linear (flat) subdivision of the game base: every triangle -> 4^k triangles on the same plane. Vertices are welded by position
    (1 micron) so parts that share a seam share sub-vertices. Per sub-triangle: parent, part, normal; per vertex: area-weighted smooth normal
    and skin weights (13 limb channels of ab.Rig, interpolated)."""

    def __init__(self, body, rig, k=2):
        self.body, self.k = body, k
        tpl = template(k)
        nc, m = len(tpl), len(body.F)
        corners = np.stack([body.A, body.B, body.C], 1)
        P = np.einsum('cjb,mbx->mcjx', tpl, corners).reshape(-1, 3)
        keys = np.round(P / 1e-6).astype(np.int64)
        _, first, inv = np.unique(keys, axis=0, return_index=True, return_inverse=True)
        inv = inv.ravel()
        self.V = P[first]
        self.F = inv.reshape(-1, 3)
        self.W = np.einsum('cjb,mbk->mcjk', tpl, rig.W[body.F]).reshape(-1, rig.W.shape[1])[first]
        self.parent = np.repeat(np.arange(m), nc)
        self.child = np.tile(np.arange(nc), m)
        self.part = body.part[self.parent]
        self.part_names = body.part_names
        self.nv, self.nt = len(self.V), len(self.F)
        self.pn = body.tri_n                                       # normal of every parent triangle
        self.refresh()

    def refresh(self, V=None):
        V = self.V if V is None else V
        self.A, self.B, self.C = (V[self.F[:, i]] for i in range(3))
        cr = np.cross(self.B - self.A, self.C - self.A)
        self.area = np.linalg.norm(cr, axis=1) / 2
        self.n = self.pn[self.parent] if V is self.V else cr / np.maximum(2 * self.area, 1e-30)[:, None]
        self.cent = (self.A + self.B + self.C) / 3
        vn = np.zeros((self.nv, 3))
        for i in range(3):
            np.add.at(vn, self.F[:, i], cr)                       # |cr| = 2 area: area-weighted
        ln = np.linalg.norm(vn, axis=1, keepdims=True)
        self.vn = np.where(ln > 1e-12, vn / np.maximum(ln, 1e-30), 0.)
        inc = sparse.coo_matrix((np.ones(3 * self.nt), (np.repeat(np.arange(self.nt), 3), self.F.ravel())), shape=(self.nt, self.nv)).tocsr()
        self.inc = inc
        self.adj = (inc @ inc.T).tocsr()                           # triangles sharing a vertex (incl. themselves)

    def ring(self, mask, rings=1):
        """Triangles within `rings` vertex-adjacency steps of `mask`."""
        cur = mask.copy()
        for _ in range(rings):
            cur = (self.adj @ cur.astype(np.float64)) > 0
        return cur

    def masked_body(self, active, V=None):
        """MaskedBody of the whole sub-mesh with vertices V (default rest) and the triangles `active` (False = hidden)."""
        V = self.V if V is None else V
        return MaskedBody(V, self.F, self.part, self.part_names, self.body.H, self.body.o, active)


# ----------------------------------------------------------------------------------------------- 2. visibility / coverage (2D-grid ray casting)

def fibonacci(n):
    i = np.arange(n) + .5
    z = 1 - 2 * i / n
    r = np.sqrt(1 - z * z)
    a = np.pi * (1 + 5 ** .5) * i
    return np.stack([r * np.cos(a), r * np.sin(a), z], 1)


def _grid_nearest(Op, tris, cell, return_idx=False):
    """Nearest hit distance t > 0 along +z of the projected frame, for every origin Op (n,3), against projected triangles tris=(A,B,C) (m,3 each,
    xy = screen, z = depth); inf when none. One 2D uniform grid of the triangles' bounding boxes. `return_idx`: also the index of the nearest triangle
    per origin (-1 when none) -> (t, idx)."""
    A, B, C = tris
    n = len(Op)
    out = np.full(n, np.inf)
    oidx = np.full(n, -1, np.int64)

    def ret():
        return (out, oidx) if return_idx else out
    if not len(A) or not n:
        return ret()
    lo = np.minimum(np.minimum(A[:, :2], B[:, :2]), C[:, :2]) - 1e-6
    hi = np.maximum(np.maximum(A[:, :2], B[:, :2]), C[:, :2]) + 1e-6
    o = lo.min(0)
    ilo = np.floor((lo - o) / cell).astype(np.int64)
    ihi = np.floor((hi - o) / cell).astype(np.int64)
    dim = ihi.max(0) + 1
    span = ihi - ilo + 1
    counts = span[:, 0] * span[:, 1]
    tri = np.repeat(np.arange(len(A)), counts)
    off = np.arange(int(counts.sum())) - np.repeat(np.cumsum(counts) - counts, counts)
    sx = span[tri, 0]
    cid = (ilo[tri, 0] + off % sx) * dim[1] + (ilo[tri, 1] + off // sx)
    order = np.argsort(cid, kind='stable')
    cid, tri = cid[order], tri[order]
    oc = np.floor((Op[:, :2] - o) / cell).astype(np.int64)
    ok = np.all((oc >= 0) & (oc < dim), axis=1)
    ocid = np.where(ok, oc[:, 0] * dim[1] + oc[:, 1], -1)
    a, b = np.searchsorted(cid, ocid, 'left'), np.searchsorted(cid, ocid, 'right')
    cnt = np.where(ok, b - a, 0)
    total = int(cnt.sum())
    if not total:
        return ret()
    ray = np.repeat(np.arange(n), cnt)
    ti = tri[np.arange(total) - np.repeat(np.cumsum(cnt) - cnt, cnt) + np.repeat(a, cnt)]
    p = Op[ray, :2]
    a2 = A[ti, :2]
    d1 = B[ti, :2] - a2
    d2 = C[ti, :2] - a2
    w = p - a2
    den = d1[:, 0] * d2[:, 1] - d1[:, 1] * d2[:, 0]
    with np.errstate(divide='ignore', invalid='ignore'):
        l1 = (w[:, 0] * d2[:, 1] - w[:, 1] * d2[:, 0]) / den
        l2 = (d1[:, 0] * w[:, 1] - d1[:, 1] * w[:, 0]) / den
    t = A[ti, 2] + l1 * (B[ti, 2] - A[ti, 2]) + l2 * (C[ti, 2] - A[ti, 2]) - Op[ray, 2]
    hit = (np.abs(den) > 1e-14) & (l1 >= -1e-9) & (l2 >= -1e-9) & (l1 + l2 <= 1 + 1e-9) & (t > 0)
    if not hit.any():
        return ret()
    ray, t, ti = ray[hit], t[hit], ti[hit]
    starts = np.flatnonzero(np.r_[True, ray[1:] != ray[:-1]])
    tmin = np.minimum.reduceat(t, starts)
    out[ray[starts]] = tmin
    if return_idx:
        counts = np.diff(np.r_[starts, len(ray)])
        is_min = np.flatnonzero(t == np.repeat(tmin, counts))
        first = is_min[np.searchsorted(is_min, starts)]          # first triangle of each ray's group that attains the minimum
        oidx[ray[starts]] = ti[first]
    return ret()


def frame_of(d):
    """Projection frame of direction d: columns (u, v, d); u = d x z (screen right for a horizontal d looking along +y), v = d x u (screen down)."""
    u = np.cross(d, [0., 0., 1.] if abs(d[2]) < .9 else [1., 0., 0.])
    u /= np.linalg.norm(u)
    v = np.cross(d, u)
    return np.stack([u, v, d], 1)


def cast_dir(d, O, groups, cell, return_idx=False):
    """Nearest hit distance along direction d from every origin for each triangle group (list of (A,B,C)); with `return_idx` each entry is (t, triangle index)."""
    M = frame_of(d)
    Op = O @ M
    return [_grid_nearest(Op, tuple(t @ M for t in g), cell, return_idx) for g in groups]


def tri_samples(sb, A=None, B=None, C=None, n=None):
    """3 sample points per sub-triangle (barycentric (4,1,1)/6 permutations). Returns points (nt*3,3), tri index (nt*3,), bary (3,3)."""
    A = sb.A if A is None else A
    B = sb.B if B is None else B
    C = sb.C if C is None else C
    bary = np.array([[4, 1, 1], [1, 4, 1], [1, 1, 4]], np.float64) / 6
    P = bary[None, :, 0, None] * A[:, None] + bary[None, :, 1, None] * B[:, None] + bary[None, :, 2, None] * C[:, None]
    return P.reshape(-1, 3), np.repeat(np.arange(len(A)), 3), bary


def visibility(A, B, C, N, garment, occluder, ndir, eps, cell, only=None):
    """Outward-hemisphere visibility of triangles (A, B, C, normals N): per triangle cos-weighted fraction of rays that escape (hit neither the
    garment nor the occluder triangles), whose nearest hit is garment, or that hit garment anywhere. Max over the 3 sample points for escape,
    mean for the others. `garment` / `occluder` = (A, B, C). `only` = triangle subset (index array)."""
    idx = np.arange(len(A)) if only is None else np.asarray(only)
    P, tri_of, _ = tri_samples(None, A[idx], B[idx], C[idx])
    Ns = N[idx][tri_of]
    O = P + Ns * eps
    ns = len(P)
    wsum, esc, gfirst, gany = (np.zeros(ns) for _ in range(4))
    for d in fibonacci(ndir):
        c = Ns @ d
        sel = np.flatnonzero(c > .05)
        if not len(sel):
            continue
        tg, tb = cast_dir(d, O[sel], [garment, occluder], cell)
        w = c[sel]
        wsum[sel] += w
        esc[sel] += w * (np.isinf(tg) & np.isinf(tb))
        gfirst[sel] += w * (tg < tb)
        gany[sel] += w * np.isfinite(tg)
    wsum = np.maximum(wsum, 1e-9)
    esc, gfirst, gany = (x / wsum for x in (esc, gfirst, gany))
    out = {k: np.zeros(len(A)) for k in ('esc', 'gfirst', 'gany')}
    out['esc'][idx] = esc.reshape(-1, 3).max(1)
    out['gfirst'][idx] = gfirst.reshape(-1, 3).mean(1)
    out['gany'][idx] = gany.reshape(-1, 3).mean(1)
    return out


def seen_counts(A, B, C, N, garments, occluder, ndir, eps, cell, cos_min=0.1):
    """UNWEIGHTED outward visibility of triangles (A, B, C, normals N) for a LIST of garments `garments` = [(A,B,C), ...] (2026-11-21, section rule 'seen'): per garment g,
    the number of the `ndir` Fibonacci directions with cos(direction, normal) > cos_min whose ray from a sample point (max over the 3 samples of the triangle) escapes,
    i.e. hits neither garment g nor the `occluder` triangles (cast once per direction). Returns {'seen_n': (G, nt) int, 'n_out': nt (outward directions, max over the samples),
    'esc' / 'gfirst' / 'gany': the legacy cos-weighted fractions of `visibility` for garment 0 (same formulas, only the occluder may differ)}."""
    P, tri_of, _ = tri_samples(None, A, B, C)
    Ns = N[tri_of]
    O = P + Ns * eps
    ns, G = len(P), len(garments)
    cnt = np.zeros((G, ns), np.int32)
    nout = np.zeros(ns, np.int32)
    wsum, esc, gfirst, gany = (np.zeros(ns) for _ in range(4))
    for d in fibonacci(ndir):
        c = Ns @ d
        sel = np.flatnonzero(c > min(cos_min, .05))
        if not len(sel):
            continue
        tb = cast_dir(d, O[sel], [occluder], cell)[0]
        w = c[sel]
        out = w > cos_min
        nout[sel] += out
        for g, gar in enumerate(garments):
            tg = cast_dir(d, O[sel], [gar], cell)[0]
            cnt[g, sel] += np.isinf(tg) & np.isinf(tb) & out
            if g == 0:
                wsum[sel] += w
                esc[sel] += w * (np.isinf(tg) & np.isinf(tb))
                gfirst[sel] += w * (tg < tb)
                gany[sel] += w * np.isfinite(tg)
    wsum = np.maximum(wsum, 1e-9)
    return {'seen_n': cnt.reshape(G, -1, 3).max(2), 'n_out': nout.reshape(-1, 3).max(1), 'esc': (esc / wsum).reshape(-1, 3).max(1),
            'gfirst': (gfirst / wsum).reshape(-1, 3).mean(1), 'gany': (gany / wsum).reshape(-1, 3).mean(1)}


# ----------------------------------------------------------------------------------------------- normal-ray gaps (body_fit's reverse-test logic)

class NormalGap:
    """Along-normal signed gap of points to a garment: t_out = nearest garment in front (<= POKE_OUT), depth = how far the garment lies BEHIND the
    surface when nothing is in front (body poking through), with body_fit's filters (hit point still inside the body; at least POKE_NEAR of the depth
    away from any other body surface). Mirrors PokeScene.poke."""

    def __init__(self, garment, body):
        self.grid = bf.RayGrid(*garment)
        self.body = body
        self.bgrid = bf.RayGrid(body.A, body.B, body.C)

    def __call__(self, P, N):
        h = self.grid.hits(P, N, -bf.POKE_IN, bf.POKE_OUT)
        t_out, t_in = h['t_out'], h['t_in']
        exit_ = -self.bgrid.hits(P, N, -bf.POKE_IN, 0., eps=1e-4)['t_in']
        depth = -t_in
        ok = np.isfinite(t_in) & np.isinf(t_out) & (depth < exit_)
        idx = np.flatnonzero(ok)
        if len(idx):
            X = P[idx] - N[idx] * depth[idx, None]
            keep = self.body.closest(X)[0] >= bf.POKE_NEAR * depth[idx]
            ok[idx[~keep]] = False
        return t_out, np.where(ok, depth, 0.)


def need_from_gap(t_out, depth, margin):
    """Inward displacement the body surface needs at a point (m): margin - t_out when the garment is closer than the margin in front,
    depth + margin when the garment lies behind the surface (poke)."""
    need = np.zeros(len(t_out))
    near = np.isfinite(t_out)
    need[near] = np.maximum(0., margin - t_out[near])
    pk = depth > 0
    need[pk] = depth[pk] + margin
    return need


# ----------------------------------------------------------------------------------------------- 3-4. hide mask and tuck

def smooth_tuck(sb, kept, need, zero, kappa, iters):
    """Lower-bounded smooth tuck depth u (m) per sub-vertex over the kept mesh: u >= need where need > 0, u = 0 on `zero` vertices, in between a
    screened inverse-length Laplacian (body_fit's KAPPA smoothing); vertices of hidden-only triangles are not in the graph (free, u = 0)."""
    F = sb.F[kept]
    e = np.unique(np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1), axis=0)
    e = e[e[:, 0] != e[:, 1]]
    w = 1. / np.maximum(np.linalg.norm(sb.V[e[:, 0]] - sb.V[e[:, 1]], axis=1), 1e-9)
    W = sparse.coo_matrix((np.r_[w, w], (np.r_[e[:, 0], e[:, 1]], np.r_[e[:, 1], e[:, 0]])), shape=(sb.nv, sb.nv)).tocsr()
    rows = np.asarray(W.sum(1)).ravel()
    S = sparse.diags(np.where(rows > 0, 1. / np.maximum(rows * (1 + kappa), 1e-30), 0.)) @ W
    tgt = np.where(zero, 0., need)
    u = tgt.copy()
    for _ in range(iters):
        u = S @ u
        u[zero] = 0.
        np.maximum(u, tgt, out=u)
        u[zero] = 0.
    return u


def poke_of(ob, U, surf, spacing_pct=None):
    """Reverse test (body_fit PokeScene) of MaskedBody `ob` (only samples on its active triangles) against the garment `surf` at welded positions U.
    Returns the poke dict plus 'body_tri' (triangle of `ob` every poking sample lies on)."""
    old = bf.POKE_SPACING_PCT
    if spacing_pct:
        bf.POKE_SPACING_PCT = spacing_pct       # in-process override only (coarser samples for intermediate / posed checks)
    try:
        smp = bf.body_samples(ob)
        scene = bf.scene_for(ob, [U])
    finally:
        bf.POKE_SPACING_PCT = old
    near = np.flatnonzero(np.all((smp['P'] >= U.min(0) - bf.POKE_OUT) & (smp['P'] <= U.max(0) + bf.POKE_OUT), axis=1))
    act = ob.active[smp['tri'][near]]
    for k in ('P', 'N', 'area', 'part', 'exit'):
        setattr(scene, k, getattr(scene, k)[act])
    btri = smp['tri'][near][act]
    pk = scene.poke([scene.cast(*bf.surf_tris(surf, U))])
    pk['body_tri'] = btri[pk['sample']]
    return pk


def drawn_ctx(mb, U):
    """PokeCtx whose body samples are only those on the ACTIVE (drawn) triangles of MaskedBody `mb` (garment at welded positions U fixes the sample window):
    hidden triangles must neither poke nor push the garment."""
    ctx = bf.PokeCtx(bf.scene_for(mb, [U]))
    smp = bf.body_samples(mb)
    near = np.flatnonzero(np.all((smp['P'] >= U.min(0) - bf.POKE_OUT) & (smp['P'] <= U.max(0) + bf.POKE_OUT), axis=1))
    act = mb.active[smp['tri'][near]]
    for k in ('P', 'N', 'area', 'part', 'exit'):
        setattr(ctx.scene, k, getattr(ctx.scene, k)[act])
    return ctx


def poke_summary(pk):
    d = pk['depth'] * 1000
    return {'area_cm2': float(pk['area'].sum() * 1e4), 'max_mm': float(d.max()) if len(d) else 0., 'p90_mm': float(np.percentile(d, 90)) if len(d) else 0.,
            'area_deeper_1mm_cm2': float(pk['area'][pk['depth'] >= 1e-3].sum() * 1e4)}


# ----------------------------------------------------------------------------------------------- 6. inflate sliders

class InflateField:
    """Soft region membership and smooth outward direction of the base at any point: 20k surface samples (region of the part, smooth normal); a point's
    membership is the Gaussian-weighted (sigma = inflate_sigma_pct % H, like align_body's skin-weight transfer) share of the K nearest samples."""

    def __init__(self, body, sigma_pct=3.0, n=20000, K=256, seed=3):
        rng = np.random.default_rng(seed)
        w = body.area / body.area.sum()
        f = rng.choice(len(body.F), size=n, p=w)
        u, v = rng.random(n), rng.random(n)
        fl = u + v > 1
        u[fl], v[fl] = 1 - u[fl], 1 - v[fl]
        bary = np.stack([1 - u - v, u, v], 1)
        self.P = np.einsum('ki,kij->kj', bary, np.stack([body.A[f], body.B[f], body.C[f]], 1))
        nn = np.einsum('ki,kij->kj', bary, body.vn[body.F[f]])
        ln = np.linalg.norm(nn, axis=1, keepdims=True)
        self.N = np.where(ln > 1e-9, nn / np.maximum(ln, 1e-30), body.tri_n[f])
        reg = np.array([ALL_REGIONS.index(REGION_OF_PART[body.part_names[p]]) for p in body.part])
        self.R = np.eye(len(ALL_REGIONS))[reg[f]]
        self.tree = cKDTree(self.P)
        self.sigma = sigma_pct / 100 * body.H
        self.K = K

    def at(self, X, chunk=2000):
        """(region weights (n, len(ALL_REGIONS)), outward direction (n,3)) at points X."""
        Rw = np.empty((len(X), len(ALL_REGIONS)))
        Dn = np.empty((len(X), 3))
        for s in range(0, len(X), chunk):
            d, idx = self.tree.query(X[s:s + chunk], k=self.K)
            k = np.exp(-((d ** 2 - d[:, :1] ** 2) / (2 * self.sigma ** 2)))
            ks = k.sum(1, keepdims=True)
            Rw[s:s + chunk] = np.einsum('nk,nkc->nc', k, self.R[idx]) / ks
            nd = np.einsum('nk,nkc->nc', k, self.N[idx])
            ln = np.linalg.norm(nd, axis=1, keepdims=True)
            Dn[s:s + chunk] = np.where(ln / ks > .3, nd / np.maximum(ln, 1e-30), self.N[idx[:, 0]])
        return Rw, Dn


def inflate(field, U, sliders_mm):
    """Welded garment vertices U (n,3) pushed along the smooth body normal by sum_r membership_r * sliders[r] (mm). Returns new U.
    One continuous displacement field of the position: no steps between regions, welded vertices (same position) move together."""
    amount = np.array([float(sliders_mm.get(r, 0.)) for r in ALL_REGIONS]) / 1000.
    Rw, Dn = field.at(U)
    return U + Dn * (Rw @ amount)[:, None]


# ----------------------------------------------------------------------------------------------- 7. patches

def make_patches(sb, scale=1.0):
    """Fixed body patches: every bone part split into bands along its principal axis; torso parts by height band x front/back/left/right.
    `scale` multiplies the band counts. Returns patch id per sub-triangle and the list of patch names."""
    base_bands = {'spine_01.x': 4, 'root.x': 2, 'neck.x': 1, 'head.x': 1, 'shoulder.l': 1, 'shoulder.r': 1, 'arm_stretch.l': 3, 'arm_stretch.r': 3,
                  'forearm_stretch.l': 3, 'forearm_stretch.r': 3, 'hand.l': 2, 'hand.r': 2, 'thigh_stretch.l': 4, 'thigh_stretch.r': 4,
                  'leg_stretch.l': 3, 'leg_stretch.r': 3, 'foot.l': 2, 'foot.r': 2}
    torso = ('spine_01.x', 'root.x')
    pid = np.full(sb.nt, -1, np.int64)
    names = []
    for pi, pn in enumerate(sb.part_names):
        sel = np.flatnonzero(sb.part == pi)
        if not len(sel):
            continue
        nb = max(1, int(round(base_bands.get(pn, 2) * scale)))
        c = sb.cent[sel]
        if pn in torso:
            t = c[:, 2]
            band = np.minimum(((t - t.min()) / max(np.ptp(t), 1e-9) * nb).astype(int), nb - 1)
            d = c[:, :2] - c[:, :2].mean(0)
            sector = np.where(np.abs(d[:, 1]) >= np.abs(d[:, 0]), np.where(d[:, 1] < 0, 0, 1), np.where(d[:, 0] > 0, 2, 3))   # front, back, left(+x), right
            label = [f'{pn}/h{b}/{"FBLR"[s]}' for b, s in zip(band, sector)]
            key = band * 4 + sector
        else:
            w = sb.area[sel]
            m = (c * w[:, None]).sum(0) / w.sum()
            _, _, vt = np.linalg.svd((c - m) * np.sqrt(w)[:, None], full_matrices=False)
            t = (c - m) @ vt[0]
            band = np.minimum(((t - t.min()) / max(np.ptp(t), 1e-9) * nb).astype(int), nb - 1)
            key = band
            label = [f'{pn}/b{b}' for b in band]
        for kk in np.unique(key):
            m_ = key == kk
            pid[sel[m_]] = len(names)
            names.append(label[int(np.flatnonzero(m_)[0])])
    assert (pid >= 0).all()
    return pid, names


def patch_report(sb, patch_id, patch_names, hidden, eligible):
    """How much of the per-triangle hidden / eligible area survives when a patch can only be hidden WHOLE (all its triangles in the set)."""
    n = len(patch_names)
    tot = np.bincount(patch_id, weights=sb.area, minlength=n)

    def whole(mask):
        inside = np.bincount(patch_id, weights=sb.area * mask, minlength=n)
        full = inside >= tot - 1e-12
        return full, float((sb.area * full[patch_id] * mask).sum())
    out = {'patches': n}
    for tag, mask in (('hidden', hidden), ('eligible', eligible)):
        full, a = whole(mask)
        ref = float((sb.area * mask).sum())
        out[tag] = {'subtri_area_cm2': ref * 1e4, 'patch_area_cm2': a * 1e4, 'ratio': a / ref if ref else 0., 'patches_hidden': int(full.sum())}
    return out


# ----------------------------------------------------------------------------------------------- 8. pose

def pose_rotvecs(rig, name):
    rv = np.zeros((12, 3))
    ix = {n: i for i, n in enumerate(rig.names)}
    if name == 'arms_elbows':
        th, el = np.radians(50), np.radians(60)
        rv[ix['arm.l']] = [0, th, 0]
        rv[ix['arm.r']] = [0, -th, 0]
        rv[ix['forearm.l']] = [0, 0, -el]
        rv[ix['forearm.r']] = [0, 0, el]
    elif name == 'thigh_knee':
        rv[ix['thigh.l']] = [-np.radians(40), 0, 0]
        rv[ix['leg.l']] = [np.radians(50), 0, 0]
    else:
        raise ValueError(name)
    return rv


class SkinTransfer:
    """Gaussian skin-weight transfer from the REST base surface (the align_body.Field recipe: sigma = KERNEL_SIGMA_PCT % H, nearest samples)."""

    def __init__(self, body, rig, n=12000, seed=5):
        rng = np.random.default_rng(seed)
        f = rng.choice(len(body.F), size=n, p=body.area / body.area.sum())
        u, v = rng.random(n), rng.random(n)
        fl = u + v > 1
        u[fl], v[fl] = 1 - u[fl], 1 - v[fl]
        bary = np.stack([1 - u - v, u, v], 1)
        self.P = np.einsum('ki,kij->kj', bary, np.stack([body.A[f], body.B[f], body.C[f]], 1))
        self.W = np.einsum('ki,kij->kj', bary, rig.W[body.F[f]])
        self.tree = cKDTree(self.P)
        self.sigma = ab.KERNEL_SIGMA_PCT / 100 * body.H

    def __call__(self, X, K=384, chunk=2000):
        out = np.empty((len(X), self.W.shape[1]))
        for s in range(0, len(X), chunk):
            d, idx = self.tree.query(X[s:s + chunk], k=K)
            k = np.exp(-((d ** 2 - d[:, :1] ** 2) / (2 * self.sigma ** 2)))
            out[s:s + chunk] = np.einsum('nk,nkc->nc', k, self.W[idx]) / k.sum(1, keepdims=True)
        return out


# ----------------------------------------------------------------------------------------------- generic sub-divided mesh (variant B analysis mesh)

class SubMesh(SubBody):
    """The same flat 4^k subdivision for ANY triangle mesh of the base (here the sectioned base): analysis only, never shipped. Vertices are welded
    by position AND skin weights (the base has duplicate seam vertices with different weights that must stay separate). `sec` = section of every
    sub-triangle; normals are the ORIGINAL facet normals (`orig` = original game-base triangle of every input triangle)."""

    def __init__(self, body, V, F, W, part, orig, sec, sec_names, k=2):
        self.body, self.k = body, k
        tpl = template(k)
        nc, m = len(tpl), len(F)
        corners = np.stack([V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]], 1)
        P = np.einsum('cjb,mbx->mcjx', tpl, corners).reshape(-1, 3)
        Wc = np.einsum('cjb,mbk->mcjk', tpl, W[F]).reshape(-1, W.shape[1])
        keys = np.concatenate([np.round(P / 1e-6).astype(np.int64), np.round(Wc * 1e5).astype(np.int64)], axis=1)
        _, first, inv = np.unique(keys, axis=0, return_index=True, return_inverse=True)
        inv = inv.ravel()
        self.V = P[first]
        self.W = Wc[first]
        self.F = inv.reshape(-1, 3)
        self.parent = np.repeat(np.arange(m), nc)
        self.child = np.tile(np.arange(nc), m)
        self.part = part[self.parent]
        self.sec = sec[self.parent]
        self.sec_names = sec_names
        self.part_names = body.part_names
        self.nv, self.nt = len(self.V), len(self.F)
        self.pn = body.tri_n[orig]
        self.refresh()


def smooth_field(V, F, need, zero, kappa, iters):
    """smooth_tuck on explicit arrays: lower-bounded screened-Laplacian field over the mesh (V, F); `zero` vertices pinned to 0."""
    nv = len(V)
    e = np.unique(np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1), axis=0)
    e = e[e[:, 0] != e[:, 1]]
    w = 1. / np.maximum(np.linalg.norm(V[e[:, 0]] - V[e[:, 1]], axis=1), 1e-9)
    W = sparse.coo_matrix((np.r_[w, w], (np.r_[e[:, 0], e[:, 1]], np.r_[e[:, 1], e[:, 0]])), shape=(nv, nv)).tocsr()
    rows = np.asarray(W.sum(1)).ravel()
    S = sparse.diags(np.where(rows > 0, 1. / np.maximum(rows * (1 + kappa), 1e-30), 0.)) @ W
    tgt = np.where(zero, 0., need)
    u = tgt.copy()
    for _ in range(iters):
        u = S @ u
        u[zero] = 0.
        np.maximum(u, tgt, out=u)
        u[zero] = 0.
    return u


def bare_filter(sm, visible, min_cm2):
    """Clean the per-triangle 'bare skin' flag: majority filter (thin streaks along grazing / poke edges are not skin) and a minimum patch area."""
    from scipy.sparse import csgraph
    visible = visible.copy()
    deg = np.asarray(sm.adj.sum(1)).ravel()
    for _ in range(2):
        visible = visible & ((sm.adj @ visible.astype(np.float64)) / deg >= .6)
    if visible.any():
        bi = np.flatnonzero(visible)
        _, lab = csgraph.connected_components(sm.adj[bi][:, bi], directed=False)
        comp_area = np.bincount(lab, weights=sm.area[bi])
        visible[bi[comp_area[lab] < min_cm2 * 1e-4]] = False
    return visible
