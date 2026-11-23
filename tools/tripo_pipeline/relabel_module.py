"""Re-run ONLY the reduction (and optionally the split) of already converted modules from their saved high-poly source (2026-10-24).

    python3 tools/tripo_pipeline/relabel_module.py <module_dir> [<module_dir> ...] [--backup-root DIR] [--threads 3] [--resplit]
        [--dry-run] [--tag relabel_20261024] [--feature-labels SPEC|none]

<module_dir> = models/<set>/<job-id>/<race>/<module>. Nothing is generated or submitted: no Tripo, Hyper3D, formy3d, browser or network
call; only `source.glb` and the job record are read. Use it after a change to the reduction front matter (e.g. the `orient_consistent`
step after `adaptive.local_repair`) for the modules whose saved levels were reduced from a flipped / inconsistent mesh.

Per module, in order:
1. The previous outputs are MOVED (same filesystem, no copy) to `<backup-root>/<set>/<job>/<race>/<module>/`: `reduced/` (LOD GLBs,
   `_colors.npz`, manifest, review.blend, wearable GLBs/blend), `wearcut/`, `highpoly.blend`; with `--resplit` also `segment_first/`,
   `pieces/`, `split_step.json`. Default backup root: experiments/relabel_20261024/backups.
2. `reduce_model.py` runs on `source.glb` with the flags the original reduction recorded in its manifest (joint role in the palette =
   --separable, hue_fit mode = --adaptive-hue, omitted views; wearable method cut = the default). Log: `<module>/reduce_<tag>.log`.
3. On success `.tripo-job.json` `module_conversion.<race>/<module>` gets the new `levels_needing_review` and `wearable` (and `split` with
   `--resplit`) plus a `relabel` note, under the same flock `convert_job.update_record` uses; every other field is kept. On failure the
   moved folders are put back and the module is left as it was.
A module with `segment_first/` or `pieces/` output is refused unless `--resplit` (its pieces would no longer match the levels).
Feature label passes (feature_hooks.py): the re-run uses the module's resolved spec (`--feature-labels`, else the job record's `feature_labels`,
else config.json `feature_labels.sets[<set>]`; `none` = off), so a reconverted studs / chain-mail set keeps its pass in the cut wearable, the
labelled levels and (with `--resplit`) the pieces.
Exit code 1 when any module failed or was refused.
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import convert_job as cj  # noqa: E402
import feature_hooks  # noqa: E402
from wearcut_rules import wearable_record  # noqa: E402

BLENDER = cj.portable.default_blender()
MODULES = ('body_armor', 'right_gauntlet_boot', 'outfit')
JOB_STATES = ('processing', 'completed', 'multiview_ready', 'processing_views', 'failed')
LEVEL_OUTPUTS = ('reduced', 'wearcut', 'highpoly.blend', 'highpoly.blend1')
SPLIT_OUTPUTS = ('segment_first', 'pieces', 'split_step.json', 'pieces_per_face.log')


def identify(module_dir):
    """(set, job id, race, module) of models/<set>/<job>/<race>/<module>."""
    parts = Path(module_dir).resolve().parts
    if len(parts) < 5 or parts[-5] != 'models':
        raise ValueError(f'not a models/<set>/<job>/<race>/<module> folder: {module_dir}')
    set_name, job_id, race, module = parts[-4:]
    if module not in MODULES:
        raise ValueError(f'unknown module {module!r} in {module_dir}')
    return set_name, job_id, race, module


def job_folder(set_name, job_id):
    for state in JOB_STATES:
        candidate = ROOT / state / f'{set_name}--{job_id}'
        if (candidate / '.tripo-job.json').is_file():
            return candidate
    raise FileNotFoundError(f'no job folder with .tripo-job.json for {set_name}--{job_id} in {JOB_STATES}')


def move_in(out, backup, names):
    """Move the named children of `out` that exist into `backup`; returns the list moved."""
    moved = []
    backup.mkdir(parents=True, exist_ok=True)
    for name in names:
        if (out / name).exists():
            if (backup / name).exists():
                raise FileExistsError(f'{backup / name} already exists; refusing to overwrite a backup')
            os.rename(out / name, backup / name)
            moved.append(name)
    return moved


def move_back(out, backup, names):
    for name in names:
        target = out / name
        if target.exists():
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        if (backup / name).exists():
            os.rename(backup / name, target)


def relabel(module_dir, backup_root, threads, resplit, tag, dry_run, feature_labels=None, reason=None):
    out = Path(module_dir).resolve()
    set_name, job_id, race, module = identify(out)
    key = f'{race}/{module}'
    job = job_folder(set_name, job_id)
    record_path = job / '.tripo-job.json'
    old_manifest = out / 'reduced' / 'manifest.json'
    if not old_manifest.is_file() or not (out / 'source.glb').is_file():
        return {'status': 'refused', 'why': 'no reduced/manifest.json or source.glb'}
    has_split = [n for n in ('segment_first', 'pieces') if (out / n).exists()]
    if has_split and not resplit:
        return {'status': 'refused', 'why': f'has {"/".join(has_split)} output; pass --resplit to rebuild it too'}
    previous = json.loads(old_manifest.read_text(encoding='utf-8-sig'))
    # the flags are read back from what the original reduction recorded (a module converted before palette.py has neither), not
    # re-derived from the job text, so the re-run reproduces its palette: separable = a joint role in the palette, adaptive hue as run
    palette = previous.get('palette') or {}
    separable = bool(palette.get('separable') or 'joint' in (palette.get('classes') or []))
    hue_mode = (previous.get('hue_fit') or {}).get('mode')
    piece_colours = hue_mode == 'on'
    omit_views = list(previous.get('omit_views') or [])
    spec, spec_source = feature_hooks.resolve_spec(set_name, module, cj.read_record(record_path), feature_labels)
    cops = previous.get('knee_cops') or {}
    knee_cops = bool(cops.get('enabled'))     # a knee-cop module is re-run with the band it was reduced with
    command = [BLENDER, '-b', '-t', str(threads), '--python', str(HERE / 'reduce_model.py'), '--',
               str(out / 'source.glb'), str(out / 'reduced')]
    command += (['--separable'] if separable else []) + (['--adaptive-hue', hue_mode] if hue_mode in ('on', 'off') else [])
    command += (['--omit-views', ','.join(omit_views)] if omit_views else [])
    command += (['--feature-labels', spec] if spec else [])
    command += (['--knee-cops', '--knee-cop-band', str(cops['band_fraction'])] if knee_cops else [])
    command += (['--knee-cop-above', str(cops['above_fraction'])] if knee_cops and cops.get('above_fraction') is not None else [])
    backup = Path(backup_root) / set_name / job_id / race / module
    note = {'separable': separable, 'piece_colours': piece_colours, 'omit_views': omit_views, **({'knee_cops': cops['band_fraction']} if knee_cops else {}), 'backup': cj.relative(backup),
            **({'feature_labels': spec, 'feature_labels_source': spec_source} if spec else {}), 'command': ' '.join(command[1:]), 'resplit': bool(resplit and has_split)}
    if dry_run:
        return dict(note, status='dry-run')
    names = list(LEVEL_OUTPUTS) + (list(SPLIT_OUTPUTS) if resplit else [])
    moved = move_in(out, backup, names)
    log = out / f'reduce_{tag}.log'
    start = time.monotonic()
    try:
        with open(log, 'w') as handle:
            run = subprocess.run(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT)
        new_manifest = out / 'reduced' / 'manifest.json'
        if run.returncode != 0 or not new_manifest.is_file():
            raise RuntimeError(f'reduce_model.py exit {run.returncode}; log {log}')
        info = json.loads(new_manifest.read_text(encoding='utf-8-sig'))
        if sorted(info.get('levels', {})) != ['0p1', '0p2', '0p4']:
            raise RuntimeError(f'new manifest has levels {sorted(info.get("levels", {}))}')
    except BaseException as error:
        move_back(out, backup, moved)
        return dict(note, status='failed', error=str(error))
    split = None
    if resplit and has_split:
        old_split = json.loads((backup / 'split_step.json').read_text(encoding='utf-8-sig')) if (backup / 'split_step.json').is_file() else {}
        method = old_split.get('requested') or cj.choose_split_method(None, module, piece_colours, separable)[0]
        split = cj.split_step(out, module, method, f'{old_split.get("why", "")}; rebuilt after {tag}'.strip('; '), spec)
    levels = info['levels']
    review = [k for k, v in levels.items() if not v['mesh_checks_pass'] or v['material_review_required']]
    note['seconds'] = round(time.monotonic() - start)
    change = dict(levels_needing_review=review, wearable=wearable_record(info))
    if spec:
        change['feature_labels'] = {'spec': spec, 'source': spec_source}
    if split is not None:
        change['split'] = split

    def apply(r):
        entry_ = r.setdefault('module_conversion', {}).setdefault(key, {})
        entry_.update(change)
        entry_.setdefault('relabel', []).append({'tag': tag, 'date': datetime.date.today().isoformat(), 'backup': note['backup'],
                                                  'reason': reason or 'reduction front matter now orients the winding (orient_consistent after local_repair)'})
    cj.update_record(record_path, apply)
    return dict(note, status='done', levels_needing_review=review, triangles={k: v['triangles'] for k, v in levels.items()},
                split=None if split is None else split.get('status'))


def main(argv):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('modules', nargs='+')
    p.add_argument('--backup-root', default=str(ROOT / 'experiments/relabel_20261024/backups'))
    p.add_argument('--threads', type=int, default=3, help='Blender threads per module')
    p.add_argument('--resplit', action='store_true', help='also move aside and rebuild segment_first/pieces (split_step)')
    p.add_argument('--tag', default='relabel_20261024')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--reason', help='why the modules are re-reduced (recorded in the job record relabel entry)')
    p.add_argument('--feature-labels', metavar='SPEC', help='studs | rings:secondary | rings:void | none; default: the job record / config.json choice for the set')
    a = p.parse_args(argv)
    failed = 0
    for module_dir in a.modules:
        try:
            result = relabel(module_dir, a.backup_root, a.threads, a.resplit, a.tag, a.dry_run, a.feature_labels, a.reason)
        except Exception as error:  # noqa: BLE001  one bad module must not stop the others
            result = {'status': 'failed', 'error': repr(error)}
        failed += result['status'] in ('failed', 'refused')
        print(json.dumps({str(module_dir): result}, indent=1), flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
