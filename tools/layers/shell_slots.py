"""Mesh container, module loader and GLB writer of the layer builder (from the shell / slot prototype shell_slots.py, 2026-11-22; the
prototype's plane-cut / zipper / stitch methods were removed with the move to tools/layers/, see commit a626d23 / tag pre-pipeline-v2 under
experiments/shell_slots_20261122/shell_slots.py).

`load_shell` reads a module's full labelled LOD (reduced/lod_error_<lv>.glb: garment roles + the generator's own bare skin, one surface), aligns it
with the module's alignment field (fit/align.json, exactly the field the wearable got) into game world space and returns a `Mesh`. `glb_bytes`
writes a layer as a flat-shaded GLB with the morph target `fill` (the Amount slider).
"""
import json
import struct
import sys
import time
from pathlib import Path

import numpy as np
from scipy import sparse
from scipy.sparse import csgraph

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'tools' / 'tripo_pipeline'))
import body_fit as bf  # noqa: E402
import align_body as ab  # noqa: E402
import fit_lab as fl  # noqa: E402

LV = '0p4'
SKIN = 'skin'
ROLE_RGB = {'skin': (0.86, 0.67, 0.53), 'primary': (0.098, 0.824, 0.157), 'secondary': (0.118, 0.255, 0.922), 'tertiary': (0.961, 0.863, 0.078),
            'accent': (0.882, 0.098, 0.863), 'joint': (0.078, 0.863, 0.922)}
PROV_RGB = {'tripo': None, 'game': (0.55, 0.78, 0.95), 'stitch': (1.0, 0.25, 0.1), 'mirror': None}
SMALL_SKIN_CM2 = 6.0          # bare-skin islands smaller than this inside the garment are red slivers: relabelled to the surrounding garment role
RAMP_PCT = 3.0                # % of body height: the garment's falloff from a snapped opening (snap) and from pinned skin (fill)


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


# ----------------------------------------------------------------------------------------------- mesh container

class Mesh:
    """Triangles with per-face role (skin / garment role), part (base part index), prov (tripo / game / stitch / mirror). Also duck-types the bits of
    bf.Surface that fit_lab.fit_local and fit_lab.graph_distance use (U, Tw, K, edges, tri_area, face_normals)."""

    def __init__(self, V, F, role, part, prov):
        self.V = np.asarray(V, np.float64)
        self.F = np.asarray(F, np.int64).reshape(-1, 3)
        n = len(self.F)
        self.role = np.asarray(role if not isinstance(role, str) else [role] * n, '<U10')
        self.part = np.asarray(part if np.ndim(part) else [part] * n, np.int64)
        self.prov = np.asarray(prov if not isinstance(prov, str) else [prov] * n, '<U8')

    def copy(self):
        return Mesh(self.V.copy(), self.F.copy(), self.role.copy(), self.part.copy(), self.prov.copy())

    def subset(self, mask):
        return Mesh(self.V, self.F[mask], self.role[mask], self.part[mask], self.prov[mask])

    # fit_lab duck typing
    @property
    def U(self):
        return self.V

    @property
    def Tw(self):
        return self.F

    @property
    def K(self):
        return len(self.V)

    def edges(self):
        e = np.sort(np.concatenate([self.F[:, [0, 1]], self.F[:, [1, 2]], self.F[:, [2, 0]]]), axis=1)
        return e[e[:, 0] != e[:, 1]]

    def tri_area(self, U=None):
        U = self.V if U is None else U
        a, b, c = U[self.F[:, 0]], U[self.F[:, 1]], U[self.F[:, 2]]
        return np.linalg.norm(np.cross(b - a, c - a), axis=1) / 2

    def face_normals(self, U=None):
        U = self.V if U is None else U
        a, b, c = U[self.F[:, 0]], U[self.F[:, 1]], U[self.F[:, 2]]
        n = np.cross(b - a, c - a)
        ln = np.linalg.norm(n, axis=1, keepdims=True)
        return np.where(ln > 1e-30, n / np.maximum(ln, 1e-30), 0.)


def compact(m, weld=1e-9):
    """Weld bit-identical (or < weld apart) positions, drop unused vertices and degenerate faces."""
    q = np.round(m.V / weld).astype(np.int64) if weld else m.V
    _, first, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    F = inv.ravel()[m.F]
    ok = (F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])
    V = m.V[first]
    used, F2 = np.unique(F[ok], return_inverse=True)
    return Mesh(V[used], F2.reshape(-1, 3), m.role[ok], m.part[ok], m.prov[ok])


def face_adjacency(F, mask=None):
    """(face_a, face_b) pairs sharing an undirected edge (both in `mask`)."""
    idx = np.flatnonzero(mask) if mask is not None else np.arange(len(F))
    if not len(idx):
        return np.zeros((0, 2), np.int64)
    G = F[idx]
    e = np.sort(np.concatenate([G[:, [0, 1]], G[:, [1, 2]], G[:, [2, 0]]]), axis=1)
    owner = np.tile(idx, 3)
    key = e[:, 0] * (int(F.max()) + 1) + e[:, 1]
    order = np.argsort(key, kind='stable')
    k, o = key[order], owner[order]
    same = k[1:] == k[:-1]
    return np.stack([o[:-1][same], o[1:][same]], 1)


def components(n_faces, pairs, mask):
    g = sparse.coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n_faces, n_faces)) if len(pairs) else sparse.coo_matrix((n_faces, n_faces))
    _, lab = csgraph.connected_components(g, directed=False)
    lab = lab.copy()
    lab[~mask] = -1
    return lab


# ----------------------------------------------------------------------------------------------- loading + snapping

def load_npz(path, kinds=None):
    """np.load of an input sidecar / cache with pickle OFF (a replaced file must not be able to run code in the worker). `kinds` {array name: dtype
    kinds, e.g. 'iu' or 'US'} makes a wrong-typed array an error too (object arrays are never accepted). Raises ValueError with a clear message."""
    try:
        z = np.load(path, allow_pickle=False)
        for name, kind in (kinds or {}).items():
            if name in z.files and z[name].dtype.kind not in kind:
                raise ValueError(f'array {name!r} has dtype {z[name].dtype}')
    except ValueError as error:
        raise ValueError(f'{path}: not a plain numeric / string npz ({error}); object arrays are not accepted') from error
    return z


def module_identity(mod):
    s, j, r, m = Path(mod).parts[-4:]
    return s, j, r, m


def load_shell(mod, body, rig, lv=None):
    """The module's full labelled LOD (`lv`, default LV), welded, in game world space, aligned with the wearable's field. Returns (Mesh, info)."""
    mod = Path(mod)
    lv = lv or LV
    man = json.loads((mod / 'reduced' / 'manifest.json').read_text(encoding='utf-8-sig'))
    glb = bf.Glb(mod / 'reduced' / f'lod_error_{lv}.glb')
    surf = bf.Surface(glb, man, body)
    names = [m.get('name') for m in glb.js.get('materials', [])]
    role = []
    for k in range(len(glb.prims)):
        mi = glb.prims[k]['def'].get('material')
        nm = names[mi] if mi is not None and mi < len(names) else 'base'
        role += [SKIN if nm == 'base' else nm] * len(glb.triangles(k))
    role = np.array(role, '<U10')
    nF = len(surf.Tw)
    part = np.full(nF, -1, np.int64)
    cz_path = mod / 'reduced' / f'lod_error_{lv}_colors.npz'
    info = {'labelled_faces': int(nF), 'source_lod': lv}
    if cz_path.is_file():
        cz = load_npz(cz_path, {'body_part_glb_order': 'iu', 'body_part_names': 'US'})
        if 'body_part_glb_order' in cz.files and len(cz['body_part_glb_order']) == nF:
            pn = [str(x) for x in cz['body_part_names']]
            lut = np.array([body.part_names.index(n) if n in body.part_names else -1 for n in pn] + [-1])
            bp = np.asarray(cz['body_part_glb_order'], np.int64)
            part = lut[np.where((bp >= 0) & (bp < len(pn)), bp, len(pn))]
    rec = json.loads((mod / 'fit' / 'align.json').read_text(encoding='utf-8-sig'))
    field = ab.Field.from_record(rec, body, rig, wearable=True)
    V = field(surf.U)
    F = surf.Tw.copy()
    ok = (F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])
    m = Mesh(V, F[ok], role[ok], part[ok], 'tripo')
    miss = m.part < 0
    if miss.any():
        cent = m.V[m.F].mean(1)
        m.part[miss] = body.measure(cent[miss])['part']
    info['parts_from_nearest'] = int(miss.sum())
    # outward winding (signed volume) and consistency
    vol = np.einsum('ij,ij->', m.V[m.F[:, 0]], np.cross(m.V[m.F[:, 1]], m.V[m.F[:, 2]]))
    if vol < 0:
        m.F = m.F[:, [0, 2, 1]]
        info['flipped'] = True
    d = np.concatenate([m.F[:, [0, 1]], m.F[:, [1, 2]], m.F[:, [2, 0]]])
    _, cnt = np.unique(d, axis=0, return_counts=True)
    info['inconsistent_directed_edges'] = int((cnt > 1).sum())
    info['align'] = {'stage1_wearable_scale_mode': rec['stage1'].get('wearable_scale_mode')}
    return m, info


def relabel_small_skin(m, cm2=SMALL_SKIN_CM2):
    """Bare-skin islands smaller than `cm2` (red slivers on the garment) take the garment role most of their neighbours have."""
    skin = m.role == SKIN
    pairs = face_adjacency(m.F, skin)
    lab = components(len(m.F), pairs, skin)
    area = m.tri_area()
    allp = face_adjacency(m.F)
    n = 0
    for c in np.unique(lab[lab >= 0]):
        fs = np.flatnonzero(lab == c)
        if area[fs].sum() * 1e4 >= cm2:
            continue
        inset = np.zeros(len(m.F), bool)
        inset[fs] = True
        nb = np.r_[allp[inset[allp[:, 0]] & ~inset[allp[:, 1]], 1], allp[inset[allp[:, 1]] & ~inset[allp[:, 0]], 0]]
        roles = [r for r in m.role[nb] if r != SKIN]
        if not roles:
            continue
        vals, cnts = np.unique(roles, return_counts=True)
        m.role[fs] = vals[cnts.argmax()]
        n += len(fs)
    return n


def vertex_part(m, faces):
    """Most frequent part of each vertex over the given faces (-1 for vertices not used by them)."""
    v = m.F[faces].ravel()
    p = np.repeat(m.part[faces], 3)
    out = np.full(len(m.V), -1, np.int64)
    if not len(v):
        return out
    key = v * 1000 + p
    u, cnt = np.unique(key, return_counts=True)
    vv, pp = u // 1000, u % 1000
    order = np.lexsort((cnt, vv))
    last = np.r_[vv[order][1:] != vv[order][:-1], True]
    out[vv[order][last]] = pp[order][last]
    return out


# ----------------------------------------------------------------------------------------------- planes, cuts, rings


def base_mesh(body):
    return Mesh(body.V, body.F, SKIN, body.part, 'game')


def mirror_mesh(m, x0, body):
    V = m.V.copy()
    V[:, 0] = 2 * x0 - V[:, 0]
    pn = body.part_names
    swap = np.array([pn.index(n[:-2] + ('.r' if n.endswith('.l') else '.l')) if n[-2:] in ('.l', '.r') and (n[:-2] + ('.r' if n.endswith('.l') else '.l')) in pn else i
                     for i, n in enumerate(pn)])
    prov = np.where(m.prov == 'tripo', 'mirror', m.prov)
    return Mesh(V, m.F[:, [0, 2, 1]], m.role, swap[m.part], prov)


# ----------------------------------------------------------------------------------------------- seams


def split_edges(V, F, mark):
    """Split the marked undirected edges (set of (i, j), i < j) at their midpoints; faces re-triangulated with their winding.
    Returns (V, F, parent face index per new face)."""
    V = list(map(tuple, V))
    mid = {}
    for e in mark:
        mid[e] = len(V)
        V.append(tuple((np.asarray(V[e[0]]) + np.asarray(V[e[1]])) / 2))
    outF, parent = [], []
    for fi, (a, b, c) in enumerate(F):
        es = [(a, b), (b, c), (c, a)]
        ms = [mid.get((min(x, y), max(x, y))) for x, y in es]
        k = sum(m is not None for m in ms)
        if k == 0:
            tris = [(a, b, c)]
        elif k == 3:
            m1, m2, m3 = ms
            tris = [(a, m1, m3), (m1, b, m2), (m3, m2, c), (m1, m2, m3)]
        elif k == 1:
            r = [i for i in range(3) if ms[i] is not None][0]
            x, y, z = [(a, b, c), (b, c, a), (c, a, b)][r]
            m = ms[r]
            tris = [(x, m, z), (m, y, z)]
        else:
            r = [i for i in range(3) if ms[i] is None][0]          # unmarked edge index: rotate so it is (z, x)
            x, y, z = [(b, c, a), (c, a, b), (a, b, c)][r]
            m1 = mid[(min(x, y), max(x, y))]
            m2 = mid[(min(y, z), max(y, z))]
            tris = [(m1, y, m2), (x, m1, m2), (x, m2, z)]
        outF += tris
        parent += [fi] * len(tris)
    return np.array(V, np.float64), np.array(outF, np.int64), np.array(parent, np.int64)


def boundary_loops(m, faces_mask=None):
    """Open boundary loops of m (edges used by one face), each ordered in m's own winding; longest first.
    Halfedge walk (2026-11-23, after the Codex review): every boundary halfedge is used exactly once; the successor of a boundary halfedge a->b is
    found by turning around b through the faces of b's fan (so a vertex where two boundary runs touch is passed correctly); where the fan walk is
    blocked (a non-manifold or inconsistently wound edge) an unused boundary halfedge starting at b is taken. A walk that cannot be closed is
    returned only up to its last real edge and is dropped if it is not a cycle (callers treat loops as cyclic; the old walk invented the closing
    edge)."""
    F = np.asarray(m.F if faces_mask is None else m.F[faces_mask], np.int64)
    if not len(F):
        return []
    d = np.stack([F, np.roll(F, -1, axis=1)], axis=2).reshape(-1, 2)       # halfedge h: face h // 3, corner h % 3 -> the next corner
    key = np.sort(d, axis=1)
    _, inv, cnt = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    inv = inv.ravel()
    border = np.flatnonzero(cnt[inv] == 1)
    if not len(border):
        return []
    order = np.argsort(inv, kind='stable')
    starts = np.r_[0, np.cumsum(cnt)[:-1]]
    two = np.flatnonzero(cnt == 2)
    i, j = order[starts[two]], order[starts[two] + 1]
    ok = (d[i, 0] == d[j, 1]) & (d[j, 0] == d[i, 1])
    twin = np.full(len(d), -1, np.int64)
    twin[i[ok]], twin[j[ok]] = j[ok], i[ok]
    isb = np.zeros(len(d), bool)
    isb[border] = True
    by_start = {}
    for h in border:
        by_start.setdefault(int(d[h, 0]), []).append(int(h))
    used = np.zeros(len(d), bool)

    def successor(h):
        cur, seen = (h // 3) * 3 + (h + 1) % 3, set()
        while twin[cur] >= 0 and cur not in seen:
            seen.add(cur)
            t = twin[cur]
            cur = (t // 3) * 3 + (t + 1) % 3
        if isb[cur] and not used[cur]:
            return int(cur)
        for c in by_start.get(int(d[h, 1]), []):
            if not used[c]:
                return c
        return None
    loops = []
    for h0 in border:
        if used[h0]:
            continue
        path, h = [], int(h0)
        while h is not None and not used[h]:
            used[h] = True
            path.append(h)
            if d[h, 1] == d[h0, 0]:
                break
            h = successor(h)
        if d[path[-1], 1] == d[h0, 0] and len(path) >= 2:
            loops.append([int(d[x, 0]) for x in path])
    loops.sort(key=lambda c: -sum(np.linalg.norm(m.V[c[k]] - m.V[c[(k + 1) % len(c)]]) for k in range(len(c))))
    return loops


# ----------------------------------------------------------------------------------------------- fill (amount slider)

def fill(m, body, margin):
    """Garment pushed out of the game base by `margin` where it is inside (or closer), with skin / game / stitch faces pinned and the garment's freedom
    ramping in over RAMP_PCT % of the height from them. Returns (U1, info)."""
    garm = (m.role != SKIN) & (m.prov != 'game') & (m.prov != 'stitch')
    pinned_v = np.unique(m.F[~garm])
    gv = np.unique(m.F[garm])
    w = np.zeros(len(m.V))
    if not len(gv):
        return m.V.copy(), {'skipped': 'no garment'}
    sub = m.subset(garm)
    sources = np.intersect1d(pinned_v, gv)
    ramp = RAMP_PCT / 100 * body.H
    if len(sources):
        dist = fl.graph_distance(sub, sources)
        w = np.clip(np.where(np.isfinite(dist), dist, ramp) / ramp, 0., 1.)
    else:
        w[gv] = 1.
    w[pinned_v] = 0.
    used = np.zeros(len(m.V), bool)
    used[m.F] = True
    w[~used] = 0.
    depth = np.maximum(-body.measure(m.V)['d'], 0.)
    reach = float(max(fl.REACH_DEFAULT, np.percentile(depth[gv], 99) + fl.REACH_EXTRA))
    old = bf.POKE_IN
    bf.POKE_IN = reach
    body._samples = None
    try:
        lo, hi = m.V[gv].min(0) - .2, m.V[gv].max(0) + .2
        base_ctx = bf.PokeCtx(bf.scene_for(body, [np.array([lo, hi])]))

        class Ctx:
            def measure(self, surf, U):
                pk = base_ctx.measure(surf, U)
                pk = bf.poke_select(pk, np.zeros(len(pk['tri']), bool))      # vertex-only fill: no game body is drawn under the clothes, so the body poking
                pk['depth_raw'] = pk['depth']                                   # between garment vertices (big low-poly triangles) is invisible; lifting for it ballooned sleeves
                return pk
        depth0 = np.maximum(margin - body.measure(m.V)['d'], 0.) * (w > 0)
        t0 = time.monotonic()
        D, info = fl.fit_local(m, body, margin, w, Ctx(), m.V, .12, 60, 12, 20, .12, .5, depth0=depth0, poke_cap=.15, poke_ignore=.03)
    finally:
        bf.POKE_IN = old
        body._samples = None
    mv = np.linalg.norm(D, axis=1) * 1000
    return m.V + D, {'seconds': round(time.monotonic() - t0, 1), 'moved_mm_max': float(mv.max()), 'moved_mm_mean_moved': float(mv[mv > .5].mean()) if (mv > .5).any() else 0.,
                     'vertices_moved': int((mv > .5).sum()), 'converged': bool(info.get('converged')), 'reach_m': reach}


# ----------------------------------------------------------------------------------------------- output

def glb_bytes(m, U1=None, colour='role', extra=None):
    """Flat-shaded GLB (glTF Y-up), one primitive per (role, prov) with material name 'role:prov'; with U1 a morph target 'fill' (POSITION deltas),
    then one more morph target per `extra` {name: positions} (e.g. 'deflate': the positions without the gnome inflation)."""
    targets = ([('fill', U1)] if U1 is not None else []) + list((extra or {}).items())
    blob, views, accs, prims, mats = bytearray(), [], [], [], []

    def add(arr, target=34962, minmax=False):
        off = len(blob)
        blob.extend(arr.tobytes())
        blob.extend(b'\0' * (-len(blob) % 4))
        views.append({'buffer': 0, 'byteOffset': off, 'byteLength': arr.nbytes, 'target': target})
        a = {'bufferView': len(views) - 1, 'componentType': 5126, 'count': len(arr), 'type': 'VEC3'}
        if minmax:
            a['min'], a['max'] = [float(x) for x in arr.min(0)], [float(x) for x in arr.max(0)]
        accs.append(a)
        return len(accs) - 1
    keys = sorted(set(zip(m.role.tolist(), m.prov.tolist())))
    for role, prov in keys:
        sel = (m.role == role) & (m.prov == prov)
        T0 = bf.bl_to_gl(m.V[m.F[sel]].reshape(-1, 3)).astype(np.float32)
        cr = np.cross(T0.reshape(-1, 3, 3)[:, 1] - T0.reshape(-1, 3, 3)[:, 0], T0.reshape(-1, 3, 3)[:, 2] - T0.reshape(-1, 3, 3)[:, 0])
        nrm = cr / np.maximum(np.linalg.norm(cr, axis=1, keepdims=True), 1e-30)
        prim = {'attributes': {'POSITION': add(T0, minmax=True), 'NORMAL': add(np.repeat(nrm, 3, axis=0).astype(np.float32))}, 'material': len(mats)}
        if targets:                                                  # POSITION and NORMAL deltas (flat normals of the target's triangles), so a
            prim['targets'] = []                                     # face that tilts under the slider is lit as it ends up
            for _, U in targets:
                T1 = bf.bl_to_gl(U[m.F[sel]].reshape(-1, 3)).astype(np.float32)
                c1 = np.cross(T1.reshape(-1, 3, 3)[:, 1] - T1.reshape(-1, 3, 3)[:, 0], T1.reshape(-1, 3, 3)[:, 2] - T1.reshape(-1, 3, 3)[:, 0])
                n1 = c1 / np.maximum(np.linalg.norm(c1, axis=1, keepdims=True), 1e-30)
                prim['targets'].append({'POSITION': add((T1 - T0).astype(np.float32), minmax=True),
                                        'NORMAL': add(np.repeat(n1 - nrm, 3, axis=0).astype(np.float32))})
        prims.append(prim)
        rgb = ROLE_RGB.get(role, (.5, .5, .5))
        mats.append({'name': f'{role}:{prov}', 'doubleSided': True, 'pbrMetallicRoughness': {'baseColorFactor': [*rgb, 1.], 'metallicFactor': 0., 'roughnessFactor': .9}})
    mesh = {'name': 'shell', 'primitives': prims}
    if targets:
        mesh['weights'] = [0.] * len(targets)
        mesh['extras'] = {'targetNames': [t for t, _ in targets]}
    js = {'asset': {'version': '2.0', 'generator': 'shell_slots.py'}, 'scene': 0, 'scenes': [{'nodes': [0]}], 'nodes': [{'mesh': 0, 'name': 'shell'}],
          'meshes': [mesh], 'materials': mats, 'accessors': accs, 'bufferViews': views, 'buffers': [{'byteLength': len(blob)}]}
    text = json.dumps(js, separators=(',', ':')).encode()
    text += b' ' * (-len(text) % 4)
    body = struct.pack('<II', len(text), 0x4E4F534A) + text + struct.pack('<II', len(blob), 0x004E4942) + bytes(blob)
    return struct.pack('<4sII', b'glTF', 2, 12 + len(body)) + body


def face_colours(m, mode='role'):
    C = np.array([ROLE_RGB.get(r, (.5, .5, .5)) for r in m.role])
    if mode == 'prov':
        C = np.where((m.prov == 'game')[:, None], PROV_RGB['game'], C)
        C = np.where((m.prov == 'band')[:, None], (.45, .62, .95), C)
        C = np.where((m.prov == 'lip')[:, None], (.98, .6, .15), C)
        C = np.where((m.prov == 'stitch')[:, None], PROV_RGB['stitch'], C)
        C = np.where(((m.prov == 'mirror') & (m.role == SKIN))[:, None], (.95, .85, .55), C)
        C = np.where(((m.prov == 'tripo') & (m.role == SKIN))[:, None], (.86, .55, .45), C)
    return C
