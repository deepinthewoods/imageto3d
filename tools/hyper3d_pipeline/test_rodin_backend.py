"""Offline tests for rodin_backend.py and the convert_job.py backend dispatch (browser is faked)."""
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
from unittest import mock
import zlib
import zipfile

import rodin_backend as rb

TRIPO = Path(__file__).resolve().parents[1] / 'tripo_pipeline'
sys.path.insert(0, str(TRIPO))
import convert_job  # noqa: E402

CONFIG = rb.load_config()
UNSET = {**CONFIG, 'expected_costs': {'geometry': None, 'material_generate': None, 'material_confirm': None}}
MODEL = 'e1690fdd-98aa-4581-a015-2a2a58a787e4'
URL = f'{rb.BASE}/{MODEL}'


def png(path, w=8, h=6, depth=8, color=6):
    def chunk(tag, data):
        body = tag + data
        return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body))
    row = b'\0' + bytes(w * 4)
    Path(path).write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, depth, color, 0, 0, 0))
                           + chunk(b'IDAT', zlib.compress(row * h)) + chunk(b'IEND', b''))


def make_views(folder, **bad):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name in CONFIG['view_order']:
        png(folder / name, **bad.get(name, {}))
    return folder


def make_glb(path, meshes=1, uv=True, texture=True, triangles=12):
    gltf = {'asset': {'version': '2.0'}, 'nodes': [{'mesh': i} for i in range(meshes)],
            'meshes': [{'primitives': [{'attributes': {'POSITION': 0, **({'TEXCOORD_0': 1} if uv else {})},
                                        'indices': 2, 'material': 0}]} for _ in range(meshes)],
            'accessors': [{'count': 3}, {'count': 3}, {'count': triangles * 3}],
            'materials': [{'pbrMetallicRoughness': {'baseColorTexture': {'index': 0}} if texture else {}}],
            'images': [{'uri': 'x.png'}] if texture else []}
    blob = json.dumps(gltf).encode()
    blob += b' ' * (-len(blob) % 4)
    Path(path).write_bytes(b'glTF' + struct.pack('<II', 2, 12 + 8 + len(blob)) + struct.pack('<II', len(blob), 0x4E4F534A) + blob)


class FakeSession:
    """Stands in for SeleniumRodin; `stages` is the sequence wait_stage reports."""
    log = []

    def __init__(self, out, stages=(), costs=None, intent_check=None, glb=None, balance=100.0, url=URL, refused=False,
                 offline=False):
        self.out, self.stages, self.costs = Path(out), list(stages), costs or {}
        self.glb, self.intent_check = glb, intent_check
        self.balance, self.url, self.refused, self.offline = balance, url, refused, offline
        FakeSession.log = self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def screenshot(self, label):
        self.calls.append(('screenshot', label))

    def open_workspace(self):
        self.calls.append(('open_workspace',))
        if self.offline:
            raise RuntimeError('Reached error page: about:neterror?e=dnsNotFound\nStacktrace: ...')

    def prepare_multiview(self, paths, config):
        self.calls.append(('prepare_multiview', [n for n, _ in paths]))

    def click_free_preview(self):
        if self.intent_check:
            self.intent_check()
        self.calls.append(('click_free_preview',))

    def wait_model_url(self, timeout):
        return self.url

    def wait_balance(self, timeout):
        return self.balance

    def insufficient_balance(self):
        return self.refused

    def open_model(self, url):
        self.calls.append(('open_model', url))

    def observe_stage(self):
        return 'geometry_preview'

    def wait_stage(self, stages, timeout):
        stage = self.stages.pop(0)
        assert stage in stages, (stage, stages)
        return stage

    def select_mesh_mode(self, config):
        self.calls.append(('select_mesh_mode', config['mesh_mode_option']))
        return 'popover text'

    def read_cost(self, kind):
        return self.costs.get(kind), f'{kind} {self.costs.get(kind)} Credits'

    def read_balance(self):
        return 100.0

    def symmetric_value(self):
        return 'Symmetric'

    def material_effort(self):
        return {'head': 'Material | Native | High', 'eight_k_on': False}

    def click(self, kind):
        if self.intent_check:
            self.intent_check(kind)
        self.calls.append(('click', kind))

    def set_delight(self):
        self.calls.append(('set_delight',))

    def export_glb(self, out_dir, config):
        self.calls.append(('export_glb',))
        target = Path(out_dir) / 'glb_model.glb'
        make_glb(target)
        return target, 'panel .glb 2K'


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_valid_four_views(self):
        plan = rb.validate_views(make_views(self.root / 'v'), CONFIG)
        self.assertEqual(plan['uploaded'], CONFIG['view_order'])
        self.assertEqual(plan['input_resolution'], [8, 6])

    def test_missing_view(self):
        folder = make_views(self.root / 'v')
        (folder / 'back.png').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing back.png'):
            rb.validate_views(folder, CONFIG)

    def test_dimension_mismatch(self):
        folder = make_views(self.root / 'v', **{'back.png': {'w': 9}})
        with self.assertRaisesRegex(ValueError, 'different native dimensions'):
            rb.validate_views(folder, CONFIG)

    def test_not_rgba8(self):
        for bad in ({'depth': 16}, {'color': 2}):
            folder = make_views(self.root / 'v', **{'front.png': bad})
            with self.assertRaisesRegex(ValueError, '8-bit RGBA'):
                rb.validate_views(folder, CONFIG)

    def test_not_png(self):
        folder = make_views(self.root / 'v')
        (folder / 'front.png').write_bytes(b'nope' * 20)
        with self.assertRaisesRegex(ValueError, 'Expected PNG'):
            rb.validate_views(folder, CONFIG)

    def test_single_view_fallback_records_front_only(self):
        plan = rb.validate_views(make_views(self.root / 'v'), {**CONFIG, 'multi_view': False})
        self.assertEqual(plan['uploaded'], ['front.png'])

    def three_views(self, omitted='back', **marker):
        folder = make_views(self.root / 'v')
        (folder / (omitted + '.png')).unlink(missing_ok=True)
        (folder / 'views_omitted.json').write_text(json.dumps({'omitted': [omitted], 'reason': 'test', **marker}))
        return folder

    def test_three_view_module_skips_the_omitted_slot(self):
        for omitted in ('back', 'front_three_quarter', 'back_three_quarter'):
            plan = rb.validate_views(self.three_views(omitted), CONFIG)
            self.assertEqual(plan['uploaded'], [n for n in CONFIG['view_order'] if n != omitted + '.png'])
            self.assertEqual(plan['images'], plan['uploaded'])
            self.assertEqual(plan['views_omitted'], [omitted])
            shutil.rmtree(self.root / 'v')

    def test_three_view_marker_is_validated(self):
        folder = make_views(self.root / 'v')                       # all four exist but the marker omits back
        (folder / 'views_omitted.json').write_text(json.dumps({'omitted': ['back']}))
        with self.assertRaisesRegex(ValueError, 'exists'):
            rb.validate_views(folder, CONFIG)
        (folder / 'views_omitted.json').write_text(json.dumps({'omitted': ['front']}))
        with self.assertRaisesRegex(ValueError, 'front is always required'):
            rb.validate_views(folder, CONFIG)
        (folder / 'views_omitted.json').write_text(json.dumps({'omitted': ['back', 'front_three_quarter']}))
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            rb.validate_views(folder, CONFIG)

    def test_view_missing_without_marker_still_fails(self):
        folder = make_views(self.root / 'v')
        (folder / 'back.png').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing back.png'):
            rb.validate_views(folder, CONFIG)


class CostTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(rb.parse_cost('Confirm\n2 Credits'), 2)
        self.assertEqual(rb.parse_cost('1 Credit'), 1)
        self.assertIsNone(rb.parse_cost('Generate'))

    def test_guard(self):
        rb.check_cost('geometry', 2, 2)
        rb.check_cost('material_generate', None, 0)
        for observed, expected in ((3, 2), (None, 2), (2, None), (None, None)):
            with self.assertRaises(rb.Stop):
                rb.check_cost('geometry', observed, expected)


class PageStateTests(unittest.TestCase):
    def test_auth(self):
        self.assertEqual(rb.classify_auth({'login_link': True, 'logout': False}), 'signed_out')
        self.assertEqual(rb.classify_auth({'no_current_user': True, 'logout': True}), 'signed_out')
        self.assertEqual(rb.classify_auth({'one_tap': True, 'logout': True}), 'signed_out')
        self.assertEqual(rb.classify_auth({'logout': True}), 'signed_in')
        self.assertEqual(rb.classify_auth({}), 'unknown')

    def test_a_page_that_never_settles_is_reloaded_before_stopping(self):
        class Driver:
            current_url = 'https://hyper3d.ai/workspace/rodin/x'

            def __init__(self, states):
                self.states = list(states)

            def execute_script(self, js):
                return self.states.pop(0) if self.states else {}

        class Wf:
            def __init__(self):
                self.loads = []

            def navigate(self, driver, url):
                self.loads.append(url)

        session = rb.SeleniumRodin(CONFIG, tempfile.mkdtemp())
        session.wf, session.screenshot = Wf(), lambda label: None
        session.driver = Driver([{}, {'logout': True}])   # the test clock allows one poll per attempt
        with mock.patch('time.sleep'), mock.patch('time.monotonic', side_effect=range(0, 10 ** 6, 20)):
            session._require_signed_in(timeout=30)
        self.assertEqual(session.wf.loads, ['https://hyper3d.ai/workspace/rodin/x'])
        session.driver, session.wf = Driver([]), Wf()
        with mock.patch('time.sleep'), mock.patch('time.monotonic', side_effect=range(0, 10 ** 6, 20)):
            with self.assertRaises(rb.Stop):
                session._require_signed_in(timeout=30)
        self.assertEqual(len(session.wf.loads), 2)

    def test_a_stale_part_file_from_an_interrupted_download_does_not_block_the_next(self):
        class Element:
            def __init__(self, text='', on_click=None):
                self.text, self.on_click = text, on_click

            def is_displayed(self):
                return True

            def get_attribute(self, name):
                return 'true'

            def click(self):
                if self.on_click:
                    self.on_click()

        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out)
        session = rb.SeleniumRodin({**CONFIG, 'download_timeout_s': 1}, out)
        session.downloads.mkdir()
        (session.downloads / 'base_basic_pbr.glb').touch()          # Firefox's placeholder, left by the crash
        (session.downloads / 'base_basic_pbr.C6g_2szt.glb.part').write_bytes(b'x' * 100)
        pack = mock.Mock(text='Pack ... Download')
        pack.find_elements.return_value = [Element(CONFIG['export_format_label']), Element(CONFIG['texture_resolution'])]
        session.wf = mock.Mock(scope=mock.Mock(return_value=pack), exact=mock.Mock(return_value=Element(
            on_click=lambda: make_glb(session.downloads / 'base_basic_pbr(1).glb'))))
        session._set_checkbox = lambda pack, label, wanted: None
        with mock.patch('time.sleep'):
            path, _ = session.export_glb(out, session.config)
        self.assertEqual(path.name, 'glb_base_basic_pbr(1).glb')

    def test_balance(self):
        self.assertEqual(rb.parse_balance('Add On\nAPI\nMCP&CLI\n1310.5\nQuinlan'), 1310.5)
        self.assertEqual(rb.parse_balance('MCP&CLI\n0\n'), 0.0)
        self.assertIsNone(rb.parse_balance('MCP&CLI\nLogin'))

    def test_poly_label(self):
        self.assertEqual(rb.parse_poly_label('Triangle Detailed sharp triangular mesh 1M'), '1M')
        self.assertEqual(rb.parse_poly_label('Triangle 500 k'), '500K')
        self.assertIsNone(rb.parse_poly_label('Triangle mesh'))

    def test_derive_stage(self):
        self.assertEqual(rb.derive_stage('unknown', 'Pack ... Download', True, False), 'export_ready')
        self.assertEqual(rb.derive_stage('unknown', 'Unlock after Material confirmed', True, False), 'unknown')
        self.assertEqual(rb.derive_stage('unknown', 'Pack', True, True), 'unknown')
        self.assertEqual(rb.derive_stage('material_preview', 'Pack', True, False), 'material_preview')

    def test_shipped_config_is_the_half_credit_path(self):
        self.assertEqual((CONFIG['effort'], CONFIG['mesh_mode_option'], CONFIG['mesh_simplify']), ('Medium', 'Triangle', False))
        self.assertEqual((CONFIG['expected_costs']['geometry'], CONFIG['budget_credits']), (0.5, 0.5))
        self.assertEqual(CONFIG['export_checkboxes'], {'Base Model': True, 'LOD': False, 'Shaded': False, 'PBR': True})


class GlbTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_good_and_bad(self):
        make_glb(self.root / 'a.glb')
        report = rb.inspect_glb(self.root / 'a.glb')
        self.assertEqual((report['triangles'], report['problems']), (12, []))
        make_glb(self.root / 'b.glb', meshes=2, uv=False, texture=False)
        self.assertEqual(len(rb.inspect_glb(self.root / 'b.glb')['problems']), 3)

    def test_zip_extraction(self):
        make_glb(self.root / 'inner.glb')
        archive = self.root / 'dl.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.write(self.root / 'inner.glb', 'model/inner.glb')
        out = self.root / 'out'
        out.mkdir()
        target = rb.extract_glb(archive, out)
        self.assertEqual(target.name, 'glb_inner.glb')
        with self.assertRaises(FileExistsError):
            rb.extract_glb(archive, out)

    def test_zip_with_shaded_and_pbr_prefers_pbr(self):
        make_glb(self.root / 'a.glb')
        archive = self.root / 'two.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.write(self.root / 'a.glb', 'base_basic_shaded.glb')
            z.write(self.root / 'a.glb', 'base_basic_pbr.glb')
        out = self.root / 'o2'
        out.mkdir()
        self.assertEqual(rb.extract_glb(archive, out).name, 'glb_base_basic_pbr.glb')

    @unittest.skipUnless(Path(__file__).resolve().parents[2].joinpath('hyper3d/human male highpoly/base_basic_pbr.glb').is_file(),
                         'sample export not present')
    def test_real_sample_export(self):
        sample = Path(__file__).resolve().parents[2] / 'hyper3d/human male highpoly/base_basic_pbr.glb'
        report = rb.inspect_glb(sample)
        self.assertEqual(report['problems'], [], report)


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.views = make_views(self.root / 'views')
        self.out = self.root / 'out'

    def do_submit(self, **kw):
        return rb.submit(self.views, self.out, config=CONFIG,
                         session_factory=lambda: FakeSession(self.out, **kw), **{k: v for k, v in kw.items() if k == 'dry_run'})

    def record(self):
        return json.loads((self.out / rb.RECORD).read_text())

    def test_submit_schema_and_intent_before_click(self):
        def check(*_):
            data = self.record()
            self.assertIn('free_preview', data['actions'])
            self.assertEqual(data['pending_action'], 'free_preview')
        rb.submit(self.views, self.out, config=CONFIG,
                  session_factory=lambda: FakeSession(self.out, intent_check=check))
        data = self.record()
        self.assertEqual((data['backend'], data['status'], data['project_id'], data['model_url']),
                         ('hyper3d', 'submitted', MODEL, URL))
        self.assertEqual(data['images_uploaded'], CONFIG['view_order'])
        self.assertEqual(data['input_resolution'], [8, 6])
        self.assertEqual((data['settings']['mesh_mode'], data['settings']['smart_low_poly'], data['settings']['mesh_simplify']), ('Triangle', False, False))
        self.assertTrue((self.out / 'events.jsonl').exists())

    def test_submit_waits_for_the_wallet_and_sets_the_record_aside_when_it_shows_zero(self):
        session = FakeSession(self.out, balance=0.0)
        with self.assertRaises(rb.Stop):
            rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: session)
        self.assertNotIn(('click_free_preview',), session.calls)
        self.assertFalse((self.out / rb.RECORD).exists())          # a re-run submits again
        aside = json.loads(next(self.out.glob('studio_submission.not_submitted_*.json')).read_text())
        self.assertEqual((aside['status'], aside['actions']), ('not_submitted', {}))
        rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out))
        self.assertEqual(self.record()['status'], 'submitted')

    def test_an_error_before_the_click_sets_the_record_aside(self):
        with self.assertRaises(rb.Stop):
            rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out, offline=True))
        self.assertFalse((self.out / rb.RECORD).exists())          # not a 'preparing' record collect refuses
        aside = json.loads(next(self.out.glob('studio_submission.not_submitted_*.json')).read_text())
        self.assertEqual((aside['status'], aside['actions']), ('not_submitted', {}))
        self.assertIn('dnsNotFound', aside['why'])
        rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out))
        self.assertEqual(self.record()['status'], 'submitted')

    def test_insufficient_balance_after_the_click_is_not_left_for_review(self):
        session = FakeSession(self.out, url=None, refused=True)
        with self.assertRaises(rb.Stop):
            rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: session)
        self.assertIn(('click_free_preview',), session.calls)
        self.assertFalse((self.out / rb.RECORD).exists())
        aside = json.loads(next(self.out.glob('studio_submission.not_submitted_*.json')).read_text())
        self.assertEqual((aside['status'], aside['pending_action']), ('not_submitted', None))

    def test_no_model_url_without_a_refusal_still_needs_review(self):
        rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out, url=None))
        self.assertEqual(self.record()['status'], 'submission_needs_review')

    def test_three_view_submit_uploads_only_present_views_and_records_omission(self):
        folder = make_views(self.root / 'three')
        (folder / 'back.png').unlink()
        (folder / 'views_omitted.json').write_text(json.dumps({'omitted': ['back'], 'reason': 'refused'}))
        session = FakeSession(self.out)
        rb.submit(folder, self.out, config=CONFIG, session_factory=lambda: session)
        present = [n for n in CONFIG['view_order'] if n != 'back.png']
        self.assertIn(('prepare_multiview', present), session.calls)
        data = self.record()
        self.assertEqual((data['images_uploaded'], data['views_omitted']), (present, ['back']))
        self.assertEqual(sorted(data['settings']['directions']), sorted(present))

    def test_four_view_record_has_no_views_omitted(self):
        self.do_submit()
        self.assertNotIn('views_omitted', self.record())

    def test_never_resubmits(self):
        self.do_submit()
        with self.assertRaisesRegex(rb.Stop, 'already exists'):
            self.do_submit()

    def test_dry_run_never_clicks(self):
        sessions = []
        rb.submit(self.views, self.out, config=CONFIG, dry_run=True,
                  session_factory=lambda: sessions.append(FakeSession(self.out)) or sessions[-1])
        self.assertEqual(self.record()['status'], 'dry_run')
        self.assertNotIn(('click_free_preview',), sessions[0].calls)

    def test_bad_views_write_no_record(self):
        make_views(self.root / 'bad', **{'back.png': {'w': 9}})
        with self.assertRaises(ValueError):
            rb.submit(self.root / 'bad', self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out))
        self.assertFalse((self.out / rb.RECORD).exists())

    def collect(self, stages, costs=None, expected=None, config=None, **kw):
        session = FakeSession(self.out, stages=stages, costs=costs)
        rb.collect(self.out, config=config or UNSET, expected_costs=expected, session_factory=lambda: session, **kw)
        return session

    def test_collect_requires_permissions_and_known_cost(self):
        self.do_submit()
        with self.assertRaisesRegex(rb.Stop, '--allow-paid'):
            self.collect(['geometry_preview'])
        with self.assertRaisesRegex(rb.Stop, '--accept-preview'):
            self.collect(['geometry_preview'], allow_paid=True)
        with self.assertRaisesRegex(rb.Stop, 'not configured'):
            self.collect(['geometry_preview'], costs={'geometry': 2}, allow_paid=True, accept_preview=True)
        with self.assertRaisesRegex(rb.Stop, 'displayed cost 3'):
            self.collect(['geometry_preview'], costs={'geometry': 3}, expected={'geometry': 2},
                         allow_paid=True, accept_preview=True)
        with self.assertRaisesRegex(rb.Stop, 'displayed cost 2'):       # smart low-poly price vs the 0.5 default
            self.collect(['geometry_preview'], costs={'geometry': 2}, config=CONFIG, allow_paid=True, accept_preview=True)
        with self.assertRaisesRegex(rb.Stop, 'exceed budget_credits'):
            self.collect(['geometry_preview'], costs={'geometry': 1}, expected={'geometry': 1},
                         config={**UNSET, 'budget_credits': 0.5}, allow_paid=True, accept_preview=True)
        data = self.record()
        self.assertEqual((data['status'], list(data['actions'])), ('submitted', ['free_preview']))   # no paid intent written
        self.assertIn('observed_cost_geometry', data)

    def test_full_collect_resume_and_export_record(self):
        self.do_submit()
        session = self.collect(['geometry_preview', 'material_ready', 'material_preview', 'export_ready'],
                               costs={'geometry': 0.5, 'material_generate': None, 'material_confirm': None},
                               config=CONFIG, allow_paid=True, accept_preview=True)   # shipped expected costs
        self.assertEqual(self.record()['credits_committed'], 0.5)
        self.assertEqual(len(self.record()['balances']), 4)
        data = self.record()
        self.assertEqual(data['status'], 'exported')
        self.assertTrue(Path(data['exports']['GLB']['path']).is_file())
        self.assertEqual(data['exports']['GLB']['inspection']['problems'], [])
        self.assertEqual(set(data['actions']), {'free_preview', 'geometry', 'material_generate', 'material_confirm'})
        self.assertIn(('select_mesh_mode', 'Triangle'), session.calls)
        self.assertIn(('set_delight',), session.calls)
        clicks = [c[1] for c in session.calls if c[0] == 'click']
        self.assertEqual(clicks, ['geometry', 'material_generate', 'material_confirm'])
        # a second collect only re-verifies the export and never opens the browser
        again = rb.collect(self.out, config=CONFIG, session_factory=lambda: self.fail('browser opened'))
        self.assertEqual(again['status'], 'exported')

    def test_crash_after_intent_never_repeats_click(self):
        self.do_submit()
        costs, expected = {'geometry': 0.5}, {'geometry': 0.5}
        session = FakeSession(self.out, stages=['geometry_preview'], costs=costs)
        session.click = lambda kind: (_ for _ in ()).throw(KeyboardInterrupt())   # dies right after the intent
        with self.assertRaises(KeyboardInterrupt):
            rb.collect(self.out, config=CONFIG, expected_costs=expected, allow_paid=True, accept_preview=True,
                       session_factory=lambda: session)
        with self.assertRaisesRegex(rb.Stop, 'already submitted'):
            self.collect(['geometry_preview'], costs=costs, expected=expected, allow_paid=True, accept_preview=True)

    def test_bad_export_blocks_status(self):
        self.do_submit()

        class BadExport(FakeSession):
            def export_glb(self, out_dir, config):
                target = Path(out_dir) / 'glb_bad.glb'
                make_glb(target, meshes=2)
                return target, ''
        with self.assertRaisesRegex(rb.Stop, 'one mesh object'):
            rb.collect(self.out, config=CONFIG, session_factory=lambda: BadExport(self.out, stages=['export_ready']))
        self.assertEqual(self.record()['status'], 'export_needs_review')

    def test_collect_rejects_tripo_record(self):
        self.out.mkdir()
        (self.out / rb.RECORD).write_text(json.dumps({'status': 'submitted', 'project_id': 'x'}))
        with self.assertRaisesRegex(rb.Stop, 'not a hyper3d record'):
            rb.collect(self.out, config=CONFIG, session_factory=lambda: self.fail('opened'))

    def test_bind(self):
        self.do_submit()
        record = rb.Record(self.out / rb.RECORD)
        with self.assertRaises(ValueError):
            record.bind('https://hyper3d.ai/workspace/rodin/' + '0' * 8 + '-0000-0000-0000-' + 'f' * 12)
        self.assertEqual(rb.parse_expect(['geometry=2']), {'geometry': 2})


class DispatchTests(unittest.TestCase):
    def test_commands(self):
        out, views = Path('/o'), Path('/v')
        self.assertEqual(convert_job.submit_command('tripo', views, out),
                         ['node', str(convert_job.TOOLS / 'studio.mjs'), 'submit', str(views), str(out)])
        self.assertEqual(convert_job.collect_command('tripo', out, {'allow_paid': True}),
                         ['node', str(convert_job.TOOLS / 'studio.mjs'), 'collect', str(out)])
        with mock.patch.dict(os.environ, {'HYPER3D_VENV': '/venv'}):
            self.assertEqual(convert_job.submit_command('hyper3d', views, out, True)[-4:],
                             ['submit', str(views), str(out), '--dry-run'])
            cmd = convert_job.collect_command('hyper3d', out, {'allow_paid': True, 'accept_preview': True})
        self.assertEqual(Path(cmd[0]), Path('/venv') / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python'))
        self.assertIn('--allow-paid', cmd)
        self.assertEqual(convert_job.collect_command('hyper3d', out)[-1], str(out))
        self.assertEqual(convert_job.option_value(['--backend=hyper3d'], '--backend'), 'hyper3d')
        self.assertEqual(convert_job.option_value(['--backend', 'tripo'], '--backend'), 'tripo')
        self.assertEqual(convert_job.option_value([], '--backend', 'tripo'), 'tripo')

    def run_main(self, backend, interior_share):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        job = root / 'processing' / 's--j'
        make_views(job / 'human_male' / 'outfit')
        (job / '.tripo-job.json').write_text(json.dumps(
            {'set': 's', 'job_id': 'j', 'modules': [{'race_gender': 'human_male', 'module': 'outfit'}]}))
        out = root / 'models/s/j/human_male/outfit'
        commands = []

        def fake_run(cmd, **kw):
            commands.append(cmd)
            joined = ' '.join(map(str, cmd))
            record = out / 'studio_submission.json'
            if ' submit ' in joined:
                out.mkdir(parents=True, exist_ok=True)
                record.write_text(json.dumps({'status': 'submitted', 'project_id': 'P',
                                              **({'backend': 'hyper3d'} if backend == 'hyper3d' else {})}))
            elif ' collect ' in joined:
                make_glb(out / 'x.glb')
                data = json.loads(record.read_text())
                data.update(status='exported', exports={'GLB': {'path': str(out / 'x.glb')}})
                record.write_text(json.dumps(data))
            elif 'retry' in joined:
                raise AssertionError('free retry must not run here')
            else:  # blender reduction
                (out / 'reduced').mkdir(parents=True, exist_ok=True)
                (out / 'reduced/manifest.json').write_text(json.dumps({
                    'interior': {'interior_area_share': interior_share, 'threshold': 0.15},
                    'levels': {'0.4': {'triangles': 5, 'mesh_checks_pass': True, 'material_review_required': False}}}))
            return mock.Mock(returncode=0)
        with mock.patch.object(convert_job, 'ROOT', root), mock.patch.object(convert_job.subprocess, 'run', fake_run), \
                mock.patch.dict(os.environ, {'HYPER3D_VENV': '/venv'}):
            try:
                convert_job.main(['s--j', '--backend', backend, '--allow-paid', '--accept-preview'])
                code = 0
            except SystemExit as exc:
                code = exc.code
        record = json.loads((job / '.tripo-job.json').read_text())
        return commands, record, code

    def test_hyper3d_routes_and_skips_free_retry(self):
        commands, record, code = self.run_main('hyper3d', 0.9)   # broken mesh: tripo would retry, hyper3d only flags
        self.assertEqual(code, 1)
        self.assertEqual(record['module_conversion']['human_male/outfit']['backend'], 'hyper3d')
        self.assertTrue(any('rodin_backend.py' in str(c) and '--allow-paid' in c for c in commands))
        self.assertFalse(any('studio.mjs' in str(c) for c in commands))

    def test_hyper3d_clean_mesh(self):
        _, record, code = self.run_main('hyper3d', 0.0)
        self.assertEqual((code, record['state'], record['conversion']),
                         (0, 'converted', 'hyper3d_and_reduction_done_pending_review'))

    def test_tripo_default_unchanged(self):
        commands, record, code = self.run_main('tripo', 0.0)
        self.assertEqual(code, 0)
        self.assertEqual(commands[0][:3], ['node', str(convert_job.TOOLS / 'studio.mjs'), 'submit'])
        self.assertEqual(commands[1][:3], ['node', str(convert_job.TOOLS / 'studio.mjs'), 'collect'])
        self.assertEqual(record['conversion'], 'tripo_and_reduction_done_pending_review')

    def test_backend_mismatch_stops(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        out = Path(temp.name)
        (out / 'studio_submission.json').write_text(json.dumps({'status': 'submitted', 'backend': 'hyper3d'}))
        with self.assertRaisesRegex(SystemExit, 'belongs to backend'):
            convert_job.collect_and_reduce('k', out, 'tripo')


if __name__ == '__main__':
    unittest.main()
