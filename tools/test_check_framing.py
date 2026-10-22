"""check_framing.py / palette.py on the real base views: every base against itself is OK, an enlarged
copy is flagged, the archived tricolor bases still use the legacy hue markers.
Run: python3 -m unittest discover -s tools -p 'test_check_framing.py'  (from the project directory)"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_framing  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
VIEWS = ('front', 'front_three_quarter', 'back', 'back_three_quarter')


def bases(folder):
    return sorted(p for p in (ROOT / folder).iterdir() if (p / 'front.png').is_file())


class FramingTest(unittest.TestCase):
    def test_every_active_base_view_matches_itself(self):
        found = bases('base_views')
        self.assertTrue(found)
        for base in found:
            for view in VIEWS:
                result = check_framing.check(base / f'{view}.png', base / f'{view}.png')
                self.assertTrue(result['ok'], f'{base.name}/{view}: {result["problems"]}')
                if (base / 'palette.json').is_file():
                    self.assertIn(result['palette_scheme'], ('white_anchor_v2', 'tricolor_v1'))

    def test_archived_tricolor_bases_use_hue_markers(self):
        for base in bases('archive/base_views_markers_tricolor_20261022'):
            result = check_framing.check(base / 'front.png', base / 'front.png')
            self.assertTrue(result['ok'])
            self.assertEqual(result['palette_scheme'], 'tricolor_v1')
            self.assertIn('head_yellow', result['markers'])

    def test_enlarged_figure_is_flagged(self):
        base = ROOT / 'base_views/human_male/front.png'
        image = Image.open(base).convert('RGBA')
        w, h = image.size
        big = image.resize((int(w * 1.1), int(h * 1.1)), Image.LANCZOS)
        left, top = (big.size[0] - w) // 2, (big.size[1] - h) // 2
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'enlarged.png'
            big.crop((left, top, left + w, top + h)).save(path)
            result = check_framing.check(base, path)
        self.assertFalse(result['ok'])
        self.assertTrue(any(p.startswith('FRAMING') for p in result['problems']), result['problems'])


if __name__ == '__main__':
    unittest.main()
