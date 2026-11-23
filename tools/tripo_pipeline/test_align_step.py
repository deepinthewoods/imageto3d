"""convert_job's align step (align_body.py + body_fit.py --source aligned): record, never-fail behaviour, the lock-owner bypass of
rewear.module_busy and the --no-align / config switches. Subprocess runs are mocked."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import convert_job
import rewear


class AlignStepTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / 'models/set/job/elf_female/right_gauntlet_boot'
        (self.out / 'reduced').mkdir(parents=True)
        self.calls, self.envs = [], []
        self.behaviour = {}                     # script name -> (status line, exit code)
        for patcher in (mock.patch.object(convert_job, 'ROOT', self.root),
                        mock.patch.object(convert_job, 'segment_first_python', return_value='venv-python'),
                        mock.patch.object(convert_job.subprocess, 'run', side_effect=self.fake_run)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_run(self, command, **kwargs):
        name = next(Path(c).name for c in command if str(c).endswith('.py'))
        self.calls.append((name, [str(c) for c in command]))
        self.envs.append(kwargs.get('env'))
        line, code = self.behaviour.get(name, ('done', 0))
        tag = 'ALIGN_BODY' if name == 'align_body.py' else 'BODY_FIT'
        kwargs['stdout'].write(f'{tag} {line:8} set/job/elf_female/right_gauntlet_boot 4.2 max_before=1.0mm\n')
        if code == 0 and line in ('done', 'fresh', 'warnings'):
            fit = self.out / 'fit'
            fit.mkdir(exist_ok=True)
            if name == 'align_body.py':
                (fit / 'align.json').write_text(json.dumps({
                    'warnings': ['a', 'b'], 'residuals': {'current': {'mean_mm': 6.16}, 'stage1': {'mean_mm': 5.2}, 'stage12': {'mean_mm': 4.88}}}))
            else:
                (fit / 'body_fit_aligned.json').write_text(json.dumps({'warnings': [], 'levels': {
                    '0p4': {'wearable': {'push': {'poke_residual': {'area_cm2': 0.0}}, 'displacement': {'max_mm': 55.0}}},
                    '0p2': {'wearable': {'displacement': {'max_mm': 40.0}}}}}))
        return mock.Mock(returncode=code)

    def test_done_record_and_commands(self):
        record = convert_job.align_step(self.out, 'right_gauntlet_boot')
        self.assertEqual([c[0] for c in self.calls], ['align_body.py', 'body_fit.py'])
        self.assertEqual(self.calls[0][1][0], 'venv-python')
        self.assertEqual(self.calls[0][1][2], str(self.out))
        fit_command = self.calls[1][1]
        self.assertEqual(fit_command[fit_command.index('--source') + 1], 'aligned')
        self.assertNotIn('experiments', ' '.join(fit_command))              # the summary file goes to a temp folder
        self.assertEqual(record['status'], 'done')
        self.assertEqual(record['align'], {'status': 'done', 'residual_mm': {'before': 6.16, 'after': 4.88}, 'warnings': 2})
        self.assertEqual(record['fit']['status'], 'done')
        self.assertEqual(record['fit']['levels'], {'0p4': {'poke_after_cm2': 0.0, 'max_displacement_mm': 55.0},
                                                   '0p2': {'poke_after_cm2': None, 'max_displacement_mm': 40.0}})
        self.assertEqual(json.loads((self.out / 'align_step.json').read_text()), record)
        self.assertIn('ALIGN_BODY', (self.out / 'align_step.log').read_text())     # both steps log into the same file
        self.assertIn('BODY_FIT', (self.out / 'align_step.log').read_text())
        self.assertIn('align done', convert_job.align_summary(record))

    def test_lock_owner_is_passed_to_both_scripts(self):
        convert_job.align_step(self.out, 'right_gauntlet_boot')
        self.assertEqual(len(self.envs), 2)
        for env in self.envs:
            self.assertEqual(env['MODULE_LOCK_OWNER'], str(os.getpid()))

    def test_fresh_outputs_count_as_done(self):
        self.behaviour = {'align_body.py': ('fresh', 0), 'body_fit.py': ('fresh', 0)}
        record = convert_job.align_step(self.out, 'right_gauntlet_boot')
        self.assertEqual((record['status'], record['align']['status'], record['fit']['status']), ('done', 'fresh', 'fresh'))

    def test_skipped_align_runs_no_fit(self):
        self.behaviour = {'align_body.py': ('skipped', 0)}
        record = convert_job.align_step(self.out, 'right_gauntlet_boot')
        self.assertEqual([c[0] for c in self.calls], ['align_body.py'])
        self.assertEqual(record['status'], 'skipped')
        self.assertEqual(record['align']['status'], 'skipped')
        self.assertNotIn('fit', record)

    def test_failed_align_never_raises(self):
        self.behaviour = {'align_body.py': ('failed', 1)}
        record = convert_job.align_step(self.out, 'right_gauntlet_boot')
        self.assertEqual(record['status'], 'failed')
        self.assertIn('align_body.py exited 1', record['error'])
        self.assertEqual([c[0] for c in self.calls], ['align_body.py'])
        self.assertTrue((self.out / 'align_step.json').is_file())

    def test_failed_fit_keeps_the_align_record(self):
        self.behaviour = {'body_fit.py': ('failed', 1)}
        record = convert_job.align_step(self.out, 'right_gauntlet_boot')
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['align']['status'], 'done')
        self.assertIn('body_fit.py exited 1', record['error'])

    def test_script_reporting_failure_with_exit_zero_is_failed(self):
        self.behaviour = {'body_fit.py': ('failed', 0)}
        self.assertEqual(convert_job.align_step(self.out, 'right_gauntlet_boot')['status'], 'failed')

    def test_skipped_fit_makes_the_step_skipped(self):
        self.behaviour = {'body_fit.py': ('skipped', 0)}
        record = convert_job.align_step(self.out, 'right_gauntlet_boot')
        self.assertEqual((record['status'], record['fit']['status']), ('skipped', 'skipped'))

    def test_missing_interpreter_never_raises(self):
        with mock.patch.object(convert_job, 'segment_first_python', side_effect=FileNotFoundError('no python')):
            record = convert_job.align_step(self.out, 'right_gauntlet_boot')
        self.assertEqual(record['status'], 'failed')
        self.assertIn('no python', record['error'])
        self.assertEqual(self.calls, [])

    def test_crashing_subprocess_never_raises(self):
        with mock.patch.object(convert_job.subprocess, 'run', side_effect=OSError('exec failed')):
            record = convert_job.align_step(self.out, 'right_gauntlet_boot')
        self.assertEqual(record['status'], 'failed')


class ModuleBusyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.module = self.root / 'models/s/j/elf_female/outfit'
        self.module.mkdir(parents=True)
        self.lock = self.root / 'models/s/j/.module_locks/elf_female__outfit.lock'
        self.lock.parent.mkdir(parents=True)
        patcher = mock.patch.object(rewear, 'ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MODULE_LOCK_OWNER', None)

    def test_live_holder_is_busy(self):
        self.lock.write_text(str(os.getpid()))
        self.assertTrue(rewear.module_busy(self.module))

    def test_no_lock_or_dead_holder_is_not_busy(self):
        self.assertFalse(rewear.module_busy(self.module))
        self.lock.write_text('999999999')
        self.assertFalse(rewear.module_busy(self.module))

    def test_the_declared_owner_may_use_its_own_lock(self):
        self.lock.write_text(str(os.getpid()))
        os.environ['MODULE_LOCK_OWNER'] = str(os.getpid())
        self.assertFalse(rewear.module_busy(self.module))

    def test_another_live_holder_stays_busy(self):
        self.lock.write_text(str(os.getpid()))
        os.environ['MODULE_LOCK_OWNER'] = str(os.getppid())               # somebody else's pid
        self.assertTrue(rewear.module_busy(self.module))
        os.environ['MODULE_LOCK_OWNER'] = 'garbage'
        self.assertTrue(rewear.module_busy(self.module))
        os.environ['MODULE_LOCK_OWNER'] = ''
        self.assertTrue(rewear.module_busy(self.module))


class ConvertJobAlignSwitchTest(unittest.TestCase):
    """convert_job.main: the step runs by default, --no-align and config.json align.enabled=false skip it; it is recorded either way."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_job(self, argv=(), config=None):
        job = self.root / 'processing' / 's--j'
        (job / 'human_male' / 'outfit').mkdir(parents=True)
        (job / '.tripo-job.json').write_text(json.dumps({'set': 's', 'job_id': 'j', 'modules': [{'race_gender': 'human_male', 'module': 'outfit'}]}))
        out = self.root / 'models/s/j/human_male/outfit'
        commands = []

        def fake_run(cmd, **kw):
            commands.append([str(c) for c in cmd])
            joined = ' '.join(commands[-1])
            record = out / 'studio_submission.json'
            if ' submit ' in joined:
                out.mkdir(parents=True, exist_ok=True)
                record.write_text(json.dumps({'status': 'submitted', 'project_id': 'P'}))
            elif ' collect ' in joined:
                (out / 'x.glb').write_bytes(b'glb')
                data = json.loads(record.read_text())
                data.update(status='exported', exports={'GLB': {'path': str(out / 'x.glb')}})
                record.write_text(json.dumps(data))
            elif 'align_body.py' in joined:
                kw['stdout'].write('ALIGN_BODY done     s/j/human_male/outfit 1.0\n')
                (out / 'fit').mkdir(exist_ok=True)
                (out / 'fit/align.json').write_text(json.dumps({'warnings': [], 'residuals': {'current': {'mean_mm': 6.0}, 'stage12': {'mean_mm': 4.0}}}))
            elif 'body_fit.py' in joined:
                kw['stdout'].write('BODY_FIT done     s/j/human_male/outfit 1.0 max_before=1.0mm\n')
            else:  # blender reduction
                (out / 'reduced').mkdir(parents=True, exist_ok=True)
                (out / 'reduced/manifest.json').write_text(json.dumps({
                    'interior': {'interior_area_share': 0, 'threshold': 0.15},
                    'levels': {'0.4': {'triangles': 5, 'mesh_checks_pass': True, 'material_review_required': False}}}))
            return mock.Mock(returncode=0)
        patches = [mock.patch.object(convert_job, 'ROOT', self.root), mock.patch.object(convert_job.subprocess, 'run', fake_run),
                   mock.patch.object(convert_job, 'segment_first_python', return_value='venv-python')]
        if config is not None:
            patches.append(mock.patch.object(convert_job, 'align_config', return_value=config))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        convert_job.main(['s--j', *argv])
        return commands, json.loads((job / '.tripo-job.json').read_text())['module_conversion']['human_male/outfit']

    def scripts(self, commands):
        return [Path(c).name for command in commands for c in command if c.endswith(('align_body.py', 'body_fit.py'))]

    def test_runs_by_default_and_is_recorded(self):
        commands, entry = self.run_job()
        self.assertEqual(self.scripts(commands), ['align_body.py', 'body_fit.py'])
        self.assertEqual(entry['align']['status'], 'done')
        self.assertEqual(entry['align']['align']['residual_mm'], {'before': 6.0, 'after': 4.0})
        self.assertTrue((self.root / 'models/s/j/human_male/outfit/align_step.json').is_file())

    def test_no_align_flag_skips_it(self):
        commands, entry = self.run_job(['--no-align'])
        self.assertEqual(self.scripts(commands), [])
        self.assertEqual(entry['align'], {'status': 'skipped', 'why': '--no-align'})

    def test_config_can_turn_it_off(self):
        commands, entry = self.run_job(config={'enabled': False})
        self.assertEqual(self.scripts(commands), [])
        self.assertEqual(entry['align']['status'], 'skipped')
        self.assertIn('config.json', entry['align']['why'])

    def test_config_enabled_true_and_missing_block(self):
        self.assertIsNone(convert_job.align_disabled_reason([]))
        with mock.patch.object(convert_job, 'align_config', return_value={}):
            self.assertIsNone(convert_job.align_disabled_reason(['--dry-run']))
        with mock.patch.object(convert_job, 'align_config', return_value={'enabled': True}):
            self.assertIsNone(convert_job.align_disabled_reason([]))
            self.assertEqual(convert_job.align_disabled_reason(['--no-align']), '--no-align')

    def test_the_shipped_config_enables_it(self):
        self.assertIs(convert_job.align_config().get('enabled'), True)


if __name__ == '__main__':
    unittest.main()
