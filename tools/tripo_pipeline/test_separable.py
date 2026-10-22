"""Separable-mode palette (joint role) and the convert_job separable switch."""
import json
from pathlib import Path
import tempfile
import unittest

import convert_job
from palette import Palette

ROOT = Path(__file__).resolve().parents[2]


class PaletteSeparableTest(unittest.TestCase):
    def test_default_palettes_are_unchanged_and_carry_the_optional_block(self):
        for path in sorted((ROOT / 'base_views').glob('*/palette.json')):
            data = json.loads(path.read_text())
            self.assertEqual(data['separable_garments'], [{'role': 'joint', 'hex': '#14DCEB'}], path)
            pal = Palette.load(path)
            self.assertEqual(pal.roles, ['primary', 'secondary', 'tertiary', 'accent'], path)
            self.assertFalse(pal.separable)
            self.assertEqual(pal.scheme, 'white_anchor_v2')

    def test_separable_appends_the_joint_garment_once(self):
        pal = Palette.for_dir(ROOT / 'base_views/human_male', separable=True)
        self.assertEqual(pal.roles, ['primary', 'secondary', 'tertiary', 'accent', 'joint'])
        self.assertEqual(pal.scheme, 'white_anchor_v2_joint')
        self.assertTrue(pal.separable)
        self.assertIs(pal.with_separable(), pal)
        # an explicit 5-colour palette (experiment files) is left alone
        again = Palette.load(ROOT / 'experiments/joint_bands_20261022/palette_joint.json', separable=True) \
            if (ROOT / 'experiments/joint_bands_20261022/palette_joint.json').is_file() else pal
        self.assertEqual(again.roles.count('joint'), 1)
        # classification: cyan is the joint class (index 5), blue stays secondary
        cyan, blue = [[.08, .86, .92]], [[.12, .25, .92]]
        self.assertEqual(int(pal.classify(cyan)[0][0]), 5)
        self.assertEqual(int(pal.classify(blue)[0][0]), 2)

    def test_legacy_default_and_snapshot_without_block_use_the_config_fallback(self):
        pal = Palette.legacy_default(separable=True)
        self.assertEqual(pal.roles, ['primary', 'secondary', 'joint'])
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            data = json.loads((ROOT / 'base_views/human_male/palette.json').read_text())
            del data['separable_garments']
            (d / 'palette.json').write_text(json.dumps(data))
            self.assertEqual(Palette.for_dir(d).roles, ['primary', 'secondary', 'tertiary', 'accent'])
            self.assertEqual(Palette.for_dir(d, separable=True).roles[-1], 'joint')
            self.assertEqual(Palette.coerce(d / 'palette.json', separable=True).roles[-1], 'joint')


class HueFitTest(unittest.TestCase):
    @staticmethod
    def colours(rng, hues):
        import colorsys
        import numpy as np
        return np.concatenate([np.array([colorsys.hsv_to_rgb(((h + rng.normal(0, 2)) % 360) / 360, .9, .9) for _ in range(n)])
                               for h, n in hues])

    def setUp(self):
        import numpy as np
        self.rng = np.random.default_rng(3)
        self.pal = Palette.for_dir(ROOT / 'base_views/human_male', separable=True)

    def test_calibrated_texture_keeps_the_nominal_palette(self):
        sample = self.colours(self.rng, [(357, 3000), (123, 2000), (229, 1000), (51, 800), (303, 700), (184, 600)])
        shifts, report = self.pal.fit_hues(sample)
        self.assertEqual(shifts, {})
        self.assertEqual(report['review'], [])

    def test_hot_pink_accent_is_followed_and_recorded(self):
        import numpy as np
        sample = self.colours(self.rng, [(357, 3000), (123, 2000), (229, 1000), (51, 800), (322, 700), (184, 600)])
        pink = self.colours(self.rng, [(322, 5)])
        self.assertNotEqual(int(self.pal.classify(pink)[0][0]), 4)          # nominal: outside the 20 deg window
        shifts, report = self.pal.fit_hues(sample)
        self.assertEqual(list(shifts), ['accent'])
        self.assertTrue(18 < shifts['accent'] < 22)
        fitted = self.pal.with_hue_shifts(shifts)
        self.assertTrue((fitted.classify(pink)[0] == 4).all())
        self.assertGreater(fitted.classify(pink)[1].min(), .9)
        self.assertEqual(self.pal.hue_shift, {})                              # the original palette is untouched
        self.assertIn('hue_shift', fitted.describe())
        # the other colours still classify as before
        for hue, role in ((123, 1), (229, 2), (51, 3), (184, 5), (357, 0)):
            self.assertEqual(int(fitted.classify(self.colours(self.rng, [(hue, 3)]))[0][0]), role)

    def test_pink_between_red_and_magenta_goes_to_the_colour_without_its_own_peak(self):
        # hue 329 is as near the body red (357) as the accent (301.5), but the body has its own peak
        sample = self.colours(self.rng, [(357, 3000), (123, 2000), (229, 1000), (51, 800), (329, 700)])
        shifts, report = self.pal.fit_hues(sample)
        self.assertEqual(list(shifts), ['accent'])
        self.assertTrue(24 < shifts['accent'] < 31)
        self.assertEqual(report['review'], [])
        pink = self.colours(self.rng, [(329, 5)])
        self.assertTrue((self.pal.with_hue_shifts(shifts).classify(pink)[0] == 4).all())

    def test_shift_is_capped_and_ambiguous_peaks_are_flagged(self):
        sample = self.colours(self.rng, [(357, 3000), (123, 2000), (229, 1000), (51, 800), (345, 700)])   # 44 deg from the accent
        shifts, report = self.pal.fit_hues(sample)
        self.assertNotIn('accent', shifts)                                    # beyond the 30 deg cap
        # a peak between blue (230) and cyan (184) is not clearly closer to either: no shift, flagged for review
        sample = self.colours(self.rng, [(357, 3000), (123, 2000), (206, 1500), (51, 800), (303, 700)])
        shifts, report = self.pal.fit_hues(sample)
        self.assertTrue(report['review'])
        self.assertNotIn('joint', shifts)
        self.assertNotIn('secondary', shifts)

    def test_two_colours_never_share_one_cluster(self):
        # one cluster at 150 deg is within 30 deg of green (125) only; a cluster at 205 is equally near blue/cyan
        sample = self.colours(self.rng, [(357, 3000), (150, 2000), (51, 800)])
        shifts, report = self.pal.fit_hues(sample)
        self.assertEqual(list(shifts), ['primary'])
        used = [c['peak'] for n, c in report['colours'].items() if c.get('applied')]
        self.assertEqual(len(used), len(set(used)))


class ConvertJobSeparableTest(unittest.TestCase):
    def test_job_is_separable_reads_the_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / 'human_male' / 'body_armor').mkdir(parents=True)
            (job / '.tripo-job.json').write_text(json.dumps({'state': 'x'}))
            self.assertFalse(convert_job.job_is_separable(job, 'human_male', 'body_armor'))
            (job / 'human_male' / 'body_armor' / 'prompts.json').write_text(json.dumps({'note': 'plain separable pauldrons'}))
            self.assertFalse(convert_job.job_is_separable(job, 'human_male', 'body_armor'))      # pauldron-only needs no 5th colour
            (job / 'human_male' / 'body_armor' / 'prompts.json').write_text(json.dumps({'joint_bands': True}))
            self.assertTrue(convert_job.job_is_separable(job, 'human_male', 'body_armor'))
            (job / 'human_male' / 'body_armor' / 'prompts.json').write_text(json.dumps({'prompt': 'cyan JOINT BANDS at armpit'}))
            self.assertTrue(convert_job.job_is_separable(job, 'human_male', 'body_armor'))


    def test_job_is_piece_colours_reads_the_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            (job / 'human_male' / 'body_armor').mkdir(parents=True)
            (job / '.tripo-job.json').write_text(json.dumps({'state': 'x'}))
            self.assertFalse(convert_job.job_is_piece_colours(job, 'human_male', 'body_armor'))
            (job / 'human_male' / 'body_armor' / 'prompts.json').write_text(json.dumps({'piece_colours': True}))
            self.assertTrue(convert_job.job_is_piece_colours(job, 'human_male', 'body_armor'))
            (job / 'human_male' / 'body_armor' / 'prompts.json').write_text(json.dumps({'prompt': 'rigid sections with per-piece base colours'}))
            self.assertTrue(convert_job.job_is_piece_colours(job, 'human_male', 'body_armor'))
            self.assertFalse(convert_job.job_is_separable(job, 'human_male', 'body_armor'))   # no joint role needed


if __name__ == '__main__':
    unittest.main()
