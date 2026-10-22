#!/usr/bin/env python3
"""Validate and stage user-approved front concepts for four-view Tripo jobs.

This tool never generates images, submits a Tripo job or marks one completed.
Only the user moves sets into approved/. The assistant runs claim after a chat
instruction, generates views with imagegen, and runs ready when all are present.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from palette import Palette  # noqa: E402

VIEWS = ('front', 'front_three_quarter', 'back', 'back_three_quarter')
ARMOR = {'body_armor', 'right_gauntlet_boot'}
OUTFIT = {'outfit'}
ANCHOR_MIN_PIXELS = 200      # a base needs at least this many anchor-coloured pixels for the concept check
ANCHOR_MATCH_MIN = 0.5       # share of the base's anchor pixels that must keep an anchor colour in the concept front
EXTRAS = ('transforms.json', 'regions.json', 'parts.npz', 'clearance.json', 'palette.json')
STATES = ('concepts', 'approved', 'processing_views', 'multiview_ready', 'processing', 'completed', 'failed')


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def clean_name(name: str) -> bool:
    return bool(name) and not name.startswith('.') and name not in {'.', '..'} and Path(name).name == name and '/' not in name and '\\' not in name


def check_symlinks(folder: Path) -> None:
    if folder.is_symlink() or any(item.is_symlink() for item in folder.rglob('*')):
        raise ValueError(f'Symlinks are not allowed in a staged set: {folder}')


def check_image(image: Path, base: Path) -> dict:
    if not image.is_file():
        raise ValueError(f'Missing image: {image}')
    if not base.is_file():
        raise ValueError(f'Missing matching base view: {base}')
    with Image.open(base) as target, Image.open(image) as candidate:
        candidate.load()
        if candidate.size != target.size:
            raise ValueError(f'Wrong dimensions: {image} is {candidate.size}, base is {target.size}')
        if candidate.mode != 'RGBA':
            raise ValueError(f'Expected RGBA image: {image}')
        alpha = candidate.getchannel('A')
        minimum, maximum = alpha.getextrema()
        if minimum != 0 or maximum == 0:
            raise ValueError(f'Expected visible subject and transparent background: {image}')
    return {'sha256': digest(image), 'dimensions': list(target.size)}


def flat_fronts(race: Path) -> dict[str, Path] | None:
    """Concept/approved layout: `<race>/<module>_front.png` side by side in one folder.

    Returns {module: path}, or None when the race folder uses per-module subfolders.
    """
    fronts = {p.name[:-len('_front.png')]: p for p in race.iterdir()
              if p.is_file() and p.name.endswith('_front.png')}
    subdirs = [p for p in race.iterdir() if p.is_dir() and not p.name.startswith('.')]
    if fronts and subdirs:
        raise ValueError(f'{race}: mix of flat <module>_front.png files and module folders')
    return fronts or None


def unflatten(race: Path) -> None:
    """Turn the flat concept layout into `<module>/front.png` folders for the view stages.

    Everything else in the folder (prompts, design records, rejected attempts) moves to
    `.concept_notes/` so it travels with the job but is not mistaken for a view or module.
    """
    fronts = flat_fronts(race)
    if not fronts:
        return
    notes = race / '.concept_notes'
    notes.mkdir(exist_ok=True)
    keep = set(fronts.values())
    for path in sorted(race.iterdir()):
        if path.is_file() and path not in keep:
            shutil.move(str(path), notes / path.name)
    for module, path in fronts.items():
        (race / module).mkdir()
        shutil.move(str(path), race / module / 'front.png')


def anchor_match(front: Path, base_front: Path, palette: Palette) -> float | None:
    """Share of `base_front`'s anchor-coloured pixels (the bare head/hand/foot markers of `palette`) that are
    still anchor-coloured in `front`. The bare anchors stay in place in every concept, so a front drawn on a
    base with other anchor colours scores ~0. None when the base shows too few anchor pixels to judge
    (not a marker base) or the sizes differ."""
    with Image.open(base_front) as b, Image.open(front) as c:
        b, c = b.convert('RGBA'), c.convert('RGBA')
    if b.size != c.size:
        return None
    ba, ca = np.asarray(b, float) / 255, np.asarray(c, float) / 255
    region = (ba[..., 3] > 0.5) & palette.anchor_mask(ba[..., :3].reshape(-1, 3)).reshape(ba.shape[:2])
    if region.sum() < ANCHOR_MIN_PIXELS:
        return None
    same = (ca[..., 3] > 0.5) & palette.anchor_mask(ca[..., :3].reshape(-1, 3)).reshape(ca.shape[:2])
    return float((region & same).sum() / region.sum())


def base_candidates(root: Path, race: str) -> list[Path]:
    """The active base first, then archived sets that carry a palette.json (made when a palette scheme
    was retired: the scheme, cameras and anchors are documented there; older archives are only
    usable through --base-views)."""
    found = [root / 'base_views' / race]
    for path in sorted((root / 'archive').glob(f'*/{race}')) if (root / 'archive').is_dir() else []:
        if (path / 'palette.json').is_file() and all((path / (v + '.png')).is_file() for v in VIEWS):
            found.append(path)
    return found


def pick_base(root: Path, race: str, fronts: dict[str, Path], override: Path | None) -> tuple[Path, dict]:
    """Which base folder to snapshot for `race`: the one whose anchor colours the approved fronts were drawn on.

    The active base wins when every front matches it. Otherwise exactly one archived base (or several with
    byte-identical views) must match, else ValueError (fail closed). `override` (`claim --base-views <dir>`,
    a folder of `<race>` base folders) is used as given. The returned record notes the scores."""
    def scores(base: Path):
        base_front = base / 'front.png'
        if not base_front.is_file():
            return None
        palette = Palette.for_dir(base)
        values = [anchor_match(path, base_front, palette) for path in fronts.values()]
        return None if any(v is None for v in values) else {m: v for m, v in zip(fronts, values)}
    def describe(base: Path, how: str, sc):
        try:
            shown = base.relative_to(root).as_posix()
        except ValueError:
            shown = str(base)
        return {'path': shown, 'selected_by': how, 'palette_scheme': Palette.for_dir(base).scheme,
                'anchor_match': None if sc is None else {m: round(v, 3) for m, v in sc.items()}}
    if override is not None:
        chosen = override / race
        if not chosen.is_dir():
            raise ValueError(f'--base-views {override} has no {race} folder')
        return chosen, describe(chosen, '--base-views override', scores(chosen))
    current = root / 'base_views' / race
    cur = scores(current)
    if cur is None or all(v >= ANCHOR_MATCH_MIN for v in cur.values()):
        return current, describe(current, 'active base' if cur is not None else 'active base (anchor check skipped: '
                                 'no marker anchors to compare)', cur)
    matching = []
    report = {current.relative_to(root).as_posix(): cur}
    for candidate in base_candidates(root, race)[1:]:
        sc = scores(candidate)
        report[candidate.relative_to(root).as_posix()] = sc
        if sc is not None and all(v >= ANCHOR_MATCH_MIN for v in sc.values()):
            matching.append((candidate, sc))
    detail = '; '.join(f'{k}: {v if v is None else {m: round(x, 2) for m, x in v.items()}}' for k, v in report.items())
    if not matching:
        raise ValueError(f'{race}: the approved fronts match neither the active base nor an archived palette base '
                         f'({detail}). Pass --base-views <folder of {race} bases> if they were drawn on another base')
    if len({tuple(digest(c / (v + '.png')) for v in VIEWS) for c, _ in matching}) > 1:
        raise ValueError(f'{race}: the approved fronts match several different archived bases '
                         f'({", ".join(c.relative_to(root).as_posix() for c, _ in matching)}); pass --base-views')
    chosen, sc = matching[0]
    return chosen, describe(chosen, 'concept anchor colours match this archived base, not the active one', sc)


def validate_set(root: Path, folder: Path, complete: bool, base_dirs: dict[str, Path] | None = None) -> dict:
    if not folder.is_dir() or not clean_name(folder.name):
        raise ValueError(f'Expected a named set directory: {folder}')
    check_symlinks(folder)
    record_path = folder / '.tripo-job.json'
    record = json.loads(record_path.read_text()) if record_path.is_file() else {}
    base_view_root = record.get('base_view_root', 'base_views')
    if base_view_root not in ('base_views', '.base_views'):
        raise ValueError(f'Unexpected base view root: {base_view_root}')
    base_root = folder / '.base_views' if base_view_root == '.base_views' else root / 'base_views'
    races = sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith('.'))
    if not races:
        raise ValueError(f'No <race_gender> directories in {folder}')
    result = {'set': folder.name, 'complete_four_views': complete, 'modules': [], 'images': {}}
    for race in races:
        if not clean_name(race.name):
            raise ValueError(f'Invalid race name: {race.name}')
        base = (base_dirs or {}).get(race.name) or base_root / race.name
        if not base.is_dir() or base.is_symlink():
            raise ValueError(f'Unknown base character: {race.name}')
        if base_view_root == '.base_views':
            for view in VIEWS:
                base_file = base / (view + '.png')
                expected = record.get('base_view_hashes', {}).get(f'{race.name}/{view}.png')
                if not expected or not base_file.is_file() or digest(base_file) != expected:
                    raise ValueError(f'Frozen base view changed or missing: {base_file}')
        flat = flat_fronts(race)
        if flat is not None and complete:
            raise ValueError(f'{race}: four-view jobs use <module>/<view>.png folders, found flat fronts')
        modules = set(flat) if flat is not None else {
            p.name for p in race.iterdir() if p.is_dir() and not p.name.startswith('.')}
        if modules not in (ARMOR, OUTFIT):
            raise ValueError(f'{race.name} needs both armor modules or one outfit module; found {sorted(modules)}')
        for module in sorted(modules):
            source = race / module
            wanted = VIEWS if complete else VIEWS[:1]
            if flat is None:
                actual = {p.name for p in source.iterdir() if p.suffix.lower() == '.png'}
                if actual != {view + '.png' for view in wanted}:
                    raise ValueError(f'{source}: expected exactly {list(wanted)} PNG views, found {sorted(actual)}')
            result['modules'].append({'race_gender': race.name, 'module': module})
            for view in wanted:
                relative = Path(race.name) / module / (view + '.png')
                if flat is not None:
                    result['images'][relative.as_posix()] = check_image(flat[module], base / (view + '.png'))
                    continue
                result['images'][relative.as_posix()] = check_image(folder / relative, base / (view + '.png'))
    return result


def stage_status(root: Path) -> dict:
    return {name: sorted(p.name for p in (root / name).iterdir() if p.is_dir())
            if (root / name).is_dir() else [] for name in STATES}


def claim(root: Path, set_name: str, base_views: Path | None = None) -> dict:
    if not clean_name(set_name):
        raise ValueError('Set name must be a single safe folder name')
    if root == Path(__file__).resolve().parents[2]:
        service = subprocess.run(['systemctl', '--user', 'is-active', 'pixal3d-armor-queue.service'],
                                 capture_output=True, text=True, check=False)
        if service.stdout.strip() == 'active':
            raise ValueError('Retired Pixal3D watcher is active; stop it before claiming a Tripo set')
    source = root / 'approved' / set_name
    # Which base were the fronts drawn on? The concept's bare anchor colours say (a front made before the
    # base was recoloured must not be frozen with the new base); fail closed when they do not.
    base_dirs, base_sources = {}, {}
    if source.is_dir():
        for race in sorted(p for p in source.iterdir() if p.is_dir() and not p.name.startswith('.')):
            fronts = flat_fronts(race) if clean_name(race.name) else None
            active = root / 'base_views' / race.name
            if fronts and (base_views is not None or (active.is_dir() and not active.is_symlink())):
                base_dirs[race.name], base_sources[race.name] = pick_base(root, race.name, fronts, base_views)
    details = validate_set(root, source, complete=False, base_dirs=base_dirs)
    if (source / '.tripo-job.json').exists():
        raise ValueError('Set already has a job record; inspect it before retrying')
    snapshot = source / '.base_views'
    if snapshot.exists():
        raise ValueError(f'Unexpected base snapshot in approved set: {snapshot}')
    base_hashes = {}
    for race in sorted({module['race_gender'] for module in details['modules']}):
        frozen = snapshot / race
        frozen.mkdir(parents=True)
        for view in VIEWS:
            filename = view + '.png'
            source_base = base_dirs.get(race, root / 'base_views' / race) / filename
            if source_base.is_symlink() or not source_base.is_file():
                raise ValueError(f'Missing or linked base view: {source_base}')
            shutil.copy2(source_base, frozen / filename)
            base_hashes[f'{race}/{filename}'] = digest(frozen / filename)
        # Cameras, body landmarks and per-face body parts the concept views were made with;
        # reduce_model.py uses them for visibility weighting and the module prior
        # (--cameras/--regions/--parts); clearance.json tells it how far bare skin can stray from the base.
        for extra in EXTRAS:
            source_extra = base_dirs.get(race, root / 'base_views' / race) / extra
            if source_extra.is_file() and not source_extra.is_symlink():
                shutil.copy2(source_extra, frozen / extra)
                base_hashes[f'{race}/{extra}'] = digest(frozen / extra)
    job_id = uuid.uuid4().hex[:12]
    job_name = f'{set_name}--{job_id}'
    destination = root / 'processing_views' / job_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    record = {'schema_version': 1, 'job_id': job_id, 'set': set_name,
              'state': 'generating_other_views', 'front_hashes':
              {p: meta['sha256'] for p, meta in details['images'].items()},
              'base_view_root': '.base_views', 'base_view_hashes': base_hashes,
              'base_view_sources': base_sources,
              'modules': details['modules'], 'conversion': 'pending',
              'tripo_upload_started': False}
    write_json(source / '.tripo-job.json', record)
    for race in sorted(p for p in source.iterdir() if p.is_dir() and not p.name.startswith('.')):
        unflatten(race)
    os.rename(source, destination)
    return {'job': job_name, 'path': str(destination), 'modules': details['modules'], 'base_views': base_sources}


BATCH_FILE = re.compile(r'^(?P<nn>\d+)_(?P<slug>[a-z0-9][a-z0-9_-]*)__(?P<module>body_armor|right_gauntlet_boot|outfit)\.png$')


def promote(root: Path, batch: Path, number: str, set_id: str | None = None, race: str | None = None,
            approve: bool = False) -> dict:
    """Copy one concept from a flat batch folder into the per-set layout (never overwrites).

    A batch (`concepts/<batch_id>/`) is flat: `<NN>_<slug>__<module>.png` per module plus `<NN>_<slug>.json`
    (brief, palette, per-module prompt/sha256, framing result...). The chosen NN becomes
    `concepts/<set_id>/<race>/<module>_front.png` + `<module>_prompts.json` (the batch json plus `module` and
    `promoted_from`), or, with `approve`, goes straight into `approved/<set_id>/<race>/` when the user said so.
    `set_id` defaults to `<batch_id>_<NN>_<slug>`; `race` to the json's `race_gender`. Fails closed: ambiguous or
    incomplete sets, a missing/incorrect record, wrong image size/alpha, an existing destination.
    """
    batch = batch.resolve()
    if not batch.is_dir() or batch.is_symlink():
        raise ValueError(f'Not a batch folder: {batch}')
    if not str(number).isdigit():
        raise ValueError(f'Concept number must be digits like 07: {number!r}')
    found = {}
    for path in sorted(batch.iterdir()):
        match = BATCH_FILE.match(path.name)
        if match and path.is_file() and not path.is_symlink() and int(match['nn']) == int(number):
            found.setdefault((match['nn'], match['slug']), {})[match['module']] = path
    if not found:
        raise ValueError(f'No concept {number} in {batch}')
    if len(found) > 1:
        raise ValueError(f'Concept {number} is ambiguous in {batch}: {sorted(f"{n}_{s}" for n, s in found)}')
    (nn, slug), files = next(iter(found.items()))
    if set(files) not in (ARMOR, OUTFIT):
        raise ValueError(f'Concept {nn}_{slug} needs both armor modules or one outfit; found {sorted(files)}')
    record_path = batch / f'{nn}_{slug}.json'
    if not record_path.is_file():
        raise ValueError(f'Missing record {record_path.name}')
    meta = json.loads(record_path.read_text())
    race = race or meta.get('race_gender')
    if not race or not clean_name(race):
        raise ValueError('Race unknown: pass --race <race_gender> (or put race_gender in the concept json)')
    base = root / 'base_views' / race / 'front.png'
    set_id = set_id or f'{batch.name}_{nn}_{slug}'
    if not clean_name(set_id):
        raise ValueError(f'Bad set id: {set_id}')
    target = root / ('approved' if approve else 'concepts') / set_id
    if target.exists():
        raise FileExistsError(f'{target} already exists; promote never overwrites')
    hashes = {}
    for module, path in files.items():
        hashes[module] = check_image(path, base)['sha256']
        recorded = ((meta.get('modules') or {}).get(module) or {}).get('sha256')
        if recorded and recorded != hashes[module]:
            raise ValueError(f'{path.name} does not match the sha256 in {record_path.name}')
    temp = target.parent / f'.promote_{uuid.uuid4().hex[:8]}'
    try:
        folder = temp / race
        folder.mkdir(parents=True)
        for module, path in files.items():
            shutil.copy2(path, folder / f'{module}_front.png')
            write_json(folder / f'{module}_prompts.json', {
                **meta, 'module': module,
                'promoted_from': {'batch': batch.name, 'number': nn, 'slug': slug, 'file': path.name,
                                  'sha256': hashes[module], 'record': record_path.name}})
        os.rename(temp, target)
    finally:
        if temp.exists():
            shutil.rmtree(temp)
    return {'set': set_id, 'path': str(target), 'race_gender': race, 'modules': sorted(files),
            'stage': 'approved' if approve else 'concepts'}


def ready(root: Path, job_name: str) -> dict:
    if not clean_name(job_name):
        raise ValueError('Job name must be a single safe folder name')
    source = root / 'processing_views' / job_name
    details = validate_set(root, source, complete=True)
    record_path = source / '.tripo-job.json'
    if not record_path.is_file():
        raise ValueError('Claimed job record is missing')
    record = json.loads(record_path.read_text())
    if record.get('state') != 'generating_other_views':
        raise ValueError(f'Unexpected job state: {record.get("state")}')
    if job_name != f'{record["set"]}--{record["job_id"]}':
        raise ValueError('Job folder does not match frozen record')
    for path, sha in record['front_hashes'].items():
        if details['images'].get(path, {}).get('sha256') != sha:
            raise ValueError(f'Approved front changed after claiming: {path}')
    destination = root / 'multiview_ready' / job_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    record['state'] = 'ready_for_tripo'
    record['view_hashes'] = {p: meta['sha256'] for p, meta in details['images'].items()}
    record['conversion'] = 'pending_playwright_worker'
    write_json(record_path, record)
    os.rename(source, destination)
    return {'job': job_name, 'path': str(destination), 'views': len(details['images']),
            'conversion': record['conversion']}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    check = sub.add_parser('check', help='Validate a set in approved/ or a staged four-view job')
    check.add_argument('folder', type=Path)
    check.add_argument('--complete', action='store_true')
    take = sub.add_parser('claim', help='Move a user-approved front set to processing_views/')
    take.add_argument('set_name')
    take.add_argument('--base-views', type=Path, help='folder of <race_gender> base folders to snapshot instead '
                      'of the auto-detected one (e.g. archive/base_views_markers_tricolor_20261022); the folder '
                      'is used as given, without the anchor-colour check')
    move = sub.add_parser('promote', help='Copy concept NN of a flat batch folder into the per-set layout')
    move.add_argument('batch_dir', type=Path)
    move.add_argument('number')
    move.add_argument('--set', dest='set_id', help='set id (default <batch>_<NN>_<slug>)')
    move.add_argument('--race', help='race_gender (default: race_gender in the concept json)')
    move.add_argument('--approve', action='store_true', help='put it straight into approved/ (only when the user said so)')
    publish = sub.add_parser('ready', help='Validate all four views and move to multiview_ready/')
    publish.add_argument('job_name')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == 'status': result = stage_status(root)
        elif args.command == 'check': result = validate_set(root, args.folder.resolve(), complete=args.complete)
        elif args.command == 'promote': result = promote(root, args.batch_dir, args.number, args.set_id, args.race, args.approve)
        elif args.command == 'claim': result = claim(root, args.set_name, args.base_views.resolve() if args.base_views else None)
        else: result = ready(root, args.job_name)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f'WORKFLOW_ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
