"""Tests of ring_labels.py on a synthetic mesh: raised rings over a recessed undershirt on a curved (cylinder) sheet, with noisy labels.

    characters/.venv/bin/python -m unittest tools/tripo_pipeline/test_ring_labels.py
"""
import sys
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ring_labels as rl  # noqa: E402

R0, H, RING_R, TUBE_W = .30, .015, .06, .016      # sheet radius, ring height, ring radius, tube half width (normalised units)
PITCH = .16


def ring_sheet(step=.0075, extent=.72, belt=(.30, .38), flat=False, seed=0):
    """Cylinder sheet (angle span chosen by arc length `extent`) with hex-lattice rings; the band `belt` (z range) is a raised
    rope with no holes. Returns v, f, truth (per face: 1 ring tube, 2 centre, 3 belt), centres."""
    ns, nz = int(extent / step), int(extent / step)
    s = np.linspace(0, extent, ns + 1) - extent / 2
    z = np.linspace(0, extent, nz + 1) + .1
    S, Z = np.meshgrid(s, z)
    centres = []
    for j in range(-1, int(extent / (PITCH * .87)) + 2):
        for i in range(-1, int(extent / PITCH) + 2):
            centres.append((-extent / 2 + i * PITCH + (PITCH / 2 if j % 2 else 0), .1 + j * PITCH * .87))
    centres = np.array(centres)
    d = np.sqrt(((np.stack([S, Z], -1)[:, :, None, :] - centres[None, None]) ** 2).sum(-1)).min(-1)
    tube = np.clip(1 - ((d - RING_R) / TUBE_W) ** 2, 0, 1)
    rope = ((Z > belt[0]) & (Z < belt[1])).astype(float)
    height = H * np.maximum(np.where(rope > 0, 1., tube), 0)
    if flat:
        height = height * 0
    r = R0 + height
    th = S / R0
    v = np.stack([r * np.sin(th), r * np.cos(th) * -1 + R0 * 0, Z], -1).reshape(-1, 3)      # faces point away from the axis
    idx = lambda i, j: j * (ns + 1) + i
    f = []
    for j in range(nz):
        for i in range(ns):
            a, b, c, e = idx(i, j), idx(i + 1, j), idx(i + 1, j + 1), idx(i, j + 1)
            f += [(a, b, c), (a, c, e)]
    f = np.array(f, np.int64)
    hf = height.reshape(-1)[f].mean(1)
    zf = Z.reshape(-1)[f].mean(1)
    truth = np.where(hf > H * .5, 1, 2)
    truth[(zf > belt[0]) & (zf < belt[1])] = 3
    return v, f, truth, centres


def noisy_labels(truth, v, f, seed=1, flip_share=.10):
    """Texture-like labels: ring tube 1 (primary), centre 2 (secondary), belt 2; plus blob errors and jagged boundary flips."""
    rng = np.random.default_rng(seed)
    lab = np.where(truth == 1, 1, 2)
    cen = v[f].mean(1)
    for _ in range(40):                                  # blobs of the wrong colour
        c = cen[rng.integers(len(cen))]
        m = np.linalg.norm(cen - c, axis=1) < .02
        m &= truth != 3
        lab[m] = 3 - lab[m]
    pairs = rl.edge_pairs(f)
    edge = lab[pairs[:, 0]] != lab[pairs[:, 1]]
    near = np.zeros(len(f), bool)
    near[pairs[edge, 0]] = near[pairs[edge, 1]] = True
    flip = near & (rng.random(len(f)) < flip_share) & (truth != 3)
    lab[flip] = 3 - lab[flip]
    return lab


class RingPass(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v, cls.f, cls.truth, cls.centres = ring_sheet()
        cls.lab = noisy_labels(cls.truth, cls.v, cls.f)
        cls.out = rl.ring_pass(cls.v, cls.f, cls.lab, 1, 2, {'sigma': .034}, log=lambda *a: None)

    def test_noisy_input_is_noisy(self):
        reg = self.truth != 3
        acc = (self.lab[reg] == self.truth[reg]).mean()
        self.assertLess(acc, .93)

    def test_rings_and_centres_recovered(self):
        reg = self.truth != 3
        new = self.out['label']
        acc = (new[reg] == self.truth[reg]).mean()
        self.assertGreater(acc, .96, acc)
        before = (self.lab[reg] == self.truth[reg]).mean()
        self.assertGreater(acc, before + .03)

    def test_every_ring_is_whole(self):
        """Faces of one ring tube (within RING_R +- TUBE_W/2 of its centre) are all ring role (or nearly)."""
        cen = self.v[self.f].mean(1)
        s = np.arctan2(cen[:, 0], -cen[:, 1]) * R0
        pts = np.stack([s, cen[:, 2]], 1)
        reg = (self.truth == 1)
        bad = 0
        total = 0
        for c in self.centres:
            if np.abs(c[0]) > .3 or c[1] < .45 or c[1] > .75:       # interior rings only
                continue
            d = np.linalg.norm(pts - c, axis=1)
            m = reg & (np.abs(d - RING_R) < TUBE_W * .5)
            if m.sum() < 30:
                continue
            total += 1
            if (self.out['label'][m] == 1).mean() < .93:
                bad += 1
        self.assertGreater(total, 2)
        self.assertEqual(bad, 0, f'{bad}/{total} rings not whole')

    def test_every_centre_is_blue(self):
        cen = self.v[self.f].mean(1)
        s = np.arctan2(cen[:, 0], -cen[:, 1]) * R0
        pts = np.stack([s, cen[:, 2]], 1)
        bad = total = 0
        for c in self.centres:
            if np.abs(c[0]) > .3 or c[1] < .45 or c[1] > .75:
                continue
            m = (self.truth == 2) & (np.linalg.norm(pts - c, axis=1) < RING_R - TUBE_W * 1.2)
            if m.sum() < 20:
                continue
            total += 1
            bad += (self.out['label'][m] == 2).mean() < .93
        self.assertGreater(total, 2)
        self.assertEqual(bad, 0)

    def test_belt_is_protected(self):
        belt = self.truth == 3
        self.assertTrue((self.out['label'][belt] == 2).all())
        self.assertTrue((self.out['kind'][belt] == 3).mean() > .9)

    def test_holes_counted(self):
        n_interior = sum(1 for c in self.centres if abs(c[0]) < .3 and .45 < c[1] < .75)
        self.assertGreaterEqual(self.out['stats']['holes']['count'], n_interior)
        self.assertLess(self.out['stats']['holes']['count'], 4 * len(self.centres))

    def test_void_mode_removes_centres_above_z(self):
        out = rl.ring_pass(self.v, self.f, self.lab, 1, 0, {'sigma': .034, 'void_above_z': .5, 'protect_mode': 'relief'}, log=lambda *a: None)
        cen = self.v[self.f].mean(1)
        up = (cen[:, 2] > .5) & (self.truth != 3)
        gone = out['label'] == 0
        self.assertGreater(gone[up & (self.truth == 2)].mean(), .9)
        self.assertLess(gone[up & (self.truth == 1)].mean(), .05)
        self.assertEqual(int(gone[cen[:, 2] <= .5].sum()), 0)         # nothing is removed below the height

    def test_no_relief_keeps_the_texture(self):
        v, f, truth, _ = ring_sheet(flat=True, extent=.5)
        lab = np.random.default_rng(3).integers(1, 3, len(f))
        out = rl.ring_pass(v, f, lab, 1, 2, {'sigma': .034}, log=lambda *a: None)
        self.assertGreater((out['label'] == lab).mean(), .97)


if __name__ == '__main__':
    unittest.main()
