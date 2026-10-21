import importlib.util
import json
import struct
import tempfile
import unittest
from pathlib import Path
from PIL import Image

SPEC = importlib.util.spec_from_file_location('armor_queue', Path(__file__).parents[1] / 'queue_service.py')
q = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(q)

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = {'root': str(self.root), 'settle_seconds': 0, 'assetfurnace': '/unused'}
        self.set = self.root / 'approved' / 'set_one'
        self.base = self.root / 'base_views' / 'human_male'
        self.base.mkdir(parents=True)
        self.doc = {'mesh_scale': 1.0, 'camera_angle_x': 0.349, 'frames': [
            {'file_path': v + '.png', 'name': v, 'transform_matrix': [[1,0,0,0],[0,1,0,-3],[0,0,1,0],[0,0,0,1]]}
            for v in q.VIEWS]}
        (self.base / 'transforms.json').write_text(json.dumps(self.doc, indent=3))
        for v in q.VIEWS:
            im = Image.new('RGBA', (16,16), (0,0,0,0))
            im.paste((100,120,130,255), (4,4,12,12))
            im.save(self.base / (v+'.png'))
            for m in q.MODULES:
                dest = self.set / 'human_male' / m
                dest.mkdir(parents=True, exist_ok=True)
                im.save(dest / (v+'.png'))

    def worker(self, runner=None):
        return q.Worker(self.cfg, runner=runner or self.fake_run, readiness=lambda: None)

    def fake_run(self, inputs, output, log):
        output.write_bytes(struct.pack('<4sII', b'glTF', 2, 12))
        q.write_json(output.with_suffix('.provenance.json'), {'output_sha256': q.digest(output)})
        log.write_text('fake backend used by tests only\n')

    def test_exact_four_views_and_unmodified_transform_snapshot(self):
        jobs = q.validate_set(self.root, self.set)
        self.assertEqual(len(jobs), 2)
        inputs = self.root / 'snapshot'
        q.snapshot(jobs[0], inputs)
        self.assertEqual((inputs/'transforms.json').read_bytes(), (self.base/'transforms.json').read_bytes())
        self.assertEqual(len(list(inputs.glob('*.png'))), 4)

    def test_incomplete_set_stays_approved(self):
        (self.set/'human_male/body_armor/back.png').unlink()
        self.worker().tick()
        self.assertTrue(self.set.is_dir())
        status = json.loads((self.root/'.queue/status.json').read_text())
        self.assertIn('back.png', status['approved']['set_one']['reason'])

    def test_missing_module_blocks_whole_set(self):
        import shutil
        shutil.rmtree(self.set/'human_male/body_armor')
        with self.assertRaises(ValueError): q.validate_set(self.root, self.set)

    def test_wrong_size_and_invalid_matrix_rejected(self):
        p = self.set/'human_male/body_armor/front.png'
        Image.new('RGBA',(8,8)).save(p)
        with self.assertRaises(ValueError): q.validate_set(self.root,self.set)
        p.write_bytes((self.base/'front.png').read_bytes())
        self.doc['frames'][0]['transform_matrix'][0][0] = float('nan')
        (self.base/'transforms.json').write_text(json.dumps(self.doc))
        with self.assertRaises(ValueError): q.validate_set(self.root,self.set)

    def test_unsafe_frame_path_rejected(self):
        self.doc['frames'][0]['file_path'] = '../front.png'
        (self.base/'transforms.json').write_text(json.dumps(self.doc))
        with self.assertRaises(ValueError): q.validate_set(self.root,self.set)

    def test_success_moves_set_and_does_not_repeat(self):
        calls=[]
        def run(*args): calls.append(args); self.fake_run(*args)
        worker=self.worker(run)
        worker.tick(); worker.tick(); worker.tick()
        self.assertEqual(len(calls),2)
        self.assertFalse(self.set.exists())
        self.assertEqual(len(list((self.root/'completed').iterdir())),1)
        self.assertEqual(len(list((self.root/'models').rglob('model.glb'))),2)

    def test_failure_preserves_sources_and_log(self):
        def run(inputs,output,log):
            log.write_text('backend failed')
            raise RuntimeError('GPU failure')
        worker=self.worker(run)
        worker.tick(); worker.tick()
        failed=list((self.root/'failed').iterdir())
        self.assertEqual(len(failed),1)
        self.assertTrue((failed[0]/'human_male/body_armor/front.png').is_file())
        self.assertEqual(len(list((self.root/'models').rglob('generation.log'))),1)

    def test_restart_skips_finished_module(self):
        count=[0]
        def run(*args):
            count[0]+=1
            if count[0]==2: raise KeyboardInterrupt()
            self.fake_run(*args)
        worker=self.worker(run)
        worker.tick()
        with self.assertRaises(KeyboardInterrupt): worker.tick()
        calls=[]
        def resumed(*args): calls.append(args); self.fake_run(*args)
        self.worker(resumed).tick()
        self.assertEqual(len(calls),1)
        self.assertEqual(len(list((self.root/'completed').iterdir())),1)

    def test_backend_unavailable_does_not_claim(self):
        worker=q.Worker(self.cfg,runner=self.fake_run,readiness=lambda: (_ for _ in ()).throw(RuntimeError('missing weights')))
        worker.tick(); worker.tick()
        self.assertTrue(self.set.exists())
        self.assertEqual(list((self.root/'processing').iterdir()),[])

    def test_provenance_hash_detects_corrupted_output(self):
        output=self.root/'model.glb'
        self.fake_run(None,output,self.root/'run.log')
        self.assertTrue(q.finished(output))
        output.write_bytes(b'broken')
        self.assertFalse(q.finished(output))

if __name__ == '__main__': unittest.main()

class BackendTests(unittest.TestCase):
    def test_real_backend_command_uses_all_views_without_matting_or_camera_override(self):
        from unittest.mock import patch
        import sys
        import time
        config = json.loads((Path(__file__).parents[1]/'config.json').read_text())
        if not Path(config['assetfurnace']).exists():
            self.skipTest('local AssetFurnace installation unavailable')
        backend = q.Backend(config)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root/'inputs'
            inputs.mkdir()
            (inputs/'transforms.json').write_text(json.dumps({'camera_angle_x':0.349,'frames':[{}]*4}))
            (inputs/'input_hashes.json').write_text('{}')
            (inputs/'front.png').write_bytes(b'input hash test')
            output = root/'model.glb'
            class Process:
                def __init__(self, command, **kwargs):
                    self.command = command
                    self.kwargs = kwargs
                    output.write_bytes(struct.pack('<4sII',b'glTF',2,12))
                def wait(self, **kwargs): return 0
            with patch.object(q.subprocess, 'Popen', side_effect=Process) as spawn:
                backend.run(inputs,output,root/'generation.log')
            command = spawn.call_args.args[0]
            self.assertEqual(command[command.index('--views')+1],str(inputs))
            self.assertEqual(command[command.index('--pixal3d-weights')+1],'mv')
            for absent in ('--sv-image','--num-views','--mesh-scale','--fov','--birefnet'):
                self.assertNotIn(absent,command)
            env=spawn.call_args.kwargs['env']
            self.assertEqual(env['HIP_VISIBLE_DEVICES'],'1')
            self.assertTrue(env['LD_LIBRARY_PATH'].startswith(config['assetfurnace']+'/vendor/rocm-local/sdk/lib'))
            self.assertTrue(q.finished(output))
            record=json.loads(output.with_suffix('.provenance.json').read_text())
            self.assertEqual(record['parameters']['views'], list(q.VIEWS))
            self.assertIn('license',record)
