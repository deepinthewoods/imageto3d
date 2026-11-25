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

While .codex_jobs/_batch/PAUSE exists (tools/pipeline_supervisor.py) no job is started (PAUSED, once).

Graceful stop (preview app v2): creating .codex_jobs/_batch/convert_worker.stop makes a worker exit before it takes its next
job (it deletes the file, emits `STOP requested; exiting`, runs finish() like the idle exit); a conversion in flight completes
first. A stop file left from before a worker started is removed at start.

--backend pixal3d (user, 2026-11-18): convert every job with the free local Pixal3D backend only (no Tripo balance check, no
Hyper3D); one generation at a time per machine (the backend's GPU lock), so one pixal3d worker is enough. --fallback pixal3d: use
it instead of Hyper3D once the Tripo credits run out (--tripo-only still stops instead).

Events (stdout and .codex_jobs/_batch/convert_events.log): CONVERT, CONVERTED, PARTIAL, FAILED, BALANCE, IDLE, FINISH, PAUSED.
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
STOP_FILE = ROOT / '.codex_jobs' / '_batch' / 'convert_worker.stop'   # graceful stop: written by the preview app (services.py)
STOP_NAP = 5   # seconds: the longest a waiting worker sleeps before it looks at the stop file again
sys.path.insert(0, str(ROOT / 'tools' / 'tripo_pipeline'))
from convert_job import claim_module, lock_holder, module_converted, read_record  # noqa: E402
import portable  # noqa: E402  (also puts PYTHONUTF8=1 into the environment of the convert_job children on Windows)
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
    while time.time() < end and not STOP_FILE.exists():
        time.sleep(min(STOP_NAP, max(0.0, end - time.time())))


def tripo_balance():
    run = subprocess.run(['node', str(ROOT / 'tools/tripo_pipeline/studio.mjs'), 'balance'],
                         capture_output=True, text=True, encoding='utf-8', cwd=ROOT, timeout=300)
    try:
        return json.loads(run.stdout.strip().splitlines()[-1])['credits']
    except (IndexError, ValueError, KeyError):
        return None


def fails(name):
    path = LOCKS / f'{name}.fails'
    return int(path.read_text(encoding='utf-8')) if path.exists() else 0


def claim(name):
    """Take the job's lock unless a live process holds it (an idle worker may still join that job, see joinable)."""
    return claim_module(LOCKS / f'{name}.lock')


def release(name):
    lock = LOCKS / f'{name}.lock'
    if lock_holder(lock) == os.getpid():
        lock.unlink(missing_ok=True)


def free_modules(job, record):
    """Modules of the job nobody has converted or is converting (module locks are written by convert_job)."""
    base = ROOT / 'models' / record['set'] / record['job_id']
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


def next_job(sets):
    """Returns (job, owner): an unheld job first (taking its lock), else a free module of a job another worker holds."""
    candidates = []
    for stage in ('processing', 'multiview_ready'):
        for job in sorted((ROOT / stage).glob('*--*')):
            set_id = job.name.split('--')[0]
            allowed = any(fnmatch.fnmatchcase(set_id, pattern) for pattern in sets if not pattern.startswith('!'))
            excluded = any(fnmatch.fnmatchcase(set_id, pattern[1:]) for pattern in sets if pattern.startswith('!'))
            if not allowed or excluded:
                continue
            try:
                record = read_record(job / '.tripo-job.json')
            except (OSError, ValueError):
                continue
            if stage == 'processing' and str(record.get('state', '')).startswith('converted'):
                continue
            free, busy = free_modules(job, record)
            if free:
                candidates.append((job, free, busy))
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


def stage_of(job):
    return job.parent.name


def convert(job, args):
    log = LOGS / f'{job.name}.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('a', encoding='utf-8') as handle:
        handle.write(f'\n=== {datetime.now().isoformat(timespec="seconds")} {" ".join(args)}\n')
        handle.flush()
        return subprocess.run([sys.executable, str(ROOT / 'tools/tripo_pipeline/convert_job.py'), job.name, *args],
                              cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT).returncode


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
    global TAG
    if args.worker > 1:
        TAG = f'[w{args.worker}] '
        profile = PROFILES / f'hyper3d-workflow-profile-w{args.worker}'
        if not profile.is_dir():
            raise SystemExit(f'missing profile clone {profile}')
        os.environ['HYPER3D_PROFILE'] = str(profile)
    sets, tripo_done, idle, low, paused = set(args.sets.split(',')), args.no_tripo, False, False, False
    STOP_FILE.unlink(missing_ok=True)   # a stop requested before this start is stale
    while True:
        if STOP_FILE.exists():
            STOP_FILE.unlink(missing_ok=True)
            emit('STOP requested; exiting')
            finish()
            return
        if PAUSE.exists():
            if not paused:
                emit('PAUSED (PAUSE file); not starting conversions')
                paused = True
            nap(min(args.poll, 30))
            continue
        paused = False
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
            balance = tripo_balance()
            emit(f'BALANCE tripo {balance}')
            submits = max(0, ((balance or 0) - args.reserve) // TRIPO_COST)
            if submits:
                code = convert(job, ['--max-submits', str(submits)])
            if balance is not None and balance - args.reserve < TRIPO_COST * 2:
                tripo_done = (tripo_balance() or 0) - args.reserve < TRIPO_COST
        name = job.name
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
