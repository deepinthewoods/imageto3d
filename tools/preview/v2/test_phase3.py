"""Tests of phase 3 of the v2 preview backend: task queue, run_task.py, orchestrator (attempt tokens, adoption, lanes, dependency, retries,
stall / kill, reconcile, queue repair, stale scan), the 3D / Aligned approvals and what they bind, Completed, amounts, rebuild, recovery,
/api/v2/layers and /api/v2/queue. Each phase 3 review finding (F1-F16, DESIGN_PHASE3.md section F) has at least one test here.

Run: python -m unittest tools/preview/v2/test_phase3.py   (temp roots only; the real root is never touched)

Builds are done by a FAKE builder (a small script that writes the same layout and last-line JSON as tools/layers/build.py and computes
the REAL fingerprint), run through the REAL detached wrapper run_task.py, so the orchestrator is tested end to end without the GPU.
Every wrapper and builder process a test starts is killed in tearDown (by the PIDs the queue and the claim files recorded).
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
import uuid

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import actions  # noqa: E402
import builds  # noqa: E402
import catalog  # noqa: E402
import guard  # noqa: E402
import orchestrator  # noqa: E402
import run_task  # noqa: E402
import server  # noqa: E402
import taskqueue  # noqa: E402
from test_v2 import RACES, V2Case  # noqa: E402

try:
    import psutil
except ImportError:      # pragma: no cover
    psutil = None

FAKE = '''import hashlib, importlib.util, json, os, pathlib, sys, time
args = sys.argv[1:]
ctl = pathlib.Path(args[args.index('--ctl') + 1])
cfg = json.loads(ctl.read_text()) if ctl.exists() else {}
spec = importlib.util.spec_from_file_location('layers_build', args[args.index('--build-py') + 1])
lb = importlib.util.module_from_spec(spec)
sys.modules['layers_build'] = lb
spec.loader.exec_module(lb)
if args[0] == 'bare':
    race = args[1]
    target, out = f'bare:{race}', pathlib.Path(args[args.index('--bare-root') + 1]) / race
    fp, tag, lod = lb.fingerprint_bare(race), 'bare', None
    slots = ['head', 'torso', 'lower_arm.l', 'lower_arm.r', 'lower_leg.l', 'lower_leg.r']
    kind, ident = 'bare', 'bare:' + race
else:
    module = pathlib.Path(args[1]).resolve()
    lod = args[args.index('--lod') + 1]
    race, kind = module.parts[-2], module.name
    target, out = f'{race}/{kind}', module / 'layers'
    fp, tag = lb.fingerprint_module(module, lod), lod
    slots = {'outfit': ['outfit'], 'body_armor': ['torso'], 'right_gauntlet_boot': ['lower_arm.l', 'lower_arm.r', 'lower_leg.l', 'lower_leg.r']}[kind]
    ident = lb._rel(module)
runs_file = ctl.parent / ('runs-' + hashlib.sha1((target + str(args[1])).encode()).hexdigest()[:10])
runs = int(runs_file.read_text()) + 1 if runs_file.exists() else 1
runs_file.write_text(str(runs))
mode = cfg.get(target, cfg.get('default', 'ok'))
print('fake build', target, 'mode', mode, 'run', runs, flush=True)


def fail(error, code=1, **extra):
    print(json.dumps({'status': 'failed', 'error': error} | extra), flush=True)
    sys.exit(code)


if '--expect-source-sha256' in args:
    want = args[args.index('--expect-source-sha256') + 1]
    if hashlib.sha256((module / 'reduced' / f'lod_error_{lod}.glb').read_bytes()).hexdigest() != want:
        fail('source changed: the LOD GLB is not the one the 3D approval pinned (send the set back to 3D)')
if mode.startswith('sleep:'):
    time.sleep(float(mode.split(':')[1]))
elif mode == 'invalid':
    fail('missing input: reduced/lod_error_0p2.glb')
elif mode == 'garment':
    fail('no garment found for lower_leg.r (boot)')
elif mode == 'transient':
    fail('inputs changed during build (retry)', retryable=True)
elif mode == 'fail_once' and runs == 1:
    fail('inputs changed during build (retry)', retryable=True)
elif mode == 'crash':
    sys.exit(3)
elif mode == 'die':
    os._exit(9)
elif mode == 'silent':
    print('done, but no verdict', flush=True)
    sys.exit(0)
elif mode == 'stderr_json':
    print(json.dumps({'status': 'ok', 'build_id': 'x'}), file=sys.stderr, flush=True)
    sys.exit(0)
elif mode == 'lie':
    print(json.dumps({'status': 'ok', 'build_id': f'{tag}-nothingthere'}), flush=True)
    sys.exit(0)
build_id = f'{tag}-{fp[:12]}'
ptr_file = out / ('current_bare.json' if kind == 'bare' else f'current_{lod}.json')
if ptr_file.exists() and json.loads(ptr_file.read_text()).get('build_id') == build_id and (out / build_id / 'build.json').exists():
    print(json.dumps({'status': 'current', 'build_id': build_id}), flush=True)
    sys.exit(0)
(out / build_id).mkdir(parents=True, exist_ok=True)
for slot in slots:
    (out / build_id / (slot + '.glb')).write_bytes(b'glb-' + slot.encode() + build_id.encode())
    (out / build_id / (slot + '.json')).write_text('{}')
record = {'build_id': build_id, 'fingerprint': fp, 'lod': lod, 'status': 'ok', 'kind': kind, 'race': race, 'warnings': [], 'metrics': {'wall_crossings_left': 0},
          'module': ident, 'required': slots, 'slots': {s: {'glb': f'{build_id}/{s}.glb', 'json': f'{build_id}/{s}.json', 'faces': 10} for s in slots}}
(out / build_id / 'build.json').write_text(json.dumps(record))
ptr_file.write_text(json.dumps(record))
print(json.dumps({'status': 'ok', 'build_id': build_id}), flush=True)
'''
BARE_SLOTS = ['head', 'torso', 'lower_arm.l', 'lower_arm.r', 'lower_leg.l', 'lower_leg.r']


class Phase3Case(V2Case):
    def setUp(self):
        super().setUp()
        base = Path(self.temp.name)
        self.fake = base / 'fake_build.py'
        self.fake.write_text(FAKE)
        self.ctl = base / 'ctl.json'
        self.bare_dir = self.root / 'tools' / 'preview' / 'cache' / 'layers'
        self.old_bare = builds.lb().BARE_ROOT
        builds.lb().BARE_ROOT = self.bare_dir
        self.old_backoff = orchestrator.BACKOFF
        orchestrator.BACKOFF = 0.0
        self.idle_value = 5.0           # present
        self.started: list[orchestrator.Orchestrator] = []
        self.extra_servers: list = []

    def tearDown(self):
        for orch in self.started + [getattr(self.server, 'orchestrator', None)]:
            if orch is not None:
                orch.stop(5)
        for extra in self.extra_servers:
            extra.server_close()
        self.kill_all()
        guard._FAULTS.clear()
        builds.lb().BARE_ROOT = self.old_bare
        orchestrator.BACKOFF = self.old_backoff
        super().tearDown()

    def kill_all(self):
        """Every wrapper and builder this test started: the PIDs in the queue and in the claim files (each checked by its create time)."""
        if psutil is None:
            return
        pids = []
        for task in taskqueue.for_root(self.root).tasks(limit=10000):
            pids += [(task.get('pid'), task.get('pid_create')), (task.get('builder_pid'), task.get('builder_create'))]
        logs = self.root / '.codex_jobs' / '_app' / 'logs'
        for claim in logs.glob('*.claim.json') if logs.is_dir() else []:
            try:
                data = json.loads(claim.read_text())
            except ValueError:
                continue
            pids += [(data.get('wrapper_pid'), data.get('wrapper_create')), (data.get('builder_pid'), data.get('builder_create'))]
        for pid, created in pids:
            if not pid or pid == os.getpid():
                continue
            try:
                proc = psutil.Process(pid)
                if created is None or abs(proc.create_time() - created) <= 2:
                    proc.kill()
                    proc.wait(10)                    # (its log handles must be closed before the temp root is removed)
            except psutil.Error:
                pass

    # ------------------------------------------------------------------------------------------------------------ helpers
    def build_bare(self, *races):
        """Publish current bare builds (pointer == build.json + stub GLBs) of races, with the real bare fingerprint."""
        for race in races:
            out = self.bare_dir / race
            fp = builds.fingerprint_bare(race)
            build_id = f'bare-{fp[:12]}'
            (out / build_id).mkdir(parents=True, exist_ok=True)
            for slot in BARE_SLOTS:
                (out / build_id / f'{slot}.glb').write_bytes(b'bare-' + slot.encode())
            record = {'build_id': build_id, 'fingerprint': fp, 'status': 'ok', 'kind': 'bare', 'race': race, 'module': f'bare:{race}', 'lod': None,
                      'required': BARE_SLOTS, 'slots': {s: {'glb': f'{build_id}/{s}.glb', 'faces': 5} for s in BARE_SLOTS}}
            (out / build_id / 'build.json').write_text(json.dumps(record))
            (out / 'current_bare.json').write_text(json.dumps(record))

    def publish_current(self, set_name, job, race, module, lod='0p2'):
        """A published build with the REAL current fingerprint (an Aligned approval refuses stale ones)."""
        return self.publish(set_name, job, race, module, lod=lod, fingerprint=builds.fingerprint_module(self.root / 'models' / set_name / job / race / module, lod))

    def module(self, job, race='human_male', module='outfit', set_name='s'):
        """models/<set>/<job>/<race>/<module> (created), the module dir a fake build runs on."""
        path = self.root / 'models' / set_name / job / race / module
        (path / 'reduced').mkdir(parents=True, exist_ok=True)
        return path

    def fake_argv(self, task):
        args = task.get('args') or {}
        if task['lane'] == 'bare':
            return [sys.executable, str(self.fake), 'bare', args['race'], '--ctl', str(self.ctl), '--build-py', str(builds.BUILD_PY), '--bare-root', str(self.bare_dir)]
        argv = [sys.executable, str(self.fake), 'module', args['module_dir'], '--lod', args['lod'], '--ctl', str(self.ctl), '--build-py', str(builds.BUILD_PY)]
        if args.get('glb_sha256'):
            argv += ['--expect-source-sha256', args['glb_sha256']]
        return argv

    def control(self, **modes):
        self.ctl.write_text(json.dumps(modes))

    def runs(self, target, arg):
        """How many times the fake builder ran for (target, first argument)."""
        f = self.ctl.parent / ('runs-' + hashlib.sha1((target + str(arg)).encode()).hexdigest()[:10])
        return int(f.read_text()) if f.exists() else 0

    def orch(self, raw=False):
        """An orchestrator on the fixture root (not started). `raw`: no approval checks and no reconcile (tests of the bare queue mechanics)."""
        orch = orchestrator.Orchestrator(self.root, argv_for=self.fake_argv, idle=lambda: self.idle_value, scan_stale=False,
                                         require_approval=not raw, reconcile_queue=not raw)
        orch.logs.mkdir(parents=True, exist_ok=True)
        return orch

    def drive(self, orch, done, timeout=90.0):
        """tick() the orchestrator until `done()`; fails after `timeout` seconds."""
        end = time.time() + timeout
        while time.time() < end:
            orch.tick()
            if done():
                return
            time.sleep(0.15)
        self.fail('timeout: ' + json.dumps([(t['id'], t['key'], t['state'], t['error']) for t in taskqueue.for_root(self.root).tasks()], indent=0))

    def settled(self):
        return not taskqueue.for_root(self.root).open_tasks()

    def settle(self, task_id, ok=True, error=None):
        """Run a queued task through claim -> running -> finish by hand (no process)."""
        q = taskqueue.for_root(self.root)
        token = uuid.uuid4().hex
        self.assertTrue(q.claim(task_id, token, None))
        self.assertTrue(q.mark_running(task_id, token, None, None))
        self.assertTrue(q.finish(task_id, token, ok, {}, error))
        return token

    def item(self, name='s'):
        return self.get(f'/api/v2/item?id=set:{name}')[1]

    def sets_ready(self, name='s', modules=('outfit',)):
        self.full_set(name, 'j1', modules)

    def shown(self, name='s'):
        """(units, lods) the 3D confirmation shows: every live unit's job + LOD, and the never-saved cells."""
        item = self.item(name)
        live = [u for u in item['units'] if u['status'] == 'reduced']
        return ({f'{u["race"]}/{u["module"]}': {'job': u['job'], 'lod': u['lod']} for u in live},
                {f'{u["race"]}/{u["module"]}': u['lod'] for u in live if not u['lod_saved'] and u['status'] == 'reduced'})

    def approve(self, name='s', rev=0, units=None, lods=None, **extra):
        shown_units, shown_lods = self.shown(name)
        args = {'units': shown_units if units is None else units, 'lods': shown_lods if lods is None else lods} | extra
        return self.act(f'set:{name}', 'approve', rev=rev, args=args)

    def aligned_args(self, name='s', amount=None, **override):
        """What the Aligned client sends: every unit's published build id, every race's bare build id, the full amount map."""
        item = self.item(name)
        groups = ('body', 'limbs') if item['kind'] == 'armour' else ('outfit',)
        full = {r: {g: (item['decisions']['amount'].get(r) or {}).get(g, 100) for g in groups} for r in item['races']}
        for race, values in (amount or {}).items():
            full.setdefault(race, {}).update(values)
        args = {'builds': {k: v['build_id'] for k, v in item['builds'].items()}, 'bare': {r: v['build_id'] for r, v in item['bare_builds'].items()},
                'amount': full}
        return args | override, item['rev']

    def approve_aligned(self, name='s', amount=None, rev=None, **override):
        args, current = self.aligned_args(name, amount, **override)
        return self.act(f'set:{name}', 'approve', rev=current if rev is None else rev, args=args)

    def approve_and_build(self, name='s', modules=('outfit',)):
        self.sets_ready(name, modules)
        status, result = self.approve(name, 0)
        self.assertEqual(status, 200, result)
        orch = self.orch()
        self.started.append(orch)
        self.idle_value = 1000.0              # away: four at a time
        self.drive(orch, self.settled)
        return orch, result

    def completed_set(self, name='s'):
        orch, _ = self.approve_and_build(name)
        status, body = self.approve_aligned(name)
        self.assertEqual((status, body.get('stage')), (200, 'completed'), body)
        return orch, body


# ======================================================================================================================== queue

class QueueTests(Phase3Case):
    def test_enqueue_is_idempotent_per_key(self):
        q = taskqueue.for_root(self.root)
        a = q.enqueue('layers', 'k1', 's', 'human_male/outfit', {'x': 1})
        self.assertEqual(q.enqueue('layers', 'k1', 's', 'human_male/outfit', {'x': 2}), a)
        self.assertEqual(q.get(a)['args'], {'x': 1})                      # unchanged
        self.assertEqual(len(q.tasks()), 1)
        self.assertTrue(q.claim(a, 'tok-a', 'log.log'))
        self.assertTrue(q.mark_running(a, 'tok-a', 123, 1.0))
        self.assertEqual(q.enqueue('layers', 'k1', 's', 'u', {}), a)
        self.assertEqual(q.get(a)['state'], 'running')                    # a running task is returned unchanged
        q.finish(a, 'tok-a', True, {'ok': 1})
        self.assertEqual(q.enqueue('layers', 'k1', 's', 'u', {}), a)
        self.assertEqual(q.get(a)['state'], 'done')                       # a done one too
        b = q.enqueue('layers', 'k2', 's', 'u', {})
        self.settle(b, ok=False, error='boom')
        self.assertEqual(q.enqueue('layers', 'k2', 's', 'u', {}), b)
        self.assertEqual(q.get(b)['state'], 'failed')                     # failed: unchanged ...
        self.assertEqual(q.enqueue('layers', 'k2', 's', 'u', {}, retry=True), b)
        self.assertEqual((q.get(b)['state'], q.get(b)['attempts'], q.get(b)['error']), ('queued', 0, None))      # ... unless retry=True
        c = q.enqueue('bare', 'k3', None, 'human_male', {})
        self.assertTrue(q.cancel(c))
        self.assertFalse(q.cancel(c))
        self.assertEqual(q.enqueue('bare', 'k3', None, 'human_male', {}), c)
        self.assertEqual(q.get(c)['state'], 'queued')                     # a cancelled task comes back with its approval
        with self.assertRaises(ValueError):
            q.enqueue('weird', 'k4', None, None, {})

    def test_api_cancel_retry_next_queued_events(self):
        q = taskqueue.for_root(self.root)
        low = q.enqueue('layers', 'low', 's', 'a', {}, priority=9)
        high = q.enqueue('layers', 'high', 's', 'b', {}, priority=1)
        bare = q.enqueue('bare', 'bare1', None, 'human_male', {}, priority=3)
        self.assertEqual([t['id'] for t in q.next_queued(('bare', 'layers'), 10)], [high, bare, low])
        self.assertEqual([t['id'] for t in q.next_queued('bare', 10)], [bare])
        self.assertTrue(q.claim(high, 'th', None))
        self.assertTrue(q.requeue(high, 'th', 'later', delay=100))
        self.assertEqual([t['id'] for t in q.next_queued(('bare', 'layers'), 10)], [bare, low])        # backoff hides it
        self.assertEqual(q.cancel_set('s'), 2)
        self.assertEqual(q.get(bare)['state'], 'queued')                  # bare tasks belong to no set
        self.assertFalse(q.retry(bare))                                   # only failed / cancelled
        self.assertTrue(q.retry(low))
        self.assertEqual({e['kind'] for e in q.events()}, {'enqueue', 'claim', 'retry', 'cancel'})
        self.assertEqual(len(q.events(after_id=q.events()[-1]['id'])), 0)
        self.assertEqual([t['key'] for t in q.tasks(state='queued', set_name='s')], ['low'])

    def test_attempts_are_claimed_only_from_queued_and_finish_once(self):
        """F6: a cancelled row is never started or restarted; a stale token cannot finish or requeue an attempt; a build finishes once."""
        q = taskqueue.for_root(self.root)
        a = q.enqueue('layers', 'ka', 's', 'u', {})
        self.assertTrue(q.cancel(a))
        self.assertFalse(q.claim(a, 't1', None))                          # cancelled: never claimed
        self.assertFalse(q.mark_running(a, 't1', 1, 1.0))                 # (the old mark_running turned cancelled into running)
        self.assertEqual(q.get(a)['state'], 'cancelled')
        b = q.enqueue('layers', 'kb', 's', 'u', {})
        self.assertTrue(q.claim(b, 'good', None))
        self.assertFalse(q.claim(b, 'second', None))                      # a second dispatcher loses
        self.assertFalse(q.finish(b, 'stale-token', True))                # a late receipt of another attempt
        self.assertFalse(q.requeue(b, 'stale-token', 'x'))
        self.assertTrue(q.mark_running(b, 'good', 5, 1.0))
        self.assertTrue(q.finish(b, 'good', True, {}))
        self.assertFalse(q.finish(b, 'good', False, None, 'again'))       # finishes once
        self.assertEqual((q.get(b)['state'], q.get(b)['attempts']), ('done', 1))

    def test_read_only_reads_never_create_the_database(self):
        other = taskqueue.TaskQueue(self.root / 'nowhere')
        self.assertEqual((other.tasks(), other.by_key('x'), other.next_queued('layers')), ([], None, []))
        self.assertFalse(other.path.exists())

    def test_wal_and_threads(self):
        q = taskqueue.for_root(self.root)
        ids = []

        def work(n):
            for i in range(10):
                ids.append(q.enqueue('layers', f'k{n}-{i}', 's', 'u', {}))
        threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual((len(set(ids)), len(q.tasks(limit=100))), (40, 40))
        with contextlib.closing(q._connect()) as conn:
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')

    def test_totals_are_sql_counts_and_the_page_is_bounded(self):
        """F16: worker and state counts come from SQL, not from the page; the page bounds are exposed."""
        q = taskqueue.for_root(self.root)
        old = q.enqueue('layers', 'old-running', 's', 'u', {})
        self.assertTrue(q.claim(old, 'tok', None))
        self.assertTrue(q.mark_running(old, 'tok', None, None))
        for i in range(30):
            q.enqueue('layers', f'newer-{i}', 's', 'u', {})
        with mock.patch.object(orchestrator, 'OPEN_PAGE', 10):
            data = orchestrator.queue_state(self.root, False)
        self.assertNotIn(old, [t['id'] for t in data['tasks']])              # the old running row is beyond the page ...
        self.assertEqual(data['workers']['running'], 1)                       # ... but still counted
        self.assertEqual((data['totals']['queued'], data['totals']['running']), (30, 1))
        self.assertEqual((data['page']['open_total'], data['page']['open_shown'], data['page']['open_limit'], data['page']['truncated']), (31, 10, 10, True))

    def test_a_damaged_database_never_breaks_the_site_and_is_rebuilt(self):
        """F9: corrupt queue -> state / item / queue still answer with queue_health; the orchestrator keeps the file, re-adopts the surviving
        wrapper from its claim (nothing starts twice) and re-queues the rest from the approval."""
        self.sets_ready()
        self.assertEqual(self.approve('s', 0)[0], 200)
        q = taskqueue.for_root(self.root)
        self.control(default='ok', **{'human_male/outfit': 'sleep:4'})
        orch = self.orch()
        self.started.append(orch)
        self.idle_value = 5.0                                               # present: one at a time
        bare_first = [t for t in q.tasks() if t['lane'] == 'bare']
        for task in bare_first:                                             # (no bare dependency in the way)
            q.cancel(task['id'])
        self.build_bare(*RACES)
        orch.tick()
        running = q.tasks(state=('starting', 'running'))
        self.assertEqual(len(running), 1, q.tasks())
        live = running[0]
        for _ in range(100):                                                # the wrapper has claimed its token
            if orch._claim(live):
                break
            time.sleep(0.05)
        # the database is destroyed
        for suffix in ('-wal', '-shm'):
            (q.path.with_name(q.path.name + suffix)).unlink(missing_ok=True)
        q.path.write_bytes(b'this is not a database' * 50)
        q._ready = False
        status, state = self.get('/api/v2/state')
        self.assertEqual(status, 200)
        self.assertFalse(state['queue_health']['ok'])
        self.assertEqual(self.get('/api/v2/queue')[0], 200)
        self.assertEqual(self.get('/api/v2/item?id=set:s')[0], 200)          # the catalog keeps working
        out = orch.reconcile()
        self.assertEqual(out['repaired']['adopted'], 1)
        self.assertEqual(len(list(q.path.parent.glob('queue.sqlite3.corrupt-*'))), 1)
        rows = {t['key']: t for t in q.tasks()}
        self.assertEqual(rows[live['key']]['state'], 'running')              # re-adopted with its token
        self.assertEqual(rows[live['key']]['token'], live['token'])
        self.assertTrue(any(t['unit'] == 'dwarf_male/outfit' and t['state'] == 'queued' for t in rows.values()))
        self.drive(orch, self.settled)
        self.assertEqual(self.runs('human_male/outfit', live['args']['module_dir']), 1)        # never started twice
        self.assertEqual(self.item()['stage'], 'aligned')
        self.assertEqual(self.get('/api/v2/state')[1]['queue_health'], {'ok': True, 'repaired': str(next(q.path.parent.glob('queue.sqlite3.corrupt-*')))})


class RunTaskTests(Phase3Case):
    def test_result_is_written_atomically_also_when_the_command_cannot_start(self):
        base = Path(self.temp.name) / 'rt'
        for name, argv, expect_exit in (('ok', [sys.executable, '-c', 'import json;print("progress");print(json.dumps({"status":"ok","n":1}));print("tail")'], 0),
                                        ('fail', [sys.executable, '-c', 'import sys;print("{bad json");sys.exit(4)'], 4),
                                        ('stderr', [sys.executable, '-c', 'import json,sys;print(json.dumps({"status":"ok"}),file=sys.stderr)'], 0),
                                        ('missing', [str(base / 'no-such-program.exe')], -1)):
            spec = {'id': 1, 'attempt': 2, 'token': f'tok-{name}', 'argv': argv, 'env': {'FOO': 'bar'}, 'log': str(base / f'{name}.log'),
                    'err': str(base / f'{name}.err.log'), 'result': str(base / f'{name}.result.json'), 'claim': str(base / f'{name}.claim.json'),
                    'task': {'key': 'k'}}
            (base).mkdir(exist_ok=True)
            (base / f'{name}.task.json').write_text(json.dumps(spec))
            run = subprocess.run([sys.executable, str(HERE / 'run_task.py'), str(base / f'{name}.task.json'), '--token', f'tok-{name}'],
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = json.loads((base / f'{name}.result.json').read_text())
            self.assertEqual((result['exit'], result['token'], result['attempt']), (expect_exit, f'tok-{name}', 2))
            self.assertIsInstance(result['finished'], float)
            claim = json.loads((base / f'{name}.claim.json').read_text())
            self.assertEqual((claim['token'], claim['task'], claim['attempt']), (f'tok-{name}', {'key': 'k'}, 2))
            self.assertIsInstance(claim['wrapper_pid'], int)
            if name == 'ok':
                self.assertEqual(result['last_json'], {'status': 'ok', 'n': 1})
                self.assertIn('progress', (base / 'ok.log').read_text())
                self.assertIsInstance(claim['builder_pid'], int)
            if name == 'fail':
                self.assertIsNone(result['last_json'])
            if name == 'stderr':                                             # stderr is kept in its own log and never read as a verdict
                self.assertIsNone(result['last_json'])
                self.assertIn('"status": "ok"', (base / 'stderr.err.log').read_text())
            if name == 'missing':
                self.assertIn('start_error', result)
        self.assertEqual([p.name for p in base.glob('*.tmp')], [])
        # an unreadable task file: nothing to write a result to, a non-zero exit (the orchestrator then sees a lost task)
        (base / 'bad.task.json').write_text('{nope')
        bad = subprocess.run([sys.executable, str(HERE / 'run_task.py'), str(base / 'bad.task.json')], capture_output=True)
        self.assertNotEqual(bad.returncode, 0)

    def test_env_reaches_the_command(self):
        base = Path(self.temp.name)
        spec = {'id': 2, 'argv': [sys.executable, '-c', 'import os,json;print(json.dumps({"v":os.environ["BODY_FIT_GPU"],"u":os.environ["PYTHONUTF8"]}))'],
                'env': orchestrator.TASK_ENV, 'log': str(base / 'e.log'), 'result': str(base / 'e.result.json')}
        result = run_task.run(spec)
        self.assertEqual(result['last_json'], {'v': '1', 'u': '1'})


# ================================================================================================================ orchestrator

class PoolTests(Phase3Case):
    def test_classify_failures(self):
        c = orchestrator.classify
        self.assertEqual(c({'exit': 0, 'last_json': {'status': 'ok'}})[0], True)
        self.assertEqual(c({'exit': 0, 'last_json': {'status': 'current'}})[0], True)
        self.assertEqual(c({'exit': 0, 'last_json': None}), (False, orchestrator.NO_VERDICT, False))      # F10: exit 0 without a verdict fails
        self.assertEqual(c({'exit': 1, 'last_json': {'status': 'ok'}})[0], False)
        self.assertEqual(c({'exit': 1, 'last_json': {'status': 'failed', 'error': 'missing input: x'}}), (False, 'missing input: x', False))
        self.assertEqual(c({'exit': 1, 'last_json': {'status': 'failed', 'error': 'no garment found for lower_leg.r (boot)'}})[2], False)
        self.assertEqual(c({'exit': 1, 'last_json': {'status': 'failed', 'error': 'inputs changed during build (retry)', 'retryable': True}})[2], True)
        self.assertEqual(c({'exit': 1, 'last_json': {'status': 'failed', 'error': 'required slot lower_arm.l missing'}})[2], False)
        self.assertEqual(c({'exit': 1, 'last_json': {'status': 'failed', 'error': 'source changed: x'}})[2], False)
        self.assertEqual(c({'exit': 3, 'last_json': None})[2], True)                      # a crash without a verdict is transient
        self.assertEqual(c({'exit': -1, 'last_json': None, 'start_error': 'FileNotFoundError: x'})[2], True)
        self.assertEqual(c({'exit': 1, 'last_json': {'status': 'failed', 'error': 'ValueError: bad geometry'}})[2], False)    # deterministic code error
        self.assertEqual(c({'exit': 1, 'last_json': {'status': 'failed', 'error': 'PermissionError: [Errno 13] x'}})[2], True)

    def test_presence_and_pool_limits(self):
        settings = self.root / '.codex_jobs' / '_app' / 'settings.json'
        self.assertEqual(orchestrator.pool_status(self.root, 5.0, None)['limit'], 1)          # present: default 1
        self.assertEqual(orchestrator.pool_status(self.root, 299.0, None)['limit'], 1)
        self.assertEqual(orchestrator.pool_status(self.root, 300.0, None)['limit'], 4)        # away: default 4
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({'layer_workers_present': 2, 'layer_workers_away': 6, 'build_stall_minutes': 7}))
        self.assertEqual((orchestrator.pool_status(self.root, 1, None)['limit'], orchestrator.pool_status(self.root, 999, None)['limit']), (2, 6))
        self.assertEqual((orchestrator.read_settings(self.root)['build_stall_minutes'], builds.stall_minutes(self.root)), (7, 7))
        settings.write_text(json.dumps({'layer_workers_present': -3, 'layer_workers_away': 'x', 'build_stall_minutes': 0}))
        self.assertEqual((orchestrator.pool_status(self.root, 1, None)['limit'], orchestrator.pool_status(self.root, 999, None)['limit']), (1, 4))
        self.assertEqual(builds.stall_minutes(self.root), 30)
        mode = self.root / '.codex_jobs' / '_batch' / 'supervisor_mode.json'
        mode.parent.mkdir(parents=True, exist_ok=True)
        mode.write_text(json.dumps({'mode': 'pause'}))
        self.assertEqual((orchestrator.pool_status(self.root, 1, None)['limit'], orchestrator.pool_status(self.root, 999, None)['limit']), (0, 0))
        mode.write_text(json.dumps({'mode': 'pause-conversions'}))            # only a full pause stops the layers
        self.assertEqual(orchestrator.pool_status(self.root, 999, None)['limit'], 4)

    def test_presence_file_when_fresh_else_last_input(self):
        presence = Path(self.temp.name) / 'presence.json'
        old = orchestrator.PRESENCE_FILE
        orchestrator.PRESENCE_FILE = presence
        try:
            now = time.time()
            presence.write_text(json.dumps({'updated_unix': now - 5, 'user_idle_seconds': 4000}))
            self.assertEqual(orchestrator.idle_seconds(now), 4000.0)
            presence.write_text(json.dumps({'updated_unix': now - 31, 'user_idle_seconds': 4000}))        # stale: the file is not trusted
            with unittest_mock_win(None):
                self.assertIsNone(orchestrator.idle_seconds(now))
            with unittest_mock_win(12.0):
                self.assertEqual(orchestrator.idle_seconds(now), 12.0)
            presence.write_text('{broken')
            with unittest_mock_win(7.0):
                self.assertEqual(orchestrator.idle_seconds(now), 7.0)
            with unittest_mock_win(None):
                self.assertTrue(orchestrator.pool_status(self.root, None, None)['present'])             # unknown = present
        finally:
            orchestrator.PRESENCE_FILE = old

    def test_lane_limits_present_away_and_pause(self):
        q = taskqueue.for_root(self.root)
        self.control(default='sleep:4')
        for i in range(6):
            q.enqueue('layers', f'k{i}', 's', f'human_male/outfit{i}', {'race': 'human_male', 'module_dir': str(self.module(f'j{i}')), 'lod': '0p2'})
        orch = self.orch(raw=True)
        mode = self.root / '.codex_jobs' / '_batch' / 'supervisor_mode.json'
        mode.parent.mkdir(parents=True, exist_ok=True)
        mode.write_text(json.dumps({'mode': 'pause'}))
        self.idle_value = 5000.0
        self.assertEqual(orch.tick()['started'], 0)                                       # paused: nothing starts even when away
        mode.write_text(json.dumps({'mode': 'auto'}))
        self.idle_value = 5.0
        self.assertEqual((orch.tick()['started'], len(q.tasks(state='running'))), (1, 1))      # present: one at a time
        self.assertEqual(orch.tick()['started'], 0)
        self.idle_value = 1000.0
        self.assertEqual(orch.tick()['started'], 3)                                       # away: four at once (one already runs)
        self.assertEqual(len(q.tasks(state='running')), 4)
        self.assertEqual(len(q.tasks(state='queued')), 2)
        mode.write_text(json.dumps({'mode': 'pause'}))
        self.assertEqual(orch.tick()['started'], 0)
        self.assertEqual(len(q.tasks(state='running')), 4)                                # a pause never stops a running build
        # the pool is shared by both lanes: bare tasks count against the same limit
        self.assertEqual(orchestrator.queue_state(self.root, True)['workers']['limit'], 0)

    def test_bare_goes_first_then_the_layers_of_its_race(self):
        q = taskqueue.for_root(self.root)
        self.control(default='ok')
        self.idle_value = 1000.0
        mod = self.root / 'models' / 's' / 'j1' / 'human_male' / 'outfit'
        (mod / 'reduced').mkdir(parents=True)
        (mod / 'reduced' / 'lod_error_0p2.glb').write_bytes(b'x')
        layers = q.enqueue('layers', 'L1', 's', 'human_male/outfit', {'race': 'human_male', 'module_dir': str(mod), 'lod': '0p2'}, priority=1)
        other = q.enqueue('layers', 'L2', 's', 'dwarf_male/outfit', {'race': 'dwarf_male', 'module_dir': str(mod.parents[1] / 'dwarf_male' / 'outfit'), 'lod': '0p2'})
        (mod.parents[1] / 'dwarf_male' / 'outfit').mkdir(parents=True)
        bare = q.enqueue('bare', 'B1', None, 'human_male', {'race': 'human_male'}, priority=3)
        orch = self.orch(raw=True)
        self.started.append(orch)
        orch.tick()
        running = {t['id'] for t in q.tasks(state='running')}
        self.assertEqual(running, {bare, other})              # the layers task of human_male waits for its bare task; dwarf_male is not blocked
        self.assertEqual(q.get(layers)['state'], 'queued')
        self.drive(orch, lambda: q.get(bare)['state'] == 'done')
        self.drive(orch, lambda: q.get(layers)['state'] in ('done', 'failed'))
        self.assertGreaterEqual(q.get(layers)['started'], q.get(bare)['started'])        # (it may start as soon as the bare build is published)
        # a current bare build releases the layers even while another bare task of the race is queued
        fresh = q.enqueue('bare', 'B2', None, 'human_male', {'race': 'human_male'})
        again = q.enqueue('layers', 'L3', 's', 'human_male/outfit', {'race': 'human_male', 'module_dir': str(mod), 'lod': '0p2'})
        self.control(default='sleep:3')
        self.idle_value = 5.0                                 # present: one at a time; bare B2 queued first in priority order
        orch.tick()
        self.assertEqual(q.get(fresh)['state'], 'running')
        self.idle_value = 1000.0
        orch.tick()
        self.assertEqual(q.get(again)['state'], 'running')    # bare:human_male is current, so L3 need not wait

    def test_retry_rules(self):
        q = taskqueue.for_root(self.root)
        self.idle_value = 1000.0
        mod = self.root / 'models' / 's' / 'j1' / 'human_male' / 'outfit'
        (mod / 'reduced').mkdir(parents=True)
        args = lambda name: {'race': 'human_male', 'module_dir': str(mod.parents[1] / name / 'outfit'), 'lod': '0p2'}       # noqa: E731
        modes = ('invalid', 'garment', 'transient', 'fail_once', 'crash', 'die', 'silent', 'stderr_json', 'lie')
        ids = {mode: q.enqueue('layers', f'k-{mode}', 's', f'{mode}/outfit', args(mode)) for mode in modes}
        self.control(**{f'{m}/outfit': m for m in ids})
        # the fake script reads the unit from the module dir: <set>/<job>/<race>/<module> -> target "<race>/outfit"; give it a per-mode race
        for mode in ids:
            (mod.parents[1] / mode / 'outfit').mkdir(parents=True, exist_ok=True)
        orch = self.orch(raw=True)
        self.started.append(orch)
        self.drive(orch, self.settled, 150)
        final = {mode: q.get(i) for mode, i in ids.items()}
        self.assertEqual((final['invalid']['state'], final['invalid']['attempts']), ('failed', 1))      # invalid input: no retry
        self.assertIn('missing input', final['invalid']['error'])
        self.assertEqual((final['garment']['state'], final['garment']['attempts']), ('failed', 1))      # missing required slot: no retry
        self.assertEqual((final['transient']['state'], final['transient']['attempts']), ('failed', 3))   # 2 automatic retries
        self.assertEqual((final['fail_once']['state'], final['fail_once']['attempts']), ('done', 2))
        self.assertEqual((final['crash']['state'], final['crash']['attempts']), ('failed', 3))          # no verdict: transient
        self.assertEqual((final['die']['state'], final['die']['attempts']), ('failed', 3))              # the builder died with no output at all
        # F10: exit 0 without a verdict on STDOUT is a failure (stderr is never read as a verdict); a verdict without its publication too
        self.assertEqual((final['silent']['state'], final['silent']['attempts'], final['silent']['error']), ('failed', 1, orchestrator.NO_VERDICT))
        self.assertEqual((final['stderr_json']['state'], final['stderr_json']['error']), ('failed', orchestrator.NO_VERDICT))
        self.assertEqual(final['lie']['state'], 'failed')
        self.assertIn('not the valid publication', final['lie']['error'])
        self.assertTrue(any(e['kind'] == 'retry' for e in q.events()))

    def test_backoff_delays_the_next_attempt(self):
        orchestrator.BACKOFF = 60.0
        q = taskqueue.for_root(self.root)
        self.idle_value = 1000.0
        mod = self.root / 'models' / 's' / 'j1' / 'human_male' / 'outfit'
        (mod / 'reduced').mkdir(parents=True)
        task = q.enqueue('layers', 'kb', 's', 'human_male/outfit', {'race': 'human_male', 'module_dir': str(mod), 'lod': '0p2'})
        self.control(default='transient')
        orch = self.orch(raw=True)
        self.started.append(orch)
        self.drive(orch, lambda: q.get(task)['attempts'] == 1 and q.get(task)['state'] == 'queued')
        row = q.get(task)
        self.assertGreater(row['not_before'], time.time() + 50)
        self.assertEqual(orch.tick()['started'], 0)           # waiting for the backoff
        self.assertIn('inputs changed', row['error'])

    def test_attempt_paths_are_per_attempt_and_late_receipts_are_ignored(self):
        """F5: every attempt has its own log / result / claim; a result carrying another token never finishes the current attempt."""
        q = taskqueue.for_root(self.root)
        self.idle_value = 1000.0
        mod = self.module('j1')
        task = q.enqueue('layers', 'kp', 's', 'human_male/outfit', {'race': 'human_male', 'module_dir': str(mod), 'lod': '0p2'})
        self.control(default='fail_once')
        orch = self.orch(raw=True)
        self.started.append(orch)
        self.drive(orch, lambda: q.get(task)['state'] == 'done')
        logs = orch.logs
        self.assertTrue((logs / f'{task}-a1.result.json').is_file() and (logs / f'{task}-a2.result.json').is_file())
        self.assertTrue((logs / f'{task}-a1.claim.json').is_file() and (logs / f'{task}-a2.claim.json').is_file())
        first = json.loads((logs / f'{task}-a1.result.json').read_text())
        second = json.loads((logs / f'{task}-a2.result.json').read_text())
        self.assertNotEqual(first['token'], second['token'])
        # a running attempt with a stray result file of another attempt: ignored
        other = q.enqueue('layers', 'kq', 's', 'human_male/outfit', {'race': 'human_male', 'module_dir': str(mod), 'lod': '0p2'})
        self.control(default='sleep:3')
        orch.tick()
        row = q.get(other)
        self.assertEqual(row['state'], 'running')
        Path(orchestrator.result_path(row)).write_text(json.dumps({'token': 'someone-else', 'exit': 0, 'last_json': {'status': 'ok', 'build_id': 'x'}}))
        orch.tick()
        self.assertEqual(q.get(other)['state'], 'running')
        self.drive(orch, lambda: q.get(other)['state'] in ('done', 'failed'))
        self.assertEqual(q.get(other)['state'], 'done')

    def test_a_crash_between_commit_and_spawn_or_after_spawn_never_starts_a_build_twice(self):
        """F5: the attempt is committed before the spawn; after a crash the claim (or the token on a command line) decides."""
        q = taskqueue.for_root(self.root)
        self.idle_value = 1000.0
        mod = self.module('j1')
        self.control(default='sleep:2')

        def crash(_task):
            raise orchestrator.SimulatedCrash()
        # 1. crash AFTER the wrapper started, BEFORE its pid was recorded: the restart adopts it through its claim
        after = q.enqueue('layers', 'after', 's', 'human_male/outfit', {'race': 'human_male', 'module_dir': str(mod), 'lod': '0p2'})
        first = self.orch(raw=True)
        first.faults['spawned'] = crash
        with self.assertRaises(orchestrator.SimulatedCrash):
            first.tick()
        row = q.get(after)
        self.assertEqual((row['state'], row['pid'], row['attempts']), ('starting', None, 1))
        for _ in range(100):
            if first._claim(row):
                break
            time.sleep(0.05)
        second = self.orch(raw=True)
        self.started.append(second)
        self.assertEqual(second.adopt(), {'adopted': 1, 'finished': 0, 'lost': 0})
        self.assertEqual(q.get(after)['state'], 'running')
        self.drive(second, lambda: q.get(after)['state'] in ('done', 'failed'))
        self.assertEqual((q.get(after)['state'], q.get(after)['attempts']), ('done', 1))
        self.assertEqual(self.runs('human_male/outfit', str(mod)), 1)                  # started once
        # 2. a crash right after the commit (nothing spawned): no claim, no process with the token -> lost, retried once
        other_mod = self.module('j2', race='dwarf_male')
        before = q.enqueue('layers', 'before', 's', 'dwarf_male/outfit', {'race': 'dwarf_male', 'module_dir': str(other_mod), 'lod': '0p2'})
        third = self.orch(raw=True)
        third.faults['claimed'] = crash
        with self.assertRaises(orchestrator.SimulatedCrash):
            third.tick()
        self.assertEqual(q.get(before)['state'], 'starting')
        self.assertEqual(self.orch(raw=True).adopt(), {'adopted': 0, 'finished': 0, 'lost': 1})
        self.assertEqual((q.get(before)['state'], q.get(before)['attempts']), ('queued', 1))
        self.drive(second, lambda: q.get(before)['state'] in ('done', 'failed'))
        self.assertEqual((q.get(before)['state'], q.get(before)['attempts']), ('done', 2))
        self.assertEqual(self.runs('dwarf_male/outfit', str(other_mod)), 1)

    def test_adoption_checks_the_process_identity(self):
        q = taskqueue.for_root(self.root)
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: (child.kill(), child.wait()))
        logs = self.root / '.codex_jobs' / '_app' / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        task = q.enqueue('layers', 'adopt', 's', 'human_male/outfit', {'race': 'human_male'})
        q.claim(task, 'tok-1', str(logs / f'{task}-a1.log'))
        q.mark_running(task, 'tok-1', child.pid, orchestrator.create_time(child.pid))
        fresh = self.orch(raw=True)
        self.assertEqual(fresh.adopt(), {'adopted': 1, 'finished': 0, 'lost': 0})
        self.assertEqual(q.get(task)['state'], 'running')
        # a result file of this attempt finishes it (the verdict must name the valid publication: a raw task with no module has none -> retried)
        (logs / f'{task}-a1.result.json').write_text(json.dumps({'token': 'tok-1', 'exit': 1, 'last_json': {'status': 'failed', 'error': 'missing input: x'}}))
        fresh.tick()
        self.assertEqual(q.get(task)['state'], 'failed')
        if psutil is not None:                  # a pid that is alive but is ANOTHER process (create time differs) is not adopted
            other = q.enqueue('layers', 'reused', 's', 'human_male/body_armor', {'race': 'human_male'})
            q.claim(other, 'tok-2', str(logs / f'{other}-a1.log'))
            q.mark_running(other, 'tok-2', child.pid, orchestrator.create_time(child.pid) - 1000)
            self.assertEqual(self.orch(raw=True).adopt(), {'adopted': 0, 'finished': 0, 'lost': 1})
            self.assertEqual((q.get(other)['state'], 'lost' in q.get(other)['error']), ('queued', True))      # lost -> automatic retry
        lost = q.enqueue('layers', 'dead', 's', 'dwarf_male/outfit', {'race': 'dwarf_male'}, max_attempts=1)
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        q.claim(lost, 'tok-3', str(logs / f'{lost}-a1.log'))
        q.mark_running(lost, 'tok-3', dead.pid, None)
        self.assertEqual(self.orch(raw=True).adopt()['lost'], 1)
        self.assertEqual((q.get(lost)['state'], q.get(lost)['error'].startswith('lost')), ('failed', True))

    def test_a_server_restart_keeps_a_running_build(self):
        """The wrapper is detached: dropping the orchestrator (server stop) leaves the build running; a new one finishes it."""
        q = taskqueue.for_root(self.root)
        self.idle_value = 1000.0
        mod = self.module('j1')
        self.control(default='sleep:3')
        task = q.enqueue('layers', 'keep', 's', 'human_male/outfit', {'race': 'human_male', 'module_dir': str(mod), 'lod': '0p2'})
        first = self.orch(raw=True)
        first.tick()
        pid = q.get(task)['pid']
        self.assertTrue(orchestrator.pid_alive(pid))
        second = self.orch(raw=True)
        self.started.append(second)
        self.assertEqual(second.adopt()['adopted'], 1)
        self.drive(second, lambda: q.get(task)['state'] in ('done', 'failed'))
        self.assertEqual((q.get(task)['state'], q.get(task)['attempts']), ('done', 1))          # never started twice

    @unittest.skipIf(psutil is None, 'needs psutil')
    def test_a_stalled_build_is_shown_and_kill_retry_stops_builder_and_wrapper(self):
        """F10: no log growth for build_stall_minutes -> `stalled`; Kill & retry kills the builder, then the wrapper, and queues a fresh attempt."""
        settings = self.root / '.codex_jobs' / '_app' / 'settings.json'
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({'build_stall_minutes': 1}))
        self.sets_ready()
        self.assertEqual(self.approve('s', 0)[0], 200)
        self.build_bare(*RACES)
        q = taskqueue.for_root(self.root)
        for task in q.tasks(lane='bare'):
            q.cancel(task['id'])
        self.control(default='sleep:120')
        orch = self.orch()
        self.started.append(orch)
        self.idle_value = 5.0
        orch.tick()
        row = q.tasks(state='running')[0]
        for _ in range(200):
            claim = orch._claim(row)
            if claim and claim.get('builder_pid'):
                break
            time.sleep(0.05)
        orch.tick()
        row = q.get(row['id'])
        self.assertEqual(row['builder_pid'], claim['builder_pid'])
        self.assertEqual(self.post('/api/v2/queue', {'op': 'kill_retry', 'id': row['id']})[0], 400)          # not stalled yet
        for _ in range(200):                                                # the builder has printed its first line (then stays silent)
            if 'fake build' in Path(row['log']).read_text(errors='replace'):
                break
            time.sleep(0.05)
        time.sleep(0.3)
        old = time.time() - 300
        for path in (Path(row['log']), Path(row['log'][:-4] + '.err.log')):
            os.utime(path, (old, old))
        if True:
            row_view = next(t for t in self.get('/api/v2/queue')[1]['tasks'] if t['id'] == row['id'])
        self.assertEqual((row_view['state'], row_view['stalled']), ('stalled', True))
        unit_task = self.item()['builds'][row['unit']]['task']
        self.assertEqual(unit_task['state'], 'stalled')
        status, body = self.post('/api/v2/queue', {'op': 'kill_retry', 'id': row['id']})
        self.assertEqual((status, body['kill_requested']), (200, True), body)
        builder, wrapper = psutil.Process(claim['builder_pid']), psutil.Process(row['pid'])
        self.control(default='ok')
        orch.tick()
        for proc in (builder, wrapper):
            with contextlib.suppress(psutil.Error):
                proc.wait(10)
            self.assertFalse(proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE)
        after = q.get(row['id'])
        self.assertIn('killed by the user', after['error'] or '')
        self.drive(orch, self.settled)
        self.assertEqual(q.get(row['id'])['state'], 'done')
        self.assertTrue((orch.logs / f'{row["id"]}-a2.log').is_file())


class unittest_mock_win:
    """Context manager replacing orchestrator.win_idle_seconds."""
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.old = orchestrator.win_idle_seconds
        orchestrator.win_idle_seconds = lambda: self.value

    def __exit__(self, *exc):
        orchestrator.win_idle_seconds = self.old


# ============================================================================================================== approvals

class ApprovalTests(Phase3Case):
    def test_approve_3d_validation_and_snapshot(self):
        self.job('s', 'j1', 'completed', (('human_male', 'outfit'), ('dwarf_male', 'outfit')), state='converted')
        self.reduce('s', 'j1', 'human_male', 'outfit', lod='0p1')                          # one saved pick, one default
        self.job('t', 'j1', 'processing', (('human_male', 'outfit'),))                     # a set that is not reduced
        status, body = self.act('set:t', 'approve', rev=0, args={'units': {'human_male/outfit': {'job': 'j1', 'lod': '0p2'}}})
        self.assertEqual(status, 400)
        self.assertIn('human_male/outfit (converting)', body['error'])
        status, body = self.approve('s', 0)                                                # dwarf_male has no reduced module yet
        self.assertEqual(status, 400)
        self.assertIn('dwarf_male/outfit (missing)', body['error'])
        self.reduce('s', 'j1', 'dwarf_male', 'outfit')
        units, lods = self.shown('s')
        self.assertEqual((units, lods), ({'human_male/outfit': {'job': 'j1', 'lod': '0p1'}, 'dwarf_male/outfit': {'job': 'j1', 'lod': '0p2'}},
                                         {'dwarf_male/outfit': '0p2'}))
        self.assertEqual(self.act('set:s', 'approve', args={'units': units})[0], 400)             # rev is required
        for bad in ({'units': 'x'}, {}, {'units': {}}, {'units': units, 'lods': {'dwarf_male/outfit': '0p9'}},
                    {'units': units | {'dwarf_male/outfit': {'job': 'j1', 'lod': '0p9'}}}, {'units': units, 'lods': {'dwarf_male/outfit': '0p4'}}):
            self.assertEqual(self.act('set:s', 'approve', rev=0, args=bad)[0], 400, bad)
        # F1: anything that differs from what the server has is a 409 `changed` with the list of changes
        for shown, why in ((units | {'human_male/outfit': {'job': 'j1', 'lod': '0p4'}}, 'lod'),            # contradicts the saved pick
                           (units | {'dwarf_male/outfit': {'job': 'other', 'lod': '0p2'}}, 'job'),
                           ({'human_male/outfit': units['human_male/outfit']}, 'missing'),
                           (units | {'elf_male/outfit': {'job': 'j1', 'lod': '0p2'}}, 'extra')):
            status, body = self.approve('s', 0, units=shown)
            self.assertEqual((status, body['code']), (409, 'changed'), why)
            self.assertTrue(body['changes'], why)
            self.assertEqual(body['item']['id'], 'set:s')
        status, body = self.approve('s', 0, lods={})                                     # the dwarf cell was shown as saved: it is not
        self.assertEqual((status, body['code']), (409, 'changed'))
        status, body = self.approve('s', 9)
        self.assertEqual((status, body['code']), (409, 'rev'))
        self.assertFalse((self.root / 'models/s/pipeline_v2.json').exists())               # nothing was written by any refusal
        self.assertFalse((self.root / 'models/s/j1/dwarf_male/outfit/chosen_lod.json').exists())
        self.assertEqual(taskqueue.for_root(self.root).tasks(), [])
        # the approval
        status, body = self.approve('s', 0)
        self.assertEqual(status, 200, body)
        self.assertEqual((body['rev'], body['stage']), (1, '3d'))
        self.assertEqual(body['undo'], {'verb': 'unapprove', 'id': 'set:s', 'args': {'to': '3d'}, 'rev': 1})
        data = json.loads((self.root / 'models/s/pipeline_v2.json').read_text())
        approval = data['lod_approval']
        self.assertEqual(sorted(approval['required_races']), ['dwarf_male', 'human_male'])
        self.assertEqual((approval['units']['human_male/outfit']['lod'], approval['units']['dwarf_male/outfit']['lod']), ('0p1', '0p2'))
        for key, unit in approval['units'].items():
            race, module = key.split('/')
            glb = self.root / 'models/s/j1' / race / module / 'reduced' / f'lod_error_{unit["lod"]}.glb'
            self.assertEqual((unit['job'], unit['glb_sha256']), ('j1', hashlib.sha256(glb.read_bytes()).hexdigest()))
        self.assertEqual(json.loads((self.root / 'models/s/j1/dwarf_male/outfit/chosen_lod.json').read_text())['lod'], '0p2')    # written with the approval
        self.assertEqual(data['history'][-1]['action'], 'approve_3d')
        tasks = taskqueue.for_root(self.root).tasks()
        sha = {k: v['glb_sha256'][:12] for k, v in approval['units'].items()}
        self.assertEqual(sorted(t['key'] for t in tasks), sorted([f'layers:s:human_male/outfit:0p1:j1:{sha["human_male/outfit"]}',
                                                                  f'layers:s:dwarf_male/outfit:0p2:j1:{sha["dwarf_male/outfit"]}',
                                                                  f'bare:human_male:{builds.body_sha12("human_male")}', f'bare:dwarf_male:{builds.body_sha12("dwarf_male")}']))
        layer = next(t for t in tasks if t['lane'] == 'layers' and t['unit'] == 'human_male/outfit')
        self.assertEqual((layer['set_name'], layer['args']['lod'], layer['args']['job'], layer['state']), ('s', '0p1', 'j1', 'queued'))
        self.assertTrue(Path(layer['args']['module_dir']).samefile(self.root / 'models/s/j1/human_male/outfit'))
        item = self.item()
        self.assertEqual((item['stage'], item['locked']), ('3d', True))
        self.assertEqual({k: v['status'] for k, v in item['builds'].items()}, {'human_male/outfit': 'queued', 'dwarf_male/outfit': 'queued'})
        self.assertIn('building 0/2', [b['text'] for b in item['card']['badges']])
        self.assertEqual({r: v['status'] for r, v in item['bare_builds'].items()}, {'human_male': 'queued', 'dwarf_male': 'queued'})
        self.assertEqual(self.approve('s', 1)[1].get('code'), 'approved')
        status, body = self.lod('s', 1, race='human_male', module='outfit', lod='0p4')
        self.assertEqual((status, body['code']), (409, 'locked'))
        # idempotent keys: unapprove + approve again re-uses the same tasks (cancelled ones come back)
        self.assertEqual(self.act('set:s', 'unapprove', rev=1)[0], 200)
        status, body = self.approve('s', 2)
        self.assertEqual(status, 200, body)
        self.assertEqual(len(taskqueue.for_root(self.root).tasks()), 4)

    def test_a_newer_conversion_at_the_same_rev_is_not_approved_in_place_of_the_shown_one(self):
        """F1 (the review's reproduction): the confirmation showed j1 at 0p2; a newer conversion with a saved 0p1 appears without a rev change."""
        self.full_set('s', 'j1')
        shown_units, shown_lods = self.shown('s')
        self.job('s', 'j2', 'completed', (('human_male', 'outfit'),), state='converted')
        self.reduce('s', 'j2', 'human_male', 'outfit', mtime=4_000_000_000, lod='0p1')
        self.assertEqual(self.item()['rev'], 0)
        status, body = self.approve('s', 0, units=shown_units, lods=shown_lods)
        self.assertEqual((status, body['code']), (409, 'changed'))
        self.assertTrue(any('human_male/outfit' in c and 'j2' in c for c in body['changes']), body['changes'])
        self.assertFalse((self.root / 'models/s/pipeline_v2.json').exists())

    def test_approve_refuses_pin_missing_and_wrong_stages(self):
        self.full_set('s')
        pin = {'schema': 1, 'rev': 4, 'lod_approval': {'units': {'human_male/outfit': {'job': 'gone', 'lod': '0p2'}}}}
        (self.root / 'models/s/pipeline_v2.json').write_text(json.dumps(pin))
        status, body = self.approve_aligned('s')
        self.assertEqual(status, 400)                                                     # aligned approve with a pin_missing unit
        self.assertEqual(self.act('set:nope', 'approve', rev=0)[0], 404)
        self.job('c', 'j', 'processing_views')
        status, body = self.act('set:c', 'approve', rev=0, args={'units': {'human_male/outfit': {'job': 'j', 'lod': '0p2'}}})
        self.assertEqual((status, 'views' in body['error']), (400, True))

    def test_unapprove_cancels_queued_tasks_but_not_running_ones(self):
        self.sets_ready()
        self.assertEqual(self.approve('s', 0)[0], 200)
        q = taskqueue.for_root(self.root)
        first = q.tasks(lane='layers')[0]
        self.assertTrue(q.claim(first['id'], 'tok', None))                       # pretend one build is running
        q.mark_running(first['id'], 'tok', None, None)
        status, body = self.act('set:s', 'unapprove', rev=1)
        self.assertEqual((status, body['stage'], body['cancelled']), (200, '3d', 1))        # the queued layers task; bare tasks belong to no set
        self.assertEqual(sorted(t['state'] for t in q.tasks()), ['cancelled', 'queued', 'queued', 'running'])
        item = self.item()
        self.assertEqual((item['locked'], item['decisions']['lod_approval'], item['builds']), (False, None, {}))
        self.assertEqual(self.act('set:s', 'unapprove', rev=2)[0], 400)                   # nothing left to send back
        # F6: the running build finishes once and is not re-queued for the withdrawn approval; Retry of it is refused
        orch = self.orch()
        q.finish(first['id'], 'tok', False, None, 'PermissionError: transient')
        self.assertEqual(self.post('/api/v2/queue', {'op': 'retry', 'id': first['id']})[0], 400)
        # the dispatcher cancels (never starts) a queued task whose approval is gone; the reconcile step cancels the bare tasks no set needs
        orch.tick()
        self.assertEqual(sorted(t['state'] for t in q.tasks()), ['cancelled', 'cancelled', 'cancelled', 'failed'])

    def test_the_queue_is_a_derived_cache_of_the_approval(self):
        """F7 / F9: a failing queue never rolls back or blocks an approval; reconcile queues what is missing and cancels what has no approval."""
        self.sets_ready()
        with mock.patch.object(taskqueue.TaskQueue, 'enqueue', side_effect=taskqueue.QueueError('queue database: disk I/O error')):
            status, body = self.approve('s', 0)
        self.assertEqual(status, 200, body)
        self.assertIn('disk I/O error', body['queue_error'])
        self.assertTrue(self.item()['locked'])
        q = taskqueue.for_root(self.root)
        self.assertEqual(q.tasks(), [])
        orch = self.orch()
        out = orch.reconcile()
        self.assertIn('s', out['queued'])
        self.assertEqual(len(q.tasks(state='queued')), 4)
        self.assertEqual(orch.reconcile()['queued'], {})                                  # idempotent
        # the approval is withdrawn behind the server's back (a lost queue cancel): reconcile cancels the tasks
        data = json.loads((self.root / 'models/s/pipeline_v2.json').read_text())
        data['lod_approval'] = None
        (self.root / 'models/s/pipeline_v2.json').write_text(json.dumps(data))
        catalog.invalidate()
        self.assertEqual(orch.reconcile()['cancelled'], 4)

    def test_races_are_frozen_by_the_3d_approval(self):
        """F3: drop / undrop are refused while the 3D approval exists (Back to 3D first)."""
        self.sets_ready()
        self.assertEqual(self.approve('s', 0)[0], 200)
        status, body = self.act('set:s', 'drop_race', rev=1, args={'race': 'dwarf_male'})
        self.assertEqual((status, body['code']), (409, 'approved'))
        self.assertEqual(self.act('set:s', 'unapprove', rev=1)[0], 200)
        self.assertEqual(self.act('set:s', 'drop_race', rev=2, args={'race': 'dwarf_male'})[0], 200)
        self.assertEqual(self.approve('s', 3)[0], 200)
        status, body = self.act('set:s', 'undrop_race', rev=4, args={'race': 'dwarf_male'})
        self.assertEqual((status, body['code']), (409, 'approved'))

    def test_amount_changes_and_groups(self):
        orch, _ = self.approve_and_build()
        post = lambda **b: self.post('/api/v2/amount', {'id': 'set:s', 'race': 'human_male', 'group': 'outfit', 'value': 50, 'rev': self.item()['rev']} | b)     # noqa: E731
        status, body = post()
        self.assertEqual((status, body['rev'], body['amount']), (200, 2, {'human_male': {'outfit': 50}}))
        for bad in ({'group': 'body'}, {'group': 'limbs'}, {'value': -1}, {'value': 101}, {'value': 5.5}, {'value': True}, {'value': '5'}, {'race': 'elf_male'},
                    {'group': 7}, {'value': None}):
            self.assertEqual(post(**bad)[0], 400, bad)
        self.assertEqual(post(rev=0)[0], 409)
        status, body = post()                                                              # the same value: nothing written
        self.assertEqual((status, body['rev']), (200, 2))
        self.assertEqual(post(value=0)[1]['rev'], 3)                                       # 0 is allowed
        self.assertEqual(post(value=100, group='outfit', race='dwarf_male')[0], 200)
        self.assertEqual(self.post('/api/v2/amount', {'id': 'set:s', 'race': 'human_male', 'group': 'outfit', 'value': 5, 'rev': 4}, token=False)[0], 403)
        self.assertEqual(self.post('/api/v2/amount', {'id': 'set:nope', 'race': 'human_male', 'group': 'outfit', 'value': 5, 'rev': 4})[0], 404)
        self.assertEqual(self.post('/api/v2/amount', {'id': 'set:s', 'race': 'human_male', 'group': 'outfit', 'value': 5})[0], 400)           # rev required

    def test_amount_needs_the_3d_approval_and_armour_groups(self):
        self.full_set('a', 'j1', ('body_armor', 'right_gauntlet_boot'))
        body = {'id': 'set:a', 'race': 'human_male', 'group': 'body', 'value': 40, 'rev': 0}
        self.assertEqual(self.post('/api/v2/amount', body)[0], 400)                        # not approved yet
        self.assertEqual(self.approve('a', 0)[0], 200)
        self.assertEqual(self.post('/api/v2/amount', body | {'rev': 1})[0], 200)
        self.assertEqual(self.post('/api/v2/amount', body | {'rev': 2, 'group': 'limbs', 'value': 25})[0], 200)
        self.assertEqual(self.post('/api/v2/amount', body | {'rev': 3, 'group': 'outfit'})[0], 400)           # armour has no `outfit`
        self.assertEqual(self.item('a')['decisions']['amount'], {'human_male': {'body': 40, 'limbs': 25}})

    def test_aligned_approve_ready_json_completed_and_unapprove(self):
        orch, _ = self.approve_and_build('a', ('body_armor', 'right_gauntlet_boot'))
        item = self.item('a')
        self.assertEqual(item['stage'], 'aligned', {k: (v['status'], v['error']) for k, v in item['builds'].items()})
        self.assertEqual({k: v['status'] for k, v in item['builds'].items()}, {f'{r}/{m}': 'published' for r in RACES for m in ('body_armor', 'right_gauntlet_boot')})
        unit = item['builds']['human_male/right_gauntlet_boot']
        self.assertEqual(sorted(unit['slots']), ['lower_arm.l', 'lower_arm.r', 'lower_leg.l', 'lower_leg.r'])
        self.assertTrue(all(url.startswith('/files/models/a/j1/human_male/right_gauntlet_boot/layers/0p2-') for url in unit['slots'].values()))
        self.assertEqual(self.get(unit['slots']['lower_arm.l'])[0], 200)                     # the GLB is served
        self.assertEqual(sorted(item['bare']['human_male']), sorted(BARE_SLOTS))
        self.assertEqual({r: v['status'] for r, v in item['bare_builds'].items()}, {r: 'published' for r in RACES})
        self.assertEqual(unit['task']['state'], 'done')
        self.assertEqual((unit['warnings'], unit['metrics']), ([], {'wall_crossings_left': 0}))
        rev = item['rev']
        self.assertEqual(self.post('/api/v2/amount', {'id': 'set:a', 'race': 'human_male', 'group': 'body', 'value': 60, 'rev': rev})[0], 200)
        # a partial amount map is a 400; a map without a race a 409 changed
        args, current = self.aligned_args('a')
        self.assertEqual(self.act('set:a', 'approve', rev=current, args=args | {'amount': {r: {'body': 5} for r in RACES}})[0], 400)
        status, body = self.act('set:a', 'approve', rev=current, args=args | {'amount': {'human_male': args['amount']['human_male']}})
        self.assertEqual((status, body['code']), (409, 'changed'))
        self.assertEqual(self.act('set:a', 'approve', rev=current, args={'amount': args['amount'], 'bare': args['bare']})[0], 400)      # builds missing
        status, body = self.approve_aligned('a', amount={'human_male': {'limbs': 30}, 'dwarf_male': {'body': 70, 'limbs': 80}})
        self.assertEqual((status, body['stage'], body['undo']['args']), (200, 'completed', {'to': 'aligned'}), body)
        ready = json.loads((self.root / 'models/a/ready.json').read_text())
        self.assertEqual((ready['schema'], ready['set'], ready['kind'], ready['approval_id']), (1, 'a', 'armour', body['approval_id']))
        self.assertEqual(ready['races']['human_male']['amount'], {'body': 60, 'limbs': 30})
        self.assertEqual(ready['races']['dwarf_male']['amount'], {'body': 70, 'limbs': 80})
        for race in RACES:
            for module in ('body_armor', 'right_gauntlet_boot'):
                entry = ready['races'][race]['units'][module]
                self.assertEqual((entry['job'], entry['lod']), ('j1', '0p2'))
                self.assertEqual(entry['build_id'], item['builds'][f'{race}/{module}']['build_id'])
                self.assertEqual(sorted(entry['slots']), sorted(item['builds'][f'{race}/{module}']['slots']))
                for slot, rec in entry['slots'].items():
                    path = self.root / rec['glb']                                          # relative to the project root, inside its build
                    self.assertFalse(Path(rec['glb']).is_absolute())
                    self.assertTrue(path.is_file(), rec['glb'])
                    self.assertEqual(rec['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
                    self.assertTrue(rec['glb'].startswith(f'models/a/j1/{race}/{module}/layers/{entry["build_id"]}/'))
        self.assertEqual(sorted(ready['bare']), sorted(RACES))
        for race, rel in ready['bare'].items():
            self.assertEqual(Path(rel).name, builds.bare_pointer(race)['build_id'])
            self.assertFalse(Path(rel).is_absolute())
        data = json.loads((self.root / 'models/a/pipeline_v2.json').read_text())
        self.assertEqual(data['aligned_approval']['approval_id'], body['approval_id'])
        self.assertEqual(data['aligned_approval']['bare'], {r: builds.bare_pointer(r)['build_id'] for r in RACES})
        self.assertEqual(data['aligned_approval']['units']['human_male/body_armor']['build_id'], item['builds']['human_male/body_armor']['build_id'])
        self.assertEqual(data['amount']['human_male'], {'body': 60, 'limbs': 30})
        self.assertEqual([c['id'] for c in self.get('/api/v2/state')[1]['tabs']['completed']], ['set:a'])
        # a changed amount keeps it in Completed with the badge; approving again clears it
        self.assertEqual(self.post('/api/v2/amount', {'id': 'set:a', 'race': 'human_male', 'group': 'body', 'value': 10, 'rev': body['rev']})[0], 200)
        card = self.get('/api/v2/state')[1]['tabs']['completed'][0]
        self.assertIn('amount changed since approval', [b['text'] for b in card['badges']])
        # unapprove completed -> aligned: ready.json revoked (renamed), approval cleared, amounts kept
        status, body = self.act('set:a', 'unapprove', rev=self.item('a')['rev'])
        self.assertEqual((status, body['stage']), (200, 'aligned'))
        self.assertFalse((self.root / 'models/a/ready.json').exists())
        self.assertEqual(len(list((self.root / 'models/a').glob('ready.json.revoked-*'))), 1)
        item = self.item('a')
        self.assertEqual((item['decisions']['aligned_approval'], item['decisions']['amount']['human_male']['body']), (None, 10))
        status, body = self.approve_aligned('a')
        self.assertEqual((status, body['stage']), (200, 'completed'))
        status, body = self.act('set:a', 'unapprove', rev=body['rev'], args={'to': '3d'})
        self.assertEqual((status, body['stage'], body['revoked'].startswith('ready.json.revoked-')), (200, '3d', True))
        self.assertEqual(len(list((self.root / 'models/a').glob('ready.json.revoked-*'))), 2)
        item = self.item('a')
        self.assertEqual((item['locked'], item['decisions']['aligned_approval'], item['builds']), (False, None, {}))

    def test_aligned_approval_is_bound_to_the_displayed_builds(self):
        """F1 (the review's reproduction): the displayed build id was replaced by a -r78 recovery build at the same rev; the bare id too."""
        orch, _ = self.approve_and_build()
        args, rev = self.aligned_args()
        old = args['builds']['human_male/outfit']
        record = self.publish('s', 'j1', 'human_male', 'outfit', fingerprint=builds.fingerprint_module(self.root / 'models/s/j1/human_male/outfit', '0p2'))
        replacement = old + '-r78'
        out = self.root / 'models/s/j1/human_male/outfit/layers'
        (out / record['build_id']).rename(out / replacement)
        record = json.loads(json.dumps(record).replace(record['build_id'], replacement))
        (out / replacement / 'build.json').write_text(json.dumps(record))
        (out / 'current_0p2.json').write_text(json.dumps(record))
        self.assertEqual(self.item()['builds']['human_male/outfit']['build_id'], replacement)
        status, body = self.act('set:s', 'approve', rev=rev, args=args)
        self.assertEqual((status, body['code']), (409, 'changed'))
        self.assertTrue(any(old in c and replacement in c for c in body['changes']), body['changes'])
        self.assertFalse((self.root / 'models/s/ready.json').exists())
        args, rev = self.aligned_args()
        status, body = self.act('set:s', 'approve', rev=rev, args=args | {'bare': dict(args['bare'], human_male='bare-other')})
        self.assertEqual((status, body['code']), (409, 'changed'))
        self.assertEqual(self.act('set:s', 'approve', rev=rev, args=args)[0], 200)

    def test_stale_builds_and_a_changed_source_are_not_certified(self):
        """F2: the Aligned approval rechecks fingerprints and the pinned GLB digest; a changed source shows `source_changed` and is never built."""
        orch, _ = self.approve_and_build()
        # a stale build (the code / inputs changed since: here, the published fingerprint is an old one)
        self.publish('s', 'j1', 'dwarf_male', 'outfit', fingerprint='0ld0ld0ld0ld')
        status, body = self.approve_aligned()
        self.assertEqual((status, body['code']), (409, 'stale'))
        self.assertTrue(any('dwarf_male/outfit' in c for c in body['changes']))
        self.publish_current('s', 'j1', 'dwarf_male', 'outfit')
        # the approved GLB is replaced by another valid LOD (the review's reproduction)
        glb = self.root / 'models/s/j1/human_male/outfit/reduced/lod_error_0p2.glb'
        glb.write_bytes(b'another lod')
        item = self.item()
        rec = item['builds']['human_male/outfit']
        self.assertEqual((rec['status'], item['stage']), ('source_changed', '3d'))
        self.assertTrue(rec['slots'])                                                     # the old layers stay viewable
        self.assertTrue(next(u for u in item['units'] if u['race'] == 'human_male')['source_changed'])
        self.assertIn('source changed 1', [b['text'] for b in item['card']['badges']])
        args = {'builds': {k: v['build_id'] for k, v in item['builds'].items()}, 'bare': {r: v['build_id'] for r, v in item['bare_builds'].items()},
                'amount': {r: {'outfit': 100} for r in RACES}}
        self.assertEqual(self.act('set:s', 'approve', rev=item['rev'], args=args)[0], 400)          # (the set is not in aligned any more)
        # a rebuild never builds it; a queued task for it fails 'source changed' at dispatch
        status, body = self.act('set:s', 'rebuild', rev=item['rev'], args={})
        self.assertEqual(body['actions']['human_male/outfit'], 'source_changed')
        q = taskqueue.for_root(self.root)
        approval = item['decisions']['lod_approval']['units']['human_male/outfit']
        unit = next(u for u in item['units'] if u['race'] == 'human_male')
        tid = q.enqueue('layers', 'forced', 's', 'human_male/outfit', actions._layer_args(self.root, unit | {'model_set': 's'}, '0p2', approval['glb_sha256']))
        orch.tick()
        self.assertEqual(q.get(tid)['state'], 'failed')
        self.assertIn('source changed', q.get(tid)['error'])
        # Back to 3D clears it
        self.assertEqual(self.act('set:s', 'unapprove', rev=item['rev'], args={'to': '3d'})[0], 200)
        self.assertFalse(self.item()['locked'])

    def test_completed_needs_every_unit_every_bare_build_and_its_ready_json(self):
        """F3: Completed = approved units == live units == 3D-approved units, every approved build and bare build still published, and ready.json
        with the approval's id; otherwise the orchestrator revokes ready.json (renamed, event logged)."""
        orch, body = self.completed_set()
        self.assertEqual(self.item()['stage'], 'completed')
        # ready.json replaced by another approval's manifest: not Completed
        ready = self.root / 'models/s/ready.json'
        saved = ready.read_text()
        ready.write_text(json.dumps(json.loads(saved) | {'approval_id': 'another'}))
        self.assertEqual(self.item()['stage'], 'aligned')
        ready.write_text(saved)
        self.assertEqual(self.item()['stage'], 'completed')
        # the bare build of a race is republished: not Completed, and reconcile revokes the manifest
        bare_ptr = self.bare_dir / 'dwarf_male' / 'current_bare.json'
        bare_saved = bare_ptr.read_text()
        other = json.loads(bare_saved)
        old_id = other['build_id']
        other = json.loads(bare_saved.replace(old_id, old_id + '-r1'))
        (self.bare_dir / 'dwarf_male' / (old_id + '-r1')).mkdir()
        for slot in BARE_SLOTS:
            (self.bare_dir / 'dwarf_male' / (old_id + '-r1') / f'{slot}.glb').write_bytes(b'x')
        (self.bare_dir / 'dwarf_male' / (old_id + '-r1') / 'build.json').write_text(json.dumps(other))
        bare_ptr.write_text(json.dumps(other))
        item = self.item()
        self.assertEqual(item['stage'], 'aligned')
        self.assertIn('bare dwarf_male: published bare build is not the approved one', item['not_completed'])
        out = orch.reconcile()
        self.assertEqual(len(out['revoked']), 1)
        self.assertFalse(ready.exists())
        self.assertEqual(len(list(ready.parent.glob('ready.json.revoked-*'))), 1)
        self.assertTrue(any(e['kind'] == 'revoke' for e in taskqueue.for_root(self.root).events()))
        bare_ptr.write_text(bare_saved)
        self.assertEqual(self.item()['stage'], 'aligned')                                  # the manifest is gone: approve again
        self.assertEqual(self.approve_aligned()[1]['stage'], 'completed')
        # the pinned conversion of a unit disappears: pin_missing, not Completed; reconcile revokes again
        (self.root / 'models/s/j1/human_male/outfit/reduced/manifest.json').unlink()
        item = self.item()
        self.assertEqual(next(u['status'] for u in item['units'] if u['race'] == 'human_male'), 'pin_missing')
        self.assertNotEqual(item['stage'], 'completed')
        self.assertEqual(len(orch.reconcile()['revoked']), 1)
        self.assertFalse(ready.exists())

    def test_unapprove_is_one_transaction_with_the_rev_checked_first(self):
        """F7: unapprove checks the rev before renaming anything, and the decision + ready.json revocation are ONE transaction."""
        orch, body = self.completed_set()
        ready = self.root / 'models/s/ready.json'
        status, answer = self.act('set:s', 'unapprove', rev=body['rev'] - 1)
        self.assertEqual((status, answer['code']), (409, 'rev'))
        self.assertTrue(ready.is_file())                                                  # nothing renamed by a refused request
        self.assertEqual(list(ready.parent.glob('ready.json.revoked-*')), [])

        def die(path):
            raise OSError('power cut')
        guard._FAULTS['replace'] = die
        status, answer = self.act('set:s', 'unapprove', rev=body['rev'])
        guard._FAULTS.clear()
        self.assertEqual(status, 400)
        self.assertTrue(ready.is_file())                                                  # rolled back as a whole
        self.assertEqual(list(ready.parent.glob('ready.json.revoked-*')), [])
        self.assertEqual(self.item()['stage'], 'completed')
        self.assertEqual(self.act('set:s', 'unapprove', rev=body['rev'])[0], 200)

    def test_pointer_validation_is_strict(self):
        """F4: a published build must be exactly this module's, with exactly the required slots, equal to its build.json, inside its folder."""
        self.full_set('a', 'j1', ('right_gauntlet_boot',))
        self.assertEqual(self.approve('a', 0)[0], 200)
        mod = self.root / 'models/a/j1/human_male/right_gauntlet_boot'
        record = self.publish_current('a', 'j1', 'human_male', 'right_gauntlet_boot')
        status = lambda: self.item('a')['builds']['human_male/right_gauntlet_boot']['status']       # noqa: E731
        self.assertEqual(status(), 'published')
        ptr, bjson = mod / 'layers/current_0p2.json', mod / 'layers' / record['build_id'] / 'build.json'
        good = ptr.read_text()
        no_boots = dict(record, required=['lower_arm.l', 'lower_arm.r'], slots={k: v for k, v in record['slots'].items() if k.startswith('lower_arm')})
        variants = {'boots removed with their declaration': (no_boots, no_boots),
                    'foreign extra slot': (dict(record, slots=dict(record['slots'], torso={'glb': f'{record["build_id"]}/torso.glb'})),) * 2,
                    'path outside the build': (dict(record, slots=dict(record['slots'], **{'lower_arm.l': {'glb': '../../../../../../outside.glb'}})),) * 2,
                    'pointer differs from build.json': (dict(record, warnings=['copied']), record),
                    'another module (suffix only)': (dict(record, module='elsewhere/' + record['module']),) * 2,
                    'another race': (dict(record, race='dwarf_male'),) * 2}
        for why, (pointer, build_json) in variants.items():
            ptr.write_text(json.dumps(pointer))
            bjson.write_text(json.dumps(build_json))
            self.assertIn(status(), ('none', 'queued'), why)                          # not published (the approval queued a build)
        ptr.write_text(good)
        bjson.write_text(good)
        self.assertEqual(status(), 'published')
        self.assertEqual(builds.lb().required_slots('right_gauntlet_boot', 'human_male'), ['lower_arm.l', 'lower_arm.r', 'lower_leg.l', 'lower_leg.r'])
        with self.assertRaises(ValueError):
            builds.rel_path(self.root, Path(self.temp.name).parent / 'outside.glb')


# =============================================================================================================== recovery

class RecoveryTests(Phase3Case):
    def interrupted_amount_write(self):
        """An Aligned approval whose transaction failed mid-way AND whose rollback failed: an unresolved intent of set s."""
        orch, _ = self.approve_and_build()
        guard._FAULTS['replace'] = lambda path: (_ for _ in ()).throw(OSError('second file refused'))
        guard._FAULTS['rollback'] = lambda: (_ for _ in ()).throw(OSError('rollback refused too'))
        status, body = self.approve_aligned()
        guard._FAULTS.clear()
        self.assertEqual(status, 400, body)
        intents = guard.list_intents(self.root)
        self.assertEqual([(i['kind'], i['set']) for i in intents], [('files_tx', 's')])
        return orch, intents[0]

    def test_a_set_with_an_unresolved_intent_is_read_only_and_recovery_never_erases_a_later_decision(self):
        """F8: writes are 409 `recovering` until recovery; recovery restores only files that still hold what the transaction wrote; a file
        changed since makes the intent a `conflict` needing a manual choice."""
        orch, intent = self.interrupted_amount_write()
        item = self.item()
        self.assertTrue(item['recovering'])
        self.assertIn('recovering: read-only', [b['text'] for b in item['card']['badges']])
        status, body = self.post('/api/v2/amount', {'id': 'set:s', 'race': 'human_male', 'group': 'outfit', 'value': 23, 'rev': item['rev']})
        self.assertEqual((status, body['code']), (409, 'recovering'))
        # a later decision lands in the file anyway (e.g. the legacy writer): recovery must not restore over it
        path = self.root / 'models/s/pipeline_v2.json'
        later = json.loads(path.read_text())
        later['amount'] = {'human_male': {'outfit': 23}}
        later['rev'] += 1
        path.write_text(json.dumps(later))
        notes = actions.recover_transactions(self.root)
        self.assertIn('conflict', notes[0])
        self.assertEqual(json.loads(path.read_text())['amount'], {'human_male': {'outfit': 23}})          # untouched
        card = next(c for c in self.get('/api/v2/state')[1]['tabs']['trash'] if c['kind'] == 'recovery')
        self.assertEqual(card['actions'], ['complete', 'rollback'])
        self.assertIn('conflict: choose keep or restore', [b['text'] for b in card['badges']])
        # the manual choice: keep the files as they are now
        self.assertEqual(self.act(card['id'], 'complete')[0], 200)
        self.assertEqual(guard.list_intents(self.root), [])
        self.assertEqual(json.loads(path.read_text())['amount'], {'human_male': {'outfit': 23}})
        self.assertFalse(self.item()['recovering'])

    def test_an_untouched_interrupted_write_is_recovered_by_the_orchestrator(self):
        orch, intent = self.interrupted_amount_write()
        before = {e['path']: e['before_sha256'] for e in intent['files']}
        out = orch.reconcile()
        self.assertTrue(any('rolled back' in n for n in out['recovered']), out)
        self.assertEqual(guard.list_intents(self.root), [])
        for rel, sha in before.items():
            self.assertEqual(guard.sha256_or_none(guard.read_bytes_or_none(self.root / rel)), sha)
        self.assertEqual(self.approve_aligned()[1]['stage'], 'completed')


# ======================================================================================================== rebuild / stale

class RebuildTests(Phase3Case):
    def test_failed_units_show_the_error_and_rebuild_retries_them(self):
        self.sets_ready()
        self.assertEqual(self.approve('s', 0)[0], 200)
        self.control(default='ok', **{'human_male/outfit': 'garment'})
        orch = self.orch()
        self.started.append(orch)
        self.idle_value = 1000.0
        self.drive(orch, self.settled)
        item = self.item()
        self.assertEqual({k: v['status'] for k, v in item['builds'].items()}, {'human_male/outfit': 'failed', 'dwarf_male/outfit': 'published'})
        self.assertIn('no garment found', item['builds']['human_male/outfit']['error'])
        self.assertEqual(item['builds']['human_male/outfit']['task']['state'], 'failed')
        self.assertIn('no garment found', item['builds']['human_male/outfit']['task']['error'])
        self.assertEqual(item['stage'], '3d')
        self.assertIn({'text': 'build failed 1', 'tone': 'error'}, item['card']['badges'])
        self.assertEqual(orch.reconcile()['queued'], {})                                    # a deterministic failure is never re-queued by itself
        self.control(default='ok')
        status, body = self.act('set:s', 'rebuild', rev=1, args={})
        self.assertEqual(status, 200, body)
        self.assertEqual((body['actions']['human_male/outfit'], body['actions']['dwarf_male/outfit']), ('retried', 'current'))
        self.drive(orch, self.settled)
        self.assertEqual(self.item()['stage'], 'aligned')
        self.assertEqual(self.act('set:s', 'rebuild', rev=1, args={'race': 'elf_male'})[0], 400)
        self.assertEqual(self.act('set:s', 'rebuild', rev=1, args={'module': 3})[0], 400)
        status, body = self.act('set:s', 'rebuild', rev=1, args={'race': 'dwarf_male', 'module': 'outfit'})
        self.assertEqual((status, body['queued']), (200, 0), body)
        self.assertEqual(self.act('set:nope', 'rebuild', rev=1)[0], 404)

    def test_rebuild_of_an_unapproved_set_is_refused(self):
        self.sets_ready()
        self.assertEqual(self.act('set:s', 'rebuild', rev=0, args={})[0], 400)

    def test_a_missing_publication_is_repaired_every_time(self):
        """F11: a done key whose publication is missing again is revalidated and built again (the second repair used to answer `current`)."""
        orch, _ = self.approve_and_build()
        pointer = self.root / 'models/s/j1/human_male/outfit/layers/current_0p2.json'
        for round_ in (1, 2):
            pointer.unlink()
            self.assertEqual(self.item()['builds']['human_male/outfit']['status'], 'none', round_)
            status, body = self.act('set:s', 'rebuild', rev=1, args={'race': 'human_male'})
            self.assertEqual((status, body['actions']['human_male/outfit']), (200, 'queued'), (round_, body))
            self.drive(orch, self.settled)
            self.assertEqual(self.item()['builds']['human_male/outfit']['status'], 'published', round_)
        # the reconcile step does the same on its own; the shared bare build too
        pointer.unlink()
        (self.bare_dir / 'dwarf_male' / 'current_bare.json').unlink()
        self.drive(orch, lambda: self.item()['stage'] == 'aligned' and self.settled())
        self.assertTrue(builds.bare_pointer('dwarf_male'))

    def test_stale_scan_marks_old_builds_and_rebuild_gets_a_new_key(self):
        self.sets_ready()
        self.assertEqual(self.approve('s', 0)[0], 200)
        for race in RACES:
            self.publish('s', 'j1', race, 'outfit', fingerprint='0ld0ld0ld0ld')
        self.build_bare(*RACES)
        for task in taskqueue.for_root(self.root).tasks():                              # the approval's tasks ran "earlier"
            self.settle(task['id'])
        data = orchestrator.scan_stale(self.root)
        self.assertEqual(sorted(data['units']), ['s/dwarf_male/outfit', 's/human_male/outfit'])
        self.assertEqual(data['units']['s/human_male/outfit']['build_id'], '0p2-0ld0ld0ld0ld')
        self.assertEqual(data['bare'], {})
        item = self.item()
        self.assertEqual({k: v['status'] for k, v in item['builds'].items()}, {'human_male/outfit': 'stale', 'dwarf_male/outfit': 'stale'})
        self.assertEqual(item['stage'], 'aligned')                                          # stale counts as published
        self.assertIn('stale 2', [b['text'] for b in item['card']['badges']])
        orch = self.orch()
        self.started.append(orch)
        self.assertEqual(orch.reconcile()['queued'], {})                                    # stale builds are not rebuilt by themselves
        status, body = self.act('set:s', 'rebuild', rev=1, args={})
        self.assertEqual((status, body['queued']), (200, 2), body)
        q = taskqueue.for_root(self.root)
        new_keys = [t['key'] for t in q.tasks(state='queued') if t['lane'] == 'layers']
        self.assertEqual(len(new_keys), 2)
        self.assertTrue(all(k.count(':') == 6 for k in new_keys), new_keys)                  # layers:S:unit:lod:job:sha12:fp12
        self.idle_value = 1000.0
        self.drive(orch, self.settled)
        self.assertEqual(orchestrator.scan_stale(self.root)['units'], {})
        item = self.item()
        self.assertEqual({v['status'] for v in item['builds'].values()}, {'published'})
        self.assertNotIn('0ld0ld0ld0ld', json.dumps(item['builds']))
        self.assertEqual(self.act('set:s', 'rebuild', rev=1, args={})[1]['queued'], 0)

    def test_rebuild_stale_queue_operation(self):
        self.sets_ready()
        self.assertEqual(self.approve('s', 0)[0], 200)
        for race in RACES:
            self.publish('s', 'j1', race, 'outfit', fingerprint='0ld0ld0ld0ld')
        self.build_bare(*RACES)
        for task in taskqueue.for_root(self.root).tasks():
            self.settle(task['id'])
        self.assertEqual(self.post('/api/v2/queue', {'op': 'rebuild_stale'})[1], {'ok': True, 'queued': 0, 'sets': []})     # nothing scanned yet
        orchestrator.scan_stale(self.root)
        status, body = self.post('/api/v2/queue', {'op': 'rebuild_stale'})
        self.assertEqual((status, body['queued'], body['sets']), (200, 2, ['s']), body)
        self.assertEqual(self.post('/api/v2/queue', {'op': 'rebuild_stale'})[1]['queued'], 0)          # idempotent while they are queued

    def test_a_pointer_with_the_current_fingerprint_is_not_stale(self):
        self.sets_ready()
        self.assertEqual(self.approve('s', 0)[0], 200)
        module = self.root / 'models/s/j1/human_male/outfit'
        fp = builds.fingerprint_module(module, '0p2')
        self.publish('s', 'j1', 'human_male', 'outfit', fingerprint=fp)
        self.publish_current('s', 'j1', 'dwarf_male', 'outfit')
        self.assertEqual(orchestrator.scan_stale(self.root)['units'], {})
        glb = module / 'reduced/lod_error_0p2.glb'
        glb.write_bytes(b'changed glb bytes')
        self.assertEqual(list(orchestrator.scan_stale(self.root)['units']), [])            # (a changed approved source is not "stale": source_changed)
        self.assertEqual(self.item()['builds']['human_male/outfit']['status'], 'source_changed')
        self.assertNotEqual(builds.fingerprint_module(module, '0p2'), fp)


# ===================================================================================================================== endpoints

class EndpointTests(Phase3Case):
    def test_layers_endpoint(self):
        self.full_set('a', 'j1', ('body_armor', 'right_gauntlet_boot'))
        self.full_set('o', 'j1', ('outfit',))
        for name in ('a', 'o'):
            self.assertEqual(self.approve(name, 0)[0], 200)
        self.assertEqual(self.get('/api/v2/layers?race=human_male')[1]['layers'], [])
        for race in RACES:
            self.publish('a', 'j1', race, 'body_armor')
            self.publish('a', 'j1', race, 'right_gauntlet_boot')
        self.publish('o', 'j1', 'human_male', 'outfit')                                  # set o: dwarf_male not built, so it stays in 3d
        self.build_bare('human_male')
        self.assertEqual(self.post('/api/v2/amount', {'id': 'set:a', 'race': 'human_male', 'group': 'limbs', 'value': 40, 'rev': 1})[0], 200)
        status, data = self.get('/api/v2/layers?race=human_male')
        self.assertEqual(status, 200)
        self.assertEqual(data['race'], 'human_male')
        self.assertEqual(sorted(data['bare']), sorted(BARE_SLOTS))
        rows = {(r['set'], r['module'], r['slot']): r for r in data['layers']}
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[('a', 'body_armor', 'torso')]['amount'], 100)
        self.assertEqual(rows[('a', 'right_gauntlet_boot', 'lower_arm.r')]['amount'], 40)
        self.assertEqual(rows[('a', 'right_gauntlet_boot', 'lower_leg.l')]['amount'], 40)
        self.assertEqual(rows[('o', 'outfit', 'outfit')]['amount'], 100)
        row = rows[('a', 'body_armor', 'torso')]
        self.assertEqual((row['stage'], row['kind'], row['title'], row['build_id']), ('aligned', 'armour', 'a', '0p2-fp0'))
        self.assertEqual(rows[('o', 'outfit', 'outfit')]['stage'], '3d')
        self.assertEqual(self.get(row['url'])[0], 200)
        dwarf = self.get('/api/v2/layers?race=dwarf_male')[1]
        self.assertEqual((dwarf['bare'], {r['set'] for r in dwarf['layers']}, len(dwarf['layers'])), ({}, {'a'}, 5))
        self.assertEqual(self.get('/api/v2/layers?race=nobody')[0], 404)
        self.assertEqual(self.get('/api/v2/layers')[0], 404)
        self.full_set('x', 'j1', ('outfit',))
        self.publish('x', 'j1', 'human_male', 'outfit')
        self.assertNotIn('x', {r['set'] for r in self.get('/api/v2/layers?race=human_male')[1]['layers']})

    def test_queue_endpoint_and_ops(self):
        q = taskqueue.for_root(self.root)
        logs = self.root / '.codex_jobs' / '_app' / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        a = q.enqueue('layers', 'ka', 's', 'human_male/outfit', {})
        (logs / f'{a}-a1.log').write_text('line one\n' * 200 + 'the last line\n')
        q.claim(a, 'tok', str(logs / f'{a}-a1.log'))
        q.mark_running(a, 'tok', 1, None)
        q.finish(a, 'tok', False, None, 'boom happened')
        b = q.enqueue('layers', 'kb', 's', 'dwarf_male/outfit', {})
        c = q.enqueue('bare', 'kc', None, 'human_male', {})
        d = q.enqueue('layers', 'kd', 's', 'dwarf_male/body_armor', {})
        self.settle(d)
        old = q.enqueue('layers', 'kold', 's', 'human_male/body_armor', {})
        self.settle(old)
        with contextlib.closing(q._connect()) as conn:
            conn.execute('UPDATE tasks SET updated=? WHERE id=?', (time.time() - 90000, old))        # finished more than 24 h ago
        status, data = self.get('/api/v2/queue')
        self.assertEqual(status, 200)
        self.assertEqual([t['id'] for t in data['tasks']], [d, c, b, a])
        failed = data['tasks'][3]
        self.assertEqual((failed['state'], failed['error'], failed['set'], failed['unit'], failed['lane'], failed['attempts']), ('failed', 'boom happened', 's', 'human_male/outfit', 'layers', 1))
        self.assertTrue(failed['log_tail'].rstrip().endswith('the last line'))
        self.assertLess(len(failed['log_tail']), 1000)
        self.assertEqual(set(data['workers']), {'limit', 'running', 'present', 'mode', 'idle', 'orchestrator'})
        self.assertFalse(data['workers']['orchestrator'])
        self.assertEqual((data['totals']['done'], data['page']['recent_total'], data['queue_health']['ok']), (2, 2, True))
        # retry / cancel (a raw task without an approval cannot be retried: F6)
        self.assertEqual(self.post('/api/v2/queue', {'op': 'retry', 'id': a})[0], 400)
        with mock.patch.object(actions, 'task_approved', return_value=True):
            self.assertEqual(self.post('/api/v2/queue', {'op': 'retry', 'id': a})[1]['state'], 'queued')
        self.assertEqual(q.get(a)['attempts'], 0)
        self.assertEqual(self.post('/api/v2/queue', {'op': 'cancel', 'id': b})[1]['state'], 'cancelled')
        self.assertEqual(self.post('/api/v2/queue', {'op': 'cancel', 'id': d})[0], 400)               # done: cannot cancel
        self.assertEqual(self.post('/api/v2/queue', {'op': 'retry', 'id': c})[0], 400)                # queued: cannot retry
        self.assertEqual(self.post('/api/v2/queue', {'op': 'kill_retry', 'id': c})[0], 400)           # not running / stalled
        self.assertEqual(self.post('/api/v2/queue', {'op': 'retry', 'id': 999})[0], 404)
        self.assertEqual(self.post('/api/v2/queue', {'op': 'nope', 'id': a})[0], 400)
        self.assertEqual(self.post('/api/v2/queue', {'op': 'retry', 'id': 'x'})[0], 400)
        self.assertEqual(self.post('/api/v2/queue', {'op': 'retry', 'id': a}, token=False)[0], 403)

    def test_allowlist_has_the_phase3_verbs_only(self):
        self.assertIn(('set', 'approve'), actions.ALLOWED)
        self.assertIn(('set', 'rebuild'), actions.ALLOWED)
        self.assertNotIn(('concepts', 'rebuild'), actions.ALLOWED)
        self.full_set('s')
        self.assertEqual(self.act('concepts:s', 'rebuild', rev=0)[0], 400)

    def test_state_serves_the_new_tabs(self):
        self.sets_ready()
        self.approve('s', 0)
        state = self.get('/api/v2/state')[1]
        self.assertEqual([c['id'] for c in state['tabs']['3d']], ['set:s'])
        self.assertIn('orchestrator', state['server'])
        self.assertIsNone(state['queue_health'])


# ================================================================================================================ server wiring

class ServerWiringTests(Phase3Case):
    def make(self, track=True, **kwargs):
        server_ = server.create_server(self.root, port=0, web=self.web, extra_hosts=[], **kwargs)
        if track:
            self.extra_servers.append(server_)
        return server_

    def test_orchestrator_only_in_the_writer_and_not_by_default(self):
        plain = server.create_server(Path(self.temp.name) / 'plain', port=0, web=self.web, extra_hosts=[])
        self.extra_servers.append(plain)
        self.assertIsNone(plain.orchestrator)
        self.assertFalse(plain.ctx.get('orchestrator'))
        self.server.shutdown()
        self.server.server_close()
        first = self.make(run_orchestrator=True)
        self.assertTrue(first.ctx['orchestrator'] and first.orchestrator.active)
        self.assertTrue(first.orchestrator.thread.is_alive())
        second = self.make(run_orchestrator=True)
        self.assertTrue(second.ctx['read_only'])
        self.assertIsNone(second.orchestrator)
        first.server_close()
        self.assertFalse(first.orchestrator is not None)
        third = self.make(run_orchestrator=True)
        self.assertTrue(third.ctx['orchestrator'])

    def test_a_held_orchestrator_lock_makes_the_server_read_only(self):
        self.server.shutdown()
        self.server.server_close()
        holder = orchestrator.FileLock(self.root / '.codex_jobs' / '_app' / 'orchestrator.lock')
        self.assertTrue(holder.acquire())
        self.addCleanup(holder.release)
        blocked = self.make(run_orchestrator=True)
        self.assertTrue(blocked.ctx['read_only'])
        self.assertIn('orchestrator lock', blocked.ctx['message'])
        self.assertIsNone(blocked.orchestrator)
        holder.release()
        blocked.server_close()
        free = self.make()
        self.assertFalse(free.ctx['read_only'])

    def test_read_only_server_refuses_the_new_posts(self):
        self.server.shutdown()
        self.server.server_close()
        ro = self.make(track=False, read_only=True)
        thread = threading.Thread(target=ro.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(ro.server_close)
        self.addCleanup(lambda: (ro.shutdown(), thread.join()))
        import http.client
        for path in ('/api/v2/amount', '/api/v2/queue', '/api/v2/action'):
            conn = http.client.HTTPConnection('127.0.0.1', ro.server_port)
            conn.request('POST', path, json.dumps({}).encode(), {'X-Token': ro.token, 'Content-Type': 'application/json'})
            response = conn.getresponse()
            self.assertEqual(response.status, 503, path)
            conn.close()

    def test_orchestrator_thread_runs_builds_end_to_end(self):
        """The real thread (started by the server): approve over HTTP, the thread builds with the fake builder, the set reaches Aligned."""
        self.server.shutdown()
        self.server.server_close()
        self.sets_ready()
        runner_server = self.make(track=False, run_orchestrator=True)
        runner_server.orchestrator.argv_for = self.fake_argv
        runner_server.orchestrator.interval = 0.2
        runner_server.orchestrator.idle = lambda: 1000.0
        self.server = runner_server
        self.token = runner_server.token
        self.thread = threading.Thread(target=runner_server.serve_forever, daemon=True)
        self.thread.start()
        self.assertEqual(self.approve('s', 0)[0], 200)
        end = time.time() + 90
        while time.time() < end and self.item()['stage'] != 'aligned':
            time.sleep(0.3)
        item = self.item()
        self.assertEqual(item['stage'], 'aligned', {k: v['status'] for k, v in item['builds'].items()})
        self.assertTrue(self.get('/api/v2/queue')[1]['workers']['orchestrator'])
        self.assertEqual(self.approve_aligned()[1]['stage'], 'completed')


if __name__ == '__main__':
    unittest.main()
