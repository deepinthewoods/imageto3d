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
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from PIL import Image

VIEWS = ('front', 'front_three_quarter', 'back', 'back_three_quarter')
ARMOR = {'body_armor', 'right_gauntlet_boot'}
OUTFIT = {'outfit'}
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


def validate_set(root: Path, folder: Path, complete: bool) -> dict:
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
        base = base_root / race.name
        if not base.is_dir() or base.is_symlink():
            raise ValueError(f'Unknown base character: {race.name}')
        if base_view_root == '.base_views':
            for view in VIEWS:
                base_file = base / (view + '.png')
                expected = record.get('base_view_hashes', {}).get(f'{race.name}/{view}.png')
                if not expected or not base_file.is_file() or digest(base_file) != expected:
                    raise ValueError(f'Frozen base view changed or missing: {base_file}')
        modules = {p.name for p in race.iterdir() if p.is_dir()}
        if modules not in (ARMOR, OUTFIT):
            raise ValueError(f'{race.name} needs both armor modules or one outfit module; found {sorted(modules)}')
        for module in sorted(modules):
            source = race / module
            wanted = VIEWS if complete else VIEWS[:1]
            actual = {p.name for p in source.iterdir() if p.suffix.lower() == '.png'}
            if actual != {view + '.png' for view in wanted}:
                raise ValueError(f'{source}: expected exactly {list(wanted)} PNG views, found {sorted(actual)}')
            result['modules'].append({'race_gender': race.name, 'module': module})
            for view in wanted:
                relative = Path(race.name) / module / (view + '.png')
                result['images'][relative.as_posix()] = check_image(folder / relative, base / (view + '.png'))
    return result


def stage_status(root: Path) -> dict:
    return {name: sorted(p.name for p in (root / name).iterdir() if p.is_dir())
            if (root / name).is_dir() else [] for name in STATES}


def claim(root: Path, set_name: str) -> dict:
    if not clean_name(set_name):
        raise ValueError('Set name must be a single safe folder name')
    if root == Path(__file__).resolve().parents[2]:
        service = subprocess.run(['systemctl', '--user', 'is-active', 'pixal3d-armor-queue.service'],
                                 capture_output=True, text=True, check=False)
        if service.stdout.strip() == 'active':
            raise ValueError('Retired Pixal3D watcher is active; stop it before claiming a Tripo set')
    source = root / 'approved' / set_name
    details = validate_set(root, source, complete=False)
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
            source_base = root / 'base_views' / race / filename
            if source_base.is_symlink() or not source_base.is_file():
                raise ValueError(f'Missing or linked base view: {source_base}')
            shutil.copy2(source_base, frozen / filename)
            base_hashes[f'{race}/{filename}'] = digest(frozen / filename)
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
              'modules': details['modules'], 'conversion': 'pending',
              'tripo_upload_started': False}
    write_json(source / '.tripo-job.json', record)
    os.rename(source, destination)
    return {'job': job_name, 'path': str(destination), 'modules': details['modules']}


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
    publish = sub.add_parser('ready', help='Validate all four views and move to multiview_ready/')
    publish.add_argument('job_name')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == 'status': result = stage_status(root)
        elif args.command == 'check': result = validate_set(root, args.folder.resolve(), complete=args.complete)
        elif args.command == 'claim': result = claim(root, args.set_name)
        else: result = ready(root, args.job_name)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f'WORKFLOW_ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
