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
import stat
import sys
import threading
import time
from urllib.parse import quote
import uuid

HERE = Path(__file__).resolve().parent
for _p in (HERE.parents[1], HERE.parent, HERE):                 # tools/ (png_check), tools/preview, tools/preview/v2
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import guard  # noqa: E402
import monsters  # noqa: E402  (tools/preview/monsters.py: monsters_root, LOD_LEVELS, CHOICE, cards)
import monster_prompts  # noqa: E402
import png_check  # noqa: E402  (tools/png_check.py: the ONE technical view validator, shared with the view runner; finding 15)

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
MONSTER_VERBS = ('convert', 'views', 'approve', 'unapprove', 'reconvert', 'view_retry', 'view_repair', 'import')
ALLOWED = frozenset({('mconcept', v) for v in CONCEPT_VERBS} | {('monster', v) for v in MONSTER_VERBS})
PROMPT_FIELDS = ('design', 'notes', 'colour_variant', 'job', 'date', 'attempts', 'size', 'mode', 'prompt')   # item.prompt (the dialog)
INTENT_FIELDS = ('adjective', 'direction', 'local_description', 'user_guidance', 'set')                     # prompt json -> item.intent
RUNNER_STATE = {'pending': 'queued', 'launching': 'running', 'running': 'running', 'done': 'done', 'failed': 'failed',
                'cancelled': 'cancelled'}
TERMINAL_VIEW_STATES = ('done', 'failed', 'cancelled')              # a view task that will not change by itself any more
# Windows reserved device names (with or without an extension) are never a monster / variant / folder name (phase 5 review F13)
RESERVED = re.compile(r'^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$)(\..*)?$', re.IGNORECASE)
ARMOUR_VARIANT = re.compile(r'^\d+-.+$')                            # War Horse `<n>-<armour>` ids: manual, never imported (B.1)
FLAT_SUBFOLDERS = ('first round',)                                  # War Horse base picks keep their flat fronts here (F15)
REDUCE_OK = 'v2_reduce_ok.json'                                     # models/<v>/reduced/: the lane's success record (F12)
RECONVERT_JOURNAL = '.v2_reconvert.json'                            # models/<v>/: the journal of a Reconvert's moves (F6)
VIEW_VALIDATE = 'rgba_png'                                          # view task field of tools/view_batch.py (F17)
DISPOSITION_STATES = ('cancelled', 'failed')                        # views.json conversion[<generation>].state
GENERATION = re.compile(r'^[0-9a-f]{12}$')                          # a frozen digest (frozen_digest)
_CACHE_LOCK = threading.Lock()


class Frozen(guard.Conflict):
    """Use enabled views on a variant whose frozen views were converted already: converting again is Reconvert (3D tab, paid). HTTP 409."""
    code = 'frozen'


class Missing(guard.Conflict):
    """A view the request names is not (or no longer) a valid image of the variant (HTTP 409 `missing`; phase 5 review F8)."""
    code = 'missing'


_FOLDER_CACHE: dict[str, tuple] = {}
_INTENT_CACHE: dict[str, tuple] = {}
_LIBRARY_CACHE: dict[str, tuple] = {}
_PNG_CACHE: dict[str, tuple] = {}
_SHA_CACHE: dict[str, tuple] = {}


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


def read_strict(path: Path, what: str) -> dict | None:
    """A JSON OBJECT file on a mutation path (recheck-before-cutover finding 11): None only when the file does not exist. A file that
    exists but cannot be read, is not JSON or is not an object raises guard.Unreadable (HTTP 409): it is never treated as missing, so
    nothing overwrites or removes it."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise guard.Unreadable(f'{what}: cannot be read: {exc}') from None
    try:
        value = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise guard.Unreadable(f'{what}: not valid JSON ({exc}); it is kept as it is, repair or remove it by hand') from None
    if not isinstance(value, dict):
        raise guard.Unreadable(f'{what}: not a JSON object; it is kept as it is, repair or remove it by hand')
    return value


def dump(data) -> bytes:
    return (json.dumps(data, indent=2) + '\n').encode('utf-8')


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def slug(name: str) -> str:
    """A task / lock name part: [A-Za-z0-9_-] (spaces and the rest become `_`). NOT an identity: two names can share a slug, so every
    name built from it also carries `canon_digest` (phase 5 review F13)."""
    return re.sub(r'[^A-Za-z0-9_-]', '_', name).strip('_') or 'x'


def canon(name: str) -> str:
    """The canonical identity of a monster / variant / folder name on Windows (case-insensitive file system): casefolded."""
    return name.casefold()


def canon_digest(*names: str) -> str:
    """8 hex of the canonical identity of a name (or of a monster/variant pair): the part of a task / lock / journal name that makes it
    unique where the slug is not (`Giant Rat` and `Giant_Rat` share a slug, never a digest)."""
    return hashlib.sha1('/'.join(canon(n) for n in names).encode('utf-8')).hexdigest()[:8]


def safe_part(name: object, what: str) -> str:
    """A folder name used as ONE path segment: no separators, no `..`, no leading dot, no reserved characters, no trailing dot or space,
    no Windows device name (CON, NUL, COM1, ...)."""
    if not isinstance(name, str) or not name or name != name.strip() or name in ('.', '..') or name.startswith('.') or \
            any(c in FORBIDDEN_CHARS or ord(c) < 32 for c in name) or name.endswith(('.', ' ')) or len(name) > 120 or \
            RESERVED.match(name):
        raise ValueError(f'Invalid {what}')
    return name


def check_variant(variant: object) -> str:
    if not isinstance(variant, str) or not VARIANT.fullmatch(variant) or '..' in variant or variant.endswith('.') or \
            RESERVED.match(variant):
        raise ValueError('Invalid variant')
    return variant


def exact_entry(parent: Path, name: str, what: str) -> bool:
    """Whether `parent/<name>` exists under exactly that name. A case alias of an existing entry (`giant rat` for `Giant Rat`) is
    refused: one physical folder must never have two identities for locks, tasks or queue keys (phase 5 review F13)."""
    names = _names(parent)
    if name in names:
        return True
    alias = [n for n in names if canon(n) == canon(name)]
    if alias:
        raise ValueError(f'Invalid {what}: {name!r} names {alias[0]!r} with a different case; use the exact name')
    return False


def _is_link(path: Path) -> bool:
    """A symlink or a Windows junction / other reparse point."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def contained(base: Path, *targets: Path) -> None:
    """Mutation-time containment (phase 5 review F13): every target lies inside `base` both lexically and after resolving, and no
    existing component between `base` and the target is a symlink or junction (a junction could redirect a write elsewhere). ValueError
    otherwise. `base` itself may be a link (the monster root of a checkout can be one); it is resolved once."""
    base = Path(base)
    try:
        real = base.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f'the monster root {base} cannot be resolved: {exc}') from None
    for target in targets:
        target = Path(target)
        try:
            rel = target.relative_to(base)
        except ValueError:
            raise ValueError(f'{target} is outside {base}') from None
        cur = base
        for part in rel.parts:
            if part in ('.', '..'):
                raise ValueError(f'{target}: relative parts are not allowed')
            cur = cur / part
            if _is_link(cur):
                raise ValueError(f'{cur} is a link or junction; refusing to write through it')
        try:
            target.resolve().relative_to(real)
        except (OSError, ValueError):
            raise ValueError(f'{target} resolves outside the monster root') from None


def lock_path(root: Path, kind: str, name: str) -> Path:
    """The lock file of a name: keyed by its CANONICAL identity (case aliases share one lock; phase 5 review F13)."""
    short = hashlib.sha1(canon(name).encode('utf-8')).hexdigest()[:8]
    return Path(root) / guard.APP / 'locks' / kind / f'{slug(canon(name))[:40]}-{short}.lock'


def folder_lock(root: Path, folder: str) -> guard.OsLock:
    return guard.os_lock(lock_path(root, 'mconcepts', folder))


def monster_lock(root: Path, monster: str) -> guard.OsLock:
    return guard.os_lock(lock_path(root, 'monsters', monster))


def murl(mroot: Path, path: Path, mtime: float | None = None) -> str | None:
    """`/mfiles/` URL of a regular file under the monster root (the route monsters.resolve_file serves), else None. The version is
    the file's modification time in NANOSECONDS plus its size (final-verify row 10: whole seconds let a replaced file keep its URL); with
    `mtime` (seconds, from a cached folder scan: no extra stat) it is that time in microseconds."""
    try:
        rel = Path(path).relative_to(mroot).as_posix()
        if mtime is None:
            if not Path(path).is_file():
                return None
            st = Path(path).stat()
            version = f'{st.st_mtime_ns}-{st.st_size}'
        else:
            version = str(int(mtime * 1_000_000))
    except (OSError, ValueError):
        return None
    return monsters.FILE_PREFIX + quote(rel, safe='/') + f'?v={version}'


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
        if entry['stage'] == 'approved':
            if not (isinstance(entry.get('monster'), str) and isinstance(entry.get('variant'), str)):
                return f'{key}: an approved entry needs monster and variant'
            try:                                                # persisted ids are validated before anything trusts them (F13)
                safe_part(entry['monster'], 'monster')
                check_variant(entry['variant'])
            except ValueError:
                return f'{key}: invalid monster / variant {entry["monster"]!r} / {entry["variant"]!r}'
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
    contained(Path(fdir).parents[1], Path(fdir) / STATE_FILE)            # concepts/<Folder>/.v2_state.json of the monster root (F13)
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
        badges.append({'text': f'damaged: {str(error)[:160]}' if 'journal' in str(error) else 'state unreadable', 'tone': 'error'})
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
        try:                                                    # one damaged folder never hides the others (F16)
            fdir = mroot / 'concepts' / folder
            state = read_state(fdir, strict=False)
            monster = monster_for_folder(mroot, folder)
            facts = None
            # an unreadable publication journal of the monster blocks its picks: the cards are read-only until it is repaired (finding 11)
            error = state.get('error') or journal_problem(root, monster)
            for concept in scan_folder(fdir):
                entry = state['concepts'].get(concept['key'])
                stage = entry['stage'] if isinstance(entry, dict) else 'concepts'
                if stage in tabs:
                    facts = facts or folder_facts(mroot, monster)
                    tabs[stage].append(concept_card(mroot, folder, monster, concept, entry, error, facts))
        except Exception:  # noqa: BLE001  (a folder that cannot be listed shows nothing; the rest stay)
            continue
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
    card = concept_card(mroot, folder, monster, concept, entry, state.get('error') or journal_problem(root, monster),
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


def names_problem(raw) -> str | None:
    """None when `variant_names.json` has the structure v2 relies on: an object whose `variants` / `retired` (when present) map names
    to objects (phase 5 review F11)."""
    if not isinstance(raw, dict):
        return 'not an object'
    for key in ('variants', 'retired'):
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, dict):
            return f'{key} is not an object'
        for name, rec in value.items():
            if not isinstance(name, str) or not name.strip():
                return f'{key}: invalid variant name {name!r}'
            if not isinstance(rec, dict):
                return f'{key}[{name!r}] is not an object'
    return None


def read_variant_names(mroot: Path, monster: str, strict: bool = False) -> dict:
    """`pipeline/<M>/variant_names.json` ({variants, retired?, note?}); absent = empty. An existing file that cannot be read or has the
    wrong structure is NEVER replaced (its `retired` numbers must stay unused): strict -> guard.Unreadable (HTTP 409; every mutation reads
    strict); not strict -> the empty ledger with `error` set (display only)."""
    path = variant_names_path(mroot, monster)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {'variants': {}}
    except OSError as exc:
        data, problem = None, f'cannot read variant_names.json: {exc}'
    else:
        try:
            data = json.loads(raw.decode('utf-8-sig'))
            problem = names_problem(data)
        except (UnicodeDecodeError, ValueError) as exc:
            data, problem = None, f'variant_names.json is not valid JSON: {exc}'
    if problem is None:
        data.setdefault('variants', {})
        return data
    if strict:
        raise guard.Unreadable(f'pipeline/{monster}/variant_names.json: {problem}; repair it by hand before changing this monster')
    return {'variants': {}, 'error': problem}


def used_variant_names(mroot: Path, monster: str, names: dict | None = None) -> set[str]:
    """Every variant name the monster has used: variant_names.json `variants` and `retired`, views/ and models/ folders (withdrawn and
    `*_prev_*` ones too) and the flat `<k>-front*` / `<k>-back*` files (also in `first round/`)."""
    pdir = mroot / 'pipeline' / monster
    names = read_variant_names(mroot, monster) if names is None else names
    used = set(names['variants']) | set(names.get('retired') or {})
    for sub in ('views', 'models'):
        for name in _names(pdir / sub):
            match = WITHDRAWN_DIR.fullmatch(name)
            if match:
                used.add(match.group(1))
            elif not name.startswith('.'):
                used.add(name)
    for folder in (pdir, *(pdir / sub for sub in FLAT_SUBFOLDERS)):
        for name in _names(folder):
            match = re.match(r'^(.+?)-(front|back)[._]', name)
            if match:
                used.add(match.group(1))
    return used


def next_variant(mroot: Path, monster: str, names: dict | None = None) -> str:
    """B.1: a brand-new monster gets `main`; one that has `main` keeps it and gets the next number from 2 (main counts as 1); a numbered
    one gets max numeric + 1 (leading numbers of `<n>-<armour>` ids count, retired numbers too); never reused. `names` = the ledger the
    caller read (strictly, for a pick)."""
    pdir = mroot / 'pipeline' / monster
    used = used_variant_names(mroot, monster, names) if pdir.is_dir() else set()
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


def canonical_variant(mroot: Path, monster: str, variant: str) -> tuple[str, str]:
    """The exact on-disk identity of a monster / variant pair (F13): the monster folder exists under exactly this name, and a variant
    folder that exists in views/ or models/ is named exactly so (case aliases are refused)."""
    if not exact_entry(mroot / 'pipeline', monster, 'monster'):
        raise not_found(f'Unknown monster {monster}')
    for sub in ('views', 'models'):
        exact_entry(mroot / 'pipeline' / monster / sub, variant, 'variant')
    return monster, variant


def view_file(name: str) -> str:
    return f'{name}.png'


def existing_views(vdir: Path) -> list[str]:
    """The views whose PNG file exists (valid or not)."""
    return [n for n in VIEW_ORDER if (Path(vdir) / view_file(n)).is_file()]


def _cached(cache: dict, path: Path, compute):
    """(`compute(path)` cached by the file's (size, mtime_ns), found); found is False when the file cannot be stat'ed."""
    try:
        st = Path(path).stat()
    except OSError:
        return None, False
    key, sig = str(path), (st.st_size, st.st_mtime_ns)
    with _CACHE_LOCK:
        hit = cache.get(key)
    if hit and hit[0] == sig:
        return hit[1], True
    value = compute(Path(path))
    with _CACHE_LOCK:
        cache[key] = (sig, value)
    return value, True


def png_problem(path: Path) -> str | None:
    """None when `path` passes `png_check.png_problem` (a decodable RGBA PNG with at least 1% fully transparent AND 1% solid (alpha >= 250)
    pixels: the rule of the view runner too, finding 15); cached by size + mtime."""
    value, found = _cached(_PNG_CACHE, path, png_check.png_problem)
    return value if found else 'missing'


def provenance_problem(vdir: Path, name: str) -> str | None:
    """None when the view needs no provenance record (front) or its `<view>.prompts.json` passes `png_check.provenance_problem`."""
    if name == 'front':
        return None
    return png_check.provenance_problem(Path(vdir) / monster_prompts.record_name(name))


def view_problem(vdir: Path, name: str) -> str | None:
    """None when the view is usable for a conversion: its PNG passes png_check and, for an extra view (left / back / right), its
    provenance record `<view>.prompts.json` is a JSON object. The ONE rule of freeze, convert, Retry and the view runner (finding 15)."""
    path = Path(vdir) / view_file(name)
    if not path.is_file():
        return 'no image'
    problem = png_problem(path)
    if problem:
        return f'{view_file(name)}: {problem}'
    problem = provenance_problem(vdir, name)
    if problem:
        return f'{monster_prompts.record_name(name)}: {problem}'
    return None


def view_repairable(vdir: Path, name: str) -> bool:
    """The image itself is a valid view and only its provenance record is missing / invalid: `view_repair` writes the record again and
    nothing is drawn again (finding 15)."""
    path, record = Path(vdir) / view_file(name), Path(vdir) / monster_prompts.record_name(name)
    if name == 'front' or not path.is_file() or png_problem(path) is not None:
        return False
    return png_check.provenance_only(png_check.view_problems(path, record), [record])


def available_views(vdir: Path) -> list[str]:
    """The views that can be chosen for a conversion: the image exists AND is valid (view_problem)."""
    return [n for n in existing_views(vdir) if view_problem(vdir, n) is None]


def views_problem(raw) -> str | None:
    """None when views.json has the structure v2 relies on (phase 5 review F16); a damaged file is never trusted or replaced."""
    if not isinstance(raw, dict):
        return 'not an object'
    if raw.get('schema') != 1:
        return f'schema {raw.get("schema")!r} is not 1'
    views = raw.get('views')
    if not isinstance(views, list):
        return 'views is not a list'
    seen = set()
    for entry in views:
        if not isinstance(entry, dict) or entry.get('name') not in VIEW_ORDER or entry['name'] in seen or \
                not isinstance(entry.get('enabled', True), bool):
            return f'invalid views entry {entry!r}'
        seen.add(entry['name'])
    if not isinstance(raw.get('unattended', False), bool):
        return 'unattended is not a boolean'
    planned = raw.get('planned')
    if planned is not None and (not isinstance(planned, list) or any(v not in EXTRA_VIEWS for v in planned)):
        return 'planned is not a list of extra views'
    frozen = raw.get('frozen')
    if frozen is not None:
        if not isinstance(frozen, dict):
            return 'frozen is not an object'
        files, digests = frozen.get('files'), frozen.get('sha256')
        if not isinstance(files, list) or not 1 <= len(files) <= 4 or len(set(map(str, files))) != len(files) or \
                any(f not in [view_file(n) for n in VIEW_ORDER] for f in files):
            return 'frozen.files is not a list of 1 to 4 view files'
        if not isinstance(digests, dict) or any(not isinstance(digests.get(f), str) for f in files):
            return 'frozen.sha256 does not cover the frozen files'
    if 'pick_gen' in raw and not isinstance(raw['pick_gen'], str):
        return 'pick_gen is not a string'
    conv = raw.get('conversion')
    if conv is not None:
        if not isinstance(conv, dict):
            return 'conversion is not an object'
        for generation, entry in conv.items():                 # every disposition is validated (finding 3): never "absent" when damaged
            problem = disposition_problem(generation, entry)
            if problem:
                return f'conversion[{generation!r}]: {problem}'
    seq = raw.get('conversion_seq')
    if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 0):
        return 'conversion_seq is not a counter'
    return None


def disposition_problem(generation: object, entry: object) -> str | None:
    """None when a views.json `conversion` entry is a readable disposition: a 12-hex generation mapped to {state: cancelled | failed,
    reason?, key?, time?, seq?}."""
    if not isinstance(generation, str) or not GENERATION.fullmatch(generation):
        return 'not a frozen generation'
    if not isinstance(entry, dict):
        return 'not an object'
    if entry.get('state') not in DISPOSITION_STATES:
        return f'state {entry.get("state")!r} is not one of {list(DISPOSITION_STATES)}'
    seq = entry.get('seq')
    if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 0):
        return 'seq is not a counter'
    return None


def read_views_json(vdir: Path) -> dict | None:
    """views.json of a variant: None when absent (a legacy variant). A file that exists but cannot be read or fails views_problem raises
    guard.Unreadable (HTTP 409 for a mutation; a read-only damaged card for the catalog, F16): it is never replaced by defaults."""
    path = Path(vdir) / VIEWS_JSON
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise guard.Unreadable(f'{VIEWS_JSON} of {Path(vdir).name}: cannot be read: {exc}') from None
    try:
        raw = json.loads(raw_bytes.decode('utf-8-sig'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise guard.Unreadable(f'{VIEWS_JSON} of {Path(vdir).name}: not valid JSON: {exc}') from None
    problem = views_problem(raw)
    if problem:
        raise guard.Unreadable(f'{VIEWS_JSON} of {Path(vdir).name}: {problem}')
    views = [{'name': e['name'], 'file': view_file(e['name']), 'enabled': e.get('enabled') is not False} for e in raw['views']]
    return raw | {'views': views, 'unattended': raw.get('unattended') is True, 'frozen': raw.get('frozen')}


def ticks_of(vj: dict | None, available: list[str]) -> dict[str, bool]:
    """{view: enabled} for every available view: the draft ticks of views.json; a view not listed yet is enabled."""
    listed = {v['name']: v['enabled'] for v in (vj or {}).get('views') or []}
    return {name: listed.get(name, True) for name in available}


def write_views_json(vdir: Path, data: dict) -> None:
    """views.json = {"schema":1,"views":[{name,file,enabled}],"unattended","frozen", ...}; other keys of `data` (planned, created, by,
    concept, imported, legacy, pick_gen, conversion, conversion_seq) are kept after those. Refused when the result would not pass views_problem."""
    order = {n: i for i, n in enumerate(VIEW_ORDER)}
    views = sorted(({'name': v['name'], 'file': view_file(v['name']), 'enabled': bool(v['enabled'])} for v in data['views']),
                   key=lambda v: order[v['name']])
    out = {'schema': 1, 'views': views, 'unattended': bool(data.get('unattended')), 'frozen': data.get('frozen')}
    out |= {k: v for k, v in data.items() if k not in out}
    problem = views_problem(out)
    if problem:
        raise ValueError(f'refusing to write an invalid {VIEWS_JSON}: {problem}')
    vdir = Path(vdir)
    if vdir.parent.name == 'views' and len(vdir.parents) > 3:               # views/<v> (or a temp pick folder) of a pipeline monster
        contained(vdir.parents[3], vdir / VIEWS_JSON)
    guard.write_atomic(vdir / VIEWS_JSON, dump(out))


def views_rev(vdir: Path, vj: dict | None) -> int:
    """The revision of what the Choose views dialog shows (phase 5 review F8): the frozen generation, the planned views, the pick
    generation and every view image / provenance file (size + mtime). Draft ticks are NOT part of it (the convert request names its files
    itself). `convert` / `reconvert` answer 409 `changed` when the request's `views_rev` is not this one."""
    files = {}
    for name in VIEW_ORDER:
        for fname in (view_file(name), f'{name}.prompts.json'):
            try:
                st = (Path(vdir) / fname).stat()
            except OSError:
                continue
            files[fname] = [st.st_size, st.st_mtime_ns]
    basis = {'frozen': frozen_digest((vj or {}).get('frozen')), 'planned': (vj or {}).get('planned'),
             'pick_gen': (vj or {}).get('pick_gen'), 'files': files, 'legacy': vj is None}
    return int(hashlib.sha256(json.dumps(basis, sort_keys=True).encode('utf-8')).hexdigest()[:12], 16)   # an int below 2**48 (JS-safe)


def frozen_digest(frozen: dict | None) -> str | None:
    if not isinstance(frozen, dict):
        return None
    return hashlib.sha256(json.dumps(frozen, sort_keys=True).encode('utf-8')).hexdigest()[:12]


def freeze(vdir: Path, files: list[str]) -> dict:
    """The frozen view list {time, files, sha256} of `files` (in the given order); every file must be a valid view (Missing otherwise)."""
    for f in files:
        problem = view_problem(vdir, str(f)[:-4]) if str(f).endswith('.png') else 'not a view file'
        if problem:
            raise Missing(f'{f}: {problem}', file=str(f), files=[str(f)])
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


LEVEL_FILES = tuple(f'lod_error_{lv}.glb' for lv in LOD_LEVELS)


def reduced_files_problem(mdir: Path) -> str | None:
    """None when reduced/ holds the five LOD GLBs, a manifest object and a materials.json object (the reducer's LAST write)."""
    red = Path(mdir) / 'reduced'
    for name in LEVEL_FILES:
        try:
            if (red / name).stat().st_size <= 0:
                return f'reduced/{name} is empty'
        except OSError:
            return f'reduced/{name} is missing'
    if not isinstance(read_json(red / 'manifest.json', None), dict):
        return 'reduced/manifest.json is missing or invalid'
    if not isinstance(read_json(red / 'materials.json', None), dict):
        return 'reduced/materials.json is missing or invalid (the reducer did not finish)'
    return None


def reduced_complete(mdir: Path) -> bool:
    """The reduced outputs are all there (the files only; a conversion of the lane is verified by reduce_status)."""
    return reduced_files_problem(mdir) is None


def write_reduce_ok(mdir: Path, frozen: dict, source: dict | None = None) -> dict:
    """The lane's generation-bound success record `reduced/v2_reduce_ok.json` (phase 5 review F12), written only after every output
    validated: {schema, generation (the frozen digest), frozen_time, files {name: {sha256, size}}, materials: true, time}. `source`
    ({file, sha256}: the RECEIPTED high-poly GLB the lane handed to the reducer and verified before and after the reduction;
    final-live review, "reduction can certify the wrong model") is stored as `source`."""
    mdir = Path(mdir)
    problem = reduced_files_problem(mdir)
    if problem:
        raise ValueError(problem)
    red = mdir / 'reduced'
    files = {name: {'sha256': sha256_file(red / name), 'size': (red / name).stat().st_size}
             for name in (*LEVEL_FILES, 'manifest.json', 'materials.json')}
    record = {'schema': 1, 'generation': frozen_digest(frozen), 'frozen_time': frozen.get('time'), 'files': files, 'materials': True,
              'time': now(), 'by': 'v2 monster lane'}
    if source is not None:
        record['source'] = dict(source)
    if len(mdir.parents) > 3:
        contained(mdir.parents[3], red / REDUCE_OK)
    guard.write_atomic(red / REDUCE_OK, dump(record))
    return record


def file_sha256(path: Path) -> str | None:
    """sha256 of a file, cached by size + mtime: a HINT only (a file replaced by other bytes of the same size with its timestamp put
    back keeps the cached digest). Nothing is certified with it: see fresh_sha256."""
    value, found = _cached(_SHA_CACHE, path, sha256_file)
    return value if found else None


def fresh_sha256(path: Path) -> str | None:
    """sha256 of the file's bytes as they are NOW (read and hashed on every call; None when it cannot be read). What an approval, the
    Completed tab and the digest the 3D modal compares its loaded bytes with are certified by (final-verify row 10)."""
    try:
        return sha256_file(Path(path))
    except OSError:
        return None


def reduce_status(mdir: Path, generation: str | None = None, deep: bool = False) -> dict:
    """{ok, legacy, problem} of a variant's reduction (F12). With the lane's record: valid when its generation is `generation` (when given)
    and every listed file still has its size (and, `deep`, its sha256, hashed from the bytes on disk now: fresh_sha256). Without a record: a variant the lane converts (`generation` given)
    is NOT verified; a legacy result (generation None) is accepted by the explicit compatibility rule manifest + 5 LOD GLBs + materials.json
    and flagged `legacy`."""
    red = Path(mdir) / 'reduced'
    rec = read_json(red / REDUCE_OK, None)
    if rec is not None:
        if not isinstance(rec, dict) or not isinstance(rec.get('files'), dict):
            return {'ok': False, 'legacy': False, 'problem': f'reduced/{REDUCE_OK} is invalid'}
        if generation is not None and rec.get('generation') != generation:
            return {'ok': False, 'legacy': False, 'problem': 'the reduction belongs to another choice of views'}
        missing = [n for n in (*LEVEL_FILES, 'materials.json') if n not in rec['files']]
        if missing:
            return {'ok': False, 'legacy': False, 'problem': f'reduced/{REDUCE_OK} does not list {", ".join(missing)}'}
        for name, meta in rec['files'].items():
            path = red / str(name)
            try:
                size = path.stat().st_size
            except OSError:
                return {'ok': False, 'legacy': False, 'problem': f'reduced/{name} is missing'}
            if not isinstance(meta, dict) or meta.get('size') != size or (deep and fresh_sha256(path) != meta.get('sha256')):
                return {'ok': False, 'legacy': False, 'problem': f'reduced/{name} changed after the reduction was verified'}
        return {'ok': True, 'legacy': False, 'problem': None, 'generation': rec.get('generation')}
    if generation is not None:
        return {'ok': False, 'legacy': False, 'problem': f'the reduction has no success record (reduced/{REDUCE_OK})'}
    problem = reduced_files_problem(mdir)
    return {'ok': problem is None, 'legacy': problem is None, 'problem': problem}


def record_of(mdir: Path) -> dict | None:
    value = read_json(Path(mdir) / 'hyper3d' / 'studio_submission.json', None)
    return value if isinstance(value, dict) else None


def hyper3d_backend():
    """tools/hyper3d_pipeline/rodin_backend (imported on first use: it is the authority on what a submission record proves)."""
    folder = str(Path(__file__).resolve().parents[2] / 'hyper3d_pipeline')
    if folder not in sys.path:
        sys.path.insert(0, folder)
    import rodin_backend
    return rodin_backend


def proven_unsubmitted(record: dict | None) -> bool:
    """A record of a PROVEN pre-click failure that is still at studio_submission.json (final-live review, "a rejected upload strands
    the unpaid variant"): `rodin_backend.proven_pre_click` (a pre-click status with no action or intent of any kind, no pending
    action, no model URL / id, no click time, no export; a `not_submitted` record still in place under those conditions counts
    too: an archive interrupted between its status write and its rename, confirm review "Pilot, rejected upload"). The backend's
    wrappers archive such a record themselves as
    `studio_submission.not_submitted_<time>.json` (on the pre-click stop and at the start of a new submit); one that is still in
    place leaves the variant UNSUBMITTED all the same: Retry / Use enabled views submit again. A record with any click intent is
    never one (it stays, for review). False when the backend cannot be asked (fail closed: the record counts)."""
    if not isinstance(record, dict):
        return False
    try:
        return bool(hyper3d_backend().proven_pre_click(record))
    except Exception:  # noqa: BLE001
        return False


def conversion_started(mdir: Path) -> bool:
    """A conversion record, a downloaded GLB or a reduced folder exists (a Use enabled views would pay again: Reconvert only). A
    record of a proven pre-click failure (proven_unsubmitted; one the backend archived as `studio_submission.not_submitted_*.json`
    is no record at all) does not count: nothing was submitted, let alone paid."""
    mdir = Path(mdir)
    hyper = mdir / 'hyper3d'
    record = hyper / 'studio_submission.json'
    started = record.is_file() and not proven_unsubmitted(record_of(mdir))
    return started or any(hyper.glob('glb_*.glb')) or (mdir / 'reduced').exists()


# ------------------------------------------------------------------------------------------------------------- view tasks

FAULTS: dict = {}            # tests: {stage: fn()} called at named points of a pick / import / reconvert (a crash is a BaseException)


def _fault(stage: str) -> None:
    hook = FAULTS.get(stage)
    if hook:
        hook()


def batch_dir(root: Path) -> Path:
    return Path(root) / '.codex_jobs' / '_batch'


def view_task_name(monster: str, variant: str, view: str) -> str:
    """`mviews--<monster slug>--<variant slug>--<canonical digest 8>--<view>`: the digest makes the name unique where the slugs are not
    (`Giant Rat` / `Giant_Rat`; phase 5 review F13)."""
    return f'{VIEW_TASK_KIND}--{slug(monster)[:40]}--{slug(variant)[:24]}--{canon_digest(monster, variant)}--{view}'


def view_task_state(root: Path, name: str, owner: tuple[str, str] | None = None) -> dict:
    """{exists, foreign, state, attempts, job, started, exit}: the view runner's status of a task (absent status file = pending). `owner`
    (monster, variant): a descriptor that names another monster / variant is `foreign` (never counted as this variant's task, F13)."""
    tasks, status = batch_dir(root) / 'tasks' / f'{name}.json', batch_dir(root) / 'status' / f'{name}.json'
    exists = tasks.is_file()
    foreign = False
    if exists and owner is not None:
        desc = read_json(tasks, None)
        foreign = not (isinstance(desc, dict) and isinstance(desc.get('monster'), str) and isinstance(desc.get('variant'), str) and
                       (canon(desc['monster']), canon(desc['variant'])) == (canon(owner[0]), canon(owner[1])))
    raw = read_json(status, None)
    raw = raw if isinstance(raw, dict) else {}
    jobs = raw.get('jobs') if isinstance(raw.get('jobs'), list) else []
    last = jobs[-1] if jobs and isinstance(jobs[-1], dict) else {}
    return {'name': name, 'exists': exists, 'foreign': foreign,
            'state': raw.get('state') if isinstance(raw.get('state'), str) else 'pending',
            'attempts': raw.get('attempts') if isinstance(raw.get('attempts'), int) else 0, 'jobs': len(jobs), 'job': last.get('job'),
            'started': last.get('started'), 'exit': last.get('exit'),
            'error': next((raw[k] for k in ('error', 'reason') if isinstance(raw.get(k), str) and raw[k]), None)}   # runner: error / guard reason


def task_started(st: dict) -> bool:
    """The runner reserved, ran or finished a launch of the task (a `cancelled` task that never launched has not started)."""
    return st['state'] in ('launching', 'running', 'done', 'failed') or st['jobs'] > 0


def task_terminal(st: dict | None) -> bool:
    return bool(st and st['exists'] and not st.get('foreign') and st['state'] in TERMINAL_VIEW_STATES)


def view_task(root: Path, mroot: Path, monster: str, variant: str, view: str, pick_gen: str) -> dict:
    """The view-runner task of one extra view (DESIGN_PHASE5.md C): launcher monsters, no checks, 2 attempts, priority 3. `validate` /
    `provenance` make the runner fail an attempt whose PNG is not a decodable RGBA PNG with transparency or whose `<view>.prompts.json` is
    missing (F17); `guard` lets it spawn only while views.json still carries this pick's generation (withdrawn = cancelled, F10)."""
    vdir = views_dir(mroot, monster, variant)
    colours = read_obj(vdir / 'colours.json')
    front, out = vdir / 'front.png', vdir / view_file(view)
    return {'name': view_task_name(monster, variant, view), 'priority': VIEW_PRIORITY,
            'prompt': monster_prompts.job_prompt(view, monster, variant, front, out, colours),
            'refs': [front.as_posix()], 'outputs': [out.as_posix()], 'checks': [], 'max_attempts': VIEW_MAX_ATTEMPTS,
            'validate': VIEW_VALIDATE, 'provenance': [monster_prompts.record_name(view)],
            'guard': {'file': str(vdir / VIEWS_JSON), 'key': 'pick_gen', 'value': pick_gen},
            'launcher': 'monsters', 'monster': monster, 'variant': variant, 'view': view, 'created': now(), 'by': 'v2'}


def _ensure_pick_gen(vdir: Path) -> str:
    """The pick generation of views.json (written when an older views.json has none). Caller holds the monster lock."""
    vj = read_views_json(vdir)
    if vj is None:
        raise ValueError(f'{vdir.name} has no {VIEWS_JSON}')
    if not vj.get('pick_gen'):
        vj['pick_gen'] = uuid.uuid4().hex
        write_views_json(vdir, vj)
    return vj['pick_gen']


def queue_view_tasks(root: Path, mroot: Path, monster: str, variant: str, views=EXTRA_VIEWS) -> list[str]:
    """Write the task files of the extra views whose image does not exist yet (an existing task file of THIS variant is kept; one that
    names another variant is a Conflict). The caller holds the monster lock (publication and withdrawal never interleave, F10).
    Returns the names written."""
    vdir = views_dir(mroot, monster, variant)
    pick_gen = _ensure_pick_gen(vdir)
    written = []
    for view in views:
        if (vdir / view_file(view)).is_file():
            continue
        task = view_task(root, mroot, monster, variant, view, pick_gen)
        path = batch_dir(root) / 'tasks' / f'{task["name"]}.json'
        if path.exists():
            if view_task_state(root, task['name'], (monster, variant))['foreign']:
                raise guard.Conflict(f'{task["name"]}.json belongs to another monster / variant; not overwritten')
            continue
        guard.write_atomic(path, dump(task))
        written.append(task['name'])
    return written


def view_states(root: Path, mroot: Path, monster: str, variant: str) -> dict[str, dict]:
    return {view: view_task_state(root, view_task_name(monster, variant, view), (monster, variant)) for view in EXTRA_VIEWS}


def guard_ok(desc: dict) -> bool:
    """The descriptor carries a usable runner guard {file, key, value} (phase 5 review F10)."""
    g = desc.get('guard')
    return isinstance(g, dict) and isinstance(g.get('file'), str) and bool(g['file']) and isinstance(g.get('key'), str) and 'value' in g


def monster_descriptors(root: Path) -> list[tuple[str, dict | None]]:
    """[(task name, descriptor | None when unreadable)] of every monster view-task descriptor in `_batch/tasks/`: the `mviews--` names
    (the current digested ones and the older undigested ones) and any descriptor that says `launcher: monsters`."""
    folder = batch_dir(root) / 'tasks'
    out = []
    for fname in _names(folder):
        if not fname.endswith('.json'):
            continue
        name = fname[:-len('.json')]
        desc = read_json(folder / fname, None)
        desc = desc if isinstance(desc, dict) else None
        if name.startswith(f'{VIEW_TASK_KIND}--') or (desc is not None and desc.get('launcher') == 'monsters'):
            out.append((name, desc))
    return out


def legacy_task_names(monster: str, variant: str) -> set[str]:
    """The names a view task of this variant had before the canonical digest was part of the name (phase 5 review row 10)."""
    return {f'{VIEW_TASK_KIND}--{slug(monster)[:40]}--{v}--{view}' for v in (variant, slug(variant), slug(variant)[:24]) for view in EXTRA_VIEWS}


def variant_descriptors(root: Path, monster: str, variant: str) -> list[str]:
    """EVERY view-task descriptor that belongs to the variant, whatever its name: the three current names, an older undigested name,
    and any descriptor whose `monster` / `variant` fields name it (phase 5 review row 10: withdrawal must find the old names too)."""
    names = {name for name in (view_task_name(monster, variant, view) for view in EXTRA_VIEWS)
             if not view_task_state(root, name, (monster, variant))['foreign']}
    legacy = legacy_task_names(monster, variant)
    for name, desc in monster_descriptors(root):
        fields = desc is not None and isinstance(desc.get('monster'), str) and isinstance(desc.get('variant'), str)
        if fields and (canon(desc['monster']), canon(desc['variant'])) == (canon(monster), canon(variant)):
            names.add(name)
        elif not fields and name in legacy:                    # an old descriptor without owner fields: its name is all there is
            names.add(name)
    return sorted(n for n in names if (batch_dir(root) / 'tasks' / f'{n}.json').exists())


def reconcile_descriptors(root: Path, errors: list | None = None) -> list[str]:
    """HOOK (monster_lane.tick; phase 5 review row 10, "before enabling dispatch"): no monster view task may wait for the runner without
    a guard or under an old undigested name. Such a descriptor that is not launching / running is renamed `<name>.json.unguarded-<time>`
    (the runner then finds no task: cancelled); when its variant still exists, plans the view and has no image, the current guarded
    descriptor is written in its place (under the monster lock, like every publication). One that is running is only reported. Returns
    `<name>: withdrawn | republished` lines."""
    root, mroot = Path(root), mroot_of(root)
    out = []
    for name, desc in monster_descriptors(root):
        try:
            fields = desc is not None and isinstance(desc.get('monster'), str) and isinstance(desc.get('variant'), str) and \
                desc.get('view') in EXTRA_VIEWS
            current = fields and name == view_task_name(desc['monster'], desc['variant'], desc['view'])
            if current and guard_ok(desc):
                continue
            st = view_task_state(root, name)
            if st['state'] in ('launching', 'running'):
                raise guard.Conflict(f'an unguarded monster view task is {st["state"]}; it cannot be withdrawn now')
            try:
                monster, variant = (safe_part(desc['monster'], 'monster'), check_variant(desc['variant'])) if fields else (None, None)
            except ValueError:
                monster = variant = None                       # an owner that is no valid name: withdrawn, never republished
            path = batch_dir(root) / 'tasks' / f'{name}.json'

            def withdraw_it() -> str:
                if not path.exists():
                    return 'gone'
                os.rename(path, path.with_name(f'{path.name}.unguarded-{stamp()}'))
                republish = monster is not None and st['state'] == 'pending' and views_dir(mroot, monster, variant).is_dir() and \
                    exact_entry(mroot / 'pipeline', monster, 'monster')
                if republish:
                    vj = read_views_json(views_dir(mroot, monster, variant))
                    if vj is not None and desc['view'] in planned_views(vj) and queue_view_tasks(root, mroot, monster, variant, (desc['view'],)):
                        return 'republished'
                return 'withdrawn'
            if monster is not None:
                with monster_lock(root, monster):
                    done = withdraw_it()
            else:
                done = withdraw_it()
            out.append(f'{name}: {done}')
        except Exception as exc:  # noqa: BLE001  (one descriptor never stops the others; shown in the orchestrator errors)
            if errors is not None:
                errors.append(f'view task {name}: {type(exc).__name__}: {exc}')
    return out


def planned_views(vj: dict | None) -> list[str]:
    """The extra views the pick / import queued (views.json `planned`); a legacy draft plans none."""
    planned = (vj or {}).get('planned')
    return [v for v in planned if v in EXTRA_VIEWS] if isinstance(planned, list) else []


def view_waiting(vdir: Path, view: str, st: dict | None) -> bool:
    """THE terminal-state rule of a planned view (finding 15; the item phase and the unattended freeze both use it): with a task
    descriptor of this variant the view is still coming until that task is terminal (done / failed / cancelled), whether or not a PNG
    appeared meanwhile (phase 5 review F9); WITHOUT a descriptor it is settled exactly when a valid view exists (image and provenance),
    else it still waits (Retry writes the task; 'repair provenance' completes a good image)."""
    if st is not None and st['exists'] and not st.get('foreign'):
        return st['state'] not in TERMINAL_VIEW_STATES
    return view_problem(vdir, view) is not None


def views_phase(root: Path, mroot: Path, monster: str, variant: str, vj: dict | None = None, states: dict | None = None) -> str:
    """legacy (no views.json) | generating (a planned view is still coming) | choose (every planned view task is terminal, nothing
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
    problem = names_problem(data)
    if problem:
        raise ValueError(f'refusing to write an invalid variant_names.json: {problem}')
    path = variant_names_path(mroot, monster)
    contained(mroot, path)
    guard.write_atomic(path, dump(data))


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


def _rmtree_own(path: Path) -> None:
    """Remove a temp folder v2 created itself (read-only snapshot files included)."""
    def unlock(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=unlock)


# The journal of a pick / import publication (phase 5 review F11): `.codex_jobs/_app/monster_journal/<monster key>.json`, written once the
# temp folder is complete and before variant_names.json, the views folder and the concept state change; deleted when all are done.
# A journal that survives (the process died) is COMPLETED by recovery (the same variant: names entry, views folder, concept state, view
# tasks); only when neither the temp folder nor the views folder exists any more is the published names entry retired instead.

def journal_dir(root: Path) -> Path:
    return Path(root) / guard.APP / 'monster_journal'


def journal_path(root: Path, monster: str) -> Path:
    return journal_dir(root) / f'{slug(canon(monster))[:40]}-{canon_digest(monster)}.json'


def read_journal(root: Path, monster: str) -> dict | None:
    """The publication journal of a monster, read STRICTLY (finding 11): None when there is none; a file that cannot be read or is not
    a pick / import journal of this monster raises guard.Unreadable and stays where it is."""
    path = journal_path(root, monster)
    j = read_strict(path, f'the publication journal {path.name}')
    if j is None:
        return None
    if j.get('op') not in ('pick', 'import') or canon(str(j.get('monster'))) != canon(monster):
        raise guard.Unreadable(f'{path.name}: not a publication journal of {monster}; check {monster} by hand before removing it')
    problem = journal_record_problem(j, monster) or journal_target_problem(mroot_of(root), monster, j)
    if problem:
        raise guard.Unreadable(f'{path.name}: not a complete {j["op"]} journal ({problem}); nothing was changed: check {monster} by hand '
                               'before removing it')
    return j


IMPORT_STAMPS = ('imported', 'imported_by')                     # what import_pick adds to the earlier names record


def journal_target_problem(mroot: Path, monster: str, j: dict) -> str | None:
    """Last-check review, row 11: the journal must PROVE its recovery target against variant_names.json as it is. Recovery writes
    `names_entry` over the variant's record (or restores `names_prev`), so the record on disk must be one of the two states the
    operation itself produces: still `names_prev` (not published yet; null = no record) or already `names_entry` (published). Any
    other record was not written by this operation: the journal cannot say what to put there, and nothing is replaced. `j` passed
    journal_record_problem. An unreadable variant_names.json is a problem too (never read as "no record")."""
    variant = j['variant']
    try:
        names = read_variant_names(mroot, monster, strict=True)
    except guard.Unreadable as exc:
        return f'variant_names.json cannot be read to check the journal against it ({exc})'
    current = names['variants'].get(variant)
    if current == j['names_entry'] or current == j['names_prev']:
        return None
    return (f'the variant_names.json record of {variant} is neither the earlier record of the journal (names_prev) nor the record it '
            'publishes (names_entry): the journal does not prove what to write there')


def journal_record_problem(j: dict, monster: str) -> str | None:
    """None when `j` is the COMPLETE record of its operation, as pick / import_pick write it (final-verify row 11). Recovery replaces
    variant_names.json entries, renames folders and rewrites concept states from these fields, so every one of them is checked BEFORE
    either file is touched; a parseable journal with a missing or malformed field is as unusable as a torn one.
      both:   variant (a valid variant name), tmp (`.v2_pick_<variant>_<8 hex>`, a plain name), names_entry (a non-empty object: the
              record recovery publishes), names_prev (present: null or the object it restores);
      pick:   folder, key (`codex<N>`), origin (a stage a pick starts from), state_entry (an object: stage approved, this variant),
              names_entry.was = key, names_prev null;
      import: names_entry.imported (the import stamp), and names_entry = names_prev + the import stamps (names_prev null: the record
              of an approved concept, was `codex<N>` + concept `codex<N>-front.png`, + the stamps); never the stamps alone.
    read_journal also checks the record against variant_names.json itself (journal_target_problem, last-check row 11)."""
    op = j.get('op')
    if op not in ('pick', 'import'):
        return f'unknown operation {op!r}'
    if not isinstance(j.get('monster'), str) or canon(j['monster']) != canon(monster):
        return f'it does not name {monster}'
    try:
        variant = check_variant(j.get('variant'))
    except ValueError:
        return f'invalid variant {j.get("variant")!r}'
    tmp = j.get('tmp')
    if not isinstance(tmp, str) or not re.fullmatch(re.escape(f'.v2_pick_{variant}_') + r'[0-9a-f]{8}', tmp):
        return f'invalid tmp {tmp!r}'
    entry = j.get('names_entry')
    if not isinstance(entry, dict) or not entry:
        return 'names_entry (the variant_names.json record to publish) is missing'
    if 'names_prev' not in j or not (j['names_prev'] is None or isinstance(j['names_prev'], dict)):
        return 'names_prev (the variant_names.json record to restore: null or an object) is missing'
    if op == 'pick':
        try:
            safe_part(j.get('folder'), 'concept folder')
        except ValueError:
            return f'invalid folder {j.get("folder")!r}'
        key = j.get('key')
        if not isinstance(key, str) or not KEY.fullmatch(key):
            return f'invalid key {key!r}'
        if j.get('origin') not in CONCEPT_VERBS['approve']:
            return f'invalid origin {j.get("origin")!r}'
        state_entry = j.get('state_entry')
        if not isinstance(state_entry, dict) or state_entry.get('stage') != 'approved' or state_entry.get('variant') != variant:
            return 'state_entry (the concept state to publish: stage approved, this variant) is missing'
        if entry.get('was') != key:
            return 'names_entry is not the record of this concept'
        if j['names_prev'] is not None:
            return 'a pick has no earlier names entry to restore'
    else:
        if not isinstance(entry.get('imported'), str) or not entry['imported']:
            return 'names_entry is not an import record (no `imported` stamp)'
        # last-check review, row 11: the entry to write must be DERIVABLE from the journal: import_pick writes the earlier record
        # (names_prev) plus the import stamps, or, for a variant only the migration knows (names_prev null), the record of its
        # approved concept plus the stamps. An entry that carries only the stamps would replace a record's provenance by nothing.
        body = {k: v for k, v in entry.items() if k not in IMPORT_STAMPS}
        if not body:
            return 'names_entry carries only the import stamps (no record of the variant to publish)'
        prev = j['names_prev']
        if isinstance(prev, dict):
            if body != {k: v for k, v in prev.items() if k not in IMPORT_STAMPS}:
                return 'names_entry is not names_prev plus the import stamps (the journal does not prove the record to publish)'
        elif not isinstance(body.get('was'), str) or not KEY.fullmatch(body['was']) or body.get('concept') != f'{body["was"]}-front.png':
            return 'names_entry is not the record of an approved concept (was / concept), and there is no names_prev it comes from'
    return None


def journal_problem(root: Path, monster: str) -> str | None:
    """Why the monster's publication journal blocks picks / imports / withdrawals (an unreadable journal), else None."""
    try:
        read_journal(root, monster)
    except guard.Unreadable as exc:
        return str(exc)
    return None


def journal_blocks(root: Path, monster: str, variant: str, problem: str | None = None) -> str | None:
    """Why the monster's publication journal makes `variant` READ-ONLY (final-verify row 11), else None: the journal cannot be used
    (unreadable, or not the complete record of its operation) and it names this variant, or does not say which variant it is about.
    Such a journal is never completed, rolled back or removed by v2; the variant is a damaged card until it is repaired by hand.
    `problem` = journal_problem(root, monster) when the caller has it."""
    problem = journal_problem(root, monster) if problem is None else problem
    if not problem:
        return None
    raw = read_json(journal_path(root, monster), None)
    named = raw.get('variant') if isinstance(raw, dict) else None
    if isinstance(named, str) and VARIANT.fullmatch(named) and named != variant:
        return None
    return problem


def _write_journal(root: Path, monster: str, data: dict) -> None:
    """Written only when no journal exists (the caller recovered first): an existing file, readable or not, is never replaced."""
    path = journal_path(root, monster)
    if path.exists():
        raise guard.Recovering(f'{path.name}: an earlier publication of {monster} is not resolved yet')
    guard.write_atomic(path, dump(data | {'schema': 1, 'time': now()}))


def _drop_journal(root: Path, monster: str) -> None:
    journal_path(root, monster).unlink(missing_ok=True)


def _recover_locked(root: Path, mroot: Path, monster: str, held_folder: str | None) -> str | None:
    """Complete (or, when impossible, retire) an interrupted pick / import of `monster`. The caller holds the monster lock and, for a
    pick, the lock of `held_folder`; a pick journal of another folder is refused (the orchestrator tick recovers it with both locks).
    Returns 'completed' / 'rolled back' / None (no journal)."""
    j = read_journal(root, monster)                              # strict: an unreadable journal blocks, it is never dropped (finding 11)
    if j is None:
        return None
    variant = check_variant(j.get('variant'))
    if j['op'] == 'pick' and j.get('folder') != held_folder:
        raise guard.Recovering(f'an interrupted pick of {monster} waits for recovery (the orchestrator completes it); try again shortly')
    names = read_variant_names(mroot, monster, strict=True)
    vdir = views_dir(mroot, monster, variant)
    tmp_name = str(j.get('tmp') or '')
    tmp = vdir.parent / tmp_name if tmp_name.startswith('.v2_pick_') and '/' not in tmp_name and '\\' not in tmp_name else None
    contained(mroot, vdir, *( [tmp] if tmp else []))
    fdir = None
    if j['op'] == 'pick':
        fdir = mroot / 'concepts' / safe_part(j.get('folder'), 'concept folder')
    if not vdir.exists() and not (tmp and (tmp / VIEWS_JSON).is_file()):
        # nothing to publish any more: retire a published names entry (its number is never used again) and undo the concept state
        if names['variants'].get(variant) == j.get('names_entry'):
            prev = j.get('names_prev')
            if isinstance(prev, dict):
                names['variants'][variant] = prev
            else:
                names['variants'].pop(variant, None)
                names.setdefault('retired', {})[variant] = dict(j.get('names_entry') or {}) | {
                    'retired': now(), 'reason': 'interrupted v2 publication rolled back'}
            _write_names(mroot, monster, names)
        if fdir is not None:
            state = read_state(fdir)
            entry = state['concepts'].get(str(j.get('key')))
            if isinstance(entry, dict) and entry.get('stage') == 'approved' and entry.get('variant') == variant:
                state['concepts'][j['key']] = {'stage': j.get('origin') if j.get('origin') in STAGES[:2] + ('archive',) else 'concepts',
                                               'time': now(), 'from': 'approved', 'recovered': 'interrupted pick rolled back'}
                write_state(fdir, state)
        if tmp and tmp.exists():
            _rmtree_own(tmp)
        _drop_journal(root, monster)
        return 'rolled back'
    if names['variants'].get(variant) != j.get('names_entry'):
        names['variants'][variant] = dict(j.get('names_entry') or {})
        _write_names(mroot, monster, names)
    if not vdir.exists():
        os.rename(tmp, vdir)
    if fdir is not None:
        state = read_state(fdir)
        entry = state['concepts'].get(str(j.get('key')))
        if not (isinstance(entry, dict) and entry.get('stage') == 'approved' and entry.get('variant') == variant):
            state['concepts'][j['key']] = dict(j.get('state_entry') or {}) | {'recovered': now()}
            write_state(fdir, state)
    try:
        queue_view_tasks(root, mroot, monster, variant)
    except (OSError, ValueError, guard.Conflict):
        pass                                                    # Retry writes a missing task file
    _drop_journal(root, monster)
    return 'completed'


def recover_journals(root: Path) -> list[str]:
    """HOOK (monster_lane.tick): complete every interrupted pick / import (each under its folder and monster locks; one bad journal never
    stops the others). Returns `<monster>: completed | rolled back | error ...` lines."""
    out = []
    root, mroot = Path(root), mroot_of(root)
    try:
        paths = sorted(journal_dir(root).glob('*.json'))
    except OSError:
        return out
    for path in paths:
        try:
            j = read_strict(path, f'the publication journal {path.name}')
            if j is None:
                continue
            monster = safe_part(j.get('monster'), 'monster')
            if path != journal_path(root, monster):
                raise guard.Unreadable(f'{path.name} does not belong to {monster}')
            if j.get('op') == 'pick':
                folder = safe_part(j.get('folder'), 'concept folder')
                with folder_lock(root, folder), monster_lock(root, monster):
                    done = _recover_locked(root, mroot, monster, folder)
            else:
                with monster_lock(root, monster):
                    done = _recover_locked(root, mroot, monster, None)
                if done == 'completed' and isinstance(j.get('names_entry'), dict):
                    _mark_concept_of_pick(root, mroot, monster, j['variant'], j['names_entry'])
            if done:
                out.append(f'{monster}: {done}')
        except Exception as exc:  # noqa: BLE001  (kept for the next pass; shown in the orchestrator errors)
            out.append(f'{path.name}: error {type(exc).__name__}: {exc}')
    return out


def pick(root: Path, folder: str, key: str, unattended: bool = False) -> dict:
    """Approve = pick (B.1-B.6) of `concepts/<folder>/<key>-front.png` from stage concepts / provisional / archive. Journaled (F11): the
    temp folder is complete before anything is published; the view tasks are written under the same monster lock (F10)."""
    root, mroot = Path(root), mroot_of(root)
    fdir = concept_dir(mroot, folder)
    exact_entry(mroot / 'concepts', folder, 'concept folder')
    concept = concept_files(fdir, key)
    monster = safe_part(monster_for_folder(mroot, folder), 'monster')
    with folder_lock(root, folder):
        with monster_lock(root, monster):
            _recover_locked(root, mroot, monster, folder)
            state = read_state(fdir)
            origin = stage_of(state, key)
            if origin not in CONCEPT_VERBS['approve']:
                raise ValueError(f'{folder}/{key} is in {origin}; only a concept in concepts, provisional or archive can be approved')
            pdir = mroot / 'pipeline' / monster
            if pdir.exists():
                exact_entry(mroot / 'pipeline', monster, 'monster')
            names = read_variant_names(mroot, monster, strict=True)
            variant = next_variant(mroot, monster, names)
            vdir, mdir = views_dir(mroot, monster, variant), models_dir(mroot, monster, variant)
            names_path = variant_names_path(mroot, monster)
            contained(mroot, pdir / 'views', vdir, mdir, names_path)
            if vdir.exists() or mdir.exists():
                raise guard.Conflict(f'{monster}/{variant} already has a views or models folder')
            tmp = pdir / 'views' / f'.v2_pick_{variant}_{uuid.uuid4().hex[:8]}'
            tmp.mkdir(parents=True)
            names_before = guard.read_bytes_or_none(names_path)
            names_entry = {'was': key, 'concept': f'{key}-front.png', 'picked': now(), 'by': 'v2'}
            state_entry = {'stage': 'approved', 'time': now(), 'monster': monster, 'variant': variant, 'from': origin,
                           'unattended': bool(unattended)}
            moved = journaled = False
            try:
                shutil.copyfile(concept['png'], tmp / 'front.png')
                if concept['colours']:
                    shutil.copyfile(concept['colours'], tmp / 'colours.json')
                if concept['prompt']:
                    shutil.copyfile(concept['prompt'], tmp / 'prompts.json')
                write_views_json(tmp, {'views': [{'name': 'front', 'enabled': True}], 'unattended': bool(unattended), 'frozen': None,
                                       'planned': list(EXTRA_VIEWS), 'created': now(), 'by': 'v2', 'pick_gen': uuid.uuid4().hex,
                                       'concept': f'concepts/{folder}/{key}-front.png'})
                _write_journal(root, monster, {'op': 'pick', 'monster': monster, 'variant': variant, 'folder': folder, 'key': key,
                                               'origin': origin, 'tmp': tmp.name, 'names_entry': names_entry, 'names_prev': None,
                                               'state_entry': state_entry})
                journaled = True
                if not names_before:
                    names = {'note': 'Written by the v2 preview app: a pick (approve of a concept) adds variants[<v>] (DESIGN_PHASE5.md B); '
                                     'an undone pick moves to retired[<v>], so its number is never used again.'} | names
                names['variants'][variant] = names_entry
                _write_names(mroot, monster, names)
                _fault('pick_names')
                os.rename(tmp, vdir)
                moved = True
                _fault('pick_renamed')
                state['concepts'][key] = state_entry
                write_state(fdir, state)
            except Exception:
                _restore(names_path, names_before)
                if moved and vdir.exists():
                    try:
                        os.rename(vdir, tmp)
                    except OSError:
                        pass
                if tmp.exists():
                    _rmtree_own(tmp)                            # our own temp folder
                if journaled and not vdir.exists():
                    _drop_journal(root, monster)
                raise
            warning = None
            try:
                tasks = queue_view_tasks(root, mroot, monster, variant)
            except (OSError, ValueError, guard.Conflict) as exc:
                tasks, warning = [], f'the view tasks could not be written ({exc}); use Retry on each view'
            _drop_journal(root, monster)
    result = {'ok': True, 'monster': monster, 'variant': variant, 'id': f'monster:{monster}/{variant}', 'from': origin,
              'views': f'pipeline/{monster}/views/{variant}', 'tasks': tasks,
              'library': library_info(mroot, monster)}
    if warning:
        result['warning'] = warning
    return result


def withdraw(root: Path, folder: str, key: str, to: str | None = None) -> dict:
    """Undo of a pick: allowed only while no view task has started, nothing is frozen and no conversion exists. The views folder is renamed
    `views/.v2_withdrawn_<v>_<time>` FIRST (the runner's guard then refuses to spawn any of its tasks, F10), the task states are checked
    again (a launch reserved meanwhile restores everything and refuses), then the task files are renamed `.json.withdrawn-<time>` and the
    variant_names entry moves to `retired`."""
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
        monster, variant = safe_part(entry['monster'], 'monster'), check_variant(entry['variant'])
        with monster_lock(root, monster):
            _recover_locked(root, mroot, monster, folder)
            canonical_variant(mroot, monster, variant)
            names_path = variant_names_path(mroot, monster)
            names_before = guard.read_bytes_or_none(names_path)
            names = read_variant_names(mroot, monster, strict=True)
            pick_rec = names['variants'].get(variant)
            if not isinstance(pick_rec, dict) or pick_rec.get('by') != 'v2' or pick_rec.get('was') != key:
                raise guard.Conflict(f'{monster}/{variant} was not picked by v2 from {key} (a pick made before v2 is undone by hand)')
            vdir, mdir = views_dir(mroot, monster, variant), models_dir(mroot, monster, variant)
            contained(mroot, vdir, mdir, names_path)
            if mdir.exists():
                raise guard.Conflict(f'{monster}/{variant} has a models folder: the conversion has started')
            vj = read_views_json(vdir)
            if vj and vj.get('frozen'):
                raise guard.Conflict(f'{monster}/{variant}: the views were chosen for conversion')

            def started() -> list[str]:
                # every descriptor of the variant, old undigested names included (phase 5 review row 10)
                current = [st for st in view_states(root, mroot, monster, variant).values() if not st['foreign']]
                names = {st['name'] for st in current}
                other = [view_task_state(root, name) for name in variant_descriptors(root, monster, variant) if name not in names]
                return [st['name'] for st in current + other if task_started(st)]
            if started():
                raise guard.Conflict(f'{monster}/{variant}: a view task has started ({", ".join(started())}); the pick can no longer be undone')
            renamed: list[tuple[Path, Path]] = []
            try:
                if vdir.exists():
                    dst = _withdrawn_name(vdir)
                    os.rename(vdir, dst)
                    renamed.append((dst, vdir))
                late = started()
                if late:
                    raise guard.Conflict(f'{monster}/{variant}: a view task started meanwhile ({", ".join(late)}); the pick stays')
                for task_name in variant_descriptors(root, monster, variant):
                    task = batch_dir(root) / 'tasks' / f'{task_name}.json'
                    if task.exists():
                        dst = task.with_name(f'{task.name}.withdrawn-{stamp()}')
                        os.rename(task, dst)
                        renamed.append((dst, task))
                retired = dict(names.get('retired') or {})
                retired[variant] = dict(pick_rec) | {'retired': now(), 'reason': 'pick undone in v2'}
                names['variants'].pop(variant, None)
                names['retired'] = retired
                _write_names(mroot, monster, names)
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
    `front.*` / `back.*` of a single-variant (`main`) monster; in the monster folder, else in `first round/` (the War Horse base picks,
    F15). {target name in views/<k>/: source path}."""
    pdir = mroot / 'pipeline' / monster
    prefix = '' if variant == 'main' else f'{variant}-'
    pairs = {'front.png': f'{prefix}front.png', 'colours.json': f'{prefix}front.colours.json', 'prompts.json': f'{prefix}front.prompts.json',
             'back.png': f'{prefix}back.png', 'back.colours.json': f'{prefix}back.colours.json', 'back.prompts.json': f'{prefix}back.prompts.json'}
    for folder in (pdir, *(pdir / sub for sub in FLAT_SUBFOLDERS)):
        if (folder / pairs['front.png']).is_file():
            return {dst: folder / src for dst, src in pairs.items() if (folder / src).is_file()}
    return {}


def _approved_concepts_of(mroot: Path, monster: str) -> dict[str, dict]:
    """{variant: {key, folder, entry}} of the concepts whose state says approved as `monster`/<variant> (the migration's mapping too)."""
    folder = folder_for_monster(mroot, monster)
    if not folder:
        return {}
    state = read_state(mroot / 'concepts' / folder, strict=False)
    out = {}
    for key, entry in state['concepts'].items():
        if entry.get('stage') == 'approved' and canon(str(entry.get('monster'))) == canon(monster) and isinstance(entry.get('variant'), str):
            out.setdefault(entry['variant'], {'key': key, 'folder': folder, 'entry': entry})
    return out


def import_sources(mroot: Path, monster: str, names: dict | None = None) -> dict[str, dict]:
    """{variant: {rec, files, how}} of the picks made before v2 that can be imported (no views/ or models/ folder yet, a flat front):
    every variant_names.json variant, plus a variant that only the migration knows (a sha256-identical flat front, F15: its record is made
    from the approved concept). War Horse `<n>-<armour>` variants stay manual (B.1)."""
    names = read_variant_names(mroot, monster) if names is None else names
    candidates = {v: (rec, 'variant_names') for v, rec in names['variants'].items() if isinstance(rec, dict)}
    for variant, info in _approved_concepts_of(mroot, monster).items():
        if variant not in candidates:
            candidates[variant] = ({'was': info['key'], 'concept': f'{info["key"]}-front.png',
                                    'how': f'migration {info["entry"].get("how") or "mapping"}'}, 'migration')
    out = {}
    for variant, (rec, how) in sorted(candidates.items()):
        if not VARIANT.fullmatch(variant) or ARMOUR_VARIANT.fullmatch(variant):
            continue
        if views_dir(mroot, monster, variant).exists() or models_dir(mroot, monster, variant).exists():
            continue
        files = flat_pick_files(mroot, monster, variant)
        if 'front.png' in files:
            out[variant] = {'rec': rec, 'files': files, 'how': how}
    return out


def importable(mroot: Path, monster: str) -> list[str]:
    """Variants picked before v2 (flat files) that have no views/ or models/ folder yet (import_sources)."""
    return list(import_sources(mroot, monster))


def manual_picks(mroot: Path, monster: str) -> dict[str, dict]:
    """{variant: {key, folder, source}} of the concepts approved as a variant that has no views/ or models/ folder and nothing to import
    from (no flat front, or a War Horse armour id): shown as a manual-only card with its source, never hidden (F15)."""
    sources = import_sources(mroot, monster)
    out = {}
    for variant, info in _approved_concepts_of(mroot, monster).items():
        if variant in sources or views_dir(mroot, monster, variant).exists() or models_dir(mroot, monster, variant).exists():
            continue
        try:
            check_variant(variant)
        except ValueError:
            continue
        out[variant] = {'key': info['key'], 'folder': info['folder'], 'source': f'concepts/{info["folder"]}/{info["key"]}-front.png',
                        'reason': 'a War Horse armour variant (manual)' if ARMOUR_VARIANT.fullmatch(variant) else 'no flat front to import'}
    return out


def import_pick(root: Path, monster: str, variant: str, unattended: bool = False) -> dict:
    """The "import pick" path (G.1): `views/<k>/` from the flat files of a variant picked before v2, with the rules of B.2-B.4 and no
    renumbering. A flat back view comes along as back.png (its task is then not needed). Queues the missing extra views. Journaled like a
    pick (F11); the view tasks are written under the monster lock (F10)."""
    root, mroot = Path(root), mroot_of(root)
    monster, variant = safe_part(monster, 'monster'), check_variant(variant)
    if ARMOUR_VARIANT.fullmatch(variant):
        raise ValueError(f'{monster}/{variant}: armour variants (<n>-<armour>) stay manual')
    canonical_variant(mroot, monster, variant)
    with monster_lock(root, monster):
        _recover_locked(root, mroot, monster, None)
        names = read_variant_names(mroot, monster, strict=True)
        vdir, mdir = views_dir(mroot, monster, variant), models_dir(mroot, monster, variant)
        names_path = variant_names_path(mroot, monster)
        contained(mroot, vdir, mdir, names_path)
        if vdir.exists() or mdir.exists():
            raise guard.Conflict(f'{monster}/{variant} already has a views or models folder')
        source = import_sources(mroot, monster, names).get(variant)
        if source is None:
            if variant in names['variants']:
                raise ValueError(f'{monster}/{variant} has no flat front image')
            raise ValueError(f'{monster}/{variant} is not a picked variant (variant_names.json)')
        files, rec = source['files'], source['rec']
        tmp = vdir.parent / f'.v2_pick_{variant}_{uuid.uuid4().hex[:8]}'
        tmp.mkdir(parents=True)
        names_before = guard.read_bytes_or_none(names_path)
        names_prev = names['variants'].get(variant)
        names_entry = dict(rec) | {'imported': now(), 'imported_by': 'v2'}
        moved = journaled = False
        try:
            for dst, src in files.items():
                shutil.copyfile(src, tmp / dst)
            views = [{'name': 'front', 'enabled': True}] + ([{'name': 'back', 'enabled': True}] if 'back.png' in files else [])
            write_views_json(tmp, {'views': views, 'unattended': bool(unattended), 'frozen': None,
                                   'planned': [v for v in EXTRA_VIEWS if view_file(v) not in files], 'created': now(), 'by': 'v2',
                                   'pick_gen': uuid.uuid4().hex,
                                   'imported': {dst: src.relative_to(mroot / 'pipeline' / monster).as_posix() for dst, src in files.items()}})
            _write_journal(root, monster, {'op': 'import', 'monster': monster, 'variant': variant, 'tmp': tmp.name,
                                           'names_entry': names_entry, 'names_prev': names_prev})
            journaled = True
            names['variants'][variant] = names_entry
            _write_names(mroot, monster, names)
            _fault('import_names')
            os.rename(tmp, vdir)
            moved = True
        except Exception:
            _restore(names_path, names_before)
            if moved and vdir.exists():
                try:
                    os.rename(vdir, tmp)
                except OSError:
                    pass
            if tmp.exists():
                _rmtree_own(tmp)                                # our own temp folder
            if journaled and not vdir.exists():
                _drop_journal(root, monster)
            raise
        warning = None
        try:
            tasks = queue_view_tasks(root, mroot, monster, variant)
        except (OSError, ValueError, guard.Conflict) as exc:
            tasks, warning = [], f'the view tasks could not be written ({exc}); use Retry on each view'
        _drop_journal(root, monster)
    concept = _mark_concept_of_pick(root, mroot, monster, variant, names_entry)
    out = {'ok': True, 'monster': monster, 'variant': variant, 'id': f'monster:{monster}/{variant}', 'copied': sorted(files),
           'tasks': tasks, 'concept': concept, 'library': library_info(mroot, monster), 'undo': None, 'how': source['how']}
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

class Unverified(guard.Conflict):
    """3D approve of a reduction the lane did not verify (no generation-bound v2_reduce_ok.json, F12). HTTP 409."""
    code = 'unverified'


def _variant_paths(root: Path, item_id: str) -> tuple[Path, str, str, Path, Path]:
    """(monster root, monster, variant, views dir, models dir) of a `monster:` id, resolved against the monster root at mutation time:
    exact on-disk names, contained paths, no link or junction in between (F13)."""
    mroot = mroot_of(root)
    monster, variant = parse_monster_id(item_id)
    canonical_variant(mroot, monster, variant)
    vdir, mdir = views_dir(mroot, monster, variant), models_dir(mroot, monster, variant)
    contained(mroot, vdir, mdir)
    damage_check(mroot, mdir)
    blocked = journal_blocks(root, monster, variant)             # an unusable publication journal about this variant (row 11)
    if blocked:
        raise guard.Unreadable(blocked)
    return mroot, monster, variant, vdir, mdir


def damage_check(mroot: Path, mdir: Path) -> None:
    """Every mutation of a variant (and its card) reads its recovery / choice files STRICTLY first (finding 11): a malformed
    `chosen_lod.json` or `.v2_reconvert.json` raises guard.Unreadable, so the variant is a read-only damaged card and the file is
    neither overwritten nor removed. A well-formed reconvert journal is fine (recovery finishes it)."""
    mdir = Path(mdir)
    if not mdir.is_dir():
        return
    contained(mroot, mdir / CHOICE, mdir / RECONVERT_JOURNAL)
    read_choice(mdir)
    read_reconvert_journal(mdir)


def _enabled_arg(raw: object) -> dict[str, bool]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError('args.enabled must map view names (front, left, back, right) to true / false')
    for name, value in raw.items():
        if name not in VIEW_ORDER or not isinstance(value, bool):
            raise ValueError('args.enabled must map view names (front, left, back, right) to true / false')
    return dict(raw)


def _files_arg(raw: object) -> list[str]:
    """args.files of convert / reconvert: the exact ordered list of the ticked view files (1 to 4 of front.png, left.png, back.png,
    right.png, no repeats). It is authoritative: nothing else is merged in (phase 5 review F8)."""
    allowed = [view_file(n) for n in VIEW_ORDER]
    if not isinstance(raw, list) or not raw or any(not isinstance(f, str) or f not in allowed for f in raw) or len(set(raw)) != len(raw):
        raise ValueError('args.files must be the ticked view files in upload order (1 to 4 of front.png, left.png, back.png, right.png)')
    return list(raw)


def _draft(vdir: Path, vj: dict | None) -> dict:
    """views.json of a variant, created in memory for a legacy variant (every available view listed, attended)."""
    if vj is not None:
        return vj
    return {'views': [{'name': n, 'enabled': True} for n in available_views(vdir)], 'unattended': False, 'frozen': None,
            'created': now(), 'by': 'v2', 'legacy': True}


NO_CHOICE = 'none'                 # choice_rev of a variant without a chosen_lod.json


def choice_rev(raw: bytes | None) -> str:
    """The APPROVAL REVISION of a variant (post-cut-over review, blocker 5): a digest of the bytes of its chosen_lod.json (`c:` + 32 hex;
    `none` without a file). Every write of the file (Approve, Unapprove, an LOD pick) makes a new one: an approval step stores a fresh
    `approval_id`, so approving the same level again is a new revision too. The item shows it (`approval_rev`); Unapprove and every
    Undo of an approval step echo it, and it is compared under the variant lock (write_choice)."""
    return NO_CHOICE if raw is None else 'c:' + hashlib.sha256(raw).hexdigest()[:32]


def choice_snapshot(mdir: Path) -> tuple[dict, str]:
    """(chosen_lod.json, its choice_rev) from ONE read of the file ({} and `none` when absent). An existing file that cannot be read is
    NEVER treated as empty (guard.Unreadable, HTTP 409)."""
    path = Path(mdir) / CHOICE
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, NO_CHOICE
    except OSError as exc:
        raise guard.Unreadable(f'{CHOICE}: cannot be read: {exc}') from None
    try:
        value = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, ValueError) as exc:
        raise guard.Unreadable(f'{CHOICE}: not valid JSON: {exc}') from None
    if not isinstance(value, dict):
        raise guard.Unreadable(f'{CHOICE}: not an object')
    return value, choice_rev(raw)


def read_choice(mdir: Path) -> dict:
    """chosen_lod.json ({} when absent); strict (choice_snapshot)."""
    return choice_snapshot(mdir)[0]


def set_ticks(root: Path, item_id: str, enabled: object) -> dict:
    """Verb `views`: the Enabled ticks (a draft; freezing is `convert`). Ticks after a freeze make a new draft (Reconvert uses them)."""
    mroot, monster, variant, vdir, _mdir = _variant_paths(root, item_id)
    enabled = _enabled_arg(enabled)
    if not vdir.is_dir():
        raise not_found(f'{monster}/{variant} has no views folder')
    with monster_lock(root, monster):
        raw = read_views_json(vdir)
        vj = _draft(vdir, raw)
        available = available_views(vdir)
        missing = [n for n in enabled if n not in available]
        if missing:
            raise ValueError(f'no such valid view image: {", ".join(missing)}')
        before = ticks_of(vj, available)
        ticks = before | enabled
        vj['views'] = [{'name': n, 'enabled': ticks[n]} for n in VIEW_ORDER if n in ticks]
        write_views_json(vdir, vj)
        rev = views_rev(vdir, vj)
    frozen = vj.get('frozen') if isinstance(vj.get('frozen'), dict) else None
    differs = frozen is not None and [n for n in VIEW_ORDER if ticks.get(n)] != [str(f)[:-4] for f in frozen.get('files') or []]
    return {'ok': True, 'ticks': ticks, 'draft_differs': differs, 'views_rev': rev,
            'undo': {'verb': 'views', 'id': item_id, 'args': {'enabled': {n: before[n] for n in enabled if n in before}}}}


def _owner(root: Path, monster: str, variant: str) -> str | None:
    """Why a conversion of the variant is active (a queued / running lane task, a live driver, a live child of a previous attempt), else
    None. An unreadable queue raises guard.Unreadable: it never means "nothing is running" (F6)."""
    import monster_lane
    return monster_lane.active_reason(root, monster, variant)


# Reconvert (F6) moves hyper3d/ and reduced/ aside as ONE journaled transaction: `models/<v>/.v2_reconvert.json` {moves: [[sub, dst]],
# generation: the new frozen digest} is written before the first rename and deleted after views.json carries the new freeze. Recovery
# (any later mutation of the variant, and the orchestrator tick) completes a journal whose generation views.json already has, and rolls
# the moves back otherwise.

def read_reconvert_journal(mdir: Path) -> dict | None:
    """`models/<v>/.v2_reconvert.json`, read STRICTLY (finding 11): None when absent; an unreadable or malformed journal raises
    guard.Unreadable and stays (it is the only record of which folders a Reconvert moved)."""
    j = read_strict(Path(mdir) / RECONVERT_JOURNAL, f'{RECONVERT_JOURNAL} of {Path(mdir).name}')
    if j is None:
        return None
    moves = j.get('moves')
    ok = isinstance(moves, list) and all(isinstance(m, list) and len(m) == 2 and m[0] in ('hyper3d', 'reduced') and
                                         isinstance(m[1], str) and re.fullmatch(r'(hyper3d|reduced)_prev_[0-9T_]+', m[1]) for m in moves)
    if not ok or not isinstance(j.get('generation'), str):
        raise guard.Unreadable(f'{RECONVERT_JOURNAL} of {Path(mdir).name}: not a reconvert journal; check the hyper3d / reduced folders '
                               'by hand before removing it')
    return j


def _recover_reconvert_locked(mdir: Path, vdir: Path) -> str | None:
    mdir = Path(mdir)
    path = mdir / RECONVERT_JOURNAL
    j = read_reconvert_journal(mdir)
    if j is None:
        return None
    moves = [(str(m[0]), str(m[1])) for m in j['moves']]
    vj = read_views_json(vdir)
    done = vj is not None and frozen_digest(vj.get('frozen')) == j.get('generation')
    for sub, dst in moves:
        src, aside = mdir / sub, mdir / dst
        if len(mdir.parents) > 3:
            contained(mdir.parents[3], src, aside)               # the recovery targets, right before they are used (finding 14)
        if done and src.exists() and not aside.exists():
            os.rename(src, aside)                                # complete a move the crash interrupted
        elif not done and aside.exists() and not src.exists():
            os.rename(aside, src)                                # roll back
    path.unlink(missing_ok=True)
    return 'completed' if done else 'rolled back'


def recover_reconverts(root: Path) -> list[str]:
    """HOOK (monster_lane.tick): finish or roll back every interrupted Reconvert (each under its monster and LOD locks)."""
    root, mroot = Path(root), mroot_of(root)
    out = []
    for monster in pipeline_monsters(mroot):
        for variant in _dirs(mroot / 'pipeline' / monster / 'models'):
            mdir = models_dir(mroot, monster, variant)
            if not (mdir / RECONVERT_JOURNAL).exists():
                continue
            try:
                contained(mroot, mdir, mdir / LOD_LOCK, mdir / RECONVERT_JOURNAL)     # BEFORE the lock file is opened (finding 14)
                with monster_lock(root, monster), guard.os_lock(mdir / LOD_LOCK):
                    done = _recover_reconvert_locked(mdir, views_dir(mroot, monster, variant))
                if done:
                    out.append(f'{monster}/{variant}: reconvert {done}')
            except Exception as exc:  # noqa: BLE001
                out.append(f'{monster}/{variant}: reconvert recovery error {type(exc).__name__}: {exc}')
    return out


def convert(root: Path, item_id: str, args: dict, reconvert: bool = False) -> dict:
    """Verb `convert` ("Use enabled views") and `reconvert` (3D tab, paid): freeze views.json {time, files, sha256} with EXACTLY
    `args.files` (in that order) and queue the conversion (monster_lane). `args.views_rev` must be the item's `views_rev` (409 `changed`
    otherwise); a listed file that is absent or invalid is 409 `missing`; `args.enabled` is refused (400). `convert` refuses once a
    conversion record / GLB / reduced/ exists (409 `frozen`); `reconvert` needs `args.confirm: true`, validates the new freeze first, then
    (under the monster lock AND the LOD lock of 3D approve) moves hyper3d/ and reduced/ aside to `*_prev_<time>` as one journaled
    transaction. Both refuse while a conversion of the variant is active (409 `busy`) or the queue cannot be read (409 `unreadable`)."""
    import monster_lane
    mroot, monster, variant, vdir, mdir = _variant_paths(root, item_id)
    if not vdir.is_dir():
        raise not_found(f'{monster}/{variant} has no views folder')
    if 'enabled' in args:
        raise ValueError('args.enabled is not accepted here: send args.files (the ticked files in upload order) and args.views_rev')
    files = _files_arg(args.get('files'))
    rev = args.get('views_rev')
    if isinstance(rev, bool) or not isinstance(rev, int):
        raise ValueError('args.views_rev is required: the views_rev of the item the choice was made on')
    if reconvert and args.get('confirm') is not True:
        raise ValueError('Reconvert pays for a new Hyper3D conversion (0.5 credits): send args.confirm = true')
    moved: list[str] = []
    with monster_lock(root, monster):
        contained(mroot, vdir / VIEWS_JSON, mdir / LOD_LOCK, mdir / RECONVERT_JOURNAL, mdir / 'hyper3d', mdir / 'reduced')   # finding 14
        lod_lock = guard.os_lock(mdir / LOD_LOCK) if reconvert and mdir.is_dir() else None
        if lod_lock is not None:
            lod_lock.__enter__()
        try:
            if mdir.is_dir():
                _recover_reconvert_locked(mdir, vdir)
            raw = read_views_json(vdir)
            owner = _owner(root, monster, variant)
            if owner:
                raise guard.Busy(f'{monster}/{variant}: {owner}')
            current = views_rev(vdir, raw)
            if rev != current:
                raise guard.Changed(f'the views of {monster}/{variant} changed since they were shown; look at them again',
                                    changes=['views'])
            missing = [f for f in files if view_problem(vdir, f[:-4])]
            if missing:
                raise Missing(f'not a valid view of {monster}/{variant}: ' + ', '.join(f'{f} ({view_problem(vdir, f[:-4])})' for f in missing),
                              file=missing[0], files=missing)
            vj = _draft(vdir, raw)
            if reconvert:
                if lod_lock is None:
                    raise ValueError(f'{monster}/{variant} has no conversion to redo; use Use enabled views')
                if read_choice(mdir).get('approved') is True:
                    raise guard.AlreadyApproved(f'{monster}/{variant} is approved; unapprove it first')
                if not conversion_started(mdir):
                    raise ValueError(f'{monster}/{variant} has no conversion to redo; use Use enabled views')
            elif conversion_started(mdir):
                raise Frozen(f'{monster}/{variant} has a conversion already; converting again is Reconvert (3D tab, paid)')
            frozen = freeze(vdir, files)                       # validated and hashed BEFORE anything moves (F6)
            moves = []
            if reconvert:
                taken = set(_names(mdir))
                for sub in ('hyper3d', 'reduced'):
                    if (mdir / sub).exists():
                        dst, n = f'{sub}_prev_{stamp()}', 2
                        while dst in taken:
                            dst, n = f'{sub}_prev_{stamp()}_{n}', n + 1
                        taken.add(dst)
                        moves.append([sub, dst])
                moved = [dst for _sub, dst in moves]
                frozen |= {'reconvert': True, 'moved': moved}
            vj['views'] = [{'name': n, 'enabled': view_file(n) in files} for n in VIEW_ORDER if n in available_views(vdir)]
            vj['frozen'] = frozen
            if reconvert:
                journal = mdir / RECONVERT_JOURNAL
                # every source and destination of the moves, resolved again right before use; no link or junction on the way (finding 14)
                contained(mroot, journal, *(mdir / sub for sub, _dst in moves), *(mdir / dst for _sub, dst in moves))
                if journal.exists():
                    raise guard.Recovering(f'{monster}/{variant}: an earlier Reconvert is not resolved yet')
                guard.write_atomic(journal, dump({'schema': 1, 'moves': moves, 'generation': frozen_digest(frozen), 'time': now()}))
                done: list[tuple[str, str]] = []
                try:
                    for sub, dst in moves:
                        os.rename(mdir / sub, mdir / dst)
                        done.append((sub, dst))
                        _fault('reconvert_moved')
                    write_views_json(vdir, vj)
                except Exception:
                    for sub, dst in reversed(done):
                        try:
                            os.rename(mdir / dst, mdir / sub)
                        except OSError:
                            raise                            # the journal stays: recovery rolls back what is left
                    journal.unlink(missing_ok=True)
                    raise
                journal.unlink(missing_ok=True)
            else:
                write_views_json(vdir, vj)
        finally:
            if lod_lock is not None:
                lod_lock.__exit__(None, None, None)
    warning = None
    try:
        task = monster_lane.enqueue(root, monster, variant, vj['frozen'])
    except Exception as exc:  # noqa: BLE001  (the freeze stands; the lane tick queues it again)
        task, warning = None, f'the conversion could not be queued yet ({type(exc).__name__}: {exc}); the lane queues it on its next pass'
    out = {'ok': True, 'monster': monster, 'variant': variant, 'frozen': vj['frozen'], 'task': task, 'moved': moved, 'undo': None,
           'views_rev': views_rev(vdir, vj)}
    if warning:
        out['warning'] = warning
    return out


def write_choice(root: Path, item_id: str, approved: bool, lod: object = None, glb_sha256: object = None,
                 approval_rev: object = None) -> dict:
    """3D Approve / Completed Unapprove: chosen_lod.json {lod, reduced, approved, glb_sha256, generation, legacy, updated, approval_id}
    under the monster lock and the variant's `.pipeline_v2.lock` (the lock of decisions.write_monster_lod and of Reconvert, F6). Approve
    needs `lod` and the `glb_sha256` the modal showed (item `lods[lod].sha256`: 409 `changed` when the file is another one now), a
    reduction the lane verified (or a legacy one by the compatibility rule; 409 `unverified` otherwise) and no active conversion (409
    `busy`).

    `approval_rev` (post-cut-over review, blocker 5) = the approval revision the client DISPLAYED (item `approval_rev`, or the
    `approval_rev` of the answer an Undo reverses; choice_rev). It is compared with the file as it is NOW, under the variant lock, before
    anything else: 409 `changed` when the record was written since, so a stale tab's Unapprove never clears a LATER approval and a stale
    Undo never approves over a later decision. Unapprove REQUIRES it (400 without); Approve compares it when given (the 3D modal's
    Approve is bound to the model bytes on screen instead; the Undo of an Unapprove sends it). The answer carries the `approval_rev`
    of the record just written."""
    if approval_rev is not None and (not isinstance(approval_rev, str) or not approval_rev):
        raise ValueError('args.approval_rev must be the approval revision shown (item approval_rev)')
    if not approved and approval_rev is None:
        raise ValueError('args.approval_rev is required: the approval revision of the item the Unapprove was made on (item approval_rev)')
    mroot, monster, variant, vdir, mdir = _variant_paths(root, item_id)
    if not mdir.is_dir():
        raise not_found(f'{monster}/{variant} has no models folder')
    contained(mroot, mdir / LOD_LOCK, mdir / CHOICE, mdir / RECONVERT_JOURNAL, mdir / 'reduced')      # before the lock is opened (finding 14)
    with monster_lock(root, monster), guard.os_lock(mdir / LOD_LOCK):
        _recover_reconvert_locked(mdir, vdir)
        record, rev_now = choice_snapshot(mdir)
        if approval_rev is not None and approval_rev != rev_now:
            is_now = f'approved at LOD {record.get("lod")}' if record.get('approved') is True else 'not approved'
            raise guard.Changed(f'{monster}/{variant}: the approval changed since you were shown it (it is {is_now} now); nothing was '
                                'changed, look again', changes=['approval'])
        previous = {'lod': record.get('lod'), 'approved': record.get('approved') is True}
        if approved:
            if record.get('approved') is True:
                raise guard.AlreadyApproved(f'{monster}/{variant} is approved already')
            if lod not in LOD_LEVELS:
                raise ValueError('args.lod (the level shown) is required')
            if not isinstance(glb_sha256, str) or not glb_sha256:
                raise ValueError('args.glb_sha256 (the digest of the level shown, item lods[].sha256) is required')
            owner = _owner(root, monster, variant)
            if owner:
                raise guard.Busy(f'{monster}/{variant}: {owner}')
            vj = read_views_json(vdir)
            generation = frozen_digest(vj.get('frozen')) if vj and vj.get('frozen') else None
            status = reduce_status(mdir, generation, deep=True)
            if not status['ok']:
                raise Unverified(f'{monster}/{variant}: the reduction is not verified ({status["problem"]})')
            path = mdir / 'reduced' / f'lod_error_{lod}.glb'
            contained(mroot, path)
            if not path.is_file():
                raise ValueError('that LOD has no file')
            actual = sha256_file(path)
            if actual != glb_sha256:
                raise guard.Changed(f'{monster}/{variant}: lod_error_{lod}.glb is not the model you looked at any more; look again',
                                    changes=[f'lod {lod}'])
            record |= {'lod': lod, 'reduced': f'reduced/lod_error_{lod}.glb', 'approved': True, 'glb_sha256': actual,
                       'generation': generation, 'legacy': bool(status.get('legacy'))}
        else:
            if record.get('approved') is not True:
                raise ValueError(f'{monster}/{variant} is not approved')
            record['approved'] = False
        record['updated'] = datetime.now(timezone.utc).isoformat()
        record['approval_id'] = uuid.uuid4().hex               # every approval step is a new revision (also the same level again)
        written = json.dumps(record, indent=1).encode('utf-8')
        guard.write_atomic(mdir / CHOICE, written)
    level = record.get('lod')
    digest = record.get('glb_sha256') if approved else (file_sha256(mdir / 'reduced' / f'lod_error_{level}.glb') if level in LOD_LEVELS
                                                      else None)
    return {'ok': True, 'lod': level, 'approved': record['approved'], 'previous': previous, 'glb_sha256': digest,
            'approval_rev': choice_rev(written)}


def view_retry(root: Path, item_id: str, view: object) -> dict:
    """Verb `view_retry` (args.view): a FAILED / cancelled view task, or a finished one whose IMAGE is invalid (png_check: not a
    decodable RGBA PNG with enough transparent and opaque pixels), goes back to pending with attempts 0 (its jobs kept); the invalid image
    and its record are first renamed `<file>.invalid-<time>` (the job never overwrites a file). A missing task file (never written) is
    written. Refused while the task is still running, while the view is valid, and when the image is valid and only its provenance record
    is missing / invalid: that is `view_repair`, which draws nothing again (finding 15)."""
    mroot, monster, variant, vdir, _mdir = _variant_paths(root, item_id)
    if view not in EXTRA_VIEWS:
        raise ValueError(f'view must be one of {list(EXTRA_VIEWS)}')
    if not (vdir / 'front.png').is_file():
        raise not_found(f'{monster}/{variant} has no front view')
    with monster_lock(root, monster):
        if read_views_json(vdir) is None:
            raise ValueError(f'{monster}/{variant} has no {VIEWS_JSON} (a legacy variant has no view tasks)')
        name = view_task_name(monster, variant, view)
        st = view_task_state(root, name, (monster, variant))
        if st['foreign']:
            raise guard.Conflict(f'{name}.json belongs to another monster / variant')
        problem = view_problem(vdir, view)
        if problem is None:
            raise ValueError(f'{view}.png exists already and is valid')
        if view_repairable(vdir, view):
            raise ValueError(f'{view}.png is a valid image; only its provenance record is wrong ({problem}): use Repair provenance '
                             '(verb view_repair), nothing is drawn again')
        if st['exists'] and st['state'] not in TERMINAL_VIEW_STATES:
            raise ValueError(f'{name} is {st["state"]}; only a failed, cancelled or finished task can be retried')
        aside = []
        for fname in (view_file(view), monster_prompts.record_name(view)):
            path = vdir / fname
            if path.exists():
                dst = vdir / f'{fname}.invalid-{stamp()}'
                contained(mroot, path, dst)
                os.rename(path, dst)
                aside.append(dst.name)
        if not st['exists']:
            written = queue_view_tasks(root, mroot, monster, variant, (view,))
            return {'ok': True, 'name': name, 'state': 'pending', 'written': bool(written), 'moved_aside': aside, 'problem': problem,
                    'undo': None}
        _ensure_pick_gen(vdir)
        path = batch_dir(root) / 'status' / f'{name}.json'
        status = read_obj(path)
        before = status.get('attempts')
        status.update(state='pending', attempts=0)
        status.pop('error', None)
        status.pop('reason', None)
        guard.write_atomic(path, dump(status))
    return {'ok': True, 'name': name, 'state': 'pending', 'attempts_before': before, 'moved_aside': aside, 'problem': problem, 'undo': None}


def _task_prompt(root: Path, name: str, st: dict) -> tuple[str | None, str | None]:
    """(prompt text, where it came from) of a view task: the descriptor's `prompt`, else the last job's `.codex_jobs/<job>/prompt.md`."""
    desc = read_obj(batch_dir(root) / 'tasks' / f'{name}.json')
    if isinstance(desc.get('prompt'), str) and desc['prompt'].strip():
        return desc['prompt'], f'.codex_jobs/_batch/tasks/{name}.json'
    job = st.get('job')
    if isinstance(job, str) and job and '/' not in job and '\\' not in job and not job.startswith('.'):
        path = Path(root) / '.codex_jobs' / job / 'prompt.md'
        try:
            text = path.read_text(encoding='utf-8')
        except OSError:
            text = ''
        if text.strip():
            return text, f'.codex_jobs/{job}/prompt.md'
    return None, None


def view_repair(root: Path, item_id: str, view: object) -> dict:
    """Verb `view_repair` (args.view), the UI's "Repair provenance" (finding 15): the view's IMAGE is valid (png_check) but its provenance
    record `<view>.prompts.json` is missing or not a JSON object. The record is written again from the task's prompt (the task descriptor's
    `prompt`, else the last job's `prompt.md`, else the prompt v2 writes for this view now); the image is neither regenerated nor touched.
    An unusable record file is first renamed `<file>.invalid-<time>` (never overwritten). Refused while the view's task still runs, when
    the image itself is invalid (that is Retry) and when the record is valid already."""
    mroot, monster, variant, vdir, _mdir = _variant_paths(root, item_id)
    if view not in EXTRA_VIEWS:
        raise ValueError(f'view must be one of {list(EXTRA_VIEWS)}')
    with monster_lock(root, monster):
        if read_views_json(vdir) is None:
            raise ValueError(f'{monster}/{variant} has no {VIEWS_JSON} (a legacy variant has no view tasks)')
        image, record, front = vdir / view_file(view), vdir / monster_prompts.record_name(view), vdir / 'front.png'
        contained(mroot, image, record)
        if _is_link(image) or _is_link(record):
            raise ValueError(f'{view}: a view file is a link; refusing')
        problem = png_problem(image) if image.is_file() else 'no image'
        if problem:
            raise ValueError(f'{view}.png is not a valid image ({problem}); use Retry to draw it again')
        why = provenance_problem(vdir, view)
        if why is None:
            raise ValueError(f'{record.name} is a valid provenance record already')
        name = view_task_name(monster, variant, view)
        st = view_task_state(root, name, (monster, variant))
        mine = st['exists'] and not st['foreign']
        if mine and st['state'] not in TERMINAL_VIEW_STATES:
            raise ValueError(f'{name} is {st["state"]}; the record is repaired once the task has finished')
        prompt, source = _task_prompt(root, name, st) if mine else (None, None)
        if prompt is None:
            prompt = monster_prompts.job_prompt(view, monster, variant, front, image, read_obj(vdir / 'colours.json'))
            source = 'written again by v2 (no task descriptor or job prompt was found)'
        size = None
        try:
            from PIL import Image
            with Image.open(image) as img:
                size = list(img.size)
        except Exception:  # noqa: BLE001  (png_problem passed; the size is only a note)
            pass
        aside = None
        if record.exists():
            dst = vdir / f'{record.name}.invalid-{stamp()}'
            contained(mroot, dst)
            os.rename(record, dst)
            aside = dst.name
        data = {'view': view, 'monster': monster, 'variant': variant, 'source_image': front.as_posix(),
                'prompt': monster_prompts.image_prompt(view, monster, read_obj(vdir / 'colours.json')), 'job_prompt': prompt,
                'reference_paths': [front.as_posix()], 'date': now(), 'job_name': st.get('job') if mine else None,
                'attempt_count': st.get('attempts') if mine else None, 'generated_file': None,
                'image_processing': 'unknown: this record was written again by the v2 preview app (repair provenance); the image was not '
                                    'regenerated or changed',
                'saved_pixel_size': size, 'png_mode': 'RGBA', 'sha256': sha256_file(image),
                'repaired': {'by': 'v2 view_repair', 'time': now(), 'reason': why, 'prompt_source': source, 'previous_record': aside}}
        guard.write_atomic(record, dump(data))
    return {'ok': True, 'view': view, 'record': record.name, 'moved_aside': aside, 'prompt_source': source, 'reason': why, 'undo': None}


def disposition_of(vj: dict | None, generation: str | None) -> dict | None:
    """The persisted outcome of a frozen generation's conversion (views.json `conversion[<frozen digest>]`: {state: cancelled | failed,
    reason, key, time, seq}) or None. It lives OUTSIDE the rebuildable queue (phase 5 review F4): a rebuilt queue never revives it. An
    entry that is not a readable disposition raises guard.Unreadable (finding 3): an indeterminate outcome is never read as "none"."""
    conv = (vj or {}).get('conversion')
    entry = conv.get(generation) if isinstance(conv, dict) and generation else None
    if entry is None:
        return None
    problem = disposition_problem(generation, entry)
    if problem:
        raise guard.Unreadable(f'{VIEWS_JSON}: conversion[{generation!r}]: {problem}')
    return entry


def disposition_seq(vj: dict | None) -> int:
    """The variant's monotonic disposition counter (views.json `conversion_seq`; an older file: the highest entry `seq`, else 0)."""
    seq = (vj or {}).get('conversion_seq')
    if isinstance(seq, int) and not isinstance(seq, bool):
        return seq
    entries = ((vj or {}).get('conversion') or {}).values()
    return max([e['seq'] for e in entries if isinstance(e, dict) and isinstance(e.get('seq'), int)] or [0])


def attempt_ref(token: object) -> str | None:
    """The id a disposition names its queue ATTEMPT by (`attempt`): a digest of the attempt's token (the token itself is never
    stored in views.json or published). None without a token."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()[:16] if isinstance(token, str) and token else None


def outcome_error(disposition: dict | None, row_state: object, row_error: object, attempt: object = None) -> object:
    """The error text a failed / cancelled lane row is PUBLISHED with, next to `conversion_seq` (final-live review, blocker 4): the
    row and views.json are two reads, so a row read before a Retry + second failure would otherwise be shown with the revision of the
    second failure (and its Retry would clear a failure the user never saw). Row and revision are therefore shown as ONE outcome:
    the text shown always belongs to the revision sent. The reason STORED WITH the disposition is published; the row's own (possibly
    longer) text is kept only when the row is PROVABLY that outcome: the same state and either the text is identical, or the
    disposition names the row's attempt (`attempt` = the row's token; the disposition's `attempt` = attempt_ref of it, written by
    monster_lane.record_failure) and the recorded reason is the beginning of that text. Confirm review ("Earlier 4, Retry"): a text that merely STARTS with the recorded reason is not that
    outcome (the older "collect failed: old material step" was shown with the revision of the newer "collect failed")."""
    if not isinstance(disposition, dict):
        return row_error
    state, reason = disposition.get('state'), str(disposition.get('reason') or disposition.get('state') or '')
    if state == 'cancelled':
        return row_error if row_state == 'cancelled' else f'cancelled: {reason}'
    if row_state != state:
        return f'{state}: {reason}'
    text, recorded = str(row_error or ''), disposition.get('attempt')
    if text == reason:
        return row_error
    # a longer text: only that of the very attempt the disposition names, and only when the recorded reason is its beginning
    same_attempt = recorded is not None and recorded == attempt_ref(attempt)
    return row_error if same_attempt and (text[:300] == reason or (reason and text.startswith(reason))) else reason


def set_disposition(root: Path, monster: str, variant: str, generation: str, value: dict | None, expect_seq: int | None = None,
                    only_absent: bool = False) -> dict:
    """Write (value) or clear (None) the disposition of a frozen generation under the monster lock, as ONE read-compare-write (finding 3).
    Every change bumps the variant's monotonic counter `conversion_seq`, and an entry carries the `seq` it was written with.
      - writing the state the generation already has changes nothing (an identical terminal transition is idempotent);
      - `expect_seq`: the change is made only while the counter still has that value, so undoing one's own write can never put an older
        disposition over a newer one (`stale: true` otherwise);
      - `only_absent` (a write): made only while the generation has NO disposition; one of another state stays (`conflict: true`,
        `previous` = it). This is the Cancel's compare-and-set (final-verify row 3): a stale Cancel never replaces a recorded failure.
    Returns {previous, seq (the counter now), written, same?, stale?, conflict?, missing? (the variant has no views.json: nothing to
    record)}."""
    mroot = mroot_of(root)
    monster, variant = safe_part(monster, 'monster'), check_variant(variant)
    if not isinstance(generation, str) or not GENERATION.fullmatch(generation):
        raise ValueError(f'invalid generation {generation!r}')
    if value is not None and (not isinstance(value, dict) or value.get('state') not in DISPOSITION_STATES):
        raise ValueError(f'a disposition needs a state of {list(DISPOSITION_STATES)}')
    vdir = views_dir(mroot, monster, variant)
    with monster_lock(root, monster):
        contained(mroot, vdir, vdir / VIEWS_JSON)
        vj = read_views_json(vdir)
        if vj is None:
            return {'previous': None, 'seq': 0, 'written': False, 'missing': True}
        conv = dict(vj.get('conversion') or {})
        seq = disposition_seq(vj)
        previous = conv.get(generation)
        if expect_seq is not None and seq != expect_seq:
            return {'previous': previous, 'seq': seq, 'written': False, 'stale': True}
        if value is None:
            if generation not in conv:
                return {'previous': None, 'seq': seq, 'written': False}
            conv.pop(generation, None)
        else:
            if isinstance(previous, dict) and previous.get('state') == value['state']:
                return {'previous': previous, 'seq': seq, 'written': False, 'same': True}
            if only_absent and previous is not None:
                return {'previous': previous, 'seq': seq, 'written': False, 'conflict': True}
            conv[generation] = {k: v for k, v in value.items() if k not in ('seq', 'time')} | {'time': now(), 'seq': seq + 1}
        seq += 1
        vj['conversion'], vj['conversion_seq'] = conv, seq
        _fault('disposition_write')
        write_views_json(vdir, vj)
    return {'previous': previous, 'seq': seq, 'written': True}


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
    if verb == 'view_repair':
        return view_repair(root, item_id, args.get('view'))
    if verb == 'approve':
        result = write_choice(root, item_id, True, args.get('lod'), args.get('glb_sha256'), args.get('approval_rev'))
        # the Undo names the approval it reverses (blocker 5): it never clears a later one
        return result | {'stage': 'completed', 'undo': {'verb': 'unapprove', 'id': item_id, 'args': {'approval_rev': result['approval_rev']}}}
    # unapprove: completed -> 3d; a variant v2 picked whose views have not started -> the pick is undone
    mroot, monster, variant, _vdir, mdir = _variant_paths(root, item_id)
    # An Unapprove that names an approval revision (the Completed modal, the Undo of an Approve) is about the APPROVAL only: when the
    # variant is not approved any more it answers 409 `changed` (write_choice) and never falls through to undoing the pick. One of an
    # approved variant WITHOUT a revision is refused by write_choice (400). (blocker 5)
    if mdir.is_dir() and (args.get('approval_rev') is not None or read_choice(mdir).get('approved') is True):
        result = write_choice(root, item_id, False, approval_rev=args.get('approval_rev'))
        return result | {'stage': '3d', 'undo': {'verb': 'approve', 'id': item_id,
                                                 'args': {'lod': result['lod'], 'glb_sha256': result['glb_sha256'],
                                                          'approval_rev': result['approval_rev']}}}
    rec = read_variant_names(mroot, monster, strict=True)['variants'].get(variant)
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
    """approved AND a verified reduction (the lane's generation-bound record, or a legacy result by the compatibility rule, F12) ->
    completed; reduced LODs -> 3d; otherwise approved (in progress). An approval whose reduction is not verified stays in 3d."""
    if raw.get('approved') and raw.get('verified', True) and raw.get('lods'):
        return 'completed'
    return '3d' if raw.get('lods') else 'approved'


def _task_view(task: dict | None) -> dict | None:
    if not task:
        return None
    return {'id': task['id'], 'state': task['state'], 'attempts': task['attempts'], 'error': task.get('error'),
            'started': task.get('started'), 'finished': task.get('finished'), 'not_before': task.get('not_before'),
            'run_now': bool((task.get('args') or {}).get('run_now')), 'key': task['key'],
            'generation': (task.get('args') or {}).get('frozen_sha'),
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
                    with_gate: bool = True, vj: dict | None = None) -> dict | None:
    """`item.conversion` (web/monster_views.js): None before anything was chosen / converted, else
    {task_id, state: queued | waiting | running | done | failed | cancelled, step: submit | preview | collect | reduce, waiting_reason (held |
    paused | user_present | browser_busy | a text) while waiting, paid (a CONFIRMED paid action), paid_uncertain (a paid click whose outcome
    is not confirmed), credits (the confirmed observed cost of the charged steps; never assumed), free_steps (confirmed steps proven free), error, attempts, run_now, record {status, stage, pending_action,
    credits_committed, why, model_url, images_uploaded, actions}, screenshots [urls, newest first], reduced (verified for this freeze),
    reduce_problem, disposition, accounting_error, started, task}.
    Recheck (findings 3-5): `key` + `generation` = the identity of the displayed task (Run now / Retry / Cancel must echo both; a row id
    is only a hint), `pause_id` = the manual pause of conversions in force (a confirmed Run now echoes it), `disposition_pending` = a
    failure / cancellation that could not be written to views.json yet (shown as an error; nothing is queued again meanwhile).
    `conversion_seq` = the variant's disposition revision as displayed (Retry / Run now of a failed or cancelled task echo it); the
    `error` of a failed / cancelled task is the outcome THAT revision belongs to (outcome_error; `vj` is one snapshot)."""
    import monster_lane
    mdir = models_dir(mroot, monster, variant)
    record = record_of(mdir)
    generation = frozen_digest(frozen)
    verify = reduce_status(mdir, generation) if generation else reduce_status(mdir)
    reduced = verify['ok']
    disposition = disposition_of(vj, generation)
    pending = monster_lane.pending_disposition(root, monster, variant, generation) if generation else None
    if task is not None and generation and not str(task.get('key') or '').endswith(f':{generation}'):
        task = None                                             # the newest row belongs to an earlier freeze
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
        rec = {k: record.get(k) for k in ('status', 'stage', 'pending_action', 'credits_committed', 'why', 'model_url', 'images_uploaded',
                                          'paid_review', 'submitted_by')}
        rec['actions'] = sorted((record.get('actions') or {}).keys()) if isinstance(record.get('actions'), dict) else []
    view = _task_view(task)
    paid = monster_lane.paid_actions(record)
    confirmed = [a for a in paid if a['status'] == 'confirmed' and not a.get('free')]     # (a free step is not a paid one)
    known = [a['credits'] for a in confirmed if isinstance(a['credits'], (int, float))]
    step = 'reduce' if reduced else record_step(mdir, record)
    error, waiting = None, None
    g = None                                                    # the ONE gate snapshot: waiting reason, pause ids and their reasons
    if view is None:
        if reduced:
            state = 'done'
        elif disposition is not None:
            state, error = disposition['state'], f'{disposition["state"]}: {disposition.get("reason") or "-"}'
        elif frozen is not None:
            state, waiting = 'waiting', 'the conversion lane queues it at its next pass'
        else:
            state, error = 'failed', f'a Hyper3D record exists (status {(record or {}).get("status")!r}) but no conversion is queued'
    elif view['state'] in ('starting', 'running', 'orphan'):
        state = 'running'
    elif view['state'] == 'done':
        state = 'done'
    elif view['state'] in ('failed', 'cancelled'):
        state = 'failed' if view['state'] == 'failed' else 'cancelled'
        verdict = view.get('verdict') or {}
        error = verdict.get('error') or view.get('error') or view['state']
        if view['state'] == 'cancelled':
            error = f'cancelled: {error}'
        # the failure `conversion_seq` (same `vj` snapshot) belongs to, when the row carries another outcome (blocker 4)
        shown = outcome_error(disposition, view['state'], error, (task or {}).get('token'))
        if shown is not error:
            error, verdict = shown, {}
        step = VERDICT_STEP.get(verdict.get('step'), step)
    else:                                                       # queued: waiting for its gate, its backoff or the running one
        state = 'queued'
        if (view.get('not_before') or 0) > time.time():
            state = 'waiting'
            waiting = 'browser_busy' if monster_lane.BUSY_TEXT in str(view.get('error') or '').lower() else (view.get('error') or 'retrying soon')
        elif with_gate:
            g = monster_lane.gate(root, task.get('args'))
            if not g['open']:
                state, waiting = 'waiting', (g['reason'] if g.get('mode_unreadable') else g['code'] or g['reason'])
            elif monster_lane.lane_tasks(root, state=monster_lane.taskqueue.ACTIVE, limit=5):
                state, waiting = 'waiting', 'another monster conversion is running'
    accounting = monster_lane.accounting_error(root, mdir / 'hyper3d' / 'studio_submission.json')
    if pending is not None and not reduced:                   # finding 3: the durable record of the outcome is still outstanding
        text = (f'the {pending.get("state") or "outcome"} of this conversion could not be recorded in views.json yet '
                f'({pending.get("error") or "unknown error"}); it is written again at every pass and nothing is queued meanwhile')
        error = f'{error}; {text}' if error else text
        if state in ('waiting', 'queued', 'running'):
            state, waiting = 'failed', None
    if g is None and with_gate:
        g = monster_lane.gate(root, (task or {}).get('args'))
    # `pause_id` + `pauses` (each {pause_id, kind, reason}) come from the SAME gate snapshot as `waiting_reason`. They are a hint
    # only: Run now always asks the server, which answers 409 `confirm` with the exact pauses to show (the challenge).
    return {'task_id': view['id'] if view else None, 'key': view['key'] if view else None,
            'pause_id': g['pause_id'] if g else None, 'pauses': list(g['pauses']) if g else [], 'disposition_pending': pending,
            'state': state, 'step': step, 'waiting_reason': waiting,
            'paid': bool(confirmed), 'paid_uncertain': [a['action'] for a in paid if a['status'] == 'uncertain'],
            'credits': round(sum(known), 3) if known else None,
            'free_steps': sum(1 for a in paid if a['status'] == 'confirmed' and a.get('free')), 'error': error, 'attempts': view['attempts'] if view else 0,
            'run_now': bool(view and view['run_now']), 'record': rec, 'screenshots': shots, 'reduced': reduced,
            'reduce_problem': None if reduced else verify['problem'], 'disposition': disposition, 'accounting_error': accounting,
            'generation': generation, 'conversion_seq': disposition_seq(vj), 'started': conversion_started(mdir), 'task': view}


def _tasks_by_variant(root: Path) -> dict[str, dict]:
    """{"<monster>/<variant>": newest lane task} (one query)."""
    try:
        import monster_lane
        return monster_lane.newest_tasks(root)
    except Exception:  # noqa: BLE001  (a damaged queue never breaks the cards)
        return {}


def _damaged_card(raw: dict, error: str) -> dict:
    """A read-only card of a variant whose state cannot be read (F16): shown in its tab with the error, no actions."""
    stage = raw.get('stage_hint') or ('3d' if raw.get('lods') else 'approved')
    return {'id': raw['id'], 'type': 'monster', 'kind': 'monster', 'name': raw.get('name') or raw['id'], 'title': raw.get('name') or raw['id'],
            'stage': stage, 'thumbnail': raw.get('image'), 'thumbnail_kind': None, 'model': None, 'races': [],
            'badges': [{'text': f'damaged: {error[:160]}', 'tone': 'error'}], 'actions': [], 'progress': None,
            'monster': raw.get('monster'), 'variant': raw.get('variant'), 'library_hint': None, 'damaged': {'error': error}}


def variant_card(root: Path, mroot: Path, raw: dict, tasks: dict) -> dict:
    """The card of a variant (monsters.cards raw) with the phase 5 badges: views a/b, Choose views, conversion state, library hint."""
    if raw.get('damaged'):
        return _damaged_card(raw, raw['damaged'])
    stage = monster_stage(raw)
    lods = raw.get('lods') or {}
    lod = raw.get('lod') if raw.get('lod') in lods else ('0p4' if '0p4' in lods else next(iter(lods), None))
    monster, variant = raw['monster'], raw['variant']
    vdir = views_dir(mroot, monster, variant)
    badges = [{'text': str(raw.get('status') or 'image only'), 'tone': 'info'}]
    actions: list[str] = []
    if raw.get('manual'):
        badges.append({'text': f'picked before v2, manual only ({raw["manual"]["reason"]}; source {raw["manual"]["source"]})',
                       'tone': 'warn'})
    elif stage == 'approved':
        vj = read_views_json(vdir)
        states = view_states(root, mroot, monster, variant) if vj is not None else {}
        phase = views_phase(root, mroot, monster, variant, vj, states)
        task = tasks.get(f'{monster}/{variant}')
        if phase == 'generating':
            planned = planned_views(vj)
            done = sum(1 for v in ('front', *planned) if view_problem(vdir, v) is None)
            badges.append({'text': f'views {done}/{1 + len(planned)}', 'tone': 'info'})
        failed = [v for v in planned_views(vj) if task_terminal(states.get(v)) and view_problem(vdir, v) is not None]
        if failed:
            badges.append({'text': f'view failed {len(failed)}', 'tone': 'error'})
        if phase == 'choose':
            badges.append({'text': 'Choose views', 'tone': 'warn'})
        if vj and vj.get('unattended') and phase != 'frozen':
            badges.append({'text': 'convert straight away', 'tone': 'info'})
        generation = frozen_digest((vj or {}).get('frozen'))
        if task and generation and not str(task.get('key') or '').endswith(f':{generation}'):
            task = None
        disposition = disposition_of(vj, generation)
        import monster_lane
        if generation and monster_lane.pending_disposition(root, monster, variant, generation) is not None:
            badges.append({'text': 'conversion outcome not recorded yet (see the Queue errors)', 'tone': 'error'})
        if task:
            text = {'queued': 'conversion queued', 'starting': 'converting', 'running': 'converting', 'orphan': 'converting',
                    'failed': 'conversion failed', 'done': 'converted', 'cancelled': 'conversion cancelled'}.get(task['state'], task['state'])
            tone = 'error' if task['state'] == 'failed' else 'warn' if task['state'] == 'cancelled' else 'info'
            badges.append({'text': text, 'tone': tone})
        elif disposition:
            badges.append({'text': f'conversion {disposition["state"]}', 'tone': 'error' if disposition['state'] == 'failed' else 'warn'})
        elif phase == 'frozen':
            badges.append({'text': 'conversion not queued', 'tone': 'warn'})
        if raw.get('importable'):
            badges.append({'text': 'picked, not imported', 'tone': 'warn'})
            actions = ['import']
    elif stage == '3d':
        if raw.get('approved'):
            badges.append({'text': f'approved, but not verified ({raw.get("verify_problem")})', 'tone': 'error'})
            actions = ['unapprove']
        elif not raw.get('verified', True):
            badges.append({'text': f'reduction not verified: {raw.get("verify_problem")}', 'tone': 'warn'})
        elif raw.get('lod') in lods:
            actions = ['approve']
    else:
        badges.append({'text': 'approved', 'tone': 'ok'})
        if raw.get('legacy_reduce'):
            badges.append({'text': 'legacy reduction', 'tone': 'info'})
        actions = ['unapprove']
    if sections_stale(models_dir(mroot, monster, variant)):
        badges.append({'text': 'sections out of date', 'tone': 'warn'})
    hint = library_hint(library_info(mroot, monster))
    if hint:
        badges.append({'text': hint, 'tone': 'warn'})
    return {'id': raw['id'], 'type': 'monster', 'kind': 'monster', 'name': raw['name'], 'title': raw['name'], 'stage': stage,
            'thumbnail': raw.get('image'), 'thumbnail_kind': None, 'model': lods[lod]['url'] if lod else None, 'races': [],
            'badges': badges, 'actions': actions, 'progress': None, 'monster': monster, 'variant': variant, 'library_hint': hint,
            # the approval revision the card shows: its hover Unapprove echoes it (post-cut-over blocker 5)
            'approval_rev': raw_choice(mroot, raw)[1] if 'unapprove' in actions else None}


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
    """Raw cards of the variants picked before v2 that were never imported (flat files only): stage approved, action import; and the
    manual-only cards of approved concepts that have nothing to import from (F15)."""
    out = []
    for monster in pipeline_monsters(mroot):
        try:
            sources = import_sources(mroot, monster)
            manual = manual_picks(mroot, monster)
        except Exception as exc:  # noqa: BLE001  (F16: one monster never breaks the others)
            out.append({'id': f'monster:{monster}/?', 'kind': 'monster', 'name': f'{monster}', 'monster': monster, 'variant': None,
                        'lods': {}, 'damaged': f'{type(exc).__name__}: {exc}'})
            continue
        for variant, source in sources.items():
            out.append({'id': f'monster:{monster}/{variant}', 'kind': 'monster', 'name': f'{monster} {variant}', 'monster': monster,
                        'variant': variant, 'status': 'picked', 'image': murl(mroot, source['files']['front.png']), 'lods': {}, 'lod': None,
                        'approved': False, 'importable': True, 'import_how': source['how'], 'roles': [], 'record': {}})
        for variant, info in manual.items():
            out.append({'id': f'monster:{monster}/{variant}', 'kind': 'monster', 'name': f'{monster} {variant}', 'monster': monster,
                        'variant': variant, 'status': 'picked', 'image': murl(mroot, mroot / info['source']), 'lods': {}, 'lod': None,
                        'approved': False, 'manual': info, 'roles': [], 'record': {}})
    return out


def approval_problem(mdir: Path, choice: dict, generation: str | None, legacy: bool) -> str | None:
    """None when an approval (chosen_lod.json `approved: true`) still certifies what is on disk (finding 10): the file of its level has the
    sha256 the user approved and the approval was made for the current choice of views. An approval without a digest is accepted only for a
    legacy reduction (approved before v2 recorded digests). The file is HASHED on every call (fresh_sha256), never taken from the
    size + mtime cache: other bytes of the same size with the timestamp restored are not the approved model (final-verify row 10)."""
    lod = choice.get('lod')
    if lod not in LOD_LEVELS:
        return 'the approval names no level'
    digest = choice.get('glb_sha256')
    if not isinstance(digest, str) or not digest:
        return None if legacy else 'the approval carries no model digest; unapprove and approve the model you see'
    if 'generation' in choice and choice.get('generation') != generation:
        return 'the approval was made for another choice of views'
    actual = fresh_sha256(Path(mdir) / 'reduced' / f'lod_error_{lod}.glb')
    if actual != digest:
        return f'reduced/lod_error_{lod}.glb is not the model that was approved (it changed after the approval)'
    return None


def _verify_raw(mroot: Path, raw: dict) -> dict:
    """Add `verified` / `verify_problem` / `legacy_reduce` (reduce_status for the variant's current freeze) to a raw card with LODs. An
    approved variant is `verified` only while its approval still holds (approval_problem: the chosen LOD's sha256 and generation,
    finding 10). The variant's choice / recovery files are read strictly first (damage_check, finding 11: guard.Unreadable = a damaged
    card)."""
    mdir = models_dir(mroot, raw['monster'], raw['variant'])
    damage_check(mroot, mdir)
    if not raw.get('lods'):
        return raw
    vj = read_views_json(views_dir(mroot, raw['monster'], raw['variant']))
    generation = frozen_digest(vj.get('frozen')) if vj and vj.get('frozen') else None
    status = reduce_status(mdir, generation)
    out = raw | {'verified': status['ok'], 'verify_problem': status['problem'], 'legacy_reduce': bool(status.get('legacy'))}
    if raw.get('approved') and status['ok']:
        problem = approval_problem(mdir, raw_choice(mroot, raw)[0], generation, bool(status.get('legacy')))
        if problem:
            out |= {'verified': False, 'verify_problem': problem}
    return out


def raw_choice(mroot: Path, raw: dict) -> tuple[dict, str]:
    """(chosen_lod.json, its approval revision) AS THE RAW CARD READ IT (monsters.choice_snapshot: `choice` + `choice_rev`, the one
    read its `lod` and `approved` come from). Display, stage, approval check and the `approval_rev` a card / item shows all use this
    one snapshot, so a concurrent approval can never give "the level of one approval with the revision of another" (final-live
    review, blocker 5). A raw without a snapshot (a flat pick to import, a damaged card) is read now (choice_snapshot, strict)."""
    if isinstance(raw.get('choice'), dict) and isinstance(raw.get('choice_rev'), str):
        return raw['choice'], raw['choice_rev']
    return choice_snapshot(models_dir(mroot, raw['monster'], raw['variant']))


def variant_raws(root: Path) -> list[dict]:
    """One raw card per variant (monsters._card for each views / models folder; one that cannot be read becomes a damaged raw, F16), plus
    the flat picks to import and the manual-only picks."""
    mroot = mroot_of(root)
    out = []
    for monster in pipeline_monsters(mroot):
        folder = mroot / 'pipeline' / monster
        for variant in sorted(set(_dirs(folder / 'views')) | set(_dirs(folder / 'models'))):
            item_id = f'monster:{monster}/{variant}'
            try:
                raw = monsters._card(mroot, monster, variant)
                if raw:
                    out.append(_verify_raw(mroot, raw))
            except Exception as exc:  # noqa: BLE001
                out.append({'id': item_id, 'kind': 'monster', 'name': f'{monster} {variant}', 'monster': monster, 'variant': variant,
                            'lods': {}, 'image': murl(mroot, views_dir(mroot, monster, variant) / 'front.png'),
                            'stage_hint': '3d' if (folder / 'models' / variant / 'reduced').is_dir() else 'approved',
                            'damaged': f'{type(exc).__name__}: {exc}'})
    out += importable_raws(mroot)
    # a publication journal that cannot be used (final-verify row 11) makes the variant it is about a read-only damaged card
    problems: dict[str, str | None] = {}
    for n, raw in enumerate(out):
        monster, variant = raw.get('monster'), raw.get('variant')
        if raw.get('damaged') or not isinstance(monster, str) or not isinstance(variant, str):
            continue
        if monster not in problems:
            problems[monster] = journal_problem(root, monster)
        blocked = journal_blocks(root, monster, variant, problems[monster]) if problems[monster] else None
        if blocked:
            out[n] = raw | {'damaged': f'Unreadable: {blocked}', 'stage_hint': monster_stage(raw)}
    return out


def variant_cards(root: Path) -> dict[str, list[dict]]:
    """{stage: [card]} of every monster variant (approved / 3d / completed), including flat picks to import; a variant whose state cannot
    be read is a read-only damaged card (F16)."""
    mroot = mroot_of(root)
    tasks = _tasks_by_variant(root)
    tabs: dict[str, list[dict]] = {'approved': [], '3d': [], 'completed': []}
    for raw in variant_raws(root):
        try:
            card = variant_card(root, mroot, raw, tasks)
        except Exception as exc:  # noqa: BLE001
            card = _damaged_card(raw, f'{type(exc).__name__}: {exc}')
        tabs[card['stage']].append(card)
    return tabs


def state_cards(root: Path) -> dict[str, list[dict]]:
    """HOOK (catalog._build_state): every monster card per tab: concept cards (concepts / provisional / archive / trash) and variant cards
    (approved / 3d / completed). Never raises: a monster root that cannot be read gives no cards (F16)."""
    try:
        tabs = concept_cards(root)
    except Exception:  # noqa: BLE001
        tabs = {s: [] for s in SHOWN_STAGES}
    try:
        variants = variant_cards(root)
    except Exception:  # noqa: BLE001
        variants = {}
    for tab, cards in variants.items():
        tabs.setdefault(tab, []).extend(cards)
    return tabs


def view_task_info(root: Path, st: dict | None) -> dict | None:
    """`views[].task` of the item: the runner's status in the UI's words (queued | running | done | failed | cancelled), the attempt limit
    of the task file and, for a failed task, the reason (the runner's error, else the last job and its exit)."""
    if not st or not st['exists'] or st.get('foreign'):
        return None
    task_file = read_obj(batch_dir(root) / 'tasks' / f'{st["name"]}.json')
    state = RUNNER_STATE.get(st['state'], st['state'])
    error = (st.get('error') or f'no image written (last job {st["job"]}, exit {st["exit"]})') if state == 'failed' else None
    return {'name': st['name'], 'state': state, 'attempts': st['attempts'], 'max_attempts': task_file.get('max_attempts'), 'error': error,
            'job': st['job'], 'started': st['started']}


def variant_item(root: Path, item_id: str) -> dict:
    """The item of a monster variant, whatever its stage (web/monster_views.js, web/monster3d.js): the raw card plus views [{name, file,
    exists, valid, problem, url, enabled, planned, task, retry, repair}] (always front, left, back, right), views_rev, phase (legacy | generating |
    choose | frozen | import | manual), legacy, importable, unattended, frozen, draft_differs, conversion (conversion_info), lods[].sha256,
    reduce (reduce_status), library_hint / library and the pick record. A damaged variant is a read-only item with `damaged` (F16)."""
    mroot = mroot_of(root)
    monster, variant = parse_monster_id(item_id)
    raw = next((r for r in variant_raws(root) if r['id'] == item_id), None)
    if raw is None:
        raise not_found(f'Unknown monster {item_id}')
    tasks = _tasks_by_variant(root)
    try:
        return _variant_item(root, mroot, monster, variant, raw, tasks)
    except (guard.Unreadable, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        error = f'{type(exc).__name__}: {exc}'
        card = _damaged_card(raw, error)
        return raw | {'stage': card['stage'], 'card': card, 'damaged': {'error': error}, 'state': 'damaged', 'views': [], 'phase': 'damaged',
                      'legacy': False,
                      'importable': False, 'frozen': None, 'conversion': None, 'actions': []}


def _variant_item(root: Path, mroot: Path, monster: str, variant: str, raw: dict, tasks: dict) -> dict:
    if raw.get('damaged'):
        raise guard.Unreadable(raw['damaged'])
    card = variant_card(root, mroot, raw, tasks)
    vdir, mdir = views_dir(mroot, monster, variant), models_dir(mroot, monster, variant)
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
        problem = view_problem(vdir, name) if path.is_file() else None
        unwritten = name in planned and not (st and st['exists'] and not st.get('foreign'))
        settled = not (st and st['exists'] and not st.get('foreign')) or st['state'] in TERMINAL_VIEW_STATES
        url = murl(mroot, path) or (raw.get('image') if importing and name == 'front' else None)
        # a valid image with a missing / invalid provenance record is REPAIRED (verb view_repair), never drawn again (finding 15)
        repairable = name != 'front' and not importing and vj is not None and settled and view_repairable(vdir, name)
        views.append({'name': name, 'file': view_file(name), 'exists': path.is_file(), 'valid': path.is_file() and problem is None,
                      'problem': problem, 'url': url, 'enabled': ticks.get(name, False), 'planned': name in planned,
                      'task': view_task_info(root, st), 'repair': 'provenance' if repairable else None,
                      'retry': name != 'front' and not importing and vj is not None and settled and not repairable and
                               (problem is not None or not path.is_file()) and (name in planned or bool(st and st['exists'])) and
                               (unwritten or task_terminal(st) or problem is not None)})
    frozen = (vj or {}).get('frozen')
    frozen = frozen | {'names': [str(f)[:-4] for f in frozen.get('files') or []]} if isinstance(frozen, dict) else None
    chosen = [n for n in VIEW_ORDER if ticks.get(n)]
    rec = read_variant_names(mroot, monster)['variants'].get(variant)
    phase = 'manual' if raw.get('manual') else 'import' if importing else views_phase(root, mroot, monster, variant, vj, states)
    lib = library_info(mroot, monster)
    # the digest the 3D modal compares the bytes it LOADED with (monster3d.js): hashed from the file now, not from the cache (row 10)
    lods = {lv: dict(info) | {'sha256': fresh_sha256(mdir / 'reduced' / f'lod_error_{lv}.glb')}
            for lv, info in (raw.get('lods') or {}).items()}
    generation = frozen_digest((vj or {}).get('frozen'))
    reduce = reduce_status(mdir, generation) if raw.get('lods') else None
    return raw | {'stage': card['stage'], 'card': card, 'lods': lods, 'damaged': None,
                  # the approval revision shown (Unapprove echoes it, blocker 5): the revision of the SAME chosen_lod.json snapshot
                  # that `lod`, `approved`, the stage and the card were built from (final-live review, blocker 5)
                  'approval_rev': raw_choice(mroot, raw)[1],
                  'views': views, 'views_json': vj, 'views_rev': views_rev(vdir, vj), 'phase': phase,
                  'legacy': phase == 'legacy' or bool(reduce and reduce.get('legacy')),
                  'importable': importing, 'unattended': bool((vj or {}).get('unattended')), 'frozen': frozen,
                  'draft_differs': frozen is not None and chosen != frozen['names'],
                  'conversion': conversion_info(root, mroot, monster, variant, tasks.get(f'{monster}/{variant}'),
                                                (vj or {}).get('frozen'), vj=vj),
                  'reduce': reduce,
                  'library': lib, 'library_hint': library_hint(lib), 'pick': rec if isinstance(rec, dict) else None}


def item(root: Path, item_id: str) -> dict:
    """HOOK (catalog.item): the item of an `mconcept:` or `monster:` id."""
    if item_id.startswith('mconcept:'):
        return concept_item(root, item_id)
    if item_id.startswith('monster:'):
        return variant_item(root, item_id)
    raise ValueError('not a monster id')


# ----------------------------------------------------------------------------------------------------------- auto freeze

def _freeze_ready(root: Path, mroot: Path, monster: str, variant: str, vj: dict) -> list[str] | None:
    """The files an unattended freeze would take now, or None while it must wait (F9): EVERY planned view is settled by the ONE
    terminal-state rule `view_waiting` (the rule of the item phase too, finding 15: its task is terminal, or it has no task descriptor
    and a valid view exists), and the views to freeze are the valid ones (at most four, view order)."""
    vdir = views_dir(mroot, monster, variant)
    states = view_states(root, mroot, monster, variant)
    for view in planned_views(vj):
        if view_waiting(vdir, view, states.get(view)):
            return None
    files = [view_file(n) for n in available_views(vdir)][:4]
    return files or None


def auto_freeze(root: Path, errors: list | None = None) -> list[str]:
    """HOOK (orchestrator tick, through monster_lane.tick): every variant with views.json `unattended: true`, nothing frozen, no conversion
    started and every planned view task terminal is frozen with its valid views (D, "unattended"); the task states and the outputs are
    checked again under the monster lock (F9). One variant's error never stops the others (F16; appended to `errors`). Returns the
    `<monster>/<variant>` frozen now."""
    root, mroot = Path(root), mroot_of(root)
    done = []
    for monster in pipeline_monsters(mroot):
        for variant in _dirs(mroot / 'pipeline' / monster / 'views'):
            try:
                vdir = views_dir(mroot, monster, variant)
                vj = read_views_json(vdir)
                if not vj or not vj.get('unattended') or vj.get('frozen'):
                    continue
                if _freeze_ready(root, mroot, monster, variant, vj) is None:
                    continue
                if conversion_started(models_dir(mroot, monster, variant)):
                    continue
                with monster_lock(root, monster):
                    contained(mroot, vdir)
                    vj = read_views_json(vdir)
                    if not vj or vj.get('frozen') or not vj.get('unattended'):
                        continue
                    files = _freeze_ready(root, mroot, monster, variant, vj)
                    if files is None:
                        continue
                    vj['views'] = [{'name': n, 'enabled': view_file(n) in files} for n in available_views(vdir)]
                    vj['frozen'] = freeze(vdir, files) | {'auto': True}
                    write_views_json(vdir, vj)
                done.append(f'{monster}/{variant}')
            except Exception as exc:  # noqa: BLE001
                if errors is not None:
                    errors.append(f'auto-freeze {monster}/{variant}: {type(exc).__name__}: {exc}')
    return done


def frozen_variants(root: Path, errors: list | None = None) -> list[dict]:
    """Every variant whose views.json has a frozen list: {monster, variant, frozen, digest, views_json}. A damaged views.json is skipped and
    reported in `errors` (F16)."""
    mroot = mroot_of(root)
    out = []
    for monster in pipeline_monsters(mroot):
        for variant in _dirs(mroot / 'pipeline' / monster / 'views'):
            try:
                vj = read_views_json(views_dir(mroot, monster, variant))
            except Exception as exc:  # noqa: BLE001
                if errors is not None:
                    errors.append(f'{monster}/{variant}: {type(exc).__name__}: {exc}')
                continue
            if vj and vj.get('frozen'):
                out.append({'monster': monster, 'variant': variant, 'frozen': vj['frozen'], 'digest': frozen_digest(vj['frozen']),
                            'views_json': vj})
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
        if names.get('error') and not state.get('error'):
            state = state | {'error': f'pipeline/{monster}/variant_names.json: {names["error"]}'}     # a damaged ledger: nothing is migrated (F11)
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
    # a pick the migration maps (sha256 / override) but variant_names.json does not list is importable too once applied (F15)
    known = {(i['monster'], i['variant']) for i in imports}
    for entry in folders:
        for rec in entry['approved']:
            pair = (rec['monster'], rec['variant'])
            if pair in known or ARMOUR_VARIANT.fullmatch(rec['variant']) or not (mroot / 'pipeline' / rec['monster']).is_dir():
                continue
            if views_dir(mroot, *pair).exists() or models_dir(mroot, *pair).exists():
                continue
            if 'front.png' in flat_pick_files(mroot, *pair):
                imports.append({'monster': pair[0], 'variant': pair[1], 'how': rec['how']})
                known.add(pair)
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
