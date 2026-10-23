"""Attachment-point placement on synthetic shapes (plain python3 + numpy, no bpy).

    python3 tools/outfit_studio/tests/test_ap.py
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))     # `ap` without importing the add-on
import ap                                                                  # noqa: E402
from ap import curves, frames, geodesic, mesh, quality, regions, sample    # noqa: E402


def icosphere(level=3, r=1.0):
    t = (1 + 5 ** .5) / 2
    V = np.array([[-1, t, 0], [1, t, 0], [-1, -t, 0], [1, -t, 0], [0, -1, t], [0, 1, t], [0, -1, -t], [0, 1, -t],
                  [t, 0, -1], [t, 0, 1], [-t, 0, -1], [-t, 0, 1]], float)
    F = np.array([[0, 11, 5], [0, 5, 1], [0, 1, 7], [0, 7, 10], [0, 10, 11], [1, 5, 9], [5, 11, 4], [11, 10, 2],
                  [10, 7, 6], [7, 1, 8], [3, 9, 4], [3, 4, 2], [3, 2, 6], [3, 6, 8], [3, 8, 9], [4, 9, 5],
                  [2, 4, 11], [6, 2, 10], [8, 6, 7], [9, 8, 1]])
    V, F, _, _ = mesh.subdivide(V / np.linalg.norm(V[0]), F, level)
    return V / np.linalg.norm(V, axis=1, keepdims=True) * r, F


def capsule(radius=.12, length=.7, level=3):
    """A vertical capsule (stretched sphere), the stand-in for a limb or torso."""
    V, F = icosphere(level, radius)
    V[:, 2] += np.where(V[:, 2] >= 0, length / 2, -length / 2)
    return V, F


def head_shape(level=3):
    """An ellipsoid skull with a nose bump; forward is -Y, up +Z, left +X."""
    V, F = icosphere(level, 1.0)
    V = V * np.array([.09, .11, .12])
    nose = np.exp(-(((V[:, 0]) / .03) ** 2 + ((V[:, 2] + .01) / .04) ** 2)) * (V[:, 1] < 0)
    V[:, 1] -= .035 * nose
    V[:, 2] += .12
    return V, F


def test_surface_basics():
    V, F = icosphere(3)
    assert abs(mesh.face_areas(V, F).sum() - 4 * np.pi) < .1
    g = mesh.graph(V, F)
    d = geodesic.dijkstra(g, [int(V[:, 2].argmax())])
    assert abs(d.max() - np.pi) < .02, d.max()                      # pole to pole along the surface
    n = mesh.vertex_normals(V, F)
    assert np.abs((n * V).sum(1) - 1).max() < .02
    p, f, b, dist = mesh.closest_points(V, F, V[:5] * 1.2)
    assert np.abs(dist - .2).max() < .01 and np.allclose(b.sum(1), 1)
    print('ok surface basics')


def test_isoline_rows_equal_arc_length():
    V, F = icosphere(3)
    S = mesh.make_surface(V, F, level=1)
    chains = curves.isolines(S['V'], S['F'], S['V'][:, 0], 0.0)          # the great circle x = 0
    ch = max(chains, key=curves.length)
    assert abs(curves.length(ch) - 2 * np.pi) < .02
    s, P, f, b = curves.resample(ch, 0, curves.length(ch), 13, False)[0:4]
    # arc length between consecutive samples measured on the dense polyline
    dense_s, dense_P, _, _ = curves.dense(ch, .002)
    idx = np.searchsorted(dense_s, s)
    arcs = np.diff(dense_s[idx])
    assert arcs.std() / arcs.mean() < 1e-2, arcs
    assert np.abs(np.linalg.norm(P, axis=1) - 1).max() < 5e-3        # samples sit on the surface
    print('ok isoline rows equal arc length')


def test_scatter_spacing_and_mirror():
    V, F = icosphere(3)
    S = mesh.make_surface(V, F, level=0)
    for n in (12, 24, 25):
        for sym in (False, True):
            v, paired = sample.scatter(S, np.ones(len(V), bool), n, symmetric=sym)
            P = V[v]
            if sym:
                P = np.vstack([P, mesh.reflect(P[np.array(paired)], 0.0)])
                assert len(P) == n, (len(P), n)
                R = mesh.reflect(P, 0.0)
                err = np.linalg.norm(R[:, None] - P[None], axis=2).min(1).max()
                assert err < 1e-6, err
            vv = np.array(v)
            d = geodesic.nn_distances(S['g'], vv, start=.3)
            if not sym:
                assert d.std() / d.mean() < .25, d.std() / d.mean()
            else:
                # the mirror images are not vertices; measure on the reflected picks too
                allv = np.concatenate([vv, sample.mirror_map(V, 0.0, 0, .3 * S['edge'])[vv[np.array(paired)]]])
                d = geodesic.nn_distances(S['g'], allv, start=.3)
                assert d.std() / d.mean() < .25, (n, d.std() / d.mean())
    print('ok scatter spacing CV < 0.25 and mirror error < 1e-6')


def test_frames_orthonormal():
    V, F = icosphere(3)
    S = mesh.make_surface(V, F, level=1, normal_radius=.2)
    f, b = sample.vertex_bary(S, np.arange(0, len(S['V']), 7))
    P, n = sample.at(S, f, b)
    z = frames.project_tangent(n, np.tile([0., 0, 1], (len(n), 1)), np.tile([1., 0, 0], (len(n), 1)))
    M = frames.matrices(P, n, z)
    R = M[:, :3, :3]
    assert np.abs(np.einsum('kji,kjl->kil', R, R) - np.eye(3)).max() < 1e-9
    assert np.allclose(np.linalg.det(R), 1.0)
    assert np.allclose(M[:, :3, 1], n, atol=1e-9)                       # +Y is the normal
    print('ok frames orthonormal')


def capsule_body():
    """A capsule used as both torso (spine part) and `arm_stretch.l`-less test body."""
    V, F = capsule()
    n = len(V)
    return {'V': V, 'F': F, 'part': np.zeros(len(F), int), 'part_names': ['spine_01.x'], 'W': np.ones((n, 1)),
            'groups': ['c_spine_01_bend.x'],
            'bones': {'spine_01.x': (np.array([0, 0, .0]), np.array([0, 0, .35]))}}


def test_torso_lines_on_capsule():
    body = capsule_body()
    ctx = ap.prepare(body, {'symmetry': True})
    counts = {f'torso_{t}': 8 for t in ap.groups.TORSO_ANGLE}
    counts['torso_rand'] = 24
    res = ap.generate(ctx, counts)
    pts = res['points']
    assert not [g for g in res['skipped'] if g.startswith('torso')], res['skipped']
    for t in ap.groups.TORSO_ANGLE:
        P = pts[f'torso_{t}']['pos']
        assert len(P) == 8
        seg = np.linalg.norm(np.diff(P, axis=0), axis=1)
        assert seg.std() / seg.mean() < .05, (t, seg)                    # equal spacing along the line
    for g in ('torso_fl', 'torso_l', 'torso_bl'):
        A, B = pts[g]['pos'], pts[ap.mirror_of(g)]['pos']
        assert np.abs(mesh.reflect(A, 0.0) - B).max() < 1e-6
    rep = quality.report(ctx, pts)
    assert rep['mirror_mm']['max'] < 1e-3 and rep['pass']['scatter_cv'], rep
    # front line (angle 0) is on the -Y side, left line on +X
    assert pts['torso_f']['pos'][:, 1].mean() < -.1 and pts['torso_l']['pos'][:, 0].mean() > .1
    print('ok torso lines on a capsule')


def test_head_on_ellipsoid():
    V, F = head_shape()
    out = ap.place_on_head_full(V, F, settings={})
    M = out['matrices']
    res = out['result']
    names = set(M)
    for need in ('AP_crown', 'AP_templeL', 'AP_templeR', 'AP_scalp_c_00', 'AP_scalp_l1_00', 'AP_scalp_r2_11',
                 'AP_scalp_rand_00', 'AP_face_rand_00'):
        assert need in names, need
    for nm, m in M.items():
        R = m[:3, :3]
        assert np.abs(R.T @ R - np.eye(3)).max() < 1e-6, nm
    pts = res['points']
    assert np.abs(mesh.reflect(pts['scalp_l1']['pos'], 0.0) - pts['scalp_r1']['pos']).max() < 1e-6
    assert pts['crown']['pos'][0, 2] > V[:, 2].max() - .01
    # rows run front -> back and stay above the hairline; the face region stays free of scalp points
    ctx = out['ctx']
    rep = quality.report(ctx, pts)
    assert rep['scalp_in_face']['count'] == 0, rep['scalp_in_face']
    for r in ('c', 'l1', 'l2'):
        P = pts[f'scalp_{r}']['pos']
        assert P[0, 1] < P[-1, 1], r
    print('ok head placement on an ellipsoid skull')


def test_regions_strategies():
    V, F = head_shape()
    S = mesh.make_surface(V, F, level=2)
    lm = regions.landmarks(S)
    for strat in ('landmark_geodesic', 'legacy'):
        r = regions.head_regions(S, strat)
        assert (r['scalp'] > 0).any() and (r['face'] > 0).any(), strat
        assert not ((r['scalp'] > 0) & (r['face'] > 0)).any(), strat
    r = regions.head_regions(S, 'landmark_geodesic')
    ref = {'V': S['V'], 'scalp': r['scalp'] > 0, 'face': r['face'] > 0}
    t = regions.head_regions(S, 'template', template=ref)
    agree = ((t['scalp'] > 0) == (r['scalp'] > 0)).mean()
    assert agree > .98, agree
    rgb = np.where((S['V'][:, 2] > .12 + .02)[:, None], [.1, .08, .05], [.9, .7, .6])      # dark hair above, skin below
    tx = regions.head_regions(S, 'texture', texture_rgb=rgb)
    assert tx['info']['strategy'] == 'texture' and (tx['scalp'] > 0).any()
    assert regions.head_regions(S, 'texture')['info']['strategy'] == 'landmark_geodesic'     # no texture: falls back
    assert lm['crown'] == int(S['V'][:, 2].argmax()) or S['V'][lm['crown'], 2] > S['V'][:, 2].max() - 1e-3
    print('ok region strategies')


def test_exclude_and_paint_override():
    V, F = head_shape()
    ex = ((V[:, 0] > .05) & (V[:, 2] > .12)).astype(float)                      # an 'ear' patch on the left
    pts = ap.place_on_head_full(V, F, exclude=ex)['result']['points']
    for g, p in pts.items():
        if g.startswith('scalp_') or g == 'face_rand':
            bad = (np.abs(p['pos'][:, 0]) > .056) & (p['pos'][:, 2] > .14)
            assert not bad.any(), (g, p['pos'][bad])
    paint = (V[:, 2] > .2).astype(float)                                        # painted scalp: only the top cap
    out = ap.place_on_head_full(V, F, paint=paint)
    assert out['result']['info']['hair_region']['strategy'].endswith('+paint')
    for g, p in out['result']['points'].items():
        if g.startswith('scalp_'):
            assert p['pos'][:, 2].min() > .2 - .01, (g, p['pos'][:, 2].min())
    print('ok exclude and paint override')


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
    print('ALL OK')
