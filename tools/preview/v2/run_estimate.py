"""What "Run everything now" would cost, for the Queue tab's mode section (GET /api/v2/queue `run_all_estimate`).

    scan(root)                                   the slow, read-only part: counts of modules / jobs / monster tasks (never writes anything)
    build(scan, tripo, hyper3d)                  the cheap part: backend split and credits from a scan plus the CURRENT setting and balances
    get(root, credits, wait=False)               what the endpoint returns: the cached scan (TTL, refreshed in the background) built with `credits`

Nothing is invented here: the modules come from `convert_list.eligible_jobs` (the convert worker's own predicate: convert list, `!` excludes,
fewer than MAX_TRIES failures, owner check, stages processing / multiview_ready, a free module) and `convert_list.coming_jobs` (the same list
and owner rules for jobs whose views are not finished yet, `processing_views/`); the backend split follows `tools/convert_queue.py`:
  - Queue setting `tripo` = `off`: the app starts the worker with `--no-tripo`, every module goes to Hyper3D;
  - `tripo` = `pixal3d`: the app starts the worker with `--backend pixal3d`; every module is generated locally (0 credits, about
    PIXAL3D_SECONDS_DEFAULT s each, or `time_estimate_s` of tools/pixal3d_pipeline/config.json when it has one); the Tripo balance is not read;
  - `tripo` = `auto`: Tripo while `(balance - reserve) // TRIPO_COST` covers modules, then Hyper3D for the rest. `TRIPO_COST` is read from
    convert_queue.py; the app starts the worker without `--reserve` (services.Service.command), so the reserve is its default 0. A balance the
    app has not read is unknown: no split is guessed, the estimate gives the range (all Hyper3D .. all Tripo);
  - `tripo` = `auto_pixal3d` (`--fallback pixal3d`): the same Tripo split, but the rest goes to Pixal3D at 0 credits instead of Hyper3D;
prices: Tripo = TRIPO_COST per module, Hyper3D = the sum of `expected_costs` of tools/hyper3d_pipeline/config.json (the default path), monsters
= monster_lane.CREDITS_PER_MODEL per queued monster_convert task that is still wanted (a failed task needs Retry; it is counted, not priced).

The scan walks the job folders and the task queue, so `get` never runs it on the request thread: the first answer is `pending`, afterwards the
last scan is served (marked `stale` once older than CACHE_TTL) and a new scan starts in the background. Any error is returned as `error`.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parents[1]
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import convert_list  # noqa: E402

CACHE_TTL = 60.0                  # seconds a scan is served before a background rescan starts
TRIPO_RESERVE = 0                 # convert_queue's `--reserve` default: the app starts the worker without it (services.Service.command)
HYPER3D_CONFIG = TOOLS / 'hyper3d_pipeline' / 'config.json'
PIXAL3D_CONFIG = TOOLS / 'pixal3d_pipeline' / 'config.json'
PIXAL3D_SECONDS_DEFAULT = 270     # about 4.5 min of local GPU time per module (tools/pixal3d_pipeline/README.md), unless the config says otherwise
MODES = ('off', 'auto', 'pixal3d', 'auto_pixal3d')      # settings.TRIPO_MODES
_STATE: dict[str, dict] = {}      # root -> {scan, at (monotonic), thread}
_LOCK = threading.Lock()


def _iso(now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc).isoformat(timespec='seconds')


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# ------------------------------------------------------------------------------------------------------------------------ prices

def tripo_cost() -> int | None:
    """`convert_queue.TRIPO_COST` (imported through the convert list's lazy import; the source line when it cannot be imported)."""
    convert_list.worker_predicate()                  # imports tools/convert_queue.py when it can be imported
    module = sys.modules.get('convert_queue')
    value = getattr(module, 'TRIPO_COST', None)
    if _number(value):
        return int(value)
    try:
        found = re.search(r'^TRIPO_COST\s*=\s*(\d+)', (TOOLS / 'convert_queue.py').read_text(encoding='utf-8'), re.M)
    except OSError:
        return None
    return int(found.group(1)) if found else None


def hyper3d_cost() -> float | None:
    """Hyper3D credits per module on the default path: the sum of `expected_costs` in tools/hyper3d_pipeline/config.json (0.5 geometry + 0 + 0)."""
    try:
        costs = json.loads(HYPER3D_CONFIG.read_text(encoding='utf-8-sig'))['expected_costs']
        values = [v for v in costs.values() if _number(v)]
    except (OSError, ValueError, KeyError, AttributeError):
        return None
    return round(sum(values), 6) if values and len(values) == len(costs) else None


def pixal3d_seconds() -> int:
    """Seconds of local GPU time per Pixal3D module: `time_estimate_s` of tools/pixal3d_pipeline/config.json when it has a number, else the constant."""
    try:
        value = json.loads(PIXAL3D_CONFIG.read_text(encoding='utf-8-sig')).get('time_estimate_s')
    except (OSError, ValueError, AttributeError):
        return PIXAL3D_SECONDS_DEFAULT
    return int(value) if _number(value) and value > 0 else PIXAL3D_SECONDS_DEFAULT


def monster_cost() -> float:
    import monster_lane
    return monster_lane.CREDITS_PER_MODEL


# ------------------------------------------------------------------------------------------------------------------------- scan

def scan_monsters(root) -> dict:
    """{queued, failed_retryable}: queued monster_convert tasks that are still wanted and not damaged (what Run everything now would start:
    monster_lane.plan_starts takes queued rows only), and failed ones that could be retried by hand (they need Retry; never priced)."""
    import monster_lane
    queued = failed = 0
    for task in monster_lane.lane_tasks(root, state='queued'):
        check = monster_lane.task_check(root, task)
        if check['wanted'] and not check['damaged']:
            queued += 1
    for task in monster_lane.lane_tasks(root, state='failed'):
        check = monster_lane.task_check(root, task)
        if check['wanted'] and not check['damaged']:
            failed += 1
    return {'queued': queued, 'failed_retryable': failed}


def scan(root) -> dict:
    """The slow part, read-only: {computed_at, ready: {jobs, modules}, coming: {jobs, modules}, monsters: {...}, errors: [...]}. A part that
    fails is None and named in `errors`; the others stay."""
    root = Path(root)
    out: dict = {'computed_at': None, 'ready': None, 'coming': None, 'monsters': None, 'errors': [], 'costs': None}
    try:
        out['costs'] = {'tripo': tripo_cost(), 'hyper3d': hyper3d_cost(), 'monster': monster_cost(), 'pixal3d_seconds': pixal3d_seconds()}   # (imports convert_queue: not on a request)
    except Exception as exc:  # noqa: BLE001
        out['errors'].append(f'prices: {type(exc).__name__}: {exc}')
    for key, work in (('ready', lambda: _group(convert_list.eligible_jobs(root), 'free')),
                      ('coming', lambda: _group(convert_list.coming_jobs(root), 'modules_left')),
                      ('monsters', lambda: scan_monsters(root))):
        try:
            out[key] = work()
        except Exception as exc:  # noqa: BLE001  (an estimate never fails a request)
            out['errors'].append(f'{key}: {type(exc).__name__}: {exc}')
    out['computed_at'] = _iso()
    return out


def _group(jobs: list[dict], field: str) -> dict:
    return {'jobs': len(jobs), 'modules': sum(int(j.get(field) or 0) for j in jobs)}


# ------------------------------------------------------------------------------------------------------------------------- build

def _split(modules: int, slots: int | None) -> tuple[int | None, int | None]:
    """(tripo modules, fallback modules) of `modules` when Tripo can take `slots` more (None = unknown balance). The fallback is Hyper3D
    (`off`, `auto`) or Pixal3D (`pixal3d`, `auto_pixal3d`)."""
    if slots is None:
        return None, None
    tripo = min(modules, slots)
    return tripo, modules - tripo


def _money(value) -> float | None:
    return None if value is None else round(value, 3)


def build(scan_: dict | None, tripo: dict | None, hyper3d: dict | None, stale: bool = False, costs: dict | None = None) -> dict:
    """The endpoint's `run_all_estimate` from a scan, the credits snapshot (`credits.tripo_state` / `hyper3d_state`) and the prices."""
    tripo, hyper3d = tripo or {}, hyper3d or {}
    costs = costs or (scan_ or {}).get('costs') or {'tripo': None, 'hyper3d': None, 'monster': None}
    mode = tripo.get('mode') if tripo.get('mode') in MODES else 'off'
    pixal = mode in ('pixal3d', 'auto_pixal3d')            # the modules Tripo does not take are generated locally, for free
    uses_tripo = mode in ('auto', 'auto_pixal3d')
    p_seconds = costs.get('pixal3d_seconds') or PIXAL3D_SECONDS_DEFAULT
    balance = tripo.get('credits') if _number(tripo.get('credits')) else None
    out: dict = {
        'pending': scan_ is None, 'stale': bool(stale), 'computed_at': (scan_ or {}).get('computed_at'), 'error': None, 'tripo_mode': mode,
        'costs': {'tripo_per_module': costs['tripo'], 'hyper3d_per_module': costs['hyper3d'], 'monster_per_task': costs['monster'],
                  'tripo_reserve': TRIPO_RESERVE, 'pixal3d_per_module': 0, 'pixal3d_seconds_per_module': p_seconds},
        'balances': {'tripo': {'credits': balance, 'checked_at': tripo.get('checked_at'), 'known': balance is not None},
                     'hyper3d': {'credits': hyper3d.get('credits'), 'at': hyper3d.get('at')}},
        'tripo_balance_known': True if not uses_tripo else balance is not None,
        'ready': None, 'coming': None, 'monsters': None, 'total': None}
    if scan_ is None:
        return out
    problems = list(scan_.get('errors') or [])
    if costs['hyper3d'] is None and not problems and not pixal:
        problems.append('the Hyper3D price per module cannot be read from tools/hyper3d_pipeline/config.json')
    if uses_tripo and costs['tripo'] is None:
        problems.append('the Tripo price per module (TRIPO_COST) cannot be read from tools/convert_queue.py')
    out['error'] = '; '.join(problems) or None
    t_cost = costs['tripo']
    h_cost = 0 if pixal else costs['hyper3d']            # the credits of one fallback module: Pixal3D is free
    if mode == 'off':
        slots: int | None = 0
    elif mode == 'pixal3d':
        slots = 0
    elif balance is None or t_cost is None:
        slots = None
    else:
        slots = max(0, int((balance - TRIPO_RESERVE) // t_cost))
    split_known = slots is not None
    t_sum = h_sum = 0.0
    low = high = 0.0              # the range while the Tripo balance is unknown: all Hyper3D .. all Tripo
    modules_total = pixal_modules = 0
    for key in ('ready', 'coming'):
        part = scan_.get(key)
        if part is None:
            continue
        n = part['modules']
        t_mod, h_mod = _split(n, slots)
        if slots is not None and t_mod is not None:
            slots -= t_mod                                   # the ready modules are converted first; "coming" ones take what is left
        entry = {'jobs': part['jobs'], 'modules': n, 'tripo_modules': t_mod, 'hyper3d_modules': None if pixal else h_mod,
                 'pixal3d_modules': h_mod if pixal else 0, 'pixal3d_seconds': h_mod * p_seconds if pixal and h_mod is not None else None,
                 'tripo_credits': None, 'hyper3d_credits': None, 'credits': None}
        if h_cost is not None and split_known:
            entry['tripo_credits'] = _money(t_mod * (t_cost or 0))
            entry['hyper3d_credits'] = _money(h_mod * h_cost)
            entry['credits'] = _money(entry['tripo_credits'] + entry['hyper3d_credits'])
            t_sum += entry['tripo_credits']
            h_sum += entry['hyper3d_credits']
        elif h_cost is not None and t_cost is not None:
            low += n * h_cost
            high += n * t_cost
        modules_total += n
        if pixal and h_mod is not None:
            pixal_modules += h_mod
        out[key] = entry
    monsters = scan_.get('monsters')
    if monsters is not None:
        m_credits = _money(monsters['queued'] * costs['monster']) if costs['monster'] is not None else None
        out['monsters'] = {'queued': monsters['queued'], 'failed_retryable': monsters['failed_retryable'], 'credits': m_credits}
        h_sum += m_credits or 0
        low += m_credits or 0
        high += m_credits or 0
    total = {'modules': modules_total, 'tripo_credits': None, 'hyper3d_credits': None, 'credits': None, 'min': None, 'max': None,
             'pixal3d_modules': pixal_modules if split_known else None,
             'pixal3d_seconds': pixal_modules * p_seconds if pixal and split_known else None}
    if h_cost is not None and split_known:
        total.update(tripo_credits=_money(t_sum), hyper3d_credits=_money(h_sum), credits=_money(t_sum + h_sum))
        total['min'] = total['max'] = total['credits']
    elif h_cost is not None and t_cost is not None:
        total.update(min=_money(low), max=_money(high))
    out['total'] = total
    return out


# ------------------------------------------------------------------------------------------------------------------------- cache

def _key(root) -> str:
    return str(Path(root).resolve())


def _worker(root, key: str) -> None:
    try:
        result = scan(root)
    except Exception as exc:  # noqa: BLE001
        result = {'computed_at': _iso(), 'ready': None, 'coming': None, 'monsters': None, 'costs': None,
                  'errors': [f'scan: {type(exc).__name__}: {exc}']}
    with _LOCK:
        entry = _STATE.setdefault(key, {'scan': None, 'at': 0.0, 'thread': None})
        entry.update(scan=result, at=time.monotonic(), thread=None)


def get(root, credits: dict | None = None, wait: bool = False, ttl: float | None = None) -> dict:
    """`run_all_estimate` for GET /api/v2/queue. Never scans on the caller's thread unless `wait` (tests): without a scan yet the answer is
    `pending`; a scan older than the TTL is served as `stale` while a new one runs in the background. `credits` = {tripo, hyper3d} (the queue
    payload's `credits`); the split and the totals use its CURRENT values on every call."""
    ttl = CACHE_TTL if ttl is None else ttl
    key = _key(root)
    with _LOCK:
        entry = _STATE.setdefault(key, {'scan': None, 'at': 0.0, 'thread': None})
        fresh = entry['scan'] is not None and time.monotonic() - entry['at'] < ttl
        if not fresh and entry['thread'] is None:
            entry['thread'] = threading.Thread(target=_worker, args=(Path(root), key), name='run-estimate', daemon=True)
            entry['thread'].start()
        thread = entry['thread']
    if wait and thread is not None:
        thread.join()
    with _LOCK:
        entry = _STATE[key]
        have, age = entry['scan'], time.monotonic() - entry['at']
    credits = credits or {}
    return build(have, credits.get('tripo'), credits.get('hyper3d'), stale=have is not None and age >= ttl)


def forget(root=None) -> None:
    """Drop the cached scan of a root (all roots without one); tests."""
    with _LOCK:
        if root is None:
            _STATE.clear()
        else:
            _STATE.pop(_key(root), None)
