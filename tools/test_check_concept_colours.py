"""check_concept_colours.py on synthetic concept images: nominal colours pass, a hot-pink accent is flagged.
Run: python3 -m unittest discover -s tools -p 'test_check_concept_colours.py'  (from the project directory)"""
import colorsys
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_concept_colours  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tripo_pipeline'))
from palette import Palette  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def hexrgb(h):
    return [int(h[i:i + 2], 16) for i in (1, 3, 5)]


def image(path, patches, hue_accent=None):
    """Alpha canvas with one block per colour; `patches` maps hex -> block, hue_accent overrides the accent hue."""
    arr = np.zeros((200, 200, 4), np.uint8)
    for i, (name, hx) in enumerate(patches.items()):
        rgb = hexrgb(hx)
        if name == 'accent' and hue_accent is not None:
            rgb = [int(c * 255) for c in colorsys.hsv_to_rgb(hue_accent / 360, .9, .88)]
        arr[i * 30:(i + 1) * 30, :150] = [*rgb, 255]
    Image.fromarray(arr).save(path)


class ConceptColourTest(unittest.TestCase):
    def setUp(self):
        self.pal = Palette.for_dir(ROOT / 'base_views/human_male')
        self.patches = {'body': self.pal.base_hex, **{r: h for r, h in self.pal.garments}}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_nominal_colours_pass(self):
        p = Path(self.tmp.name) / 'ok.png'
        image(p, self.patches)
        r = check_concept_colours.measure(p, self.pal)
        self.assertTrue(r['ok'], r['problems'])
        for n in self.patches:
            self.assertEqual(r['colours'][n]['status'], 'ok', n)

    def test_hot_pink_accent_is_flagged(self):
        p = Path(self.tmp.name) / 'pink.png'
        image(p, self.patches, hue_accent=322)
        r = check_concept_colours.measure(p, self.pal)
        self.assertFalse(r['ok'])
        self.assertEqual(r['colours']['accent']['status'], 'DRIFT')
        self.assertGreater(r['colours']['accent']['drift'], 12)
        self.assertEqual(r['colours']['primary']['status'], 'ok')
        self.assertEqual(check_concept_colours.main([str(p), '--palette', str(ROOT / 'base_views/human_male')]), 1)

    def test_small_drift_under_threshold_passes(self):
        p = Path(self.tmp.name) / 'near.png'
        image(p, self.patches, hue_accent=308)
        self.assertTrue(check_concept_colours.measure(p, self.pal)['ok'])

    def test_absent_colour_is_not_a_problem(self):
        p = Path(self.tmp.name) / 'few.png'
        image(p, {k: v for k, v in self.patches.items() if k != 'tertiary'})
        r = check_concept_colours.measure(p, self.pal)
        self.assertTrue(r['ok'])
        self.assertEqual(r['colours']['tertiary']['status'], 'absent')


if __name__ == '__main__':
    unittest.main()
