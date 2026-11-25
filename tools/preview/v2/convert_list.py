"""The convert worker's allow-list, `.codex_jobs/_batch/convert_sets.txt` (DESIGN_PHASE4.md section B).

`tools/convert_queue.py --sets-file` re-reads this file every loop. One pattern per line (fnmatch, case-sensitive); `#` starts a comment line
(only at the very first column, as the worker reads it); a leading `!` is an exclude. A job is converted when its set id
(`job.name.split('--')[0]`) matches an allow pattern and no exclude (`convert_queue.next_job`).

    patterns(root) / matches(set_id, patterns) / is_listed(root, set_id)   read side (the worker's rules)
    add_sets(root, set_ids)                                                append `<set>` and `<set>_*` under a lock, idempotent
    pending_jobs(root) / held_jobs(root)                                   staged jobs the worker will / will not take

Nothing here ever removes or reorders an existing line; `add_sets` only appends (atomic replace of the whole file).
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

SET_RE = re.compile(r'^[a-z0-9][a-z0-9_]*$')
STAGES = ('processing', 'multiview_ready')   # the stages `convert_queue.next_job` looks at (in its order)


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


def add_sets(root, set_ids) -> dict:
    """Append `<set>` and `<set>_*` for every id (a gnome-only job is `<set>_gnome--<id>`, so its set id needs the wildcard line).
    Under the file lock; an exact line already present (stripped) is not added again. A set an existing `!` exclude matches is still
    written but reported in `excluded` (the worker keeps skipping it; the user decides about the exclude).
    Returns {"added": [lines], "present": [lines], "excluded": {set: [patterns]}}."""
    ids = [set_ids] if isinstance(set_ids, str) else list(set_ids)
    bad = [s for s in ids if not isinstance(s, str) or not SET_RE.match(s)]
    if bad:
        raise ValueError(f'invalid set id(s): {bad}')
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
        for set_id in ids:
            for line in (set_id, f'{set_id}_*'):
                if line in have:
                    present.append(line)
                elif line not in added:
                    added.append(line)
        if added:
            body = raw if not raw or raw.endswith(b'\n') else raw + newline.encode()
            guard.write_atomic(path, body + ''.join(line + newline for line in added).encode('utf-8'))
        current = parse(path.read_text(encoding='utf-8')) if path.exists() else []
    return {'added': added, 'present': present,
            'excluded': {s: found for s in ids if (found := excluded_by(s, current))}}


# ---------------------------------------------------------------------------------------------------------------------- staged jobs

def _record(job: Path) -> dict | None:
    try:
        record = json.loads((job / '.tripo-job.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _unconverted_modules(root: Path, record: dict) -> int:
    """Modules of the job nobody converted yet (`convert_job.module_converted`: a module_conversion entry and reduced/manifest.json)."""
    base = Path(root) / 'models' / str(record.get('set', '')) / str(record.get('job_id', ''))
    done = record.get('module_conversion') if isinstance(record.get('module_conversion'), dict) else {}
    count = 0
    for entry in record.get('modules') or []:
        try:
            race, module = entry['race_gender'], entry['module']
        except (KeyError, TypeError):
            continue
        if not (f'{race}/{module}' in done and (base / race / module / 'reduced' / 'manifest.json').exists()):
            count += 1
    return count


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
