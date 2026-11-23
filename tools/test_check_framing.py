"""check_framing.py / palette.py on the real base views: every base against itself is OK, an enlarged
copy is flagged, the archived tricolor bases still use the legacy hue markers.
Run: python3 -m unittest discover -s tools -p 'test_check_framing.py'  (from the project directory)"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
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


GARMENT_GREEN = np.array([25, 210, 40, 255], dtype=np.uint8)


class AnchorInPlaceTest(unittest.TestCase):
    """White anchors judged on their outline: covered = in place, moved or enlarged = flagged."""
    BASE = ROOT / 'base_views/human_male/front.png'

    def setUp(self):
        self.pixels = np.array(Image.open(self.BASE).convert('RGBA'))
        self.blobs = check_framing.anchor_blobs(Image.open(self.BASE))   # head, left hand, left foot (top to bottom)

    def check(self, pixels):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'edited.png'
            Image.fromarray(pixels).save(path)
            return check_framing.check(self.BASE, path)

    def test_hand_partly_under_a_sleeve_is_in_place(self):
        hand = self.blobs[1]['mask']
        xs = np.nonzero(hand)[1]
        cuff = hand & (np.arange(hand.shape[1])[None, :] < np.percentile(xs, 35))   # wrist side (viewer-right hand)
        if xs.mean() < self.pixels.shape[1] / 2:
            cuff = hand & (np.arange(hand.shape[1])[None, :] > np.percentile(xs, 65))
        pixels = self.pixels.copy()
        pixels[cuff] = GARMENT_GREEN
        result = self.check(pixels)
        self.assertTrue(result['ok'], result['problems'])
        marker = result['markers']['left_hand_white']
        self.assertTrue(marker.get('aligned'), marker)
        self.assertGreater(marker['covered'], 0.2)

    def test_moved_hand_is_flagged(self):
        hand = check_framing.dilate(self.blobs[1]['mask'], 2)
        pixels = self.pixels.copy()
        moved = np.roll(self.pixels, 12, axis=1)
        pixels[hand] = 0                                    # the hand is gone from its place ...
        target = np.roll(hand, 12, axis=1)
        pixels[target] = moved[target]                      # ... and drawn 12 px further out
        result = self.check(pixels)
        self.assertFalse(result['ok'])
        self.assertTrue(any(p.startswith('POSE: left_hand_white') for p in result['problems']), result['problems'])
        self.assertFalse(result['markers']['left_hand_white'].get('aligned'))

    def test_covered_foot_drawn_lower_is_flagged(self):
        foot = check_framing.dilate(self.blobs[2]['mask'], 2)
        pixels = self.pixels.copy()
        target = np.roll(foot, 90, axis=0)
        pixels[target] = np.roll(self.pixels, 90, axis=0)[target]   # the white foot painted 90 px lower ...
        pixels[foot] = GARMENT_GREEN                                 # ... below a hem that covers its real place
        result = self.check(pixels)
        self.assertTrue(any(p.startswith('POSE: left_foot_white covered in place') for p in result['problems']),
                        result['problems'])

    def test_white_right_hand_is_side(self):
        right = np.fliplr(self.blobs[1]['mask']) & (self.pixels[..., 3] > 127)   # the T-pose mirror of the left hand
        pixels = self.pixels.copy()
        pixels[right] = (237, 237, 237, 255)                                         # painted in the anchor white
        result = self.check(pixels)
        self.assertTrue(any(p.startswith('SIDE: anchor white') for p in result['problems']), result['problems'])

    def test_enlarged_head_is_framing(self):
        head = self.blobs[0]
        ys, xs = np.nonzero(check_framing.dilate(head['mask'], 3))
        box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
        crop = Image.fromarray(self.pixels).crop(box)
        big = crop.resize((round(crop.width * 1.15), round(crop.height * 1.15)), Image.LANCZOS)
        canvas = Image.fromarray(self.pixels)
        canvas.alpha_composite(big, (round(head['cx'] - big.width * (head['cx'] - box[0]) / crop.width),
                                     round(head['cy'] - big.height * (head['cy'] - box[1]) / crop.height)))
        result = self.check(np.array(canvas))
        self.assertTrue(any(p.startswith('FRAMING: head_white outline') for p in result['problems']), result['problems'])


if __name__ == '__main__':
    unittest.main()
