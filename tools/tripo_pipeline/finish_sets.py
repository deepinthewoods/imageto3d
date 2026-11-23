#!/usr/bin/env python3
"""Move a set's converted jobs from processing/ to completed/ once it is converted for every race.

    python3 tools/tripo_pipeline/finish_sets.py [--root DIR] [--set SET ...] [--apply] [--json]

Rule (user, 2026-10-26): a set leaves the pipeline (and shows up in the preview site's Models tab) only when each
module of the set is converted for every required race, and no job of the set is still in processing_views/,
multiview_ready/ or an unconverted processing/ job. Required races are the folders of base_views/ with a
palette.json, gnomes included (user, 2026-11-14: gnomes are processed like every other race; before that a set did
not wait for its gnome). A job whose set id ends in `_<race>`
(plain_plate_mail_01_gnome, also `_gnome_female`) belongs to the base set. Everything in failed/ is ignored. The default is a dry run;
`--apply` moves each converted processing/ job whose own modules all have their reduced artifacts
to completed/<same name> and adds a `completed` record to its .tripo-job.json. The move is serialized by a flock,
so queue workers can call it concurrently.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import portable  # noqa: E402
STAGES = ('processing_views', 'multiview_ready', 'processing', 'completed')
ARTIFACTS = ('manifest.json', 'lod_error_0p4.glb', 'lod_error_0p2.glb', 'lod_error_0p1.glb',
             'lod_error_0p4__wearable.glb')
REASON = 'set converted for every race'


def races_of(root):
    """(required, all) race folders of base_views/ that carry a palette.json."""
    found = sorted(p.parent.name for p in (root / 'base_views').glob('*/palette.json'))
    return list(found), found


def set_of(name, races):
    """Set id of a job folder name, with a trailing gnome `_<race>` or `_<race family>` (`_gnome`) removed: only gnome
    parts ever got their own set id, and other suffixes (`_elf`, `_orc`) could be part of a real set name."""
    set_id = name.split('--')[0]
    gnomes = [race for race in races if race.startswith('gnome')]
    for suffix in sorted({s for race in gnomes for s in (race, race.rsplit('_', 1)[0])}, key=len, reverse=True):
        if set_id.endswith('_' + suffix):
            return set_id[:-len(suffix) - 1]
    return set_id


def read_job(path):
    try:
        record = json.loads((path / '.tripo-job.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def job_modules(path, record):
    """[(race, module)] from the record, or from the <race>/<module>/ folders when there is none."""
    if record is not None:
        return sorted({(m['race_gender'], m['module']) for m in record.get('modules', [])
                       if isinstance(m, dict) and 'race_gender' in m and 'module' in m})
    return sorted((r.name, m.name) for r in path.iterdir() if r.is_dir() and not r.name.startswith('.')
                  for m in r.iterdir() if m.is_dir() and not m.name.startswith('.'))


def missing_artifacts(root, job, race, module):
    base = root / 'models' / job['model_set'] / job['job_id'] / race / module / 'reduced'
    return [name for name in ARTIFACTS if not (base / name).is_file()]


def scan(root, races):
    """Every job outside failed/, grouped by set id."""
    sets = {}
    for stage in STAGES:
        for path in sorted((root / stage).glob('*--*')):
            if not path.is_dir():
                continue
            record = read_job(path)
            set_dir, job_dir = path.name.split('--', 1)
            job = {'name': path.name, 'stage': stage, 'path': path, 'record': record,
                   'state': record.get('state') if record else None,
                   'model_set': (record or {}).get('set') or set_dir, 'job_id': (record or {}).get('job_id') or job_dir,
                   'modules': job_modules(path, record)}
            job['converted'] = str(job['state'] or '').startswith('converted')
            sets.setdefault(set_of(path.name, races), []).append(job)
    return sets


def judge(root, set_id, jobs, required):
    """Status of one set: complete flag, missing {module: [races]}, in-progress jobs, jobs to move, blocked jobs."""
    modules = sorted({m for job in jobs for _, m in job['modules']})
    done, blocked = set(), []
    for job in jobs:
        if job['stage'] not in ('processing', 'completed') or not job['converted']:
            continue
        gaps = {}
        for race, module in job['modules']:
            lacking = missing_artifacts(root, job, race, module)
            if lacking:
                gaps[f'{race}/{module}'] = lacking
            else:
                done.add((race, module))
        if gaps and job['stage'] == 'processing':
            blocked.append({'job': job['name'], 'missing': gaps})
    missing = {m: [r for r in required if (r, m) not in done] for m in modules}
    missing = {m: races for m, races in missing.items() if races}
    in_progress = []
    for job in jobs:
        if job['stage'] == 'completed' or (job['stage'] == 'processing' and job['converted']):
            continue
        in_progress.append(f"{job['stage']}/{job['name']}")
    complete = bool(modules) and not missing and not in_progress
    blocked_names = {b['job'] for b in blocked}
    movable = [job['name'] for job in jobs if complete and job['stage'] == 'processing' and job['converted']
               and job['name'] not in blocked_names]
    return {'set': set_id, 'complete': complete, 'modules': modules, 'missing': missing, 'in_progress': in_progress,
            'blocked': blocked if complete else [], 'would_move': movable, 'moved': [], 'errors': []}


def mark_completed(path):
    from convert_job import update_record
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    update_record(path / '.tripo-job.json', lambda record: record.update(
        {'completed': {'time': stamp, 'by': 'finish_sets.py', 'reason': REASON}}))


def move_jobs(root, status):
    """Move the set's jobs (re-checked under the lock by the caller) to completed/; fill status moved/errors."""
    for name in status['would_move']:
        src, dst = root / 'processing' / name, root / 'completed' / name
        if not src.is_dir():
            status['errors'].append(f'{name}: already gone from processing/ (moved by another worker?)')
        elif dst.exists():
            status['errors'].append(f'{name}: refusing to move, {dst} already exists')
        else:
            try:
                (root / 'completed').mkdir(exist_ok=True)
                os.rename(src, dst)
            except OSError as error:
                status['errors'].append(f'{name}: move failed: {error}')
                continue
            status['moved'].append(name)
            try:
                mark_completed(dst)
            except Exception as error:  # the folder is already moved; report the missing record
                status['errors'].append(f'{name}: moved, but writing the completed record failed: {error}')


def run(root=ROOT, only=None, apply=False):
    """Status dicts for the sets (all, or those in `only`); with `apply`, moves the complete ones' jobs."""
    root = Path(root)
    required, every = races_of(root)
    if apply:
        lock = root / '.codex_jobs' / '_batch' / 'finish_sets.lock'
        lock.parent.mkdir(parents=True, exist_ok=True)
        with portable.file_lock(lock, exclusive=True):
            return _run(root, required, every, only, True)
    return _run(root, required, every, only, False)


def _run(root, required, every, only, apply):
    results = []
    for set_id, jobs in sorted(scan(root, every).items()):
        if only and set_id not in only:
            continue
        status = judge(root, set_id, jobs, required)
        if apply and status['would_move']:
            move_jobs(root, status)
        results.append(status)
    return results


def render(results, apply):
    lines = []
    for s in results:
        lines.append(f"[{'complete' if s['complete'] else 'incomplete'}] {s['set']}")
        if not s['modules']:
            lines.append('    no modules known (jobs without a record)')
        for module, races in s['missing'].items():
            lines.append(f"    missing {module}: {', '.join(races)}")
        for job in s['in_progress']:
            lines.append(f'    in progress: {job}')
        for item in s['blocked']:
            gaps = '; '.join(f"{key}: {', '.join(names)}" for key, names in item['missing'].items())
            lines.append(f"    stays (missing artifacts): {item['job']} -> {gaps}")
        for name in s['moved'] if apply else s['would_move']:
            lines.append(f"    {'moved' if apply else 'would move'}: processing/{name} -> completed/{name}")
        lines.extend(f'    ERROR {error}' for error in s['errors'])
    done = [s for s in results if s['complete']]
    count = sum(len(s['moved'] if apply else s['would_move']) for s in results)
    lines.append(f"{len(done)} complete, {len(results) - len(done)} incomplete set(s); "
                 f"{count} job(s) {'moved' if apply else 'would move (dry run, use --apply)'}")
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--set', action='append', default=[], dest='sets', help='only this set id (repeatable)')
    parser.add_argument('--apply', action='store_true', help='move the jobs (default: dry run)')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    results = run(args.root, set(args.sets) or None, args.apply)
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(render(results, args.apply))
    return 1 if any(s['errors'] for s in results) else 0


if __name__ == '__main__':
    sys.exit(main())
