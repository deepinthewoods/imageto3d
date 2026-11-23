"""Offline tests for pixal3d_backend.py and the convert_job.py pixal3d dispatch (no GPU, no WSL: the generator is faked)."""
import json
import struct
import sys
import tempfile
import unittest
import zlib
from array import array
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[0] / 'tripo_pipeline'))
import pixal3d_backend as pb  # noqa: E402
import convert_job  # noqa: E402

VIEWS = ('front', 'front_three_quarter', 'back', 'back_three_quarter')


def png(path, size=8, colour_type=6):
    raw = b''.join(b'\0' + b'\x10\x20\x30\xff' * size for _ in range(size))
    chunk = lambda kind, data: struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    Path(path).write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, colour_type, 0, 0, 0))
                           + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def make_job(root, omit=()):
    """<root>/job/{.base_views/dwarf_male/transforms.json, dwarf_male/body_armor/<views>.png}; returns the views dir."""
    views = Path(root) / 'job' / 'dwarf_male' / 'body_armor'
    views.mkdir(parents=True)
    snap = Path(root) / 'job' / '.base_views' / 'dwarf_male'
    snap.mkdir(parents=True)
    frames = [{'name': n, 'file_path': f'{n}.png', 'transform_matrix': [[1, 0, 0, i], [0, 1, 0, 0], [0, 0, 1, 3], [0, 0, 0, 1]]}
              for i, n in enumerate(reversed(VIEWS))]   # deliberately not front-first
    (snap / 'transforms.json').write_text(json.dumps({'camera_angle_x': 0.349, 'mesh_scale': 1.0, 'frames': frames}))
    for name in VIEWS:
        if name not in omit:
            png(views / f'{name}.png')
    return views


def make_glb(path, positions, normals, faces):
    pos = array('f', [c for p in positions for c in p]).tobytes()
    nor = array('f', [c for n in normals for c in n]).tobytes()
    idx = array('I', [i for f in faces for i in f]).tobytes()
    binary = pos + nor + idx
    gltf = {'asset': {'version': '2.0'}, 'scenes': [{'nodes': [0]}], 'nodes': [{'mesh': 0}],
            'meshes': [{'primitives': [{'attributes': {'POSITION': 0, 'NORMAL': 1}, 'indices': 2}]}],
            'buffers': [{'byteLength': len(binary)}],
            'bufferViews': [{'buffer': 0, 'byteOffset': 0, 'byteLength': len(pos)},
                            {'buffer': 0, 'byteOffset': len(pos), 'byteLength': len(nor)},
                            {'buffer': 0, 'byteOffset': len(pos) + len(nor), 'byteLength': len(idx)}],
            'accessors': [{'bufferView': 0, 'componentType': 5126, 'count': len(positions), 'type': 'VEC3',
                           'min': [min(p[i] for p in positions) for i in range(3)], 'max': [max(p[i] for p in positions) for i in range(3)]},
                          {'bufferView': 1, 'componentType': 5126, 'count': len(normals), 'type': 'VEC3'},
                          {'bufferView': 2, 'componentType': 5125, 'count': len(faces) * 3, 'type': 'SCALAR'}]}
    pb.write_glb(path, gltf, bytearray(binary))


class Inputs(unittest.TestCase):
    def test_stage_orders_front_first_and_uses_the_job_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            views = make_job(tmp)
            names, note = pb.stage(views, Path(tmp) / 'out')
            self.assertEqual(names, list(VIEWS))
            self.assertIn('job snapshot', note)
            t = json.loads((Path(tmp) / 'out' / pb.INPUT_DIR / 'transforms.json').read_text())
            self.assertEqual([f['file_path'] for f in t['frames']], [f'{n}.png' for n in VIEWS])
            self.assertTrue(all((Path(tmp) / 'out' / pb.INPUT_DIR / f'{n}.png').is_file() for n in VIEWS))

    def test_three_view_module_passes_only_existing_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            views = make_job(tmp, omit=('back',))
            names, _ = pb.stage(views, Path(tmp) / 'out')
            self.assertEqual(names, ['front', 'front_three_quarter', 'back_three_quarter'])

    def test_missing_front_and_rgb_views_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            views = make_job(tmp, omit=('front',))
            with self.assertRaises(pb.Stop):
                pb.stage(views, Path(tmp) / 'out')
        with tempfile.TemporaryDirectory() as tmp:
            views = make_job(tmp)
            png(views / 'back.png', colour_type=2)
            with self.assertRaises(pb.Stop):
                pb.stage(views, Path(tmp) / 'out')


class Glb(unittest.TestCase):
    def test_rotate_y180_negates_x_and_z_and_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'm.glb'
            make_glb(path, [(1, 2, 3), (-4, 5, 0.5), (0, 0, -1)], [(0, 0, 1)] * 3, [(0, 1, 2)])
            gltf, binary = pb.rotate_y180(*pb.read_glb(path))
            pb.write_glb(path, gltf, binary)
            gltf, binary = pb.read_glb(path)
            pos = array('f'); pos.frombytes(bytes(binary[:36]))
            nor = array('f'); nor.frombytes(bytes(binary[36:72]))
            self.assertEqual(list(pos), [-1, 2, -3, 4, 5, -0.5, 0, 0, 1])
            self.assertEqual(list(nor)[2], -1)
            self.assertEqual(gltf['accessors'][0]['min'], [-1, 0, -3])
            self.assertEqual(gltf['accessors'][0]['max'], [4, 5, 1])
            self.assertEqual(pb.face_count(gltf), 1)

    def test_node_transform_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'm.glb'
            make_glb(path, [(1, 2, 3)] * 3, [(0, 0, 1)] * 3, [(0, 1, 2)])
            gltf, binary = pb.read_glb(path)
            gltf['nodes'][0]['rotation'] = [0, 1, 0, 0]
            with self.assertRaises(pb.Stop):
                pb.rotate_y180(gltf, binary)


class Flow(unittest.TestCase):
    def fake_generation(self, faces=30000):
        def run(config, out_dir, seed, log):
            n = faces
            make_glb(Path(out_dir) / 'glb_pixal3d_raw.glb', [(0, 0, 0)] * 3, [(0, 0, 1)] * 3, [(0, 1, 2)] * n)
            return 12.0
        return run

    def test_submit_then_collect_writes_the_tripo_record_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            views, out = make_job(tmp), Path(tmp) / 'out'
            pb.submit(views, out, seed_offset=1)
            record = json.loads((out / pb.RECORD).read_text())
            self.assertEqual((record['backend'], record['status'], record['seed']), ('pixal3d', 'submitted', pb.load_config()['seed'] + 1))
            with self.assertRaises(pb.Stop):
                pb.submit(views, out)                      # never a second submission over a record
            with mock.patch.object(pb, 'run_generation', self.fake_generation()):
                pb.collect(out)
            record = json.loads((out / pb.RECORD).read_text())
            self.assertEqual(record['status'], 'exported')
            self.assertTrue(Path(record['exports']['GLB']['path']).is_file())
            self.assertEqual(Path(record['exports']['GLB']['path']).name, 'glb_pixal3d.glb')
            self.assertFalse((out / 'glb_pixal3d_raw.glb').exists())

    def test_tiny_mesh_leaves_the_module_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            views, out = make_job(tmp), Path(tmp) / 'out'
            pb.submit(views, out)
            with mock.patch.object(pb, 'run_generation', self.fake_generation(faces=10)), self.assertRaises(pb.Stop):
                pb.collect(out)
            self.assertEqual(json.loads((out / pb.RECORD).read_text())['status'], 'submitted')

    def test_generation_script_quotes_paths_and_cleans_up(self):
        script = pb.generation_script(pb.load_config(), "/mnt/f/a b/in", "/mnt/f/a b/x.glb", 43, 'tag')
        self.assertIn("'/mnt/f/a b/in'", script)
        self.assertIn('--seed 43', script)
        self.assertIn('rm -rf', script.splitlines()[-1])

    def test_convert_job_dispatch(self):
        self.assertIn('pixal3d', convert_job.BACKENDS)
        submit = convert_job.submit_command('pixal3d', Path('v'), Path('o'), options={'seed_offset': 2})
        self.assertEqual(submit[-5:], ['submit', 'v', 'o', '--seed-offset', '2'])
        self.assertEqual(submit[-2:], ['--seed-offset', '2'])
        self.assertEqual(convert_job.collect_command('pixal3d', Path('o'))[-2:], ['collect', 'o'])

    def test_stripped_double_skin_is_not_regenerated(self):
        stripped = {'interior': {'interior_area_share': 0.53, 'threshold': 0.15, 'stripped': True,
                                 'after_strip': {'interior_area_share': 0.026}}}
        self.assertIsNotNone(convert_job.regenerate_reason(stripped))           # the Tripo rule would regenerate it
        self.assertIsNone(convert_job.pixal3d_regenerate_reason(stripped))
        stripped['interior']['after_strip']['interior_area_share'] = 0.3
        self.assertEqual(convert_job.pixal3d_regenerate_reason(stripped)['slug'], 'interior_shell')
        unstripped = {'interior': {'interior_area_share': 0.4, 'threshold': 0.15, 'stripped': False}}
        self.assertIsNotNone(convert_job.pixal3d_regenerate_reason(unstripped))


if __name__ == '__main__':
    unittest.main()
