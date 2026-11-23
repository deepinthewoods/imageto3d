"""Chain-mail ring detection and colour clean-up on the HIGH-POLY labels (2026-10-25; integrated as the opt-in `rings` feature label pass,
see feature_hooks.py: `wearcut.py` / `segment_first.py` `--feature-labels rings:secondary|rings:void`; this file is the algorithm and an
analysis CLI that writes into a scratch folder).

    characters/.venv/bin/python tools/tripo_pipeline/ring_labels.py <module_dir> <out_dir> [--ring primary] [--centre secondary|void|<role>]
        [--sigma auto|0.034] [--void-above-z auto|<z>] [--levels 0p4,0p2,0p1] [--no-recut] [--no-relabel-lod]

Problem. Tripo paints chain-mail rings from a few blurred views: ring centres (the undershirt showing through, `centre`) come out
partly green, ring tubes partly blue, the boundaries are jagged. The geometry is far more reliable: a ring is a raised torus-like tube
over a recessed undershirt (or over nothing). So the high-poly mesh is classified by **relief** - the height of every face over a
heat-smoothed copy of the garment surface - with the texture only as a weak prior and as the calibration of the threshold:

 1. garment faces = the cleaned high-poly labels > 0 (`<module>/wearcut/labels.npz`, same face order as `wearcut/prep/prep.npz`).
 2. relief h = (P - P_s) . N_s per vertex, P_s / N_s the position / normal diffused over the garment mesh (cotangent heat flow, no crease
    weights, scale sigma ~ 1.2 x ring hole diameter; a ladder of scales from ONE sparse factorisation, the scale with the best
    texture-supervised class separation wins). Positive = raised over its surroundings.
 3. protected = large connected blobs of a NON-ring class (belt, shoulder patch, buckle, under-skirt; area > big_area_factor x sigma^2):
    they are not ring centres and keep their colour.
 4. local calibration: texture-confident ring faces and centre faces give, by normalised heat convolution at a larger scale, a local ring
    level m_r(x) and centre level m_c(x); threshold t = (m_r + m_c) / 2, score = (h - t) / ((m_r - m_c) / 2). Where the separation is
    below `sep_min` there is no relief (no rings): the texture labels stay. Two EM-like passes (weak labels = the geometric result).
 5. decision = score + texture_prior x (+-1) > 0 -> ring, else centre; specks smaller than a fraction of a hole are absorbed.
 6. rings: the centre components are the holes (count, equivalent diameter), every ring face is assigned to its nearest hole.
 7. the new label: ring -> `ring` role (primary), centre -> `centre` role (secondary), or `void` (label 0 = base: the cut machinery
    removes those faces, the wearable gets real holes), optionally only above a waist height (`void_above_z`).

Outputs (out_dir): ring_labels.npz (`label` new, `label_texture` old, `kind` 0 untouched / 1 ring / 2 centre / 3 protected, `h` face relief,
`score`, `hole_id`, `ring_id`; face order of prep.npz), ring_labels.json (every number). With --recut / --relabel-lod see recut_wearable()
and relabel_lod(): the wearable is re-cut with wearcut.run into the scratch folder with the rings hook (`feature_hooks.make_hook`;
nothing under models/ is written), and the full labelled LODs get the high-poly labels transferred.
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
from scipy import sparse
from scipy.sparse import csgraph
from scipy.sparse import linalg as spla
from scipy.spatial import cKDTree

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

ROLES = ['base', 'primary', 'secondary', 'tertiary', 'accent']

P = {
    'ring': 'primary',               # role of the rings (tubes)
    'centre': 'secondary',           # role of the ring centres: a role name, or 'void' (faces removed, label 0)
    'sigma0': .012,                  # smoothing scale of one factorised step (normalised units, height 2)
    'ladder': (2, 4, 8, 16, 32),     # steps -> sigma = sigma0 * sqrt(steps)  (.017 .024 .034 .048 .068)
    'sigma': None,                   # None / 'auto': best of the ladder; or a number (nearest ladder step)
    'default_sigma': .034,
    'calib_sigma': .09,              # scale of the local calibration (normalised convolution)
    'big_area_factor_void': 25.,     # the same in void mode (filler blobs of gap material can be sizeable; buckle / patches / belt are larger)
    'big_area_factor': 10.,          # a non-ring blob larger than this x sigma^2 is protected (belt, patch, skirt ...)
    'protect_mode': None,            # 'relief' (centre = a role: protect large non-ring blobs that are raised or flat) / 'size' (void: every large non-ring blob)
    'open_radius': .012,             # size mode: a protected blob is opened with this radius (thin webs of filler are not design blobs)
    'protect_frac': .35,             # a large non-ring blob whose median relief is above centre + this x (ring - centre) is protected
    'sep_min': .003,                 # ring/centre level separation below which the area has no relief (normalised units)
    'texture_prior': .2,             # weight of the texture class (+-1) next to the relief score
    'majority_iterations': 2,
    'score_smooth': 3,               # face-graph smoothing iterations of the score (evens out the sawtooth on the steep ring walls)
    'passes': 2,
    'min_hole_share': .12,           # centre components smaller than this share of the median hole are specks
    'min_ring_share': .05,           # ring components (isolated) smaller than this share of the median hole are specks
    'void_above_z': None,            # void mode: only faces above this height (None: all, 'auto': the top of the buckle / belt)
    'crease_deg': 25., 'crease_low': 1.,   # 1.0 = no crease weighting (relief must be smoothed across the ring walls)
}


# ----------------------------------------------------------------------------------------------- mesh helpers

def tri_geometry(v, f):
    tri = v[f]
    cr = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    a = np.linalg.norm(cr, axis=1) / 2
    n = cr / np.maximum(2 * a[:, None], 1e-30)
    return tri.mean(1), a, n


def compact(v, f):
    ids, inv = np.unique(f, return_inverse=True)
    return v[ids], inv.reshape(-1, 3), ids


def edge_pairs(f):
    """(E, 2) face pairs sharing a manifold edge."""
    n = len(f)
    a = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
    b = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
    face = np.tile(np.arange(n), 3)
    key = np.minimum(a, b).astype(np.int64) * (int(f.max()) + 1) + np.maximum(a, b)
    order = np.argsort(key, kind='stable')
    key, face = key[order], face[order]
    first = np.flatnonzero(key[1:] == key[:-1])
    ok = np.ones(len(first), bool)
    ok[1:] &= first[1:] != first[:-1] + 1
    ok[:-1] &= first[1:] != first[:-1] + 1
    return np.stack([face[first[ok]], face[first[ok] + 1]], 1)


def components(mask, pairs, n):
    """Connected components of the faces in `mask` (shared-edge adjacency). Returns (count, id per face, -1 outside the mask)."""
    keep = mask[pairs[:, 0]] & mask[pairs[:, 1]]
    g = sparse.coo_matrix((np.ones(keep.sum(), np.int8), (pairs[keep, 0], pairs[keep, 1])), shape=(n, n))
    k, comp = csgraph.connected_components(g, directed=False)
    comp = np.where(mask, comp, -1)
    ids, inv = np.unique(comp[mask], return_inverse=True)
    out = np.full(n, -1, np.int64)
    out[mask] = inv
    return len(ids), out


def cotan_operator(v, f, crease_deg, crease_low):
    """Cotangent stiffness C and lumped mass; crease weights as wearcut.smoothing_operator (crease_low = 1 switches them off)."""
    cen, area, nrm = tri_geometry(v, f)
    tri = v[f]
    e = [tri[:, 2] - tri[:, 1], tri[:, 0] - tri[:, 2], tri[:, 1] - tri[:, 0]]
    cot = np.zeros((len(f), 3))
    for k in range(3):
        a, b = -e[(k + 1) % 3], e[(k + 2) % 3]
        cross = np.linalg.norm(np.cross(a, b), axis=1)
        cot[:, k] = (a * b).sum(1) / np.maximum(cross, 1e-20)
    w = np.clip(cot, 0, 30) / 2
    if crease_low < 1.:
        import wearcut as wc
        twin = wc.half_edge_twins(f)
        ang = np.degrees(np.arccos(np.clip((nrm[:, None, :] * nrm[np.maximum(twin, 0)]).sum(2), -1, 1)))
        w = w * np.where(twin >= 0, crease_low + (1 - crease_low) * np.exp(-(ang / crease_deg) ** 2), 1.)
    rows = np.concatenate([f[:, 1], f[:, 2], f[:, 0], f[:, 2], f[:, 0], f[:, 1]])
    cols = np.concatenate([f[:, 2], f[:, 0], f[:, 1], f[:, 1], f[:, 2], f[:, 0]])
    vals = np.concatenate([w[:, 0], w[:, 1], w[:, 2], w[:, 0], w[:, 1], w[:, 2]])
    W = sparse.coo_matrix((vals, (rows, cols)), shape=(len(v), len(v))).tocsr()
    C = sparse.diags(np.asarray(W.sum(1)).ravel()) - W
    mass = np.bincount(f.ravel(), weights=np.repeat(area / 3, 3), minlength=len(v))
    return C.tocsr(), np.maximum(mass, 1e-12)


class Heat:
    """Backward-Euler heat flow on a mesh from one sparse LU factorisation: smooth(X, steps) applies (M + tau C)^-1 M `steps` times
    (sigma_eff ~ sqrt(2 tau steps))."""

    def __init__(self, v, f, sigma, crease_deg=25., crease_low=1.):
        self.C, self.mass = cotan_operator(v, f, crease_deg, crease_low)
        self.sigma = sigma
        A = (sparse.diags(self.mass) + (sigma ** 2 / 2) * self.C).tocsc()
        self.lu = spla.splu(A, permc_spec='COLAMD')

    def smooth(self, X, steps=1):
        X = np.asarray(X, np.float64)
        one = X.ndim == 1
        if one:
            X = X[:, None]
        for _ in range(steps):
            X = self.lu.solve(self.mass[:, None] * X)
        return X[:, 0] if one else X


def vertex_attr(f, nv, face_values, weights):
    """Area-weighted mean of face values (n,) or (n, d) at the vertices."""
    fv = np.asarray(face_values, np.float64)
    if fv.ndim == 1:
        fv = fv[:, None]
    out = np.zeros((nv, fv.shape[1]))
    den = np.bincount(f.ravel(), weights=np.repeat(weights, 3), minlength=nv)
    for k in range(fv.shape[1]):
        out[:, k] = np.bincount(f.ravel(), weights=np.repeat(fv[:, k] * weights, 3), minlength=nv)
    return out / np.maximum(den, 1e-30)[:, None]


def relief_ladder(v, f, heat, ladder):
    """Relief h (per vertex) at every step count of the ladder, from the heat-smoothed positions and normals."""
    cen, area, nrm = tri_geometry(v, f)
    vn = vertex_attr(f, len(v), nrm, area)
    X = np.concatenate([v, vn], axis=1)
    out = {}
    done = 0
    for k in sorted(ladder):
        X = heat.smooth(X, k - done)
        done = k
        Ps, Ns = X[:, :3], X[:, 3:]
        Ns = Ns / np.maximum(np.linalg.norm(Ns, axis=1, keepdims=True), 1e-12)
        out[k] = ((v - Ps) * Ns).sum(1)
    return out


def hop_opening(mask, pairs, hops):
    """Morphological opening of a face set on the face graph with a structuring element of `hops` edge steps: keeps the thick parts
    (buckle, belt, patches), drops thin webs and strands. Returns a boolean mask."""
    n = len(mask)
    A = sparse.coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n)).tocsr()
    A = A + A.T
    out_src = np.flatnonzero(~mask)
    d_out = csgraph.dijkstra(A, directed=False, indices=out_src, min_only=True, limit=hops + 1) if len(out_src) else np.full(n, np.inf)
    core = mask & (d_out > hops)
    if not core.any():
        return np.zeros(n, bool)
    Ab = A.multiply(mask[:, None]).multiply(mask[None, :]).tocsr()
    d_core = csgraph.dijkstra(Ab, directed=False, indices=np.flatnonzero(core), min_only=True, limit=hops + 1)
    return mask & (d_core <= hops + 1)


def smooth_faces(x, pairs, iterations):
    """x <- (x + mean of the edge neighbours) / 2, repeated."""
    if not iterations:
        return x
    n = len(x)
    A = sparse.coo_matrix((np.ones(len(pairs) * 2), (np.r_[pairs[:, 0], pairs[:, 1]], np.r_[pairs[:, 1], pairs[:, 0]])), shape=(n, n)).tocsr()
    deg = np.maximum(np.asarray(A.sum(1)).ravel(), 1)
    for _ in range(iterations):
        x = .5 * x + .5 * (A @ x) / deg
    return x


def majority_filter(cls, pairs, iterations):
    """Boolean class per face: a face whose edge neighbours (at least 2 of its 3) all disagree takes their class; removes 1-face spikes."""
    n = len(cls)
    A = sparse.coo_matrix((np.ones(len(pairs) * 2), (np.r_[pairs[:, 0], pairs[:, 1]], np.r_[pairs[:, 1], pairs[:, 0]])), shape=(n, n)).tocsr()
    deg = np.asarray(A.sum(1)).ravel()
    for _ in range(iterations):
        share = (A @ cls.astype(np.float64)) / np.maximum(deg, 1)
        flip = np.where(cls, share < .34, share > .66) & (deg >= 2)
        if not flip.any():
            break
        cls = np.where(flip, ~cls, cls)
    return cls


def face_mean(f, vertex_values):
    return vertex_values[f].mean(1)


# ----------------------------------------------------------------------------------------------- the pass

def fisher(h, w_r, w_c, area):
    """Texture-supervised separation of the two weak classes: median gap over the sum of the (area-weighted) absolute deviations."""
    if w_r.sum() < 20 or w_c.sum() < 20:
        return 0.
    mr, mc = np.median(h[w_r]), np.median(h[w_c])
    dr = np.median(np.abs(h[w_r] - mr))
    dc = np.median(np.abs(h[w_c] - mc))
    return float((mr - mc) / max(dr + dc, 1e-9))


def calibrate(heat_c, f, area, h, w_r, w_c, glob, pseudo=.02):
    """Local ring / centre relief levels by normalised heat convolution: the weak-class weighted mean of h, flowed over the garment
    (numerator and weight flowed alike), falling back to the global level `glob` = (ring, centre) where a class is absent nearby."""
    nv = len(heat_c.mass)
    cols = []
    for w in (w_r, w_c):
        wa = area * w
        cols.append(np.bincount(f.ravel(), weights=np.repeat(h * wa, 3), minlength=nv) / heat_c.mass)
        cols.append(np.bincount(f.ravel(), weights=np.repeat(wa, 3), minlength=nv) / heat_c.mass)
    Y = heat_c.smooth(np.stack(cols, 1), 1)
    m_r = (Y[:, 0] + pseudo * glob[0]) / (Y[:, 1] + pseudo)
    m_c = (Y[:, 2] + pseudo * glob[1]) / (Y[:, 3] + pseudo)
    return m_r, m_c


def ring_pass(v, f, label, ring, centre, params=None, log=print, cache=None):
    """Relief classification of the garment faces. label: int per face (0 base, 1.. roles); ring / centre: role index of the rings /
    of their centres (0: void). Returns a dict (label, kind, h, score, hole_id, ring_id, stats ...), face order of f."""
    p = dict(P)
    p.update(params or {})
    t0 = time.monotonic()
    n = len(f)
    label = np.asarray(label, np.int64)
    garment = label > 0
    cen, area, nrm = tri_geometry(v, f)
    stats = {'faces': int(n), 'garment_faces': int(garment.sum()), 'garment_area': float(area[garment].sum())}
    vs, fs, vid = compact(v, f[garment])
    pairs_all = edge_pairs(f)
    sub_idx = np.flatnonzero(garment)
    a_s = area[garment]
    lab_s = label[garment]
    pairs_s = edge_pairs(fs)
    ns = len(fs)
    log(f'RING garment {ns} faces, {len(vs)} vertices')

    ladder = tuple(p['ladder'])
    cache = Path(cache) if cache else None
    if cache is not None and cache.is_file() and np.load(cache)['n'] == len(vs):
        z = np.load(cache)
        hv = {k: z[f'h{k}'].astype(np.float64) for k in ladder}
        log(f'RING relief ladder from cache {cache}')
    else:
        heat = Heat(vs, fs, p['sigma0'], p['crease_deg'], p['crease_low'])
        hv = relief_ladder(vs, fs, heat, ladder)
        if cache is not None:
            np.savez_compressed(cache, n=len(vs), **{f'h{k}': hv[k].astype(np.float32) for k in ladder})
    log(f'RING relief ladder done {time.monotonic() - t0:.1f}s')
    hf = {k: face_mean(fs, hv[k]) for k in ladder}
    sig = {k: p['sigma0'] * math.sqrt(k) for k in ladder}

    # ---- candidate blobs: large connected blobs of a non-ring class (belt, shoulder patch, buckle, under-skirt, but also a connected
    # network of recessed spaces between rings). Weak classes for the scale / calibration: ring-coloured faces vs the small non-ring blobs.
    non_ring = lab_s != ring
    cand = np.zeros(ns, bool)
    blob = np.full(ns, -1, np.int64)
    nb = 0
    big_factor = p['big_area_factor_void'] if centre == 0 else p['big_area_factor']
    big_default = big_factor * (p['default_sigma'] if p['sigma'] in (None, 'auto') else float(p['sigma'])) ** 2
    for role in np.unique(lab_s[non_ring]):
        m = lab_s == role
        k, comp = components(m, pairs_s, ns)
        if not k:
            continue
        ca = np.bincount(comp[m], weights=a_s[m], minlength=k)
        is_big = m & (ca[np.maximum(comp, 0)] > big_default)
        cand |= is_big
        blob[is_big] = comp[is_big] + nb
        nb += k
    w_r0 = lab_s == ring
    w_c0 = non_ring & ~cand

    # ---- scale
    fish = {k: fisher(hf[k], w_r0, w_c0, a_s) for k in ladder}
    if p['sigma'] in (None, 'auto'):
        best = max(ladder, key=lambda k: fish[k]) if max(fish.values()) > 0 else min(ladder, key=lambda k: abs(sig[k] - p['default_sigma']))
    else:
        best = min(ladder, key=lambda k: abs(sig[k] - float(p['sigma'])))
    h = hf[best]
    stats['scale'] = {'sigma': sig[best], 'steps': best, 'fisher': {f'{sig[k]:.3f}': round(fish[k], 3) for k in ladder}}
    log(f'RING sigma {sig[best]:.3f} fisher ' + json.dumps(stats['scale']['fisher']))

    # ---- protection: a candidate blob as raised as the rings (belt, patch, buckle) or flat (under-skirt) is not a ring centre; a recessed
    # blob (the connected spaces between rings) is one.
    g_r0 = float(np.median(h[w_r0])) if w_r0.any() else 0.
    g_c0 = float(np.median(h[w_c0])) if w_c0.sum() > 20 else g_r0 - 2 * float(np.median(np.abs(h[w_r0] - g_r0)) + 1e-4)
    protect_level = g_c0 + p['protect_frac'] * (g_r0 - g_c0)
    # the raised part of every candidate blob is cut out first (a belt can be joined to a network of recessed gaps), then its connected
    # components that are still large are the protected ones.
    mode = p['protect_mode'] or ('size' if centre == 0 else 'relief')
    raised = cand & (smooth_faces(h, pairs_s, 3) >= protect_level) if mode == 'relief' else cand
    k, comp = components(raised, pairs_s, ns)
    protected = np.zeros(ns, bool)
    blob_info = []
    if k:
        ca = np.bincount(comp[raised], weights=a_s[raised], minlength=k)
        for c_ in np.flatnonzero(ca > big_default):
            m = comp == c_
            if mode == 'size':
                m = hop_opening(m, pairs_s, int(round(p['open_radius'] / max(float(np.sqrt(a_s.mean() * 4 / np.sqrt(3))), 1e-6))))
                if not m.any():
                    continue
            protected |= m
            blob_info.append({'area': float(ca[c_]), 'median_relief': float(np.median(h[m])), 'role': int(np.bincount(lab_s[m]).argmax())})
    w_c0 = non_ring & ~protected
    region = ~protected
    stats['protected_area_share'] = float(a_s[protected].sum() / a_s.sum())
    stats['blobs'] = sorted(blob_info, key=lambda d: -d['area'])[:12]
    stats['protect_level'] = protect_level

    # ---- calibration, two passes
    heat_c = Heat(vs, fs, p['calib_sigma'], p['crease_deg'], p['crease_low'])
    w_r, w_c = w_r0, w_c0
    cls = w_r.copy()
    score = np.zeros(ns)
    valid = np.zeros(ns, bool)
    tex = np.where(lab_s == ring, 1., -1.)
    for it in range(p['passes']):
        g_r = float(np.median(h[w_r])) if w_r.any() else 0.
        g_c = float(np.median(h[w_c])) if w_c.any() else 0.
        m_r_v, m_c_v = calibrate(heat_c, fs, a_s, h, w_r, w_c, (g_r, g_c))
        m_r, m_c = face_mean(fs, m_r_v), face_mean(fs, m_c_v)
        sep = m_r - m_c
        thr = (m_r + m_c) / 2
        valid = sep > p['sep_min']
        score = (h - thr) / np.maximum(sep / 2, p['sep_min'] / 2)
        score = smooth_faces(score, pairs_s, p['score_smooth'])
        cls = (score + p['texture_prior'] * tex) > 0
        cls = majority_filter(cls, pairs_s, p['majority_iterations'])
        w_r = cls & region & valid
        w_c = ~cls & region & valid
        stats[f'pass{it}'] = {'global_ring': g_r, 'global_centre': g_c, 'sep_median': float(np.median(sep[region])),
                              'valid_area_share': float(a_s[valid & region].sum() / max(a_s[region].sum(), 1e-20)),
                              'ring_area_share': float(a_s[cls & region & valid].sum() / max(a_s[region & valid].sum(), 1e-20))}
        log(f'RING pass {it}: sep {np.median(sep[region]):.4f} valid {stats[f"pass{it}"]["valid_area_share"]:.2f}')

    # ---- clean-up of specks, in the valid region only
    act = region & valid
    for it in range(3):
        c_mask = ~cls & act
        k, comp = components(c_mask, pairs_s, ns)
        if not k:
            break
        ca = np.bincount(comp[c_mask], weights=a_s[c_mask], minlength=k)
        big = ca[ca > np.quantile(ca, .5)] if k > 4 else ca
        hole_ref = float(np.median(big)) if len(big) else 0.
        small = c_mask & (ca[np.maximum(comp, 0)] < p['min_hole_share'] * hole_ref)
        r_mask = cls & act
        k2, comp2 = components(r_mask, pairs_s, ns)
        ra = np.bincount(comp2[r_mask], weights=a_s[r_mask], minlength=k2) if k2 else np.zeros(0)
        small_r = r_mask & (ra[np.maximum(comp2, 0)] < p['min_ring_share'] * max(hole_ref, 1e-12))
        if not small.any() and not small_r.any():
            break
        cls = np.where(small, True, cls)
        cls = np.where(small_r, False, cls)
    stats['specks_absorbed_passes'] = it + 1

    # ---- holes and rings
    c_mask = ~cls & act
    kh, hole = components(c_mask, pairs_s, ns)
    ha = np.bincount(hole[c_mask], weights=a_s[c_mask], minlength=kh) if kh else np.zeros(0)
    cs = np.stack([np.bincount(hole[c_mask], weights=cen[garment][c_mask][:, d] * a_s[c_mask], minlength=kh) for d in range(3)], 1) / np.maximum(ha, 1e-20)[:, None]
    med = float(np.median(ha)) if kh else 0.
    ring_id = np.full(ns, -1, np.int64)
    r_mask = cls & act
    if kh:
        dist, nearest = cKDTree(cs).query(cen[garment][r_mask])
        lim = 2.4 * math.sqrt(med / math.pi)
        ring_id[r_mask] = np.where(dist < lim, nearest, -1)
    stats['holes'] = {'count': int(kh), 'median_area': med, 'median_equiv_diameter': float(2 * math.sqrt(med / math.pi)),
                      'area_p10_p90': [float(np.quantile(ha, .1)), float(np.quantile(ha, .9))] if kh else None,
                      'rings_assigned_area_share': float(a_s[ring_id >= 0].sum() / max(a_s[r_mask].sum(), 1e-20))}

    # ---- optional void restriction: only above a height
    apply_z = np.ones(ns, bool)
    zcut = None
    if p['void_above_z'] is not None:
        zcut = p['void_above_z']
        if zcut == 'auto':
            zcut = auto_waist_z(cen[garment], lab_s, a_s, pairs_s, protected, big_default)
        apply_z = cen[garment][:, 2] > zcut
    stats['void_above_z'] = None if zcut is None else float(zcut)

    # ---- new labels
    new_s = lab_s.copy()
    flip_r = r_mask & apply_z
    flip_c = c_mask & apply_z
    new_s[flip_r] = ring
    new_s[flip_c] = centre
    kind_s = np.zeros(ns, np.int8)
    kind_s[protected] = 3
    kind_s[r_mask] = 1
    kind_s[c_mask] = 2
    out = {k_: np.zeros(n, t) for k_, t in (('label', np.int64), ('kind', np.int8), ('h', np.float32), ('score', np.float32),
                                            ('hole_id', np.int64), ('ring_id', np.int64))}
    out['label'][:] = label
    out['label'][sub_idx] = new_s
    out['kind'][sub_idx] = kind_s
    out['h'][sub_idx] = h
    out['score'][sub_idx] = score
    out['hole_id'][:] = -1
    out['hole_id'][sub_idx] = hole
    out['ring_id'][:] = -1
    out['ring_id'][sub_idx] = ring_id
    ch = new_s != lab_s
    stats['changed'] = {'faces': int(ch.sum()), 'area_share_of_garment': float(a_s[ch].sum() / a_s.sum()),
                        'to_ring': int(((new_s == ring) & (lab_s != ring)).sum()), 'ring_area_share_of_garment': float(a_s[ring == new_s].sum() / a_s.sum()),
                        'ring_to_centre': int(((lab_s == ring) & (new_s != ring)).sum()),
                        'texture_agreement_before_area_share': float(a_s[(lab_s == ring) == (kind_s == 1)][act[(lab_s == ring) == (kind_s == 1)]].sum() / max(a_s[act].sum(), 1e-20))}
    stats['seconds'] = round(time.monotonic() - t0, 1)
    out['stats'] = stats
    out['holes_centroids'] = cs
    out['holes_area'] = ha
    return out


def auto_waist_z(cen, lab, area, pairs, protected, big):
    """Top of the belt: the highest point of the largest protected blob of the rarest garment class that has a wide, flat blob
    (the buckle of the chain shirt) or else of the widest protected blob."""
    n = len(lab)
    best, z = 0., None
    for role in np.unique(lab[protected]):
        m = (lab == role) & protected
        k, comp = components(m, pairs, n)
        for c in range(k):
            sel = comp == c
            if area[sel].sum() < big:
                continue
            width = np.ptp(cen[sel, 0])
            zr = np.quantile(cen[sel, 2], .5)
            if .3 < zr < 1.6 and width > best:
                best, z = width, float(np.quantile(cen[sel, 2], .98))
    return z if z is not None else 0.


# ----------------------------------------------------------------------------------------------- module level

def role_index(pal_roles, name):
    if name in ('void', 'base', None):
        return 0
    if name not in pal_roles:
        raise ValueError(f'role {name!r} not in the palette {pal_roles}')
    return list(pal_roles).index(name) + 1


def load_module(module_dir):
    """Inputs of one converted module (read only): high-poly mesh + cleaned labels of wearcut, manifest, palette."""
    import wearcut as wc
    import wearcut_rules as R
    md = Path(module_dir)
    prep = np.load(md / 'wearcut' / 'prep' / 'prep.npz')
    lab = np.load(md / 'wearcut' / 'labels.npz')
    manifest = json.loads((md / 'reduced' / 'manifest.json').read_text(encoding='utf-8-sig'))
    pal_dict = R.palette_dict(manifest)
    pal = wc.palette_from_dict(pal_dict)
    label = lab['label'].astype(np.int64)
    if (md / 'wearcut' / 'feature_labels.npz').is_file():      # a module already rebuilt with a feature pass: analyse the labels before it
        label = np.load(md / 'wearcut' / 'feature_labels.npz')['label_old'].astype(np.int64)
    return {'dir': md, 'v': prep['v'].astype(np.float64), 'f': prep['f'].astype(np.int64), 'label': label,
            'part': prep['part'].astype(np.int64), 'part_names': [str(x) for x in prep['part_names']], 'manifest': manifest,
            'palette_dict': pal_dict, 'palette': pal, 'module': manifest.get('module') or md.name}


def recut_wearable(M, out_dir, label, levels=('0p4', '0p2', '0p1'), params=None, centre='secondary', pass_params=None):
    """Re-run wearcut.run on the module's prep with the rings label hook (the pass is computed again inside the run, the `label`
    argument is only checked against it). Nothing under models/ is written: the prep is read, the results go to out_dir."""
    import wearcut as wc
    import feature_hooks
    md = M['dir']
    plane = json.loads((md / 'wearcut' / 'plane.json').read_text(encoding='utf-8-sig'))
    plane = plane.get('plane', plane)
    pp = dict(pass_params or {})
    z = pp.pop('void_above_z', None)
    spec = 'rings:void:' + ('all' if z is None else str(z)) if centre == 'void' else f'rings:{centre}'
    hook = feature_hooks.make_hook(spec, pp)
    saved = dict(wc.P)
    try:
        r = wc.run(md / 'wearcut' / 'prep', out_dir, M['module'], M['palette_dict'], plane, tuple(levels), True, dict(params or {}, label_hook=hook))
    finally:
        wc.P.clear()
        wc.P.update(saved)
    if not (np.asarray(label, np.int8) == np.load(Path(out_dir) / 'feature_labels.npz')['label_new']).all():
        print('RING recut: the hook labels differ from the given label field (other parameters?)')
    return r


def lod_roles(path, manifest, pal):
    """Labelled / wearable GLB -> (v normalised frame, f, role index per face), glb face order."""
    import wearcut as wc
    gv, gf, names = wc.read_glb(path)
    lut = {n: i for i, n in enumerate(['base', *pal.roles])}
    return wc.from_glb(gv, manifest), gf, np.array([lut.get(str(n), 0) for n in names], np.int64)


def transfer_role_array(v, f, role, hp_cent, hp_area, new_label, kind, K):
    """New role per triangle of a labelled full LOD (v in the normalised frame, role 0 base / 1.. garment): a triangle whose covered
    high-poly area lies mostly (> 50%) in the pass's ring / centre region (kind 1 / 2) takes the majority new label there (0 = void ->
    the triangle becomes base in the full LOD). Returns (new role, owner = covering LOD triangle per high-poly face)."""
    import segment_first as sf
    votes, owner = sf.cover_votes(v[f], hp_cent, hp_area, new_label, K)
    in_reg = (kind == 1) | (kind == 2)
    in_votes = np.zeros((len(f), K))
    np.add.at(in_votes, (owner[in_reg], new_label[in_reg]), hp_area[in_reg])
    cover = in_votes.sum(1) / np.maximum(votes.sum(1), 1e-20)
    sel = (cover > .5) & (role > 0)
    new = role.copy()
    new[sel] = in_votes[sel].argmax(1)
    return new, owner, int(sel.sum())


def relabel_lod(M, ring, kind, new_label, lv, out_dir):
    """Transfer the relabelled high-poly roles to the full labelled LOD of level `lv` (reduced/lod_error_<lv>.glb), see
    transfer_role_array. Writes relabel_lod_<lv>.npz (v, f, role_old, role_new, agreement stats)."""
    md = M['dir']
    v, f, role = lod_roles(md / 'reduced' / f'lod_error_{lv}.glb', M['manifest'], M['palette'])
    hv, hf = M['v'], M['f']
    cen, area, _ = tri_geometry(hv, hf)
    K = len(M['palette'].roles) + 1
    new, owner, n_sel = transfer_role_array(v, f, role, cen, area, new_label, kind, K)
    # agreement with the geometric truth, in hp area: LOD role of the covering face == the high-poly label
    truth = (kind == 1) | (kind == 2)
    agree_old = float(area[truth & (role[owner] == new_label)].sum() / max(area[truth].sum(), 1e-20))
    agree_new = float(area[truth & (new[owner] == new_label)].sum() / max(area[truth].sum(), 1e-20))
    a_lod = tri_geometry(v, f)[1]
    info = {'level': lv, 'faces': int(len(f)), 'faces_changed': int((new != role).sum()), 'area_changed_share': float(a_lod[new != role].sum() / a_lod.sum()),
            'agreement_with_hp_geometry_before': agree_old, 'agreement_with_hp_geometry_after': agree_new,
            'ring_region_faces': n_sel, 'faces_per_hole_estimate': None}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out_dir / f'relabel_lod_{lv}.npz', v=v.astype(np.float32), f=f.astype(np.int32), role_old=role.astype(np.int8), role_new=new.astype(np.int8))
    return info


def surface_agreement(M, label, kind, v, f, role, ring_idx, centre_idx):
    """How well a LOD surface (v, f, role per face; a wearable or a labelled level) reproduces the geometric ring / centre classes of
    the high-poly mesh (hp faces of kind 1 / 2 only, each given to its nearest LOD face): hp area share whose LOD face has the same role,
    the LOD faces whose covered ring-region area is mixed (< 75% one class), the number of connected centre-role patches the LOD still
    resolves and (void) the LOD area lying mostly over gap faces."""
    import segment_first as sf
    hv, hf = M['v'], M['f']
    cen, area, _ = tri_geometry(hv, hf)
    sel = (kind == 1) | (kind == 2)
    tris = v[f]
    votes, owner = sf.cover_votes(tris, cen[sel], area[sel], kind[sel], 4)         # classes 1 ring, 2 centre
    a_lod = tri_geometry(v, f)[1]
    tot = votes.sum(1)
    top = votes[:, 1:3].max(1) / np.maximum(votes[:, 1:3].sum(1), 1e-20)
    covered = votes[:, 1:3].sum(1) > 0
    res = {'faces': int(len(f)), 'area': float(a_lod.sum()), 'covered_faces': int(covered.sum())}
    k_ring = kind[sel] == 1
    a_sel = area[sel]
    if centre_idx > 0:
        want = np.where(k_ring, ring_idx, centre_idx)
        res['hp_area_role_agreement'] = float(a_sel[role[owner] == want].sum() / max(a_sel.sum(), 1e-20))
        res['ring_area_on_ring_role'] = float(a_sel[k_ring & (role[owner] == ring_idx)].sum() / max(a_sel[k_ring].sum(), 1e-20))
        res['centre_area_on_centre_role'] = float(a_sel[~k_ring & (role[owner] == centre_idx)].sum() / max(a_sel[~k_ring].sum(), 1e-20))
        pairs = edge_pairs(f)
        k, comp = components(role == centre_idx, pairs, len(f))
        ca = np.bincount(comp[comp >= 0], weights=a_lod[comp >= 0], minlength=k) if k else np.zeros(0)
        res['centre_patches'] = int((ca > 0).sum())
        res['centre_patches_over_1e-4'] = int((ca > 1e-4).sum())
    else:
        gap_over = covered & (votes[:, 2] > votes[:, 1])
        res['area_share_over_gap'] = float(a_lod[gap_over].sum() / max(a_lod.sum(), 1e-20))
        res['gap_hp_area_share_covered_by_lod'] = None
        import wearcut as wc
        wv, wf, _ = wc.weld(v, f, 2e-5)
        loops = wc.open_edge_loops(wv, wf)
        res['open_boundary_loops'] = int(len(loops))
    res['mixed_area_share'] = float(a_lod[covered & (top < .75)].sum() / max(a_lod[covered].sum(), 1e-20))
    return res


def run_module(module_dir, out_dir, ring='primary', centre='secondary', params=None, levels=('0p4', '0p2', '0p1'), recut=True, relabel=True,
               cache=True, log=print):
    """Everything for one module into out_dir (nothing under models/ is written). Returns the summary dict (also ring_labels.json)."""
    import wearcut_rules as R
    t0 = time.monotonic()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    M = load_module(module_dir)
    roles = M['palette'].roles
    ring_idx, centre_idx = role_index(roles, ring), role_index(roles, centre)
    res = ring_pass(M['v'], M['f'], M['label'], ring_idx, centre_idx, params, log=log, cache=(out_dir / 'cache_relief.npz') if cache else None)
    stats = res.pop('stats')
    cen_pts, hole_area = res.pop('holes_centroids'), res.pop('holes_area')
    np.savez_compressed(out_dir / 'ring_labels.npz', label=res['label'].astype(np.int8), label_texture=M['label'].astype(np.int8), kind=res['kind'],
                        h=res['h'], score=res['score'], hole_id=res['hole_id'].astype(np.int32), ring_id=res['ring_id'].astype(np.int32),
                        hole_centroids=cen_pts.astype(np.float32), hole_area=hole_area.astype(np.float32))
    summary = {'module_dir': str(Path(module_dir)), 'module': M['module'], 'ring': ring, 'centre': centre, 'ring_index': ring_idx,
               'centre_index': centre_idx, 'params': {k: v for k, v in {**P, **(params or {})}.items()}, 'ring_pass': stats, 'wearable': {}, 'lods': {}}
    label, kind = res['label'], res['kind']
    if recut:
        wdir = out_dir / 'wearcut'
        r = recut_wearable(M, wdir, label, levels, None, centre, {k: v_ for k, v_ in (params or {}).items() if k != 'sigma_pct'})
        summary['wearcut'] = {'fallback': r.get('fallback'), 'cut': r['cut'], 'seconds': r.get('seconds')}
        for lv in levels:
            z = np.load(wdir / f'wearcut_lod_{lv}.npz')
            new = surface_agreement(M, label, kind, z['v'].astype(np.float64), z['f'].astype(np.int64), z['role'].astype(np.int64), ring_idx, centre_idx)
            wv, wf, wr = lod_roles(Path(module_dir) / 'reduced' / f'lod_error_{lv}__wearable.glb', M['manifest'], M['palette'])
            old = surface_agreement(M, label, kind, wv, wf, wr, ring_idx, centre_idx)
            summary['wearable'][lv] = {'old': old, 'new': new}
            log(f'RING wearable {lv}: old {old.get("hp_area_role_agreement")} new {new.get("hp_area_role_agreement")}')
    if relabel:
        for lv in levels:
            try:
                info = relabel_lod(M, ring_idx, kind, label, lv, out_dir / 'lods')
            except Exception as e:      # a level without the labelled GLB
                summary['lods'][lv] = {'error': repr(e)}
                continue
            z = np.load(out_dir / 'lods' / f'relabel_lod_{lv}.npz')
            v_, f_ = z['v'].astype(np.float64), z['f'].astype(np.int64)
            info['old'] = surface_agreement(M, label, kind, v_, f_, z['role_old'].astype(np.int64), ring_idx, centre_idx)
            info['new'] = surface_agreement(M, label, kind, v_, f_, z['role_new'].astype(np.int64), ring_idx, centre_idx)
            summary['lods'][lv] = info
    summary['seconds'] = round(time.monotonic() - t0, 1)
    (out_dir / 'ring_labels.json').write_text(json.dumps(jsonable(summary), indent=1) + '\n')
    return summary


def jsonable(x):
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating,)):
        return float(x)
    if isinstance(x, np.ndarray):
        return x.tolist()
    return x


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('module_dir')
    ap.add_argument('out_dir')
    ap.add_argument('--ring', default='primary')
    ap.add_argument('--centre', default='secondary', help="role of the ring centres, or 'void' to remove them (faces labelled base)")
    ap.add_argument('--sigma', default='auto')
    ap.add_argument('--void-above-z', default=None, help="void only above this height (a number or 'auto' = top of the belt / buckle)")
    ap.add_argument('--protect-mode', choices=['relief', 'size'], help='how large non-ring blobs (belt, patches, buckle) are protected; default relief, size for void')
    ap.add_argument('--levels', default='0p4,0p2,0p1')
    ap.add_argument('--no-recut', action='store_true')
    ap.add_argument('--no-relabel-lod', action='store_true')
    ap.add_argument('--no-cache', action='store_true')
    a = ap.parse_args(argv)
    params = {}
    if a.sigma != 'auto':
        params['sigma'] = float(a.sigma)
    if a.protect_mode:
        params['protect_mode'] = a.protect_mode
    if a.void_above_z is not None:
        params['void_above_z'] = a.void_above_z if a.void_above_z == 'auto' else float(a.void_above_z)
    s = run_module(a.module_dir, a.out_dir, a.ring, a.centre, params, tuple(a.levels.split(',')), not a.no_recut, not a.no_relabel_lod, not a.no_cache)
    print('RING_DONE', json.dumps({'holes': s['ring_pass']['holes']['count'], 'changed_area_share': s['ring_pass']['changed']['area_share_of_garment'], 'seconds': s['seconds']}))


if __name__ == '__main__':
    main()
