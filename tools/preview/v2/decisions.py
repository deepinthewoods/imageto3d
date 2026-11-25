"""Decision files of the v2 preview app (see DESIGN.md sections 3 and 6).

`models/<set>/pipeline_v2.json` is written only here: atomic (tmp + os.replace, retried on the Windows PermissionError a
reader or virus scanner can cause), under a per-set CROSS-PROCESS lock (`models/<set>/.pipeline_v2.lock`), every mutation
bumps `rev` and appends to `history`. LOD picks are mutations too: they bump the same `rev`.
`chosen_lod.json` keeps its existing format (`serve.write_lod`); it is written into the module folder of the chosen job.
A file that exists but cannot be read is NEVER replaced by defaults: every mutation of that set is refused (HTTP 409).
Nothing here moves folders or calls the pipeline tools.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import serve  # noqa: E402  (tools/preview/serve.py, used as a library)
import guard  # noqa: E402
from guard import Conflict, JobMismatch, LodLocked, RevConflict, Unreadable  # noqa: E402,F401  (re-exported for callers)

FILENAME = 'pipeline_v2.json'
LOCKNAME = '.pipeline_v2.lock'
SCHEMA = 1
HISTORY_LIMIT = 500
LOD_LEVELS = serve.LOD_LEVELS            # ('0p4', '0p2', '0p1')
now = guard.now


def models_dir(root: Path, set_name: str) -> Path:
    return serve.direct(root, 'models', set_name)


def set_lock(root: Path, set_name: str) -> guard.OsLock:
    """Cross-process, per-thread re-entrant lock of one set's decisions (creates models/<set>/ on first use)."""
    return guard.os_lock(models_dir(root, set_name) / LOCKNAME)


def defaults(set_name: str) -> dict:
    return {'schema': SCHEMA, 'set': set_name, 'rev': 0, 'dropped': {}, 'lod_approval': None, 'amount': {},
            'aligned_approval': None, 'history': []}


def validate(raw: object) -> str | None:
    """Why `raw` is not a usable pipeline_v2.json (None = fine; absent keys are fine)."""
    if not isinstance(raw, dict):
        return 'not a JSON object'
    if raw.get('schema', SCHEMA) != SCHEMA:
        return f'unsupported schema {raw.get("schema")!r}'
    rev = raw.get('rev', 0)
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0:
        return f'invalid rev {rev!r}'
    for key, kind in (('dropped', dict), ('amount', dict), ('history', list)):
        if key in raw and not isinstance(raw[key], kind):
            return f'{key} is not a {kind.__name__}'
    for key in ('lod_approval', 'aligned_approval'):
        if raw.get(key) is not None and not isinstance(raw[key], dict):
            return f'{key} is not an object'
    return None


def normalise(set_name: str, raw: object) -> dict:
    """The file content with every key present (call `validate` first: an invalid one only gives the defaults)."""
    data = defaults(set_name)
    if isinstance(raw, dict) and validate(raw) is None:
        for key in data:
            if key in raw and key not in ('schema', 'set'):
                data[key] = raw[key]
    return data


def path_of(root: Path, set_name: str) -> Path:
    return models_dir(root, set_name) / FILENAME


def read_raw(path: Path) -> tuple[object, str | None]:
    """(parsed JSON | None, error). Absent file = (None, None); anything unreadable = (None, reason)."""
    try:
        text = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f'cannot read {path.name}: {exc}'
    try:
        value = json.loads(text)
    except ValueError as exc:
        return None, f'{path.name} is not valid JSON ({exc})'
    problem = validate(value)
    return (None, f'{path.name}: {problem}') if problem else (value, None)


def interpret(set_name: str, raw: object, error: str | None) -> tuple[dict, str | None]:
    """(decisions with every key, error): defaults only when the file is absent; an error leaves defaults + the reason."""
    return normalise(set_name, raw if not error else None), error


def load(root: Path, set_name: str) -> dict:
    """Current decisions; raises Unreadable when the file exists but cannot be used (defaults only for an absent file)."""
    raw, error = read_raw(path_of(root, set_name))
    if error:
        raise Unreadable(error)
    return normalise(set_name, raw)


def dump(data: dict) -> bytes:
    return json.dumps(data, indent=1).encode('utf-8')


def atomic_write_json(path: Path, data: object, retries: int = 8) -> None:
    """Write `data` as JSON to `path` through a unique temp file; os.replace is retried when Windows refuses it for a moment."""
    guard.write_atomic(path, json.dumps(data, indent=1).encode('utf-8'))


def check_rev(data: dict, rev: object) -> None:
    if rev is not None and (not isinstance(rev, int) or isinstance(rev, bool) or rev != data['rev']):
        raise RevConflict(data['rev'], rev)


def bumped(data: dict, action: str, detail: dict) -> dict:
    out = dict(data)
    out['rev'] = data['rev'] + 1
    out['history'] = (list(data['history']) + [{'time': now(), 'action': action, 'detail': detail}])[-HISTORY_LIMIT:]
    return out


def mutate(root: Path, set_name: str, rev: object, action: str, detail: dict, change) -> dict:
    """Read-modify-write of `pipeline_v2.json` under the set's cross-process lock.

    `rev` is the revision the client saw (None skips the check); `change(data)` edits the loaded dict in place (it may raise
    ValueError to refuse). Returns the written content."""
    with set_lock(root, set_name):
        data = load(root, set_name)
        check_rev(data, rev)
        change(data)
        data = bumped(data, action, detail)
        guard.write_atomic(path_of(root, set_name), dump(data))
        return data


# ----------------------------------------------------------------------------------------------------------- set verbs

def drop_race(root: Path, set_name: str, race: str, rev: object, required: list[str], reason: str = '',
              meta: dict | None = None) -> dict:
    """Drop `race` from the set. `required` = the set's required races; `meta` restores a dropped race's earlier time / reason (undo)."""
    if race not in required:
        raise ValueError(f'{race} is not a required race of {set_name}')
    meta = meta if isinstance(meta, dict) else {}

    def change(data: dict) -> None:
        if race in data['dropped']:
            raise ValueError(f'{race} is already dropped from {set_name}')
        if len([r for r in required if r not in data['dropped']]) <= 1:
            raise ValueError('Cannot drop every race')
        data['dropped'][race] = {'time': str(meta.get('time') or now()), 'reason': str(meta.get('reason', reason))[:300]}
    return mutate(root, set_name, rev, 'drop_race', {'race': race, 'reason': str(meta.get('reason', reason))[:300]}, change)


def undrop_race(root: Path, set_name: str, race: str, rev: object) -> dict:
    """Require `race` again. The data's previous drop metadata is returned in `data['_undropped']` for the undo descriptor."""
    removed: dict = {}

    def change(data: dict) -> None:
        if race not in data['dropped']:
            raise ValueError(f'{race} is not dropped from {set_name}')
        removed.update(data['dropped'][race] if isinstance(data['dropped'][race], dict) else {})
        del data['dropped'][race]
    data = mutate(root, set_name, rev, 'undrop_race', {'race': race}, change)
    return data | {'_undropped': removed}


def send_back(root: Path, set_name: str, rev: object, target: str) -> dict:
    """`unapprove` of a set: `3d` clears lod_approval and aligned_approval, `aligned` clears aligned_approval only."""
    def change(data: dict) -> None:
        if target == '3d':
            if not data['lod_approval']:
                raise ValueError(f'{set_name} has no LOD approval to clear')
            data['lod_approval'] = None
            data['aligned_approval'] = None
        elif target == 'aligned':
            if not data['aligned_approval']:
                raise ValueError(f'{set_name} has no aligned approval to clear')
            data['aligned_approval'] = None
        else:
            raise ValueError(f'Cannot send {set_name} back to {target}')
    return mutate(root, set_name, rev, 'unapprove', {'to': target}, change)


# ------------------------------------------------------------------------------------------------------ chosen_lod.json

def module_dir(root: Path, set_name: str, job: str, race: str, module: str) -> Path:
    folder = models_dir(root, set_name)
    for part in (job, race, module):
        folder = folder / serve.safe_name(part)
    return folder


def read_chosen(folder: Path) -> str | None:
    lod = serve.read_json(folder / serve.LOD_CHOICE).get('lod')
    return lod if lod in LOD_LEVELS else None


def lod_record(lod: str) -> dict:
    """The same record `serve.write_lod` writes."""
    return {'lod': lod, 'reduced': f'reduced/lod_error_{lod}.glb', 'wearable': f'reduced/lod_error_{lod}__wearable.glb',
            'updated': datetime.now(timezone.utc).isoformat()}


def write_lods(root: Path, set_name: str, rev: object, writes: list[tuple[str, str, str, str, str | None]], action: str = 'lod',
               expect_rev: bool = True) -> dict:
    """Write `chosen_lod.json` for [(model_set, job, race, module, lod | None)] and bump the set's rev, all-or-nothing.

    Under the set lock: the pipeline_v2.json must be readable and at `rev`; every folder must exist and (for a pick) hold that
    level's GLB, else nothing is written. The chosen_lod files and the bumped pipeline_v2.json are replaced as ONE transaction
    (guard.apply_transaction: backups, an intent file, rollback on any error, recovery after a crash).
    Returns {"previous": {"race/module": lod | None}, "rev": new rev}."""
    with set_lock(root, set_name):
        data = load(root, set_name)
        check_rev(data, rev)
        if data['lod_approval']:
            raise LodLocked('approved; send it back to 3D first')
        plan: list[tuple[Path, bytes | None]] = []
        previous: dict[str, str | None] = {}
        for model_set, job, race, module, lod in writes:
            if lod is not None and lod not in LOD_LEVELS:
                raise ValueError(f'Invalid LOD {lod!r}')
            folder = module_dir(root, model_set, job, race, module)
            if folder.is_symlink() or not folder.is_dir():
                raise ValueError(f'Model folder not found: {race}/{module} (job {job})')
            if lod is not None and not (folder / 'reduced' / f'lod_error_{lod}.glb').is_file():
                raise ValueError(f'{race}/{module} has no {lod} model')
            previous[f'{race}/{module}'] = read_chosen(folder)
            plan.append((folder / serve.LOD_CHOICE, None if lod is None else json.dumps(lod_record(lod), indent=1).encode('utf-8')))
        data = bumped(data, action, {'units': {f'{w[2]}/{w[3]}': w[4] for w in writes}})
        plan.append((path_of(root, set_name), dump(data)))
        guard.apply_transaction(root, f'{action} {set_name}', plan)
        return {'previous': previous, 'rev': data['rev']}


# --------------------------------------------------------------------------------------------------------------- monsters

def monster_folder(root: Path, monster_id: str) -> Path:
    """models folder of `monster:<Monster>/<variant>` (the same check monsters.write_choice makes)."""
    m = serve.monsters
    if not isinstance(monster_id, str) or not monster_id.startswith('monster:'):
        raise ValueError('Invalid monster id')
    monster, _, variant = monster_id.removeprefix('monster:').partition('/')
    base = m.monsters_root(root)
    if monster not in m._dirs(base / 'pipeline') or variant not in m._dirs(base / 'pipeline' / monster / 'models'):
        raise ValueError('Monster model folder not found')
    return base / 'pipeline' / monster / 'models' / variant


def write_monster_lod(root: Path, monster_id: str, lod: object) -> dict:
    """Monster LOD pick: same record as monsters.write_choice, but under a per-variant cross-process lock, a unique temp file and the
    Windows replace retry. An approved monster keeps its level (LodLocked)."""
    m = serve.monsters
    if lod not in m.LOD_LEVELS:
        raise ValueError('Invalid LOD')
    folder = monster_folder(root, monster_id)
    with guard.os_lock(folder / LOCKNAME):
        record = m._read(folder / m.CHOICE)
        if record.get('approved') is True:
            raise LodLocked('approved; send it back to 3D first')
        if not (folder / 'reduced' / f'lod_error_{lod}.glb').is_file():
            raise ValueError('That LOD has no file')
        record |= {'lod': lod, 'reduced': f'reduced/lod_error_{lod}.glb', 'updated': datetime.now(timezone.utc).isoformat()}
        guard.write_atomic(folder / m.CHOICE, json.dumps(record, indent=1).encode('utf-8'))
    return {'ok': True, 'lod': lod, 'approved': False}
