"""Decision files of the v2 preview app (see DESIGN.md sections 3 and 6).

`models/<set>/pipeline_v2.json` is written only here: atomic (tmp + os.replace, retried on the Windows PermissionError a
reader or virus scanner can cause), under a per-set CROSS-PROCESS lock (`models/<set>/.pipeline_v2.lock`), every mutation
bumps `rev` and appends to `history`. LOD picks are mutations too: they bump the same `rev`.
`chosen_lod.json` keeps the format the old site wrote (`lod_record`); it is written into the module folder of the chosen job.
A file that exists but cannot be read (invalid UTF-8 or JSON, a wrongly shaped nested record) is NEVER replaced by defaults: every mutation of
that set is refused (HTTP 409). A set with an unresolved interrupted transaction is read-only too (409 `recovering`, guard.check_recovering).
Multi-file changes (LOD picks, both approvals, unapprove with its ready.json revocation) are ONE guard.apply_transaction each.
Nothing here moves folders or calls the pipeline tools.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import uuid

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import serve  # noqa: E402  (tools/preview/serve.py, used as a library)
import guard  # noqa: E402
from guard import AlreadyApproved, Changed, Conflict, JobMismatch, LodLocked, Recovering, RevConflict, StaleBuild, StaleChoice, Unreadable  # noqa: E402,F401

FILENAME = 'pipeline_v2.json'
LOCKNAME = '.pipeline_v2.lock'
SCHEMA = 1
HISTORY_LIMIT = 500
LOD_LEVELS = serve.LOD_LEVELS            # ('0p4', '0p2', '0p1')
READY = 'ready.json'
UNIT_KEY = re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
SHA256 = re.compile(r'^[0-9a-f]{64}$')
now = guard.now


def models_dir(root: Path, set_name: str) -> Path:
    return serve.direct(root, 'models', set_name)


def set_lock(root: Path, set_name: str) -> guard.OsLock:
    """Cross-process, per-thread re-entrant lock of one set's decisions (creates models/<set>/ on first use)."""
    return guard.os_lock(models_dir(root, set_name) / LOCKNAME)


def defaults(set_name: str) -> dict:
    return {'schema': SCHEMA, 'set': set_name, 'rev': 0, 'dropped': {}, 'lod_approval': None, 'amount': {},
            'aligned_approval': None, 'history': []}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _amount_problem(where: str, amount: object) -> str | None:
    if not isinstance(amount, dict):
        return f'{where} is not an object'
    for race, groups in amount.items():
        if not isinstance(race, str) or not isinstance(groups, dict):
            return f'{where}.{race} is not an object'
        for group, value in groups.items():
            if not isinstance(group, str) or not _is_int(value) or not 0 <= value <= 100:
                return f'{where}.{race}.{group} is not an integer from 0 to 100'
    return None


def _units_problem(where: str, units: object, fields: dict) -> str | None:
    """`units` = {"race/module": {field: kind}} with every field of `fields` (a kind of None = optional text)."""
    if not isinstance(units, dict):
        return f'{where}.units is not an object'
    for key, entry in units.items():
        if not isinstance(key, str) or not UNIT_KEY.match(key):
            return f'{where}.units has an invalid unit {key!r}'
        if not isinstance(entry, dict):
            return f'{where}.units.{key} is not an object'
        for field, required in fields.items():
            value = entry.get(field)
            if value is None and not required:
                continue
            if field == 'lod' and value not in LOD_LEVELS:
                return f'{where}.units.{key}.lod {value!r} is not a LOD'
            if not isinstance(value, str) or not value:
                return f'{where}.units.{key}.{field} is missing or not text'
            if field == 'glb_sha256' and not SHA256.match(value):
                return f'{where}.units.{key}.glb_sha256 is not a sha256'
    return None


def validate(raw: object) -> str | None:
    """Why `raw` is not a usable pipeline_v2.json (None = fine; absent keys are fine). Nested records are checked too (phase 1 review #10):
    a malformed approval must give an "unreadable" card, never break the derivation."""
    if not isinstance(raw, dict):
        return 'not a JSON object'
    if raw.get('schema', SCHEMA) != SCHEMA:
        return f'unsupported schema {raw.get("schema")!r}'
    rev = raw.get('rev', 0)
    if not _is_int(rev) or rev < 0:
        return f'invalid rev {rev!r}'
    for key, kind in (('dropped', dict), ('amount', dict), ('history', list)):
        if key in raw and not isinstance(raw[key], kind):
            return f'{key} is not a {kind.__name__}'
    for key in ('lod_approval', 'aligned_approval'):
        if raw.get(key) is not None and not isinstance(raw[key], dict):
            return f'{key} is not an object'
    for race, meta in (raw.get('dropped') or {}).items():
        if not isinstance(race, str) or not isinstance(meta, dict):
            return f'dropped.{race} is not an object'
    if 'amount' in raw:
        problem = _amount_problem('amount', raw['amount'])
        if problem:
            return problem
    lod = raw.get('lod_approval')
    if lod is not None:
        problem = _units_problem('lod_approval', lod.get('units'), {'job': True, 'lod': True, 'glb_sha256': False})
        if problem:
            return problem
        frozen = lod.get('required_races')
        if frozen is not None and (not isinstance(frozen, list) or not all(isinstance(r, str) for r in frozen)):
            return 'lod_approval.required_races is not a list of races'
        if lod.get('approval_id') is not None and not isinstance(lod['approval_id'], str):
            return 'lod_approval.approval_id is not text'
    aligned = raw.get('aligned_approval')
    if aligned is not None:
        problem = _units_problem('aligned_approval', aligned.get('units'), {'job': True, 'lod': True, 'build_id': True})
        if problem:
            return problem
        if aligned.get('amount') is not None:
            problem = _amount_problem('aligned_approval.amount', aligned['amount'])
            if problem:
                return problem
        bare = aligned.get('bare')
        if bare is not None and (not isinstance(bare, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in bare.items())):
            return 'aligned_approval.bare is not a {race: build id} map'
        if aligned.get('approval_id') is not None and not isinstance(aligned['approval_id'], str):
            return 'aligned_approval.approval_id is not text'
        if aligned.get('ready_sha256') is not None and not (isinstance(aligned['ready_sha256'], str) and SHA256.match(aligned['ready_sha256'])):
            return 'aligned_approval.ready_sha256 is not a sha256'
    return None


def normalise(set_name: str, raw: object) -> dict:
    """The file content with every key present (call `validate` first: an invalid one only gives the defaults)."""
    data = defaults(set_name)
    if isinstance(raw, dict) and validate(raw) is None:
        for key in data:
            if key in raw and key not in ('schema', 'set'):
                data[key] = raw[key]
    return data


def approval_gen(lod_approval: dict | None) -> str | None:
    """The generation of a 3D approval (re-check R8 / R11): its `approval_id` (written since the re-check), else its `time` (older approvals)."""
    if not isinstance(lod_approval, dict):
        return None
    value = lod_approval.get('approval_id') or lod_approval.get('time')
    return str(value) if value else None


def path_of(root: Path, set_name: str) -> Path:
    return models_dir(root, set_name) / FILENAME


def read_raw(path: Path) -> tuple[object, str | None]:
    """(parsed JSON | None, error). Absent file = (None, None); anything unreadable = (None, reason)."""
    try:
        text = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return None, None
    except UnicodeDecodeError as exc:
        return None, f'{path.name} is not valid UTF-8 ({exc.reason} at byte {exc.start})'
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
        guard.check_recovering(root, set_name)
        data = load(root, set_name)
        check_rev(data, rev)
        change(data)
        data = bumped(data, action, detail)
        guard.write_atomic(path_of(root, set_name), dump(data))
        return data


def _not_approved(data: dict, set_name: str) -> None:
    """Race changes are only possible before the 3D approval (phase 3 review F3): the approval froze the required races."""
    if data['lod_approval']:
        raise AlreadyApproved(f'{set_name} is approved in 3D: the races are frozen; send it back to 3D first')


# ----------------------------------------------------------------------------------------------------------- set verbs

def drop_race(root: Path, set_name: str, race: str, rev: object, required: list[str], reason: str = '',
              meta: dict | None = None) -> dict:
    """Drop `race` from the set. `required` = the set's required races; `meta` restores a dropped race's earlier time / reason (undo)."""
    if race not in required:
        raise ValueError(f'{race} is not a required race of {set_name}')
    meta = meta if isinstance(meta, dict) else {}

    def change(data: dict) -> None:
        _not_approved(data, set_name)
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
        _not_approved(data, set_name)
        if race not in data['dropped']:
            raise ValueError(f'{race} is not dropped from {set_name}')
        removed.update(data['dropped'][race] if isinstance(data['dropped'][race], dict) else {})
        del data['dropped'][race]
    data = mutate(root, set_name, rev, 'undrop_race', {'race': race}, change)
    return data | {'_undropped': removed}


def revoked_name(folder: Path, kind: str = 'revoked') -> Path:
    """A free `ready.json.<kind>-<time>[-n]` path in `folder` (`revoked` / `quarantine`; such manifests are never deleted)."""
    stamp = now().replace(':', '').replace('+0000', '').replace('+00:00', '')
    revoked = folder / f'{READY}.{kind}-{stamp}'
    n = 2
    while revoked.exists():
        revoked = folder / f'{READY}.{kind}-{stamp}-{n}'
        n += 1
    return revoked


def revoke_plan(root: Path, set_name: str, kind: str = 'revoked') -> tuple[list[tuple[Path, bytes | None]], Path | None]:
    """The transaction steps that revoke `models/<set>/ready.json` (renamed = its bytes written to a revoked name, then deleted)."""
    ready = models_dir(root, set_name) / READY
    old = guard.read_bytes_or_none(ready)
    if old is None:
        return [], None
    revoked = revoked_name(ready.parent, kind)
    return [(revoked, old), (ready, None)], revoked


def ready_bytes(ready: dict) -> bytes:
    """The exact bytes of a ready.json (the Aligned approval stores their sha256 as `ready_sha256`, re-check R4)."""
    return json.dumps(ready, indent=1).encode('utf-8')


def ready_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def send_back(root: Path, set_name: str, rev: object, target: str) -> dict:
    """`unapprove` of a set: `3d` clears lod_approval and aligned_approval, `aligned` clears aligned_approval only. ONE transaction with the
    ready.json revocation, and the rev is checked BEFORE anything is renamed (phase 3 review F7). Returns the decisions + `_revoked` (name | None)."""
    with set_lock(root, set_name):
        guard.check_recovering(root, set_name)
        data = load(root, set_name)
        check_rev(data, rev)
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
        steps, revoked = revoke_plan(root, set_name)
        data = bumped(data, 'unapprove', {'to': target} | ({'revoked': revoked.name} if revoked else {}))
        guard.apply_transaction(root, f'unapprove {set_name}', steps + [(path_of(root, set_name), dump(data))], set_name)
        return data | {'_revoked': revoked.name if revoked else None}


def revoke_ready(root: Path, set_name: str, reason: str, kind: str = 'revoked') -> str | None:
    """Revoke ready.json alone (the orchestrator, when the approval it certifies no longer holds): renamed to `ready.json.revoked-<time>`, or with
    `kind='quarantine'` to `ready.json.quarantine-<time>` (a manifest that cannot be verified any more: unreadable decisions, bytes that are not
    the approved ones; re-check R4). The bytes are kept. Call under the set lock."""
    steps, revoked = revoke_plan(root, set_name, kind)
    if not steps:
        return None
    guard.apply_transaction(root, f'{"quarantine" if kind == "quarantine" else "revoke"} ready {set_name}: {reason}'[:200], steps, set_name)
    return revoked.name


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
    """The chosen_lod.json record (the format the retired old site wrote; the outfit exporter reads it)."""
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
        guard.check_recovering(root, set_name)
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
        guard.apply_transaction(root, f'{action} {set_name}', plan, set_name)
        return {'previous': previous, 'rev': data['rev']}


# ------------------------------------------------------------------------------------------------------- phase 3 approvals

def approve_3d(root: Path, set_name: str, rev: object, writes: list[tuple[str, str, str, str, str]], units: dict, required_races: list[str]) -> dict:
    """The 3D approval, ONE transaction: the unsaved LOD picks (`writes` = [(model_set, job, race, module, lod)] -> chosen_lod.json) and
    pipeline_v2.json with `lod_approval = {time, units, required_races}` (`units` = {"race/module": {job, lod, glb_sha256}}), rev bumped.
    Refused (409) when the file is at another rev or the set is already approved."""
    with set_lock(root, set_name):
        guard.check_recovering(root, set_name)
        data = load(root, set_name)
        check_rev(data, rev)
        if data['lod_approval']:
            raise AlreadyApproved(f'{set_name} is already approved in 3D; send it back first')
        plan: list[tuple[Path, bytes | None]] = []
        for key, entry in units.items():
            if not isinstance(entry.get('glb_sha256'), str) or not SHA256.match(entry['glb_sha256']):
                raise ValueError(f'{key}: a 3D approval pins the source digest (glb_sha256) of every unit')
        for model_set, job, race, module, lod in writes:
            if lod not in LOD_LEVELS:
                raise ValueError(f'Invalid LOD {lod!r}')
            folder = module_dir(root, model_set, job, race, module)
            if folder.is_symlink() or not (folder / 'reduced' / f'lod_error_{lod}.glb').is_file():
                raise ValueError(f'{race}/{module} has no {lod} model')
            plan.append((folder / serve.LOD_CHOICE, json.dumps(lod_record(lod), indent=1).encode('utf-8')))
        data['lod_approval'] = {'time': now(), 'approval_id': uuid.uuid4().hex, 'units': units, 'required_races': list(required_races)}
        data = bumped(data, 'approve_3d', {'units': {k: v['lod'] for k, v in units.items()}, 'picked': len(writes)})
        plan.append((path_of(root, set_name), dump(data)))
        guard.apply_transaction(root, f'approve_3d {set_name}', plan, set_name)
        return data


def approve_aligned(root: Path, set_name: str, rev: object, aligned: dict, amount: dict, ready_path: Path, ready: bytes) -> dict:
    """The Aligned approval, ONE transaction: pipeline_v2.json (`aligned_approval` with its `approval_id` and `ready_sha256` = the sha256 of `ready`,
    `amount` merged per race, rev bumped), `ready.json` (exactly the bytes `ready`, carrying the same `approval_id`) and, when an older ready.json
    exists, its revoked copy."""
    if aligned.get('ready_sha256') != ready_digest(ready):
        raise ValueError('aligned_approval.ready_sha256 is not the digest of the ready.json written with it')
    with set_lock(root, set_name):
        guard.check_recovering(root, set_name)
        data = load(root, set_name)
        check_rev(data, rev)
        if not data['lod_approval']:
            raise ValueError(f'{set_name} has no 3D approval')
        data['aligned_approval'] = aligned
        merged = {race: dict(groups) for race, groups in data['amount'].items() if isinstance(groups, dict)}
        for race, groups in amount.items():
            merged.setdefault(race, {}).update(groups)
        data['amount'] = merged
        data = bumped(data, 'approve_aligned', {'units': {k: v['build_id'] for k, v in aligned['units'].items()}, 'approval_id': aligned.get('approval_id')})
        steps, _revoked = revoke_plan(root, set_name)
        steps = [s for s in steps if s[0] != ready_path]                     # the old manifest is kept as a revoked copy, then replaced
        guard.apply_transaction(root, f'approve_aligned {set_name}', steps + [(path_of(root, set_name), dump(data)), (ready_path, ready)], set_name)
        return data


def set_amount(root: Path, set_name: str, rev: object, race: str, group: str, value: int) -> dict:
    """Save one Amount (0..100) of a race. The same value writes nothing (the rev stays). Returns the decisions."""
    with set_lock(root, set_name):
        guard.check_recovering(root, set_name)
        data = load(root, set_name)
        check_rev(data, rev)
        groups = data['amount'].get(race)
        if isinstance(groups, dict) and groups.get(group) == value:
            return data
        data['amount'] = {r: dict(g) for r, g in data['amount'].items() if isinstance(g, dict)}
        data['amount'].setdefault(race, {})[group] = value
        data = bumped(data, 'amount', {'race': race, 'group': group, 'value': value})
        guard.write_atomic(path_of(root, set_name), dump(data))
        return data


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
    Windows replace retry. An approved monster keeps its level (LodLocked). chosen_lod.json is read strictly (guard.Unreadable)."""
    m = serve.monsters
    if lod not in m.LOD_LEVELS:
        raise ValueError('Invalid LOD')
    folder = monster_folder(root, monster_id)
    import monsters_v2                                         # (lazy: monsters_v2 never imports decisions)
    monsters_v2.contained(m.monsters_root(root), folder / LOCKNAME, folder / m.CHOICE, folder / 'reduced' / f'lod_error_{lod}.glb')
    with guard.os_lock(folder / LOCKNAME):
        # STRICT reads (recheck finding 11): a malformed chosen_lod.json or reconvert journal is 409 `unreadable` and stays as it is
        # (the tolerant legacy reader treated it as empty and this write then replaced it)
        monsters_v2.read_reconvert_journal(folder)
        record = monsters_v2.read_choice(folder)
        if record.get('approved') is True:
            raise LodLocked('approved; send it back to 3D first')
        if not (folder / 'reduced' / f'lod_error_{lod}.glb').is_file():
            raise ValueError('That LOD has no file')
        record |= {'lod': lod, 'reduced': f'reduced/lod_error_{lod}.glb', 'updated': datetime.now(timezone.utc).isoformat()}
        guard.write_atomic(folder / m.CHOICE, json.dumps(record, indent=1).encode('utf-8'))
    return {'ok': True, 'lod': lod, 'approved': False}
