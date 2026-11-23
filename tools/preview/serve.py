#!/usr/bin/env python3
"""Local, loopback-only concept review and model preview server."""
from __future__ import annotations

import argparse
import gzip
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from concurrent.futures import ThreadPoolExecutor
import mimetypes
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit
import webbrowser

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parents[1]
MODULES = ('body_armor', 'right_gauntlet_boot', 'outfit')
VIEWS = ('front', 'front_three_quarter', 'back', 'back_three_quarter')
STAGES = ('processing_views', 'multiview_ready', 'processing', 'failed')
HELD = 'held_conversions'  # staged jobs that must not be converted until the user releases the hold (AGENTS.md); shown in the Held tab, not in Pipeline
ARCHIVE = 'archive/concepts'  # sets kept for reference, out of the review queue
MODEL_APPROVAL = 'model_approval.json'  # models/<set>/model_approval.json {"approved": true, ...}: the set was approved in the Models tab
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')
FLAT = re.compile(r'^(\d+_[a-z0-9][a-z0-9_-]*)__([a-z_]+)\.png$')
LOCK = threading.RLock()


def _load_monsters():
    """monsters.py (the Monsters tab) loaded by path: this file is itself loaded by path in the tests."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('preview_monsters', HERE / 'monsters.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


monsters = _load_monsters()


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
    """`/files/` URL of a regular, non-symlink file under `root`, or None. One lstat; a path outside `root` is resolved the slow way."""
    if not path:
        return None
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            return None
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


# Result cache for the slow scans (model modules, concept sets, pipeline jobs). The filesystem is a slow FUSE mount where every syscall costs
# ~0.3 ms, so a result is kept while every directory it listed (and every file whose mtime or content it used) still has the mtime it had
# when the result was built: adding, removing or replacing a file changes a listed directory's mtime, and rewriting a used file in place
# changes its own. Results built from anything touched within RACY seconds are not kept (a write in the same timestamp tick would be
# invisible), so folders that workers are writing into are simply rescanned.
CACHE: dict[tuple, tuple[dict[str, int | None], object]] = {}
SCANS = [0]  # how many results were built (not served from the cache); read by the tests
RACY = 3.0
POOL = ThreadPoolExecutor(max_workers=8)


def _mtime(path: str) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _listing(path: str, deps: dict | None = None) -> dict[str, bool] | None:
    """{name: is_dir} of the non-symlink entries of a directory (None when it is not one); records its mtime in `deps` first."""
    if deps is not None:
        deps[path] = _mtime(path)
        if deps[path] is None:
            return None
    try:
        with os.scandir(path) as entries:
            return {e.name: e.is_dir(follow_symlinks=False) for e in entries if not e.is_symlink()}
    except OSError:
        return None


def _stamp(path: str, deps: dict) -> int | None:
    deps[path] = _mtime(path)
    return deps[path]


def _not_older(path: str, source: str, deps: dict | None) -> bool:
    """True when `path` is at least as new as `source` (the file it was derived from); both mtimes are recorded in `deps` when given."""
    new, old = (_stamp(path, deps), _stamp(source, deps)) if deps is not None else (_mtime(path), _mtime(source))
    return new is not None and old is not None and new >= old


def _memo(kind: str, root: Path, path: object, build) -> object:
    """`build(deps)` for the folder `path`, served from CACHE while the mtimes it recorded in `deps` are unchanged."""
    key = (kind, str(root), str(path))
    cached = CACHE.get(key)
    if cached and all(_mtime(dep) == stamp for dep, stamp in cached[0].items()):
        return cached[1]
    started = time.time()
    deps: dict[str, int | None] = {}
    SCANS[0] += 1
    result = build(deps)
    if all(stamp is None or stamp / 1e9 < started - RACY for stamp in deps.values()):
        CACHE[key] = (deps, result)
    else:
        CACHE.pop(key, None)
    return result


def _prune(kind: str, root: Path, seen: set[str]) -> None:
    for key in [k for k in CACHE if k[0] == kind and k[1] == str(root) and k[2] not in seen]:
        CACHE.pop(key, None)


def rel_url(root: Path, path: Path) -> str:
    """`/files/` URL of a path under `root` that the caller already knows is a regular file (no filesystem access)."""
    return '/files/' + quote(path.relative_to(root).as_posix(), safe='/')


def set_card(root: Path, folder: Path, stage: str, bases: dict | None = None) -> dict | None:
    bases = {} if bases is None else bases  # race -> base front URL, shared by the cards of one request

    def build(deps: dict) -> list[dict]:
        races = []
        for race in sorted(n for n, is_dir in (_listing(str(folder), deps) or {}).items() if is_dir):
            if not NAME.fullmatch(race):
                continue
            listing = _listing(f'{folder}/{race}', deps) or {}
            images, info = {}, {}
            for module in MODULES:
                if listing.get(f'{module}_front.png') is False:
                    images[module] = rel_url(root, folder / race / f'{module}_front.png')
                    record = folder / race / f'{module}_prompts.json'
                    _stamp(str(record), deps)
                    info[module] = summary(read_json(record))
            if images:
                races.append({'name': race, 'images': images, 'info': info})
        return races

    races = []
    for race in _memo('set', root, folder, build):
        if race['name'] not in bases:
            bases[race['name']] = file_url(root, root / 'base_views' / race['name'] / 'front.png')
        races.append(race | {'base': bases[race['name']]})
    if not races:
        return None
    return {'id': f'{stage}:{folder.name}', 'name': folder.name, 'kind': 'set', 'races': races}


def legacy_entries(root: Path, batch: Path, prefix: str = 'legacy') -> list[dict]:
    groups = {}
    for path in sorted(batch.iterdir()):
        match = FLAT.fullmatch(path.name)
        if match and path.is_file() and not path.is_symlink() and match[2] in MODULES:
            groups.setdefault(match[1], {})[match[2]] = file_url(root, path)
    entries = []
    for key, images in groups.items():
        meta = read_json(batch / f'{key}.json')
        entries.append({'id': f'{prefix}:{batch.name}:{key}', 'name': key, 'kind': 'legacy',
                        'batch': batch.name, 'races': [{'name': meta.get('race_gender') or 'unknown',
                        'images': images, 'info': {'record': summary(meta)}}],
                        'incomplete': set(images) not in ({'outfit'}, {'body_armor', 'right_gauntlet_boot'})})
    return entries


def concepts(root: Path, stage: str) -> list[dict]:
    parent = root / (ARCHIVE if stage == 'archive' else stage)
    result, bases = [], {}
    for name in sorted(n for n, is_dir in (_listing(str(parent), {}) or {}).items() if is_dir and NAME.fullmatch(n)):
        folder = parent / name
        card = set_card(root, folder, stage, bases)
        if card:
            result.append(card)
        elif stage == 'concepts':
            result.extend(legacy_entries(root, folder))
        elif stage == 'archive':
            result.extend(legacy_entries(root, folder, 'archlegacy'))
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


def _job_views(folder: Path, deps: dict) -> tuple[dict, list[dict]]:
    stage, name = folder.parent.name, folder.name
    top = _listing(str(folder), deps) or {}
    record = {}
    if '.tripo-job.json' in top:
        _stamp(f'{folder}/.tripo-job.json', deps)  # rewritten in place by the converter
        record = read_json(folder / '.tripo-job.json')
    races = []
    for race in sorted(n for n, is_dir in top.items() if is_dir and not n.startswith('.')):
        subs = _listing(f'{folder}/{race}', deps) or {}
        for module in MODULES:
            if not subs.get(module):
                continue
            sub = _listing(f'{folder}/{race}/{module}', deps) or {}
            images = {view: f'/files/{quote(f"{stage}/{name}/{race}/{module}/{view}.png", safe="/")}'
                      for view in VIEWS if sub.get(f'{view}.png') is False}
            if images:
                races.append({'name': race, 'module': module, 'images': images})
    return record, races


def _base_races(root: Path) -> list[str]:
    """Race folders of `base_views/` that carry a palette.json (the active bases)."""
    def build(deps: dict) -> list[str]:
        parent = root / 'base_views'
        return sorted(race for race, is_dir in (_listing(str(parent), deps) or {}).items()
                      if is_dir and NAME.fullmatch(race) and 'palette.json' in (_listing(f'{parent}/{race}', deps) or {}))
    return _memo('races', root, root / 'base_views', build)


def _job_set(name: str, races: list[str]) -> str:
    """Set id of a job folder name; a set id ending in `_<race>` (e.g. `x_gnome_female`) belongs to the base set `x`."""
    set_id = name.split('--')[0]
    for race in races:
        if set_id.endswith('_' + race) and len(set_id) > len(race) + 1:
            return set_id[:-len(race) - 1]
    return set_id


def _is_converted(job: dict) -> bool:
    return job['stage'] == 'processing' and str(job['state'] or '').startswith('converted')


def _waiting_for(root: Path, result: list[dict]) -> None:
    """Adds `waiting_for` to every converted job of `processing/`: what keeps its set from being complete (and so from moving to `completed/`, i.e. the
    Models tab): races (gnomes included, user 2026-11-14) no converted job of the set covers, and other jobs of the set that are still being generated or converted."""
    races = _base_races(root)
    required = list(races)
    covered: dict[str, set[str]] = {}
    pending: dict[str, list[str]] = {}
    for job in result + jobs(root, ('completed',)):
        if job['stage'] == 'failed':
            continue
        set_id = _job_set(job['name'], races)
        if _is_converted(job) or job['stage'] == 'completed':
            covered.setdefault(set_id, set()).update(view['name'] for view in job['views'])
        else:
            pending.setdefault(set_id, []).append(f'{job["name"]} ({"converting" if job["stage"] == "processing" else "generating views"})')
    for job in result:
        if _is_converted(job):
            set_id = _job_set(job['name'], races)
            job['waiting_for'] = [f'{race} (not converted)' for race in required if race not in covered.get(set_id, ())] + pending.get(set_id, [])


def jobs(root: Path, stages: tuple[str, ...] = STAGES) -> list[dict]:
    result = []
    for stage in stages:
        parent = root / stage
        for name in sorted(n for n, is_dir in (_listing(str(parent)) or {}).items() if is_dir):
            record, races = _memo('job', root, parent / name, lambda deps: _job_views(parent / name, deps))
            result.append({'id': f'{stage}:{name}', 'name': name, 'stage': stage,
                           'conversion': record.get('conversion', 'unknown'), 'state': record.get('state'),
                           'module_conversion': record.get('module_conversion', {}), 'views': races, 'waiting_for': []})
    if any(_is_converted(job) for job in result):
        _waiting_for(root, result)
    return result


def fresh_file(path: Path, source: Path) -> Path | None:
    """`path` when it is a file at least as new as `source` (the file it was derived from), else None."""
    try:
        return path if path.is_file() and path.stat().st_mtime >= source.stat().st_mtime else None
    except OSError:
        return None


def _module_scan(root: Path, module: str, race: str, name: str, job: str, deps: dict) -> tuple[dict, str | None] | None:
    rel = os.path.relpath(module, root)
    url = lambda *parts: '/files/' + quote('/'.join((rel.replace(os.sep, '/'), *parts)), safe='/')
    top = _listing(module, deps) or {}
    reduced = _listing(f'{module}/reduced', deps) if top.get('reduced') else None
    reduced = reduced or {}
    source = 'source.glb' if top.get('source.glb') is False else next(
        (n for n in sorted(top) if n.endswith('.glb') and top[n] is False), None)
    variants = {}
    for level in ('0p4', '0p2', '0p1'):
        wearable, full = f'lod_error_{level}__wearable.glb', f'lod_error_{level}.glb'
        found = {}
        if reduced.get(wearable) is False:
            found['wearable'] = url('reduced', wearable)
        if reduced.get(full) is False:
            found['full'] = url('reduced', full)
        if source:
            found['highpoly'] = url(source)
        for key, suffix in FIT_METHODS.items():
            fitted = f'lod_error_{level}__wearable{suffix}.glb'
            origin = f'lod_error_{level}__wearable{FIT_ORIGIN.get(key, "")}.glb'       # the file a twin was made from: it must not be newer
            if reduced.get(fitted) is False and reduced.get(wearable) is False and reduced.get(origin) is False:
                if _not_older(f'{module}/reduced/{fitted}', f'{module}/reduced/{wearable}', deps) and _not_older(f'{module}/reduced/{fitted}', f'{module}/reduced/{origin}', deps):
                    found[key] = url('reduced', fitted)
        for key, (suffix, origins) in BODY_OVERLAYS.items():       # the Tripo body overlay (align_body.py): hidden when older than what it was made from
            overlay = f'lod_error_{level}{suffix}.glb'
            if reduced.get(overlay) is False and all(reduced.get(f'lod_error_{level}{o}.glb') is False and _not_older(f'{module}/reduced/{overlay}', f'{module}/reduced/lod_error_{level}{o}.glb', deps) for o in origins):
                found[key] = url('reduced', overlay)
        variants[level] = {key: found[key] for key in ('wearable', 'full', 'highpoly', *FIT_METHODS, *BODY_OVERLAYS) if key in found}
    bases = []
    for sub in ('segment_first/pieces', 'pieces'):
        folder = f'{module}/{sub}'
        listing = _listing(folder, deps) if sub == 'pieces' or top.get('segment_first') else None
        bases.append((sub, folder, listing))
    pieces, pieces_fitted = {}, {key: {} for key in FIT_METHODS}
    for level in ('0p4', '0p2', '0p1'):
        for sub, folder, listing in bases:
            if not listing or not listing.get(f'lod_{level}'):
                continue
            names = sorted(n for n, is_dir in (_listing(f'{folder}/lod_{level}', deps) or {}).items() if not is_dir and n.endswith('.glb'))
            if names:
                pieces[level] = [url(sub, f'lod_{level}', n) for n in names]
                for key, suffix in FIT_METHODS.items():
                    twin = f'lod_{level}{suffix}'
                    twins = _listing(f'{folder}/{twin}', deps) if listing.get(twin) else None
                    if twins and all(twins.get(n) is False for n in names) and not (FIT_ORIGIN.get(key) and not listing.get(f'lod_{level}{FIT_ORIGIN[key]}')):
                        # Only the first twin pair is watched for in-place rewrites (a re-split rewrites every piece, body_fit.py replaces files
                        # atomically so the folder mtime moves); stamping every piece would cost two stats per piece on every request.
                        origin = f'lod_{level}{FIT_ORIGIN.get(key, "")}'
                        if all([_not_older(f'{folder}/{twin}/{n}', f'{folder}/lod_{level}/{n}', deps if i == 0 else None)
                                and (origin == f'lod_{level}' or _not_older(f'{folder}/{twin}/{n}', f'{folder}/{origin}/{n}', deps if i == 0 else None))
                                for i, n in enumerate(names)]):
                            pieces_fitted[key][level] = [url(sub, twin, n) for n in names]  # body_fit.py output: all or nothing
                break
    if not any(variants.values()) and not pieces:
        return None
    render = None
    for sub, listing, fname in (('pieces', dict(bases[1][2] or {}), 'pieces_render.png'), ('segment_first/pieces', dict(bases[0][2] or {}), 'pieces_render.png'),
                                ('', top, 'studio_result.png'), ('', top, 'after_submit.png')):
        if listing.get(fname) is False:
            render = url(*(sub.split('/') if sub else []), fname)
            break
    lod = None
    if top.get('chosen_lod.json') is False:
        _stamp(f'{module}/{LOD_CHOICE}', deps)
        lod = read_json(Path(module) / LOD_CHOICE).get('lod')
    chosen = f'lod_error_{lod if lod in LOD_LEVELS else "0p4"}'
    model, preview = f'{chosen}.glb', None
    if reduced.get(model) is False:
        thumb = f'{chosen}.thumb.png'
        if reduced.get(thumb) is False:
            if _not_older(f'{module}/reduced/{thumb}', f'{module}/reduced/{model}', deps):
                preview = url('reduced', thumb) + f'?v={deps[f"{module}/reduced/{thumb}"] // 10**9}'
    entry = {'race': race, 'module': name, 'preview_model': url('reduced', model) if reduced.get(model) is False else None,
             'preview': preview, 'job': job, 'variants': variants, 'pieces': pieces, 'pieces_fitted': pieces_fitted['fitted'],
             'pieces_fitted_vertex': pieces_fitted['fitted_vertex'], 'pieces_aligned': pieces_fitted['aligned'],
             'pieces_aligned_fitted': pieces_fitted['aligned_fitted'], 'lod': lod}
    return entry, render


def _cached_module(root: Path, module: Path, race: str, name: str, job: str) -> tuple[dict, str | None] | None:
    return _memo('module', root, module, lambda deps: _module_scan(root, str(module), race, name, job, deps))


def _subdirs(path: str) -> list[str]:
    try:
        with os.scandir(path) as entries:
            return sorted(e.name for e in entries if e.is_dir(follow_symlinks=False))
    except OSError:
        return []


def completed_jobs(root: Path, set_name: str) -> list[str]:
    """Job folders `models/<set>/<job>` whose job folder `completed/<set>--<job>` exists (the set was finished by finish_sets.py)."""
    done = {n for n, is_dir in (_listing(str(root / 'completed')) or {}).items() if is_dir}
    return sorted(n for n, is_dir in (_listing(str(direct(root, 'models', set_name))) or {}).items() if is_dir and f'{set_name}--{n}' in done)


def model_approved(marker: Path) -> bool:
    return read_json(marker).get('approved') is True


def _converted_at(module: Path) -> float:
    for path in (module / 'reduced' / 'manifest.json', module):
        try:
            return path.stat().st_mtime
        except OSError:
            pass
    return 0.


def newest_jobs(parent: Path, plan: list[tuple]) -> list[tuple]:
    """One (set, job, race, module) per set, race and module: when several completed jobs converted the same race and module
    (a gnome redone on the footless base, 2026-10-27), only the newest conversion (reduced/manifest.json) is shown, the rule
    tools/outfit_studio/core/pipeline_source.latest uses for the same models."""
    newest: dict[tuple, tuple] = {}
    for p in plan:
        at = _converted_at(parent / p[0] / p[1] / p[2] / p[3])
        key = (p[0], p[2], p[3])
        if key not in newest or at > newest[key][0]:
            newest[key] = (at, p[1])
    return [p for p in plan if newest[(p[0], p[2], p[3])][1] == p[1]]


def model_cards(root: Path, pipeline: list[dict]) -> list[dict]:
    """Cards of the Models and Approved models tabs: only the jobs of `models/` that are in `completed/`; `approved` tells the two tabs apart."""
    parent = root / 'models'
    if not parent.is_dir():
        return []
    finished = jobs(root, ('completed',))
    done = {job['name'] for job in finished}
    job_fronts = {}
    for job in pipeline + finished:
        set_name = job['name'].split('--')[0]
        for entry in job['views']:
            job_fronts[(set_name, entry['name'], entry['module'])] = entry['images'].get('front')
    plan, approved = [], {}
    for set_name in _subdirs(str(parent)):
        listing = _listing(f'{parent}/{set_name}') or {}
        jobs_done = sorted(n for n, is_dir in listing.items() if is_dir and f'{set_name}--{n}' in done)
        if jobs_done:
            approved[set_name] = listing.get(MODEL_APPROVAL) is False and model_approved(parent / set_name / MODEL_APPROVAL)
        for job_name in jobs_done:
            for race_name in _subdirs(f'{parent}/{set_name}/{job_name}'):
                for module_name in _subdirs(f'{parent}/{set_name}/{job_name}/{race_name}'):
                    plan.append((set_name, job_name, race_name, module_name))
    plan = newest_jobs(parent, plan)
    scanned = list(POOL.map(lambda p: _cached_module(root, parent / p[0] / p[1] / p[2] / p[3], p[2], p[3], p[1]), plan))
    _prune('module', root, {str(parent / p[0] / p[1] / p[2] / p[3]) for p in plan})
    by_set: dict[str, list[dict]] = {}
    for (set_name, _job, race_name, module_name), result in zip(plan, scanned):
        if result:
            entry, render = result
            by_set.setdefault(set_name, []).append(
                entry | {'thumbnail': render or job_fronts.get((set_name, race_name, module_name))})
    result = []
    for set_name in sorted(by_set):
        model_entries = by_set[set_name]
        # Group by module (all body armour, then all gauntlet/boots) so the viewer browses one kind at a time.
        order = {m: i for i, m in enumerate(MODULES)}
        model_entries.sort(key=lambda e: (order.get(e['module'], len(MODULES)), e['module'], e['race'], e['job']))
        # Card previews: the first entry of each module (body armour, then gauntlet/boots).
        firsts = list({e['module']: e for e in reversed(model_entries)}.values())[::-1]
        # The same per race for the Models tab's race dropdown: {race: {previews, thumbnail}}, races without a model are absent.
        by_race = {}
        for race_name in sorted({e['race'] for e in model_entries}):
            of_race = [e for e in model_entries if e['race'] == race_name]
            race_firsts = list({e['module']: e for e in reversed(of_race)}.values())[::-1]
            by_race[race_name] = {'previews': [{'model': e['preview_model'], 'image': e['preview']} for e in race_firsts if e['preview_model']][:2],
                                  'thumbnail': next((e['thumbnail'] for e in of_race if e['thumbnail']), None)}
        result.append({'id': f'model:{set_name}', 'name': set_name, 'approved': approved[set_name],
                       'previews': [{'model': e['preview_model'], 'image': e['preview']} for e in firsts if e['preview_model']][:2],
                       'thumbnail': next((e['thumbnail'] for e in model_entries if e['thumbnail']), None),
                       'previews_by_race': by_race, 'entries': model_entries})
    return result


REVIEW_LISTS = HERE / 'review.json'   # Review tab: {"lists": [{"name", "title", "modules": [models/<set>/<job>/<race>/<module>], "before": archive dir}]}


def pretty_set(name: str) -> str:
    """A set name cut after its number, for the Review tab's model picker: breastplate_07_hand_me_down_cuirass -> breastplate_07."""
    bits = name.split('_')
    idx = next((i for i, b in enumerate(bits) if b.isdigit()), None)
    return '_'.join(bits[:idx + 1]) if idx is not None else name


def review_cards(root: Path) -> list[dict]:
    """Cards of the Review tab: hand-picked modules (review.json) shown like a model set, each followed by its "before" copy when the list
    names a backup folder (archive/<name>/ holding the same relative paths, e.g. the align outputs saved before a re-run)."""
    lists = (read_json(REVIEW_LISTS) or {}).get('lists') or []
    cards = []
    for spec in lists:
        name = spec.get('name')
        if not isinstance(name, str) or not NAME.fullmatch(name):
            continue
        before = spec.get('before')
        entries = []
        for rel in spec.get('modules') or []:
            parts = Path(rel).parts
            if len(parts) != 5 or parts[0] != 'models' or not all(NAME.fullmatch(x) for x in parts[1:]):
                continue
            module = root.joinpath(*parts)
            result = _cached_module(root, module, parts[3], parts[4], parts[2])
            if not result:
                continue
            entry, render = result
            entry = json.loads(json.dumps(entry)) | {'thumbnail': render}
            entries.append(entry | {'job': pretty_set(parts[1]) + ' · ' + spec.get('after_label', 'now')})
            if isinstance(before, str) and (root / before / rel).is_dir():
                old = json.loads(json.dumps(entry))
                src = '/files/' + quote((before + '/' + rel).replace(os.sep, '/'), safe='/')
                for level, found in old['variants'].items():
                    for key, suffix in (('aligned', '__wearable_aligned'), ('aligned_fitted', '__wearable_aligned_fitted'),
                                        ('red_body_aligned', '__tripo_body_aligned')):
                        fname = 'lod_error_' + level + suffix + '.glb'
                        if (root / before / rel / 'reduced' / fname).is_file():
                            found[key] = src + '/reduced/' + fname
                        else:
                            found.pop(key, None)
                    for key in ('fitted', 'fitted_vertex'):
                        found.pop(key, None)
                old['pieces_aligned'], old['pieces_aligned_fitted'] = {}, {}        # piece folders were not backed up
                old['job'] = pretty_set(parts[1]) + ' · ' + spec.get('before_label', 'before')
                entries.append(old)
        if not entries:
            continue
        firsts = list({e['module']: e for e in reversed(entries)}.values())[::-1]
        by_race = {}
        for race_name in sorted({e['race'] for e in entries}):
            of_race = [e for e in entries if e['race'] == race_name]
            by_race[race_name] = {'previews': [{'model': e['preview_model'], 'image': e['preview']} for e in of_race if e['preview_model']][:2],
                                  'thumbnail': next((e['thumbnail'] for e in of_race if e['thumbnail']), None)}
        cards.append({'id': 'review:' + name, 'name': spec.get('title') or name, 'approved': False, 'kind': 'review',
                      'previews': [{'model': e['preview_model'], 'image': e['preview']} for e in firsts if e['preview_model']][:2],
                      'thumbnail': next((e['thumbnail'] for e in entries if e['thumbnail']), None),
                      'previews_by_race': by_race, 'entries': entries})
    return cards


def held_cards(root: Path) -> list[dict]:
    """Cards of the Held tab (read-only): the jobs of `held_conversions/` grouped by set, one card per set."""
    by_set: dict[str, list[dict]] = {}
    for job in jobs(root, (HELD,)):
        by_set.setdefault(job['name'].split('--')[0], []).append(
            {key: job[key] for key in ('id', 'name', 'stage', 'conversion', 'state', 'views')})
    return [{'id': f'held:{set_name}', 'name': set_name, 'kind': 'held', 'jobs': by_set[set_name]} for set_name in sorted(by_set)]


LOD_LEVELS = ('0p4', '0p2', '0p1')
LOD_CHOICE = 'chosen_lod.json'  # the LOD the user picked in the viewer for the final model
# Variants written beside the originals: body_fit.py --method full | vertex (`_fitted`, `_fitted_vertex`), align_body.py (`_aligned`: the wearable
# deformed so the Tripo body sits on the game base) and body_fit.py --source aligned (`_aligned_fitted`); the same suffixes name the piece folders.
FIT_METHODS = {'fitted': '_fitted', 'fitted_vertex': '_fitted_vertex', 'aligned': '_aligned', 'aligned_fitted': '_aligned_fitted'}
FIT_ORIGIN = {'aligned_fitted': '_aligned'}      # a twin made from another twin: stale when that one is newer
# The preview's "Red body" overlay (align_body.py): the converted model's own base-role faces, as converted and deformed with the wearable's alignment field.
# key -> (file suffix, the files of the level it is made from: '' = the labelled lod_error_<lv>.glb)
BODY_OVERLAYS = {'red_body': ('__tripo_body', ('',)), 'red_body_aligned': ('__tripo_body_aligned', ('', '__wearable_aligned'))}
MARK_CATEGORIES = ('extra', 'misc')
MARK_LIMIT = 32 * 1024 * 1024


def marks_path(root: Path, url: object) -> Path:
    """Sidecar `<stem>.marks.json` beside a model GLB under `models/`, from its `/files/` URL."""
    if not isinstance(url, str) or not url.startswith('/files/models/'):
        raise ValueError('Marks need a model URL')
    model = root / unquote(url.removeprefix('/files/'))
    if model.suffix.lower() != '.glb' or model.is_symlink() or not model.is_file():
        raise ValueError('Model file not found')
    try:
        model.resolve(strict=True).relative_to((root / 'models').resolve(strict=True))
    except (ValueError, OSError):
        raise ValueError('Model escapes models folder') from None
    return model.with_name(model.stem + '.marks.json')


def model_file(root: Path, url: object) -> Path:
    """The validated model GLB under `models/` for its `/files/` URL."""
    return marks_path(root, url).with_name(Path(unquote(str(url))).name)


def clearance_path(root: Path, url: object) -> Path:
    """Sidecar `<stem>.clearance.json` (made by body_fit.py) beside a model GLB under `models/`."""
    model = model_file(root, url)
    return model.with_name(model.stem + '.clearance.json')


def read_clearance(root: Path, url: object) -> dict:
    path = clearance_path(root, url)
    record = read_json(path)
    if not isinstance(record.get('meshes'), list):
        raise ValueError(f'not measured: no {path.name} beside {path.parent.name}/{Path(unquote(str(url))).name}')
    return record | {'url': url}


def archived_module(root: Path, url: object) -> Path | None:
    """For a backup copy under `archive/<name>/models/<set>/<job>/<race>/<module>/...` (the Review tab's "before" entries): the live module
    `models/<set>/<job>/<race>/<module>` it was copied from, so the viewer places it on the body the same way. None for any other URL."""
    if not isinstance(url, str) or not url.startswith('/files/archive/'):
        return None
    rel = Path(unquote(url.removeprefix('/files/')).split('?')[0])
    parts = rel.parts
    if len(parts) < 9 or parts[2] != 'models' or rel.suffix.lower() != '.glb' or not all(NAME.fullmatch(x) for x in (parts[1], *parts[3:7])):
        raise ValueError('Archived model is not inside archive/<name>/models/<set>/<job>/<race>/<module>')
    try:
        (root / rel).resolve(strict=True).relative_to((root / 'archive').resolve(strict=True))
    except (ValueError, OSError):
        raise ValueError('Archived model not found')
    return root.joinpath('models', *parts[3:7])


def module_dir(root: Path, url: object) -> Path:
    """`models/<set>/<job>/<race>/<module>` of a model GLB URL (the GLB may sit in reduced/, a pieces folder, ...)."""
    archived = archived_module(root, url)
    if archived is not None:
        return archived
    parts = model_file(root, url).relative_to(root).parts
    if len(parts) < 6 or not all(NAME.fullmatch(part) for part in parts[1:5]):
        raise ValueError('Model is not inside models/<set>/<job>/<race>/<module>')
    return root.joinpath(*parts[:5])


def read_marks(root: Path, url: object) -> dict:
    path = marks_path(root, url)
    record = read_json(path)
    return {'url': url, 'faces': record.get('faces', []) if isinstance(record.get('faces'), list) else []}


def write_marks(root: Path, url: object, faces: object) -> dict:
    path = marks_path(root, url)
    if not isinstance(faces, list):
        raise ValueError('faces must be a list')
    clean = []
    for face in faces:
        if not isinstance(face, dict) or face.get('category') not in MARK_CATEGORIES:
            raise ValueError('Invalid face mark')
        mesh, triangle, centroid = face.get('mesh_index'), face.get('triangle'), face.get('centroid')
        if not all(isinstance(v, int) and v >= 0 for v in (mesh, triangle)):
            raise ValueError('Invalid face index')
        if not (isinstance(centroid, list) and len(centroid) == 3 and all(isinstance(v, (int, float)) for v in centroid)):
            raise ValueError('Invalid face centroid')
        clean.append({'mesh_index': mesh, 'mesh': str(face.get('mesh', ''))[:200], 'triangle': triangle,
                      'category': face['category'], 'centroid': [round(float(v), 6) for v in centroid]})
    counts = {c: sum(f['category'] == c for f in clean) for c in MARK_CATEGORIES}
    record = {'model': path.with_name(path.name.removesuffix('.marks.json') + '.glb').relative_to(root).as_posix(),
              'updated': datetime.now(timezone.utc).isoformat(), 'categories': list(MARK_CATEGORIES),
              'space': 'glTF scene space (Y-up); Blender import = (x, -z, y)',
              'triangle': 'index within the primitive (three.js faceIndex), meshes in glTF traversal order',
              'counts': counts, 'faces': clean}
    with LOCK:
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(record, indent=1))
        tmp.replace(path)
    return {'ok': True, 'counts': counts}


def thumb_path(model: Path) -> Path:
    return model.with_name(model.stem + '.thumb.png')


def fresh_thumb(root: Path, model: Path) -> str | None:
    """URL of the cached preview render, or None when missing or older than the model."""
    thumb = thumb_path(model)
    try:
        if thumb.stat().st_mtime < model.stat().st_mtime:
            return None
    except OSError:
        return None
    url = file_url(root, thumb)
    return url and f'{url}?v={int(thumb.stat().st_mtime)}'


def write_thumb(root: Path, url: object, png: bytes) -> dict:
    model = model_file(root, url)
    if not png.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('Not a PNG')
    with LOCK:
        tmp = thumb_path(model).with_name(thumb_path(model).name + '.tmp')
        tmp.write_bytes(png)
        tmp.replace(thumb_path(model))
    return {'ok': True, 'image': fresh_thumb(root, model)}


BASE_CACHE = HERE / 'cache' / 'bases'  # made by export_bases.py (Blender) from projects/<race>.blend


def on_body(root: Path, url: object) -> dict:
    """The placement of any GLB of a module (wearable, fitted, pieces, ...) on the game base: base GLB URL and the column-major glTF-space matrix of
    tools/outfit_studio/core/transform.native_to_world (the chain Outfit Studio imports items with)."""
    module = module_dir(root, url)
    manifest = read_json(module / 'reduced' / 'manifest.json')
    fit = (manifest.get('parts') or {}).get('fit') or {}
    if 'scale' not in fit or 'translation' not in fit:
        raise ValueError('manifest has no base fit (parts.fit)')
    race = module.parent.name
    record = read_json(BASE_CACHE / f'{safe_name(race)}.json')
    base = BASE_CACHE / f'{race}.glb'
    if not record or not base.is_file():
        raise ValueError(f'no cached base body for {race}: run blender -b -P tools/preview/export_bases.py')
    import importlib.util
    import numpy as np
    spec = importlib.util.spec_from_file_location('outfit_transform', root / 'tools' / 'outfit_studio' / 'core' / 'transform.py')
    tf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tf)
    to_blender = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], float)  # glTF (x, y, z) -> Blender (x, -z, y), row vectors
    gltf = np.vstack([np.zeros(3), np.eye(3)])
    world = tf.native_to_world(gltf @ to_blender, manifest, fit, record['H'], np.asarray(record['o'])) @ to_blender.T
    matrix = np.eye(4)
    matrix[:3, :3] = (world[1:] - world[0]).T
    matrix[:3, 3] = world[0]
    return {'base': file_url(root, base), 'matrix': matrix.T.ravel().tolist(), 'race': race}


BODY_SECTIONS = Path('experiments') / 'body_mask_20261104' / 'out'   # prototype outputs of experiments/body_mask_20261104 (body_sections.py); absent = feature inert


BODY_STRATEGIES = [{'id': 'legacy', 'label': 'Legacy (cut, under-garment rule)'}, {'id': 'seen', 'label': 'Seen (cut, visible skin kept)'}, {'id': 'tuck', 'label': 'Tuck (closed body, tucked under the garment)'},
                   {'id': 'bridge', 'label': 'Bridge (tuck + skin strip from the garment rims)'}, {'id': 'red_rim', 'label': 'Red rim (tuck + rim strip shaped by the generator skin)'},
                   {'id': 'red_patch', 'label': 'Red patch (sections at the rims morphed to the generator skin + strip)'}]
BODY_DEFAULT = 'seen'  # strategy shown when fit_choice.json has none (or it has no record for this module); user 2026-11-21: seen (no gaps) instead of tuck (distorts deep garments)


def body_rule(rec: dict) -> str:
    """The rule a body sections record was made with: 'tuck', 'seen' or 'legacy' (records from before the 2026-11-21 gap fix carry no rule)."""
    sec = (rec.get('params') or {}).get('sections') or {}
    return str(rec.get('mode') or (rec.get('seen_inputs') or {}).get('rule') or sec.get('mode') or sec.get('vis_rule') or 'legacy')


def body_sections(root: Path, url: object, strategy: object = None) -> dict:
    """Prototype "Body sections" of a module (experiments/body_mask_20261104): the sectioned base GLB of its race, the section nodes to leave out
    (state `hide`; `connector` sections are replaced by the outfit's connector meshes) and the connector GLB, as /files/ URLs. `{}` when the module has
    no output there (or it was made for another job), so the viewer option stays disabled. Body strategies (2026-11-21): a record per strategy in
    `out/<set>__<job>__<race>__<module>__body-<strategy>/` (legacy / seen / tuck / bridge / red_rim / red_patch, gapfix.py); `strategy` picks one (else BODY_DEFAULT, else the
    live record `out/<key>/`), `strategies` lists the available ones. The base / connector URLs carry `?v=<mtime>` (/files/ is cached for an hour)."""
    module = module_dir(root, url)
    set_name, job, race, name = module.parents[2].name, module.parents[1].name, module.parent.name, module.name
    out_root = root / BODY_SECTIONS
    live = _fitlab_lib(root).sections_dir(set_name, job, race, name, out_root)   # job-keyed folder, old name read when it is this job's
    key = f'{set_name}__{job}__{race}__{name}'

    def valid(folder):
        rec = read_json(folder / 'body_sections.json')
        return rec if rec and not rec.get('states_only') and f'/{job}/' in f'/{rec.get("module", "")}/' else None
    avail = {st['id']: out_root / f'{key}__body-{st["id"]}' for st in BODY_STRATEGIES if valid(out_root / f'{key}__body-{st["id"]}')}
    live_rec = valid(live)
    if live_rec and body_rule(live_rec) in {st['id'] for st in BODY_STRATEGIES}:
        avail[body_rule(live_rec)] = live          # the live record (what the fit lab fits against) stands for its own rule, over an older __body-<rule> copy
    pick = strategy if strategy in avail else (BODY_DEFAULT if BODY_DEFAULT in avail else (body_rule(live_rec) if live_rec and body_rule(live_rec) in avail else None))
    out = avail[pick] if pick else live
    record = valid(out)
    if not record:
        return {}
    base = root / 'experiments' / 'body_mask_20261104' / str(record.get('sectioned_base_glb', ''))
    base_url, conn_url = _fitlab_url(root, base), _fitlab_url(root, out / str(record.get('connectors_glb', 'connectors.glb')))
    if not base_url:
        return {}
    states = {v['base_node']: v['state'] for v in (record.get('sections') or {}).values() if isinstance(v, dict) and v.get('base_node')}
    listed = [{'id': st['id'], 'label': st['label']} for st in BODY_STRATEGIES if st['id'] in avail] or [{'id': 'live', 'label': 'Live record'}]
    return {'base': base_url, 'connectors': conn_url, 'layout': record.get('layout'), 'states': states, 'strategy': pick or 'live', 'strategies': listed,
            'connector_nodes': [v['connector_node'] for v in (record.get('sections') or {}).values() if isinstance(v, dict) and v.get('connector_node')],
            'hide': [node for node, state in states.items() if state in ('hide', 'connector')]}


# ------------------------------------------------------------------------------------------- fit lab (tools/tripo_pipeline/fit_lab.py)

FITLAB_STRATEGIES = [{'id': 'original', 'label': 'Original (aligned)'}, {'id': 'current', 'label': 'Current fit (whole body)'},
                     {'id': 'sections_fill', 'label': 'Sections: fill to body'}, {'id': 'sections_tight', 'label': 'Sections: tight (drawn body)'}]
FITLAB_SYMMETRY = [{'sym': 'off', 'scope': 'none'}, {'sym': 'l2r', 'scope': 'limbs'}, {'sym': 'l2r', 'scope': 'whole'}, {'sym': 'r2l', 'scope': 'limbs'}, {'sym': 'r2l', 'scope': 'whole'}]
FITLAB_DEFAULT = {'strategy': 'sections_fill', 'amount': 100, 'sym': 'off', 'scope': 'none'}
FITLAB_LOD = '0p4'
FIT_CHOICE = 'fit_choice.json'  # <module>/fit_choice.json {strategy, amount, sym, scope, lod, updated}
FITLAB_TIMEOUT = 900  # seconds one on-demand computation (incl. body sections) may take
FITLAB_SYMMAP_TIMEOUT = 300  # seconds the on-demand symmetry map of one module may take
FITLAB_LOCKS: dict[str, threading.Lock] = {}


class FitLabError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _fitlab_aligned(module: Path) -> Path:
    return module / 'reduced' / f'lod_error_{FITLAB_LOD}__wearable_aligned.glb'


def _fitlab_lib(root: Path):
    sys.path.insert(0, str(root / 'tools' / 'tripo_pipeline'))
    try:
        import fit_lab
    finally:
        sys.path.pop(0)
    return fit_lab


def _fitlab_url(root: Path, path: Path) -> str | None:
    url = file_url(root, path)
    try:
        return url and f'{url}?v={path.stat().st_mtime_ns // 1000000}'
    except OSError:
        return None


def validate_choice(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError('choice must be an object')
    strategy, sym, scope, amount = data.get('strategy'), data.get('sym'), data.get('scope'), data.get('amount')
    if strategy not in {s['id'] for s in FITLAB_STRATEGIES}:
        raise ValueError('Invalid strategy')
    if {'sym': sym, 'scope': scope} not in FITLAB_SYMMETRY:
        raise ValueError('Invalid symmetry / scope')
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not 0 <= amount <= 100:
        raise ValueError('amount must be a number from 0 to 100')
    body = data.get('body')
    out = {'strategy': strategy, 'amount': amount, 'sym': sym, 'scope': scope}
    if body is not None:
        if body not in {st['id'] for st in BODY_STRATEGIES} | {'live'}:
            raise ValueError('Invalid body strategy')
        out['body'] = body
    return out


def fitlab_choice_of(module: Path) -> dict:
    record = read_json(module / FIT_CHOICE)
    try:
        return {**validate_choice(record), 'updated': record.get('updated')}
    except ValueError:
        return dict(FITLAB_DEFAULT)


def fitlab_info(root: Path, url: object) -> dict:
    module = module_dir(root, url)
    aligned = _fitlab_aligned(module)
    if aligned.is_symlink() or not aligned.is_file():
        return {'available': False, 'lod': FITLAB_LOD}
    fl = _fitlab_lib(root)
    paths = fl.module_paths(module)
    cached = {cid: _fitlab_url(root, path) for cid, path in fl.cached_urls(paths).items()}
    return {'available': True, 'lod': FITLAB_LOD, 'base': _fitlab_url(root, aligned), 'sections': body_sections(root, url, fitlab_choice_of(module).get('body')), 'strategies': FITLAB_STRATEGIES,
            'symmetry': FITLAB_SYMMETRY, 'cached': {cid: u for cid, u in cached.items() if u}, 'choice': fitlab_choice_of(module), 'symmap': fitlab_symmap_urls(root, module)}


def _fitlab_symlib(root: Path):
    sys.path.insert(0, str(root / 'tools' / 'tripo_pipeline'))
    try:
        import fit_symmap
    finally:
        sys.path.pop(0)
    return fit_symmap


def fitlab_symmap_urls(root: Path, module: Path) -> dict | None:
    """`{bin, json}` URLs of the module's fresh symmetry map (tools/tripo_pipeline/fit_symmap.py: symmetry is applied in the viewer by mirroring both endpoints of the
    Amount slider), or None when it is missing or stale (GET /api/fitlab/symmap builds it)."""
    fs = _fitlab_symlib(root)
    if not fs.is_fresh(module):
        return None
    paths = fs.map_paths(module)
    bin_url, json_url = _fitlab_url(root, paths['map_bin']), _fitlab_url(root, paths['map_json'])
    return {'bin': bin_url, 'json': json_url} if bin_url and json_url else None


def fitlab_symmap(root: Path, url: object) -> dict:
    """URLs of the module's symmetry map, built on demand (fit_symmap.py in a subprocess, one at a time per module; a few seconds)."""
    module = module_dir(root, url)
    aligned = _fitlab_aligned(module)
    if aligned.is_symlink() or not aligned.is_file():
        raise FitLabError('module has no aligned 0p4 wearable')
    with LOCK:
        lock = FITLAB_LOCKS.setdefault(str(module), threading.Lock())
    with lock:
        urls = fitlab_symmap_urls(root, module)
        if not urls:
            cmd = [sys.executable, '-u', str(root / 'tools' / 'tripo_pipeline' / 'fit_symmap.py'), str(module)]
            try:
                done = subprocess.run(cmd, capture_output=True, text=True, timeout=FITLAB_SYMMAP_TIMEOUT, cwd=str(root))
            except subprocess.TimeoutExpired:
                raise FitLabError(f'symmetry map timed out after {FITLAB_SYMMAP_TIMEOUT} s', 504) from None
            urls = fitlab_symmap_urls(root, module)
            if done.returncode != 0 or not urls:
                tail = ' | '.join((done.stdout + '\n' + done.stderr).strip().splitlines()[-4:])
                raise FitLabError(f'symmetry map failed: {tail}'[:600], 500)
    return urls


def fitlab_combo(root: Path, url: object, strategy: object, sym: object, scope: object) -> dict:
    """URL of a fit combo's GLB; a symmetry-off combo is computed on demand (fit_lab.py in a subprocess, one at a time per module) when it is not cached,
    a symmetric one only served when an old baked file is cached."""
    module = module_dir(root, url)
    aligned = _fitlab_aligned(module)
    if aligned.is_symlink() or not aligned.is_file():
        raise FitLabError('module has no aligned 0p4 wearable')
    fl = _fitlab_lib(root)
    try:
        strategy, sym, scope = fl.parse_combo(f'{strategy}__{sym}__{scope}')
    except ValueError as exc:
        raise FitLabError(str(exc)) from None
    cid = f'{strategy}__{sym}__{scope}'
    paths = fl.module_paths(module)
    if cid == 'original__off__none':
        return {'url': _fitlab_url(root, aligned)}
    if sym != 'off':      # symmetry is applied in the viewer from the symmetry map (see fitlab_symmap); old baked symmetric GLBs are still served when cached
        if cid in fl.cached_urls(paths):
            return {'url': _fitlab_url(root, fl.cached_urls(paths)[cid])}
        raise FitLabError('symmetric fits are no longer computed: the viewer mirrors the symmetry-off fit with the symmetry map (/api/fitlab/symmap)')
    with LOCK:
        lock = FITLAB_LOCKS.setdefault(str(module), threading.Lock())
    with lock:
        if cid not in fl.cached_urls(paths):
            cmd = [sys.executable, '-u', str(root / 'tools' / 'tripo_pipeline' / 'fit_lab.py'), str(module), '--combos', cid]
            try:
                done = subprocess.run(cmd, capture_output=True, text=True, timeout=FITLAB_TIMEOUT, cwd=str(root))
            except subprocess.TimeoutExpired:
                raise FitLabError(f'fit timed out after {FITLAB_TIMEOUT} s', 504) from None
            if done.returncode != 0 or cid not in fl.cached_urls(paths):
                tail = ' | '.join((done.stdout + '\n' + done.stderr).strip().splitlines()[-4:])
                raise FitLabError(f'fit failed: {tail}'[:600], 500)
        return {'url': _fitlab_url(root, fl.cached_urls(paths)[cid])}


def write_fit_choice(module: Path, choice: dict) -> None:
    record = {**choice, 'lod': FITLAB_LOD, 'updated': datetime.now(timezone.utc).isoformat()}
    with LOCK:
        tmp = module / (FIT_CHOICE + '.tmp')
        tmp.write_text(json.dumps(record, indent=1))
        tmp.replace(module / FIT_CHOICE)


def fitlab_choice(root: Path, data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError('body must be an object')
    module = module_dir(root, data.get('url'))
    choice = validate_choice(data)
    write_fit_choice(module, choice)
    return {'ok': True, 'choice': choice}


def fitlab_apply_all(root: Path, data: object) -> dict:
    """The same choice for every module of the set with this module's name (any job, any race) that has an aligned 0p4 wearable."""
    if not isinstance(data, dict):
        raise ValueError('body must be an object')
    module = module_dir(root, data.get('url'))
    choice = validate_choice(data)
    written = []
    for target in sorted(module.parents[2].glob(f'*/*/{module.name}')):
        aligned = _fitlab_aligned(target)
        if target.is_symlink() or not target.is_dir() or aligned.is_symlink() or not aligned.is_file():
            continue
        if not all(NAME.fullmatch(part) for part in target.relative_to(root).parts[1:]):
            continue
        write_fit_choice(target, choice)
        written.append(target.relative_to(root).as_posix())
    return {'ok': True, 'count': len(written), 'modules': written, 'choice': choice}


def write_lod(root: Path, data: dict) -> dict:
    if data.get('lod') not in LOD_LEVELS:
        raise ValueError('Invalid LOD')
    folder = direct(root, 'models', data.get('set'))
    for part in ('job', 'race', 'module'):
        folder = folder / safe_name(data.get(part))
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError('Model folder not found')
    record = {'lod': data['lod'], 'reduced': f'reduced/lod_error_{data["lod"]}.glb',
              'wearable': f'reduced/lod_error_{data["lod"]}__wearable.glb',
              'updated': datetime.now(timezone.utc).isoformat()}
    with LOCK:
        (folder / LOD_CHOICE).write_text(json.dumps(record, indent=1))
    return {'ok': True, 'lod': data['lod']}


def state(root: Path, models: bool = True) -> dict:
    """All tabs; `models=False` leaves out the (slowest) models and approved models tabs, e.g. for the refresh after a concept approve/reject."""
    pipeline = jobs(root)
    result = {'concepts': concepts(root, 'concepts'), 'provisional': concepts(root, 'provisional'), 'approved': concepts(root, 'approved'),
              'archive': concepts(root, 'archive'), 'pipeline': pipeline, 'held': held_cards(root)}
    if models:
        cards = model_cards(root, pipeline)
        result['models'] = [c for c in cards if not c['approved']]
        result['approved_models'] = [c | {'id': f'approvedmodel:{c["name"]}'} for c in cards if c['approved']]
    return result | {'review': review_cards(root), 'trash': trash_cards(root), 'monsters': monsters.cards(root)}


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


def origin_split(src: object) -> tuple[str, str]:
    """Split a trash origin like `concepts/x` or `archive/concepts/x` into (folder, name)."""
    for folder in ('concepts', ARCHIVE):
        prefix = folder + '/'
        if isinstance(src, str) and src.startswith(prefix) and src == f'{prefix}{src.removeprefix(prefix)}':
            return folder, safe_name(src.removeprefix(prefix))
    raise ValueError('Invalid origin')


def action(root: Path, verb: str, item_id: str) -> dict:
    parts = item_id.split(':')
    if not all(NAME.fullmatch(p) for p in parts):
        raise ValueError('Invalid ID')
    src = dst = None
    result = {}
    with LOCK:
        try:
            if verb in ('archive', 'unarchive') and len(parts) == 2 and parts[0] == ('concepts' if verb == 'archive' else 'archive'):
                to_archive = verb == 'archive'
                src = direct(root, 'concepts' if to_archive else ARCHIVE, parts[1])
                dst = direct(root, ARCHIVE if to_archive else 'concepts', parts[1])
                if not src.is_dir() or not set_card(root, src, parts[0]): raise FileNotFoundError(src)
                if dst.exists(): raise FileExistsError(dst)
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dst)
                result['undo_id'] = f'{"archive" if to_archive else "concepts"}:{dst.name}'
            elif verb in ('archive', 'unarchive') and len(parts) == 3 and parts[0] == ('legacy' if verb == 'archive' else 'archlegacy'):
                to_archive = verb == 'archive'
                batch = direct(root, 'concepts' if to_archive else ARCHIVE, parts[1])
                key = safe_name(parts[2])
                src = batch / key
                files = flat_files(batch, key)
                dst = direct(root, ARCHIVE if to_archive else 'concepts', parts[1])
                move_files(files, dst)  # refuses when any destination file exists
                result['undo_id'] = f'{"archlegacy" if to_archive else "legacy"}:{parts[1]}:{key}'
            elif verb == 'approve' and len(parts) == 2 and parts[0] in ('concepts', 'archive'):
                # First approval hop: into provisional/. The second hop (provisional -> approved) is its own verb branch below.
                src, dst = direct(root, 'concepts' if parts[0] == 'concepts' else ARCHIVE, parts[1]), direct(root, 'provisional', parts[1])
                if not src.is_dir(): raise FileNotFoundError(src)
                if dst.exists(): raise FileExistsError(dst)
                if not set_card(root, src, parts[0]): raise ValueError('No per-set fronts found')
                dst.parent.mkdir(exist_ok=True)
                src.rename(dst)
                result['undo_id'] = f'provisional:{dst.name}'
                # Validation happens after the move. Keep the moved set even when it fails.
                command = [sys.executable, str(root / 'tools' / 'tripo_pipeline' / 'workflow.py'),
                           '--root', str(root), 'check', str(dst)]
                checked = subprocess.run(command, capture_output=True, text=True, check=False)
                if checked.returncode:
                    result['warning'] = checked.stderr.strip() or checked.stdout.strip() or 'Validation failed'
                else:
                    result['check'] = json.loads(checked.stdout)
            elif verb == 'approve' and len(parts) == 2 and parts[0] == 'provisional':
                # Second approval hop: provisional -> approved (the final stage workflow.py claim reads).
                src, dst = direct(root, 'provisional', parts[1]), direct(root, 'approved', parts[1])
                if not src.is_dir(): raise FileNotFoundError(src)
                if dst.exists(): raise FileExistsError(dst)
                if not set_card(root, src, 'provisional'): raise ValueError('No per-set fronts found')
                dst.parent.mkdir(exist_ok=True)
                src.rename(dst)
                result['undo_id'] = f'approved:{dst.name}'
                command = [sys.executable, str(root / 'tools' / 'tripo_pipeline' / 'workflow.py'),
                           '--root', str(root), 'check', str(dst)]
                checked = subprocess.run(command, capture_output=True, text=True, check=False)
                if checked.returncode:
                    result['warning'] = checked.stderr.strip() or checked.stdout.strip() or 'Validation failed'
                else:
                    result['check'] = json.loads(checked.stdout)
            elif verb == 'approve' and len(parts) == 3 and parts[0] in ('legacy', 'archlegacy'):
                origin_dir = 'concepts' if parts[0] == 'legacy' else ARCHIVE
                batch, key = direct(root, origin_dir, parts[1]), safe_name(parts[2])
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
                (dst / f'{key}.preview-origin.json').write_text(json.dumps({'kind': 'legacy', 'src': f'{origin_dir}/{batch.name}', 'approved_set': promoted['set']}))
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
                origin_dir, name = origin_split(origin.get('src'))
                dst = direct(root, origin_dir, name)
                if dst.exists(): raise FileExistsError(dst)
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dst)
            elif verb == 'restore' and len(parts) == 3 and parts[0] == 'trashlegacy':
                batch, key = direct(root, 'trash/concepts', parts[1]), safe_name(parts[2])
                sidecar = batch / f'{key}.preview-origin.json'
                origin = read_json(sidecar)
                if origin.get('kind') != 'legacy': raise ValueError('Missing origin sidecar')
                src = batch / key
                origin_dir, dest_name = origin_split(origin.get('src'))
                dst = direct(root, origin_dir, dest_name)
                files = flat_files(batch, key)
                if any((dst / p.name).exists() for p in files): raise FileExistsError('Restore destination exists')
                move_files(files, dst)
                # Keep the origin sidecar as an audit record; no files are deleted.
            elif verb == 'unapprove' and len(parts) == 2 and parts[0] == 'approved':
                # Approved steps back to provisional (one review stage at a time).
                src, dst = direct(root, 'approved', parts[1]), direct(root, 'provisional', parts[1])
                if not src.is_dir() or not set_card(root, src, 'approved'): raise FileNotFoundError(src)
                if (src / '.tripo-job.json').exists(): raise ValueError('Set has already been claimed')
                if dst.exists(): raise FileExistsError(dst)
                dst.parent.mkdir(exist_ok=True)
                src.rename(dst)
                result['undo_id'] = f'provisional:{dst.name}'
            elif verb == 'unapprove' and len(parts) == 2 and parts[0] == 'provisional':
                # Provisional steps back to concepts.
                src, dst = direct(root, 'provisional', parts[1]), direct(root, 'concepts', parts[1])
                if not src.is_dir() or not set_card(root, src, 'provisional'): raise FileNotFoundError(src)
                if (src / '.tripo-job.json').exists(): raise ValueError('Set has already been claimed')
                if dst.exists(): raise FileExistsError(dst)
                dst.parent.mkdir(exist_ok=True)
                src.rename(dst)
                result['undo_id'] = f'concepts:{dst.name}'
            elif verb == 'approve' and len(parts) == 2 and parts[0] == 'model':
                src = direct(root, 'models', parts[1])
                dst = src / MODEL_APPROVAL
                finished = completed_jobs(root, parts[1])
                if not finished: raise FileNotFoundError(f'No completed job for {parts[1]}')
                if model_approved(dst): raise FileExistsError(f'{parts[1]} is already approved')
                record = {'approved': True, 'time': datetime.now(timezone.utc).isoformat(), 'set': parts[1], 'jobs': finished}
                tmp = dst.with_name(dst.name + '.tmp')
                tmp.write_text(json.dumps(record, indent=1))
                tmp.replace(dst)
                result['undo_id'] = f'approvedmodel:{parts[1]}'
            elif verb == 'unapprove' and len(parts) == 2 and parts[0] == 'approvedmodel':
                src = direct(root, 'models', parts[1]) / MODEL_APPROVAL
                if not model_approved(src): raise FileNotFoundError(f'{parts[1]} is not approved')
                src.unlink()  # the actions.jsonl log is the audit record
                result['undo_id'] = f'model:{parts[1]}'
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
        if 'gzip' in (self.headers.get('Accept-Encoding') or '').lower() and len(raw) > 4096:
            body = gzip.compress(raw, 6)
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Length', str(len(body)))
        else:
            body = raw
            self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == '/api/state':
            self.send_json(200, state(self.root, parse_qs(urlsplit(self.path).query).get('models', ['1'])[0] != '0'))
            return
        if path == '/api/on_body':
            try:
                self.send_json(200, on_body(self.root, parse_qs(urlsplit(self.path).query).get('url', [''])[0]))
            except (ValueError, OSError, KeyError) as exc:
                self.send_json(400, {'ok': False, 'error': str(exc)})
            return
        if path == '/api/body_sections':
            try:
                q = parse_qs(urlsplit(self.path).query)
                self.send_json(200, body_sections(self.root, q.get('url', [''])[0], (q.get('strategy') or [None])[0]))
            except (ValueError, OSError, KeyError) as exc:
                self.send_json(400, {'ok': False, 'error': str(exc)})
            return
        if path == '/api/fitlab':
            try:
                self.send_json(200, fitlab_info(self.root, parse_qs(urlsplit(self.path).query).get('url', [''])[0]))
            except (ValueError, OSError, KeyError) as exc:
                self.send_json(400, {'ok': False, 'error': str(exc)})
            return
        if path == '/api/fitlab/symmap':
            try:
                self.send_json(200, fitlab_symmap(self.root, parse_qs(urlsplit(self.path).query).get('url', [''])[0]))
            except FitLabError as exc:
                self.send_json(exc.status, {'ok': False, 'error': str(exc)})
            except (ValueError, OSError, KeyError) as exc:
                self.send_json(400, {'ok': False, 'error': str(exc)})
            return
        if path == '/api/fitlab/combo':
            query = parse_qs(urlsplit(self.path).query)
            try:
                self.send_json(200, fitlab_combo(self.root, *(query.get(k, [''])[0] for k in ('url', 'strategy', 'sym', 'scope'))))
            except FitLabError as exc:
                self.send_json(exc.status, {'ok': False, 'error': str(exc)})
            except (ValueError, OSError, KeyError) as exc:
                self.send_json(400, {'ok': False, 'error': str(exc)})
            return
        if path == '/api/clearance':
            try:
                self.send_json(200, read_clearance(self.root, parse_qs(urlsplit(self.path).query).get('url', [''])[0]))
            except ValueError as exc:
                self.send_json(400, {'ok': False, 'error': str(exc)})
            return
        if path == '/api/marks':
            try:
                self.send_json(200, read_marks(self.root, parse_qs(urlsplit(self.path).query).get('url', [''])[0]))
            except ValueError as exc:
                self.send_json(400, {'ok': False, 'error': str(exc)})
            return
        if path in ('/', '/index.html', '/app.js', '/fitsym.js', '/monsters.js', '/style.css'):
            file = HERE / ('index.html' if path == '/' else path.lstrip('/'))
        elif path.startswith('/files/'):
            relative = unquote(path.removeprefix('/files/'))
            file = self.root / relative
            if file.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp', '.glb') and not (file.name in ('symmap.bin', 'symmap.json') and file.parent.name.startswith('lod_') and file.parent.parent.name == 'fitlab'):
                self.send_error(403); return
            try:
                file.resolve(strict=True).relative_to(self.root.resolve(strict=True))
            except (ValueError, OSError):
                self.send_error(403); return
        elif path.startswith(monsters.FILE_PREFIX):     # Monsters tab: images and GLBs of characters/monsters/aaa
            file = monsters.resolve_file(self.root, path)
            if file is None:
                self.send_error(404); return
        else:
            self.send_error(404); return
        if not file.is_file():
            self.send_error(404); return
        raw = file.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        if path.startswith('/files/'):
            self.send_header('Cache-Control', 'private, max-age=3600')  # immutable review artifacts
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        verb = urlsplit(self.path).path.removeprefix('/api/')
        if urlsplit(self.path).path == '/api/thumbnail':
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size < 1 or size > 8 * 1024 * 1024: raise ValueError('Invalid request size')
                url = parse_qs(urlsplit(self.path).query).get('url', [''])[0]
                self.send_json(200, write_thumb(self.root, url, self.rfile.read(size)))
            except (ValueError, OSError) as exc:
                self.send_json(400, {'ok': False, 'error': str(exc)})
            return
        if urlsplit(self.path).path in ('/api/fitlab/choice', '/api/fitlab/apply_all'):
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size < 1 or size > 4096: raise ValueError('Invalid request size')
                data = json.loads(self.rfile.read(size))
                handler = fitlab_choice if urlsplit(self.path).path.endswith('/choice') else fitlab_apply_all
                self.send_json(200, handler(self.root, data))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                self.send_json(400, {'ok': False, 'error': str(exc)})
            return
        if self.path == '/api/monsters/choice':
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size < 1 or size > 4096: raise ValueError('Invalid request size')
                self.send_json(200, monsters.write_choice(self.root, json.loads(self.rfile.read(size))))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                self.send_json(400, {'ok': False, 'error': str(exc)})
            return
        if self.path == '/api/lod':
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size < 1 or size > 4096: raise ValueError('Invalid request size')
                self.send_json(200, write_lod(self.root, json.loads(self.rfile.read(size))))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                self.send_json(400, {'ok': False, 'error': str(exc)})
            return
        if self.path == '/api/marks':
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size < 1 or size > MARK_LIMIT: raise ValueError('Invalid request size')
                data = json.loads(self.rfile.read(size))
                self.send_json(200, write_marks(self.root, data['url'], data['faces']))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                self.send_json(400, {'ok': False, 'error': str(exc)})
            return
        if verb not in ('approve', 'reject', 'restore', 'unapprove', 'archive', 'unarchive') or self.path.split('?')[0] != '/api/' + verb:
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
