"""Tests of symmetry_cut.py on synthetic meshes (plain python: numpy + scipy).

    characters/.venv/bin/python -m unittest tools/tripo_pipeline/test_symmetry_cut.py
"""
import sys
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import face_labels as fl  # noqa: E402
import symmetry as sym  # noqa: E402
import symmetry_cut as sc  # noqa: E402


def grid(nx, ny, x0=0., x1=1., y0=0., y1=1.):
    """Triangulated plane z = 0 (normals +Z), (nx + 1) x (ny + 1) vertices."""
    xs, ys = np.linspace(x0, x1, nx + 1), np.linspace(y0, y1, ny + 1)
    v = np.array([(x, y, 0.) for y in ys for x in xs])
    idx = lambda i, j: j * (nx + 1) + i
    f = []
    for j in range(ny):
        for i in range(nx):
            a, b, c, d = idx(i, j), idx(i + 1, j), idx(i + 1, j + 1), idx(i, j + 1)
            f += [(a, b, c), (a, c, d)]
    return v, np.array(f, np.int64)


class CutMesh(unittest.TestCase):
    def test_iso_cut_is_exact_and_clean(self):
        v, f = grid(10, 10)
        u = v[:, 0] * 1.0 + .013 * v[:, 1]                       # a tilted linear field: iso 0.5 is a slanted straight line
        v2, f2, parent, cut = sc.cut_mesh(v, f, u, .5)
        vc, fc = sc.compact(v2, f2)
        self.assertTrue(np.all(sc.tri_area(vc, fc) > 0))
        # garment side only: every kept vertex satisfies u >= 0.5 (cut vertices lie on the iso-line)
        u2 = vc[:, 0] + .013 * vc[:, 1]
        self.assertGreaterEqual(float(u2.min()), .5 - 2e-4)      # cut vertices are kept 1e-3 of an edge off a crossing at a vertex
        # exact area of {x + 0.013 y >= 0.5} in the unit square
        want = 0.5 + .013 * .5                                   # integral over y of (1 - (0.5 - .013 y))
        self.assertAlmostEqual(float(sc.tri_area(vc, fc).sum()), want, places=3)
        # normals stay +Z (winding preserved) and the open boundary is one chain between the cut ends plus the square's border
        n = fl.face_normals(vc, fc)[0]
        self.assertTrue(np.all(n[:, 2] > .999))
        self.assertTrue(np.all(parent >= 0) and parent.max() < len(f))

    def test_no_crossing_keeps_everything_or_nothing(self):
        v, f = grid(4, 4)
        v2, f2, parent, _ = sc.cut_mesh(v, f, np.ones(len(v)), .5)
        self.assertEqual(len(f2), len(f))
        v2, f2, parent, _ = sc.cut_mesh(v, f, np.zeros(len(v)), .5)
        self.assertEqual(len(f2), 0)


class MirrorCopy(unittest.TestCase):
    def test_reflection_flips_winding_and_keeps_outward_normals(self):
        # open tetrahedron-like cap on the x < 0 side
        v = np.array([[-1., 0, 0], [-2., 1, 0], [-2., -1, 0], [-1.5, 0, 1.]])
        f = np.array([[0, 1, 3], [1, 2, 3], [2, 0, 3]])
        plane = sym.Plane.from_x(.1)
        vm, fm, rm = sc.mirror_copy(v, f, np.array([1, 2, 1]), plane)
        n0 = fl.face_normals(v, f)[0]
        n1 = fl.face_normals(vm, fm)[0]
        self.assertTrue(np.allclose(n1, plane.reflect_vectors(n0), atol=1e-9))
        self.assertTrue(np.allclose(plane.reflect(vm), v))
        self.assertTrue(np.array_equal(rm, [1, 2, 1]))

    def test_assemble_welds_touching_pieces(self):
        v, f = grid(2, 2, 0., 1.)
        va, fa, ra = v, f, np.ones(len(f), np.int64)
        vb, fb = grid(2, 2, 1., 2.)
        vv, ff, rr = sc.assemble([(va, fa, ra), (vb, fb, np.full(len(fb), 2))])
        self.assertEqual(len(vv), 9 + 9 - 3)        # the x = 1 column is shared
        self.assertEqual(len(ff), len(fa) + len(fb))


class Smoothing(unittest.TestCase):
    def test_constant_field_and_total_variation(self):
        v, f = grid(30, 30)
        nrm, _ = fl.face_normals(v, f)
        C, mass = sc.smoothing_operator(v, f, nrm, 25., .1)
        u, info = sc.solve_smooth(C, mass, np.full(len(v), .7), 1e-3)
        self.assertTrue(np.allclose(u, .7, atol=1e-5))
        step = np.where(v[:, 0] > .5 + 1e-9, 1., np.where(v[:, 0] < .5 - 1e-9, 0., .5))
        u, info = sc.solve_smooth(C, mass, step, 1e-3)
        self.assertEqual(info, 0)
        row = np.isclose(v[:, 1], .5)
        order = np.argsort(v[row, 0])
        ur = u[row][order]
        self.assertTrue(np.all(np.diff(ur) >= -1e-6))                      # monotone across the step
        self.assertTrue(np.allclose(ur + ur[::-1], 1., atol=1e-3))         # antisymmetric about the step: the 0.5 level stays at x = 0.5
        self.assertLessEqual(float(u.max()), 1 + 1e-6)
        self.assertGreaterEqual(float(u.min()), -1e-6)


class Pooling(unittest.TestCase):
    def setUp(self):
        v, f = grid(24, 12, -1., 1., 0., .5)
        self.v, self.f = v, f
        self.cent = v[f].mean(1)
        n = len(f)
        self.M = {'v': v, 'f': f, 'plane': sym.Plane.from_x(0.), 'part': np.zeros(n, np.int64),
                  'tab': {'expected': np.array([False])}}
        self.left = self.cent[:, 0] > 0
        sc.P['pool_mode'] = 'split'

    def pooled(self, ev, cos, expected=False):
        M = dict(self.M, cos=cos)
        M['tab'] = {'expected': np.array([expected])}
        mp = sc.mirror_partners(M)
        return mp, sc.pool_evidence(M, ev, mp, True)

    def test_unseen_side_borrows_partner(self):
        n = len(self.f)
        ev = np.zeros((n, 2))
        ev[~self.left, 1] = 1.                    # right half: garment, seen
        seen_left = self.left & (self.cent[:, 1] < .3)
        ev[seen_left, 1] = 1.                     # lower left half: seen and agrees (the evidence of a symmetric neighbourhood)
        cos = np.where(self.left & ~seen_left, 0., .9)    # upper left half: never seen, no evidence
        mp, (pooled, cos_eff, pi, pi_role, cat, info, aux) = self.pooled(ev, cos)
        probe = self.left & (self.cent[:, 1] > .31) & (self.cent[:, 1] < .37)
        self.assertGreater(float(pi[probe].mean()), .5)
        self.assertGreater(float(pooled[probe, 1].mean()), .5)       # the unseen left half now carries the garment evidence
        self.assertTrue(np.all(cos_eff[probe] >= .9 - 1e-9))

    def test_well_seen_disagreement_blocks(self):
        n = len(self.f)
        ev = np.zeros((n, 2))
        ev[~self.left, 1] = 1.
        ev[self.left, 0] = 1.                     # the left half is clearly bare, both sides well seen
        cos = np.full(n, .9)
        mp, (pooled, cos_eff, pi, pi_role, cat, info, aux) = self.pooled(ev, cos)
        self.assertLess(float(pi.mean()), .05)
        self.assertLess(float(pooled[self.left, 1].mean()), .05)
        self.assertEqual(int(np.bincount(cat, minlength=8)[4] > 0), 1)

    def test_expected_asymmetric_region_never_pools(self):
        n = len(self.f)
        ev = np.zeros((n, 2))
        ev[~self.left, 1] = 1.
        cos = np.where(self.left, 0., .9)
        mp, (pooled, cos_eff, pi, pi_role, cat, info, aux) = self.pooled(ev, cos, expected=True)
        self.assertEqual(float(pi.max()), 0.)
        self.assertTrue(np.all(cat == 6))


class BoundaryMetrics(unittest.TestCase):
    def test_sawtooth_is_rougher_than_a_line(self):
        v, f = grid(40, 8, 0., 2., 0., .4)
        straight, _ = sc.boundary_metrics(v, f)
        z = v.copy()
        edge = np.isclose(z[:, 1], .4)
        z[edge, 1] += np.where(np.arange(edge.sum()) % 2 == 0, .03, -.03)      # sawtooth top edge, teeth of 5% of the length
        saw, _ = sc.boundary_metrics(z, f)
        self.assertGreater(saw['rough_rms_pct'], straight['rough_rms_pct'])
        self.assertGreater(saw['sharp_turns_per_100pct'], straight['sharp_turns_per_100pct'])

    def test_mirror_mismatch_zero_for_a_mirrored_pair(self):
        v, f = grid(10, 10, .2, 1., 0., 1.)
        plane = sym.Plane.from_x(0.)
        vv, ff, rr = sc.assemble([(v, f, np.ones(len(f), np.int64)), sc.mirror_copy(v, f, np.ones(len(f), np.int64), plane)])
        _, dense = sc.boundary_metrics(vv, ff, min_len_pct=1.)
        mm = sc.mirror_mismatch(dense, plane)
        self.assertLess(mm['max'], 1e-6)


if __name__ == '__main__':
    unittest.main()
