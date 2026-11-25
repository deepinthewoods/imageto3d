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

from contextlib import ExitStack, contextmanager, nullcontext
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import serve  # noqa: E402  (tools/preview/serve.py, used as a library)
import builds  # noqa: E402
import catalog  # noqa: E402
import decisions  # noqa: E402
import guard  # noqa: E402
import taskqueue  # noqa: E402
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
    ('set', 'drop_race'), ('set', 'undrop_race'), ('set', 'unapprove'), ('set', 'approve'), ('set', 'rebuild'),
})
SET_VERBS = ('drop_race', 'undrop_race', 'unapprove', 'approve', 'rebuild')
FLAT_TYPES = ('legacy', 'archlegacy', 'trashlegacy')
WITHDRAWN = guard.APP / 'withdrawn'
PROMOTION_MARK = '.preview-promotion.json'       # written into approved/<set>/ by a legacy approve; travels with the folder into its job when claimed
JOB_STAGES = ('processing_views', 'multiview_ready', 'processing', 'failed', 'completed', serve.HELD)
PUBLISH_WAIT = 30.0          # seconds the Aligned approval waits for a builder's publish lock (the builder holds it only to commit / adopt)
MANUAL_CHECK = ('manual check needed: this entry was recorded before promotions carried an id, so there is no proof that approved/{set} is the '
                'folder it promoted; check approved/{set} by hand (move it back or claim it), then remove the entry')


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

def promotion_of(folder: Path) -> str | None:
    """The promotion id a legacy approve wrote into a promoted set folder (None when there is none)."""
    value = serve.read_json(Path(folder) / PROMOTION_MARK).get('promotion_id')
    return value if isinstance(value, str) else None


def claimed_promotion(root: Path, set_name: str, promotion_id: str) -> str | None:
    """The job folder (`<stage>/<set>--<job>`) a claim moved the promotion `promotion_id` of `set_name` into, if any."""
    for stage in JOB_STAGES:
        parent = root / stage
        if not parent.is_dir():
            continue
        for folder in parent.iterdir():
            if folder.name.split('--')[0] == set_name and '--' in folder.name and promotion_of(folder) == promotion_id:
                return f'{stage}/{folder.name}'
    return None


def withdraw_approved(root: Path, set_name: str, promotion_id: str | None) -> tuple[str, Path | None]:
    """Take `approved/<set>` out of approved/ if nobody claimed it (moved to .codex_jobs/_app/withdrawn/, never deleted).
    Returns ('withdrawn', where it went), or ('absent', None) when the set is not in approved/ and nothing else holds it; a claim, or a copy that already
    moved on to another review stage, is a ValueError and changes nothing. POSITIVE EVIDENCE is required (phase 3 re-check R7): the folder must carry
    the promotion mark `promotion_id`; a folder without a mark or with another one is never withdrawn, and without a `promotion_id` (an entry recorded
    before promotions carried an id) nothing is withdrawn at all ("manual check needed")."""
    src = serve.direct(root, 'approved', set_name)
    if not promotion_id:
        raise ValueError(MANUAL_CHECK.format(set=set_name))
    with _locked_claim(root, set_name):
        if src.exists():
            if _claimed(src):
                raise ValueError(f'{set_name} has already been claimed; it cannot be withdrawn')
            found = promotion_of(src)
            if found != promotion_id:
                raise ValueError(f'approved/{set_name} is not the promotion this entry made ({"another promotion" if found else "it has no promotion mark"}); '
                                 'it was not withdrawn')
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
    """Undo an interrupted flat move: withdraw the promoted set (legacy approve; only the folder carrying THIS promotion's mark, re-check R7), put
    the files back, remove the sidecar."""
    approved = rec.get('approved_set')
    if approved and (rec.get('promoted') or (root / 'approved' / approved).exists()):
        withdraw_approved(root, approved, rec.get('promotion_id'))
    _back_pairs(root, rec.get('files') or [])
    sidecar = rec.get('sidecar')
    if sidecar:
        try:
            (root / sidecar['path']).unlink()
        except FileNotFoundError:
            pass


def _complete_flat(root: Path, rec: dict) -> None:
    """Finish an interrupted flat move. For a legacy approve the promotion must exist: still in approved/ (with this promotion's mark when the
    intent has an id), or already CLAIMED by the supervisor (a job folder carrying this promotion id; phase 1 review #4)."""
    if rec.get('verb') == 'approve':
        approved, promotion = rec.get('approved_set'), rec.get('promotion_id')
        folder = root / 'approved' / approved if approved else None
        here = bool(folder) and folder.is_dir() and (promotion is None or promotion_of(folder) == promotion)
        if not here and not (approved and promotion and claimed_promotion(root, approved, promotion)):
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
    promotion_id = uuid.uuid4().hex

    def promote(rec: dict) -> dict:
        # the promotion id goes INTO the staging folder before workflow.py renames it into approved/ (re-check R7): a claim can never take the
        # promotion before it carries its mark, and nothing here writes into approved/ afterwards
        command = [sys.executable, str(root / 'tools' / 'tripo_pipeline' / 'workflow.py'),
                   '--root', str(root), 'promote', str(batch), key.split('_', 1)[0], '--approve', '--promotion-id', promotion_id]
        run = subprocess.run(command, capture_output=True, text=True, check=False)
        if run.returncode:
            raise ValueError(run.stderr.strip() or run.stdout.strip() or 'Promote failed')
        promoted = json.loads(run.stdout)
        result['promoted'] = promoted | ({'path': str(promoted['path']).replace('\\', '/')} if 'path' in promoted else {})
        if promoted.get('promotion_id') != promotion_id:
            raise ValueError('workflow.py promote did not record the promotion id (an older workflow.py?); the promotion is not trusted')
        sidecar = rec['sidecar']
        sidecar['content'] = sidecar['content'] | {'approved_set': promoted['set'], 'promotion_id': promotion_id}
        return {'approved_set': promoted['set'], 'sidecar': sidecar, 'promoted': True}

    sidecar = (dst / f'{key}.preview-origin.json', {'kind': 'legacy', 'src': f'{origin_dir}/{batch.name}', 'approved_set': approved_set,
                                                     'promotion_id': promotion_id})
    with serve.LOCK:
        try:
            flat_move(root, 'approve', files, dst, sidecar, extra={'approved_set': approved_set, 'promoted': False, 'promotion_id': promotion_id},
                      before=promote)
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
                promotion = origin.get('promotion_id') if isinstance(origin.get('promotion_id'), str) else None
                withdrawn, kept = withdraw_approved(root, serve.safe_name(origin['approved_set']), promotion)
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
    owner = guard.intent_set(rec)                   # also a pre-fix intent without `set` (from its decision path, re-check R12)
    with serve.LOCK, (decisions.set_lock(root, owner) if owner and NAME.fullmatch(owner) else nullcontext()):
        if kind == 'files_tx':
            rec = guard.read_intent(root, intent_id)            # (re-read under the set lock)
            if rec.get('conflict'):
                # a manual choice: Complete keeps the files as they are now, Roll back restores the backups over the later changes
                if verb == 'rollback':
                    guard.recover_files(root, rec, force=True)
            elif verb != 'rollback':
                raise ValueError('An interrupted write can only be rolled back')
            elif guard.recover_files(root, rec) == 'conflict':
                guard.update_intent(root, intent_id, conflict=True, conflict_files=guard.recovery_plan(root, rec.get('files') or [])[1])
                raise guard.Conflict('A file of this write was changed again since; choose Complete (keep) or Roll back (restore the backups)')
        elif kind == 'flat_move':
            (_complete_flat if verb == 'complete' else _rollback_flat)(root, rec)
        else:
            raise ValueError('This recovery entry cannot be resolved here; its file is in .codex_jobs/_app/intents/')
        guard.finish_intent(root, intent_id)
        serve.log_action(root, f'recovery_{verb}', rec.get('label'), None, 'ok')
    return {'ok': True, 'undo': None}


def recover_transactions(root: Path) -> list[str]:
    """Roll back interrupted multi-file writes (server start, and every orchestrator loop): each under its set's lock, and only while every file
    still holds what the transaction wrote or its old content (guard.recover_files); anything else marks the intent `conflict` (a Recovery card
    needing a manual choice; the set stays read-only). Interrupted moves stay listed as Recovery cards. Returns what was done."""
    notes = []
    for rec in guard.list_intents(root):
        if rec.get('kind') != 'files_tx' or rec.get('conflict'):
            continue
        set_name = guard.intent_set(rec)                       # (pre-fix intents: inferred from the decision path, re-check R12)
        set_name = set_name if set_name and NAME.fullmatch(set_name) else None
        try:
            with decisions.set_lock(root, set_name) if set_name else nullcontext():
                try:
                    rec = guard.read_intent(root, rec['id'])
                except FileNotFoundError:
                    continue
                outcome = guard.recover_files(root, rec)
                if outcome == 'restored':
                    guard.finish_intent(root, rec['id'])
                    notes.append(f'rolled back {rec.get("label")}')
                else:
                    guard.update_intent(root, rec['id'], conflict=True, conflict_files=guard.recovery_plan(root, rec.get('files') or [])[1])
                    notes.append(f'conflict: {rec.get("label")} was changed again since; it needs a manual choice (Trash > Recovery)')
        except guard.Busy:
            notes.append(f'{rec.get("label")}: the set is busy; recovery retries later')
        except OSError as exc:
            notes.append(f'could not roll back {rec.get("label")}: {exc}')
    if notes:
        catalog.invalidate()
    return notes


def recover_at_start(root: Path) -> list[str]:
    """Server start (single writer): recover_transactions."""
    return recover_transactions(root)


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

# ------------------------------------------------------------------------------------------------ phase 3: approvals and builds

def _live_units(info: dict) -> list[dict]:
    return [u for u in info['units'] if u['status'] != 'dropped']


def _unit_key(unit: dict) -> str:
    return f'{unit["race"]}/{unit["module"]}'


def _check_writable(info: dict, rev: object) -> None:
    """Under the set lock: readable decisions and the client's rev."""
    if info['decisions_error']:
        raise decisions.Unreadable(info['decisions_error'])
    if isinstance(rev, bool) or not isinstance(rev, int) or rev != info['rev']:
        raise decisions.RevConflict(info['rev'], rev)


def _not_reduced(live: list[dict]) -> list[str]:
    return [f'{_unit_key(u)} ({u["status"].replace("_", " ")})' for u in live if u['status'] != 'reduced']


def _layer_args(root: Path, unit: dict, lod: str, glb_sha256: str) -> dict:
    return {'module_dir': str(builds.module_dir(root, unit['model_set'], unit['job'], unit['race'], unit['module'])), 'lod': lod,
            'race': unit['race'], 'module': unit['module'], 'job': unit['job'], 'model_set': unit['model_set'], 'glb_sha256': glb_sha256}


def _ensure(queue: taskqueue.TaskQueue, lane: str, key: str, key_with_fp, current, retry_failed: bool, set_name: str | None, unit: str,
            args: dict, priority: int, gen: str | None = None, explicit: bool = False) -> str:
    """Make sure a task for `key` is on its way and return what happened: `queued`, `retried`, `running`, `current` (a done task whose build
    is still current and valid), `failed` (left as it is), `cancelled` (the user cancelled it for this approval: left as it is) or `waiting`.
    A done task whose build is NOT current (code or inputs changed, or the build is gone) gets a NEW key (`key_with_fp()`): the fingerprint changed,
    so the work is new. When that fingerprint key is itself done and its publication is still missing or invalid, the row gets a fresh attempt
    (phase 3 review F11: every done key is revalidated, never assumed).
    `gen` = the approval generation the work is for (layers; None for bare): a queued row is re-tagged with it, a failed row of an OLDER generation
    is retried (its failure belongs to the old approval, re-check R11). `explicit` = an Approve / Rebuild / Retry (a user action): only then is a
    row the user cancelled for this generation queued again (re-check R8); reconcile never resurrects it."""
    task = queue.by_key(key)
    if task is None:
        queue.enqueue(lane, key, set_name, unit, args, priority, approval_gen=gen)
        return 'queued'
    state = task['state']
    same_gen = gen is None or task.get('approval_gen') == gen
    if state == 'queued':
        if not same_gen:
            queue.enqueue(lane, key, set_name, unit, args, priority, approval_gen=gen)        # (re-tags the waiting row)
        return 'waiting'                                       # already queued (nothing new)
    if state in taskqueue.ACTIVE:
        return 'running'
    if state == 'cancelled':
        if task.get('cancel_gen') and not explicit and (gen is None or task['cancel_gen'] == gen):
            return 'cancelled'
        queue.enqueue(lane, key, set_name, unit, args, priority, approval_gen=gen)
        return 'queued'
    if state == 'failed':
        if retry_failed or not same_gen:
            queue.retry(task['id'], approval_gen=gen)
            return 'retried'
        return 'failed'
    if current():
        return 'current'
    if key_with_fp is None:
        queue.reset_done(task['id'], 'the published build is missing or invalid: built again', approval_gen=gen)
        return 'queued'
    fp_key = key_with_fp()
    if fp_key == key:
        queue.reset_done(task['id'], 'the published build is missing or invalid: built again', approval_gen=gen)
        return 'queued'
    return _ensure(queue, lane, fp_key, None, current, retry_failed, set_name, unit, args, priority, gen, explicit)


def _bare_current(root: Path, race: str) -> bool:
    ptr = builds.bare_pointer(root, race)
    return bool(ptr) and ptr.get('fingerprint') == builds.fingerprint_bare(race)


def _ensure_bare(root: Path, race: str, retry_failed: bool, explicit: bool = False) -> str:
    return _ensure(taskqueue.for_root(root), 'bare', builds.bare_key(race), lambda: builds.bare_key(race, builds.fingerprint_bare(race)),
                   lambda: _bare_current(root, race), retry_failed, None, race, {'race': race}, 3, None, explicit)


def _ensure_layers(root: Path, set_name: str, unit: dict, approved: dict, retry_failed: bool, gen: str | None, explicit: bool = False) -> str:
    key, lod, job, sha = _unit_key(unit), approved['lod'], approved['job'], approved.get('glb_sha256')
    if not isinstance(sha, str) or not sha:
        return builds.NEEDS_REVIEW                             # an older pin without the source digest is never built (re-check R10)
    module = builds.module_dir(root, unit['model_set'], unit['job'], unit['race'], unit['module'])

    def current() -> bool:
        ptr = builds.pointer_at(module, lod)
        return bool(ptr) and ptr.get('fingerprint') == builds.fingerprint_module(module, lod)
    return _ensure(taskqueue.for_root(root), 'layers', builds.layer_key(set_name, key, lod, job, sha),
                   lambda: builds.layer_key(set_name, key, lod, job, sha, builds.fingerprint_module(module, lod)), current, retry_failed,
                   set_name, key, _layer_args(root, unit, lod, sha), 5, gen, explicit)


def task_gen(root: Path, task: dict) -> str | None:
    """The generation of the 3D approval a layers task's set has NOW (None: no approval / unreadable / a bare task)."""
    set_name = task.get('set_name')
    if task.get('lane') != 'layers' or not isinstance(set_name, str) or not NAME.fullmatch(set_name):
        return None
    try:
        return decisions.approval_gen(decisions.load(root, set_name)['lod_approval'])
    except guard.Conflict:
        return None


def task_approved(root: Path, task: dict, derived: dict | None = None) -> bool:
    """A task may (still) run: a layers task only while its set has an active 3D approval of exactly that unit (job, LOD, source digest, and the
    approval generation the row was queued for, when it carries one: re-check R11); a bare task while some approved set needs its race (phase 3
    review F6). Unreadable decisions count as not approved."""
    args = task.get('args') if isinstance(task.get('args'), dict) else {}
    if task.get('lane') == 'bare':
        race = args.get('race') or task.get('unit')
        derived = derived or catalog.derive_all(root)
        return any(info['locked'] and race in info['races'] for info in derived['sets'].values())
    set_name = task.get('set_name')
    if not isinstance(set_name, str) or not NAME.fullmatch(set_name):
        return False
    try:
        dec = decisions.load(root, set_name)
    except guard.Conflict:
        return False
    entry = ((dec['lod_approval'] or {}).get('units') or {}).get(task.get('unit'))
    if not isinstance(entry, dict) or task.get('unit') in (dec['dropped'] or {}):
        return False
    gen = decisions.approval_gen(dec['lod_approval'])
    return entry.get('job') == args.get('job') and entry.get('lod') == args.get('lod') and bool(entry.get('glb_sha256')) and \
        entry.get('glb_sha256') == args.get('glb_sha256') and task.get('approval_gen') in (None, gen)


def ensure_set_tasks(root: Path, info: dict, retry_failed: bool = False) -> dict[str, str]:
    """Queue what an approved set still needs (idempotent; the orchestrator's reconcile step and Approve both call it): the bare task of every live
    race without a published bare build, and a layers task for every approved unit WITHOUT a valid publication and without a live / queued task.
    A failed unit is only retried when `retry_failed` (a deterministic failure must not loop); a published one (even stale) is left alone; a unit
    whose pinned source changed is never queued (it needs Back to 3D)."""
    approved = (info['decisions']['lod_approval'] or {}).get('units') or {}
    gen = decisions.approval_gen(info['decisions']['lod_approval'])
    actions: dict[str, str] = {}
    for race in sorted({u['race'] for u in _live_units(info)}):
        if not builds.bare_pointer(root, race):
            actions[f'bare:{race}'] = _ensure_bare(root, race, retry_failed, explicit=retry_failed)
    for unit in _live_units(info):
        key = _unit_key(unit)
        rec = info['builds'].get(key)
        if unit['status'] != 'reduced' or not isinstance(approved.get(key), dict) or rec is None:
            continue
        if rec['status'] in builds.BUILT + (builds.SOURCE_CHANGED, builds.NEEDS_REVIEW, 'queued', 'building') or \
                (rec['status'] in ('failed', builds.CANCELLED) and not retry_failed):
            continue
        actions[key] = _ensure_layers(root, info['name'], unit, approved[key], retry_failed, gen, explicit=retry_failed)
    return {k: v for k, v in actions.items() if v not in ('waiting', 'cancelled')}        # only what this call changed


def _units_arg(args: dict) -> tuple[dict[str, dict], dict[str, str]]:
    """(args.units, args.lods). `units` = {"race/module": {job, lod}} for EVERY live unit the confirmation showed; `lods` = {"race/module": lod}
    for the never-saved cells only (DESIGN_PHASE3.md E1; each equals units[key].lod)."""
    units = args.get('units')
    if not isinstance(units, dict) or not units or not all(isinstance(k, str) and isinstance(v, dict) and isinstance(v.get('job'), str)
                                                           and isinstance(v.get('lod'), str) for k, v in units.items()):
        raise ValueError('args.units is required: {"race/module": {"job", "lod"}} for EVERY unit the client displayed')
    lods = args.get('lods') if args.get('lods') is not None else {}
    if not isinstance(lods, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in lods.items()):
        raise ValueError('args.lods must map "race/module" to a LOD')
    bad = sorted({v['lod'] for v in units.values()} | set(lods.values()) - set(decisions.LOD_LEVELS))
    bad = [b for b in bad if b not in decisions.LOD_LEVELS]
    if bad:
        raise ValueError(f'Invalid LOD {bad[0]!r}')
    wrong = sorted(k for k, v in lods.items() if k in units and units[k]['lod'] != v)
    if wrong:
        raise ValueError(f'args.lods contradicts args.units for {", ".join(wrong)}')
    return units, lods


def _diff(kind: str, shown: dict, now: dict, label) -> list[str]:
    """Differences between what the client displayed and what the server has (missing, extra, changed)."""
    out = []
    for key in sorted(set(shown) | set(now)):
        if key not in shown:
            out.append(f'{kind} {key} was not displayed')
        elif key not in now:
            out.append(f'{kind} {key} is not part of the set any more')
        elif shown[key] != now[key]:
            out.append(f'{kind} {key}: {label(shown[key])} displayed, {label(now[key])} now')
    return out


def approve_3d(root: Path, set_name: str, rev: object, args: dict) -> dict:
    """`approve` in 3d (DESIGN_PHASE3.md A, C, E), under the set lock. `args.units` = {"race/module": {job, lod}} for EVERY live unit the client
    displayed (unsaved cells included); any difference from the resolved job and the saved-or-default LOD, a missing or an extra unit is a 409
    `changed` (review F1). Then ONE transaction (the unsaved picks' chosen_lod.json + lod_approval with glb_sha256 and required_races); the
    tasks are queued afterwards, best effort: the orchestrator's reconcile step queues whatever is missing (review F7)."""
    shown, unsaved = _units_arg(args)
    with decisions.set_lock(root, set_name):
        info = catalog.derive_one(root, set_name)
        if not info:
            raise catalog.NotFound(f'Unknown set {set_name}')
        _check_writable(info, rev)
        if info['locked']:
            raise guard.AlreadyApproved(f'{set_name} is already approved in 3D; send it back first')
        live = _live_units(info)
        if info['stage'] != '3d' or not live:
            missing = _not_reduced(live)
            raise ValueError(f'{set_name} is not ready for approval: ' + (', '.join(missing) if missing else 'every race is dropped'))
        now = {_unit_key(u): {'job': u['job'], 'lod': u['lod']} for u in live}
        diffs = _diff('unit', {k: {'job': v['job'], 'lod': v['lod']} for k, v in shown.items()}, now, lambda v: f'job {v["job"]} lod {v["lod"]}')
        saved = {_unit_key(u) for u in live if u['lod_saved']}
        # args.lods must name EXACTLY the never-saved units (re-check R15): a saved one, an unknown one or a missing one is a change
        diffs += [f'unit {k}: its LOD was saved meanwhile' for k in sorted(set(unsaved) & saved)]
        diffs += [f'unit {k}: shown as saved, but it has no saved LOD' for k in sorted(set(now) - saved - set(unsaved)) if k in shown]
        diffs += [f'unit {k}: in args.lods but not a unit of the set' for k in sorted(set(unsaved) - set(now))]
        if diffs:
            raise guard.Changed('the set changed since it was displayed (' + '; '.join(diffs[:6]) + '); review it again', changes=diffs)
        approved: dict[str, dict] = {}
        writes: list[tuple[str, str, str, str, str]] = []
        for unit in live:
            key, lod = _unit_key(unit), unit['lod']
            if not unit['lod_saved']:
                writes.append((unit['model_set'], unit['job'], unit['race'], unit['module'], lod))
            glb = builds.source_glb(builds.module_dir(root, unit['model_set'], unit['job'], unit['race'], unit['module']), lod)
            if not glb.is_file():
                raise ValueError(f'{key} has no {lod} model')
            approved[key] = {'job': unit['job'], 'lod': lod, 'glb_sha256': builds.file_sha256(glb)}
        data = decisions.approve_3d(root, set_name, rev, writes, approved, info['required_races'])
        queue_error = None
        try:
            catalog.invalidate()
            fresh = catalog.derive_one(root, set_name)
            if fresh:
                ensure_set_tasks(root, fresh, retry_failed=True)
        except Exception as exc:  # noqa: BLE001  (the approval stands; the reconcile step queues the work)
            queue_error = f'{type(exc).__name__}: {exc}'
    out = {'ok': True, 'undo': _undo('unapprove', f'set:{set_name}', {'to': '3d'}, data['rev']), 'rev': data['rev'], 'queued': len(live)}
    return out | ({'queue_error': queue_error} if queue_error else {})


def _amount_arg(raw: object, kind: str, races: list[str]) -> dict[str, dict[str, int]]:
    """args.amount = {race: {group: 0..100}}: the client's FULL map (every live race, every group of the kind). Another set of races is a 409
    `changed`; a missing / unknown group or a bad value a 400."""
    if not isinstance(raw, dict):
        raise ValueError('args.amount is required: {race: {group: 0..100}} for every race the client displayed')
    diffs = _diff('race', {r: True for r in raw}, {r: True for r in races}, str)
    if diffs:
        raise guard.Changed('the races changed since the set was displayed (' + '; '.join(diffs[:6]) + ')', changes=diffs)
    groups = catalog.AMOUNT_GROUPS.get(kind, ('outfit',))
    out: dict[str, dict[str, int]] = {}
    for race, values in raw.items():
        if not isinstance(values, dict) or sorted(values) != sorted(groups):
            raise ValueError(f'amount.{race}: a {kind} set has exactly the groups {", ".join(groups)}')
        out[race] = {group: _percent(value) for group, value in values.items()}
    return out


def _percent(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ValueError('value must be an integer from 0 to 100')
    return value


def _ids_arg(args: dict, name: str) -> dict[str, str | None]:
    raw = args.get(name)
    if not isinstance(raw, dict) or not all(isinstance(k, str) and (v is None or isinstance(v, str)) for k, v in raw.items()):
        raise ValueError(f'args.{name} is required: the {"build" if name == "builds" else "bare build"} id the client displayed per '
                         f'{"unit" if name == "builds" else "race"} (null for none)')
    return raw


def approve_aligned(root: Path, set_name: str, rev: object, args: dict) -> dict:
    """`approve` in aligned / completed (DESIGN_PHASE3.md A, D.5, E). The request carries what the client displayed: `args.builds` =
    {"race/module": build_id}, `args.bare` = {race: bare build_id}, `args.amount` (full map); under the set lock they must be the published ids
    (409 `changed`, review F1). Every displayed build is then re-checked synchronously: the module and bare fingerprints are recomputed and the
    approved source GLB rehashed; a stale build or a changed source is a 409 `stale` (review F2). ONE transaction writes `aligned_approval`
    (units, bare ids, amount, a new `approval_id`) and `models/<set>/ready.json` (same approval_id; every path relative to the project root and
    checked to resolve inside its build folder, review F4)."""
    shown_builds, shown_bare = _ids_arg(args, 'builds'), _ids_arg(args, 'bare')
    with decisions.set_lock(root, set_name):
        info = catalog.derive_one(root, set_name)
        if not info:
            raise catalog.NotFound(f'Unknown set {set_name}')
        _check_writable(info, rev)
        if info['stage'] not in ('aligned', 'completed'):          # completed: approving again (new amounts) rewrites aligned_approval and ready.json
            # an Aligned payload is always answered with the Aligned conflicts (re-check R15): the client reloads on a 409, never on a 400
            moved = [k for k, rec in info['builds'].items() if rec['status'] in (builds.SOURCE_CHANGED, builds.NEEDS_REVIEW)]
            if moved:
                raise guard.StaleBuild(f'the approved LOD file of {", ".join(moved)} changed since the 3D approval; send the set back to 3D',
                                       changes=[f'{k}: {info["builds"][k]["status"].replace("_", " ")}' for k in moved])
            raise guard.Changed(f'{set_name} is in {info["stage"]} now, not in Aligned; review it again',
                                changes=[f'stage: aligned displayed, {info["stage"]} now'])
        live = _live_units(info)
        pins = [_unit_key(u) for u in live if u['status'] == 'pin_missing']
        if pins:
            raise ValueError(f'the approved conversion of {", ".join(pins)} is missing; send the set back to 3D')
        races = sorted({u['race'] for u in live}, key=catalog.race_rank)
        now_builds = {_unit_key(u): (info['builds'].get(_unit_key(u)) or {}).get('build_id') for u in live}
        snaps = info.get('bare_snaps') or {}
        bare_ptrs = {race: (snaps[race] if race in snaps else builds.bare_snapshot(root, race))[0] for race in races}
        now_bare = {race: ptr['build_id'] if ptr else None for race, ptr in bare_ptrs.items()}
        diffs = _diff('build', shown_builds, now_builds, str) + _diff('bare', shown_bare, now_bare, str)
        if diffs:
            raise guard.Changed('the layers changed since they were displayed (' + '; '.join(diffs[:6]) + '); review them again', changes=diffs)
        given = _amount_arg(args.get('amount'), info['kind'], races)
        changed = [k for k, rec in info['builds'].items() if rec['status'] == builds.SOURCE_CHANGED]
        if changed:
            raise guard.StaleBuild(f'the approved LOD file of {", ".join(changed)} changed since the 3D approval; send the set back to 3D',
                                   changes=[f'{k}: source changed' for k in changed])
        problems = [f'{key} ({rec["status"]})' for key, rec in info['builds'].items() if rec['status'] not in builds.BUILT]
        problems += [f'{_unit_key(u)} (no build)' for u in live if _unit_key(u) not in info['builds']]
        if problems:
            raise ValueError('layer builds are not published: ' + ', '.join(problems))
        no_bare = [race for race in races if not bare_ptrs[race]]
        if no_bare:
            raise ValueError('the bare layers are not built for: ' + ', '.join(no_bare))
        approved = info['decisions']['lod_approval']['units']
        stale = []
        modules = {}
        for unit in live:
            key = _unit_key(unit)
            rec = info['builds'][key]
            module = builds.module_dir(root, unit['model_set'], unit['job'], unit['race'], unit['module'])
            modules[key] = (unit, module, rec['lod'])
            try:
                if builds.lb().fingerprint_module(module, rec['lod']) != (builds.module_snapshot(module, rec['lod'])[0] or {}).get('fingerprint'):
                    stale.append(f'{key} (code or inputs changed since the build)')
                if builds.file_sha256(builds.source_glb(module, rec['lod'])) != approved[key].get('glb_sha256'):
                    stale.append(f'{key} (the approved LOD file changed)')
            except OSError as exc:
                stale.append(f'{key} ({exc})')
        for race in races:
            if builds.lb().fingerprint_bare(race) != bare_ptrs[race].get('fingerprint'):
                stale.append(f'bare {race} (code or game body changed since the build)')
        stale = [f'{key} (marked stale by the stale scan)' for key, rec in info['builds'].items() if rec['status'] == 'stale' and
                 not any(s.startswith(key + ' ') for s in stale)] + stale
        if stale:
            raise guard.StaleBuild('stale layers cannot be approved: ' + ', '.join(stale[:8]) + '; rebuild them (or send the set back to 3D)', changes=stale)
        # Re-check R2 / R3: hold the builder's publish lock of EVERY output root the manifest names (module layers folders and bare race folders, in
        # sorted path order; the builder never takes a set lock), re-read every pointer uncached (full validation, slot bytes rehashed against the
        # recorded digests), compare with what the client displayed and write the decision + ready.json before any builder can publish again.
        roots = sorted({str(module / 'layers') for _u, module, _l in modules.values()} | {str(builds.bare_root(root) / race) for race in races})
        with ExitStack() as stack:
            for out_root in roots:
                Path(out_root).mkdir(parents=True, exist_ok=True)
                try:
                    stack.enter_context(builds.lb()._publish_lock(Path(out_root), PUBLISH_WAIT))
                except TimeoutError:
                    raise guard.Busy('a layer build of this set is being published right now; try again in a moment') from None
            changes, units, ready_units, bare_dirs = [], {}, {race: {} for race in races}, {}
            for key, (unit, module, lod) in modules.items():
                ptr, problem = builds.module_snapshot(module, lod, fresh=True)
                if not ptr or ptr['build_id'] != shown_builds.get(key):
                    changes.append(f'build {key}: {shown_builds.get(key)} displayed, {ptr["build_id"] if ptr else "none (" + str(problem) + ")"} now')
                    continue
                out_root, folder = module / 'layers', module / 'layers' / ptr['build_id']
                slots = {}
                for slot in sorted(ptr['slots']):
                    glb = builds.contained(out_root / ptr['slots'][slot]['glb'], folder)
                    slots[slot] = {'glb': builds.rel_path(root, glb), 'sha256': ptr['slots'][slot]['sha256']}     # (verified just now)
                units[key] = {'job': approved[key]['job'], 'lod': lod, 'build_id': ptr['build_id']}
                ready_units[unit['race']][unit['module']] = units[key] | {'slots': slots}
            for race in races:
                ptr, problem = builds.bare_snapshot(root, race, fresh=True)
                if not ptr or ptr['build_id'] != shown_bare.get(race):
                    changes.append(f'bare {race}: {shown_bare.get(race)} displayed, {ptr["build_id"] if ptr else "none (" + str(problem) + ")"} now')
                    continue
                if ptr.get('fingerprint') != bare_ptrs[race].get('fingerprint'):
                    changes.append(f'bare {race}: republished while approving')
                    continue
                out_root = builds.bare_root(root) / race
                bare_dirs[race] = builds.rel_path(root, builds.contained(out_root / ptr['build_id'], out_root))
            if changes:
                raise guard.Changed('the layers changed while approving (' + '; '.join(changes[:6]) + '); review them again', changes=changes)
            stamp, approval_id = guard.now(), uuid.uuid4().hex
            ready = {'schema': 1, 'set': set_name, 'kind': info['kind'], 'approved_at': stamp, 'approval_id': approval_id,
                     'races': {race: {'amount': given[race], 'units': ready_units[race]} for race in races}, 'bare': bare_dirs}
            raw = decisions.ready_bytes(ready)
            aligned = {'time': stamp, 'approval_id': approval_id, 'units': units, 'amount': given, 'bare': {r: now_bare[r] for r in races},
                       'ready_sha256': decisions.ready_digest(raw)}
            data = decisions.approve_aligned(root, set_name, rev, aligned, given, decisions.models_dir(root, set_name) / decisions.READY, raw)
    return {'ok': True, 'undo': _undo('unapprove', f'set:{set_name}', {'to': 'aligned'}, data['rev']), 'rev': data['rev'], 'approval_id': approval_id}


def rebuild_set(root: Path, set_name: str, rev: object, args: dict) -> dict:
    """`rebuild` (optionally one `race` / `module`): failed tasks are retried; stale, unpublished or lost builds get a task with a new key (the
    fingerprint changed); a current build stays current; a unit whose pinned source changed is not built (`source_changed`: Back to 3D). The
    bare layers of the affected races are included. Writes no decision."""
    race_filter, module_filter = args.get('race'), args.get('module')
    for value in (race_filter, module_filter):
        if value is not None and not isinstance(value, str):
            raise ValueError('args.race / args.module must be text')
    with decisions.set_lock(root, set_name):
        info = catalog.derive_one(root, set_name)
        if not info:
            raise catalog.NotFound(f'Unknown set {set_name}')
        if info['decisions_error']:
            raise decisions.Unreadable(info['decisions_error'])
        if not info['locked']:
            raise ValueError(f'{set_name} is not approved in 3D: nothing to rebuild')
        approved = info['decisions']['lod_approval']['units']
        gen = decisions.approval_gen(info['decisions']['lod_approval'])
        picked = [u for u in _live_units(info) if u['status'] == 'reduced' and isinstance(approved.get(_unit_key(u)), dict)
                  and race_filter in (None, u['race']) and module_filter in (None, u['module'])]
        if not picked:
            raise ValueError('no approved unit matches')
        actions: dict[str, str] = {}
        for race in sorted({u['race'] for u in picked}):
            actions[f'bare:{race}'] = _ensure_bare(root, race, True, explicit=True)
        for unit in picked:
            if unit.get('source_changed'):
                actions[_unit_key(unit)] = builds.SOURCE_CHANGED
                continue
            actions[_unit_key(unit)] = _ensure_layers(root, set_name, unit, approved[_unit_key(unit)], True, gen, explicit=True)
    queued = sum(1 for v in actions.values() if v in ('queued', 'retried'))
    return {'ok': True, 'undo': None, 'queued': queued, 'actions': actions}


def unapprove_set(root: Path, set_name: str, rev: object, info: dict, args: dict) -> dict:
    """`unapprove`: 3d (building) / aligned -> 3d clears lod_approval; completed -> aligned clears aligned_approval; any step that clears
    aligned_approval also revokes ready.json. ONE transaction (rev checked before anything is renamed; review F7). Queued tasks of the set are
    cancelled afterwards (best effort: the reconcile step cancels whatever has no matching approval); running ones finish once."""
    default = {'aligned': '3d', 'completed': 'aligned'}.get(info['stage']) or ('3d' if info['locked'] else None)
    target = args.get('to') if isinstance(args.get('to'), str) else default
    if target not in ('3d', 'aligned') or default is None:
        raise ValueError(f'{set_name} is in {info["stage"]}; nothing to send back')
    data = decisions.send_back(root, set_name, rev, target)
    cancelled = 0
    if target == '3d':
        try:
            cancelled = taskqueue.for_root(root).cancel_set(set_name)
        except taskqueue.QueueError:
            pass
    return data | {'_cancelled': cancelled}


def set_action(root: Path, verb: str, set_name: str, rev: object, args: dict) -> dict:
    info = catalog.derive_one(root, set_name)          # the set must exist BEFORE anything is created or locked
    if not info:
        raise catalog.NotFound(f'Unknown set {set_name}')
    if rev is None:
        raise ValueError('rev is required for set verbs')
    race = args.get('race')
    extra: dict = {}
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
    elif verb == 'approve':
        # route by the PAYLOAD first (re-check R15): an Aligned approval (args.builds / args.bare) always gets the Aligned checks, a 3D one
        # (args.units) the 3D checks, whatever the stage became since the client drew it; each answers a changed stage with a 409
        if 'builds' in args or 'bare' in args:
            result = approve_aligned(root, set_name, rev, args)
        elif 'units' in args or info['stage'] in ('3d', 'approved'):
            result = approve_3d(root, set_name, rev, args)
        else:
            result = approve_aligned(root, set_name, rev, args)
        return _finish_set(root, verb, set_name, result)
    elif verb == 'rebuild':
        return _finish_set(root, verb, set_name, rebuild_set(root, set_name, rev, args))
    else:     # unapprove: see unapprove_set
        data = unapprove_set(root, set_name, rev, info, args)
        extra = {'cancelled': data.pop('_cancelled'), 'revoked': data.pop('_revoked')}
        undo = lambda new: None                                                        # noqa: E731
    catalog.invalidate()
    fresh = catalog.derive_one(root, set_name)
    serve.log_action(root, verb, f'models/{set_name}', f'models/{set_name}/{decisions.FILENAME}', f'ok rev {data["rev"]}')
    return {'ok': True, 'undo': undo(data['rev']), 'rev': data['rev'], 'stage': fresh['stage'] if fresh else None} | extra


def _finish_set(root: Path, verb: str, set_name: str, result: dict) -> dict:
    catalog.invalidate()
    fresh = catalog.derive_one(root, set_name)
    serve.log_action(root, verb, f'models/{set_name}', f'models/{set_name}/{decisions.FILENAME}', f'ok rev {result.get("rev", "-")}')
    return result | {'stage': fresh['stage'] if fresh else None} | ({} if 'rev' in result else {'rev': fresh['rev'] if fresh else None})


def set_amount(root: Path, data: object) -> dict:
    """POST /api/v2/amount {id: "set:S", race, group, value, rev}: save one Amount. The group must match the set's kind (outfit: `outfit`;
    armour: `body` / `limbs`). Needs the 3D approval (the sliders live in Aligned). A different value bumps the rev; the stage stays (a
    Completed set shows the "amount changed since approval" badge until it is approved again)."""
    if not isinstance(data, dict) or not isinstance(data.get('id'), str) or not data['id'].startswith('set:'):
        raise ValueError('id (set:<name>) is required')
    set_name = serve.safe_name(data['id'].removeprefix('set:'))
    rev, race, group = data.get('rev'), data.get('race'), data.get('group')
    if rev is None:
        raise ValueError('rev is required')
    value = _percent(data.get('value'))
    if not isinstance(race, str) or not isinstance(group, str):
        raise ValueError('race and group are required')
    if not catalog.derive_one(root, set_name):
        raise catalog.NotFound(f'Unknown set {set_name}')
    with decisions.set_lock(root, set_name):
        info = catalog.derive_one(root, set_name)
        if not info:
            raise catalog.NotFound(f'Unknown set {set_name}')
        _check_writable(info, rev)
        groups = catalog.AMOUNT_GROUPS.get(info['kind'], ('outfit',))
        if group not in groups:
            raise ValueError(f'a {info["kind"]} set has the amount groups {", ".join(groups)}, not {group!r}')
        if race not in info['races']:
            raise ValueError(f'{race} is not a live race of {set_name}')
        if not info['locked']:
            raise ValueError(f'{set_name} is not approved in 3D yet')
        result = decisions.set_amount(root, set_name, rev, race, group, value)
    catalog.invalidate()
    fresh = catalog.derive_one(root, set_name)
    return {'ok': True, 'rev': result['rev'], 'amount': result['amount'], 'stage': fresh['stage'] if fresh else None}


QUEUE_OPS = ('retry', 'cancel', 'kill_retry', 'rebuild_stale')


def rebuild_stale(root: Path) -> dict:
    """POST /api/v2/queue {op: "rebuild_stale"}: for every set in 3d / aligned / completed with a 3D approval, queue the rebuild of every stale
    published layer build (a new fingerprint key) and of the stale bare builds of its races (DESIGN_PHASE3.md E4). Returns {queued, sets}."""
    derived = catalog.derive_all(root)
    queued, sets = 0, []
    for name, info in sorted(derived['sets'].items()):
        if not info['locked'] or info['stage'] not in ('3d', 'aligned', 'completed') or info['decisions_error'] or info.get('recovering'):
            continue
        units = [u for u in _live_units(info) if (info['builds'].get(_unit_key(u)) or {}).get('status') == 'stale']
        # a stale bare build counts on its own (re-check R14): the stale scan's entry must still name the published bare build
        races = sorted(race for race, rec in (info.get('bare_builds') or {}).items() if rec['status'] == 'stale')
        if not units and not races:
            continue
        with decisions.set_lock(root, name):
            fresh = catalog.derive_one(root, name)
            if not fresh or not fresh['locked']:
                continue
            approved = fresh['decisions']['lod_approval']['units']
            gen = decisions.approval_gen(fresh['decisions']['lod_approval'])
            got = [_ensure_bare(root, race, True, explicit=True) for race in races]
            for unit in _live_units(fresh):
                key = _unit_key(unit)
                if (fresh['builds'].get(key) or {}).get('status') == 'stale' and isinstance(approved.get(key), dict):
                    got.append(_ensure_layers(root, name, unit, approved[key], True, gen, explicit=True))
        n = sum(1 for g in got if g in ('queued', 'retried'))
        if n:
            queued += n
            sets.append(name)
    catalog.invalidate()
    return {'ok': True, 'queued': queued, 'sets': sets}


def queue_op(root: Path, data: object) -> dict:
    """POST /api/v2/queue {op: "retry" | "cancel" | "kill", id}: Retry puts a failed / cancelled task back in the queue (fresh attempts), only
    while its approval is still active (a withdrawn set's task is refused, review F6); Cancel stops a QUEUED one; Kill (& retry) flags a
    starting / running one (e.g. stalled): the orchestrator kills its builder, then its wrapper (each by PID after a command-line and
    create-time check) and queues a fresh attempt (review F10). Phase 4 extends this."""
    if not isinstance(data, dict) or data.get('op') not in QUEUE_OPS:
        raise ValueError('op must be retry, cancel, kill_retry or rebuild_stale')
    if data['op'] == 'rebuild_stale':
        return rebuild_stale(root)
    task_id = data.get('id')
    if isinstance(task_id, bool) or not isinstance(task_id, int):
        raise ValueError('id (a task number) is required')
    queue = taskqueue.for_root(root)
    task = queue.get(task_id)
    if task is None:
        raise catalog.NotFound(f'Unknown task {task_id}')
    op = data['op']
    gen = task_gen(root, task)
    if op == 'retry':
        if task['state'] in ('failed', 'cancelled') and (not task_approved(root, task) or
                                                        (task['lane'] == 'layers' and task.get('approval_gen') not in (None, gen))):
            raise ValueError(f'task {task_id} belongs to an approval that was withdrawn or changed; approve the set again instead')
        done = queue.retry(task_id, approval_gen=gen if task['lane'] == 'layers' else None)
    elif op == 'cancel':
        # a MANUAL cancel holds for the current approval generation: reconcile never queues it again (re-check R8)
        done = queue.cancel(task_id, 'manual', manual_gen=(gen or 'manual') if task['lane'] == 'layers' else 'manual')
    else:
        import time
        token = data.get('token')
        if not isinstance(token, str) or not token:
            raise ValueError('token is required: the attempt (queue row `token`) the client saw; Kill & retry acts on that attempt only')
        if task.get('token') != token:
            raise guard.Changed(f'task {task_id} runs another attempt now; reload the queue', changes=[f'task {task_id}: attempt changed'])
        if task['state'] != 'orphan' and not builds.is_stalled(task, time.time(), builds.stall_minutes(root)):
            raise ValueError(f'task {task_id} is not stalled; Kill & retry is only for a build whose output stopped (or an orphaned builder)')
        done = queue.request_kill(task_id, token)
    if not done:
        state = queue.get(task_id)['state']
        raise ValueError(f'task {task_id} is {state}; ' + {'retry': 'only a failed or cancelled task can be retried',
                                                          'cancel': 'only a queued task can be cancelled',
                                                          'kill_retry': 'only a starting, running or orphaned task can be killed'}[op])
    catalog.invalidate()
    row = queue.get(task_id)
    return {'ok': True, 'id': task_id, 'state': row['state'], 'kill_requested': bool(row.get('kill_requested'))}


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
