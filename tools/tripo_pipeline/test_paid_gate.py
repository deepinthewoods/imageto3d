"""convert_job.py paid-step gate (DESIGN_PHASE4 I.1, review_v2_phase4 finding 2), Tripo browser lock (I.3, finding 8), the
click-time paid gate of the children (I.8, phase 5 finding 2) and the Tripo profile reservation (phase-4 finding 8).
Temp roots and a temp $XDG_DATA_HOME (profile); subprocess.run faked: nothing is submitted, collected or reduced for real. The
studio.mjs tests run node only on paths that exit before Playwright opens a browser.

    python -X utf8 -m pytest tools/tripo_pipeline/test_paid_gate.py -q
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import convert_job  # noqa: E402
import tripo_browser_lock as tbl  # noqa: E402


def lock_held(root):
    """True when the Tripo browser lock of `root` is held (probed through a fresh handle)."""
    path = tbl.lock_path(root)
    if not path.exists():
        return False
    with open(path, 'a+b') as handle:
        if tbl.try_lock(handle):
            tbl.release_lock(handle)
            return False
        return True


class Root(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.batch = self.root / '.codex_jobs' / '_batch'
        self.batch.mkdir(parents=True)
        patch = mock.patch.object(convert_job, 'ROOT', self.root)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(convert_job.portable, 'ROOT', self.root)   # record-lock sidecars stay in the temp root
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root / 'xdg')})   # never the real Tripo profile's lock
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('IMAGETO3D_PAID_GATE', None)

    def hold(self, reason='supervisor restart'):
        (self.batch / 'CONVERT_HOLD').write_text(json.dumps({'since': '2026-11-25T12:00:00', 'reason': reason,
                                                             'owner_pid': 1, 'owner_create_time': 1.0}))

    def pause(self, scope=('conversions',), text=None):
        (self.batch / 'PAUSE').write_text(text if text is not None else
                                          json.dumps({'since': 'x', 'reason': 'manual pause-conversions', 'scope': list(scope)}))


class GateTests(Root):
    def test_gate_files_follow_the_root(self):
        self.assertEqual(convert_job.gate_files(), (self.batch / 'PAUSE', self.batch / 'CONVERT_HOLD'))

    def test_gate_reasons(self):
        self.assertIsNone(convert_job.conversion_gate())
        self.pause(scope=('views',))                       # a views-only pause does not stop conversions
        self.assertIsNone(convert_job.conversion_gate())
        self.pause()
        self.assertEqual(convert_job.conversion_gate(), 'PAUSE: manual pause-conversions')
        self.pause(text='{broken')                         # unreadable: fail closed
        self.assertEqual(convert_job.conversion_gate(), 'PAUSE: supervisor pause')
        (self.batch / 'PAUSE').unlink()
        self.hold()
        self.assertEqual(convert_job.conversion_gate(), 'CONVERT_HOLD: supervisor restart')
        (self.batch / 'CONVERT_HOLD').write_text('')       # a hold is a hold, readable or not
        self.assertEqual(convert_job.conversion_gate(), 'CONVERT_HOLD: conversion hold')

    def test_wait_for_gate_blocks_until_the_hold_is_gone(self):
        self.hold()
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                (self.batch / 'CONVERT_HOLD').unlink()
        with mock.patch.object(convert_job.time, 'sleep', sleep), mock.patch('builtins.print') as printed:
            convert_job.wait_for_gate('human_male/outfit', 'a paid Hyper3D collect')
        self.assertEqual(len(sleeps), 2)
        lines = [c.args[0] for c in printed.call_args_list]
        self.assertEqual(sum('PAUSED (CONVERT_HOLD: supervisor restart)' in l for l in lines), 1)   # printed once

    def test_a_paid_studio_call_rechecks_the_gate_under_the_lock(self):
        reasons = iter([None, 'PAUSE: user active', None, None])   # open, closed once it holds the lock, open, open
        runs = []
        ran = mock.Mock(returncode=0)
        with mock.patch.object(convert_job, 'conversion_gate', lambda: next(reasons)), \
                mock.patch.object(convert_job.subprocess, 'run', lambda cmd, **kw: runs.append(lock_held(self.root)) or ran):
            self.assertIs(convert_job.run_studio(['node', 'studio.mjs', 'submit'], 'k', 'a new Tripo submission', paid=True), ran)
        self.assertEqual(runs, [True])                     # ran once, under the lock
        self.assertFalse(lock_held(self.root))

    def test_a_studio_call_waits_for_a_busy_tripo_browser(self):
        runs, entered = [], threading.Event()
        release = threading.Event()

        def holder():
            with tbl.tripo_browser_lock(self.root, what='v2 balance check'):
                entered.set()
                release.wait(30)
        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(entered.wait(30))
        threading.Timer(1.0, release.set).start()
        with mock.patch.object(convert_job.subprocess, 'run', lambda cmd, **kw: runs.append(1) or mock.Mock(returncode=0)), \
                mock.patch('builtins.print') as printed:
            convert_job.run_studio(['node', 'studio.mjs', 'collect'], 'k', 'Tripo collect', paid=False)
        thread.join(30)
        self.assertEqual(runs, [1])
        self.assertTrue(any('Tripo browser busy (held by pid' in c.args[0] for c in printed.call_args_list))


class ChildGateTests(Root):
    """Phase 5 finding 2 on the convert_job side: the paid children get the gate and the profile reservation; a child exit 4
    (it stopped at the gate before its click) is waited for and repeated, never counted as a failure."""

    def test_a_paid_tripo_submit_passes_the_gate_and_the_reservation_and_repeats_after_exit_4(self):
        runs, codes = [], iter([4, 0])
        hold = self.batch / 'CONVERT_HOLD'

        def run(cmd, env=None, **kw):
            runs.append(env)
            code = next(codes)
            if code == 4:
                hold.write_text('{"reason": "restart"}')   # the app held conversions while studio.mjs waited for the Studio queue
            return mock.Mock(returncode=code)
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            hold.unlink()
        with mock.patch.object(convert_job.subprocess, 'run', run), mock.patch.object(convert_job.time, 'sleep', sleep), \
                mock.patch('builtins.print'):
            result = convert_job.run_studio(['node', 'studio.mjs', 'submit'], 'k', 'a new Tripo submission', paid=True, check=True)
        self.assertEqual((result.returncode, len(runs), len(sleeps)), (0, 2, 1))   # waited for the gate, then ran again
        for env in runs:
            self.assertEqual(json.loads(env['IMAGETO3D_PAID_GATE'])['root'], str(self.root))
            self.assertEqual(Path(env[tbl.PROFILE_LOCK_ENV]), tbl.profile_lock_path(self.root / 'xdg' / tbl.PROFILE_NAME))
            self.assertTrue(env[tbl.PROFILE_TOKEN_ENV])

    def test_a_free_studio_call_gets_the_reservation_but_no_gate(self):
        seen = []
        with mock.patch.object(convert_job.subprocess, 'run', lambda cmd, env=None, **kw: seen.append(env) or mock.Mock(returncode=4)):
            result = convert_job.run_studio(['node', 'studio.mjs', 'collect'], 'k', 'Tripo collect', paid=False)
        self.assertEqual((result.returncode, len(seen)), (4, 1))                    # exit 4 of a free call is just its exit code
        self.assertNotIn('IMAGETO3D_PAID_GATE', seen[0])
        self.assertIn(tbl.PROFILE_LOCK_ENV, seen[0])

    def test_check_raises_on_other_failures(self):
        with mock.patch.object(convert_job.subprocess, 'run', lambda cmd, **kw: mock.Mock(returncode=1)):
            with self.assertRaises(subprocess.CalledProcessError):
                convert_job.run_studio(['node', 'studio.mjs', 'submit'], 'k', 'a new Tripo submission', paid=True, check=True)

    def test_a_gate_closed_only_by_the_childs_extra_command_is_waited_for_not_respawned(self):
        flag = self.root / 'lane_paused'
        flag.write_text('x')
        cmd = [sys.executable, '-c', f'import os, sys; print("lane paused"); sys.exit(1 if os.path.exists(r"{flag}") else 0)']
        codes, runs, sleeps = iter([4, 0]), [], []
        real_run = subprocess.run

        def fake_run(c, **kw):   # subprocess.run is one function: the gate's own cmd check must still really run
            if 'rodin_backend.py' not in c:
                return real_run(c, **kw)
            runs.append(kw['env'])
            return mock.Mock(returncode=next(codes))

        def sleep(seconds):
            sleeps.append(seconds)
            flag.unlink()
        with mock.patch.dict(os.environ, {'IMAGETO3D_PAID_GATE': json.dumps({'root': str(self.root), 'cmd': cmd})}), \
                mock.patch.object(convert_job.subprocess, 'run', fake_run), \
                mock.patch.object(convert_job.time, 'sleep', sleep), mock.patch('builtins.print') as printed:
            run = convert_job.run_paid_child(['python', 'rodin_backend.py', 'collect'], 'k', 'a paid Hyper3D collect', paid=True)
        self.assertEqual((run.returncode, len(runs), len(sleeps)), (0, 2, 1))
        self.assertTrue(any('PAUSED (lane paused)' in c.args[0] for c in printed.call_args_list))

    def test_an_inherited_gate_is_passed_on_unchanged(self):
        spec = json.dumps({'root': 'elsewhere', 'override_pause': True})
        with mock.patch.dict(os.environ, {'IMAGETO3D_PAID_GATE': spec}):
            self.assertEqual(convert_job.gate_env(), {'IMAGETO3D_PAID_GATE': spec})
        self.assertEqual(json.loads(convert_job.gate_env()['IMAGETO3D_PAID_GATE']), {'root': str(self.root), 'override_pause': False})


class MainTests(Root):
    """convert_job.main with a faked subprocess.run (studio.mjs / rodin_backend / Blender)."""

    def make_job(self, backend=None, status='submitted'):
        job = self.root / 'processing' / 's--j'
        (job / 'human_male' / 'outfit').mkdir(parents=True)
        (job / '.tripo-job.json').write_text(json.dumps(
            {'set': 's', 'job_id': 'j', 'modules': [{'race_gender': 'human_male', 'module': 'outfit'}]}))
        self.out = self.root / 'models/s/j/human_male/outfit'
        if backend:
            self.out.mkdir(parents=True)
            (self.out / 'studio_submission.json').write_text(json.dumps(
                {'status': status, 'project_id': 'P', **({'backend': backend} if backend != 'tripo' else {})}))
        return job

    def fake_run(self, commands, during=None, exits=None):
        out = lambda: self.out   # noqa: E731
        self.envs = []

        def run(cmd, **kw):
            joined = ' '.join(map(str, cmd))
            commands.append((joined, lock_held(self.root), convert_job.conversion_gate()))
            self.envs.append((joined, kw.get('env')))
            if during:
                during(joined)
            for part, codes in (exits or {}).items():
                if part in joined and codes:
                    return mock.Mock(returncode=codes.pop(0))
            record = out() / 'studio_submission.json'
            if ' submit ' in joined:
                out().mkdir(parents=True, exist_ok=True)
                record.write_text(json.dumps({'status': 'submitted', 'project_id': 'P',
                                              **({'backend': 'hyper3d'} if 'rodin_backend' in joined else {})}))
            elif ' collect ' in joined:
                (out() / 'x.glb').write_bytes(b'glTF')
                data = json.loads(record.read_text())
                data.update(status='exported', exports={'GLB': {'path': str(out() / 'x.glb')}})
                record.write_text(json.dumps(data))
            elif 'reduce_model.py' in joined:
                (out() / 'reduced').mkdir(parents=True, exist_ok=True)
                (out() / 'reduced/manifest.json').write_text(json.dumps({
                    'interior': {'interior_area_share': 0.0, 'threshold': 0.15},
                    'levels': {'0.4': {'triangles': 5, 'mesh_checks_pass': True, 'material_review_required': False}}}))
            return mock.Mock(returncode=0)
        return run

    def main(self, argv, commands, sleep=None, during=None, exits=None):
        patches = [mock.patch.object(convert_job.subprocess, 'run', self.fake_run(commands, during, exits)),
                   mock.patch.object(convert_job, 'winding_step', lambda out, module: {'status': 'done'}),
                   mock.patch.dict(os.environ, {'HYPER3D_VENV': str(self.root / 'venv')})]
        if sleep:
            patches.append(mock.patch.object(convert_job.time, 'sleep', sleep))
        for patch in patches:
            patch.start()
        try:
            convert_job.main(['s--j', '--no-align', *argv])
        finally:
            for patch in reversed(patches):
                patch.stop()

    def lift_after(self, n, path):
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == n:
                path.unlink()
        return sleeps, sleep

    def test_a_paid_hyper3d_collect_of_an_existing_submission_waits_for_the_hold(self):
        """The reviewer's case: the module was submitted before the restart; its paid collect must not run while
        CONVERT_HOLD exists (before the fix only a NEW submission waited)."""
        self.make_job(backend='hyper3d')
        self.hold()
        commands = []
        sleeps, sleep = self.lift_after(3, self.batch / 'CONVERT_HOLD')
        self.main(['--backend', 'hyper3d', '--allow-paid', '--accept-preview'], commands, sleep)
        self.assertEqual(len(sleeps), 3)
        collect = [c for c in commands if 'rodin_backend.py collect' in c[0]]
        self.assertEqual(len(collect), 1)
        self.assertIn('--allow-paid', collect[0][0])
        self.assertIsNone(collect[0][2])                   # the gate was open when it ran

    def test_a_paid_hyper3d_collect_waits_for_a_conversions_pause_too(self):
        self.make_job(backend='hyper3d')
        self.pause()
        commands = []
        sleeps, sleep = self.lift_after(1, self.batch / 'PAUSE')
        self.main(['--backend', 'hyper3d', '--allow-paid', '--accept-preview'], commands, sleep)
        self.assertEqual(len(sleeps), 1)
        self.assertTrue(all(c[2] is None for c in commands if 'collect' in c[0]))

    def test_an_unpaid_hyper3d_collect_does_not_wait(self):
        self.make_job(backend='hyper3d')
        self.hold()
        commands = []
        self.main(['--backend', 'hyper3d'], commands, sleep=lambda s: self.fail('waited'))
        self.assertTrue(any('rodin_backend.py collect' in c[0] for c in commands))

    def test_a_tripo_submission_waits_and_runs_under_the_tripo_lock(self):
        self.make_job()
        self.hold()
        commands = []
        sleeps, sleep = self.lift_after(2, self.batch / 'CONVERT_HOLD')
        self.main([], commands, sleep)
        submit = [c for c in commands if 'studio.mjs submit' in c[0]]
        collect = [c for c in commands if 'studio.mjs collect' in c[0]]
        self.assertEqual((len(submit), len(collect)), (1, 1))
        self.assertEqual((submit[0][1], submit[0][2]), (True, None))   # under the lock, gate open
        self.assertTrue(collect[0][1])                                  # collect under the lock too
        self.assertFalse(any(c[1] for c in commands if 'reduce_model.py' in c[0]))   # Blender steps never hold it
        self.assertFalse(lock_held(self.root))

    def test_the_free_tripo_collect_does_not_wait(self):
        self.make_job(backend='tripo')
        self.pause()
        commands = []
        self.main([], commands, sleep=lambda s: self.fail('waited'))
        collect = [c for c in commands if 'studio.mjs collect' in c[0]]
        self.assertEqual(len(collect), 1)
        self.assertTrue(collect[0][1])

    def test_a_hyper3d_collect_that_stopped_at_the_gate_waits_and_resumes(self):
        """The reviewer's scenario end to end on the convert_job side: CONVERT_HOLD appears while the rodin collect child waits for
        the provider stage; the child stops before the click with exit 4; convert_job waits for the gate and resumes the collect
        (no failure, no SystemExit)."""
        self.make_job(backend='hyper3d')
        commands, hold = [], self.batch / 'CONVERT_HOLD'

        def during(joined):
            if 'rodin_backend.py collect' in joined and not hold.exists() and len(commands) == 1:
                hold.write_text('{"reason": "supervisor restart"}')
        sleeps, sleep = self.lift_after(2, hold)
        self.main(['--backend', 'hyper3d', '--allow-paid', '--accept-preview'], commands, sleep, during,
                  exits={'rodin_backend.py collect': [4]})
        collects = [c for c in commands if 'rodin_backend.py collect' in c[0]]
        self.assertEqual(len(collects), 2)                 # stopped once at the gate, resumed once the hold was gone
        self.assertEqual(len(sleeps), 2)
        self.assertIsNone(collects[1][2])
        record = json.loads((self.root / 'processing' / 's--j' / '.tripo-job.json').read_text())
        self.assertEqual(record['state'], 'converted')
        envs = [env for joined, env in self.envs if 'rodin_backend.py' in joined]
        self.assertTrue(envs and all(json.loads(env['IMAGETO3D_PAID_GATE'])['root'] == str(self.root) for env in envs))

    def test_a_hyper3d_submission_waits_for_the_hold(self):
        self.make_job()
        self.hold()
        commands = []
        sleeps, sleep = self.lift_after(1, self.batch / 'CONVERT_HOLD')
        self.main(['--backend', 'hyper3d', '--allow-paid', '--accept-preview'], commands, sleep)
        self.assertEqual(len(sleeps), 1)
        submits = [c for c in commands if 'rodin_backend.py submit' in c[0]]
        self.assertEqual(len(submits), 1)
        self.assertIsNone(submits[0][2])
        self.assertFalse(submits[0][1])                    # Hyper3D steps do not take the Tripo lock


STUDIO = Path(__file__).resolve().parent / 'studio.mjs'
LOCK_TOOL = Path(__file__).resolve().parent / 'tripo_browser_lock.py'


class ProfileReservationTests(unittest.TestCase):
    """Phase-4 finding 8: the Tripo profile is reserved by its RESOLVED path (one reservation for every checkout), and studio.mjs
    opens it only under a verified reservation (taking one itself when started without)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.xdg = self.base / 'xdg'
        self.profile = self.xdg / tbl.PROFILE_NAME
        env = mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(self.xdg)})
        env.start()
        self.addCleanup(env.stop)
        for name in (tbl.PROFILE_LOCK_ENV, tbl.PROFILE_TOKEN_ENV, 'IMAGETO3D_PAID_GATE'):
            os.environ.pop(name, None)

    def test_the_default_profile_matches_studio_mjs(self):
        self.assertEqual(tbl.default_profile(), self.profile)
        with mock.patch.dict(os.environ):
            os.environ.pop('XDG_DATA_HOME')
            self.assertEqual(tbl.default_profile(), Path(os.path.expanduser('~')) / '.local' / 'share' / tbl.PROFILE_NAME)

    def test_two_checkouts_share_one_reservation(self):
        a, b = self.base / 'checkout_a', self.base / 'checkout_b'
        with tbl.tripo_browser_lock(a, what='checkout a') as env:
            self.assertEqual(Path(env[tbl.PROFILE_LOCK_ENV]), tbl.profile_lock_path(self.profile))
            with self.assertRaisesRegex(tbl.TripoBrowserBusy, 'checkout a'):
                with tbl.tripo_browser_lock(b, what='checkout b', wait=0):
                    pass
            with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(self.base / 'other_xdg')}):
                with tbl.tripo_browser_lock(b, what='another profile', wait=0):   # a different profile is not blocked
                    pass
        with tbl.tripo_browser_lock(b, wait=0):
            pass

    def test_the_key_is_the_resolved_path(self):
        spelled = self.xdg / 'sub' / '..' / tbl.PROFILE_NAME
        self.assertEqual(tbl.profile_lock_path(spelled), tbl.profile_lock_path(self.profile))
        with tbl.profile_reservation(self.profile):
            with self.assertRaises(tbl.TripoBrowserBusy):
                with tbl.profile_reservation(spelled, wait=0):
                    pass

    def test_verify(self):
        with tbl.profile_reservation(self.profile) as env:
            lock, token = env[tbl.PROFILE_LOCK_ENV], env[tbl.PROFILE_TOKEN_ENV]
            self.assertIsNone(tbl.verify(self.profile, lock, token))
            self.assertIn('another token', tbl.verify(self.profile, lock, 'f' * 32))
            self.assertIn('is not the one of profile', tbl.verify(self.base / 'elsewhere' / tbl.PROFILE_NAME, lock, token))
            self.assertIn('no reservation', tbl.verify(self.profile, '', ''))
        self.assertIsNotNone(tbl.verify(self.profile, lock, token))       # released: the token is cleared / not held

    def test_run_gives_the_command_a_verified_reservation_and_reports_busy(self):
        probe = ('import os, subprocess, sys; sys.exit(subprocess.call([sys.executable, r"%s", "verify", "--profile", r"%s", '
                 '"--lock", os.environ["%s"], "--token", os.environ["%s"]]))'
                 % (LOCK_TOOL, self.profile, tbl.PROFILE_LOCK_ENV, tbl.PROFILE_TOKEN_ENV))
        run = subprocess.run([sys.executable, str(LOCK_TOOL), 'run', '--profile', str(self.profile), '--', sys.executable, '-c',
                              probe], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        with tbl.profile_reservation(self.profile, what='a test holder'):
            run = subprocess.run([sys.executable, str(LOCK_TOOL), 'run', '--profile', str(self.profile), '--wait', '0', '--',
                                  sys.executable, '-c', 'print("ran")'], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, tbl.EXIT_BUSY)
        self.assertNotIn('ran', run.stdout)
        self.assertIn('a test holder', run.stderr)

    @unittest.skipUnless(shutil.which('node'), 'needs node')
    def test_studio_mjs_refuses_an_unverified_reservation(self):
        env = {**os.environ, tbl.PROFILE_LOCK_ENV: str(tbl.profile_lock_path(self.profile)), tbl.PROFILE_TOKEN_ENV: 'bogus'}
        run = subprocess.run(['node', str(STUDIO), 'balance'], capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn('Tripo profile reservation refused', run.stderr)

    @unittest.skipUnless(shutil.which('node'), 'needs node')
    def test_studio_mjs_without_a_reservation_takes_one_and_waits_for_a_busy_profile(self):
        """A direct `node studio.mjs balance` (the preview app's check, a manual run) re-runs itself under the reservation; with the
        profile reserved by another checkout's holder it never opens the profile (exit 75, no browser)."""
        with tbl.tripo_browser_lock(self.base / 'other_checkout', what='other checkout convert_job'):
            run = subprocess.run(['node', str(STUDIO), 'balance'], capture_output=True, text=True, timeout=120,
                                 env={**os.environ, tbl.PROFILE_WAIT_ENV: '0'})
        self.assertEqual(run.returncode, tbl.EXIT_BUSY, run.stdout + run.stderr)
        self.assertIn('other checkout convert_job', run.stderr)
        self.assertEqual(run.stdout.strip(), '')


class BrowserLifetimeTests(unittest.TestCase):
    """Recheck-before-cutover finding 18: the reservation must not end while node / Firefox still has the profile open. The
    holder's byte lock dies with the holder, so studio.mjs registers its pid beside the profile and the next acquirer waits for
    that process (or a Firefox on the profile) to exit. Stand-in processes only: no node, no browser."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.profile = self.base / 'xdg' / tbl.PROFILE_NAME
        self.profile.parent.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(self.base / 'xdg')})
        env.start()
        self.addCleanup(env.stop)
        self.pid_file = tbl.browser_pid_path(self.profile)

    def sleeper(self, *extra):
        """A stand-in for a surviving node / Firefox process."""
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)', *extra])
        self.addCleanup(lambda: (child.kill(), child.wait()))
        return child

    def stop(self, child):
        child.kill()
        child.wait()

    def acquire(self, wait=0.3):
        with tbl.profile_reservation(self.profile, wait=wait, announce=lambda text: None):
            return True

    def test_the_pid_file_is_beside_the_profile_like_the_lock(self):
        self.assertEqual(self.pid_file.parent, tbl.profile_lock_path(self.profile).parent)
        self.assertEqual(self.pid_file.name, f'{tbl.PROFILE_NAME}.imageto3d.pid')
        self.assertEqual(tbl.browser_pid_path(self.base / 'xdg' / 'x' / '..' / tbl.PROFILE_NAME), self.pid_file)

    def test_a_registered_node_that_outlived_its_holder_keeps_the_profile_busy_until_it_exits(self):
        node = self.sleeper()
        self.assertEqual(tbl._record_browser(self.profile, node.pid), self.pid_file)
        data = json.loads(self.pid_file.read_text())
        self.assertEqual(data['pid'], node.pid)
        self.assertIsInstance(data['create_time'], float)
        started = time.monotonic()
        with self.assertRaisesRegex(tbl.TripoBrowserBusy, f'node pid {node.pid} left by a holder that is gone'):
            self.acquire(wait=0.3)                                   # the byte lock is free, the profile is not
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(self.pid_file.exists())
        with self.assertRaises(tbl.TripoBrowserBusy):
            with tbl.tripo_browser_lock(self.base / 'checkout', wait=0, announce=lambda text: None):   # convert_job's entry point
                pass
        self.stop(node)
        self.assertTrue(self.acquire())                              # drained: the next acquirer gets it
        self.assertFalse(self.pid_file.exists())                     # and the stale file is gone

    def test_the_waiter_gets_the_profile_as_soon_as_the_orphan_exits(self):
        node = self.sleeper()
        tbl._record_browser(self.profile, node.pid)
        threading.Timer(0.5, lambda: self.stop(node)).start()
        with mock.patch.object(tbl, 'POLL_S', 0.1):
            self.assertTrue(self.acquire(wait=30))

    def test_a_reused_pid_is_not_mistaken_for_the_registered_process(self):
        other = self.sleeper()
        tbl._record_browser(self.profile, other.pid)
        data = json.loads(self.pid_file.read_text())
        data['create_time'] -= 3600                                  # the registered process started an hour before this one
        self.pid_file.write_text(json.dumps(data))
        self.assertEqual(tbl.profile_users(self.profile), [])
        self.assertFalse(self.pid_file.exists())

    def test_a_firefox_left_on_the_profile_by_a_dead_node_keeps_it_busy(self):
        node = self.sleeper()
        tbl._record_browser(self.profile, node.pid)
        self.stop(node)                                              # node is gone, its browser is not
        with mock.patch.object(tbl, 'BROWSER_NAMES', ('python',)):   # the stand-in "browser" is a python process
            browser = self.sleeper('-no-remote', '-profile', str(self.profile))
            elsewhere = self.sleeper('-profile', str(self.base / 'another-profile'))
            users = tbl.profile_users(self.profile)
            self.assertEqual(len(users), 1, users)
            self.assertIn(f'pid {browser.pid} still on the profile', users[0])
            with self.assertRaisesRegex(tbl.TripoBrowserBusy, 'still on the profile'):
                self.acquire(wait=0.2)
            self.assertTrue(self.pid_file.exists())
            self.stop(browser)
            self.assertTrue(self.acquire())                          # a browser on ANOTHER profile does not count
            self.assertFalse(self.pid_file.exists())
            self.assertIsNone(elsewhere.poll())

    def test_without_a_pid_file_nothing_is_scanned(self):
        with mock.patch.object(tbl, '_processes', side_effect=AssertionError('scanned')):
            self.assertEqual(tbl.profile_users(self.profile), [])
            self.assertTrue(self.acquire(wait=0))

    def test_a_pid_file_that_cannot_be_checked_fails_closed(self):
        node = self.sleeper()
        tbl._record_browser(self.profile, node.pid)
        with mock.patch.object(tbl, '_create_time', return_value='unknown'):
            self.assertIn('cannot check it', tbl.profile_users(self.profile)[0])
        self.stop(node)
        with mock.patch.object(tbl, '_processes', return_value=None):
            self.assertIn('cannot be listed', tbl.profile_users(self.profile)[0])
        self.assertTrue(self.pid_file.exists())
        self.pid_file.write_text('{half')                            # damaged: decided by the browser scan
        self.assertEqual(tbl.profile_users(self.profile), [])
        self.assertFalse(self.pid_file.exists())

    def test_unregister_only_removes_the_callers_own_registration(self):
        node = self.sleeper()
        tbl._record_browser(self.profile, node.pid)
        self.assertFalse(tbl.unregister_browser(self.profile, node.pid + 1))
        self.assertTrue(self.pid_file.exists())
        self.assertTrue(tbl.unregister_browser(self.profile, node.pid))
        self.assertFalse(tbl.unregister_browser(self.profile, node.pid))
        with self.assertRaises(ValueError):
            dead = subprocess.run([sys.executable, '-c', 'import os; print(os.getpid())'], capture_output=True, text=True)
            tbl._record_browser(self.profile, int(dead.stdout))

    def test_verify_registers_only_a_valid_reservation(self):
        node = self.sleeper()
        bad = subprocess.run([sys.executable, str(LOCK_TOOL), 'verify', '--profile', str(self.profile), '--lock',
                              str(tbl.profile_lock_path(self.profile)), '--token', 'bogus', '--register-pid', str(node.pid)],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(bad.returncode, 1)
        self.assertFalse(self.pid_file.exists())
        with tbl.profile_reservation(self.profile) as env:
            run = subprocess.run([sys.executable, str(LOCK_TOOL), 'verify', '--profile', str(self.profile), '--lock',
                                  env[tbl.PROFILE_LOCK_ENV], '--token', env[tbl.PROFILE_TOKEN_ENV], '--register-pid', str(node.pid)],
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual(Path(run.stdout.strip().splitlines()[-1]), self.pid_file)   # studio.mjs reads the path from stdout
            self.assertEqual(json.loads(self.pid_file.read_text())['pid'], node.pid)
        users = subprocess.run([sys.executable, str(LOCK_TOOL), 'users', '--profile', str(self.profile)], capture_output=True, text=True)
        self.assertEqual(users.returncode, 1)
        self.assertIn(f'node pid {node.pid}', users.stdout)

    def test_the_reviewers_case_the_holder_dies_and_its_child_still_runs(self):
        """`run` (the holder) spawns a child that registers itself like studio.mjs and keeps running; the holder is killed.
        The kernel frees the byte lock, but the next `run` reports busy until the child exits."""
        child_code = ('import os, subprocess, sys, time\n'
                      'r = subprocess.run([sys.executable, sys.argv[1], "verify", "--profile", sys.argv[2], "--lock", os.environ["%s"], '
                      '"--token", os.environ["%s"], "--register-pid", str(os.getpid())], capture_output=True, text=True)\n'
                      'print("registered" if r.returncode == 0 else "refused " + r.stdout, os.getpid(), flush=True)\n'
                      'time.sleep(120)\n' % (tbl.PROFILE_LOCK_ENV, tbl.PROFILE_TOKEN_ENV))
        holder = subprocess.Popen([sys.executable, str(LOCK_TOOL), 'run', '--profile', str(self.profile), '--', sys.executable, '-c',
                                   child_code, str(LOCK_TOOL), str(self.profile)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdout.close()))
        word, pid = holder.stdout.readline().split()[:2]
        self.assertEqual(word, 'registered')
        child_pid = int(pid)
        self.addCleanup(lambda: subprocess.run(['taskkill', '/F', '/PID', str(child_pid)] if os.name == 'nt'
                                               else ['kill', '-9', str(child_pid)], capture_output=True))
        with self.assertRaises(tbl.TripoBrowserBusy):                # the live holder has it
            self.acquire(wait=0)
        holder.kill()                                                # the holder dies; its child (the "node") survives
        holder.wait()
        self.assertIsNotNone(tbl._create_time(child_pid))
        again = subprocess.run([sys.executable, str(LOCK_TOOL), 'run', '--profile', str(self.profile), '--wait', '0.5', '--',
                                sys.executable, '-c', 'print("ran")'], capture_output=True, text=True, timeout=60)
        self.assertEqual(again.returncode, tbl.EXIT_BUSY, again.stdout + again.stderr)
        self.assertNotIn('ran', again.stdout)
        self.assertIn(f'node pid {child_pid}', again.stderr)
        subprocess.run(['taskkill', '/F', '/PID', str(child_pid)] if os.name == 'nt' else ['kill', '-9', str(child_pid)],
                       capture_output=True)
        deadline = time.monotonic() + 20
        while tbl._create_time(child_pid) is not None and time.monotonic() < deadline:
            time.sleep(0.1)
        again = subprocess.run([sys.executable, str(LOCK_TOOL), 'run', '--profile', str(self.profile), '--wait', '5', '--',
                                sys.executable, '-c', 'print("ran")'], capture_output=True, text=True, timeout=60)
        self.assertEqual((again.returncode, again.stdout.strip()), (0, 'ran'), again.stderr)

    def test_registration_is_one_handshake_with_acquisition_the_reviewers_sequence(self):
        """Final-verify row 18: the old verifier passed, its holder exited, a replacement acquired the profile; the old verifier
        must not register any more (python API and the command studio.mjs runs)."""
        node = self.sleeper()
        old = tbl.profile_reservation(self.profile, what='old holder')
        env = old.__enter__()
        lock, token = env[tbl.PROFILE_LOCK_ENV], env[tbl.PROFILE_TOKEN_ENV]
        self.assertIsNone(tbl.verify(self.profile, lock, token))            # the old verifier passed
        old.__exit__(None, None, None)                                      # its holder exited
        with tbl.profile_reservation(self.profile, what='replacement', wait=0) as fresh:   # a replacement acquired the profile
            self.assertNotEqual(fresh[tbl.PROFILE_TOKEN_ENV], token)
            with self.assertRaisesRegex(tbl.ReservationRefused, 'another token'):
                tbl.register_browser(self.profile, node.pid, lock, token)
            run = subprocess.run([sys.executable, str(LOCK_TOOL), 'verify', '--profile', str(self.profile), '--lock', lock,
                                  '--token', token, '--register-pid', str(node.pid)], capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
            self.assertIn('another token', run.stdout)
            self.assertFalse(self.pid_file.exists())                        # nothing was published
            # the replacement's own child registers through the reservation that is live now
            self.assertEqual(tbl.register_browser(self.profile, node.pid, fresh[tbl.PROFILE_LOCK_ENV], fresh[tbl.PROFILE_TOKEN_ENV]),
                             self.pid_file)
        with self.assertRaisesRegex(tbl.ReservationRefused, 'another token|not held'):   # ended: its token is void too
            tbl.register_browser(self.profile, node.pid, fresh[tbl.PROFILE_LOCK_ENV], fresh[tbl.PROFILE_TOKEN_ENV])

    def test_a_killed_holders_token_is_void_once_a_replacement_holds_the_lock(self):
        """A holder that is KILLED never clears its token. The replacement voids it the moment it takes the byte lock, even while
        it still waits for an orphan to leave the profile, so the dead holder's child cannot register behind it."""
        child_code = ('import os, time\nprint(os.environ["%s"], os.environ["%s"], flush=True)\ntime.sleep(120)\n'
                      % (tbl.PROFILE_LOCK_ENV, tbl.PROFILE_TOKEN_ENV))
        holder = subprocess.Popen([sys.executable, str(LOCK_TOOL), 'run', '--profile', str(self.profile), '--', sys.executable, '-c',
                                   child_code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdout.close()))
        lock, token = holder.stdout.readline().split()[:2]
        self.assertIsNone(tbl.verify(self.profile, lock, token))            # valid while the holder lives
        orphan, late = self.sleeper(), self.sleeper()
        tbl._record_browser(self.profile, orphan.pid)                       # something still on the profile
        holder.kill()
        holder.wait()
        self.assertIn(f'token {token} ', self.profile_lock_text())          # killed: the token is still in the file
        entered, done = threading.Event(), threading.Event()

        def replacement():
            with mock.patch.object(tbl, 'POLL_S', 0.05):
                with tbl.profile_reservation(self.profile, what='replacement', wait=30, announce=lambda text: entered.set()):
                    done.wait(20)

        thread = threading.Thread(target=replacement, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(20))                                   # it holds the byte lock and waits for the orphan
        with self.assertRaisesRegex(tbl.ReservationRefused, 'another token'):
            tbl.register_browser(self.profile, late.pid, lock, token)       # the dead holder's child: refused already
        self.assertEqual(json.loads(self.pid_file.read_text())['pid'], orphan.pid)
        self.stop(orphan)
        deadline = time.monotonic() + 20
        while ' replacement' not in self.profile_lock_text() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn(' replacement', self.profile_lock_text())             # the replacement finished acquiring
        with self.assertRaises(tbl.ReservationRefused):
            tbl.register_browser(self.profile, late.pid, lock, token)
        self.assertFalse(self.pid_file.exists())
        done.set()
        thread.join(20)

    def profile_lock_text(self):
        return tbl.profile_lock_path(self.profile).read_text(encoding='utf-8', errors='replace')

    def test_acquisition_and_registration_exclude_each_other(self):
        """While a handshake is under way (here: held by the test) nobody acquires, registers or gets a `valid` answer."""
        node = self.sleeper()
        with tbl.profile_reservation(self.profile) as env:
            with mock.patch.object(tbl, 'HANDSHAKE_WAIT_S', 0.2):
                with tbl._handshake(self.profile) as held:
                    self.assertTrue(held)
                    with self.assertRaisesRegex(tbl.ReservationRefused, 'handshake is busy'):
                        tbl.register_browser(self.profile, node.pid, env[tbl.PROFILE_LOCK_ENV], env[tbl.PROFILE_TOKEN_ENV])
                    self.assertIn('handshake is busy', tbl.verify(self.profile, env[tbl.PROFILE_LOCK_ENV], env[tbl.PROFILE_TOKEN_ENV]))
                    self.assertFalse(self.pid_file.exists())
        with tbl._handshake(self.profile):
            started = time.monotonic()
            with self.assertRaisesRegex(tbl.TripoBrowserBusy, 'handshake'):
                self.acquire(wait=0)
            self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(self.acquire(wait=0))
        self.assertEqual(tbl.handshake_lock_path(self.profile).name, f'{tbl.PROFILE_NAME}.imageto3d.handshake.lock')

    def test_studio_mjs_registers_itself_and_unregisters_only_after_a_normal_end(self):
        text = STUDIO.read_text(encoding='utf-8')
        self.assertIn("'--register-pid',String(process.pid)", text)
        self.assertLess(text.index("'--register-pid',String(process.pid)"), text.index('async function openStudio'))   # before any launch
        tail = text.rstrip().splitlines()
        self.assertEqual(tail[-1].split('(')[0], 'unregisterBrowser')                # the last statement, after the awaited command
        self.assertNotIn("process.on('exit'", text)                                  # an error exit keeps the registration


class DrainTests(Root):
    """DESIGN_PHASE4 I.10 (pre-cut-over review finding 1): a process blocked at the paid gate records that, and leaves for a drain."""

    def setUp(self):
        super().setUp()
        self.job = self.root / 'processing' / 'set_a--abc'
        self.job.mkdir(parents=True)
        self.record = self.job / '.tripo-job.json'
        self.record.write_text(json.dumps({'set': 'set_a', 'job_id': 'abc', 'modules': []}))
        self.lock = self.root / 'models' / 'set_a' / 'abc' / '.module_locks' / 'human_male__outfit.lock'
        run = mock.patch.dict(convert_job._RUN, {'job': 'set_a--abc', 'record': self.record, 'lock': None})
        run.start()
        self.addCleanup(run.stop)
        os.environ.pop(convert_job.DRAIN_AWARE_ENV, None)

    def drain(self):
        (self.batch / 'CONVERT_DRAIN').write_text(json.dumps({'reason': 'legacy convert worker drain'}))

    def exits(self):
        return [json.loads(p.read_text()) for p in sorted((self.batch / 'drain_exits').glob('*.json'))]

    def test_the_gate_wait_record_exists_only_while_it_waits(self):
        self.hold()
        seen = []

        def sleep(seconds):
            seen.append(json.loads(convert_job.gate_wait_path().read_text()))
            (self.batch / 'CONVERT_HOLD').unlink()
        with mock.patch.object(convert_job.time, 'sleep', sleep), mock.patch('builtins.print'):
            convert_job.wait_for_gate('human_male/outfit', 'a new submission')
        self.assertEqual((seen[0]['pid'], seen[0]['job'], seen[0]['module'], seen[0]['step']),
                         (os.getpid(), 'set_a--abc', 'human_male/outfit', 'a new submission'))
        self.assertFalse(convert_job.gate_wait_path().exists())
        self.assertFalse((self.batch / 'drain_exits').exists())                      # no drain: it simply went on

    def test_a_gate_waiter_leaves_for_a_drain_deferred_and_without_its_lock(self):
        self.hold('legacy convert worker drain')
        self.assertTrue(convert_job.claim_module(self.lock))
        convert_job._RUN['lock'] = self.lock
        (self.batch / 'convert_locks').mkdir()
        (self.batch / 'convert_locks' / 'set_a--abc.fails').write_text('1')

        def sleep(seconds):
            self.drain()                                                             # the drain begins while it waits
        with mock.patch.object(convert_job.time, 'sleep', sleep), mock.patch('builtins.print') as printed, \
                self.assertRaises(SystemExit) as left:
            convert_job.wait_for_gate('human_male/outfit', 'a paid Hyper3D collect')
        self.assertEqual(left.exception.code, convert_job.DRAIN_EXIT)
        self.assertEqual(convert_job.DRAIN_EXIT, 76)                                 # never 75 (a busy Tripo browser profile)
        self.assertFalse(self.lock.exists())                                         # module lock released
        self.assertFalse(convert_job.gate_wait_path().exists())
        deferred = json.loads(self.record.read_text())['deferred']['human_male/outfit']
        self.assertEqual((deferred['step'], deferred['pid']), ('a paid Hyper3D collect', os.getpid()))
        self.assertNotIn('state', json.loads(self.record.read_text()))               # not a failure, no state change
        (entry,) = self.exits()
        self.assertEqual((entry['job'], entry['module'], entry['fails_before'], entry['parent_aware'], entry['lock_released']),
                         ('set_a--abc', 'human_male/outfit', 1, False, True))
        self.assertTrue(any('DEFERRED' in c.args[0] for c in printed.call_args_list))

    def test_a_drain_alone_closes_the_gate_and_a_current_worker_is_named_aware(self):
        self.drain()                                                                 # no hold (a stale drain): still no paid step
        self.assertEqual(convert_job.conversion_gate(), 'CONVERT_DRAIN: legacy convert worker drain')
        with mock.patch.dict(os.environ, {convert_job.DRAIN_AWARE_ENV: '1'}), mock.patch('builtins.print'), \
                self.assertRaises(SystemExit) as left:
            convert_job.wait_for_gate('human_male/outfit', 'a new submission')       # before the module lock is claimed
        self.assertEqual(left.exception.code, convert_job.DRAIN_EXIT)
        (entry,) = self.exits()
        self.assertEqual((entry['parent_aware'], entry['lock_released'], entry['fails_before']), (True, False, 0))

    def test_a_child_stopped_at_the_click_gate_leaves_for_a_drain_too(self):
        self.hold()
        self.drain()
        env = dict(os.environ, **convert_job.paid_gate.env_for(self.root))
        with mock.patch('builtins.print'), self.assertRaises(SystemExit) as left:
            convert_job.wait_for_child_gate('human_male/outfit', 'a new Tripo submission', env)
        self.assertEqual(left.exception.code, convert_job.DRAIN_EXIT)
        self.assertEqual(len(self.exits()), 1)

    def test_without_a_drain_a_paused_step_keeps_waiting(self):
        self.pause()                                                                 # the user's pause: wait, never leave
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                (self.batch / 'PAUSE').unlink()
        with mock.patch.object(convert_job.time, 'sleep', sleep), mock.patch('builtins.print'):
            convert_job.wait_for_gate('human_male/outfit', 'a new submission')
        self.assertEqual(len(sleeps), 3)
        self.assertNotIn('deferred', json.loads(self.record.read_text()))

    def test_a_drain_alone_stops_the_final_check_before_the_child_and_the_child_gate(self):
        """Final-verify new finding 1: with ONLY CONVERT_DRAIN (the hold is gone) no paid child is started; a step that was past
        the outer check when the drain began is stopped by the check under the browser lock and leaves with DRAIN_EXIT."""
        self.drain()
        self.assertIn('CONVERT_DRAIN', convert_job.paid_gate.closed_reason(convert_job.gate_env()))   # the child's own gate
        with mock.patch.object(convert_job.subprocess, 'run', side_effect=AssertionError('a paid child was started')), \
                mock.patch('builtins.print'), self.assertRaises(SystemExit) as left:
            convert_job.run_studio(['node', 'studio.mjs', 'submit'], 'human_male/outfit', 'a new Tripo submission', paid=True)
        self.assertEqual(left.exception.code, convert_job.DRAIN_EXIT)
        (self.batch / 'CONVERT_DRAIN').unlink()
        real_gate, looks = convert_job.conversion_gate, []

        def gate():                                       # open at the outer wait; the drain begins before the final check
            looks.append(1)
            if len(looks) == 2:
                self.drain()
            return real_gate()
        with mock.patch.object(convert_job, 'conversion_gate', gate), \
                mock.patch.object(convert_job.subprocess, 'run', side_effect=AssertionError('a paid child was started')), \
                mock.patch('builtins.print'), self.assertRaises(SystemExit) as left:
            convert_job.run_studio(['node', 'studio.mjs', 'submit'], 'human_male/outfit', 'a new Tripo submission', paid=True)
        self.assertEqual(left.exception.code, convert_job.DRAIN_EXIT)
        self.assertEqual(len(self.exits()), 2)
        # a child that stopped at its click gate (exit 4) because of the drain alone: the parent leaves too, it never respawns
        env = dict(os.environ, **convert_job.paid_gate.env_for(self.root))
        with mock.patch('builtins.print'), self.assertRaises(SystemExit) as left:
            convert_job.wait_for_child_gate('human_male/outfit', 'a new Tripo submission', env)
        self.assertEqual(left.exception.code, convert_job.DRAIN_EXIT)
        with mock.patch.object(convert_job.subprocess, 'run', side_effect=AssertionError('a paid child was started')), \
                mock.patch('builtins.print'), self.assertRaises(SystemExit) as left:
            convert_job.run_paid_child(['python', 'rodin_backend.py', 'collect'], 'human_male/outfit', 'a paid Hyper3D collect', paid=True)
        self.assertEqual(left.exception.code, convert_job.DRAIN_EXIT)

    def test_record_locks_follow_the_records_own_root_when_the_caller_works_elsewhere(self):
        """Final-verify test isolation: convert_queue / the v2 app import this module with the real ROOT and work on a fixture
        root; the lock sidecar of a fixture record must be in the fixture root, never in the checkout convert_job was imported
        from. Here `checkout` stands for that import root."""
        checkout = self.root / 'checkout'
        (checkout / 'processing' / 'own--1').mkdir(parents=True)
        own = checkout / 'processing' / 'own--1' / '.tripo-job.json'
        own.write_text('{}')
        with mock.patch.object(convert_job, 'ROOT', checkout), mock.patch.object(convert_job.portable, 'ROOT', checkout):
            self.assertEqual(convert_job.record_root(own), checkout)                          # a job of this checkout
            self.assertEqual(convert_job.record_root(self.record), self.root)                 # a fixture job elsewhere
            convert_job.update_record(self.record, lambda r: r.__setitem__('y', 2))
            self.assertEqual(convert_job.read_record(self.record)['y'], 2)
            convert_job.update_record(own, lambda r: r.__setitem__('z', 3))
            if os.name == 'nt':
                self.assertEqual([p.name for p in (checkout / '.codex_jobs' / '_batch' / 'record_locks').iterdir()],
                                 ['own--1__.tripo-job.json.lock'])
                self.assertTrue(convert_job.portable.record_lock_path(self.record, self.root).is_file())
            else:
                self.assertFalse((checkout / '.codex_jobs').exists())

    def test_record_locks_stay_in_the_root_of_the_job(self):
        convert_job.update_record(self.record, lambda r: r.__setitem__('x', 1))
        self.assertEqual(convert_job.read_record(self.record)['x'], 1)


if __name__ == '__main__':
    unittest.main()
