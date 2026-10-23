#!/usr/bin/env python3
"""Local, loopback-only concept review and model preview server."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
from urllib.parse import unquote, urlsplit
import webbrowser

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parents[1]
MODULES = ('body_armor', 'right_gauntlet_boot', 'outfit')
VIEWS = ('front', 'front_three_quarter', 'back', 'back_three_quarter')
STAGES = ('processing_views', 'multiview_ready', 'processing', 'failed')
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')
FLAT = re.compile(r'^(\d+_[a-z0-9][a-z0-9_-]*)__([a-z_]+)\.png$')
LOCK = threading.RLock()


def safe_name(value: str) -> str:
    if not isinstance(value, str) or not NAME.fullmatch(value) or value in ('.', '..'):
        raise ValueError('Invalid ID component')
    return value


def direct(root: Path, folder: str, name: str) -> Path:
    name = safe_name(name)
    parent = root / folder
    if parent.is_symlink() or not parent.resolve(strict=False).is_relative_to(root.resolve()):
        raise ValueError('Linked folder is not allowed')
    path = parent / name
    if path.is_symlink() or path.resolve(strict=False).parent != parent.resolve():
        raise ValueError('Path escapes expected folder')
    return path


def file_url(root: Path, path: Path | None) -> str | None:
    if not path or not path.is_file() or path.is_symlink():
        return None
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None
    from urllib.parse import quote
    return '/files/' + quote(rel, safe='/')


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def summary(meta: dict) -> dict:
    attempts = meta.get('attempt_count', meta.get('attempts', 1))
    if isinstance(attempts, list):
        attempts = len(attempts)
    return {key: meta.get(key) for key in ('brief', 'colours', 'colors', 'palette', 'swatches', 'notes', 'design') if key in meta} | {'attempts': attempts}


def set_card(root: Path, folder: Path, stage: str) -> dict | None:
    races = []
    for race in sorted(folder.iterdir()) if folder.is_dir() else []:
        if not race.is_dir() or race.is_symlink() or not NAME.fullmatch(race.name):
            continue
        images, info = {}, {}
        for module in MODULES:
            image = file_url(root, race / f'{module}_front.png')
            if image:
                images[module] = image
                info[module] = summary(read_json(race / f'{module}_prompts.json'))
        if images:
            base = file_url(root, root / 'base_views' / race.name / 'front.png')
            races.append({'name': race.name, 'images': images, 'info': info, 'base': base})
    if not races:
        return None
    return {'id': f'{stage}:{folder.name}', 'name': folder.name, 'kind': 'set', 'races': races}


def legacy_entries(root: Path, batch: Path) -> list[dict]:
    groups = {}
    for path in sorted(batch.iterdir()):
        match = FLAT.fullmatch(path.name)
        if match and path.is_file() and not path.is_symlink() and match[2] in MODULES:
            groups.setdefault(match[1], {})[match[2]] = file_url(root, path)
    entries = []
    for key, images in groups.items():
        meta = read_json(batch / f'{key}.json')
        entries.append({'id': f'legacy:{batch.name}:{key}', 'name': key, 'kind': 'legacy',
                        'batch': batch.name, 'races': [{'name': meta.get('race_gender') or 'unknown',
                        'images': images, 'info': {'record': summary(meta)}}],
                        'incomplete': set(images) not in ({'outfit'}, {'body_armor', 'right_gauntlet_boot'})})
    return entries


def concepts(root: Path, stage: str) -> list[dict]:
    parent = root / stage
    if not parent.is_dir():
        return []
    result = []
    for folder in sorted(parent.iterdir()):
        if not folder.is_dir() or folder.is_symlink() or not NAME.fullmatch(folder.name):
            continue
        card = set_card(root, folder, stage)
        if card:
            result.append(card)
        elif stage == 'concepts':
            result.extend(legacy_entries(root, folder))
    return result


def trash_cards(root: Path) -> list[dict]:
    parent = root / 'trash' / 'concepts'
    if not parent.is_dir():
        return []
    result = []
    for folder in sorted(parent.iterdir()):
        if not folder.is_dir() or folder.is_symlink():
            continue
        origin = read_json(folder / '.preview-origin.json')
        if origin.get('kind') == 'set':
            card = set_card(root, folder, 'trash')
            if card:
                card['origin'] = origin.get('src')
                result.append(card)
        else:
            for sidecar in sorted(folder.glob('*.preview-origin.json')):
                key = sidecar.name.removesuffix('.preview-origin.json')
                if not NAME.fullmatch(key):
                    continue
                meta = read_json(sidecar)
                images = {m: file_url(root, folder / f'{key}__{m}.png') for m in MODULES}
                images = {m: u for m, u in images.items() if u}
                if images:
                    result.append({'id': f'trashlegacy:{folder.name}:{key}', 'name': key,
                                   'kind': 'legacy', 'batch': folder.name, 'origin': meta.get('src'),
                                   'races': [{'name': meta.get('race_gender') or 'unknown', 'images': images,
                                              'info': {'record': summary(read_json(folder / f'{key}.json'))}}]})
    return result


def jobs(root: Path, stages: tuple[str, ...] = STAGES) -> list[dict]:
    result = []
    for stage in stages:
        parent = root / stage
        if not parent.is_dir():
            continue
        for folder in sorted(parent.iterdir()):
            if not folder.is_dir() or folder.is_symlink():
                continue
            record = read_json(folder / '.tripo-job.json')
            races = []
            for race in sorted(folder.iterdir()):
                if not race.is_dir() or race.is_symlink() or race.name.startswith('.'):
                    continue
                for module in MODULES:
                    sub = race / module
                    images = {view: file_url(root, sub / f'{view}.png') for view in VIEWS}
                    images = {view: url for view, url in images.items() if url}
                    if images:
                        races.append({'name': race.name, 'module': module, 'images': images})
            result.append({'id': f'{stage}:{folder.name}', 'name': folder.name, 'stage': stage,
                           'conversion': record.get('conversion', 'unknown'), 'state': record.get('state'),
                           'module_conversion': record.get('module_conversion', {}), 'views': races})
    return result


def model_cards(root: Path, pipeline: list[dict]) -> list[dict]:
    parent = root / 'models'
    if not parent.is_dir():
        return []
    job_fronts = {}
    for job in pipeline + jobs(root, ('completed',)):
        set_name = job['name'].split('--')[0]
        for entry in job['views']:
            job_fronts[(set_name, entry['name'], entry['module'])] = entry['images'].get('front')
    result = []
    for set_dir in sorted(parent.iterdir()):
        if not set_dir.is_dir() or set_dir.is_symlink():
            continue
        model_entries = []
        for job_dir in sorted(set_dir.iterdir()):
            if not job_dir.is_dir() or job_dir.is_symlink():
                continue
            for race_dir in sorted(job_dir.iterdir()):
                if not race_dir.is_dir() or race_dir.is_symlink():
                    continue
                for module_dir in sorted(race_dir.iterdir()):
                    if not module_dir.is_dir() or module_dir.is_symlink():
                        continue
                    reduced = module_dir / 'reduced'
                    source = module_dir / 'source.glb'
                    if not source.is_file():
                        source = next(iter(sorted(module_dir.glob('*.glb'))), source)
                    variants = {}
                    for level in ('0p4', '0p2', '0p1'):
                        variants[level] = {key: item for key, path in {
                            'wearable': reduced / f'lod_error_{level}__wearable.glb',
                            'full': reduced / f'lod_error_{level}.glb',
                            'highpoly': source,
                        }.items() if (item := file_url(root, path))}
                    pieces = {}
                    for level in ('0p4', '0p2', '0p1'):
                        for base in (module_dir / 'segment_first' / 'pieces', module_dir / 'pieces'):
                            files = sorted((base / f'lod_{level}').glob('*.glb'))
                            urls = [u for p in files if (u := file_url(root, p))]
                            if urls:
                                pieces[level] = urls
                                break
                    if not any(variants.values()) and not pieces:
                        continue
                    render = next((file_url(root, p) for p in (module_dir / 'pieces' / 'pieces_render.png',
                              module_dir / 'segment_first' / 'pieces' / 'pieces_render.png',
                              module_dir / 'studio_result.png', module_dir / 'after_submit.png')
                              if file_url(root, p)), None)
                    model_entries.append({'race': race_dir.name, 'module': module_dir.name,
                                          'job': job_dir.name, 'variants': variants, 'pieces': pieces,
                                          'thumbnail': render or job_fronts.get((set_dir.name, race_dir.name, module_dir.name))})
        if model_entries:
            result.append({'id': f'model:{set_dir.name}', 'name': set_dir.name,
                           'thumbnail': next((e['thumbnail'] for e in model_entries if e['thumbnail']), None),
                           'entries': model_entries})
    return result


def state(root: Path) -> dict:
    pipeline = jobs(root)
    return {'concepts': concepts(root, 'concepts'), 'approved': concepts(root, 'approved'),
            'pipeline': pipeline, 'models': model_cards(root, pipeline), 'trash': trash_cards(root)}


def unique(path: Path) -> Path:
    if not path.exists():
        return path
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    return path.with_name(path.name + '_' + stamp)


def move_files(paths: list[Path], destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if any((destination / p.name).exists() for p in paths):
        raise FileExistsError('Destination file already exists')
    for path in paths:
        shutil.move(str(path), str(destination / path.name))


def flat_files(batch: Path, key: str) -> list[Path]:
    safe_name(key)
    files = sorted(p for p in batch.iterdir() if p.is_file() and not p.is_symlink()
                   and (p.name == f'{key}.json' or any(p.name == f'{key}__{m}.png' for m in MODULES)))
    if not any(p.suffix == '.png' for p in files):
        raise FileNotFoundError('Flat concept images not found')
    return files


def action(root: Path, verb: str, item_id: str) -> dict:
    parts = item_id.split(':')
    if not all(NAME.fullmatch(p) for p in parts):
        raise ValueError('Invalid ID')
    src = dst = None
    result = {}
    with LOCK:
        try:
            if verb == 'approve' and len(parts) == 2 and parts[0] == 'concepts':
                src, dst = direct(root, 'concepts', parts[1]), direct(root, 'approved', parts[1])
                if not src.is_dir(): raise FileNotFoundError(src)
                if dst.exists(): raise FileExistsError(dst)
                if not set_card(root, src, 'concepts'): raise ValueError('No per-set fronts found')
                dst.parent.mkdir(exist_ok=True)
                src.rename(dst)
                # Validation happens after the approved move. Keep the moved set even when it fails.
                command = [sys.executable, str(root / 'tools' / 'tripo_pipeline' / 'workflow.py'),
                           '--root', str(root), 'check', str(dst)]
                checked = subprocess.run(command, capture_output=True, text=True, check=False)
                if checked.returncode:
                    result['warning'] = checked.stderr.strip() or checked.stdout.strip() or 'Validation failed'
                else:
                    result['check'] = json.loads(checked.stdout)
            elif verb == 'approve' and len(parts) == 3 and parts[0] == 'legacy':
                batch, key = direct(root, 'concepts', parts[1]), safe_name(parts[2])
                src = batch / key
                files = flat_files(batch, key)
                if not re.fullmatch(r'\d+_[a-z0-9][a-z0-9_-]*', key): raise ValueError('Invalid flat entry')
                command = [sys.executable, str(root / 'tools' / 'tripo_pipeline' / 'workflow.py'),
                           '--root', str(root), 'promote', str(batch), key.split('_', 1)[0], '--approve']
                promoted_run = subprocess.run(command, capture_output=True, text=True, check=False)
                if promoted_run.returncode:
                    raise ValueError(promoted_run.stderr.strip() or promoted_run.stdout.strip() or 'Promote failed')
                promoted = json.loads(promoted_run.stdout)
                result['promoted'] = promoted
                dst = unique(root / 'trash' / 'concepts' / batch.name)
                move_files(files, dst)
                (dst / f'{key}.preview-origin.json').write_text(json.dumps({'kind': 'legacy', 'src': f'concepts/{batch.name}', 'approved_set': promoted['set']}))
                result['undo_id'] = f'trashlegacy:{dst.name}:{key}'
            elif verb == 'reject' and len(parts) == 2 and parts[0] == 'concepts':
                src = direct(root, 'concepts', parts[1])
                if not src.is_dir() or not set_card(root, src, 'concepts'): raise FileNotFoundError(src)
                dst = unique(root / 'trash' / 'concepts' / src.name)
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dst)
                (dst / '.preview-origin.json').write_text(json.dumps({'kind': 'set', 'src': f'concepts/{src.name}'}))
                result['undo_id'] = f'trash:{dst.name}'
            elif verb == 'reject' and len(parts) == 3 and parts[0] == 'legacy':
                batch, key = direct(root, 'concepts', parts[1]), safe_name(parts[2])
                src = batch / key
                files = flat_files(batch, key)
                dst = root / 'trash' / 'concepts' / batch.name
                if any((dst / p.name).exists() for p in files) or (dst / f'{key}.preview-origin.json').exists():
                    dst = unique(dst)
                move_files(files, dst)
                (dst / f'{key}.preview-origin.json').write_text(json.dumps({'kind': 'legacy', 'src': f'concepts/{batch.name}'}))
                result['undo_id'] = f'trashlegacy:{dst.name}:{key}'
            elif verb == 'restore' and len(parts) == 2 and parts[0] == 'trash':
                src = direct(root, 'trash/concepts', parts[1])
                origin = read_json(src / '.preview-origin.json')
                if origin.get('kind') != 'set': raise ValueError('Missing origin sidecar')
                name = origin['src'].removeprefix('concepts/')
                if origin['src'] != f'concepts/{name}': raise ValueError('Invalid origin')
                dst = direct(root, 'concepts', name)
                if dst.exists(): raise FileExistsError(dst)
                src.rename(dst)
            elif verb == 'restore' and len(parts) == 3 and parts[0] == 'trashlegacy':
                batch, key = direct(root, 'trash/concepts', parts[1]), safe_name(parts[2])
                sidecar = batch / f'{key}.preview-origin.json'
                origin = read_json(sidecar)
                if origin.get('kind') != 'legacy': raise ValueError('Missing origin sidecar')
                src = batch / key
                dest_name = origin['src'].removeprefix('concepts/')
                if origin['src'] != f'concepts/{dest_name}': raise ValueError('Invalid origin')
                dst = direct(root, 'concepts', dest_name)
                files = flat_files(batch, key)
                if any((dst / p.name).exists() for p in files): raise FileExistsError('Restore destination exists')
                move_files(files, dst)
                # Keep the origin sidecar as an audit record; no files are deleted.
            elif verb == 'unapprove' and len(parts) == 2 and parts[0] == 'approved':
                src, dst = direct(root, 'approved', parts[1]), direct(root, 'concepts', parts[1])
                if not src.is_dir() or not set_card(root, src, 'approved'): raise FileNotFoundError(src)
                if (src / '.tripo-job.json').exists(): raise ValueError('Set has already been claimed')
                if dst.exists(): raise FileExistsError(dst)
                dst.parent.mkdir(exist_ok=True)
                src.rename(dst)
            else:
                raise ValueError('Action and ID do not match')
            result |= {'ok': True, 'src': str(src.relative_to(root)) if src else None,
                       'dst': str(dst.relative_to(root)) if dst else None}
            log_action(root, verb, result['src'], result['dst'], 'ok' if not result.get('warning') else result['warning'])
            return result
        except Exception as exc:
            log_action(root, verb, str(src.relative_to(root)) if src else None,
                       str(dst.relative_to(root)) if dst else None, f'error: {exc}')
            raise


def log_action(root: Path, verb: str, src: str | None, dst: str | None, result: str) -> None:
    path = root / 'tools' / 'preview' / 'actions.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as out:
        out.write(json.dumps({'time': datetime.now(timezone.utc).isoformat(), 'action': verb,
                              'src': src, 'dst': dst, 'result': result}) + '\n')


class Handler(BaseHTTPRequestHandler):
    root = DEFAULT_ROOT

    def send_json(self, status: int, data: dict) -> None:
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == '/api/state':
            self.send_json(200, state(self.root))
            return
        if path in ('/', '/index.html', '/app.js', '/style.css'):
            file = HERE / ('index.html' if path == '/' else path.lstrip('/'))
        elif path.startswith('/files/'):
            relative = unquote(path.removeprefix('/files/'))
            file = self.root / relative
            if file.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp', '.glb'):
                self.send_error(403); return
            try:
                file.resolve(strict=True).relative_to(self.root.resolve(strict=True))
            except (ValueError, OSError):
                self.send_error(403); return
        else:
            self.send_error(404); return
        if not file.is_file():
            self.send_error(404); return
        raw = file.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        verb = urlsplit(self.path).path.removeprefix('/api/')
        if verb not in ('approve', 'reject', 'restore', 'unapprove') or self.path.split('?')[0] != '/api/' + verb:
            self.send_error(404); return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size < 1 or size > 4096: raise ValueError('Invalid request size')
            data = json.loads(self.rfile.read(size))
            result = action(self.root, verb, data['id'])
            self.send_json(200, result)
        except (ValueError, KeyError, OSError, RuntimeError) as exc:
            self.send_json(400, {'ok': False, 'error': str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    url = f'http://127.0.0.1:{server.server_port}/'
    print(url, flush=True)
    if args.open: webbrowser.open(url)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == '__main__':
    main()
