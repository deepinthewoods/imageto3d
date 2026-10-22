import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from job_state import Job, BASE, model_id, verify_zip
from workflow import observe

ID = 'e1690fdd-98aa-4581-a015-2a2a58a787e4'


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root/'input.png'
        self.image.write_bytes(b'input image bytes')
        self.job = Job.create(self.root/'jobs', self.image)

    def test_same_image_reuses_job_even_if_renamed(self):
        self.job.bind(BASE+'/'+ID)
        copy = self.root/'renamed.png'
        copy.write_bytes(self.image.read_bytes())
        same = Job.create(self.root/'jobs', copy)
        self.assertEqual(same.path, self.job.path)
        self.assertEqual(same.data['model_id'], ID)

    def test_crash_after_intent_does_not_repeat_click(self):
        self.job.intent('geometry_confirm')
        recovered = Job(self.job.path)
        with self.assertRaises(RuntimeError):
            recovered.intent('geometry_confirm')
        recovered.observed('material_ready')
        with self.assertRaises(RuntimeError):
            recovered.intent('geometry_confirm')
        recovered.intent('material_generate')

    def test_bind_drops_query_and_disallows_different_model(self):
        self.job.bind(BASE+'/'+ID+'?tracking=unused')
        self.assertEqual(self.job.data['model_url'], BASE+'/'+ID)
        with self.assertRaises(ValueError):
            self.job.bind(BASE+'/11111111-1111-1111-1111-111111111111')
        self.assertIsNone(model_id(BASE+'/tab/mine'))
        self.assertIsNone(model_id('https://example.com/workspace/rodin/'+ID))

    def test_input_mutation_is_detected(self):
        self.image.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.job.verify_input()

    def test_zip_requires_fbx_and_textures(self):
        archive = self.root/'out.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('base.obj', 'mesh')
        with self.assertRaises(ValueError):
            verify_zip(archive)
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('base.fbx', 'mesh')
            z.writestr('texture.png', 'pixels')
        self.assertEqual(len(verify_zip(archive)['files']), 2)

    def test_history_and_state_persist(self):
        self.job.bind(BASE+'/'+ID)
        saved = json.loads(self.job.path.read_text())
        events = [json.loads(s) for s in self.job.path.with_name('events.jsonl').read_text().splitlines()]
        self.assertEqual(saved['model_id'], ID)
        self.assertEqual(events[-1]['event'], 'model_linked')

    def test_resume_stage_detection(self):
        class Node:
            def __init__(self, text): self.text = text
        class Driver:
            current_url = BASE+'/'+ID
        cases = [
            ('Redo\nConfirm', 'Unlock: Geometry', 'Unlock after Material confirmed', 'geometry_preview'),
            ('Generating...\n40%', 'Unlock: Geometry', 'Unlock after Material confirmed', 'geometry_processing'),
            ('Geometry', 'De-light\nGenerate', 'Unlock after Material confirmed', 'material_ready'),
            ('Geometry', 'Redo\nConfirm', 'Unlock after Material confirmed', 'material_preview'),
            ('Geometry', 'Generating...\n60%', 'Unlock after Material confirmed', 'material_processing'),
            ('Geometry', 'Options\nDe-light', 'Download', 'export_ready'),
        ]
        for g,m,p,want in cases:
            with self.subTest(want=want), patch('workflow.scope', side_effect=[Node(g),Node(m),Node(p)]), patch('workflow.visible', return_value=[]), patch('workflow.exact', side_effect=lambda node,label: label if label in node.text.splitlines() else None):
                self.assertEqual(observe(Driver())['stage'], want)


if __name__ == '__main__':
    unittest.main()
