"""Tests for align_body.py (synthetic torso + two-segment arm, synthetic GLB). Run with characters/.venv/bin/python:

    python -m unittest tools/tripo_pipeline/test_align_body.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation as Rot

sys.path.insert(0, str(Path(__file__).resolve().parent))
import align_body as ab  # noqa: E402
import body_fit as bf  # noqa: E402
import test_body_fit as tbf  # noqa: E402

SHOULDER, ELBOW, WRIST = np.array([.12, 0, 1.4]), np.array([.42, 0, 1.4]), np.array([.72, 0, 1.4])


def tube(a, b, radius, n=20, rings=7, closed=True):
    """Closed outward-wound tube from point a to point b (caps included); returns V, F."""
    axis = (b - a) / np.linalg.norm(b - a)
    u = np.cross(axis, [0, 0, 1.]) if abs(axis[2]) < .9 else np.cross(axis, [1., 0, 0])
    u /= np.linalg.norm(u)
    v = np.cross(axis, u)
    ang = np.linspace(0, 2 * np.pi, n, endpoint=False)
    V = [a + t * (b - a) + radius * (np.cos(x) * u + np.sin(x) * v) for t in np.linspace(0, 1, rings) for x in ang]
    V += [a, b]
    F = []
    for r in range(rings - 1):
        for i in range(n):
            p, q = r * n + i, r * n + (i + 1) % n
            F += [[p, q, q + n], [p, q + n, p + n]]
    for i in range(n):
        j = (i + 1) % n
        F += [[len(V) - 2, j, i], [len(V) - 1, (rings - 1) * n + i, (rings - 1) * n + j]]
    return np.array(V), np.array(F)


def synthetic():
    """Torso + head + legs + a static right arm (all static) and a left arm made of upper arm + forearm along +x at z = 1.4 with the left arm chain
    rigged; returns (body, rig, number of static vertices before the arm)."""
    static = [tube(np.array([0, 0, .9]), np.array([0, 0, 1.6]), .13), tube(np.array([0, 0, 1.6]), np.array([0, .01, 1.82]), .09),
              tube(np.array([.1, 0, .0]), np.array([.1, .02, .9]), .07), tube(np.array([-.1, 0, .0]), np.array([-.1, -.02, .9]), .06),
              tube(np.array([-.12, 0, 1.4]), np.array([-.8, -.03, 1.4]), .04, rings=11)]
    arm = [tube(SHOULDER, ELBOW, .045, rings=11), tube(ELBOW, WRIST, .04, rings=11)]
    Vs, Fs, base = [], [], 0
    for V, F in static + arm:
        Vs.append(V)
        Fs.append(F + base)
        base += len(V)
    nt = sum(len(V) for V, _ in static)
    V, F = np.concatenate(Vs), np.concatenate(Fs)
    part = np.concatenate([np.full(len(F_), 0 if k < len(static) else k - len(static) + 1) for k, (_, F_) in enumerate(static + arm)]).astype(int)
    body = bf.Body(V, F, part, ['spine_01.x', 'arm_stretch.l', 'forearm_stretch.l'])
    W = np.zeros((len(V), 13))
    ia, ifo = 1 + ab.Rig.names.index('arm.l'), 1 + ab.Rig.names.index('forearm.l')
    x = V[:, 0]
    in_arm = np.arange(len(V)) >= nt
    # arm weight 1 beyond the shoulder (smooth ramp over 4 cm), forearm weight ramps in over +-3 cm around the elbow
    wa = np.clip((x - SHOULDER[0]) / .04, 0, 1) * in_arm
    wf = np.clip((x - (ELBOW[0] - .03)) / .06, 0, 1) * in_arm
    W[:, ia], W[:, ifo] = wa * (1 - wf), wf
    W[:, 0] = 1 - W[:, ia] - W[:, ifo]
    pivot = np.tile([5., 5., 5.], (12, 1))
    axis = np.tile([1., 0, 0], (12, 1))
    pivot[ia - 1], pivot[ifo - 1] = SHOULDER, ELBOW
    return body, ab.Rig(W, pivot, axis), nt


def surface_points(body, mask_parts, n, seed=0):
    rng = np.random.default_rng(seed)
    idx = np.flatnonzero(np.isin(body.part, mask_parts))
    return ab.sample_triangles(body.A, body.B, body.C, body.area, idx, n, rng)[0]


class StageOne(unittest.TestCase):
    def test_recovers_scale_rotation_translation(self):
        body, rig, _ = synthetic()
        P = surface_points(body, [0, 1, 2], 6000)
        c = np.array([0., 0., 1.2])
        true = np.r_[np.log([.95, 1.06, 1.01]), np.radians([.5, -.8, 3.]), [.005, -.008, .012]]       # Tripo -> base: Y = T(X)
        # build the Tripo-side points X = T^-1(P)
        R = Rot.from_rotvec(true[3:6]).as_matrix()
        X = ((P - c - true[6:9]) @ R) / np.exp(true[:3]) + c
        ev = {'pts': X, 'w': np.full(len(X), 1. / len(X)), 'hold': np.zeros(len(X), bool)}
        body.o = np.array([0., 0., 0.])       # centre of the stage-1 pivot comes from the body frame; irrelevant for the recovery
        r = ab.fit_stage1(body, ev)
        Y = ab.t1_apply(r['p'], X, r['c'])
        self.assertLess(body.closest(Y)[0].mean(), .3 * body.closest(X)[0].mean())          # a tube-like body is partly degenerate (sliding along limbs)
        self.assertLess(np.linalg.norm(Y - P, axis=1).mean(), 3e-3)
        np.testing.assert_allclose(np.exp(r['p'][:3]), [.95, 1.06, 1.01], atol=.04)

    def test_rotation_is_bounded(self):
        body, rig, _ = synthetic()
        P = surface_points(body, [0, 1, 2], 3000)
        R = Rot.from_rotvec([0, 0, np.radians(20.)]).as_matrix()
        ev = {'pts': P @ R.T, 'w': np.full(len(P), 1. / len(P)), 'hold': np.zeros(len(P), bool)}
        r = ab.fit_stage1(body, ev)
        self.assertLessEqual(np.degrees(np.abs(r['p'][3:6])).max(), ab.ROT_MAX_DEG + 1e-6)


class StageTwo(unittest.TestCase):
    def setUp(self):
        self.body, self.rig, self.nt = synthetic()
        self.rv = np.zeros((12, 3))
        self.ia, self.ifo = ab.Rig.names.index('arm.l'), ab.Rig.names.index('forearm.l')
        self.rv[self.ia] = np.radians([0., 3., 7.])
        self.rv[self.ifo] = np.radians([0., -2., 6.])

    def posed_skin(self, parts, n=5000):
        rng = np.random.default_rng(3)
        idx = np.flatnonzero(np.isin(self.body.part, parts))
        w = self.body.area[idx] / self.body.area[idx].sum()
        f = rng.choice(idx, size=n, p=w)
        u, v = rng.random(n), rng.random(n)
        flip = u + v > 1
        u[flip], v[flip] = 1 - u[flip], 1 - v[flip]
        bary = np.stack([1 - u - v, u, v], 1)
        x = np.einsum('ki,kij->kj', bary, np.stack([self.body.A[f], self.body.B[f], self.body.C[f]], 1))
        W = np.einsum('ki,kij->kj', bary, self.rig.W[self.body.F[f]])
        return x, self.rig.pose(x, W, self.rig.matrices(self.rv))

    def test_lbs_unpose_returns_a_posed_limb_to_rest(self):
        x, Y = self.posed_skin([1, 2])
        field = ab.Field(self.body, self.rig, np.zeros(9), np.zeros(3), self.rv)
        back = field.unpose(Y)
        err = np.linalg.norm(back - x, axis=1)
        mid = (x[:, 0] > .2) & (x[:, 0] < .36) | (x[:, 0] > .5) & (x[:, 0] < .68)       # away from the shoulder and the elbow blend
        self.assertLess(np.median(err[mid]), 3e-3)         # the kernel (4 % of the height, 73 mm here) is wider than this 9 cm thick test arm
        self.assertLess(err.mean(), 6e-3)
        self.assertLess(err.max(), 3e-2)

    def test_rotations_recovered_from_bare_skin(self):
        _, Y = self.posed_skin([1, 2])
        rv, info = ab.fit_stage2(self.body, self.rig, Y, np.full(len(Y), 1. / len(Y)), np.zeros((0, 3)))
        x, _ = self.posed_skin([1, 2], 2000)
        M0, M1 = self.rig.matrices(self.rv), self.rig.matrices(rv)
        W = np.zeros((len(x), 13))
        W[:, 0] = 1
        # the recovered pose puts the arm where the true one is (relative rotations of a chain can trade off; the world pose cannot)
        for c, (x0, x1) in enumerate([(.2, .36), (.5, .68)]):
            sel = (x[:, 0] > x0) & (x[:, 0] < x1)
            Wc = np.zeros((sel.sum(), 13))
            Wc[:, 1 + (self.ia if c == 0 else self.ifo)] = 1
            err = np.linalg.norm(self.rig.pose(x[sel], Wc, M0) - self.rig.pose(x[sel], Wc, M1), axis=1)
            self.assertLess(err.mean(), 4e-3)           # from ~25 mm; a tube leaves the swing of the distal bone only weakly constrained
        # channels without evidence stay at rest
        rest = [i for i in range(12) if i not in (self.ia, self.ifo)]
        self.assertLess(np.abs(rv[rest]).max(), 1e-6)
        rep = ab.bone_report(self.rig, rv, info)
        self.assertEqual(rep['arm.l']['confidence'], 'high')
        self.assertEqual(rep['thigh.r']['confidence'], 'low')

    def test_forearm_only_evidence_is_carried_by_the_chain(self):
        _, Y = self.posed_skin([2])
        rv, info = ab.fit_stage2(self.body, self.rig, Y, np.full(len(Y), 1. / len(Y)), np.zeros((0, 3)))
        posed = bf.Body(self.rig.pose(self.body.V, self.rig.W, self.rig.matrices(rv)), self.body.F, self.body.part, self.body.part_names)
        self.assertLess(posed.closest(Y)[0].mean(), 1.5e-3)
        rep = ab.bone_report(self.rig, rv, info)
        self.assertEqual(rep['forearm.l']['confidence'], 'high')
        self.assertIn(rep['arm.l']['confidence'], ('medium', 'high'))
        self.assertEqual(rep['hand.l']['confidence'], 'low')

    def test_twist_is_penalised(self):
        rv = np.zeros((12, 3))
        rv[self.ifo] = np.radians([20., 0., 0.])         # a pure twist about the bone axis (+x) is invisible on a round tube
        _, Y = self.posed_skin([2], 3000)
        rv2, _ = ab.fit_stage2(self.body, self.rig, Y, np.full(len(Y), 1. / len(Y)), np.zeros((0, 3)))
        self.assertLess(abs(np.degrees(np.dot(rv2[self.ifo], self.rig.axis[self.ifo]))), 1.)


class CoreFirst(unittest.TestCase):
    """The experimental `core` variant: stage 1 sees only head / neck / torso / pelvis; stage 2 adds bone lengths and chain-root offsets."""

    def evidence(self, shift_arm, torso_share):
        body, rig, nt = synthetic()
        names = ['spine_01.x', 'arm_stretch.l', 'forearm_stretch.l']
        torso = surface_points(body, [0], 3000, seed=1)
        arm = surface_points(body, [1, 2], 3000, seed=2) + np.array([0, shift_arm, 0.])      # a limb posed differently
        pts = np.concatenate([torso, arm])
        part = np.r_[np.zeros(len(torso), int), np.ones(len(arm), int)]
        ev = {'part_names': names, 'usable_area_m2': 1.0, 'eval': {'part': np.r_[np.zeros(int(100 * torso_share), int), np.ones(100 - int(100 * torso_share), int)]},
              'fit': {'pts': pts, 'w': np.full(len(pts), 1. / len(pts)), 'hold': np.zeros(len(pts), bool), 'part': part}}
        return body, rig, ev

    def test_limb_skin_does_not_move_the_core(self):
        body, _, ev = self.evidence(0., .5)
        base = ab.core_stage1(body, ev)
        _, _, ev2 = self.evidence(.06, .5)                  # the arm sits 6 cm off: a global fit of everything would shift / turn the body
        shifted = ab.core_stage1(body, ev2)
        np.testing.assert_allclose(shifted['p'], base['p'], atol=1e-9)
        self.assertEqual(base['mode'], 'strong')
        everything = ab.fit_stage1(body, ev2['fit'])
        self.assertGreater(np.abs(everything['p'] - base['p']).max(), 1e-3)

    def test_weak_core_pins_scale_and_rotation(self):
        body, _, ev = self.evidence(.04, .02)
        s1 = ab.core_stage1(body, ev)
        self.assertEqual(s1['mode'], 'weak')
        self.assertLessEqual(np.abs(np.exp(s1['p'][:3]) - 1).max(), ab.WEAK_SCALE_BOUNDS[1] - 1 + 1e-6)
        self.assertLessEqual(np.degrees(np.abs(s1['p'][3:6])).max(), ab.WEAK_ROT_MAX_DEG + 1e-6)

    def test_length_scale_and_root_offset_matrices(self):
        _, rig, _ = synthetic()
        i = ab.Rig.names.index('forearm.l')
        ls = np.ones(12)
        ls[i] = 1.05
        off = np.zeros((12, 3))
        ia = ab.Rig.names.index('arm.l')
        off[ia] = [.004, 0, -.006]
        M = rig.matrices(np.zeros((12, 3)), ls, off)
        p = rig.pivot[i] + rig.axis[i] * .2                 # a point .2 m along the bone from its pivot (arm chain moved by the root offset)
        moved = M[1 + i, :3, :3] @ p + M[1 + i, :3, 3]
        np.testing.assert_allclose(moved, rig.pivot[i] + rig.axis[i] * .2 * 1.05 + off[ia], atol=1e-12)
        np.testing.assert_allclose(M[1 + ab.Rig.names.index('thigh.l')], np.eye(4), atol=1e-12)

    def test_stage2_explains_a_longer_forearm_and_an_offset_root(self):
        body, rig, _ = synthetic()
        ia, ifo = ab.Rig.names.index('arm.l'), ab.Rig.names.index('forearm.l')
        ls = np.ones(12)
        ls[ifo] = 1.05
        off = np.zeros((12, 3))
        off[ia] = [0, .006, .004]
        rng = np.random.default_rng(3)
        idx = np.flatnonzero(np.isin(body.part, [1, 2]))
        P, f = ab.sample_triangles(body.A, body.B, body.C, body.area, idx, 5000, rng)
        # skin weights at the sample points: barycentric mix of the vertex weights of their triangle
        W = rig.W[body.F[f]].mean(1)
        Y = rig.pose(P, W, rig.matrices(np.zeros((12, 3)), ls, off))
        rv, info = ab.fit_stage2(body, rig, Y, np.full(len(Y), 1. / len(Y)), np.zeros((0, 3)), lengths=True)
        posed = bf.Body(rig.pose(body.V, rig.W, rig.matrices(rv, info['length_scale'], info['root_offset'])), body.F, body.part, body.part_names)
        self.assertLess(posed.closest(Y)[0].mean(), 1.5e-3)
        # (the length of a round tube is hardly observable: the pose is explained, the individual lengths stay near the prior)
        self.assertLessEqual(np.abs(info['length_scale'] - 1).max(), ab.LEN_MAX + 1e-9)
        self.assertLessEqual(np.abs(info['root_offset']).max(), ab.OFF_MAX + 1e-9)
        field = ab.Field(body, rig, np.zeros(9), np.zeros(3), rv, ls=info['length_scale'], off=info['root_offset'])
        self.assertFalse(field.identity)
        rep = ab.bone_report(rig, rv, info)
        self.assertIn('length_scale_pct', rep['forearm.l'])


class FieldProperties(unittest.TestCase):
    def setUp(self):
        self.body, self.rig, _ = synthetic()
        rv = np.zeros((12, 3))
        rv[ab.Rig.names.index('arm.l')] = np.radians([0, 2, 6])
        rv[ab.Rig.names.index('forearm.l')] = np.radians([0, -2, 5])
        self.field = ab.Field(self.body, self.rig, np.r_[np.log([.97, 1.04, 1.]), np.radians([0, 0, 2.]), [.003, -.004, .002]], np.array([0., 0., 1.2]), rv)

    def test_continuous_across_blend_zones(self):
        t = np.linspace(0, .6, 601)        # 1 mm steps along the arm 3 cm outside its axis, through the shoulder and elbow blends
        X = np.stack([.05 + t, np.zeros_like(t), np.full_like(t, 1.43)], 1)
        Y = self.field(X)
        step = np.linalg.norm(np.diff(Y, axis=0), axis=1)
        self.assertLess(step.max(), 3e-3)             # no jump: at most 3x the 1 mm input step

    def test_chunking_does_not_change_the_field(self):
        X = np.random.default_rng(4).uniform([-.2, -.2, .9], [.8, .2, 1.6], (500, 3))
        a = self.field.unpose(X)
        b = np.concatenate([self.field.unpose(X[i:i + 37]) for i in range(0, 500, 37)])
        np.testing.assert_allclose(a, b, atol=1e-12)

    def test_pieces_sharing_a_boundary_stay_together(self):
        seam = np.random.default_rng(5).uniform([.3, -.05, 1.35], [.5, .05, 1.45], (200, 3))      # boundary vertices of two pieces
        piece_a = np.concatenate([seam, seam[:5] + [0, 0, .1]])
        piece_b = np.concatenate([seam[::-1], seam[:7] - [0, .1, 0]])
        a, b = self.field(piece_a)[:200], self.field(piece_b)[:200][::-1]
        np.testing.assert_allclose(a, b, atol=1e-12)

    def test_wearable_field_drops_the_per_axis_scale_only(self):
        X = np.random.default_rng(7).uniform([-.2, -.2, .9], [.8, .2, 1.6], (60, 3))
        for mode, expect in (('axes', np.log([.97, 1.04, 1.])), ('uniform', np.full(3, np.log([.97, 1.04, 1.]).mean())), ('none', np.zeros(3))):
            w = self.field.wearable(mode)
            np.testing.assert_allclose(w.t1p[:3], expect, atol=1e-12)
            np.testing.assert_allclose(w.t1p[3:], self.field.t1p[3:])         # rotation + translation untouched
            np.testing.assert_allclose(w.rv, self.field.rv)                   # same stage 2
        self.assertEqual(self.field.t1p[0], np.log(.97))                      # the body-evidence field itself is not modified
        self.assertIs(self.field.wearable('axes'), self.field)
        # a point at the stage-1 centre is only moved by the translation (+ stage 2): the scale cannot shift it
        c = self.field.c
        np.testing.assert_allclose(self.field.wearable('none').t1(c[None])[0], c + self.field.t1p[6:9], atol=1e-12)
        self.assertGreater(np.abs(self.field.wearable('none')(X) - self.field(X)).max(), 1e-3)

    def test_record_round_trip_selects_the_wearable_scale(self):
        rec = {'stage1': {'scale': [.97, 1.04, 1.], 'rotvec_deg': [0, 0, 2.], 'translation_mm': [3, -4, 2], 'center': [0, 0, 1.2], 'wearable_scale_mode': 'uniform'},
               'stage2': {'bones': {n: {'rotvec_deg': [0, 0, 0]} for n in self.rig.names}}}
        rec['stage2']['bones']['arm.l']['rotvec_deg'] = [0, 2, 6]
        body_f = ab.Field.from_record(rec, self.body, self.rig)
        wear_f = ab.Field.from_record(rec, self.body, self.rig, wearable=True)
        np.testing.assert_allclose(np.exp(body_f.t1p[:3]), [.97, 1.04, 1.])
        np.testing.assert_allclose(np.exp(wear_f.t1p[:3]), np.exp(np.log([.97, 1.04, 1.]).mean()))
        del rec['stage1']['wearable_scale_mode']                                # an old (v1) record: per-axis for the wearable too
        np.testing.assert_allclose(np.exp(ab.Field.from_record(rec, self.body, self.rig, wearable=True).t1p[:3]), [.97, 1.04, 1.])

    def test_identity_when_nothing_is_fitted(self):
        f = ab.Field(self.body, self.rig, np.zeros(9), np.array([0., 0., 1.]), np.zeros((12, 3)))
        X = np.random.default_rng(6).uniform(-1, 1, (50, 3))
        np.testing.assert_allclose(f(X), X, atol=1e-12)


class Mirror(unittest.TestCase):
    def test_left_copy_is_the_exact_reflection_of_the_aligned_right_piece(self):
        body, rig, _ = synthetic()
        rv = np.zeros((12, 3))
        rv[ab.Rig.names.index('arm.l')] = np.radians([0, 2, 6])
        field = ab.Field(body, rig, np.r_[np.log([.97, 1.04, 1.]), np.radians([0, 0, 2.]), [.003, -.004, .002]], np.array([0., 0., 1.2]), rv)
        n = np.array([np.cos(.05), np.sin(.05), 0.])
        plane_cur = (n, .004)                                  # the wearable's own (tilted) mirror plane
        rng = np.random.default_rng(8)
        right = rng.uniform([-.8, -.1, .0], [-.2, .1, 1.5], (300, 3))
        left = ab.reflect(right, plane_cur)
        U = np.concatenate([right, left])
        mask = np.r_[np.zeros(300, bool), np.ones(300, bool)]
        out = ab.align_vertices(field, U, mask, plane_cur, 0.)
        np.testing.assert_allclose(ab.reflect(out[mask], 0.), out[~mask], atol=1e-9)
        np.testing.assert_allclose(out[~mask], field(right), atol=1e-12)         # the right piece is just the field

    def test_refine_plane_finds_the_plane_of_a_pair(self):
        rng = np.random.default_rng(9)
        right = rng.uniform([-.8, -.1, .0], [-.2, .1, 1.5], (400, 3))
        n = np.array([np.cos(.04), np.sin(.04), .01])
        n /= np.linalg.norm(n)
        left = ab.reflect(right, (n, .003))
        U = np.concatenate([right, left])
        mask = np.r_[np.zeros(400, bool), np.ones(400, bool)]
        plane = ab.refine_plane(U, mask, 0.)
        self.assertLess(ab.mirror_pair_error(U, mask, plane).max(), 1e-6)


class BakeToNative(unittest.TestCase):
    def test_aligned_glb_matches_the_field_and_keeps_everything_else(self):
        body, rig, _ = synthetic()
        body.o, body.H = bf.tf.rest_frame(body.V)
        rv = np.zeros((12, 3))
        rv[ab.Rig.names.index('arm.l')] = np.radians([0, 2, 6])
        field = ab.Field(body, rig, np.r_[np.log([.97, 1.04, 1.]), np.radians([0, 0, 2.]), [.003, -.004, .002]], np.array([0., 0., 1.2]), rv)
        prims = tbf.with_normals(tbf.shell_prims(body, inside=.2, outside=.22, z0=1.0, z1=1.5))
        with tempfile.TemporaryDirectory() as tmp:
            src, out = Path(tmp) / 'w.glb', Path(tmp) / 'w_aligned.glb'
            src.write_bytes(tbf.build_glb(prims))
            surf = bf.Surface(bf.Glb(src), tbf.MANIFEST, body)
            info = {}
            U_new, _, info = ab.aligned_surface(surf, field, None, 0., 0., out, info)
            after = bf.Surface(bf.Glb(out), tbf.MANIFEST, body)
            np.testing.assert_allclose(np.sort(after.U, axis=0), np.sort(U_new, axis=0), atol=2e-6)
            self.assertEqual(len(after.T), len(surf.T))
            a, b = bf.Glb(src), bf.Glb(out)
            self.assertEqual(a.js['materials'], b.js['materials'])
            self.assertEqual([p['def'].get('material') for p in a.prims], [p['def'].get('material') for p in b.prims])
            self.assertIsNotNone(b.normals(0))
            self.assertIn('strain', info)


def labelled_glb(body, names=('base', 'primary')):
    """A labelled-LOD-like GLB: two primitives (one per role), materials `names`, prim k uses material k."""
    import json
    import struct
    raw = tbf.build_glb(tbf.with_normals(tbf.shell_prims(body, inside=.2, outside=.22, z0=1.0, z1=1.5)))
    size, kind = struct.unpack_from('<II', raw, 12)
    js = json.loads(raw[20:20 + size])
    js['materials'] = [{'name': n} for n in names]
    for k, prim in enumerate(js['meshes'][0]['primitives']):
        prim['material'] = k
    text = json.dumps(js, separators=(',', ':')).encode()
    text += b' ' * (-len(text) % 4)
    rest = raw[20 + size:]
    out = struct.pack('<II', len(text), kind) + text + rest
    return struct.pack('<4sII', b'glTF', 2, 12 + len(out)) + out


class RedBody(unittest.TestCase):
    def setUp(self):
        self.body, self.rig, _ = synthetic()
        self.body.o, self.body.H = bf.tf.rest_frame(self.body.V)
        rv = np.zeros((12, 3))
        rv[ab.Rig.names.index('arm.l')] = np.radians([0, 2, 6])
        self.field = ab.Field(self.body, self.rig, np.r_[np.zeros(3), np.radians([0, 0, 2.]), [.003, -.004, .002]], np.array([0., 0., 1.2]), rv)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.glb = self.dir / 'lod.glb'
        self.glb.write_bytes(labelled_glb(self.body))
        g = bf.Glb(self.glb)
        self.n = [len(g.triangles(k)) for k in range(2)]

    def tearDown(self):
        self.tmp.cleanup()

    def colours(self, n_total, white=5, order=None):
        rgb = np.tile([.9, .1, .14], (n_total, 1))
        rgb[:white] = [.93, .93, .93]
        path = self.dir / 'c.npz'
        np.savez(path, glb_face_order=np.arange(n_total) if order is None else order, material_id=np.r_[np.zeros(self.n[0], int), np.ones(n_total - self.n[0], int)],
                 face_mean_source_rgb=rgb)
        return np.load(path)

    def test_only_the_base_material_faces_are_taken(self):
        P, anchor, note = ab.base_soup(bf.Glb(self.glb))
        self.assertEqual(P.shape, (self.n[0], 3, 3))
        self.assertFalse(anchor.any())
        g = bf.Glb(self.glb)
        np.testing.assert_allclose(P, g.positions(0)[g.triangles(0)])
        glb = labelled_glb(self.body, names=('primary', 'secondary'))              # no `base` material: nothing to show
        (self.dir / 'nobase.glb').write_bytes(glb)
        self.assertIsNone(ab.base_soup(bf.Glb(self.dir / 'nobase.glb')))

    def test_white_anchors_only_when_the_npz_matches_the_glb(self):
        total = sum(self.n)
        P, anchor, note = ab.base_soup(bf.Glb(self.glb), self.colours(total))
        self.assertEqual(int(anchor.sum()), 5)
        self.assertTrue(anchor[:5].all())
        P, anchor, note = ab.base_soup(bf.Glb(self.glb), self.colours(total + 2))        # two faces more in the npz (a face-count mismatch): all red
        self.assertFalse(anchor.any())
        self.assertIn('does not match', note)

    def test_overlay_glb_is_flat_shaded_red_and_white(self):
        P, _, _ = ab.base_soup(bf.Glb(self.glb))
        anchor = np.zeros(len(P), bool)
        anchor[:4] = True
        out = self.dir / 'body.glb'
        ab.write_body_glb(out, P, anchor)
        g = bf.Glb(out)
        self.assertEqual([m['name'] for m in g.js['materials']], ['tripo_body', 'tripo_body_anchor'])
        np.testing.assert_allclose(g.js['materials'][0]['pbrMetallicRoughness']['baseColorFactor'][:3], [230 / 255, 25 / 255, 35 / 255])
        self.assertEqual([len(g.triangles(k)) for k in range(2)], [len(P) - 4, 4])
        np.testing.assert_allclose(g.positions(1).reshape(-1, 3, 3), P[:4], atol=1e-6)
        n = g.normals(0)
        np.testing.assert_allclose(np.linalg.norm(n, axis=1), 1., atol=1e-5)
        np.testing.assert_allclose(n[0], n[1], atol=1e-6)                                      # flat: one normal per triangle
        ab.write_body_glb(self.dir / 'red.glb', P, np.zeros(len(P), bool))
        self.assertEqual(len(bf.Glb(self.dir / 'red.glb').prims), 1)                           # no anchors: one primitive

    def test_aligned_overlay_is_the_wearable_field_in_the_native_frame(self):
        P, _, _ = ab.base_soup(bf.Glb(self.glb))
        Pa = ab.body_to_aligned(P, self.field, tbf.MANIFEST, self.body)
        fit = tbf.MANIFEST['parts']['fit']
        world = lambda T: bf.tf.native_to_world(bf.gl_to_bl(T.reshape(-1, 3)), tbf.MANIFEST, fit, self.body.H, self.body.o)
        np.testing.assert_allclose(world(Pa), self.field(world(P)), atol=1e-9)
        self.assertGreater(np.abs(Pa - P).max(), 1e-3)
        ident = ab.Field(self.body, self.rig, np.zeros(9), np.array([0., 0., 1.]), np.zeros((12, 3)))
        np.testing.assert_allclose(ab.body_to_aligned(P, ident, tbf.MANIFEST, self.body), P, atol=1e-9)

    def test_write_bodies_per_level(self):
        module = self.dir / 'module'
        (module / 'reduced').mkdir(parents=True)
        (module / 'reduced' / 'lod_error_0p4.glb').write_bytes(self.glb.read_bytes())
        (module / 'reduced' / 'lod_error_0p2.glb').write_bytes((self.dir / 'lod.glb').read_bytes())
        (module / 'reduced' / 'lod_error_0p2_colors.npz').write_bytes(b'not an npz')               # unreadable colours: all red
        (module / 'reduced' / 'lod_error_0p1.glb').write_bytes(b'not a glb')                         # a broken level must not stop the others
        rec = ab.write_bodies(module, tbf.MANIFEST, self.body, self.field, ('0p4', '0p2', '0p1'))
        self.assertEqual(sorted(rec), ['0p1', '0p2', '0p4'])
        self.assertEqual(rec['0p4']['status'], 'done')
        self.assertEqual(rec['0p4']['faces'], self.n[0])
        self.assertEqual((rec['0p2']['status'], rec['0p2']['anchor_faces']), ('done', 0))
        self.assertEqual(rec['0p1']['status'], 'failed')
        self.assertTrue((module / 'reduced' / 'lod_error_0p4__tripo_body.glb').is_file())
        self.assertTrue((module / 'reduced' / 'lod_error_0p4__tripo_body_aligned.glb').is_file())
        self.assertTrue((module / 'reduced' / 'lod_error_0p2__tripo_body.glb').is_file())
        self.assertFalse((module / 'reduced' / 'lod_error_0p1__tripo_body.glb').exists())
        ab.write_bodies(module, tbf.MANIFEST, self.body, None, ('0p4',))                            # no field: only the as-converted overlay is (re)written
        self.assertTrue((module / 'reduced' / 'lod_error_0p4__tripo_body.glb').is_file())


class RigFile(unittest.TestCase):
    def test_group_channels(self):
        self.assertEqual(ab.channel_of_group('c_arm_twist.l'), ('arm', 'l'))
        self.assertEqual(ab.channel_of_group('c_forearm_stretch.r'), ('forearm', 'r'))
        self.assertEqual(ab.channel_of_group('hand.l'), ('hand', 'l'))
        self.assertEqual(ab.channel_of_group('toes_01.r'), ('foot', 'r'))
        self.assertEqual(ab.channel_of_group('c_surface_foot.l'), ('foot', 'l'))
        self.assertIsNone(ab.channel_of_group('shoulder.l'))
        self.assertIsNone(ab.channel_of_group('c_spine_01_bend.x'))

    @unittest.skipUnless((bf.BASE_CACHE / 'elf_male_rig.npz').is_file(), 'no exported rig')
    def test_real_rig(self):
        rig = ab.Rig.from_npz(bf.BASE_CACHE / 'elf_male_rig.npz')
        np.testing.assert_allclose(rig.W.sum(1), 1., atol=1e-5)
        names = ab.Rig.names
        self.assertGreater(rig.pivot[names.index('arm.l')][0], 0)           # anatomical left = +X
        self.assertLess(rig.pivot[names.index('arm.r')][0], 0)
        self.assertGreater(rig.pivot[names.index('hand.l')][0], rig.pivot[names.index('forearm.l')][0])
        np.testing.assert_allclose(np.linalg.norm(rig.axis, axis=1), 1., atol=1e-9)
        self.assertEqual(len(rig.W), len(bf.get_body('elf_male').V))


if __name__ == '__main__':
    unittest.main()
