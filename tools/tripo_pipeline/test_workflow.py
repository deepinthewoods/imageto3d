"""Check the state moves and frozen-front contract with small temporary images."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from PIL import Image

import workflow


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        base = self.root / 'base_views/human_male'
        base.mkdir(parents=True)
        image = Image.new('RGBA', (32, 32), (0, 0, 0, 0))
        for x in range(8, 24):
            for y in range(4, 28):
                image.putpixel((x, y), (120, 90, 60, 255))
        for view in workflow.VIEWS:
            image.save(base / (view + '.png'))

    def make_set(self, name, modules):
        result = self.root / 'approved' / name / 'human_male'
        for module in modules:
            target = result / module
            target.mkdir(parents=True)
            shutil.copy2(self.root / 'base_views/human_male/front.png', target / 'front.png')
        return result.parent

    def fill_views(self, claimed):
        for module in claimed['modules']:
            folder = Path(claimed['path']) / module['race_gender'] / module['module']
            for view in workflow.VIEWS[1:]:
                shutil.copy2(Path(claimed['path']) / '.base_views/human_male' / (view + '.png'), folder / (view + '.png'))

    def test_armor_claim_and_ready(self):
        self.make_set('armor01', workflow.ARMOR)
        job = workflow.claim(self.root, 'armor01')
        self.assertFalse((self.root / 'approved/armor01').exists())
        self.fill_views(job)
        ready = workflow.ready(self.root, job['job'])
        self.assertEqual(ready['views'], 8)
        record = json.loads((Path(ready['path']) / '.tripo-job.json').read_text())
        self.assertEqual(record['state'], 'ready_for_tripo')
        self.assertEqual(len(record['view_hashes']), 8)

    def test_outfit_and_missing_view(self):
        self.make_set('outfit01', workflow.OUTFIT)
        job = workflow.claim(self.root, 'outfit01')
        with self.assertRaisesRegex(ValueError, 'expected exactly'):
            workflow.ready(self.root, job['job'])
        self.fill_views(job)
        self.assertEqual(workflow.ready(self.root, job['job'])['views'], 4)

    def test_approved_front_hash_is_frozen(self):
        self.make_set('armor02', workflow.ARMOR)
        job = workflow.claim(self.root, 'armor02')
        self.fill_views(job)
        target = Path(job['path']) / 'human_male/body_armor/front.png'
        picture = Image.open(target).convert('RGBA')
        picture.putpixel((12, 12), (20, 100, 200, 255))
        picture.save(target)
        with self.assertRaisesRegex(ValueError, 'Approved front changed'):
            workflow.ready(self.root, job['job'])
        self.assertTrue(Path(job['path']).is_dir())

    def test_incomplete_armor_pair_rejected(self):
        self.make_set('incomplete', {'body_armor'})
        with self.assertRaisesRegex(ValueError, 'both armor modules'):
            workflow.claim(self.root, 'incomplete')
        self.assertTrue((self.root / 'approved/incomplete').is_dir())

    def test_ready_uses_frozen_base_after_active_resolution_changes(self):
        self.make_set('old_resolution', workflow.OUTFIT)
        job = workflow.claim(self.root, 'old_resolution')
        replacement = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        replacement.putpixel((32, 32), (255, 0, 0, 255))
        for view in workflow.VIEWS:
            replacement.save(self.root / 'base_views/human_male' / (view + '.png'))
        self.fill_views(job)
        ready = workflow.ready(self.root, job['job'])
        checked = workflow.validate_set(self.root, Path(ready['path']), complete=True)
        self.assertEqual({tuple(meta['dimensions']) for meta in checked['images'].values()}, {(32, 32)})
        frozen = Path(ready['path']) / '.base_views/human_male/front.png'
        frozen.unlink()
        with self.assertRaisesRegex(ValueError, 'Frozen base view changed or missing'):
            workflow.validate_set(self.root, Path(ready['path']), complete=True)


if __name__ == '__main__':
    unittest.main()
