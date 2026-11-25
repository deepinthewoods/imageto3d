"""Offline tests for the `"launcher": "monsters"` task field of tools/view_batch.py (temp roots, a fake launcher script).

  python -m unittest tools/test_view_batch_launcher.py        (from the imageto3d root)
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import view_batch  # noqa: E402

REAL_SLEEP = time.sleep
BASH = view_batch.SHELL[0] if view_batch.SHELL else shutil.which('bash')
FAKE_LAUNCHER = """#!/usr/bin/env bash
# fake monsters/aaa/tools/codex_imagegen.sh: writes the output named on the prompt's first line, like a finished Codex job
ROOT=$(cd "$(dirname "$0")/.." && pwd)
job=$1; prompt=$2; shift 2
logdir="$ROOT/.codex_jobs/$job"
[[ -e "$logdir" ]] && { echo "job dir exists" >&2; exit 2; }
mkdir -p "$logdir"
pwd > "$logdir/cwd.txt"
printf '%s\\n' "$@" > "$logdir/refs.txt"
out=$(head -1 "$prompt" | tr -d '\\r')
if [[ "$out" != SKIP* ]]; then mkdir -p "$(dirname "$out")"; echo fake > "$out"; fi
echo "${FAKE_EXIT:-0}" > "$logdir/exit_code"
echo "${FAKE_LOG:-}" > "$logdir/log.txt"
touch "$logdir/.done"
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'imageto3d'
        self.monsters = Path(self.temp.name) / 'monsters' / 'aaa'
        (self.monsters / 'tools').mkdir(parents=True)
        self.script = self.monsters / 'tools' / 'codex_imagegen.sh'
        self.script.write_text(FAKE_LAUNCHER, newline='\n')
        self.script.chmod(0o755)
        self.batch = self.root / '.codex_jobs' / '_batch'
        for sub in ('tasks', 'status'):
            (self.batch / sub).mkdir(parents=True)
        patches = {'ROOT': self.root, 'BATCH': self.batch, 'TASKS': self.batch / 'tasks', 'STATUS': self.batch / 'status',
                   'EVENTS': self.batch / 'events.log', 'PAUSE': self.batch / 'PAUSE'}
        for name, value in patches.items():
            patcher = mock.patch.object(view_batch, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {'IMAGETO3D_MONSTERS': str(self.monsters)})
        env.start()
        self.addCleanup(env.stop)

    def task(self, name='m-left', **extra):
        out = self.monsters / 'pipeline' / 'Hobgoblin' / 'views' / '2' / 'left.png'
        task = {'name': name, 'priority': 3, 'prompt': f'{out.as_posix()}\nDraw it from the left.',
                'refs': [(self.monsters / 'front.png').as_posix()], 'outputs': [out.as_posix()], 'checks': [],
                'max_attempts': 2, **extra}
        view_batch.save(self.batch / 'tasks' / f'{name}.json', task)
        return task, out

    def status(self, name='m-left'):
        return json.loads((self.batch / 'status' / f'{name}.json').read_text())


class HelperTests(Base):
    def test_monsters_root_env_and_default(self):
        self.assertEqual(view_batch.monsters_root(), self.monsters)
        with mock.patch.dict(os.environ):
            os.environ.pop('IMAGETO3D_MONSTERS')
            self.assertEqual(view_batch.monsters_root(), self.root.parent / 'monsters' / 'aaa')

    def test_launcher_root(self):
        self.assertEqual(view_batch.launcher_root({}), self.root)
        self.assertEqual(view_batch.launcher_root({'launcher': 'imageto3d'}), self.root)
        self.assertEqual(view_batch.launcher_root({'launcher': 'monsters'}), self.monsters)
        with self.assertRaisesRegex(ValueError, 'unknown launcher'):
            view_batch.launcher_root({'launcher': 'nope'})

    @unittest.skipUnless(os.name == 'nt', 'the .done marker rule is the Windows one')
    def test_job_alive_reads_the_launchers_job_dir(self):
        status = {'jobs': [{'job': 'j-a01', 'started': time.strftime('%Y-%m-%dT%H:%M:%S')}]}
        self.assertTrue(view_batch.job_alive('j-a01', status, self.monsters))
        (self.monsters / '.codex_jobs' / 'j-a01').mkdir(parents=True)
        (self.monsters / '.codex_jobs' / 'j-a01' / '.done').touch()
        self.assertFalse(view_batch.job_alive('j-a01', status, self.monsters))
        self.assertTrue(view_batch.job_alive('j-a01', status))          # this project's own folder has no marker
        self.assertTrue(view_batch.job_alive('j-a01', status, self.root))


class LaunchTests(Base):
    def run_once(self, popen=None, tasks_after=None):
        """One scan of run() (the loop is stopped by an exception at its sleep)."""
        class Stop(Exception):
            pass

        def stop(_seconds):
            raise Stop()
        with mock.patch.object(view_batch.time, 'sleep', stop), mock.patch.object(view_batch, 'emit'):
            if popen:
                with mock.patch.object(view_batch.subprocess, 'Popen', popen):
                    with self.assertRaises(Stop):
                        view_batch.run(mock.Mock(parallel=4, exit_when_idle=False))
            else:
                with self.assertRaises(Stop):
                    view_batch.run(mock.Mock(parallel=4, exit_when_idle=False))

    def test_a_monster_task_is_launched_from_the_monster_root(self):
        self.task(launcher='monsters')
        calls = []
        self.run_once(popen=lambda cmd, **kw: calls.append((cmd, kw)) or mock.Mock())
        (cmd, kw), = calls
        self.assertEqual(cmd[len(view_batch.SHELL)], str(self.monsters / 'tools/codex_imagegen.sh'))
        self.assertEqual(Path(kw['cwd']), self.monsters)
        self.assertEqual(kw['env']['CODEX_TIMEOUT'], '5400')
        self.assertEqual(cmd[len(view_batch.SHELL) + 1], 'm-left-a01')
        self.assertEqual(self.status()['state'], 'running')
        self.assertEqual(self.status()['jobs'][-1]['job'], 'm-left-a01')

    def test_default_task_is_unchanged(self):
        self.task()                                   # no launcher field
        calls = []
        self.run_once(popen=lambda cmd, **kw: calls.append((cmd, kw)) or mock.Mock())
        (cmd, kw), = calls
        self.assertEqual(cmd[len(view_batch.SHELL)], str(self.root / 'tools/codex_imagegen.sh'))
        self.assertEqual(Path(kw['cwd']), self.root)

    def test_name_collision_looks_in_the_launchers_job_folder(self):
        self.task(launcher='monsters')
        (self.monsters / '.codex_jobs' / 'm-left-a01').mkdir(parents=True)     # an old monster job of that name
        (self.root / '.codex_jobs' / 'm-left-a02').mkdir(parents=True)         # another project's: must not matter
        calls = []
        self.run_once(popen=lambda cmd, **kw: calls.append(cmd) or mock.Mock())
        self.assertEqual(calls[0][len(view_batch.SHELL) + 1], 'm-left-a02')
        self.assertEqual(self.status()['jobs'][-1]['job'], 'm-left-a02')

    def test_unknown_launcher_or_missing_script_fails_the_task_not_the_runner(self):
        self.task('bad', launcher='nope')
        self.task('gone', launcher='monsters')
        self.script.unlink()
        calls = []
        self.run_once(popen=lambda cmd, **kw: calls.append(cmd) or mock.Mock())
        self.assertEqual(calls, [])
        self.assertEqual((self.status('bad')['state'], self.status('gone')['state']), ('failed', 'failed'))

    def test_a_finished_monster_job_is_judged_by_the_monster_job_folder(self):
        task, out = self.task(launcher='monsters')
        job = self.monsters / '.codex_jobs' / 'm-left-a01'
        job.mkdir(parents=True)
        (job / '.done').touch()
        (job / 'exit_code').write_text('1\n')
        (job / 'log.txt').write_text('ERROR: {"type":"usage_limit_reached","resets_at": 4102444800}')
        view_batch.save(self.batch / 'status' / 'm-left.json', {
            'state': 'running', 'attempts': 0, 'jobs': [{'job': 'm-left-a01', 'started': '2026-11-25T10:00:00'}]})
        (self.root / '.codex_jobs' / 'm-left-a01').mkdir(parents=True)           # no marker in this project's folder
        self.run_once()
        status = self.status()
        self.assertEqual((status['state'], status['jobs'][-1]['exit']), ('pending', '1'))   # limit_reset read the monster log
        out.parent.mkdir(parents=True)
        out.write_text('x')
        view_batch.save(self.batch / 'status' / 'm-left.json', {**status, 'state': 'running'})
        self.run_once()
        self.assertEqual(self.status()['state'], 'done')

    @unittest.skipUnless(BASH and Path(BASH).exists(), 'needs bash for the fake launcher')
    def test_end_to_end_with_the_fake_launcher(self):
        task, out = self.task(launcher='monsters')
        sleeps = []

        def short_sleep(_seconds):
            sleeps.append(1)
            REAL_SLEEP(0.3)
            if len(sleeps) > 60:
                raise AssertionError('the monster job never finished')
        with mock.patch.object(view_batch.time, 'sleep', short_sleep):
            view_batch.run(mock.Mock(parallel=4, exit_when_idle=True))
        self.assertTrue(out.is_file())
        self.assertEqual(self.status()['state'], 'done')
        REAL_SLEEP(1)           # let the fake script exit so the temp folder can be removed
        job = self.monsters / '.codex_jobs' / 'm-left-a01'
        self.assertTrue((job / '.done').is_file())
        self.assertFalse((self.root / '.codex_jobs' / 'm-left-a01').exists())
        self.assertTrue((self.batch / 'prompts' / 'm-left-a01.md').is_file())      # the prompt stays in the runner's batch folder
        # the script ran with the monster root as its working directory
        self.assertTrue((job / 'cwd.txt').read_text().strip().endswith('/monsters/aaa'))


class SingleInstanceAndReservationTests(Base):
    """DESIGN_PHASE4 I.5 / review_v2_phase4 finding 10: one runner, and a launch is reserved in the status BEFORE the spawn
    and reconciled at the next start (both launcher roots)."""

    run_once = LaunchTests.run_once

    def reserved(self, name='m-left', job='m-left-a01'):
        view_batch.save(self.batch / 'status' / f'{name}.json', {
            'state': 'launching', 'attempts': 0, 'job': job, 'started': '2026-11-25T10:00:00',
            'jobs': [{'job': job, 'started': '2026-11-25T10:00:00'}]})

    def test_a_second_runner_exits_while_the_first_holds_the_lock(self):
        handle = view_batch.acquire_instance_lock()
        self.assertIsNotNone(handle)
        try:
            self.task()
            calls = []
            with mock.patch.object(view_batch.subprocess, 'Popen', lambda cmd, **kw: calls.append(cmd) or mock.Mock()):
                self.assertEqual(view_batch.run(mock.Mock(parallel=4, exit_when_idle=False)), 1)
            self.assertEqual(calls, [])                                          # nothing launched, no status written
            self.assertFalse((self.batch / 'status' / 'm-left.json').exists())
        finally:
            handle.close()
        again = view_batch.acquire_instance_lock()                               # released with its holder
        self.assertIsNotNone(again)
        again.close()

    def test_the_lock_is_held_by_another_process_too(self):
        import subprocess
        code = ('import sys; sys.path.insert(0, r"%s"); import view_batch; from pathlib import Path; '
                'view_batch.BATCH = Path(r"%s"); h = view_batch.acquire_instance_lock(); print(h is not None, flush=True); '
                'sys.stdin.readline()' % (Path(view_batch.__file__).resolve().parent, self.batch))
        child = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'True')
            self.assertIsNone(view_batch.acquire_instance_lock())
        finally:
            child.communicate('\n', timeout=30)
        handle = view_batch.acquire_instance_lock()
        self.assertIsNotNone(handle)
        handle.close()

    def test_the_status_says_launching_while_the_job_is_spawned(self):
        self.task(launcher='monsters')
        seen = []
        self.run_once(popen=lambda cmd, **kw: seen.append(self.status()) or mock.Mock())
        (during,) = seen
        self.assertEqual((during['state'], during['job'], during['jobs'][-1]['job']), ('launching', 'm-left-a01', 'm-left-a01'))
        after = self.status()
        self.assertEqual(after['state'], 'running')
        self.assertNotIn('job', after)
        self.assertEqual([j['job'] for j in after['jobs']], ['m-left-a01'])

    def test_a_stop_between_spawn_and_status_never_launches_a_second_job(self):
        """The reviewer's sequence: the runner spawned the job and died before recording it; the restarted runner must adopt
        that job, not allocate the next attempt for the same outputs."""
        self.task(launcher='monsters')
        self.reserved()
        (self.monsters / '.codex_jobs' / 'm-left-a01').mkdir(parents=True)     # the spawned launcher created its job folder
        calls = []
        with mock.patch.object(view_batch, 'job_alive', lambda job, status, root=None: True):
            self.run_once(popen=lambda cmd, **kw: calls.append(cmd) or mock.Mock())
        self.assertEqual(calls, [])                                             # no second job
        status = self.status()
        self.assertEqual((status['state'], [j['job'] for j in status['jobs']]), ('running', ['m-left-a01']))
        self.assertNotIn('job', status)

    def test_a_reservation_whose_launcher_still_runs_is_adopted(self):
        self.task()
        self.reserved()
        calls = []
        with mock.patch.object(view_batch, 'launcher_alive', lambda job: job == 'm-left-a01'), \
                mock.patch.object(view_batch, 'job_alive', lambda job, status, root=None: True):
            self.run_once(popen=lambda cmd, **kw: calls.append(cmd) or mock.Mock())
        self.assertEqual((calls, self.status()['state']), ([], 'running'))

    def test_a_reservation_that_never_launched_goes_back_to_pending_and_runs_once(self):
        self.task()
        self.reserved()
        calls = []
        with mock.patch.object(view_batch, 'launcher_alive', lambda job: False):
            self.run_once(popen=lambda cmd, **kw: calls.append(cmd) or mock.Mock())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][len(view_batch.SHELL) + 1], 'm-left-a01')    # the reserved number is reused, not skipped
        status = self.status()
        self.assertEqual((status['state'], [j['job'] for j in status['jobs']]), ('running', ['m-left-a01']))

    def test_a_reservation_is_judged_in_the_monster_root_not_this_project(self):
        self.task(launcher='monsters')
        self.reserved()
        (self.root / '.codex_jobs' / 'm-left-a01').mkdir(parents=True)          # same name in the wrong project: ignored
        with mock.patch.object(view_batch, 'launcher_alive', lambda job: False):
            view_batch.reconcile_launching()
        self.assertEqual((self.status()['state'], self.status()['jobs']), ('pending', []))

    def test_a_failed_spawn_drops_the_reservation(self):
        self.task()

        def boom(cmd, **kw):
            raise OSError('no bash')
        self.run_once(popen=boom)
        status = self.status()
        self.assertEqual((status['state'], status['jobs']), ('pending', []))
        self.assertNotIn('job', status)


class GuardTests(Base):
    """Phase 5 review finding 10 (runner side): the descriptor and its guard are read again right before the spawn."""

    run_once = LaunchTests.run_once

    def guard_file(self, **data):
        path = self.monsters / 'pipeline' / 'Hobgoblin' / 'concept_state.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    def guarded(self, path, value='g7'):
        return self.task(launcher='monsters', guard={'file': str(path), 'key': 'pick_generation', 'value': value})

    def launches(self):
        calls = []
        self.run_once(popen=lambda cmd, **kw: calls.append(cmd) or mock.Mock())
        return calls

    def test_a_matching_guard_launches(self):
        self.guarded(self.guard_file(pick_generation='g7'))
        self.assertEqual(len(self.launches()), 1)
        self.assertEqual(self.status()['state'], 'running')

    def test_a_missing_or_different_guard_cancels_without_a_spawn(self):
        for name, setup in (('missing', lambda: self.guarded(self.monsters / 'nope.json')),
                            ('different', lambda: self.guarded(self.guard_file(pick_generation='g8'))),
                            ('no key', lambda: self.guarded(self.guard_file(other=1))),
                            ('malformed', lambda: self.task(launcher='monsters', guard={'file': 3}))):
            for path in (self.batch / 'status').glob('*.json'):
                path.unlink()
            setup()
            self.assertEqual(self.launches(), [], name)
            status = self.status()
            self.assertEqual(status['state'], 'cancelled', name)
            self.assertTrue(status['reason'].startswith('guard: '), name)
            self.assertEqual(status['jobs'], [], name)                       # the reservation was dropped
            self.assertNotIn('job', status)
            self.assertFalse((self.batch / 'prompts' / 'm-left-a01.md').exists())
        self.assertEqual(self.launches(), [])                                # cancelled stays cancelled

    def test_a_withdrawal_between_the_scan_and_the_spawn_is_seen(self):
        """The reviewer's race: the pick is withdrawn after the runner cached the pending descriptor; the guard is re-read after
        the `launching` reservation, so nothing is spawned."""
        guard = self.guard_file(pick_generation='g7')
        self.guarded(guard)
        real_save = view_batch.save

        def save(path, data):
            real_save(path, data)
            if data.get('state') == 'launching':
                guard.write_text(json.dumps({'pick_generation': 'g8'}))      # Undo withdrew the pick right now
        with mock.patch.object(view_batch, 'save', save):
            self.assertEqual(self.launches(), [])
        self.assertEqual((self.status()['state'], self.status()['reason']), ('cancelled', 'guard: withdrawn'))

    def test_a_deleted_descriptor_is_not_launched(self):
        self.task(launcher='monsters')
        real_save = view_batch.save

        def save(path, data):
            real_save(path, data)
            if data.get('state') == 'launching':
                (self.batch / 'tasks' / 'm-left.json').unlink()
        with mock.patch.object(view_batch, 'save', save):
            self.assertEqual(self.launches(), [])
        self.assertEqual((self.status()['state'], self.status()['reason']), ('cancelled', 'task withdrawn'))

    def test_a_changed_descriptor_is_launched_only_as_changed(self):
        task, _ = self.task(launcher='monsters')
        real_save = view_batch.save
        changed = []

        def save(path, data):
            real_save(path, data)
            if data.get('state') == 'launching' and not changed:
                changed.append(1)
                real_save(self.batch / 'tasks' / 'm-left.json', {**task, 'refs': []})
        with mock.patch.object(view_batch, 'save', save):
            self.assertEqual(self.launches(), [])
        self.assertEqual((self.status()['state'], self.status()['jobs']), ('pending', []))
        calls = self.launches()                                              # the next scan launches the NEW descriptor
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][len(view_batch.SHELL) + 3:], [])           # no refs: the changed task


class ValidationTests(Base):
    """Phase 5 review finding 17 (runner side): `validate: rgba_png` and `provenance` decide whether a finished task is done."""

    run_once = LaunchTests.run_once

    def image(self, path, mode='RGBA', clear=0.5, opaque=0.5):
        from PIL import Image
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new(mode, (10, 10), (200, 10, 10, 128) if mode == 'RGBA' else (200, 10, 10))
        if mode == 'RGBA':
            pixels = image.load()
            n_clear, n_opaque = int(clear * 100), int(opaque * 100)
            for i in range(100):
                pixels[i % 10, i // 10] = (200, 10, 10, 0 if i < n_clear else 255 if i < n_clear + n_opaque else 128)
        image.save(path)

    def finished(self, **extra):
        task, out = self.task(launcher='monsters', validate='rgba_png', provenance=['left.prompts.json'], **extra)
        job = self.monsters / '.codex_jobs' / 'm-left-a01'
        job.mkdir(parents=True)
        (job / '.done').touch()
        (job / 'exit_code').write_text('0\n')
        (job / 'log.txt').write_text('')
        view_batch.save(self.batch / 'status' / 'm-left.json', {
            'state': 'running', 'attempts': 0, 'jobs': [{'job': 'm-left-a01', 'started': '2026-11-25T10:00:00'}]})
        return task, out

    def provenance(self, out, text='{"prompt": "x"}'):
        (out.parent / 'left.prompts.json').write_text(text)

    def test_a_valid_output_with_provenance_is_done(self):
        _, out = self.finished()
        self.image(out)
        self.provenance(out)
        self.run_once()
        self.assertEqual(self.status()['state'], 'done')
        self.assertTrue(out.exists())

    def test_invalid_images_fail_the_attempt_and_are_set_aside(self):
        cases = (('fully opaque', dict(clear=0.0, opaque=1.0), 'fully transparent'),
                 ('fully transparent', dict(clear=1.0, opaque=0.0), 'fully opaque'),
                 ('RGB', dict(mode='RGB'), 'mode RGB, not RGBA'))
        for label, kw, reason in cases:
            for path in list((self.batch / 'status').glob('*.json')):
                path.unlink()
            shutil.rmtree(self.monsters / '.codex_jobs', ignore_errors=True)
            shutil.rmtree(self.monsters / 'pipeline', ignore_errors=True)
            _, out = self.finished()
            self.image(out, **kw)
            self.provenance(out)
            self.run_once(popen=lambda cmd, **k: mock.Mock())
            status = self.status()
            self.assertEqual(status['attempts'], 1, label)
            self.assertIn('left.png: ', status['error'], label)
            self.assertIn(reason, status['error'], label)
            self.assertFalse(out.exists(), label)                            # never counted as done, generated again
            self.assertTrue(list(out.parent.glob('left.png.invalid-*')), label)

    def test_a_non_png_and_a_missing_provenance(self):
        _, out = self.finished()
        out.parent.mkdir(parents=True)
        out.write_bytes(b'\x89PNG\r\n\x1a\n' + b'garbage' * 10)               # a PNG header only: the old header check passed it
        self.run_once(popen=lambda cmd, **k: mock.Mock())
        error = self.status()['error']
        self.assertIn('left.png: does not decode', error)
        self.assertIn('left.prompts.json: provenance missing', error)

    def test_missing_provenance_alone_regenerates_the_image(self):
        _, out = self.finished()
        self.image(out)
        self.run_once(popen=lambda cmd, **k: mock.Mock())
        status = self.status()
        self.assertEqual((status['attempts'], status['error']), (1, 'left.prompts.json: provenance missing'))
        self.assertFalse(out.exists())                                       # an image without its record is generated again
        self.provenance(out, '{broken')
        self.assertEqual([r for _, r in view_batch.validation_problems(json.loads((self.batch / 'tasks' / 'm-left.json').read_text()))
                          if 'provenance' in r], ['provenance is not readable JSON (JSONDecodeError)'])

    def test_the_attempt_budget_ends_in_failed(self):
        _, out = self.finished(max_attempts=1)
        self.image(out, clear=0.0, opaque=1.0)
        self.provenance(out)
        self.run_once()
        self.assertEqual(self.status()['state'], 'failed')

    def test_an_existing_invalid_output_is_set_aside_before_the_launch(self):
        task, out = self.task(launcher='monsters', validate='rgba_png')
        self.image(out, clear=0.0, opaque=1.0)
        prompts = []

        def popen(cmd, **kw):
            prompts.append(Path(cmd[len(view_batch.SHELL) + 2]).read_text())
            return mock.Mock()
        self.run_once(popen=popen)
        self.assertFalse(out.exists())
        self.assertNotIn('RESUME NOTE', prompts[0])                          # the invalid file is not kept as "already saved"

    def test_a_task_without_the_fields_is_unchanged(self):
        task, out = self.task(launcher='monsters')
        job = self.monsters / '.codex_jobs' / 'm-left-a01'
        job.mkdir(parents=True)
        (job / '.done').touch()
        (job / 'exit_code').write_text('0\n')
        view_batch.save(self.batch / 'status' / 'm-left.json', {
            'state': 'running', 'attempts': 0, 'jobs': [{'job': 'm-left-a01', 'started': '2026-11-25T10:00:00'}]})
        out.parent.mkdir(parents=True)
        out.write_text('not even an image')
        self.run_once()
        self.assertEqual(self.status()['state'], 'done')


if __name__ == '__main__':
    unittest.main()
