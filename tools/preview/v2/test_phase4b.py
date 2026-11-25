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


def fam(set_id):
    """The exact convert-list ids of a set in the V2Case fixture (races dwarf_male, human_male; DESIGN_PHASE4.md I.6)."""
    return [set_id, f'{set_id}_dwarf_male', f'{set_id}_human_male']


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
        self.assertEqual(state['status']['services'], {'ok': True, 'problems': [], 'managed': True, 'scanned': True})
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
        self.assertEqual(result['convert_list']['added'], fam('one'))                     # exact ids only (DESIGN_PHASE4.md I.6)
        self.assertEqual(self.lines(), fam('one'))
        self.concept('two', 'provisional')
        self.assertEqual(self.act('provisional:two', 'approve')[0], 200)
        self.assertEqual(self.lines(), fam('one') + fam('two'))
        self.concept('three')
        self.assertEqual(self.act('concepts:three', 'provisional')[0], 200)                 # the first hop is no processing request
        self.assertNotIn('three', self.lines())
        # approving again after an unapprove + approve keeps one line per pattern
        self.assertEqual(self.act('approved:one', 'unapprove', args={'to': 'concepts'})[0], 200)
        status, result = self.act('concepts:one', 'approve')
        self.assertEqual((status, result['convert_list']['added'], result['convert_list']['present']), (200, [], fam('one')))
        self.assertEqual(self.lines().count('one'), 1)

    def test_legacy_promote_adds_the_promoted_set(self):
        batch = self.root / 'concepts/batch'
        batch.mkdir(exist_ok=True)
        (batch / '01_plate__outfit.png').write_bytes(b'png')
        (batch / '01_plate.json').write_text('{"race_gender": "human_male"}')
        status, result = self.act('legacy:batch:01_plate', 'approve')
        self.assertEqual(status, 200, result)
        self.assertEqual(self.lines(), fam('batch_01_plate'))

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
        self.assertEqual(self.lines(), ['!one'] + fam('one'))


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
        self.assertEqual((status, body['added']), (200, fam('r')))
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
        self.assertEqual(set(queue['mode']) - {'pause_id'},
                         {'mode', 'pause', 'idle_seconds', 'idle_source', 'away', 'supervisor_running', 'supervisor_pid', 'warning'})
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
                                                                  'gnome': False, 'drift': False, 'images': []})

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
        status = {k: v for k, v in state['status'].items() if k != 'pause_id'}       # (pause_id: the pause identity, review finding 5)
        status['mode'] = {k: v for k, v in status['mode'].items() if k != 'pause_id'}
        self.assertEqual(status, {'mode': {'mode': 'auto', 'since': None, 'label': state['status']['mode']['label']}, 'paused': False,
                                  'services': None, 'queue_health': None, 'pause_reason': None, 'services_problems': None})
        self.batch()
        (self.root / '.codex_jobs/_batch/PAUSE').write_text(json.dumps({'since': 'x', 'reason': 'manual: pause-conversions', 'scope': ['conversions']}))
        self.assertEqual(self.post('/api/v2/queue', {'op': 'mode', 'mode': 'pause-conversions'})[0], 200)
        _s, state = self.get('/api/v2/state')
        paused = {k: v for k, v in state['status']['paused'].items() if k != 'pause_id'}
        self.assertEqual((state['status']['mode']['mode'], state['status']['mode']['label'], paused),
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


# ======================================================================================= phase 4 review (review_v2_phase4_20261125)

import hashlib  # noqa: E402
import subprocess  # noqa: E402

import guard  # noqa: E402
import psutil  # noqa: E402
from test_phase4 import CONVERT_ONLY, VIEWS_ONLY  # noqa: E402

LOCK_HOLDER = '''import sys, time
sys.path.insert(0, sys.argv[2])
from claim_lock import try_lock
handle = open(sys.argv[1], 'a+b')
assert try_lock(handle)
print('locked', flush=True)
time.sleep(120)
'''


class ReviewServicesTest(TempRoot):
    """Findings 2, 5, 6, 8, 16, 20 against the fake services of a temp root."""

    def popen(self, *args):
        proc = subprocess.Popen([sys.executable, *args], cwd=str(self.root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.popens.append(proc)
        return proc

    def lock_holder(self, path):
        proc = subprocess.Popen([sys.executable, '-c', LOCK_HOLDER, str(path), str(TOOLS / 'tripo_pipeline')], stdout=subprocess.PIPE, text=True)
        self.popens.append(proc)
        self.assertEqual(proc.stdout.readline().strip(), 'locked')
        return proc

    def hold(self):
        return services.read_hold(self.root)

    def sup(self):
        found = self.running('supervisor')
        return found[0] if found else None

    # ---------------------------------------------------------------------------------------------------------------- F5
    def test_f5_command_lines_are_parsed_like_the_interpreter_and_the_daemon(self):
        root, py = str(self.root), sys.executable
        sup, views, conv = (services.SERVICES[n] for n in ('supervisor', 'views', 'convert'))
        cases = [
            (views, [py, '-c', 'import time', 'tools/view_batch.py', 'run'], None),          # the reviewer's unrelated driver
            (views, [py, 'driver.py', 'tools/view_batch.py', 'run'], None),
            (views, [py, '-m', 'view_batch', 'run'], None),
            (views, [py, '-u', 'tools/view_batch.py', 'run'], 'match'),
            (views, [py, '-X', 'utf8', '-Wignore', 'tools/view_batch.py', 'run', '--parallel', '8'], 'match'),
            (views, [py, 'tools/view_batch.py', 'status'], None),
            (views, [py, 'tools/view_batch.py', 'run', '--help'], None),
            (conv, [py, 'tools/convert_queue.py', '--worker=2'], None),                    # the reviewer's worker 2
            (conv, [py, 'tools/convert_queue.py', '--worker', '2'], None),
            (conv, [py, 'tools/convert_queue.py', '--work', '2'], None),                   # an argparse prefix
            (conv, [py, 'tools/convert_queue.py', '--worker=1'], 'match'),
            (conv, [py, 'tools/convert_queue.py', '--sets-file', 'x'], 'match'),          # default worker 1
            (conv, [py, 'tools/convert_queue.py', '--worker', '2', '--worker', '1'], 'match'),   # the last one wins
            (sup, [py, 'tools/pipeline_supervisor.py', 'run'], 'match'),
            (sup, [py, 'tools/pipeline_supervisor.py', '--root', str(Path(root).parent / 'elsewhere'), 'run'], None),   # another root
            (sup, [py, 'tools/pipeline_supervisor.py', '--root', root, 'run'], 'match'),
            (sup, [py, 'tools/pipeline_supervisor.py', '--root=.', 'run'], 'match'),
            (sup, [py, 'tools/pipeline_supervisor.py', 'run', '--dry-run'], None),         # one pass, not the daemon
            (sup, [py, 'tools/pipeline_supervisor.py', 'stop'], None),
        ]
        for svc, cmd, want in cases:
            with self.subTest(cmd=cmd[1:]):
                self.assertEqual(services.identify(root, svc, cmd, root), want)
        self.assertEqual(services.identify(root, views, [py, 'tools/view_batch.py', 'run'], None), 'unverified')     # cwd unknown
        self.assertEqual(services.identify(root, views, [py, str(self.root / 'tools/view_batch.py'), 'run'], None), 'match')
        self.assertIsNone(services.identify(root, views, [py, str(Path(root).parent / 'x' / 'tools/view_batch.py'), 'run'], None))
        long_cmd = [py, '-u'] + ['-X', 'utf8'] * 6 + ['tools/view_batch.py', 'run']    # no bounded token scan
        self.assertEqual(services.identify(root, views, long_cmd, root), 'match')

    def test_f5_the_reviewers_processes_are_never_adopted(self):
        other = Path(tempfile.mkdtemp(prefix='v2p4r_other_')).resolve()
        try:
            self.popen('tools/convert_queue.py', '--sets-file', 'x', '--worker=2')
            self.popen('tools/pipeline_supervisor.py', '--root', str(other), 'run')
            self.popen('-c', 'import time; time.sleep(60)', str(self.root / 'tools/view_batch.py'), 'run')
            time.sleep(1.0)
            found, maybe = services.scan_full(self.root)
            self.assertEqual({k: v for k, v in found.items() if v}, {})
            self.assertEqual({k: v for k, v in maybe.items() if v}, {})
            w1 = self.popen('tools/convert_queue.py', '--worker=1')
            self.wait_for(lambda: self.running('convert'), what='worker 1 spelled --worker=1')
            self.assertEqual([i['pid'] for i in self.running('convert')], [w1.pid])
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_f5_an_unverified_lookalike_blocks_every_start(self):
        settings.save(self.root, VIEWS_ONLY)
        ghost = {'pid': 4242, 'ppid': 0, 'create_time': 1.0, 'cmdline': [sys.executable, 'tools/view_batch.py', 'run'], 'cwd': None}
        real = services.scan_full

        def scan_full(root, names=None):
            found, maybe = real(root, names)
            if 'views' in maybe:
                maybe['views'] = maybe['views'] + [ghost]
            return found, maybe
        with mock.patch.object(services, 'scan_full', side_effect=scan_full):
            events = services.tick(self.root)['events']
            self.assertTrue(any('could not be verified' in e for e in events), events)
            with self.assertRaisesRegex(services.ServiceError, 'could not be verified'):
                services.start(self.root, 'views')
        self.assertEqual(self.running('views'), [])
        status = services.status(self.root)['services']['views']
        self.assertEqual(status['state'], 'waiting')
        self.assertTrue(any(p.startswith('views: not started') for p in progress.problems(services.status(self.root)['services'])))

    def test_f5_a_held_view_runner_lock_blocks_a_second_runner(self):
        settings.save(self.root, VIEWS_ONLY)
        holder = self.lock_holder(self.root / '.codex_jobs/_batch/view_runner.lock')
        events = services.tick(self.root)['events']
        self.assertTrue(any('view_runner.lock is held' in e for e in events), events)
        self.assertEqual(self.running('views'), [])
        holder.kill()
        holder.wait(10)
        self.assertTrue(any(e.startswith('views: started') for e in services.tick(self.root)['events']))

    def test_f5_a_signal_goes_only_to_a_rechecked_handle(self):
        sleeper = self.popen('-c', 'import time; time.sleep(60)')
        inst = {'pid': sleeper.pid, 'create_time': psutil.Process(sleeper.pid).create_time(), 'cmdline': ['x']}
        with self.assertRaisesRegex(services.ServiceError, 'no longer the views service'):
            services._terminate(self.root, services.SERVICES['views'], inst)          # same pid + create time, but not the view runner
        self.assertIsNone(sleeper.poll())

    def test_f5_the_server_keeps_its_writer_lock_while_the_services_thread_runs(self):
        settings.save(self.root, {'services': {'supervisor': False, 'views': False, 'convert': False}})
        web = Path(tempfile.mkdtemp(prefix='v2p4r_web_'))
        (web / 'index.html').write_text('<html><head></head></html>')
        try:
            srv = server.create_server(self.root, port=0, web=web, extra_hosts=[], manage_services=True, services_interval=3600.0)
            manager, lock = srv.services, srv.lock
            with mock.patch.object(manager, 'stop', return_value=False):          # a stop request is still running
                srv.server_close()
            probe = guard.ServerLock(self.root)
            self.assertFalse(probe.acquire())                                     # no second writer (and manager) can start meanwhile
            self.assertTrue(manager.stop())
            lock.release()
            self.assertTrue(probe.acquire())
            probe.release()
        finally:
            shutil.rmtree(web, ignore_errors=True)

    # ---------------------------------------------------------------------------------------------------------------- F2
    def test_f2_manager_start_holds_conversions_until_the_supervisor_ticked(self):
        settings.save(self.root, {'services': {'supervisor': True, 'views': False, 'convert': True}})
        services.begin_management(self.root)
        hold = self.hold()
        self.assertEqual(hold['owner_pid'], os.getpid())
        self.assertIn('manager start', hold['reason'])
        events = services.tick(self.root)['events']
        self.assertTrue(any(e.startswith('supervisor: started') for e in events), events)
        self.assertTrue(any(e.startswith('convert: waiting for the supervisor') for e in events), events)   # start order (F2)
        self.assertEqual(self.running('convert'), [])
        self.assertTrue(self.hold())
        self.wait_for(lambda: services.supervisor_ticked(self.root, self.sup(), hold['since_unix']), what='a supervisor tick')
        events = services.tick(self.root)['events']
        self.assertTrue(any('conversion hold released' in e for e in events), events)
        self.assertTrue(any(e.startswith('convert: started') for e in events), events)
        self.assertIsNone(self.hold())

    def test_f2_stopping_the_supervisor_writes_the_hold_first_and_keeps_it(self):
        settings.save(self.root, {'services': {'supervisor': True, 'views': False, 'convert': False}})
        self.spawn('supervisor')
        services.tick(self.root)
        self.assertIsNone(self.hold())
        seen = []
        real = services.subprocess.run

        def run(cmd, **kwargs):
            seen.append(self.hold())                                               # the hold exists when `stop` is sent
            return real(cmd, **kwargs)
        with mock.patch.object(services.subprocess, 'run', side_effect=run):
            services.stop(self.root, 'supervisor', wait=10)
        self.assertTrue(seen and seen[0] and 'supervisor stop' in seen[0]['reason'])
        for _ in range(2):
            services.tick(self.root)                                                # the supervisor stays stopped: the hold stays
        self.assertTrue(self.hold())
        self.assertEqual(services.status(self.root)['hold']['owner'], 'this server')
        services.start(self.root, 'supervisor')
        self.wait_for(lambda: 'conversion hold released' in ' '.join(services.tick(self.root)['events']), what='the new supervisor tick')
        self.assertIsNone(self.hold())

    def test_f2_restart_keeps_a_continuous_hold_across_the_gap(self):
        settings.save(self.root, {'services': {'supervisor': True, 'views': False, 'convert': False}})
        old = self.spawn('supervisor')
        services.tick(self.root)
        self.ctl('pipeline_supervisor', {'no_tick': True})                          # the new supervisor has not completed a tick yet
        services.restart(self.root, 'supervisor', wait=10)
        old.wait(10)
        hold = self.hold()
        self.assertIn('supervisor restart', hold['reason'])
        # the OLD supervisor's last tick (after the hold was written, on its way out) never releases the hold of the new one
        (self.root / '.codex_jobs/_batch/supervisor_tick.json').write_text(json.dumps({'pid': old.pid, 'time': time.time() + 60}))
        self.assertFalse(services.supervisor_ticked(self.root, self.sup(), hold['since_unix']))
        self.assertFalse(any('released' in e for e in services.tick(self.root)['events']))
        self.assertTrue(self.hold())
        self.ctl('pipeline_supervisor', {})
        self.wait_for(lambda: 'conversion hold released' in ' '.join(services.tick(self.root)['events']), what='the new supervisor tick')

    def test_f2_hold_ownership_rules(self):
        now = time.time()
        sup = {'pid': 999999, 'create_time': 5.0}
        batch = self.root / '.codex_jobs/_batch'
        (batch / 'supervisor_tick.json').write_text(json.dumps({'pid': 999999, 'create_time': 5.0, 'time': now + 5}))
        sleeper = self.popen('-c', 'import time; time.sleep(60)')
        live = {'since_unix': now - 3600, 'reason': 'by hand', 'owner_pid': sleeper.pid, 'owner_create_time': psutil.Process(sleeper.pid).create_time()}
        services.hold_path(self.root).write_text(json.dumps(live))
        self.assertEqual(services.begin_management(self.root), [])                # a live other owner: never touched
        self.assertIsNone(services.release_hold(self.root, sup))
        self.assertEqual(self.hold()['owner_pid'], sleeper.pid)
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait(10)
        services.hold_path(self.root).write_text(json.dumps({'since_unix': now - 30, 'reason': 'recent', 'owner_pid': dead.pid}))
        self.assertIsNone(services.assert_hold(self.root, 'tick'))                # a recent hold of an exited owner stays as it is
        self.assertIsNone(services.release_hold(self.root, sup))
        services.hold_path(self.root).write_text(json.dumps({'since_unix': now - 3600, 'reason': 'stale', 'owner_pid': dead.pid}))
        self.assertIn('stale', services.assert_hold(self.root, 'tick'))          # older than 10 min: replaced by ours
        self.assertEqual(self.hold()['owner_pid'], os.getpid())
        self.assertFalse(services.supervisor_ticked(self.root, {'pid': 1, 'create_time': 5.0}, 0))      # another pid
        self.assertFalse(services.supervisor_ticked(self.root, {'pid': 999999, 'create_time': 50.0}, 0))  # same pid, another process
        self.assertFalse(services.supervisor_ticked(self.root, sup, now + 10))  # a tick before the hold
        self.assertIn('released', services.release_hold(self.root, sup))
        self.assertIsNone(self.hold())

    # ---------------------------------------------------------------------------------------------------------------- F16
    def test_f16_an_unreadable_settings_file_never_starts_a_disabled_worker(self):
        settings.save(self.root, {'services': {'supervisor': False, 'views': False, 'convert': False}})
        settings.load(self.root)
        settings.path(self.root).write_text('{broken', encoding='utf-8')
        services.tick(self.root)
        self.assertEqual({k: v for k, v in services.scan(self.root).items() if v}, {})    # the last valid policy: everything off
        settings._LAST.clear()                                                    # a fresh server process with the broken file
        events = services.tick(self.root)['events']
        self.assertEqual({k: v for k, v in services.scan(self.root).items() if v}, {})
        self.assertTrue(any('not started' in e for e in events), events)

    # ---------------------------------------------------------------------------------------------------------------- F8
    def test_f8_check_tripo_is_busy_while_the_browser_lock_is_held(self):
        holder = self.lock_holder(credits.browser_lock_path(self.root))
        result = credits.check_tripo(self.root, run=lambda root: (0, '{"credits": 5}', ''))
        self.assertEqual(result['status'], 'busy')
        self.assertIn('browser_tripo.lock', result['message'])
        check = orchestrator.TripoCheck(self.root, run=lambda root: (0, '{"credits": 5}', ''), busy=lambda: [])
        self.assertEqual(check.request()['status'], 'busy')
        holder.kill()
        holder.wait(10)
        self.assertEqual(credits.check_tripo(self.root, run=lambda root: (0, '{"credits": 5}', ''))['status'], 'ok')

    def test_f8_only_a_numeric_balance_of_a_successful_check_counts(self):
        self.assertEqual(credits.check_tripo(self.root, run=lambda root: (0, 'x\n{"credits": 40}\n', ''))['credits'], 40)
        for code, out in ((1, '{"credits": 12}'), (0, '{"credits": null}'), (3, '{"credits": null}'), (0, '{"credits": "12"}'),
                          (0, '{"credits": true}'), (0, 'no json')):
            with self.subTest(code=code, out=out):
                self.assertEqual(credits.check_tripo(self.root, run=lambda root: (code, out, ''))['status'], 'error')
                self.assertEqual(credits.tripo_state(self.root)['credits'], 40)      # the last good observation stays

    def test_f8_a_balance_check_during_a_hyper3d_scan_is_not_lost(self):
        sub = self.root / 'models/s/j/human_male/outfit/studio_submission.json'
        sub.parent.mkdir(parents=True)
        sub.write_text(json.dumps({'balances': [{'credits': 9, 'at': '2026-11-25T10:00:00+00:00', 'stage': 'x'}]}))
        real = credits.submission_files

        def racing(root, extra_roots=None):
            files = real(root, extra_roots)
            credits.check_tripo(root, run=lambda r: (0, '{"credits": 77}', ''))   # written while the scan runs
            return files
        with mock.patch.object(credits, 'submission_files', side_effect=racing):
            credits.scan_hyper3d(self.root, extra_roots=[])
        cache = credits.read_cache(self.root)
        self.assertEqual((cache['tripo']['credits'], cache['hyper3d']['best']['credits']), (77, 9))


# ------------------------------------------------------------------------------------------------- catalog / actions / progress

class ReviewActionsTest(V2Case):
    """Findings 3, 13, 14, 18, 19 through the API of a temp root."""

    def op(self, **body):
        return self.post('/api/v2/queue', body)

    def lines(self):
        path = self.root / '.codex_jobs/_batch/convert_sets.txt'
        return path.read_text(encoding='utf-8').splitlines() if path.exists() else []

    # ---------------------------------------------------------------------------------------------------------------- F3
    def test_f3_release_needs_an_exact_set_never_a_prefix(self):
        self.job('plate_01', 'a1b2c3d4e5f6', 'multiview_ready', state='ready_for_tripo')
        self.job('plate_02', 'b1b2c3d4e5f6', 'multiview_ready', state='ready_for_tripo')
        status, body = self.op(op='release', set='plate')                          # the reviewer's typo
        self.assertEqual(status, 404, body)
        self.assertEqual(self.lines(), [])
        self.assertEqual(convert_list.pending_jobs(self.root), [])
        status, body = self.op(op='release', set='plate_01')
        self.assertEqual((status, body['added']), (200, fam('plate_01')))
        self.assertEqual([j['job'] for j in convert_list.pending_jobs(self.root)], ['plate_01--a1b2c3d4e5f6'])
        self.assertFalse(any('*' in line for line in self.lines()))
        # a race-suffixed staged job belongs to its base set (catalog.job_set); a part of the suffix does not
        # (pre-cut-over review finding 2: only when the job RECORDS that set as its parent; a job without one is ambiguous and skipped)
        self.job('hood', 'd1b2c3d4e5f6', 'multiview_ready', units=(('dwarf_male', 'outfit'),), suffix='_dwarf_male', state='ready_for_tripo')
        status, body = self.op(op='release', set='hood')
        self.assertEqual(status, 200, body)
        self.assertNotIn('hood_dwarf_male', body['added'])
        self.assertIn('ambiguous', body['skipped']['hood'][0]['reason'])
        self.job('cape', 'c1b2c3d4e5f6', 'multiview_ready', units=(('dwarf_male', 'outfit'),), suffix='_dwarf_male', state='ready_for_tripo',
                 extra={'parent_set': 'cape'})
        self.assertEqual(self.op(op='release', set='cape_dwarf')[0], 404)
        status, body = self.op(op='release', set='cape')
        self.assertEqual(status, 200, body)
        self.assertIn('cape_dwarf_male', body['added'])
        self.assertIn('cape_dwarf_male--c1b2c3d4e5f6', [j['job'] for j in convert_list.pending_jobs(self.root)])

    def test_f3_red_coat_approve_and_release_share_one_grammar(self):
        self.concept('red-coat')
        status, result = self.act('concepts:red-coat', 'approve')
        self.assertEqual(status, 200, result)
        self.assertNotIn('warning', result)                                          # the reviewer got a list warning here
        self.assertEqual(result['convert_list']['added'], fam('red-coat'))
        self.job('red-coat', 'a1b2c3d4e5f6', 'multiview_ready', state='ready_for_tripo')
        status, body = self.op(op='release', set='red-coat')                        # the reviewer got a 400 here
        self.assertEqual((status, body['added'], body['present']), (200, [], fam('red-coat')))
        self.assertEqual(self.op(op='release', set='red coat')[0], 400)

    # ---------------------------------------------------------------------------------------------------------------- F13
    def legacy(self):
        batch = self.root / 'concepts/batch'
        batch.mkdir(parents=True, exist_ok=True)
        (batch / '01_plate__outfit.png').write_bytes(b'png')
        (batch / '01_plate.json').write_text('{"race_gender": "human_male"}')
        return batch

    def crashed_promotion(self, promotion='p-123', claimed=True, promoted=False):
        """The crash window of the reviewer: promote published approved/<set> with its mark, the parent never saved promoted=true, and
        (claimed) the supervisor then claimed the folder."""
        batch = self.legacy()
        approved = self.root / 'approved/batch_01_plate'
        (approved / 'human_male').mkdir(parents=True)
        (approved / actions.PROMOTION_MARK).write_text(json.dumps({'promotion_id': promotion}))
        if claimed:
            target = self.root / 'processing_views/batch_01_plate--a1b2c3d4e5f6'
            approved.rename(target)
            (target / '.tripo-job.json').write_text(json.dumps({'set': 'batch_01_plate', 'job_id': 'a1b2c3d4e5f6', 'state': 'claimed',
                                                                'modules': [{'race_gender': 'human_male', 'module': 'outfit'}]}))
        files = [{'src': f'concepts/batch/{f.name}', 'dst': f'trash/concepts/batch/{f.name}'} for f in sorted(batch.iterdir())]
        intent = guard.new_intent(self.root, {'kind': 'flat_move', 'verb': 'approve', 'label': 'approve 01_plate', 'files': files,
                                              'sidecar': {'path': 'trash/concepts/batch/01_plate.preview-origin.json',
                                                          'content': {'kind': 'legacy', 'src': 'concepts/batch', 'approved_set': 'batch_01_plate',
                                                                      'promotion_id': promotion}},
                                              'approved_set': 'batch_01_plate', 'promoted': promoted, 'promotion_id': promotion})
        guard.abandon_intent(intent)
        return intent, batch

    def test_f13_rollback_refuses_a_promotion_claimed_before_the_flag_was_saved(self):
        intent, batch = self.crashed_promotion()
        status, body = self.act(f'intent:{intent}', 'rollback')
        self.assertEqual(status, 409, body)
        self.assertIn('already claimed', body['error'])
        self.assertTrue((self.root / 'processing_views/batch_01_plate--a1b2c3d4e5f6').is_dir())
        self.assertEqual([i['id'] for i in guard.list_intents(self.root)], [intent])   # the Recovery card stays
        status, body = self.act(f'intent:{intent}', 'complete')                     # Complete is the way out, with the list update
        self.assertEqual(status, 200, body)
        self.assertEqual(body['convert_list']['added'], fam('batch_01_plate'))
        self.assertEqual(sorted(p.name for p in batch.iterdir()), [])
        self.assertEqual(guard.list_intents(self.root), [])

    def test_f13_rollback_withdraws_an_unclaimed_promotion_whatever_the_flag(self):
        intent, batch = self.crashed_promotion(claimed=False)
        status, body = self.act(f'intent:{intent}', 'rollback')
        self.assertEqual(status, 200, body)
        self.assertFalse((self.root / 'approved/batch_01_plate').exists())
        self.assertTrue(any((self.root / '.codex_jobs/_app/withdrawn').iterdir()))

    def test_f13_complete_never_accepts_a_folder_by_name_and_refuses_ambiguity(self):
        approved = self.root / 'approved/batch_01_plate'
        (approved / 'human_male').mkdir(parents=True)                                 # an unrelated approved folder of the same name
        self.legacy()
        intent = guard.new_intent(self.root, {'kind': 'flat_move', 'verb': 'approve', 'label': 'approve 01_plate', 'files': [],
                                              'approved_set': 'batch_01_plate', 'promoted': True, 'sidecar': None})
        guard.abandon_intent(intent)
        status, body = self.act(f'intent:{intent}', 'complete')
        self.assertEqual(status, 409, body)
        self.assertIn('manual check needed', body['error'])
        trashed = self.root / 'trash/concepts/batch'
        self.assertFalse(trashed.exists() and any(trashed.iterdir()))
        # the same promotion id in two places: ambiguous
        (approved / actions.PROMOTION_MARK).write_text(json.dumps({'promotion_id': 'p-9'}))
        copy = self.root / 'processing_views/batch_01_plate--b1b2c3d4e5f6'
        copy.mkdir(parents=True)
        (copy / actions.PROMOTION_MARK).write_text(json.dumps({'promotion_id': 'p-9'}))
        guard.update_intent(self.root, intent, promotion_id='p-9')
        status, body = self.act(f'intent:{intent}', 'complete')
        self.assertEqual(status, 409, body)
        self.assertIn('ambiguous', body['error'])

    # ---------------------------------------------------------------------------------------------------------------- F14
    def test_f14_a_malformed_job_never_stops_the_other_sets(self):
        self.job('good', 'a1b2c3d4e5f6', 'processing_views')
        self.job('odd', 'b1b2c3d4e5f6', 'processing_views', extra={'conversion_errors': 'boom', 'modules': [{'race_gender': 'human_male',
                                                                                                             'module': 'outfit'}, 7]})
        derived = catalog.derive_all(self.root)
        self.assertEqual(derived['sets']['good']['stage'], 'approved')
        odd = derived['sets']['odd']
        self.assertIsNone(odd.get('derive_error'))
        self.assertTrue(any('conversion_errors is a str' in w for u in odd['units'] for w in u['warnings']))
        orch = orchestrator.Orchestrator(self.root, scan_stale=False)
        self.assertEqual(orch.reconcile()['errors'], [])                             # the reviewer got a TypeError for every set here
        # an exception inside the scan of one job, and inside the derivation of one set
        self.job('ugly', 'c1b2c3d4e5f6', 'processing_views')
        real_scan, real_derive = catalog._job_scan, catalog.derive_set

        def scan(folder, deps):
            if folder.name.startswith('ugly'):
                raise TypeError('unhashable thing')
            return real_scan(folder, deps)

        def derive(root, name, *args, **kwargs):
            if name == 'good':
                raise KeyError('broken')
            return real_derive(root, name, *args, **kwargs)
        catalog.invalidate()
        with mock.patch.object(catalog, '_job_scan', side_effect=scan), mock.patch.object(catalog, 'derive_set', side_effect=derive):
            derived = catalog._derive_all(self.root)
            out = orch.reconcile()
            state = catalog._build_state(self.root)
        self.assertIn('TypeError', derived['sets']['ugly']['derive_error'])
        self.assertIn('KeyError', derived['sets']['good']['derive_error'])
        self.assertIsNone(derived['sets']['odd'].get('derive_error'))                # the healthy one is still derived
        self.assertTrue(any('ugly' in e and 'skipped' in e for e in out['errors']), out['errors'])
        cards = {c['name']: c for c in state['tabs']['approved']}
        self.assertTrue(any(b['text'].startswith('damaged:') for b in cards['ugly']['badges']))
        self.assertIn('odd', cards)

    # ---------------------------------------------------------------------------------------------------------------- F18
    def drift_hold(self):
        folder = self.job('s', 'a1b2c3d4e5f6', 'processing_views', units=(('dwarf_male', 'outfit'),))
        image = folder / 'dwarf_male/outfit/back.png'
        image.write_bytes(b'\x89PNG drifted back')
        batch = self.root / '.codex_jobs/_batch'
        batch.mkdir(parents=True, exist_ok=True)
        holds = {'s--a1b2c3d4e5f6': {'sig': 'x', 'reasons': ['dwarf_male: back.png colour DRIFT (primary hue 331)']}}
        (batch / 'supervisor_state.json').write_text(json.dumps({'holds': holds, 'sets': {}}))
        (batch / 'advance.json').write_text(json.dumps({'sets': [{'pattern': 'x_*'}], 'auto_accept_view_drift': False}))
        return image

    def test_f18_accept_drift_is_hash_bound_and_uses_the_supervisors_record(self):
        image = self.drift_hold()
        rel = image.relative_to(self.root).as_posix()
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        _s, queue = self.get('/api/v2/queue')
        hold = queue['holds'][0]
        self.assertTrue(hold['drift'])
        self.assertEqual(hold['images'], [{'path': rel, 'sha256': digest, 'race': 'dwarf_male'}])     # what the Accept button sends
        key = 's--a1b2c3d4e5f6'
        status, body = self.op(op='accept_drift', key=key, images=[{'path': rel, 'sha256': '0' * 64}])
        self.assertEqual((status, body['code']), (409, 'changed'))                    # reviewed bytes are not the current ones
        status, body = self.op(op='accept_drift', key=key, images=[{'path': rel.replace('back', 'front'), 'sha256': digest}])
        self.assertEqual(status, 400)                                                 # not an image the hold names
        status, body = self.op(op='accept_drift', key=key, images=[{'path': '../x/back.png', 'sha256': digest}])
        self.assertEqual(status, 400)
        self.assertEqual(self.op(op='accept_drift', key='nope', images=[{'path': rel, 'sha256': digest}])[0], 404)
        advance = json.loads((self.root / '.codex_jobs/_batch/advance.json').read_text())
        self.assertNotIn('accepted_drift', advance)                                   # nothing written so far
        status, body = self.op(op='accept_drift', key=key, images=[{'path': rel, 'sha256': digest}])
        self.assertEqual(status, 200, body)
        self.assertEqual(body['accepted'], [{'path': rel, 'sha256': digest}])
        advance = json.loads((self.root / '.codex_jobs/_batch/advance.json').read_text())
        self.assertEqual([e['sha256'] for e in advance['accepted_drift']], [digest])
        self.assertEqual((advance['sets'], advance['auto_accept_view_drift']), ([{'pattern': 'x_*'}], False))   # the rest is kept
        self.assertEqual(self.op(op='accept_drift', key=key, images=[{'path': rel, 'sha256': digest}])[1]['already'], [{'path': rel, 'sha256': digest}])
        ops = [e['detail'] for e in taskqueue.for_root(self.root).events() if e['kind'] == 'op']
        self.assertTrue(any(d.startswith('accept_drift: s--a1b2c3d4e5f6: accepted 1') for d in ops), ops)
        import pipeline_supervisor
        sup = pipeline_supervisor.Supervisor(self.root, dry_run=True)
        self.assertIn(digest, sup.accepted_drift())                                    # the supervisor reads it as its own

    # ---------------------------------------------------------------------------------------------------------------- F19
    def test_f19_progress_never_borrows_another_jobs_views(self):
        old = self.job('s', 'a1b2c3d4e5f6', 'completed', units=(('human_male', 'outfit'), ('dwarf_male', 'outfit')), state='converted')
        batch = self.root / '.codex_jobs/_batch'
        for sub in ('tasks', 'status'):
            (batch / sub).mkdir(parents=True, exist_ok=True)
        name = 'views--s--dwarf_male--outfit'                                         # an older base-set task of the OLD job
        outputs = [str(old / 'dwarf_male/outfit' / f'{v}.png') for v in progress.VIEWS]
        (batch / 'tasks' / f'{name}.json').write_text(json.dumps({'name': name, 'outputs': outputs}))
        (batch / 'status' / f'{name}.json').write_text(json.dumps({'state': 'done', 'attempts': 1, 'jobs': [{'job': 'x'}]}))
        new = self.job('s', 'b1b2c3d4e5f6', 'processing_views', units=(('dwarf_male', 'outfit'),), suffix='_dwarf_male')
        (new / 'dwarf_male/outfit/front.png').unlink()                                # a fresh job with zero PNGs
        unit, _item = self.unit('s', 'dwarf_male')
        self.assertEqual(unit['job'], 'b1b2c3d4e5f6')
        self.assertEqual((unit['progress']['views_done'], unit['progress']['views_total'], unit['progress']['views_task']), (0, 4, None))
        own = 'views--s_dwarf_male--dwarf_male--outfit'
        (batch / 'tasks' / f'{own}.json').write_text(json.dumps({'name': own, 'outputs': [str(new / 'dwarf_male/outfit' / f'{v}.png')
                                                                                            for v in progress.VIEWS]}))
        unit, _item = self.unit('s', 'dwarf_male')
        self.assertEqual((unit['progress']['views_task'], unit['progress']['views_done']), (own, 0))

    def test_f19_a_failed_monster_task_log_is_read_under_the_monster_root(self):
        batch = self.root / '.codex_jobs/_batch'
        for sub in ('tasks', 'status'):
            (batch / sub).mkdir(parents=True, exist_ok=True)
        name = 'monster--Goblin--extra'
        (batch / 'tasks' / f'{name}.json').write_text(json.dumps({'name': name, 'outputs': [], 'launcher': 'monsters'}))
        (batch / 'status' / f'{name}.json').write_text(json.dumps({'state': 'failed', 'attempts': 2, 'jobs': [{'job': 'goblin-a02'}]}))
        log = self.mroot / '.codex_jobs/goblin-a02/log.txt'
        log.parent.mkdir(parents=True)
        log.write_text('the monster launcher said: rate limited\n')
        (self.root / '.codex_jobs/goblin-a02').mkdir(parents=True)
        (self.root / '.codex_jobs/goblin-a02/log.txt').write_text('a character job of the same name\n')
        failed = progress.views_section(self.root)['failed']
        self.assertEqual(len(failed), 1)
        self.assertIn('the monster launcher said', failed[0]['log_tail'])

    # ------------------------------------------------------------------------------------------------- the header status
    def test_status_services_carries_managed_and_scanned(self):
        self.assertEqual(progress.status_summary(self.root, [], None, scanned=False)['services'],
                         {'ok': True, 'problems': [], 'managed': True, 'scanned': False})
        self.assertIsNone(progress.status_summary(self.root, None, None)['services'])     # without --manage-services
        _s, state = self.get('/api/v2/state')
        self.assertIsNone(state['status']['services'])


# ======================================================================== phase 5 review, table "Phase 4 review fixes at 8ebc5cd"

from datetime import datetime  # noqa: E402

SUPERVISOR_ONLY = {'services': {'supervisor': True, 'views': False, 'convert': False}}
SLEEP_CHILD = ['-c', 'import time; time.sleep(60)']


class Phase5ServicesTest(TempRoot):
    """Rows 2, 6, 17 (the view runner side) and 20 against the fake services of a temp root."""
    popen, hold, sup, lock_holder = ReviewServicesTest.popen, ReviewServicesTest.hold, ReviewServicesTest.sup, ReviewServicesTest.lock_holder

    def event(self, text):
        with open(self.root / '.codex_jobs/_batch/convert_events.log', 'a', encoding='utf-8') as handle:
            handle.write(f'{datetime.now():%m-%d %H:%M:%S} {text}\n')

    def legacy_convert(self):
        """A running worker of the code before the per-worker stop file (I.2 legacy rule), adopted by the manager."""
        settings.save(self.root, CONVERT_ONLY)
        old = self.spawn('convert')
        self.ctl('convert_queue', {'ignore_stop': True})
        script = self.root / 'tools/convert_queue.py'
        os.utime(script, (time.time() + 600, time.time() + 600))
        services.tick(self.root)
        return old

    def children(self, pid):
        try:
            return psutil.Process(pid).children()
        except psutil.Error:
            return []

    def kill_children(self, pid):
        for child in self.children(pid):
            try:
                child.kill()
                child.wait(10)
            except psutil.Error:
                pass

    def not_suspended(self, pid):
        return psutil.Process(pid).status() != psutil.STATUS_STOPPED

    # ---------------------------------------------------------------------------------------------------------------- row 2
    def test_row2_a_tick_of_the_outgoing_supervisor_never_releases_the_hold(self):
        """The supervisor a stop was sent to keeps ticking while its stop is pending: its ticks never release the hold; only a supervisor
        started after it does."""
        settings.save(self.root, SUPERVISOR_ONLY)
        old = self.spawn('supervisor')
        services.tick(self.root)
        self.ctl('pipeline_supervisor', {'ignore_stop': True})                      # it does not exit yet, and keeps ticking
        result = services.stop(self.root, 'supervisor', wait=0.5)
        self.assertTrue(result['pending'])
        hold = self.hold()
        self.assertEqual([r['pid'] for r in hold['retiring']], [old.pid])
        self.wait_for(lambda: services.supervisor_ticked(self.root, self.sup(), hold['since_unix']), what='a tick of the outgoing supervisor')
        for _ in range(2):
            events = services.tick(self.root)['events']
            self.assertFalse(any('released' in e for e in events), events)
        self.assertTrue(self.hold())
        self.assertIn(str(old.pid), services.hold_state(self.root)['message'])
        # even once its stop record is gone (a failed stop command, a lost record) the retiring generation never releases it
        rec = services.read_record(self.root, 'supervisor')
        services.write_record(self.root, 'supervisor', rec | {'stop_requested': None})
        self.assertIsNone(services.release_hold(self.root, self.sup()))
        services.write_record(self.root, 'supervisor', rec)
        self.ctl('pipeline_supervisor', {})                                          # it honours the stop now
        old.wait(10)
        services.tick(self.root)
        self.assertTrue(self.hold())                                                 # no supervisor: still held
        services.start(self.root, 'supervisor')
        self.wait_for(lambda: 'conversion hold released' in ' '.join(services.tick(self.root)['events']), what='the successor tick')
        self.assertIsNone(self.hold())

    def test_row2_a_manager_restart_keeps_the_retiring_generation(self):
        outgoing = self.popen(*SLEEP_CHILD)
        ident = {'pid': outgoing.pid, 'create_time': psutil.Process(outgoing.pid).create_time()}
        services.assert_hold(self.root, 'supervisor restart', renew=True, retiring=ident)
        services.begin_management(self.root)                                         # a new manager renews the hold: the outgoing one stays named
        self.assertEqual([r['pid'] for r in self.hold()['retiring']], [outgoing.pid])
        (self.root / '.codex_jobs/_batch/supervisor_tick.json').write_text(json.dumps({'pid': outgoing.pid, 'create_time': ident['create_time'],
                                                                                      'time': time.time() + 60}))
        self.assertTrue(services.supervisor_ticked(self.root, ident, self.hold()['since_unix']))
        self.assertIsNone(services.release_hold(self.root, ident))
        self.assertTrue(self.hold())
        outgoing.kill()
        outgoing.wait(10)
        services.begin_management(self.root)
        self.assertNotIn('retiring', self.hold())                                     # a dead generation is pruned on the next write

    # ---------------------------------------------------------------------------------------------------------------- row 6
    def test_row6_a_legacy_drain_holds_first_and_never_stops_a_woken_worker(self):
        old = self.legacy_convert()
        services.hold_path(self.root).unlink(missing_ok=True)                         # (as if a supervisor had released the startup hold)
        self.event('PAUSED (PAUSE file); not starting conversions')                  # its log says it sleeps ...
        self.ctl('convert_queue', {'ignore_stop': True, 'child': SLEEP_CHILD})       # ... but it woke and launched a child since
        self.addCleanup(self.kill_children, old.pid)
        self.wait_for(lambda: self.children(old.pid), what='the woken worker child')
        result = services.stop(self.root, 'convert', wait=0.5)
        self.assertTrue(result['pending'], result)
        self.assertIn('child process', result['message'])
        self.assertIn('legacy convert worker drain', self.hold()['reason'])         # the hold was written before anything else
        self.assertTrue(services.read_record(self.root, 'convert')['drain_hold'])
        self.assertEqual([i['pid'] for i in self.running('convert')], [old.pid])     # not stopped
        self.assertTrue(self.not_suspended(old.pid))                                  # and resumed after the frozen check
        self.assertIn('drained', services.release_blocker(self.root, None))          # no supervisor tick releases the hold meanwhile
        self.kill_children(old.pid)
        self.ctl('convert_queue', {'ignore_stop': True})
        lock = self.root / '.codex_jobs/_batch/convert_locks/s--abc123.lock'           # between claim and conversion: it holds a job lock
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(old.pid), encoding='utf-8')
        services.tick(self.root)
        self.assertEqual([i['pid'] for i in self.running('convert')], [old.pid])
        self.assertIn('job lock', services.status(self.root)['services']['convert']['message'])
        self.assertTrue(self.not_suspended(old.pid))
        lock.unlink()
        result = services.tick(self.root)
        self.assertTrue(any('verified while suspended' in e for e in result['events']), result['events'])
        old.wait(10)
        self.assertEqual(self.running('convert'), [])
        self.assertFalse(services.read_record(self.root, 'convert').get('drain_hold'))
        self.assertIsNone(services.release_blocker(self.root, None))

    def test_row6_the_frozen_check_runs_while_the_worker_is_suspended(self):
        old = self.legacy_convert()
        self.event('IDLE: no staged job; waiting')
        seen = []
        real = services._worker_idle_problem

        def spy(root, proc, inst):
            seen.append(proc.status())
            return real(root, proc, inst)
        with mock.patch.object(services, '_worker_idle_problem', side_effect=spy):
            result = services.stop(self.root, 'convert', wait=0.5)
        self.assertFalse(result['pending'], result)
        self.assertEqual(seen, [psutil.STATUS_STOPPED])
        old.wait(10)

    # --------------------------------------------------------------------------------------------- row 17 (view runner)
    def test_row17_an_older_view_runner_is_stopped_only_while_it_launches_nothing(self):
        settings.save(self.root, VIEWS_ONLY)
        runner = self.spawn('views')
        services.tick(self.root)
        self.assertTrue(services.legacy_runner(self.root, self.running('views')[0]))   # no view_runner.lock, no launch reservation
        job = 'views--s--human_male--outfit-a01'
        self.ctl('view_batch', {'child': SLEEP_CHILD + ['tools/codex_imagegen.sh', job, 'prompt.md']})
        self.addCleanup(self.kill_children, runner.pid)
        self.wait_for(lambda: self.children(runner.pid), what='the launched Codex job')
        codex = self.children(runner.pid)[0]
        self.addCleanup(lambda: codex.is_running() and codex.kill())
        result = services.stop(self.root, 'views', wait=0.5)
        self.assertTrue(result['pending'], result)
        self.assertIn(f'launching Codex job {job}', result['message'])
        self.assertEqual([i['pid'] for i in self.running('views')], [runner.pid])
        self.assertTrue(self.not_suspended(runner.pid))
        self.assertEqual(services.status(self.root)['services']['views']['state'], 'stopping')
        status = self.root / '.codex_jobs/_batch/status'
        status.mkdir(parents=True, exist_ok=True)
        (status / 'views--s--human_male--outfit.json').write_text(json.dumps({'state': 'running', 'attempts': 0, 'jobs': [{'job': job}]}))
        result = services.tick(self.root)                                             # recorded now: the next runner adopts it
        self.assertTrue(any('older view runner stopped' in e for e in result['events']), result['events'])
        runner.wait(10)
        self.assertTrue(codex.is_running())                                           # the Codex job itself continues
        self.assertEqual(result['completed'][0]['action'], 'stop')

    # ------------------------------------------------------------------------------- Tripo profile reservation (agent A's lock)
    def test_the_balance_check_holds_the_profile_reservation_and_passes_its_env(self):
        import tripo_browser_lock
        seen = {}

        def fake_run(cmd, **kwargs):
            seen.update(env=kwargs.get('env') or {}, cmd=cmd)
            return subprocess.CompletedProcess(cmd, 0, '{"credits": 12}\n', '')
        with mock.patch.object(credits.subprocess, 'run', side_effect=fake_run):
            result = credits.check_tripo(self.root)
        self.assertEqual((result['status'], result['credits']), ('ok', 12))
        lock = seen['env'].get(tripo_browser_lock.PROFILE_LOCK_ENV)
        self.assertEqual(Path(lock), tripo_browser_lock.profile_lock_path())          # the resolved profile of this (temp) environment
        self.assertTrue(str(lock).startswith(str(self.root)))                          # never the real profile in a test
        self.assertTrue(seen['env'].get(tripo_browser_lock.PROFILE_TOKEN_ENV))
        holder = self.lock_holder(tripo_browser_lock.profile_lock_path())              # another checkout / a manual studio.mjs
        self.assertTrue(credits.tripo_browser_busy(self.root))
        busy = credits.check_tripo(self.root, run=lambda root: (0, '{"credits": 5}', ''))
        self.assertEqual(busy['status'], 'busy')
        holder.kill()
        holder.wait(10)
        self.assertFalse(credits.tripo_browser_busy(self.root))

    # ---------------------------------------------------------------------------------------------------------------- row 20
    def test_row20_a_held_job_lock_with_every_module_free_is_no_work(self):
        job = self.root / 'multiview_ready/new_set--abc123'
        job.mkdir(parents=True)
        (job / '.tripo-job.json').write_text(json.dumps({'set': 'new_set', 'job_id': 'abc123', 'state': 'ready',
                                                         'modules': [{'race_gender': 'human_male', 'module': 'outfit'}]}))
        convert_list.add_sets(self.root, ['new_set'])
        holder = self.popen(*SLEEP_CHILD)
        lock = self.root / '.codex_jobs/_batch/convert_locks/new_set--abc123.lock'
        lock.parent.mkdir(parents=True)
        lock.write_text(str(holder.pid), encoding='utf-8')
        with mock.patch.object(convert_list, 'worker_predicate', return_value=None):
            self.assertEqual(convert_list.eligible_jobs(self.root), [])               # cannot be claimed, nothing to join
            self.assertFalse(services.has_work(self.root))
            lock.unlink()
            self.assertEqual([j['job'] for j in convert_list.eligible_jobs(self.root)], ['new_set--abc123'])
        calls = []

        def predicate(root, name):
            calls.append((Path(root), name))
            return False, 'the worker says no'
        with mock.patch.object(convert_list, 'worker_predicate', return_value=predicate):
            self.assertEqual(convert_list.eligible_jobs(self.root), [])               # the worker's own predicate decides
        self.assertEqual(calls, [(self.root, 'new_set--abc123')])

    def test_row20_the_workers_own_predicate_is_used(self):
        self.assertIsNotNone(convert_list.worker_predicate())                          # tools/convert_queue.py job_eligible
        job = self.root / 'multiview_ready/new_set--abc123'
        job.mkdir(parents=True)
        (job / '.tripo-job.json').write_text(json.dumps({'set': 'new_set', 'job_id': 'abc123', 'state': 'ready',
                                                         'modules': [{'race_gender': 'human_male', 'module': 'outfit'}]}))
        convert_list.add_sets(self.root, ['new_set'])
        holder = self.popen(*SLEEP_CHILD)
        lock = self.root / '.codex_jobs/_batch/convert_locks/new_set--abc123.lock'
        lock.parent.mkdir(parents=True)
        lock.write_text(str(holder.pid), encoding='utf-8')
        self.assertEqual(convert_list.eligible_jobs(self.root), [])
        lock.unlink()
        self.assertEqual([j['job'] for j in convert_list.eligible_jobs(self.root)], ['new_set--abc123'])

    def test_row20_a_joinable_job_in_processing_is_work(self):
        job = self.root / 'processing/new_set--abc123'
        job.mkdir(parents=True)
        (job / '.tripo-job.json').write_text(json.dumps({'set': 'new_set', 'job_id': 'abc123', 'state': 'converting', 'modules': [
            {'race_gender': 'human_male', 'module': 'outfit'}, {'race_gender': 'dwarf_male', 'module': 'outfit'}]}))
        convert_list.add_sets(self.root, ['new_set'])
        holder = self.popen(*SLEEP_CHILD)
        locks = self.root / '.codex_jobs/_batch/convert_locks'
        locks.mkdir(parents=True)
        (locks / 'new_set--abc123.lock').write_text(str(holder.pid), encoding='utf-8')
        module_lock = self.root / 'models/new_set/abc123/.module_locks/human_male__outfit.lock'
        module_lock.parent.mkdir(parents=True)
        module_lock.write_text(str(holder.pid), encoding='utf-8')                       # another worker converts one module: join the other
        with mock.patch.object(convert_list, 'worker_predicate', return_value=None):
            found = convert_list.eligible_jobs(self.root)
        self.assertEqual([(j['job'], j['free'], j['reason']) for j in found], [('new_set--abc123', 1, 'joinable')])


class Phase5ManagerTest(TempRoot):
    """Row 18 (the server side) and the startup-hold event the monster dispatcher waits for."""
    manager, result_of = ServiceManagerTest.manager, ServiceManagerTest.result_of

    def setUp(self):
        super().setUp()
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.stop()
        super().tearDown()

    def test_row18_a_pending_stop_keeps_its_request_id_to_the_terminal_event(self):
        worker = self.spawn('convert')
        self.ctl('convert_queue', {'busy': True})                                     # it acknowledges, then finishes its job first
        manager = self.manager(CONVERT_ONLY)
        wait_for(lambda: manager.statuses, what='first tick')
        answer = manager.request('convert', 'stop')
        first = self.result_of(manager, answer['request'])
        self.assertTrue(first['ok'] and first['pending'], first)
        self.assertIn(answer['request'], [p['id'] for p in manager.view()['pending']])    # still pending in the view (never "lost")
        self.ctl('convert_queue', {})
        worker.wait(15)
        manager.wake()
        final = wait_for(lambda: next((e for e in manager.events if e['kind'] == 'request' and e.get('request') == answer['request']
                                       and e.get('pending') is False), None), what='the terminal event')
        self.assertTrue(final['ok'])
        self.assertEqual((final['name'], final['action']), ('convert', 'stop'))
        self.assertIn('stopped', final['text'])
        self.assertNotIn(answer['request'], [p['id'] for p in manager.view()['pending']])

    def test_row18_a_new_request_supersedes_a_pending_one(self):
        self.spawn('convert')
        self.ctl('convert_queue', {'busy': True})
        manager = self.manager(CONVERT_ONLY)
        wait_for(lambda: manager.statuses, what='first tick')
        first = manager.request('convert', 'stop')['request']
        self.assertTrue(self.result_of(manager, first)['pending'])
        second = manager.request('convert', 'restart')['request']
        ended = wait_for(lambda: next((e for e in manager.events if e.get('request') == first and e.get('pending') is False), None),
                         what='the superseded outcome')
        self.assertIn(f'superseded by request {second}', ended['text'])
        self.ctl('convert_queue', {})

    def test_the_startup_hold_is_signalled_before_the_first_tick(self):
        order = []
        manager = orchestrator.ServiceManager(self.root, interval=3600.0, tick=lambda root, cfg: order.append(manager.hold_asserted.is_set())
                                              or {'services': {}, 'events': []})
        self.managers.append(manager)
        self.assertFalse(manager.hold_asserted.is_set())
        manager.start()
        wait_for(lambda: order, what='the first tick')
        self.assertEqual(order[0], True)
        self.assertTrue(services.read_hold(self.root))

    def test_a_failed_startup_hold_is_retried_before_anything_runs(self):
        calls, ticks = [], []
        real = services.begin_management

        def flaky(root):
            calls.append(1)
            if len(calls) == 1:
                raise OSError('disk busy')
            return real(root)
        manager = orchestrator.ServiceManager(self.root, interval=3600.0, tick=lambda root, cfg: ticks.append(1) or {'services': {}, 'events': []})
        self.managers.append(manager)
        with mock.patch.object(services, 'begin_management', side_effect=flaky):
            manager.start()
            self.assertFalse(ticks)
            wait_for(lambda: ticks, what='the first tick (after the 5 s retry)')
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(manager.hold_asserted.is_set())
        self.assertTrue(any('begin: OSError' in e for e in manager.errors))


class Phase5ActionsTest(V2Case):
    """Row 3: the family namespace of a set is reserved at approval and recorded for the catalog."""

    def lines(self):
        path = self.root / '.codex_jobs/_batch/convert_sets.txt'
        return path.read_text(encoding='utf-8').splitlines() if path.exists() else []

    def test_row3_a_set_named_like_another_sets_family_id_is_refused(self):
        self.concept('cape')
        status, result = self.act('concepts:cape', 'approve')
        self.assertEqual(status, 200, result)
        self.assertIn('cape_human_male', self.lines())                                # cape's pre-authorized family entry
        self.assertEqual(convert_list.owner_of(self.root, 'cape_human_male'), 'cape')
        self.concept('cape_human_male')
        status, body = self.act('concepts:cape_human_male', 'approve')
        self.assertEqual((status, body['code']), (409, 'namespace'), body)
        self.assertTrue((self.root / 'concepts/cape_human_male').is_dir())            # nothing moved
        self.assertIn('reserved', body['error'])

    def test_row3_a_set_whose_family_id_is_an_existing_set_is_refused(self):
        self.concept('wrap_dwarf_male')
        status, result = self.act('concepts:wrap_dwarf_male', 'approve')
        self.assertEqual(status, 200, result)
        self.concept('wrap')
        status, body = self.act('concepts:wrap', 'approve')
        self.assertEqual((status, body['code']), (409, 'namespace'), body)
        self.assertIn('wrap_dwarf_male exists', body['error'])
        self.assertNotIn('wrap', self.lines())
        # without the reservation add_sets still never writes another set's id as a family id
        added = convert_list.add_sets(self.root, ['wrap'])
        self.assertNotIn('wrap_dwarf_male', added['added'])
        self.assertEqual([s['id'] for s in added['skipped']['wrap']], ['wrap_dwarf_male'])

    def test_row3_the_catalog_uses_the_recorded_owner_not_the_suffix(self):
        self.concept('wrap_dwarf_male')
        self.assertEqual(self.act('concepts:wrap_dwarf_male', 'approve')[0], 200)
        self.job('wrap_dwarf_male', 'a1b2c3d4e5f6', 'processing_views', units=(('dwarf_male', 'outfit'),))
        catalog.invalidate()
        sets = catalog.derive_all(self.root)['sets']
        self.assertIn('wrap_dwarf_male', sets)                                       # its own set, not "wrap"
        self.assertNotIn('wrap', sets)
        self.assertEqual(catalog.job_set('wrap_dwarf_male--a1b2c3d4e5f6', ['dwarf_male', 'human_male']), 'wrap')   # the legacy suffix rule
        self.assertEqual(catalog.job_set('zz--a1b2c3d4e5f6', ['dwarf_male'], self.root, 'cape'), 'cape')          # a record's parent_set wins

    def test_row3_an_unrelated_name_and_a_re_approval_are_fine(self):
        self.concept('cape_dwarf')                                                    # not a race suffix
        self.assertIsNone(convert_list.namespace_conflict(self.root, 'cape_dwarf'))
        self.concept('hood')
        self.assertEqual(self.act('concepts:hood', 'approve')[0], 200)
        self.assertEqual(self.act('approved:hood', 'unapprove')[0], 200)
        self.assertIsNone(convert_list.namespace_conflict(self.root, 'hood'))          # its own family ids never block it


# ------------------------------------------------------------------------------- pre-cut-over review (DESIGN_PHASE4.md I.10)

REAL_CONVERT_JOB = TOOLS / 'tripo_pipeline' / 'convert_job.py'
# A convert_job.py of the temp root that runs the REAL gate code (wait_for_gate, the module lock, the job record) against that root: it claims
# its module lock, then blocks at the paid gate; with ctl {"work": true} it never reaches the gate (work in flight).
GATE_JOB = '''
import importlib.util, json, pathlib, sys, time
real = pathlib.Path(%r)
sys.path.insert(0, str(real.parent))
spec = importlib.util.spec_from_file_location('real_convert_job', real)
cj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cj)
root = pathlib.Path.cwd()
cj.ROOT, cj.GATE_POLL = root, 0.2
job = sys.argv[1]
record = root / 'processing' / job / '.tripo-job.json'
lock = root / 'models' / 's' / 'abc123' / '.module_locks' / 'human_male__outfit.lock'
cj._RUN.update(job=job, record=record if record.exists() else None, lock=None)
ctl = root / '.codex_jobs' / '_app' / 'fake_gate_job.json'
if ctl.exists() and json.loads(ctl.read_text()).get('work'):
    time.sleep(600)
assert cj.claim_module(lock)
cj._RUN['lock'] = lock
cj.wait_for_gate('human_male/outfit', 'a new submission')
time.sleep(600)
''' % str(REAL_CONVERT_JOB)
# An OLDER convert worker: it knows neither CONVERT_HOLD nor the stop file, counts every non-zero child exit in <job>.fails and takes the job
# again at once (it never stops cycling by itself).
CYCLING_WORKER = '''
import datetime, os, pathlib, subprocess, sys, time
root = pathlib.Path.cwd()
batch = root / '.codex_jobs' / '_batch'
locks = batch / 'convert_locks'
locks.mkdir(parents=True, exist_ok=True)
JOB = 's--abc123'

def event(text):
    with open(batch / 'convert_events.log', 'a') as handle:
        handle.write(f'{datetime.datetime.now():%m-%d %H:%M:%S} {text}\\n')
while True:
    (locks / f'{JOB}.lock').write_text(str(os.getpid()))
    event(f'CONVERT {JOB}')
    code = subprocess.run([sys.executable, 'tools/tripo_pipeline/convert_job.py', JOB]).returncode
    fails = locks / f'{JOB}.fails'
    if code:
        fails.write_text(str((int(fails.read_text()) if fails.exists() else 0) + 1))
        event(f'FAILED {JOB}: convert_job exit {code}')
    (locks / f'{JOB}.lock').unlink()
    time.sleep(0.05)
'''


class CutoverDrainTest(TempRoot):
    """Finding 1 (the drain never deadlocks on a child blocked at the paid gate) and the row 6 residual (the persisted suspension)."""
    popen, hold = ReviewServicesTest.popen, ReviewServicesTest.hold
    event, children, kill_children = Phase5ServicesTest.event, Phase5ServicesTest.children, Phase5ServicesTest.kill_children
    JOB = 's--abc123'

    def setUp(self):
        super().setUp()
        self.batch = self.root / '.codex_jobs/_batch'
        (self.root / 'tools/tripo_pipeline/convert_job.py').write_text(GATE_JOB, encoding='utf-8')
        folder = self.root / 'processing' / self.JOB
        folder.mkdir(parents=True)
        self.record = folder / '.tripo-job.json'
        self.record.write_text(json.dumps({'set': 's', 'job_id': 'abc123', 'state': 'ready', 'modules': [{'race_gender': 'human_male', 'module': 'outfit'}]}))
        self.module_lock = self.root / 'models/s/abc123/.module_locks/human_male__outfit.lock'
        self.pause = self.batch / 'PAUSE'
        self.pause_text = json.dumps({'since': 'x', 'reason': 'manual pause-conversions', 'scope': ['conversions']})
        self.pause.write_text(self.pause_text)                                       # the user's pause: it must survive the whole drain
        self.fails = self.batch / 'convert_locks' / f'{self.JOB}.fails'

    def legacy(self, script=None, **ctl):
        settings.save(self.root, CONVERT_ONLY)
        path = self.root / 'tools/convert_queue.py'
        if script:
            path.write_text(script, encoding='utf-8')
        services.begin_management(self.root)                                         # the hold exists before the worker runs (I.1)
        self.ctl('convert_queue', {'ignore_stop': True} | ctl)
        old = self.spawn('convert')
        self.addCleanup(self.kill_children, old.pid)
        os.utime(path, (time.time() + 600, time.time() + 600))                       # it runs code older than the file
        return old

    def waiters(self):
        return services.gate_waiters(self.root)

    def drain_until_gone(self, timeout=90.0):
        events, end = [], time.monotonic() + timeout
        while self.running('convert'):
            self.assertLess(time.monotonic(), end, f'the drain did not finish: {events[-6:]}')
            events += services.tick(self.root)['events']
            time.sleep(0.3)
        return events + services.tick(self.root)['events']

    def check_drained(self):
        self.assertEqual(self.running('convert'), [])
        self.assertEqual(self.running('convert_job'), [])                            # the gate-waiter left by itself
        self.assertFalse(services.drain_path(self.root).exists())                    # CONVERT_DRAIN removed when the drain completed
        self.assertEqual(self.pause.read_text(), self.pause_text)                    # the user's pause was never touched
        self.assertTrue(self.hold())                                                 # no supervisor ticked: conversions stay held
        self.assertFalse(self.module_lock.exists())                                  # no module lock left behind
        self.assertEqual(list((self.batch / 'convert_locks').glob('*.lock')), [])    # and no job lock
        self.assertFalse(self.fails.exists())                                        # no failure counted
        self.assertEqual(list((self.batch / 'drain_exits').glob('*.json')), [])
        self.assertEqual(list((self.batch / 'gate_wait').glob('*.json')), [])
        self.assertFalse(services.suspensions_path(self.root).exists())
        rec = services.read_record(self.root, 'convert')
        self.assertFalse(rec.get('drain_hold'))
        self.assertIsNone(services.release_blocker(self.root, None))
        deferred = json.loads(self.record.read_text())['deferred']['human_male/outfit']
        self.assertIn('CONVERT_HOLD', deferred['reason'])
        return rec

    def test_f1_a_child_blocked_at_the_paid_gate_never_deadlocks_the_drain(self):
        """The reviewer's reproduction: the legacy worker's child (current code) waits for the very hold the drain keeps."""
        old = self.legacy(child=['tools/tripo_pipeline/convert_job.py', self.JOB])
        self.wait_for(self.waiters, what='the child at the paid gate')
        self.assertEqual((self.waiters()[0]['job'], self.waiters()[0]['module']), (self.JOB, 'human_male/outfit'))
        self.assertTrue(self.module_lock.exists())
        services.tick(self.root)
        self.event('CONVERT ' + self.JOB)
        seen = []
        real = services.write_drain

        def spy(root, inst, now=None):
            seen.append((self.hold(), [w['job'] for w in self.waiters()], inst['pid']))
            return real(root, inst, now)
        with mock.patch.object(services, 'write_drain', side_effect=spy):
            result = services.stop(self.root, 'convert', wait=0.5)
            events = [result['message']] + self.drain_until_gone()
        self.assertEqual(len(seen), 1)
        hold, waiting, worker = seen[0]
        self.assertTrue(hold and hold['owner_pid'] == os.getpid())                   # CONVERT_HOLD first, then CONVERT_DRAIN
        self.assertEqual((waiting, worker), ([self.JOB], old.pid))                   # the child was blocked at the gate when the drain began
        self.assertTrue(any('CONVERT_DRAIN removed' in e for e in events), events)
        old.wait(10)
        rec = self.check_drained()
        self.assertEqual([(r['job'], r['exits']) for r in rec['drain_restored']], [(self.JOB, 1)])       # recorded which job left
        self.assertIn(self.JOB, (self.batch / 'convert_drain.log').read_text())

    def test_f1_the_count_an_older_worker_wrote_for_a_drain_exit_is_restored(self):
        old = self.legacy(child=['tools/tripo_pipeline/convert_job.py', self.JOB])
        self.wait_for(self.waiters, what='the child at the paid gate')
        services.tick(self.root)
        services.write_drain(self.root, self.running('convert')[0])                  # (the drain began: the gate-waiter leaves)
        self.wait_for(lambda: not self.running('convert_job'), what='the gate-waiter to leave')
        exits = services.drain_exits(self.root)
        self.assertEqual([(e['job'], e['fails_before'], e['parent_aware'], e['lock_released']) for e in exits], [(self.JOB, 0, False, True)])
        self.fails.parent.mkdir(parents=True, exist_ok=True)
        self.fails.write_text('1')                                                   # the older worker counted the non-zero exit ...
        self.event(f'FAILED {self.JOB}: convert_job exit {services.DRAIN_EXIT}')
        self.event('PAUSED (CONVERT_HOLD: preview app manager start); not starting conversions')         # ... and sleeps on the hold
        result = services.stop(self.root, 'convert', wait=0.5)
        events = [result['message']] + self.drain_until_gone()
        self.assertTrue(any('restored 1 -> 0' in e for e in events), events)
        old.wait(10)
        rec = self.check_drained()
        self.assertEqual([(r['job'], r['fails_from'], r['fails_to']) for r in rec['drain_restored']], [(self.JOB, 1, 0)])

    def test_f1_a_worker_that_keeps_cycling_is_frozen_and_stopped_while_it_has_no_child(self):
        old = self.legacy(script=CYCLING_WORKER)
        self.wait_for(self.waiters, what='the child at the paid gate')
        services.tick(self.root)
        events = [services.stop(self.root, 'convert', wait=0.5)['message']] + self.drain_until_gone()
        self.assertTrue(any('kept taking jobs' in e for e in events), events)
        old.wait(10)
        rec = self.check_drained()
        self.assertTrue(all(r.get('fails_to', 0) == 0 for r in rec['drain_restored']), rec['drain_restored'])

    def test_f1_work_in_flight_is_never_interrupted(self):
        """A child that is not at the gate (a conversion in flight) keeps the drain waiting: nothing is frozen for good, nothing killed."""
        self.ctl('gate_job', {'work': True})
        old = self.legacy(child=['tools/tripo_pipeline/convert_job.py', self.JOB])
        self.wait_for(lambda: self.running('convert_job'), what='the working child')
        services.tick(self.root)
        self.event('CONVERT ' + self.JOB)
        result = services.stop(self.root, 'convert', wait=0.5)
        self.assertTrue(result['pending'])
        self.assertIn('runs now', result['message'])
        for _ in range(2):
            services.tick(self.root)
        self.assertEqual([i['pid'] for i in self.running('convert')], [old.pid])
        self.assertEqual(len(self.running('convert_job')), 1)
        self.assertNotEqual(psutil.Process(old.pid).status(), psutil.STATUS_STOPPED)
        self.assertTrue(services.drain_path(self.root).exists())

    def test_f1_a_stale_drain_file_is_removed_at_manager_start(self):
        services.write_drain(self.root, {'pid': 999999, 'create_time': 1.0})
        lock = self.batch / 'convert_locks' / f'{self.JOB}.lock'
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text('999999')
        events = services.begin_management(self.root)
        self.assertTrue(any('CONVERT_DRAIN removed' in e for e in events), events)
        self.assertFalse(services.drain_path(self.root).exists())
        self.assertFalse(lock.exists())

    def test_f1_restoring_never_raises_a_count_and_ignores_aware_parents(self):
        folder = self.batch / 'drain_exits'
        folder.mkdir()
        self.fails.parent.mkdir(parents=True, exist_ok=True)
        base = {'pid': 999999, 'create_time': 1.0, 'job': self.JOB, 'module': 'human_male/outfit', 'time': time.time()}
        (folder / 'a.json').write_text(json.dumps(base | {'fails_before': 1, 'parent_aware': False}))
        (folder / 'b.json').write_text(json.dumps(base | {'fails_before': 2, 'parent_aware': False}))
        (folder / 'c.json').write_text(json.dumps(base | {'fails_before': 2, 'parent_aware': True}))       # a current worker: counted nothing
        (folder / 'd.json').write_text(json.dumps(base | {'job': 't--manual', 'fails_before': 2, 'parent_aware': False}))   # run by hand
        (self.fails.parent / 't--manual.fails').write_text('2')
        self.fails.write_text('3')
        restored = {r['job']: r for r in services.settle_drain_exits(self.root)}
        self.assertEqual((restored[self.JOB]['fails_from'], restored[self.JOB]['fails_to'], restored[self.JOB]['counted']), (3, 1, 2))
        self.assertEqual(self.fails.read_text(), '1')
        self.assertNotIn('fails_to', restored['t--manual'])                          # nobody counted it: never below what it was before
        self.assertEqual((self.fails.parent / 't--manual.fails').read_text(), '2')
        self.assertEqual(list(folder.glob('*.json')), [])

    # ----------------------------------------------------------------------------------------------------- row 6 residual
    def test_row6_the_suspension_is_persisted_before_and_cleared_after(self):
        os.remove(self.root / 'tools/tripo_pipeline/convert_job.py')
        old = self.legacy()
        services.tick(self.root)
        self.event('IDLE: no staged job; waiting')
        seen = []

        def check(proc):
            seen.append((proc.status(), services.read_suspensions(self.root)))
            return 'not now'
        gone, problem = services.frozen_stop(self.root, services.SERVICES['convert'], self.running('convert')[0], check)
        self.assertEqual((gone, problem), (False, 'not now'))
        status, items = seen[0]
        self.assertEqual(status, psutil.STATUS_STOPPED)
        self.assertEqual([(i['pid'], i['service'], i['terminated']) for i in items], [(old.pid, 'convert', False)])
        self.assertAlmostEqual(items[0]['create_time'], psutil.Process(old.pid).create_time(), delta=1.5)
        self.assertEqual(services.read_suspensions(self.root), [])                   # resumed: the record is gone
        self.assertNotEqual(psutil.Process(old.pid).status(), psutil.STATUS_STOPPED)

    def test_row6_a_manager_start_resumes_what_a_crashed_manager_left_suspended(self):
        os.remove(self.root / 'tools/tripo_pipeline/convert_job.py')
        old = self.legacy()
        inst = self.running('convert')[0]
        other = self.popen(*SLEEP_CHILD)                                             # not a service of this checkout
        stranger = {'pid': other.pid, 'create_time': psutil.Process(other.pid).create_time()}
        services._note_suspension(self.root, 'convert', inst)                        # the manager died between suspend and resume
        services._note_suspension(self.root, 'convert', stranger)
        services._note_suspension(self.root, 'convert', {'pid': 999999, 'create_time': 1.0})       # terminated meanwhile
        psutil.Process(old.pid).suspend()
        psutil.Process(other.pid).suspend()
        self.addCleanup(lambda: psutil.Process(other.pid).resume() if psutil.pid_exists(other.pid) else None)
        events = services.begin_management(self.root)
        self.assertTrue(any(f'pid {old.pid} was left suspended' in e and 'resumed' in e for e in events), events)
        self.assertNotEqual(psutil.Process(old.pid).status(), psutil.STATUS_STOPPED)
        self.assertEqual(psutil.Process(other.pid).status(), psutil.STATUS_STOPPED)   # never touched: it is not this checkout's service
        self.assertTrue(any(f'pid {other.pid}' in e and 'not touched' in e for e in events), events)
        self.assertEqual(services.read_suspensions(self.root), [])
        self.assertEqual(services.begin_management(self.root)[1:], [])


class CutoverSupervisorTest(TempRoot):
    """Finding 7 (a failed supervisor stop delivery) and finding 20 (a restart's terminal outcome)."""
    hold, sup = ReviewServicesTest.hold, ReviewServicesTest.sup

    def supervisor(self):
        settings.save(self.root, SUPERVISOR_ONLY)
        old = self.spawn('supervisor')
        services.begin_management(self.root)
        self.wait_for(lambda: 'conversion hold released' in ' '.join(services.tick(self.root)['events']), what='the first release')
        return old

    def test_f7_a_failed_stop_command_withdraws_the_retiring_designation(self):
        old = self.supervisor()
        failed = subprocess.CompletedProcess([], 1, '', 'boom: the stop command crashed')
        with mock.patch.object(services.subprocess, 'run', return_value=failed), self.assertRaises(services.ServiceError) as caught:
            services.stop(self.root, 'supervisor', wait=0.5)
        self.assertIn('not delivered', str(caught.exception))
        rec = services.read_record(self.root, 'supervisor')
        self.assertEqual((rec['stop_delivery']['state'], rec['stop_requested'], rec['then_start']), ('failed', None, False))
        self.assertIn('boom', rec['stop_delivery']['why'])
        self.assertTrue(self.hold())                                                  # the hold written before the stop is still there ...
        self.assertNotIn('retiring', self.hold())                                     # ... but this supervisor is no outgoing generation
        self.assertIsNone(services.release_blocker(self.root, self.sup()))
        self.wait_for(lambda: 'conversion hold released' in ' '.join(services.tick(self.root)['events']), what='the healthy supervisor tick')
        self.assertIsNone(self.hold())
        self.assertIsNone(old.poll())
        self.assertEqual(services.status(self.root)['services']['supervisor']['state'], 'running')

    def test_f7_a_stop_command_that_cannot_run_is_non_delivery_too(self):
        self.supervisor()
        with mock.patch.object(services.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['x'], 60)), \
                self.assertRaises(services.ServiceError):
            services.restart(self.root, 'supervisor', wait=0.5)
        self.assertEqual(services.read_record(self.root, 'supervisor')['stop_delivery']['state'], 'failed')
        self.assertNotIn('retiring', self.hold())

    def test_f7_a_failed_command_whose_stop_file_exists_was_delivered(self):
        old = self.supervisor()
        self.ctl('pipeline_supervisor', {'ignore_stop': True})

        def run(cmd, **kwargs):
            services.supervisor_stop_file(self.root).write_text('stop')               # it wrote the request, then failed
            return subprocess.CompletedProcess(cmd, 1, '', 'late failure')
        with mock.patch.object(services.subprocess, 'run', side_effect=run):
            result = services.stop(self.root, 'supervisor', wait=0.5)
        self.assertTrue(result['pending'])
        self.assertEqual(services.read_record(self.root, 'supervisor')['stop_delivery']['state'], 'delivered')
        self.assertEqual([r['pid'] for r in self.hold()['retiring']], [old.pid])       # still the outgoing generation
        services.tick(self.root, now=time.time() + 60)
        self.assertTrue(self.hold())

    def test_f7_a_pending_stop_that_never_reached_the_supervisor_is_reconciled(self):
        old = self.supervisor()
        answered = subprocess.CompletedProcess([], 0, 'supervisor: not running', '')    # exit 0, but nothing was requested
        with mock.patch.object(services.subprocess, 'run', return_value=answered):
            result = services.stop(self.root, 'supervisor', wait=0.5, request=7)
        self.assertTrue(result['pending'])
        self.assertEqual(services.read_record(self.root, 'supervisor')['stop_delivery']['state'], 'unconfirmed')
        self.assertFalse(any('released' in e for e in services.tick(self.root)['events']))      # inside the grace: still the outgoing one
        self.assertEqual([r['pid'] for r in self.hold()['retiring']], [old.pid])
        result = services.tick(self.root, now=time.time() + services.STOP_DELIVERY_GRACE + 5)
        done = result['completed']
        self.assertEqual([(d['request'], d['ok'], d['action']) for d in done], [(7, False, 'stop')])
        self.assertIn('not delivered', done[0]['text'])
        self.assertEqual(services.read_record(self.root, 'supervisor')['stop_delivery']['state'], 'failed')
        self.wait_for(lambda: self.hold() is None or 'released' in ' '.join(services.tick(self.root)['events']), what='the release')
        self.assertIsNone(self.hold())
        self.assertIsNone(old.poll())

    # ------------------------------------------------------------------------------------------------------------ finding 20
    def pending_restart(self):
        settings.save(self.root, CONVERT_ONLY)
        worker = self.spawn('convert')
        self.ctl('convert_queue', {'busy': True})
        services.tick(self.root)
        result = services.restart(self.root, 'convert', wait=0.5, request=11)
        self.assertTrue(result['pending'])
        self.assertNotIn('replacement', result)
        self.ctl('convert_queue', {})
        worker.wait(15)
        return worker

    def test_f20_a_restart_ends_with_the_replacement_identified(self):
        worker = self.pending_restart()
        done = services.tick(self.root)['completed']
        self.assertEqual([(d['request'], d['action'], d['ok']) for d in done], [(11, 'restart', True)])
        new = self.running('convert')[0]
        self.assertNotEqual(new['pid'], worker.pid)
        self.assertEqual(done[0]['replacement']['pid'], new['pid'])
        self.assertAlmostEqual(done[0]['replacement']['create_time'], new['create_time'], delta=1.5)
        self.assertIn(f'started pid {new["pid"]}', done[0]['text'])

    def test_f20_a_restart_whose_start_fails_reports_the_failure(self):
        self.pending_restart()
        with mock.patch.object(services, '_spawn', side_effect=OSError('no interpreter')):
            done = services.tick(self.root)['completed']
        self.assertEqual([(d['request'], d['ok'], d['replacement']) for d in done], [(11, False, None)])
        self.assertIn('restart failed: could not start: no interpreter', done[0]['text'])

    def test_f20_a_blocked_restart_reports_the_blocker(self):
        self.pending_restart()
        with mock.patch.object(services, '_start_blocker', return_value='an instance might run'):
            result = services.tick(self.root)
        done = result['completed']
        self.assertEqual([(d['request'], d['ok']) for d in done], [(11, False)])
        self.assertIn('restart blocked: an instance might run', done[0]['text'])
        self.assertEqual(self.running('convert'), [])
        self.assertEqual(services.tick(self.root)['completed'], [])                   # reported once; it starts when the blocker is gone
        self.assertEqual(len(self.running('convert')), 1)

    def test_f20_an_immediate_restart_names_its_replacement_or_fails(self):
        settings.save(self.root, CONVERT_ONLY)
        worker = self.spawn('convert')
        services.tick(self.root)
        result = services.restart(self.root, 'convert', wait=10)
        self.assertFalse(result['pending'])
        self.assertEqual(result['replacement']['pid'], self.running('convert')[0]['pid'])
        self.assertNotEqual(result['replacement']['pid'], worker.pid)
        with mock.patch.object(services, '_spawn', side_effect=OSError('no interpreter')), self.assertRaises(services.ServiceError):
            services.restart(self.root, 'convert', wait=10)

    def test_f20_the_manager_never_reports_a_restart_done_without_a_replacement(self):
        manager = orchestrator.ServiceManager(self.root, tick=lambda root, s: {'services': {}, 'events': [], 'completed': []})
        base = {'name': 'convert', 'action': 'restart', 'time': time.time(), 'old_pid': 4242}

        def settle(status):
            manager.events = []
            manager.outstanding = {5: dict(base, id=5)}
            manager.statuses = {'convert': status}
            manager._settle_outstanding()
            return [(e['ok'], e['pending'], e['text']) for e in manager.events]
        self.assertEqual(settle({'state': 'stopping', 'stop_pending': True, 'pid': 4242}), [])
        (ok, pending, text), = settle({'state': 'waiting', 'stop_pending': False, 'pid': None, 'message': 'x is held'})
        self.assertEqual((ok, pending), (False, False))
        self.assertIn('not restarted', text)
        self.assertFalse(settle({'state': 'running', 'stop_pending': False, 'pid': 4242})[0][0])     # the old instance: not a replacement
        ok, pending, text = settle({'state': 'running', 'stop_pending': False, 'pid': 4343})[0]
        self.assertTrue(ok)
        self.assertIn('replacement pid 4343', text)


class CutoverNamespaceTest(V2Case):
    """Finding 2: existing job / model ownership is checked under the registration lock and again at dispatch."""

    def lines(self):
        path = self.root / '.codex_jobs/_batch/convert_sets.txt'
        return path.read_text(encoding='utf-8').splitlines() if path.exists() else []

    def staged(self, parent=None, job_id='abcdef123456', suffix='_human_male'):
        return self.job('cape', job_id, 'multiview_ready', suffix=suffix, state='ready_for_tripo',
                        extra={'parent_set': parent} if parent else None)

    def test_f2_the_reviewers_case_an_independent_staged_job_is_never_authorized(self):
        self.staged(parent='cape_human_male')                                         # its record names itself as its set
        self.concept('cape')
        status, body = self.act('concepts:cape', 'approve')
        self.assertEqual((status, body['code']), (409, 'namespace'), body)
        self.assertIn('names itself as its set', body['error'])
        self.assertEqual(self.lines(), [])
        added = convert_list.add_sets(self.root, ['cape'])                            # the registration itself (e.g. Release) skips it too
        self.assertEqual(added['added'], ['cape', 'cape_dwarf_male'])
        self.assertEqual([s['id'] for s in added['skipped']['cape']], ['cape_human_male'])
        self.assertIsNone(convert_list.owner_of(self.root, 'cape_human_male'))
        self.assertEqual(convert_list.pending_jobs(self.root), [])
        self.assertEqual(convert_list.eligible_jobs(self.root), [])

    def test_f2_ownership_must_be_provable(self):
        self.staged()                                                                 # no recorded parent: ambiguous
        added = convert_list.add_sets(self.root, ['cape'])
        self.assertIn('ambiguous', added['skipped']['cape'][0]['reason'])
        self.assertIn('ambiguous', convert_list.namespace_conflict(self.root, 'cape'))
        self.staged(parent='hood', job_id='bbbbbb123456', suffix='_dwarf_male')       # another set's job
        self.assertEqual([s['id'] for s in convert_list.add_sets(self.root, ['cape'])['skipped']['cape']], ['cape_dwarf_male', 'cape_human_male'])
        # cape_dwarf_male was written before hood's job appeared: the line stays (nothing is ever removed), the dispatch check refuses the job
        self.assertEqual(self.lines(), ['cape', 'cape_dwarf_male'])
        self.assertEqual(convert_list.pending_jobs(self.root), [])
        self.assertEqual([(h['job'], h['reason']) for h in convert_list.held_jobs(self.root)],
                         [('cape_dwarf_male--bbbbbb123456', 'owner'), ('cape_human_male--abcdef123456', 'unlisted')])

    def test_f2_a_job_that_records_this_set_as_its_parent_is_accepted(self):
        self.staged(parent='cape')
        self.concept('cape')
        status, result = self.act('concepts:cape', 'approve')
        self.assertEqual(status, 200, result)
        self.assertEqual(result['convert_list']['added'], fam('cape'))
        self.assertEqual([j['job'] for j in convert_list.pending_jobs(self.root)], ['cape_human_male--abcdef123456'])
        self.assertEqual(convert_list.held_jobs(self.root), [])

    def test_f2_a_model_folder_without_a_job_record_is_ambiguous(self):
        (self.root / 'models/cape_dwarf_male/aaaaaa111111').mkdir(parents=True)
        added = convert_list.add_sets(self.root, ['cape'])
        self.assertEqual([s['id'] for s in added['skipped']['cape']], ['cape_dwarf_male'])
        self.assertIn('models/cape_dwarf_male', added['skipped']['cape'][0]['reason'])
        for stage in ('completed', 'failed', 'held_conversions', 'processing', 'processing_views'):   # every job stage is looked at
            self.job('wrap', 'cccccc123456', stage, suffix='_human_male', extra={'parent_set': 'other'})
            self.assertIn(f'{stage}/wrap_human_male--cccccc123456', convert_list.namespace_conflict(self.root, 'wrap'), stage)
            shutil.rmtree(self.root / stage / 'wrap_human_male--cccccc123456')

    def test_f2_an_unreadable_namespace_blocks_registration_and_is_never_replaced(self):
        convert_list.add_sets(self.root, ['hood'])
        path = convert_list.namespace_path(self.root)
        for broken in ('{"sets": {"hood": ', '[1, 2]', '{"sets": {}, "families": {"a b": "hood"}}'):
            path.write_text(broken, encoding='utf-8')
            before = self.lines()
            with self.assertRaises(convert_list.NamespaceUnreadable):
                convert_list.add_sets(self.root, ['cape'])
            self.assertEqual(path.read_text(encoding='utf-8'), broken)                # not replaced
            self.assertEqual(self.lines(), before)                                    # nothing written to the convert list either
            self.assertIn('set_namespace.json', convert_list.namespace_conflict(self.root, 'cape'))
        self.concept('cape')
        status, body = self.act('concepts:cape', 'approve')
        self.assertEqual((status, body['code']), (409, 'namespace'), body)
        self.assertTrue((self.root / 'concepts/cape').is_dir())

    def test_f2_the_recorded_owner_is_verified_at_dispatch(self):
        """A convert list that already carries the family id (written before this fix, or by hand) authorizes only the owner's job."""
        import convert_queue
        self.staged(parent='cape_human_male')
        convert_list.list_path(self.root).parent.mkdir(parents=True, exist_ok=True)
        convert_list.list_path(self.root).write_text('cape\ncape_human_male\n', encoding='utf-8')
        convert_list.namespace_path(self.root).write_text(json.dumps({'sets': {'cape': 'x'}, 'families': {'cape_human_male': 'cape'}}))
        pats = convert_list.patterns(self.root)
        job = 'cape_human_male--abcdef123456'
        ok, reason = convert_queue.owner_allows(self.root, job, {'parent_set': 'cape_human_male'}, pats)
        self.assertFalse(ok)
        self.assertIn('for the set cape', reason)
        self.assertEqual(convert_list.pending_jobs(self.root), [])
        self.assertEqual(convert_list.eligible_jobs(self.root), [])
        held = convert_list.held_jobs(self.root)
        self.assertEqual([(h['job'], h['reason']) for h in held], [(job, 'owner')])
        self.assertIn('for the set cape', held[0]['detail'])
        self.assertTrue(convert_queue.owner_allows(self.root, job, {'parent_set': 'cape'}, pats)[0])              # cape's own job
        self.assertTrue(convert_queue.owner_allows(self.root, job, {}, pats)[0])                                  # registered as cape's
        self.assertFalse(convert_queue.owner_allows(self.root, job, {'parent_set': 'hood'}, pats)[0])             # hood is not listed
        self.assertFalse(convert_queue.owner_allows(self.root, job, {}, ['cape_human_male'])[0])                  # its owner cape is not listed
        self.assertTrue(convert_queue.owner_allows(self.root, 'solo--a1', {'parent_set': 'solo'}, ['solo'])[0])   # names itself, own id listed
        self.assertTrue(convert_queue.owner_allows(self.root, 'solo--a1', {}, ['solo'])[0])
        convert_list.namespace_path(self.root).write_text('{broken', encoding='utf-8')
        ok, reason = convert_queue.owner_allows(self.root, job, {'parent_set': 'cape'}, pats)
        self.assertFalse(ok)
        self.assertIn('unreadable', reason)
        # the worker's own loop applies it
        with mock.patch.object(convert_queue, 'ROOT', self.root), mock.patch.object(convert_queue, 'LOCKS', self.root / '.codex_jobs/_batch/convert_locks'), \
                mock.patch.object(convert_queue, 'EVENTS', self.root / '.codex_jobs/_batch/convert_events.log'), \
                mock.patch.object(convert_queue, '_OWNER_REPORTED', set()), mock.patch('builtins.print'):
            convert_list.namespace_path(self.root).write_text(json.dumps({'sets': {'cape': 'x'}, 'families': {'cape_human_male': 'cape'}}))
            self.assertEqual(convert_queue.next_job(set(pats)), (None, False))
        self.assertIn(f'OWNER {job}: not converted', (self.root / '.codex_jobs/_batch/convert_events.log').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
