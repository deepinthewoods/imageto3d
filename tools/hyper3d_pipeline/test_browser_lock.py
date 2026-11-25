"""Offline tests for the Hyper3D browser lock (rodin_backend.BrowserLock / SeleniumRodin.__enter__): fake sessions, no browser.

Two real processes: one holds the lock, the other waits and either gets it (holder let go) or gives up with
Stop('Hyper3D profile busy ...').
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import rodin_backend as rb
from test_rodin_backend import CONFIG, make_views, FakeSession

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

    def test_release_is_idempotent_and_reusable(self):
        lock = rb.BrowserLock(wait=0)
        with lock:
            with self.assertRaisesRegex(rb.Stop, 'profile busy'):
                rb.BrowserLock(wait=0).acquire()          # a second handle, even in this process, is refused
        lock.release()
        with rb.BrowserLock(wait=0):
            pass

    def test_a_fresh_lock_file_is_created_with_its_folders(self):
        self.assertFalse(self.lock_file.parent.exists())
        with rb.BrowserLock(wait=0):
            self.assertTrue(self.lock_file.is_file())
        self.assertTrue(self.lock_file.is_file())          # never deleted


class SessionTests(unittest.TestCase):
    """SeleniumRodin.__enter__ / __exit__ with the browser start stubbed out."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patch = mock.patch.dict(os.environ, {'HYPER3D_BROWSER_LOCK': str(self.root / 'lock'), 'HYPER3D_LOCK_WAIT': '0.2'})
        patch.start()
        self.addCleanup(patch.stop)

    def held_elsewhere(self):
        probe = rb.BrowserLock(wait=0)
        try:
            probe.acquire()
        except rb.Stop:
            return True
        probe.release()
        return False

    def test_enter_takes_the_lock_before_the_browser_and_exit_releases_it(self):
        order = []
        session = rb.SeleniumRodin(CONFIG, self.root / 'out')
        session._start = lambda: order.append(('start', self.held_elsewhere())) or session
        with session:
            self.assertTrue(self.held_elsewhere())
        self.assertEqual(order, [('start', True)])           # the lock was already held when the browser start ran
        self.assertFalse(self.held_elsewhere())

    def test_exit_quits_the_driver_and_releases_even_when_quit_fails(self):
        session = rb.SeleniumRodin(CONFIG, self.root / 'out')
        session._start = lambda: session
        session.driver = mock.Mock()
        session.driver.quit.side_effect = RuntimeError('geckodriver died')
        with self.assertRaises(RuntimeError):
            with session:
                pass
        self.assertFalse(self.held_elsewhere())

    def test_a_failed_start_releases_the_lock(self):
        for error in (rb.Stop('Firefox profile is open elsewhere'), RuntimeError('no firefox'), KeyboardInterrupt()):
            session = rb.SeleniumRodin(CONFIG, self.root / 'out')

            def boom(error=error):
                raise error
            session._start = boom
            with self.assertRaises(type(error)):
                session.__enter__()
            self.assertFalse(self.held_elsewhere(), error)

    def test_a_busy_browser_stops_before_anything_is_launched(self):
        launched = []
        session = rb.SeleniumRodin(CONFIG, self.root / 'out')
        session._start = lambda: launched.append(1) or session
        with rb.BrowserLock(wait=0):
            with self.assertRaisesRegex(rb.Stop, r'Hyper3D profile busy \(another conversion holds the browser\)'):
                session.__enter__()
        self.assertEqual(launched, [])
        self.assertIsNone(session.lock)

    def test_subclasses_inherit_the_lock(self):
        class Wrapper(rb.SeleniumRodin):          # what the monster wrappers do
            pass
        wrapper = Wrapper(CONFIG, self.root / 'out')
        wrapper._start = lambda: wrapper
        with rb.BrowserLock(wait=0):
            with self.assertRaisesRegex(rb.Stop, 'profile busy'):
                wrapper.__enter__()


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

    def test_busy_browser_sets_the_record_aside_and_a_rerun_submits(self):
        class Busy(rb.SeleniumRodin):
            def _start(self):
                raise AssertionError('must not start')
        with rb.BrowserLock(wait=0):
            with self.assertRaisesRegex(rb.Stop, 'profile busy.*nothing was generated'):
                rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: Busy(CONFIG, self.out))
        self.assertFalse((self.out / rb.RECORD).exists())
        aside = json.loads(next(self.out.glob('studio_submission.not_submitted_*.json')).read_text())
        self.assertEqual((aside['status'], aside['actions']), ('not_submitted', {}))
        rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out))
        self.assertEqual(json.loads((self.out / rb.RECORD).read_text())['status'], 'submitted')

    def test_collect_busy_just_stops(self):
        rb.submit(self.views, self.out, config=CONFIG, session_factory=lambda: FakeSession(self.out))
        with rb.BrowserLock(wait=0):
            with self.assertRaisesRegex(rb.Stop, 'profile busy'):
                rb.collect(self.out, config=CONFIG, allow_paid=True, accept_preview=True,
                           session_factory=lambda: rb.SeleniumRodin(CONFIG, self.out))
        data = json.loads((self.out / rb.RECORD).read_text())
        self.assertEqual((data['status'], list(data['actions'])), ('submitted', ['free_preview']))   # nothing paid


if __name__ == '__main__':
    unittest.main()
