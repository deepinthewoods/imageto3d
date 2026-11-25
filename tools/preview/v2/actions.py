"""Verbs of POST /api/v2/action and POST /api/v2/lod (DESIGN.md sections 4 and 6).

v2 owns every folder move itself (it no longer wraps `serve.action`): the allowlist `ALLOWED` of (id type, verb) pairs is the whole
write surface, so a request can never reach the old site's `model:` / `approvedmodel:` branches. What the moves guarantee:

* anything that moves `approved/<set>` out of approved/ (unapprove, the withdrawal inside a legacy undo) holds the cross-process
  CLAIM LOCK of that set (tools/tripo_pipeline/claim_lock.py, the lock `workflow.py claim` holds for its whole run) without
  waiting, then refuses when `.tripo-job.json` or `.base_views` exists in the folder (a claim owns it);
* a trash move writes the origin sidecar into the folder BEFORE renaming it (removed again when the rename fails);
* a flat (several files) move first writes an intent record (guard.py), rolls back on failure and leaves the intent as a
  "Recovery" card (Complete / Roll back) when even that fails;
* responses carry POSIX paths.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import serve  # noqa: E402  (tools/preview/serve.py, used as a library)
import catalog  # noqa: E402
import decisions  # noqa: E402
import guard  # noqa: E402
from claim_lock import ClaimBusy, claim_lock  # noqa: E402  (tools/tripo_pipeline on sys.path through guard.py)

NAME = serve.NAME
# folder of each unclaimed stage, for the `args.to` of unapprove
STAGE_FOLDER = {'concepts': 'concepts', 'provisional': 'provisional', 'approved': 'approved', 'archive': serve.ARCHIVE}
UNAPPROVE_TO = {'approved': ('provisional', 'concepts', 'archive'), 'provisional': ('concepts', 'archive')}
UNAPPROVE_DEFAULT = {'approved': 'provisional', 'provisional': 'concepts'}
# (id type, verb) pairs the action endpoint accepts (DESIGN.md section 4); anything else is a 400, never a fallthrough
ALLOWED = frozenset({
    ('concepts', 'provisional'), ('archive', 'provisional'),
    ('concepts', 'approve'), ('archive', 'approve'), ('provisional', 'approve'), ('legacy', 'approve'), ('archlegacy', 'approve'),
    ('concepts', 'reject'), ('legacy', 'reject'),
    ('approved', 'unapprove'), ('provisional', 'unapprove'),
    ('concepts', 'archive'), ('legacy', 'archive'), ('archive', 'unarchive'), ('archlegacy', 'unarchive'),
    ('trash', 'restore'), ('trashlegacy', 'restore'),
    ('intent', 'complete'), ('intent', 'rollback'),
    ('set', 'drop_race'), ('set', 'undrop_race'), ('set', 'unapprove'),
})
SET_VERBS = ('drop_race', 'undrop_race', 'unapprove')
FLAT_TYPES = ('legacy', 'archlegacy', 'trashlegacy')
WITHDRAWN = guard.APP / 'withdrawn'
JOB_STAGES = ('processing_views', 'multiview_ready', 'processing', 'failed', 'completed', serve.HELD)


def _undo(verb: str | None, item_id: str | None, args: dict | None = None, rev: int | None = None) -> dict | None:
    if not verb or not item_id:
        return None
    undo = {'verb': verb, 'id': item_id}
    if args:
        undo['args'] = args
    if rev is not None:
        undo['rev'] = rev
    return undo


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _check(root: Path, dst: Path, result: dict) -> None:
    """workflow.py check on the moved set; a failure is only a warning (the move stands), like serve.action."""
    command = [sys.executable, str(root / 'tools' / 'tripo_pipeline' / 'workflow.py'), '--root', str(root), 'check', str(dst)]
    checked = subprocess.run(command, capture_output=True, text=True, check=False)
    if checked.returncode:
        result['warning'] = checked.stderr.strip() or checked.stdout.strip() or 'Validation failed'
    else:
        try:
            result['check'] = json.loads(checked.stdout)
        except ValueError:
            result['warning'] = 'workflow.py check printed no JSON'


def _claimed(folder: Path) -> bool:
    """A claim owns the folder once it created its job record or its base snapshot (the snapshot comes first)."""
    return (folder / '.tripo-job.json').exists() or (folder / '.base_views').exists()


@contextmanager
def _locked_claim(root: Path, set_name: str):
    """The claim lock of `set_name`, not waiting: guard.Busy (HTTP 409) while a claim is running."""
    try:
        with claim_lock(root, set_name):
            yield
    except ClaimBusy:
        raise guard.Busy(f'{set_name} is being claimed; try again when the claim is done') from None


# ------------------------------------------------------------------------------------------------------------ folder moves

def _move(root: Path, verb: str, src: Path, dst: Path, after=None, sidecar: tuple[str, dict] | None = None, log_verb: str | None = None) -> dict:
    """Rename src to dst under serve.LOCK with serve.log_action bookkeeping. `sidecar` = (file name, content) is written into the
    folder BEFORE the rename (and removed again when the rename fails); `after(result)` runs once the folder moved."""
    result: dict = {}
    log_verb = log_verb or verb
    with serve.LOCK:
        try:
            if not src.is_dir():
                raise FileNotFoundError(src)
            if dst.exists():
                raise FileExistsError(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            written = None
            if sidecar:
                written = src / sidecar[0]
                written.write_text(json.dumps(sidecar[1]), encoding='utf-8')
            try:
                src.rename(dst)
            except BaseException:
                if written is not None:
                    try:
                        written.unlink()
                    except OSError:
                        pass
                raise
            if after:
                after(result)
            result |= {'ok': True, 'src': _rel(root, src), 'dst': _rel(root, dst)}
            serve.log_action(root, log_verb, result['src'], result['dst'], result.get('warning') or 'ok')
            return result
        except Exception as exc:
            serve.log_action(root, log_verb, _rel(root, src), _rel(root, dst), f'error: {exc}')
            raise


def hop_to_provisional(root: Path, stage: str, name: str) -> dict:
    """concepts|archive -> provisional (the old first approval hop); workflow.py check afterwards."""
    src, dst = serve.direct(root, STAGE_FOLDER[stage], name), serve.direct(root, 'provisional', name)
    if not src.is_dir():
        raise FileNotFoundError(src)
    if not serve.set_card(root, src, stage):
        raise ValueError('No per-set fronts found')
    return _move(root, 'provisional', src, dst, lambda result: _check(root, dst, result), log_verb='approve')


def direct_approve(root: Path, stage: str, name: str) -> dict:
    """concepts|archive -> approved directly: refuses when provisional/<S> or approved/<S> exists; workflow.py check afterwards."""
    src = serve.direct(root, STAGE_FOLDER[stage], name)
    dst = serve.direct(root, 'approved', name)
    if serve.direct(root, 'provisional', name).exists():
        raise FileExistsError(serve.direct(root, 'provisional', name))
    if not src.is_dir():
        raise FileNotFoundError(src)
    if not serve.set_card(root, src, stage):
        raise ValueError('No per-set fronts found')
    return _move(root, 'approve', src, dst, lambda result: _check(root, dst, result))


def provisional_approve(root: Path, name: str) -> dict:
    src, dst = serve.direct(root, 'provisional', name), serve.direct(root, 'approved', name)
    if not src.is_dir():
        raise FileNotFoundError(src)
    if not serve.set_card(root, src, 'provisional'):
        raise ValueError('No per-set fronts found')
    return _move(root, 'approve', src, dst, lambda result: _check(root, dst, result))


def unapprove_to(root: Path, stage: str, name: str, target: str | None) -> dict:
    """`unapprove` (default one step back, or `args.to`). Out of approved/ the claim lock is held without waiting and a folder a
    claim owns (`.tripo-job.json` or `.base_views`) is refused, so a live claim can never lose its folder."""
    target = target or UNAPPROVE_DEFAULT[stage]
    if target not in UNAPPROVE_TO.get(stage, ()):
        raise ValueError(f'Cannot unapprove {stage} to {target}')
    src = serve.direct(root, STAGE_FOLDER[stage], name)
    dst = serve.direct(root, STAGE_FOLDER[target], name)

    def move() -> dict:
        if not src.is_dir() or not serve.set_card(root, src, stage):
            raise FileNotFoundError(src)
        if _claimed(src):
            raise ValueError('Set has already been claimed')
        return _move(root, 'unapprove', src, dst)
    if stage == 'approved':
        with _locked_claim(root, name):
            return move()
    return move()


def reject_set(root: Path, name: str) -> dict:
    src = serve.direct(root, 'concepts', name)
    if not src.is_dir() or not serve.set_card(root, src, 'concepts'):
        raise FileNotFoundError(src)
    dst = serve.unique(root / 'trash' / 'concepts' / src.name)
    return _move(root, 'reject', src, dst, sidecar=('.preview-origin.json', {'kind': 'set', 'src': f'concepts/{src.name}'})) | {'undo_id': f'trash:{dst.name}'}


def archive_set(root: Path, verb: str, name: str) -> dict:
    to_archive = verb == 'archive'
    src = serve.direct(root, 'concepts' if to_archive else serve.ARCHIVE, name)
    dst = serve.direct(root, serve.ARCHIVE if to_archive else 'concepts', name)
    if not src.is_dir() or not serve.set_card(root, src, 'concepts' if to_archive else 'archive'):
        raise FileNotFoundError(src)
    return _move(root, verb, src, dst)


def restore_set(root: Path, name: str) -> dict:
    src = serve.direct(root, 'trash/concepts', name)
    origin = serve.read_json(src / '.preview-origin.json')
    if origin.get('kind') != 'set':
        raise ValueError('Missing origin sidecar')
    origin_dir, dest = serve.origin_split(origin.get('src'))
    return _move(root, 'restore', src, serve.direct(root, origin_dir, dest))


# -------------------------------------------------------------------------------------------------------------- flat moves

def withdraw_approved(root: Path, set_name: str) -> tuple[str, Path | None]:
    """Take `approved/<set>` out of approved/ if nobody claimed it (moved to .codex_jobs/_app/withdrawn/, never deleted).
    Returns ('withdrawn', where it went), or ('absent', None) when the set is not in approved/ and nothing else holds it; a claim, or a copy that already
    moved on to another review stage, is a ValueError and changes nothing."""
    src = serve.direct(root, 'approved', set_name)
    with _locked_claim(root, set_name):
        if src.exists():
            if _claimed(src):
                raise ValueError(f'{set_name} has already been claimed; it cannot be withdrawn')
            dst = serve.unique(root / WITHDRAWN / set_name)
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            return 'withdrawn', dst
    for stage in JOB_STAGES:
        parent = root / stage
        if parent.is_dir() and any(p.name.split('--')[0] == set_name and '--' in p.name for p in parent.iterdir()):
            raise ValueError(f'{set_name} has already been claimed ({stage}); it cannot be withdrawn')
    for folder in ('provisional', 'concepts', serve.ARCHIVE):
        if (root / folder / set_name).exists():
            raise ValueError(f'{set_name} was moved on to {folder}/; move it back or reject it there first')
    return 'absent', None


def _flat_pairs(root: Path, files: list[Path], dst_dir: Path) -> list[dict]:
    return [{'src': _rel(root, f), 'dst': _rel(root, dst_dir / f.name)} for f in files]


def _move_pairs(root: Path, pairs: list[dict]) -> None:
    for pair in pairs:
        src, dst = root / pair['src'], root / pair['dst']
        if dst.exists() and not src.exists():
            continue                                  # already moved (a resumed intent)
        if dst.exists():
            raise FileExistsError(f'Destination file already exists: {pair["dst"]}')
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))


def _back_pairs(root: Path, pairs: list[dict]) -> None:
    for pair in pairs:
        src, dst = root / pair['src'], root / pair['dst']
        if src.exists() or not dst.exists():
            continue
        shutil.move(str(dst), str(src))


def _write_sidecar(root: Path, rec: dict) -> None:
    sidecar = rec.get('sidecar')
    if sidecar:
        path = root / sidecar['path']
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sidecar['content']), encoding='utf-8')


def _rollback_flat(root: Path, rec: dict) -> None:
    """Undo an interrupted flat move: withdraw the promoted set (legacy approve), put the files back, remove the sidecar."""
    approved = rec.get('approved_set')
    if approved and (rec.get('promoted') or (root / 'approved' / approved).exists()):
        withdraw_approved(root, approved)               # the pre-check guaranteed the set did not exist before this approve
    _back_pairs(root, rec.get('files') or [])
    sidecar = rec.get('sidecar')
    if sidecar:
        try:
            (root / sidecar['path']).unlink()
        except FileNotFoundError:
            pass


def _complete_flat(root: Path, rec: dict) -> None:
    if rec.get('verb') == 'approve' and not (rec.get('approved_set') and (root / 'approved' / rec['approved_set']).is_dir()):
        raise ValueError('The promotion did not finish; roll this back instead')
    _write_sidecar(root, rec)
    _move_pairs(root, rec.get('files') or [])


def flat_move(root: Path, verb: str, files: list[Path], dst_dir: Path, sidecar: tuple[Path, dict] | None = None,
              extra: dict | None = None, before=None) -> None:
    """Move the flat files of one entry into `dst_dir` as a recoverable operation: an intent record first, the sidecar, the files
    one by one, intent deleted. On any failure everything is rolled back; if that fails too the intent stays for Recovery."""
    if any((dst_dir / f.name).exists() for f in files):
        raise FileExistsError('Destination file already exists')
    rec = {'kind': 'flat_move', 'verb': verb, 'label': f'{verb} {files[0].stem.split("__")[0] if files else ""}'.strip(),
           'files': _flat_pairs(root, files, dst_dir),
           'sidecar': {'path': _rel(root, sidecar[0]), 'content': sidecar[1]} if sidecar else None} | (extra or {})
    intent = guard.new_intent(root, rec)
    try:
        if before:
            rec |= before(rec) or {}
            guard.update_intent(root, intent, **{k: v for k, v in rec.items() if k not in ('kind',)})
        dst_dir.mkdir(parents=True, exist_ok=True)
        _write_sidecar(root, rec)
        _move_pairs(root, rec['files'])
    except BaseException:
        try:
            _rollback_flat(root, rec)
        except Exception:
            guard.abandon_intent(intent)          # the intent file stays: the Trash tab lists it as Recovery
            raise
        guard.finish_intent(root, intent)
        raise
    guard.finish_intent(root, intent)


def _flat_args(root: Path, parts: list[str], origin_dir: str) -> tuple[Path, str, list[Path]]:
    batch, key = serve.direct(root, origin_dir, parts[1]), serve.safe_name(parts[2])
    return batch, key, serve.flat_files(batch, key)


def legacy_approve(root: Path, kind: str, parts: list[str]) -> dict:
    """Promote a flat entry straight into approved/ (workflow.py promote --approve) and move its flat files to the trash with an
    origin sidecar that names the promoted set (so the undo can withdraw exactly that set)."""
    origin_dir = 'concepts' if kind == 'legacy' else serve.ARCHIVE
    batch, key, files = _flat_args(root, parts, origin_dir)
    if not re.fullmatch(r'\d+_[a-z0-9][a-z0-9_-]*', key):
        raise ValueError('Invalid flat entry')
    approved_set = f'{batch.name}_{key}'               # workflow.promote's default set id
    if (root / 'approved' / approved_set).exists():
        raise FileExistsError(f'approved/{approved_set} already exists')
    dst = serve.unique(root / 'trash' / 'concepts' / batch.name)
    result: dict = {}

    def promote(rec: dict) -> dict:
        command = [sys.executable, str(root / 'tools' / 'tripo_pipeline' / 'workflow.py'),
                   '--root', str(root), 'promote', str(batch), key.split('_', 1)[0], '--approve']
        run = subprocess.run(command, capture_output=True, text=True, check=False)
        if run.returncode:
            raise ValueError(run.stderr.strip() or run.stdout.strip() or 'Promote failed')
        promoted = json.loads(run.stdout)
        result['promoted'] = promoted | ({'path': str(promoted['path']).replace('\\', '/')} if 'path' in promoted else {})
        sidecar = rec['sidecar']
        sidecar['content'] = sidecar['content'] | {'approved_set': promoted['set']}
        return {'approved_set': promoted['set'], 'sidecar': sidecar, 'promoted': True}

    sidecar = (dst / f'{key}.preview-origin.json', {'kind': 'legacy', 'src': f'{origin_dir}/{batch.name}', 'approved_set': approved_set})
    with serve.LOCK:
        try:
            flat_move(root, 'approve', files, dst, sidecar, extra={'approved_set': approved_set, 'promoted': False}, before=promote)
        except Exception as exc:
            serve.log_action(root, 'approve', f'{origin_dir}/{batch.name}/{key}', None, f'error: {exc}')
            raise
        serve.log_action(root, 'approve', f'{origin_dir}/{batch.name}/{key}', _rel(root, dst), 'ok')
    return result | {'ok': True, 'src': f'{origin_dir}/{batch.name}/{key}', 'dst': _rel(root, dst), 'undo_id': f'trashlegacy:{dst.name}:{key}'}


def legacy_reject(root: Path, parts: list[str]) -> dict:
    batch, key, files = _flat_args(root, parts, 'concepts')
    dst = root / 'trash' / 'concepts' / batch.name
    if any((dst / p.name).exists() for p in files) or (dst / f'{key}.preview-origin.json').exists():
        dst = serve.unique(dst)
    sidecar = (dst / f'{key}.preview-origin.json', {'kind': 'legacy', 'src': f'concepts/{batch.name}'})
    with serve.LOCK:
        try:
            flat_move(root, 'reject', files, dst, sidecar)
        except Exception as exc:
            serve.log_action(root, 'reject', f'concepts/{batch.name}/{key}', None, f'error: {exc}')
            raise
        serve.log_action(root, 'reject', f'concepts/{batch.name}/{key}', _rel(root, dst), 'ok')
    return {'ok': True, 'src': f'concepts/{batch.name}/{key}', 'dst': _rel(root, dst), 'undo_id': f'trashlegacy:{dst.name}:{key}'}


def legacy_archive(root: Path, verb: str, parts: list[str]) -> dict:
    to_archive = verb == 'archive'
    origin_dir = 'concepts' if to_archive else serve.ARCHIVE
    batch, key, files = _flat_args(root, parts, origin_dir)
    dst = serve.direct(root, serve.ARCHIVE if to_archive else 'concepts', parts[1])
    with serve.LOCK:
        try:
            flat_move(root, verb, files, dst)           # refuses when any destination file exists
        except Exception as exc:
            serve.log_action(root, verb, f'{origin_dir}/{batch.name}/{key}', None, f'error: {exc}')
            raise
        serve.log_action(root, verb, f'{origin_dir}/{batch.name}/{key}', _rel(root, dst), 'ok')
    return {'ok': True, 'src': f'{origin_dir}/{batch.name}/{key}', 'dst': _rel(root, dst),
            'undo_id': f'{"archlegacy" if to_archive else "legacy"}:{parts[1]}:{key}'}


def restore_legacy(root: Path, parts: list[str]) -> dict:
    """Put a trashed flat entry back. When the entry was trashed by an approve, the exact promoted set is withdrawn from approved/
    first (same claim lock and checks as unapprove); if a claim already owns it nothing changes and the error says so."""
    batch, key = serve.direct(root, 'trash/concepts', parts[1]), serve.safe_name(parts[2])
    origin = serve.read_json(batch / f'{key}.preview-origin.json')
    if origin.get('kind') != 'legacy':
        raise ValueError('Missing origin sidecar')
    origin_dir, dest_name = serve.origin_split(origin.get('src'))
    dst = serve.direct(root, origin_dir, dest_name)
    files = serve.flat_files(batch, key)
    if any((dst / p.name).exists() for p in files):
        raise FileExistsError('Restore destination exists')
    withdrawn, kept = None, None
    with serve.LOCK:
        try:
            if isinstance(origin.get('approved_set'), str):
                withdrawn, kept = withdraw_approved(root, serve.safe_name(origin['approved_set']))
            # the origin sidecar stays in the trash folder as an audit record; no files are deleted
            flat_move(root, 'restore', files, dst)
        except Exception as exc:
            if kept is not None:                       # the flat files could not be restored: put the withdrawn set back
                try:
                    kept.rename(serve.direct(root, 'approved', origin['approved_set']))
                except OSError:
                    pass
            serve.log_action(root, 'restore', _rel(root, batch / key), None, f'error: {exc}')
            raise
        serve.log_action(root, 'restore', _rel(root, batch / key), _rel(root, dst), 'ok' if not withdrawn else f'ok; {origin["approved_set"]} {withdrawn}')
    return {'ok': True, 'src': _rel(root, batch / key), 'dst': _rel(root, dst), 'withdrawn': withdrawn}


# ------------------------------------------------------------------------------------------------------------------ recovery

def resolve_intent(root: Path, intent_id: str, verb: str) -> dict:
    """Complete or roll back an interrupted change listed in the Trash tab's Recovery group."""
    serve.safe_name(intent_id)
    if intent_id in guard._ACTIVE:
        raise guard.Busy('That change is still running')
    rec = guard.read_intent(root, intent_id)
    kind = rec.get('kind')
    with serve.LOCK:
        if kind == 'files_tx':
            if verb != 'rollback':
                raise ValueError('An interrupted write can only be rolled back')
            guard.restore_files(root, rec.get('files') or [])
        elif kind == 'flat_move':
            (_complete_flat if verb == 'complete' else _rollback_flat)(root, rec)
        else:
            raise ValueError('This recovery entry cannot be resolved here; its file is in .codex_jobs/_app/intents/')
        guard.finish_intent(root, intent_id)
        serve.log_action(root, f'recovery_{verb}', rec.get('label'), None, 'ok')
    return {'ok': True, 'undo': None}


def recover_at_start(root: Path) -> list[str]:
    """Server start (single writer): roll back interrupted multi-file writes (LOD transactions) from their backups. Interrupted moves
    stay listed as Recovery cards. Returns what was rolled back / what failed."""
    notes = []
    for rec in guard.list_intents(root):
        if rec.get('kind') == 'files_tx':
            try:
                guard.restore_files(root, rec.get('files') or [])
                guard.finish_intent(root, rec['id'])
                notes.append(f'rolled back {rec.get("label")}')
            except OSError as exc:
                notes.append(f'could not roll back {rec.get("label")}: {exc}')
    return notes


# ----------------------------------------------------------------------------------------------------------------- concepts

def concept_action(root: Path, verb: str, item_id: str, args: dict) -> dict:
    parts = item_id.split(':')
    kind = parts[0]
    name = parts[1] if len(parts) > 1 else ''
    if kind in FLAT_TYPES:
        if len(parts) != 3:
            raise ValueError('Invalid ID')
    elif len(parts) != 2:
        raise ValueError('Invalid ID')
    if verb == 'provisional':
        result = hop_to_provisional(root, kind, name)
        return result | {'undo': _undo('unapprove', f'provisional:{name}', {'to': kind})}
    if verb == 'approve':
        if kind in ('concepts', 'archive'):
            result = direct_approve(root, kind, name)
            return result | {'undo': _undo('unapprove', f'approved:{name}', {'to': kind})}
        if kind == 'provisional':
            return provisional_approve(root, name) | {'undo': _undo('unapprove', f'approved:{name}')}
        result = legacy_approve(root, kind, parts)
        return result | {'undo': _undo('restore', result['undo_id'])}
    if verb == 'unapprove':
        target = args.get('to') if isinstance(args.get('to'), str) else None
        result = unapprove_to(root, kind, name, target)
        target = target or UNAPPROVE_DEFAULT[kind]
        if kind == 'approved':
            # the inverse returns the set to approved: from provisional through the second hop, from concepts / archive directly
            back = ('approve', f'{target}:{name}')
        else:
            back = ('provisional', f'{target}:{name}')
        return result | {'undo': _undo(*back)}
    if verb == 'reject':
        if kind == 'legacy':
            result = legacy_reject(root, parts)
        else:
            result = reject_set(root, name)
        return result | {'undo': _undo('restore', result['undo_id'])}
    if verb in ('archive', 'unarchive'):
        result = legacy_archive(root, verb, parts) if kind in FLAT_TYPES else archive_set(root, verb, name)
        back = 'unarchive' if verb == 'archive' else 'archive'
        if kind in FLAT_TYPES:
            back_id = result['undo_id']
        else:
            back_id = f'{"archive" if verb == "archive" else "concepts"}:{name}'
        return result | {'undo': _undo(back, back_id)}
    if verb == 'restore':
        result = restore_legacy(root, parts) if kind == 'trashlegacy' else restore_set(root, name)
        return result | {'undo': None}
    raise ValueError(f'Unknown verb {verb}')


# ------------------------------------------------------------------------------------------------------------------ set verbs

def set_action(root: Path, verb: str, set_name: str, rev: object, args: dict) -> dict:
    info = catalog.derive_one(root, set_name)          # the set must exist BEFORE anything is created or locked
    if not info:
        raise catalog.NotFound(f'Unknown set {set_name}')
    if rev is None:
        raise ValueError('rev is required for set verbs')
    race = args.get('race')
    if verb in ('drop_race', 'undrop_race'):
        if not isinstance(race, str):
            raise ValueError('args.race is required')
        if race not in info['required_races']:
            raise ValueError(f'{race} is not a required race of {set_name}')
        if verb == 'drop_race':
            meta = args.get('meta') if isinstance(args.get('meta'), dict) else None
            data = decisions.drop_race(root, set_name, race, rev, info['required_races'], str(args.get('reason') or ''), meta)
            undo = lambda new: _undo('undrop_race', f'set:{set_name}', {'race': race}, new)          # noqa: E731
        else:
            data = decisions.undrop_race(root, set_name, race, rev)
            prior = data.pop('_undropped', {}) or {}
            undo = lambda new: _undo('drop_race', f'set:{set_name}', {'race': race, 'meta': {'time': prior.get('time'), 'reason': prior.get('reason', '')}}, new)  # noqa: E731
    else:     # unapprove: aligned -> 3d clears lod_approval (and the pin with it); completed -> aligned clears aligned_approval
        default = {'aligned': '3d', 'completed': 'aligned'}.get(info['stage'])
        target = args.get('to') if isinstance(args.get('to'), str) else default
        if target not in ('3d', 'aligned') or (not default and not info['locked']):
            raise ValueError(f'{set_name} is in {info["stage"]}; nothing to send back')
        data = decisions.send_back(root, set_name, rev, target)
        undo = lambda new: None                                                        # noqa: E731
    catalog.invalidate()
    fresh = catalog.derive_one(root, set_name)
    serve.log_action(root, verb, f'models/{set_name}', f'models/{set_name}/{decisions.FILENAME}', f'ok rev {data["rev"]}')
    return {'ok': True, 'undo': undo(data['rev']), 'rev': data['rev'], 'stage': fresh['stage'] if fresh else None}


def perform(root: Path, data: object) -> dict:
    """POST /api/v2/action body {"id", "verb", "rev"?, "args"?}."""
    if not isinstance(data, dict):
        raise ValueError('body must be an object')
    item_id, verb = data.get('id'), data.get('verb')
    args = data.get('args') if isinstance(data.get('args'), dict) else {}
    if not isinstance(item_id, str) or not isinstance(verb, str):
        raise ValueError('id and verb are required')
    parts = item_id.split(':')
    if not all(NAME.fullmatch(p) for p in parts):
        raise ValueError('Invalid ID')
    if (parts[0], verb) not in ALLOWED:
        raise ValueError(f'The {verb} verb does not exist for {parts[0]} items')
    try:
        if parts[0] == 'set':
            if len(parts) != 2:
                raise ValueError('Invalid ID')
            return set_action(root, verb, parts[1], data.get('rev'), args)
        if parts[0] == 'intent':
            if len(parts) != 2:
                raise ValueError('Invalid ID')
            return resolve_intent(root, parts[1], verb)
        return concept_action(root, verb, item_id, args)
    finally:
        catalog.invalidate()


# ------------------------------------------------------------------------------------------------------------------ LOD

def _jobs_of(data: dict, wanted: dict) -> dict:
    """{"race/module": job} the client displayed; one `job` for a single write, `jobs` for all / restore."""
    if isinstance(data.get('jobs'), dict):
        jobs = data['jobs']
    elif isinstance(data.get('job'), str) and len(wanted) == 1:
        jobs = {next(iter(wanted)): data['job']}
    else:
        raise ValueError('job (or jobs) is required: the conversion the client displayed')
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in jobs.items()):
        raise ValueError('jobs must map "race/module" to a job id')
    return jobs


def set_lod(root: Path, data: object) -> dict:
    """POST /api/v2/lod. Single {"id","race","module","lod","job","rev"}; all {"id","lod","all":true,"jobs":{key: job},"rev"};
    restore {"id","units":{"race/module": lod|null},"jobs":{key: job},"rev"}. A stale rev, a different conversion than the displayed
    one, or an approved set is a 409; every successful write bumps the set's rev."""
    if not isinstance(data, dict) or not isinstance(data.get('id'), str):
        raise ValueError('id is required')
    item_id = data['id']
    if item_id.startswith('monster:'):
        result = decisions.write_monster_lod(root, item_id, data.get('lod'))
        catalog.invalidate()
        return result
    if not item_id.startswith('set:'):
        raise ValueError('LOD needs a set or monster id')
    set_name = serve.safe_name(item_id.removeprefix('set:'))
    rev = data.get('rev')
    if rev is None:
        raise ValueError('rev is required')
    if not catalog.derive_one(root, set_name):
        raise catalog.NotFound(f'Unknown set {set_name}')
    with decisions.set_lock(root, set_name):
        info = catalog.derive_one(root, set_name)
        if not info:
            raise catalog.NotFound(f'Unknown set {set_name}')
        if info['decisions_error']:
            raise decisions.Unreadable(info['decisions_error'])
        if isinstance(rev, bool) or rev != info['rev']:
            raise decisions.RevConflict(info['rev'], rev)
        if info['locked']:
            raise decisions.LodLocked('approved; send it back to 3D first')
        units = {f'{u["race"]}/{u["module"]}': u for u in info['units']}
        wanted: dict[str, str | None] = {}
        if isinstance(data.get('units'), dict):
            for key, lod in data['units'].items():
                if key not in units:
                    raise ValueError(f'Unknown unit {key}')
                wanted[key] = lod
        elif data.get('all') is True:
            if data.get('lod') not in decisions.LOD_LEVELS:
                raise ValueError('Invalid LOD')
            wanted = {key: data['lod'] for key, u in units.items() if u['status'] == 'reduced'}
            if not wanted:
                raise ValueError('The set has no reduced unit')
        else:
            key = f'{data.get("race")}/{data.get("module")}'
            if key not in units:
                raise ValueError(f'Unknown unit {key}')
            wanted[key] = data.get('lod')
            if wanted[key] not in decisions.LOD_LEVELS:
                raise ValueError('Invalid LOD')
        jobs = _jobs_of(data, wanted)
        writes = []
        for key, lod in wanted.items():
            unit = units[key]
            if unit['status'] != 'reduced':
                raise ValueError(f'{key} is {unit["status"]}, not reduced')
            if jobs.get(key) != unit['job']:
                raise decisions.JobMismatch(f'{key}: the conversion changed ({jobs.get(key)!r} displayed, {unit["job"]!r} now); reload')
            writes.append((unit['model_set'], unit['job'], unit['race'], unit['module'], lod))
        result = decisions.write_lods(root, set_name, rev, writes)       # validates every unit first; all-or-nothing; bumps rev
    catalog.invalidate()
    return {'ok': True, 'written': len(writes), 'previous': result['previous'], 'rev': result['rev']}
