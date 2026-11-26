"""Offline tests for rodin_backend.py and the convert_job.py backend dispatch (browser is faked)."""
import contextlib
import io
import itertools
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import threading
import time
import types
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


@contextlib.contextmanager
def no_hold(path):
    """rb.hold_directory switched off: what a system without the share mode does (the pin checks alone)."""
    yield False


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

    # ---- site change 2026-11-26: percentage wallet pill + "What's New" dialog (probe .codex_jobs/hyper3d_probe_20261126/)
    WALLET = {'header': '35%', 'aria': 'Business \u00b7 35%', 'plan_percent': '35%', 'figures': ['861.50', '2,496.00'],
              'permanent': '10.00', 'meter': '34.515224358974365',
              'summary': 'Plan remaining35%Available / total861.50 / 2,496.00 creditsPermanent available10.00 credits'}

    def test_wallet_panel_gives_the_absolute_credits(self):
        self.assertEqual(rb.parse_wallet(self.WALLET),
                         {'credits': 871.5, 'plan_available': 861.5, 'plan_total': 2496.0, 'permanent': 10.0,
                          'plan_percent': 35.0, 'header': '35%'})
        self.assertEqual(rb.parse_wallet({**self.WALLET, 'meter': None})['credits'], 871.5)   # the bar is optional evidence
        # the header as it is now carries no credit number: the legacy parser cannot (and must not) read one from it
        self.assertIsNone(rb.parse_balance('Business\nAdd On\nAPI\nMCP\n35%\nGen-2.5 Agent-Ready'))

    def test_wallet_while_the_account_loads_is_a_certain_zero(self):
        loading = {'header': '0', 'aria': 'Permanent credits \u00b7 0', 'plan_percent': '\u2014', 'figures': ['0.00', '\u2014'],
                   'permanent': '0.00', 'meter': None,
                   'summary': 'Plan used up\u2014Available / total0.00 / \u2014 creditsPermanent available0.00 credits'}
        self.assertEqual(rb.parse_wallet(loading)['credits'], 0.0)
        self.assertEqual(rb.parse_wallet({**loading, 'header': '12', 'permanent': '12.00'})['credits'], 12.0)
        self.assertIsNone(rb.parse_wallet({**loading, 'figures': ['5.00', '\u2014']}))    # credits without a plan total
        self.assertIsNone(rb.parse_wallet({**loading, 'header': '3'}))                    # pill != permanent credits

    def test_wallet_is_unreadable_rather_than_guessed(self):
        w = self.WALLET
        for bad in (None, {}, 'x',
                    {**w, 'figures': []}, {**w, 'figures': ['861.50']}, {**w, 'figures': ['35%', '2,496.00']},
                    {**w, 'figures': ['861.50', 'lots']}, {**w, 'figures': ['', '2,496.00']},
                    {**w, 'figures': ['2,600.00', '2,496.00']},                 # more available than the plan holds
                    {**w, 'figures': ['861.50', '0.00']},
                    {**w, 'permanent': None}, {**w, 'permanent': '10.00 USD'},
                    {**w, 'summary': 'Plan remaining35%Available / total861.50 / 2,496.00 pointsPermanent available10.00'},
                    {**w, 'summary': None},
                    {**w, 'meter': '50'}, {**w, 'meter': 'full'},               # the bar disagrees with the figures
                    {**w, 'plan_percent': '60%', 'header': '60%'},              # the printed percentage disagrees
                    {**w, 'plan_percent': None},
                    {**w, 'header': '36%'}, {**w, 'header': ''}, {**w, 'header': 'Business'}, {**w, 'header': None},
                    {**w, 'header': '861.5'}):                                  # a number that is not the permanent credits
            self.assertIsNone(rb.parse_wallet(bad), bad)
        # a percentage alone is never turned into credits
        self.assertIsNone(rb.parse_wallet({'header': '35%', 'plan_percent': '35%', 'meter': '35', 'figures': [],
                                           'permanent': None, 'summary': 'Plan remaining35%'}))

    def wallet_session(self, script_result, body='Business\nAdd On\nAPI\nMCP\n35%'):
        session = rb.SeleniumRodin(CONFIG, tempfile.mkdtemp())

        class Driver:
            def execute_script(self, js, *args):
                if isinstance(script_result, Exception):
                    raise script_result
                return script_result

            def find_element(self, by, value):
                return mock.Mock(text=body)

        session.driver = Driver()
        return session

    def test_read_balance_uses_the_wallet_panel(self):
        session = self.wallet_session(dict(self.WALLET))
        self.assertEqual(session.read_balance(), 871.5)
        self.assertEqual(session.last_wallet['plan_available'], 861.5)
        with mock.patch('time.sleep'):
            self.assertEqual(session.wait_balance(60), 871.5)

    def test_read_balance_is_none_when_the_panel_cannot_be_read(self):
        for result in (None, {**self.WALLET, 'figures': []}, RuntimeError('page gone')):
            session = self.wallet_session(result)
            self.assertIsNone(session.read_balance())
            self.assertIsNone(session.last_wallet)
        # ... so the submit stops after its wait: the last value read is None, never a number made from "35%"
        with mock.patch('time.sleep'), mock.patch('time.monotonic', side_effect=itertools.count(0, 30)):
            self.assertIsNone(self.wallet_session(None).wait_balance(60))

    def test_read_balance_still_reads_the_header_from_before_the_change(self):
        session = self.wallet_session(None, body='Add On\nAPI\nMCP&CLI\n1310.5\nQuinlan')
        self.assertEqual(session.read_balance(), 1310.5)
        self.assertIsNone(session.last_wallet)

    def test_submit_records_the_wallet_figures_behind_the_balance(self):
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)
        views = out / 'views'
        make_views(views)
        session = FakeSession(out / 'job', balance=871.5)
        session.last_wallet = rb.parse_wallet(self.WALLET)
        data = rb.submit(views, out / 'job', config=CONFIG, session_factory=lambda: session)
        self.assertEqual((data['balance_before_submit'], data['wallet_before_submit']['permanent']), (871.5, 10.0))

    WHATS_NEW = {'label': "What's New", 'title': 'Low-Poly, Now with Artist-Style Topology',
                 'buttons': [{'text': '', 'aria': 'Close'}, {'text': '', 'aria': 'Play video'},
                             {'text': '', 'aria': 'Turn sound on'}, {'text': 'Got it', 'aria': ''}]}

    def test_announcement_button_is_got_it_or_the_close_x_and_nothing_else(self):
        pick = rb.announcement_button
        self.assertEqual(pick(self.WHATS_NEW), 3)                                    # Got it
        self.assertEqual(pick({**self.WHATS_NEW, 'buttons': self.WHATS_NEW['buttons'][:3]}), 0)   # no Got it: the X
        self.assertEqual(pick({'label': '', 'buttons': [{'text': 'Learn more', 'aria': ''}, {'text': ' got it ', 'aria': ''}]}), 1)
        self.assertEqual(pick({'label': 'Announcement', 'buttons': [{'text': 'Try it now', 'aria': ''}, {'text': '', 'aria': 'close'}]}), 1)
        # an announcement without either button is not touched (its other buttons are never pressed)
        self.assertIsNone(pick({'label': "What's New", 'buttons': [{'text': 'Try it now', 'aria': ''}, {'text': 'Upgrade', 'aria': ''}]}))
        self.assertIsNone(pick({'label': "What's New", 'buttons': [{'text': 'Close and buy', 'aria': 'Close'}]}))
        # any other dialog is left alone, also when it has a Close X: only announcements are closed
        for other in ({'label': 'Confirm purchase', 'buttons': [{'text': '', 'aria': 'Close'}, {'text': 'Pay $12', 'aria': ''}]},
                      {'label': '', 'buttons': [{'text': 'Confirm0.5 Credits', 'aria': ''}, {'text': 'Cancel', 'aria': ''}]},
                      {'label': 'Top up', 'buttons': [{'text': 'Got it, pay', 'aria': ''}, {'text': 'OK', 'aria': ''}]},
                      {'label': None, 'buttons': None}, {}):
            self.assertIsNone(pick(other), other)

    def dialog_session(self, pages):
        """A SeleniumRodin whose page shows `pages[0]`, then `pages[1]` after the first click, ... (the last one stays).
        Each page is a list of dialogs; clicks are recorded as (dialog label, button text or aria)."""
        session = rb.SeleniumRodin(CONFIG, tempfile.mkdtemp())
        self.clicked, self.shots, state = [], [], {'page': 0}
        test = self

        class Button:
            def __init__(self, dialog, button, stuck):
                self.dialog, self.button, self.stuck = dialog, button, stuck

            def click(self):
                test.clicked.append((self.dialog['label'], self.button['text'] or self.button['aria']))
                if not self.stuck:
                    state['page'] = min(state['page'] + 1, len(pages) - 1)

        class Driver:
            def execute_script(self, js, *args):
                return [{**d, 'elements': [Button(d, b, d.get('stuck')) for b in d['buttons']]} for d in pages[state['page']]]

        session.driver, session.screenshot = Driver(), self.shots.append
        return session

    def test_dismiss_announcements_clicks_got_it_and_reports(self):
        session = self.dialog_session([[self.WHATS_NEW], []])
        out = io.StringIO()
        with mock.patch('time.sleep'), contextlib.redirect_stdout(out):
            closed = session.dismiss_announcements()
        self.assertEqual(closed, ["What's New - Low-Poly, Now with Artist-Style Topology"])
        self.assertEqual(self.clicked, [("What's New", 'Got it')])
        self.assertIn('Hyper3D announcement closed', out.getvalue())

    def test_dismiss_announcements_closes_several_and_leaves_other_dialogs(self):
        second = {'label': 'Announcement', 'title': 'Sale', 'buttons': [{'text': 'Shop now', 'aria': ''}, {'text': '', 'aria': 'Close'}]}
        purchase = {'label': 'Confirm purchase', 'title': '', 'buttons': [{'text': '', 'aria': 'Close'}, {'text': 'Pay $12', 'aria': ''}]}
        session = self.dialog_session([[purchase, self.WHATS_NEW, second], [purchase, second], [purchase]])
        with mock.patch('time.sleep'), contextlib.redirect_stdout(io.StringIO()):
            closed = session.dismiss_announcements()
        self.assertEqual(len(closed), 2)
        self.assertEqual(self.clicked, [("What's New", 'Got it'), ('Announcement', 'Close')])   # never Shop now / Pay / the purchase X
        self.assertEqual(session._covering_dialogs(), ' (a dialog covers the page: Confirm purchase)')

    def test_dismiss_announcements_without_one_clicks_nothing(self):
        purchase = {'label': 'Confirm purchase', 'title': '', 'buttons': [{'text': '', 'aria': 'Close'}, {'text': 'Pay $12', 'aria': ''}]}
        for page in ([], [purchase]):
            session = self.dialog_session([page])
            with mock.patch('time.sleep'), mock.patch('time.monotonic', side_effect=itertools.count(0, 3)):
                self.assertEqual(session.dismiss_announcements(wait=4), [])
            self.assertEqual(self.clicked, [])
        session = self.dialog_session([[]])
        session.driver.execute_script = mock.Mock(side_effect=RuntimeError('navigating'))
        self.assertEqual(session.dismiss_announcements(), [])

    def test_dismiss_announcements_waits_for_a_late_dialog(self):
        session = self.dialog_session([[self.WHATS_NEW], []])
        reads, original = [], session.driver.execute_script

        def late(js, *args):                       # the dialog only shows on the third look
            reads.append(1)
            return original(js, *args) if len(reads) != 1 and len(reads) != 2 else []

        session.driver.execute_script = late
        with mock.patch('time.sleep'), mock.patch('time.monotonic', side_effect=itertools.count(0, 1)), \
                contextlib.redirect_stdout(io.StringIO()):
            closed = session.dismiss_announcements(wait=4)
        self.assertEqual((len(closed), self.clicked), (1, [("What's New", 'Got it')]))

    def test_an_announcement_that_stays_open_stops_the_run(self):
        session = self.dialog_session([[{**self.WHATS_NEW, 'stuck': True}]])
        with mock.patch('time.sleep'), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(rb.Stop, 'announcement dialog did not close'):
                session.dismiss_announcements()
        self.assertEqual(len(self.clicked), rb.SeleniumRodin.ANNOUNCEMENT_ROUNDS)
        self.assertEqual(set(self.clicked), {("What's New", 'Got it')})
        self.assertEqual(self.shots, ['announcement_not_dismissed'])

    def test_pages_are_cleared_of_announcements_before_they_are_used(self):
        session = rb.SeleniumRodin(CONFIG, tempfile.mkdtemp())
        order = []
        session.wf = mock.Mock(navigate=lambda driver, url: order.append('navigate'))
        session._require_signed_in = lambda: order.append('signed_in')
        session.dismiss_announcements = lambda wait=0: order.append(('dismiss', wait))
        with mock.patch('time.sleep'):
            session.open_workspace()
            session.open_model('https://hyper3d.ai/workspace/rodin/x')
        self.assertEqual(order, ['navigate', 'signed_in', ('dismiss', rb.ANNOUNCEMENT_WAIT_S)] * 2)
        # the Free Preview and price reads close one first; a dialog that will not close stops before anything is clicked
        session.dismiss_announcements = mock.Mock(side_effect=rb.Stop('announcement dialog did not close'))
        session._control = mock.Mock(side_effect=AssertionError('control looked up under a dialog'))
        session.wf = mock.Mock(exact=mock.Mock(side_effect=AssertionError('Generate looked up under a dialog')))
        chains = types.ModuleType('selenium.webdriver.common.action_chains')
        chains.ActionChains = mock.Mock()
        modules = {'selenium': mock.Mock(), 'selenium.webdriver': mock.Mock(), 'selenium.webdriver.common': mock.Mock(),
                   'selenium.webdriver.common.action_chains': chains}
        with mock.patch.dict(sys.modules, modules):
            for call in (session.click_free_preview, lambda: session.read_cost('geometry')):
                with self.assertRaisesRegex(rb.Stop, 'did not close'):
                    call()

    def tab_session(self, tab_shows_on):
        """SeleniumRodin._select_multiview_tab against a fake page: the Multi-view tab is visible from hover number
        `tab_shows_on` (None = never)."""
        session = rb.SeleniumRodin(CONFIG, tempfile.mkdtemp())
        self.shots, self.dismissed, hovers = [], [], []
        visible = mock.Mock(is_displayed=lambda: True)

        class Driver:
            def find_elements(self, by, xpath):
                if 'Multi-view' in xpath:
                    return [visible] if tab_shows_on is not None and len(hovers) >= tab_shows_on else []
                return [visible]

            def execute_script(self, js, *args):
                if 'elementFromPoint' in js:
                    hovers.append(1)
                    return visible
                if 'generation-tabs' in js:
                    return '--items: 2; --active: 1;'
                return [{'label': 'Survey', 'title': '', 'buttons': [], 'elements': []}]      # open_dialogs

        session.driver, session.wf, session.screenshot = Driver(), mock.Mock(), self.shots.append
        session.dismiss_announcements = lambda wait=0: self.dismissed.append(len(hovers))
        chains, by = types.ModuleType('selenium.webdriver.common.action_chains'), types.ModuleType('selenium.webdriver.common.by')
        chains.ActionChains, by.By = mock.Mock(), types.SimpleNamespace(XPATH='xpath')
        modules = {'selenium': mock.Mock(), 'selenium.webdriver': mock.Mock(), 'selenium.webdriver.common': mock.Mock(),
                   'selenium.webdriver.common.action_chains': chains, 'selenium.webdriver.common.by': by}
        return session, modules, visible

    def test_multiview_tab_is_looked_for_after_announcements_are_closed(self):
        session, modules, tab = self.tab_session(1)
        with mock.patch.dict(sys.modules, modules), mock.patch('time.sleep'):
            session._select_multiview_tab()
        self.assertEqual((self.dismissed, self.shots), ([0], []))          # closed before the first hover
        tab.click.assert_called_once()
        session, modules, tab = self.tab_session(2)                        # a dialog opened late: second hover finds it
        with mock.patch.dict(sys.modules, modules), mock.patch('time.sleep'):
            session._select_multiview_tab()
        self.assertEqual(self.dismissed, [0, 1])
        tab.click.assert_called_once()

    def test_a_missing_multiview_tab_names_the_dialog_in_the_way(self):
        session, modules, tab = self.tab_session(None)
        with mock.patch.dict(sys.modules, modules), mock.patch('time.sleep'):
            with self.assertRaisesRegex(rb.Stop, r'Multi-view tab not found \(a dialog covers the page: Survey\)'):
                session._select_multiview_tab()
        self.assertEqual(self.shots, ['multiview_tab_missing'])
        tab.click.assert_not_called()

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
        # final-live review: the same verified bytes already in place are adopted, never a FileExistsError
        self.assertEqual(rb.place_glb(archive, out), (target, True))
        self.assertFalse(list(out.glob(rb.EXPORT_STAGING + '*')))                 # the staging folder is gone
        make_glb(self.root / 'inner.glb', triangles=99)                          # another model under the same name
        with zipfile.ZipFile(archive, 'w') as z:
            z.write(self.root / 'inner.glb', 'model/inner.glb')
        kept = target.read_bytes()
        with self.assertRaisesRegex(rb.Stop, 'DIFFERENT content .*needs review'):
            rb.extract_glb(archive, out)
        self.assertEqual(target.read_bytes(), kept)                              # never overwritten
        self.assertFalse(list(out.glob(rb.EXPORT_STAGING + '*')))

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
        self.assertEqual(upload, (self.out / rb.UPLOAD_DIR / data['upload_dir_id']['name']).resolve())   # a fresh folder (row 14)
        info = os.lstat(upload)
        self.assertEqual((data['upload_dir_id']['dev'], data['upload_dir_id']['ino']), (info.st_dev, info.st_ino))
        self.assertIsNone(rb.upload_pin_problem(self.out, data['upload_dir_id']))
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
        upload = Path(self.record()['upload_dir'])
        before = rb.sha256_file(upload / 'front.png')
        (self.views / 'front.png').write_bytes((self.views / 'front.png').read_bytes() + b'changed')
        with self.assertRaisesRegex(rb.Stop, 'already exists'):
            self.submit()
        self.assertEqual(rb.sha256_file(upload / 'front.png'), before)
        self.assertEqual([p.name for p in (self.out / rb.UPLOAD_DIR).iterdir()], [upload.name])   # no second snapshot folder

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

    # -- last-check row 6: the final gate check and the click are one critical section under the gate transition mutex
    def test_a_hold_cannot_be_written_between_the_final_check_and_the_click(self):
        self.submit()
        gate, batch, seen = rb.paid_gate, self.batch, {}
        gate_root = self.gate_root

        class Clicking(FakeSession):
            def click(self, kind):
                # the app tries to hold conversions while this click is dispatched: its writer cannot get the mutex
                try:
                    with gate.transition_lock(gate_root, wait=0.05, what='hold writer'):
                        (batch / 'CONVERT_HOLD').write_text('{}')
                    seen[kind] = 'written'
                except gate.GateBusy:
                    seen[kind] = 'busy'
                seen['owner'] = gate.lock_owner(gate_root)
                super().click(kind)
        session = Clicking(self.out, stages=['geometry_preview', 'export_ready'], costs=self.paid_costs())
        self.collect(session)
        self.assertEqual(seen['geometry'], 'busy')
        self.assertEqual((seen['owner']['pid'], seen['owner']['what']), (os.getpid(), 'paid click'))
        self.assertIn(('click', 'geometry'), session.calls)
        self.assertFalse((batch / 'CONVERT_HOLD').exists())
        self.assertFalse(gate.lock_busy(gate_root))                              # released after the click
        with gate.transition_lock(gate_root, wait=0.05):                         # and a writer gets it at once
            pass

    def test_a_slow_control_lookup_inside_the_click_keeps_the_lock_and_a_queue_pause_stops_the_next_click(self):
        """Post-cut-over review, blockers 2 and 3: session.click looks the control up AFTER the gate check; while that stalls a
        pause writer stays locked out (the lock does not expire), and a manual pause MODE closes the gate without any PAUSE file."""
        self.submit()
        gate, batch, order = rb.paid_gate, self.batch, []
        gate_root = self.gate_root
        mode = batch / gate.MODE_FILE

        def pause():                                                             # Queue Pause: the mode write, under the lock
            with gate.transition_lock(gate_root, wait=20.0, what='mode pause'):
                mode.write_text(json.dumps({'mode': 'pause'}))
                order.append('paused')
        writer = threading.Thread(target=pause)

        class Stalling(FakeSession):
            def click(self, kind):
                writer.start()
                time.sleep(0.5)                                                  # the control look-up stalls
                assert writer.is_alive() and not mode.exists()
                order.append(f'click {kind}')
                super().click(kind)

            def wait_stage(self, stages, timeout):
                if writer.ident is not None:
                    writer.join(20)                                              # the Pause request has been acknowledged
                return super().wait_stage(stages, timeout)
        session = Stalling(self.out, stages=['geometry_preview', 'material_ready'], costs=self.paid_costs())
        self.addCleanup(lambda: writer.ident is None or writer.join(20))
        with self.assertRaisesRegex(rb.GateClosed, 'mode pause'):                # acknowledged: the next paid step does not click
            self.collect(session)
        self.assertEqual(order, ['click geometry', 'paused'])                    # the pause landed after the click, not inside it
        self.assertFalse((batch / 'PAUSE').exists())                             # no supervisor tick was needed
        self.assertEqual([c for c in session.calls if c[0] == 'click'], [('click', 'geometry')])

    def test_a_gate_transition_in_progress_is_a_closed_gate_and_withdraws_the_intent(self):
        self.submit()
        session = FakeSession(self.out, stages=['geometry_preview'], costs=self.paid_costs())
        real = rb.paid_gate.checked_dispatch
        with rb.paid_gate.transition_lock(self.gate_root, what='a hold being written'), \
                mock.patch.object(rb.paid_gate, 'checked_dispatch', lambda dispatch: real(dispatch, wait=0.1)):
            with self.assertRaisesRegex(rb.GateClosed, 'gate transition in progress'):
                self.collect(session)
        self.assertFalse([c for c in session.calls if c[0] == 'click'])
        data = self.record()
        self.assertEqual((data['pending_action'], list(data['actions'])), (None, ['free_preview']))
        self.assertNotIn('credits_committed', data)
        session = FakeSession(self.out, stages=['geometry_preview', 'export_ready'], costs=self.paid_costs())
        self.collect(session)                                                    # resumable once the transition is over
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
    def upload(self):
        """The snapshot folder of the current record (a fresh folder per submission: last-check row 14)."""
        return self.out / rb.UPLOAD_DIR / self.record()['upload_dir_id']['name']

    def tamper(self, name='front.png'):
        path = self.upload() / name
        os.chmod(path, 0o666)
        path.write_bytes(path.read_bytes() + b'tampered')

    def test_the_upload_copies_are_read_only_and_verified(self):
        self.submit()
        upload = self.upload()
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

    # -- row 14 (final-verify, last-check): a fresh pinned snapshot folder per submission; destinations are never written through
    PLAN = property(lambda self: {'views_dir': self.views, 'images': list(CONFIG['view_order']), 'uploaded': list(CONFIG['view_order'])})

    def victim(self):
        """A read-only file OUTSIDE the output dir that a redirected snapshot would overwrite."""
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

    def junction(self, target, link):
        """A directory link `link` -> `target` (a junction on Windows: no privilege needed), removed again at the end."""
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(target, link, target_is_directory=True)
        self.addCleanup(lambda: (os.rmdir(link) if os.name == 'nt' else os.unlink(link)) if os.path.lexists(link) else None)

    def test_every_submission_gets_a_fresh_exclusively_created_folder(self):
        first, _, pin1 = rb.snapshot_inputs(self.PLAN, self.out)
        second, inputs, pin2 = rb.snapshot_inputs(self.PLAN, self.out)
        self.assertNotEqual(first, second)                                      # never reused
        self.assertEqual({first.parent, second.parent}, {self.out / rb.UPLOAD_DIR})
        self.assertEqual((first.name, second.name), (pin1['name'], pin2['name']))
        for target, pin in ((first, pin1), (second, pin2)):
            info = os.lstat(target)
            self.assertEqual((pin['dev'], pin['ino']), (info.st_dev, info.st_ino))
            self.assertIsNone(rb.upload_pin_problem(self.out, pin))
            self.assertEqual(sorted(p.name for p in target.iterdir()), sorted(CONFIG['view_order']))   # no temp file left
        for item in inputs:
            self.assertEqual(item['sha256'], rb.sha256_file(self.views / item['name']))
            self.assertEqual(item['size'], (self.views / item['name']).stat().st_size)
            self.assertFalse(os.access(second / item['name'], os.W_OK))
        # an entry that already has the name is never reused: the mkdir is exclusive
        with mock.patch.object(rb.os, 'urandom', lambda n: b'\x01' * n), mock.patch.object(rb.time, 'strftime', lambda *a: 'STAMP'):
            taken = self.out / rb.UPLOAD_DIR / f'STAMP-{os.getpid()}-{"01" * 6}'
            taken.mkdir()
            (taken / 'front.png').write_bytes(b'somebody else\'s')
            with self.assertRaisesRegex(rb.Stop, 'already exists and is never reused'):
                rb.snapshot_inputs(self.PLAN, self.out)
            self.assertEqual((taken / 'front.png').read_bytes(), b'somebody else\'s')

    def test_the_reviewers_case_a_folder_swapped_for_a_junction_before_the_replace_writes_nothing_there(self):
        """Last-check row 14: the checked upload directory is swapped for a junction to another directory that holds a
        front.png; the replacement must not be redirected there."""
        victim = self.victim()
        other = victim.parent
        (other / 'front.png').write_bytes(b'another directory\'s front.png')
        real_own, swapped = rb._own_destination, []

        def swap_then_check(target, name):
            result = real_own(target, name)
            if name == 'front.png' and len(swapped) == 2:                        # the 3rd look: right before the os.replace
                aside = Path(str(target) + '.aside')
                os.rename(target, aside)                                         # the attacker moves the checked folder away
                self.junction(other, target)                                     # ... and puts a junction in its place
            if name == 'front.png':
                swapped.append(name)
            return result
        session = FakeSession(self.out)
        # prior item 14: on Windows the held folder cannot be renamed at all (FinalLiveTests); this is the pin check behind
        # it, as it works where no hold exists
        with mock.patch.object(rb, '_own_destination', swap_then_check), mock.patch.object(rb, 'hold_directory', no_hold):
            with self.assertRaisesRegex(rb.Stop, 'not a plain folder|was replaced'):
                rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: session)
        self.assertEqual(len(swapped), 3)
        self.assertEqual((other / 'front.png').read_bytes(), b'another directory\'s front.png')   # not replaced
        self.assertEqual(sorted(p.name for p in other.iterdir()), ['front.png', 'victim.png'])       # nothing written through it
        self.assertEqual(victim.read_bytes(), b'not a view: must survive')
        self.assertFalse((self.out / rb.RECORD).exists())                        # stopped before the record, the upload, any click
        self.assertFalse(session.calls)

    def test_a_folder_replaced_by_another_plain_folder_fails_the_pin(self):
        target, _, pin = rb.snapshot_inputs(self.PLAN, self.out)
        self.assertIsNone(rb.upload_pin_problem(self.out, pin))
        os.rename(target, Path(str(target) + '.aside'))
        target.mkdir()                                                           # same name, another directory
        self.assertIn('was replaced', rb.upload_pin_problem(self.out, pin))
        os.rmdir(target)
        self.assertIn('is gone', rb.upload_pin_problem(self.out, pin))
        for bad in (None, {}, {'name': '../x', 'dev': 1, 'ino': 1}, {'name': 'a', 'dev': True, 'ino': 1}, {'name': 'a', 'dev': 1}):
            self.assertIn('malformed', rb.upload_pin_problem(self.out, bad))

    def test_a_swap_after_the_snapshot_stops_before_the_upload_and_before_a_paid_click(self):
        victim = self.victim()
        swap = {}

        def do_swap():
            target = self.upload()
            aside = Path(str(target) + '.aside')
            os.rename(target, aside)
            copy = victim.parent / f'copy{len(swap)}'                            # identical bytes elsewhere: only the pin can tell
            shutil.copytree(aside, copy)
            self.junction(copy, target)
            swap.update(target=target, aside=aside)

        class Swapping(FakeSession):
            def open_workspace(self):
                do_swap()                                                        # after the snapshot, before the upload
                super().open_workspace()
        session = Swapping(self.out)
        with mock.patch.object(rb, 'hold_directory', no_hold):                   # the pin check behind the hold (see above)
            with self.assertRaisesRegex(rb.Stop, 'upload copies changed after they were hashed .*not a plain folder'):
                rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: session)
        self.assertFalse([c for c in session.calls if c[0] in ('prepare_multiview', 'click_free_preview')])
        # the same swap after a good submit: collect stops before the paid intent and click
        os.rmdir(swap['target'])
        self.submit()
        do_swap()
        self.assertIn('not a plain folder', rb.verify_upload_inputs(self.out)[0])
        session = FakeSession(self.out, stages=['geometry_preview'], costs=self.COSTS)
        with self.assertRaisesRegex(rb.Stop, 'upload inputs differ from the recorded submission before geometry'):
            self.collect(session)
        self.assertFalse([c for c in session.calls if c[0] == 'click'])
        self.assertEqual(list(self.record()['actions']), ['free_preview'])

    def test_an_upload_folder_that_is_a_link_or_junction_is_refused(self):
        victim = self.victim()
        self.out.mkdir(parents=True)
        self.junction(victim.parent, self.out / rb.UPLOAD_DIR)
        self.assert_refused(victim, 'not a plain folder of the output')
        self.assertEqual([p.name for p in victim.parent.iterdir()], ['victim.png'])   # nothing was written through the junction

    def test_destinations_inside_the_folder_are_never_written_through(self):
        """_replace_own / _own_destination (final-verify row 14) on a folder of the test's own: a hard link, a symbolic link, a
        folder and a non-bare name are refused; an own read-only file is replaced by a new file."""
        victim = self.victim()
        folder = self.root / 'folder'
        folder.mkdir()
        os.link(victim, folder / 'front.png')                                   # the same file under the destination name
        with self.assertRaisesRegex(rb.Stop, 'hard-linked to another file'):
            rb._replace_own(self.views / 'front.png', folder, 'front.png')
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ['front.png'])   # no temp file left
        (folder / 'side.png').mkdir()
        with self.assertRaisesRegex(rb.Stop, 'not a regular file'):
            rb._replace_own(self.views / 'front.png', folder, 'side.png')
        for name in ('../escaped.png', 'sub/front.png', '..', ''):
            with self.assertRaisesRegex(rb.Stop, 'not a plain file name'):
                rb._replace_own(self.views / 'front.png', folder, name)
        self.assertFalse((self.root / 'escaped.png').exists())
        try:
            os.symlink(victim, folder / 'back.png')
        except (OSError, NotImplementedError):
            pass                                                                 # no symlink privilege here: the other cases ran
        else:
            with self.assertRaisesRegex(rb.Stop, 'is a link, junction or not a regular file'):
                rb._replace_own(self.views / 'back.png', folder, 'back.png')
            self.assertTrue(os.path.islink(folder / 'back.png'))
        self.assertEqual(victim.read_bytes(), b'not a view: must survive')
        self.assertFalse(os.access(victim, os.W_OK))
        old = folder / 'own.png'
        old.write_bytes(b'an earlier snapshot')
        os.chmod(old, 0o444)
        before = os.stat(old)
        calls = []
        digest, size = rb._replace_own(self.views / 'front.png', folder, 'own.png', lambda: calls.append(1))
        after = os.stat(old)
        self.assertEqual(len(calls), 2)                                         # the pin: before the temp file and before the swap
        self.assertNotEqual((before.st_ino, before.st_size), (after.st_ino, after.st_size))   # a new file took the name
        self.assertEqual((digest, size), (rb.sha256_file(self.views / 'front.png'), (self.views / 'front.png').stat().st_size))
        self.assertFalse(os.access(old, os.W_OK))

        def refuse():
            raise rb.Stop('pin lost')
        with self.assertRaisesRegex(rb.Stop, 'pin lost'):
            rb._replace_own(self.views / 'back.png', folder, 'new.png', refuse)
        self.assertFalse((folder / 'new.png').exists())
        self.assertFalse([p for p in folder.iterdir() if p.name.endswith('.tmp')])

    def test_a_record_from_before_the_pin_keeps_the_flat_folder(self):
        self.submit()
        path = self.out / rb.RECORD
        data = json.loads(path.read_text())
        flat = self.out / rb.UPLOAD_DIR
        for name in CONFIG['view_order']:
            shutil.copyfile(self.upload() / name, flat / name)
        del data['upload_dir_id']
        data['upload_dir'] = str(flat.resolve())
        path.write_text(json.dumps(data))
        self.assertEqual(rb.verify_upload_inputs(self.out), [])
        self.assertEqual(rb.verify_upload_inputs(self.record()), [])

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
        self.submit()                                                            # a re-run snapshots into a new folder
        self.assertEqual((self.record()['status'], rb.verify_upload_inputs(self.out)), ('submitted', []))
        self.assertEqual(len(list((self.out / rb.UPLOAD_DIR).iterdir())), 2)

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


class FinalLiveTests(unittest.TestCase):
    """review_v2_final_live_20261126: prior item 14 (the pinned upload folder is held against renaming), "interrupted paid
    export cannot resume" and "a rejected upload strands the unpaid variant"."""

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

    def asides(self):
        return sorted(self.out.glob('studio_submission.not_submitted_*.json'))

    def upload(self):
        return self.out / rb.UPLOAD_DIR / self.record()['upload_dir_id']['name']

    # -- prior item 14
    @unittest.skipUnless(os.name == 'nt', 'the share-mode hold exists on Windows only')
    def test_item14_the_pinned_folder_cannot_be_renamed_during_the_writes_the_upload_or_the_click(self):
        """The reviewer's trace: the upload directory moved between its pin check and the replacement. With the handle held
        (no FILE_SHARE_DELETE) the move itself is refused, at every point of the submit."""
        refused = []

        def try_swap(where):
            folders = [p for p in (self.out / rb.UPLOAD_DIR).iterdir() if p.is_dir()]
            self.assertEqual(len(folders), 1)
            for entry in (folders[0], self.out / rb.UPLOAD_DIR):               # the folder, and its container above it
                with self.assertRaises(PermissionError, msg=f'{where}: {entry}'):
                    os.rename(entry, Path(str(entry) + '.aside'))
                self.assertTrue(entry.is_dir())
            with self.assertRaises(OSError, msg=where):
                os.rmdir(folders[0])
            refused.append(where)
        real_own = rb._own_destination

        def own(target, name):
            result = real_own(target, name)
            if Path(target).parent.name == rb.UPLOAD_DIR:
                try_swap(f'write {name}')                                        # between the look and the temp file / os.replace
            return result

        class Session(FakeSession):
            def open_workspace(self):
                try_swap('before the upload')
                super().open_workspace()

            def prepare_multiview(self, paths, config):
                try_swap('during the upload')
                for _, path in paths:                                            # the browser can still read the copies
                    assert Path(path).read_bytes()
                return super().prepare_multiview(paths, config)

            def click_free_preview(self):
                try_swap('at the click')
                super().click_free_preview()
        session = Session(self.out)
        with mock.patch.object(rb, '_own_destination', own):
            rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: session)
        self.assertEqual(self.record()['status'], 'submitted')
        self.assertGreaterEqual(sum(w.startswith('write ') for w in refused), 3 * len(CONFIG['view_order']))
        for where in ('before the upload', 'during the upload', 'at the click'):
            self.assertIn(where, refused)
        self.assertEqual(rb.verify_upload_inputs(self.out), [])
        # the hold ends with the submit: the folder can be moved again (and the pin then catches it, as before)
        folder = self.upload()
        os.rename(folder, Path(str(folder) + '.aside'))
        self.assertIn('is gone', rb.verify_upload_inputs(self.out)[0])
        os.rename(Path(str(folder) + '.aside'), folder)

    @unittest.skipUnless(os.name == 'nt', 'the share-mode hold exists on Windows only')
    def test_item14_hold_directory_holds_the_entry_itself_and_stops_when_it_cannot(self):
        folder = self.root / 'held'
        folder.mkdir()
        with rb.hold_directory(folder) as held:
            self.assertTrue(held)
            with self.assertRaises(PermissionError):
                os.rename(folder, self.root / 'moved')
            (folder / 'a.txt').write_text('x')                                   # files inside are not affected
            os.replace(folder / 'a.txt', folder / 'b.txt')
            (folder / 'b.txt').unlink()
        os.rename(folder, self.root / 'moved')                                   # released
        with self.assertRaisesRegex(rb.Stop, 'could not be held open'):
            with rb.hold_directory(self.root / 'missing'):
                self.fail('held a folder that does not exist')
        snapshot, _, pin = rb.snapshot_inputs({'views_dir': self.views, 'images': list(CONFIG['view_order']),
                                               'uploaded': list(CONFIG['view_order'])}, self.out)
        os.rename(snapshot, Path(str(snapshot) + '.aside'))                      # without a caller's stack the hold ended
        self.assertIn('is gone', rb.upload_pin_problem(self.out, pin))

    # -- interrupted paid export
    def export_session(self, archive, **kw):
        test = self

        class Exporting(FakeSession):
            def export_glb(self, out_dir, config):                               # as SeleniumRodin.export_glb places a download
                self.calls.append(('export_glb',))
                target, self.export_adopted = rb.place_glb(archive, Path(out_dir))
                test.assertFalse(list(Path(out_dir).glob(rb.EXPORT_STAGING + '*')))
                return target, 'panel .glb 2K'
        return Exporting(self.out, **kw)

    def archive(self, name='dl.zip', **glb):
        make_glb(self.root / 'inner.glb', **glb)
        path = self.root / name
        with zipfile.ZipFile(path, 'w') as z:
            z.write(self.root / 'inner.glb', 'model/base_basic_pbr.glb')
        return path

    def interrupted_after_extraction(self):
        """A paid collect (0.5-credit geometry confirm) interrupted after the ZIP was extracted and before the export receipt."""
        rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out))
        session = self.export_session(self.archive(), stages=['geometry_preview', 'export_ready'],
                                      costs={'geometry': 0.5, 'material_generate': None, 'material_confirm': None})
        with mock.patch.object(rb, '_record_export', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                rb.collect(self.out, config=CONFIG, allow_paid=True, accept_preview=True, session_factory=lambda: session)
        self.assertIn(('click', 'geometry'), session.calls)
        self.assertNotIn('exports', self.record())                               # no receipt
        target = self.out / 'glb_base_basic_pbr.glb'
        self.assertTrue(target.is_file())
        return target

    def test_the_reviewers_case_an_interrupted_paid_export_resumes_by_adopting_the_identical_glb(self):
        target = self.interrupted_after_extraction()
        digest = rb.sha256_file(target)
        for attempt in range(2):                                                 # "both retries hit FileExistsError"
            session = self.export_session(self.archive(f'again{attempt}.zip'), stages=['export_ready'])
            data = rb.collect(self.out, config=CONFIG, session_factory=lambda: session)
            self.assertFalse([c for c in session.calls if c[0] == 'click'])      # nothing is paid again
            self.assertEqual(data['status'], 'exported')
            receipt = data['exports']['GLB']
            self.assertEqual((receipt['path'], receipt['sha256'], receipt['bytes']), (str(target.resolve()), digest, target.stat().st_size))
            self.assertTrue(receipt['adopted_existing'])
            self.assertEqual(receipt['inspection']['problems'], [])
            if attempt:
                self.assertFalse(session.calls)                                  # the receipt exists: no browser at all
        self.assertEqual(sorted(p.name for p in self.out.glob('glb_*')), ['glb_base_basic_pbr.glb'])

    def test_a_different_glb_in_place_stops_the_resumed_export_for_review(self):
        target = self.interrupted_after_extraction()
        kept = target.read_bytes()
        for other in (self.archive('other.zip', triangles=77),                   # same size class, other bytes
                      self.archive('longer.zip', triangles=123456789)):          # another size
            session = self.export_session(other, stages=['export_ready'])
            with self.assertRaisesRegex(rb.Stop, 'Export destination exists with DIFFERENT content .*needs review'):
                rb.collect(self.out, config=CONFIG, session_factory=lambda: session)
            self.assertEqual(target.read_bytes(), kept)
            self.assertNotIn('exports', self.record())
            self.assertFalse(list(self.out.glob(rb.EXPORT_STAGING + '*')))
        # a folder under the name is not a file to adopt
        blocked = self.root / 'blocked'
        blocked.mkdir()
        (blocked / 'glb_base_basic_pbr.glb').mkdir()
        with self.assertRaisesRegex(rb.Stop, 'not a plain file'):
            rb.place_glb(self.archive('third.zip'), blocked)

    def test_export_staging_and_the_plain_glb_download(self):
        out = self.root / 'plain'
        (out / 'downloads').mkdir(parents=True)
        (out / (rb.EXPORT_STAGING + '999_dead')).mkdir()                         # left by a killed run
        (out / (rb.EXPORT_STAGING + '999_dead') / 'glb_half.glb').write_bytes(b'half')
        make_glb(out / 'downloads' / 'model.glb')
        self.assertEqual(rb.place_glb(out / 'downloads' / 'model.glb', out), (out.resolve() / 'glb_model.glb', False))
        self.assertFalse(list(out.glob(rb.EXPORT_STAGING + '*')))
        self.assertEqual(rb.place_glb(out / 'downloads' / 'model.glb', out), (out.resolve() / 'glb_model.glb', True))
        make_glb(out / 'downloads' / 'model.glb', triangles=5)
        os.unlink(out / 'glb_model.glb')                                         # (the download was a hard link of it)
        make_glb(out / 'glb_model.glb', triangles=6)
        with self.assertRaisesRegex(rb.Stop, 'DIFFERENT content'):
            rb.place_glb(out / 'downloads' / 'model.glb', out)
        # no hard links available: a staged copy is renamed into place
        other = self.root / 'nolink'
        other.mkdir()
        with mock.patch.object(rb.os, 'link', side_effect=OSError('no hard links')):
            target, adopted = rb.place_glb(self.archive('nolink.zip'), other)
            self.assertEqual((adopted, rb.inspect_glb(target)['problems']), (False, []))
            self.assertEqual(rb.place_glb(self.archive('nolink.zip'), other), (target, True))
        self.assertFalse(list(other.glob(rb.EXPORT_STAGING + '*')))
        # a truncated archive never reaches the final name
        broken = self.root / 'broken.zip'
        data = bytearray(self.archive('whole.zip').read_bytes())
        data[40] ^= 0xFF
        broken.write_bytes(bytes(data))
        empty = self.root / 'empty'
        empty.mkdir()
        with self.assertRaises((ValueError, zipfile.BadZipFile)):
            rb.place_glb(broken, empty)
        self.assertEqual([p.name for p in empty.iterdir() if p.name != rb.RECORD_LOCK], [])

    # -- a rejected upload
    def test_the_reviewers_case_a_rejected_upload_archives_the_preparing_record_and_the_retry_submits_fresh(self):
        class Rejected(FakeSession):
            def prepare_multiview(self, paths, config):
                self.calls.append(('prepare_multiview', [n for n, _ in paths]))
                raise rb.Stop(f'Expected {len(paths)} uploaded thumbnails, found {len(paths) - 1}')
        session = Rejected(self.out)
        with self.assertRaisesRegex(rb.Stop, 'Expected 4 uploaded thumbnails, found 3; nothing was generated, record moved to '
                                             'studio_submission.not_submitted_.*a re-run submits again'):
            rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: session)
        self.assertNotIn(('click_free_preview',), session.calls)
        self.assertFalse((self.out / rb.RECORD).exists())                        # no stranded 'preparing' record
        aside = json.loads(self.asides()[0].read_text())
        self.assertEqual((aside['status'], aside['actions'], aside['model_url'], aside['pending_action']),
                         ('not_submitted', {}, None, None))
        self.assertIn('thumbnails', aside['why'])
        with self.assertRaises(rb.Stop):                                         # twice in one second: neither aside is overwritten
            rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: Rejected(self.out))
        self.assertEqual(len(self.asides()), 2)
        rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out))   # the retry
        self.assertEqual(self.record()['status'], 'submitted')
        self.assertEqual(len(self.asides()), 2)

    # -- confirm review (.codex_jobs/review_v2_confirm_20261126/result.md, "Pilot, rejected upload")
    def interrupted_archive(self):
        """The reviewer's case: a thumbnail rejection, and the archive is interrupted between its status write and its rename."""
        class Rejected(FakeSession):
            def prepare_multiview(self, paths, config):
                self.calls.append(('prepare_multiview', [n for n, _ in paths]))
                raise rb.Stop(f'Expected {len(paths)} uploaded thumbnails, found {len(paths) - 1}')
        session = Rejected(self.out)
        with mock.patch.object(rb, '_archive_fault', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: session)
        self.assertNotIn(('click_free_preview',), session.calls)
        stranded = self.record()
        self.assertEqual((stranded['status'], stranded['actions'], stranded['pending_action'], self.asides()),
                         ('not_submitted', {}, None, []))
        self.assertIn('thumbnails', stranded['why'])
        return stranded

    def test_an_archive_interrupted_before_its_rename_is_resumed_by_the_next_submit(self):
        stranded = self.interrupted_archive()
        self.assertTrue(rb.proven_pre_click(stranded))                           # still proves that nothing was clicked
        session = FakeSession(self.out)
        rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: session)   # the retry
        self.assertIn(('click_free_preview',), session.calls)
        self.assertEqual(self.record()['status'], 'submitted')
        aside = [json.loads(p.read_text()) for p in self.asides()]
        self.assertEqual(len(aside), 1)
        self.assertEqual((aside[0]['status'], aside[0]['why'], aside[0]['actions']), ('not_submitted', stranded['why'], {}))
        self.assertIn('stopped before the Free Preview click', aside[0]['archive_resumed']['why'])

    def test_set_aside_pre_click_resumes_a_pending_archive_and_is_interruptible_again(self):
        stranded = self.interrupted_archive()
        with mock.patch.object(rb, '_archive_fault', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):                           # interrupted a second time: still resumable
                rb.set_aside_pre_click(self.out, 'again')
        self.assertTrue(rb.proven_pre_click(self.record()))
        aside = rb.set_aside_pre_click(self.out, 'resumed by the caller')
        self.assertEqual(self.asides(), [aside])
        self.assertFalse((self.out / rb.RECORD).exists())
        data = json.loads(aside.read_text())
        self.assertEqual((data['status'], data['why'], data['archive_resumed']['why']),
                         ('not_submitted', stranded['why'], 'resumed by the caller'))

    def test_a_not_submitted_record_with_any_click_trace_is_not_resumed(self):
        """not_submitted after a REFUSED click (Insufficient Balance) keeps its free intent: never taken for a pending archive."""
        base = self.interrupted_archive()
        self.assertTrue(rb.proven_pre_click(base))
        for change in ({'actions': {'free_preview': {'intent_at': 'x'}}}, {'pending_action': 'free_preview'}, {'model_url': URL},
                       {'model_id': MODEL}, {'project_id': MODEL}, {'submit_clicked_at': 'x'}, {'after_submit_at': 'x'},
                       {'exports': {'GLB': {}}}, {'credits_committed': 0.5}, {'backend': None}):
            data = {**base, **change}
            self.assertFalse(rb.proven_pre_click(data), change)
            (self.out / rb.RECORD).write_text(json.dumps(data))
            self.assertIsNone(rb.set_aside_pre_click(self.out, 'x'), change)
            with self.assertRaisesRegex(rb.Stop, 'Submission record already exists'):
                rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: self.fail('opened'))
            self.assertEqual((self.record(), self.asides()), (data, []), change)

    def test_a_submit_killed_before_its_click_leaves_a_record_the_next_submit_archives(self):
        for where in ('prepare_multiview', 'wait_balance'):                      # status preparing / ready_to_submit
            class Killed(FakeSession):
                pass
            setattr(Killed, where, lambda self, *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
            with self.assertRaises(KeyboardInterrupt):
                rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: Killed(self.out))
            stranded = self.record()
            self.assertIn(stranded['status'], rb.PRE_CLICK_STATUSES)
            self.assertTrue(rb.proven_pre_click(stranded))
            before = len(self.asides())
            with self.assertRaisesRegex(rb.Stop, 'record status'):               # what the reviewer saw: collect refuses it
                rb.collect(self.out, config=CONFIG, session_factory=lambda: self.fail('opened'))
            session = FakeSession(self.out)
            rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: session)
            self.assertIn(('click_free_preview',), session.calls)
            self.assertEqual(self.record()['status'], 'submitted')
            self.assertEqual(len(self.asides()), before + 1)
            self.assertIn('stopped before the Free Preview click', json.loads(self.asides()[-1].read_text())['why'])
            shutil.rmtree(self.out, onerror=lambda f, p, _: (os.chmod(p, 0o666), f(p)))

    def test_a_record_with_any_click_intent_is_never_archived(self):
        """The free intent is written before the click: once it exists the click may have happened."""
        class ClickDies(FakeSession):
            def click_free_preview(self):
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: ClickDies(self.out))
        pending = self.record()
        self.assertEqual((pending['status'], pending['pending_action']), ('ready_to_submit', 'free_preview'))
        self.assertFalse(rb.proven_pre_click(pending))
        self.assertIsNone(rb.set_aside_pre_click(self.out, 'x'))
        with self.assertRaisesRegex(rb.Stop, 'Submission record already exists'):
            rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: self.fail('opened'))
        self.assertEqual((self.record(), self.asides()), (pending, []))
        base = {'backend': 'hyper3d', 'status': 'preparing', 'actions': {}, 'pending_action': None, 'model_url': None,
                'model_id': None, 'project_id': None}
        self.assertTrue(rb.proven_pre_click(base))
        for change in ({'actions': {'free_preview': {'intent_at': 'x'}}}, {'actions': {'geometry': {}}},
                       {'pending_action': 'free_preview'}, {'model_url': URL}, {'model_id': MODEL}, {'project_id': MODEL},
                       {'submit_clicked_at': 'x'}, {'status': 'submission_attempted'}, {'status': 'submission_needs_review'},
                       {'status': 'submitted'}, {'status': 'dry_run'}, {'status': 'exported'}, {'backend': None},
                       {'exports': {'GLB': {}}}, {'credits_committed': 0.5}):
            self.assertFalse(rb.proven_pre_click({**base, **change}), change)
        self.assertFalse(rb.proven_pre_click(None))

    def test_set_aside_pre_click_archives_only_under_the_output_lock(self):
        self.assertIsNone(rb.set_aside_pre_click(self.out, 'x'))                 # no folder, no record
        self.out.mkdir()
        rb.Record.create(self.out / rb.RECORD, {'backend': 'hyper3d', 'status': 'preparing', 'actions': {},
                                                'pending_action': None, 'model_url': None})
        held, release = threading.Event(), threading.Event()

        def holder():
            with rb.output_lock(self.out):
                held.set()
                release.wait(10)
        thread = threading.Thread(target=holder)
        thread.start()
        try:
            self.assertTrue(held.wait(10))
            with self.assertRaisesRegex(rb.Stop, rb.OUTPUT_BUSY):                # another command is handling this output
                rb.set_aside_pre_click(self.out, 'x')
            self.assertTrue((self.out / rb.RECORD).exists())
        finally:
            release.set()
            thread.join()
        aside = rb.set_aside_pre_click(self.out, 'the upload was rejected')
        self.assertEqual((aside.parent, json.loads(aside.read_text())['why']), (self.out, 'the upload was rejected'))
        self.assertFalse((self.out / rb.RECORD).exists())
        (self.out / rb.RECORD).write_text(json.dumps({'status': 'preparing', 'project_id': None}))   # Tripo's studio.mjs record
        self.assertIsNone(rb.set_aside_pre_click(self.out, 'x'))
        (self.out / rb.RECORD).write_text('{not json')
        self.assertIsNone(rb.set_aside_pre_click(self.out, 'x'))


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
