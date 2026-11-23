"""Plain python3 + numpy tests of outfit_studio.core (transform, channels, morph transfer, hashing, pipeline source).

    python3 tools/outfit_studio/tests/test_core.py
Real-data checks use models/ and base_views/ read-only and are skipped when a file is missing."""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2]))
from outfit_studio import paths  # noqa: E402
from outfit_studio.core import channels, hashing, morph_transfer as mt, pipeline_source as ps, transform as tf  # noqa: E402

MANIFEST = {'rotation_to_review': [[0, -1, 0], [1, 0, 0], [0, 0, 1]], 'normalization_scale': 2.3487,
            'normalization_origin_rotated': [.01, -.02, .03]}
FIT = {'scale': .9946, 'translation': [.0002, -.0119, .0118]}


def grid(n=4):
    """A flat n x n grid in the z = 0 plane (triangles)."""
    xs = np.linspace(0, 1, n + 1)
    V = np.array([(x, y, 0.) for y in xs for x in xs])
    F = []
    for j in range(n):
        for i in range(n):
            a = j * (n + 1) + i
            F += [(a, a + 1, a + n + 2), (a, a + n + 2, a + n + 1)]
    return V, np.array(F)


class TransformTest(unittest.TestCase):
    def test_matches_export_native_source(self):
        src = (paths.ROOT / 'tools' / 'tripo_pipeline' / 'reduce_model.py').read_text()
        self.assertIn("original = (co / info['normalization_scale'] + info['normalization_origin_rotated']) @ rotation", src)
        rng = np.random.default_rng(1)
        co = rng.normal(size=(50, 3))
        R = np.array(MANIFEST['rotation_to_review'], float)
        original = (co / MANIFEST['normalization_scale'] + MANIFEST['normalization_origin_rotated']) @ R
        np.testing.assert_allclose(tf.review_from_native(original, MANIFEST), co, atol=1e-12)
        np.testing.assert_allclose(tf.native_from_review(co, MANIFEST), original, atol=1e-12)

    def test_round_trip_chain(self):
        rng = np.random.default_rng(2)
        P = rng.normal(size=(40, 3))
        o, H = np.array([.01, -.02, 0.]), 1.62
        W = tf.native_to_world(P, MANIFEST, FIT, H, o)
        np.testing.assert_allclose(tf.world_to_native(W, MANIFEST, FIT, H, o), P, atol=1e-10)

    def test_rest_frame(self):
        V = np.array([[-1, -.3, 0], [1, .3, 1.6], [0, 0, .5]])
        o, H = tf.rest_frame(V)
        np.testing.assert_allclose(o, [0, 0, 0])
        self.assertAlmostEqual(H, 1.6)

    def test_mirror(self):
        V, F = grid(3)
        V[:, 0] += .2
        M, MF = tf.mirror_mesh(V, F, 0.)
        np.testing.assert_allclose(M[:, 0], -V[:, 0])
        n = lambda V, F: np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
        self.assertTrue((n(V, F)[:, 2] * n(M, MF)[:, 2] > 0).all())      # winding flipped: the normal keeps its side of the plane... of z
        M2, F2 = tf.mirror_mesh(M, MF, 0.)
        np.testing.assert_allclose(M2, V)
        np.testing.assert_array_equal(F2, F)

    def test_mirror_plane(self):
        sym = np.array([[-1, 0, 0], [1, 0, 0], [0, 1, 1], [0, -1, 2]], float)
        self.assertEqual(tf.mirror_plane(sym), 0.)
        shifted = sym + [.05, 0, 0]
        self.assertAlmostEqual(tf.mirror_plane(shifted), .05)


class MorphTest(unittest.TestCase):
    def test_closest_points_vs_dense_sampling(self):
        rng = np.random.default_rng(3)
        V = rng.normal(size=(12, 3))
        F = np.array([(0, 1, 2), (2, 3, 4), (4, 5, 6), (6, 7, 8), (8, 9, 10), (1, 5, 11), (3, 7, 0)])
        P = rng.normal(size=(60, 3)) * 1.5
        tri, bary, dist = mt.closest_points(P, V, F)
        self.assertTrue(np.allclose(bary.sum(1), 1) and (bary > -1e-9).all())
        # reconstructed point lies on its triangle at the reported distance
        Q = np.einsum('nj,njk->nk', bary, V[F[tri]])
        np.testing.assert_allclose(np.linalg.norm(Q - P, axis=1), dist, atol=1e-9)
        u = rng.dirichlet([1, 1, 1], 4000)
        for k in range(0, 60, 7):
            pts = np.einsum('sj,tjk->stk', u, V[F]).reshape(-1, 3)
            self.assertLessEqual(dist[k], np.linalg.norm(pts - P[k], axis=1).min() + 1e-9)

    def test_linear_delta_is_reproduced(self):
        V, F = grid(5)
        A = np.array([[.1, .2, 0], [0, -.3, .1], [.05, 0, .2]])
        delta = V @ A + [.01, 0, .02]
        P = np.random.default_rng(4).uniform(.05, .95, (80, 3)) * [1, 1, 0] + [0, 0, .07]     # above the plane
        tri, bary, dist = mt.closest_points(P, V, F)
        got = mt.barycentric_delta(tri, bary, F, delta)
        foot = P * [1, 1, 0]
        np.testing.assert_allclose(got, foot @ A + [.01, 0, .02], atol=1e-9)
        np.testing.assert_allclose(dist, .07, atol=1e-9)

    def test_small_keys_are_dropped(self):
        V, F = grid(2)
        tri, bary, _ = mt.closest_points(V + [0, 0, .1], V, F)
        out = mt.transfer_deltas(tri, bary, F, {'big': np.full((len(V), 3), .01), 'tiny': np.full((len(V), 3), 1e-7)})
        self.assertEqual(list(out), ['big'])


class HashTest(unittest.TestCase):
    def setUp(self):
        V, self.F = grid(3)
        self.V = np.round(V, 3)
        self.ch = np.arange(len(self.F)) % 2
        self.w = {'a': np.linspace(0, 1, len(V)), 'b': 1 - np.linspace(0, 1, len(V))}

    def test_stable(self):
        h = hashing.piece_hash(self.V, self.F, self.ch, self.w)
        self.assertEqual(h, hashing.piece_hash(self.V + 1e-7, self.F, self.ch, dict(reversed(list(self.w.items())))))
        self.assertEqual(h, hashing.piece_hash(self.V, self.F, self.ch, {**self.w, 'zero': np.zeros(len(self.V))}))

    def test_sensitive(self):
        h = hashing.piece_hash(self.V, self.F, self.ch, self.w)
        moved = self.V.copy()
        moved[3, 2] += 1e-4
        self.assertNotEqual(h, hashing.piece_hash(moved, self.F, self.ch, self.w))
        self.assertNotEqual(h, hashing.piece_hash(self.V, self.F[:, [0, 2, 1]], self.ch, self.w))
        self.assertNotEqual(h, hashing.piece_hash(self.V, self.F, 1 - self.ch, self.w))
        w2 = {**self.w, 'a': self.w['a'] + .01}
        self.assertNotEqual(h, hashing.piece_hash(self.V, self.F, self.ch, w2))

    def test_fingerprints(self):
        e = [('b', 'a', (0, 0, 0), (0, 1, 0), .1), ('a', '', (0, 0, 0), (0, 0, 1), 0.)]
        self.assertEqual(hashing.rig_fingerprint(e), hashing.rig_fingerprint(e[::-1]))
        self.assertNotEqual(hashing.rig_fingerprint(e), hashing.rig_fingerprint([e[0], ('a', '', (0, 0, 0), (0, 0, 1.001), 0.)]))
        self.assertEqual(hashing.base_fingerprint(self.V), hashing.base_fingerprint(self.V + 1e-7))


class ChannelTest(unittest.TestCase):
    def test_armour(self):
        piece = np.array(['chest'] * 6 + ['arm_l'] * 3 + ['arm_r'] * 3)
        role = np.array(['primary'] * 3 + ['secondary'] * 2 + ['tertiary'] + ['secondary'] * 3 + ['tertiary', 'secondary', 'secondary'])
        area = np.ones(12)
        ch, idx = channels.build_channels('body_armor', piece, role, area, ['chest', 'arm_l', 'arm_r'])
        self.assertEqual([c['id'] for c in ch], ['decoration', 'chest', 'chest_secondary', 'arm_l', 'arm_r'])
        self.assertEqual([c['role'] for c in ch], ['tertiary', 'primary', 'secondary', 'secondary', 'secondary'])
        self.assertEqual(idx.tolist(), [1, 1, 1, 2, 2, 0, 3, 3, 3, 0, 4, 4])

    def test_minor_role_folds_in(self):
        piece = np.array(['chest'] * 10)
        role = np.array(['primary'] * 9 + ['secondary'])
        ch, idx = channels.build_channels('boots', piece, role, np.ones(10))
        self.assertEqual([c['id'] for c in ch], ['chest'])
        self.assertTrue((idx == 0).all())

    def test_outfit_and_merge(self):
        role = np.array(['secondary', 'primary', 'primary', 'accent'])
        ch, idx = channels.build_channels('outfit', np.array(['outfit'] * 4), role, np.ones(4))
        self.assertEqual([c['id'] for c in ch], ['primary', 'secondary', 'accent'])
        self.assertEqual(idx.tolist(), [1, 0, 0, 2])
        old = [{'id': 'secondary', 'label': 'Cloak', 'role': 'accent', 'material': 'leather'}]
        merged = channels.merge_channels(ch, old)
        self.assertEqual((merged[1]['role'], merged[1]['material'], merged[1]['label']), ('accent', 'leather', 'Cloak'))
        self.assertEqual(merged[0]['role'], 'primary')


class SourceTest(unittest.TestCase):
    def test_weld_pairs_components(self):
        V, F = grid(2)
        soup_v = V[F].reshape(-1, 3)
        soup_f = np.arange(len(soup_v)).reshape(-1, 3)
        W, G = ps.weld(soup_v, soup_f)
        self.assertEqual(len(W), len(V))
        self.assertEqual(len(G), len(F))
        pairs = ps.face_pairs(G)
        self.assertEqual(len(pairs), len(ps.face_pairs(F)))
        self.assertTrue((ps.components(len(G), pairs) == 0).all())
        lab = ps.components(len(G), pairs[:0])
        self.assertEqual(len(set(lab)), len(G))

    def test_limbs_and_mirror(self):
        V, F = grid(3)                                   # 18 faces, one connected sheet
        part = np.array(['forearm_stretch.r'] * 6 + ['arm_stretch.r'] * 4 + ['hand.r'] * 4 + ['arm_stretch.l'] * 4)
        pairs = ps.face_pairs(F)
        gaunt, boot = ps.limb_pieces(part, pairs)
        self.assertTrue((boot == '').all())
        self.assertEqual(set(gaunt[:6]), {'cuff_r'})
        self.assertEqual(set(gaunt[10:14]), {'hand_r'})
        piece = ps.cut_piece(V, F, gaunt == 'cuff_r', role=np.array(['primary'] * len(F)), part=part, src=np.zeros(len(F), int))
        left = ps.mirror_piece(piece, 0.)
        np.testing.assert_allclose(left['V'][:, 0], -piece['V'][:, 0])
        self.assertTrue(set(left['part']) <= {'forearm_stretch.l', 'arm_stretch.l'})
        self.assertEqual(ps.swap_side('cuff_r'), 'cuff_l')
        self.assertEqual(ps.swap_side('chest'), 'chest')

    def test_zone_pieces(self):
        V, F = grid(6)                                   # 72 faces, 12 per row of cells
        cell = np.arange(len(F)) // 2
        row, col = cell // 6, cell % 6
        part = np.where(row < 2, 'spine_01.x', np.where(row < 4, 'root.x', np.where(col < 3, 'thigh_stretch.l', 'thigh_stretch.r')))
        role = np.array(['primary'] * len(F), dtype='<U12')
        role[(row == 0) & (col < 2)] = 'accent'          # no shoulder part: stays chest
        pairs = ps.face_pairs(F)
        piece = ps.zone_pieces(part, role, ps.tri_areas(V, F), pairs)
        self.assertEqual(set(piece[row < 2]), {'chest'})
        self.assertEqual(set(piece[(row >= 2) & (row < 4)]), {'pelvis'})
        self.assertEqual(set(piece[(row >= 4) & (col < 3)]), {'thigh_l'})
        self.assertEqual(set(piece[(row >= 4) & (col >= 3)]), {'thigh_r'})
        part2 = part.copy()
        part2[(row == 0) & (col < 2)] = 'arm_stretch.l'
        piece2 = ps.zone_pieces(part2, role, ps.tri_areas(V, F), pairs)
        self.assertEqual(set(piece2[(row == 0) & (col < 2)]), {'pauldron_l'})

    def test_uncertain_takes_neighbour_role(self):
        V, F = grid(2)
        role = np.array(['primary'] * len(F), dtype='<U12')
        role[3] = 'uncertain'
        out = ps.resolve_uncertain(role, ps.face_pairs(F))
        self.assertEqual(out[3], 'primary')


class FittedSourceTest(unittest.TestCase):
    """Body-fitted wearables / pieces (tools/tripo_pipeline/body_fit.py) are preferred when fresh."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.reduced = self.dir / 'reduced'
        self.reduced.mkdir()
        self.entry = {'dir': str(self.dir), 'reduced': str(self.reduced)}
        self.plain = self.reduced / 'lod_error_0p4__wearable.glb'
        self.fitted = self.reduced / 'lod_error_0p4__wearable_fitted.glb'
        self.plain.write_bytes(b'glb')

    def touch(self, path, mtime):
        path.write_bytes(b'glb')
        os.utime(path, (mtime, mtime))

    def test_wearable_prefers_fresh_fitted(self):
        self.assertEqual(ps.wearable_path(self.entry, '0p4'), self.plain)          # nothing fitted yet
        self.touch(self.plain, 1000)
        self.touch(self.fitted, 1000)                                              # equal mtime counts as fresh
        self.assertEqual(ps.wearable_path(self.entry, '0p4'), self.fitted)
        self.assertTrue(ps.is_fitted(ps.wearable_path(self.entry, '0p4')))
        self.assertEqual(ps.wearable_path(self.entry, '0p2'), self.reduced / 'lod_error_0p2__wearable.glb')

    def test_wearable_stale_fitted_and_opt_out(self):
        self.touch(self.fitted, 1000)
        self.touch(self.plain, 2000)                                               # unfitted rebuilt after the fit
        self.assertEqual(ps.wearable_path(self.entry, '0p4'), self.plain)
        self.assertFalse(ps.is_fitted(ps.wearable_path(self.entry, '0p4')))
        self.touch(self.fitted, 3000)
        self.assertEqual(ps.wearable_path(self.entry, '0p4'), self.fitted)
        self.assertEqual(ps.wearable_path(self.entry, '0p4', fitted=False), self.plain)
        ps.USE_FITTED = False
        try:
            self.assertEqual(ps.wearable_path(self.entry, '0p4'), self.plain)
        finally:
            ps.USE_FITTED = True

    def pieces(self, sub='segment_first/pieces'):
        d = self.dir / sub
        (d / 'lod_0p4').mkdir(parents=True)
        (d / 'pieces.json').write_text('{}')
        for n in ('chest', 'pelvis'):
            self.touch(d / 'lod_0p4' / f'{n}.glb', 1000)
        return d

    def test_pieces_prefer_complete_fresh_fitted_folder(self):
        d = self.pieces()
        plain = ps.pieces_output(self.entry, '0p4')
        self.assertEqual({n: p.parent.name for n, p in plain.items()}, {'chest': 'lod_0p4', 'pelvis': 'lod_0p4'})
        (d / 'lod_0p4_fitted').mkdir()
        self.touch(d / 'lod_0p4_fitted' / 'chest.glb', 1500)
        self.assertEqual(ps.pieces_output(self.entry, '0p4'), plain)               # pelvis twin missing: all or nothing
        self.touch(d / 'lod_0p4_fitted' / 'pelvis.glb', 1500)
        fitted = ps.pieces_output(self.entry, '0p4')
        self.assertEqual({n: p.parent.name for n, p in fitted.items()}, {'chest': 'lod_0p4_fitted', 'pelvis': 'lod_0p4_fitted'})
        self.assertTrue(all(ps.is_fitted(p) for p in fitted.values()))
        self.assertTrue(ps.is_fitted(d / 'lod_0p4_fitted'))
        self.assertFalse(ps.is_fitted(d / 'lod_0p4'))
        self.assertEqual(ps.pieces_output(self.entry, '0p4', fitted=False), plain)

    def test_pieces_stale_fitted_piece_falls_back(self):
        d = self.pieces('pieces')
        (d / 'lod_0p4_fitted').mkdir()
        self.touch(d / 'lod_0p4_fitted' / 'chest.glb', 1500)
        self.touch(d / 'lod_0p4_fitted' / 'pelvis.glb', 500)                       # older than pelvis.glb: stale
        out = ps.pieces_output(self.entry, '0p4')
        self.assertEqual({p.parent.name for p in out.values()}, {'lod_0p4'})


class FitLabSourceTest(unittest.TestCase):
    """Input mesh choice of the new game standard (fit_choice.json -> lab default -> aligned_fitted -> aligned -> legacy)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / 'set' / 'job1' / 'elf_male' / 'outfit'
        self.reduced = self.dir / 'reduced'
        self.lab = self.dir / 'fitlab' / 'lod_0p4'
        self.lab.mkdir(parents=True)
        self.reduced.mkdir()
        self.entry = {'set_id': 'set', 'source_set': 'set', 'job_id': 'job1', 'race': 'elf_male', 'module': 'outfit',
                      'dir': str(self.dir), 'reduced': str(self.reduced), 'levels': ['0p4', '0p2']}
        self.aligned = self.reduced / 'lod_error_0p4__wearable_aligned.glb'
        self.touch(self.reduced / 'lod_error_0p4__wearable.glb', 1000)

    def touch(self, path, mtime, data=b'glb'):
        path.write_bytes(data)
        os.utime(path, (mtime, mtime))

    def index(self, combo='sections_fill__off__none', **extra):
        import json
        (self.lab / 'index.json').write_text(json.dumps({'combos': {combo: {'inputs': {'aligned': ps.stamp(self.aligned)}}}, **extra}))

    def test_fallback_chain(self):
        self.assertEqual(ps.source_choice(self.entry, '0p4')['kind'], 'legacy')
        self.touch(self.aligned, 1000)
        self.assertEqual(ps.source_choice(self.entry, '0p4')['kind'], 'aligned')
        self.touch(self.reduced / 'lod_error_0p4__wearable_aligned_fitted.glb', 1500)
        self.assertEqual(ps.source_choice(self.entry, '0p4')['kind'], 'aligned_fitted')
        combo = self.lab / 'sections_fill__off__none.glb'
        self.touch(combo, 1600)
        self.assertEqual(ps.source_choice(self.entry, '0p4')['kind'], 'aligned_fitted')       # no index entry: not trusted
        self.index()
        got = ps.source_choice(self.entry, '0p4')
        self.assertEqual((got['kind'], got['path'], got['choice']['strategy']), ('fitlab_default', combo, 'sections_fill'))
        self.touch(self.aligned, 2000)                                                      # re-aligned after the lab ran: stale
        self.assertEqual(ps.source_choice(self.entry, '0p4')['kind'], 'aligned')

    def test_other_lods_never_use_the_lab(self):
        self.touch(self.reduced / 'lod_error_0p2__wearable_aligned.glb', 1000)
        self.touch(self.reduced / 'lod_error_0p2__wearable_aligned_fitted.glb', 1000)
        (self.dir / 'fit_choice.json').write_text('{"strategy": "sections_fill", "amount": 100}')
        got = ps.source_choice(self.entry, '0p2')
        self.assertEqual(got['kind'], 'aligned_fitted')
        self.assertIn('fit lab only covers', got['note'])

    def test_fit_choice_needs_bake_until_fresh(self):
        self.touch(self.aligned, 1000)
        self.touch(self.lab / 'current__off__none.glb', 1000)
        (self.dir / 'fit_choice.json').write_text('{"strategy": "current", "amount": 100, "sym": "off", "scope": "none", "lod": "0p4"}')
        os.utime(self.dir / 'fit_choice.json', (1100, 1100))
        got = ps.source_choice(self.entry, '0p4')
        self.assertEqual((got['kind'], got['needs_bake']), ('fitlab_choice', True))
        self.assertEqual(got['path'], self.reduced / ps.BAKED_NAME)
        self.assertEqual(got['choice'], {'strategy': 'current', 'amount': 100, 'sym': 'off', 'scope': 'none'})
        self.touch(self.reduced / ps.BAKED_NAME, 1200)
        self.assertFalse(ps.source_choice(self.entry, '0p4')['needs_bake'])
        os.utime(self.dir / 'fit_choice.json', (1300, 1300))                                 # choice changed after the bake
        self.assertTrue(ps.source_choice(self.entry, '0p4')['needs_bake'])
        with self.assertRaises(FileNotFoundError):
            (self.reduced / ps.BAKED_NAME).unlink()
            ps.resolve_source(self.entry, '0p4', bake=False)

    def test_lod_choice_suspect_and_approval(self):
        self.assertEqual(ps.resolve_lod(self.entry, 'auto'), '0p4')
        (self.dir / 'chosen_lod.json').write_text('{"lod": "0p2"}')
        self.assertEqual(ps.resolve_lod(self.entry, 'auto'), '0p2')
        self.assertEqual(ps.resolve_lod(self.entry, '0p4'), '0p4')
        with self.assertRaises(ValueError):
            ps.resolve_lod(self.entry, '0p1')
        self.assertEqual(ps.suspect_reasons(self.entry), [])
        self.index(suspect=True, suspect_reasons=['area ratio'])
        self.assertEqual(ps.suspect_reasons(self.entry), ['area ratio'])
        models = Path(self.tmp.name)
        self.assertFalse(ps.model_approved(self.entry, models))
        (models / 'set' / 'model_approval.json').write_text('{"approved": true}')
        self.assertTrue(ps.model_approved(self.entry, models))


class SectionsTest(unittest.TestCase):
    SECS = {'arm_stretch.l#0': {'state': 'hide', 'base_node': 'sec_arm_stretch_l_0', 'connector_node': None},
            'arm_stretch.l#1': {'state': 'connector', 'base_node': 'sec_arm_stretch_l_1', 'connector_node': 'conn_arm_stretch_l_1'},
            'thigh_stretch.r#0': {'state': 'connector', 'base_node': 'sec_thigh_stretch_r_0', 'connector_node': 'conn_thigh_stretch_r_0'},
            'foot.l#0': {'state': 'hide', 'base_node': 'sec_foot_l_0', 'connector_node': None},
            'neck.x#0': {'state': 'show', 'base_node': 'sec_neck_x_0', 'connector_node': None}}

    def record(self):
        secs = {}
        for key, s in self.SECS.items():
            part, side, index = ps.parse_section_key(key)
            secs[key] = {**s, 'part': part, 'side': side, 'index': index}
        return {'sections': secs}

    def test_keys_and_node_names(self):
        self.assertEqual(ps.parse_section_key('thigh_stretch.l#1'), ('thigh_stretch', 'l', 1))
        self.assertEqual(ps.parse_section_key('neck.x#0'), ('neck', 'x', 0))
        self.assertEqual(ps.parse_section_key('spine_01.x#2'), ('spine_01', 'x', 2))
        self.assertEqual(ps.swap_node_side('conn_thigh_stretch_l_1'), 'conn_thigh_stretch_r_1')
        self.assertEqual(ps.swap_node_side('conn_neck_x_0'), 'conn_neck_x_0')
        with self.assertRaises(ValueError):
            ps.parse_section_key('nonsense')

    def test_whole_item_keeps_every_state(self):
        states, conns = ps.item_sections(self.record(), 'body_armor')
        self.assertEqual(states['sec_arm_stretch_l_0'], 'hide')
        self.assertEqual(sorted(conns), ['conn_arm_stretch_l_1', 'conn_thigh_stretch_r_0'])
        self.assertEqual(conns['conn_arm_stretch_l_1'], {'section': 'sec_arm_stretch_l_1', 'mirror_of': None})

    def test_gauntlets_and_boots_only_touch_their_limb_parts(self):
        gs, gc = ps.item_sections(self.record(), 'gauntlets')
        bs, bc = ps.item_sections(self.record(), 'boots')
        self.assertEqual((gs['sec_arm_stretch_l_0'], gs['sec_foot_l_0'], sorted(gc)), ('hide', 'show', ['conn_arm_stretch_l_1']))
        self.assertEqual((bs['sec_arm_stretch_l_0'], bs['sec_foot_l_0'], sorted(bc)), ('show', 'hide', ['conn_thigh_stretch_r_0']))

    def test_symmetry_map_states_and_mirrored_connectors(self):
        symmap = {'sections': {'l2r': {'limbs': {
            'states': {'sec_arm_stretch_l_0': 'hide', 'sec_arm_stretch_l_1': 'connector', 'sec_thigh_stretch_r_0': 'show',
                       'sec_foot_l_0': 'hide', 'sec_neck_x_0': 'show', 'sec_arm_stretch_r_1': 'connector'},
            'connectors': ['conn_arm_stretch_l_1'], 'mirrored': ['conn_arm_stretch_l_1'], 'hide': []}}}}
        rec = self.record()
        rec['sections']['arm_stretch.r#1'] = {'state': 'show', 'base_node': 'sec_arm_stretch_r_1', 'connector_node': None,
                                              'part': 'arm_stretch', 'side': 'r', 'index': 1}
        states, conns = ps.item_sections(rec, 'body_armor', symmap, 'l2r', 'limbs')
        self.assertEqual(states['sec_thigh_stretch_r_0'], 'show')
        self.assertEqual(conns['conn_arm_stretch_r_1'], {'section': 'sec_arm_stretch_r_1', 'mirror_of': 'conn_arm_stretch_l_1'})
        self.assertNotIn('conn_thigh_stretch_r_0', conns)

    def test_read_glb_nodes(self):
        import json
        import struct
        v = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], '<f4')           # glTF space: (x, y up, z)
        blob = v.tobytes() + np.array([0, 1, 2], '<u2').tobytes() + b'\0\0'
        js = {'asset': {'version': '2.0'}, 'scenes': [{'nodes': [0]}], 'nodes': [{'name': 'conn_a', 'mesh': 0}],
              'meshes': [{'primitives': [{'attributes': {'POSITION': 0}, 'indices': 1}]}],
              'accessors': [{'bufferView': 0, 'componentType': 5126, 'count': 3, 'type': 'VEC3'},
                            {'bufferView': 1, 'componentType': 5123, 'count': 3, 'type': 'SCALAR'}],
              'bufferViews': [{'buffer': 0, 'byteLength': 36}, {'buffer': 0, 'byteOffset': 36, 'byteLength': 6}],
              'buffers': [{'byteLength': len(blob)}]}
        j = json.dumps(js).encode()
        j += b' ' * (-len(j) % 4)
        data = struct.pack('<4sII', b'glTF', 2, 12 + 8 + len(j) + 8 + len(blob)) + struct.pack('<II', len(j), 0x4E4F534A) + j \
            + struct.pack('<II', len(blob), 0x004E4942) + blob
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / 'c.glb'
            p.write_bytes(data)
            nodes = ps.read_glb_nodes(p)
        self.assertEqual(list(nodes), ['conn_a'])
        np.testing.assert_allclose(nodes['conn_a']['v'], [[0, 0, 0], [1, 0, 0], [0, 0, 1]])   # Blender: (x, -z, y)
        self.assertEqual(nodes['conn_a']['f'].tolist(), [[0, 1, 2]])


class BothSidesTest(unittest.TestCase):
    """Gauntlets / boots take both sides of the wearable as they are (fit lab), unless `mirror` asks for the old behaviour."""

    def setup(self):
        V, F = grid(3)                                   # 18 faces
        part = np.array(['forearm_stretch.r'] * 4 + ['hand.r'] * 2 + ['forearm_stretch.l'] * 4 + ['hand.l'] * 2 + ['spine_01.x'] * 6)
        role = np.array(['primary'] * len(F), dtype='<U12')
        return V, F, role, part, np.zeros(len(F), int)

    def test_own_left_side_is_used(self):
        V, F, role, part, src = self.setup()
        spec = ps.make_items('right_gauntlet_boot', V, F, role, part, src, 0.)['gauntlets']
        self.assertEqual(sorted(spec['pieces']), ['cuff_l', 'cuff_r', 'hand_l', 'hand_r'])
        self.assertEqual(spec['mirrored'], [])
        self.assertEqual(len(spec['pieces']['cuff_l']['F']), 4)
        self.assertTrue(all(p.endswith('.l') for p in spec['pieces']['cuff_l']['part']))
        self.assertIn('6 faces outside', ' '.join(spec['warnings']))

    def test_missing_left_is_mirrored_and_mirror_flag(self):
        V, F, role, part, src = self.setup()
        part[6:12] = 'spine_01.x'                       # no left limb in the wearable
        spec = ps.make_items('right_gauntlet_boot', V, F, role, part, src, 0.)['gauntlets']
        self.assertEqual(sorted(spec['mirrored']), ['cuff_l', 'hand_l'])
        V, F, role, part, src = self.setup()
        spec = ps.make_items('right_gauntlet_boot', V, F, role, part, src, 0., mirror=True)['gauntlets']
        self.assertEqual(sorted(spec['mirrored']), ['cuff_l', 'hand_l'])
        np.testing.assert_allclose(spec['pieces']['cuff_l']['V'][:, 0], -spec['pieces']['cuff_r']['V'][:, 0])


def real_cases():
    ents = {(e['race'], e['module']): e for e in ps.latest(ps.discover())} if paths.MODELS.is_dir() else {}
    return ents


@unittest.skipUnless((paths.MODELS / 'plain_plate_mail_01').is_dir(), 'models/ not available')
class RealDataTest(unittest.TestCase):
    """Wearables land on the base: against parts.npz (normalised base, height 2) in numpy only."""

    def test_all_converted_modules(self):
        ents = real_cases()
        self.assertGreaterEqual(len(ents), 22)
        for (race, module), e in sorted(ents.items()):
            man = ps.load_manifest(e)
            fit = (man.get('parts') or {}).get('fit')
            if not fit:
                continue                                    # older manifest: needs the Blender fit (test_blender_import)
            parts = np.load(paths.base_views(race) / 'parts.npz')
            glb = ps.read_glb(ps.wearable_path(e, '0p4', fitted=False))
            bp = tf.base_from_review(tf.review_from_native(glb['v'], man), fit)
            _, _, d = mt.closest_points(bp, parts['v'], parts['f'])
            limit = .04 if fit.get('residual_fitted_pct_height', 0) < 1.5 else .10     # a poor manifest fit is reported, not hidden
            self.assertLess(np.median(d) / 2, limit, f'{race} {module}: median {np.median(d) / 2:.3f} of height')
            V, F = ps.weld(bp, glb['f'])
            self.assertEqual(len(F), len(glb['f']))
            fd = ps.wearable_face_data(e, '0p4', len(F))
            self.assertIsNotNone(fd, f'{race} {module}: colors.npz arrays do not match the wearable')
            items = ps.make_items(module, V, F, ps.face_roles(glb), fd['part'], fd['src'], 0., mirror=True)
            self.assertTrue(items)
            for item_type, spec in items.items():
                faces = sum(len(p['F']) for p in spec['pieces'].values())
                n_ch = len(spec['channels'])
                for p in spec['pieces'].values():
                    self.assertTrue(p['channel'].max() < n_ch and p['channel'].min() >= 0)
                    self.assertLessEqual(len(p['V']), 3 * len(p['F']))
                if module == 'body_armor':
                    self.assertEqual(faces, len(F))
                    self.assertTrue({'chest', 'pelvis'} <= set(spec['pieces']), f'{race}: {list(spec["pieces"])}')
                else:
                    for n in spec['pieces']:
                        if n.endswith('_r'):
                            self.assertEqual(len(spec['pieces'][n]['F']), len(spec['pieces'][ps.swap_side(n)]['F']))

    def test_mirrored_pieces_are_exact_twins(self):
        e = real_cases()[('dwarf_male', 'right_gauntlet_boot')]
        man = ps.load_manifest(e)
        glb = ps.read_glb(ps.wearable_path(e, '0p4', fitted=False))
        bp = tf.base_from_review(tf.review_from_native(glb['v'], man), man['parts']['fit'])
        V, F = ps.weld(bp, glb['f'])
        fd = ps.wearable_face_data(e, '0p4', len(F))
        spec = ps.make_items('right_gauntlet_boot', V, F, ps.face_roles(glb), fd['part'], fd['src'], 0., mirror=True)['gauntlets']
        r, l = spec['pieces']['cuff_r'], spec['pieces']['cuff_l']
        np.testing.assert_allclose(l['V'] * [-1, 1, 1], r['V'])
        self.assertTrue((l['src'] == ps.MIRRORED).all())
        self.assertTrue(all(p.endswith('.l') or p.endswith('.x') for p in l['part']))

    def test_discovery_skips_attempts_and_other_generations(self):
        ents = ps.discover()
        self.assertFalse([e for e in ents if '_attempt' in e['dir'] or e['reduced'].endswith(('reduced_v2', 'reduced_majority'))])
        self.assertEqual({e['module'] for e in ents} - set(ps.MODULES), set())
        self.assertTrue(all(Path(e['reduced']).name == 'reduced' for e in ents))


if __name__ == '__main__':
    unittest.main(verbosity=2)
