#!/usr/bin/env python3
"""Dry-run migration report of the v2 preview app (PIPELINE_V2_PLAN.md section 9). READ-ONLY.

    python tools/preview/v2/migrate.py [--root R] [--out .codex_jobs/_app/migration_report]

Derives every card's v2 stage from today's folders (nothing is moved, deleted or written under the root except the two report
files `<out>.json` and `<out>.md`) and lists what will go where: stage counts, per-set unit status, chosen jobs and conflicts,
existing LOD picks, legacy model approvals, stuck sets, source.glb symlinks onto D:, the convert_sets.txt allowlist effect and
the prototype layer inventory. It is idempotent: running it twice on unchanged folders gives the same numbers.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import serve  # noqa: E402
import catalog  # noqa: E402

SETS_FILE = Path('.codex_jobs') / '_batch' / 'convert_sets.txt'
LAYERS = Path('experiments') / 'shell_slots_20261122' / 'out' / 'layers'


def counts_by(items, key) -> dict:
    out: dict = {}
    for item in items:
        out[key(item)] = out.get(key(item), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: str(kv[0])))


def source_links(root: Path) -> dict:
    """source.glb symlinks of models/<set>/<job>/<race>/<module>: how many, where they point (drive), how many are broken."""
    total = on_d = broken = 0
    other: dict[str, int] = {}
    examples = []
    models = root / 'models'
    for set_dir in sorted(serve._subdirs(str(models))):
        for job in serve._subdirs(f'{models}/{set_dir}'):
            for race in serve._subdirs(f'{models}/{set_dir}/{job}'):
                for module in serve._subdirs(f'{models}/{set_dir}/{job}/{race}'):
                    link = models / set_dir / job / race / module / 'source.glb'
                    if not os.path.islink(link):
                        continue
                    total += 1
                    target = os.readlink(link).replace('\\\\?\\', '').replace('\\', '/')
                    drive = target[:2].upper() if len(target) > 1 and target[1] == ':' else ('/mnt/' + target.split('/')[2].upper() if target.startswith('/mnt/') else 'relative/other')
                    if drive in ('D:', '/mnt/D'):
                        on_d += 1
                        if len(examples) < 3:
                            examples.append(target)
                    else:
                        other[drive] = other.get(drive, 0) + 1
                    if not os.path.exists(link):
                        broken += 1
    return {'symlinks': total, 'point_to_d': on_d, 'other_targets': other, 'broken_now': broken, 'd_drive_reachable': os.path.isdir('D:\\'),
            'examples': examples}


def allowlist(root: Path) -> dict:
    """Effect of .codex_jobs/_batch/convert_sets.txt on jobs the old conversion worker would pick (convert_queue.next_job rule)."""
    path = root / SETS_FILE
    try:
        lines = [l.strip() for l in path.read_text(encoding='utf-8').splitlines()]
    except OSError:
        return {'file': None}
    patterns = [l for l in lines if l and not l.startswith('#')]
    allow = [p for p in patterns if not p.startswith('!')]
    deny = [p[1:] for p in patterns if p.startswith('!')]
    skipped, picked = [], []
    for stage in ('multiview_ready', 'processing'):
        for name in sorted(serve._subdirs(str(root / stage))):
            if '--' not in name:
                continue
            set_id = name.split('--')[0]
            record = catalog.read_json(root / stage / name / '.tripo-job.json')
            if stage == 'processing' and str(record.get('state') or '').startswith('converted'):
                continue          # already converted: the worker never looks at it
            ok = any(fnmatch.fnmatchcase(set_id, p) for p in allow) and not any(fnmatch.fnmatchcase(set_id, p) for p in deny)
            (picked if ok else skipped).append(f'{stage}/{name}')
    return {'file': str(SETS_FILE).replace('\\', '/'), 'patterns': len(allow), 'excludes': len(deny), 'would_convert': len(picked),
            'would_skip': len(skipped), 'skipped_jobs': skipped}


def layers_inventory(root: Path) -> dict:
    base = root / LAYERS
    if not base.is_dir():
        return {'path': str(LAYERS).replace('\\', '/'), 'exists': False}
    files = races = 0
    for _dir, subdirs, names in os.walk(base):
        files += sum(1 for n in names if n.lower().endswith('.glb'))
    races = len([d for d in os.listdir(base) if (base / d).is_dir()])
    return {'path': str(LAYERS).replace('\\', '/'), 'exists': True, 'glb_files': files, 'race_folders': races}


def build_report(root: Path) -> dict:
    catalog.STATE_TTL = 0
    state = catalog.state(root)
    derived = catalog.derive_all(root)
    races = derived['races']
    sets = derived['sets']
    models = catalog.scan_models(root, races, {name: catalog.model_sets(name, info['jobs']) for name, info in sets.items()})
    retired = derived['retired']
    stage_counts = {'characters': {}, 'monsters': {}}
    for tab, cards in state['tabs'].items():
        for card in cards:
            bucket = stage_counts['monsters' if card['type'] == 'monster' else 'characters']
            bucket[tab] = bucket.get(tab, 0) + 1
    per_set, conflicts, stuck, legacy_approval, in_3d_from_completed, completed_not_in_3d = {}, [], [], [], [], []
    picks_all: dict[str, int] = {}
    picks_chosen: dict[str, int] = {}
    unpicked = picks_elsewhere = 0
    for name, info in sets.items():
        units = info['units']
        job_stage = {j['id']: j['stage'] for j in info['jobs']}
        jobs_models = (models.get(name) or {}).get('jobs', {})
        for rm in jobs_models.values():
            for modules in rm.values():
                for entry in modules.values():
                    if entry['reduced'] and entry['lod']:
                        picks_all[entry['lod']] = picks_all.get(entry['lod'], 0) + 1
        for unit in units:
            if unit['status'] != 'reduced':
                continue
            if unit['lod_saved']:
                picks_chosen[unit['lod']] = picks_chosen.get(unit['lod'], 0) + 1
            else:
                unpicked += 1
                if any(rm.get(unit['race'], {}).get(unit['module'], {}).get('lod') for jid, rm in jobs_models.items() if jid != unit['job']):
                    picks_elsewhere += 1
            if unit.get('multi'):
                conflicts.append({'set': name, 'unit': f'{unit["race"]}/{unit["module"]}', 'jobs': unit['multi'], 'chosen': unit['job']})
        status = counts_by(units, lambda u: u['status'])
        bad = [f'{u["race"]}/{u["module"]}={u["status"]}' for u in units if u['status'] in ('failed', 'missing')]
        if info['stage'] == 'approved' and status.get('reduced') and bad:
            stuck.append({'set': name, 'reduced': status.get('reduced'), 'problem_units': bad[:30], 'problem_count': len(bad)})
        marker = serve.read_json(root / 'models' / name / serve.MODEL_APPROVAL)
        if marker.get('approved') is True:
            legacy_approval.append({'set': name, 'stage': info['stage'], 'jobs': marker.get('jobs')})
        from_completed = any(s == 'completed' for s in job_stage.values())
        if info['stage'] == '3d' and from_completed:
            in_3d_from_completed.append(name)
        elif from_completed:
            completed_not_in_3d.append({'set': name, 'stage': info['stage'], 'units': status})
        per_set[name] = {'stage': info['stage'], 'kind': info['kind'], 'modules': info['modules'], 'units': status,
                         'jobs': counts_by(info['jobs'], lambda j: j['stage']),
                         'chosen_jobs': {f'{u["race"]}/{u["module"]}': u['job'] for u in units if u['status'] == 'reduced'},
                         'dropped': sorted(info['decisions']['dropped'])}
    known = {ms for name, info in sets.items() for ms in catalog.model_sets(name, info['jobs'])}
    retired_sets = {job['model_set'] for job in retired}
    orphans = sorted(n for n in serve._subdirs(str(root / 'models')) if n not in known and n not in retired_sets)
    retired_report = [{'job': job['rel'], 'set': job['set'], 'model_dir': f'models/{job["model_set"]}' if (root / 'models' / job['model_set']).is_dir() else None}
                      for job in retired]
    summary = {
        'sets_with_jobs': len(sets),
        'units': counts_by([u for i in sets.values() for u in i['units']], lambda u: u['status']),
        'set_stage': counts_by(sets.values(), lambda i: i['stage']),
        'stage_counts': stage_counts,
        'conflict_units': len(conflicts),
        'chosen_lod_files_by_level_all_jobs': dict(sorted(picks_all.items())),
        'picks_on_chosen_units_by_level': dict(sorted(picks_chosen.items())),
        'reduced_units_without_pick': unpicked,
        'unpicked_units_with_pick_in_another_job': picks_elsewhere,
        'legacy_model_approval_sets': len(legacy_approval),
        'sets_in_3d_with_a_completed_job': len(in_3d_from_completed),
        'stuck_sets': len(stuck),
        'sets_with_a_completed_job_not_in_3d': len(completed_not_in_3d),
        'models_dirs_without_job_folder': len(orphans),
        'retired_pixal3d_jobs': len(retired),
    }
    return {'generated': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'root': str(root), 'races': races, 'summary': summary,
            'conflicts': conflicts, 'stuck': stuck, 'legacy_model_approval': legacy_approval, 'in_3d_from_completed': in_3d_from_completed, 'completed_not_in_3d': completed_not_in_3d,
            'models_without_job_folder': orphans, 'retired_pixal3d_jobs': retired_report, 'source_links': source_links(root), 'convert_sets': allowlist(root),
            'prototype_layers': layers_inventory(root), 'sets': per_set}


def to_markdown(report: dict) -> str:
    s = report['summary']
    lines = ['# v2 migration dry run', '', f'Generated {report["generated"]} for `{report["root"]}` (read-only; nothing was moved or deleted).', '',
             '## Stage counts (what the new tabs will show)', '', '| tab | characters | monsters |', '|---|---|---|']
    tabs = sorted({t for b in s['stage_counts'].values() for t in b})
    for tab in tabs:
        lines.append(f'| {tab} | {s["stage_counts"]["characters"].get(tab, 0)} | {s["stage_counts"]["monsters"].get(tab, 0)} |')
    lines += ['', '## Summary', '']
    for key, value in s.items():
        if key != 'stage_counts':
            lines.append(f'- **{key}**: {json.dumps(value)}')
    links, conv, proto = report['source_links'], report['convert_sets'], report['prototype_layers']
    lines += ['', '## source.glb symlinks', '', f'- {links["symlinks"]} symlinks, {links["point_to_d"]} point to D:, other targets {json.dumps(links["other_targets"])}, '
              f'{links["broken_now"]} broken now, D: reachable: {links["d_drive_reachable"]}']
    lines += ['', '## convert_sets.txt allowlist (old conversion worker)', '']
    if conv.get('file'):
        lines.append(f'- {conv["patterns"]} allow patterns, {conv["excludes"]} excludes; of the unconverted jobs in multiview_ready/processing '
                     f'{conv["would_convert"]} would be converted and {conv["would_skip"]} skipped')
        lines += [f'  - skipped: `{j}`' for j in conv['skipped_jobs'][:60]]
    else:
        lines.append('- file not found')
    lines += ['', '## Prototype layer inventory', '', f'- {json.dumps(proto)}']
    lines += ['', f'## Stuck sets ({len(report["stuck"])}): some units reduced, others failed or missing', '']
    for item in report['stuck']:
        lines.append(f'- `{item["set"]}`: reduced {item["reduced"]}, {item["problem_count"]} problem units, e.g. {", ".join(item["problem_units"][:4])}')
    lines += ['', f'## Sets with a job in completed/ that will not be in 3D ({len(report["completed_not_in_3d"])})', '']
    lines += [f'- `{i["set"]}` -> {i["stage"]} {json.dumps(i["units"])}' for i in report['completed_not_in_3d']]
    lines += ['', f'## Conflicts ({len(report["conflicts"])} units with several converted jobs)', '']
    for item in report['conflicts'][:80]:
        lines.append(f'- `{item["set"]}` {item["unit"]}: {", ".join(item["jobs"])} (chosen {item["chosen"]})')
    lines += ['', f'## Legacy model_approval.json ({len(report["legacy_model_approval"])})', '']
    lines += [f'- `{i["set"]}` -> stage {i["stage"]}' for i in report['legacy_model_approval']]
    lines += ['', f'## models/ folders without a job folder anywhere ({len(report["models_without_job_folder"])}; not shown as cards)', '']
    lines += [f'- `{n}`' for n in report['models_without_job_folder']]
    lines += ['', f'## Retired Pixal3D jobs ({len(report["retired_pixal3d_jobs"])}; not active work, no card, listed here only)', '']
    lines += [f'- `{i["job"]}` (set `{i["set"]}`, models: {i["model_dir"] or "none"})' for i in report['retired_pixal3d_jobs']]
    lines += ['', '## Sets', '', '| set | stage | kind | units | jobs |', '|---|---|---|---|---|']
    for name, rec in report['sets'].items():
        lines.append(f'| {name} | {rec["stage"]} | {rec["kind"]} | {json.dumps(rec["units"])} | {json.dumps(rec["jobs"])} |')
    return '\n'.join(lines) + '\n'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=serve.DEFAULT_ROOT)
    parser.add_argument('--out', type=Path, default=Path('.codex_jobs') / '_app' / 'migration_report')
    args = parser.parse_args()
    root = args.root.resolve()
    out = args.out if args.out.is_absolute() else root / args.out
    report = build_report(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_name(out.name + '.json').write_text(json.dumps(report, indent=1), encoding='utf-8')
    out.with_name(out.name + '.md').write_text(to_markdown(report), encoding='utf-8')
    print(json.dumps(report['summary'], indent=1))
    print('stuck:', len(report['stuck']), '| conflicts:', len(report['conflicts']), '| source links:', report['source_links'], '| convert_sets:',
          {k: v for k, v in report['convert_sets'].items() if k != 'skipped_jobs'}, '| layers:', report['prototype_layers'])
    print('wrote', out.with_name(out.name + '.json'), 'and .md')


if __name__ == '__main__':
    main()
