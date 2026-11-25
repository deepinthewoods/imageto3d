"""The layer builder as a tool (pipeline v2, phase 2): per-module API, fingerprint, validated atomic publish, CLI.

    python tools/layers/build.py module <module_dir> --lod 0p2 [--out DIR] [--cpu] [--force]
    python tools/layers/build.py bare <race> [--out DIR] [--cpu] [--force]
    python tools/layers/build.py parity <ref_dir> <ref_set> <new_dir> [--lod 0p2] [--json FILE]

    build_module(module_dir, lod, out_root=None, gpu=True, force=False) -> dict    every slot of one generated module at one reduction level
    build_bare(race, out_root=None, gpu=True, force=False) -> dict                 the game body's own layers of a race

What is built is the prototype's layer code (layers.py / geodiv.py / shell_slots.py / pieces.py, moved here from experiments/shell_slots_20261122/);
this file adds the contract around it:

  * a fingerprint (sha256 over the code AND every repo-local file the build imports, the body caches, the module's identity (set/job/race/module),
    its fit + LOD files and the LOD) names the build: build_id = '<lod>-<fp[:12]>' ('bare-<fp[:12]>' for bare layers). A published build whose pointer
    carries the same fingerprint and that VALIDATES (build.json, every required slot's GLB + JSON, every GLB parsed whole, the morph target 'fill')
    is returned as it is (status 'current'); `force` skips that and builds anyway (CPU / GPU comparisons).
  * the build is written into <out_root>/.attempt-<tag>-<fp12>-<pid>/, checked (every required slot of the module, see REQUIRED SLOTS below), then,
    under the exclusive lock <out_root>/.publish.lock: the fingerprint is recomputed (changed inputs: the attempt is discarded, status 'failed',
    'inputs changed during build', retryable), a valid build with the same fingerprint is adopted when one is already there, otherwise the attempt is
    renamed to <out_root>/<build_id>/ and the per-LOD pointer <out_root>/current_<lod>.json ('current_bare.json') is replaced atomically. A damaged
    existing build folder is never rewritten: the new build gets the id '<lod>-<fp12>-r<n>'. A failure writes <out_root>/last_failure.json (only from a
    worker whose inputs are still the current ones) and leaves the pointers and every earlier build as they were. Old builds are never deleted here.

REQUIRED SLOTS (a missing one fails the build, e.g. 'no garment found for lower_leg.r (boot)'): outfit -> outfit; body_armor -> torso;
right_gauntlet_boot -> lower_arm.l/.r + lower_leg.l/.r, the leg slots only where the race's game body has legs (layers.slots(), the gnome has none);
bare -> head, torso, lower_arm.l/.r (+ lower_leg.l/.r where the body has legs).

One build per process is the production use (the layer lane starts a fresh process per task); repeated in-process calls are safe too (the
game-body caches are dropped when the body cache files change). The CLI prints the result dict as JSON on its LAST stdout line on every path
(argument errors, bad inputs, build failures); exit 0 for 'ok' / 'current', 1 for 'failed'.
"""
import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import time
import traceback
from pathlib import Path

for _v in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):      # one build per process; the callers run several processes
    os.environ.setdefault(_v, '1')

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
BASE_CACHE = ROOT / 'tools' / 'preview' / 'cache' / 'bases'
BARE_ROOT = ROOT / 'tools' / 'preview' / 'cache' / 'layers'
LODS = ('0p4', '0p2', '0p1')
MORPH = 'fill'
FP_SCHEMA = 3                                                       # bump with a change of what the fingerprint covers
# Every repo-local file the build path loads (checked by test_build.test_fingerprint_dependency_closure against sys.modules + spec_from_file_location).
CODE_DEPS = ('tools/layers/layers.py', 'tools/layers/geodiv.py', 'tools/layers/shell_slots.py', 'tools/layers/pieces.py', 'tools/layers/build.py',
             'tools/tripo_pipeline/body_fit.py', 'tools/tripo_pipeline/align_body.py', 'tools/tripo_pipeline/fit_lab.py',
             'tools/tripo_pipeline/gpu_geometry.py', 'tools/tripo_pipeline/rewear.py', 'tools/tripo_pipeline/portable.py',
             'tools/outfit_studio/core/transform.py', 'experiments/body_mask_20261104/bm_core.py')
MODULE_KINDS = ('outfit', 'body_armor', 'right_gauntlet_boot')
LEG_SLOTS = ('lower_leg.l', 'lower_leg.r')
SLOT_MIN = {'outfit': ('outfit',), 'body_armor': ('torso',), 'right_gauntlet_boot': ('lower_arm.l', 'lower_arm.r'),
            'bare': ('head', 'torso', 'lower_arm.l', 'lower_arm.r')}
SLOT_ALLOWED = {'outfit': ('outfit',), 'body_armor': ('torso',), 'right_gauntlet_boot': ('lower_arm.l', 'lower_arm.r') + LEG_SLOTS,
                'bare': ('head', 'torso', 'lower_arm.l', 'lower_arm.r') + LEG_SLOTS}
SLOT_LABEL = {'outfit': 'outfit', 'torso': 'armour', 'lower_arm': 'glove', 'lower_leg': 'boot', 'head': 'head'}
RACE_RE = re.compile(r'[a-z][a-z0-9_]*')
ATTEMPT_STALE_S = 3600.0                                            # an '.attempt-*' older than this with no live owner process is abandoned
ATTEMPT_MAX_S = 24 * 3600.0                                         # ... and one older than this is abandoned whoever owns the pid (a reused pid)
LOCK_TIMEOUT_S = 300.0


# ----------------------------------------------------------------------------------------------- fingerprint

def _file_hash(p):
    """sha256 of a file, or 'absent'. Source files are hashed with LF line endings (a CRLF checkout must not change the fingerprint)."""
    p = Path(p)
    if not p.is_file():
        return 'absent'
    b = p.read_bytes()
    if p.suffix in ('.py', '.json'):
        b = b.replace(b'\r\n', b'\n')
    return hashlib.sha256(b).hexdigest()


def _combine(parts):
    h = hashlib.sha256()
    for k, v in parts:
        h.update(f'{k}={v}\n'.encode())
    return h.hexdigest()


def code_parts():
    return [(f'code/{rel}', _file_hash(ROOT / rel)) for rel in CODE_DEPS]


def module_race(module_dir):
    return Path(module_dir).resolve().parts[-2]


def module_identity(module_dir):
    """'<set>/<job>/<race>/<module>': the pinned job a build belongs to (part of the fingerprint: the same bytes in another job or kind are another build)."""
    return '/'.join(Path(module_dir).resolve().parts[-4:])


def fingerprint_module(module_dir, lod):
    """sha256 over the code (CODE_DEPS), the module identity and kind, the race's game body caches, the module's fit/align.json, the LOD GLB
    (+ colours npz), reduced/manifest.json and the LOD."""
    module_dir = Path(module_dir).resolve()
    race = module_race(module_dir)
    red = module_dir / 'reduced'
    parts = [('schema', FP_SCHEMA), ('identity', module_identity(module_dir)), ('kind', module_dir.name)] + code_parts() + [
        (f'bases/{race}.npz', _file_hash(BASE_CACHE / f'{race}.npz')),
        (f'bases/{race}_rig.npz', _file_hash(BASE_CACHE / f'{race}_rig.npz')),
        ('fit/align.json', _file_hash(module_dir / 'fit' / 'align.json')),
        (f'reduced/lod_error_{lod}.glb', _file_hash(red / f'lod_error_{lod}.glb')),
        (f'reduced/lod_error_{lod}_colors.npz', _file_hash(red / f'lod_error_{lod}_colors.npz')),
        ('reduced/manifest.json', _file_hash(red / 'manifest.json')),
        ('lod', lod)]
    return _combine(parts)


def fingerprint_bare(race):
    return _combine([('schema', FP_SCHEMA), ('kind', 'bare'), ('race', race)] + code_parts() +
                    [(f'bases/{race}.npz', _file_hash(BASE_CACHE / f'{race}.npz')), (f'bases/{race}_rig.npz', _file_hash(BASE_CACHE / f'{race}_rig.npz'))])


# ----------------------------------------------------------------------------------------------- files

def _write_json_atomic(path, obj):
    path = Path(path)
    tmp = path.with_name(f'{path.name}.tmp-{os.getpid()}')
    try:
        tmp.write_text(json.dumps(obj, indent=1, default=float), encoding='utf-8')
        for k in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:                                   # Windows: a reader has the target open for a moment
                if k == 9:
                    raise
                time.sleep(0.1 * (k + 1))
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None


def _rename_dir(src, dst):
    for k in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if k == 9:
                raise
            time.sleep(0.2 * (k + 1))


def _pid_alive(pid):
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    if os.name == 'nt':
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)         # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259                                          # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


@contextlib.contextmanager
def _publish_lock(out_root, timeout=LOCK_TIMEOUT_S):
    """Exclusive per-output-root lock (<out_root>/.publish.lock; msvcrt byte lock on Windows, fcntl.flock elsewhere): the OS drops it when the
    process dies. Waits up to `timeout` seconds, then raises TimeoutError."""
    f = open(Path(out_root) / '.publish.lock', 'a+b')
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f'publish lock {Path(out_root) / ".publish.lock"} is held by another process') from None
                time.sleep(0.05)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                if os.name == 'nt':
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


def _clean_abandoned(out_root, keep=None):
    """Remove '.attempt-*' folders (and stray '*.tmp-<pid>' files) of builders that are gone: older than ATTEMPT_STALE_S with no live pid in the name, or
    older than ATTEMPT_MAX_S. Called under the publish lock."""
    now = time.time()
    for p in list(Path(out_root).glob('.attempt-*')) + list(Path(out_root).glob('*.tmp-*')):
        if keep is not None and p == keep:
            continue
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        m = re.search(r'-(\d+)$', p.name)
        pid = int(m.group(1)) if m else None
        if age > ATTEMPT_MAX_S or (age > ATTEMPT_STALE_S and (pid is None or not _pid_alive(pid))):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    p.unlink()


# ----------------------------------------------------------------------------------------------- GLB / build validation

_CT_SIZE = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
_NCOMP = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}


def check_glb(path, need_morph=False):
    """Parse a layer GLB completely: header and length, JSON chunk, BIN chunk length, buffers / views / accessors in range, every primitive's accessors
    and morph targets, material indices. Returns (problems [str], info {'faces', 'targets'}); with need_morph also the target MORPH on every mesh."""
    problems, info = [], {'faces': 0, 'targets': []}
    try:
        b = Path(path).read_bytes()
    except OSError as error:
        return [f'unreadable ({error})'], info
    if len(b) < 28:
        return ['truncated (shorter than a GLB header)'], info
    magic, version, total = struct.unpack_from('<4sII', b, 0)
    if magic != b'glTF' or version != 2:
        return ['not a glTF 2 binary'], info
    if total != len(b):
        return [f'length field {total} != file size {len(b)} (truncated?)'], info
    n, kind = struct.unpack_from('<II', b, 12)
    if kind != 0x4E4F534A or 20 + n + 8 > len(b):
        return ['bad JSON chunk'], info
    try:
        js = json.loads(b[20:20 + n])
    except ValueError:
        return ['JSON chunk does not parse'], info
    off = 20 + n
    blen, bkind = struct.unpack_from('<II', b, off)
    if bkind != 0x004E4942:
        return ['bad BIN chunk'], info
    if off + 8 + blen != len(b):
        return [f'BIN chunk length {blen} does not match the file ({len(b) - off - 8} bytes there)'], info
    bufs, views, accs = js.get('buffers') or [], js.get('bufferViews') or [], js.get('accessors') or []
    if len(bufs) != 1 or not 0 < bufs[0].get('byteLength', 0) <= blen:
        return [f'buffer length {bufs[0].get("byteLength") if bufs else None} does not fit the BIN chunk ({blen})'], info
    for i, v in enumerate(views):
        if v.get('buffer', 0) != 0 or v.get('byteOffset', 0) < 0 or v.get('byteOffset', 0) + v.get('byteLength', -1) > bufs[0]['byteLength'] or v.get('byteLength', -1) <= 0:
            problems.append(f'bufferView {i} outside the buffer')
    for i, a in enumerate(accs):
        v = views[a['bufferView']] if isinstance(a.get('bufferView'), int) and 0 <= a['bufferView'] < len(views) else None
        if v is None or a.get('componentType') not in _CT_SIZE or a.get('type') not in _NCOMP or a.get('count', 0) <= 0:
            problems.append(f'accessor {i} invalid')
            continue
        need = a.get('byteOffset', 0) + a['count'] * _CT_SIZE[a['componentType']] * _NCOMP[a['type']]
        if need > v.get('byteLength', 0):
            problems.append(f'accessor {i} runs past its bufferView')
    if problems:
        return problems[:6], info

    def acc_ok(i):
        return isinstance(i, int) and 0 <= i < len(accs)
    meshes = js.get('meshes') or []
    if not meshes:
        return ['no mesh'], info
    nmat = len(js.get('materials') or [])
    for mesh in meshes:
        names = (mesh.get('extras') or {}).get('targetNames', [])
        info['targets'] = list(names)
        if need_morph and MORPH not in names:
            problems.append(f'no morph target {MORPH!r}')
        for p in mesh.get('primitives') or []:
            pos = (p.get('attributes') or {}).get('POSITION')
            if not acc_ok(pos) or accs[pos]['count'] % 3:
                problems.append('primitive without a usable POSITION accessor')
                continue
            if 'material' in p and not (isinstance(p['material'], int) and 0 <= p['material'] < nmat):
                problems.append('primitive material out of range')
            tg = p.get('targets') or []
            if len(tg) != len(names):
                problems.append('primitive morph targets do not match targetNames')
            for t in tg:
                if not acc_ok(t.get('POSITION')) or accs[t['POSITION']]['count'] != accs[pos]['count']:
                    problems.append('morph target POSITION does not match the primitive')
            if 'indices' in p:
                problems.append('indexed primitive (layers are written unindexed)')
            info['faces'] += accs[pos]['count'] // 3
    if not info['faces'] and not problems:
        problems.append('no faces')
    return problems[:6], info


def _required_problems(kind, required):
    """The module kind's contract on the list of required slots: at least SLOT_MIN, nothing outside SLOT_ALLOWED, the leg slots both or neither."""
    if kind not in SLOT_MIN:
        return [f'unknown kind {kind!r}']
    req = set(required or [])
    out = []
    miss = [k for k in SLOT_MIN[kind] if k not in req]
    if miss:
        out.append(f'required slots missing from the build contract: {", ".join(miss)}')
    extra = sorted(req - set(SLOT_ALLOWED[kind]))
    if extra:
        out.append(f'slots not allowed for {kind}: {", ".join(extra)}')
    if sum(k in req for k in LEG_SLOTS) == 1:
        out.append('only one leg slot required (both or neither)')
    return out


def validate_build(build_dir, fp=None, expect_id=None):
    """Problems ([str], empty = valid) of a build folder: build.json present and ok (matching `fp` / `expect_id` when given), required slots follow the
    kind's contract, every required slot has its GLB (parsed whole, morph target 'fill' for module layers, faces) and its JSON."""
    build_dir = Path(build_dir)
    rec = _read_json(build_dir / 'build.json')
    if not isinstance(rec, dict):
        return ['build.json missing or unreadable']
    problems = []
    if rec.get('status') != 'ok':
        problems.append(f"build.json status {rec.get('status')!r}")
    if expect_id is not None and rec.get('build_id') != expect_id:
        problems.append(f"build.json build_id {rec.get('build_id')!r} != {expect_id!r}")
    if fp is not None and rec.get('fingerprint') != fp:
        problems.append('build.json fingerprint differs')
    kind = rec.get('kind')
    required = rec.get('required')
    if not isinstance(required, list) or not required:
        problems.append('build.json has no required slot list')
        return problems
    problems += _required_problems(kind, required)
    slots = rec.get('slots') if isinstance(rec.get('slots'), dict) else {}
    for k in required:
        if k not in slots:
            problems.append(f'slot {k}: not in build.json')
            continue
        g, j = build_dir / f'{k}.glb', build_dir / f'{k}.json'
        if not g.is_file() or not j.is_file() or not isinstance(_read_json(j), dict):
            problems.append(f'slot {k}: glb/json missing or unreadable')
            continue
        bad, info = check_glb(g, need_morph=kind != 'bare')
        if bad:
            problems.append(f'slot {k}: glb {"; ".join(bad)}')
        elif info['faces'] != slots[k].get('faces'):
            problems.append(f"slot {k}: glb has {info['faces']} faces, build.json says {slots[k].get('faces')}")
    return problems


# ----------------------------------------------------------------------------------------------- process state

def _gpu_configure(gpu):
    """BODY_FIT_GPU must be set before body_fit is imported; when it already is (a second build in this process) its switches are set directly."""
    if gpu:
        os.environ['BODY_FIT_GPU'] = '1'
    else:
        os.environ.pop('BODY_FIT_GPU', None)
    bf = sys.modules.get('body_fit')
    if bf is not None:
        bf._GPU_PARTS = {'rays', 'closest', 'raster'} if gpu else set()
        bf._GPU_ON = bool(bf._GPU_PARTS)


def _gpu_used(gpu):
    """True when the GPU path was enabled and no GPU error switched the process back to the CPU (body_fit prints that to stderr)."""
    bf = sys.modules.get('body_fit')
    return bool(gpu and bf is not None and getattr(bf, '_GPU_ON', False) and 'gpu_geometry' in sys.modules)


_BODY_STAMPS = {}


def _body_stamp(race):
    out = []
    for x in (f'{race}.npz', f'{race}_rig.npz'):
        try:
            st = (BASE_CACHE / x).stat()
            out.append((st.st_size, st.st_mtime_ns))
        except OSError:
            out.append(None)
    return tuple(out)


def _reset_caches(race):
    """Drop the in-memory game-body caches of `race` (body_fit._BODIES, geodiv._GAME_BODY, layers._LAYER_BODY, fit_lab._PLANES) when the body cache files
    changed (size, mtime) since the last build of this race in this process: a repeated build must never use the old body."""
    stamp = _body_stamp(race)
    if _BODY_STAMPS.get(race) == stamp:
        return
    _BODY_STAMPS[race] = stamp
    for mod, name in (('body_fit', '_BODIES'), ('layers', '_LAYER_BODY'), ('fit_lab', '_PLANES'), ('geodiv', '_GAME_BODY')):
        cache = getattr(sys.modules.get(mod), name, None)
        if isinstance(cache, dict):
            for k in [k for k in cache if k == race or (isinstance(k, tuple) and k and k[0] == race)]:
                del cache[k]


def _peak_mb():
    try:
        import psutil
        mi = psutil.Process().memory_info()
        return round(getattr(mi, 'peak_wset', mi.rss) / 2 ** 20, 1)
    except Exception:  # noqa: BLE001
        return None


def _rel(p):
    p = Path(p).resolve()
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def _race_problem(race):
    """None when `race` is a single identifier with game body caches, else why not (checked before anything is written)."""
    if not isinstance(race, str) or not RACE_RE.fullmatch(race):
        return f'invalid race {race!r} (a single lowercase identifier is required)'
    for x in (f'{race}.npz', f'{race}_rig.npz'):
        if not (BASE_CACHE / x).is_file():
            return f'missing game body cache {BASE_CACHE / x}'
    return None


# ----------------------------------------------------------------------------------------------- the build

class _Job:
    """What one build is: tag ('0p2' | 'bare'), lod, race, kind, fingerprint and the way to recompute it, and how to run the layer code."""

    def __init__(self, tag, lod, race, kind, module_path, fp, fp_fn, gpu, force, run):
        self.tag, self.lod, self.race, self.kind, self.module_path = tag, lod, race, kind, module_path
        self.fp, self.fp_fn, self.gpu, self.force, self.run = fp, fp_fn, gpu, force, run
        self.base_id = f'{tag}-{fp[:12]}'
        self.bare = kind == 'bare'


def _pointer(out_root, tag):
    return Path(out_root) / f'current_{tag}.json'


def _previous_build(out_root, tag):
    rec = _read_json(_pointer(out_root, tag))
    return rec.get('build_id') if isinstance(rec, dict) and rec.get('status') == 'ok' else None


def _failure(out_root, job_or_none, error, *, module_path=None, lod=None, tag=None, fp=None, build_id=None, seconds=None, gpu=None, tb=None, **extra):
    tag = tag or (job_or_none.tag if job_or_none else None)
    fail = {'status': 'failed', 'build_id': build_id, 'fingerprint': fp if job_or_none is None else job_or_none.fp,
            'lod': lod if job_or_none is None else job_or_none.lod, 'module': module_path if job_or_none is None else job_or_none.module_path,
            'error': error, 'traceback': tb, 'time': _now(), 'seconds': seconds, 'gpu': gpu, 'peak_rss_mb': _peak_mb(),
            'previous_build': _previous_build(out_root, tag) if tag and out_root else None}
    fail.update(extra)
    return dict(fail, out_root=str(out_root) if out_root else None)


def _write_failure(out_root, fail):
    """last_failure.json under the publish lock (best effort)."""
    try:
        Path(out_root).mkdir(parents=True, exist_ok=True)
        with _publish_lock(out_root, 30.0):
            _write_json_atomic(Path(out_root) / 'last_failure.json', {k: v for k, v in fail.items() if k != 'out_root'})
    except Exception as error:  # noqa: BLE001
        fail['failure_file_error'] = f'{type(error).__name__}: {error}'


def _fail_early(out_root, module_path, lod, message, tag, write=True):
    fail = _failure(out_root, None, message, module_path=module_path, lod=lod, tag=tag)
    if write:
        _write_failure(out_root, fail)
    return fail


def _record(res, build_id, job, seconds, gpu_used):
    slots = {k: {'glb': f'{build_id}/{k}.glb', 'json': f'{build_id}/{k}.json', 'faces': st['faces']} for k, st in sorted(res['slots'].items())}
    warnings = [f'{k}: {w}' for k, st in sorted(res['slots'].items()) for w in st.get('warnings', [])]
    wc = {k: st['wall_crossings_left'] for k, st in sorted(res['slots'].items()) if st.get('wall_crossings_left') is not None}
    flat = [x for v in wc.values() for x in v]
    mirrored = sorted(k for k in res['slots'] if k.endswith('.l') and res['module'] == 'right_gauntlet_boot')
    metrics = {'wall_crossings_left': max(flat) if flat else (None if not job.bare else 0), 'wall_crossings_left_by_slot': wc,
               'boundary_edges': {k: st['boundary_edges'] for k, st in sorted(res['slots'].items())},
               'ring_vertices_merged': {k: st['ring_original_merged'] for k, st in sorted(res['slots'].items()) if st.get('ring_original_merged')},
               'ring_vertices_inserted': {k: st['ring_line_vertices'] for k, st in sorted(res['slots'].items()) if st.get('ring_line_vertices')},
               'skipped': dict(res['skipped']), 'mirrored_from_right': mirrored}
    return {'build_id': build_id, 'fingerprint': job.fp, 'lod': job.lod, 'status': 'ok', 'time': _now(), 'seconds': seconds, 'slots': slots,
            'required': list(res['expected']), 'warnings': warnings, 'metrics': metrics, 'gpu': gpu_used, 'module': job.module_path,
            'race': res['race'], 'set': res['set'], 'kind': res['module'], 'peak_rss_mb': _peak_mb()}


def _check_result(res, job):
    """Raise unless the layer code built every REQUIRED slot of this module kind (a slot without garment is a failure, not a skip)."""
    problems = []
    if res.get('module') != job.kind:
        problems.append(f"result is for {res.get('module')!r}, not {job.kind!r}")
    required = list(res.get('expected') or [])
    problems += _required_problems(job.kind, required)
    for k in required:
        if k in res['slots']:
            continue
        reason = res['skipped'].get(k)
        if reason == 'no garment in this slot':
            problems.append(f"no garment found for {k} ({SLOT_LABEL.get(k.split('.')[0], k)})")
        else:
            problems.append(f'slot {k} not built ({reason or "no result"})')
    if problems:
        raise RuntimeError('build check failed: ' + '; '.join(problems))


def _free_id(out_root, base):
    if not (Path(out_root) / base).exists():
        return base
    n = 1
    while (Path(out_root) / f'{base}-r{n}').exists():
        n += 1
    return f'{base}-r{n}'


def _adopt(out_root, job):
    """A VALID published build with this job's fingerprint ('<base>' or a recovery '<base>-r<n>' folder): point the pointer at it (when it does not
    already) and return its record as status 'current'; None when there is none. Called under the publish lock."""
    out_root = Path(out_root)
    names = [job.base_id] + sorted((p.name for p in out_root.glob(f'{job.base_id}-r*') if p.is_dir()), key=lambda n: int(n.rsplit('-r', 1)[1]) if n.rsplit('-r', 1)[1].isdigit() else 0)
    for name in names:
        d = out_root / name
        if not d.is_dir() or validate_build(d, fp=job.fp, expect_id=name):
            continue
        rec = _read_json(d / 'build.json')
        ptr = _read_json(_pointer(out_root, job.tag))
        if not (isinstance(ptr, dict) and ptr.get('build_id') == name and ptr.get('fingerprint') == job.fp):
            _write_json_atomic(_pointer(out_root, job.tag), rec)
        return dict(rec, status='current', out_root=str(out_root))
    return None


def _current(out_root, job):
    """The cheap path: the pointer names a build with this fingerprint that validates."""
    rec = _read_json(_pointer(out_root, job.tag))
    if isinstance(rec, dict) and rec.get('status') == 'ok' and rec.get('fingerprint') == job.fp and isinstance(rec.get('build_id'), str) and \
            not validate_build(Path(out_root) / rec['build_id'], fp=job.fp, expect_id=rec['build_id']):
        return dict(rec, status='current', out_root=str(out_root))
    return None


def _commit(out_root, job, attempt, res, seconds):
    """Under the publish lock: recheck the inputs, adopt an identical winner or rename the attempt into place and move the pointer."""
    out_root = Path(out_root)
    with _publish_lock(out_root):
        _clean_abandoned(out_root, keep=attempt)
        if job.fp_fn() != job.fp:
            shutil.rmtree(attempt, ignore_errors=True)
            return _failure(out_root, job, 'inputs changed during build (retry)', seconds=seconds, gpu=_gpu_used(job.gpu), retryable=True)
        if not job.force:
            got = _adopt(out_root, job)
            if got is not None:
                shutil.rmtree(attempt, ignore_errors=True)
                return got
        build_id = _free_id(out_root, job.base_id)
        rec = _record(res, build_id, job, seconds, _gpu_used(job.gpu))
        _write_json_atomic(attempt / 'build.json', rec)
        problems = validate_build(attempt, fp=job.fp, expect_id=build_id)
        if problems:
            raise RuntimeError('build check failed: ' + '; '.join(problems))
        _rename_dir(attempt, out_root / build_id)
        _write_json_atomic(_pointer(out_root, job.tag), rec)
        lf = _read_json(out_root / 'last_failure.json')
        if isinstance(lf, dict) and lf.get('lod') == job.lod and lf.get('module') == job.module_path:      # (a failure of another LOD stays)
            with contextlib.suppress(OSError):
                (out_root / 'last_failure.json').unlink()
        return dict(rec, out_root=str(out_root))


def _fail_build(out_root, job, attempt, error, t0):
    """A failed attempt: remove it and, under the lock, record the failure unless the inputs moved on or an identical valid build exists meanwhile."""
    shutil.rmtree(attempt, ignore_errors=True)
    fail = _failure(out_root, job, f'{type(error).__name__}: {error}', build_id=job.base_id, seconds=round(time.monotonic() - t0, 1),
                    gpu=_gpu_used(job.gpu), tb=''.join(traceback.format_exception(type(error), error, error.__traceback__)))
    try:
        with _publish_lock(out_root, 60.0):
            if job.fp_fn() != job.fp:
                fail['superseded'] = True                           # a newer worker owns the pointer and the failure file
            elif not job.force and (got := _adopt(out_root, job)) is not None:
                return got
            else:
                _write_json_atomic(Path(out_root) / 'last_failure.json', {k: v for k, v in fail.items() if k != 'out_root'})
    except Exception as e2:  # noqa: BLE001
        fail['failure_file_error'] = f'{type(e2).__name__}: {e2}'
    return fail


def _publish(out_root, job):
    """The shared publish scheme (see the module doc). Returns the result dict; never raises except KeyboardInterrupt / SystemExit."""
    out_root = Path(out_root)
    try:
        out_root.mkdir(parents=True, exist_ok=True)
        if not job.force:
            got = _current(out_root, job)
            if got is not None:
                return got
        attempt = out_root / f'.attempt-{job.tag}-{job.fp[:12]}-{os.getpid()}'
        if attempt.exists():
            shutil.rmtree(attempt, ignore_errors=True)               # (our own leftover of an earlier run of this pid)
        attempt.mkdir(parents=True)
    except OSError as error:
        return _failure(out_root, job, f'cannot set up the output folder: {type(error).__name__}: {error}')
    t0 = time.monotonic()
    err = None
    try:
        _gpu_configure(job.gpu)
        _reset_caches(job.race)
        res = job.run(attempt)
        seconds = round(time.monotonic() - t0, 1)
        _check_result(res, job)
        return _commit(out_root, job, attempt, res, seconds)
    except (KeyboardInterrupt, SystemExit):
        shutil.rmtree(attempt, ignore_errors=True)
        raise
    except BaseException as error:  # noqa: BLE001
        err = error
    return _fail_build(out_root, job, attempt, err, t0)


def build_module(module_dir, lod, out_root=None, gpu=True, force=False):
    """Build every layer slot of one generated module (models/<set>/<job>/<race>/<module>) at reduction level `lod` ('0p4' | '0p2' | '0p1').
    out_root defaults to <module_dir>/layers. `force` builds even when a valid build with this fingerprint is published (the old one is never touched;
    the new build gets a '-r<n>' id). Returns the build record (the pointer's content) with status 'ok' (built now), 'current' (the same fingerprint was
    already published and validates; nothing done) or 'failed' (error + traceback; the pointers and earlier builds are untouched).
    ValueError for a bad lod or a module dir that does not exist."""
    if lod not in LODS:
        raise ValueError(f'lod must be one of {LODS}, got {lod!r}')
    module_dir = Path(module_dir).resolve()
    if not module_dir.is_dir():
        raise ValueError(f'module dir not found: {module_dir}')
    out_root = Path(out_root) if out_root else module_dir / 'layers'
    mp_ = _rel(module_dir)
    kind, race = module_dir.name, module_race(module_dir)
    if kind not in MODULE_KINDS:
        return _fail_early(out_root, mp_, lod, f'unknown module {kind!r} (expected one of {MODULE_KINDS})', lod)
    missing = [str(module_dir / x) for x in (f'reduced/lod_error_{lod}.glb', 'reduced/manifest.json', 'fit/align.json') if not (module_dir / x).is_file()]
    if missing:
        return _fail_early(out_root, mp_, lod, 'missing input: ' + ', '.join(missing), lod)
    problem = _race_problem(race)
    if problem:
        return _fail_early(out_root, mp_, lod, problem, lod)
    try:
        fp = fingerprint_module(module_dir, lod)
    except OSError as error:
        return _fail_early(out_root, mp_, lod, f'cannot read the inputs: {type(error).__name__}: {error}', lod)

    def run(attempt):
        import layers
        return layers.module_layers(module_dir, lod, attempt)
    return _publish(out_root, _Job(lod, lod, race, kind, mp_, fp, lambda: fingerprint_module(module_dir, lod), gpu, force, run))


def build_bare(race, out_root=None, gpu=True, force=False):
    """Build the bare layers of one race (head, torso, lower_arm.r/l, lower_leg.r/l where the body has legs: the game body's own faces). out_root defaults
    to tools/preview/cache/layers/<race>. Same record / publish scheme as build_module (build_id 'bare-<fp[:12]>', pointer current_bare.json, no LOD).
    An invalid race (not a single identifier of an existing game body cache) returns a failed result and writes nothing."""
    problem = _race_problem(race)
    if problem:
        return _fail_early(None, f'bare:{race}', None, problem, 'bare', write=False)
    out_root = Path(out_root) if out_root else BARE_ROOT / race            # (race is a validated identifier: this stays inside BARE_ROOT)
    try:
        fp = fingerprint_bare(race)
    except OSError as error:
        return _fail_early(out_root, f'bare:{race}', None, f'cannot read the inputs: {type(error).__name__}: {error}', 'bare')

    def run(attempt):
        import layers
        return layers.bare_layers(race, attempt)
    return _publish(out_root, _Job('bare', None, race, 'bare', f'bare:{race}', fp, lambda: fingerprint_bare(race), gpu, force, run))


# ----------------------------------------------------------------------------------------------- parity

def _read_glb(path, morph=None, colours=False):
    p = str(HERE / 'checks')
    if p not in sys.path:
        sys.path.insert(0, p)
    import glbread2
    return glbread2.read(path, morph=morph, colours=colours)


def _tri_rows(V0, V1, F):
    """Per face: its corners in CYCLIC order (winding kept) starting at the corner with the smallest position key at weight 0 (so faces match whatever the
    primitive order), as a key row + the corner positions at weight 0 and 1 in that order, and the face order that sorts the keys."""
    import numpy as np
    P0, P1 = V0[F], V1[F]
    q = np.round((P0 + 4.0) * 1e5).astype(np.int64)
    kk = (q[..., 0] * 800001 + q[..., 1]) * 800001 + q[..., 2]
    o = (np.argmin(kk, axis=1)[:, None] + np.arange(3)[None]) % 3
    kk = np.take_along_axis(kk, o, 1)
    rows = np.lexsort((kk[:, 2], kk[:, 1], kk[:, 0]))

    def take(P):
        return np.take_along_axis(P, o[..., None], 1)[rows]
    return kk[rows], take(P0), take(P1), rows


def compare_glb(ref_glb, new_glb, tie_max=12, pos_tol_mm=0.05):
    """Compare two layer GLBs: face / vertex counts, per-material face counts and, with faces matched by position AND winding (the primitive order may
    differ), the vertex distance (mm) at morph weight 0 and 1, the face material names and their colour values (baseColorFactor).
    A face whose material differs only in its garment ROLE (both roles non-skin, same provenance) is a tie-break face (the old hash-seed-dependent vote
    of geodiv.relabel_off_body_skin; reported separately, at most `tie_max` of them are accepted); any other difference is real.
    Returns the measurements plus 'ok' and 'problems'."""
    import numpy as np
    sys.path.insert(0, str(HERE / 'checks'))
    import glbread2
    V0, F0, M0, C0 = _read_glb(ref_glb, colours=True)
    V0f = _read_glb(ref_glb, morph='fill')[0]
    V1, F1, M1, C1 = _read_glb(new_glb, colours=True)
    V1f = _read_glb(new_glb, morph='fill')[0]
    out = {'faces_ref': int(len(F0)), 'faces_new': int(len(F1)), 'verts_ref': int(len(V0)), 'verts_new': int(len(V1))}
    mats0 = {m: int((M0 == m).sum()) for m in sorted(set(M0.tolist()))}
    mats1 = {m: int((M1 == m).sum()) for m in sorted(set(M1.tolist()))}
    out['roles_equal'] = mats0 == mats1
    out['roles_ref'], out['roles_new'] = mats0, mats1
    out['w0_max_mm'] = out['w0_mean_mm'] = out['w1_max_mm'] = out['w1_mean_mm'] = None
    out['faces_matched'] = False
    out['tie_break_faces'] = out['role_diff_faces'] = out['colour_diff_faces'] = None
    problems = []
    if len(F0) == len(F1):
        k0, a0, a1, r0 = _tri_rows(V0, V0f, F0)
        k1, b0, b1, r1 = _tri_rows(V1, V1f, F1)
        out['faces_matched'] = bool((k0 == k1).all())
        if out['faces_matched']:
            m0, m1 = M0[r0], M1[r1]
            role0, prov0 = glbread2.split_names(m0)
            role1, prov1 = glbread2.split_names(m1)
            diff = m0 != m1
            tie = diff & (prov0 == prov1) & (role0 != 'skin') & (role1 != 'skin')
            out['tie_break_faces'] = int(tie.sum())
            out['role_diff_faces'] = int((diff & ~tie).sum())
            out['colour_diff_faces'] = int((np.abs(C0[r0] - C1[r1]).max(1) > 1e-6)[~tie].sum())
            d0 = np.linalg.norm(a0 - b0, axis=2)
            d1 = np.linalg.norm(a1 - b1, axis=2)
            out.update(w0_max_mm=float(d0.max() * 1000), w0_mean_mm=float(d0.mean() * 1000), w1_max_mm=float(d1.max() * 1000), w1_mean_mm=float(d1.mean() * 1000))
    if out['faces_ref'] != out['faces_new']:
        problems.append(f"face count {out['faces_ref']} vs {out['faces_new']}")
    elif not out['faces_matched']:
        problems.append('faces differ in position or winding')
    else:
        if out['role_diff_faces']:
            problems.append(f"{out['role_diff_faces']} faces differ in role / provenance (not a tie-break)")
        if out['tie_break_faces'] > tie_max:
            problems.append(f"{out['tie_break_faces']} tie-break role differences (more than {tie_max})")
        if out['colour_diff_faces']:
            problems.append(f"{out['colour_diff_faces']} faces differ in colour")
        if max(out['w0_max_mm'], out['w1_max_mm']) > pos_tol_mm:
            problems.append(f"vertex positions differ by {max(out['w0_max_mm'], out['w1_max_mm']):.3f} mm")
    out['ok'], out['problems'] = not problems, problems
    return out


def _json_diff(a, b, path='', out=None, tol=1e-6):
    """Leaves that differ between two notes trees (floats within tol equal); timing / bookkeeping keys are skipped."""
    out = [] if out is None else out
    skip = ('seconds', 'code_mtime', 'glb', 'module', 'module_path', 'set', 'job', 'ring_original_merged', 'ring_line_vertices')   # (the last two: added since the prototype)
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k in skip:
                continue
            if k not in a or k not in b:
                out.append(f'{path}/{k}: only in {"ref" if k in a else "new"}')
            else:
                _json_diff(a[k], b[k], f'{path}/{k}', out, tol)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f'{path}: length {len(a)} vs {len(b)}')
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                _json_diff(x, y, f'{path}[{i}]', out, tol)
    elif isinstance(a, float) or isinstance(b, float):
        try:
            if abs(float(a) - float(b)) > tol * max(1.0, abs(float(a))):
                out.append(f'{path}: {a} vs {b}')
        except (TypeError, ValueError):
            out.append(f'{path}: {a} vs {b}')
    elif a != b:
        out.append(f'{path}: {a} vs {b}')
    return out


def _parity_fail(error, **extra):
    return dict({'status': 'failed', 'error': error, 'problems': [error], 'slots': {}}, **extra)


def parity(ref_dir, ref_set, new_dir, lod=None, tie_max=12):
    """Compare a new build with the prototype's <ref_dir>/<slot>__<ref_set>.glb. new_dir is a build folder (has build.json) or an out_root with
    pointers (current_<lod>.json; `lod` picks one, else it must be the only one). Checks the expected slot set (the build's required slots must exist in
    the reference, the build and the reference must have the same slots), then per slot compare_glb (+ notes and the mirrored flag). A missing input, a
    missing / extra slot or any difference beyond the tie-break is a failure. Returns {'status': 'ok'|'failed', 'slots': {slot: result}, 'problems': [...],
    'tie_break': {slot: n}, 'build_id', 'kind', 'lod'}."""
    ref_dir, new_dir = Path(ref_dir), Path(new_dir)
    if not ref_dir.is_dir():
        return _parity_fail(f'reference folder not found: {ref_dir}')
    if not new_dir.is_dir():
        return _parity_fail(f'build folder not found: {new_dir}')
    if not (new_dir / 'build.json').is_file():
        ptrs = {p.name[len('current_'):-len('.json')]: p for p in new_dir.glob('current_*.json')}
        pick = lod if lod else (next(iter(ptrs)) if len(ptrs) == 1 else None)
        if pick is None or pick not in ptrs:
            return _parity_fail(f'no build.json in {new_dir} and no unambiguous pointer (pass --lod; found: {sorted(ptrs) or "none"})')
        rec = _read_json(ptrs[pick])
        if not isinstance(rec, dict) or not rec.get('build_id'):
            return _parity_fail(f'pointer {ptrs[pick].name} unreadable')
        new_dir = new_dir / rec['build_id']
    rec = _read_json(new_dir / 'build.json')
    if not isinstance(rec, dict):
        return _parity_fail(f'build.json missing or unreadable in {new_dir}')
    problems = [f'build: {p}' for p in validate_build(new_dir)]
    kind = rec.get('kind')
    new_slots = sorted(p.stem for p in new_dir.glob('*.glb'))
    ref_slots = sorted(k for k in SLOT_ALLOWED.get(kind, ()) if (ref_dir / f'{k}__{ref_set}.glb').is_file())
    required = list(rec.get('required') or [])
    for k in required:
        if k not in ref_slots:
            problems.append(f'required slot {k} has no reference {k}__{ref_set}.glb')
    for k in sorted(set(ref_slots) - set(new_slots)):
        problems.append(f'slot {k} is in the reference but not in the build')
    for k in sorted(set(new_slots) - set(ref_slots)):
        problems.append(f'slot {k} is in the build but not in the reference')
    if not new_slots:
        problems.append('the build has no GLB')
    slots, tie = {}, {}
    for slot in sorted(set(new_slots) & set(ref_slots)):
        ref, g = ref_dir / f'{slot}__{ref_set}.glb', new_dir / f'{slot}.glb'
        r = compare_glb(ref, g, tie_max=tie_max)
        jr, jn = _read_json(ref.with_suffix('.json')), _read_json(g.with_suffix('.json'))
        r['notes_diff'] = _json_diff(jr.get('notes'), jn.get('notes')) if jr and jn else ['no json']
        r['mirrored_flag_equal'] = bool((jr or {}).get('mirrored_from_right') == (jn or {}).get('mirrored_from_right'))
        if r['notes_diff'] and not r['tie_break_faces']:             # (a tie-break face may also change a count in the notes)
            r['ok'] = False
            r['problems'].append(f"notes differ ({len(r['notes_diff'])}: {r['notes_diff'][0][:100]})")
        if not r['mirrored_flag_equal']:
            r['ok'] = False
            r['problems'].append('mirrored_from_right differs')
        if r['tie_break_faces']:
            tie[slot] = r['tie_break_faces']
        problems += [f'{slot}: {p}' for p in r['problems']]
        slots[slot] = r
    return {'status': 'failed' if problems else 'ok', 'build_id': rec.get('build_id'), 'kind': kind, 'lod': rec.get('lod'), 'required': required,
            'slots': slots, 'tie_break': tie, 'problems': problems}


# ----------------------------------------------------------------------------------------------- CLI

class _ArgError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _ArgError(message)


def _emit(res, code=None):
    print(json.dumps(res, default=float), flush=True)
    if code is None:
        code = 0 if res.get('status') in ('ok', 'current') else 1
    return code


def main(argv=None):
    try:
        return _main(argv)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        return _emit({'status': 'failed', 'error': 'interrupted'}, 1)
    except BaseException as error:  # noqa: BLE001
        return _emit({'status': 'failed', 'error': f'{type(error).__name__}: {error}', 'traceback': traceback.format_exc()}, 1)


def _main(argv):
    ap = _Parser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('module')
    a.add_argument('module_dir')
    a.add_argument('--lod', required=True, choices=LODS)
    a.add_argument('--out')
    a.add_argument('--cpu', action='store_true')
    a.add_argument('--force', action='store_true')
    b = sub.add_parser('bare')
    b.add_argument('race')
    b.add_argument('--out')
    b.add_argument('--cpu', action='store_true')
    b.add_argument('--force', action='store_true')
    c = sub.add_parser('parity')
    c.add_argument('ref_dir')
    c.add_argument('ref_set')
    c.add_argument('new_dir')
    c.add_argument('--lod', choices=LODS)
    c.add_argument('--json')
    try:
        args = ap.parse_args(argv)
    except _ArgError as error:
        return _emit({'status': 'failed', 'error': f'argument error: {error}', 'previous_build': None}, 1)
    if args.cmd == 'parity':
        res = parity(args.ref_dir, args.ref_set, args.new_dir, lod=args.lod)
        if args.json:
            try:
                Path(args.json).write_text(json.dumps(res, indent=1, default=float), encoding='utf-8')
            except OSError as error:
                res['problems'].append(f'cannot write {args.json}: {error}')
                res['status'] = 'failed'
        return _emit(res, 0 if res['status'] == 'ok' else 1)
    if args.cmd == 'module':
        out = Path(args.out) if args.out else Path(args.module_dir) / 'layers'
        tag = args.lod
    else:
        out = Path(args.out) if args.out else BARE_ROOT / args.race
        tag = 'bare'
    try:
        if args.cmd == 'module':
            res = build_module(args.module_dir, args.lod, args.out, gpu=not args.cpu, force=args.force)
        else:
            res = build_bare(args.race, args.out, gpu=not args.cpu, force=args.force)
    except ValueError as error:                                       # bad arguments (no such module dir)
        res = {'status': 'failed', 'error': f'ValueError: {error}', 'previous_build': _previous_build(out, tag)}
    except Exception as error:  # noqa: BLE001
        res = {'status': 'failed', 'error': f'{type(error).__name__}: {error}', 'traceback': traceback.format_exc(), 'previous_build': _previous_build(out, tag)}
    return _emit(res)


if __name__ == '__main__':
    sys.exit(main())
