"""Tests for feature_labels.py (studs) on a synthetic plate: venv python (numpy + scipy), a few seconds.

    characters/.venv/bin/python -m unittest tools/tripo_pipeline/test_feature_labels.py
"""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import feature_labels as fl  # noqa: E402

PRIMARY, TER = 1, 3
EDGE = .004


def plate(nx=150, ny=100):
    """Flat plate in the xy plane (normal +z), triangulated grid with edge ~EDGE; returns v, f."""
    xs, ys = np.arange(nx + 1) * EDGE, np.arange(ny + 1) * EDGE
    X, Y = np.meshgrid(xs, ys, indexing='ij')
    v = np.stack([X.ravel(), Y.ravel(), np.zeros(X.size)], 1)
    idx = np.arange(X.size).reshape(nx + 1, ny + 1)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel(), idx[:-1, 1:].ravel()
    f = np.vstack([np.stack([a, b, c], 1), np.stack([a, c, d], 1)])
    return v, f


def dome(v, centre, rb=.02, h=.012):
    d = np.hypot(v[:, 0] - centre[0], v[:, 1] - centre[1])
    R = (rb ** 2 + h ** 2) / (2 * h)
    inside = d < rb
    v[inside, 2] += np.sqrt(np.maximum(R ** 2 - d[inside] ** 2, 0)) - (R - h)


class Scene:
    def __init__(self):
        v, f = plate()
        self.centres = {'full0': (.10, .12), 'full1': (.25, .12), 'full2': (.40, .12), 'partial': (.10, .26),
                        'plain': (.25, .26)}
        for k, c in self.centres.items():
            dome(v, c)
        # a thin raised ridge (not a stud, not yellow) and a yellow trim band (flat, slightly raised)
        ridge = (np.abs(v[:, 0] - .50) < .003) & (v[:, 1] > .05) & (v[:, 1] < .35)
        v[ridge, 2] += .01
        band = (v[:, 1] > .33) & (v[:, 1] < .37)
        v[band, 2] += .003
        self.v, self.f = v, f
        cent = v[f].mean(1)
        self.cent = cent
        n = len(f)
        self.lab = np.full(n, PRIMARY)
        self.ev = np.zeros(n)

        def in_disc(c, r):
            return np.hypot(cent[:, 0] - c[0], cent[:, 1] - c[1]) < r
        self.disc = {k: in_disc(c, .02) for k, c in self.centres.items()}
        for k in ('full0', 'full1', 'full2'):
            self.lab[self.disc[k]] = TER
            self.ev[self.disc[k]] = 1.
        half = self.disc['partial'] & (cent[:, 0] < self.centres['partial'][0])
        self.lab[half] = TER
        self.ev[self.disc['partial']] = np.where(cent[self.disc['partial'], 0] < self.centres['partial'][0] + .005, 1., 0.)
        # spill: a flat yellow blob beside full0 (offset texture disc), outside its dome
        self.spill = in_disc((.10 + .03, .12), .012) & ~self.disc['full0']
        self.lab[self.spill] = TER
        self.ev[self.spill] = 1.
        # a yellow flat disc blob in the middle of nowhere with no dome is NOT touched by the spill rule (too far from any stud)
        self.band = (cent[:, 1] > .33) & (cent[:, 1] < .37)
        self.lab[self.band] = TER
        self.ev[self.band] = 1.
        self.ridge = (np.abs(cent[:, 0] - .50) < .008) & (cent[:, 1] > .06) & (cent[:, 1] < .31)

    def run(self, **params):
        params = dict({'radius': .02}, **params)
        return fl.studs(self.v, self.f, self.lab, self.ev, TER, params, log=lambda *a: None)


class StudsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Scene()
        cls.r = cls.s.run()

    def test_counts(self):
        info = self.r['info']
        # five domes: three yellow, one half yellow, one plain
        self.assertEqual(info['studs'], 5, [x for x in info['rejected']])
        self.assertEqual(info['studs_geometry_only'], 1)
        self.assertEqual(info['before'], {'full': 3, 'partial': 1, 'none': 1})
        self.assertEqual(info['fixed'], 2)

    def test_every_dome_fully_tertiary(self):
        new = self.r['lab_new']
        for k in ('full0', 'full1', 'full2', 'partial', 'plain'):
            m = self.s.disc[k] & (np.hypot(self.s.cent[:, 0] - self.s.centres[k][0], self.s.cent[:, 1] - self.s.centres[k][1]) < .013)
            self.assertGreater(m.sum(), 40)
            self.assertTrue((new[m] == TER).mean() > .99, k)

    def test_no_yellow_beyond_the_dome(self):
        new = self.r['lab_new']
        for k in ('full0', 'full1', 'full2', 'partial', 'plain'):
            c = self.s.centres[k]
            ring = (np.hypot(self.s.cent[:, 0] - c[0], self.s.cent[:, 1] - c[1]) > .03) & (np.hypot(self.s.cent[:, 0] - c[0], self.s.cent[:, 1] - c[1]) < .05)
            ring &= ~self.s.band
            self.assertTrue((new[ring] != TER).all(), k)

    def test_spill_removed(self):
        new = self.r['lab_new']
        self.assertGreater(self.s.spill.sum(), 50)
        self.assertTrue((new[self.s.spill] == PRIMARY).mean() > .98)
        self.assertGreater(self.r['info']['spill']['spill_area'], 0)

    def test_trim_band_kept(self):
        new = self.r['lab_new']
        self.assertTrue((new[self.s.band] == TER).all())

    def test_ridge_is_not_a_stud(self):
        new = self.r['lab_new']
        self.assertTrue((new[self.s.ridge] == PRIMARY).all())

    def test_base_faces_untouched(self):
        s = Scene()
        s.lab[s.cent[:, 0] > .45] = 0          # a base zone (the ridge region)
        r = s.run()
        self.assertTrue((r['lab_new'][s.lab == 0] == 0).all())

    def test_without_spill_option(self):
        r = self.s.run(spill=False)
        self.assertTrue((r['lab_new'][self.s.spill] == TER).all())

    def test_plain_dome_far_from_yellow_is_rejected(self):
        s = Scene()
        s.ev[:] = 0.                           # no yellow evidence anywhere: nothing is confirmed, plain domes need a confirmed neighbour
        s.lab[s.lab == TER] = PRIMARY
        r = s.run()
        self.assertEqual(r['info']['studs'], 0)

    def test_radius_estimate_from_yellow_blobs(self):
        s = Scene()
        a, nrm, cent = fl.face_geometry(s.v, s.f)
        pairs, _ = fl.adjacency(s.f)
        r, info = fl.radius_estimate(cent, nrm, a, pairs, s.ev, s.lab > 0, EDGE, dict(fl.P))
        self.assertAlmostEqual(r, .02, delta=.006, msg=str(info))


class FlatStudTest(unittest.TestCase):
    def test_flat_topped_boss_with_yellow_texture_is_a_stud(self):
        v, f = plate(100, 60)
        c = (.2, .12)
        d = np.hypot(v[:, 0] - c[0], v[:, 1] - c[1])
        v[d < .03, 2] += .008                      # a flat-topped boss, radius 0.03, 8 mm high
        cent = v[f].mean(1)
        dc = np.hypot(cent[:, 0] - c[0], cent[:, 1] - c[1])
        lab = np.full(len(f), PRIMARY)
        ev = np.where(dc < .03, 1., 0.)
        r = fl.studs(v, f, lab, ev, TER, {'radius': .03}, log=lambda *a: None)
        self.assertEqual(r['info']['studs'], 1, r['info']['rejected'])
        self.assertTrue((r['lab_new'][dc < .027] == TER).all())
        self.assertTrue((r['lab_new'][dc > .045] == PRIMARY).all())

    def test_yellow_ghost_on_a_flat_plate_is_not_a_stud(self):
        v, f = plate(100, 60)
        cent = v[f].mean(1)
        dc = np.hypot(cent[:, 0] - .2, cent[:, 1] - .12)
        lab = np.full(len(f), PRIMARY)
        ev = np.where(dc < .03, 1., 0.)
        r = fl.studs(v, f, lab, ev, TER, {'radius': .03}, log=lambda *a: None)
        self.assertEqual(r['info']['studs'], 0)
        self.assertTrue((r['lab_new'] == PRIMARY).all())


class TransferTest(unittest.TestCase):
    def test_transfer_role_array_changes_only_triangles_that_cover_changed_faces(self):
        # one big LOD triangle over a high-poly patch whose faces turn tertiary, another over an unchanged patch
        v = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [2, 0, 0], [3, 0, 0], [2, 1, 0]], float)
        f = np.array([[0, 1, 2], [3, 4, 5]])
        cent = np.array([[.3, .3, 0], [.4, .2, 0], [2.3, .3, 0], [2.4, .2, 0]])
        area = np.ones(4)
        old = np.array([PRIMARY, PRIMARY, PRIMARY, PRIMARY])
        new = np.array([TER, TER, PRIMARY, PRIMARY])
        out = fl.transfer_role_array(v, f, np.array([PRIMARY, PRIMARY]), cent, area, old, new, 5)
        self.assertEqual(out.tolist(), [TER, PRIMARY])
        base = fl.transfer_role_array(v, f, np.array([0, PRIMARY]), cent, area, old, new, 5)   # a base triangle stays base
        self.assertEqual(base.tolist(), [0, PRIMARY])


if __name__ == '__main__':
    unittest.main()
