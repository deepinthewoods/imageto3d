"""Geometry division of the shell / slot prototype (2026-11-22): no planes.

    python experiments/shell_slots_20261122/geodiv.py batch          (the REVIEW set of shell_slots.py; results replace the plane-based ones)
    python experiments/shell_slots_20261122/geodiv.py outfit <module dir>
    python experiments/shell_slots_20261122/geodiv.py armour <body_armor dir> [<right_gauntlet_boot dir>]

Every module covers a SLOT: a set of the game body's own parts (outfit: all but head, hands and bare feet; body armour: all but head, forearms + hands,
shins + feet; gauntlet / boots: one forearm + hand and the same side's shin + foot). Slots nobody covers are the game body's own faces of those parts.
  * Skin: a module's own skin is dropped; the game faces of its slot that lay under that skin (closest module face is skin) replace it. Two slots meet
    on the game body's own part loops, i.e. the same base vertices, so a seam between game skin regions is the base's own connectivity.
  * Garment: never cut. A garment piece stays whole with its module (pieces whose area lies mostly outside the slot are dropped as noise).
  * Joins: where a garment edge lies on its own slot and close to the body, the game region's edge vertices are moved onto it (onto its own vertices
    when one is within SNAP_MM) and joined. A garment edge that reaches into another slot (a collar over the jaw, a gauntlet flare over the elbow)
    or stands off the body is an OVERHANG: it is pushed clear of the game skin and gets a short lip turned in under the skin; the neighbouring
    slot's skin continues underneath.
Garment edges stay where the generator put them, except that edges within ATTACH_MM of the body are snapped onto it first (the garment follows).
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np
from scipy import sparse
from scipy.sparse import csgraph

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from shell_slots import (SKIN, Mesh, ab, armour_modules, base_mesh, bf, boundary_loops, components, compact, concat, face_adjacency,  # noqa: E402
                         fill, fill_holes, fl, has_footwear, label_under, load_shell, log, mirror_mesh, module_identity, outfit_closest,
                         part_bodies, pav, relabel_small_skin, relax_folds, review_modules, seg_closest, splice_loops, split_edges, stats,
                         vertex_part, write_base, write_result, REVIEW, ROOT, RAMP_PCT)

# ---- options the viewer switches (variants built on request by serve.py; all off = the plain result listed in out/index.json)
FOLD_SAFE_JOINS = False     # (a) where a join folds game skin, it is undone there: that stretch of garment edge is tucked under the skin instead
FOLD_SAFE_PASSES = 3
SYMMETRY = 'off'            # (b) 'sleeve_shift' | 'sleeve_average' | 'sleeve_copy' | 'whole_mirror' (see symmetrise)
SYM_SHOULDER_MM = 40.0      # sleeve methods act from this far out along the game arm (copy: the cut plane) ...
SYM_RAMP_MM = 60.0          # ... reaching full effect this much further out (shift / average)
SYM_STEP_MM = 15.0          # cross-section spacing along the arm
SYM_BLEND_MM = 60.0         # sleeve_copy: the copied sleeve's edge is moved onto the torso's, fading over this
SYM_PAIR_MM = 80.0          # sleeve_copy: a copied edge loop is stitched to the torso loop it lies closest to, if within this (mean)
SPIKE_CLEAN = False         # (c) generator spikes: a vertex standing out of its neighbours' plane on sliver triangles is flattened into that
SPIKE_LEN_MM = 60.0         #     plane; dangling sliver triangles are dropped. Sliver: longest edge > SPIKE_LEN_MM and height < SPIKE_H_MM
SPIKE_H_MM = 6.0
SPIKE_OUT_MM = 10.0         #     ... and standing out more than this
EDGE_REFINE = False         # (d) a game triangle whose corners all lie on one side of the cut but which has sample points inside it on the other
EDGE_REFINE_ITERS = 4       #     side (a narrow skin strip / V-neck tip / strap passing through it) gets its edges longer than EDGE_REFINE_MIN_MM
EDGE_REFINE_MIN_MM = 15.0   #     split; repeated, so the strip is cut in instead of lost inside one big game triangle
DENSITY_AWARE = False       # (e) thresholds follow the meshes instead of fixed mm: join move / snap limits from the local triangle size, the cut's
                            #     inward search from the module's own skin depth, sliver test by shape (aspect) not mm, refinement sizes from the
                            #     module's edge length, clearances at least the facet error (sagitta) of the garment, anatomical distances x H / 1.8 m
DENS_PARTS = {'move', 'iso', 'sliver', 'refine', 'anat'}   # which density-aware rules (e) switches on. Tested 2026-11-23 (red / see-through px over 26
                            # views): gnome leather set 27085 -> 13032, sahel 6821 -> 5105, human breastplate same, human female 1034 -> 1375.
                            # Left out: 'snap' (snap limit from the garment edge length: human female +1443, gnome nothing) and 'clear'
                            # (clearance from the facet error: gnome -2197 but human female +691).
DENS_MOVE_MODE = 'up'       # the move limit may only grow past MOVE_MAX_MM ('both': gnome better, human female +1307 where it shrank; 'down')
DENS_MOVE_K = 0.6           # a game edge vertex moves at most this x its mean edge length onto a garment edge (clamped 20..90 mm)
DENS_SNAP_K = 0.5           # ... onto the garment's own vertex when within this x that vertex's mean edge length (10..60 mm)
DENS_SLIVER_ASPECT = 0.08   # sliver: height / longest edge below this, longest edge above half the mesh's median edge
DENS_REFINE_K = 0.4         # (d) refinement down to this x the module's median edge (10..40 mm); sign refinement 1.5x that
DENS_CLEAR_MAX_MM = 20.0    # clearance from the facet error at most this
_DENS = {'iso_in': None, 'anat': 1.0, 'ref': None}    # per-module values while a module is processed
CODE_MTIME = max((Path(__file__).resolve().parent / f).stat().st_mtime for f in ('geodiv.py', 'shell_slots.py'))
AUTO = True                 # options chosen per model from measurements made in this run (2026-11-23, user: "ideally we'd automatically figure out
                            # the best configuration per model and only process that"); an explicit variant key turns it off. See auto_symmetry.
AUTO_SYM_EXCESS = 0.25      # an arm whose cross-sections sit this much further off the game arm than the other arm's (median, in game-arm radii) is
                            # malformed: the module is made symmetric. Measured 2026-11-23: human modules <= 0.07, gnome median 0.02, the five
                            # lopsided gnome modules (ceremony dress, king's guard, cotehardie, tongan dress, hand-me-down cuirass) 0.35-0.83
AUTO_SYM_METHOD = 'whole_mirror'    # (kept for reference; auto_symmetry decides between whole_mirror and sleeve_shift)
AUTO_DESIGN_ASYM = 0.10     # a design whose front / back view differs from its mirror image on more than this fraction of garment pixels is
                            # asymmetric on purpose (design_symmetry.py; most designs < 0.04, the ceremony dress wrap 0.44)
VARIANT = None              # set by `geodiv.py variant`: results go to out/variants/<name>__<key>.* (index.json untouched)
ATTACH_IN_MM = 35.0      # ... and garment edges inside the game body within this (the human female's bodice corners beside the neck, 26 mm in)
ATTACH_MM = 20.0         # garment edges within this of the game body are snapped onto it (the garment follows over RAMP_PCT % of the height)
REFINE_EDGE_MM = 25.0    # game triangles straddling a skin / garment boundary are split until their edges are shorter than this
REFINE_ITERS = 0
SIGN_REFINE_ITERS = 3      # split a game edge whose midpoint has the other cut sign than both ends (a skin strip / strap between two vertices)
SIGN_REFINE_MIN_MM = 30.0  # ... only edges longer than this
SIGN_REFINE_EPS_MM = 3.0   # ... and only when the midpoint is clearly on the other side
MOVE_MAX_MM = 45.0       # a game edge vertex moves at most this far onto a garment edge
SNAP_MM = 30.0           # ... onto the garment edge's own vertex when one is this close to its place on the edge
SINK_MAX_MM = 30.0       # a garment edge inside the game body by up to this is still joined (the game skin dips to meet it)
LIFT_MAX_MM = 12.0       # a garment edge further off the body than this is not joined (overhang)
CLEAR_MM = 4.0           # overhanging garment is pushed at least this far off the game body ...
CLEAR_RAMP_MM = 40.0     # ... near its free edge (falloff along the garment)
LIP_DEPTH_MM = 3.0       # the lip of a free garment edge ends this far under the game skin ...
LIP_CLOSE_MM = 50.0       # garment edges up to this far off the body get a lip down to under the skin (closes the ring between a boot top and the
                          # leg); further off (a wide flare) they stay open as generated
LIP_MAX_MM = 25.0        # ... or this long, whichever is shorter
GAME_PART_INSIDE_MM = 2.0  # garment faces on a game-owned part whose centre is less than this outside the game body are dropped
GAME_PART_FLAT_MM = 15.0   # garment faces nearest a game hand / bare foot and within this of its surface are dropped (hands stay bare)
TUCK_DROP = True          # garment faces within TUCK_MM of the garment edge, more than TUCK_IN_MM inside the game body and within TUCK_SKIN_MM of the
TUCK_MM = 30.0            # generator's skin are dropped (the game skin covers them)
TUCK_IN_MM = 2.0
TUCK_SKIN_MM = 10.0
HIDE_UNDER_SKIN = True    # garment faces with the generator's own skin over them (within UNDER_SKIN_MM along their normal) are dropped
UNDER_SKIN_MM = 25.0
SPECK_CM2 = 20.0         # garment patches smaller than this lying on a game-owned part (head, hands, bare feet) are dropped
MIN_GAME_CM2 = 2.0       # game skin islands smaller than this are dropped
USE_JOINS = True         # False: every garment edge is layered (game skin runs on under it, the edge is pushed clear and tucked in with a lip);
                         # True: edges on the module's own slot are joined by moving the game skin's edge vertices onto them (fragile)
EXT_NEAR_MM = 0.0        # ... and always this far (0: only where the garment is clear above it)
OPEN_CLEAR = True         # near openings: skin runs on under any garment that covers it; the garment is pushed clear of it (face samples)
OPEN_PUSH_MAX_MM = 30.0
SINK_FOLD_GUARD = True     # a pulled-back vertex that turns one of its kept game faces over (> FOLD_DEG from its normal before) goes back
FOLD_DEG = 75.0
SINK_FROM_MM = 30.0       # hidden game skin vertices poking through a garment facet are pulled back in (ray from this far inside)
DEBUG_EXT = None
EXT_CLEAR_MM = 12.0       # ... and only where the garment is at least this far above it (under a tight gauntlet plate it would poke through)
EXT_FACE_MM = 400.0      # ... only game faces with edges up to this long
EXT_MM = 70.0            # layered: the game skin runs on this far under a garment edge


# ----------------------------------------------------------------------------------------------- slots

def parts(body, *names):
    return [body.part_names.index(n) for n in names if n in body.part_names and (body.part == body.part_names.index(n)).sum() > 0]


def lower_arm(body, s):
    return parts(body, f'forearm_stretch.{s}', f'hand.{s}')


def lower_leg(body, s):
    return parts(body, f'leg_stretch.{s}', f'foot.{s}')


def all_parts(body):
    return [i for i in range(len(body.part_names)) if (body.part == i).sum() > 0]


SEE_THROUGH_SETS = ('chain', 'scale')  # sets whose garment has holes all over (chain mail rings): the body under them is kept (imprint mode)


def see_through(mod):
    return any(k in Path(mod).resolve().parts[-4] for k in SEE_THROUGH_SETS)


IMPRINT = True               # joins by imprinting (imprint.py, 2026-11-24): garment edge loops near the body are joined to their foot curves cut into
                             # one shared copy of the game body (watertight), instead of process_module's joins / lips / skin run-on
FIX_INVERTED = True          # finished mesh: faces seen (almost) only from behind whose normal points into the body are flipped (seal.fix_inverted;
                             # 2026-11-23: dwarf pleated skirt red 51k -> 11k, human_female breastplate 36k -> 10k, clean results untouched)
MERGE_FLAT_SKIN = True       # finished mesh: game skin vertices whose faces are all flat around them are collapsed (seal.merge_flat; lossless)
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

def snap_rims(m, body, race):
    """Garment edge vertices (shared with skin) within ATTACH_MM of the game body go onto it (closest point on their own part + touching parts); the
    garment follows with a smooth falloff over RAMP_PCT % of the height. Edges further off (a loose flare) stay where the generator put them."""
    skin_f = m.role == SKIN
    sv, gv = np.unique(m.F[skin_f]), np.unique(m.F[~skin_f])
    rims = np.intersect1d(sv, gv)
    if not len(rims):
        return {'rims': 0}
    vp = vertex_part(m, np.flatnonzero(~skin_f))
    subs = part_bodies(body, race)
    D = np.zeros_like(m.V)
    for p in np.unique(vp[rims]):
        idx = rims[vp[rims] == p]
        sb = subs[int(p)] if p >= 0 else body
        d, tri, bary = sb.closest(m.V[idx])
        tgt = bary[:, :1] * sb.A[tri] + bary[:, 1:2] * sb.B[tri] + bary[:, 2:] * sb.C[tri]
        inside = body.measure(m.V[idx])['d'] < 0                 # an edge buried in the game body (generated body slimmer there) comes out from further
        near = d <= np.where(inside, ATTACH_IN_MM, ATTACH_MM) / 1000
        D[idx[near]] = (tgt - m.V[idx])[near]
    moved = np.linalg.norm(D[rims], axis=1) > 0
    gonly = np.zeros(len(m.V), bool)
    gonly[gv] = True
    gonly[rims] = False
    ramp = RAMP_PCT / 100 * body.H
    e = np.unique(np.sort(np.concatenate([m.F[~skin_f][:, [0, 1]], m.F[~skin_f][:, [1, 2]], m.F[~skin_f][:, [2, 0]]]), axis=1), axis=0)
    if len(e) and gonly.any():
        w = np.linalg.norm(m.V[e[:, 0]] - m.V[e[:, 1]], axis=1) + 1e-9
        G = sparse.coo_matrix((np.r_[w, w], (np.r_[e[:, 0], e[:, 1]], np.r_[e[:, 1], e[:, 0]])), shape=(len(m.V), len(m.V))).tocsr()
        dist, _, src = csgraph.dijkstra(G, directed=False, indices=rims, min_only=True, return_predecessors=True)
        t = np.clip(np.where(np.isfinite(dist), dist, np.inf) / ramp, 0., 1.)
        fall = 1. - t * t * (3. - 2. * t)
        ok = gonly & (src >= 0) & (fall > 0)
        D[ok] = fall[ok, None] * D[src[ok]]
    m.V = m.V + D
    mm = np.linalg.norm(D[rims], axis=1) * 1000
    return {'rims': int(len(rims)), 'snapped': int(moved.sum()), 'left_off_body': int((~moved).sum()),
            'snap_mm_p95': float(np.percentile(mm[moved], 95)) if moved.any() else 0.}


# ----------------------------------------------------------------------------------------------- inflate (a module generated too slim for its game body)

INFLATE = True
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
INFL_NEAR_MM = 40.0         # Inflation slider (morph target 'deflate'): a result vertex this close to the inflated module (garment or its own skin) takes
INFL_FAR_MM = 90.0          # its inflation offset in full, fading to none at this distance. Since 2026-11-23 the module's skin is part of the field, so the
INFL_GAME_FADE_MM = 60.0    # game body under the garment deflates with it (user: the slider left the body behind -> red); the game's own pieces (head,
                            # hands) stay, the deflation fading in over INFL_GAME_FADE_MM from them
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


OFF_BODY_SKIN_MM = 15.0   # a generator 'skin' face standing this far off the game body with garment on at least 2 of its 3 sides is garment
                          # (a boot-top triangle labelled skin), and takes its neighbours' garment role


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
                changed.append((f, max(set(roles), key=roles.count)))
        if not changed:
            break
        for f, r in changed:
            m.role[f] = r
        total += len(changed)
    return total


def prepare_geo(mod, body, rig, race):
    m, info = load_shell(mod, bf.get_body(race), rig)            # the alignment field is defined on the original game body
    _DENS['anat'] = float(body.H / 1.8) if DENSITY_AWARE and 'anat' in DENS_PARTS else 1.0
    sym = SYMMETRY
    if AUTO:
        sym, info['auto_symmetry'] = auto_symmetry(m, body, mod)
    if sym != 'off' and symmetric_kind(mod):
        info['symmetry'] = symmetrise(m, body, float(fl.mirror_plane(race, body)[0]), sym)
    if SPIKE_CLEAN:
        info['spikes'] = clean_spikes(m)
    tripo = m.copy()
    if INFLATE:
        G = np.unique(m.F[m.role != SKIN])
        deep = float(np.percentile(body.measure(m.V[G])['d'], 5)) * 1000 if len(G) else 0.
        info['inflate'] = {'garment_p5_depth_mm': round(deep, 1)}
        if deep < -INFLATE_TRIGGER_MM:                            # only a module generated clearly too slim (the gnome); ordinary fits stay as generated
            info['inflate']['passes'] = [inflate_to_body(m, body) for _ in range(INFLATE_PASSES)]
            m.infl = (m.V.copy(), m.F.copy(), (m.V - tripo.V).copy())   # inflated module (garment AND its skin) + per-vertex offset, for the slider
    info['small_skin_relabelled'] = relabel_small_skin(m)
    info['off_body_skin_relabelled'] = relabel_off_body_skin(m, body)
    info['rim_snap'] = snap_rims(m, body, race)
    return m, tripo, info


# ----------------------------------------------------------------------------------------------- (b) symmetry, (c) spikes

def symmetric_kind(mod):
    """Symmetry applies to modules generated with both arms (outfits, body armour); gauntlets / boots are mirrored anyway."""
    return Path(mod).name in ('outfit', 'body_armor')


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


def arm_frame(body, side):
    """(shoulder point, unit axis shoulder -> wrist, reach to the fingertips, arm part ids) of the game arm on `side`."""
    pn = body.part_names
    arm = [pn.index(f'{q}.{side}') for q in ('arm_stretch', 'forearm_stretch', 'hand') if f'{q}.{side}' in pn]
    va = set(body.F[body.part == pn.index(f'arm_stretch.{side}')].ravel())
    J = _joint(body, va, set(body.F[~np.isin(body.part, arm)].ravel()))
    vf = set(body.F[body.part == pn.index(f'forearm_stretch.{side}')].ravel())
    vh = body.F[body.part == pn.index(f'hand.{side}')]
    W = _joint(body, vf, set(vh.ravel()))
    a = (W - J) / np.linalg.norm(W - J)
    return J, a, float(((body.V[np.unique(vh)] - J) @ a).max()), arm


def section(V, F, c, a, s_):
    """Length-weighted centre and mean radius of the cut of triangles F by the plane through c + s_ a with normal a (None, None when nothing crosses)."""
    d = (V - c) @ a - s_
    df = d[F]
    cr = (df.max(1) > 0) & (df.min(1) < 0)
    if not cr.any():
        return None, None
    segs = []
    for f, dd in zip(F[cr], df[cr]):
        q = [V[f[i]] + dd[i] / (dd[i] - dd[j]) * (V[f[j]] - V[f[i]]) for i, j in ((0, 1), (1, 2), (2, 0)) if (dd[i] > 0) != (dd[j] > 0)]
        if len(q) == 2:
            segs.append(q)
    P = np.array(segs)
    L = np.linalg.norm(P[:, 1] - P[:, 0], axis=1)
    if L.sum() < 1e-9:
        return None, None
    M = P.mean(1)
    cen = (M * L[:, None]).sum(0) / L.sum()
    q = M - cen
    rad = float((np.linalg.norm(q - (q @ a)[:, None] * a, axis=1) * L).sum() / L.sum())
    return cen, rad


def arm_profile(m, body, side):
    """Cross-sections of the module's arm region (garment and its own skin) along the game arm: stations, centres, radii, game centres."""
    J, a, reach, arm = arm_frame(body, side)
    mf, gf = np.isin(m.part, arm), np.isin(body.part, arm)
    st = np.arange((SYM_SHOULDER_MM / 1000 * _DENS['anat']), reach, SYM_STEP_MM / 1000)
    C, Rr, G = [], [], []
    for s_ in st:
        c, r = section(m.V, m.F[mf], J, a, s_)
        g, _ = section(body.V, body.F[gf], J, a, s_)
        C.append(c if c is not None else np.full(3, np.nan))
        Rr.append(r if r is not None else np.nan)
        G.append(g if g is not None else np.full(3, np.nan))
    C, Rr, G = np.array(C), np.array(Rr), np.array(G)
    ok = np.isfinite(Rr)
    if ok.sum() >= 2:                                   # gaps take the nearest valid station; then a 3-station moving average
        idx = np.flatnonzero(ok)
        near = idx[np.abs(np.arange(len(st))[:, None] - idx[None]).argmin(1)]
        C, Rr = C[near], Rr[near]
        k = np.ones(3) / 3
        C = np.stack([np.convolve(np.pad(C[:, i], 1, mode='edge'), k, 'valid') for i in range(3)], 1)
        Rr = np.convolve(np.pad(Rr, 1, mode='edge'), k, 'valid')
    return {'J': J, 'a': a, 'arm': arm, 's': st, 'C': C, 'R': Rr, 'G': G, 'ok': ok}


def _mirror_pts(P, x0):
    P = np.array(P, np.float64, copy=True)
    P[..., 0] = 2 * x0 - P[..., 0]
    return P


def _morph_arm(m, pr, Ct, Rt, side_mask):
    """Move the arm region's vertices so that each cross-section's centre / radius goes from (pr C, R) to (Ct, Rt), fading in from the shoulder."""
    J, a, st = pr['J'], pr['a'], pr['s']
    vids = np.unique(m.F[side_mask])
    v = m.V[vids]
    sv = (v - J) @ a
    w = np.clip((sv - (SYM_SHOULDER_MM / 1000 * _DENS['anat'])) / ((SYM_RAMP_MM / 1000 * _DENS['anat'])), 0, 1)
    w = w * w * (3 - 2 * w)
    ip = lambda Y: np.stack([np.interp(sv, st, Y[:, i]) for i in range(3)], 1) if Y.ndim == 2 else np.interp(sv, st, Y)  # noqa: E731
    Cw, Rw, Cx, Rx = ip(pr['C']), ip(pr['R']), ip(Ct), ip(Rt)
    dc = Cx - Cw
    dc -= (dc @ a)[:, None] * a                      # the vertex keeps its own place along the arm
    q = v - Cw
    perp = q - (q @ a)[:, None] * a
    sc = np.clip(Rx / np.maximum(Rw, 1e-6), 0.5, 2.0) - 1
    mv = w[:, None] * (dc + sc[:, None] * perp)
    m.V = m.V.copy()
    m.V[vids] = v + mv
    return float(np.linalg.norm(mv, axis=1).max()) * 1000


def arm_excess(m, body):
    """Median over stations along the game arms of (left offset - right offset) / game arm radius, where offset is how far the module's arm
    cross-section centre lies from the game arm's (perpendicular to the arm). Positive: the left arm is the worse one. None without arms."""
    prs = {sd: arm_profile(m, body, sd) for sd in 'lr'}
    ex = []
    for k in range(min(len(prs['l']['s']), len(prs['r']['s']))):
        vals = []
        for sd in 'lr':
            pr = prs[sd]
            c, _ = section(m.V, m.F[np.isin(m.part, pr['arm'])], pr['J'], pr['a'], pr['s'][k])
            g, rg = section(body.V, body.F[np.isin(body.part, pr['arm'])], pr['J'], pr['a'], pr['s'][k])
            if c is None or g is None or not rg:
                break
            dv = (c - g) - ((c - g) @ pr['a']) * pr['a']
            vals.append((float(np.linalg.norm(dv)), rg))
        if len(vals) == 2:
            ex.append((vals[0][0] - vals[1][0]) / max(vals[0][1], vals[1][1]))
    return (float(np.median(ex)), len(ex)) if len(ex) >= 3 else (None, len(ex))


def one_sided_views(mod):
    """True when the module was generated from views that saw only one side: both three-quarter cameras of its job's frozen base views on the
    same side of the body (the gnome jobs before 2026-11-23 had front-left AND back-left). None when the job's cameras are not found."""
    import json
    import design_symmetry as ds
    _, job = ds.module_views(mod)
    if job is None:
        return None
    race = Path(mod).parts[-2]
    for tf in (job / '.base_views' / race / 'transforms.json', ROOT / 'base_views' / race / 'transforms.json'):
        if tf.is_file():
            fr = {f['file_path']: f['transform_matrix'] for f in json.loads(tf.read_text(encoding='utf-8')).get('frames', [])}
            a, b = fr.get('front_three_quarter.png'), fr.get('back_three_quarter.png')
            if a and b:
                return bool(np.sign(a[0][3]) == np.sign(b[0][3]) and abs(a[0][3]) > 0.3 and abs(b[0][3]) > 0.3)
    return None


def auto_symmetry(m, body, mod):
    """AUTO: the symmetry method for this module and why (only outfits / body armour; gauntlets and boots are mirrored anyway).
      one-sided generation (both three-quarter views on one side) + symmetric design  -> whole_mirror (the better half, by arm offsets)
      a malformed arm (arm_excess >= AUTO_SYM_EXCESS) + symmetric design               -> whole_mirror
      a malformed arm + asymmetric design                                              -> sleeve_shift (the torso keeps its asymmetry)
      otherwise                                                                        -> off"""
    if not symmetric_kind(mod):
        return 'off', {'reason': 'not a two-armed module'}
    import design_symmetry as ds
    try:
        ex, n = arm_excess(m, body)
    except Exception as error:  # noqa: BLE001        a body whose arms cannot be measured: no symmetry decision from them
        ex, n = None, f'{type(error).__name__}: {error}'
    da = ds.design_asymmetry(mod, Path(mod).parts[-2])
    asym = None if da is None else da['asymmetry']
    one = one_sided_views(mod)
    why = {'arm_excess_radii': None if ex is None else round(ex, 3), 'stations': n, 'threshold': AUTO_SYM_EXCESS,
           'design_asymmetry': asym, 'design_threshold': AUTO_DESIGN_ASYM, 'one_sided_views': one}
    symmetric = asym is not None and asym < AUTO_DESIGN_ASYM
    bad_arm = ex is not None and abs(ex) >= AUTO_SYM_EXCESS
    if symmetric and one:
        return 'whole_mirror', {**why, 'reason': 'generated from one side only, symmetric design: the better half mirrored'}
    if bad_arm and symmetric:
        return 'whole_mirror', {**why, 'reason': f'{"left" if ex > 0 else "right"} arm malformed, symmetric design: the better half mirrored'}
    if bad_arm:
        return 'sleeve_shift', {**why, 'reason': f'{"left" if ex > 0 else "right"} arm malformed, design asymmetric: only the sleeve is moved'}
    return 'off', {**why, 'reason': 'arms agree' if ex is not None else 'no arm cross-sections'}


def symmetrise(m, body, x0, method):
    """(b) Make a module's arms (or halves) symmetric, in place. The 'better' side is the one whose arm cross-section centres lie closer to the game
    arm's (the game body is mirror-exact). Methods:
      sleeve_shift   the worse arm's cross-sections are moved / scaled onto the mirrored better arm's (its own triangles kept; fades in from the shoulder)
      sleeve_average both arms go halfway, to the average of themselves and the mirrored other arm
      sleeve_copy    the worse arm (its arm-part triangles beyond a plane SYM_SHOULDER_MM out from the shoulder) is replaced by the mirrored better
                     arm; each copied edge loop is moved onto the nearest torso loop (fading over SYM_BLEND_MM) and stitched to it with a strip
      whole_mirror   the better half (cut at the centre plane) is mirrored onto the other half; the halves meet exactly on the plane and are welded"""
    pr = {sd: arm_profile(m, body, sd) for sd in 'lr'}
    off = {sd: (float(np.nanmean(np.linalg.norm(pr[sd]['C'][pr[sd]['ok']] - pr[sd]['G'][pr[sd]['ok']], axis=1))) if pr[sd]['ok'].any() else np.inf)
           for sd in 'lr'}
    good = 'l' if off['l'] <= off['r'] else 'r'
    bad = 'r' if good == 'l' else 'l'
    info = {'method': method, 'arm_offset_mm': {k: round(v * 1000, 1) for k, v in off.items()}, 'better_side': good}
    if method in ('sleeve_shift', 'sleeve_average'):
        if not (pr['l']['ok'].sum() >= 2 and pr['r']['ok'].sum() >= 2):
            info['skipped'] = 'no arm cross-sections'
            return info
        Cg_m, Rg = _mirror_pts(pr[good]['C'], x0), pr[good]['R']
        if method == 'sleeve_shift':
            info['moved_mm_max'] = {bad: round(_morph_arm(m, pr[bad], Cg_m, Rg, np.isin(m.part, pr[bad]['arm'])), 1)}
        else:
            Ct, Rt = (pr[bad]['C'] + Cg_m) / 2, (pr[bad]['R'] + Rg) / 2
            mb = _morph_arm(m, pr[bad], Ct, Rt, np.isin(m.part, pr[bad]['arm']))
            mg = _morph_arm(m, pr[good], _mirror_pts(Ct, x0), Rt, np.isin(m.part, pr[good]['arm']))
            info['moved_mm_max'] = {bad: round(mb, 1), good: round(mg, 1)}
    elif method == 'whole_mirror':
        sgn = 1. if good == 'l' else -1.                  # anatomical left = +x
        phi = (m.V[:, 0] - x0) * sgn
        phi = np.where(np.abs(phi) < 1e-7, 1e-7, phi)
        M2, ph = iso_cut(m, phi, np.ones(len(m.F), bool))
        half = compact(M2.subset(ph[M2.F].sum(1) > 0))
        whole = compact(concat([half, mirror_mesh(half, x0, body)]), weld=1e-6)
        info.update({'faces_before': int(len(m.F)), 'faces_after': int(len(whole.F))})
        m.V, m.F, m.role, m.part, m.prov = whole.V, whole.F, whole.role, whole.part, whole.prov
    elif method == 'sleeve_copy':
        info.update(_sleeve_copy(m, body, x0, pr, good, bad))
    return info


def _cut_beyond(m, J, a, arm):
    """Cut m's faces on the arm's side at the plane SYM_SHOULDER_MM out along the arm; returns (mesh, mask of the arm faces beyond the plane)."""
    phi = (m.V - J) @ a - (SYM_SHOULDER_MM / 1000 * _DENS['anat'])
    phi = np.where(np.abs(phi) < 1e-7, 1e-7, phi)
    side = np.sign(J[0]) * m.V[:, 0] > 0
    M2, ph = iso_cut(m, phi, side[m.F].all(1))
    beyond = (ph[M2.F].sum(1) > 0) & np.isin(M2.part, arm)
    # only pieces of it that touch the plane (a stray far piece labelled arm stays)
    lab = _comp(M2.F, beyond)
    touch = set(int(x) for x in np.unique(lab[beyond & (np.abs(ph[M2.F]).min(1) < 1e-6)]))
    beyond &= np.isin(lab, list(touch))
    return M2, beyond


def _comp(F, mask):
    """Edge-connected component label per face (-1 outside mask)."""
    return components(len(F), face_adjacency(F, mask), mask)


def _loops_of(m, mask):
    return [np.array(L) for L in splice_loops(boundary_loops(m.subset(mask))) if len(L) >= 3]


def _sleeve_copy(m, body, x0, pr, good, bad):
    Jg, ag, armg = pr[good]['J'], pr[good]['a'], pr[good]['arm']
    Jb, ab_, armb = pr[bad]['J'], pr[bad]['a'], pr[bad]['arm']
    M1, beyond_b = _cut_beyond(m, Jb, ab_, armb)                     # the worse arm, removed
    M2, beyond_g = _cut_beyond(M1, Jg, ag, armg)                     # the better arm, copied (cut in the same mesh so ids stay in M2)
    beyond_b = np.r_[beyond_b, np.zeros(0, bool)]
    # M1 faces were split again by the second cut only on the good side, so recompute the worse region in M2 by position and part
    sidev = np.sign(Jb[0]) * M2.V[:, 0] > 0
    phb = (M2.V - Jb) @ ab_ - (SYM_SHOULDER_MM / 1000 * _DENS['anat'])
    lab = _comp(M2.F, sidev[M2.F].all(1) & (phb[M2.F].sum(1) > 0) & np.isin(M2.part, armb))
    rem = sidev[M2.F].all(1) & (phb[M2.F].sum(1) > 0) & np.isin(M2.part, armb)
    touch = set(int(x) for x in np.unique(lab[rem & (np.abs(phb[M2.F]).min(1) < 1e-6)]))
    rem &= np.isin(lab, list(touch))
    keep = ~rem
    torso = compact(M2.subset(keep))
    copy = mirror_mesh(compact(M2.subset(beyond_g)), x0, body)
    info = {'faces_removed': int(rem.sum()), 'faces_copied': int(len(copy.F))}
    if not len(copy.F):
        info['skipped'] = 'nothing to copy'
        return info
    # pair each copied edge loop with the torso hole loop nearest to it
    hole_loops = _loops_of(torso, np.ones(len(torso.F), bool))
    copy_loops = _loops_of(copy, np.ones(len(copy.F), bool))
    from scipy.spatial import cKDTree
    pairs, blend_mm = [], []
    used = set()
    for cl in copy_loops:
        best, bd = None, np.inf
        for hi, hlp in enumerate(hole_loops):
            if hi in used:
                continue
            HA, HB = torso.V[hlp], torso.V[np.roll(hlp, -1)]
            Q, d, _ = seg_closest(copy.V[cl], HA, HB)
            if d.mean() < bd:
                best, bd = hi, d.mean()
        if best is not None and bd <= SYM_PAIR_MM / 1000:
            used.add(best)
            pairs.append((cl, hole_loops[best]))
    # blend: every copied boundary vertex goes onto its torso loop; copied vertices nearby follow, fading over SYM_BLEND_MM
    if pairs:
        bvid = np.concatenate([cl for cl, _ in pairs])
        disp = np.zeros((len(bvid), 3))
        k0 = 0
        for cl, hlp in pairs:
            Q, d, _ = seg_closest(copy.V[cl], torso.V[hlp], torso.V[np.roll(hlp, -1)])
            disp[k0:k0 + len(cl)] = Q - copy.V[cl]
            blend_mm += list(d * 1000)
            k0 += len(cl)
        dist, nn = cKDTree(copy.V[bvid]).query(copy.V)
        wgt = np.clip(1 - dist / ((SYM_BLEND_MM / 1000 * _DENS['anat'])), 0, 1)
        copy.V = copy.V + wgt[:, None] * disp[nn]
    # stitch: a strip of triangles between each copied loop and its torso loop (greedy shortest diagonal), wound like the torso faces
    nt = len(torso.V)
    allV = np.concatenate([torso.V, copy.V])
    strips, srole = [], []
    for cl, hlp in pairs:
        A, B = list(hlp), list(cl + nt)
        ax = ab_
        def turn(L):
            P = allV[L] - allV[L].mean(0)
            return np.sign(np.cross(P, np.roll(P, -1, axis=0)).sum(0) @ ax)
        if turn(A) != turn(B):
            B = B[::-1]
        k = int(np.argmin(np.linalg.norm(allV[B] - allV[A[0]], axis=1)))
        B = B[k:] + B[:k]
        i = j = 0
        T = []
        while i < len(A) or j < len(B):
            a0, a1 = A[i % len(A)], A[(i + 1) % len(A)]
            b0, b1 = B[j % len(B)], B[(j + 1) % len(B)]
            if j >= len(B) or (i < len(A) and np.linalg.norm(allV[a1] - allV[b0]) <= np.linalg.norm(allV[b1] - allV[a0])):
                T.append((a0, a1, b0))
                i += 1
            else:
                T.append((a0, b1, b0))
                j += 1
        # winding: the strip uses the torso loop edge a0 -> a1 opposite to the torso face that owns it
        e2f = {}
        for f in torso.F:
            for x, y in ((f[0], f[1]), (f[1], f[2]), (f[2], f[0])):
                e2f[(int(x), int(y))] = True
        flip = (int(A[0]), int(A[1 % len(A)])) in e2f
        T = [(t[0], t[2], t[1]) for t in T] if flip else T
        strips += T
        own = torso.role[np.flatnonzero((torso.F == A[0]).any(1))[:1]]
        srole += [own[0] if len(own) else 'primary'] * len(T)
    out = concat([torso, copy])
    if strips:
        out = Mesh(out.V, np.concatenate([out.F, np.array(strips, np.int64)]), np.r_[out.role, srole], np.r_[out.part, [-1] * len(strips)],
                   np.r_[out.prov, ['stitch'] * len(strips)])
        miss = out.part < 0
        out.part[miss] = body.measure(out.V[out.F[miss]].mean(1))['part']
    out = compact(out, weld=1e-6)
    m.V, m.F, m.role, m.part, m.prov = out.V, out.F, out.role, out.part, out.prov
    info.update({'loops_copied': len(copy_loops), 'loops_stitched': len(pairs), 'strip_faces': len(strips),
                 'edge_moved_mm': [round(float(np.median(blend_mm)), 1), round(float(np.max(blend_mm)), 1)] if blend_mm else None})
    return info


def edge_stats(V, F):
    """(mean incident edge length per vertex, median edge length) of a triangle mesh."""
    E = np.unique(np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1), axis=0)
    L = np.linalg.norm(V[E[:, 0]] - V[E[:, 1]], axis=1)
    s_, c_ = np.zeros(len(V)), np.zeros(len(V))
    for k in (0, 1):
        np.add.at(s_, E[:, k], L)
        np.add.at(c_, E[:, k], 1)
    return s_ / np.maximum(c_, 1), float(np.median(L)) if len(L) else 0.


def sliver_mask(Ls, A2):
    """Sliver triangles: fixed mm (SPIKE_LEN_MM / SPIKE_H_MM) or, density-aware, by shape: height / longest edge < DENS_SLIVER_ASPECT and the longest
    edge above half the mesh's median edge."""
    Lmax = Ls.max(1)
    h = A2 / np.maximum(Lmax, 1e-12)
    if DENSITY_AWARE and 'sliver' in DENS_PARTS:
        return (h / np.maximum(Lmax, 1e-12) < DENS_SLIVER_ASPECT) & (Lmax > 0.5 * np.median(Ls)) & (A2 > 1e-12)
    return (Lmax > SPIKE_LEN_MM / 1000) & (h < SPIKE_H_MM / 1000) & (A2 > 1e-12)


def facet_clearance(V, F, verts):
    """Density-aware clearance per vertex: at least CLEAR_MM, and at least the facet error of the garment there (a flat triangle of edge h on a
    surface bending by angle theta per edge lies h * theta / 8 inside the curve), at most DENS_CLEAR_MAX_MM."""
    hv, _ = edge_stats(V, F)
    N = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    N /= np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-18)
    vn = np.zeros((len(V), 3))
    for k in range(3):
        np.add.at(vn, F[:, k], N)
    vn /= np.maximum(np.linalg.norm(vn, axis=1, keepdims=True), 1e-18)
    ang = np.zeros(len(V))
    for k in range(3):
        np.maximum.at(ang, F[:, k], np.arccos(np.clip((N * vn[F[:, k]]).sum(1), -1, 1)))
    sag = hv * (2 * ang) / 8
    return np.clip(sag[verts], CLEAR_MM / 1000, DENS_CLEAR_MAX_MM / 1000)


def refine_min():
    """(d) refinement target: EDGE_REFINE_MIN_MM, or density-aware DENS_REFINE_K x the module's median edge (10..40 mm)."""
    if DENSITY_AWARE and _DENS['ref']:
        return float(np.clip(DENS_REFINE_K * _DENS['ref'], 0.010, 0.040))
    return EDGE_REFINE_MIN_MM / 1000


def module_iso_in(m, body):
    """Density-aware inward search of the cut: deep enough for the module's own skin (its deepest 1 % plus 15 mm), 40..120 mm."""
    sk = m.role == SKIN
    if not sk.any():
        return None
    d = body.measure(m.V[m.F[sk]].mean(1))['d']
    return float(np.clip(max(ISO_IN_MM / 1000, -np.percentile(d, 1) + 0.015), ISO_IN_MM / 1000, 0.12))


def _segs_cross_tris(P0, P1, A, B, C, eps=1e-9):
    """(S, T) bool: segment s (P0[s] -> P1[s], ends excluded) passes through triangle t (A, B, C)."""
    if not len(P0) or not len(A):
        return np.zeros((len(P0), len(A)), bool)
    D = (P1 - P0)[:, None, :]
    e1, e2 = (B - A)[None], (C - A)[None]
    pv = np.cross(D, e2)
    det = (e1 * pv).sum(-1)
    ok = np.abs(det) > eps
    inv = 1. / np.where(ok, det, 1.)
    tv = P0[:, None, :] - A[None]
    u = (tv * pv).sum(-1) * inv
    qv = np.cross(tv, e1)
    v = (D * qv).sum(-1) * inv
    t = (e2 * qv).sum(-1) * inv
    return ok & (u > 1e-7) & (v > 1e-7) & (u + v < 1 - 1e-7) & (t > 1e-6) & (t < 1 - 1e-6)


def move_is_safe(V, F, vf, v, new, tree=None, reach=None):
    """A vertex move v -> new is safe when the path and the moved faces cross no other triangle near it, and no incident face that keeps its area
    turns over (layers keep their order). vf: faces per vertex; tree: cKDTree of V (for the neighbourhood)."""
    from scipy.spatial import cKDTree
    old = V[v].copy()
    inc = np.array(vf[v], np.int64)
    if not len(inc):
        return True
    ring = np.unique(F[inc])
    L = np.linalg.norm(V[ring] - old, axis=1).max()
    tree = tree or cKDTree(V)
    near_v = tree.query_ball_point(old, (reach or 0.) + L + np.linalg.norm(new - old) + 1e-4)
    near_f = np.unique(np.concatenate([np.array(vf[x], np.int64) for x in near_v] + [np.zeros(0, np.int64)]))
    near_f = near_f[~np.isin(near_f, inc)]
    near_f = near_f[~np.any(F[near_f] == v, axis=1)]
    W = V.copy()
    W[v] = new
    n0 = np.cross(V[F[inc, 1]] - V[F[inc, 0]], V[F[inc, 2]] - V[F[inc, 0]])
    n1 = np.cross(W[F[inc, 1]] - W[F[inc, 0]], W[F[inc, 2]] - W[F[inc, 0]])
    a0, a1 = np.linalg.norm(n0, axis=1), np.linalg.norm(n1, axis=1)
    keepa = a1 > 0.2 * a0
    if np.any(keepa & ((n0 * n1).sum(1) <= 0)):
        return False
    if not len(near_f):
        return True
    # faces near v that share a vertex of the ring are skipped for the edge tests of that vertex only via the endpoint exclusion (t in (0, 1))
    TA, TB, TC = W[F[near_f, 0]], W[F[near_f, 1]], W[F[near_f, 2]]
    nb = np.setdiff1d(ring, [v])
    segs0 = np.vstack([old[None], np.repeat(new[None], len(nb), 0)])
    segs1 = np.vstack([new[None], W[nb]])
    hit = _segs_cross_tris(segs0, segs1, TA, TB, TC)
    # a ring edge v-n may touch triangles that contain n: those share an endpoint, ignore
    for k, x in enumerate(nb, start=1):
        hit[k] &= ~np.any(F[near_f] == x, axis=1)
    if hit.any():
        return False
    # edges of the nearby triangles through the moved faces
    E0 = np.concatenate([TA, TB, TC])
    E1 = np.concatenate([TB, TC, TA])
    own = np.concatenate([F[near_f][:, [0, 1]], F[near_f][:, [1, 2]], F[near_f][:, [2, 0]]])
    MA, MB, MC = W[F[inc, 0]], W[F[inc, 1]], W[F[inc, 2]]
    hit2 = _segs_cross_tris(E0, E1, MA, MB, MC)
    share = np.zeros_like(hit2)
    for j, f in enumerate(inc):
        share[:, j] = np.isin(own, F[f]).any(1)
    return not np.any(hit2 & ~share)


def collapse_folded_slivers(m):
    """Sliver triangles (longest edge > SPIKE_LEN_MM, height < SPIKE_H_MM) folded over against at least two of their edge neighbours show their back
    as a long red line: the vertex opposite the long edge is moved onto that edge (the sliver loses its area; at most SPIKE_H_MM moved, no faces
    change). Returns the number collapsed."""
    T = m.V[m.F]
    Ls = np.stack([np.linalg.norm(T[:, 1] - T[:, 0], axis=1), np.linalg.norm(T[:, 2] - T[:, 1], axis=1), np.linalg.norm(T[:, 0] - T[:, 2], axis=1)], 1)
    N = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
    A2 = np.linalg.norm(N, axis=1)
    N = N / np.maximum(A2, 1e-18)[:, None]
    sliver = sliver_mask(Ls, A2)
    if not sliver.any():
        return 0
    E = np.sort(np.concatenate([m.F[:, [0, 1]], m.F[:, [1, 2]], m.F[:, [2, 0]]]), axis=1)
    fe = np.tile(np.arange(len(m.F)), 3)
    ue, inv = np.unique(E, axis=0, return_inverse=True)
    inv = inv.ravel()
    order = np.argsort(inv, kind='stable')
    starts = np.searchsorted(inv[order], np.arange(len(ue)))
    ends = np.searchsorted(inv[order], np.arange(len(ue)), side='right')
    V = m.V.copy()
    n = 0
    from scipy.spatial import cKDTree
    tree = cKDTree(V)
    vf = [[] for _ in range(len(V))]
    for fi, fv in enumerate(m.F):
        for x in fv:
            vf[x].append(fi)
    for f in np.flatnonzero(sliver):
        against = 0
        for k in range(3):
            e = inv[f + k * len(m.F)]
            for g in fe[order[starts[e]:ends[e]]]:
                if g != f and N[f] @ N[g] < 0:
                    against += 1
        if against < 2:
            continue
        k = int(np.argmax(Ls[f]))                       # long edge (k, k+1); apex is the third corner
        a_, b_, c_ = m.F[f][k], m.F[f][(k + 1) % 3], m.F[f][(k + 2) % 3]
        ab = V[b_] - V[a_]
        t = np.clip((V[c_] - V[a_]) @ ab / max(ab @ ab, 1e-18), 0, 1)
        new = V[a_] + t * ab
        if not move_is_safe(V, m.F, vf, c_, new, tree):          # would push a layer through another: leave it
            continue
        V[c_] = new
        n += 1
    m.V = V
    return n


def clean_spikes(m):
    """(c) Flatten generator spikes in place: a vertex most of whose triangles are slivers (longest edge > SPIKE_LEN_MM, height < SPIKE_H_MM) and that
    stands more than SPIKE_OUT_MM out of the plane fitted to its neighbours is moved into that plane; sliver triangles with two open edges are dropped."""
    T = m.V[m.F]
    Ls = np.stack([np.linalg.norm(T[:, 1] - T[:, 0], axis=1), np.linalg.norm(T[:, 2] - T[:, 1], axis=1), np.linalg.norm(T[:, 0] - T[:, 2], axis=1)], 1)
    A2 = np.linalg.norm(np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0]), axis=1)
    sliver = sliver_mask(Ls, A2)
    E = np.sort(np.concatenate([m.F[:, [0, 1]], m.F[:, [1, 2]], m.F[:, [2, 0]]]), axis=1)
    ue, inv, cnt = np.unique(E, axis=0, return_inverse=True, return_counts=True)
    inv = inv.ravel()
    open_per_face = (cnt[inv] == 1).reshape(3, -1).sum(0)
    drop = sliver & (open_per_face >= 2)
    nbr = [set() for _ in range(len(m.V))]
    for x, y in ue:
        nbr[x].add(int(y))
        nbr[y].add(int(x))
    vf = [[] for _ in range(len(m.V))]
    for fi, f in enumerate(m.F):
        for v in f:
            vf[v].append(fi)
    moved = []
    V = m.V.copy()
    from scipy.spatial import cKDTree
    tree = cKDTree(V)
    refused = 0
    for v in np.unique(m.F[sliver & ~drop]):
        fs = vf[v]
        if np.mean(sliver[fs]) < 0.5 or len(nbr[v]) < 3:
            continue
        P = V[list(nbr[v])]
        c = P.mean(0)
        _, _, vt = np.linalg.svd(P - c)
        n = vt[-1]
        h = (V[v] - c) @ n
        if abs(h) > SPIKE_OUT_MM / 1000:
            if not move_is_safe(V, m.F, vf, v, V[v] - h * n, tree):    # the flattening would cross another layer
                refused += 1
                continue
            V[v] = V[v] - h * n
            moved.append(abs(h) * 1000)
    m.V = V
    if drop.any():
        keep = ~drop
        m.F, m.role, m.part, m.prov = m.F[keep], m.role[keep], m.part[keep], m.prov[keep]
    folded = collapse_folded_slivers(m)
    return {'sliver_faces': int(sliver.sum()), 'folded_slivers_collapsed': folded, 'spike_vertices_flattened': len(moved),
            'flattened_mm_max': round(max(moved), 1) if moved else 0., 'dangling_slivers_dropped': int(drop.sum()), 'moves_refused_crossing': refused}


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

REGION_TEST = 'iso'      # 'skin': game faces whose closest module face is skin; 'rays': faces no garment covers along the normal (strays in hollows)
BORDER_JOIN_MM = 40.0    # a free garment edge this close to an uncovered stretch of the slot's border loop is moved onto it and joined
GROW_EXPOSED = True      # slot faces no garment covers (along the normal), connected to the game skin or the slot border, are game skin too
GROW_NEAR_MM = 150.0     # ... only within this of the slot border
GROW_COVER_IN_MM = 40.0  # ... and a garment this far inside the game body still covers
HIDDEN_OUT_MM = 120.0    # slot skin the generator's head / hand skin covers within this along its normal (a beard over a V-neck) becomes garment (patch)
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


def under_counts(Gm, oc, S, m, game_only):
    """Per game face, over 4 samples: how many have a skin face as their closest module face, how many of those are skin of a game-owned part
    (the generator's head / beard), and the garment role nearest the face (for patches)."""
    A0, B0, C0 = Gm.V[Gm.F[:, 0]], Gm.V[Gm.F[:, 1]], Gm.V[Gm.F[:, 2]]
    c = (A0 + B0 + C0) / 3
    P = np.concatenate([c, (2 * c + A0) / 3, (2 * c + B0) / 3, (2 * c + C0) / 3])
    _, tri, _ = oc.closest(P)
    f = oc.part[tri]
    skin = S[f].reshape(4, -1)
    foreign = (skin.ravel() & np.isin(m.part[f], game_only if game_only is not None else [])).reshape(4, -1)
    g = m.role != SKIN
    role = np.full(len(Gm.F), 'primary', dtype='<U10')
    if g.any():
        gb = bf.Body(m.V, m.F[g], np.flatnonzero(g), [])
        _, gt, _ = gb.closest(c)
        role = m.role[gb.part[gt]]
    return skin.sum(0), foreign.sum(0), role


HIDDEN_SKIN = False       # off: it also hid the thighs under loose tassets (holes); the gauntlet poke it was for came from the elbow skin
HIDDEN_SKIN_MM = 60.0     # module skin with its own garment within this along its normal is hidden skin (inside a gauntlet), cut as garment
POKE_DROP = False         # off: it opened holes under the tassets and did not reach the gauntlet poke
POKE_CLEAR_MM = 6.0       # game skin faces with garment along their normal from ISO_IN_MM inside to this far outside are not kept (they would poke)
ISO_PICK = 'inner'        # which module surface a game vertex corresponds to along its normal: 'inner' (the body before what is worn on it) or 'nearest'
ISO_IN_MM = 40.0          # iso cut: the module surface a game vertex corresponds to is found along its normal from this far inside ...
ISO_MISS = 'skin'         # a game vertex with no module surface along its normal within reach: 'skin' or 'closest' (the closest module point decides)
ISO_MISS_PHI = 0.02
RIM_EPS_MM = 4.0          # phi read inside a module face keeps that face's side by at least this (a one-triangle garment strip is not 0)
BODY_OF_GM = []            # set by game_region_iso (the game body the cut is made on)
ISO_OUT_MM = 30.0         # ... to this far outside (nearest hit; the closest module point where the line misses)


def module_psi(m, keepg):
    """Signed geodesic distance over the module to its kept-garment boundary: + on skin (and dropped garment), - on kept garment, 0 on the rim."""
    gv = np.zeros(len(m.V), bool)
    gv[np.unique(m.F[keepg])] = True
    sv = np.zeros(len(m.V), bool)
    sv[np.unique(m.F[~keepg])] = True
    rim = gv & sv
    e = np.unique(np.sort(np.concatenate([m.F[:, [0, 1]], m.F[:, [1, 2]], m.F[:, [2, 0]]]), axis=1), axis=0)
    w = np.linalg.norm(m.V[e[:, 0]] - m.V[e[:, 1]], axis=1) + 1e-9
    G = sparse.coo_matrix((np.r_[w, w], (np.r_[e[:, 0], e[:, 1]], np.r_[e[:, 1], e[:, 0]])), shape=(len(m.V), len(m.V))).tocsr()
    if rim.any():
        dist = csgraph.dijkstra(G, directed=False, indices=np.flatnonzero(rim), min_only=True)
    else:
        dist = np.full(len(m.V), np.inf)
    dist = np.where(np.isfinite(dist), dist, 1.)
    psi = np.where(gv & ~sv, -dist, dist)
    psi[rim] = 0.
    # the side of each module face (- kept garment, + skin): a garment strip one triangle wide has all three corners on the rim, so its psi is 0
    # everywhere inside it (Codex review 2026-11-23: 33 such triangles, 757 cm2, on the gnome ceremony dress); phi_points keeps the face's side
    m.psi_fsign = np.where(keepg, -1., 1.)
    return psi


def vertex_normals(V, F):
    n = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    vn = np.zeros_like(V)
    for k in range(3):
        np.add.at(vn, F[:, k], n)
    return vn / (np.linalg.norm(vn, axis=1, keepdims=True) + 1e-12)


def nearest_hits(P, N, V, F):
    """Per point: the face of (V, F) the line P + t N meets nearest to P, t from -ISO_IN_MM to +ISO_OUT_MM; (-1 where none) and its barycentrics."""
    ci, co = (_DENS['iso_in'] or ISO_IN_MM / 1000), ISO_OUT_MM / 1000
    O = P - N * ci
    D = N * (ci + co)
    TA, TB, TC = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    e1, e2 = TB - TA, TC - TA
    face = np.full(len(P), -1)
    bu, bv_ = np.zeros(len(P)), np.zeros(len(P))
    for k in range(0, len(P), 128):
        o, d = O[k:k + 128, None, :], D[k:k + 128, None, :]
        pv = np.cross(d, e2[None])
        det = (e1[None] * pv).sum(2)
        ok = np.abs(det) > 1e-14
        inv = np.where(ok, 1. / np.where(ok, det, 1.), 0.)
        tv = o - TA[None]
        u = (tv * pv).sum(2) * inv
        qv = np.cross(tv, e1[None])
        v = (d * qv).sum(2) * inv
        t = (e2[None] * qv).sum(2) * inv
        hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t >= 0) & (t <= 1)
        score = np.where(hit, (t if ISO_PICK == 'inner' else np.abs(t * (ci + co) - ci)), np.inf)
        j = score.argmin(1)
        r = np.arange(len(j))
        good = np.isfinite(score[r, j])
        face[k:k + 128] = np.where(good, j, -1)
        bu[k:k + 128] = u[r, j]
        bv_[k:k + 128] = v[r, j]
    return face, bu, bv_


def phi_at(Gm, vids, m, psi, oc):
    """The module's psi at the module point each game vertex corresponds to (along its normal, else closest)."""
    vn = vertex_normals(Gm.V, Gm.F)
    return phi_points(Gm.V[vids], vn[vids], m, psi, oc)


def phi_points(P, N, m, psi, oc):
    """phi_at for any points P with unit normals N."""
    vids = np.arange(len(P))
    f, u, v = nearest_hits(P, N, m.V, m.F)
    out = np.zeros(len(vids))
    hit = f >= 0
    fs = getattr(m, 'psi_fsign', None)
    if fs is not None and len(fs) != len(m.F):
        fs = None
    if hit.any():
        Fh = m.F[f[hit]]
        out[hit] = (1 - u[hit] - v[hit]) * psi[Fh[:, 0]] + u[hit] * psi[Fh[:, 1]] + v[hit] * psi[Fh[:, 2]]
        if fs is not None:                                # never the other side's sign (nor 0) inside a face
            sg = fs[f[hit]]
            out[hit] = sg * np.maximum(sg * out[hit], RIM_EPS_MM / 1000)
    if (~hit).any():
        _, tri, bary = oc.closest(P[~hit])
        Fc = m.F[oc.part[tri]]
        val = (bary * psi[Fc]).sum(1)
        if fs is not None:
            sg = fs[oc.part[tri]]
            val = sg * np.maximum(sg * val, RIM_EPS_MM / 1000)
        if ISO_MISS == 'skin':
            # nothing of the module along the normal: the closest module point decides where the module is close to (or inside) the game body;
            # where it stands well off (a breastplate plate in front of an open V-neck), it is loose garment and the skin runs on under it
            Q = (bary[:, :, None] * m.V[Fc]).sum(1)
            dq = BODY_OF_GM[0].measure(Q)['d'] if BODY_OF_GM else np.zeros(len(Q))
            val = np.where(dq > ISO_OUT_MM / 1000, ISO_MISS_PHI, val)
        out[~hit] = val
    return out


def iso_cut(Gm, phi, ins):
    """Cut the slot faces of Gm along phi = 0 (straight within each triangle, cut points shared by neighbours). Returns (Mesh, phi)."""
    V = [tuple(x) for x in Gm.V]
    ph = list(phi)
    cut = {}

    def cv(x, y):
        key = (min(x, y), max(x, y))
        if key not in cut:
            t = phi[x] / (phi[x] - phi[y])
            cut[key] = len(V)
            V.append(tuple(Gm.V[x] + t * (Gm.V[y] - Gm.V[x])))
            ph.append(0.)
        return cut[key]
    outF, parent = [], []
    for fi, (a, b, c) in enumerate(Gm.F):
        sg = phi[[a, b, c]] > 0
        if not ins[fi] or sg.all() or not sg.any():
            outF.append((a, b, c))
            parent.append(fi)
            continue
        lone = int(np.flatnonzero(sg)[0]) if sg.sum() == 1 else int(np.flatnonzero(~sg)[0])
        i, j, k = [(a, b, c), (b, c, a), (c, a, b)][lone]
        pp, qq = cv(i, j), cv(i, k)
        outF += [(i, pp, qq), (pp, j, k), (pp, k, qq)]
        parent += [fi] * 3
    parent = np.array(parent)
    return Mesh(np.array(V), np.array(outF, np.int64), Gm.role[parent], Gm.part[parent], Gm.prov[parent]), np.array(ph)


def game_region_iso(m, body, slot, game_only, keepg):
    """The slot's game faces that lie under the module's skin, cut exactly along the module's garment edges projected onto the game body (each game
    vertex takes the module's signed distance to its garment edge at the module point along its normal; the zero line is cut into the mesh).
    Returns (Mesh, R, border vertex mask, edges split, cut vertices)."""
    Gm = base_mesh(body).copy()
    BODY_OF_GM[:] = [body]
    # the module's own skin that its garment covers (the generator's forearm inside its gauntlet) is hidden: it counts as garment for the cut
    hidden = np.zeros(len(m.F), bool)
    sk = np.flatnonzero(~keepg)
    if HIDDEN_SKIN and len(sk) and keepg.any():
        subm = Mesh(m.V, m.F[sk], m.role[sk], m.part[sk], m.prov[sk])
        hidden[sk[covered_count(subm, m.V, m.F[keepg], cover_in=0., cover_out=HIDDEN_SKIN_MM) >= 3]] = True
    m.hidden_skin = int(hidden.sum())
    psi = module_psi(m, keepg | hidden)
    oc = outfit_closest(m)
    phi = phi_at(Gm, np.arange(len(Gm.V)), m, psi, oc)
    splits = 0
    for it in range(REFINE_ITERS):
        ins = np.isin(Gm.part, slot)
        bv = np.zeros(len(Gm.V), bool)
        bv[np.intersect1d(np.unique(Gm.F[ins]), np.unique(Gm.F[~ins]))] = True
        sg = phi > 0
        mixed = ins & sg[Gm.F].any(1) & ~sg[Gm.F].all(1)
        if not mixed.any():
            break
        E = np.unique(np.sort(np.concatenate([Gm.F[mixed][:, [0, 1]], Gm.F[mixed][:, [1, 2]], Gm.F[mixed][:, [2, 0]]]), axis=1), axis=0)
        L = np.linalg.norm(Gm.V[E[:, 0]] - Gm.V[E[:, 1]], axis=1)
        mark = {tuple(map(int, e)) for e, l_ in zip(E, L) if l_ > REFINE_EDGE_MM / 1000 and not (bv[e[0]] and bv[e[1]])}
        if not mark:
            break
        n0 = len(Gm.V)
        V2, F2, par = split_edges(Gm.V, Gm.F, mark)
        Gm = Mesh(V2, F2, Gm.role[par], Gm.part[par], Gm.prov[par])
        phi = np.r_[phi, phi_at(Gm, np.arange(n0, len(Gm.V)), m, psi, oc)]
        splits += len(mark)
    BARY = np.array([(i / 4, j / 4, 1 - i / 4 - j / 4) for i in range(5) for j in range(5 - i)])
    BARY = BARY[(BARY > 1e-9).sum(1) >= 2]                    # interior and edge samples, not the corners
    for it in range(EDGE_REFINE_ITERS if EDGE_REFINE else 0):
        ins = np.isin(Gm.part, slot)
        bv = np.zeros(len(Gm.V), bool)
        bv[np.intersect1d(np.unique(Gm.F[ins]), np.unique(Gm.F[~ins]))] = True
        sg = phi[Gm.F] > 0
        T = Gm.V[Gm.F]
        Lf = np.stack([np.linalg.norm(T[:, 1] - T[:, 0], axis=1), np.linalg.norm(T[:, 2] - T[:, 1], axis=1), np.linalg.norm(T[:, 0] - T[:, 2], axis=1)], 1)
        cf = np.flatnonzero(ins & (sg.all(1) | ~sg.any(1)) & (Lf.max(1) > refine_min()))
        if not len(cf):
            break
        vn = vertex_normals(Gm.V, Gm.F)
        P = np.einsum('kc,fcx->fkx', BARY, T[cf]).reshape(-1, 3)
        N = np.einsum('kc,fcx->fkx', BARY, vn[Gm.F[cf]]).reshape(-1, 3)
        N /= np.linalg.norm(N, axis=1, keepdims=True) + 1e-12
        ps = phi_points(P, N, m, psi, oc).reshape(len(cf), len(BARY))
        other = ((ps > 0) != sg[cf, :1]) & (np.abs(ps) > SIGN_REFINE_EPS_MM / 1000)
        hitf = cf[other.any(1)]
        mark = set()
        for f in hitf:
            for k, (x, y) in enumerate(((0, 1), (1, 2), (2, 0))):
                a_, b_ = int(Gm.F[f, x]), int(Gm.F[f, y])
                if Lf[f, k] > refine_min() and not (bv[a_] and bv[b_]):
                    mark.add((min(a_, b_), max(a_, b_)))
        if not mark:
            break
        n0 = len(Gm.V)
        V2, F2, par = split_edges(Gm.V, Gm.F, mark)
        Gm = Mesh(V2, F2, Gm.role[par], Gm.part[par], Gm.prov[par])
        phi = np.r_[phi, phi_at(Gm, np.arange(n0, len(Gm.V)), m, psi, oc)]
        splits += len(mark)
    for it in range(SIGN_REFINE_ITERS):
        ins = np.isin(Gm.part, slot)
        bv = np.zeros(len(Gm.V), bool)
        bv[np.intersect1d(np.unique(Gm.F[ins]), np.unique(Gm.F[~ins]))] = True
        E = np.unique(np.sort(np.concatenate([Gm.F[ins][:, [0, 1]], Gm.F[ins][:, [1, 2]], Gm.F[ins][:, [2, 0]]]), axis=1), axis=0)
        L = np.linalg.norm(Gm.V[E[:, 0]] - Gm.V[E[:, 1]], axis=1)
        same = (phi[E[:, 0]] > 0) == (phi[E[:, 1]] > 0)
        cand = E[same & (L > (1.5 * refine_min() if DENSITY_AWARE and _DENS['ref'] else SIGN_REFINE_MIN_MM / 1000)) & ~(bv[E[:, 0]] & bv[E[:, 1]])]
        if not len(cand):
            break
        vn = vertex_normals(Gm.V, Gm.F)
        nm = vn[cand[:, 0]] + vn[cand[:, 1]]
        nm /= np.linalg.norm(nm, axis=1, keepdims=True) + 1e-12
        pm = phi_points((Gm.V[cand[:, 0]] + Gm.V[cand[:, 1]]) / 2, nm, m, psi, oc)
        flip = ((pm > 0) != (phi[cand[:, 0]] > 0)) & (np.abs(pm) > SIGN_REFINE_EPS_MM / 1000)
        mark = {tuple(map(int, e)) for e in cand[flip]}
        if not mark:
            break
        n0 = len(Gm.V)
        V2, F2, par = split_edges(Gm.V, Gm.F, mark)
        Gm = Mesh(V2, F2, Gm.role[par], Gm.part[par], Gm.prov[par])
        phi = np.r_[phi, phi_at(Gm, np.arange(n0, len(Gm.V)), m, psi, oc)]
        splits += len(mark)
    ins = np.isin(Gm.part, slot)
    bv = np.zeros(len(Gm.V), bool)
    bv[np.intersect1d(np.unique(Gm.F[ins]), np.unique(Gm.F[~ins]))] = True
    phi = np.where(np.abs(phi) < 1e-7, 1e-7, phi)
    # a part-loop edge is never cut (the neighbouring piece shares it): its two ends take the sign of the more decided one
    for x, y in np.unique(np.sort(np.concatenate([Gm.F[ins][:, [0, 1]], Gm.F[ins][:, [1, 2]], Gm.F[ins][:, [2, 0]]]), axis=1), axis=0):
        if bv[x] and bv[y] and (phi[x] > 0) != (phi[y] > 0):
            if abs(phi[x]) >= abs(phi[y]):
                phi[y] = np.sign(phi[x]) * 1e-6
            else:
                phi[x] = np.sign(phi[y]) * 1e-6
    n0 = len(Gm.V)
    Gm, phi = iso_cut(Gm, phi, ins)
    ins = np.isin(Gm.part, slot)
    bv = np.r_[bv, np.zeros(len(Gm.V) - n0, bool)]
    R = ins & (phi[Gm.F].sum(1) > 0)
    return Gm, R, bv, splits, len(Gm.V) - n0


def game_region(m, body, slot, game_only=None, keepg=None):
    """The game faces of the slot lying under the module's skin (closest module face is skin), the triangles straddling the boundary split.
    Returns (refined game Mesh, region mask, border vertex mask: on the slot's part loops, never moved, number of edges split)."""
    gF = m.F[m.role != SKIN]
    oc = outfit_closest(m)
    S = m.role == SKIN
    splits = 0
    if REGION_TEST == 'iso' and keepg is not None:
        Gm, R, bv, splits, ncut = game_region_iso(m, body, slot, game_only, keepg)
        ins = np.isin(Gm.part, slot)
        cnt, cfor, gro = under_counts(Gm, oc, S, m, game_only)
        Gm_iso = (Gm, R, bv)
    else:
        Gm_iso = None
        Gm = base_mesh(body).copy()
    for it in range(0 if Gm_iso else REFINE_ITERS + 1):
        ins = np.isin(Gm.part, slot)
        bv = np.zeros(len(Gm.V), bool)
        bv[np.intersect1d(np.unique(Gm.F[ins]), np.unique(Gm.F[~ins]))] = True
        cnt, cfor, gro = under_counts(Gm, oc, S, m, game_only)
        if REGION_TEST == 'rays':
            cnt = 4 - covered_count(Gm, m.V, gF)
        if it == REFINE_ITERS:
            break
        mixed = (cnt > 0) & (cnt < 4) & ins
        if not mixed.any():
            break
        E = np.unique(np.sort(np.concatenate([Gm.F[mixed][:, [0, 1]], Gm.F[mixed][:, [1, 2]], Gm.F[mixed][:, [2, 0]]]), axis=1), axis=0)
        L = np.linalg.norm(Gm.V[E[:, 0]] - Gm.V[E[:, 1]], axis=1)
        mark = {tuple(map(int, e)) for e, l_ in zip(E, L) if l_ > REFINE_EDGE_MM / 1000 and not (bv[e[0]] and bv[e[1]])}
        if not mark:
            break
        V2, F2, par = split_edges(Gm.V, Gm.F, mark)
        Gm = Mesh(V2, F2, Gm.role[par], Gm.part[par], Gm.prov[par])
        splits += len(mark)
    if Gm_iso is None:
        R = (cnt >= (2 if USE_JOINS else 1)) & ins
    elif POKE_DROP and keepg is not None and keepg.any():
        # skin kept under a garment (the generator's arm under a gauntlet) that lies within POKE_CLEAR_MM above it, or below it, would show through
        # the low-poly garment: those faces go (the garment is right there)
        Rs = np.flatnonzero(R)
        sub = Mesh(Gm.V, Gm.F[Rs], Gm.role[Rs], Gm.part[Rs], Gm.prov[Rs])
        close = covered_count(sub, m.V, m.F[keepg], cover_in=(_DENS['iso_in'] or ISO_IN_MM / 1000) * 1000, cover_out=POKE_CLEAR_MM)
        R[Rs[close >= 2]] = False
        Gm.poke_dropped = int((close >= 2).sum())
    R = fill_holes(Gm, R) & ins
    pairs = face_adjacency(Gm.F, R)
    lab = components(len(Gm.F), pairs, R)
    area = Gm.tri_area()
    for c in np.unique(lab[lab >= 0]):
        f = lab == c
        if area[f].sum() * 1e4 < MIN_GAME_CM2:
            R &= ~f
    # grow: slot faces next to the game skin or to the slot's border (the game head / hands) that no garment covers along their normal at all
    # (the gap between a small game head and a wide neckline) join it; isolated uncovered faces (hollows under a loose garment) do not
    grown = 0
    if GROW_EXPOSED:
        cov = covered_count(Gm, m.V, m.F[m.role != SKIN], cover_in=GROW_COVER_IN_MM)
        nearb = np.zeros(len(Gm.F), bool)
        if bv.any():
            from scipy.spatial import cKDTree
            dvb = cKDTree(Gm.V[bv]).query(Gm.V)[0]
            nearb = (dvb[Gm.F] <= GROW_NEAR_MM / 1000).all(1)
        open_ = ins & (cov == 0) & ~R & nearb
        if open_.any():
            seed = R | (ins & np.isin(Gm.F, np.flatnonzero(bv)).any(1) & open_)
            grow = seed | open_
            pairs = face_adjacency(Gm.F, grow)
            lab = components(len(Gm.F), pairs, grow)
            keep_l = np.unique(lab[seed & (lab >= 0)])
            add = open_ & np.isin(lab, keep_l)
            grown = int(add.sum())
            R = R | add
    neck = [i for i, n_ in enumerate(body.part_names) if n_.startswith('neck')]
    hf = S & np.isin(m.part, game_only if game_only is not None else [])
    hid = covered_count(Gm, m.V, m.F[hf], cover_in=0., cover_out=HIDDEN_OUT_MM) if hf.any() else np.zeros(len(Gm.F), int)
    patch = R & (hid >= 2) & ~np.isin(Gm.part, neck)
    Gm.grown = grown
    return Gm, R, bv, splits, patch, gro


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


def true_runs(flags):
    """Maximal runs of True in a cyclic boolean array, in order. Returns (runs, whole_loop)."""
    n = len(flags)
    if flags.all():
        return [np.arange(n)], True
    start = int(np.flatnonzero(~flags)[0])
    runs, cur = [], []
    for k in np.r_[np.arange(start + 1, n), np.arange(0, start + 1)]:
        if flags[k]:
            cur.append(int(k))
        elif cur:
            runs.append(np.array(cur))
            cur = []
    if cur:
        runs.append(np.array(cur))
    return runs, False


def strip(A, pa, B, pb):
    """Triangles between two chains running the same way along one line: A (garment edge vertices, in the garment's boundary direction) at
    positions pa, B (game edge vertices) at positions pb; both non-decreasing."""
    tris, i, j = [], 0, 0
    while i < len(A) - 1 or j < len(B) - 1:
        if j >= len(B) - 1 or (i < len(A) - 1 and pa[i + 1] <= pb[j + 1]):
            tris.append((A[i + 1], A[i], B[j]))
            i += 1
        else:
            tris.append((B[j], B[j + 1], A[i]))
            j += 1
    return tris


def process_module(m, body, slot, game_only=None):
    """The module's garment (whole), the game skin of its slot that its skin covered, the joins between them and lips on the free garment edges.
    Returns (Mesh, notes)."""
    slot = np.asarray(slot)
    _DENS['iso_in'] = module_iso_in(m, body) if DENSITY_AWARE and 'iso' in DENS_PARTS else None
    _DENS['ref'] = edge_stats(m.V, m.F[m.role != SKIN])[1] if DENSITY_AWARE and 'refine' in DENS_PARTS and (m.role != SKIN).any() else None
    keepg, dropped = kept_garment(m, slot, game_only, body)
    Gm, R, bv, splits, patch, gro = game_region(m, body, slot, game_only, keepg)
    garment = m.subset(keepg)
    V = m.V.copy()
    nV = len(V)
    notes = {'garment_faces': int(keepg.sum()), 'garment_dropped_cm2': round(dropped, 1), 'game_skin_faces': int(R.sum()), 'game_edges_split': splits}
    hl = [np.array(x) for x in splice_loops(boundary_loops(garment))] if keepg.any() else []
    hl = [h for h in hl if len(h) >= 3 and np.linalg.norm(V[np.roll(h, -1)] - V[h], axis=1).sum() > 1e-3]   # degenerate edge loops are no edges
    nb = np.cross(Gm.V[Gm.F[:, 1]] - Gm.V[Gm.F[:, 0]], Gm.V[Gm.F[:, 2]] - Gm.V[Gm.F[:, 0]])
    ext = 0
    V0j = Gm.V.copy()
    move_lim = np.full(len(Gm.V), MOVE_MAX_MM / 1000)
    snap_lim = np.full(len(V), SNAP_MM / 1000)
    if DENSITY_AWARE and 'move' in DENS_PARTS:                    # (e) limits from the local triangle size
        move_lim = np.clip(DENS_MOVE_K * edge_stats(Gm.V, Gm.F)[0], 0.020, 0.090)
        if DENS_MOVE_MODE == 'up':                               # only ever more than MOVE_MAX_MM (coarse meshes)
            move_lim = np.maximum(move_lim, MOVE_MAX_MM / 1000)
        elif DENS_MOVE_MODE == 'down':                           # only ever less (fine meshes)
            move_lim = np.minimum(move_lim, MOVE_MAX_MM / 1000)
    if DENSITY_AWARE and 'snap' in DENS_PARTS and len(garment.F):
        snap_lim = np.clip(DENS_SNAP_K * edge_stats(V, garment.F)[0], 0.010, 0.060)
    excl = np.zeros(len(Gm.V), bool)                         # (a) game edge vertices left out of the joins because joining them folded skin
    for attempt in range(FOLD_SAFE_PASSES + 1 if FOLD_SAFE_JOINS else 1):
        Gm.V = V0j.copy()
        joined, moved, strips, move_mm, runs_done, skipped = set(), [], [], [], 0, []
        if USE_JOINS and hl and R.any():
            HA = np.concatenate([V[h] for h in hl])
            HB = np.concatenate([V[np.roll(h, -1)] for h in hl])
            hloop = np.concatenate([[li] * len(h) for li, h in enumerate(hl)])
            seglen = np.linalg.norm(HB - HA, axis=1)
            cum = np.zeros(len(HA))
            loop_len = np.zeros(len(hl))
            for li in range(len(hl)):
                s_ = np.flatnonzero(hloop == li)
                cum[s_] = np.r_[0., np.cumsum(seglen[s_])[:-1]]
                loop_len[li] = seglen[s_].sum()
            for L in splice_loops(boundary_loops(Gm.subset(R))):
                L = np.array(L)
                free = ~bv[L]
                if not free.any():
                    continue
                Q, d, seg = seg_closest(Gm.V[L], HA, HB)
                mq = body.measure(Q)
                ok = free & ~excl[L] & (d <= move_lim[L]) & np.isin(mq['part'], slot) & (mq['d'] <= LIFT_MAX_MM / 1000) & (mq['d'] >= -SINK_MAX_MM / 1000)
                runs, whole_loop = true_runs(ok)
                for run in runs:
                    pieces, cur = [], [run[0]]
                    for a_, b_ in zip(run[:-1], run[1:]):
                        if hloop[seg[b_]] != hloop[seg[a_]]:
                            pieces.append(np.array(cur))
                            cur = []
                        cur.append(b_)
                    pieces.append(np.array(cur))
                    work = [(pc, whole_loop and len(pieces) == 1) for pc in pieces]
                    while work:
                        pc, whole = work.pop(0)
                        if len(pc) < 2:
                            continue
                        pc = pc[::-1]                                         # game edge reversed: runs the same way as the garment edge
                        li = int(hloop[seg[pc[0]]])
                        s_ = np.flatnonzero(hloop == li)
                        h, Lh = hl[li], loop_len[li]
                        gp = (cum[seg[pc]] + np.linalg.norm(Q[pc] - HA[seg[pc]], axis=1)) / Lh
                        gp = np.unwrap(gp * 2 * np.pi) / (2 * np.pi)
                        st = np.diff(gp)
                        if (st < -1e-9).sum() > (st > 1e-9).sum():
                            skipped.append('runs backwards along the garment edge')
                            continue
                        tg = pav(gp)
                        thv = cum[s_] / Lh
                        u = (tg % 1.) * Lh
                        k = np.clip(np.searchsorted(cum[s_], u, side='right') - 1, 0, len(h) - 1)
                        f_ = np.clip((u - cum[s_][k]) / np.maximum(seglen[s_][k], 1e-12), 0., 1.)
                        Qm = HA[s_][k] + f_[:, None] * (HB[s_][k] - HA[s_][k])
                        dd_ = ((tg[:, None] - thv[None, :] + .5) % 1.) - .5
                        kk = np.abs(dd_).argmin(1)
                        near_v = np.linalg.norm(V[h[kk]] - Qm, axis=1) <= snap_lim[h[kk]]
                        tg = np.where(near_v, tg - dd_[np.arange(len(tg)), kk], tg)
                        Qm = np.where(near_v[:, None], V[h[kk]], Qm)
                        far = np.linalg.norm(Qm - Gm.V[L[pc]], axis=1) > move_lim[L[pc]]     # ordering along the edge pulled it too far: split there
                        if far.any():
                            sub, cur = [], []
                            for kq in range(len(pc)):
                                if far[kq]:
                                    if cur:
                                        sub.append(np.array(cur)[::-1])
                                    cur = []
                                else:
                                    cur.append(pc[kq])
                            if cur:
                                sub.append(np.array(cur)[::-1])
                            work += [(x, False) for x in sub if len(x) >= 2]
                            skipped.append('split at a far move')
                            continue
                        gids = L[pc]
                        B = list(nV + gids)
                        if whole:                                              # the whole loop: close it
                            B.append(B[0])
                            tg = np.r_[tg, tg[0] + 1.]
                        t0, t1 = tg[0], tg[-1]
                        cand = sorted((p + kt, j) for kt in range(int(np.floor(t0)) - 1, int(np.floor(t1)) + 2) for j, p in enumerate(thv))
                        ps = np.array([c[0] for c in cand])
                        i0 = int(np.flatnonzero(ps <= t0 + 1e-9)[-1])
                        i1 = i0 + len(h) if whole else int(np.flatnonzero(ps >= t1 - 1e-9)[0])
                        chain = cand[i0:i1 + 1]
                        A = [int(h[j]) for _, j in chain]
                        pa = [p for p, _ in chain]
                        # each garment edge is joined by one run only: trim edges another run already took off the ends; skip if one is inside
                        while len(A) > 1 and (A[0], A[1]) in joined:
                            A, pa = A[1:], pa[1:]
                        while len(A) > 1 and (A[-2], A[-1]) in joined:
                            A, pa = A[:-1], pa[:-1]
                        if any((a_, b_) in joined for a_, b_ in zip(A[:-1], A[1:])):
                            skipped.append('overlaps another join')
                            continue
                        for a_, b_ in zip(A[:-1], A[1:]):
                            joined.add((a_, b_))
                        strips += strip(A, np.array(pa), B, tg)
                        move_mm += list(np.linalg.norm(Qm - Gm.V[gids], axis=1) * 1000)
                        Gm.V[gids] = Qm
                        moved += list(gids)
                        runs_done += 1
        if moved:
            relax_folds(Gm, nb, np.array(moved), np.flatnonzero(bv), body)
        if not FOLD_SAFE_JOINS or not moved:
            break
        na = np.cross(Gm.V[Gm.F[:, 1]] - Gm.V[Gm.F[:, 0]], Gm.V[Gm.F[:, 2]] - Gm.V[Gm.F[:, 0]])
        bad = ((nb * na).sum(1) < 0) & R
        if not bad.any():
            break
        near = np.zeros(len(Gm.V), bool)
        near[np.unique(Gm.F[bad])] = True
        cand = np.unique(Gm.F[near[Gm.F].any(1)])             # moved vertices on a folded face or on a face touching one
        mvd = np.zeros(len(Gm.V), bool)
        mvd[np.array(moved, np.int64)] = True
        cand = cand[mvd[cand] & ~excl[cand]]
        if not len(cand):
            break
        excl[cand] = True
    notes['joins_left_out_for_folds'] = int(excl.sum())
    # border joins: where the slot side of its border loop is garment-covered (no game skin there), a free garment edge close to the loop is moved
    # onto it and joined to the loop's own vertices (the neighbouring piece, e.g. the game head, is not touched)
    bj_runs, bj_mm = 0, []
    if hl:
        ins = np.isin(Gm.part, slot)
        Gs_ = Gm.subset(ins)
        face_of = {}
        for f in np.flatnonzero(ins):
            a0, b0, c0 = (int(x) for x in Gm.F[f])
            for x, y in ((a0, b0), (b0, c0), (c0, a0)):
                face_of[(x, y)] = f
        hset = [set(h.tolist()) for h in hl]
        for BL in splice_loops(boundary_loops(Gs_)):
            BL = np.array(BL)
            if not bv[BL].all():
                continue                                          # not a border loop
            nb_parts = np.unique(Gm.part[~ins & np.isin(Gm.F, BL).any(1)])
            if game_only is None or not np.isin(nb_parts, game_only).any():
                continue                                          # only loops against a game-owned piece (head, hands); a knee / elbow loop borders another module
            unc = np.array([not R[face_of.get((int(x), int(y)), 0)] for x, y in zip(BL, np.roll(BL, -1))])   # edge k: BL[k] -> BL[k+1]
            if not unc.any():
                continue
            fe = [(a_, b_) for h in hl for a_, b_ in zip(h.tolist(), np.roll(h, -1).tolist()) if (a_, b_) not in joined]
            if not fe:
                break
            FA, FB = V[[e[0] for e in fe]], V[[e[1] for e in fe]]
            runs, whole_loop = true_runs(unc)
            for run in runs:
                chain = np.r_[BL, BL[:1]] if whole_loop else BL[np.r_[run, (run[-1] + 1) % len(BL)]]
                Q, d, seg = seg_closest(Gm.V[chain], FA, FB)
                if np.median(d) > BORDER_JOIN_MM / 1000:
                    continue
                li = int(np.bincount([next(i for i, hs in enumerate(hset) if fe[sg][0] in hs) for sg in seg]).argmax())
                h = hl[li]
                Lc = Gm.V[chain]
                cA, cB = Lc[:-1], Lc[1:]
                Qh, dh, sh = seg_closest(V[h], cA, cB)
                near = dh <= BORDER_JOIN_MM / 1000
                if near.sum() < 2:
                    continue
                hr, hwhole = true_runs(near)
                hrun = max(hr, key=len)
                idx = np.r_[np.arange(len(h)), 0] if hwhole else hrun
                A = h[idx]
                if np.median(body.measure(V[A])['d']) > LIFT_MAX_MM / 1000:
                    continue                                   # an edge standing off the body (a flared boot top) keeps its shape: no join
                if any((int(x), int(y)) in joined for x, y in zip(A[:-1], A[1:])):
                    continue
                clen = np.r_[0., np.cumsum(np.linalg.norm(cB - cA, axis=1))]
                ppos = clen[sh[idx]] + np.linalg.norm(Qh[idx] - cA[sh[idx]], axis=1)
                pa = ppos                                              # garment edge and slot border both run with the slot below them: same way
                if (np.diff(pa) < -1e-9).sum() > (np.diff(pa) > 1e-9).sum():
                    continue
                pa = pav(pa)
                seg_l = np.diff(clen)
                lo_k = max(0, int(np.searchsorted(clen, pa[0] - 1e-9, side='right')) - 1)
                hi_k = min(len(chain) - 1, int(np.searchsorted(clen, pa[-1] + 1e-9, side='left')))
                if hi_k - lo_k < 1:
                    continue
                Bc = chain[lo_k:hi_k + 1]
                pb = clen[lo_k:hi_k + 1]
                # move the garment edge onto the loop; the garment follows over CLEAR_RAMP_MM, joined vertices stay
                Dm = np.zeros_like(V)
                Dm[A] = Qh[idx] - V[A]
                bj_mm += list(np.linalg.norm(Dm[A], axis=1) * 1000)
                ge = np.unique(np.sort(np.concatenate([garment.F[:, [0, 1]], garment.F[:, [1, 2]], garment.F[:, [2, 0]]]), axis=1), axis=0)
                w = np.linalg.norm(V[ge[:, 0]] - V[ge[:, 1]], axis=1) + 1e-9
                Gg = sparse.coo_matrix((np.r_[w, w], (np.r_[ge[:, 0], ge[:, 1]], np.r_[ge[:, 1], ge[:, 0]])), shape=(nV, nV)).tocsr()
                dist, _, src = csgraph.dijkstra(Gg, directed=False, indices=np.unique(A), min_only=True, return_predecessors=True)
                t_ = np.clip(np.where(np.isfinite(dist), dist, np.inf) / (CLEAR_RAMP_MM / 1000), 0., 1.)
                fall = 1. - t_ * t_ * (3. - 2. * t_)
                okv = (src >= 0) & (fall > 0)
                okv[np.unique(A)] = False
                for e_ in joined:
                    okv[list(e_)] = False
                Dm[okv] = fall[okv, None] * Dm[src[okv]]
                V += Dm
                for x, y in zip(A[:-1], A[1:]):
                    joined.add((int(x), int(y)))
                strips += strip([int(x) for x in A], pa, list(nV + Bc), pb)
                bj_runs += 1
    # free garment edges: overhangs, pushed clear of the game body and lipped under its skin
    free_edges = [(int(a_), int(b_)) for h in hl for a_, b_ in zip(h, np.roll(h, -1)) if (int(a_), int(b_)) not in joined]
    if free_edges:                                                # the game skin runs on under the free (overhanging) edges, where the garment is clearly above it
        EA = V[[e[0] for e in free_edges]]
        EB = V[[e[1] for e in free_edges]]
        ins = np.isin(Gm.part, slot)
        cand = np.flatnonzero(ins & ~R)
        if len(cand):
            T = Gm.V[Gm.F[cand]]
            P = np.concatenate([T.mean(1), T[:, 0], T[:, 1], T[:, 2]])
            dmin = np.full(len(P), np.inf)
            for k0 in range(0, len(EA), 256):
                dmin = np.minimum(dmin, seg_closest(P, EA[k0:k0 + 256], EB[k0:k0 + 256])[1])
            dface = dmin.reshape(4, -1).min(0)
            emax = np.max(np.stack([np.linalg.norm(T[:, i] - T[:, (i + 1) % 3], axis=1) for i in range(3)], 1), 1)
            near = (dface <= EXT_MM / 1000) & (emax <= EXT_FACE_MM / 1000)
            if near.any():                                   # the garment must lie over all of the face, its corners included (a big base triangle
                ni = np.flatnonzero(near)                    # sticking out past the garment edge fails at a corner): 4 inner samples + 3 corners
                sub = Mesh(Gm.V, Gm.F[cand[ni]], Gm.role[cand[ni]], Gm.part[cand[ni]], Gm.prov[cand[ni]])
                cov = covered_count(sub, V, garment.F, cover_in=0., cover_out=COVER_OUT_MM)
                Tn = Gm.V[Gm.F[cand[ni]]]
                cn = Tn.mean(1)
                if DEBUG_EXT is not None and isinstance(DEBUG_EXT, list):
                    DEBUG_EXT.append({'inner_cov': np.bincount(cov, minlength=5).tolist()})
                for k_ in (range(3) if REGION_TEST != 'iso' else ()):   # corner samples (not needed after the exact cut: no face reaches past an edge)
                    Tk = Tn.copy()
                    Tk[:, :] = cn[:, None] + .9 * (Tn[:, [k_]] - cn[:, None]) + .02 * (Tn - Tn[:, [k_]])
                    subk = Mesh(np.concatenate([Gm.V, Tk.reshape(-1, 3)]), (len(Gm.V) + np.arange(3 * len(ni))).reshape(-1, 3), sub.role, sub.part, sub.prov)
                    cov = np.minimum(cov, 4 * (covered_count(subk, V, garment.F, cover_in=0., cover_out=COVER_OUT_MM) >= 3))
                near[ni[cov < 4]] = False
            close = dface <= EXT_NEAR_MM / 1000                      # right next to the edge the skin always runs on (the edge is pushed clear)
            # further in, only where the closest garment point lies outside (along the game normal) at least CLEAR_MM away, for every sample
            gb = bf.Body(V, garment.F, np.arange(len(garment.F)), []) if len(garment.F) else None
            if gb is not None and near.any() and not OPEN_CLEAR:     # (with OPEN_CLEAR the garment is pushed clear of the kept skin instead)
                Pn = P.reshape(4, -1, 3)[:, near].reshape(-1, 3)
                dd, tri, bary = gb.closest(Pn)
                cp = bary[:, :1] * gb.A[tri] + bary[:, 1:2] * gb.B[tri] + bary[:, 2:] * gb.C[tri]
                nrm = np.cross(T[near][:, 1] - T[near][:, 0], T[near][:, 2] - T[near][:, 0])
                nrm /= np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-12
                above = (((cp - Pn).reshape(4, -1, 3) * nrm[None]).sum(2) >= (EXT_CLEAR_MM if not OPEN_CLEAR else -1.) / 1000).all(0)
                near_idx = np.flatnonzero(near)
                if DEBUG_EXT is not None and isinstance(DEBUG_EXT, list):
                    cz_ = T[near].mean(1); sb_ = (cz_[:, 2] > 0.3) & (cz_[:, 2] < 0.47) & (cz_[:, 1] > 0.03) & (cz_[:, 0] < 0)
                    DEBUG_EXT.append({'back_after_cov': int(sb_.sum()), 'back_above': int((sb_ & above).sum()),
                                      'along_mm': np.round(((cp - Pn).reshape(4, -1, 3) * nrm[None]).sum(2)[:, sb_] * 1000).astype(int).T.tolist()})
                near[near_idx[~above]] = False
                near |= close
            if near.any() and len(garment.F) and not OPEN_CLEAR:   # and nowhere on it is the garment closer than EXT_CLEAR_MM (or below it)
                ni = np.flatnonzero(near)
                sub = Mesh(Gm.V, Gm.F[cand[ni]], Gm.role[cand[ni]], Gm.part[cand[ni]], Gm.prov[cand[ni]])
                tight = covered_count(sub, V, garment.F, cover_in=(_DENS['iso_in'] or ISO_IN_MM / 1000) * 1000, cover_out=EXT_CLEAR_MM)
                near[ni[tight >= 1]] = False
            if DEBUG_EXT:
                cz = T.mean(1)
                sel = (cz[:, 2] > 0.3) & (cz[:, 2] < 0.47) & (cz[:, 1] > 0.03) & (cz[:, 0] < 0)
                DEBUG_EXT.append({'n_back': int(sel.sum()), 'within': int((sel & (dface <= EXT_MM / 1000)).sum()), 'kept': int((sel & near).sum()),
                                  'dface_mm': np.round(dface[sel] * 1000).astype(int).tolist()})
            R[cand[near]] = True
            ext = int(near.sum())
            ext_faces = cand[near]
    jv = {v for e in joined for v in e}
    pushed, lipV, lipF, lipR = 0, np.zeros((0, 3)), [], []
    if 'ext_faces' not in locals():
        ext_faces = np.zeros(0, np.int64)
    if free_edges:
        ev = np.unique(np.array(free_edges).ravel())
        ge = np.unique(np.sort(np.concatenate([garment.F[:, [0, 1]], garment.F[:, [1, 2]], garment.F[:, [2, 0]]]), axis=1), axis=0)
        w = np.linalg.norm(V[ge[:, 0]] - V[ge[:, 1]], axis=1) + 1e-9
        Gg = sparse.coo_matrix((np.r_[w, w], (np.r_[ge[:, 0], ge[:, 1]], np.r_[ge[:, 1], ge[:, 0]])), shape=(nV, nV)).tocsr()
        dist = csgraph.dijkstra(Gg, directed=False, indices=ev, min_only=True)
        reach = (EXT_MM + CLEAR_RAMP_MM) / 1000
        cv = np.array([v for v in np.flatnonzero(np.isfinite(dist) & (dist <= reach)) if v not in jv], np.int64)
        over = np.zeros(nV, bool)
        if OPEN_CLEAR and len(ext_faces) and len(garment.F):
            # garment lying over the game skin kept under it (a large shin triangle reaches far below the boot top): pushed clear at full strength
            gvx = np.array([v for v in np.unique(garment.F) if v not in jv], np.int64)
            eb = bf.Body(Gm.V, Gm.F[ext_faces], np.arange(len(ext_faces)), [])
            d_ext = eb.closest(V[gvx])[0]
            d_all = np.abs(body.measure(V[gvx])['d'])
            over[gvx[(d_ext <= d_all + 0.002) & (d_ext <= OPEN_PUSH_MAX_MM / 1000 + CLEAR_MM / 1000)]] = True
            cv = np.union1d(cv, np.flatnonzero(over))
        if len(cv):
            ms = body.measure(V[cv])
            fall = 1. - np.clip((np.where(np.isfinite(dist[cv]), dist[cv], np.inf) - EXT_MM / 1000) / (CLEAR_RAMP_MM / 1000), 0., 1.) ** 2
            fall = np.where(over[cv], 1., fall)
            clr = np.full(nV, CLEAR_MM / 1000)
            if DENSITY_AWARE and 'clear' in DENS_PARTS and len(garment.F):
                clr[np.unique(garment.F)] = facet_clearance(V, garment.F, np.unique(garment.F))
            need = np.maximum(clr[cv] - ms['d'], 0.)
            if OPEN_CLEAR:
                # measured on the garment's faces too (centres and edge midpoints), so a flat low-poly facet cannot cut into the kept body between
                # its vertices: each vertex takes the largest need of the samples of its faces (limbs only; the torso / neck keep the vertex rule)
                limb = np.isin(ms['part'], [i for i, n_ in enumerate(body.part_names) if n_.split('_')[0] in ('forearm', 'arm', 'hand', 'leg', 'thigh', 'foot')])
                gfi = np.flatnonzero(np.isin(garment.F, cv).any(1))
                if len(gfi):
                    T = V[garment.F[gfi]]
                    samp = np.concatenate([T.mean(1), (T[:, 0] + T[:, 1]) / 2, (T[:, 1] + T[:, 2]) / 2, (T[:, 2] + T[:, 0]) / 2])
                    ds = body.measure(samp)['d'].reshape(4, -1).min(0)
                    fneed = np.minimum(np.maximum(clr[garment.F[gfi]].max(1) - ds, 0.), OPEN_PUSH_MAX_MM / 1000)
                    vneed = np.zeros(nV)
                    for k_ in range(3):
                        np.maximum.at(vneed, garment.F[gfi, k_], fneed)
                    need = np.where(limb, np.maximum(need, vneed[cv]), need)
            amt = np.minimum(need, OPEN_PUSH_MAX_MM / 1000) * fall
            V[cv] += ms['n'] * amt[:, None]
            pushed = int((amt > 1e-5).sum())
        ms = body.measure(V[ev])
        vec = -ms['n'] * (ms['d'] + LIP_DEPTH_MM / 1000)[:, None]
        ln = np.linalg.norm(vec, axis=1, keepdims=True)
        cap = (LIP_CLOSE_MM + LIP_DEPTH_MM) / 1000                  # an edge up to LIP_CLOSE_MM off the body is closed all the way to under the skin
        vec = np.where(ln > cap, vec * cap / np.maximum(ln, 1e-12), vec)
        lipV = V[ev] + vec
        lid = {int(v): nV + len(Gm.V) + i for i, v in enumerate(ev)}
        own = {}
        for f, (a, b, c) in enumerate(garment.F):
            for x, y in ((a, b), (b, c), (c, a)):
                own[(int(x), int(y))] = str(garment.role[f])
        far_off = {int(v) for v, dd_ in zip(ev, ms['d']) if dd_ > LIP_CLOSE_MM / 1000}
        on_piece = {int(v) for v, pp in zip(ev, ms['part']) if game_only is not None and pp in set(np.asarray(game_only).tolist())}
        for a_, b_ in free_edges:
            if a_ in jv and b_ in jv:
                continue                                   # between two joined runs: a short free edge, closed by the join's end triangles
            if a_ in on_piece or b_ in on_piece:
                continue                                   # over a game-owned piece (a cuff over a bigger game hand): a lip would poke out of it
            if a_ in far_off or b_ in far_off:
                continue                                   # an edge standing well off the body (a flared boot top): open, as the generator made it
            lipF += [(b_, a_, lid[a_]), (b_, lid[a_], lid[b_])]
            lipR += [own.get((a_, b_), 'primary')] * 2
    # game skin kept under a garment edge: a corner of a big game triangle can poke through the middle of a garment facet. Each vertex used only by
    # those hidden faces is checked with a ray from SINK_FROM_MM inside the body out along its normal: if the first garment surface is nearer than
    # the vertex + CLEAR_MM, the vertex is pulled back inside it (hidden skin moves, the garment keeps its generated shape)
    sunk = 0
    if OPEN_CLEAR and R.any() and len(garment.F):
        # every kept game vertex may move, except: on the slot border (shared with the game head / hands pieces), in a strip, or joined onto a
        # garment edge (an R boundary vertex sitting on a garment vertex)
        from scipy.spatial import cKDTree
        fixed = set(np.flatnonzero(bv).tolist()) | {int(x) - nV for x in np.array(strips, np.int64).ravel() if nV <= x < nV + len(Gm.V)}
        Fr = Gm.F[R]
        er = np.sort(np.concatenate([Fr[:, [0, 1]], Fr[:, [1, 2]], Fr[:, [2, 0]]]), axis=1)
        ue, uc = np.unique(er, axis=0, return_counts=True)
        rb = np.unique(ue[uc == 1])
        if len(rb):
            dj, _ = cKDTree(V[np.unique(garment.F)]).query(Gm.V[rb])
            fixed |= set(rb[dj < 0.002].tolist())
        iv = np.array([v for v in np.unique(Fr) if v not in fixed], np.int64)
        if len(iv):
            vn = vertex_normals(Gm.V, Gm.F[R])[iv]
            o = Gm.V[iv] - vn * (SINK_FROM_MM / 1000)
            T = V[garment.F]
            t = ray_first_hit(o, vn, T[:, 0], T[:, 1], T[:, 2])
            lim = (SINK_FROM_MM + CLEAR_MM) / 1000             # the vertex must end up CLEAR_MM inside the first garment surface
            mv = np.isfinite(t) & (t < lim) & (t > 1e-4)
            if mv.any():
                Gm.V = Gm.V.copy()
                tri_n = lambda VV: np.cross(VV[Fr[:, 1]] - VV[Fr[:, 0]], VV[Fr[:, 2]] - VV[Fr[:, 0]])  # noqa: E731
                pre = tri_n(Gm.V)
                old = Gm.V[iv[mv]].copy()
                Gm.V[iv[mv]] = o[mv] + vn[mv] * (t[mv] - CLEAR_MM / 1000)[:, None]
                moved_ids = iv[mv]
                if SINK_FOLD_GUARD:
                    back = np.zeros(len(moved_ids), bool)
                    cosl = np.cos(np.radians(FOLD_DEG))
                    for _ in range(6):
                        post = tri_n(Gm.V)
                        cs = (pre * post).sum(1) / np.maximum(np.linalg.norm(pre, axis=1) * np.linalg.norm(post, axis=1), 1e-18)
                        bad = cs < cosl
                        if not bad.any():
                            break
                        hit = np.isin(moved_ids, np.unique(Fr[bad])) & ~back
                        if not hit.any():
                            break
                        Gm.V[moved_ids[hit]] = old[hit]
                        back |= hit
                    notes['sink_reverted_for_folds'] = int(back.sum())
                    mv_n = int((~back).sum())
                else:
                    mv_n = int(mv.sum())
                sunk = mv_n
    if len(nb) == len(Gm.F) and R.any():
        na = np.cross(Gm.V[Gm.F[:, 1]] - Gm.V[Gm.F[:, 0]], Gm.V[Gm.F[:, 2]] - Gm.V[Gm.F[:, 0]])
        notes['folded_game_faces'] = int(((nb * na).sum(1) < 0)[R].sum())
    # assemble: garment faces (vertex ids into V), strips, game region (offset nV), lips (offset nV + len(Gm.V))
    Rf = Gm.F[R] + nV
    sF = np.array(strips, np.int64).reshape(-1, 3)
    lF = np.array(lipF, np.int64).reshape(-1, 3)
    out = Mesh(np.concatenate([V, Gm.V, lipV]), np.concatenate([garment.F, sF, Rf, lF]),
               np.r_[garment.role, [SKIN] * len(sF), np.where(patch[R], gro[R], SKIN), lipR],
               np.r_[garment.part, [-1] * len(sF), Gm.part[R], [-1] * len(lF)],
               np.r_[garment.prov, ['stitch'] * len(sF), np.where(patch[R], 'patch', 'band'), ['lip'] * len(lF)])
    out.part[out.part < 0] = body.measure(out.V[out.F[out.part < 0]].mean(1))['part'] if (out.part < 0).any() else out.part[out.part < 0]
    notes.update({'hidden_skin_vertices_sunk': sunk, 'garment_tucked_under_skin_dropped': getattr(m, 'tucked', 0), 'garment_under_generator_skin_dropped': getattr(m, 'under_skin', 0), 'module_skin_hidden_faces': getattr(m, 'hidden_skin', 0), 'skin_dropped_under_close_garment': getattr(Gm, 'poke_dropped', 0), 'grown_exposed_faces': getattr(Gm, 'grown', 0), 'patch_faces': int((patch & R).sum()), 'border_joins': bj_runs, 'border_join_move_mm': round(float(np.max(bj_mm)), 1) if bj_mm else None, 'game_skin_faces': int(R.sum()), 'game_faces_under_garment_edges': ext, 'joined_runs': runs_done, 'joined_garment_edges': len(joined), 'free_garment_edges': len(free_edges), 'lip_faces': int(len(lF)),
                  'pushed_vertices': pushed, 'move_mm': [round(float(np.median(move_mm)), 1), round(float(np.max(move_mm)), 1)] if move_mm else None,
                  'runs_skipped': skipped})
    if DENSITY_AWARE:
        notes['density'] = {'iso_in_mm': round((_DENS['iso_in'] or ISO_IN_MM / 1000) * 1000, 1), 'module_median_edge_mm': round((_DENS['ref'] or 0) * 1000, 1)}
    _DENS['iso_in'] = _DENS['ref'] = None
    return out, notes


PIECE_CLEAR_MM = 4.0     # garment is pushed at least this far off the game head / hands / feet ...
PIECE_PUSH_MAX_MM = 30.0  # ... by at most this
PIECE_RAMP_MM = 40.0      # ... with this falloff along the garment


def clear_of_pieces(out, body, game_parts):
    """Garment vertices inside (or within PIECE_CLEAR_MM of) a game-only piece (the head, hands, bare feet) are pushed out along THAT piece's
    normal (capped), the garment around following over PIECE_RAMP_MM; vertices on joins stay. Measured against the piece alone, so a torso
    normal never drives it."""
    if not game_parts:
        return 0
    pf = np.isin(body.part, game_parts)
    piece = bf.Body(body.V, body.F[pf], np.flatnonzero(pf), [])           # part = the full body's face index: normals from the full (outward) body
    garm = (out.role != SKIN) & (out.prov != 'patch')
    gv = np.unique(out.F[garm])
    fixed = np.zeros(len(out.V), bool)
    fixed[np.unique(out.F[~garm])] = True                 # shared with skin / join strips / patches
    dd, tri, bary = piece.closest(out.V[gv])
    cp = bary[:, :1] * piece.A[tri] + bary[:, 1:2] * piece.B[tri] + bary[:, 2:] * piece.C[tri]
    nrm = body.tri_n[piece.part[tri]]
    sd = ((out.V[gv] - cp) * nrm).sum(1)
    need = np.clip(PIECE_CLEAR_MM / 1000 - sd, 0., PIECE_PUSH_MAX_MM / 1000) * (dd < .06)
    need[fixed[gv]] = 0.
    D = np.zeros_like(out.V)
    D[gv] = nrm * need[:, None]
    src_v = gv[need > 0]
    if not len(src_v):
        return 0
    e = np.unique(np.sort(np.concatenate([out.F[garm][:, [0, 1]], out.F[garm][:, [1, 2]], out.F[garm][:, [2, 0]]]), axis=1), axis=0)
    w = np.linalg.norm(out.V[e[:, 0]] - out.V[e[:, 1]], axis=1) + 1e-9
    G = sparse.coo_matrix((np.r_[w, w], (np.r_[e[:, 0], e[:, 1]], np.r_[e[:, 1], e[:, 0]])), shape=(len(out.V), len(out.V))).tocsr()
    dist, _, src = csgraph.dijkstra(G, directed=False, indices=src_v, min_only=True, return_predecessors=True)
    t = np.clip(np.where(np.isfinite(dist), dist, np.inf) / (PIECE_RAMP_MM / 1000), 0., 1.)
    fall = 1. - t * t * (3. - 2. * t)
    ok = (src >= 0) & (fall > 0) & ~fixed
    ok[src_v] = False
    D[ok] = fall[ok, None] * D[src[ok]]
    out.V = out.V + D
    return int(len(src_v))


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


# ----------------------------------------------------------------------------------------------- drivers

def mirrored_field(field, x0):
    """An inflation field of a module mirrored with mirror_mesh (positions and offsets reflected, faces re-wound)."""
    V, F, D = field
    V = V.copy()
    V[:, 0] = 2 * x0 - V[:, 0]
    D = D.copy()
    D[:, 0] = -D[:, 0]
    return V, F[:, [0, 2, 1]], D


def deflate_positions(out, fields):
    """Positions of `out` without the inflation (the viewer's Inflation slider): each vertex takes the inflation offset of the closest point on the
    inflated module (its garment and its own skin, so the game body under the garment shrinks with it), in full within INFL_NEAR_MM, fading to none
    at INFL_FAR_MM. The game's own pieces (head, hands, feet) never move; the deflation fades in over INFL_GAME_FADE_MM from them."""
    if not fields:
        return None, None
    from scipy.spatial import cKDTree
    P = out.V
    best = np.full(len(P), np.inf)
    disp = np.zeros_like(P)
    for Vf, Ff, Df in fields:
        if not len(Ff):
            continue
        sub = bf.Body(Vf, Ff, np.zeros(len(Ff), np.int64), ['garment'])
        d, tri, bary = sub.closest(P)
        d = np.abs(d)
        dd = bary[:, :1] * Df[Ff[tri, 0]] + bary[:, 1:2] * Df[Ff[tri, 1]] + bary[:, 2:] * Df[Ff[tri, 2]]
        upd = d < best
        best[upd] = d[upd]
        disp[upd] = dd[upd]
    w = np.clip((INFL_FAR_MM / 1000 - best) / ((INFL_FAR_MM - INFL_NEAR_MM) / 1000), 0, 1)
    gv = np.unique(out.F[out.prov == 'game'])
    if len(gv):
        dg, _ = cKDTree(P[gv]).query(P)
        t = np.clip(dg / (INFL_GAME_FADE_MM / 1000), 0, 1)
        w *= t * t * (3 - 2 * t)                  # the game's own head / hands never move; the body around them follows smoothly
    U = P - disp * w[:, None]
    mv = np.linalg.norm(U - P, axis=1)
    moved = mv > 1e-4
    return U, {'vertices_moved': int(moved.sum()), 'max_mm': round(float(mv.max()) * 1000, 1),
               'median_moved_mm': round(float(np.median(mv[moved])) * 1000, 1) if moved.any() else 0.}


def write_variant(name, key, out, U1, Ud, ghosts, rec):
    """A variant result: out/variants/<name>__<key>.glb (+ its generator ghosts, scene npz and record json); index.json is not touched."""
    import json
    from shell_slots import OUT, face_colours, glb_bytes
    vd = OUT / 'variants'
    (vd / 'scenes').mkdir(parents=True, exist_ok=True)
    stem = f'{name}__{key}'
    (vd / f'{stem}.glb').write_bytes(glb_bytes(out, U1, extra={'deflate': Ud} if Ud is not None else None))
    gh = {}
    for g, gm in ghosts.items():
        (vd / f'{stem}__{g}.glb').write_bytes(glb_bytes(gm))
        gh[g] = f'variants/{stem}__{g}.glb'
    np.savez_compressed(vd / 'scenes' / f'{stem}__a0.npz', gV=out.V.astype(np.float32), gF=out.F.astype(np.int32), gC=face_colours(out, 'role').astype(np.float32),
                        bV=np.zeros((0, 3), np.float32), bF=np.zeros((0, 3), np.int32), bC=np.zeros((0, 3), np.float32))
    rec.update({'glb': f'variants/{stem}.glb', 'ghosts': {**gh, 'base': rec['ghosts']['base']}, 'variant': key, 'code_mtime': CODE_MTIME})
    (vd / f'{stem}.json').write_text(json.dumps(rec, default=float), encoding='utf-8')


OPTION_LETTERS = {'a': 'FOLD_SAFE_JOINS', 'b': 'SYMMETRY', 'c': 'SPIKE_CLEAN', 'd': 'EDGE_REFINE', 'e': 'DENSITY_AWARE'}


def apply_variant(key):
    """key like 'a1-bsleeve_shift-c0-d1' -> module options."""
    global VARIANT, AUTO
    AUTO = False                                                     # the viewer's explicit options
    for part in key.split('-'):
        k, v = part[0], part[1:]
        name = OPTION_LETTERS[k]
        globals()[name] = v if name == 'SYMMETRY' else v == '1'
    if SYMMETRY not in ('off', 'sleeve_shift', 'sleeve_average', 'sleeve_copy', 'whole_mirror'):
        raise ValueError(f'unknown symmetry method {SYMMETRY}')
    VARIANT = key


KEPT_SKIN_SINK = True       # (2026-11-24, user: dwarf_female plate_basic "a bunch of the body and arms are showing through") a body region the
                            # imprint keeps whole (one real uncovered patch in it: the back of a thigh) keeps all its skin under the garment, and
                            # where the garment lies inside the body (dwarf armour sits 0-30 mm inside at Amount 0) that skin shows through it
KEPT_SINK_MAX_MM = 70.0     # a kept skin vertex is pulled in at most this far along its normal, to CLEAR_MM under the first garment surface
KEPT_SINK_THICK = 0.45      # ... and never past this fraction of the body's thickness there (measured along the normal to the skin opposite)
KEPT_SINK_SPREAD = 4        # rounds in which neighbours follow a sunk vertex part of the way
KEPT_SINK_GEN_MM = 25.0     # skin of the generated model within this of a vertex keeps it where it is (the design shows skin there; 80 kept most of a slim dwarf armour's skin)
KEPT_SINK_GEN_GARMENT_MM = 80.0   # ... unless another generated module has garment as its nearest surface within this


def sink_kept_skin(out, U1, generated=()):
    """Pull kept game skin (prov band) under the garment that covers it: per vertex, a ray along the vertex normal from KEPT_SINK_FROM_MM inside;
    the first garment / lip surface it meets within KEPT_SINK_MAX_MM of the vertex (or outside it, closer than CLEAR_MM) puts the vertex CLEAR_MM
    under that surface; the deeper of the two states (out.V, U1) wins and both states get the same move. Vertices of any other face (lips, game
    pieces, garment) stay; a move that turns a skin face over (> FOLD_DEG) is undone. Nothing is cut, so the mesh stays closed.
    `generated`: the generator's modules (with their skin): where the generated model itself shows skin (a V-neck, a bare midriff) nothing sinks,
    even with garment just under the skin (the sahel farmer's V-neck filled in green without this)."""
    sk = (out.role == SKIN) & (out.prov == 'band')
    occ = out.role != SKIN
    if not sk.any() or not occ.any():
        return None
    other = np.zeros(len(out.V), bool)
    other[out.F[~sk].ravel()] = True
    vs = np.setdiff1d(np.unique(out.F[sk]), np.flatnonzero(other))
    if not len(vs):
        return None
    Fs = out.F[sk]
    n = vertex_normals(out.V, Fs)[vs]
    clr = CLEAR_MM / 1000
    gen = [g for g in generated if len(g.F)]
    if gen:                    # each generated module's own surface nearest each vertex: skin there (and no module's garment) -> stays bare.
        skin_near = np.zeros(len(vs), bool)                            # per module: an armour set's gauntlet/boot module is generated on a bare
        garment_near = np.zeros(len(vs), bool)                         # body, so its torso is skin where the body armour module has garment
        for g in gen:
            dg, tg, _ = bf.Body(g.V, g.F, np.zeros(len(g.F), int), ['x']).closest(out.V[vs])
            gs = np.asarray(g.role)[tg] == SKIN
            skin_near |= gs & (dg < KEPT_SINK_GEN_MM / 1000)
            garment_near |= ~gs & (dg < KEPT_SINK_GEN_GARMENT_MM / 1000)
        keep_bare = skin_near & ~garment_near
        if keep_bare.all():
            return {'vertices': 0, 'generated_skin': int(keep_bare.sum())}
        vs, n = vs[~keep_bare], n[~keep_bare]
    S_ = out.V[out.F[out.role == SKIN]]                                # thickness: from the vertex inward to the skin opposite
    th = ray_first_hit(out.V[vs] - n * 1e-3, -n, S_[:, 0], S_[:, 1], S_[:, 2]) + 1e-3
    mx = np.minimum(KEPT_SINK_MAX_MM / 1000, KEPT_SINK_THICK * np.where(np.isfinite(th), th, 0.))
    frm = mx + clr
    d = np.zeros(len(vs))
    for X in ([out.V] if U1 is None else [out.V, U1]):
        T = X[out.F[occ]]
        o = X[vs] - n * frm[:, None]
        t = ray_first_hit(o, n, T[:, 0], T[:, 1], T[:, 2])
        ok = np.isfinite(t) & (t > frm - mx) & (t < frm + clr) & (mx > clr)
        d = np.minimum(d, np.where(ok, t - clr - frm, 0.))           # <= 0: inward along the normal
    if not (d < -1e-5).any():
        return {'vertices': 0}
    # neighbours follow a sunk vertex part of the way (a patch sinks together instead of single vertices folding their faces), never deeper
    # than their own limit allows
    pos = np.full(len(out.V), -1)
    pos[vs] = np.arange(len(vs))
    E = np.concatenate([Fs[:, [0, 1]], Fs[:, [1, 2]], Fs[:, [2, 0]]])
    E = E[(pos[E[:, 0]] >= 0) & (pos[E[:, 1]] >= 0)]
    a_, b_ = pos[E[:, 0]], pos[E[:, 1]]
    deg = np.maximum(np.bincount(np.r_[a_, b_], minlength=len(vs)), 1)
    Tv = out.V[out.F[occ]]                                             # only skin with garment over it follows (bare skin by a hem never dents)
    under = np.isfinite(ray_first_hit(out.V[vs] - n * clr, n, Tv[:, 0], Tv[:, 1], Tv[:, 2]))
    for _ in range(KEPT_SINK_SPREAD):
        avg = np.bincount(np.r_[a_, b_], np.r_[d[b_], d[a_]], len(vs)) / deg
        d = np.where(under, np.minimum(d, np.maximum(.6 * avg, -mx)), d)
    mv = d < -1e-5
    tri_n = lambda VV: np.cross(VV[Fs[:, 1]] - VV[Fs[:, 0]], VV[Fs[:, 2]] - VV[Fs[:, 0]])  # noqa: E731
    pre = tri_n(out.V)                                                 # (fold guard on the Amount-0 shape)
    delta = np.zeros_like(out.V)
    delta[vs[mv]] = n[mv] * d[mv, None]
    cosl = np.cos(np.radians(FOLD_DEG))
    for it in range(10):                                               # a move that turns a face over is halved (the last round: undone)
        post = tri_n(out.V + delta)
        cs = (pre * post).sum(1) / np.maximum(np.linalg.norm(pre, axis=1) * np.linalg.norm(post, axis=1), 1e-18)
        bad = cs < cosl
        if not bad.any():
            break
        hit = np.unique(Fs[bad])
        hit = hit[np.abs(delta[hit]).sum(1) > 0]
        if not len(hit):
            break
        delta[hit] *= 0. if it == 9 else .5
    back = np.zeros(len(out.V), bool)
    back[vs[mv]] = np.linalg.norm(delta[vs[mv]], axis=1) < np.abs(d[mv]) * .99
    out.V = out.V + delta
    if U1 is not None:
        U1 += delta
    moved = np.linalg.norm(delta, axis=1)
    return {'vertices': int((moved > 0).sum()), 'move_mm': [round(float(np.median(moved[moved > 0]) * 1000), 1), round(float(moved.max() * 1000), 1)]
            if (moved > 0).any() else None, 'reduced_for_folds': int(back.sum()), 'generated_skin': int(keep_bare.sum()) if gen else None}


def finish(name, kind, race, module, body, tripo_ghosts, pieces, info, notes, t0, game_parts=(), fields=(), modules=None):
    out = compact(concat(pieces))
    notes['garment_pushed_off_game_pieces'] = clear_of_pieces(out, body, list(game_parts))
    if SPIKE_CLEAN:                                            # (c) also the finished mesh: folded lips / skin slivers / generator slivers
        notes['folded_slivers_collapsed_in_result'] = collapse_folded_slivers(out)
    if MERGE_FLAT_SKIN:                                        # undo flat subdivision / refinement where nothing was cut or moved (lossless)
        import seal
        sk = (out.role == SKIN) & np.isin(out.prov, ['band', 'game'])
        F2, alive, rem = seal.merge_flat(out.V, out.F, sk)
        n0 = len(out.F)
        out = compact(Mesh(out.V, F2[alive], out.role[alive], out.part[alive], out.prov[alive]))
        notes['flat_skin_merged'] = {'faces_before': int(n0), 'faces_after': int(len(out.F)), 'vertices_removed': int(rem)}
    if hasattr(body, 'subdivided_from'):
        notes['game_body_subdivided'] = {'faces': body.subdivided_from[:2], 'median_edge_mm': body.subdivided_from[2], 'max_edge_mm': GAME_MAX_EDGE_MM}
    margin = fl.DEFAULTS['margin_pct'] / 100 * body.H
    U1, finfo = fill_grouped(out, body, margin)
    if KEPT_SKIN_SINK:                                         # game skin kept under a garment (its region could not go) pulled in under it
        notes['kept_skin_sunk'] = sink_kept_skin(out, U1, list(tripo_ghosts.values()))
    Ud, dinfo = deflate_positions(out, list(fields))
    if FIX_INVERTED:                                           # faces wound the wrong way round (seen from behind from outside) are turned over;
        import seal                                            # after the morphs (they are per vertex), so the winding is all that changes
        flip = seal.fix_inverted(out.V, out.F, body, always=out.prov == 'lip')   # lip strips: on visibility alone (a strip folds over)
        out.F[flip] = out.F[flip][:, ::-1]
        notes['inverted_faces_flipped'] = int(flip.sum())
    rec = {'name': name, 'kind': kind, 'race': race, 'module': module, 'glb': f'{name}.glb', 'method': 'geometry division (no planes)',
           'ghosts': {**{k: f'{name}__{k}.glb' for k in tripo_ghosts}, 'base': write_base(race, body)}, 'load': info, 'seams': notes, 'fill': finfo,
           'inflation': dinfo, 'stats': stats(out), 'seconds': round(time.monotonic() - t0, 1)}
    rec['modules'] = list(modules or [module])
    rec['code_mtime'] = CODE_MTIME
    try:                                                       # watertightness of the finished mesh (watertight.py: open edges by kind)
        import watertight as wt_
        sm = wt_.summary(wt_.scan(out.V, out.F, np.array([f'{r}:{p}' for r, p in zip(out.role, out.prov)])))
        rec['watertight'] = {'open_m': sm['open_m'], 'crack_m': sm['crack']['m'], 'gap_m': sm['gap']['m'], 'free_m': sm['free']['m'],
                             't_junction_m': sm['t-junction']['m'], 'nonmanifold_edges': sm['nonmanifold_edges']}
    except Exception as error:  # noqa: BLE001
        rec['watertight'] = {'error': f'{type(error).__name__}: {error}'}
    loads = [info] if 'labelled_faces' in info else [v for v in info.values() if isinstance(v, dict)]
    syms = [L['symmetry']['method'] for L in loads if isinstance(L.get('symmetry'), dict)]
    rec['options'] = ('automatic: ' if AUTO else '') + f"symmetry {'/'.join(syms) or 'off'}" + \
        ''.join(f', {k}' for k, on in (('fold-safe joins', FOLD_SAFE_JOINS), ('spike cleanup', SPIKE_CLEAN), ('edge refinement', EDGE_REFINE),
                                       ('density-aware', DENSITY_AWARE)) if on) + \
        (f", game body subdivided to {GAME_MAX_EDGE_MM:.0f} mm" if hasattr(body, 'subdivided_from') else '') + \
        (', imprinted joins' if IMPRINT else '')
    if VARIANT:
        write_variant(name, VARIANT, out, U1, Ud, tripo_ghosts, rec)
    else:
        write_result(name, out, U1, tripo_ghosts, rec, {'deflate': Ud} if Ud is not None else None)
    log('done', name, f'{rec["seconds"]} s', f'{len(out.F)} faces')
    return rec


def run_outfit(mod, name=None):
    mod = Path(mod).resolve()
    s, j, race, module = module_identity(mod)
    name = name or f'outfit__{race}__{s.split("_outfit_")[-1][:28]}'
    t0 = time.monotonic()
    body = game_body(race)
    rig = ab.Rig.from_npz(bf.BASE_CACHE / f'{race}_rig.npz')
    m, tripo, info = prepare_geo(mod, body, rig, race)
    game = parts(body, 'head.x', 'hand.l', 'hand.r')
    notes = {}
    for side in 'lr':                                     # a foot without footwear is the game's own foot
        fp = parts(body, f'foot.{side}')
        if fp and has_footwear(m, body, side) < 20:
            game += fp
            notes[f'foot.{side}'] = 'bare: game foot'
        elif fp:
            notes[f'foot.{side}'] = 'footwear: kept as generated'
    if notes.get('foot.l', '').startswith('bare') != notes.get('foot.r', '').startswith('bare') and parts(body, 'foot.l'):
        notes['feet'] = 'footwear on one side only: the other foot is the game foot (left-footwear mirroring not done in this mode yet)'
    slot = [p for p in all_parts(body) if p not in game]
    if IMPRINT:
        import imprint
        keepg, dropped = kept_garment(m, slot, game, body)
        out, nt = imprint.build(body, [(m.subset(keepg), slot, see_through(mod))], game)
        nt['garment_dropped_cm2'] = round(dropped, 1)
        notes['outfit'] = nt
        return finish(name, 'outfit', race, mod.relative_to(ROOT).as_posix(), body, {'tripo': tripo}, [out], info, notes, t0, game,
                      [m.infl] if hasattr(m, 'infl') else [], [mod.relative_to(ROOT).as_posix()])
    out, nt = process_module(m, body, slot, game)
    notes['outfit'] = nt
    return finish(name, 'outfit', race, mod.relative_to(ROOT).as_posix(), body, {'tripo': tripo}, [out, game_faces(body, game)], info, notes, t0, game,
                  [m.infl] if hasattr(m, 'infl') else [], [mod.relative_to(ROOT).as_posix()])


CROSS_SUPPRESS = True     # remove game skin of one module that another module's garment covers (suppress_covered)
CROSS_IN_MM = 40.0        # game skin of one module that another module's garment covers (from this far inside ...
CROSS_MIN_SAMPLES = 4     # ... when all 4 samples of the face are covered (a partly covered face left a hole at the elbow / knee)
CROSS_OUT_MM = 30.0       # ... to this far outside, along the skin's normal) is removed: a gauntlet reaching past the elbow line


def suppress_covered(target, others):
    """Remove target's game-skin faces (prov band) that the garment of the other modules covers. Returns (Mesh, faces removed)."""
    sk = np.flatnonzero(target.prov == 'band')
    gar = [(o.V, o.F[(o.role != SKIN) & np.isin(o.prov, ['tripo', 'mirror'])]) for o in others]
    gar = [(V, F) for V, F in gar if len(F)]
    if not len(sk) or not gar:
        return target, 0
    sub = Mesh(target.V, target.F[sk], target.role[sk], target.part[sk], target.prov[sk])
    hit = np.zeros(len(sk), int)
    for V, F in gar:
        hit = np.maximum(hit, covered_count(sub, V, F, cover_in=CROSS_IN_MM, cover_out=CROSS_OUT_MM))
    drop = np.zeros(len(target.F), bool)
    drop[sk[hit >= CROSS_MIN_SAMPLES]] = True
    return target.subset(~drop), int(drop.sum())


def run_armour(mod_a, mod_b=None, name=None):
    mod_a = Path(mod_a).resolve()
    s, j, race, module = module_identity(mod_a)
    sb_ = module_identity(mod_b)[0] if mod_b else None
    name = name or f'armour__{race}__{s.split("_")[0]}_{s.split("_")[1]}' + (f'__arms_{sb_.split("_")[0]}_{sb_.split("_")[1]}' if sb_ else '__base_arms')
    t0 = time.monotonic()
    body = game_body(race)
    rig = ab.Rig.from_npz(bf.BASE_CACHE / f'{race}_rig.npz')
    x0 = fl.mirror_plane(race, body)[0]
    head = parts(body, 'head.x')
    limbs = {s_: lower_arm(body, s_) + lower_leg(body, s_) for s_ in 'lr'}
    slot_a = [p for p in all_parts(body) if p not in head + limbs['l'] + limbs['r']]
    A, tripoA, infoA = prepare_geo(mod_a, body, rig, race)
    fields = [A.infl] if hasattr(A, 'infl') else []
    if IMPRINT:
        import imprint
        ghosts, info, mods = {'tripo_armor': tripoA}, {'body_armor': infoA}, []
        kA, _ = kept_garment(A, slot_a, head, body)
        mods.append((A.subset(kA), slot_a, see_through(mod_a)))
        game_parts = head + ([] if mod_b else limbs['l'] + limbs['r'])
        if mod_b:
            B, tripoB, infoB = prepare_geo(mod_b, body, rig, race)
            if CENTRE_LIMBS:
                infoB['centred_on_game_limbs'] = centre_limb_pieces(B, body, 'r')
            ghosts['tripo_arms'] = tripoB
            info['right_gauntlet_boot'] = infoB
            if hasattr(B, 'infl'):
                fields += [B.infl, mirrored_field(B.infl, x0)]
            Bm = mirror_mesh(B, x0, body)
            kB, _ = kept_garment(B, limbs['r'], head, body)
            kBl, _ = kept_garment(Bm, limbs['l'], head, body)
            mods += [(B.subset(kB), limbs['r'], see_through(mod_b)), (Bm.subset(kBl), limbs['l'], see_through(mod_b))]
        out, nt = imprint.build(body, mods, game_parts)
        return finish(name, 'armour', race, mod_a.relative_to(ROOT).as_posix(), body, ghosts, [out], info, {'imprint': nt}, t0, game_parts, fields,
                      [mod_a.relative_to(ROOT).as_posix()] + ([Path(mod_b).resolve().relative_to(ROOT).as_posix()] if mod_b else []))
    outA, nA = process_module(A, body, slot_a, head)
    notes = {'body_armor': nA}
    pieces = [outA, game_faces(body, head)]
    ghosts = {'tripo_armor': tripoA}
    info = {'body_armor': infoA}
    if mod_b:
        B, tripoB, infoB = prepare_geo(mod_b, body, rig, race)
        if CENTRE_LIMBS:
            infoB['centred_on_game_limbs'] = centre_limb_pieces(B, body, 'r')
        ghosts['tripo_arms'] = tripoB
        info['right_gauntlet_boot'] = infoB
        outB, nB = process_module(B, body, limbs['r'], head)
        notes['gauntlet_boot.r'] = nB
        if hasattr(B, 'infl'):
            fields += [B.infl, mirrored_field(B.infl, x0)]
        Bm = mirror_mesh(B, x0, body)                                      # the left pair: the module mirrored, then fitted to the left slot
        outBl, nBl = process_module(Bm, body, limbs['l'], head)
        notes['gauntlet_boot.l (mirrored)'] = nBl
        if CROSS_SUPPRESS:
            outA, nA['skin_under_other_module'] = suppress_covered(outA, [outB, outBl])
            outB, nB['skin_under_other_module'] = suppress_covered(outB, [outA])
            outBl, nBl['skin_under_other_module'] = suppress_covered(outBl, [outA])
        pieces = [outA, game_faces(body, head), outB, outBl]
    else:
        pieces.append(game_faces(body, limbs['l'] + limbs['r']))
        notes['forearms / shins'] = 'game body'
    return finish(name, 'armour', race, mod_a.relative_to(ROOT).as_posix(), body, ghosts, pieces, info, notes, t0,
                  head + ([] if mod_b else limbs['l'] + limbs['r']), fields,
                  [mod_a.relative_to(ROOT).as_posix()] + ([Path(mod_b).resolve().relative_to(ROOT).as_posix()] if mod_b else []))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('what', choices=('outfit', 'armour', 'batch', 'variant'))
    ap.add_argument('modules', nargs='*')
    a = ap.parse_args(argv)
    if a.what == 'variant':                                     # geodiv.py variant <result name> <key>: one result again with the viewer's options
        import json
        from shell_slots import OUT
        name, key = a.modules
        rec = next(r for r in json.loads((OUT / 'index.json').read_text())['results'] if r['name'] == name)
        apply_variant(key)
        mods = [ROOT / x for x in rec['modules']]
        if rec['kind'] == 'outfit':
            run_outfit(mods[0], name=name)
        else:
            run_armour(mods[0], mods[1] if len(mods) > 1 else None, name=name)
        return
    if a.what == 'outfit':
        for m in a.modules:
            run_outfit(m)
    elif a.what == 'armour':
        run_armour(a.modules[0], a.modules[1] if len(a.modules) > 1 else None)
    else:
        for m in review_modules():
            try:
                run_outfit(m)
            except Exception as error:  # noqa: BLE001
                log('FAILED', m, type(error).__name__, error)
        for sa, sb, race in REVIEW['armour']:
            ma, mb = armour_modules(sa, sb, race)
            try:
                run_armour(ma, mb)
            except Exception as error:  # noqa: BLE001
                log('FAILED', sa, sb, type(error).__name__, error)


if __name__ == '__main__':
    main()
