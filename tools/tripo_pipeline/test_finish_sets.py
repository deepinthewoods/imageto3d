"""finish_sets: set completeness across races, in-progress blocking, gnome handling, apply (temp root)."""
import json
from pathlib import Path

import pytest

import finish_sets

RACES = ['dwarf_male', 'human_male']
ARTIFACTS = finish_sets.ARTIFACTS


def make_root(tmp_path, races):
    for race in races + ['no_palette']:
        folder = tmp_path / 'base_views' / race
        folder.mkdir(parents=True)
        if race != 'no_palette':
            (folder / 'palette.json').write_text('{}')
    return tmp_path


@pytest.fixture
def root(tmp_path):
    """Two races, no gnome base."""
    return make_root(tmp_path, RACES)


@pytest.fixture
def groot(tmp_path):
    """The same with a gnome base: gnomes are a required race like any other (user, 2026-11-14)."""
    return make_root(tmp_path, RACES + ['gnome_female'])


def make_job(root, stage, set_id, job_id, modules, state='converted', artifacts=True, model_set=None):
    path = root / stage / f'{set_id}--{job_id}'
    path.mkdir(parents=True)
    (path / '.tripo-job.json').write_text(json.dumps({
        'set': model_set or set_id, 'job_id': job_id, 'state': state,
        'modules': [{'module': m, 'race_gender': r} for r, m in modules]}))
    if artifacts and state.startswith('converted'):
        for race, module in modules:
            reduced = root / 'models' / (model_set or set_id) / job_id / race / module / 'reduced'
            reduced.mkdir(parents=True)
            for name in ARTIFACTS:
                (reduced / name).write_text('x')
    return path


def status(root, set_id, apply=False):
    return {s['set']: s for s in finish_sets.run(root, apply=apply)}[set_id]


def test_complete_and_incomplete(root):
    make_job(root, 'processing', 'a', '1', [('dwarf_male', 'outfit')])
    assert status(root, 'a')['missing'] == {'outfit': ['human_male']}
    make_job(root, 'processing', 'a', '2', [('human_male', 'outfit')])
    s = status(root, 'a')
    assert s['complete'] and not s['missing']
    assert sorted(s['would_move']) == ['a--1', 'a--2']


def test_gnome_required_for_completeness(groot):
    root = groot
    make_job(root, 'processing', 'a', '1', [('dwarf_male', 'outfit'), ('human_male', 'outfit')])
    assert status(root, 'a')['missing'] == {'outfit': ['gnome_female']}
    make_job(root, 'processing', 'b', '1', [('dwarf_male', 'outfit'), ('human_male', 'outfit')])
    make_job(root, 'processing_views', 'b', '2', [('gnome_female', 'outfit')], state='views')
    s = status(root, 'b')
    assert not s['complete'] and s['in_progress'] == ['processing_views/b--2']
    make_job(root, 'processing', 'c', '1', [('gnome_female', 'outfit')])
    assert status(root, 'c')['missing'] == {'outfit': ['dwarf_male', 'human_male']}
    make_job(root, 'processing', 'a', '2', [('gnome_female', 'outfit')])
    s = status(root, 'a')
    assert s['complete'] and sorted(s['would_move']) == ['a--1', 'a--2']


def test_in_progress_blocks(root):
    make_job(root, 'processing', 'a', '1', [('dwarf_male', 'outfit'), ('human_male', 'outfit')])
    make_job(root, 'multiview_ready', 'a', '2', [('human_male', 'outfit')], state='ready')
    s = status(root, 'a')
    assert not s['complete'] and s['in_progress'] == ['multiview_ready/a--2']
    assert s['would_move'] == []


def test_unconverted_processing_job_blocks(root):
    make_job(root, 'processing', 'a', '1', [('dwarf_male', 'outfit'), ('human_male', 'outfit')])
    make_job(root, 'processing', 'a', '2', [('dwarf_male', 'body_armor')], state='uploaded')
    s = status(root, 'a')
    assert not s['complete'] and s['in_progress'] == ['processing/a--2']
    assert s['missing'] == {'body_armor': ['dwarf_male', 'human_male']}


def test_failed_ignored(root):
    make_job(root, 'processing', 'a', '1', [('dwarf_male', 'outfit'), ('human_male', 'outfit')])
    make_job(root, 'failed', 'a', '2__held_gnome_female', [('gnome_female', 'outfit')], state='held')
    make_job(root, 'failed', 'x', '3', [('human_male', 'outfit')], state='held')
    results = {s['set']: s for s in finish_sets.run(root)}
    assert results['a']['complete'] and results['a']['modules'] == ['outfit']
    assert 'x' not in results


def test_gnome_suffix_groups_with_base_set(groot):
    root = groot
    make_job(root, 'processing', 'plate_01', '1', [('dwarf_male', 'body_armor'), ('human_male', 'body_armor')])
    make_job(root, 'processing', 'plate_01_gnome', '2', [('gnome_female', 'body_armor')])
    results = {s['set']: s for s in finish_sets.run(root)}
    assert 'plate_01_gnome' not in results
    assert results['plate_01']['complete']
    assert sorted(results['plate_01']['would_move']) == ['plate_01--1', 'plate_01_gnome--2']


def test_apply_moves_and_writes_record(groot):
    root = groot
    make_job(root, 'processing', 'a', '1', [('dwarf_male', 'outfit'), ('human_male', 'outfit')])
    make_job(root, 'processing', 'a_gnome', '2', [('gnome_female', 'outfit')])
    s = status(root, 'a', apply=True)
    assert sorted(s['moved']) == ['a--1', 'a_gnome--2'] and not s['errors']
    assert not (root / 'processing' / 'a--1').exists()
    record = json.loads((root / 'completed' / 'a--1' / '.tripo-job.json').read_text())
    assert record['completed']['by'] == 'finish_sets.py' and record['completed']['reason'].startswith('set converted')
    assert record['state'] == 'converted'
    # a second pass is a no-op, and a later gnome-only job of the complete set moves too
    make_job(root, 'processing', 'a_gnome', '3', [('gnome_female', 'outfit')])
    s = status(root, 'a', apply=True)
    assert s['moved'] == ['a_gnome--3']


def test_dry_run_moves_nothing(root):
    make_job(root, 'processing', 'a', '1', [('dwarf_male', 'outfit'), ('human_male', 'outfit')])
    assert status(root, 'a')['would_move'] == ['a--1']
    assert (root / 'processing' / 'a--1').is_dir() and not (root / 'completed').exists()


def test_missing_artifact_keeps_job(root):
    make_job(root, 'processing', 'a', '1', [('dwarf_male', 'outfit'), ('human_male', 'outfit')])
    make_job(root, 'processing', 'a', '2', [('human_male', 'outfit'), ('human_male', 'cloak')])
    make_job(root, 'processing', 'a', '3', [('dwarf_male', 'cloak'), ('human_male', 'cloak')])
    (root / 'models/a/2/human_male/cloak/reduced/lod_error_0p4__wearable.glb').unlink()
    s = status(root, 'a', apply=True)
    assert s['complete'] and sorted(s['moved']) == ['a--1', 'a--3']
    assert s['blocked'][0]['job'] == 'a--2'
    assert (root / 'processing' / 'a--2').is_dir()


def test_missing_artifact_means_pair_not_done(root):
    make_job(root, 'processing', 'a', '1', [('dwarf_male', 'outfit'), ('human_male', 'outfit')])
    (root / 'models/a/1/human_male/outfit/reduced/manifest.json').unlink()
    s = status(root, 'a')
    assert not s['complete'] and s['missing'] == {'outfit': ['human_male']}


def test_refuses_existing_destination(root):
    make_job(root, 'processing', 'a', '1', [('dwarf_male', 'outfit'), ('human_male', 'outfit')])
    (root / 'completed' / 'a--1').mkdir(parents=True)
    s = status(root, 'a', apply=True)
    assert s['moved'] == [] and 'already exists' in s['errors'][0]
    assert (root / 'processing' / 'a--1').is_dir()


def test_set_filter_and_cli_json(root, capsys):
    make_job(root, 'processing', 'a', '1', [('dwarf_male', 'outfit'), ('human_male', 'outfit')])
    make_job(root, 'processing', 'b', '1', [('dwarf_male', 'outfit')])
    assert finish_sets.main(['--root', str(root), '--set', 'b', '--json']) == 0
    data = json.loads(capsys.readouterr().out)
    assert [s['set'] for s in data] == ['b'] and not data[0]['complete']
