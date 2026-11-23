"""Opt-in feature cleanup of the high-poly garment labels: `studs` mode (2026-10-25; integrated as the `studs` feature label pass, see
feature_hooks.py: `wearcut.py` / `segment_first.py` `--feature-labels studs`; this file is the algorithm and an analysis CLI).

    characters/.venv/bin/python tools/tripo_pipeline/feature_labels.py studs <module_dir> --out <out_dir>
        [--recut] [--pieces] [--levels 0p4,0p2,0p1] [--radius R] [--alpha A] [--no-spill] [--force]

Why: round studs / rivets are painted tertiary (yellow) in the concept. The texture of the converted model is yellow on (nearly) every
stud, but the label field loses them: `wearcut.run` / `segment_first` clean the high-poly labels with `absorb_small` (a same-label
component under 0.05% of the garment area is a speck and takes the surrounding label) and a stud is ~0.03-0.07% of it, so most studs
turn into the surrounding plate colour, a few survive whole, a few partly (where a stud touches other yellow or the texture disc is
offset from the dome: Tripo's texture and geometry are not registered to better than about a stud radius), and the offset part of the
disc stays yellow on the flat plate (spill). `studs` repairs the label field using the GEOMETRY of the domes:

  1. scale: median radius of the compact yellow-evidence blobs of the texture (`radius_estimate`; `--radius` overrides; 0.02 if
     there are fewer than 3), everything below is relative to that radius r.
  2. raised height h per vertex = normal offset from its heat-diffused position (sigma 1.0 r, uniform cotangent Laplacian: studs
     vanish, the plate stays) minus the same quantity smoothed at 2 x sigma (a convex body's shrink offset and gentle plate steps are
     removed, the studs stay): `height_field`.
  3. seeds = garment faces with h > 0.15 r; opened on the face graph (erode / dilate by ~0.35 r: plate-edge ridges, creases and
     seams are thinner than that and vanish) and closed by ~0.45 r (a flat-topped stud is a ring of raised height); connected components = stud candidates; per candidate area, projected radius,
     roundness (sqrt(l2/l1) of the centroid PCA) and fill (projected area / disc of the farthest centroid): `blob_stats`.
  4. accept: round and 0.4-1.8 r (yellow evidence >= 0.15) or 0.75-1.35 x the confirmed studs' core size, roundness >= 0.75, with a
     yellow stud within `near` (no yellow evidence at all: design consistency, "the studs of this design look like this").
  5. extent: the seed grown outward over faces with h >= alpha x the stud's peak (alpha 0.2) for at most 0.7 r: the dome down to its base.
  6. relabel: extent -> tertiary. Spill: tertiary faces outside every stud extent, within 1.0 r of one, not raised, in a component
     under 6 stud areas (a trim band, a belt, a buckle is big or raised: kept) -> the majority role of the garment faces around it.
Output (out_dir): `studs_labels.npz` (label_old / label_new / stud_id per face, spill, h per vertex), `studs.json` (numbers and a row per
candidate with its verdict) and, with `--recut`, `recut/` = `wearcut.run` repeated with the stud hook (`feature_hooks.make_hook('studs')`)
plus the relabelled full LODs, with `--pieces` `pieces_run/` = `segment_first.process` with the hook. Reads
`<module>/wearcut/{prep/prep.npz,labels.npz,palette.json,plane.json}` and `reduced/` only; never writes there (this analysis CLI is not
how the pipeline applies the pass: `rewear.py` / `reduce_model.py` / `convert_job.py` `--feature-labels studs` are).
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
from scipy.spatial import cKDTree

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import segment_first as sf  # noqa: E402
import wearcut as wc  # noqa: E402

H = 2.                      # normalised height of every module's frame
P = {
    'radius': None,         # stud radius r in normalised units; None = estimated from the yellow texture blobs, 0.02 when that fails
    'default_radius': .02,
    'min_blobs': 3,         # compact yellow blobs needed for a radius estimate
    'sigma_r': 1.0,         # heat-diffusion sigma = sigma_r x r (a stud of radius r is gone, the plate stays)
    'baseline_r': 2.0,      # the high-pass baseline is smoothed at this x sigma
    'seed_r': .15,          # raised-height threshold of the seeds = seed_r x r
    'close_r': .45,         # closing radius of the seeds = close_r x r (fills the plateau of a flat-topped stud)
    'open_r': .35,          # opening radius of the seeds = open_r x r (>= 1 ring)
    'min_area_r': .15,      # candidates smaller than this x pi r^2 (projected) are noise
    'yellow_min': .15,      # tertiary evidence share of a candidate that counts as "yellow-evidenced"
    'round_yellow': .55, 'round_plain': .75,
    'fill_min': .5,
    'size_yellow': (.4, 1.8),   # core projected radius / r (the yellow-evidenced studs)
    'size_plain': (.75, 1.35),  # core projected radius / the median core radius of the confirmed studs
    'near': .35,            # a plain (unyellowed) candidate needs a yellow-evidenced stud within this distance (normalised units)
    'alpha': .2,            # extent: faces down to alpha x the stud's peak height
    'smooth_rounds': 3,     # majority smoothing rounds of the extent rim
    'floor_r': .6,          # ... and never below this x the seed height (noise floor of the field)
    'reach_r': .7,          # ... for at most this x r beyond the seed
    'texture_discs': True,  # compact yellow-evidence blobs without a dome (flat studs) count as studs too
    'tex_size': (.6, 1.4), 'tex_round': .8, 'tex_fill': .65,   # blob radius / r, roundness, fill
    'tex_raised': .6,       # ... and its median raised height at least this x the seed height (a yellow ghost on a flat plate is not a stud)
    'spill': True,
    'spill_reach_r': 1.0,   # spill: tertiary component outside all studs within this x r of one
    'spill_max_studs': 6.,  # ... under this many stud areas
    'spill_raised_r': .5,   # ... whose faces are all flatter than this x seed height
}


# ----------------------------------------------------------------------------------------------- loading

def load_module(module_dir):
    """The high-poly arrays of a converted module (wearcut/prep + labels) and its palette; read-only."""
    module_dir = Path(module_dir)
    wdir = module_dir / 'wearcut'
    prep = wc.load_prep(wdir / 'prep')
    lab = np.load(wdir / 'labels.npz')['label'].astype(np.int64)
    if (wdir / 'feature_labels.npz').is_file():            # a module already rebuilt with a feature pass: analyse the labels before it
        lab = np.load(wdir / 'feature_labels.npz')['label_old'].astype(np.int64)
    manifest = json.loads((module_dir / 'reduced' / 'manifest.json').read_text(encoding='utf-8-sig'))
    pal = wc.palette_from_dict(json.loads((wdir / 'palette.json').read_text(encoding='utf-8-sig')))
    return dict(prep, lab=lab, pal=pal, manifest=manifest, module=manifest.get('module') or module_dir.name, dir=module_dir,
                plane=json.loads((wdir / 'plane.json').read_text(encoding='utf-8-sig')))


def tertiary_evidence(pal, colors, cos, garment):
    """Per face share of its three texture points that vote tertiary (classify_faces evidence), with the reduction's palette and the
    wearcut hue-shift guard. Returns (ev_ter (n,), tertiary class id)."""
    pal, _ = wc.check_hue_shift(pal, colors, garment)
    _, ev, _, pal, _ = sf.classify_faces(pal, colors, cos, hue_fit=False)
    ter = 1 + list(pal.roles).index('tertiary')
    return ev[:, ter], ter


# ----------------------------------------------------------------------------------------------- geometry

def face_geometry(v, f):
    tri = v[f]
    c = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    a = np.linalg.norm(c, axis=1) / 2
    n = c / np.maximum(2 * a, 1e-20)[:, None]
    return a, n, tri.mean(1)


def vertex_normals(v, f, n, a):
    vn = np.zeros_like(v)
    for k in range(3):
        np.add.at(vn, f[:, k], n * a[:, None])
    return vn / np.maximum(np.linalg.norm(vn, axis=1), 1e-20)[:, None]


def height_field(v, f, n, a, sigma, baseline_factor=2.):
    """Raised height per vertex: normal offset of the vertex from its heat-diffused position (uniform-crease cotangent Laplacian,
    (M + tau C) x = M v, tau = sigma^2 / 2) minus the same offset smoothed at baseline_factor x sigma. Returns (h, d)."""
    C, mass = wc.smoothing_operator(v, f, n, 180., 1.)
    vn = vertex_normals(v, f, n, a)
    S = np.stack([wc.solve_smooth(C, mass, v[:, k], sigma ** 2 / 2)[0] for k in range(3)], 1)
    d = ((v - S) * vn).sum(1)
    base, _ = wc.solve_smooth(C, mass, d, (baseline_factor * sigma) ** 2 / 2)
    return d - base, d


def adjacency(f):
    pairs = sf.edge_pairs(f)
    return pairs, sf.csr_adjacency(pairs, len(f))


def components(mask, pairs, n):
    """Connected components of the faces in `mask` over the face graph: (labels (n,) with -1 outside, count)."""
    keep = mask[pairs[:, 0]] & mask[pairs[:, 1]]
    p = pairs[keep]
    g = sparse.coo_matrix((np.ones(len(p), np.int8), (p[:, 0], p[:, 1])), shape=(n, n))
    _, lab = csgraph.connected_components(g, directed=False)
    out = np.full(n, -1, np.int64)
    idx = np.flatnonzero(mask)
    uniq, inv = np.unique(lab[idx], return_inverse=True)
    out[idx] = inv
    return out, len(uniq)


def erode(mask, adj, k):
    for _ in range(k):
        mask = mask & ~((adj @ (~mask).astype(np.float32)) > 0)
    return mask


def dilate(mask, adj, k):
    for _ in range(k):
        mask = mask | ((adj @ mask.astype(np.float32)) > 0)
    return mask


def blob_stats(cent, nrm, area, idx):
    """Shape of one face set: centroid, projected radius, roundness, fill, normal axis. The set is a roughly planar disc or a dome:
    the plane is the PCA plane of its face centroids (the smallest eigenvector is the axis), the projected area is sum(area |n.axis|)."""
    w = area[idx]
    c = (cent[idx] * w[:, None]).sum(0) / w.sum()
    X = cent[idx] - c
    cov = (X * w[:, None]).T @ X / w.sum()
    lam, vec = np.linalg.eigh(cov)                 # ascending
    axis = vec[:, 0]
    mean_n = (nrm[idx] * w[:, None]).sum(0)
    if axis @ mean_n < 0:
        axis = -axis
    proj = float((w * np.abs(nrm[idx] @ axis)).sum())
    x2 = X @ vec[:, 2]
    y2 = X @ vec[:, 1]
    rmax = float(np.sqrt(np.max(x2 ** 2 + y2 ** 2))) if len(idx) else 0.
    return {'centre': c, 'axis': axis, 'proj_area': proj, 'r': math.sqrt(proj / math.pi), 'area': float(w.sum()),
            'roundness': float(math.sqrt(max(lam[1], 0) / max(lam[2], 1e-20))),
            'fill': float(proj / max(math.pi * rmax ** 2, 1e-20)), 'rmax': rmax, 'faces': int(len(idx))}


def radius_estimate(cent, nrm, area, pairs, ev_ter, garment, edge, p):
    """Median projected radius of the compact yellow-evidence blobs (|texture| says where the studs are, however much of each
    the label field kept). Returns (r, info)."""
    n = len(area)
    mask = (ev_ter >= .5) & garment
    comp, cnt = components(mask, pairs, n)
    order = np.argsort(comp[comp >= 0], kind='stable')
    idx_all = np.flatnonzero(comp >= 0)[order]
    sc = comp[idx_all]
    bounds = np.flatnonzero(np.diff(sc)) + 1
    rs = []
    for grp in np.split(idx_all, bounds):
        if len(grp) < 12:
            continue
        s = blob_stats(cent, nrm, area, grp)
        if s['r'] >= 2.5 * edge and s['roundness'] >= .7 and s['fill'] >= .5:
            rs.append(s['r'])
    info = {'yellow_components': int(cnt), 'compact_blobs': len(rs)}
    if len(rs) >= p['min_blobs']:
        info.update(median_r=float(np.median(rs)), p10=float(np.quantile(rs, .1)), p90=float(np.quantile(rs, .9)))
        return float(np.median(rs)), info
    info['fallback'] = 'fewer than min_blobs compact yellow blobs'
    return p['default_radius'], info


# ----------------------------------------------------------------------------------------------- studs

def grow_extents(core_id, hf, garment, pairs, peak, steps, alpha, floor=0.):
    """Grow every stud core outward over garment faces with h >= alpha x its peak for `steps` rings (ids propagate through the pairs)."""
    ids = core_id.copy()
    a, b = pairs[:, 0], pairs[:, 1]
    for _ in range(steps):
        for src, dst in ((a, b), (b, a)):
            ok = (ids[src] >= 0) & (ids[dst] < 0) & garment[dst]
            ok &= hf[dst] >= np.maximum(alpha * peak[np.maximum(ids[src], 0)], floor)
            ids[dst[ok]] = ids[src[ok]]
    return ids


def smooth_extents(ext_id, garment, core, adj, pairs, rounds=2):
    """Majority smoothing of the stud extents on the face graph (rim jaggies): a non-member face with 2+ member neighbours joins (the id
    of its first member neighbour), a member (not core) face with 1 or fewer member neighbours leaves."""
    ids = ext_id.copy()
    a, b = pairs[:, 0], pairs[:, 1]
    for _ in range(rounds):
        member = ids >= 0
        cnt = np.asarray(adj @ member.astype(np.float32)).ravel()
        join = ~member & garment & (cnt >= 2)
        leave = member & ~core & (cnt <= 1)
        nid = np.full(len(ids), -1, np.int64)
        for src, dst in ((a, b), (b, a)):
            ok = member[src] & join[dst]
            nid[dst[ok]] = ids[src[ok]]
        ids[join] = nid[join]
        ids[leave] = -1
    return ids


def flood_majority(lab, free, voters, pairs, n, K, rounds=60):
    """Free faces take the majority role of voting neighbours, ring by ring."""
    adj = sf.csr_adjacency(pairs, n)
    out = lab.copy()
    free = free.copy()
    voters = voters.copy()
    for _ in range(rounds):
        if not free.any():
            break
        onehot = sparse.csr_matrix((voters.astype(np.float32), (np.arange(n), out)), shape=(n, K))
        votes = (adj @ onehot).toarray()
        has = free & (votes.sum(1) > 0)
        if not has.any():
            break
        out[has] = votes[has].argmax(1)
        voters |= has
        free &= ~has
    return out, int(free.sum())


def studs(v, f, lab, ev_ter, ter, params=None, log=print):
    """Stud cleanup of one high-poly label field. v (nv,3) f (n,3) the cleaned high-poly mesh, lab (n,) its role labels (0 base,
    1..K garment), ev_ter (n,) the texture evidence for tertiary, ter the tertiary class id. Returns a result dict:
    lab_new, stud_id (per face: id of the stud whose extent contains it, -1), spill (faces taken off tertiary), h (per vertex),
    info (numbers, `studs` rows, radius)."""
    p = dict(P, **(params or {}))
    t0 = time.monotonic()
    n = len(f)
    K = int(max(lab.max(), ter)) + 1
    a, nrm, cent = face_geometry(v, f)
    pairs, adj = adjacency(f)
    garment = lab > 0
    edge = float(math.sqrt(np.median(a) * 4 / math.sqrt(3)))
    info = {'faces': n, 'edge_length': edge, 'garment_area': float(a[garment].sum()), 'params': {k: p[k] for k in p}}
    if p['radius']:
        r, rinfo = float(p['radius']), {'given': True}
    else:
        r, rinfo = radius_estimate(cent, nrm, a, pairs, ev_ter, garment, edge, p)
    info['radius'] = r
    info['radius_info'] = rinfo
    log(f'STUDS radius {r:.4f} {rinfo}')
    sigma = p['sigma_r'] * r
    h_v, _ = height_field(v, f, nrm, a, sigma, p['baseline_r'])
    hf = h_v[f].mean(1)
    log(f'STUDS height field sigma {sigma:.4f} ({time.monotonic() - t0:.0f}s)')
    seed_thr = p['seed_r'] * r
    seeds = garment & (hf > seed_thr)
    k_open = max(1, int(round(p['open_r'] * r / (.87 * edge))))
    seeds = dilate(erode(seeds, adj, k_open), adj, k_open) & (hf > seed_thr) & garment
    k_close = max(1, int(round(p['close_r'] * r / (.87 * edge))))
    if k_close:                                  # flat-topped studs are rings of raised height (the plateau is flat): close them
        seeds = erode(dilate(seeds, adj, k_close), adj, k_close) & garment
    comp, cnt = components(seeds, pairs, n)
    info.update(seed_threshold=seed_thr, open_rings=k_open, close_rings=k_close, candidates=int(cnt))
    rows = []
    face_sets = []
    if cnt:
        idx_all = np.flatnonzero(comp >= 0)
        order = np.argsort(comp[idx_all], kind='stable')
        idx_all = idx_all[order]
        bounds = np.flatnonzero(np.diff(comp[idx_all])) + 1
        face_sets = np.split(idx_all, bounds)
    area_min = p['min_area_r'] * math.pi * r * r
    cand = []
    for ci, grp in enumerate(face_sets):
        s = blob_stats(cent, nrm, a, grp)
        if s['proj_area'] < area_min:
            continue
        s['id'] = ci
        s['peak'] = float(np.quantile(hf[grp], .9))
        s['yellow'] = float((ev_ter[grp] * a[grp]).sum() / a[grp].sum())
        s['old_tertiary'] = float(a[grp][lab[grp] == ter].sum() / a[grp].sum())
        cand.append(s)
    info['candidates_over_min_area'] = len(cand)
    lo_y, hi_y = p['size_yellow']
    lo_p, hi_p = p['size_plain']
    ok_y = []
    for s in cand:
        ratio = s['r'] / r
        s['r_ratio'] = ratio
        s['verdict'] = None
        if s['yellow'] >= p['yellow_min']:
            if lo_y <= ratio <= hi_y and s['roundness'] >= p['round_yellow'] and s['fill'] >= p['fill_min']:
                s['verdict'] = 'stud (yellow evidence)'
                ok_y.append(s)
            else:
                s['verdict'] = f"rejected (yellow, shape): r/R {ratio:.2f} round {s['roundness']:.2f} fill {s['fill']:.2f}"
    tree = cKDTree(np.array([s['centre'] for s in ok_y])) if ok_y else None
    # the seed (core) is a bit smaller than the dome: plain candidates are compared with the core size of the confirmed studs
    ref = float(np.median([s['r'] for s in ok_y])) if ok_y else .9 * r
    info['core_radius_ref'] = ref
    for s in cand:
        if s['verdict'] is not None:
            continue
        ratio = s['r'] / ref
        if not (lo_p <= ratio <= hi_p and s['roundness'] >= p['round_plain'] and s['fill'] >= p['fill_min']):
            s['verdict'] = f"rejected (plain, shape): r/R {ratio:.2f} round {s['roundness']:.2f} fill {s['fill']:.2f}"
        elif tree is None or tree.query(s['centre'])[0] > p['near']:
            s['verdict'] = 'rejected (plain, no yellow stud nearby)'
        else:
            s['verdict'] = 'stud (geometry only)'
    accepted = [s for s in cand if s['verdict'].startswith('stud')]
    # extents
    core_id = np.full(n, -1, np.int64)
    peak = np.zeros(len(accepted))
    for k, s in enumerate(accepted):
        core_id[face_sets[s['id']]] = k
        peak[k] = max(s['peak'], 1e-9)
    steps = max(1, int(round(p['reach_r'] * r / (.87 * edge))))
    ext_id = grow_extents(core_id, hf, garment, pairs, peak, steps, p['alpha'], p['floor_r'] * seed_thr) if accepted else core_id
    if accepted and p['smooth_rounds']:
        ext_id = smooth_extents(ext_id, garment, core_id >= 0, adj, pairs, p['smooth_rounds'])
    # flat studs (a disc with hardly any relief): compact yellow-evidence blobs the geometry did not explain are studs as they are
    tex_rows = []
    if p['texture_discs']:
        taken = dilate(ext_id >= 0, adj, 2)
        tmask = (ev_ter >= .5) & garment & ~taken
        tcomp, tcnt = components(tmask, pairs, n)
        if tcnt:
            idxs = np.flatnonzero(tcomp >= 0)
            order = np.argsort(tcomp[idxs], kind='stable')
            idxs = idxs[order]
            bounds = np.flatnonzero(np.diff(tcomp[idxs])) + 1
            nxt = len(accepted)
            for grp in np.split(idxs, bounds):
                if len(grp) < 12:
                    continue
                st = blob_stats(cent, nrm, a, grp)
                ratio = st['r'] / r
                if (p['tex_size'][0] <= ratio <= p['tex_size'][1] and st['roundness'] >= p['tex_round'] and st['fill'] >= p['tex_fill']
                        and float(np.median(hf[grp])) >= p['tex_raised'] * seed_thr):
                    ext_id[grp] = nxt
                    tex_rows.append({'id': nxt, 'centre': [round(float(x), 4) for x in st['centre']], 'r': round(st['r'], 4), 'faces': int(len(grp)),
                                     'area': float(a[grp].sum()), 'yellow_evidence': round(float((ev_ter[grp] * a[grp]).sum() / a[grp].sum()), 3),
                                     'old_tertiary_share': round(float(a[grp][lab[grp] == ter].sum() / a[grp].sum()), 3),
                                     'verdict': 'stud (texture disc, no dome)', 'roundness': round(st['roundness'], 3), 'fill': round(st['fill'], 3)})
                    nxt += 1
    new = lab.copy()
    in_ext = ext_id >= 0
    new[in_ext] = ter
    # spill
    spill = np.zeros(n, bool)
    spill_info = {'enabled': bool(p['spill'])}
    if p['spill'] and in_ext.any():
        cand_sp = (lab == ter) & ~in_ext
        scomp, scnt = components(cand_sp, pairs, n)
        etree = cKDTree(cent[in_ext])
        dist, _ = etree.query(cent[cand_sp])
        dist_full = np.full(n, np.inf)
        dist_full[cand_sp] = dist
        stud_area = float(np.median([s['proj_area'] for s in accepted])) if accepted else math.pi * r * r
        sp_rows = []
        if scnt:
            idxs = np.flatnonzero(scomp >= 0)
            order = np.argsort(scomp[idxs], kind='stable')
            idxs = idxs[order]
            bounds = np.flatnonzero(np.diff(scomp[idxs])) + 1
            for grp in np.split(idxs, bounds):
                comp_area = float(a[grp].sum())
                near = float(dist_full[grp].min())
                flat = bool((hf[grp] < p['spill_raised_r'] * seed_thr).all() or np.quantile(hf[grp], .9) < p['spill_raised_r'] * seed_thr)
                take = near <= p['spill_reach_r'] * r and comp_area <= p['spill_max_studs'] * stud_area and flat
                sp_rows.append({'faces': int(len(grp)), 'area': comp_area, 'near': near, 'flat': flat, 'spill': bool(take)})
                if take:
                    spill[grp] = True
        spill_info.update(components_near=len(sp_rows), spill_components=int(sum(1 for x in sp_rows if x['spill'])),
                          spill_area=float(a[spill].sum()), spill_faces=int(spill.sum()),
                          kept_tertiary_components=int(sum(1 for x in sp_rows if not x['spill'])))
        if spill.any():
            voters = (new > 0) & (new != ter) & ~spill
            new, left = flood_majority(new, spill, voters, pairs, n, K)
            spill_info['unresolved_faces'] = left
    # statistics
    stud_rows = []
    for k, s in enumerate(accepted):
        m = ext_id == k
        old_share = float(a[m][lab[m] == ter].sum() / max(a[m].sum(), 1e-20))
        kind = 'full' if old_share >= .95 else ('none' if old_share < .05 else 'partial')
        stud_rows.append({'id': k, 'centre': [round(float(x), 4) for x in s['centre']], 'r': round(s['r'], 4), 'faces': int(m.sum()), 'area': float(a[m].sum()),
                          'yellow_evidence': round(s['yellow'], 3), 'old_tertiary_share': round(old_share, 3), 'kind_before': kind,
                          'verdict': s['verdict'], 'peak_h': round(s['peak'], 5), 'roundness': round(s['roundness'], 3), 'fill': round(s['fill'], 3)})
    for tr in tex_rows:
        os_ = tr['old_tertiary_share']
        tr['kind_before'] = 'full' if os_ >= .95 else ('none' if os_ < .05 else 'partial')
        stud_rows.append(tr)
    kinds = {kk: sum(1 for x in stud_rows if x['kind_before'] == kk) for kk in ('full', 'partial', 'none')}
    rej = [{'centre': [round(float(x), 4) for x in s['centre']], 'r': round(s['r'], 4), 'yellow': round(s['yellow'], 3), 'verdict': s['verdict']}
           for s in cand if not s['verdict'].startswith('stud')]
    info.update(studs=len(accepted) + len(tex_rows), studs_flat_texture=len(tex_rows), studs_geometry_only=sum(1 for s in accepted if s['verdict'].endswith('only)')),
                before=kinds, fixed=kinds['partial'] + kinds['none'], rejected=rej,
                stud_area_total=float(a[in_ext].sum()), faces_to_tertiary=int((in_ext & (lab != ter)).sum()),
                area_to_tertiary=float(a[in_ext & (lab != ter)].sum()), spill=spill_info,
                tertiary_area_before=float(a[lab == ter].sum()), tertiary_area_after=float(a[new == ter].sum()),
                rows=stud_rows, seconds=round(time.monotonic() - t0, 1))
    return {'lab_new': new, 'stud_id': ext_id, 'spill': spill, 'h': h_v, 'info': info}


def transfer_role_array(v_norm, f, role_old, hp_cent, hp_area, lab_old, lab_new, K, min_changed=.25):
    """New role per triangle of a labelled full LOD (v_norm: normalised frame): a triangle whose covered high-poly area contains at least
    `min_changed` of changed faces takes the majority NEW role of what it covers (only if it was a garment triangle; base stays base);
    the others keep their role."""
    tris = v_norm[f]
    votes_new, tri_of = sf.cover_votes(tris, hp_cent, hp_area, lab_new, K)
    ch_area = np.zeros(len(tris))
    np.add.at(ch_area, tri_of, hp_area * (lab_old != lab_new))
    share = ch_area / np.maximum(votes_new.sum(1), 1e-20)
    take = (share >= min_changed) & (role_old > 0)
    new_role = role_old.copy()
    cand = votes_new.copy()
    cand[:, 0] = 0
    new_role[take] = cand[take].argmax(1)
    return new_role


def relabel_level(level_glb, manifest, pal, hp_cent, hp_area, lab_old, lab_new, K, min_changed=.25):
    """A labelled full LOD (lod_error_<lv>.glb), see transfer_role_array. Returns (v_glb, f, names_old, role_old, role_new, info)."""
    gv, gf, names = wc.read_glb(level_glb)
    lut = {nm: i for i, nm in enumerate(['base', *pal.roles])}
    role_old = np.array([lut.get(str(nm), 0) for nm in names], np.int64)
    vn = wc.from_glb(gv, manifest)
    new_role = transfer_role_array(vn, gf, role_old, hp_cent, hp_area, lab_old, lab_new, K, min_changed)
    la = wc.tri_area(vn, gf)
    ter = 1 + list(pal.roles).index('tertiary')
    info = {'faces': int(len(gf)), 'faces_changed': int((new_role != role_old).sum()),
            'tertiary_area_before': float(la[role_old == ter].sum()), 'tertiary_area_after': float(la[new_role == ter].sum())}
    return gv, gf, names, role_old, new_role, info


# ----------------------------------------------------------------------------------------------- driver

def mat_tables(pal):
    """Material names and sRGB colours for write_glb (index 0 = base)."""
    names = ['base', *pal.roles]
    rgb = [(.902, .098, .137)]
    for _, hex_ in pal.garments:
        h = hex_.lstrip('#')
        rgb.append(tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)))
    return names, rgb


def run_module(module_dir, out_dir, recut=False, levels=('0p4', '0p2', '0p1'), params=None, force=False, log=print, pieces=False):
    module_dir, out_dir = Path(module_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if (out_dir / 'studs_labels.npz').exists() and not force:
        raise SystemExit(f'{out_dir}/studs_labels.npz exists (use --force)')
    M = load_module(module_dir)
    ev_ter, ter = tertiary_evidence(M['pal'], M['colors'], M['cos'], M['lab'] > 0)
    res = studs(M['v'], M['f'], M['lab'], ev_ter, ter, params, log)
    info = res['info']
    info['module_dir'] = str(module_dir)
    np.savez_compressed(out_dir / 'studs_labels.npz', label_old=M['lab'].astype(np.int8), label_new=res['lab_new'].astype(np.int8),
                        stud_id=res['stud_id'].astype(np.int32), spill=res['spill'], h=res['h'].astype(np.float32), tertiary=np.int8(ter))
    (out_dir / 'studs.json').write_text(json.dumps(wc.jsonable(info), indent=1) + '\n')
    if recut:
        import feature_hooks
        a, nrm, cent = face_geometry(M['v'], M['f'])
        rdir = out_dir / 'recut'
        rdir.mkdir(exist_ok=True)
        K = len(M['pal'].roles) + 1
        pal_dict = json.loads((module_dir / 'wearcut' / 'palette.json').read_text(encoding='utf-8-sig'))
        t = time.monotonic()
        result = wc.run(module_dir / 'wearcut' / 'prep', rdir, M['module'], pal_dict, M['plane'], tuple(levels), True,
                        {'label_hook': feature_hooks.make_hook('studs', params)}, save_labels=True)
        info['recut'] = {'faces_overridden': int((res['lab_new'] != M['lab']).sum()), 'seconds': round(time.monotonic() - t, 1),
                         'fallback': result.get('fallback'),
                         'levels': {lv: {'faces': r_.get('faces'), 'area': r_.get('area')} for lv, r_ in result['levels'].items()}}
        names, rgb = mat_tables(M['pal'])
        lvl = {}
        for lv in levels:
            glb = module_dir / 'reduced' / f'lod_error_{lv}.glb'
            if not glb.is_file():
                continue
            gv, gf, nm, r_old, r_new, linfo = relabel_level(glb, M['manifest'], M['pal'], cent, a, M['lab'], res['lab_new'], K)
            wc.write_glb(rdir / f'lod_error_{lv}__studs.glb', gv, gf, r_new, names, rgb)
            np.savez_compressed(rdir / f'lod_{lv}_labelled.npz', v=wc.from_glb(gv, M['manifest']).astype(np.float32), f=gf.astype(np.int32),
                                role_old=r_old.astype(np.int8), role_new=r_new.astype(np.int8))
            lvl[lv] = linfo
        info['relabelled_levels'] = lvl
        (out_dir / 'studs.json').write_text(json.dumps(wc.jsonable(info), indent=1) + '\n')
    if pieces:
        import feature_hooks
        sfdir = module_dir / 'segment_first'
        if not (sfdir / 'prep' / 'prep.npz').is_file():
            info['pieces'] = {'skipped': 'no segment_first/prep in this module'}
        else:
            t = time.monotonic()
            pdir = out_dir / 'pieces_run'
            result, seg = sf.process(module_dir, pdir, tuple(levels), render=False, blend=False, prep_dir=sfdir / 'prep',
                                     label_hook=feature_hooks.make_hook('studs', params))
            r_ = seg['feature_labels']
            info['pieces'] = {'seconds': round(time.monotonic() - t, 1), 'studs': r_['studs'], 'before': r_['before'], 'fixed': r_['fixed'],
                              'wearable_triangles': {lv: l_['wearable_triangles'] for lv, l_ in seg['levels'].items()}}
        (out_dir / 'studs.json').write_text(json.dumps(wc.jsonable(info), indent=1) + '\n')
    return info


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('mode', choices=['studs'])
    ap.add_argument('module_dir')
    ap.add_argument('--out', required=True)
    ap.add_argument('--recut', action='store_true', help='also re-run the wearable cut and relabel the full LODs, into <out>/recut')
    ap.add_argument('--pieces', action='store_true', help='also re-run the segment-first rigid split (modules with segment_first/) into <out>/pieces_run')
    ap.add_argument('--levels', default='0p4,0p2,0p1')
    ap.add_argument('--radius', type=float, help='stud radius in normalised units (height 2); default: estimated from the yellow texture blobs')
    ap.add_argument('--alpha', type=float)
    ap.add_argument('--no-spill', action='store_true')
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args(argv)
    params = {}
    if a.radius:
        params['radius'] = a.radius
    if a.alpha is not None:
        params['alpha'] = a.alpha
    if a.no_spill:
        params['spill'] = False
    info = run_module(a.module_dir, a.out, a.recut, tuple(a.levels.split(',')), params, a.force, pieces=a.pieces)
    brief = {k: info[k] for k in ('radius', 'studs', 'studs_geometry_only', 'before', 'fixed', 'faces_to_tertiary', 'seconds')}
    brief['spill'] = {k: v_ for k, v_ in info['spill'].items() if k in ('spill_components', 'spill_area', 'spill_faces')}
    print('STUDS_DONE', json.dumps(wc.jsonable(brief)))


if __name__ == '__main__':
    main()
