"""Wearable by cut: symmetry-pooled evidence, off-body / hem-riser rules, a clean iso-line cut and exact mirrored pairs (2026-10-24).

Integration of the approved prototype symmetry_cut.py (experiments/symmetry_cut_20261024). Plain python with numpy + scipy
(characters/.venv), no Blender; the Blender side (building prep.npz, fitting the mirror plane, exporting the wearable GLBs and the
sidecar arrays) is wearcut_blender.py, the module rules shared by both are wearcut_rules.py.

    python tools/tripo_pipeline/wearcut.py <prep_dir> <out_dir> --module M --palette palette.json --plane plane.json
        [--levels 0p4,0p2,0p1] [--pool on|off] [--pool-mode auto|strict|split] [--no-riser] [--no-off-body] [--keep-flaps]

prep_dir: prep.npz + prep.json (wearcut_blender.build_prep: the cleaned high-poly mesh of reduce_model with its three texture
colours per face, body part, concept-camera cosine, and the off-body distance / base-normal agreement per face).
palette.json: the palette the REDUCTION used (manifest `palette`, including its `hue_shift`), so the garment roles are the pipeline's:
the hues are not fitted again here. plane.json: the fitted mirror plane (`normal`, `offset`; symmetry.fit_plane).

What it does on the HIGH-POLY mesh (0.5-1 M faces):
 A. evidence per face = vote mass of its three texture points per class (segment_first.classify_faces with the given palette) x the
    concept-camera visibility cosine; the off-body rule (face_labels.off_body_excess at high-poly level: a face clearly outside the
    fitted base body beyond its part's clearance, where the module lets garment be, cannot be base) lowers the base cost;
 B. symmetry as a second evidence source: the mirror partner of every face across the fitted plane, pooled with a weight from LOCAL
    agreement, blocked where well-seen partners disagree and in the module's expected-asymmetric regions (see pool_evidence);
 C. Potts clean-up on the high-poly graph, the hem-riser rule (base faces that face along the limb next to garment become garment),
    a crease-aware smooth vertex field, the mesh is CUT along its 0.5 iso-line (triangles split, no whole-face deletion), the garment
    is reduced on its own (locked, smoothed open boundary), single-edge flap triangles are dropped;
 D. one-sided pairs (right gauntlet/boot, outfit footwear): the left piece is the exact reflection of the finished right piece. An outfit
    whose right footwear is not a separate component (a shoe joined to the hose) gets the foot part cut off at the ankle (the 0.5 iso-line
    of the smoothed `foot.r` zone indicator) and reflected; the right side stays one continuous garment (`footwear`: component | ankle_cut | none).
Per level it writes wearcut_lod_<lv>.npz (v, f in the normalised frame, role 1..K, part id, source, mirrored per face), labels.npz (the
cleaned high-poly labels) and wearcut.json (every number: pooling statistics, rules, cut, reduction, timings).

Opt-in feature label pass (`--feature-labels SPEC` / `params['label_hook']`, feature_hooks.py): studs painted tertiary keep their
colour, chain-mail ring centres get their own role or are removed. It runs on the final high-poly labels right before the cut field
is built; without it nothing changes. feature_labels.npz (label_old / label_new per face + the pass's arrays) and feature_labels.json
are written beside labels.npz, whose `label` is then the label after the pass.
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
import wearcut_rules as R  # noqa: E402
from palette import Palette, hsv, ramp  # noqa: E402

LEVELS = sf.LEVELS
WORKERS = 3
HUE_UNUSABLE_MARGIN = .10       # a hue shift is dropped when it leaves this much more saturated garment-zone colour unclassified
SOURCES = fl.SOURCES              # texture, symmetry, prior, neighbours, off_body, riser, mirrored
FOOT_COVER_MIN = .2               # share of a foot zone's surface the cut garment must cover for the foot to count as shod / covered
ANKLE_SIGMA_PCT = 1.0             # smoothing length (% of height) of the foot-zone indicator whose 0.5 iso-line is the ankle cut
ANKLE_MIN_FACES = 20              # a foot piece smaller than this is not a shoe
ANKLE_AREA_RATIO = (.5, 2.0)      # foot piece area / garment area on the foot.r zone that the cut must keep (a cut far off the zone fails)

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
    'pool_mode': 'auto',               # 'strict': pooling follows the agreement of the full class; 'split': base/garment mass follows the silhouette only;
                                       # 'auto': strict when the design is asymmetric as a whole (global class agreement prior G = 0), else split
    'potts_lambda': .5, 'bare_cost': R.BARE_COST,
    'off_body': True,                  # high-poly off-body rule (needs off_dist / off_thr in the prep)
    'off_penalty': fl.OFF_BODY_PENALTY,  # base cost of a face fully outside the fitted body (clear texture = 1)
    'riser': True,                     # high-poly hem-riser rule
    'riser_rings': 6,                  # the low-poly rule runs 2 rings of ~1.5% of the height; high-poly faces are ~0.25% wide
    'riser_min_angle': fl.RISER_MIN_ANGLE,
    'sigma_pct': .8,                   # field smoothing length (% of height)
    'crease_deg': 25., 'crease_low': .1,
    'iso': .5,
    'min_island_share': 5e-4,
    'flaps': True, 'flap_min_height': .06, 'flap_passes': 4,   # split_pieces FLAP_DEFAULTS
    'max_plane_tilt_deg': 10.,
    'hue_fit': False,                  # experiment: fit the hues here (the prototype did) instead of using the reduction's palette as it is
    'save_cut': False,                 # debug: write cut_mesh.npz (the cut high-poly garment with role / part / source per face)
    'label_hook': None,                # opt-in feature label pass (feature_hooks.make_hook: studs / rings); None = labels exactly as before
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
        print(f'WEARCUT {key} {self.laps[key]}s', flush=True)

    def total(self):
        self.laps['total_s'] = round(time.monotonic() - self.t0, 2)
        return self.laps


from_glb = R.from_glb


def tri_area(v, f):
    return np.linalg.norm(np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]]), axis=1) / 2


def compact(v, f):
    ids, inv = np.unique(f, return_inverse=True)
    return v[ids], inv.reshape(-1, 3)


part_tables = R.part_tables


# ----------------------------------------------------------------------------------------------- load

def load_prep(prep_dir):
    """prep.npz + prep.json of wearcut_blender.build_prep (or segment_first_prep.py: no off-body arrays, that rule is then off)."""
    prep_dir = Path(prep_dir)
    meta = json.loads((prep_dir / 'prep.json').read_text(encoding='utf-8-sig'))
    with np.load(prep_dir / 'prep.npz') as z:       # closed again: an open npz handle blocks deleting/replacing the file on Windows
        n = len(z['f'])
        return {'meta': meta, 'v': z['v'].astype(np.float64), 'f': z['f'].astype(np.int64), 'colors': z['colors'],
                'part': z['part'].astype(np.int64), 'part_names': [str(x) for x in z['part_names']], 'cos': z['cos'].astype(np.float64),
                'off_dist': z['off_dist'].astype(np.float64) if 'off_dist' in z.files else None,
                'off_thr': z['off_thr'].astype(np.float64) if 'off_thr' in z.files else None,
                'base_agree': z['base_agree'].astype(np.float64) if 'base_agree' in z.files else np.ones(n)}


def palette_from_dict(d):
    """The palette a reduction used (manifest `palette`, `hue_shift` included) exactly as it classified there."""
    d = dict(d)
    pal = Palette(d, d.get('source', 'manifest'))
    pal.separable = bool(d.get('separable'))
    shifts = d.get('hue_shift') or {}
    return pal.with_hue_shifts(shifts) if shifts else pal


# ----------------------------------------------------------------------------------------------- A + B: evidence and pooling

def check_hue_shift(pal, colors, allowed, margin=HUE_UNUSABLE_MARGIN):
    """The reduction's hue shifts (Palette.fit_hues on its 0.4% level samples; they can rest on a weak texture peak) move a colour's
    window of +-20 degrees; on the high-poly colours of the module's garment zones a shift that leaves clearly more saturated pixels
    unclassified than the nominal palette does (a green window moved off most of the green) is not used here: the nominal hues are.
    Returns (palette to use, record)."""
    shifts = dict(pal.hue_shift or {})
    if not shifts:
        return pal, {'hue_shift': {}, 'used': 'nominal hues (the reduction fitted none)'}
    col = colors[allowed].reshape(-1, 3).astype(np.float64)
    if len(col) > 600000:
        col = col[np.random.default_rng(1).choice(len(col), 600000, replace=False)]
    _, sat, val = hsv(col)
    keep = (sat > .3) & (val > .2)
    nominal = pal.with_hue_shifts({})

    def unusable(p):
        role, _ = p.classify(col)
        return float((role[keep] == p.unusable).mean()) if keep.any() else 0.
    u_shift, u_nom = unusable(pal), unusable(nominal)
    rec = {'hue_shift': shifts, 'unusable_share_with_shift': u_shift, 'unusable_share_nominal': u_nom}
    if u_shift > u_nom + margin:
        rec['used'] = "nominal hues: the reduction's shift leaves much more of the garment colour unclassified at high-poly level"
        return nominal, rec
    rec['used'] = "the reduction's hue shift"
    return pal, rec


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

def potts_cost(evidence, pairs, bare_cost, base_cost=None, lam=None, iterations=14, seed=3):
    """segment_first.potts with a per-face cost of garment (module prior `bare_cost`) and of base (`base_cost`: the off-body rule,
    a face clearly outside the fitted body cannot be bare skin). evidence = c * p (data cost c (1 - p[l]))."""
    lam = P['potts_lambda'] if lam is None else lam
    n, K = evidence.shape
    adj = sf.csr_adjacency(pairs, n)
    deg = np.asarray(adj.sum(1)).ravel()
    E = evidence.sum(1)
    unary = E[:, None] - evidence
    unary[:, 1:] += bare_cost[:, None]
    lab = evidence.argmax(1)
    lab = np.where((bare_cost >= 1.) & (lab > 0), 0, lab)
    if base_cost is not None:
        unary[:, 0] += base_cost
        off = (base_cost > .5 * P['off_penalty']) & (lab == 0)
        if off.any():         # start the flipped faces on their best garment class (or the first one: neighbours decide)
            g = evidence[:, 1:].argmax(1) + 1
            lab = np.where(off, g, lab)
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

# ----------------------------------------------------------------------------------------------- rules at high-poly level

def hem_risers_hp(lab, cand, pairs, rings, classes):
    """face_labels.hem_risers on the high-poly graph: a base face that shares an edge with a garment face, where the module allows
    garment and whose normal differs from the nearest fitted-base normal by more than `riser_min_angle` (`cand`: it faces along the
    limb, not out of it, e.g. the underside of a hem Tripo painted red) takes the garment role most of its garment neighbours have.
    At most `rings` rounds, each only next to garment faces (high-poly faces are ~0.25% of the height wide, so the low-poly rule's
    two rings of ~1.5% become `riser_rings`). Returns (labels, flipped mask)."""
    n = len(lab)
    adj = sf.csr_adjacency(pairs, n)
    lab = lab.copy()
    flipped = np.zeros(n, bool)
    for _ in range(rings):
        wear = lab > 0
        onehot = sparse.csr_matrix((np.ones(int(wear.sum()), np.float32), (np.flatnonzero(wear), lab[wear])), shape=(n, classes))
        votes = (adj @ onehot).toarray()
        move = (lab == 0) & cand & (votes[:, 1:].sum(1) > 0)
        if not move.any():
            break
        lab[move] = votes[move][:, 1:].argmax(1) + 1
        flipped |= move
    return lab, flipped


def prune_flaps(v, f, min_height=.12, passes=4):
    """split_pieces.prune_flaps without its Level object: a triangle hanging off the rest of the surface by ONE edge (or by none)
    whose free tip stands more than `min_height` (normalised units; 6% of the body height 2) off that edge is a flap of the
    reduction, not garment. Removing a leaf face opens no hole; chains are peeled leaf by leaf, up to `passes`.
    Returns (dropped mask, number of dropped faces per pass)."""
    n = len(f)
    dropped = np.zeros(n, bool)
    per_pass = []
    for _ in range(passes):
        ids = np.flatnonzero(~dropped)
        fa = f[ids]
        if not len(fa):
            break
        pairs, edges = sf.edge_pairs(fa, True)
        deg = np.bincount(pairs.ravel(), minlength=len(fa))
        leaf = np.flatnonzero(deg <= 1)
        if not len(leaf):
            break
        height = np.zeros(len(fa))
        iso = leaf[deg[leaf] == 0]
        if len(iso):
            t = v[fa[iso]]
            height[iso] = np.max([np.linalg.norm(t[:, a] - t[:, b], axis=1) for a, b in ((0, 1), (1, 2), (2, 0))], axis=0)
        one = leaf[deg[leaf] == 1]
        if len(one):
            row_of = np.full(len(fa), -1, np.int64)
            rows = np.arange(len(pairs))
            row_of[pairs[:, 0]] = rows
            row_of[pairs[:, 1]] = rows
            e = edges[row_of[one]]
            tri = fa[one]
            free = np.where((tri != e[:, :1]) & (tri != e[:, 1:]), tri, -1).max(1)
            ok = ((tri != e[:, :1]) & (tri != e[:, 1:])).sum(1) == 1
            a, b = v[e[:, 0]], v[e[:, 1]]
            ab = b - a
            foot = a + ab * (((v[free] - a) * ab).sum(1) / np.maximum((ab * ab).sum(1), 1e-20))[:, None]
            height[one] = np.where(ok, np.linalg.norm(v[free] - foot, axis=1), 0.)
        bad = leaf[height[leaf] > min_height]
        if not len(bad):
            break
        dropped[ids[bad]] = True
        per_pass.append(int(len(bad)))
    return dropped, per_pass


def face_sources(lab, own_arg, pooled_arg, pi, cat, bare, off_flip, riser):
    """Which evidence decided each high-poly face (face_labels.SOURCES): texture by default; neighbours when the label differs from
    the face's own best class (or it had no evidence); symmetry when the partner's evidence decided or filled in; prior when the
    module's bare zone forced base; off_body / riser for the rules above."""
    src = np.zeros(len(lab), np.int8)
    src[lab != own_arg] = SOURCES.index('neighbours')
    src[(pi > .5) & (lab == pooled_arg) & ((lab != own_arg) | (cat == 2))] = SOURCES.index('symmetry')
    src[bare & (lab == 0) & (own_arg > 0)] = SOURCES.index('prior')
    src[off_flip] = SOURCES.index('off_body')
    src[riser] = SOURCES.index('riser')
    return src


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


def weld_with_attrs(v, f, tol, **attrs):
    """weld() that keeps per-triangle attributes (triangles that collapse are dropped with theirs). -> v, f, {name: array}."""
    pairs = cKDTree(v).query_pairs(tol, output_type='ndarray')
    n = len(v)
    g = sparse.coo_matrix((np.ones(len(pairs), np.int8), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
    k, comp = csgraph.connected_components(g, directed=False)
    first = np.full(k, n)
    np.minimum.at(first, comp, np.arange(n))
    f2 = first[comp][f]
    ok = (f2[:, 0] != f2[:, 1]) & (f2[:, 1] != f2[:, 2]) & (f2[:, 0] != f2[:, 2])
    vv, ff = compact(v, f2[ok])
    return vv, ff, {k_: a_[ok] for k_, a_ in attrs.items()}


def weld_with_roles(v, f, role, tol):
    """weld_with_attrs with a single role array."""
    vv, ff, at = weld_with_attrs(v, f, tol, role=role)
    return vv, ff, at['role']


def mirror_copy(v, f, plane):
    """The exact reflection of a piece across `plane`, winding flipped so the normals still face outward."""
    return plane.reflect(v), f[:, ::-1].copy()


def drop_duplicate_faces(v, f, **attrs):
    """Opposite-wound duplicate triangle pairs vanish (segment_first.cancel_pairs_np), other duplicates keep their first face: the
    glTF exporter would merge them silently and the per-face arrays would no longer match the file."""
    keep = sf.cancel_pairs_np(v, f)
    f = f[keep]
    attrs = {k: a[keep] for k, a in attrs.items()}
    _, first = np.unique(np.sort(f, axis=1), axis=0, return_index=True)
    mask = np.zeros(len(f), bool)
    mask[first] = True
    return f[mask], {k: a[mask] for k, a in attrs.items()}


def assemble(pieces, tol=1e-6):
    """Concatenate pieces (v, f, {attr: per-face array}) and weld coincident vertices (a piece that touches the plane is welded
    to its mirror image); duplicate triangles are dropped. -> v, f, {attr: array}."""
    vs, fs, base = [], [], 0
    for v, f, _ in pieces:
        vs.append(v)
        fs.append(f + base)
        base += len(v)
    keys = list(pieces[0][2])
    attrs = {k: np.concatenate([p[2][k] for p in pieces]) for k in keys}
    vv, ff, at = weld_with_attrs(np.vstack(vs), np.vstack(fs), tol, **attrs)
    ff, at = drop_duplicate_faces(vv, ff, **at)
    vv, ff2 = compact(vv, ff)
    return vv, ff2, at


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


read_glb = R.read_glb    # numpy-only copy shared with the Blender side (wearcut_rules.py)

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



# ----------------------------------------------------------------------------------------------- reduction of the garment

def reduce_garment(v, f, role, part, src, hp_area, fraction, flaps=None, n_parts=None):
    """Reduce the whole garment piece at `fraction` of the height (absolute error 2 x fraction on height 2), cut boundary kept clean.
    role / part / src: per face of (v, f) (the cut high-poly faces); every reduced face takes the garment role, body part and label
    source holding most of the high-poly area it covers. flaps: None or dict(min_height, passes) to drop single-edge flaps.
    Returns (v_out, faces, {role, part, src}, info)."""
    err = 2 * fraction
    lock, snap_to, kept, junction, binfo = boundary_plan(v, f, .5 * err)
    red, mesh_err = sf.simplify(v, f, err, lock.copy())
    red = snap_to[red]
    ok = (red[:, 0] != red[:, 1]) & (red[:, 1] != red[:, 2]) & (red[:, 0] != red[:, 2])
    red = red[ok]
    red = red[sf.cancel_pairs_np(v, red)]
    finfo = {}
    if flaps:
        dropped, per_pass = prune_flaps(v, red, **flaps)
        finfo = {'dropped_flaps': int(dropped.sum()), 'per_pass': per_pass,
                 'dropped_area_share': float(tri_area(v, red)[dropped].sum() / max(tri_area(v, red).sum(), 1e-20))}
        red = red[~dropped]
    v_out, sinfo = sf.smooth_border_vertices(v, red, kept, junction, err)
    cen = v[f].mean(1)
    tris = v_out[red]
    K = int(role.max()) + 1
    votes, tri_of = sf.cover_votes(tris, cen, hp_area, role, K)
    empty = votes.sum(1) <= 0
    near = None
    if empty.any() and (~empty).any():
        tc = tris.mean(1)
        near = np.flatnonzero(~empty)[cKDTree(tc[~empty]).query(tc[empty])[1]]
    out = {}
    for name, values, count in (('role', role, K), ('part', part, n_parts or int(part.max()) + 1), ('src', src, len(SOURCES))):
        vt = votes if name == 'role' else np.zeros((len(tris), count))
        if name != 'role':
            np.add.at(vt, (tri_of, values), hp_area)
        r = vt.argmax(1)
        if near is not None:
            r[empty] = r[near]
        out[name] = r
    out['role'] = np.maximum(out['role'], 1)           # a garment role is >= 1 (0 would be base)
    ids, inv = np.unique(red, return_inverse=True)
    return v_out[ids], inv.reshape(-1, 3), out, {'boundary': binfo, 'smoothing': sinfo, 'flaps': finfo, 'meshopt_error': mesh_err,
                                                  'faces': int(len(red))}


# ----------------------------------------------------------------------------------------------- D: pairs

def split_pair(M, module, v3, f3, parent, area3):
    """Split the cut garment into the RIGHT piece that is reflected to make the pair and the REST that is kept as it is.
    right_gauntlet_boot: right = everything right of the plane (the left limb of the mesh is discarded). outfit: right = the
    connected components that lie (>= 80% of their area) on the right foot / lower leg parts (the right footwear), the same on
    the left are discarded, rest = the robe. Returns (pair or None, info): None when the module has no pair; for an outfit
    whose footwear is not a separate component the info says so (`status` 'no_pair_found') and the caller tries ankle_foot_piece."""
    plane, names = M['plane'], M['part_names']
    cent = v3[f3].mean(1)
    if module == 'right_gauntlet_boot':
        right = plane.signed(cent) < 0
        return ({'right': right, 'rest': None},
                {'status': 'pair', 'rule': 'right of the plane', 'right_faces': int(right.sum()), 'discarded_left_faces': int((~right).sum())})
    if module != 'outfit':
        return None, {'status': 'no_pair_module'}
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
    info = {'status': 'pair', 'components': int(k), 'right_footwear_components': int(right_c.sum()),
            'left_footwear_components_discarded': int(left_c.sum()), 'right_area_share': float(a_tot[right_c].sum() / a_tot.sum())}
    if not right_c.any():
        info.update(status='no_pair_found', note='no separate right footwear component: no mirrored pair')
        return None, info
    right = right_c[comp]
    rest = ~right & ~left_c[comp]
    info.update(right_faces=int(right.sum()), rest_faces=int(rest.sum()), discarded_left_faces=int(left_c[comp].sum()))
    return {'right': right, 'rest': rest}, info


def ankle_foot_piece(M, v3, f3, parent, area3, area, role_hp, part3, src3):
    """Outfit whose right footwear is NOT a separate component (a shoe drawn joined to the hose / trouser leg): cut the foot part off
    the garment for the mirror. The right garment is left as it is; only a copy of this piece is reflected onto the bare left foot.
    The cut is the 0.5 iso-line of the smoothed indicator of the right foot zone (`foot.r` body part, i.e. the ankle of the fitted base)
    on the cut garment, the same machinery as the garment cut itself (triangles split, clean loop). Nothing is added when the right foot
    has no garment (barefoot design) or the left foot already has (>= FOOT_COVER_MIN of its zone); the cut is a failure (-> `faces`)
    when the piece is not plausible. Returns (piece or None, info): info['status'] 'ankle_cut' | 'none' (reason in info) | 'failed'."""
    names, plane = M['part_names'], M['plane']
    ids = {n: i for i, n in enumerate(names)}
    if 'foot.r' not in ids or 'foot.l' not in ids:
        return None, {'status': 'failed', 'reason': 'the base has no foot.r / foot.l body part'}
    fr, fleft = ids['foot.r'], ids['foot.l']
    zone = {s: float(area[M['part'] == i].sum()) for s, i in (('r', fr), ('l', fleft))}
    wear = {s: float(area3[part3 == i].sum()) for s, i in (('r', fr), ('l', fleft))}
    cover = {s: wear[s] / max(zone[s], 1e-20) for s in 'rl'}
    info = {'foot_zone_area': zone, 'garment_area_on_foot': wear, 'foot_cover_share': cover, 'cover_min': FOOT_COVER_MIN}
    if cover['r'] < FOOT_COVER_MIN:
        return None, dict(info, status='none', reason='barefoot: the right foot has no garment')
    if cover['l'] >= FOOT_COVER_MIN:
        return None, dict(info, status='none', reason='the left foot is already covered')
    nv = len(v3)
    nrm3 = fl.face_normals(v3, f3)[0]
    u0, _ = face_to_vertex(f3, nv, (part3 == fr).astype(np.float64), area3)
    C, mass = smoothing_operator(v3, f3, nrm3, P['crease_deg'], P['crease_low'])
    sigma = ANKLE_SIGMA_PCT / 100 * 2.
    u, cg = solve_smooth(C, mass, u0, sigma ** 2 / 2)
    v4, f4, parent4, _ = cut_mesh(v3, f3, u, .5)
    if not len(f4):
        return None, dict(info, status='failed', reason='the ankle iso-line leaves no foot piece')
    keep, isl = drop_islands(f4, tri_area(v4, f4), P['min_island_share'])
    f4, parent4 = f4[keep], parent4[keep]
    vp, fp = compact(v4, f4)
    ap = tri_area(vp, fp)
    ratio = float(ap.sum() / max(wear['r'], 1e-20))
    right_share = float(ap[plane.signed(vp[fp].mean(1)) < 0].sum() / max(ap.sum(), 1e-20))
    open_edges = len(open_edge_loops(vp, fp))
    info.update(cg_info=int(cg), piece_faces=int(len(fp)), piece_area=float(ap.sum()), piece_area_over_foot_garment=ratio, piece_right_share=right_share,
                piece_open_loops=int(open_edges), islands=isl, garment_area_kept_on_right=float(area3.sum()))
    if len(fp) < ANKLE_MIN_FACES:
        return None, dict(info, status='failed', reason=f'the ankle cut leaves a foot piece of {len(fp)} faces')
    if not ANKLE_AREA_RATIO[0] <= ratio <= ANKLE_AREA_RATIO[1]:
        return None, dict(info, status='failed', reason=f'the ankle cut piece covers {ratio:.2f} x the garment area on the foot zone')
    if right_share < .9:
        return None, dict(info, status='failed', reason=f'only {100 * right_share:.0f}% of the ankle cut piece is on the right of the plane')
    if ap.sum() >= .95 * area3.sum() or open_edges == 0:
        return None, dict(info, status='failed', reason='the ankle cut did not separate a foot from the rest of the garment')
    piece = {'v': vp, 'f': fp, 'role': role_hp[parent4], 'part': part3[parent4], 'src': src3[parent4], 'area': ap}
    return piece, dict(info, status='ankle_cut')


def reflect_piece(vr, fr, at, plane, mirror_id):
    """The exact mirrored copy of a reduced right piece (v, f, attributes) across `plane` and its pair check record."""
    vm, fm = mirror_copy(vr, fr, plane)
    am = {'role': at['role'].copy(), 'part': mirror_id[at['part']], 'src': np.full(len(fr), SOURCES.index('mirrored')),
          'mirrored': np.ones(len(fr), np.int64)}
    n_r = fl.face_normals(vr, fr)[0]
    n_m = fl.face_normals(vm, fm)[0]
    key = np.sort(np.vstack([fr, fm + len(vr)]), axis=1)
    check = {'right_faces': int(len(fr)), 'mirrored_faces': int(len(fm)), 'duplicate_faces': int(len(key) - len(np.unique(key, axis=0))),
             'normal_reflection_max_error': float(np.abs(n_m - plane.reflect_vectors(n_r)).max()),
             'vertex_reflection_max_error': float(np.abs(plane.reflect(vm) - vr).max())}
    return (vm, fm, am), check


# ----------------------------------------------------------------------------------------------- the whole module

def run(prep_dir, out_dir, module, palette, plane, levels=('0p4', '0p2', '0p1'), pooling=True, params=None, save_labels=True):
    """Wearable by cut of one module. prep_dir: wearcut_blender.build_prep output; palette: the reduction's palette dict (manifest
    `palette`); plane: symmetry.Plane or its dict. Writes wearcut_lod_<lv>.npz / labels.npz / wearcut.json to out_dir.
    Returns the result dict (`fallback`: a reason when the wearable must not be used, e.g. an outfit with no separable footwear)."""
    params = dict(params or {})
    hook = params.pop('label_hook', P['label_hook'])      # scoped to this run, never left in P
    if params:
        P.update(params)
    clock = Clock()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    prep = load_prep(prep_dir)
    meta = prep['meta']
    pal = palette_from_dict(palette)
    if isinstance(plane, dict):
        plane = sym.Plane(plane['normal'], plane['offset'])
    tilt = plane.tilt_deg
    if tilt > P['max_plane_tilt_deg']:
        raise ValueError(f'mirror plane tilted {tilt:.1f} degrees from X (max {P["max_plane_tilt_deg"]}): fit not trusted')
    M = dict(prep, plane=plane, module=module)
    M['tab'] = tab = part_tables(M['part_names'], module)
    v, f, part, H = M['v'], M['f'], M['part'], 2.
    clock.lap('load')

    # ---- A. evidence (the reduction's palette, hues as it fitted them: nothing is fitted again here)
    pal, hue_info = check_hue_shift(pal, M['colors'], tab['allowed'][part])
    role0, ev, vis0, pal, hue_rep = sf.classify_faces(pal, M['colors'], M['cos'], hue_fit=P['hue_fit'])
    hue_info.update(fitted_here=bool(P['hue_fit']), fit=hue_rep or None, palette_scheme=pal.scheme)
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
    allowed = tab['allowed'][part]
    bare_cost = tab['cost'][part]
    excess = np.zeros(len(f))
    off_info = {'enabled': False}
    if P['off_body'] and M['off_dist'] is not None and M['off_thr'] is not None:
        excess = np.clip((M['off_dist'] - M['off_thr']) / fl.OFF_BODY_RAMP, 0, 1) * allowed
        off_info = {'enabled': True, 'faces_beyond_threshold': int((excess > 0).sum()), 'area_share_beyond_threshold': float(area[excess > 0].sum() / area.sum())}
    base_cost = P['off_penalty'] * excess if (excess > 0).any() else None
    lab = potts_cost(pooled, pairs, bare_cost, base_cost)
    pooled_arg = np.where(pooled.sum(1) > 0, pooled.argmax(1), -1)
    off_flip = (excess > .5) & (lab > 0) & (pooled_arg == 0)
    off_info.update(faces_flipped_from_base=int(off_flip.sum()), area_share_flipped=float(area[off_flip].sum() / area.sum()))
    lab, reflood_info = sf.reflood_unseen(lab, cos_eff, area, pairs)
    lab, absorb_info = sf.absorb_small(lab, area, pairs)
    edge_len = np.linalg.norm(v[pair_edges[:, 0]] - v[pair_edges[:, 1]], axis=1)
    lab, sliver_info = sf.fill_slivers(lab, area, pairs, edge_len, bare_cost >= 1., max_width_pct=1.0)
    riser = np.zeros(len(f), bool)
    riser_info = {'enabled': False}
    if P['riser'] and M['off_dist'] is not None and (M['base_agree'] < 1).any():
        cand = allowed & (M['base_agree'] < math.cos(math.radians(P['riser_min_angle'])))
        lab, riser = hem_risers_hp(lab, cand, pairs, P['riser_rings'], K)
        riser_info = {'enabled': True, 'rings': P['riser_rings'], 'candidate_faces': int(cand.sum()), 'faces_flipped': int(riser.sum()),
                      'area_share_flipped': float(area[riser].sum() / area.sum())}
    own_arg = np.where(ev.sum(1) > 0, ev.argmax(1), -1)
    # opt-in feature pass on the final high-poly labels (studs / ring centres): garment roles change, rings may also void faces (label 0);
    # the sources stay those of the labels before the pass (it is geometry evidence, not a texture/prior decision), so the
    # low-evidence review share does not move. No hook: nothing here runs.
    lab_pre, hook_res, field_sigma_pct = lab, None, P['sigma_pct']
    if hook is not None:
        lab_pre = lab.copy()
        hook_res = hook(dict(v=v, f=f, lab=lab_pre, area=area, part=part, part_names=M['part_names'], ev=ev, pal=pal, colors=M['colors'],
                             cos=M['cos'], module=module, out_dir=out_dir, log=print))
        lab = np.asarray(hook_res['lab'], lab.dtype)
        if lab.shape != lab_pre.shape or ((lab_pre == 0) & (lab != 0)).any():
            raise ValueError('label hook must return one label per face and never turn base faces into garment')
        field_sigma_pct = hook_res.get('params', {}).get('sigma_pct', field_sigma_pct)
    src_hp = face_sources(lab_pre, own_arg, pooled_arg, pi, cat, bare_cost >= 1., off_flip, riser)
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
    sigma = field_sigma_pct / 100 * H
    tau = sigma ** 2 / 2
    u, cg = solve_smooth(C, mass, u0, tau)
    clock.lap('C_field')
    v2, f2, parent, cutmask = cut_mesh(v, f, u, P['iso'])
    keep_isl, isl_info = drop_islands(f2, tri_area(v2, f2), P['min_island_share'])
    f2, parent = f2[keep_isl], parent[keep_isl]
    # roles of the kept high-poly faces: the face's own garment label, or flooded from garment neighbours
    kept_faces = np.zeros(len(f), bool)
    kept_faces[parent] = True
    garment_role = flood_roles(lab.copy(), kept_faces & (lab == 0), pairs)
    role_hp = np.maximum(garment_role[parent], 1)
    v3, f3 = compact(v2, f2)
    area3 = tri_area(v3, f3)
    part3, src3 = part[parent], src_hp[parent].astype(np.int64)
    clock.lap('C_cut')
    if P['save_cut']:
        np.savez_compressed(out_dir / 'cut_mesh.npz', v=v3.astype(np.float32), f=f3.astype(np.int32), role=role_hp.astype(np.int8), part=part3.astype(np.int16), src=src3.astype(np.int8))

    result = {'module': module, 'params': {k: v_ for k, v_ in P.items() if k != 'label_hook'}, 'plane': plane.to_dict(), 'hue': hue_info, 'pooling': pinfo, 'symmetrised_field': sym_info,
              'off_body': off_info, 'riser': riser_info, 'reflood_unseen': reflood_info, 'absorb_small': absorb_info,
              'fill_slivers': {k: v_ for k, v_ in sliver_info.items() if k != 'components'},
              'field': {'cg_info': int(cg), 'tau': tau, 'mean_edge_len_pct': 100 * h_mean / H, 'sigma_pct': field_sigma_pct},
              'cut': {'hp_faces': int(len(f)), 'garment_faces_cut': int(len(f3)), 'islands': isl_info,
                      'garment_area_cut': float(area3.sum()), 'garment_area_label': float(area[lab > 0].sum()),
                      'faces_labelled_garment': int((lab > 0).sum())},
              'label_source_area_share': {nm: float(area[src_hp == i].sum() / area.sum()) for i, nm in enumerate(SOURCES) if (src_hp == i).any()},
              'levels': {}, 'fallback': None, 'footwear': None}
    if hook is None:                                  # a plain re-run must not leave a feature pass's files behind
        for stale in ('feature_labels.npz', 'feature_labels.json'):
            (out_dir / stale).unlink(missing_ok=True)
    if hook_res is not None:
        result['feature_labels'] = {'spec': getattr(hook, 'spec', None), **jsonable(hook_res.get('summary') or {})}
        np.savez_compressed(out_dir / 'feature_labels.npz', label_old=lab_pre.astype(np.int8), label_new=lab.astype(np.int8),
                            **{k: np.asarray(a) for k, a in (hook_res.get('arrays') or {}).items()})
        (out_dir / 'feature_labels.json').write_text(json.dumps(jsonable({'spec': getattr(hook, 'spec', None), **(hook_res.get('info') or {})}), indent=1) + '\n')
    if save_labels:
        np.savez_compressed(out_dir / 'labels.npz', label=lab.astype(np.int8), source=src_hp.astype(np.int8), part=part.astype(np.int16),
                            area=area.astype(np.float32), part_names=np.array(M['part_names']), source_names=np.array(SOURCES))

    # ---- D. pair modules: the finished right piece is reflected; here the split of the cut garment into right piece / rest
    pair, pair_info = split_pair(M, module, v3, f3, parent, area3)
    result['pair'] = pair_info
    foot = None
    if module == 'outfit':
        if pair is not None:
            result['footwear'] = 'component'
        else:
            # the right footwear is joined to the leg garment: cut the foot part off at the ankle for the mirror (the right side is unchanged)
            foot, ankle = ankle_foot_piece(M, v3, f3, parent, area3, area, role_hp, part3, src3)
            pair_info['ankle'] = ankle
            if ankle['status'] == 'ankle_cut':
                result['footwear'] = 'ankle_cut'
                pair_info.update(status='ankle_cut', note='no separate right footwear component: the foot part is cut off at the ankle and reflected')
            elif ankle['status'] == 'none':
                result['footwear'] = 'none'
                pair_info.update(status='no_footwear_mirror', note=ankle['reason'])
            else:
                result['fallback'] = ('outfit: no separate right footwear component and no ankle cut (' + ankle['reason'] +
                                      '), so no exact left/right pair')
    mirror_id = R.mirror_part_ids(M['part_names'])
    # ---- reduce at each level
    flaps = {'min_height': P['flap_min_height'] * H, 'passes': P['flap_passes']} if P['flaps'] else None
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
            vr, fr, at, info_ = reduce_garment(vP, fP, role_hp[sel], part3[sel], src3[sel], area3[sel], frac, flaps, len(M['part_names']))
            at['mirrored'] = np.zeros(len(fr), np.int64)
            rinfo[name] = info_
            pieces.append((vr, fr, at))
            if name == 'right':
                mirrored_piece, rinfo['pair_check'] = reflect_piece(vr, fr, at, plane, mirror_id)
                pieces.append(mirrored_piece)
        if foot is not None:
            # the garment above is the whole (continuous) right side; the foot piece is reduced on its own and only its reflection is added
            vr, fr, at, info_ = reduce_garment(foot['v'], foot['f'], foot['role'], foot['part'], foot['src'], foot['area'], frac, flaps,
                                               len(M['part_names']))
            at['mirrored'] = np.zeros(len(fr), np.int64)
            rinfo['foot'] = info_
            mirrored_piece, rinfo['pair_check'] = reflect_piece(vr, fr, at, plane, mirror_id)
            pieces.append(mirrored_piece)
        vv, ff, at = assemble(pieces)
        a_lv = tri_area(vv, ff)
        low_ev = np.isin(at['src'], [SOURCES.index('prior'), SOURCES.index('neighbours')]) & (at['mirrored'] == 0)
        rinfo.update(faces=int(len(ff)), area=float(a_lv.sum()), mirrored_faces=int(at['mirrored'].sum()),
                     low_evidence_area_share=float(a_lv[low_ev].sum() / max(a_lv.sum(), 1e-20)), seconds=round(time.monotonic() - t0, 2))
        np.savez_compressed(out_dir / f'wearcut_lod_{lv}.npz', v=vv.astype(np.float32), f=ff.astype(np.int32), role=at['role'].astype(np.int8),
                            part=at['part'].astype(np.int16), src=at['src'].astype(np.int8), mirrored=at['mirrored'].astype(np.int8),
                            part_names=np.array(M['part_names']), source_names=np.array(SOURCES))
        result['levels'][lv] = rinfo
        print(f'WEARCUT level {lv}: {len(ff)} faces', flush=True)
    clock.lap('C_reduce')
    result['seconds'] = clock.total()
    (out_dir / 'wearcut.json').write_text(json.dumps(jsonable(result), indent=1) + '\n')
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('prep_dir')
    ap.add_argument('out_dir')
    ap.add_argument('--module', required=True, choices=['body_armor', 'right_gauntlet_boot', 'outfit'])
    ap.add_argument('--palette', required=True, help='palette dict json of the reduction (manifest `palette`, hue_shift included)')
    ap.add_argument('--plane', required=True, help='json with `normal` and `offset` (or a symmetry.json / manifest-like with a `plane` block)')
    ap.add_argument('--levels', default='0p4,0p2,0p1')
    ap.add_argument('--pool', choices=['on', 'off'], default='on')
    ap.add_argument('--pool-mode', choices=['strict', 'split', 'auto'])
    ap.add_argument('--sigma', type=float, help='field smoothing length in %% of the height (default 0.8)')
    ap.add_argument('--no-off-body', action='store_true')
    ap.add_argument('--no-riser', action='store_true')
    ap.add_argument('--keep-flaps', action='store_true')
    ap.add_argument('--feature-labels', metavar='SPEC', help='opt-in label pass on the high-poly labels before the cut: studs | rings:secondary | rings:void '
                    '(feature_hooks.py; writes feature_labels.npz / .json beside labels.npz)')
    a = ap.parse_args(argv)
    params = {}
    if a.feature_labels:
        import feature_hooks
        params['label_hook'] = feature_hooks.make_hook(a.feature_labels)
    if a.pool_mode:
        params['pool_mode'] = a.pool_mode
    if a.sigma is not None:
        params['sigma_pct'] = a.sigma
    if a.no_off_body:
        params['off_body'] = False
    if a.no_riser:
        params['riser'] = False
    if a.keep_flaps:
        params['flaps'] = False
    pl = json.loads(Path(a.plane).read_text(encoding='utf-8-sig'))
    pl = pl.get('plane', pl)
    result = run(a.prep_dir, a.out_dir, a.module, json.loads(Path(a.palette).read_text(encoding='utf-8-sig')), pl, tuple(a.levels.split(',')), a.pool == 'on', params)
    if result['fallback']:
        print('WEARCUT_FALLBACK', result['fallback'], flush=True)
    print('WEARCUT_DONE', json.dumps({k: v_.get('faces') for k, v_ in result['levels'].items()}), flush=True)


if __name__ == '__main__':
    main()
