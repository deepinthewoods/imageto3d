"""Three-view modules: the views_omitted.json marker, the camera filter of the visibility weighting and convert_job."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np

import convert_job
import face_labels
import views_omitted


def write_transforms(path):
    frames = []
    for i, name in enumerate(('front', 'front_three_quarter', 'back_three_quarter', 'back')):
        matrix = np.eye(4)
        matrix[:3, 3] = (i, 0, 0)
        frames.append({'file_path': name + '.png', 'name': name, 'transform_matrix': matrix.tolist()})
    path.write_text(json.dumps({'camera_angle_x': 0.35, 'frames': frames}))
    return path


REGIONS = {'base_scene_bbox': {'min': [-1, -1, 0], 'max': [1, 1, 2]}}


class MarkerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def write(self, data):
        (self.folder / views_omitted.MARKER).write_text(data if isinstance(data, str) else json.dumps(data))

    def test_no_marker_means_four_views(self):
        self.assertIsNone(views_omitted.load(self.folder))
        self.assertEqual(views_omitted.omitted_views(self.folder), [])

    def test_valid_marker(self):
        self.write({'omitted': ['back'], 'reason': 'refused', 'date': '2026-10-24'})
        marker = views_omitted.load(self.folder)
        self.assertEqual((marker['omitted'], marker['reason'], marker['date']), (['back'], 'refused', '2026-10-24'))
        self.assertEqual(len(marker['sha256']), 64)
        self.write({'omitted': ['back.png']})                       # a file name is tolerated
        self.assertEqual(views_omitted.omitted_views(self.folder), ['back'])

    def test_invalid_markers(self):
        for data, message in (({'omitted': ['front']}, 'front is always required'),
                              ({'omitted': ['back', 'front_three_quarter']}, 'omit exactly one'),
                              ({'omitted': ['back', 'front_three_quarter', 'back_three_quarter']}, 'omit exactly one'),
                              ({'omitted': []}, 'omit exactly one'),
                              ({'omitted': ['up']}, 'unknown view'),
                              ({'omitted': [3]}, 'view names'),
                              ({'omitted': 'back'}, 'needs'),
                              ([], 'needs'), ('{nope', 'not valid JSON')):
            self.write(data)
            with self.assertRaisesRegex(ValueError, message):
                views_omitted.load(self.folder)

    def test_two_view_marker(self):
        self.write({'omitted': ['back_three_quarter', 'front_three_quarter'], 'reason': 'gnome: front + back only'})
        self.assertEqual(sorted(views_omitted.omitted_views(self.folder)), ['back_three_quarter', 'front_three_quarter'])
        (self.folder / 'front_three_quarter.png').write_bytes(b'x')
        with self.assertRaisesRegex(ValueError, 'exists'):
            views_omitted.load(self.folder)

    def test_marker_naming_an_existing_view_is_rejected(self):
        (self.folder / 'back.png').write_bytes(b'x')
        self.write({'omitted': ['back']})
        with self.assertRaisesRegex(ValueError, 'exists'):
            views_omitted.load(self.folder)


class CameraFilterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.transforms = write_transforms(Path(self.temp.name) / 'transforms.json')

    def names(self, **kw):
        return [c['name'] for c in face_labels.load_cameras(self.transforms, REGIONS, **kw)]

    def test_default_keeps_all_four_cameras(self):
        self.assertEqual(self.names(), ['front', 'front_three_quarter', 'back_three_quarter', 'back'])
        self.assertEqual(self.names(omit=()), self.names())
        self.assertEqual(len(face_labels.load_cameras(self.transforms, REGIONS, None)), 4)

    def test_omit_drops_only_that_camera(self):
        self.assertEqual(self.names(omit=['back']), ['front', 'front_three_quarter', 'back_three_quarter'])
        self.assertEqual(self.names(omit=['front_three_quarter.png']), ['front', 'back_three_quarter', 'back'])


class ConvertJobTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_job(self, omitted, in_record=False):
        job = self.root / 'processing' / 's--j'
        module = job / 'human_male' / 'outfit'
        module.mkdir(parents=True)
        entry = {'race_gender': 'human_male', 'module': 'outfit'}
        if omitted and in_record:
            entry['views_omitted'] = {'omitted': [omitted], 'reason': 'refused'}
        elif omitted:
            (module / 'views_omitted.json').write_text(json.dumps({'omitted': [omitted], 'reason': 'refused'}))
        (job / '.tripo-job.json').write_text(json.dumps({'set': 's', 'job_id': 'j', 'modules': [entry]}))
        out = self.root / 'models/s/j/human_male/outfit'
        commands = []

        def fake_run(cmd, **kw):
            commands.append([str(c) for c in cmd])
            joined = ' '.join(commands[-1])
            record = out / 'studio_submission.json'
            if ' submit ' in joined:
                out.mkdir(parents=True, exist_ok=True)
                record.write_text(json.dumps({'status': 'submitted', 'project_id': 'P'}))
            elif ' collect ' in joined:
                (out / 'x.glb').write_bytes(b'glb')
                data = json.loads(record.read_text())
                data.update(status='exported', exports={'GLB': {'path': str(out / 'x.glb')}})
                record.write_text(json.dumps(data))
            else:  # blender reduction
                (out / 'reduced').mkdir(parents=True, exist_ok=True)
                (out / 'reduced/manifest.json').write_text(json.dumps({
                    'interior': {'interior_area_share': 0, 'threshold': 0.15},
                    'levels': {'0.4': {'triangles': 5, 'mesh_checks_pass': True, 'material_review_required': False}}}))
            return mock.Mock(returncode=0)
        with mock.patch.object(convert_job, 'ROOT', self.root), mock.patch.object(convert_job.subprocess, 'run', fake_run):
            convert_job.main(['s--j'])
        return commands, json.loads((job / '.tripo-job.json').read_text())

    def test_four_view_job_reduces_without_omit_views(self):
        commands, record = self.run_job(None)
        reduce = next(c for c in commands if 'reduce_model.py' in ' '.join(c))
        self.assertNotIn('--omit-views', reduce)
        self.assertNotIn('views_omitted', record['module_conversion']['human_male/outfit'])

    def test_marker_is_passed_to_the_reduction_and_recorded(self):
        commands, record = self.run_job('back')
        reduce = next(c for c in commands if 'reduce_model.py' in ' '.join(c))
        self.assertEqual(reduce[reduce.index('--omit-views') + 1], 'back')
        self.assertEqual(record['module_conversion']['human_male/outfit']['views_omitted'], ['back'])

    def test_job_record_entry_is_used_when_the_marker_file_is_gone(self):
        commands, _ = self.run_job('front_three_quarter', in_record=True)
        reduce = next(c for c in commands if 'reduce_model.py' in ' '.join(c))
        self.assertEqual(reduce[reduce.index('--omit-views') + 1], 'front_three_quarter')

    def test_segment_first_prep_gets_the_manifest_omission(self):
        out = self.root / 'm'
        (out / 'reduced').mkdir(parents=True)
        (out / 'reduced/manifest.json').write_text(json.dumps({'cameras': 'c.json', 'omit_views': ['back']}))
        commands = []
        with mock.patch.object(convert_job, 'run_logged', lambda cmd, log, label: commands.append([str(c) for c in cmd])):
            with self.assertRaises(RuntimeError):                   # prep.npz is not written by the fake
                convert_job.segment_first_split(out, 'body_armor')
        prep = commands[0]
        self.assertEqual(prep[prep.index('--omit-views') + 1], 'back')


class KneeCopsJobTest(unittest.TestCase):
    """convert_job: the `knee_cops` flag of a body_armor module (key only) reaches the reduction and the segment-first prep."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.job = self.root / 'processing' / 's--j'

    def write(self, rel, data):
        path = self.job / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data) if not isinstance(data, str) else data)

    def test_detection_reads_the_key_only(self):
        self.write('human_male/.concept_notes/body_armor_prompts.json', {'module': 'body_armor', 'knee_cops': True})
        self.assertTrue(convert_job.job_is_knee_cops(self.job, 'human_male', 'body_armor'))
        self.assertFalse(convert_job.job_is_knee_cops(self.job, 'human_male', 'outfit'))            # body_armor only
        self.assertFalse(convert_job.job_is_knee_cops(self.job, 'orc_male', 'body_armor'))          # another race's notes
        self.write('human_male/body_armor/prompts.json', {'knee_cops': False, 'prompt': 'plate with knee cops'})
        self.write('human_male/.concept_notes/body_armor_prompts.json', {'prompt': 'no knee cops, knee_cops absent'})
        self.assertFalse(convert_job.job_is_knee_cops(self.job, 'human_male', 'body_armor'))        # words do not switch it on

    def test_detection_from_the_job_record(self):
        self.write('.tripo-job.json', {'modules': [{'race_gender': 'human_male', 'module': 'body_armor', 'knee_cops': True},
                                                   {'race_gender': 'orc_male', 'module': 'body_armor'}]})
        self.assertTrue(convert_job.job_is_knee_cops(self.job, 'human_male', 'body_armor'))
        self.assertFalse(convert_job.job_is_knee_cops(self.job, 'orc_male', 'body_armor'))
        self.write('.tripo-job.json', {'knee_cops': True, 'modules': []})
        self.assertTrue(convert_job.job_is_knee_cops(self.job, 'orc_male', 'body_armor'))

    def run_job(self, flagged, argv=()):
        entry = {'race_gender': 'human_male', 'module': 'body_armor'}
        self.write('.tripo-job.json', {'set': 's', 'job_id': 'j', 'modules': [entry]})
        (self.job / 'human_male' / 'body_armor').mkdir(parents=True, exist_ok=True)
        if flagged:
            self.write('human_male/body_armor/prompts.json', {'knee_cops': True})
        out = self.root / 'models/s/j/human_male/body_armor'
        commands = []

        def fake_run(cmd, **kw):
            commands.append([str(c) for c in cmd])
            joined = ' '.join(commands[-1])
            record = out / 'studio_submission.json'
            if ' submit ' in joined:
                out.mkdir(parents=True, exist_ok=True)
                record.write_text(json.dumps({'status': 'submitted', 'project_id': 'P'}))
            elif ' collect ' in joined:
                (out / 'x.glb').write_bytes(b'glb')
                data = json.loads(record.read_text())
                data.update(status='exported', exports={'GLB': {'path': str(out / 'x.glb')}})
                record.write_text(json.dumps(data))
            else:  # blender reduction
                (out / 'reduced').mkdir(parents=True, exist_ok=True)
                manifest = {'interior': {'interior_area_share': 0, 'threshold': 0.15},
                            'levels': {'0.4': {'triangles': 5, 'mesh_checks_pass': True, 'material_review_required': False}}}
                if '--knee-cops' in commands[-1]:
                    manifest['knee_cops'] = {'enabled': True, 'band_fraction': 0.33, 'above_fraction': 0.15, 'prior': 'parts',
                                             'sides': {'l': {'status': 'applied', 'faces_moved': 7}, 'r': {'status': 'unchanged', 'reason': 'no knee ring'}}}
                (out / 'reduced/manifest.json').write_text(json.dumps(manifest))
            return mock.Mock(returncode=0)
        with mock.patch.object(convert_job, 'ROOT', self.root), mock.patch.object(convert_job.subprocess, 'run', fake_run):
            convert_job.main(['s--j', *argv])
        return commands, json.loads((self.job / '.tripo-job.json').read_text())

    def test_flag_goes_to_the_reduction_and_the_record(self):
        commands, record = self.run_job(True)
        reduce = next(c for c in commands if 'reduce_model.py' in ' '.join(c))
        self.assertIn('--knee-cops', reduce)
        entry = record['module_conversion']['human_male/body_armor']['knee_cops']
        self.assertEqual(entry, {'enabled': True, 'band_fraction': 0.33, 'above_fraction': 0.15, 'prior': 'parts',
                                 'sides': {'l': 7, 'r': 'no knee ring'}})

    def test_without_the_flag_nothing_changes(self):
        commands, record = self.run_job(False)
        reduce = next(c for c in commands if 'reduce_model.py' in ' '.join(c))
        self.assertNotIn('--knee-cops', reduce)
        self.assertNotIn('knee_cops', record['module_conversion']['human_male/body_armor'])

    def test_command_line_flag_forces_it_for_body_armor(self):
        commands, _ = self.run_job(False, ['--knee-cops'])
        reduce = next(c for c in commands if 'reduce_model.py' in ' '.join(c))
        self.assertIn('--knee-cops', reduce)

    def test_segment_first_prep_gets_the_manifest_flag_and_band(self):
        out = self.root / 'm'
        (out / 'reduced').mkdir(parents=True)
        (out / 'reduced/manifest.json').write_text(json.dumps({'knee_cops': {'enabled': True, 'band_fraction': 0.4, 'above_fraction': 0.2}}))
        commands = []
        with mock.patch.object(convert_job, 'run_logged', lambda cmd, log, label: commands.append([str(c) for c in cmd])):
            with self.assertRaises(RuntimeError):
                convert_job.segment_first_split(out, 'body_armor')
        prep = commands[0]
        self.assertIn('--knee-cops', prep)
        self.assertEqual(prep[prep.index('--knee-cop-band') + 1], '0.4')
        self.assertEqual(prep[prep.index('--knee-cop-above') + 1], '0.2')

    def test_segment_first_prep_without_the_flag_is_unchanged(self):
        out = self.root / 'm'
        (out / 'reduced').mkdir(parents=True)
        (out / 'reduced/manifest.json').write_text(json.dumps({'cameras': 'c.json'}))
        commands = []
        with mock.patch.object(convert_job, 'run_logged', lambda cmd, log, label: commands.append([str(c) for c in cmd])):
            with self.assertRaises(RuntimeError):
                convert_job.segment_first_split(out, 'body_armor')
        self.assertNotIn('--knee-cops', commands[0])


if __name__ == '__main__':
    unittest.main()
