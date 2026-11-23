"""Offline tests for formy3d_backend.py, formy3d_cookies.py and the convert_job.py formy3d dispatch (browser faked)."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

import formy3d_backend as fb
import formy3d_cookies

HYPER3D = Path(__file__).resolve().parents[1] / 'hyper3d_pipeline'
TRIPO = Path(__file__).resolve().parents[1] / 'tripo_pipeline'
sys.path.insert(0, str(TRIPO))
import convert_job  # noqa: E402
sys.path.insert(0, str(HYPER3D))
from test_rodin_backend import make_glb, png  # noqa: E402  (shared fixtures)

CONFIG = fb.load_config()
UUID = 'cb0e93e1-b2a8-474e-a5ce-24ab4b08f38f'
GLB_URL = 'https://v3b.fal.media/files/b/0a9e9161/WBl1DOQneR2X77WtrSTT__model.glb'


def make_views(folder, **bad):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name in CONFIG['view_order']:
        png(folder / name, **bad.get(name, {}))
    return folder


class FakeSession:
    """Stands in for PlaywrightFormy3d."""
    log = []

    def __init__(self, out, cost=167, balance=1000, outcome=None, generation=None, intent_check=None, glb=True):
        self.out, self.cost, self.balance = Path(out), cost, balance
        self.outcome = outcome if outcome is not None else {'code': 0, 'request_id': 'req', 'generation_uuid': UUID,
                                                            'queue_position': 0, 'request_summary': {'pbr': False}}
        self.generation, self.intent_check, self.glb = generation, intent_check, glb
        FakeSession.log = self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def screenshot(self, label):
        self.calls.append(('screenshot', label))

    def require_signed_in(self):
        self.calls.append(('require_signed_in',))

    def read_balance(self):
        return self.balance

    def open_generator(self):
        self.calls.append(('open_generator',))

    def model_chip_text(self):
        return 'TRIPO3D H3.1 New'

    def prepare_form(self, paths, slots, config):
        self.calls.append(('prepare_form', [(n, slots[n]) for n, _ in paths]))
        return {'slots': slots}

    def read_cost(self):
        return self.cost

    def click_generate(self, config):
        if self.intent_check:
            self.intent_check()
        self.calls.append(('click_generate',))
        return self.outcome

    def wait_generation(self, generation_uuid, timeout_s, interval_s):
        self.calls.append(('wait_generation', generation_uuid))
        return self.generation

    def find_generation(self, generation_uuid):
        return self.generation

    def download_glb(self, url, generation_uuid):
        self.calls.append(('download_glb', url))
        target = self.out / f'glb_{generation_uuid}.glb'
        make_glb(target) if self.glb is True else target.write_bytes(b'junk')
        return target


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_slot_mapping_matches_studio_order(self):
        plan = fb.validate_views(make_views(self.root / 'v'), CONFIG)
        self.assertEqual(plan['slots'], {'front.png': 'Front View', 'front_three_quarter.png': 'Left View',
                                         'back_three_quarter.png': 'Right View', 'back.png': 'Back View'})
        self.assertEqual(plan['uploaded'], ['front.png', 'front_three_quarter.png', 'back_three_quarter.png', 'back.png'])

    def test_three_view_module_leaves_the_omitted_slot_empty(self):
        folder = make_views(self.root / 'v')
        (folder / 'front_three_quarter.png').unlink()
        (folder / 'views_omitted.json').write_text(json.dumps({'omitted': ['front_three_quarter'], 'reason': 'test'}))
        plan = fb.validate_views(folder, CONFIG)
        self.assertEqual(plan['slots'], {'front.png': 'Front View', 'back_three_quarter.png': 'Right View',
                                         'back.png': 'Back View'})
        self.assertEqual(plan['views_omitted'], ['front_three_quarter'])
        (folder / 'views_omitted.json').write_text(json.dumps({'omitted': ['front']}))
        with self.assertRaisesRegex(ValueError, 'front is always required'):
            fb.validate_views(folder, CONFIG)

    def test_bad_views(self):
        with self.assertRaisesRegex(ValueError, 'different native dimensions'):
            fb.validate_views(make_views(self.root / 'a', **{'back.png': {'w': 9}}), CONFIG)
        with self.assertRaisesRegex(ValueError, '8-bit RGBA'):
            fb.validate_views(make_views(self.root / 'b', **{'front.png': {'color': 2}}), CONFIG)
        folder = make_views(self.root / 'c')
        (folder / 'back.png').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing back.png'):
            fb.validate_views(folder, CONFIG)

    def test_single_view_fallback(self):
        plan = fb.validate_views(make_views(self.root / 'v'), {**CONFIG, 'multi_view': False})
        self.assertEqual(plan['slots'], {'front.png': 'Front View'})


class HelperTests(unittest.TestCase):
    def test_parse_generate_cost(self):
        self.assertEqual(fb.parse_generate_cost('Generate 3D Model\n- 167'), 167)
        self.assertEqual(fb.parse_generate_cost('Generate 3D Model - 67 '), 67)
        self.assertEqual(fb.parse_generate_cost('Generate 3D Model - 0.5'), 0.5)
        self.assertIsNone(fb.parse_generate_cost('Generate 3D Model'))
        self.assertIsNone(fb.parse_generate_cost(''))

    def test_label_of(self):
        self.assertEqual(fb.label_of('\nPBR\nEnable PBR materials.'), 'PBR')
        self.assertEqual(fb.label_of(''), '')

    def test_pick_glb(self):
        urls = [GLB_URL, 'https://x/y_preview.png']
        self.assertEqual(fb.pick_glb(urls, CONFIG), GLB_URL)
        self.assertEqual(fb.pick_glb(json.dumps(urls), CONFIG), GLB_URL)
        both = ['https://x/a_base.glb?sig=1', 'https://x/a_model.glb?sig=2']
        self.assertTrue(fb.pick_glb(both, CONFIG).endswith('a_model.glb?sig=2'))
        with self.assertRaises(fb.Stop):
            fb.pick_glb(['https://x/y_preview.png'], CONFIG)
        with self.assertRaises(fb.Stop):
            fb.pick_glb('[]', CONFIG)

    def test_check_request(self):
        good = {'model': 'fal/tripo3d/h3.1/multiview-to-3d', 'pbr': False, 'quad': False, 'texture': True,
                'texture_quality': 'hd', 'geometry_quality': 'detailed', 'image': 'data:image/png;base64,AA',
                'left_image': 'data:image/png;base64,AA'}
        self.assertEqual(fb.check_request(good, CONFIG), [])
        self.assertTrue(fb.check_request({**good, 'pbr': True}, CONFIG))
        self.assertTrue(fb.check_request({**good, 'quad': True}, CONFIG))
        self.assertTrue(fb.check_request({**good, 'geometry_quality': 'standard'}, CONFIG))
        self.assertTrue(fb.check_request({**good, 'model': 'tripo3d/v2.5/image-to-3d'}, CONFIG))
        self.assertTrue(fb.check_request({**good, 'texture': False}, CONFIG))
        self.assertTrue(fb.check_request({'model': 'fal/tripo3d/h3.1/image-to-3d', 'pbr': False}, CONFIG))
        self.assertTrue(fb.check_request('nope', CONFIG))

    def test_summarize_request_hides_images(self):
        summary = fb.summarize_request({'image': 'data:image/png;base64,' + 'A' * 500, 'pbr': False, 'x': [1]})
        self.assertEqual(summary['image'], {'data_url_prefix': 'data:image/png;base64,AAAAAAAA', 'chars': 522})
        self.assertEqual((summary['pbr'], summary['x']), (False, '<list>'))

    def test_config_choices(self):
        self.assertEqual((CONFIG['texture'], CONFIG['geometry_quality'], CONFIG['pbr'], CONFIG['quad']),
                         ('HD', 'Detailed', False, False))
        self.assertEqual(CONFIG['expected_costs']['generate'], 167)
        self.assertLessEqual(CONFIG['expected_costs']['generate'], CONFIG['budget_credits'])


class SubmitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.views = make_views(self.root / 'v')
        self.out = self.root / 'out'

    def run_submit(self, **kw):
        session = kw.pop('session', None) or FakeSession(self.out)
        return fb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: session, **kw), session

    def test_dry_run_never_clicks(self):
        record, session = self.run_submit(dry_run=True)
        self.assertEqual(record['status'], 'dry_run')
        self.assertEqual(record['displayed_cost_credits'], 167)
        self.assertNotIn(('click_generate',), session.calls)
        self.assertIn(('screenshot', 'before_submit'), session.calls)
        self.assertEqual(record['backend'], 'formy3d')

    def test_three_view_dry_run_uploads_only_present_slots_and_records_omission(self):
        (self.views / 'back.png').unlink()
        (self.views / 'views_omitted.json').write_text(json.dumps({'omitted': ['back'], 'reason': 'test'}))
        record, session = self.run_submit(dry_run=True)
        form = next(c for c in session.calls if c[0] == 'prepare_form')
        self.assertEqual(form[1], [('front.png', 'Front View'), ('front_three_quarter.png', 'Left View'),
                                   ('back_three_quarter.png', 'Right View')])
        self.assertEqual((record['views_omitted'], record['images_uploaded']),
                         (['back'], ['front.png', 'front_three_quarter.png', 'back_three_quarter.png']))

    def test_requires_allow_paid_and_leaves_no_record(self):
        session = FakeSession(self.out)
        with self.assertRaisesRegex(fb.Stop, '--allow-paid'):
            self.run_submit(session=session)
        self.assertNotIn(('click_generate',), session.calls)
        self.assertFalse((self.out / 'studio_submission.json').exists())

    def test_cost_must_match(self):
        for shown in (100, 168, None):
            session = FakeSession(self.out, cost=shown)
            with self.assertRaises(fb.Stop):
                self.run_submit(session=session, allow_paid=True)
            self.assertNotIn(('click_generate',), session.calls)
            self.assertFalse((self.out / 'studio_submission.json').exists())

    def test_expect_cost_override(self):
        record, _ = self.run_submit(session=FakeSession(self.out, cost=100), allow_paid=True, expect_cost=100)
        self.assertEqual(record['status'], 'submitted')

    def test_budget_and_balance(self):
        with self.assertRaisesRegex(fb.Stop, 'budget_credits'):
            self.run_submit(session=FakeSession(self.out, cost=300), allow_paid=True, expect_cost=300)
        with self.assertRaisesRegex(fb.Stop, 'balance'):
            self.run_submit(session=FakeSession(self.out, balance=50), allow_paid=True)

    def test_happy_path_records_intent_before_click_and_the_id(self):
        record_path = self.out / 'studio_submission.json'
        seen = {}

        def intent_check():
            data = json.loads(record_path.read_text())
            seen['pending'], seen['actions'] = data['pending_action'], list(data['actions'])
        record, session = self.run_submit(session=FakeSession(self.out, intent_check=intent_check), allow_paid=True)
        self.assertEqual(seen, {'pending': 'generate', 'actions': ['generate']})
        self.assertEqual((record['status'], record['project_id'], record['generation_uuid'], record['request_id']),
                         ('submitted', UUID, UUID, 'req'))
        saved = json.loads(record_path.read_text())
        self.assertEqual((saved['backend'], saved['status'], saved['credits_committed'], saved['pending_action']),
                         ('formy3d', 'submitted', 167, None))
        self.assertEqual(session.calls.count(('click_generate',)), 1)

    def test_existing_record_is_never_resubmitted(self):
        self.run_submit(allow_paid=True)
        session = FakeSession(self.out)
        with self.assertRaisesRegex(fb.Stop, 'already exists'):
            self.run_submit(session=session, allow_paid=True)
        self.assertEqual(session.calls, [])

    def test_no_id_means_needs_review_and_never_repeats(self):
        session = FakeSession(self.out, outcome={'code': None, 'error': 'timeout'})
        with self.assertRaisesRegex(fb.Stop, 'bind'):
            self.run_submit(session=session, allow_paid=True)
        saved = json.loads((self.out / 'studio_submission.json').read_text())
        self.assertEqual((saved['status'], saved['pending_action']), ('submission_needs_review', 'generate'))
        with self.assertRaises(fb.Stop):                      # record exists: no second click possible
            self.run_submit(session=FakeSession(self.out), allow_paid=True)

    def test_bind_recovers(self):
        session = FakeSession(self.out, outcome={'code': None, 'error': 'timeout'})
        with self.assertRaises(fb.Stop):
            self.run_submit(session=session, allow_paid=True)
        record = fb.FormyRecord(self.out / 'studio_submission.json')
        record.bind_generation(UUID.upper())
        self.assertEqual((record.data['generation_uuid'], record.data['project_id']), (UUID, UUID))
        with self.assertRaises(ValueError):
            record.bind_generation('not-a-uuid')
        with self.assertRaises(ValueError):
            record.bind_generation('0' * 8 + UUID[8:-1] + '1')


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / 'out'
        views = make_views(Path(self.temp.name) / 'v')
        fb.submit(views, self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out), allow_paid=True)
        self.done = {'uuid': UUID, 'status': 'completed', 'model': 'tripo3d/h3.1/multiview-to-3d',
                     'credits_consumed': '167', 'result_urls': json.dumps([GLB_URL, 'https://x/p_preview.png'])}

    def test_collect_exports_glb(self):
        session = FakeSession(self.out, generation=self.done)
        record = fb.collect(self.out, config=CONFIG, session_factory=lambda: session, timeout_s=5)
        self.assertEqual(record['status'], 'exported')
        glb = record['exports']['GLB']
        self.assertTrue(Path(glb['path']).is_file())
        self.assertEqual(glb['inspection']['problems'], [])
        self.assertIn(('download_glb', GLB_URL), session.calls)
        self.assertEqual(record['studio_result']['topology'], 'triangle')
        self.assertNotIn('cost_mismatch', record)

    def test_second_collect_does_not_open_a_browser(self):
        fb.collect(self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out, generation=self.done))

        def boom():
            raise AssertionError('browser must not start')
        record = fb.collect(self.out, config=CONFIG, session_factory=boom)
        self.assertEqual(record['status'], 'exported')

    def test_cost_mismatch_is_noted(self):
        session = FakeSession(self.out, generation={**self.done, 'credits_consumed': '134'})
        record = fb.collect(self.out, config=CONFIG, session_factory=lambda: session)
        self.assertEqual(record['cost_mismatch'], {'displayed': 167, 'consumed': '134'})

    def test_bad_glb_needs_review(self):
        session = FakeSession(self.out, generation=self.done, glb=False)
        with self.assertRaises(ValueError):
            fb.collect(self.out, config=CONFIG, session_factory=lambda: session)

    def test_rejects_foreign_or_unsubmitted_records(self):
        path = self.out / 'studio_submission.json'
        data = json.loads(path.read_text())
        path.write_text(json.dumps({**data, 'backend': 'hyper3d'}))
        with self.assertRaisesRegex(fb.Stop, 'not a formy3d record'):
            fb.collect(self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out))
        path.write_text(json.dumps({**data, 'status': 'submission_needs_review'}))
        with self.assertRaisesRegex(fb.Stop, 'inspect'):
            fb.collect(self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out))
        path.write_text(json.dumps({**data, 'generation_uuid': None}))
        with self.assertRaisesRegex(fb.Stop, 'no generation id'):
            fb.collect(self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out))


class PollTests(unittest.TestCase):
    def test_wait_generation_stages(self):
        class Page(fb.PlaywrightFormy3d):
            def __init__(self, states):
                self.states = list(states)

            def find_generation(self, generation_uuid):
                state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
                return None if state is None else {'uuid': generation_uuid, 'status': state, 'error_message': 'boom'}
        self.assertEqual(Page([None, 'processing', 'completed']).wait_generation(UUID, 5, 0)['status'], 'completed')
        with self.assertRaisesRegex(fb.Stop, 'ended as'):
            Page(['processing', 'failed']).wait_generation(UUID, 5, 0)
        with self.assertRaises(TimeoutError):
            Page(['processing']).wait_generation(UUID, 0.05, 0.01)


class PreviewTests(unittest.TestCase):
    def test_preview_is_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            views = make_views(Path(temp) / 'v')
            out = Path(temp) / 'o'
            session = FakeSession(out)
            info = fb.preview(out, views_dir=views, config=CONFIG, session_factory=lambda: session)
            self.assertEqual((info['balance'], info['displayed_cost_credits']), (1000, 167))
            self.assertNotIn(('click_generate',), session.calls)
            self.assertFalse((out / 'studio_submission.json').exists())
            self.assertTrue((out / 'preview.json').is_file())


class CookieTests(unittest.TestCase):
    def test_only_formy3d_cookies_unexpired(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / 'profile'
            profile.mkdir()
            db = sqlite3.connect(profile / 'cookies.sqlite')
            db.execute('create table moz_cookies (name text, value text, host text, path text, expiry integer, '
                       'isSecure integer, isHttpOnly integer, sameSite integer)')
            future, past = int(time.time()) + 1000, int(time.time()) - 1000
            rows = [('__Secure-authjs.session-token', 'SECRET', 'formy3d.com', '/', future, 1, 1, 0),
                    ('g_state', 'x', '.formy3d.com', '/', future * 1000, 0, 0, 1),
                    ('old', 'x', 'formy3d.com', '/', past, 0, 0, 1),
                    ('other', 'x', 'example.com', '/', future, 0, 0, 1),
                    ('lookalike', 'x', 'notformy3d.com', '/', future, 0, 0, 1)]
            db.executemany('insert into moz_cookies values (?,?,?,?,?,?,?,?)', rows)
            db.commit()
            db.close()
            cookies = formy3d_cookies.firefox_cookies(profile)
            self.assertEqual(formy3d_cookies.cookie_names(cookies), ['__Secure-authjs.session-token', 'g_state'])
            self.assertEqual(next(c for c in cookies if c['name'].startswith('__Secure'))['sameSite'], 'None')
            self.assertNotIn('SECRET', ' '.join(formy3d_cookies.cookie_names(cookies)))
        with self.assertRaises(FileNotFoundError):
            formy3d_cookies.firefox_cookies(Path('/nonexistent/profile'))


class DispatchTests(unittest.TestCase):
    def test_commands(self):
        out, views = Path('/o'), Path('/v')
        with mock.patch.dict(os.environ, {'FORMY3D_VENV': '/venv'}):
            cmd = convert_job.submit_command('formy3d', views, out, True, {'allow_paid': True})
            self.assertEqual((cmd[0], cmd[2:]), ('/venv/bin/python', ['submit', '/v', '/o', '--dry-run', '--allow-paid']))
            self.assertTrue(cmd[1].endswith('formy3d_pipeline/formy3d_backend.py'))
            self.assertEqual(convert_job.submit_command('formy3d', views, out)[2:], ['submit', '/v', '/o'])
            self.assertEqual(convert_job.collect_command('formy3d', out, {'allow_paid': True})[2:], ['collect', '/o'])
        self.assertEqual(convert_job.submit_command('tripo', views, out),
                         ['node', str(convert_job.TOOLS / 'studio.mjs'), 'submit', '/v', '/o'])
        self.assertIn('formy3d', convert_job.BACKENDS)

    def run_main(self, argv_extra, interior_share=0.0, submit_code=0):
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
                if submit_code:
                    return mock.Mock(returncode=submit_code)
                out.mkdir(parents=True, exist_ok=True)
                record.write_text(json.dumps({'status': 'submitted', 'project_id': 'P', 'backend': 'formy3d'}))
            elif ' collect ' in joined:
                make_glb(out / 'x.glb')
                data = json.loads(record.read_text())
                data.update(status='exported', exports={'GLB': {'path': str(out / 'x.glb')}})
                record.write_text(json.dumps(data))
            elif 'retry' in joined:
                raise AssertionError('free retry must not run here')
            else:
                (out / 'reduced').mkdir(parents=True, exist_ok=True)
                (out / 'reduced/manifest.json').write_text(json.dumps({
                    'interior': {'interior_area_share': interior_share, 'threshold': 0.15},
                    'levels': {'0.4': {'triangles': 5, 'mesh_checks_pass': True, 'material_review_required': False}}}))
            return mock.Mock(returncode=0)
        with mock.patch.object(convert_job, 'ROOT', root), mock.patch.object(convert_job.subprocess, 'run', fake_run), \
                mock.patch.dict(os.environ, {'FORMY3D_VENV': '/venv'}):
            try:
                convert_job.main(['s--j', '--backend', 'formy3d'] + argv_extra)
                code = 0
            except SystemExit as exc:
                code = exc.code
        return commands, json.loads((job / '.tripo-job.json').read_text()), code

    def test_formy3d_routes_and_passes_allow_paid_to_submit_only(self):
        commands, record, code = self.run_main(['--allow-paid'], 0.9)   # broken mesh: only flagged, no free retry
        self.assertEqual(code, 1)
        submit = next(c for c in commands if ' submit ' in ' '.join(map(str, c)))
        collect = next(c for c in commands if ' collect ' in ' '.join(map(str, c)))
        self.assertIn('--allow-paid', submit)
        self.assertNotIn('--allow-paid', collect)
        self.assertEqual(record['module_conversion']['human_male/outfit']['backend'], 'formy3d')
        self.assertFalse(any('studio.mjs' in str(c) for c in commands))

    def test_formy3d_clean_mesh(self):
        _, record, code = self.run_main(['--allow-paid'])
        self.assertEqual((code, record['state'], record['conversion']),
                         (0, 'converted', 'formy3d_and_reduction_done_pending_review'))

    def test_submit_guard_stop_is_resumable(self):
        _, record, code = self.run_main([], submit_code=3)
        self.assertIn('formy3d submit stopped', str(code))
        self.assertNotIn('state', record)


if __name__ == '__main__':
    unittest.main()
