"""The convert worker's allow-list, `.codex_jobs/_batch/convert_sets.txt` (DESIGN_PHASE4.md section B).

`tools/convert_queue.py --sets-file` re-reads this file every loop. One pattern per line (fnmatch, case-sensitive); `#` starts a comment line
(only at the very first column, as the worker reads it); a leading `!` is an exclude. A job is converted when its set id
(`job.name.split('--')[0]`) matches an allow pattern and no exclude (`convert_queue.next_job`).

    patterns(root) / matches(set_id, patterns) / is_listed(root, set_id)   read side (the worker's rules)
    family_ids(root, set_id)                                               the exact job set ids of a set (DESIGN_PHASE4.md I.6)
    add_sets(root, set_ids, extra=())                                      append those exact ids under a lock, idempotent
    pending_jobs(root) / held_jobs(root)                                   staged jobs the worker will / will not take
    eligible_jobs(root)                                                    listed jobs the worker would really take now (F20, row 20)
    namespace_conflict(root, set_id) / owner_of(root, id)                  the reserved family namespace (phase 5 review row 3)

Nothing here ever removes or reorders an existing line; `add_sets` only appends (atomic replace of the whole file). It never writes a wildcard
(phase 4 review F3 / DESIGN_PHASE4.md I.6): a set id `S` (literal, the approval grammar `serve.NAME`) is written as `S`, `S_<race>` for every race
of `base_views/*/palette.json` and `S_<family>` for the gnome race families (`S_gnome`, the historical gnome-only job name); existing user
patterns stay as they are.
"""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import guard  # noqa: E402

try:
    import psutil  # noqa: E402
except ImportError:      # module-lock holders then count as alive (the conservative answer for "is this module free?")
    psutil = None

NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')   # = serve.NAME: ONE set-name grammar for approve and release (F3; a test compares them)
SET_RE = NAME
STAGES = ('processing', 'multiview_ready')   # the stages `convert_queue.next_job` looks at (in its order)
MAX_TRIES = 3                                # = tools/convert_queue.py MAX_TRIES (a test compares them)


def list_path(root) -> Path:
    return Path(root) / '.codex_jobs' / '_batch' / 'convert_sets.txt'


def lock_path(root) -> Path:
    return Path(root) / '.codex_jobs' / '_batch' / 'convert_sets.lock'


def parse(text: str) -> list[str]:
    """The patterns of a file's text, exactly as the worker reads them: stripped, blank lines and column-0 `#` lines skipped."""
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith('#')]


def patterns(root) -> list[str]:
    try:
        return parse(list_path(root).read_text(encoding='utf-8'))
    except OSError:
        return []


def excluded_by(set_id: str, patterns_: list[str]) -> list[str]:
    """The `!` patterns that match `set_id` (as written, with the `!`)."""
    return [p for p in patterns_ if p.startswith('!') and fnmatch.fnmatchcase(set_id, p[1:])]


def allowed_by(set_id: str, patterns_: list[str]) -> list[str]:
    return [p for p in patterns_ if not p.startswith('!') and fnmatch.fnmatchcase(set_id, p)]


def matches(set_id: str, patterns_: list[str]) -> bool:
    """The worker's rule: some allow pattern matches and no `!` exclude does."""
    return bool(allowed_by(set_id, patterns_)) and not excluded_by(set_id, patterns_)


def status(set_id: str, patterns_: list[str]) -> dict:
    """{"listed": bool, "state": "listed"|"unlisted"|"excluded", "allow": [...], "exclude": [...]}; an exclude wins over an allow."""
    allow, exclude = allowed_by(set_id, patterns_), excluded_by(set_id, patterns_)
    state = 'excluded' if exclude else ('listed' if allow else 'unlisted')
    return {'listed': state == 'listed', 'state': state, 'allow': allow, 'exclude': exclude}


def is_listed(root, set_id: str) -> bool:
    return matches(set_id, patterns(root))


def base_races(root) -> list[str]:
    """Race folders of `base_views/` that carry a palette.json (as serve._base_races / finish_sets.races_of)."""
    try:
        return sorted(p.parent.name for p in (Path(root) / 'base_views').glob('*/palette.json') if NAME.fullmatch(p.parent.name))
    except OSError:
        return []


def family_suffixes(races: list[str]) -> list[str]:
    """The job-name suffixes of a set's family: every race, plus the gnome race families (`gnome` of `gnome_female`)."""
    return list(races) + sorted({r.rsplit('_', 1)[0] for r in races if r.startswith('gnome') and '_' in r})


def family_ids(root, set_id: str, races: list[str] | None = None) -> list[str]:
    """The exact job set ids the pipeline creates for a set (DESIGN_PHASE4.md I.6): `S`, `S_<race>` for every race with a palette.json and
    `S_<gnome family>` (`S_gnome`, the historical gnome-only job name that catalog.job_set / finish_sets.set_of map back to `S`)."""
    races = base_races(root) if races is None else races
    out = [set_id]
    for suffix in family_suffixes(races):
        if f'{set_id}_{suffix}' not in out:
            out.append(f'{set_id}_{suffix}')
    return out


# ------------------------------------------------------------------------------------------------------------ set id namespace (row 3)
# A family id `S_<race>` written for set S pre-authorizes every job of that name. An independently named set that happens to end in a race
# suffix must therefore never share it (phase 5 review row 3): the namespace is RESERVED when a set is approved (namespace_conflict refuses the
# approval) and RECORDED in `.codex_jobs/_app/set_namespace.json` = {"sets": {S: time}, "families": {S_<race>: S}} by add_sets, so the catalog
# (catalog.job_set) and add_sets know whose a suffixed id is instead of guessing from the suffix.

CONCEPT_STAGES = ('concepts', 'provisional', 'approved', 'archive/concepts')
JOB_STAGES = ('processing_views', 'multiview_ready', 'processing', 'completed', 'failed', 'held_conversions')
_NS_CACHE: dict[str, tuple] = {}


def namespace_path(root) -> Path:
    return Path(root) / guard.APP / 'set_namespace.json'


def read_namespace(root) -> dict:
    """{"sets": {set: time}, "families": {family id: set}} (empty when absent or unreadable; cached by the file's mtime)."""
    path = namespace_path(root)
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {'sets': {}, 'families': {}}
    hit = _NS_CACHE.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    data = {'sets': {k: v for k, v in (raw.get('sets') or {}).items() if isinstance(k, str) and NAME.fullmatch(k)}
            if isinstance(raw.get('sets'), dict) else {},
            'families': {k: v for k, v in (raw.get('families') or {}).items() if isinstance(k, str) and isinstance(v, str)
                         and NAME.fullmatch(k) and NAME.fullmatch(v)} if isinstance(raw.get('families'), dict) else {}}
    _NS_CACHE[str(path)] = (stamp, data)
    return data


def owner_of(root, set_id: str) -> str | None:
    """The recorded owner of an id: itself when it was approved / released as a set, its parent when it was written as a family id, else
    None (unrecorded: the caller falls back to the suffix rule)."""
    data = read_namespace(root)
    if set_id in data['sets']:
        return set_id
    return data['families'].get(set_id)


def _folders(root, stages) -> set[str]:
    out = set()
    for stage in stages:
        try:
            out |= {p.name for p in (Path(root) / stage).iterdir() if p.is_dir() and NAME.fullmatch(p.name)}
        except OSError:
            continue
    return out


def independent_sets(root) -> set[str]:
    """Sets that exist in their own right: concept-stage set folders (concepts, provisional, approved, archive) and recorded sets."""
    return _folders(root, CONCEPT_STAGES) | set(read_namespace(root)['sets'])


def namespace_conflict(root, set_id: str, races: list[str] | None = None) -> str | None:
    """Why approving `set_id` would share another set's convert-list family (phase 5 review row 3), else None:
      - `set_id` was written as a family id of another set (set_namespace.json),
      - `set_id` = `<P>_<suffix>` for a race / gnome-family suffix and P is a set (a concept-stage folder, a recorded set, a job's set prefix
        or a models/ folder): its jobs would match P's pre-authorized family entry,
      - an independent set `<set_id>_<suffix>` exists: approving `set_id` would pre-authorize it as `set_id`'s race job."""
    races = base_races(root) if races is None else races
    suffixes = family_suffixes(races)
    data = read_namespace(root)
    parent = data['families'].get(set_id)
    if parent and parent != set_id:
        return f'{set_id} is reserved: it is the convert-list job id of the set {parent}'
    known = independent_sets(root) | _folders(root, ('models',))
    for stage in JOB_STAGES:
        try:
            known |= {p.name.split('--')[0] for p in (Path(root) / stage).iterdir() if p.is_dir() and '--' in p.name}
        except OSError:
            continue
    for suffix in suffixes:
        tail = '_' + suffix
        if set_id.endswith(tail) and len(set_id) > len(tail):
            base = set_id[:-len(tail)]
            if base in known and base != set_id:
                return (f'{set_id} ends in {tail} and {base} is a set: its jobs would share the convert-list entry of {base}\'s {suffix} jobs; '
                        'choose another set id')
    for other in sorted(independent_sets(root)):
        if other != set_id and other.startswith(set_id + '_') and other[len(set_id) + 1:] in suffixes \
                and data['families'].get(other) in (None, set_id) and other in (set(data['sets']) | _folders(root, CONCEPT_STAGES)):
            return (f'the set {other} exists: approving {set_id} would pre-authorize it as {set_id}\'s {other[len(set_id) + 1:]} job; '
                    'choose another set id')
    return None


def _register(root, set_ids: list[str], families: dict[str, str]) -> None:
    """Record approved / released sets and the family ids written for them (under the convert-list lock; never removes an entry)."""
    data = read_namespace(root)
    sets, fams = dict(data['sets']), dict(data['families'])
    changed = False
    for set_id in set_ids:
        if set_id not in sets:
            sets[set_id], changed = guard.now(), True
    for fid, parent in families.items():
        if fid not in fams and fid not in sets:
            fams[fid], changed = parent, True
    if changed:
        guard.write_atomic(namespace_path(root), (json.dumps({'sets': sets, 'families': fams}, indent=1, sort_keys=True) + '\n').encode('utf-8'))


def add_sets(root, set_ids, extra=()) -> dict:
    """Append the exact family ids (`family_ids`) of every set, plus the exact ids in `extra` (e.g. the prefix of a staged job of the set),
    never a wildcard. Under the file lock; an exact line already present (stripped) is not added again. A set an existing `!` exclude matches
    is still written but reported in `excluded` (the worker keeps skipping it; the user decides about the exclude). A family id that is an
    independent set of its own (another set's folder, a recorded set, or another set's recorded family id) is NOT written: it is reported in
    `skipped` (phase 5 review row 3). The sets and the family ids written are recorded in set_namespace.json.
    Returns {"added": [lines], "present": [lines], "excluded": {set: [patterns]}, "skipped": {set: [{id, reason}]}}."""
    ids = [set_ids] if isinstance(set_ids, str) else list(set_ids)
    extra = [extra] if isinstance(extra, str) else list(extra)
    bad = [s for s in ids + extra if not isinstance(s, str) or not NAME.fullmatch(s)]
    if bad:
        raise ValueError(f'invalid set id(s): {bad}')
    races = base_races(root)
    namespace = read_namespace(root)
    independent = independent_sets(root)
    wanted, skipped, owned = {}, {}, {}
    for set_id in ids:
        lines = [set_id]
        for fid in family_ids(root, set_id, races)[1:]:
            owner = namespace['families'].get(fid)
            if fid in independent or (owner and owner != set_id):
                skipped.setdefault(set_id, []).append({'id': fid, 'reason': f'{fid} is ' + (f'a family id of {owner}' if owner and owner != set_id
                                                                                               else 'a set of its own')})
                continue
            lines.append(fid)
            owned[fid] = set_id
        wanted[set_id] = lines
    path = list_path(root)
    with guard.os_lock(lock_path(root)):
        _register(root, ids, owned)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raw = b''
        text = raw.decode('utf-8', errors='replace')
        newline = '\r\n' if '\r\n' in text else '\n'
        have = {line.strip() for line in text.splitlines()}
        added, present = [], []
        for line in [line for lines in wanted.values() for line in lines] + extra:
            if line in have:
                if line not in present:
                    present.append(line)
            elif line not in added:
                added.append(line)
        if added:
            body = raw if not raw or raw.endswith(b'\n') else raw + newline.encode()
            guard.write_atomic(path, body + ''.join(line + newline for line in added).encode('utf-8'))
        current = parse(path.read_text(encoding='utf-8')) if path.exists() else []
    excluded = {}
    for set_id, lines in wanted.items():
        found = sorted({p for line in lines for p in excluded_by(line, current)})
        if found:
            excluded[set_id] = found
    return {'added': added, 'present': present, 'excluded': excluded, 'skipped': skipped}


# ---------------------------------------------------------------------------------------------------------------------- staged jobs

def _record(job: Path) -> dict | None:
    try:
        record = json.loads((job / '.tripo-job.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _unconverted(root: Path, record: dict) -> list[tuple[str, str]]:
    """[(race, module)] of the job nobody converted yet (`convert_job.module_converted`: a module_conversion entry and reduced/manifest.json)."""
    base = Path(root) / 'models' / str(record.get('set', '')) / str(record.get('job_id', ''))
    done = record.get('module_conversion') if isinstance(record.get('module_conversion'), dict) else {}
    out = []
    modules = record.get('modules') if isinstance(record.get('modules'), list) else []
    for entry in modules:
        if not isinstance(entry, dict) or not isinstance(entry.get('race_gender'), str) or not isinstance(entry.get('module'), str):
            continue
        race, module = entry['race_gender'], entry['module']
        if not (f'{race}/{module}' in done and (base / race / module / 'reduced' / 'manifest.json').exists()):
            out.append((race, module))
    return out


def _unconverted_modules(root: Path, record: dict) -> int:
    return len(_unconverted(root, record))


def _holder_alive(lock: Path) -> bool:
    """A module / job lock file names a live holder pid (convert_job.lock_holder; without psutil any pid counts as alive)."""
    try:
        pid = int(lock.read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    if psutil is None:
        return True
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def fails(root, job: str) -> int:
    """`convert_locks/<job>.fails` (convert_queue.fails; 0 when absent or unreadable)."""
    try:
        return int((Path(root) / '.codex_jobs' / '_batch' / 'convert_locks' / f'{job}.fails').read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        return 0


def staged_jobs(root) -> list[dict]:
    """Jobs the worker would consider if the set were listed: `multiview_ready/` jobs and `processing/` jobs whose record is not
    `converted*` and that still have an unconverted module. [{job, stage, set, state, modules_left}]"""
    out = []
    for stage in STAGES:
        for job in sorted((Path(root) / stage).glob('*--*')):
            if not job.is_dir():
                continue
            record = _record(job)
            if record is None:
                continue   # the worker skips a job whose record it cannot read
            state = str(record.get('state', ''))
            if stage == 'processing' and state.startswith('converted'):
                continue
            left = _unconverted_modules(root, record)
            if left:
                out.append({'job': job.name, 'stage': stage, 'set': job.name.split('--')[0], 'state': state, 'modules_left': left})
    return out


def pending_jobs(root) -> list[dict]:
    """Staged jobs the worker will take (their set matches the list), each with the matching patterns."""
    pats = patterns(root)
    return [{**j, 'allow': allowed_by(j['set'], pats)} for j in staged_jobs(root) if matches(j['set'], pats)]


def worker_predicate():
    """`tools/convert_queue.py job_eligible(root, job_name) -> (bool, reason)`: the worker's own read-only claim / join predicate (phase 5 review
    row 20), imported lazily (convert_queue imports the conversion tooling); None when it is missing (an older convert_queue.py) or cannot be
    imported, and eligible_jobs falls back to its own copy of the rule."""
    try:
        tools = str(HERE.parents[1])
        if tools not in sys.path:
            sys.path.insert(0, tools)
        import convert_queue  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    fn = getattr(convert_queue, 'job_eligible', None)
    return fn if callable(fn) else None


def _fallback_eligible(root, job: dict, record: dict) -> tuple[bool, str, int]:
    """The worker's rule (convert_queue.next_job, read-only) when its predicate is unavailable: (eligible, reason, free modules). A job is taken
    when it has a FREE module (unconverted, no live module-lock holder) AND either its job lock (`convert_locks/<job>.lock`) has no live holder
    (claim) or, in processing/, another worker's convert_job holds some of its modules (join). A held job lock with every module free can be
    neither claimed nor joined."""
    base = Path(root) / 'models' / str(record.get('set', '')) / str(record.get('job_id', ''))
    free = busy = 0
    for race, module in _unconverted(root, record):
        if _holder_alive(base / '.module_locks' / f'{race}__{module}.lock'):
            busy += 1
        else:
            free += 1
    if not free:
        return False, 'no free module', 0
    if not _holder_alive(Path(root) / '.codex_jobs' / '_batch' / 'convert_locks' / f'{job["job"]}.lock'):
        return True, 'claimable', free
    if job['stage'] == 'processing' and busy:
        return True, 'joinable', free
    return False, 'its job lock is held and no module of it is being converted (cannot be claimed or joined)', free


def eligible_jobs(root, max_tries: int = MAX_TRIES) -> list[dict]:
    """Listed staged jobs the worker would really take now (convert_queue.next_job, read-only; phase 4 review F20, phase 5 review row 20):
    fewer than `max_tries` failures and claimable or joinable by the worker's own predicate `convert_queue.job_eligible` (worker_predicate; the
    copy in _fallback_eligible when it is unavailable). [{job, stage, set, free, fails, reason}]"""
    out = []
    predicate = worker_predicate()
    for job in pending_jobs(root):
        count = fails(root, job['job'])
        if count >= max_tries:
            continue
        record = _record(Path(root) / job['stage'] / job['job'])
        if record is None:
            continue
        ok, reason, free = _fallback_eligible(root, job, record)
        if predicate is not None:
            try:
                verdict = predicate(Path(root), job['job'])
                ok, reason = bool(verdict[0]), str(verdict[1]) if len(verdict) > 1 else ''
            except Exception as exc:  # noqa: BLE001  (the predicate failing never wakes the worker)
                ok, reason = False, f'convert_queue.job_eligible failed: {type(exc).__name__}: {exc}'
        if ok:
            out.append({'job': job['job'], 'stage': job['stage'], 'set': job['set'], 'free': free, 'fails': count, 'reason': reason})
    return out


def held_jobs(root) -> list[dict]:
    """Staged jobs the worker ignores: set not in the convert list ("held: not in the convert list") or excluded by a `!` pattern.
    [{job, stage, set, state, modules_left, reason: "unlisted"|"excluded", exclude: [...]}]; Release = `add_sets(root, [set])`
    (for `excluded` that does not help: the exclude line has to go)."""
    pats = patterns(root)
    out = []
    for job in staged_jobs(root):
        info = status(job['set'], pats)
        if info['state'] != 'listed':
            out.append({**job, 'reason': info['state'], 'exclude': info['exclude']})
    return out
