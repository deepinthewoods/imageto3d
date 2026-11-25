"""Tests of tools/layers/build.py (fingerprint, required slots, validation / damaged-build recovery, locked publication with concurrent processes,
inputs changed during a build, invalid races, the CLI's JSON on every failure path, in-process cache reset, parity) with a stub in place of layers.py,
plus a few real-code checks run in subprocesses (the fingerprint dependency closure, the role tie-break under several PYTHONHASHSEEDs, pickle-free loads).

    python tools/layers/test_build.py          (or: python -m unittest tools/layers/test_build.py -v)

No real model, game body or GPU is needed: `layers` is replaced by a stub that writes small but complete GLBs.
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build  # noqa: E402

ROLE_RGB = {'skin': (0.86, 0.67, 0.53), 'primary': (0.098, 0.824, 0.157), 'secondary': (0.118, 0.255, 0.922), 'accent': (0.882, 0.098, 0.863)}


def write_glb(path, faces=10, morph=True, names=None, rgb=None, tris=None, trunc_bin=False):
    """A complete GLB the way shell_slots.glb_bytes writes it (flat, one primitive per material, POSITION + NORMAL, morph target 'fill'); `names` one
    'role:prov' per face (default primary:tripo), `rgb` {role: colour}; `tris` (faces, 3, 3) positions (default a deterministic strip)."""
    if tris is None:
        base = np.arange(faces, dtype=np.float32)[:, None, None] * 0.1
        tris = base + np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], np.float32)[None] * 0.05
    tris = np.asarray(tris, np.float32)
    faces = len(tris)
    names = list(names or ['primary:tripo'] * faces)
    blob, views, accs, prims, mats = bytearray(), [], [], [], []

    def add(arr):
        arr = np.asarray(arr, np.float32)
        off = len(blob)
        blob.extend(arr.tobytes())
        blob.extend(b'\0' * (-len(blob) % 4))
        views.append({'buffer': 0, 'byteOffset': off, 'byteLength': arr.nbytes, 'target': 34962})
        accs.append({'bufferView': len(views) - 1, 'componentType': 5126, 'count': len(arr), 'type': 'VEC3'})
        return len(accs) - 1
    for nm in sorted(set(names)):
        sel = np.array([n == nm for n in names])
        T0 = tris[sel].reshape(-1, 3)
        prim = {'attributes': {'POSITION': add(T0), 'NORMAL': add(np.tile([0, 1, 0], (len(T0), 1)))}, 'material': len(mats)}
        if morph:
            prim['targets'] = [{'POSITION': add(np.full_like(T0, 0.01)), 'NORMAL': add(np.zeros_like(T0))}]
        prims.append(prim)
        role = nm.split(':')[0]
        col = (rgb or {}).get(role) or ROLE_RGB.get(role, (.5, .5, .5))
        mats.append({'name': nm, 'doubleSided': True, 'pbrMetallicRoughness': {'baseColorFactor': [*col, 1.], 'metallicFactor': 0., 'roughnessFactor': .9}})
    mesh = {'name': 'shell', 'primitives': prims}
    if morph:
        mesh['weights'] = [0.]
        mesh['extras'] = {'targetNames': ['fill']}
    js = {'asset': {'version': '2.0'}, 'scene': 0, 'scenes': [{'nodes': [0]}], 'nodes': [{'mesh': 0, 'name': 'shell'}], 'meshes': [mesh], 'materials': mats,
          'accessors': accs, 'bufferViews': views, 'buffers': [{'byteLength': len(blob)}]}
    text = json.dumps(js, separators=(',', ':')).encode()
    text += b' ' * (-len(text) % 4)
    body = struct.pack('<II', len(text), 0x4E4F534A) + text + struct.pack('<II', len(blob), 0x004E4942) + bytes(blob)
    data = struct.pack('<4sII', b'glTF', 2, 12 + len(body)) + body
    if trunc_bin:                                                   # cut inside the binary chunk, lengths fixed up so only the buffers are short
        data = data[:20 + len(text) + 8 + 16]
        data = data[:8] + struct.pack('<I', len(data)) + data[12:]
    Path(path).write_bytes(data)


class StubLayers:
    """Stands in for layers.py: module_layers(mod, lod, out_dir) / bare_layers(race, out_dir) with a configurable behaviour."""

    def __init__(self):
        self.calls = 0
        self.mode = 'ok'
        self.module = 'right_gauntlet_boot'
        self.expected = ['lower_arm.r', 'lower_arm.l']
        self.skipped = {}
        self.hook = None
        self.delay = 0.0
        self._LAYER_BODY = {}

    def module_layers(self, mod, lod, out_dir):
        self.calls += 1
        out = Path(out_dir)
        if self.hook:
            self.hook()
        if self.delay:
            time.sleep(self.delay)
        if self.mode == 'raise':
            (out / 'half.glb').write_bytes(b'x')
            raise RuntimeError('boom')
        slots = {}
        for k in self.expected:
            if k in self.skipped or (self.mode == 'drop_slot' and k == self.expected[-1]):
                continue
            write_glb(out / f'{k}.glb', 10, morph=self.mode != 'no_morph')
            (out / f'{k}.json').write_text('{}', encoding='utf-8')
            slots[k] = {'faces': 10, 'warnings': ['w'] if k == self.expected[0] else [], 'wall_crossings_left': [0, 1], 'boundary_edges': 0,
                        'ring_vertices': 9, 'ring_vertices_expected': 6, 'ring_original_merged': 1, 'ring_line_vertices': 4}
        return {'race': Path(mod).parts[-2], 'set': 's', 'job': 'j', 'module': self.module, 'module_path': 'x', 'lod': lod, 'slots': slots,
                'skipped': dict(self.skipped), 'expected': list(self.expected), 'load': {'source_lod': lod}}

    def bare_layers(self, race, out_dir):
        self.calls += 1
        out = Path(out_dir)
        if self.delay:
            time.sleep(self.delay)
        slots = {}
        for k in ('head', 'torso', 'lower_arm.l', 'lower_arm.r'):
            write_glb(out / f'{k}.glb', 3, morph=False, names=['skin:game'] * 3)
            (out / f'{k}.json').write_text('{}', encoding='utf-8')
            slots[k] = {'faces': 3, 'warnings': [], 'wall_crossings_left': None, 'boundary_edges': 0}
        return {'race': race, 'set': 'bare', 'module': 'bare', 'slots': slots, 'skipped': {}, 'expected': list(slots), 'load': {}}


BODY_PARTS = ('head.x', 'spine_01.x', 'forearm_stretch.l', 'hand.l', 'forearm_stretch.r', 'hand.r', 'leg_stretch.l', 'foot.l', 'leg_stretch.r', 'foot.r')


def write_body(path, legs=False, salt='', compressed=False):
    """A small game body cache npz (part / part_names like tools/preview/cache/bases/<race>.npz): with or without lower-leg faces. The fake
    human_male of these tests is LEGLESS by default (two gloves, like the stub's default result); tests that need legs pass legs=True."""
    part = np.array([0, 1, 2, 3, 4, 5] + ([6, 7, 8, 9] if legs else []), dtype=np.int64)
    with open(path, 'wb') as f:
        (np.savez_compressed if compressed else np.savez)(f, part=part, part_names=np.array(BODY_PARTS),
                                                          salt=np.array([len(salt)] + [ord(c) for c in salt], dtype=np.int32))


def make_module(root, set_name='set_a', job='job1', race='human_male', kind='right_gauntlet_boot'):
    mod = root / 'models' / set_name / job / race / kind
    (mod / 'reduced').mkdir(parents=True)
    (mod / 'fit').mkdir()
    for lod in build.LODS:
        (mod / 'reduced' / f'lod_error_{lod}.glb').write_bytes(f'glb {lod}'.encode())
    (mod / 'reduced' / 'lod_error_0p2_colors.npz').write_bytes(b'npz')
    (mod / 'reduced' / 'manifest.json').write_text('{"a": 1}', encoding='utf-8')
    (mod / 'fit' / 'align.json').write_text('{"b": 2}', encoding='utf-8')
    return mod


def worker(cfg):
    """Subprocess entry (python test_build.py --worker '<json>'): build with the stub and print the result."""
    build.BASE_CACHE = Path(cfg['bases'])
    stub = StubLayers()
    stub.delay = cfg.get('delay', 0.0)
    stub.expected = cfg.get('expected', stub.expected)
    sys.modules['layers'] = stub
    if cfg.get('bare'):
        res = build.build_bare(cfg['race'], cfg['out'], gpu=False)
    else:
        res = build.build_module(cfg['mod'], cfg['lod'], cfg['out'], gpu=False)
    print(json.dumps(res, default=float), flush=True)


def last_json(text):
    return json.loads(text.strip().splitlines()[-1])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='layers_test_'))
        self.mod = make_module(self.tmp)
        self.bases = self.tmp / 'bases'
        self.bases.mkdir()
        for n in ('human_male_rig.npz', 'gnome_female_rig.npz'):
            (self.bases / n).write_bytes(n.encode())
        for race in ('human_male', 'gnome_female'):
            write_body(self.bases / f'{race}.npz')
        self._old = (build.BASE_CACHE, build.BARE_ROOT, {k: sys.modules.get(k) for k in ('layers', 'body_fit', 'geodiv', 'fit_lab')}, os.environ.get('BODY_FIT_GPU'))
        build.BASE_CACHE = self.bases
        build.BARE_ROOT = self.tmp / 'bare_cache'
        build._BODY_STAMPS.clear()
        self.stub = StubLayers()
        sys.modules['layers'] = self.stub
        self.out = self.tmp / 'out'

    def tearDown(self):
        build.BASE_CACHE, build.BARE_ROOT = self._old[0], self._old[1]
        for k, v in self._old[2].items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        if self._old[3] is None:
            os.environ.pop('BODY_FIT_GPU', None)
        else:
            os.environ['BODY_FIT_GPU'] = self._old[3]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, lod='0p2', **kw):
        return build.build_module(self.mod, lod, self.out, gpu=False, **kw)

    def pointer(self, tag='0p2'):
        return json.loads((self.out / f'current_{tag}.json').read_text(encoding='utf-8'))

    def attempts(self):
        return [p.name for p in self.out.iterdir() if p.name.startswith('.attempt')]

    def spawn(self, **cfg):
        cfg.setdefault('bases', str(self.bases))
        cfg.setdefault('mod', str(self.mod))
        cfg.setdefault('out', str(self.out))
        cfg.setdefault('lod', '0p2')
        return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker', json.dumps(cfg)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class FingerprintTest(Base):
    def test_stable_and_sensitive(self):
        a = build.fingerprint_module(self.mod, '0p2')
        self.assertEqual(a, build.fingerprint_module(self.mod, '0p2'))
        self.assertEqual(len(a), 64)
        self.assertNotEqual(a, build.fingerprint_module(self.mod, '0p4'))                 # the lod string + its files
        (self.mod / 'fit' / 'align.json').write_text('{"b": 3}', encoding='utf-8')
        b = build.fingerprint_module(self.mod, '0p2')
        self.assertNotEqual(a, b)
        (self.bases / 'human_male_rig.npz').write_bytes(b'other')
        self.assertNotEqual(b, build.fingerprint_module(self.mod, '0p2'))                 # the body cache
        c = build.fingerprint_module(self.mod, '0p2')
        (self.mod / 'reduced' / 'lod_error_0p2_colors.npz').unlink()                       # an optional file: its absence is part of the hash
        self.assertNotEqual(c, build.fingerprint_module(self.mod, '0p2'))

    def test_bound_to_module_identity_and_kind(self):
        a = build.fingerprint_module(self.mod, '0p2')
        other_job = make_module(self.tmp / 'x', job='job2')                                 # byte-identical inputs, another pinned job
        other_kind = make_module(self.tmp / 'y', kind='outfit')
        other_set = make_module(self.tmp / 'z', set_name='set_b')
        fps = {a, build.fingerprint_module(other_job, '0p2'), build.fingerprint_module(other_kind, '0p2'), build.fingerprint_module(other_set, '0p2')}
        self.assertEqual(len(fps), 4)
        r1 = self.build()
        r2 = build.build_module(other_job, '0p2', self.out, gpu=False)                      # the same explicit out_root: not the first module's build
        self.assertEqual(r1['status'], 'ok')
        self.assertEqual(r2['status'], 'ok', r2.get('error'))
        self.assertNotEqual(r1['build_id'], r2['build_id'])

    def test_code_dependencies_are_hashed(self):
        names = [k for k, _ in build.code_parts()]
        for n in ('tools/layers/layers.py', 'tools/layers/geodiv.py', 'tools/layers/shell_slots.py', 'tools/layers/pieces.py', 'tools/layers/build.py',
                  'tools/tripo_pipeline/body_fit.py', 'tools/tripo_pipeline/align_body.py', 'tools/tripo_pipeline/fit_lab.py', 'tools/tripo_pipeline/gpu_geometry.py',
                  'tools/outfit_studio/core/transform.py'):
            self.assertIn(f'code/{n}', names)
        self.assertTrue(all(h != 'absent' for _, h in build.code_parts()), 'a code file the fingerprint covers is missing')
        old = build.CODE_DEPS, build.ROOT
        try:
            fake = self.tmp / 'repo'
            (fake / 'a').mkdir(parents=True)
            (fake / 'a' / 'transform.py').write_text('x = 1\n', encoding='utf-8')
            build.CODE_DEPS, build.ROOT = ('a/transform.py',), fake
            f1 = build.fingerprint_module(self.mod, '0p2')
            (fake / 'a' / 'transform.py').write_text('x = 2\n', encoding='utf-8')            # editing the transform changes the fingerprint
            self.assertNotEqual(f1, build.fingerprint_module(self.mod, '0p2'))
        finally:
            build.CODE_DEPS, build.ROOT = old

    def test_line_endings_ignored(self):
        p = self.tmp / 'x.py'
        p.write_bytes(b'a = 1\nb = 2\n')
        h1 = build._file_hash(p)
        p.write_bytes(b'a = 1\r\nb = 2\r\n')
        self.assertEqual(h1, build._file_hash(p))
        self.assertEqual(build._file_hash(self.tmp / 'nope.py'), 'absent')

    def test_dependency_closure(self):
        """Every repo-local file the build path imports (sys.modules, and files loaded by spec_from_file_location such as the transform and the GPU module)
        is in CODE_DEPS."""
        code = r'''
import importlib.util, json, sys
from pathlib import Path
logged = []
orig = importlib.util.spec_from_file_location
def spy(name, location=None, *a, **k):
    logged.append(str(location))
    return orig(name, location, *a, **k)
importlib.util.spec_from_file_location = spy
sys.path.insert(0, sys.argv[1])
import build, layers, geodiv, pieces, shell_slots
try:
    sys.modules['body_fit']._gpu()
except Exception:
    pass
root = Path(sys.argv[1]).parents[1].resolve()
files = {getattr(m, '__file__', None) for m in list(sys.modules.values())} | set(logged)
rel = set()
for f in files:
    if not f:
        continue
    try:
        rel.add(Path(f).resolve().relative_to(root).as_posix())
    except ValueError:
        pass
print(json.dumps(sorted(rel)))
'''
        p = subprocess.run([sys.executable, '-c', code, str(HERE)], capture_output=True, text=True, timeout=300)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        local = [f for f in last_json(p.stdout) if not f.startswith('tools/layers/test_build') and f.endswith('.py')]
        self.assertIn('tools/layers/layers.py', local)
        self.assertIn('tools/tripo_pipeline/gpu_geometry.py', local)
        self.assertIn('tools/outfit_studio/core/transform.py', local)
        missing = sorted(set(local) - set(build.CODE_DEPS))
        self.assertEqual(missing, [], f'repo-local files imported by the build but not in build.CODE_DEPS: {missing}')


class ArgumentsTest(Base):
    def test_lod_validation(self):
        for bad in ('0p3', '', None, '0.2'):
            with self.assertRaises(ValueError):
                build.build_module(self.mod, bad, self.out)
        with self.assertRaises(ValueError):
            build.build_module(self.tmp / 'no_such_module', '0p2', self.out)
        self.assertEqual(self.stub.calls, 0)

    def test_missing_lod_glb_fails_cleanly_with_previous_build(self):
        ok = self.build('0p1')
        self.assertEqual(ok['status'], 'ok', ok.get('error'))
        (self.mod / 'reduced' / 'lod_error_0p1.glb').unlink()
        r = build.build_module(self.mod, '0p1', self.out, gpu=False)
        self.assertEqual(r['status'], 'failed')
        self.assertIn('lod_error_0p1.glb', r['error'])
        self.assertEqual(r['previous_build'], ok['build_id'])
        self.assertTrue((self.out / 'last_failure.json').is_file())
        self.assertEqual(self.pointer('0p1')['build_id'], ok['build_id'])
        self.assertEqual(self.stub.calls, 1)

    def test_unknown_module_kind(self):
        mod = make_module(self.tmp / 'k', kind='shield')
        r = build.build_module(mod, '0p2', self.out, gpu=False)
        self.assertEqual(r['status'], 'failed')
        self.assertIn('unknown module', r['error'])

    def test_invalid_bare_race_writes_nothing(self):
        before = sorted(p.name for p in self.tmp.iterdir())
        for bad in ('../escape', '..', '/abs/path', 'C:/evil', 'a/b', 'a\\b', '', 'Human_Male', 'nobody', None, 5, 'human_male/../x'):
            r = build.build_bare(bad, gpu=False)                                              # default destination: BARE_ROOT/<race>
            self.assertEqual(r['status'], 'failed', bad)
            self.assertIsNone(r['out_root'], bad)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), before)                   # no bare_cache folder, nothing outside
        self.assertFalse(build.BARE_ROOT.exists())
        r = build.build_bare('nobody', self.out / 'explicit', gpu=False)                       # an explicit --out is not created either
        self.assertEqual(r['status'], 'failed')
        self.assertFalse((self.out / 'explicit').exists())
        self.assertEqual(self.stub.calls, 0)


class PublishTest(Base):
    def test_publish_current_rebuild_and_per_lod_pointers(self):
        r = self.build()
        self.assertEqual(r['status'], 'ok', r.get('error'))
        bid = r['build_id']
        self.assertTrue(bid.startswith('0p2-') and len(bid) == 4 + 12)
        self.assertEqual(sorted(r['slots']), ['lower_arm.l', 'lower_arm.r'])
        self.assertEqual(r['required'], ['lower_arm.r', 'lower_arm.l'])
        self.assertEqual(r['slots']['lower_arm.r']['glb'], f'{bid}/lower_arm.r.glb')
        self.assertTrue((self.out / r['slots']['lower_arm.r']['glb']).is_file())
        self.assertEqual(r['warnings'], ['lower_arm.r: w'])
        self.assertEqual(r['metrics']['wall_crossings_left'], 1)
        self.assertEqual(r['metrics']['ring_vertices_merged'], {'lower_arm.l': 1, 'lower_arm.r': 1})     # originals merged: never negative
        self.assertEqual(r['metrics']['ring_vertices_inserted'], {'lower_arm.l': 4, 'lower_arm.r': 4})   # inserted ring-line vertices: a separate count
        self.assertFalse(r['gpu'])
        self.assertEqual(self.pointer()['build_id'], bid)
        self.assertFalse((self.out / 'current.json').exists())
        self.assertEqual(json.loads((self.out / bid / 'build.json').read_text(encoding='utf-8'))['build_id'], bid)
        self.assertEqual(self.attempts(), [])
        r2 = self.build()                                                                      # same fingerprint: nothing is built
        self.assertEqual((r2['status'], r2['build_id'], self.stub.calls), ('current', bid, 1))
        r4 = self.build('0p4')                                                                 # another LOD: its own pointer, the 0p2 one stays
        self.assertEqual(r4['status'], 'ok')
        self.assertEqual((self.pointer('0p2')['build_id'], self.pointer('0p4')['build_id']), (bid, r4['build_id']))
        (self.mod / 'fit' / 'align.json').write_text('{"b": 9}', encoding='utf-8')            # an input changes: a new build, the old one stays
        r3 = self.build()
        self.assertEqual(r3['status'], 'ok')
        self.assertNotEqual(r3['build_id'], bid)
        self.assertTrue((self.out / bid).is_dir() and (self.out / r3['build_id']).is_dir())
        self.assertEqual(self.pointer()['build_id'], r3['build_id'])

    def test_failure_keeps_previous_build(self):
        r = self.build()
        before = (self.out / 'current_0p2.json').read_text(encoding='utf-8')
        (self.mod / 'fit' / 'align.json').write_text('{"b": 5}', encoding='utf-8')
        self.stub.mode = 'raise'
        f = self.build()
        self.assertEqual(f['status'], 'failed')
        self.assertIn('boom', f['error'])
        self.assertIn('RuntimeError', f['traceback'])
        self.assertEqual(f['previous_build'], r['build_id'])
        self.assertEqual((self.out / 'current_0p2.json').read_text(encoding='utf-8'), before)
        self.assertTrue((self.out / r['build_id'] / 'lower_arm.r.glb').is_file())
        self.assertEqual(self.attempts(), [])
        self.assertEqual(json.loads((self.out / 'last_failure.json').read_text(encoding='utf-8'))['status'], 'failed')
        self.assertEqual(sorted(p.name for p in self.out.iterdir() if p.is_dir()), [r['build_id']])      # the failed build left no folder
        self.stub.mode = 'ok'                                                                  # the next success clears last_failure.json
        self.assertEqual(self.build()['status'], 'ok')
        self.assertFalse((self.out / 'last_failure.json').exists())

    def test_other_lod_success_keeps_failure_record(self):
        self.stub.mode = 'raise'
        self.assertEqual(self.build('0p2')['status'], 'failed')
        self.stub.mode = 'ok'
        self.assertEqual(self.build('0p4')['status'], 'ok')
        self.assertTrue((self.out / 'last_failure.json').is_file())                           # it belongs to 0p2
        self.assertEqual(self.build('0p2')['status'], 'ok')
        self.assertFalse((self.out / 'last_failure.json').exists())

    def test_default_out_root_is_module_layers(self):
        r = build.build_module(self.mod, '0p4', gpu=False)
        self.assertEqual(r['status'], 'ok', r.get('error'))
        self.assertTrue((self.mod / 'layers' / 'current_0p4.json').is_file())
        self.assertTrue((self.mod / 'layers' / r['build_id'] / 'lower_arm.r.glb').is_file())

    def test_bare_build(self):
        r = build.build_bare('human_male', self.out / 'bare', gpu=False)
        self.assertEqual(r['status'], 'ok', r.get('error'))
        self.assertTrue(r['build_id'].startswith('bare-'))
        self.assertEqual(sorted(r['slots']), ['head', 'lower_arm.l', 'lower_arm.r', 'torso'])  # bare layers have no 'fill' morph and need none
        self.assertTrue((self.out / 'bare' / 'current_bare.json').is_file())
        self.assertEqual(build.build_bare('human_male', self.out / 'bare', gpu=False)['status'], 'current')
        write_body(self.bases / 'human_male.npz', salt='new body')
        r2 = build.build_bare('human_male', self.out / 'bare', gpu=False)
        self.assertEqual(r2['status'], 'ok')
        self.assertNotEqual(r2['build_id'], r['build_id'])

    def test_force_builds_again_and_never_touches_the_old_build(self):
        r = self.build()
        glb = self.out / r['build_id'] / 'lower_arm.r.glb'
        stamp = glb.stat().st_mtime_ns
        f = self.build(force=True)
        self.assertEqual(f['status'], 'ok')
        self.assertEqual((self.stub.calls, f['build_id']), (2, r['build_id'] + '-r1'))
        self.assertEqual(glb.stat().st_mtime_ns, stamp)
        self.assertEqual(self.pointer()['build_id'], f['build_id'])
        self.assertEqual(self.build()['status'], 'current')                                   # (the pointer's build is valid and has this fingerprint)

    def test_unwritable_output_path_is_a_structured_failure(self):
        self.out.write_text('i am a file', encoding='utf-8')
        r = self.build()
        self.assertEqual(r['status'], 'failed')
        self.assertIn('output folder', r['error'])

    def test_abandoned_attempts_are_cleaned_under_the_lock(self):
        self.out.mkdir()
        old_dead = self.out / '.attempt-0p2-aaaaaaaaaaaa-99999999'
        old_live = self.out / f'.attempt-0p2-bbbbbbbbbbbb-{os.getpid()}'
        young_dead = self.out / '.attempt-0p2-cccccccccccc-99999998'
        for p in (old_dead, old_live, young_dead):
            p.mkdir()
            (p / 'x.glb').write_bytes(b'x')
        t = time.time() - 2 * 3600
        for p in (old_dead, old_live):
            os.utime(p, (t, t))
        self.assertEqual(self.build()['status'], 'ok')
        left = set(self.attempts())
        self.assertEqual(left, {old_live.name, young_dead.name})                                # only the old one with no live pid went

    def test_in_process_cache_reset_follows_the_body_cache(self):
        stub_bf = types.ModuleType('body_fit')
        stub_bf._BODIES = {'human_male': 'old', 'gnome_female': 'g'}
        stub_gd = types.ModuleType('geodiv')
        stub_gd._GAME_BODY = {('human_male', 100.0): 'sub', ('gnome_female', 100.0): 'gsub'}
        stub_fl = types.ModuleType('fit_lab')
        stub_fl._PLANES = {'human_male': 'plane', 'gnome_female': 'gp'}
        self.stub._LAYER_BODY = {'human_male': 'lb', 'gnome_female': 'glb'}
        sys.modules.update({'body_fit': stub_bf, 'geodiv': stub_gd, 'fit_lab': stub_fl})
        self.assertEqual(self.build()['status'], 'ok')                                         # first build of the race: stale entries of it are dropped
        self.assertEqual((stub_bf._BODIES, stub_fl._PLANES, self.stub._LAYER_BODY), ({'gnome_female': 'g'}, {'gnome_female': 'gp'}, {'gnome_female': 'glb'}))
        self.assertEqual(stub_gd._GAME_BODY, {('gnome_female', 100.0): 'gsub'})
        stub_bf._BODIES['human_male'] = 'fresh'                                                # the same body cache: the in-memory body is kept
        (self.mod / 'fit' / 'align.json').write_text('{"b": 4}', encoding='utf-8')
        self.assertEqual(self.build()['status'], 'ok')
        self.assertEqual(stub_bf._BODIES['human_male'], 'fresh')
        write_body(self.bases / 'human_male.npz', salt='a different body cache (size and mtime change)')
        stub_gd._GAME_BODY[('human_male', 100.0)] = 'sub2'
        self.assertEqual(self.build()['status'], 'ok')                                         # the body cache changed: every cache of the race is dropped
        self.assertNotIn('human_male', stub_bf._BODIES)
        self.assertNotIn(('human_male', 100.0), stub_gd._GAME_BODY)
        self.assertIn('gnome_female', stub_bf._BODIES)

    def test_gpu_switch(self):
        build._gpu_configure(True)
        self.assertEqual(os.environ.get('BODY_FIT_GPU'), '1')
        build._gpu_configure(False)
        self.assertNotIn('BODY_FIT_GPU', os.environ)
        self.assertFalse(build._gpu_used(False))


class RequiredSlotsTest(Base):
    def setUp(self):
        super().setUp()
        write_body(self.bases / 'human_male.npz', legs=True)                                    # these humans have legs: boots are required

    def test_one_glove_human_fails(self):
        self.stub.expected = ['lower_arm.r', 'lower_arm.l', 'lower_leg.r', 'lower_leg.l']
        self.stub.skipped = {'lower_arm.l': 'no garment in this slot'}
        r = self.build()
        self.assertEqual(r['status'], 'failed')
        self.assertIn('no garment found for lower_arm.l (glove)', r['error'])
        self.assertFalse((self.out / 'current_0p2.json').exists())

    def test_missing_boot_fails_with_a_clear_message(self):
        self.stub.expected = ['lower_arm.r', 'lower_arm.l', 'lower_leg.r', 'lower_leg.l']
        self.stub.skipped = {'lower_leg.r': 'no garment in this slot', 'lower_leg.l': 'no garment in this slot'}
        r = self.build()
        self.assertEqual(r['status'], 'failed')
        self.assertIn('no garment found for lower_leg.r (boot)', r['error'])
        self.assertFalse((self.out / 'current_0p2.json').exists())

    def test_human_full_set_publishes_and_gnome_two_gloves_publish(self):
        self.stub.expected = ['lower_arm.r', 'lower_arm.l', 'lower_leg.r', 'lower_leg.l']
        r = self.build()
        self.assertEqual((r['status'], len(r['slots'])), ('ok', 4), r.get('error'))
        self.assertEqual(r['required'], self.stub.expected)
        gmod = make_module(self.tmp / 'g', race='gnome_female')
        self.stub.expected = ['lower_arm.r', 'lower_arm.l']                                    # layers.slots(): the gnome body has no leg slots
        g = build.build_module(gmod, '0p2', self.out / 'gnome', gpu=False)
        self.assertEqual((g['status'], sorted(g['slots'])), ('ok', ['lower_arm.l', 'lower_arm.r']), g.get('error'))
        # a human result WITHOUT its boots (both boots and their declaration gone) is not the human contract: refused
        hmod = make_module(self.tmp / 'h', race='human_male')
        h = build.build_module(hmod, '0p2', self.out / 'human2', gpu=False)
        self.assertEqual(h['status'], 'failed')
        self.assertIn('lower_leg', h['error'])

    def test_required_slots_come_from_the_game_body(self):
        self.assertEqual(build.required_slots('right_gauntlet_boot', 'human_male'), ['lower_arm.l', 'lower_arm.r', 'lower_leg.l', 'lower_leg.r'])
        self.assertEqual(build.required_slots('right_gauntlet_boot', 'gnome_female'), ['lower_arm.l', 'lower_arm.r'])
        self.assertEqual(build.required_slots('bare', 'gnome_female'), ['head', 'torso', 'lower_arm.l', 'lower_arm.r'])
        self.assertEqual((build.required_slots('outfit', 'gnome_female'), build.required_slots('body_armor', 'x')), (['outfit'], ['torso']))
        write_body(self.bases / 'gnome_female.npz', legs=True, compressed=True)                 # the stdlib reader also reads a compressed npz
        self.assertEqual(build.required_slots('bare', 'gnome_female')[-2:], ['lower_leg.l', 'lower_leg.r'])
        for bad in (('nope', 'human_male'), ('bare', 'Bad Race'), ('bare', 'elf_female')):
            with self.assertRaises(ValueError):
                build.required_slots(*bad)
        (self.bases / 'elf_female.npz').write_bytes(b'not a zip')
        with self.assertRaises(ValueError):
            build.required_slots('bare', 'elf_female')
        # the stdlib .npy reader agrees with numpy on the real repo caches (read only, when they exist)
        real = build.ROOT / 'tools' / 'preview' / 'cache' / 'bases'
        for race in ('human_male', 'gnome_female'):
            if (real / f'{race}.npz').is_file():
                z = np.load(real / f'{race}.npz', allow_pickle=False)
                legs = {str(n) for n in z['part_names'][np.unique(z['part'])]} & {'leg_stretch.l', 'foot.l', 'leg_stretch.r', 'foot.r'}
                old = build.BASE_CACHE
                build.BASE_CACHE = real
                try:
                    self.assertEqual(bool(build.body_leg_sides(race)), bool(legs), race)
                finally:
                    build.BASE_CACHE = old

    def test_stored_build_without_boots_or_with_foreign_slots_is_invalid(self):
        self.stub.expected = ['lower_arm.r', 'lower_arm.l', 'lower_leg.r', 'lower_leg.l']
        r = self.build()
        self.assertEqual(r['status'], 'ok', r.get('error'))
        d = self.out / r['build_id']
        rec = json.loads((d / 'build.json').read_text(encoding='utf-8'))
        # both boots and their declaration removed: the stored manifest still follows the generic kind contract, but not the human's
        cut = dict(rec, required=['lower_arm.r', 'lower_arm.l'], slots={k: v for k, v in rec['slots'].items() if k.startswith('lower_arm')})
        (d / 'build.json').write_text(json.dumps(cut), encoding='utf-8')
        self.assertTrue(any('lower_leg' in p for p in build.validate_build(d)))
        # an extra (foreign) slot in the map, or a slot path outside the build folder
        bid = r['build_id']
        for damaged in (dict(rec, slots=dict(rec['slots'], torso={'glb': f'{bid}/torso.glb', 'faces': 10})),
                        dict(rec, slots=dict(rec['slots'], **{'lower_arm.l': dict(rec['slots']['lower_arm.l'], glb='../elsewhere/lower_arm.l.glb')}))):
            (d / 'build.json').write_text(json.dumps(damaged), encoding='utf-8')
            self.assertTrue(build.validate_build(d), damaged['slots'])
        (d / 'build.json').write_text(json.dumps(rec), encoding='utf-8')
        self.assertEqual(build.validate_build(d), [])
        self.assertTrue(build.validate_build(d, expect={'race': 'gnome_female'}))           # another module identity
        # the publish path checks the same: the boot-less build.json is not 'current', it is rebuilt under a fresh id
        (d / 'build.json').write_text(json.dumps(cut), encoding='utf-8')
        (self.out / 'current_0p2.json').write_text(json.dumps(cut), encoding='utf-8')
        r2 = self.build()
        self.assertEqual((r2['status'], r2['build_id']), ('ok', bid + '-r1'), r2.get('error'))

    def test_contract_per_module_kind(self):
        self.assertEqual(build._required_problems('outfit', ['outfit']), [])
        self.assertEqual(build._required_problems('body_armor', ['torso']), [])
        self.assertTrue(build._required_problems('body_armor', ['outfit']))
        self.assertTrue(build._required_problems('right_gauntlet_boot', ['lower_arm.r']))                  # both gloves are required
        self.assertTrue(build._required_problems('right_gauntlet_boot', ['lower_arm.r', 'lower_arm.l', 'lower_leg.r']))   # legs both or neither
        self.assertEqual(build._required_problems('right_gauntlet_boot', ['lower_arm.r', 'lower_arm.l']), [])
        self.assertTrue(build._required_problems('outfit', []))
        stub_expected = ['torso']
        self.stub.module, self.stub.expected = 'outfit', stub_expected                          # a result for the wrong kind is rejected
        r = self.build()
        self.assertEqual(r['status'], 'failed')

    def test_incomplete_builds_are_rejected(self):
        self.stub.mode = 'drop_slot'
        f = self.build()
        self.assertEqual(f['status'], 'failed')
        self.assertIn('lower_arm.l', f['error'])
        self.assertFalse((self.out / 'current_0p2.json').exists())
        self.stub.mode = 'no_morph'
        f = self.build()
        self.assertEqual(f['status'], 'failed')
        self.assertIn("morph target 'fill'", f['error'])
        self.assertFalse((self.out / 'current_0p2.json').exists())
        self.stub.mode = 'ok'
        self.stub.skipped = {k: 'no garment in this slot' for k in self.stub.expected}         # nothing built at all is a failure
        self.assertEqual(self.build()['status'], 'failed')


class RecoveryTest(Base):
    def damage_and_rebuild(self, damage):
        r = self.build()
        d = self.out / r['build_id']
        before = sorted((p.name, p.stat().st_size) for p in d.iterdir() if p.is_file())
        damage(d)
        damaged = sorted((p.name, p.stat().st_size) for p in d.iterdir() if p.is_file())
        r2 = self.build()
        self.assertEqual(r2['status'], 'ok', r2.get('error'))                                    # rebuilt, not reused
        self.assertEqual(r2['build_id'], r['build_id'] + '-r1')                                  # under a fresh id: the damaged folder is not rewritten
        self.assertEqual(sorted((p.name, p.stat().st_size) for p in d.iterdir() if p.is_file()), damaged)
        self.assertEqual(self.pointer()['build_id'], r2['build_id'])
        self.assertEqual(build.validate_build(self.out / r2['build_id'], expect_id=r2['build_id']), [])
        self.assertEqual(self.build()['status'], 'current')
        self.assertEqual(self.build()['build_id'], r2['build_id'])
        return before, r, r2

    def test_missing_slot_json(self):
        self.damage_and_rebuild(lambda d: (d / 'lower_arm.l.json').unlink())

    def test_missing_glb(self):
        self.damage_and_rebuild(lambda d: (d / 'lower_arm.r.glb').unlink())

    def test_empty_slot_map(self):
        def damage(d):
            rec = json.loads((d / 'build.json').read_text(encoding='utf-8'))
            rec['slots'] = {}
            (d / 'build.json').write_text(json.dumps(rec), encoding='utf-8')
            ptr = self.out / 'current_0p2.json'
            ptr.write_text(json.dumps(rec), encoding='utf-8')
        self.damage_and_rebuild(damage)

    def test_glb_truncated_before_its_binary_data(self):
        def damage(d):
            write_glb(d / 'lower_arm.l.glb', 10, trunc_bin=True)
        self.damage_and_rebuild(damage)

    def test_glb_with_wrong_length_field(self):
        def damage(d):
            b = (d / 'lower_arm.l.glb').read_bytes()
            (d / 'lower_arm.l.glb').write_bytes(b[:-40])
        self.damage_and_rebuild(damage)

    def test_missing_build_json(self):
        self.damage_and_rebuild(lambda d: (d / 'build.json').unlink())

    def test_second_recovery_gets_the_next_id(self):
        before, r, r2 = self.damage_and_rebuild(lambda d: (d / 'lower_arm.l.json').unlink())
        (self.out / r2['build_id'] / 'lower_arm.r.json').unlink()
        r3 = self.build()
        self.assertEqual((r3['status'], r3['build_id']), ('ok', r['build_id'] + '-r2'))

    def test_valid_existing_folder_is_republished_from_its_own_build_json(self):
        r = self.build()
        (self.out / 'current_0p2.json').unlink()                                                # the pointer is lost (crash after the rename)
        calls = self.stub.calls
        r2 = self.build()
        self.assertEqual((r2['status'], r2['build_id'], self.stub.calls), ('current', r['build_id'], calls))   # adopted from build.json, nothing built
        self.assertEqual(self.pointer()['build_id'], r['build_id'])
        self.assertEqual(self.pointer()['time'], r['time'])                                    # the retained build.json, not new metadata
        self.assertEqual(self.attempts(), [])

    def test_damaged_pointer_is_repaired_from_build_json(self):
        r = self.build()
        ptr = self.out / 'current_0p2.json'
        good = json.loads(ptr.read_text(encoding='utf-8'))
        for damaged in (dict(good, slots={}), dict(good, required=['lower_arm.r']), dict(good, warnings=['copied from elsewhere'])):
            ptr.write_text(json.dumps(damaged), encoding='utf-8')
            calls = self.stub.calls
            r2 = self.build()
            self.assertEqual((r2['status'], r2['build_id'], self.stub.calls), ('current', r['build_id'], calls), damaged)
            self.assertEqual(json.loads(ptr.read_text(encoding='utf-8')), good)              # the pointer is the build's own build.json again

    def test_expected_source_digest(self):
        glb = self.mod / 'reduced' / 'lod_error_0p2.glb'
        sha = build._sha256(glb)
        with self.assertRaises(ValueError):
            build.build_module(self.mod, '0p2', self.out, gpu=False, expect_source_sha256='xyz')
        f = build.build_module(self.mod, '0p2', self.out, gpu=False, expect_source_sha256='0' * 64)
        self.assertEqual(f['status'], 'failed')
        self.assertIn('source changed', f['error'])
        self.assertFalse(f.get('retryable'))
        self.assertEqual(self.stub.calls, 0)
        self.assertFalse((self.out / 'current_0p2.json').exists())
        # the file changes WHILE the build runs: refused under the publish lock, nothing published
        self.stub.hook = lambda: glb.write_bytes(b'another approved LOD')
        f = build.build_module(self.mod, '0p2', self.out, gpu=False, expect_source_sha256=sha)
        self.assertEqual(f['status'], 'failed')
        self.assertIn('source changed', f['error'])
        self.assertFalse((self.out / 'current_0p2.json').exists())
        self.assertEqual(self.attempts(), [])
        self.stub.hook = None
        ok = build.build_module(self.mod, '0p2', self.out, gpu=False, expect_source_sha256=build._sha256(glb))
        self.assertEqual(ok['status'], 'ok', ok.get('error'))
        self.assertEqual(build.build_module(self.mod, '0p2', self.out, gpu=False, expect_source_sha256=build._sha256(glb))['status'], 'current')

    def test_glb_checker(self):
        good = self.tmp / 'g.glb'
        write_glb(good, 6)
        self.assertEqual(build.check_glb(good, True)[0], [])
        self.assertEqual(build.check_glb(good, True)[1]['faces'], 6)
        self.assertEqual(build.check_glb(self.tmp / 'nope.glb')[0][0][:10], 'unreadable')
        write_glb(good, 6, morph=False)
        self.assertIn("no morph target 'fill'", build.check_glb(good, True)[0])
        self.assertEqual(build.check_glb(good, False)[0], [])
        for cut in (5, 19, 30, 200):
            good.write_bytes(good.read_bytes()[:cut])
            self.assertTrue(build.check_glb(good)[0])
            write_glb(good, 6, morph=False)
        write_glb(good, 6, trunc_bin=True)
        self.assertTrue(build.check_glb(good)[0])


class ConcurrencyTest(Base):
    def run_workers(self, *cfgs):
        procs = [self.spawn(**c) for c in cfgs]
        outs = [p.communicate(timeout=240) for p in procs]
        for p, (o, e) in zip(procs, outs):
            self.assertEqual(p.returncode, 0, e[-1500:])
        return [last_json(o) for o, _ in outs]

    def test_different_lods_publish_independently(self):
        a, b = self.run_workers({'lod': '0p2', 'delay': 1.0}, {'lod': '0p4', 'delay': 0.2})
        self.assertEqual((a['status'], b['status']), ('ok', 'ok'), (a.get('error'), b.get('error')))
        self.assertEqual((self.pointer('0p2')['build_id'], self.pointer('0p4')['build_id']), (a['build_id'], b['build_id']))
        self.assertFalse((self.out / 'last_failure.json').exists())
        self.assertEqual(self.attempts(), [])

    def test_same_lod_same_inputs_one_build_one_adopter(self):
        a, b = self.run_workers({'delay': 1.0}, {'delay': 1.0})
        self.assertEqual(sorted((a['status'], b['status'])), ['current', 'ok'], (a.get('error'), b.get('error')))
        self.assertEqual(a['build_id'], b['build_id'])
        self.assertEqual([p.name for p in self.out.iterdir() if p.is_dir()], [a['build_id']])
        self.assertFalse((self.out / 'last_failure.json').exists())                          # the loser did not record a failure
        self.assertEqual(self.pointer()['build_id'], a['build_id'])
        self.assertEqual(self.attempts(), [])

    def test_older_worker_cannot_replace_the_newer_selection(self):
        slow = self.spawn(delay=3.0)                                                          # starts with the old inputs ...
        for _ in range(300):                                                                  # (wait until it is building: its fingerprint is taken)
            if self.out.is_dir() and self.attempts():
                break
            time.sleep(0.1)
        (self.mod / 'fit' / 'align.json').write_text('{"b": "newer"}', encoding='utf-8')     # ... the inputs change ...
        fast = self.spawn(delay=0.0)                                                          # ... a newer worker publishes first
        fo, fe = fast.communicate(timeout=240)
        so, se = slow.communicate(timeout=240)
        f, s = last_json(fo), last_json(so)
        self.assertEqual(f['status'], 'ok', fe[-800:])
        self.assertEqual(s['status'], 'failed', se[-800:])
        self.assertIn('inputs changed during build', s['error'])
        self.assertTrue(s.get('retryable'))
        self.assertEqual(self.pointer()['build_id'], f['build_id'])                           # the pointer is the winner's, untouched by the loser
        self.assertFalse((self.out / 'last_failure.json').exists())                          # and so is the failure record
        self.assertEqual([p.name for p in self.out.iterdir() if p.is_dir()], [f['build_id']])
        self.assertEqual(self.attempts(), [])

    def test_inputs_changed_during_the_build_in_process(self):
        before = self.build()
        (self.mod / 'fit' / 'align.json').write_text('{"b": 11}', encoding='utf-8')
        ptr_before = (self.out / 'current_0p2.json').read_text(encoding='utf-8')
        self.stub.hook = lambda: (self.mod / 'fit' / 'align.json').write_text('{"b": 12}', encoding='utf-8')
        r = self.build()
        self.assertEqual(r['status'], 'failed')
        self.assertIn('inputs changed during build', r['error'])
        self.assertTrue(r['retryable'])
        self.assertEqual(r['previous_build'], before['build_id'])
        self.assertEqual((self.out / 'current_0p2.json').read_text(encoding='utf-8'), ptr_before)
        self.assertFalse((self.out / 'last_failure.json').exists())
        self.assertEqual(self.attempts(), [])
        self.stub.hook = None
        self.assertEqual(self.build()['status'], 'ok')                                         # retrying works

    def test_publish_lock_is_exclusive_across_processes(self):
        self.out.mkdir()
        code = ('import sys,time; sys.path.insert(0, sys.argv[1]); import build; from pathlib import Path\n'
                'with build._publish_lock(Path(sys.argv[2])):\n    print("held", flush=True); time.sleep(2.0)\n')
        p = subprocess.Popen([sys.executable, '-c', code, str(HERE), str(self.out)], stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), 'held')
        t0 = time.monotonic()
        with self.assertRaises(TimeoutError):
            with build._publish_lock(self.out, timeout=0.5):
                pass
        self.assertGreater(time.monotonic() - t0, 0.4)
        p.wait(timeout=30)
        p.stdout.close()
        with build._publish_lock(self.out, timeout=5.0):                                       # released when the holder ended
            pass


class CliTest(Base):
    def run_cli(self, *args):
        p = subprocess.run([sys.executable, str(HERE / 'build.py'), *args], capture_output=True, text=True, cwd=str(self.tmp), timeout=120)
        return p.returncode, (last_json(p.stdout) if p.stdout.strip() else None), p

    def assertFailed(self, args, text=None):
        code, res, p = self.run_cli(*args)
        self.assertEqual(code, 1, (args, p.stdout, p.stderr[-500:]))
        self.assertIsNotNone(res, (args, p.stderr[-500:]))
        self.assertEqual(res['status'], 'failed')
        if text:
            self.assertIn(text, res['error'])
        return res

    def test_help_from_any_cwd(self):
        p = subprocess.run([sys.executable, str(HERE / 'build.py'), '--help'], capture_output=True, text=True, cwd=str(self.tmp))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('module', p.stdout)

    def test_every_failure_path_prints_json_and_exits_1(self):
        self.assertFailed([], 'argument error')                                                       # no sub-command
        self.assertFailed(['module'], 'argument error')                                              # missing arguments
        self.assertFailed(['module', str(self.mod), '--lod', '0p3'], 'argument error')              # invalid lod
        self.assertFailed(['bare'], 'argument error')
        self.assertFailed(['parity'], 'argument error')
        self.assertFailed(['nonsense'], 'argument error')
        self.assertFailed(['module', str(self.tmp / 'missing'), '--lod', '0p2'], 'module dir not found')
        self.assertFailed(['bare', '../escape'], 'invalid race')
        self.assertFailed(['bare', 'nobody'], 'missing game body cache')
        self.assertFailed(['module', str(self.mod), '--lod', '0p2', '--expect-source-sha256', 'nothex'], 'expect_source_sha256')
        occupied = self.tmp / 'occupied'
        occupied.write_text('x', encoding='utf-8')
        res = self.assertFailed(['module', str(self.mod), '--lod', '0p2', '--out', str(occupied)])    # the output path is a file (also: no game body cache here)
        self.assertTrue(res['error'])
        self.assertFailed(['parity', str(self.tmp / 'r'), 'set', str(self.tmp / 'n')], 'reference folder not found')

    def test_failure_names_the_previous_build(self):
        r = self.build('0p2')
        self.assertEqual(r['status'], 'ok')
        (self.mod / 'reduced' / 'lod_error_0p2.glb').unlink()
        res = self.assertFailed(['module', str(self.mod), '--lod', '0p2', '--out', str(self.out)], 'lod_error_0p2.glb')
        self.assertEqual(res['previous_build'], r['build_id'])

    def test_occupied_output_path_with_valid_inputs(self):
        (self.tmp / 'rootdir').mkdir()
        occupied = self.tmp / 'occ2'
        occupied.write_text('x', encoding='utf-8')
        mod = self.mod
        code = ('import sys, json; sys.path.insert(0, sys.argv[1]); import build, test_build; from pathlib import Path\n'
                'build.BASE_CACHE = Path(sys.argv[2]); sys.modules["layers"] = test_build.StubLayers()\n'
                'sys.exit(build.main(["module", sys.argv[3], "--lod", "0p2", "--out", sys.argv[4], "--cpu"]))')
        p = subprocess.run([sys.executable, '-c', code, str(HERE), str(self.bases), str(mod), str(occupied)], capture_output=True, text=True, cwd=str(self.tmp), timeout=120)
        self.assertEqual(p.returncode, 1, p.stderr[-800:])
        res = last_json(p.stdout)
        self.assertEqual(res['status'], 'failed')
        self.assertIn('output folder', res['error'])


class ParityTest(Base):
    def setUp(self):
        super().setUp()
        self.ref = self.tmp / 'ref'
        self.ref.mkdir()
        self.stub.expected = ['lower_arm.r', 'lower_arm.l']

    def prepare(self):
        """A published stub build and a matching reference folder (<slot>__<set>.glb / .json)."""
        r = self.build()
        self.assertEqual(r['status'], 'ok')
        for k in r['slots']:
            shutil.copy(self.out / r['build_id'] / f'{k}.glb', self.ref / f'{k}__s.glb')
            (self.ref / f'{k}__s.json').write_text(json.dumps({'notes': {'a': 1.0}, 'mirrored_from_right': k.endswith('.l')}), encoding='utf-8')
            (self.out / r['build_id'] / f'{k}.json').write_text(json.dumps({'notes': {'a': 1.0}, 'mirrored_from_right': k.endswith('.l')}), encoding='utf-8')
        return r

    def test_identical_passes(self):
        r = self.prepare()
        p = build.parity(self.ref, 's', self.out, lod='0p2')
        self.assertEqual((p['status'], p['problems']), ('ok', []))
        self.assertEqual(sorted(p['slots']), ['lower_arm.l', 'lower_arm.r'])
        s = p['slots']['lower_arm.r']
        self.assertEqual((s['faces_matched'], s['tie_break_faces'], s['role_diff_faces'], s['colour_diff_faces']), (True, 0, 0, 0))
        self.assertEqual(max(s['w0_max_mm'], s['w1_max_mm']), 0.0)
        self.assertEqual(build.parity(self.ref, 's', self.out / r['build_id'])['status'], 'ok')   # a build folder works too

    def test_missing_inputs_fail(self):
        self.prepare()
        for args in ((self.ref, 's', self.tmp / 'nowhere'), (self.tmp / 'noref', 's', self.out), (self.ref, 's', self.tmp)):
            p = build.parity(*args)
            self.assertEqual(p['status'], 'failed', args)
            self.assertTrue(p['problems'])
        (self.ref / 'lower_arm.l__s.glb').unlink()                                                # a required slot without reference
        p = build.parity(self.ref, 's', self.out, lod='0p2')
        self.assertEqual(p['status'], 'failed')
        self.assertTrue(any('lower_arm.l' in x for x in p['problems']))

    def test_missing_and_extra_slots_fail(self):
        r = self.prepare()
        (self.out / r['build_id'] / 'lower_arm.l.glb').unlink()
        p = build.parity(self.ref, 's', self.out, lod='0p2')
        self.assertEqual(p['status'], 'failed')
        self.assertTrue(any('not in the build' in x for x in p['problems']))
        write_glb(self.out / r['build_id'] / 'lower_leg.r.glb', 3)
        p = build.parity(self.ref, 's', self.out, lod='0p2')
        self.assertTrue(any('lower_leg.r is in the build but not in the reference' in x for x in p['problems']))

    def test_reversed_winding_fails(self):
        r = self.prepare()
        rev = self.out / r['build_id'] / 'lower_arm.r.glb'
        V, F, M = build._read_glb(rev)
        tris = V[F][:, ::-1]                                                                      # corner order reversed: same corners, other winding
        tris_gl = np.stack([tris[..., 0], tris[..., 2], -tris[..., 1]], axis=-1)                 # back to glTF Y-up
        write_glb(rev, tris=tris_gl, names=list(M))
        p = build.parity(self.ref, 's', self.out, lod='0p2')
        self.assertEqual(p['status'], 'failed')
        self.assertFalse(p['slots']['lower_arm.r']['faces_matched'])

    def test_changed_colours_fail(self):
        r = self.prepare()
        g = self.out / r['build_id'] / 'lower_arm.r.glb'
        V, F, M = build._read_glb(g)
        tris_gl = np.stack([V[F][..., 0], V[F][..., 2], -V[F][..., 1]], axis=-1)
        write_glb(g, tris=tris_gl, names=list(M), rgb={'primary': (1.0, 1.0, 1.0)})               # same names, other colour value
        p = build.parity(self.ref, 's', self.out, lod='0p2')
        self.assertEqual(p['status'], 'failed')
        self.assertEqual(p['slots']['lower_arm.r']['colour_diff_faces'], 10)
        self.assertTrue(p['slots']['lower_arm.r']['roles_equal'])

    def test_tie_break_is_reported_separately_and_bounded(self):
        r = self.prepare()
        g = self.out / r['build_id'] / 'lower_arm.r.glb'
        V, F, M = build._read_glb(g)
        tris_gl = np.stack([V[F][..., 0], V[F][..., 2], -V[F][..., 1]], axis=-1)
        names = list(M)
        names[3] = 'accent:tripo'                                                                  # a garment-role vote that went the other way
        write_glb(g, tris=tris_gl, names=names)
        p = build.parity(self.ref, 's', self.out, lod='0p2')
        self.assertEqual(p['tie_break'], {'lower_arm.r': 1})
        self.assertEqual((p['slots']['lower_arm.r']['role_diff_faces'], p['slots']['lower_arm.r']['colour_diff_faces']), (0, 0))
        self.assertEqual(p['status'], 'ok', p['problems'])
        names[4] = 'skin:tripo'                                                                    # skin <-> garment is a real difference
        write_glb(g, tris=tris_gl, names=names)
        p = build.parity(self.ref, 's', self.out, lod='0p2')
        self.assertEqual(p['status'], 'failed')
        self.assertEqual(p['slots']['lower_arm.r']['role_diff_faces'], 1)


class RealCodeTest(unittest.TestCase):
    """Checks of the real layer code, each in a subprocess (importing it loads body_fit / scipy; the stub tests must not see it)."""

    def run_py(self, code, env=None, timeout=300):
        e = dict(os.environ, **(env or {}))
        p = subprocess.run([sys.executable, '-c', code, str(HERE)], capture_output=True, text=True, env=e, timeout=timeout)
        self.assertEqual(p.returncode, 0, p.stderr[-2500:])
        return last_json(p.stdout)

    def test_role_tie_break_is_hash_seed_independent(self):
        code = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
import numpy as np
import geodiv as gd
res = {}
for name, a, b in (('custom', 'quinary', 'lining'), ('standard', 'primary', 'accent'), ('standard2', 'accent', 'primary'), ('mixed', 'lining', 'secondary')):
    # f0: a skin face far from the body, touching one face of role a and one of role b -> a tie decided by the tie-break
    V = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0], [-1, 0, 0], [0, -1, 0]], float)
    F = np.array([[0, 1, 2], [1, 3, 2], [0, 2, 4]])
    m = gd.Mesh(V, F, ['skin', a, b], [0, 0, 0], 'tripo')
    class B:
        def measure(self, P):
            return {'d': np.full(len(P), 1.0)}
    n = gd.relabel_off_body_skin(m, B(), iters=1)
    res[name] = [n, str(m.role[0])]
res['pick'] = [gd.pick_role(['quinary', 'lining']), gd.pick_role(['lining', 'quinary']), gd.pick_role(['primary', 'accent']), gd.pick_role(['x', 'x', 'primary'])]
print(json.dumps(res))
'''
        outs = []
        for seed in ('0', '1', '2', '3', '4', '5'):
            outs.append(self.run_py(code, {'PYTHONHASHSEED': seed}))
        self.assertTrue(all(o == outs[0] for o in outs), outs)
        self.assertEqual(outs[0]['custom'], [1, 'lining'])                       # lexical fallback for roles with no canonical place
        self.assertEqual(outs[0]['standard'], [1, 'primary'])                    # the canonical priority
        self.assertEqual(outs[0]['standard2'], [1, 'primary'])
        self.assertEqual(outs[0]['mixed'], [1, 'secondary'])                     # a canonical role beats a custom one
        self.assertEqual(outs[0]['pick'], ['lining', 'lining', 'primary', 'x'])

    def test_pickle_free_loads(self):
        code = r'''
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import numpy as np
import shell_slots as ss
d = Path(tempfile.mkdtemp())
np.savez(d / 'ok.npz', body_part_glb_order=np.arange(3), body_part_names=np.array(['a', 'bb']))
np.savez(d / 'obj.npz', body_part_glb_order=np.array([{'x': 1}, None, 3], dtype=object), body_part_names=np.array(['a']))
np.savez(d / 'obj_names.npz', body_part_glb_order=np.arange(3), body_part_names=np.array([['a'], None], dtype=object))
out = {}
ss.load_npz(d / 'ok.npz', {'body_part_glb_order': 'iu', 'body_part_names': 'US'})
for n in ('obj', 'obj_names'):
    try:
        ss.load_npz(d / (n + '.npz'), {'body_part_glb_order': 'iu', 'body_part_names': 'US'})
        out[n] = 'accepted'
    except ValueError as e:
        out[n] = str(e)
print(json.dumps(out))
'''
        out = self.run_py(code)
        for n in ('obj', 'obj_names'):
            self.assertIn('not accepted', out[n])
        src = (HERE / 'layers.py').read_text(encoding='utf-8') + (HERE / 'shell_slots.py').read_text(encoding='utf-8')
        self.assertNotIn('allow_pickle=True', src)


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == '--worker':
        worker(json.loads(sys.argv[2]))
    else:
        unittest.main()
