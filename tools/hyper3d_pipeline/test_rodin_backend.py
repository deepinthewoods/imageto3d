"""Offline tests for rodin_backend.py and the convert_job.py backend dispatch (browser is faked)."""
import contextlib
import io
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
        self.calls.append(('select_mesh_mode', config.get('mesh_mode_card') or 'Best Quality'))
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

    def mesh_session(self, states, exact_card=True):
        """A SeleniumRodin whose browser is replaced by recorded card states (one per read; the last one repeats) and
        recorded actions. `wf.exact` finds a matching label anywhere on the page when `exact_card` (it must never be
        clicked: only the title inside a verified card is)."""
        session = rb.SeleniumRodin(CONFIG, tempfile.mkdtemp())
        self.actions, self.shots, states = [], [], list(states)

        class Driver:
            def execute_script(self, js, *args):
                self.args = args
                return states.pop(0) if len(states) > 1 else states[0]

        scope = mock.Mock(text='Geometry card text')
        session.driver = Driver()
        session.wf = mock.Mock(GEOMETRY='geo', scope=mock.Mock(return_value=scope),
                               exact=mock.Mock(return_value='page label' if exact_card else None))
        session._control = lambda kind: 'control'
        session._hover = lambda element, nudge=False: self.actions.append(('hover', element, nudge))
        session._click_element = lambda element: self.actions.append(('click', element))
        session.screenshot = self.shots.append
        return session

    @staticmethod
    def card(selected, text='Best Quality\nTri\n1M'):
        return {'selected': selected, 'text': text, 'title': 'card title'}

    def clicks(self):
        return [a for a in self.actions if a[0] == 'click']

    def test_select_mesh_mode_picks_best_quality_card(self):
        session = self.mesh_session([self.card('true')])
        with mock.patch('time.sleep'):
            info = session.select_mesh_mode(CONFIG)
        self.assertEqual(session.driver.args, ('Best Quality',))
        self.assertEqual((session.last_slider, self.shots), ('1M', []))
        self.assertEqual(info, 'Geometry card text | Best Quality card: Best Quality Tri 1M')
        self.assertEqual(self.clicks(), [])                                    # already selected: no click
        session = self.mesh_session([self.card('false'), self.card('true')])  # not selected yet: one click, then verified
        with mock.patch('time.sleep'):
            session.select_mesh_mode(CONFIG)
        self.assertEqual(self.clicks(), [('click', 'card title')])           # the card's own title, nothing else

    def test_select_mesh_mode_success_on_last_click(self):
        # three unselected reads (three clicks); the card is selected only after the third click: judged after it
        session = self.mesh_session([self.card('false')] * 3 + [self.card('true')])
        with mock.patch('time.sleep'):
            session.select_mesh_mode(CONFIG)
        self.assertEqual((len(self.clicks()), self.shots), (3, []))

    def test_select_mesh_mode_never_clicks_outside_the_card(self):
        # the label exists elsewhere on the page (e.g. inside the paid Confirm control) but no card: zero clicks
        session = self.mesh_session([None], exact_card=True)
        with mock.patch('time.sleep'), self.assertRaisesRegex(rb.Stop, 'no .Best Quality. card'):
            session.select_mesh_mode(CONFIG)
        self.assertEqual((self.clicks(), self.shots), ([], ['mesh_mode_missing']))

    def test_select_mesh_mode_rejects_clean_topology(self):
        clean = {'selected': 'true', 'text': 'Clean Topology\nQuad Tri N-gon\n6K', 'title': 'card title'}
        session = self.mesh_session([clean])
        with mock.patch('time.sleep'), self.assertRaisesRegex(rb.Stop, 'not selected as Tri'):
            session.select_mesh_mode(dict(CONFIG, triangle_polycount_label='6K'))
        mixed = {'selected': 'true', 'text': 'Best Quality\nQuad Tri N-gon\n1M', 'title': 'card title'}
        session = self.mesh_session([mixed])
        with mock.patch('time.sleep'), self.assertRaisesRegex(rb.Stop, 'not selected as Tri'):
            session.select_mesh_mode(CONFIG)
        session = self.mesh_session([self.card('true')])
        with mock.patch('time.sleep'), self.assertRaisesRegex(rb.Stop, 'not allowed'):
            session.select_mesh_mode(dict(CONFIG, mesh_mode_card='Clean Topology'))

    def test_select_mesh_mode_stops_on_missing_or_wrong_card(self):
        with mock.patch('time.sleep'):
            session = self.mesh_session([None], exact_card=False)
            with self.assertRaisesRegex(rb.Stop, 'no .Best Quality. card'):
                session.select_mesh_mode(CONFIG)
            self.assertEqual(self.shots, ['mesh_mode_missing'])
            session = self.mesh_session([self.card('false')])   # never becomes selected
            with self.assertRaisesRegex(rb.Stop, 'not selected as Tri / 1M'):
                session.select_mesh_mode(CONFIG)
            self.assertEqual(self.shots, ['mesh_mode_not_raw'])
            session = self.mesh_session([self.card('true', 'Best Quality\nQuad\n1M')])    # wrong type
            with self.assertRaisesRegex(rb.Stop, 'not selected as Tri'):
                session.select_mesh_mode(CONFIG)
            session = self.mesh_session([self.card('true', 'Best Quality\nTri\n500K')])   # slider not at 1M
            with self.assertRaisesRegex(rb.Stop, 'Tri / 1M'):
                session.select_mesh_mode(CONFIG)
            self.assertEqual(self.shots, ['mesh_mode_not_raw'])

    def test_legacy_mesh_mode_option_is_optional(self):
        config = {k: v for k, v in CONFIG.items() if k != 'mesh_mode_option'}
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        (folder / 'config.json').write_text(json.dumps(config))
        self.assertNotIn('mesh_mode_option', rb.load_config(folder / 'config.json'))

    def test_pre_phase0_config_still_selects_best_quality(self):
        # a config written before the fix: legacy mesh_mode_option "Triangle", no mesh_mode_card
        legacy = {k: v for k, v in CONFIG.items() if k != 'mesh_mode_card'} | {'mesh_mode_option': 'Triangle'}
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        (folder / 'config.json').write_text(json.dumps(legacy))
        loaded = rb.load_config(folder / 'config.json')
        session = self.mesh_session([self.card('true')])
        with mock.patch('time.sleep'):
            info = session.select_mesh_mode(loaded)
        self.assertEqual(session.driver.args, ('Best Quality',))
        self.assertIn('Best Quality card', info)

    def test_derive_stage(self):
        self.assertEqual(rb.derive_stage('unknown', 'Pack ... Download', True, False), 'export_ready')
        self.assertEqual(rb.derive_stage('unknown', 'Unlock after Material confirmed', True, False), 'unknown')
        self.assertEqual(rb.derive_stage('unknown', 'Pack', True, True), 'unknown')
        self.assertEqual(rb.derive_stage('material_preview', 'Pack', True, False), 'material_preview')

    def test_shipped_config_is_the_half_credit_path(self):
        self.assertEqual((CONFIG['effort'], CONFIG['mesh_mode_card'], CONFIG['mesh_simplify']), ('Medium', 'Best Quality', False))
        self.assertEqual(CONFIG['triangle_polycount_label'], '1M')
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
        self.assertEqual((data['settings']['mesh_mode'], data['settings']['smart_low_poly'], data['settings']['mesh_simplify']), ('Best Quality', False, False))
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
        self.assertIn(('select_mesh_mode', 'Best Quality'), session.calls)
        self.assertIn(('set_delight',), session.calls)
        clicks = [c[1] for c in session.calls if c[0] == 'click']
        self.assertEqual(clicks, ['geometry', 'material_generate', 'material_confirm'])
        # a second collect only re-verifies the export and never opens the browser
        again = rb.collect(self.out, config=CONFIG, session_factory=lambda: self.fail('browser opened'))
        self.assertEqual(again['status'], 'exported')

    def test_collect_resumes_an_old_format_record_with_an_old_config_and_selects_best_quality(self):
        """Regression (review of phase 1, finding 22): a record written before the mesh-mode fix (`settings.mesh_mode` "Triangle") and a
        config of that time (legacy `mesh_mode_option` "Triangle", no `mesh_mode_card`) still resume collect: the geometry step selects the
        Best Quality card and reaches the export."""
        self.do_submit()
        path = self.out / rb.RECORD
        stored = json.loads(path.read_text())
        stored['settings']['mesh_mode'] = 'Triangle'
        path.write_text(json.dumps(stored))
        old_config = {k: v for k, v in CONFIG.items() if k != 'mesh_mode_card'} | {'mesh_mode_option': 'Triangle'}
        session = self.collect(['geometry_preview', 'material_ready', 'material_preview', 'export_ready'],
                               costs={'geometry': 0.5, 'material_generate': None, 'material_confirm': None},
                               config=old_config, allow_paid=True, accept_preview=True)
        self.assertIn(('select_mesh_mode', 'Best Quality'), session.calls)
        self.assertNotIn(('select_mesh_mode', 'Triangle'), session.calls)
        data = self.record()
        self.assertEqual(data['status'], 'exported')
        self.assertEqual(data['credits_committed'], 0.5)
        self.assertEqual([c[1] for c in session.calls if c[0] == 'click'], ['geometry', 'material_generate', 'material_confirm'])

    def test_crash_after_intent_never_repeats_click(self):
        self.do_submit()
        costs, expected = {'geometry': 0.5}, {'geometry': 0.5}
        session = FakeSession(self.out, stages=['geometry_preview'], costs=costs)
        session.click = lambda kind: (_ for _ in ()).throw(KeyboardInterrupt())   # dies right after the intent
        with self.assertRaises(KeyboardInterrupt):
            rb.collect(self.out, config=CONFIG, expected_costs=expected, allow_paid=True, accept_preview=True,
                       session_factory=lambda: session)
        with self.assertRaisesRegex(rb.Stop, 'geometry intent is unresolved .* needs review'):   # recheck finding 8
            self.collect(['geometry_preview'], costs=costs, expected=expected, allow_paid=True, accept_preview=True)
        self.assertFalse([c for c in FakeSession.log if c[0] == 'click'])
        self.assertEqual(self.record()['pending_action'], 'geometry')

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


class Phase5Tests(unittest.TestCase):
    """Phase 5 review: finding 1 (free preview resolved once its model is identified), finding 2 (click-time paid gate: a gate
    closed during the stage wait stops before the intent and the click, exit 4, resumable) and finding 7 (recorded inputs)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.views = make_views(self.root / 'views')
        self.out = self.root / 'out'
        self.gate_root = self.root / 'gate_root'
        self.batch = self.gate_root / '.codex_jobs' / '_batch'
        self.batch.mkdir(parents=True)
        env = mock.patch.dict(os.environ, rb.paid_gate.env_for(self.gate_root))
        env.start()
        self.addCleanup(env.stop)

    def record(self):
        return json.loads((self.out / rb.RECORD).read_text())

    def hold(self):
        (self.batch / 'CONVERT_HOLD').write_text(json.dumps({'since': 'x', 'reason': 'supervisor restart'}))

    def submit(self, **kw):
        session = FakeSession(self.out, **kw)
        session.uploaded = []
        session.prepare_multiview = lambda paths, config: session.uploaded.extend(paths)
        rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: session)
        return session

    # -- finding 1
    def test_a_submit_with_a_model_url_resolves_the_free_preview(self):
        self.submit()
        data = self.record()
        self.assertEqual((data['status'], data['pending_action']), ('submitted', None))
        entry = data['actions']['free_preview']
        self.assertEqual((entry['model_id'], entry['resolved_by']), (MODEL, 'submit'))
        self.assertIn('intent_at', entry)
        self.assertIn('done_at', entry)

    def old_record(self):
        """A record as the pre-fix writer left it: submitted, bound to a model, pending_action still free_preview."""
        self.submit()
        path = self.out / rb.RECORD
        data = json.loads(path.read_text())
        data['pending_action'] = 'free_preview'
        data['actions']['free_preview'] = {'intent_at': data['actions']['free_preview']['intent_at']}
        path.write_text(json.dumps(data))

    def test_an_existing_bound_record_is_resolved_by_inspect(self):
        self.old_record()
        rb.inspect_only(self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out))
        data = self.record()
        self.assertEqual(data['pending_action'], None)
        self.assertEqual(data['actions']['free_preview']['resolved_by'], 'inspect')
        self.assertEqual(data['last_observation'], {'stage': 'geometry_preview'})

    def test_an_existing_bound_record_is_resolved_on_load_by_collect_and_preview(self):
        for how, run in (('collect', lambda: rb.collect(self.out, config=CONFIG, session_factory=lambda: FakeSession(
                              self.out, stages=['export_ready']))),
                         ('preview', lambda: rb.preview(self.out, config=CONFIG, session_factory=lambda: FakeSession(
                              self.out, stages=['export_ready'])))):
            shutil.rmtree(self.out, ignore_errors=True)
            self.old_record()
            run()
            data = self.record()
            self.assertEqual((data['actions']['free_preview']['resolved_by'], data['pending_action']), (how, None), how)

    def test_an_unidentified_click_stays_pending_until_bind(self):
        self.submit(url=None)
        data = self.record()
        self.assertEqual((data['status'], data['pending_action']), ('submission_needs_review', 'free_preview'))
        rb.inspect_only(self.out, config=CONFIG, session_factory=lambda: self.fail('no model to open'))
        self.assertEqual(self.record()['pending_action'], 'free_preview')        # ambiguous: still protected
        rb.bind(self.out, URL)
        data = self.record()
        self.assertEqual((data['pending_action'], data['actions']['free_preview']['resolved_by']), (None, 'bind'))
        self.assertEqual(data['model_id'], MODEL)

    def test_a_pending_paid_action_is_never_resolved_as_free(self):
        self.submit()
        path = self.out / rb.RECORD
        data = json.loads(path.read_text())
        data['pending_action'] = 'geometry'
        path.write_text(json.dumps(data))
        record = rb.Record(path)
        self.assertFalse(record.resolve_free_preview('test'))
        self.assertEqual(self.record()['pending_action'], 'geometry')

    # -- finding 7
    def test_submit_records_and_uploads_the_exact_input_bytes(self):
        session = self.submit()
        data = self.record()
        upload = Path(data['upload_dir'])
        self.assertEqual(upload, (self.out / rb.UPLOAD_DIR).resolve())
        self.assertEqual([n for n, _ in session.uploaded], CONFIG['view_order'])
        self.assertTrue(all(Path(p).parent.resolve() == upload for _, p in session.uploaded))   # the copies went up
        inputs = rb.record_inputs(rb.Record(self.out / rb.RECORD))
        self.assertEqual([i['name'] for i in inputs], CONFIG['view_order'])
        for item in inputs:
            self.assertEqual(item['sha256'], rb.sha256_file(self.views / item['name']))
            self.assertEqual(item['size'], (self.views / item['name']).stat().st_size)
        self.assertEqual(rb.record_inputs(self.out), inputs)
        self.assertEqual(rb.record_inputs(data), inputs)
        self.assertEqual(data['source'], str(self.views.resolve()))

    def test_record_inputs_of_an_old_or_damaged_record(self):
        self.assertIsNone(rb.record_inputs({'status': 'submitted'}))            # written before the field: unknown
        for bad in ('x', [{'name': 'a'}], [{'name': 'a', 'sha256': 'z' * 64, 'size': 1}],
                    [{'name': 'a', 'sha256': '0' * 64, 'size': True}]):
            with self.assertRaises(ValueError):
                rb.record_inputs({'inputs': bad})

    def test_an_existing_record_keeps_its_upload_snapshot(self):
        self.submit()
        before = rb.sha256_file(self.out / rb.UPLOAD_DIR / 'front.png')
        (self.views / 'front.png').write_bytes((self.views / 'front.png').read_bytes() + b'changed')
        with self.assertRaisesRegex(rb.Stop, 'already exists'):
            self.submit()
        self.assertEqual(rb.sha256_file(self.out / rb.UPLOAD_DIR / 'front.png'), before)

    # -- finding 2
    def collect(self, session):
        return rb.collect(self.out, config=CONFIG, allow_paid=True, accept_preview=True, session_factory=lambda: session)

    def paid_costs(self):
        return {'geometry': 0.5, 'material_generate': None, 'material_confirm': None}

    def test_a_hold_set_during_the_stage_wait_stops_before_the_intent_and_the_click(self):
        """The reviewer's scenario: CONVERT_HOLD appears while the collector waits for the provider stage."""
        self.submit()
        hold = self.hold

        class Waiting(FakeSession):
            def wait_stage(self, stages, timeout):
                hold()                                        # the app holds conversions during the wait
                return super().wait_stage(stages, timeout)
        session = Waiting(self.out, stages=['geometry_preview'], costs=self.paid_costs())
        with self.assertRaisesRegex(rb.GateClosed, 'CONVERT_HOLD'):
            self.collect(session)
        self.assertFalse([c for c in session.calls if c[0] == 'click'])
        self.assertNotIn(('select_mesh_mode', 'Best Quality'), session.calls)   # stopped before anything was selected
        data = self.record()
        self.assertEqual((data['status'], data['pending_action'], list(data['actions'])), ('submitted', None, ['free_preview']))
        self.assertNotIn('credits_committed', data)
        # resumable: once the hold is gone the same record collects normally
        (self.batch / 'CONVERT_HOLD').unlink()
        session = FakeSession(self.out, stages=['geometry_preview', 'material_ready', 'material_preview', 'export_ready'],
                              costs=self.paid_costs())
        self.collect(session)
        self.assertEqual([c[1] for c in session.calls if c[0] == 'click'], ['geometry', 'material_generate', 'material_confirm'])
        self.assertEqual((self.record()['status'], self.record()['credits_committed']), ('exported', 0.5))

    def test_a_gate_closing_between_the_intent_and_the_click_withdraws_the_intent(self):
        self.submit()
        hold = self.hold

        class Late(FakeSession):
            def symmetric_value(self):
                hold()                                        # after the intent, before the click
                return 'Symmetric'
        session = Late(self.out, stages=['geometry_preview'], costs=self.paid_costs())
        with self.assertRaises(rb.GateClosed):
            self.collect(session)
        self.assertFalse([c for c in session.calls if c[0] == 'click'])
        data = self.record()
        self.assertEqual((data['pending_action'], list(data['actions'])), (None, ['free_preview']))
        self.assertNotIn('credits_committed', data)
        events = [json.loads(l)['event'] for l in (self.out / 'events.jsonl').read_text().splitlines()]
        self.assertEqual(events[events.index('geometry_intent_withdrawn') - 3:events.index('geometry_intent_withdrawn') + 1],
                         ['submission_intent', 'credits_committed', 'symmetric_selector', 'geometry_intent_withdrawn'])
        (self.batch / 'CONVERT_HOLD').unlink()
        session = FakeSession(self.out, stages=['geometry_preview', 'export_ready'], costs=self.paid_costs())
        self.collect(session)                                 # not refused as "already submitted"
        self.assertEqual([c[1] for c in session.calls if c[0] == 'click'], ['geometry'])

    def test_a_pause_closes_the_gate_unless_run_now_overrides_it(self):
        self.submit()
        (self.batch / 'PAUSE').write_text(json.dumps({'reason': 'manual pause-conversions', 'scope': ['conversions']}))
        with self.assertRaisesRegex(rb.GateClosed, 'PAUSE'):
            self.collect(FakeSession(self.out, stages=['geometry_preview'], costs=self.paid_costs()))
        with mock.patch.dict(os.environ, rb.paid_gate.env_for(self.gate_root, override_pause=True)):
            session = FakeSession(self.out, stages=['geometry_preview', 'export_ready'], costs=self.paid_costs())
            self.collect(session)
        self.assertIn(('click', 'geometry'), session.calls)

    def test_the_cli_exits_4_and_names_the_gate(self):
        self.submit()
        self.hold()
        session = FakeSession(self.out, stages=['geometry_preview'], costs=self.paid_costs())
        err = io.StringIO()
        with mock.patch.object(rb, 'SeleniumRodin', lambda config, out: session), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                rb.main(['collect', str(self.out), '--allow-paid', '--accept-preview'])
        self.assertEqual(caught.exception.code, rb.EXIT_GATE_CLOSED)
        self.assertIn('PAID GATE CLOSED: conversions are held', err.getvalue())
        self.assertFalse([c for c in session.calls if c[0] == 'click'])

    def test_no_gate_variable_means_a_manual_run(self):
        self.submit()
        self.hold()
        with mock.patch.dict(os.environ):
            os.environ.pop(rb.paid_gate.ENV)
            session = FakeSession(self.out, stages=['geometry_preview', 'export_ready'], costs=self.paid_costs())
            self.collect(session)
        self.assertIn(('click', 'geometry'), session.calls)


class RecheckTests(unittest.TestCase):
    """Recheck-before-cutover findings 8 (a paid intent is confirmed only by an advanced stage), 12 (bind moves a positively
    identified free preview to `submitted` in one write) and 13 (the final upload copies are read-only and re-verified)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.views = make_views(self.root / 'views')
        self.out = self.root / 'out'
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(rb.paid_gate.ENV, None)                 # a manual run: no gate

    def record(self):
        return json.loads((self.out / rb.RECORD).read_text())

    def wipe(self):
        """Delete the output folder; its upload copies are read-only (finding 13), which Windows refuses to delete as they are."""
        def unlock(function, path, _info):
            os.chmod(path, 0o666)
            function(path)
        shutil.rmtree(self.out, onerror=unlock)

    def events(self):
        return [json.loads(line)['event'] for line in (self.out / 'events.jsonl').read_text().splitlines()]

    def submit(self, **kw):
        session = FakeSession(self.out, **kw)
        rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: session)
        return session

    def collect(self, session):
        return rb.collect(self.out, config=CONFIG, allow_paid=True, accept_preview=True, session_factory=lambda: session)

    COSTS = {'geometry': 0.5, 'material_generate': None, 'material_confirm': None}

    def crashed_after_the_geometry_intent(self):
        """A collect that died between the geometry intent and (maybe) its click."""
        self.submit()
        session = FakeSession(self.out, stages=['geometry_preview'], costs=self.COSTS)
        session.click = lambda kind: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.collect(session)
        self.assertEqual(self.record()['pending_action'], 'geometry')

    # -- finding 8
    def test_the_same_stage_never_clears_a_pending_geometry_intent(self):
        self.crashed_after_the_geometry_intent()
        class Previewing(FakeSession):
            def dump_geometry_panel(self, label):
                return 'panel'
        rb.preview(self.out, config=CONFIG, session_factory=lambda: Previewing(self.out, stages=['geometry_preview']))
        data = self.record()                                    # the reviewer's case: preview used to clear it
        self.assertEqual(data['pending_action'], 'geometry')
        self.assertEqual((data['paid_review']['action'], data['paid_review']['stage']), ('geometry', 'geometry_preview'))
        self.assertNotIn('confirmed_at', data['actions']['geometry'])
        rb.inspect_only(self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out))
        self.assertEqual(self.record()['pending_action'], 'geometry')
        session = FakeSession(self.out, stages=['geometry_preview'], costs=self.COSTS)
        with self.assertRaisesRegex(rb.Stop, 'needs review'):
            self.collect(session)
        self.assertFalse([c for c in session.calls if c[0] == 'click'])          # never re-clicked automatically
        self.assertNotIn(('select_mesh_mode', 'Best Quality'), session.calls)
        data = self.record()
        self.assertEqual((data['pending_action'], data['status']), ('geometry', 'submitted'))
        self.assertNotIn('confirmed_at', data['actions']['geometry'])

    def test_an_advanced_stage_confirms_the_action_with_its_cost_and_ids(self):
        self.crashed_after_the_geometry_intent()

        class Later(FakeSession):
            def read_balance(self):
                return 99.5

            def provider_action_id(self, kind):
                return f'act-{kind}'
        session = Later(self.out, stages=['material_ready', 'material_preview', 'export_ready'], costs=self.COSTS)
        self.collect(session)
        data = self.record()
        entry = data['actions']['geometry']
        self.assertEqual((entry['observed_cost'], entry['stage_before'], entry['stage_after'], entry['confirmed_by']),
                         (0.5, 'geometry_preview', 'material_ready', 'stage_advanced'))
        self.assertEqual((entry['balance_before'], entry['balance_after'], entry['balance_delta']), (100.0, 99.5, 0.5))
        self.assertEqual((entry['provider_action_id'], entry['model_id']), ('act-geometry', MODEL))
        self.assertIn('confirmed_at', entry)
        self.assertEqual((data['pending_action'], data['paid_review'], data['status']), (None, None, 'exported'))
        self.assertEqual([c[1] for c in session.calls if c[0] == 'click'], ['material_generate', 'material_confirm'])   # geometry not repeated
        for kind in ('material_generate', 'material_confirm'):
            self.assertIn('confirmed_at', data['actions'][kind], kind)
            self.assertIsNone(data['actions'][kind]['observed_cost'], kind)

    def test_a_normal_collect_confirms_every_paid_action_and_records_no_id_it_cannot_see(self):
        self.submit()
        session = FakeSession(self.out, stages=['geometry_preview', 'material_ready', 'material_preview', 'export_ready'], costs=self.COSTS)
        self.collect(session)
        data = self.record()
        self.assertEqual([data['actions'][k]['stage_after'] for k in rb.COST_KINDS], ['material_ready', 'material_preview', 'export_ready'])
        self.assertEqual([data['actions'][k]['provider_action_id'] for k in rb.COST_KINDS], [None, None, None])
        self.assertEqual(self.events().count('paid_action_confirmed'), 3)
        self.assertNotIn('confirmed_at', data['actions']['free_preview'])        # the free preview is not a paid action

    def test_observed_rules(self):
        self.submit()
        record = rb.Record(self.out / rb.RECORD)
        self.assertIsNone(record.observed('geometry_preview'))                   # nothing pending
        for kind, same, later in (('geometry', 'geometry_preview', 'export_ready'),
                                  ('material_generate', 'material_ready', 'material_preview'),
                                  ('material_confirm', 'material_preview', 'export_ready')):
            record.intent(kind, observed_cost=1)
            self.assertEqual(record.observed(same), kind)                        # same stage: pending
            self.assertEqual(record.observed('unknown'), kind)                   # an unknown stage proves nothing
            if kind != 'geometry':
                self.assertEqual(record.observed('geometry_preview'), kind)      # an earlier stage neither
            self.assertEqual(record.data['pending_action'], kind)
            self.assertIsNone(record.observed(later))
            self.assertEqual((record.data['pending_action'], record.data['actions'][kind]['stage_after']), (None, later))

    def test_an_unidentified_free_preview_is_not_cleared_by_a_stage(self):
        self.submit(url=None)
        record = rb.Record(self.out / rb.RECORD)
        self.assertEqual(record.observed('export_ready'), 'free_preview')
        self.assertEqual(self.record()['pending_action'], 'free_preview')

    # -- finding 12
    def test_bind_moves_a_recovered_free_preview_to_submitted_and_collect_accepts_it(self):
        self.submit(url=None)                                                    # the reviewer's case: a free submit, URL missed
        self.assertEqual(self.record()['status'], 'submission_needs_review')
        before = len(self.events())
        rb.bind(self.out, URL)
        data = self.record()
        self.assertEqual((data['status'], data['pending_action'], data['model_id'], data['submitted_by']), ('submitted', None, MODEL, 'bind'))
        self.assertEqual(data['actions']['free_preview']['resolved_by'], 'bind')
        self.assertEqual(self.events()[before:], ['model_linked'])               # ONE record write: link + intent + status
        session = FakeSession(self.out, stages=['geometry_preview', 'export_ready'], costs=self.COSTS)
        self.collect(session)                                                    # collectable now
        self.assertEqual(self.record()['status'], 'exported')

    def test_the_normal_submit_binds_and_sets_the_status_in_one_write(self):
        self.submit()
        events = self.events()
        self.assertEqual(events[events.index('submission_clicked') + 1:], ['model_linked'])
        self.assertEqual((self.record()['status'], self.record()['pending_action']), ('submitted', None))

    def stored(self, **changes):
        path = self.out / rb.RECORD
        data = json.loads(path.read_text())
        data.update(changes)
        path.write_text(json.dumps(data))
        return data

    def test_records_left_behind_by_the_old_bind_and_by_a_crash_are_recovered_on_load(self):
        # the pre-fix bind: pending cleared, free preview resolved for this model, status left at needs_review
        self.submit()
        self.stored(status='submission_needs_review')
        session = FakeSession(self.out, stages=['export_ready'])
        rb.collect(self.out, config=CONFIG, session_factory=lambda: session)   # refused before the fix (status needs_review)
        self.assertEqual(self.record()['status'], 'exported')
        # a crash between the old two writes: bound, intent still pending, status submission_attempted
        self.wipe()
        self.submit()
        data = self.stored(status='submission_attempted', pending_action='free_preview')
        data['actions']['free_preview'] = {'intent_at': 'x'}
        self.stored(actions=data['actions'])
        rb.inspect_only(self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out))
        data = self.record()
        self.assertEqual((data['status'], data['pending_action'], data['actions']['free_preview']['resolved_by']), ('submitted', None, 'inspect'))

    def test_a_click_whose_status_write_was_lost_is_recovered_by_bind_only_while_the_intent_is_pending(self):
        self.submit(url=None)
        self.stored(status='ready_to_submit')                                    # intent written, crash before 'submission_clicked'
        rb.bind(self.out, URL)
        self.assertEqual(self.record()['status'], 'submitted')
        self.wipe()
        self.submit()
        self.stored(status='ready_to_submit')                                    # no pending intent: not the same situation
        rb.bind(self.out, URL)
        self.assertEqual(self.record()['status'], 'ready_to_submit')

    def test_ambiguous_and_paid_intents_stay_protected(self):
        # a paid intent pending: bind links the model but neither resolves nor moves the status
        self.submit(url=None)
        self.stored(pending_action='geometry')
        rb.bind(self.out, URL)
        data = self.record()
        self.assertEqual((data['status'], data['pending_action'], data['model_id']), ('submission_needs_review', 'geometry', MODEL))
        self.assertNotIn('done_at', data['actions']['free_preview'])
        # no free-preview intent at all (ambiguous): the status stays
        self.wipe()
        self.submit(url=None)
        self.stored(pending_action=None, actions={})
        rb.bind(self.out, URL)
        self.assertEqual(self.record()['status'], 'submission_needs_review')
        # a free preview resolved for ANOTHER model: the status stays
        self.wipe()
        self.submit()
        other = MODEL.replace('e169', 'f000')
        self.stored(status='submission_needs_review', actions={'free_preview': {'intent_at': 'x', 'done_at': 'y', 'model_id': other}})
        rb.inspect_only(self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out))
        self.assertEqual(self.record()['status'], 'submission_needs_review')
        # a different model than the bound one is still refused
        with self.assertRaises(ValueError):
            rb.bind(self.out, f'{rb.BASE}/{other}')
        # other statuses are never moved
        for status in ('preparing', 'dry_run', 'not_submitted', 'export_needs_review', 'exported'):
            self.stored(status=status, actions={'free_preview': {'intent_at': 'x', 'done_at': 'y', 'model_id': MODEL}})
            rb.bind(self.out, URL)
            self.assertEqual(self.record()['status'], status)

    # -- finding 13
    def tamper(self, name='front.png'):
        path = self.out / rb.UPLOAD_DIR / name
        os.chmod(path, 0o666)
        path.write_bytes(path.read_bytes() + b'tampered')

    def test_the_upload_copies_are_read_only_and_verified(self):
        self.submit()
        upload = self.out / rb.UPLOAD_DIR
        for name in CONFIG['view_order']:
            self.assertFalse(os.access(upload / name, os.W_OK), name)
            with self.assertRaises(PermissionError):
                (upload / name).write_bytes(b'x')
        self.assertEqual(rb.verify_upload_inputs(rb.Record(self.out / rb.RECORD)), [])
        self.assertEqual(rb.verify_upload_inputs(self.out), [])
        self.assertEqual(rb.verify_upload_inputs(self.out / rb.RECORD), [])
        self.assertEqual(rb.verify_upload_inputs(self.record()), [])
        self.assertIsNone(rb.verify_upload_inputs({'status': 'submitted'}))      # a record from before the field: unknown
        self.tamper()
        problems = rb.verify_upload_inputs(self.out)
        self.assertEqual(len(problems), 1)
        self.assertIn('front.png', problems[0])
        os.chmod(upload / 'back.png', 0o666)
        (upload / 'back.png').unlink()
        self.assertEqual(len(rb.verify_upload_inputs(self.out)), 2)

    # -- final-verify row 14: the writer checks every final destination and never writes through an existing entry
    def victim(self):
        """A read-only file OUTSIDE the output dir that a linked destination would redirect the overwrite to."""
        outside = self.root / 'outside'
        outside.mkdir(exist_ok=True)
        path = outside / 'victim.png'
        path.write_bytes(b'not a view: must survive')
        os.chmod(path, 0o444)
        self.addCleanup(lambda: os.chmod(path, 0o666) if path.exists() else None)
        return path

    def assert_refused(self, victim, pattern):
        session = FakeSession(self.out)
        with self.assertRaisesRegex(rb.Stop, pattern):
            rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: session)
        self.assertEqual(victim.read_bytes(), b'not a view: must survive')
        self.assertFalse(os.access(victim, os.W_OK))                            # its read-only bit was not cleared either
        self.assertFalse((self.out / rb.RECORD).exists())
        self.assertFalse(session.calls)                                         # nothing was opened or uploaded

    def test_a_hard_linked_destination_is_refused_and_the_outside_file_is_untouched(self):
        victim = self.victim()
        upload = self.out / rb.UPLOAD_DIR
        upload.mkdir(parents=True)
        os.link(victim, upload / 'front.png')                                   # the same file under the destination name
        self.assert_refused(victim, 'hard-linked to another file')
        self.assertEqual(sorted(p.name for p in upload.iterdir()), ['front.png'])   # no view was written, no temp file left

    def test_a_symlinked_destination_is_refused_and_not_followed(self):
        victim = self.victim()
        upload = self.out / rb.UPLOAD_DIR
        upload.mkdir(parents=True)
        try:
            os.symlink(victim, upload / 'back.png')
        except (OSError, NotImplementedError) as error:
            self.skipTest(f'cannot create a symlink here ({error})')
        self.assert_refused(victim, 'is a link, junction or not a regular file')
        self.assertTrue(os.path.islink(upload / 'back.png'))

    def test_an_upload_folder_that_is_a_link_or_junction_is_refused(self):
        victim = self.victim()
        self.out.mkdir(parents=True)
        link = self.out / rb.UPLOAD_DIR
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(victim.parent), str(link))
        else:
            os.symlink(victim.parent, link, target_is_directory=True)
        self.addCleanup(lambda: os.rmdir(link) if os.name == 'nt' else os.unlink(link))
        self.assert_refused(victim, 'not a plain folder of the output')
        self.assertEqual([p.name for p in victim.parent.iterdir()], ['victim.png'])   # nothing was written through the junction

    def test_a_destination_that_is_a_folder_or_not_a_bare_name_is_refused(self):
        upload = self.out / rb.UPLOAD_DIR
        (upload / 'front.png').mkdir(parents=True)
        with self.assertRaisesRegex(rb.Stop, 'not a regular file'):
            rb.snapshot_inputs({'views_dir': self.views, 'images': ['front.png'], 'uploaded': ['front.png']}, self.out)
        for name in ('../escaped.png', 'sub/front.png', '..', ''):
            with self.assertRaisesRegex(rb.Stop, 'not a plain file name'):
                rb.snapshot_inputs({'views_dir': self.views, 'images': [name], 'uploaded': [name]}, self.out)
        self.assertFalse((self.out / 'escaped.png').exists())

    def test_an_own_read_only_snapshot_is_replaced_by_a_new_file_not_written_in_place(self):
        upload = self.out / rb.UPLOAD_DIR
        upload.mkdir(parents=True)
        old = upload / 'front.png'
        old.write_bytes(b'an earlier snapshot')
        os.chmod(old, 0o444)
        keep = self.root / 'kept_handle.png'
        before = os.stat(old)
        plan = {'views_dir': self.views, 'images': list(CONFIG['view_order']), 'uploaded': list(CONFIG['view_order'])}
        target, inputs = rb.snapshot_inputs(plan, self.out)
        self.assertEqual(target, upload)
        after = os.stat(old)
        self.assertNotEqual((before.st_ino, before.st_size), (after.st_ino, after.st_size))   # a new file took the name
        self.assertEqual(old.read_bytes(), (self.views / 'front.png').read_bytes())
        self.assertEqual([i['name'] for i in inputs], list(CONFIG['view_order']))
        for item in inputs:
            self.assertEqual(item['sha256'], rb.sha256_file(self.views / item['name']))
            self.assertEqual(item['size'], (self.views / item['name']).stat().st_size)
            self.assertFalse(os.access(upload / item['name'], os.W_OK))
        self.assertEqual(sorted(p.name for p in upload.iterdir()), sorted(CONFIG['view_order']))   # no temp file left
        self.assertFalse(keep.exists())
        # a stale marker of the snapshot's own is removed although it is read-only
        marker = upload / 'views_omitted.json'
        marker.write_text('{}')
        os.chmod(marker, 0o444)
        rb.snapshot_inputs(plan, self.out)
        self.assertFalse(marker.exists())

    def test_a_copy_changed_before_the_upload_stops_the_submit_before_anything_is_sent(self):
        tamper = self.tamper

        class Tampering(FakeSession):
            def open_workspace(self):
                tamper()                                                         # after the hash, before the upload
                super().open_workspace()
        session = Tampering(self.out)
        with self.assertRaisesRegex(rb.Stop, 'upload copies changed after they were hashed'):
            rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: session)
        self.assertFalse([c for c in session.calls if c[0] in ('prepare_multiview', 'click_free_preview')])
        self.assertFalse((self.out / rb.RECORD).exists())                        # set aside: nothing was generated
        self.submit()                                                            # a re-run replaces the read-only snapshot
        self.assertEqual((self.record()['status'], rb.verify_upload_inputs(self.out)), ('submitted', []))

    def test_a_copy_changed_during_the_upload_stops_before_the_click(self):
        tamper = self.tamper

        class Tampering(FakeSession):
            def prepare_multiview(self, paths, config):
                super().prepare_multiview(paths, config)
                tamper()
        session = Tampering(self.out)
        with self.assertRaisesRegex(rb.Stop, 'changed during the upload'):
            rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: session)
        self.assertNotIn(('click_free_preview',), session.calls)

    def test_collect_stops_before_the_paid_click_when_the_uploaded_evidence_differs(self):
        self.submit()
        self.tamper('back.png')
        session = FakeSession(self.out, stages=['geometry_preview'], costs=self.COSTS)
        with self.assertRaisesRegex(rb.Stop, 'upload inputs differ from the recorded submission before geometry'):
            self.collect(session)
        self.assertFalse([c for c in session.calls if c[0] == 'click'])
        self.assertNotIn(('select_mesh_mode', 'Best Quality'), session.calls)
        data = self.record()
        self.assertEqual((data['pending_action'], list(data['actions'])), (None, ['free_preview']))   # no paid intent written
        self.assertIn('back.png', data['inputs_problems'][0])
        self.assertNotIn('credits_committed', data)

    def test_a_copy_changed_while_the_cost_is_read_stops_before_the_intent(self):
        self.submit()
        tamper = self.tamper

        class Late(FakeSession):
            def read_cost(self, kind):
                tamper()
                return super().read_cost(kind)
        session = Late(self.out, stages=['geometry_preview'], costs=self.COSTS)
        with self.assertRaisesRegex(rb.Stop, 'upload inputs differ'):
            self.collect(session)
        self.assertFalse([c for c in session.calls if c[0] == 'click'])
        self.assertEqual(list(self.record()['actions']), ['free_preview'])

    def test_a_record_without_recorded_inputs_still_collects(self):
        self.submit()
        path = self.out / rb.RECORD
        data = json.loads(path.read_text())
        del data['inputs']
        path.write_text(json.dumps(data))
        self.tamper()                                                            # unknown inputs: nothing to compare
        session = FakeSession(self.out, stages=['geometry_preview', 'export_ready'], costs=self.COSTS)
        self.collect(session)
        self.assertIn(('click', 'geometry'), session.calls)


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
        profile = mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(root / 'xdg')})   # a temp Tripo profile reservation: the
        profile.start()                                                              # tripo run never opens the real profile's locks
        self.addCleanup(profile.stop)
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
