"""Quality report of a set of attachment points (SPEC section 5), for new or existing points alike.

    mirror pairing error   max distance from a point's reflection to the nearest point of its twin group
    spacing CV             coefficient of variation of the nearest-neighbour *surface* distance per group
    scalp points in face   scalp groups whose points lie in the face region
    joint points           points where the largest rigid-segment weight is below the purity threshold
"""
import numpy as np

from . import api, geodesic, groups as G, mesh

CV_LIMIT = 0.25
MIRROR_LIMIT_MM = 1.0
PURITY_TOL = 0.03          # a mirrored point is judged on the other side's own weights, which differ slightly
HEAD_GROUPS = ('crown', 'templeL', 'templeR', 'scalp_rand', 'face_rand')


def is_head_group(g):
    return g in HEAD_GROUPS or g.startswith('scalp_')


def group_surface(ctx, g):
    """The surface a group lives on (head, limb part or torso), or None."""
    if is_head_group(g):
        return ctx['head']
    if g.startswith('torso_'):
        return api.region_surface(ctx, G.TORSO_PARTS)
    region = g.rsplit('_', 1)[0]
    if region in G.LIMBS:
        return api.region_surface(ctx, (G.LIMBS[region][0],))
    return None


def locate(surf, P):
    """Closest surface point of each position: (face, bary, distance)."""
    _, f, b, d = mesh.closest_points(surf['V'], surf['F'], P)
    return f, b, d


def from_rig(old, thr=None, groups=None):
    """Points dict (group -> {'pos', 'thr'}) from `existing_points`-style entries, in index order.
    `thr` maps group -> the purity threshold it was generated with; `groups` restricts the groups."""
    by = {}
    for name, p in old.items():
        if groups is None or p['group'] in groups:
            by.setdefault(p['group'], []).append((-1 if p['index'] is None else p['index'], p['head']))
    out = {g: {'pos': np.array([h for _, h in sorted(v, key=lambda t: t[0])])} for g, v in by.items()}
    for g, t in (thr or {}).items():
        if g in out:
            out[g]['thr'] = t
    return out


def mirror_error(points, x0=0.0):
    """(max error in metres, {group: error}) over groups whose twin is present."""
    by, worst = {}, 0.0
    for g, p in points.items():
        tw = G.mirror_of(g)
        if tw not in points or G.family_of(g) is None:
            continue
        R = mesh.reflect(p['pos'], x0)
        Q = points[tw]['pos']
        d = np.linalg.norm(R[:, None, :] - Q[None], axis=2).min(1).max()
        by[g] = float(d)
        worst = max(worst, float(d))
    return worst, by


def spacing_cv(ctx, points, groups=None):
    """{group: (cv, mean distance)}. Scatter groups: nearest-neighbour distance measured along the
    surface; rows: distance between consecutive points (a row should be equally spaced)."""
    out = {}
    for g, p in points.items():
        if (groups is not None and g not in groups) or len(p['pos']) < 3 or g in G.SINGLES:
            continue
        if g.endswith('_rand'):
            surf = group_surface(ctx, g)
            if surf is None:
                continue
            f, b, _ = locate(surf, p['pos'])
            v = surf['F'][f][np.arange(len(f)), b.argmax(1)]
            d = geodesic.nn_distances(surf['g'], v, start=2 * surf['edge'])
        else:
            d = np.linalg.norm(np.diff(p['pos'], axis=0), axis=1)
        d = d[np.isfinite(d)]
        if len(d) >= 2 and d.mean() > 0:
            out[g] = (float(d.std() / d.mean()), float(d.mean()))
    return out


def scalp_in_face(ctx, points):
    """Number of scalp-group points inside the face region (landmark strategy as reference)."""
    hs = ctx['head']
    if hs is None:
        return 0, {}
    hr = api.head_context(ctx, hs, 'landmark_geodesic')
    n, by = 0, {}
    for g, p in points.items():
        if not (g.startswith('scalp_') or g in ('crown', 'templeL', 'templeR')):
            continue
        f, b, _ = locate(hs, p['pos'])
        m = mesh.bary_eval(hs, hr['face'], f, b)
        k = int((m > 0).sum())
        if k:
            by[g] = k
        n += k
    return n, by


def joint_points(ctx, points):
    """Number of points where the dominant segment's weight share is below the purity threshold the
    group was generated with (the configured one for points that carry none)."""
    n, by = 0, {}
    for g, p in points.items():
        surf = group_surface(ctx, g)
        if surf is None:
            continue
        f, b, _ = locate(surf, p['pos'])
        pur = mesh.bary_eval(surf, surf['attrs']['pur'], f, b)
        k = int((pur < p.get('thr', ctx['s']['joint_purity']) - PURITY_TOL).sum())
        if k:
            by[g] = k
        n += k
    return n, by


def off_surface(ctx, points):
    """Largest distance of any point from the surface its group lives on (metres) and per group."""
    by = {}
    for g, p in points.items():
        surf = group_surface(ctx, g)
        if surf is not None:
            by[g] = float(locate(surf, p['pos'])[2].max())
    return max(by.values(), default=0.0), by


def report(ctx, points, scatter_groups=None):
    """The full report as plain Python (JSON-serialisable), with pass/fail flags."""
    mirror, mirror_by = mirror_error(points, ctx['x0'])
    cv = spacing_cv(ctx, points)
    sc = {g: v for g, v in cv.items() if g.endswith('_rand')}
    rows = {g: v for g, v in cv.items() if g not in sc}
    face_n, face_by = scalp_in_face(ctx, points)
    joint_n, joint_by = joint_points(ctx, points)
    off, off_by = off_surface(ctx, points)
    rep = {
        'counts': {g: int(len(p['pos'])) for g, p in sorted(points.items())},
        'mirror_mm': {'max': mirror * 1000, 'by_group': {g: v * 1000 for g, v in mirror_by.items()}},
        'spacing_cv': {'scatter_max': max((v[0] for v in sc.values()), default=0.0),
                       'scatter_mean': float(np.mean([v[0] for v in sc.values()])) if sc else 0.0,
                       'rows_max': max((v[0] for v in rows.values()), default=0.0),
                       'by_group': {g: v[0] for g, v in cv.items()}},
        'scalp_in_face': {'count': face_n, 'by_group': face_by},
        'joint_points': {'count': joint_n, 'by_group': joint_by},
        'off_surface_mm': {'max': off * 1000, 'by_group': {g: v * 1000 for g, v in off_by.items() if v > 1e-4}},
        'relaxed': {g: p['thr'] for g, p in sorted(points.items())
                    if 0 < p.get('thr', 0) < ctx['s']['joint_purity'] - 1e-9},
    }
    rep['pass'] = {'mirror': rep['mirror_mm']['max'] < MIRROR_LIMIT_MM,
                   'scatter_cv': rep['spacing_cv']['scatter_max'] < CV_LIMIT,
                   'scalp_in_face': face_n == 0,
                   'joints': joint_n == 0 or not ctx['s']['exclude_joints']}
    return rep


def format_report(rep):
    """A few lines for the UI/log."""
    p = rep['pass']
    flag = lambda k: 'ok' if p[k] else 'FAIL'  # noqa: E731
    return '\n'.join([
        f"mirror max {rep['mirror_mm']['max']:.3f} mm [{flag('mirror')}]",
        f"scatter spacing CV max {rep['spacing_cv']['scatter_max']:.3f} (mean {rep['spacing_cv']['scatter_mean']:.3f}) "
        f"[{flag('scatter_cv')}], rows max {rep['spacing_cv']['rows_max']:.3f}",
        f"scalp points in face region {rep['scalp_in_face']['count']} [{flag('scalp_in_face')}]",
        f"points at joints {rep['joint_points']['count']} [{flag('joints')}]",
    ])
