"""Library tests of tools/preview/serve.py (the old site's HTTP routes were retired in pipeline v2 phase 6; v2 imports the rest).

Run with python -X utf8 -m unittest tools/preview/test_serve.py."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('preview_serve', HERE / 'serve.py')
serve = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(serve)


class LibraryContractTests(unittest.TestCase):
    def test_every_name_v2_uses_exists(self):
        """Every `serve.<name>` the v2 server code refers to is still defined (the phase 6 trim kept the library)."""
        used = set()
        for path in (HERE / 'v2').glob('*.py'):
            if path.name.startswith('test_'):
                continue
            used |= set(re.findall(r'\bserve\.([A-Za-z_][A-Za-z0-9_]*)', path.read_text(encoding='utf-8')))
        used.discard('py')                                                   # "serve.py" in prose
        self.assertTrue(used)
        self.assertEqual(sorted(n for n in used if not hasattr(serve, n)), [])

    def test_old_site_is_gone(self):
        for name in ('Handler', 'state', 'action', 'model_cards', 'held_cards', 'fitlab_info', 'body_sections', 'write_marks', 'on_body', 'write_lod'):
            self.assertFalse(hasattr(serve, name), name)
        for name in ('app.js', 'index.html', 'style.css', 'fitsym.js', 'monsters.js', 'test_fitsym.mjs', 'preview.sh'):
            self.assertFalse((HERE / name).exists(), name)
        self.assertTrue((HERE / 'web' / 'index.html').is_file())             # the v2 app

    def test_main_says_retired_and_exits_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as stop:
            serve.main()
        self.assertEqual(stop.exception.code, 2)
        self.assertIn('tools/preview/v2/server.py', err.getvalue())
        self.assertIn('--manage-services', err.getvalue())

    def test_monsters_module_is_loaded(self):
        self.assertTrue(callable(serve.monsters.cards))
        self.assertEqual(serve.monsters.FILE_PREFIX, '/mfiles/')


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ('concepts', 'provisional', 'approved', 'trash/concepts', 'archive/concepts', 'base_views', 'tools/preview'):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp.cleanup()

    def concept(self, name='one', stage='concepts'):
        race = self.root / stage / name / 'human_male'
        race.mkdir(parents=True)
        (race / 'outfit_front.png').write_bytes(b'png')
        (race / 'outfit_prompts.json').write_text('{"brief":"robe","attempt_count":2}')

    def age(self, *folders):
        """Backdate every file and folder below `folders` so the result cache keeps them (recent mtimes are never cached)."""
        for folder in folders:
            for where, dirs, files in os.walk(folder, topdown=False):
                for name in files + dirs:
                    os.utime(Path(where) / name, (1000, 1000))
                os.utime(where, (1000, 1000))

    # -------------------------------------------------------------------------------------------------- names and paths
    def test_names_and_direct_paths(self):
        self.assertEqual(serve.safe_name('set_a-1'), 'set_a-1')
        for bad in ('..', '.', 'a/b', '', 'x y', '_lead', None):
            with self.assertRaises(ValueError, msg=bad):
                serve.safe_name(bad)
        self.assertEqual(serve.direct(self.root, 'concepts', 'one'), self.root / 'concepts' / 'one')
        with self.assertRaises(ValueError):
            serve.direct(self.root, 'concepts', '../one')

    def test_direct_refuses_a_linked_entry(self):
        outside = Path(tempfile.mkdtemp())
        try:
            try:
                (self.root / 'concepts' / 'leak').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('symlinks need developer mode on Windows')
            with self.assertRaises(ValueError):
                serve.direct(self.root, 'concepts', 'leak')
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_job_set_and_base_races(self):
        for race in ('human_male', 'gnome_female'):
            (self.root / 'base_views' / race).mkdir(parents=True)
            (self.root / 'base_views' / race / 'palette.json').write_text('{}')
        (self.root / 'base_views' / 'old_race').mkdir()                      # no palette.json: not an active race
        races = serve._base_races(self.root)
        self.assertEqual(races, ['gnome_female', 'human_male'])
        self.assertEqual(serve._job_set('plain--abc', races), 'plain')
        self.assertEqual(serve._job_set('plain_gnome_female--abc', races), 'plain')
        self.assertEqual(serve._job_set('gnome_female--abc', races), 'gnome_female')

    def test_flat_helpers_and_unique(self):
        batch = self.root / 'concepts' / 'batch'
        batch.mkdir()
        (batch / '01_plate__body_armor.png').write_bytes(b'png')
        (batch / '01_plate.json').write_text('{}')
        (batch / '02_other__outfit.png').write_bytes(b'png')
        self.assertEqual([p.name for p in serve.flat_files(batch, '01_plate')], ['01_plate.json', '01_plate__body_armor.png'])
        with self.assertRaises(FileNotFoundError):
            serve.flat_files(batch, '03_none')
        self.assertEqual(serve.origin_split('concepts/x'), ('concepts', 'x'))
        self.assertEqual(serve.origin_split('archive/concepts/x'), ('archive/concepts', 'x'))
        for bad in ('approved/x', 'concepts/../x', None):
            with self.assertRaises(ValueError, msg=bad):
                serve.origin_split(bad)
        self.assertEqual(serve.unique(batch / 'free'), batch / 'free')
        self.assertNotEqual(serve.unique(batch), batch)

    def test_log_action(self):
        serve.log_action(self.root, 'approve', 'concepts/one', 'approved/one', 'ok')
        record = json.loads((self.root / 'tools/preview/actions.jsonl').read_text().splitlines()[-1])
        self.assertEqual((record['action'], record['src'], record['dst'], record['result']), ('approve', 'concepts/one', 'approved/one', 'ok'))

    # -------------------------------------------------------------------------------------------------- concept stage
    def test_concept_cards(self):
        self.concept()
        self.concept('two', 'provisional')
        cards = serve.concepts(self.root, 'concepts')
        self.assertEqual([c['id'] for c in cards], ['concepts:one'])
        race = cards[0]['races'][0]
        self.assertEqual(race['images']['outfit'], '/files/concepts/one/human_male/outfit_front.png')
        self.assertEqual(race['info']['outfit'], {'brief': 'robe', 'attempts': 2})
        self.assertEqual([c['id'] for c in serve.concepts(self.root, 'provisional')], ['provisional:two'])

    def test_legacy_entries_in_concepts_and_archive(self):
        for stage in ('concepts', 'archive/concepts'):
            batch = self.root / stage / 'batch'
            batch.mkdir()
            for module in ('body_armor', 'right_gauntlet_boot'):
                (batch / f'01_plate__{module}.png').write_bytes(b'png')
            (batch / '01_plate.json').write_text('{"race_gender":"human_male"}')
        legacy, = serve.concepts(self.root, 'concepts')
        self.assertEqual((legacy['id'], legacy['kind'], legacy['incomplete']), ('legacy:batch:01_plate', 'legacy', False))
        self.assertEqual(legacy['races'][0]['name'], 'human_male')
        self.assertEqual(serve.concepts(self.root, 'archive')[0]['id'], 'archlegacy:batch:01_plate')

    def test_trash_cards(self):
        self.concept('gone', 'trash/concepts')
        (self.root / 'trash/concepts/gone/.preview-origin.json').write_text(json.dumps({'kind': 'set', 'src': 'concepts/gone'}))
        flat = self.root / 'trash/concepts/batch'
        flat.mkdir()
        (flat / '01_plate__outfit.png').write_bytes(b'png')
        (flat / '01_plate.preview-origin.json').write_text(json.dumps({'kind': 'legacy', 'src': 'concepts/batch'}))
        self.assertEqual([(c['id'], c['origin']) for c in serve.trash_cards(self.root)],
                         [('trashlegacy:batch:01_plate', 'concepts/batch'), ('trash:gone', 'concepts/gone')])

    def test_job_views(self):
        job = self.root / 'processing_views' / 'set--job1'
        (job / 'human_male' / 'outfit').mkdir(parents=True)
        for view in ('front', 'back'):
            (job / 'human_male' / 'outfit' / f'{view}.png').write_bytes(b'png')
        (job / '.base_views' / 'human_male').mkdir(parents=True)           # hidden folders are not races
        (job / '.tripo-job.json').write_text('{"conversion": "pending"}')
        record, races = serve._job_views(job, {})
        self.assertEqual(record['conversion'], 'pending')
        self.assertEqual([(r['name'], r['module'], sorted(r['images'])) for r in races], [('human_male', 'outfit', ['back', 'front'])])

    def test_concept_cache_sees_changes(self):
        self.concept()
        self.age(self.root / 'concepts')
        serve.concepts(self.root, 'concepts')
        before = serve.SCANS[0]
        serve.concepts(self.root, 'concepts')
        self.assertEqual(serve.SCANS[0], before)                             # warm: nothing rebuilt
        race = self.root / 'concepts/one/human_male'
        (race / 'outfit_prompts.json').write_text('{"brief":"cloak"}')      # in place: only the file mtime moves
        self.assertEqual(serve.concepts(self.root, 'concepts')[0]['races'][0]['info']['outfit']['brief'], 'cloak')
        self.age(self.root / 'concepts')
        (race / 'body_armor_front.png').write_bytes(b'png')
        self.assertEqual(sorted(serve.concepts(self.root, 'concepts')[0]['races'][0]['images']), ['body_armor', 'outfit'])

    # -------------------------------------------------------------------------------------------------- module scan (Review tab)
    URL = '/files/models/set_a/set_a--job/human_male/body_armor/'

    def fitted_module(self, with_pieces=True):
        """A module with an unfitted wearable (mtime 1000), a fitted one (2000), pieces and fitted pieces."""
        module = self.root / 'models/set_a/set_a--job/human_male/body_armor'
        reduced = module / 'reduced'
        reduced.mkdir(parents=True)
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

    def entry(self, module):
        result = serve._cached_module(self.root, module, module.parent.name, module.name, module.parents[1].name)
        return result[0] if result else None

    def test_fitted_variants_and_freshness(self):
        module, made = self.fitted_module()
        entry = self.entry(module)
        self.assertEqual(entry['variants']['0p4']['fitted'], self.URL + 'reduced/lod_error_0p4__wearable_fitted.glb')
        self.assertEqual(entry['pieces']['0p4'], [self.URL + f'segment_first/pieces/lod_0p4/{n}.glb' for n in ('chest', 'pelvis')])
        self.assertEqual(entry['pieces_fitted']['0p4'], [self.URL + f'segment_first/pieces/lod_0p4_fitted/{n}.glb' for n in ('chest', 'pelvis')])
        self.assertNotIn('fitted', entry['variants']['0p2'])
        os.utime(made['plain'], (3000, 3000))                                # unfitted rebuilt after the fit: stale
        os.utime(module / 'segment_first/pieces/lod_0p4/pelvis.glb', (3000, 3000))
        entry = self.entry(module)
        self.assertNotIn('fitted', entry['variants']['0p4'])
        self.assertIn('wearable', entry['variants']['0p4'])
        self.assertEqual(entry['pieces_fitted'], {})                         # one stale twin: all or nothing

    def test_red_body_overlays_follow_their_sources(self):
        module, made = self.fitted_module()
        reduced = module / 'reduced'
        labelled = reduced / 'lod_error_0p4.glb'
        labelled.write_bytes(b'glb')
        os.utime(labelled, (1000, 1000))
        files = {'red_body': reduced / 'lod_error_0p4__tripo_body.glb', 'red_body_aligned': reduced / 'lod_error_0p4__tripo_body_aligned.glb',
                 'aligned': reduced / 'lod_error_0p4__wearable_aligned.glb'}
        for key, mtime in (('aligned', 1500), ('red_body', 2000), ('red_body_aligned', 2000)):
            files[key].write_bytes(b'glb')
            os.utime(files[key], (mtime, mtime))
        variants = self.entry(module)['variants']['0p4']
        self.assertEqual(variants['red_body'], self.URL + 'reduced/lod_error_0p4__tripo_body.glb')
        self.assertEqual(variants['aligned'], self.URL + 'reduced/lod_error_0p4__wearable_aligned.glb')
        os.utime(files['aligned'], (3000, 3000))
        variants = self.entry(module)['variants']['0p4']
        self.assertIn('red_body', variants)
        self.assertNotIn('red_body_aligned', variants)
        self.assertEqual(variants['full'], self.URL + 'reduced/lod_error_0p4.glb')

    def test_chosen_lod_and_preview_thumbnail(self):
        module, _ = self.fitted_module(with_pieces=False)
        full = module / 'reduced' / 'lod_error_0p2.glb'
        full.write_bytes(b'glb')
        os.utime(full, (1000, 1000))
        (module / serve.LOD_CHOICE).write_text('{"lod": "0p2"}')
        entry = self.entry(module)
        self.assertEqual((entry['lod'], entry['preview']), ('0p2', None))
        self.assertTrue(entry['preview_model'].endswith('/reduced/lod_error_0p2.glb'))
        result = serve.write_thumb(self.root, entry['preview_model'], b'\x89PNG\r\n\x1a\nxx')
        self.assertTrue(full.with_name('lod_error_0p2.thumb.png').is_file())
        self.assertEqual(result['image'], serve.fresh_thumb(self.root, full))
        stamp = full.with_name('lod_error_0p2.thumb.png').stat().st_mtime + 10
        os.utime(full, (stamp, stamp))                     # model rebuilt after the render: stale
        self.assertIsNone(serve.fresh_thumb(self.root, full))
        for bad_url, png in ((entry['preview_model'], b'not png'), ('/files/concepts/x.glb', b'\x89PNG\r\n\x1a\n'),
                             ('/files/models/../concepts/x.glb', b'\x89PNG\r\n\x1a\n')):
            with self.assertRaises(ValueError):
                serve.write_thumb(self.root, bad_url, png)

    def test_review_cards(self):
        module, _ = self.fitted_module(with_pieces=False)
        lists = self.root / 'review.json'
        lists.write_text(json.dumps({'lists': [{'name': 'check', 'title': 'Check', 'modules': [module.relative_to(self.root).as_posix(), 'models/../x/y/z/w']}]}))
        old, serve.REVIEW_LISTS = serve.REVIEW_LISTS, lists
        try:
            card, = serve.review_cards(self.root)
        finally:
            serve.REVIEW_LISTS = old
        self.assertEqual((card['id'], card['name'], card['kind']), ('review:check', 'Check', 'review'))
        self.assertEqual([e['race'] for e in card['entries']], ['human_male'])


if __name__ == '__main__':
    unittest.main()
