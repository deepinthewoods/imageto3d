#!/usr/bin/env python3
"""Library of the preview app (the old site it served was retired on 2026-11-25, pipeline v2 phase 6).

The old review site (the HTTP handler that lived here, app.js, index.html, style.css, fitsym.js, monsters.js: the fit lab, body
sections, face marks, the Models / Approved models / Pipeline / Held tabs and their approve / promote endpoints) was removed. The
site is now the v2 app: `python -X utf8 -u tools/preview/v2/server.py --port 8765 --manage-services` (tools/preview/v2/CUTOVER.md).

What stays is the library the v2 server (tools/preview/v2/*.py) imports: the name rules (`NAME`, `safe_name`, `direct`), the
mtime-validated scan cache (`_memo`, `_listing`, `_stamp`, `_prune`, `POOL`), the concept-stage scans (`set_card`, `concepts`,
`trash_cards`), the job view scan (`_job_views`), races and set ids (`_base_races`, `_job_set`), the Review tab scan (`review_cards`
with `_cached_module`), thumbnails (`write_thumb`, `fresh_thumb`), the flat legacy helpers (`flat_files`, `origin_split`, `unique`),
the action log (`log_action`) and the monster scan module (`monsters`). Running this file only prints where the site went (exit 2).

Data files the old UI wrote stay where they are and are not touched: `fit_choice.json`, `*.marks.json`, the body sections of
experiments/body_mask_20261104, `models/<set>/model_approval.json` (read by v2/migrate.py) and `chosen_lod.json`.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from urllib.parse import quote, unquote

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parents[1]
MODULES = ('body_armor', 'right_gauntlet_boot', 'outfit')
VIEWS = ('front', 'front_three_quarter', 'back', 'back_three_quarter')
HELD = 'held_conversions'  # staged jobs that must not be converted until the user releases the hold (AGENTS.md)
ARCHIVE = 'archive/concepts'  # sets kept for reference, out of the review queue
MODEL_APPROVAL = 'model_approval.json'  # models/<set>/model_approval.json {"approved": true, ...}: the retired Models tab's approval (read by v2/migrate.py)
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


LOD_LEVELS = ('0p4', '0p2', '0p1')
LOD_CHOICE = 'chosen_lod.json'  # the LOD the user picked in the viewer for the final model
# Variants written beside the originals: body_fit.py --method full | vertex (`_fitted`, `_fitted_vertex`), align_body.py (`_aligned`: the wearable
# deformed so the Tripo body sits on the game base) and body_fit.py --source aligned (`_aligned_fitted`); the same suffixes name the piece folders.
FIT_METHODS = {'fitted': '_fitted', 'fitted_vertex': '_fitted_vertex', 'aligned': '_aligned', 'aligned_fitted': '_aligned_fitted'}
FIT_ORIGIN = {'aligned_fitted': '_aligned'}      # a twin made from another twin: stale when that one is newer
# The preview's "Red body" overlay (align_body.py): the converted model's own base-role faces, as converted and deformed with the wearable's alignment field.
# key -> (file suffix, the files of the level it is made from: '' = the labelled lod_error_<lv>.glb)
BODY_OVERLAYS = {'red_body': ('__tripo_body', ('',)), 'red_body_aligned': ('__tripo_body_aligned', ('', '__wearable_aligned'))}


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


def unique(path: Path) -> Path:
    if not path.exists():
        return path
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    return path.with_name(path.name + '_' + stamp)


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


def log_action(root: Path, verb: str, src: str | None, dst: str | None, result: str) -> None:
    path = root / 'tools' / 'preview' / 'actions.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as out:
        out.write(json.dumps({'time': datetime.now(timezone.utc).isoformat(), 'action': verb,
                              'src': src, 'dst': dst, 'result': result}) + '\n')


RETIRED = """The old preview site (tools/preview/serve.py) was retired on 2026-11-25 (pipeline v2, phase 6).
Start the v2 app instead, from the repo root:

    python -X utf8 -u tools/preview/v2/server.py --port 8765 --manage-services

It manages the pipeline services (supervisor, view runner, convert worker) itself; see tools/preview/v2/CUTOVER.md.
Never start a second supervisor, view runner or convert worker by hand while it runs."""


def main() -> None:
    print(RETIRED, file=sys.stderr, flush=True)
    raise SystemExit(2)


if __name__ == '__main__':
    main()
