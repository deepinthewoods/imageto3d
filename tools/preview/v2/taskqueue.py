"""The task queue of the v2 preview app: SQLite (WAL) at `.codex_jobs/_app/queue.sqlite3` (DESIGN_ORCHESTRATOR.md section 2,
DESIGN_PHASE3.md sections A and E).

Named taskqueue.py, not queue.py: the v2 folder is first on sys.path and a file called queue.py would shadow the stdlib `queue`.

Thread-safety: ONE module-level lock around every statement; a connection is opened per call (cheap for SQLite, safe across threads
and processes). Only the orchestrator thread starts and finishes tasks (`claim`, `mark_running`, `finish`, `requeue`); HTTP threads
only `enqueue`, `retry`, `cancel` or `request_kill`. A read-only server (no writer lock) may read an existing database but never creates one.

Task states: queued -> starting -> running -> done | failed; queued -> cancelled; running -> orphan (the wrapper died while its builder still runs:
the worker slot stays taken until the builder is gone too, phase 3 re-check R6). A failed or cancelled task can be queued again.
Columns of the re-check: `approval_gen` (the 3D approval generation the row was queued for, R11) and `cancel_gen` (set by a MANUAL cancel: the
generation it was cancelled for; reconcile never resurrects such a row for that generation, R8; Retry / a new approval clears it).
Every start is an ATTEMPT with its own uuid `token` (phase 3 review F5): `claim` commits it (queued -> starting, only from queued),
the detached wrapper claims the token in its claim file, and `mark_running` / `finish` / `requeue` only act on the row while it still
carries that token, so a late receipt of an older attempt, a cancel or a second dispatcher can never finish or restart it twice.

The queue is a DERIVED cache of the decisions (review F9): a damaged database never breaks a reader. Reads return empty results and
record `health` (`{"ok", "error", "corrupt", "since"}`); writes raise QueueError. The orchestrator then `repair()`s it: the damaged file is
kept as `queue.sqlite3.corrupt-<time>`, a fresh database is created and the work is reconstructed from the surviving wrappers' claim files
and the committed approvals.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import threading
import time

APP = Path('.codex_jobs') / '_app'
DB_NAME = 'queue.sqlite3'
LANES = ('bare', 'layers')
STATES = ('queued', 'starting', 'running', 'orphan', 'done', 'failed', 'cancelled')
ACTIVE = ('starting', 'running', 'orphan')         # rows that hold a worker slot
OPEN = ('queued',) + ACTIVE
LOCK = threading.RLock()
SCHEMA = '''
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY, lane TEXT NOT NULL, key TEXT UNIQUE NOT NULL, set_name TEXT, unit TEXT, args TEXT,
  state TEXT NOT NULL, priority INTEGER, attempts INTEGER DEFAULT 0, max_attempts INTEGER DEFAULT 3,
  pid INTEGER, pid_create REAL, started REAL, finished REAL, log TEXT, result TEXT, error TEXT,
  created REAL, updated REAL, not_before REAL DEFAULT 0);
CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state, lane, priority, id);
CREATE INDEX IF NOT EXISTS tasks_set ON tasks(set_name);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, time REAL, task_id INTEGER, kind TEXT, detail TEXT);
'''
# columns added after the first phase 3 schema (added to an existing database by ALTER TABLE)
EXTRA_COLUMNS = (('token', 'TEXT'), ('builder_pid', 'INTEGER'), ('builder_create', 'REAL'), ('kill_requested', 'INTEGER DEFAULT 0'),
                 ('approval_gen', 'TEXT'), ('cancel_gen', 'TEXT'))
JSON_FIELDS = ('args', 'result')
CORRUPT_TEXT = ('not a database', 'malformed', 'file is encrypted', 'disk image')


class QueueError(RuntimeError):
    """The queue database cannot be written right now (damaged or busy); the decisions stay valid, the orchestrator reconstructs the work."""


def db_path(root: Path) -> Path:
    return Path(root) / APP / DB_NAME


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    task = dict(row)
    for field in JSON_FIELDS:
        raw = task.get(field)
        try:
            task[field] = json.loads(raw) if raw else None
        except ValueError:
            task[field] = None
    for column, _kind in EXTRA_COLUMNS:
        task.setdefault(column, None)
    return task


def _corrupt(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, sqlite3.DatabaseError) and any(t in text for t in CORRUPT_TEXT)


class TaskQueue:
    """The queue of one project root. Cheap to construct; use `for_root(root)`."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = db_path(self.root)
        self._ready = False
        self.health: dict = {'ok': True, 'error': None, 'corrupt': False, 'since': None}

    # ------------------------------------------------------------------------------------------------------ plumbing
    def _connect(self, create: bool = True) -> sqlite3.Connection | None:
        if not create and not self.path.exists():
            return None
        fresh = not self.path.exists()
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=20, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA busy_timeout=20000')
            if create and (fresh or not self._ready):
                conn.execute('PRAGMA journal_mode=WAL')
                conn.executescript(SCHEMA)
                have = {r[1] for r in conn.execute('PRAGMA table_info(tasks)').fetchall()}
                for column, kind in EXTRA_COLUMNS:
                    if column not in have:
                        conn.execute(f'ALTER TABLE tasks ADD COLUMN {column} {kind}')
                self._ready = True
        except BaseException:
            conn.close()
            raise
        return conn

    def _failed(self, exc: BaseException) -> None:
        """Record a database error in `health` (a damaged file stays `corrupt` until repair())."""
        corrupt = _corrupt(exc)
        if not self.health.get('corrupt') or corrupt:
            self.health = {'ok': False, 'error': f'{type(exc).__name__}: {exc}', 'corrupt': corrupt or bool(self.health.get('corrupt')),
                           'since': self.health.get('since') or time.time()}

    def _good(self) -> None:
        if not self.health['ok'] and not self.health.get('corrupt'):
            self.health = {'ok': True, 'error': None, 'corrupt': False, 'since': None}

    def _run(self, sql: str, params=(), *, write: bool = True, one: bool = False, many: bool = False):
        """One statement under the module lock. Reads of a database that does not exist yet give None / []; reads of a damaged one too (the
        error goes to `health`); a failed write raises QueueError."""
        with LOCK:
            try:
                conn = self._connect(create=write)
                if conn is None:
                    return [] if many else None
                try:
                    cursor = conn.execute(sql, params)
                    if many:
                        out = [_row(r) for r in cursor.fetchall()]
                    elif one:
                        out = _row(cursor.fetchone())
                    else:
                        out = cursor.rowcount
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._failed(exc)
                if write:
                    raise QueueError(f'queue database: {exc}') from exc
                return [] if many else None
            self._good()
            return out

    def _tx(self, work):
        """`work(conn)` inside one BEGIN IMMEDIATE transaction under the module lock (rolled back on error). sqlite errors -> QueueError."""
        with LOCK:
            try:
                conn = self._connect()
                try:
                    conn.execute('BEGIN IMMEDIATE')
                    try:
                        out = work(conn)
                    except BaseException:
                        conn.execute('ROLLBACK')
                        raise
                    conn.execute('COMMIT')
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._failed(exc)
                raise QueueError(f'queue database: {exc}') from exc
            self._good()
            return out

    @staticmethod
    def _event(conn: sqlite3.Connection, task_id: int | None, kind: str, detail: str = '') -> None:
        conn.execute('INSERT INTO events(time, task_id, kind, detail) VALUES (?,?,?,?)', (time.time(), task_id, kind, str(detail)[:500]))

    def event(self, kind: str, detail: str = '', task_id: int | None = None) -> None:
        """A free event (e.g. `revoke` of a ready.json); best effort."""
        try:
            self._tx(lambda conn: self._event(conn, task_id, kind, detail))
        except QueueError:
            pass

    # --------------------------------------------------------------------------------------------------------- health
    def health_view(self) -> dict:
        return dict(self.health)

    def check(self) -> dict:
        """Probe the database (integrity of the header + both tables); updates and returns `health`."""
        with LOCK:
            if not self.path.exists():
                self._good()
                return self.health_view()
            try:
                conn = self._connect(create=False)
                try:
                    conn.execute('SELECT COUNT(*) FROM tasks').fetchone()
                    conn.execute('SELECT COUNT(*) FROM events').fetchone()
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._failed(exc)
                return self.health_view()
            self._good()
            return self.health_view()

    def repair(self) -> Path | None:
        """Keep the damaged database as `<name>.corrupt-<time>` (with its -wal / -shm files) and start an empty one. Orchestrator only.
        Returns the kept path (None when there was no file)."""
        with LOCK:
            stamp = time.strftime('%Y%m%dT%H%M%S')
            kept = None
            for suffix in ('', '-wal', '-shm'):
                src = self.path.with_name(self.path.name + suffix)
                if src.exists():
                    dst = self.path.with_name(f'{self.path.name}{suffix}.corrupt-{stamp}')
                    n = 2
                    while dst.exists():
                        dst = self.path.with_name(f'{self.path.name}{suffix}.corrupt-{stamp}-{n}')
                        n += 1
                    for attempt in range(10):
                        try:
                            os.replace(src, dst)
                            break
                        except PermissionError:
                            if attempt == 9:
                                raise
                            time.sleep(0.1 * (attempt + 1))
                    kept = kept or dst
            self._ready = False
            self.health = {'ok': True, 'error': None, 'corrupt': False, 'since': None, 'repaired': str(kept) if kept else None, 'repaired_at': time.time()}
            return kept

    # --------------------------------------------------------------------------------------------------------- writes
    def enqueue(self, lane: str, key: str, set_name: str | None, unit: str | None, args: dict | None = None, priority: int = 5,
                max_attempts: int = 3, retry: bool = False, approval_gen: str | None = None) -> int:
        """Queue a task and return its id. IDEMPOTENT per `key`: a queued, starting, running or done task with that key is returned unchanged;
        a failed one too unless `retry=True`; a cancelled one is queued again (its approval came back). `approval_gen` is written into a new or
        re-queued row (and into a still-queued one)."""
        if lane not in LANES:
            raise ValueError(f'unknown lane {lane!r}')
        now = time.time()

        def work(conn):
            row = conn.execute('SELECT id, state FROM tasks WHERE key=?', (key,)).fetchone()
            if row is None:
                cursor = conn.execute(
                    'INSERT INTO tasks(lane,key,set_name,unit,args,state,priority,attempts,max_attempts,created,updated,not_before,approval_gen) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?)',
                    (lane, key, set_name, unit, json.dumps(args or {}), 'queued', priority, 0, max_attempts, now, now, approval_gen))
                self._event(conn, cursor.lastrowid, 'enqueue', key)
                return cursor.lastrowid
            if row['state'] == 'cancelled' or (row['state'] == 'failed' and retry):
                self._requeue_sql(conn, row['id'], now, reset=True)
                self._event(conn, row['id'], 'requeue', f'{row["state"]} -> queued')
            if approval_gen is not None and (row['state'] in ('cancelled', 'queued') or (row['state'] == 'failed' and retry)):
                conn.execute('UPDATE tasks SET approval_gen=? WHERE id=?', (approval_gen, row['id']))
            return row['id']
        return self._tx(work)

    @staticmethod
    def _requeue_sql(conn: sqlite3.Connection, task_id: int, now: float, reset: bool, not_before: float = 0.0, error: str | None = None) -> None:
        conn.execute('UPDATE tasks SET state=?, pid=NULL, pid_create=NULL, builder_pid=NULL, builder_create=NULL, token=NULL, kill_requested=0, cancel_gen=NULL, '
                     'started=NULL, finished=NULL, result=NULL, not_before=?, updated=?, '
                     'attempts=CASE WHEN ? THEN 0 ELSE attempts END, error=CASE WHEN ? THEN NULL ELSE ? END WHERE id=?',
                     ('queued', not_before, now, int(reset), int(reset), error, task_id))

    def claim(self, task_id: int, token: str, log: str | None) -> bool:
        """Orchestrator only: commit a new attempt BEFORE anything is spawned: queued -> starting, attempts + 1, the attempt's token and log.
        Only a row that is still queued is claimed (a cancel or a second dispatcher wins otherwise): False then."""
        now = time.time()

        def work(conn):
            cursor = conn.execute('UPDATE tasks SET state=?, token=?, pid=NULL, pid_create=NULL, builder_pid=NULL, builder_create=NULL, kill_requested=0, '
                                  'started=?, finished=NULL, log=?, attempts=attempts+1, updated=?, error=NULL, result=NULL WHERE id=? AND state=?',
                                  ('starting', token, now, log, now, task_id, 'queued'))
            if cursor.rowcount != 1:
                return False
            self._event(conn, task_id, 'claim', token)
            return True
        return self._tx(work)

    def mark_running(self, task_id: int, token: str | None, pid: int | None, create_time: float | None) -> bool:
        """Orchestrator only: starting -> running with the wrapper's process (or a new process identity of a running attempt), only for
        the attempt that still carries `token`."""
        now = time.time()

        def work(conn):
            cursor = conn.execute('UPDATE tasks SET state=?, pid=?, pid_create=?, updated=? WHERE id=? AND token IS ? AND state IN (?,?)',
                                  ('running', pid, create_time, now, task_id, token, 'starting', 'running'))
            if cursor.rowcount != 1:
                return False
            self._event(conn, task_id, 'start', f'pid {pid}')
            return True
        return self._tx(work)

    def mark_orphan(self, task_id: int, token: str | None) -> bool:
        """Orchestrator only: the attempt's wrapper is gone but its builder still runs (re-check R6): running -> orphan; the slot stays taken."""
        now = time.time()

        def work(conn):
            cursor = conn.execute('UPDATE tasks SET state=?, updated=? WHERE id=? AND token IS ? AND state IN (?,?)',
                                  ('orphan', now, task_id, token, 'starting', 'running'))
            if cursor.rowcount == 1:
                self._event(conn, task_id, 'orphan', 'the wrapper died; its builder still runs')
            return cursor.rowcount == 1
        return self._tx(work)

    def kill_failed(self, task_id: int, token: str | None, error: str) -> bool:
        """Orchestrator only: Kill & retry could not verify / stop a process of the attempt: the row keeps its state AND its slot, the request is
        cleared (the user may try again) and the reason is shown (re-check R6)."""
        now = time.time()
        placeholders = ','.join('?' * len(ACTIVE))

        def work(conn):
            cursor = conn.execute(f'UPDATE tasks SET kill_requested=0, error=?, updated=? WHERE id=? AND token IS ? AND state IN ({placeholders})',
                                  (error, now, task_id, token, *ACTIVE))
            if cursor.rowcount == 1:
                self._event(conn, task_id, 'kill_failed', error)
            return cursor.rowcount == 1
        return self._tx(work)

    def set_builder(self, task_id: int, token: str | None, pid: int | None, create_time: float | None) -> None:
        self._run('UPDATE tasks SET builder_pid=?, builder_create=?, updated=? WHERE id=? AND token IS ?', (pid, create_time, time.time(), task_id, token))

    def finish(self, task_id: int, token: str | None, ok: bool, result: dict | None = None, error: str | None = None) -> bool:
        """Orchestrator only: starting / running -> done | failed, only for the attempt with `token` (a build finishes once)."""
        now = time.time()

        def work(conn):
            cursor = conn.execute('UPDATE tasks SET state=?, finished=?, result=?, error=?, updated=?, kill_requested=0 WHERE id=? AND token IS ? AND state IN (?,?,?)',
                                  ('done' if ok else 'failed', now, json.dumps(result) if result is not None else None, None if ok else (error or 'failed'),
                                   now, task_id, token, *ACTIVE))
            if cursor.rowcount != 1:
                return False
            self._event(conn, task_id, 'done' if ok else 'failed', error or '')
            return True
        return self._tx(work)

    def fail_queued(self, task_id: int, error: str) -> bool:
        """Orchestrator only: a queued task that cannot start at all (its source changed): queued -> failed."""
        now = time.time()

        def work(conn):
            cursor = conn.execute('UPDATE tasks SET state=?, finished=?, error=?, updated=? WHERE id=? AND state=?', ('failed', now, error, now, task_id, 'queued'))
            if cursor.rowcount == 1:
                self._event(conn, task_id, 'failed', error)
            return cursor.rowcount == 1
        return self._tx(work)

    def requeue(self, task_id: int, token: str | None, error: str, delay: float = 0.0, reset: bool = False) -> bool:
        """Orchestrator only: an automatic retry of the attempt with `token` (keeps the attempts count unless `reset`, waits `delay` seconds)."""
        now = time.time()

        def work(conn):
            row = conn.execute('SELECT state, token FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None or row['state'] not in ACTIVE or row['token'] != token:
                return False
            self._requeue_sql(conn, task_id, now, reset=reset, not_before=now + delay, error=error)
            if reset:                                            # a manual retry (Kill & retry): fresh attempts, the reason stays visible
                conn.execute('UPDATE tasks SET error=? WHERE id=?', (error, task_id))
            self._event(conn, task_id, 'retry', error)
            return True
        return self._tx(work)

    def reset_done(self, task_id: int, reason: str, approval_gen: str | None = None) -> bool:
        """A done task whose publication is missing or invalid gets a fresh attempt under the same key (review F11; attempts kept)."""
        now = time.time()

        def work(conn):
            row = conn.execute('SELECT state FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None or row['state'] != 'done':
                return False
            self._requeue_sql(conn, task_id, now, reset=False, error=reason)
            if approval_gen is not None:
                conn.execute('UPDATE tasks SET approval_gen=? WHERE id=?', (approval_gen, task_id))
            self._event(conn, task_id, 'repair', reason)
            return True
        return self._tx(work)

    def retry(self, task_id: int, approval_gen: str | None = None) -> bool:
        """A failed or cancelled task goes back to queued with a fresh attempts count (the Retry button; it clears a manual cancel). False when it
        is in another state. `approval_gen` (when given) re-tags the row for that approval."""
        now = time.time()

        def work(conn):
            row = conn.execute('SELECT state FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                raise ValueError(f'unknown task {task_id}')
            if row['state'] not in ('failed', 'cancelled'):
                return False
            self._requeue_sql(conn, task_id, now, reset=True)
            if approval_gen is not None:
                conn.execute('UPDATE tasks SET approval_gen=? WHERE id=?', (approval_gen, task_id))
            self._event(conn, task_id, 'retry', 'manual')
            return True
        return self._tx(work)

    def cancel(self, task_id: int, detail: str = '', manual_gen: str | None = None) -> bool:
        """A queued task is cancelled; a running one cannot be cancelled (Kill & retry stops it). False when not queued. `manual_gen` (the Queue's
        Cancel button): the approval generation the user cancelled it for, kept in `cancel_gen` so reconcile never puts it back (re-check R8)."""
        now = time.time()

        def work(conn):
            row = conn.execute('SELECT state FROM tasks WHERE id=?', (task_id,)).fetchone()
            if row is None:
                raise ValueError(f'unknown task {task_id}')
            if row['state'] != 'queued':
                return False
            conn.execute('UPDATE tasks SET state=?, finished=?, updated=?, cancel_gen=? WHERE id=? AND state=?',
                         ('cancelled', now, now, manual_gen, task_id, 'queued'))
            self._event(conn, task_id, 'cancel', detail)
            return True
        return self._tx(work)

    def cancel_set(self, set_name: str) -> int:
        """Cancel every QUEUED task of a set (running ones finish and keep their builds). Returns how many."""
        now = time.time()

        def work(conn):
            ids = [r['id'] for r in conn.execute('SELECT id FROM tasks WHERE set_name=? AND state=?', (set_name, 'queued')).fetchall()]
            for task_id in ids:
                conn.execute('UPDATE tasks SET state=?, finished=?, updated=? WHERE id=? AND state=?', ('cancelled', now, now, task_id, 'queued'))
                self._event(conn, task_id, 'cancel', f'set {set_name} unapproved')
            return len(ids)
        return self._tx(work)

    def request_kill(self, task_id: int, token: str) -> bool:
        """Kill & retry (HTTP): flag the starting / running / orphan attempt `token` (the one the client saw, re-check R6); the orchestrator kills
        its builder and wrapper and queues a fresh attempt only once both are confirmed gone."""
        now = time.time()

        def work(conn):
            cursor = conn.execute('UPDATE tasks SET kill_requested=1, updated=? WHERE id=? AND token IS ? AND state IN (?,?,?)',
                                  (now, task_id, token, *ACTIVE))
            if cursor.rowcount == 1:
                self._event(conn, task_id, 'kill_requested', '')
            return cursor.rowcount == 1
        return self._tx(work)

    def insert_running(self, lane: str, key: str, set_name: str | None, unit: str | None, args: dict, attempt: int, token: str, log: str,
                       pid: int | None, create_time: float | None, started: float | None, max_attempts: int = 3,
                       approval_gen: str | None = None) -> int | None:
        """Reconstruction after a lost / repaired database: a surviving wrapper (its claim file) becomes a running row again. None when the key
        already exists."""
        now = time.time()

        def work(conn):
            if conn.execute('SELECT id FROM tasks WHERE key=?', (key,)).fetchone() is not None:
                return None
            cursor = conn.execute('INSERT INTO tasks(lane,key,set_name,unit,args,state,priority,attempts,max_attempts,pid,pid_create,started,log,'
                                  'created,updated,not_before,token,approval_gen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)',
                                  (lane, key, set_name, unit, json.dumps(args or {}), 'running', 5, attempt, max_attempts, pid, create_time,
                                   started or now, log, now, now, token, approval_gen))
            self._event(conn, cursor.lastrowid, 'adopt_claim', token)
            return cursor.lastrowid
        return self._tx(work)

    def adopt_attempt(self, lane: str, key: str, set_name: str | None, unit: str | None, args: dict, attempt: int, token: str, log: str | None,
                      pid: int | None, create_time: float | None, started: float | None, max_attempts: int = 3,
                      approval_gen: str | None = None) -> tuple[str, int | None]:
        """Merge a SURVIVING attempt (its claim / token process is alive) into the database by attempt identity (phase 4 review F12), whatever
        else the database holds: ('inserted', id) a new running row; ('known', id) the row already carries this token; ('merged', id) the row
        of the key was queued / failed / cancelled / done and now IS this running attempt (it is never started a second time); ('conflict', id)
        the row of the key runs ANOTHER live attempt (left alone)."""
        if lane not in LANES:
            raise ValueError(f'unknown lane {lane!r}')
        now = time.time()

        def work(conn):
            row = conn.execute('SELECT id, state, token FROM tasks WHERE key=?', (key,)).fetchone()
            if row is None:
                cursor = conn.execute('INSERT INTO tasks(lane,key,set_name,unit,args,state,priority,attempts,max_attempts,pid,pid_create,started,log,'
                                      'created,updated,not_before,token,approval_gen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)',
                                      (lane, key, set_name, unit, json.dumps(args or {}), 'running', 5, attempt, max_attempts, pid, create_time,
                                       started or now, log, now, now, token, approval_gen))
                self._event(conn, cursor.lastrowid, 'adopt_claim', token)
                return 'inserted', cursor.lastrowid
            if row['token'] == token:
                return 'known', row['id']
            if row['state'] in ACTIVE:
                return 'conflict', row['id']
            conn.execute('UPDATE tasks SET state=?, token=?, pid=?, pid_create=?, builder_pid=NULL, builder_create=NULL, kill_requested=0, '
                         'cancel_gen=NULL, started=?, finished=NULL, log=?, attempts=?, updated=?, error=NULL, result=NULL, not_before=0, '
                         'approval_gen=COALESCE(?, approval_gen) WHERE id=?',
                         ('running', token, pid, create_time, started or now, log, attempt, now, approval_gen, row['id']))
            self._event(conn, row['id'], 'adopt_claim', f'{token} (merged into the {row["state"]} row)')
            return 'merged', row['id']
        return self._tx(work)

    # ---------------------------------------------------------------------------------------------------------- reads
    def get(self, task_id: int) -> dict | None:
        return self._run('SELECT * FROM tasks WHERE id=?', (task_id,), write=False, one=True)

    def by_key(self, key: str) -> dict | None:
        return self._run('SELECT * FROM tasks WHERE key=?', (key,), write=False, one=True)

    def next_queued(self, lane: str | tuple | list | None, n: int = 20, now: float | None = None) -> list[dict]:
        """Queued tasks of a lane (or lanes) that are past their backoff, best first (priority, then oldest)."""
        lanes = [lane] if isinstance(lane, str) else list(lane or LANES)
        marks = ','.join('?' * len(lanes))
        return self._run(f'SELECT * FROM tasks WHERE state=? AND lane IN ({marks}) AND not_before<=? ORDER BY priority, id LIMIT ?',
                         ('queued', *lanes, now if now is not None else time.time(), n), write=False, many=True)

    def tasks(self, state: str | tuple | list | None = None, lane: str | None = None, set_name: str | None = None, unit: str | None = None,
              since: float | None = None, limit: int = 500) -> list[dict]:
        """Filtered tasks, newest first. `since`: only tasks updated after that time (the 24 h window of finished ones)."""
        where, params = self._where(state, lane, set_name, unit, since)
        sql = 'SELECT * FROM tasks' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY id DESC LIMIT ?'
        return self._run(sql, (*params, limit), write=False, many=True)

    def count(self, state: str | tuple | list | None = None, since: float | None = None) -> int:
        where, params = self._where(state, None, None, None, since)
        row = self._run('SELECT COUNT(*) AS n FROM tasks' + (' WHERE ' + ' AND '.join(where) if where else ''), params, write=False, one=True)
        return int(row['n']) if row else 0

    def totals(self) -> dict:
        """{state: count} over the whole table (independent of any page)."""
        with LOCK:
            try:
                conn = self._connect(create=False)
                if conn is None:
                    return {s: 0 for s in STATES}
                try:
                    rows = conn.execute('SELECT state, COUNT(*) FROM tasks GROUP BY state').fetchall()
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                self._failed(exc)
                return {s: 0 for s in STATES}
        out = {s: 0 for s in STATES}
        out.update({r[0]: int(r[1]) for r in rows})
        return out

    @staticmethod
    def _where(state, lane, set_name, unit, since) -> tuple[list[str], list]:
        where, params = [], []
        if state is not None:
            states = [state] if isinstance(state, str) else list(state)
            where.append(f'state IN ({",".join("?" * len(states))})')
            params += states
        for column, value in (('lane', lane), ('set_name', set_name), ('unit', unit)):
            if value is not None:
                where.append(f'{column}=?')
                params.append(value)
        if since is not None:
            where.append('updated>=?')
            params.append(since)
        return where, params

    def open_tasks(self) -> list[dict]:
        """Every queued, starting or running task (the orchestrator's working set)."""
        return self._run('SELECT * FROM tasks WHERE state IN (?,?,?,?) ORDER BY priority, id', OPEN, write=False, many=True)

    def events(self, after_id: int = 0, limit: int = 500) -> list[dict]:
        return self._run('SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?', (after_id, limit), write=False, many=True)


_QUEUES: dict[str, TaskQueue] = {}


def for_root(root: Path) -> TaskQueue:
    """The (per-process cached) queue of a root."""
    key = str(Path(root))
    with LOCK:
        return _QUEUES.setdefault(key, TaskQueue(Path(root)))
