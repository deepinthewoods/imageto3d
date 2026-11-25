"""convert_job.py paid-step gate (DESIGN_PHASE4 I.1, review_v2_phase4 finding 2) and Tripo browser lock (I.3, finding 8).
Temp roots, subprocess.run faked: nothing is submitted, collected or reduced for real.

    python -X utf8 -m pytest tools/tripo_pipeline/test_paid_gate.py -q
"""
import json
import os
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
        with mock.patch.object(convert_job, 'conversion_gate', lambda: next(reasons)), \
                mock.patch.object(convert_job.subprocess, 'run', lambda cmd, **kw: runs.append(lock_held(self.root)) or 'ran'):
            self.assertEqual(convert_job.run_studio(['node', 'studio.mjs', 'submit'], 'k', 'a new Tripo submission', paid=True), 'ran')
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
        with mock.patch.object(convert_job.subprocess, 'run', lambda cmd, **kw: runs.append(1) or 'ran'), \
                mock.patch('builtins.print') as printed:
            convert_job.run_studio(['node', 'studio.mjs', 'collect'], 'k', 'Tripo collect', paid=False)
        thread.join(30)
        self.assertEqual(runs, [1])
        self.assertTrue(any('Tripo browser busy (held by pid' in c.args[0] for c in printed.call_args_list))


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

    def fake_run(self, commands, during=None):
        out = lambda: self.out   # noqa: E731

        def run(cmd, **kw):
            joined = ' '.join(map(str, cmd))
            commands.append((joined, lock_held(self.root), convert_job.conversion_gate()))
            if during:
                during(joined)
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

    def main(self, argv, commands, sleep=None, during=None):
        patches = [mock.patch.object(convert_job.subprocess, 'run', self.fake_run(commands, during)),
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


if __name__ == '__main__':
    unittest.main()
