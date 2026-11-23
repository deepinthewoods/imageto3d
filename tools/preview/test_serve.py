"""Run with python3 -m unittest tools/preview/test_serve.py."""
import http.client
from http.server import ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

SPEC = importlib.util.spec_from_file_location('preview_serve', Path(__file__).with_name('serve.py'))
serve = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(serve)

STUB = '''import json, pathlib, sys
args=sys.argv[1:]
root=pathlib.Path(args[args.index('--root')+1]); command=args[args.index('--root')+2]
if command=='check':
 p=pathlib.Path(args[-1]);
 if p.name=='bad': print('invalid set',file=sys.stderr);sys.exit(2)
 print(json.dumps({'set':p.name,'valid':True})); sys.exit(0)
if command=='promote':
 batch=pathlib.Path(args[args.index('promote')+1]); number=args[args.index('promote')+2]
 files=list(batch.glob(number+'_*__*.png'))
 if not files: print('missing images',file=sys.stderr);sys.exit(2)
 key=files[0].name.split('__')[0]; record=batch/(key+'.json')
 if not record.exists(): print('Missing record',file=sys.stderr);sys.exit(2)
 if len(files)!=2: print('Incomplete pair',file=sys.stderr);sys.exit(2)
 target=root/'approved'/(batch.name+'_'+key)
 if target.exists(): print('already exists',file=sys.stderr);sys.exit(2)
 target.joinpath('human_male').mkdir(parents=True)
 for f in files: target.joinpath('human_male',f.name.split('__')[1].replace('.png','_front.png')).write_bytes(f.read_bytes())
 print(json.dumps({'set':target.name,'stage':'approved'}))
'''


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ('concepts', 'provisional', 'approved', 'trash/concepts', 'archive/concepts', 'base_views', 'tools/tripo_pipeline', 'tools/preview'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        (self.root / 'tools/tripo_pipeline/workflow.py').write_text(STUB)
        class TestHandler(serve.Handler):
            root = self.root
            def log_message(self, *_): pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), TestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def req(self, method, path, payload=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        body = json.dumps(payload).encode() if payload is not None else None
        conn.request(method, path, body=body, headers={'Content-Type':'application/json'} if body else {})
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response.status, json.loads(raw) if path.startswith('/api/') else raw

    def concept(self, name='one'):
        race = self.root / 'concepts' / name / 'human_male'
        race.mkdir(parents=True)
        (race / 'outfit_front.png').write_bytes(b'png')
        (race / 'outfit_prompts.json').write_text('{"brief":"robe","attempt_count":2}')

    def test_approve_unapprove_and_warning(self):
        self.concept()
        status, result = self.req('POST','/api/approve',{'id':'concepts:one'})
        self.assertEqual(status,200)
        self.assertTrue(result['check']['valid'])
        self.assertEqual(result['undo_id'],'provisional:one')
        self.assertFalse((self.root/'concepts/one').exists())
        self.assertTrue((self.root/'provisional/one').exists())
        status, result = self.req('POST','/api/approve',{'id':'provisional:one'})
        self.assertEqual(status,200)
        self.assertEqual(result['undo_id'],'approved:one')
        self.assertTrue((self.root/'approved/one').exists())
        status, result = self.req('POST','/api/unapprove',{'id':'approved:one'})
        self.assertEqual(status,200)
        self.assertTrue((self.root/'provisional/one').exists())
        status, result = self.req('POST','/api/unapprove',{'id':'provisional:one'})
        self.assertEqual(status,200)
        self.assertTrue((self.root/'concepts/one').exists())
        self.assertEqual(len((self.root/'tools/preview/actions.jsonl').read_text().splitlines()),4)

    def test_approval_validation_warning_keeps_move(self):
        self.concept('bad')
        status,result=self.req('POST','/api/approve',{'id':'concepts:bad'})
        self.assertEqual(status,200)
        self.assertIn('invalid set',result['warning'])
        self.assertTrue((self.root/'provisional/bad').exists())

    def test_reject_restore(self):
        self.concept()
        status, result = self.req('POST','/api/reject',{'id':'concepts:one'})
        self.assertEqual(status,200)
        self.assertTrue((self.root/'trash/concepts/one/.preview-origin.json').exists())
        status, result = self.req('POST','/api/restore',{'id':result['undo_id']})
        self.assertEqual(status,200)
        self.assertTrue((self.root/'concepts/one').exists())

    def test_clashes_and_claim_guard(self):
        self.concept()
        (self.root/'provisional/one').mkdir(parents=True)
        status,_=self.req('POST','/api/approve',{'id':'concepts:one'})
        self.assertEqual(status,400)
        self.assertTrue((self.root/'concepts/one').exists())
        (self.root/'provisional/one').rmdir()
        self.req('POST','/api/approve',{'id':'concepts:one'})
        self.req('POST','/api/approve',{'id':'provisional:one'})
        (self.root/'provisional/one').mkdir()
        status,_=self.req('POST','/api/unapprove',{'id':'approved:one'})
        self.assertEqual(status,400)
        (self.root/'provisional/one').rmdir()
        (self.root/'approved/one/.tripo-job.json').write_text('{}')
        status,_=self.req('POST','/api/unapprove',{'id':'approved:one'})
        self.assertEqual(status,400)

    def test_legacy_promote_and_missing_record(self):
        batch=self.root/'concepts/batch'
        batch.mkdir()
        for module in ('body_armor','right_gauntlet_boot'):
            (batch/f'01_plate__{module}.png').write_bytes(b'png')
        status,result=self.req('POST','/api/approve',{'id':'legacy:batch:01_plate'})
        self.assertEqual(status,400)
        self.assertIn('Missing record',result['error'])
        self.assertTrue((batch/'01_plate__body_armor.png').exists())
        (batch/'01_plate.json').write_text('{"race_gender":"human_male"}')
        state=serve.state(self.root)
        self.assertEqual(state['concepts'][0]['id'],'legacy:batch:01_plate')
        status,result=self.req('POST','/api/approve',{'id':'legacy:batch:01_plate'})
        self.assertEqual(status,200)
        self.assertTrue((self.root/'approved/batch_01_plate').exists())
        self.assertFalse((batch/'01_plate.json').exists())
        self.assertTrue(result['undo_id'].startswith('trashlegacy:'))

    def test_legacy_reject_restore(self):
        batch=self.root/'concepts/batch';batch.mkdir()
        (batch/'01_plate__outfit.png').write_bytes(b'png')
        (batch/'01_plate.json').write_text('{}')
        status,result=self.req('POST','/api/reject',{'id':'legacy:batch:01_plate'})
        self.assertEqual(status,200)
        status,_=self.req('POST','/api/restore',{'id':result['undo_id']})
        self.assertEqual(status,200)
        self.assertTrue((batch/'01_plate__outfit.png').exists())

    def test_archive_unarchive_set(self):
        self.concept()
        status, result = self.req('POST','/api/archive',{'id':'concepts:one'})
        self.assertEqual(status,200)
        self.assertEqual(result['dst'],'archive/concepts/one')
        self.assertEqual(result['undo_id'],'archive:one')
        self.assertFalse((self.root/'concepts/one').exists())
        state=serve.state(self.root)
        self.assertEqual([c['id'] for c in state['archive']],['archive:one'])
        self.assertEqual(state['concepts'],[])
        status, result = self.req('POST','/api/unarchive',{'id':'archive:one'})
        self.assertEqual(status,200)
        self.assertTrue((self.root/'concepts/one/human_male/outfit_front.png').exists())
        self.assertFalse((self.root/'archive/concepts/one').exists())
        log=[json.loads(l) for l in (self.root/'tools/preview/actions.jsonl').read_text().splitlines()]
        self.assertEqual([l['action'] for l in log],['archive','unarchive'])

    def test_approve_from_archive(self):
        self.concept()
        self.req('POST','/api/archive',{'id':'concepts:one'})
        status, result = self.req('POST','/api/approve',{'id':'archive:one'})
        self.assertEqual(status,200)
        self.assertTrue(result['check']['valid'])
        self.assertTrue((self.root/'provisional/one').exists())
        self.assertFalse((self.root/'archive/concepts/one').exists())
        self.concept('bad')
        self.req('POST','/api/archive',{'id':'concepts:bad'})
        status, result = self.req('POST','/api/approve',{'id':'archive:bad'})
        self.assertEqual(status,200)
        self.assertIn('invalid set',result['warning'])
        self.assertTrue((self.root/'provisional/bad').exists())

    def test_archive_refuses_overwrite(self):
        self.concept()
        (self.root/'archive/concepts/one').mkdir()
        status,_=self.req('POST','/api/archive',{'id':'concepts:one'})
        self.assertEqual(status,400)
        self.assertTrue((self.root/'concepts/one').exists())
        (self.root/'archive/concepts/one').rmdir()
        self.req('POST','/api/archive',{'id':'concepts:one'})
        self.concept()
        status,_=self.req('POST','/api/unarchive',{'id':'archive:one'})
        self.assertEqual(status,400)
        self.assertTrue((self.root/'archive/concepts/one').exists())
        (self.root/'provisional/one').mkdir()
        status,_=self.req('POST','/api/approve',{'id':'archive:one'})
        self.assertEqual(status,400)
        self.assertTrue((self.root/'archive/concepts/one').exists())
        status,_=self.req('POST','/api/archive',{'id':'archive:one'})
        self.assertEqual(status,400)
        status,_=self.req('POST','/api/unarchive',{'id':'concepts:one'})
        self.assertEqual(status,400)

    def test_legacy_archive_browse_approve(self):
        batch=self.root/'archive/concepts/batch';batch.mkdir()
        for module in ('body_armor','right_gauntlet_boot'):
            (batch/f'01_plate__{module}.png').write_bytes(b'png')
        (batch/'01_plate.json').write_text('{"race_gender":"human_male"}')
        state=serve.state(self.root)
        self.assertEqual(state['archive'][0]['id'],'archlegacy:batch:01_plate')
        self.assertEqual(state['concepts'],[])
        status,result=self.req('POST','/api/unarchive',{'id':'archlegacy:batch:01_plate'})
        self.assertEqual(status,200)
        self.assertTrue((self.root/'concepts/batch/01_plate.json').exists())
        status,result=self.req('POST','/api/archive',{'id':result['undo_id']})
        self.assertEqual(status,200)
        self.assertTrue((batch/'01_plate__outfit.png').exists() or (batch/'01_plate__body_armor.png').exists())
        # destination clash is refused and nothing moves
        (self.root/'concepts/batch').mkdir(exist_ok=True)
        (self.root/'concepts/batch/01_plate.json').write_text('{}')
        status,_=self.req('POST','/api/unarchive',{'id':'archlegacy:batch:01_plate'})
        self.assertEqual(status,400)
        self.assertTrue((batch/'01_plate__body_armor.png').exists())
        (self.root/'concepts/batch/01_plate.json').unlink()
        status,result=self.req('POST','/api/approve',{'id':'archlegacy:batch:01_plate'})
        self.assertEqual(status,200)
        self.assertTrue((self.root/'approved/batch_01_plate').exists())
        self.assertFalse((batch/'01_plate.json').exists())
        origin=json.loads((self.root/'trash/concepts/batch/01_plate.preview-origin.json').read_text())
        self.assertEqual(origin['src'],'archive/concepts/batch')
        status,_=self.req('POST','/api/restore',{'id':result['undo_id']})
        self.assertEqual(status,200)
        self.assertTrue((batch/'01_plate.json').exists())

    def test_traversal_and_symlink_escape(self):
        self.concept()
        for bad in ('concepts:..','concepts:../one','concepts:one/../../x'):
            status,_=self.req('POST','/api/reject',{'id':bad})
            self.assertEqual(status,400)
        outside=self.root.parent/'preview_outside_test.png'
        outside.write_bytes(b'outside')
        try:
            (self.root/'concepts/leak.png').symlink_to(outside)
            status,_=self.req('GET','/files/concepts/leak.png')
            self.assertEqual(status,403)
            status,_=self.req('GET','/files/%2e%2e/%2e%2e/etc/passwd')
            self.assertEqual(status,403)
        finally:
            outside.unlink()

    def finish(self, job, set_name='set_a'):
        """Mark a models/<set>/<job> as completed (what finish_sets.py does): its job folder is in completed/."""
        (self.root / 'completed' / f'{set_name}--{job}').mkdir(parents=True, exist_ok=True)

    def model(self, job, race, module, set_name='set_a', complete=True):
        reduced = self.root / 'models' / set_name / job / race / module / 'reduced'
        reduced.mkdir(parents=True)
        if complete:
            self.finish(job, set_name)
        (reduced / 'lod_error_0p4__wearable.glb').write_bytes(b'glb')
        return reduced / 'lod_error_0p4__wearable.glb'

    def test_models_grouped_by_module(self):
        for race in ('human_male', 'dwarf_male'):
            for module in ('right_gauntlet_boot', 'body_armor'):
                self.model(f'set_a--{race}', race, module)
        _, data = self.req('GET', '/api/state')
        order = [(e['module'], e['race']) for e in data['models'][0]['entries']]
        self.assertEqual(order, [('body_armor', 'dwarf_male'), ('body_armor', 'human_male'),
                                 ('right_gauntlet_boot', 'dwarf_male'), ('right_gauntlet_boot', 'human_male')])

    def test_models_tab_only_lists_completed_jobs(self):
        self.model('set_a--done', 'human_male', 'body_armor')
        self.model('set_a--wip', 'orc_male', 'body_armor', complete=False)
        self.model('set_b--wip', 'human_male', 'body_armor', set_name='set_b', complete=False)
        self.model('set_c--held', 'gnome_female', 'outfit', set_name='set_c', complete=False)
        _, data = self.req('GET', '/api/state')
        self.assertEqual([c['id'] for c in data['models']], ['model:set_a'])
        self.assertEqual([e['job'] for e in data['models'][0]['entries']], ['set_a--done'])
        self.assertEqual(data['approved_models'], [])
        self.finish('set_b--wip', 'set_b')
        _, data = self.req('GET', '/api/state')
        self.assertEqual([c['id'] for c in data['models']], ['model:set_a', 'model:set_b'])

    def test_models_tab_shows_only_the_newest_conversion_of_a_race_and_module(self):
        old = self.model('set_a--hathead', 'gnome_female', 'outfit')
        self.model('set_a--hathead', 'human_male', 'outfit')
        new = self.model('set_a--footless', 'gnome_female', 'outfit')
        for glb, at in ((old, 1000), (new, 2000)):
            manifest = glb.parent / 'manifest.json'
            manifest.write_text('{}')
            os.utime(manifest, (at, at))
        _, data = self.req('GET', '/api/state')
        entries = {(e['race'], e['job']) for e in data['models'][0]['entries']}
        self.assertEqual(entries, {('gnome_female', 'set_a--footless'), ('human_male', 'set_a--hathead')})

    def test_model_approve_and_unapprove(self):
        self.model('set_a--done', 'human_male', 'body_armor')
        self.model('set_a--wip', 'orc_male', 'body_armor', complete=False)
        self.model('set_b--done', 'human_male', 'body_armor', set_name='set_b')
        status, result = self.req('POST', '/api/approve', {'id': 'model:set_a'})
        self.assertEqual((status, result['undo_id']), (200, 'approvedmodel:set_a'))
        marker = json.loads((self.root / 'models/set_a/model_approval.json').read_text())
        self.assertEqual((marker['approved'], marker['set'], marker['jobs']), (True, 'set_a', ['set_a--done']))
        self.assertIn('T', marker['time'])
        _, data = self.req('GET', '/api/state')
        self.assertEqual([c['id'] for c in data['models']], ['model:set_b'])
        self.assertEqual([c['id'] for c in data['approved_models']], ['approvedmodel:set_a'])
        self.assertEqual([e['job'] for e in data['approved_models'][0]['entries']], ['set_a--done'])
        self.assertTrue(data['approved_models'][0]['entries'][0]['variants']['0p4']['wearable'])
        _, fast = self.req('GET', '/api/state?models=0')
        self.assertNotIn('approved_models', fast)
        status, result = self.req('POST', '/api/unapprove', {'id': 'approvedmodel:set_a'})
        self.assertEqual((status, result['undo_id']), (200, 'model:set_a'))
        self.assertFalse((self.root / 'models/set_a/model_approval.json').exists())
        _, data = self.req('GET', '/api/state')
        self.assertEqual([c['id'] for c in data['models']], ['model:set_a', 'model:set_b'])
        self.assertEqual(data['approved_models'], [])
        log = [json.loads(l) for l in (self.root / 'tools/preview/actions.jsonl').read_text().splitlines()]
        self.assertEqual([(l['action'], l['result']) for l in log], [('approve', 'ok'), ('unapprove', 'ok')])
        self.assertEqual(log[0]['src'], 'models/set_a')
        self.assertEqual(log[0]['dst'], 'models/set_a/model_approval.json')

    def test_model_approve_refusals(self):
        self.model('set_a--wip', 'human_male', 'body_armor', complete=False)
        self.model('set_b--done', 'human_male', 'body_armor', set_name='set_b')
        status, _ = self.req('POST', '/api/approve', {'id': 'model:set_a'})          # no completed job
        self.assertEqual(status, 400)
        self.assertFalse((self.root / 'models/set_a/model_approval.json').exists())
        status, _ = self.req('POST', '/api/approve', {'id': 'model:nope'})
        self.assertEqual(status, 400)
        self.assertEqual(self.req('POST', '/api/approve', {'id': 'model:set_b'})[0], 200)
        status, result = self.req('POST', '/api/approve', {'id': 'model:set_b'})       # double approve
        self.assertEqual(status, 400)
        self.assertIn('already approved', result['error'])
        for verb, bad in (('unapprove', 'approvedmodel:set_a'), ('unapprove', 'model:set_b'), ('approve', 'approvedmodel:set_b'),
                          ('approve', 'model:..'), ('approve', 'model:set_b:x'), ('unapprove', 'approvedmodel:..')):
            self.assertEqual(self.req('POST', f'/api/{verb}', {'id': bad})[0], 400, bad)
        self.assertTrue((self.root / 'models/set_b/model_approval.json').exists())
        log = [json.loads(l) for l in (self.root / 'tools/preview/actions.jsonl').read_text().splitlines()]
        self.assertEqual(sum(l['result'] == 'ok' for l in log), 1)
        self.assertTrue(log[0]['result'].startswith('error: '))

    def test_approval_marker_must_say_approved(self):
        self.model('set_a--done', 'human_male', 'body_armor')
        (self.root / 'models/set_a/model_approval.json').write_text('{"approved": false}')
        _, data = self.req('GET', '/api/state')
        self.assertEqual((len(data['models']), data['approved_models']), (1, []))
        self.assertEqual(self.req('POST', '/api/approve', {'id': 'model:set_a'})[0], 200)

    def test_pipeline_waiting_for(self):
        for race in ('human_male', 'orc_male', 'gnome_female'):
            (self.root / 'base_views' / race).mkdir(parents=True)
            (self.root / 'base_views' / race / 'palette.json').write_text('{}')
        (self.root / 'base_views/old_race').mkdir()                                  # no palette.json: not a required race

        def job(stage, name, race, state=None):
            folder = self.root / stage / name
            (folder / race / 'outfit').mkdir(parents=True)
            (folder / race / 'outfit' / 'front.png').write_bytes(b'png')
            if state:
                (folder / '.tripo-job.json').write_text(json.dumps({'state': state}))
        waiting = lambda: {j['name']: j['waiting_for'] for j in self.req('GET', '/api/state')[1]['pipeline']}
        job('processing', 'plain--aaa', 'human_male', 'converted')
        self.assertEqual(waiting()['plain--aaa'], ['gnome_female (not converted)', 'orc_male (not converted)'])   # gnomes are required too
        job('processing_views', 'plain--bbb', 'orc_male')
        job('multiview_ready', 'plain--mmm', 'orc_male')
        job('processing', 'plain_gnome_female--ccc', 'gnome_female', 'converting')   # gnome suffix: same set as `plain`
        job('processing', 'other--ddd', 'orc_male')                                   # another set: ignored
        job('failed', 'plain--fff', 'orc_male')
        result = waiting()
        self.assertEqual(sorted(result['plain--aaa']), sorted(['gnome_female (not converted)', 'orc_male (not converted)', 'plain--bbb (generating views)',
                                                               'plain--mmm (generating views)', 'plain_gnome_female--ccc (converting)']))
        self.assertEqual((result['plain--bbb'], result['other--ddd']), ([], []))     # only converted processing jobs get a list
        job('processing', 'plain--eee', 'orc_male', 'converted')                      # orc is converted now; the unfinished jobs remain
        result = waiting()
        self.assertEqual(sorted(result['plain--aaa']), ['gnome_female (not converted)', 'plain--bbb (generating views)', 'plain--mmm (generating views)', 'plain_gnome_female--ccc (converting)'])
        self.assertEqual(result['plain--eee'], result['plain--aaa'])
        for stage, name in (('processing_views', 'plain--bbb'), ('multiview_ready', 'plain--mmm')):
            import shutil
            shutil.rmtree(self.root / stage / name)
        (self.root / 'processing/plain_gnome_female--ccc/.tripo-job.json').write_text('{"state": "converted"}')
        result = waiting()
        self.assertEqual((result['plain--aaa'], result['plain--eee']), ([], []))       # complete: nothing keeps it from `completed/`
        # a race covered only by a job already in completed/ counts too
        import shutil
        shutil.rmtree(self.root / 'processing/plain--eee')
        self.assertEqual(waiting()['plain--aaa'], ['orc_male (not converted)'])
        (self.root / 'completed/plain--zzz/orc_male/outfit').mkdir(parents=True)
        (self.root / 'completed/plain--zzz/orc_male/outfit/front.png').write_bytes(b'png')
        self.assertEqual(waiting()['plain--aaa'], [])

    def test_face_marks_roundtrip(self):
        glb = self.model('set_a--job', 'human_male', 'body_armor')
        url = '/files/models/set_a/set_a--job/human_male/body_armor/reduced/lod_error_0p4__wearable.glb'
        status, data = self.req('GET', '/api/marks?url=' + url)
        self.assertEqual((status, data['faces']), (200, []))
        face = {'mesh_index': 0, 'mesh': 'Body', 'triangle': 7, 'category': 'extra', 'centroid': [0.1, 1.2, -0.3]}
        status, data = self.req('POST', '/api/marks', {'url': url, 'faces': [face, face | {'triangle': 9, 'category': 'misc'}]})
        self.assertEqual((status, data['counts']), (200, {'extra': 1, 'misc': 1}))
        record = json.loads(glb.with_name('lod_error_0p4__wearable.marks.json').read_text())
        self.assertEqual(record['model'], 'models/set_a/set_a--job/human_male/body_armor/reduced/lod_error_0p4__wearable.glb')
        _, data = self.req('GET', '/api/marks?url=' + url)
        self.assertEqual([f['triangle'] for f in data['faces']], [7, 9])
        for bad in ({'url': url, 'faces': [face | {'category': 'other'}]}, {'url': url, 'faces': [face | {'triangle': -1}]},
                    {'url': '/files/concepts/x.glb', 'faces': []}, {'url': '/files/models/../concepts/x.glb', 'faces': []}):
            status, _ = self.req('POST', '/api/marks', bad)
            self.assertEqual(status, 400)

    def test_chosen_lod_saved_per_model(self):
        self.model('set_a--job', 'human_male', 'body_armor')
        ids = {'set': 'set_a', 'job': 'set_a--job', 'race': 'human_male', 'module': 'body_armor'}
        status, _ = self.req('POST', '/api/lod', ids | {'lod': '0p2'})
        self.assertEqual(status, 200)
        _, data = self.req('GET', '/api/state')
        self.assertEqual(data['models'][0]['entries'][0]['lod'], '0p2')
        for bad in (ids | {'lod': '0p3'}, ids | {'race': '..', 'lod': '0p2'}, ids | {'module': 'outfit', 'lod': '0p2'}):
            status, _ = self.req('POST', '/api/lod', bad)
            self.assertEqual(status, 400)

    def test_model_preview_thumbnail_cache(self):
        glb = self.model('set_a--job', 'human_male', 'body_armor')
        full = glb.with_name('lod_error_0p4.glb')
        full.write_bytes(b'glb')
        self.model('set_a--job', 'human_male', 'right_gauntlet_boot').with_name('lod_error_0p4.glb').write_bytes(b'glb')
        _, data = self.req('GET', '/api/state')
        previews = data['models'][0]['previews']
        self.assertEqual([p['image'] for p in previews], [None, None])
        self.assertTrue(previews[0]['model'].endswith('/body_armor/reduced/lod_error_0p4.glb'))
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        conn.request('POST', '/api/thumbnail?url=' + previews[0]['model'], body=b'\x89PNG\r\n\x1a\nxx', headers={'Content-Type': 'image/png'})
        response = conn.getresponse(); result = json.loads(response.read()); conn.close()
        self.assertEqual(response.status, 200)
        self.assertTrue(full.with_name('lod_error_0p4.thumb.png').is_file())
        _, data = self.req('GET', '/api/state')
        self.assertEqual(data['models'][0]['previews'][0]['image'], result['image'])
        import os
        os.utime(full, (full.stat().st_mtime + 10,) * 2)  # model rebuilt after the render: stale
        _, data = self.req('GET', '/api/state')
        self.assertIsNone(data['models'][0]['previews'][0]['image'])
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        conn.request('POST', '/api/thumbnail?url=' + previews[0]['model'], body=b'not png')
        self.assertEqual(conn.getresponse().status, 400); conn.close()

    def fitted_module(self, with_pieces=True):
        """A module with an unfitted wearable (mtime 1000), a fitted one (2000), pieces and fitted pieces."""
        import os
        module = self.root / 'models/set_a/set_a--job/human_male/body_armor'
        reduced = module / 'reduced'
        reduced.mkdir(parents=True)
        self.finish('set_a--job')
        made = {'plain': reduced / 'lod_error_0p4__wearable.glb', 'fitted': reduced / 'lod_error_0p4__wearable_fitted.glb'}
        for key, mtime in (('plain', 1000), ('fitted', 2000)):
            made[key].write_bytes(b'glb')
            os.utime(made[key], (mtime, mtime))
        if with_pieces:
            for sub, mtime in (('lod_0p4', 1000), ('lod_0p4_fitted', 2000)):
                (module / 'segment_first/pieces' / sub).mkdir(parents=True)
                for name in ('chest', 'pelvis'):
                    f = module / 'segment_first/pieces' / sub / f'{name}.glb'
                    f.write_bytes(b'glb')
                    os.utime(f, (mtime, mtime))
        return module, made

    URL = '/files/models/set_a/set_a--job/human_male/body_armor/'

    def test_fitted_variants_and_freshness(self):
        import os
        module, made = self.fitted_module()
        _, data = self.req('GET', '/api/state')
        entry = data['models'][0]['entries'][0]
        self.assertEqual(entry['variants']['0p4']['fitted'], self.URL + 'reduced/lod_error_0p4__wearable_fitted.glb')
        self.assertEqual(entry['pieces']['0p4'], [self.URL + f'segment_first/pieces/lod_0p4/{n}.glb' for n in ('chest', 'pelvis')])
        self.assertEqual(entry['pieces_fitted']['0p4'], [self.URL + f'segment_first/pieces/lod_0p4_fitted/{n}.glb' for n in ('chest', 'pelvis')])
        self.assertNotIn('fitted', entry['variants']['0p2'])
        self.assertNotIn('0p2', entry['pieces_fitted'])
        os.utime(made['plain'], (3000, 3000))                       # unfitted rebuilt after the fit: stale
        os.utime(module / 'segment_first/pieces/lod_0p4/pelvis.glb', (3000, 3000))
        _, data = self.req('GET', '/api/state')
        entry = data['models'][0]['entries'][0]
        self.assertNotIn('fitted', entry['variants']['0p4'])
        self.assertIn('wearable', entry['variants']['0p4'])
        self.assertEqual(entry['pieces_fitted'], {})                # one stale twin: pieces_fitted is all or nothing
        self.assertEqual(len(entry['pieces']['0p4']), 2)

    def test_aligned_variants_appear_only_when_files_exist_and_are_fresh(self):
        import os
        module, made = self.fitted_module()
        _, data = self.req('GET', '/api/state')
        entry = data['models'][0]['entries'][0]
        for key in ('aligned', 'aligned_fitted'):
            self.assertNotIn(key, entry['variants']['0p4'])
        self.assertEqual((entry['pieces_aligned'], entry['pieces_aligned_fitted']), ({}, {}))
        reduced = module / 'reduced'
        files = {'aligned': reduced / 'lod_error_0p4__wearable_aligned.glb', 'aligned_fitted': reduced / 'lod_error_0p4__wearable_aligned_fitted.glb'}
        for key, mtime in (('aligned', 1500), ('aligned_fitted', 2500)):
            files[key].write_bytes(b'glb')
            os.utime(files[key], (mtime, mtime))
        for sub, mtime in (('lod_0p4_aligned', 1500), ('lod_0p4_aligned_fitted', 2500)):
            (module / 'segment_first/pieces' / sub).mkdir()
            for name in ('chest', 'pelvis'):
                f = module / 'segment_first/pieces' / sub / f'{name}.glb'
                f.write_bytes(b'glb')
                os.utime(f, (mtime, mtime))
        _, data = self.req('GET', '/api/state')
        entry = data['models'][0]['entries'][0]
        self.assertEqual(entry['variants']['0p4']['aligned'], self.URL + 'reduced/lod_error_0p4__wearable_aligned.glb')
        self.assertEqual(entry['variants']['0p4']['aligned_fitted'], self.URL + 'reduced/lod_error_0p4__wearable_aligned_fitted.glb')
        self.assertEqual(entry['pieces_aligned']['0p4'], [self.URL + f'segment_first/pieces/lod_0p4_aligned/{n}.glb' for n in ('chest', 'pelvis')])
        self.assertEqual(entry['pieces_aligned_fitted']['0p4'], [self.URL + f'segment_first/pieces/lod_0p4_aligned_fitted/{n}.glb' for n in ('chest', 'pelvis')])
        self.assertIn('fitted', entry['variants']['0p4'])                       # the existing variants are unchanged
        self.assertEqual(len(entry['pieces_fitted']['0p4']), 2)
        # the aligned wearable is redone after the aligned+fitted one was made: only the aligned+fitted twins turn stale
        os.utime(files['aligned'], (3000, 3000))
        os.utime(module / 'segment_first/pieces/lod_0p4_aligned/pelvis.glb', (3000, 3000))
        _, data = self.req('GET', '/api/state')
        entry = data['models'][0]['entries'][0]
        self.assertIn('aligned', entry['variants']['0p4'])
        self.assertNotIn('aligned_fitted', entry['variants']['0p4'])
        self.assertIn('0p4', entry['pieces_aligned'])
        self.assertEqual(entry['pieces_aligned_fitted'], {})
        # the original wearable is newer than the aligned one: the aligned variants are stale
        os.utime(made['plain'], (4000, 4000))
        _, data = self.req('GET', '/api/state')
        self.assertNotIn('aligned', data['models'][0]['entries'][0]['variants']['0p4'])

    def test_red_body_overlays_appear_only_when_files_exist_and_are_fresh(self):
        import os
        module, made = self.fitted_module()
        reduced = module / 'reduced'
        labelled = reduced / 'lod_error_0p4.glb'
        labelled.write_bytes(b'glb')
        os.utime(labelled, (1000, 1000))
        os.utime(made['plain'], (1000, 1000))
        _, data = self.req('GET', '/api/state')
        variants = data['models'][0]['entries'][0]['variants']['0p4']
        self.assertNotIn('red_body', variants)
        self.assertNotIn('red_body_aligned', variants)
        files = {'red_body': reduced / 'lod_error_0p4__tripo_body.glb', 'red_body_aligned': reduced / 'lod_error_0p4__tripo_body_aligned.glb',
                 'aligned': reduced / 'lod_error_0p4__wearable_aligned.glb'}
        for key, mtime in (('aligned', 1500), ('red_body', 2000), ('red_body_aligned', 2000)):
            files[key].write_bytes(b'glb')
            os.utime(files[key], (mtime, mtime))
        _, data = self.req('GET', '/api/state')
        variants = data['models'][0]['entries'][0]['variants']['0p4']
        self.assertEqual(variants['red_body'], self.URL + 'reduced/lod_error_0p4__tripo_body.glb')
        self.assertEqual(variants['red_body_aligned'], self.URL + 'reduced/lod_error_0p4__tripo_body_aligned.glb')
        self.assertNotIn('red_body', data['models'][0]['entries'][0]['variants']['0p2'])
        os.utime(files['aligned'], (3000, 3000))                       # the aligned wearable was redone after the overlay: only the aligned overlay is stale
        _, data = self.req('GET', '/api/state')
        variants = data['models'][0]['entries'][0]['variants']['0p4']
        self.assertIn('red_body', variants)
        self.assertNotIn('red_body_aligned', variants)
        os.utime(labelled, (4000, 4000))                               # the labelled LOD was rewritten: both are stale
        _, data = self.req('GET', '/api/state')
        variants = data['models'][0]['entries'][0]['variants']['0p4']
        self.assertEqual(sorted(k for k in variants if k.startswith('red_body')), [])
        self.assertEqual(variants['full'], self.URL + 'reduced/lod_error_0p4.glb')      # the other variants are unchanged

    def test_pieces_fitted_in_plain_pieces_folder(self):
        module, _ = self.fitted_module(with_pieces=False)
        for sub in ('lod_0p2', 'lod_0p2_fitted'):
            (module / 'pieces' / sub).mkdir(parents=True)
            (module / 'pieces' / sub / 'chest.glb').write_bytes(b'glb')
        _, data = self.req('GET', '/api/state')
        entry = data['models'][0]['entries'][0]
        self.assertEqual(entry['pieces_fitted']['0p2'], [self.URL + 'pieces/lod_0p2_fitted/chest.glb'])

    def age(self, *folders):
        """Backdate every file and folder below `folders` so the result cache keeps them (recent mtimes are never cached),
        then request the state once so that everything is cached."""
        import os
        folders = (*folders, *(f for f in (self.root / 'completed', self.root / 'base_views') if f.exists()))
        for folder in folders:
            for where, dirs, files in os.walk(folder, topdown=False):
                for name in files + dirs:
                    os.utime(Path(where) / name, (1000, 1000), follow_symlinks=False)
                os.utime(where, (1000, 1000))
        self.req('GET', '/api/state')

    def scans(self):
        """(number of results rebuilt rather than served from the cache, state) of one request."""
        before = serve.SCANS[0]
        _, data = self.req('GET', '/api/state')
        return serve.SCANS[0] - before, data

    def test_model_cache_serves_unchanged_modules_and_sees_changes(self):
        import os
        module, made = self.fitted_module()
        other = self.model('set_a--other', 'dwarf_male', 'body_armor').parents[1]
        models = self.root / 'models'
        entry = lambda data, race='human_male': next(e for e in data['models'][0]['entries'] if e['race'] == race)
        self.age(models)
        self.assertEqual(self.scans()[0], 0)                      # warm: nothing rebuilt
        # a new LOD file appears
        (module / 'reduced' / 'lod_error_0p2__wearable.glb').write_bytes(b'glb')
        count, data = self.scans()
        self.assertIn('wearable', entry(data)['variants']['0p2'])
        self.assertEqual(count, 1)                                # only that module
        self.age(models)
        # a stale fitted GLB appears next to an unfitted one, then is rewritten in place (folder listing unchanged)
        fitted = other / 'reduced' / 'lod_error_0p4__wearable_fitted.glb'
        fitted.write_bytes(b'glb')
        os.utime(fitted, (500, 500))
        self.assertNotIn('fitted', entry(self.scans()[1], 'dwarf_male')['variants']['0p4'])
        self.age(models)
        self.assertEqual(self.scans()[0], 0)
        os.utime(fitted, (2000, 2000))
        count, data = self.scans()
        self.assertIn('fitted', entry(data, 'dwarf_male')['variants']['0p4'])
        self.assertEqual(count, 1)
        self.age(models)
        # chosen_lod.json created, then rewritten in place
        ids = {'set': 'set_a', 'job': 'set_a--job', 'race': 'human_male', 'module': 'body_armor'}
        self.req('POST', '/api/lod', ids | {'lod': '0p2'})
        self.assertEqual(entry(self.scans()[1])['lod'], '0p2')
        self.age(models)
        self.assertEqual(self.scans()[0], 0)
        self.req('POST', '/api/lod', ids | {'lod': '0p1'})
        self.assertEqual(entry(self.scans()[1])['lod'], '0p1')
        self.age(models)
        # the unfitted wearable is rebuilt in place: the fitted file turns stale, and fresh again
        self.assertIn('fitted', entry(self.scans()[1])['variants']['0p4'])
        os.utime(made['plain'], (3000, 3000))
        self.assertNotIn('fitted', entry(self.scans()[1])['variants']['0p4'])
        os.utime(made['plain'], (1000, 1000))
        self.assertIn('fitted', entry(self.scans()[1])['variants']['0p4'])
        # a piece is re-split in place: the fitted pieces turn stale (the first piece is watched), then a piece is added
        self.assertIn('0p4', entry(self.scans()[1])['pieces_fitted'])
        os.utime(module / 'segment_first/pieces/lod_0p4/chest.glb', (3000, 3000))
        self.assertEqual(entry(self.scans()[1])['pieces_fitted'], {})
        os.utime(module / 'segment_first/pieces/lod_0p4/chest.glb', (1000, 1000))
        self.assertIn('0p4', entry(self.scans()[1])['pieces_fitted'])
        (module / 'segment_first/pieces/lod_0p4/arm.glb').write_bytes(b'glb')
        data = self.scans()[1]
        self.assertEqual(len(entry(data)['pieces']['0p4']), 3)
        self.assertEqual(entry(data)['pieces_fitted'], {})        # the new piece has no fitted twin
        self.age(models)
        # a rendered thumbnail appears; a model is added; one is removed
        (module / 'reduced' / 'lod_error_0p1.glb').write_bytes(b'glb')
        os.utime(module / 'reduced' / 'lod_error_0p1.glb', (1000, 1000))
        self.assertIsNone(entry(self.scans()[1])['preview'])
        self.age(models)
        thumb = module / 'reduced' / 'lod_error_0p1.thumb.png'
        thumb.write_bytes(b'png')
        os.utime(thumb, (2000, 2000))
        self.assertEqual(entry(self.scans()[1])['preview'], self.URL + 'reduced/lod_error_0p1.thumb.png?v=2000')
        self.model('set_a--third', 'orc_male', 'body_armor')
        self.assertEqual(len(self.scans()[1]['models'][0]['entries']), 3)
        import shutil
        shutil.rmtree(other.parent)
        self.assertEqual([e['job'] for e in self.scans()[1]['models'][0]['entries']], ['set_a--job', 'set_a--third'])

    def held_job(self, name, races, record=True):
        folder = self.root / 'held_conversions' / name
        for race, module in races:
            (folder / race / module).mkdir(parents=True)
            for view in ('front', 'back'):
                (folder / race / module / f'{view}.png').write_bytes(b'png')
        (folder / '.base_views' / races[0][0]).mkdir(parents=True)               # hidden folders are not races
        (folder / '.base_views' / races[0][0] / 'front.png').write_bytes(b'png')
        (folder / '.concept_notes').mkdir()
        if record:
            (folder / '.tripo-job.json').write_text(json.dumps({'conversion': 'pending_playwright_worker'}))

    def test_held_tab_groups_held_jobs_by_set_and_pipeline_skips_them(self):
        self.held_job('long_skirt--aaa', [('human_female', 'outfit')])
        self.held_job('long_skirt--bbb', [('dwarf_male', 'outfit'), ('human_male', 'outfit')], record=False)
        self.held_job('other_dress--ccc', [('gnome_female', 'outfit')])
        (self.root / 'processing_views/live--ddd/human_male/outfit').mkdir(parents=True)
        (self.root / 'processing_views/live--ddd/human_male/outfit/front.png').write_bytes(b'png')
        _, data = self.req('GET', '/api/state')
        self.assertEqual([c['id'] for c in data['held']], ['held:long_skirt', 'held:other_dress'])
        skirt = data['held'][0]
        self.assertEqual(skirt['kind'], 'held')
        self.assertEqual([j['name'] for j in skirt['jobs']], ['long_skirt--aaa', 'long_skirt--bbb'])
        self.assertEqual([j['id'] for j in skirt['jobs']], ['held_conversions:long_skirt--aaa', 'held_conversions:long_skirt--bbb'])
        self.assertEqual([j['conversion'] for j in skirt['jobs']], ['pending_playwright_worker', 'unknown'])
        self.assertEqual([(v['name'], v['module'], sorted(v['images'])) for v in skirt['jobs'][1]['views']],
                         [('dwarf_male', 'outfit', ['back', 'front']), ('human_male', 'outfit', ['back', 'front'])])
        self.assertEqual(skirt['jobs'][0]['views'][0]['images']['front'],
                         '/files/held_conversions/long_skirt--aaa/human_female/outfit/front.png')
        status, _ = self.req('GET', skirt['jobs'][0]['views'][0]['images']['front'])
        self.assertEqual(status, 200)
        self.assertEqual([j['name'] for j in data['pipeline']], ['live--ddd'])      # held jobs stay out of Pipeline
        self.assertEqual(self.req('GET', '/api/state?models=0')[1]['held'], data['held'])
        for verb in ('approve', 'reject', 'unapprove', 'restore'):                    # read-only: no action works on a held id
            self.assertEqual(self.req('POST', f'/api/{verb}', {'id': 'held:long_skirt'})[0], 400)
        self.assertTrue((self.root / 'held_conversions/long_skirt--aaa').is_dir())

    def test_held_tab_empty_without_folder(self):
        self.assertEqual(self.req('GET', '/api/state')[1]['held'], [])

    def test_models_carry_previews_per_race(self):
        for race, module in (('human_male', 'body_armor'), ('human_male', 'right_gauntlet_boot'), ('dwarf_male', 'body_armor')):
            glb = self.model(f'set_a--{race}-{module}', race, module)
            (glb.parent / 'lod_error_0p4.glb').write_bytes(b'glb')
        self.model('set_b--only', 'orc_male', 'outfit', set_name='set_b')            # no full GLB: no 3D preview for the race
        _, data = self.req('GET', '/api/state')
        cards = {c['name']: c for c in data['models']}
        by_race = cards['set_a']['previews_by_race']
        self.assertEqual(sorted(by_race), ['dwarf_male', 'human_male'])
        self.assertEqual([p['model'].split('/')[5] for p in by_race['dwarf_male']['previews']], ['dwarf_male'])
        self.assertEqual(len(by_race['human_male']['previews']), 2)                    # body armour and gauntlet/boot
        self.assertTrue(all('/human_male/' in p['model'] for p in by_race['human_male']['previews']))
        self.assertEqual(cards['set_a']['previews'][0]['model'], by_race['dwarf_male']['previews'][0]['model'])   # default = first race
        self.assertEqual(list(cards['set_b']['previews_by_race']), ['orc_male'])
        self.assertEqual(cards['set_b']['previews_by_race']['orc_male']['previews'], [])
        self.assertIn('thumbnail', by_race['dwarf_male'])

    def test_state_without_models(self):
        self.model('set_a--job', 'human_male', 'body_armor')
        _, full = self.req('GET', '/api/state')
        _, fast = self.req('GET', '/api/state?models=0')
        self.assertEqual(len(full['models']), 1)
        self.assertNotIn('models', fast)
        self.assertNotIn('approved_models', fast)
        self.assertEqual({k: v for k, v in full.items() if k not in ('models', 'approved_models')}, fast)

    def test_concept_and_job_caches_see_changes(self):
        import os
        self.concept()
        race = self.root / 'concepts/one/human_male'
        job = self.root / 'processing_views/set--job1/human_male/outfit'
        job.mkdir(parents=True)
        (job / 'front.png').write_bytes(b'png')
        self.age(self.root / 'concepts', self.root / 'processing_views')
        self.assertEqual(self.scans()[0], 0)
        (race / 'outfit_prompts.json').write_text('{"brief":"cloak"}')   # in place: only the file mtime moves
        count, data = self.scans()
        self.assertEqual((data['concepts'][0]['races'][0]['info']['outfit']['brief'], count), ('cloak', 1))
        self.age(self.root / 'concepts')
        (race / 'body_armor_front.png').write_bytes(b'png')
        self.assertEqual(sorted(self.scans()[1]['concepts'][0]['races'][0]['images']), ['body_armor', 'outfit'])
        (job / 'back.png').write_bytes(b'png')
        self.assertEqual(sorted(self.scans()[1]['pipeline'][0]['views'][0]['images']), ['back', 'front'])
        self.age(self.root / 'concepts', self.root / 'processing_views')
        record = job.parents[1] / '.tripo-job.json'
        record.write_text('{"conversion":"done"}')
        self.assertEqual(self.scans()[1]['pipeline'][0]['conversion'], 'done')
        self.age(self.root / 'processing_views')
        record.write_text('{"conversion":"running"}')                    # in place again
        os.utime(record, (1001, 1001))
        self.assertEqual(self.scans()[1]['pipeline'][0]['conversion'], 'running')

    def test_clearance_endpoint(self):
        module, made = self.fitted_module(with_pieces=False)
        url = self.URL + 'reduced/lod_error_0p4__wearable_fitted.glb'
        status, data = self.req('GET', '/api/clearance?url=' + url)
        self.assertEqual(status, 400)
        self.assertIn('not measured', data['error'])
        record = {'model': 'x.glb', 'margin_mm': 2.0, 'metrics': {'penetrating_area_pct': 0.5},
                  'meshes': [{'mesh_index': 0, 'name': 'Body', 'd_mm': [-1.5, 0.5, 4.0]}]}
        made['fitted'].with_name('lod_error_0p4__wearable_fitted.clearance.json').write_text(json.dumps(record))
        status, data = self.req('GET', '/api/clearance?url=' + url)
        self.assertEqual((status, data.get('error')), (200, None))
        self.assertEqual((data['margin_mm'], data['meshes'][0]['d_mm'], data['url']), (2.0, [-1.5, 0.5, 4.0], url))
        for bad in ('/files/concepts/x.glb', '/files/models/../concepts/x.glb', self.URL + 'reduced/missing.glb',
                    self.URL + 'reduced/lod_error_0p4__wearable.marks.json', ''):
            status, data = self.req('GET', '/api/clearance?url=' + bad)
            self.assertEqual(status, 400, bad)
        (module / 'reduced' / 'bad.glb').write_bytes(b'glb')
        (module / 'reduced' / 'bad.clearance.json').write_text('[1, 2]')
        status, _ = self.req('GET', '/api/clearance?url=' + self.URL + 'reduced/bad.glb')
        self.assertEqual(status, 400)

    def test_on_body_works_for_any_module_glb(self):
        import shutil
        import numpy as np
        module, made = self.fitted_module()
        core = self.root / 'tools/outfit_studio/core'
        core.mkdir(parents=True)
        shutil.copy(Path(__file__).parents[1] / 'outfit_studio/core/transform.py', core / 'transform.py')
        manifest = {'rotation_to_review': [[0, -1, 0], [1, 0, 0], [0, 0, 1]], 'normalization_scale': 2.0,
                    'normalization_origin_rotated': [.01, -.02, .03], 'parts': {'fit': {'scale': .99, 'translation': [0, .01, .02]}}}
        (module / 'reduced' / 'manifest.json').write_text(json.dumps(manifest))
        cache = self.root / 'tools/preview/cache/bases'
        cache.mkdir(parents=True)
        (cache / 'human_male.json').write_text(json.dumps({'H': 1.8, 'o': [0, 0, 0]}))
        (cache / 'human_male.glb').write_bytes(b'glb')
        old, serve.BASE_CACHE = serve.BASE_CACHE, cache
        try:
            reference = None
            aligned = ('reduced/lod_error_0p4__wearable_aligned.glb', 'reduced/lod_error_0p4__wearable_aligned_fitted.glb',
                       'reduced/lod_error_0p4__tripo_body.glb', 'reduced/lod_error_0p4__tripo_body_aligned.glb',       # red-body overlays: placed like the wearable
                       'segment_first/pieces/lod_0p4_aligned/chest.glb', 'segment_first/pieces/lod_0p4_aligned_fitted/pelvis.glb')
            for rel in aligned:             # align_body.py / body_fit.py --source aligned files are in the native frame too: same placement
                (module / rel).parent.mkdir(parents=True, exist_ok=True)
                (module / rel).write_bytes(b'glb')
            for rel in ('reduced/lod_error_0p4__wearable.glb', 'reduced/lod_error_0p4__wearable_fitted.glb',
                        'segment_first/pieces/lod_0p4/chest.glb', 'segment_first/pieces/lod_0p4_fitted/pelvis.glb', *aligned):
                status, data = self.req('GET', '/api/on_body?url=' + self.URL + rel)
                self.assertEqual(status, 200, (rel, data))
                self.assertEqual((data['race'], data['base']), ('human_male', '/files/tools/preview/cache/bases/human_male.glb'))
                self.assertEqual(len(data['matrix']), 16)
                reference = reference or data['matrix']
                np.testing.assert_allclose(data['matrix'], reference)       # one placement per module
            status, data = self.req('GET', '/api/on_body?url=' + self.URL + 'nope.glb')
            self.assertEqual(status, 400)
            (module / 'reduced' / 'manifest.json').unlink()
            status, data = self.req('GET', '/api/on_body?url=' + self.URL + 'reduced/lod_error_0p4__wearable.glb')
            self.assertEqual((status, 'base fit' in data['error']), (400, True))
        finally:
            serve.BASE_CACHE = old


if __name__=='__main__':
    unittest.main()
