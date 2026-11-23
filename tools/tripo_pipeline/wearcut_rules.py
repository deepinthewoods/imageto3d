"""Module rules shared by the wearable-by-cut step (numpy only, so Blender's python can import it as well as the venv one).

`wearcut.py` (numpy + scipy) cuts the garment out of the high-poly mesh; `wearcut_blender.py` (bpy) builds its inputs and
exports its outputs. Both need to know, per body part of a module, where garment may be (module prior), where it is asymmetric
by design, and which side a part is on.
"""
import json
import struct
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]

# Cost of a garment label on a body part (Potts unary, same scale as the vote mass of a high-poly face, <= 1): the module prior.
BARE_COST = 1.2

# Body-part families that are asymmetric by design in a one-sided module, where nothing is ever pooled. Same as
# symmetry.EXPECTED_ASYMMETRIC except the outfit: only its FOOT (the right footwear); the robe hem on the lower legs is
# symmetric, and a boot shaft or any other one-sided garment on the lower leg is still caught by the rule "well seen on both
# sides and disagree: do not pool".
EXPECTED_ASYMMETRIC = {
    'right_gauntlet_boot': ('hand', 'forearm_stretch', 'foot', 'leg_stretch'),
    'outfit': ('foot',),
    'body_armor': (),
}


def part_family(name):
    """`arm_stretch.l` -> `arm_stretch`; central parts (`.x`) and unsided parts (`skirt`) keep their name."""
    return name[:-2] if name[-2:] in ('.l', '.r', '.x') else name


def part_side(name):
    """0 central/unsided, 1 anatomical left (+X), 2 anatomical right (-X)."""
    return 1 if name.endswith('.l') else 2 if name.endswith('.r') else 0


def part_tables(names, module):
    """Per body part id: side, family, expected-asymmetric flag, the cost of garment on that part (the module prior: where the
    base must stay bare) and `allowed` (garment may be there: the same set as face_labels' wear prior >= OFF_BODY_MIN_PRIOR)."""
    side = np.array([part_side(n) for n in names])
    fam = [part_family(n) for n in names]
    expected = EXPECTED_ASYMMETRIC.get(module, ())
    exp = np.array([f_ in expected for f_ in fam])
    cost = np.zeros(len(names))
    for i, n in enumerate(names):
        if module == 'body_armor':
            cost[i] = BARE_COST if n.startswith(('forearm_stretch.', 'hand.', 'leg_stretch.', 'foot.', 'head.')) else 0.
        elif module == 'right_gauntlet_boot':
            if n in ('forearm_stretch.r', 'hand.r', 'leg_stretch.r', 'foot.r'):
                cost[i] = 0.
            elif n == 'thigh_stretch.r':
                cost[i] = .6
            elif n.endswith('.l'):
                cost[i] = 3.        # the left limb is discarded: the left piece is the reflection of the right one
            else:
                cost[i] = BARE_COST
        elif module == 'outfit':
            cost[i] = BARE_COST if n.startswith(('head.', 'hand.')) or n == 'foot.l' else 0.
    return {'side': side, 'family': fam, 'expected': exp, 'cost': cost, 'allowed': cost < 1.}


def mirror_part_ids(names):
    """Part id of the anatomical counterpart (`.r` <-> `.l`); central and unsided parts map to themselves."""
    ids = {n: i for i, n in enumerate(names)}
    out = np.arange(len(names))
    for i, n in enumerate(names):
        if n.endswith('.r'):
            out[i] = ids.get(n[:-2] + '.l', i)
        elif n.endswith('.l'):
            out[i] = ids.get(n[:-2] + '.r', i)
    return out


def wearable_record(info):
    """What a reduction recorded about the wearable (manifest `wearable`) for the job record (.tripo-job.json module_conversion):
    requested / used method, status, fallback reason, the method of every level and the wearcut cache folder (relative to the project)."""
    w = info.get('wearable') or {}
    levels = {lv: (v.get('wearable_method') or 'faces') for lv, v in (info.get('levels') or {}).items()}
    out = {'requested': w.get('requested', 'faces'), 'method': w.get('method', 'faces'), 'status': w.get('status', 'done'), 'levels': levels}
    if w.get('fallback_reason'):
        out['fallback_reason'] = w['fallback_reason']
    if w.get('footwear'):
        out['footwear'] = w['footwear']
    if (w.get('feature_labels') or {}).get('spec'):
        out['feature_labels'] = w['feature_labels']['spec']
    if w.get('dir'):
        try:
            out['dir'] = str(Path(w['dir']).resolve().relative_to(ROOT))
        except ValueError:
            out['dir'] = str(w['dir'])
    return out


def palette_dict(info):
    """The palette dict a reduction used: manifest `palette` (hue_shift included), or for a manifest converted before palette.py existed
    its recorded legacy swatches (tricolor_v1), else the legacy default."""
    if info.get('palette'):
        return info['palette']
    from palette import Palette
    for entry in (info.get('levels') or {}).values():
        sw = (entry.get('material') or {}).get('swatches')
        if sw:
            return Palette.from_swatches(sw, 'manifest swatches (legacy tricolor_v1)').describe()
    return Palette.legacy_default().describe()


# ----------------------------------------------------------------------------------------------- consistent winding

def _manifold_pairs(f):
    """Face pairs sharing an edge used by exactly two faces: (face a, face b, same-direction flag, number of boundary edges per face,
    edge vertices (m, 2))."""
    n = len(f)
    a = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
    b = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
    face = np.tile(np.arange(n), 3)
    key = np.minimum(a, b).astype(np.int64) * (int(f.max()) + 1) + np.maximum(a, b)
    order = np.argsort(key, kind='stable')
    key, a, face = key[order], a[order], face[order]
    bv = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])[order]
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    counts = np.diff(np.r_[starts, len(key)])
    two = starts[counts == 2]
    boundary_face = face[starts[counts == 1]]
    return face[two], face[two + 1], (a[two] == a[two + 1]).astype(np.int8), np.bincount(boundary_face, minlength=n), np.stack([a[two], bv[two]], 1)


def _flood_components(n, a, b):
    """Connected components of the face graph with edges a-b: level-by-level breadth-first flood fill in numpy. -> comp id per face, count."""
    src = np.concatenate([a, b])
    dst = np.concatenate([b, a])
    o = np.argsort(src, kind='stable')
    dst = dst[o]
    indptr = np.zeros(n + 1, np.int64)
    np.cumsum(np.bincount(src, minlength=n), out=indptr[1:])
    comp = np.full(n, -1, np.int64)
    c = 0
    for root in range(n):
        if comp[root] >= 0:
            continue
        comp[root] = c
        frontier = np.array([root])
        while frontier.size:
            counts = indptr[frontier + 1] - indptr[frontier]
            total = int(counts.sum())
            if not total:
                break
            pos = np.repeat(indptr[frontier] - np.cumsum(counts) + counts, counts) + np.arange(total)
            nb = dst[pos]
            nb = np.unique(nb[comp[nb] < 0])
            comp[nb] = c
            frontier = nb
        c += 1
    return comp, c


def _near_faces(n, fa, fb, seeds, rings):
    """Faces within `rings` face-adjacency steps of the `seeds` faces (bool mask), level by level."""
    near = np.zeros(n, bool)
    near[seeds] = True
    src = np.concatenate([fa, fb])
    dst = np.concatenate([fb, fa])
    for _ in range(rings):
        grow = np.zeros(n, bool)
        grow[dst[near[src]]] = True
        if not (grow & ~near).any():
            break
        near |= grow
    return near


def _parity_flips(n, fa, fb, same):
    """Spanning-tree solution: breadth-first flood fill, a face is flipped relative to the neighbour it was reached from when they traverse
    their shared edge in the same direction. Exact for an orientable mesh; with conflicts (odd cycles) the tree decides."""
    src = np.concatenate([fa, fb])
    dst = np.concatenate([fb, fa])
    ww = np.concatenate([same, same])
    o = np.argsort(src, kind='stable')
    dst, ww = dst[o], ww[o]
    indptr = np.zeros(n + 1, np.int64)
    np.cumsum(np.bincount(src, minlength=n), out=indptr[1:])
    parity = np.zeros(n, np.int8)
    seen = np.zeros(n, bool)
    for root in range(n):
        if seen[root]:
            continue
        seen[root] = True
        frontier = np.array([root])
        while frontier.size:
            counts = indptr[frontier + 1] - indptr[frontier]
            total = int(counts.sum())
            if not total:
                break
            pos = np.repeat(indptr[frontier] - np.cumsum(counts) + counts, counts) + np.arange(total)
            nb, par = dst[pos], np.repeat(parity[frontier], counts) ^ ww[pos]
            fresh = ~seen[nb]
            nb, par = nb[fresh], par[fresh]
            if not len(nb):
                break
            nb, first = np.unique(nb, return_index=True)
            parity[nb] = par[first]
            seen[nb] = True
            frontier = nb
    return parity.astype(bool)


def _region_flips(n, fa, fb, same, edge, rings):
    """Flip decision per face by regions: the faces farther than `rings` steps from every inconsistent edge are grouped by consistent
    edges (a flipped patch that is still attached by a run of consistent edges along its seam separates from the rest this way); the band
    around the inconsistent edges joins, over consistent edges only, the region it touches first. Regions are then flipped relative to
    each other greedily (largest fixed) to leave as few edges inconsistent as possible, so an isolated inconsistent edge (a real fold) is
    left alone and a flipped patch is turned around as a whole."""
    seeds = np.concatenate([fa[same == 1], fb[same == 1]])
    near = _near_faces(n, fa, fb, seeds, rings)
    good = same == 0
    link = good & ~near[fa] & ~near[fb]
    region, k = _flood_components(n, fa[link], fb[link])
    region = np.where(near, -1, region)
    ca, cb_ = fa[good], fb[good]
    for _ in range(rings + 2):
        take = (region[ca] < 0) & (region[cb_] >= 0)
        take2 = (region[cb_] < 0) & (region[ca] >= 0)
        if not take.any() and not take2.any():
            break
        new_region = region.copy()
        new_region[ca[take]] = region[cb_[take]]
        new_region[cb_[take2]] = region[ca[take2]]
        region = new_region
    left = region < 0                        # faces cut off by inconsistent edges from every region: their own regions
    if left.any():
        both = left[ca] & left[cb_]
        sub, _ = _flood_components(n, ca[both], cb_[both])
        region = np.where(left, k + sub, region)
        _, region = np.unique(region, return_inverse=True)
        region = region.reshape(-1)
        k = int(region.max()) + 1
    ra, rb = region[fa], region[fb]
    cross = ra != rb
    flip_region = np.zeros(k, bool)
    if cross.any():
        pair = np.sort(np.stack([ra[cross], rb[cross]], 1), axis=1)
        inc = same[cross].astype(np.int64)
        keys, inv = np.unique(pair, axis=0, return_inverse=True)
        inv = inv.reshape(-1)
        n_inc = np.bincount(inv, weights=inc, minlength=len(keys))
        n_con = np.bincount(inv, weights=1 - inc, minlength=len(keys))
        size = np.bincount(region, minlength=k)
        nbrs = {}
        for (i, j), ni, nc in zip(keys.tolist(), n_inc.tolist(), n_con.tolist()):
            nbrs.setdefault(i, []).append((j, ni, nc))
            nbrs.setdefault(j, []).append((i, ni, nc))
        order = sorted(nbrs, key=lambda r_: -size[r_])
        for _ in range(10):
            moved = False
            for r_ in order[1:]:
                # unsatisfied edges if r_ is not flipped (c0) / flipped (c1): two regions with equal flips leave their inconsistent
                # edges unsatisfied, with different flips their consistent ones
                c0 = sum((ni if not flip_region[j] else nc) for j, ni, nc in nbrs[r_])
                c1 = sum((nc if not flip_region[j] else ni) for j, ni, nc in nbrs[r_])
                new = c1 < c0 or (flip_region[r_] and c1 == c0)
                if new != flip_region[r_]:
                    flip_region[r_] = new
                    moved = True
            if not moved:
                break
    return flip_region[region], k, int(flip_region.sum())


def orient_consistent(v, f, closed_boundary_share=1e-3, rings=16, passes=4):
    """Make the winding of a triangle mesh consistent and outward without disturbing what is already consistent.

    adaptive.local_repair leaves patches whose winding disagrees with their neighbours (sometimes a whole shell is inside out), and how a
    patch ends up differs from run to run; meshopt locks the vertices of an inconsistent edge as non-manifold, so such a patch also keeps
    the simplified mesh dense. A manifold edge is inconsistent when both faces traverse it in the same direction. Two solutions are tried,
    a spanning-tree flood fill (`_parity_flips`) and flips of whole regions (`_region_flips`); each shell (manifold-connected component)
    then gets the orientation of its signed volume when it is (nearly) closed (boundary edges <= `closed_boundary_share` of its edges),
    else the one that changes the fewest input faces; the solution with the fewest inconsistent edges left wins (ties: the fewest faces
    changed). Face order and vertex ids are kept. Returns (faces, info)."""
    f = np.asarray(f, np.int64)
    n = len(f)
    fa, fb, same, bnd, edge = _manifold_pairs(f)
    info = {'faces': int(n), 'inconsistent_edges_before': int(same.sum())}
    shell, s = _flood_components(n, fa, fb)
    shell_bnd = np.bincount(shell, weights=bnd, minlength=s)
    closed = shell_bnd <= closed_boundary_share * np.bincount(shell, minlength=s) * 1.5
    cyc = lambda x: x[:, [1, 2, 0]]
    best = None
    region_info = {}
    for name in (('regions', 'tree') if same.any() else ('none',)):
        if name == 'none':
            flip = np.zeros(n, bool)
        elif name == 'regions':
            flip, k, nflip = _region_flips(n, fa, fb, same, edge, rings)
            region_info = {'regions': int(k), 'regions_flipped': nflip}
        else:
            flip = _parity_flips(n, fa, fb, same)
        g = f.copy()
        g[flip] = g[flip][:, ::-1]
        t = v[g]
        vol = np.einsum('ij,ij->i', t[:, 0], np.cross(t[:, 1], t[:, 2])) / 6
        shell_vol = np.bincount(shell, weights=vol, minlength=s)
        changed = np.bincount(shell, weights=flip.astype(float), minlength=s) / np.maximum(np.bincount(shell, minlength=s), 1)
        flip_shell = np.where(closed, shell_vol < 0, changed > .5)
        g[flip_shell[shell]] = g[flip_shell[shell]][:, ::-1]
        after = int(_manifold_pairs(g)[2].sum())
        unchanged = (g == f).all(1) | (g == cyc(f)).all(1) | (g == cyc(cyc(f))).all(1)
        key = (after, int((~unchanged).sum()))
        if best is None or key < best[0]:
            best = (key, g, name, int(flip_shell.sum()))
    key, g, name, shells_flipped = best
    info.update(inconsistent_edges_after=key[0], flipped_faces=key[1], shells=int(s), shells_flipped=shells_flipped, method=name, **region_info)
    return g, info


# ----------------------------------------------------------------------------------------------- GLB frame / reader (numpy only)

def from_glb(v_glb, meta):
    """Inverse of segment_first.to_glb: the glTF frame (x, z, -y after de-normalisation) -> normalised Blender frame."""
    g = np.asarray(v_glb, np.float64)
    p = np.stack([g[:, 0], -g[:, 2], g[:, 1]], 1)
    q = p @ np.array(meta['rotation_to_review']).T
    return (q - np.array(meta['normalization_origin_rotated'])) * meta['normalization_scale']


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
