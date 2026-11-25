"""Offline tests for the monster Hyper3D wrappers (characters/monsters/aaa/tools/hyper3d_{single,multiview,twoview}.py):
upload plan, record fields, size rule and the 2-4 image limit, with fake sessions / a fake driver and no browser."""
import contextlib
import io
import itertools
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import rodin_backend as rb
from test_rodin_backend import CONFIG, FakeSession, make_views, png

MONSTER_TOOLS = Path(__file__).resolve().parents[3] / 'monsters' / 'aaa' / 'tools'
if not (MONSTER_TOOLS / 'hyper3d_multiview.py').is_file():
    raise unittest.SkipTest(f'monster wrappers not found in {MONSTER_TOOLS}')
sys.path.insert(0, str(MONSTER_TOOLS))
import hyper3d_multiview as mv  # noqa: E402
import hyper3d_single as single  # noqa: E402
import hyper3d_twoview as two  # noqa: E402


def cli(module_main, *argv):
    """Run a wrapper's main; returns (exit code or None, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    code = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            module_main(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class Temp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.views = self.root / 'views'
        self.views.mkdir()
        self.out = self.root / 'out'

    def make(self, **sizes):
        """PNG files in views/: name -> (w, h)."""
        for name, (w, h) in sizes.items():
            png(self.views / name, w=w, h=h)


class ParseViewsTests(unittest.TestCase):
    def test_two_to_four_in_order(self):
        self.assertEqual(mv.parse_views('front.png,back.png'), ['front.png', 'back.png'])
        self.assertEqual(mv.parse_views('right.png, front.png ,left.png'), ['right.png', 'front.png', 'left.png'])
        self.assertEqual(mv.parse_views('a.png,b.png,c.png,d.png'), ['a.png', 'b.png', 'c.png', 'd.png'])

    def test_five_images_are_refused(self):
        with self.assertRaisesRegex(ValueError, '2 to 4.*got 5'):
            mv.parse_views('a.png,b.png,c.png,d.png,e.png')

    def test_one_or_none_is_refused(self):
        for text in ('front.png', '', None):
            with self.assertRaisesRegex(ValueError, '2 to 4'):
                mv.parse_views(text)

    def test_bad_names(self):
        for text in ('a.png,a.png', 'a.png,A.PNG', 'a.png,b.jpg', 'a.png,../b.png', 'a.png,sub/b.png', 'a.png,sub\\b.png',
                     'a.png,', 'a.png,.png'):
            with self.assertRaises(ValueError, msg=text):
                mv.parse_views(text)


class ConfigTests(unittest.TestCase):
    def test_multiview_config(self):
        config = mv.multi_view_config(['left.png', 'front.png', 'right.png'])
        self.assertEqual(config['view_order'], ['left.png', 'front.png', 'right.png'])
        self.assertEqual((config['directions'], config['multi_view'], config['max_images']), ({}, True, 4))
        self.assertTrue(config['allow_mixed_sizes'])
        self.assertEqual(config['effort'], CONFIG['effort'])             # the outfit backend's settings are untouched
        self.assertEqual(config['expected_costs'], CONFIG['expected_costs'])

    def test_the_shipped_config_is_not_modified(self):
        mv.multi_view_config(['a.png', 'b.png'])
        self.assertNotIn('allow_mixed_sizes', rb.load_config())
        self.assertEqual(rb.load_config()['view_order'], CONFIG['view_order'])

    def test_twoview_keeps_the_fixed_plan_and_the_size_rule(self):
        config = two.two_view_config()
        self.assertEqual((config['view_order'], config['directions'], config['multi_view']),
                         (['front.png', 'back.png'], {}, True))
        self.assertNotIn('allow_mixed_sizes', config)

    def test_single_config(self):
        front = single.single_image_config()
        self.assertEqual((front['view_order'], front['directions'], front['multi_view']),
                         (['front.png'], {'front.png': 'Front'}, False))
        left = single.single_image_config('left.png')
        self.assertEqual((left['view_order'], left['directions'], left['multi_view']), (['left.png'], {}, False))
        for bad in ('sub/left.png', '..\\x.png', 'left.jpg', '.png', ''):
            with self.assertRaises(ValueError, msg=bad):
                single.single_image_config(bad)


class ValidateTests(Temp):
    def test_mixed_sizes_pass_for_multiview_and_are_listed(self):
        self.make(**{'front.png': (8, 6), 'back.png': (9, 7), 'left.png': (10, 5)})
        plan = rb.validate_views(self.views, mv.multi_view_config(['front.png', 'back.png', 'left.png']))
        self.assertEqual(plan['uploaded'], ['front.png', 'back.png', 'left.png'])
        self.assertEqual(plan['input_resolutions'], {'front.png': [8, 6], 'back.png': [9, 7], 'left.png': [10, 5]})
        self.assertEqual(plan['input_resolution'], [8, 6])

    def test_equal_sizes_have_no_resolutions_list(self):
        self.make(**{'front.png': (8, 6), 'back.png': (8, 6)})
        self.assertNotIn('input_resolutions', rb.validate_views(self.views, mv.multi_view_config(['front.png', 'back.png'])))

    def test_twoview_and_the_outfit_backend_still_refuse_mixed_sizes(self):
        self.make(**{'front.png': (8, 6), 'back.png': (9, 6)})
        with self.assertRaisesRegex(ValueError, 'different native dimensions'):
            rb.validate_views(self.views, two.two_view_config())
        with self.assertRaisesRegex(ValueError, 'different native dimensions'):
            rb.validate_views(self.views, {**CONFIG, 'view_order': ['front.png', 'back.png']})

    def test_png_and_rgba_checks_stay(self):
        self.make(**{'front.png': (8, 6)})
        png(self.views / 'back.png', depth=16)
        with self.assertRaisesRegex(ValueError, '8-bit RGBA'):
            rb.validate_views(self.views, mv.multi_view_config(['front.png', 'back.png']))
        (self.views / 'back.png').write_bytes(b'nope' * 20)
        with self.assertRaisesRegex(ValueError, 'Expected PNG'):
            rb.validate_views(self.views, mv.multi_view_config(['front.png', 'back.png']))
        (self.views / 'back.png').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing back.png'):
            rb.validate_views(self.views, mv.multi_view_config(['front.png', 'back.png']))


class CliTests(Temp):
    def test_check_prints_the_plan(self):
        self.make(**{'front.png': (8, 6), 'right.png': (9, 9)})
        code, out, _ = cli(mv.main, 'check', str(self.views), '--views', 'right.png,front.png')
        self.assertIsNone(code)
        plan = json.loads(out)
        self.assertEqual((plan['uploaded'], plan['input_resolutions']['right.png']), (['right.png', 'front.png'], [9, 9]))

    def test_five_images_are_refused_on_the_command_line(self):
        self.make(**{n: (8, 6) for n in ('a.png', 'b.png', 'c.png', 'd.png', 'e.png')})
        code, out, err = cli(mv.main, 'check', str(self.views), '--views', 'a.png,b.png,c.png,d.png,e.png')
        self.assertEqual(code, 2)
        self.assertIn('INVALID INPUT', err)
        self.assertEqual(out, '')
        code, _, err = cli(mv.main, 'submit', str(self.views), str(self.out), '--views', 'a.png,b.png,c.png,d.png,e.png')
        self.assertEqual(code, 2)
        self.assertFalse(self.out.exists())

    def test_views_is_required_for_check_and_submit_only(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertRaises(SystemExit, mv.main, ['check', str(self.views)])
            self.assertRaises(SystemExit, mv.main, ['submit', str(self.views), str(self.out)])
        self.out.mkdir()
        (self.out / rb.RECORD).write_text(json.dumps({'status': 'submitted', 'backend': 'hyper3d'}))   # no URL: inspect is a no-op
        code, out, _ = cli(mv.main, 'inspect', str(self.out))                                         # no --views needed
        self.assertEqual((code, json.loads(out)['status']), (None, 'submitted'))

    def test_twoview_check_is_strict_and_unchanged(self):
        self.make(**{'front.png': (8, 6), 'back.png': (8, 6)})
        code, out, _ = cli(lambda argv: single.main(argv, make_config=two.two_view_config, session_class=two.TwoViewRodin), 'check', str(self.views))
        self.assertEqual((code, json.loads(out)['uploaded']), (None, ['front.png', 'back.png']))
        png(self.views / 'back.png', w=9, h=6)
        code, _, err = cli(lambda argv: single.main(argv, make_config=two.two_view_config, session_class=two.TwoViewRodin), 'check', str(self.views))
        self.assertEqual(code, 2)
        self.assertIn('different native dimensions', err)

    def test_single_view_option(self):
        self.make(**{'front.png': (8, 6), 'left.png': (9, 9)})
        code, out, _ = cli(single.main, 'check', str(self.views))                     # default: front.png
        self.assertEqual((code, json.loads(out)['uploaded']), (None, ['front.png']))
        code, out, _ = cli(single.main, 'check', str(self.views), '--view', 'left.png')
        self.assertEqual((code, json.loads(out)['uploaded'], json.loads(out)['input_resolution']), (None, ['left.png'], [9, 9]))
        code, _, err = cli(single.main, 'check', str(self.views), '--view', 'right.png')
        self.assertEqual(code, 2)
        self.assertIn('Missing right.png', err)
        code, _, err = cli(single.main, 'check', str(self.views), '--view', '../x.png')
        self.assertEqual(code, 2)


class SubmitRecordTests(Temp):
    def submit(self, config, **kw):
        session = FakeSession(self.out, **kw)
        rb.submit(self.views, self.out, config=config, session_factory=lambda: session)
        return session, json.loads((self.out / rb.RECORD).read_text())

    def test_multiview_upload_plan_and_record(self):
        self.make(**{'front.png': (8, 6), 'back.png': (9, 7), 'left.png': (8, 6), 'right.png': (6, 6)})
        order = ['right.png', 'front.png', 'back.png']
        session, data = self.submit(mv.multi_view_config(order))
        self.assertIn(('prepare_multiview', order), session.calls)       # upload order = the list's order; left.png not sent
        self.assertEqual((data['images_uploaded'], data['images']), (order, order))
        self.assertTrue(data['settings']['workflow'].endswith('Multi-view'))
        self.assertEqual(data['settings']['directions'], {n: None for n in order})
        self.assertEqual(data['input_resolutions'], {'right.png': [6, 6], 'front.png': [8, 6], 'back.png': [9, 7]})
        self.assertEqual(data['status'], 'submitted')

    def test_equal_size_multiview_record_has_no_resolutions_list(self):
        self.make(**{'front.png': (8, 6), 'back.png': (8, 6)})
        _, data = self.submit(mv.multi_view_config(['front.png', 'back.png']))
        self.assertNotIn('input_resolutions', data)
        self.assertEqual(data['input_resolution'], [8, 6])

    def test_single_non_front_record(self):
        self.make(**{'front.png': (8, 6), 'left.png': (9, 9)})
        session, data = self.submit(single.single_image_config('left.png'))
        self.assertIn(('prepare_multiview', ['left.png']), session.calls)
        self.assertEqual((data['images_uploaded'], data['input_resolution']), (['left.png'], [9, 9]))
        self.assertEqual(data['settings']['workflow'], 'Rodin / Image to 3D')
        self.assertEqual(data['settings']['directions'], {'left.png': None})

    def test_single_default_record_is_unchanged(self):
        self.make(**{'front.png': (8, 6), 'back.png': (8, 6)})
        _, data = self.submit(single.single_image_config())
        self.assertEqual((data['images_uploaded'], data['settings']['directions']), (['front.png'], {'front.png': 'Front'}))

    def test_mixed_sizes_are_refused_before_any_record_for_twoview(self):
        self.make(**{'front.png': (8, 6), 'back.png': (9, 7)})
        with self.assertRaisesRegex(ValueError, 'different native dimensions'):
            self.submit(two.two_view_config())
        self.assertFalse((self.out / rb.RECORD).exists())


class FakeElement:
    def __init__(self, on_keys=None, attrs=None):
        self.on_keys, self.attrs, self.sent = on_keys, attrs or {}, None

    def get_attribute(self, name):
        return self.attrs.get(name)

    def send_keys(self, keys):
        self.sent = keys
        if self.on_keys:
            self.on_keys(keys)


class FakeDriver:
    """Just enough of selenium's driver for MultiViewRodin.prepare_multiview: one multi-file input, a thumbnail strip."""

    def __init__(self, thumbs_per_file=1, labels=('Multi-view',)):
        self.cards, self.labels, self.thumbs_per_file = [], list(labels), thumbs_per_file
        self.input = FakeElement(on_keys=self._uploaded, attrs={'accept': 'image/png', 'multiple': 'true'})

    def _uploaded(self, keys):
        self.cards = [object() for _ in keys.split('\n')] * self.thumbs_per_file

    def find_elements(self, by, selector):
        if 'rodin-homepage-upload-strip' in selector:
            return list(self.cards)
        if selector == '.rodin-homepage-upload-wrapper > input[type=file]':
            return [self.input]
        return []

    def execute_script(self, js, *args):
        return self.labels


class PrepareMultiviewTests(Temp):
    def session(self, driver):
        session = mv.MultiViewRodin(CONFIG, self.out)
        session.driver = driver
        session.wf = mock.Mock(exact=mock.Mock(return_value=True))
        session._pick_effort = lambda *a, **k: None
        session._select_multiview_tab = lambda: None
        shots = []
        session.screenshot = shots.append
        return session, shots

    def paths(self, names):
        return [(n, self.views / n) for n in names]

    def run_prepare(self, session, names, config=None):
        fake_by = types.SimpleNamespace(By=types.SimpleNamespace(CSS_SELECTOR='css selector'))
        modules = {'selenium': mock.Mock(), 'selenium.webdriver': mock.Mock(), 'selenium.webdriver.common': mock.Mock(),
                   'selenium.webdriver.common.by': fake_by}
        # time.monotonic jumps 20 s per call so the 30 s upload wait runs out at once
        with mock.patch.dict(sys.modules, modules), mock.patch('time.sleep'), \
                mock.patch('time.monotonic', side_effect=itertools.count(0, 20)):
            return session.prepare_multiview(self.paths(names), config or mv.multi_view_config(names))

    def test_uploads_the_files_in_list_order_and_reports(self):
        names = ['right.png', 'front.png', 'back.png', 'left.png']
        driver = FakeDriver()
        session, _ = self.session(driver)
        info = self.run_prepare(session, names)
        self.assertEqual(driver.input.sent, '\n'.join(str(self.views / n) for n in names))
        self.assertEqual((info['thumbnails'], info['views'], info['directions_assigned'], info['mode']),
                         (4, names, False, 'multi_view'))

    def test_plan_must_equal_config_view_order(self):
        session, _ = self.session(FakeDriver())
        with self.assertRaisesRegex(rb.Stop, 'multi-view plan expected'):
            self.run_prepare(session, ['front.png', 'back.png'], mv.multi_view_config(['back.png', 'front.png']))

    def test_five_or_one_image_plans_are_refused_by_the_session_too(self):
        names = ['a.png', 'b.png', 'c.png', 'd.png', 'e.png']
        driver = FakeDriver()
        session, _ = self.session(driver)
        config = {**mv.multi_view_config(['a.png', 'b.png']), 'view_order': names}
        with self.assertRaisesRegex(rb.Stop, '2 to 4 images, got 5'):
            self.run_prepare(session, names, config)
        self.assertIsNone(driver.input.sent)                 # nothing was uploaded
        with self.assertRaisesRegex(rb.Stop, '2 to 4 images, got 1'):
            self.run_prepare(session, ['a.png'], {**config, 'view_order': ['a.png']})

    def test_thumbnail_count_and_mode_label_are_checked(self):
        session, shots = self.session(FakeDriver(thumbs_per_file=0))
        with self.assertRaisesRegex(rb.Stop, 'Expected 2 uploaded thumbnails, found 0'):
            self.run_prepare(session, ['a.png', 'b.png'])
        self.assertEqual(shots, ['upload_count_mismatch'])
        session, shots = self.session(FakeDriver(labels=('Single image', 'Multi-view')))
        with self.assertRaisesRegex(rb.Stop, 'does not show Multi-view alone'):
            self.run_prepare(session, ['a.png', 'b.png'])
        self.assertEqual(shots, ['multiview_mode_lost'])

    def test_twoview_session_is_the_same_class_with_its_two_file_plan(self):
        self.assertIs(two.TwoViewRodin, mv.MultiViewRodin)
        driver = FakeDriver()
        session, _ = self.session(driver)
        self.run_prepare(session, ['front.png', 'back.png'], two.two_view_config())
        self.assertEqual(driver.input.sent, '\n'.join(str(self.views / n) for n in ('front.png', 'back.png')))


class WrapperGateTests(Temp):
    """Phase 5 review finding 2: a monster wrapper's collect stops at a closed paid gate with exit 4 (resumable, no click)."""

    def test_collect_exits_4_at_a_closed_gate_and_resumes(self):
        self.make(**{'front.png': (8, 6), 'back.png': (8, 6)})
        config = mv.multi_view_config(['front.png', 'back.png'])
        rb.submit(self.views, self.out, config=config, session_factory=lambda: FakeSession(self.out))
        gate_root = self.root / 'gate_root'
        (gate_root / '.codex_jobs' / '_batch').mkdir(parents=True)
        hold = gate_root / '.codex_jobs' / '_batch' / 'CONVERT_HOLD'

        class Waiting(FakeSession):
            def wait_stage(self, stages, timeout):
                hold.write_text('{"reason": "supervisor restart"}')       # set while the collector waits for the stage
                return super().wait_stage(stages, timeout)
        sessions = []

        def session_class(cfg, out):
            sessions.append(Waiting(out, stages=['geometry_preview'], costs={'geometry': 0.5}))
            return sessions[-1]
        main = lambda argv: single.main(argv, make_config=mv.config_from_args, session_class=session_class)   # noqa: E731
        with mock.patch.dict('os.environ', rb.paid_gate.env_for(gate_root)):
            code, _, err = cli(main, 'collect', str(self.out), '--allow-paid', '--accept-preview')
        self.assertEqual(code, rb.EXIT_GATE_CLOSED)
        self.assertIn('PAID GATE CLOSED: conversions are held', err)
        self.assertFalse([c for c in sessions[0].calls if c[0] == 'click'])
        data = json.loads((self.out / rb.RECORD).read_text())
        self.assertEqual((data['pending_action'], list(data['actions'])), (None, ['free_preview']))


class WrapperLockTests(unittest.TestCase):
    def test_wrapper_sessions_inherit_the_backends_browser_lock(self):
        self.assertTrue(issubclass(mv.MultiViewRodin, rb.SeleniumRodin))
        self.assertTrue(issubclass(single.SingleImageRodin, rb.SeleniumRodin))
        self.assertIs(mv.MultiViewRodin.__enter__, rb.SeleniumRodin.__enter__)
        self.assertIs(single.SingleImageRodin.__exit__, rb.SeleniumRodin.__exit__)
        self.assertIs(single.rb, rb)                              # one module object: the same LOCK_FILE
        self.assertEqual(Path(single.rb.__file__).resolve(), Path(rb.__file__).resolve())


if __name__ == '__main__':
    unittest.main()
