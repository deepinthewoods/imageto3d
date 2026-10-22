"""seam_metrics.py on synthetic pieces. Needs numpy + scipy (run with the characters/.venv python):
  <venv>/bin/python -m unittest tools/tripo_pipeline/test_seam_metrics.py   (from the project directory)"""
import sys
import unittest
from pathlib import Path

import numpy as np

try:
    import scipy  # noqa: F401
except ImportError:
    raise unittest.SkipTest('scipy not available')

sys.path.insert(0, str(Path(__file__).resolve().parent))
import seam_metrics as sm  # noqa: E402


def plane(n=60, zigzag=False):
    """n x n grid on [0, .6]^2 (30% of the normalised height 2 wide). Piece B = a square block of cells in the middle (its closed seam
    with piece A is a loop: no open ends), with a notch every other row when `zigzag`. B has its own vertex copies (same positions), so
    the welding by position has to close the seam. Returns (v, f, piece, index of the first copied vertex)."""
    xs = np.linspace(0, .6, n)
    v = np.array([(x, y, 0.) for y in xs for x in xs])
    f, cells = [], []
    for j in range(n - 1):
        for i in range(n - 1):
            a, b, c, d = j * n + i, j * n + i + 1, (j + 1) * n + i + 1, (j + 1) * n + i
            f += [(a, b, c), (a, c, d)]
            cells += [(i, j), (i, j)]
    f, cells = np.array(f), np.array(cells)
    c0 = n // 2
    half = 12 + (np.where(zigzag, (cells[:, 1] % 2) * 3, 0))
    piece = ((np.abs(cells[:, 0] - c0) < half) & (np.abs(cells[:, 1] - c0) < 12)).astype(np.int64)
    b_vertices = np.unique(f[piece == 1])
    v2 = np.concatenate([v, v[b_vertices]])
    remap = np.arange(len(v))
    remap[b_vertices] = len(v) + np.arange(len(b_vertices))
    f2 = f.copy()
    f2[piece == 1] = remap[f[piece == 1]]
    return v2, f2, piece, len(v)


class SeamMetricsTest(unittest.TestCase):
    names = {0: 'A', 1: 'B'}

    def test_welded_seam_has_no_gap(self):
        v, f, piece, _ = plane()
        r = sm.level_report(v, f, piece, self.names)
        e = r['pairs']['A|B']
        self.assertGreater(e['seam_edges'], 40)
        self.assertEqual(r['gap_vertices'], 0)
        self.assertEqual(r['tjunction_vertices'], 0)
        self.assertEqual(r['thin_holes'], 0)

    def test_displaced_vertex_is_a_gap(self):
        v, f, piece, n0 = plane()
        xs = v[n0:, 0]
        seam_copy = [n0 + i for i in np.flatnonzero(np.isclose(xs, xs.min())) if abs(v[n0 + i, 1] - .3) < .05]
        self.assertTrue(seam_copy)
        v2 = v.copy()
        v2[seam_copy[len(seam_copy) // 2]] += np.array([.004, 0, 0])               # 0.2% of the height off the shared seam
        r = sm.level_report(v2, f, piece, self.names)
        self.assertGreater(r['gap_vertices'] + r['tjunction_vertices'], 0)
        self.assertGreater(r['gap_max_pct'], .05)
        self.assertLess(r['gap_max_pct'], .25)

    def test_jaggedness_separates_a_zigzag_from_a_clean_seam(self):
        v, f, piece, _ = plane()
        clean = sm.level_report(v, f, piece, self.names)['pairs']['A|B']['jaggedness']
        v, f, piece, _ = plane(zigzag=True)
        zig = sm.level_report(v, f, piece, self.names)['pairs']['A|B']['jaggedness']
        self.assertLess(clean['length_ratio'], 1.12)                              # the corners of the square loop round off a little
        self.assertGreater(zig['length_ratio'], clean['length_ratio'] + .1)
        self.assertGreater(zig['rms_dev_pct'], clean['rms_dev_pct'])

    def test_reversals_count_turns_of_more_than_90_degrees(self):
        t = np.linspace(0, .6, 80)
        straight = np.stack([t, 0 * t, 0 * t], 1)
        spiky = straight.copy()
        spiky[10::10, 0] -= .02                                                    # every 10th vertex jumps back: two reversals each
        self.assertEqual(sm.chain_jaggedness(straight, False)['reversals'], 0)
        self.assertGreaterEqual(sm.chain_jaggedness(spiky, False)['reversals'], 10)

    def test_a_hole_inside_a_piece_is_found_as_thin(self):
        v, f, piece, _ = plane()
        c = v[f].mean(1)
        hole = (np.abs(c[:, 0] - .08) < .006) & (c[:, 1] > .1) & (c[:, 1] < .2)      # a slit of base inside piece A
        r = sm.level_report(v, f[~hole], piece[~hole], self.names)
        self.assertGreaterEqual(r['thin_holes'], 1)


if __name__ == '__main__':
    unittest.main()
