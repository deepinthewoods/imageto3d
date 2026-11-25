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
        data['actions']['geometry'] = {'paid': True}
        data['observed_cost_geometry'] = {'credits': 0.5, 'text': '0.5 Credits'}
        data['credits_committed'] = data.get('credits_committed', 0) + 0.5
        with open(mroot / 'paid_clicks.txt', 'a') as paid:
            paid.write('1\n')
    if mode == 'stop_collect':
        rec.write_text(json.dumps(data))
        (out / 'before_material_generate.png').write_bytes(b'png')
        print('STOPPED: material_generate: displayed cost 2 does not match 0', file=sys.stderr)
        sys.exit(3)
    (out / 'glb_m1.glb').write_bytes(b'glb')
    data['status'] = 'exported'
    data['exports'] = {'GLB': {'path': str(out / 'glb_m1.glb')}}
    rec.write_text(json.dumps(data))
    print(json.dumps({'status': 'exported'}, indent=2))
else:
    sys.exit(2)
'''

FAKE_REDUCE = r'''
import json, os, pathlib, sys
mroot = pathlib.Path(__file__).resolve().parents[1]
with open(mroot / 'calls.jsonl', 'a', encoding='utf-8') as log:
    log.write(json.dumps({'script': 'reduce_monster.py', 'argv': sys.argv[1:]}) + '\n')
monster, _, variant = sys.argv[1].partition('/')
model = mroot / 'pipeline' / monster / 'models' / variant
out = model / 'reduced'
if out.exists():
    if '--redo' not in sys.argv:
        raise SystemExit(f'{out} exists; pass --redo')
    out.rename(model / f'reduced_prev_test_{len(list(model.glob("reduced_prev_test*")))}')
if os.environ.get('FAKE_REDUCE') == 'fail':
    raise SystemExit('reduction failed (exit 1, missing levels)')
out.mkdir(parents=True)
for lv in ('0p8', '0p6', '0p4', '0p2', '0p1'):
    (out / f'lod_error_{lv}.glb').write_bytes(b'glb' + lv.encode())
(out / 'manifest.json').write_text(json.dumps({'levels': {lv: {'triangles': 100} for lv in ('0p8', '0p6', '0p4', '0p2', '0p1')}}))
if os.environ.get('FAKE_REDUCE') != 'no_materials':
    (out / 'materials.json').write_text(json.dumps({'materials': {}}))
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
        self.assertIn('no transparent pixel', views['left']['problem'])
        self.assertIn('provenance', views['right']['problem'])
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
        (mdir / 'chosen_lod.json').write_text('{not json')
        with self.assertRaises(guard.Unreadable):                                                 # an unreadable choice is never "not approved"
            self.convert(mid, ['front.png'], verb='reconvert', confirm=True)
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
        self.assertEqual((r['lod'], r['approved'], r['undo']), ('0p2', True, {'verb': 'unapprove', 'id': mid}))
        choice = json.loads((self.mroot / 'pipeline/Goblin Shaman/models/main/chosen_lod.json').read_text())
        self.assertEqual((choice['glb_sha256'], choice['legacy']), (sha, True))
        card = self.tabs()['completed'][mid]
        self.assertEqual(card['actions'], ['unapprove'])
        self.assertIn('legacy reduction', [b['text'] for b in card['badges']])
        with self.assertRaises(guard.AlreadyApproved):
            self.act(mid, 'approve', lod='0p2', glb_sha256=sha)
        r = self.act(mid, 'unapprove')
        self.assertEqual(r['undo'], {'verb': 'approve', 'id': mid, 'args': {'lod': '0p2', 'glb_sha256': sha}})
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

    def mode(self, mode=None, pause=None, since='2026-11-25T10:00:00'):
        batch = self.root / '.codex_jobs/_batch'
        if mode:
            (batch / 'supervisor_mode.json').write_text(json.dumps({'mode': mode, 'since': since}))
        if pause:
            (batch / 'PAUSE').write_text(json.dumps(pause))

    def test_gate_and_bound_pause_override(self):
        g = monster_lane.gate
        self.assertFalse(g(self.root, {}, 10, True)['open'])
        self.assertTrue(g(self.root, {}, 400, True)['open'])
        self.assertTrue(g(self.root, {'run_now': True}, 10, True)['open'])
        self.mode(pause={'reason': 'user active (views keep running)', 'scope': ['conversions']})
        self.assertFalse(g(self.root, {}, 400, True)['open'])
        self.assertTrue(g(self.root, {'run_now': True}, 10, True)['open'])
        self.mode(mode='pause-conversions', pause={'reason': 'manual pause-conversions', 'scope': ['conversions']})
        closed = g(self.root, {'run_now': True}, 400, True)
        self.assertEqual((closed['open'], closed['needs_confirm']), (False, True))
        key = closed['pause_key']
        self.assertTrue(g(self.root, {'run_now': True, 'override_pause': key}, 10, True)['open'])
        self.assertFalse(g(self.root, {'run_now': True, 'override_pause': True}, 10, True)['open'])   # an unbound override is no override
        self.mode(mode='pause-conversions', since='2026-11-25T11:00:00')                         # a NEW pause
        self.assertFalse(g(self.root, {'run_now': True, 'override_pause': key}, 10, True)['open'])
        (self.root / '.codex_jobs/_batch/PAUSE').unlink()
        self.mode(mode='run')
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
        self.assertTrue(monster_lane.run_now(self.root, task['id'])['run_now'])
        self.assertEqual([t['id'] for t in monster_lane.plan_starts(self.root, idle=10, idle_known=True)['start']], [task['id']])
        self.mode(mode='pause-conversions')
        with self.assertRaises(monster_lane.ConfirmPaused):
            monster_lane.run_now(self.root, task['id'])
        r = monster_lane.run_now(self.root, task['id'], confirm=True)
        self.assertTrue(r['override_pause'])
        self.assertEqual(len(monster_lane.plan_starts(self.root, idle=10, idle_known=True)['start']), 1)
        argv = monster_lane.task_argv(self.root, q.get(task['id']))
        self.assertEqual(argv[3:5], [str(HERE / 'monster_lane.py'), 'run'])
        self.assertIn('--run-now', argv)
        self.assertEqual(argv[argv.index('--override-pause') + 1], r['pause_key'])
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
        monster_lane.run_now(self.root, task['id'])
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
            'actions': {'free_preview': {}, 'geometry': {}}, 'observed_cost_geometry': {'credits': 0.5}, 'credits_committed': 0.5,
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
                                                              'id': 'hyper3d:m2:geometry'}])
        self.assertIsNone(monster_lane.reconcile_paid(self.root, 'Giant Rat', 'main', ['front.png'], self.root / 'r.json', record))
        summary = monster_lane.paid_summary(self.root)
        self.assertEqual((summary['count'], summary['credits'], summary['uncertain']['count']), (0, 0, 1))
        record |= {'pending_action': None, 'observed_cost_geometry': {'credits': 0.5}}
        monster_lane.reconcile_paid(self.root, 'Giant Rat', 'main', ['front.png'], self.root / 'r.json', record)
        summary = monster_lane.paid_summary(self.root)
        self.assertEqual((summary['count'], summary['credits'], summary['uncertain']['count']), (1, 0.5, 0))

    def test_log_write_error_stays_visible_until_repaired(self):
        record = {'model_id': 'm3', 'actions': {'geometry': {}}, 'pending_action': None, 'observed_cost_geometry': {'credits': 0.5}}
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
        actions.queue_op(self.root, {'op': 'cancel', 'id': task_id})
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
        actions.queue_op(self.root, {'op': 'retry', 'id': rows[0]['id']})
        self.assertIsNone(mv.disposition_of(mv.read_views_json(mv.views_dir(self.mroot, 'Giant Rat', v)), gen))
        self.assertEqual([t['id'] for t in monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start']], [rows[0]['id']])

    def test_exact_key_lookup_beyond_5000_rows(self):
        mid, v, task_id = self.frozen()
        actions.queue_op(self.root, {'op': 'cancel', 'id': task_id})
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
        r = actions.queue_op(self.root, {'op': 'run_now', 'id': task_id, 'confirm': True})
        q = taskqueue.for_root(self.root)
        self.assertEqual(q.get(task_id)['args']['override_pause'], r['pause_key'])
        self.assertEqual(len(monster_lane.plan_starts(self.root, idle=10, idle_known=True)['start']), 1)
        actions.queue_op(self.root, {'op': 'cancel', 'id': task_id})
        (batch / 'supervisor_mode.json').write_text(json.dumps({'mode': 'pause-conversions', 'since': 'B'}))   # a new manual pause
        actions.queue_op(self.root, {'op': 'retry', 'id': task_id})                                # an ordinary retry
        args = q.get(task_id)['args']
        self.assertEqual((args.get('run_now'), 'override_pause' in args), (False, False))
        self.assertEqual(monster_lane.plan_starts(self.root, idle=1000, idle_known=True)['start'], [])
        self.assertFalse(monster_lane.gate(self.root, {'run_now': True, 'override_pause': r['pause_key']}, 10, True)['open'])


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
        actions.queue_op(self.root, {'op': 'cancel', 'id': task_id})
        out = actions.queue_op(self.root, {'op': 'retry', 'id': task_id})
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
            status, r3 = call('POST', '/api/v2/queue', {'op': 'run_now', 'id': r['task']})
            self.assertEqual((status, r3['code']), (409, 'confirm'))
            status, r4 = call('POST', '/api/v2/queue', {'op': 'run_now', 'id': r['task'], 'confirm': True})
            self.assertEqual((status, r4['override_pause']), (200, True))
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
        out = actions.queue_op(self.root, {'op': 'run_now', 'id': task_id})
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
            actions.queue_op(self.root, {'op': 'run_now', 'id': task_id})
        self.assertTrue(actions.queue_op(self.root, {'op': 'run_now', 'id': task_id, 'confirm': True})['override_pause'])
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
