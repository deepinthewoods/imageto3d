"""Geometry helpers of the layer builder (from the shell / slot prototype geodiv.py, 2026-11-22; the prototype's combined-result method
(process_module / run_outfit / run_armour / variants / symmetry) was removed with the move to tools/layers/, see commit a626d23 / tag
pre-pipeline-v2 under experiments/shell_slots_20261122/geodiv.py).

What the layers use: the game body of a race (`game_body`, subdivided where its triangles are long), slot parts (`parts`, `lower_arm`, `lower_leg`,
`all_parts`, `game_faces`), the module loader re-exports from shell_slots (`load_shell`, `mirror_mesh`, ...), `relabel_off_body_skin`,
`inflate_to_body` (a layer generated far too slim), `centre_limb_pieces` (gauntlets / boots standing off the game limbs), `kept_garment` and
`fill_grouped` (the Amount morph: garment pushed out of the body, per limb group).
"""
import sys
from pathlib import Path

import numpy as np
from scipy import sparse
from scipy.sparse import csgraph

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from shell_slots import (SKIN, Mesh, ab, base_mesh, bf, components, compact, face_adjacency, load_npz,  # noqa: E402, F401  (several are re-exported: layers.py uses gd.load_shell, gd.ab, ...)
                         fill, fl, load_shell, log, mirror_mesh, module_identity, relabel_small_skin, split_edges,
                         vertex_part, ROOT)

_DENS = {'iso_in': None, 'anat': 1.0, 'ref': None}    # per-module values while a module is processed
CODE_MTIME = max((Path(__file__).resolve().parent / f).stat().st_mtime for f in ('geodiv.py', 'shell_slots.py'))
GAME_PART_INSIDE_MM = 2.0  # garment faces on a game-owned part whose centre is less than this outside the game body are dropped
GAME_PART_FLAT_MM = 15.0   # garment faces nearest a game hand / bare foot and within this of its surface are dropped (hands stay bare)
TUCK_DROP = True          # garment faces within TUCK_MM of the garment edge, more than TUCK_IN_MM inside the game body and within TUCK_SKIN_MM of the
TUCK_MM = 30.0            # generator's skin are dropped (the game skin covers them)
TUCK_IN_MM = 2.0
TUCK_SKIN_MM = 10.0
HIDE_UNDER_SKIN = True    # garment faces with the generator's own skin over them (within UNDER_SKIN_MM along their normal) are dropped
UNDER_SKIN_MM = 25.0
SPECK_CM2 = 20.0         # garment patches smaller than this lying on a game-owned part (head, hands, bare feet) are dropped


# ----------------------------------------------------------------------------------------------- slots

def parts(body, *names):
    return [body.part_names.index(n) for n in names if n in body.part_names and (body.part == body.part_names.index(n)).sum() > 0]


def lower_arm(body, s):
    return parts(body, f'forearm_stretch.{s}', f'hand.{s}')


def lower_leg(body, s):
    return parts(body, f'leg_stretch.{s}', f'foot.{s}')


def all_parts(body):
    return [i for i in range(len(body.part_names)) if (body.part == i).sum() > 0]


GAME_SUBDIV = 'auto'         # 'auto': a game body with long triangles (median edge > GAME_SUBDIV_FROM_MM: the gnome, 127 mm) is subdivided before it is
GAME_SUBDIV_FROM_MM = 100.0  # cut and joined, every edge longer than GAME_MAX_EDGE_MM split at its midpoint (flat: the surface does not change). The
GAME_MAX_EDGE_MM = 100.0     # cut / joins / lips then work at the density they work at on humans (74-100 mm). 'on': always. 'off': never.
_GAME_BODY = {}


def game_body(race):
    """The game body the fit is made on: bf.get_body(race), subdivided where GAME_SUBDIV says so (same surface, same parts)."""
    body = bf.get_body(race)
    E = np.unique(np.sort(np.concatenate([body.F[:, [0, 1]], body.F[:, [1, 2]], body.F[:, [2, 0]]]), axis=1), axis=0)
    med = float(np.median(np.linalg.norm(body.V[E[:, 0]] - body.V[E[:, 1]], axis=1))) * 1000
    if GAME_SUBDIV == 'off' or (GAME_SUBDIV == 'auto' and med <= GAME_SUBDIV_FROM_MM):
        return body
    key = (race, GAME_MAX_EDGE_MM)
    if key not in _GAME_BODY:
        V, F, part = body.V, body.F, body.part
        for _ in range(4):
            E = np.unique(np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1), axis=0)
            L = np.linalg.norm(V[E[:, 0]] - V[E[:, 1]], axis=1)
            mark = {tuple(map(int, e)) for e in E[L > GAME_MAX_EDGE_MM / 1000]}
            if not mark:
                break
            V, F, par = split_edges(V, F, mark)
            part = part[par]
        sub = bf.Body(V, F, part, body.part_names)
        sub.subdivided_from = (len(body.F), len(sub.F), round(med, 1))
        sub.orig = body                     # the same surface with its original triangles: used where only the surface matters (the fill morph)
        _GAME_BODY[key] = sub
        log('game body', race, f'subdivided {len(body.F)} -> {len(sub.F)} faces (median edge {med:.0f} mm, max {GAME_MAX_EDGE_MM:.0f} mm)')
    return _GAME_BODY[key]


def game_faces(body, slot):
    g = base_mesh(body)
    return g.subset(np.isin(g.part, slot))


# ----------------------------------------------------------------------------------------------- loading


# ----------------------------------------------------------------------------------------------- inflate (a module generated too slim for its game body)

INFLATE_GROUPS = {'torso': ('root.x', 'spine_01.x', 'spine_02.x', 'neck.x', 'skirt'),
                  'arm.l': ('arm_stretch.l', 'forearm_stretch.l'), 'arm.r': ('arm_stretch.r', 'forearm_stretch.r'),
                  'leg.l': ('thigh_stretch.l', 'leg_stretch.l'), 'leg.r': ('thigh_stretch.r', 'leg_stretch.r')}
INFLATE_THETA = 24          # angular bins around each axis
INFLATE_BIN_MM = 30.0       # bin length along the axis
INFLATE_MARGIN_MM = 5.0     # garment ends up at least this far outside the game body (skin: on it)
INFLATE_MIN_MM = 8.0        # deficits below this are left alone (ordinary fits) ...
INFLATE_FULL_MM = 20.0      # ... and taken in full from this
INFLATE_SPHERE_ASPECT = 1.4  # a torso less tall than this times its width is widened outward from its centre (ball), not from a vertical axis
INFLATE_ELEV = 12
INFLATE_TRIGGER_MM = 40.0   # run only when the deepest 5 % of the garment lies this far inside the game body (60 until 2026-11-23: the sahel farmer at 58 mm lost its V-neck; humans / dwarf are 22-28 mm)
INFLATE_PASSES = 4
INFLATE_SMOOTH_ITERS = 8    # displacement smoothing over the module mesh (keeps neighbouring groups together)


def ray_first_hit(O, D, TA, TB, TC):
    """Smallest positive t of the rays O + t D on the triangles (inf where none)."""
    e1, e2 = TB - TA, TC - TA
    best = np.full(len(O), np.inf)
    for k in range(0, len(O), 256):
        o, d = O[k:k + 256, None, :], D[k:k + 256, None, :]
        pv = np.cross(d, e2[None])
        det = (e1[None] * pv).sum(2)
        ok = np.abs(det) > 1e-12
        inv = np.where(ok, 1. / np.where(ok, det, 1.), 0.)
        tv = o - TA[None]
        u = (tv * pv).sum(2) * inv
        qv = np.cross(tv, e1[None])
        v = (d * qv).sum(2) * inv
        t = (e2[None] * qv).sum(2) * inv
        hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-6)
        best[k:k + 256] = np.where(hit, t, np.inf).min(1)
    return best


def _smooth_grid(G, valid, sig=1.2):
    """Normalised Gaussian smoothing of a (s, theta) grid, theta periodic; empty cells filled from their neighbours."""
    from scipy import ndimage
    W = valid.astype(float)
    Z = np.where(valid, G, 0.)
    a = ndimage.gaussian_filter(Z, sig, mode=('nearest', 'wrap'))
    b = ndimage.gaussian_filter(W, sig, mode=('nearest', 'wrap'))
    return np.where(b > 1e-6, a / np.maximum(b, 1e-6), 0.)


def inflate_to_body(m, body):
    """A module generated slimmer than its game body (a flat gnome torso) is widened, per body group, outward from the group's axis (vertical for the
    torso, the limb's own axis for arms / legs): per (position along the axis, angle) the deepest garment point must clear the game surface by
    INFLATE_MARGIN_MM, and everything at that angle and height moves out by that much, fading out over the garment that is already outside, so layers
    keep their order (a skirt stays under the bodice) and nothing moves up or down. The displacement is smoothed over the module's mesh."""
    pn = body.part_names
    vpart = body.measure(m.V)['part']
    D = np.zeros_like(m.V)
    garm_v = np.zeros(len(m.V), bool)
    garm_v[np.unique(m.F[m.role != SKIN])] = True
    used = np.zeros(len(m.V), bool)
    used[m.F] = True
    info = {}
    margin, lo, hi = INFLATE_MARGIN_MM / 1000, INFLATE_MIN_MM / 1000, INFLATE_FULL_MM / 1000
    for g, names in INFLATE_GROUPS.items():
        ids = [pn.index(n) for n in names if n in pn and (body.part == pn.index(n)).sum() > 0]
        if not ids:
            continue
        fm = np.isin(body.part, ids)
        GV = body.V[np.unique(body.F[fm])]
        c = GV.mean(0)
        sphere = g == 'torso' and np.ptp(GV[:, 2]) < INFLATE_SPHERE_ASPECT * max(np.ptp(GV[:, 0]), np.ptp(GV[:, 1]))
        sel = used & np.isin(vpart, ids)
        if not sel.any():
            continue
        X = m.V[sel]
        if sphere:                                   # a ball-shaped torso (gnome): outward from its centre, so shoulder tops are lifted too
            ns = INFLATE_ELEV
            el = -np.pi / 2 + (np.arange(ns) + .5) * np.pi / ns
            th = (np.arange(INFLATE_THETA) + .5) * 2 * np.pi / INFLATE_THETA
            dirs = np.stack([np.cos(el)[:, None] * np.cos(th)[None], np.cos(el)[:, None] * np.sin(th)[None], np.repeat(np.sin(el)[:, None], INFLATE_THETA, 1)], -1)
            Rg = ray_first_hit(np.repeat(c[None], ns * INFLATE_THETA, 0), dirs.reshape(-1, 3), body.A[fm], body.B[fm], body.C[fm]).reshape(ns, INFLATE_THETA)
            rel = X - c
            r = np.linalg.norm(rel, axis=1)
            elev = np.arcsin(np.clip(rel[:, 2] / np.maximum(r, 1e-9), -1., 1.))
            ks = np.clip(((elev + np.pi / 2) / np.pi * ns).astype(int), 0, ns - 1)
            ang = np.arctan2(rel[:, 1], rel[:, 0]) % (2 * np.pi)
            kt = np.clip((ang / (2 * np.pi) * INFLATE_THETA).astype(int), 0, INFLATE_THETA - 1)
        else:
            if g == 'torso':
                a = np.array([0., 0., 1.])
            else:
                w_, vec = np.linalg.eigh(np.cov((GV - c).T))
                a = vec[:, -1]
            e1 = np.cross(a, [0., 1., 0.]) if abs(a[1]) < .9 else np.cross(a, [1., 0., 0.])
            e1 /= np.linalg.norm(e1)
            e2 = np.cross(a, e1)
            sg = (GV - c) @ a
            s0, s1 = sg.min(), sg.max()
            ns = max(2, int(np.ceil((s1 - s0) / (INFLATE_BIN_MM / 1000))))
            sc = s0 + (np.arange(ns) + .5) * (s1 - s0) / ns
            th = (np.arange(INFLATE_THETA) + .5) * 2 * np.pi / INFLATE_THETA
            # axis centre per slice (torso: the slice's own centroid, so a belly in front is not off-centre)
            cen = np.repeat(c[None], ns, 0)
            if g == 'torso':
                for k in range(ns):
                    sl = np.abs(sg - sc[k]) <= (s1 - s0) / ns
                    if sl.sum() >= 3:
                        cen[k] = c + ((GV[sl] - c) - np.outer((GV[sl] - c) @ a, a)).mean(0) + (sc[k]) * a
                    else:
                        cen[k] = c + sc[k] * a
            else:
                cen = c[None] + sc[:, None] * a[None]
            dirs = np.cos(th)[:, None] * e1[None] + np.sin(th)[:, None] * e2[None]
            O = np.repeat(cen, INFLATE_THETA, 0)
            Dr = np.tile(dirs, (ns, 1))
            Rg = ray_first_hit(O, Dr, body.A[fm], body.B[fm], body.C[fm]).reshape(ns, INFLATE_THETA)
            sv = (X - c) @ a
            ks = np.clip(np.round((sv - s0) / (s1 - s0) * ns - .5).astype(int), 0, ns - 1)
            rel = X - cen[ks]
            rel -= np.outer(rel @ a, a)
            r = np.linalg.norm(rel, axis=1)
            ang = np.arctan2(rel @ e2, rel @ e1) % (2 * np.pi)
            kt = np.clip((ang / (2 * np.pi) * INFLATE_THETA).astype(int), 0, INFLATE_THETA - 1)
        Rv = Rg[ks, kt]
        need = np.where(garm_v[sel], Rv + margin - r, Rv - r)
        need[~np.isfinite(need)] = -np.inf
        grid = np.full((ns, INFLATE_THETA), -np.inf)
        np.maximum.at(grid, (ks, kt), need)
        valid = np.isfinite(grid)
        grid = np.where(valid, np.maximum(grid, 0.), 0.)
        grid = _smooth_grid(grid, valid)
        t_ = np.clip((grid - lo) / (hi - lo), 0., 1.)
        grid = grid * t_ * t_ * (3 - 2 * t_)
        dv = grid[ks, kt]
        if not (dv > 0).any():
            info[g] = {'max_mm': 0.}
            continue
        L = 2 * dv + .03
        R0 = Rv + margin
        fade = np.clip(1. - np.maximum(r - np.where(np.isfinite(R0), R0, r), 0.) / L, 0., 1.)
        u = rel / np.maximum(r, 1e-9)[:, None]
        D[np.flatnonzero(sel)] = u * (dv * fade)[:, None]
        info[g] = {'max_mm': round(float(dv.max() * 1000), 1), 'vertices_moved': int(((dv * fade) > 1e-3).sum()),
                   'median_moved_mm': round(float(np.median((dv * fade)[(dv * fade) > 1e-3]) * 1000), 1) if ((dv * fade) > 1e-3).any() else 0.}
    if not np.abs(D).any():
        return info
    # smooth over the mesh (Jacobi), so groups meet without a step
    e = np.unique(np.sort(np.concatenate([m.F[:, [0, 1]], m.F[:, [1, 2]], m.F[:, [2, 0]]]), axis=1), axis=0)
    A_ = sparse.coo_matrix((np.ones(2 * len(e)), (np.r_[e[:, 0], e[:, 1]], np.r_[e[:, 1], e[:, 0]])), shape=(len(m.V), len(m.V))).tocsr()
    deg = np.asarray(A_.sum(1)).ravel()
    for _ in range(INFLATE_SMOOTH_ITERS):
        nb = (A_ @ D) / np.maximum(deg, 1)[:, None]
        D = np.where((deg > 0)[:, None], .5 * D + .5 * nb, D)
    m.V = m.V + D
    info['moved_mm_max'] = round(float(np.linalg.norm(D, axis=1).max() * 1000), 1)
    return info


ROLE_ORDER = ('primary', 'secondary', 'tertiary', 'accent', 'joint')   # tie-break of the majority vote below (was: the set's string-hash order)
OFF_BODY_SKIN_MM = 15.0   # a generator 'skin' face standing this far off the game body with garment on at least 2 of its 3 sides is garment
                          # (a boot-top triangle labelled skin), and takes its neighbours' garment role


def pick_role(roles):
    """The majority garment role of `roles` (a list of role names). Ties go to the earlier role of ROLE_ORDER, then (custom roles, which have no
    canonical place) to the lexically smaller name: the result never depends on set order or PYTHONHASHSEED."""
    return sorted(set(roles), key=lambda r: (-roles.count(r), ROLE_ORDER.index(r) if r in ROLE_ORDER else len(ROLE_ORDER), r))[0]


def relabel_off_body_skin(m, body, iters=3):
    """Skin faces that are clearly not on a body (OFF_BODY_SKIN_MM off the game body) and mostly surrounded by garment are mislabelled garment."""
    pairs = face_adjacency(m.F, np.ones(len(m.F), bool))
    nb = [[] for _ in range(len(m.F))]
    for a_, b_ in pairs:
        nb[a_].append(b_)
        nb[b_].append(a_)
    d = body.measure(m.V[m.F].mean(1))['d']
    total = 0
    for _ in range(iters):
        changed = []
        for f in np.flatnonzero((m.role == SKIN) & (d > OFF_BODY_SKIN_MM / 1000)):
            roles = [str(m.role[x]) for x in nb[f] if m.role[x] != SKIN]
            if len(roles) >= 2:
                changed.append((f, pick_role(roles)))
        if not changed:
            break
        for f, r in changed:
            m.role[f] = r
        total += len(changed)
    return total


# ----------------------------------------------------------------------------------------------- (b) symmetry, (c) spikes


def _joint(body, va, vb):
    """Centre of the joint between two parts: their shared vertices, or where they are separate pieces (the halfling_male hand) the vertices of
    each that lie within 10 mm of the gap between them."""
    sh = sorted(va & vb)
    if sh:
        return body.V[sh].mean(0)
    from scipy.spatial import cKDTree
    A, B = np.array(sorted(va)), np.array(sorted(vb))
    da, _ = cKDTree(body.V[B]).query(body.V[A])
    db, _ = cKDTree(body.V[A]).query(body.V[B])
    g = min(da.min(), db.min())
    return np.concatenate([body.V[A[da <= g + 0.01]], body.V[B[db <= g + 0.01]]]).mean(0)


# ----------------------------------------------------------------------------------------------- centre gauntlets / boots on the game limbs

CENTRE_LIMBS = True
CENTRE_MAX_MM = 160.0      # a limb piece is moved at most this far across its limb (60 until 2026-11-24: a dwarf gauntlet generated 150 mm in front of the forearm stayed 90 mm off)
CENTRE_FALLOFF_MM = 60.0   # ... the module around it follows over this


def _section_centre(V, F, c, a, s):
    """Length-weighted centre of the section of (V, F) by the plane through c + s a with normal a (None when it misses)."""
    d = (V - c) @ a - s
    da = d[F]
    cut = ~((da > 0).all(1) | (da < 0).all(1))
    mids, lens = [], []
    for tri in F[cut]:
        pts = []
        for i in range(3):
            x, y = tri[i], tri[(i + 1) % 3]
            if (d[x] > 0) != (d[y] > 0):
                u = d[x] / (d[x] - d[y])
                pts.append(V[x] + u * (V[y] - V[x]))
        if len(pts) == 2:
            mids.append((pts[0] + pts[1]) / 2)
            lens.append(np.linalg.norm(pts[1] - pts[0]))
    if not lens or sum(lens) < 1e-3:
        return None
    return (np.array(mids) * np.array(lens)[:, None]).sum(0) / sum(lens)


def centre_limb_pieces(m, body, side='r'):
    """A gauntlet / boot module's generator puts the covered limb where it likes (no bare skin there to align with): each garment piece on the
    forearm / shin is moved across the limb so its cross-section centres (middle 60 % of the game limb) match the game limb's (offset fitted as
    o0 + o1 s along the limb, so a slight tilt is followed); the rest of the module follows over CENTRE_FALLOFF_MM. Returns notes."""
    pn = body.part_names
    garm = m.role != SKIN
    pairs = face_adjacency(m.F, garm)
    lab = components(len(m.F), pairs, garm)
    vpart = body.measure(m.V)['part']
    D = np.zeros_like(m.V)
    moved_any = np.zeros(len(m.V), bool)
    notes = {}
    for limb, main, extra in (('forearm', f'forearm_stretch.{side}', f'hand.{side}'), ('shin', f'leg_stretch.{side}', f'foot.{side}')):
        if main not in pn:
            continue
        ids = [pn.index(main)] + ([pn.index(extra)] if extra in pn else [])
        fm = body.part == pn.index(main)
        if fm.sum() < 4:
            continue
        GV = body.V[np.unique(body.F[fm])]
        c = GV.mean(0)
        # the limb axis runs joint to joint (elbow -> wrist, knee -> ankle). The main direction of the part's vertices was used before,
        # but a stubby part is wider than long (dwarf shins, gnome forearms: 66-87 degrees off), and centring "across" such an axis moved
        # every dwarf boot ~60 mm UP the leg (user, 2026-11-24: "the boots are way higher ... feet sticking out the bottom")
        up = {'forearm': f'arm_stretch.{side}', 'shin': f'thigh_stretch.{side}'}[limb]
        vm = set(body.F[fm].ravel())
        a, axis_from = None, 'joints'
        if up in pn and extra in pn and (body.part == pn.index(up)).any() and (body.part == pn.index(extra)).any():
            J1 = _joint(body, vm, set(body.F[body.part == pn.index(up)].ravel()))
            J2 = _joint(body, vm, set(body.F[body.part == pn.index(extra)].ravel()))
            if np.linalg.norm(J2 - J1) > 1e-3:
                a = (J2 - J1) / np.linalg.norm(J2 - J1)
        if a is None:
            w_, vec = np.linalg.eigh(np.cov((GV - c).T))
            a, axis_from = vec[:, -1], 'principal axis (no joints)'
        sg = (GV - c) @ a
        s0, s1 = sg.min(), sg.max()
        comps = [k for k in np.unique(lab[lab >= 0]) if np.isin(vpart[np.unique(m.F[lab == k])], ids).mean() > .5]
        if not comps:
            continue
        pf = np.isin(lab, comps)
        sl = np.linspace(s0 + .2 * (s1 - s0), s1 - .2 * (s1 - s0), 7)
        offs, ss_ = [], []
        for s_ in sl:
            gc = _section_centre(body.V, body.F[fm], c, a, s_)
            mc = _section_centre(m.V, m.F[pf], c, a, s_)
            if gc is None or mc is None:
                continue
            o = gc - mc
            offs.append(o - (o @ a) * a)
            ss_.append(s_)
        if len(offs) < 3:
            notes[limb] = 'too few sections'
            continue
        offs, ss_ = np.array(offs), np.array(ss_)
        A_ = np.c_[np.ones(len(ss_)), ss_]
        coef, *_ = np.linalg.lstsq(A_, offs, rcond=None)
        verts = np.unique(m.F[pf])
        sv = np.clip((m.V[verts] - c) @ a, ss_.min(), ss_.max())
        o = coef[0][None] + sv[:, None] * coef[1][None]
        o -= np.outer(o @ a, a)
        n_ = np.linalg.norm(o, axis=1, keepdims=True)
        o = np.where(n_ > (CENTRE_MAX_MM / 1000 * _DENS['anat']), o * ((CENTRE_MAX_MM / 1000 * _DENS['anat'])) / np.maximum(n_, 1e-12), o)
        D[verts] = o
        moved_any[verts] = True
        notes[limb] = {'pieces': len(comps), 'axis': axis_from, 'offset_mm': [round(float(x), 1) for x in np.median(offs, 0) * 1000],
                       'moved_mm_max': round(float(np.linalg.norm(o, axis=1).max() * 1000), 1)}
    if moved_any.any():
        e = np.unique(np.sort(np.concatenate([m.F[:, [0, 1]], m.F[:, [1, 2]], m.F[:, [2, 0]]]), axis=1), axis=0)
        w = np.linalg.norm(m.V[e[:, 0]] - m.V[e[:, 1]], axis=1) + 1e-9
        G = sparse.coo_matrix((np.r_[w, w], (np.r_[e[:, 0], e[:, 1]], np.r_[e[:, 1], e[:, 0]])), shape=(len(m.V), len(m.V))).tocsr()
        src_v = np.flatnonzero(moved_any)
        dist, _, src = csgraph.dijkstra(G, directed=False, indices=src_v, min_only=True, return_predecessors=True)
        t = np.clip(np.where(np.isfinite(dist), dist, np.inf) / ((CENTRE_FALLOFF_MM / 1000 * _DENS['anat'])), 0., 1.)
        fall = 1. - t * t * (3. - 2. * t)
        ok = ~moved_any & (src >= 0) & (fall > 0)
        D[ok] = fall[ok, None] * D[src[ok]]
        m.V = m.V + D
    return notes


# ----------------------------------------------------------------------------------------------- one module in its slot

COVER_IN_MM = 40.0       # a game face is covered when the line along its normal, from this far inside ...
COVER_OUT_MM = 150.0     # ... to this far outside, meets the module's garment


def covered_count(Gm, garment_V, garment_F, cover_in=None, cover_out=None):
    """Per game face: how many of 4 samples (centroid and three inner points) have the module's garment on the line along the face normal
    (COVER_IN_MM inside to COVER_OUT_MM outside). A garment the generator put inside the game body still covers."""
    A0, B0, C0 = Gm.V[Gm.F[:, 0]], Gm.V[Gm.F[:, 1]], Gm.V[Gm.F[:, 2]]
    c = (A0 + B0 + C0) / 3
    n = np.cross(B0 - A0, C0 - A0)
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    P = np.concatenate([c, (2 * c + A0) / 3, (2 * c + B0) / 3, (2 * c + C0) / 3])
    N = np.concatenate([n] * 4)
    ci = COVER_IN_MM if cover_in is None else cover_in
    co = COVER_OUT_MM if cover_out is None else cover_out
    O = P - N * (ci / 1000)
    D = N * ((ci + co) / 1000)
    hit = np.zeros(len(P), bool)
    if not len(garment_F):
        return np.zeros(len(Gm.F), int)
    TA, TB, TC = garment_V[garment_F[:, 0]], garment_V[garment_F[:, 1]], garment_V[garment_F[:, 2]]
    e1, e2 = TB - TA, TC - TA
    for k in range(0, len(P), 256):
        o, d = O[k:k + 256, None, :], D[k:k + 256, None, :]
        pv = np.cross(d, e2[None])
        det = (e1[None] * pv).sum(2)
        ok = np.abs(det) > 1e-12
        inv = np.where(ok, 1. / np.where(ok, det, 1.), 0.)
        tv = o - TA[None]
        u = (tv * pv).sum(2) * inv
        qv = np.cross(tv, e1[None])
        v = (d * qv).sum(2) * inv
        t = (e2[None] * qv).sum(2) * inv
        hit[k:k + 256] = (ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t >= 0) & (t <= 1)).any(1)
    return hit.reshape(4, -1).sum(0)


def kept_garment(m, slot, game_only=None, body=None):
    """Garment faces kept for the module: whole connected garment pieces whose area lies mostly in the slot."""
    garm = m.role != SKIN
    pairs = face_adjacency(m.F, garm)
    lab = components(len(m.F), pairs, garm)
    area = m.tri_area()
    keep = np.zeros(len(m.F), bool)
    dropped = 0.
    for c in np.unique(lab[lab >= 0]):
        f = lab == c
        if area[f & np.isin(m.part, slot)].sum() >= .5 * area[f].sum():
            keep |= f
        else:
            dropped += area[f].sum()
    # specks of garment on a game-owned part (the generator's sleeve colour painted onto a hand) go too
    if game_only is not None and len(game_only):
        on_game = keep & np.isin(m.part, game_only)
        pairs = face_adjacency(m.F, on_game)
        lab2 = components(len(m.F), pairs, on_game)
        for c in np.unique(lab2[lab2 >= 0]):
            f = lab2 == c
            if area[f].sum() * 1e4 < SPECK_CM2:
                keep &= ~f
                dropped += area[f].sum()
        # garment faces on a game-owned part that lie inside the game body (the generator's cuff painted onto a smaller hand) cannot show: dropped
        if body is not None:                               # garment lying flat on a game hand / bare foot (sleeve colour painted onto the hand)
            limbs_only = [g for g in game_only if not body.part_names[g].startswith('head')]
            if limbs_only:
                ms_ = body.measure(m.V[m.F].mean(1))
                flat = keep & np.isin(ms_['part'], limbs_only) & (np.abs(ms_['d']) < GAME_PART_FLAT_MM / 1000)
                keep &= ~flat
                dropped += area[flat].sum()
        if HIDE_UNDER_SKIN and body is not None:
            sk = m.role == SKIN
            kf = np.flatnonzero(keep)
            if len(kf) and sk.any():
                subk = Mesh(m.V, m.F[kf], m.role[kf], m.part[kf], m.prov[kf])
                under = covered_count(subk, m.V, m.F[sk], cover_in=0., cover_out=UNDER_SKIN_MM) >= 3
                keep[kf[under]] = False
                dropped += area[kf[under]].sum()
                m.under_skin = int(under.sum())
        if TUCK_DROP and body is not None and keep.any() and (m.role == SKIN).any():
            # the tuck zone: garment faces near the garment edge, under the game skin, that the generator's own skin lay right on (the bodice edge
            # tucked under the shoulder skin): on the game body they would show as slivers inside the skin, so the game skin covers them instead
            sk = m.role == SKIN
            gvx = np.zeros(len(m.V), bool); gvx[np.unique(m.F[keep])] = True
            svx = np.zeros(len(m.V), bool); svx[np.unique(m.F[~keep])] = True
            rim = np.flatnonzero(gvx & svx)
            if len(rim):
                ge = np.unique(np.sort(np.concatenate([m.F[keep][:, [0, 1]], m.F[keep][:, [1, 2]], m.F[keep][:, [2, 0]]]), axis=1), axis=0)
                w = np.linalg.norm(m.V[ge[:, 0]] - m.V[ge[:, 1]], axis=1) + 1e-9
                G = sparse.coo_matrix((np.r_[w, w], (np.r_[ge[:, 0], ge[:, 1]], np.r_[ge[:, 1], ge[:, 0]])), shape=(len(m.V), len(m.V))).tocsr()
                dist = csgraph.dijkstra(G, directed=False, indices=rim, min_only=True)
                kf = np.flatnonzero(keep & (dist[m.F] <= TUCK_MM / 1000).all(1))
                if len(kf):
                    cen = m.V[m.F[kf]].mean(1)
                    inside = body.measure(cen)['d'] < -TUCK_IN_MM / 1000
                    skb = bf.Body(m.V, m.F[sk], np.flatnonzero(sk), [])
                    dsk = skb.closest(cen)[0]
                    tuck = inside & (dsk < TUCK_SKIN_MM / 1000)
                    keep[kf[tuck]] = False
                    dropped += area[kf[tuck]].sum()
                    m.tucked = int(tuck.sum())
        on_game = keep & np.isin(m.part, game_only)
        if on_game.any() and body is not None:
            dd = body.measure(m.V[m.F[on_game]].mean(1))['d']
            inside = np.flatnonzero(on_game)[dd < GAME_PART_INSIDE_MM / 1000]
            keep[inside] = False
            dropped += area[inside].sum()
    return keep, float(dropped * 1e4)


LIMB_GROUPS = {'arm.l': ('arm_stretch.l', 'forearm_stretch.l', 'hand.l'), 'arm.r': ('arm_stretch.r', 'forearm_stretch.r', 'hand.r')}


def keep_winding(sub, F):
    """bf.Body orients a mesh by its signed volume about the origin, which means nothing for an OPEN piece (an arm without the torso: the sign
    depends on where the arm is, so a whole group could come out inside-out). Give the piece back the winding its faces have on the closed body."""
    F = np.asarray(F, np.int64)
    if len(sub.F) != len(F) or np.array_equal(sub.F, F) or not np.array_equal(sub.F, F[:, [0, 2, 1]]):
        return sub
    sub.F = F.copy()
    sub.B, sub.C = sub.C, sub.B
    sub.tri_n = -sub.tri_n
    sub.vn = -sub.vn
    return sub


def fill_grouped(out, body, margin):
    """The Amount slider's fill, per limb group: a sleeve is pushed out of its own arm only, the rest out of the body without the arms (so arms that
    hang close to the torso do not push sleeves sideways off it). Returns (U1, info)."""
    vp = vertex_part(out, np.flatnonzero((out.role != SKIN) & (out.prov != 'patch')))
    pn = body.part_names
    groups = {}
    used = np.zeros(len(pn), bool)
    for g, names in LIMB_GROUPS.items():
        ids = [pn.index(n) for n in names if n in pn]
        if ids:
            groups[g] = ids
            used[ids] = True
    groups['core'] = list(np.flatnonzero(~used))
    U1 = out.V.copy()
    info = {}
    for g, ids in groups.items():
        sel = np.isin(vp, ids)
        if not sel.any():
            continue
        fm = np.isin(body.part, ids)
        if fm.sum() < 4:
            continue
        sub = keep_winding(bf.Body(body.V, body.F[fm], body.part[fm], body.part_names), body.F[fm])
        sub.H, sub.o = body.H, body.o
        keep = out.copy()
        # free in this group's pass: garment faces with ANY vertex of the group (a shoulder face with corners in the arm and the core is free in
        # both passes; each vertex takes the move of its own group). All-vertices made it pinned skin in both: never pushed out
        mask_f = np.isin(out.F, np.flatnonzero(sel)).any(1)
        keep.role = np.where(mask_f | (out.role == SKIN), out.role, SKIN)
        try:
            Ug, gi = fill(keep, sub, margin)
        except Exception as error:  # noqa: BLE001
            info[g] = f'{type(error).__name__}: {error}'
            continue
        U1[sel] = Ug[sel]
        info[g] = {k: gi.get(k) for k in ('vertices_moved', 'moved_mm_max')}
    return U1, info
