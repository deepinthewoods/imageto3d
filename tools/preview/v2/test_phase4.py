"""Tests of the standalone building blocks of phase 4 of the v2 preview backend: settings, modes, convert list, credits, the service manager,
the supervisor's claim-lock fix and the convert worker's stop file.

Run: python -X utf8 -m unittest tools/preview/v2/test_phase4.py   (temp roots only; the real root and the live services are never touched)

The service manager is tested against FAKE services: tiny python scripts placed at `<temp root>/tools/<real script name>` that match the
manager's process patterns for that temp root only (it resolves the script path against the process cwd), so the live supervisor / view runner /
convert worker of the real checkout are invisible to these tests. Every process a test starts is killed in tearDown.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import warnings
from unittest import mock

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parents[1]
for _p in (HERE.parent, HERE, TOOLS, TOOLS / 'tripo_pipeline'):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import convert_list  # noqa: E402
import credits  # noqa: E402
import guard  # noqa: E402
import modes  # noqa: E402
import orchestrator  # noqa: E402
import services  # noqa: E402
import settings  # noqa: E402

import psutil  # noqa: E402


FAKE = textwrap.dedent('''
    import datetime, json, os, pathlib, subprocess, sys, time
    child = None                                         # ctl {"child": [args]}: start ONE child `python <args>` (a job it launched)
    stem = pathlib.Path(__file__).stem
    root = pathlib.Path.cwd()
    batch, app = root / '.codex_jobs' / '_batch', root / '.codex_jobs' / '_app'
    batch.mkdir(parents=True, exist_ok=True)
    app.mkdir(parents=True, exist_ok=True)
    (app / f'fake_{stem}.args').write_text(json.dumps(sys.argv[1:]))
    if stem == 'pipeline_supervisor' and sys.argv[-1] == 'stop':
        (batch / 'supervisor.stop').write_text('stop')
        sys.exit(0)
    worker = int(sys.argv[sys.argv.index('--worker') + 1]) if '--worker' in sys.argv else 1
    stopfile = {'pipeline_supervisor': 'supervisor.stop', 'convert_queue': f'convert_worker{worker}.stop'}.get(stem)
    ctl_path = app / f'fake_{stem}.json'
    t0 = time.time()

    def event(text):
        with open(batch / 'convert_events.log', 'a') as handle:
            handle.write(f'{datetime.datetime.now():%m-%d %H:%M:%S} {text}\\n')

    def names_me(path):
        try:
            return json.loads(path.read_text()).get('pid') == os.getpid()
        except (OSError, ValueError, AttributeError):
            return False
    if stem == 'convert_queue' and (json.loads(ctl_path.read_text()) if ctl_path.exists() else {}).get('sleep_event'):
        event('PAUSED (PAUSE file); not starting conversions')
    ctl = {}
    while True:
        try:
            ctl = json.loads(ctl_path.read_text()) if ctl_path.exists() else {}
        except (OSError, ValueError):                     # (read while the test rewrites it: keep the last one)
            pass
        if ctl.get('child') and child is None:
            child = subprocess.Popen([sys.executable, *ctl['child']])
        if stem == 'pipeline_supervisor' and not ctl.get('no_tick'):     # the supervisor's end-of-tick record (DESIGN_PHASE4 I.1)
            tmp = batch / f'supervisor_tick.{os.getpid()}.tmp'
            tmp.write_text(json.dumps({'pid': os.getpid(), 'time': time.time(), 'mode': 'auto', 'paused': False}))
            try:
                os.replace(tmp, batch / 'supervisor_tick.json')
            except OSError:                                  # a reader has it open (Windows): the next pass writes it
                pass
        if stem == 'pipeline_supervisor' and not ctl.get('ignore_stop') and (batch / stopfile).exists():
            (batch / stopfile).unlink()
            (batch / 'PAUSE').unlink(missing_ok=True)   # like Supervisor.release_all on exit
            break
        if stem == 'convert_queue' and not ctl.get('ignore_stop') and names_me(batch / stopfile):   # the per-worker stop (I.2)
            (batch / f'convert_worker{worker}.stopping').write_text(json.dumps({'pid': os.getpid(), 'time': time.time()}))
            while (json.loads(ctl_path.read_text()) if ctl_path.exists() else {}).get('busy'):
                time.sleep(0.05)                         # finishing the job in flight
            event('STOP requested; exiting')
            (batch / stopfile).unlink(missing_ok=True)
            (batch / f'convert_worker{worker}.stopping').unlink(missing_ok=True)
            break
        if ctl.get('exit_after') is not None and time.time() - t0 >= ctl['exit_after']:
            if ctl.get('idle_exit'):
                with open(batch / 'convert_events.log', 'a') as handle:
                    handle.write('11-25 10:00:00 IDLE: view batch runner gone, nothing staged and no conversion in flight; exiting\\n')
            sys.exit(ctl.get('code', 0))
        time.sleep(0.05)
''')
SCRIPTS = {'supervisor': 'tools/pipeline_supervisor.py', 'views': 'tools/view_batch.py', 'convert': 'tools/convert_queue.py',
           'convert_job': 'tools/tripo_pipeline/convert_job.py'}
VIEWS_ONLY = {'services': {'supervisor': False, 'views': True, 'convert': False}}
CONVERT_ONLY = {'services': {'supervisor': False, 'views': False, 'convert': True}}


class TempRoot(unittest.TestCase):
    """A temp root with the fake service scripts; tearDown kills every fake process of the root and removes it."""

    def setUp(self):
        warnings.simplefilter('ignore', ResourceWarning)   # the manager's detached Popen objects are dropped on purpose; tearDown kills them
        self.root = Path(tempfile.mkdtemp(prefix='v2p4_')).resolve()
        xdg = mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root / '_xdg')})   # the Tripo profile reservation of the temp root
        xdg.start()
        self.addCleanup(xdg.stop)
        for rel in SCRIPTS.values():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(FAKE, encoding='utf-8')
        (self.root / '.codex_jobs' / '_batch').mkdir(parents=True)
        (self.root / '.codex_jobs' / '_app').mkdir(parents=True)
        self.popens = []

    def tearDown(self):
        self.kill_all()
        for _ in range(20):
            try:
                shutil.rmtree(self.root)
                break
            except OSError:
                time.sleep(0.2)

    def kill_all(self):
        for name, items in services.scan(self.root, list(SCRIPTS)).items():
            for inst in items:
                try:
                    psutil.Process(inst['pid']).kill()
                except psutil.Error:
                    pass
        for popen in self.popens:
            try:
                popen.kill()
                popen.wait(5)
            except (OSError, subprocess.SubprocessError):
                pass

    def spawn(self, name, *extra):
        """Start a fake the way a user would by hand (relative script, cwd = root): adoptable, not started by the manager."""
        args = {'supervisor': ['run'], 'views': ['run', '--parallel', '8'], 'convert': ['--sets-file', 'x.txt', '--worker', '1'],
                'convert_job': ['job--abc']}[name]
        popen = subprocess.Popen([sys.executable, SCRIPTS[name], *args, *extra], cwd=str(self.root),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.popens.append(popen)
        self.wait_for(lambda: services.scan(self.root, [name])[name])
        return popen

    def wait_for(self, condition, timeout=15.0, what='condition'):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result = condition()
            if result:
                return result
            time.sleep(0.1)
        self.fail(f'timed out waiting for {what}')

    def running(self, name):
        return services.scan(self.root, [name])[name]

    def ctl(self, stem, data):
        (self.root / '.codex_jobs' / '_app' / f'fake_{stem}.json').write_text(json.dumps(data))

    def kill(self, name):
        for inst in self.running(name):
            psutil.Process(inst['pid']).kill()
        self.wait_for(lambda: not self.running(name), what=f'{name} to die')


# ------------------------------------------------------------------------------------------------------------------------ settings

class SettingsTest(TempRoot):
    def test_defaults_when_the_file_is_missing_or_broken(self):
        self.assertEqual(settings.load(self.root), settings.DEFAULTS)
        self.assertEqual(settings.health(self.root)['source'], 'defaults')
        settings.path(self.root).write_text('{not json', encoding='utf-8')
        self.assertEqual(settings.load(self.root), settings.DEFAULTS)              # the last valid policy (the defaults of the missing file)
        settings.path(self.root).write_text('[1, 2]', encoding='utf-8')
        self.assertEqual(settings.load(self.root), settings.DEFAULTS)
        self.assertEqual(settings.health(self.root)['source'], 'last_valid')

    def test_an_unreadable_file_keeps_the_last_valid_policy_or_holds_starts(self):
        """Review F16: a read failure never turns a disabled service back on."""
        settings.save(self.root, {'services': {'convert': False}, 'tripo': 'off'})
        self.assertFalse(settings.load(self.root)['services']['convert'])
        settings.path(self.root).write_text('{"services": {"convert": fal', encoding='utf-8')    # a torn / broken write
        loaded = settings.load(self.root)
        self.assertFalse(loaded['services']['convert'])
        health = settings.health(self.root)
        self.assertEqual((health['source'], health['hold_starts']), ('last_valid', False))
        self.assertIn('not valid JSON', health['error'])
        # a fresh process (no last valid policy) with a broken file: defaults for display, but every start is held
        settings._LAST.clear()
        self.assertEqual(settings.load(self.root), settings.DEFAULTS)
        self.assertTrue(settings.health(self.root)['hold_starts'])
        with self.assertRaises(settings.SettingsError):                          # nothing to merge a patch onto
            settings.save(self.root, {'view_parallel': 3})
        events = services.tick(self.root)['events']
        self.assertTrue(any('not started' in e and 'settings.json' in e for e in events), events)
        self.assertEqual({k: v for k, v in services.scan(self.root).items() if v}, {})
        self.assertTrue(any('settings' in e for e in services.status(self.root)['errors']))
        settings.path(self.root).write_text(json.dumps({'services': {'supervisor': False, 'views': False, 'convert': False}}), encoding='utf-8')
        self.assertFalse(settings.health(self.root)['hold_starts'])

    def test_load_merges_a_legacy_file_and_replaces_invalid_values(self):
        settings.path(self.root).write_text(json.dumps({
            'layer_workers_away': 2, 'layer_workers_present': -1, 'view_parallel': 'many', 'tripo': 'sometimes',
            'services': {'convert': False, 'views': 'yes', 'other': True}, 'allowed_hosts': ['a.ts.net', 7], 'extra': 1}), encoding='utf-8')
        loaded = settings.load(self.root)
        self.assertEqual(loaded['layer_workers_away'], 2)
        self.assertEqual(loaded['layer_workers_present'], 1)          # invalid -> default
        self.assertEqual(loaded['view_parallel'], 8)
        self.assertEqual(loaded['tripo'], 'off')
        self.assertEqual(loaded['services'], {'supervisor': True, 'views': False, 'convert': False})   # invalid flag: off, never a start (F16)
        self.assertTrue(any('services.views' in p for p in settings.health(self.root)['problems']))
        self.assertEqual(loaded['allowed_hosts'], [])                  # one bad entry invalidates the list
        self.assertNotIn('extra', loaded)
        self.assertEqual(set(loaded), set(settings.DEFAULTS))          # a superset of what the orchestrator reads
        self.assertTrue({'layer_workers_present', 'layer_workers_away'} <= set(loaded))

    def test_save_validates_merges_and_keeps_unknown_keys(self):
        settings.path(self.root).write_text(json.dumps({'future_key': {'a': 1}, 'services': {'views': False}}), encoding='utf-8')
        saved = settings.save(self.root, {'view_parallel': 4, 'services': {'convert': False}, 'allowed_hosts': ['x.ts.net', 'x.ts.net', '[::1]']})
        self.assertEqual(saved['view_parallel'], 4)
        self.assertEqual(saved['services'], {'supervisor': True, 'views': False, 'convert': False})
        self.assertEqual(saved['allowed_hosts'], ['x.ts.net', '[::1]'])
        raw = json.loads(settings.path(self.root).read_text(encoding='utf-8'))
        self.assertEqual(raw['future_key'], {'a': 1})
        self.assertEqual(list(settings.path(self.root).parent.glob('settings.json.*')), [])   # no temp files left

    def test_save_rejects_bad_values_and_writes_nothing(self):
        bad = [{'view_parallel': 0}, {'view_parallel': 17}, {'view_parallel': True}, {'view_parallel': '8'}, {'tripo': 'on'},
               {'services': {'convert': 'no'}}, {'services': {'nope': True}}, {'services': []}, {'allowed_hosts': 'a'},
               {'allowed_hosts': ['has space']}, {'allowed_hosts': ['http://x']}, {'paid_budget_hyper3d': -1}, {'layer_workers_away': 99},
               {'mystery': 1}]
        for patch in bad:
            with self.subTest(patch=patch), self.assertRaises(settings.SettingsError):
                settings.save(self.root, patch)
        with self.assertRaises(settings.SettingsError) as raised:
            settings.save(self.root, {'view_parallel': 0, 'tripo': 'x'})
        self.assertEqual(len(raised.exception.problems), 2)
        self.assertFalse(settings.path(self.root).exists())
        with self.assertRaises(settings.SettingsError):
            settings.save(self.root, 'text')


# --------------------------------------------------------------------------------------------------------------------------- modes

class ModesTest(TempRoot):
    def test_constants_match_the_supervisor(self):
        import pipeline_supervisor
        self.assertEqual(modes.MODES, pipeline_supervisor.MODES)
        self.assertEqual(modes.AWAY_AFTER, pipeline_supervisor.AWAY_AFTER)
        self.assertEqual(modes.LOCK_OFFSET, pipeline_supervisor.LOCK_OFFSET)

    def test_write_mode_uses_the_supervisors_file_format(self):
        import pipeline_supervisor
        self.assertEqual(modes.read_mode(self.root)['mode'], 'auto')          # missing file = the supervisor default
        for preset, mode in modes.PRESETS.items():
            self.assertEqual(modes.write_mode(self.root, preset)['mode'], mode)
        self.assertEqual(modes.write_mode(self.root, 'pause')['mode'], 'pause')
        raw = json.loads((self.root / '.codex_jobs/_batch/supervisor_mode.json').read_text(encoding='utf-8'))
        self.assertEqual(set(raw) - {'pause_id'}, {'mode', 'since'})          # (pause_id: the pause identity of modes.py, review finding 5)
        sup = pipeline_supervisor.Supervisor(self.root)
        self.assertEqual(sup.mode(), 'pause')                                  # the supervisor reads what we wrote
        with self.assertRaises(modes.ModeError):
            modes.write_mode(self.root, 'turbo')
        self.assertEqual(sup.mode(), 'pause')

    def test_invalid_mode_file_reads_as_auto(self):
        (self.root / '.codex_jobs/_batch/supervisor_mode.json').write_text('{"mode": "banana"}', encoding='utf-8')
        self.assertEqual(modes.read_mode(self.root)['mode'], 'auto')

    def test_read_pause(self):
        self.assertIsNone(modes.read_pause(self.root))
        path = self.root / '.codex_jobs/_batch/PAUSE'
        path.write_text(json.dumps({'since': 's', 'reason': 'manual pause', 'scope': ['conversions']}), encoding='utf-8')
        pause = modes.read_pause(self.root)
        self.assertEqual({k: v for k, v in pause.items() if k != 'pause_id'}, {'since': 's', 'reason': 'manual pause', 'scope': ['conversions']})
        path.write_text(json.dumps({'since': 's', 'reason': 'old'}), encoding='utf-8')
        self.assertEqual(modes.read_pause(self.root)['scope'], ['conversions', 'views'])
        path.write_text('garbage', encoding='utf-8')
        self.assertEqual(modes.read_pause(self.root)['reason'], 'unreadable PAUSE file')

    def test_idle_seconds_prefers_a_fresh_presence_file(self):
        presence = self.root / 'presence.json'
        presence.write_text(json.dumps({'updated_unix': 1000.0, 'user_idle_seconds': 412.0}), encoding='utf-8')
        with mock.patch.object(modes, 'win_idle_seconds', return_value=7.0):
            self.assertEqual(modes.idle_seconds(1010.0, presence), (412.0, 'presence.json'))
            self.assertEqual(modes.idle_seconds(1100.0, presence), (7.0, 'GetLastInputInfo'))      # stale file
            self.assertEqual(modes.idle_seconds(1010.0, self.root / 'missing.json'), (7.0, 'GetLastInputInfo'))
        with mock.patch.object(modes, 'win_idle_seconds', return_value=None):
            self.assertEqual(modes.idle_seconds(1100.0, presence), (None, 'unknown'))

    def test_supervisor_running_probes_the_lock_without_disturbing_it(self):
        import pipeline_supervisor
        self.assertEqual(modes.supervisor_running(self.root), (False, ''))     # no lock file; nothing is created
        self.assertFalse((self.root / '.codex_jobs/_batch/supervisor.lock').exists())
        sup = pipeline_supervisor.Supervisor(self.root)
        handle = sup.acquire()
        try:
            self.assertEqual(modes.supervisor_running(self.root), (True, str(os.getpid())))
            self.assertEqual(modes.supervisor_running(self.root), (True, str(os.getpid())))   # the probe did not release the lock
            second = pipeline_supervisor.Supervisor(self.root)
            self.assertIsNone(second.acquire())
        finally:
            handle.close()
        self.assertEqual(modes.supervisor_running(self.root)[0], False)         # released: the probe lock is taken and dropped again
        again = pipeline_supervisor.Supervisor(self.root).acquire()
        self.assertIsNotNone(again)
        again.close()

    def test_snapshot(self):
        presence = self.root / 'presence.json'
        presence.write_text(json.dumps({'updated_unix': time.time(), 'user_idle_seconds': 600.0}), encoding='utf-8')
        snap = modes.snapshot(self.root, presence_file=presence)
        self.assertTrue(snap['away'])
        self.assertFalse(snap['supervisor_running'])
        self.assertIn('supervisor not running', snap['warning'])


# ----------------------------------------------------------------------------------------------------------------------- convert list

class ConvertListTest(TempRoot):
    def sets_file(self, text):
        convert_list.list_path(self.root).write_text(text, encoding='utf-8', newline='')

    def stage(self, stage, set_id, job_id='aaa111', state='ready', modules=(('human_male', 'outfit'),), converted=()):
        job = self.root / stage / f'{set_id}--{job_id}'
        job.mkdir(parents=True)
        record = {'set': set_id, 'job_id': job_id, 'state': state, 'modules': [{'race_gender': r, 'module': m} for r, m in modules],
                  'module_conversion': {f'{r}/{m}': {} for r, m in converted}}
        (job / '.tripo-job.json').write_text(json.dumps(record), encoding='utf-8')
        for r, m in converted:
            out = self.root / 'models' / set_id / job_id / r / m / 'reduced'
            out.mkdir(parents=True)
            (out / 'manifest.json').write_text('{}', encoding='utf-8')
        return job

    def test_matches_follows_the_workers_semantics(self):
        pats = convert_list.parse('# comment\n\nnoble_outfit_*\n!noble_outfit_09_*\nexact_set\n  padded  \n # not a comment, a pattern\n')
        self.assertEqual(pats, ['noble_outfit_*', '!noble_outfit_09_*', 'exact_set', 'padded', '# not a comment, a pattern'])
        self.assertTrue(convert_list.matches('noble_outfit_01_x', pats))
        self.assertFalse(convert_list.matches('noble_outfit_09_x', pats))     # exclude wins
        self.assertTrue(convert_list.matches('exact_set', pats))
        self.assertFalse(convert_list.matches('exact_set_2', pats))
        self.assertFalse(convert_list.matches('unknown', pats))
        self.assertEqual(convert_list.status('noble_outfit_09_x', pats)['state'], 'excluded')
        self.assertEqual(convert_list.status('unknown', pats)['state'], 'unlisted')

    def test_matches_agrees_with_convert_queue_next_job(self):
        import convert_queue
        self.sets_file('good_set\n!bad_*\nbad_set\n')
        for set_id in ('good_set', 'bad_set', 'other_set'):
            self.stage('multiview_ready', set_id)
        with mock.patch.object(convert_queue, 'ROOT', self.root), mock.patch.object(convert_queue, 'LOCKS', self.root / 'locks'):
            job, owner = convert_queue.next_job(set(convert_list.patterns(self.root)))
        self.assertEqual(job.name.split('--')[0], 'good_set')
        self.assertEqual(sorted(j['set'] for j in convert_list.pending_jobs(self.root)), ['good_set'])
        self.assertEqual(sorted((j['set'], j['reason']) for j in convert_list.held_jobs(self.root)),
                         [('bad_set', 'excluded'), ('other_set', 'unlisted')])

    def races(self, *races):
        for race in races:
            (self.root / 'base_views' / race).mkdir(parents=True, exist_ok=True)
            (self.root / 'base_views' / race / 'palette.json').write_text('{}', encoding='utf-8')

    def test_add_sets_appends_exact_family_ids_idempotently_and_keeps_everything_else(self):
        """DESIGN_PHASE4.md I.6: `<set>`, `<set>_<race>` for every race with a palette.json and `<set>_gnome`; never a wildcard."""
        self.races('human_male', 'gnome_female')
        (self.root / 'base_views' / 'no_palette').mkdir(parents=True)
        original = '# header\nalpha_*\n\n!beta_*\n# tail comment\ngamma'
        self.sets_file(original)                                              # no trailing newline
        result = convert_list.add_sets(self.root, ['delta_01_x'])
        family = ['delta_01_x', 'delta_01_x_gnome_female', 'delta_01_x_human_male', 'delta_01_x_gnome']
        self.assertEqual(result['added'], family)
        text = convert_list.list_path(self.root).read_text(encoding='utf-8')
        self.assertEqual(text, original + '\n' + ''.join(f + '\n' for f in family))
        self.assertFalse(any('*' in line for line in family))
        again = convert_list.add_sets(self.root, ['delta_01_x', 'eps_02'])
        self.assertEqual(again['added'], ['eps_02', 'eps_02_gnome_female', 'eps_02_human_male', 'eps_02_gnome'])
        self.assertEqual(again['present'], family)
        pats = convert_list.patterns(self.root)
        self.assertTrue(convert_list.matches('delta_01_x_gnome', pats))         # the historical gnome-only job `<set>_gnome--<id>`
        self.assertTrue(convert_list.matches('delta_01_x_gnome_female', pats))
        self.assertFalse(convert_list.matches('delta_01_x_other', pats))         # no wildcard: an unrelated name never matches
        self.assertEqual(convert_list.add_sets(self.root, ['zeta'], extra=['zeta_legacy'])['added'][-1], 'zeta_legacy')

    def test_add_sets_keeps_crlf_creates_the_file_and_reports_excludes(self):
        self.assertEqual(convert_list.patterns(self.root), [])
        convert_list.add_sets(self.root, 'first_set')
        self.assertEqual(convert_list.list_path(self.root).read_text(encoding='utf-8'), 'first_set\n')
        self.sets_file('# h\r\n!blocked_*\r\n')
        result = convert_list.add_sets(self.root, ['blocked_set'])
        self.assertEqual(result['excluded'], {'blocked_set': ['!blocked_*']})
        self.assertEqual(convert_list.list_path(self.root).read_bytes(), b'# h\r\n!blocked_*\r\nblocked_set\r\n')
        self.assertFalse(convert_list.is_listed(self.root, 'blocked_set'))     # still excluded: only the user can lift the `!`

    def test_add_sets_rejects_bad_ids_with_the_approval_grammar(self):
        import serve
        self.assertEqual(convert_list.NAME.pattern, serve.NAME.pattern)        # ONE set-name grammar for approve and release (F3)
        for bad in ('', 'Has Space', '../x', 'a/b', '*', 'x*', '!x', '#x', '-x', 'a?b', '[a]'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                convert_list.add_sets(self.root, [bad])
        self.assertFalse(convert_list.list_path(self.root).exists())
        self.assertEqual(convert_list.add_sets(self.root, ['red-coat', 'Upper_1'])['added'], ['red-coat', 'Upper_1'])

    def test_add_sets_is_safe_under_concurrent_writers(self):
        self.races('human_male')
        self.sets_file('# x\n')
        threads = [threading.Thread(target=convert_list.add_sets, args=(self.root, [f'set_{i}'])) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = convert_list.patterns(self.root)
        self.assertEqual(len(lines), 24)
        self.assertEqual(len(set(lines)), 24)

    def test_eligible_jobs_follow_the_workers_rules(self):
        """Review F20: work = a listed staged job under MAX_TRIES failures with a module nobody converts."""
        self.sets_file('s\n')
        self.stage('multiview_ready', 's')
        self.assertEqual([j['job'] for j in convert_list.eligible_jobs(self.root)], ['s--aaa111'])
        locks = self.root / '.codex_jobs/_batch/convert_locks'
        locks.mkdir(parents=True)
        (locks / 's--aaa111.fails').write_text(str(convert_list.MAX_TRIES))
        self.assertEqual(convert_list.eligible_jobs(self.root), [])            # stuck: the worker would only exit idle again
        self.assertFalse(services.has_work(self.root))
        (locks / 's--aaa111.fails').write_text('2')
        self.assertTrue(services.has_work(self.root))
        mod_locks = self.root / 'models/s/aaa111/.module_locks'
        mod_locks.mkdir(parents=True)
        (mod_locks / 'human_male__outfit.lock').write_text(str(os.getpid()))  # a live holder converts the only module
        self.assertEqual(convert_list.eligible_jobs(self.root), [])
        import convert_queue
        self.assertEqual(convert_list.MAX_TRIES, convert_queue.MAX_TRIES)

    def test_held_jobs_skip_converted_and_finished_jobs(self):
        self.sets_file('listed_set\n')
        self.stage('multiview_ready', 'held_set')
        self.stage('processing', 'held_done', state='converted')
        self.stage('processing', 'held_all_modules', converted=(('human_male', 'outfit'),))
        self.stage('processing', 'held_half', modules=(('human_male', 'outfit'), ('elf_male', 'outfit')), converted=(('human_male', 'outfit'),))
        self.stage('multiview_ready', 'listed_set')
        broken = self.root / 'processing' / 'held_broken--zzz'
        broken.mkdir(parents=True)
        (broken / '.tripo-job.json').write_text('{oops', encoding='utf-8')
        held = convert_list.held_jobs(self.root)
        self.assertEqual(sorted(j['set'] for j in held), ['held_half', 'held_set'])
        self.assertEqual({j['stage'] for j in held}, {'processing', 'multiview_ready'})
        self.assertEqual([j['modules_left'] for j in held if j['set'] == 'held_half'], [1])
        convert_list.add_sets(self.root, ['held_set', 'held_half'])
        self.assertEqual(convert_list.held_jobs(self.root), [])                # Release
        self.assertEqual(sorted(j['set'] for j in convert_list.pending_jobs(self.root)), ['held_half', 'held_set', 'listed_set'])


# ----------------------------------------------------------------------------------------------------------------------- credits

class CreditsTest(TempRoot):
    def submission(self, rel, balances, mtime=None):
        path = self.root / rel / 'studio_submission.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'balances': balances}), encoding='utf-8')
        if mtime:
            os.utime(path, (mtime, mtime))
        return path

    def test_tripo_state_off_and_cached(self):
        state = credits.tripo_state(self.root)
        self.assertEqual(state['mode'], 'off')
        self.assertEqual(state['text'], 'off (no credits this month)')
        self.assertIsNone(state['credits'])

    def test_check_tripo_runs_node_only_when_nothing_uses_the_profile(self):
        calls = []

        def run(root):
            calls.append(root)
            return 0, 'log noise\n{"credits": 123}\n', ''
        job = self.spawn('convert_job')
        busy = credits.check_tripo(self.root, run=run)
        self.assertEqual(busy['status'], 'busy')
        self.assertIn('convert_job.py', busy['message'])
        self.assertEqual(calls, [])                                            # never started the check while a conversion runs
        self.assertFalse(credits.cache_path(self.root).exists())
        job.kill()
        job.wait(10)
        self.wait_for(lambda: not self.running('convert_job'))
        result = credits.check_tripo(self.root, run=run, now=1_700_000_000)
        self.assertEqual((result['status'], result['credits']), ('ok', 123))
        self.assertEqual(len(calls), 1)
        cached = json.loads(credits.cache_path(self.root).read_text(encoding='utf-8'))['tripo']
        self.assertEqual((cached['credits'], cached['ok']), (123, True))
        state = credits.tripo_state(self.root)
        self.assertEqual((state['credits'], state['checked_at']), (123, cached['checked_at']))

    def test_check_tripo_failures_keep_the_last_good_balance(self):
        credits.check_tripo(self.root, run=lambda r: (0, '{"credits": 50}', ''))
        failed = credits.check_tripo(self.root, run=lambda r: (1, 'no json here', 'browser profile locked'))
        self.assertEqual(failed['status'], 'error')
        self.assertIn('browser profile locked', failed['message'])
        state = credits.tripo_state(self.root)
        self.assertEqual((state['credits'], state['ok']), (50, False))

        def explode(root):
            raise OSError('node missing')
        self.assertEqual(credits.check_tripo(self.root, run=explode)['status'], 'error')

    def test_hyper3d_picks_the_newest_balance_across_models_and_extra_roots(self):
        extra = self.root / 'monsters' / 'pipeline'
        old, new = 1_700_000_000, 1_700_100_000
        self.submission('models/s1/j1/human_male/outfit',
                        [{'stage': 'a', 'credits': 900, 'at': '2026-11-01T10:00:00+00:00'}, {'stage': 'b', 'credits': 880.5, 'at': '2026-11-02T10:00:00+00:00'}], old)
        self.submission('models/s2/j2/elf_male/outfit', [{'stage': 'c', 'credits': 700, 'at': '2026-11-03T09:00:00.123456+00:00'}], old + 10)
        self.submission('models/s3/j3/elf_male/outfit', [{'stage': 'x'}, {'credits': 'n/a', 'at': '2026-12-01T00:00:00+00:00'}], old + 20)
        (self.root / 'models/s4/j4/dwarf/outfit').mkdir(parents=True)
        (self.root / 'models/s4/j4/dwarf/outfit/studio_submission.json').write_text('{broken', encoding='utf-8')
        self.submission('models/s5/j5/dwarf_male/outfit/too/deep', [{'credits': 1, 'at': '2026-12-31T00:00:00+00:00'}], new)    # beyond the depth limit
        self.submission('monsters/pipeline/Goblin/models/1/hyper3d', [{'stage': 'm', 'credits': 650, 'at': '2026-11-10T08:00:00+00:00'}], new)
        state = credits.hyper3d_state(self.root, [extra])
        self.assertEqual((state['credits'], state['at'], state['stage']), (650, '2026-11-10T08:00:00+00:00', 'm'))
        self.assertTrue(state['source'].endswith('studio_submission.json'))
        cached = json.loads(credits.cache_path(self.root).read_text(encoding='utf-8'))['hyper3d']
        self.assertEqual(cached['best']['credits'], 650)
        self.assertEqual(credits.hyper3d_state(self.root, [], scan=False)['credits'], 650)

    def test_hyper3d_none_when_no_submission_has_a_balance(self):
        self.assertEqual(credits.hyper3d_state(self.root, [])['credits'], None)
        self.submission('models/s/j/r/m', [])
        self.assertIsNone(credits.hyper3d_state(self.root, [])['credits'])

    def test_hyper3d_rescan_reads_only_changed_files(self):
        def entry(credits_, day, stage='s'):
            return {'credits': credits_, 'at': f'2026-11-{day:02d}T00:00:00+00:00', 'stage': stage}

        def stamp(day):
            return credits._parse_time(f'2026-11-{day:02d}T00:00:00+00:00') + 5      # a file is written just after its newest balance
        self.submission('models/a/j/r/m', [entry(500, 1)], stamp(1))
        self.submission('models/b/j/r/m', [entry(600, 2)], stamp(2))
        reads = []
        real = credits._best_balance
        with mock.patch.object(credits, '_best_balance', side_effect=lambda p: reads.append(p.parent.parent.parent.parent.name) or real(p)):
            self.assertEqual(credits.hyper3d_state(self.root, [])['credits'], 600)
            self.assertGreaterEqual(len(reads), 1)
            reads.clear()
            self.assertEqual(credits.hyper3d_state(self.root, [])['credits'], 600)
            self.assertEqual(reads, [])                                        # nothing changed: no file is read
            self.submission('models/c/j/r/m', [entry(550, 3)], stamp(3))
            state = credits.hyper3d_state(self.root, [])
            self.assertEqual(reads, ['c'])                                     # only the new file
            self.assertEqual(state['credits'], 550)                            # newest by time, not the highest number
            reads.clear()
            self.submission('models/b/j/r/m', [entry(600, 2), entry(400, 9, 't')], stamp(9))
            self.assertEqual(credits.hyper3d_state(self.root, [])['credits'], 400)   # an updated file is read again
            self.assertEqual(reads, ['b'])

    def test_hyper3d_first_scan_stops_at_files_older_than_the_best_balance(self):
        base = 1_700_000_000
        for i in range(6):
            self.submission(f'models/s{i}/j/r/m', [{'credits': 100 + i, 'at': f'2026-11-0{i + 1}T00:00:00+00:00', 'stage': 's'}], base + i * 86400)
        reads = []
        real = credits._best_balance
        with mock.patch.object(credits, '_best_balance', side_effect=lambda p: reads.append(p) or real(p)):
            self.assertEqual(credits.hyper3d_state(self.root, [])['credits'], 105)
        self.assertLess(len(reads), 6)

    def test_snapshot(self):
        snap = credits.snapshot(self.root, [])
        self.assertEqual(set(snap), {'tripo', 'hyper3d'})


# --------------------------------------------------------------------------------------------------------------------------- services

class ServicesTest(TempRoot):
    def tick(self, now=None, holds=False, **overrides):
        """services.tick; the conversion-hold events (I.1, tested on their own) are dropped unless `holds`."""
        result = services.tick(self.root, {**settings.load(self.root), **overrides} if overrides else None, now=now)
        if not holds:
            result['events'] = [e for e in result['events'] if 'conversion hold' not in e]
        return result

    def rec(self, name):
        return services.read_record(self.root, name)

    def settle(self, **overrides):
        """Tick until every enabled service runs (the convert worker starts only after the supervisor's first tick, I.1); returns the events.
        Then one more tick, so the hold release / adoption events are behind us."""
        cfg = {**settings.load(self.root), **overrides}
        events = []
        end = time.monotonic() + 20
        while time.monotonic() < end:
            events += self.tick(**overrides)['events']
            if all(self.running(n) for n, on in cfg['services'].items() if on):
                break
            time.sleep(0.2)
        else:
            self.fail('services did not settle')
        time.sleep(0.2)
        events += self.tick(**overrides)['events']
        return events

    def test_commands_follow_the_settings(self):
        cfg = settings.load(self.root)
        sup = services.SERVICES['supervisor'].command(cfg)
        self.assertEqual(sup[1:], ['-X', 'utf8', '-u', 'tools/pipeline_supervisor.py', 'run'])
        self.assertEqual(services.SERVICES['views'].command({**cfg, 'view_parallel': 5})[1:],
                         ['-u', 'tools/view_batch.py', 'run', '--parallel', '5'])
        convert = services.SERVICES['convert'].command(cfg)
        self.assertEqual(convert[1:], ['-X', 'utf8', '-u', 'tools/convert_queue.py', '--sets-file', '.codex_jobs/_batch/convert_sets.txt',
                                       '--worker', '1', '--no-tripo'])
        self.assertNotIn('--no-tripo', services.SERVICES['convert'].command({**cfg, 'tripo': 'auto'}))

    def test_spawn_is_detached_with_utf8_and_appended_logs(self):
        seen = {}

        class FakePopen:
            pid = os.getpid()

            def __init__(self, cmd, **kwargs):
                seen.update(kwargs, cmd=cmd)

        with mock.patch.object(services.subprocess, 'Popen', FakePopen):
            inst = services._spawn(self.root, services.SERVICES['views'], settings.load(self.root))
        self.assertEqual(seen['cwd'], str(self.root))
        self.assertEqual(seen['env']['PYTHONUTF8'], '1')
        if os.name == 'nt':
            self.assertEqual(seen['creationflags'], 0x00000200 | 0x08000000)   # own hidden console, not DETACHED_PROCESS (no empty terminal tabs)
        self.assertEqual(seen['stdout'].name, str(self.root / '.codex_jobs/_batch/views_win.out.log'))
        self.assertEqual(seen['stderr'].name, str(self.root / '.codex_jobs/_batch/views_win.err.log'))
        self.assertEqual(inst['cmdline'][-2:], ['--parallel', '8'])
        with mock.patch.object(services.subprocess, 'Popen', FakePopen):
            services._spawn(self.root, services.SERVICES['views'], settings.load(self.root))
        self.assertEqual((self.root / '.codex_jobs/_batch/views_win.out.log').read_text(encoding='utf-8').count('preview app starts'), 2)

    def test_adopts_a_hand_started_service_and_never_starts_a_second(self):
        popen = self.spawn('supervisor')
        events = self.tick()['events']
        self.assertIn(f'supervisor: adopted pid {popen.pid}', events)
        rec = self.rec('supervisor')
        self.assertEqual((rec['pid'], rec['adopted'], rec['running']), (popen.pid, True, True))
        self.assertAlmostEqual(rec['create_time'], psutil.Process(popen.pid).create_time(), delta=1.5)
        self.assertIn('pipeline_supervisor.py', ' '.join(rec['cmdline']))
        self.assertEqual(len(self.running('supervisor')), 1)
        self.tick()
        self.assertEqual([i['pid'] for i in self.running('supervisor')], [popen.pid])
        status = services.status(self.root)['services']['supervisor']
        self.assertEqual((status['state'], status['pid'], status['adopted']), ('running', popen.pid, True))
        self.assertGreaterEqual(status['uptime'], 0)

    def test_starts_missing_enabled_services_detached_and_only_once(self):
        result = self.tick()
        started = sorted(e.split(':')[0] for e in result['events'] if ': started' in e)
        self.assertEqual(started, ['supervisor', 'views'])                         # the convert worker waits for the supervisor's first tick
        self.assertTrue(any(e.startswith('convert: waiting for the supervisor') for e in result['events']), result['events'])
        self.assertTrue(services.read_hold(self.root))                              # no supervisor ran: conversions are held (I.1)
        self.assertEqual(self.running('convert'), [])
        self.assertEqual(services.status(self.root)['services']['convert']['state'], 'waiting')
        self.wait_for(lambda: services.supervisor_tick(self.root), what='the supervisor tick')
        events = self.tick()['events']
        self.assertTrue(any(e.startswith('convert: started') for e in events), events)
        for name in ('supervisor', 'views', 'convert'):
            self.wait_for(lambda n=name: self.running(n), what=f'{name} to run')
            self.assertEqual(len(self.running(name)), 1)
            rec = self.rec(name)
            self.assertFalse(rec['adopted'])
            self.assertTrue((self.root / f'.codex_jobs/_batch/{name}_win.out.log').exists())
        args = json.loads((self.root / '.codex_jobs/_app/fake_view_batch.args').read_text())
        self.assertEqual(args, ['run', '--parallel', '8'])
        convert_args = json.loads((self.root / '.codex_jobs/_app/fake_convert_queue.args').read_text())
        self.assertIn('--no-tripo', convert_args)
        self.wait_for(lambda: services.read_hold(self.root) is None or self.tick() and services.read_hold(self.root) is None,
                      what='the hold to be released after the supervisor ticked')
        self.assertEqual(self.tick()['events'], [])                                   # next pass: nothing to do
        for name in ('supervisor', 'views', 'convert'):
            self.assertEqual(len(self.running(name)), 1)
            self.assertEqual(services.status(self.root)['services'][name]['state'], 'running')

    def test_a_disabled_service_is_not_started_but_a_running_one_is_left_alone(self):
        self.tick(services={'supervisor': True, 'views': False, 'convert': False})
        self.wait_for(lambda: self.running('supervisor'))
        self.assertEqual(self.running('views'), [])
        self.assertEqual(services.status(self.root)['services']['views']['state'], 'stopped')
        self.spawn('views')
        self.tick(services={'supervisor': True, 'views': False, 'convert': False})
        self.assertEqual(len(self.running('views')), 1)

    def test_processes_of_other_roots_and_lookalikes_are_ignored(self):
        other = Path(tempfile.mkdtemp(prefix='v2p4_other_')).resolve()
        try:
            script = other / 'tools' / 'view_batch.py'
            script.parent.mkdir(parents=True)
            script.write_text(FAKE, encoding='utf-8')
            popen = subprocess.Popen([sys.executable, 'tools/view_batch.py', 'run'], cwd=str(other), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.popens.append(popen)
            lookalike = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', 'view_batch.py', 'run'], cwd=str(self.root))
            self.popens.append(lookalike)
            time.sleep(0.7)
            self.assertEqual(self.running('views'), [])
            self.assertEqual(services.scan(other, ['views'])['views'][0]['pid'], popen.pid)
            # `run` is required: a service script started with another sub-command is not the service
            plain = subprocess.Popen([sys.executable, 'tools/view_batch.py', 'status'], cwd=str(self.root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.popens.append(plain)
            time.sleep(0.7)
            self.assertEqual(self.running('views'), [])
            # convert workers other than 1 belong to the user
            w2 = subprocess.Popen([sys.executable, 'tools/convert_queue.py', '--sets-file', 'x', '--worker', '2'], cwd=str(self.root),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.popens.append(w2)
            time.sleep(0.7)
            self.assertEqual(self.running('convert'), [])
        finally:
            for popen in self.popens:
                popen.kill()
            shutil.rmtree(other, ignore_errors=True)

    def test_duplicate_instances_are_an_error_and_nothing_is_touched(self):
        settings.save(self.root, VIEWS_ONLY)
        first, second = self.spawn('views'), self.spawn('views')
        self.wait_for(lambda: len(self.running('views')) == 2)
        result = self.tick()
        self.assertTrue(any('2 instances run' in e for e in result['events']))
        status = services.status(self.root)
        self.assertEqual(status['services']['views']['state'], 'duplicate')
        self.assertEqual(len(status['services']['views']['duplicates']), 2)
        self.assertTrue(any('views' in e for e in status['errors']))
        self.assertEqual({i['pid'] for i in self.running('views')}, {first.pid, second.pid})   # neither was killed
        self.assertEqual(self.tick()['events'], [e for e in self.tick()['events'] if 'views' not in e])   # no repeated event for the same pids
        with self.assertRaises(services.ServiceError):
            services.stop(self.root, 'views')
        with self.assertRaises(services.ServiceError):
            services.start(self.root, 'views')
        self.assertEqual(len(self.running('views')), 2)
        second.kill()
        second.wait(10)
        self.wait_for(lambda: len(self.running('views')) == 1)
        self.tick()
        self.assertEqual(services.status(self.root)['services']['views']['state'], 'running')

    def test_stop_supervisor_uses_its_stop_command_and_is_not_restarted(self):
        popen = self.spawn('supervisor')
        self.tick()
        result = services.stop(self.root, 'supervisor', wait=10)
        self.assertTrue(result['ok'])
        self.assertFalse(result['pending'])
        self.assertEqual(self.running('supervisor'), [])
        popen.wait(10)
        self.assertEqual(result['status']['state'], 'stopped')
        self.assertTrue(self.rec('supervisor')['user_stopped'])
        self.assertEqual([e for e in self.tick(now=time.time() + 100000)['events'] if e.startswith('supervisor')], [])   # a deliberate stop is never auto-restarted
        self.assertEqual(self.running('supervisor'), [])
        status = services.start(self.root, 'supervisor')
        self.assertEqual(status['state'], 'running')
        self.assertFalse(status['adopted'])
        self.wait_for(lambda: self.running('supervisor'))

    def test_restart_supervisor(self):
        popen = self.spawn('supervisor')
        self.tick()
        result = services.restart(self.root, 'supervisor', wait=10)
        self.assertEqual(result['status']['state'], 'running')
        self.assertNotEqual(result['status']['pid'], popen.pid)
        self.wait_for(lambda: len(self.running('supervisor')) == 1)
        self.assertFalse(self.rec('supervisor')['user_stopped'])

    def test_supervisor_restart_puts_the_pause_file_back(self):
        pause = self.root / '.codex_jobs/_batch/PAUSE'
        pause.write_text('{"since": "s", "reason": "manual pause-conversions", "scope": ["conversions"]}\n', encoding='utf-8')
        self.spawn('supervisor')
        self.tick()
        result = services.restart(self.root, 'supervisor', wait=10)
        self.assertEqual(result['status']['state'], 'running')
        self.assertTrue(pause.exists())                                                 # the fake removed it on exit; the manager restored it
        self.assertEqual(json.loads(pause.read_text(encoding='utf-8'))['reason'], 'manual pause-conversions')
        self.assertNotIn('pause_backup', self.rec('supervisor'))
        services.stop(self.root, 'supervisor', wait=10)                                 # a plain stop leaves the supervisor's own cleanup alone
        self.assertFalse(pause.exists())

    def test_stop_views_by_pid_after_rechecking_the_process(self):
        settings.save(self.root, VIEWS_ONLY)
        popen = self.spawn('views')
        self.tick()
        inst = self.running('views')[0]
        with self.assertRaises(services.ServiceError):                              # another create time = another process: refuse
            services._terminate(self.root, services.SERVICES['views'], {**inst, 'create_time': inst['create_time'] - 100})
        self.assertEqual(len(self.running('views')), 1)
        result = services.stop(self.root, 'views')
        self.assertFalse(result['pending'])
        self.assertEqual(self.running('views'), [])
        popen.wait(10)
        self.assertEqual(self.tick()['events'], [])
        self.assertEqual(self.running('views'), [])
        self.assertEqual(services.status(self.root)['services']['views']['state'], 'stopped')

    def test_stop_of_a_service_that_is_not_running_blocks_the_auto_start(self):
        result = services.stop(self.root, 'views')
        self.assertIn('not running', result['message'])
        self.assertEqual(self.tick(services={'supervisor': False, 'views': True, 'convert': False})['events'], [])
        self.assertEqual(self.running('views'), [])

    def stop_file(self, worker=1):
        path = self.root / f'.codex_jobs/_batch/convert_worker{worker}.stop'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None

    def test_convert_stop_is_graceful_while_a_conversion_runs(self):
        """I.2: the stop names the worker process; the worker acknowledges (.stopping), finishes its job and exits. Never killed."""
        settings.save(self.root, CONVERT_ONLY)
        worker, job = self.spawn('convert'), self.spawn('convert_job')
        self.tick()
        self.assertEqual(services.status(self.root, deep=True)['convert_jobs'][0]['pid'], job.pid)
        with self.assertRaisesRegex(services.ServiceError, 'convert_job.py is running'):
            services.stop(self.root, 'convert', force=True)
        self.assertIsNone(self.stop_file())
        self.ctl('convert_queue', {'busy': True})                                    # it acknowledges, then finishes the job in flight
        result = services.stop(self.root, 'convert', wait=1.0, request=7)
        self.assertTrue(result['pending'])
        self.assertIn('acknowledged', result['message'])
        stop = self.stop_file()
        self.assertEqual((stop['pid'], stop['request']), (worker.pid, 7))
        self.assertAlmostEqual(stop['create_time'], psutil.Process(worker.pid).create_time(), delta=1.5)
        self.assertTrue((self.root / '.codex_jobs/_batch/convert_worker1.stopping').exists())
        self.assertEqual(result['status']['state'], 'stopping')
        self.assertTrue(result['status']['stop_pending'])
        for _ in range(3):
            self.tick()                                                                # re-evaluated, never killed on a timeout
        self.assertEqual(len(self.running('convert')), 1)
        self.assertEqual(len(self.running('convert_job')), 1)
        self.ctl('convert_queue', {})                                                  # the job (finish + offload) is done: it exits
        self.wait_for(lambda: not self.running('convert'), what='the worker to exit through its stop file')
        worker.wait(10)
        events = self.tick(now=time.time())['events']
        self.assertIn('convert: stopped', events)
        self.assertTrue(self.rec('convert')['user_stopped'])
        self.assertEqual(self.tick()['events'], [])
        self.assertEqual(self.running('convert'), [])
        self.assertIsNone(self.stop_file())
        self.assertFalse((self.root / '.codex_jobs/_batch/convert_worker.stop').exists())   # the old shared marker is never written

    def test_a_convert_stop_never_kills_on_timeout(self):
        """Review F6: a timeout is no proof of an old worker; a worker that has not acknowledged keeps running and the stop stays pending."""
        settings.save(self.root, CONVERT_ONLY)
        worker = self.spawn('convert')
        self.ctl('convert_queue', {'ignore_stop': True})                              # (a busy worker that has not reached its loop top yet)
        self.tick()
        result = services.stop(self.root, 'convert', wait=0.6)
        self.assertTrue(result['pending'])
        self.assertIn('acknowledge', result['message'])
        for _ in range(3):
            self.tick(now=time.time() + 3600)
        self.assertEqual([i['pid'] for i in self.running('convert')], [worker.pid])
        self.assertEqual(services.status(self.root)['services']['convert']['state'], 'stopping')
        self.ctl('convert_queue', {})
        self.wait_for(lambda: not self.running('convert'), what='the worker to honour the stop')
        self.assertIn('convert: stopped', self.tick()['events'])

    def test_a_stop_for_worker_1_is_never_taken_by_worker_2(self):
        settings.save(self.root, CONVERT_ONLY)
        w1 = self.spawn('convert')
        w2 = subprocess.Popen([sys.executable, 'tools/convert_queue.py', '--sets-file', 'x', '--worker', '2'], cwd=str(self.root),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.popens.append(w2)
        self.ctl('convert_queue', {'busy': True})
        self.tick()
        self.assertEqual([i['pid'] for i in self.running('convert')], [w1.pid])        # worker 2 is the user's own
        result = services.stop(self.root, 'convert', wait=1.0)
        self.assertTrue(result['pending'])
        self.assertEqual(self.stop_file()['pid'], w1.pid)
        self.assertIsNone(self.stop_file(2))
        self.ctl('convert_queue', {})
        self.wait_for(lambda: not self.running('convert'), what='worker 1 to exit')
        time.sleep(0.5)
        self.assertIsNone(w2.poll())                                                   # worker 2 keeps running

    def test_a_legacy_worker_is_stopped_only_while_it_sleeps(self):
        """I.2 legacy rule: no stop support -> stopped through its verified handle only when no convert_job.py runs and its last event since it
        started is PAUSED / IDLE / LOW DISK after its last CONVERT."""
        settings.save(self.root, CONVERT_ONLY)
        events_log = self.root / '.codex_jobs/_batch/convert_events.log'
        old = self.spawn('convert')
        self.ctl('convert_queue', {'ignore_stop': True})                               # it never reads a stop file
        script = self.root / 'tools/convert_queue.py'
        os.utime(script, (time.time() + 600, time.time() + 600))                       # it runs code older than the current file
        self.assertTrue(services.legacy_worker(self.root, self.running('convert')[0]))
        self.tick()

        def event(text):
            with open(events_log, 'a', encoding='utf-8') as handle:
                handle.write(f'{datetime.now():%m-%d %H:%M:%S} {text}\n')
        event('CONVERT s--abc123')
        result = services.stop(self.root, 'convert', wait=0.5)
        self.assertTrue(result['pending'])
        self.assertIn('sleeps', result['message'])
        event('[w2] PAUSED (PAUSE file); not starting conversions')                   # another worker's line does not count
        self.tick()
        self.assertEqual(len(self.running('convert')), 1)
        job = self.spawn('convert_job')
        event('PAUSED (PAUSE file); not starting conversions')
        self.tick()
        self.assertEqual(len(self.running('convert')), 1)                             # a convert_job.py runs: not touched
        self.assertIn('convert_job.py', services.status(self.root)['services']['convert']['message'])
        job.kill()
        job.wait(10)
        self.wait_for(lambda: not self.running('convert_job'))
        events = self.tick()['events']
        self.assertTrue(any('asleep' in e for e in events), events)
        old.wait(10)
        self.assertEqual(self.running('convert'), [])
        self.assertTrue(self.rec('convert')['user_stopped'])

    def test_convert_restart_waits_for_the_conversion_then_restarts(self):
        settings.save(self.root, CONVERT_ONLY)
        self.spawn('convert')
        job = self.spawn('convert_job')
        self.ctl('convert_queue', {'busy': True})
        self.tick()
        result = services.restart(self.root, 'convert', wait=0.5)
        self.assertTrue(result['pending'])
        self.ctl('convert_queue', {})
        self.wait_for(lambda: not self.running('convert'), what='worker exit')
        self.tick()
        self.wait_for(lambda: self.running('convert'), what='restart by the next tick')
        self.assertFalse(self.rec('convert')['user_stopped'])
        self.assertIn(job.pid, [i['pid'] for i in self.running('convert_job')])

    def test_a_stale_per_worker_stop_file_is_removed(self):
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait(10)
        path = self.root / '.codex_jobs/_batch/convert_worker1.stop'
        path.write_text(json.dumps({'pid': dead.pid, 'create_time': 1.0, 'request': 1, 'time': 'x'}), encoding='utf-8')
        (self.root / '.codex_jobs/_batch/convert_worker1.stopping').write_text(json.dumps({'pid': dead.pid}), encoding='utf-8')
        self.tick(services={'supervisor': False, 'views': False, 'convert': False})
        self.assertFalse(path.exists())
        self.assertFalse((self.root / '.codex_jobs/_batch/convert_worker1.stopping').exists())

    def test_crash_restart_backoff_then_hold(self):
        self.settle()
        for name in ('supervisor', 'views', 'convert'):
            self.wait_for(lambda n=name: self.running(n))
        t0 = time.time()
        self.kill('views')
        events = self.tick(now=t0)['events']
        self.assertTrue(any(e.startswith('views: exited unexpectedly; restart in 60 s') for e in events), events)
        status = services.status(self.root, now=t0)['services']['views']
        self.assertEqual((status['state'], status['restart_in'], status['fails']), ('crashed', 60, 1))
        self.assertEqual(self.tick(now=t0 + 30)['events'], [])                          # too early
        self.assertEqual(self.running('views'), [])
        events = self.tick(now=t0 + 61)['events']
        self.assertTrue(any('views: started' in e for e in events), events)
        self.wait_for(lambda: self.running('views'))
        self.kill('views')                                                              # second quick failure
        events = self.tick(now=t0 + 62)['events']
        self.assertTrue(any('restart in 300 s' in e for e in events), events)
        self.assertEqual(self.tick(now=t0 + 200)['events'], [])
        self.assertTrue(any('views: started' in e for e in self.tick(now=t0 + 363)['events']))
        self.wait_for(lambda: self.running('views'))
        self.kill('views')                                                              # third failure: held
        events = self.tick(now=t0 + 364)['events']
        self.assertTrue(any('held after 3 failures' in e for e in events), events)
        status = services.status(self.root, now=t0 + 364)['services']['views']
        self.assertEqual(status['state'], 'held')
        self.assertIn('restart failed 3 times', status['error'])
        self.assertEqual(self.tick(now=t0 + 100000)['events'], [])                      # never again by itself
        self.assertEqual(self.running('views'), [])
        started = services.start(self.root, 'views')                                    # the Start button clears the hold
        self.assertEqual(started['state'], 'running')
        self.assertEqual((started['fails'], started['error']), (0, None))
        self.wait_for(lambda: self.running('views'))
        self.assertEqual(self.rec('views')['held'], False)

    def test_a_clean_exit_counts_as_a_failure_for_non_convert_services(self):
        self.tick()
        self.wait_for(lambda: self.running('views'))
        self.ctl('view_batch', {'exit_after': 0.0, 'code': 0})
        self.wait_for(lambda: not self.running('views'), what='clean exit')
        events = self.tick(now=time.time())['events']
        self.assertTrue(any('views: exited unexpectedly' in e for e in events), events)

    def test_a_stable_run_resets_the_failure_count(self):
        self.tick()
        self.wait_for(lambda: self.running('views'))
        t0 = time.time()
        self.kill('views')
        self.tick(now=t0)
        self.tick(now=t0 + 61)
        self.wait_for(lambda: self.running('views'))
        self.assertEqual(self.rec('views')['fails'], 1)
        self.tick(now=t0 + 61 + services.STABLE_AFTER + 5)
        self.assertEqual((self.rec('views')['fails'], self.rec('views')['error']), (0, None))

    def test_the_convert_worker_idle_exit_restarts_only_when_work_appears(self):
        self.settle()
        self.wait_for(lambda: self.running('convert'))
        self.ctl('convert_queue', {'exit_after': 0.0, 'code': 0, 'idle_exit': True})
        self.wait_for(lambda: not self.running('convert'), what='idle exit')
        events = self.tick(now=time.time())['events']
        self.assertIn('convert: exited (idle, nothing staged)', events)
        status = services.status(self.root)['services']['convert']
        self.assertEqual((status['state'], status['fails']), ('idle', 0))
        self.ctl('convert_queue', {})
        self.assertEqual(self.tick()['events'], [])                                      # still nothing staged: stays down
        self.assertEqual(self.running('convert'), [])
        job = self.root / 'multiview_ready' / 'new_set--abc123'
        job.mkdir(parents=True)
        (job / '.tripo-job.json').write_text(json.dumps({'set': 'new_set', 'job_id': 'abc123', 'state': 'ready',
                                                          'modules': [{'race_gender': 'human_male', 'module': 'outfit'}]}), encoding='utf-8')
        self.assertEqual(self.tick()['events'], [])                                      # staged but not in the convert list: held, no start
        convert_list.add_sets(self.root, ['new_set'])
        events = self.tick()['events']
        self.assertTrue(any('convert: started' in e and 'work appeared' in e for e in events), events)
        self.wait_for(lambda: self.running('convert'))

    def test_a_stale_stop_file_is_removed_when_nothing_runs(self):
        stop = self.root / '.codex_jobs/_batch/convert_worker.stop'
        stop.write_text('old', encoding='utf-8')
        self.tick(services={'supervisor': False, 'views': False, 'convert': False})
        self.assertFalse(stop.exists())

    def test_start_refuses_when_running_and_a_spawn_failure_counts_as_a_failure(self):
        self.spawn('views')
        with self.assertRaisesRegex(services.ServiceError, 'already running'):
            services.start(self.root, 'views')
        with self.assertRaises(services.ServiceError):
            services.start(self.root, 'nope')
        self.kill('views')
        with mock.patch.object(services, '_spawn', side_effect=OSError('no python')):
            events = self.tick(services={'supervisor': False, 'views': True, 'convert': False})['events']
            self.assertTrue(any('views: exited unexpectedly' in e for e in events) or any('could not start' in e for e in events), events)
            with self.assertRaisesRegex(services.ServiceError, 'could not start'):
                services.start(self.root, 'views')

    def test_status_shows_the_last_log_lines_and_the_last_error(self):
        batch = self.root / '.codex_jobs/_batch'
        (batch / 'views_win.out.log').write_text('\n'.join(f'line {i}' for i in range(50)) + '\n', encoding='utf-8')
        (batch / 'views_win.err.log').write_text('Traceback\nValueError: boom\n', encoding='utf-8')
        (batch / 'events.log').write_text('10:00 DONE x ok\n', encoding='utf-8')
        status = services.status(self.root, lines=5)['services']['views']
        self.assertEqual(status['logs']['out'], [f'line {i}' for i in range(45, 50)])
        self.assertEqual(status['logs']['err'][-1], 'ValueError: boom')
        self.assertEqual(status['logs']['events'], ['10:00 DONE x ok'])
        self.assertEqual(status['logs']['out_file'], 'views_win.out.log')
        legacy = batch / 'view_batch_win8.out.log'                                      # an adopted hand-started runner logs elsewhere: the newest wins
        legacy.write_text('legacy line\n', encoding='utf-8')
        os.utime(legacy, (time.time() + 50, time.time() + 50))
        self.assertEqual(services.status(self.root, lines=5)['services']['views']['logs']['out'], ['legacy line'])
        self.tick(services={'supervisor': False, 'views': True, 'convert': False})
        self.wait_for(lambda: self.running('views'))
        self.kill('views')
        self.tick(now=time.time())
        self.assertIn('ValueError: boom', services.status(self.root)['services']['views']['error'])

    def test_a_reused_pid_is_not_a_running_service(self):
        services.write_record(self.root, 'views', {'pid': os.getpid(), 'create_time': time.time() - 99999, 'running': True, 'adopted': False})
        self.assertEqual(services.status(self.root)['services']['views']['state'], 'stopped')

    def test_scan_scopes_convert_job_to_the_root(self):
        self.assertEqual(services.convert_job_running(self.root), [])
        job = self.spawn('convert_job')
        self.assertEqual([i['pid'] for i in services.convert_job_running(self.root)], [job.pid])
        other = Path(tempfile.mkdtemp(prefix='v2p4_other_')).resolve()
        try:
            self.assertEqual(services.convert_job_running(other), [])
        finally:
            shutil.rmtree(other, ignore_errors=True)


# --------------------------------------------------------------------------- supervisor claim lock (DESIGN_PHASE4 D) and convert stop file

class SupervisorClaimLockTest(unittest.TestCase):
    def setUp(self):
        import test_pipeline_supervisor as tps
        import pipeline_supervisor as ps
        self.tps, self.ps = tps, ps
        self.root = Path(tempfile.mkdtemp(prefix='v2p4_sup_')).resolve()
        for race in tps.RACES:
            tps.png(self.root / 'base_views' / race / 'front.png')
        (self.root / '.codex_jobs' / '_batch' / 'tasks').mkdir(parents=True)
        (self.root / '.codex_jobs' / '_batch' / 'status').mkdir(parents=True)
        tps.write(self.root / '.codex_jobs' / '_batch' / 'briefs' / 'rules.txt', 'rules')
        tps.write(self.root / '.codex_jobs' / '_batch' / 'advance.json', {'sets': [
            {'pattern': 'ragged_outfit_*', 'extra_file': '.codex_jobs/_batch/briefs/rules.txt', 'views_priority': 2}]})
        for race in tps.RACES:
            tps.make_front(self.root, race=race)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def make_sup(self, observe):
        tps = self.tps
        sup = tps.make_sup(self.root)
        inner = sup.run_cmd

        def run(command, timeout=900):
            command_text = ' '.join(str(c) for c in command)
            if ' claim ' in command_text:
                observe(sup)
            return inner(command, timeout)
        run.of = inner.of
        sup.run_cmd = run
        return sup

    def test_gate1_holds_the_set_lock_from_before_the_first_move_until_claim_returns(self):
        from claim_lock import ClaimBusy, claim_lock
        seen = {}

        def observe(sup):
            seen['approved_exists'] = (self.root / 'approved' / self.tps.SET).is_dir()
            seen['env'] = sup.environment().get('IMAGETO3D_CLAIM_LOCK_HELD')
            try:
                with claim_lock(self.root, self.tps.SET):
                    seen['busy'] = False
            except ClaimBusy:
                seen['busy'] = True

        sup = self.make_sup(observe)
        real_rename = Path.rename
        moves = []

        def watching_rename(path, target):
            if 'concepts' in str(path) and 'approved' in str(target):
                try:
                    with claim_lock(self.root, self.tps.SET):
                        moves.append('unlocked')
                except ClaimBusy:
                    moves.append('locked')
            return real_rename(path, target)

        with mock.patch.object(Path, 'rename', watching_rename):
            sup.advance()
        self.assertEqual(moves, ['locked', 'locked'])                  # both race folders moved while the lock was held
        self.assertTrue(seen['approved_exists'] and seen['busy'])      # still held while the claim child runs
        self.assertEqual(seen['env'], self.tps.SET)                    # the child is told its caller holds the lock
        self.assertIsNone(sup.claim_held)
        self.assertNotIn('IMAGETO3D_CLAIM_LOCK_HELD', sup.environment())
        with claim_lock(self.root, self.tps.SET):                      # released after the claim returned
            pass
        self.assertEqual(len(sup.run_cmd.of('add-views')), 1)
        self.assertTrue(self.ps.read_json(sup.state_file)['sets'][self.tps.SET]['views_added'])

    def test_a_busy_claim_lock_leaves_the_folders_in_concepts_for_the_next_pass(self):
        from claim_lock import claim_lock
        sup = self.make_sup(lambda s: None)
        with mock.patch.object(self.ps, 'CLAIM_LOCK_WAIT', 0.2):
            with claim_lock(self.root, self.tps.SET):                  # the preview app is unapproving this set
                sup.advance()
                self.assertTrue((self.root / 'concepts' / self.tps.SET / 'dwarf_male').is_dir())
                self.assertFalse((self.root / 'approved' / self.tps.SET).exists())
                self.assertFalse(sup.run_cmd.of('claim'))
                self.assertIsNone(sup.claim_held)
            text = (self.root / '.codex_jobs/_batch/events.log').read_text(encoding='utf-8')
            self.assertIn('claim lock busy', text)
            sup.advance()                                              # free again: the next pass claims
        self.assertTrue(sup.run_cmd.of('claim'))
        self.assertTrue(self.ps.read_json(sup.state_file)['sets'][self.tps.SET]['views_added'])

    def test_the_claim_child_skips_its_own_lock_only_when_told(self):
        """workflow.claim waits for the lock unless IMAGETO3D_CLAIM_LOCK_HELD names this set (the supervisor holds it)."""
        import workflow
        from claim_lock import claim_lock
        calls = []
        with mock.patch.object(workflow, '_claim_locked', side_effect=lambda root, name, base: calls.append(name) or {'path': 'x'}):
            with claim_lock(self.root, 'some_set'):
                with mock.patch.dict(os.environ, {'IMAGETO3D_CLAIM_LOCK_HELD': 'some_set'}):
                    self.assertEqual(workflow.claim(self.root, 'some_set'), {'path': 'x'})
                self.assertEqual(calls, ['some_set'])
                with mock.patch.dict(os.environ, {'IMAGETO3D_CLAIM_LOCK_HELD': 'another_set'}), \
                        mock.patch.object(workflow, 'claim_lock', side_effect=RuntimeError('would take the lock')):
                    with self.assertRaisesRegex(RuntimeError, 'would take the lock'):
                        workflow.claim(self.root, 'some_set')


class ConvertQueueStopFileTest(unittest.TestCase):
    def setUp(self):
        import convert_queue
        self.cq = convert_queue
        self.real_nap = convert_queue.nap
        self.root = Path(tempfile.mkdtemp(prefix='v2p4_cq_')).resolve()
        batch = self.root / '.codex_jobs' / '_batch'
        batch.mkdir(parents=True)
        self.stop = batch / 'convert_worker.stop'
        self.patches = [mock.patch.object(convert_queue, 'ROOT', self.root), mock.patch.object(convert_queue, 'EVENTS', batch / 'convert_events.log'),
                        mock.patch.object(convert_queue, 'LOCKS', batch / 'convert_locks'), mock.patch.object(convert_queue, 'PAUSE', batch / 'PAUSE'),
                        mock.patch.object(convert_queue, 'STOP_FILE', self.stop), mock.patch.object(convert_queue, 'MIN_FREE', 0),
                        mock.patch.object(convert_queue, 'nap', lambda seconds: None),
                        mock.patch.object(convert_queue.portable, 'process_running', lambda pattern: pattern.startswith('view_batch'))]
        self.finished = []
        self.patches.append(mock.patch.object(convert_queue, 'finish', lambda name=None: self.finished.append(name)))
        for patch in self.patches:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])
        self.addCleanup(shutil.rmtree, self.root, True)

    def events(self):
        path = self.root / '.codex_jobs/_batch/convert_events.log'
        return path.read_text(encoding='utf-8').splitlines() if path.exists() else []

    def test_stop_file_makes_an_idle_worker_exit_through_finish(self):
        self.stop.write_text('stale', encoding='utf-8')               # left from before the worker started: removed, no exit
        calls = []

        def next_job(sets):
            calls.append(not self.stop.exists())                        # first call: the stale file is already gone
            if len(calls) == 1:
                self.stop.write_text('stop now', encoding='utf-8')     # the app asks for a stop while the worker waits for jobs
            return None, False

        with mock.patch.object(self.cq, 'next_job', next_job):
            self.cq.main(['--sets', 'x', '--poll', '1', '--no-tripo'])
        self.assertEqual(calls, [True])
        self.assertFalse(self.stop.exists())
        self.assertTrue(any(line.endswith('STOP requested; exiting') for line in self.events()), self.events())
        self.assertEqual(self.finished, [None])                         # finish() ran like the idle exit

    def test_stop_during_a_conversion_is_honoured_before_the_next_job(self):
        taken = []

        def next_job(sets):
            taken.append(1)
            return self.root / 'processing' / 'set_a--abc', True

        def convert(job, args):
            self.stop.write_text('x', encoding='utf-8')               # requested while the job converts: it finishes first
            return 0

        with mock.patch.object(self.cq, 'next_job', next_job), mock.patch.object(self.cq, 'convert', convert), \
                mock.patch.object(self.cq, 'release', lambda name: None):
            self.cq.main(['--sets', 'x', '--no-tripo'])
        self.assertEqual(len(taken), 1)                                 # no second job was taken
        events = self.events()
        self.assertTrue(any('CONVERTED set_a--abc' in line for line in events), events)
        self.assertTrue(events[-1].endswith('STOP requested; exiting'))
        self.assertFalse(self.stop.exists())
        self.assertEqual(self.finished, ['set_a--abc', None])

    def test_stop_file_is_checked_while_paused(self):
        (self.root / '.codex_jobs/_batch/PAUSE').write_text('{}', encoding='utf-8')
        naps = []

        def nap(seconds):
            naps.append(seconds)
            self.stop.write_text('x', encoding='utf-8')

        with mock.patch.object(self.cq, 'nap', nap), mock.patch.object(self.cq, 'next_job', side_effect=AssertionError('took a job')):
            self.cq.main(['--sets', 'x', '--no-tripo'])
        self.assertEqual(len(naps), 1)
        self.assertTrue(any('PAUSED' in line for line in self.events()))
        self.assertTrue(self.events()[-1].endswith('STOP requested; exiting'))

    def test_nap_wakes_for_a_stop_request(self):
        self.stop.write_text('x', encoding='utf-8')
        started = time.time()
        self.real_nap(30)                                              # returns at once: the stop file is there
        self.assertLess(time.time() - started, 2)
        self.stop.unlink()
        started = time.time()
        with mock.patch.object(self.cq, 'STOP_NAP', 0.1):
            self.real_nap(0.3)
        self.assertGreaterEqual(time.time() - started, 0.25)


class GateTransitionTest(TempRoot):
    """Last-check review row 6: every write / removal of CONVERT_HOLD and CONVERT_DRAIN (and the PAUSE the manager puts back) happens under
    the paid-gate transition mutex (tools/paid_gate.py), which a paid click holds from its final gate check to its dispatch."""

    def setUp(self):
        super().setUp()
        import paid_gate
        self.gate = paid_gate
        self.batch = self.root / '.codex_jobs' / '_batch'

    def test_every_hold_and_drain_change_is_made_under_the_mutex(self):
        seen, watched = [], {'CONVERT_HOLD', 'CONVERT_DRAIN', 'PAUSE'}
        real_write, real_unlink = guard.write_atomic, Path.unlink

        def owner():
            return (self.gate.lock_owner(self.root) or {}).get('what')   # None: nobody holds the lock (an unserialized write)

        def write(path, data):
            if Path(path).name in watched:
                seen.append(('write', Path(path).name, owner()))
            real_write(path, data)

        def unlink(path, missing_ok=False):
            if path.name in watched and path.parent == self.batch:
                seen.append(('unlink', path.name, owner()))
            real_unlink(path, missing_ok=missing_ok)
        me = {'pid': os.getpid(), 'create_time': services._my_create_time()}
        old = {'pid': 999999, 'create_time': 1.0}
        with mock.patch.object(guard, 'write_atomic', write), mock.patch.object(Path, 'unlink', unlink):
            services.write_hold(self.root, 'supervisor restart', now=time.time() - 60, retiring=[old])
            self.assertTrue(services.withdraw_retiring(self.root, old))
            (self.batch / services.TICK_FILE).write_text(json.dumps({'pid': me['pid'], 'create_time': me['create_time'], 'time': time.time()}))
            self.assertIn('released', services.release_hold(self.root, me) or '')
            services.write_drain(self.root, old)
            services.settle_drain(self.root)
            services._restore_pause(self.root, {'pause_backup': '{"reason": "user active"}'})
        self.assertEqual(seen, [('write', 'CONVERT_HOLD', 'CONVERT_HOLD (supervisor restart)'),
                                ('write', 'CONVERT_HOLD', 'CONVERT_HOLD (retiring withdrawn)'),
                                ('unlink', 'CONVERT_HOLD', 'CONVERT_HOLD released'),
                                ('write', 'CONVERT_DRAIN', 'CONVERT_DRAIN'), ('unlink', 'CONVERT_DRAIN', 'CONVERT_DRAIN removed'),
                                ('write', 'PAUSE', 'PAUSE restored after a supervisor restart')])
        self.assertFalse(self.gate.lock_busy(self.root))                # released every time
        self.assertFalse((self.batch / 'CONVERT_HOLD').exists() or (self.batch / 'CONVERT_DRAIN').exists())

    def test_the_reviewers_case_a_hold_written_during_the_intent_write_cannot_be_followed_by_a_click(self):
        """The click's critical section (final check, intent, dispatch) holds the mutex: the manager's hold lands after the click was
        dispatched, or before the check (then nothing is clicked). Never between the two."""
        env = self.gate.env_for(self.root)
        hold, order = self.batch / 'CONVERT_HOLD', []
        writer = threading.Thread(target=lambda: (services.write_hold(self.root, 'supervisor restart'), order.append('hold written')))

        def intent_then_click():
            order.append('intent')
            writer.start()                                              # the hold is requested exactly while the intent is written
            writer.join(0.4)
            self.assertTrue(writer.is_alive(), 'the hold writer did not wait for the click')
            self.assertFalse(hold.exists())
            order.append('click')
        self.gate.checked_dispatch(intent_then_click, env)
        writer.join(20)
        self.assertEqual(order, ['intent', 'click', 'hold written'])
        self.assertTrue(hold.exists())
        with self.assertRaisesRegex(self.gate.GateClosed, 'CONVERT_HOLD'):  # and the next click sees it under the mutex
            self.gate.checked_dispatch(lambda: order.append('second click'), env)
        self.assertNotIn('second click', order)
        self.assertFalse(self.gate.lock_busy(self.root))

    def test_a_crashed_holder_leaves_no_lock_and_a_busy_lock_is_retried_then_refused(self):
        """Post-cut-over review, blocker 2: the lock is an OS byte lock. A holder that died holds nothing (the kernel released it; no
        stale folder to reclaim), and a holder that is alive is never overtaken: after the retries the write is refused, not made."""
        holder = subprocess.Popen([sys.executable, '-X', 'utf8', str(Path(self.gate.__file__)), 'hold', '--root', str(self.root),
                                   '--what', 'crashed click'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.addCleanup(lambda: (holder.kill(), holder.wait(), holder.stdin.close(), holder.stdout.close()))
        self.assertEqual(holder.stdout.readline().strip(), b'held')
        self.assertEqual(self.gate.lock_owner(self.root)['pid'], holder.pid)
        holder.kill()                                                   # the click's process dies inside its critical section
        holder.wait(10)
        services.write_hold(self.root, 'manager start')                 # at once: nothing to wait out
        self.assertTrue((self.batch / 'CONVERT_HOLD').exists())
        (self.batch / 'CONVERT_HOLD').unlink()
        calls = []
        with self.gate.transition_lock(self.root, what='slow click'), \
                mock.patch.object(self.gate, 'transition_lock', lambda root, wait=0, what='': calls.append(what) or self.gate_busy()):
            with self.assertRaisesRegex(services.ServiceError, 'stayed busy.*slow click'):
                services.gate_transition(self.root, lambda: calls.append('action'), 'x')
        self.assertEqual(calls, ['x'] * services.GATE_LOCK_TRIES)       # retried, then refused: never written beside a live holder
        with self.gate.transition_lock(self.root, what='slow click'), mock.patch.object(services, 'GATE_LOCK_TRIES', 2), \
                mock.patch.object(services, 'GATE_LOCK_WAIT', 0.05):
            with self.assertRaises(services.ServiceError):
                services.write_hold(self.root, 'supervisor restart')
            self.assertFalse((self.batch / 'CONVERT_HOLD').exists())    # the real lock: not written while the click is inside
        services.write_hold(self.root, 'supervisor restart')
        self.assertTrue((self.batch / 'CONVERT_HOLD').exists())

    def gate_busy(self):
        class Busy:
            def __enter__(inner):
                raise self.gate.GateBusy('busy')
        return Busy()



@unittest.skipUnless(os.name == 'nt', 'Windows consoles')
class HiddenConsoleTest(unittest.TestCase):
    """User 2026-11-26: a service or task the app starts must OWN a (hidden) console, so the console programs it starts inherit it
    instead of each getting a new console (which Windows Terminal, as the default terminal, shows as an empty tab)."""

    def test_services_and_tasks_get_a_console_their_children_inherit(self):
        import subprocess as sp
        probe = PROBE
        for flags in (services.DETACHED, orchestrator.DETACHED):
            self.assertFalse(flags & 0x00000008)                       # never DETACHED_PROCESS
            out = sp.run([sys.executable, '-c', probe], capture_output=True, text=True, creationflags=flags, timeout=60).stdout.split()
            self.assertGreaterEqual(int(out[0]), 1, out)               # the started process has a console
            self.assertGreaterEqual(int(out[1]), 2, out)               # and its child shares it (no new console of its own)


PROBE = """
import ctypes, subprocess, sys
buf = (ctypes.c_uint * 8)()
mine = ctypes.windll.kernel32.GetConsoleProcessList(buf, 8)
kid = subprocess.run([sys.executable, '-c',
                      'import ctypes; b = (ctypes.c_uint * 8)(); print(ctypes.windll.kernel32.GetConsoleProcessList(b, 8))'],
                     capture_output=True, text=True).stdout.strip()
print(mine, kid)
"""

if __name__ == '__main__':
    unittest.main()
