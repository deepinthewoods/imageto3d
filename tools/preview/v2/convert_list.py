"""The convert worker's allow-list, `.codex_jobs/_batch/convert_sets.txt` (DESIGN_PHASE4.md section B).

`tools/convert_queue.py --sets-file` re-reads this file every loop. One pattern per line (fnmatch, case-sensitive); `#` starts a comment line
(only at the very first column, as the worker reads it); a leading `!` is an exclude. A job is converted when its set id
(`job.name.split('--')[0]`) matches an allow pattern and no exclude (`convert_queue.next_job`).

    patterns(root) / matches(set_id, patterns) / is_listed(root, set_id)   read side (the worker's rules)
    family_ids(root, set_id)                                               the exact job set ids of a set (DESIGN_PHASE4.md I.6)
    add_sets(root, set_ids, extra=())                                      append those exact ids under a lock, idempotent
    pending_jobs(root) / held_jobs(root)                                   staged jobs the worker will / will not take
    eligible_jobs(root)                                                    listed jobs the worker would really take now (F20)

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


def family_ids(root, set_id: str, races: list[str] | None = None) -> list[str]:
    """The exact job set ids the pipeline creates for a set (DESIGN_PHASE4.md I.6): `S`, `S_<race>` for every race with a palette.json and
    `S_<gnome family>` (`S_gnome`, the historical gnome-only job name that catalog.job_set / finish_sets.set_of map back to `S`)."""
    races = base_races(root) if races is None else races
    families = sorted({r.rsplit('_', 1)[0] for r in races if r.startswith('gnome') and '_' in r})
    out = [set_id]
    for suffix in races + families:
        if f'{set_id}_{suffix}' not in out:
            out.append(f'{set_id}_{suffix}')
    return out


def add_sets(root, set_ids, extra=()) -> dict:
    """Append the exact family ids (`family_ids`) of every set, plus the exact ids in `extra` (e.g. the prefix of a staged job of the set),
    never a wildcard. Under the file lock; an exact line already present (stripped) is not added again. A set an existing `!` exclude matches
    is still written but reported in `excluded` (the worker keeps skipping it; the user decides about the exclude).
    Returns {"added": [lines], "present": [lines], "excluded": {set: [patterns]}}."""
    ids = [set_ids] if isinstance(set_ids, str) else list(set_ids)
    extra = [extra] if isinstance(extra, str) else list(extra)
    bad = [s for s in ids + extra if not isinstance(s, str) or not NAME.fullmatch(s)]
    if bad:
        raise ValueError(f'invalid set id(s): {bad}')
    races = base_races(root)
    wanted = {set_id: family_ids(root, set_id, races) for set_id in ids}
    path = list_path(root)
    with guard.os_lock(lock_path(root)):
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
    return {'added': added, 'present': present, 'excluded': excluded}


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


def eligible_jobs(root, max_tries: int = MAX_TRIES) -> list[dict]:
    """Listed staged jobs the worker would really take now (convert_queue.next_job, read-only; phase 4 review F20): fewer than `max_tries`
    failures and at least one FREE module (unconverted and no live module-lock holder). [{job, stage, set, free, fails}]"""
    out = []
    for job in pending_jobs(root):
        count = fails(root, job['job'])
        if count >= max_tries:
            continue
        record = _record(Path(root) / job['stage'] / job['job'])
        if record is None:
            continue
        base = Path(root) / 'models' / str(record.get('set', '')) / str(record.get('job_id', ''))
        free = sum(1 for race, module in _unconverted(root, record) if not _holder_alive(base / '.module_locks' / f'{race}__{module}.lock'))
        if free:
            out.append({'job': job['job'], 'stage': job['stage'], 'set': job['set'], 'free': free, 'fails': count})
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
