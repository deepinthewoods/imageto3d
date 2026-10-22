"""Seam quality of rigid pieces: do neighbouring pieces meet along one shared, clean, smooth seam at rest?

Usage (venv python with numpy + scipy; heights are in normalised units, the model is 2 tall, results in % of height):
    python tools/tripo_pipeline/seam_metrics.py <segment-first out dir> [--levels 0p4,0p2,0p1] [--prep-dir DIR] [--json out.json]
        reads <out>/levels/<lv>.npz (v_norm, f_norm, piece), <out>/highpoly_labels.npz and prep/prep.npz (high-poly seams)

Per level and per pair of pieces (A, B) that touch on the high-poly mesh:
  * welded (by position) boundary edges of every piece; the A|B seam = boundary edges present in both;
  * gap: boundary vertices of A that lie in the seam zone of A|B (within `zone` of B's boundary) but are not on B's
    boundary (and the same for B), with their distance to B's boundary polyline: `tjunction` ones (< 1e-5 of the height)
    sit on the other piece's edge but are not its vertices (a welded seam keeps a hairline crack there), the others are holes;
  * jaggedness of every seam chain: length / length of the Gaussian-smoothed polyline (sigma = `sigma` of the height),
    rms / max deviation to the smoothed polyline, and reversals (turns of more than 90 degrees between consecutive edges);
  * boundary loops of the whole welded wearable (neck, cuffs, hems and holes); `thin_holes` = closed ones whose mean width
    (2 x area / perimeter) is under `thin_hole_pct` of the height, on a component that has another boundary loop (a slit of base between
    two garment pieces; the single outer boundary of a small island is not a hole).
The same chain metrics are computed for the high-poly seam (`highpoly_seams`) to show what the cut starts from.
"""
import argparse
import json
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

HEIGHT = 2.0
POS_TOL = 1e-6


def weld(v, tol=POS_TOL):
    """Vertex id per position (positions equal within `tol` share an id): (new id per vertex, unique positions)."""
    key = np.round(v / tol).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    return inv.ravel(), v[first]


def edge_keys(f):
    a = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
    b = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
    return np.minimum(a, b).astype(np.int64), np.maximum(a, b).astype(np.int64)


def boundary_edges(f):
    """(E, 2) edges used by exactly one face of `f`, and the number of edges used by 3+ faces."""
    lo, hi = edge_keys(f)
    key = lo * (int(f.max()) + 1) + hi
    u, idx, c = np.unique(key, return_index=True, return_counts=True)
    one = c == 1
    return np.stack([lo[idx[one]], hi[idx[one]]], 1), int((c > 2).sum())


def chains_of(edges):
    """Ordered vertex chains of an edge set (loops closed: first == last). Chains break at vertices of degree != 2."""
    nbrs = {}
    for a, b in edges.tolist():
        nbrs.setdefault(a, []).append(b)
        nbrs.setdefault(b, []).append(a)
    used = set()
    out = []

    def walk(s, n):
        ch = [s, n]
        used.add((min(s, n), max(s, n)))
        cur = n
        while len(nbrs[cur]) == 2 and cur != s:
            nxt = [x for x in nbrs[cur] if (min(cur, x), max(cur, x)) not in used]
            if not nxt:
                break
            used.add((min(cur, nxt[0]), max(cur, nxt[0])))
            cur = nxt[0]
            ch.append(cur)
        return ch

    for s in sorted(v for v, ns in nbrs.items() if len(ns) != 2):
        for x in nbrs[s]:
            if (min(s, x), max(s, x)) not in used:
                out.append(walk(s, x))
    for s in sorted(nbrs):
        for x in nbrs[s]:
            if (min(s, x), max(s, x)) not in used:
                out.append(walk(s, x))
    return out


def resample(P, step, closed):
    seg = np.linalg.norm(np.diff(P, axis=0), axis=1)
    arc = np.r_[0, np.cumsum(seg)]
    n = max(int(round(arc[-1] / step)), 2)
    t = np.linspace(0, arc[-1], n + 1)
    return np.stack([np.interp(t, arc, P[:, k]) for k in range(3)], 1)


def smooth(P, sigma_samples, closed):
    """Gaussian smoothing along the polyline; open ends are reflected about the end points so they stay in place."""
    r = int(max(3 * sigma_samples, 1))
    w = np.exp(-.5 * (np.arange(-r, r + 1) / max(sigma_samples, 1e-9)) ** 2)
    w /= w.sum()
    n = len(P)
    if closed:
        idx = np.arange(-r, n + r) % (n - 1)
        pad = P[idx]
    else:
        pad = np.concatenate([2 * P[0] - P[1:r + 1][::-1], P, 2 * P[-1] - P[-r - 1:-1][::-1]])
    return np.stack([np.convolve(pad[:, k], w, mode='valid') for k in range(3)], 1)


def chain_jaggedness(P, closed, sigma=.015, step=.0025):
    """Metrics of one chain (positions P in normalised units). Lengths and deviations in % of the height."""
    raw_len = float(np.linalg.norm(np.diff(P, axis=0), axis=1).sum())
    if len(P) < 3 or raw_len < 4 * sigma * HEIGHT:                 # shorter than the smoothing window: nothing to judge
        return None
    seg = np.diff(P, axis=0)
    seg = seg[np.linalg.norm(seg, axis=1) > 1e-12]
    u = seg / np.linalg.norm(seg, axis=1)[:, None]
    cos = (u[1:] * u[:-1]).sum(1)
    reversals = int((cos < 0).sum())
    R = resample(P, step, closed)
    S = smooth(R, sigma * HEIGHT / step, closed)
    sm_len = float(np.linalg.norm(np.diff(S, axis=0), axis=1).sum())
    dev = np.linalg.norm(R - S, axis=1)
    return {'length_pct': 100 * raw_len / HEIGHT, 'length_ratio': raw_len / max(sm_len, 1e-12), 'reversals': reversals,
            'reversals_per_100pct': reversals / max(100 * raw_len / HEIGHT, 1e-9) * 100, 'rms_dev_pct': 100 * float(np.sqrt((dev ** 2).mean())) / HEIGHT,
            'max_dev_pct': 100 * float(dev.max()) / HEIGHT, 'vertices': int(len(P))}


def merge_chain_metrics(items):
    """Length-weighted summary of chain_jaggedness dicts."""
    items = [i for i in items if i]
    if not items:
        return None
    L = np.array([i['length_pct'] for i in items])
    w = L / L.sum()
    return {'chains': len(items), 'length_pct': float(L.sum()), 'length_ratio': float(sum(i['length_ratio'] * x for i, x in zip(items, w))),
            'reversals': int(sum(i['reversals'] for i in items)), 'reversals_per_100pct': float(sum(i['reversals'] for i in items) / L.sum() * 100),
            'rms_dev_pct': float(np.sqrt(sum(i['rms_dev_pct'] ** 2 * x for i, x in zip(items, w)))),
            'max_dev_pct': float(max(i['max_dev_pct'] for i in items)), 'vertices': int(sum(i['vertices'] for i in items))}


def polyline_tree(P_by_edge, edges, step=.0007):
    """k-d tree over points sampled every `step` along the given edges (positions P_by_edge[edges])."""
    a, b = P_by_edge[edges[:, 0]], P_by_edge[edges[:, 1]]
    L = np.linalg.norm(b - a, axis=1)
    k = np.maximum(np.ceil(L / step).astype(int), 1)
    owner = np.repeat(np.arange(len(edges)), k + 1)
    off = np.arange(len(owner)) - np.repeat(np.cumsum(k + 1) - (k + 1), k + 1)
    t = off / np.repeat(k, k + 1)
    pts = a[owner] + (b[owner] - a[owner]) * t[:, None]
    return cKDTree(pts)


def vertex_components(f, n):
    """Connected-component id per vertex of the face set `f` (vertices are connected through shared faces)."""
    from scipy import sparse
    from scipy.sparse import csgraph
    a = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
    b = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
    g = sparse.coo_matrix((np.ones(len(a), np.int8), (a, b)), shape=(n, n))
    return csgraph.connected_components(g, directed=False)[1]


def point_segment_distance(P, A, B):
    """Distance of every point of P (m, 3) to the nearest of the segments A[i]-B[i] (n, 3), exact, chunked."""
    out = np.empty(len(P))
    ab = B - A
    den = np.maximum((ab * ab).sum(1), 1e-30)
    for s in range(0, len(P), 256):
        p = P[s:s + 256, None, :]
        t = np.clip(((p - A[None]) * ab[None]).sum(2) / den[None], 0, 1)
        q = A[None] + t[..., None] * ab[None]
        out[s:s + 256] = np.linalg.norm(p - q, axis=2).min(1)
    return out


def piece_boundaries(vw, f, piece, ids):
    """{piece id: (boundary edges (welded ids), non-manifold edge count)}."""
    out = {}
    for p in ids:
        out[int(p)] = boundary_edges(f[piece == p])
    return out


def seam_pairs_from_graph(f, piece):
    """Pairs of piece ids that share at least one edge (the pieces came from one connected surface, same vertex ids)."""
    lo, hi = edge_keys(f)
    key = lo * (int(f.max()) + 1) + hi
    order = np.argsort(key, kind='stable')
    key, face = key[order], np.tile(np.arange(len(f)), 3)[order]
    same = key[1:] == key[:-1]
    i = np.flatnonzero(same)
    pa, pb = piece[face[i]], piece[face[i + 1]]
    m = pa != pb
    pairs = {tuple(sorted(x)) for x in zip(pa[m].tolist(), pb[m].tolist())}
    return sorted(pairs)


def level_report(v, f, piece, names, zone=.008, sigma=.015, hp_seam_pts=None, thin_hole_pct=1.):
    """Seam / gap / hole / jaggedness report of one reduced level. v, f: normalised positions and faces of ALL pieces
    (shared vertex ids are not trusted: vertices are welded by position); piece: piece id per face; names: id -> name."""
    wid, wpos = weld(v)
    fw = wid[f]
    keep = (fw[:, 0] != fw[:, 1]) & (fw[:, 1] != fw[:, 2]) & (fw[:, 0] != fw[:, 2])
    fw, piece = fw[keep], piece[keep]
    ids = sorted(int(p) for p in np.unique(piece))
    bnd = piece_boundaries(wpos, fw, piece, ids)
    pairs = sorted(hp_seam_pts) if hp_seam_pts else seam_pairs_from_graph(fw, piece)
    pairs = [(a, b) for a, b in pairs if a in bnd and b in bnd]
    zone_abs = zone * HEIGHT
    rep = {'pairs': {}, 'pieces': {}}
    keyof = lambda e: e[:, 0] * (len(wpos) + 1) + e[:, 1]                      # noqa: E731
    all_chains = []
    for a, b in pairs:
        ea, eb = bnd[a][0], bnd[b][0]
        if not len(ea) or not len(eb):
            continue
        ka, kb = keyof(ea), keyof(eb)
        ma, mb = np.isin(ka, kb), np.isin(kb, ka)
        seam_edges = ea[ma]
        chains = chains_of(seam_edges) if len(seam_edges) else []
        jag = []
        for ch in chains:
            jag.append(chain_jaggedness(wpos[ch], len(ch) > 2 and ch[0] == ch[-1], sigma))
        all_chains += jag
        entry = {'seam_edges': int(ma.sum()), 'seam_length_pct': float(100 * np.linalg.norm(wpos[seam_edges[:, 0]] - wpos[seam_edges[:, 1]], axis=1).sum() / HEIGHT),
                 'jaggedness': merge_chain_metrics(jag)}
        for x, y, ex, ey, mx in ((a, b, ea, eb, ma), (b, a, eb, ea, mb)):
            vx, vy = np.unique(ex), np.unique(ey)
            tree_y = polyline_tree(wpos, ey)
            d, _ = tree_y.query(wpos[vx])
            if hp_seam_pts:                                                    # the seam zone of x|y: near the high-poly cut itself
                sp_, ends = hp_seam_pts[(a, b)]
                near = cKDTree(sp_).query(wpos[vx])[0] < zone_abs
                if len(ends):                                                  # the open ends of a seam chain: the natural boundaries part there
                    near &= cKDTree(ends).query(wpos[vx])[0] > 2.5 * zone_abs
            else:
                near = d < zone_abs                                            # in the seam zone of x|y
            on_y = np.isin(vx, vy)
            bad = near & ~on_y
            # exact distance to y's boundary segments for the bad ones
            dist = np.zeros(len(vx))
            if bad.any():
                dist[bad] = point_segment_distance(wpos[vx[bad]], wpos[ey[:, 0]], wpos[ey[:, 1]])
            tj = bad & (dist < 1e-5 * HEIGHT)
            gap = bad & ~tj
            entry[f'{names.get(x, x)}_vertices_not_on_{names.get(y, y)}'] = {
                'zone_vertices': int(near.sum()), 'not_on_other': int(bad.sum()), 'tjunction': int(tj.sum()), 'gap': int(gap.sum()),
                'gap_max_pct': float(100 * dist[gap].max() / HEIGHT) if gap.any() else 0., 'gap_mean_pct': float(100 * dist[gap].mean() / HEIGHT) if gap.any() else 0.,
                'unmatched_boundary_edges_in_zone': int((~mx & (near[np.searchsorted(vx, ex[:, 0])] & near[np.searchsorted(vx, ex[:, 1])])).sum())}
        rep['pairs'][f'{names.get(a, a)}|{names.get(b, b)}'] = entry
    # holes: boundary loops of the whole wearable (all pieces welded): openings that no piece closes
    for p in ids:
        rep['pieces'][names.get(p, str(p))] = {'faces': int((piece == p).sum()), 'boundary_edges': int(len(bnd[p][0])), 'non_manifold_edges': bnd[p][1]}
    ew, _ = boundary_edges(fw)
    loops = chains_of(ew) if len(ew) else []
    comp = vertex_components(fw, len(wpos)) if len(ew) else None
    per_comp = {}
    for ch in loops:
        per_comp[comp[ch[0]]] = per_comp.get(comp[ch[0]], 0) + 1
    info = []
    for ch in loops:
        Pl = wpos[np.array(ch[:-1])]
        L = float(np.linalg.norm(wpos[np.array(ch[:-1])] - wpos[np.array(ch[1:])], axis=1).sum())
        closed = bool(ch[0] == ch[-1])
        area = float(np.linalg.norm(np.cross(Pl[1:] - Pl[0], np.roll(Pl[1:], -1, axis=0) - Pl[0]).sum(0))) / 2 if len(Pl) > 2 else 0.
        info.append({'length_pct': 100 * L / HEIGHT, 'closed': closed, 'width_pct': (100 * 2 * area / max(L, 1e-12) / HEIGHT) if closed else None,
                     'island': per_comp[comp[ch[0]]] == 1, 'centre': [round(float(x), 3) for x in Pl.mean(0)]})
    thin = [i for i in info if i['closed'] and not i['island'] and i['width_pct'] < thin_hole_pct]
    rep['wearable_loops'] = sorted(({k: (round(x, 2) if isinstance(x, float) else x) for k, x in i.items()} for i in info), key=lambda i: -i['length_pct'])
    rep['thin_hole_list'] = [{k: (round(x, 2) if isinstance(x, float) else x) for k, x in i.items()} for i in thin]
    rep['all_seams'] = merge_chain_metrics(all_chains)
    gaps = [(k, kk, vv) for k, e in rep['pairs'].items() for kk, vv in e.items() if isinstance(vv, dict) and 'gap' in vv]
    rep['gap_vertices'] = int(sum(vv['gap'] for _, _, vv in gaps))
    rep['tjunction_vertices'] = int(sum(vv['tjunction'] for _, _, vv in gaps))
    rep['gap_max_pct'] = float(max([vv['gap_max_pct'] for _, _, vv in gaps] + [0.]))
    rep['thin_holes'] = len(thin)
    return rep


def highpoly_seams(v, f_wear, piece, names, sigma=.015, pts=None):
    """Jaggedness of the A|B seams on the high-poly wearable graph (before any reduction): {A|B: merged chain metrics}.
    `pts` (a dict) receives {(A, B): dense points along the seam} for level_report's seam zones."""
    out = {}
    for a, b in seam_pairs_from_graph(f_wear, piece):
        sel_a = f_wear[piece == a]
        ea, _ = boundary_edges(sel_a)
        eb, _ = boundary_edges(f_wear[piece == b])
        ka, kb = ea[:, 0] * (len(v) + 1) + ea[:, 1], eb[:, 0] * (len(v) + 1) + eb[:, 1]
        se = ea[np.isin(ka, kb)]
        if not len(se):
            continue
        jag = [chain_jaggedness(v[ch], len(ch) > 2 and ch[0] == ch[-1], sigma) for ch in chains_of(se)]
        if pts is not None:
            cs = chains_of(se)
            pts[(a, b)] = (np.concatenate([resample(v[ch], .002, False) for ch in cs]),
                           np.array([v[ch[e]] for ch in cs if ch[0] != ch[-1] for e in (0, -1)]).reshape(-1, 3))
        out[f'{names.get(a, a)}|{names.get(b, b)}'] = merge_chain_metrics(jag)
    return out


def hp_reference(out_dir, prep_dir=None, section_classes=(0, 1, 3)):
    """High-poly reference of a segment_first run: wearable faces (normalised), their garment class, section, area, and the
    colour boundary (midpoints of face pairs whose classes differ, both among `section_classes`)."""
    import segment_first as sf
    out_dir = Path(out_dir)
    h = np.load(out_dir / 'highpoly_labels.npz')
    z = np.load(Path(prep_dir) / 'prep.npz' if prep_dir else out_dir / 'prep' / 'prep.npz')
    v, f = z['v'].astype(np.float64), z['f'].astype(np.int64)
    fw = f[h['wear']]
    role = h['label'][h['wear']].astype(np.int64) - 1
    cent = v[fw].mean(1)
    t = v[fw]
    area = np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1) / 2
    pairs = sf.edge_pairs(fw)
    diff = (role[pairs[:, 0]] != role[pairs[:, 1]]) & np.isin(role[pairs[:, 0]], section_classes) & np.isin(role[pairs[:, 1]], section_classes)
    sel = pairs[diff]
    return {'cent': cent, 'area': area, 'role': role, 'piece': h['piece'].astype(np.int64), 'tree': cKDTree((cent[sel[:, 0]] + cent[sel[:, 1]]) / 2)}


def border_mixing(v, f, ref, n_roles=4, n_pieces=8):
    """Share of the wearable's triangle area whose top section / garment class covers < 75% of the high-poly area under it."""
    import segment_first as sf
    tris = v[f]
    area = np.linalg.norm(np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1) / 2
    rv, _ = sf.cover_votes(tris, ref['cent'], ref['area'], ref['role'], n_roles)
    pv, _ = sf.cover_votes(tris, ref['cent'], ref['area'], ref['piece'], n_pieces)
    return {'piece_mixed_area_share': sf.top_share_stats(pv, area)['mixed_area_share'], 'role_mixed_area_share': sf.top_share_stats(rv, area)['mixed_area_share']}


def colour_alignment(v, f, piece, ref):
    """Distance (% of height) of the cuts between pieces (3 points per cut edge) to the nearest high-poly colour boundary."""
    wid, wpos = weld(v)
    fw = wid[f]
    pts = []
    ids = [int(p) for p in np.unique(piece)]
    bnd = {p: boundary_edges(fw[piece == p])[0] for p in ids}
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            ka = bnd[a][:, 0] * (len(wpos) + 1) + bnd[a][:, 1]
            kb = bnd[b][:, 0] * (len(wpos) + 1) + bnd[b][:, 1]
            e = bnd[a][np.isin(ka, kb)]
            if len(e):
                p0, p1 = wpos[e[:, 0]], wpos[e[:, 1]]
                pts += [p0, (p0 + p1) / 2, p1]
    d = ref['tree'].query(np.concatenate(pts))[0] / HEIGHT * 100
    return {'mean_pct': float(d.mean()), 'p95_pct': float(np.percentile(d, 95)), 'max_pct': float(d.max())}


def run(out_dir, levels, prep_dir=None, hp=True, zone=.008, sigma=.015):
    out_dir = Path(out_dir)
    h = np.load(out_dir / 'highpoly_labels.npz')
    names = {int(i): str(n) for i, n in zip(h['piece_ids'], h['piece_names'])}
    res = {'levels': {}}
    pts = None
    if hp:
        pts = {}
        z = np.load(Path(prep_dir) / 'prep.npz' if prep_dir else out_dir / 'prep' / 'prep.npz')
        f = z['f'][h['wear']]
        res['highpoly_seams'] = highpoly_seams(z['v'].astype(np.float64), f, h['piece'].astype(np.int64), names, sigma, pts)
    ref = hp_reference(out_dir, prep_dir) if hp else None
    for lv in levels:
        l = np.load(out_dir / 'levels' / f'{lv}.npz')
        v, f, pc = l['v_norm'].astype(np.float64), l['f_norm'], l['piece'].astype(np.int64)
        res['levels'][lv] = level_report(v, f, pc, names, zone, sigma, pts or None)
        res['levels'][lv]['triangles'] = int(len(f))
        if ref is not None:
            res['levels'][lv]['border_mixing'] = border_mixing(v, f, ref)
            res['levels'][lv]['colour_alignment'] = colour_alignment(v, f, pc, ref)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('out_dir')
    ap.add_argument('--levels', default='0p4,0p2,0p1')
    ap.add_argument('--prep-dir')
    ap.add_argument('--no-highpoly', action='store_true')
    ap.add_argument('--json')
    a = ap.parse_args(argv)
    res = run(a.out_dir, a.levels.split(','), a.prep_dir, not a.no_highpoly)
    text = json.dumps(res, indent=1, default=float)
    if a.json:
        Path(a.json).write_text(text)
    for lv, r in res['levels'].items():
        print(lv, 'tris', r['triangles'], 'gap_vertices', r['gap_vertices'], 'tjunction', r['tjunction_vertices'], 'gap_max%', round(r['gap_max_pct'], 3),
              'thin_holes', r['thin_holes'], 'seams', json.dumps(r['all_seams'], default=float))
        for k, e in r['pairs'].items():
            if k in ('chest|pelvis', 'pelvis|chest'):
                print('  ', k, json.dumps(e, default=float))
    if 'highpoly_seams' in res:
        for k, e in res['highpoly_seams'].items():
            print('hp', k, json.dumps(e, default=float))


if __name__ == '__main__':
    main()
