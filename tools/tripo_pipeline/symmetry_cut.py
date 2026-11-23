"""Symmetry-pooled evidence + clean iso-line cut + exact mirrored pairs (EXPERIMENT 2026-10-24; nothing imports this).

Usage (plain python with numpy + scipy + Pillow, e.g. characters/.venv; needs prep/prep.npz from segment_first_prep.py):
    python tools/tripo_pipeline/symmetry_cut.py <module_dir> <out_dir> [--prep-dir DIR] [--scan-json symmetry.json]
        [--levels 0p4,0p2,0p1] [--pool on|off] [--sigma PCT] [--no-metrics]

<module_dir> is a models/<set>/<job>/<race>/<module> folder (reduced/manifest.json, reduced/*.glb are read, never written).
Writes <out_dir>/proto_lod_<lv>__wearable.glb (+ .npz), current_lod_<lv>.npz (the pipeline's wearable in the same frame),
heat_*.npz, symmetry_cut.json (parameters, pooling statistics, timings, metrics).

What it does, on the HIGH-POLY mesh of segment_first_prep (0.5-1 M faces), reusing segment_first / symmetry / palette:
 A. evidence per face = vote mass of its three texture points per class (segment_first.classify_faces: palette + adaptive hue
    fit on the seen faces) with the concept-camera visibility cosine;
 B. symmetry as a second evidence source: mirror partner of every face across the fitted plane (symmetry.py via the scan's
    symmetry.json), pooled with a weight from LOCAL agreement (geometric closeness x normal agreement x smoothed share of
    well-seen compared pairs that agree, with a global prior where there is no local data); well-seen disagreement blocks
    pooling; a poorly seen side borrows the partner's evidence; never across the module's expected-asymmetric regions;
 C. Potts clean-up of the labels on the high-poly graph, a crease-aware smooth scalar field on the vertices (symmetrised where
    pooling is active), the mesh is CUT along the 0.5 iso-line (triangles split, not deleted), the garment is reduced on its own
    with the cut boundary simplified as a polyline (Douglas-Peucker, locked vertices, Laplacian smoothing) as segment_first does
    for section borders;
 D. one-sided pairs (right gauntlet/boot, outfit footwear): the left piece is the exact reflection of the finished right piece.
"""
import argparse
import json
import math
import struct
import sys
import time
from pathlib import Path

import numpy as np
from scipy import ndimage, sparse
from scipy.sparse import csgraph
from scipy.sparse import linalg as spla
from scipy.spatial import cKDTree

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import face_labels as fl  # noqa: E402
import segment_first as sf  # noqa: E402
import symmetry as sym  # noqa: E402
from palette import Palette, ramp  # noqa: E402

LEVELS = sf.LEVELS
WORKERS = 3

# ---- parameters (one place; recorded in the output json)
P = {
    'seen_cos': fl.GRAZING_COS,        # a face counts as well seen at this best view cosine
    'conf_min': .5,                    # evidence mass for a confident class
    'match_dist': .03,                 # partner centroid within this (normalised units, height 2)
    'match_normal': .5,                # reflected normal . partner normal
    'geo_sigma': .012,                 # geometric closeness: exp(-(point-to-plane distance / sigma)^2)
    'cell': .06,                       # local agreement grid cell (3% of height); box-smoothed over 3x3x3 cells
    'pseudo_area': .006,               # area of well-seen compared faces that outweighs the global prior
    'S_lo': .75, 'S_hi': .95,          # local agreement score at which pooling starts / is full
    'G_lo': .6, 'G_hi': .9,            # global garment agreement mapped to the prior G in [0, 1]
    'borrow_conf': .3,                 # a side with evidence confidence below this "borrows"
    'pool_mode': 'auto',              # 'strict': pooling follows the agreement of the full class; 'split': base/garment mass follows the silhouette only;
                                      # 'auto': strict when the design is asymmetric as a whole (global class agreement prior G = 0), else split
    'potts_lambda': .5, 'bare_cost': 1.2,
    'sigma_pct': .8,                   # field smoothing length (% of height)
    'crease_deg': 25., 'crease_low': .1,
    'iso': .5,
    'min_island_share': 5e-4,
}


# ----------------------------------------------------------------------------------------------- helpers

def jsonable(x):
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, (np.floating, float)):
        return None if not math.isfinite(float(x)) else float(x)
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    if isinstance(x, np.ndarray):
        return jsonable(x.tolist())
    return x


class Clock:
    def __init__(self):
        self.t0 = self.t = time.monotonic()
        self.laps = {}

    def lap(self, key):
        now = time.monotonic()
        self.laps[key] = round(now - self.t, 2)
        self.t = now
        print(f'SYMCUT {key} {self.laps[key]}s', flush=True)

    def total(self):
        self.laps['total_s'] = round(time.monotonic() - self.t0, 2)
        return self.laps


def from_glb(v_glb, meta):
    """Inverse of segment_first.to_glb: the glTF frame (x, z, -y after de-normalisation) -> normalised Blender frame."""
    g = np.asarray(v_glb, np.float64)
    p = np.stack([g[:, 0], -g[:, 2], g[:, 1]], 1)
    q = p @ np.array(meta['rotation_to_review']).T
    return (q - np.array(meta['normalization_origin_rotated'])) * meta['normalization_scale']


def tri_area(v, f):
    return np.linalg.norm(np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]]), axis=1) / 2


def compact(v, f):
    ids, inv = np.unique(f, return_inverse=True)
    return v[ids], inv.reshape(-1, 3)


# Body-part families that are asymmetric by design in a one-sided module, where nothing is ever pooled. Same as symmetry.EXPECTED_ASYMMETRIC
# except the outfit: only its FOOT (the right footwear); the robe hem on the lower legs is symmetric, and a boot shaft or any other one-sided
# garment on the lower leg is still caught by the rule "well seen on both sides and disagree: do not pool".
EXPECTED_ASYMMETRIC = {'outfit': ('foot',)}


def part_tables(names, module):
    """Per body part id: side (0 central, 1 left/+X, 2 right/-X), family, expected-asymmetric flag and the Potts cost of
    garment on that part (module prior: where the base must stay bare)."""
    side = np.array([sym.part_side(n) for n in names])
    fam = [sym.part_family(n) for n in names]
    expected = EXPECTED_ASYMMETRIC.get(module, sym.expected_asymmetric_families(module))
    exp = np.array([f_ in expected for f_ in fam])
    cost = np.zeros(len(names))
    for i, n in enumerate(names):
        if module == 'body_armor':
            cost[i] = P['bare_cost'] if n.startswith(('forearm_stretch.', 'hand.', 'leg_stretch.', 'foot.', 'head.')) else 0.
        elif module == 'right_gauntlet_boot':
            if n in ('forearm_stretch.r', 'hand.r', 'leg_stretch.r', 'foot.r'):
                cost[i] = 0.
            elif n == 'thigh_stretch.r':
                cost[i] = .6
            elif n.endswith('.l'):
                cost[i] = 3.        # the left limb is discarded: the left piece is the reflection of the right one
            else:
                cost[i] = P['bare_cost']
        elif module == 'outfit':
            cost[i] = P['bare_cost'] if n.startswith(('head.', 'hand.')) or n == 'foot.l' else 0.
    return {'side': side, 'family': fam, 'expected': exp, 'cost': cost}


# ----------------------------------------------------------------------------------------------- load

def load_module(module_dir, prep_dir, scan_json):
    module_dir = Path(module_dir)
    manifest = json.loads((module_dir / 'reduced' / 'manifest.json').read_text(encoding='utf-8-sig'))
    z = np.load(Path(prep_dir) / 'prep.npz')
    meta = json.loads((Path(prep_dir) / 'prep.json').read_text(encoding='utf-8-sig'))
    d = dict(manifest['palette'])
    pal = Palette(d, d.get('source', 'manifest'))                  # nominal hues: classify_faces fits its own
    scan = json.loads(Path(scan_json).read_text(encoding='utf-8-sig'))
    plane = sym.Plane(scan['plane']['normal'], scan['plane']['offset'])
    return {'dir': module_dir, 'manifest': manifest, 'module': manifest['module'], 'meta': meta, 'pal': pal, 'scan': scan,
            'plane': plane, 'v': z['v'].astype(np.float64), 'f': z['f'].astype(np.int64), 'colors': z['colors'],
            'part': z['part'].astype(np.int64), 'part_names': [str(n) for n in z['part_names']], 'cos': z['cos'].astype(np.float64)}


# ----------------------------------------------------------------------------------------------- A + B: evidence and pooling

CAT_NAMES = ['no partner', 'pooled (both seen)', 'borrowed (one side poorly seen)', 'weak pooling', 'blocked: well-seen disagreement',
             'blocked: asymmetric neighbourhood', 'blocked: expected-asymmetric region', 'blocked: no local data, asymmetric prior']
CAT_RGB = np.array([(.55, .55, .58), (.20, .72, .30), (.95, .80, .10), (.55, .85, .80), (.88, .10, .10), (.98, .55, .08),
                    (.25, .35, .95), (.70, .25, .85)])


def mirror_partners(M):
    v, f, plane = M['v'], M['f'], M['plane']
    nrm, area = fl.face_normals(v, f)
    cent = v[f].mean(1)
    refl = plane.reflect(cent)
    d_nn, m = cKDTree(cent).query(refl, workers=WORKERS)
    nag = (plane.reflect_vectors(nrm) * nrm[m]).sum(1)
    d_perp = np.abs(((refl - cent[m]) * nrm[m]).sum(1))
    return {'cent': cent, 'nrm': nrm, 'area': area, 'm': m.astype(np.int64), 'd_nn': d_nn, 'nagree': nag, 'd_perp': d_perp}


def local_grid_stats(cent, weights):
    """Sum of `weights` (dict name -> per-face array) on a (cell)-grid, box-smoothed over 3x3x3 cells; returns a lookup function."""
    cell = P['cell']
    lo = cent.min(0) - cell
    idx = np.floor((cent - lo) / cell).astype(np.int64)
    dims = idx.max(0) + 1
    flat = np.ravel_multi_index(idx.T, dims)
    grids = {}
    for k, w in weights.items():
        g = np.bincount(flat, weights=w, minlength=int(np.prod(dims))).reshape(dims)
        grids[k] = ndimage.uniform_filter(g, size=3, mode='constant') * 27     # sum over 3x3x3 cells
    return {k: g.reshape(-1)[flat] for k, g in grids.items()}


def pool_evidence(M, ev, mp, pooling=True):
    """B. Pool the evidence of mirror partners where the neighbourhood is symmetric.

    Per face i with partner m(i): pi_geo = closeness (point-to-plane distance of the reflected centroid to the partner's face plane,
    normal agreement); the local symmetry score S is the smoothed (3x3x3 cells of 3% of the height) share of well-seen, confidently
    classified pairs that AGREE, with `pseudo_area` of the global agreement as prior (so a neighbourhood without any well-seen pair
    follows the whole model's agreement). pi = ramp(S) x pi_geo, 0 where the face itself is well seen on both sides and disagrees,
    0 in the module's expected-asymmetric body parts. Two weights: pi_bin pools the base-vs-garment mass, pi_role the split
    between garment colours. pool_mode 'strict' (default): both follow the agreement of the full class (role included), so a design whose
    colours are asymmetric (random leaves, a diagonal strap) switches all pooling off there; 'split': pi_bin follows only the
    base/garment agreement (silhouette symmetric, trims not).
    Returns (pooled evidence c' p' (n, K), effective view cosine, pi_bin, pi_role, category, info, aux)."""
    n, K = ev.shape
    part, tab = M['part'], M['tab']
    m, area, cos = mp['m'], mp['area'], M['cos']
    E = ev.sum(1)
    vis = np.maximum(ramp(cos, 0, P['seen_cos']), .05)
    conf = vis * E                                                   # evidence confidence of a face
    p = ev / np.maximum(E, 1e-12)[:, None]
    cls = ev.argmax(1)
    cls[E <= 0] = 0
    binv = cls > 0
    matched = (mp['d_nn'] <= P['match_dist']) & (mp['nagree'] >= P['match_normal'])
    seen_both = (cos >= P['seen_cos']) & (cos[m] >= P['seen_cos'])
    conf_both = (E >= P['conf_min']) & (E[m] >= P['conf_min'])
    cmp_ = matched & seen_both & conf_both
    dis_bin = cmp_ & (binv != binv[m])
    dis_any = cmp_ & (cls != cls[m])
    expected = tab['expected'][part] | tab['expected'][part[m]]
    cmp_stat = cmp_ & ~expected                                      # expected-asymmetric regions do not count in the statistics
    garment_pair = cmp_stat & (binv | binv[m])
    n_gp = int(garment_pair.sum())

    def agreement(dis):
        return 1. - float((dis & garment_pair).sum()) / n_gp if n_gp >= 300 else None
    ga_bin, ga_any = agreement(dis_bin), agreement(dis_any)
    prior = lambda g: float(np.clip(((g if g is not None else .75) - P['G_lo']) / (P['G_hi'] - P['G_lo']), 0, 1))
    G_bin, G_any = prior(ga_bin), prior(ga_any)
    loc = local_grid_stats(mp['cent'], {'cmp': area * cmp_stat, 'dis_bin': area * (dis_bin & cmp_stat), 'dis_any': area * (dis_any & cmp_stat)})
    ps = P['pseudo_area']
    S_bin = (loc['cmp'] - loc['dis_bin'] + ps * G_bin) / (loc['cmp'] + ps)
    S_any = (loc['cmp'] - loc['dis_any'] + ps * G_any) / (loc['cmp'] + ps)
    has_local = loc['cmp'] >= ps
    ramp_S = lambda S: np.clip((S - P['S_lo']) / (P['S_hi'] - P['S_lo']), 0, 1)
    geo = np.exp(-(mp['d_perp'] / P['geo_sigma']) ** 2) * np.clip((mp['nagree'] - P['match_normal']) / .4, 0, 1)
    ok = matched & ~expected
    pi_role = ramp_S(S_any) * geo * ok * ~dis_any
    mode = P['pool_mode'] if P['pool_mode'] != 'auto' else ('strict' if G_any <= .05 else 'split')
    if mode == 'split':
        pi_bin = ramp_S(S_bin) * geo * ok * ~dis_bin
    else:
        pi_bin = pi_role
    if not pooling:
        pi_bin = np.zeros(n)
        pi_role = np.zeros(n)
    g = p[:, 1:].sum(1)
    w_b = pi_bin * conf[m]
    den = conf + w_b
    g_new = np.where(den > 0, (conf * g + w_b * g[m]) / np.maximum(den, 1e-12), g)
    r = p[:, 1:] / np.maximum(g, 1e-12)[:, None]
    w_r = conf * g
    w_rm = pi_role * conf[m] * g[m]
    r_new = np.where((w_r + w_rm)[:, None] > 0, (w_r[:, None] * r + w_rm[:, None] * r[m]) / np.maximum(w_r + w_rm, 1e-12)[:, None], r)
    pooled_p = np.concatenate([(1 - g_new)[:, None], g_new[:, None] * r_new], 1)
    conf_new = np.minimum(1., conf + w_b)
    pooled = pooled_p * conf_new[:, None]
    pi = pi_bin
    cos_eff = np.where(pi > .5, np.maximum(cos, cos[m]), cos)
    cat = np.zeros(n, np.int64)
    borrow = (pi > .5) & (conf < P['borrow_conf']) & (conf[m] >= conf)
    blocked = matched & (pi == 0)
    cat[(pi > 0) & (pi <= .5)] = 3
    cat[pi > .5] = 1
    cat[borrow] = 2
    cat[blocked] = 5
    cat[blocked & ~has_local] = 7
    cat[dis_any if mode == 'strict' else dis_bin] = 4
    cat[~matched] = 0
    cat[expected] = 6
    tot = area.sum()
    info = {'pool_mode': mode, 'pool_mode_setting': P['pool_mode'], 'matched_share': float((area * matched).sum() / tot), 'garment_pairs_compared': n_gp,
            'global_garment_agreement_binary': ga_bin, 'global_garment_agreement_role': ga_any, 'global_prior_G_binary': G_bin, 'global_prior_G_role': G_any,
            'well_seen_pair_share': float((area * seen_both).sum() / tot),
            'well_seen_role_disagree_share_of_compared': float((area * dis_any).sum() / max((area * cmp_stat).sum(), 1e-20)),
            'well_seen_binary_disagree_share_of_compared': float((area * dis_bin).sum() / max((area * cmp_stat).sum(), 1e-20)),
            'pool_weight_mean_area': float((area * pi_bin).sum() / tot), 'role_pool_weight_mean_area': float((area * pi_role).sum() / tot),
            'category_area_share': {CAT_NAMES[i]: float(area[cat == i].sum() / tot) for i in range(len(CAT_NAMES))}, 'pooling': bool(pooling)}
    return pooled, cos_eff, pi_bin, pi_role, cat, info, {'conf': conf, 'matched': matched}


# ----------------------------------------------------------------------------------------------- C: labels, field, cut

def potts_cost(evidence, pairs, bare_cost, lam=None, iterations=14, seed=3):
    """segment_first.potts with a per-face cost of garment (module prior). evidence = c * p (data cost c (1 - p[l]))."""
    lam = P['potts_lambda'] if lam is None else lam
    n, K = evidence.shape
    adj = sf.csr_adjacency(pairs, n)
    deg = np.asarray(adj.sum(1)).ravel()
    E = evidence.sum(1)
    unary = E[:, None] - evidence
    unary[:, 1:] += bare_cost[:, None]
    lab = evidence.argmax(1)
    lab = np.where((bare_cost >= 1.) & (lab > 0), 0, lab)
    rng = np.random.default_rng(seed)
    for it in range(iterations):
        onehot = sparse.csr_matrix((np.ones(n, np.float32), (np.arange(n), lab)), shape=(n, K))
        nb = (adj @ onehot).toarray()
        new = (unary + lam * (deg[:, None] - nb)).argmin(1)
        lab = np.where(rng.random(n) < (.5 if it < iterations - 1 else 1.), new, lab)
    return lab


def flood_roles(role, free, pairs, rounds=200):
    """Free faces take the majority GARMENT role of already assigned garment neighbours, ring by ring (base faces never vote)."""
    n = len(role)
    adj = sf.csr_adjacency(pairs, n)
    K = int(role.max()) + 1
    out = role.copy()
    free = free.copy()
    for _ in range(rounds):
        if not free.any():
            break
        known = (~free) & (out > 0)
        onehot = sparse.csr_matrix((known.astype(np.float32), (np.arange(n), out)), shape=(n, K))
        votes = (adj @ onehot).toarray()
        has = free & (votes.sum(1) > 0)
        if not has.any():
            break
        out[has] = votes[has].argmax(1)
        free &= ~has
    return out


def half_edge_twins(f):
    """Twin face of each half-edge (n, 3) (edge opposite corner k of face i is half-edge [i, k]); -1 if boundary or non-manifold."""
    n = len(f)
    a = np.stack([f[:, 1], f[:, 2], f[:, 0]], 1).reshape(-1)
    b = np.stack([f[:, 2], f[:, 0], f[:, 1]], 1).reshape(-1)
    key = np.minimum(a, b).astype(np.int64) * (int(f.max()) + 1) + np.maximum(a, b)
    order = np.argsort(key, kind='stable')
    ks = key[order]
    same = ks[1:] == ks[:-1]
    first = np.flatnonzero(same)
    ok = np.ones(len(first), bool)
    ok[1:] &= first[1:] != first[:-1] + 1
    ok[:-1] &= first[1:] != first[:-1] + 1
    i0, i1 = order[first[ok]], order[first[ok] + 1]
    twin = np.full(3 * n, -1, np.int64)
    twin[i0] = i1 // 3
    twin[i1] = i0 // 3
    return twin.reshape(n, 3)


def smoothing_operator(v, f, nrm, crease_deg, crease_low):
    """Crease-aware cotangent Laplacian: returns (C stiffness (sparse, SPD-ish), M lumped mass vector). Each half-edge weight is
    the cotangent of its opposite corner / 2, clamped >= 0, times a crease factor from the dihedral angle to its twin face."""
    n = len(v)
    tri = v[f]
    e = [tri[:, 2] - tri[:, 1], tri[:, 0] - tri[:, 2], tri[:, 1] - tri[:, 0]]       # edge opposite corner k
    cot = np.zeros((len(f), 3))
    for k in range(3):
        a, b = -e[(k + 1) % 3], e[(k + 2) % 3]                                          # the two edges meeting at corner k
        cross = np.linalg.norm(np.cross(a, b), axis=1)
        cot[:, k] = (a * b).sum(1) / np.maximum(cross, 1e-20)
    twin = half_edge_twins(f)
    ang = np.degrees(np.arccos(np.clip((nrm[:, None, :] * nrm[np.maximum(twin, 0)]).sum(2), -1, 1)))
    cw = np.where(twin >= 0, crease_low + (1 - crease_low) * np.exp(-(ang / crease_deg) ** 2), 1.)
    w = np.clip(cot, 0, 30) * cw / 2
    rows = np.concatenate([f[:, 1], f[:, 2], f[:, 0], f[:, 2], f[:, 0], f[:, 1]])
    cols = np.concatenate([f[:, 2], f[:, 0], f[:, 1], f[:, 1], f[:, 2], f[:, 0]])
    vals = np.concatenate([w[:, 0], w[:, 1], w[:, 2], w[:, 0], w[:, 1], w[:, 2]])
    W = sparse.coo_matrix((vals, (rows, cols)), shape=(n, n)).tocsr()
    C = sparse.diags(np.asarray(W.sum(1)).ravel()) - W
    area = tri_area(v, f)
    mass = np.bincount(f.ravel(), weights=np.repeat(area / 3, 3), minlength=n)
    return C.tocsr(), np.maximum(mass, 1e-12)


def solve_smooth(C, mass, u0, tau):
    """(M + tau C) u = M u0 by preconditioned CG."""
    A = (sparse.diags(mass) + tau * C).tocsr()
    diag = A.diagonal()
    pre = sparse.diags(1. / np.maximum(diag, 1e-20))
    u, info = spla.cg(A, mass * u0, x0=u0, rtol=1e-6, maxiter=400, M=pre)
    return u, info


def face_to_vertex(f, nv, value, weight):
    num = np.bincount(f.ravel(), weights=np.repeat(value * weight, 3), minlength=nv)
    den = np.bincount(f.ravel(), weights=np.repeat(weight, 3), minlength=nv)
    return num / np.maximum(den, 1e-20), den


def vertex_partners(M, pi_f, mp):
    """Mirror partner vertex of every vertex and the vertex pooling weight (area-weighted mean of the faces' pi)."""
    v, f, plane = M['v'], M['f'], M['plane']
    d, mv = cKDTree(v).query(plane.reflect(v), workers=WORKERS)
    pi_v, _ = face_to_vertex(f, len(v), pi_f, mp['area'])
    q = np.exp(-(d / .01) ** 2)
    return mv.astype(np.int64), pi_v * q, d


def cut_mesh(v, f, u, thr=.5):
    """Split triangles along the iso-line u = thr and keep the u >= thr side. Returns (v2, f2, parent face of each new triangle,
    cut vertex mask of v2, uncompacted)."""
    inside = u >= thr
    k = inside[f]
    cnt = k.sum(1)
    full = np.flatnonzero(cnt == 3)
    one = np.flatnonzero(cnt == 1)
    two = np.flatnonzero(cnt == 2)
    nv = len(v)
    # rotate rows: one -> inside vertex first; two -> outside vertex last
    def rotated(ids, first_is_inside):
        rows = f[ids]
        idx = k[ids].argmax(1) if first_is_inside else (np.argmin(k[ids], axis=1) + 1) % 3
        return np.take_along_axis(rows, (idx[:, None] + np.arange(3)[None]) % 3, 1)
    r1, r2 = rotated(one, True), rotated(two, False)
    # edges needing a point: (one) a-b, c-a ; (two) b-c, c-a
    ea = np.concatenate([r1[:, 0], r1[:, 2], r2[:, 1], r2[:, 2]])
    eb = np.concatenate([r1[:, 1], r1[:, 0], r2[:, 2], r2[:, 0]])
    lo, hi = np.minimum(ea, eb), np.maximum(ea, eb)
    key = lo.astype(np.int64) * nv + hi
    ukey, inv = np.unique(key, return_inverse=True)
    ulo, uhi = ukey // nv, ukey % nv
    t = np.clip((thr - u[ulo]) / (u[uhi] - u[ulo]), 1e-3, 1 - 1e-3)      # a crossing exactly at a vertex would make a zero-area triangle
    newv = v[ulo] + t[:, None] * (v[uhi] - v[ulo])
    pid = nv + inv
    n1, n2 = len(one), len(two)
    p_ab, p_ca = pid[:n1], pid[n1:2 * n1]
    p_bc, p_ca2 = pid[2 * n1:2 * n1 + n2], pid[2 * n1 + n2:]
    tris = [f[full],
            np.stack([r1[:, 0], p_ab, p_ca], 1),
            np.stack([r2[:, 0], r2[:, 1], p_bc], 1),
            np.stack([r2[:, 0], p_bc, p_ca2], 1)]
    parent = np.concatenate([full, one, two, two])
    v2 = np.vstack([v, newv])
    cutmask = np.zeros(len(v2), bool)
    cutmask[nv:] = True
    return v2, np.vstack(tris), parent, cutmask


def drop_islands(f, area, min_share):
    pairs = sf.edge_pairs(f)
    g = sparse.coo_matrix((np.ones(len(pairs), np.int8), (pairs[:, 0], pairs[:, 1])), shape=(len(f), len(f)))
    k, comp = csgraph.connected_components(g, directed=False)
    a = np.bincount(comp, weights=area, minlength=k)
    keep = (a >= min_share * area.sum())[comp]
    return keep, {'components': int(k), 'dropped_components': int((a < min_share * area.sum()).sum()), 'dropped_area_share': float(area[~keep].sum() / area.sum())}


# ----------------------------------------------------------------------------------------------- reduction of the garment

def boundary_plan(v, f, eps):
    """Kept boundary vertices (locks) and snap map for the open boundary of the garment: every boundary chain is simplified as a
    polyline (Douglas-Peucker, tolerance eps); like segment_first.border_plan but for the cut boundary (kind 0 edges)."""
    edges, kind = sf.border_edges(f, np.zeros(len(f), np.int64))
    chains, junction = sf.border_chains(edges)
    keep = np.zeros(len(v), bool)
    keep[list(junction)] = True
    snap_to = np.arange(len(v))
    kept = []
    for ch in chains:
        ch = np.array(ch)
        P_ = v[ch]
        closed = len(ch) > 2 and ch[0] == ch[-1]
        if closed:
            mid = int(np.linalg.norm(P_ - P_[0], axis=1).argmax())
            idx = np.union1d(sf.douglas_peucker(P_[:mid + 1], eps), mid + sf.douglas_peucker(P_[mid:], eps))
        else:
            idx = sf.douglas_peucker(P_, eps)
        keep[ch[idx]] = True
        kept.append(ch[idx])
        arc = np.r_[0, np.cumsum(np.linalg.norm(np.diff(P_, axis=0), axis=1))]
        ka = arc[idx]
        inset = set(idx.tolist())
        for j in range(len(ch)):
            if j not in inset:
                snap_to[ch[j]] = ch[idx[np.abs(ka - arc[j]).argmin()]]
    return keep, snap_to, kept, junction, {'chains': len(chains), 'kept_vertices': int(keep.sum()), 'boundary_vertices': int(sum(len(c) for c in chains))}


def reduce_garment(v, f, role, hp_area, fraction, smooth=True):
    """Reduce the whole garment at `fraction` of the height (absolute error 2 x fraction on height 2), cut boundary kept clean.
    Returns (v_out, faces, roles per face, info)."""
    err = 2 * fraction
    lock, snap_to, kept, junction, binfo = boundary_plan(v, f, .5 * err)
    locked = lock.copy()
    red, mesh_err = sf.simplify(v, f, err, locked)
    red = snap_to[red]
    ok = (red[:, 0] != red[:, 1]) & (red[:, 1] != red[:, 2]) & (red[:, 0] != red[:, 2])
    red = red[ok]
    red = red[sf.cancel_pairs_np(v, red)]
    v_out, sinfo = (sf.smooth_border_vertices(v, red, kept, junction, err)) if smooth else (v, {})
    cen = v[f].mean(1)
    tris = v_out[red]
    votes, _ = sf.cover_votes(tris, cen, hp_area, role, int(role.max()) + 1)
    r = votes.argmax(1)
    empty = votes.sum(1) <= 0
    if empty.any() and (~empty).any():
        tc = tris.mean(1)
        r[empty] = r[~empty][cKDTree(tc[~empty]).query(tc[empty])[1]]
    # fix: tiny role 0 would be 'base'; garment roles are >= 1
    r = np.maximum(r, 1)
    ids, inv = np.unique(red, return_inverse=True)
    return v_out[ids], inv.reshape(-1, 3), r, {'boundary': binfo, 'smoothing': sinfo, 'meshopt_error': mesh_err, 'faces': int(len(red))}


# ----------------------------------------------------------------------------------------------- D: exact mirrored pair

def weld(v, f, tol=1e-6):
    tree = cKDTree(v)
    pairs = tree.query_pairs(tol, output_type='ndarray')
    if not len(pairs):
        return v, f, 0
    n = len(v)
    g = sparse.coo_matrix((np.ones(len(pairs), np.int8), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
    k, comp = csgraph.connected_components(g, directed=False)
    first = np.full(k, n)
    np.minimum.at(first, comp, np.arange(n))
    f2 = first[comp][f]
    ok = (f2[:, 0] != f2[:, 1]) & (f2[:, 1] != f2[:, 2]) & (f2[:, 0] != f2[:, 2])
    vv, ff = compact(v, f2[ok])
    return vv, ff, int(n - len(np.unique(first[comp])))


def weld_with_roles(v, f, role, tol):
    """weld() that keeps a role per triangle (triangles that collapse are dropped with their role)."""
    pairs = cKDTree(v).query_pairs(tol, output_type='ndarray')
    n = len(v)
    g = sparse.coo_matrix((np.ones(len(pairs), np.int8), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
    k, comp = csgraph.connected_components(g, directed=False)
    first = np.full(k, n)
    np.minimum.at(first, comp, np.arange(n))
    f2 = first[comp][f]
    ok = (f2[:, 0] != f2[:, 1]) & (f2[:, 1] != f2[:, 2]) & (f2[:, 0] != f2[:, 2])
    vv, ff = compact(v, f2[ok])
    return vv, ff, role[ok]


def mirror_copy(v, f, role, plane):
    """The exact reflection of a piece across `plane`, winding flipped so the normals still face outward."""
    return plane.reflect(v), f[:, ::-1].copy(), role.copy()


def assemble(pieces, tol=1e-6):
    """Concatenate pieces (v, f, role) and weld coincident vertices (a piece that touches the plane is welded to its mirror image)."""
    vs, fs, rs, base = [], [], [], 0
    for v, f, r in pieces:
        vs.append(v)
        fs.append(f + base)
        rs.append(r)
        base += len(v)
    v, f, r = np.vstack(vs), np.vstack(fs), np.concatenate(rs)
    vw, fw, rw = weld_with_roles(v, f, r, tol)
    return vw, fw, rw


# ----------------------------------------------------------------------------------------------- I/O: GLB

def write_glb(path, v_glb, f, role, mat_names, mat_rgb):
    """Minimal glTF 2.0 binary: one primitive per used material (roles are 1-based indices into mat_names/mat_rgb)."""
    path = Path(path)
    chunks, accessors, views, prims = [], [], [], []
    offset = 0

    def add(buf, target):
        nonlocal offset
        pad = (-len(buf)) % 4
        views.append({'buffer': 0, 'byteOffset': offset, 'byteLength': len(buf), 'target': target})
        chunks.append(buf + b'\0' * pad)
        offset += len(buf) + pad
        return len(views) - 1

    pos = np.ascontiguousarray(v_glb, np.float32)
    add(pos.tobytes(), 34962)
    accessors.append({'bufferView': 0, 'componentType': 5126, 'count': len(pos), 'type': 'VEC3',
                      'min': pos.min(0).tolist(), 'max': pos.max(0).tolist()})
    used = sorted(set(int(r) for r in role))
    mats = []
    for r in used:
        idx = np.ascontiguousarray(f[role == r], np.uint32).reshape(-1)
        bv = add(idx.tobytes(), 34963)
        accessors.append({'bufferView': bv, 'componentType': 5125, 'count': len(idx), 'type': 'SCALAR'})
        mats.append({'name': mat_names[r], 'pbrMetallicRoughness': {'baseColorFactor': [*(np.clip(mat_rgb[r], 0, 1) ** 2.2).tolist(), 1.],
                                                                    'metallicFactor': 0., 'roughnessFactor': .9}})
        prims.append({'attributes': {'POSITION': 0}, 'indices': len(accessors) - 1, 'material': len(mats) - 1})
    gltf = {'asset': {'version': '2.0', 'generator': 'symmetry_cut.py'}, 'scene': 0, 'scenes': [{'nodes': [0]}],
            'nodes': [{'mesh': 0}], 'meshes': [{'primitives': prims}], 'materials': mats, 'accessors': accessors,
            'bufferViews': views, 'buffers': [{'byteLength': offset}]}
    js = json.dumps(gltf).encode()
    js += b' ' * ((-len(js)) % 4)
    bin_ = b''.join(chunks)
    total = 12 + 8 + len(js) + 8 + len(bin_)
    path.write_bytes(struct.pack('<4sII', b'glTF', 2, total) + struct.pack('<I4s', len(js), b'JSON') + js +
                     struct.pack('<I4s', len(bin_), b'BIN\0') + bin_)


def read_glb(path):
    """(vertices (glTF frame), triangles, material name per triangle) of a binary glTF written by Blender or write_glb."""
    data = Path(path).read_bytes()
    jlen = struct.unpack_from('<I', data, 12)[0]
    gltf = json.loads(data[20:20 + jlen])
    blen_off = 20 + jlen
    blen = struct.unpack_from('<I', data, blen_off)[0]
    binb = data[blen_off + 8:blen_off + 8 + blen]
    dt = {5126: np.float32, 5125: np.uint32, 5123: np.uint16, 5121: np.uint8}
    nc = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4}

    def acc(i):
        a = gltf['accessors'][i]
        bv = gltf['bufferViews'][a['bufferView']]
        arr = np.frombuffer(binb, dt[a['componentType']], a['count'] * nc[a['type']], bv.get('byteOffset', 0) + a.get('byteOffset', 0))
        return arr.reshape(a['count'], nc[a['type']]) if nc[a['type']] > 1 else arr
    vs, fs, names, base = [], [], [], 0
    for mesh in gltf['meshes']:
        for prim in mesh['primitives']:
            p = acc(prim['attributes']['POSITION']).astype(np.float64)
            idx = acc(prim['indices']).astype(np.int64).reshape(-1, 3) if 'indices' in prim else np.arange(len(p)).reshape(-1, 3)
            vs.append(p)
            fs.append(idx + base)
            names += [gltf['materials'][prim['material']]['name'] if 'material' in prim else 'base'] * len(idx)
            base += len(p)
    return np.vstack(vs), np.vstack(fs), np.array(names)


# ----------------------------------------------------------------------------------------------- metrics

def open_edge_loops(v, f):
    """Chains of the open boundary (edges used by one triangle) as lists of vertex ids."""
    edges, kind = sf.border_edges(f, np.zeros(len(f), np.int64))
    edges = edges[kind == 0]
    if not len(edges):
        return []
    chains, _ = sf.border_chains(edges)
    return chains


def resample(P_, step, closed):
    """Uniform arclength resampling of a polyline."""
    seg = np.linalg.norm(np.diff(P_, axis=0), axis=1)
    arc = np.r_[0, np.cumsum(seg)]
    L = arc[-1]
    if L < step * 3:
        return None, L
    n = max(int(round(L / step)), 4)
    s = np.linspace(0, L, n, endpoint=not closed)
    return np.stack([np.interp(s, arc, P_[:, i]) for i in range(3)], 1), L


def smooth_loop(Q, sigma_samples, closed):
    if closed:
        out = np.stack([ndimage.gaussian_filter1d(Q[:, i], sigma_samples, mode='wrap') for i in range(3)], 1)
    else:
        out = np.stack([ndimage.gaussian_filter1d(Q[:, i], sigma_samples, mode='nearest') for i in range(3)], 1)
    return out


def loop_len(Q, closed):
    d = np.linalg.norm(np.diff(Q, axis=0), axis=1).sum()
    return d + (np.linalg.norm(Q[0] - Q[-1]) if closed else 0.)


def boundary_metrics(v, f, min_len_pct=10., step_pct=.5, sigma_pct=2.):
    """Jaggedness of the open-boundary loops. jag = raw length / length of the 2%-smoothed loop - 1 (length-weighted mean over
    loops); sharp = boundary turns over 50 degrees per 100% of height at 0.5% resampling; also dense boundary points."""
    H = 2.
    chains = open_edge_loops(v, f)
    step, sig = step_pct / 100 * H, sigma_pct / 100 * H
    tot_raw = tot_smooth = tot_len = 0.
    sharp = 0
    turn_sum = 0.
    loops = []
    dense = []
    rough_all = []
    for ch in chains:
        ch = np.array(ch)
        closed = len(ch) > 2 and ch[0] == ch[-1]
        Pc = v[ch][:-1] if closed else v[ch]
        L_raw = loop_len(Pc, closed)
        if L_raw < min_len_pct / 100 * H:
            continue
        Q, _ = resample(v[ch], step, closed)
        if Q is None:
            continue
        Qs = smooth_loop(Q, sig / step, closed)
        Ls = loop_len(Qs, closed)
        rough = np.linalg.norm(smooth_loop(Q, 1.5 / step_pct * 1., closed) - Q, axis=1) * 50.     # % of height, sigma 1.5% of height... in samples
        rough_all.append(rough)
        Lr = loop_len(v[ch][:-1] if closed else v[ch], closed)
        t = np.diff(np.vstack([Q, Q[:1]]) if closed else Q, axis=0)
        t /= np.maximum(np.linalg.norm(t, axis=1, keepdims=True), 1e-12)
        ang = np.degrees(np.arccos(np.clip((t[:-1] * t[1:]).sum(1), -1, 1)))
        # turning over a window of 2 steps removes numerical noise of the resampling
        sharp += int((ang > 50).sum())
        turn_sum += float(ang.sum())
        tot_raw += Lr
        tot_smooth += Ls
        tot_len += L_raw
        loops.append({'length_pct': 100 * L_raw / H, 'jag': max(Lr / Ls - 1, 0.), 'closed': bool(closed),
                      'centre': ((v[ch].mean(0))).tolist()})
        Qd, _ = resample(v[ch], .005, closed)
        dense.append(Qd if Qd is not None else Q)
    out = {'loops': len(loops), 'boundary_length_pct_height': 100 * tot_len / H,
           'jag_excess': max(tot_raw / tot_smooth - 1, 0.) if tot_smooth > 0 else None,
           'sharp_turns_per_100pct': (sharp / (100 * tot_len / H) * 100) if tot_len > 0 else None,
           'turn_deg_per_pct': turn_sum / (100 * tot_len / H) if tot_len > 0 else None,
           'rough_rms_pct': float(np.sqrt(np.mean(np.concatenate(rough_all) ** 2))) if rough_all else None,
           'rough_p95_pct': float(np.quantile(np.concatenate(rough_all), .95)) if rough_all else None, 'per_loop': loops}
    out['dense_per_loop'] = dense
    return out, (np.vstack(dense) if dense else np.zeros((0, 3)))


def mirror_mismatch(dense, plane):
    """Distance (% of height) from the mirror image of every boundary sample to the nearest boundary sample."""
    if len(dense) < 10:
        return None
    d, _ = cKDTree(dense).query(plane.reflect(dense))
    d = d * 50.
    return {'mean': float(d.mean()), 'p50': float(np.median(d)), 'p95': float(np.quantile(d, .95)), 'max': float(d.max()), 'samples': int(len(d))}


def surface_samples(v, f, count, seed=1):
    a = tri_area(v, f)
    rng = np.random.default_rng(seed)
    ids = rng.choice(len(f), count, p=a / a.sum())
    r1, r2 = np.sqrt(rng.random(count)), rng.random(count)
    w = np.stack([1 - r1, r1 * (1 - r2), r1 * r2], 1)
    return np.einsum('ij,ijk->ik', w, v[f[ids]]), ids


def surface_error(v, f, hp_cent):
    """One-sided deviation of the garment LOD from the high-poly surface (% of height): nearest high-poly centroid."""
    pts, _ = surface_samples(v, f, 20000)
    d, _ = cKDTree(hp_cent).query(pts, workers=WORKERS)
    return {'p50_pct': float(np.median(d) * 50), 'p95_pct': float(np.quantile(d, .95) * 50), 'max_pct': float(d.max() * 50)}


# ----------------------------------------------------------------------------------------------- the whole module

def role_index(pal, names):
    lut = {n: i for i, n in enumerate(['base', *pal.roles])}
    return np.array([lut.get(str(n), 0) for n in names], np.int64)


def run_module(module_dir, out_dir, prep_dir, scan_json, levels=('0p4', '0p2', '0p1'), pooling=True, sigma_pct=None, metrics=True):
    clock = Clock()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if sigma_pct is not None:
        P['sigma_pct'] = sigma_pct
    M = load_module(module_dir, prep_dir, scan_json)
    module, v, f = M['module'], M['v'], M['f']
    M['tab'] = tab = part_tables(M['part_names'], module)
    H = 2.
    meta = M['meta']
    clock.lap('load')

    # ---- A. evidence
    role0, ev, vis0, pal, hue_rep = sf.classify_faces(M['pal'], M['colors'], M['cos'])
    K = ev.shape[1]
    clock.lap('A_classify')

    # ---- B. pooling
    mp = mirror_partners(M)
    clock.lap('B_partners')
    pooled, cos_eff, pi, pi_role, cat, pinfo, aux = pool_evidence(M, ev, mp, pooling)
    clock.lap('B_pool')

    # ---- C. labels on the high-poly graph
    pairs, pair_edges = sf.edge_pairs(f, True)
    area = mp['area']
    bare_cost = tab['cost'][M['part']]
    lab = potts_cost(pooled, pairs, bare_cost)
    lab, reflood_info = sf.reflood_unseen(lab, cos_eff, area, pairs)
    lab, absorb_info = sf.absorb_small(lab, area, pairs)
    edge_len = np.linalg.norm(v[pair_edges[:, 0]] - v[pair_edges[:, 1]], axis=1)
    lab, sliver_info = sf.fill_slivers(lab, area, pairs, edge_len, bare_cost >= 1., max_width_pct=1.0)
    clock.lap('C_labels')
    bin_ = (lab > 0).astype(np.float64)
    u0, _ = face_to_vertex(f, len(v), bin_, area)
    sym_info = {}
    if pooling:
        mv, a_v, dv = vertex_partners(M, pi, mp)
        a_v = .5 * a_v
        u0 = (1 - a_v) * u0 + a_v * u0[mv]
        sym_info = {'vertex_pool_weight_mean': float(a_v.mean() * 2), 'vertex_partner_dist_p95_pct': float(np.quantile(dv, .95) * 50)}
    C, mass = smoothing_operator(v, f, mp['nrm'], P['crease_deg'], P['crease_low'])
    h_mean = float(np.sqrt(mass.sum() / len(v)))
    sigma = P['sigma_pct'] / 100 * H
    tau = sigma ** 2 / 2
    u, cg = solve_smooth(C, mass, u0, tau)
    clock.lap('C_field')
    v2, f2, parent, cutmask = cut_mesh(v, f, u, P['iso'])
    keep_isl, isl_info = drop_islands(f2, tri_area(v2, f2), P['min_island_share'])
    f2, parent = f2[keep_isl], parent[keep_isl]
    # roles of the kept high-poly faces: the face's own garment label, or flooded from garment neighbours
    garment_role = lab.copy()
    kept_faces = np.zeros(len(f), bool)
    kept_faces[parent] = True
    free = kept_faces & (lab == 0)
    garment_role = flood_roles(garment_role, free, pairs)
    role_hp = np.maximum(garment_role[parent], 1)
    v3, f3 = compact(v2, f2)
    area3 = tri_area(v3, f3)
    clock.lap('C_cut')
    hp_cent3 = v3[f3].mean(1)

    result = {'module_dir': str(module_dir), 'module': module, 'backend': M['scan'].get('backend'), 'params': dict(P),
              'plane': M['scan']['plane'].get('normal'), 'hue_fit': hue_rep, 'pooling': pinfo, 'symmetrised_field': sym_info,
              'reflood_unseen': reflood_info, 'absorb_small': absorb_info, 'fill_slivers': {k: v_ for k, v_ in sliver_info.items() if k != 'components'},
              'field': {'cg_info': int(cg), 'tau': tau, 'mean_edge_len_pct': 100 * h_mean / H, 'sigma_pct': P['sigma_pct']},
              'cut': {'hp_faces': int(len(f)), 'garment_faces_cut': int(len(f3)), 'islands': isl_info,
                      'garment_area_cut': float(area3.sum()), 'garment_area_label': float(area[lab > 0].sum()),
                      'faces_labelled_garment': int((lab > 0).sum())}, 'levels': {}}

    # ---- D. pair modules: the finished right piece is reflected; here the split of the cut garment into right piece / rest
    pair = split_pair(M, module, v3, f3, parent, area3)
    result['pair'] = pair['info'] if pair else None
    np.savez_compressed(out_dir / 'heat_pooling_faces.npz', cat=cat.astype(np.int8), pi=pi.astype(np.float32), pi_role=pi_role.astype(np.float32))
    # ---- reduce at each level
    pal_srgb = pal.material_srgb()
    mat_names = pal.material_names()
    outputs = {}
    for lv in levels:
        frac = dict(LEVELS)[lv]
        t0 = time.monotonic()
        pieces, rinfo = [], {}
        if pair is None:
            sels = {'garment': np.ones(len(f3), bool)}
        else:
            sels = {k: m_ for k, m_ in (('rest', pair['rest']), ('right', pair['right'])) if m_ is not None and m_.any()}
        for name, sel in sels.items():
            vP, fP = compact(v3, f3[sel])
            vr, fr, rr, info_ = reduce_garment(vP, fP, role_hp[sel], area3[sel], frac)
            rinfo[name] = info_
            pieces.append((vr, fr, rr))
            if name == 'right':
                vm, fm, rm = mirror_copy(vr, fr, rr, M['plane'])
                pieces.append((vm, fm, rm))
                n_r = fl.face_normals(vr, fr)[0]
                n_m = fl.face_normals(vm, fm)[0]
                key = np.sort(np.vstack([fr, fm + len(vr)]), axis=1)
                rinfo['pair_check'] = {'right_faces': int(len(fr)), 'mirrored_faces': int(len(fm)),
                                       'duplicate_faces': int(len(key) - len(np.unique(key, axis=0))),
                                       'normal_reflection_max_error': float(np.abs(n_m - M['plane'].reflect_vectors(n_r)).max()),
                                       'vertex_reflection_max_error': float(np.abs(M['plane'].reflect(vm) - vr).max())}
        vv, ff, role_out = assemble(pieces)
        rinfo['faces'] = int(len(ff))
        outputs[lv] = (vv, ff, role_out)
        rinfo['seconds'] = round(time.monotonic() - t0, 2)
        v_glb = sf.to_glb(vv, meta)
        write_glb(out_dir / f'proto_lod_{lv}__wearable.glb', v_glb, ff, role_out, mat_names, pal_srgb)
        np.savez_compressed(out_dir / f'proto_lod_{lv}.npz', v=vv.astype(np.float32), f=ff.astype(np.int32), role=role_out.astype(np.int8))
        result['levels'][lv] = {'reduction': rinfo}
        print(f'SYMCUT level {lv}: {len(ff)} faces', flush=True)
    clock.lap('C_reduce')

    # ---- full-body LOD (for the pooling heat map) and the current wearable in the same frame
    cur = {}
    for lv in levels:
        for name, suffix in (('current', f'lod_error_{lv}__wearable.glb'), ('full', f'lod_error_{lv}.glb')):
            path = Path(module_dir) / 'reduced' / suffix
            if not path.is_file():
                continue
            gv, gf, gn = read_glb(path)
            nv_ = from_glb(gv, meta)
            # the exporter splits vertices per material / normal: weld them (keeping the role of the surviving triangles)
            rl = role_index(pal, gn)
            vv_, ff_, rl = weld_with_roles(nv_, gf, rl, 2e-5)
            cur[(name, lv)] = (vv_, ff_, rl)
            if name == 'current':
                np.savez_compressed(out_dir / f'current_lod_{lv}.npz', v=vv_.astype(np.float32), f=ff_.astype(np.int32), role=rl.astype(np.int8))
    if ('full', levels[0]) in cur:
        fv, ff_, _ = cur[('full', levels[0])]
        votes, _ = sf.cover_votes(fv[ff_], mp['cent'], area, cat, len(CAT_NAMES))
        lod_cat = votes.argmax(1)
        np.savez_compressed(out_dir / 'heat_pooling_lod.npz', v=fv.astype(np.float32), f=ff_.astype(np.int32), cat=lod_cat.astype(np.int8),
                            rgb=CAT_RGB[lod_cat].astype(np.float32))
    clock.lap('io_current')

    if metrics:
        result['metrics'] = compute_metrics(M, mp, u, garment_role, outputs, cur, levels, pair_left_mask(M, module, mp) if pair is not None else None, out_dir)
        clock.lap('metrics')
    plan_renders(out_dir, module, pal, M['plane'], levels[0])
    result['seconds'] = clock.total()
    (out_dir / 'symmetry_cut.json').write_text(json.dumps(jsonable(result), indent=1) + '\n')
    return result


def split_pair(M, module, v3, f3, parent, area3):
    """Split the cut garment into the RIGHT piece that is reflected to make the pair and the REST that is kept as it is.
    right_gauntlet_boot: right = everything right of the plane (the left limb of the mesh is discarded). outfit: right = the
    connected components that lie (>= 80% of their area) on the right foot / lower leg parts (the right footwear), the same on
    the left are discarded, rest = the robe. Returns None when the module has no pair or no footwear component exists."""
    plane, names = M['plane'], M['part_names']
    cent = v3[f3].mean(1)
    if module == 'right_gauntlet_boot':
        right = plane.signed(cent) < 0
        return {'right': right, 'rest': None, 'info': {'rule': 'right of the plane', 'right_faces': int(right.sum()),
                                                      'discarded_left_faces': int((~right).sum())}}
    if module != 'outfit':
        return None
    pairs = sf.edge_pairs(f3)
    n = len(f3)
    g = sparse.coo_matrix((np.ones(len(pairs), np.int8), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
    k, comp = csgraph.connected_components(g, directed=False)
    part = M['part'][parent]
    ids = lambda side: [i for i, nm in enumerate(names) if nm in (f'foot.{side}', f'leg_stretch.{side}')]
    a_tot = np.bincount(comp, weights=area3, minlength=k)
    frac = {side: np.bincount(comp, weights=area3 * np.isin(part, ids(side)), minlength=k) / np.maximum(a_tot, 1e-20) for side in 'rl'}
    cc = np.stack([np.bincount(comp, weights=cent[:, i] * area3, minlength=k) / np.maximum(a_tot, 1e-20) for i in range(3)], 1)
    side_sign = plane.signed(cc)
    right_c = (frac['r'] >= .8) & (side_sign < 0)
    left_c = (frac['l'] >= .8) & (side_sign > 0)
    info = {'components': int(k), 'right_footwear_components': int(right_c.sum()), 'left_footwear_components_discarded': int(left_c.sum()),
            'right_area_share': float(a_tot[right_c].sum() / a_tot.sum())}
    if not right_c.any():
        info['note'] = 'no separate right footwear component: no mirrored pair'
        return None
    right = right_c[comp]
    rest = ~right & ~left_c[comp]
    info.update(right_faces=int(right.sum()), rest_faces=int(rest.sum()), discarded_left_faces=int(left_c[comp].sum()))
    return {'right': right, 'rest': rest, 'info': info}


def pair_left_mask(M, module, mp):
    """High-poly faces whose label in the prototype is the mirror of the right piece (the left limb / left footwear)."""
    left = M['plane'].signed(mp['cent']) > 0
    if module == 'outfit':
        ids = [i for i, nm in enumerate(M['part_names']) if nm in ('foot.l', 'leg_stretch.l')]
        left &= np.isin(M['part'], ids)
    return left


def compute_metrics(M, mp, u, garment_role, outputs, cur, levels, pair_left, glb_dir=None):
    """Current (pipeline wearable) vs prototype, per level: faces, area, boundary jaggedness, left/right opening mismatch, and the
    share of the high-poly surface (area) whose garment/base/role label differs between the pipeline's labelled LOD and the cut."""
    plane, f, cent, area = M['plane'], M['f'], mp['cent'], mp['area']
    uf = u[f].mean(1)
    proto_lab = np.where(uf >= P['iso'], np.maximum(garment_role, 1), 0)
    if pair_left is not None:                   # the left limb of Tripo is discarded: its label is the mirrored right one
        proto_lab[pair_left] = proto_lab[mp['m'][pair_left]]
    out = {}
    for lv in levels:
        vv, ff, rr = outputs[lv]
        cv, cf, cr = cur[('current', lv)]
        ent = {}
        dense_cur = None
        for name, (v_, f_) in (('current', (cv, cf)), ('proto', (vv, ff))):
            bm, dense = boundary_metrics(v_, f_)
            per_loop = bm['per_loop']
            if len(dense) > 10:
                tree_b = cKDTree(dense)
                for lp, dl in zip(per_loop, bm['dense_per_loop']):
                    lp['mirror_mismatch_mean_pct'] = float(tree_b.query(plane.reflect(dl))[0].mean() * 50)
                    lp['mirror_mismatch_max_pct'] = float(tree_b.query(plane.reflect(dl))[0].max() * 50)
            ent[name] = {'faces': int(len(f_)), 'area': float(tri_area(v_, f_).sum()),
                         'boundary': {k: val for k, val in bm.items() if k not in ('per_loop', 'dense_per_loop')},
                         'per_loop': per_loop, 'mirror_mismatch': mirror_mismatch(dense, plane)}
            if name == 'current':
                dense_cur = dense
        ent['area_ratio_proto_over_current'] = ent['proto']['area'] / max(ent['current']['area'], 1e-20)
        ent['current']['surface_deviation'] = surface_error(cv, cf, cent)
        ent['proto']['surface_deviation'] = surface_error(vv, ff, cent)
        gv_, gf_, _ = read_glb(glb_dir / f'proto_lod_{lv}__wearable.glb') if glb_dir is not None else (None, np.zeros((0, 3)), None)
        ent['proto']['glb_roundtrip_triangles'] = int(len(gf_))
        if ('full', lv) in cur:
            fv, ffu, fr = cur[('full', lv)]
            wp, wi = surface_samples(cv, cf, 100000, seed=6)
            wear_pts, wear_roles = wp, cr[wi]
            pts, ids = surface_samples(fv, ffu, 150000, seed=4)
            _, near = cKDTree(pts).query(cent, workers=WORKERS)
            lab_cur = fr[ids[near]]
            if pair_left is not None:   # the labelled LOD keeps the concept's one-sided gear: take the left from the pipeline's wearable (its mirrored copies)
                left = pair_left
                dwear, iw = cKDTree(wear_pts).query(cent[left], workers=WORKERS)
                lab_cur[left] = np.where(dwear < 2 * dict(LEVELS)[lv] + 1e-3, wear_roles[iw], 0)
            ch = lab_cur != proto_lab
            flip = (lab_cur > 0) != (proto_lab > 0)
            role_only = ch & ~flip
            tot = area.sum()
            far = np.ones(len(cent), bool)
            if dense_cur is not None and len(dense_cur):
                far = cKDTree(dense_cur).query(cent, workers=WORKERS)[0] > .03
            ent['label_change'] = {'changed_area_share': float(area[ch].sum() / tot), 'base_garment_flip_area_share': float(area[flip].sum() / tot),
                                   'role_only_area_share': float(area[role_only].sum() / tot),
                                   'flip_area_share_beyond_1p5pct_of_current_boundary': float(area[flip & far].sum() / tot),
                                   'changed_share_of_current_garment_area': float(area[ch].sum() / max(area[lab_cur > 0].sum(), 1e-20)),
                                   'garment_area_share_current': float(area[lab_cur > 0].sum() / tot),
                                   'garment_area_share_proto': float(area[proto_lab > 0].sum() / tot)}
        out[lv] = ent
    return out


def default_scan_json(module_dir):
    p = Path(module_dir).resolve().parts
    return ROOT / 'experiments/symmetry_scan_20261024' / p[-4] / p[-3] / p[-2] / p[-1] / 'symmetry.json'


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('module_dir')
    ap.add_argument('out_dir')
    ap.add_argument('--prep-dir', help='prep.npz + prep.json (default <module_dir>/segment_first/prep)')
    ap.add_argument('--scan-json', help='symmetry.json of symmetry_scan.py (default experiments/symmetry_scan_20261024/...)')
    ap.add_argument('--levels', default='0p4,0p2,0p1')
    ap.add_argument('--pool', choices=['on', 'off'], default='on')
    ap.add_argument('--sigma', type=float, help='field smoothing length in %% of the height (default 0.8)')
    ap.add_argument('--pool-mode', choices=['strict', 'split', 'auto'], help="strict: all pooling follows full-class agreement; split: base/garment mass follows the silhouette only; auto (default): strict when the design is asymmetric as a whole")
    ap.add_argument('--no-metrics', action='store_true')
    a = ap.parse_args(argv)
    prep = Path(a.prep_dir) if a.prep_dir else Path(a.module_dir) / 'segment_first' / 'prep'
    scan = Path(a.scan_json) if a.scan_json else default_scan_json(a.module_dir)
    if a.pool_mode:
        P['pool_mode'] = a.pool_mode
    run_module(a.module_dir, a.out_dir, prep, scan, tuple(a.levels.split(',')), a.pool == 'on', a.sigma, not a.no_metrics)



# ----------------------------------------------------------------------------------------------- render plan (for symmetry_cut_render.py)

def boundary_polylines(v, f, min_len_pct=3.):
    out = []
    for ch in open_edge_loops(v, f):
        ch = np.array(ch)
        if loop_len(v[ch], False) >= min_len_pct / 50:
            out.append(v[ch])
    return out


def pick_regions(polys, plane, count=4):
    """Group boundary polylines into regions to photograph: a loop and its mirror image share a region (so the pair is framed
    together), a self-symmetric loop (neck) is alone. Returns [{'centre', 'half', 'length'}] largest first."""
    if not polys:
        return []
    cen = np.array([p.mean(0) for p in polys])
    ln = np.array([loop_len(p, False) for p in polys])
    used = np.zeros(len(polys), bool)
    regions = []
    for i in np.argsort(-ln):
        if used[i]:
            continue
        members = [i]
        used[i] = True
        # loops of nearly the same place (current + prototype, or pieces of one opening)
        for j in range(len(polys)):
            if not used[j] and np.linalg.norm(cen[j] - cen[i]) < .3:
                members.append(j)
                used[j] = True
        mc = plane.reflect(cen[i])
        if np.linalg.norm(mc - cen[i]) > .25:
            for j in range(len(polys)):
                if not used[j] and np.linalg.norm(cen[j] - mc) < .3:
                    members.append(j)
                    used[j] = True
        pts = np.vstack([polys[j] for j in members])
        lo, hi = pts.min(0), pts.max(0)
        c = (lo + hi) / 2
        half = max((hi[0] - lo[0]) / 2, (hi[2] - lo[2]) / 2, .12) * 1.35
        regions.append({'centre': c.tolist(), 'half': float(half), 'length': float(sum(ln[j] for j in members))})
    regions.sort(key=lambda r: -r['length'])
    return regions[:count]


def plan_renders(out_dir, module, pal, plane, lv='0p4'):
    out_dir = Path(out_dir)
    cur = np.load(out_dir / f'current_lod_{lv}.npz')
    pro = np.load(out_dir / f'proto_lod_{lv}.npz')
    cv, cf = cur['v'].astype(np.float64), cur['f'].astype(np.int64)
    pv, pf = pro['v'].astype(np.float64), pro['f'].astype(np.int64)
    pc, pp = boundary_polylines(cv, cf), boundary_polylines(pv, pf)
    regions = pick_regions(pc + pp, plane)
    arrays = {f'cur_b{i}': p for i, p in enumerate(pc)}
    arrays.update({f'pro_b{i}': p for i, p in enumerate(pp)})
    np.savez_compressed(out_dir / 'render_plan.npz', **arrays)
    plan = {'level': lv, 'module': module, 'regions': regions, 'plane': {'normal': plane.n.tolist(), 'offset': plane.d},
            'roles_rgb': pal.material_srgb().tolist(), 'n_cur': len(pc), 'n_pro': len(pp)}
    (out_dir / 'render_plan.json').write_text(json.dumps(plan, indent=1) + '\n')
    return plan


if __name__ == '__main__':
    main()
