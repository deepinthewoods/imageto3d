"""Monster variant scan (monsters.py, used by v2 monsters_v2.py). Run with python -m unittest tools/preview/test_monsters.py."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('preview_monsters', Path(__file__).with_name('monsters.py'))
monsters = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monsters)


class MonsterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / 'imageto3d'          # stands for the imageto3d root
        self.base = Path(self.tmp.name) / 'monsters' / 'aaa'      # its sibling, found without the env override
        self.project.mkdir()
        self.saved = os.environ.pop('IMAGETO3D_MONSTERS', None)
        views = self.base / 'pipeline' / 'Rust Monster' / 'views' / 'codex1'
        views.mkdir(parents=True)
        (views / 'front.png').write_bytes(b'png')
        (views / 'colours.json').write_text(json.dumps({'roles': {'skin': {'hex': '#E61923', 'parts': ['body']}}}))
        (self.base / 'source.png').write_bytes(b'png')
        (views / 'prompts.json').write_text(json.dumps({'source_image_path': str(self.base / 'source.png')}))
        self.model = self.base / 'pipeline' / 'Rust Monster' / 'models' / 'codex1'
        (self.model / 'hyper3d').mkdir(parents=True)
        (self.model / 'reduced').mkdir()

    def tearDown(self):
        if self.saved is not None:
            os.environ['IMAGETO3D_MONSTERS'] = self.saved
        self.tmp.cleanup()

    def convert(self):
        (self.model / 'hyper3d' / 'studio_submission.json').write_text(json.dumps({'status': 'exported', 'studio_result': {'faces': 1000000}}))
        (self.model / 'hyper3d' / 'glb_base_basic_pbr.glb').write_bytes(b'glb')

    def reduce(self, levels=('0p4', '0p2')):
        for level in levels:
            (self.model / 'reduced' / f'lod_error_{level}.glb').write_bytes(b'glb')
        (self.model / 'reduced' / 'manifest.json').write_text(json.dumps({'levels': {lv: {'triangles': 1000 * (i + 1)} for i, lv in enumerate(levels)}}))

    def test_missing_folder_is_an_empty_tab(self):
        self.assertEqual(monsters.cards(Path(self.tmp.name) / 'elsewhere' / 'imageto3d'), [])

    def test_card_follows_the_pipeline_stage(self):
        card, = monsters.cards(self.project)
        self.assertEqual((card['id'], card['kind'], card['name'], card['status']), ('monster:Rust Monster/codex1', 'monster', 'Rust Monster codex1', 'image only'))
        self.assertTrue(card['image'].startswith('/mfiles/pipeline/Rust%20Monster/views/codex1/front.png'))
        self.assertTrue(card['source_image'].startswith('/mfiles/source.png'))
        self.assertEqual(card['roles'], [{'role': 'skin', 'hex': '#E61923', 'parts': ['body']}])
        self.assertFalse(set(card) & {'races', 'views', 'jobs', 'entries'})        # keys the old app.js used to pick an outfit view
        self.convert()
        card, = monsters.cards(self.project)
        self.assertEqual((card['status'], card['highpoly_faces'], card['lods']), ('converted', 1000000, {}))
        self.assertIn('glb_base_basic_pbr.glb', card['highpoly'])
        self.reduce()
        card, = monsters.cards(self.project)
        self.assertEqual(card['status'], 'reduced')
        self.assertEqual({lv: v['faces'] for lv, v in card['lods'].items()}, {'0p4': 1000, '0p2': 2000})
        self.assertIsNone(card['lod'])

    def test_files_are_served_only_from_the_monsters_folder(self):
        self.assertEqual(monsters.resolve_file(self.project, '/mfiles/pipeline/Rust%20Monster/views/codex1/front.png'),
                         self.base / 'pipeline' / 'Rust Monster' / 'views' / 'codex1' / 'front.png')
        self.assertIsNone(monsters.resolve_file(self.project, '/mfiles/pipeline/Rust%20Monster/views/codex1/colours.json'))
        (Path(self.tmp.name) / 'secret.png').write_bytes(b'png')
        self.assertIsNone(monsters.resolve_file(self.project, '/mfiles/../../secret.png'))
        self.assertIsNone(monsters.resolve_file(self.project, '/mfiles/pipeline/missing.png'))

    def test_choice_needs_a_level_file_and_keeps_other_fields(self):
        self.convert()
        self.reduce()
        key = {'id': 'monster:Rust Monster/codex1'}
        for bad in ({'id': 'model:x'}, key | {'lod': '0p1'}, key | {'lod': '9'}, key | {'approved': True}, key | {'approved': 'yes'},
                    {'id': 'monster:Rust Monster/../codex1', 'lod': '0p4'}, {'id': 'monster:../x/codex1', 'lod': '0p4'}):
            with self.assertRaises(ValueError, msg=bad):
                monsters.write_choice(self.project, bad)
        self.assertFalse((self.model / monsters.CHOICE).exists())
        self.assertEqual(monsters.write_choice(self.project, key | {'lod': '0p2'}), {'ok': True, 'lod': '0p2', 'approved': False})
        self.assertEqual(monsters.write_choice(self.project, key | {'approved': True}), {'ok': True, 'lod': '0p2', 'approved': True})
        record = json.loads((self.model / monsters.CHOICE).read_text())
        self.assertEqual((record['lod'], record['reduced'], record['approved']), ('0p2', 'reduced/lod_error_0p2.glb', True))
        card, = monsters.cards(self.project)
        self.assertEqual((card['lod'], card['approved']), ('0p2', True))
        self.assertEqual(monsters.write_choice(self.project, key | {'lod': '0p4'})['approved'], True)


if __name__ == '__main__':
    unittest.main()
