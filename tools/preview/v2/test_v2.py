"""Tests of the v2 preview backend. Run: python -m unittest tools/preview/v2/test_v2.py  (temp roots only; the real root is never touched)."""
import gzip
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import shutil
import subprocess
import textwrap
import threading
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import actions  # noqa: E402
import catalog  # noqa: E402
import decisions  # noqa: E402
import guard  # noqa: E402
import migrate  # noqa: E402
import server  # noqa: E402
import serve  # noqa: E402
from claim_lock import claim_lock  # noqa: E402

STUB = '''import json, pathlib, sys
args=sys.argv[1:]
root=pathlib.Path(args[args.index('--root')+1]); command=args[args.index('--root')+2]
if command=='check':
 p=pathlib.Path(args[-1])
 if p.name=='bad': print('invalid set',file=sys.stderr);sys.exit(2)
 print(json.dumps({'set':p.name,'valid':True})); sys.exit(0)
if command=='promote':
 batch=pathlib.Path(args[args.index('promote')+1]); number=args[args.index('promote')+2]
 files=list(batch.glob(number+'_*__*.png'))
 key=files[0].name.split('__')[0]
 target=root/'approved'/(batch.name+'_'+key)
 staging=root/'approved'/('.promote_'+key)
 staging.joinpath('human_male').mkdir(parents=True)
 for f in files: staging.joinpath('human_male',f.name.split('__')[1].replace('.png','_front.png')).write_bytes(f.read_bytes())
 pid=args[args.index('--promotion-id')+1] if '--promotion-id' in args else None
 if pid: staging.joinpath('.preview-promotion.json').write_text(json.dumps({'promotion_id':pid}))
 staging.rename(target)
 print(json.dumps({'set':target.name,'stage':'approved'}|({'promotion_id':pid} if pid else {})))
'''
RACES = ('human_male', 'dwarf_male')
LAYER_SLOTS = {'outfit': ('outfit',), 'body_armor': ('torso',), 'right_gauntlet_boot': ('lower_arm.l', 'lower_arm.r', 'lower_leg.l', 'lower_leg.r')}
PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 16


def layer_glb(tag: str = '', morph: bool = True) -> bytes:
    """A minimal VALID layer GLB (one triangle, morph target 'fill' unless `morph` is False) that tools/layers/build.py's check_glb accepts;
    `tag` goes into asset.generator so every slot / build has its own bytes. One face."""
    import struct
    pos = struct.pack('<9f', 0, 0, 0, 1, 0, 0, 0, 1, 0)
    blob = pos + (pos if morph else b'')
    views = [{'buffer': 0, 'byteOffset': 0, 'byteLength': 36}] + ([{'buffer': 0, 'byteOffset': 36, 'byteLength': 36}] if morph else [])
    accs = [{'bufferView': i, 'componentType': 5126, 'count': 3, 'type': 'VEC3'} for i in range(len(views))]
    prim = {'attributes': {'POSITION': 0}} | ({'targets': [{'POSITION': 1}]} if morph else {})
    mesh = {'primitives': [prim]} | ({'extras': {'targetNames': ['fill']}} if morph else {})
    js = json.dumps({'asset': {'version': '2.0', 'generator': tag}, 'meshes': [mesh], 'accessors': accs, 'bufferViews': views,
                     'buffers': [{'byteLength': len(blob)}]}).encode()
    js += b' ' * (-len(js) % 4)
    body = struct.pack('<II', len(js), 0x4E4F534A) + js + struct.pack('<II', len(blob), 0x004E4942) + blob
    return struct.pack('<4sII', b'glTF', 2, 12 + len(body)) + body


def write_slot(folder: Path, build_id: str, slot: str, morph: bool = True) -> dict:
    """Write `<slot>.glb` (valid) + `<slot>.json` into a build folder; returns its build.json slot entry with faces, size and sha256."""
    import hashlib
    data = layer_glb(f'{build_id}/{slot}', morph)
    (folder / f'{slot}.glb').write_bytes(data)
    (folder / f'{slot}.json').write_text('{}')
    return {'glb': f'{build_id}/{slot}.glb', 'json': f'{build_id}/{slot}.json', 'faces': 1, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


class V2Case(unittest.TestCase):
    def setUp(self):
        catalog.STATE_TTL = 0
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / 'project'
        xdg = mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(base / '_xdg')})   # never the real Tripo profile reservation
        xdg.start()
        self.addCleanup(xdg.stop)
        self.monsters_env = os.environ.get('IMAGETO3D_MONSTERS')
        self.mroot = base / 'monsters_aaa'
        os.environ['IMAGETO3D_MONSTERS'] = str(self.mroot)
        for name in ('concepts', 'provisional', 'approved', 'trash/concepts', 'archive/concepts', 'models', 'completed', 'processing',
                     'processing_views', 'multiview_ready', 'failed', 'held_conversions', 'tools/tripo_pipeline', 'tools/preview'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        for race in RACES:
            (self.root / 'base_views' / race).mkdir(parents=True)
            (self.root / 'base_views' / race / 'palette.json').write_text('{}')
        (self.root / 'tools/tripo_pipeline/workflow.py').write_text(STUB)
        self.web = base / 'web'
        (self.web / 'vendor').mkdir(parents=True)
        (self.web / 'index.html').write_text('<!doctype html><html><head><meta name="v2-token" content=""><title>t</title></head><body></body></html>')
        (self.web / 'main.js').write_text('export const x = 1;')
        (self.web / 'vendor' / 'three.module.js').write_text('//three')
        (self.web / 'secret.py').write_text('nope')
        self.server = server.create_server(self.root, port=0, web=self.web, extra_hosts=[])
        self.token = self.server.token
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        if self.monsters_env is None:
            os.environ.pop('IMAGETO3D_MONSTERS', None)
        else:
            os.environ['IMAGETO3D_MONSTERS'] = self.monsters_env
        self.temp.cleanup()

    # ------------------------------------------------------------------------------------------------------ helpers
    def raw(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        conn.close()
        return result

    def get(self, path, **headers):
        status, head, data = self.raw('GET', path, headers=headers)
        if path.startswith('/api/'):
            if head.get('Content-Encoding') == 'gzip':
                data = gzip.decompress(data)
            return status, json.loads(data)
        return status, data

    def post(self, path, payload, token=True, headers=None):
        head = {'Content-Type': 'application/json'} | (headers or {})
        if token:
            head['X-Token'] = self.token if token is True else token
        status, _h, data = self.raw('POST', path, json.dumps(payload).encode(), head)
        return status, json.loads(data)

    def act(self, id, verb, **extra):
        return self.post('/api/v2/action', {'id': id, 'verb': verb} | extra)

    def concept(self, name='one', folder='concepts', race='human_male', module='outfit'):
        folder_path = self.root / folder / name / race
        folder_path.mkdir(parents=True)
        (folder_path / f'{module}_front.png').write_bytes(b'png')
        (folder_path / f'{module}_prompts.json').write_text('{"brief":"robe","attempt_count":2}')

    def job(self, set_name, job_id, stage, units=(('human_male', 'outfit'),), errors=(), state='generating_other_views', suffix='', record_set=None, extra=None):
        folder = self.root / stage / f'{set_name}{suffix}--{job_id}'
        for race, module in units:
            (folder / race / module).mkdir(parents=True, exist_ok=True)
            (folder / race / module / 'front.png').write_bytes(b'png')
        record = {'set': record_set or f'{set_name}{suffix}', 'job_id': job_id, 'state': state, 'modules': [{'race_gender': r, 'module': m} for r, m in units],
                  'conversion_errors': list(errors)} | (extra or {})
        (folder / '.tripo-job.json').write_text(json.dumps(record))
        return folder

    def reduce(self, set_name, job_id, race, module, levels=('0p4', '0p2', '0p1'), mtime=None, lod=None):
        """A reduced module under models/<set_name>/ (pass the PHYSICAL model dir, e.g. `s_dwarf_male`, for suffixed jobs)."""
        reduced = self.root / 'models' / set_name / job_id / race / module / 'reduced'
        reduced.mkdir(parents=True, exist_ok=True)
        for lv in levels:
            (reduced / f'lod_error_{lv}.glb').write_bytes(b'glb')
        manifest = reduced / 'manifest.json'
        manifest.write_text(json.dumps({'levels': {lv: {'triangles': 1000 + i} for i, lv in enumerate(('0p4', '0p2', '0p1'))}}))
        if mtime:
            os.utime(manifest, (mtime, mtime))
        if lod:
            (reduced.parent / 'chosen_lod.json').write_text(json.dumps({'lod': lod}))
        return reduced

    def fronts(self, set_name, race, state=None):
        """A supervisor `fronts--<set>--<race>` task (and its status file when `state` is given)."""
        batch = self.root / '.codex_jobs' / '_batch'
        (batch / 'tasks').mkdir(parents=True, exist_ok=True)
        (batch / 'status').mkdir(parents=True, exist_ok=True)
        name = f'fronts--{set_name}--{race}'
        (batch / 'tasks' / f'{name}.json').write_text(json.dumps({'name': name, 'outputs': []}))
        if state:
            (batch / 'status' / f'{name}.json').write_text(json.dumps({'state': state}))

    def lod(self, set_name='s', rev=0, **body):
        """POST /api/v2/lod with the job the client displayed (taken from the item unless given)."""
        _, item = self.get(f'/api/v2/item?id=set:{set_name}')
        jobs = {f'{u["race"]}/{u["module"]}': u['job'] for u in item['units']}
        payload = {'id': f'set:{set_name}', 'rev': rev} | body
        if 'race' in body and 'job' not in body:
            payload['job'] = jobs[f'{body["race"]}/{body["module"]}']
        elif 'job' not in body and 'jobs' not in body:
            payload['jobs'] = jobs
        return self.post('/api/v2/lod', payload)

    def sha(self, set_name, job, race, module='outfit', lod='0p2'):
        """sha256 of a reduced LOD GLB (what a 3D approval pins as glb_sha256)."""
        import hashlib
        return hashlib.sha256((self.root / 'models' / set_name / job / race / module / 'reduced' / f'lod_error_{lod}.glb').read_bytes()).hexdigest()

    def unit(self, set_name, race='human_male', module='outfit'):
        _, item = self.get(f'/api/v2/item?id=set:{set_name}')
        return next(u for u in item['units'] if u['race'] == race and u['module'] == module), item

    def full_set(self, name='s', job='j1', modules=('outfit',)):
        units = tuple((r, m) for r in RACES for m in modules)
        self.job(name, job, 'completed', units, state='converted')
        for race, module in units:
            self.reduce(name, job, race, module)

    def publish(self, set_name, job, race, module, lod='0p2', fingerprint='fp0', slots=None, model_set=None):
        """A published layer build (pointer + build folder with stub GLBs) of a module, as tools/layers/build.py would leave it."""
        ms = model_set or set_name
        out = self.root / 'models' / ms / job / race / module / 'layers'
        build_id = f'{lod}-{fingerprint[:12]}'
        (out / build_id).mkdir(parents=True, exist_ok=True)
        slots = slots or LAYER_SLOTS[module]
        entries = {slot: write_slot(out / build_id, build_id, slot) for slot in slots}
        import builds as _builds
        record = {'build_id': build_id, 'fingerprint': fingerprint, 'lod': lod, 'status': 'ok', 'kind': module, 'race': race, 'set': ms,
                  'module': _builds.lb()._rel(out.parent), 'required': list(slots), 'warnings': [], 'metrics': {'wall_crossings_left': 0},
                  'slots': entries}
        (out / build_id / 'build.json').write_text(json.dumps(record))
        (out / f'current_{lod}.json').write_text(json.dumps(record))
        return record

    def monster(self, monster='Horse', variant='1', lods=('0p4',), approved=None, image=True, complete=False):
        """`complete`: a full legacy reduction (5 LODs, manifest.json, materials.json): only such a one can be Completed (phase 5 review F12)."""
        views = self.mroot / 'pipeline' / monster / 'views' / variant
        models = self.mroot / 'pipeline' / monster / 'models' / variant
        views.mkdir(parents=True, exist_ok=True)
        models.mkdir(parents=True, exist_ok=True)
        if image:
            (views / 'front.png').write_bytes(b'png')
        for lv in (('0p8', '0p6', '0p4', '0p2', '0p1') if complete else lods):
            (models / 'reduced').mkdir(exist_ok=True)
            (models / 'reduced' / f'lod_error_{lv}.glb').write_bytes(b'glb')
        if complete:
            (models / 'reduced' / 'manifest.json').write_text('{"levels": {}}')
            (models / 'reduced' / 'materials.json').write_text('{"materials": {}}')
        if approved is not None:
            (models / 'chosen_lod.json').write_text(json.dumps({'lod': lods[0], 'approved': approved}))


class StageTests(V2Case):
    def test_unit_status_each_stage(self):
        self.job('s_views', 'a', 'processing_views')
        self.job('s_ready', 'a', 'multiview_ready', state='ready_for_tripo')
        self.job('s_conv', 'a', 'processing', state='ready_for_tripo')
        self.job('s_held', 'a', 'held_conversions')
        self.job('s_failed', 'a', 'failed')
        self.job('s_err', 'a', 'processing', errors=['human_male/outfit: doubled interior shell'], state='partially_converted')
        for name, status in (('s_views', 'views'), ('s_ready', 'ready'), ('s_conv', 'converting'), ('s_held', 'held'), ('s_failed', 'failed'), ('s_err', 'failed')):
            unit, item = self.unit(name)
            self.assertEqual((name, unit['status'], item['stage']), (name, status, 'approved'))
        self.assertEqual([u['race'] for u in self.unit('s_views')[1]['units']], ['human_male'])      # dwarf_male: never claimed, no fronts task
        self.assertEqual(self.unit('s_views')[1]['not_converted'], [{'race': 'dwarf_male', 'reason': 'never claimed'}])
        self.fronts('s_views', 'dwarf_male')                                                             # a planned (pending) front makes the race required
        unit, item = self.unit('s_views', 'dwarf_male')
        self.assertEqual(unit['status'], 'missing')
        self.assertIn('doubled', self.unit('s_err')[0]['error'])
        _, state = self.get('/api/v2/state')
        card = next(c for c in state['tabs']['approved'] if c['id'] == 'set:s_conv')
        self.assertEqual(card['progress']['human_male']['outfit']['status'], 'converting')     # phase 4: the unit's progress record
        self.assertEqual(set(card['progress']['human_male']['outfit']), {'status', 'views_done', 'views_total', 'held_reason', 'converting_step', 'error'})
        self.assertEqual((card['type'], card['kind'], card['stage'], card['actions']), ('character', 'outfit', 'approved', []))

    def test_reduced_beats_converting_and_set_reaches_3d(self):
        self.job('s', 'a', 'processing', (('human_male', 'outfit'), ('dwarf_male', 'outfit')), state='ready_for_tripo')
        self.reduce('s', 'a', 'human_male', 'outfit')
        self.assertEqual(self.unit('s')[0]['status'], 'reduced')
        self.assertEqual(self.unit('s', 'dwarf_male')[1]['stage'], 'approved')
        self.reduce('s', 'a', 'dwarf_male', 'outfit')
        unit, item = self.unit('s', 'dwarf_male')
        self.assertEqual((unit['status'], item['stage']), ('reduced', '3d'))
        self.assertEqual(unit['lod'], '0p2')
        self.assertFalse(unit['lod_saved'])
        self.assertEqual(unit['lods']['0p4']['faces'], 1000)
        self.assertTrue(unit['lods']['0p1']['url'].endswith('/models/s/a/dwarf_male/outfit/reduced/lod_error_0p1.glb'))
        self.assertIn('front', unit['views'])
        _, state = self.get('/api/v2/state')
        self.assertEqual([c['id'] for c in state['tabs']['3d']], ['set:s'])
        card = state['tabs']['3d'][0]
        self.assertTrue(card['model'].endswith('lod_error_0p2.glb'))
        self.assertTrue(card['thumbnail'].endswith('front.png'))        # the job's front until a fresh .thumb.png exists
        self.assertEqual(sorted(card['races']), sorted(RACES))

    def test_incomplete_reduction_is_not_reduced(self):
        self.job('s', 'a', 'processing', state='ready_for_tripo')
        self.reduce('s', 'a', 'human_male', 'outfit', levels=('0p4', '0p2'))
        self.assertEqual(self.unit('s')[0]['status'], 'converting')

    def test_pinned_job_and_newest_conversion(self):
        self.full_set('s', 'old')
        self.job('s', 'new', 'completed', (('human_male', 'outfit'),), state='converted')
        self.reduce('s', 'old', 'human_male', 'outfit', mtime=1000)
        self.reduce('s', 'new', 'human_male', 'outfit', mtime=2000)
        unit, _ = self.unit('s')
        self.assertEqual((unit['job'], unit['jobs']), ('new', ['new', 'old']))
        self.assertEqual(unit['folder'], 'completed/s--new')
        # before the approval a pick lands in the newest conversion (the one the item shows) and bumps the rev
        status, result = self.lod('s', 0, race='human_male', module='outfit', lod='0p1')
        self.assertEqual((status, result['rev']), (200, 1))
        self.assertTrue((self.root / 'models/s/new/human_male/outfit/chosen_lod.json').is_file())
        self.assertFalse((self.root / 'models/s/old/human_male/outfit/chosen_lod.json').exists())
        pin = {'schema': 1, 'rev': 3, 'lod_approval': {'units': {'human_male/outfit': {'job': 'old', 'lod': '0p2', 'glb_sha256': self.sha('s', 'old', 'human_male')}}}}
        (self.root / 'models/s/pipeline_v2.json').write_text(json.dumps(pin))
        unit, item = self.unit('s')
        self.assertEqual((unit['job'], item['rev'], item['stage'], item['locked']), ('old', 3, '3d', True))   # approved, layer build not published yet
        self.publish('s', 'old', 'human_male', 'outfit')
        unit, item = self.unit('s')
        self.assertEqual((unit['job'], item['rev'], item['stage'], item['locked']), ('old', 3, 'aligned', True))   # lod_approval without aligned_approval
        self.assertEqual((unit['lod'], unit['lod_saved'], unit['frozen']), ('0p2', True, True))     # the FROZEN LOD, not chosen_lod.json (#12)
        (self.root / 'models/s/old/human_male/outfit/chosen_lod.json').write_text(json.dumps({'lod': '0p4'}))
        self.assertEqual(self.unit('s')[0]['lod'], '0p2')
        pin['aligned_approval'] = {'units': {'human_male/outfit': {'job': 'old', 'lod': '0p2', 'build_id': '0p2-fp0'}}}
        (self.root / 'models/s/pipeline_v2.json').write_text(json.dumps(pin))
        unit, item = self.unit('s')
        self.assertEqual(item['stage'], 'aligned')                                     # no bare builds / ready.json: not Completed (F3)
        self.assertIn('ready.json is missing or belongs to another approval', item['not_completed'])

    def test_physical_model_path_of_suffixed_jobs(self):
        """`<set>_<race>--<id>` and `<set>_gnome--<id>` belong to the base set card, but the converter wrote models/<record set>/<id>/..."""
        (self.root / 'base_views' / 'gnome_female').mkdir()
        (self.root / 'base_views' / 'gnome_female' / 'palette.json').write_text('{}')
        self.job('s', 'a', 'completed', (('human_male', 'outfit'),), state='converted')
        self.job('s', 'b', 'completed', (('dwarf_male', 'outfit'),), state='converted', suffix='_dwarf_male')
        self.job('s', 'g', 'completed', (('gnome_female', 'outfit'),), state='converted', suffix='_gnome')
        self.reduce('s', 'a', 'human_male', 'outfit')
        self.reduce('s_dwarf_male', 'b', 'dwarf_male', 'outfit')
        self.reduce('s_gnome', 'g', 'gnome_female', 'outfit')
        _, state = self.get('/api/v2/state')
        self.assertEqual([c['id'] for c in state['tabs']['3d']], ['set:s'])
        self.assertEqual([c['id'] for c in state['tabs']['approved']], [])
        _, item = self.get('/api/v2/item?id=set:s')
        units = {u['race']: u for u in item['units']}
        self.assertEqual({r: u['status'] for r, u in units.items()}, {'human_male': 'reduced', 'dwarf_male': 'reduced', 'gnome_female': 'reduced'})
        self.assertEqual({r: u['model_set'] for r, u in units.items()}, {'human_male': 's', 'dwarf_male': 's_dwarf_male', 'gnome_female': 's_gnome'})
        self.assertTrue(units['dwarf_male']['lods']['0p2']['url'].endswith('/models/s_dwarf_male/b/dwarf_male/outfit/reduced/lod_error_0p2.glb'))
        self.assertTrue(units['gnome_female']['lods']['0p4']['url'].endswith('/models/s_gnome/g/gnome_female/outfit/reduced/lod_error_0p4.glb'))
        # a pick is written next to the physical model, the decisions stay with the card's set
        status, result = self.lod('s', 0, race='dwarf_male', module='outfit', lod='0p4')
        self.assertEqual(status, 200)
        self.assertTrue((self.root / 'models/s_dwarf_male/b/dwarf_male/outfit/chosen_lod.json').is_file())
        self.assertTrue((self.root / 'models/s/pipeline_v2.json').is_file())
        self.assertEqual(self.lod('s', 1, lod='0p1', all=True)[1]['written'], 3)
        self.assertEqual(json.loads((self.root / 'models/s_gnome/g/gnome_female/outfit/chosen_lod.json').read_text())['lod'], '0p1')
        self.assertEqual(self.unit('s', 'dwarf_male')[0]['lod'], '0p1')

    def test_set_name_with_race_suffix_belongs_to_base_set(self):
        self.job('s', 'a', 'processing', (('human_male', 'outfit'),), state='converted')
        self.job('s', 'b', 'processing', (('dwarf_male', 'outfit'),), state='converted', suffix='_dwarf_male')
        self.reduce('s', 'a', 'human_male', 'outfit')
        self.reduce('s_dwarf_male', 'b', 'dwarf_male', 'outfit')
        _, state = self.get('/api/v2/state')
        self.assertEqual([c['id'] for c in state['tabs']['3d']], ['set:s'])

    def test_required_races_come_from_jobs_and_live_fronts_not_from_todays_palettes(self):
        self.job('s', 'a', 'completed', (('human_male', 'outfit'),), state='converted')
        self.reduce('s', 'a', 'human_male', 'outfit')
        self.assertEqual(self.unit('s')[1]['stage'], '3d')
        # a base added after the set was reviewed must not drag it back to Approved
        (self.root / 'base_views' / 'elf_female').mkdir()
        (self.root / 'base_views' / 'elf_female' / 'palette.json').write_text('{}')
        _, item = self.get('/api/v2/item?id=set:s')
        self.assertEqual((item['stage'], item['required_races']), ('3d', ['human_male']))
        self.assertEqual(sorted(n['race'] for n in item['not_converted']), ['dwarf_male', 'elf_female'])
        # a planned front (any state but failed) makes a race required; a failed one does not
        self.fronts('s', 'dwarf_male', 'running')
        self.fronts('s', 'elf_female', 'failed')
        _, item = self.get('/api/v2/item?id=set:s')
        self.assertEqual((item['stage'], item['required_races']), ('approved', ['dwarf_male', 'human_male']))
        self.assertEqual(item['not_converted'], [{'race': 'elf_female', 'reason': 'fronts failed'}])
        # the 3D approval freezes the list
        (self.root / 'models/s').mkdir(parents=True, exist_ok=True)
        (self.root / 'models/s/pipeline_v2.json').write_text(json.dumps({'rev': 2, 'lod_approval': {'units': {}, 'required_races': ['human_male']}}))
        _, item = self.get('/api/v2/item?id=set:s')
        self.assertEqual((item['stage'], item['required_races']), ('aligned', ['human_male']))

    def test_pinned_job_missing_is_an_error_never_a_substitute(self):
        self.full_set('s', 'old')
        self.job('s', 'new', 'completed', (('human_male', 'outfit'),), state='converted')
        self.reduce('s', 'new', 'human_male', 'outfit', mtime=4_000_000_000)
        pin = {'schema': 1, 'rev': 4, 'lod_approval': {'units': {'human_male/outfit': {'job': 'gone', 'lod': '0p2', 'glb_sha256': '0' * 64},
                                                                   'dwarf_male/outfit': {'job': 'old', 'lod': '0p2', 'glb_sha256': self.sha('s', 'old', 'dwarf_male')}}}}
        (self.root / 'models/s/pipeline_v2.json').write_text(json.dumps(pin))
        self.assertEqual(self.unit('s')[1]['stage'], '3d')           # dwarf_male/outfit is approved but not built yet
        self.publish('s', 'old', 'dwarf_male', 'outfit')
        unit, item = self.unit('s')
        self.assertEqual((unit['status'], unit['job'], item['stage']), ('pin_missing', 'gone', 'aligned'))
        self.assertIn('gone', unit['error'])
        self.assertEqual(unit['lods'], {})
        self.assertEqual(self.unit('s', 'dwarf_male')[0]['status'], 'reduced')
        _, state = self.get('/api/v2/state')
        card = state['tabs']['aligned'][0]
        self.assertTrue(any('missing' in b['text'] and b['tone'] == 'error' for b in card['badges']))
        # the user can send the set back to 3D, which clears the pin (and then the newest conversion is shown again)
        status, result = self.act('set:s', 'unapprove', rev=4)
        self.assertEqual((status, result['stage'], result['rev']), (200, '3d', 5))
        unit, item = self.unit('s')
        self.assertEqual((unit['status'], unit['job'], item['locked']), ('reduced', 'new', False))

    def test_retired_pixal3d_jobs_are_not_work(self):
        folder = self.root / 'completed' / 'teal--p1'
        (folder / 'human_male' / 'outfit').mkdir(parents=True)
        (folder / '.pixal3d-job.json').write_text('{"state": "done"}')
        (self.root / 'models' / 'teal' / 'p1').mkdir(parents=True)
        self.full_set('real')
        _, state = self.get('/api/v2/state')
        self.assertEqual([c['id'] for c in state['tabs']['approved']], [])
        self.assertEqual([c['id'] for c in state['tabs']['3d']], ['set:real'])
        report = migrate.build_report(self.root)
        self.assertEqual((report['summary']['sets_with_jobs'], report['summary']['retired_pixal3d_jobs']), (1, 1))
        self.assertEqual(report['retired_pixal3d_jobs'], [{'job': 'completed/teal--p1', 'set': 'teal', 'model_dir': 'models/teal'}])
        self.assertNotIn('teal', report['models_without_job_folder'])
        self.assertIn('## Retired Pixal3D jobs (1', migrate.to_markdown(report))

    def test_mesh_review_warnings_are_surfaced(self):
        extra = {'module_conversion': {'human_male/outfit': {'backend': 'tripo', 'levels_needing_review': ['0p4', '0p2'], 'needs_manual_review': True,
                                                              'input_warning': {'missing_input': 'front_three_quarter'}}}}
        self.job('s', 'a', 'completed', (('human_male', 'outfit'), ('dwarf_male', 'outfit')), state='converted', extra=extra,
                 errors=['human_male/outfit: doubled interior shell (25% of the area is enclosed);'])
        self.reduce('s', 'a', 'human_male', 'outfit')
        self.reduce('s', 'a', 'dwarf_male', 'outfit')
        unit, item = self.unit('s')
        self.assertEqual(unit['status'], 'reduced')
        self.assertIsNone(unit['error'])
        self.assertTrue(unit['warnings'][0].startswith('doubled interior shell'))
        self.assertIn('needs manual review', unit['warnings'])
        self.assertIn('input views incomplete (missing front_three_quarter)', unit['warnings'])
        self.assertEqual(unit['review_levels'], ['0p4', '0p2'])
        self.assertEqual(self.unit('s', 'dwarf_male')[0]['warnings'], [])
        _, state = self.get('/api/v2/state')
        self.assertIn({'text': 'warnings 1', 'tone': 'warn'}, state['tabs']['3d'][0]['badges'])

    def test_thumbnail_kind_render_or_fallback(self):
        self.full_set('s')
        _, state = self.get('/api/v2/state')
        card = state['tabs']['3d'][0]
        self.assertEqual((card['thumbnail_kind'], card['thumbnail'].endswith('front.png')), ('fallback', True))
        glb = self.root / 'models/s/j1/human_male/outfit/reduced/lod_error_0p2.glb'
        glb.with_name('lod_error_0p2.thumb.png').write_bytes(PNG)
        os.utime(glb, (1000, 1000))
        _, state = self.get('/api/v2/state')
        self.assertEqual(state['tabs']['3d'][0]['thumbnail_kind'], 'render')
        self.concept('c')
        _, state = self.get('/api/v2/state')
        self.assertIsNone(state['tabs']['concepts'][0]['thumbnail_kind'])

    def test_aligned_and_completed_tabs(self):
        self.full_set('s')
        (self.root / 'models/s/pipeline_v2.json').write_text(json.dumps({'rev': 1, 'lod_approval': {'units': {}}}))
        _, state = self.get('/api/v2/state')
        self.assertEqual(([c['id'] for c in state['tabs']['aligned']], state['tabs']['3d']), (['set:s'], []))
        (self.root / 'models/s/pipeline_v2.json').write_text(json.dumps({'rev': 2, 'lod_approval': {'units': {}}, 'aligned_approval': {'units': {}}}))
        _, state = self.get('/api/v2/state')
        # an aligned approval that does not name every live unit, its bare builds and a matching ready.json is NOT Completed (phase 3 review F3;
        # the full Completed rule is tested in test_phase3.py)
        self.assertEqual(([c['id'] for c in state['tabs']['aligned']], state['tabs']['completed']), (['set:s'], []))
        item = self.get('/api/v2/item?id=set:s')[1]
        self.assertIn('approved units differ from the live units', item['not_completed'])

    def test_card_kind_and_fresh_thumb(self):
        self.full_set('arm', modules=('body_armor', 'right_gauntlet_boot'))
        self.full_set('out')
        glb = self.root / 'models/arm/j1/human_male/body_armor/reduced/lod_error_0p2.glb'
        thumb = glb.with_name('lod_error_0p2.thumb.png')
        thumb.write_bytes(PNG)
        os.utime(glb, (1000, 1000))
        _, state = self.get('/api/v2/state')
        cards = {c['id']: c for c in state['tabs']['3d']}
        self.assertEqual((cards['set:arm']['kind'], cards['set:out']['kind']), ('armour', 'outfit'))
        self.assertIn('lod_error_0p2.thumb.png?v=', cards['set:arm']['thumbnail'])
        self.assertEqual(state['races'], sorted(RACES))
        _, item = self.get('/api/v2/item?id=set:arm')
        self.assertEqual(item['modules'], ['body_armor', 'right_gauntlet_boot'])


class ConceptTests(V2Case):
    def test_state_concept_cards_and_item(self):
        self.concept('one')
        self.concept('two', 'provisional')
        self.concept('three', 'archive/concepts')
        self.concept('four', 'approved', module='body_armor')
        batch = self.root / 'concepts/batch'
        batch.mkdir()
        (batch / '01_plate__outfit.png').write_bytes(b'png')
        (batch / '01_plate.json').write_text('{"race_gender":"human_male"}')
        _, state = self.get('/api/v2/state')
        tabs = state['tabs']
        self.assertEqual(set(tabs), {'concepts', 'provisional', 'approved', '3d', 'aligned', 'completed', 'review', 'archive', 'trash'})
        by_id = {c['id']: c for cards in tabs.values() for c in cards}
        self.assertEqual(by_id['concepts:one']['actions'], ['provisional', 'approve', 'reject'])
        self.assertEqual(by_id['provisional:two']['actions'], ['approve', 'unapprove'])
        self.assertEqual(by_id['archive:three']['actions'], ['approve', 'unarchive'])
        self.assertEqual(by_id['approved:four']['actions'], ['unapprove'])
        self.assertEqual(by_id['legacy:batch:01_plate']['actions'], ['approve', 'reject'])
        self.assertEqual(by_id['legacy:batch:01_plate']['kind'], 'legacy')
        self.assertEqual(by_id['approved:four']['kind'], 'armour')
        self.assertEqual(by_id['concepts:one']['thumbnail'], '/files/concepts/one/human_male/outfit_front.png')
        status, item = self.get('/api/v2/item?id=concepts:one')
        self.assertEqual(status, 200)
        self.assertEqual(item['detail']['races'][0]['images'], {'outfit': '/files/concepts/one/human_male/outfit_front.png'})
        self.assertEqual(item['detail']['races'][0]['info']['outfit']['attempts'], 2)
        status, item = self.get('/api/v2/item?id=legacy:batch:01_plate')
        self.assertEqual(item['detail']['races'][0]['info']['record']['attempts'], 1)
        self.assertEqual(self.get('/api/v2/item?id=concepts:nothing')[0], 404)
        self.assertEqual(self.get('/api/v2/item?id=concepts:..')[0], 400)

    def test_direct_approve_provisional_and_undo(self):
        self.concept('one')
        status, result = self.act('concepts:one', 'approve')
        self.assertEqual(status, 200)
        self.assertTrue(result['check']['valid'])
        self.assertEqual(result['undo'], {'verb': 'unapprove', 'id': 'approved:one', 'args': {'to': 'concepts'}})
        self.assertTrue((self.root / 'approved/one/human_male/outfit_front.png').exists())
        self.assertFalse((self.root / 'concepts/one').exists())
        self.assertFalse((self.root / 'provisional/one').exists())
        status, back = self.act(**{'id': result['undo']['id'], 'verb': result['undo']['verb'], 'args': result['undo']['args']})
        self.assertEqual(status, 200)
        self.assertTrue((self.root / 'concepts/one').exists())
        status, result = self.act('concepts:one', 'provisional')
        self.assertEqual((status, result['undo']), (200, {'verb': 'unapprove', 'id': 'provisional:one', 'args': {'to': 'concepts'}}))
        self.assertTrue((self.root / 'provisional/one').exists())
        status, result = self.act('provisional:one', 'approve')
        self.assertEqual((status, result['undo']), (200, {'verb': 'unapprove', 'id': 'approved:one'}))
        self.assertTrue((self.root / 'approved/one').exists())
        status, result = self.act('approved:one', 'unapprove')
        self.assertEqual(status, 200)
        self.assertTrue((self.root / 'provisional/one').exists())
        self.assertEqual(result['undo'], {'verb': 'approve', 'id': 'provisional:one'})
        status, result = self.act('provisional:one', 'unapprove')
        self.assertTrue((self.root / 'concepts/one').exists())
        self.assertEqual(result['undo'], {'verb': 'provisional', 'id': 'concepts:one'})
        log = [json.loads(l) for l in (self.root / 'tools/preview/actions.jsonl').read_text().splitlines()]
        self.assertEqual([l['action'] for l in log], ['approve', 'unapprove', 'approve', 'approve', 'unapprove', 'unapprove'])

    def test_direct_approve_refusals_and_warning(self):
        self.concept('one')
        (self.root / 'provisional/one').mkdir()
        self.assertEqual(self.act('concepts:one', 'approve')[0], 400)
        (self.root / 'provisional/one').rmdir()
        (self.root / 'approved/one').mkdir()
        self.assertEqual(self.act('concepts:one', 'approve')[0], 400)
        self.assertTrue((self.root / 'concepts/one').exists())
        (self.root / 'approved/one').rmdir()
        self.concept('bad')
        status, result = self.act('concepts:bad', 'approve')
        self.assertEqual(status, 200)
        self.assertIn('invalid set', result['warning'])
        self.assertTrue((self.root / 'approved/bad').exists())          # the move stands
        self.concept('arch', 'archive/concepts')
        status, result = self.act('archive:arch', 'approve')
        self.assertEqual((status, result['undo']['args']), (200, {'to': 'archive'}))
        self.assertTrue((self.root / 'approved/arch').exists())
        self.act(result['undo']['id'], 'unapprove', args=result['undo']['args'])
        self.assertTrue((self.root / 'archive/concepts/arch').exists())
        for bad in ('concepts:..', 'concepts:../one', 'concepts:one/../../x'):
            self.assertEqual(self.act(bad, 'approve')[0], 400)
        self.assertEqual(self.act('concepts:one', 'explode')[0], 400)
        self.assertEqual(self.act('concepts:one', 'unapprove')[0], 400)
        (self.root / 'approved/claimed').mkdir()
        (self.root / 'approved/claimed/human_male').mkdir()
        (self.root / 'approved/claimed/human_male/outfit_front.png').write_bytes(b'x')
        (self.root / 'approved/claimed/.tripo-job.json').write_text('{}')
        self.assertEqual(self.act('approved:claimed', 'unapprove')[0], 400)

    def test_reject_restore_archive_and_legacy(self):
        self.concept('one')
        status, result = self.act('concepts:one', 'reject')
        self.assertEqual(status, 200)
        self.assertTrue((self.root / 'trash/concepts/one/.preview-origin.json').exists())
        _, state = self.get('/api/v2/state')
        self.assertEqual([(c['id'], c['actions']) for c in state['tabs']['trash']], [('trash:one', ['restore'])])
        status, back = self.act(result['undo']['id'], result['undo']['verb'])
        self.assertEqual(status, 200)
        self.assertTrue((self.root / 'concepts/one').exists())
        status, result = self.act('concepts:one', 'archive')
        self.assertEqual((status, result['undo']), (200, {'verb': 'unarchive', 'id': 'archive:one'}))
        self.assertEqual(self.act('archive:one', 'unarchive')[0], 200)
        batch = self.root / 'concepts/batch'
        batch.mkdir()
        (batch / '01_plate__outfit.png').write_bytes(b'png')
        (batch / '01_plate.json').write_text('{}')
        status, result = self.act('legacy:batch:01_plate', 'reject')
        self.assertEqual(status, 200)
        self.assertEqual(self.act(result['undo']['id'], 'restore')[0], 200)
        self.assertTrue((batch / '01_plate__outfit.png').exists())
        status, result = self.act('legacy:batch:01_plate', 'approve')
        self.assertEqual(status, 200)
        self.assertTrue((self.root / 'approved/batch_01_plate').exists())
        self.assertEqual(self.act('legacy:batch:01_plate', 'provisional')[0], 400)


class SecurityTests(V2Case):
    def test_token_and_origin(self):
        self.concept('one')
        body = {'id': 'concepts:one', 'verb': 'reject'}
        self.assertEqual(self.post('/api/v2/action', body, token=False)[0], 403)
        self.assertEqual(self.post('/api/v2/action', body, token='wrong')[0], 403)
        host = f'127.0.0.1:{self.server.server_port}'
        self.assertEqual(self.post('/api/v2/action', body, headers={'Origin': 'http://evil.example'})[0], 403)
        self.assertEqual(self.post('/api/v2/action', body, headers={'Origin': 'null'})[0], 403)
        self.assertEqual(self.post('/api/v2/lod', {'id': 'set:x'}, token=False)[0], 403)
        self.assertEqual(self.raw('POST', '/api/v2/thumbnail?url=x', PNG, {'Content-Type': 'image/png'})[0], 403)
        self.assertTrue((self.root / 'concepts/one').exists())
        status, result = self.post('/api/v2/action', body, headers={'Origin': f'http://{host}'})
        self.assertEqual(status, 200)
        self.assertEqual(self.raw('POST', '/api/v2/nothing', b'{}', {'X-Token': self.token})[0], 404)

    def test_index_token_and_static(self):
        status, html = self.get('/')
        self.assertEqual(status, 200)
        self.assertIn(f'<meta name="v2-token" content="{self.token}">'.encode(), html)
        _, head, _ = self.raw('GET', '/main.js')
        self.assertTrue(head['Content-Type'].startswith('text/javascript'))
        self.assertEqual(self.get('/vendor/three.module.js')[1], b'//three')
        for bad in ('/secret.py', '/../project/tools/preview/actions.jsonl', '/%2e%2e/project/tools/tripo_pipeline/workflow.py', '/nothing.js'):
            self.assertEqual(self.get(bad)[0], 404, bad)
        self.assertEqual(server.inject_token('<html><head></head></html>', 'T'), '<html><head><meta name="v2-token" content="T"></head></html>')

    def test_files_traversal_and_rules(self):
        self.concept('one')
        outside = self.root.parent / 'v2_outside.png'
        outside.write_bytes(b'outside')
        (self.root / 'concepts/leak.png').symlink_to(outside)
        (self.root / 'concepts/notes.txt').write_text('hi')
        try:
            self.assertEqual(self.get('/files/concepts/leak.png')[0], 403)
            self.assertEqual(self.get('/files/%2e%2e/%2e%2e/etc/passwd')[0], 403)
            self.assertEqual(self.get('/files/%2e%2e/v2_outside.png')[0], 403)
            self.assertEqual(self.get('/files/concepts/notes.txt')[0], 403)
            self.assertEqual(self.get('/files/concepts/one/human_male/missing.png')[0], 404)        # permitted but absent
            status, data = self.get('/files/concepts/one/human_male/outfit_front.png')
            self.assertEqual((status, data), (200, b'png'))
            _, head, _ = self.raw('GET', '/files/concepts/one/human_male/outfit_front.png')
            self.assertEqual(head['Cache-Control'], 'private, max-age=3600')
            self.assertEqual(self.get('/mfiles/../../x.png')[0], 403)
        finally:
            outside.unlink()

    def test_gzip_over_4kb(self):
        for i in range(60):
            self.concept(f'concept_number_{i:03d}')
        status, head, data = self.raw('GET', '/api/v2/state', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual((status, head.get('Content-Encoding')), (200, 'gzip'))
        self.assertEqual(len(json.loads(gzip.decompress(data))['tabs']['concepts']), 60)
        self.assertEqual(self.raw('GET', '/api/v2/item?id=set:nope')[0], 404)


class DecisionTests(V2Case):
    def test_drop_race_moves_stuck_set_to_3d_and_rev_conflict(self):
        self.job('s', 'a', 'processing', (('human_male', 'outfit'), ('dwarf_male', 'outfit')), errors=['dwarf_male/outfit: broken'], state='partially_converted')
        self.reduce('s', 'a', 'human_male', 'outfit')
        unit, item = self.unit('s', 'dwarf_male')
        self.assertEqual((unit['status'], item['stage'], item['rev']), ('failed', 'approved', 0))
        status, result = self.act('set:s', 'drop_race', rev=0, args={'race': 'dwarf_male'})
        self.assertEqual((status, result['rev'], result['stage']), (200, 1, '3d'))
        self.assertEqual(result['undo'], {'verb': 'undrop_race', 'id': 'set:s', 'args': {'race': 'dwarf_male'}, 'rev': 1})      # the undo carries the returned rev
        record = json.loads((self.root / 'models/s/pipeline_v2.json').read_text())
        self.assertEqual((record['rev'], list(record['dropped']), record['history'][0]['action']), (1, ['dwarf_male'], 'drop_race'))
        unit, item = self.unit('s', 'dwarf_male')
        self.assertEqual((unit['status'], item['stage']), ('dropped', '3d'))
        _, state = self.get('/api/v2/state')
        self.assertEqual([c['id'] for c in state['tabs']['3d']], ['set:s'])
        self.assertEqual(state['tabs']['3d'][0]['races'], ['human_male'])
        # stale rev -> 409 with the current item
        status, result = self.act('set:s', 'undrop_race', rev=0, args={'race': 'dwarf_male'})
        self.assertEqual(status, 409)
        self.assertEqual((result['rev'], result['item']['rev']), (1, 1))
        self.assertEqual(self.act('set:s', 'undrop_race', args={'race': 'dwarf_male'})[0], 400)         # rev required
        self.assertEqual(self.act('set:s', 'drop_race', rev=1, args={'race': 'dwarf_male'})[0], 400)    # already dropped
        self.assertEqual(self.act('set:s', 'drop_race', rev=1, args={'race': 'martian'})[0], 400)
        self.assertEqual(self.act('set:s', 'drop_race', rev=1, args={'race': 'human_male'})[0], 400)    # cannot drop every race
        status, result = self.act('set:s', 'undrop_race', rev=1, args={'race': 'dwarf_male'})
        self.assertEqual((status, result['stage'], result['rev']), (200, 'approved', 2))
        self.assertEqual(self.act('set:s', 'approve', rev=2)[0], 400)             # phase 3 verbs do not exist yet
        self.assertEqual(self.act('set:s', 'rebuild', rev=2)[0], 400)
        self.assertTrue(list((self.root / 'models/s').glob('*.tmp')) == [])

    def test_lod_single_all_and_stale_rev(self):
        self.full_set('s', modules=('body_armor', 'right_gauntlet_boot'))
        status, result = self.lod('s', 0, race='human_male', module='body_armor', lod='0p4')
        self.assertEqual((status, result['rev']), (200, 1))              # every LOD write bumps the set's rev
        record = json.loads((self.root / 'models/s/j1/human_male/body_armor/chosen_lod.json').read_text())
        self.assertEqual((record['lod'], record['reduced'], record['wearable']), ('0p4', 'reduced/lod_error_0p4.glb', 'reduced/lod_error_0p4__wearable.glb'))
        self.assertEqual(json.loads((self.root / 'models/s/pipeline_v2.json').read_text())['rev'], 1)
        unit, item = self.unit('s', 'human_male', 'body_armor')
        self.assertEqual((unit['lod'], unit['lod_saved'], item['rev']), ('0p4', True, 1))
        for bad in ({'race': 'human_male', 'module': 'body_armor', 'lod': '0p3'}, {'race': '..', 'module': 'body_armor', 'lod': '0p2'},
                    {'race': 'human_male', 'module': 'outfit', 'lod': '0p2'}):
            self.assertEqual(self.post('/api/v2/lod', {'id': 'set:s', 'rev': 1, 'job': 'j1'} | bad)[0], 400)
        status, result = self.lod('s', 1, lod='0p1', all=True)
        self.assertEqual((status, result['written'], result['rev']), (200, 4, 2))
        self.assertEqual(result['previous']['human_male/body_armor'], '0p4')
        self.assertIsNone(result['previous']['dwarf_male/body_armor'])
        for race in RACES:
            for module in ('body_armor', 'right_gauntlet_boot'):
                self.assertEqual(json.loads((self.root / f'models/s/j1/{race}/{module}/chosen_lod.json').read_text())['lod'], '0p1')
        # undo through `units` with the returned previous map and rev, null removes a pick
        status, undone = self.lod('s', 2, units=result['previous'])
        self.assertEqual((status, undone['rev']), (200, 3))
        self.assertEqual(self.unit('s', 'human_male', 'body_armor')[0]['lod'], '0p4')
        self.assertFalse((self.root / 'models/s/j1/dwarf_male/body_armor/chosen_lod.json').exists())
        self.assertEqual(self.unit('s', 'dwarf_male', 'body_armor')[0]['lod_saved'], False)       # an absent pick stays absent
        # stale rev (two tabs): the second write at the old rev is a 409, nothing is overwritten
        status, result = self.lod('s', 3, race='human_male', module='body_armor', lod='0p2')
        self.assertEqual(status, 200)
        status, result = self.lod('s', 3, race='human_male', module='body_armor', lod='0p1')
        self.assertEqual((status, result['code'], result['rev']), (409, 'rev', 4))
        self.assertEqual(self.unit('s', 'human_male', 'body_armor')[0]['lod'], '0p2')
        self.assertEqual(self.post('/api/v2/lod', {'id': 'set:s', 'lod': '0p2', 'all': True, 'jobs': {}})[0], 400)       # rev required

    def test_lod_write_is_bound_to_the_displayed_job(self):
        self.full_set('s', 'old')
        self.job('s', 'new', 'completed', (('human_male', 'outfit'),), state='converted')
        self.reduce('s', 'old', 'human_male', 'outfit', mtime=1000)
        _, item = self.get('/api/v2/item?id=set:s')
        shown = item['units'][0]['job']
        self.assertEqual(shown, 'old')
        self.reduce('s', 'new', 'human_male', 'outfit', mtime=2000)       # a newer reduction lands while the tab is open
        body = {'id': 'set:s', 'rev': 0, 'race': 'human_male', 'module': 'outfit', 'lod': '0p1'}
        status, result = self.post('/api/v2/lod', body | {'job': shown})
        self.assertEqual((status, result['code']), (409, 'job'))
        self.assertFalse(list((self.root / 'models/s').glob('*/*/outfit/chosen_lod.json')))
        self.assertEqual(self.post('/api/v2/lod', body)[0], 400)          # a write without the displayed job is refused
        status, result = self.post('/api/v2/lod', body | {'job': 'new'})
        self.assertEqual(status, 200)
        self.assertTrue((self.root / 'models/s/new/human_male/outfit/chosen_lod.json').is_file())
        # All / restore need the displayed job of every unit
        status, result = self.post('/api/v2/lod', {'id': 'set:s', 'rev': 1, 'lod': '0p1', 'all': True, 'jobs': {'human_male/outfit': 'old', 'dwarf_male/outfit': 'j1'}})
        self.assertEqual((status, result['code']), (409, 'job'))

    def test_lod_is_locked_after_the_3d_approval(self):
        self.full_set('s')
        pin = {'schema': 1, 'rev': 2, 'lod_approval': {'units': {'human_male/outfit': {'job': 'j1', 'lod': '0p2'}}}}
        (self.root / 'models/s/pipeline_v2.json').write_text(json.dumps(pin))
        status, result = self.lod('s', 2, race='human_male', module='outfit', lod='0p1')
        self.assertEqual((status, result['code']), (409, 'locked'))
        self.assertIn('send it back to 3D', result['error'])
        self.assertEqual(self.lod('s', 2, lod='0p1', all=True)[0], 409)
        self.assertEqual(self.lod('s', 2, units={'human_male/outfit': None})[0], 409)
        self.assertFalse((self.root / 'models/s/j1/human_male/outfit/chosen_lod.json').exists())
        self.assertEqual(json.loads((self.root / 'models/s/pipeline_v2.json').read_text())['rev'], 2)
        # sending it back to 3D unlocks it
        self.assertEqual(self.act('set:s', 'unapprove', rev=2)[0], 200)
        self.assertEqual(self.lod('s', 3, race='human_male', module='outfit', lod='0p1')[0], 200)

    def test_bulk_lod_write_is_transactional_for_all_and_undo(self):
        self.full_set('s')
        self.assertEqual(self.lod('s', 0, race='human_male', module='outfit', lod='0p4')[0], 200)
        self.assertEqual(self.lod('s', 1, race='dwarf_male', module='outfit', lod='0p4')[0], 200)
        picks = lambda: {r: json.loads((self.root / f'models/s/j1/{r}/outfit/chosen_lod.json').read_text())['lod'] for r in RACES}   # noqa: E731
        before = {p: p.read_bytes() for p in (self.root / 'models/s').rglob('*.json')}
        real = guard.replace_retry
        calls = []

        def second_fails(src, dst, retries=8):
            calls.append(dst)
            if len(calls) == 3:                      # call 1 writes the intent file, 2 replaces the first target, 3 the second
                raise OSError('disk full')
            return real(src, dst, retries)
        for body in ({'lod': '0p1', 'all': True}, {'units': {'human_male/outfit': '0p1', 'dwarf_male/outfit': None}}):
            calls.clear()
            with mock.patch.object(guard, 'replace_retry', second_fails):
                status, result = self.lod('s', 2, **body)
            self.assertEqual(status, 400, result)
            self.assertEqual(len(calls), 3 + 2 + 1, calls)       # the failure came after one replacement and the rollback restored through replace_retry
            self.assertEqual(picks(), {'human_male': '0p4', 'dwarf_male': '0p4'})
            self.assertEqual({p: p.read_bytes() for p in (self.root / 'models/s').rglob('*.json')}, before)      # incl. pipeline_v2.json rev
            self.assertEqual(list(self.root.rglob('*.tmp')), [])
            self.assertEqual(guard.list_intents(self.root), [])
        self.assertEqual(self.lod('s', 2, lod='0p1', all=True)[0], 200)

    def test_interrupted_transaction_is_rolled_back_at_the_next_start(self):
        self.full_set('s')
        self.assertEqual(self.lod('s', 0, race='human_male', module='outfit', lod='0p4')[0], 200)
        before = {p: p.read_bytes() for p in (self.root / 'models/s').rglob('*.json')}
        real = guard.replace_retry
        calls = []

        def die(src, dst, retries=8):
            calls.append(dst)
            if len(calls) == 3:
                raise OSError('power cut')
            return real(src, dst, retries)
        with mock.patch.object(guard, 'replace_retry', die), mock.patch.object(guard, 'restore_files', side_effect=OSError('still broken')):
            self.assertEqual(self.lod('s', 1, lod='0p1', all=True)[0], 400)
        self.assertNotEqual({p: p.read_bytes() for p in (self.root / 'models/s').rglob('*.json')}, before)       # half done
        intents = guard.list_intents(self.root)
        self.assertEqual([i['kind'] for i in intents], ['files_tx'])
        _, state = self.get('/api/v2/state')
        self.assertEqual([(c['kind'], c['actions']) for c in state['tabs']['trash']], [('recovery', ['rollback'])])
        notes = actions.recover_at_start(self.root)              # what create_server runs for the writer
        self.assertEqual(len(notes), 1)
        self.assertEqual({p: p.read_bytes() for p in (self.root / 'models/s').rglob('*.json')}, before)
        self.assertEqual(guard.list_intents(self.root), [])

    def test_unreadable_decisions_are_never_replaced_by_defaults(self):
        self.full_set('s')
        path = self.root / 'models/s/pipeline_v2.json'
        for content in (b'{"schema": 1, "rev": 7, "dropped": {"human_ma', b'[1, 2]', b'{"schema": 2, "rev": 1}', b'{"rev": "x"}'):
            path.write_bytes(content)
            unit, item = self.unit('s')
            self.assertIsNotNone(item['error'], content)
            self.assertEqual(item['rev'], 0)
            _, state = self.get('/api/v2/state')
            self.assertIn({'text': 'decisions unreadable', 'tone': 'error'}, state['tabs']['3d'][0]['badges'])
            status, result = self.act('set:s', 'drop_race', rev=0, args={'race': 'dwarf_male'})
            self.assertEqual((status, result['code']), (409, 'unreadable'))
            self.assertEqual(self.lod('s', 0, race='human_male', module='outfit', lod='0p1')[0], 409)
            self.assertEqual(path.read_bytes(), content)
            self.assertFalse((self.root / 'models/s/j1/human_male/outfit/chosen_lod.json').exists())
        path.write_text(json.dumps({'schema': 1, 'rev': 7}))        # fixed: mutations work again
        self.assertEqual(self.act('set:s', 'drop_race', rev=7, args={'race': 'dwarf_male'})[0], 200)

    def test_malformed_nested_records_and_invalid_utf8_give_an_unreadable_card(self):
        """Phase 1 review #10: nested approval data is validated and a decoding error is an unreadable card, never a broken derivation."""
        self.full_set('s')
        path = self.root / 'models/s/pipeline_v2.json'
        for raw in (json.dumps({'rev': 1, 'lod_approval': {'units': {'human_male/outfit': 'x'}}}),
                    json.dumps({'rev': 1, 'lod_approval': {'units': {'human_male/outfit': {'job': 'j1', 'lod': '0p9'}}}}),
                    json.dumps({'rev': 1, 'lod_approval': {'units': [1]}}),
                    json.dumps({'rev': 1, 'amount': {'human_male': {'outfit': 'lots'}}}),
                    json.dumps({'rev': 1, 'aligned_approval': {'units': {'human_male/outfit': {'job': 'j1', 'lod': '0p2'}}}}),
                    json.dumps({'rev': 1, 'dropped': {'human_male': 3}})):
            path.write_text(raw)
            status, state = self.get('/api/v2/state')
            self.assertEqual(status, 200, raw)
            card = next(c for c in sum(state['tabs'].values(), []) if c['id'] == 'set:s')
            self.assertIn('decisions unreadable', [b['text'] for b in card['badges']], raw)
            self.assertTrue(self.get('/api/v2/item?id=set:s')[1]['error'], raw)
            self.assertEqual(self.act('set:s', 'drop_race', rev=1, args={'race': 'dwarf_male'})[1]['code'], 'unreadable', raw)
        path.write_bytes(b'{"rev": 1, "note": "' + bytes([0xff, 0xfe]) + b'"}')
        status, item = self.get('/api/v2/item?id=set:s')
        self.assertEqual(status, 200)
        self.assertIn('UTF-8', item['error'])

    def test_set_verbs_check_the_set_before_creating_anything(self):
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        self.assertEqual(self.act('set:ghost', 'drop_race', rev=0, args={'race': 'human_male'})[0], 404)
        self.assertEqual(self.post('/api/v2/lod', {'id': 'set:ghost', 'rev': 0, 'race': 'human_male', 'module': 'outfit', 'lod': '0p1', 'job': 'x'})[0], 404)
        self.assertEqual(sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')), before)
        self.assertFalse((self.root / 'models/ghost').exists())

    def test_race_undo_carries_the_returned_rev_and_the_drop_metadata(self):
        self.full_set('s')
        status, dropped = self.act('set:s', 'drop_race', rev=0, args={'race': 'dwarf_male', 'reason': 'no gnome sleeves'})
        self.assertEqual((status, dropped['undo']['rev']), (200, 1))
        stored = json.loads((self.root / 'models/s/pipeline_v2.json').read_text())['dropped']['dwarf_male']
        status, restored = self.act('set:s', 'undrop_race', rev=1, args={'race': 'dwarf_male'})
        self.assertEqual((status, restored['undo']['rev'], restored['undo']['args']['meta']), (200, 2, stored))
        # a later decision moved the rev on: the undo is a conflict, it never silently reverses the newer decision
        self.assertEqual(self.act('set:s', 'drop_race', rev=2, args={'race': 'human_male'})[0], 200)
        status, result = self.act(**{'id': restored['undo']['id'], 'verb': restored['undo']['verb'], 'rev': restored['undo']['rev'], 'args': restored['undo']['args']})
        self.assertEqual(status, 409)
        # at the right rev the undo restores the same time and reason
        self.assertEqual(self.act('set:s', 'undrop_race', rev=3, args={'race': 'human_male'})[0], 200)
        status, again = self.act('set:s', 'drop_race', rev=4, args=restored['undo']['args'])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads((self.root / 'models/s/pipeline_v2.json').read_text())['dropped']['dwarf_male'], stored)

    def test_action_allowlist_blocks_old_site_verbs(self):
        self.full_set('s')
        marker = self.root / 'models/s/model_approval.json'
        marker.write_text('{"approved": true}')
        for item_id, verb in (('approvedmodel:s', 'unapprove'), ('model:s', 'approve'), ('approvedmodel:s', 'restore'), ('trash:s', 'approve'),
                              ('set:s', 'approve'), ('set:s', 'reject'), ('monster:Horse/1', 'reject'), ('review:x', 'restore'), ('intent:x', 'approve')):
            status, result = self.act(item_id, verb)
            self.assertEqual(status, 400, (item_id, verb))
        self.assertTrue(marker.is_file())
        self.assertFalse(list(self.root.glob('models/s/*.json.bak')))

    def test_lod_all_is_all_or_nothing(self):
        self.full_set('s', modules=('outfit',))
        (self.root / 'models/s/j1/dwarf_male/outfit/reduced/lod_error_0p1.glb').unlink()
        writes = [('j1', 'human_male', 'outfit', '0p1'), ('j1', 'dwarf_male', 'outfit', '0p1')]
        writes = [('s',) + w for w in writes]
        with self.assertRaises(ValueError) as caught:
            decisions.write_lods(self.root, 's', 0, writes)
        self.assertIn('dwarf_male/outfit', str(caught.exception))
        self.assertFalse((self.root / 'models/s/j1/human_male/outfit/chosen_lod.json').exists())
        status, result = self.lod('s', 0, lod='0p1', all=True)   # dwarf is no longer reduced: only human is written
        self.assertEqual((status, result['written']), (200, 1))

    def test_atomic_write_retries_permission_error(self):
        calls = []
        real = os.replace

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError('busy')
            return real(src, dst)
        guard.os.replace = flaky
        try:
            decisions.atomic_write_json(self.root / 'x.json', {'a': 1})
        finally:
            guard.os.replace = real
        self.assertEqual((len(calls), json.loads((self.root / 'x.json').read_text())), (3, {'a': 1}))
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_concurrent_mutations_serialise(self):
        results = []

        def run(race):
            try:
                decisions.mutate(self.root, 's', None, 'drop_race', {'race': race}, lambda d: d['dropped'].update({race: {}}))
                results.append(race)
            except Exception as exc:       # pragma: no cover
                results.append(exc)
        threads = [threading.Thread(target=run, args=(f'race_{i}',)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        data = decisions.load(self.root, 's')
        self.assertEqual((data['rev'], len(data['dropped']), len(data['history'])), (8, 8, 8))

    def test_thumbnail_post(self):
        self.full_set('s')
        url = '/files/models/s/j1/human_male/outfit/reduced/lod_error_0p2.glb'
        status, _h, data = self.raw('POST', f'/api/v2/thumbnail?url={url}', PNG, {'X-Token': self.token, 'Content-Type': 'image/png'})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)['ok'])
        self.assertTrue((self.root / 'models/s/j1/human_male/outfit/reduced/lod_error_0p2.thumb.png').is_file())
        _, state = self.get('/api/v2/state')
        self.assertIn('lod_error_0p2.thumb.png?v=', state['tabs']['3d'][0]['thumbnail'])
        status, _h, data = self.raw('POST', f'/api/v2/thumbnail?url={url}', b'not a png', {'X-Token': self.token})
        self.assertEqual(status, 400)
        status, _h, data = self.raw('POST', '/api/v2/thumbnail?url=/files/concepts/x.glb', PNG, {'X-Token': self.token})
        self.assertEqual(status, 400)


class MonsterTests(V2Case):
    def test_monster_cards_stage_and_lod(self):
        self.monster('Horse', '1', lods=('0p4', '0p2'))
        self.monster('Horse', '2', lods=('0p4',), approved=True, complete=True)
        self.monster('Horse', '3', lods=(), image=True)
        _, state = self.get('/api/v2/state')
        cards = {c['id']: c for tab, cs in state['tabs'].items() for c in cs if c['type'] == 'monster'}
        self.assertEqual({k: (v['stage'], v['kind']) for k, v in cards.items()},
                         {'monster:Horse/1': ('3d', 'monster'), 'monster:Horse/2': ('completed', 'monster'), 'monster:Horse/3': ('approved', 'monster')})
        card = cards['monster:Horse/1']
        self.assertTrue(card['thumbnail'].startswith('/mfiles/pipeline/Horse/views/1/front.png'))
        self.assertTrue(card['model'].startswith('/mfiles/pipeline/Horse/models/1/reduced/lod_error_0p4.glb'))
        self.assertEqual((card['races'], card['actions']), ([], []))
        status, item = self.get('/api/v2/item?id=monster:Horse/1')
        self.assertEqual((status, item['stage'], sorted(item['lods'])), (200, '3d', ['0p2', '0p4']))
        status, _ = self.post('/api/v2/lod', {'id': 'monster:Horse/1', 'lod': '0p2'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads((self.mroot / 'pipeline/Horse/models/1/chosen_lod.json').read_text())['lod'], '0p2')
        self.assertEqual(self.post('/api/v2/lod', {'id': 'monster:Horse/1', 'lod': '0p6'})[0], 400)
        self.assertEqual(self.get('/mfiles/pipeline/Horse/views/1/front.png')[1], b'png')
        self.assertEqual(self.get('/mfiles/pipeline/Horse/views/1/colours.json')[0], 403)      # not a served suffix
        self.assertEqual(self.get('/mfiles/pipeline/Horse/views/1/missing.png')[0], 404)
        self.assertEqual(self.get('/api/v2/item?id=monster:Horse/9')[0], 404)


class MoveSafetyTests(V2Case):
    def legacy(self):
        batch = self.root / 'concepts/batch'
        batch.mkdir(exist_ok=True)
        (batch / '01_plate__outfit.png').write_bytes(b'png')
        (batch / '01_plate.json').write_text('{"race_gender": "human_male"}')
        return batch

    def test_unapprove_never_moves_a_folder_a_claim_owns(self):
        self.concept('one', 'approved')
        with claim_lock(self.root, 'one'):                      # workflow.py claim holds this for its whole run
            for body in ({}, {'args': {'to': 'concepts'}}, {'args': {'to': 'archive'}}):
                status, result = self.act('approved:one', 'unapprove', **body)
                self.assertEqual((status, result['code']), (409, 'busy'), body)
                self.assertIn('being claimed', result['error'])
            self.assertTrue((self.root / 'approved/one').exists())
        # the claim has written its snapshot but not yet its record: still refused (this was the verified race)
        (self.root / 'approved/one/.base_views/human_male').mkdir(parents=True)
        for body in ({}, {'args': {'to': 'concepts'}}):
            status, result = self.act('approved:one', 'unapprove', **body)
            self.assertEqual(status, 400, body)
            self.assertIn('claimed', result['error'])
        self.assertTrue((self.root / 'approved/one/.base_views').exists())
        shutil.rmtree(self.root / 'approved/one/.base_views')
        self.assertEqual(self.act('approved:one', 'unapprove', args={'to': 'concepts'})[0], 200)
        self.assertTrue((self.root / 'concepts/one').exists())

    def test_undo_of_unapprove_returns_the_set_to_approved(self):
        for origin, folder in (('concepts', 'concepts'), ('archive', 'archive/concepts')):
            name = f'from_{origin}'
            self.concept(name, 'approved')
            status, result = self.act(f'approved:{name}', 'unapprove', args={'to': origin})
            self.assertEqual((status, result['undo']), (200, {'verb': 'approve', 'id': f'{origin}:{name}'}))
            self.assertTrue((self.root / folder / name).exists())
            status, back = self.act(**result['undo'])
            self.assertEqual(status, 200)
            self.assertTrue((self.root / 'approved' / name).exists())            # approved again, not provisional
            self.assertFalse((self.root / 'provisional' / name).exists())

    def test_undo_of_legacy_approve_withdraws_the_exact_promoted_set(self):
        batch = self.legacy()
        status, result = self.act('legacy:batch:01_plate', 'approve')
        self.assertEqual(status, 200)
        self.assertTrue((self.root / 'approved/batch_01_plate').is_dir())
        self.assertFalse((batch / '01_plate__outfit.png').exists())
        self.assertNotIn('\\', json.dumps(result))                              # POSIX paths in the response
        sidecar = json.loads((self.root / 'trash/concepts/batch/01_plate.preview-origin.json').read_text())
        self.assertEqual(sidecar['approved_set'], 'batch_01_plate')
        self.assertEqual(result['undo'], {'verb': 'restore', 'id': 'trashlegacy:batch:01_plate'})
        status, back = self.act(result['undo']['id'], 'restore')
        self.assertEqual((status, back['withdrawn']), (200, 'withdrawn'))
        self.assertFalse((self.root / 'approved/batch_01_plate').exists())          # the processing request is gone ...
        self.assertTrue((batch / '01_plate__outfit.png').exists())                   # ... and the flat entry is back
        self.assertTrue(list((self.root / '.codex_jobs/_app/withdrawn').glob('batch_01_plate*')))      # kept, not deleted
        self.assertEqual(guard.list_intents(self.root), [])

    def test_undo_of_legacy_approve_after_claim_changes_nothing(self):
        batch = self.legacy()
        self.assertEqual(self.act('legacy:batch:01_plate', 'approve')[0], 200)
        approved = self.root / 'approved/batch_01_plate'

        def untouched():
            self.assertTrue(approved.is_dir() or (self.root / 'processing_views/batch_01_plate--abc').is_dir() or (self.root / 'provisional/batch_01_plate').is_dir())
            self.assertFalse((batch / '01_plate__outfit.png').exists())
            self.assertTrue((self.root / 'trash/concepts/batch/01_plate__outfit.png').exists())
        with claim_lock(self.root, 'batch_01_plate'):                              # claim running
            status, result = self.act('trashlegacy:batch:01_plate', 'restore')
            self.assertEqual((status, result['code']), (409, 'busy'))
        untouched()
        (approved / '.base_views').mkdir()                                          # claim started (snapshot first)
        status, result = self.act('trashlegacy:batch:01_plate', 'restore')
        self.assertEqual(status, 400)
        self.assertIn('already been claimed', result['error'])
        untouched()
        (approved / '.base_views').rmdir()
        approved.rename(self.root / 'processing_views/batch_01_plate--abc')         # claim finished: the folder is a job now
        status, result = self.act('trashlegacy:batch:01_plate', 'restore')
        self.assertEqual(status, 400)
        self.assertIn('already been claimed', result['error'])
        untouched()
        (self.root / 'processing_views/batch_01_plate--abc').rename(self.root / 'provisional/batch_01_plate')      # moved on by hand
        status, result = self.act('trashlegacy:batch:01_plate', 'restore')
        self.assertEqual(status, 400)
        self.assertIn('moved on', result['error'])
        untouched()
        self.assertEqual(guard.list_intents(self.root), [])

    def test_a_claim_right_after_the_promotion_finds_the_mark_already_there(self):
        """Re-check R7 (the reviewer's reproduction): a claim takes approved/<set> the moment promote published it. The promotion id is already in
        the folder (written into promote's staging folder) and nothing recreates approved/<set> afterwards."""
        self.legacy()
        real_run = actions.subprocess.run
        seen = []

        def run_then_claim(command, *args, **kwargs):
            out = real_run(command, *args, **kwargs)
            if 'promote' in command and out.returncode == 0:
                src = self.root / 'approved/batch_01_plate'
                seen.append(sorted(p.name for p in src.iterdir()))
                src.rename(self.root / 'processing_views/batch_01_plate--abc')            # the supervisor claims it at once
            return out
        with mock.patch.object(actions.subprocess, 'run', side_effect=run_then_claim):
            status, body = self.act('legacy:batch:01_plate', 'approve')
        self.assertEqual(status, 200, body)
        self.assertEqual(seen, [['.preview-promotion.json', 'human_male']])
        self.assertFalse((self.root / 'approved/batch_01_plate').exists())                 # never recreated with only the mark
        mark = json.loads((self.root / 'processing_views/batch_01_plate--abc' / actions.PROMOTION_MARK).read_text())
        sidecar = json.loads((self.root / 'trash/concepts/batch/01_plate.preview-origin.json').read_text())
        self.assertEqual(mark['promotion_id'], sidecar['promotion_id'])
        self.assertEqual(guard.list_intents(self.root), [])

    def test_a_pre_fix_sidecar_or_intent_never_withdraws_anything(self):
        """Re-check R7: withdrawal needs positive evidence. An Undo entry or an interrupted approve recorded before promotions carried an id
        fails with "manual check needed" and moves nothing."""
        self.legacy()
        self.assertEqual(self.act('legacy:batch:01_plate', 'approve')[0], 200)
        approved = self.root / 'approved/batch_01_plate'
        sidecar_path = self.root / 'trash/concepts/batch/01_plate.preview-origin.json'
        sidecar = json.loads(sidecar_path.read_text())
        sidecar.pop('promotion_id')
        sidecar_path.write_text(json.dumps(sidecar))
        status, result = self.act('trashlegacy:batch:01_plate', 'restore')
        self.assertEqual(status, 400)
        self.assertIn('manual check needed', result['error'])
        self.assertTrue((approved / actions.PROMOTION_MARK).is_file())                       # untouched
        self.assertFalse((self.root / 'concepts/batch/01_plate__outfit.png').exists())
        # an interrupted legacy approve of the old format (no promotion id): Roll back refuses too
        intent = guard.new_intent(self.root, {'kind': 'flat_move', 'verb': 'approve', 'label': 'approve 01_plate', 'files': [],
                                              'approved_set': 'batch_01_plate', 'promoted': True, 'sidecar': None})
        guard.abandon_intent(intent)
        status, result = self.act(f'intent:{intent}', 'rollback')
        self.assertEqual(status, 400)
        self.assertIn('manual check needed', result['error'])
        self.assertTrue(approved.is_dir())

    def test_an_old_undo_never_withdraws_a_later_promotion_of_the_same_name(self):
        """Phase 1 review #3: the promotion id of the sidecar must match the mark in approved/<set>/."""
        self.legacy()
        self.assertEqual(self.act('legacy:batch:01_plate', 'approve')[0], 200)
        approved = self.root / 'approved/batch_01_plate'
        sidecar = json.loads((self.root / 'trash/concepts/batch/01_plate.preview-origin.json').read_text())
        mark = json.loads((approved / actions.PROMOTION_MARK).read_text())
        self.assertEqual(sidecar['promotion_id'], mark['promotion_id'])
        # the first promotion went away and a LATER one of the same name took its place
        approved.rename(self.root / 'elsewhere')
        approved.mkdir()
        (approved / actions.PROMOTION_MARK).write_text(json.dumps({'promotion_id': 'a-later-one'}))
        status, result = self.act('trashlegacy:batch:01_plate', 'restore')
        self.assertEqual(status, 400)
        self.assertIn('not the promotion this entry made', result['error'])
        self.assertTrue((approved / actions.PROMOTION_MARK).is_file())               # untouched
        (approved / actions.PROMOTION_MARK).unlink()                                 # a promotion without any mark is not withdrawn either
        self.assertEqual(self.act('trashlegacy:batch:01_plate', 'restore')[0], 400)
        self.assertTrue(approved.is_dir())

    def test_complete_of_an_interrupted_legacy_approve_whose_promotion_was_claimed(self):
        """Phase 1 review #4: the promotion was claimed by the supervisor after the flat move was interrupted: Complete finishes the move."""
        batch = self.legacy()
        real = shutil.move

        def only_first(src, dst):
            if not getattr(only_first, 'used', False):
                only_first.used = True
                return real(src, dst)
            raise OSError('disk went away')
        with mock.patch.object(actions.shutil, 'move', only_first), mock.patch.object(actions, '_rollback_flat', side_effect=OSError('rollback failed')):
            self.assertEqual(self.act('legacy:batch:01_plate', 'approve')[0], 400)
        intent = guard.list_intents(self.root)[0]
        self.assertTrue(intent['promoted'] and intent['promotion_id'])
        (self.root / 'approved/batch_01_plate').rename(self.root / 'processing_views/batch_01_plate--abc')     # the claim moved it on
        status, result = self.act(f'intent:{intent["id"]}', 'complete')
        self.assertEqual(status, 200, result)
        self.assertEqual(guard.list_intents(self.root), [])
        self.assertEqual(sorted(p.name for p in batch.iterdir()), [])
        self.assertTrue((self.root / 'trash/concepts/batch/01_plate.preview-origin.json').is_file())
        # a claimed folder of ANOTHER promotion does not count
        self.legacy()
        with mock.patch.object(actions.shutil, 'move', only_first), mock.patch.object(actions, '_rollback_flat', side_effect=OSError('x')):
            only_first.used = False
            (self.root / 'processing_views/batch_01_plate--abc').rename(self.root / 'processing_views/batch_01_plate--old')
            self.assertEqual(self.act('legacy:batch:01_plate', 'approve')[0], 400)
        intent = guard.list_intents(self.root)[0]
        approved = self.root / 'approved/batch_01_plate'
        (approved / actions.PROMOTION_MARK).write_text(json.dumps({'promotion_id': 'someone-else'}))
        approved.rename(self.root / 'processing_views/batch_01_plate--new')
        self.assertEqual(self.act(f'intent:{intent["id"]}', 'complete')[0], 400)

    def test_reject_writes_the_origin_sidecar_before_the_rename(self):
        self.concept('one')
        sidecar = self.root / 'concepts/one/.preview-origin.json'
        sidecar.mkdir()                                                             # the sidecar cannot be written
        self.assertEqual(self.act('concepts:one', 'reject')[0], 400)
        self.assertTrue((self.root / 'concepts/one/human_male/outfit_front.png').exists())
        self.assertEqual(list((self.root / 'trash/concepts').iterdir()), [])
        sidecar.rmdir()
        real = Path.rename

        def fail(path, target):
            if path.name == 'one':
                raise PermissionError('in use')
            return real(path, target)
        with mock.patch.object(Path, 'rename', fail):
            self.assertEqual(self.act('concepts:one', 'reject')[0], 400)
        self.assertFalse(sidecar.exists())                                          # removed again: nothing half done
        self.assertTrue((self.root / 'concepts/one').exists())
        status, result = self.act('concepts:one', 'reject')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads((self.root / 'trash/concepts/one/.preview-origin.json').read_text()), {'kind': 'set', 'src': 'concepts/one'})
        _, state = self.get('/api/v2/state')
        self.assertEqual([c['id'] for c in state['tabs']['trash']], ['trash:one'])      # never stranded without its sidecar

    def test_flat_move_failure_is_rolled_back(self):
        batch = self.legacy()
        real = shutil.move
        calls = []

        def second_fails(src, dst):
            calls.append(src)
            if len(calls) == 2:
                raise OSError('boom')
            return real(src, dst)
        with mock.patch.object(actions.shutil, 'move', second_fails):
            self.assertEqual(self.act('legacy:batch:01_plate', 'reject')[0], 400)
        self.assertEqual(sorted(p.name for p in batch.iterdir()), ['01_plate.json', '01_plate__outfit.png'])
        self.assertEqual(list(self.root.glob('trash/concepts/*/*')), [])             # sidecar removed again
        self.assertEqual(guard.list_intents(self.root), [])

    def test_failed_rollback_stays_listed_as_recovery_and_can_be_completed_or_rolled_back(self):
        batch = self.legacy()
        real = shutil.move

        def only_first(src, dst):
            if not getattr(only_first, 'used', False):
                only_first.used = True
                return real(src, dst)
            raise OSError('disk went away')
        for resolution in ('rollback', 'complete'):
            only_first.used = False
            with mock.patch.object(actions.shutil, 'move', only_first):
                self.assertEqual(self.act('legacy:batch:01_plate', 'reject')[0], 400)
            intents = guard.list_intents(self.root)
            self.assertEqual([i['kind'] for i in intents], ['flat_move'])
            _, state = self.get('/api/v2/state')
            card = next(c for c in state['tabs']['trash'] if c['kind'] == 'recovery')
            self.assertEqual((card['id'], card['actions']), (f'intent:{intents[0]["id"]}', ['complete', 'rollback']))
            _, detail = self.get(f'/api/v2/item?id={card["id"]}')
            self.assertEqual(detail['detail']['races'][0]['info']['intent']['files'], 2)
            status, result = self.act(card['id'], resolution)
            self.assertEqual(status, 200)
            self.assertEqual(guard.list_intents(self.root), [])
            if resolution == 'rollback':
                self.assertEqual(sorted(p.name for p in batch.iterdir()), ['01_plate.json', '01_plate__outfit.png'])
                self.assertEqual(list(self.root.glob('trash/concepts/*/*')), [])
            else:
                self.assertEqual(list(batch.iterdir()), [])
                self.assertEqual(sorted(p.name for p in (self.root / 'trash/concepts/batch').iterdir()),
                                 ['01_plate.json', '01_plate.preview-origin.json', '01_plate__outfit.png'])
        self.assertEqual(self.act('intent:nothere', 'rollback')[0], 400)


class HostAndPathTests(V2Case):
    def test_unknown_host_is_refused_on_every_route(self):
        port = self.server.server_port
        for host in (f'evil.example:{port}', 'evil.example', f'127.0.0.1:{port + 1}', ''):
            for path in ('/', '/index.html', '/api/v2/state', '/api/v2/item?id=set:x', '/main.js', '/files/concepts/x.png', '/mfiles/x.png'):
                self.assertEqual(self.raw('GET', path, headers={'Host': host})[0], 421, (host, path))
            self.assertEqual(self.raw('POST', '/api/v2/action', b'{}', {'Host': host, 'X-Token': self.token})[0], 421)
        for host in (f'127.0.0.1:{port}', f'localhost:{port}', f'LOCALHOST:{port}'):
            self.assertEqual(self.raw('GET', '/api/v2/state', headers={'Host': host})[0], 200)
        # a token in the page is only handed to an expected Host (DNS rebinding)
        self.assertNotIn(self.token.encode(), self.raw('GET', '/', headers={'Host': 'evil.example'})[2])

    def test_tailnet_and_configured_hosts_and_origin_matching(self):
        fake = subprocess.CompletedProcess([], 0, json.dumps({'Self': {'DNSName': 'pc.tail1234.ts.net.', 'HostName': 'PC', 'TailscaleIPs': ['100.64.0.7']}}), '')
        (self.root / '.codex_jobs/_app').mkdir(parents=True, exist_ok=True)
        (self.root / '.codex_jobs/_app/settings.json').write_text(json.dumps({'allowed_hosts': ['review.example.test']}))
        found = server.discover_hosts(self.root, run=lambda *a, **k: fake)
        self.assertEqual(found, ['100.64.0.7', 'pc', 'pc.tail1234.ts.net', 'review.example.test'])
        self.assertEqual(server.discover_hosts(self.root, run=mock.Mock(side_effect=FileNotFoundError())), ['review.example.test'])
        serve = json.dumps({'Web': {'pc.tail1234.ts.net:8445': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8765'}}},
                                    'pc.tail1234.ts.net:8443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:3002'}}}}})
        def run(cmd, **kw):   # tailscale serve front-ends that proxy to this server's port keep their own port in Host
            return subprocess.CompletedProcess(cmd, 0, serve if 'serve' in cmd else fake.stdout, '')
        found_serve = server.discover_hosts(self.root, run=run, port=8765)
        self.assertIn('pc.tail1234.ts.net:8445', found_serve)
        self.assertNotIn('pc.tail1234.ts.net:8443', found_serve)
        second = server.create_server(self.root, port=0, web=self.web, extra_hosts=found, read_only=True)
        thread = threading.Thread(target=second.serve_forever, daemon=True)
        thread.start()
        try:
            port = second.server_port
            call = lambda method, path, headers, body=None: (lambda c: (c.request(method, path, body=body, headers=headers), c.getresponse().status)[1])(  # noqa: E731
                http.client.HTTPConnection('127.0.0.1', port))
            self.assertEqual(call('GET', '/api/v2/state', {'Host': 'pc.tail1234.ts.net'}), 200)
            self.assertEqual(call('GET', '/api/v2/state', {'Host': f'pc.tail1234.ts.net:{port}'}), 200)
            self.assertEqual(call('GET', '/api/v2/state', {'Host': 'other.ts.net'}), 421)
            # Origin is compared with the accepted hosts, not with whatever Host arrived
            post = lambda origin: call('POST', '/api/v2/action', {'Host': 'pc.tail1234.ts.net', 'Origin': origin, 'X-Token': second.token}, b'{}')   # noqa: E731
            self.assertEqual(post('https://other.ts.net'), 403)
            self.assertEqual(post('null'), 403)
            self.assertEqual(post('ftp://pc.tail1234.ts.net'), 403)
            self.assertEqual(post('https://pc.tail1234.ts.net'), 503)          # accepted origin; this server is read-only
            self.assertEqual(post(f'http://localhost:{port}'), 503)
        finally:
            second.shutdown()
            second.server_close()
            thread.join()

    def test_path_text_is_checked_before_any_filesystem_access(self):
        for bad in ('../x.png', '%2e%2e/x.png', '..%5Cx.png', 'C:/Windows/x.png', 'C%3A%5Cx.png', '%5C%5Cevil%5Cshare%5Cx.png', '//evil/share/x.png',
                    '/etc/x.png', 'a//b.png', 'a/./b.png', 'x.png:stream', 'a/b.png.', 'a/b.png%20', 'a%00b.png', 'a/<b>.png'):
            self.assertIsNone(server.clean_relative(bad), bad)
        self.assertEqual(server.clean_relative('concepts/one/outfit%20front.png'), 'concepts/one/outfit front.png')
        touched = []
        with mock.patch.object(Path, 'resolve', side_effect=lambda *a, **k: touched.append(1) or (_ for _ in ()).throw(AssertionError('resolve'))), \
                mock.patch.object(Path, 'is_file', side_effect=lambda *a, **k: touched.append(1) or (_ for _ in ()).throw(AssertionError('is_file'))):
            for bad in ('%5C%5Cevil%5Cshare%5Cx.png', 'C%3A/x.png', '..%2Fx.png', '/abs.png'):
                self.assertEqual(server.files_lookup(self.root, '/files/' + bad), (None, 403), bad)
                self.assertEqual(server.mfiles_lookup(self.root, '/mfiles/' + bad), (None, 403), bad)
        self.assertEqual(touched, [])
        for bad in ('/files/%5C%5Cevil%5Cshare%5Cx.png', '/files/C:/x.png', '/files//etc/x.png', '/mfiles/..%5C..%5Cx.png'):
            self.assertEqual(self.get(bad)[0], 403, bad)
        self.assertEqual(self.get('/%5C%5Cevil%5Cshare%5Cx.js')[0], 404)

    def test_index_is_served_through_the_containment_check(self):
        outside = self.root.parent / 'outside_index.html'
        outside.write_text('<html><head></head><body>SECRET</body></html>')
        (self.web / 'index.html').unlink()
        (self.web / 'index.html').symlink_to(outside)
        try:
            for path in ('/', '/index.html'):
                status, data = self.get(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b'SECRET', data if isinstance(data, bytes) else b'')
        finally:
            (self.web / 'index.html').unlink()
            outside.unlink()
        (self.web / 'index.html').unlink(missing_ok=True)
        status, data = self.get('/')                                                # no index at all: the placeholder
        self.assertEqual(status, 200)
        self.assertIn(b'backend is running', data)


class WriterTests(V2Case):
    def test_second_server_on_the_same_root_is_read_only(self):
        self.concept('one')
        second = server.create_server(self.root, port=0, web=self.web, extra_hosts=[])
        thread = threading.Thread(target=second.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertTrue(second.ctx['read_only'])
            self.assertFalse(self.server.ctx['read_only'])
            conn = http.client.HTTPConnection('127.0.0.1', second.server_port)
            body = json.dumps({'id': 'concepts:one', 'verb': 'reject'}).encode()
            conn.request('POST', '/api/v2/action', body, {'X-Token': second.token, 'Content-Type': 'application/json'})
            response = conn.getresponse()
            data = json.loads(response.read())
            self.assertEqual((response.status, data['code']), (503, 'read_only'))
            self.assertIn('another preview v2 server', data['error'])
            self.assertTrue((self.root / 'concepts/one').exists())
            conn.request('GET', '/api/v2/state')
            state = json.loads(conn.getresponse().read())
            self.assertTrue(state['server']['read_only'])
            self.assertEqual(len(state['tabs']['concepts']), 1)                       # reads work
            self.assertFalse(self.get('/api/v2/state')[1]['server']['read_only'])
        finally:
            second.shutdown()
            second.server_close()
            thread.join()
        self.assertEqual(self.act('concepts:one', 'reject')[0], 200)               # the writer still writes

    def restart_writer(self):
        """Stop the setUp server (this releases its writer lock); `start_writer` brings one back for tearDown."""
        self.server.shutdown()
        self.server.server_close()

    def start_writer(self):
        self.server = server.create_server(self.root, port=0, web=self.web, extra_hosts=[])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def test_the_writer_lock_is_released_with_the_server(self):
        self.restart_writer()
        third = server.create_server(self.root, port=0, web=self.web, extra_hosts=[])
        try:
            self.assertFalse(third.ctx['read_only'])
        finally:
            third.server_close()
        self.start_writer()                                                        # for tearDown

    def test_start_rolls_back_an_interrupted_write(self):
        self.full_set('s')
        path = self.root / 'models/s/pipeline_v2.json'
        path.write_text(json.dumps({'rev': 4}))
        written = guard.sha256_or_none(path.read_bytes())
        guard.new_intent(self.root, {'kind': 'files_tx', 'label': 'lod s', 'set': 's',
                                     'files': [{'path': 'models/s/pipeline_v2.json', 'before_b64': None, 'after_sha256': written}]})
        guard._ACTIVE.clear()                                                        # the process that wrote it is gone
        self.restart_writer()
        started = server.create_server(self.root, port=0, web=self.web, extra_hosts=[], read_only=False)
        started.server_close()
        self.assertFalse(path.exists())                                              # restored to "absent"
        self.assertEqual(guard.list_intents(self.root), [])
        # an OLD intent without the written digest, over a file that is neither its old nor any known content: never restored blindly
        path.write_text(json.dumps({'rev': 9}))
        guard.new_intent(self.root, {'kind': 'files_tx', 'label': 'lod s', 'set': 's', 'files': [{'path': 'models/s/pipeline_v2.json', 'before_b64': None}]})
        guard._ACTIVE.clear()
        started = server.create_server(self.root, port=0, web=self.web, extra_hosts=[], read_only=False)
        started.server_close()
        self.assertTrue(path.exists())
        self.assertEqual([bool(i.get('conflict')) for i in guard.list_intents(self.root)], [True])
        for intent in guard.list_intents(self.root):
            guard.finish_intent(self.root, intent['id'])
        self.start_writer()

    def test_set_lock_is_cross_process(self):
        script = textwrap.dedent(f"""
            import sys, time
            sys.path[:0] = [{str(HERE)!r}, {str(HERE.parent)!r}]
            from pathlib import Path
            import decisions
            root, name, n = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3])
            if name == 'hold':
                with decisions.set_lock(root, 's'):
                    print('ready', flush=True)
                    time.sleep(n)
            else:
                for i in range(int(n)):
                    decisions.mutate(root, 's', None, 'drop_race', {{}}, lambda d, k=f'{{name}}{{i}}': d['dropped'].update({{k: {{}}}}))
        """)
        holder = subprocess.Popen([sys.executable, '-c', script, str(self.root), 'hold', '2.5'], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), 'ready')
            started = decisions.now()
            with self.assertRaises(guard.Busy):                                        # another PROCESS holds it
                with guard.OsLock(decisions.models_dir(self.root, 's') / decisions.LOCKNAME, wait=0.3):
                    pass
        finally:
            holder.wait(15)
            holder.stdout.close()
        workers = [subprocess.Popen([sys.executable, '-c', script, str(self.root), f'p{i}', '6']) for i in range(3)]
        for worker in workers:
            self.assertEqual(worker.wait(120), 0)
        data = decisions.load(self.root, 's')
        self.assertEqual((data['rev'], len(data['dropped']), len(data['history'])), (18, 18, 18))      # no lost update across processes

    def test_monster_lod_writes_are_locked_unique_and_refused_when_approved(self):
        self.monster('Horse', '1', lods=('0p4', '0p2'), approved=True)
        self.monster('Horse', '2', lods=('0p4', '0p2', '0p1'))
        before = (self.mroot / 'pipeline/Horse/models/1/chosen_lod.json').read_bytes()
        status, result = self.post('/api/v2/lod', {'id': 'monster:Horse/1', 'lod': '0p2'})
        self.assertEqual((status, result['code']), (409, 'locked'))
        self.assertEqual((self.mroot / 'pipeline/Horse/models/1/chosen_lod.json').read_bytes(), before)
        errors = []

        def write(level):
            status, _ = self.post('/api/v2/lod', {'id': 'monster:Horse/2', 'lod': level})
            if status != 200:
                errors.append(status)
        threads = [threading.Thread(target=write, args=(lv,)) for lv in ('0p4', '0p2', '0p1') * 4]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        record = json.loads((self.mroot / 'pipeline/Horse/models/2/chosen_lod.json').read_text())
        self.assertIn(record['lod'], ('0p4', '0p2', '0p1'))
        self.assertEqual(list((self.mroot / 'pipeline/Horse/models/2').glob('*.tmp')), [])
        self.assertEqual(self.post('/api/v2/lod', {'id': 'monster:Horse/2', 'lod': '0p6'})[0], 400)
        self.assertEqual(self.post('/api/v2/lod', {'id': 'monster:Horse/9', 'lod': '0p2'})[0], 400)


class MigrationTests(V2Case):
    def test_report_is_read_only_and_idempotent(self):
        self.full_set('done')
        self.job('stuck', 'a', 'processing', (('human_male', 'outfit'), ('dwarf_male', 'outfit')), errors=['dwarf_male/outfit: broken'])
        self.reduce('stuck', 'a', 'human_male', 'outfit', lod='0p2')
        self.job('stuck', 'b', 'completed', (('human_male', 'outfit'),))
        self.reduce('stuck', 'b', 'human_male', 'outfit', mtime=4_000_000_000)
        self.concept('c')
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        report = migrate.build_report(self.root)
        after = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        self.assertEqual(before, after)
        self.assertEqual(json.dumps(report['summary'], sort_keys=True), json.dumps(migrate.build_report(self.root)['summary'], sort_keys=True))
        s = report['summary']
        self.assertEqual((s['set_stage'], s['stuck_sets'], s['conflict_units']), ({'3d': 1, 'approved': 1}, 1, 1))
        self.assertEqual((s['picks_on_chosen_units_by_level'], s['unpicked_units_with_pick_in_another_job'], s['chosen_lod_files_by_level_all_jobs']), ({}, 1, {'0p2': 1}))   # the pick sits in the older job 'a'
        self.assertEqual(s['stage_counts']['characters']['concepts'], 1)
        self.assertEqual(report['stuck'][0]['set'], 'stuck')
        self.assertTrue(migrate.to_markdown(report).startswith('# v2 migration dry run'))


if __name__ == '__main__':
    unittest.main()
