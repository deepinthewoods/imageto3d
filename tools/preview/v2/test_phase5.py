"""Tests of phase 5 of the v2 preview backend (DESIGN_PHASE5.md): monsters as a kind (monsters_v2.py, monster_prompts.py,
monster_lane.py) and their integration into catalog / actions / orchestrator / server, including the phase 5 review fixes F1-F18
(DESIGN_PHASE5.md H.10).

Run: python -X utf8 -m unittest tools/preview/v2/test_phase5.py

Everything is offline: a temp imageto3d root and a temp monster root (`IMAGETO3D_MONSTERS`), FAKE Hyper3D wrappers and a fake
reduce_monster.py in `<temp monster root>/tools/` (they write studio_submission.json, a glb and reduced/ like the real ones and log every call;
mode `real` drives the REAL rodin_backend record writer with a fake browser session), `V2_HYPER3D_PYTHON` = this python. No browser, no Codex
job, no paid step, no write to the real monster root or the real `.codex_jobs`.
"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import zlib

HERE = Path(__file__).resolve().parent
for _p in (HERE.parents[1], HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import guard  # noqa: E402
import actions  # noqa: E402
import catalog  # noqa: E402
import modes  # noqa: E402
import monster_lane  # noqa: E402
import monster_prompts  # noqa: E402
import monsters_v2 as mv  # noqa: E402
import orchestrator  # noqa: E402
import paid_gate  # noqa: E402
import progress  # noqa: E402
import server  # noqa: E402
import taskqueue  # noqa: E402

REAL_MROOT = (HERE.parents[2].parent / 'monsters' / 'aaa').resolve()
TOOLS = HERE.parents[1]

FAKE_H3D = r'''
import hashlib, json, os, pathlib, struct, sys
mroot = pathlib.Path(__file__).resolve().parents[1]
mode = os.environ.get('FAKE_H3D', 'ok')
sys.path.insert(0, os.environ['FAKE_TOOLS'])
import paid_gate
with open(mroot / 'calls.jsonl', 'a', encoding='utf-8') as log:
    log.write(json.dumps({'script': pathlib.Path(__file__).name, 'argv': sys.argv[1:], 'gate': os.environ.get(paid_gate.ENV)}) + '\n')
cmd = sys.argv[1]
files = (sys.argv[sys.argv.index('--view') + 1].split(',') if '--view' in sys.argv else
         sys.argv[sys.argv.index('--views') + 1].split(',') if '--views' in sys.argv else [])


def sha(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def make_glb(path):
    gltf = {'asset': {'version': '2.0'}, 'nodes': [{'mesh': 0}],
            'meshes': [{'primitives': [{'attributes': {'POSITION': 0, 'TEXCOORD_0': 1}, 'indices': 2, 'material': 0}]}],
            'accessors': [{'count': 3}, {'count': 3}, {'count': 36}],
            'materials': [{'pbrMetallicRoughness': {'baseColorTexture': {'index': 0}}}], 'images': [{'uri': 'x.png'}]}
    blob = json.dumps(gltf).encode()
    blob += b' ' * (-len(blob) % 4)
    pathlib.Path(path).write_bytes(b'glTF' + struct.pack('<II', 2, 12 + 8 + len(blob)) + struct.pack('<II', len(blob), 0x4E4F534A) + blob)


if mode.startswith('real'):
    # the REAL rodin_backend record writer (submit / collect) with a fake browser session (phase 5 review F1)
    sys.path.insert(0, str(pathlib.Path(os.environ['FAKE_TOOLS']) / 'hyper3d_pipeline'))
    import rodin_backend as rb
    MODEL = 'e1690fdd-98aa-4581-a015-2a2a58a787e4'

    class Session:
        def __init__(self, out, stages=()):
            self.out, self.stages = pathlib.Path(out), list(stages)
        def __enter__(self): return self
        def __exit__(self, *exc): pass
        def screenshot(self, label): pass
        def open_workspace(self): pass
        def prepare_multiview(self, paths, config): return {'thumbnails': len(paths)}
        def click_free_preview(self): pass
        def wait_model_url(self, timeout): return f'{rb.BASE}/{MODEL}'
        def wait_balance(self, timeout): return 100.0
        def insufficient_balance(self): return False
        def open_model(self, url): pass
        def wait_stage(self, stages, timeout): return self.stages.pop(0)
        def select_mesh_mode(self, config): return 'Best Quality'
        def read_cost(self, kind): return {'geometry': 0.5}.get(kind, 0), f'{kind} cost'
        def read_balance(self): return 100.0
        def symmetric_value(self): return 'Symmetric'
        def material_effort(self): return {'head': 'Material | Native | High', 'eight_k_on': False}
        def click(self, kind):
            if kind == 'geometry':
                with open(mroot / 'paid_clicks.txt', 'a') as paid:
                    paid.write('1\n')
        def set_delight(self): pass
        def export_glb(self, out_dir, config):
            target = pathlib.Path(out_dir) / 'glb_model.glb'
            make_glb(target)
            return target, 'GLB export'
    config = rb.load_config()
    try:
        if cmd == 'submit':
            views, out = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
            config |= {'view_order': files, 'directions': {}, 'multi_view': len(files) > 1, 'allow_mixed_sizes': True, 'max_images': 4}
            data = rb.submit(views, out, config=config, session_factory=lambda: Session(out))
            if mode == 'real_unresolved':      # an older backend that left the free intent pending on a bound record
                rec = rb.Record(out / rb.RECORD)
                rec.save('test', pending_action='free_preview')
        elif cmd == 'collect':
            out = pathlib.Path(sys.argv[2])
            rb.collect(out, allow_paid='--allow-paid' in sys.argv, accept_preview='--accept-preview' in sys.argv, config=config,
                       session_factory=lambda: Session(out, ['geometry_preview', 'material_ready', 'material_preview', 'export_ready']))
    except paid_gate.GateClosed as exc:
        print(f'PAID GATE CLOSED: {exc}', file=sys.stderr)
        sys.exit(4)
    except rb.Stop as exc:
        print(f'STOPPED: {exc}', file=sys.stderr)
        sys.exit(3)
    print(json.dumps({'status': 'ok'}))
    sys.exit(0)

if cmd == 'submit':
    views, out = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
    if mode == 'busy_submit':
        print('STOPPED: Hyper3D profile busy (another conversion holds the browser); nothing was generated', file=sys.stderr)
        sys.exit(3)
    rec = out / 'studio_submission.json'
    if rec.exists():
        print('STOPPED: record exists', file=sys.stderr)
        sys.exit(3)
    for name in files:
        assert (views / name).is_file(), name
    out.mkdir(parents=True, exist_ok=True)
    inputs = [{'name': n, 'sha256': sha(views / n), 'size': (views / n).stat().st_size} for n in files]
    (out / 'upload_inputs').mkdir(exist_ok=True)
    for n in files:                              # the final copies the backend uploads (rodin_backend.verify_upload_inputs)
        (out / 'upload_inputs' / n).write_bytes((views / n).read_bytes())
    if mode == 'other_inputs':
        inputs[0]['sha256'] = '0' * 64
    rec.write_text(json.dumps({'backend': 'hyper3d', 'status': 'submitted', 'model_id': 'm1', 'model_url': 'https://hyper3d.test/m1',
                               'actions': {'free_preview': {}}, 'pending_action': None, 'images_uploaded': files, 'credits_committed': 0,
                               'source': str(views.resolve()), 'inputs': inputs}))
    print(json.dumps({'status': 'submitted'}, indent=2))
elif cmd == 'collect':
    out = pathlib.Path(sys.argv[2])
    rec = out / 'studio_submission.json'
    data = json.loads(rec.read_text())
    if '--allow-paid' not in sys.argv or '--accept-preview' not in sys.argv:
        print('STOPPED: geometry may spend credits; pass --allow-paid', file=sys.stderr)
        sys.exit(3)
    if mode == 'busy_collect':
        print('STOPPED: Hyper3D profile busy (another conversion holds the browser)', file=sys.stderr)
        sys.exit(3)
    if mode == 'hold_during_collect':           # the hold takes effect while the collector waits for the browser / a stage
        (pathlib.Path(os.environ['FAKE_ROOT']) / '.codex_jobs' / '_batch' / 'CONVERT_HOLD').write_text('{"reason": "restart"}')
    reason = paid_gate.closed_reason()          # the click-time gate (DESIGN_PHASE4.md I.8), right before the paid click
    if reason:
        print(f'PAID GATE CLOSED: {reason}', file=sys.stderr)
        sys.exit(4)
    if 'geometry' not in data['actions']:
        data['actions']['geometry'] = {'paid': True, 'observed_cost': 0.5}
        if mode != 'unconfirmed_collect':         # the backend saw the paid stage advance (the only proof of a charge)
            data['actions']['geometry'] |= {'confirmed_at': '2026-11-25T12:00:00', 'confirmed_by': 'stage', 'provider_action_id': None}
        else:
            data['pending_action'] = 'geometry'
            data['paid_review'] = {'action': 'geometry', 'stage': 'geometry_preview', 'at': 'now', 'why': 'the stage did not advance'}
        data['observed_cost_geometry'] = {'credits': 0.5, 'text': '0.5 Credits'}
        data['credits_committed'] = data.get('credits_committed', 0) + 0.5
        with open(mroot / 'paid_clicks.txt', 'a') as paid:
            paid.write('1\n')
    if mode in ('stop_collect', 'unconfirmed_collect'):
        rec.write_text(json.dumps(data))
        (out / 'before_material_generate.png').write_bytes(b'png')
        print('STOPPED: material_generate: displayed cost 2 does not match 0', file=sys.stderr)
        sys.exit(3)
    (out / 'glb_m1.glb').write_bytes(b'glb')
    data['status'] = 'exported'
    # the export receipt of rodin_backend._record_export: the file, its size and its sha256
    data['exports'] = {'GLB': {'path': str(out / 'glb_m1.glb'), 'bytes': 3, 'sha256': sha(out / 'glb_m1.glb')}}
    if mode == 'no_receipt_sha':                 # an older record: the receipt names the file only
        data['exports'] = {'GLB': {'path': str(out / 'glb_m1.glb')}}
    rec.write_text(json.dumps(data))
    print(json.dumps({'status': 'exported'}, indent=2))
else:
    sys.exit(2)
'''

FAKE_REDUCE = r'''
import hashlib, json, os, pathlib, sys
mroot = pathlib.Path(__file__).resolve().parents[1]
with open(mroot / 'calls.jsonl', 'a', encoding='utf-8') as log:
    log.write(json.dumps({'script': 'reduce_monster.py', 'argv': sys.argv[1:]}) + '\n')
monster, _, variant = sys.argv[1].partition('/')
model = mroot / 'pipeline' / monster / 'models' / variant
out = model / 'reduced'
mode = os.environ.get('FAKE_REDUCE')
# like the real reduce_monster.py: --glb + --sha256 name the exact model; a mismatch is refused before anything is touched
source = sha = None
if '--glb' in sys.argv:
    source, sha = pathlib.Path(sys.argv[sys.argv.index('--glb') + 1]), sys.argv[sys.argv.index('--sha256') + 1]
    if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != sha:
        raise SystemExit(f'{source} is not the expected model before the reduction; nothing was reduced from it')
else:
    source = sorted((model / 'hyper3d').glob('glb_*.glb'))[0]      # the old manual behaviour: whatever GLB is there
if mode == 'other_glb':                          # a reducer that ignores its flags and takes another GLB of the folder
    source, sha = sorted(p for p in (model / 'hyper3d').glob('glb_*.glb') if p != source)[0], None
if out.exists():
    if '--redo' not in sys.argv:
        raise SystemExit(f'{out} exists; pass --redo')
    out.rename(model / f'reduced_prev_test_{len(list(model.glob("reduced_prev_test*")))}')
if mode == 'fail':
    raise SystemExit('reduction failed (exit 1, missing levels)')
out.mkdir(parents=True)
for lv in ('0p8', '0p6', '0p4', '0p2', '0p1'):
    (out / f'lod_error_{lv}.glb').write_bytes(b'glb' + lv.encode())
(out / 'manifest.json').write_text(json.dumps({'levels': {lv: {'triangles': 100} for lv in ('0p8', '0p6', '0p4', '0p2', '0p1')}}))
if mode == 'swap_during':                        # the receipted file is replaced while the reducer runs
    source.write_bytes(b'another model of the same name')
if mode != 'no_materials':
    (out / 'materials.json').write_text(json.dumps({'materials': {}, 'source': f'hyper3d/{source.name}',
                                                    **({'source_sha256': sha} if sha else {})}))
print(f'{sys.argv[1]}: 1 s')
'''

LIBRARY = '''
PIPELINE_LIBRARY = {
    "Goblin Shaman": "aaa bipeds/Goblin Shaman",
    "War Horse": "aaa quadrupeds/Warhorse",
}
'''

COLOURS = {'palette': 'monster_palette_v1', 'roles': {
    'skin': {'hex': '#E61923', 'parts': ['head and neck', 'legs and hooves']},
    'clothing': {'hex': '#19D228', 'parts': ['saddle cloth']},
    'extra': {'hex': '#14DCEB', 'meaning': 'mane', 'parts': ['mane and tail']},
    'eyes': {'hex': '#E119DC', 'parts': ['both eyes']}}}


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data))


def png(name: str, opaque: bool = False, w: int = 4, h: int = 4) -> bytes:
    """A real 8-bit RGBA PNG (one transparent pixel unless `opaque`), unique per name (a tEXt chunk carries it)."""
    rows = b''
    for y in range(h):
        row = b'\x00'
        for x in range(w):
            row += bytes((200, 30, 40, 255 if opaque or (x, y) != (0, 0) else 0))
        rows += row
    return (b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0)) +
            _chunk(b'tEXt', b'name\x00' + name.encode('utf-8')) + _chunk(b'IDAT', zlib.compress(rows)) + _chunk(b'IEND', b''))


class Crash(BaseException):
    """A simulated process exit at a fault point (no inline rollback runs)."""


class MonsterCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / 'project'
        self.mroot = base / 'monsters_aaa'
        (self.root / '.codex_jobs' / '_app').mkdir(parents=True)
        (self.root / '.codex_jobs' / '_batch').mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {'IMAGETO3D_MONSTERS': str(self.mroot), 'V2_HYPER3D_PYTHON': sys.executable,
                                                'FAKE_TOOLS': str(TOOLS), 'FAKE_ROOT': str(self.root)})
        self.env.start()
        for name in ('FAKE_H3D', 'FAKE_REDUCE', paid_gate.ENV):
            os.environ.pop(name, None)
        self.ttl = catalog.STATE_TTL
        catalog.STATE_TTL = 0
        self.idle_patch = mock.patch.object(modes, 'idle_seconds', lambda *a, **k: (10.0, 'test'))   # the user is at the PC (in-process)
        self.idle_patch.start()
        mv.FAULTS.clear()
        self.build_monster_root()
        self.assertNotEqual(mv.mroot_of(self.root).resolve(), REAL_MROOT)

    def tearDown(self):
        mv.FAULTS.clear()
        catalog.STATE_TTL = self.ttl
        self.idle_patch.stop()
        self.env.stop()
        taskqueue._QUEUES.pop(str(self.root), None)
        for path in self.mroot.rglob('*'):                   # read-only input snapshots
            try:
                os.chmod(path, 0o666)
            except OSError:
                pass
        self.temp.cleanup()

    # ---------------------------------------------------------------------------------------------------- fixture
    def concept(self, folder, n, prompt=True, colours=True, attempt=False):
        fdir = self.mroot / 'concepts' / folder
        fdir.mkdir(parents=True, exist_ok=True)
        (fdir / f'codex{n}-front.png').write_bytes(png(f'{folder}{n}'))
        if prompt:
            (fdir / f'codex{n}-front.prompt.json').write_text(json.dumps({'monster': folder, 'adjective': f'adj{n}', 'prompt': f'prompt {n}',
                                                                           'job': f'job-{n}', 'attempts': '1'}))
        if colours:
            (fdir / f'codex{n}-front.colours.json').write_text(json.dumps(COLOURS))
        if attempt:
            (fdir / f'codex{n}-front_attempt1.png').write_bytes(png('attempt'))
        return fdir

    def build_monster_root(self):
        m = self.mroot
        tools = m / 'tools'
        tools.mkdir(parents=True)
        for name in ('hyper3d_single.py', 'hyper3d_multiview.py'):
            (tools / name).write_text(FAKE_H3D)
        (tools / 'reduce_monster.py').write_text(FAKE_REDUCE)
        (tools / 'build_monster_blends.py').write_text(LIBRARY)
        (tools / 'codex_imagegen.sh').write_text('#!/bin/sh\nexit 1\n')
        for n in (1, 2, 3):
            self.concept('Warhorse', n, attempt=(n == 2))
        (m / 'concepts' / 'Warhorse' / 'codex4-front.prompt.json').write_text('{}')           # JSON without PNG: hidden
        (m / 'concepts' / 'Warhorse' / '1.png').write_bytes(png('legacy'))                    # loose legacy PNG: hidden
        (m / 'concepts' / 'Warhorse' / 'adjectives.json').write_text(json.dumps({'adjectives': {'codex1': 'stocky'}}))
        (m / 'concepts' / 'Warhorse' / 'local_model_descriptions.json').write_text(
            json.dumps({'descriptions': {'codex2': {'text': 'a horse made of hedges'}}}))
        for n in (1, 2, 3):
            self.concept('Hobgoblin', n)
        self.concept('Goblin Shaman', 4)
        self.concept('Giant Rat', 1)
        self.concept('Giant Rat', 2)
        wh = m / 'pipeline' / 'War Horse'
        (wh / 'views' / '1-plate').mkdir(parents=True)
        (wh / 'views' / '1-plate' / 'front.png').write_bytes(png('wh 1-plate'))
        (wh / 'variant_names.json').write_text(json.dumps({'variants': {'1': {'was': 'codex1', 'concept': 'codex1-front.png'},
                                                                         '1-plate': {'was': 'codex1-plate', 'concept': 'codex1-front.png'},
                                                                         '6-scale': {'was': 'codex16-scale', 'concept': 'codex16-front.png'}}}))
        hob = m / 'pipeline' / 'Hobgoblin'
        hob.mkdir(parents=True)
        (hob / 'variant_names.json').write_text(json.dumps({'variants': {'1': {'was': 'codex1', 'concept': 'codex1-front.png'}}}))
        (hob / '1-front.png').write_bytes(png('hob flat 1'))
        (hob / '1-front.colours.json').write_text(json.dumps(COLOURS))
        (hob / '1-front.prompts.json').write_text('{"prompt": "x"}')
        (hob / '1-back.png').write_bytes(png('hob flat 1 back'))
        (hob / '1-back.prompts.json').write_text('{"prompt": "back"}')
        (hob / '2-front.png').write_bytes(png('Hobgoblin2'))                                   # sha256-identical to concepts/Hobgoblin/codex2
        gs = m / 'pipeline' / 'Goblin Shaman'
        (gs / 'views' / 'main').mkdir(parents=True)
        (gs / 'views' / 'main' / 'front.png').write_bytes(png('gs recolour'))
        (gs / 'front.png').write_bytes(png('gs flat'))
        (gs / 'variant_names.json').write_text(json.dumps({'variants': {'main': {'was': 'codex4 (then 4)'}}}))
        self.converted('Goblin Shaman', 'main')
        ka = m / 'pipeline' / 'Kobold Artificer'
        (ka / 'views' / 'main').mkdir(parents=True)
        (ka / 'views' / 'main' / 'front.png').write_bytes(png('ka'))

    def converted(self, monster, variant, approved=None, lod='0p4', verify=True):
        """reduced/ with 5 LODs, manifest and materials.json; for a v2 variant with a freeze, the lane's success record too (verify)."""
        mdir = self.mroot / 'pipeline' / monster / 'models' / variant
        red = mdir / 'reduced'
        red.mkdir(parents=True, exist_ok=True)
        for lv in mv.LOD_LEVELS:
            (red / f'lod_error_{lv}.glb').write_bytes(b'glb' + lv.encode())
        (red / 'manifest.json').write_text('{"levels": {}}')
        (red / 'materials.json').write_text('{"materials": {}}')
        vdir = self.mroot / 'pipeline' / monster / 'views' / variant
        if verify and (vdir / mv.VIEWS_JSON).is_file():
            vj = mv.read_views_json(vdir)
            if vj and vj.get('frozen'):
                mv.write_reduce_ok(mdir, vj['frozen'])
        if approved is not None:
            (mdir / 'chosen_lod.json').write_text(json.dumps({'lod': lod, 'approved': approved}))

    def state(self, folder):
        return json.loads((self.mroot / 'concepts' / folder / mv.STATE_FILE).read_text())

    def calls(self):
        path = self.mroot / 'calls.jsonl'
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def paid_clicks(self):
        path = self.mroot / 'paid_clicks.txt'
        return len(path.read_text().splitlines()) if path.exists() else 0

    def act(self, item_id, verb, **args):
        return mv.perform(self.root, {'id': item_id, 'verb': verb, 'args': args})

    def convert(self, item_id, files, verb='convert', **extra):
        rev = mv.item(self.root, item_id)['views_rev']
        return self.act(item_id, verb, files=list(files), views_rev=rev, **extra)

    def tabs(self):
        return {tab: {c['id']: c for c in cards} for tab, cards in mv.state_cards(self.root).items()}

    def finish_views(self, monster, variant, views=('left', 'back', 'right'), state='done', opaque=False):
        vdir = mv.views_dir(self.mroot, monster, variant)
        for view in views:
            name = mv.view_task_name(monster, variant, view)
            if state in ('done', 'launching_with_png'):
                (vdir / f'{view}.png').write_bytes(png(f'{monster}{variant}{view}', opaque=opaque))
                (vdir / f'{view}.prompts.json').write_text(json.dumps({'view': view, 'prompt': 'p'}))
            runner_state = 'launching' if state == 'launching_with_png' else state
            guard.write_atomic(self.root / '.codex_jobs/_batch/status' / f'{name}.json',
                               json.dumps({'state': runner_state, 'attempts': 1 if runner_state == 'done' else 2,
                                           'jobs': [{'job': f'{name}-a01'}]}).encode())

    def ident(self, task_id):
        """{key, generation} of a lane row: what the client displays and every queue op must echo (recheck finding 4)."""
        row = taskqueue.for_root(self.root).get(task_id)
        return {'key': row['key'], 'generation': row['args']['frozen_sha']}

    def seq(self, task_id):
        """The disposition revision a lane row is displayed with (`conversion_seq`): Retry / Run now of a failed or cancelled row echo it
        (post-cut-over blocker 4)."""
        return monster_lane.shown_seq(self.root, taskqueue.for_root(self.root).get(task_id))

    def qop(self, op, task_id, **extra):
        if extra.get('confirm') and 'pause_id' not in extra:
            extra['pause_id'] = monster_lane.pause_id(self.root)
        if op in ('retry', 'run_now'):
            extra.setdefault('conversion_seq', self.seq(task_id))
        return actions.queue_op(self.root, {'op': op, 'id': task_id} | self.ident(task_id) | extra)

    def run_now(self, task_id, **extra):
        if extra.get('confirm') and 'pause_id' not in extra:
            extra['pause_id'] = monster_lane.pause_id(self.root)
        extra.setdefault('conversion_seq', self.seq(task_id))
        return monster_lane.run_now(self.root, task_id, **(self.ident(task_id) | extra))

    def supervisor_pause(self, reason, scope, since='s', mirror=True):
        """What the supervisor writes when it pauses (DESIGN_PHASE4.md I.11): its OWN record supervisor_pause.json and, as `mirror`
        says, the PAUSE mirror whose sha256 the record names. Returns the mirror's bytes."""
        batch = self.root / '.codex_jobs/_batch'
        body = (json.dumps({'since': since, 'reason': reason, 'scope': scope}, indent=2) + '\n').encode('utf-8')
        if mirror:
            guard.write_atomic(batch / 'PAUSE', body)
        guard.write_atomic(batch / 'supervisor_pause.json', json.dumps(
            {'since': since, 'reason': reason, 'scope': scope, 'pid': os.getpid(), 'mirror': hashlib.sha256(body).hexdigest()}).encode())
        return body

    def supervisor_resume(self, mirror=True):
        """The supervisor resumes: its record goes, and PAUSE too when it was its mirror."""
        batch = self.root / '.codex_jobs/_batch'
        (batch / 'supervisor_pause.json').unlink(missing_ok=True)
        if mirror:
            (batch / 'PAUSE').unlink(missing_ok=True)

    def unapprove(self, item_id, **args):
        """Unapprove as the Completed modal sends it: with the approval revision the item shows (post-cut-over blocker 5)."""
        return self.act(item_id, 'unapprove', approval_rev=mv.item(self.root, item_id)['approval_rev'], **args)

    def pick_rat(self, n=1, unattended=False):
        r = self.act(f'mconcept:Giant Rat/codex{n}', 'approve', unattended=unattended)
        return f'monster:Giant Rat/{r["variant"]}', r['variant']


# ------------------------------------------------------------------------------------------------------------------ prompts

class PromptTest(unittest.TestCase):
    def test_views_ascii_established_wording_and_colours(self):
        for view in monster_prompts.VIEWS:
            text = monster_prompts.image_prompt(view, 'War Horse', COLOURS)
            text.encode('ascii')
            self.assertIn('Image 1 shows a papercraft creature from the front.', text)
            self.assertIn('- red #E61923: head and neck; legs and hooves.', text)
            self.assertIn('- cyan #14DCEB: mane and tail (mane).', text)
            self.assertIn('Each region is ONE hue.', text)
            self.assertIn('genuinely transparent (real alpha)', text)
            self.assertIn('whole creature in frame', text)
            self.assertIn('do not', text.split('FRONT in Image 1')[1][:80])
        back = monster_prompts.image_prompt('back', 'War Horse', COLOURS)
        self.assertIn('turned 180 degrees around it', back)
        self.assertIn('kept the same height and distance', back)
        left = monster_prompts.image_prompt('left', 'War Horse', COLOURS)
        self.assertIn("a quarter of the way round the creature to the creature's own LEFT side (about 90 degrees", left)
        self.assertIn('anatomical left', left)
        right = monster_prompts.image_prompt('right', 'War Horse', COLOURS)
        self.assertIn("creature's own RIGHT side", right)
        self.assertIn('anatomical right', right)
        self.assertEqual(monster_prompts.colour_lines(None)[0][:15], '- red #E61923: ')
        with self.assertRaises(ValueError):
            monster_prompts.image_prompt('top', 'x', None)

    def test_job_prompt_paths_record_scope_and_no_framing_check(self):
        """F17: the job prompt asks for a technical format check only (no framing / pose / colour inspection or redraw)."""
        text = monster_prompts.job_prompt('left', 'War Horse \u2014 \u00b0', '7', Path('F:/m/pipeline/War Horse/views/7/front.png'),
                                          Path('F:/m/pipeline/War Horse/views/7/left.png'), COLOURS)
        text.encode('ascii')
        self.assertIn('`F:/m/pipeline/War Horse/views/7/left.png`', text)
        self.assertIn('`F:/m/pipeline/War Horse/views/7/left.prompts.json`', text)
        self.assertIn('2-back.prompts.json', text)
        self.assertIn('Regenerate ONCE only if that format is wrong', text)
        self.assertIn('Do NOT inspect or judge framing', text)
        self.assertNotIn('cut off by the frame', text)
        self.assertNotIn('whole_creature_in_frame', text)
        self.assertIn('No conversion or 3D service calls', text)


# ------------------------------------------------------------------------------------------------------------------ intake

class IntakeTest(MonsterCase):
    def test_finals_only_mapping_intent(self):
        tabs = self.tabs()
        ids = set(tabs['concepts'])
        self.assertEqual({i for i in ids if i.startswith('mconcept:Warhorse/')},
                         {'mconcept:Warhorse/codex1', 'mconcept:Warhorse/codex2', 'mconcept:Warhorse/codex3'})
        card = tabs['concepts']['mconcept:Warhorse/codex1']
        self.assertEqual((card['type'], card['kind'], card['stage']), ('monster', 'monster', 'concepts'))
        self.assertEqual(card['title'], 'War Horse \u00b7 codex1')
        self.assertTrue(card['thumbnail'].startswith('/mfiles/concepts/Warhorse/codex1-front.png?v='))
        self.assertEqual(card['actions'], ['provisional', 'approve', 'reject'])
        self.assertEqual((card['next_variant'], card['library_hint'], card['concept']), ('7', None, 'codex1'))
        item = mv.item(self.root, 'mconcept:Warhorse/codex2')
        self.assertEqual((item['monster'], item['folder'], item['concept'], item['image']), ('War Horse', 'Warhorse', 'codex2', item['thumbnail']))
        self.assertEqual(item['intent'], {'local_model_descriptions': 'a horse made of hedges', 'adjective': 'adj2'})
        self.assertEqual(mv.item(self.root, 'mconcept:Warhorse/codex1')['intent'], {'adjectives': 'stocky', 'adjective': 'adj1'})
        self.assertEqual(item['prompt'], {'prompt': 'prompt 2', 'job': 'job-2', 'attempts': '1'})
        self.assertEqual([r['role'] for r in item['roles']], ['skin', 'clothing', 'extra', 'eyes'])
        rat = mv.item(self.root, 'mconcept:Giant Rat/codex1')
        self.assertEqual((rat['next_variant'], rat['library_hint']), ('main', mv.LIBRARY_BADGE))
        self.assertIn('new monster', [b['text'] for b in rat['badges']])
        for bad in ('mconcept:Warhorse/codex4', 'mconcept:../x/codex1', 'mconcept:Warhorse/x1', 'mconcept:Nope/codex1'):
            errors = (ValueError, LookupError, catalog.NotFound)
            with self.assertRaises(errors):
                mv.item(self.root, bad)

    def test_numbering(self):
        self.assertEqual(mv.next_variant(self.mroot, 'Giant Rat'), 'main')
        self.assertEqual(mv.next_variant(self.mroot, 'Kobold Artificer'), '2')
        self.assertEqual(mv.next_variant(self.mroot, 'Goblin Shaman'), '2')
        self.assertEqual(mv.next_variant(self.mroot, 'War Horse'), '7')
        names = json.loads((self.mroot / 'pipeline/Hobgoblin/variant_names.json').read_text())
        names['retired'] = {'9': {'was': 'codex9'}}
        (self.mroot / 'pipeline/Hobgoblin/variant_names.json').write_text(json.dumps(names))
        self.assertEqual(mv.next_variant(self.mroot, 'Hobgoblin'), '10')

    def test_unreadable_state(self):
        (self.mroot / 'concepts' / 'Warhorse' / mv.STATE_FILE).write_text('{not json')
        card = self.tabs()['concepts']['mconcept:Warhorse/codex1']
        self.assertEqual(card['actions'], [])
        self.assertIn('state unreadable', [b['text'] for b in card['badges']])
        with self.assertRaises(guard.Unreadable):
            self.act('mconcept:Warhorse/codex1', 'provisional')
        self.assertEqual((self.mroot / 'concepts' / 'Warhorse' / mv.STATE_FILE).read_text(), '{not json')

    def test_stage_verbs_and_allowlist(self):
        cid = 'mconcept:Giant Rat/codex1'
        r = self.act(cid, 'provisional')
        self.assertEqual(r['undo'], {'verb': 'unapprove', 'id': cid, 'args': {'to': 'concepts'}})
        self.assertIn(cid, self.tabs()['provisional'])
        self.assertEqual(self.act(cid, 'unapprove')['stage'], 'concepts')
        r = self.act(cid, 'reject')
        self.assertEqual(r['undo'], {'verb': 'restore', 'id': cid})
        self.assertIn('from concepts', [b['text'] for b in self.tabs()['trash'][cid]['badges']])
        self.assertEqual(self.act(cid, 'restore')['stage'], 'concepts')
        self.act(cid, 'archive')
        self.assertEqual(self.tabs()['archive'][cid]['actions'], ['approve', 'unarchive'])
        self.act(cid, 'unarchive')
        self.assertEqual(self.state('Giant Rat')['concepts']['codex1']['stage'], 'concepts')
        with self.assertRaises(ValueError):
            self.act(cid, 'restore')
        with self.assertRaises(ValueError):
            self.act(cid, 'rebuild')
        with self.assertRaises(ValueError):
            self.act('monster:War Horse/1-plate', 'reject')
        self.assertTrue(mv.handles('mconcept:x/codex1') and mv.handles('monster:x/1') and not mv.handles('set:x'))
        self.assertTrue((self.mroot / 'concepts/Giant Rat/codex1-front.png').is_file())


# --------------------------------------------------------------------------------------------------------------------- pick

class PickTest(MonsterCase):
    def test_pick_new_monster_main_tasks_carry_guard_and_validation(self):
        """B + F10 / F17: the view tasks carry the pick-generation guard, `validate: rgba_png` and the provenance record."""
        r = self.act('mconcept:Giant Rat/codex2', 'approve')
        self.assertEqual((r['monster'], r['variant'], r['from']), ('Giant Rat', 'main', 'concepts'))
        self.assertEqual(r['undo'], {'verb': 'unapprove', 'id': 'mconcept:Giant Rat/codex2', 'args': {'to': 'concepts'}})
        vdir = self.mroot / 'pipeline/Giant Rat/views/main'
        self.assertEqual((vdir / 'front.png').read_bytes(), png('Giant Rat2'))
        self.assertEqual(json.loads((vdir / 'colours.json').read_text()), COLOURS)
        vj = json.loads((vdir / 'views.json').read_text())
        self.assertEqual((vj['schema'], vj['views'], vj['unattended'], vj['frozen']),
                         (1, [{'name': 'front', 'file': 'front.png', 'enabled': True}], False, None))
        self.assertRegex(vj['pick_gen'], r'^[0-9a-f]{32}$')
        names = json.loads((self.mroot / 'pipeline/Giant Rat/variant_names.json').read_text())
        self.assertEqual({k: v for k, v in names['variants']['main'].items() if k != 'picked'},
                         {'was': 'codex2', 'concept': 'codex2-front.png', 'by': 'v2'})
        self.assertFalse(list((self.mroot / 'pipeline/Giant Rat').glob('*-front.*')))
        entry = self.state('Giant Rat')['concepts']['codex2']
        self.assertEqual((entry['stage'], entry['monster'], entry['variant']), ('approved', 'Giant Rat', 'main'))
        self.assertEqual(len(r['tasks']), 3)
        for view in ('left', 'back', 'right'):
            name = mv.view_task_name('Giant Rat', 'main', view)
            self.assertTrue(name.startswith('mviews--Giant_Rat--main--') and name.endswith(f'--{view}'))
            task = json.loads((self.root / '.codex_jobs/_batch/tasks' / f'{name}.json').read_text())
            self.assertEqual((task['launcher'], task['checks'], task['max_attempts'], task['priority']), ('monsters', [], 2, 3))
            self.assertEqual((task['validate'], task['provenance']), ('rgba_png', [f'{view}.prompts.json']))
            self.assertEqual(task['guard'], {'file': str(vdir / 'views.json'), 'key': 'pick_gen', 'value': vj['pick_gen']})
            self.assertEqual(task['outputs'], [(vdir / f'{view}.png').as_posix()])
            task['prompt'].encode('ascii')
        self.assertFalse(list(mv.journal_dir(self.root).glob('*.json')))                         # the publication journal is gone
        card = self.tabs()['approved']['monster:Giant Rat/main']
        texts = [b['text'] for b in card['badges']]
        self.assertIn(mv.LIBRARY_BADGE, texts)
        self.assertIn('views 1/4', texts)
        r2 = self.act('mconcept:Giant Rat/codex1', 'approve', unattended=True)
        self.assertEqual(r2['variant'], '2')

    def test_pick_existing_numbered_and_library_mapped(self):
        self.act('mconcept:Warhorse/codex3', 'provisional')
        r = self.act('mconcept:Warhorse/codex3', 'approve')
        self.assertEqual((r['monster'], r['variant'], r['from']), ('War Horse', '7', 'provisional'))
        self.assertTrue(r['library']['mapped'])

    def test_undo_pick_until_a_view_task_starts(self):
        cid = 'mconcept:Warhorse/codex2'
        r = self.act(cid, 'approve')
        self.assertEqual(r['variant'], '7')
        undo = self.act(cid, r['undo']['verb'], **r['undo']['args'])
        self.assertEqual(undo['stage'], 'concepts')
        self.assertFalse((self.mroot / 'pipeline/War Horse/views/7').exists())
        self.assertTrue(list((self.mroot / 'pipeline/War Horse/views').glob('.v2_withdrawn_7_*')))
        names = json.loads((self.mroot / 'pipeline/War Horse/variant_names.json').read_text())
        self.assertNotIn('7', names['variants'])
        self.assertEqual(names['retired']['7']['was'], 'codex2')
        self.assertFalse(list((self.root / '.codex_jobs/_batch/tasks').glob('*.json')))
        self.assertEqual(len(list((self.root / '.codex_jobs/_batch/tasks').glob('*.json.withdrawn-*'))), 3)
        r = self.act(cid, 'approve')
        self.assertEqual(r['variant'], '8')
        self.finish_views('War Horse', '8', ('left',), state='running')
        with self.assertRaises(guard.Conflict):
            self.act(cid, 'unapprove', to='concepts')
        with self.assertRaises(guard.Conflict):
            self.act('monster:War Horse/8', 'unapprove')
        self.assertTrue((self.mroot / 'pipeline/War Horse/views/8/front.png').is_file())

    def test_withdraw_invalidates_guard_first_and_refuses_a_late_launch(self):
        """F10: the views folder (the guard file) is renamed before the task files; a launch reserved meanwhile restores everything."""
        cid = 'mconcept:Giant Rat/codex1'
        r = self.act(cid, 'approve')
        v = r['variant']
        real = mv.view_states
        calls = {'n': 0}

        def states(root, mroot, monster, variant):
            calls['n'] += 1
            out = real(root, mroot, monster, variant)
            if calls['n'] >= 2:                                 # the runner reserved a launch after the first check
                self.assertFalse((mv.views_dir(self.mroot, monster, variant) / 'views.json').exists())   # guard already invalid
                out['left'] = out['left'] | {'state': 'launching'}
            return out
        with mock.patch.object(mv, 'view_states', states):
            with self.assertRaises(guard.Conflict):
                self.act(cid, 'unapprove', to='concepts')
        self.assertTrue((self.mroot / f'pipeline/Giant Rat/views/{v}/views.json').is_file())     # restored
        self.assertEqual(len(list((self.root / '.codex_jobs/_batch/tasks').glob('*.json'))), 3)
        self.assertEqual(self.state('Giant Rat')['concepts']['codex1']['stage'], 'approved')

    def test_task_publication_under_the_monster_lock(self):
        """F10: the view tasks are written while the pick still holds the monster lock."""
        seen = []
        real = mv.queue_view_tasks

        def spy(root, mroot, monster, variant, views=mv.EXTRA_VIEWS):
            seen.append(mv.monster_lock(root, monster)._depth)
            return real(root, mroot, monster, variant, views)
        with mock.patch.object(mv, 'queue_view_tasks', spy):
            self.act('mconcept:Giant Rat/codex1', 'approve')
        self.assertEqual(seen, [1])

    def test_import_flat_pick_without_renumbering(self):
        card = self.tabs()['approved']['monster:Hobgoblin/1']
        self.assertEqual(card['actions'], ['import'])
        self.assertIn('picked, not imported', [b['text'] for b in card['badges']])
        r = self.act('monster:Hobgoblin/1', 'import', unattended=True)
        vdir = self.mroot / 'pipeline/Hobgoblin/views/1'
        self.assertEqual(sorted(r['copied']), ['back.png', 'back.prompts.json', 'colours.json', 'front.png', 'prompts.json'])
        self.assertEqual((vdir / 'back.png').read_bytes(), png('hob flat 1 back'))
        vj = json.loads((vdir / 'views.json').read_text())
        self.assertEqual([v['name'] for v in vj['views']], ['front', 'back'])
        self.assertEqual(vj['planned'], ['left', 'right'])
        self.assertTrue(vj['unattended'] and vj['pick_gen'])
        self.assertEqual(sorted(p.name for p in (self.root / '.codex_jobs/_batch/tasks').glob('*.json')),
                         sorted(f'{mv.view_task_name("Hobgoblin", "1", v)}.json' for v in ('left', 'right')))
        self.assertEqual(r['concept'], 'mconcept:Hobgoblin/codex1')
        self.assertEqual(self.state('Hobgoblin')['concepts']['codex1']['variant'], '1')
        self.assertTrue((self.mroot / 'pipeline/Hobgoblin/1-front.png').is_file())
        with self.assertRaises(guard.Conflict):
            self.act('monster:Hobgoblin/1', 'import')
        with self.assertRaises(guard.Conflict):
            self.act('mconcept:Hobgoblin/codex1', 'unapprove')


class JournalTest(MonsterCase):
    """F11 / F10: damaged ledgers refuse mutations; an interrupted pick / import is completed by recovery (the same variant)."""

    def test_malformed_variant_names_refuses_and_is_never_replaced(self):
        path = self.mroot / 'pipeline/War Horse/variant_names.json'
        for bad in ('{not json', '{"variants": []}', '{"variants": {"1": "x"}}', '{"variants": {}, "retired": [9]}'):
            path.write_text(bad)
            with self.assertRaises(guard.Unreadable):
                self.act('mconcept:Warhorse/codex3', 'approve')
            self.assertEqual(path.read_text(), bad)
            self.assertFalse((self.mroot / 'pipeline/War Horse/views/7').exists())
        path.write_text('{"variants": [}')
        with self.assertRaises(guard.Unreadable):
            self.act('monster:Hobgoblin/1', 'import') if False else mv.import_pick(self.root, 'War Horse', '1')

    def test_crash_after_rename_is_completed_by_recovery(self):
        mv.FAULTS['pick_renamed'] = lambda: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            self.act('mconcept:Giant Rat/codex1', 'approve')
        mv.FAULTS.clear()
        vdir = self.mroot / 'pipeline/Giant Rat/views/main'
        self.assertTrue((vdir / 'views.json').is_file())
        self.assertNotEqual(json.loads((self.mroot / 'concepts/Giant Rat/.v2_state.json').read_text()
                                       if (self.mroot / 'concepts/Giant Rat/.v2_state.json').exists() else '{"concepts": {}}')
                            .get('concepts', {}).get('codex1', {}).get('stage'), 'approved')
        self.assertEqual(len(list(mv.journal_dir(self.root).glob('*.json'))), 1)
        out = monster_lane.tick(self.root)
        self.assertIn('Giant Rat: completed', out['recovered'])
        entry = self.state('Giant Rat')['concepts']['codex1']
        self.assertEqual((entry['stage'], entry['variant']), ('approved', 'main'))
        self.assertEqual(len(list((self.root / '.codex_jobs/_batch/tasks').glob('*.json'))), 3)
        self.assertFalse(list(mv.journal_dir(self.root).glob('*.json')))
        self.assertEqual(self.act('mconcept:Giant Rat/codex2', 'approve')['variant'], '2')         # nothing repeated / reused

    def test_crash_after_names_completes_the_same_variant_or_retires_it(self):
        mv.FAULTS['pick_names'] = lambda: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            self.act('mconcept:Warhorse/codex3', 'approve')
        mv.FAULTS.clear()
        names = json.loads((self.mroot / 'pipeline/War Horse/variant_names.json').read_text())
        self.assertEqual(names['variants']['7']['was'], 'codex3')
        self.assertFalse((self.mroot / 'pipeline/War Horse/views/7').exists())
        with self.assertRaises(guard.Recovering):                                               # another folder's journal: the tick recovers it
            mv.import_pick(self.root, 'War Horse', '1')
        self.assertIn('War Horse: completed', monster_lane.tick(self.root)['recovered'])
        self.assertTrue((self.mroot / 'pipeline/War Horse/views/7/views.json').is_file())
        self.assertEqual(self.state('Warhorse')['concepts']['codex3']['variant'], '7')
        # the temp folder gone too: the published number is retired, never reused
        mv.FAULTS['pick_names'] = lambda: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            self.act('mconcept:Warhorse/codex2', 'approve')
        mv.FAULTS.clear()
        for tmp in (self.mroot / 'pipeline/War Horse/views').glob('.v2_pick_8_*'):
            mv._rmtree_own(tmp)
        self.assertIn('War Horse: rolled back', monster_lane.tick(self.root)['recovered'])
        names = json.loads((self.mroot / 'pipeline/War Horse/variant_names.json').read_text())
        self.assertNotIn('8', names['variants'])
        self.assertIn('8', names['retired'])
        self.assertEqual(self.act('mconcept:Warhorse/codex2', 'approve')['variant'], '9')


# ------------------------------------------------------------------------------------------------------------- views / convert

class ViewsTest(MonsterCase):
    def test_choose_views_freeze_exact_files_and_queue(self):
        mid, v = self.pick_rat()
        self.finish_views('Giant Rat', v, ('left', 'back'))
        self.finish_views('Giant Rat', v, ('right',), state='failed')
        texts = [b['text'] for b in self.tabs()['approved'][mid]['badges']]
        self.assertIn('Choose views', texts)
        self.assertIn('view failed 1', texts)
        item = mv.item(self.root, mid)
        self.assertEqual(item['phase'], 'choose')
        self.assertEqual([(x['name'], x['exists'], x['enabled'], x['retry']) for x in item['views']],
                         [('front', True, True, False), ('left', True, True, False), ('back', True, True, False), ('right', False, False, True)])
        r = self.act(mid, 'views', enabled={'left': False})
        self.assertEqual(r['ticks'], {'front': True, 'left': False, 'back': True})
        self.assertEqual(r['views_rev'], item['views_rev'])                                       # ticks are not part of the revision
        with self.assertRaises(ValueError):
            self.act(mid, 'views', enabled={'right': True})
        with self.assertRaises(ValueError):                                                       # F8: enabled maps are refused for convert
            self.act(mid, 'convert', enabled={'left': True}, views_rev=item['views_rev'])
        with self.assertRaises(ValueError):
            self.act(mid, 'convert', files=[], views_rev=item['views_rev'])
        with self.assertRaises(ValueError):
            self.act(mid, 'convert', files=['front.png'])                                         # no views_rev
        r = self.convert(mid, ['front.png', 'left.png', 'back.png'])
        self.assertEqual(r['frozen']['files'], ['front.png', 'left.png', 'back.png'])
        vdir = self.mroot / f'pipeline/Giant Rat/views/{v}'
        self.assertEqual(r['frozen']['sha256']['left.png'], hashlib.sha256((vdir / 'left.png').read_bytes()).hexdigest())
        task = taskqueue.for_root(self.root).get(r['task'])
        self.assertEqual((task['lane'], task['state'], task['max_attempts'], task['set_name']),
                         ('monster_convert', 'queued', 1, f'monster:Giant Rat/{v}'))
        self.assertEqual(task['args']['files'], ['front.png', 'left.png', 'back.png'])
        with self.assertRaises(guard.Busy):
            self.convert(mid, ['front.png'])
        r = self.act(mid, 'view_retry', view='right')
        self.assertEqual(r['state'], 'pending')

    def test_convert_takes_exactly_the_listed_files_and_checks_the_revision(self):
        """F8: nothing is merged in (a view that arrived later stays out); a stale views_rev is 409 changed; a missing file 409 missing."""
        mid, v = self.pick_rat()
        self.finish_views('Giant Rat', v, ('left',))
        rev = mv.item(self.root, mid)['views_rev']
        self.finish_views('Giant Rat', v, ('right',))                                             # arrives while the dialog is open
        with self.assertRaises(guard.Changed) as caught:
            self.act(mid, 'convert', files=['front.png'], views_rev=rev)
        self.assertEqual(caught.exception.code, 'changed')
        with self.assertRaises(mv.Missing) as caught:
            self.convert(mid, ['front.png', 'back.png'])
        self.assertEqual((caught.exception.code, caught.exception.extra['file']), ('missing', 'back.png'))
        r = self.convert(mid, ['front.png'])
        self.assertEqual(r['frozen']['files'], ['front.png'])                                    # right.png exists, never added
        vj = json.loads((self.mroot / f'pipeline/Giant Rat/views/{v}/views.json').read_text())
        self.assertEqual({x['name']: x['enabled'] for x in vj['views']}, {'front': True, 'left': False, 'right': False})

    def test_unattended_waits_for_every_planned_task_to_be_terminal(self):
        """F9: PNGs that appeared while their jobs still run do not end the wait."""
        mid, v = self.pick_rat(unattended=True)
        self.assertEqual(monster_lane.tick(self.root)['frozen'], [])
        self.finish_views('Giant Rat', v, state='launching_with_png')
        out = monster_lane.tick(self.root)
        self.assertEqual((out['frozen'], out['queued']), ([], []))
        self.assertEqual(mv.item(self.root, mid)['phase'], 'generating')
        self.finish_views('Giant Rat', v, ('left', 'back'))
        self.finish_views('Giant Rat', v, ('right',), state='failed')
        (mv.views_dir(self.mroot, 'Giant Rat', v) / 'right.png').unlink()
        out = monster_lane.tick(self.root)
        self.assertEqual((out['frozen'], out['queued']), ([f'Giant Rat/{v}'], [f'Giant Rat/{v}']))
        vj = json.loads((self.mroot / f'pipeline/Giant Rat/views/{v}/views.json').read_text())
        self.assertEqual(vj['frozen']['files'], ['front.png', 'left.png', 'back.png'])
        self.assertTrue(vj['frozen']['auto'])
        self.assertEqual(monster_lane.tick(self.root)['queued'], [])

    def test_invalid_view_is_not_available_and_can_be_retried(self):
        """F17: an opaque PNG or one without its provenance record is not a valid view; Retry moves it aside and re-runs the task."""
        mid, v = self.pick_rat()
        self.finish_views('Giant Rat', v, ('left',), opaque=True)
        self.finish_views('Giant Rat', v, ('back',))
        self.finish_views('Giant Rat', v, ('right',))
        (mv.views_dir(self.mroot, 'Giant Rat', v) / 'right.prompts.json').unlink()
        item = mv.item(self.root, mid)
        views = {x['name']: x for x in item['views']}
        self.assertEqual((views['left']['valid'], views['left']['retry']), (False, True))
        self.assertIn('fully transparent', views['left']['problem'])                              # png_check's rule and words
        self.assertIn('provenance', views['right']['problem'])
        self.assertEqual((views['right']['retry'], views['right']['repair']), (False, 'provenance'))   # a good image is repaired, not redrawn
        self.assertEqual((views['back']['valid'], views['back']['retry']), (True, False))
        self.assertEqual(mv.available_views(mv.views_dir(self.mroot, 'Giant Rat', v)), ['front', 'back'])
        with self.assertRaises(mv.Missing):
            self.convert(mid, ['front.png', 'left.png'])
        r = self.act(mid, 'view_retry', view='left')
        self.assertEqual((r['state'], r['moved_aside'][0].startswith('left.png.invalid-')), ('pending', True))
        status = json.loads((self.root / '.codex_jobs/_batch/status' / f'{mv.view_task_name("Giant Rat", v, "left")}.json').read_text())
        self.assertEqual((status['state'], status['attempts']), ('pending', 0))
        self.assertFalse((mv.views_dir(self.mroot, 'Giant Rat', v) / 'left.png').exists())
        with self.assertRaises(ValueError):
            self.act(mid, 'view_retry', view='back')                                              # valid already

    def test_reconvert_moves_aside_as_a_validated_transaction(self):
        mid, v = self.pick_rat()
        self.finish_views('Giant Rat', v)
        self.convert(mid, ['front.png', 'left.png', 'back.png', 'right.png'])
        q = taskqueue.for_root(self.root)
        for task in q.tasks(lane=monster_lane.LANE):
            q.claim(task['id'], 'tok', None)
            q.finish(task['id'], 'tok', True)
        self.converted('Giant Rat', v)
        (self.mroot / f'pipeline/Giant Rat/models/{v}/hyper3d').mkdir()
        with self.assertRaises(guard.Conflict):
            self.convert(mid, ['front.png'])
        with self.assertRaises(ValueError):
            self.convert(mid, ['front.png'], verb='reconvert')                                    # no confirm
        mdir = self.mroot / f'pipeline/Giant Rat/models/{v}'
        rev = mv.item(self.root, mid)['views_rev']
        (mdir / 'chosen_lod.json').write_text('{not json')
        with self.assertRaises(guard.Unreadable):                                                 # an unreadable choice is never "not approved"
            self.act(mid, 'reconvert', files=['front.png'], views_rev=rev, confirm=True)
        (mdir / 'chosen_lod.json').write_text(json.dumps({'lod': '0p4', 'approved': True}))
        with self.assertRaises(guard.AlreadyApproved):
            self.convert(mid, ['front.png'], verb='reconvert', confirm=True)
        (mdir / 'chosen_lod.json').write_text(json.dumps({'lod': '0p4', 'approved': False}))
        before = (self.mroot / f'pipeline/Giant Rat/views/{v}/views.json').read_text()
        # an error after the first move rolls everything back (F6)
        mv.FAULTS['reconvert_moved'] = lambda: (_ for _ in ()).throw(OSError('injected'))
        with self.assertRaises(OSError):
            self.convert(mid, ['front.png'], verb='reconvert', confirm=True)
        mv.FAULTS.clear()
        self.assertTrue((mdir / 'reduced').is_dir() and (mdir / 'hyper3d').is_dir())
        self.assertFalse(list(mdir.glob('*_prev_*')) or (mdir / mv.RECONVERT_JOURNAL).exists())
        self.assertEqual((self.mroot / f'pipeline/Giant Rat/views/{v}/views.json').read_text(), before)
        # a crash after the first move: the journal makes recovery roll it back
        mv.FAULTS['reconvert_moved'] = lambda: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            self.convert(mid, ['front.png'], verb='reconvert', confirm=True)
        mv.FAULTS.clear()
        self.assertTrue((mdir / mv.RECONVERT_JOURNAL).exists())
        self.assertIn(f'Giant Rat/{v}: reconvert rolled back', monster_lane.tick(self.root)['recovered'])
        self.assertTrue((mdir / 'reduced').is_dir() and (mdir / 'hyper3d').is_dir())
        # a freeze that cannot be validated moves nothing
        record = mv.views_dir(self.mroot, 'Giant Rat', v) / 'back.prompts.json'
        saved = record.read_bytes()
        record.unlink()
        with self.assertRaises(mv.Missing):
            self.convert(mid, ['front.png', 'back.png'], verb='reconvert', confirm=True)
        self.assertTrue((mdir / 'reduced').is_dir() and not list(mdir.glob('*_prev_*')))
        record.write_bytes(saved)
        r = self.convert(mid, ['front.png', 'left.png', 'back.png'], verb='reconvert', confirm=True)
        self.assertEqual(r['frozen']['files'], ['front.png', 'left.png', 'back.png'])
        self.assertEqual(sorted(m.split('_prev_')[0] for m in r['moved']), ['hyper3d', 'reduced'])
        self.assertFalse((mdir / 'reduced').exists() or (mdir / mv.RECONVERT_JOURNAL).exists())

    def test_reconvert_and_approve_refuse_while_a_driver_owns_the_variant(self):
        mid, v = self.pick_rat()
        self.finish_views('Giant Rat', v)
        self.convert(mid, ['front.png'])
        q = taskqueue.for_root(self.root)
        task = q.tasks(lane=monster_lane.LANE)[0]
        q.cancel(task['id'], 'test')
        self.converted('Giant Rat', v)
        lock = guard.OsLock(monster_lane.driver_lock_path(self.root, 'Giant Rat', v), wait=0)
        with lock:
            self.assertEqual(monster_lane.active_reason(self.root, 'Giant Rat', v), 'a conversion driver of this variant is running')
            with self.assertRaises(guard.Busy):
                self.convert(mid, ['front.png'], verb='reconvert', confirm=True)
            sha = mv.item(self.root, mid)['lods']['0p4']['sha256']
            with self.assertRaises(guard.Busy):
                self.act(mid, 'approve', lod='0p4', glb_sha256=sha)
        # an unreadable queue never means "nothing runs"
        taskqueue._QUEUES.pop(str(self.root), None)
        db = taskqueue.db_path(self.root)
        for suffix in ('', '-wal', '-shm'):
            Path(str(db) + suffix).unlink(missing_ok=True)
        db.write_bytes(b'this is not a database' * 100)
        with self.assertRaises(guard.Unreadable):
            self.convert(mid, ['front.png'], verb='reconvert', confirm=True)
        self.assertTrue((mv.models_dir(self.mroot, 'Giant Rat', v) / 'reduced').is_dir())


class CompletedTest(MonsterCase):
    """F12: Completed and 3D approve need the generation-bound reduce success record (or the explicit legacy rule) and the LOD digest."""

    def test_verified_reduction_digest_and_legacy_rule(self):
        mid = 'monster:Goblin Shaman/main'                                                        # legacy: manifest + 5 GLBs + materials.json
        self.assertEqual(self.tabs()['3d'][mid]['actions'], [])
        item = mv.item(self.root, mid)
        sha = item['lods']['0p2']['sha256']
        self.assertEqual(sha, hashlib.sha256(b'glb0p2').hexdigest())
        self.assertTrue(item['reduce']['legacy'])
        with self.assertRaises(ValueError):
            self.act(mid, 'approve', lod='0p2')                                                   # no digest
        with self.assertRaises(guard.Changed) as caught:
            self.act(mid, 'approve', lod='0p2', glb_sha256='0' * 64)
        self.assertEqual(caught.exception.code, 'changed')
        r = self.act(mid, 'approve', lod='0p2', glb_sha256=sha)
        self.assertEqual((r['lod'], r['approved'], r['undo']),
                         ('0p2', True, {'verb': 'unapprove', 'id': mid, 'args': {'approval_rev': r['approval_rev']}}))
        self.assertEqual(r['approval_rev'], mv.item(self.root, mid)['approval_rev'])
        choice = json.loads((self.mroot / 'pipeline/Goblin Shaman/models/main/chosen_lod.json').read_text())
        self.assertEqual((choice['glb_sha256'], choice['legacy']), (sha, True))
        card = self.tabs()['completed'][mid]
        self.assertEqual(card['actions'], ['unapprove'])
        self.assertIn('legacy reduction', [b['text'] for b in card['badges']])
        with self.assertRaises(guard.AlreadyApproved):
            self.act(mid, 'approve', lod='0p2', glb_sha256=sha)
        self.assertEqual(card['approval_rev'], r['approval_rev'])                                 # the card's hover Unapprove echoes it
        with self.assertRaises(ValueError):
            self.act(mid, 'unapprove')                                                            # no approval revision: refused
        r = self.unapprove(mid)
        self.assertEqual(r['undo'], {'verb': 'approve', 'id': mid,
                                     'args': {'lod': '0p2', 'glb_sha256': sha, 'approval_rev': r['approval_rev']}})
        self.assertEqual(self.tabs()['3d'][mid]['actions'], ['approve'])
        # an approved choice over an incomplete reduction (no materials.json) is not Completed
        self.act(mid, 'approve', lod='0p2', glb_sha256=sha)
        (self.mroot / 'pipeline/Goblin Shaman/models/main/reduced/materials.json').unlink()
        card = self.tabs()['3d'][mid]
        self.assertEqual(card['actions'], ['unapprove'])
        self.assertIn('not verified', ' '.join(b['text'] for b in card['badges']))

    def test_v2_variant_needs_the_lane_record(self):
        mid, v = self.pick_rat()
        self.finish_views('Giant Rat', v)
        self.convert(mid, ['front.png'])
        self.converted('Giant Rat', v, verify=False)
        (self.mroot / f'pipeline/Giant Rat/models/{v}/chosen_lod.json').write_text(json.dumps({'lod': '0p4', 'approved': False}))
        q = taskqueue.for_root(self.root)
        q.cancel(q.tasks(lane=monster_lane.LANE)[0]['id'], 'test')
        sha = mv.item(self.root, mid)['lods']['0p4']['sha256']
        with self.assertRaises(mv.Unverified):
            self.act(mid, 'approve', lod='0p4', glb_sha256=sha)
        self.assertIn('reduction not verified', ' '.join(b['text'] for b in self.tabs()['3d'][mid]['badges']))
        (self.mroot / f'pipeline/Giant Rat/models/{v}/chosen_lod.json').write_text(json.dumps({'lod': '0p4', 'approved': True}))
        self.assertIn(mid, self.tabs()['3d'])                                                     # approved but unverified: not Completed
        (self.mroot / f'pipeline/Giant Rat/models/{v}/chosen_lod.json').write_text(json.dumps({'lod': '0p4', 'approved': False}))
        mv.write_reduce_ok(mv.models_dir(self.mroot, 'Giant Rat', v), mv.read_views_json(mv.views_dir(self.mroot, 'Giant Rat', v))['frozen'])
        self.assertTrue(self.act(mid, 'approve', lod='0p4', glb_sha256=sha)['approved'])
        self.assertIn(mid, self.tabs()['completed'])
        # only the chosen LOD, no manifest: never Completed
        red = self.mroot / 'pipeline/Kobold Artificer/models/main/reduced'
        red.mkdir(parents=True)
        (red / 'lod_error_0p4.glb').write_bytes(b'glb')
        (red.parent / 'chosen_lod.json').write_text(json.dumps({'lod': '0p4', 'approved': True}))
        self.assertIn('monster:Kobold Artificer/main', self.tabs()['3d'])


# --------------------------------------------------------------------------------------------------------------------- lane

class LaneTest(MonsterCase):
    def frozen_variant(self, views=('left', 'back'), files=None, n=1):
        mid, v = self.pick_rat(n)
        self.finish_views('Giant Rat', v, views)
        out = self.convert(mid, files or ['front.png'] + [f'{n}.png' for n in views])
        return v, taskqueue.for_root(self.root).get(out['task'])

    def mode(self, mode=None, pause=None, since='2026-11-25T10:00:00', own=False):
        batch = self.root / '.codex_jobs/_batch'
        if mode:
            (batch / 'supervisor_mode.json').write_text(json.dumps({'mode': mode, 'since': since}))
        if pause and own:                                       # the supervisor's own pause: its record + the PAUSE mirror (I.11)
            self.supervisor_pause(pause['reason'], pause['scope'], pause.get('since', 's'))
        elif pause:                                             # somebody else's PAUSE (a manual / maintenance pause)
            (batch / 'PAUSE').write_text(json.dumps(pause))

    def test_gate_and_bound_pause_override(self):
        g = monster_lane.gate
        self.assertFalse(g(self.root, {}, 10, True)['open'])
        self.assertTrue(g(self.root, {}, 400, True)['open'])
        self.assertTrue(g(self.root, {'run_now': True}, 10, True)['open'])
        self.mode(pause={'reason': 'user active (views keep running)', 'scope': ['conversions']}, own=True)
        self.assertFalse(g(self.root, {}, 400, True)['open'])
        self.assertTrue(g(self.root, {'run_now': True}, 10, True)['open'])
        self.mode(mode='pause-conversions', pause={'reason': 'manual pause-conversions', 'scope': ['conversions']}, own=True)
        closed = g(self.root, {'run_now': True}, 400, True)
        self.assertEqual((closed['open'], closed['needs_confirm']), (False, True))
        key = closed['pause_id']
        self.assertTrue(g(self.root, {'run_now': True, 'override_pause': key}, 10, True)['open'])
        self.assertFalse(g(self.root, {'run_now': True, 'override_pause': True}, 10, True)['open'])   # an unbound override is no override
        self.mode(mode='pause-conversions', since='2026-11-25T11:00:00')                         # a NEW pause
        self.assertFalse(g(self.root, {'run_now': True, 'override_pause': key}, 10, True)['open'])
        self.mode(mode='run')
        # the supervisor has not ticked yet: its own pause still reflects the EARLIER mode, an independent pause until it is lifted
        stale = g(self.root, {'run_now': True}, 1000, True)
        self.assertEqual((stale['open'], stale['code']), (False, 'paused'))
        self.assertIn('supervisor pause: manual pause-conversions', stale['pause_reason'])
        self.supervisor_resume()
        self.assertTrue(g(self.root, {}, 0, True)['open'])
        self.mode(mode='pause')
        self.assertFalse(g(self.root, {}, 1000, True)['open'])

    def test_gate_cli(self):
        """F2: the gate sub-command the click-time check runs: exit 0 open, 1 + reason when closed."""
        argv = ['gate', '--root', str(self.root), '--task-args', json.dumps({'run_now': True}), '--idle', '10']
        self.assertEqual(monster_lane.main(argv), 0)
        (self.root / '.codex_jobs/_batch/CONVERT_HOLD').write_text('{"reason": "restart"}')
        self.assertEqual(monster_lane.main(argv), 1)
        self.assertEqual(monster_lane.main(['gate', '--root', str(self.root), '--task-args', '[1]']), 1)
        cmd = monster_lane.gate_command(self.root, {'run_now': True}, 10, True)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('CONVERT_HOLD', proc.stdout)

    def test_driver_single_view_from_a_verified_snapshot_resumable_never_pays_twice(self):
        v, task = self.frozen_variant(views=('left',), files=['front.png'])
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, frozen_sha=task['args']['frozen_sha'], run_now=True,
                                              idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual(verdict['status'], 'ok', verdict)
        calls = self.calls()
        self.assertEqual([(c['script'], c['argv'][0]) for c in calls],
                         [('hyper3d_single.py', 'submit'), ('hyper3d_single.py', 'collect'), ('reduce_monster.py', 'Giant Rat/' + v)])
        gen = task['args']['frozen_sha']
        snap = mv.models_dir(self.mroot, 'Giant Rat', v) / 'hyper3d' / f'inputs_{gen}'
        self.assertEqual(Path(calls[0]['argv'][1]), snap)                                       # F7: uploaded from the verified snapshot
        self.assertFalse(os.access(snap / 'front.png', os.W_OK))
        self.assertEqual(calls[0]['argv'][-2:], ['--view', 'front.png'])
        self.assertEqual(calls[1]['argv'][-2:], ['--allow-paid', '--accept-preview'])
        gate = json.loads(calls[1]['gate'])                                                     # F2: the click-time gate of the child
        self.assertEqual((gate['root'], gate['override_pause'], gate['cmd'][3:5]), (str(self.root), True, [str(HERE / 'monster_lane.py'), 'gate']))
        entries = monster_lane.paid_entries(self.root)
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]['id'], entries[0]['status'], entries[0]['credits'], entries[0]['views']),
                         ('hyper3d:m1:geometry', 'confirmed', 0.5, ['front.png']))
        ok = json.loads((mv.models_dir(self.mroot, 'Giant Rat', v) / 'reduced' / mv.REDUCE_OK).read_text())
        self.assertEqual((ok['generation'], ok['materials']), (gen, True))                       # F12
        again = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual(again['status'], 'ok')
        self.assertEqual(len(self.calls()), 3)
        self.assertEqual(self.paid_clicks(), 1)
        self.assertEqual(monster_lane.paid_summary(self.root)['count'], 1)
        self.assertEqual(monster_lane.finish_outcome(self.root, task, {'exit': 0, 'last_json': again})['outcome'], 'done')

    def test_driver_multiview_order(self):
        v, task = self.frozen_variant(views=('left', 'back', 'right'), files=['front.png', 'left.png', 'back.png'])
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual(verdict['status'], 'ok', verdict)
        submit = self.calls()[0]
        self.assertEqual((submit['script'], submit['argv'][-2:]), ('hyper3d_multiview.py', ['--views', 'front.png,left.png,back.png']))

    def test_busy_requeues_and_stop_after_paying_resumes(self):
        v, task = self.frozen_variant()
        os.environ['FAKE_H3D'] = 'busy_submit'
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual(verdict['status'], 'busy')
        outcome = monster_lane.finish_outcome(self.root, task, {'exit': 1, 'last_json': verdict})
        self.assertEqual((outcome['outcome'], outcome['delay']), ('requeue', 300.0))
        os.environ['FAKE_H3D'] = 'stop_collect'
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual((verdict['status'], verdict['step'], verdict['paid']), ('failed', 'collect', True))
        self.assertIn('STOPPED: material_generate', verdict['error'])
        self.assertTrue(any(s.endswith('before_material_generate.png') for s in verdict['screenshots']))
        self.assertEqual(monster_lane.finish_outcome(self.root, task, {'exit': 1, 'last_json': verdict})['outcome'], 'failed')
        self.assertEqual(len(monster_lane.paid_entries(self.root)), 1)
        item = mv.item(self.root, f'monster:Giant Rat/{v}')
        self.assertEqual(item['conversion']['record']['status'], 'submitted')
        self.assertEqual((item['conversion']['paid'], item['conversion']['credits']), (True, 0.5))
        os.environ['FAKE_H3D'] = 'ok'
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual(verdict['status'], 'ok', verdict)
        self.assertEqual([c['argv'][0] for c in self.calls() if c['script'] != 'reduce_monster.py'], ['submit', 'submit', 'collect', 'collect'])
        self.assertEqual(self.paid_clicks(), 1)
        self.assertEqual(len(monster_lane.paid_entries(self.root)), 1)

    def test_gate_closed_waits_reduce_failure(self):
        v, task = self.frozen_variant()
        self.mode(mode='pause-conversions')
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual((verdict['status'], verdict['step']), ('waiting', 'submit'))
        self.assertEqual(monster_lane.finish_outcome(self.root, task, {'exit': 1, 'last_json': verdict})['delay'], 60.0)
        self.assertEqual(self.calls(), [])
        self.mode(mode='views-run')
        os.environ['FAKE_REDUCE'] = 'fail'
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, idle=1000, idle_known=True, say=lambda t: None)
        self.assertEqual((verdict['status'], verdict['step']), ('failed', 'reduce'), verdict)
        os.environ['FAKE_REDUCE'] = 'no_materials'                                                # F12: no materials.json = not done
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, idle=1000, idle_known=True, say=lambda t: None)
        self.assertEqual((verdict['status'], verdict['step']), ('failed', 'reduce'), verdict)
        self.assertIn('materials.json', verdict['error'])
        os.environ.pop('FAKE_REDUCE')
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, idle=1000, idle_known=True, say=lambda t: None)
        self.assertEqual(verdict['status'], 'ok', verdict)
        self.assertEqual([c['script'] for c in self.calls()].count('hyper3d_multiview.py'), 2)

    def test_hold_at_click_time_is_a_wait_not_a_paid_click(self):
        """F2: the hold appears while the collector waits; the child's click-time gate stops it (exit 4) and the lane requeues."""
        v, task = self.frozen_variant()
        os.environ['FAKE_H3D'] = 'hold_during_collect'
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual((verdict['status'], verdict['step']), ('waiting', 'collect'), verdict)
        self.assertIn('CONVERT_HOLD', verdict['error'])
        self.assertEqual(self.paid_clicks(), 0)
        self.assertEqual(monster_lane.finish_outcome(self.root, task, {'exit': 1, 'last_json': verdict})['outcome'], 'requeue')
        # a manual pause at click time without a bound override: closed through the gate command too
        (self.root / '.codex_jobs/_batch/CONVERT_HOLD').unlink()
        os.environ['FAKE_H3D'] = 'ok'
        self.mode(pause={'reason': 'manual', 'scope': ['conversions'], 'since': 's1'})
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual(verdict['status'], 'waiting')
        self.assertEqual(self.paid_clicks(), 0)

    def test_record_inputs_must_be_the_frozen_generation(self):
        """F7: a record made from other bytes is refused before anything is spent; a changed image never reaches the upload."""
        v, task = self.frozen_variant()
        os.environ['FAKE_H3D'] = 'other_inputs'
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual((verdict['status'], verdict['step']), ('failed', 'check'), verdict)
        self.assertIn('other images', verdict['error'])
        self.assertEqual([c['argv'][0] for c in self.calls()], ['submit'])
        self.assertEqual(self.paid_clicks(), 0)
        v2, _task2 = self.frozen_variant(n=2)
        (mv.views_dir(self.mroot, 'Giant Rat', v2) / 'left.png').write_bytes(png('changed after the freeze'))
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v2, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual((verdict['status'], verdict['step']), ('failed', 'check'))
        self.assertIn('changed since the views were chosen', verdict['error'])

    def test_frozen_changed_and_plan_starts(self):
        v, task = self.frozen_variant()
        q = taskqueue.for_root(self.root)
        plan = monster_lane.plan_starts(self.root, idle=10, idle_known=True)
        self.assertEqual((plan['start'], plan['waiting'][task['id']][:7]), ([], 'waiting'))
        plan = monster_lane.plan_starts(self.root, idle=1000, idle_known=True)
        self.assertEqual([t['id'] for t in plan['start']], [task['id']])
        self.assertTrue(self.run_now(task['id'])['run_now'])
        self.assertEqual([t['id'] for t in monster_lane.plan_starts(self.root, idle=10, idle_known=True)['start']], [task['id']])
        self.mode(mode='pause-conversions')
        with self.assertRaises(monster_lane.ConfirmPaused):
            self.run_now(task['id'])
        r = self.run_now(task['id'], confirm=True)
        self.assertTrue(r['override_pause'])
        self.assertEqual(len(monster_lane.plan_starts(self.root, idle=10, idle_known=True)['start']), 1)
        argv = monster_lane.task_argv(self.root, q.get(task['id']))
        self.assertEqual(argv[3:5], [str(HERE / 'monster_lane.py'), 'run'])
        self.assertIn('--run-now', argv)
        self.assertEqual(argv[argv.index('--override-pause') + 1], r['pause_id'])
        vdir = self.mroot / f'pipeline/Giant Rat/views/{v}'
        vj = json.loads((vdir / 'views.json').read_text())
        vj['frozen'] = mv.freeze(vdir, ['front.png'])
        mv.write_views_json(vdir, vj)
        wanted, why = monster_lane.task_wanted(self.root, q.get(task['id']))
        self.assertFalse(wanted)
        self.assertIn('chosen again', why)
        plan = monster_lane.plan_starts(self.root, idle=1000, idle_known=True)
        self.assertEqual([t['id'] for t, _ in plan['cancel']], [task['id']])

    def test_limit_one_active(self):
        v, task = self.frozen_variant()
        mid2, v2 = self.pick_rat(2)
        self.finish_views('Giant Rat', v2, ('left',))
        second = self.convert(mid2, ['front.png'])['task']
        q = taskqueue.for_root(self.root)
        self.assertTrue(q.claim(task['id'], 'tok', None))
        plan = monster_lane.plan_starts(self.root, idle=1000, idle_known=True)
        self.assertEqual((plan['start'], plan['active']), ([], 1))
        self.assertIn(second, plan['waiting'])

    def test_run_now_rows_first_beyond_blocked_rows(self):
        """F18: 50 gated rows never starve a Run now row."""
        q = taskqueue.for_root(self.root)
        ids = [q.enqueue_once(monster_lane.LANE, f'{monster_lane.LANE}:X/{n}:{n:012d}', f'monster:X/{n}', 'convert',
                              {'monster': 'X', 'variant': str(n), 'frozen_sha': f'{n:012d}'}, priority=4, max_attempts=1)[0]
               for n in range(51)]
        q.mark_run_now(ids[-1])
        with mock.patch.object(monster_lane, 'task_check', lambda root, task: {'wanted': True, 'why': None, 'damaged': False}):
            plan = monster_lane.plan_starts(self.root, idle=10, idle_known=True)
        self.assertEqual([t['id'] for t in plan['start']], [ids[-1]])
        self.assertEqual(len(plan['waiting']), 50)

    def test_end_to_end_through_run_task(self):
        v, task = self.frozen_variant()
        self.run_now(task['id'])
        task = taskqueue.for_root(self.root).get(task['id'])
        logs = self.root / '.codex_jobs/_app/logs'
        logs.mkdir(parents=True)
        spec = {'id': task['id'], 'attempt': 1, 'token': 'tok1', 'argv': monster_lane.task_argv(self.root, task), 'env': monster_lane.TASK_ENV,
                'log': str(logs / 'x.log'), 'err': str(logs / 'x.err.log'), 'result': str(logs / 'x.result.json'),
                'claim': str(logs / 'x.claim.json'), 'cwd': str(HERE.parents[2]), 'task': {'key': task['key'], 'lane': task['lane']}}
        (logs / 'x.task.json').write_text(json.dumps(spec))
        subprocess.run([sys.executable, str(HERE / 'run_task.py'), str(logs / 'x.task.json'), '--token', 'tok1'], check=True, timeout=180)
        result = json.loads((logs / 'x.result.json').read_text())
        self.assertEqual(result['exit'], 0, (logs / 'x.log').read_text() + (logs / 'x.err.log').read_text())
        self.assertEqual(result['last_json']['status'], 'ok')
        self.assertEqual(monster_lane.finish_outcome(self.root, task, result)['outcome'], 'done')
        self.assertEqual(monster_lane.lane_status(self.root)['paid']['count'], 1)
        self.assertIn(f'monster:Giant Rat/{v}', self.tabs()['3d'])


class RealRecordTest(MonsterCase):
    """F1: the lane with the REAL rodin_backend record writer (submit / collect with a fake browser session)."""

    def test_free_preview_advances_to_collect_and_ambiguous_clicks_stay_protected(self):
        mid, v = self.pick_rat()
        self.finish_views('Giant Rat', v, ('left',))
        task = taskqueue.for_root(self.root).get(self.convert(mid, ['front.png', 'left.png'])['task'])
        os.environ['FAKE_H3D'] = 'real_unresolved'
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, frozen_sha=task['args']['frozen_sha'], run_now=True,
                                              idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual(verdict['status'], 'ok', verdict)
        record = mv.record_of(mv.models_dir(self.mroot, 'Giant Rat', v))
        self.assertEqual((record['status'], record['pending_action']), ('exported', None))
        self.assertEqual(self.paid_clicks(), 1)
        entries = monster_lane.paid_entries(self.root)
        self.assertEqual([(e['action'], e['status'], e['credits']) for e in entries], [('geometry', 'confirmed', 0.5)])
        self.assertEqual(entries[0]['id'], 'hyper3d:e1690fdd-98aa-4581-a015-2a2a58a787e4:geometry')
        # ambiguous clicks: an unbound free preview and a pending paid click are never resumed by the lane
        self.assertFalse(monster_lane.free_preview_resolved({'pending_action': 'free_preview', 'status': 'submission_attempted',
                                                             'model_id': None, 'model_url': None}))
        self.assertTrue(monster_lane.free_preview_resolved({'pending_action': 'free_preview', 'status': 'submitted', 'model_id': 'abc',
                                                            'model_url': 'https://x/abc'}))
        mid2, v2 = self.pick_rat(2)
        task2 = taskqueue.for_root(self.root).get(self.convert(mid2, ['front.png'])['task'])
        os.environ['FAKE_H3D'] = 'ok'
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v2, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual(verdict['status'], 'ok')
        hyper = mv.models_dir(self.mroot, 'Giant Rat', v2) / 'hyper3d'
        rec = json.loads((hyper / 'studio_submission.json').read_text())
        rec.update(status='submitted', pending_action='geometry')
        (hyper / 'studio_submission.json').write_text(json.dumps(rec))
        for name in hyper.glob('glb_*.glb'):
            name.unlink()
        (mv.models_dir(self.mroot, 'Giant Rat', v2) / 'reduced' / mv.REDUCE_OK).unlink()
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v2, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual((verdict['status'], verdict['step']), ('failed', 'collect'))
        self.assertIn("unresolved 'geometry' click", verdict['error'])
        self.assertEqual(task2['lane'], monster_lane.LANE)


class AccountingTest(MonsterCase):
    """F5: confirmed and uncertain outcomes apart, reconciled by provider action id on every resume, visible write errors."""

    def frozen(self):
        mid, v = self.pick_rat()
        self.finish_views('Giant Rat', v, ('left',))
        self.convert(mid, ['front.png'])
        return mid, v, mv.models_dir(self.mroot, 'Giant Rat', v)

    def test_exported_record_is_reconciled_on_resume(self):
        mid, v, mdir = self.frozen()
        gen = mv.read_views_json(mv.views_dir(self.mroot, 'Giant Rat', v))['frozen']
        hyper = mdir / 'hyper3d'
        snap, problem = monster_lane.snapshot_inputs(hyper, mv.views_dir(self.mroot, 'Giant Rat', v), gen)
        self.assertIsNone(problem)
        (hyper / 'glb_m9.glb').write_bytes(b'glb')
        (hyper / 'studio_submission.json').write_text(json.dumps({
            'backend': 'hyper3d', 'status': 'exported', 'model_id': 'm9', 'model_url': 'https://h/m9', 'pending_action': None,
            'exports': {'GLB': {'path': str(hyper / 'glb_m9.glb'), 'bytes': 3, 'sha256': hashlib.sha256(b'glb').hexdigest()}},
            'actions': {'free_preview': {}, 'geometry': {'confirmed_at': 't', 'observed_cost': 0.5}}, 'credits_committed': 0.5,
            'inputs': [{'name': 'front.png', 'sha256': gen['sha256']['front.png'], 'size': 1}], 'images_uploaded': ['front.png']}))
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual(verdict['status'], 'ok', verdict)
        self.assertEqual([c['script'] for c in self.calls()], ['reduce_monster.py'])
        self.assertEqual(monster_lane.paid_summary(self.root)['count'], 1)
        monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual(len(monster_lane.paid_entries(self.root)), 1)                            # idempotent per provider action id

    def test_uncertain_intent_is_not_counted_and_never_assumed(self):
        record = {'model_id': 'm2', 'actions': {'geometry': {}}, 'pending_action': 'geometry', 'credits_committed': 0.5}
        self.assertEqual(monster_lane.paid_actions(record), [{'action': 'geometry', 'status': 'uncertain', 'credits': None,
                                                              'id': 'hyper3d:m2:geometry', 'provider_id': None, 'evidence': None,
                                                              'model_id': 'm2'}])
        self.assertIsNone(monster_lane.reconcile_paid(self.root, 'Giant Rat', 'main', ['front.png'], self.root / 'r.json', record))
        summary = monster_lane.paid_summary(self.root)
        self.assertEqual((summary['count'], summary['credits'], summary['uncertain']['count']), (0, 0, 1))
        # recheck finding 8: a cleared intent, an observed cost, even an exported record prove no charge
        record |= {'pending_action': None, 'observed_cost_geometry': {'credits': 0.5}, 'status': 'exported', 'stage': 'geometry_preview'}
        self.assertEqual(monster_lane.paid_actions(record)[0]['status'], 'uncertain')
        monster_lane.reconcile_paid(self.root, 'Giant Rat', 'main', ['front.png'], self.root / 'r.json', record)
        summary = monster_lane.paid_summary(self.root)
        self.assertEqual((summary['count'], summary['uncertain']['count'], summary['uncertain']['credits']), (0, 1, 0.5))
        # only the backend's confirmation (actions.<kind>.confirmed_at) counts it
        record['actions'] = {'geometry': {'confirmed_at': '2026-11-25T12:00:00', 'confirmed_by': 'stage', 'provider_action_id': 'pa-7',
                                          'observed_cost': 0.5}}
        monster_lane.reconcile_paid(self.root, 'Giant Rat', 'main', ['front.png'], self.root / 'r.json', record)
        summary = monster_lane.paid_summary(self.root)
        self.assertEqual((summary['count'], summary['credits'], summary['uncertain']['count']), (1, 0.5, 0))
        self.assertEqual(monster_lane.paid_entries(self.root)[-1]['provider_id'], 'pa-7')

    def test_log_write_error_stays_visible_until_repaired(self):
        record = {'model_id': 'm3', 'actions': {'geometry': {'confirmed_at': 't', 'provider_action_id': 'pa-3'}}, 'pending_action': None,
                  'observed_cost_geometry': {'credits': 0.5}}
        rec_path = self.root / 'r3.json'
        rec_path.write_text(json.dumps(record))
        monster_lane.paid_log_path(self.root).mkdir(parents=True)                                 # the log cannot be written
        error = monster_lane.reconcile_paid(self.root, 'Giant Rat', 'main', ['front.png'], rec_path, record)
        self.assertIn('could not be written', error)
        self.assertEqual(len(monster_lane.lane_status(self.root)['paid']['errors']), 1)
        self.assertTrue(monster_lane.repair_accounting(self.root))                                # still failing: still listed
        monster_lane.paid_log_path(self.root).rmdir()
        self.assertEqual(monster_lane.repair_accounting(self.root), [])
        self.assertEqual(monster_lane.lane_status(self.root)['paid']['errors'], [])
        self.assertEqual(monster_lane.paid_summary(self.root)['count'], 1)


class DispositionTest(MonsterCase):
    """F4: Cancel / failure survive a rebuilt queue (persisted per generation); Run now override bound to the pause; Retry clears both."""

    def frozen(self):
        mid, v = self.pick_rat()
        self.finish_views('Giant Rat', v, ('left',))
        return mid, v, self.convert(mid, ['front.png'])['task']

    def test_cancel_survives_a_rebuilt_queue_and_retry_clears_it(self):
        mid, v, task_id = self.frozen()
        self.qop('cancel', task_id)
        vj = mv.read_views_json(mv.views_dir(self.mroot, 'Giant Rat', v))
        gen = mv.frozen_digest(vj['frozen'])
        self.assertEqual(vj['conversion'][gen]['state'], 'cancelled')
        taskqueue._QUEUES.pop(str(self.root), None)                                               # the queue is replaced
        db = taskqueue.db_path(self.root)
        for suffix in ('', '-wal', '-shm'):
            Path(str(db) + suffix).unlink(missing_ok=True)
        out = monster_lane.tick(self.root)
        self.assertEqual((out['queued'], out['restored']), ([], [f'Giant Rat/{v}']))
        q = taskqueue.for_root(self.root)
        rows = q.tasks(lane=monster_lane.LANE)
        self.assertEqual([r['state'] for r in rows], ['cancelled'])
        self.assertEqual(monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start'], [])
        self.assertEqual(mv.item(self.root, mid)['conversion']['state'], 'cancelled')
        self.qop('retry', rows[0]['id'])
        self.assertIsNone(mv.disposition_of(mv.read_views_json(mv.views_dir(self.mroot, 'Giant Rat', v)), gen))
        self.assertEqual([t['id'] for t in monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start']], [rows[0]['id']])

    def test_exact_key_lookup_beyond_5000_rows(self):
        mid, v, task_id = self.frozen()
        self.qop('cancel', task_id)
        conn = sqlite3.connect(str(taskqueue.db_path(self.root)))
        now = time.time()
        conn.executemany('INSERT INTO tasks(lane,key,set_name,unit,args,state,priority,created,updated) VALUES (?,?,?,?,?,?,?,?,?)',
                         [(monster_lane.LANE, f'filler:{n}', 'monster:Z/1', 'convert', '{}', 'done', 4, now, now) for n in range(5100)])
        conn.commit()
        conn.close()
        out = monster_lane.tick(self.root)
        self.assertEqual(out['queued'], [])
        self.assertEqual(taskqueue.for_root(self.root).get(task_id)['state'], 'cancelled')

    def test_failure_is_persisted_before_the_row_fails(self):
        mid, v, task_id = self.frozen()
        q = taskqueue.for_root(self.root)
        q.claim(task_id, 'tok', None)
        orch = orchestrator.Orchestrator(self.root, idle=lambda: 10.0, scan_stale=False)
        orch._finish(q.get(task_id), {'exit': 1, 'last_json': {'status': 'failed', 'step': 'collect', 'error': 'STOPPED: cost'}})
        vj = mv.read_views_json(mv.views_dir(self.mroot, 'Giant Rat', v))
        self.assertEqual(mv.disposition_of(vj, mv.frozen_digest(vj['frozen']))['state'], 'failed')

    def test_run_now_override_is_bound_to_the_confirmed_pause_and_cleared_by_retry(self):
        mid, v, task_id = self.frozen()
        batch = self.root / '.codex_jobs/_batch'
        (batch / 'supervisor_mode.json').write_text(json.dumps({'mode': 'pause-conversions', 'since': 'A'}))
        r = self.qop('run_now', task_id, confirm=True)
        q = taskqueue.for_root(self.root)
        self.assertEqual(q.get(task_id)['args']['override_pause'], r['pause_id'])
        self.assertEqual(len(monster_lane.plan_starts(self.root, idle=10, idle_known=True)['start']), 1)
        self.qop('cancel', task_id)
        (batch / 'supervisor_mode.json').write_text(json.dumps({'mode': 'pause-conversions', 'since': 'B'}))   # a new manual pause
        self.qop('retry', task_id)                                # an ordinary retry
        args = q.get(task_id)['args']
        self.assertEqual((args.get('run_now'), 'override_pause' in args), (False, False))
        self.assertEqual(monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start'], [])
        self.assertFalse(monster_lane.gate(self.root, {'run_now': True, 'override_pause': r['pause_id']}, 10, True)['open'])


class IdentityTest(MonsterCase):
    """F13: ambiguous Windows names, case aliases, persisted ids, junctions and task-name collisions."""

    def test_names(self):
        for bad in ('Rat.', 'Rat ', 'CON', 'nul.txt', 'com1', 'a/b', '..', '.x'):
            with self.assertRaises(ValueError, msg=bad):
                mv.safe_part(bad, 'monster')
        for bad in ('1.', 'aux', 'x..y'):
            with self.assertRaises(ValueError, msg=bad):
                mv.check_variant(bad)
        with self.assertRaises(ValueError):                                                       # a case alias of Goblin Shaman
            self.act('monster:goblin shaman/main', 'approve', lod='0p4', glb_sha256='x')
        with self.assertRaises((ValueError, catalog.NotFound)):
            mv.item(self.root, 'monster:goblin shaman/main')
        self.assertNotEqual(mv.view_task_name('Giant Rat', '1', 'left'), mv.view_task_name('Giant_Rat', '1', 'left'))
        self.assertEqual(mv.lock_path(self.root, 'monsters', 'Giant Rat'), mv.lock_path(self.root, 'monsters', 'giant rat'))

    def test_persisted_ids_and_foreign_descriptors(self):
        (self.mroot / 'concepts/Giant Rat' / mv.STATE_FILE).write_text(json.dumps(
            {'schema': 1, 'concepts': {'codex1': {'stage': 'approved', 'monster': '..', 'variant': '1'}}}))
        card = self.tabs()['concepts']['mconcept:Giant Rat/codex2']
        self.assertIn('state unreadable', [b['text'] for b in card['badges']])
        with self.assertRaises(guard.Unreadable):
            self.act('mconcept:Giant Rat/codex1', 'unapprove')
        (self.mroot / 'concepts/Giant Rat' / mv.STATE_FILE).unlink()
        mid, v = self.pick_rat()
        name = mv.view_task_name('Giant Rat', v, 'left')
        path = self.root / '.codex_jobs/_batch/tasks' / f'{name}.json'
        task = json.loads(path.read_text())
        task['monster'] = 'Someone Else'
        path.write_text(json.dumps(task))
        self.assertTrue(mv.view_task_state(self.root, name, ('Giant Rat', v))['foreign'])
        self.assertIsNone(mv.item(self.root, mid)['views'][1]['task'])
        path.unlink()
        path.write_text(json.dumps(task))
        with mock.patch.object(Path, 'is_file', lambda self: False if self.name == 'left.png' else Path.exists(self)):
            pass
        with self.assertRaises(guard.Conflict):
            with mv.monster_lock(self.root, 'Giant Rat'):
                mv.queue_view_tasks(self.root, self.mroot, 'Giant Rat', v, ('left',))

    @unittest.skipUnless(os.name == 'nt', 'Windows junctions')
    def test_junction_is_refused(self):
        import _winapi
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        models = self.mroot / 'pipeline' / 'Kobold Artificer' / 'models'
        models.mkdir(parents=True)
        _winapi.CreateJunction(str(outside), str(models / 'main'))
        with self.assertRaises(ValueError):
            mv.contained(self.mroot, models / 'main' / 'chosen_lod.json')
        with self.assertRaises(ValueError):
            self.act('monster:Kobold Artificer/main', 'approve', lod='0p4', glb_sha256='x')
        self.assertFalse(list(outside.iterdir()))


class TreeTest(MonsterCase):
    """F14: a live child of a monster attempt keeps the slot; Kill & retry drains the whole tree before the slot is released."""

    def test_child_holds_the_slot_and_kill_drains_it(self):
        mid, v = self.pick_rat()
        self.finish_views('Giant Rat', v, ('left',))
        task_id = self.convert(mid, ['front.png'])['task']
        q = taskqueue.for_root(self.root)
        q.claim(task_id, 'tok', None)
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        q.mark_running(task_id, 'tok', dead.pid, None)                                            # the wrapper and driver are gone
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        try:
            path = monster_lane.children_path(self.root, 'Giant Rat', v)
            guard.write_atomic(path, json.dumps({'set_name': f'monster:Giant Rat/{v}', 'driver': None, 'children': [
                {'pid': child.pid, 'create': monster_lane._create_time(child.pid), 'argv': ['collect']}]}).encode())
            orch = orchestrator.Orchestrator(self.root, idle=lambda: 10.0, scan_stale=False)
            self.assertIn(orch._check_active(q.get(task_id), time.time()), ('orphaned', 'alive'))
            self.assertIn(q.get(task_id)['state'], ('orphan', 'running'))
            self.assertEqual(monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True,
                                                         say=lambda t: None)['status'], 'busy')
            self.assertTrue(monster_lane.active_reason(self.root, 'Giant Rat', v))
            self.assertTrue(monster_lane.live_children(self.root, 'Giant Rat', v))
            q.request_kill(task_id, 'tok')
            self.assertEqual(orch._check_active(q.get(task_id), time.time()), 'killed')
            child.wait(timeout=20)
            self.assertIsNotNone(child.poll())
            self.assertEqual(q.get(task_id)['state'], 'queued')
            self.assertFalse(path.exists())
        finally:
            if child.poll() is None:
                child.kill()


class StartupHoldTest(MonsterCase):
    def test_monster_dispatcher_waits_for_the_manager_hold(self):
        """F2: with managed services no monster conversion starts before `hold_asserted` is set."""
        mid, v = self.pick_rat()
        self.finish_views('Giant Rat', v, ('left',))
        task_id = self.convert(mid, ['front.png'])['task']
        (self.root / '.codex_jobs/_batch/supervisor_mode.json').write_text(json.dumps({'mode': 'run'}))
        ready = threading.Event()
        orch = orchestrator.Orchestrator(self.root, idle=lambda: 1000.0, scan_stale=False, monster_ready=ready)
        with mock.patch.object(orch, '_spawn', lambda task: True) as _spawn:
            self.assertEqual(orch._start_monsters(time.time(), 1000.0), 0)
            ready.set()
            self.assertEqual(orch._start_monsters(time.time(), 1000.0), 1)
        self.assertEqual(taskqueue.for_root(self.root).get(task_id)['state'], 'queued')


class DamagedTest(MonsterCase):
    """F16: one damaged variant is a read-only card; the catalog, the item and the unattended scanner keep working for the others."""

    def test_scalar_views_isolated(self):
        mid, v = self.pick_rat(unattended=True)
        mid2, v2 = self.pick_rat(2, unattended=True)
        self.finish_views('Giant Rat', v2)
        (mv.views_dir(self.mroot, 'Giant Rat', v) / 'views.json').write_text(json.dumps({'schema': 1, 'views': 3, 'frozen': None}))
        catalog.invalidate()
        tabs = {tab: {c['id']: c for c in cards} for tab, cards in catalog.state(self.root)['tabs'].items()}
        card = tabs['approved'][mid]
        self.assertEqual(card['actions'], [])
        self.assertTrue(card['badges'][0]['text'].startswith('damaged:'))
        self.assertIn('Choose views', [b['text'] for b in tabs['approved'][mid2]['badges']] + ['Choose views'])
        item = catalog.item(self.root, mid)
        self.assertIn('views is not a list', item['damaged']['error'])
        out = monster_lane.tick(self.root)
        self.assertEqual(out['frozen'], [f'Giant Rat/{v2}'])
        self.assertTrue(any('views is not a list' in e for e in out['errors']))
        with self.assertRaises(guard.Unreadable):
            self.act(mid, 'views', enabled={'front': True})


# --------------------------------------------------------------------------------------------------------------- migration

class MigrationTest(MonsterCase):
    def test_plan_apply_idempotent_no_moves(self):
        before = sorted(p.relative_to(self.mroot).as_posix() for p in self.mroot.rglob('*') if p.is_file())
        (self.mroot / 'concepts' / 'Warhorse' / mv.STATE_FILE).write_text(json.dumps(
            {'schema': 1, 'concepts': {'codex3': {'stage': 'provisional', 'time': 't'}}}))
        plan = mv.migration_plan(self.root)
        folders = {f['folder']: f for f in plan['folders']}
        self.assertEqual(folders['Warhorse']['approved'], [{'key': 'codex1', 'monster': 'War Horse', 'variant': '1', 'how': 'variant_names'}])
        self.assertEqual(folders['Warhorse']['archive'], ['codex2'])
        hob = {a['key']: a for a in folders['Hobgoblin']['approved']}
        self.assertEqual((hob['codex2']['variant'], hob['codex2']['how']), ('2', 'sha256'))
        self.assertEqual(folders['Goblin Shaman']['approved'], [{'key': 'codex4', 'monster': 'Goblin Shaman', 'variant': 'main', 'how': 'override'}])
        self.assertEqual(plan['totals']['approved'], 4)
        self.assertEqual(plan['importable'], [{'monster': 'Hobgoblin', 'variant': '1'}, {'monster': 'Hobgoblin', 'variant': '2', 'how': 'sha256'}])
        self.assertFalse((self.mroot / 'concepts' / 'Giant Rat' / mv.STATE_FILE).exists())
        result = mv.apply_migration(self.root, plan)
        self.assertEqual(result['written']['Warhorse'], 2)
        tabs = self.tabs()
        self.assertFalse([i for i in tabs['concepts'] if i.startswith('mconcept:')])
        self.assertEqual(len([i for i in tabs['archive'] if i.startswith('mconcept:')]), 4)
        again = mv.migration_plan(self.root)
        self.assertEqual((again['totals']['archive'], again['totals']['approved']), (0, 0))
        after = sorted(p.relative_to(self.mroot).as_posix() for p in self.mroot.rglob('*') if p.is_file())
        self.assertEqual(sorted(set(after) - set(before)), sorted(f'concepts/{f}/{mv.STATE_FILE}' for f in
                                                                  ('Warhorse', 'Hobgoblin', 'Goblin Shaman', 'Giant Rat')))
        self.assertFalse(set(before) - set(after))

    def test_migrated_picks_are_shown_and_importable(self):
        """F15: a sha-only pick gets an Import card; a War Horse base pick imports from `first round/`; armour variants stay manual;
        an approved concept with nothing to import is a manual-only card with its source (never hidden)."""
        fr = self.mroot / 'pipeline/War Horse/first round'
        fr.mkdir()
        (fr / '1-front.png').write_bytes(png('wh first round 1'))
        (fr / '1-front.colours.json').write_text(json.dumps(COLOURS))
        (self.mroot / 'pipeline/War Horse/1-plate-front.png').write_bytes(png('wh plate'))
        mv.apply_migration(self.root)
        tabs = self.tabs()
        hob2 = tabs['approved']['monster:Hobgoblin/2']
        self.assertEqual(hob2['actions'], ['import'])
        self.assertIn('monster:War Horse/1', tabs['approved'])
        self.assertEqual(tabs['approved']['monster:War Horse/1']['actions'], ['import'])
        self.assertNotIn('monster:War Horse/1-plate', [i for i, c in tabs['approved'].items() if c['actions'] == ['import']])
        self.assertNotIn('War Horse', [i['monster'] for i in mv.migration_plan(self.root)['importable'] if i['variant'] == '1-plate'])
        r = self.act('monster:Hobgoblin/2', 'import')
        self.assertEqual((r['variant'], r['how']), ('2', 'migration'))
        names = json.loads((self.mroot / 'pipeline/Hobgoblin/variant_names.json').read_text())
        self.assertEqual(names['variants']['2']['was'], 'codex2')
        r = self.act('monster:War Horse/1', 'import')
        self.assertEqual((self.mroot / 'pipeline/War Horse/views/1/front.png').read_bytes(), png('wh first round 1'))
        with self.assertRaises(ValueError):
            self.act('monster:War Horse/1-plate', 'import')
        # an approved concept with no flat front: manual only, with its source
        state = self.state('Giant Rat')
        state['concepts']['codex1'] = {'stage': 'approved', 'time': 't', 'monster': 'Giant Rat', 'variant': '5'}
        (self.mroot / 'pipeline/Giant Rat').mkdir(parents=True)
        mv.write_state(self.mroot / 'concepts/Giant Rat', state)
        card = self.tabs()['approved']['monster:Giant Rat/5']
        self.assertEqual(card['actions'], [])
        self.assertIn('source concepts/Giant Rat/codex1-front.png', ' '.join(b['text'] for b in card['badges']))
        self.assertEqual(mv.item(self.root, 'monster:Giant Rat/5')['phase'], 'manual')

    def test_cli_dry_run_writes_report_only(self):
        out = subprocess.run([sys.executable, '-X', 'utf8', str(HERE / 'monsters_v2.py'), 'migrate', '--root', str(self.root)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout.splitlines()[0])['archive'], 5)
        self.assertTrue((self.root / '.codex_jobs/_app/monster_migration_report.md').is_file())
        self.assertFalse(list(self.mroot.rglob(mv.STATE_FILE)))


# ------------------------------------------------------------------------------------------------------------------ wiring

class WiringTest(MonsterCase):
    """catalog / actions / server with the monster modules wired in (PHASE5 H)."""

    def cat(self):
        catalog.invalidate()
        return {tab: {c['id']: c for c in cards} for tab, cards in catalog.state(self.root)['tabs'].items()}

    def perform(self, item_id, verb, **args):
        return actions.perform(self.root, {'id': item_id, 'verb': verb, 'args': args})

    def test_catalog_cards_per_stage(self):
        tabs = self.cat()
        cid = 'mconcept:Giant Rat/codex1'
        self.assertEqual(tabs['concepts'][cid]['actions'], ['provisional', 'approve', 'reject'])
        self.assertEqual(tabs['3d']['monster:Goblin Shaman/main']['type'], 'monster')
        self.assertEqual(tabs['approved']['monster:Kobold Artificer/main']['stage'], 'approved')
        self.assertEqual(tabs['approved']['monster:Hobgoblin/1']['actions'], ['import'])
        self.perform(cid, 'provisional')
        self.perform(cid, 'archive')
        self.assertEqual(self.cat()['archive'][cid]['actions'], ['approve', 'unarchive'])
        self.perform(cid, 'unarchive')
        self.perform(cid, 'reject')
        self.perform(cid, 'restore')
        r = self.perform(cid, 'approve', unattended=False)
        self.assertIn(f'monster:Giant Rat/{r["variant"]}', self.cat()['approved'])
        self.converted('Goblin Shaman', 'main', approved=True)
        self.assertIn('monster:Goblin Shaman/main', self.cat()['completed'])
        self.assertEqual(catalog.item(self.root, 'monster:Goblin Shaman/main')['stage'], 'completed')
        for bad in ('mconcept:Nope/codex1', 'monster:Goblin Shaman/9', 'monster:Nobody/1'):
            with self.assertRaises(catalog.NotFound):
                catalog.item(self.root, bad)
        with self.assertRaises(ValueError):
            catalog.item(self.root, 'mconcept:../x/codex1')

    def test_item_ui_shape(self):
        r = self.perform('mconcept:Giant Rat/codex1', 'approve')
        mid, v = r['id'], r['variant']
        item = catalog.item(self.root, mid)
        self.assertEqual((item['phase'], item['legacy'], item['importable'], item['frozen'], item['conversion']),
                         ('generating', False, False, None, None))
        self.assertTrue(isinstance(item['views_rev'], int) and 0 <= item['views_rev'] < 2 ** 53)
        self.assertEqual([(x['name'], bool(x['url']), x['task'] and x['task']['state']) for x in item['views']],
                         [('front', True, None), ('left', False, 'queued'), ('back', False, 'queued'), ('right', False, 'queued')])
        self.finish_views('Giant Rat', v, ('left', 'back'))
        self.finish_views('Giant Rat', v, ('right',), state='failed')
        item = catalog.item(self.root, mid)
        self.assertEqual((item['views'][3]['task']['state'], item['views'][3]['retry']), ('failed', True))
        self.perform(mid, 'convert', files=['front.png', 'left.png'], views_rev=item['views_rev'])
        item = catalog.item(self.root, mid)
        self.assertEqual(item['frozen']['names'], ['front', 'left'])
        c = item['conversion']
        self.assertEqual((c['state'], c['step'], c['paid'], c['credits'], c['waiting_reason']), ('waiting', 'submit', False, None, 'user_present'))
        item = catalog.item(self.root, 'monster:Hobgoblin/1')
        self.assertEqual((item['phase'], item['importable']), ('import', True))
        self.assertTrue(item['views'][0]['url'].startswith('/mfiles/pipeline/Hobgoblin/1-front.png'))

    def test_queue_ops_and_state(self):
        r = self.perform('mconcept:Giant Rat/codex1', 'approve')
        mid, v = r['id'], r['variant']
        self.finish_views('Giant Rat', v, ('left',))
        self.finish_views('Giant Rat', v, ('back',), state='failed')
        name = mv.view_task_name('Giant Rat', v, 'back')
        self.assertEqual(actions.queue_op(self.root, {'op': 'view_retry', 'name': name})['state'], 'pending')
        task_id = self.perform(mid, 'convert', files=['front.png'], views_rev=catalog.item(self.root, mid)['views_rev'])['task']
        self.qop('cancel', task_id)
        out = self.qop('retry', task_id)
        self.assertEqual(out['state'], 'queued')
        qs = orchestrator.queue_state(self.root, True)
        self.assertEqual((qs['monsters']['lane'], qs['monsters']['limit'], qs['monsters']['totals']), ('monster_convert', 1, {'queued': 1}))
        self.assertEqual(qs['monsters']['paid']['count'], 0)
        self.assertIn('code', qs['monsters']['gate'])

    def test_http_routes(self):
        srv = server.create_server(self.root, port=0, web=HERE.parent / 'web', extra_hosts=[])
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            def call(method, path, body=None):
                conn = http.client.HTTPConnection('127.0.0.1', srv.server_port)
                head = {'Content-Type': 'application/json', 'X-Token': srv.token} if body is not None else {}
                conn.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=head)
                resp = conn.getresponse()
                data = json.loads(resp.read() or b'null')
                conn.close()
                return resp.status, data
            status, state = call('GET', '/api/v2/state')
            self.assertEqual(status, 200)
            self.assertEqual(call('GET', '/api/v2/item?id=mconcept:Nope/codex1')[0], 404)
            status, r = call('POST', '/api/v2/action', {'id': 'mconcept:Giant Rat/codex1', 'verb': 'approve', 'args': {'unattended': False}})
            self.assertEqual((status, r['variant']), (200, 'main'))
            mid = r['id']
            self.finish_views('Giant Rat', 'main')
            from urllib.parse import quote
            rev = call('GET', '/api/v2/item?id=' + quote(mid))[1]['views_rev']
            status, r0 = call('POST', '/api/v2/action', {'id': mid, 'verb': 'convert', 'args': {'enabled': {'front': True}}})
            self.assertEqual(status, 400)
            status, r0 = call('POST', '/api/v2/action', {'id': mid, 'verb': 'convert', 'args': {'files': ['front.png'], 'views_rev': 1}})
            self.assertEqual((status, r0['code']), (409, 'changed'))
            status, r0 = call('POST', '/api/v2/action', {'id': mid, 'verb': 'convert', 'args': {'files': ['front.png', 'nope.png'], 'views_rev': rev}})
            self.assertEqual(status, 400)
            (mv.views_dir(self.mroot, 'Giant Rat', 'main') / 'left.prompts.json').rename(
                mv.views_dir(self.mroot, 'Giant Rat', 'main') / 'left.prompts.json.bak')
            rev = call('GET', '/api/v2/item?id=' + quote(mid))[1]['views_rev']
            status, r0 = call('POST', '/api/v2/action', {'id': mid, 'verb': 'convert', 'args': {'files': ['front.png', 'left.png'], 'views_rev': rev}})
            self.assertEqual((status, r0['code'], r0['file']), (409, 'missing', 'left.png'))
            status, r = call('POST', '/api/v2/action', {'id': mid, 'verb': 'convert', 'args': {'files': ['front.png'], 'views_rev': rev}})
            self.assertEqual(status, 200, r)
            status, r2 = call('POST', '/api/v2/action', {'id': mid, 'verb': 'convert', 'args': {'files': ['front.png'], 'views_rev': rev}})
            self.assertEqual((status, r2['code']), (409, 'busy'))
            (self.root / '.codex_jobs/_batch/supervisor_mode.json').write_text(json.dumps({'mode': 'pause-conversions', 'since': 'x'}))
            self.assertEqual(call('POST', '/api/v2/queue', {'op': 'run_now', 'id': r['task']})[0], 400)   # a row number alone
            ident = {'id': r['task']} | self.ident(r['task'])
            status, r3 = call('POST', '/api/v2/queue', {'op': 'run_now'} | ident)
            self.assertEqual((status, r3['code']), (409, 'confirm'))
            # the challenge over HTTP: the exact pauses to show (final-live review), each id = its source + file revision
            self.assertEqual([(p['kind'], p['reason']) for p in r3['pauses']], [('mode', 'mode pause-conversions')])
            self.assertEqual(r3['pause_id'], r3['pauses'][0]['pause_id'])
            self.assertEqual(r3['pause_id'], call('GET', '/api/v2/state')[1]['status']['pause_id'])
            self.assertEqual(r3['pause_id'], call('GET', '/api/v2/queue')[1]['mode']['pause_id'])
            status, r4 = call('POST', '/api/v2/queue', {'op': 'run_now', 'confirm': True} | ident)
            self.assertEqual((status, r4['code']), (409, 'confirm'))                               # confirm without the pause it is for
            status, r4 = call('POST', '/api/v2/queue', {'op': 'run_now', 'confirm': True, 'pause_id': r3['pause_id']} | ident)
            self.assertEqual((status, r4['override_pause']), (200, True))
            status, r5 = call('POST', '/api/v2/queue', {'op': 'cancel', 'id': r['task'], 'key': ident['key'], 'generation': '0' * 12})
            self.assertEqual((status, r5['code']), (409, 'changed'))
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join()


class OrchestratorLaneTest(MonsterCase):
    """The lane through the real orchestrator tick: fake Hyper3D / reduce scripts in the temp monster root, run_task.py wrappers."""

    def setUp(self):
        super().setUp()
        self.orch = orchestrator.Orchestrator(self.root, idle=lambda: 10.0, scan_stale=False)
        self.orch.logs.mkdir(parents=True, exist_ok=True)

    def frozen(self, views=('left', 'back')):
        mid, v = self.pick_rat()
        self.finish_views('Giant Rat', v, views)
        out = self.convert(mid, ['front.png'] + [f'{n}.png' for n in views])
        return v, out['task']

    def drive(self, done, timeout=120.0):
        end = time.time() + timeout
        while time.time() < end:
            self.orch.tick()
            if done():
                return
            time.sleep(0.2)
        self.fail('timeout: ' + json.dumps([(t['id'], t['lane'], t['state'], t['error']) for t in taskqueue.for_root(self.root).tasks()]))

    def task(self, task_id):
        return taskqueue.for_root(self.root).get(task_id)

    def test_tick_gate_run_now_and_finish(self):
        v, task_id = self.frozen()
        self.orch.tick()
        self.assertEqual(self.task(task_id)['state'], 'queued')
        guard.write_atomic(self.root / '.codex_jobs/_batch/CONVERT_HOLD', json.dumps({'since': 'now', 'reason': 'supervisor restart'}).encode())
        out = self.qop('run_now', task_id)
        self.assertIn('CONVERT_HOLD', out['warning'])
        self.orch.tick()
        self.assertEqual(self.task(task_id)['state'], 'queued')
        self.assertEqual(catalog.item(self.root, f'monster:Giant Rat/{v}')['conversion']['waiting_reason'], 'held')
        (self.root / '.codex_jobs/_batch/CONVERT_HOLD').unlink()
        with mock.patch.object(orchestrator, 'pool_status', lambda *a, **k: {'limit': 0, 'present': True, 'idle': 10, 'mode': None}):
            self.drive(lambda: self.task(task_id)['state'] in ('done', 'failed'))
        task = self.task(task_id)
        self.assertEqual(task['state'], 'done', (task['error'], task.get('result')))
        self.assertEqual([c['argv'][0] for c in self.calls()], ['submit', 'collect', f'Giant Rat/{v}'])
        self.assertEqual(monster_lane.paid_summary(self.root)['count'], 1)
        self.assertEqual(catalog.item(self.root, f'monster:Giant Rat/{v}')['conversion']['state'], 'done')
        self.orch.tick()
        self.assertEqual(len([t for t in taskqueue.for_root(self.root).tasks() if t['lane'] == monster_lane.LANE]), 1)

    def test_paused_needs_confirm_then_overrides(self):
        v, task_id = self.frozen()
        (self.root / '.codex_jobs/_batch/supervisor_mode.json').write_text(json.dumps({'mode': 'pause-conversions', 'since': 'p1'}))
        self.orch.idle = lambda: 1000.0
        self.orch.tick()
        self.assertEqual(self.task(task_id)['state'], 'queued')
        with self.assertRaises(monster_lane.ConfirmPaused):
            self.qop('run_now', task_id)
        self.assertTrue(self.qop('run_now', task_id, confirm=True)['override_pause'])
        self.drive(lambda: self.task(task_id)['state'] in ('done', 'failed'))
        self.assertEqual(self.task(task_id)['state'], 'done', self.task(task_id)['error'])

    def test_reconcile_never_cancels_and_plan_cancels_unwanted(self):
        v, task_id = self.frozen()
        self.assertEqual(self.orch.reconcile()['cancelled'], 0)
        self.assertTrue(self.orch._approved(self.task(task_id)))
        vdir = self.mroot / f'pipeline/Giant Rat/views/{v}'
        vj = json.loads((vdir / 'views.json').read_text())
        vj['frozen'] = mv.freeze(vdir, ['front.png'])
        mv.write_views_json(vdir, vj)
        self.orch.tick()
        self.assertEqual(self.task(task_id)['state'], 'cancelled')
        tasks = [t for t in taskqueue.for_root(self.root).tasks() if t['lane'] == monster_lane.LANE]
        self.assertEqual(sorted(t['state'] for t in tasks), ['cancelled', 'queued'])

    def test_busy_is_requeued_not_failed(self):
        v, task_id = self.frozen()
        q = taskqueue.for_root(self.root)
        self.assertTrue(q.claim(task_id, 'tok', None))
        self.orch._finish(self.task(task_id), {'exit': 1, 'last_json': {'status': 'busy', 'error': 'STOPPED: Hyper3D profile busy'}})
        task = self.task(task_id)
        self.assertEqual(task['state'], 'queued')
        conv = catalog.item(self.root, f'monster:Giant Rat/{v}')['conversion']
        self.assertEqual((conv['state'], conv['waiting_reason']), ('waiting', 'browser_busy'))
        self.assertTrue(q.claim(task_id, 'tok2', None))
        self.orch._finish(self.task(task_id), {'exit': 1, 'last_json': {'status': 'failed', 'step': 'collect', 'error': 'STOPPED: cost'}})
        self.assertEqual((self.task(task_id)['state'], self.task(task_id)['error']), ('failed', 'STOPPED: cost'))
        conv = catalog.item(self.root, f'monster:Giant Rat/{v}')['conversion']
        self.assertEqual((conv['state'], conv['step'], conv['error']), ('failed', 'collect', 'STOPPED: cost'))


# ------------------------------------------------------------------------------------- recheck before cut-over (2026-11-25)

class RecheckCase(MonsterCase):
    """Shared fixture of the recheck-before-cutover regressions (review_v2_recheck_cutover_20261125; DESIGN_PHASE5.md H.11)."""

    def frozen(self, n=1, views=('left',), files=('front.png',)):
        mid, v = self.pick_rat(n)
        self.finish_views('Giant Rat', v, views)
        task_id = self.convert(mid, list(files))['task']
        vj = mv.read_views_json(mv.views_dir(self.mroot, 'Giant Rat', v))
        return mid, v, task_id, mv.frozen_digest(vj['frozen'])

    def drop_queue(self):
        taskqueue._QUEUES.pop(str(self.root), None)
        db = taskqueue.db_path(self.root)
        for suffix in ('', '-wal', '-shm'):
            Path(str(db) + suffix).unlink(missing_ok=True)

    def vj(self, v):
        return mv.read_views_json(mv.views_dir(self.mroot, 'Giant Rat', v))

    def sleeper(self, seconds=120):
        proc = subprocess.Popen([sys.executable, '-c', f'import time; time.sleep({seconds})'])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc


class RecheckDispositionTest(RecheckCase):
    """Finding 3: idempotent terminal transitions, no older disposition over a newer one, a failed write is outstanding durable work."""

    def test_cancel_stands_when_the_dispatcher_cancels_the_row_first(self):
        mid, v, task_id, gen = self.frozen()
        real = taskqueue.TaskQueue.cancel
        calls = []

        def cancel(queue, tid, detail='', manual_gen=None, expect=None):
            if not calls:                                       # the dispatcher saw the new disposition and cancelled the row first
                calls.append(1)
                self.assertEqual(mv.disposition_of(self.vj(v), gen)['state'], 'cancelled')
                self.assertTrue(real(queue, tid, 'the conversion of this choice was cancelled; use Retry'))
            return real(queue, tid, detail, manual_gen, expect)
        with mock.patch.object(taskqueue.TaskQueue, 'cancel', cancel):
            out = self.qop('cancel', task_id)
        self.assertEqual(out['state'], 'cancelled')
        self.assertEqual(mv.disposition_of(self.vj(v), gen)['state'], 'cancelled')               # the durable cancel was NOT erased
        self.assertEqual(self.qop('cancel', task_id)['state'], 'cancelled')                      # the identical transition again: fine
        self.drop_queue()
        out = monster_lane.tick(self.root)
        self.assertEqual((out['queued'], out['restored']), ([], [f'Giant Rat/{v}']))

    def test_cancel_takes_back_only_its_own_write_never_an_older_state(self):
        mid, v, task_id, gen = self.frozen()
        q = taskqueue.for_root(self.root)
        real = taskqueue.TaskQueue.cancel

        def started(queue, tid, detail='', manual_gen=None, expect=None):
            queue.claim(tid, 'tok', None)                       # the row started between the disposition write and the cancel
            return real(queue, tid, detail, manual_gen, expect)
        with mock.patch.object(taskqueue.TaskQueue, 'cancel', started):
            with self.assertRaises(ValueError):
                self.qop('cancel', task_id)
        self.assertIsNone(mv.disposition_of(self.vj(v), gen))                                     # our own write is taken back
        self.assertEqual(q.get(task_id)['state'], 'starting')
        # ... but not when a newer disposition was written meanwhile (the attempt failed): the newer one stays
        q.requeue(task_id, 'tok', 'again')

        def failed_meanwhile(queue, tid, detail='', manual_gen=None, expect=None):
            queue.claim(tid, 'tok2', None)
            mv.set_disposition(self.root, 'Giant Rat', v, gen, {'state': 'failed', 'reason': 'newer', 'key': 'k'})
            return real(queue, tid, detail, manual_gen, expect)
        with mock.patch.object(taskqueue.TaskQueue, 'cancel', failed_meanwhile):
            with self.assertRaises(ValueError):
                self.qop('cancel', task_id)
        disp = mv.disposition_of(self.vj(v), gen)
        self.assertEqual((disp['state'], disp['reason']), ('failed', 'newer'))
        self.assertGreater(disp['seq'], 1)                                                       # a monotonic sequence per variant

    def test_identical_transition_is_idempotent_and_sequence_guards_undo(self):
        mid, v, task_id, gen = self.frozen()
        first = mv.set_disposition(self.root, 'Giant Rat', v, gen, {'state': 'failed', 'reason': 'a'})
        again = mv.set_disposition(self.root, 'Giant Rat', v, gen, {'state': 'failed', 'reason': 'b'})
        self.assertEqual((first['written'], again['written'], again.get('same'), again['seq']), (True, False, True, first['seq']))
        self.assertEqual(mv.disposition_of(self.vj(v), gen)['reason'], 'a')
        stale = mv.set_disposition(self.root, 'Giant Rat', v, gen, None, expect_seq=first['seq'] - 1)
        self.assertEqual((stale['written'], stale.get('stale')), (False, True))
        self.assertIsNotNone(mv.disposition_of(self.vj(v), gen))

    def test_failed_disposition_write_keeps_the_row_unfinished_and_blocks_reconstruction(self):
        mid, v, task_id, gen = self.frozen()
        q = taskqueue.for_root(self.root)
        q.claim(task_id, 'tok', None)
        orch = orchestrator.Orchestrator(self.root, idle=lambda: 10.0, scan_stale=False)
        result = {'exit': 1, 'last_json': {'status': 'failed', 'step': 'collect', 'error': 'STOPPED: cost'}}
        mv.FAULTS['disposition_write'] = lambda: (_ for _ in ()).throw(OSError('disk full'))
        orch._finish(q.get(task_id), result)
        row = q.get(task_id)
        self.assertEqual(row['state'], 'starting')                                               # NOT finished: the slot stays held
        self.assertIn('could not be recorded', row['error'])
        self.assertTrue(any('could not be recorded' in e for e in orch.errors))
        pending = monster_lane.pending_disposition(self.root, 'Giant Rat', v, gen)
        self.assertEqual((pending['state'], 'disk full' in pending['error']), ('failed', True))
        conv = mv.item(self.root, mid)['conversion']
        self.assertEqual(conv['state'], 'failed')
        self.assertIn('could not be recorded', conv['error'])
        self.assertIn('conversion outcome not recorded yet', ' '.join(b['text'] for b in self.tabs()['approved'][mid]['badges']))
        # the derived database is replaced while the write is still outstanding: nothing is queued again
        self.drop_queue()
        out = monster_lane.tick(self.root)
        self.assertEqual((out['queued'], out['restored']), ([], []))
        self.assertTrue(any('not recorded' in e for e in out['errors']))
        self.assertEqual(taskqueue.for_root(self.root).tasks(lane=monster_lane.LANE), [])
        # the write works again: the tick completes the outstanding work, and only then the generation is restored as FAILED
        mv.FAULTS.clear()
        out = monster_lane.tick(self.root)
        self.assertEqual((out['queued'], out['restored']), ([], [f'Giant Rat/{v}']))
        self.assertIsNone(monster_lane.pending_disposition(self.root, 'Giant Rat', v, gen))
        self.assertEqual(mv.disposition_of(self.vj(v), gen)['state'], 'failed')
        self.assertEqual([t['state'] for t in taskqueue.for_root(self.root).tasks(lane=monster_lane.LANE)], ['failed'])

    def test_row_fails_at_a_later_pass_once_the_record_is_written(self):
        mid, v, task_id, gen = self.frozen()
        q = taskqueue.for_root(self.root)
        q.claim(task_id, 'tok', None)
        orch = orchestrator.Orchestrator(self.root, idle=lambda: 10.0, scan_stale=False)
        result = {'exit': 1, 'last_json': {'status': 'failed', 'step': 'collect', 'error': 'STOPPED: cost'}}
        mv.FAULTS['disposition_write'] = lambda: (_ for _ in ()).throw(OSError('disk full'))
        orch._lost(q.get(task_id))
        self.assertEqual(q.get(task_id)['state'], 'starting')
        mv.FAULTS.clear()
        orch._finish(q.get(task_id), result)
        self.assertEqual(q.get(task_id)['state'], 'failed')
        self.assertIsNone(monster_lane.pending_disposition(self.root, 'Giant Rat', v, gen))

    def test_invalid_disposition_state_is_damage_not_absence(self):
        mid, v, task_id, gen = self.frozen()
        self.qop('cancel', task_id)
        path = mv.views_dir(self.mroot, 'Giant Rat', v) / 'views.json'
        raw = json.loads(path.read_text())
        raw['conversion'][gen]['state'] = 'canceled?'
        path.write_text(json.dumps(raw))
        with self.assertRaises(guard.Unreadable):
            mv.read_views_json(path.parent)
        self.assertIsNotNone(mv.views_problem(raw))
        with self.assertRaises(guard.Unreadable):
            mv.disposition_of({'conversion': {gen: {'state': 'done'}}}, gen)
        self.drop_queue()
        out = monster_lane.tick(self.root)                                                       # indeterminate: never reconstructed
        self.assertEqual((out['queued'], out['restored']), ([], []))
        self.assertTrue(any(f'Giant Rat/{v}' in e for e in out['errors']))
        self.assertTrue(self.tabs()['approved'][mid]['damaged'])
        self.assertEqual(json.loads(path.read_text()), raw)                                      # never replaced


class RecheckIdentityTest(RecheckCase):
    """Finding 4: Run now / Retry / Cancel act on the task the client displayed (key + generation), never on whoever has that row id."""

    def test_stale_row_number_never_reaches_another_variant(self):
        mid1, v1, t1, g1 = self.frozen(1)
        mid2, v2, t2, g2 = self.frozen(2)
        shown = {'id': t2} | self.ident(t2)                     # what the client displays for Giant Rat/<v2>
        self.qop('cancel', t1)
        self.qop('cancel', t2)
        self.drop_queue()
        q = taskqueue.for_root(self.root)
        # the rebuilt queue restores them in the other order: the old row number of v2 now belongs to v1
        for v, g in ((v2, g2), (v1, g1)):
            frozen = self.vj(v)['frozen']
            q.restore_terminal(monster_lane.LANE, monster_lane.task_key('Giant Rat', v, frozen), monster_lane.set_name('Giant Rat', v),
                               'convert', monster_lane._args('Giant Rat', v, frozen), 'cancelled', 'restored', 4, 1)
        by_variant = {t['set_name']: t for t in q.tasks(lane=monster_lane.LANE)}
        self.assertNotEqual(by_variant[f'monster:Giant Rat/{v2}']['id'], shown['id'])
        self.assertEqual(q.get(shown['id'])['set_name'], f'monster:Giant Rat/{v1}')
        out = actions.queue_op(self.root, {'op': 'run_now', 'conversion_seq': mv.disposition_seq(self.vj(v2))} | shown)
        self.assertEqual(out['key'], shown['key'])
        self.assertEqual(q.by_key(shown['key'])['state'], 'queued')                               # the displayed task, found by its key
        self.assertEqual(by_variant[f'monster:Giant Rat/{v1}']['state'], 'cancelled')
        self.assertEqual(q.get(shown['id'])['state'], 'cancelled')                                # the row with that number is untouched
        self.assertIsNotNone(mv.disposition_of(self.vj(v1), g1))

    def test_identity_is_required_and_an_expired_one_is_409_changed(self):
        mid, v, task_id, gen = self.frozen()
        ident = self.ident(task_id)
        for op in ('cancel', 'retry', 'run_now'):
            with self.assertRaises(ValueError):
                actions.queue_op(self.root, {'op': op, 'id': task_id})                             # a row number alone
            with self.assertRaises(ValueError):
                actions.queue_op(self.root, {'op': op, 'id': task_id, 'key': ident['key']})        # no generation
            with self.assertRaises(guard.Changed) as caught:
                actions.queue_op(self.root, {'op': op, 'id': task_id, 'key': ident['key'], 'generation': '0' * 12})
            self.assertEqual(caught.exception.code, 'changed')
            with self.assertRaises(guard.Changed):
                actions.queue_op(self.root, {'op': op, 'id': task_id, 'key': f'{monster_lane.LANE}:Giant Rat/nope:{gen}', 'generation': gen})
        self.assertEqual(taskqueue.for_root(self.root).get(task_id)['state'], 'queued')
        self.assertIsNone(mv.disposition_of(self.vj(v), gen))

    def test_the_state_change_compares_the_identity_in_its_own_transaction(self):
        mid, v, task_id, gen = self.frozen()
        q = taskqueue.for_root(self.root)
        other = {'key': 'monster_convert:Other/1:000000000000', 'generation': '000000000000'}
        for call in (lambda: q.cancel(task_id, 'x', expect=other), lambda: q.mark_run_now(task_id, None, expect=other),
                     lambda: q.retry(task_id, expect=other),
                     lambda: q.cancel(task_id, 'x', expect={'key': self.ident(task_id)['key'], 'generation': 'f' * 12})):
            with self.assertRaises(taskqueue.Expired):
                call()
        row = q.get(task_id)
        self.assertEqual((row['state'], row['args'].get('run_now')), ('queued', False))
        # the row is replaced between the lookup and the write (a rebuilt database): the lane answers 409 changed, nothing is written
        task = monster_lane.resolve_task(self.root, task_id, **self.ident(task_id))
        self.drop_queue()
        q = taskqueue.for_root(self.root)
        q.enqueue_once(monster_lane.LANE, 'monster_convert:Other/1:000000000000', 'monster:Other/1', 'convert', {'frozen_sha': '0' * 12})
        with self.assertRaises(guard.Changed):
            monster_lane.cancel_task(self.root, task)
        self.assertIsNone(mv.disposition_of(self.vj(v), gen))
        self.assertEqual(q.tasks(lane=monster_lane.LANE)[0]['state'], 'queued')

    def test_queue_and_item_expose_the_identity(self):
        mid, v, task_id, gen = self.frozen()
        row = next(t for t in orchestrator.queue_state(self.root, True)['tasks'] if t['id'] == task_id)
        self.assertEqual((row['key'], row['generation']), (self.ident(task_id)['key'], gen))
        conv = mv.item(self.root, mid)['conversion']
        self.assertEqual((conv['key'], conv['generation'], conv['task_id']), (row['key'], gen, task_id))


class RecheckPauseTest(RecheckCase):
    """Finding 5: every pause transition has its own pause_id; a confirmed Run now overrides exactly the pause the user saw."""

    def test_two_pauses_within_one_second_have_different_ids(self):
        first = modes.write_mode(self.root, 'pause')['pause_id']
        modes.write_mode(self.root, 'run')
        second = modes.write_mode(self.root, 'pause')['pause_id']
        self.assertTrue(first and second and first != second)
        self.assertEqual(modes.read_mode(self.root)['since'][:17], modes.read_mode(self.root)['since'][:17])
        self.assertFalse(monster_lane.gate(self.root, {'run_now': True, 'override_pause': first}, 1000, True)['open'])
        self.assertTrue(monster_lane.gate(self.root, {'run_now': True, 'override_pause': second}, 10, True)['open'])
        self.assertIsNone(modes.write_mode(self.root, 'run')['pause_id'])
        # files written by somebody else (the supervisor, its `mode` command) carry no id: each write is its own identity
        batch = self.root / '.codex_jobs/_batch'
        body = json.dumps({'mode': 'pause-conversions', 'since': '2026-11-25T10:00:00'})
        (batch / 'supervisor_mode.json').write_text(body)
        a = modes.read_mode(self.root)['pause_id']
        os.remove(batch / 'supervisor_mode.json')
        (batch / 'supervisor_mode.json').write_text(body)                                         # resume ... pause again, the same second
        b = modes.read_mode(self.root)['pause_id']
        self.assertTrue(a.startswith('mode:file:') and b.startswith('mode:file:') and a != b)   # (source + file revision)
        self.assertEqual(b, modes.read_mode(self.root)['pause_id'])                               # stable while the file is not rewritten
        modes.write_mode(self.root, 'views-run')
        pause = json.dumps({'since': 's', 'reason': 'manual pause-conversions', 'scope': ['conversions']})
        (batch / 'PAUSE').write_text(pause)
        p1 = modes.manual_pause(self.root)
        (batch / 'PAUSE').unlink()
        (batch / 'PAUSE').write_text(pause)
        p2 = modes.manual_pause(self.root)
        self.assertEqual((p1['kind'], p2['kind'], p1['pause_id'] != p2['pause_id']), ('pause', 'pause', True))
        (batch / 'PAUSE').unlink()
        self.supervisor_pause('user active', ['conversions', 'views'])
        self.assertIsNone(modes.manual_pause(self.root))                                          # the idle gate is not a manual pause

    def test_confirm_must_echo_the_pause_the_user_saw(self):
        mid, v, task_id, gen = self.frozen()
        ident = {'id': task_id} | self.ident(task_id)
        seen = modes.write_mode(self.root, 'pause-conversions')['pause_id']
        with self.assertRaises(monster_lane.ConfirmPaused) as caught:
            actions.queue_op(self.root, {'op': 'run_now'} | ident)
        self.assertEqual(caught.exception.extra['pause_id'], seen)
        with self.assertRaises(monster_lane.ConfirmPaused):                                       # `confirm: true` alone is no consent
            actions.queue_op(self.root, {'op': 'run_now', 'confirm': True} | ident)
        modes.write_mode(self.root, 'views-run')                                                  # resumed ... and paused again
        later = modes.write_mode(self.root, 'pause-conversions')['pause_id']
        with self.assertRaises(monster_lane.ConfirmPaused) as caught:                             # the consent was for the EARLIER pause
            actions.queue_op(self.root, {'op': 'run_now', 'confirm': True, 'pause_id': seen} | ident)
        self.assertEqual(caught.exception.extra['pause_id'], later)
        q = taskqueue.for_root(self.root)
        self.assertFalse(q.get(task_id)['args'].get('run_now'))
        out = actions.queue_op(self.root, {'op': 'run_now', 'confirm': True, 'pause_id': later} | ident)
        self.assertEqual((out['override_pause'], out['pause_id'], q.get(task_id)['args']['override_pause']), (True, later, later))
        self.assertEqual(len(monster_lane.plan_starts(self.root, idle=10, idle_known=True)['start']), 1)
        modes.write_mode(self.root, 'pause-conversions')                                          # a later pause is NOT overridden
        self.assertEqual(monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start'], [])

    def test_pause_id_is_exposed_to_the_client(self):
        mid, v, task_id, gen = self.frozen()
        pid = modes.write_mode(self.root, 'pause')['pause_id']
        self.assertEqual(modes.snapshot(self.root, supervisor=(False, ''))['pause_id'], pid)       # queue `mode`
        self.assertEqual(server.status_of(self.root, {})['pause_id'], pid)                        # GET /api/v2/state status
        self.assertEqual(monster_lane.lane_status(self.root)['gate']['pause_id'], pid)            # queue `monsters.gate`
        conv = mv.item(self.root, mid)['conversion']
        self.assertEqual((conv['pause_id'], conv['waiting_reason']), (pid, 'paused'))


class RecheckPaidTest(RecheckCase):
    """Finding 8 (lane side): a repair carries its evidence; only the backend's confirmation counts a charge."""

    def test_repair_survives_a_moved_record(self):
        record = {'model_id': 'm8', 'actions': {'geometry': {'confirmed_at': 't', 'provider_action_id': 'pa-8', 'observed_cost': 0.5}},
                  'pending_action': None}
        rec_dir = self.root / 'hyper3d'
        rec_dir.mkdir()
        rec_path = rec_dir / 'studio_submission.json'
        rec_path.write_text(json.dumps(record))
        monster_lane.paid_log_path(self.root).mkdir(parents=True)                                 # the log cannot be written
        self.assertIn('could not be written', monster_lane.reconcile_paid(self.root, 'Giant Rat', 'main', ['front.png'], rec_path, record))
        stored = monster_lane.lane_status(self.root)['paid']['errors'][0]
        self.assertEqual([(a['id'], a['provider_id'], a['credits'], a['status']) for a in stored['actions']],
                         [('hyper3d:m8:geometry', 'pa-8', 0.5, 'confirmed')])
        rec_dir.rename(self.root / 'hyper3d_prev_20261125T120000')                                # Reconvert moved the record away
        monster_lane.paid_log_path(self.root).rmdir()
        self.assertEqual(monster_lane.repair_accounting(self.root), [])
        summary = monster_lane.paid_summary(self.root)
        self.assertEqual((summary['count'], summary['credits'], summary['errors']), (1, 0.5, []))
        self.assertEqual(monster_lane.paid_entries(self.root)[0]['provider_id'], 'pa-8')
        # a NEW conversion's record at the same path is never mistaken for the old one
        rec_dir.mkdir()
        other = {'model_id': 'm9', 'actions': {'geometry': {'confirmed_at': 't2', 'observed_cost': 0.5}}}
        rec_path.write_text(json.dumps(other))
        monster_lane.paid_log_path(self.root).unlink()
        monster_lane.paid_log_path(self.root).mkdir()
        monster_lane.reconcile_paid(self.root, 'Giant Rat', 'main', ['front.png'], rec_path, record)
        monster_lane.paid_log_path(self.root).rmdir()
        self.assertEqual(monster_lane.repair_accounting(self.root), [])
        self.assertEqual([e['id'] for e in monster_lane.paid_entries(self.root)], ['hyper3d:m8:geometry'])

    def test_an_error_without_evidence_and_without_its_record_stays_open(self):
        guard.write_atomic(monster_lane.paid_errors_path(self.root), json.dumps({
            (self.root / 'gone.json').as_posix(): {'time': 't', 'error': 'old', 'monster': 'Giant Rat', 'variant': 'main', 'views': []}}).encode())
        open_errors = monster_lane.repair_accounting(self.root)
        self.assertEqual(len(open_errors), 1)
        self.assertIn('is gone', open_errors[0])
        self.assertEqual(len(monster_lane.paid_summary(self.root)['errors']), 1)                  # never cleared silently

    def test_unconfirmed_click_is_uncertain_and_stays_protected(self):
        mid, v, task_id, gen = self.frozen()
        os.environ['FAKE_H3D'] = 'unconfirmed_collect'          # the click was sent, the backend did not see the stage advance
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual((verdict['status'], verdict['paid'], verdict['paid_uncertain']), ('failed', False, ['geometry']))
        summary = monster_lane.paid_summary(self.root)
        self.assertEqual((summary['count'], summary['credits'], summary['uncertain']), (0, 0, {'count': 1, 'credits': 0.5}))
        conv = mv.item(self.root, mid)['conversion']
        self.assertEqual((conv['paid'], conv['paid_uncertain'], conv['record']['paid_review']['action']), (False, ['geometry'], 'geometry'))
        os.environ['FAKE_H3D'] = 'ok'                           # never clicked again by the lane
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual(verdict['status'], 'failed')
        self.assertIn("unresolved 'geometry' click", verdict['error'])
        self.assertEqual(self.paid_clicks(), 1)

    def test_changed_upload_copy_stops_before_the_paid_collect(self):
        mid, v, task_id, gen = self.frozen()
        os.environ['FAKE_H3D'] = 'busy_collect'
        monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        upload = mv.models_dir(self.mroot, 'Giant Rat', v) / 'hyper3d' / 'upload_inputs' / 'front.png'
        upload.write_bytes(upload.read_bytes() + b'x')
        os.environ['FAKE_H3D'] = 'ok'
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual((verdict['status'], verdict['step']), ('failed', 'check'))
        self.assertIn('uploaded', verdict['error'])
        self.assertEqual(self.paid_clicks(), 0)


DRIVER_STUB = r'''
import os, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
import monster_lane
tracker = monster_lane._Tracker(sys.argv[2], 'Giant Rat', sys.argv[3], own_process=True)
grand = 'import subprocess, sys, time; p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"]); print(p.pid, flush=True); time.sleep(120)'
child = subprocess.Popen([sys.executable, '-c', grand], stdout=subprocess.PIPE, text=True)
print(tracker.job or '-', child.pid, child.stdout.readline().strip(), flush=True)
time.sleep(120)
'''


class RecheckTreeTest(RecheckCase):
    """Finding 9: ownership before any signal; the whole tree is owned through the driver's exit; incomplete evidence keeps the slot."""

    def running(self, pid=None, create=None):
        mid, v, task_id, gen = self.frozen()
        q = taskqueue.for_root(self.root)
        q.claim(task_id, 'tok', None)
        q.mark_running(task_id, 'tok', pid, create)
        return v, task_id, q

    def tree_file(self, v, **data):
        path = monster_lane.children_path(self.root, 'Giant Rat', v)
        guard.write_atomic(path, json.dumps({'set_name': f'monster:Giant Rat/{v}', 'driver': None, 'children': []} | data).encode())
        return path

    def test_unverified_parent_means_no_signal_at_all(self):
        stranger = self.sleeper()                               # a live process on the recorded wrapper pid, identity NOT proven
        v, task_id, q = self.running(stranger.pid, None)
        child = self.sleeper()
        self.tree_file(v, children=[{'pid': child.pid, 'create': monster_lane._create_time(child.pid), 'argv': ['collect']}])
        orch = orchestrator.Orchestrator(self.root, idle=lambda: 10.0, scan_stale=False)
        q.request_kill(task_id, 'tok')
        self.assertEqual(orch._check_active(q.get(task_id), time.time()), 'kill_failed')
        self.assertIsNone(stranger.poll())                                                       # nothing was signalled:
        self.assertIsNone(child.poll())                                                          # not even the recorded child
        row = q.get(task_id)
        self.assertIn(row['state'], taskqueue.ACTIVE)                                            # the slot stays held, visibly
        self.assertIn('did not signal anything', row['error'])
        self.assertIn('could not be verified', row['error'])

    def test_recorded_process_without_a_create_time_blocks_every_signal(self):
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        v, task_id, q = self.running(dead.pid, None)
        unknown, known = self.sleeper(), self.sleeper()
        self.tree_file(v, children=[{'pid': unknown.pid, 'create': None, 'argv': ['?']},
                                    {'pid': known.pid, 'create': monster_lane._create_time(known.pid), 'argv': ['collect']}])
        plan = monster_lane.tree_plan(self.root, 'Giant Rat', v)
        self.assertEqual(len(plan['blocking']), 1)
        orch = orchestrator.Orchestrator(self.root, idle=lambda: 10.0, scan_stale=False)
        q.request_kill(task_id, 'tok')
        self.assertEqual(orch._check_active(q.get(task_id), time.time()), 'kill_failed')
        self.assertIsNone(unknown.poll())
        self.assertIsNone(known.poll())
        self.assertIn(q.get(task_id)['state'], taskqueue.ACTIVE)

    def test_stale_pid_never_lends_its_descendants(self):
        """A recorded pid whose create time does not match is another process now: neither it nor its children are in the plan."""
        parent = subprocess.Popen([sys.executable, '-c', 'import subprocess, sys, time; subprocess.Popen([sys.executable, "-c", '
                                   '"import time; time.sleep(120)"]); time.sleep(120)'])
        time.sleep(1.0)
        import psutil
        kids = psutil.Process(parent.pid).children(recursive=True)
        self.assertEqual(len(kids), 1)

        def clean():
            for proc in [*kids, parent]:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        self.addCleanup(clean)
        mid, v, task_id, gen = self.frozen()
        self.tree_file(v, children=[{'pid': parent.pid, 'create': monster_lane._create_time(parent.pid) - 500, 'argv': ['old']}])
        plan = monster_lane.tree_plan(self.root, 'Giant Rat', v, [(parent.pid, monster_lane._create_time(parent.pid) - 500)])
        self.assertEqual((plan['procs'], plan['blocking']), ([], []))
        self.assertIsNone(monster_lane.tree_hold(self.root, 'Giant Rat', v))                      # nothing of that record lives

    def test_incomplete_evidence_keeps_the_slot(self):
        mid, v, task_id, gen = self.frozen()
        path = self.tree_file(v, driver={'pid': 999999, 'create': 1.0}, incomplete='a child was being started (python.exe ...)')
        held = monster_lane.tree_hold(self.root, 'Giant Rat', v)
        self.assertIn('not fully known', held)
        self.assertTrue(path.exists())                                                           # kept until somebody checked
        plan = monster_lane.plan_starts(self.root, idle=1000, idle_known=True)
        self.assertEqual((plan['start'], plan['active']), ([], 1))
        self.assertEqual(monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True,
                                                     say=lambda t: None)['status'], 'busy')
        path.write_text('{broken')                                                               # an unreadable record proves nothing either
        self.assertIn('not fully known', monster_lane.tree_hold(self.root, 'Giant Rat', v))
        path.unlink()
        self.assertEqual(len(monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start']), 1)

    def test_surviving_grandchild_is_recorded_and_holds_the_slot(self):
        mid, v, task_id, gen = self.frozen()
        tracker = monster_lane._Tracker(self.root, 'Giant Rat', v)
        note = Path(self.temp.name) / 'grand.pid'
        code = ('import subprocess, sys, time; D = subprocess.DEVNULL; '
                'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=D, stdout=D, stderr=D); '
                'open(sys.argv[1], "w").write(str(p.pid)); time.sleep(2.5)')
        code_out, _err = monster_lane._run([sys.executable, '-c', code, str(note)], HERE, lambda t: None, None, tracker)
        self.assertEqual(code_out, 0)                           # the step is over, its grandchild lives on
        grand = int(note.read_text())
        self.addCleanup(lambda: __import__('psutil').pid_exists(grand) and __import__('psutil').Process(grand).kill())
        tracker.close()
        path = monster_lane.children_path(self.root, 'Giant Rat', v)
        raw = json.loads(path.read_text())                      # (the driver ran inside this test process: it is "gone" now)
        path.write_text(json.dumps(raw | {'driver': None}))
        data = monster_lane.recorded_children(self.root, 'Giant Rat', v)
        self.assertIn(grand, [d['pid'] for d in data['descendants']])                            # the grandchild did not escape the record
        self.assertIn(str(grand), monster_lane.tree_hold(self.root, 'Giant Rat', v))
        self.assertEqual(monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start'], [])
        plan = monster_lane.tree_plan(self.root, 'Giant Rat', v)
        self.assertIn(grand, [p.pid for p in plan['procs']])
        self.assertEqual(monster_lane.drain_tree(self.root, 'Giant Rat', v, plan, 10.0)['problems'], [])
        self.assertFalse(monster_lane.children_path(self.root, 'Giant Rat', v).exists())

    def test_spawn_is_refused_when_it_cannot_be_recorded(self):
        mid, v, task_id, gen = self.frozen()
        tracker = monster_lane._Tracker(self.root, 'Giant Rat', v)
        with mock.patch.object(guard, 'write_atomic', side_effect=OSError('read-only')), \
                mock.patch.object(subprocess, 'Popen') as popen:
            code, err = monster_lane._run([sys.executable, '-c', 'pass'], Path(self.temp.name), lambda t: None, None, tracker)
        self.assertEqual((code, popen.call_count), (-1, 0))
        self.assertIn('could not be recorded', err)
        tracker.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows Job Objects')
    def test_job_object_owns_the_tree_through_the_drivers_exit(self):
        mid, v, task_id, gen = self.frozen()
        stub = Path(self.temp.name) / 'driver_stub.py'
        stub.write_text(DRIVER_STUB)
        driver = subprocess.Popen([sys.executable, str(stub), str(HERE), str(self.root), v], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: driver.poll() is None and driver.kill())
        job, child, grand = driver.stdout.readline().split()
        child, grand = int(child), int(grand)
        import psutil
        self.addCleanup(lambda: [psutil.Process(p).kill() for p in (child, grand) if psutil.pid_exists(p)] and None)
        self.assertTrue(job.startswith(monster_lane.JOB_PREFIX), job)
        state = monster_lane.tree_state(self.root, 'Giant Rat', v)                                # this process takes a handle of the job
        self.assertTrue(state['job'])
        self.assertEqual({p['pid'] for p in state['live']}, {driver.pid, child, grand})
        driver.kill()                                                                            # the driver dies; nothing closes the job
        driver.wait(10)
        deadline = time.time() + 10
        while time.time() < deadline and driver.pid in {p['pid'] for p in monster_lane.tree_state(self.root, 'Giant Rat', v)['live']}:
            time.sleep(0.1)
        state = monster_lane.tree_state(self.root, 'Giant Rat', v)
        self.assertTrue(state['job'])
        self.assertEqual({p['pid'] for p in state['live']}, {child, grand})                       # still owned, grandchild included
        self.assertTrue(psutil.pid_exists(child) and psutil.pid_exists(grand))                    # (KILL_ON_JOB_CLOSE is off)
        self.assertIn('still runs', monster_lane.tree_hold(self.root, 'Giant Rat', v))
        plan = monster_lane.tree_plan(self.root, 'Giant Rat', v)
        self.assertIsNotNone(plan['job'])
        out = monster_lane.drain_tree(self.root, 'Giant Rat', v, plan, 10.0)
        self.assertEqual((out['problems'], out['killed']), ([], 2))
        deadline = time.time() + 10
        while time.time() < deadline and (psutil.pid_exists(child) or psutil.pid_exists(grand)):
            time.sleep(0.1)
        self.assertFalse(psutil.pid_exists(child) or psutil.pid_exists(grand))
        self.assertFalse(monster_lane.children_path(self.root, 'Giant Rat', v).exists())
        self.assertIsNone(monster_lane.tree_hold(self.root, 'Giant Rat', v))


class RecheckApproveTest(RecheckCase):
    """Finding 10 (server side): Completed certifies the approved LOD's sha256 and generation, not its size."""

    def approved(self):
        mid, v, task_id, gen = self.frozen()
        q = taskqueue.for_root(self.root)
        q.claim(task_id, 'tok', None)
        q.finish(task_id, 'tok', True)
        self.converted('Giant Rat', v)
        sha = mv.item(self.root, mid)['lods']['0p4']['sha256']
        self.act(mid, 'approve', lod='0p4', glb_sha256=sha)
        self.assertIn(mid, self.tabs()['completed'])
        return mid, v, gen, mv.models_dir(self.mroot, 'Giant Rat', v)

    def test_replaced_glb_of_equal_length_leaves_completed(self):
        mid, v, gen, mdir = self.approved()
        glb = mdir / 'reduced' / 'lod_error_0p4.glb'
        size = glb.stat().st_size
        time.sleep(0.05)
        glb.write_bytes(b'X' * size)                                                             # other bytes, the same length
        self.assertEqual(mv.reduce_status(mdir, gen)['ok'], True)                                # (the size-only check is blind to it)
        card = self.tabs()['3d'][mid]
        self.assertNotIn(mid, self.tabs()['completed'])
        self.assertEqual(card['actions'], ['unapprove'])
        self.assertIn('not the model that was approved', ' '.join(b['text'] for b in card['badges']))
        # another level changing does not touch the approval of 0p4
        glb.write_bytes(b'glb0p4')
        other = mdir / 'reduced' / 'lod_error_0p8.glb'
        other.write_bytes(b'Y' * other.stat().st_size)
        self.assertIn(mid, self.tabs()['completed'])

    def test_approval_of_another_generation_is_not_completed(self):
        mid, v, gen, mdir = self.approved()
        choice = json.loads((mdir / 'chosen_lod.json').read_text())
        self.assertEqual(choice['generation'], gen)
        choice['generation'] = '0' * 12
        (mdir / 'chosen_lod.json').write_text(json.dumps(choice))
        self.assertIn('another choice of views', ' '.join(b['text'] for b in self.tabs()['3d'][mid]['badges']))
        # an approval without a digest counts only for a legacy reduction (approved before v2 recorded digests)
        (mdir / 'chosen_lod.json').write_text(json.dumps({'lod': '0p4', 'approved': True}))
        self.assertIn('no model digest', ' '.join(b['text'] for b in self.tabs()['3d'][mid]['badges']))
        legacy = self.mroot / 'pipeline/Goblin Shaman/models/main/chosen_lod.json'
        legacy.write_text(json.dumps({'lod': '0p4', 'approved': True}))
        self.assertIn('monster:Goblin Shaman/main', self.tabs()['completed'])


class RecheckStrictReadTest(RecheckCase):
    """Finding 11: a malformed journal / chosen_lod.json blocks the mutations it guards and is never overwritten or removed."""

    def test_malformed_pick_journal_blocks_picks_and_stays(self):
        path = mv.journal_path(self.root, 'Giant Rat')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"op": "pick", "monster": "Giant Rat", "tmp": ".v2_pi')                # a torn write
        before = path.read_bytes()
        with self.assertRaises(guard.Unreadable):
            self.act('mconcept:Giant Rat/codex1', 'approve')
        self.assertEqual(path.read_bytes(), before)                                              # not replaced, not removed
        self.assertFalse((self.mroot / 'pipeline' / 'Giant Rat').exists())
        out = monster_lane.tick(self.root)
        self.assertTrue(any(path.name in e for e in out['errors']))
        self.assertEqual(path.read_bytes(), before)
        card = self.tabs()['concepts']['mconcept:Giant Rat/codex1']
        self.assertEqual(card['actions'], [])                                                    # read-only until it is repaired
        self.assertIn('damaged', ' '.join(b['text'] for b in card['badges']))
        self.assertTrue(self.tabs()['concepts']['mconcept:Hobgoblin/codex3']['actions'])          # other monsters are not affected
        path.unlink()
        self.assertEqual(self.act('mconcept:Giant Rat/codex1', 'approve')['variant'], 'main')
        # a journal that is JSON but no journal of this monster is not dropped either
        path.write_text(json.dumps({'op': 'pick', 'monster': 'Somebody Else'}))
        with self.assertRaises(guard.Unreadable):
            self.act('mconcept:Giant Rat/codex2', 'approve')
        self.assertTrue(path.exists())

    def converted_variant(self):
        mid, v, task_id, gen = self.frozen(views=('left', 'back'))
        q = taskqueue.for_root(self.root)
        q.claim(task_id, 'tok', None)
        q.finish(task_id, 'tok', True)
        self.converted('Giant Rat', v)
        (self.mroot / f'pipeline/Giant Rat/models/{v}/hyper3d').mkdir()
        return mid, v, mv.models_dir(self.mroot, 'Giant Rat', v)

    def test_malformed_reconvert_journal_blocks_the_variant_and_stays(self):
        mid, v, mdir = self.converted_variant()
        item = mv.item(self.root, mid)
        rev, sha = item['views_rev'], item['lods']['0p4']['sha256']
        journal = mdir / mv.RECONVERT_JOURNAL
        journal.write_bytes(b'{"schema": 1, "moves": [["hyper3d", "hyper3d_pr')
        before = journal.read_bytes()
        for verb, args in (('reconvert', {'files': ['front.png'], 'views_rev': rev, 'confirm': True}),
                           ('approve', {'lod': '0p4', 'glb_sha256': sha}), ('views', {'enabled': {'left': False}}),
                           ('view_retry', {'view': 'right'})):
            with self.assertRaises(guard.Unreadable, msg=verb):
                self.act(mid, verb, **args)
        with self.assertRaises(guard.Unreadable):
            actions.set_lod(self.root, {'id': mid, 'lod': '0p2'})
        self.assertEqual(journal.read_bytes(), before)
        self.assertTrue((mdir / 'hyper3d').is_dir() and (mdir / 'reduced').is_dir() and not list(mdir.glob('*_prev_*')))
        out = monster_lane.tick(self.root)
        self.assertTrue(any('reconvert recovery error' in e for e in out['errors']))
        self.assertEqual(journal.read_bytes(), before)
        card = self.tabs()['3d'][mid]                                                            # a read-only damaged card, in its tab
        self.assertEqual((card['actions'], bool(card['damaged'])), ([], True))
        self.assertEqual(mv.item(self.root, mid)['state'], 'damaged')
        # a moves list that is no reconvert journal is refused as well (it would rename arbitrary folders)
        journal.write_text(json.dumps({'schema': 1, 'moves': [['reduced', '../../elsewhere']], 'generation': 'x'}))
        with self.assertRaises(guard.Unreadable):
            self.act(mid, 'approve', lod='0p4', glb_sha256=sha)
        journal.unlink()
        self.assertTrue(self.act(mid, 'approve', lod='0p4', glb_sha256=sha)['approved'])

    def test_malformed_chosen_lod_is_never_overwritten_by_the_lod_writer(self):
        mid, v, mdir = self.converted_variant()
        choice = mdir / 'chosen_lod.json'
        for damage in (b'{"lod": "0p4", "appro', b'[1, 2]'):
            choice.write_bytes(damage)
            with self.assertRaises(guard.Unreadable):
                actions.set_lod(self.root, {'id': mid, 'lod': '0p2'})
            with self.assertRaises(guard.Unreadable):
                self.act(mid, 'approve', lod='0p4', glb_sha256='0' * 64)
            self.assertEqual(choice.read_bytes(), damage)
            self.assertTrue(self.tabs()['3d'][mid]['damaged'])
        choice.unlink()
        self.assertEqual(actions.set_lod(self.root, {'id': mid, 'lod': '0p2'})['lod'], '0p2')


@unittest.skipUnless(os.name == 'nt', 'junctions')
class RecheckContainmentTest(RecheckCase):
    """Finding 14: every nested destination is resolved again right before use; a link or junction on the way is refused."""

    def junction(self, link):
        import _winapi
        outside = Path(self.temp.name) / f'outside_{link.name}'
        outside.mkdir()
        link.parent.mkdir(parents=True, exist_ok=True)
        _winapi.CreateJunction(str(outside), str(link))
        return outside

    def test_driver_refuses_a_linked_hyper3d_folder(self):
        mid, v, task_id, gen = self.frozen()
        outside = self.junction(mv.models_dir(self.mroot, 'Giant Rat', v) / 'hyper3d')
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual((verdict['status'], verdict['step']), ('failed', 'check'))
        self.assertIn('link or junction', verdict['error'])
        self.assertEqual((list(outside.iterdir()), self.calls()), ([], []))                        # nothing written, nothing started

    def test_driver_refuses_a_linked_snapshot_upload_or_reduced_folder(self):
        mid, v, task_id, gen = self.frozen()
        mdir = mv.models_dir(self.mroot, 'Giant Rat', v)
        outside = self.junction(mdir / 'hyper3d' / f'inputs_{gen}')
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual(verdict['status'], 'failed')
        self.assertEqual((list(outside.iterdir()), self.calls()), ([], []))
        os.rmdir(mdir / 'hyper3d' / f'inputs_{gen}')
        up = self.junction(mdir / 'hyper3d' / 'upload_inputs')
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual(verdict['status'], 'failed')
        self.assertEqual((list(up.iterdir()), self.calls()), ([], []))
        os.rmdir(mdir / 'hyper3d' / 'upload_inputs')
        red = self.junction(mdir / 'reduced')
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual(verdict['status'], 'failed')
        self.assertEqual((list(red.iterdir()), self.calls()), ([], []))

    def test_snapshot_and_recovery_destinations_are_checked(self):
        mid, v, task_id, gen = self.frozen()
        mdir, vdir = mv.models_dir(self.mroot, 'Giant Rat', v), mv.views_dir(self.mroot, 'Giant Rat', v)
        outside = self.junction(mdir / 'hyper3d')
        snap, problem = monster_lane.snapshot_inputs(mdir / 'hyper3d', vdir, self.vj(v)['frozen'], self.mroot)
        self.assertIsNone(snap)
        self.assertIn('refusing', problem)
        self.assertEqual(list(outside.iterdir()), [])
        # an interrupted Reconvert whose folder was swapped for a junction: recovery opens no lock and renames nothing through it
        os.rmdir(mdir / 'hyper3d')
        os.rmdir(mdir)
        elsewhere = self.junction(mdir)
        (elsewhere / mv.RECONVERT_JOURNAL).write_text(json.dumps({'schema': 1, 'moves': [['reduced', 'reduced_prev_20261125T000000']],
                                                                  'generation': 'x'}))
        (elsewhere / 'reduced_prev_20261125T000000').mkdir()
        out = mv.recover_reconverts(self.root)
        self.assertTrue(out and 'error' in out[0])
        self.assertEqual(sorted(p.name for p in elsewhere.iterdir()), [mv.RECONVERT_JOURNAL, 'reduced_prev_20261125T000000'])


class RecheckViewsTest(RecheckCase):
    """Finding 15 (app side): png_check is the one validator; a good image with bad provenance is repaired, not redrawn; one
    terminal-state rule for the item phase and the unattended freeze."""

    def test_app_uses_the_runners_validator(self):
        import png_check
        mid, v = self.pick_rat()
        vdir = mv.views_dir(self.mroot, 'Giant Rat', v)
        self.finish_views('Giant Rat', v, ('left', 'back', 'right'))
        from PIL import Image
        Image.new('RGBA', (16, 16), (0, 0, 0, 0)).save(vdir / 'left.png')                         # completely invisible
        Image.new('RGB', (16, 16), (200, 0, 0)).save(vdir / 'back.png')
        for view in ('left', 'back', 'right'):
            self.assertEqual(mv.png_problem(vdir / f'{view}.png'), png_check.png_problem(vdir / f'{view}.png'))
        self.assertIn('solid pixels', mv.view_problem(vdir, 'left'))
        self.assertIn('not RGBA', mv.view_problem(vdir, 'back'))
        self.assertEqual(mv.available_views(vdir), ['front', 'right'])
        with self.assertRaises(mv.Missing):
            mv.freeze(vdir, ['front.png', 'left.png'])                                            # freeze
        with self.assertRaises(mv.Missing):
            self.convert(mid, ['front.png', 'left.png'])                                          # convert
        self.assertEqual(self.act(mid, 'view_retry', view='left')['state'], 'pending')            # retry: the runner would reject it too
        (vdir / 'right.prompts.json').write_text('[1]')                                           # provenance must be a JSON OBJECT
        self.assertEqual(mv.view_problem(vdir, 'right'), 'right.prompts.json: ' + png_check.provenance_problem(vdir / 'right.prompts.json'))

    def test_repair_provenance_keeps_the_image(self):
        mid, v = self.pick_rat()
        vdir = mv.views_dir(self.mroot, 'Giant Rat', v)
        self.finish_views('Giant Rat', v, ('left', 'back', 'right'))
        name = mv.view_task_name('Giant Rat', v, 'left')
        status = self.root / '.codex_jobs/_batch/status' / f'{name}.json'
        guard.write_atomic(status, json.dumps({'state': 'failed', 'reason': 'provenance', 'error': 'left.prompts.json: provenance missing',
                                               'attempts': 1, 'jobs': [{'job': f'{name}-a01'}]}).encode())   # the runner's verdict
        (vdir / 'left.prompts.json').unlink()
        (vdir / 'back.prompts.json').write_text('not json at all')
        image = (vdir / 'left.png').read_bytes()
        views = {x['name']: x for x in mv.item(self.root, mid)['views']}
        self.assertEqual([(views[n]['valid'], views[n]['retry'], views[n]['repair']) for n in ('left', 'back', 'right')],
                         [(False, False, 'provenance'), (False, False, 'provenance'), (True, False, None)])
        with self.assertRaises(ValueError) as caught:
            self.act(mid, 'view_retry', view='left')                                              # never a regeneration
        self.assertIn('view_repair', str(caught.exception))
        self.assertEqual((vdir / 'left.png').read_bytes(), image)
        r = self.act(mid, 'view_repair', view='left')
        self.assertEqual((r['record'], r['moved_aside'], r['prompt_source']), ('left.prompts.json', None, f'.codex_jobs/_batch/tasks/{name}.json'))
        record = json.loads((vdir / 'left.prompts.json').read_text())
        task = json.loads((self.root / '.codex_jobs/_batch/tasks' / f'{name}.json').read_text())
        self.assertEqual((record['job_prompt'], record['view'], record['variant'], record['sha256']),
                         (task['prompt'], 'left', v, hashlib.sha256(image).hexdigest()))
        self.assertEqual(record['prompt'], monster_prompts.image_prompt('left', 'Giant Rat', COLOURS))
        self.assertEqual((vdir / 'left.png').read_bytes(), image)                                 # the image was not touched
        self.assertEqual(json.loads(status.read_text())['state'], 'failed')                       # the runner marks it done (REPAIRED) itself
        r = self.act(mid, 'view_repair', view='back')
        self.assertTrue(r['moved_aside'].startswith('back.prompts.json.invalid-'))                # the bad record is kept aside
        self.assertEqual((vdir / r['moved_aside']).read_text(), 'not json at all')
        self.assertEqual(mv.available_views(vdir), ['front', 'left', 'back', 'right'])
        with self.assertRaises(ValueError):
            self.act(mid, 'view_repair', view='left')                                             # valid already
        (vdir / 'right.png').write_bytes(png('opaque', opaque=True))
        (vdir / 'right.prompts.json').unlink()
        with self.assertRaises(ValueError):
            self.act(mid, 'view_repair', view='right')                                            # a bad IMAGE is Retry's
        self.assertEqual({x['name']: (x['retry'], x['repair']) for x in mv.item(self.root, mid)['views']}['right'], (True, None))

    def test_one_terminal_state_rule_for_the_phase_and_the_unattended_freeze(self):
        mid, v = self.pick_rat(unattended=True)
        vdir = mv.views_dir(self.mroot, 'Giant Rat', v)
        tasks = self.root / '.codex_jobs/_batch/tasks'
        for path in tasks.glob('*.json'):                       # the descriptors are gone (never written, or lost)
            path.unlink()
        for view in ('left', 'back'):
            (vdir / f'{view}.png').write_bytes(png(f'hand {view}'))
            (vdir / f'{view}.prompts.json').write_text('{"prompt": "p"}')
        # right: no descriptor and no image -> BOTH say "still coming"
        self.assertEqual(mv.item(self.root, mid)['phase'], 'generating')
        self.assertEqual(mv.auto_freeze(self.root), [])
        (vdir / 'right.png').write_bytes(png('hand right'))
        # right: a good image without provenance -> still BOTH waiting; the UI offers the repair
        self.assertEqual(mv.item(self.root, mid)['phase'], 'generating')
        self.assertEqual(mv.auto_freeze(self.root), [])
        self.assertEqual({x['name']: x['repair'] for x in mv.item(self.root, mid)['views']}['right'], 'provenance')
        self.act(mid, 'view_repair', view='right')
        # every planned view is a valid file without a descriptor -> BOTH are settled (it used to be `choose` here and a wait forever there)
        self.assertEqual(mv.item(self.root, mid)['phase'], 'choose')
        self.assertEqual(mv.auto_freeze(self.root), [f'Giant Rat/{v}'])
        self.assertEqual(self.vj(v)['frozen']['files'], ['front.png', 'left.png', 'back.png', 'right.png'])


class RecheckDescriptorTest(RecheckCase):
    """Phase 5 row 10: old undigested / unguarded monster view descriptors are found on withdraw and never left to the runner."""

    def legacy(self, v, view='left', guarded=False, **extra):
        name = f'mviews--Giant_Rat--{v}--{view}'                # the name before the canonical digest was part of it
        desc = {'name': name, 'launcher': 'monsters', 'monster': 'Giant Rat', 'variant': v, 'view': view, 'prompt': 'old', 'outputs': []}
        if guarded:
            desc['guard'] = {'file': 'x', 'key': 'pick_gen', 'value': 'y'}
        path = self.root / '.codex_jobs/_batch/tasks' / f'{name}.json'
        guard.write_atomic(path, json.dumps(desc | extra).encode())
        return name, path

    def test_withdraw_finds_and_withdraws_old_names(self):
        cid = 'mconcept:Giant Rat/codex1'
        v = self.act(cid, 'approve')['variant']
        name, path = self.legacy(v)
        self.assertNotIn(name, [mv.view_task_name('Giant Rat', v, x) for x in mv.EXTRA_VIEWS])
        self.assertIn(name, mv.variant_descriptors(self.root, 'Giant Rat', v))
        guard.write_atomic(self.root / '.codex_jobs/_batch/status' / f'{name}.json', json.dumps({'state': 'running', 'attempts': 1}).encode())
        with self.assertRaises(guard.Conflict):                                                  # an old-named job that started blocks the undo
            self.act(cid, 'unapprove', to='concepts')
        self.assertTrue(path.exists())
        (self.root / '.codex_jobs/_batch/status' / f'{name}.json').unlink()
        self.act(cid, 'unapprove', to='concepts')
        self.assertEqual(list((self.root / '.codex_jobs/_batch/tasks').glob('*.json')), [])        # the old name was withdrawn too
        self.assertEqual(len(list((self.root / '.codex_jobs/_batch/tasks').glob('*.withdrawn-*'))), 4)

    def test_unguarded_descriptors_are_reconciled_before_dispatch(self):
        cid = 'mconcept:Giant Rat/codex1'
        v = self.act(cid, 'approve')['variant']
        tasks = self.root / '.codex_jobs/_batch/tasks'
        current = tasks / f'{mv.view_task_name("Giant Rat", v, "left")}.json'
        current.unlink()                                        # only the old descriptor exists for `left`
        old_left, old_left_path = self.legacy(v, 'left')
        orphan, orphan_path = self.legacy('9', 'back')          # a variant that does not exist (withdrawn long ago)
        running, running_path = self.legacy(v, 'right')
        guard.write_atomic(self.root / '.codex_jobs/_batch/status' / f'{running}.json', json.dumps({'state': 'running'}).encode())
        desc = json.loads((tasks / f'{mv.view_task_name("Giant Rat", v, "back")}.json').read_text())
        self.assertTrue(mv.guard_ok(desc))
        out = monster_lane.tick(self.root)
        self.assertIn(f'{old_left}: republished', out['recovered'])
        self.assertIn(f'{orphan}: withdrawn', out['recovered'])
        self.assertFalse(old_left_path.exists() or orphan_path.exists())
        self.assertTrue(mv.guard_ok(json.loads(current.read_text())))                             # the guarded, digested descriptor replaces it
        self.assertTrue(running_path.exists())                                                   # a running one is only reported
        self.assertTrue(any(running in e for e in out['errors']))
        left = [p.name for p in tasks.glob('*.json')]
        self.assertEqual(sorted(left), sorted([f'{mv.view_task_name("Giant Rat", v, x)}.json' for x in mv.EXTRA_VIEWS] + [f'{running}.json']))
        self.assertEqual(monster_lane.tick(self.root)['recovered'], [])                           # idempotent
        # a current-named descriptor that lost its guard is not left to the runner either
        desc.pop('guard')
        (tasks / f'{desc["name"]}.json').write_text(json.dumps(desc))
        out = monster_lane.tick(self.root)
        self.assertIn(f'{desc["name"]}: republished', out['recovered'])
        self.assertTrue(mv.guard_ok(json.loads((tasks / f'{desc["name"]}.json').read_text())))


# ------------------------------------------------------------------------------------- final verification (2026-11-25)

SURVIVOR_STUB = r'''
import os, pathlib, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
import monster_lane
monster_lane._Tracker.WATCH_S = 60          # no watcher sample falls into the child's short life: only the job can know the grandchild
tracker = monster_lane._Tracker(sys.argv[2], 'Giant Rat', sys.argv[3], own_process=True)
note = sys.argv[4]
child = ('import subprocess, sys; D = subprocess.DEVNULL; '
         'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=D, stdout=D, stderr=D); '
         'open(sys.argv[1], "w").write(str(p.pid))')
code, _err = monster_lane._run([sys.executable, '-c', child, note], pathlib.Path(sys.argv[1]), lambda t: None, None, tracker)
tracker.close()                              # the driver's cleanup, with the short-lived child gone and its grandchild alive
print(tracker.job or '-', code, flush=True)
'''


class FinalVerifyCase(RecheckCase):
    """Shared fixture of the final-verification regressions (review_v2_final_verify_20261125; DESIGN_PHASE5.md H.12)."""

    def batch(self):
        return self.root / '.codex_jobs' / '_batch'

    def rebuild(self):
        """The derived queue database is replaced and the lane ticks: {queued, restored} of that tick."""
        self.drop_queue()
        out = monster_lane.tick(self.root)
        return out['queued'], out['restored']


class FinalVerifyDispositionTest(FinalVerifyCase):
    """Row 3: Cancel is a compare-and-set on the generation's disposition; a wrapper that cannot be spawned fails durably."""

    def test_stale_cancel_never_overwrites_or_clears_an_intervening_failure(self):
        mid, v, task_id, gen = self.frozen()
        q = taskqueue.for_root(self.root)
        shown = monster_lane.resolve_task(self.root, task_id, **self.ident(task_id))     # what the Cancel click resolved: a QUEUED row
        self.assertEqual(shown['state'], 'queued')
        # ... meanwhile the attempt started and failed: the failure is recorded, the row fails
        q.claim(task_id, 'tok', None)
        orch = orchestrator.Orchestrator(self.root, idle=lambda: 10.0, scan_stale=False)
        orch._finish(q.get(task_id), {'exit': 1, 'last_json': {'status': 'failed', 'step': 'collect', 'error': 'STOPPED: cost'}})
        failed = mv.disposition_of(self.vj(v), gen)
        self.assertEqual((failed['state'], q.get(task_id)['state']), ('failed', 'failed'))
        # the stale Cancel arrives now: it must not write `cancelled` over the failure, loses the SQLite race, and clears nothing
        self.assertFalse(monster_lane.cancel_task(self.root, shown))
        after = mv.disposition_of(self.vj(v), gen)
        self.assertEqual((after['state'], after['reason'], after['seq']), ('failed', 'STOPPED: cost', failed['seq']))
        self.assertEqual(mv.disposition_seq(self.vj(v)), failed['seq'])                           # not even touched
        self.assertEqual(self.rebuild(), ([], [f'Giant Rat/{v}']))                               # a rebuilt queue never queues it
        self.assertEqual([t['state'] for t in taskqueue.for_root(self.root).tasks(lane=monster_lane.LANE)], ['failed'])
        self.assertEqual(monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start'], [])

    def test_cancel_write_is_a_compare_and_set(self):
        mid, v, task_id, gen = self.frozen()
        first = mv.set_disposition(self.root, 'Giant Rat', v, gen, {'state': 'failed', 'reason': 'a'})
        lost = mv.set_disposition(self.root, 'Giant Rat', v, gen, {'state': 'cancelled', 'reason': 'b'}, only_absent=True)
        self.assertEqual((lost['written'], lost.get('conflict'), lost['previous']['state'], lost['seq']), (False, True, 'failed', first['seq']))
        self.assertEqual(mv.disposition_of(self.vj(v), gen)['state'], 'failed')
        mv.set_disposition(self.root, 'Giant Rat', v, gen, None)
        self.assertTrue(mv.set_disposition(self.root, 'Giant Rat', v, gen, {'state': 'cancelled'}, only_absent=True)['written'])
        # the failure lands between the Cancel's own write and its SQLite cancel: the newer failure stays (sequence guard)
        mv.set_disposition(self.root, 'Giant Rat', v, gen, None)
        real = taskqueue.TaskQueue.cancel

        def failed_meanwhile(queue, tid, detail='', manual_gen=None, expect=None):
            queue.claim(tid, 'tok', None)
            monster_lane.record_failure(self.root, queue.get(tid), 'STOPPED: later')
            queue.finish(tid, 'tok', False, None, 'STOPPED: later')
            return real(queue, tid, detail, manual_gen, expect)
        with mock.patch.object(taskqueue.TaskQueue, 'cancel', failed_meanwhile):
            with self.assertRaises(ValueError):
                self.qop('cancel', task_id)
        self.assertEqual(mv.disposition_of(self.vj(v), gen)['state'], 'failed')
        self.assertEqual(self.rebuild(), ([], [f'Giant Rat/{v}']))

    def test_wrapper_spawn_failure_is_a_durable_failure(self):
        mid, v, task_id, gen = self.frozen()
        q = taskqueue.for_root(self.root)
        orch = orchestrator.Orchestrator(self.root, idle=lambda: 10.0, scan_stale=False)
        with mock.patch.object(orchestrator.subprocess, 'Popen', side_effect=OSError('no more processes')):
            self.assertFalse(orch._spawn(q.get(task_id)))
        row = q.get(task_id)
        self.assertEqual(row['state'], 'failed')
        self.assertIn('could not start', row['error'])
        disp = mv.disposition_of(self.vj(v), gen)
        self.assertEqual(disp['state'], 'failed')                                                # recorded BEFORE the row finished
        self.assertIn('could not start', disp['reason'])
        self.assertEqual(self.rebuild(), ([], [f'Giant Rat/{v}']))
        self.assertEqual([t['state'] for t in taskqueue.for_root(self.root).tasks(lane=monster_lane.LANE)], ['failed'])
        self.assertEqual(monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start'], [])

    def test_spawn_failure_that_cannot_be_recorded_keeps_the_row_unfinished(self):
        mid, v, task_id, gen = self.frozen()
        q = taskqueue.for_root(self.root)
        orch = orchestrator.Orchestrator(self.root, idle=lambda: 10.0, scan_stale=False)
        mv.FAULTS['disposition_write'] = lambda: (_ for _ in ()).throw(OSError('disk full'))
        with mock.patch.object(orchestrator.subprocess, 'Popen', side_effect=OSError('no more processes')):
            self.assertFalse(orch._spawn(q.get(task_id)))
        self.assertEqual(q.get(task_id)['state'], 'starting')                                    # not finished: the slot stays held
        self.assertEqual(monster_lane.pending_disposition(self.root, 'Giant Rat', v, gen)['state'], 'failed')
        self.assertEqual(self.rebuild(), ([], []))                                               # nothing is queued while it is pending
        mv.FAULTS.clear()
        self.assertEqual(monster_lane.tick(self.root)['restored'], [f'Giant Rat/{v}'])
        self.assertEqual(mv.disposition_of(self.vj(v), gen)['state'], 'failed')


class FinalVerifyPauseTest(FinalVerifyCase):
    """Row 5 and new finding 1: the consent names ALL independent manual pauses; a drain closes the gate like the hold."""

    def child_gate(self, args):
        """The paid child's click-time check of one attempt, exactly as the driver configures it (paid_gate + the lane's `gate` command)."""
        env = paid_gate.env_for(self.root, override_pause=bool(args.get('run_now') or args.get('override_pause')),
                                cmd=monster_lane.gate_command(self.root, args, 10, True))
        return paid_gate.closed_reason(env)

    def test_a_manual_pause_after_the_confirmation_closes_lane_and_paid_child(self):
        mid, v, task_id, gen = self.frozen()
        ident = {'id': task_id} | self.ident(task_id)
        seen = modes.write_mode(self.root, 'pause-conversions')['pause_id']
        out = actions.queue_op(self.root, {'op': 'run_now', 'confirm': True, 'pause_id': seen} | ident)
        self.assertEqual(out['pause_id'], seen)
        args = taskqueue.for_root(self.root).get(task_id)['args']
        self.assertEqual(len(monster_lane.plan_starts(self.root, idle=10, idle_known=True)['start']), 1)
        self.assertIsNone(self.child_gate(args))
        # the supervisor reflects ITS OWN mode in PAUSE: the same pause, the consent still covers it
        pause = self.batch() / 'PAUSE'
        self.supervisor_pause('manual pause-conversions', ['conversions'], '2026-11-25T10:00:01')
        self.assertEqual(monster_lane.pause_id(self.root), seen)
        self.assertEqual(len(monster_lane.plan_starts(self.root, idle=10, idle_known=True)['start']), 1)
        self.assertIsNone(self.child_gate(args))
        self.assertEqual(monster_lane.main(['gate', '--root', str(self.root), '--task-args', json.dumps(args), '--idle', '10']), 0)
        # a distinct MANUAL maintenance PAUSE after the confirmation: an independent pause nobody consented to
        pause.write_text(json.dumps({'since': '2026-11-25T10:05:00', 'reason': 'maintenance: disk swap', 'scope': ['conversions']}))
        manual = modes.manual_pause(self.root)
        self.assertEqual(([p['kind'] for p in manual['pauses']], manual['pause_id'] != seen), (['mode', 'pause'], True))
        gate = monster_lane.gate(self.root, args, 10, True)
        self.assertEqual((gate['open'], gate['code'], gate['needs_confirm']), (False, 'paused', True))
        self.assertIn('maintenance: disk swap', gate['reason'])
        self.assertEqual(monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start'], [])     # the lane
        self.assertIn('maintenance: disk swap', self.child_gate(args))                                      # the paid child
        self.assertEqual(monster_lane.main(['gate', '--root', str(self.root), '--task-args', json.dumps(args), '--idle', '10']), 1)
        proc = subprocess.run(monster_lane.gate_command(self.root, args, 10, True), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('maintenance: disk swap', proc.stdout)
        # the old consent is refused; a new one names both pauses and holds for exactly them
        with self.assertRaises(monster_lane.ConfirmPaused) as caught:
            actions.queue_op(self.root, {'op': 'run_now', 'confirm': True, 'pause_id': seen} | ident)
        both = caught.exception.extra['pause_id']
        self.assertEqual(both, manual['pause_id'])
        actions.queue_op(self.root, {'op': 'run_now', 'confirm': True, 'pause_id': both} | ident)
        args = taskqueue.for_root(self.root).get(task_id)['args']
        self.assertIsNone(self.child_gate(args))
        self.assertEqual(len(monster_lane.plan_starts(self.root, idle=10, idle_known=True)['start']), 1)
        pause.write_text(json.dumps({'since': '2026-11-25T10:09:00', 'reason': 'maintenance: again', 'scope': ['conversions']}))
        self.assertEqual(monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start'], [])     # another new manual pause
        self.assertIsNotNone(self.child_gate(args))
        pause.unlink()                                                                           # lifted: the rest was consented to
        self.assertEqual(len(monster_lane.plan_starts(self.root, idle=10, idle_known=True)['start']), 1)

    def test_what_counts_as_the_supervisors_own_reflection(self):
        # asked about the supervisor's OWN record (supervisor_pause.json), never about a PAUSE file (post-cut-over blocker 1)
        mode = {'mode': 'pause', 'pause_id': 'm'}
        same = {'reason': 'manual pause', 'scope': ['conversions', 'views'], 'readable': True}
        self.assertTrue(modes.mode_reflection(mode, same))
        for other in (same | {'reason': 'manual pause-conversions'},           # the reflection of ANOTHER mode
                      same | {'scope': ['conversions']},                        # not the scope the supervisor writes for this mode
                      same | {'readable': False},                               # a record that could not be read
                      same | {'reason': 'maintenance'}):
            self.assertFalse(modes.mode_reflection(mode, other), other)
            self.assertEqual(modes.own_kind(mode, other), 'other')
        self.assertFalse(modes.mode_reflection({'mode': 'views-run', 'pause_id': None}, same))    # no pause mode: a pause of its own
        self.assertFalse(modes.mode_reflection(mode, None))
        self.assertEqual((modes.own_kind(mode, same), modes.own_kind(mode, None), modes.own_kind(mode, same | {'scope': ['views']})),
                         ('mode', None, None))
        # the idle gate (the supervisor's reflection of the user's activity) is never a manual pause, and Run now still overrides it
        modes.write_mode(self.root, 'views-run')
        self.supervisor_pause('user active (views keep running)', ['conversions'])
        self.assertIsNone(modes.manual_pause(self.root))
        self.assertTrue(monster_lane.gate(self.root, {'run_now': True}, 10, True)['open'])
        self.assertFalse(modes.consent_covers(None, None))
        self.assertFalse(modes.consent_covers('a', None))
        manual = {'pause_id': 'a+b', 'pauses': [{'pause_id': 'a'}, {'pause_id': 'b'}]}
        self.assertEqual([modes.consent_covers(c, manual) for c in ('a+b', 'b+a+c', 'a', 'b', '', True)],
                         [True, True, False, False, False, False])

    def test_drain_closes_the_gate_and_run_now_never_overrides_it(self):
        mid, v, task_id, gen = self.frozen()
        drain = self.batch() / 'CONVERT_DRAIN'
        drain.write_text(json.dumps({'since': 'now', 'reason': 'worker restart'}))
        for args in ({}, {'run_now': True}, {'run_now': True, 'override_pause': 'anything'}):
            gate = monster_lane.gate(self.root, args, 1000, True)
            self.assertEqual((gate['open'], gate['code'], gate.get('draining'), gate['needs_confirm']), (False, 'held', True, False), args)
            self.assertIn('CONVERT_DRAIN', gate['reason'])
            self.assertIn('CONVERT_DRAIN', self.child_gate(args))                                # = paid_gate.file_reason
        self.assertEqual(monster_lane.main(['gate', '--root', str(self.root), '--task-args', json.dumps({'run_now': True}),
                                            '--idle', '1000']), 1)
        proc = subprocess.run(monster_lane.gate_command(self.root, {'run_now': True}, 1000, True), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('CONVERT_DRAIN', proc.stdout)
        out = self.qop('run_now', task_id)                                                       # marked, with the warning; it does not start
        self.assertIn('CONVERT_DRAIN', out['warning'])
        plan = monster_lane.plan_starts(self.root, idle=1000, idle_known=True)
        self.assertEqual(plan['start'], [])
        self.assertIn('CONVERT_DRAIN', plan['waiting'][task_id])
        self.assertEqual(mv.item(self.root, mid)['conversion']['waiting_reason'], 'held')
        self.assertEqual(monster_lane.lane_status(self.root)['gate']['code'], 'held')
        # the driver itself stops at its first step, nothing is submitted or paid
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=1000, idle_known=True, say=lambda t: None)
        self.assertEqual((verdict['status'], self.calls(), self.paid_clicks()), ('waiting', [], 0))
        drain.write_text('{torn')                                                                # an unreadable drain file still drains
        self.assertFalse(monster_lane.gate(self.root, {'run_now': True}, 1000, True)['open'])
        drain.unlink()
        self.assertEqual(len(monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start']), 1)


class FinalVerifyPaidTest(FinalVerifyCase):
    """Row 8: every reconciliation merges the stored repair under the ledger lock and clears only what the ledger carries."""

    def test_reconvert_then_an_ordinary_reconcile_keeps_the_charge(self):
        mid, v, task_id, gen = self.frozen()
        monster_lane.paid_log_path(self.root).mkdir(parents=True)                                 # the ledger cannot be written
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual((verdict['status'], self.paid_clicks()), ('ok', 1))
        self.assertIn('could not be written', verdict['accounting_error'])
        mdir = mv.models_dir(self.mroot, 'Giant Rat', v)
        rec_path = mdir / 'hyper3d' / 'studio_submission.json'
        stored = monster_lane.accounting_error(self.root, rec_path)
        self.assertEqual([(a['id'], a['credits'], a['status']) for a in stored['actions']], [('hyper3d:m1:geometry', 0.5, 'confirmed')])
        q = taskqueue.for_root(self.root)
        q.claim(task_id, 'tok', None)
        q.finish(task_id, 'tok', True)
        self.convert(mid, ['front.png'], verb='reconvert', confirm=True)                          # Reconvert moves the record away
        self.assertFalse(rec_path.exists())
        monster_lane.paid_log_path(self.root).rmdir()                                             # the ledger works again
        # the ORDINARY reconciliation of the next run (the driver's `account`: no record at the path, an unrepaired error for it)
        self.assertIsNone(monster_lane.reconcile_paid(self.root, 'Giant Rat', v, ['front.png'], rec_path, mv.record_of(mdir)))
        summary = monster_lane.paid_summary(self.root)
        self.assertEqual((summary['count'], summary['credits'], summary['errors']), (1, 0.5, []))  # the 0.5 credits are in the ledger
        self.assertEqual([e['id'] for e in monster_lane.paid_entries(self.root)], ['hyper3d:m1:geometry'])
        self.assertEqual(monster_lane.repair_accounting(self.root), [])

    def test_the_drivers_own_next_run_logs_the_moved_charge(self):
        mid, v, task_id, gen = self.frozen()
        monster_lane.paid_log_path(self.root).mkdir(parents=True)
        monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        q = taskqueue.for_root(self.root)
        q.claim(task_id, 'tok', None)
        q.finish(task_id, 'tok', True)
        self.convert(mid, ['front.png'], verb='reconvert', confirm=True)
        monster_lane.paid_log_path(self.root).rmdir()
        os.environ['FAKE_H3D'] = 'busy_submit'                  # the new attempt gets no further than its accounting + a busy browser
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None)
        self.assertEqual((verdict['status'], verdict['accounting_error']), ('busy', None))
        summary = monster_lane.paid_summary(self.root)
        self.assertEqual((summary['count'], summary['credits'], summary['errors'], self.paid_clicks()), (1, 0.5, [], 1))

    def test_another_models_record_at_the_path_does_not_erase_the_stored_action(self):
        rec_path = self.root / 'hyper3d' / 'studio_submission.json'
        rec_path.parent.mkdir()
        old = {'model_id': 'm8', 'actions': {'geometry': {'confirmed_at': 't', 'observed_cost': 0.5}}}
        monster_lane.paid_log_path(self.root).mkdir(parents=True)
        monster_lane.reconcile_paid(self.root, 'Giant Rat', 'main', ['front.png'], rec_path, old)
        monster_lane.paid_log_path(self.root).rmdir()
        new = {'model_id': 'm9', 'actions': {'geometry': {'confirmed_at': 't2', 'observed_cost': 0.5}}}
        self.assertIsNone(monster_lane.reconcile_paid(self.root, 'Giant Rat', 'main', ['front.png'], rec_path, new))
        self.assertEqual(sorted(e['id'] for e in monster_lane.paid_entries(self.root)), ['hyper3d:m8:geometry', 'hyper3d:m9:geometry'])
        summary = monster_lane.paid_summary(self.root)
        self.assertEqual((summary['count'], summary['credits'], summary['errors']), (2, 1.0, []))
        # an error entry WITHOUT stored actions is not cleared by a record of another model (or by no record): it stays listed
        guard.write_atomic(monster_lane.paid_errors_path(self.root), json.dumps({
            rec_path.as_posix(): {'time': 't', 'error': 'old', 'monster': 'Giant Rat', 'variant': 'main', 'views': [], 'model_id': 'm7'}}).encode())
        monster_lane.reconcile_paid(self.root, 'Giant Rat', 'main', ['front.png'], rec_path, new)
        monster_lane.reconcile_paid(self.root, 'Giant Rat', 'main', ['front.png'], rec_path, None)
        self.assertEqual(len(monster_lane.paid_summary(self.root)['errors']), 1)


@unittest.skipUnless(os.name == 'nt', 'Windows Job Objects')
class FinalVerifyTreeTest(FinalVerifyCase):
    """Row 9: the Job Object is asked before the ownership record is removed; a survivor keeps it and is reported."""

    def test_short_lived_child_leaves_a_grandchild_the_record_stays(self):
        import psutil
        mid, v, task_id, gen = self.frozen()
        stub = Path(self.temp.name) / 'survivor_stub.py'
        stub.write_text(SURVIVOR_STUB)
        note = Path(self.temp.name) / 'grand.pid'
        driver = subprocess.run([sys.executable, str(stub), str(HERE), str(self.root), v, str(note)], capture_output=True, text=True,
                                timeout=120)
        self.assertEqual(driver.returncode, 0, driver.stderr)
        grand = int(note.read_text())
        self.addCleanup(lambda: psutil.pid_exists(grand) and psutil.Process(grand).kill())
        job, code = driver.stdout.split()
        self.assertTrue(job.startswith(monster_lane.JOB_PREFIX), driver.stdout)                   # the driver owned a job
        self.assertEqual(code, '0')                                                              # the child exited by itself
        self.assertTrue(psutil.pid_exists(grand))
        path = monster_lane.children_path(self.root, 'Giant Rat', v)
        self.assertTrue(path.exists())                                                           # the driver's cleanup KEPT the record
        data = monster_lane.recorded_children(self.root, 'Giant Rat', v)
        by_pid = {d['pid']: d for d in data['descendants']}                                        # (a console host may be there too)
        self.assertIn(grand, by_pid)
        self.assertIsNotNone(by_pid[grand]['create'])
        held = monster_lane.tree_hold(self.root, 'Giant Rat', v)                                  # reports the survivor
        self.assertIn(str(grand), held)
        self.assertTrue(path.exists())
        self.assertIn(grand, [p['pid'] for p in monster_lane.live_children(self.root, 'Giant Rat', v)])
        plan = monster_lane.plan_starts(self.root, idle=1000, idle_known=True)
        self.assertEqual((plan['start'], plan['active']), ([], 1))                                # the slot is not released
        self.assertEqual(monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True,
                                                     say=lambda t: None)['status'], 'busy')
        drained = monster_lane.drain_tree(self.root, 'Giant Rat', v, monster_lane.tree_plan(self.root, 'Giant Rat', v), 10.0)
        self.assertEqual(drained['problems'], [])
        self.assertFalse(psutil.pid_exists(grand))
        self.assertFalse(path.exists())                                                          # removed once nothing of it runs
        self.assertIsNone(monster_lane.tree_hold(self.root, 'Giant Rat', v))

    def test_a_job_that_cannot_be_queried_leaves_the_record_incomplete(self):
        mid, v, task_id, gen = self.frozen()
        tracker = monster_lane._Tracker(self.root, 'Giant Rat', v)
        tracker._handle = 1                                                                      # (a handle the query fails for)
        with mock.patch.object(monster_lane, 'job_pids', return_value=None), mock.patch.object(monster_lane, 'job_close'):
            tracker.close()
        path = monster_lane.children_path(self.root, 'Giant Rat', v)
        self.assertIn('could not be queried', json.loads(path.read_text())['incomplete'])
        path.write_text(json.dumps(json.loads(path.read_text()) | {'driver': None}))             # (the driver ran inside this test)
        self.assertIn('not fully known', monster_lane.tree_hold(self.root, 'Giant Rat', v))
        self.assertTrue(path.exists())


class FinalVerifyApproveTest(FinalVerifyCase):
    """Row 10 (server side): approval and Completed are certified by hashing the file, the cache is a hint; URLs carry ns + size."""

    def approved(self):
        mid, v, task_id, gen = self.frozen()
        q = taskqueue.for_root(self.root)
        q.claim(task_id, 'tok', None)
        q.finish(task_id, 'tok', True)
        self.converted('Giant Rat', v)
        sha = mv.item(self.root, mid)['lods']['0p4']['sha256']
        self.act(mid, 'approve', lod='0p4', glb_sha256=sha)
        self.assertIn(mid, self.tabs()['completed'])
        return mid, v, gen, mv.models_dir(self.mroot, 'Giant Rat', v)

    def replace_keeping_stat(self, path, data):
        st = path.stat()
        self.assertEqual(len(data), st.st_size)
        path.write_bytes(data)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        after = path.stat()
        self.assertEqual((after.st_size, after.st_mtime_ns), (st.st_size, st.st_mtime_ns))        # the size + mtime cache is blind to it

    def test_same_size_replacement_with_the_timestamp_restored_is_not_completed(self):
        mid, v, gen, mdir = self.approved()
        glb = mdir / 'reduced' / 'lod_error_0p4.glb'
        original = glb.read_bytes()
        approved_sha = hashlib.sha256(original).hexdigest()
        self.assertEqual(mv.file_sha256(glb), approved_sha)                                      # the hint cache is primed
        self.replace_keeping_stat(glb, b'Z' * len(original))
        self.assertEqual(mv.file_sha256(glb), approved_sha)                                      # ... and stale now
        tabs = self.tabs()
        self.assertNotIn(mid, tabs['completed'])
        card = tabs['3d'][mid]
        self.assertEqual(card['actions'], ['unapprove'])
        self.assertIn('not the model that was approved', ' '.join(b['text'] for b in card['badges']))
        # the item shows the digest of the bytes on disk, so the modal compares its loaded bytes with the truth
        self.assertEqual(mv.item(self.root, mid)['lods']['0p4']['sha256'], hashlib.sha256(b'Z' * len(original)).hexdigest())
        self.replace_keeping_stat(glb, original)                                                 # the approved bytes are back
        self.assertIn(mid, self.tabs()['completed'])

    def test_approve_certifies_the_bytes_not_the_cached_digest(self):
        mid, v, task_id, gen = self.frozen()
        q = taskqueue.for_root(self.root)
        q.claim(task_id, 'tok', None)
        q.finish(task_id, 'tok', True)
        self.converted('Giant Rat', v)
        mdir = mv.models_dir(self.mroot, 'Giant Rat', v)
        glb = mdir / 'reduced' / 'lod_error_0p4.glb'
        shown = mv.item(self.root, mid)['lods']['0p4']['sha256']                                  # what the modal loaded and hashed
        mv.file_sha256(glb)
        self.replace_keeping_stat(glb, b'Q' * glb.stat().st_size)
        with self.assertRaises(mv.Unverified):                                                   # the verified reduction is re-hashed (deep)
            self.act(mid, 'approve', lod='0p4', glb_sha256=shown)
        self.assertFalse((mdir / 'chosen_lod.json').exists())
        other = mdir / 'reduced' / 'lod_error_0p8.glb'                                           # also for a level that is not the chosen one
        self.replace_keeping_stat(glb, b'glb0p4')
        mv.file_sha256(other)
        self.replace_keeping_stat(other, b'W' * other.stat().st_size)
        with self.assertRaises(mv.Unverified):
            self.act(mid, 'approve', lod='0p4', glb_sha256=shown)
        self.replace_keeping_stat(other, b'glb0p8')
        self.assertTrue(self.act(mid, 'approve', lod='0p4', glb_sha256=shown)['approved'])

    def test_file_urls_are_versioned_by_nanoseconds_and_size(self):
        mid, v, gen, mdir = self.approved()
        glb = mdir / 'reduced' / 'lod_error_0p4.glb'
        st = glb.stat()
        url = mv.item(self.root, mid)['lods']['0p4']['url']
        self.assertTrue(url.endswith(f'lod_error_0p4.glb?v={st.st_mtime_ns}-{st.st_size}'), url)
        self.assertTrue(mv.murl(self.mroot, glb).endswith(f'?v={st.st_mtime_ns}-{st.st_size}'))
        os.utime(glb, ns=(st.st_atime_ns, st.st_mtime_ns + 1000))                                 # a rewrite within the same second
        self.assertNotEqual(mv.item(self.root, mid)['lods']['0p4']['url'], url)


class FinalVerifyJournalTest(FinalVerifyCase):
    """Row 11: the FULL operation-specific journal record is validated before either file is mutated."""

    def journal(self, **fields):
        path = mv.journal_path(self.root, 'Giant Rat')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema': 1, 'time': 't', 'monster': 'Giant Rat'} | fields))
        return path

    def test_import_journal_without_names_entry_changes_nothing(self):
        mid, v = self.pick_rat()                                                                 # an existing pick with its provenance
        names_path = mv.variant_names_path(self.mroot, 'Giant Rat')
        names_before = names_path.read_bytes()
        self.assertEqual(json.loads(names_before)['variants'][v]['was'], 'codex1')
        vdir = mv.views_dir(self.mroot, 'Giant Rat', v)
        views_before = (vdir / 'views.json').read_bytes()
        path = self.journal(op='import', variant=v, tmp=f'.v2_pick_{v}_0123abcd')                  # parseable, but no names_entry
        before = path.read_bytes()
        with self.assertRaises(guard.Unreadable) as caught:
            mv.read_journal(self.root, 'Giant Rat')
        self.assertIn('names_entry', str(caught.exception))
        out = monster_lane.tick(self.root)                                                       # recovery refuses it
        self.assertTrue(any(path.name in e and 'names_entry' in e for e in out['errors']), out['errors'])
        self.assertFalse(any('completed' in r or 'rolled back' in r for r in out['recovered']))
        self.assertEqual(names_path.read_bytes(), names_before)                                  # the provenance is NOT replaced by {}
        self.assertEqual(path.read_bytes(), before)                                              # the journal is NOT deleted
        self.assertEqual((vdir / 'views.json').read_bytes(), views_before)
        # the variant is a read-only damaged card; its mutations and new picks of the monster are refused
        card = self.tabs()['approved'][mid]
        self.assertEqual((card['actions'], bool(card['damaged'])), ([], True))
        self.assertIn('names_entry', card['damaged']['error'])
        self.assertEqual(mv.item(self.root, mid)['state'], 'damaged')
        for verb, args in (('views', {'enabled': {'front': True}}), ('view_retry', {'view': 'left'}),
                           ('convert', {'files': ['front.png'], 'views_rev': 1})):
            with self.assertRaises(guard.Unreadable, msg=verb):
                self.act(mid, verb, **args)
        with self.assertRaises(guard.Unreadable):
            self.act('mconcept:Giant Rat/codex2', 'approve')
        self.assertEqual((names_path.read_bytes(), path.read_bytes()), (names_before, before))
        self.assertTrue(self.tabs()['concepts']['mconcept:Hobgoblin/codex3']['actions'])          # other monsters are not affected
        path.unlink()                                                                            # repaired by hand
        self.assertFalse(self.tabs()['approved'][mid].get('damaged'))
        self.assertIsInstance(self.act(mid, 'views', enabled={'front': True}), dict)

    def test_every_field_recovery_uses_is_checked(self):
        entry = {'was': 'codex1', 'concept': 'codex1-front.png', 'picked': 't', 'by': 'v2'}
        state = {'stage': 'approved', 'time': 't', 'monster': 'Giant Rat', 'variant': 'main', 'from': 'concepts', 'unattended': False}
        pick = {'op': 'pick', 'monster': 'Giant Rat', 'variant': 'main', 'folder': 'Giant Rat', 'key': 'codex1', 'origin': 'concepts',
                'tmp': '.v2_pick_main_0123abcd', 'names_entry': entry, 'names_prev': None, 'state_entry': state}
        imp = {'op': 'import', 'monster': 'Giant Rat', 'variant': '1', 'tmp': '.v2_pick_1_0123abcd',
               'names_entry': {'was': 'codex1', 'imported': 't', 'imported_by': 'v2'}, 'names_prev': {'was': 'codex1'}}
        self.assertIsNone(mv.journal_record_problem(pick, 'Giant Rat'))
        self.assertIsNone(mv.journal_record_problem(imp, 'Giant Rat'))
        broken = [{k: val for k, val in pick.items() if k != drop} for drop in ('variant', 'folder', 'key', 'origin', 'tmp', 'names_entry',
                                                                              'names_prev', 'state_entry')]
        broken += [{k: val for k, val in imp.items() if k != drop} for drop in ('variant', 'tmp', 'names_entry', 'names_prev')]
        broken += [pick | {'names_entry': {}}, pick | {'names_entry': entry | {'was': 'codex9'}}, pick | {'tmp': '../elsewhere'},
                   pick | {'tmp': '.v2_pick_other_0123abcd'}, pick | {'state_entry': state | {'variant': '2'}},
                   pick | {'state_entry': state | {'stage': 'concepts'}}, pick | {'origin': 'trash'}, pick | {'key': 'front'},
                   pick | {'variant': '../x'}, pick | {'names_prev': {'was': 'x'}}, pick | {'monster': 'Hobgoblin'},
                   imp | {'names_entry': {'was': 'codex1'}}, imp | {'names_prev': 'x'}, imp | {'op': 'withdraw'}]
        for record in broken:
            self.assertIsNotNone(mv.journal_record_problem(record, 'Giant Rat'), record)

    def test_journals_the_app_writes_itself_are_complete(self):
        mv.FAULTS['pick_renamed'] = lambda: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            self.act('mconcept:Giant Rat/codex1', 'approve')
        mv.FAULTS.clear()
        self.assertEqual(mv.read_journal(self.root, 'Giant Rat')['op'], 'pick')
        self.assertEqual(monster_lane.tick(self.root)['recovered'], ['Giant Rat: completed'])
        mv.FAULTS['import_names'] = lambda: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            mv.import_pick(self.root, 'Hobgoblin', '1')
        mv.FAULTS.clear()
        self.assertEqual(mv.read_journal(self.root, 'Hobgoblin')['op'], 'import')
        self.assertIn('Hobgoblin: completed', monster_lane.tick(self.root)['recovered'])
        self.assertEqual(json.loads(mv.variant_names_path(self.mroot, 'Hobgoblin').read_text())['variants']['1']['imported_by'], 'v2')


# ------------------------------------------------------------------------------------------------- last check (review_v2_last_check)

SUCCESSOR_STUB = r'''
import os, pathlib, subprocess, sys, time
sys.path.insert(0, sys.argv[1])
import monster_lane, psutil
monster_lane._Tracker.WATCH_S = 60          # no watcher sample: only the cleanup's own looks at the job count
tracker = monster_lane._Tracker(sys.argv[2], 'Giant Rat', sys.argv[3], own_process=True)
go, note = pathlib.Path(sys.argv[4]), sys.argv[5]
member = ('import os, pathlib, subprocess, sys, time\n'
          'D = subprocess.DEVNULL\n'
          'while not pathlib.Path(sys.argv[1]).exists(): time.sleep(0.02)\n'
          'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=D, stdout=D, stderr=D)\n'
          'open(sys.argv[2] + ".tmp", "w").write(str(p.pid))\n'
          'os.replace(sys.argv[2] + ".tmp", sys.argv[2])\n')
D = subprocess.DEVNULL
proc = subprocess.Popen([sys.executable, '-c', member, str(go), note], stdin=D, stdout=D, stderr=D)   # a member of the driver's job
real = tracker._job_survivors


def survivors():
    out = real()                             # the cleanup looks at the job: the member lives and is on record
    assert proc.pid in tracker.descendants, tracker.descendants
    go.write_text('go')                      # ... and NOW, during the cleanup, it spawns a successor and exits
    proc.wait(60)
    while psutil.pid_exists(proc.pid):
        time.sleep(0.02)
    assert pathlib.Path(note).exists()
    return out


tracker._job_survivors = survivors
tracker.close()
print(tracker.job or '-', proc.pid, flush=True)
'''


class LastCheckCancelTest(FinalVerifyCase):
    """New blocker + row 3: Cancel never answers success while the generation stays runnable; a repeated Cancel is never lost."""

    def lost_cancel(self):
        """A row that is cancelled in SQLite while its durable disposition is MISSING (what the reviewer's repeated Cancel resolved)."""
        mid, v, task_id, gen = self.frozen()
        self.assertTrue(taskqueue.for_root(self.root).cancel(task_id, 'manual', manual_gen='manual'))
        self.assertIsNone(mv.disposition_of(self.vj(v), gen))
        return mid, v, task_id, gen, {'id': task_id} | self.ident(task_id)

    def runnable(self):
        return [t['key'] for t in monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start']]

    def lane_states(self):
        return [t['state'] for t in taskqueue.for_root(self.root).tasks(lane=monster_lane.LANE)]

    def test_queue_rebuilt_between_the_handlers_two_lookups(self):
        mid, v, task_id, gen, ident = self.lost_cancel()
        real = monster_lane.resolve_task

        def resolve(*args, **kwargs):
            row = real(*args, **kwargs)                         # the handler's FIRST lookup: the cancelled row
            self.assertEqual(row['state'], 'cancelled')
            self.assertEqual(self.rebuild(), ([f'Giant Rat/{v}'], []))    # the queue is rebuilt: no disposition, so it is QUEUED again
            self.assertEqual(self.runnable(), [row['key']])     # (what HEAD then left selected for conversion)
            return row
        with mock.patch.object(monster_lane, 'resolve_task', resolve):
            out = actions.queue_op(self.root, {'op': 'cancel'} | ident)
        self.assertEqual((out['ok'], out['state']), (True, 'cancelled'))                         # the Cancel was RE-APPLIED, not lost
        self.assertEqual(mv.disposition_of(self.vj(v), gen)['state'], 'cancelled')               # durably
        self.assertEqual((self.lane_states(), self.runnable()), (['cancelled'], []))
        self.assertEqual(self.rebuild(), ([], [f'Giant Rat/{v}']))                               # and it survives the next rebuild
        self.assertEqual((self.lane_states(), self.runnable()), (['cancelled'], []))

    def test_queue_rebuilt_inside_cancel_before_its_own_lookup(self):
        mid, v, task_id, gen, ident = self.lost_cancel()
        shown = monster_lane.resolve_task(self.root, task_id, ident['key'], ident['generation'])
        real = taskqueue.TaskQueue.by_key
        calls = []

        def by_key(queue, key):
            if not calls:                                       # right before cancel_task's own (second) lookup
                calls.append(1)
                self.assertEqual(self.rebuild(), ([f'Giant Rat/{v}'], []))
            return real(queue, key)
        with mock.patch.object(taskqueue.TaskQueue, 'by_key', by_key):
            self.assertTrue(monster_lane.cancel_task(self.root, shown))
        self.assertEqual(mv.disposition_of(self.vj(v), gen)['state'], 'cancelled')
        self.assertEqual((self.lane_states(), self.runnable()), (['cancelled'], []))

    def test_rebuilt_queue_without_a_row_yet_gets_the_durable_cancel(self):
        mid, v, task_id, gen, ident = self.lost_cancel()
        shown = monster_lane.resolve_task(self.root, task_id, ident['key'], ident['generation'])
        self.drop_queue()                                       # rebuilt, not ticked yet: no row at all
        self.assertTrue(monster_lane.cancel_task(self.root, shown))
        self.assertEqual(mv.disposition_of(self.vj(v), gen)['state'], 'cancelled')
        out = monster_lane.tick(self.root)
        self.assertEqual((out['queued'], out['restored']), ([], [f'Giant Rat/{v}']))             # never queued again
        self.assertEqual((self.lane_states(), self.runnable()), (['cancelled'], []))

    def test_state_changed_between_the_lookups_is_409_changed(self):
        mid, v, task_id, gen, ident = self.lost_cancel()
        real = monster_lane.resolve_task

        def resolve(*args, **kwargs):
            row = real(*args, **kwargs)
            self.rebuild()                                      # queued again ...
            q = taskqueue.for_root(self.root)
            q.claim(q.by_key(row['key'])['id'], 'tok', None)    # ... and the dispatcher started it
            return row
        with mock.patch.object(monster_lane, 'resolve_task', resolve):
            with self.assertRaises(guard.Changed) as caught:
                actions.queue_op(self.root, {'op': 'cancel'} | ident)
        self.assertEqual(caught.exception.code, 'changed')                                       # never `ok: true`
        self.assertIsNone(mv.disposition_of(self.vj(v), gen))                                    # nothing claimed to be cancelled
        self.assertEqual(self.lane_states(), ['starting'])

    def test_start_between_the_durable_write_and_the_row_cancel_takes_the_write_back(self):
        mid, v, task_id, gen, ident = self.lost_cancel()
        shown = monster_lane.resolve_task(self.root, task_id, ident['key'], ident['generation'])
        self.rebuild()
        real = taskqueue.TaskQueue.cancel

        def started_meanwhile(queue, tid, detail='', manual_gen=None, expect=None):
            self.assertEqual(mv.disposition_of(self.vj(v), gen)['state'], 'cancelled')           # the durable disposition came FIRST
            queue.claim(tid, 'tok', None)
            return real(queue, tid, detail, manual_gen, expect)
        with mock.patch.object(taskqueue.TaskQueue, 'cancel', started_meanwhile):
            with self.assertRaises(guard.Changed):
                monster_lane.cancel_task(self.root, shown)
        self.assertIsNone(mv.disposition_of(self.vj(v), gen))                                    # our own write, taken back
        self.assertEqual(self.lane_states(), ['starting'])

    def test_repeat_is_success_only_with_a_durable_cancel_and_no_runnable_row(self):
        mid, v, task_id, gen = self.frozen()
        self.assertEqual(self.qop('cancel', task_id)['state'], 'cancelled')
        seq = mv.disposition_seq(self.vj(v))
        self.assertEqual(self.qop('cancel', task_id)['state'], 'cancelled')                      # idempotent: durable + not runnable
        self.assertEqual(mv.disposition_seq(self.vj(v)), seq)                                    # (nothing rewritten)
        ident = {'id': task_id} | self.ident(task_id)
        # the durable disposition says FAILED by now (not cancelled): the repeat is not a success
        mv.set_disposition(self.root, 'Giant Rat', v, gen, None)
        mv.set_disposition(self.root, 'Giant Rat', v, gen, {'state': 'failed', 'reason': 'STOPPED: cost'})
        with self.assertRaises(guard.Changed):
            actions.queue_op(self.root, {'op': 'cancel'} | ident)
        self.assertEqual(mv.disposition_of(self.vj(v), gen)['state'], 'failed')
        self.assertEqual(self.runnable(), [])

    def test_answer_never_reports_a_runnable_row(self):
        mid, v, task_id, gen, ident = self.lost_cancel()
        real = monster_lane.cancel_task

        def cancel_then_revive(root, task, detail='manual'):
            done = real(root, task, detail)
            mv.set_disposition(root, 'Giant Rat', v, gen, None)                                 # (a Retry right after it)
            taskqueue.for_root(root).retry(taskqueue.for_root(root).by_key(task['key'])['id'])
            return done
        with mock.patch.object(monster_lane, 'cancel_task', cancel_then_revive):
            with self.assertRaises(guard.Changed):
                actions.queue_op(self.root, {'op': 'cancel'} | ident)


class LastCheckPauseTest(FinalVerifyCase):
    """Row 5: a pause's identity and reason come from ONE snapshot; a consent for pause A is never accepted while B is reported."""

    def pause_file(self, reason):
        guard.write_atomic(self.batch() / 'PAUSE', json.dumps({'since': 's', 'reason': reason, 'scope': ['conversions']}).encode())

    def hook(self, fn):
        modes.READS.append(fn)
        self.addCleanup(lambda: fn in modes.READS and modes.READS.remove(fn))

    def test_each_file_is_read_once_per_gate(self):
        modes.write_mode(self.root, 'pause-conversions')
        self.pause_file('maintenance: A')
        seen = []
        self.hook(lambda path: seen.append(Path(path).name))
        with mock.patch.object(modes, 'pause_extra', side_effect=AssertionError('a second look at PAUSE')), \
                mock.patch.object(modes, 'file_identity', side_effect=AssertionError('a second look at the file')), \
                mock.patch.object(modes, 'read_json', side_effect=AssertionError('a second look at the file')):
            gate = monster_lane.gate(self.root, {'run_now': True}, 10, True)
            self.assertEqual(sorted(seen), ['PAUSE', 'supervisor_mode.json', 'supervisor_pause.json'])
            del seen[:]
            manual = modes.manual_pause(self.root)
            self.assertEqual(sorted(seen), ['PAUSE', 'supervisor_mode.json', 'supervisor_pause.json'])
            del seen[:]
            snap = modes.snapshot(self.root, supervisor=(False, ''))
            self.assertEqual(sorted(seen), ['PAUSE', 'supervisor_mode.json', 'supervisor_pause.json'])
        self.assertEqual((gate['pause_id'], snap['pause_id']), (manual['pause_id'], manual['pause_id']))
        self.assertNotIn('extra', gate['pause'])                                                 # the API shape is unchanged
        self.assertNotIn('extra', snap['pause'])

    def test_replacing_pause_a_by_b_between_reads_never_accepts_the_consent_for_a(self):
        modes.write_mode(self.root, 'views-run')
        self.pause_file('maintenance: A')
        id_a = modes.manual_pause(self.root)['pause_id']
        args = {'run_now': True, 'override_pause': id_a}                                          # the user confirmed pause A
        self.assertTrue(monster_lane.gate(self.root, args, 10, True)['open'])
        swapped = []

        def replace(path):                                      # the reviewer's writer: A is replaced by B right after a read of PAUSE
            if Path(path).name == 'PAUSE' and not swapped:
                swapped.append(1)
                self.pause_file('maintenance: B')
        self.hook(replace)
        gate = monster_lane.gate(self.root, args, 10, True)
        # that evaluation read A, as ONE snapshot: A's reason with A's id (the consent for A covers A) ...
        self.assertEqual((gate['pause_reason'], gate['pause_id'], gate['pause']['reason'], gate['open']),
                         ('maintenance: A', id_a, 'maintenance: A', True))
        self.assertEqual(swapped, [1])
        # ... and from the next read on it is B with B's id: reported AND compared as B, the consent for A is not accepted
        gate = monster_lane.gate(self.root, args, 10, True)
        self.assertEqual((gate['pause_reason'], gate['open'], gate['code'], gate['needs_confirm']), ('maintenance: B', False, 'paused', True))
        self.assertNotEqual(gate['pause_id'], id_a)
        self.assertFalse(modes.consent_covers(id_a, modes.manual_pause(self.root)))
        # manual_pauses given a display-shaped pause (no `extra`) does not complete it from a second look: it takes one new snapshot
        del swapped[:]
        self.pause_file('maintenance: A2')
        shown = modes.read_pause(self.root)                     # (the hook replaced A2 by B right after this read)
        self.assertEqual(shown['reason'], 'maintenance: A2')
        manual = modes.manual_pause(self.root, modes.read_mode(self.root), shown)
        self.assertEqual((manual['reason'], manual['pause_id'] != shown['pause_id']), ('maintenance: B', True))

    def test_a_writer_flipping_a_and_b_never_yields_a_mixed_snapshot(self):
        modes.write_mode(self.root, 'views-run')
        self.pause_file('maintenance: A')
        id_a = modes.manual_pause(self.root)['pause_id']
        args = {'run_now': True, 'override_pause': id_a}
        stop = threading.Event()

        def writer():
            n = 0
            while not stop.is_set():
                n += 1
                try:
                    self.pause_file(f'maintenance: B{n}')
                except OSError:
                    pass
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            for _ in range(300):
                gate = monster_lane.gate(self.root, args, 10, True)
                if gate['open']:                                # only ever for A itself, reported as A
                    self.assertEqual((gate['pause_reason'], gate['pause_id']), ('maintenance: A', id_a))
                else:
                    self.assertNotEqual(gate['pause_id'], id_a)
        finally:
            stop.set()
            thread.join(10)

    def test_write_mode_runs_under_the_gate_transition_mutex(self):
        real = guard.write_atomic
        held = []

        def write(path, data):
            if Path(path).name == 'supervisor_mode.json':
                held.append(paid_gate.lock_busy(self.root))     # (the OS lock of paid_gate.transition_lock: post-cut-over blocker 2)
            return real(path, data)
        with mock.patch.object(guard, 'write_atomic', write):
            self.assertEqual(modes.write_mode(self.root, 'pause')['mode'], 'pause')
        self.assertEqual((held, paid_gate.lock_busy(self.root)), ([True], False))                 # held for the write, released after it
        before = (self.batch() / 'supervisor_mode.json').read_bytes()
        with paid_gate.transition_lock(self.root, what='a paid click'):                           # a click's check + dispatch is running
            with mock.patch.object(modes, 'LOCK_WAIT', 0.2):
                with self.assertRaises(modes.ModeError):
                    modes.write_mode(self.root, 'run')
            self.assertEqual((self.batch() / 'supervisor_mode.json').read_bytes(), before)        # nothing was written meanwhile
            done = []
            thread = threading.Thread(target=lambda: done.append(modes.write_mode(self.root, 'run')['mode']), daemon=True)
            thread.start()                                      # a writer RETRIES while the mutex is busy ...
            time.sleep(0.3)
            self.assertEqual((done, (self.batch() / 'supervisor_mode.json').read_bytes()), ([], before))
        thread.join(30)                                         # ... and writes once the click is through
        self.assertEqual((done, modes.read_mode(self.root)['mode']), (['run'], 'run'))


    def test_gate_cli_is_read_only_and_never_takes_the_transition_mutex(self):
        # a paid click holds the mutex WHILE it runs the gate command: the command must answer, not wait for the mutex
        modes.write_mode(self.root, 'views-run')
        args = {'run_now': True, 'override_pause': None}
        before = sorted(p.name for p in self.batch().iterdir())
        with paid_gate.transition_lock(self.root, what='a paid click'):
            held = sorted(p.name for p in self.batch().iterdir())
            start = time.monotonic()
            proc = subprocess.run(monster_lane.gate_command(self.root, args, 1000, True), capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(monster_lane.main(['gate', '--root', str(self.root), '--task-args', json.dumps(args), '--idle', '1000']), 0)
            self.assertTrue(monster_lane.gate(self.root, args, 1000, True)['open'])
            self.assertLess(time.monotonic() - start, 30)
            self.assertEqual(sorted(p.name for p in self.batch().iterdir()), held)                # nothing written, the mutex untouched
        self.assertEqual(sorted(p.name for p in self.batch().iterdir()), before)


class LastCheckPaidTest(FinalVerifyCase):
    """Row 8: the paid-ledger error file is updated under the ledger lock (read-merge-write): concurrent failures keep both repairs."""

    def records(self):
        out = []
        for n in (1, 2):
            record = {'model_id': f'm{n}', 'actions': {'geometry': {'confirmed_at': 't', 'observed_cost': 0.5}}, 'pending_action': None}
            path = self.root / f'rec{n}' / 'studio_submission.json'
            path.parent.mkdir()
            path.write_text(json.dumps(record))
            out.append((path, record))
        return out

    def test_two_concurrent_ledger_failures_keep_both_repairs(self):
        records = self.records()
        monster_lane.paid_log_path(self.root).mkdir(parents=True)                                 # the ledger cannot be written
        real = monster_lane._read_errors

        def slow_read(root):                                    # widens the read-merge-write window of the error file
            value = real(root)
            time.sleep(0.15)
            return value
        barrier = threading.Barrier(2)
        texts = []

        def fail(path, record):
            barrier.wait(10)
            texts.append(monster_lane.reconcile_paid(self.root, 'Giant Rat', 'main', ['front.png'], path, record))
        with mock.patch.object(monster_lane, '_read_errors', slow_read):
            threads = [threading.Thread(target=fail, args=pair) for pair in records]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
        self.assertEqual(len(texts), 2)
        self.assertTrue(all(t and 'could not be written' in t for t in texts), texts)
        errors = monster_lane.paid_summary(self.root)['errors']
        self.assertEqual(sorted(e['record'] for e in errors), sorted(p.as_posix() for p, _r in records))    # BOTH errors are kept
        self.assertEqual(sorted(a['id'] for e in errors for a in e['actions']), ['hyper3d:m1:geometry', 'hyper3d:m2:geometry'])
        monster_lane.paid_log_path(self.root).rmdir()                                             # the ledger works again
        self.assertEqual(monster_lane.repair_accounting(self.root), [])
        summary = monster_lane.paid_summary(self.root)
        self.assertEqual((summary['count'], summary['credits'], summary['errors']), (2, 1.0, []))  # 1.0 credits, not 0.5

    def test_a_failure_during_another_records_repair_is_not_overwritten(self):
        (path1, record1), (path2, record2) = self.records()
        monster_lane.paid_log_path(self.root).mkdir(parents=True)
        monster_lane.reconcile_paid(self.root, 'Giant Rat', 'main', ['front.png'], path1, record1)
        monster_lane.paid_log_path(self.root).rmdir()
        real = monster_lane._update_error
        late = []

        def update(root, rec_text, change):
            if not late and rec_text == path1.as_posix():       # record 2 fails while record 1's repair is about to clear its entry
                late.append(1)
                real(root, path2.as_posix(), lambda current: {'time': 't', 'error': 'the paid log could not be written (test)',
                                                              'monster': 'Giant Rat', 'variant': 'main', 'views': ['front.png'],
                                                              'model_id': 'm2', 'actions': monster_lane.paid_actions(record2)})
            return real(root, rec_text, change)
        with mock.patch.object(monster_lane, '_update_error', update):
            self.assertIsNone(monster_lane.reconcile_paid(self.root, 'Giant Rat', 'main', ['front.png'], path1, record1))
        errors = monster_lane.paid_summary(self.root)['errors']
        self.assertEqual([e['record'] for e in errors], [path2.as_posix()])                       # 1 repaired, 2 still listed
        self.assertEqual(monster_lane.repair_accounting(self.root), [])
        summary = monster_lane.paid_summary(self.root)
        self.assertEqual((summary['count'], summary['credits'], summary['errors']), (2, 1.0, []))


@unittest.skipUnless(os.name == 'nt', 'Windows Job Objects')
class LastCheckTreeTest(FinalVerifyCase):
    """Row 9: a final empty-job check immediately before the ownership record is unlinked."""

    def test_successor_spawned_during_cleanup_keeps_the_record(self):
        import psutil
        mid, v, task_id, gen = self.frozen()
        stub = Path(self.temp.name) / 'successor_stub.py'
        stub.write_text(SUCCESSOR_STUB)
        go, note = Path(self.temp.name) / 'go', Path(self.temp.name) / 'successor.pid'
        driver = subprocess.run([sys.executable, str(stub), str(HERE), str(self.root), v, str(go), str(note)], capture_output=True,
                                text=True, timeout=120)
        successor = int(note.read_text()) if note.exists() else 0
        self.addCleanup(lambda: successor and psutil.pid_exists(successor) and psutil.Process(successor).kill())
        self.assertEqual(driver.returncode, 0, driver.stderr)
        job, member = driver.stdout.split()
        self.assertTrue(job.startswith(monster_lane.JOB_PREFIX), driver.stdout)                   # a REAL job object owned the tree
        self.assertFalse(psutil.pid_exists(int(member)))                                          # the member exited during the cleanup
        self.assertTrue(psutil.pid_exists(successor))                                             # its successor lives
        path = monster_lane.children_path(self.root, 'Giant Rat', v)
        self.assertTrue(path.exists())                                                           # the record was NOT unlinked
        by_pid = {d['pid']: d for d in monster_lane.recorded_children(self.root, 'Giant Rat', v)['descendants']}
        self.assertIn(successor, by_pid)
        self.assertIsNotNone(by_pid[successor]['create'])
        self.assertIn(str(successor), monster_lane.tree_hold(self.root, 'Giant Rat', v))          # the slot stays held, by name
        plan = monster_lane.plan_starts(self.root, idle=1000, idle_known=True)
        self.assertEqual((plan['start'], plan['active']), ([], 1))
        drained = monster_lane.drain_tree(self.root, 'Giant Rat', v, monster_lane.tree_plan(self.root, 'Giant Rat', v), 10.0)
        self.assertEqual(drained['problems'], [])
        self.assertFalse(psutil.pid_exists(successor))
        self.assertFalse(path.exists())

    def test_final_look_decides_the_unlink(self):
        mid, v, task_id, gen = self.frozen()
        path = monster_lane.children_path(self.root, 'Giant Rat', v)
        sleeper = self.sleeper()
        for members, kept, text in (([], False, None), ([sleeper.pid], True, None), (None, True, 'could not be queried')):
            tracker = monster_lane._Tracker(self.root, 'Giant Rat', v)
            tracker._scan = lambda: False                       # the earlier looks of the cleanup saw nothing ...
            tracker._job_survivors = lambda: None
            tracker._handle = 1
            looks = []

            def job_pids(handle, members=members, looks=looks):
                looks.append(1)                                 # ... the FINAL look, right before the unlink, sees `members`
                return None if members is None else [os.getpid()] + members
            with mock.patch.object(monster_lane, 'job_pids', job_pids), mock.patch.object(monster_lane, 'job_close'):
                tracker.close()
            self.assertEqual((looks, path.exists()), ([1], kept), members)
            if kept:
                data = json.loads(path.read_text())
                if text:
                    self.assertIn(text, data['incomplete'])
                else:
                    self.assertEqual([d['pid'] for d in data['descendants']], [sleeper.pid])
                path.unlink()


class LastCheckJournalTest(FinalVerifyCase):
    """Row 11: the journal must prove its recovery target (names_entry derivable from the journal + names_prev, and from the file)."""

    def journal(self, **fields):
        path = mv.journal_path(self.root, 'Giant Rat')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema': 1, 'time': 't', 'monster': 'Giant Rat'} | fields))
        return path

    def refused(self, mid, v, needle, **fields):
        """A journal with these fields changes NOTHING: names, views and the journal stay byte-identical, the card is damaged."""
        names_path = mv.variant_names_path(self.mroot, 'Giant Rat')
        vdir = mv.views_dir(self.mroot, 'Giant Rat', v)
        names_before, views_before = names_path.read_bytes(), (vdir / 'views.json').read_bytes()
        path = self.journal(op='import', variant=v, tmp=f'.v2_pick_{v}_0123abcd', **fields)
        before = path.read_bytes()
        with self.assertRaises(guard.Unreadable) as caught:
            mv.read_journal(self.root, 'Giant Rat')
        self.assertIn(needle, str(caught.exception))
        out = monster_lane.tick(self.root)
        self.assertTrue(any(path.name in e and needle in e for e in out['errors']), out['errors'])
        self.assertFalse(any('completed' in r or 'rolled back' in r for r in out['recovered']), out['recovered'])
        self.assertEqual(names_path.read_bytes(), names_before)                                  # the provenance is NOT erased
        self.assertEqual(path.read_bytes(), before)                                              # the journal is NOT deleted
        self.assertEqual((vdir / 'views.json').read_bytes(), views_before)
        mv.invalidate()
        card = self.tabs()['approved'][mid]
        self.assertEqual((card['actions'], bool(card['damaged'])), ([], True))                   # a read-only damaged card
        self.assertIn(needle, card['damaged']['error'])
        with self.assertRaises(guard.Unreadable):
            self.act(mid, 'views', enabled={'front': True})
        with self.assertRaises(guard.Unreadable):
            self.act('mconcept:Giant Rat/codex2', 'approve')
        self.assertEqual((names_path.read_bytes(), path.read_bytes()), (names_before, before))
        path.unlink()
        mv.invalidate()

    def test_damaged_import_journal_with_only_the_import_stamps(self):
        mid, v = self.pick_rat()                                                                 # an existing pick with its provenance
        names = json.loads(mv.variant_names_path(self.mroot, 'Giant Rat').read_text())['variants'][v]
        self.assertEqual(names['was'], 'codex1')
        stamps = {'imported': '2026-11-25T10:00:00+00:00', 'imported_by': 'v2'}
        # the reviewer's journal: what is left of names_entry are the import stamps
        self.refused(mid, v, 'only the import stamps', names_entry=dict(stamps), names_prev=None)
        self.refused(mid, v, 'only the import stamps', names_entry=dict(stamps), names_prev={})
        self.refused(mid, v, 'only the import stamps', names_entry=dict(stamps), names_prev=dict(names))
        # an entry that is not names_prev + the stamps (a field of the earlier record was lost)
        self.refused(mid, v, 'not names_prev plus the import stamps', names_entry={'concept': names['concept']} | stamps,
                     names_prev=dict(names))
        # no names_prev and not the record of an approved concept
        self.refused(mid, v, 'not the record of an approved concept', names_entry={'note': 'x'} | stamps, names_prev=None)
        # internally consistent, but the file holds ANOTHER record of that variant: the journal does not prove the target
        self.refused(mid, v, 'does not prove what to write', names_entry={'was': 'codex9', 'concept': 'codex9-front.png'} | stamps,
                     names_prev=None)
        self.refused(mid, v, 'does not prove what to write', names_entry={'was': 'codex9'} | stamps, names_prev={'was': 'codex9'})
        self.assertFalse(self.tabs()['approved'][mid].get('damaged'))                             # repaired by hand: usable again
        self.assertIsInstance(self.act(mid, 'views', enabled={'front': True}), dict)

    def test_record_rules_for_an_import(self):
        stamps = {'imported': 't', 'imported_by': 'v2'}
        base = {'op': 'import', 'monster': 'Giant Rat', 'variant': '1', 'tmp': '.v2_pick_1_0123abcd'}
        ok = [base | {'names_entry': {'was': 'codex1 (then 1)'} | stamps, 'names_prev': {'was': 'codex1 (then 1)'}},
              base | {'names_entry': {'was': 'codex1', 'x': 1} | stamps, 'names_prev': {'was': 'codex1', 'x': 1, 'imported': 'old'}},
              base | {'names_entry': {'was': 'codex3', 'concept': 'codex3-front.png', 'how': 'migration mapping'} | stamps,
                      'names_prev': None}]
        bad = [base | {'names_entry': dict(stamps), 'names_prev': None}, base | {'names_entry': dict(stamps), 'names_prev': {}},
               base | {'names_entry': dict(stamps), 'names_prev': {'was': 'codex1'}},
               base | {'names_entry': {'was': 'codex2'} | stamps, 'names_prev': {'was': 'codex1'}},
               base | {'names_entry': {'was': 'codex1', 'extra': 1} | stamps, 'names_prev': {'was': 'codex1'}},
               base | {'names_entry': {'was': 'main'} | stamps, 'names_prev': None},
               base | {'names_entry': {'was': 'codex3', 'concept': 'codex4-front.png'} | stamps, 'names_prev': None}]
        for record in ok:
            self.assertIsNone(mv.journal_record_problem(record, 'Giant Rat'), record)
        for record in bad:
            self.assertIsNotNone(mv.journal_record_problem(record, 'Giant Rat'), record)

    def test_real_interrupted_publications_still_recover(self):
        # the journals the app writes prove their target at every crash point: before and after the names entry is published
        for fault in ('import_names',):
            mv.FAULTS[fault] = lambda: (_ for _ in ()).throw(Crash())
            with self.assertRaises(Crash):
                mv.import_pick(self.root, 'Hobgoblin', '1')
            mv.FAULTS.clear()
            self.assertIsNone(mv.journal_problem(self.root, 'Hobgoblin'))
            self.assertIn('Hobgoblin: completed', monster_lane.tick(self.root)['recovered'])
        entry = json.loads(mv.variant_names_path(self.mroot, 'Hobgoblin').read_text())['variants']['1']
        self.assertEqual((entry['was'], entry['concept'], entry['imported_by']), ('codex1', 'codex1-front.png', 'v2'))
        mv.FAULTS['pick_renamed'] = lambda: (_ for _ in ()).throw(Crash())
        with self.assertRaises(Crash):
            self.act('mconcept:Giant Rat/codex1', 'approve')
        mv.FAULTS.clear()
        self.assertIsNone(mv.journal_problem(self.root, 'Giant Rat'))
        self.assertEqual(monster_lane.tick(self.root)['recovered'], ['Giant Rat: completed'])


class PostCutoverPauseTest(FinalVerifyCase):
    """Post-cut-over review (review_v2_post_cutover_20261126), blocker 1: Run-now consent covers EVERY independent pause in force, and
    a supervisor tick that rewrites PAUSE never turns an unconfirmed maintenance pause into a confirmed one. A PAUSE file is the
    supervisor's only when its record (supervisor_pause.json, DESIGN_PHASE4.md I.11) proves it (`mirror` = sha256 of the PAUSE bytes)."""

    OLD_REFLECTION = {'since': '2026-11-26T09:00:00', 'reason': 'manual pause-conversions', 'scope': ['conversions']}
    child_gate = FinalVerifyPauseTest.child_gate            # the paid child's click-time check, as the driver configures it

    def old_tick(self):
        """One tick of the PRE-fix supervisor in mode pause-conversions (pipeline_supervisor.py tick_screen as reviewed): a PAUSE whose
        reason / scope differ is OVERWRITTEN with its reflection of the mode, keeping `since`; no record of its own."""
        pause = self.batch() / 'PAUSE'
        current = json.loads(pause.read_text()) if pause.exists() else {}
        if current.get('reason') != 'manual pause-conversions' or current.get('scope') != ['conversions']:
            guard.write_atomic(pause, (json.dumps({'since': current.get('since') or 'now', 'reason': 'manual pause-conversions',
                                                   'scope': ['conversions']}, indent=2) + '\n').encode())

    def closed_everywhere(self, args, text=None):
        gate = monster_lane.gate(self.root, args, 10, True)
        self.assertEqual((gate['open'], gate['code'], gate['needs_confirm']), (False, 'paused', True), gate)
        self.assertEqual(monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start'], [])     # the lane
        self.assertIsNotNone(self.child_gate(args))                                                          # the paid child
        self.assertEqual(monster_lane.main(['gate', '--root', str(self.root), '--task-args', json.dumps(args), '--idle', '10']), 1)
        if text:
            self.assertIn(text, gate['reason'])
        return gate

    def open_everywhere(self, args):
        self.assertTrue(monster_lane.gate(self.root, args, 10, True)['open'])
        self.assertEqual(len(monster_lane.plan_starts(self.root, idle=10, idle_known=True)['start']), 1)
        self.assertIsNone(self.child_gate(args))

    def confirmed(self):
        mid, v, task_id, gen = self.frozen()
        ident = {'id': task_id} | self.ident(task_id)
        seen = modes.write_mode(self.root, 'pause-conversions')['pause_id']
        out = actions.queue_op(self.root, {'op': 'run_now', 'confirm': True, 'pause_id': seen} | ident)
        self.assertEqual(out['pause_id'], seen)
        return task_id, ident, seen, taskqueue.for_root(self.root).get(task_id)['args']

    def test_a_maintenance_pause_overwritten_by_a_supervisor_tick_keeps_the_gate_closed(self):
        # The reviewer's sequence: consent for `pause-conversions`, then a separate maintenance PAUSE, then ONE supervisor tick.
        task_id, ident, seen, args = self.confirmed()
        self.open_everywhere(args)                                # the confirmed Run now runs through the mode pause
        pause = self.batch() / 'PAUSE'
        pause.write_text(json.dumps({'since': '2026-11-26T09:05:00', 'reason': 'maintenance: disk swap', 'scope': ['conversions']}))
        self.closed_everywhere(args, 'maintenance: disk swap')    # an independent pause nobody confirmed
        self.old_tick()                                           # the supervisor rewrites it as its reflection of the mode
        self.assertEqual(json.loads(pause.read_text())['reason'], 'manual pause-conversions')
        gate = self.closed_everywhere(args)                       # ... and the SAME consent does not reopen the gate
        manual = modes.manual_pause(self.root)
        self.assertEqual([p['kind'] for p in manual['pauses']], ['mode', 'pause'])
        self.assertNotEqual(gate['pause_id'], seen)
        self.old_tick()                                           # further ticks change nothing
        self.closed_everywhere(args)
        # the old consent is refused; only a confirmation that names what is in force now opens it, and only for exactly that
        with self.assertRaises(monster_lane.ConfirmPaused) as caught:
            actions.queue_op(self.root, {'op': 'run_now', 'confirm': True, 'pause_id': seen} | ident)
        both = caught.exception.extra['pause_id']
        self.assertEqual(both, manual['pause_id'])
        actions.queue_op(self.root, {'op': 'run_now', 'confirm': True, 'pause_id': both} | ident)
        args = taskqueue.for_root(self.root).get(task_id)['args']
        self.open_everywhere(args)
        pause.write_text(json.dumps({'since': '2026-11-26T09:20:00', 'reason': 'maintenance: again', 'scope': ['conversions']}))
        self.closed_everywhere(args, 'maintenance: again')
        self.old_tick()
        self.closed_everywhere(args)                              # again: rewritten, still not confirmed

    def test_a_rewritten_pause_with_the_bytes_of_a_confirmed_one_is_still_a_new_pause(self):
        # The consent names the mode AND an (unproven) reflection PAUSE; maintenance replaces it; the tick writes the SAME bytes back.
        mid, v, task_id, gen = self.frozen()
        ident = {'id': task_id} | self.ident(task_id)
        modes.write_mode(self.root, 'pause-conversions')
        pause = self.batch() / 'PAUSE'
        body = (json.dumps(self.OLD_REFLECTION, indent=2) + '\n').encode()
        guard.write_atomic(pause, body)                           # an older supervisor's reflection: nothing proves whose it is
        both = monster_lane.pause_id(self.root)
        self.assertEqual([p['kind'] for p in modes.manual_pause(self.root)['pauses']], ['mode', 'pause'])
        actions.queue_op(self.root, {'op': 'run_now', 'confirm': True, 'pause_id': both} | ident)
        args = taskqueue.for_root(self.root).get(task_id)['args']
        self.open_everywhere(args)
        guard.write_atomic(pause, json.dumps(self.OLD_REFLECTION | {'reason': 'maintenance'}).encode())
        self.closed_everywhere(args, 'maintenance')
        time.sleep(0.02)
        guard.write_atomic(pause, body)                           # byte for byte what the user confirmed, but another write
        self.assertEqual(pause.read_bytes(), body)
        self.closed_everywhere(args)
        self.assertNotEqual(monster_lane.pause_id(self.root), both)

    def test_only_the_supervisors_record_makes_a_pause_file_its_reflection(self):
        task_id, ident, seen, args = self.confirmed()
        mirror = self.supervisor_pause('manual pause-conversions', ['conversions'], '2026-11-26T09:00:00')   # the new supervisor ticked
        self.assertEqual(monster_lane.pause_id(self.root), seen)  # the same pause as the mode: one consent covers both
        self.open_everywhere(args)
        pause = self.batch() / 'PAUSE'
        # a maintenance pause replaces the mirror; the supervisor leaves a PAUSE that is not its own alone (I.11)
        guard.write_atomic(pause, json.dumps({'since': 'x', 'reason': 'maintenance: disk swap', 'scope': ['conversions']}).encode())
        self.closed_everywhere(args, 'maintenance: disk swap')
        # nothing a PAUSE file SAYS makes it the supervisor's: not the reflection's wording, not a writer's name, not the idle reason
        for claim in ({'since': 'x', 'reason': 'manual pause-conversions', 'scope': ['conversions']},
                      {'since': '2026-11-26T09:00:00', 'reason': 'manual pause-conversions', 'scope': ['conversions'],
                       'written_by': 'supervisor'},
                      {'since': 'x', 'reason': 'user active (views keep running)', 'scope': ['conversions']},
                      {'since': 'x', 'reason': 'user active', 'scope': ['conversions', 'views']}):
            guard.write_atomic(pause, json.dumps(claim).encode())
            gate = self.closed_everywhere(args)
            self.assertEqual([p['kind'] for p in modes.manual_pause(self.root)['pauses']], ['mode', 'pause'], claim)
            self.assertNotEqual(gate['pause_id'], seen)
        guard.write_atomic(pause, mirror)                         # exactly the bytes the supervisor's record names: proven its mirror
        self.assertEqual(monster_lane.pause_id(self.root), seen)
        self.open_everywhere(args)
        pause.unlink()                                            # no PAUSE at all: the supervisor's record still reflects the mode
        self.open_everywhere(args)

    def test_the_supervisors_own_pause_is_read_from_its_record(self):
        mid, v, task_id, gen = self.frozen()
        batch = self.batch()
        modes.write_mode(self.root, 'views-run')
        # idle: only the supervisor's record makes a `user active` pause the idle gate (Run now overrides it, nothing to confirm)
        self.supervisor_pause('user active (views keep running)', ['conversions'])
        gate = monster_lane.gate(self.root, {}, 1000, True)
        self.assertEqual((gate['open'], gate['code'], gate['paused'], gate['supervisor_pause']['reason']),
                         (False, 'user_present', False, 'user active (views keep running)'))
        self.assertTrue(monster_lane.gate(self.root, {'run_now': True}, 10, True)['open'])
        self.supervisor_resume(mirror=False)                      # the same PAUSE without the record: an independent pause
        gate = monster_lane.gate(self.root, {'run_now': True}, 10, True)
        self.assertEqual((gate['open'], gate['code'], gate['needs_confirm']), (False, 'paused', True))
        self.assertEqual(modes.manual_pause(self.root)['kind'], 'pause')
        (batch / 'PAUSE').unlink()
        # the reflection of a mode that is NOT the current one (the supervisor has not ticked since the mode changed): independent
        self.supervisor_pause('manual pause', ['conversions', 'views'])
        manual = modes.manual_pause(self.root)
        self.assertEqual((manual['kind'], manual['reason']), ('supervisor', 'supervisor pause: manual pause'))
        gate = monster_lane.gate(self.root, {'run_now': True}, 1000, True)
        self.assertEqual((gate['open'], gate['code']), (False, 'paused'))
        self.assertTrue(monster_lane.gate(self.root, {'run_now': True, 'override_pause': manual['pause_id']}, 10, True)['open'])
        modes.write_mode(self.root, 'pause')                      # ... and of the current one: the same pause as the mode
        self.assertEqual([p['kind'] for p in modes.manual_pause(self.root)['pauses']], ['mode'])
        # a record that cannot be read pauses, and proves nothing about PAUSE
        modes.write_mode(self.root, 'views-run')
        (batch / 'supervisor_pause.json').write_text('{not json')
        manual = modes.manual_pause(self.root)
        self.assertEqual([p['kind'] for p in manual['pauses']], ['supervisor', 'pause'])
        self.assertFalse(monster_lane.gate(self.root, {'run_now': True}, 1000, True)['open'])
        snap = modes.snapshot(self.root, supervisor=(False, ''))
        self.assertEqual(snap['pause_id'], manual['pause_id'])
        self.assertEqual(set(monster_lane.gate(self.root, {}, 10, True)['supervisor_pause']), {'since', 'reason', 'scope', 'pause_id'})
        self.assertEqual(modes.OWN_PAUSE, paid_gate.SUPERVISOR_PAUSE)

    def test_the_pair_of_files_caught_between_its_two_writes_is_looked_at_once_more(self):
        modes.write_mode(self.root, 'pause-conversions')
        seen = modes.read_mode(self.root)['pause_id']
        self.supervisor_pause('manual pause-conversions', ['conversions'], 'A')
        new = self.supervisor_pause('manual pause-conversions', ['conversions'], 'B', mirror=False)   # the record first ...
        looks = []

        def finish(path):                                         # ... and the mirror right after this reader's first look
            if Path(path).name == 'supervisor_pause.json':
                looks.append(1)
                if len(looks) == 1:
                    guard.write_atomic(self.batch() / 'PAUSE', new)
        modes.READS.append(finish)
        self.addCleanup(lambda: finish in modes.READS and modes.READS.remove(finish))
        gate = monster_lane.gate(self.root, {'run_now': True, 'override_pause': seen}, 10, True)
        self.assertEqual((gate['open'], gate['pause_id'], len(looks)), (True, seen, 2))
        # a pair that stays inconsistent is an independent pause (fail closed), decided on the SECOND set of snapshots alone
        self.supervisor_pause('manual pause-conversions', ['conversions'], 'C', mirror=False)
        del looks[:]
        looks.append(0)                                           # (the hook writes nothing this time)
        gate = monster_lane.gate(self.root, {'run_now': True, 'override_pause': seen}, 10, True)
        self.assertEqual((gate['open'], gate['code']), (False, 'paused'))


class PostCutoverRetryTest(FinalVerifyCase):
    """Post-cut-over review (review_v2_post_cutover_20261126), blocker 4: a stale Retry never erases a newer failure. Retry is bound to
    the displayed disposition revision (`conversion_seq`), and a Retry that loses the queue transition puts the disposition back."""

    def fail_attempt(self, task_id, error):
        q = taskqueue.for_root(self.root)
        q.claim(task_id, f'tok-{error}', None)
        orch = orchestrator.Orchestrator(self.root, idle=lambda: 10.0, scan_stale=False)
        orch._finish(q.get(task_id), {'exit': 1, 'last_json': {'status': 'failed', 'step': 'collect', 'error': error}})
        self.assertEqual(q.get(task_id)['state'], 'failed')

    def rebuild(self):
        self.drop_queue()
        return monster_lane.tick(self.root)

    def test_the_displayed_revision_is_shown_and_required(self):
        mid, v, task_id, gen = self.frozen()
        self.fail_attempt(task_id, 'first')
        seq = mv.disposition_seq(self.vj(v))
        self.assertEqual(mv.item(self.root, mid)['conversion']['conversion_seq'], seq)
        row = [t for t in orchestrator.queue_state(self.root, True)['tasks'] if t['id'] == task_id][0]
        self.assertEqual((row['state'], row['conversion_seq']), ('failed', seq))
        for bad in ({}, {'conversion_seq': None}, {'conversion_seq': True}, {'conversion_seq': str(seq)}):
            with self.assertRaises(ValueError):
                actions.queue_op(self.root, {'op': 'retry', 'id': task_id} | self.ident(task_id) | bad)
            with self.assertRaises(ValueError):
                actions.queue_op(self.root, {'op': 'run_now', 'id': task_id} | self.ident(task_id) | bad)
        self.assertEqual(mv.disposition_of(self.vj(v), gen)['reason'], 'first')                   # nothing was cleared
        self.assertEqual(actions.queue_op(self.root, {'op': 'retry', 'id': task_id, 'conversion_seq': seq} | self.ident(task_id))['state'],
                         'queued')
        self.assertIsNone(mv.disposition_of(self.vj(v), gen))

    def test_stale_retry_after_a_newer_failure_then_rebuild_stays_failed(self):
        mid, v, task_id, gen = self.frozen()
        self.fail_attempt(task_id, 'first')
        shown = {'op': 'retry', 'id': task_id, 'conversion_seq': self.seq(task_id)} | self.ident(task_id)   # what an old tab displays
        self.qop('retry', task_id)                                # another tab retries; the new attempt fails again
        self.fail_attempt(task_id, 'second (newer)')
        newer = mv.disposition_of(self.vj(v), gen)
        self.assertEqual((newer['state'], newer['reason']), ('failed', 'second (newer)'))
        with self.assertRaises(guard.Changed) as caught:
            actions.queue_op(self.root, shown)                    # the stale tab's Retry
        self.assertEqual(caught.exception.code, 'changed')
        with self.assertRaises(guard.Changed):
            actions.queue_op(self.root, shown | {'op': 'run_now'})   # ... and its Run now (a Retry first)
        self.assertEqual(mv.disposition_of(self.vj(v), gen), newer)                               # the newer failure is still recorded
        self.assertEqual(taskqueue.for_root(self.root).get(task_id)['state'], 'failed')
        out = self.rebuild()
        self.assertEqual((out['queued'], out['restored']), ([], [f'Giant Rat/{v}']))
        rows = taskqueue.for_root(self.root).tasks(lane=monster_lane.LANE)
        self.assertEqual([r['state'] for r in rows], ['failed'])
        self.assertEqual(monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start'], [])
        self.assertEqual(mv.item(self.root, mid)['conversion']['state'], 'failed')

    def test_retry_that_loses_the_queue_transition_keeps_the_failure(self):
        # The reviewer's case: the failure of the running attempt is recorded (record_failure runs BEFORE the row fails), a Retry made
        # on a stale `failed` row clears nothing for good: the queue transition answers False and the disposition is put back.
        mid, v, task_id, gen = self.frozen()
        self.fail_attempt(task_id, 'first')
        stale_row = dict(taskqueue.for_root(self.root).get(task_id))                               # state failed
        self.qop('retry', task_id)
        q = taskqueue.for_root(self.root)
        q.claim(task_id, 'tok2', None)                                                             # the new attempt runs ...
        monster_lane.record_failure(self.root, q.get(task_id), 'second (newer)')                   # ... and its failure is recorded
        self.assertIn(q.get(task_id)['state'], taskqueue.ACTIVE)
        seq = mv.disposition_seq(self.vj(v))
        self.assertFalse(monster_lane.retry_task(self.root, stale_row, seq))                       # even with the CURRENT revision
        kept = mv.disposition_of(self.vj(v), gen)
        self.assertEqual((kept['state'], kept['reason']), ('failed', 'second (newer)'))
        self.assertGreater(mv.disposition_seq(self.vj(v)), seq)                                    # cleared + put back: two more revisions
        with self.assertRaises(guard.Changed):
            monster_lane.retry_task(self.root, stale_row, seq)                                     # and the old revision is stale now
        q.finish(task_id, 'tok2', False, 'second (newer)')
        out = self.rebuild()
        self.assertEqual((out['queued'], out['restored']), ([], [f'Giant Rat/{v}']))
        self.assertEqual([r['state'] for r in taskqueue.for_root(self.root).tasks(lane=monster_lane.LANE)], ['failed'])
        self.assertEqual(monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start'], [])

    def test_retry_of_an_expired_row_keeps_the_disposition(self):
        mid, v, task_id, gen = self.frozen()
        self.fail_attempt(task_id, 'first')
        row = dict(taskqueue.for_root(self.root).get(task_id))
        seq = self.seq(task_id)
        with mock.patch.object(taskqueue.TaskQueue, 'retry', side_effect=taskqueue.Expired('the task is another one now')):
            with self.assertRaises(guard.Changed):
                monster_lane.retry_task(self.root, row, seq)
        self.assertEqual(mv.disposition_of(self.vj(v), gen)['reason'], 'first')
        with mock.patch.object(taskqueue.TaskQueue, 'retry', side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(sqlite3.OperationalError):
                monster_lane.retry_task(self.root, row, mv.disposition_seq(self.vj(v)))
        self.assertEqual(mv.disposition_of(self.vj(v), gen)['reason'], 'first')                    # put back also when the queue raised


class PostCutoverApprovalTest(FinalVerifyCase):
    """Post-cut-over review, blocker 5: Unapprove and Undo name the approval revision that was displayed (the revision of
    chosen_lod.json), compared under the variant lock; a stale one answers 409 `changed` and the newer approval stays."""

    def reduced(self):
        mid, v, task_id, gen = self.frozen()
        q = taskqueue.for_root(self.root)
        q.claim(task_id, 'tok', None)
        q.finish(task_id, 'tok', True)
        self.converted('Giant Rat', v)
        return mid, v, mv.models_dir(self.mroot, 'Giant Rat', v)

    def approve(self, mid, lod):
        return self.act(mid, 'approve', lod=lod, glb_sha256=mv.item(self.root, mid)['lods'][lod]['sha256'])

    def choice(self, mdir):
        return json.loads((mdir / 'chosen_lod.json').read_text())

    def test_stale_0p4_unapprove_after_a_0p2_approval_is_409_and_the_approval_is_kept(self):
        mid, v, mdir = self.reduced()
        self.approve(mid, '0p4')
        old_tab = mv.item(self.root, mid)                         # an old tab shows: approved at 0p4
        self.assertEqual((old_tab['approved'], old_tab['lod']), (True, '0p4'))
        self.unapprove(mid)                                       # elsewhere: unapproved, then approved at 0p2
        second = self.approve(mid, '0p2')
        before = (mdir / 'chosen_lod.json').read_bytes()
        with self.assertRaises(guard.Changed) as caught:
            self.act(mid, 'unapprove', approval_rev=old_tab['approval_rev'])
        self.assertEqual(caught.exception.code, 'changed')
        self.assertEqual((mdir / 'chosen_lod.json').read_bytes(), before)                          # the 0p2 approval is untouched
        self.assertEqual((self.choice(mdir)['approved'], self.choice(mdir)['lod']), (True, '0p2'))
        self.assertIn(mid, self.tabs()['completed'])
        with self.assertRaises(ValueError):
            self.act(mid, 'unapprove')                            # no revision at all: refused, never "whatever is approved now"
        self.assertEqual((mdir / 'chosen_lod.json').read_bytes(), before)
        r = self.act(mid, 'unapprove', approval_rev=second['approval_rev'])                        # the tab that shows 0p2 can
        self.assertEqual((r['approved'], r['lod'], r['stage']), (False, '0p2', '3d'))

    def test_stale_undo_never_reverses_a_later_decision(self):
        mid, v, mdir = self.reduced()
        first = self.approve(mid, '0p4')
        undo_approve = first['undo']                              # the toast's Undo of the 0p4 approval
        self.assertEqual(undo_approve, {'verb': 'unapprove', 'id': mid, 'args': {'approval_rev': first['approval_rev']}})
        un = self.unapprove(mid)
        undo_unapprove = un['undo']                               # the toast's Undo of that Unapprove: approve 0p4 again
        self.assertEqual(undo_unapprove['args']['approval_rev'], un['approval_rev'])
        self.approve(mid, '0p2')                                  # a later decision
        before = (mdir / 'chosen_lod.json').read_bytes()
        for undo in (undo_approve, undo_unapprove):
            with self.assertRaises(guard.Changed):
                self.act(undo['id'], undo['verb'], **undo['args'])
            self.assertEqual((mdir / 'chosen_lod.json').read_bytes(), before)
        self.unapprove(mid)                                       # later still: not approved, level 0p2
        before = (mdir / 'chosen_lod.json').read_bytes()
        with self.assertRaises(guard.Changed):
            self.act(undo_unapprove['id'], undo_unapprove['verb'], **undo_unapprove['args'])       # would have approved 0p4 again
        with self.assertRaises(guard.Changed):                    # a stale Unapprove of a variant that is NOT approved any more is 409 too:
            self.act(mid, 'unapprove', **undo_approve['args'])    # it never falls through to undoing the pick
        self.assertEqual((mdir / 'chosen_lod.json').read_bytes(), before)
        self.assertTrue(mv.views_dir(self.mroot, 'Giant Rat', v).is_dir())

    def test_a_fresh_undo_works_and_every_step_is_a_new_revision(self):
        mid, v, mdir = self.reduced()
        first = self.approve(mid, '0p4')
        r = self.act(first['undo']['id'], first['undo']['verb'], **first['undo']['args'])          # Undo of the approval
        self.assertEqual((r['approved'], r['stage']), (False, '3d'))
        again = self.act(r['undo']['id'], r['undo']['verb'], **r['undo']['args'])                  # Undo of the Undo: approved at 0p4 again
        self.assertEqual((again['approved'], again['lod']), (True, '0p4'))
        self.assertEqual(len({first['approval_rev'], r['approval_rev'], again['approval_rev']}), 3)   # the same level again: a new revision
        with self.assertRaises(guard.Changed):
            self.act(mid, 'unapprove', approval_rev=first['approval_rev'])                         # the first approval's tab is stale
        # an LOD pick is a write of the record too: the revision an unapproved item shows changes with it
        self.unapprove(mid)
        shown = mv.item(self.root, mid)['approval_rev']
        import decisions
        decisions.write_monster_lod(self.root, mid, '0p1')
        self.assertNotEqual(mv.item(self.root, mid)['approval_rev'], shown)
        self.assertEqual(mv.choice_rev(None), 'none')

    def test_the_revision_is_compared_under_the_variant_lock(self):
        mid, v, mdir = self.reduced()
        first = self.approve(mid, '0p4')
        seen = []
        real = mv.choice_snapshot

        def snapshot(folder):
            seen.append(guard.os_lock(mdir / mv.LOD_LOCK)._depth)   # the variant lock is HELD while the record is read and compared
            return real(folder)
        with mock.patch.object(mv, 'choice_snapshot', snapshot):
            mv.write_choice(self.root, mid, False, approval_rev=first['approval_rev'])
        self.assertEqual(seen[-1], 1)                             # (earlier strict reads of the damage check are unlocked)


class FinalLiveCase(FinalVerifyCase):
    """Shared fixture of the final-live regressions (.codex_jobs/review_v2_final_live_20261126/result.md, the monster findings)."""

    def fail_attempt(self, task_id, error):
        q = taskqueue.for_root(self.root)
        q.claim(task_id, f'tok-{error}', None)
        orch = orchestrator.Orchestrator(self.root, idle=lambda: 10.0, scan_stale=False)
        orch._finish(q.get(task_id), {'exit': 1, 'last_json': {'status': 'failed', 'step': 'collect', 'error': error}})
        self.assertEqual(q.get(task_id)['state'], 'failed')

    def drive(self, v, **extra):
        return monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, idle=10, idle_known=True, say=lambda t: None, **extra)

    def scripts(self):
        return [c['script'] for c in self.calls()]


class FinalLivePauseTest(FinalLiveCase):
    """Blocker 1 (consent = pause SOURCE + file revision), "payment without pause confirmation" (an unreadable mode holds dispatch and
    refuses Run now) and "undisclosed maintenance override" (Run now is confirmed through the server's challenge)."""

    def pause_file(self, reason, **extra):
        guard.write_atomic(self.batch() / 'PAUSE', json.dumps({'since': 's', 'reason': reason, 'scope': ['conversions']} | extra).encode())

    def child_gate(self, args):
        env = paid_gate.env_for(self.root, override_pause=True, cmd=monster_lane.gate_command(self.root, args, 10, True))
        return paid_gate.closed_reason(env)

    def test_foreign_pause_that_keeps_its_pause_id_but_changes_its_reason_needs_new_consent(self):
        # The reviewer's case: a foreign PAUSE carries its own `pause_id`; it is rewritten with another reason and the SAME pause_id.
        mid, v, task_id, gen = self.frozen()
        ident = {'id': task_id} | self.ident(task_id)
        self.pause_file('maintenance: A', pause_id='kept-by-its-writer')
        with self.assertRaises(monster_lane.ConfirmPaused) as caught:
            actions.queue_op(self.root, {'op': 'run_now'} | ident)
        id_a = caught.exception.extra['pause_id']
        self.assertTrue(id_a.startswith('pause:file:'), id_a)                                    # the source and the file revision
        self.assertNotIn('kept-by-its-writer', id_a)                                            # never the id stored in the file
        actions.queue_op(self.root, {'op': 'run_now', 'confirm': True, 'pause_id': id_a} | ident)
        args = taskqueue.for_root(self.root).get(task_id)['args']
        self.assertEqual(args['override_pause'], id_a)
        self.assertTrue(monster_lane.gate(self.root, args, 10, True)['open'])
        self.assertIsNone(self.child_gate(args))
        self.pause_file('maintenance: B (nobody confirmed this)', pause_id='kept-by-its-writer')  # same stored id, another reason
        gate = monster_lane.gate(self.root, args, 10, True)
        self.assertEqual((gate['open'], gate['code'], gate['needs_confirm']), (False, 'paused', True))
        self.assertNotEqual(gate['pause_id'], id_a)
        self.assertEqual(monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start'], [])     # the lane
        self.assertIn('maintenance: B', self.child_gate(args))                                              # the paid child
        with self.assertRaises(monster_lane.ConfirmPaused) as caught:                                      # the old consent, echoed again
            actions.queue_op(self.root, {'op': 'run_now', 'confirm': True, 'pause_id': id_a} | ident)
        self.assertEqual([p['reason'] for p in caught.exception.extra['pauses']], ['maintenance: B (nobody confirmed this)'])
        # the very same bytes written again are another revision too (a consent is for one write of the file)
        id_b = monster_lane.pause_id(self.root)
        self.pause_file('maintenance: B (nobody confirmed this)', pause_id='kept-by-its-writer')
        self.assertNotEqual(monster_lane.pause_id(self.root), id_b)

    def test_the_id_names_the_source_and_a_foreign_mode_rewrite_is_a_new_pause(self):
        mode_file = self.batch() / 'supervisor_mode.json'
        guard.write_atomic(mode_file, json.dumps({'mode': 'pause', 'since': 's', 'pause_id': 'm'}).encode())
        first = modes.read_mode(self.root)['pause_id']
        self.assertTrue(first.startswith('mode:file:'), first)
        self.assertEqual(first, modes.read_mode(self.root)['pause_id'])                          # stable while the file is not rewritten
        guard.write_atomic(mode_file, json.dumps({'mode': 'pause-conversions', 'since': 's', 'pause_id': 'm'}).encode())
        second = modes.read_mode(self.root)['pause_id']
        self.assertNotEqual(first, second)
        self.assertFalse(modes.consent_covers(first, modes.manual_pause(self.root)))
        written = modes.write_mode(self.root, 'pause')                                           # our own write: the id of THAT revision
        self.assertEqual(written['pause_id'], modes.read_mode(self.root)['pause_id'])
        self.assertTrue(modes.consent_covers(written['pause_id'], modes.manual_pause(self.root)))
        self.supervisor_pause('weird', ['conversions'])                                          # an independent supervisor pause
        kinds = {p['kind']: p['pause_id'].split(':')[0] for p in modes.manual_pause(self.root)['pauses']}
        self.assertEqual(kinds, {'mode': 'mode', 'supervisor': 'supervisor'})
        # the same file identity under another source is another id: a consent never moves from one source to another
        self.assertNotEqual(modes.source_id('pause', 'file:x'), modes.source_id('mode', 'file:x'))

    def test_unreadable_mode_refuses_run_now_also_behind_a_hold(self):
        """Confirm review: with CONVERT_HOLD present AND an unreadable mode, Run now must not save an unconfirmed override."""
        mid, v, task_id, gen = self.frozen()
        ident = {'id': task_id} | self.ident(task_id)
        (self.batch() / 'CONVERT_HOLD').write_text(json.dumps({'reason': 'test hold'}))
        (self.batch() / 'supervisor_mode.json').write_bytes(b'{"mode": "pause-conv')
        gate = monster_lane.gate(self.root, {'run_now': True}, 1000, True)
        self.assertEqual((gate['open'], gate['code'], gate.get('mode_unreadable')), (False, 'held', True))
        with self.assertRaises(guard.Unreadable):
            actions.queue_op(self.root, {'op': 'run_now'} | ident)
        self.assertFalse(taskqueue.for_root(self.root).get(task_id)['args'].get('run_now'))

    def test_unreadable_mode_holds_dispatch_and_run_now_is_refused(self):
        # The reviewer's case: supervisor_mode.json cannot be read and the supervisor has mirrored nothing yet (no PAUSE, no record).
        mid, v, task_id, gen = self.frozen()
        ident = {'id': task_id} | self.ident(task_id)
        mode_file = self.batch() / 'supervisor_mode.json'
        q = taskqueue.for_root(self.root)
        for raw in (b'{"mode": "pause-conv', b'[]', b'{"mode": "paus"}', b'{}'):
            mode_file.write_bytes(raw)
            self.assertFalse((self.batch() / 'PAUSE').exists() or (self.batch() / modes.OWN_PAUSE).exists())
            for args in ({}, {'run_now': True}, {'run_now': True, 'override_pause': 'anything'}):
                gate = monster_lane.gate(self.root, args, 1000, True)                             # also while the user is away
                self.assertEqual((gate['open'], gate['code'], gate.get('mode_unreadable'), gate['needs_confirm']),
                                 (False, 'held', True, False), (raw, args))
                self.assertIn('supervisor_mode.json', gate['reason'])
                self.assertIn('supervisor_mode.json', self.child_gate(args))                      # the paid child's final check
                self.assertEqual(monster_lane.main(['gate', '--root', str(self.root), '--task-args', json.dumps(args), '--idle', '1000']), 1)
            for body in ({}, {'confirm': True}, {'confirm': True, 'pause_id': 'anything'}):
                with self.assertRaises(guard.Unreadable) as caught:                              # an unconfirmed request too
                    actions.queue_op(self.root, {'op': 'run_now'} | ident | body)
                self.assertEqual((caught.exception.code, caught.exception.status), ('unreadable', 409))
            self.assertFalse(q.get(task_id)['args'].get('run_now'))                              # nothing was marked
            self.assertEqual(monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start'], [])
            self.assertIn('supervisor_mode.json', modes.snapshot(self.root, supervisor=(True, '1'))['warning'])
            conv = mv.item(self.root, mid)['conversion']
            self.assertEqual(conv['state'], 'waiting')
            self.assertIn('supervisor_mode.json', conv['waiting_reason'])
        q.mark_run_now(task_id, None)                                                            # even a task marked earlier waits
        self.assertEqual(monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start'], [])
        # readable again (a pause mode): the ordinary confirmation; then a mode that runs: Run now is accepted
        modes.write_mode(self.root, 'pause-conversions')
        with self.assertRaises(monster_lane.ConfirmPaused):
            actions.queue_op(self.root, {'op': 'run_now'} | ident)
        modes.write_mode(self.root, 'views-run')
        self.assertTrue(actions.queue_op(self.root, {'op': 'run_now'} | ident)['run_now'])
        self.assertEqual(len(monster_lane.plan_starts(self.root, idle=10, idle_known=True)['start']), 1)
        mode_file.unlink()                                                                       # an ABSENT file is the default mode
        self.assertTrue(monster_lane.gate(self.root, {'run_now': True}, 10, True)['open'])

    def test_run_now_challenge_names_exactly_the_pauses_in_force(self):
        # The reviewer's case: the page saw "paused" (the mode); a maintenance PAUSE is added before the confirmation is sent.
        mid, v, task_id, gen = self.frozen()
        ident = {'id': task_id} | self.ident(task_id)
        modes.write_mode(self.root, 'pause-conversions')
        conv = mv.item(self.root, mid)['conversion']                                             # what the Approved modal read
        self.assertEqual((conv['waiting_reason'], [p['kind'] for p in conv['pauses']]), ('paused', ['mode']))
        self.assertEqual(conv['pause_id'], conv['pauses'][0]['pause_id'])                        # one gate snapshot
        self.pause_file('maintenance: disk swap')                                                # added between the reads
        with self.assertRaises(monster_lane.ConfirmPaused) as caught:                            # the item's id, sent as consent
            actions.queue_op(self.root, {'op': 'run_now', 'confirm': True, 'pause_id': conv['pause_id']} | ident)
        challenge = caught.exception.extra
        self.assertEqual([(p['kind'], p['reason']) for p in challenge['pauses']],
                         [('mode', 'mode pause-conversions'), ('pause', 'maintenance: disk swap')])
        self.assertEqual(challenge['pause_id'], modes.ID_JOIN.join(p['pause_id'] for p in challenge['pauses']))
        self.assertIn('maintenance: disk swap', str(caught.exception))
        self.assertFalse(taskqueue.for_root(self.root).get(task_id)['args'].get('run_now'))
        with self.assertRaises(monster_lane.ConfirmPaused):                                      # a challenge is asked for first
            actions.queue_op(self.root, {'op': 'run_now'} | ident)
        self.pause_file('maintenance: firmware')                                                 # ... and it changes while the dialog is open
        with self.assertRaises(monster_lane.ConfirmPaused) as caught:
            actions.queue_op(self.root, {'op': 'run_now', 'confirm': True, 'pause_id': challenge['pause_id']} | ident)
        newer = caught.exception.extra
        self.assertEqual([p['reason'] for p in newer['pauses']], ['mode pause-conversions', 'maintenance: firmware'])
        out = actions.queue_op(self.root, {'op': 'run_now', 'confirm': True, 'pause_id': newer['pause_id']} | ident)
        self.assertEqual((out['override_pause'], out['pause_id']), (True, newer['pause_id']))
        args = taskqueue.for_root(self.root).get(task_id)['args']
        self.assertTrue(monster_lane.gate(self.root, args, 10, True)['open'])
        self.assertEqual(monster_lane.gate(self.root, {}, 10, True)['pauses'], newer['pauses'])  # the gate lists the same pauses

    def test_the_client_confirms_through_the_server_challenge_only(self):
        web = HERE.parent / 'web'
        views = (web / 'monster_views.js').read_text(encoding='utf-8')
        self.assertNotIn('askFirst', views)                                                      # no consent from what the page read
        self.assertNotIn('pauseId', views)
        self.assertNotIn('conversion.pause_id', views)
        self.assertIn('error.body?.pauses', views)                                               # the dialog shows the server's reasons
        self.assertIn('error.body?.pause_id', views)                                             # and echoes the challenge id
        self.assertIn("import { runNowTask } from './monster_views.js'", (web / 'queue.js').read_text(encoding='utf-8'))
        self.assertIn("'pauses'", (HERE / 'server.py').read_text(encoding='utf-8'))              # the 409 body carries the list


class FinalLiveOutcomeTest(FinalLiveCase):
    """Blocker 4: a failed row is published with the failure its `conversion_seq` belongs to."""

    def test_a_row_read_before_the_second_failure_shows_the_failure_of_its_revision(self):
        mid, v, task_id, gen = self.frozen()
        self.fail_attempt(task_id, 'first')
        stale_row = dict(taskqueue.for_root(self.root).get(task_id))                             # the queue rows are read HERE ...
        vj_then = self.vj(v)
        self.qop('retry', task_id)
        self.fail_attempt(task_id, 'second (newer)')                                             # ... views.json after this
        newer = mv.disposition_of(self.vj(v), gen)
        view = orchestrator.publish_outcome(self.root, stale_row, orchestrator.task_view(stale_row, time.time()))
        self.assertEqual((view['error'], view['conversion_seq'], view['outcome_seq']),
                         ('second (newer)', mv.disposition_seq(self.vj(v)), newer['seq']))
        # the Approved modal's item: the same pair from its one views.json snapshot
        conv = mv.conversion_info(self.root, self.mroot, 'Giant Rat', v, stale_row, self.vj(v)['frozen'], vj=self.vj(v))
        self.assertEqual((conv['state'], conv['error'], conv['conversion_seq']), ('failed', 'second (newer)', view['conversion_seq']))
        conv = mv.conversion_info(self.root, self.mroot, 'Giant Rat', v, stale_row, vj_then['frozen'], vj=vj_then)
        self.assertEqual((conv['error'], conv['conversion_seq']), ('first', mv.disposition_seq(vj_then)))   # the old pair, whole
        # the Retry of what is on screen clears exactly the failure that was shown
        out = actions.queue_op(self.root, {'op': 'retry', 'id': task_id, 'conversion_seq': view['conversion_seq']} | self.ident(task_id))
        self.assertEqual(out['state'], 'queued')
        self.assertIsNone(mv.disposition_of(self.vj(v), gen))

    def test_the_queue_row_and_its_revision_are_one_outcome(self):
        mid, v, task_id, gen = self.frozen()
        self.fail_attempt(task_id, 'first ' + 'x' * 400)                                         # longer than the recorded reason
        row = [t for t in orchestrator.queue_state(self.root, True)['tasks'] if t['id'] == task_id][0]
        disp = mv.disposition_of(self.vj(v), gen)
        self.assertEqual((row['error'], row['conversion_seq'], row['outcome_seq']),
                         ('first ' + 'x' * 400, mv.disposition_seq(self.vj(v)), disp['seq']))    # its own outcome: the full text
        # a row whose text is NOT the recorded outcome (whatever wrote it) is shown with the recorded one: the pair stays whole
        q = taskqueue.for_root(self.root)
        other = dict(q.get(task_id)) | {'error': 'a failure views.json does not carry'}
        view = orchestrator.publish_outcome(self.root, other, orchestrator.task_view(other, time.time()))
        self.assertEqual((view['error'], view['outcome_seq']), (disp['reason'], disp['seq']))
        cancelled = dict(q.get(task_id)) | {'state': 'cancelled', 'error': 'manual'}             # the row says cancelled, the record failed
        view = orchestrator.publish_outcome(self.root, cancelled, orchestrator.task_view(cancelled, time.time()))
        self.assertEqual(view['error'], f'failed: {disp["reason"]}')
        self.assertEqual(mv.outcome_error({'state': 'cancelled', 'reason': 'cancelled by the user'}, 'failed', 'first'),
                         'cancelled: cancelled by the user')
        self.assertEqual(mv.outcome_error(None, 'failed', 'first'), 'first')


class ConfirmOutcomeTest(FinalLiveCase):
    """Confirm review (.codex_jobs/review_v2_confirm_20261126/result.md, "Earlier 4, Retry"): the text a failed row is shown with
    always belongs to the `conversion_seq` sent with it, also when the newer reason is a PREFIX of the older row's text."""

    OLD, NEW = 'collect failed: old material step', 'collect failed'

    def test_the_reviewers_case_an_older_longer_text_is_never_shown_with_the_newer_revision(self):
        mid, v, task_id, gen = self.frozen()
        self.fail_attempt(task_id, self.OLD)
        stale_row = dict(taskqueue.for_root(self.root).get(task_id))                             # the queue row is read HERE ...
        first = mv.disposition_of(self.vj(v), gen)
        self.qop('retry', task_id)
        self.fail_attempt(task_id, self.NEW)                                                     # ... views.json after this
        newer = mv.disposition_of(self.vj(v), gen)
        self.assertEqual((newer['reason'], newer['seq']), (self.NEW, mv.disposition_seq(self.vj(v))))
        self.assertNotEqual(first['attempt'], newer['attempt'])                                  # each failure names its attempt
        self.assertEqual(newer['attempt'], mv.attempt_ref(f'tok-{self.NEW}'))
        self.assertNotIn('tok-', json.dumps(self.vj(v)['conversion']))                           # never the token itself
        view = orchestrator.publish_outcome(self.root, stale_row, orchestrator.task_view(stale_row, time.time()))
        self.assertEqual((view['error'], view['conversion_seq'], view['outcome_seq']), (self.NEW, newer['seq'], newer['seq']))
        conv = mv.conversion_info(self.root, self.mroot, 'Giant Rat', v, stale_row, self.vj(v)['frozen'], vj=self.vj(v))
        self.assertEqual((conv['state'], conv['error'], conv['conversion_seq']), ('failed', self.NEW, newer['seq']))
        # the live row (the attempt the disposition names) is shown with its own text and the same revision
        row = [t for t in orchestrator.queue_state(self.root, True)['tasks'] if t['id'] == task_id][0]
        self.assertEqual((row['error'], row['conversion_seq'], row['outcome_seq']), (self.NEW, newer['seq'], newer['seq']))
        # Retry of what is on screen clears the failure that was SHOWN (the newer one)
        out = actions.queue_op(self.root, {'op': 'retry', 'id': task_id, 'conversion_seq': view['conversion_seq']} | self.ident(task_id))
        self.assertEqual(out['state'], 'queued')
        self.assertIsNone(mv.disposition_of(self.vj(v), gen))

    def test_the_rows_own_text_needs_the_attempt_or_the_identical_text(self):
        named = {'state': 'failed', 'reason': self.NEW, 'attempt': mv.attempt_ref('tok-new')}
        self.assertEqual(mv.outcome_error(named, 'failed', self.OLD, 'tok-old'), self.NEW)       # another attempt: the stored reason
        self.assertEqual(mv.outcome_error(named, 'failed', self.OLD), self.NEW)                  # a row without a token
        self.assertEqual(mv.outcome_error(named, 'failed', self.NEW + ' (the full text)', 'tok-new'), self.NEW + ' (the full text)')
        legacy = {'state': 'failed', 'reason': self.NEW}                                         # written before `attempt` existed
        self.assertEqual(mv.outcome_error(legacy, 'failed', self.OLD, 'tok-old'), self.NEW)      # a prefix match proves nothing
        self.assertEqual(mv.outcome_error(legacy, 'failed', self.NEW, 'tok-old'), self.NEW)
        self.assertIsNone(mv.attempt_ref(None))
        self.assertIsNone(mv.attempt_ref(''))


class FinalLiveApprovalTest(FinalLiveCase):
    """Blocker 5: the 3D / Completed modal's level, approval and `approval_rev` come from ONE chosen_lod.json snapshot."""

    def test_item_and_card_show_one_choice_snapshot_under_a_concurrent_approval(self):
        import monsters as monsters_v1
        mid, v, task_id, gen = self.frozen()
        q = taskqueue.for_root(self.root)
        q.claim(task_id, 'tok', None)
        q.finish(task_id, 'tok', True)
        self.converted('Giant Rat', v)
        mdir = mv.models_dir(self.mroot, 'Giant Rat', v)
        choice = mdir / 'chosen_lod.json'

        def approve(lod):
            return self.act(mid, 'approve', lod=lod, glb_sha256=mv.item(self.root, mid)['lods'][lod]['sha256'])
        approve('0p4')
        bytes_04 = choice.read_bytes()
        self.unapprove(mid)
        approve('0p2')
        bytes_02 = choice.read_bytes()
        choice.write_bytes(bytes_04)                             # the modal opens on the 0p4 approval ...
        real = monsters_v1.choice_snapshot

        def racing(folder):
            out = real(folder)
            if Path(folder) == mdir:
                choice.write_bytes(bytes_02)                     # ... and another tab's 0p2 approval lands right after that read
            return out
        with mock.patch.object(monsters_v1, 'choice_snapshot', racing):
            item = mv.item(self.root, mid)
        self.assertEqual((item['lod'], item['approved'], item['stage']), ('0p4', True, 'completed'))
        self.assertEqual(item['approval_rev'], mv.choice_rev(bytes_04))                          # the revision OF the approval shown
        self.assertEqual(item['card']['approval_rev'], mv.choice_rev(bytes_04))
        self.assertEqual(choice.read_bytes(), bytes_02)
        with self.assertRaises(guard.Changed):                                                   # its Unapprove never clears 0p2
            self.act(mid, 'unapprove', approval_rev=item['approval_rev'])
        self.assertEqual(choice.read_bytes(), bytes_02)
        choice.write_bytes(bytes_04)
        with mock.patch.object(monsters_v1, 'choice_snapshot', racing):
            card = self.tabs()['completed'][mid]
        self.assertEqual(card['approval_rev'], mv.choice_rev(bytes_04))
        fresh = mv.item(self.root, mid)                                                          # a later look: the 0p2 pair, whole
        self.assertEqual((fresh['lod'], fresh['approval_rev']), ('0p2', mv.choice_rev(bytes_02)))
        self.assertEqual(monsters_v1.choice_snapshot(mdir)[1], mv.choice_snapshot(mdir)[1])      # one revision formula
        self.assertEqual(monsters_v1.choice_snapshot(mdir / 'nope'), ({}, mv.NO_CHOICE))


class FinalLiveTreeTest(FinalLiveCase):
    """Blocker 7: without a Job Object the fallback kill signals the RETAINED verified psutil.Process object, never a reopened pid."""

    def tree_file(self, v, **data):
        path = monster_lane.children_path(self.root, 'Giant Rat', v)
        guard.write_atomic(path, json.dumps({'set_name': f'monster:Giant Rat/{v}', 'driver': None, 'children': []} | data).encode())
        return path

    def test_the_verified_object_is_planned_and_signalled_without_reopening_the_pid(self):
        import psutil
        mid, v, task_id, gen = self.frozen()
        child = self.sleeper()
        create = monster_lane._create_time(child.pid)
        self.tree_file(v, children=[{'pid': child.pid, 'create': create, 'argv': ['collect']}])
        verified = monster_lane._open_proven(child.pid, create)
        self.assertEqual((verified.pid, verified.create_time()), (child.pid, create))
        self.assertIsNone(monster_lane._open_proven(child.pid, create + 60))                     # the pid of ANOTHER process by now
        self.assertIsNone(monster_lane._open_proven(child.pid, None))
        opened = []
        with mock.patch.object(monster_lane, '_open_proven', lambda pid, created: opened.append(pid) or verified):
            plan = monster_lane.tree_plan(self.root, 'Giant Rat', v)
        self.assertEqual((plan['job'], opened), (None, [child.pid]))                             # ONE verified open per recorded process
        self.assertIs(plan['procs'][0], verified)                                                # that very object is kept
        calls = []
        real_kill = psutil.Process.kill

        def kill(proc):
            calls.append(proc)
            real_kill(proc)

        def no_reopen(pid, created):
            raise AssertionError(f'pid {pid} was opened again for the signal')
        with mock.patch.object(psutil.Process, 'kill', kill), mock.patch.object(monster_lane, '_open_proven', no_reopen):
            out = monster_lane.drain_tree(self.root, 'Giant Rat', v, plan, wait=20)
        self.assertEqual((out['killed'], out['problems'], [p is verified for p in calls]), (1, [], [True]))
        self.assertIsNotNone(child.wait(timeout=20))

    def test_a_retained_object_whose_process_is_gone_is_never_signalled(self):
        import psutil
        mid, v, task_id, gen = self.frozen()

        class Replaced:
            """A verified process that exited; its pid belongs to another process now (psutil: is_running() is False)."""
            pid = os.getpid()
            killed = 0

            def is_running(self):
                return False

            def kill(self):
                Replaced.killed += 1
        gone = Replaced()
        with mock.patch.object(psutil, 'wait_procs', lambda procs, timeout=None: (list(procs), [])):
            out = monster_lane.drain_tree(self.root, 'Giant Rat', v, {'job': None, 'procs': [gone], 'blocking': [], 'incomplete': None})
        self.assertEqual((Replaced.killed, out['problems']), (0, []))                            # this very test process was not killed


class FinalLiveReduceTest(FinalLiveCase):
    """"Reduction can certify the wrong model": only the GLB the export receipt names is reduced, verified before and after."""

    GLB = b'the model that was paid for'

    def exported(self, receipt=True, name='glb_m9.glb', write=True):
        mid, v, task_id, gen = self.frozen()
        vdir, mdir = mv.views_dir(self.mroot, 'Giant Rat', v), mv.models_dir(self.mroot, 'Giant Rat', v)
        frozen = mv.read_views_json(vdir)['frozen']
        hyper = mdir / 'hyper3d'
        _snap, problem = monster_lane.snapshot_inputs(hyper, vdir, frozen)
        self.assertIsNone(problem)
        if write:
            (hyper / name).write_bytes(self.GLB)
        entry = {'path': str(hyper / name), 'bytes': len(self.GLB), 'sha256': hashlib.sha256(self.GLB).hexdigest()}
        if receipt is not True:
            entry = receipt
        (hyper / 'studio_submission.json').write_text(json.dumps({
            'backend': 'hyper3d', 'status': 'exported', 'model_id': 'm9', 'model_url': 'https://h/m9', 'pending_action': None,
            'actions': {'free_preview': {}, 'geometry': {'confirmed_at': 't', 'observed_cost': 0.5}}, 'credits_committed': 0.5,
            'inputs': [{'name': 'front.png', 'sha256': frozen['sha256']['front.png'], 'size': 1}], 'images_uploaded': ['front.png'],
            **({'exports': {'GLB': entry}} if entry is not None else {})}))
        return mid, v, mdir, hyper, gen

    def not_certified(self, v, mdir, gen, needle, reduced=False):
        verdict = self.drive(v)
        self.assertEqual(verdict['status'], 'failed', verdict)
        self.assertIn(needle, verdict['error'])
        self.assertEqual('reduce_monster.py' in self.scripts(), reduced)
        self.assertFalse((mdir / 'reduced' / mv.REDUCE_OK).exists())
        self.assertFalse(mv.reduce_status(mdir, gen)['ok'])
        self.assertNotIn('collect', [c['argv'][0] for c in self.calls()])                        # and nothing was paid again
        return verdict

    def test_missing_receipted_glb_is_never_replaced_by_another_one(self):
        # The reviewer's case: the receipted GLB is gone and a different glb_*.glb lies in the folder.
        mid, v, mdir, hyper, gen = self.exported(write=False)
        (hyper / 'glb_other.glb').write_bytes(b'some other model')
        verdict = self.not_certified(v, mdir, gen, 'the receipted GLB glb_m9.glb is missing')
        self.assertIn('glb_other.glb', verdict['error'])
        self.assertEqual(self.scripts(), [])

    def test_receipted_file_with_other_bytes_is_refused_before_the_reducer_starts(self):
        mid, v, mdir, hyper, gen = self.exported()
        (hyper / 'glb_m9.glb').write_bytes(b'another model of the same name')
        self.not_certified(v, mdir, gen, 'is not the model the export receipt names before the reduction')
        self.assertEqual(self.scripts(), [])

    def test_a_record_without_a_usable_receipt_is_not_reduced(self):
        for receipt, needle in ((None, 'no export receipt'), ({'path': 'glb_m9.glb'}, 'carries no sha256'),
                                ({'path': '../glb_m9.glb', 'sha256': '0' * 64}, 'not a glb_*.glb of this variant'),
                                ({'path': 'model.glb', 'sha256': '0' * 64}, 'not a glb_*.glb of this variant')):
            with self.subTest(receipt=receipt):
                self.assertEqual(monster_lane.receipted_glb({'exports': {'GLB': receipt}} if receipt else {}, self.root)[0], None)
                self.assertIn(needle, monster_lane.receipted_glb({'exports': {'GLB': receipt}} if receipt else {}, self.root)[2])
        mid, v, mdir, hyper, gen = self.exported(receipt={'path': str(self.mroot / 'x' / 'glb_m9.glb')})
        self.not_certified(v, mdir, gen, 'carries no sha256')
        self.assertEqual(self.scripts(), [])

    def test_the_reducer_gets_the_receipted_path_and_digest_and_the_success_record_names_them(self):
        mid, v, mdir, hyper, gen = self.exported()
        (hyper / 'glb_aaa_other.glb').write_bytes(b'some other model')                           # sorts first: the old pick
        verdict = self.drive(v)
        self.assertEqual(verdict['status'], 'ok', verdict)
        sha = hashlib.sha256(self.GLB).hexdigest()
        argv = [c['argv'] for c in self.calls() if c['script'] == 'reduce_monster.py'][0]
        self.assertEqual(argv, [f'Giant Rat/{v}', '--glb', str(hyper / 'glb_m9.glb'), '--sha256', sha])
        ok = json.loads((mdir / 'reduced' / mv.REDUCE_OK).read_text())
        self.assertEqual(ok['source'], {'file': 'hyper3d/glb_m9.glb', 'sha256': sha})
        self.assertTrue(mv.reduce_status(mdir, gen)['ok'])

    def test_a_reducer_that_took_another_glb_is_not_certified(self):
        mid, v, mdir, hyper, gen = self.exported()
        (hyper / 'glb_other.glb').write_bytes(b'some other model')
        with mock.patch.dict(os.environ, {'FAKE_REDUCE': 'other_glb'}):
            self.not_certified(v, mdir, gen, "the reducer reduced 'hyper3d/glb_other.glb'", reduced=True)

    def test_a_glb_replaced_during_the_reduction_is_not_certified(self):
        mid, v, mdir, hyper, gen = self.exported()
        with mock.patch.dict(os.environ, {'FAKE_REDUCE': 'swap_during'}):
            self.not_certified(v, mdir, gen, 'is not the model the export receipt names after the reduction', reduced=True)

    def test_collect_that_leaves_no_receipt_digest_is_not_reduced(self):
        mid, v, task_id, gen = self.frozen()
        with mock.patch.dict(os.environ, {'FAKE_H3D': 'no_receipt_sha'}):
            verdict = self.drive(v)
        self.assertEqual((verdict['status'], verdict['step']), ('failed', 'collect'), verdict)
        self.assertIn('carries no sha256', verdict['error'])
        self.assertNotIn('reduce_monster.py', self.scripts())

    @unittest.skipUnless((REAL_MROOT / 'tools' / 'reduce_monster.py').is_file(), 'the monster repo is not checked out')
    def test_real_reduce_monster_flags_refuse_a_mismatch_before_blender(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('reduce_monster_under_test', REAL_MROOT / 'tools' / 'reduce_monster.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.PIPELINE = Path(self.temp.name) / 'dry_pipeline'                                    # never the real pipeline folder
        model = mod.PIPELINE / 'Rat' / 'models' / '1'
        (model / 'hyper3d').mkdir(parents=True)
        good, other = model / 'hyper3d' / 'glb_good.glb', model / 'hyper3d' / 'glb_other.glb'
        good.write_bytes(self.GLB)
        other.write_bytes(b'some other model')
        sha = hashlib.sha256(self.GLB).hexdigest()
        ran = []
        mod.find_blender = lambda explicit=None: ran.append('blender looked up') or 'blender'
        mod.reduce = lambda *a: ran.append(a)
        for argv, needle in ((['Rat/1', '--glb', str(good)], 'given together'), (['Rat/1', '--sha256', sha], 'given together'),
                             (['Rat/1', '--glb', str(other), '--sha256', sha], 'is not the expected model'),
                             (['Rat/1', '--glb', str(model / 'hyper3d' / 'glb_gone.glb'), '--sha256', sha], 'does not exist'),
                             (['Rat/1', '--glb', str(mod.PIPELINE / 'glb_out.glb'), '--sha256', sha], 'is not a glb_*.glb in'),
                             (['Rat/1', '--glb', str(good), '--sha256', 'abc'], '64 hex digits'),
                             (['Rat/1', 'Rat/2', '--glb', str(good), '--sha256', sha], 'exactly ONE')):
            with self.assertRaises(SystemExit) as caught:
                mod.main(argv)
            self.assertIn(needle, str(caught.exception))
            self.assertEqual(ran, [])                                                           # refused before Blender is looked for
        mod.main(['Rat/1', '--glb', str(good), '--sha256', sha])
        self.assertEqual(ran, ['blender looked up', ('Rat/1', 'blender', False, str(good), sha)])
        self.assertEqual(mod.checked_source(model, good, sha), (good, sha))
        good.write_bytes(b'changed while Blender ran')
        with self.assertRaises(SystemExit) as caught:
            mod.checked_source(model, good, sha, when='after the reduction')
        self.assertIn('changed during the reduction', str(caught.exception))
        ran.clear()
        mod.main(['Rat/1', '--redo'])                                                            # manual use: the old behaviour
        self.assertEqual(ran, ['blender looked up', ('Rat/1', 'blender', True, None, None)])
        self.assertEqual(mod.source_glb.__name__, 'source_glb')


class FinalLiveUnsubmittedTest(FinalLiveCase):
    """"A rejected upload strands the unpaid variant" (the v2 side): a proven pre-click failure leaves the variant unsubmitted."""

    def hyper(self, v):
        out = mv.models_dir(self.mroot, 'Giant Rat', v) / 'hyper3d'
        out.mkdir(parents=True, exist_ok=True)
        return out

    def pre_click(self, v, **fields):
        record = {'backend': 'hyper3d', 'status': 'preparing', 'started_at': 't', 'model_id': None, 'model_url': None, 'actions': {},
                  'pending_action': None, 'why': None} | fields
        (self.hyper(v) / 'studio_submission.json').write_text(json.dumps(record))
        return record

    def test_an_archived_pre_click_record_is_no_conversion_and_retry_submits_again(self):
        mid, v, task_id, gen = self.frozen()
        hyper = self.hyper(v)
        # what the backend leaves after a rejected upload: the archived record, the upload copies and a screenshot
        (hyper / 'studio_submission.not_submitted_20261126T010203.json').write_text(json.dumps(
            {'backend': 'hyper3d', 'status': 'not_submitted', 'why': 'the page shows 3 thumbnails for 4 files', 'actions': {}}))
        (hyper / 'upload_inputs').mkdir()
        (hyper / 'upload_inputs' / 'front.png').write_bytes(b'stale')
        mdir = mv.models_dir(self.mroot, 'Giant Rat', v)
        self.assertIsNone(mv.record_of(mdir))
        self.assertFalse(mv.conversion_started(mdir))
        self.fail_attempt(task_id, 'STOPPED: the page shows 3 thumbnails for 4 files; nothing was generated')
        self.assertEqual(mv.item(self.root, mid)['conversion']['state'], 'failed')
        self.assertEqual(self.qop('retry', task_id)['state'], 'queued')                          # Retry ...
        verdict = self.drive(v)
        self.assertEqual(verdict['status'], 'ok', verdict)                                       # ... submits again
        self.assertEqual([(c['script'], c['argv'][0]) for c in self.calls()][:2],
                         [('hyper3d_single.py', 'submit'), ('hyper3d_single.py', 'collect')])
        self.assertEqual(self.paid_clicks(), 1)
        self.assertTrue((hyper / 'studio_submission.not_submitted_20261126T010203.json').is_file())   # the evidence stays

    def test_a_proven_pre_click_record_left_in_place_is_archived_and_use_enabled_views_resubmits(self):
        mid, v, task_id, gen = self.frozen()
        self.pre_click(v, why='the page shows 3 thumbnails for 4 files')
        mdir, hyper = mv.models_dir(self.mroot, 'Giant Rat', v), self.hyper(v)
        self.assertTrue(mv.proven_unsubmitted(mv.record_of(mdir)))
        self.assertFalse(mv.conversion_started(mdir))
        self.fail_attempt(task_id, 'STOPPED: thumbnails')
        out = self.convert(mid, ['front.png', 'left.png'])                                       # Use enabled views: not 409 `frozen`
        self.assertNotEqual(self.ident(out['task'])['generation'], gen)
        verdict = self.drive(v)
        self.assertEqual(verdict['status'], 'ok', verdict)
        self.assertTrue(any(s.startswith('unsubmitted record set aside') for s in verdict['steps']), verdict['steps'])
        archived = list(hyper.glob('studio_submission.not_submitted_*.json'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(json.loads(archived[0].read_text())['why'], 'the v2 monster lane found a proven pre-click failure')
        self.assertEqual([(c['script'], c['argv'][0]) for c in self.calls()][:2],
                         [('hyper3d_multiview.py', 'submit'), ('hyper3d_multiview.py', 'collect')])
        self.assertEqual(self.paid_clicks(), 1)

    def test_a_record_with_any_click_intent_stops_for_review(self):
        cases = ({'actions': {'free_preview': {'intent_at': 't'}}, 'pending_action': 'free_preview'},
                 {'actions': {'free_preview': {'intent_at': 't'}}},
                 {'model_url': 'https://hyper3d.test/m1'}, {'model_id': 'm1'}, {'submit_clicked_at': 't'},
                 {'status': 'submission_attempted'}, {'credits_committed': 0.5})
        mid, v, task_id, gen = self.frozen()
        mdir = mv.models_dir(self.mroot, 'Giant Rat', v)
        self.fail_attempt(task_id, 'STOPPED: the submission needs review')
        for fields in cases:
            with self.subTest(fields=fields):
                record = self.pre_click(v, **fields)
                self.assertFalse(mv.proven_unsubmitted(record))
                self.assertTrue(mv.conversion_started(mdir))
                with self.assertRaises(mv.Frozen):                                               # Use enabled views: refused
                    self.convert(mid, ['front.png', 'left.png'])
                verdict = self.drive(v)
                self.assertEqual(verdict['status'], 'failed', verdict)
                self.assertEqual(self.calls(), [])                                               # nothing was submitted or collected
                self.assertEqual(json.loads((mdir / 'hyper3d' / 'studio_submission.json').read_text()), record)   # untouched
                self.assertEqual(list((mdir / 'hyper3d').glob('studio_submission.not_submitted_*')), [])
        self.assertEqual(self.paid_clicks(), 0)


class ConfirmInterruptedArchiveTest(FinalLiveUnsubmittedTest):
    """Confirm review ("Pilot, rejected upload"): the backend's archive was interrupted between its status write and its rename, so a
    `not_submitted` record with zero click intents is still in place. The variant is unsubmitted: Retry and Use enabled views work."""

    WHY = 'Expected 4 uploaded thumbnails, found 3'

    def interrupted(self, v):
        return self.pre_click(v, status='not_submitted', why=self.WHY)

    def archived(self, v):
        return [json.loads(p.read_text()) for p in self.hyper(v).glob('studio_submission.not_submitted_*.json')]

    def test_retry_resumes_the_pending_archive_and_submits_again(self):
        mid, v, task_id, gen = self.frozen()
        record = self.interrupted(v)
        mdir = mv.models_dir(self.mroot, 'Giant Rat', v)
        self.assertTrue(mv.proven_unsubmitted(record))
        self.assertFalse(mv.conversion_started(mdir))
        self.fail_attempt(task_id, f'STOPPED: {self.WHY}')
        self.assertEqual(self.qop('retry', task_id)['state'], 'queued')                          # Retry ...
        verdict = self.drive(v)
        self.assertEqual(verdict['status'], 'ok', verdict)                                       # ... submits again
        self.assertTrue(any(s.startswith('unsubmitted record set aside') for s in verdict['steps']), verdict['steps'])
        archived = self.archived(v)
        self.assertEqual([(a['status'], a['why'], a['actions']) for a in archived], [('not_submitted', self.WHY, {})])   # its own reason
        self.assertIn('archive_resumed', archived[0])
        self.assertEqual([(c['script'], c['argv'][0]) for c in self.calls()][:2],
                         [('hyper3d_single.py', 'submit'), ('hyper3d_single.py', 'collect')])
        self.assertEqual(self.paid_clicks(), 1)

    def test_use_enabled_views_accepts_the_variant(self):
        mid, v, task_id, gen = self.frozen()
        self.interrupted(v)
        self.fail_attempt(task_id, f'STOPPED: {self.WHY}')
        out = self.convert(mid, ['front.png', 'left.png'])                                       # not 409 `frozen`
        self.assertNotEqual(self.ident(out['task'])['generation'], gen)
        verdict = self.drive(v)
        self.assertEqual(verdict['status'], 'ok', verdict)
        self.assertEqual([a['why'] for a in self.archived(v)], [self.WHY])
        self.assertEqual([(c['script'], c['argv'][0]) for c in self.calls()][:2],
                         [('hyper3d_multiview.py', 'submit'), ('hyper3d_multiview.py', 'collect')])
        self.assertEqual(self.paid_clicks(), 1)

    def test_a_not_submitted_record_with_a_click_trace_stays_for_review(self):
        cases = ({'actions': {'free_preview': {'intent_at': 't'}}}, {'submit_clicked_at': 't'}, {'model_url': 'https://hyper3d.test/m1'},
                 {'model_id': 'm1'}, {'exports': {'GLB': {}}}, {'credits_committed': 0.5}, {'pending_action': 'free_preview'})
        mid, v, task_id, gen = self.frozen()
        mdir = mv.models_dir(self.mroot, 'Giant Rat', v)
        self.fail_attempt(task_id, 'STOPPED: Hyper3D answered "Insufficient Balance" to Free Preview')
        for fields in cases:
            with self.subTest(fields=fields):
                record = self.pre_click(v, status='not_submitted', why='refused', **fields)
                self.assertFalse(mv.proven_unsubmitted(record))
                self.assertTrue(mv.conversion_started(mdir))
                with self.assertRaises(mv.Frozen):
                    self.convert(mid, ['front.png', 'left.png'])
                verdict = self.drive(v)
                self.assertEqual(verdict['status'], 'failed', verdict)
                self.assertEqual(self.calls(), [])
                self.assertEqual(json.loads((mdir / 'hyper3d' / 'studio_submission.json').read_text()), record)   # untouched
                self.assertEqual(self.archived(v), [])
        self.assertEqual(self.paid_clicks(), 0)


class MigrateCliTest(MonsterCase):
    def run_migrate(self, *extra):
        return subprocess.run([sys.executable, '-X', 'utf8', str(HERE / 'migrate.py'), '--root', str(self.root), *extra],
                              capture_output=True, text=True, timeout=120, env=dict(os.environ))

    def test_dry_run_then_apply(self):
        out = self.run_migrate('--monsters')
        self.assertEqual(out.returncode, 0, out.stderr)
        report = json.loads((self.root / '.codex_jobs/_app/monster_migration_report.json').read_text())
        self.assertEqual((report['totals']['archive'], report['totals']['approved']), (5, 4))
        self.assertFalse(list(self.mroot.rglob(mv.STATE_FILE)))
        self.assertEqual(self.run_migrate('--apply').returncode, 2)
        out = self.run_migrate('--monsters', '--apply')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(list(self.mroot.rglob(mv.STATE_FILE))), 4)


if __name__ == '__main__':
    unittest.main()
