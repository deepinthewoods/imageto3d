"""Layer-build facts for the v2 preview app (DESIGN_PHASE3.md section A): which builds are published, which are stale, the URLs of their
slots, the bare layers of a race and the fingerprint (cached by input mtimes). Read-only apart from `stale.json`, which only the
orchestrator writes (through `write_stale`).

`tools/layers/build.py` (the builder; stdlib only at import time) is loaded BY PATH under the name `layers_build`: a plain `import build`
could be shadowed by an unrelated installed package, and `tools/layers` would otherwise have to be put on sys.path of the server.

A pointer counts as PUBLISHED only when (phase 3 review F4): it is ok, names this exact module (repo-relative path equality, race, kind, LOD), its
required list and slot map are EXACTLY `build.required_slots(kind, race)`, it is byte-for-byte the immutable `<build_id>/build.json` it names, the
build folder is a real folder directly in the output root, every slot path is `<build_id>/<slot>.glb`, resolves inside that folder and exists, AND
the builder's own full validator passes (`build.validate_build`: every GLB parsed whole, faces, slot JSONs, and the per-slot `size` / `sha256` the
builder recorded at publication; phase 3 re-check R3). The validation is cached by the (mtime, size, inode) of build.json and every slot file;
certification (`verify_bytes`) rehashes the slot bytes uncached. A damaged pointer is not published (status `damaged` when a pointer exists); the
builder repairs it from build.json under its publish lock (status `current`) or builds a fresh generation.

Bare layers live under the PROJECT ROOT (`<root>/tools/preview/cache/layers/<race>/`, re-check R13): a fixture root never reads or writes the real
repository's cache, and the dispatcher passes that folder to the builder as `--out`.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import threading
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import guard  # noqa: E402
import settings as app_settings  # noqa: E402

REPO = HERE.parents[2]
BUILD_PY = REPO / 'tools' / 'layers' / 'build.py'
STALE_FILE = 'stale.json'
ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
_BUILD = None
_LOAD_LOCK = threading.Lock()


def lb():
    """The loaded tools/layers/build.py module."""
    global _BUILD
    with _LOAD_LOCK:
        if _BUILD is None:
            if 'layers_build' in sys.modules:
                _BUILD = sys.modules['layers_build']
            else:
                spec = importlib.util.spec_from_file_location('layers_build', BUILD_PY)
                module = importlib.util.module_from_spec(spec)
                sys.modules['layers_build'] = module
                spec.loader.exec_module(module)
                _BUILD = module
        return _BUILD


BARE_REL = Path('tools') / 'preview' / 'cache' / 'layers'


def bare_root(root: Path) -> Path:
    """`<root>/tools/preview/cache/layers`: the bare layers of a project root (re-check R13; on the real root the builder's default BARE_ROOT)."""
    return Path(root) / BARE_REL


# ------------------------------------------------------------------------------------------------------------------ json cache

_JSON: dict[str, tuple[tuple, object]] = {}
_JSON_LOCK = threading.Lock()


def read_json(path: Path) -> dict | None:
    """The JSON object in `path` (None when absent / unreadable / not an object), re-read only when (mtime, size) changed."""
    key = str(path)
    try:
        st = os.stat(path)
    except OSError:
        with _JSON_LOCK:
            _JSON.pop(key, None)
        return None
    stamp = (st.st_mtime_ns, st.st_size)
    with _JSON_LOCK:
        hit = _JSON.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    value = value if isinstance(value, dict) else None
    with _JSON_LOCK:
        _JSON[key] = (stamp, value)
    return value


# --------------------------------------------------------------------------------------------------------------------- urls

def file_url(root: Path, path: Path) -> str:
    """`/files/` URL of a built file relative to the project root (ValueError outside it: no other root is ever read, re-check R13)."""
    try:
        return '/files/' + quote(Path(path).relative_to(Path(root)).as_posix(), safe='/')
    except ValueError:
        raise ValueError(f'{path} is outside the project') from None


def rel_path(root: Path, path: Path) -> str:
    """The RESOLVED path relative to the project root (posix). ValueError for anything outside it (never an absolute path)."""
    path = Path(path).resolve()
    try:
        return path.relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        raise ValueError(f'{path} is outside the project') from None


def contained(path: Path, folder: Path) -> Path:
    """`path` resolved; ValueError unless it is inside `folder` (resolved) and not a link."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f'{path} is a link')
    real, base = path.resolve(), Path(folder).resolve()
    if real != base and base not in real.parents:
        raise ValueError(f'{path} resolves outside {folder}')
    return real


# ------------------------------------------------------------------------------------------------------------------ pointers

def module_dir(root: Path, model_set: str, job: str, race: str, module: str) -> Path:
    return Path(root) / 'models' / model_set / job / race / module


NO_POINTER = 'no pointer'
_VALID: dict[tuple, tuple[tuple, list]] = {}
_VALID_LOCK = threading.Lock()


def _validation(out_root: Path, build_id: str, ptr: dict, expect: dict, fresh: bool) -> list[str]:
    """`build.validate_build` of the folder the pointer names (digests included), cached by the stamps of build.json and every slot file."""
    folder = Path(out_root) / build_id
    files = [folder / 'build.json'] + [folder / f'{slot}.{ext}' for slot in sorted(ptr.get('slots') or {}) for ext in ('glb', 'json')]
    stamps = tuple(_stamp(f) for f in files)
    key = (str(folder), tuple(sorted(expect.items(), key=lambda kv: kv[0])))
    if not fresh:
        with _VALID_LOCK:
            hit = _VALID.get(key)
            if hit and hit[0] == stamps:
                return hit[1]
    problems = lb().validate_build(folder, expect_id=build_id, expect=expect, name=build_id)
    with _VALID_LOCK:
        _VALID[key] = (stamps, problems)
    return problems


def pointer_problem(out_root: Path, ptr: dict | None, expect: dict, fresh: bool = False) -> str | None:
    """Why a pointer does not name a published build (None = it does; see the module doc). `expect` = {kind, race, module, lod}. `fresh` skips the
    validation cache (the dispatcher's verdict check)."""
    if not isinstance(ptr, dict) or ptr.get('status') != 'ok':
        return NO_POINTER
    build_id = ptr.get('build_id')
    if not isinstance(build_id, str) or not ID_RE.fullmatch(build_id):
        return 'bad build id'
    for key, value in expect.items():
        if ptr.get(key) != value:
            return f'other {key}'
    try:
        want = sorted(lb().required_slots(expect['kind'], expect['race']))
    except ValueError as exc:
        return f'bad contract ({exc})'
    required, slots = ptr.get('required'), ptr.get('slots')
    if not isinstance(required, list) or sorted(map(str, required)) != want or not isinstance(slots, dict) or sorted(slots) != want:
        return 'bad contract'
    folder = Path(out_root) / build_id
    try:
        if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != Path(out_root).resolve():
            return 'build folder missing or linked'
    except OSError:
        return 'build folder unreadable'
    if read_json(folder / 'build.json') != ptr:
        return 'pointer differs from build.json'
    for slot in want:
        entry = slots.get(slot)
        if not isinstance(entry, dict) or entry.get('glb') != f'{build_id}/{slot}.glb':
            return f'slot {slot} path'
        try:
            glb = contained(Path(out_root) / entry['glb'], folder)
        except (ValueError, OSError):
            return f'slot {slot} outside its build'
        if not glb.is_file():
            return f'slot {slot} missing'
    problems = _validation(out_root, build_id, ptr, expect, fresh)
    if problems:
        return 'invalid build: ' + '; '.join(problems[:3])
    return None


def verify_bytes(out_root: Path, ptr: dict) -> list[str]:
    """Certification (Aligned approval, ready.json, re-check R3): every slot GLB of the publication still has exactly the size and sha256 the builder
    recorded, rehashed now (uncached). Problems, empty = the bytes are the published ones."""
    out = []
    folder = Path(out_root) / str(ptr.get('build_id'))
    for slot, entry in sorted((ptr.get('slots') or {}).items()):
        if not isinstance(entry, dict):
            out.append(f'slot {slot}: no entry')
            continue
        problem = lb().slot_digest_problem(folder / f'{slot}.glb', entry)
        if problem:
            out.append(f'slot {slot}: {problem}')
    return out


def snapshot(out_root: Path, pointer_name: str, expect: dict, fresh: bool = False) -> tuple[dict | None, str | None]:
    """ONE read of a pointer (re-check R2): (the pointer when it names a valid publication else None, the problem: None, NO_POINTER or why the
    existing pointer is not a valid publication). URLs and ids shown together must come from the same snapshot."""
    ptr = read_json(Path(out_root) / pointer_name)
    problem = pointer_problem(out_root, ptr, expect, fresh)
    return (None if problem else ptr), problem


def module_expect(module: Path, lod: str) -> dict:
    """What a pointer of the module dir `module` at `lod` must say: the builder's own identity of it (repo-relative path, else absolute posix)."""
    module = Path(module)
    return {'kind': module.name, 'race': module.parent.name, 'module': lb()._rel(module), 'lod': lod}


def module_snapshot(module: Path, lod: str, fresh: bool = False) -> tuple[dict | None, str | None]:
    """`snapshot` of the module dir's `layers/current_<lod>.json`."""
    return snapshot(Path(module) / 'layers', f'current_{lod}.json', module_expect(module, lod), fresh)


def pointer_at(module: Path, lod: str, fresh: bool = False) -> dict | None:
    """The published build of the module dir `module` at `lod` (`layers/current_<lod>.json`), or None."""
    return module_snapshot(module, lod, fresh)[0]


def module_pointer(root: Path, model_set: str, job: str, race: str, module: str, lod: str) -> dict | None:
    """The published build of a module at `lod`, only when it passes `pointer_problem` (a copied or foreign pointer never counts)."""
    return pointer_at(module_dir(root, model_set, job, race, module), lod)


def bare_expect(race: str) -> dict:
    return {'kind': 'bare', 'race': race, 'module': f'bare:{race}', 'lod': None}


def bare_snapshot(root: Path, race: str, fresh: bool = False) -> tuple[dict | None, str | None]:
    """`snapshot` of `<root>/tools/preview/cache/layers/<race>/current_bare.json`."""
    return snapshot(bare_root(root) / race, 'current_bare.json', bare_expect(race), fresh)


def bare_pointer(root: Path, race: str, fresh: bool = False) -> dict | None:
    return bare_snapshot(root, race, fresh)[0]


def slot_urls(root: Path, out_root: Path, ptr: dict | None) -> dict:
    """`{slot: url}` of a pointer snapshot (empty without one)."""
    if not ptr:
        return {}
    return {slot: file_url(root, Path(out_root) / entry['glb']) for slot, entry in ptr['slots'].items()
            if isinstance(entry, dict) and isinstance(entry.get('glb'), str)}


def bare_slots(root: Path, race: str) -> dict:
    """`{slot: url}` of the current bare build of a race (empty when there is none), from one pointer read."""
    return slot_urls(root, bare_root(root) / race, bare_pointer(root, race))


# --------------------------------------------------------------------------------------------------------------------- stale

def stale_path(root: Path) -> Path:
    return Path(root) / guard.APP / STALE_FILE


def stale_state(root: Path) -> dict:
    """`{"time", "units": {"S/race/module": {...}}, "bare": {race: {...}}}` written by the orchestrator's stale scan (empty when never scanned)."""
    data = read_json(stale_path(root)) or {}
    return {'time': data.get('time'), 'units': data.get('units') if isinstance(data.get('units'), dict) else {},
            'bare': data.get('bare') if isinstance(data.get('bare'), dict) else {}}


def write_stale(root: Path, data: dict) -> None:
    guard.write_atomic(stale_path(root), json.dumps(data, indent=1).encode('utf-8'))


# ----------------------------------------------------------------------------------------------------------------- fingerprint

_FP: dict[tuple, tuple[tuple, str]] = {}
_FP_LOCK = threading.Lock()


def _stamp(path: Path) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _inputs_module(module: Path, lod: str) -> list[Path]:
    build = lb()
    race = module.resolve().parts[-2]
    red = module / 'reduced'
    return ([Path(build.ROOT) / rel for rel in build.CODE_DEPS] +
            [Path(build.BASE_CACHE) / f'{race}.npz', Path(build.BASE_CACHE) / f'{race}_rig.npz', module / 'fit' / 'align.json',
             red / f'lod_error_{lod}.glb', red / f'lod_error_{lod}_colors.npz', red / 'manifest.json'])


def fingerprint_module(module: Path, lod: str) -> str:
    """`build.fingerprint_module`, cached by the mtimes / sizes of every file it hashes (the hashing reads several MB per call)."""
    module = Path(module).resolve()
    stamps = tuple(_stamp(p) for p in _inputs_module(module, lod))
    key = ('module', str(module), lod)
    with _FP_LOCK:
        hit = _FP.get(key)
        if hit and hit[0] == stamps:
            return hit[1]
    fp = lb().fingerprint_module(module, lod)
    with _FP_LOCK:
        _FP[key] = (stamps, fp)
    return fp


def fingerprint_bare(race: str) -> str:
    build = lb()
    stamps = tuple(_stamp(p) for p in [Path(build.ROOT) / rel for rel in build.CODE_DEPS] +
                   [Path(build.BASE_CACHE) / f'{race}.npz', Path(build.BASE_CACHE) / f'{race}_rig.npz'])
    key = ('bare', race)
    with _FP_LOCK:
        hit = _FP.get(key)
        if hit and hit[0] == stamps:
            return hit[1]
    fp = build.fingerprint_bare(race)
    with _FP_LOCK:
        _FP[key] = (stamps, fp)
    return fp


def body_sha12(race: str) -> str:
    """First 12 hex digits of the sha256 of the race's game body cache (`tools/preview/cache/bases/<race>.npz`): the key of a bare task."""
    import hashlib
    path = Path(lb().BASE_CACHE) / f'{race}.npz'
    stamp = _stamp(path)
    key = ('body', race)
    with _FP_LOCK:
        hit = _FP.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        digest = 'absent'
    with _FP_LOCK:
        _FP[key] = (stamp, digest)
    return digest


def file_sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def cached_sha256(path: Path) -> str | None:
    """file_sha256 cached by (mtime, size, inode); None when the file is missing or unreadable."""
    stamp = _stamp(path)
    if stamp is None:
        return None
    key = ('sha', str(path))
    with _FP_LOCK:
        hit = _FP.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
    try:
        digest = file_sha256(path)
    except OSError:
        return None
    with _FP_LOCK:
        _FP[key] = (stamp, digest)
    return digest


def source_glb(module: Path, lod: str) -> Path:
    return Path(module) / 'reduced' / f'lod_error_{lod}.glb'


# ---------------------------------------------------------------------------------------------------------------------- task keys

def layer_key(set_name: str, unit: str, lod: str, job: str, glb_sha256: str, fingerprint: str | None = None) -> str:
    """`layers:S:race/module:lod:job:<glb sha12>` (+ `:<fp12>` for a rebuild of a stale / unpublished unit: the fingerprint changed, so the key does)."""
    key = f'layers:{set_name}:{unit}:{lod}:{job}:{glb_sha256[:12]}'
    return f'{key}:{fingerprint[:12]}' if fingerprint else key


def bare_key(race: str, fingerprint: str | None = None) -> str:
    key = f'bare:{race}:{body_sha12(race)}'
    return f'{key}:{fingerprint[:12]}' if fingerprint else key


# ------------------------------------------------------------------------------------------------------------------ unit status

BUILT = ('published', 'stale')
SOURCE_CHANGED = 'source_changed'
NEEDS_REVIEW = 'needs_review'          # a 3D pin without glb_sha256 (approved before digests were recorded): Back to 3D (re-check R10)
DAMAGED = 'damaged'                    # a pointer exists but does not name a valid publication (bytes changed, invalid GLB ...; re-check R3)
CANCELLED = 'cancelled'                # the user cancelled this approval generation's task (re-check R8): not re-queued until Retry / Rebuild
ACTIVE_STATES = ('starting', 'running', 'orphan')
STALL_DEFAULT = 30


def stall_minutes(root: Path) -> int:
    """settings.json `build_stall_minutes` (an integer 1..1440, default 30; read through settings.load, the only settings reader): an attempt
    whose logs stop growing that long is `stalled`."""
    return app_settings.load(root)['build_stall_minutes']


def last_output(task: dict) -> float | None:
    """When the attempt's two logs (`<id>-a<n>.log` / `.err.log`) last grew: the heartbeat. None without logs."""
    log = task.get('log')
    if not log or not str(log).endswith('.log'):
        return None
    stamps = []
    for path in (str(log), str(log)[:-4] + '.err.log'):
        try:
            stamps.append(os.stat(path).st_mtime)
        except OSError:
            pass
    return max(stamps) if stamps else None


def is_stalled(task: dict, now: float, minutes: int) -> bool:
    """An active (starting / running) attempt whose logs have not grown for `minutes` (a hung build)."""
    if task.get('state') not in ('starting', 'running') or not task.get('started'):
        return False
    last = last_output(task)
    return now - (last if last is not None else task['started']) > minutes * 60.0


def shown_state(task: dict, now: float, minutes: int) -> str:
    """The state the UI shows: `stalled` for a stalled active attempt, `running` for a starting one, `orphan` for an attempt whose wrapper died
    while its builder still runs (re-check R6: the slot stays taken, Kill is offered), else the stored state."""
    if task['state'] == 'orphan':
        return 'orphan'
    if is_stalled(task, now, minutes):
        return 'stalled'
    return 'running' if task['state'] == 'starting' else task['state']


def task_brief(task: dict, now: float, stall: int) -> dict:
    """`{id, state, elapsed, error?}` of a task for item.builds / item.bare_builds (`state` as shown: queued | running | stalled | done |
    failed | cancelled)."""
    started = task.get('started')
    end = task.get('finished') or now
    out = {'id': task['id'], 'state': shown_state(task, now, stall), 'elapsed': round(end - started, 1) if started else None}
    if task['state'] == 'failed' and task.get('error'):
        out['error'] = str(task['error'])[-600:]
    return out


def bare_build(race: str, snap: tuple[dict | None, str | None], tasks: list[dict], stale: dict, now: float, stall: int = STALL_DEFAULT) -> dict:
    """`item.bare_builds[race]` = {status: published | stale | building | queued | failed | damaged | cancelled | none, build_id, error?, task?};
    `snap` = the race's ONE pointer snapshot of this derivation (`bare_snapshot`); `tasks` = the bare tasks of the race, newest first. A published
    build keeps its status while a rebuild runs (`task` shows it)."""
    ptr, problem = snap
    active = next((t for t in tasks if t['state'] in ACTIVE_STATES + ('queued',)), None)
    newest = tasks[0] if tasks else None
    record: dict = {'status': 'none', 'build_id': None}
    if ptr:
        marked = stale.get(race)
        record |= {'status': 'stale' if isinstance(marked, dict) and marked.get('build_id') == ptr['build_id'] else 'published',
                   'build_id': ptr['build_id']}
    elif active:
        record['status'] = 'queued' if active['state'] == 'queued' else 'building'
    elif problem and problem != NO_POINTER:
        record |= {'status': DAMAGED, 'error': f'the published bare build is not valid: {problem}'[:600]}
    elif newest and newest['state'] == 'failed':
        record |= {'status': 'failed', 'error': str(newest.get('error') or '')[-600:] or None}
    elif newest and newest['state'] == 'cancelled' and newest.get('cancel_gen'):
        record['status'] = CANCELLED
    task = active or newest
    if task:
        record['task'] = task_brief(task, now, stall)
    return record


def _failure_text(out_root: Path, lod: str, module_tail: str, task_error: str | None) -> str | None:
    """The newest failure text: `layers/last_failure.json` when it is about this LOD and module, else the task's own error."""
    fail = read_json(out_root / 'last_failure.json')
    if isinstance(fail, dict) and fail.get('lod') == lod and isinstance(fail.get('module'), str) and fail['module'].endswith(module_tail):
        text = str(fail.get('error') or '')
        if text:
            return text[-600:]
    return (task_error or None) and str(task_error)[-600:]


def task_matches(task: dict, approved: dict, gen: str | None) -> bool:
    """A layers task belongs to the CURRENT approval of its unit (re-check R11): same job, LOD and source digest, and the same approval generation
    (`approval_gen` column, written whenever `actions._ensure` queues the row for an approval). An older approval's attempt never matches."""
    args = task.get('args') if isinstance(task.get('args'), dict) else {}
    return (args.get('lod') == approved.get('lod') and args.get('job') == approved.get('job') and bool(approved.get('glb_sha256'))
            and args.get('glb_sha256') == approved.get('glb_sha256') and gen is not None and task.get('approval_gen') == gen)


def unit_build(root: Path, set_name: str, key: str, unit: dict, approved: dict, tasks: list[dict], stale: dict, now: float,
               stall: int = STALL_DEFAULT, gen: str | None = None) -> dict:
    """The `item.builds[key]` record of one approved unit. `tasks` = the set's layer tasks of this unit (any state), newest first; only those of
    the current approval (`task_matches`, generation `gen`) count. The pointer is read ONCE (re-check R2): status, id and slot URLs come from it."""
    race, module, model_set, job = unit['race'], unit['module'], unit['model_set'], unit['job']
    lod = approved.get('lod')
    mod = module_dir(root, model_set, job, race, module)
    ptr, problem = module_snapshot(mod, lod) if lod else (None, NO_POINTER)
    pinned = approved.get('glb_sha256')
    changed = bool(lod and pinned) and cached_sha256(source_glb(mod, lod)) != pinned
    mine = [t for t in tasks if task_matches(t, approved, gen)]
    active = next((t for t in mine if t['state'] in ACTIVE_STATES + ('queued',)), None)
    newest = mine[0] if mine else None
    out_root = mod / 'layers'
    record = {'status': 'none', 'build_id': None, 'lod': lod, 'slots': {}, 'warnings': [], 'metrics': {}, 'error': None, 'task': None}
    if lod and not pinned:
        # an older pin without the source digest: nothing can be built or certified for it (re-check R10)
        record |= {'status': NEEDS_REVIEW, 'error': 'approved before source digests were recorded; send the set back to 3D and approve it again'}
        return record
    if changed:
        # the approved LOD GLB is not the file the approval pinned: no build of it can be certified; the old layers stay viewable
        record |= {'status': SOURCE_CHANGED, 'error': 'the approved LOD file changed since the 3D approval; send the set back to 3D'}
        if ptr:
            record |= {'build_id': ptr['build_id'], 'slots': {s: file_url(root, out_root / e['glb']) for s, e in ptr['slots'].items()},
                       'warnings': [str(w) for w in ptr.get('warnings') or []], 'metrics': ptr.get('metrics') if isinstance(ptr.get('metrics'), dict) else {}}
    elif ptr:
        marked = stale.get(f'{set_name}/{key}')
        record |= {'status': 'stale' if isinstance(marked, dict) and marked.get('build_id') == ptr['build_id'] else 'published',
                   'build_id': ptr['build_id'], 'slots': slot_urls(root, out_root, ptr),
                   'warnings': [str(w) for w in ptr.get('warnings') or []], 'metrics': ptr.get('metrics') if isinstance(ptr.get('metrics'), dict) else {}}
    elif active:
        record['status'] = 'queued' if active['state'] == 'queued' else 'building'
    elif problem and problem != NO_POINTER:
        record |= {'status': DAMAGED, 'error': f'the published build is not valid: {problem}'[:600]}
    elif newest and newest['state'] == 'failed':
        record['status'] = 'failed'
        record['error'] = _failure_text(out_root, lod, f'{model_set}/{job}/{race}/{module}', newest.get('error'))
    elif newest and newest['state'] == 'cancelled' and newest.get('cancel_gen') == gen:
        record |= {'status': CANCELLED, 'error': 'cancelled in the queue; Rebuild (or Retry in the queue) builds it'}
    task = active or newest
    if task:
        record['task'] = task_brief(task, now, stall)
        if record['status'] in BUILT and task['state'] == 'failed' and not active:
            record['error'] = (task.get('error') or '')[-600:] or None          # a failed rebuild: the old build stays published
    return record


def counts(builds: dict[str, dict]) -> dict:
    """{"total", "built", "failed", "building", "none", "stale", "source_changed", "needs_review", "damaged", "cancelled"} over item.builds
    (built = published or stale; `none` also counts the damaged and cancelled units: not built)."""
    out = {'total': len(builds), 'built': 0, 'failed': 0, 'building': 0, 'none': 0, 'stale': 0, 'source_changed': 0, 'needs_review': 0,
           'damaged': 0, 'cancelled': 0}
    for rec in builds.values():
        status = rec['status']
        if status in BUILT:
            out['built'] += 1
            out['stale'] += status == 'stale'
        elif status in (SOURCE_CHANGED, NEEDS_REVIEW):
            out[status] += 1
        elif status in (DAMAGED, CANCELLED):
            out[status] += 1
            out['none'] += 1
        elif status in ('building', 'queued'):
            out['building'] += 1
        elif status == 'failed':
            out['failed'] += 1
        else:
            out['none'] += 1
    return out
