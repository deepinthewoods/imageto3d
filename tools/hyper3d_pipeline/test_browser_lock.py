"""Offline tests for the Hyper3D locks (browser_lock.py): fake sessions / fake selenium modules, no browser.

Browser lock: two real processes: one holds the lock, the other waits and either gets it (holder let go) or gives up with
Stop('Hyper3D profile busy ...'). The lock is taken where the profile is opened (browser_lock.open_profile, used by
SeleniumRodin._start, native_browser.run and workflow.py main) and is re-entrant within one process.

Output (paid-record) lock (review_v2_phase4 finding 1): two collectors on one output with the real OS lock: exactly one
paid confirmation click; a concurrent inspector or collector stops with 'another process is handling this output' without
touching the record; the record is read only after the lock is taken.
"""
import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import browser_lock
import rodin_backend as rb
from test_rodin_backend import CONFIG, MODEL, URL, make_views, FakeSession

HERE = Path(__file__).resolve().parent
HOLDER = """
import sys
sys.path.insert(0, {here!r})
import rodin_backend as rb
with rb.BrowserLock(wait=0):
    print('held', flush=True)
    sys.stdin.readline()
print('released', flush=True)
"""
WAITER = """
import sys
sys.path.insert(0, {here!r})
import rodin_backend as rb
try:
    with rb.BrowserLock():
        print('acquired', flush=True)
except rb.Stop as exc:
    print('STOP ' + str(exc), flush=True)
    raise SystemExit(3)
"""


def held_elsewhere(path):
    """True when the OS lock on `path` is held (probed through a fresh handle, which conflicts even with this process's
    own hold: the re-entrancy is the module's bookkeeping, not the OS lock's)."""
    path = Path(path)
    if not path.exists():
        return False
    with open(path, 'a+b') as handle:
        if browser_lock.claim_lock.try_lock(handle):
            browser_lock.claim_lock.release_lock(handle)
            return False
        return True


class LockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lock_file = Path(self.temp.name) / '.codex_jobs' / '_batch' / 'browser_hyper3d.lock'
        self.env = {**os.environ, 'HYPER3D_BROWSER_LOCK': str(self.lock_file), 'PYTHONIOENCODING': 'utf-8'}
        patch = mock.patch.dict(os.environ, {'HYPER3D_BROWSER_LOCK': str(self.lock_file)})
        patch.start()
        self.addCleanup(patch.stop)

    def start_holder(self):
        proc = subprocess.Popen([sys.executable, '-c', HOLDER.format(here=str(HERE))], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)
        self.addCleanup(self.finish, proc)
        self.assertEqual(proc.stdout.readline().strip(), 'held', proc.stderr.read() if proc.poll() is not None else '')
        return proc

    @staticmethod
    def finish(proc):
        if proc.poll() is None:
            proc.stdin.write('\n')
            proc.stdin.flush()
            proc.wait(timeout=30)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()

    def waiter(self, wait):
        return subprocess.Popen([sys.executable, '-c', WAITER.format(here=str(HERE))], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env={**self.env, 'HYPER3D_LOCK_WAIT': str(wait)})

    def test_the_default_lock_file_is_the_imageto3d_roots(self):
        root = HERE.parents[1]
        self.assertEqual(rb.LOCK_FILE, root / '.codex_jobs' / '_batch' / 'browser_hyper3d.lock')
        self.assertTrue((root / 'AGENTS.md').is_file())
        with mock.patch.dict(os.environ):
            os.environ.pop('HYPER3D_BROWSER_LOCK')
            self.assertEqual(rb.BrowserLock().path, rb.LOCK_FILE)

    def test_wait_seconds_default_and_env(self):
        with mock.patch.dict(os.environ):
            os.environ.pop('HYPER3D_LOCK_WAIT', None)
            self.assertEqual(rb.lock_wait_seconds(), 600.0)
            os.environ['HYPER3D_LOCK_WAIT'] = '2.5'
            self.assertEqual(rb.lock_wait_seconds(), 2.5)
            os.environ['HYPER3D_LOCK_WAIT'] = 'soon'
            self.assertEqual(rb.lock_wait_seconds(), 600.0)
            os.environ['HYPER3D_LOCK_WAIT'] = '-5'
            self.assertEqual(rb.lock_wait_seconds(), 0.0)

    def test_a_second_process_gives_up_after_the_short_wait(self):
        holder = self.start_holder()
        started = time.monotonic()
        waiter = self.waiter(1.5)
        out, err = waiter.communicate(timeout=60)
        self.assertEqual(waiter.returncode, 3, err)
        self.assertIn('STOP Hyper3D profile busy (another conversion holds the browser)', out)
        self.assertIn('pid', out)                        # the holder's pid is named
        self.assertGreaterEqual(time.monotonic() - started, 1.4)
        self.assertIn('waiting up to', err)
        self.assertIsNone(holder.poll())                 # the holder is untouched

    def test_a_second_process_waits_and_then_gets_the_lock(self):
        holder = self.start_holder()
        waiter = self.waiter(60)
        time.sleep(2)
        self.assertIsNone(waiter.poll())                 # still waiting
        holder.stdin.write('\n')
        holder.stdin.flush()
        out, err = waiter.communicate(timeout=60)
        self.assertEqual((waiter.returncode, out.strip()), (0, 'acquired'), err)

    def test_the_kernel_frees_the_lock_when_the_holder_dies(self):
        holder = self.start_holder()
        holder.kill()
        holder.wait(timeout=30)
        with rb.BrowserLock(wait=5):
            pass

    def test_reentrant_within_this_process_and_released_by_the_last_holder(self):
        outer = rb.BrowserLock(wait=0).acquire()
        inner = rb.BrowserLock(wait=0).acquire()          # same process: joins the hold (no self-deadlock)
        self.assertEqual(browser_lock.held_by_this_process(), 2)
        inner.release()
        inner.release()                                   # idempotent per object
        self.assertTrue(held_elsewhere(self.lock_file))   # the outer hold keeps the OS lock
        waiter = self.waiter(0)                           # another process is still refused
        out, _ = waiter.communicate(timeout=60)
        self.assertEqual(waiter.returncode, 3, out)
        outer.release()
        self.assertEqual(browser_lock.held_by_this_process(), 0)
        self.assertFalse(held_elsewhere(self.lock_file))
        waiter = self.waiter(0)
        out, err = waiter.communicate(timeout=60)
        self.assertEqual((waiter.returncode, out.strip()), (0, 'acquired'), err)

    def test_a_fresh_lock_file_is_created_with_its_folders(self):
        self.assertFalse(self.lock_file.parent.exists())
        with rb.BrowserLock(wait=0):
            self.assertTrue(self.lock_file.is_file())
        self.assertTrue(self.lock_file.is_file())          # never deleted


# --------------------------------------------------------------------------- fake selenium / browser modules

class FakeOptions:
    def __init__(self):
        self.args, self.prefs = [], {}
        self.binary_location = self.page_load_strategy = None

    def add_argument(self, arg):
        self.args.append(arg)

    def set_preference(self, key, value):
        self.prefs[key] = value


class FakeDriver:
    def __init__(self, log, lock_file):
        self.log, self.lock_file = log, lock_file
        self.current_url, self.title, self.window_handles = 'about:blank', '', ['w1']
        self.current_window_handle = 'w1'
        log.append(('firefox', held_elsewhere(lock_file)))   # the browser starts with the lock held

    def set_window_size(self, *a):
        pass

    def set_page_load_timeout(self, *a):
        pass

    def get(self, url):
        self.current_url = url

    def find_elements(self, *a):
        return []

    def save_screenshot(self, path):
        pass

    def quit(self):
        self.log.append(('quit', held_elsewhere(self.lock_file)))   # still held while the browser closes


def fake_modules(log, lock_file):
    """selenium (just what the three entry points import), browser.py (playwright) and hidden_display stand-ins."""
    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        return mod
    webdriver = module('selenium.webdriver', Firefox=lambda service=None, options=None: FakeDriver(log, lock_file))
    by = types.SimpleNamespace(TAG_NAME='tag name', CSS_SELECTOR='css selector', XPATH='xpath')
    return {
        'selenium': module('selenium', webdriver=webdriver),
        'selenium.webdriver': webdriver,
        'selenium.webdriver.firefox': module('selenium.webdriver.firefox'),
        'selenium.webdriver.firefox.options': module('selenium.webdriver.firefox.options', Options=FakeOptions),
        'selenium.webdriver.firefox.service': module('selenium.webdriver.firefox.service',
                                                     Service=lambda *a, **k: ('service', a, k)),
        'selenium.webdriver.common': module('selenium.webdriver.common'),
        'selenium.webdriver.common.by': module('selenium.webdriver.common.by', By=by),
        'selenium.webdriver.common.action_chains': module('selenium.webdriver.common.action_chains', ActionChains=object),
        'selenium.common': module('selenium.common'),
        'selenium.common.exceptions': module('selenium.common.exceptions', TimeoutException=type('TimeoutException', (Exception,), {}),
                                             WebDriverException=type('WebDriverException', (Exception,), {})),
        'browser': module('browser', collect_images=lambda inputs: [], save_snapshot=lambda path, payload: None,
                          WORKSPACE='https://hyper3d.ai/workspace/rodin', LOGIN='https://hyper3d.ai/workspace/login'),
        'hidden_display': module('hidden_display', hidden_display=lambda: None),
    }


class EntryPointTests(unittest.TestCase):
    """Every function that opens the profile takes the browser lock before the profile check and the Firefox start, keeps it
    until Firefox has quit, and stops without launching anything while another process holds it (finding 15)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lock_file = self.root / 'browser_hyper3d.lock'
        self.profile = self.root / 'profile'
        self.log = []
        env = mock.patch.dict(os.environ, {'HYPER3D_BROWSER_LOCK': str(self.lock_file), 'HYPER3D_LOCK_WAIT': '0.2',
                                           'HYPER3D_PROFILE': str(self.profile), 'HYPER3D_FIREFOX': sys.executable,
                                           'HYPER3D_GECKODRIVER': sys.executable})
        env.start()
        self.addCleanup(env.stop)
        modules = mock.patch.dict(sys.modules, fake_modules(self.log, self.lock_file))
        modules.start()
        self.addCleanup(modules.stop)
        for name in ('native_browser', 'workflow'):     # imported fresh under the fakes (and the temp profile)
            sys.modules.pop(name, None)
        import native_browser
        self.nb = native_browser
        real_check = native_browser.check_profile

        def check(profile):
            self.log.append(('check', held_elsewhere(self.lock_file)))
            return real_check(profile)
        native_browser.check_profile = check

    def hyper3d_workflow(self):
        spec = importlib.util.spec_from_file_location('workflow', HERE / 'workflow.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules['workflow'] = module
        spec.loader.exec_module(module)
        return module

    def test_selenium_rodin_start_locks_before_the_profile_check_and_until_exit(self):
        sys.modules['workflow'] = types.ModuleType('workflow')
        session = rb.SeleniumRodin(CONFIG, self.root / 'out')
        with session:
            self.assertTrue(held_elsewhere(self.lock_file))
        self.assertEqual(self.log, [('check', True), ('firefox', True), ('quit', True)])
        self.assertFalse(held_elsewhere(self.lock_file))

    def test_monster_wrapper_subclasses_inherit_the_lock(self):
        sys.modules['workflow'] = types.ModuleType('workflow')

        class Wrapper(rb.SeleniumRodin):          # what the monster wrappers do: no __enter__ / _start of their own
            pass
        holder =subprocess.Popen([sys.executable, '-c', HOLDER.format(here=str(HERE))], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True,
                                  env={**os.environ, 'HYPER3D_BROWSER_LOCK': str(self.lock_file)})
        try:
            self.assertEqual(holder.stdout.readline().strip(), 'held')
            with self.assertRaisesRegex(rb.Stop, r'Hyper3D profile busy \(another conversion holds the browser\)'):
                Wrapper(CONFIG, self.root / 'out').__enter__()
            self.assertEqual(self.log, [])                     # neither the profile check nor Firefox ran
        finally:
            holder.communicate('\n', timeout=30)

    def test_a_failed_start_releases_the_lock(self):
        sys.modules['workflow'] = types.ModuleType('workflow')
        for error in (ValueError('Firefox is open'), RuntimeError('no firefox'), KeyboardInterrupt()):
            def boom(*a, error=error, **k):
                raise error
            with mock.patch.object(sys.modules['selenium.webdriver'], 'Firefox', boom):
                session = rb.SeleniumRodin(CONFIG, self.root / 'out')
                with self.assertRaises(rb.Stop if isinstance(error, ValueError) else type(error)):
                    session.__enter__()
            self.assertFalse(held_elsewhere(self.lock_file), error)
            self.assertEqual(browser_lock.held_by_this_process(self.lock_file), 0)

    def test_a_driver_that_fails_after_start_is_quit_and_the_lock_freed(self):
        sys.modules['workflow'] = types.ModuleType('workflow')
        with mock.patch.object(FakeDriver, 'set_window_size', side_effect=RuntimeError('window')):
            with self.assertRaises(RuntimeError):
                rb.SeleniumRodin(CONFIG, self.root / 'out').__enter__()
        self.assertEqual([k for k, _ in self.log], ['check', 'firefox', 'quit'])
        self.assertFalse(held_elsewhere(self.lock_file))

    def test_native_browser_inspect_holds_the_lock(self):
        args = types.SimpleNamespace(profile=str(self.profile), command='inspect', headless=True, inputs=[], offset=0, slots=1,
                                     cycles=0, interval=15)
        with contextlib.redirect_stdout(open(os.devnull, 'w')):
            self.nb.run(args)
        self.assertEqual(self.log, [('check', True), ('firefox', True), ('quit', True)])
        self.assertFalse(held_elsewhere(self.lock_file))

    def test_native_browser_login_holds_the_lock_while_the_window_is_open(self):
        args = types.SimpleNamespace(profile=str(self.profile), command='login')
        calls = []
        with mock.patch.object(self.nb, 'WINDOWS', True), mock.patch.object(self.nb, 'FIREFOX', 'firefox.exe'), \
                mock.patch.object(self.nb.subprocess, 'call', lambda cmd: calls.append(held_elsewhere(self.lock_file)) or 0), \
                contextlib.redirect_stdout(open(os.devnull, 'w')):
            with self.assertRaises(SystemExit):
                self.nb.run(args)
        self.assertEqual((calls, self.log), ([True], [('check', True)]))
        self.assertFalse(held_elsewhere(self.lock_file))

    def test_native_browser_stops_while_another_process_holds_the_profile(self):
        holder = subprocess.Popen([sys.executable, '-c', HOLDER.format(here=str(HERE))], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True,
                                  env={**os.environ, 'HYPER3D_BROWSER_LOCK': str(self.lock_file)})
        try:
            self.assertEqual(holder.stdout.readline().strip(), 'held')
            args = types.SimpleNamespace(profile=str(self.profile), command='inspect', headless=True, inputs=[], offset=0,
                                         slots=1, cycles=0, interval=15)
            with self.assertRaisesRegex(browser_lock.Stop, 'profile busy'):
                self.nb.run(args)
            self.assertEqual(self.log, [])
        finally:
            holder.communicate('\n', timeout=30)

    def test_hyper3d_workflow_main_holds_the_lock(self):
        image = self.root / 'in.png'
        image.write_bytes(b'png')
        workflow = self.hyper3d_workflow()
        argv = ['workflow.py', 'run', str(image), '--jobs-root', str(self.root / 'jobs'), '--inspect']
        with mock.patch.object(sys, 'argv', argv), contextlib.redirect_stdout(open(os.devnull, 'w')):
            workflow.main()
        self.assertEqual(self.log, [('check', True), ('firefox', True), ('quit', True)])
        self.assertFalse(held_elsewhere(self.lock_file))


# --------------------------------------------------------------------------- submit / collect with a busy browser

class SubmitBusyTests(unittest.TestCase):
    """A busy browser at submit time must not leave a 'preparing' record that blocks the re-run."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patch = mock.patch.dict(os.environ, {'HYPER3D_BROWSER_LOCK': str(self.root / 'lock'), 'HYPER3D_LOCK_WAIT': '0.2'})
        patch.start()
        self.addCleanup(patch.stop)
        self.views = make_views(self.root / 'views')
        self.out = self.root / 'out'
        self.holder = None

    def hold_elsewhere(self):
        holder = subprocess.Popen([sys.executable, '-c', HOLDER.format(here=str(HERE))], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True, env={**os.environ})
        self.addCleanup(lambda: holder.poll() is None and holder.communicate('\n', timeout=30))
        self.assertEqual(holder.stdout.readline().strip(), 'held')

    def test_busy_browser_sets_the_record_aside_and_a_rerun_submits(self):
        class Busy(rb.SeleniumRodin):
            def _start(self):
                with rb.BrowserLock(wait=0.2):      # what open_profile does first
                    raise AssertionError('must not start')
        self.hold_elsewhere()
        with self.assertRaisesRegex(rb.Stop, 'profile busy.*nothing was generated'):
            rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: Busy(CONFIG, self.out))
        self.assertFalse((self.out / rb.RECORD).exists())
        aside = json.loads(next(self.out.glob('studio_submission.not_submitted_*.json')).read_text())
        self.assertEqual((aside['status'], aside['actions']), ('not_submitted', {}))
        rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out))
        self.assertEqual(json.loads((self.out / rb.RECORD).read_text())['status'], 'submitted')

    def test_collect_busy_just_stops(self):
        rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out))
        self.hold_elsewhere()

        class Busy(rb.SeleniumRodin):
            def _start(self):
                with rb.BrowserLock(wait=0.2):
                    raise AssertionError('must not start')
        with self.assertRaisesRegex(rb.Stop, 'profile busy'):
            rb.collect(self.out, config=CONFIG, allow_paid=True, accept_preview=True,
                       session_factory=lambda: Busy(CONFIG, self.out))
        data = json.loads((self.out / rb.RECORD).read_text())
        self.assertEqual((data['status'], list(data['actions'])), ('submitted', ['free_preview']))   # nothing paid


# --------------------------------------------------------------------------- per-output record lock (finding 1)

COLLECTOR = r"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, {here!r})
import rodin_backend as rb
from test_rodin_backend import FakeSession, CONFIG
out, log, go, mode = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]

class Slow(FakeSession):
    # reports the same unresolved geometry preview to every collector, and waits (inside the session) for the test
    def open_model(self, url):
        print('opened', flush=True)
        while not go.exists():
            time.sleep(0.05)
    def observe_stage(self):
        return 'geometry_preview'
    def click(self, kind):
        with log.open('a') as f:
            f.write(kind + '\n')

session = Slow(out, stages=['geometry_preview', 'export_ready'], costs={{'geometry': 0.5}})
try:
    if mode == 'collect':
        rb.collect(out, config=CONFIG, allow_paid=True, accept_preview=True, session_factory=lambda: session)
    else:
        rb.inspect_only(out, config=CONFIG, session_factory=lambda: session)
    print('done', flush=True)
except rb.Stop as exc:
    print('STOP ' + str(exc), flush=True)
    raise SystemExit(3)
"""


class OutputLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.views = make_views(self.root / 'views')
        self.out = self.root / 'out'
        rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out))
        self.log, self.go = self.root / 'clicks.log', self.root / 'go'
        self.script = self.root / 'collector.py'
        self.script.write_text(COLLECTOR.format(here=str(HERE)), encoding='utf-8')

    def record_bytes(self):
        return (self.out / rb.RECORD).read_bytes(), (self.out / 'events.jsonl').read_bytes()

    def spawn(self, mode):
        proc = subprocess.Popen([sys.executable, str(self.script), str(self.out), str(self.log), str(self.go), mode],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def first_holds_then(self, first_mode, second_mode):
        first = self.spawn(first_mode)
        self.assertEqual(first.stdout.readline().strip(), 'opened', first.stderr.read() if first.poll() is not None else '')
        before = self.record_bytes()
        second = self.spawn(second_mode)
        out2, err2 = second.communicate(timeout=60)
        after_second = self.record_bytes()
        self.go.touch()
        out1, err1 = first.communicate(timeout=60)
        return (first.returncode, out1, err1), (second.returncode, out2, err2), before, after_second

    def clicks(self):
        return self.log.read_text().split() if self.log.exists() else []

    def test_two_collectors_with_the_real_lock_confirm_exactly_once(self):
        (code1, out1, err1), (code2, out2, err2), before, after = self.first_holds_then('collect', 'collect')
        self.assertEqual(code2, 3, err2)
        self.assertIn('STOP another process is handling this output', out2)
        self.assertEqual(after, before)                          # the second collector touched nothing
        self.assertEqual(code1, 0, err1)
        self.assertEqual(self.clicks(), ['geometry'])            # exactly one paid confirmation
        data = json.loads((self.out / rb.RECORD).read_text())
        self.assertEqual((data['credits_committed'], data['status']), (0.5, 'exported'))
        self.assertEqual(sorted(data['actions']), ['free_preview', 'geometry'])

    def test_an_inspector_never_overwrites_a_running_collect(self):
        (code1, _, err1), (code2, out2, _), before, after = self.first_holds_then('collect', 'inspect')
        self.assertEqual((code2, after), (3, before))
        self.assertIn('another process is handling this output', out2)
        self.assertEqual(code1, 0, err1)
        data = json.loads((self.out / rb.RECORD).read_text())
        self.assertEqual(sorted(data['actions']), ['free_preview', 'geometry'])   # the collect's intent survived
        self.assertNotIn('last_observation', data)

    def test_a_collector_waits_out_nothing_while_an_inspector_holds_the_output(self):
        (code1, _, err1), (code2, out2, _), before, after = self.first_holds_then('inspect', 'collect')
        self.assertEqual((code2, after), (3, before))
        self.assertIn('another process is handling this output', out2)
        self.assertEqual((code1, self.clicks()), (0, []))
        self.assertEqual(json.loads((self.out / rb.RECORD).read_text())['last_observation'], {'stage': 'geometry_preview'})

    def test_two_threads_of_one_process_exclude_each_other(self):
        opened, release = threading.Event(), threading.Event()

        class Holding(FakeSession):
            def open_model(self, url):
                opened.set()
                release.wait(30)
        result = {}

        def first():
            result['first'] = rb.inspect_only(self.out, config=CONFIG, session_factory=lambda: Holding(self.out))
        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(opened.wait(30))
        try:
            with self.assertRaisesRegex(rb.Stop, rb.OUTPUT_BUSY):
                rb.collect(self.out, config=CONFIG, allow_paid=True, accept_preview=True,
                           session_factory=lambda: self.fail('second session opened'))
        finally:
            release.set()
            thread.join(30)
        self.assertIn('last_observation', result['first'])

    def test_the_record_is_read_after_the_lock_is_taken(self):
        """A collect that queued behind another command sees what that command wrote (here: the geometry was already
        confirmed), never its own stale copy."""
        real = browser_lock.output_lock

        @contextlib.contextmanager
        def lock_then_peer_writes(out_dir):
            with real(out_dir) as outer:
                data = json.loads((self.out / rb.RECORD).read_text())
                data['actions']['geometry'] = {'intent_at': 'peer'}     # what a finished peer collect left behind
                data['credits_committed'] = 0.5
                (self.out / rb.RECORD).write_text(json.dumps(data))
                yield outer
        session = FakeSession(self.out, stages=['geometry_preview'], costs={'geometry': 0.5})
        with mock.patch.object(rb, 'output_lock', lock_then_peer_writes):
            with self.assertRaisesRegex(rb.Stop, 'already submitted'):
                rb.collect(self.out, config=CONFIG, allow_paid=True, accept_preview=True, session_factory=lambda: session)
        self.assertNotIn(('click', 'geometry'), session.calls)
        self.assertEqual(json.loads((self.out / rb.RECORD).read_text())['credits_committed'], 0.5)

    def test_record_creation_is_exclusive(self):
        other = self.root / 'other'
        other.mkdir()
        rb.Record.create(other / rb.RECORD, {'status': 'preparing'})
        with mock.patch.object(Path, 'exists', return_value=False):     # a racer that passed the existence check
            with self.assertRaisesRegex(rb.Stop, 'already exists'):
                rb.Record.create(other / rb.RECORD, {'status': 'other'})
        self.assertEqual(json.loads((other / rb.RECORD).read_text()), {'status': 'preparing'})
        self.assertEqual(sorted(p.name for p in other.iterdir()), [rb.RECORD])   # no temp file left

    def test_bind_takes_the_lock_and_rereads(self):
        other = self.root / 'bind_out'
        other.mkdir()
        rb.Record.create(other / rb.RECORD, {'status': 'submission_attempted', 'model_id': None})
        stale = rb.Record(other / rb.RECORD)
        data = json.loads((other / rb.RECORD).read_text())
        data['note'] = 'written after the stale copy was read'
        (other / rb.RECORD).write_text(json.dumps(data))
        stale.bind(URL)
        fresh = json.loads((other / rb.RECORD).read_text())
        self.assertEqual((fresh['model_id'], fresh['note']), (MODEL, 'written after the stale copy was read'))
        with browser_lock.output_lock(other):
            holder_thread = {}
            thread = threading.Thread(target=lambda: holder_thread.setdefault('error', self._bind_error(other)))
            thread.start()
            thread.join(30)
        self.assertIn(rb.OUTPUT_BUSY, holder_thread['error'])

    @staticmethod
    def _bind_error(out):
        try:
            rb.bind(out, URL)
        except rb.Stop as exc:
            return str(exc)
        return 'no stop'


if __name__ == '__main__':
    unittest.main()
