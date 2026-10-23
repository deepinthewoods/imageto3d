#!/usr/bin/env python3
"""Outfit Studio batch driver (SPEC section 8): plain python3, one headless Blender process per base and step.

    python3 tools/outfit_studio/run.py <command> [--base b|all] [--set s|all] [--lod 0p4] [--batch name|dir]
        [--resume] [--all-status] [--project-dir dir] [--items a,b] [--only clip,..] [--limit N]

Commands: import, validate, export-base, export-anims, export-items, all (= import, validate, export-base,
export-items, export-anims per base; a base whose validation has errors is not exported).
Logs and per-step result JSON go to `<batch>/logs/`. Exit codes: 0 ok, 1 failures (listed), 2 usage."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))        # .../tools, so `outfit_studio` is importable

from outfit_studio import batch, paths  # noqa: E402

STEPS = {'all': ('import', 'validate', 'export-base', 'export-items', 'export-anims')}


def pipeline_bases(set_id):
    from outfit_studio.core import pipeline_source as ps
    return sorted({e['race'] for e in ps.discover() if set_id == 'all' or set_id in (e['set_id'], e.get('source_set'))})


def select_bases(cmd, a):
    known = paths.bases()
    if a.base != 'all':
        bad = [b for b in a.base.split(',') if b not in known]
        if bad:
            sys.exit(f'unknown base {bad}; known: {known}')
        return a.base.split(',')
    if cmd in ('import', 'all'):
        return [b for b in pipeline_bases(a.set) if b in known]
    if cmd in ('validate', 'export-items'):
        return [b for b in known if batch.project_file(b, a.project_dir).exists()]
    return known


def child_args(step, base, a, batch_dir, result):
    argv = ['--base', base, '--set', a.set, '--lod', a.lod, '--batch', batch_dir, '--result', result]
    if a.project_dir:
        argv += ['--project-dir', a.project_dir]
    if a.resume:
        argv.append('--resume')
    if a.all_status:
        argv.append('--all-status')
    if a.items:
        argv += ['--items', a.items]
    if a.reimport:
        argv.append('--reimport')
    if step == 'export-anims':
        argv += (['--only', a.only] if a.only else []) + (['--limit', a.limit] if a.limit else [])
    return argv


def run_step(step, base, a, batch_dir):
    """One command for one base in its own Blender process (export-anims drives its own chunk processes)."""
    logs = Path(batch_dir) / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    result, log = logs / f'{step}_{base}.json', logs / f'{step}_{base}.log'
    result.unlink(missing_ok=True)
    t0 = time.time()
    code = batch.run(step, child_args(step, base, a, batch_dir, result), log)
    r = json.loads(result.read_text()) if result.exists() else {'ok': False, 'failed': [{'error': f'no result (exit {code}); see {log}'}]}
    r.setdefault('ok', code == 0)
    r['ok'] = r['ok'] and code == 0
    r.update(step=step, base=base, exit=code, log=str(log), wall=round(time.time() - t0, 1))
    return r


def describe(r):
    bits = [f"{r['step']:13} {r['base']:16} {'ok' if r['ok'] else 'FAILED':6} {r['wall']}s"]
    if r['step'] == 'import':
        bits.append(f"imported {len(r.get('imported', []))}, skipped {len(r.get('skipped', []))}" + (' (project created)' if r.get('created') else '')
                    + (f" - {r['note']}" if r.get('note') else ''))
    elif r['step'] == 'validate':
        bits.append(f"{r.get('errors', '?')} errors, {r.get('warnings', '?')} warnings")
    elif r['step'] == 'export-items':
        res = r.get('results', [])
        bits.append(f"{sum(x['status'] == 'exported' for x in res)} exported, {sum(x['status'] == 'skipped' for x in res)} skipped")
    for f in r.get('failed', []):
        bits.append(f"\n    FAILED {f.get('item_id', f.get('set_id', ''))} {f.get('error', '')}")
    if r['step'] == 'validate':
        bits += [f"\n    {x['level']} {x['item_id']}/{x['piece']}: {x['code']}: {x['message']}" for x in r.get('results', []) if x['level'] == 'error']
    return ' '.join(bits)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('command', choices=batch.COMMANDS + ('all',))
    p.add_argument('--base', default='all', help='base name(s, comma separated) or all')
    p.add_argument('--set', default='all')
    p.add_argument('--lod', default='0p4')
    p.add_argument('--batch', default=None, help='batch name under export/outfit_studio or an absolute folder (default YYYYMMDD-vN)')
    p.add_argument('--resume', action='store_true', help='skip / complete what exists in the batch instead of refusing to overwrite')
    p.add_argument('--all-status', action='store_true', help='export-items: every item, not only approved ones')
    p.add_argument('--reimport', action='store_true')
    p.add_argument('--project-dir', default=None, help=f'outfit projects folder (default {paths.OUTFIT_PROJECTS})')
    p.add_argument('--items', default='')
    p.add_argument('--only', default='')
    p.add_argument('--limit', type=int, default=None)
    a = p.parse_args(argv)
    cmds = STEPS.get(a.command, (a.command,))
    bases = select_bases(a.command, a)
    batch_dir = batch.resolve_batch(a.batch, a.resume)
    if not bases:
        print('nothing to do: no matching bases')
        return 0
    print(f'batch {batch_dir}\nbases {", ".join(bases)}; steps {", ".join(cmds)}', flush=True)
    results, failed = [], []
    for base in bases:
        for step in cmds:
            r = run_step(step, base, a, batch_dir)
            results.append(r)
            print(describe(r), flush=True)
            if not r['ok']:
                failed.append(r)
                break                                   # later steps of this base depend on this one
    (Path(batch_dir) / 'logs').mkdir(parents=True, exist_ok=True)
    (Path(batch_dir) / 'logs' / f'run_{time.strftime("%H%M%S")}.json').write_text(json.dumps(
        [{k: v for k, v in r.items() if k != 'results'} for r in results], indent=2, default=str) + '\n')
    print(f'{len(results)} steps, {len(failed)} failed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
