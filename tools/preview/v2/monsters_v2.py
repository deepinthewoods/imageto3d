"""Monsters as a kind of the v2 preview app (DESIGN_PHASE5.md sections A, B, C, D, F; the lane itself is monster_lane.py).

The monster files live in the monster pipeline root (`characters/monsters/aaa`, `$IMAGETO3D_MONSTERS` in tests; `monsters.monsters_root`),
which is a DIFFERENT repository. v2 never moves a monster file; everything here is a state change, a copy into a new folder, or an
atomic JSON write:

  concepts/<Folder>/codexN-front.png (+ .prompt.json, .colours.json)   a concept; attempts / JSON-only numbers / loose PNGs are hidden
  concepts/<Folder>/.v2_state.json   {"schema":1,"concepts":{"codexN":{"stage","time","monster"?,"variant"?,"from"?,...}}}
                                      (absent concept = stage `concepts`; written atomically under a per-folder lock)
  pipeline/<M>/variant_names.json    `variants[<v>]` = {"was","concept","picked","by":"v2"} for a v2 pick; an undone pick moves to
                                      `retired[<v>]`, so a number is never used twice
  pipeline/<M>/views/<v>/            front.png, colours.json, prompts.json (copies), views.json, and the extra views left/back/right.png
                                      (+ <view>.prompts.json written by the Codex jobs)
  pipeline/<M>/models/<v>/           hyper3d/ (studio_submission.json, glb_*.glb), reduced/ (5 LODs), chosen_lod.json

Locks (cross-process OS byte locks, guard.os_lock, kept in THIS project's `.codex_jobs/_app/locks/` so the monster repository gets no lock
files): `mconcepts/<folder>.lock` around every read-check-write of a folder's state file, `monsters/<monster>.lock` around a pick, an
import, a withdrawal and every views.json write. chosen_lod.json is written under `models/<v>/.pipeline_v2.lock`, the lock of
`decisions.write_monster_lod` (the v2 locked monster writer).

Hooks wired into catalog / actions / orchestrator / migrate (DESIGN_PHASE5.md H.1): `state_cards`, `item`, `perform` (+ `ALLOWED`), `auto_freeze` (orchestrator tick,
through monster_lane.tick), `migration_plan` / `apply_migration` (migrate.py --monsters). Nothing here imports catalog.py / actions.py /
orchestrator.py / server.py (catalog's NotFound is looked up lazily so the HTTP layer answers 404).
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import time
from urllib.parse import quote
import uuid

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import guard  # noqa: E402
import monsters  # noqa: E402  (tools/preview/monsters.py: monsters_root, LOD_LEVELS, CHOICE, cards)
import monster_prompts  # noqa: E402

STATE_FILE = '.v2_state.json'
STAGES = ('concepts', 'provisional', 'approved', 'archive', 'trash')
SHOWN_STAGES = ('concepts', 'provisional', 'archive', 'trash')      # picked (approved) concepts are shown as their variant card
FINAL = re.compile(r'^codex(\d+)-front\.png$')
KEY = re.compile(r'^codex(\d+)$')
VARIANT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')
LEADING_NUMBER = re.compile(r'^(\d+)(?:-|$)')
WITHDRAWN_DIR = re.compile(r'^\.v2_withdrawn_(.+)_\d{8}T\d{6}(?:_\d+)?$')
VIEW_ORDER = ('front', 'left', 'back', 'right')                     # views.json order = upload order of the frozen list
EXTRA_VIEWS = monster_prompts.VIEWS                                 # left, back, right
LOD_LEVELS = monsters.LOD_LEVELS
CHOICE = monsters.CHOICE
LOD_LOCK = '.pipeline_v2.lock'                                      # = decisions.LOCKNAME (decisions.write_monster_lod)
VIEWS_JSON = 'views.json'
VIEW_TASK_KIND = 'mviews'                                           # task name `mviews--<monster slug>--<variant>--<view>`
VIEW_PRIORITY = 3
VIEW_MAX_ATTEMPTS = 2
LIBRARY_BADGE = 'no library folder mapping (add to PIPELINE_LIBRARY)'
# migration: picks known only through `was` (DESIGN_PHASE5.md A): (concept folder, concept key) -> (monster, variant)
MIGRATION_OVERRIDES = {('Goblin Shaman', 'codex4'): ('Goblin Shaman', 'main')}
FORBIDDEN_CHARS = set('<>:"|?*/\\')
CONCEPT_ACTIONS = {'concepts': ['provisional', 'approve', 'reject'], 'provisional': ['approve', 'unapprove'],
                   'archive': ['approve', 'unarchive'], 'trash': ['restore']}
# (id type, verb) pairs of POST /api/v2/action for monster ids (the integration adds them to actions.ALLOWED)
CONCEPT_VERBS = {'provisional': ('concepts', 'archive'), 'approve': ('concepts', 'provisional', 'archive'),
                 'reject': ('concepts', 'provisional'), 'archive': ('concepts', 'provisional'), 'unarchive': ('archive',),
                 'unapprove': ('provisional', 'approved'), 'restore': ('trash',)}
MONSTER_VERBS = ('convert', 'views', 'approve', 'unapprove', 'reconvert', 'view_retry', 'import')
ALLOWED = frozenset({('mconcept', v) for v in CONCEPT_VERBS} | {('monster', v) for v in MONSTER_VERBS})
PROMPT_FIELDS = ('design', 'notes', 'colour_variant', 'job', 'date', 'attempts', 'size', 'mode', 'prompt')   # item.prompt (the dialog)
INTENT_FIELDS = ('adjective', 'direction', 'local_description', 'user_guidance', 'set')                     # prompt json -> item.intent
RUNNER_STATE = {'pending': 'queued', 'launching': 'running', 'running': 'running', 'done': 'done', 'failed': 'failed'}
_CACHE_LOCK = threading.Lock()


class Frozen(guard.Conflict):
    """Use enabled views on a variant whose frozen views were converted already: converting again is Reconvert (3D tab, paid). HTTP 409."""
    code = 'frozen'

_FOLDER_CACHE: dict[str, tuple] = {}
_INTENT_CACHE: dict[str, tuple] = {}
_LIBRARY_CACHE: dict[str, tuple] = {}


# ------------------------------------------------------------------------------------------------------------------- basics

def now() -> str:
    return guard.now()


def stamp() -> str:
    return time.strftime('%Y%m%dT%H%M%S')


def mroot_of(root: Path) -> Path:
    return monsters.monsters_root(Path(root))


def not_found(message: str) -> Exception:
    """catalog.NotFound (HTTP 404) when catalog is loaded (the server), else a LookupError (tests of this module alone)."""
    cat = sys.modules.get('catalog')
    return cat.NotFound(message) if cat is not None and hasattr(cat, 'NotFound') else LookupError(message)


def invalidate() -> None:
    cat = sys.modules.get('catalog')
    if cat is not None and hasattr(cat, 'invalidate'):
        cat.invalidate()


def read_json(path: Path, default=None):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default
    return value


def read_obj(path: Path) -> dict:
    value = read_json(path, {})
    return value if isinstance(value, dict) else {}


def dump(data) -> bytes:
    return (json.dumps(data, indent=2) + '\n').encode('utf-8')


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def slug(name: str) -> str:
    """A task / lock name part: [A-Za-z0-9_-] (spaces and the rest become `_`)."""
    return re.sub(r'[^A-Za-z0-9_-]', '_', name).strip('_') or 'x'


def safe_part(name: object, what: str) -> str:
    """A folder name used as ONE path segment: no separators, no `..`, no leading dot, no reserved characters."""
    if not isinstance(name, str) or not name or name != name.strip() or name in ('.', '..') or name.startswith('.') or \
            any(c in FORBIDDEN_CHARS or ord(c) < 32 for c in name) or name.endswith('.'):
        raise ValueError(f'Invalid {what}')
    return name


def check_variant(variant: object) -> str:
    if not isinstance(variant, str) or not VARIANT.fullmatch(variant) or '..' in variant:
        raise ValueError('Invalid variant')
    return variant


def lock_path(root: Path, kind: str, name: str) -> Path:
    short = hashlib.sha1(name.encode('utf-8')).hexdigest()[:8]
    return Path(root) / guard.APP / 'locks' / kind / f'{slug(name)[:40]}-{short}.lock'


def folder_lock(root: Path, folder: str) -> guard.OsLock:
    return guard.os_lock(lock_path(root, 'mconcepts', folder))


def monster_lock(root: Path, monster: str) -> guard.OsLock:
    return guard.os_lock(lock_path(root, 'monsters', monster))


def murl(mroot: Path, path: Path, mtime: float | None = None) -> str | None:
    """`/mfiles/` URL of a regular file under the monster root (the route monsters.resolve_file serves), else None."""
    try:
        rel = Path(path).relative_to(mroot).as_posix()
        if mtime is None:
            if not Path(path).is_file():
                return None
            mtime = Path(path).stat().st_mtime
    except (OSError, ValueError):
        return None
    return monsters.FILE_PREFIX + quote(rel, safe='/') + f'?v={int(mtime)}'


def _dirs(path: Path) -> list[str]:
    try:
        with os.scandir(path) as entries:
            return sorted(e.name for e in entries if e.is_dir(follow_symlinks=False) and not e.name.startswith('.'))
    except OSError:
        return []


def _names(path: Path) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


# ------------------------------------------------------------------------------------------------------------ folder -> monster

def norm(name: str) -> str:
    return name.lower().replace(' ', '')


def pipeline_monsters(mroot: Path) -> list[str]:
    return _dirs(mroot / 'pipeline')


def monster_for_folder(mroot: Path, folder: str) -> str:
    """`concepts/<Folder>` -> the `pipeline/<M>` whose name matches lowercase without spaces (`Warhorse` <-> `War Horse`); with no
    match the folder name itself (the new pipeline folder)."""
    key = norm(folder)
    for monster in pipeline_monsters(mroot):
        if norm(monster) == key:
            return monster
    return folder


def folder_for_monster(mroot: Path, monster: str) -> str | None:
    key = norm(monster)
    for folder in _dirs(mroot / 'concepts'):
        if norm(folder) == key:
            return folder
    return None


def library_map(mroot: Path) -> dict | None:
    """`PIPELINE_LIBRARY` of `<monster root>/tools/build_monster_blends.py` (read with ast, never imported); None when it cannot be read."""
    path = Path(mroot) / 'tools' / 'build_monster_blends.py'
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    key = str(path)
    with _CACHE_LOCK:
        hit = _LIBRARY_CACHE.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
    value = None
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'PIPELINE_LIBRARY' for t in node.targets):
                found = ast.literal_eval(node.value)
                value = found if isinstance(found, dict) else None
    except (OSError, ValueError, SyntaxError):
        value = None
    with _CACHE_LOCK:
        _LIBRARY_CACHE[key] = (mtime, value)
    return value


def library_info(mroot: Path, monster: str) -> dict:
    mapping = library_map(mroot)
    if mapping is None:
        return {'known': False, 'mapped': None, 'folder': None}
    return {'known': True, 'mapped': monster in mapping, 'folder': mapping.get(monster)}


def library_hint(lib: dict) -> str | None:
    """B.6: the hint text when PIPELINE_LIBRARY can be read and does not map the monster (None otherwise)."""
    return LIBRARY_BADGE if lib.get('known') and not lib.get('mapped') else None


# ---------------------------------------------------------------------------------------------------------------- state files

def concept_dir(mroot: Path, folder: str) -> Path:
    folder = safe_part(folder, 'concept folder')
    path = mroot / 'concepts' / folder
    if not path.is_dir():
        raise not_found(f'Unknown monster concept folder {folder}')
    return path


def state_problem(raw) -> str | None:
    if not isinstance(raw, dict):
        return 'not an object'
    if raw.get('schema') != 1:
        return f'schema {raw.get("schema")!r} is not 1'
    concepts = raw.get('concepts')
    if not isinstance(concepts, dict):
        return 'concepts is not an object'
    for key, entry in concepts.items():
        if not isinstance(key, str) or not KEY.fullmatch(key):
            return f'invalid concept key {key!r}'
        if not isinstance(entry, dict) or entry.get('stage') not in STAGES:
            return f'{key}: invalid entry'
        if entry['stage'] == 'approved' and not (isinstance(entry.get('monster'), str) and isinstance(entry.get('variant'), str)):
            return f'{key}: an approved entry needs monster and variant'
    return None


def read_state(fdir: Path, strict: bool = True) -> dict:
    """The folder's `.v2_state.json` ({schema, concepts}); absent = empty. An existing file that cannot be read is NEVER replaced by
    defaults: strict -> guard.Unreadable (HTTP 409); not strict -> the empty state with `error` set (cards show a badge)."""
    path = Path(fdir) / STATE_FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {'schema': 1, 'concepts': {}}
    except OSError as exc:
        problem = f'cannot read {STATE_FILE}: {exc}'
    else:
        try:
            data = json.loads(raw.decode('utf-8-sig'))
            problem = state_problem(data)
        except (UnicodeDecodeError, ValueError) as exc:
            problem, data = f'{STATE_FILE} is not valid JSON: {exc}', None
        if problem is None:
            return data
    if strict:
        raise guard.Unreadable(f'{Path(fdir).name}/{STATE_FILE}: {problem}')
    return {'schema': 1, 'concepts': {}, 'error': problem}


def write_state(fdir: Path, data: dict) -> None:
    clean = {'schema': 1, 'concepts': dict(sorted(data['concepts'].items(), key=lambda kv: int(KEY.fullmatch(kv[0]).group(1))))}
    problem = state_problem(clean)
    if problem:
        raise ValueError(f'refusing to write an invalid state: {problem}')
    guard.write_atomic(Path(fdir) / STATE_FILE, dump(clean))


def stage_of(state: dict, key: str) -> str:
    entry = state['concepts'].get(key)
    return entry['stage'] if isinstance(entry, dict) else 'concepts'


# ------------------------------------------------------------------------------------------------------------------ concepts

def scan_folder(fdir: Path) -> list[dict]:
    """The FINAL concepts of a folder, number order: {key, n, png, mtime, prompt, colours}. Attempts (`_attemptK`), numbers with JSON
    but no PNG and loose PNGs are not concepts. Cached by the folder's mtime."""
    fdir = Path(fdir)
    try:
        mtime = fdir.stat().st_mtime_ns
    except OSError:
        return []
    key = str(fdir)
    with _CACHE_LOCK:
        hit = _FOLDER_CACHE.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
    files: dict[str, float] = {}
    try:
        with os.scandir(fdir) as entries:
            for entry in entries:
                try:
                    if entry.is_file():
                        files[entry.name] = entry.stat().st_mtime
                except OSError:
                    continue
    except OSError:
        return []
    out = []
    for name, file_mtime in files.items():
        match = FINAL.fullmatch(name)
        if not match:
            continue
        base = name[:-len('.png')]
        out.append({'key': f'codex{match.group(1)}', 'n': int(match.group(1)), 'png': fdir / name, 'mtime': file_mtime,
                    'prompt': fdir / f'{base}.prompt.json' if f'{base}.prompt.json' in files else None,
                    'colours': fdir / f'{base}.colours.json' if f'{base}.colours.json' in files else None})
    out.sort(key=lambda c: c['n'])
    with _CACHE_LOCK:
        _FOLDER_CACHE[key] = (mtime, out)
    return out


def concept_files(fdir: Path, key: str) -> dict:
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise ValueError('Invalid concept key')
    for concept in scan_folder(fdir):
        if concept['key'] == key:
            return concept
    raise not_found(f'Unknown concept {fdir.name}/{key}')


def _find_key(obj, key: str, depth: int = 3):
    if not isinstance(obj, dict) or depth < 0:
        return None
    if key in obj:
        return obj[key]
    for value in obj.values():
        found = _find_key(value, key, depth - 1)
        if found is not None:
            return found
    return None


def concept_intent(fdir: Path, key: str) -> dict:
    """The per-N intent from the folder's set files (adjectives.json, reimagined_directions.json, local_model_descriptions.json,
    guided_round.json, ...): {file stem: text}."""
    out = {}
    for name in _names(fdir):
        if not name.endswith('.json') or name.startswith('.') or name.startswith('codex'):
            continue
        path = Path(fdir) / name
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            continue
        with _CACHE_LOCK:
            hit = _INTENT_CACHE.get(str(path))
        if hit and hit[0] == mtime:
            data = hit[1]
        else:
            data = read_obj(path)
            with _CACHE_LOCK:
                _INTENT_CACHE[str(path)] = (mtime, data)
        value = _find_key(data, key)
        if isinstance(value, dict):
            value = value.get('text') or value.get('description') or value.get('direction')
        if isinstance(value, (str, int, float)):
            out[name[:-len('.json')]] = str(value)
    return out


def concept_id(folder: str, key: str) -> str:
    return f'mconcept:{folder}/{key}'


def parse_concept_id(item_id: str) -> tuple[str, str]:
    rest = item_id.removeprefix('mconcept:')
    folder, sep, key = rest.rpartition('/')
    if not sep:
        raise ValueError('Invalid monster concept id')
    safe_part(folder, 'concept folder')
    if not KEY.fullmatch(key):
        raise ValueError('Invalid concept key')
    return folder, key


def concept_card(mroot: Path, folder: str, monster: str, concept: dict, entry: dict | None, error: str | None = None,
                 facts: dict | None = None) -> dict:
    """`facts` = {next_variant, library_hint} of the folder's monster (computed once per folder by the callers)."""
    stage = (entry or {}).get('stage', 'concepts')
    facts = facts if facts is not None else folder_facts(mroot, monster)
    badges = []
    if error:
        badges.append({'text': 'state unreadable', 'tone': 'error'})
    if concept['prompt'] is None:
        badges.append({'text': 'no prompt record', 'tone': 'warn'})
    if stage == 'trash' and (entry or {}).get('from'):
        badges.append({'text': f'from {entry["from"]}', 'tone': 'info'})
    if facts['next_variant'] == 'main' and stage in ('concepts', 'provisional', 'archive'):
        badges.append({'text': 'new monster', 'tone': 'info'})
    return {'id': concept_id(folder, concept['key']), 'type': 'monster', 'kind': 'monster', 'name': f'{monster} {concept["key"]}',
            'title': f'{monster} · {concept["key"]}', 'stage': stage, 'thumbnail': murl(mroot, concept['png'], concept['mtime']),
            'thumbnail_kind': None, 'model': None, 'races': [], 'badges': badges,
            'actions': [] if error else list(CONCEPT_ACTIONS.get(stage, [])), 'progress': None, 'monster': monster, 'folder': folder,
            'concept': concept['key'], 'next_variant': facts['next_variant'], 'library_hint': facts['library_hint']}


def folder_facts(mroot: Path, monster: str) -> dict:
    """{next_variant, library_hint} of a monster: the variant an approve would assign now and the PIPELINE_LIBRARY hint."""
    return {'next_variant': next_variant(mroot, monster), 'library_hint': library_hint(library_info(mroot, monster))}


def concept_cards(root: Path) -> dict[str, list[dict]]:
    """{stage: [card]} of every final monster concept in concepts / provisional / archive / trash (approved ones are their variant)."""
    mroot = mroot_of(root)
    tabs: dict[str, list[dict]] = {s: [] for s in SHOWN_STAGES}
    for folder in _dirs(mroot / 'concepts'):
        fdir = mroot / 'concepts' / folder
        state = read_state(fdir, strict=False)
        monster = monster_for_folder(mroot, folder)
        facts = None
        for concept in scan_folder(fdir):
            entry = state['concepts'].get(concept['key'])
            stage = entry['stage'] if isinstance(entry, dict) else 'concepts'
            if stage in tabs:
                facts = facts or folder_facts(mroot, monster)
                tabs[stage].append(concept_card(mroot, folder, monster, concept, entry, state.get('error'), facts))
    return tabs


def concept_item(root: Path, item_id: str) -> dict:
    """The item of a monster concept (the shape of the concept dialog, web/monster_concept.js): the card plus monster, folder, concept,
    image, next_variant, variant, library_hint / library, prompt (the prompt json's design, notes, ... and the full `prompt` text), intent
    ({source: text}: the folder's set files and the prompt json's adjective / direction / ...), roles (.colours.json)."""
    mroot = mroot_of(root)
    folder, key = parse_concept_id(item_id)
    fdir = concept_dir(mroot, folder)
    concept = concept_files(fdir, key)
    state = read_state(fdir, strict=False)
    entry = state['concepts'].get(key)
    monster = monster_for_folder(mroot, folder)
    lib = library_info(mroot, monster)
    card = concept_card(mroot, folder, monster, concept, entry, state.get('error'),
                        {'next_variant': next_variant(mroot, monster), 'library_hint': library_hint(lib)})
    raw_prompt = read_obj(concept['prompt']) if concept['prompt'] else {}
    colours = read_obj(concept['colours']) if concept['colours'] else {}
    prompt = {k: raw_prompt.get(k) for k in PROMPT_FIELDS if raw_prompt.get(k) not in (None, '', [])}
    intent = concept_intent(fdir, key)
    for k in INTENT_FIELDS:
        value = raw_prompt.get(k)
        if isinstance(value, (str, int, float)) and str(value).strip() and k not in intent:
            intent[k] = str(value)
    approved = isinstance(entry, dict) and entry.get('stage') == 'approved'
    return card | {'card': card, 'concept': key, 'image': card['thumbnail'], 'prompt': prompt, 'intent': intent or None,
                   'roles': [{'role': r, 'hex': v.get('hex'), 'parts': v.get('parts') or []} for r, v in (colours.get('roles') or {}).items()
                             if isinstance(v, dict)],
                   'variant': entry['variant'] if approved else None,
                   'variant_id': f'monster:{entry["monster"]}/{entry["variant"]}' if approved else None,
                   'state': entry, 'state_error': state.get('error'), 'library': lib,
                   'pipeline_exists': (mroot / 'pipeline' / monster).is_dir()}


# --------------------------------------------------------------------------------------------------------------- variants

def variant_names_path(mroot: Path, monster: str) -> Path:
    return mroot / 'pipeline' / monster / 'variant_names.json'


def read_variant_names(mroot: Path, monster: str) -> dict:
    data = read_obj(variant_names_path(mroot, monster))
    if not isinstance(data.get('variants'), dict):
        data['variants'] = {}
    if 'retired' in data and not isinstance(data['retired'], dict):
        data['retired'] = {}
    return data


def used_variant_names(mroot: Path, monster: str) -> set[str]:
    """Every variant name the monster has used: variant_names.json `variants` and `retired`, views/ and models/ folders (withdrawn and
    `*_prev_*` ones too) and the flat `<k>-front*` / `<k>-back*` files."""
    pdir = mroot / 'pipeline' / monster
    names = read_variant_names(mroot, monster)
    used = set(names['variants']) | set(names.get('retired') or {})
    for sub in ('views', 'models'):
        for name in _names(pdir / sub):
            match = WITHDRAWN_DIR.fullmatch(name)
            if match:
                used.add(match.group(1))
            elif not name.startswith('.'):
                used.add(name)
    for name in _names(pdir):
        match = re.match(r'^(.+?)-(front|back)[._]', name)
        if match:
            used.add(match.group(1))
    return used


def next_variant(mroot: Path, monster: str) -> str:
    """B.1: a brand-new monster gets `main`; one that has `main` keeps it and gets the next number from 2 (main counts as 1); a numbered
    one gets max numeric + 1 (leading numbers of `<n>-<armour>` ids count, retired numbers too); never reused."""
    pdir = mroot / 'pipeline' / monster
    used = used_variant_names(mroot, monster) if pdir.is_dir() else set()
    has_main = 'main' in used or (pdir / 'front.png').is_file()
    if not used and not has_main:
        return 'main'
    numbers = {int(m.group(1)) for name in used if (m := LEADING_NUMBER.match(name))}
    if has_main:
        numbers.add(1)
    return str(max(numbers | {0}) + 1)


def views_dir(mroot: Path, monster: str, variant: str) -> Path:
    return mroot / 'pipeline' / monster / 'views' / variant


def models_dir(mroot: Path, monster: str, variant: str) -> Path:
    return mroot / 'pipeline' / monster / 'models' / variant


def parse_monster_id(item_id: str) -> tuple[str, str]:
    rest = item_id.removeprefix('monster:')
    monster, sep, variant = rest.rpartition('/')
    if not sep:
        raise ValueError('Invalid monster id')
    return safe_part(monster, 'monster'), check_variant(variant)


def view_file(name: str) -> str:
    return f'{name}.png'


def available_views(vdir: Path) -> list[str]:
    return [n for n in VIEW_ORDER if (Path(vdir) / view_file(n)).is_file()]


def read_views_json(vdir: Path) -> dict | None:
    """views.json of a variant (None for a legacy variant without one). Malformed entries are dropped, never trusted."""
    raw = read_json(Path(vdir) / VIEWS_JSON, None)
    if not isinstance(raw, dict):
        return None
    views = []
    for entry in raw.get('views') or []:
        if isinstance(entry, dict) and entry.get('name') in VIEW_ORDER:
            views.append({'name': entry['name'], 'file': view_file(entry['name']), 'enabled': entry.get('enabled') is not False})
    frozen = raw.get('frozen') if isinstance(raw.get('frozen'), dict) else None
    return raw | {'views': views, 'unattended': raw.get('unattended') is True, 'frozen': frozen}


def ticks_of(vj: dict | None, available: list[str]) -> dict[str, bool]:
    """{view: enabled} for every available view: the draft ticks of views.json; a view not listed yet is enabled."""
    listed = {v['name']: v['enabled'] for v in (vj or {}).get('views') or []}
    return {name: listed.get(name, True) for name in available}


def write_views_json(vdir: Path, data: dict) -> None:
    """views.json = {"schema":1,"views":[{name,file,enabled}],"unattended","frozen", ...}; other keys of `data` (planned, created, by,
    concept, imported, legacy) are kept after those."""
    order = {n: i for i, n in enumerate(VIEW_ORDER)}
    views = sorted(({'name': v['name'], 'file': view_file(v['name']), 'enabled': bool(v['enabled'])} for v in data['views']),
                   key=lambda v: order[v['name']])
    out = {'schema': 1, 'views': views, 'unattended': bool(data.get('unattended')), 'frozen': data.get('frozen')}
    out |= {k: v for k, v in data.items() if k not in out}
    guard.write_atomic(Path(vdir) / VIEWS_JSON, dump(out))


def frozen_digest(frozen: dict | None) -> str | None:
    if not isinstance(frozen, dict):
        return None
    return hashlib.sha256(json.dumps(frozen, sort_keys=True).encode('utf-8')).hexdigest()[:12]


def freeze(vdir: Path, files: list[str]) -> dict:
    """The frozen view list {time, files, sha256} of `files` (front-first view order)."""
    return {'time': now(), 'files': list(files), 'sha256': {f: sha256_file(Path(vdir) / f) for f in files}}


def frozen_problem(vdir: Path, frozen: dict | None) -> str | None:
    """None when every frozen file still has its frozen bytes."""
    if not isinstance(frozen, dict) or not isinstance(frozen.get('files'), list) or not frozen['files']:
        return 'no frozen view list'
    digests = frozen.get('sha256') if isinstance(frozen.get('sha256'), dict) else {}
    for name in frozen['files']:
        path = Path(vdir) / str(name)
        if not path.is_file():
            return f'{name} is missing'
        if digests.get(name) != sha256_file(path):
            return f'{name} changed since the views were chosen'
    return None


def reduced_complete(mdir: Path) -> bool:
    red = Path(mdir) / 'reduced'
    return (red / 'manifest.json').is_file() and all((red / f'lod_error_{lv}.glb').is_file() for lv in LOD_LEVELS)


def record_of(mdir: Path) -> dict | None:
    value = read_json(Path(mdir) / 'hyper3d' / 'studio_submission.json', None)
    return value if isinstance(value, dict) else None


def conversion_started(mdir: Path) -> bool:
    """A conversion record, a downloaded GLB or a reduced folder exists (a Use enabled views would pay again: Reconvert only)."""
    mdir = Path(mdir)
    hyper = mdir / 'hyper3d'
    return (hyper / 'studio_submission.json').is_file() or any(hyper.glob('glb_*.glb')) or (mdir / 'reduced').exists()


# ------------------------------------------------------------------------------------------------------------- view tasks

def batch_dir(root: Path) -> Path:
    return Path(root) / '.codex_jobs' / '_batch'


def view_task_name(monster: str, variant: str, view: str) -> str:
    return f'{VIEW_TASK_KIND}--{slug(monster)}--{slug(variant)}--{view}'


def view_task_state(root: Path, name: str) -> dict:
    """{exists, state, attempts, job, started}: the view runner's status of a task (absent status file = pending)."""
    tasks, status = batch_dir(root) / 'tasks' / f'{name}.json', batch_dir(root) / 'status' / f'{name}.json'
    raw = read_json(status, None)
    raw = raw if isinstance(raw, dict) else {}
    jobs = raw.get('jobs') if isinstance(raw.get('jobs'), list) else []
    last = jobs[-1] if jobs and isinstance(jobs[-1], dict) else {}
    return {'name': name, 'exists': tasks.is_file(), 'state': raw.get('state') if isinstance(raw.get('state'), str) else 'pending',
            'attempts': raw.get('attempts') if isinstance(raw.get('attempts'), int) else 0, 'jobs': len(jobs), 'job': last.get('job'),
            'started': last.get('started'), 'exit': last.get('exit')}


def task_started(st: dict) -> bool:
    return st['state'] != 'pending' or st['jobs'] > 0


def view_task(root: Path, mroot: Path, monster: str, variant: str, view: str) -> dict:
    """The view-runner task of one extra view (DESIGN_PHASE5.md C): launcher monsters, no checks, 2 attempts, priority 3."""
    vdir = views_dir(mroot, monster, variant)
    colours = read_obj(vdir / 'colours.json')
    front, out = vdir / 'front.png', vdir / view_file(view)
    return {'name': view_task_name(monster, variant, view), 'priority': VIEW_PRIORITY,
            'prompt': monster_prompts.job_prompt(view, monster, variant, front, out, colours),
            'refs': [front.as_posix()], 'outputs': [out.as_posix()], 'checks': [], 'max_attempts': VIEW_MAX_ATTEMPTS,
            'launcher': 'monsters', 'monster': monster, 'variant': variant, 'view': view, 'created': now(), 'by': 'v2'}


def queue_view_tasks(root: Path, mroot: Path, monster: str, variant: str, views=EXTRA_VIEWS) -> list[str]:
    """Write the task files of the extra views whose image does not exist yet (an existing task file is kept). Returns the names written."""
    vdir = views_dir(mroot, monster, variant)
    written = []
    for view in views:
        if (vdir / view_file(view)).is_file():
            continue
        task = view_task(root, mroot, monster, variant, view)
        path = batch_dir(root) / 'tasks' / f'{task["name"]}.json'
        if path.exists():
            continue
        guard.write_atomic(path, dump(task))
        written.append(task['name'])
    return written


def view_states(root: Path, mroot: Path, monster: str, variant: str) -> dict[str, dict]:
    return {view: view_task_state(root, view_task_name(monster, variant, view)) for view in EXTRA_VIEWS}


def planned_views(vj: dict | None) -> list[str]:
    """The extra views the pick / import queued (views.json `planned`); a legacy draft plans none."""
    planned = (vj or {}).get('planned')
    return [v for v in planned if v in EXTRA_VIEWS] if isinstance(planned, list) else []


def view_waiting(vdir: Path, view: str, st: dict | None) -> bool:
    """A planned view is still coming: no image yet and its task is pending / running, or not written yet (Retry writes it)."""
    if (Path(vdir) / view_file(view)).is_file():
        return False
    return st is None or not st['exists'] or st['state'] in ('pending', 'running')


def views_phase(root: Path, mroot: Path, monster: str, variant: str, vj: dict | None = None, states: dict | None = None) -> str:
    """legacy (no views.json) | generating (a planned view is still coming) | choose (the planned views are there or failed, nothing
    frozen) | frozen."""
    vdir = views_dir(mroot, monster, variant)
    vj = read_views_json(vdir) if vj is None else vj
    if vj is None:
        return 'legacy'
    if vj.get('frozen'):
        return 'frozen'
    states = view_states(root, mroot, monster, variant) if states is None else states
    if any(view_waiting(vdir, view, states.get(view)) for view in planned_views(vj)):
        return 'generating'
    return 'choose'


# -------------------------------------------------------------------------------------------------------------- the pick

def _write_names(mroot: Path, monster: str, data: dict) -> None:
    guard.write_atomic(variant_names_path(mroot, monster), dump(data))


def _restore(path: Path, before: bytes | None) -> None:
    try:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            guard.write_atomic(path, before)
    except OSError:
        pass


def _withdrawn_name(vdir: Path) -> Path:
    base = vdir.with_name(f'.v2_withdrawn_{vdir.name}_{stamp()}')
    out, n = base, 2
    while out.exists():
        out = base.with_name(f'{base.name}_{n}')
        n += 1
    return out


def pick(root: Path, folder: str, key: str, unattended: bool = False) -> dict:
    """Approve = pick (B.1-B.6) of `concepts/<folder>/<key>-front.png` from stage concepts / provisional / archive."""
    root, mroot = Path(root), mroot_of(root)
    fdir = concept_dir(mroot, folder)
    concept = concept_files(fdir, key)
    monster = monster_for_folder(mroot, folder)
    with folder_lock(root, folder):
        state = read_state(fdir)
        origin = stage_of(state, key)
        if origin not in CONCEPT_VERBS['approve']:
            raise ValueError(f'{folder}/{key} is in {origin}; only a concept in concepts, provisional or archive can be approved')
        with monster_lock(root, monster):
            pdir = mroot / 'pipeline' / monster
            variant = next_variant(mroot, monster)
            vdir, mdir = views_dir(mroot, monster, variant), models_dir(mroot, monster, variant)
            if vdir.exists() or mdir.exists():
                raise guard.Conflict(f'{monster}/{variant} already has a views or models folder')
            tmp = pdir / 'views' / f'.v2_pick_{variant}_{uuid.uuid4().hex[:8]}'
            tmp.mkdir(parents=True)
            names_path = variant_names_path(mroot, monster)
            names_before = guard.read_bytes_or_none(names_path)
            moved = False
            try:
                shutil.copyfile(concept['png'], tmp / 'front.png')
                if concept['colours']:
                    shutil.copyfile(concept['colours'], tmp / 'colours.json')
                if concept['prompt']:
                    shutil.copyfile(concept['prompt'], tmp / 'prompts.json')
                write_views_json(tmp, {'views': [{'name': 'front', 'enabled': True}], 'unattended': bool(unattended), 'frozen': None,
                                       'planned': list(EXTRA_VIEWS), 'created': now(), 'by': 'v2',
                                       'concept': f'concepts/{folder}/{key}-front.png'})
                names = read_variant_names(mroot, monster)
                if not names_before:
                    names = {'note': 'Written by the v2 preview app: a pick (approve of a concept) adds variants[<v>] (DESIGN_PHASE5.md B); '
                                     'an undone pick moves to retired[<v>], so its number is never used again.'} | names
                names['variants'][variant] = {'was': key, 'concept': f'{key}-front.png', 'picked': now(), 'by': 'v2'}
                _write_names(mroot, monster, names)
                os.rename(tmp, vdir)
                moved = True
                entry = {'stage': 'approved', 'time': now(), 'monster': monster, 'variant': variant, 'from': origin,
                         'unattended': bool(unattended)}
                state['concepts'][key] = entry
                write_state(fdir, state)
            except BaseException:
                _restore(names_path, names_before)
                if moved and vdir.exists():
                    try:
                        os.rename(vdir, _withdrawn_name(vdir))
                    except OSError:
                        pass
                if tmp.exists():
                    shutil.rmtree(tmp, ignore_errors=True)       # our own temp folder
                raise
    warning = None
    try:
        tasks = queue_view_tasks(root, mroot, monster, variant)
    except OSError as exc:
        tasks, warning = [], f'the view tasks could not be written ({exc}); use Retry on each view'
    result = {'ok': True, 'monster': monster, 'variant': variant, 'id': f'monster:{monster}/{variant}', 'from': origin,
              'views': f'pipeline/{monster}/views/{variant}', 'tasks': tasks,
              'library': library_info(mroot, monster)}
    if warning:
        result['warning'] = warning
    return result


def withdraw(root: Path, folder: str, key: str, to: str | None = None) -> dict:
    """Undo of a pick: allowed only while no view task has started, nothing is frozen and no conversion exists. Task files are renamed
    `.json.withdrawn-<time>`, the views folder to `views/.v2_withdrawn_<v>_<time>`, the variant_names entry moves to `retired`."""
    root, mroot = Path(root), mroot_of(root)
    target = to or 'provisional'
    if target not in ('provisional', 'concepts', 'archive'):
        raise ValueError('to must be provisional, concepts or archive')
    fdir = concept_dir(mroot, folder)
    with folder_lock(root, folder):
        state = read_state(fdir)
        entry = state['concepts'].get(key)
        if stage_of(state, key) != 'approved':
            raise ValueError(f'{folder}/{key} is not approved')
        monster, variant = entry['monster'], entry['variant']
        with monster_lock(root, monster):
            names_path = variant_names_path(mroot, monster)
            names_before = guard.read_bytes_or_none(names_path)
            names = read_variant_names(mroot, monster)
            pick_rec = names['variants'].get(variant)
            if not isinstance(pick_rec, dict) or pick_rec.get('by') != 'v2' or pick_rec.get('was') != key:
                raise guard.Conflict(f'{monster}/{variant} was not picked by v2 from {key} (a pick made before v2 is undone by hand)')
            vdir, mdir = views_dir(mroot, monster, variant), models_dir(mroot, monster, variant)
            if mdir.exists():
                raise guard.Conflict(f'{monster}/{variant} has a models folder: the conversion has started')
            vj = read_views_json(vdir)
            if vj and vj.get('frozen'):
                raise guard.Conflict(f'{monster}/{variant}: the views were chosen for conversion')
            started = [st['name'] for st in view_states(root, mroot, monster, variant).values() if task_started(st)]
            if started:
                raise guard.Conflict(f'{monster}/{variant}: a view task has started ({", ".join(started)}); the pick can no longer be undone')
            renamed: list[tuple[Path, Path]] = []
            try:
                for view in EXTRA_VIEWS:
                    task = batch_dir(root) / 'tasks' / f'{view_task_name(monster, variant, view)}.json'
                    if task.exists():
                        dst = task.with_name(f'{task.name}.withdrawn-{stamp()}')
                        os.rename(task, dst)
                        renamed.append((dst, task))
                retired = dict(names.get('retired') or {})
                retired[variant] = dict(pick_rec) | {'retired': now(), 'reason': 'pick undone in v2'}
                names['variants'].pop(variant, None)
                names['retired'] = retired
                _write_names(mroot, monster, names)
                if vdir.exists():
                    dst = _withdrawn_name(vdir)
                    os.rename(vdir, dst)
                    renamed.append((dst, vdir))
                state['concepts'][key] = {'stage': target, 'time': now(), 'from': 'approved', 'withdrawn': f'{monster}/{variant}'}
                write_state(fdir, state)
            except BaseException:
                for dst, src in reversed(renamed):
                    try:
                        os.rename(dst, src)
                    except OSError:
                        pass
                _restore(names_path, names_before)
                raise
    return {'ok': True, 'monster': monster, 'variant': variant, 'to': target, 'unattended': bool(entry.get('unattended'))}


def flat_pick_files(mroot: Path, monster: str, variant: str) -> dict[str, Path]:
    """The flat files of an already-picked variant: `<k>-front.png` (+ .colours.json / .prompts.json, `<k>-back.*`), or the plain
    `front.*` / `back.*` of a single-variant (`main`) monster. {target name in views/<k>/: source path}."""
    pdir = mroot / 'pipeline' / monster
    prefix = '' if variant == 'main' else f'{variant}-'
    pairs = {'front.png': f'{prefix}front.png', 'colours.json': f'{prefix}front.colours.json', 'prompts.json': f'{prefix}front.prompts.json',
             'back.png': f'{prefix}back.png', 'back.colours.json': f'{prefix}back.colours.json', 'back.prompts.json': f'{prefix}back.prompts.json'}
    return {dst: pdir / src for dst, src in pairs.items() if (pdir / src).is_file()}


def importable(mroot: Path, monster: str) -> list[str]:
    """Variants of variant_names.json picked before v2 (flat files) that have no views/ or models/ folder yet."""
    names = read_variant_names(mroot, monster)
    out = []
    for variant in sorted(names['variants']):
        if not VARIANT.fullmatch(variant) or views_dir(mroot, monster, variant).exists() or models_dir(mroot, monster, variant).exists():
            continue
        if 'front.png' in flat_pick_files(mroot, monster, variant):
            out.append(variant)
    return out


def import_pick(root: Path, monster: str, variant: str, unattended: bool = False) -> dict:
    """The "import pick" path (G.1): `views/<k>/` from the flat files of a variant picked before v2, with the rules of B.2-B.4 and no
    renumbering. A flat back view comes along as back.png (its task is then not needed). Queues the missing extra views."""
    root, mroot = Path(root), mroot_of(root)
    monster, variant = safe_part(monster, 'monster'), check_variant(variant)
    if not (mroot / 'pipeline' / monster).is_dir():
        raise not_found(f'Unknown monster {monster}')
    with monster_lock(root, monster):
        names = read_variant_names(mroot, monster)
        rec = names['variants'].get(variant)
        if not isinstance(rec, dict):
            raise ValueError(f'{monster}/{variant} is not a picked variant (variant_names.json)')
        vdir = views_dir(mroot, monster, variant)
        if vdir.exists() or models_dir(mroot, monster, variant).exists():
            raise guard.Conflict(f'{monster}/{variant} already has a views or models folder')
        files = flat_pick_files(mroot, monster, variant)
        if 'front.png' not in files:
            raise ValueError(f'{monster}/{variant} has no flat front image')
        tmp = vdir.parent / f'.v2_pick_{variant}_{uuid.uuid4().hex[:8]}'
        tmp.mkdir(parents=True)
        names_path = variant_names_path(mroot, monster)
        names_before = guard.read_bytes_or_none(names_path)
        try:
            for dst, src in files.items():
                shutil.copyfile(src, tmp / dst)
            views = [{'name': 'front', 'enabled': True}] + ([{'name': 'back', 'enabled': True}] if 'back.png' in files else [])
            write_views_json(tmp, {'views': views, 'unattended': bool(unattended), 'frozen': None,
                                   'planned': [v for v in EXTRA_VIEWS if view_file(v) not in files], 'created': now(), 'by': 'v2',
                                   'imported': {dst: src.name for dst, src in files.items()}})
            names['variants'][variant] = dict(rec) | {'imported': now(), 'imported_by': 'v2'}
            _write_names(mroot, monster, names)
            os.rename(tmp, vdir)
        except BaseException:
            _restore(names_path, names_before)
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)               # our own temp folder
            raise
    concept = _mark_concept_of_pick(root, mroot, monster, variant, rec)
    warning = None
    try:
        tasks = queue_view_tasks(root, mroot, monster, variant)
    except OSError as exc:
        tasks, warning = [], f'the view tasks could not be written ({exc}); use Retry on each view'
    out = {'ok': True, 'monster': monster, 'variant': variant, 'id': f'monster:{monster}/{variant}', 'copied': sorted(files),
           'tasks': tasks, 'concept': concept, 'library': library_info(mroot, monster), 'undo': None}
    if warning:
        out['warning'] = warning
    return out


def _mark_concept_of_pick(root: Path, mroot: Path, monster: str, variant: str, rec: dict) -> str | None:
    """After an import: the concept the variant came from (`variant_names` `concept`) is approved with its monster / variant (best effort)."""
    concept = rec.get('concept') if isinstance(rec.get('concept'), str) else None
    match = re.fullmatch(r'(codex\d+)-front\.png', concept or '')
    folder = folder_for_monster(mroot, monster)
    if not match or not folder:
        return None
    key = match.group(1)
    fdir = mroot / 'concepts' / folder
    if not (fdir / f'{key}-front.png').is_file():
        return None
    try:
        with folder_lock(root, folder):
            state = read_state(fdir)
            entry = state['concepts'].get(key)
            if isinstance(entry, dict) and entry.get('stage') == 'approved':
                return concept_id(folder, key)
            state['concepts'][key] = {'stage': 'approved', 'time': now(), 'monster': monster, 'variant': variant,
                                      'from': stage_of(state, key), 'how': 'import'}
            write_state(fdir, state)
    except (guard.Conflict, OSError, ValueError):
        return None
    return concept_id(folder, key)


# ---------------------------------------------------------------------------------------------------------- concept verbs

def set_stage(root: Path, folder: str, key: str, allowed_from: tuple, target: str, extra: dict | None = None) -> str:
    """Move a concept between unpicked stages (a state change only). Returns the stage it came from."""
    mroot = mroot_of(root)
    fdir = concept_dir(mroot, folder)
    concept_files(fdir, key)
    with folder_lock(root, folder):
        state = read_state(fdir)
        origin = stage_of(state, key)
        if origin not in allowed_from:
            raise ValueError(f'{folder}/{key} is in {origin}; expected {" or ".join(allowed_from)}')
        state['concepts'][key] = {'stage': target, 'time': now(), 'from': origin} | (extra or {})
        write_state(fdir, state)
    return origin


def concept_action(root: Path, verb: str, item_id: str, args: dict) -> dict:
    folder, key = parse_concept_id(item_id)
    if verb not in CONCEPT_VERBS:
        raise ValueError(f'The {verb} verb does not exist for monster concepts')
    if verb == 'provisional':
        origin = set_stage(root, folder, key, CONCEPT_VERBS['provisional'], 'provisional')
        return {'ok': True, 'stage': 'provisional', 'undo': {'verb': 'unapprove', 'id': item_id, 'args': {'to': origin}}}
    if verb == 'approve':
        unattended = args.get('unattended') is True
        result = pick(root, folder, key, unattended)
        return result | {'stage': 'approved', 'undo': {'verb': 'unapprove', 'id': item_id, 'args': {'to': result['from']}}}
    if verb == 'unapprove':
        mroot = mroot_of(root)
        stage = stage_of(read_state(concept_dir(mroot, folder)), key)
        to = args.get('to') if isinstance(args.get('to'), str) else None
        if stage == 'approved':
            result = withdraw(root, folder, key, to)
            back = {'verb': 'approve', 'id': item_id} | ({'args': {'unattended': True}} if result['unattended'] else {})
            return result | {'stage': result['to'], 'undo': back}
        target = to or 'concepts'
        if target not in ('concepts', 'archive'):
            raise ValueError('to must be concepts or archive')
        set_stage(root, folder, key, ('provisional',), target)
        return {'ok': True, 'stage': target, 'undo': {'verb': 'provisional', 'id': item_id}}
    if verb == 'reject':
        origin = set_stage(root, folder, key, CONCEPT_VERBS['reject'], 'trash')
        return {'ok': True, 'stage': 'trash', 'from': origin, 'undo': {'verb': 'restore', 'id': item_id}}
    if verb == 'archive':
        set_stage(root, folder, key, CONCEPT_VERBS['archive'], 'archive')
        return {'ok': True, 'stage': 'archive', 'undo': {'verb': 'unarchive', 'id': item_id}}
    if verb == 'unarchive':
        set_stage(root, folder, key, ('archive',), 'concepts')
        return {'ok': True, 'stage': 'concepts', 'undo': {'verb': 'archive', 'id': item_id}}
    # restore: back to the stage it was rejected from (concepts by default)
    mroot = mroot_of(root)
    fdir = concept_dir(mroot, folder)
    with folder_lock(root, folder):
        state = read_state(fdir)
        entry = state['concepts'].get(key) or {}
        if stage_of(state, key) != 'trash':
            raise ValueError(f'{folder}/{key} is not in the trash')
        target = entry.get('from') if entry.get('from') in ('concepts', 'provisional', 'archive') else 'concepts'
        state['concepts'][key] = {'stage': target, 'time': now(), 'from': 'trash'}
        write_state(fdir, state)
    return {'ok': True, 'stage': target, 'undo': None}


# --------------------------------------------------------------------------------------------------------- variant verbs

def _variant_paths(root: Path, item_id: str) -> tuple[Path, str, str, Path, Path]:
    mroot = mroot_of(root)
    monster, variant = parse_monster_id(item_id)
    if not (mroot / 'pipeline' / monster).is_dir():
        raise not_found(f'Unknown monster {monster}')
    return mroot, monster, variant, views_dir(mroot, monster, variant), models_dir(mroot, monster, variant)


def _enabled_arg(raw: object) -> dict[str, bool]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError('args.enabled must map view names (front, left, back, right) to true / false')
    for name, value in raw.items():
        if name not in VIEW_ORDER or not isinstance(value, bool):
            raise ValueError('args.enabled must map view names (front, left, back, right) to true / false')
    return dict(raw)


def _draft(vdir: Path, vj: dict | None) -> dict:
    """views.json of a variant, created in memory for a legacy variant (every available view listed, attended)."""
    if vj is not None:
        return vj
    return {'views': [{'name': n, 'enabled': True} for n in available_views(vdir)], 'unattended': False, 'frozen': None,
            'created': now(), 'by': 'v2', 'legacy': True}


def set_ticks(root: Path, item_id: str, enabled: object) -> dict:
    """Verb `views`: the Enabled ticks (a draft; freezing is `convert`). Ticks after a freeze make a new draft (Reconvert uses them)."""
    mroot, monster, variant, vdir, _mdir = _variant_paths(root, item_id)
    enabled = _enabled_arg(enabled)
    if not vdir.is_dir():
        raise not_found(f'{monster}/{variant} has no views folder')
    with monster_lock(root, monster):
        vj = _draft(vdir, read_views_json(vdir))
        available = available_views(vdir)
        missing = [n for n in enabled if n not in available]
        if missing:
            raise ValueError(f'no such view image yet: {", ".join(missing)}')
        before = ticks_of(vj, available)
        ticks = before | enabled
        vj['views'] = [{'name': n, 'enabled': ticks[n]} for n in VIEW_ORDER if n in ticks]
        write_views_json(vdir, vj)
    frozen = vj.get('frozen') if isinstance(vj.get('frozen'), dict) else None
    differs = frozen is not None and [n for n in VIEW_ORDER if ticks.get(n)] != [str(f)[:-4] for f in frozen.get('files') or []]
    return {'ok': True, 'ticks': ticks, 'draft_differs': differs,
            'undo': {'verb': 'views', 'id': item_id, 'args': {'enabled': {n: before[n] for n in enabled if n in before}}}}


def _open_conversion(root: Path, monster: str, variant: str) -> dict | None:
    import monster_lane
    return monster_lane.open_task(root, monster, variant)


def convert(root: Path, item_id: str, args: dict, reconvert: bool = False) -> dict:
    """Verb `convert` ("Use enabled views") and `reconvert` (3D tab, paid): freeze views.json {time, files, sha256} and queue the conversion
    (monster_lane). `convert` refuses once a conversion record / GLB / reduced/ exists; `reconvert` moves hyper3d/ and reduced/ aside to
    `*_prev_<time>` first and needs `args.confirm: true`."""
    import monster_lane
    mroot, monster, variant, vdir, mdir = _variant_paths(root, item_id)
    if not vdir.is_dir():
        raise not_found(f'{monster}/{variant} has no views folder')
    if reconvert and args.get('confirm') is not True:
        raise ValueError('Reconvert pays for a new Hyper3D conversion (0.5 credits): send args.confirm = true')
    moved = []
    with monster_lock(root, monster):
        vj = _draft(vdir, read_views_json(vdir))
        available = available_views(vdir)
        ticks = ticks_of(vj, available)
        if 'enabled' in args:
            enabled = _enabled_arg(args['enabled'])
            missing = [n for n, on in enabled.items() if on and n not in available]
            if missing:
                raise ValueError(f'no such view image: {", ".join(missing)}')
            ticks |= {n: v for n, v in enabled.items() if n in available}
        files = [view_file(n) for n in VIEW_ORDER if ticks.get(n)]
        if not files:
            raise ValueError('enable at least one view')
        if len(files) > 4:
            raise ValueError('at most four views')
        if _open_conversion(root, monster, variant):
            raise guard.Busy(f'{monster}/{variant} has a conversion queued or running')
        if reconvert:
            choice = read_obj(mdir / CHOICE)
            if choice.get('approved') is True:
                raise guard.AlreadyApproved(f'{monster}/{variant} is approved; unapprove it first')
            if not conversion_started(mdir):
                raise ValueError(f'{monster}/{variant} has no conversion to redo; use Use enabled views')
            for sub in ('hyper3d', 'reduced'):
                src = mdir / sub
                if src.exists():
                    dst = mdir / f'{sub}_prev_{stamp()}'
                    n = 2
                    while dst.exists():
                        dst = mdir / f'{sub}_prev_{stamp()}_{n}'
                        n += 1
                    os.rename(src, dst)
                    moved.append(dst.name)
        elif conversion_started(mdir):
            raise Frozen(f'{monster}/{variant} has a conversion already; converting again is Reconvert (3D tab, paid)')
        vj['views'] = [{'name': n, 'enabled': ticks[n]} for n in VIEW_ORDER if n in ticks]
        vj['frozen'] = freeze(vdir, files)
        if reconvert:
            vj['frozen']['reconvert'] = True
            vj['frozen']['moved'] = moved
        write_views_json(vdir, vj)
    warning = None
    try:
        task = monster_lane.enqueue(root, monster, variant, vj['frozen'])
    except Exception as exc:  # noqa: BLE001  (the freeze stands; the lane tick queues it again)
        task, warning = None, f'the conversion could not be queued yet ({type(exc).__name__}: {exc}); the lane queues it on its next pass'
    out = {'ok': True, 'monster': monster, 'variant': variant, 'frozen': vj['frozen'], 'task': task, 'moved': moved, 'undo': None}
    if warning:
        out['warning'] = warning
    return out


def write_choice(root: Path, item_id: str, approved: bool, lod: object = None) -> dict:
    """3D Approve / Completed Unapprove: chosen_lod.json {lod, reduced, approved, updated} under the variant's `.pipeline_v2.lock`
    (the lock of decisions.write_monster_lod)."""
    mroot, monster, variant, _vdir, mdir = _variant_paths(root, item_id)
    if not mdir.is_dir():
        raise not_found(f'{monster}/{variant} has no models folder')
    with guard.os_lock(mdir / LOD_LOCK):
        record = read_obj(mdir / CHOICE)
        previous = {'lod': record.get('lod'), 'approved': record.get('approved') is True}
        if approved:
            if record.get('approved') is True:
                raise guard.AlreadyApproved(f'{monster}/{variant} is approved already')
            level = lod if lod is not None else record.get('lod')
            if level not in LOD_LEVELS:
                raise ValueError('choose a LOD before approving')
            if not (mdir / 'reduced' / f'lod_error_{level}.glb').is_file():
                raise ValueError('that LOD has no file')
            record |= {'lod': level, 'reduced': f'reduced/lod_error_{level}.glb', 'approved': True}
        else:
            if record.get('approved') is not True:
                raise ValueError(f'{monster}/{variant} is not approved')
            record['approved'] = False
        record['updated'] = datetime.now(timezone.utc).isoformat()
        guard.write_atomic(mdir / CHOICE, json.dumps(record, indent=1).encode('utf-8'))
    return {'ok': True, 'lod': record.get('lod'), 'approved': record['approved'], 'previous': previous}


def view_retry(root: Path, item_id: str, view: object) -> dict:
    """Verb `view_retry` (args.view): a FAILED view task goes back to pending with attempts 0 (its jobs kept); a missing task file (never
    written) is written. Refused while the view image exists."""
    mroot, monster, variant, vdir, _mdir = _variant_paths(root, item_id)
    if view not in EXTRA_VIEWS:
        raise ValueError(f'view must be one of {list(EXTRA_VIEWS)}')
    if not (vdir / 'front.png').is_file():
        raise not_found(f'{monster}/{variant} has no front view')
    if (vdir / view_file(view)).is_file():
        raise ValueError(f'{view}.png exists already')
    name = view_task_name(monster, variant, view)
    st = view_task_state(root, name)
    if not st['exists']:
        written = queue_view_tasks(root, mroot, monster, variant, (view,))
        return {'ok': True, 'name': name, 'state': 'pending', 'written': bool(written), 'undo': None}
    if st['state'] != 'failed':
        raise ValueError(f'{name} is {st["state"]}; only a failed view task can be retried')
    path = batch_dir(root) / 'status' / f'{name}.json'
    status = read_obj(path)
    before = status.get('attempts')
    status.update(state='pending', attempts=0)
    guard.write_atomic(path, dump(status))
    return {'ok': True, 'name': name, 'state': 'pending', 'attempts_before': before, 'undo': None}


def variant_action(root: Path, verb: str, item_id: str, args: dict) -> dict:
    if verb not in MONSTER_VERBS:
        raise ValueError(f'The {verb} verb does not exist for monster variants')
    if verb == 'import':
        monster, variant = parse_monster_id(item_id)
        return import_pick(root, monster, variant, args.get('unattended') is True)
    if verb == 'views':
        return set_ticks(root, item_id, args.get('enabled'))
    if verb in ('convert', 'reconvert'):
        return convert(root, item_id, args, reconvert=verb == 'reconvert')
    if verb == 'view_retry':
        return view_retry(root, item_id, args.get('view'))
    if verb == 'approve':
        result = write_choice(root, item_id, True, args.get('lod'))
        return result | {'stage': 'completed', 'undo': {'verb': 'unapprove', 'id': item_id}}
    # unapprove: completed -> 3d; a variant v2 picked whose views have not started -> the pick is undone
    mroot, monster, variant, _vdir, mdir = _variant_paths(root, item_id)
    if read_obj(mdir / CHOICE).get('approved') is True:
        result = write_choice(root, item_id, False)
        return result | {'stage': '3d', 'undo': {'verb': 'approve', 'id': item_id, 'args': {'lod': result['lod']}}}
    rec = read_variant_names(mroot, monster)['variants'].get(variant)
    folder = folder_for_monster(mroot, monster)
    if isinstance(rec, dict) and rec.get('by') == 'v2' and isinstance(rec.get('was'), str) and folder:
        return concept_action(root, 'unapprove', concept_id(folder, rec['was']), args)
    raise ValueError(f'{monster}/{variant} is not approved, and its pick was not made by v2')


def perform(root: Path, data: object) -> dict:
    """POST /api/v2/action for `mconcept:<Folder>/<codexN>` and `monster:<Monster>/<variant>` ids (called by actions.perform BEFORE its
    `NAME` check: these ids contain spaces and a slash). Body {"id", "verb", "args"?}; no `rev` (monster state is not versioned)."""
    if not isinstance(data, dict):
        raise ValueError('body must be an object')
    item_id, verb = data.get('id'), data.get('verb')
    args = data.get('args') if isinstance(data.get('args'), dict) else {}
    if not isinstance(item_id, str) or not isinstance(verb, str):
        raise ValueError('id and verb are required')
    kind = item_id.split(':', 1)[0]
    if (kind, verb) not in ALLOWED:
        raise ValueError(f'The {verb} verb does not exist for {kind} items')
    try:
        if kind == 'mconcept':
            return concept_action(root, verb, item_id, args)
        return variant_action(root, verb, item_id, args)
    finally:
        invalidate()


def handles(item_id: object) -> bool:
    return isinstance(item_id, str) and (item_id.startswith('mconcept:') or item_id.startswith('monster:'))


# --------------------------------------------------------------------------------------------------------- variant cards

def monster_stage(raw: dict) -> str:
    """approved -> completed; reduced LODs -> 3d; otherwise approved (in progress) (the phase 4 catalog rule)."""
    if raw.get('approved'):
        return 'completed'
    return '3d' if raw.get('lods') else 'approved'


def _task_view(task: dict | None) -> dict | None:
    if not task:
        return None
    return {'id': task['id'], 'state': task['state'], 'attempts': task['attempts'], 'error': task.get('error'),
            'started': task.get('started'), 'finished': task.get('finished'), 'not_before': task.get('not_before'),
            'run_now': bool((task.get('args') or {}).get('run_now')), 'key': task['key'],
            'verdict': (task.get('result') or {}).get('last_json') if isinstance(task.get('result'), dict) else None}


VERDICT_STEP = {'check': 'submit', 'driver': 'submit', 'submit': 'submit', 'collect': 'collect', 'reduce': 'reduce', 'done': 'reduce'}


def record_step(mdir: Path, record: dict | None) -> str:
    """The conversion step a record is at: submit (no record) | preview (submitted, the free preview, nothing paid) | collect (paid, not
    exported) | reduce (exported GLB)."""
    import monster_lane
    if record is None:
        return 'submit'
    if record.get('status') == 'exported' and any((Path(mdir) / 'hyper3d').glob('glb_*.glb')):
        return 'reduce'
    return 'collect' if monster_lane.has_paid(record) else 'preview'


def conversion_info(root: Path, mroot: Path, monster: str, variant: str, task: dict | None, frozen: dict | None = None,
                    with_gate: bool = True) -> dict | None:
    """`item.conversion` (web/monster_views.js): None before anything was chosen / converted, else
    {task_id, state: queued | waiting | running | done | failed, step: submit | preview | collect | reduce, waiting_reason (held | paused |
    user_present | browser_busy | a text) while waiting, paid, credits, error, attempts, run_now, record {status, stage, pending_action,
    credits_committed, why, model_url, images_uploaded, actions}, screenshots [urls, newest first], reduced, started, task}."""
    import monster_lane
    mdir = models_dir(mroot, monster, variant)
    record = record_of(mdir)
    reduced = reduced_complete(mdir)
    if task is None and frozen is None and record is None:
        return None
    shots = []
    hyper = mdir / 'hyper3d'
    try:
        pngs = sorted((p for p in hyper.iterdir() if p.suffix.lower() == '.png'), key=lambda p: p.stat().st_mtime, reverse=True)[:6]
        shots = [u for u in (murl(mroot, p) for p in pngs) if u]
    except OSError:
        pass
    rec = None
    if record is not None:
        rec = {k: record.get(k) for k in ('status', 'stage', 'pending_action', 'credits_committed', 'why', 'model_url', 'images_uploaded')}
        rec['actions'] = sorted((record.get('actions') or {}).keys()) if isinstance(record.get('actions'), dict) else []
    view = _task_view(task)
    paid = monster_lane.has_paid(record)
    credits = record.get('credits_committed') if record else None
    credits = credits if isinstance(credits, (int, float)) and not isinstance(credits, bool) and credits > 0 else \
        (monster_lane.CREDITS_PER_MODEL if paid else None)
    step = 'reduce' if reduced else record_step(mdir, record)
    error, waiting = None, None
    if view is None:
        if reduced:
            state = 'done'
        elif frozen is not None:
            state, waiting = 'waiting', 'the conversion lane queues it at its next pass'
        else:
            state, error = 'failed', f'a Hyper3D record exists (status {(record or {}).get("status")!r}) but no conversion is queued'
    elif view['state'] in ('starting', 'running', 'orphan'):
        state = 'running'
    elif view['state'] == 'done':
        state = 'done'
    elif view['state'] in ('failed', 'cancelled'):
        state = 'failed'
        verdict = view.get('verdict') or {}
        error = verdict.get('error') or view.get('error') or view['state']
        if view['state'] == 'cancelled':
            error = f'cancelled: {error}'
        step = VERDICT_STEP.get(verdict.get('step'), step)
    else:                                                       # queued: waiting for its gate, its backoff or the running one
        state = 'queued'
        if (view.get('not_before') or 0) > time.time():
            state = 'waiting'
            waiting = 'browser_busy' if monster_lane.BUSY_TEXT in str(view.get('error') or '').lower() else (view.get('error') or 'retrying soon')
        elif with_gate:
            g = monster_lane.gate(root, task.get('args'))
            if not g['open']:
                state, waiting = 'waiting', g['code'] or g['reason']
            elif monster_lane.lane_tasks(root, state=monster_lane.taskqueue.ACTIVE, limit=5):
                state, waiting = 'waiting', 'another monster conversion is running'
    return {'task_id': view['id'] if view else None, 'state': state, 'step': step, 'waiting_reason': waiting, 'paid': paid, 'credits': credits,
            'error': error, 'attempts': view['attempts'] if view else 0, 'run_now': bool(view and view['run_now']), 'record': rec,
            'screenshots': shots, 'reduced': reduced, 'started': conversion_started(mdir), 'task': view}


def _tasks_by_variant(root: Path) -> dict[str, dict]:
    """{"<monster>/<variant>": newest lane task} (one query)."""
    try:
        import monster_lane
        return monster_lane.newest_tasks(root)
    except Exception:  # noqa: BLE001  (a damaged queue never breaks the cards)
        return {}


def variant_card(root: Path, mroot: Path, raw: dict, tasks: dict) -> dict:
    """The card of a variant (monsters.cards raw) with the phase 5 badges: views a/b, Choose views, conversion state, library hint."""
    stage = monster_stage(raw)
    lods = raw.get('lods') or {}
    lod = raw.get('lod') if raw.get('lod') in lods else ('0p4' if '0p4' in lods else next(iter(lods), None))
    monster, variant = raw['monster'], raw['variant']
    vdir = views_dir(mroot, monster, variant)
    badges = [{'text': str(raw.get('status') or 'image only'), 'tone': 'info'}]
    actions: list[str] = []
    if stage == 'approved':
        vj = read_views_json(vdir)
        states = view_states(root, mroot, monster, variant) if vj is not None else {}
        phase = views_phase(root, mroot, monster, variant, vj, states)
        task = tasks.get(f'{monster}/{variant}')
        if phase == 'generating':
            planned = planned_views(vj)
            done = sum(1 for v in ('front', *planned) if (vdir / view_file(v)).is_file())
            badges.append({'text': f'views {done}/{1 + len(planned)}', 'tone': 'info'})
        failed = [v for v in planned_views(vj) if (st := states.get(v)) and st['exists'] and st['state'] == 'failed'
                  and not (vdir / view_file(v)).is_file()]
        if failed:
            badges.append({'text': f'view failed {len(failed)}', 'tone': 'error'})
        if phase == 'choose':
            badges.append({'text': 'Choose views', 'tone': 'warn'})
        if vj and vj.get('unattended') and phase != 'frozen':
            badges.append({'text': 'convert straight away', 'tone': 'info'})
        if task:
            text = {'queued': 'conversion queued', 'starting': 'converting', 'running': 'converting', 'orphan': 'converting',
                    'failed': 'conversion failed', 'done': 'converted', 'cancelled': 'conversion cancelled'}.get(task['state'], task['state'])
            tone = 'error' if task['state'] == 'failed' else 'warn' if task['state'] == 'cancelled' else 'info'
            badges.append({'text': text, 'tone': tone})
        elif phase == 'frozen':
            badges.append({'text': 'conversion not queued', 'tone': 'warn'})
        if raw.get('importable'):
            badges.append({'text': 'picked, not imported', 'tone': 'warn'})
            actions = ['import']
    elif stage == '3d':
        if raw.get('lod') in lods:
            actions = ['approve']
    else:
        badges.append({'text': 'approved', 'tone': 'ok'})
        actions = ['unapprove']
    if sections_stale(models_dir(mroot, monster, variant)):
        badges.append({'text': 'sections out of date', 'tone': 'warn'})
    hint = library_hint(library_info(mroot, monster))
    if hint:
        badges.append({'text': hint, 'tone': 'warn'})
    return {'id': raw['id'], 'type': 'monster', 'kind': 'monster', 'name': raw['name'], 'title': raw['name'], 'stage': stage,
            'thumbnail': raw.get('image'), 'thumbnail_kind': None, 'model': lods[lod]['url'] if lod else None, 'races': [],
            'badges': badges, 'actions': actions, 'progress': None, 'monster': monster, 'variant': variant, 'library_hint': hint}


def _newest_mtime(folder: Path) -> float | None:
    try:
        with os.scandir(folder) as entries:
            times = [e.stat().st_mtime for e in entries if e.is_file()]
    except OSError:
        return None
    return max(times) if times else None


def sections_stale(mdir: Path) -> bool:
    """`sections/` (monsters' split_monster_sections.py) is older than the current `reduced/` (Reconvert leaves sections/ in place): its
    newest file predates reduced/manifest.json."""
    sections = _newest_mtime(Path(mdir) / 'sections')
    if sections is None:
        return False
    try:
        return sections < (Path(mdir) / 'reduced' / 'manifest.json').stat().st_mtime
    except OSError:
        return False


def importable_raws(mroot: Path) -> list[dict]:
    """Raw cards of the variants picked before v2 that were never imported (flat files only): stage approved, action import."""
    out = []
    for monster in pipeline_monsters(mroot):
        for variant in importable(mroot, monster):
            files = flat_pick_files(mroot, monster, variant)
            out.append({'id': f'monster:{monster}/{variant}', 'kind': 'monster', 'name': f'{monster} {variant}', 'monster': monster,
                        'variant': variant, 'status': 'picked', 'image': murl(mroot, files['front.png']), 'lods': {}, 'lod': None,
                        'approved': False, 'importable': True, 'roles': [], 'record': {}})
    return out


def variant_raws(root: Path) -> list[dict]:
    mroot = mroot_of(root)
    return list(monsters.cards(Path(root))) + importable_raws(mroot)


def variant_cards(root: Path) -> dict[str, list[dict]]:
    """{stage: [card]} of every monster variant (approved / 3d / completed), including flat picks to import."""
    mroot = mroot_of(root)
    tasks = _tasks_by_variant(root)
    tabs: dict[str, list[dict]] = {'approved': [], '3d': [], 'completed': []}
    for raw in variant_raws(root):
        card = variant_card(root, mroot, raw, tasks)
        tabs[card['stage']].append(card)
    return tabs


def state_cards(root: Path) -> dict[str, list[dict]]:
    """HOOK (catalog._build_state): every monster card per tab: concept cards (concepts / provisional / archive / trash) and variant cards
    (approved / 3d / completed). Replaces the `monsters.cards` loop."""
    tabs = concept_cards(root)
    for tab, cards in variant_cards(root).items():
        tabs.setdefault(tab, []).extend(cards)
    return tabs


def view_task_info(root: Path, st: dict | None) -> dict | None:
    """`views[].task` of the item: the runner's status in the UI's words (queued | running | done | failed), the attempt limit of the task
    file and, for a failed task, the reason (the runner records no text: the last job and its exit)."""
    if not st or not st['exists']:
        return None
    task_file = read_obj(batch_dir(root) / 'tasks' / f'{st["name"]}.json')
    state = RUNNER_STATE.get(st['state'], st['state'])
    error = f'no image written (last job {st["job"]}, exit {st["exit"]})' if state == 'failed' else None
    return {'name': st['name'], 'state': state, 'attempts': st['attempts'], 'max_attempts': task_file.get('max_attempts'), 'error': error,
            'job': st['job'], 'started': st['started']}


def variant_item(root: Path, item_id: str) -> dict:
    """The item of a monster variant, whatever its stage (web/monster_views.js, web/monster3d.js): the raw card plus views [{name, file,
    exists, url, enabled, planned, task, retry}] (always front, left, back, right), phase (legacy | generating | choose | frozen | import),
    legacy, importable, unattended, frozen, draft_differs, conversion (conversion_info), library_hint / library and the pick record."""
    mroot = mroot_of(root)
    monster, variant = parse_monster_id(item_id)
    raw = next((r for r in variant_raws(root) if r['id'] == item_id), None)
    if raw is None:
        raise not_found(f'Unknown monster {item_id}')
    tasks = _tasks_by_variant(root)
    card = variant_card(root, mroot, raw, tasks)
    vdir = views_dir(mroot, monster, variant)
    vj = read_views_json(vdir)
    available = available_views(vdir)
    ticks = ticks_of(vj, available)
    states = view_states(root, mroot, monster, variant)
    planned = planned_views(vj)
    importing = bool(raw.get('importable'))
    views = []
    for name in VIEW_ORDER:
        path = vdir / view_file(name)
        st = states.get(name)
        failed = bool(st and st['exists'] and st['state'] == 'failed')
        unwritten = name in planned and not (st and st['exists'])
        url = murl(mroot, path) or (raw.get('image') if importing and name == 'front' else None)
        views.append({'name': name, 'file': view_file(name), 'exists': path.is_file(), 'url': url,
                      'enabled': ticks.get(name, False), 'planned': name in planned, 'task': view_task_info(root, st),
                      'retry': name != 'front' and not importing and not path.is_file() and (failed or unwritten)})
    frozen = (vj or {}).get('frozen')
    frozen = frozen | {'names': [str(f)[:-4] for f in frozen.get('files') or []]} if isinstance(frozen, dict) else None
    chosen = [n for n in VIEW_ORDER if ticks.get(n)]
    rec = read_variant_names(mroot, monster)['variants'].get(variant)
    phase = 'import' if importing else views_phase(root, mroot, monster, variant, vj, states)
    lib = library_info(mroot, monster)
    return raw | {'stage': card['stage'], 'card': card,
                  'views': views, 'views_json': vj, 'phase': phase, 'legacy': phase == 'legacy', 'importable': importing,
                  'unattended': bool((vj or {}).get('unattended')), 'frozen': frozen,
                  'draft_differs': frozen is not None and chosen != frozen['names'],
                  'conversion': conversion_info(root, mroot, monster, variant, tasks.get(f'{monster}/{variant}'), frozen),
                  'library': lib, 'library_hint': library_hint(lib), 'pick': rec if isinstance(rec, dict) else None}


def item(root: Path, item_id: str) -> dict:
    """HOOK (catalog.item): the item of an `mconcept:` or `monster:` id."""
    if item_id.startswith('mconcept:'):
        return concept_item(root, item_id)
    if item_id.startswith('monster:'):
        return variant_item(root, item_id)
    raise ValueError('not a monster id')


# ----------------------------------------------------------------------------------------------------------- auto freeze

def auto_freeze(root: Path) -> list[str]:
    """HOOK (orchestrator tick, through monster_lane.tick): every variant with views.json `unattended: true`, nothing frozen, and no view
    task pending / running is frozen with every available view (D, "unattended"). Returns the `<monster>/<variant>` frozen now."""
    root, mroot = Path(root), mroot_of(root)
    done = []
    for monster in pipeline_monsters(mroot):
        for variant in _dirs(mroot / 'pipeline' / monster / 'views'):
            vdir = views_dir(mroot, monster, variant)
            vj = read_views_json(vdir)
            if not vj or not vj.get('unattended') or vj.get('frozen'):
                continue
            if views_phase(root, mroot, monster, variant, vj) != 'choose':
                continue
            mdir = models_dir(mroot, monster, variant)
            if conversion_started(mdir):
                continue
            try:
                with monster_lock(root, monster):
                    vj = read_views_json(vdir)
                    if not vj or vj.get('frozen') or not vj.get('unattended'):
                        continue
                    files = [view_file(n) for n in available_views(vdir)][:4]
                    if not files:
                        continue
                    vj['views'] = [{'name': n[:-4], 'enabled': True} for n in files]
                    vj['frozen'] = freeze(vdir, files) | {'auto': True}
                    write_views_json(vdir, vj)
                done.append(f'{monster}/{variant}')
            except (guard.Conflict, OSError):
                continue
    return done


def frozen_variants(root: Path) -> list[dict]:
    """Every variant whose views.json has a frozen list: {monster, variant, frozen, digest}."""
    mroot = mroot_of(root)
    out = []
    for monster in pipeline_monsters(mroot):
        for variant in _dirs(mroot / 'pipeline' / monster / 'views'):
            vj = read_views_json(views_dir(mroot, monster, variant))
            if vj and vj.get('frozen'):
                out.append({'monster': monster, 'variant': variant, 'frozen': vj['frozen'], 'digest': frozen_digest(vj['frozen'])})
    return out


# ------------------------------------------------------------------------------------------------------------- migration

def _flat_fronts(mroot: Path, monster: str) -> dict[str, list[str]]:
    """{sha256: [variant]} of the flat `<k>-front.png` (and `front.png` = main) of a pipeline monster, plus views/<v>/front.png."""
    pdir = mroot / 'pipeline' / monster
    out: dict[str, list[str]] = {}
    for name in _names(pdir):
        match = re.fullmatch(r'(.+)-front\.png', name)
        variant = match.group(1) if match else ('main' if name == 'front.png' else None)
        if variant:
            try:
                out.setdefault(sha256_file(pdir / name), []).append(variant)
            except OSError:
                continue
    for variant in _dirs(pdir / 'views'):
        front = pdir / 'views' / variant / 'front.png'
        if front.is_file():
            try:
                out.setdefault(sha256_file(front), []).append(variant)
            except OSError:
                continue
    return out


def _best_variant(variants: list[str]) -> str:
    """Of several variants that came from one concept, the base one (no `-<armour>` suffix), else `main`, else the first."""
    plain = sorted(v for v in variants if '-' not in v)
    if plain:
        return sorted(plain, key=lambda v: (not v.isdigit(), int(v) if v.isdigit() else 0, v))[0]
    return 'main' if 'main' in variants else sorted(variants)[0]


def migration_plan(root: Path) -> dict:
    """HOOK (migrate.py --monsters, dry run): what the migration would write. Every final concept that exists now and is not in its folder's
    state file goes to `archive`, unless it is already picked: `variant_names.json` `variants[k].concept == "codexN-front.png"`, a
    sha256-identical flat `<k>-front.png` (or views/<v>/front.png), or an explicit override (Goblin Shaman codex4 -> main); picked
    concepts become `approved` with their monster and variant. Concepts the state file already lists are kept as they are. Reads only."""
    mroot = mroot_of(root)
    folders = []
    totals = {'folders': 0, 'finals': 0, 'archive': 0, 'approved': 0, 'kept': 0, 'unreadable': 0}
    for folder in _dirs(mroot / 'concepts'):
        fdir = mroot / 'concepts' / folder
        monster = monster_for_folder(mroot, folder)
        state = read_state(fdir, strict=False)
        names = read_variant_names(mroot, monster) if (mroot / 'pipeline' / monster).is_dir() else {'variants': {}}
        by_concept: dict[str, list[str]] = {}
        for variant, rec in names['variants'].items():
            if isinstance(rec, dict) and isinstance(rec.get('concept'), str):
                by_concept.setdefault(rec['concept'], []).append(variant)
        shas = _flat_fronts(mroot, monster) if (mroot / 'pipeline' / monster).is_dir() else {}
        concepts = scan_folder(fdir)
        entry = {'folder': folder, 'monster': monster, 'pipeline': (mroot / 'pipeline' / monster).is_dir(), 'finals': len(concepts),
                 'archive': [], 'approved': [], 'kept': [], 'error': state.get('error')}
        totals['folders'] += 1
        totals['finals'] += len(concepts)
        if state.get('error'):
            totals['unreadable'] += 1
            folders.append(entry)
            continue
        for concept in concepts:
            key = concept['key']
            if key in state['concepts']:
                entry['kept'].append(key)
                continue
            how, variant = None, None
            if (folder, key) in MIGRATION_OVERRIDES:
                monster_o, variant = MIGRATION_OVERRIDES[(folder, key)]
                how = 'override'
                if monster_o != monster:
                    variant = None
            if variant is None and f'{key}-front.png' in by_concept:
                variant, how = _best_variant(by_concept[f'{key}-front.png']), 'variant_names'
            if variant is None and shas:
                try:
                    found = shas.get(sha256_file(concept['png']))
                except OSError:
                    found = None
                if found:
                    variant, how = _best_variant(found), 'sha256'
            if variant is not None:
                entry['approved'].append({'key': key, 'monster': monster, 'variant': variant, 'how': how})
            else:
                entry['archive'].append(key)
        totals['archive'] += len(entry['archive'])
        totals['approved'] += len(entry['approved'])
        totals['kept'] += len(entry['kept'])
        folders.append(entry)
    imports = [{'monster': m, 'variant': v} for m in pipeline_monsters(mroot) for v in importable(mroot, m)]
    return {'time': now(), 'monster_root': str(mroot), 'totals': totals, 'folders': folders, 'importable': imports}


def apply_migration(root: Path, plan: dict | None = None) -> dict:
    """HOOK (migrate.py --monsters --apply): write the plan into the state files (per folder, under its lock, re-checked: a concept that the
    state file lists by now is left alone). Never moves a file. Returns {written: {folder: n}, totals}."""
    plan = plan or migration_plan(root)
    mroot = mroot_of(root)
    written: dict[str, int] = {}
    stamp_now = now()
    for entry in plan['folders']:
        if entry.get('error') or not (entry['archive'] or entry['approved']):
            continue
        fdir = mroot / 'concepts' / entry['folder']
        with folder_lock(root, entry['folder']):
            state = read_state(fdir)
            n = 0
            for key in entry['archive']:
                if key not in state['concepts']:
                    state['concepts'][key] = {'stage': 'archive', 'time': stamp_now, 'from': 'concepts', 'migrated': True}
                    n += 1
            for rec in entry['approved']:
                if rec['key'] not in state['concepts']:
                    state['concepts'][rec['key']] = {'stage': 'approved', 'time': stamp_now, 'monster': rec['monster'],
                                                     'variant': rec['variant'], 'migrated': True, 'how': rec['how']}
                    n += 1
            if n:
                write_state(fdir, state)
        written[entry['folder']] = n
    return {'ok': True, 'written': written, 'totals': plan['totals'], 'importable': plan['importable']}


def migration_markdown(plan: dict) -> str:
    t = plan['totals']
    lines = [f'# Monster concept migration ({plan["time"]})', '', f'Monster root: `{plan["monster_root"]}`', '',
             f'{t["folders"]} folders, {t["finals"]} final concepts: **{t["archive"]} to archive**, **{t["approved"]} approved (picked)**, '
             f'{t["kept"]} already in a state file (kept), {t["unreadable"]} unreadable state file(s).', '',
             '| Folder | Monster | Finals | Archive | Approved | Kept |', '| --- | --- | --- | --- | --- | --- |']
    for f in plan['folders']:
        picked = ', '.join(f'{a["key"]}->{a["variant"]} ({a["how"]})' for a in f['approved']) or '-'
        lines.append(f'| {f["folder"]} | {f["monster"]}{"" if f["pipeline"] else " (new)"} | {f["finals"]} | {len(f["archive"])} | '
                     f'{picked} | {len(f["kept"])}{" ERROR " + f["error"] if f.get("error") else ""} |')
    lines += ['', '## Picked before v2, not imported (Import in the Approved tab)', '']
    lines += [f'- {i["monster"]} {i["variant"]}' for i in plan['importable']] or ['- none']
    return '\n'.join(lines) + '\n'


def main(argv: list[str] | None = None) -> int:
    """`python tools/preview/v2/monsters_v2.py migrate [--root R] [--apply]`: dry run (report files only) or apply."""
    import argparse
    parser = argparse.ArgumentParser(description=main.__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    mig = sub.add_parser('migrate')
    mig.add_argument('--root', type=Path, default=HERE.parents[2])
    mig.add_argument('--apply', action='store_true')
    mig.add_argument('--out', type=Path, default=Path('.codex_jobs') / '_app' / 'monster_migration_report')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    plan = migration_plan(root)
    out = args.out if args.out.is_absolute() else root / args.out
    guard.write_atomic(out.with_suffix('.json'), dump(plan))
    guard.write_atomic(out.with_suffix('.md'), migration_markdown(plan).encode('utf-8'))
    print(json.dumps(plan['totals']))
    if args.apply:
        result = apply_migration(root, plan)
        print(json.dumps({'written': result['written']}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
