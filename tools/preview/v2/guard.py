"""Cross-process safety of the v2 preview app: conflict errors, OS locks, the single-writer server lock, move/write intents and the
multi-file write transaction (DESIGN.md section 6). Stdlib only; nothing here knows about sets or concepts.

* `OsLock` = a per-thread re-entrant lock that also holds an OS byte lock on a lock file, so two PROCESSES (two v2 servers, a
  server and a pipeline tool) never run the same read-check-write at once. The OS releases it when its holder dies.
* `ServerLock` (`.codex_jobs/_app/server.lock`): the one v2 server per root that may write; a second server serves read-only.
* Intents (`.codex_jobs/_app/intents/<id>.json`) are written BEFORE a multi-step change and deleted when it finished, so an
  interrupted change is found again (listed in the Trash tab as "Recovery", or rolled back at the next server start).
* `apply_transaction`: all-or-nothing replacement of several small files (in-memory backups + an intent for a crash). The intent names the
  set it belongs to and the sha256 of every file's old and new content: recovery (`recover_files`) restores a backup only while each file still
  holds what the transaction wrote (or its old content); anything else is a CONFLICT left for a manual choice (phase 3 review F8). While a set has
  an unresolved intent every write of that set is refused (`check_recovering`, HTTP 409 `recovering`).
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid

HERE = Path(__file__).resolve().parent
PIPELINE = HERE.parents[1] / 'tripo_pipeline'
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))
from claim_lock import release_lock, try_lock  # noqa: E402

APP = Path('.codex_jobs') / '_app'
LOCK_WAIT = 15.0            # seconds an OsLock waits for another process before giving up (HTTP 409 "busy")


# ------------------------------------------------------------------------------------------------------------------ errors

class Conflict(Exception):
    """Refused because the item is not in the state the request assumed (HTTP 409). `code` tells the client why."""
    status = 409
    code = 'conflict'

    def __init__(self, message: str, **extra) -> None:
        super().__init__(message)
        self.extra = extra


class RevConflict(Conflict):
    """The client's `rev` is not the file's current one."""
    code = 'rev'

    def __init__(self, current: int, given: object) -> None:
        super().__init__(f'stale rev: the set is at rev {current}, the request carried {given}')
        self.current = current


class Unreadable(Conflict):
    """pipeline_v2.json exists but cannot be read or does not have the expected shape: no mutation until it is fixed."""
    code = 'unreadable'


class LodLocked(Conflict):
    """The persisted LOD of an approved unit may not change (send the set back to 3D first)."""
    code = 'locked'


class JobMismatch(Conflict):
    """The unit's conversion is not the one the client displayed."""
    code = 'job'


class AlreadyApproved(Conflict):
    """The set already has the approval this verb would write (send it back first)."""
    code = 'approved'


class StaleChoice(Conflict):
    """A LOD or conversion the client sent is not what the server has (reload)."""
    code = 'lod'


class Busy(Conflict):
    """Another process holds the lock (a claim in progress, or another writer)."""
    code = 'busy'


class Changed(Conflict):
    """The approval request does not describe what the server has now (another conversion, LOD, build or race than the client displayed)."""
    code = 'changed'


class StaleBuild(Conflict):
    """A displayed build is stale (code / inputs changed) or its pinned source GLB changed: rebuild, or send the set back to 3D."""
    code = 'stale'


class Recovering(Conflict):
    """The set has an interrupted multi-file change that is not resolved yet: read-only until recovery finishes."""
    code = 'recovering'


class ReadOnly(Exception):
    """This server does not hold the single-writer lock (HTTP 503)."""
    status = 503


# ------------------------------------------------------------------------------------------------------------------- locks

class OsLock:
    """Per-thread re-entrant lock + OS byte lock on `path` (created on first use)."""

    def __init__(self, path: Path, wait: float = LOCK_WAIT) -> None:
        self.path = Path(path)
        self.wait = wait
        self._rlock = threading.RLock()
        self._depth = 0
        self._handle = None

    def __enter__(self) -> 'OsLock':
        self._rlock.acquire()
        try:
            if self._depth == 0:
                self._acquire_os()
            self._depth += 1
        except BaseException:
            self._rlock.release()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._release_os()
        self._rlock.release()

    def _acquire_os(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, 'a+b')
        deadline = time.monotonic() + self.wait
        while not try_lock(handle):
            if time.monotonic() >= deadline:
                handle.close()
                raise Busy(f'{self.path.name}: another process is writing this item, try again')
            time.sleep(0.02)
        self._handle = handle

    def _release_os(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            try:
                release_lock(handle)
            finally:
                handle.close()


_LOCKS: dict[str, OsLock] = {}
_GUARD = threading.Lock()


def os_lock(path: Path, wait: float = LOCK_WAIT) -> OsLock:
    """The (process-wide) OsLock of a lock file."""
    key = str(path)
    with _GUARD:
        return _LOCKS.setdefault(key, OsLock(path, wait))


class ServerLock:
    """Single-writer lock of a root: `acquire()` is False when another v2 server holds it (that one writes, this one is read-only)."""

    def __init__(self, root: Path) -> None:
        self.path = Path(root) / APP / 'server.lock'
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, 'a+b')
        if not try_lock(handle):
            handle.close()
            return False
        self.handle = handle
        try:
            handle.seek(0)
            handle.truncate(0)
            handle.write(f'{os.getpid()}\n'.encode())
            handle.flush()
        except OSError:
            pass
        return True

    def release(self) -> None:
        handle, self.handle = self.handle, None
        if handle:
            try:
                release_lock(handle)
            except OSError:
                pass
            handle.close()


# ----------------------------------------------------------------------------------------------------------------- file io

def replace_retry(src: Path, dst: Path, retries: int = 8) -> None:
    """os.replace, retried when Windows refuses it for a moment (a reader or a virus scanner holds the file)."""
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def tmp_name(path: Path) -> Path:
    return path.with_name(f'{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:6]}.tmp')


def write_atomic(path: Path, data: bytes) -> None:
    """Write bytes to `path` through a unique temp file + replace_retry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_name(path)
    try:
        tmp.write_bytes(data)
        replace_retry(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def read_bytes_or_none(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


# --------------------------------------------------------------------------------------------------------------- intents

_ACTIVE: set[str] = set()


def intents_dir(root: Path) -> Path:
    return Path(root) / APP / 'intents'


def new_intent(root: Path, record: dict) -> str:
    """Persist an intent record and mark it active (this process is working on it right now)."""
    intent_id = uuid.uuid4().hex[:16]
    with _GUARD:
        _ACTIVE.add(intent_id)
    write_atomic(intents_dir(root) / f'{intent_id}.json', json.dumps({'id': intent_id, 'created': now(), 'pid': os.getpid()} | record, indent=1).encode())
    return intent_id


def update_intent(root: Path, intent_id: str, **fields) -> None:
    path = intents_dir(root) / f'{intent_id}.json'
    record = json.loads(path.read_text(encoding='utf-8'))
    write_atomic(path, json.dumps(record | fields, indent=1).encode())


def finish_intent(root: Path, intent_id: str) -> None:
    """The change is complete (or fully rolled back): forget the intent."""
    with _GUARD:
        _ACTIVE.discard(intent_id)
    try:
        (intents_dir(root) / f'{intent_id}.json').unlink()
    except FileNotFoundError:
        pass


def abandon_intent(intent_id: str) -> None:
    """Leave the intent file for recovery (a rollback failed); it is no longer this process's live work."""
    with _GUARD:
        _ACTIVE.discard(intent_id)


def read_intent(root: Path, intent_id: str) -> dict:
    path = intents_dir(root) / f'{intent_id}.json'
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise FileNotFoundError(f'Unknown recovery entry {intent_id}') from None
    if not isinstance(record, dict):
        raise FileNotFoundError(f'Unknown recovery entry {intent_id}')
    return record


def list_intents(root: Path) -> list[dict]:
    """Incomplete intents, oldest first; the ones this process is working on right now are left out."""
    folder = intents_dir(root)
    out = []
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith('.json'))
    except OSError:
        return []
    with _GUARD:
        active = set(_ACTIVE)
    for name in names:
        intent_id = name[:-5]
        if intent_id in active:
            continue
        try:
            record = json.loads((folder / name).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            record = {'id': intent_id, 'kind': 'unreadable', 'label': 'unreadable intent file'}
        if isinstance(record, dict):
            out.append(record | {'id': intent_id})
    return out


# ------------------------------------------------------------------------------------------------------------ transaction

_FAULTS: dict = {}        # tests only: {'replace': fn(path) called before every file after the first, 'rollback': fn() before the rollback}

def _rel(root: Path, path: Path) -> str:
    return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()


def sha256_or_none(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


SET_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')          # (serve.NAME)
SET_FILES = re.compile(r'^models/([A-Za-z0-9][A-Za-z0-9_-]*)/(pipeline_v2\.json|ready\.json(\.(revoked|quarantine)-[^/]*)?)$')


def intent_set(rec: dict) -> str | None:
    """The set an intent belongs to: its `set` field, else (an intent written before intents named their set, phase 3 re-check R12) the set whose
    decision file / manifest (`models/<set>/pipeline_v2.json`, `models/<set>/ready.json...`) it recorded. None when neither says."""
    named = rec.get('set')
    if isinstance(named, str) and SET_NAME.match(named):
        return named
    for entry in rec.get('files') or []:
        match = SET_FILES.match(str(entry.get('path') or '')) if isinstance(entry, dict) else None
        if match:
            return match.group(1)
    return None


def set_intents(root: Path, set_name: str) -> list[dict]:
    """Unresolved intents (not running in this process right now) that belong to `set_name` (`intent_set`: also pre-fix intents without `set`)."""
    return [rec for rec in list_intents(root) if intent_set(rec) == set_name]


def check_recovering(root: Path, set_name: str) -> None:
    """Refuse (409 `recovering`) a write of a set with an unresolved interrupted change."""
    pending = set_intents(root, set_name)
    if pending:
        state = 'needs a manual choice in Trash > Recovery' if any(r.get('conflict') for r in pending) else 'is being recovered'
        raise Recovering(f'{set_name} has an interrupted change ({pending[0].get("label")}) that {state}; try again when it is resolved')


def recovery_plan(root: Path, files: list[dict]) -> tuple[list[dict], list[str]]:
    """(entries to restore, conflicting paths) of an interrupted transaction. A file is restored when it still holds what the transaction
    wrote (`after_sha256`), skipped when it already holds its old content, and a CONFLICT otherwise (a later write changed it). An entry of an
    old intent without `after_sha256` can only be skipped (old content) or is a conflict."""
    restore, conflicts = [], []
    for entry in files:
        path = Path(root) / entry['path']
        try:
            current = sha256_or_none(read_bytes_or_none(path))
        except OSError as exc:
            conflicts.append(f'{entry["path"]} ({exc})')
            continue
        before = entry.get('before_b64')
        before_sha = entry.get('before_sha256', sha256_or_none(None if before is None else base64.b64decode(before)))
        if current == before_sha:
            continue
        if 'after_sha256' in entry and current == entry['after_sha256']:
            restore.append(entry)
        else:
            conflicts.append(entry['path'])
    return restore, conflicts


def recover_files(root: Path, rec: dict, force: bool = False) -> str:
    """Roll an interrupted transaction back: 'restored' (every file is back to its old content), or 'conflict' (nothing was changed: some file
    holds content neither the transaction nor its backup has). `force` (a manual choice) restores every backup anyway. Call it under the
    set's lock."""
    files = rec.get('files') or []
    if force:
        restore_files(root, files)
        return 'restored'
    restore, conflicts = recovery_plan(root, files)
    if conflicts:
        return 'conflict'
    restore_files(root, restore)
    return 'restored'


def restore_files(root: Path, files: list[dict]) -> None:
    """Put every {"path", "before_b64"} back (None = the file did not exist)."""
    errors = []
    for entry in files:
        path = Path(root) / entry['path']
        try:
            before = entry.get('before_b64')
            if before is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                write_atomic(path, base64.b64decode(before))
        except OSError as exc:
            errors.append(f'{entry["path"]}: {exc}')
    if errors:
        raise OSError('; '.join(errors))


def apply_transaction(root: Path, label: str, plan: list[tuple[Path, bytes | None]], set_name: str | None = None) -> None:
    """Replace several files all-or-nothing. `plan` = [(path, new bytes | None to delete)]; `set_name` = the set the files belong to (its writes
    are refused while the intent is unresolved).

    1. back the old contents up (memory + an intent file with the old and new sha256 of every file, so a crash can still be rolled back);
    2. write every new content to a unique temp file next to its target;
    3. replace / delete one by one; on any error restore the already replaced ones from the backups.
    The intent is removed when everything is in place or fully rolled back; if the rollback itself fails it stays for recovery."""
    root = Path(root)
    backups = []
    for path, new in plan:
        old = read_bytes_or_none(path)
        backups.append({'path': _rel(root, path), 'before_b64': None if old is None else base64.b64encode(old).decode('ascii'),
                        'before_sha256': sha256_or_none(old), 'after_sha256': sha256_or_none(new)})
    intent = new_intent(root, {'kind': 'files_tx', 'label': label, 'files': backups} | ({'set': set_name} if set_name else {}))
    temps: list[Path | None] = []
    try:
        try:
            for path, new in plan:
                if new is None:
                    temps.append(None)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = tmp_name(path)
                temps.append(tmp)
                tmp.write_bytes(new)
            for index, ((path, new), tmp) in enumerate(zip(plan, temps)):
                if index and _FAULTS.get('replace'):
                    _FAULTS['replace'](path)
                if tmp is None:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    replace_retry(tmp, path)
        except BaseException:
            try:
                if _FAULTS.get('rollback'):
                    _FAULTS['rollback']()
                restore_files(root, backups)
            except OSError:
                abandon_intent(intent)           # the intent file stays: Recovery / the orchestrator finishes the rollback
                raise
            finish_intent(root, intent)
            raise
        finish_intent(root, intent)
    finally:
        for tmp in temps:
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass
