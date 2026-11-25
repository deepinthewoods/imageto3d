"""png_check.py: the one technical validator of generated view images and their provenance records (recheck-before-cutover
finding 15), shared by tools/view_batch.py and the preview app.

    python -X utf8 -m pytest tools/test_png_check.py -q
"""
import builtins
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import png_check  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def image(self, name='view.png', mode='RGBA', clear=0.5, opaque=0.5, fmt=None, size=(10, 10)):
        """An image with the given shares of fully transparent / fully opaque pixels (the rest half transparent)."""
        from PIL import Image
        path = self.dir / name
        image = Image.new(mode, size, (200, 10, 10, 128) if mode == 'RGBA' else (200, 10, 10) if mode == 'RGB' else 0)
        if mode == 'RGBA':
            pixels = image.load()
            total = size[0] * size[1]
            n_clear, n_opaque = round(clear * total), round(opaque * total)
            for i in range(total):
                pixels[i % size[0], i // size[0]] = (200, 10, 10, 0 if i < n_clear else 255 if i < n_clear + n_opaque else 128)
        image.save(path, format=fmt)
        return path


class PngProblemTests(Base):
    def test_a_usable_view(self):
        self.assertIsNone(png_check.png_problem(self.image()))
        self.assertIsNone(png_check.png_problem(str(self.image('s.png'))))                 # str paths too

    def test_the_share_thresholds_are_inclusive_at_one_percent(self):
        self.assertEqual(png_check.MIN_SHARE, 0.01)
        self.assertIsNone(png_check.png_problem(self.image('edge.png', clear=0.01, opaque=0.01)))
        reason = png_check.png_problem(self.image('opaque.png', clear=0.0, opaque=1.0))
        self.assertEqual(reason, 'only 0.00% fully transparent pixels (at least 1% needed)')
        reason = png_check.png_problem(self.image('clear.png', clear=1.0, opaque=0.0))     # the reviewer's invisible PNG
        self.assertEqual(reason, 'only 0.00% solid pixels (alpha >= 250; at least 1% needed)')
        big = self.image('big.png', clear=0.005, opaque=0.9, size=(100, 100))              # 0.5% transparent: not enough
        self.assertIn('fully transparent', png_check.png_problem(big))
        self.assertIn('solid pixels', png_check.png_problem(self.image('soft.png', clear=0.5, opaque=0.0)))   # only half-transparent ink

    def test_real_cutouts_draw_their_subject_just_below_255(self):
        """Codex views put the subject at alpha ~250-254 (measured on real monster images): that counts as solid."""
        from PIL import Image
        path = Path(self.temp.name) / 'soft_edge.png'
        image = Image.new('RGBA', (10, 10), (0, 0, 0, 0))
        for i in range(40):
            image.putpixel((i % 10, i // 10), (200, 10, 10, 252))
        image.save(path)
        self.assertIsNone(png_check.png_problem(path))

    def test_mode_format_and_decoding(self):
        self.assertEqual(png_check.png_problem(self.image('rgb.png', mode='RGB')), 'mode RGB, not RGBA')
        self.assertEqual(png_check.png_problem(self.image('grey.png', mode='L')), 'mode L, not RGBA')
        self.assertEqual(png_check.png_problem(self.image('really_a.png', fmt='TIFF')), 'not a PNG (TIFF)')
        self.assertEqual(png_check.png_problem(self.dir / 'nope.png'), 'missing')
        header = self.dir / 'header.png'
        header.write_bytes(b'\x89PNG\r\n\x1a\n' + b'garbage' * 10)                          # a PNG signature only
        self.assertTrue(png_check.png_problem(header).startswith('does not decode ('))
        whole = self.image('whole.png').read_bytes()
        cut = self.dir / 'cut.png'
        cut.write_bytes(whole[:len(whole) // 2])                                             # truncated
        self.assertTrue(png_check.png_problem(cut).startswith('does not decode ('))
        text = self.dir / 'text.png'
        text.write_text('not an image')
        self.assertTrue(png_check.png_problem(text).startswith('does not decode ('))
        self.assertTrue(png_check.png_problem(self.dir).startswith('does not decode ('))    # a directory

    def test_it_fails_closed_without_pillow(self):
        path = self.image()
        real = builtins.__import__

        def no_pil(name, *args, **kwargs):
            if name == 'PIL' or name.startswith('PIL.'):
                raise ImportError('no PIL')
            return real(name, *args, **kwargs)
        with mock.patch.object(builtins, '__import__', no_pil):
            self.assertEqual(png_check.png_problem(path), 'cannot validate: Pillow is not installed for this python')

    def test_it_never_changes_the_file(self):
        path = self.image()
        before = (path.read_bytes(), sorted(p.name for p in self.dir.iterdir()))
        png_check.png_problem(path)
        self.assertEqual((path.read_bytes(), sorted(p.name for p in self.dir.iterdir())), before)
        path.unlink()                                                                        # and keeps no handle open (Windows)


class ProvenanceTests(Base):
    def record(self, text, name='view.prompts.json', encoding='utf-8'):
        path = self.dir / name
        path.write_text(text, encoding=encoding)
        return path

    def test_a_json_object_is_required(self):
        self.assertIsNone(png_check.provenance_problem(self.record('{"prompt": "x"}')))
        self.assertIsNone(png_check.provenance_problem(self.record('{}', 'empty.json')))
        self.assertIsNone(png_check.provenance_problem(self.record('{"a": 1}', 'bom.json', encoding='utf-8-sig')))
        self.assertIsNone(png_check.provenance_problem(str(self.record('{"a": 1}', 'str.json'))))
        for text in ('[]', '[{"prompt": "x"}]', '"text"', '3', 'null', 'true'):
            self.assertEqual(png_check.provenance_problem(self.record(text)), 'provenance is not a JSON object', text)

    def test_missing_and_unreadable(self):
        self.assertEqual(png_check.provenance_problem(self.dir / 'nope.json'), 'provenance missing')
        self.assertEqual(png_check.provenance_problem(self.dir), 'provenance missing')      # a directory is not a record
        self.assertEqual(png_check.provenance_problem(self.record('{broken')), 'provenance is not readable JSON (JSONDecodeError)')
        self.assertEqual(png_check.provenance_problem(self.record('')), 'provenance is not readable JSON (JSONDecodeError)')
        bad = self.dir / 'latin.json'
        bad.write_bytes(b'{"a": "\xff\xfe"}')
        self.assertEqual(png_check.provenance_problem(bad), 'provenance is not readable JSON (UnicodeDecodeError)')


class ViewProblemsTests(Base):
    def test_image_and_record_together(self):
        png = self.image()
        record = self.dir / 'view.prompts.json'
        self.assertEqual(png_check.view_problems(png), [])                                   # no provenance asked for
        self.assertEqual(png_check.view_problems(png, record), [(record, 'provenance missing')])
        record.write_text(json.dumps({'prompt': 'x'}))
        self.assertEqual(png_check.view_problems(png, record), [])
        bad = self.image('bad.png', mode='RGB')
        record.write_text('[]')
        self.assertEqual(png_check.view_problems(str(bad), str(record)),
                         [(bad, 'mode RGB, not RGBA'), (record, 'provenance is not a JSON object')])

    def test_provenance_only_is_the_repair_without_regeneration_rule(self):
        png, record = self.image(), self.dir / 'view.prompts.json'
        problems = png_check.view_problems(png, record)
        self.assertTrue(png_check.provenance_only(problems, [record]))                       # good image, missing record
        self.assertTrue(png_check.provenance_only(problems, [str(record)]))
        self.assertFalse(png_check.provenance_only([], [record]))                            # nothing wrong
        bad = self.image('bad.png', mode='RGB')
        self.assertFalse(png_check.provenance_only(png_check.view_problems(bad, record), [record]))   # the image fails too
        self.assertFalse(png_check.provenance_only([(None, 'unknown validate rule')], [record]))


if __name__ == '__main__':
    unittest.main()
