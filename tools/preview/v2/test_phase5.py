"""Tests of phase 5 of the v2 preview backend (DESIGN_PHASE5.md): monsters as a kind, built as new modules
(monsters_v2.py, monster_prompts.py, monster_lane.py) before their integration into catalog / actions / orchestrator / server.

Run: python -X utf8 -m unittest tools/preview/v2/test_phase5.py

Everything is offline: a temp imageto3d root and a temp monster root (`IMAGETO3D_MONSTERS`), FAKE Hyper3D wrappers and a fake
reduce_monster.py in `<temp monster root>/tools/` (they write studio_submission.json, a glb and reduced/ like the real ones and log every call),
`V2_HYPER3D_PYTHON` = this python. No browser, no Codex job, no paid step, no write to the real monster root or the real `.codex_jobs`.
The wiring tests (WiringTest, OrchestratorLaneTest, MigrateCliTest) go through the real catalog / actions / orchestrator / server / migrate.py
with the `monster_convert` lane registered in taskqueue.LANES.
"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
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
import progress  # noqa: E402
import server  # noqa: E402
import taskqueue  # noqa: E402

REAL_MROOT = (HERE.parents[2].parent / 'monsters' / 'aaa').resolve()

FAKE_H3D = r'''
import json, os, pathlib, sys
mroot = pathlib.Path(__file__).resolve().parents[1]
mode = os.environ.get('FAKE_H3D', 'ok')
with open(mroot / 'calls.jsonl', 'a', encoding='utf-8') as log:
    log.write(json.dumps({'script': pathlib.Path(__file__).name, 'argv': sys.argv[1:]}) + '\n')
cmd = sys.argv[1]
if cmd == 'submit':
    views, out = pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
    if mode == 'busy_submit':
        print('STOPPED: Hyper3D profile busy (another conversion holds the browser); nothing was generated', file=sys.stderr)
        sys.exit(3)
    rec = out / 'studio_submission.json'
    if rec.exists():
        print('STOPPED: record exists', file=sys.stderr)
        sys.exit(3)
    files = sys.argv[sys.argv.index('--view') + 1].split(',') if '--view' in sys.argv else sys.argv[sys.argv.index('--views') + 1].split(',')
    for name in files:
        assert (views / name).is_file(), name
    out.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps({'backend': 'hyper3d', 'status': 'submitted', 'model_id': 'm1', 'model_url': 'https://hyper3d.test/m1',
                               'actions': {'free_preview': {}}, 'pending_action': None, 'images_uploaded': files, 'credits_committed': 0}))
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
    if 'geometry' not in data['actions']:
        data['actions']['geometry'] = {'paid': True}
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
    out.rename(model / 'reduced_prev_test')
if os.environ.get('FAKE_REDUCE') == 'fail':
    raise SystemExit('reduction failed (exit 1, missing levels)')
out.mkdir(parents=True)
for lv in ('0p8', '0p6', '0p4', '0p2', '0p1'):
    (out / f'lod_error_{lv}.glb').write_bytes(b'glb' + lv.encode())
(out / 'manifest.json').write_text(json.dumps({'levels': {lv: {'triangles': 100} for lv in ('0p8', '0p6', '0p4', '0p2', '0p1')}}))
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


def png(name: str) -> bytes:
    return b'\x89PNG\r\n\x1a\n' + name.encode('utf-8')


class MonsterCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.root = base / 'project'
        self.mroot = base / 'monsters_aaa'
        (self.root / '.codex_jobs' / '_app').mkdir(parents=True)
        (self.root / '.codex_jobs' / '_batch').mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {'IMAGETO3D_MONSTERS': str(self.mroot), 'V2_HYPER3D_PYTHON': sys.executable})
        self.env.start()
        os.environ.pop('FAKE_H3D', None)
        os.environ.pop('FAKE_REDUCE', None)
        self.ttl = catalog.STATE_TTL
        catalog.STATE_TTL = 0
        self.idle_patch = mock.patch.object(modes, 'idle_seconds', lambda *a, **k: (10.0, 'test'))   # the user is at the PC (in-process)
        self.idle_patch.start()
        self.build_monster_root()
        self.assertNotEqual(mv.mroot_of(self.root).resolve(), REAL_MROOT)

    def tearDown(self):
        catalog.STATE_TTL = self.ttl
        self.idle_patch.stop()
        self.env.stop()
        taskqueue._QUEUES.pop(str(self.root), None)
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
        # concepts: Warhorse (-> War Horse), Hobgoblin, Goblin Shaman, Giant Rat (new monster)
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
        # pipeline: War Horse (numbered + armour ids), Hobgoblin (flat picks), Goblin Shaman (main, converted), Kobold Artificer (main)
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

    def converted(self, monster, variant, approved=None, lod='0p4'):
        red = self.mroot / 'pipeline' / monster / 'models' / variant / 'reduced'
        red.mkdir(parents=True, exist_ok=True)
        for lv in mv.LOD_LEVELS:
            (red / f'lod_error_{lv}.glb').write_bytes(b'glb')
        (red / 'manifest.json').write_text('{"levels": {}}')
        if approved is not None:
            (red.parent / 'chosen_lod.json').write_text(json.dumps({'lod': lod, 'approved': approved}))

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

    def tabs(self):
        return {tab: {c['id']: c for c in cards} for tab, cards in mv.state_cards(self.root).items()}

    def finish_views(self, monster, variant, views=('left', 'back', 'right'), state='done'):
        vdir = mv.views_dir(self.mroot, monster, variant)
        for view in views:
            name = mv.view_task_name(monster, variant, view)
            if state == 'done':
                (vdir / f'{view}.png').write_bytes(png(f'{monster}{variant}{view}'))
            guard.write_atomic(self.root / '.codex_jobs/_batch/status' / f'{name}.json',
                               json.dumps({'state': state, 'attempts': 1 if state == 'done' else 2, 'jobs': [{'job': f'{name}-a01'}]}).encode())


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

    def test_job_prompt_paths_record_scope(self):
        text = monster_prompts.job_prompt('left', 'War Horse \u2014 \u00b0', '7', Path('F:/m/pipeline/War Horse/views/7/front.png'),
                                          Path('F:/m/pipeline/War Horse/views/7/left.png'), COLOURS)
        text.encode('ascii')
        self.assertIn('`F:/m/pipeline/War Horse/views/7/left.png`', text)
        self.assertIn('`F:/m/pipeline/War Horse/views/7/left.prompts.json`', text)
        self.assertIn('2-back.prompts.json', text)
        self.assertIn('Regenerate ONCE only if the background is opaque or the creature is cut off', text)
        self.assertIn('No conversion or 3D service calls', text)


# ------------------------------------------------------------------------------------------------------------------ intake

class IntakeTest(MonsterCase):
    def test_finals_only_mapping_intent(self):
        tabs = self.tabs()
        ids = set(tabs['concepts'])
        self.assertIn('mconcept:Warhorse/codex1', ids)
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
        self.assertEqual((item['next_variant'], item['variant'], item['library_hint']), ('7', None, None))
        rat = mv.item(self.root, 'mconcept:Giant Rat/codex1')
        self.assertEqual((rat['next_variant'], rat['library_hint']), ('main', mv.LIBRARY_BADGE))
        self.assertIn('new monster', [b['text'] for b in rat['badges']])
        for bad in ('mconcept:Warhorse/codex4', 'mconcept:../x/codex1', 'mconcept:Warhorse/x1', 'mconcept:Nope/codex1'):
            errors = (ValueError, LookupError) + ((sys.modules['catalog'].NotFound,) if 'catalog' in sys.modules else ())
            with self.assertRaises(errors):
                mv.item(self.root, bad)

    def test_numbering(self):
        self.assertEqual(mv.next_variant(self.mroot, 'Giant Rat'), 'main')                      # brand-new monster
        self.assertEqual(mv.next_variant(self.mroot, 'Kobold Artificer'), '2')                  # has main
        self.assertEqual(mv.next_variant(self.mroot, 'Goblin Shaman'), '2')
        self.assertEqual(mv.next_variant(self.mroot, 'War Horse'), '7')                          # 6-scale counts
        names = json.loads((self.mroot / 'pipeline/Hobgoblin/variant_names.json').read_text())
        names['retired'] = {'9': {'was': 'codex9'}}
        (self.mroot / 'pipeline/Hobgoblin/variant_names.json').write_text(json.dumps(names))
        self.assertEqual(mv.next_variant(self.mroot, 'Hobgoblin'), '10')                         # retired numbers are never reused

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
            self.act(cid, 'restore')                                    # not in the trash
        with self.assertRaises(ValueError):
            self.act(cid, 'rebuild')                                    # not an allowed pair
        with self.assertRaises(ValueError):
            self.act('monster:War Horse/1-plate', 'reject')
        self.assertTrue(mv.handles('mconcept:x/codex1') and mv.handles('monster:x/1') and not mv.handles('set:x'))
        # no monster file moved
        self.assertTrue((self.mroot / 'concepts/Giant Rat/codex1-front.png').is_file())


# --------------------------------------------------------------------------------------------------------------------- pick

class PickTest(MonsterCase):
    def test_pick_new_monster_main(self):
        r = self.act('mconcept:Giant Rat/codex2', 'approve')
        self.assertEqual((r['monster'], r['variant'], r['from']), ('Giant Rat', 'main', 'concepts'))
        self.assertEqual(r['undo'], {'verb': 'unapprove', 'id': 'mconcept:Giant Rat/codex2', 'args': {'to': 'concepts'}})
        vdir = self.mroot / 'pipeline/Giant Rat/views/main'
        self.assertEqual((vdir / 'front.png').read_bytes(), png('Giant Rat2'))
        self.assertEqual(json.loads((vdir / 'colours.json').read_text()), COLOURS)
        self.assertEqual(json.loads((vdir / 'prompts.json').read_text())['prompt'], 'prompt 2')
        vj = json.loads((vdir / 'views.json').read_text())
        self.assertEqual((vj['schema'], vj['views'], vj['unattended'], vj['frozen']),
                         (1, [{'name': 'front', 'file': 'front.png', 'enabled': True}], False, None))
        names = json.loads((self.mroot / 'pipeline/Giant Rat/variant_names.json').read_text())
        self.assertEqual({k: v for k, v in names['variants']['main'].items() if k != 'picked'},
                         {'was': 'codex2', 'concept': 'codex2-front.png', 'by': 'v2'})
        self.assertFalse(list((self.mroot / 'pipeline/Giant Rat').glob('*-front.*')))          # no flat files
        entry = self.state('Giant Rat')['concepts']['codex2']
        self.assertEqual((entry['stage'], entry['monster'], entry['variant']), ('approved', 'Giant Rat', 'main'))
        # three view tasks, launcher monsters, no checks, 2 attempts, priority 3
        self.assertEqual(len(r['tasks']), 3)
        for view in ('left', 'back', 'right'):
            task = json.loads((self.root / '.codex_jobs/_batch/tasks' / f'mviews--Giant_Rat--main--{view}.json').read_text())
            self.assertEqual((task['launcher'], task['checks'], task['max_attempts'], task['priority']), ('monsters', [], 2, 3))
            self.assertEqual(task['outputs'], [(vdir / f'{view}.png').as_posix()])
            self.assertEqual(task['refs'], [(vdir / 'front.png').as_posix()])
            task['prompt'].encode('ascii')
        # library hint (Giant Rat is not in PIPELINE_LIBRARY)
        self.assertFalse(r['library']['mapped'])
        card = self.tabs()['approved']['monster:Giant Rat/main']
        texts = [b['text'] for b in card['badges']]
        self.assertIn(mv.LIBRARY_BADGE, texts)
        self.assertIn('views 1/4', texts)
        self.assertNotIn('mconcept:Giant Rat/codex2', self.tabs()['concepts'])
        # the second pick of the monster gets 2 (main counts as 1)
        r2 = self.act('mconcept:Giant Rat/codex1', 'approve', unattended=True)
        self.assertEqual(r2['variant'], '2')
        self.assertTrue(json.loads((self.mroot / 'pipeline/Giant Rat/views/2/views.json').read_text())['unattended'])

    def test_pick_existing_numbered_and_library_mapped(self):
        self.act('mconcept:Warhorse/codex3', 'provisional')
        r = self.act('mconcept:Warhorse/codex3', 'approve')
        self.assertEqual((r['monster'], r['variant'], r['from']), ('War Horse', '7', 'provisional'))
        self.assertTrue(r['library']['mapped'])
        card = self.tabs()['approved']['monster:War Horse/7']
        self.assertNotIn(mv.LIBRARY_BADGE, [b['text'] for b in card['badges']])

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
        # picked again: a NEW number (7 is retired)
        r = self.act(cid, 'approve')
        self.assertEqual(r['variant'], '8')
        # a started view task blocks the undo
        self.finish_views('War Horse', '8', ('left',), state='running')
        with self.assertRaises(guard.Conflict):
            self.act(cid, 'unapprove', to='concepts')
        with self.assertRaises(guard.Conflict):
            self.act('monster:War Horse/8', 'unapprove')
        self.assertTrue((self.mroot / 'pipeline/War Horse/views/8/front.png').is_file())

    def test_import_flat_pick_without_renumbering(self):
        card = self.tabs()['approved']['monster:Hobgoblin/1']
        self.assertEqual(card['actions'], ['import'])
        self.assertIn('picked, not imported', [b['text'] for b in card['badges']])
        r = self.act('monster:Hobgoblin/1', 'import', unattended=True)
        vdir = self.mroot / 'pipeline/Hobgoblin/views/1'
        self.assertEqual(sorted(r['copied']), ['back.png', 'back.prompts.json', 'colours.json', 'front.png', 'prompts.json'])
        self.assertEqual((vdir / 'front.png').read_bytes(), png('hob flat 1'))
        self.assertEqual((vdir / 'back.png').read_bytes(), png('hob flat 1 back'))
        vj = json.loads((vdir / 'views.json').read_text())
        self.assertEqual([v['name'] for v in vj['views']], ['front', 'back'])
        self.assertEqual(vj['planned'], ['left', 'right'])
        self.assertTrue(vj['unattended'])
        self.assertEqual(sorted(p.name for p in (self.root / '.codex_jobs/_batch/tasks').glob('*.json')),
                         ['mviews--Hobgoblin--1--left.json', 'mviews--Hobgoblin--1--right.json'])
        self.assertEqual(r['concept'], 'mconcept:Hobgoblin/codex1')
        self.assertEqual(self.state('Hobgoblin')['concepts']['codex1']['variant'], '1')
        self.assertTrue((self.mroot / 'pipeline/Hobgoblin/1-front.png').is_file())                  # flat files stay
        self.assertEqual(json.loads((self.mroot / 'pipeline/Hobgoblin/variant_names.json').read_text())['variants']['1']['was'], 'codex1')
        with self.assertRaises(guard.Conflict):
            self.act('monster:Hobgoblin/1', 'import')
        with self.assertRaises(guard.Conflict):                                                    # not a v2 pick: undone by hand
            self.act('mconcept:Hobgoblin/codex1', 'unapprove')


# ------------------------------------------------------------------------------------------------------------- views / convert

class ViewsTest(MonsterCase):
    def pick(self, unattended=False):
        r = self.act('mconcept:Giant Rat/codex1', 'approve', unattended=unattended)
        return f'monster:Giant Rat/{r["variant"]}', r['variant']

    def test_choose_views_freeze_and_queue(self):
        mid, v = self.pick()
        self.finish_views('Giant Rat', v, ('left', 'back'))
        self.finish_views('Giant Rat', v, ('right',), state='failed')
        card = self.tabs()['approved'][mid]
        texts = [b['text'] for b in card['badges']]
        self.assertIn('Choose views', texts)
        self.assertIn('view failed 1', texts)
        item = mv.item(self.root, mid)
        self.assertEqual(item['phase'], 'choose')
        self.assertEqual([(x['name'], x['exists'], x['enabled'], x['retry']) for x in item['views']],
                         [('front', True, True, False), ('left', True, True, False), ('back', True, True, False), ('right', False, False, True)])
        r = self.act(mid, 'views', enabled={'left': False})
        self.assertEqual(r['ticks'], {'front': True, 'left': False, 'back': True})
        self.assertEqual(r['undo'], {'verb': 'views', 'id': mid, 'args': {'enabled': {'left': True}}})
        with self.assertRaises(ValueError):
            self.act(mid, 'views', enabled={'right': True})                      # no image
        with self.assertRaises(ValueError):
            self.act(mid, 'convert', enabled={'front': False, 'left': False, 'back': False})
        r = self.act(mid, 'convert', enabled={'left': True})
        self.assertEqual(r['frozen']['files'], ['front.png', 'left.png', 'back.png'])
        vdir = self.mroot / f'pipeline/Giant Rat/views/{v}'
        self.assertEqual(r['frozen']['sha256']['left.png'], hashlib.sha256((vdir / 'left.png').read_bytes()).hexdigest())
        task = taskqueue.for_root(self.root).get(r['task'])
        self.assertEqual((task['lane'], task['state'], task['max_attempts'], task['set_name']),
                         ('monster_convert', 'queued', 1, f'monster:Giant Rat/{v}'))
        self.assertEqual(task['args']['files'], ['front.png', 'left.png', 'back.png'])
        self.assertIn('conversion queued', [b['text'] for b in self.tabs()['approved'][mid]['badges']])
        with self.assertRaises(guard.Busy):
            self.act(mid, 'convert', enabled={'front': True})                     # queued already
        # retry of the failed view
        r = self.act(mid, 'view_retry', view='right')
        self.assertEqual(r['state'], 'pending')
        status = json.loads((self.root / '.codex_jobs/_batch/status/mviews--Giant_Rat--main--right.json').read_text())
        self.assertEqual((status['state'], status['attempts'], len(status['jobs'])), ('pending', 0, 1))

    def test_unattended_auto_freeze_and_lane_reconcile(self):
        mid, v = self.pick(unattended=True)
        self.assertEqual(monster_lane.tick(self.root), {'frozen': [], 'queued': []})              # views still coming
        self.finish_views('Giant Rat', v)
        out = monster_lane.tick(self.root)
        self.assertEqual(out, {'frozen': [f'Giant Rat/{v}'], 'queued': [f'Giant Rat/{v}']})
        vj = json.loads((self.mroot / f'pipeline/Giant Rat/views/{v}/views.json').read_text())
        self.assertEqual(vj['frozen']['files'], ['front.png', 'left.png', 'back.png', 'right.png'])
        self.assertTrue(vj['frozen']['auto'])
        self.assertEqual(monster_lane.tick(self.root), {'frozen': [], 'queued': []})              # idempotent

    def test_convert_refused_after_conversion_reconvert_moves_aside(self):
        mid, v = self.pick()
        self.finish_views('Giant Rat', v)
        self.converted('Giant Rat', v)
        with self.assertRaises(guard.Conflict):
            self.act(mid, 'convert', enabled={'front': True})
        with self.assertRaises(ValueError):
            self.act(mid, 'reconvert')                                             # no confirm
        (self.mroot / f'pipeline/Giant Rat/models/{v}/chosen_lod.json').write_text(json.dumps({'lod': '0p4', 'approved': True}))
        with self.assertRaises(guard.Conflict):
            self.act(mid, 'reconvert', confirm=True)                               # approved
        (self.mroot / f'pipeline/Giant Rat/models/{v}/chosen_lod.json').write_text(json.dumps({'lod': '0p4', 'approved': False}))
        self.act(mid, 'views', enabled={'right': False})
        r = self.act(mid, 'reconvert', confirm=True)
        self.assertEqual(r['frozen']['files'], ['front.png', 'left.png', 'back.png'])
        self.assertEqual([m.split('_prev_')[0] for m in r['moved']], ['reduced'])
        mdir = self.mroot / f'pipeline/Giant Rat/models/{v}'
        self.assertFalse((mdir / 'reduced').exists())
        self.assertTrue(list(mdir.glob('reduced_prev_*')))

    def test_approve_3d_and_unapprove_completed(self):
        mid = 'monster:Goblin Shaman/main'
        self.assertEqual(self.tabs()['3d'][mid]['actions'], [])                    # no saved LOD yet
        with self.assertRaises(ValueError):
            self.act(mid, 'approve')
        r = self.act(mid, 'approve', lod='0p2')
        self.assertEqual((r['lod'], r['approved'], r['undo']), ('0p2', True, {'verb': 'unapprove', 'id': mid}))
        choice = json.loads((self.mroot / 'pipeline/Goblin Shaman/models/main/chosen_lod.json').read_text())
        self.assertEqual((choice['lod'], choice['reduced'], choice['approved']), ('0p2', 'reduced/lod_error_0p2.glb', True))
        self.assertTrue((self.mroot / 'pipeline/Goblin Shaman/models/main' / mv.LOD_LOCK).exists())
        card = self.tabs()['completed'][mid]
        self.assertEqual(card['actions'], ['unapprove'])
        self.assertNotIn(mv.LIBRARY_BADGE, [b['text'] for b in card['badges']])
        with self.assertRaises(guard.AlreadyApproved):
            self.act(mid, 'approve', lod='0p2')
        r = self.act(mid, 'unapprove')
        self.assertEqual(r['undo'], {'verb': 'approve', 'id': mid, 'args': {'lod': '0p2'}})
        self.assertIn(mid, self.tabs()['3d'])
        self.assertEqual(self.tabs()['3d'][mid]['actions'], ['approve'])


# -------------------------------------------------------------------------------------------------------------------- lane

class LaneTest(MonsterCase):
    def frozen_variant(self, views=('left', 'back'), enabled=None):
        r = self.act('mconcept:Giant Rat/codex1', 'approve')
        v = r['variant']
        self.finish_views('Giant Rat', v, views)
        out = self.act(f'monster:Giant Rat/{v}', 'convert', enabled=enabled or {n: True for n in views})
        return v, taskqueue.for_root(self.root).get(out['task'])

    def mode(self, mode=None, pause=None):
        batch = self.root / '.codex_jobs/_batch'
        if mode:
            (batch / 'supervisor_mode.json').write_text(json.dumps({'mode': mode}))
        if pause:
            (batch / 'PAUSE').write_text(json.dumps(pause))

    def test_gate(self):
        g = monster_lane.gate
        self.assertFalse(g(self.root, {}, 10, True)['open'])                       # auto: the user is at the PC
        self.assertTrue(g(self.root, {}, 400, True)['open'])                       # away
        self.assertTrue(g(self.root, {'run_now': True}, 10, True)['open'])         # Run now overrides the idle gate
        self.mode(pause={'reason': 'user active (views keep running)', 'scope': ['conversions']})
        self.assertFalse(g(self.root, {}, 400, True)['open'])
        self.assertTrue(g(self.root, {'run_now': True}, 10, True)['open'])
        self.mode(mode='pause-conversions', pause={'reason': 'manual pause-conversions', 'scope': ['conversions']})
        closed = g(self.root, {'run_now': True}, 400, True)
        self.assertEqual((closed['open'], closed['needs_confirm']), (False, True))  # Run now does not override a manual pause
        self.assertTrue(g(self.root, {'run_now': True, 'override_pause': True}, 10, True)['open'])
        (self.root / '.codex_jobs/_batch/PAUSE').unlink()
        self.mode(mode='run')
        self.assertTrue(g(self.root, {}, 0, True)['open'])
        self.mode(mode='pause')
        self.assertFalse(g(self.root, {}, 1000, True)['open'])

    def test_driver_single_view_resumable_never_pays_twice(self):
        v, task = self.frozen_variant(views=('left',), enabled={'left': False})
        self.assertEqual(task['args']['files'], ['front.png'])
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, frozen_sha=task['args']['frozen_sha'], run_now=True, say=lambda t: None)
        self.assertEqual(verdict['status'], 'ok', verdict)
        calls = self.calls()
        self.assertEqual([(c['script'], c['argv'][0]) for c in calls],
                         [('hyper3d_single.py', 'submit'), ('hyper3d_single.py', 'collect'), ('reduce_monster.py', 'Giant Rat/' + v)])
        self.assertEqual(calls[0]['argv'][-2:], ['--view', 'front.png'])
        self.assertEqual(calls[1]['argv'][-2:], ['--allow-paid', '--accept-preview'])
        entries = monster_lane.paid_entries(self.root)
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]['kind'], entries[0]['monster'], entries[0]['variant'], entries[0]['views'], entries[0]['credits']),
                         ('hyper3d', 'Giant Rat', v, ['front.png'], 0.5))
        self.assertTrue(entries[0]['record'].endswith('/hyper3d/studio_submission.json'))
        # a second run does nothing (reduced already): no submit, no collect, no second paid entry
        again = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, say=lambda t: None)
        self.assertEqual(again['status'], 'ok')
        self.assertEqual(len(self.calls()), 3)
        self.assertEqual(self.paid_clicks(), 1)
        self.assertEqual(monster_lane.paid_summary(self.root)['count'], 1)
        self.assertEqual(monster_lane.finish_outcome(self.root, task, {'exit': 0, 'last_json': again})['outcome'], 'done')

    def test_driver_multiview_order(self):
        v, task = self.frozen_variant(views=('left', 'back', 'right'), enabled={'left': True, 'back': True, 'right': False})
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, say=lambda t: None)
        self.assertEqual(verdict['status'], 'ok', verdict)
        submit = self.calls()[0]
        self.assertEqual(submit['script'], 'hyper3d_multiview.py')
        self.assertEqual(submit['argv'][-2:], ['--views', 'front.png,left.png,back.png'])
        self.assertEqual(self.calls()[1]['script'], 'hyper3d_multiview.py')

    def test_busy_requeues_and_stop_after_paying_resumes(self):
        v, task = self.frozen_variant()
        os.environ['FAKE_H3D'] = 'busy_submit'
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, say=lambda t: None)
        self.assertEqual(verdict['status'], 'busy')
        outcome = monster_lane.finish_outcome(self.root, task, {'exit': 1, 'last_json': verdict})
        self.assertEqual((outcome['outcome'], outcome['delay']), ('requeue', 300.0))
        os.environ['FAKE_H3D'] = 'stop_collect'
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, say=lambda t: None)
        self.assertEqual((verdict['status'], verdict['step'], verdict['paid']), ('failed', 'collect', True))
        self.assertIn('STOPPED: material_generate', verdict['error'])
        self.assertTrue(any(s.endswith('before_material_generate.png') for s in verdict['screenshots']))
        self.assertEqual(monster_lane.finish_outcome(self.root, task, {'exit': 1, 'last_json': verdict})['outcome'], 'failed')
        self.assertEqual(len(monster_lane.paid_entries(self.root)), 1)
        item = mv.item(self.root, f'monster:Giant Rat/{v}')
        self.assertEqual(item['conversion']['record']['status'], 'submitted')
        self.assertIn('geometry', item['conversion']['record']['actions'])
        # Retry: resumes with collect (never a second submit, never a second paid click / log entry)
        os.environ['FAKE_H3D'] = 'ok'
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, say=lambda t: None)
        self.assertEqual(verdict['status'], 'ok', verdict)
        self.assertEqual([c['argv'][0] for c in self.calls() if c['script'] != 'reduce_monster.py'], ['submit', 'submit', 'collect', 'collect'])
        self.assertEqual(self.paid_clicks(), 1)
        self.assertEqual(len(monster_lane.paid_entries(self.root)), 1)

    def test_gate_closed_waits_reduce_failure(self):
        v, task = self.frozen_variant()
        self.mode(mode='pause-conversions')
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, run_now=True, say=lambda t: None)
        self.assertEqual((verdict['status'], verdict['step']), ('waiting', 'submit'))
        self.assertEqual(monster_lane.finish_outcome(self.root, task, {'exit': 1, 'last_json': verdict})['delay'], 60.0)
        self.assertEqual(self.calls(), [])
        self.mode(mode='views-run')
        os.environ['FAKE_REDUCE'] = 'fail'
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, idle=1000, idle_known=True, say=lambda t: None)
        self.assertEqual((verdict['status'], verdict['step']), ('failed', 'reduce'), verdict)
        os.environ.pop('FAKE_REDUCE')
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, idle=1000, idle_known=True, say=lambda t: None)
        self.assertEqual(verdict['status'], 'ok', verdict)
        self.assertEqual([c['script'] for c in self.calls()].count('hyper3d_multiview.py'), 2)   # submit + collect once each

    def test_frozen_changed_and_plan_starts(self):
        v, task = self.frozen_variant()
        q = taskqueue.for_root(self.root)
        plan = monster_lane.plan_starts(self.root, idle=10, idle_known=True)
        self.assertEqual((plan['start'], plan['waiting'][task['id']][:7]), ([], 'waiting'))
        plan = monster_lane.plan_starts(self.root, idle=1000, idle_known=True)
        self.assertEqual([t['id'] for t in plan['start']], [task['id']])
        # Run now: the idle gate opens for this task only
        self.assertTrue(monster_lane.run_now(self.root, task['id'])['run_now'])
        self.assertEqual([t['id'] for t in monster_lane.plan_starts(self.root, idle=10, idle_known=True)['start']], [task['id']])
        self.mode(mode='pause-conversions')
        with self.assertRaises(monster_lane.ConfirmPaused):
            monster_lane.run_now(self.root, task['id'])
        self.assertTrue(monster_lane.run_now(self.root, task['id'], confirm=True)['override_pause'])
        self.assertEqual(len(monster_lane.plan_starts(self.root, idle=10, idle_known=True)['start']), 1)
        argv = monster_lane.task_argv(self.root, q.get(task['id']))
        self.assertEqual(argv[3:5], [str(HERE / 'monster_lane.py'), 'run'])
        self.assertIn('--run-now', argv)
        self.assertIn('--override-pause', argv)
        # the views were chosen again: the old task is no longer wanted
        self.act(f'monster:Giant Rat/{v}', 'views', enabled={'back': False})
        vdir = self.mroot / f'pipeline/Giant Rat/views/{v}'
        vj = json.loads((vdir / 'views.json').read_text())
        vj['frozen'] = mv.freeze(vdir, ['front.png'])
        mv.write_views_json(vdir, vj)
        wanted, why = monster_lane.task_wanted(self.root, q.get(task['id']))
        self.assertFalse(wanted)
        self.assertIn('chosen again', why)
        plan = monster_lane.plan_starts(self.root, idle=1000, idle_known=True)
        self.assertEqual([t['id'] for t, _ in plan['cancel']], [task['id']])
        verdict = monster_lane.run_conversion(self.root, 'Giant Rat', v, frozen_sha=task['args']['frozen_sha'], run_now=True, say=lambda t: None)
        self.assertEqual((verdict['status'], verdict['step']), ('failed', 'check'))
        self.assertEqual(self.calls(), [])

    def test_limit_one_active(self):
        v, task = self.frozen_variant()
        r = self.act('mconcept:Giant Rat/codex2', 'approve')
        self.finish_views('Giant Rat', r['variant'], ('left',))
        second = self.act(f'monster:Giant Rat/{r["variant"]}', 'convert', enabled={'front': True})['task']
        q = taskqueue.for_root(self.root)
        self.assertTrue(q.claim(task['id'], 'tok', None))                              # one attempt active
        plan = monster_lane.plan_starts(self.root, idle=1000, idle_known=True)
        self.assertEqual((plan['start'], plan['active']), ([], 1))
        self.assertIn(second, plan['waiting'])

    def test_end_to_end_through_run_task(self):
        """The orchestrator's way: run_task.py wrapper + task_argv + TASK_ENV -> result file -> finish_outcome."""
        v, task = self.frozen_variant()
        monster_lane.run_now(self.root, task['id'])
        task = taskqueue.for_root(self.root).get(task['id'])
        logs = self.root / '.codex_jobs/_app/logs'
        logs.mkdir(parents=True)
        spec = {'id': task['id'], 'attempt': 1, 'token': 'tok1', 'argv': monster_lane.task_argv(self.root, task), 'env': monster_lane.TASK_ENV,
                'log': str(logs / 'x.log'), 'err': str(logs / 'x.err.log'), 'result': str(logs / 'x.result.json'),
                'claim': str(logs / 'x.claim.json'), 'cwd': str(HERE.parents[2]), 'task': {'key': task['key'], 'lane': task['lane']}}
        (logs / 'x.task.json').write_text(json.dumps(spec))
        subprocess.run([sys.executable, str(HERE / 'run_task.py'), str(logs / 'x.task.json'), '--token', 'tok1'], check=True, timeout=120)
        result = json.loads((logs / 'x.result.json').read_text())
        self.assertEqual(result['exit'], 0, (logs / 'x.log').read_text() + (logs / 'x.err.log').read_text())
        self.assertEqual(result['last_json']['status'], 'ok')
        self.assertEqual(monster_lane.finish_outcome(self.root, task, result)['outcome'], 'done')
        self.assertEqual(monster_lane.lane_status(self.root)['paid']['count'], 1)
        self.assertIn('3d', {c['stage'] for c in self.tabs()['3d'].values() if c['id'] == f'monster:Giant Rat/{v}'})


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
        self.assertEqual(folders['Warhorse']['kept'], ['codex3'])
        hob = {a['key']: a for a in folders['Hobgoblin']['approved']}
        self.assertEqual((hob['codex1']['variant'], hob['codex1']['how']), ('1', 'variant_names'))
        self.assertEqual((hob['codex2']['variant'], hob['codex2']['how']), ('2', 'sha256'))
        self.assertEqual(folders['Hobgoblin']['archive'], ['codex3'])
        self.assertEqual(folders['Goblin Shaman']['approved'], [{'key': 'codex4', 'monster': 'Goblin Shaman', 'variant': 'main', 'how': 'override'}])
        self.assertEqual((folders['Giant Rat']['archive'], folders['Giant Rat']['pipeline']), (['codex1', 'codex2'], False))
        self.assertEqual(plan['totals']['approved'], 4)
        self.assertEqual(plan['importable'], [{'monster': 'Hobgoblin', 'variant': '1'}])
        self.assertIn('Hobgoblin 1', mv.migration_markdown(plan))
        self.assertFalse((self.mroot / 'concepts' / 'Giant Rat' / mv.STATE_FILE).exists())        # the dry run writes nothing
        result = mv.apply_migration(self.root, plan)
        self.assertEqual(result['written']['Warhorse'], 2)
        state = self.state('Warhorse')['concepts']
        self.assertEqual((state['codex1']['stage'], state['codex1']['variant'], state['codex2']['stage'], state['codex3']['stage']),
                         ('approved', '1', 'archive', 'provisional'))
        tabs = self.tabs()
        self.assertFalse([i for i in tabs['concepts'] if i.startswith('mconcept:')])
        self.assertEqual(len([i for i in tabs['archive'] if i.startswith('mconcept:')]), 4)
        again = mv.migration_plan(self.root)
        self.assertEqual((again['totals']['archive'], again['totals']['approved']), (0, 0))
        self.assertEqual(sum(mv.apply_migration(self.root, again)['written'].values()), 0)
        after = sorted(p.relative_to(self.mroot).as_posix() for p in self.mroot.rglob('*') if p.is_file())
        self.assertEqual(sorted(set(after) - set(before)), sorted(f'concepts/{f}/{mv.STATE_FILE}' for f in
                                                                  ('Warhorse', 'Hobgoblin', 'Goblin Shaman', 'Giant Rat')))
        self.assertFalse(set(before) - set(after))                                                  # nothing moved or deleted

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
        self.assertEqual(tabs['3d']['monster:Goblin Shaman/main']['type'], 'monster')            # the phase 4 behaviour stays
        self.assertEqual(tabs['approved']['monster:Kobold Artificer/main']['stage'], 'approved')
        self.assertEqual(tabs['approved']['monster:Hobgoblin/1']['actions'], ['import'])
        self.perform(cid, 'provisional')
        self.assertIn(cid, self.cat()['provisional'])
        self.perform(cid, 'archive')                                                              # Archive from provisional (the dialog)
        self.assertEqual(self.cat()['archive'][cid]['actions'], ['approve', 'unarchive'])
        self.perform(cid, 'unarchive')
        self.perform(cid, 'archive')                                                              # ... and from concepts
        self.perform(cid, 'unarchive')
        self.perform(cid, 'reject')
        self.assertEqual(self.cat()['trash'][cid]['actions'], ['restore'])
        self.perform(cid, 'restore')
        r = self.perform(cid, 'approve', unattended=False)
        tabs = self.cat()
        self.assertNotIn(cid, tabs['concepts'])
        self.assertIn(f'monster:Giant Rat/{r["variant"]}', tabs['approved'])
        self.converted('Goblin Shaman', 'main', approved=True)
        self.assertIn('monster:Goblin Shaman/main', self.cat()['completed'])
        # items through catalog.item; unknown ids are catalog.NotFound (HTTP 404), bad ones ValueError (400)
        self.assertEqual(catalog.item(self.root, 'mconcept:Warhorse/codex1')['monster'], 'War Horse')
        self.assertEqual(catalog.item(self.root, 'monster:Goblin Shaman/main')['stage'], 'completed')
        for bad in ('mconcept:Nope/codex1', 'monster:Goblin Shaman/9', 'monster:Nobody/1'):
            with self.assertRaises(catalog.NotFound):
                catalog.item(self.root, bad)
        with self.assertRaises(ValueError):
            catalog.item(self.root, 'mconcept:../x/codex1')

    def test_verbs_through_actions_allowlist(self):
        cid = 'mconcept:Warhorse/codex3'
        for item_id, verb in ((cid, 'rebuild'), (cid, 'drop_race'), ('monster:War Horse/1-plate', 'reject'),
                              ('monster:War Horse/1-plate', 'restore'), ('mconcept:Warhorse/codex3', 'complete')):
            with self.assertRaises(ValueError, msg=(item_id, verb)):
                self.perform(item_id, verb)
        with self.assertRaises(ValueError):
            actions.perform(self.root, {'id': 'set:../x', 'verb': 'approve'})                     # the set path keeps its NAME check
        r = self.perform(cid, 'approve', unattended=True)
        self.assertEqual((r['variant'], r['undo']), ('7', {'verb': 'unapprove', 'id': cid, 'args': {'to': 'concepts'}}))
        self.assertTrue(json.loads((self.mroot / 'pipeline/War Horse/views/7/views.json').read_text())['unattended'])
        undo = self.perform(cid, 'unapprove', to='concepts')
        self.assertEqual(undo['undo'], {'verb': 'approve', 'id': cid, 'args': {'unattended': True}})
        self.assertEqual(self.perform('monster:Hobgoblin/1', 'import', unattended=False)['variant'], '1')

    def test_item_ui_shape(self):
        r = self.perform('mconcept:Giant Rat/codex1', 'approve')
        mid, v = r['id'], r['variant']
        item = catalog.item(self.root, mid)
        self.assertEqual((item['phase'], item['legacy'], item['importable'], item['frozen'], item['conversion']),
                         ('generating', False, False, None, None))
        self.assertEqual([(x['name'], bool(x['url']), x['task'] and x['task']['state']) for x in item['views']],
                         [('front', True, None), ('left', False, 'queued'), ('back', False, 'queued'), ('right', False, 'queued')])
        self.assertEqual(item['views'][1]['task']['max_attempts'], 2)
        self.finish_views('Giant Rat', v, ('left', 'back'))
        self.finish_views('Giant Rat', v, ('right',), state='failed')
        item = catalog.item(self.root, mid)
        right = item['views'][3]
        self.assertEqual((right['task']['state'], right['retry']), ('failed', True))
        self.assertIn('no image written', right['task']['error'])
        self.assertEqual(item['phase'], 'choose')
        self.assertEqual(item['library_hint'], mv.LIBRARY_BADGE)
        self.perform(mid, 'convert', enabled={'front': True, 'left': True, 'back': False})
        item = catalog.item(self.root, mid)
        self.assertEqual(item['frozen']['names'], ['front', 'left'])
        self.assertFalse(item['draft_differs'])
        c = item['conversion']
        self.assertEqual((c['state'], c['step'], c['paid'], c['credits'], c['waiting_reason']), ('waiting', 'submit', False, None, 'user_present'))
        self.assertIsInstance(c['task_id'], int)
        with self.assertRaises(guard.Busy):
            self.perform(mid, 'convert', enabled={'front': True})
        # importable flat pick: the front comes from the flat file
        item = catalog.item(self.root, 'monster:Hobgoblin/1')
        self.assertEqual((item['phase'], item['importable']), ('import', True))
        self.assertTrue(item['views'][0]['url'].startswith('/mfiles/pipeline/Hobgoblin/1-front.png'))

    def test_reconvert_codes_and_sections_badge(self):
        r = self.perform('mconcept:Giant Rat/codex1', 'approve')
        mid, v = r['id'], r['variant']
        self.finish_views('Giant Rat', v)
        self.converted('Giant Rat', v)
        mdir = self.mroot / f'pipeline/Giant Rat/models/{v}'
        with self.assertRaises(mv.Frozen) as caught:
            self.perform(mid, 'convert', enabled={'front': True})
        self.assertEqual(caught.exception.code, 'frozen')
        (mdir / 'chosen_lod.json').write_text(json.dumps({'lod': '0p4', 'approved': True}))
        with self.assertRaises(guard.AlreadyApproved):
            self.perform(mid, 'reconvert', confirm=True)
        (mdir / 'chosen_lod.json').write_text(json.dumps({'lod': '0p4', 'approved': False}))
        (mdir / 'sections').mkdir()
        (mdir / 'sections' / 'materials.json').write_text('{}')
        os.utime(mdir / 'sections' / 'materials.json', (time.time() - 100, time.time() - 100))
        self.assertIn('sections out of date', [b['text'] for b in self.cat()['3d'][mid]['badges']])
        os.utime(mdir / 'sections' / 'materials.json', None)
        os.utime(mdir / 'reduced' / 'manifest.json', (time.time() - 50, time.time() - 50))
        self.assertNotIn('sections out of date', [b['text'] for b in self.cat()['3d'][mid]['badges']])
        out = self.perform(mid, 'reconvert', confirm=True, enabled={'right': False})
        self.assertEqual(out['frozen']['files'], ['front.png', 'left.png', 'back.png'])
        self.assertTrue((mdir / 'sections').is_dir())                                             # Reconvert leaves sections/ in place
        self.assertIn(mid, self.cat()['approved'])
        self.converted('Giant Rat', v)                                                            # the new reduced/ is newer than sections/
        os.utime(mdir / 'sections' / 'materials.json', (time.time() - 100, time.time() - 100))
        self.assertIn('sections out of date', [b['text'] for b in self.cat()['3d'][mid]['badges']])

    def test_queue_ops_retry_view_retry_and_state(self):
        r = self.perform('mconcept:Giant Rat/codex1', 'approve')
        mid, v = r['id'], r['variant']
        self.finish_views('Giant Rat', v, ('left',))
        self.finish_views('Giant Rat', v, ('back',), state='failed')
        name = mv.view_task_name('Giant Rat', v, 'back')
        out = actions.queue_op(self.root, {'op': 'view_retry', 'name': name})                    # the phase 4 op accepts mviews names
        self.assertEqual(out['state'], 'pending')
        task_id = self.perform(mid, 'convert', enabled={'front': True})['task']
        q = taskqueue.for_root(self.root)
        q.cancel(task_id, 'manual')
        out = actions.queue_op(self.root, {'op': 'retry', 'id': task_id})                        # task_approved -> monster_lane.task_wanted
        self.assertEqual(out['state'], 'queued')
        qs = orchestrator.queue_state(self.root, True)
        self.assertEqual((qs['monsters']['lane'], qs['monsters']['limit'], qs['monsters']['totals']), ('monster_convert', 1, {'queued': 1}))
        self.assertEqual(qs['monsters']['paid']['count'], 0)
        self.assertIn('code', qs['monsters']['gate'])
        self.assertEqual(qs['workers']['running'], 0)
        q.claim(task_id, 'tok', None)
        self.assertEqual(orchestrator.queue_state(self.root, True)['workers']['running'], 0)     # the layer pool counts its own lanes
        # the failed view task's log under the monster root (progress, phase 4 F19)
        job = f'{name}-a01'
        (self.mroot / '.codex_jobs' / job).mkdir(parents=True)
        (self.mroot / '.codex_jobs' / job / 'log.txt').write_text('monster codex log tail\n')
        guard.write_atomic(self.root / '.codex_jobs/_batch/status' / f'{name}.json',
                           json.dumps({'state': 'failed', 'attempts': 2, 'jobs': [{'job': job, 'exit': '1'}]}).encode())
        views = progress.views_section(self.root)
        self.assertIn('mviews', views['by_kind'])
        self.assertIn('monster codex log tail', next(f for f in views['failed'] if f['name'] == name)['log_tail'])

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
            self.assertIn('mconcept:Giant Rat/codex1', {c['id'] for c in state['tabs']['concepts']})
            self.assertEqual(call('GET', '/api/v2/item?id=mconcept:Nope/codex1')[0], 404)
            self.assertEqual(call('GET', '/api/v2/item?id=mconcept:Giant%20Rat/codex1')[1]['next_variant'], 'main')
            status, r = call('POST', '/api/v2/action', {'id': 'mconcept:Giant Rat/codex1', 'verb': 'approve', 'args': {'unattended': False}})
            self.assertEqual((status, r['variant']), (200, 'main'))
            mid = r['id']
            self.assertEqual(call('POST', '/api/v2/action', {'id': mid, 'verb': 'reject'})[0], 400)
            self.finish_views('Giant Rat', 'main')
            status, r = call('POST', '/api/v2/action', {'id': mid, 'verb': 'convert', 'args': {'enabled': {'front': True}}})
            self.assertEqual(status, 200, r)
            status, r2 = call('POST', '/api/v2/action', {'id': mid, 'verb': 'convert', 'args': {'enabled': {'front': True}}})
            self.assertEqual((status, r2['code']), (409, 'busy'))
            (self.root / '.codex_jobs/_batch/supervisor_mode.json').write_text(json.dumps({'mode': 'pause-conversions'}))
            status, r3 = call('POST', '/api/v2/queue', {'op': 'run_now', 'id': r['task']})
            self.assertEqual((status, r3['code']), (409, 'confirm'))
            status, r4 = call('POST', '/api/v2/queue', {'op': 'run_now', 'id': r['task'], 'confirm': True})
            self.assertEqual((status, r4['override_pause']), (200, True))
            status, queue = call('GET', '/api/v2/queue')
            self.assertEqual(queue['monsters']['totals'], {'queued': 1})
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join()


class OrchestratorLaneTest(MonsterCase):
    """The lane through the real orchestrator tick: fake Hyper3D / reduce scripts in the temp monster root, run_task.py wrappers."""

    def setUp(self):
        super().setUp()
        self.idle_value = 10.0
        self.orch = orchestrator.Orchestrator(self.root, idle=lambda: self.idle_value, scan_stale=False)
        self.orch.logs.mkdir(parents=True, exist_ok=True)

    def frozen(self, views=('left', 'back')):
        r = actions.perform(self.root, {'id': 'mconcept:Giant Rat/codex1', 'verb': 'approve', 'args': {}})
        v = r['variant']
        self.finish_views('Giant Rat', v, views)
        out = actions.perform(self.root, {'id': f'monster:Giant Rat/{v}', 'verb': 'convert', 'args': {'enabled': {n: True for n in views}}})
        return v, out['task']

    def drive(self, done, timeout=90.0):
        end = time.time() + timeout
        while time.time() < end:
            self.orch.tick()
            if done():
                return
            time.sleep(0.2)
        self.fail('timeout: ' + json.dumps([(t['id'], t['lane'], t['state'], t['error']) for t in taskqueue.for_root(self.root).tasks()]))

    def task(self, task_id):
        return taskqueue.for_root(self.root).get(task_id)

    def hold(self):
        guard.write_atomic(self.root / '.codex_jobs/_batch/CONVERT_HOLD', json.dumps({'since': 'now', 'reason': 'supervisor restart'}).encode())

    def test_tick_gate_run_now_and_finish(self):
        v, task_id = self.frozen()
        self.orch.tick()
        self.assertEqual(self.task(task_id)['state'], 'queued')                                  # auto mode, the user is at the PC
        item = catalog.item(self.root, f'monster:Giant Rat/{v}')
        self.assertEqual((item['conversion']['state'], item['conversion']['waiting_reason']), ('waiting', 'user_present'))
        # the conversion hold is never overridden, not even by Run now
        self.hold()
        out = actions.queue_op(self.root, {'op': 'run_now', 'id': task_id})
        self.assertIn('CONVERT_HOLD', out['warning'])
        self.orch.tick()
        self.assertEqual(self.task(task_id)['state'], 'queued')
        self.assertEqual(catalog.item(self.root, f'monster:Giant Rat/{v}')['conversion']['waiting_reason'], 'held')
        (self.root / '.codex_jobs/_batch/CONVERT_HOLD').unlink()
        # Run now (marked above): starts although the user is present, and runs outside the layer pool (limit 0 here)
        with mock.patch.object(orchestrator, 'pool_status', lambda *a, **k: {'limit': 0, 'present': True, 'idle': 10, 'mode': None}):
            self.drive(lambda: self.task(task_id)['state'] in ('done', 'failed'))
        task = self.task(task_id)
        self.assertEqual(task['state'], 'done', (task['error'], task.get('result')))
        self.assertEqual([c['argv'][0] for c in self.calls()], ['submit', 'collect', f'Giant Rat/{v}'])
        self.assertEqual(monster_lane.paid_summary(self.root)['count'], 1)
        catalog.invalidate()
        tabs = {tab: [c['id'] for c in cards] for tab, cards in catalog.state(self.root)['tabs'].items()}
        self.assertIn(f'monster:Giant Rat/{v}', tabs['3d'])
        self.assertEqual(catalog.item(self.root, f'monster:Giant Rat/{v}')['conversion']['state'], 'done')
        self.orch.tick()
        self.assertEqual(len([t for t in taskqueue.for_root(self.root).tasks() if t['lane'] == monster_lane.LANE]), 1)   # never queued twice

    def test_paused_needs_confirm_then_overrides(self):
        v, task_id = self.frozen()
        (self.root / '.codex_jobs/_batch/supervisor_mode.json').write_text(json.dumps({'mode': 'pause-conversions'}))
        self.idle_value = 1000.0
        self.orch.tick()
        self.assertEqual(self.task(task_id)['state'], 'queued')                                  # paused: away is not enough
        self.assertEqual(catalog.item(self.root, f'monster:Giant Rat/{v}')['conversion']['waiting_reason'], 'paused')
        with self.assertRaises(monster_lane.ConfirmPaused) as caught:
            actions.queue_op(self.root, {'op': 'run_now', 'id': task_id})
        self.assertEqual(caught.exception.code, 'confirm')
        self.assertTrue(actions.queue_op(self.root, {'op': 'run_now', 'id': task_id, 'confirm': True})['override_pause'])
        self.drive(lambda: self.task(task_id)['state'] in ('done', 'failed'))
        self.assertEqual(self.task(task_id)['state'], 'done')

    def test_pause_file_then_run_mode(self):
        v, task_id = self.frozen()
        (self.root / '.codex_jobs/_batch/supervisor_mode.json').write_text(json.dumps({'mode': 'run'}))
        (self.root / '.codex_jobs/_batch/PAUSE').write_text(json.dumps({'reason': 'manual', 'scope': ['conversions']}))
        self.idle_value = 1000.0
        self.orch.tick()
        self.assertEqual(self.task(task_id)['state'], 'queued')                                  # a manual PAUSE holds even in mode run
        (self.root / '.codex_jobs/_batch/PAUSE').unlink()
        # mode run, no pause: starts by itself (no Run now). (The idle gate of a real run is read by the driver from presence.json, so
        # the away case is covered by plan_starts / gate above, not by a spawned driver.)
        self.drive(lambda: self.task(task_id)['state'] in ('done', 'failed'))
        self.assertEqual(self.task(task_id)['state'], 'done')

    def test_reconcile_never_cancels_and_plan_cancels_unwanted(self):
        v, task_id = self.frozen()
        out = self.orch.reconcile()
        self.assertEqual(out['cancelled'], 0)
        self.assertEqual(self.task(task_id)['state'], 'queued')
        self.assertTrue(self.orch._approved(self.task(task_id)))
        # the views are chosen again: the lane's plan cancels the old task at the next tick; the lane tick queues the new freeze
        vdir = self.mroot / f'pipeline/Giant Rat/views/{v}'
        vj = json.loads((vdir / 'views.json').read_text())
        vj['frozen'] = mv.freeze(vdir, ['front.png'])
        mv.write_views_json(vdir, vj)
        self.orch.tick()
        old = self.task(task_id)
        self.assertEqual(old['state'], 'cancelled')
        cancels = [e['detail'] for e in taskqueue.for_root(self.root).events() if e['task_id'] == task_id and e['kind'] == 'cancel']
        self.assertIn('chosen again', cancels[-1])
        tasks = [t for t in taskqueue.for_root(self.root).tasks() if t['lane'] == monster_lane.LANE]
        self.assertEqual(sorted(t['state'] for t in tasks), ['cancelled', 'queued'])

    def test_unattended_auto_freeze_through_tick(self):
        r = actions.perform(self.root, {'id': 'mconcept:Giant Rat/codex2', 'verb': 'approve', 'args': {'unattended': True}})
        self.orch.tick()
        self.assertFalse([t for t in taskqueue.for_root(self.root).tasks() if t['lane'] == monster_lane.LANE])
        self.finish_views('Giant Rat', r['variant'])
        self.orch.tick()
        tasks = [t for t in taskqueue.for_root(self.root).tasks() if t['lane'] == monster_lane.LANE]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]['args']['files'], ['front.png', 'left.png', 'back.png', 'right.png'])

    def test_busy_is_requeued_not_failed(self):
        v, task_id = self.frozen()
        q = taskqueue.for_root(self.root)
        self.assertTrue(q.claim(task_id, 'tok', None))
        self.orch._finish(self.task(task_id), {'exit': 1, 'last_json': {'status': 'busy', 'error': 'STOPPED: Hyper3D profile busy'}})
        task = self.task(task_id)
        self.assertEqual(task['state'], 'queued')
        self.assertGreater(task['not_before'], time.time() + 200)
        conv = catalog.item(self.root, f'monster:Giant Rat/{v}')['conversion']
        self.assertEqual((conv['state'], conv['waiting_reason']), ('waiting', 'browser_busy'))
        self.assertTrue(q.claim(task_id, 'tok2', None))
        self.orch._finish(self.task(task_id), {'exit': 1, 'last_json': {'status': 'failed', 'step': 'collect', 'error': 'STOPPED: cost'}})
        task = self.task(task_id)
        self.assertEqual((task['state'], task['error']), ('failed', 'STOPPED: cost'))             # a paid step is never retried by itself
        conv = catalog.item(self.root, f'monster:Giant Rat/{v}')['conversion']
        self.assertEqual((conv['state'], conv['step'], conv['error']), ('failed', 'collect', 'STOPPED: cost'))


class MigrateCliTest(MonsterCase):
    def run_migrate(self, *extra):
        env = dict(os.environ)
        return subprocess.run([sys.executable, '-X', 'utf8', str(HERE / 'migrate.py'), '--root', str(self.root), *extra],
                              capture_output=True, text=True, timeout=120, env=env)

    def test_dry_run_then_apply(self):
        out = self.run_migrate('--monsters')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(str(self.mroot), out.stdout)
        report = json.loads((self.root / '.codex_jobs/_app/monster_migration_report.json').read_text())
        self.assertEqual((report['totals']['archive'], report['totals']['approved']), (5, 4))
        self.assertFalse(list(self.mroot.rglob(mv.STATE_FILE)))                                   # the dry run writes nothing there
        self.assertEqual(self.run_migrate('--apply').returncode, 2)                               # --apply needs --monsters
        out = self.run_migrate('--monsters', '--apply')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(list(self.mroot.rglob(mv.STATE_FILE))), 4)
        catalog.invalidate()
        tabs = catalog.state(self.root)['tabs']
        self.assertFalse([c for c in tabs['concepts'] if c['id'].startswith('mconcept:')])
        self.assertEqual(len([c for c in tabs['archive'] if c['id'].startswith('mconcept:')]), 5)
        again = self.run_migrate('--monsters', '--apply')
        self.assertEqual(json.loads(again.stdout.splitlines()[1])['archive'], 0)


if __name__ == '__main__':
    unittest.main()
