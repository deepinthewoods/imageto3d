#!/usr/bin/env python3
"""Convert staged jobs one after another: Tripo while its credits last, then Hyper3D.

    python3 tools/convert_queue.py --sets set_a,set_b,... | --sets-file f [--reserve 0] [--poll 300] [--worker N] [--no-tripo]
        [--backend tripo|pixal3d] [--fallback hyper3d|pixal3d]

Only jobs whose set id is listed are touched (older jobs in multiview_ready/ or processing/ are left alone). Each
loop takes the next listed job from processing/ (not yet `converted*`) or multiview_ready/, reads the Tripo credit
balance (`studio.mjs balance`), runs `convert_job.py <job> --max-submits K` with K = (balance - reserve) // 25, and,
when that leaves modules unsubmitted (exit 3) or K is 0, `convert_job.py <job> --backend hyper3d --allow-paid
--accept-preview` for the rest (the user chose Hyper3D once Tripo credits run out). A job that fails otherwise is
reported and skipped until nothing else is staged, then retried (at most MAX_TRIES runs per job). Waits `--poll`
seconds for new jobs and exits when nothing is staged and the view batch runner (tools/view_batch.py run) is gone.

Parallel workers: start several queues with distinct `--worker N`. Each claims a job through
`.codex_jobs/_batch/convert_locks/<job>.lock` (holder pid; a dead holder's lock is taken over) and shares failure
counts there (`<job>.fails`). Worker N > 1 drives Hyper3D through its own clone of the signed-in Firefox profile,
`~/snap/firefox/common/hyper3d-workflow-profile-wN` (HYPER3D_PROFILE), because one profile cannot be open twice.
`--no-tripo` skips the Tripo balance check (only one browser may use the Tripo profile at a time).

After a job converts (and once before an idle exit) finish_sets.py --apply moves every set that is converted for all
races from processing/ to completed/ (flock-serialized, errors only logged).

While .codex_jobs/_batch/PAUSE exists with "conversions" in its scope (tools/pipeline_supervisor.py; an unreadable PAUSE counts)
or .codex_jobs/_batch/CONVERT_HOLD exists (the preview app's service manager keeps it while the supervisor is stopped, restarting
or has not completed a tick yet; DESIGN_PHASE4 I.1) no job is started (PAUSED, once per reason). convert_job.py itself waits
before every paid step too (a Hyper3D collect of an existing submission included).

Tripo balance (review_v2_phase4 finding 4): `studio.mjs balance` runs under the Tripo browser lock
(.codex_jobs/_batch/browser_tripo.lock, tripo_browser_lock.py) and counts only when it exits 0 with a numeric `credits`. An
unreadable balance (nonzero exit, null credits, parse error, timeout, busy browser) never switches to the fallback backend and
never marks Tripo as used up: the job is held (`HOLD <job>: Tripo balance unreadable ...`, not counted as a failure), its lock is
released, and the balance is read again before the next submission. The last good reading is kept in memory and named in the
message.

Partial jobs (phase 5 review finding 3): when convert_job stops after `--max-submits` (exit 3, modules left unsubmitted) the
balance is read again. Unreadable: the job is held and retried (never the fallback backend, Tripo stays on). Still enough for a
module (balance - reserve >= 25): the job continues on Tripo at the next loop. Only a VERIFIED shortfall (a numeric balance below
the per-module cost, also when the first reading already was) or the explicit policy (--no-tripo) sends the rest to the fallback
backend (--tripo-only: STOP instead).

`job_eligible(root, job_name) -> (bool, reason)` is the read-only form of the worker's claim/join rules (fewer than MAX_TRIES fails,
at least one free module, the job lock free or joinable) for the preview app's convert list (phase-4 finding 20).

Graceful stop (DESIGN_PHASE4 I.2): the preview app writes .codex_jobs/_batch/convert_worker<N>.stop = {pid, create_time,
request, time} naming ONE worker process. Worker N honours only a file naming its own pid and create_time (worker 2 never
touches worker 1's file): between jobs it writes convert_worker<N>.stopping = {pid, time}, emits `STOP requested; exiting`,
runs finish() (which offloads the sets it completes), then removes both files and exits; a conversion in flight completes
first. A stop file naming a dead process is left to the app, except that a worker removes its own number's stale files at start.
Legacy: worker 1 still honours the bare .codex_jobs/_batch/convert_worker.stop of the first phase-4 app (removed at start when
left from before; deleted when honoured).

--backend pixal3d (user, 2026-11-18): convert every job with the free local Pixal3D backend only (no Tripo balance check, no
Hyper3D); one generation at a time per machine (the backend's GPU lock), so one pixal3d worker is enough. --fallback pixal3d: use
it instead of Hyper3D once the Tripo credits run out (--tripo-only still stops instead).

Drain (DESIGN_PHASE4 I.10; pre-cut-over review finding 1): while .codex_jobs/_batch/CONVERT_DRAIN exists no job is started
(PAUSED, like CONVERT_HOLD). A convert_job.py that was blocked at its paid gate when the drain began leaves with exit 76
(convert_job.DRAIN_EXIT: module lock released, module `deferred` in the job record, nothing paid in flight); that is reported as
`DEFERRED <job>` and is never counted in `<job>.fails`. The children get $IMAGETO3D_DRAIN_AWARE so the manager knows this worker
does not count the exit (it restores the count an older worker wrote).

Recorded owner (pre-cut-over review finding 2): a set id on the list authorizes a job only when the job's recorded owner agrees
(owner_allows): a job whose record names `parent_set` X is taken only when X is on the list and, where the preview app recorded
whose id the job's prefix is (.codex_jobs/_app/set_namespace.json), that owner is X; a job without `parent_set` whose prefix was
written as a family id of set S needs S on the list. A refused job is reported once (`OWNER <job>: ...`) and left alone.

Events (stdout and .codex_jobs/_batch/convert_events.log): CONVERT, CONVERTED, PARTIAL, FAILED, DEFERRED, OWNER, BALANCE, IDLE, FINISH,
PAUSED.
"""
import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVENTS = ROOT / '.codex_jobs' / '_batch' / 'convert_events.log'
TRIPO_COST = 25
MAX_TRIES = 3
LOGS = ROOT / '.codex_jobs' / '_batch' / 'convert_logs'
LOCKS = ROOT / '.codex_jobs' / '_batch' / 'convert_locks'
PAUSE = ROOT / '.codex_jobs' / '_batch' / 'PAUSE'   # written by tools/pipeline_supervisor.py while the screen is on
STOP_FILE = ROOT / '.codex_jobs' / '_batch' / 'convert_worker.stop'   # legacy graceful stop (worker 1 only; see the docstring)
STOP_NAP = 5   # seconds: the longest a waiting worker sleeps before it looks at the stop file again
BALANCE_RETRY = 300   # seconds a job held for an unreadable Tripo balance waits before the balance is read again
BALANCE_LOCK_WAIT = 600   # seconds the balance check waits for the Tripo browser lock (a v2 balance check, a running studio call)
WORKER = 1   # this process's --worker number (set in main)
sys.path.insert(0, str(ROOT / 'tools' / 'tripo_pipeline'))
from convert_job import DRAIN_AWARE_ENV, DRAIN_EXIT, claim_module, lock_holder, module_converted, read_record  # noqa: E402
import portable  # noqa: E402  (also puts PYTHONUTF8=1 into the environment of the convert_job children on Windows)
from tripo_browser_lock import TripoBrowserBusy, tripo_browser_lock  # noqa: E402
import finish_sets  # noqa: E402
import offload_models  # noqa: E402
PROFILES = Path.home() / 'snap/firefox/common'
TAG = ''
MIN_FREE = 10 * 2**30  # don't start a conversion with less free disk (a module writes ~0.35 GB; the disk filled on 2026-10-26)


def emit(line):
    line = f'{datetime.now():%m-%d %H:%M:%S} {TAG}{line}'
    print(line, flush=True)
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS.open('a', encoding='utf-8') as handle:
        handle.write(line + '\n')


def nap(seconds):
    """time.sleep in slices so a stop request is noticed within STOP_NAP seconds."""
    end = time.time() + seconds
    while time.time() < end and not stop_request(WORKER):
        time.sleep(min(STOP_NAP, max(0.0, end - time.time())))


# ------------------------------------------------------------------ gate (PAUSE / CONVERT_HOLD) and graceful stop

def batch_file(name):
    return ROOT / '.codex_jobs' / '_batch' / name


def read_json(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def conversions_blocked():
    """Why no job may start now, else None: CONVERT_HOLD exists, or PAUSE exists and its scope covers conversions (a PAUSE
    without a readable scope counts)."""
    hold = batch_file('CONVERT_HOLD')
    if hold.exists():
        return f'CONVERT_HOLD: {(read_json(hold) or {}).get("reason") or "conversion hold"}'
    drain = batch_file('CONVERT_DRAIN')
    if drain.exists():   # DESIGN_PHASE4 I.10: no intake while an older worker is drained (the file counts, readable or not)
        return f'CONVERT_DRAIN: {(read_json(drain) or {}).get("reason") or "conversion drain"}'
    if PAUSE.exists():
        scope = (read_json(PAUSE) or {}).get('scope')
        if not isinstance(scope, list) or 'conversions' in scope:
            return 'PAUSE file'
    return None


_IDENTITY = []


def own_identity():
    """(pid, create_time or None) of this process (create_time from psutil when it is installed)."""
    if not _IDENTITY:
        try:
            import psutil
            created = psutil.Process(os.getpid()).create_time()
        except Exception:   # noqa: BLE001  no psutil: the pid alone identifies the worker
            created = None
        _IDENTITY.append((os.getpid(), created))
    return _IDENTITY[0]


def names_me(data):
    """True when a stop request names this process (pid and, when both sides know it, create_time within 1 s)."""
    pid, created = own_identity()
    try:
        if int(data.get('pid')) != pid:
            return False
        if created is not None and data.get('create_time') is not None:
            return abs(float(data['create_time']) - created) < 1
        return True
    except (TypeError, ValueError):
        return False


def worker_files(worker):
    return batch_file(f'convert_worker{worker}.stop'), batch_file(f'convert_worker{worker}.stopping')


def stop_request(worker):
    """The stop file addressed to this process, else None: convert_worker<N>.stop naming our pid/create_time; for worker 1
    also the legacy bare convert_worker.stop."""
    stop, _ = worker_files(worker)
    data = read_json(stop) if stop.exists() else None
    if data and names_me(data):
        return stop
    if worker == 1 and STOP_FILE.exists():
        return STOP_FILE
    return None


def clear_stale_stop(worker):
    """At start: remove this worker number's .stop / .stopping files that name a dead process (a request for an earlier
    worker that never saw it), and the legacy bare file for worker 1."""
    for path in worker_files(worker):
        if not path.exists():
            continue
        data = read_json(path) or {}
        try:
            pid = int(data.get('pid') or 0)
        except (TypeError, ValueError):
            pid = 0
        if not portable.pid_alive(pid) or (pid == os.getpid() and not names_me(data)):
            path.unlink(missing_ok=True)
    if worker == 1:
        STOP_FILE.unlink(missing_ok=True)   # a stop requested before this start is stale


def honour_stop(worker, path):
    """Acknowledge (convert_worker<N>.stopping), finish and offload like the idle exit, then remove both files."""
    _, stopping = worker_files(worker)
    if path != STOP_FILE:
        stopping.write_text(json.dumps({'pid': os.getpid(), 'time': datetime.now().isoformat(timespec='seconds')}),
                            encoding='utf-8')
    else:
        path.unlink(missing_ok=True)
    emit('STOP requested; exiting')
    finish()
    if path != STOP_FILE:
        path.unlink(missing_ok=True)
        stopping.unlink(missing_ok=True)


# ------------------------------------------------------------------ Tripo balance

def read_tripo_balance(wait=None):
    """(credits, None) from `studio.mjs balance` under the Tripo browser lock, or (None, why) when it cannot be read: the
    browser is busy past `wait` (default BALANCE_LOCK_WAIT) seconds, studio.mjs times out or exits non-zero, or its last line
    is not JSON with a numeric, non-negative `credits`. Callers must treat (None, why) as unknown, never as zero."""
    wait = BALANCE_LOCK_WAIT if wait is None else wait
    try:
        with tripo_browser_lock(ROOT, what=f'convert_queue balance (worker {WORKER})', wait=wait,
                                announce=lambda text: emit(f'BALANCE: {text}')) as reservation:
            run = subprocess.run(['node', str(ROOT / 'tools/tripo_pipeline/studio.mjs'), 'balance'],
                                 capture_output=True, text=True, encoding='utf-8', errors='replace', cwd=ROOT, timeout=300,
                                 env={**os.environ, **(reservation or {})})
    except TripoBrowserBusy as error:
        return None, str(error)
    except subprocess.TimeoutExpired:
        return None, 'studio.mjs balance timed out after 300 s'
    except OSError as error:
        return None, f'studio.mjs balance could not run: {error}'
    lines = (run.stdout or '').strip().splitlines()
    if run.returncode != 0:
        tail = ' | '.join(((run.stderr or '').strip().splitlines() or lines)[-2:])[:200]
        return None, f'studio.mjs balance exit {run.returncode}' + (f': {tail}' if tail else '')
    try:
        credits = json.loads(lines[-1])['credits']
    except (IndexError, ValueError, KeyError, TypeError):
        return None, 'studio.mjs balance printed no JSON balance'
    if isinstance(credits, bool) or not isinstance(credits, (int, float)) or credits < 0 or credits != credits:
        return None, f'studio.mjs balance gave no numeric balance ({credits!r})'
    return credits, None


def tripo_balance():
    """The Tripo balance or None when it cannot be read (see read_tripo_balance; None is unknown, never zero)."""
    return read_tripo_balance()[0]


def fails(name, locks=None):
    path = (LOCKS if locks is None else locks) / f'{name}.fails'
    return int(path.read_text(encoding='utf-8')) if path.exists() else 0


def claim(name):
    """Take the job's lock unless a live process holds it (an idle worker may still join that job, see joinable)."""
    return claim_module(LOCKS / f'{name}.lock')


def release(name):
    lock = LOCKS / f'{name}.lock'
    if lock_holder(lock) == os.getpid():
        lock.unlink(missing_ok=True)


def free_modules(job, record, root=None):
    """Modules of the job nobody has converted or is converting (module locks are written by convert_job)."""
    base = (ROOT if root is None else root) / 'models' / record['set'] / record['job_id']
    free, busy = 0, 0
    for entry in record['modules']:
        race, module = entry['race_gender'], entry['module']
        if module_converted(record, f'{race}/{module}', base / race / module):
            continue
        if lock_holder(base / '.module_locks' / f'{race}__{module}.lock'):
            busy += 1
        else:
            free += 1
    return free, busy


PROBE_WAIT = 0.5   # seconds job_eligible waits for a record a writer holds before it answers 'record busy'


def probe_record(path, root, wait=None):
    """The job record for the read-only eligibility probe (recheck-before-cutover finding 16): the lock is the one of `root`
    (never the imported checkout's), is never created (a record no writer of that root ever locked is read as it is) and is
    waited for at most `wait` (default PROBE_WAIT) seconds, then portable.LockBusy. Nothing is written anywhere."""
    with portable.file_lock(path, exclusive=False, sidecar=True, timeout=PROBE_WAIT if wait is None else wait, create=False, root=root):
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def candidate(job, root=None, probe=False):
    """(free, busy) modules of a staged job folder when the worker would consider it at all, else (None, reason): its record is
    readable, it is not `converted*` in processing/, and at least one module is neither converted nor held (free).
    `probe` (job_eligible): the record is read through probe_record (root-scoped, bounded, creates nothing); a record a writer
    holds right now is (None, 'record busy'), i.e. temporarily not a candidate."""
    try:
        record = probe_record(job / '.tripo-job.json', ROOT if root is None else root) if probe else read_record(job / '.tripo-job.json')
    except portable.LockBusy:
        return None, 'record busy'
    except (OSError, ValueError) as error:
        return None, f'job record unreadable ({type(error).__name__})'
    if probe and not isinstance(record, dict):
        return None, 'job record unreadable (not an object)'
    if stage_of(job) == 'processing' and str(record.get('state', '')).startswith('converted'):
        return None, f'already {record.get("state")}'
    free, busy = free_modules(job, record, root)
    if not free:
        return None, ('every unconverted module is being converted by another worker' if busy else 'no module left to convert')
    return (free, busy), None


def listed(set_id, sets):
    """The convert list's rule: some allow pattern matches `set_id` and no `!` exclude does."""
    return (any(fnmatch.fnmatchcase(set_id, pattern) for pattern in sets if not pattern.startswith('!'))
            and not any(fnmatch.fnmatchcase(set_id, pattern[1:]) for pattern in sets if pattern.startswith('!')))


def read_namespace(root):
    """The preview app's record of whose a listed id is (`.codex_jobs/_app/set_namespace.json`, tools/preview/v2/convert_list.py):
    ({set id: ...}, {family id: parent set}); ({}, {}) when the file does not exist; None when it exists but cannot be read."""
    path = Path(root) / '.codex_jobs' / '_app' / 'set_namespace.json'
    try:
        raw = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {}, {}
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get('sets', {}), dict) or not isinstance(raw.get('families', {}), dict):
        return None
    return raw.get('sets', {}), {k: v for k, v in raw.get('families', {}).items() if isinstance(v, str)}


def owner_allows(root, job_name, record, sets):
    """(ok, reason): the dispatch check of the job's RECORDED owner (pre-cut-over review finding 2). `sets` = the convert list's
    patterns; the caller already knows that the job's own prefix is listed. A listed id authorizes the job only when:
      - the record names `parent_set` X: X is listed, and the namespace's owner of the job's prefix (itself when it was approved
        as a set, the set it was written for as a family id), when recorded, is X; with an unreadable namespace the owner cannot
        be verified and the job waits;
      - the record names no parent: when the prefix was written as a family id of S, S is listed (else the list alone decides).
    Read-only."""
    prefix = job_name.split('--')[0]
    parent = record.get('parent_set') if isinstance(record, dict) else None
    namespace = read_namespace(root)
    owner = None
    if namespace:
        owner = prefix if prefix in namespace[0] else namespace[1].get(prefix)
    if parent is not None:
        if not isinstance(parent, str) or not parent:
            return False, 'its record has an invalid parent_set'
        if not listed(parent, sets):
            return False, f'its record names the set {parent}, which is not on the convert list'
        if namespace is None:
            return False, 'set_namespace.json is unreadable: the owner its record names cannot be verified'
        if owner and owner != parent:
            return False, f'its record names the set {parent}, but {prefix} is on the convert list for the set {owner}'
        return True, f'owner {parent}'
    if owner and owner != prefix and not listed(owner, sets):
        return False, f'{prefix} is a family id of the set {owner}, which is not on the convert list'
    return True, f'owner {owner or prefix}'


_OWNER_REPORTED = set()


def owner_ok(job, sets):
    """owner_allows for a staged job folder in the worker's loop; a refusal is emitted once per job and reason."""
    try:
        record = read_record(job / '.tripo-job.json')
    except (OSError, ValueError):
        return False   # candidate() skips an unreadable record too
    ok, reason = owner_allows(ROOT, job.name, record, sets)
    if not ok and (job.name, reason) not in _OWNER_REPORTED:
        _OWNER_REPORTED.add((job.name, reason))
        emit(f'OWNER {job.name}: not converted: {reason}')
    return ok


def next_job(sets):
    """Returns (job, owner): an unheld job first (taking its lock), else a free module of a job another worker holds. A job is
    considered only when its set id is listed AND its recorded owner agrees (owner_ok)."""
    candidates = []
    for stage in ('processing', 'multiview_ready'):
        for job in sorted((ROOT / stage).glob('*--*')):
            set_id = job.name.split('--')[0]
            if not listed(set_id, sets):
                continue
            counts, _ = candidate(job)
            if counts and owner_ok(job, sets):
                candidates.append((job, *counts))
    for max_fails in (0, MAX_TRIES - 1):
        for job, free, busy in candidates:
            if fails(job.name) <= max_fails and job.exists() and claim(job.name):
                return job, True
        # join only jobs whose holder runs the module-locking convert_job (it has live module locks)
        joinable = [(free, job) for job, free, busy in candidates
                    if busy and fails(job.name) <= max_fails and stage_of(job) == 'processing']
        if joinable:
            return max(joinable)[1], False
    return None, False


def job_eligible(root, job_name):
    """(eligible, reason): whether a worker would start or join `job_name` now, by exactly next_job's rules but read-only (no lock
    is taken, nothing is written): the job folder is in processing/ or multiview_ready/, its record is readable and not
    `converted*`, at least one module is free, it has failed fewer than MAX_TRIES times, and its job lock is free (or held by a
    dead process: claim takes it over) or, held by a live worker, the job is joinable (in processing/ with a module a live worker
    is converting). It is scoped to `root` (the record lock of that root, never the imported checkout's), creates no file or
    folder and never waits longer than PROBE_WAIT: a record a writer holds is (False, 'record busy'). The convert list (set patterns) and the PAUSE / CONVERT_HOLD gate are the caller's to check."""
    root = Path(root)
    locks = root / '.codex_jobs' / '_batch' / 'convert_locks'
    job = next((root / stage / job_name for stage in ('processing', 'multiview_ready') if (root / stage / job_name).is_dir()), None)
    if job is None:
        return False, 'not staged (no folder in processing/ or multiview_ready/)'
    counts, why = candidate(job, root, probe=True)
    if not counts:
        return False, why
    free, busy = counts
    try:
        failed = fails(job_name, locks)
    except (OSError, ValueError):
        return False, 'failure count unreadable'
    if failed > MAX_TRIES - 1:
        return False, f'failed {failed} times (MAX_TRIES {MAX_TRIES})'
    holder = lock_holder(locks / f'{job_name}.lock')
    if holder is None or holder == os.getpid():
        return True, f'{free} free module(s); job lock free'
    if busy and stage_of(job) == 'processing':
        return True, f'{free} free module(s); joinable (job lock held by pid {holder}, {busy} module(s) in progress)'
    return False, f'job lock held by pid {holder} and the job is not joinable (no module in progress)'


def stage_of(job):
    return job.parent.name


def convert(job, args):
    log = LOGS / f'{job.name}.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('a', encoding='utf-8') as handle:
        handle.write(f'\n=== {datetime.now().isoformat(timespec="seconds")} {" ".join(args)}\n')
        handle.flush()
        return subprocess.run([sys.executable, str(ROOT / 'tools/tripo_pipeline/convert_job.py'), job.name, *args],
                              cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT,
                              env={**os.environ, DRAIN_AWARE_ENV: '1'}).returncode   # this worker never counts DRAIN_EXIT as a failure


def offload(set_id, names):
    """Move the completed jobs' high-poly and heavy working files to the archive drive (config.json `offload`;
    user, 2026-11-18); never raises, a missing drive is only reported."""
    try:
        enabled, dest = offload_models.settings(ROOT)
        if not enabled:
            return
        result = offload_models.offload(offload_models.job_dirs(ROOT, names), ROOT, dest, apply=True)
        if result['skipped']:
            emit(f"OFFLOAD {set_id}: skipped, {result['skipped']}")
            return
        emit(f"OFFLOAD {set_id}: moved {result['files']} file(s), {result['bytes'] / 1e9:.1f} GB to {dest}")
        for error in result['errors'][:5]:
            emit(f'OFFLOAD {set_id}: {error}')
    except Exception as error:  # the move step must never stop a worker
        emit(f'OFFLOAD failed: {error!r}')


def finish(job_name=None):
    """Run finish_sets --apply for the set of `job_name` (all sets when None); never raises."""
    try:
        only = {finish_sets.set_of(job_name, finish_sets.races_of(ROOT)[1])} if job_name else None
        for status in finish_sets.run(ROOT, only, apply=True):
            for name in status['moved']:
                emit(f"FINISH {status['set']}: moved {name} to completed/")
            if status['moved']:
                offload(status['set'], status['moved'])
            for error in status['errors']:
                emit(f"FINISH {status['set']}: {error}")
    except Exception as error:  # the move step must never stop a worker
        emit(f'FINISH failed: {error!r}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--sets', default='', help='comma-separated set ids or fnmatch patterns')
    parser.add_argument('--sets-file', type=Path, help='one set id/pattern per line; re-read every loop, so sets can be added '
                        'without restarting the workers')
    parser.add_argument('--reserve', type=int, default=0)
    parser.add_argument('--poll', type=int, default=300)
    parser.add_argument('--worker', type=int, default=1)
    parser.add_argument('--no-tripo', action='store_true')
    parser.add_argument('--tripo-only', action='store_true', help='never fall back to Hyper3D: when the Tripo credits '
                        'run out the worker reports STOP and exits (user, 2026-11-14)')
    parser.add_argument('--backend', choices=('tripo', 'pixal3d'), default='tripo',
                        help='pixal3d: convert with the free local Pixal3D backend only (tools/pixal3d_pipeline)')
    parser.add_argument('--fallback', choices=('hyper3d', 'pixal3d'), default='hyper3d',
                        help='backend for the modules left when the Tripo credits run out (default hyper3d)')
    args = parser.parse_args(argv)
    global TAG, WORKER
    WORKER = args.worker
    if args.worker > 1:
        TAG = f'[w{args.worker}] '
        profile = PROFILES / f'hyper3d-workflow-profile-w{args.worker}'
        if not profile.is_dir():
            raise SystemExit(f'missing profile clone {profile}')
        os.environ['HYPER3D_PROFILE'] = str(profile)
    sets, tripo_done, idle, low, paused = set(args.sets.split(',')), args.no_tripo, False, False, None
    last_balance, balance_hold = None, None   # last good Tripo reading (credits, time); the unreadable-balance reason last reported
    clear_stale_stop(args.worker)
    while True:
        stop = stop_request(args.worker)
        if stop:
            honour_stop(args.worker, stop)
            return
        blocked = conversions_blocked()
        if blocked:
            if paused != blocked:
                emit(f'PAUSED ({blocked}); not starting conversions')
                paused = blocked
            nap(min(args.poll, 30))
            continue
        paused = None
        free = shutil.disk_usage(ROOT).free
        if free < MIN_FREE:
            if not low:
                emit(f'LOW DISK: {free / 2**30:.1f} GB free (minimum {MIN_FREE // 2**30} GB); not starting conversions')
                low = True
            nap(args.poll)
            continue
        low = False
        if args.sets_file:
            sets = set(filter(None, args.sets.split(','))) | {l.strip() for l in args.sets_file.read_text(encoding='utf-8').splitlines()
                                                              if l.strip() and not l.startswith('#')}
        job, owner = next_job(sets)
        if job is None:
            if not idle:
                emit('IDLE: no staged job; waiting')
                idle = True
            runner_gone = not portable.process_running('view_batch.py run')            # pgrep -f on Linux, psutil scan on Windows
            busy = portable.process_running('tripo_pipeline/convert_job.py')
            if runner_gone and not busy:
                emit('IDLE: view batch runner gone, nothing staged and no conversion in flight; exiting')
                finish()
                return
            if runner_gone and busy and not idle:
                emit('IDLE: nothing staged, but conversions are in flight; waiting (runner gone)')
                idle = True
            nap(args.poll)
            continue
        idle = False
        emit(f'CONVERT {job.name}' + ('' if owner else ' (joining: free modules)'))
        code = 3
        if args.backend == 'pixal3d':
            code = convert(job, ['--backend', 'pixal3d'])
        elif not tripo_done:
            balance, problem = read_tripo_balance()
            if balance is None:
                # unknown is not zero (finding 4): no fallback backend, tripo_done unchanged; hold the job and read again
                if balance_hold != problem:
                    last = (f'; last good balance {last_balance[0]} at {last_balance[1]}' if last_balance else '')
                    emit(f'HOLD {job.name}: Tripo balance unreadable ({problem}); not converting it on any backend, '
                         f'retrying in {min(args.poll, BALANCE_RETRY)} s{last}')
                    balance_hold = problem
                release(job.name)
                nap(min(args.poll, BALANCE_RETRY))
                continue
            balance_hold = None
            last_balance = (balance, datetime.now().isoformat(timespec='seconds'))
            emit(f'BALANCE tripo {balance}')
            submits = max(0, (balance - args.reserve) // TRIPO_COST)
            if not submits:
                tripo_done = True    # a verified shortfall: a numeric balance below the per-module cost
            else:
                code = convert(job, ['--max-submits', str(submits)])
                if code != DRAIN_EXIT and (code == 3 or balance - args.reserve < TRIPO_COST * 2):
                    after, problem = read_tripo_balance()
                    if after is None and code == 3:
                        # phase 5 finding 3: a partial job on an unknown balance is held, never sent to the fallback backend
                        emit(f'HOLD {job.name}: Tripo balance unreadable after a partial job ({problem}); not switching to '
                             f'{args.fallback}, Tripo stays on, retrying in {min(args.poll, BALANCE_RETRY)} s'
                             + (f'; last good balance {last_balance[0]} at {last_balance[1]}' if last_balance else ''))
                        balance_hold = problem
                        release(job.name)
                        nap(min(args.poll, BALANCE_RETRY))
                        continue
                    if after is None:
                        emit(f'BALANCE tripo unreadable after {job.name} ({problem}); Tripo stays on, read again before the next job')
                    else:
                        last_balance = (after, datetime.now().isoformat(timespec='seconds'))
                        emit(f'BALANCE tripo {after} after {job.name}')
                        tripo_done = after - args.reserve < TRIPO_COST
                        if code == 3 and not tripo_done:
                            # still affordable: the rest stays on Tripo (the next loop claims the job again); not a failure
                            emit(f'PARTIAL {job.name}: Tripo balance {after} still covers a module; continuing on Tripo')
                            release(job.name)
                            continue
        name = job.name
        if code == DRAIN_EXIT:
            # DESIGN_PHASE4 I.10: the child was blocked at its paid gate when a drain began and left (lock released, module deferred)
            emit(f'DEFERRED {name}: convert_job left its paid gate for a conversion drain (CONVERT_DRAIN); not a failure')
            release(name)
            continue
        if code == 3 and args.tripo_only and args.backend == 'tripo':
            emit(f'STOP {name}: Tripo credits exhausted and --tripo-only is set; no Hyper3D fallback, worker exiting')
            release(name)
            finish()
            return
        if code == 3 and args.backend == 'tripo':
            if not args.no_tripo:
                emit(f'PARTIAL {name}: Tripo credits exhausted; continuing on {args.fallback}')
            tripo_done = True
            code = convert(job, ['--backend', 'pixal3d'] if args.fallback == 'pixal3d'
                           else ['--backend', 'hyper3d', '--allow-paid', '--accept-preview'])
        if code == DRAIN_EXIT:
            emit(f'DEFERRED {name}: convert_job left its paid gate for a conversion drain (CONVERT_DRAIN); not a failure')
            release(name)
            continue
        if code == 0:
            emit(f'CONVERTED {name}')
            (LOCKS / f'{name}.fails').unlink(missing_ok=True)
            finish(name)
        elif code == 4:
            emit(f'SHARED {name}: no free modules left; other workers are finishing theirs')
        else:
            (LOCKS / f'{name}.fails').write_text(str(fails(name) + 1))
            emit(f'FAILED {name}: convert_job exit {code}; see {LOGS / (name + ".log")}')
        release(name)


if __name__ == '__main__':
    main()
