"""Tests of phase 4 part B of the v2 preview backend (DESIGN_PHASE4.md H): the services thread and its request queue, `--manage-services` off,
the convert list on approve, the phase 4 queue ops, the Approved-tab unit progress and the header status summary.

Run: python -X utf8 -m unittest tools/preview/v2/test_phase4b.py   (temp roots only; the real root and the live services are never touched)

Service tests use the FAKE service scripts of test_phase4.py at `<temp root>/tools/<real script name>`: the manager matches a process only when its
script resolves inside the temp root, so the live supervisor / view runner / convert worker of the real checkout are invisible here. Every
process a test starts is killed in tearDown (by PID, after the server and its services thread are stopped).
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
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
import actions  # noqa: E402
import builds  # noqa: E402
import catalog  # noqa: E402
import convert_list  # noqa: E402
import credits  # noqa: E402
import orchestrator  # noqa: E402
import progress  # noqa: E402
import server  # noqa: E402
import services  # noqa: E402
import settings  # noqa: E402
import taskqueue  # noqa: E402
from test_phase4 import TempRoot  # noqa: E402
from test_v2 import V2Case  # noqa: E402

ALL_OFF = {'services': {'supervisor': False, 'views': False, 'convert': False}}


def wait_for(condition, timeout=30.0, what='condition'):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = condition()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError(f'timed out waiting for {what}')


# ------------------------------------------------------------------------------------------------------------- services thread

class ServiceManagerTest(TempRoot):
    """orchestrator.ServiceManager against the fake services of a temp root."""

    def setUp(self):
        super().setUp()
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.stop()
        super().tearDown()

    def manager(self, settings_data=None, start=True, interval=3600.0):
        if settings_data is not None:
            settings.save(self.root, settings_data)
        manager = orchestrator.ServiceManager(self.root, interval=interval)
        self.managers.append(manager)
        if start:
            manager.start()
        return manager

    def result_of(self, manager, request_id):
        return wait_for(lambda: next((e for e in manager.events if e['kind'] == 'request' and e.get('request') == request_id), None),
                        what=f'request {request_id}')

    def test_tick_adopts_and_keeps_the_last_snapshot(self):
        hand = self.spawn('views')
        manager = self.manager(ALL_OFF)
        wait_for(lambda: manager.statuses, what='first tick')
        views = manager.statuses['views']
        self.assertEqual((views['state'], views['adopted'], views['pid']), ('running', True, hand.pid))
        self.assertEqual(manager.statuses['supervisor']['state'], 'stopped')
        self.assertEqual(self.running('supervisor') + self.running('convert'), [])          # disabled: never started
        self.assertTrue(any('adopted' in e['text'] for e in manager.events))
        self.assertEqual(manager.problems(), [])                                             # disabled + not running is no problem
        view = manager.view()
        self.assertTrue(view['managed'] and view['scanned'])
        self.assertEqual(view['services']['views']['state'], 'running')
        kinds = {e['kind'] for e in taskqueue.for_root(self.root).events()}
        self.assertIn('service', kinds)                                                      # tick events go to the queue's event log

    def test_an_enabled_missing_service_is_started_by_the_tick(self):
        manager = self.manager({'services': {'supervisor': False, 'views': True, 'convert': False}})
        wait_for(lambda: self.running('views'), what='views to be started')
        wait_for(lambda: (manager.statuses or {}).get('views', {}).get('state') == 'running', what='snapshot')
        self.assertFalse(manager.statuses['views']['adopted'])
        self.assertEqual(self.running('supervisor'), [])

    def test_start_stop_requests_run_on_the_services_thread(self):
        manager = self.manager(ALL_OFF)
        wait_for(lambda: manager.statuses, what='first tick')
        answer = manager.request('views', 'start')
        self.assertEqual((answer['ok'], answer['pending'], answer['name'], answer['action']), (True, True, 'views', 'start'))
        event = self.result_of(manager, answer['request'])
        self.assertTrue(event['ok'], event)
        self.assertTrue(self.running('views'))
        self.assertEqual(manager.statuses['views']['state'], 'running')
        event = self.result_of(manager, manager.request('views', 'stop')['request'])
        self.assertTrue(event['ok'], event)
        self.assertEqual(self.running('views'), [])
        self.assertEqual(manager.statuses['views']['state'], 'stopped')
        event = self.result_of(manager, manager.request('views', 'stop')['request'])     # not running: ok, nothing to do
        self.assertTrue(event['ok'])
        self.spawn('supervisor')
        event = self.result_of(manager, manager.request('supervisor', 'start')['request'])  # refused by the manager: already running
        self.assertFalse(event['ok'])
        self.assertIn('already running', event['text'])
        self.assertEqual(len(self.running('supervisor')), 1)

    def test_restart_of_the_supervisor_keeps_the_pause_file(self):
        manager = self.manager(ALL_OFF)
        wait_for(lambda: manager.statuses, what='first tick')
        old = self.spawn('supervisor')
        pause = self.root / '.codex_jobs/_batch/PAUSE'
        text = json.dumps({'since': '2026-11-25T10:00:00', 'reason': 'manual: pause-conversions', 'scope': ['conversions']})
        pause.write_text(text, encoding='utf-8')
        event = self.result_of(manager, manager.request('supervisor', 'restart')['request'])
        self.assertTrue(event['ok'], event)
        running = self.running('supervisor')
        self.assertEqual(len(running), 1)
        self.assertNotEqual(running[0]['pid'], old.pid)                                      # a new supervisor
        self.assertEqual(pause.read_text(encoding='utf-8'), text)                           # the old pause was written straight back
        self.assertEqual(manager.statuses['supervisor']['state'], 'running')

    def test_requests_are_validated_and_one_per_service_at_a_time(self):
        manager = self.manager(ALL_OFF, start=False)                                         # thread not running: requests stay queued
        for name, action in (('nope', 'start'), ('views', 'kill'), (None, 'start')):
            with self.assertRaises(ValueError):
                manager.request(name, action)
        manager.request('views', 'start')
        with self.assertRaises(orchestrator.guard.Busy):
            manager.request('views', 'stop')
        manager.request('convert', 'stop')                                                   # another service is fine
        self.assertEqual([r['name'] for r in manager.view()['pending']], ['views', 'convert'])
        self.assertEqual(self.running('views'), [])                                          # nothing ran on the caller's thread

    def test_stop_of_the_manager_never_stops_a_service(self):
        manager = self.manager(ALL_OFF)
        self.result_of(manager, manager.request('views', 'start')['request'])
        manager.stop()
        self.assertFalse(manager.thread.is_alive())
        self.assertTrue(self.running('views'))


# ------------------------------------------------------------------------------------------------------------- server wiring

class ServerServicesTest(TempRoot):
    """The `service` op and the services keys through HTTP, with and without --manage-services."""

    def setUp(self):
        super().setUp()
        warnings.simplefilter('ignore', ResourceWarning)
        self.web = Path(tempfile.mkdtemp(prefix='v2p4b_web_'))
        (self.web / 'index.html').write_text('<!doctype html><html><head></head><body></body></html>')
        for name in ('concepts', 'provisional', 'approved', 'models', 'processing', 'processing_views', 'multiview_ready', 'completed'):
            (self.root / name).mkdir(exist_ok=True)
        (self.root / 'base_views/human_male').mkdir(parents=True)
        (self.root / 'base_views/human_male/palette.json').write_text('{}')
        self.server = None

    def start_server(self, manage):
        catalog.STATE_TTL = 0
        self.server = server.create_server(self.root, port=0, web=self.web, extra_hosts=[], manage_services=manage, services_interval=3600.0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
        shutil.rmtree(self.web, ignore_errors=True)
        super().tearDown()

    def call(self, method, path, payload=None):
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=60)
        headers = {'Content-Type': 'application/json', 'X-Token': self.server.token}
        conn.request(method, path, body=json.dumps(payload).encode() if payload is not None else None, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, data

    def test_without_manage_services_nothing_is_started_and_the_service_op_is_refused(self):
        self.start_server(False)                                    # every service enabled (defaults): still nothing may be started
        time.sleep(1.5)
        self.assertEqual({k: v for k, v in services.scan(self.root).items() if v}, {})
        self.assertFalse((self.root / '.codex_jobs/_app/services').exists())
        self.assertIsNone(self.server.services)
        status, body = self.call('POST', '/api/v2/queue', {'op': 'service', 'name': 'views', 'action': 'start'})
        self.assertEqual((status, body['code']), (409, 'services_off'))
        self.assertEqual(body['error'], 'service management is off (start the server with --manage-services)')
        self.assertEqual(self.running('views'), [])
        status, queue = self.call('GET', '/api/v2/queue')
        self.assertEqual((status, queue['services']['managed'], queue['services']['problems']), (200, False, None))
        status, state = self.call('GET', '/api/v2/state')
        self.assertIsNone(state['status']['services'])
        self.assertIsNone(state['status']['services_problems'])
        status, body = self.call('POST', '/api/v2/queue', {'op': 'mode', 'mode': 'pause'})      # the rest works
        self.assertEqual((status, body['mode']['mode']), (200, 'pause'))

    def test_with_manage_services_adopts_and_acts_on_requests(self):
        settings.save(self.root, ALL_OFF)
        hand = self.spawn('views')
        self.start_server(True)
        self.assertIsNotNone(self.server.services)

        def adopted():
            _status, queue = self.call('GET', '/api/v2/queue')
            views = queue['services']['services'].get('views') or {}
            return queue if views.get('state') == 'running' and views.get('adopted') else None
        queue = wait_for(adopted, what='adoption through the API')
        self.assertEqual(queue['services']['services']['views']['pid'], hand.pid)
        self.assertTrue(queue['services']['managed'])
        status, state = self.call('GET', '/api/v2/state')
        self.assertEqual(state['status']['services'], {'ok': True, 'problems': []})
        status, body = self.call('POST', '/api/v2/queue', {'op': 'service', 'name': 'views', 'action': 'stop'})
        self.assertEqual((status, body['ok'], body['pending']), (200, True, True))
        wait_for(lambda: not self.running('views'), what='views to stop')

        def outcome():
            _s, queue = self.call('GET', '/api/v2/queue')
            return next((e for e in queue['services']['events'] if e.get('request') == body['request']), None)
        event = wait_for(outcome, what='request outcome in the queue view')
        self.assertTrue(event['ok'])
        status, body = self.call('POST', '/api/v2/queue', {'op': 'service', 'name': 'views', 'action': 'explode'})
        self.assertEqual(status, 400)
        ops = [e['detail'] for e in taskqueue.for_root(self.root).events() if e['kind'] == 'op']
        self.assertTrue(any(d.startswith('service: stop views') for d in ops), ops)

    def test_settings_patch_wakes_the_manager_and_hosts_follow(self):
        settings.save(self.root, ALL_OFF)
        self.start_server(True)
        wait_for(lambda: self.server.services.ticks >= 1, what='first tick')
        ticks = self.server.services.ticks
        status, body = self.call('POST', '/api/v2/queue', {'op': 'settings', 'patch': {'allowed_hosts': ['box.example.test']}})
        self.assertEqual(status, 200)
        wait_for(lambda: self.server.services.ticks > ticks, what='a tick after the settings change')
        self.assertIn('box.example.test', self.server.ctx['hosts'])


# ---------------------------------------------------------------------------------------------------------- convert list / ops

class ConvertListOnApproveTest(V2Case):
    def lines(self):
        path = self.root / '.codex_jobs/_batch/convert_sets.txt'
        return path.read_text(encoding='utf-8').splitlines() if path.exists() else []

    def test_direct_and_provisional_approve_add_the_set(self):
        self.concept('one')
        status, result = self.act('concepts:one', 'approve')
        self.assertEqual(status, 200, result)
        self.assertTrue((self.root / 'approved/one').is_dir())
        self.assertEqual(result['convert_list']['added'], ['one', 'one_*'])
        self.assertEqual(self.lines(), ['one', 'one_*'])
        self.concept('two', 'provisional')
        self.assertEqual(self.act('provisional:two', 'approve')[0], 200)
        self.assertEqual(self.lines(), ['one', 'one_*', 'two', 'two_*'])
        self.concept('three')
        self.assertEqual(self.act('concepts:three', 'provisional')[0], 200)                 # the first hop is no processing request
        self.assertNotIn('three', self.lines())
        # approving again after an unapprove + approve keeps one line per pattern
        self.assertEqual(self.act('approved:one', 'unapprove', args={'to': 'concepts'})[0], 200)
        status, result = self.act('concepts:one', 'approve')
        self.assertEqual((status, result['convert_list']['added'], result['convert_list']['present']), (200, [], ['one', 'one_*']))
        self.assertEqual(self.lines().count('one'), 1)

    def test_legacy_promote_adds_the_promoted_set(self):
        batch = self.root / 'concepts/batch'
        batch.mkdir(exist_ok=True)
        (batch / '01_plate__outfit.png').write_bytes(b'png')
        (batch / '01_plate.json').write_text('{"race_gender": "human_male"}')
        status, result = self.act('legacy:batch:01_plate', 'approve')
        self.assertEqual(status, 200, result)
        self.assertEqual(self.lines(), ['batch_01_plate', 'batch_01_plate_*'])

    def test_a_failing_convert_list_is_a_warning_and_the_move_stands(self):
        self.concept('one')
        with mock.patch.object(convert_list, 'add_sets', side_effect=OSError('disk full')):
            status, result = self.act('concepts:one', 'approve')
        self.assertEqual(status, 200)
        self.assertTrue((self.root / 'approved/one').is_dir())
        self.assertIn('convert list not updated', result['warning'])
        self.assertIn('disk full', result['warning'])
        self.assertIsNone(result['convert_list'])
        self.assertEqual(result['undo'], {'verb': 'unapprove', 'id': 'approved:one', 'args': {'to': 'concepts'}})

    def test_an_exclude_is_reported(self):
        path = self.root / '.codex_jobs/_batch/convert_sets.txt'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('!one\n', encoding='utf-8')
        self.concept('one')
        status, result = self.act('concepts:one', 'approve')
        self.assertEqual(status, 200)
        self.assertIn('excluded by !one', result['warning'])
        self.assertEqual(self.lines(), ['!one', 'one', 'one_*'])


class QueueOpsTest(V2Case):
    def op(self, **body):
        return self.post('/api/v2/queue', body)

    def batch(self):
        path = self.root / '.codex_jobs' / '_batch'
        for sub in ('tasks', 'status', 'convert_locks'):
            (path / sub).mkdir(parents=True, exist_ok=True)
        return path

    def test_mode(self):
        for mode, written in (('auto', 'views-run'), ('pause-conversions', 'pause-conversions'), ('pause', 'pause'), ('run', 'run')):
            status, body = self.op(op='mode', mode=mode)
            self.assertEqual((status, body['mode']['mode']), (200, written))
            raw = json.loads((self.root / '.codex_jobs/_batch/supervisor_mode.json').read_text(encoding='utf-8'))
            self.assertEqual(raw['mode'], written)
        self.assertEqual(self.op(op='mode', mode='turbo')[0], 400)
        self.assertEqual(self.op(op='nope')[0], 400)
        status, state = self.get('/api/v2/state')
        self.assertEqual(state['status']['mode']['mode'], 'run')

    def test_settings_and_its_problems(self):
        status, body = self.op(op='settings', patch={'view_parallel': 4, 'services': {'convert': False}})
        self.assertEqual((status, body['settings']['view_parallel'], body['settings']['services']['convert']), (200, 4, False))
        status, body = self.op(op='settings', patch={'view_parallel': 0, 'tripo': 'maybe'})
        self.assertEqual(status, 400)
        self.assertEqual(len(body['problems']), 2)
        self.assertEqual(settings.load(self.root)['view_parallel'], 4)                       # nothing written by the bad patch
        status, queue = self.get('/api/v2/queue')
        self.assertEqual(queue['settings']['view_parallel'], 4)

    def test_release_of_a_held_job(self):
        self.job('r', 'a1b2c3d4e5f6', 'multiview_ready', state='ready_for_tripo')
        _s, queue = self.get('/api/v2/queue')
        self.assertEqual([(h['job'], h['reason']) for h in queue['conversions']['held']], [('r--a1b2c3d4e5f6', 'unlisted')])
        unit, item = self.unit('r')
        self.assertEqual((unit['progress']['held_kind'], unit['progress']['held_reason']), ('convert_list', 'not in convert list'))
        self.assertIn('not in convert list', [b['text'] for b in item['card']['badges']])
        status, body = self.op(op='release', set='r')
        self.assertEqual((status, body['added']), (200, ['r', 'r_*']))
        _s, queue = self.get('/api/v2/queue')
        self.assertEqual(queue['conversions']['held'], [])
        self.assertIsNone(self.unit('r')[0]['progress']['held_kind'])
        self.assertEqual(self.op(op='release', set='unknown_set')[0], 404)
        self.assertEqual(self.op(op='release', set='Bad Name')[0], 400)

    def test_view_retry(self):
        batch = self.batch()
        name = 'views--s--human_male--outfit'
        (batch / 'tasks' / f'{name}.json').write_text(json.dumps({'name': name, 'outputs': []}))
        jobs = [{'job': f'{name}-a01', 'started': '2026-11-25T10:00:00', 'exit': '1'}]
        (batch / 'status' / f'{name}.json').write_text(json.dumps({'state': 'running', 'attempts': 1, 'jobs': jobs}))
        status, body = self.op(op='view_retry', name=name)
        self.assertEqual(status, 400)
        self.assertIn('only a failed task', body['error'])
        (batch / 'status' / f'{name}.json').write_text(json.dumps({'state': 'failed', 'attempts': 4, 'jobs': jobs, 'checks': {}}))
        status, body = self.op(op='view_retry', name=name)
        self.assertEqual((status, body['attempts_before'], body['jobs']), (200, 4, 1))
        raw = json.loads((batch / 'status' / f'{name}.json').read_text(encoding='utf-8'))
        self.assertEqual((raw['state'], raw['attempts'], raw['jobs']), ('pending', 0, jobs))
        self.assertEqual(self.op(op='view_retry', name='views--s--elf--outfit')[0], 404)
        self.assertEqual(self.op(op='view_retry', name='../x')[0], 400)

    def test_convert_reset(self):
        batch = self.batch()
        (batch / 'convert_locks' / 's--a1b2c3d4e5f6.fails').write_text('3')
        _s, queue = self.get('/api/v2/queue')
        self.assertEqual(queue['conversions']['fails'], [{'job': 's--a1b2c3d4e5f6', 'count': 3, 'stuck': True}])
        status, body = self.op(op='convert_reset', job='s--a1b2c3d4e5f6')
        self.assertEqual((status, body['count_before']), (200, 3))
        self.assertFalse((batch / 'convert_locks' / 's--a1b2c3d4e5f6.fails').exists())
        self.assertEqual(self.op(op='convert_reset', job='s--a1b2c3d4e5f6')[0], 404)
        self.assertEqual(self.op(op='convert_reset', job='../../x')[0], 400)
        details = [e['detail'] for e in taskqueue.for_root(self.root).events() if e['kind'] == 'op']
        self.assertTrue(any(d.startswith('convert_reset: s--a1b2c3d4e5f6') for d in details), details)

    def test_check_tripo_on_a_worker_thread_and_busy(self):
        checker = self.server.ctx['tripo']
        checker.busy = lambda: [{'pid': 4242}]
        status, body = self.op(op='check_tripo')
        self.assertEqual((status, body['status']), (200, 'busy'))                            # credits.check_tripo's shape
        self.assertIn('convert_job.py is running', body['message'])
        self.assertIsNone(checker.thread)
        gate = threading.Event()

        def run(root):
            gate.wait(10)
            return 0, 'checking\n{"credits": 120}\n', ''
        checker.busy, checker.run = (lambda: []), run
        status, body = self.op(op='check_tripo')
        self.assertEqual((status, body['status'], body['pending']), (200, 'running', True))
        self.assertEqual(self.op(op='check_tripo')[1]['status'], 'busy')                     # one check at a time
        gate.set()
        wait_for(lambda: checker.view()['last'], what='the balance check')
        _s, queue = self.get('/api/v2/queue')
        self.assertEqual(queue['credits']['check']['last'], {'status': 'ok', 'credits': 120, 'checked_at': queue['credits']['check']['last']['checked_at']})
        self.assertEqual(queue['credits']['tripo']['credits'], 120)

    def test_queue_keys(self):
        _s, queue = self.get('/api/v2/queue')
        for key in ('tasks', 'workers', 'services', 'mode', 'credits', 'disk', 'views', 'conversions', 'holds', 'settings'):
            self.assertIn(key, queue)
        self.assertGreater(queue['disk']['free'], 0)
        self.assertEqual(set(queue['mode']), {'mode', 'pause', 'idle_seconds', 'idle_source', 'away', 'supervisor_running', 'supervisor_pid', 'warning'})
        self.assertEqual(set(queue['conversions']), {'current', 'events_tail', 'fails', 'held', 'excludes', 'max_tries'})
        self.assertEqual(queue['settings'], settings.DEFAULTS)


# ------------------------------------------------------------------------------------------------------------- unit progress

class ProgressTest(V2Case):
    def batch(self):
        path = self.root / '.codex_jobs' / '_batch'
        for sub in ('tasks', 'status', 'convert_locks'):
            (path / sub).mkdir(parents=True, exist_ok=True)
        return path

    def task(self, name, outputs, state=None, attempts=0, job=None):
        batch = self.batch()
        (batch / 'tasks' / f'{name}.json').write_text(json.dumps({'name': name, 'outputs': outputs, 'prompt': 'x'}))
        if state:
            jobs = [{'job': job or f'{name}-a01', 'started': '2026-11-25T10:00:00'}]
            tmp = batch / 'status' / f'{name}.tmp'
            tmp.write_text(json.dumps({'state': state, 'attempts': attempts, 'jobs': jobs}))
            os.replace(tmp, batch / 'status' / f'{name}.json')

    def outputs(self, folder, race='human_male', module='outfit', views=progress.VIEWS):
        return [str(folder / race / module / f'{v}.png') for v in views]

    def test_views_from_the_task_status(self):
        folder = self.job('s', 'a1b2c3d4e5f6', 'processing_views')                          # front.png exists
        unit, item = self.unit('s')
        self.assertEqual((unit['progress']['views_done'], unit['progress']['views_total'], unit['progress']['views_state']), (1, 4, None))
        name = 'views--s--human_male--outfit'
        self.task(name, self.outputs(folder), 'running')
        unit, item = self.unit('s')
        self.assertEqual((unit['progress']['views_done'], unit['progress']['views_total']), (1, 4))
        self.assertEqual((unit['progress']['views_state'], unit['progress']['views_task']), ('running', name))
        (folder / 'human_male/outfit/back.png').write_bytes(b'png')
        unit, item = self.unit('s')
        self.assertEqual(unit['progress']['views_done'], 2)
        self.assertIn('views 0/1', [b['text'] for b in item['card']['badges']])
        self.assertEqual(item['card']['progress'], {'human_male': {'outfit': {'status': 'views', 'views_done': 2, 'views_total': 4, 'held_reason': None,
                                                                              'converting_step': None, 'error': None}}})
        self.task(name, self.outputs(folder), 'done')
        unit, item = self.unit('s')
        self.assertEqual((unit['progress']['views_done'], unit['progress']['views_total']), (4, 4))
        self.assertNotIn('views 0/1', [b['text'] for b in item['card']['badges']])

    def test_legacy_three_view_task_and_failed_views(self):
        folder = self.job('s', 'a1b2c3d4e5f6', 'processing_views')
        name = 'views--s--human_male--outfit'
        self.task(name, self.outputs(folder, views=progress.VIEWS[1:]), 'failed', attempts=4)
        unit, item = self.unit('s')
        self.assertEqual((unit['progress']['views_done'], unit['progress']['views_total']), (1, 4))
        self.assertIn('views task failed after 4 attempt(s)', unit['progress']['error'])
        self.assertIn({'text': 'failed 1', 'tone': 'error'}, item['card']['badges'])
        _s, queue = self.get('/api/v2/queue')
        self.assertEqual([f['name'] for f in queue['views']['failed']], [name])
        self.assertEqual(queue['views']['counts'], {'failed': 1})

    def test_suffixed_retry_task_and_job_prefix(self):
        folder = self.job('s', 'a1b2c3d4e5f6', 'processing_views', units=(('dwarf_male', 'outfit'),), suffix='_dwarf_male')
        self.task('views--s_dwarf_male--dwarf_male--outfit', self.outputs(folder, 'dwarf_male'), 'failed', attempts=4)
        time.sleep(0.05)
        self.task('views--s_dwarf_male--dwarf_male--outfit-r2', self.outputs(folder, 'dwarf_male'), 'running')
        unit, _item = self.unit('s', 'dwarf_male')
        self.assertEqual((unit['progress']['views_task'], unit['progress']['views_state']), ('views--s_dwarf_male--dwarf_male--outfit-r2', 'running'))
        self.assertIsNone(unit['progress']['error'])

    def test_fronts_of_an_unclaimed_race(self):
        self.job('s', 'a1b2c3d4e5f6', 'processing_views')
        self.fronts('s', 'dwarf_male', 'running')
        unit, _item = self.unit('s', 'dwarf_male')
        self.assertEqual(unit['status'], 'missing')
        self.assertEqual((unit['progress']['fronts_state'], unit['progress']['views_done'], unit['progress']['views_total']), ('running', 0, 4))

    def test_supervisor_holds(self):
        self.job('s', 'a1b2c3d4e5f6', 'processing_views', units=(('human_male', 'outfit'), ('dwarf_male', 'outfit')))
        holds = {'s--a1b2c3d4e5f6': {'sig': 'x', 'reasons': ['dwarf_male/outfit: framing back.png: - SIDE: anchor white painted']}}
        self.batch()
        (self.root / '.codex_jobs/_batch/supervisor_state.json').write_text(json.dumps({'holds': holds, 'sets': {}}))
        human, item = self.unit('s')
        dwarf, _item = self.unit('s', 'dwarf_male')
        self.assertIsNone(human['progress']['held_kind'])
        self.assertEqual(dwarf['progress']['held_kind'], 'supervisor')
        self.assertIn('SIDE', dwarf['progress']['held_reason'])
        self.assertIn({'text': 'held 1', 'tone': 'warn'}, item['card']['badges'])
        holds['s--a1b2c3d4e5f6']['reasons'] = ['views task failed: s--dwarf_male--outfit', 'workflow.py ready failed: boom']
        (self.root / '.codex_jobs/_batch/supervisor_state.json').write_text(json.dumps({'holds': holds, 'sets': {}}))
        human, _item = self.unit('s')
        dwarf, _item = self.unit('s', 'dwarf_male')
        self.assertEqual(human['progress']['held_reason'], 'workflow.py ready failed: boom')            # a job-wide reason
        self.assertIn('views task failed', dwarf['progress']['held_reason'])
        _s, queue = self.get('/api/v2/queue')
        self.assertEqual(queue['holds'][0] | {'reasons': None}, {'key': 's--a1b2c3d4e5f6', 'reasons': None, 'set': 's', 'job': 's--a1b2c3d4e5f6',
                                                                  'gnome': False, 'drift': False})

    def test_set_level_hold_of_an_unclaimed_race(self):
        self.job('s', 'a1b2c3d4e5f6', 'processing_views')
        self.fronts('s', 'dwarf_male', 'done')
        self.batch()
        (self.root / '.codex_jobs/_batch/supervisor_state.json').write_text(json.dumps({'holds': {'s': {'reasons': ['dwarf_male: no concepts folder']}}}))
        dwarf, _item = self.unit('s', 'dwarf_male')
        self.assertEqual((dwarf['progress']['held_kind'], dwarf['progress']['held_reason']), ('supervisor', 'dwarf_male: no concepts folder'))
        self.assertIsNone(self.unit('s')[0]['progress']['held_kind'])                       # the claimed race is not held by gate 1

    def test_excluded_and_held_conversions(self):
        self.job('s', 'a1b2c3d4e5f6', 'multiview_ready', state='ready_for_tripo')
        self.batch()
        (self.root / '.codex_jobs/_batch/convert_sets.txt').write_text('s\n!s\n')
        unit, item = self.unit('s')
        self.assertEqual((unit['progress']['held_kind'], unit['progress']['held_reason']), ('excluded', 'excluded'))
        self.assertIn('!s', unit['progress']['held_detail'])
        self.assertIn('excluded from convert list', [b['text'] for b in item['card']['badges']])
        self.job('h', 'a1b2c3d4e5f6', 'held_conversions')
        unit, item = self.unit('h')
        self.assertEqual(unit['progress']['held_kind'], 'held_conversions')
        self.assertIn({'text': 'held 1', 'tone': 'warn'}, item['card']['badges'])

    def test_converting_step_and_fails(self):
        self.job('s', 'a1b2c3d4e5f6', 'processing', state='ready_for_tripo')
        (self.root / '.codex_jobs/_batch/convert_sets.txt').parent.mkdir(parents=True, exist_ok=True)
        (self.root / '.codex_jobs/_batch/convert_sets.txt').write_text('s\n')
        unit, item = self.unit('s')
        self.assertEqual((unit['status'], unit['progress']['converting_step'], unit['progress']['held_kind']), ('converting', None, None))
        self.assertIn('converting 1/1', [b['text'] for b in item['card']['badges']])
        batch = self.batch()
        me = str(os.getpid())                                                                 # a live holder
        (batch / 'convert_locks' / 's--a1b2c3d4e5f6.lock').write_text(me)
        module = self.root / 'models/s/a1b2c3d4e5f6/human_male/outfit'
        (module.parent.parent / '.module_locks').mkdir(parents=True)
        (module.parent.parent / '.module_locks' / 'human_male__outfit.lock').write_text(me)
        unit, _item = self.unit('s')
        self.assertEqual((unit['progress']['converting_step'], unit['progress']['converting_pid']), ('submitting', os.getpid()))
        module.mkdir(parents=True, exist_ok=True)
        (module / 'studio_submission.json').write_text(json.dumps({'backend': 'hyper3d', 'status': 'submitted', 'stage': 'geometry_preview'}))
        self.assertEqual(self.unit('s')[0]['progress']['converting_step'], 'hyper3d: geometry_preview')
        (module / 'reduced').mkdir()
        self.assertEqual(self.unit('s')[0]['progress']['converting_step'], 'reducing')
        _s, queue = self.get('/api/v2/queue')
        self.assertEqual(queue['conversions']['current'], [{'job': 's--a1b2c3d4e5f6', 'module': 'human_male/outfit', 'holder_pid': os.getpid(),
                                                            'step': 'reducing'}])
        (batch / 'convert_locks' / 's--a1b2c3d4e5f6.fails').write_text('1')
        unit, _item = self.unit('s')
        self.assertEqual((unit['progress']['convert_fails'], unit['progress']['error']), (1, None))           # the worker tries again
        (batch / 'convert_locks' / 's--a1b2c3d4e5f6.fails').write_text('3')
        unit, item = self.unit('s')
        self.assertIn('failed 3 times', unit['progress']['error'])
        self.assertIn({'text': 'failed 1', 'tone': 'error'}, item['card']['badges'])

    def test_reduced_and_dropped_units(self):
        self.full_set()
        unit, _item = self.unit('s')
        self.assertEqual((unit['progress']['views_done'], unit['progress']['views_total']), (1, 1))      # the views the job kept
        self.assertIsNone(unit['progress']['error'])

    def test_state_status_summary(self):
        _s, state = self.get('/api/v2/state')
        self.assertEqual(state['status'], {'mode': {'mode': 'auto', 'since': None, 'label': state['status']['mode']['label']}, 'paused': False,
                                           'services': None, 'queue_health': None, 'pause_reason': None, 'services_problems': None})
        self.batch()
        (self.root / '.codex_jobs/_batch/PAUSE').write_text(json.dumps({'since': 'x', 'reason': 'manual: pause-conversions', 'scope': ['conversions']}))
        self.assertEqual(self.post('/api/v2/queue', {'op': 'mode', 'mode': 'pause-conversions'})[0], 200)
        _s, state = self.get('/api/v2/state')
        self.assertEqual((state['status']['mode']['mode'], state['status']['mode']['label'], state['status']['paused']),
                         ('pause-conversions', 'Pause conversions', {'since': 'x', 'reason': 'manual: pause-conversions', 'scope': ['conversions']}))

    def test_settings_problems_and_release_shapes(self):
        status, body = self.post('/api/v2/queue', {'op': 'settings', 'patch': {'view_parallel': 99}})
        self.assertEqual((status, set(body)), (400, {'ok', 'error', 'problems'}))
        status, body = self.post('/api/v2/queue', {'op': 'service', 'name': 'views', 'action': 'start'})
        self.assertEqual((status, body['code']), (409, 'services_off'))
        self.assertIn('service management is off', body['error'])

    def test_task_index_is_cached_by_directory_mtime(self):
        self.task('views--s--human_male--outfit', [], 'running')
        first = progress.task_index(self.root)
        self.assertIs(progress.task_index(self.root), first)
        self.task('views--s--human_male--outfit', [], 'done')                                # atomic replace: the folder mtime moves
        second = progress.task_index(self.root)
        self.assertEqual(second['status']['views--s--human_male--outfit']['state'], 'done')
        self.assertIsNot(second, first)

    def test_hold_reason_scopes(self):
        races = ['human_male', 'dwarf_male']
        self.assertEqual(progress.reason_scope('dwarf_male/outfit: framing x', races), [('dwarf_male', 'outfit')])
        self.assertEqual(progress.reason_scope('dwarf_male: front.png colour DRIFT (primary)', races), [('dwarf_male', None)])
        self.assertEqual(progress.reason_scope('fronts task failed: dwarf_male, human_male', races), [('dwarf_male', None), ('human_male', None)])
        self.assertEqual(progress.reason_scope('views task failed: s--human_male--outfit', races), [('human_male', 'outfit')])
        self.assertEqual(progress.reason_scope('workflow.py ready failed: x', races), [])

    def test_constants_match_the_convert_worker(self):
        import convert_queue
        self.assertEqual(progress.CONVERT_MAX_TRIES, convert_queue.MAX_TRIES)


class SettingsReaderTest(V2Case):
    def test_the_orchestrator_and_the_builds_read_through_settings_load(self):
        path = self.root / '.codex_jobs/_app/settings.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'layer_workers_present': 2, 'layer_workers_away': 20, 'build_stall_minutes': 7}))
        self.assertEqual(orchestrator.read_settings(self.root), {'layer_workers_present': 2, 'layer_workers_away': 4, 'build_stall_minutes': 7})
        self.assertEqual(builds.stall_minutes(self.root), 7)
        with mock.patch.object(settings, 'load', wraps=settings.load) as load:
            orchestrator.pool_status(self.root, 999.0, None)
            builds.stall_minutes(self.root)
        self.assertEqual(load.call_count, 2)


if __name__ == '__main__':
    unittest.main()
