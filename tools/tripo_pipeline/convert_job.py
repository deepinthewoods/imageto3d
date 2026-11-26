"""Convert one staged four-view job: Tripo Studio submit/collect, then reduction, module by module.

Usage (from the imageto3d folder):
  python3 tools/tripo_pipeline/convert_job.py <set>--<job-id> [--dry-run] [--separable] [--piece-colours]
      [--split-method segment-first|per-face|none]
      [--backend tripo|hyper3d|formy3d|pixal3d] [--allow-paid --accept-preview]   (--allow-paid: hyper3d + formy3d, --accept-preview: hyper3d)
      [--max-submits N] [--wearable-method cut|faces] [--knee-cops] [--feature-labels studs|rings[:secondary|:void]|none]
      [--no-align]

- Takes the job from multiview_ready/ (moving it to processing/) or continues one already in processing/.
- Modules run strictly one after another: Studio disables Generate while another model of the
  account is generating, and studio.mjs waits for that anyway.
- Resumable: a module with a submission record is collected, not resubmitted (no double charge);
  a collected module with reduced/manifest.json is skipped.
- Outputs per module in models/<set>/<job-id>/<race>/<module>/: Tripo OBJ/GLB, source.glb,
  reduced/ (0.4/0.2/0.1% GLBs, wearable GLBs, review.blend, wearable.blend); highpoly.blend only on demand.
  reduce_model.py finds module, swatches, cameras and body regions from that layout.
- A module whose Tripo mesh is broken (currently: a doubled interior shell, see reduce_model's
  interior audit) is regenerated with the Studio project's free retries (studio.mjs retry); each
  failed attempt is kept as <module>_attemptNN_<reason>/. When the free retries are used up it never
  pays for a new generation: the last attempt stays as the module output, the module is flagged
  `free_retries_exhausted` / `needs_manual_review` in the job record, the job state becomes
  `converted_with_errors`, and the script prints ERROR lines and exits 1.
- Split step (after the reduction, resumable, never fails the conversion): a `piece_colours` `body_armor` module is cut
  into rigid pieces by SEGMENT-FIRST (the default): segment_first_prep.py in Blender -> <module>/segment_first/prep/, then
  segment_first.py (venv python with numpy + scipy) -> <module>/segment_first/{levels,pieces,segment_first.json}. If that
  fails it falls back to `split_pieces.py <module>/reduced --rigid auto` -> <module>/pieces/ and prints/records why.
  `--split-method per-face` runs only that fallback path, `none` skips the step (the default for every other job, including
  separable joint-band jobs). The method used is recorded in .tripo-job.json module_conversion.<race>/<module>.split and in
  <module>/split_step.json.
- Align step (after the split, before the job record is updated, never fails the conversion; not in --dry-run): `align_body.py <module>`
  aligns the wearable and the rigid pieces to the game base body (reduced/lod_error_<lv>__wearable_aligned.glb, lod_<lv>_aligned/,
  fit/align.json, red-body overlays), then `body_fit.py <module> --source aligned` fits them out of the body (..._aligned_fitted.*,
  fit/body_fit_aligned.json); about 2 minutes per module. Both scripts skip a module whose lock is held, so this process passes its pid
  in $MODULE_LOCK_OWNER (rewear.module_busy treats a lock held by that pid as free). Log: <module>/align_step.log; record:
  <module>/align_step.json and module_conversion.<race>/<module>.align {status done|failed|skipped, align {residual_mm before/after,
  warnings}, fit {warnings, levels.<lv> {poke_after_cm2, max_displacement_mm}}, error?}. `--no-align` skips it, as does `config.json`
  `"align": {"enabled": false}`; modules converted without it are backfilled with `align_body.py --all` then
  `body_fit.py --all --source aligned`.
- Winding step (after the align step; 2026-11-22, never fails the conversion): `fix_winding.py <module> --apply` turns flipped faces
  of every module GLB (reduced/*.glb, rigid pieces) the right way round: faces wound against their neighbours and closed pieces that
  are inside-out (not cavities). Only those triangles' corner order changes. Record: module_conversion.<race>/<module>.winding
  {status, glbs, glbs_fixed, faces_fixed, pieces_needing_review}; per-GLB detail <module>/reduced/winding.json; originals of changed
  GLBs kept as <name>.glb.prewinding. Older modules: `fix_winding.py --all --apply`.
- Three-view modules: a module folder with `views_omitted.json` (or a job record entry `views_omitted`) is submitted with
  the views that exist (the backends skip the omitted slot) and reduced with `--omit-views <view>`, so surfaces only the
  omitted camera saw count as unseen in the texture-visibility weighting. Recorded in module_conversion.<key>.views_omitted.
- Knee cops (body_armor only): a module whose prompts json / job record says `"knee_cops": true` (or `--knee-cops`) is reduced
  with `reduce_model.py --knee-cops` (the base's knee band becomes the body part knee_cop: garment-allowed prior, no bare cost in the
  wearable cut; the rigid split cuts the cop as its own sections knee_l / knee_r under the thighs) and its segment-first prep gets
  the same flag and band from the manifest. Recorded in
  reduced/manifest.json `knee_cops` and .tripo-job.json module_conversion.<race>/<module>.knee_cops.
- Wearable (garment alone, base removed): reduce_model.py cuts it out of the high-poly mesh (`--wearable-method cut`, the default: clean
  openings, exact left/right pairs, wearcut.py) and falls back to the faces of the reduced level (`faces`, the previous behaviour) per
  level on any failure; `--wearable-method faces` here forces the old behaviour. The method actually used and any fallback reason are
  recorded in .tripo-job.json module_conversion.<race>/<module>.wearable (and in reduced/manifest.json `wearable`).
- Feature label passes (opt-in, off unless chosen; feature_hooks.py, README "Feature label passes"): `--feature-labels SPEC` here, else the
  job record's `feature_labels` (a spec, or {"default": spec, "<module>": spec}), else `config.json` `feature_labels.sets[<set id>]`, picks a
  pass over the high-poly labels: `studs` (round studs keep the tertiary colour), `rings:secondary` (chain-mail ring tubes / centres) or
  `rings:void` (ring centres removed). It is passed to `reduce_model.py --feature-labels` (the cut wearable and the labelled levels) and to
  `segment_first.py --feature-labels` (the rigid pieces); `none` switches a configured choice off. Recorded in
  module_conversion.<race>/<module>.feature_labels (spec, where it came from) and reduced/manifest.json `wearable.feature_labels`.
- Leaves the job in processing/ with state `converted` and prints which levels need review.
  Moving it to completed/ is a separate step after that review.
- Pause / conversion hold (tools/pipeline_supervisor.py; DESIGN_PHASE4 I.1): while `.codex_jobs/_batch/CONVERT_HOLD` exists (the preview
  app's service manager keeps it while the supervisor is stopped, restarting or has not completed a tick yet) or `.codex_jobs/_batch/PAUSE`
  exists with "conversions" in its scope (an unreadable PAUSE counts), the process waits (prints once) before EVERY paid step: a new
  submission (Tripo, Hyper3D, formy3d; also a pixal3d one, as before), and a Hyper3D collect with --allow-paid of a module that was
  submitted earlier (its geometry/material confirmations cost credits). The free Tripo collect and the free Tripo retry do not wait;
  heavy local (Blender) steps are frozen by the supervisor as before.
- Tripo browser lock (DESIGN_PHASE4 I.3): every studio.mjs call (submit, collect, retry) runs under
  `.codex_jobs/_batch/browser_tripo.lock` and the reservation of the resolved Tripo Firefox profile (tripo_browser_lock.py; the child
  gets $IMAGETO3D_TRIPO_PROFILE_LOCK / _TOKEN and studio.mjs verifies them), the locks the convert worker's and the preview app's balance
  checks take; a busy lock is waited for. A paid step re-checks the pause/hold after it got the lock.
- Click-time paid gate (DESIGN_PHASE4 I.8, tools/paid_gate.py): the paid children (a Tripo submit's studio.mjs, every rodin_backend.py
  command) get $IMAGETO3D_PAID_GATE (`paid_gate.env_for(ROOT)`, or the one this process inherited) and check it again right before
  their credit-capable click. A child that stopped there exits 4 (nothing clicked; its record unchanged or set aside): this process
  waits for the gate again and repeats the step; it is never counted as a failure.
- Drain (DESIGN_PHASE4 I.10; pre-cut-over review finding 1): while this process is BLOCKED at the gate (nothing paid in flight) it keeps
  `.codex_jobs/_batch/gate_wait/<pid>.json` = {pid, create_time, job, module, step, since} (removed when it leaves the gate). When
  `.codex_jobs/_batch/CONVERT_DRAIN` exists while it waits (the service manager drains an older convert worker; the file alone also
  closes the gate), it releases its module lock, marks the module in the job record's `deferred` (not a failure; removed when the module
  converts), writes `.codex_jobs/_batch/drain_exits/<pid>_<ms>.json` = {pid, ppid, job, module, step, time, fails_before, parent_aware,
  lock_released} and exits 76 (DRAIN_EXIT; not 75, which is a busy Tripo browser profile). A current convert_queue.py (it sets $IMAGETO3D_DRAIN_AWARE) never counts that exit as a
  failure; the manager restores the failure count an older worker wrote for it. Re-running the job continues it.
--dry-run: opens Studio, fills the form and screenshots it without pressing Generate.
--max-submits N: submit at most N new modules in this run (e.g. what the Tripo credit balance covers); the others are
  skipped, the job stays in processing/ with state `partially_converted` and the script exits 3. Re-run (e.g. with
  another --backend) to continue. A module that already has a submission record is always collected with the backend
  that submitted it, whatever --backend says, so a job can mix backends.
--backend hyper3d: optional alternative image->3D step (Hyper3D Rodin, high-poly textured GLB, no Smart
  Low-poly; tools/hyper3d_pipeline/README.md). Tripo stays the default. Submit starts only the FREE preview;
  collect needs --allow-paid --accept-preview and a configured expected credit cost, otherwise it stops with
  a message (exit 1) and the module stays resumable. The Tripo free-retry loop is skipped for hyper3d
  (its retries cost credits): a broken mesh is only flagged for manual review.
--backend formy3d: optional third backend, Tripo3D H3.1 multi-view through formy3d.com (browser runner in
  tools/formy3d_pipeline/, textured GLB, no preview stage). Submit is the only paid step: it needs --allow-paid and a
  displayed Generate price equal to config.json expected_costs.generate, otherwise it stops with a message (exit 1) and
  nothing is clicked; collect (free download) waits for the model and fetches the GLB. --dry-run fills the form only.
  Like hyper3d it has no free-retry loop. See tools/formy3d_pipeline/README.md.
--backend pixal3d: optional FREE local backend (user, 2026-11-18): Pixal3D, camera-aware multi-view TRELLIS.2, on this PC's GPU
  through WSL (tools/pixal3d_pipeline/, ~4-5 min per module). Submit only stages the views and the job's frozen cameras; the
  generation runs at collect (resumable). Its output is closed, one component, ~1M faces, 2048 atlas, turned to Tripo's facing.
  A mesh the reduction flags as broken is regenerated with the next seed, up to config.json `retries` times (no cost).
  See tools/pixal3d_pipeline/README.md.
"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / 'tools/tripo_pipeline'
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
import feature_hooks  # noqa: E402
import portable  # noqa: E402
import views_omitted  # noqa: E402
from tripo_browser_lock import tripo_browser_lock  # noqa: E402
from wearcut_rules import wearable_record  # noqa: E402
HYPER3D = ROOT / 'tools/hyper3d_pipeline'
FORMY3D = ROOT / 'tools/formy3d_pipeline'
PIXAL3D = ROOT / 'tools/pixal3d_pipeline'
BACKENDS = ('tripo', 'hyper3d', 'formy3d', 'pixal3d')
PAUSE = ROOT / '.codex_jobs/_batch/PAUSE'   # tools/pipeline_supervisor.py: no paid step while it exists (scope "conversions")
CONVERT_HOLD = ROOT / '.codex_jobs/_batch/CONVERT_HOLD'   # preview app service manager (DESIGN_PHASE4 I.1): no paid step while it exists
_IMPORT_ROOT = ROOT
GATE_POLL = 15   # seconds between looks at PAUSE / CONVERT_HOLD while a paid step waits
DRAIN_EXIT = 76  # exit code (75 = Tripo browser busy, tripo_browser_lock.EXIT_BUSY): left a closed gate because CONVERT_DRAIN exists; the module is deferred, never a failure
DRAIN_AWARE_ENV = 'IMAGETO3D_DRAIN_AWARE'   # set by a convert_queue.py that does not count DRAIN_EXIT as a failure
_RUN = {'job': None, 'record': None, 'lock': None}   # main(): the job name, its record path and the module lock this process holds
BLENDER = portable.default_blender()   # $BLENDER, else /usr/local/bin/blender (Linux) / the newest installed Blender (Windows)
portable.utf8_environment()            # Windows: child python / Blender processes read and write UTF-8
SPLIT_METHODS = ('segment-first', 'per-face', 'none')
WEARABLE_METHODS = ('cut', 'faces')


def _load_paid_gate():
    """tools/paid_gate.py by path (tools/ never joins sys.path), shared through sys.modules['paid_gate']."""
    import importlib.util
    if 'paid_gate' in sys.modules:
        return sys.modules['paid_gate']
    spec = importlib.util.spec_from_file_location('paid_gate', Path(__file__).resolve().parents[1] / 'paid_gate.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules['paid_gate'] = module
    spec.loader.exec_module(module)
    return module


paid_gate = _load_paid_gate()
GATE_CLOSED = paid_gate.EXIT_GATE_CLOSED   # a paid child stopped at a closed gate before its click: wait and repeat the step


def gate_env():
    """{IMAGETO3D_PAID_GATE: ...} for a paid child: the gate this process was given (a caller's own gate stays in force), else
    CONVERT_HOLD / CONVERT_DRAIN / PAUSE / supervisor_pause.json / the manual pause mode of ROOT (paid_gate.file_reason)."""
    inherited = os.environ.get(paid_gate.ENV)
    return {paid_gate.ENV: inherited} if inherited else paid_gate.env_for(ROOT)


def write_record(path, record):
    path.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')


def gate_files():
    """(PAUSE, CONVERT_HOLD) of the current ROOT (a test that points ROOT at a temp root gets that root's files; one that
    patched the constants keeps them)."""
    if ROOT == _IMPORT_ROOT:
        return PAUSE, CONVERT_HOLD
    batch = ROOT / '.codex_jobs' / '_batch'
    return batch / 'PAUSE', batch / 'CONVERT_HOLD'


def _gate_json(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def drain_files():
    """(CONVERT_DRAIN, gate_wait/, drain_exits/) under the batch folder of the current ROOT (DESIGN_PHASE4 I.10)."""
    batch = gate_files()[1].parent
    return batch / 'CONVERT_DRAIN', batch / 'gate_wait', batch / 'drain_exits'


def drain_requested():
    """CONVERT_DRAIN exists (readable or not): a process blocked at the gate leaves instead of waiting."""
    return drain_files()[0].exists()


def conversion_gate():
    """Why a paid step must wait right now, else None: CONVERT_HOLD exists, or PAUSE exists and its scope covers conversions
    (a PAUSE without a readable scope counts: fail closed), or CONVERT_DRAIN exists (a drain without its hold still closes the gate),
    or the supervisor's own pause (supervisor_pause.json) covers conversions, or the manual mode in supervisor_mode.json is `pause` /
    `pause-conversions` (post-cut-over review, blockers 1 and 3: the mode file closes the gate by itself, at once; the supervisor's
    PAUSE reflection of it, up to a 30 s tick later, is not waited for). = paid_gate.file_reason, which the paid children check
    again under the dispatch lock."""
    pause, hold = gate_files()
    if hold.exists():
        data = _gate_json(hold) or {}
        return f'CONVERT_HOLD: {data.get("reason") or "conversion hold"}'
    if drain_requested():
        return f'CONVERT_DRAIN: {(_gate_json(drain_files()[0]) or {}).get("reason") or "conversion drain"}'
    if pause.exists():
        data = _gate_json(pause)
        scope = (data or {}).get('scope')
        if not isinstance(scope, list) or 'conversions' in scope:
            return f'PAUSE: {(data or {}).get("reason") or "supervisor pause"}'
    return paid_gate.supervisor_pause_reason(pause.parent) or paid_gate.mode_reason(pause.parent)


def _own_create_time():
    try:
        import psutil
        return psutil.Process(os.getpid()).create_time()
    except Exception:   # noqa: BLE001  (no psutil: the pid alone names this process)
        return None


def _write_json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, path)


def gate_wait_path():
    return drain_files()[1] / f'{os.getpid()}.json'


def gate_wait_enter(key, step):
    """Record that this process is blocked at the gate (nothing paid in flight): gate_wait/<pid>.json. Never raises."""
    try:
        _write_json_atomic(gate_wait_path(), {'pid': os.getpid(), 'create_time': _own_create_time(), 'job': _RUN.get('job'), 'module': key,
                                              'step': step, 'since': time.time()})
    except OSError:
        pass


def gate_wait_leave():
    try:
        gate_wait_path().unlink(missing_ok=True)
    except OSError:
        pass


def _fails_before():
    """The job's current failure count (`convert_locks/<job>.fails`): 0 when absent, None when unknown."""
    if not _RUN.get('job'):
        return None
    path = gate_files()[1].parent / 'convert_locks' / f'{_RUN["job"]}.fails'
    try:
        return int(path.read_text(encoding='utf-8').strip())
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        return None


def leave_for_drain(key, step, reason):
    """Leave a closed gate because CONVERT_DRAIN exists (DESIGN_PHASE4 I.10): nothing paid is in flight here. The module lock this process
    holds is released, the module is marked `deferred` in the job record (not a failure, not a state change), the exit is recorded in
    drain_exits/ for the manager (which restores the failure count an older worker writes for a non-zero exit) and the process exits
    DRAIN_EXIT. Every step is best effort: the exit itself always happens."""
    now = time.time()
    lock, released = _RUN.get('lock'), False
    try:
        if lock is not None and lock_holder(lock) == os.getpid():
            lock.unlink(missing_ok=True)
            released = True
    except OSError:
        pass
    _RUN['lock'] = None
    note = {'time': time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(now)), 'step': step, 'reason': reason, 'pid': os.getpid()}
    if _RUN.get('record') is not None:
        try:
            update_record(_RUN['record'], lambda r: r.setdefault('deferred', {}).__setitem__(key, note))
        except (OSError, ValueError) as error:
            print(f'== {key}: the deferral could not be written to the job record ({error})', flush=True)
    try:
        _write_json_atomic(drain_files()[2] / f'{os.getpid()}_{int(now * 1000)}.json',
                           {'pid': os.getpid(), 'create_time': _own_create_time(), 'ppid': os.getppid(), 'job': _RUN.get('job'),
                            'module': key, 'step': step, 'reason': reason, 'time': now, 'fails_before': _fails_before(),
                            'parent_aware': bool(os.environ.get(DRAIN_AWARE_ENV)), 'lock_released': released})
    except OSError as error:
        print(f'== {key}: the drain exit could not be recorded ({error})', flush=True)
    print(f'== {key}: DEFERRED (CONVERT_DRAIN while waiting at the gate: {reason}); nothing paid in flight, module lock '
          f'{"released" if released else "not held"}; exit {DRAIN_EXIT} before {step}', flush=True)
    gate_wait_leave()
    raise SystemExit(DRAIN_EXIT)


def wait_for_gate(key, step):
    """Block (printing once) while conversion_gate() says a paid step must wait; returns at once when it is open. While it blocks it
    keeps gate_wait/<pid>.json; with CONVERT_DRAIN it leaves through leave_for_drain (exit DRAIN_EXIT) instead of waiting."""
    announced = False
    try:
        while True:
            reason = conversion_gate()
            if not reason:
                break
            if drain_requested():
                leave_for_drain(key, step, reason)
            if not announced:
                print(f'== {key}: PAUSED ({reason}); waiting before {step}', flush=True)
                announced = True
                gate_wait_enter(key, step)
            time.sleep(GATE_POLL)
    finally:
        if announced:
            gate_wait_leave()
    if announced:
        print(f'== {key}: pause/hold lifted; {step}', flush=True)


def run_studio(command, key, step, paid, check=False, env=None, **kwargs):
    """subprocess.run(command, **kwargs) of a studio.mjs call under the Tripo browser lock and profile reservation (waited for; the
    child gets the reservation in its environment). A paid step first waits for the pause/hold gate, checks it again once it holds
    the lock (a pause may have started while it waited) and passes the gate to the child, which checks it right before the Generate
    click; a child that stopped there (exit GATE_CLOSED: nothing clicked, its record set aside) is waited for and run again.
    `check`: raise CalledProcessError on any other non-zero exit."""
    while True:
        if paid:
            wait_for_gate(key, step)
        with tripo_browser_lock(ROOT, what=f'convert_job {key} {step}',
                                announce=lambda text: print(f'== {key}: {text} before {step}', flush=True)) as reservation:
            if paid and conversion_gate():
                continue
            child_env = dict(os.environ if env is None else env, **(reservation or {}), **(gate_env() if paid else {}))
            run = subprocess.run(command, env=child_env, **kwargs)
        if paid and run.returncode == GATE_CLOSED:
            print(f'== {key}: the paid gate closed before the click ({step}); waiting for it, then repeating', flush=True)
            wait_for_child_gate(key, step, child_env)
            continue
        if check and run.returncode:
            raise subprocess.CalledProcessError(run.returncode, command)
        return run


def run_paid_child(command, key, step, paid, **kwargs):
    """subprocess.run of a non-Tripo backend command that may click a paid control (rodin_backend.py). The child always gets the
    gate; a paid run waits for it first, and a child exit GATE_CLOSED (it stopped before a credit-capable click, record unchanged)
    waits for the gate and runs the command again: a resumable wait, never a failure."""
    env = kwargs.pop('env', None)
    while True:
        if paid:
            wait_for_gate(key, step)
        child_env = dict(os.environ if env is None else env, **gate_env())
        run = subprocess.run(command, env=child_env, **kwargs)
        if paid and run.returncode == GATE_CLOSED:
            print(f'== {key}: the paid gate closed before a click ({step}); waiting for it, then resuming', flush=True)
            wait_for_child_gate(key, step, child_env)
            continue
        return run


def wait_for_child_gate(key, step, child_env):
    """After a child's exit GATE_CLOSED: wait (printing once) until the gate the CHILD was given is open again (its extra `cmd`
    included), so a gate that only the child's check sees as closed never turns into a tight respawn loop."""
    announced = False
    try:
        while True:
            reason = paid_gate.closed_reason(child_env)
            if not reason:
                break
            if drain_requested():   # the child stopped before its click: nothing paid in flight (DESIGN_PHASE4 I.10)
                leave_for_drain(key, step, reason)
            if not announced:
                print(f'== {key}: PAUSED ({reason}); waiting before {step}', flush=True)
                announced = True
                gate_wait_enter(key, step)
            time.sleep(GATE_POLL)
    finally:
        if announced:
            gate_wait_leave()


# Several queue workers may convert different modules of one job at once: each module is claimed with a lock
# file, and the job record is only ever read-modified-written under an exclusive lock (portable.file_lock: flock on
# Linux; on Windows a sidecar `<record>.lock` byte lock that readers take exclusively too).
def record_root(path):
    """The checkout whose `.codex_jobs/_batch/record_locks/` carries the lock sidecar of the job record `path` (final-verify
    review, test isolation): ROOT while the record lies under it (every real job; a test that patched ROOT), else the root the
    record itself belongs to, `<root>/<stage>/<job>/<record>`. So a caller that works on another root (convert_queue and the v2
    app in their test fixtures, which import this module with the real ROOT) never opens a lock sidecar in this checkout, and
    readers and writers of one record still agree on the sidecar."""
    resolved = Path(path).resolve()
    try:
        resolved.relative_to(Path(ROOT).resolve())
        return ROOT
    except ValueError:
        pass
    parents = resolved.parents
    return parents[2] if len(parents) > 2 else parents[-1]


def read_record(path):
    with portable.file_lock(path, exclusive=False, sidecar=True, root=record_root(path)):   # the sidecar of the record's own root
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def update_record(path, change):
    with portable.file_lock(path, exclusive=True, sidecar=True, root=record_root(path)):
        record = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        change(record)
        Path(path).write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
        return record


def lock_holder(lock):
    try:
        holder = int(lock.read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        return None
    return holder if portable.pid_alive(holder) else None


def claim_module(lock):
    """Atomically create `lock` holding our pid (link of a complete temp file); take over a dead holder's lock."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    temp = lock.with_name(f'{lock.name}.{os.getpid()}')
    temp.write_text(str(os.getpid()), encoding='utf-8')
    try:
        for _ in range(2):
            try:
                os.link(temp, lock)
                return True
            except FileExistsError:
                holder = lock_holder(lock)
                if holder == os.getpid():
                    return True
                if holder:
                    return False
                lock.unlink(missing_ok=True)
        return False
    finally:
        temp.unlink(missing_ok=True)


def module_converted(record, key, out):
    return key in record.get('module_conversion', {}) and (out / 'reduced' / 'manifest.json').exists()


def hyper3d_python():
    """Interpreter with selenium for the Hyper3D browser runner (config.json, overridable by $HYPER3D_VENV)."""
    if os.environ.get('HYPER3D_VENV'):
        return portable.venv_python_dir(os.environ['HYPER3D_VENV'])
    config = json.loads((HYPER3D / 'config.json').read_text(encoding='utf-8-sig'))
    return portable.backend_python(config['venv_python'], config.get('venv_python_windows'))


def formy3d_python():
    """Interpreter with Playwright for the formy3d browser runner (config.json, overridable by $FORMY3D_VENV)."""
    if os.environ.get('FORMY3D_VENV'):
        return portable.venv_python_dir(os.environ['FORMY3D_VENV'])
    config = json.loads((FORMY3D / 'config.json').read_text(encoding='utf-8-sig'))
    return portable.backend_python(config['venv_python'], config.get('venv_python_windows'))


def pixal3d_retries():
    """How many times a broken pixal3d mesh is regenerated with the next seed (tools/pixal3d_pipeline/config.json `retries`)."""
    try:
        return int(json.loads((PIXAL3D / 'config.json').read_text(encoding='utf-8-sig')).get('retries', 2))
    except (OSError, ValueError):
        return 2


def submit_command(backend, views, out, dry_run=False, options=None):
    if backend == 'pixal3d':   # local and free: submit only stages the inputs, the generation runs at collect
        options = options or {}
        return [sys.executable, str(PIXAL3D / 'pixal3d_backend.py'), 'submit', str(views), str(out)] \
            + (['--dry-run'] if dry_run else []) \
            + (['--seed-offset', str(options['seed_offset'])] if options.get('seed_offset') else [])
    if backend == 'formy3d':
        options = options or {}
        return [formy3d_python(), str(FORMY3D / 'formy3d_backend.py'), 'submit', str(views), str(out)] \
            + (['--dry-run'] if dry_run else []) + (['--allow-paid'] if options.get('allow_paid') else [])
    if backend == 'hyper3d':
        return [hyper3d_python(), str(HYPER3D / 'rodin_backend.py'), 'submit', str(views), str(out)] \
            + (['--dry-run'] if dry_run else [])
    return ['node', str(TOOLS / 'studio.mjs'), 'submit', str(views), str(out)]


def collect_command(backend, out, options=None):
    if backend == 'pixal3d':
        return [sys.executable, str(PIXAL3D / 'pixal3d_backend.py'), 'collect', str(out)]
    if backend == 'formy3d':
        return [formy3d_python(), str(FORMY3D / 'formy3d_backend.py'), 'collect', str(out)]
    if backend == 'hyper3d':
        options = options or {}
        return [hyper3d_python(), str(HYPER3D / 'rodin_backend.py'), 'collect', str(out)] \
            + (['--allow-paid'] if options.get('allow_paid') else []) \
            + (['--accept-preview'] if options.get('accept_preview') else [])
    return ['node', str(TOOLS / 'studio.mjs'), 'collect', str(out)]


def option_value(argv, name, default=None):
    """`--name value` or `--name=value` from argv."""
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(name + '='):
            return arg.split('=', 1)[1]
    return default


def job_is_separable(job, race, module):
    """True when the job's records say separable / joint bands: `.tripo-job.json` (separable, joint_bands), a
    `prompts.json` / `<module>_prompts.json` beside the views or in `.concept_notes/` with such a key or the words
    'joint band' in its text (plain pauldron-only separable jobs do not need the fifth colour). `--separable` on the
    command line forces it."""
    def flagged(path):
        try:
            text = path.read_text(encoding='utf-8-sig')
            data = json.loads(text)
        except (OSError, ValueError):
            return False
        if isinstance(data, dict) and (data.get('joint_bands') or data.get('separable') in ('joint_bands', 'bands')):
            return True
        return any(w in text.lower() for w in ('joint band', 'joint_band'))
    record = job / '.tripo-job.json'
    candidates = [record, job / race / module / 'prompts.json', job / race / f'{module}_prompts.json',
                  job / race / '.concept_notes' / f'{module}_prompts.json', job / race / '.concept_notes' / 'prompts.json']
    return any(flagged(c) for c in candidates if c.is_file())


def job_is_piece_colours(job, race, module):
    """True when the job's records say rigid sections with per-piece base colours (`piece_colours` / `rigid_sections` key,
    or the words 'per-piece base colour' / 'piece colours' in `.tripo-job.json` / `prompts.json`). Such a job uses the
    standard 4-colour palette (no joint role) but the reduction fits the palette hues to the model (`--adaptive-hue on`).
    `--piece-colours` on the command line forces it."""
    def flagged(path):
        try:
            text = path.read_text(encoding='utf-8-sig')
            data = json.loads(text)
        except (OSError, ValueError):
            return False
        if isinstance(data, dict) and (data.get('piece_colours') or data.get('rigid_sections')):
            return True
        return any(w in text.lower() for w in ('per-piece base colour', 'per-piece base color', 'piece colours', 'piece_colours'))
    candidates = [job / '.tripo-job.json', job / race / module / 'prompts.json', job / race / f'{module}_prompts.json',
                  job / race / '.concept_notes' / f'{module}_prompts.json', job / race / '.concept_notes' / 'prompts.json']
    return any(flagged(c) for c in candidates if c.is_file())


def job_is_knee_cops(job, race, module):
    """True when a body_armor module's records say it has knee cops (plate cuisses that continue over the knee and end just below it):
    a truthy `knee_cops` key in the module's prompts json (`<race>/<module>/prompts.json`, `<race>/<module>_prompts.json`,
    `<race>/.concept_notes/...`), in `.tripo-job.json`, or in that module's `.tripo-job.json` `modules[]` entry. Key only, no word
    matching: concept text that says "no knee cops" must not switch it on. Always False for other modules.
    `--knee-cops` on the command line forces it for every body_armor module of the job."""
    if module != 'body_armor':
        return False

    def flagged(path):
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        if data.get('knee_cops'):
            return True
        return any(isinstance(m, dict) and m.get('race_gender') == race and m.get('module') == module and m.get('knee_cops')
                   for m in data.get('modules') or ())
    candidates = [job / '.tripo-job.json', job / race / module / 'prompts.json', job / race / f'{module}_prompts.json',
                  job / race / '.concept_notes' / f'{module}_prompts.json', job / race / '.concept_notes' / 'prompts.json']
    return any(flagged(c) for c in candidates if c.is_file())


def knee_cop_record(info):
    """What a reduction recorded about the knee band (manifest `knee_cops`) for the job record: band fraction, prior source and the
    faces moved per side (module_conversion.<race>/<module>.knee_cops)."""
    cops = info.get('knee_cops') or {}
    out = {'enabled': bool(cops.get('enabled')), 'band_fraction': cops.get('band_fraction'), 'above_fraction': cops.get('above_fraction'),
           'prior': cops.get('prior')}
    if cops.get('sides'):
        out['sides'] = {k: v.get('faces_moved', v.get('reason')) for k, v in cops['sides'].items()}
    return out


def module_omitted_views(job, race, module, entry=None):
    """Views a three-view module is converted without: the module folder's views_omitted.json, else the job record entry
    (`modules[].views_omitted.omitted`). [] for an ordinary four-view module; ValueError for an invalid marker."""
    omitted = views_omitted.omitted_views(job / race / module)
    if not omitted and entry and isinstance(entry.get('views_omitted'), dict):
        omitted = views_omitted.parse(entry['views_omitted'])
    return omitted


def collect_and_reduce(key, out, backend='tripo', options=None, separable=False, piece_colours=False, omit_views=(), wearable_method=None,
                       knee_cops=False, feature_labels=None):
    """Collect a submitted module from Studio (if needed), then reduce it; returns (state, manifest)."""
    submission = out / 'studio_submission.json'
    state = json.loads(submission.read_text(encoding='utf-8-sig'))
    if state.get('backend', 'tripo') != backend:
        raise SystemExit(f'{key}: submission record belongs to backend {state.get("backend", "tripo")!r}, '
                         f'not {backend!r}; inspect {submission}')
    if state.get('status') not in ('submitted', 'exported'):
        raise SystemExit(f'{key}: submission record says {state.get("status")!r}; inspect {submission}')
    if state.get('status') != 'exported' or 'GLB' not in state.get('exports', {}):
        print(f'== {key}: collect', flush=True)
        if backend == 'tripo':
            run_studio(collect_command('tripo', out), key, 'Tripo collect', paid=False, check=True, cwd=ROOT)
        else:
            if backend == 'hyper3d':
                # its geometry / material confirmations cost credits: an existing submission waits like a new one, and the child
                # re-checks the gate right before each click (exit 4 = it stopped there: wait and resume)
                run = run_paid_child(collect_command(backend, out, options), key, 'a paid Hyper3D collect',
                                     paid=bool((options or {}).get('allow_paid')), cwd=ROOT)
            else:
                run = subprocess.run(collect_command(backend, out, options), cwd=ROOT)
            if run.returncode != 0:  # a guard stop (credits/permissions/sign-in) leaves the module resumable
                raise SystemExit(f'{key}: {backend} collect stopped (exit {run.returncode}); '
                                 f'fix what it printed and re-run; record {submission}')
        state = json.loads(submission.read_text(encoding='utf-8-sig'))
    source = out / 'source.glb'
    if not source.exists():
        try:
            os.link(state['exports']['GLB']['path'], source)  # identical bytes; a hard link instead of a ~40 MB copy
        except OSError:
            shutil.copy2(state['exports']['GLB']['path'], source)
    manifest = out / 'reduced' / 'manifest.json'
    if not manifest.exists():
        print(f'== {key}: reduce', flush=True)
        subprocess.run([BLENDER, '-b', '-t', '6', '--python', str(TOOLS / 'reduce_model.py'), '--',
                        str(source), str(out / 'reduced')] + (['--separable'] if separable else []) + (['--adaptive-hue', 'on'] if piece_colours else [])
                       + (['--omit-views', ','.join(omit_views)] if omit_views else [])
                       + (['--wearable-method', wearable_method] if wearable_method else [])
                       + (['--knee-cops'] if knee_cops else [])
                       + (['--feature-labels', feature_labels] if feature_labels else []), check=True, cwd=ROOT)
    return state, json.loads(manifest.read_text(encoding='utf-8-sig'))


def segment_first_config():
    """config.json `segment_first` block (default_method, venv_python, levels); {} when absent."""
    try:
        return json.loads((TOOLS / 'config.json').read_text(encoding='utf-8-sig')).get('segment_first') or {}
    except (OSError, ValueError):
        return {}


def segment_first_python():
    """Interpreter with numpy + scipy + Pillow for segment_first.py: $SEGMENT_FIRST_PYTHON, config.json segment_first.venv_python,
    else the characters/.venv beside the imageto3d folder. Raises FileNotFoundError naming every place it looked."""
    candidates = portable.venv_python_candidates(ROOT, segment_first_config().get('venv_python'))
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    raise FileNotFoundError('no python with numpy+scipy for segment_first.py; looked for $SEGMENT_FIRST_PYTHON, '
                            f'config.json segment_first.venv_python and the characters/.venv (tried: {candidates})')


def choose_split_method(requested, module, piece_colours, separable):
    """(method, why) for the split step. requested: the --split-method value or None. The default is segment-first for a
    piece_colours body_armor job (config.json segment_first.default_method can change it) and none for everything else,
    joint-band separable jobs included (those keep split_pieces bands, run by hand). The rigid split only exists for
    body_armor, so any other module is always `none`."""
    if requested is not None and requested not in SPLIT_METHODS:
        raise ValueError(f'--split-method must be one of {SPLIT_METHODS}, not {requested!r}')
    if module != 'body_armor':
        return 'none', f'{module} has no rigid piece split'
    if requested is not None:
        return requested, '--split-method'
    if separable:
        return 'none', 'separable joint-band job (split_pieces --rigid bands is run by hand)'
    if piece_colours:
        method = segment_first_config().get('default_method', 'segment-first')
        return (method if method in SPLIT_METHODS else 'segment-first'), 'piece_colours job default'
    return 'none', 'not a piece_colours job'


def relative(path):
    """`path` relative to ROOT when it lies under it, else as it is."""
    try:
        return Path(path).relative_to(ROOT).as_posix()   # job records hold forward slashes on every OS
    except ValueError:
        return str(path)


def run_logged(command, log, label, env=None, append=False):
    """Run `command` from ROOT with its output in `log` (appended with `append`); raises RuntimeError with the log tail on a non-zero
    exit. `env`: the child's environment (default: ours)."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, 'a' if append else 'w') as handle:
        run = subprocess.run(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT, **({'env': env} if env is not None else {}))
    if run.returncode != 0:
        tail = ' | '.join(log.read_text(encoding='utf-8', errors='replace').strip().splitlines()[-6:])
        raise RuntimeError(f'{label} exited {run.returncode} (log {log}): {tail}')


def segment_first_split(out, module, feature_labels=None):
    """Segment-first split of a reduced module folder: prep (Blender) then segment_first.py; both skipped when their output exists.
    Inputs (cameras, regions, palette) are the ones the reduction recorded in reduced/manifest.json. `feature_labels`: opt-in label pass
    spec for the high-poly labels the sections are cut from (feature_hooks.py)."""
    sf = out / 'segment_first'
    prep = sf / 'prep'
    if not (prep / 'prep.npz').exists():
        manifest = json.loads((out / 'reduced' / 'manifest.json').read_text(encoding='utf-8-sig'))
        sf.mkdir(parents=True, exist_ok=True)
        command = [BLENDER, '-b', '-t', '6', '--python', str(TOOLS / 'segment_first_prep.py'), '--',
                   str(out / 'source.glb'), str(prep), '--module', module]
        if manifest.get('cameras'):
            command += ['--cameras', manifest['cameras']]
        if manifest.get('regions'):
            command += ['--regions', manifest['regions']]
        cops = manifest.get('knee_cops') or {}
        if cops.get('enabled'):                 # knee-cop module: the prep relabels the knee band as knee_cop parts like the reduction did
            command += ['--knee-cops', '--knee-cop-band', str(cops['band_fraction'])]
            if cops.get('above_fraction') is not None:
                command += ['--knee-cop-above', str(cops['above_fraction'])]
        if manifest.get('omit_views'):          # three-view module: the omitted camera does not count as having seen a surface
            command += ['--omit-views', ','.join(manifest['omit_views'])]
        if manifest.get('palette'):             # the palette the reduction used (job snapshot / base), frozen beside the run
            write_record(sf / 'palette_used.json', manifest['palette'])
            command += ['--palette', str(sf / 'palette_used.json')]
        print(f'== {out.parent.name}/{module}: split (segment-first): prep', flush=True)
        run_logged(command, sf / 'prep.log', 'segment_first_prep.py')
        if not (prep / 'prep.npz').exists():
            raise RuntimeError(f'segment_first_prep.py wrote no prep.npz (log {sf / "prep.log"})')
    if (sf / 'pieces' / 'pieces.json').exists():
        return sf
    print(f'== {out.parent.name}/{module}: split (segment-first): pieces', flush=True)
    levels = segment_first_config().get('levels')
    run_logged([segment_first_python(), str(TOOLS / 'segment_first.py'), str(out), str(sf), '--reduced', str(out / 'reduced'),
                '--source', str(out / 'source.glb'), '--prep-dir', str(prep)] + (['--levels', levels] if levels else [])
               + (['--feature-labels', feature_labels] if feature_labels else []),
               sf / 'segment_first.log', 'segment_first.py')
    if not (sf / 'pieces' / 'pieces.json').exists():
        raise RuntimeError(f'segment_first.py wrote no pieces/pieces.json (log {sf / "segment_first.log"})')
    return sf


def per_face_split(out, module):
    """Fallback / `per-face` method: split_pieces.py --rigid auto on the per-face reduction -> <module>/pieces/."""
    pieces = out / 'pieces'
    if (pieces / 'pieces.json').exists():
        return pieces
    try:
        python = segment_first_python()
    except FileNotFoundError:
        python = sys.executable
    print(f'== {out.parent.name}/{module}: split (per-face): split_pieces --rigid auto', flush=True)
    run_logged([python, str(TOOLS / 'split_pieces.py'), str(out / 'reduced'), '--module', module, '--rigid', 'auto'],
               out / 'pieces_per_face.log', 'split_pieces.py')
    if not (pieces / 'pieces.json').exists():
        raise RuntimeError(f'split_pieces.py wrote no pieces/pieces.json (log {out / "pieces_per_face.log"})')
    return pieces


def split_step(out, module, method, why='', feature_labels=None):
    """Run the split `method` on the reduced module folder `out`; never raises. Returns the record written to
    <out>/split_step.json and .tripo-job.json: {method (used), requested, why, status done|fallback|failed|skipped,
    outputs, fallback_reason?, error?, feature_labels?}. A failed segment-first falls back to the per-face split_pieces (which has no
    feature label pass: the record then says so)."""
    record = {'requested': method, 'method': method, 'why': why}
    if feature_labels and method != 'none':
        record['feature_labels'] = feature_labels
    if method == 'none':
        record['status'] = 'skipped'
        return record
    try:
        if method == 'segment-first':
            try:
                record['outputs'] = relative(segment_first_split(out, module, feature_labels))
                record['status'] = 'done'
            except Exception as error:  # noqa: BLE001  the split must never fail the conversion
                record['fallback_reason'] = f'segment-first failed: {error}'
                print(f'WARNING {out.parent.name}/{module}: {record["fallback_reason"]}; falling back to '
                      f'split_pieces --rigid auto', flush=True)
                record['method'], record['status'] = 'per-face', 'fallback'
                record.pop('feature_labels', None)
                record['outputs'] = relative(per_face_split(out, module))
        else:
            record.pop('feature_labels', None)
            record['outputs'] = relative(per_face_split(out, module))
            record['status'] = 'done'
    except Exception as error:  # noqa: BLE001
        record['status'], record['error'] = 'failed', str(error)
        print(f'WARNING {out.parent.name}/{module}: split step failed ({error}); the reduction is intact', flush=True)
    if out.is_dir():
        write_record(out / 'split_step.json', record)
    return record


def align_config():
    """config.json `align` block (enabled); {} when absent."""
    try:
        return json.loads((TOOLS / 'config.json').read_text(encoding='utf-8-sig')).get('align') or {}
    except (OSError, ValueError):
        return {}


def align_disabled_reason(args):
    """Why the align step is off (`--no-align`, config.json align.enabled false), else None."""
    if '--no-align' in args:
        return '--no-align'
    if align_config().get('enabled', True) is False:
        return 'config.json align.enabled is false'
    return None


def tool_status(log, tag):
    """(status, detail) from the last `<tag> <status> <module> ...` line align_body.py / body_fit.py print into `log`;
    ('failed', 'no status line') when there is none."""
    status, detail = 'failed', 'no status line'
    try:
        lines = log.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return status, detail
    for line in lines:
        words = line.split(None, 2)
        if len(words) >= 2 and words[0] == tag:
            status, detail = words[1], (words[2] if len(words) > 2 else '')
    return status, detail


def align_step(out, module):
    """Align the module's wearable and rigid pieces to the game base body (align_body.py) and fit them out of it
    (body_fit.py --source aligned); never raises. Writes <out>/align_step.json (log: <out>/align_step.log) and returns
    {status done|failed|skipped, align: {status, detail?, residual_mm: {before, after}, warnings}, fit: {status, detail?, warnings,
    levels: {<lv>: {poke_after_cm2, max_displacement_mm}}}, error?}. `done` = both scripts reported done or fresh; `skipped` = a script
    reported skipped (e.g. no cached base); `failed` = a script failed or crashed. The scripts check the module lock; our pid goes to
    them in $MODULE_LOCK_OWNER so that the lock this process holds does not make them skip the module."""
    record = {'status': 'skipped'}
    log = out / 'align_step.log'
    print(f'== {out.parent.name}/{module}: align to the base body, then fit', flush=True)
    env = dict(os.environ, MODULE_LOCK_OWNER=str(os.getpid()))
    try:
        python = segment_first_python()
        run_logged([python, str(TOOLS / 'align_body.py'), str(out)], log, 'align_body.py', env=env)
        status, detail = tool_status(log, 'ALIGN_BODY')
        record['align'] = {'status': status, **({'detail': detail[:300]} if detail and status not in ('done', 'fresh') else {})}
        align_json = out / 'fit' / 'align.json'
        if status in ('done', 'fresh') and align_json.is_file():
            data = json.loads(align_json.read_text(encoding='utf-8-sig'))
            residuals = data.get('residuals') or {}
            record['align'].update({'residual_mm': {'before': (residuals.get('current') or {}).get('mean_mm'),
                                                    'after': (residuals.get('stage12') or {}).get('mean_mm')},
                                    'warnings': len(data.get('warnings') or [])})
        if status == 'failed':
            raise RuntimeError(f'align_body.py reported failure (log {log}): {detail[:200]}')
        if align_json.is_file() and status in ('done', 'fresh'):
            summary_dir = tempfile.mkdtemp(prefix='body_fit_step_')  # body_fit.py writes a summary; keep it off experiments/
            try:
                run_logged([python, str(TOOLS / 'body_fit.py'), str(out), '--source', 'aligned',
                            '--summary', str(Path(summary_dir) / 'summary.json')], log, 'body_fit.py', env=env, append=True)
            finally:
                shutil.rmtree(summary_dir, ignore_errors=True)
            status, detail = tool_status(log, 'BODY_FIT')
            record['fit'] = {'status': status, **({'detail': detail[:300]} if detail and status not in ('done', 'fresh') else {})}
            fit_json = out / 'fit' / 'body_fit_aligned.json'
            if status in ('done', 'fresh', 'warnings') and fit_json.is_file():
                data = json.loads(fit_json.read_text(encoding='utf-8-sig'))
                levels = {}
                for lv, row in (data.get('levels') or {}).items():
                    wear = (row or {}).get('wearable') or {}
                    levels[lv] = {'poke_after_cm2': ((wear.get('push') or {}).get('poke_residual') or {}).get('area_cm2'),
                                  'max_displacement_mm': (wear.get('displacement') or {}).get('max_mm')}
                record['fit'].update({'warnings': len(data.get('warnings') or []), 'levels': levels})
            if status == 'failed':
                raise RuntimeError(f'body_fit.py reported failure (log {log}): {detail[:200]}')
            record['status'] = 'done' if status in ('done', 'fresh', 'warnings') else 'skipped'
        else:
            record['status'] = 'skipped'
    except Exception as error:  # noqa: BLE001  the align step must never fail the conversion
        record['status'], record['error'] = 'failed', str(error)
        print(f'WARNING {out.parent.name}/{module}: align step failed ({error}); the reduction and split are intact', flush=True)
    if out.is_dir():
        write_record(out / 'align_step.json', record)
    return record


def winding_step(out, module):
    """Turn flipped faces of the module's GLBs the right way round (fix_winding.py --apply: faces wound against their neighbours, closed
    pieces that are inside-out; only the corner order of those triangles changes, so face order and sidecar arrays stay valid); never
    raises. Returns {status done|failed, glbs, glbs_fixed, faces_fixed, pieces_needing_review, error?}; per-GLB detail in
    <out>/reduced/winding.json, log <out>/winding_step.log."""
    log = out / 'winding_step.log'
    record = {'status': 'failed'}
    try:
        run_logged([segment_first_python(), str(TOOLS / 'fix_winding.py'), str(out), '--apply'], log, 'fix_winding.py',
                   env=dict(os.environ, MODULE_LOCK_OWNER=str(os.getpid())))
        rep = json.loads((out / 'reduced' / 'winding.json').read_text(encoding='utf-8'))
        record = {'status': 'done', 'glbs': len(rep), 'glbs_fixed': sum(1 for v in rep.values() if v.get('fixed')),
                  'faces_fixed': sum(v.get('flipped', 0) for v in rep.values() if v.get('fixed')),
                  'pieces_needing_review': sum(v.get('pieces_needing_review', 0) for v in rep.values())}
        errors = [k for k, v in rep.items() if v.get('error')]
        if errors:
            record['errors'] = errors[:5]
    except Exception as error:  # noqa: BLE001  never fails the conversion
        record['error'] = str(error)[:300]
        print(f'WARNING {out.parent.name}/{module}: winding step failed ({error}); the GLBs are unchanged', flush=True)
    return record


def align_summary(record):
    """One-line digest of an align_step record for the `== <key>: align ...` line."""
    align, fit = record.get('align') or {}, record.get('fit') or {}
    parts = [f'align {align["status"]}'] if align else []
    residual = align.get('residual_mm') or {}
    if residual.get('before') is not None and residual.get('after') is not None:
        parts.append(f'residual {residual["before"]:.1f} -> {residual["after"]:.1f} mm')
    if fit:
        pokes = [v['poke_after_cm2'] for v in (fit.get('levels') or {}).values() if v.get('poke_after_cm2') is not None]
        parts.append(f'fit {fit["status"]}' + (f' (poke after {max(pokes):.1f} cm2)' if pokes else ''))
    if record.get('error'):
        parts.append(record['error'][:120])
    return ', '.join(parts)


def regenerate_reason(info):
    """A Tripo mesh worth regenerating: currently a doubled interior shell (reduce_model's audit)."""
    interior = info.get('interior') or {}
    share = interior.get('interior_area_share', 0)
    if share >= interior.get('threshold', float('inf')):
        return {'slug': 'interior_shell', 'text': f'doubled interior shell ({share:.0%} of the area is enclosed)'}
    return None


def pixal3d_regenerate_reason(info):
    """regenerate_reason for a pixal3d mesh. Its reference remesh wraps every thin sheet as a closed two-sided skin, so the
    raw audit always reports ~50% enclosed area; reduce_model strips that inner layer (trial 2026-11-18: 53% -> 2.6%, Tripo
    7%). Only a mesh still enclosed after the strip is worth regenerating."""
    interior = info.get('interior') or {}
    after = (interior.get('after_strip') or {}).get('interior_area_share') if interior.get('stripped') else None
    if after is None:
        return regenerate_reason(info)
    if after >= interior.get('threshold', float('inf')):
        return {'slug': 'interior_shell', 'text': f'interior shell left after the strip ({after:.0%} of the area is enclosed)'}
    return None


def main(argv):
    if not argv or argv[0].startswith('-'):
        raise SystemExit(__doc__)
    job_name, dry_run = argv[0], '--dry-run' in argv[1:]
    backend = option_value(argv[1:], '--backend', 'tripo')
    if backend not in BACKENDS:
        raise SystemExit(f'--backend must be one of {BACKENDS}, not {backend!r}')
    options = {'allow_paid': '--allow-paid' in argv[1:], 'accept_preview': '--accept-preview' in argv[1:]}
    requested_split = option_value(argv[1:], '--split-method')
    max_submits = int(option_value(argv[1:], '--max-submits', -1))
    wearable_method = option_value(argv[1:], '--wearable-method')
    if wearable_method is not None and wearable_method not in WEARABLE_METHODS:
        raise SystemExit(f'--wearable-method must be one of {WEARABLE_METHODS}, not {wearable_method!r}')
    cli_feature_labels = option_value(argv[1:], '--feature-labels')
    if cli_feature_labels is not None:
        try:
            feature_hooks.normalise_spec(cli_feature_labels)
        except ValueError as error:
            raise SystemExit(str(error))
    submits, skipped = 0, []
    if requested_split is not None and requested_split not in SPLIT_METHODS:
        raise SystemExit(f'--split-method must be one of {SPLIT_METHODS}, not {requested_split!r}')
    job = ROOT / 'processing' / job_name
    staged = ROOT / 'multiview_ready' / job_name
    if not job.is_dir():
        if not staged.is_dir():
            raise SystemExit(f'No job {job_name} in multiview_ready/ or processing/')
        if not dry_run:
            # validate before taking it out of the ready queue
            subprocess.run([sys.executable, str(TOOLS / 'workflow.py'), 'check', str(staged), '--complete'],
                           check=True, stdout=subprocess.DEVNULL)
            job.parent.mkdir(exist_ok=True)
            try:
                os.rename(staged, job)
            except FileNotFoundError:  # another worker moved it first
                if not job.is_dir():
                    raise
        else:
            job = staged
    record_path = job / '.tripo-job.json'
    record = read_record(record_path)
    _RUN.update(job=job_name, record=None if dry_run else record_path, lock=None)
    record.setdefault('module_conversion', {})
    locks = ROOT / 'models' / record['set'] / record['job_id'] / '.module_locks'
    summary, errors, elsewhere = {}, [], []
    for entry in record['modules']:
        race, module = entry['race_gender'], entry['module']
        key = f'{race}/{module}'
        separable = '--separable' in argv[1:] or job_is_separable(job, race, module)
        if separable:
            print(f'== {key}: separable job (joint-band palette)', flush=True)
        piece_colours = '--piece-colours' in argv[1:] or job_is_piece_colours(job, race, module)
        knee_cops = ('--knee-cops' in argv[1:] and module == 'body_armor') or job_is_knee_cops(job, race, module)
        omit_views = module_omitted_views(job, race, module, entry)
        try:
            feature_labels, feature_source = feature_hooks.resolve_spec(record['set'], module, record, cli_feature_labels)
        except ValueError as error:
            raise SystemExit(f'{key}: {error}')
        if feature_labels:
            print(f'== {key}: feature label pass {feature_labels} ({feature_source})', flush=True)
        if omit_views:
            print(f'== {key}: three-view module (omitted: {", ".join(omit_views)}); visibility weighting skips that camera', flush=True)
        if piece_colours:
            print(f'== {key}: per-piece base colours job (standard palette, adaptive hue fit on)', flush=True)
        if knee_cops:
            print(f'== {key}: knee cops (the knee band is garment-allowed and its own rigid section knee_l / knee_r)', flush=True)
        out = ROOT / 'models' / record['set'] / record['job_id'] / race / module
        if dry_run:  # never leave a dry-run record where the real submission will go
            out = Path(tempfile.mkdtemp(prefix=f'tripo_dryrun_{module}_'))
        submission = out / 'studio_submission.json'
        env = dict(os.environ, **({'TRIPO_DRY_RUN': '1'} if dry_run else {}))
        if (out / 'reduced' / 'manifest.json').exists() and key in record['module_conversion']:
            summary[key] = 'already converted'
            continue
        if not submission.exists() and 0 <= max_submits <= submits:
            skipped.append(key)
            summary[key] = 'skipped: --max-submits reached'
            continue
        if not dry_run and not submission.exists():
            wait_for_gate(key, 'a new submission')
        lock = locks / f'{race}__{module}.lock'
        if not dry_run:
            if not claim_module(lock):
                elsewhere.append(key)
                summary[key] = 'being converted by another worker'
                continue
            _RUN['lock'] = lock
            if module_converted(read_record(record_path), key, out):  # finished by another worker meanwhile
                lock.unlink(missing_ok=True)
                _RUN['lock'] = None
                summary[key] = 'already converted'
                continue
        if submission.exists():  # collect with the backend that submitted it
            module_backend = json.loads(submission.read_text(encoding='utf-8-sig')).get('backend', 'tripo')
        else:
            module_backend = backend
            submits += 1
        if not submission.exists():
            print(f'== {key}: submit', flush=True)
            if module_backend == 'tripo':   # the Tripo browser lock; a real submit re-checks the pause/hold under it
                run_studio(submit_command(module_backend, job / race / module, out, dry_run), key, 'a new Tripo submission',
                           paid=not dry_run, check=True, cwd=ROOT, env=env)
            else:
                if not dry_run:
                    wait_for_gate(key, f'a new {module_backend} submission')
                if module_backend in ('formy3d', 'pixal3d'):
                    run = subprocess.run(submit_command(module_backend, job / race / module, out, dry_run, options), cwd=ROOT, env=env)
                    if run.returncode != 0:  # a guard stop (--allow-paid/price/sign-in; pixal3d: bad inputs) leaves the module resumable
                        raise SystemExit(f'{key}: {module_backend} submit stopped (exit {run.returncode}); '
                                         f'fix what it printed and re-run')
                else:   # hyper3d: the free preview (not gated in the child); the gate is passed on all the same
                    command = submit_command(module_backend, job / race / module, out, dry_run)
                    run = run_paid_child(command, key, f'a new {module_backend} submission', paid=False, cwd=ROOT, env=env)
                    if run.returncode:
                        raise subprocess.CalledProcessError(run.returncode, command)
        if dry_run:
            summary[key] = (f'dry run: inputs staged in {out / "pixal3d_input"}, nothing generated' if module_backend == 'pixal3d'
                            else f'dry run: form filled, nothing submitted; screenshot {out / "before_submit.png"}')
            continue
        state, info = collect_and_reduce(key, out, module_backend, options, separable, piece_colours, omit_views, wearable_method, knee_cops, feature_labels)
        # Hyper3D/formy3d retries cost credits, so only Tripo's free-retry loop runs; a broken mesh is flagged
        reason = regenerate_reason(info) if module_backend == 'tripo' else None
        previous = record['module_conversion'].get(key, {})
        while reason and not previous.get('free_retries_exhausted'):
            # a broken Tripo mesh is regenerated with the project's free retries, never by paying again
            failed = out.parent / f'{module}_attempt{len(list(out.parent.glob(module + "_attempt*"))) + 1:02d}_{reason["slug"]}'
            os.rename(out, failed)
            print(f'== {key}: {reason["text"]}; moved to {failed.name}; free retry', flush=True)
            run = run_studio(['node', str(TOOLS / 'studio.mjs'), 'retry', str(failed), str(out)], key, 'Tripo free retry',
                             paid=False, cwd=ROOT)
            retry_state = json.loads(submission.read_text(encoding='utf-8-sig')) if submission.exists() else {}
            if run.returncode != 0:
                if retry_state.get('status') != 'no_free_retries':
                    if not out.exists():  # never leave the module empty: a resume would pay for a new submission
                        os.rename(failed, out)
                    raise SystemExit(f'{key}: free retry failed; inspect {out}')
                # keep the last attempt as the module output, flagged; the retry record goes beside it
                n = len(list(out.parent.glob(module + '_retry_exhausted_record*'))) + 1
                os.rename(out, out.parent / f'{module}_retry_exhausted_record{n:02d}')
                os.rename(failed, out)
                state, info = collect_and_reduce(key, out, separable=separable, omit_views=omit_views, wearable_method=wearable_method, knee_cops=knee_cops, feature_labels=feature_labels)
                previous = {'free_retries_exhausted': True}
                errors.append(f'{key}: {reason["text"]} and all free retries are used; '
                              f'needs manual review (do not pay for another generation blindly)')
                break
            state, info = collect_and_reduce(key, out, separable=separable, omit_views=omit_views, wearable_method=wearable_method, knee_cops=knee_cops, feature_labels=feature_labels)
            reason = regenerate_reason(info)
        # pixal3d is local and free: a broken mesh is regenerated with the next seed (each attempt is kept beside the module)
        seed_offset = 0
        while module_backend == 'pixal3d' and pixal3d_regenerate_reason(info) and seed_offset < pixal3d_retries():
            seed_offset += 1
            reason = pixal3d_regenerate_reason(info)
            failed = out.parent / f'{module}_attempt{len(list(out.parent.glob(module + "_attempt*"))) + 1:02d}_{reason["slug"]}'
            os.rename(out, failed)
            print(f'== {key}: {reason["text"]}; moved to {failed.name}; regenerating with seed offset {seed_offset}', flush=True)
            run = subprocess.run(submit_command('pixal3d', job / race / module, out, options={'seed_offset': seed_offset}), cwd=ROOT)
            if run.returncode != 0:
                raise SystemExit(f'{key}: pixal3d resubmit stopped (exit {run.returncode}); the previous attempt is {failed}')
            state, info = collect_and_reduce(key, out, 'pixal3d', options, separable, piece_colours, omit_views, wearable_method,
                                             knee_cops, feature_labels)
        if module_backend == 'pixal3d' and pixal3d_regenerate_reason(info):
            errors.append(f'{key}: {pixal3d_regenerate_reason(info)["text"]}; still broken after {seed_offset} pixal3d regeneration(s), '
                          f'needs manual review')
        elif module_backend != 'tripo' and regenerate_reason(info):
            errors.append(f'{key}: {regenerate_reason(info)["text"]}; {module_backend} has no free retry, '
                          f'needs manual review (do not pay for another generation blindly)')
        method, why = choose_split_method(requested_split, module, piece_colours, separable)
        split = split_step(out, module, method, why, feature_labels)
        if method != 'none':
            print(f'== {key}: split {split["status"]} ({split["method"]})', flush=True)
        no_align = align_disabled_reason(argv[1:])
        align = {'status': 'skipped', 'why': no_align} if no_align else align_step(out, module)
        print(f'== {key}: align {align["status"]} ({no_align or align_summary(align)})', flush=True)
        winding = winding_step(out, module)
        print(f'== {key}: winding {winding["status"]} ({winding.get("faces_fixed", 0)} faces fixed in {winding.get("glbs_fixed", 0)} GLBs'
              f'{", " + str(winding["pieces_needing_review"]) + " pieces need review" if winding.get("pieces_needing_review") else ""}'
              f'{", " + winding["error"][:120] if winding.get("error") else ""})', flush=True)
        levels = info['levels']
        review = [k for k, v in levels.items()
                  if not v['mesh_checks_pass'] or v['material_review_required']]
        record['module_conversion'][key] = {'backend': module_backend, 'project_id': state.get('project_id'),
                                            'models': out.relative_to(ROOT).as_posix(),
                                            'levels_needing_review': review,
                                            'split': split,
                                            'align': align,
                                            'winding': winding,
                                            'wearable': wearable_record(info),
                                            **({'views_omitted': omit_views} if omit_views else {}),
                                            **({'knee_cops': knee_cop_record(info)} if knee_cops else {}),
                                            **({'feature_labels': {'spec': feature_labels, 'source': feature_source,
                                                                   **({k: info['wearable']['feature_labels'][k] for k in ('skipped', 'holes', 'studs', 'fixed')
                                                                       if k in info.get('wearable', {}).get('feature_labels', {})})}}
                                               if feature_labels else {}),
                                            **({'free_retries_exhausted': True, 'needs_manual_review':
                                                regenerate_reason(info)['text']}
                                               if previous.get('free_retries_exhausted') and regenerate_reason(info) else {})}
        entry_done = record['module_conversion'][key]

        def module_done(r, key=key, entry_done=entry_done):
            r.setdefault('module_conversion', {})[key] = entry_done
            if isinstance(r.get('deferred'), dict):      # a module that left a drain earlier (DESIGN_PHASE4 I.10) is converted now
                r['deferred'].pop(key, None)
                if not r['deferred']:
                    del r['deferred']
        record = update_record(record_path, module_done)
        lock.unlink(missing_ok=True)
        _RUN['lock'] = None
        summary[key] = {k: v['triangles'] for k, v in levels.items()} | {'review': review}
    unfinished = []
    if not dry_run:
        def finish(r):
            r.setdefault('conversion_errors', []).extend(e for e in errors if e not in r['conversion_errors'])
            base = ROOT / 'models' / r['set'] / r['job_id']
            unfinished[:] = [f'{e["race_gender"]}/{e["module"]}' for e in r['modules']
                             if not module_converted(r, f'{e["race_gender"]}/{e["module"]}',
                                                     base / e['race_gender'] / e['module'])]
            if unfinished and not skipped:
                return  # other workers still own modules; whoever finishes the last one sets the state
            backends = sorted({v.get('backend', 'tripo') for v in r['module_conversion'].values()}) or [backend]
            r['state'] = ('partially_converted' if skipped else
                          'converted_with_errors' if r['conversion_errors'] else 'converted')
            r['conversion'] = f'{"+".join(backends)}_and_reduction_done_pending_review'
        update_record(record_path, finish)
        if not unfinished:
            try:
                locks.rmdir()
            except OSError:
                pass
    print(json.dumps({'job': job.relative_to(ROOT).as_posix(), 'modules': summary}, indent=2))
    for line in errors:
        print(f'ERROR {line}', file=sys.stderr)
    if errors:
        raise SystemExit(1)
    if skipped:
        print(f'PARTIAL {len(skipped)} module(s) not submitted (--max-submits {max_submits}): {skipped}', file=sys.stderr)
        raise SystemExit(3)
    if unfinished:
        print(f'SHARED {len(unfinished)} module(s) still being converted by other workers: {unfinished}', file=sys.stderr)
        raise SystemExit(4)


if __name__ == '__main__':
    main(sys.argv[1:])
