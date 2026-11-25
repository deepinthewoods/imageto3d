"""Shell / slot prototype (2026-11-22, user request). EXPERIMENT: outfits and armour as the generator drew them, with no game body under the clothes.

    python experiments/shell_slots_20261122/shell_slots.py outfit <module_dir> [--name N]
    python experiments/shell_slots_20261122/shell_slots.py armour <body_armor_dir> [<right_gauntlet_boot_dir>] [--name N]
    python experiments/shell_slots_20261122/shell_slots.py batch          (the review set in REVIEW)

Approach 1, outfits ("shell"): the module's full labelled LOD (reduced/lod_error_0p4.glb: the garment roles + the generator's own bare skin, ONE surface) is
aligned with the module's alignment field (fit/align.json, exactly the field the wearable got), its bare skin is snapped onto the game base surface (the garment
next to every opening follows with a smooth falloff), and the head and both hands (and both feet when the outfit has no footwear) are replaced by the game base's
own parts: cut at a plane in bare skin and stitched with a thin strip to the base's ring at that plane. The bare anatomical-left foot (concept anchor) is replaced
by the mirror of the right lower leg below a plane just under the left garment hem. Nothing sits under the clothes, so nothing can poke through, and a corset
stays as thin as it was drawn.

Approach 2, armour ("slots"): every module is cut at fixed canonical rings (neck, elbows, knees = the base's part-boundary loops) and each piece ends exactly on
the game base's ring polygon there (a thin stitch strip from the piece's own ring), so pieces of different sets, and the base's own pieces for empty slots, join
with no runtime work. body_armor supplies the torso / upper-arm / thigh piece, right_gauntlet_boot the right forearm + hand and shin + foot (mirrored to the left).

Amount: every result carries a morph target `fill` = the garment's VERTICES pushed out of the game base by the margin where the generator made it skinnier
(fit_lab.fit_local without the reverse poke test; skin, game parts and stitches pinned; the garment's freedom ramps in over 3 % of the body height from them).
Slider 0 = as generated.

Outputs (out/): <name>.glb (+ morph `fill`), <name>__tripo.glb (the aligned full mesh before any change), base_<race>.glb, index.json (viewer list + notes),
renders/<name>.png. View: python -m http.server 8790 --bind 127.0.0.1 --directory experiments/shell_slots_20261122/out, then /view.html.
"""
import argparse
import json
import os
import struct
import sys
import time
from pathlib import Path

import numpy as np
from scipy import sparse
from scipy.sparse import csgraph

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / 'out'
sys.path.insert(0, str(ROOT / 'tools' / 'tripo_pipeline'))
import body_fit as bf  # noqa: E402
import align_body as ab  # noqa: E402
import fit_lab as fl  # noqa: E402

LV = '0p4'
SKIN = 'skin'
GARMENT_ROLES = ('primary', 'secondary', 'tertiary', 'accent', 'joint')
ROLE_RGB = {'skin': (0.86, 0.67, 0.53), 'primary': (0.098, 0.824, 0.157), 'secondary': (0.118, 0.255, 0.922), 'tertiary': (0.961, 0.863, 0.078),
            'accent': (0.882, 0.098, 0.863), 'joint': (0.078, 0.863, 0.922)}
PROV_RGB = {'tripo': None, 'game': (0.55, 0.78, 0.95), 'stitch': (1.0, 0.25, 0.1), 'mirror': None}
SMALL_SKIN_CM2 = 6.0          # bare-skin islands smaller than this inside the garment are red slivers: relabelled to the surrounding garment role
RAMP_PCT = 3.0                # % of body height: the garment's falloff from a snapped opening (snap) and from pinned skin (fill)
BAND_MM = 8.0                 # a seam plane is "bare" when every face within this distance of it (near the ring) is skin
SEAMS = {
    'head': dict(P='neck.x', Q='head.x', chain=None),
    'hand.l': dict(P='forearm_stretch.l', Q='hand.l', chain=('forearm_stretch.l', 'hand.l')),
    'hand.r': dict(P='forearm_stretch.r', Q='hand.r', chain=('forearm_stretch.r', 'hand.r')),
    'foot.l': dict(P='leg_stretch.l', Q='foot.l', chain=('leg_stretch.l', 'foot.l')),
    'foot.r': dict(P='leg_stretch.r', Q='foot.r', chain=('leg_stretch.r', 'foot.r')),
    'elbow.l': dict(P='arm_stretch.l', Q='forearm_stretch.l', chain=('arm_stretch.l', 'forearm_stretch.l', 'hand.l')),
    'elbow.r': dict(P='arm_stretch.r', Q='forearm_stretch.r', chain=('arm_stretch.r', 'forearm_stretch.r', 'hand.r')),
    'knee.l': dict(P='thigh_stretch.l', Q='leg_stretch.l', chain=('thigh_stretch.l', 'leg_stretch.l', 'foot.l')),
    'knee.r': dict(P='thigh_stretch.r', Q='leg_stretch.r', chain=('thigh_stretch.r', 'leg_stretch.r', 'foot.r')),
}
FALLBACK_P = {'neck.x': 'spine_01.x'}
ARMOUR_OFFSETS = [0., -.1, .1, -.2, .2, -.3, .3]     # canonical elbow / knee rings: first offset (fraction of the part length) where every module of the combination is bare
# candidate plane offsets per seam kind, as fractions of the proximal (-) / distal (+) part length; the first bare one wins
OFFSETS = {'head': [0, -.25, -.5, .1, .2], 'hand': [0, -.12, .12, -.25, .25, -.4], 'foot': [0, -.12, .12, -.25, .25]}
REVIEW = {
    'outfits': ['models/noble_outfit_08_florentine_lady_gamurra/53a03a669695/human_female/outfit',
                'models/noble_outfit_41_basque_noble_doublet/8841eb97a0f8/human_male/outfit',
                'models/peasant_outfit_01_saxon_tunic_leg_wraps',     # resolved to dwarf_male below
                # gnome_female skipped for now (user 2026-11-22): 'models/noble_outfit_08_florentine_lady_gamurra/5e8e1e44ff7c/gnome_female/outfit'
                # the five gnome modules re-aligned with the fixed gnome rig (user 2026-11-22: "put the gnomes in the prototype")
                'models/african_tribal_outfit_05_sahel_indigo_farmer/9e1d2e6718ce/gnome_female/outfit',
                'models/african_ceremonial_outfit_04_grassland_ceremony_dress/ceed644246e8/gnome_female/outfit',
               ],
    'armour': [('breastplate_07_hand_me_down_cuirass', 'breastplate_07_hand_me_down_cuirass', 'human_male'),
               ('breastplate_07_hand_me_down_cuirass', 'padded_06_tinker_quilted_coat', 'human_male'),
               ('breastplate_07_hand_me_down_cuirass', None, 'human_male'),
               # gnome armour (re-aligned 2026-11-22): module paths name the exact job
               ('models/breastplate_07_hand_me_down_cuirass/77590726e534/gnome_female/body_armor',
                'models/breastplate_07_hand_me_down_cuirass/77590726e534/gnome_female/right_gauntlet_boot', 'gnome_female'),
               # round-head gnome gauntlets (user 2026-11-23: cebe94d93d90 was drawn on the old hat-head gnome base), all on the same body armour
               ('models/breastplate_07_hand_me_down_cuirass/77590726e534/gnome_female/body_armor',
                'models/leather_14_caravan_outrider_leather/c65d5f8f57ac/gnome_female/right_gauntlet_boot', 'gnome_female'),
               ('models/breastplate_07_hand_me_down_cuirass/77590726e534/gnome_female/body_armor',
                'models/padded_06_tinker_quilted_coat/babde1bdf65f/gnome_female/right_gauntlet_boot', 'gnome_female'),
               ('models/breastplate_07_hand_me_down_cuirass/77590726e534/gnome_female/body_armor',
                'models/chain_mail_05_stable_mail_tunic/3cabfa0efc7e/gnome_female/right_gauntlet_boot', 'gnome_female'),
               ('models/breastplate_07_hand_me_down_cuirass/77590726e534/gnome_female/body_armor',
                'models/studded_leather_09_guardsman_studded_cuirass/967779b017aa/gnome_female/right_gauntlet_boot', 'gnome_female'),
               ('models/breastplate_07_hand_me_down_cuirass/77590726e534/gnome_female/body_armor',
                'models/scale_mail_15_trail_warden_scales/f29bab596585/gnome_female/right_gauntlet_boot', 'gnome_female'),
               ('models/breastplate_07_hand_me_down_cuirass/77590726e534/gnome_female/body_armor',
                'models/magic_plate_mail_15_phloem_shell/ab673b503e36/gnome_female/right_gauntlet_boot', 'gnome_female'),
               # full round-head gnome sets (user 2026-11-23: 'process a couple more gnome armour outfits'), body armour + its own gauntlet
               ('models/scale_mail_15_trail_warden_scales/f29bab596585/gnome_female/body_armor',
                'models/scale_mail_15_trail_warden_scales/f29bab596585/gnome_female/right_gauntlet_boot', 'gnome_female'),
               ('models/magic_plate_mail_15_phloem_shell/ab673b503e36/gnome_female/body_armor',
                'models/magic_plate_mail_15_phloem_shell/ab673b503e36/gnome_female/right_gauntlet_boot', 'gnome_female'),
               ('models/leather_14_caravan_outrider_leather/c65d5f8f57ac/gnome_female/body_armor',
                'models/leather_14_caravan_outrider_leather/c65d5f8f57ac/gnome_female/right_gauntlet_boot', 'gnome_female')],
}


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


def concat(meshes):
    V, F, R, P, S, off = [], [], [], [], [], 0
    for m in meshes:
        V.append(m.V)
        F.append(m.F + off)
        R.append(m.role)
        P.append(m.part)
        S.append(m.prov)
        off += len(m.V)
    return Mesh(np.concatenate(V), np.concatenate(F), np.concatenate(R), np.concatenate(P), np.concatenate(S))


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
        cz = np.load(cz_path, allow_pickle=True)
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


def part_neighbours(body):
    P = len(body.part_names)
    inc = sparse.coo_matrix((np.ones(body.F.size), (body.F.ravel(), np.repeat(body.part, 3))), shape=(len(body.V), P)).tocsr()
    inc.data[:] = 1
    adj = (inc.T @ inc).toarray() > 0
    return adj


_SUBS = {}


def part_bodies(body, race):
    """Per base part: a closest-point body of that part and the parts touching it (the snap never jumps to another limb)."""
    if race not in _SUBS:
        adj = part_neighbours(body)
        subs = {}
        for p in range(len(body.part_names)):
            mask = adj[p][body.part]
            subs[p] = bf.Body(body.V, body.F[mask], body.part[mask], body.part_names) if mask.sum() >= 4 else body     # a part without faces (shoulders on some bases): whole body
        _SUBS[race] = subs
    return _SUBS[race]


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


def snap_skin(m, body, race):
    """Move every bare-skin vertex onto the game base surface (closest point on its own part + the parts touching it); the garment next to the
    openings follows with a smooth falloff over RAMP_PCT % of the height (graph distance along the garment, then neighbour averaging with the openings held)."""
    skin_f = m.role == SKIN
    sv = np.unique(m.F[skin_f])
    gv = np.unique(m.F[~skin_f])
    rims = np.intersect1d(sv, gv)
    vp = vertex_part(m, np.flatnonzero(skin_f))
    subs = part_bodies(body, race)
    D = np.zeros_like(m.V)
    dist_mm = np.zeros(len(sv))
    for p in np.unique(vp[sv]):
        idx = sv[vp[sv] == p]
        sb = subs[int(p)]
        d, tri, bary = sb.closest(m.V[idx])
        tgt = bary[:, :1] * sb.A[tri] + bary[:, 1:2] * sb.B[tri] + bary[:, 2:] * sb.C[tri]
        D[idx] = tgt - m.V[idx]
    dist_mm = np.linalg.norm(D[sv], axis=1) * 1000
    n0 = m.face_normals()
    # garment follow-through
    H = body.H
    ramp = RAMP_PCT / 100 * H
    gonly = np.zeros(len(m.V), bool)
    gonly[gv] = True
    gonly[rims] = False
    if len(rims) and gonly.any():
        e = np.unique(np.sort(np.concatenate([m.F[~skin_f][:, [0, 1]], m.F[~skin_f][:, [1, 2]], m.F[~skin_f][:, [2, 0]]]), axis=1), axis=0)
        w = np.linalg.norm(m.V[e[:, 0]] - m.V[e[:, 1]], axis=1) + 1e-9
        G = sparse.coo_matrix((np.r_[w, w], (np.r_[e[:, 0], e[:, 1]], np.r_[e[:, 1], e[:, 0]])), shape=(len(m.V), len(m.V))).tocsr()
        dist, _, src = csgraph.dijkstra(G, directed=False, indices=rims, min_only=True, return_predecessors=True)
        t = np.clip(np.where(np.isfinite(dist), dist, np.inf) / ramp, 0., 1.)
        fall = 1. - t * t * (3. - 2. * t)
        ok = gonly & (src >= 0) & (fall > 0)
        D[ok] = fall[ok, None] * D[src[ok]]
        # neighbour averaging over the garment band, openings + skin held
        A = sparse.coo_matrix((np.ones(len(e) * 2), (np.r_[e[:, 0], e[:, 1]], np.r_[e[:, 1], e[:, 0]])), shape=(len(m.V), len(m.V))).tocsr()
        deg = np.asarray(A.sum(1)).ravel()
        for _ in range(10):
            avg = (A @ D) / np.maximum(deg, 1)[:, None]
            D[ok] = avg[ok]
    m.V = m.V + D
    n1 = m.face_normals()
    flips = int(((n0 * n1).sum(1) < 0).sum())
    g_mm = np.linalg.norm(D[gonly], axis=1) * 1000 if gonly.any() else np.zeros(1)
    return {'skin_vertices': int(len(sv)), 'rim_vertices': int(len(rims)), 'snap_mm': {'p50': float(np.percentile(dist_mm, 50)), 'p95': float(np.percentile(dist_mm, 95)),
            'max': float(dist_mm.max())} if len(dist_mm) else None, 'garment_follow_mm_max': float(g_mm.max()), 'flipped_faces': flips}


# ----------------------------------------------------------------------------------------------- planes, cuts, rings

def joint_plane(body, P, Q):
    """The seam plane between base parts P (proximal) and Q (distal). Connected parts: the best-fit plane of their shared boundary loop. A distal part that is
    its own mesh island (human_male hands, dwarf / gnome heads: the island overlaps the end of P): a plane across the middle of the overlap, flagged
    `island` (the shell's opening there is hidden inside the base island; no stitch)."""
    pn = body.part_names
    if P in FALLBACK_P and (body.part == pn.index(P)).sum() == 0:
        P = FALLBACK_P[P]                 # dwarf / gnome / orc bases have no neck part: the head sits on the torso
    pi, qi = pn.index(P), pn.index(Q)
    if (body.part == qi).sum() < 4:
        raise ValueError(f'the {body.part_names[qi]} part has no faces on this base')
    vp = np.unique(body.F[body.part == pi])
    vq = np.unique(body.F[body.part == qi])
    ring = np.intersect1d(vp, vq)
    if len(ring) >= 4:
        X = body.V[ring]
        c = X.mean(0)
        _, _, vt = np.linalg.svd(X - c)
        n = vt[2]
        if (body.V[vq].mean(0) - c) @ n < 0:
            n = -n
        r = float(np.linalg.norm(X - c, axis=1).max())
        return {'c': c, 'n': n, 'r': r, 'Lp': float(-((body.V[vp] - c) @ n).min()), 'Lq': float(((body.V[vq] - c) @ n).max()), 'island': False}
    cP, cQ = body.V[vp].mean(0), body.V[vq].mean(0)
    n = (cQ - cP) / np.linalg.norm(cQ - cP)
    sP, sQ = (body.V[vp] - cP) @ n, (body.V[vq] - cP) @ n
    pmax, qmin = sP.max(), sQ.min()
    s0 = qmin + max(.5 * (pmax - qmin), .004) if pmax > qmin else qmin + .004
    c = cP + s0 * n
    near = vq[np.abs(sQ - s0) < .015]
    X = body.V[near] if len(near) else body.V[vq]
    rad = np.linalg.norm((X - c) - ((X - c) @ n)[:, None] * n, axis=1)
    return {'c': c, 'n': n, 'r': float(rad.max()), 'Lp': float(s0 - sP.min()), 'Lq': float(sQ.max() - s0), 'island': True, 'overlap_mm': float((pmax - qmin) * 1000)}


def plane_at(jp, frac):
    L = jp['Lq'] if frac > 0 else jp['Lp']
    return {'c': jp['c'] + frac * L * jp['n'], 'n': jp['n'], 'r': jp['r'], 'frac': frac, 'island': jp.get('island', False)}


_ISL = {}


def base_islands(body):
    """Mesh-island id per base face."""
    key = id(body)
    if key not in _ISL:
        n = len(body.V)
        e = np.concatenate([body.F[:, [0, 1]], body.F[:, [1, 2]], body.F[:, [2, 0]]])
        g = sparse.coo_matrix((np.ones(len(e)), (e[:, 0], e[:, 1])), shape=(n, n))
        _, lab = csgraph.connected_components(g, directed=False)
        _ISL[key] = lab[body.F[:, 0]]
    return _ISL[key]


def island_piece(body, Q):
    """The base islands made mostly of part Q (the base's own separate hand / head meshes)."""
    isl = base_islands(body)
    qi = body.part_names.index(Q)
    keep = np.zeros(len(body.F), bool)
    for i in np.unique(isl[body.part == qi]):
        f = isl == i
        if (body.part[f] == qi).mean() >= .5:
            keep |= f
    return base_mesh(body).subset(keep)


def reflect_plane(pl, x0):
    c = pl['c'].copy()
    c[0] = 2 * x0 - c[0]
    n = pl['n'].copy()
    n[0] = -n[0]
    return {**pl, 'c': c, 'n': n}


def near_radius(pl):
    return max(2.2 * pl['r'], pl['r'] + 0.05)


def chain_idx(body, chain):
    return None if chain is None else np.array([body.part_names.index(x) for x in chain if x in body.part_names])


def plane_bare(m, pl, body, chain):
    """True when every face near the ring (within near_radius of the plane centre, of the seam's chain parts) that comes within BAND_MM of the plane is skin."""
    s = (m.V - pl['c']) @ pl['n']
    R = near_radius(pl)
    near = (np.linalg.norm(m.V - pl['c'], axis=1) <= R)[m.F].any(1)
    sf = s[m.F]
    band = near & (sf.max(1) >= -BAND_MM / 1000) & (sf.min(1) <= BAND_MM / 1000)
    ci = chain_idx(body, chain)
    if ci is not None:
        band &= np.isin(m.part, ci)
    if not band.any():
        return False, 'no faces at the plane'
    g = band & (m.role != SKIN)
    return (not g.any()), f'{int(g.sum())} garment faces of {int(band.sum())} at the plane'


def split_plane(m, pl):
    """Split the triangles near the plane (within near_radius) that cross it; intersection points are shared between neighbours. Returns a new Mesh and
    the signed distance per vertex (0 on the plane)."""
    c, n = pl['c'], pl['n']
    V = m.V.copy()
    s = (V - c) @ n
    R = near_radius(pl)
    near_v = np.linalg.norm(V - c, axis=1) <= R
    snap = near_v & (np.abs(s) < 2e-4)                     # vertices within 0.2 mm of the plane go onto it (no slivers)
    V[snap] -= s[snap, None] * n
    s[snap] = 0.
    sf = s[m.F]
    cross = near_v[m.F].any(1) & (sf.max(1) > 0) & (sf.min(1) < 0)
    # make the crossing set closed under shared crossing edges (no T-junctions): grow over faces sharing an edge whose ends have opposite signs
    while True:
        e = np.concatenate([m.F[cross][:, [0, 1]], m.F[cross][:, [1, 2]], m.F[cross][:, [2, 0]]])
        e = e[(s[e[:, 0]] * s[e[:, 1]]) < 0]
        if not len(e):
            break
        es = set(map(tuple, np.sort(e, axis=1)))
        allc = np.flatnonzero(~cross & (sf.max(1) > 0) & (sf.min(1) < 0))
        add = [f for f in allc if any(tuple(sorted((m.F[f][k], m.F[f][(k + 1) % 3]))) in es for k in range(3))]
        if not add:
            break
        cross[add] = True
    newV, cache = [], {}

    def ipt(a, b):
        key = (a, b) if a < b else (b, a)
        if key not in cache:
            t = s[key[0]] / (s[key[0]] - s[key[1]])
            cache[key] = len(V) + len(newV)
            newV.append(V[key[0]] + t * (V[key[1]] - V[key[0]]))
        return cache[key]
    keepF = ~cross
    outF, src = [m.F[keepF]], [np.flatnonzero(keepF)]
    addF, addS = [], []
    for fi in np.flatnonzero(cross):
        f = m.F[fi]
        for sign in (1, -1):
            poly = []
            for k in range(3):
                a, b = int(f[k]), int(f[(k + 1) % 3])
                sa, sb = sign * s[a], sign * s[b]
                if sa >= 0:
                    poly.append(a)
                if (sa > 0 and sb < 0) or (sa < 0 and sb > 0):
                    poly.append(ipt(a, b))
            for k in range(1, len(poly) - 1):
                addF.append((poly[0], poly[k], poly[k + 1]))
                addS.append(fi)
    if addF:
        outF.append(np.array(addF, np.int64))
        src.append(np.array(addS, np.int64))
    V2 = np.concatenate([V, np.array(newV).reshape(-1, 3)])
    s2 = np.r_[s, np.zeros(len(newV))]
    F2 = np.concatenate(outF)
    so = np.concatenate(src)
    return Mesh(V2, F2, m.role[so], m.part[so], m.prov[so]), s2


def ordered_ring(F_side, F_other_mask, F, V):
    """Boundary loop between a face set and the rest: the cycle of edges shared by a face of the set and a face outside it, ordered in the set's own
    winding direction (edge a->b as it appears in the set's faces). Returns the largest cycle (vertex index list) and the number of cycles."""
    S = F[F_side]
    O = F[F_other_mask]
    so = set(map(tuple, np.sort(np.concatenate([O[:, [0, 1]], O[:, [1, 2]], O[:, [2, 0]]]), axis=1)))
    de = np.concatenate([S[:, [0, 1]], S[:, [1, 2]], S[:, [2, 0]]])
    de = [tuple(x) for x in de if tuple(sorted(x)) in so]
    if not de:
        return [], 0
    nxt = {}
    for a, b in de:
        nxt.setdefault(a, []).append(b)
    seen, cycles = set(), []
    for a0, _ in de:
        if a0 in seen:
            continue
        cyc, a = [a0], a0
        seen.add(a0)
        for _ in range(len(de) + 1):
            bs = [b for b in nxt.get(a, []) if b not in seen or b == a0]
            if not bs:
                break
            b = bs[0]
            if b == a0:
                break
            cyc.append(b)
            seen.add(b)
            a = b
        cycles.append(cyc)
    cycles.sort(key=lambda c: -sum(np.linalg.norm(V[c[i]] - V[c[(i + 1) % len(c)]]) for i in range(len(c))))
    return cycles[0], len(cycles)


def cut(m, pl, body, chain, distal_min=0.3):
    """Cut `m` at the plane: returns (proximal Mesh, distal Mesh, ring (vertex ids into both, ordered in the PROXIMAL piece's winding), note). The distal
    piece is every connected region beyond the plane made mostly (area share >= distal_min) of the seam's chain parts (or, chain None, everything
    beyond the plane within reach of the ring); its vertex array is shared with the proximal piece."""
    m2, s = split_plane(m, pl)
    sf = s[m2.F]
    pos = (sf.min(1) >= 0) & (sf.max(1) > 0)
    pairs = face_adjacency(m2.F, pos)
    lab = components(len(m2.F), pairs, pos)
    area = m2.tri_area()
    ci = chain_idx(body, chain)
    R = near_radius(pl)
    near_f = (np.linalg.norm(m2.V - pl['c'], axis=1) <= R)[m2.F].any(1)
    distal = np.zeros(len(m2.F), bool)
    for c in np.unique(lab[lab >= 0]):
        fs = lab == c
        if ci is not None:
            share = area[fs & np.isin(m2.part, ci)].sum() / max(area[fs].sum(), 1e-12)
            if share >= distal_min:
                distal |= fs
        elif (fs & near_f).any():
            distal |= fs
    if not distal.any():
        return m2, m2.subset(np.zeros(len(m2.F), bool)), [], 'nothing beyond the plane'
    ring, ncyc = ordered_ring(~distal, distal, m2.F, m2.V)
    note = f'{ncyc} ring loop(s)' if ncyc != 1 else ''
    return m2.subset(~distal), m2.subset(distal), ring, note


def loop_sense(V, ring, pl):
    """+1 / -1: the loop's rotation sense about the plane normal."""
    P = V[ring] - pl['c']
    n = pl['n']
    u = np.cross(n, [1., 0., 0.] if abs(n[0]) < .9 else [0., 1., 0.])
    u /= np.linalg.norm(u)
    w = np.cross(n, u)
    x, y = P @ u, P @ w
    return 1 if (x * np.roll(y, -1) - np.roll(x, -1) * y).sum() > 0 else -1


def zipper(piece, ring_s, ring_pos_b, pl, conform=True, role=None, vrole=None):
    """Attach a stitch strip to `piece` from its own ring `ring_s` (vertex ids, ordered in the piece's winding) to the canonical ring polygon `ring_pos_b`
    (positions, ordered in the winding of the BASE piece on the same side of the seam), so the piece ends exactly on the canonical ring. Returns the
    piece with the strip (prov 'stitch') and the new ring vertex ids."""
    Ps = piece.V[ring_s]
    Pb = np.asarray(ring_pos_b)
    if loop_sense(piece.V, ring_s, pl) != loop_sense(np.asarray(ring_pos_b), np.arange(len(Pb)), pl):
        ring_s = ring_s[::-1]       # the piece's local winding disagrees with the base: follow the base (reported by the caller)
        Ps = piece.V[ring_s]
        flipped = True
    else:
        flipped = False
    gap0 = np.linalg.norm(Ps[:, None, :] - Pb[None], axis=2).min(1) * 1000
    if conform:
        piece = conform_ring(piece, ring_s, Pb)
        Ps = piece.V[ring_s]
    i0 = int(np.argmin(np.linalg.norm(Ps - Pb[0], axis=1)))
    ring_s = list(ring_s[i0:]) + list(ring_s[:i0])
    Ps = piece.V[ring_s]

    def frac(P):
        d = np.linalg.norm(np.roll(P, -1, axis=0) - P, axis=1)
        t = np.r_[0., np.cumsum(d)[:-1]]
        return t / max(d.sum(), 1e-12)
    ts, tb = frac(Ps), frac(Pb)
    n, mm = len(ring_s), len(Pb)
    base_id = len(piece.V)
    bid = np.arange(base_id, base_id + mm)
    tris = []
    i = j = 0
    while i < n or j < mm:
        tin = ts[i + 1] if i + 1 < n else 1.
        tjn = tb[j + 1] if j + 1 < mm else 1.
        if j >= mm or (i < n and tin <= tjn):
            tris.append((ring_s[(i + 1) % n], ring_s[i % n], bid[j % mm]))
            i += 1
        else:
            tris.append((bid[j % mm], bid[(j + 1) % mm], ring_s[i % n]))
            j += 1
    kinds = [0 if t[2] >= base_id else 1 for t in tris]              # 0: two piece vertices, 1: one piece vertex
    tris = np.array(tris, np.int64)
    # role of the strip: the role of the piece faces touching the ring (skin for a bare ring, the garment's role for a lid onto a garment cut)
    rs = set(ring_s)
    touch = np.isin(piece.F, list(rs)).any(1)
    roles = piece.role[touch]
    forced = role
    role = SKIN
    if forced is not None:
        role = forced
    elif len(roles) and (roles == SKIN).mean() < .5:
        vals, cnts = np.unique(roles[roles != SKIN], return_counts=True)
        role = str(vals[cnts.argmax()])
    part = int(np.bincount(piece.part[touch]).argmax()) if touch.any() else 0
    troles = [role] * len(tris)
    if vrole is not None:        # per triangle: a garment role only where the strip closes a garment cut (both piece-side vertices cut through that garment)
        troles = []
        for t, k in zip(tris, kinds):
            pv = [t[0], t[1]] if k == 0 else [t[2]]
            rr = {vrole.get(int(v)) for v in pv}
            troles.append(rr.pop() if len(rr) == 1 and None not in rr else SKIN)
    out = Mesh(np.concatenate([piece.V, Pb]), np.concatenate([piece.F, tris]), np.r_[piece.role, troles], np.r_[piece.part, [part] * len(tris)],
               np.r_[piece.prov, ['stitch'] * len(tris)])
    return out, {'ring_vertices': n, 'canonical_vertices': mm, 'gap_mm_max': float(gap0.max()), 'gap_mm_p50': float(np.median(gap0)), 'strip_role': role, 'winding_followed_base': flipped,
                 'conformed': bool(conform)}


CONFORM_MM = 30.0       # the conform's falloff along the piece's surface from the ring


def conform_ring(piece, ring, Pb):
    """Move the piece's ring vertices onto the canonical ring polygon (closest point on its edges) and carry the move into the piece's own vertices over
    CONFORM_MM along the surface (smooth falloff), so the stitch strip has (almost) no width: a low-poly shell's cross-section can lie several cm off the
    game body's at the seam (big triangles are chords), and the strip would otherwise be a visible shelf."""
    ring = np.asarray(ring)
    A, B = Pb, np.roll(Pb, -1, axis=0)
    X = piece.V[ring]
    ab_ = B - A
    t = np.clip(np.einsum('kij,ij->ki', X[:, None, :] - A[None], ab_) / np.maximum((ab_ ** 2).sum(1), 1e-18)[None], 0., 1.)
    Q = A[None] + t[..., None] * ab_[None]
    j = np.linalg.norm(Q - X[:, None, :], axis=2).argmin(1)
    tgt = Q[np.arange(len(ring)), j]
    D = np.zeros_like(piece.V)
    D[ring] = tgt - X
    e = np.unique(piece.edges(), axis=0)
    w = np.linalg.norm(piece.V[e[:, 0]] - piece.V[e[:, 1]], axis=1) + 1e-9
    G = sparse.coo_matrix((np.r_[w, w], (np.r_[e[:, 0], e[:, 1]], np.r_[e[:, 1], e[:, 0]])), shape=(len(piece.V), len(piece.V))).tocsr()
    dist, _, src = csgraph.dijkstra(G, directed=False, indices=ring, min_only=True, return_predecessors=True)
    tt = np.clip(np.where(np.isfinite(dist), dist, np.inf) / (CONFORM_MM / 1000), 0., 1.)
    fall = 1. - tt * tt * (3. - 2. * tt)
    ok = (src >= 0) & (fall > 0)
    D[ok] = fall[ok, None] * D[src[ok]]
    D[ring] = tgt - X
    out = piece.copy()
    out.V = piece.V + D
    return out


def base_mesh(body):
    return Mesh(body.V, body.F, SKIN, body.part, 'game')


def canonical(body, pl, chain):
    """The game base cut at the plane: (distal piece, ring positions in the distal piece's winding, ring positions in the proximal piece's winding)."""
    prox, dist, ring, note = cut(base_mesh(body), pl, body, chain)
    if not ring:
        raise RuntimeError(f'base has no ring at this plane ({note})')
    Pprox = prox.V[ring]
    return dist, Pprox[::-1].copy(), Pprox.copy()


def mirror_mesh(m, x0, body):
    V = m.V.copy()
    V[:, 0] = 2 * x0 - V[:, 0]
    pn = body.part_names
    swap = np.array([pn.index(n[:-2] + ('.r' if n.endswith('.l') else '.l')) if n[-2:] in ('.l', '.r') and (n[:-2] + ('.r' if n.endswith('.l') else '.l')) in pn else i
                     for i, n in enumerate(pn)])
    prov = np.where(m.prov == 'tripo', 'mirror', m.prov)
    return Mesh(V, m.F[:, [0, 2, 1]], m.role, swap[m.part], prov)


# ----------------------------------------------------------------------------------------------- seams

def choose_plane(m, body, seam, offsets, need_bare=True, also=None):
    """First candidate plane of the seam that is bare skin on `m` (and on `also`, a (mesh, mirrored?) check). Returns (plane, note)."""
    jp = joint_plane(body, SEAMS[seam]['P'], SEAMS[seam]['Q'])
    if jp['island']:
        offsets = [0., .1]                # only planes inside the base island hide the opening
    tried = []
    for f in offsets:
        pl = plane_at(jp, f)
        ok, why = plane_bare(m, pl, body, SEAMS[seam]['chain'])
        tried.append(f'{f:+.2f}: {why}')
        if ok or not need_bare:
            return pl, {'frac': f, 'bare': ok, 'tried': tried}
    pl = plane_at(jp, offsets[0])
    return pl, {'frac': offsets[0], 'bare': False, 'tried': tried}


BAND_STEP_MM = 5.0       # search step of the band's inner plane
BAND_MIN_MM = 10.0       # a shorter bare stretch gets no band (the old stitch at the seam)
BAND_MAX_FRAC = .6       # the band reaches at most this share of the limb part's length from the seam
USE_BANDS = True


def offset_plane(pl, t):
    return {**pl, 'c': pl['c'] + t * pl['n'], 'offset_mm': round(t * 1000, 1)}


def base_cut(body, pl, chain):
    """The game base cut at the seam plane: (proximal piece, distal piece, ring ids (proximal winding) into their shared vertex array)."""
    prox, dist, ring, note = cut(base_mesh(body), pl, body, chain)
    if not ring:
        raise RuntimeError(f'base has no ring at this plane ({note})')
    return prox, dist, np.asarray(ring)


def band_extent(piece, pl, body, chain, side, L):
    """Signed offset (m, along the seam normal) of the band's inner plane: as far from the seam toward the piece's own garment (side 'prox': -n,
    'dist': +n) as the piece stays bare skin (plane_bare keeps BAND_MM off the garment); None when that is shorter than BAND_MIN_MM."""
    sgn = -1. if side == 'prox' else 1.
    step, last, t = BAND_STEP_MM / 1000, None, 0.
    while t <= L * BAND_MAX_FRAC:
        ok, _ = plane_bare(piece, offset_plane(pl, sgn * t), body, chain)
        if not ok:
            break
        last = t
        t += step
    if last is None or last < BAND_MIN_MM / 1000:
        return None
    return sgn * last


BAND_TILTS_DEG = (0., 10., 20., 30.)   # the band's inner plane may tilt this much off the seam plane to follow an oblique hem
BAND_CLEAR_MM = 5.0                      # the inner plane must stay this far from the seam ring all round


def band_planes(piece, pl, body, chain, side, L):
    """The band's inner plane: from the seam toward the piece's garment (side 'prox': -n, 'dist': +n), tilted up to BAND_TILTS_DEG to follow an
    oblique hem, as far from the seam as the piece stays bare skin (plane_bare keeps BAND_MM off the garment) and at least BAND_CLEAR_MM from the
    seam plane wherever it crosses the game limb. Returns each direction's furthest candidate (at least BAND_MIN_MM from the seam), largest mean
    distance from the seam first; seam_piece takes the first whose cut through the piece lies on the game limb's cut (ring_gap)."""
    sgn = -1. if side == 'prox' else 1.
    n = pl['n'] / np.linalg.norm(pl['n'])
    u = np.cross(n, [0., 0., 1.] if abs(n[2]) < .9 else [1., 0., 0.]); u /= np.linalg.norm(u)
    v = np.cross(n, u)
    base = base_mesh(body)
    ci = chain_idx(body, chain)
    E = np.unique(np.sort(base.edges(), axis=1), axis=0)
    if ci is not None:                       # edges of the seam's chain parts only
        fm = np.isin(body.part, ci)
        ev = np.zeros(len(body.V), bool); ev[np.unique(body.F[fm])] = True
        E = E[ev[E].all(1)]
    P0, P1 = body.V[E[:, 0]], body.V[E[:, 1]]
    R = near_radius(pl)
    out = []
    step = BAND_STEP_MM / 1000
    for ta in BAND_TILTS_DEG:
        for ang in ((0.,) if ta == 0 else np.radians(np.arange(0, 360, 45))):
            nn = n + np.tan(np.radians(ta)) * (np.cos(ang) * u + np.sin(ang) * v)
            nn /= np.linalg.norm(nn)
            t, last = step, None
            while t <= L * BAND_MAX_FRAC:
                cand = {**pl, 'c': pl['c'] + sgn * t * n, 'n': nn}
                d0, d1 = (P0 - cand['c']) @ nn, (P1 - cand['c']) @ nn
                x = (d0 * d1) < 0
                if not x.any():
                    break
                X = P0[x] + (d0[x] / (d0[x] - d1[x]))[:, None] * (P1[x] - P0[x])
                X = X[np.linalg.norm(X - cand['c'], axis=1) <= R]
                if not len(X):
                    break
                ds = sgn * ((X - pl['c']) @ n)                       # distance of the inner ring from the seam plane, toward the piece
                if ds.min() < BAND_CLEAR_MM / 1000:
                    t += step
                    continue
                ok, _ = plane_bare(piece, cand, body, chain)
                if not ok:
                    break
                last = (cand, float(ds.mean()), float(ds.min()), float(ds.max()))
                t += step
            if last and last[1] >= BAND_MIN_MM / 1000:
                cand, mean, lo, hi = last
                tilt = float(np.degrees(np.arccos(np.clip(abs(cand['n'] @ n), -1, 1))))
                out.append({**cand, 'offset_mm': round(mean * 1000, 1), 'band_mm': [round(lo * 1000, 1), round(hi * 1000, 1)], 'tilt_deg': round(tilt, 1)})
    out.sort(key=lambda c: -c['offset_mm'])
    return out


def seam_piece(piece, pl, side, body, chain, L, bc, seam=None):
    """The part of `piece` on `side` ('prox' / 'dist') of the seam plane, ending exactly on the canonical ring. Where the piece is bare skin next to the
    seam, that skin is replaced by a band of the GAME base's own surface (from the seam to just short of the piece's garment), so the seam joins game skin
    to game skin with the same facets on both sides and the only generator / game join sits at the garment hem. bc = base_cut(body, pl, chain).
    Returns (mesh, seam ring vertex ids in the mesh (in the mesh's winding), info)."""
    prox_b, dist_b, ring = bc
    P = prox_b.V[ring]                                   # canonical ring, proximal winding
    tried = []
    if USE_SKIN_JOIN and seam is not None and plane_bare(piece, pl, body, chain)[0]:
        try:
            return skin_join(piece, pl, side, body, chain, bc, seam)
        except RuntimeError as error:
            tried.append(f'skin join: {error}')
    for pl_in in (band_planes(piece, pl, body, chain, side, L) if USE_BANDS else []):
        try:
            res = band_join(piece, pl_in, side, body, chain, bc)
        except RuntimeError as error:
            tried.append(f'{pl_in["offset_mm"]} mm / {pl_in["tilt_deg"]} deg: {error}')
            continue
        res[2]['band']['rejected'] = tried
        return res
    prox, dist, pring, note = cut(piece, pl, body, chain)
    if not pring:
        raise RuntimeError(f'no ring on the piece ({note})')
    if side == 'prox':
        out, zi = to_hem(prox, np.array(pring), P, pl, -1., body, chain, at_seam=True)
    else:
        out, zi = to_hem(dist, np.array(pring)[::-1], P[::-1], pl, 1., body, chain, at_seam=True)
    ids = np.arange(len(out.V) - len(P), len(out.V))                         # zipper appends the canonical ring last
    return out, ids, {'band': None, 'band_rejected': tried, 'stitch': zi, 'cut_note': note}


# ----------------------------------------------------------------------------------------------- skin join
USE_SKIN_JOIN = True
SNAP_MM = 30.0           # ... when one lies within this of the vertex's place on the edge
SNAP_TO_EDGE_VERTICES = True   # game edge vertices go onto the outfit edge's own vertices (and merge), not anywhere along it
SKIN_REACH = .22         # the outfit skin replaced at a seam lies within this fraction of the body height from the seam centre
MOVE_MAX_MM = 150.0      # a game edge vertex may move at most this far onto the garment edge
LIFT_MAX_MM = 12.0       # a game edge vertex rises at most this far off the body to meet the outfit edge; further, it stays on the body
SKIN_SEED_MM = 50.0      # skin faces this close to the seam plane are replaced whatever their part label


def side_parts(seam, side):
    """Body parts whose outfit skin is replaced on one side of a seam (the side's own limb segment; the neck takes the upper chest, a wrist / ankle
    the whole arm / leg segment above it)."""
    s = seam.split('.')[-1] if '.' in seam else 'x'
    kind = seam.split('.')[0]
    table = {'head': (('neck.x', 'spine_01.x'), ('head.x',)),
             'hand': ((f'forearm_stretch.{s}', f'arm_stretch.{s}'), (f'hand.{s}',)),
             'foot': ((f'leg_stretch.{s}', f'thigh_stretch.{s}'), (f'foot.{s}',)),
             'elbow': ((f'arm_stretch.{s}',), (f'forearm_stretch.{s}', f'hand.{s}')),
             'knee': ((f'thigh_stretch.{s}',), (f'leg_stretch.{s}', f'foot.{s}'))}
    return table[kind][0 if side == 'prox' else 1]


def outfit_closest(m):
    """Closest-point helper over a Mesh: a bf.Body whose 'part' carries the Mesh face index."""
    return bf.Body(m.V, m.F, np.arange(len(m.F)), [])


def seg_closest(X, A, B):
    """Closest point to each X on the segments A[k]-B[k]: (points, distances, segment index)."""
    ab_ = B - A
    t = np.clip(np.einsum('kij,ij->ki', X[:, None, :] - A[None], ab_) / np.maximum((ab_ ** 2).sum(1), 1e-18)[None], 0., 1.)
    Q = A[None] + t[..., None] * ab_[None]
    d = np.linalg.norm(Q - X[:, None, :], axis=2)
    j = d.argmin(1)
    return Q[np.arange(len(X)), j], d[np.arange(len(X)), j], j


REFINE_EDGE_MM = 60.0    # game triangles straddling the replaced skin's edge are split until their edges are shorter than this
REFINE_ITERS = 2


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


def label_under(Gm, oc, S):
    """Per game face: how many of 4 samples have their closest outfit face in S."""
    GA, GB, GC = Gm.V[Gm.F[:, 0]], Gm.V[Gm.F[:, 1]], Gm.V[Gm.F[:, 2]]
    gc = (GA + GB + GC) / 3
    _, tri, _ = oc.closest(np.concatenate([gc, (2 * gc + GA) / 3, (2 * gc + GB) / 3, (2 * gc + GC) / 3]))
    return S[oc.part[tri]].reshape(4, -1).sum(0)


def fill_holes(m, mask, frac=.5):
    """Add to `mask` the faces of every connected non-mask region of m that touches the mask region and is smaller than frac of its area, except the
    largest such region (the outside)."""
    rest = ~mask
    pairs = face_adjacency(m.F, rest)
    lab = components(len(m.F), pairs, rest)
    area = m.tri_area()
    labs = [l_ for l_ in np.unique(lab) if l_ >= 0]
    if len(labs) < 2:
        return mask
    sizes = {l_: float(area[lab == l_].sum()) for l_ in labs}
    big = max(sizes, key=sizes.get)
    out = mask.copy()
    for l_ in labs:
        if l_ != big and sizes[l_] < frac * float(area[mask].sum()):
            out |= lab == l_
    return out


def splice_loops(loops):
    """Join boundary loops that share a vertex (one boundary pinched at a vertex) into single loops."""
    loops = [list(lp) for lp in loops]
    merged = True
    while merged and len(loops) > 1:
        merged = False
        for a in range(len(loops)):
            for b in range(a + 1, len(loops)):
                common = set(loops[a]) & set(loops[b])
                if common:
                    v = next(iter(common))
                    la, lb = loops[a], loops[b]
                    k, r = la.index(v), lb.index(v)
                    lb = lb[r:] + lb[:r]
                    loops[a] = la[:k] + lb + la[k:]
                    del loops[b]
                    merged = True
                    break
            if merged:
                break
    return loops


def pav(y):
    """Pool-adjacent-violators: the closest non-decreasing sequence (least squares)."""
    vals, wts, cnt = [], [], []
    for v in y:
        vals.append(float(v)); wts.append(1.); cnt.append(1)
        while len(vals) > 1 and vals[-2] > vals[-1]:
            w = wts[-2] + wts[-1]
            v2 = (vals[-2] * wts[-2] + vals[-1] * wts[-1]) / w
            c = cnt[-2] + cnt[-1]
            vals[-2:], wts[-2:], cnt[-2:] = [v2], [w], [c]
    out = []
    for v, c in zip(vals, cnt):
        out += [v] * c
    return np.array(out)


def point_on_loop(HA, HB, hcum, Ltot, t):
    """Points at the loop positions t (fractions of the loop length, any real: taken mod 1) of the closed polyline with segments HA-HB."""
    u = (np.asarray(t) % 1.) * Ltot
    k = np.clip(np.searchsorted(hcum, u, side='right') - 1, 0, len(HA) - 1)
    seg = np.linalg.norm(HB - HA, axis=1)
    f = np.clip((u - hcum[k]) / np.maximum(seg[k], 1e-12), 0., 1.)
    return HA[k] + f[:, None] * (HB[k] - HA[k])


RELAX_RINGS = 2
RELAX_ITERS = 8


def relax_near_edge(m, edge, ring, body):
    """Untangle the game patch next to its moved edge: the vertices within RELAX_RINGS edge-rings of it (not the edge, not the canonical ring) are
    moved to the average of their neighbours and back onto the game body surface, RELAX_ITERS times."""
    E = np.unique(np.sort(np.concatenate([m.F[:, [0, 1]], m.F[:, [1, 2]], m.F[:, [2, 0]]]), axis=1), axis=0)
    nbr = [[] for _ in range(len(m.V))]
    for a_, b_ in E:
        nbr[a_].append(b_)
        nbr[b_].append(a_)
    fixed = set(int(x) for x in edge) | set(int(x) for x in ring)
    front, free = set(int(x) for x in edge), set()
    for _ in range(RELAX_RINGS):
        nxt = set()
        for v in front:
            for w in nbr[v]:
                if w not in fixed and w not in free:
                    nxt.add(w)
        free |= nxt
        front = nxt
    first = set()
    for v in edge:
        first |= {w for w in nbr[int(v)] if w in free}
    free = np.array(sorted(free), np.int64)
    if not len(free):
        return
    lift = np.array([v in first for v in free])          # the row next to the edge may rise off the body (spreads the step up to a garment edge)
    for _ in range(RELAX_ITERS):
        avg = np.array([m.V[nbr[v]].mean(0) for v in free])
        d, tri, bary = body.closest(avg)
        on = bary[:, :1] * body.A[tri] + bary[:, 1:2] * body.B[tri] + bary[:, 2:] * body.C[tri]
        outside = ((avg - on) * body.tri_n[tri]).sum(1) > 0
        m.V[free] = np.where((lift & outside)[:, None], avg, on)


def relax_folds(m, nb, edge, ring, body, iters=6):
    """Untangle only where the edge move folded game faces (normal turned against its original nb): the free vertices of those faces and their
    neighbours go to the average of their neighbours, back on the game body (or above it next to a lifted edge), until nothing is folded. Every other
    vertex keeps its place on the base's own flat faces, so the low-poly facets stay the base's."""
    E = np.unique(np.sort(np.concatenate([m.F[:, [0, 1]], m.F[:, [1, 2]], m.F[:, [2, 0]]]), axis=1), axis=0)
    nbr = [[] for _ in range(len(m.V))]
    for a_, b_ in E:
        nbr[a_].append(b_)
        nbr[b_].append(a_)
    fixed = set(int(x) for x in edge) | set(int(x) for x in ring)
    edge_s = set(int(x) for x in edge)
    for _ in range(iters):
        na = np.cross(m.V[m.F[:, 1]] - m.V[m.F[:, 0]], m.V[m.F[:, 2]] - m.V[m.F[:, 0]])
        bad = (nb * na).sum(1) < 0
        if not bad.any():
            return
        vs = set(int(v) for v in np.unique(m.F[bad]))
        vs |= {w for v in list(vs) for w in nbr[v]}
        free = np.array(sorted(v for v in vs if v not in fixed), np.int64)
        if not len(free):
            return
        avg = np.array([m.V[nbr[v]].mean(0) for v in free])
        d, tri, bary = body.closest(avg)
        on = bary[:, :1] * body.A[tri] + bary[:, 1:2] * body.B[tri] + bary[:, 2:] * body.C[tri]
        lift = np.array([bool(set(nbr[v]) & edge_s) for v in free]) & (((avg - on) * body.tri_n[tri]).sum(1) > 0)
        m.V[free] = np.where(lift[:, None], avg, on)


def skin_join(piece, pl, side, body, chain, bc, seam):
    """Seam join with game skin up to the garment edge, joined by moving vertices (no fill strip of its own).
    The outfit skin connected to the seam on this side (its limb segment, within SKIN_REACH) is removed; the game base faces lying under that skin
    (closest outfit face is one of the removed ones), grown from the canonical ring, replace it. Under a loose garment (tassets, a skirt) the outfit
    has skin beneath it, so the game skin carries on up under it. The game patch's outer edge vertices are then moved onto the outfit's open edge
    (the garment edge, or the attachment line under a loose garment) and the two edges are joined; both lie on the same line, so the join triangles
    have (almost) no area. Raises RuntimeError when the regions are not one simple band (the caller falls back to the band / stitch joins)."""
    prox_b, dist_b, ring = bc
    pp, pd, pring, note = cut(piece, pl, body, chain)
    if not pring:
        raise RuntimeError(f'no ring on the piece ({note})')
    P = pp if side == 'prox' else pd
    Gb = prox_b if side == 'prox' else dist_b
    pring = np.asarray(pring)
    # 1. the outfit skin to replace
    allowed = [body.part_names.index(x) for x in side_parts(seam, side) if x in body.part_names]
    cen = P.V[P.F].mean(1)
    reach = SKIN_REACH * body.H
    sd = np.abs((cen - pl['c']) @ pl['n'])
    cand = (P.role == SKIN) & (np.linalg.norm(cen - pl['c'], axis=1) <= reach) & (np.isin(P.part, allowed) | (sd <= SKIN_SEED_MM / 1000))
    seed = np.isin(P.F, pring).any(1)
    if (seed & (P.role != SKIN)).any():
        raise RuntimeError('garment touches the seam ring')
    pairs = face_adjacency(P.F, cand)
    lab = components(len(P.F), pairs, cand)
    S = np.isin(lab, np.unique(lab[seed & cand])) & (lab >= 0)
    if not S.any():
        raise RuntimeError('no outfit skin at the seam')
    # 2. the game skin under it: game faces of this side's own parts whose closest outfit face is replaced skin, grown from the canonical ring;
    #    triangles straddling the edge are split (the base's limb triangles are long) so the edge follows the outfit's
    oc = outfit_closest(P)
    ringset = set(int(x) for x in ring)
    Gm = Gb.copy()
    gpart_ok = np.isin(Gm.part, allowed)
    ring_e = set()
    for k in range(len(ring)):
        a_, b_ = int(ring[k]), int(ring[(k + 1) % len(ring)])
        ring_e.add((min(a_, b_), max(a_, b_)))
    for it in range(REFINE_ITERS + 1):
        cnt = label_under(Gm, oc, S)
        on_ring = np.isin(Gm.F, ring).any(1)
        gm = ((cnt >= 2) & gpart_ok) | on_ring
        if it == REFINE_ITERS:
            break
        mixed = ((cnt > 0) & (cnt < 4) & gpart_ok) | (on_ring & (cnt < 4))
        E = np.concatenate([Gm.F[mixed][:, [0, 1]], Gm.F[mixed][:, [1, 2]], Gm.F[mixed][:, [2, 0]]])
        E = np.unique(np.sort(E, axis=1), axis=0)
        L_ = np.linalg.norm(Gm.V[E[:, 0]] - Gm.V[E[:, 1]], axis=1)
        mark = {tuple(map(int, e)) for e, l_ in zip(E, L_) if l_ > REFINE_EDGE_MM / 1000 and tuple(map(int, e)) not in ring_e}
        if not mark:
            break
        V2, F2, par = split_edges(Gm.V, Gm.F, mark)
        Gm = Mesh(V2, F2, Gm.role[par], Gm.part[par], Gm.prov[par])
        gpart_ok = np.isin(Gm.part, allowed)
    pairs = face_adjacency(Gm.F, gm)
    lab = components(len(Gm.F), pairs, gm)
    G = np.isin(lab, np.unique(lab[on_ring])) & (lab >= 0)
    G = fill_holes(Gm, G)
    # the outfit skin removed is the skin over the game patch (so both regions end on the same line)
    gb = bf.Body(Gm.V, Gm.F, np.arange(len(Gm.F)), [])
    _, gt, _ = gb.closest(cen)
    over = G[gb.part[gt]]
    S2 = S & (over | seed)
    if not S2.any():
        raise RuntimeError('no outfit skin over the game patch')
    pairs = face_adjacency(P.F, S2)
    lab = components(len(P.F), pairs, S2)
    S = np.isin(lab, np.unique(lab[seed & S2])) & (lab >= 0)
    keep = P.subset(~S)
    sv = np.zeros(len(P.V), bool)
    sv[np.unique(P.F[S])] = True
    hl = [lp for lp in boundary_loops(keep) if sv[lp].mean() > .6]
    if len(hl) != 1:
        raise RuntimeError(f'{len(hl)} outfit edge loops around the replaced skin')
    H = np.array(hl[0])
    gsub = Gm.subset(G)
    gl = boundary_loops(gsub)
    outer = splice_loops([lp for lp in gl if not (set(lp) & ringset)])
    inner = [lp for lp in gl if set(lp) & ringset]
    if len(outer) != 1 or len(inner) != 1 or len(inner[0]) != len(ring):
        raise RuntimeError(f'game patch has {len(outer)} outer / {len(inner)} inner edge loops')
    Bg = np.array(outer[0])
    Gm = gsub
    Gm.V = Gm.V.copy()
    # 3. move the game patch's outer edge onto the outfit edge
    HA, HB = keep.V[H], keep.V[np.roll(H, -1)]
    Q, d, seg = seg_closest(Gm.V[Bg], HA, HB)
    hl_ = np.linalg.norm(HB - HA, axis=1)
    hcum = np.r_[0., np.cumsum(hl_)[:-1]]
    gpar = (hcum[seg] + np.linalg.norm(Q - HA[seg], axis=1)) / max(hl_.sum(), 1e-12)     # position of each moved game vertex along the outfit edge
    if d.max() > MOVE_MAX_MM / 1000:
        raise RuntimeError(f'a game edge vertex would move {d.max() * 1000:.0f} mm')
    # 4. order the game edge along the outfit edge (game edge reversed: it then runs the same way round), unwrap the positions, make them
    #    non-decreasing (pool-adjacent-violators) and put each game edge vertex at its ordered position on the outfit edge
    off = len(keep.V)
    order = np.arange(len(Bg))[::-1]
    Gs, gp = Bg[order], gpar[order]
    i0 = int(np.argmin(gp))
    Gs, gp = np.r_[Gs[i0:], Gs[:i0]], np.r_[gp[i0:], gp[:i0]]
    gp = np.unwrap(gp * 2 * np.pi) / (2 * np.pi)
    steps = np.diff(gp)
    if (steps < -1e-9).sum() > (steps > 1e-9).sum():      # the moved game edge runs backwards along the outfit edge
        raise RuntimeError('outfit and game edges wind opposite ways')
    back = int((steps < -1e-9).sum())
    tg = pav(gp)
    tg = tg - np.floor(tg[0])
    Ltot = max(hl_.sum(), 1e-12)
    if SNAP_TO_EDGE_VERTICES:     # each game edge vertex goes onto the nearest outfit edge VERTEX (in order); the ones that land together merge below
        thv = hcum / Ltot
        dd_ = ((tg[:, None] - thv[None, :] + .5) % 1.) - .5
        kk = np.abs(dd_).argmin(1)
        Qseg = point_on_loop(HA, HB, hcum, Ltot, tg)
        near_v = np.linalg.norm(keep.V[H[kk]] - Qseg, axis=1) <= SNAP_MM / 1000      # an outfit vertex close by: go onto it, else stay on the edge
        tg = np.where(near_v, np.floor(tg) + thv[kk] + np.where(thv[kk] - (tg % 1.) > .5, -1., 0.) + np.where((tg % 1.) - thv[kk] > .5, 1., 0.), tg)
        Qm = np.where(near_v[:, None], keep.V[H[kk]], Qseg)
    else:
        Qm = point_on_loop(HA, HB, hcum, Ltot, tg)
    d = np.linalg.norm(Qm - Gm.V[Gs], axis=1)
    if d.max() > MOVE_MAX_MM / 1000:
        raise RuntimeError(f'a game edge vertex would move {d.max() * 1000:.0f} mm')
    # an edge point far off the body (the join line on a loose garment's inside, e.g. under tassets) stays on the body: the join strip closes
    # that gap out of sight instead of the game skin bulging up to it
    dq, tq, bq = body.closest(Qm)
    onb = bq[:, :1] * body.A[tq] + bq[:, 1:2] * body.B[tq] + bq[:, 2:] * body.C[tq]
    far = dq > LIFT_MAX_MM / 1000
    Qm = np.where(far[:, None], onb, Qm)
    nb = np.cross(Gm.V[Gm.F[:, 1]] - Gm.V[Gm.F[:, 0]], Gm.V[Gm.F[:, 2]] - Gm.V[Gm.F[:, 0]])
    Gm.V[Gs] = Qm
    flipped_raw = int(((nb * np.cross(Gm.V[Gm.F[:, 1]] - Gm.V[Gm.F[:, 0]], Gm.V[Gm.F[:, 2]] - Gm.V[Gm.F[:, 0]])).sum(1) < 0).sum())
    relax_folds(Gm, nb, Gs, ring, body)
    na = np.cross(Gm.V[Gm.F[:, 1]] - Gm.V[Gm.F[:, 0]], Gm.V[Gm.F[:, 2]] - Gm.V[Gm.F[:, 0]])
    flipped = int(((nb * na).sum(1) < 0).sum())
    tg = tg % 1.
    if tg[-1] < tg[0]:                                     # the ordered run crossed the outfit edge's start: rotate it back to start there
        k = int(np.argmin(tg))
        Gs, tg = np.r_[Gs[k:], Gs[:k]], np.r_[tg[k:], tg[:k]]
    th = hcum / Ltot
    n, m_ = len(H), len(Gs)
    tris, i, j = [], 0, 0
    while i < n or j < m_:
        tin = th[i + 1] if i + 1 < n else 1.
        tjn = tg[j + 1] if j + 1 < m_ else 1.
        if j >= m_ or (i < n and tin <= tjn):
            tris.append((H[(i + 1) % n], H[i % n], off + Gs[j % m_]))
            i += 1
        else:
            tris.append((off + Gs[j % m_], off + Gs[(j + 1) % m_], H[i % n]))
            j += 1
    tris = np.array(tris, np.int64)
    part = int(np.bincount(P.part[S]).argmax())
    nt = len(tris)
    out = Mesh(np.concatenate([keep.V, Gm.V]), np.concatenate([keep.F, tris, Gm.F + off]),
               np.r_[keep.role, [SKIN] * nt, [SKIN] * len(Gm.F)], np.r_[keep.part, [part] * nt, Gm.part],
               np.r_[keep.prov, ['stitch'] * nt, ['band'] * len(Gm.F)])
    ids = (np.asarray(ring) if side == 'prox' else np.asarray(ring)[::-1]) + off
    # weld: game edge vertices that landed on an outfit edge vertex become that vertex; faces collapsed by it go
    q = np.round(out.V / 1e-9).astype(np.int64)
    _, first, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    inv = inv.ravel()
    F = inv[out.F]
    okf = (F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])
    out = Mesh(out.V[first], F[okf], out.role[okf], out.part[okf], out.prov[okf])
    ids = inv[ids]
    st = out.prov == 'stitch'
    T_ = out.V[out.F[st]]
    area = np.linalg.norm(np.cross(T_[:, 1] - T_[:, 0], T_[:, 2] - T_[:, 0]), axis=1).sum() / 2
    nt = int(st.sum())
    info = {'skin_join': {'outfit_skin_faces_replaced': int(S.sum()), 'game_faces': int(len(Gm.F)), 'edge_vertices': [int(n), int(m_)], 'join_faces': nt,
                          'move_mm': [round(float(np.median(d)) * 1000, 1), round(float(d.max()) * 1000, 1)], 'join_area_cm2': round(float(area) * 1e4, 2),
                          'game_faces_flipped': flipped, 'flipped_before_relax': flipped_raw, 'edge_kept_on_body': int(far.sum()), 'edge_order_glitches': back}, 'band': None, 'cut_note': note}
    return out, ids, info


BAND_GAP_MM = 25.0       # a band candidate is used only when the piece's cut at its inner plane lies within this of the game limb's cut


def ring_gap(X, Pb):
    """Largest distance (m) from the points X to the closed polygon Pb."""
    A, B = Pb, np.roll(Pb, -1, axis=0)
    ab_ = B - A
    t = np.clip(np.einsum('kij,ij->ki', X[:, None, :] - A[None], ab_) / np.maximum((ab_ ** 2).sum(1), 1e-18)[None], 0., 1.)
    Q = A[None] + t[..., None] * ab_[None]
    return float(np.linalg.norm(Q - X[:, None, :], axis=2).min(1).max())


def band_join(piece, pl_in, side, body, chain, bc):
    """seam_piece with a game-skin band up to the inner plane pl_in (see there). Raises RuntimeError when a cut fails or the piece's cut ring is not
    on the game limb's (gap over BAND_GAP_MM: the plane caught another garment, e.g. a skirt hem near the ankle)."""
    prox_b, dist_b, ring = bc
    if side == 'prox':
        _, band, ring_in, bnote = cut(prox_b, pl_in, body, chain)          # game skin between the inner plane and the seam
        Pin = band.V[ring_in]                                               # proximal winding of the inner ring
        pp, _, pring, note = cut(piece, pl_in, body, chain)
        if not pring or not ring_in:
            raise RuntimeError(f'band cut failed (piece: {note}, base: {bnote})')
        g = ring_gap(pp.V[pring], Pin)
        if g > BAND_GAP_MM / 1000:
            raise RuntimeError(f'piece ring {g * 1000:.0f} mm off the game ring')
        pz, zi = to_hem(pp, np.array(pring), Pin, pl_in, -1., body, chain)
        ids = ring + len(pz.V)                                              # the band keeps the base vertex ids: seam ring in proximal winding
    else:
        band, _, ring_in, bnote = cut(dist_b, pl_in, body, chain)
        Pin = band.V[ring_in][::-1]                                         # distal winding of the inner ring
        _, pd, pring, note = cut(piece, pl_in, body, chain)
        if not pring or not ring_in:
            raise RuntimeError(f'band cut failed (piece: {note}, base: {bnote})')
        g = ring_gap(pd.V[pring], Pin)
        if g > BAND_GAP_MM / 1000:
            raise RuntimeError(f'piece ring {g * 1000:.0f} mm off the game ring')
        pz, zi = to_hem(pd, np.array(pring)[::-1], Pin, pl_in, 1., body, chain)
        ids = ring[::-1] + len(pz.V)                                        # seam ring in the distal winding
    band = band.copy()
    band.prov[:] = 'band'
    return concat([pz, band]), ids, {'band': {'length_mm': pl_in['offset_mm'], 'range_mm': pl_in['band_mm'], 'tilt_deg': pl_in['tilt_deg'], 'faces': int(len(band.F)),
                                              'ring_gap_mm': round(g * 1000, 1)}, 'stitch': zi, 'cut_note': note}


HEM_REACH_MM = 70.0      # the strip of generator skin between the band and the garment hem is removed when the hem lies within this of the band


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


def to_hem(piece, ring, Pin, pl_in, sgn, body, chain, at_seam=False):
    """Join the band's inner ring to the piece. The piece was cut at the band's inner plane; the generator skin left between that plane and the
    garment hem (up to HEM_REACH_MM) is removed and the band stitched straight to the hem, so the generator / game join hugs the garment edge.
    sgn = direction from the plane into the piece (-1: the piece is on the proximal side). Falls back to the plain conformed stitch.
    at_seam: the plane is the seam itself and the piece's garment may cross it; where it does, the cut garment edge is conformed onto the canonical
    ring and its strip keeps the garment's role (the rest of the strip, from the ring to the hem, is skin)."""
    s = sgn * ((piece.V - pl_in['c']) @ pl_in['n'])                          # distance from the plane into the piece
    R = near_radius(pl_in)
    near = np.linalg.norm(piece.V - pl_in['c'], axis=1) <= R + HEM_REACH_MM / 1000
    ci = chain_idx(body, chain)
    inchain = np.isin(piece.part, ci) if ci is not None else np.ones(len(piece.F), bool)
    skin = (piece.role == SKIN) & inchain
    garm_v = np.unique(piece.F[(piece.role != SKIN) & inchain])
    garm_v = garm_v[near[garm_v]]
    if not len(garm_v):
        return zipper(piece, ring, Pin, pl_in)
    s_lim = min(float(s[garm_v].max()), HEM_REACH_MM / 1000) + .002
    ok_f = skin & near[piece.F].all(1) & (s[piece.F].max(1) <= s_lim)
    # flood from the faces on the cut ring through removable skin faces
    pairs = face_adjacency(piece.F, ok_f)
    lab = components(len(piece.F), pairs, ok_f)
    seed = np.isin(piece.F, ring).any(1) & ok_f
    kill = np.isin(lab, np.unique(lab[seed])) & (lab >= 0)
    if not kill.any():
        return zipper(piece, ring, Pin, pl_in)
    keep = piece.subset(~kill)
    loops = [lp for lp in boundary_loops(keep) if near[lp].mean() > .8]
    if not loops:
        return zipper(piece, ring, Pin, pl_in)
    lp = np.array(loops[0])
    vrole, gcut = {}, []
    on_plane = np.abs(s[lp]) < 5e-4
    for v, op in zip(lp, on_plane):
        if not op:
            continue
        rr = keep.role[(keep.F == v).any(1)]
        rr = rr[rr != SKIN]
        if len(rr):
            vals, cnts = np.unique(rr, return_counts=True)
            vrole[int(v)] = str(vals[cnts.argmax()])
            gcut.append(int(v))
    if at_seam and gcut:
        keep = conform_ring(keep, np.array(gcut), Pin)
    out, zi = zipper(keep, lp, Pin, pl_in, conform=False, role=SKIN, vrole=vrole)
    zi['garment_cut_vertices'] = len(gcut)
    zi['hem'] = {'removed_skin_faces': int(kill.sum()), 'loop_vertices': int(len(lp)), 'reach_mm': round(s_lim * 1000, 1)}
    return out, zi


def replace_with_base(cur, body, seam, pl, notes):
    """Remove the distal part of `cur` beyond the plane and put the game base's own distal piece there (stitched to the canonical ring; with a game-skin
    band on the shell side where it is bare, see seam_piece)."""
    chain = SEAMS[seam]['chain']
    if pl.get('island'):                 # the base part is its own island overlapping P: the shell's opening stays open inside it (as the base's own joint does)
        prox, dist, ring, note = cut(cur, pl, body, chain)
        notes[seam] = {**notes.get(seam, {}), 'island': True, 'cut_note': note, 'removed_faces': int(len(dist.F))}
        return prox, [island_piece(body, SEAMS[seam]['Q'])]
    bc = base_cut(body, pl, chain)
    jp = joint_plane(body, SEAMS[seam]['P'], SEAMS[seam]['Q'])
    try:
        out, _, info = seam_piece(cur, pl, 'prox', body, chain, jp['Lp'], bc, seam)
    except RuntimeError as error:
        notes[seam] = {**notes.get(seam, {}), 'error': str(error)}
        return cur, []
    notes[seam] = {**notes.get(seam, {}), **info}
    return out, [bc[1]]


def has_footwear(m, body, side='r'):
    fp = [body.part_names.index(x) for x in (f'foot.{side}',) if x in body.part_names]
    g = np.isin(m.part, fp) & (m.role != SKIN)
    return float(m.tri_area()[g].sum() * 1e4)


def mirror_left_foot(cur, body, x0, notes):
    """Outfits: the anatomical-left lower leg below the highest bare plane on the left shin (contiguous bare from the ankle up) is replaced by the mirror of
    the right one below the mirrored plane; both meet the left canonical ring."""
    jp = joint_plane(body, 'leg_stretch.l', 'foot.l')
    fr = [0., -.1, -.2, -.3, -.4, -.5, -.6]
    chosen, tried = None, []
    for f in fr:
        pl = plane_at(jp, f)
        ok, why = plane_bare(cur, pl, body, ('leg_stretch.l', 'foot.l'))
        tried.append(f'{f:+.2f}: {why}')
        if not ok:
            break
        chosen = pl
    if chosen is None:
        for f in (.1, .2, .3):                          # the left garment covers the ankle: try inside the foot
            pl = plane_at(jp, f)
            ok, why = plane_bare(cur, pl, body, ('leg_stretch.l', 'foot.l'))
            tried.append(f'{f:+.2f}: {why}')
            if ok:
                chosen = pl
                break
    note = {'tried': tried}
    if chosen is None:
        notes['foot.l mirror'] = {**note, 'error': 'no bare plane on the left lower leg: left foot kept as generated'}
        return cur, []
    plr = reflect_plane(chosen, x0)
    plr['n'] = plr['n'] if (plr['n'] @ joint_plane(body, 'leg_stretch.r', 'foot.r')['n']) > 0 else -plr['n']
    chain_r = ('leg_stretch.r', 'foot.r')
    _, rdist, rring, rnote = cut(cur, plr, body, chain_r)
    right_bare, why_r = plane_bare(cur, plr, body, chain_r)
    prox, ldist, lring, lnote = cut(cur, chosen, body, ('leg_stretch.l', 'foot.l'))
    if not rring or not lring:
        notes['foot.l mirror'] = {**note, 'error': f'ring missing (right: {rnote}, left: {lnote})'}
        return cur, []
    bdist, ring_d, ring_p = canonical(body, chosen, ('leg_stretch.l', 'foot.l'))
    prox, z1 = zipper(prox, np.array(lring), ring_p, chosen)
    mir = mirror_mesh(rdist, x0, body)
    # the right distal piece's ring, in ITS winding = reverse of the proximal order; after mirroring (faces flipped) the order reverses again
    ring_m = np.array(rring)
    piece, z2 = zipper(mir, ring_m, ring_d, chosen)
    notes['foot.l mirror'] = {**note, 'frac': chosen['frac'], 'right_plane_bare': right_bare, 'right_plane': why_r, 'stitch_left': z1, 'stitch_mirror': z2}
    return prox, [piece]


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


def write_result(name, m, U1, extra_ghosts, record, extra_targets=None):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f'{name}.glb').write_bytes(glb_bytes(m, U1, extra=extra_targets))
    for gname, gm in extra_ghosts.items():
        (OUT / f'{name}__{gname}.glb').write_bytes(glb_bytes(gm))
    sc = OUT / 'scenes'
    sc.mkdir(exist_ok=True)
    for tag, U, mode in (('a0', m.V, 'role'), ('a100', U1 if U1 is not None else m.V, 'role'), ('prov', m.V, 'prov')):
        np.savez_compressed(sc / f'{name}__{tag}.npz', gV=U.astype(np.float32), gF=m.F.astype(np.int32), gC=face_colours(m, mode).astype(np.float32),
                            bV=np.zeros((0, 3), np.float32), bF=np.zeros((0, 3), np.int32), bC=np.zeros((0, 3), np.float32))
    for gname, gm in extra_ghosts.items():
        np.savez_compressed(sc / f'{name}__{gname}.npz', gV=gm.V.astype(np.float32), gF=gm.F.astype(np.int32), gC=face_colours(gm, 'prov').astype(np.float32),
                            bV=np.zeros((0, 3), np.float32), bF=np.zeros((0, 3), np.int32), bC=np.zeros((0, 3), np.float32))
    idx_path = OUT / 'index.json'
    with index_lock():                                   # several batch workers may finish at once (process_all.py)
        idx = json.loads(idx_path.read_text()) if idx_path.is_file() else {'results': []}
        idx['results'] = [r for r in idx['results'] if r['name'] != name] + [record]
        idx['results'].sort(key=lambda r: (r['kind'], r['name']))
        tmp = idx_path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(idx, indent=1, default=float))
        os.replace(tmp, idx_path)


class index_lock:
    """Exclusive lock on out/index.json across processes (a lock file created with O_EXCL; a lock older than 120 s is taken over)."""
    def __enter__(self):
        self.p = OUT / 'index.json.lock'
        t0 = time.monotonic()
        while True:
            try:
                self.fd = os.open(self.p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.p.stat().st_mtime > 120:
                        self.p.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() - t0 > 300:
                    raise TimeoutError('index.json.lock held for 5 min')
                time.sleep(0.2)

    def __exit__(self, *a):
        os.close(self.fd)
        try:
            self.p.unlink()
        except FileNotFoundError:
            pass


def write_base(race, body):
    p = OUT / f'base_{race}.glb'
    if not p.is_file():
        OUT.mkdir(parents=True, exist_ok=True)
        p.write_bytes(glb_bytes(base_mesh(body)))
    return p.name


# ----------------------------------------------------------------------------------------------- drivers

def prepare(mod, body, rig, race):
    m, info = load_shell(mod, body, rig)
    tripo = m.copy()
    info['small_skin_relabelled'] = relabel_small_skin(m)
    info['snap'] = snap_skin(m, body, race)
    return m, tripo, info


def stats(m):
    a = m.tri_area()
    return {'faces': int(len(m.F)), 'vertices': int(len(m.V)), 'area_cm2_by_prov': {p: float(a[m.prov == p].sum() * 1e4) for p in np.unique(m.prov)}}


def run_outfit(mod, name=None):
    mod = Path(mod).resolve()
    s, j, race, module = module_identity(mod)
    name = name or f'outfit__{race}__{s.split("_outfit_")[-1][:28]}'
    t0 = time.monotonic()
    body = bf.get_body(race)
    rig = ab.Rig.from_npz(bf.BASE_CACHE / f'{race}_rig.npz')
    x0 = fl.mirror_plane(race, body)[0]
    m, tripo, info = prepare(mod, body, rig, race)
    notes = {}
    pieces = []
    cur = m
    for seam, kind in (('head', 'head'), ('hand.l', 'hand'), ('hand.r', 'hand')):
        if SEAMS[seam]['Q'] not in body.part_names or (body.part == body.part_names.index(SEAMS[seam]['Q'])).sum() < 4:
            notes[seam] = 'no such part on this base'
            continue
        try:
            pl, note = choose_plane(cur, body, seam, OFFSETS[kind])
            notes[seam] = note
            cur, add = replace_with_base(cur, body, seam, pl, notes)
            pieces += add
        except Exception as error:  # noqa: BLE001 - one seam failing keeps the generated part
            notes[seam] = {**notes.get(seam, {}), 'error': f'{type(error).__name__}: {error}'}
    if 'foot.l' in body.part_names and (body.part == body.part_names.index('foot.l')).sum() >= 4:
        fw, fwl = has_footwear(cur, body, 'r'), has_footwear(cur, body, 'l')
        notes['footwear_cm2'] = {'right': fw, 'left': fwl}
        if fw >= 20 and fwl >= 20:
            notes['feet'] = 'both feet already have footwear: kept as generated'
        elif fw < 20:
            for seam in ('foot.l', 'foot.r'):
                try:
                    pl, note = choose_plane(cur, body, seam, OFFSETS['foot'])
                    notes[seam] = note
                    cur, add = replace_with_base(cur, body, seam, pl, notes)
                    pieces += add
                except Exception as error:  # noqa: BLE001
                    notes[seam] = {**notes.get(seam, {}), 'error': f'{type(error).__name__}: {error}'}
        else:
            try:
                cur, add = mirror_left_foot(cur, body, x0, notes)
                pieces += add
            except Exception as error:  # noqa: BLE001
                notes['foot.l mirror'] = {**notes.get('foot.l mirror', {}), 'error': f'{type(error).__name__}: {error}'}
    out = compact(concat([cur] + pieces))
    margin = fl.DEFAULTS['margin_pct'] / 100 * body.H
    U1, finfo = fill(out, body, margin)
    rec = {'name': name, 'kind': 'outfit', 'race': race, 'module': mod.relative_to(ROOT).as_posix(), 'glb': f'{name}.glb', 'ghosts': {'tripo': f'{name}__tripo.glb', 'base': write_base(race, body)},
           'load': info, 'seams': notes, 'fill': finfo, 'stats': stats(out), 'seconds': round(time.monotonic() - t0, 1)}
    write_result(name, out, U1, {'tripo': tripo}, rec)
    log('done', name, f'{rec["seconds"]} s')
    return rec


def armour_modules(set_a, set_b, race):
    def find(s, module):
        if '/' in s:                                    # an exact module path (models/<set>/<job>/<race>/<module>)
            return ROOT / s if (ROOT / s / 'fit' / 'align.json').is_file() else None
        hits = sorted(ROOT.glob(f'models/{s}/*/{race}/{module}/reduced/lod_error_{LV}.glb'))
        hits = [h.parents[1] for h in hits if (h.parents[1] / 'fit' / 'align.json').is_file()]
        return hits[0] if hits else None
    return find(set_a, 'body_armor'), (find(set_b, 'right_gauntlet_boot') if set_b else None)


def run_armour(mod_a, mod_b=None, name=None):
    mod_a = Path(mod_a).resolve()
    s, j, race, module = module_identity(mod_a)
    sb_ = module_identity(mod_b)[0] if mod_b else None
    name = name or f'armour__{race}__{s.split("_")[0]}_{s.split("_")[1]}' + (f'__arms_{sb_.split("_")[0]}_{sb_.split("_")[1]}' if sb_ else '__base_arms')
    t0 = time.monotonic()
    body = bf.get_body(race)
    rig = ab.Rig.from_npz(bf.BASE_CACHE / f'{race}_rig.npz')
    x0 = fl.mirror_plane(race, body)[0]
    notes = {'body_armor': mod_a.relative_to(ROOT).as_posix(), 'right_gauntlet_boot': Path(mod_b).resolve().relative_to(ROOT).as_posix() if mod_b else None}
    A, tripoA, infoA = prepare(mod_a, body, rig, race)
    pieces = []
    pl, note = choose_plane(A, body, 'head', OFFSETS['head'])
    notes['head'] = note
    A, add = replace_with_base(A, body, 'head', pl, notes)
    pieces += add
    canon = {}
    B = tripoB = infoB = None
    if mod_b:
        B, tripoB, infoB = prepare(mod_b, body, rig, race)
    frac = {}
    for joint in ('elbow', 'knee'):                                  # canonical rings: one offset per joint for both sides, the first where A (both sides) and B (right) are bare
        tried = []
        for f in ARMOUR_OFFSETS:
            ok = True
            for side in ('l', 'r'):
                seam = f'{joint}.{side}'
                pl = plane_at(joint_plane(body, SEAMS[seam]['P'], SEAMS[seam]['Q']), f)
                a_ok, a_why = plane_bare(A, pl, body, SEAMS[seam]['chain'])
                b_ok, b_why = plane_bare(B, pl, body, SEAMS[seam]['chain']) if (B is not None and side == 'r') else (True, '')
                ok &= a_ok and b_ok
                tried.append(f'{seam} {f:+.2f}: A {a_why}' + (f'; B {b_why}' if b_why else ''))
            if ok:
                break
        frac[joint] = f if ok else 0.
        notes[f'{joint} ring'] = {'frac': frac[joint], 'all_bare': bool(ok), 'tried': tried}
    for seam in ('elbow.l', 'elbow.r', 'knee.l', 'knee.r'):
        jp = joint_plane(body, SEAMS[seam]['P'], SEAMS[seam]['Q'])
        pl = plane_at(jp, frac[seam.split('.')[0]])
        chain = SEAMS[seam]['chain']
        bare, why = plane_bare(A, pl, body, chain)
        notes[f'A {seam}'] = {'bare': bare, 'why': why}
        bc = base_cut(body, pl, chain)
        canon[seam] = (pl, bc, jp)
        try:
            A, _, info = seam_piece(A, pl, 'prox', body, chain, jp['Lp'], bc, seam)
            notes[f'A {seam}'].update(info)
        except RuntimeError as error:
            notes[f'A {seam}']['error'] = str(error)
    ghosts = {'tripo_armor': tripoA}
    if mod_b:
        ghosts['tripo_arms'] = tripoB
        for seam in ('elbow.r', 'knee.r'):
            pl, bc, jp = canon[seam]
            chain = SEAMS[seam]['chain']
            bare, why = plane_bare(B, pl, body, chain)
            try:
                piece, ids, info = seam_piece(B, pl, 'dist', body, chain, jp['Lq'], bc, seam)
            except RuntimeError as error:
                notes[f'B {seam}'] = {'bare': bare, 'why': why, 'error': str(error)}
                continue
            pieces.append(piece)
            other = seam.replace('.r', '.l')
            plo, bco, jpo = canon[other]
            notes[f'B {seam}'] = {'bare': bare, 'why': why, **info}
            # the left piece: the outfit mirrored first, then joined at the LEFT ring with the left leg's / arm's own game skin
            Bm = mirror_mesh(B, x0, body)
            try:
                mpiece, _, minfo = seam_piece(Bm, plo, 'dist', body, SEAMS[other]['chain'], jpo['Lq'], bco, other)
                pieces.append(mpiece)
                notes[f'B {other} (mirrored)'] = minfo
            except RuntimeError as error:
                notes[f'B {other} (mirrored)'] = {'error': str(error)}
    else:
        for seam in ('elbow.l', 'elbow.r', 'knee.l', 'knee.r'):
            pieces.append(canon[seam][1][1])
    out = compact(concat([A] + pieces))
    margin = fl.DEFAULTS['margin_pct'] / 100 * body.H
    U1, finfo = fill(out, body, margin)
    rec = {'name': name, 'kind': 'armour', 'race': race, 'module': notes['body_armor'], 'glb': f'{name}.glb',
           'ghosts': {**{k: f'{name}__{k}.glb' for k in ghosts}, 'base': write_base(race, body)}, 'load': {'body_armor': infoA, 'right_gauntlet_boot': infoB},
           'seams': notes, 'fill': finfo, 'stats': stats(out), 'seconds': round(time.monotonic() - t0, 1)}
    write_result(name, out, U1, ghosts, rec)
    log('done', name, f'{rec["seconds"]} s')
    return rec


def review_modules():
    outs = []
    for p in REVIEW['outfits']:
        q = ROOT / p
        if not (q / 'reduced').is_dir():
            hits = sorted(ROOT.glob(p + '*/*/dwarf_male/outfit'))
            if not hits:
                continue
            q = hits[0]
        outs.append(q)
    return outs


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('what', choices=('outfit', 'armour', 'batch'))
    ap.add_argument('modules', nargs='*')
    ap.add_argument('--name')
    a = ap.parse_args(argv)
    if a.what == 'outfit':
        for m in a.modules:
            run_outfit(m, a.name)
    elif a.what == 'armour':
        run_armour(a.modules[0], a.modules[1] if len(a.modules) > 1 else None, a.name)
    else:
        for m in review_modules():
            try:
                run_outfit(m)
            except Exception as error:  # noqa: BLE001
                log('FAILED', m, type(error).__name__, error)
        for sa, sb, race in REVIEW['armour']:
            ma, mb = armour_modules(sa, sb, race)
            if ma is None:
                log('no body_armor for', sa, race)
                continue
            try:
                run_armour(ma, mb)
            except Exception as error:  # noqa: BLE001
                log('FAILED', sa, sb, type(error).__name__, error)


if __name__ == '__main__':
    main()
