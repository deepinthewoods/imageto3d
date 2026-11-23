"""Tests of feature_hooks.py (specs, per-set resolution, the studs / rings hooks and the LOD role transfer): venv python.

    characters/.venv/bin/python -m unittest tools/tripo_pipeline/test_feature_hooks.py
"""
import sys
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import feature_hooks as fh  # noqa: E402
from palette import Palette  # noqa: E402
import test_feature_labels as tfl  # noqa: E402


class Specs(unittest.TestCase):
    def test_parse_and_normalise(self):
        self.assertIsNone(fh.parse_spec(None))
        self.assertIsNone(fh.parse_spec('none'))
        self.assertEqual(fh.normalise_spec('studs'), 'studs')
        self.assertEqual(fh.normalise_spec('rings'), 'rings:secondary')
        self.assertEqual(fh.normalise_spec('rings:secondary'), 'rings:secondary')
        self.assertEqual(fh.normalise_spec('rings:void'), 'rings:void:auto')
        self.assertEqual(fh.normalise_spec('rings:void:all'), 'rings:void:all')
        self.assertEqual(fh.normalise_spec('rings:void:1.05'), 'rings:void:1.05')
        self.assertEqual(fh.parse_spec('rings:void:all')['void_above_z'], None)
        self.assertEqual(fh.parse_spec('rings:void')['void_above_z'], 'auto')
        for bad in ('stud', 'studs:x', 'rings:secondary:auto', 'holes', 'rings:void:up'):
            with self.assertRaises(ValueError, msg=bad):
                fh.parse_spec(bad)

    def test_resolution_order_cli_record_config(self):
        cfg = {'sets': {'S': 'studs'}, 'modules': ['body_armor']}
        self.assertEqual(fh.resolve_spec('S', 'body_armor', None, None, cfg), ('studs', 'config.json feature_labels.sets'))
        self.assertEqual(fh.resolve_spec('S', 'outfit', None, None, cfg), (None, None))             # module not listed
        self.assertEqual(fh.resolve_spec('T', 'body_armor', None, None, cfg), (None, None))
        self.assertEqual(fh.resolve_spec('S', 'body_armor', {'feature_labels': 'rings'}, None, cfg)[0], 'rings:secondary')
        self.assertEqual(fh.resolve_spec('S', 'body_armor', {'feature_labels': {'default': 'rings', 'body_armor': 'studs'}}, None, cfg)[0], 'studs')
        self.assertEqual(fh.resolve_spec('S', 'body_armor', {'feature_labels': 'rings'}, 'none', cfg), (None, '--feature-labels'))
        self.assertEqual(fh.resolve_spec('S', 'body_armor', {'feature_labels': 'rings'}, 'studs', cfg)[0], 'studs')
        self.assertEqual(fh.resolve_spec('S', 'body_armor', {}, None, cfg)[0], 'studs')

    def test_the_shipped_config_names_the_five_sets(self):
        cfg = fh.config_block()
        self.assertEqual(fh.normalise_spec(cfg['sets']['chain_shirt_02_second_hand_ringshirt']), 'rings:void:auto')
        self.assertEqual(fh.normalise_spec(cfg['sets']['chain_mail_05_stable_mail_tunic']), 'rings:secondary')
        for s in ('breastplate_07_hand_me_down_cuirass', 'splint_14_garrison_issue', 'studded_leather_09_guardsman_studded_cuirass'):
            self.assertEqual(fh.normalise_spec(cfg['sets'][s]), 'studs')


class StudsHook(unittest.TestCase):
    def test_hook_returns_the_stud_pass_and_never_touches_base_faces(self):
        sc = tfl.Scene()
        pal = Palette.coerce(HERE.parents[1] / 'base_views/human_male')
        roles = list(pal.roles)
        ter = 1 + roles.index('tertiary')
        self.assertEqual(ter, tfl.TER)
        K = len(roles) + 1
        ev = np.zeros((len(sc.f), K))
        ev[:, ter] = sc.ev
        lab = sc.lab.copy()
        lab[:50] = 0                                                       # some base faces
        hook = fh.make_hook('studs', {'radius': .02})
        self.assertEqual(hook.spec, 'studs')
        res = hook(dict(v=sc.v, f=sc.f, lab=lab, area=np.ones(len(sc.f)), ev=ev, pal=pal, log=lambda *a: None))
        new = res['lab']
        self.assertTrue((new[:50] == 0).all())
        self.assertGreater(res['summary']['studs'], 0)
        c = sc.centres['partial']
        core = sc.disc['partial'] & (np.hypot(sc.cent[:, 0] - c[0], sc.cent[:, 1] - c[1]) < .013)
        self.assertTrue((new[core] == tfl.TER).mean() > .99)                 # the half-lost stud is whole again
        self.assertIn('stud_id', res['arrays'])
        self.assertEqual(res['params'], {})

    def test_a_palette_without_tertiary_is_left_alone(self):
        class P:
            roles = ['primary', 'secondary']
        lab = np.array([1, 2, 0])
        res = fh.make_hook('studs')(dict(v=None, f=None, lab=lab, ev=None, pal=P()))
        self.assertTrue((res['lab'] == lab).all())
        self.assertIn('skipped', res['summary'])


class RingsHook(unittest.TestCase):
    def test_void_hook_asks_for_a_finer_field_and_leaves_base_alone(self):
        import test_ring_labels as trl
        v, f, truth, _ = trl.ring_sheet()
        lab = trl.noisy_labels(truth, v, f)
        pal = Palette.legacy_default()
        hook = fh.make_hook('rings:void:all')
        self.assertEqual(hook.spec, 'rings:void:all')
        res = hook(dict(v=v, f=f, lab=lab, area=np.ones(len(f)), ev=None, pal=pal, log=lambda *a: None))
        self.assertEqual(res['params'], {'sigma_pct': .3})
        self.assertTrue((res['lab'][lab == 0] == 0).all())
        self.assertGreater(res['summary']['voided_area'], 0)
        self.assertIn('kind', res['arrays'])

    def test_transfer_for_rings_voids_covered_triangles(self):
        v = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [2, 0, 0], [3, 0, 0], [2, 1, 0]], float)
        f = np.array([[0, 1, 2], [3, 4, 5]])
        cent = np.array([[.3, .3, 0], [.4, .2, 0], [2.3, .3, 0], [2.4, .2, 0]])
        arrays = {'label_old': np.array([2, 2, 1, 1]), 'label_new': np.array([0, 0, 1, 1]), 'kind': np.array([2, 2, 1, 1])}
        out, info = fh.transfer_roles('rings:void:all', v, f, np.array([2, 1]), cent, np.ones(4), arrays, 5)
        self.assertEqual(out.tolist(), [0, 1])
        self.assertEqual(info['faces_changed'], 1)
        out2, _ = fh.transfer_roles('rings:void:all', v, f, np.array([0, 1]), cent, np.ones(4), arrays, 5)   # base stays base
        self.assertEqual(out2.tolist(), [0, 1])


if __name__ == '__main__':
    unittest.main()
