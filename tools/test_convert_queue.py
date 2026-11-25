"""convert_queue.py: the Tripo balance (review_v2_phase4 finding 4), the CONVERT_HOLD / PAUSE gate (DESIGN_PHASE4 I.1), the
Tripo browser lock around the balance check (I.3, finding 8) and the per-worker graceful stop (I.2, finding 6).
Temp roots; studio.mjs and convert_job.py are never run.

    python -X utf8 -m pytest tools/test_convert_queue.py -q
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convert_queue as cq  # noqa: E402
import tripo_browser_lock as tbl  # noqa: E402

REAL_NAP = cq.nap
REAL_CONVERT = cq.convert


def done(stdout='', returncode=0, stderr=''):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.batch = self.root / '.codex_jobs' / '_batch'
        (self.batch / 'convert_locks').mkdir(parents=True)
        self.job = self.root / 'processing' / 'set_a--abc'
        self.naps, self.finished, self.converts = [], [], []
        self.on_nap = None
        patches = [mock.patch.object(cq, 'ROOT', self.root), mock.patch.object(cq, 'EVENTS', self.batch / 'convert_events.log'),
                   mock.patch.object(cq, 'LOCKS', self.batch / 'convert_locks'), mock.patch.object(cq, 'PAUSE', self.batch / 'PAUSE'),
                   mock.patch.object(cq, 'STOP_FILE', self.batch / 'convert_worker.stop'), mock.patch.object(cq, 'MIN_FREE', 0),
                   mock.patch.object(cq, 'nap', self.nap), mock.patch.object(cq, 'finish', self.finish),
                   mock.patch.object(cq, 'release', lambda name: None),
                   mock.patch.object(cq, 'convert', lambda job, args: self.converts.append(list(args)) or self.code),
                   mock.patch.object(cq.portable, 'process_running', lambda pattern: pattern.startswith('view_batch')),
                   mock.patch.object(cq, 'WORKER', 1),
                   mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root / 'xdg')})]   # a temp Tripo profile reservation
        self.code = 0
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(patches)])

    def nap(self, seconds):
        self.naps.append(seconds)
        if self.on_nap:
            self.on_nap(len(self.naps))

    def finish(self, name=None):
        self.finished.append((name, self.stopping_exists()))

    def stopping_exists(self, worker=1):
        return (self.batch / f'convert_worker{worker}.stopping').exists()

    def events(self):
        path = self.batch / 'convert_events.log'
        return path.read_text(encoding='utf-8').splitlines() if path.exists() else []

    def my_stop(self, worker=1, **extra):
        pid, created = cq.own_identity()
        path = self.batch / f'convert_worker{worker}.stop'
        path.write_text(json.dumps({'pid': pid, 'create_time': created, 'request': 'r1', 'time': 'now', **extra}))
        return path

    def stop_on_nap(self, n=1, worker=1):
        def on_nap(count):
            if count >= n:
                self.my_stop(worker)
        self.on_nap = on_nap


class BalanceReadTests(Base):
    def read(self, result):
        with mock.patch.object(cq.subprocess, 'run', side_effect=[result] if not callable(result) else result):
            return cq.read_tripo_balance(wait=0)

    def test_only_a_successful_numeric_reading_counts(self):
        self.assertEqual(self.read(done('noise\n{"credits": 120}\n')), (120, None))
        self.assertEqual(self.read(done('{"credits": 0}')), (0, None))
        for result, why in ((done('{"credits": null}', 2, 'Tripo balance not found'), 'exit 2'),
                            (done('{"credits": null}'), 'no numeric balance'),          # an old studio.mjs: exit 0 with null
                            (done('{"credits": true}'), 'no numeric balance'),
                            (done('{"credits": -5}'), 'no numeric balance'),
                            (done('{"credits": "12"}'), 'no numeric balance'),
                            (done('Error: page crashed', 1, 'TimeoutError: x'), 'exit 1: TimeoutError'),
                            (done('not json'), 'no JSON balance'),
                            (done(''), 'no JSON balance')):
            credits, problem = self.read(result)
            self.assertIsNone(credits, result)
            self.assertIn(why, problem)

    def test_a_timeout_or_missing_node_is_unreadable(self):
        def timeout(*a, **k):
            raise subprocess.TimeoutExpired('node', 300)

        def missing(*a, **k):
            raise FileNotFoundError('node')
        self.assertEqual(self.read(timeout), (None, 'studio.mjs balance timed out after 300 s'))
        self.assertIn('could not run', self.read(missing)[1])

    def test_the_check_runs_under_the_tripo_browser_lock_and_reports_busy(self):
        seen = []

        def run(cmd, **kw):
            with open(tbl.lock_path(self.root), 'a+b') as probe:
                seen.append(not tbl.try_lock(probe))
            return done('{"credits": 50}')
        self.assertEqual(self.read(run), (50, None))
        self.assertEqual(seen, [True])
        with tbl.tripo_browser_lock(self.root, what='v2 balance check'):
            with mock.patch.object(cq.subprocess, 'run', side_effect=AssertionError('ran while busy')):
                credits, problem = cq.read_tripo_balance(wait=0)
        self.assertIsNone(credits)
        self.assertIn('Tripo browser busy', problem)


class BalanceLoopTests(Base):
    """main(): an unreadable balance holds the job; it never falls back to Hyper3D and never ends Tripo for the worker."""

    def run_main(self, balances, next_jobs=None, argv=()):
        readings = iter(balances)
        jobs = iter(next_jobs or [(self.job, True)] * 10)
        with mock.patch.object(cq, 'read_tripo_balance', lambda wait=None: next(readings)), \
                mock.patch.object(cq, 'next_job', lambda sets: next(jobs)):
            cq.main(['--sets', 'set_a', '--poll', '1', *argv])

    def test_an_unreadable_balance_never_dispatches_hyper3d(self):
        """The reviewer's case: auto mode, the balance missing -> before the fix `--backend hyper3d --allow-paid` ran."""
        self.stop_on_nap(1)
        self.run_main([(None, 'studio.mjs balance exit 2: Tripo balance not found')])
        self.assertEqual(self.converts, [])                         # nothing ran, on any backend
        events = self.events()
        self.assertTrue(any('HOLD set_a--abc: Tripo balance unreadable (studio.mjs balance exit 2' in l for l in events), events)
        self.assertFalse(any('PARTIAL' in l or 'FAILED' in l for l in events), events)
        self.assertFalse((self.batch / 'convert_locks' / 'set_a--abc.fails').exists())   # a hold is not a failure

    def test_after_a_hold_the_balance_is_read_again_and_tripo_continues(self):
        self.stop_on_nap(2)
        self.run_main([(None, 'busy'), (200, None), (None, 'busy')])
        self.assertEqual(self.converts, [['--max-submits', '8']])   # Tripo, not Hyper3D, once the balance was readable
        self.assertTrue(any(l.endswith('BALANCE tripo 200') for l in self.events()))

    def test_an_unreadable_recheck_does_not_end_tripo(self):
        """Low balance, then the re-check fails: Tripo stays on (before the fix `(None or 0) < 25` set tripo_done)."""
        self.code = 0
        self.stop_on_nap(1)
        self.run_main([(30, None), (None, 'exit 1'), (None, 'exit 1')])
        self.assertEqual(self.converts, [['--max-submits', '1']])
        events = self.events()
        self.assertTrue(any('BALANCE tripo unreadable after set_a--abc (exit 1); Tripo stays on' in l for l in events), events)
        self.assertTrue(any('HOLD set_a--abc: Tripo balance unreadable (exit 1)' in l and 'last good balance 30' in l
                            for l in events), events)                # the next job read the balance again

    def test_a_readable_zero_balance_still_falls_back(self):
        self.code = 3
        self.on_nap = None
        jobs = [(self.job, True), (None, False)]
        self.stop_on_nap(1)
        self.run_main([(10, None), (10, None)], next_jobs=jobs)
        self.assertEqual(self.converts, [['--backend', 'hyper3d', '--allow-paid', '--accept-preview']])

    def test_the_hold_is_reported_once_per_reason(self):
        self.stop_on_nap(3)
        self.run_main([(None, 'busy')] * 3)
        self.assertEqual(sum('HOLD set_a--abc' in l for l in self.events()), 1)


class PartialJobTests(BalanceLoopTests):
    """Phase 5 review finding 3 / phase-4 #4: after a partial job (convert_job exit 3 from --max-submits) only a VERIFIED
    shortfall switches to the fallback backend."""

    def hyper3d_runs(self):
        return [c for c in self.converts if '--backend' in c]

    def test_the_reviewers_case_an_unreadable_recheck_after_a_partial_job_holds(self):
        """Balance 30, one submission permitted, exit 3, the recheck unreadable: before the fix `--backend hyper3d --allow-paid
        --accept-preview` ran and Tripo was disabled for the worker's lifetime."""
        self.code = 3
        self.stop_on_nap(1)
        self.run_main([(30, None), (None, 'studio.mjs balance exit 2')], next_jobs=[(self.job, True)])
        self.assertEqual(self.converts, [['--max-submits', '1']])
        self.assertEqual(self.hyper3d_runs(), [])
        events = self.events()
        self.assertTrue(any('HOLD set_a--abc: Tripo balance unreadable after a partial job' in l for l in events), events)
        self.assertFalse(any('FAILED' in l or 'continuing on hyper3d' in l for l in events), events)
        self.assertFalse((self.batch / 'convert_locks' / 'set_a--abc.fails').exists())

    def test_after_the_hold_tripo_continues_when_the_balance_is_readable_again(self):
        self.code = 3
        readings = [(30, None), (None, 'exit 2'), (55, None), (5, None)]
        self.stop_on_nap(2)
        self.run_main(readings, next_jobs=[(self.job, True), (self.job, True), (None, False)])
        self.assertEqual(self.converts[:2], [['--max-submits', '1'], ['--max-submits', '2']])   # Tripo again, not Hyper3D

    def test_a_sufficient_second_balance_keeps_the_job_on_tripo(self):
        self.code = 3
        self.stop_on_nap(1)
        self.run_main([(80, None), (60, None)], next_jobs=[(self.job, True), (None, False)])
        self.assertEqual(self.converts, [['--max-submits', '3']])
        self.assertEqual(self.hyper3d_runs(), [])
        self.assertTrue(any('PARTIAL set_a--abc: Tripo balance 60 still covers a module; continuing on Tripo' in l
                            for l in self.events()))

    def test_a_verified_shortfall_after_a_partial_job_falls_back(self):
        self.code = 3
        self.stop_on_nap(1)
        self.run_main([(30, None), (5, None)], next_jobs=[(self.job, True), (None, False)])
        self.assertEqual(self.converts, [['--max-submits', '1'], ['--backend', 'hyper3d', '--allow-paid', '--accept-preview']])

    def test_a_verified_shortfall_with_tripo_only_stops(self):
        self.code = 3
        self.run_main([(30, None), (5, None)], next_jobs=[(self.job, True)], argv=('--tripo-only',))
        self.assertEqual(self.converts, [['--max-submits', '1']])
        self.assertTrue(any('STOP set_a--abc' in l for l in self.events()))

    def test_an_unreadable_recheck_with_tripo_only_holds_too(self):
        self.code = 3
        self.stop_on_nap(1)
        self.run_main([(30, None), (None, 'busy')], next_jobs=[(self.job, True)], argv=('--tripo-only',))
        self.assertFalse(any('STOP set_a--abc' in l for l in self.events()))
        self.assertEqual(self.converts, [['--max-submits', '1']])

    def test_no_tripo_policy_goes_straight_to_the_fallback(self):
        self.code = 0
        self.stop_on_nap(1)
        self.run_main([], next_jobs=[(self.job, True), (None, False)], argv=('--no-tripo',))
        self.assertEqual(self.converts, [['--backend', 'hyper3d', '--allow-paid', '--accept-preview']])


class JobEligibleTests(unittest.TestCase):
    """Phase-4 finding 20: convert_queue.job_eligible(root, job) is the worker's claim/join rule, read-only."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.locks = self.root / '.codex_jobs' / '_batch' / 'convert_locks'
        self.locks.mkdir(parents=True)

    def job(self, stage='processing', name='set_a--abc', modules=('outfit', 'body_armor'), **record):
        folder = self.root / stage / name
        folder.mkdir(parents=True)
        (folder / '.tripo-job.json').write_text(json.dumps({
            'set': 'set_a', 'job_id': 'abc', 'modules': [{'race_gender': 'human_male', 'module': m} for m in modules], **record}))
        return folder

    def live_pid(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: (child.kill(), child.wait()))
        return child.pid

    def module_lock(self, module, pid):
        locks = self.root / 'models' / 'set_a' / 'abc' / '.module_locks'
        locks.mkdir(parents=True, exist_ok=True)
        (locks / f'human_male__{module}.lock').write_text(str(pid))

    def converted(self, module):
        out = self.root / 'models' / 'set_a' / 'abc' / 'human_male' / module / 'reduced'
        out.mkdir(parents=True)
        (out / 'manifest.json').write_text('{}')

    def test_rules(self):
        self.assertEqual(cq.job_eligible(self.root, 'set_a--abc'), (False, 'not staged (no folder in processing/ or multiview_ready/)'))
        job = self.job()
        self.assertEqual(cq.job_eligible(self.root, 'set_a--abc')[0], True)
        (self.locks / 'set_a--abc.fails').write_text('2')
        self.assertEqual(cq.job_eligible(self.root, 'set_a--abc')[0], True)          # fewer than MAX_TRIES
        (self.locks / 'set_a--abc.fails').write_text(str(cq.MAX_TRIES))
        ok, why = cq.job_eligible(self.root, 'set_a--abc')
        self.assertEqual((ok, why), (False, f'failed {cq.MAX_TRIES} times (MAX_TRIES {cq.MAX_TRIES})'))
        (self.locks / 'set_a--abc.fails').unlink()
        record = json.loads((job / '.tripo-job.json').read_text())
        (job / '.tripo-job.json').write_text(json.dumps({**record, 'state': 'converted'}))
        self.assertEqual(cq.job_eligible(self.root, 'set_a--abc'), (False, 'already converted'))
        (job / '.tripo-job.json').write_text('{broken')
        self.assertFalse(cq.job_eligible(self.root, 'set_a--abc')[0])

    def test_the_reviewers_case_a_held_job_lock_with_every_module_free_is_not_eligible(self):
        self.job()
        pid = self.live_pid()
        (self.locks / 'set_a--abc.lock').write_text(str(pid))
        ok, why = cq.job_eligible(self.root, 'set_a--abc')
        self.assertFalse(ok)
        self.assertIn('not joinable', why)
        self.module_lock('outfit', pid)                                              # now a live worker converts one module
        ok, why = cq.job_eligible(self.root, 'set_a--abc')
        self.assertTrue(ok)
        self.assertIn('joinable', why)

    def test_a_dead_holders_job_lock_is_claimable(self):
        self.job()
        dead = subprocess.run([sys.executable, '-c', 'import os; print(os.getpid())'], capture_output=True, text=True)
        (self.locks / 'set_a--abc.lock').write_text(dead.stdout.strip())
        self.assertTrue(cq.job_eligible(self.root, 'set_a--abc')[0])

    def test_a_staged_job_held_elsewhere_is_never_joinable(self):
        self.job(stage='multiview_ready')
        pid = self.live_pid()
        (self.locks / 'set_a--abc.lock').write_text(str(pid))
        self.module_lock('outfit', pid)
        self.assertFalse(cq.job_eligible(self.root, 'set_a--abc')[0])

    def test_no_free_module(self):
        self.job()
        self.converted('outfit')
        record = json.loads((self.root / 'processing' / 'set_a--abc' / '.tripo-job.json').read_text())
        record['module_conversion'] = {'human_male/outfit': {}}
        (self.root / 'processing' / 'set_a--abc' / '.tripo-job.json').write_text(json.dumps(record))
        self.module_lock('body_armor', self.live_pid())
        ok, why = cq.job_eligible(self.root, 'set_a--abc')
        self.assertEqual((ok, why), (False, 'every unconverted module is being converted by another worker'))

    # ---- recheck-before-cutover finding 16: root-scoped, bounded, read-only
    def tree(self, root):
        return sorted(str(p.relative_to(root)) for p in root.rglob('*'))

    def test_a_temp_root_query_creates_nothing_in_the_imported_checkout_or_in_the_root(self):
        self.job()
        other = Path(tempfile.mkdtemp())                                             # stands in for the imported (real) checkout
        self.addCleanup(lambda: __import__('shutil').rmtree(other, ignore_errors=True))
        before = self.tree(self.root)
        with mock.patch.object(cq.portable, 'ROOT', other):
            self.assertTrue(cq.job_eligible(self.root, 'set_a--abc')[0])
        self.assertEqual(self.tree(other), [])                                       # no sidecar under the imported checkout root
        self.assertEqual(self.tree(self.root), before)                               # and none under the queried root either
        self.assertFalse((self.root / '.codex_jobs' / '_batch' / 'record_locks').exists())

    def hold_record_lock(self, record):
        """A separate process that holds the record's lock of THIS root the way a writer does (convert_job.update_record)."""
        code = ('import sys, time; sys.path.insert(0, sys.argv[1]); import portable\n'
                'with portable.file_lock(sys.argv[2], exclusive=True, sidecar=True, root=sys.argv[3]):\n'
                '    print("held", flush=True); time.sleep(60)\n')
        child = subprocess.Popen([sys.executable, '-c', code, str(Path(cq.portable.__file__).parent), str(record), str(self.root)],
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (child.kill(), child.wait(), child.stdout.close()))
        self.assertEqual(child.stdout.readline().strip(), 'held')
        return child

    def test_a_held_record_lock_answers_record_busy_quickly(self):
        job = self.job()
        child = self.hold_record_lock(job / '.tripo-job.json')
        started = time.monotonic()
        self.assertEqual(cq.job_eligible(self.root, 'set_a--abc'), (False, 'record busy'))
        self.assertLess(time.monotonic() - started, cq.PROBE_WAIT + 3)
        child.kill()
        child.wait()
        self.assertTrue(cq.job_eligible(self.root, 'set_a--abc')[0])                 # only temporarily ineligible

    def test_the_probe_lock_is_bounded_and_the_writers_default_still_blocks_and_creates(self):
        job = self.job()
        record = job / '.tripo-job.json'
        with self.assertRaises(FileNotFoundError):                                   # a missing record, as before
            with cq.portable.file_lock(self.root / 'nope.json', sidecar=True, timeout=0, create=False, root=self.root):
                pass
        with cq.portable.file_lock(record, exclusive=True, sidecar=True, root=self.root):   # the writers' form
            pass
        if cq.portable.WINDOWS:
            self.assertTrue(cq.portable.record_lock_path(record, self.root).is_file())
        self.hold_record_lock(record)
        with self.assertRaises(cq.portable.LockBusy):
            with cq.portable.file_lock(record, exclusive=False, sidecar=True, timeout=0.2, create=False, root=self.root):
                pass

    def test_it_agrees_with_next_job(self):
        self.job()
        with mock.patch.object(cq, 'ROOT', self.root), mock.patch.object(cq, 'LOCKS', self.locks), \
                mock.patch.object(cq.portable, 'ROOT', self.root):   # the worker's record lock stays in the temp root too
            self.assertTrue(cq.job_eligible(self.root, 'set_a--abc')[0])
            job, owner = cq.next_job({'set_a'})
            self.assertEqual((job.name, owner), ('set_a--abc', True))
            cq.release('set_a--abc')
            (self.locks / 'set_a--abc.fails').write_text(str(cq.MAX_TRIES))
            self.assertFalse(cq.job_eligible(self.root, 'set_a--abc')[0])
            self.assertEqual(cq.next_job({'set_a'}), (None, False))


class GateTests(Base):
    def test_convert_hold_starts_no_job(self):
        (self.batch / 'CONVERT_HOLD').write_text(json.dumps({'since': 'x', 'reason': 'supervisor restart'}))
        self.stop_on_nap(1)
        with mock.patch.object(cq, 'next_job', side_effect=AssertionError('took a job')):
            cq.main(['--sets', 'x', '--no-tripo'])
        self.assertTrue(any('PAUSED (CONVERT_HOLD: supervisor restart); not starting conversions' in l for l in self.events()))

    def test_pause_scope_decides(self):
        self.assertIsNone(cq.conversions_blocked())
        cq.PAUSE.write_text(json.dumps({'scope': ['views']}))
        self.assertIsNone(cq.conversions_blocked())
        cq.PAUSE.write_text(json.dumps({'scope': ['conversions']}))
        self.assertEqual(cq.conversions_blocked(), 'PAUSE file')
        cq.PAUSE.write_text('not json')
        self.assertEqual(cq.conversions_blocked(), 'PAUSE file')
        cq.PAUSE.unlink()
        (self.batch / 'CONVERT_HOLD').write_text('')
        self.assertEqual(cq.conversions_blocked(), 'CONVERT_HOLD: conversion hold')


class WorkerStopTests(Base):
    def test_worker_honours_its_own_file_acknowledges_finishes_then_cleans_up(self):
        self.stop_on_nap(1)
        with mock.patch.object(cq, 'next_job', lambda sets: (None, False)):
            cq.main(['--sets', 'x', '--no-tripo'])
        self.assertEqual(self.finished, [(None, True)])             # finish (and its offload) ran after the .stopping ack
        self.assertFalse((self.batch / 'convert_worker1.stop').exists())
        self.assertFalse(self.stopping_exists())
        self.assertTrue(self.events()[-1].endswith('STOP requested; exiting'))

    def test_a_request_during_a_conversion_is_honoured_before_the_next_job(self):
        taken = []

        def next_job(sets):
            taken.append(1)
            return self.job, True

        def convert(job, args):
            self.my_stop()
            return 0
        with mock.patch.object(cq, 'next_job', next_job), mock.patch.object(cq, 'convert', convert):
            cq.main(['--sets', 'x', '--no-tripo'])
        self.assertEqual(len(taken), 1)
        self.assertEqual(self.finished, [('set_a--abc', False), (None, True)])

    def test_a_file_naming_another_process_is_ignored_and_kept(self):
        stop = self.batch / 'convert_worker1.stop'
        seen = []

        def on_nap(count):
            if count == 1:      # a request for another worker-1 process (e.g. the one the app thinks is running)
                stop.write_text(json.dumps({'pid': 4, 'create_time': 1.0, 'request': 'not for us'}))
            elif count == 2:
                seen.append((cq.stop_request(1), stop.exists()))
                pid, _ = cq.own_identity()
                stop.write_text(json.dumps({'pid': pid, 'create_time': 1.0, 'request': 'pid reused'}))   # our pid, wrong create time
            elif count == 3:
                seen.append((cq.stop_request(1), stop.exists()))
                self.my_stop()
        self.on_nap = on_nap
        with mock.patch.object(cq, 'next_job', lambda sets: (None, False)):
            cq.main(['--sets', 'x', '--no-tripo'])
        if cq.own_identity()[1] is not None:
            self.assertEqual(seen, [(None, True), (None, True)])     # ignored and kept both times
        self.assertEqual(len(self.naps), 3)
        self.assertEqual(self.finished, [(None, True)])

    def test_worker_two_never_consumes_worker_ones_files(self):
        profiles = self.root / 'profiles'
        (profiles / 'hyper3d-workflow-profile-w2').mkdir(parents=True)
        mine = self.my_stop(worker=1)                               # addressed to worker 1 (it names this pid, but worker 1)
        legacy = self.batch / 'convert_worker.stop'
        legacy.write_text('stop')
        seen = []

        def on_nap(count):
            seen.append((mine.exists(), legacy.exists()))
            if count == 2:
                self.my_stop(worker=2)
        self.on_nap = on_nap
        with mock.patch.object(cq, 'PROFILES', profiles), mock.patch.dict(os.environ), \
                mock.patch.object(cq, 'next_job', lambda sets: (None, False)):
            cq.main(['--sets', 'x', '--no-tripo', '--worker', '2'])
        self.assertEqual(seen, [(True, True), (True, True)])        # worker 2 left both alone
        self.assertTrue(mine.exists() and legacy.exists())
        self.assertFalse((self.batch / 'convert_worker2.stop').exists())
        self.assertEqual(self.finished, [(None, False)])            # finish ran for worker 2's own stop

    def test_stale_files_of_a_dead_worker_are_removed_at_start(self):
        stop, stopping = self.batch / 'convert_worker1.stop', self.batch / 'convert_worker1.stopping'
        dead = subprocess.run([sys.executable, '-c', 'import os; print(os.getpid())'], capture_output=True, text=True)
        pid = int(dead.stdout)
        stop.write_text(json.dumps({'pid': pid, 'create_time': 1.0}))
        stopping.write_text(json.dumps({'pid': pid, 'time': 'x'}))
        seen = []

        def on_nap(count):
            seen.append((stop.exists(), stopping.exists()))
            self.my_stop()
        self.on_nap = on_nap
        with mock.patch.object(cq, 'next_job', lambda sets: (None, False)):
            cq.main(['--sets', 'x', '--no-tripo'])
        self.assertEqual(seen, [(False, False)])                     # removed at start; the worker went on to IDLE
        self.assertTrue(any('IDLE' in l for l in self.events()))

    def test_the_real_nap_wakes_for_its_own_request(self):
        import time
        self.my_stop()
        started = time.time()
        REAL_NAP(30)
        self.assertLess(time.time() - started, 2)


class DrainTests(Base):
    """DESIGN_PHASE4 I.10 (pre-cut-over review finding 1): no intake during a drain; a child's drain exit is never a failure."""

    def test_convert_drain_starts_no_job(self):
        (self.batch / 'CONVERT_DRAIN').write_text(json.dumps({'reason': 'legacy convert worker drain'}))
        self.assertEqual(cq.conversions_blocked(), 'CONVERT_DRAIN: legacy convert worker drain')
        self.stop_on_nap(1)
        with mock.patch.object(cq, 'next_job', side_effect=AssertionError('took a job')):
            cq.main(['--sets', 'x', '--no-tripo'])
        self.assertTrue(any('PAUSED (CONVERT_DRAIN: legacy convert worker drain)' in l for l in self.events()))
        (self.batch / 'CONVERT_HOLD').write_text(json.dumps({'reason': 'supervisor restart'}))
        self.assertEqual(cq.conversions_blocked(), 'CONVERT_HOLD: supervisor restart')       # the hold is named first

    def test_a_drain_exit_is_deferred_and_never_counted(self):
        self.code = cq.DRAIN_EXIT
        self.assertEqual(cq.DRAIN_EXIT, 76)
        jobs = iter([(self.job, True)])

        def next_job(sets):
            (self.batch / 'CONVERT_DRAIN').write_text('{}')                          # the drain begins while the child waits at its gate
            return next(jobs)
        self.stop_on_nap(1)
        with mock.patch.object(cq, 'next_job', side_effect=next_job):
            cq.main(['--sets', 'set_a', '--no-tripo'])
        self.assertEqual(len(self.converts), 1)                                      # no fallback backend, no second job
        self.assertFalse((self.batch / 'convert_locks' / 'set_a--abc.fails').exists())
        events = self.events()
        self.assertTrue(any(' DEFERRED set_a--abc: ' in l for l in events), events)
        self.assertFalse(any('FAILED' in l for l in events), events)
        self.assertTrue(any('PAUSED (CONVERT_DRAIN' in l for l in events), events)

    def test_a_drain_exit_on_the_tripo_path_reads_no_balance_again(self):
        self.code = cq.DRAIN_EXIT
        reads = []
        jobs = iter([(self.job, True)])

        def next_job(sets):
            (self.batch / 'CONVERT_DRAIN').write_text('{}')
            return next(jobs)
        self.stop_on_nap(1)
        with mock.patch.object(cq, 'next_job', side_effect=next_job), \
                mock.patch.object(cq, 'read_tripo_balance', lambda wait=None: reads.append(1) or (30, None)):
            cq.main(['--sets', 'set_a'])
        self.assertEqual((len(reads), self.converts), (1, [['--max-submits', '1']]))
        self.assertFalse((self.batch / 'convert_locks' / 'set_a--abc.fails').exists())

    def test_the_children_are_told_that_this_worker_knows_the_drain_exit(self):
        seen = {}

        def run(cmd, **kwargs):
            seen.update(kwargs.get('env') or {})
            return done()
        with mock.patch.object(cq, 'convert', REAL_CONVERT), mock.patch.object(cq, 'LOGS', self.batch / 'convert_logs'), \
                mock.patch.object(cq.subprocess, 'run', run):
            cq.convert(self.job, [])
        self.assertEqual(seen.get(cq.DRAIN_AWARE_ENV), '1')


class OwnerTests(Base):
    """Pre-cut-over review finding 2: a listed id authorizes a job only when its recorded owner agrees."""

    def stage(self, name, parent=None):
        job = self.root / 'multiview_ready' / name
        job.mkdir(parents=True)
        record = {'set': name.split('--')[0], 'job_id': name.split('--')[1], 'modules': [{'race_gender': 'human_male', 'module': 'outfit'}]}
        (job / '.tripo-job.json').write_text(json.dumps(record | ({'parent_set': parent} if parent else {})))
        return job

    def namespace(self, text):
        path = self.root / '.codex_jobs' / '_app' / 'set_namespace.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_the_reviewers_case_is_not_dispatched(self):
        self.stage('cape_human_male--abcdef', parent='cape_human_male')                 # an independent set's job
        self.namespace(json.dumps({'sets': {'cape': 'x'}, 'families': {'cape_human_male': 'cape'}}))
        sets = {'cape', 'cape_human_male'}                                              # the family id was written for cape
        self.assertEqual(cq.next_job(sets), (None, False))
        self.assertEqual(cq.next_job(sets), (None, False))
        owner = [l for l in self.events() if ' OWNER ' in l]
        self.assertEqual(len(owner), 1, owner)                                          # reported once
        self.assertIn('for the set cape', owner[0])

    def test_rules(self):
        self.assertEqual(cq.read_namespace(self.root), ({}, {}))                        # no preview app record: the list alone decides
        self.assertTrue(cq.owner_allows(self.root, 'a_gnome--1', {}, ['a_gnome'])[0])
        self.assertTrue(cq.owner_allows(self.root, 'a_gnome--1', {'parent_set': 'a'}, ['a', 'a_gnome'])[0])
        self.assertFalse(cq.owner_allows(self.root, 'a_gnome--1', {'parent_set': 'a'}, ['a_gnome'])[0])     # its parent is not listed
        self.assertFalse(cq.owner_allows(self.root, 'a_gnome--1', {'parent_set': 'a'}, ['a', 'a_gnome', '!a'])[0])
        self.assertFalse(cq.owner_allows(self.root, 'a_gnome--1', {'parent_set': 7}, ['a_gnome'])[0])
        self.namespace(json.dumps({'sets': {'a': 'x', 'b_gnome': 'x'}, 'families': {'a_gnome': 'a'}}))
        self.assertTrue(cq.owner_allows(self.root, 'a_gnome--1', {}, ['a', 'a_gnome'])[0])
        self.assertFalse(cq.owner_allows(self.root, 'a_gnome--1', {}, ['a_gnome'])[0])                      # registered for a: a must be listed
        self.assertFalse(cq.owner_allows(self.root, 'b_gnome--1', {'parent_set': 'b'}, ['b', 'b_gnome'])[0])   # b_gnome is a set of its own
        self.assertTrue(cq.owner_allows(self.root, 'b_gnome--1', {'parent_set': 'b_gnome'}, ['b_gnome'])[0])
        self.namespace('{broken')
        self.assertIsNone(cq.read_namespace(self.root))
        self.assertFalse(cq.owner_allows(self.root, 'a_gnome--1', {'parent_set': 'a'}, ['a', 'a_gnome'])[0])   # cannot be verified
        self.assertTrue(cq.owner_allows(self.root, 'a--1', {}, ['a'])[0])

    def test_an_owned_job_is_taken(self):
        job = self.stage('cape_human_male--abcdef', parent='cape')
        self.namespace(json.dumps({'sets': {'cape': 'x'}, 'families': {'cape_human_male': 'cape'}}))
        self.assertEqual(cq.next_job({'cape', 'cape_human_male'}), (job, True))


if __name__ == '__main__':
    unittest.main()
