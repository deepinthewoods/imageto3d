"""Fixed body sections of a base (pure numpy, no bpy): the base body cut along planes perpendicular to the bone axes.

Ported from `experiments/body_mask_20261104/bm_sections.py` (`LAYOUTS`, `find_loop`, `layout_cuts`, `cut_base`) so that the export can
run inside Blender without scipy / body_fit. The cut itself is identical (exact plane cuts of the flat facets, or "loop" mode: no cut, whole
triangles go to one side where the base already has an edge loop near the target). Differences from the experiment:
- per-vertex data is any array `attrs` (n, k) (all skin groups and the morph deltas of the real export), interpolated along cut edges;
- the part names / pivots come from the Blender base instead of `tools/preview/cache`.

Sections are named `<part>#<band>`; `node_name()` gives the glTF node name `sec_<part>_<band>` (dots to underscores).
"""
import fnmatch
import os

import numpy as np

# part pattern -> cuts. 'bone': fractions along the bone (0 = proximal joint, 1 = distal joint), planes perpendicular to the bone axis;
# 'z': fractions of the part's own height range (torso bands). Parts not listed are one section.
LAYOUTS = {
    'fine': {
        'spine_01.x': {'z': [0.14, 0.64]},           # waist band, under-chest band
        'root.x': {'z': [0.48]},                     # hip band
        'arm_stretch.*': {'bone': [0.5]},
        'forearm_stretch.*': {'bone': [0.5]},
        'thigh_stretch.*': {'bone': [0.5, 0.85]},    # mid-thigh and just above the knee (a coat hem and a boot top end in different sections)
        'leg_stretch.*': {'bone': [0.5]},
    },
    'coarse': {},                                    # the bone-part boundaries only (every part = one section)
}
if os.environ.get('BM_THIGH'):                       # experiment hook, as in the prototype
    LAYOUTS['fine']['thigh_stretch.*'] = {'bone': [float(x) for x in os.environ['BM_THIGH'].split(',')]}
LAYOUT_SNAP = {'fine': True, 'coarse': False}
# an edge loop is used when it lies within tol_frac of the segment length of the target and is at most w_ok_mm thick
SNAP = dict(tol_frac=0.25, w_ok_mm=12.0, step_mm=1.0)
LIMBS = ('arm', 'forearm', 'hand', 'thigh', 'leg', 'foot')
BONE_OF = {'arm_stretch': ('arm', 'forearm'), 'forearm_stretch': ('forearm', 'hand'), 'thigh_stretch': ('thigh', 'leg'), 'leg_stretch': ('leg', 'foot')}
# the rest joint of each limb channel: the head of this ARP bone (+ '.l' / '.r')
PIVOT_BONE = {'arm': 'c_arm_twist', 'forearm': 'c_forearm_stretch', 'hand': 'hand', 'thigh': 'c_thigh_twist', 'leg': 'c_leg_stretch', 'foot': 'foot'}
# races whose per-face thigh / leg part labels are wrong (elf_female: leg_stretch faces reach up to 0.38 H over the thigh): the thigh / shin
# split of those faces is taken from the rig skin weights instead
PART_FROM_SKIN = ('elf_female',)


def pivot_names():
    return [f'{limb}.{side}' for side in 'lr' for limb in LIMBS]


def channel_of_group(name):
    """(limb, side) a base vertex group (ARP deform bone) belongs to, or None (static: spine, shoulders, neck, head, root)."""
    side = 'l' if name.endswith('.l') else ('r' if name.endswith('.r') else None)
    if side is None:
        return None
    for prefix, limb in (('c_forearm_', 'forearm'), ('c_arm_', 'arm'), ('hand.', 'hand'), ('c_thigh_', 'thigh'), ('c_leg_', 'leg'),
                         ('foot.', 'foot'), ('toes_', 'foot'), ('c_surface_foot', 'foot')):
        if name.startswith(prefix):
            return limb, side
    return None


def channel_weights(weights, group_names):
    """{'thigh.l': (n,), ...} skin weight per limb channel from the full group matrix (n, groups)."""
    out = {n: np.zeros(len(weights)) for n in pivot_names()}
    for gi, g in enumerate(group_names):
        ch = channel_of_group(g)
        if ch:
            out[f'{ch[0]}.{ch[1]}'] += weights[:, gi]
    return out


class Mesh:
    """Triangle soup of the body: V (n,3) world rest positions, F (m,3), part (m,) index into part_names."""

    def __init__(self, V, F, part, part_names):
        V = np.asarray(V, np.float64)
        F = np.asarray(F, np.int64)
        part = np.asarray(part, np.int64)
        a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
        cross = np.cross(b - a, c - a)
        keep = np.linalg.norm(cross, axis=1) > 1e-12                      # degenerate triangles carry nothing
        self.dropped = int((~keep).sum())
        self.relabelled = 0
        self.keep = keep
        self.V, self.F, self.part = V, F[keep], part[keep]
        self.part_names = [str(n) for n in part_names]
        self.A, self.B, self.C = V[self.F[:, 0]], V[self.F[:, 1]], V[self.F[:, 2]]
        cross = cross[keep]
        self.area = np.linalg.norm(cross, axis=1) / 2
        self.tri_n = cross / (2 * self.area[:, None])


# ----------------------------------------------------------------------------------------------- plane cuts / edge-loop snapping

def find_loop(body, part, pi, nrm, target, tol):
    """Best existing edge loop of part `pi` near the plane (nrm . x = target): a vertex ring whose two sides (t < tau - w and t > tau + w,
    t = nrm . x) share no edge, i.e. no triangle of the part straddles it. For a threshold tau the thinnest such ring has
    w(tau) = max over edges (a, b) with t_a < tau < t_b of min(tau - t_a, t_b - tau). Returns (tau, w) of the feasible tau (w <= SNAP w_ok)
    closest to the target within +-tol, or None."""
    F = body.F[part == pi]
    e = np.unique(np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1), axis=0)
    ta, tb = body.V[e[:, 0]] @ nrm, body.V[e[:, 1]] @ nrm
    lo, hi = np.minimum(ta, tb), np.maximum(ta, tb)
    step = SNAP['step_mm'] / 1000
    best = None
    for tau in target + np.arange(-tol, tol + step / 2, step):
        m = (lo < tau) & (hi > tau)
        w = float(np.max(np.minimum(tau - lo[m], hi[m] - tau))) if m.any() else 0.
        if w * 1000 <= SNAP['w_ok_mm']:
            d = abs(tau - target)
            if best is None or d < best[2] - 1e-12:
                best = (float(tau), w, d)
    return None if best is None else best[:2]


# a limb part may hold triangles whose dominant skin channel is its own limb or a neighbour in the chain; anything else is a stray label
# (elf_female: one foot triangle carries the arm material, which stretched `sec_arm_stretch_*_1` down to the feet)
CHAIN_OK = {'arm': ('arm', 'forearm'), 'forearm': ('arm', 'forearm', 'hand'), 'hand': ('forearm', 'hand'),
            'thigh': ('thigh', 'leg'), 'leg': ('thigh', 'leg', 'foot'), 'foot': ('leg', 'foot')}
PART_OF_LIMB = {'arm': 'arm_stretch', 'forearm': 'forearm_stretch', 'hand': 'hand', 'thigh': 'thigh_stretch', 'leg': 'leg_stretch', 'foot': 'foot'}


def relabel_strays(body, skin, part):
    """Reassign limb triangles whose dominant skin channel (mean of the three vertices, > 0.5) is not their own limb or a chain neighbour on the
    same side to the part of that channel. Returns (part, number of triangles changed)."""
    part = part.copy()
    names = body.part_names
    chans = list(skin)
    W = np.stack([skin[c] for c in chans], 1)[body.F].mean(1)                     # (tris, channels)
    best, top = W.argmax(1), W.max(1)
    changed = 0
    for pi, pn in enumerate(names):
        stem, _, side = pn.partition('.')
        limb = next((l for l, p in PART_OF_LIMB.items() if p == stem), None)
        if limb is None or side not in ('l', 'r'):
            continue
        for t in np.flatnonzero((part == pi) & (top > 0.5)):
            cl, cs = chans[best[t]].split('.')
            if cs == side and cl in CHAIN_OK[limb]:
                continue
            target = f'{PART_OF_LIMB[cl]}.{cs}'
            if target in names:
                part[t] = names.index(target)
                changed += 1
    return part, changed


def effective_parts(body, skin, race):
    """Per-triangle part index used for the sections: body.part with stray limb labels fixed (relabel_strays), and for races in PART_FROM_SKIN
    every triangle labelled thigh_stretch.* or leg_stretch.* goes to the thigh or the shin by its dominant skin channel (mean of its three vertices)."""
    part = body.part.copy()
    if skin is not None:
        part, body.relabelled = relabel_strays(body, skin, part)
    if race in PART_FROM_SKIN and skin is not None:
        for side in 'lr':
            ti, li = body.part_names.index(f'thigh_stretch.{side}'), body.part_names.index(f'leg_stretch.{side}')
            cand = np.flatnonzero((part == ti) | (part == li))
            wt = skin[f'thigh.{side}'][body.F[cand]].mean(1)
            wl = skin[f'leg.{side}'][body.F[cand]].mean(1)
            part[cand] = np.where(wt >= wl, ti, li)
    return part


def layout_cuts(body, pivots, layout, race='', part=None):
    """{part index: [cut, ...]} sorted along the normal. cut = {name, normal, target, tau, mode 'loop' | 'plane', w, dist_mm, ...}.
    `pivots` = {'arm.l': xyz, ...} (pivot_names())."""
    table = LAYOUTS[layout]
    snap = LAYOUT_SNAP[layout]
    part = body.part if part is None else part
    out = {}
    for pi, pn in enumerate(body.part_names):
        for pat, spec in table.items():
            if not fnmatch.fnmatch(pn, pat) or not (part == pi).any():
                continue
            cuts = []
            vids = np.unique(body.F[part == pi])
            if 'z' in spec:
                z = body.V[vids, 2]
                nrm, L = np.array([0., 0., 1.]), float(z.max() - z.min())
                for f in spec['z']:
                    cuts.append({'name': f'{pn} z{f:g}', 'normal': nrm, 'target': float(z.min() + f * L), 'length': L,
                                 'centre': np.array([0., float(body.V[vids, 1].mean()), float(z.min() + f * L)])})
            if 'bone' in spec:
                limb, side = pn.split('.')[0], pn.split('.')[1]
                a, b = BONE_OF[limb]
                p0, p1 = pivots[f'{a}.{side}'], pivots[f'{b}.{side}']
                L = float(np.linalg.norm(p1 - p0))
                nrm = (p1 - p0) / L
                for f in spec['bone']:
                    c = p0 + f * (p1 - p0)
                    cuts.append({'name': f'{pn} bone{f:g}', 'normal': nrm, 'target': float(nrm @ c), 'length': L, 'centre': c})
            for c in cuts:
                c['mode'], c['tau'], c['w'] = 'plane', c['target'], 0.
                if snap:
                    r = find_loop(body, part, pi, c['normal'], c['target'], SNAP['tol_frac'] * c['length'])
                    if r is not None:
                        c['mode'], c['tau'], c['w'] = 'loop', r[0], r[1]
                c['dist_mm'] = abs(c['tau'] - c['target']) * 1000
                c['part'] = pn
                c['suggest'] = None
                if c['mode'] == 'plane' and snap:
                    cc = c['centre'] + (c['target'] - c['normal'] @ c['centre']) * c['normal']
                    c['suggest'] = (f'suggest adding an edge loop in projects/{race}.blend on {pn} perpendicular to the bone at '
                                    f'({cc[0]:.3f}, {cc[1]:.3f}, {cc[2]:.3f}) m (world, rest pose)')
            out[pi] = sorted(cuts, key=lambda c: c['target'])
    return out


def _ear_clip(poly, V):
    """Triangulate a convex polygon (list of vertex ids, collinear points allowed) by repeatedly cutting off the largest-area ear."""
    poly = list(poly)
    tris = []
    while len(poly) > 3:
        best, bi = -1., None
        for i in range(len(poly)):
            a, b, c = poly[i - 1], poly[i], poly[(i + 1) % len(poly)]
            ar = np.linalg.norm(np.cross(V(b) - V(a), V(c) - V(a)))
            if ar > best:
                best, bi = ar, i
        a, b, c = poly[bi - 1], poly[bi], poly[(bi + 1) % len(poly)]
        tris.append((a, b, c))
        poly.pop(bi)
    tris.append(tuple(poly))
    return tris


def cut_base(body, pivots, attrs, layout='fine', race='', skin=None, eps=1e-9):
    """The sectioned base of a race: exact plane cuts of the flat facets.

    `body` is a Mesh, `attrs` (n, k) any per-vertex data interpolated linearly along cut edges (skin weights, morph deltas), `skin` the
    per-channel weights (channel_weights) used by effective_parts. Returns a dict: V (n', 3), A (n', k), F (m', 3), part, orig (index into
    body.F), band, sec (section id per triangle), sec_names, report (counts, area / planarity / boundary checks, cut boundaries)."""
    n0 = len(body.V)
    attrs = np.asarray(attrs, np.float64)
    newV, newW = [], []
    cache, edge_cuts = {}, {}
    part_eff = effective_parts(body, skin, race)
    planes = layout_cuts(body, pivots, layout, race, part_eff)

    def pos(i):
        return body.V[i] if i < n0 else newV[i - n0]

    def wt(i):
        return attrs[i] if i < n0 else newW[i - n0]

    def mk(a, b, sa, sb, key_tag):
        lo, hi = (a, b) if a < b else (b, a)
        key = (lo, hi, key_tag)
        if key not in cache:
            slo, shi = (sa, sb) if a < b else (sb, sa)
            t = slo / (slo - shi)                                # canonical direction lo -> hi: both neighbours compute the SAME bits
            vid = n0 + len(newV)
            newV.append(pos(lo) + t * (pos(hi) - pos(lo)))
            newW.append(wt(lo) + t * (wt(hi) - wt(lo)))
            cache[key] = vid
            if lo < n0 and hi < n0:
                edge_cuts.setdefault((lo, hi), []).append((t, vid))
        return cache[key]

    def clip(poly, sval, sign, tag):
        out = []
        for i in range(len(poly)):
            a, b = poly[i], poly[(i + 1) % len(poly)]
            sa, sb = sign * sval[a], sign * sval[b]
            if sa >= -eps:
                out.append(a)
            if (sa > eps and sb < -eps) or (sa < -eps and sb > eps):
                out.append(mk(a, b, sval[a], sval[b], tag))
        return out

    outF, outPart, outBand, outOrig = [], [], [], []
    for t in range(len(body.F)):
        pi = int(part_eff[t])
        pl = planes.get(pi, [])
        pieces = [(tuple(int(x) for x in body.F[t]), 0)]
        for k, cut in enumerate(pl):
            nrm, off = cut['normal'], cut['tau']
            nxt = []
            tag = (pi, k)
            for tri, band in pieces:
                if band != k:
                    nxt.append((tri, band))
                    continue
                if cut['mode'] == 'loop':          # existing edge loop: whole triangles go to one side, nothing is cut
                    ts = [float(nrm @ pos(v)) - off for v in tri]
                    w = cut['w'] + 1e-9
                    if any(x < -w for x in ts):
                        nb = k
                    elif any(x > w for x in ts):
                        nb = k + 1
                    else:
                        nb = k if sum(ts) / 3 < 0 else k + 1
                    nxt.append((tri, nb))
                    continue
                sval = {v: float(nrm @ pos(v)) - off for v in tri}
                for sign, nb in ((-1, k), (1, k + 1)):
                    poly = clip(list(tri), sval, sign, tag)
                    if len(poly) < 3:
                        continue
                    for tt in _ear_clip(poly, pos):
                        if np.linalg.norm(np.cross(pos(tt[1]) - pos(tt[0]), pos(tt[2]) - pos(tt[0]))) > 1e-13:
                            nxt.append((tt, nb))
            pieces = nxt
        for tri, band in pieces:
            outF.append(tri)
            outPart.append(pi)
            outBand.append(band)
            outOrig.append(t)
    # conforming fix: uncut triangles (original vertices only) next to a cut edge get the cut vertices inserted (fan, no new geometry)
    fixed = 0
    F2, P2, B2, O2 = [], [], [], []
    for tri, pi, band, o in zip(outF, outPart, outBand, outOrig):
        if max(tri) < n0 and any((min(tri[i], tri[(i + 1) % 3]), max(tri[i], tri[(i + 1) % 3])) in edge_cuts for i in range(3)):
            poly = []
            for i in range(3):
                a, b = tri[i], tri[(i + 1) % 3]
                poly.append(a)
                ins = edge_cuts.get((min(a, b), max(a, b)))
                if ins:
                    ins = sorted(ins)                                 # by t from the lower id
                    poly += [v for _, v in (ins if a < b else ins[::-1])]
            for tt in _ear_clip(poly, pos):
                F2.append(tt)
                P2.append(pi)
                B2.append(band)
                O2.append(o)
            fixed += 1
        else:
            F2.append(tri)
            P2.append(pi)
            B2.append(band)
            O2.append(o)
    V = np.concatenate([body.V, np.array(newV).reshape(-1, 3)])
    A = np.concatenate([attrs, np.array(newW).reshape(-1, attrs.shape[1])])
    F = np.array(F2, np.int64)
    part, band, orig = np.array(P2), np.array(B2), np.array(O2)
    names, sec = [], np.zeros(len(F), np.int64)
    order = {}
    for i in np.lexsort((band, part)):
        key = (int(part[i]), int(band[i]))
        if key not in order:
            order[key] = len(names)
            names.append(f'{body.part_names[key[0]]}#{key[1]}')
        sec[i] = order[key]
    # checks
    Pa, Pb, Pc = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    area = np.linalg.norm(np.cross(Pb - Pa, Pc - Pa), axis=1) / 2
    a_orig = np.bincount(orig, weights=area, minlength=len(body.F))
    plane_err = max(np.abs(np.einsum('ij,ij->i', (X - body.A[orig]), body.tri_n[orig])).max() for X in (Pa, Pb, Pc))

    def boundary_len(Vv, Ff):
        e = np.sort(np.concatenate([Ff[:, [0, 1]], Ff[:, [1, 2]], Ff[:, [2, 0]]]), axis=1)
        k = e[:, 0] * (len(Vv) + 1) + e[:, 1]
        u, c = np.unique(k, return_counts=True)
        ub = u[c == 1]
        return float(np.linalg.norm(Vv[ub // (len(Vv) + 1)] - Vv[ub % (len(Vv) + 1)], axis=1).sum())
    report = {'layout': layout, 'tris_before': int(len(body.F)), 'tris_after': int(len(F)), 'verts_before': int(n0), 'verts_after': int(len(V)),
              'sections': len(names), 'cut_vertices': int(len(newV)), 'neighbour_fan_fixed_tris': fixed, 'degenerate_dropped': body.dropped, 'stray_labels_fixed': body.relabelled,
              'area_conservation_max_rel_err': float(np.abs(a_orig / body.area - 1).max()), 'max_off_plane_um': float(plane_err * 1e6),
              'boundary_length_m_before': boundary_len(body.V, body.F), 'boundary_length_m_after': boundary_len(V, F),
              'boundaries': [{'name': c['name'], 'mode': c['mode'], 'dist_from_target_mm': round(c['dist_mm'], 1),
                              'ring_thickness_mm': round(c['w'] * 1000, 1), 'suggest': c['suggest']}
                             for pi_ in sorted(planes) for c in planes[pi_]],
              'loops': int(sum(c['mode'] == 'loop' for pi_ in planes for c in planes[pi_])),
              'plane_fallbacks': int(sum(c['mode'] == 'plane' for pi_ in planes for c in planes[pi_]))}
    return {'V': V, 'A': A, 'F': F, 'part': part, 'orig': orig, 'band': band, 'sec': sec, 'sec_names': names, 'report': report}


def node_name(sec):
    """glTF node name of a section: `sec_<part>_<band>`; no dots (some viewers strip them from node names)."""
    return 'sec_' + sec.replace('#', '_').replace('.', '_')
