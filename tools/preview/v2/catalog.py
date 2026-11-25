"""Cards and item details of the v2 preview app (DESIGN.md sections 2 and 4). Read-only.

Everything is derived from the folders the pipeline already writes; nothing here moves or writes files. The slow scans reuse
serve.py's mtime-validated result cache (`serve._memo` / `serve._listing`), and a short TTL cache over the assembled state keeps
bursts of requests (several open tabs) cheap.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import serve  # noqa: E402  (tools/preview/serve.py, used as a library)
import builds  # noqa: E402
import convert_list  # noqa: E402
import decisions  # noqa: E402
import guard  # noqa: E402
import monsters_v2  # noqa: E402  (phase 5: monster concept and variant cards / items)
import progress  # noqa: E402
import taskqueue  # noqa: E402

monsters = serve.monsters
NAME = serve.NAME
TABS = ('concepts', 'provisional', 'approved', '3d', 'aligned', 'completed', 'review', 'archive', 'trash')
JOB_STAGES = ('processing_views', 'multiview_ready', 'processing', 'failed', 'completed', serve.HELD)
FOLDER_STATUS = {'processing_views': 'views', 'multiview_ready': 'ready', 'processing': 'converting', serve.HELD: 'held', 'failed': 'failed'}
STATUS_ORDER = ('converting', 'ready', 'views', 'held', 'failed', 'missing')     # best first, after `dropped` and `reduced`
UNIT_STATUSES = ('reduced', 'dropped', 'pin_missing') + STATUS_ORDER
LODS = serve.LOD_LEVELS
DEFAULT_LOD = '0p2'              # shown (not saved) when a module has no chosen_lod.json: the level 124 of 133 existing picks use
RACE_PREF = ('human_male', 'human_female')
MODULE_RANK = {m: i for i, m in enumerate(serve.MODULES)}
ARMOUR_MODULES = ('body_armor', 'right_gauntlet_boot')
STATE_TTL = 1.0                  # seconds the assembled state / derivation is reused (0 disables); mutations invalidate it
POOL = ThreadPoolExecutor(max_workers=6)     # outer tasks only; the inner per-set scans use serve.POOL
_GEN = [0]
_LOCKS: dict[str, threading.Lock] = {}
_CACHE: dict[tuple, tuple[float, int, object]] = {}

CONCEPT_ACTIONS = {'concepts': ['provisional', 'approve', 'reject'], 'provisional': ['approve', 'unapprove'], 'approved': ['unapprove'],
                   'archive': ['approve', 'unarchive'], 'trash': ['restore']}
LEGACY_ACTIONS = {'concepts': ['approve', 'reject'], 'archive': ['approve', 'unarchive'], 'trash': ['restore']}


def invalidate() -> None:
    """Drop the TTL caches (called after every mutation)."""
    _GEN[0] += 1


def _cached(kind: str, root: Path, build):
    key = (kind, str(root))
    hit = _CACHE.get(key)
    if hit and STATE_TTL > 0 and hit[1] == _GEN[0] and time.monotonic() - hit[0] < STATE_TTL:
        return hit[2]
    with _LOCKS.setdefault(kind, threading.Lock()):     # one lock per kind: the state build waits for the derivation on another thread
        hit = _CACHE.get(key)
        if hit and STATE_TTL > 0 and hit[1] == _GEN[0] and time.monotonic() - hit[0] < STATE_TTL:
            return hit[2]
        gen = _GEN[0]
        value = build()
        _CACHE[key] = (time.monotonic(), gen, value)
        return value


def pretty(name: str) -> str:
    return name.replace('_', ' ')


def race_rank(race: str) -> tuple:
    return (RACE_PREF.index(race) if race in RACE_PREF else len(RACE_PREF), race)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------------------------------------------------- job scan

WARN_FLAGS = (('needs_manual_review', 'needs manual review'), ('manual_review', 'needs manual review'),
              ('free_retries_exhausted', 'free retries exhausted'))


def _short(value: object, limit: int = 200) -> str:
    text = ' '.join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + '...'


def _entry_warnings(entry: dict) -> tuple[list[str], list[str]]:
    """(warnings, levels needing review) a job record keeps about one converted module."""
    warnings = []
    for key, text in WARN_FLAGS:
        if entry.get(key) and text not in warnings:
            warnings.append(text)
    if isinstance(entry.get('input_warning'), dict):
        missing = entry['input_warning'].get('missing_input')
        warnings.append('input views incomplete' + (f' (missing {missing})' if missing else ''))
    for key in ('warnings', 'review_warnings'):
        if isinstance(entry.get(key), list):
            warnings += [_short(w) for w in entry[key] if isinstance(w, str)]
    raw = entry.get('levels_needing_review')
    levels = sorted((lv for lv in raw if lv in LODS), reverse=True) if isinstance(raw, list) else []
    return warnings, levels


def _job_scan(folder: Path, deps: dict) -> dict:
    """What the derivation needs from one job folder (race-independent, so it can be memoised by folder mtimes)."""
    _record, views = serve._job_views(folder, deps)       # lists the folder tree into `deps`; its own record read is cp1252, so read again
    record = read_json(folder / '.tripo-job.json')
    top = serve._listing(str(folder), deps) or {}
    retired = '.pixal3d-job.json' in top and '.tripo-job.json' not in top       # a retired Pixal3D job: inventoried, never work
    problems = []                                   # record fields of the wrong type: ignored, reported (phase 4 review F14)
    for field, kind in (('modules', list), ('conversion_errors', list), ('module_conversion', dict)):
        if field in record and record[field] is not None and not isinstance(record[field], kind):
            problems.append(f'.tripo-job.json: {field} is a {type(record[field]).__name__}, not a {kind.__name__}; ignored')
    modules = []
    for entry in record.get('modules') if isinstance(record.get('modules'), list) else []:
        if isinstance(entry, dict) and isinstance(entry.get('race_gender'), str) and isinstance(entry.get('module'), str):
            modules.append((entry['race_gender'], entry['module']))
    by_view = {(v['name'], v['module']): v['images'] for v in views}
    if not modules:
        modules = sorted(by_view)
    errors = {}
    for text in record.get('conversion_errors') if isinstance(record.get('conversion_errors'), list) else []:
        if isinstance(text, str) and ': ' in text:
            key, _, message = text.partition(': ')
            errors[key] = message
    conversion = record.get('module_conversion') if isinstance(record.get('module_conversion'), dict) else {}
    backends, warnings, review_levels = {}, {}, {}
    for key, entry in conversion.items():
        if isinstance(entry, dict):
            if entry.get('error'):
                errors.setdefault(key, str(entry['error'])[:300])
            if entry.get('backend'):
                backends[key] = entry['backend']
            found, levels = _entry_warnings(entry)
            if found:
                warnings[key] = found
            if levels:
                review_levels[key] = levels
    return {'record_set': record.get('set') if isinstance(record.get('set'), str) and NAME.fullmatch(record['set']) else None,
            'parent_set': record.get('parent_set') if isinstance(record.get('parent_set'), str) and NAME.fullmatch(record['parent_set']) else None,
            'job_id': record.get('job_id') if isinstance(record.get('job_id'), str) and NAME.fullmatch(record['job_id']) else None,
            'state': record.get('state') if isinstance(record.get('state'), str) else None, 'conversion': record.get('conversion'),
            'retired': retired, 'modules': sorted(set(modules)), 'views': by_view, 'errors': errors, 'backends': backends,
            'warnings': warnings, 'review_levels': review_levels, 'converted': sorted(conversion), 'problems': problems, 'scan_error': None}


def _broken_job(error: str) -> dict:
    """What a job folder whose scan failed contributes (F14): nothing but the error, so the set is shown as damaged."""
    return {'record_set': None, 'parent_set': None, 'job_id': None, 'state': None, 'conversion': None, 'retired': False, 'modules': [], 'views': {}, 'errors': {},
            'backends': {}, 'warnings': {}, 'review_levels': {}, 'converted': [], 'problems': [], 'scan_error': error}


def job_set(name: str, races: list[str], root: Path | None = None, parent: str | None = None) -> str:
    """Set id of a job folder name. A RECORDED owner wins (phase 5 review row 3): the job record's `parent_set` (`parent`), else, with `root`,
    convert_list.owner_of (set_namespace.json: an id approved / released as a set of its own is its own set, an id written as a family id
    belongs to the set it was written for). Without a record: `serve._job_set` (a trailing `_<race>` belongs to the base set) plus the
    documented gnome family suffix `_gnome` (tools/tripo_pipeline/finish_sets.py `set_of`; other family suffixes could be part of a real set
    name)."""
    if isinstance(parent, str) and NAME.fullmatch(parent):
        return parent
    if root is not None:
        owner = convert_list.owner_of(root, name.split('--')[0])
        if owner:
            return owner
    base = serve._job_set(name, races)
    if base == name.split('--')[0]:
        for family in sorted({r.rsplit('_', 1)[0] for r in races if r.startswith('gnome') and '_' in r}, key=len, reverse=True):
            if base.endswith('_' + family) and len(base) > len(family) + 1:
                return base[:-len(family) - 1]
    return base


def scan_jobs(root: Path, races: list[str]) -> tuple[dict[str, list[dict]], list[dict]]:
    """({set id: [job]}, [retired job]) over every job folder (processing_views, multiview_ready, processing, failed, completed,
    held_conversions). A folder with a `.pixal3d-job.json` and no `.tripo-job.json` is a retired Pixal3D job: inventoried, never work."""
    result: dict[str, list[dict]] = {}
    retired: list[dict] = []
    seen = set()
    for stage in JOB_STAGES:
        parent = root / stage
        for name in sorted(n for n, is_dir in (serve._listing(str(parent)) or {}).items() if is_dir and not n.startswith('.') and '--' in n):
            folder = parent / name
            seen.add(str(folder))
            try:
                info = serve._memo('v2job', root, folder, lambda deps, f=folder: _job_scan(f, deps))
            except Exception as exc:  # noqa: BLE001  (one damaged job never stops the scan of the others, F14)
                info = _broken_job(f'{stage}/{name}: scan failed ({type(exc).__name__}: {exc})')
            set_name = job_set(name, races, root, info.get('parent_set'))
            job_id = info['job_id'] or name.partition('--')[2]
            job = info | {'name': name, 'stage': stage, 'rel': f'{stage}/{name}', 'id': job_id, 'set': set_name,
                          'model_set': info['record_set'] or name.split('--')[0]}
            (retired if info['retired'] else result.setdefault(set_name, [])).append(job)
    serve._prune('v2job', root, seen)
    return result, retired


def model_sets(set_name: str, jobs: list[dict]) -> list[str]:
    """The models/ folders of a set card: its own name (the decisions) and the physical model dir of every job record
    (a `<set>_<race>--<id>` job's record says `set: <set>_<race>`, and the converter wrote models/<set>_<race>/<id>/...)."""
    return sorted({set_name} | {job['model_set'] for job in jobs if NAME.fullmatch(job['model_set'])})


def fronts_index(root: Path, races: list[str]) -> dict[str, dict[str, str]]:
    """{set id: {race: front task state}} of the supervisor's `fronts--<set>--<race>` tasks (status file = state, absent = pending).
    A race with several tasks counts as `failed` only when every one of them failed. Reads progress.task_index (cached by the task / status
    folders' mtimes: a warm call costs two stat calls instead of one per status file)."""
    index = progress.task_index(root)
    out: dict[str, dict[str, str]] = {}
    for name in sorted(index['tasks']):
        parts = name.split('--') if name.startswith('fronts--') else []
        if len(parts) != 3 or parts[2] not in races:
            continue
        state = progress.state_of(index, name)['state']
        entry = out.setdefault(job_set(parts[1], races, root), {})
        if parts[2] not in entry or entry[parts[2]] == 'failed':
            entry[parts[2]] = state
    return out


# ------------------------------------------------------------------------------------------------------------ model scan

def _models_scan(root: Path, model_set: str, races: list[str], deps: dict) -> dict:
    """{'jobs': {job_id: {race: {module: {reduced, mtime, lod, set}}}}, 'decisions': raw pipeline_v2.json | None, 'decisions_error'} of models/<model_set>."""
    base = f'{root}/models/{model_set}'
    top = serve._listing(base, deps) or {}
    jobs: dict[str, dict] = {}
    for job_id in sorted(n for n, is_dir in top.items() if is_dir and not n.startswith('.') and NAME.fullmatch(n)):
        listing = serve._listing(f'{base}/{job_id}', deps) or {}
        for race in sorted(n for n, is_dir in listing.items() if is_dir and n in races):
            for module, is_dir in sorted((serve._listing(f'{base}/{job_id}/{race}', deps) or {}).items()):
                if not is_dir or module.startswith('.') or not NAME.fullmatch(module):
                    continue
                path = f'{base}/{job_id}/{race}/{module}'
                top_files = serve._listing(path, deps) or {}
                reduced = serve._listing(f'{path}/reduced', deps) if top_files.get('reduced') else None
                complete = bool(reduced) and reduced.get('manifest.json') is False and all(reduced.get(f'lod_error_{lv}.glb') is False for lv in LODS)
                lod = None
                if top_files.get(serve.LOD_CHOICE) is False:
                    serve._stamp(f'{path}/{serve.LOD_CHOICE}', deps)         # rewritten in place by the LOD picker
                    lod = decisions.read_chosen(Path(path))
                entry = {'reduced': complete, 'lod': lod, 'mtime': 0, 'set': model_set}
                if complete:
                    entry['mtime'] = serve._stamp(f'{path}/reduced/manifest.json', deps) or 0
                jobs.setdefault(job_id, {}).setdefault(race, {})[module] = entry
    raw, error = None, None
    if top.get(decisions.FILENAME) is False:
        serve._stamp(f'{base}/{decisions.FILENAME}', deps)
        raw, error = decisions.read_raw(Path(f'{base}/{decisions.FILENAME}'))
    ready = None
    if top.get(decisions.READY) is False:
        serve._stamp(f'{base}/{decisions.READY}', deps)
        ready = ready_facts(Path(f'{base}/{decisions.READY}'))
    return {'jobs': jobs, 'decisions': raw, 'decisions_error': error, 'ready': ready}


def ready_facts(path: Path) -> dict:
    """{approval_id, sha256, valid} of a models/<set>/ready.json: the sha256 of its exact bytes (the Aligned approval stores it as `ready_sha256`,
    re-check R4) and whether it parses as an object carrying an approval_id."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return {'approval_id': None, 'sha256': None, 'valid': False}
    try:
        value = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, ValueError):
        value = None
    approval_id = value.get('approval_id') if isinstance(value, dict) and isinstance(value.get('approval_id'), str) else None
    return {'approval_id': approval_id, 'sha256': hashlib.sha256(raw).hexdigest(), 'valid': approval_id is not None}


def scan_models(root: Path, races: list[str], plan: dict[str, list[str]]) -> dict[str, dict]:
    """{set id: {'jobs': merged over its model dirs, 'decisions', 'decisions_error'}}; `plan` = {set id: [models/ folder names]} (model_sets())."""
    def one(model_set: str) -> dict:
        try:
            return serve._memo('v2models', root, root / 'models' / model_set, lambda deps: _models_scan(root, model_set, races, deps))
        except Exception as exc:  # noqa: BLE001  (isolated per model folder, F14: its set is shown damaged)
            return {'jobs': {}, 'decisions': None, 'decisions_error': None, 'ready': None,
                    'scan_error': f'models/{model_set}: scan failed ({type(exc).__name__}: {exc})'}
    wanted = sorted({ms for sets in plan.values() for ms in sets})
    scanned = dict(zip(wanted, serve.POOL.map(one, wanted)))
    out = {}
    for set_name, sets in plan.items():
        merged: dict = {}
        for ms in sets:
            for job_id, by_race in scanned[ms]['jobs'].items():
                merged.setdefault(job_id, {}).update(by_race)
        own = scanned.get(set_name) or {'decisions': None, 'decisions_error': None}
        errors = [scanned[ms]['scan_error'] for ms in sets if scanned[ms].get('scan_error')]
        out[set_name] = {'jobs': merged, 'decisions': own['decisions'], 'decisions_error': own['decisions_error'], 'ready': own.get('ready'),
                         'scan_error': '; '.join(errors) or None}
    return out


# ------------------------------------------------------------------------------------------------------------ derivation

def _kind_of(modules: list[str]) -> str:
    if any(m in ARMOUR_MODULES for m in modules):
        return 'armour'
    return 'outfit' if 'outfit' in modules else 'legacy'


def _job_status(job: dict, key: str) -> str:
    """The status one job gives a unit it carries and did not reduce."""
    if job['stage'] == 'completed':
        return 'failed' if key in job['errors'] else 'missing'
    if key in job['errors']:
        return 'failed'
    return FOLDER_STATUS[job['stage']]


def required_races(jobs: list[dict], fronts: dict, dec: dict, races: list[str]) -> list[str]:
    """Races a set needs: those in any of its job records (claimed) plus those with a still-planned fronts task (state not `failed`).
    A race never claimed and without a live fronts task is not required (shown as info). Once the 3D approval froze
    `required_races` into `lod_approval`, that list wins (a base added later cannot send a reviewed set back)."""
    frozen = (dec['lod_approval'] or {}).get('required_races')
    if isinstance(frozen, list) and all(isinstance(r, str) for r in frozen) and frozen:
        return sorted(frozen, key=race_rank)
    wanted = {race for job in jobs for race, _module in job['modules']}
    wanted |= {race for race, state in (fronts or {}).items() if state != 'failed'}
    return [race for race in races if race in wanted] + sorted(r for r in wanted if r not in races)


AMOUNT_GROUPS = {'armour': ('body', 'limbs'), 'outfit': ('outfit',), 'legacy': ('outfit',)}
SLOT_GROUP = {'torso': 'body', 'lower_arm.l': 'limbs', 'lower_arm.r': 'limbs', 'lower_leg.l': 'limbs', 'lower_leg.r': 'limbs', 'outfit': 'outfit'}
DEFAULT_AMOUNT = 100


def amount_of(dec: dict, race: str, group: str) -> int:
    groups = dec['amount'].get(race)
    value = groups.get(group) if isinstance(groups, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else DEFAULT_AMOUNT


def _unit_builds(root: Path, set_name: str, units: list[dict], dec: dict) -> dict[str, dict]:
    """`{"race/module": build record}` of the approved units of a set (empty without a 3D approval; see builds.unit_build: only the tasks of the
    CURRENT approval generation count, re-check R11). pin_missing and dropped units have none: they cannot be built."""
    gen = decisions.approval_gen(dec['lod_approval'])
    approved = (dec['lod_approval'] or {}).get('units')
    if not isinstance(approved, dict):
        return {}
    by_unit: dict[str, list[dict]] = {}
    try:                     # the queue is a derived cache: a damaged database must never break the catalog (its health is in /api/v2/state)
        tasks = taskqueue.for_root(root).tasks(lane='layers', set_name=set_name, limit=2000)
    except Exception:  # noqa: BLE001
        tasks = []
    for task in tasks:
        by_unit.setdefault(task['unit'], []).append(task)
    stale = builds.stale_state(root)['units']
    now = time.time()
    stall = builds.stall_minutes(root)
    out = {}
    for unit in units:
        key = f'{unit["race"]}/{unit["module"]}'
        if unit['status'] == 'reduced' and isinstance(approved.get(key), dict):
            out[key] = builds.unit_build(root, set_name, key, unit, approved[key], by_unit.get(key, []), stale, now, stall, gen)
    return out


def bare_snapshots(root: Path, races: list[str]) -> dict[str, tuple]:
    """ONE pointer read per race for a whole derivation (re-check R2): {race: (pointer | None, problem)}."""
    return {race: builds.bare_snapshot(root, race) for race in races}


def bare_builds(root: Path, races: list[str], snaps: dict[str, tuple] | None = None) -> dict[str, dict]:
    """`item.bare_builds` = {race: builds.bare_build(...)} (status, build_id, task) of the live races of an approved set, from `snaps`."""
    snaps = snaps if snaps is not None else bare_snapshots(root, races)
    try:
        tasks = taskqueue.for_root(root).tasks(lane='bare', limit=2000)
    except Exception:  # noqa: BLE001  (a damaged queue never breaks the item)
        tasks = []
    stale = builds.stale_state(root)['bare']
    now, stall = time.time(), builds.stall_minutes(root)
    return {race: builds.bare_build(race, snaps[race], [t for t in tasks if t['unit'] == race], stale, now, stall) for race in races}


def completed_problems(dec: dict, live_keys: list[str], unit_builds: dict[str, dict], races: list[str], ready: dict | None,
                       bare_snaps: dict[str, tuple] | None = None, root: Path | None = None) -> list[str]:
    """Why the Aligned approval does not hold any more (empty = Completed; phase 3 review F3): the approved unit set must be the live required
    unit set (frozen required races) and the 3D-approved set, every approved build id must be the published (fully validated, slot digests
    included: re-check R3) one, every approved bare build must still be the published bare build of its race, and models/<set>/ready.json must
    exist, parse, carry the approval's `approval_id` AND have exactly the bytes whose sha256 the approval stored (`ready_sha256`, re-check R4)."""
    approval, pinned = dec['aligned_approval'] or {}, (dec['lod_approval'] or {}).get('units') or {}
    approved = approval.get('units') if isinstance(approval.get('units'), dict) else {}
    out = []
    live = set(live_keys)
    if set(approved) != live:
        out.append('approved units differ from the live units')
    if set(pinned) != live:
        out.append('3D-approved units differ from the live units')
    for key in sorted(live):
        rec, entry = unit_builds.get(key), approved.get(key)
        if not rec or rec['status'] not in builds.BUILT:
            out.append(f'{key}: no published build')
        elif not isinstance(entry, dict) or entry.get('build_id') != rec['build_id']:
            out.append(f'{key}: published build is not the approved one')
    bare = approval.get('bare') if isinstance(approval.get('bare'), dict) else {}
    for race in races:
        ptr = (bare_snaps[race] if bare_snaps is not None and race in bare_snaps else builds.bare_snapshot(root, race))[0]
        if not ptr or bare.get(race) != ptr['build_id']:
            out.append(f'bare {race}: published bare build is not the approved one')
    approval_id = approval.get('approval_id')
    if not approval_id or not ready or ready.get('approval_id') != approval_id:
        out.append('ready.json is missing or belongs to another approval')
    elif not ready.get('valid') or not approval.get('ready_sha256') or ready.get('sha256') != approval.get('ready_sha256'):
        out.append('ready.json is not the manifest this approval wrote (its bytes differ from ready_sha256)')
    return out


def _amount_changed(dec: dict, kind: str, races: list[str]) -> bool:
    """The saved Amount differs from the one the Aligned approval froze (races x groups of the set's kind, default 100)."""
    approval = dec['aligned_approval']
    if not approval:
        return False
    frozen = {'amount': approval.get('amount') if isinstance(approval.get('amount'), dict) else {}}
    return any(amount_of(dec, race, group) != amount_of(frozen, race, group) for race in races for group in AMOUNT_GROUPS.get(kind, ('outfit',)))


def derive_set(root: Path, set_name: str, jobs: list[dict], models: dict, races: list[str], fronts: dict | None = None,
               intents: list[dict] | None = None, facts: progress.Facts | None = None) -> dict:
    """The units, stage and decisions of one claimed set (DESIGN.md section 2). `intents` = the unresolved guard intents (read-only marker).
    `facts` (progress.Facts of the whole derivation) adds `unit["progress"]` (DESIGN_PHASE4.md H); derive_one leaves it None."""
    dec, dec_error = decisions.interpret(set_name, (models or {}).get('decisions'), (models or {}).get('decisions_error'))
    model_jobs = (models or {}).get('jobs', {})
    modules = sorted({m for job in jobs for _race, m in job['modules']}, key=lambda m: (MODULE_RANK.get(m, 99), m))
    job_by_id = {job['id']: job for job in jobs}
    pinned = (dec['lod_approval'] or {}).get('units') or {}
    required = required_races(jobs, fronts or {}, dec, races)
    units = []
    for race in required:
        for module in modules:
            key = f'{race}/{module}'
            carriers = [job for job in jobs if (race, module) in job['modules']]
            reduced = {jid: rm[race][module] for jid, rm in model_jobs.items() if race in rm and module in rm[race] and rm[race][module]['reduced']}
            unit = {'race': race, 'module': module, 'job': None, 'jobs': sorted({j['id'] for j in carriers} | set(reduced)),
                    'folder': None, 'lod': None, 'lod_saved': False, 'error': None, 'backend': None, 'model_set': None,
                    'warnings': [], 'review_levels': []}
            pin = (pinned.get(key) or {}).get('job') if isinstance(pinned.get(key), dict) else None
            if race in dec['dropped']:
                unit['status'] = 'dropped'
            elif pin and pin not in reduced:
                # the approved conversion is gone or incomplete: never substitute another mesh; the set can only be sent back to 3D
                unit |= {'status': 'pin_missing', 'job': pin, 'error': f'the approved conversion {pin} is missing or incomplete'}
                if pin in job_by_id:
                    unit['folder'] = job_by_id[pin]['rel']
            elif reduced:
                chosen = pin if pin else max(reduced, key=lambda j: (reduced[j]['mtime'], j))
                entry = reduced[chosen]
                unit |= {'status': 'reduced', 'job': chosen, 'lod': entry['lod'] or DEFAULT_LOD, 'lod_saved': entry['lod'] is not None,
                         'model_set': entry['set'], 'multi': sorted(reduced) if len(reduced) > 1 else None, 'frozen': False}
                if pin:
                    # a 3D-approved unit shows the FROZEN LOD of the approval, never the mutable chosen_lod.json (phase 1 review #12)
                    unit |= {'lod': pinned[key]['lod'], 'lod_saved': True, 'frozen': True}
                if chosen in job_by_id:
                    record = job_by_id[chosen]
                    unit['folder'] = record['rel']
                    unit['backend'] = record['backends'].get(key)
                    unit['warnings'] = list(record['warnings'].get(key, []))
                    unit['review_levels'] = list(record['review_levels'].get(key, []))
                    if key in record['errors']:
                        unit['warnings'].insert(0, record['errors'][key])
            else:
                best = None
                for job in carriers:
                    status = _job_status(job, key)
                    if best is None or STATUS_ORDER.index(status) < STATUS_ORDER.index(best[0]):
                        best = (status, job)
                if best:
                    unit |= {'status': best[0], 'job': best[1]['id'], 'folder': best[1]['rel'], 'error': best[1]['errors'].get(key)}
                else:
                    unit['status'] = 'missing'
            units.append(unit)
    for unit in units:
        job = job_by_id.get(unit['job']) if unit['job'] else None
        unit['progress'] = progress.unit_progress(root, set_name, unit, job, facts) if facts is not None else None
        if job and job.get('problems'):
            unit['warnings'] = list(unit['warnings']) + [p for p in job['problems'] if p not in unit['warnings']]
    live = [u for u in units if u['status'] != 'dropped']
    blocking = [u for u in live if u['status'] not in ('reduced', 'pin_missing')]
    kind = _kind_of(modules)
    unit_builds = _unit_builds(root, set_name, units, dec)
    for unit in units:
        rec = unit_builds.get(f'{unit["race"]}/{unit["module"]}')
        unit['source_changed'] = bool(rec and rec['status'] == builds.SOURCE_CHANGED)
        unit['needs_review'] = bool(rec and rec['status'] == builds.NEEDS_REVIEW)
    counts = builds.counts(unit_builds)
    live_races = [r for r in required if r not in dec['dropped']]
    snaps = bare_snapshots(root, live_races) if dec['lod_approval'] else {}
    bare_state = bare_builds(root, live_races, snaps) if dec['lod_approval'] else {}
    counts['stale_bare'] = sum(1 for rec in bare_state.values() if rec['status'] == 'stale')
    not_completed = []
    if live and not blocking:
        stage = '3d'
        if dec['lod_approval'] and counts['built'] >= counts['total']:          # rule 3: every approved unit's layer build is published (or stale)
            stage = 'aligned'
            if dec['aligned_approval']:
                not_completed = completed_problems(dec, [f'{u["race"]}/{u["module"]}' for u in live], unit_builds, live_races,
                                                   (models or {}).get('ready'), snaps, root)
                if not not_completed:
                    stage = 'completed'
    else:
        stage = 'approved'
    not_converted = [{'race': r, 'reason': 'fronts failed' if (fronts or {}).get(r) == 'failed' else 'never claimed'}
                     for r in races if r not in required]
    recovering = [rec for rec in (intents or []) if guard.intent_set(rec) == set_name]
    return {'name': set_name, 'kind': kind, 'modules': modules, 'stage': stage, 'units': units,
            'builds': unit_builds, 'build_counts': counts, 'bare_snaps': snaps, 'bare_builds': bare_state, 'amount_changed': _amount_changed(dec, kind, [u['race'] for u in live]),
            'decisions': dec, 'decisions_error': dec_error, 'rev': dec['rev'], 'jobs': jobs,
            'required_races': required, 'not_converted': not_converted, 'locked': bool(dec['lod_approval']),
            'races': live_races, 'ready': (models or {}).get('ready'), 'not_completed': not_completed,
            'recovering': [{'id': rec['id'], 'label': rec.get('label'), 'conflict': bool(rec.get('conflict'))} for rec in recovering]}


def derive_all(root: Path) -> dict:
    return _cached('derive', root, lambda: _derive_all(root))


def _scan_errors(jobs: list[dict], models: dict | None) -> list[str]:
    return [job['scan_error'] for job in jobs if job.get('scan_error')] + ([models['scan_error']] if (models or {}).get('scan_error') else [])


def damaged_set(set_name: str, jobs: list[dict], models: dict | None, error: str) -> dict:
    """The info of a set whose scan or derivation failed (phase 4 review F14): visible (Approved tab, with the error as a badge), READ-ONLY
    (`decisions_error` is set, so every write refuses) and skipped by the orchestrator's reconcile (`derive_error`: no task is queued or
    cancelled and no ready.json is revoked on the strength of a broken derivation)."""
    try:
        dec, _dec_error = decisions.interpret(set_name, (models or {}).get('decisions'), (models or {}).get('decisions_error'))
    except Exception:  # noqa: BLE001
        dec, _dec_error = decisions.interpret(set_name, None, None)
    return {'name': set_name, 'kind': 'legacy', 'modules': [], 'stage': 'approved', 'units': [], 'builds': {}, 'build_counts': {},
            'bare_snaps': {}, 'bare_builds': {}, 'amount_changed': False, 'decisions': dec, 'decisions_error': f'the set could not be derived: {error}',
            'derive_error': error, 'rev': dec['rev'], 'jobs': jobs, 'required_races': [], 'not_converted': [], 'locked': bool(dec['lod_approval']),
            'races': [], 'ready': (models or {}).get('ready'), 'not_completed': [], 'recovering': []}


def _derive_or_damaged(root: Path, set_name: str, jobs: list[dict], models: dict | None, races: list[str], fronts: dict | None,
                       intents: list[dict], facts=None) -> dict:
    """derive_set, isolated per set (F14): a scan error of one of its jobs / model folders or an exception of the derivation gives the damaged
    set (with its error) instead of breaking the derivation of every set."""
    errors = _scan_errors(jobs, models)
    if errors:
        return damaged_set(set_name, jobs, models, '; '.join(errors)[:500])
    try:
        return derive_set(root, set_name, jobs, models, races, fronts, intents, facts)
    except Exception as exc:  # noqa: BLE001
        return damaged_set(set_name, jobs, models, f'{type(exc).__name__}: {exc}'[:500])


def _derive_all(root: Path) -> dict:
    races = serve._base_races(root)
    jobs, retired = scan_jobs(root, races)
    models = scan_models(root, races, {name: model_sets(name, jobs[name]) for name in sorted(jobs)})
    fronts = fronts_index(root, races)
    intents = guard.list_intents(root)
    facts = progress.Facts(root, races, lambda prefix: job_set(prefix, races, root))
    sets = {name: _derive_or_damaged(root, name, jobs[name], models.get(name), races, fronts.get(name), intents, facts) for name in sorted(jobs)}
    return {'races': races, 'sets': sets, 'retired': retired}


def derive_one(root: Path, set_name: str) -> dict | None:
    """One set, derived fresh from the folders (no TTL cache): what a write must check against, under the set's lock (a damaged set comes back
    as damaged_set: read-only)."""
    races = serve._base_races(root)
    jobs, _retired = scan_jobs(root, races)
    if set_name not in jobs:
        return None
    models = scan_models(root, races, {set_name: model_sets(set_name, jobs[set_name])})
    return _derive_or_damaged(root, set_name, jobs[set_name], models[set_name], races, fronts_index(root, races).get(set_name),
                              guard.list_intents(root))


# ------------------------------------------------------------------------------------------------------------------ cards

def _unit_rank(unit: dict) -> tuple:
    return (race_rank(unit['race']), MODULE_RANK.get(unit['module'], 99))


def _unit_glb(root: Path, unit: dict) -> Path | None:
    if unit['status'] != 'reduced':
        return None
    return root / 'models' / unit['model_set'] / unit['job'] / unit['race'] / unit['module'] / 'reduced' / f'lod_error_{unit["lod"]}.glb'


def _first_front(info: dict) -> str | None:
    best = None
    for job in info['jobs']:
        for (race, module), images in job['views'].items():
            if images.get('front'):
                rank = (race_rank(race), MODULE_RANK.get(module, 99), job['stage'] != 'completed')
                if best is None or rank < best[0]:
                    best = (rank, images['front'])
    return best[1] if best else None


def _counts(units: list[dict]) -> dict:
    counts = {s: 0 for s in UNIT_STATUSES}
    for unit in units:
        counts[unit['status']] += 1
    return counts


def _alert_units(units: list[dict]) -> int:
    """Units whose job record carries a warning other than the (very common) per-level review list."""
    return sum(1 for u in units if u['warnings'])


def set_card(root: Path, info: dict) -> dict:
    name, stage, units = info['name'], info['stage'], info['units']
    live = [u for u in units if u['status'] != 'dropped']
    counts = _counts(units)
    badges = []
    thumbnail = model = kind = None
    if info.get('derive_error'):
        badges.append({'text': f'damaged: {info["derive_error"][:160]}', 'tone': 'error'})
    elif info['decisions_error']:
        badges.append({'text': 'decisions unreadable', 'tone': 'error'})
    if info.get('recovering'):
        badges.append({'text': 'recovering: read-only', 'tone': 'error'})
    if stage == 'approved':
        badges.append({'text': f'reduced {counts["reduced"]}/{len(live)}', 'tone': 'info'})
        plain = (('converting', 'info'), ('ready', 'info'), ('views', 'info'), ('held', 'warn'), ('failed', 'error'), ('missing', 'warn'),
                 ('pin_missing', 'error'))
        if any(u.get('progress') for u in live):
            # with unit progress (DESIGN_PHASE4.md H) views / converting / held / failed come as `views a/b`, `converting n/m`, `held n`,
            # `not in convert list`, `excluded from convert list`, `failed n`; the plain status counts stay for the rest
            badges += progress.card_badges(units)
            plain = (('ready', 'info'), ('missing', 'warn'), ('pin_missing', 'error'))
        for status, tone in plain:
            if counts[status]:
                badges.append({'text': f'{status.replace("_", " ")} {counts[status]}', 'tone': tone})
    else:
        saved = sum(u['lod_saved'] for u in live)
        badges.append({'text': f'LOD picked {saved}/{len(live)}', 'tone': 'ok' if saved == len(live) else 'info'})
        if counts['pin_missing']:
            badges.append({'text': f'approved conversion missing {counts["pin_missing"]}', 'tone': 'error'})
        if any(u.get('multi') for u in live):
            badges.append({'text': 'several conversions', 'tone': 'warn'})
        first = min((u for u in live if u['status'] == 'reduced'), key=_unit_rank, default=None)
        glb = _unit_glb(root, first) if first else None
        if glb:
            model = serve.rel_url(root, glb)
            thumbnail = serve.fresh_thumb(root, glb)
            kind = 'render' if thumbnail else None
    counts = info.get('build_counts') or {}
    if info['decisions']['lod_approval'] and counts.get('total'):
        if stage == '3d':
            if counts['building']:
                badges.append({'text': f'building {counts["built"]}/{counts["total"]}', 'tone': 'info'})
            if counts['failed']:
                badges.append({'text': f'build failed {counts["failed"]}', 'tone': 'error'})
            if counts['none']:
                badges.append({'text': f'not built {counts["none"]}', 'tone': 'warn'})
        if counts.get('source_changed'):
            badges.append({'text': f'source changed {counts["source_changed"]}', 'tone': 'error'})
        if counts.get('needs_review'):
            badges.append({'text': f'needs 3D review {counts["needs_review"]}', 'tone': 'error'})
        if counts.get('damaged'):
            badges.append({'text': f'damaged build {counts["damaged"]}', 'tone': 'error'})
    if info['decisions']['lod_approval']:
        stale_n = counts.get('stale', 0) + counts.get('stale_bare', 0)           # stale bare layers count too (re-check R14)
        if stale_n:
            badges.append({'text': f'stale {stale_n}', 'tone': 'warn'})
    if info.get('amount_changed'):
        badges.append({'text': 'amount changed since approval', 'tone': 'warn'})
    alerts = _alert_units(live)
    if alerts:
        badges.append({'text': f'warnings {alerts}', 'tone': 'warn'})
    if info['decisions']['dropped']:
        badges.append({'text': 'dropped: ' + ', '.join(sorted(info['decisions']['dropped'])), 'tone': 'warn'})
    if not thumbnail:
        thumbnail = _first_front(info)
        kind = 'fallback' if thumbnail and model else None      # a model exists: the client renders the real thumbnail over it
    return {'id': f'set:{name}', 'type': 'character', 'kind': info['kind'], 'name': name, 'title': pretty(name), 'stage': stage,
            'thumbnail': thumbnail, 'thumbnail_kind': kind, 'model': model, 'races': info['races'], 'badges': badges, 'actions': [],
            'progress': {race: {u['module']: card_progress(u) for u in units if u['race'] == race} for race in sorted({u['race'] for u in units})}
            if stage == 'approved' else None}


def card_progress(unit: dict) -> dict | str:
    """`card.progress[race][module]` (DESIGN_PHASE4.md H.1): the unit's progress record {status, views_done, views_total, held_reason,
    converting_step, error}; just the status text when the derivation carried no progress (a fresh single-set derivation)."""
    p = unit.get('progress')
    if not p:
        return unit['status']
    return {'status': unit['status'], 'views_done': p['views_done'], 'views_total': p['views_total'], 'held_reason': p['held_reason'],
            'converting_step': p['converting_step'], 'error': p['error']}


def _concept_kind(raw: dict) -> str:
    if raw.get('kind') == 'legacy':
        return 'legacy'
    modules = {m for race in raw.get('races', []) for m in race.get('images', {})}
    return 'armour' if modules & set(ARMOUR_MODULES) else 'outfit'


def concept_card(raw: dict, tab: str) -> dict:
    """Card of a serve.py concept-stage entry (set or flat legacy entry)."""
    races = raw.get('races') or []
    legacy = raw.get('kind') == 'legacy'
    best = None
    for race in races:
        for module, url in (race.get('images') or {}).items():
            rank = (race_rank(race['name']), MODULE_RANK.get(module, 99))
            if url and (best is None or rank < best[0]):
                best = (rank, url)
    badges = []
    if raw.get('incomplete'):
        badges.append({'text': 'incomplete', 'tone': 'warn'})
    if raw.get('origin'):
        badges.append({'text': f'from {raw["origin"]}', 'tone': 'info'})
    if tab == 'approved':
        badges.append({'text': 'waiting to be claimed', 'tone': 'info'})
    actions = (LEGACY_ACTIONS if legacy else CONCEPT_ACTIONS).get(tab, [])
    return {'id': raw['id'], 'type': 'character', 'kind': _concept_kind(raw), 'name': raw['name'], 'title': pretty(raw['name']), 'stage': tab,
            'thumbnail': best[1] if best else None, 'thumbnail_kind': None, 'model': None, 'races': sorted(r['name'] for r in races), 'badges': badges,
            'actions': list(actions), 'progress': None}


RECOVERY_KINDS = {'flat_move': 'interrupted move', 'files_tx': 'interrupted write', 'unreadable': 'unreadable record'}


def recovery_card(rec: dict) -> dict:
    """A Trash-tab card for an interrupted multi-step change (guard.py intents). Moves: Complete / Roll back. Writes: Roll back; a write whose
    files were changed again since (`conflict`) needs a manual choice: Complete = keep the files as they are now, Roll back = restore the backups."""
    kind = rec.get('kind')
    conflict = bool(rec.get('conflict'))
    actions = ['complete', 'rollback'] if kind == 'flat_move' or conflict else ['rollback']
    badges = [{'text': RECOVERY_KINDS.get(kind, str(kind)), 'tone': 'warn'}, {'text': str(rec.get('created') or '')[:19], 'tone': 'info'}]
    if conflict:
        badges.insert(0, {'text': 'conflict: choose keep or restore', 'tone': 'error'})
    owner = guard.intent_set(rec)
    if owner:
        badges.append({'text': f'set {owner} read-only', 'tone': 'warn'})
    return {'id': f'intent:{rec["id"]}', 'type': 'character', 'kind': 'recovery', 'name': rec['id'],
            'title': f'Recovery: {rec.get("label") or RECOVERY_KINDS.get(kind, kind)}', 'stage': 'trash', 'thumbnail': None, 'thumbnail_kind': None,
            'model': None, 'races': [], 'badges': badges, 'actions': actions, 'progress': None}


def review_card(raw: dict) -> dict:
    modules = {e.get('module') for e in raw.get('entries', [])}
    model = next((p['model'] for p in raw.get('previews', []) if p.get('model')), None)
    return {'id': raw['id'], 'type': 'character', 'kind': 'armour' if modules & set(ARMOUR_MODULES) else 'outfit', 'name': raw['name'],
            'title': raw['name'], 'stage': 'review', 'thumbnail': raw.get('thumbnail'), 'thumbnail_kind': None, 'model': model,
            'races': sorted((raw.get('previews_by_race') or {})), 'badges': [], 'actions': [], 'progress': None}


def _by_name(cards: list[dict]) -> list[dict]:
    return sorted(cards, key=lambda c: (c['name'], c['id']))


def state(root: Path) -> dict:
    return _cached('state', root, lambda: _build_state(root))


def _build_state(root: Path) -> dict:
    stages = ('concepts', 'provisional', 'approved')
    futures = {
        'derived': POOL.submit(derive_all, root),
        'concepts': {s: POOL.submit(serve.concepts, root, s) for s in stages},
        'archive': POOL.submit(serve.concepts, root, 'archive'),
        'trash': POOL.submit(serve.trash_cards, root),
        'review': POOL.submit(serve.review_cards, root),
        'monsters': POOL.submit(monsters_v2.state_cards, root),     # phase 5: {tab: [card]} of concepts and variants
    }
    derived = futures['derived'].result()
    tabs: dict[str, list[dict]] = {tab: [] for tab in TABS}
    claimed = set(derived['sets'])
    for stage in stages:
        tabs[stage] = [concept_card(c, stage) for c in futures['concepts'][stage].result() if c['name'] not in claimed or c['kind'] == 'legacy']
    tabs['archive'] = [concept_card(c, 'archive') for c in futures['archive'].result()]
    tabs['trash'] = [concept_card(c, 'trash') for c in futures['trash'].result()] + [recovery_card(rec) for rec in guard.list_intents(root)]
    tabs['review'] = [review_card(c) for c in futures['review'].result()]
    for info in derived['sets'].values():
        tabs[info['stage']].append(set_card(root, info))
    for tab, cards in futures['monsters'].result().items():
        tabs[tab].extend(cards)
    return {'races': derived['races'], 'tabs': {tab: _by_name(cards) for tab, cards in tabs.items()}, 'queue': None, 'credits': None, 'paused': None,
            'stale': stale_summary(derived)}


def stale_summary(derived: dict) -> dict:
    """`state.stale` (re-check R14): what the Queue's Rebuild stale would rebuild: {units: n, bare: [race], sets: [set]} over the approved sets
    in 3d / aligned / completed (stale published units, and the stale bare builds of their live races)."""
    units, bare, sets = 0, set(), []
    for name, info in sorted(derived['sets'].items()):
        if not info['locked'] or info['stage'] not in ('3d', 'aligned', 'completed'):
            continue
        n = (info.get('build_counts') or {}).get('stale', 0)
        races = {race for race, rec in (info.get('bare_builds') or {}).items() if rec['status'] == 'stale'}
        if n or races:
            units += n
            bare |= races
            sets.append(name)
    return {'units': units, 'bare': sorted(bare), 'sets': sets}


# ------------------------------------------------------------------------------------------------------------------- item

class NotFound(Exception):
    pass


def _lods_of(root: Path, unit: dict) -> dict:
    module = root / 'models' / unit['model_set'] / unit['job'] / unit['race'] / unit['module']
    levels = (read_json(module / 'reduced' / 'manifest.json').get('levels') or {})
    out = {}
    for lv in LODS:
        entry = levels.get(lv) if isinstance(levels.get(lv), dict) else {}
        faces = entry.get('triangles')
        out[lv] = {'url': serve.rel_url(root, module / 'reduced' / f'lod_error_{lv}.glb'), 'faces': faces if isinstance(faces, int) else None}
    return out


def set_item(root: Path, set_name: str) -> dict:
    info = derive_all(root)['sets'].get(set_name)
    if not info:
        raise NotFound(f'Unknown set {set_name}')
    return set_item_of(root, info)


def set_item_of(root: Path, info: dict) -> dict:
    set_name = info['name']
    jobs = {job['id']: job for job in info['jobs']}
    units = []
    for unit in info['units']:
        job = jobs.get(unit['job'])
        views = {}
        if job:
            views = job['views'].get((unit['race'], unit['module']), {})
        units.append({'race': unit['race'], 'module': unit['module'], 'status': unit['status'], 'job': unit['job'], 'jobs': unit['jobs'],
                      'folder': unit['folder'], 'views': views,
                      'lods': _lods_of(root, unit) if unit['status'] == 'reduced' else {},
                      'lod': unit['lod'], 'lod_saved': unit['lod_saved'], 'frozen': bool(unit.get('frozen')),
                      'source_changed': bool(unit.get('source_changed')), 'needs_review': bool(unit.get('needs_review')),
                      'error': unit['error'], 'backend': unit['backend'],
                      'model_set': unit['model_set'], 'warnings': unit['warnings'], 'review_levels': unit['review_levels'],
                      'progress': unit.get('progress')})
    card = set_card(root, info)
    snaps = info.get('bare_snaps') or {}
    # the bare URLs and the bare build ids come from the SAME pointer read of this derivation (re-check R2)
    bare = {race: builds.slot_urls(root, builds.bare_root(root) / race, snaps[race][0]) for race in info['races'] if race in snaps} if info['locked'] else {}
    bare_state = (info.get('bare_builds') or {}) if info['locked'] else {}
    return {'id': f'set:{set_name}', 'name': set_name, 'kind': info['kind'], 'stage': info['stage'], 'rev': info['rev'],
            'races': info['races'], 'required_races': info['required_races'], 'not_converted': info['not_converted'],
            'modules': info['modules'], 'locked': info['locked'], 'error': info['decisions_error'], 'units': units,
            'decisions': info['decisions'], 'builds': info['builds'], 'bare': bare, 'bare_builds': bare_state,
            'not_completed': info.get('not_completed') or [], 'recovering': info.get('recovering') or [], 'card': card}


def layers_for_race(root: Path, race: str) -> dict:
    """GET /api/v2/layers?race=R: the bare layers of the race and every published build of it in sets in 3d / aligned / completed
    (`amount` = the set's saved Amount of the group of the slot: torso -> body, lower_* -> limbs, outfit -> outfit; default 100)."""
    derived = derive_all(root)
    if race not in derived['races']:
        raise NotFound(f'Unknown race {race}')
    layers = []
    for name, info in sorted(derived['sets'].items()):
        if info['stage'] not in ('3d', 'aligned', 'completed') or not info['locked']:
            continue
        title = pretty(name)
        for key, rec in sorted(info['builds'].items()):
            unit_race, module = key.split('/', 1)
            if unit_race != race or rec['status'] not in builds.BUILT:
                continue
            for slot, url in sorted(rec['slots'].items()):
                layers.append({'set': name, 'title': title, 'kind': info['kind'], 'module': module, 'slot': slot, 'url': url, 'stage': info['stage'],
                               'build_id': rec['build_id'], 'amount': amount_of(info['decisions'], race, SLOT_GROUP.get(slot, 'outfit'))})
    return {'race': race, 'bare': builds.bare_slots(root, race), 'layers': layers}


def _concept_raw(root: Path, item_id: str) -> dict:
    parts = item_id.split(':')
    if not all(NAME.fullmatch(p) for p in parts):
        raise ValueError('Invalid ID')
    kind = parts[0]
    if len(parts) == 2 and kind in ('concepts', 'provisional', 'approved', 'archive', 'trash'):
        folder = {'trash': 'trash/concepts', 'archive': serve.ARCHIVE}.get(kind, kind)
        path = serve.direct(root, folder, parts[1])
        raw = serve.set_card(root, path, kind) if path.is_dir() else None
        if not raw:
            raise NotFound(f'Unknown {kind} set {parts[1]}')
        return raw | ({'origin': read_json(path / '.preview-origin.json').get('src')} if kind == 'trash' else {})
    if len(parts) == 3 and kind in ('legacy', 'archlegacy', 'trashlegacy'):
        tab = {'legacy': 'concepts', 'archlegacy': 'archive', 'trashlegacy': 'trash'}[kind]
        source = serve.trash_cards(root) if tab == 'trash' else serve.concepts(root, tab)
        for raw in source:
            if raw['id'] == item_id:
                return raw
    raise NotFound(f'Unknown item {item_id}')


def item(root: Path, item_id: str) -> dict:
    if not isinstance(item_id, str) or not item_id:
        raise ValueError('Missing id')
    if item_id.startswith('set:'):
        return set_item(root, serve.safe_name(item_id.removeprefix('set:')))
    if item_id.startswith(('monster:', 'mconcept:')):
        return monsters_v2.item(root, item_id)                   # unknown ids: monsters_v2.not_found = NotFound (404)
    if item_id.startswith('intent:'):
        intent_id = item_id.removeprefix('intent:')
        try:
            rec = guard.read_intent(root, serve.safe_name(intent_id)) | {'id': intent_id}
        except FileNotFoundError as exc:
            raise NotFound(str(exc)) from None
        card = recovery_card(rec)
        files = rec.get('files') or []
        facts = {'kind': rec.get('kind'), 'label': rec.get('label'), 'created': rec.get('created'), 'files': len(files),
                 'verb': rec.get('verb'), 'approved_set': rec.get('approved_set'), 'set': rec.get('set'), 'conflict': rec.get('conflict'),
                 'conflict_files': rec.get('conflict_files')}
        facts = {k: v for k, v in facts.items() if v not in (None, '')}
        return card | {'detail': {'races': [{'name': 'recovery', 'images': {}, 'info': {'intent': facts}}]}}
    if item_id.startswith('review:'):
        for raw in serve.review_cards(root):
            if raw['id'] == item_id:
                return raw | {'stage': 'review', 'card': review_card(raw)}
        raise NotFound(f'Unknown review list {item_id}')
    raw = _concept_raw(root, item_id)
    tab = item_id.split(':')[0]
    tab = {'legacy': 'concepts', 'archlegacy': 'archive', 'trashlegacy': 'trash'}.get(tab, tab)
    card = concept_card(raw, tab)
    return card | {'detail': {'races': raw.get('races', [])}}
