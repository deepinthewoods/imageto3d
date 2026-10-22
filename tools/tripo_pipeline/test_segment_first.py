"""segment_first.py building blocks on synthetic meshes. Needs numpy + scipy (run with the characters/.venv python):
  <venv>/bin/python -m unittest tools/tripo_pipeline/test_segment_first.py   (from the project directory)"""
import sys
import unittest
from pathlib import Path

import numpy as np

try:
    import scipy  # noqa: F401
except ImportError:                                   # the blender/system python of the other tests has no scipy
    raise unittest.SkipTest('scipy not available')

sys.path.insert(0, str(Path(__file__).resolve().parent))
import segment_first as sf  # noqa: E402


def grid(n=40, wave=True):
    """Open n x n plane (z = 0), two sections split along a gentle zigzag in x ~ .5; returns v, f, piece."""
    xs = np.linspace(0, 1, n)
    v = np.array([(x, y, 0.) for y in xs for x in xs])
    f = []
    for j in range(n - 1):
        for i in range(n - 1):
            a, b, c, d = j * n + i, j * n + i + 1, (j + 1) * n + i + 1, (j + 1) * n + i
            f += [(a, b, c), (a, c, d)]
    f = np.array(f)
    cx = v[f].mean(1)[:, 0]
    cy = v[f].mean(1)[:, 1]
    edge = .5 + (.004 * np.sin(cy * 60) if wave else 0)
    return v, f, (cx > edge).astype(np.int64)


class SegmentFirstTest(unittest.TestCase):
    def test_edge_pairs_and_components(self):
        v, f, piece = grid(8)
        pairs = sf.edge_pairs(f)
        self.assertEqual(len(pairs), 7 * 7 + 2 * 7 * 6)              # diagonals + interior grid edges of the triangulated grid
        import split_pieces as sp
        sf.patch_split_pieces()
        g = sp.Graph(f)
        comp = g.components(piece == 0)
        self.assertEqual(comp.max(), 0)
        self.assertEqual(int((comp == 0).sum()), int((piece == 0).sum()))

    def test_potts_removes_a_speck_and_keeps_the_edge(self):
        v, f, piece = grid(30, wave=False)
        pairs = sf.edge_pairs(f)
        ev = np.zeros((len(f), 3))
        ev[np.arange(len(f)), 1 + piece] = .9
        speck = 200
        ev[speck] = 0
        ev[speck, 2 - piece[speck]] = .5
        lab, _ = sf.potts(ev, np.ones(len(f)), pairs)
        self.assertEqual(int(lab[speck]), 1 + int(piece[speck]))
        self.assertEqual(int((lab != 1 + piece).sum()), 0)

    def test_absorb_small_fills_pinholes(self):
        v, f, piece = grid(30, wave=False)
        pairs = sf.edge_pairs(f)
        _, area, _ = __import__('split_pieces').triangle_geometry(v, f)
        lab = piece.copy()
        island = [2 * (10 * 29 + 3), 2 * (10 * 29 + 3) + 1, 2 * (10 * 29 + 4)]
        lab[island] = 5                                             # a 3-face island of another label deep inside a section
        out, info = sf.absorb_small(lab, area, pairs, min_share=.01)
        self.assertTrue((out == piece).all())

    def test_bare_zone_prior_pulls_to_base(self):
        v, f, piece = grid(20, wave=False)
        pairs = sf.edge_pairs(f)
        ev = np.zeros((len(f), 2))
        ev[:, 1] = .6                                               # weak garment evidence everywhere
        bare = np.zeros(len(f), bool)
        bare[:len(f) // 2] = True
        lab, _ = sf.potts(ev, np.ones(len(f)), pairs, bare=bare)
        self.assertTrue((lab[:len(f) // 4] == 0).all())

    def test_douglas_peucker(self):
        t = np.linspace(0, 1, 200)
        P = np.stack([t, .0005 * np.sin(t * 90), np.zeros(200)], 1)
        self.assertEqual(len(sf.douglas_peucker(P, .01)), 2)
        self.assertGreater(len(sf.douglas_peucker(P, 1e-5)), 20)

    def test_sections_reduced_alone_share_their_border(self):
        v, f, piece = grid(60)
        out, info = sf.reduce_sections(v, f, piece, .01)
        self.assertEqual(sorted(out), [0, 1])
        self.assertLess(sum(len(x) for x in out.values()), len(f) / 4)
        borders = []
        for lab in (0, 1):
            fp = out[lab]
            e = np.sort(np.concatenate([fp[:, [0, 1]], fp[:, [1, 2]], fp[:, [2, 0]]]), axis=1)
            u, c = np.unique(e, axis=0, return_counts=True)
            bv = set(np.unique(u[c == 1]).tolist())
            # the vertices on the cut x ~ .5 of this section (its boundary vertices that lie between the two sections)
            borders.append({i for i in bv if abs(v[i, 0] - .5) < .02 and 0 < v[i, 1] < 1})
        self.assertGreater(len(borders[0]), 1)
        self.assertEqual(borders[0], borders[1])                    # identical border vertices -> they weld
        for lab in (0, 1):
            fp = out[lab]
            a = np.cross(v[fp[:, 1]] - v[fp[:, 0]], v[fp[:, 2]] - v[fp[:, 0]])
            self.assertTrue((np.linalg.norm(a, axis=1) > 0).all())

    def test_cover_votes_and_top_share(self):
        tris = np.array([[[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[1, 1, 0], [2, 1, 0], [1, 2, 0]]], float)
        cent = np.array([[.3, .3, 0], [.2, .2, 0], [1.3, 1.3, 0]])
        votes, owner = sf.cover_votes(tris, cent, np.ones(3), np.array([0, 1, 1]), 2, per_area=.1)
        self.assertEqual(owner.tolist(), [0, 0, 1])
        self.assertAlmostEqual(votes[0].tolist()[0], 1.0)
        stats = sf.top_share_stats(votes, np.array([.5, .5]))
        self.assertAlmostEqual(stats['mixed_area_share'], .5)

    def test_fill_slivers_closes_thin_base_strips_only(self):
        v, f, piece = grid(60, wave=False)
        pairs, edges = sf.edge_pairs(f, True)
        _, area, _ = __import__('split_pieces').triangle_geometry(v, f)
        cx = v[f].mean(1)[:, 0]
        lab = np.where(cx < .5, 1, 2)
        lab[(cx > .49) & (cx < .52)] = 0                           # ~2 cells wide strip of base between two garment labels
        lab[cx > .85] = 0                                          # a wide base area touching the mesh edge: not a sliver
        el = np.linalg.norm(v[edges[:, 0]] - v[edges[:, 1]], axis=1)
        out, info = sf.fill_slivers(lab, area, pairs, el, max_width_pct=6., max_share=.5, height=1.)
        self.assertEqual(info['filled_components'], 1)             # the strip; the wide base area at the right edge is not a sliver
        strip = (cx > .49) & (cx < .52)
        self.assertTrue((out[strip] > 0).all())
        self.assertTrue(set(np.unique(out[strip])) == {1, 2})      # filled from both sides
        self.assertTrue((out[cx > .85] == 0).all())
        # the strip's left half is label 1, its right half label 2 (ring by ring from both sides)
        self.assertEqual(int(out[strip & (cx < .505)].min()), 1)
        self.assertEqual(int(out[strip & (cx > .505)].max()), 2)

    def test_fill_slivers_keeps_bare_zone_and_wide_areas(self):
        v, f, piece = grid(60, wave=False)
        pairs, edges = sf.edge_pairs(f, True)
        _, area, _ = __import__('split_pieces').triangle_geometry(v, f)
        cx = v[f].mean(1)[:, 0]
        lab = np.where(cx < .5, 1, 2)
        lab[(cx > .49) & (cx < .52)] = 0
        el = np.linalg.norm(v[edges[:, 0]] - v[edges[:, 1]], axis=1)
        bare = (cx > .49) & (cx < .52)
        out, info = sf.fill_slivers(lab, area, pairs, el, bare=bare, max_width_pct=6., max_share=.5, height=1.)
        self.assertTrue((out == lab).all())                        # the strip lies in a bare zone (forearm, lower leg, head): stays base
        out, info = sf.fill_slivers(lab, area, pairs, el, max_width_pct=1., max_share=.5, height=1.)
        self.assertTrue((out == lab).all())                        # 3% wide strip is wider than the 1% limit

    def test_mincut_sections_removes_teeth_and_keeps_a_straight_cut(self):
        v, f, piece = grid(80, wave=False)
        pairs, edges = sf.edge_pairs(f, True)
        cx, cy = v[f].mean(1)[:, 0], v[f].mean(1)[:, 1]
        tooth = (np.floor(cy * 79) % 6 < 3).astype(float) * .022               # square teeth, 3 cells high, 1.7 cells deep
        ragged = (cx > .5 + tooth).astype(np.int64)

        def spread(lab):
            cut = lab[pairs[:, 0]] != lab[pairs[:, 1]]
            return float(((v[edges[cut, 0]] + v[edges[cut, 1]]) / 2)[:, 0].std())

        out, info = sf.mincut_sections(ragged, f, v, pairs, edges, band_pct=6., height=1.)
        self.assertLess(spread(out), .2 * spread(ragged))
        self.assertGreater(info['faces_changed'], 0)
        self.assertLess(info['seam_length_after_pct'], info['seam_length_before_pct'])
        straight = (cx > .5).astype(np.int64)
        out2, info2 = sf.mincut_sections(straight, f, v, pairs, edges, band_pct=6., height=1.)
        self.assertLess(spread(out2), 1e-6)
        self.assertLessEqual(info2['faces_changed'], 2 * 79)                    # at most the half-cell shift of a mesh-edge staircase

    def test_mincut_sections_follows_a_crease_inside_the_band(self):
        n = 80
        v, f, piece = grid(n, wave=False)
        v = v.copy()
        v[:, 2] = np.maximum(v[:, 0] - .56, 0) * 1.5                          # a fold line at x = .56
        pairs, edges = sf.edge_pairs(f, True)
        cx = v[f].mean(1)[:, 0]
        near = (cx > .53).astype(np.int64)                                      # the cut lies 3 cells before the fold
        far = (cx > .40).astype(np.int64)                                       # ... or 16 cells before it (outside the band)
        kw = dict(band_pct=6., height=1.)
        flat, _ = sf.mincut_sections(near, f, v, pairs, edges, crease_low=1., **kw)
        crease, _ = sf.mincut_sections(near, f, v, pairs, edges, crease_low=.15, **kw)
        self.assertLess(cx[flat == 1].min(), .55)
        self.assertGreater(cx[crease == 1].min(), .555)                         # snapped onto the fold
        far_out, info = sf.mincut_sections(far, f, v, pairs, edges, crease_low=.15, **kw)
        self.assertLess(cx[far_out == 1].min(), .42)                            # nothing to snap to inside the band
        self.assertEqual(sf.mincut_sections((cx > .5).astype(np.int64) * 0, f, v, pairs, edges, **kw)[1]['faces_changed'], 0)

    def test_smooth_border_vertices_keeps_ends_and_the_weld(self):
        v, f, piece = grid(60)
        rng = np.random.default_rng(1)
        v = v.copy()
        out, info, v2 = sf.reduce_sections(v, f, piece, .01, {'iterations': 6, 'lam': .5, 'max_move': .35}, True)
        self.assertEqual(sorted(out), [0, 1])
        moved = np.flatnonzero(np.linalg.norm(v2 - v, axis=1) > 0)
        self.assertGreater(len(moved), 0)
        self.assertLessEqual(np.linalg.norm(v2 - v, axis=1).max(), .35 * 2 * .01 + 1e-9)
        ends = np.flatnonzero((np.abs(v[:, 0] - .5) < .02) & ((v[:, 1] == 0) | (v[:, 1] == 1)))
        for e in ends:
            self.assertTrue(np.allclose(v2[e], v[e]))                    # the junction vertices at the open ends stay
        for lab in (0, 1):                                              # no triangle flipped
            fp = out[lab]
            a = np.cross(v[fp[:, 1]] - v[fp[:, 0]], v[fp[:, 2]] - v[fp[:, 0]])
            b = np.cross(v2[fp[:, 1]] - v2[fp[:, 0]], v2[fp[:, 2]] - v2[fp[:, 0]])
            self.assertTrue(((a * b).sum(1) > 0).all())


if __name__ == '__main__':
    unittest.main()
