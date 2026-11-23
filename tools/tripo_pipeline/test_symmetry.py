"""Plain-numpy tests of symmetry.py (plane fit and region statistics); no Blender needed.

    python3 -m unittest tools/tripo_pipeline/test_symmetry.py

The synthetic "surface" is a point cloud that is exactly mirror-symmetric about a known tilted, shifted plane, plus
asymmetric extras; nearest-surface distance is brute force.
"""
import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import symmetry as sym  # noqa: E402


def brute_force_distance(cloud):
    cloud = cloud.astype(np.float32)
    sq = (cloud ** 2).sum(1)

    def dist(points):
        points = np.asarray(points, np.float32)
        out = np.empty(len(points))
        for i in range(0, len(points), 400):
            chunk = points[i:i + 400]
            d2 = (chunk ** 2).sum(1)[:, None] + sq[None] - 2 * chunk @ cloud.T
            out[i:i + 400] = np.sqrt(np.maximum(d2.min(1), 0))
        return out
    return dist


def symmetric_cloud(plane, rng, count=3000):
    """Half a figure (torso blob, arm blob, head blob with random offsets) on the +n side, mirrored exactly."""
    half = np.concatenate([
        rng.normal([.45, 0., 1.1], [.18, .12, .35], (count // 3, 3)),     # torso half
        rng.normal([1.0, 0., 1.5], [.30, .05, .06], (count // 3, 3)),     # arm
        rng.normal([.15, .02, 1.85], [.08, .09, .12], (count // 3, 3))])  # head half
    base = sym.Plane.from_x(0.)
    full = np.concatenate([half, base.reflect(half)])
    # rigidly rotate/shift the symmetric figure so its mirror plane is `plane`
    a = np.array([1., 0., 0.])
    axis = np.cross(a, plane.n)
    s, c = np.linalg.norm(axis), float(a @ plane.n)
    if s < 1e-12:
        rot = np.eye(3)
    else:
        k = axis / s
        K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
        rot = np.eye(3) + K * s + K @ K * (1 - c)
    return full @ rot.T + plane.n * plane.d


class PlaneFit(unittest.TestCase):
    def test_reflect_is_an_involution(self):
        p = sym.Plane.from_angles(.05, -.03, .07)
        pts = np.random.default_rng(1).normal(size=(50, 3))
        np.testing.assert_allclose(p.reflect(p.reflect(pts)), pts, atol=1e-12)
        np.testing.assert_allclose(p.signed(p.reflect(pts)), -p.signed(pts), atol=1e-12)

    def test_recovers_tilted_shifted_plane_with_asymmetric_extras(self):
        truth = sym.Plane.from_angles(math.radians(2.5), math.radians(-1.5), .04)
        rng = np.random.default_rng(3)
        cloud = symmetric_cloud(truth, rng)
        extras = rng.normal([-.7, -.05, 1.3], [.12, .04, .15], (450, 3))          # one-sided gear, 13% of the points
        cloud = np.concatenate([cloud, extras])
        queries = cloud[rng.choice(len(cloud), 700, replace=False)]
        fitted, info = sym.fit_plane(brute_force_distance(cloud), queries, inits=[('x0', sym.Plane.from_x(0.))],
                                     scan=.1, scan_step=.02)
        self.assertLess(abs(fitted.x_at(0, 1) - truth.x_at(0, 1)), .004)          # < 0.2% of height
        self.assertLess(abs(math.degrees(fitted.yaw - truth.yaw)), .5)
        self.assertLess(abs(math.degrees(fitted.pitch - truth.pitch)), .5)
        self.assertLess(info['cost'], info['starts']['scan']['cost_start'])

    def test_exactly_symmetric_cloud_has_zero_cost_at_the_truth(self):
        truth = sym.Plane.from_angles(.02, .01, -.03)
        cloud = symmetric_cloud(truth, np.random.default_rng(5), 1500)
        dist = brute_force_distance(cloud)
        self.assertLess(sym.trimmed_mean(dist(truth.reflect(cloud[::5]))), 1e-4)
        self.assertGreater(sym.trimmed_mean(dist(sym.Plane.from_x(0.).reflect(cloud[::5]))), 1e-2)


class Stats(unittest.TestCase):
    def synthetic_rec(self):
        rng = np.random.default_rng(0)
        n = 1000
        rec = {'p': rng.random((n, 3)) * [1, 1, 2], 'n': np.tile([0., -1., 0.], (n, 1)), 'geo': rng.random(n) * .01,
               'nagree': np.ones(n), 'role': np.zeros(n, int), 'weight': np.ones(n), 'role_p': np.zeros(n, int),
               'weight_p': np.ones(n), 'vis': np.ones(n), 'vis_p': np.ones(n), 'part': np.zeros(n, int),
               'part_p': np.zeros(n, int)}
        return rec

    def test_family_names_and_sides(self):
        self.assertEqual(sym.part_family('arm_stretch.l'), 'arm_stretch')
        self.assertEqual(sym.part_family('head.x'), 'head')
        self.assertEqual(sym.part_family('skirt'), 'skirt')
        self.assertEqual([sym.part_side(n) for n in ('hand.l', 'hand.r', 'neck.x', 'skirt')], [1, 2, 0, 0])

    def test_one_sided_gauntlet_is_expected_not_flagged(self):
        rec = self.synthetic_rec()
        names = ['hand.l', 'hand.r', 'spine_01.x']
        rec['part'] = np.array([0, 1, 2] * 333 + [2])
        rec['part_p'] = np.array([1, 0, 2] * 333 + [2])
        right = rec['part'] == 1
        rec['role'][right] = 1                    # garment on the right hand only
        rec['role_p'][rec['part_p'] == 1] = 1     # ... and its partner on the left hand sees the garment
        rec['role'][rec['part'] == 0] = 0
        out = sym.summarise(rec, names, 'right_gauntlet_boot', 2)
        self.assertIn('hand', out['by_family'])
        self.assertTrue(out['by_family']['hand']['expected_asymmetric'])
        self.assertEqual(out['by_family']['hand']['flags'], [])
        self.assertEqual(out['asymmetric_regions'], [])
        self.assertIsNotNone(out['expected_asymmetric'])
        self.assertAlmostEqual(out['global']['colour']['agree'], 1.0)

    def test_same_asymmetry_on_body_armor_is_flagged(self):
        rec = self.synthetic_rec()
        names = ['hand.l', 'hand.r', 'spine_01.x']
        rec['part'] = np.array([0, 1, 2] * 333 + [2])
        rec['part_p'] = np.array([1, 0, 2] * 333 + [2])
        rec['role'][rec['part'] == 1] = 1
        rec['role_p'][rec['part_p'] == 1] = 1
        out = sym.summarise(rec, names, 'body_armor', 2)
        self.assertEqual([r['family'] for r in out['asymmetric_regions']], ['hand'])
        self.assertIn('colour', out['asymmetric_regions'][0]['flags'])

    def test_unseen_disagreement_is_not_flagged(self):
        rec = self.synthetic_rec()
        names = ['spine_01.x']
        rec['role'][:] = 1
        rec['vis_p'][:] = 0.                     # the partner side was never seen
        out = sym.summarise(rec, names, 'body_armor', 2)
        self.assertEqual(out['asymmetric_regions'], [])
        self.assertEqual(out['global']['colour']['well_seen_compared'], 0)
        self.assertEqual(out['global']['colour']['poorly_seen_disagree'], 1.0)


if __name__ == '__main__':
    unittest.main()
