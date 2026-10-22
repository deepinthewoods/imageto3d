"""Check the state moves and frozen-front contract with small temporary images."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from PIL import Image

import convert_job
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
        (base / 'regions.json').write_text('{}')
        (base / 'parts.npz').write_bytes(b'parts')
        (base / 'clearance.json').write_text('{}')

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

    def test_claim_freezes_regions_and_parts(self):
        self.make_set('armor03', workflow.ARMOR)
        job = workflow.claim(self.root, 'armor03')
        frozen = Path(job['path']) / '.base_views/human_male'
        self.assertEqual((frozen / 'parts.npz').read_bytes(), b'parts')
        record = json.loads((Path(job['path']) / '.tripo-job.json').read_text())
        self.assertEqual((frozen / 'clearance.json').read_text(), '{}')
        self.assertIn('human_male/parts.npz', record['base_view_hashes'])
        self.assertIn('human_male/regions.json', record['base_view_hashes'])

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


RED, YELLOW, WHITE = (230, 25, 35, 255), (245, 220, 20, 255), (237, 237, 237, 255)
LEGACY = {'scheme': 'tricolor_v1', 'base': {'name': 'body_red', 'hex': '#E61923'},
          'anchors': {'kind': 'hue', 'markers': [{'part': 'head', 'hex': '#F5DC14'}]},
          'garments': [{'role': 'primary', 'hex': '#19D228'}, {'role': 'secondary', 'hex': '#1E41EB'}]}
WHITE_V2 = {'scheme': 'white_anchor_v2', 'base': {'name': 'body_red', 'hex': '#E61923'},
            'anchors': {'kind': 'achromatic', 'hex': '#EDEDED', 'parts': ['head', 'left_hand', 'left_foot']},
            'garments': [{'role': 'primary', 'hex': '#19D228'}, {'role': 'secondary', 'hex': '#1E41EB'},
                         {'role': 'tertiary', 'hex': '#F5DC14'}, {'role': 'accent', 'hex': '#E119DC'}]}


def marker_image(head):
    image = Image.new('RGBA', (100, 100), (0, 0, 0, 0))
    for x in range(30, 70):
        for y in range(10, 90):
            image.putpixel((x, y), head if (x in range(40, 60) and y in range(10, 30)) else RED)
    return image


class BaseDetectionTest(unittest.TestCase):
    """claim snapshots the base the approved fronts were drawn on (anchor colours), not blindly the active one."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write_base(self.root / 'base_views/human_male', WHITE, WHITE_V2)
        self.write_base(self.root / 'archive/base_views_tricolor_x/human_male', YELLOW, LEGACY)

    def write_base(self, folder, head, palette):
        folder.mkdir(parents=True)
        for view in workflow.VIEWS:
            marker_image(head).save(folder / (view + '.png'))
        (folder / 'palette.json').write_text(json.dumps(palette))
        (folder / 'regions.json').write_text('{}')

    def make_set(self, name, head, modules=('outfit',)):
        race = self.root / 'approved' / name / 'human_male'
        race.mkdir(parents=True)
        for module in modules:
            marker_image(head).save(race / f'{module}_front.png')

    def frozen_scheme(self, job):
        return json.loads((Path(job['path']) / '.base_views/human_male/palette.json').read_text())['scheme']

    def test_front_on_active_base_uses_active_base(self):
        self.make_set('new', WHITE)
        job = workflow.claim(self.root, 'new')
        self.assertEqual(self.frozen_scheme(job), 'white_anchor_v2')
        self.assertEqual(job['base_views']['human_male']['path'], 'base_views/human_male')

    def test_front_on_old_tricolor_base_uses_archive(self):
        self.make_set('old', YELLOW)
        job = workflow.claim(self.root, 'old')
        self.assertEqual(self.frozen_scheme(job), 'tricolor_v1')
        self.assertEqual(job['base_views']['human_male']['path'], 'archive/base_views_tricolor_x/human_male')
        record = json.loads((Path(job['path']) / '.tripo-job.json').read_text())
        self.assertIn('human_male/palette.json', record['base_view_hashes'])
        self.assertEqual(record['base_view_sources']['human_male']['palette_scheme'], 'tricolor_v1')

    def test_front_matching_nothing_fails_closed(self):
        self.make_set('odd', (20, 30, 240, 255))      # blue head: neither white nor yellow
        with self.assertRaisesRegex(ValueError, 'match neither'):
            workflow.claim(self.root, 'odd')
        self.assertTrue((self.root / 'approved/odd').is_dir())

    def test_two_different_matching_archives_are_ambiguous(self):
        other = self.root / 'archive/base_views_tricolor_y/human_male'
        self.write_base(other, YELLOW, LEGACY)
        picture = Image.open(other / 'front.png')
        picture.putpixel((31, 80), (1, 2, 3, 255))     # a different frozen view set
        picture.save(other / 'front.png')
        self.make_set('amb', YELLOW)
        with self.assertRaisesRegex(ValueError, 'several different archived bases'):
            workflow.claim(self.root, 'amb')

    def test_base_views_override(self):
        self.make_set('forced', WHITE)
        job = workflow.claim(self.root, 'forced', base_views=self.root / 'archive/base_views_tricolor_x')
        self.assertEqual(self.frozen_scheme(job), 'tricolor_v1')
        self.assertEqual(job['base_views']['human_male']['selected_by'], '--base-views override')


class PromoteTest(unittest.TestCase):
    """promote copies one concept of a flat batch folder into the per-set layout; never overwrites."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        base = self.root / 'base_views/human_male'
        base.mkdir(parents=True)
        self.image = Image.new('RGBA', (32, 32), (0, 0, 0, 0))
        for x in range(8, 24):
            for y in range(4, 28):
                self.image.putpixel((x, y), (120, 90, 60, 255))
        for view in workflow.VIEWS:
            self.image.save(base / (view + '.png'))
        self.batch = self.root / 'concepts/batch_01'
        self.batch.mkdir(parents=True)

    def add(self, nn, slug, present, **meta):
        for module in present:
            self.image.save(self.batch / f'{nn}_{slug}__{module}.png')
        (self.batch / f'{nn}_{slug}.json').write_text(json.dumps({'race_gender': 'human_male', 'brief': 'x', **meta}))

    def test_promote_armor_pair_to_concepts(self):
        self.add('07', 'sergeant_plate', ['body_armor', 'right_gauntlet_boot'])
        result = workflow.promote(self.root, self.batch, '7')
        target = self.root / 'concepts/batch_01_07_sergeant_plate/human_male'
        self.assertEqual(result['stage'], 'concepts')
        self.assertEqual(sorted(p.name for p in target.iterdir()),
                         ['body_armor_front.png', 'body_armor_prompts.json', 'right_gauntlet_boot_front.png',
                          'right_gauntlet_boot_prompts.json'])
        record = json.loads((target / 'body_armor_prompts.json').read_text())
        self.assertEqual(record['module'], 'body_armor')
        self.assertEqual(record['promoted_from']['file'], '07_sergeant_plate__body_armor.png')
        self.assertTrue((self.batch / '07_sergeant_plate__body_armor.png').is_file())   # batch untouched
        with self.assertRaises(FileExistsError):
            workflow.promote(self.root, self.batch, '07')

    def test_promote_straight_into_approved_then_claim(self):
        self.add('12', 'blue_robe', ['outfit'])
        result = workflow.promote(self.root, self.batch, '12', set_id='blue_robe', approve=True)
        self.assertEqual(result['stage'], 'approved')
        self.assertTrue((self.root / 'approved/blue_robe/human_male/outfit_front.png').is_file())
        self.assertEqual(workflow.claim(self.root, 'blue_robe')['modules'], [{'race_gender': 'human_male', 'module': 'outfit'}])

    def test_incomplete_missing_record_and_bad_inputs_fail_closed(self):
        self.add('01', 'half', ['body_armor'])
        with self.assertRaisesRegex(ValueError, 'both armor modules or one outfit'):
            workflow.promote(self.root, self.batch, '01')
        self.image.save(self.batch / '02_norecord__outfit.png')
        with self.assertRaisesRegex(ValueError, 'Missing record'):
            workflow.promote(self.root, self.batch, '02')
        with self.assertRaisesRegex(ValueError, 'No concept 9'):
            workflow.promote(self.root, self.batch, '9')
        self.add('03', 'a', ['outfit'])
        self.add('003', 'b', ['outfit'])
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            workflow.promote(self.root, self.batch, '03')
        self.add('04', 'wrongsize', ['outfit'])
        Image.new('RGBA', (16, 16), (0, 0, 0, 0)).save(self.batch / '04_wrongsize__outfit.png')
        with self.assertRaisesRegex(ValueError, 'Wrong dimensions'):
            workflow.promote(self.root, self.batch, '04')
        self.assertFalse((self.root / 'concepts/batch_01_04_wrongsize').exists())
        self.assertEqual([p.name for p in (self.root / 'concepts').iterdir()], ['batch_01'])   # no temp left behind

    def test_recorded_sha_must_match(self):
        self.add('05', 'sha', ['outfit'], modules={'outfit': {'sha256': '0' * 64}})
        with self.assertRaisesRegex(ValueError, 'does not match the sha256'):
            workflow.promote(self.root, self.batch, '05')


class SplitStepTest(unittest.TestCase):
    """convert_job's split step: method selection and the segment-first -> per-face fallback, subprocess mocked."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / 'models/set/job/human_male/body_armor'
        (self.out / 'reduced').mkdir(parents=True)
        (self.out / 'source.glb').write_bytes(b'glb')
        (self.out / 'reduced/manifest.json').write_text(json.dumps({
            'cameras': 'base_views/human_male/transforms.json', 'regions': 'base_views/human_male/regions.json',
            'palette': {'scheme': 'white_anchor_v2', 'garments': [{'role': 'primary', 'hex': '#19D228'}]}}))
        self.calls = []
        for patcher in (mock.patch.object(convert_job, 'ROOT', self.root), mock.patch.object(convert_job, 'BLENDER', 'blender'),
                        mock.patch.object(convert_job, 'segment_first_python', return_value='venv-python'),
                        mock.patch.object(convert_job.subprocess, 'run', side_effect=self.fake_run)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fail_on = set()

    def fake_run(self, command, **kwargs):
        name = next(Path(c).name for c in command if str(c).endswith('.py'))
        self.calls.append((name, command))
        if name in self.fail_on:
            kwargs['stdout'].write('boom: something broke\n')
            return mock.Mock(returncode=1)
        if name == 'segment_first_prep.py':
            prep = Path(command[command.index('--') + 2])
            prep.mkdir(parents=True)
            (prep / 'prep.npz').write_bytes(b'npz')
        elif name == 'segment_first.py':
            out = Path(command[3])
            (out / 'pieces').mkdir(parents=True, exist_ok=True)
            (out / 'pieces/pieces.json').write_text('{}')
        elif name == 'split_pieces.py':
            (self.out / 'pieces').mkdir()
            (self.out / 'pieces/pieces.json').write_text('{}')
        return mock.Mock(returncode=0)

    def test_method_selection(self):
        choose = convert_job.choose_split_method
        self.assertEqual(choose(None, 'body_armor', True, False)[0], 'segment-first')
        self.assertEqual(choose(None, 'body_armor', False, False)[0], 'none')
        self.assertEqual(choose(None, 'body_armor', True, True)[0], 'none')          # separable joint-band jobs: split_pieces by hand
        self.assertEqual(choose(None, 'outfit', True, False)[0], 'none')
        self.assertEqual(choose('per-face', 'body_armor', True, False)[0], 'per-face')
        self.assertEqual(choose('segment-first', 'body_armor', False, False)[0], 'segment-first')
        self.assertEqual(choose('none', 'body_armor', True, False)[0], 'none')
        self.assertEqual(choose('segment-first', 'right_gauntlet_boot', True, False)[0], 'none')
        with self.assertRaises(ValueError):
            choose('bogus', 'body_armor', True, False)

    def test_segment_first_runs_prep_then_split_with_the_reductions_inputs(self):
        record = convert_job.split_step(self.out, 'body_armor', 'segment-first')
        self.assertEqual((record['status'], record['method']), ('done', 'segment-first'))
        self.assertEqual([n for n, _ in self.calls], ['segment_first_prep.py', 'segment_first.py'])
        prep = self.calls[0][1]
        self.assertEqual(prep[0], 'blender')
        for flag, value in (('--module', 'body_armor'), ('--cameras', 'base_views/human_male/transforms.json'),
                            ('--regions', 'base_views/human_male/regions.json')):
            self.assertEqual(prep[prep.index(flag) + 1], value)
        self.assertEqual(json.loads(Path(prep[prep.index('--palette') + 1]).read_text())['scheme'], 'white_anchor_v2')
        split = self.calls[1][1]
        self.assertEqual(split[0], 'venv-python')
        self.assertEqual(split[split.index('--reduced') + 1], str(self.out / 'reduced'))
        self.assertEqual(split[split.index('--prep-dir') + 1], str(self.out / 'segment_first/prep'))
        self.assertEqual(json.loads((self.out / 'split_step.json').read_text())['status'], 'done')
        self.assertEqual(record['outputs'], 'models/set/job/human_male/body_armor/segment_first')

    def test_segment_first_is_resumable(self):
        convert_job.split_step(self.out, 'body_armor', 'segment-first')
        self.calls.clear()
        record = convert_job.split_step(self.out, 'body_armor', 'segment-first')
        self.assertEqual((self.calls, record['status']), ([], 'done'))
        (self.out / 'segment_first/pieces/pieces.json').unlink()                      # prep is kept, only the split reruns
        convert_job.split_step(self.out, 'body_armor', 'segment-first')
        self.assertEqual([n for n, _ in self.calls], ['segment_first.py'])

    def test_failed_segment_first_falls_back_to_per_face_and_says_why(self):
        self.fail_on = {'segment_first.py'}
        record = convert_job.split_step(self.out, 'body_armor', 'segment-first')
        self.assertEqual((record['status'], record['method'], record['requested']), ('fallback', 'per-face', 'segment-first'))
        self.assertIn('boom: something broke', record['fallback_reason'])
        split_pieces = self.calls[-1][1]
        self.assertEqual(split_pieces[split_pieces.index('--rigid') + 1], 'auto')
        self.assertEqual(split_pieces[0], 'venv-python')
        self.assertEqual(json.loads((self.out / 'split_step.json').read_text())['method'], 'per-face')

    def test_missing_venv_python_falls_back(self):
        with mock.patch.object(convert_job, 'segment_first_python', side_effect=FileNotFoundError('no python with numpy+scipy')):
            record = convert_job.split_step(self.out, 'body_armor', 'segment-first')
        self.assertEqual(record['status'], 'fallback')
        self.assertIn('no python with numpy+scipy', record['fallback_reason'])
        self.assertEqual(self.calls[-1][1][0], convert_job.sys.executable)

    def test_both_failing_never_raises(self):
        self.fail_on = {'segment_first_prep.py', 'split_pieces.py'}
        record = convert_job.split_step(self.out, 'body_armor', 'segment-first')
        self.assertEqual(record['status'], 'failed')
        self.assertIn('fallback_reason', record)
        self.assertIn('split_pieces.py exited 1', record['error'])

    def test_per_face_and_none(self):
        record = convert_job.split_step(self.out, 'body_armor', 'per-face')
        self.assertEqual((record['status'], record['method']), ('done', 'per-face'))
        self.assertEqual([n for n, _ in self.calls], ['split_pieces.py'])
        self.calls.clear()
        self.assertEqual(convert_job.split_step(self.out, 'body_armor', 'none')['status'], 'skipped')
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
