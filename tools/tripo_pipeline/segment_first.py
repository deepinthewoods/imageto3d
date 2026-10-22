"""Segment first, simplify second: the default rigid-piece split for piece-colour plate armour (convert_job.py runs it after
the reduction of a `piece_colours` body_armor job; split_pieces.py --rigid regions/auto on the per-face reduction is the fallback;
reduce_model.py is untouched).

Usage (plain Python with numpy + scipy + Pillow, e.g. the characters/.venv; needs prep.npz from segment_first_prep.py):
    python tools/tripo_pipeline/segment_first.py <concept_dir> <out_dir> [--reduced DIR] [--source GLB] [--palette DIR|JSON]
          [--levels 0p4,0p2,0p1] [--render] [--no-blend] [--overlap] [--no-cross-joint] [--prep-dir DIR]
          [--no-fill-slivers] [--sliver-width PCT] [--no-smooth-cuts] [--cut-band PCT] [--no-smooth-border]
        <concept_dir> holds reduced/manifest.json (the reduction's output; a real job passes its models/<set>/<job>/<race>/<module>
        folder, which also holds source.glb; an experiment folder keeps the GLB in tripo/source.glb). --reduced points at another
        reduced/ folder, --source is only recorded. <out_dir>/prep/ (or --prep-dir) holds prep.npz + prep.json; the palette is the one
        prep.json recorded (the reduction's own), unless --palette says otherwise. Defaults are the approved run (seam fix on,
        cross-joint shelf on, overlap off, levels 0p4,0p2,0p1, piece blends but no renders: --render adds pieces_render.png;
        --no-render is accepted and does nothing).

Why: reduce_model.py reduces the whole geometry colour-blind and labels each low-poly face afterwards, so 4-10% of the
wear area sits in mixed faces on section borders and the right edges do not exist in the LOD. Here:
 1. every HIGH-POLY face is classified from the texture (palette + adaptive hue fit on the seen faces);
 2. the labels are cleaned on the high-poly face graph (Potts/ICM with texture confidence x concept-view visibility as
    data term; unseen-dominated regions are re-flooded from seen faces), base/anchor faces are removed and the wearable is
    cut into sections with split_pieces.sections_regions on the high-poly graph (body-part zones, enclosure, spanning);
 3. each section is simplified ON ITS OWN at the same absolute error levels (meshoptimizer via ctypes, the error is
    measured against the whole model's normalised height 2): the section borders are first simplified as polylines
    (Douglas-Peucker, tolerance = half the level error) and the kept border vertices are locked in both neighbouring
    sections; border vertices that are not kept are merged into the kept neighbour, so neighbouring sections share exactly
    the same border polyline (and split_pieces' cut/overlap/pose code works unchanged);
 4. each reduced face takes the garment role holding most of the high-poly area it covers.
Seam clean-up (on by default, each step has a switch): thin base slivers between garment faces are filled before the segmentation (fill_slivers, they would
be holes in the armour); the cuts between sections are straightened by minimum cuts on the high-poly graph (mincut_sections: shortest path in a band around the
colour boundary, preferring folds); the kept border polylines are smoothed after the reduction (smooth_border_vertices). seam_metrics.py measures the result.
Between 2 and 3 the sections get the cross-joint fix (split_pieces.reassign_cross_joint, in build_sections): fringe patches that hang beyond
their joint (e.g. the chest-coloured skirt below the waist pivot) move to the section on that side of the joint; --no-cross-joint skips it.
Overlap strips are off by default (--overlap adds them).
"""
import argparse
import ctypes as C
import json
import sys
import time
from pathlib import Path

import numpy as np
from scipy import sparse
from scipy.sparse import csgraph
from scipy.spatial import cKDTree

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import seam_metrics  # noqa: E402
import split_pieces as sp  # noqa: E402
from palette import Palette, hsv, ramp  # noqa: E402

ROOT = HERE.parents[1]
LIB = C.CDLL(str(ROOT / 'tools/lowpoly/lib/libmeshoptimizer.so'))
_SIMPLIFY = LIB.meshopt_simplifyWithAttributes
_P, _S = C.c_void_p, C.c_size_t
_SIMPLIFY.argtypes = [_P, _P, _S, _P, _S, _S, _P, _S, _P, _S, _P, _S, C.c_float, C.c_uint, _P]
_SIMPLIFY.restype = _S
LEVELS = [('0p4', .004), ('0p2', .002), ('0p1', .001)]


# ----------------------------------------------------------------------------------------------- frames / helpers

def to_glb(v_norm, meta):
    """Normalised (Blender, Z up) frame -> the frame of the wearable GLBs: de-normalise like reduce_model.export_native,
    then the glTF exporter's Z-up -> Y-up (x, z, -y)."""
    ns = meta['normalization_scale']
    p = (np.asarray(v_norm, float) / ns + np.array(meta['normalization_origin_rotated'])) @ np.array(meta['rotation_to_review'])
    return np.stack([p[:, 0], p[:, 2], -p[:, 1]], 1)


def edge_pairs(f, with_edges=False):
    """(E,2) face pairs sharing an edge (manifold edges only: exactly two faces), numpy only. with_edges: also the (E,2) vertex pair
    of each shared edge."""
    n = len(f)
    a = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
    b = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
    face = np.tile(np.arange(n), 3)
    key = np.minimum(a, b).astype(np.int64) * (int(f.max()) + 1) + np.maximum(a, b)
    order = np.argsort(key, kind='stable')
    key, face = key[order], face[order]
    ea, eb = a[order], b[order]
    same = key[1:] == key[:-1]
    first = np.flatnonzero(same)
    ok = np.ones(len(first), bool)
    ok[1:] &= first[1:] != first[:-1] + 1            # the middle of a 3+ run is not a manifold pair
    ok[:-1] &= first[1:] != first[:-1] + 1
    pairs = np.stack([face[first[ok]], face[first[ok] + 1]], 1)
    if with_edges:
        return pairs, np.stack([ea[first[ok]], eb[first[ok]]], 1)
    return pairs


def csr_adjacency(pairs, n):
    data = np.ones(len(pairs) * 2, np.float32)
    rows = np.concatenate([pairs[:, 0], pairs[:, 1]])
    cols = np.concatenate([pairs[:, 1], pairs[:, 0]])
    return sparse.csr_matrix((data, (rows, cols)), shape=(n, n))


def fast_components(self, mask=None, same=None):
    """scipy twin of split_pieces.Graph.components (same contract: ids -1 outside `mask`)."""
    p = self.pairs
    n = self.n
    if len(p):
        keep = np.ones(len(p), bool)
        if mask is not None:
            keep &= mask[p[:, 0]] & mask[p[:, 1]]
        if same is not None:
            keep &= same[p[:, 0]] == same[p[:, 1]]
        q = p[keep]
    else:
        q = p
    g = sparse.coo_matrix((np.ones(len(q), np.int8), (q[:, 0], q[:, 1])), shape=(n, n))
    k, lab = csgraph.connected_components(g, directed=False)
    comp = np.full(n, -1, np.int64)
    valid = np.ones(n, bool) if mask is None else mask
    if valid.any():
        _, inv = np.unique(lab[valid], return_inverse=True)
        comp[valid] = inv
    return comp


def tree_nearest_centroid(query, ref, chunk=512):
    """cKDTree twin of split_pieces.nearest_centroid: (index, distance) of the nearest ref point per query point."""
    d, i = cKDTree(ref).query(query)
    return i.astype(np.int64), d


def patch_split_pieces():
    """Make split_pieces usable at ~0.5 M faces: scipy connected components, k-d tree nearest centroid."""
    sp.Graph.components = fast_components
    sp.nearest_centroid = tree_nearest_centroid


# ----------------------------------------------------------------------------------------------- 1. classify

def classify_faces(pal, colors, cos, hue_fit=True):
    """Per high-poly face: evidence e[l] (garment/base class vote mass over its 3 texture points, 0..1) and the visibility
    weight. Returns (role per face = argmax evidence, evidence (n, K), visibility weight (n,), the fitted palette, report)."""
    n = len(colors)
    flat = colors.reshape(-1, 3).astype(np.float64)
    seen_flat = np.repeat(cos > 0, 3)
    report = {}
    if hue_fit and seen_flat.any():
        shifts, rep = pal.fit_hues(flat[seen_flat])
        if shifts:
            pal = pal.with_hue_shifts(shifts)
        report = {'active': True, 'shifts': shifts, **{k: rep[k] for k in ('review',)}}
    role, weight = pal.classify(flat)
    K = len(pal.roles) + 1
    ev = np.zeros((n, K))
    face = np.repeat(np.arange(n), 3)
    ok = role < K
    np.add.at(ev, (face[ok], role[ok]), weight[ok])
    ev /= 3.
    vis = np.maximum(ramp(cos, 0, .25), .05).astype(np.float64)       # face_labels.visibility_weight with its constants
    return ev.argmax(1), ev, vis, pal, report


# ----------------------------------------------------------------------------------------------- 2. clean on the high-poly

def potts(evidence, vis, pairs, lam=.5, iterations=14, seed=3, bare=None, bare_cost=1.2):
    """Approximate Potts minimisation (iterated conditional modes with random half updates) over the face graph.
    Cost of label l at face i: vis_i * (E_i - e_i[l]) + lam * (neighbours not labelled l). Faces without evidence (E = 0)
    simply follow their neighbours. Returns (labels, info)."""
    n, K = evidence.shape
    adj = csr_adjacency(pairs, n)
    deg = np.asarray(adj.sum(1)).ravel()
    E = evidence.sum(1)
    unary = vis[:, None] * (E[:, None] - evidence)
    if bare is not None:                       # module prior: the base must stay bare here (forearm, lower leg, head)
        unary[:, 1:] += bare_cost * bare[:, None]
    lab = evidence.argmax(1)
    if bare is not None:
        lab = np.where(bare & (lab > 0), 0, lab)
    rng = np.random.default_rng(seed)
    changed = []
    for it in range(iterations):
        onehot = sparse.csr_matrix((np.ones(n, np.float32), (np.arange(n), lab)), shape=(n, K))
        nb = (adj @ onehot).toarray()
        cost = unary + lam * (deg[:, None] - nb)
        new = cost.argmin(1)
        pick = rng.random(n) < (.5 if it < iterations - 1 else 1.)
        upd = pick & (new != lab)
        changed.append(int(upd.sum()))
        lab = np.where(pick, new, lab)
    return lab, {'lambda': lam, 'iterations': iterations, 'faces_changed_per_iteration': changed,
                 'bare_zone_faces': int(bare.sum()) if bare is not None else 0, 'bare_cost': bare_cost if bare is not None else None}


def absorb_small(lab, area, pairs, min_share=5e-4, rounds=6):
    """Same-label face components smaller than `min_share` of the total area (specks, pin-holes of base inside the armour,
    flecks of wear on the body) take the label most of their border faces have; repeated, since a merge can unite specks."""
    n = len(lab)
    out = lab.copy()
    total = float(area.sum())
    moved = 0
    for _ in range(rounds):
        keep = out[pairs[:, 0]] == out[pairs[:, 1]]
        g = sparse.coo_matrix((np.ones(keep.sum(), np.int8), (pairs[keep, 0], pairs[keep, 1])), shape=(n, n))
        k, comp = csgraph.connected_components(g, directed=False)
        a = np.bincount(comp, weights=area, minlength=k)
        small = (a < min_share * total)[comp]
        cross = ~keep & (small[pairs[:, 0]] | small[pairs[:, 1]])
        if not small.any() or not cross.any():
            break
        K = int(out.max()) + 1
        votes = np.zeros((k, K))
        pa, pb = pairs[cross, 0], pairs[cross, 1]
        for x, y in ((pa, pb), (pb, pa)):                 # x is the small side
            m = small[x]
            np.add.at(votes, (comp[x[m]], out[y[m]]), 1.)
        new = votes.argmax(1)
        has = votes.sum(1) > 0
        mv = small & has[comp]
        if not mv.any():
            break
        out = np.where(mv, new[comp], out)
        moved += int(mv.sum())
    return out, {'min_share': min_share, 'faces_relabelled': moved}


def reflood_unseen(lab, cos, area, pairs, min_share=.7):
    """Regions of one label that are mostly unseen by the concept cameras (Tripo invented them) do not start sections: their
    faces are re-labelled from the surrounding seen faces (majority of labelled neighbours, ring by ring)."""
    n = len(lab)
    g = sparse.coo_matrix((np.ones(len(pairs), np.int8), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
    keep = lab[pairs[:, 0]] == lab[pairs[:, 1]]
    gg = sparse.coo_matrix((np.ones(keep.sum(), np.int8), (pairs[keep, 0], pairs[keep, 1])), shape=(n, n))
    k, comp = csgraph.connected_components(gg, directed=False)
    unseen_area = np.bincount(comp, weights=area * (cos <= 0), minlength=k)
    tot_area = np.bincount(comp, weights=area, minlength=k)
    bad = (unseen_area / np.maximum(tot_area, 1e-20) >= min_share)[comp]
    info = {'unseen_dominated_faces': int(bad.sum()), 'unseen_dominated_area_share': float(area[bad].sum() / area.sum())}
    if not bad.any() or bad.all():
        return lab, info
    adj = csr_adjacency(pairs, n)
    K = int(lab.max()) + 1
    out = lab.copy()
    free = bad.copy()
    for _ in range(400):
        if not free.any():
            break
        known = ~free
        onehot = sparse.csr_matrix((known.astype(np.float32), (np.arange(n), out)), shape=(n, K))
        votes = (adj @ onehot).toarray()
        has = free & (votes.sum(1) > 0)
        if not has.any():
            break
        out[has] = votes[has].argmax(1)
        free &= ~has
    info['reflooded_iterations'] = _ + 1
    return out, info


def flood_labels(lab, free, pairs, rounds=400):
    """Give every `free` face the majority label of its already labelled neighbours, ring by ring, so a strip filled from both
    sides ends in the middle. Returns the new labels (faces that never get a labelled neighbour keep theirs)."""
    n = len(lab)
    adj = csr_adjacency(pairs, n)
    K = int(lab.max()) + 1
    out = lab.copy()
    free = free.copy()
    for _ in range(rounds):
        if not free.any():
            break
        onehot = sparse.csr_matrix(((~free).astype(np.float32), (np.arange(n), out)), shape=(n, K))
        votes = (adj @ onehot).toarray()
        has = free & (votes.sum(1) > 0)
        if not has.any():
            break
        out[has] = votes[has].argmax(1)
        free &= ~has
    return out


def fill_slivers(lab, area, pairs, edge_len, bare=None, max_width_pct=1.0, max_share=.02, height=2.):
    """Thin strips of base left between garment faces (Tripo's red-baked seams: a base sliver of 0.5% of the height between the
    breastplate and the fauld) are not body, they are holes in the armour once the base is removed. Every base component that
    is enclosed by garment, does not touch the module's bare zones, is small (area share <= max_share) and thin (mean width
    2 x area / perimeter <= max_width_pct of the height) takes the garment labels of its border, ring by ring.
    pairs / edge_len: face pairs and the length of the shared edge. Returns (labels, info)."""
    n = len(lab)
    base = lab == 0
    keep = base[pairs[:, 0]] & base[pairs[:, 1]]
    g = sparse.coo_matrix((np.ones(keep.sum(), np.int8), (pairs[keep, 0], pairs[keep, 1])), shape=(n, n))
    k, comp = csgraph.connected_components(g, directed=False)
    comp = np.where(base, comp, -1)
    cross = base[pairs[:, 0]] != base[pairs[:, 1]]
    bface = np.where(base[pairs[cross, 0]], pairs[cross, 0], pairs[cross, 1])
    per = np.bincount(comp[bface], weights=edge_len[cross], minlength=k)
    ar = np.bincount(comp[base], weights=area[base], minlength=k)
    cnt = np.bincount(comp[base], minlength=k)
    touches_bare = np.zeros(k)
    if bare is not None:
        touches_bare = np.bincount(comp[base & bare], minlength=k)
    width_pct = 100. * 2 * ar / np.maximum(per, 1e-12) / height
    share = ar / float(area.sum())
    cand = (cnt > 0) & (per > 0) & (width_pct <= max_width_pct) & (share <= max_share) & (touches_bare == 0)
    free = cand[np.maximum(comp, 0)] & base
    info = {'max_width_pct': max_width_pct, 'max_share': max_share, 'filled_components': int(cand.sum()), 'faces_filled': int(free.sum()),
            'area_share_filled': float(area[free].sum() / area.sum()),
            'components': [{'faces': int(cnt[i]), 'area_share': float(share[i]), 'width_pct_height': float(width_pct[i])}
                           for i in np.flatnonzero(cand)[:20]]}
    if not free.any():
        return lab, info
    return flood_labels(lab, free, pairs), info


# ----------------------------------------------------------------------------------------------- 3. wearable + sections

def manifest_for(meta, current_manifest):
    """The manifest fields split_pieces.find_plane / find_joints read."""
    return {'normalization_scale': meta['normalization_scale'], 'normalization_origin_rotated': meta['normalization_origin_rotated'],
            'rotation_to_review': meta['rotation_to_review'], 'parts': meta['parts'], 'module': meta['module'],
            'palette': {'garments': [{'role': r} for r in current_manifest['palette']['garments'] and
                                     [g['role'] for g in current_manifest['palette']['garments']]]}}


def highpoly_level(v_glb, f, lab, part, part_names, pal, mf, reduced_dir, height, name='hp'):
    """split_pieces.Level of the wearable high-poly faces (lab >= 1); materials = the garment roles."""
    wear = lab >= 1
    mats = [{'name': r, 'color': [float(x) for x in c] + [1.]} for r, c in zip(pal.roles, pal.material_srgb()[1:1 + len(pal.roles)])]
    level = sp.Level(name, v_glb, f[wear], lab[wear] - 1, mats, height)
    level.height_source = 'manifest normalization_scale'
    level.part = part[wear].astype(np.int64)
    level.part_names = [str(n) for n in part_names]
    level.parts_info = {'method': 'segment_first prep (face_labels.transfer_parts)'}
    level.plane = sp.find_plane(mf, reduced_dir, level, None)
    level.joints = sp.find_joints(mf, reduced_dir)
    return level, wear


def segment_highpoly(level, min_island_faces, cross_joint=None):
    """Regions segmentation on the high-poly wearable graph: the same sections_regions as the low-poly pipeline, then (build_sections)
    the cross-joint reassignment of fringe patches (cross_joint False = off, None = config default)."""
    spec = sp.load_spec('body_armor', None, overrides={'rigid_method': 'regions', 'joints': 'all', 'min_island_faces': int(min_island_faces),
                                                       'cross_joint': cross_joint})
    spec['rigid'].setdefault('snap', {})
    sec = sp.build_sections(level, spec, 'regions', False)
    return sec


def seam_length(v, edges, pairs, piece):
    """Total length of the edges that separate two different sections (edges: the shared vertex pair of every `pairs` row)."""
    cut = piece[pairs[:, 0]] != piece[pairs[:, 1]]
    return float(np.linalg.norm(v[edges[cut, 0]] - v[edges[cut, 1]], axis=1).sum())


def _pair_mincut(a_lab, b_lab, label, band, pairs, cap_e, area, pull):
    """Binary min-cut for one pair of sections: faces labelled a_lab / b_lab inside `band` are free, the faces of those two labels
    just outside the band are fixed terminals. Edge capacity = cost of cutting that edge (cap_e), plus a pull of `pull` x area per face
    toward its current label. Returns the new label array (only band faces of the two labels can change)."""
    n = len(label)
    is_a, is_b = label == a_lab, label == b_lab
    both = is_a | is_b
    free = band & both
    if not free.any():
        return label
    touch = np.zeros(n, bool)
    ok = free[pairs[:, 0]] | free[pairs[:, 1]]
    p0, p1 = pairs[ok, 0], pairs[ok, 1]
    touch[p0] = touch[p1] = True
    ring = touch & both & ~free                              # fixed terminal faces next to the free ones
    nodes = np.flatnonzero(free | ring)
    loc = np.full(n, -1, np.int64)
    loc[nodes] = np.arange(len(nodes))
    keep = (loc[pairs[:, 0]] >= 0) & (loc[pairs[:, 1]] >= 0)
    a, b, c = loc[pairs[keep, 0]], loc[pairs[keep, 1]], cap_e[keep]
    m = len(nodes)
    S, T = m, m + 1
    big = int(2 ** 30)
    fixed_a = np.flatnonzero(ring[nodes] & is_a[nodes])
    fixed_b = np.flatnonzero(ring[nodes] & is_b[nodes])
    freeidx = np.flatnonzero(free[nodes])
    ua = freeidx[is_a[nodes][freeidx]]
    ub = freeidx[is_b[nodes][freeidx]]
    src = np.concatenate([a, b, np.full(len(fixed_a), S), np.full(len(ua), S), ub, fixed_b])
    dst = np.concatenate([b, a, fixed_a, ua, np.full(len(ub), T), np.full(len(fixed_b), T)])
    cap = np.concatenate([c, c, np.full(len(fixed_a), big), np.maximum((area[nodes[ua]] * pull).astype(np.int64), 1),
                          np.maximum((area[nodes[ub]] * pull).astype(np.int64), 1), np.full(len(fixed_b), big)])
    cap = np.minimum(cap, big).astype(np.int32)
    G = sparse.csr_matrix((cap, (src, dst)), shape=(m + 2, m + 2))
    G.sum_duplicates()
    flow = csgraph.maximum_flow(G, S, T)
    res = (G - flow.flow).tocsr()
    res.data = np.where(res.data > 0, 1, 0).astype(np.int8)
    res.eliminate_zeros()
    reach = csgraph.breadth_first_order(res, S, directed=True, return_predecessors=False)
    side_a = np.zeros(m + 2, bool)
    side_a[reach] = True
    out = label.copy()
    out[nodes[freeidx]] = np.where(side_a[freeidx], a_lab, b_lab)
    return out


def mincut_sections(piece, f, v, pairs, edges, band_pct=1.5, pull_pct=None, crease_deg=15., crease_low=.15, min_share=5e-4, height=2.):
    """Cut straightening by minimum cuts: for every pair of neighbouring sections (A, B) the faces of A and B within `band_pct` of
    the height of their interface are re-split by the minimum cut (scipy max-flow) of the dual graph whose edge cost is
    length x (crease_low + (1 - crease_low) exp(-(dihedral / crease_deg)^2)) and whose faces are pulled back to their current section with
    a cost proportional to their area (`pull_pct` = the displacement in % of the height at which a flat panel's cost equals the
    gain of reaching a crease, default band_pct). The cut becomes the shortest closed path in the band: one-triangle teeth and
    zigzags vanish, and it settles on a fold that runs within the band (cost x crease_low there). Faces outside the band never change.
    Components made only of relabelled faces and tiny are undone. Returns (piece, info)."""
    n = len(piece)
    tri = v[f]
    cross_n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    area = np.linalg.norm(cross_n, axis=1) / 2
    nrm = cross_n / np.maximum(np.linalg.norm(cross_n, axis=1), 1e-20)[:, None]
    ang = np.degrees(np.arccos(np.clip((nrm[pairs[:, 0]] * nrm[pairs[:, 1]]).sum(1), -1, 1)))
    wgt = crease_low + (1 - crease_low) * np.exp(-(ang / crease_deg) ** 2)
    elen = np.linalg.norm(v[edges[:, 0]] - v[edges[:, 1]], axis=1)
    cent = tri.mean(1)
    spacing = float(np.linalg.norm(cent[pairs[:, 0]] - cent[pairs[:, 1]], axis=1).mean())
    pull_pct = band_pct if pull_pct is None else pull_pct
    # capacities in integers: the longest edge x 1 -> 1e6; pull: gain per unit length (1 - crease_low) = pull x displacement x (length 1)
    S = 1e6 / max(float(elen.max()), 1e-12)
    cap_e = np.maximum(np.round(elen * wgt * S), 1)
    pull = (1 - crease_low) / (pull_pct / 100 * height) * S                      # cost per unit area
    adj = csr_adjacency(pairs, n)
    info = {'method': 'mincut', 'band_pct': band_pct, 'pull_pct': pull_pct, 'crease_deg': crease_deg, 'crease_low': crease_low,
            'face_spacing_pct': 100 * spacing / height, 'seam_length_before_pct': 100 * seam_length(v, edges, pairs, piece) / height}
    cut0 = piece[pairs[:, 0]] != piece[pairs[:, 1]]
    out = piece.copy()
    reach = max(int(np.ceil(band_pct / 100 * height / spacing)), 1)
    done = []
    for a_lab, b_lab in sorted({tuple(sorted(x)) for x in zip(piece[pairs[cut0, 0]].tolist(), piece[pairs[cut0, 1]].tolist())}):
        sel = cut0 & (((piece[pairs[:, 0]] == a_lab) & (piece[pairs[:, 1]] == b_lab)) | ((piece[pairs[:, 0]] == b_lab) & (piece[pairs[:, 1]] == a_lab)))
        band = np.zeros(n, bool)
        band[pairs[sel].ravel()] = True
        for _ in range(reach):
            band |= (adj @ band.astype(np.float32)) > 0
        before = out
        out = _pair_mincut(a_lab, b_lab, out, band, pairs, cap_e.astype(np.int64), area, pull)
        done.append({'pair': [int(a_lab), int(b_lab)], 'faces_changed': int((out != before).sum())})
    same = out[pairs[:, 0]] == out[pairs[:, 1]]
    g = sparse.coo_matrix((np.ones(same.sum(), np.int8), (pairs[same, 0], pairs[same, 1])), shape=(n, n))
    k, comp = csgraph.connected_components(g, directed=False)
    untouched = np.bincount(comp, weights=(out == piece).astype(float), minlength=k)
    comp_area = np.bincount(comp, weights=area, minlength=k)
    tiny_new = (untouched == 0) & (comp_area < min_share * area.sum())
    revert = tiny_new[comp]
    out[revert] = piece[revert]
    changed = out != piece
    per = {}
    for l in np.unique(piece):
        a0, a1 = float(area[piece == l].sum()), float(area[out == l].sum())
        per[int(l)] = {'faces_before': int((piece == l).sum()), 'faces_after': int((out == l).sum()), 'area_change_pct': 100 * (a1 - a0) / max(a0, 1e-20)}
    info.update(pairs=done, faces_changed=int(changed.sum()), area_share_changed=float(area[changed].sum() / area.sum()), reverted_new_islands=int(tiny_new.sum()),
                seam_length_after_pct=100 * seam_length(v, edges, pairs, out) / height, sections=per)
    return out, info


# ----------------------------------------------------------------------------------------------- 4. section borders

def border_edges(f, piece):
    """Vertex pairs of the wearable's edges that separate two sections or are an open boundary (one face)."""
    n = len(f)
    a = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
    b = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
    face = np.tile(np.arange(n), 3)
    lo, hi = np.minimum(a, b).astype(np.int64), np.maximum(a, b).astype(np.int64)
    key = lo * (int(f.max()) + 1) + hi
    order = np.argsort(key, kind='stable')
    key, face, lo, hi = key[order], face[order], lo[order], hi[order]
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    counts = np.diff(np.r_[starts, len(key)])
    f0 = face[starts]
    f1 = face[np.minimum(starts + 1, len(key) - 1)]
    border = (counts == 1) | (counts > 2) | ((counts == 2) & (piece[f0] != piece[f1]))
    kind = np.where(counts[border] == 1, 0, 1)                   # 0 open boundary, 1 between sections
    return np.stack([lo[starts][border], hi[starts][border]], 1), kind


def border_chains(edges):
    """Chains of border edges between junction vertices (border-graph degree != 2); closed loops without a junction
    become one chain starting and ending at the same vertex. Returns a list of vertex-id lists and the junction set."""
    nbrs = {}
    for a, b in edges.tolist():
        nbrs.setdefault(a, []).append(b)
        nbrs.setdefault(b, []).append(a)
    junction = {v for v, ns in nbrs.items() if len(ns) != 2}
    used = set()
    chains = []

    def walk(start, nxt):
        chain = [start, nxt]
        used.add((min(start, nxt), max(start, nxt)))
        prev, cur = start, nxt
        while cur not in junction and cur != start:
            n2 = [x for x in nbrs[cur] if (min(cur, x), max(cur, x)) not in used]
            if not n2:
                break
            used.add((min(cur, n2[0]), max(cur, n2[0])))
            prev, cur = cur, n2[0]
            chain.append(cur)
        return chain

    for j in sorted(junction):
        for x in nbrs[j]:
            if (min(j, x), max(j, x)) not in used:
                chains.append(walk(j, x))
    for v in sorted(nbrs):
        for x in nbrs[v]:
            if (min(v, x), max(v, x)) not in used:
                chains.append(walk(v, x))
    return chains, junction


def douglas_peucker(P, eps):
    """Indices of the points kept by Douglas-Peucker with tolerance `eps` (always the two ends)."""
    n = len(P)
    keep = np.zeros(n, bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        seg = P[j] - P[i]
        L = np.linalg.norm(seg)
        rel = P[i + 1:j] - P[i]
        if L < 1e-12:
            d = np.linalg.norm(rel, axis=1)
        else:
            t = np.clip(rel @ seg / (L * L), 0, 1)
            d = np.linalg.norm(rel - t[:, None] * seg, axis=1)
        k = int(d.argmax())
        if d[k] > eps:
            m = i + 1 + k
            keep[m] = True
            stack += [(i, m), (m, j)]
    return np.flatnonzero(keep)


def border_plan(v, f, piece, eps):
    """Kept border vertices S (bool mask over vertices) and `snap_to` (vertex -> kept vertex for every dropped vertex of a
    shared border, nearest along its chain). Chains between two sections are simplified as polylines (Douglas-Peucker,
    tolerance `eps`); chains that are only an open boundary of one section are left to the simplifier, only their end
    (junction) vertices are kept."""
    edges, kind = border_edges(f, piece)
    kind_of = {(int(a), int(b)): int(k) for (a, b), k in zip(edges.tolist(), kind.tolist())}
    chains, junction = border_chains(edges)
    keep = np.zeros(len(v), bool)
    keep[list(junction)] = True
    snap_to = np.arange(len(v))
    shared_vertices = 0
    n_shared = 0
    kept_chains = []
    for ch in chains:
        kinds = [kind_of[(min(a, b), max(a, b))] for a, b in zip(ch[:-1], ch[1:])]
        if not any(kinds):                                # open boundary of one section only
            continue
        n_shared += 1
        ch = np.array(ch)
        P = v[ch]
        closed = len(ch) > 2 and ch[0] == ch[-1]
        if closed:                                       # split the loop at its farthest vertex
            m = int(np.linalg.norm(P - P[0], axis=1).argmax())
            idx = np.union1d(douglas_peucker(P[:m + 1], eps), m + douglas_peucker(P[m:], eps))
        else:
            idx = douglas_peucker(P, eps)
        keep[ch[idx]] = True
        kept_chains.append(ch[idx])
        shared_vertices += len(ch)
        arc = np.r_[0, np.cumsum(np.linalg.norm(np.diff(P, axis=0), axis=1))]
        kept_arc = arc[idx]
        idx_set = set(idx.tolist())
        for k in range(len(ch)):
            if k not in idx_set:
                snap_to[ch[k]] = ch[idx[np.abs(kept_arc - arc[k]).argmin()]]
    return keep, snap_to, {'chains': len(chains), 'shared_chains': n_shared, 'shared_border_vertices': int(shared_vertices),
                           'kept_vertices': int(keep.sum()), 'junctions': len(junction), 'kept_chains': kept_chains, 'junction_vertices': junction}


def smooth_border_vertices(v, faces, kept_chains, junctions, err, iterations=4, lam=.5, max_move=.35):
    """Laplacian smoothing of the kept border polylines of the REDUCED sections (positions only; the topology and the shared vertex
    ids stay, so neighbouring sections still weld exactly). The ends (junctions of three sections / open boundaries) stay put, no
    vertex moves farther than `max_move` x the reduction error from its place on the surface, and a move that flips or collapses
    a triangle is undone. Returns (new positions, info)."""
    out = v.copy()
    moved_ids = []
    for ch in kept_chains:
        ch = np.asarray(ch)
        closed = len(ch) > 2 and ch[0] == ch[-1]
        c = ch[:-1] if closed else ch
        n = len(c)
        if n < 3:
            continue
        P = v[c].copy()
        free = np.ones(n, bool)
        if not closed:
            free[0] = free[-1] = False
        free &= ~np.isin(c, list(junctions))
        Q = P.copy()
        for _ in range(iterations):
            nb = (np.roll(Q, 1, axis=0) + np.roll(Q, -1, axis=0)) / 2
            Q = np.where(free[:, None], Q + lam * (nb - Q), Q)
        d = Q - P
        L = np.linalg.norm(d, axis=1)
        cap = max_move * err
        Q = np.where((L > cap)[:, None], P + d * (cap / np.maximum(L, 1e-20))[:, None], Q)
        out[c] = Q
        moved_ids.append(c[free])
    moved = np.unique(np.concatenate(moved_ids)) if moved_ids else np.zeros(0, np.int64)
    tri0, tri1 = v[faces], out[faces]
    n0 = np.cross(tri0[:, 1] - tri0[:, 0], tri0[:, 2] - tri0[:, 0])
    reverted = 0
    for _ in range(6):
        tri1 = out[faces]
        n1 = np.cross(tri1[:, 1] - tri1[:, 0], tri1[:, 2] - tri1[:, 0])
        bad = ((n0 * n1).sum(1) <= .05 * (n0 * n0).sum(1))
        if not bad.any():
            break
        bv = np.unique(faces[bad])
        bv = bv[np.isin(bv, moved)]
        if not len(bv):
            break
        out[bv] = v[bv]
        reverted += len(bv)
    disp = np.linalg.norm(out[moved] - v[moved], axis=1) if len(moved) else np.zeros(1)
    return out, {'iterations': iterations, 'lambda': lam, 'max_move_error_fraction': max_move, 'moved_vertices': int((disp > 0).sum()),
                 'reverted_vertices': int(reverted), 'max_move_pct_height': float(100 * disp.max() / 2), 'mean_move_pct_height': float(100 * disp[disp > 0].mean() / 2) if (disp > 0).any() else 0.}


# ----------------------------------------------------------------------------------------------- 5. reduce each section

def simplify(v, f, error, locked):
    """meshopt_simplifyWithAttributes exactly as tools/lowpoly adaptive_lod04.simplify / tripo_adaptive_study.reduce_error
    (no target count, absolute error) with a vertex lock mask over the compact vertex array of (v, f)."""
    ids, inv = np.unique(f, return_inverse=True)
    vv = np.ascontiguousarray(v[ids], np.float32)
    ff = np.ascontiguousarray(inv.reshape(-1, 3), np.uint32)
    locks = np.zeros(len(vv), np.uint8)
    locks[locked[ids]] = 1
    dest = np.empty(ff.size, np.uint32)
    result = C.c_float()
    count = _SIMPLIFY(dest.ctypes.data, ff.ctypes.data, ff.size, vv.ctypes.data, len(vv), 12, None, 0, None, 0,
                      locks.ctypes.data, 0, error, 4, C.byref(result))
    if count == 0:
        raise ValueError('No triangles survived the error limit')
    return ids[dest[:count].reshape(-1, 3).astype(np.int64)], float(result.value)


def cancel_pairs_np(v, f):
    """adaptive_cleanup04.cancel_pairs without bpy: drop opposite-wound duplicate triangle pairs."""
    key = np.sort(f, axis=1)
    _, inv, count = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    keep = np.ones(len(f), bool)
    for group in np.flatnonzero(count == 2):
        ids = np.flatnonzero(inv == group)
        a, b = f[ids]
        na = np.cross(v[a[1]] - v[a[0]], v[a[2]] - v[a[0]])
        nb = np.cross(v[b[1]] - v[b[0]], v[b[2]] - v[b[0]])
        if na @ nb < 0:
            keep[ids] = False
    return keep


def reduce_sections(v, f_w, piece, fraction, smooth_border=None, return_positions=False):
    """Reduce every section of the wearable (faces f_w with section ids `piece`) on its own at `fraction` of the whole
    model's normalised height. smooth_border: None or a dict of smooth_border_vertices keyword arguments (kept border vertices are
    moved after the reduction, identically for both neighbouring sections). Returns (faces per section (global vertex ids), info), and
    with return_positions also the vertex positions (v itself unless smoothed)."""
    err = 2 * fraction
    keep_mask, snap_to, binfo = border_plan(v, f_w, piece, .5 * err)
    kept_chains, junctions = binfo.pop('kept_chains'), binfo.pop('junction_vertices')
    out, info = {}, {'border': binfo, 'sections': {}}
    for lab in np.unique(piece):
        fp = f_w[piece == lab]
        ids = np.unique(fp)
        locked = np.zeros(len(v), bool)
        locked[ids] = keep_mask[ids]
        red, mesh_err = simplify(v, fp, err, locked)
        # border vertices that survived but are not kept are merged into their kept neighbour along the border
        red = snap_to[red]
        ok = (red[:, 0] != red[:, 1]) & (red[:, 1] != red[:, 2]) & (red[:, 0] != red[:, 2])
        red = red[ok]
        keep = cancel_pairs_np(v, red)
        out[int(lab)] = red[keep]
        info['sections'][int(lab)] = {'high_poly_faces': int(len(fp)), 'reduced_faces': int(keep.sum()), 'meshopt_error': mesh_err,
                                      'locked_border_vertices': int(locked.sum())}
    v_out = v
    if smooth_border is not None:
        v_out, info['border_smoothing'] = smooth_border_vertices(v, np.concatenate(list(out.values())), kept_chains, junctions, err, **smooth_border)
    return (out, info, v_out) if return_positions else (out, info)


# ----------------------------------------------------------------------------------------------- 6. roles of the reduced faces

def tri_samples(tris, per_area):
    """Sample points on triangles (area-proportional, at least 1 per triangle): (points, owner triangle)."""
    area = np.linalg.norm(np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1) / 2
    k = np.clip(np.ceil(area / per_area), 1, 400).astype(int)
    owner = np.repeat(np.arange(len(tris)), k)
    rng = np.random.default_rng(5)
    r1, r2 = rng.random(len(owner)), rng.random(len(owner))
    s = np.sqrt(r1)
    bary = np.stack([1 - s, s * (1 - r2), s * r2], 1)
    return np.einsum('ni,nij->nj', bary, tris[owner]), owner


def cover_votes(tris, hp_cent, hp_area, hp_class, n_class, per_area=None):
    """For every triangle: area of the high-poly faces it covers per class (each high-poly face goes to the triangle whose
    surface samples are nearest to its centroid). Returns (votes (m, n_class), owner per high-poly face)."""
    if per_area is None:
        per_area = max(float(hp_area.sum()) / 150000, 1e-9)
    pts, owner = tri_samples(tris, per_area)
    _, idx = cKDTree(pts).query(hp_cent)
    tri_of = owner[idx]
    votes = np.zeros((len(tris), n_class))
    np.add.at(votes, (tri_of, hp_class), hp_area)
    return votes, tri_of


def top_share_stats(votes, tri_area, threshold=.75):
    """Area share of the triangles whose top class holds < threshold of the covered high-poly area, and the mean top share."""
    tot = votes.sum(1)
    top = votes.max(1) / np.maximum(tot, 1e-20)
    ok = tot > 0
    a = tri_area[ok]
    return {'mixed_area_share': float(a[top[ok] < threshold].sum() / a.sum()), 'mean_top_share': float((top[ok] * a).sum() / a.sum()),
            'faces': int(ok.sum())}


# ----------------------------------------------------------------------------------------------- 7. driver

def hp_inert_snap(level, label, spec, a, b, info):
    info.update(skipped='high-poly graph: snap re-cut not run (too large for the python min-cut)')
    return label


def process(concept_dir, out_dir, levels=('0p4', '0p2', '0p1'), render=False, blend=True, palette_dir=None, overlap=False,
            cross_joint=None, prep_dir=None, fill=True, smooth_cuts=True, smooth_border=True, cut_band_pct=1.5, sliver_width_pct=1.0,
            reduced_dir=None, source=None):
    """Whole split for one module. concept_dir: the folder with reduced/manifest.json of the reduction (reduced_dir overrides the
    reduced/ folder; source = the source GLB, recorded only); out_dir/prep/ (or prep_dir) from segment_first_prep.py. palette_dir:
    palette folder/JSON, default the palette prep.json recorded (what the reduction used). Overlap strips are off unless `overlap`; cross_joint False switches the
    fringe reassignment off; fill / smooth_cuts / smooth_border switch the seam clean-up steps (base slivers between garment faces,
    cut straightening on the high-poly graph, smoothing of the kept border polylines) off. Writes out_dir/pieces/ (lod_<lv>/..., pieces.json) and out_dir/segment_first.json."""
    concept_dir, out_dir = Path(concept_dir), Path(out_dir)
    reduced_dir = Path(reduced_dir) if reduced_dir else concept_dir / 'reduced'
    if (out_dir / 'pieces' / 'pieces.json').exists():
        raise SystemExit(f'{out_dir}/pieces/pieces.json exists; choose a new --out (earlier attempts are never overwritten)')
    out_dir.mkdir(parents=True, exist_ok=True)
    patch_split_pieces()
    sp.snap_cut = hp_inert_snap
    timing = {}
    t0 = time.monotonic()

    def lap(key, t):
        timing[key] = round(time.monotonic() - t, 2)
        print(f'SEGMENT_FIRST {key} {timing[key]}s', flush=True)

    t = time.monotonic()
    prep_dir = Path(prep_dir) if prep_dir else out_dir / 'prep'
    z = np.load(prep_dir / 'prep.npz')
    meta = json.loads((prep_dir / 'prep.json').read_text())
    cur = json.loads((reduced_dir / 'manifest.json').read_text())
    v, f = z['v'].astype(np.float64), z['f'].astype(np.int64)
    if palette_dir:
        pal = Palette.coerce(Path(palette_dir))
    elif meta.get('palette'):
        pal = Palette.coerce(meta['palette'], source=str(meta['palette'].get('source', 'prep.json')))
    else:
        pal = Palette.coerce(ROOT / 'base_views/human_male')
    lap('load', t)
    t = time.monotonic()
    _, ev_unused = None, None
    role0, ev, vis, pal2, hue_rep = classify_faces(pal, z['colors'], z['cos'])
    lap('classify_high_poly_faces', t)
    t = time.monotonic()
    pairs, pair_edges = edge_pairs(f, True)
    _, area, _ = sp.triangle_geometry(v, f)
    pn = [str(x) for x in z['part_names']]
    bare_ids = [i for i, n_ in enumerate(pn) if n_.startswith(('forearm_stretch.', 'hand.', 'leg_stretch.', 'foot.', 'head.'))]
    bare = np.isin(z['part'], bare_ids)
    lab, potts_info = potts(ev, vis, pairs, bare=bare)
    lab, reflood_info = reflood_unseen(lab, z['cos'], area, pairs)
    lab, absorb_info = absorb_small(lab, area, pairs)
    sliver_info = None
    if fill:
        edge_len = np.linalg.norm(v[pair_edges[:, 0]] - v[pair_edges[:, 1]], axis=1)
        lab, sliver_info = fill_slivers(lab, area, pairs, edge_len, bare, max_width_pct=sliver_width_pct)
    lap('clean_labels_on_high_poly', t)
    t = time.monotonic()
    mf = {k: meta[k] for k in ('normalization_scale', 'normalization_origin_rotated', 'rotation_to_review', 'parts', 'module')}
    vg = to_glb(v, meta)
    height = 2 / meta['normalization_scale']
    level_hp, wear = highpoly_level(vg, f, lab, z['part'], z['part_names'], pal2, mf, reduced_dir, height)
    lp_wear = None
    try:                      # the per-face split's wearable faces at 0p4 (pieces_auto), else the reduction's own count (the same number)
        lp_wear = json.loads((concept_dir / 'pieces_auto' / 'pieces.json').read_text())['levels']['0p4']['wearable_faces']
    except (OSError, KeyError, ValueError):
        lp_wear = (cur.get('levels', {}).get('0p4') or {}).get('wearable_triangles')
    min_island = round(6 * level_hp.n / lp_wear) if lp_wear else 2500
    sec_hp = segment_highpoly(level_hp, min_island, cross_joint)
    cj_info = sec_hp.report.get('cross_joint')
    lap('segment_high_poly', t)
    piece = sec_hp.label.copy()
    names = dict(sec_hp.names)
    fw = f[wear]
    smooth_info = None
    if smooth_cuts:
        t = time.monotonic()
        wp, wedges = edge_pairs(fw, True)
        piece, smooth_info = mincut_sections(piece, fw, v, wp, wedges, band_pct=cut_band_pct)
        lap('smooth_cuts_on_high_poly', t)
    hp_seam_pts = {}
    hp_seams = seam_metrics.highpoly_seams(v, fw, piece.astype(np.int64), names, pts=hp_seam_pts)
    hp_role = lab[wear] - 1
    hp_part = z['part'][wear].astype(np.int64)
    hp_cent = v[fw].mean(1)
    hp_area = area[wear]
    n_part = len(z['part_names'])
    np.savez_compressed(out_dir / 'highpoly_labels.npz', label=lab.astype(np.int8), wear=wear, piece=piece.astype(np.int8),
                        piece_names=np.array([names[i] for i in sorted(names)]), piece_ids=np.array(sorted(names)))
    mats = level_hp.materials
    spec_base = {'rigid_method': 'given', 'overlap': bool(overlap), 'pose_test': True, 'joints': 'all'}
    out = out_dir / 'pieces'
    out.mkdir(parents=True, exist_ok=True)
    result = {'source': str(concept_dir), 'source_glb': str(source) if source else None, 'reduced': str(reduced_dir), 'module': 'body_armor', 'method': 'segment_first', 'style': 'roles', 'piece': 'pauldron',
              'rigid_method': 'given', 'overlap': bool(overlap), 'cross_joint': cj_info, 'levels': {}}
    seg = {'timing_s': timing, 'palette_hue_fit': hue_rep, 'potts': potts_info, 'reflood_unseen': reflood_info,
           'absorb_small': absorb_info, 'fill_slivers': sliver_info, 'smooth_cuts': smooth_info, 'highpoly_seams': hp_seams, 'high_poly': {'faces': int(len(f)), 'wear_faces': int(wear.sum()),
                                                      'wear_area_share': float(area[wear].sum() / area.sum()),
                                                      'pieces': {names[l]: int((piece == l).sum()) for l in names},
                                                      'regions_report': sec_hp.report.get('regions'), 'cross_joint': cj_info,
                                                      'min_island_faces': int(min_island)},
           'levels': {}}
    (out_dir / 'levels').mkdir(exist_ok=True)
    for lv in levels:
        fraction = dict(LEVELS)[lv]
        t = time.monotonic()
        faces_by, rinfo, v_lv = reduce_sections(v, fw, piece, fraction, {} if smooth_border else None, True)
        tr = time.monotonic() - t
        F_all = np.concatenate([faces_by[l] for l in sorted(faces_by)])
        plabel = np.concatenate([np.full(len(faces_by[l]), l, np.int64) for l in sorted(faces_by)])
        ids, inv = np.unique(F_all, return_inverse=True)
        Fc = inv.reshape(-1, 3)
        Vn = v_lv[ids]
        tris = Vn[Fc]
        role = np.zeros(len(Fc), np.int64)
        partf = np.zeros(len(Fc), np.int64)
        for l in sorted(faces_by):
            tsel = np.flatnonzero(plabel == l)
            hsel = np.flatnonzero(piece == l)
            votes, _ = cover_votes(tris[tsel], hp_cent[hsel], hp_area[hsel], hp_role[hsel], len(mats))
            pv, _ = cover_votes(tris[tsel], hp_cent[hsel], hp_area[hsel], hp_part[hsel], n_part)
            tot = votes.sum(1)
            r = votes.argmax(1)
            p = pv.argmax(1)
            empty = tot <= 0
            if empty.any() and (~empty).any():
                cen = tris[tsel].mean(1)
                nn = cKDTree(cen[~empty]).query(cen[empty])[1]
                r[empty], p[empty] = r[~empty][nn], p[~empty][nn]
            role[tsel], partf[tsel] = r, p
        lp = sp.Level(lv, to_glb(Vn, meta), Fc, role, mats, height)
        lp.height_source = level_hp.height_source
        lp.part = partf
        lp.part_names = level_hp.part_names
        lp.parts_info = level_hp.parts_info
        lp.plane = sp.find_plane(mf, reduced_dir, lp, None)
        lp.joints = sp.find_joints(mf, reduced_dir)
        np.savez_compressed(out_dir / 'levels' / f'{lv}.npz', raw_v=lp.raw_v, raw_f=Fc, piece=plabel.astype(np.int8), role=role,
                            part=partf, v_norm=Vn, f_norm=Fc)
        spec = sp.load_spec('body_armor', None, overrides={**spec_base, 'given_label': plabel})
        ts = time.monotonic()
        info = sp.split_level(lp, spec, out / f'lod_{lv}', render, blend)
        info['rigid']['cross_joint'] = cj_info                       # applied on the high-poly graph, before the reduction
        result['levels'][lv] = info
        seg['levels'][lv] = {'reduce_s': round(tr, 2), 'split_outputs_pose_s': round(time.monotonic() - ts, 2), 'reduction': rinfo,
                             'wearable_triangles': int(len(Fc)),
                             'seams': seam_metrics.level_report(Vn.astype(np.float64), Fc, plabel, names, hp_seam_pts=hp_seam_pts or None)}
        print(f'SEGMENT_FIRST {lv}: {len(Fc)} wearable triangles; ' +
              ', '.join(f'{k} {p["faces"]}f/{p["components"]}c' for k, p in info['pieces'].items()), flush=True)
    timing['total_s'] = round(time.monotonic() - t0, 2)
    seg['timing_s'] = timing
    (out / 'pieces.json').write_text(json.dumps(result, indent=2, default=float))
    (out_dir / 'segment_first.json').write_text(json.dumps(seg, indent=2, default=float))
    print('SEGMENT_FIRST_DONE', json.dumps(timing), flush=True)
    return result, seg


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('concept_dir')
    ap.add_argument('out_dir')
    ap.add_argument('--levels', default='0p4,0p2,0p1')
    ap.add_argument('--reduced', help='the reduced/ folder with manifest.json (default <concept_dir>/reduced)')
    ap.add_argument('--source', help='source GLB of the module (recorded in pieces.json only; the geometry comes from prep/)')
    ap.add_argument('--palette', help='palette folder or palette.json (default: the palette recorded in prep.json)')
    ap.add_argument('--render', action='store_true', help='also render pieces_render.png per level (off by default)')
    ap.add_argument('--no-render', action='store_true', help='accepted for old command lines (rendering is off by default)')
    ap.add_argument('--no-blend', action='store_true')
    ap.add_argument('--overlap', action='store_true', help='add the hidden overlap strips at the cuts (off by default)')
    ap.add_argument('--no-cross-joint', action='store_true', help='keep fringe patches beyond their joint on the section the colours gave them')
    ap.add_argument('--prep-dir', help='directory with prep.npz + prep.json (default <out_dir>/prep)')
    ap.add_argument('--no-fill-slivers', action='store_true', help='keep thin base slivers between garment faces as holes')
    ap.add_argument('--no-smooth-cuts', action='store_true', help='keep the colour-boundary staircase as the cut between sections')
    ap.add_argument('--sliver-width', type=float, default=1.0, help='base strips between garment faces narrower than this %% of the height are filled (default 1.0)')
    ap.add_argument('--cut-band', type=float, default=1.5, help="cut straightening: faces within this %% of the height of a cut may move (default 1.5)")
    ap.add_argument('--no-smooth-border', action='store_true', help='keep the kept border polylines where the Douglas-Peucker pass left them')
    a = ap.parse_args(argv)
    process(a.concept_dir, a.out_dir, a.levels.split(','), a.render and not a.no_render, not a.no_blend, palette_dir=a.palette, overlap=a.overlap,
            cross_joint=False if a.no_cross_joint else None, prep_dir=a.prep_dir, fill=not a.no_fill_slivers,
            smooth_cuts=not a.no_smooth_cuts, smooth_border=not a.no_smooth_border, cut_band_pct=a.cut_band,
            sliver_width_pct=a.sliver_width, reduced_dir=a.reduced, source=a.source)


if __name__ == '__main__':
    main()
