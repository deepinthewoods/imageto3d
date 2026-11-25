"""Pipeline progress of the v2 preview app (DESIGN_PHASE4.md sections E, F and H). Read-only: nothing here writes a file.

    task_index(root)          the view runner's `.codex_jobs/_batch/tasks/*.json` + `status/*.json`. Cached by the two directories' mtimes
                              (the runner, the supervisor and v2's Retry replace files atomically, so every change moves a directory mtime;
                              a full rescan still happens every REFRESH_S) and per file by (mtime, size): a warm call is two stat calls.
    Facts(root, races, set_of) one snapshot for a whole catalog derivation: task index, supervisor holds, convert-list patterns,
                              `convert_locks/*.fails` counts and live job locks.
    unit_progress(...)        `progress` of one unit: views_done / views_total / views_state, fronts_state, held_reason / held_kind,
                              converting_step, error (DESIGN_PHASE4.md H "Approved tab progress").
    views_section / conversions_section / holds_section / status_summary / problems    GET /api/v2/queue and /api/v2/state.

Task names (tools/view_batch.py): `views--<job prefix>--<race>--<module>[-<suffix>]` (the prefix is the job folder's set id, e.g. `S`,
`S_dwarf_male`, `S_gnome`) and `fronts--<set>--<race>[-<suffix>]`; a status file absent = `pending`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import convert_list  # noqa: E402

try:
    import psutil  # noqa: E402
except ImportError:      # pid checks fall back to "unknown = not alive"
    psutil = None

VIEWS = ('front', 'front_three_quarter', 'back', 'back_three_quarter')
REFRESH_S = 5.0                   # a full rescan of the task folders at least this often, whatever the directory mtimes say
LOG_TAIL_BYTES = 1500
EVENTS_TAIL = 20
CONVERT_MAX_TRIES = 3             # = tools/convert_queue.py MAX_TRIES: a job with this many .fails is no longer taken (a test compares them)
_LOCK = threading.Lock()
_INDEX: dict[str, dict] = {}
_FILES: dict[tuple, tuple] = {}   # (kind, path) -> (signature, parsed) for small json files read often


# ------------------------------------------------------------------------------------------------------------------- helpers

def batch(root) -> Path:
    return Path(root) / '.codex_jobs' / '_batch'


def _mtime(path: Path) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None


def _cached_json(kind: str, path: Path):
    """A small json file, re-read only when its (mtime, size) changed."""
    try:
        st = os.stat(path)
    except OSError:
        _FILES.pop((kind, str(path)), None)
        return None
    sig = (st.st_mtime_ns, st.st_size)
    hit = _FILES.get((kind, str(path)))
    if hit and hit[0] == sig:
        return hit[1]
    value = _json(path)
    _FILES[(kind, str(path))] = (sig, value)
    return value


def tail_text(path: Path, size: int = LOG_TAIL_BYTES) -> str:
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - size))
            return handle.read().decode('utf-8', 'replace').lstrip()
    except OSError:
        return ''


def tail_lines(path: Path, n: int = EVENTS_TAIL) -> list[str]:
    return [line for line in tail_text(path, 16384).splitlines() if line.strip()][-n:]


def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0 or psutil is None:
        return False
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def lock_holder(path: Path) -> int | None:
    """The live pid written in a convert lock file (tools/tripo_pipeline/convert_job.py `claim_module`), else None."""
    try:
        pid = int(Path(path).read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def iso_age(text, now: float) -> float | None:
    """Seconds since an ISO local timestamp (the runner's `started`)."""
    from datetime import datetime
    try:
        return round(now - datetime.fromisoformat(str(text)).timestamp(), 1)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# ---------------------------------------------------------------------------------------------------------------- task index

def parse_name(name: str) -> dict:
    """{kind, prefix, race, module, suffix} of a task name (module None for a fronts task; unknown shapes keep only `kind`)."""
    parts = name.split('--')
    entry = {'kind': parts[0], 'prefix': None, 'race': None, 'module': None, 'suffix': ''}
    if parts[0] == 'views' and len(parts) == 4:
        module, _, suffix = parts[3].partition('-')
        entry.update(prefix=parts[1], race=parts[2], module=module, suffix=suffix)
    elif parts[0] == 'fronts' and len(parts) == 3:
        race, _, suffix = parts[2].partition('-')
        entry.update(prefix=parts[1], race=race, suffix=suffix)
    return entry


def _scan(folder: Path) -> dict[str, tuple]:
    """{name without .json: (mtime_ns, size)} of the `*.json` files of a folder (one directory listing; DirEntry.stat is free on Windows)."""
    out = {}
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.name.endswith('.json'):
                    try:
                        if entry.is_file():
                            st = entry.stat()
                            out[entry.name[:-5]] = (st.st_mtime_ns, st.st_size)
                    except OSError:
                        continue
    except OSError:
        pass
    return out


def _status_of(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    jobs = raw.get('jobs') if isinstance(raw.get('jobs'), list) else []
    last = jobs[-1] if jobs and isinstance(jobs[-1], dict) else {}
    attempts = raw.get('attempts')
    return {'state': raw.get('state') if isinstance(raw.get('state'), str) else 'pending',
            'attempts': attempts if isinstance(attempts, int) and not isinstance(attempts, bool) else 0,
            'jobs': len(jobs), 'job': last.get('job'), 'started': last.get('started'), 'exit': last.get('exit')}


def task_index(root) -> dict:
    """{'tasks': {name: parse_name + {mtime}}, 'status': {name: _status_of}} of `.codex_jobs/_batch` (only names with a task file)."""
    tasks_dir, status_dir = batch(root) / 'tasks', batch(root) / 'status'
    key = str(Path(root))
    with _LOCK:
        stamp = (_mtime(tasks_dir), _mtime(status_dir))
        cached = _INDEX.get(key)
        if cached and cached['stamp'] == stamp and time.monotonic() - cached['scanned'] < REFRESH_S:
            return cached['view']
        old_tasks = cached['task_sigs'] if cached else {}
        old_status = cached['status_files'] if cached else {}
        task_sigs = _scan(tasks_dir)
        old_view = cached['view']['tasks'] if cached else {}
        tasks = {name: old_view[name] if old_tasks.get(name) == sig and name in old_view else parse_name(name) | {'mtime': sig[0]}
                 for name, sig in task_sigs.items()}
        status_files, statuses = {}, {}
        for name, sig in _scan(status_dir).items():
            if name not in task_sigs:
                continue
            prev = old_status.get(name)
            parsed = prev[1] if prev and prev[0] == sig else _status_of(_json(status_dir / f'{name}.json'))
            status_files[name] = (sig, parsed)
            statuses[name] = parsed
        view = {'tasks': tasks, 'status': statuses}
        _INDEX[key] = {'stamp': stamp, 'scanned': time.monotonic(), 'task_sigs': task_sigs, 'status_files': status_files, 'view': view}
        return view


def task_outputs(root, name: str) -> list[str]:
    """The view names (file stems) a task writes, from its `outputs` (Linux or Windows paths alike); cached by the file's (mtime, size)."""
    raw = _cached_json('task', batch(root) / 'tasks' / f'{name}.json')
    outputs = raw.get('outputs') if isinstance(raw, dict) and isinstance(raw.get('outputs'), list) else []
    return [PurePosixPath(str(o).replace('\\', '/')).stem for o in outputs if isinstance(o, str)]


def state_of(index: dict, name: str) -> dict:
    return index['status'].get(name) or {'state': 'pending', 'attempts': 0, 'jobs': 0, 'job': None, 'started': None, 'exit': None}


# --------------------------------------------------------------------------------------------------------------------- facts

def read_holds(root) -> dict:
    """supervisor_state.json `holds`: {key: {reasons, sig}} (key = a job folder name, a set id, or `<set>#gnome`)."""
    raw = _cached_json('supervisor_state', batch(root) / 'supervisor_state.json')
    holds = raw.get('holds') if isinstance(raw, dict) and isinstance(raw.get('holds'), dict) else {}
    return {k: v for k, v in holds.items() if isinstance(k, str) and isinstance(v, dict)}


def convert_locks(root) -> dict:
    """{'fails': {job: n > 0}, 'locks': {job: live holder pid}} of `.codex_jobs/_batch/convert_locks/`."""
    folder = batch(root) / 'convert_locks'
    fails, locks = {}, {}
    try:
        with os.scandir(folder) as entries:
            names = [e.name for e in entries]
    except OSError:
        names = []
    for name in names:
        if name.endswith('.fails'):
            raw = _cached_json('fails', folder / name)
            if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                fails[name[:-6]] = raw
        elif name.endswith('.lock'):
            holder = lock_holder(folder / name)
            if holder:
                locks[name[:-5]] = holder
    return {'fails': fails, 'locks': locks}


def reason_scope(reason: str, races) -> list[tuple[str, str | None]]:
    """The units a hold reason names, as [(race, module | None)]: `race/module: ...`, `race: ...`, task names `...--race--module` in the text,
    or bare race names (`fronts task failed: dwarf_male, elf_female`). [] = the reason concerns every unit of what is held."""
    races = set(races)
    head = reason.split(':', 1)[0].strip()
    if '/' in head and ' ' not in head and head.split('/', 1)[0] in races:
        race, module = head.split('/', 1)
        return [(race, module)]
    if head in races:
        return [(head, None)]
    found = []
    for word in reason.replace(',', ' ').replace(';', ' ').split():
        parts = word.strip('()[]').split('--')
        if len(parts) == 4 and parts[2] in races:
            found.append((parts[2], parts[3].partition('-')[0]))
        elif len(parts) == 3 and parts[1] in races:
            found.append((parts[1], parts[2].partition('-')[0]))
        elif len(parts) == 1 and parts[0] in races:
            found.append((parts[0], None))
    return found


class Facts:
    """One snapshot of the pipeline files for a whole derivation (built once per catalog derivation; cheap when nothing changed)."""

    def __init__(self, root, races: list[str], set_of) -> None:
        self.root = Path(root)
        self.races = races
        self.index = task_index(root)
        self.views: dict[tuple, list[str]] = {}
        self.fronts: dict[tuple, list[str]] = {}
        for name, entry in self.index['tasks'].items():
            if entry['kind'] == 'views' and entry['module']:
                self.views.setdefault((set_of(entry['prefix']), entry['race'], entry['module']), []).append(name)
            elif entry['kind'] == 'fronts' and entry['race']:
                self.fronts.setdefault((set_of(entry['prefix']), entry['race']), []).append(name)
        self.holds = read_holds(root)
        self.set_holds: dict[str, list[tuple[str, list[str]]]] = {}
        for key, value in self.holds.items():
            if '--' not in key:
                base = key.split('#', 1)[0]
                self.set_holds.setdefault(set_of(base), []).append((key, [str(r) for r in value.get('reasons') or []]))
        self.patterns = convert_list.patterns(root)
        locks = convert_locks(root)
        self.fails, self.job_locks = locks['fails'], locks['locks']

    # ---------------------------------------------------------------------------------------------------- lookups
    def _newest(self, names: list[str], prefix: str | None) -> str | None:
        if not names:
            return None
        if prefix:
            own = [n for n in names if self.index['tasks'][n]['prefix'] == prefix]
            names = own or names
        return max(names, key=lambda n: (self.index['tasks'][n]['mtime'], n))

    def view_task(self, set_name: str, race: str, module: str, prefix: str | None) -> str | None:
        return self._newest(self.views.get((set_name, race, module), []), prefix)

    def fronts_task(self, set_name: str, race: str) -> str | None:
        return self._newest(self.fronts.get((set_name, race), []), None)

    def hold_reasons(self, set_name: str, job_name: str | None, race: str, module: str, claimed: bool) -> list[str]:
        """The supervisor hold reasons that concern this unit: job holds of its job (all reasons, or only the ones naming its race /
        module), and, for a unit not claimed yet, the set's gate-1 holds (`<set>` for the other races, `<set>#gnome` for gnomes)."""
        found: list[str] = []
        sources = []
        if job_name and job_name in self.holds:
            sources.append([str(r) for r in self.holds[job_name].get('reasons') or []])
        if not claimed:
            for key, reasons in self.set_holds.get(set_name, []):
                if key.endswith('#gnome') == race.startswith('gnome'):
                    sources.append(reasons)
        for reasons in sources:
            for reason in reasons:
                scope = reason_scope(reason, self.races)
                if not scope or any(r == race and m in (None, module) for r, m in scope):
                    found.append(reason)
        return found


# ------------------------------------------------------------------------------------------------------------- unit progress

def module_step(root, model_set: str, job_id: str, race: str, module: str) -> str:
    """What a module being converted is doing now, from its files (convert_job.py writes module_conversion only at the end)."""
    folder = Path(root) / 'models' / model_set / job_id / race / module
    if (folder / 'reduced' / 'manifest.json').exists():
        return 'post-processing (split / align / winding)'
    if (folder / 'reduced').is_dir():
        return 'reducing'
    submission = _json(folder / 'studio_submission.json')
    if not isinstance(submission, dict):
        return 'submitting'
    backend = str(submission.get('backend') or 'tripo')
    stage = submission.get('stage') or submission.get('status')
    return f'{backend}: {stage}' if stage else backend


def empty_progress(error: str | None = None) -> dict:
    return {'views_done': None, 'views_total': None, 'views_state': None, 'views_task': None, 'views_attempts': None,
            'fronts_state': None, 'fronts_task': None, 'held_reason': None, 'held_kind': None, 'held_detail': None,
            'converting_step': None, 'converting_pid': None, 'convert_fails': None, 'error': error}


def unit_progress(root, set_name: str, unit: dict, job: dict | None, facts: Facts) -> dict:
    """`progress` of one unit (DESIGN_PHASE4.md H): views x/n from the view task, the hold that keeps it, the conversion step, the error.
    `job` = the catalog job record that gives the unit its status (None when no job carries it yet)."""
    race, module, status = unit['race'], unit['module'], unit['status']
    out = empty_progress(unit.get('error'))
    if status == 'dropped':
        return out
    images = set((job or {}).get('views', {}).get((race, module), {}) or {}) & set(VIEWS)
    prefix = job['name'].split('--')[0] if job else None
    if job is None and status not in ('reduced', 'pin_missing'):
        out.update(views_done=0, views_total=len(VIEWS))
        name = facts.fronts_task(set_name, race)
        if name:
            st = state_of(facts.index, name)
            out.update(fronts_state=st['state'], fronts_task=name)
            if st['state'] == 'failed' and not out['error']:
                out['error'] = f'fronts task failed: {name}'
    elif job is not None and job['stage'] == 'processing_views' and status not in ('reduced', 'pin_missing'):
        name = facts.view_task(set_name, race, module, prefix)
        state, outputs = None, set()
        if name:
            st = state_of(facts.index, name)
            state = st['state']
            outputs = set(task_outputs(root, name)) & set(VIEWS)
            out.update(views_state=state, views_task=name, views_attempts=st['attempts'])
            if state == 'failed' and not out['error']:
                out['error'] = f'views task failed after {st["attempts"]} attempt(s): {name}'
        # the views the module ends with: the task's outputs plus the views already there (a legacy task kept the concept front; a
        # two-view gnome task writes back.png only); without a task (not queued yet) all four
        needed = (outputs | images) if outputs else (set(VIEWS) | images)
        total = len(needed)
        out.update(views_total=total, views_done=total if state == 'done' else len(images & needed))
    else:
        total = len(images) or len(VIEWS)
        out.update(views_done=total, views_total=total)
    # held: supervisor hold > held_conversions/ > excluded > not in the convert list
    reasons = facts.hold_reasons(set_name, job['name'] if job else None, race, module, claimed=job is not None or status != 'missing')
    stage = job['stage'] if job else None
    if reasons:
        out.update(held_kind='supervisor', held_reason='; '.join(reasons)[:400])
    elif status == 'held':
        out.update(held_kind='held_conversions', held_reason='held for review (held_conversions/)')
    elif job and stage in convert_list.STAGES and status in ('ready', 'converting'):
        info = convert_list.status(prefix, facts.patterns)
        if info['state'] == 'excluded':
            out.update(held_kind='excluded', held_reason='excluded', held_detail=f'excluded by {", ".join(info["exclude"])} in convert_sets.txt')
        elif info['state'] == 'unlisted':
            out.update(held_kind='convert_list', held_reason='not in convert list', held_detail='Release adds the set to convert_sets.txt')
    if job and status in ('ready', 'converting') and job['name'] in facts.job_locks:
        lock = Path(root) / 'models' / job['model_set'] / job['id'] / '.module_locks' / f'{race}__{module}.lock'
        holder = lock_holder(lock)
        if holder:
            out.update(converting_step=module_step(root, job['model_set'], job['id'], race, module), converting_pid=holder)
    fails = facts.fails.get(job['name']) if job else None
    if fails and status in ('ready', 'converting', 'failed'):
        out['convert_fails'] = fails
        if fails >= CONVERT_MAX_TRIES and not out['error']:      # below the limit the worker simply tries again
            out['error'] = f'conversion failed {fails} times; the convert worker no longer takes this job (Reset in the Queue tab)'
    return out


def card_badges(units: list[dict]) -> list[dict]:
    """Approved-tab card badges from the live units' progress (DESIGN_PHASE4.md H): views a/b, converting n/m, held, not in convert list,
    excluded, failed. Units without progress (a fresh derive_one) are skipped."""
    live = [u for u in units if u['status'] != 'dropped' and u.get('progress')]
    if not live:
        return []
    badges = []
    n = len(live)
    views_ok = sum(1 for u in live if (u['progress']['views_done'] or 0) >= (u['progress']['views_total'] or 1))
    if views_ok < n:
        badges.append({'text': f'views {views_ok}/{n}', 'tone': 'info'})
    converting = sum(1 for u in live if u['status'] == 'converting')
    if converting:
        badges.append({'text': f'converting {converting}/{n}', 'tone': 'info'})
    held = sum(1 for u in live if u['progress']['held_kind'] in ('supervisor', 'held_conversions'))
    if held:
        badges.append({'text': f'held {held}', 'tone': 'warn'})
    if any(u['progress']['held_kind'] == 'convert_list' for u in live):
        badges.append({'text': 'not in convert list', 'tone': 'warn'})
    if any(u['progress']['held_kind'] == 'excluded' for u in live):
        badges.append({'text': 'excluded from convert list', 'tone': 'warn'})
    failed = sum(1 for u in live if u['status'] == 'failed' or (u['progress']['error'] and u['status'] not in ('reduced', 'pin_missing')))
    if failed:
        badges.append({'text': f'failed {failed}', 'tone': 'error'})
    return badges


# ------------------------------------------------------------------------------------------------------------- queue sections

def views_section(root, now: float | None = None, limit: int = 200) -> dict:
    """`views` of GET /api/v2/queue: counts by state (and by task kind), running tasks with elapsed time, failed tasks with the last job's
    log tail (Retry = op view_retry)."""
    now = time.time() if now is None else now
    index = task_index(root)
    counts: dict[str, int] = {}
    by_kind: dict[str, dict[str, int]] = {}
    running, failed = [], []
    for name in sorted(index['tasks']):
        st = state_of(index, name)
        counts[st['state']] = counts.get(st['state'], 0) + 1
        kind = index['tasks'][name]['kind']
        by_kind.setdefault(kind, {})
        by_kind[kind][st['state']] = by_kind[kind].get(st['state'], 0) + 1
        if st['state'] == 'running':
            running.append({'name': name, 'job': st['job'], 'started': st['started'], 'elapsed': iso_age(st['started'], now)})
        elif st['state'] == 'failed':
            job = st['job']
            tail = tail_text(Path(root) / '.codex_jobs' / job / 'log.txt') if isinstance(job, str) and job and '/' not in job and '\\' not in job else ''
            failed.append({'name': name, 'attempts': st['attempts'], 'job': job, 'exit': st['exit'], 'log_tail': tail})
    return {'counts': counts, 'by_kind': by_kind, 'running': running[:limit], 'failed': failed[:limit], 'total': len(index['tasks'])}


def _job_folder(root, job: str) -> Path | None:
    for stage in ('processing', 'multiview_ready', 'processing_views', 'completed', 'failed'):
        folder = Path(root) / stage / job
        if folder.is_dir():
            return folder
    return None


def conversions_section(root) -> dict:
    """`conversions` of GET /api/v2/queue: the job(s) a worker holds and the module(s) being converted, the convert_events.log tail, the jobs
    with failures (Reset = op convert_reset) and the staged jobs the worker ignores (`convert_list.held_jobs`; Release = op release)."""
    locks = convert_locks(root)
    current = []
    for job, holder in sorted(locks['locks'].items()):
        folder = _job_folder(root, job)
        record = _json(folder / '.tripo-job.json') if folder else None
        record = record if isinstance(record, dict) else {}
        model_set, job_id = str(record.get('set') or job.split('--')[0]), str(record.get('job_id') or job.partition('--')[2])
        module_locks = Path(root) / 'models' / model_set / job_id / '.module_locks'
        modules = []
        try:
            names = sorted(p.name for p in module_locks.glob('*.lock'))
        except OSError:
            names = []
        for name in names:
            module_holder = lock_holder(module_locks / name)
            if module_holder:
                race, _, module = name[:-5].partition('__')
                modules.append({'job': job, 'module': f'{race}/{module}', 'holder_pid': module_holder,
                                'step': module_step(root, model_set, job_id, race, module)})
        current += modules or [{'job': job, 'module': None, 'holder_pid': holder, 'step': None}]
    return {'current': current, 'events_tail': tail_lines(batch(root) / 'convert_events.log'),
            'fails': [{'job': job, 'count': n, 'stuck': n >= CONVERT_MAX_TRIES} for job, n in sorted(locks['fails'].items())],
            'max_tries': CONVERT_MAX_TRIES,
            'held': convert_list.held_jobs(root),
            'excludes': [p for p in convert_list.patterns(root) if p.startswith('!')]}


def holds_section(root, set_of=None) -> list[dict]:
    """`holds` of GET /api/v2/queue: [{key, reasons, set, job, gnome, drift}] from supervisor_state.json."""
    out = []
    for key, value in sorted(read_holds(root).items()):
        job = key if '--' in key else None
        base = key.split('--')[0].split('#', 1)[0]
        reasons = [str(r) for r in value.get('reasons') or []]
        out.append({'key': key, 'reasons': reasons, 'set': set_of(base) if set_of else base, 'job': job, 'gnome': key.endswith('#gnome'),
                    'drift': any('colour DRIFT' in r for r in reasons)})
    return out


def problems(statuses: dict | None) -> list[str] | None:
    """The service states the header counts as problems: duplicate / held / crashed, and an enabled service that is not running without a
    stop request (an idle convert worker and a deliberately stopped service are fine)."""
    if statuses is None:
        return None
    out = []
    for name, st in sorted(statuses.items()):
        state = st.get('state')
        if state in ('duplicate', 'held', 'crashed'):
            out.append(f'{name}: {state}' + (f' ({st.get("message")})' if st.get('message') else ''))
        elif state == 'stopped' and st.get('enabled') and st.get('message') != 'stopped on request':
            out.append(f'{name}: not running')
    return out


def status_summary(root, services_problems: list[str] | None, queue_health) -> dict:
    """`state.status` (DESIGN_PHASE4.md H.1, the shape the phase 4 client reads): {mode: {mode, label, since}, paused: false | {reason, since,
    scope}, services: null (no --manage-services) | {ok, problems: [text]}, queue_health}, plus the flat copies `pause_reason` and
    `services_problems` (the first H wording). Two small file reads."""
    import modes
    mode = modes.read_mode(root)
    pause = modes.read_pause(root)
    services_ = None if services_problems is None else {'ok': not services_problems, 'problems': list(services_problems)}
    return {'mode': mode, 'paused': pause if pause is not None else False, 'services': services_, 'queue_health': queue_health,
            'pause_reason': (pause or {}).get('reason'), 'services_problems': services_problems}
