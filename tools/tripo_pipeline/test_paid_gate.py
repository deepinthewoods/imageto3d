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


if __name__ == '__main__':
    unittest.main()
