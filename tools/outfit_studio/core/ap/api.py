"""Attachment-point placement: orchestration (pure numpy; the Blender side only feeds arrays in).

    ctx = prepare(body, settings)                  # subdivided surfaces, weights, regions
    out = generate(ctx, counts)                    # {'points': {name: point}, 'skipped': [...], ...}
    rep = quality.report(ctx, points)              # the SPEC section 5 quality numbers

`body` is the base mesh in rest pose: `V` (n,3), `F` (m,3) triangles, `part` (m,) index into
`part_names` (the `bone_<part>` material names), `W` (n,g) deform weights with `groups` names and
`bones` {name: (head, tail)}. A *point* is `{'group', 'index', 'pos', 'n', 'z', 'parent'}`.
`place_on_head` runs the head part on any head mesh (no rig needed) and returns 4x4 matrices.
"""
import zlib

import numpy as np

from . import curves, frames, geodesic, groups as G, mesh, regions, sample, weights

AXES = regions.AXES


# ------------------------------------------------------------------ context
def _submesh(V, F, face_mask, vert_attrs):
    used = np.unique(F[face_mask])
    remap = -np.ones(len(V), np.int64)
    remap[used] = np.arange(len(used))
    return V[used], remap[F[face_mask]], {k: np.asarray(a)[used] for k, a in vert_attrs.items()}


def prepare(body, settings=None, axes=None, x0=None):
    """Build the fine surfaces the generators and the quality report work on."""
    s = {**G.DEFAULTS, **(settings or {})}
    ax = {k: np.asarray(v, float) for k, v in (axes or body.get('axes') or AXES).items()}
    V = np.asarray(body['V'], float)
    F = np.asarray(body['F'], np.int64)
    height = float(np.ptp(V @ ax['up']))
    names = list(body['part_names'])
    W = np.asarray(body.get('W', np.zeros((len(V), 0))), float)
    groups = list(body.get('groups', []))
    if W.shape[1]:
        Sg, segs = weights.segment_weights(W, groups)
    else:
        Sg, segs = np.ones((len(V), 1)), ['all']
    va = {'Sg': Sg, 'W': W}
    for key, src in (('rgb', 'rgb'), ('paint', 'paint_scalp'), ('exclude', 'exclude')):   # optional per-vertex head inputs
        if body.get(src) is not None:
            va[key] = np.asarray(body[src], float)
    ctx = {'s': s, 'axes': ax, 'x0': float(body.get('x0', 0.0) if x0 is None else x0), 'height': height,
           'part_names': names, 'groups': groups, 'bones': body.get('bones', {}), 'cache': {},
           'segments': segs, 'face_part': np.asarray(body['part'], np.int64)}
    radius = s['normal_radius'] * height
    lvl = min(3, mesh.level_for(V, F, .008 * height))
    ctx['S'] = mesh.make_surface(V, F, {'part': ctx['face_part']}, va, level=lvl, normal_radius=radius)
    ctx['S']['attrs']['pur'] = weights.purity(ctx['S']['attrs']['Sg'])
    hp = [i for i, n in enumerate(names) if n == G.HEAD_PART]
    ctx['head'] = None
    if hp and np.isin(ctx['face_part'], hp).any():
        hm = np.isin(ctx['face_part'], hp)
        hv, hf, hva = _submesh(V, F, hm, va)
        hs = mesh.make_surface(hv, hf, {'part': ctx['face_part'][hm]}, hva,
                               level=min(4, mesh.level_for(hv, hf, .0045 * height), mesh.level_cap(len(hf), 24000)),
                               normal_radius=radius)
        hs['attrs']['pur'] = weights.purity(hs['attrs']['Sg'])
        hs['src_faces'] = np.flatnonzero(hm)
        ctx['head'] = hs
    return ctx


def part_ids(ctx, parts):
    return [i for i, n in enumerate(ctx['part_names']) if n in parts]


def torso_axis(ctx):
    """Unit spine direction (pointing up) from the spine bones, else world up."""
    bones = [ctx['bones'][b] for b in G.TORSO_BONES if b in ctx['bones']]
    axis = mesh.unit(np.asarray(bones[-1][1] - bones[0][0], float)) if bones else ctx['axes']['up']
    return axis if np.dot(axis, ctx['axes']['up']) >= 0 else -axis


def region_surface(ctx, parts):
    """The sub-surface made of the faces of the named parts (None if empty or too small).

    Its `attrs['pur']` is the weight share of the region's own segment smoothed along the limb or
    spine axis, so that joint bands are found from the weights without being fooled by single
    corners of big triangles."""
    key = tuple(sorted(parts))
    c = ctx['cache']
    if key not in c:
        S = ctx['S']
        ids = part_ids(ctx, parts)
        m = np.isin(S['fattrs']['part'], ids) if ids else np.zeros(len(S['F']), bool)
        seg = weights.segment_of(parts[0])
        if m.sum() < 8 and len(parts) == 1 and seg in ctx['segments']:
            # a part with (almost) no faces of its own: take the faces the weights give to its segment
            Sg = S['attrs']['Sg']
            share = Sg[:, ctx['segments'].index(seg)] / np.maximum(Sg.sum(1), 1e-9)
            m = share[S['F']].mean(1) >= .5
        rs = mesh.sub_surface(S, m) if m.sum() >= 8 else None
        if rs is not None:
            if key == tuple(sorted(G.TORSO_PARTS)):
                axis = torso_axis(ctx)
            elif parts[0] in ctx['bones']:
                h, t = ctx['bones'][parts[0]]
                axis = mesh.unit(np.asarray(t - h, float))
            else:
                axis = None
            if axis is not None:
                ext = float(np.ptp(rs['V'] @ axis))
                rs['attrs']['pur'] = weights.axial_share(rs['V'], rs['attrs']['Sg'], axis, .06 * ext)
        c[key] = rs
    return c[key]


def purity_threshold(ctx):
    return ctx['s']['joint_purity'] if ctx['s']['exclude_joints'] else 0.0


# ------------------------------------------------------------------ point construction
def _point_set(surf, face, bary, z_hint, ctx, fallback=None, thr=None):
    P, n = sample.at(surf, face, bary)
    z = frames.project_tangent(n, z_hint, fallback if fallback is not None else [ctx['axes']['up']] * len(n))
    W = mesh.bary_eval(surf, surf['attrs']['W'], face, bary) if surf['attrs']['W'].shape[1] else None
    parent = [ctx['groups'][i] for i in W.argmax(1)] if W is not None else [''] * len(P)
    return {'pos': P, 'n': n, 'z': z, 'parent': parent, 'thr': purity_threshold(ctx) if thr is None else thr}


def _mirror(pts, x0, axis=0):
    """The exact mirror image of a point set (parents swapped to the other side). On a mirror-symmetric
    mesh it lies on the surface; where the mesh is asymmetric it may float by that asymmetry, which the
    engine's nearest-surface rebinding absorbs and the report lists as `off_surface_mm`."""
    return {'pos': mesh.reflect(pts['pos'], x0, axis), 'n': mesh.reflect(pts['n'], 0.0, axis),
            'z': mesh.reflect(pts['z'], 0.0, axis), 'parent': [weights.swap_side(p) for p in pts['parent']],
            'thr': pts.get('thr', 0.0)}


def _take(pts, mask):
    return {'pos': pts['pos'][mask], 'n': pts['n'][mask], 'z': pts['z'][mask],
            'parent': [p for p, m in zip(pts['parent'], mask) if m], 'thr': pts.get('thr', 0.0)}


def _cat(a, b):
    return {'pos': np.vstack([a['pos'], b['pos']]), 'n': np.vstack([a['n'], b['n']]),
            'z': np.vstack([a['z'], b['z']]), 'parent': a['parent'] + b['parent'], 'thr': a.get('thr', 0.0)}


def _scatter_set(ctx, surf, verts, paired, z_hint, x0):
    """Points at the picked vertices plus the mirror images of the paired ones."""
    f, b = sample.vertex_bary(surf, verts)
    pts = _point_set(surf, f, b, z_hint(f, b), ctx)
    paired = np.asarray(paired, bool)
    if paired.any():
        pts = _cat(pts, _mirror(_take(pts, paired), x0))
    pts['verts'] = np.asarray(verts)
    return pts


def _count(ctx, counts, group, default, length=None):
    s = ctx['s']
    n = counts.get(group) or default
    if s['row_mode'] == 'spacing' and length:
        n = max(2, int(round(length / max(s['spacing'], 1e-3))))
    return max(1, int(round(n * s['density'])))


# ------------------------------------------------------------------ head
def head_context(ctx, hs, strategy, template=None, texture_rgb=None, paint=None):
    """Regions + crown geodesics of a head surface (cached per strategy)."""
    key = ('head', strategy)
    if key in ctx['cache']:
        return ctx['cache'][key]
    s = ctx['s']
    texture_rgb = hs['attrs'].get('rgb') if texture_rgb is None else texture_rgb
    if paint is None and hs['attrs'].get('paint') is not None and (hs['attrs']['paint'] > .5).any():
        paint = hs['attrs']['paint']
    prm = {'hairline': s['hairline']}
    r = regions.head_regions(hs, strategy, ctx['axes'], ctx['x0'], prm, template, texture_rgb, paint)
    lm = r['lm']
    up = ctx['axes']['up']
    ex = hs['attrs'].get('exclude')
    if ex is not None and (ex > .5).any():  # ears, horns...: no hair or face points on them
        keep = regions.mask_to_margin(hs['g'], ex <= .5)
        r['scalp'], r['face'] = np.minimum(r['scalp'], keep), np.minimum(r['face'], keep)
    if s['symmetry']:                       # make the regions exactly mirror-symmetric (min with the mirror vertex)
        mm = sample.mirror_map(hs['V'], ctx['x0'], 0, .3 * hs['edge'])
        for k in ('scalp', 'face'):
            r[k] = np.where(mm >= 0, np.minimum(r[k], r[k][np.maximum(mm, 0)]), r[k])
    scalp = r['scalp']
    ok = scalp >= 0
    crown = lm['crown']
    if ok.any():
        top = np.flatnonzero(ok)
        zt = hs['V'][top] @ up
        near = top[zt >= zt.max() - 1e-4 * lm['H']]
        crown = int(near[np.abs(lm['L'][near, 0]).argmin()])
    dc = geodesic.dijkstra(hs['g'], [crown])
    dc = np.where(np.isfinite(dc), dc, 0.0)
    grad = frames.gradient_field(hs, dc)
    r.update({'crown': crown, 'dcrown': dc, 'grad': mesh.unit(grad),
              'side': sample.signed_midline(hs, lm['L'][:, 0])})
    ctx['cache'][key] = r
    return r


def _toward_crown(hr, hs, face, bary):
    g = mesh.bary_eval(hs, hr['grad'], face, bary)
    return -g


def head_points(ctx, hs, counts, want, template=None, texture_rgb=None, paint=None):
    """Hair + scatter groups on a head surface. Returns (points by group, skipped, info)."""
    s = ctx['s']
    out, skipped = {}, []
    ax, x0 = ctx['axes'], ctx['x0']
    fwd, up = ax['fwd'], ax['up']
    thr, floor = purity_threshold(ctx), s['joint_floor']
    pur = hs['attrs']['pur']
    hr_hair = head_context(ctx, hs, s['strategy_hair'], template, texture_rgb, paint)
    hr_sc = hr_hair if s['strategy_scatter'] == s['strategy_hair'] else head_context(
        ctx, hs, s['strategy_scatter'], template, texture_rgb, paint)
    info = {'hair_region': hr_hair['info'], 'scatter_region': hr_sc['info']}
    lm = hr_hair['lm']
    inset = .004 * lm['H']
    sym = s['symmetry']
    scalp, L = hr_hair['scalp'], lm['L']

    def crown_hint(hr, f, b):
        return -mesh.bary_eval(hs, hr['grad'], f, b)

    def make(f, b, z, **kw):
        return _point_set(hs, f, b, z, ctx, fallback=np.tile(fwd, (len(f), 1)), **kw)

    if 'crown' in want:
        f, b = sample.vertex_bary(hs, [hr_hair['crown']])
        out['crown'] = make(f, b, np.tile(fwd, (1, 1)))
    edge_band = (scalp >= 0) & (scalp <= 2.5 * hs['edge'])             # the hairline itself
    for name, sign in (('templeL', 1), ('templeR', -1)):
        twin = G.mirror_of(name)
        if name not in want or (sym and sign < 0 and twin in want):
            continue
        cand = None
        for frac in (.2, -.5, -9.0):                                   # front corners first, then anything on the hairline
            c = edge_band & (sign * L[:, 0] > 0) & (L[:, 1] >= lm['cy'] + frac * (lm['front'] - lm['cy']))
            if c.any():
                cand = c
                break
        if cand is None:
            skipped += [x for x in (name, twin if sym else name) if x in want]
            continue
        pure = cand & (pur >= thr) if (cand & (pur >= thr)).any() else cand
        v = np.flatnonzero(pure)[(sign * L[pure, 0]).argmax()]
        f, b = sample.vertex_bary(hs, [v])
        out[name] = make(f, b, crown_hint(hr_hair, f, b), thr=min(thr, float(pur[v])))
        if sym and sign > 0 and twin in want:
            out[twin] = _mirror(out[name], x0)
    # scalp rows: iso-lines of the signed surface distance from the mid-plane, hairline to nape
    offsets = {'c': 0.0, 'l1': G.ROW_OFFSET['l1'], 'l2': G.ROW_OFFSET['l2'],
               'r1': -G.ROW_OFFSET['l1'], 'r2': -G.ROW_OFFSET['l2']}
    ok_scalp = scalp >= 0
    half_w = float(hr_hair['side'][ok_scalp].max()) if ok_scalp.any() else 0.0
    step = .6 * hs['edge']
    done = {}
    for g in [g for g in want if G.family_of(g) == 'hair' and g.startswith('scalp_')]:
        src = G.mirror_of(g) if sym and g[6:] in ('r1', 'r2') else g      # generate the left row, mirror it
        if src not in done:
            chains = []
            for ch in curves.isolines(hs['V'], hs['F'], hr_hair['side'], offsets[src[6:]] * half_w):
                chains += curves.clip(hs['F'], ch, scalp, 0.0)
            n = _count(ctx, counts, src, G.DEFAULT_COUNT['hair'])
            best, t = sample.adaptive_row(hs, chains, thr, floor, step, key_dir=-fwd, min_len=2 * hs['edge'])
            res = None
            r = sample.rows_on_chain(hs, best, n, True, inset) if best is not None else None
            if r is not None:
                res = make(r[0], r[1], crown_hint(hr_hair, r[0], r[1]), thr=t)
            done[src] = res
        res = done[src]
        if res is None:
            skipped.append(g)
        else:
            out[g] = res if src == g else _mirror(res, x0)
    # scatter on the head
    spacing = s['spacing'] if s['row_mode'] == 'spacing' else 0.0
    for g in [g for g in want if g in ('scalp_rand', 'face_rand')]:
        margin = hr_sc['scalp'] if g == 'scalp_rand' else hr_sc['face']
        ok, t = sample.adaptive_candidates(pur, thr, floor)
        cand = (margin >= inset) & ok
        n = _count(ctx, counts, g, G.DEFAULT_COUNT['scatter'])
        v, paired = sample.scatter(hs, cand, n, spacing, x0=x0, symmetric=sym, seed=zlib.crc32(g.encode()))
        if not v:
            skipped.append(g)
            continue
        if g == 'scalp_rand':
            hint = lambda f, b: crown_hint(hr_sc, f, b)                         # noqa: E731
        else:
            hint = lambda f, b: np.tile(up, (len(f), 1))                        # noqa: E731
        out[g] = _scatter_set(ctx, hs, v, paired, hint, x0)
        out[g]['thr'] = t
    return out, skipped, info


# ------------------------------------------------------------------ limbs and torso
def _rot(v, axis, deg):
    a = np.radians(deg)
    k = axis / np.linalg.norm(axis)
    return v * np.cos(a) + np.cross(k, v) * np.sin(a) + k * np.dot(k, v) * (1 - np.cos(a))


def _clip_all(F, chains, field, thr=0.0):
    out = []
    for ch in chains:
        out += curves.clip(F, ch, field, thr)
    return out


def plane_row(ctx, rs, o, axis, d, n_for, tangent):
    """One iso-angle row on a region surface: the section of the surface with the half-plane
    through `axis` towards `d`. Returns points or None. `n_for(length)` gives the point count."""
    V, F = rs['V'], rs['F']
    thr = purity_threshold(ctx)
    rel = V - o
    chains = curves.isolines(V, F, rel @ np.cross(axis, d), 0.0)
    chains = _clip_all(F, chains, rel @ d, 0.0)
    if not chains:
        return None
    a = rel @ axis
    lo, hi = a.min(), a.max()
    m = 0.0 if thr > 0 else .08 * (hi - lo)
    chains = _clip_all(F, chains, a - (lo + m), 0.0)
    chains = _clip_all(F, chains, (hi - m) - a, 0.0)
    best, t = sample.adaptive_row(rs, chains, thr, ctx['s']['joint_floor'], .6 * rs['edge'], key_dir=axis,
                                  min_len=2 * rs['edge'])
    if best is None:
        return None
    r = sample.rows_on_chain(rs, best, n_for(best[2] - best[1]), False, .5 * rs['edge'])
    if r is None:
        return None
    return _point_set(rs, r[0], r[1], np.tile(tangent, (len(r[0]), 1)), ctx, thr=t)


def limb_region(ctx, region):
    """Surface of a limb segment; None where it is missing, tiny or has no comparable twin on the
    other side (a legless base), so those groups are left alone rather than placed on stubs."""
    rs = region_surface(ctx, (G.LIMBS[region][0],))
    if rs is None or rs['area'].sum() < 2e-3 * ctx['height'] ** 2:
        return None
    if ctx['s']['symmetry']:
        twin = region_surface(ctx, (G.LIMBS[G.mirror_of(region + '_x')[:-2]][0],))
        if twin is None or not .5 < twin['area'].sum() / rs['area'].sum() < 2:
            return None
    return rs


def limb_rows(ctx, counts, want):
    s, ax = ctx['s'], ctx['axes']
    out, skipped = {}, []
    sign = -1.0 if s['limb_tangent'] == 'proximal' else 1.0
    todo = {}
    for g in want:
        if G.family_of(g) == 'limbs':
            region, tok = g.rsplit('_', 1)
            todo.setdefault(region, []).append(tok)
    for region in sorted(todo):
        left = region.endswith('L')
        if s['symmetry'] and not left and region[:-1] + 'L' in todo:
            continue                          # generated as the mirror of its left twin
        part, bone, dirs = G.LIMBS[region]
        wanted = {region: todo[region]}
        twin = region[:-1] + ('R' if left else 'L')
        if s['symmetry'] and left:
            wanted[twin] = todo.get(twin, [])
        rs = limb_region(ctx, region)
        if rs is None or bone not in ctx['bones']:
            for r, toks in wanted.items():
                skipped += [f'{r}_{t}' for t in toks]
            continue
        h, t = ctx['bones'][bone]
        axis = mesh.unit(np.asarray(t - h, float))
        w = rs['area'][:, None]
        o = (rs['V'][rs['F']].mean(1) * w).sum(0) / w.sum()
        side = 1.0 if left else -1.0
        dirmap = {'up': ax['up'], 'dn': -ax['up'], 'fwd': ax['fwd'], 'bk': -ax['fwd'],
                  'out': side * ax['left'], 'in': -side * ax['left']}
        for tok in dirs:
            names = [f'{r}_{tok}' for r in wanted if tok in wanted[r]]
            if not names:
                continue
            d = dirmap[tok] - np.dot(dirmap[tok], axis) * axis
            if np.linalg.norm(d) < 1e-4:
                skipped += names
                continue
            cnt = max(counts.get(n, 0) for n in names)
            res = plane_row(ctx, rs, o, axis, d / np.linalg.norm(d),
                            lambda L, c=cnt, n0=names[0]: _count(ctx, {n0: c}, n0, G.DEFAULT_COUNT['limbs'], L),
                            sign * axis)
            for nme in names:
                if res is None:
                    skipped.append(nme)
                else:
                    out[nme] = res if nme.rsplit('_', 1)[0] == region else _mirror(res, ctx['x0'])
    return out, skipped


def torso_lines(ctx, counts, want):
    s, ax = ctx['s'], ctx['axes']
    out, skipped = {}, []
    toks = [g[6:] for g in want if G.family_of(g) == 'torso']
    if not toks:
        return out, skipped
    rs = region_surface(ctx, G.TORSO_PARTS)
    if rs is None:
        return out, [f'torso_{t}' for t in toks]
    axis = torso_axis(ctx)
    w = rs['area'][:, None]
    o = (rs['V'][rs['F']].mean(1) * w).sum(0) / w.sum()
    o = o + ax['left'] * (ctx['x0'] - np.dot(o, ax['left']))         # the axis lies in the mid-plane
    d0 = ax['fwd'] - np.dot(ax['fwd'], axis) * axis
    d0 = d0 / np.linalg.norm(d0)
    sym = s['symmetry']
    todo = {t for t in toks if not sym or G.is_left('torso_' + t) or G.mirror_of('torso_' + t) == 'torso_' + t}
    todo |= {G.mirror_of('torso_' + t)[6:] for t in toks if sym and not G.is_left('torso_' + t)
             and G.mirror_of('torso_' + t) != 'torso_' + t}
    for t in sorted(todo):
        g = f'torso_{t}'
        n = max(counts.get(g, 0), counts.get(G.mirror_of(g), 0))
        res = plane_row(ctx, rs, o, axis, _rot(d0, axis, G.TORSO_ANGLE[t]),
                        lambda L, n=n, g=g: _count(ctx, {g: n}, g, G.DEFAULT_COUNT['torso'], L), ax['up'])
        for gg in {g, G.mirror_of(g)} & set(want):
            if res is None:
                skipped.append(gg)
            else:
                out[gg] = res if gg == g else _mirror(res, ctx['x0'])
    return out, skipped


def body_scatter(ctx, counts, want):
    s, ax = ctx['s'], ctx['axes']
    out, skipped = {}, []
    thr, floor, x0, sym = purity_threshold(ctx), s['joint_floor'], ctx['x0'], s['symmetry']
    spacing = s['spacing'] if s['row_mode'] == 'spacing' else 0.0
    for g in want:
        if not g.endswith('_rand') or g in ('scalp_rand', 'face_rand') or g in out:
            continue
        region = g[:-5]
        if region == 'torso':
            parts, centre = G.TORSO_PARTS, True
        elif region in G.LIMBS:
            parts, centre = (G.LIMBS[region][0],), False
            if sym and region.endswith('R') and G.mirror_of(g) in want:
                continue                       # mirrored from the left twin below
        else:
            continue
        rs = region_surface(ctx, parts) if centre else limb_region(ctx, region)
        twin = G.mirror_of(g)
        if rs is None:
            skipped += [x for x in (g, twin if sym else g) if x in want]
            continue
        n = _count(ctx, {g: max(counts.get(g, 0), counts.get(twin, 0))}, g, G.DEFAULT_COUNT['scatter'])
        ok, t = sample.adaptive_candidates(rs['attrs']['pur'], thr, floor)
        v, paired = sample.scatter(rs, ok, n, spacing, x0=x0, symmetric=centre and sym,
                                   seed=zlib.crc32(g.encode()))
        if not v:
            skipped.append(g)
            continue
        pts = _scatter_set(ctx, rs, v, paired, lambda f, b: np.tile(ax['up'], (len(f), 1)), x0)
        pts['thr'] = t
        out[g] = pts
        if twin != g and twin in want and sym:
            out[twin] = _mirror(pts, x0)
    return out, skipped


def region_masks(ctx, strategy=None):
    """Scalp and face masks in [0, 1] per triangle of the input mesh (0 off the head), for storing as
    the `os_region_<family>` face attributes."""
    hs = ctx['head']
    if hs is None:
        return {}
    hr = head_context(ctx, hs, strategy or ctx['s']['strategy_hair'])
    soft = hr['lm']['prm']['soft'] * hr['lm']['H']
    n_src = len(ctx['face_part'])
    out = {}
    for k in ('scalp', 'face'):
        fine = regions.mask_of(hr[k], soft)[hs['F']].mean(1)
        cnt = np.bincount(hs['fparent'], minlength=len(hs['src_faces']))
        val = np.bincount(hs['fparent'], fine, len(hs['src_faces'])) / np.maximum(cnt, 1)
        full = np.zeros(n_src)
        full[hs['src_faces']] = val
        out[k] = full
    return out


# ------------------------------------------------------------------ everything
def generate(ctx, counts=None, groups=None, template=None, texture_rgb=None, paint=None):
    """Generate the requested groups (default: those in `counts`, else every known group).

    Returns {'points': {group: {'pos','n','z','parent'}}, 'skipped': [group...], 'info': {...}}.
    Families switched off or set to 'keep' are not touched (they are not in `points`)."""
    s = ctx['s']
    counts = dict(counts or {})
    want = list(groups) if groups is not None else (list(counts) or list(G.default_groups()))
    if not counts:
        counts = G.default_groups()
    fam_on = {'hair': s['gen_hair'] and s['strategy_hair'] != 'keep',
              'scatter': s['gen_scatter'],
              'limbs': s['gen_limbs'] and s['strategy_limbs'] != 'keep',
              'torso': s['gen_torso'] and s['strategy_torso'] != 'keep'}
    want = [g for g in want if G.family_of(g) and fam_on[G.family_of(g)]]
    # a mirrored group also needs its twin generated
    if s['symmetry']:
        want = list(dict.fromkeys(want + [G.mirror_of(g) for g in want if G.mirror_of(g) in counts]))
    points, skipped, info = {}, [], {}
    head = ctx['head']
    hw = [g for g in want if G.family_of(g) == 'hair' or g in ('scalp_rand', 'face_rand')]
    if hw and head is not None:
        p, sk, inf = head_points(ctx, head, counts, hw, template, texture_rgb, paint)
        points.update(p)
        skipped += sk
        info.update(inf)
    elif hw:
        skipped += hw
    for fn in (limb_rows, torso_lines, body_scatter):
        p, sk = fn(ctx, counts, want)
        points.update(p)
        skipped += sk
    skipped = [g for g in dict.fromkeys(skipped) if g not in points]
    return {'points': points, 'skipped': skipped, 'info': info}


def matrices(points):
    """{group: 4x4 (k,4,4)} frames for a generated point dict."""
    return {g: frames.matrices(p['pos'], p['n'], p['z']) for g, p in points.items()}


def point_name(group, i):
    return 'AP_' + group if group in G.SINGLES else f'AP_{group}_{i:02d}'


def named_matrices(points):
    """{AP bone name: 4x4} with the old naming (`AP_<group>_<NN>`, singles unnumbered)."""
    out = {}
    for g, M in matrices(points).items():
        for i in range(len(M)):
            out[point_name(g, i)] = M[i]
    return out


# ------------------------------------------------------------------ library heads
def place_on_head(V, F, counts=None, settings=None, texture_rgb=None, template=None, paint=None,
                  axes=None, x0=0.0, exclude=None):
    """Hair, crown, temple, scalp and face points on an arbitrary head mesh (no rig, no weights).

    `V` (n,3), `F` (m,3 triangles) in the head's frame (+Z up, -Y forward, +X the character's left
    unless `axes` says otherwise). `texture_rgb` (n,3) is the texture colour per vertex for the
    texture strategy, `paint` (n,) vertex weights override the scalp region, `exclude` (n,) marks
    vertices that must stay free of hair/face points (ears, horns). Returns {'AP_<name>': 4x4 numpy matrix} (columns X, Y=normal, Z=tangent, position)
    plus the generation result under the key `'_result'` via `place_on_head_full`."""
    return place_on_head_full(V, F, counts, settings, texture_rgb, template, paint, axes, x0, exclude)['matrices']


def place_on_head_full(V, F, counts=None, settings=None, texture_rgb=None, template=None, paint=None,
                       axes=None, x0=0.0, exclude=None):
    V = np.asarray(V, float)
    F = np.asarray(F, np.int64)
    part = np.zeros(len(F), np.int64)
    body = {'V': V, 'F': F, 'part': part, 'part_names': [G.HEAD_PART], 'W': np.zeros((len(V), 0)),
            'groups': [], 'bones': {}, 'x0': x0}
    st = dict(settings or {})
    st.update({'gen_limbs': False, 'gen_torso': False, 'exclude_joints': False})
    if texture_rgb is not None:
        body['rgb'] = np.asarray(texture_rgb, float)
    if paint is not None:
        body['paint_scalp'] = np.asarray(paint, float)
    if exclude is not None:
        body['exclude'] = np.asarray(exclude, float)
    ctx = prepare(body, st, axes, x0)
    counts = counts or {g: c for g, c in G.default_groups(('hair', 'scatter')).items()
                        if g in G.SINGLES or g.startswith('scalp_') or g == 'face_rand'}
    want = [g for g in counts if g in G.SINGLES or g.startswith('scalp_') or g == 'face_rand']
    res = generate(ctx, counts, want, template)
    return {'matrices': named_matrices(res['points']), 'result': res, 'ctx': ctx}
