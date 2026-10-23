"""Run with python3 -m unittest tools/preview/test_serve.py."""
import http.client
from http.server import ThreadingHTTPServer
import importlib.util
import json
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
        for name in ('concepts', 'approved', 'trash/concepts', 'base_views', 'tools/tripo_pipeline', 'tools/preview'):
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
        self.assertFalse((self.root/'concepts/one').exists())
        self.assertTrue((self.root/'approved/one').exists())
        status, result = self.req('POST','/api/unapprove',{'id':'approved:one'})
        self.assertEqual(status,200)
        self.assertTrue((self.root/'concepts/one').exists())
        self.assertEqual(len((self.root/'tools/preview/actions.jsonl').read_text().splitlines()),2)

    def test_approval_validation_warning_keeps_move(self):
        self.concept('bad')
        status,result=self.req('POST','/api/approve',{'id':'concepts:bad'})
        self.assertEqual(status,200)
        self.assertIn('invalid set',result['warning'])
        self.assertTrue((self.root/'approved/bad').exists())

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
        (self.root/'approved/one').mkdir()
        status,_=self.req('POST','/api/approve',{'id':'concepts:one'})
        self.assertEqual(status,400)
        self.assertTrue((self.root/'concepts/one').exists())
        (self.root/'approved/one').rmdir()
        self.req('POST','/api/approve',{'id':'concepts:one'})
        (self.root/'concepts/one').mkdir()
        status,_=self.req('POST','/api/unapprove',{'id':'approved:one'})
        self.assertEqual(status,400)
        (self.root/'concepts/one').rmdir()
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


if __name__=='__main__':
    unittest.main()
