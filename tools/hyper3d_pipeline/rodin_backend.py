"""Hyper3D Rodin as an alternative image->3D backend for convert_job.py (high-poly, textured GLB).

  rodin_backend.py submit <views_dir> <out_dir> [--dry-run]
  rodin_backend.py collect <out_dir> [--allow-paid --accept-preview] [--expect-cost kind=N ...]
  rodin_backend.py inspect <out_dir>          read-only: reopen the model, record its server stage
  rodin_backend.py bind <out_dir> <model-url> recover a submission interrupted before the URL was captured

Contract with convert_job.py: <out_dir>/studio_submission.json is the same record Tripo's studio.mjs writes
(`status` submitted -> exported, `project_id`, `exports.GLB.path`) plus `backend: "hyper3d"`.

Cost safety (fail closed):
- submit only presses *Free Preview* (the page must say so) and writes the submission intent first;
  an existing record, an unresolved intent or an already-recorded action is never repeated.
- collect resumes from the server stage. Every credit-capable click (geometry confirm, material
  generate, material confirm) needs --allow-paid, geometry also --accept-preview, and the displayed
  credit cost must equal the expected cost from config.json / --expect-cost; unknown -> stop and report.
- No Smart Low-poly: the geometry step selects the "Best Quality" card (config `mesh_mode_card`; raw, Tri, 1M) and stops
  unless it reads selected / Tri / `triangle_polycount_label`. (`mesh_mode_option` "Triangle" is legacy, unused.)

The browser part (SeleniumRodin) drives the native Firefox profile of this folder (see README.md) and
is UNVERIFIED against a signed-in session; everything else is offline-tested with a fake session.
Locks (browser_lock.py): SeleniumRodin._start opens the profile through browser_lock.open_profile, which takes the
cross-process browser lock `.codex_jobs/_batch/browser_hyper3d.lock` before the profile check and the launch (re-entrant within a
process; waits up to $HYPER3D_LOCK_WAIT seconds, default 600, then Stop 'Hyper3D profile busy'); the monster wrappers inherit it.
Every command (submit / collect / preview / inspect / bind) holds `<out_dir>/.record.lock` for its whole run and reads the record
only after taking it; a second command on the same output stops at once with 'another process is handling this output'.

Click-time paid gate (DESIGN_PHASE4 I.8, tools/paid_gate.py): every credit-capable step (geometry confirm, material generate,
material confirm) calls paid_gate.check() when it starts, before it writes its intent and again right before the click. A closed
gate raises GateClosed: no intent is left behind (one written a moment earlier is withdrawn: nothing was clicked), the CLI prints
`PAID GATE CLOSED: <reason>` and exits EXIT_GATE_CLOSED (4); the caller waits for its gate and runs collect again. The free
preview submit is not gated. Without $IMAGETO3D_PAID_GATE (a manual run) the explicit --allow-paid is the authority.

Free preview resolution (phase 5 review finding 1): the `free_preview` intent is resolved (pending_action cleared,
actions.free_preview gets done_at / model_id / resolved_by) as soon as its model is positively identified: the URL captured at
submit, a `bind`, and on load by collect / preview / inspect of an existing record that is bound to a model. An unidentified
click (no model URL) stays pending.

Inputs (phase 5 review finding 7): submit copies the views it uploads into <out_dir>/upload_inputs/, uploads those copies and
stores `inputs: [{name, sha256, size}]` (upload order, the exact bytes uploaded) and `upload_dir` in the record;
`record_inputs(record)` reads it (None for a record written before the field existed = unknown).
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import struct
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from job_state import BASE, atomic_json, model_id, now  # noqa: E402

CONFIG = HERE / 'config.json'


def _load_tripo_module(name):
    """tools/tripo_pipeline/<name>.py (stdlib-only helpers), loaded by path so that folder never joins sys.path
    (it has its own workflow.py/palette.py that must not shadow this folder's modules)."""
    spec = importlib.util.spec_from_file_location(name, HERE.parent / 'tripo_pipeline' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


views_omitted = _load_tripo_module('views_omitted')   # three-view module marker
claim_lock = _load_tripo_module('claim_lock')         # try_lock / release_lock: the OS byte lock of workflow.py claim


def _load_paid_gate():
    """tools/paid_gate.py, shared through sys.modules['paid_gate'] so every importer sees one GateClosed class."""
    if 'paid_gate' in sys.modules:
        return sys.modules['paid_gate']
    spec = importlib.util.spec_from_file_location('paid_gate', HERE.parent / 'paid_gate.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules['paid_gate'] = module
    spec.loader.exec_module(module)
    return module


paid_gate = _load_paid_gate()
GateClosed = paid_gate.GateClosed               # a paid click was not made because the gate closed (resumable)
EXIT_GATE_CLOSED = paid_gate.EXIT_GATE_CLOSED   # the CLI's exit code for it
UPLOAD_DIR = 'upload_inputs'                    # <out_dir>/upload_inputs/: the exact files submit uploaded
RECORD = 'studio_submission.json'
COST_KINDS = ('geometry', 'material_generate', 'material_confirm')
STAGES_TO_ACT = {'geometry_preview', 'material_ready', 'material_preview', 'export_ready'}


# Stop, the browser lock and the per-output record lock live in browser_lock.py (no selenium import), so native_browser.py
# and workflow.py take the same browser lock when they open the profile.
from browser_lock import (BrowserLock, LOCK_BUSY, LOCK_FILE, LOCK_FILE_ENV, LOCK_POLL_S, LOCK_WAIT_ENV,  # noqa: E402,F401
                          LOCK_WAIT_S, OUTPUT_BUSY, RECORD_LOCK, Stop, lock_wait_seconds, open_profile, output_lock)


BALANCE_WAIT_S = 60   # the header balance loads late (0, then the real one); Generate with 0 shown = "Insufficient Balance"
INSUFFICIENT = 'Insufficient Balance'


def stat_or_none(path):
    try:
        return path.stat()
    except (FileNotFoundError, PermissionError):   # Windows: a file Firefox is renaming/holding
        return None


def not_submitted(record, why):
    """Nothing was generated (no click, or Hyper3D refused it): set the record aside so a re-run submits again."""
    record.save('not_submitted', status='not_submitted', pending_action=None, why=why)
    aside = record.path.with_name(f'{record.path.stem}.not_submitted_{time.strftime("%Y%m%dT%H%M%S")}.json')
    record.path.rename(aside)
    raise Stop(f'{why}; nothing was generated, record moved to {aside.name}, a re-run submits again')


# --------------------------------------------------------------------------- config / inputs

def load_config(path=CONFIG):
    config = json.loads(Path(path).read_text())
    for key in ('view_order', 'directions', 'expected_costs', 'workspace_url'):   # mesh_mode_option is legacy, optional
        if key not in config:
            raise ValueError(f'config.json is missing {key!r}')
    return config


def png_info(path):
    """(width, height) of an 8-bit RGBA PNG, same checks as studio.mjs pngInfo."""
    head = Path(path).read_bytes()[:26]
    if len(head) < 26 or head[:8] != b'\x89PNG\r\n\x1a\n' or head[12:16] != b'IHDR':
        raise ValueError(f'Expected PNG: {path}')
    if head[24] != 8 or head[25] != 6:
        raise ValueError(f'Expected 8-bit RGBA PNG: {path}')
    return struct.unpack('>II', head[16:24])


def validate_views(views_dir, config):
    """Check the module's views exist, are 8-bit RGBA PNGs of identical size; returns the upload plan.

    With multi_view false only the first view (front) is used. Never resizes anything. A config with `allow_mixed_sizes`
    (only the monster multi-view wrapper sets it; Hyper3D's acceptance of mixed sizes is unverified) skips the
    identical-size rule; the plan then also carries `input_resolutions` {name: [w, h]} (`input_resolution` stays the
    first view's).

    Three-view modules: a `views_omitted.json` marker in `views_dir` (tools/tripo_pipeline/views_omitted.py) drops that
    view from the plan, so its slot is simply not uploaded; `images` and `uploaded` list the views that exist and
    `views_omitted` the omitted names (without .png). Without a marker all of `view_order` must exist.
    """
    views_dir = Path(views_dir)
    omitted = views_omitted.omitted_views(views_dir)
    names = [n for n in config['view_order'] if n[:-len('.png')] not in omitted]
    for name in names:
        if not (views_dir / name).is_file():
            raise ValueError(f'Missing {name}')
    dims = [png_info(views_dir / name) for name in names]
    mixed = any(d != dims[0] for d in dims)
    if mixed and not config.get('allow_mixed_sizes'):
        raise ValueError(f'Multi-view inputs have different native dimensions: {dims}')
    used = names if config.get('multi_view') else names[:1]
    if config.get('max_images') and len(used) > config['max_images']:
        raise ValueError(f'{len(used)} views exceed the max_images={config["max_images"]} of this config')
    return {'views_dir': str(views_dir.resolve()), 'images': names, 'uploaded': used,
            'input_resolution': list(dims[0]), 'views_omitted': omitted,
            **({'input_resolutions': {n: list(d) for n, d in zip(names, dims)}} if mixed else {})}


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------- record (intent guard)

class Record:
    """studio_submission.json with write-before-click intents; mirrors job_state.Job semantics. The commands below create,
    read and write it only while they hold the output's `.record.lock` (browser_lock.output_lock)."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text())

    def reload(self):
        self.data = json.loads(self.path.read_text())
        return self

    @classmethod
    def create(cls, path, data):
        """Exclusive creation: the complete record is written to a temp file and hard-linked into place, which fails when a
        record already exists (never overwritten, never seen half-written)."""
        path = Path(path)
        if path.exists():
            raise Stop(f'Submission record already exists; collect or inspect instead: {path}')
        temp = path.with_name(f'{path.name}.{os.getpid()}.new')
        atomic_json(temp, data)
        try:
            os.link(temp, path)
        except FileExistsError:
            raise Stop(f'Submission record already exists; collect or inspect instead: {path}') from None
        finally:
            temp.unlink(missing_ok=True)
        return cls(path)

    def save(self, event, **changes):
        self.data.update(changes)
        self.data['updated_at'] = now()
        atomic_json(self.path, self.data)
        with self.path.with_name('events.jsonl').open('a') as stream:
            stream.write(json.dumps({'at': now(), 'event': event, **changes}, default=str) + '\n')

    def bind(self, url, resolve=True):
        """Bind the model URL under the output lock (taken here when the caller does not hold it yet; the record is then
        re-read first, so a bind run on its own never writes back a stale copy). A bound model identifies the free preview:
        with `resolve` its pending intent is resolved too (resolve_free_preview)."""
        ident = model_id(url)
        if not ident:
            raise ValueError('Expected a Hyper3D model URL ending in a UUID')
        with output_lock(self.path.parent) as outermost:
            if outermost:
                self.reload()
            if self.data.get('model_id') not in (None, ident):
                raise ValueError('This record is already bound to a different model')
            self.save('model_linked', model_id=ident, model_url=f'{BASE}/{ident}', project_id=ident)
            if resolve:
                self.resolve_free_preview('bind')

    def identified_model(self):
        """The model id this record is positively bound to (its model_url parses to a Hyper3D model UUID and model_id does not
        contradict it), else None."""
        ident = model_id(self.data.get('model_url') or '')
        if ident and self.data.get('model_id') not in (None, ident):
            return None
        return ident

    def resolve_free_preview(self, how, **changes):
        """Phase 5 review finding 1: a pending `free_preview` intent whose model is positively identified (a bound model URL) is
        done: pending_action is cleared and actions.free_preview gets done_at / model_id / resolved_by, in ONE save together with
        `changes`. Returns True when it resolved. An unidentified click (no model URL) and any other pending action stay as they
        are (`changes`, when given, are saved anyway)."""
        ident = self.identified_model()
        if self.data.get('pending_action') != 'free_preview' or not ident:
            if changes:
                self.save('updated', **changes)
            return False
        actions = dict(self.data.get('actions') or {})
        entry = dict(actions.get('free_preview') or {})
        entry.update(done_at=now(), model_id=ident, resolved_by=how)
        actions['free_preview'] = entry
        self.save('free_preview_resolved', pending_action=None, actions=actions, **changes)
        return True

    def restore(self, data, event):
        """Put back a copy of the record taken before an intent that was never clicked (the paid gate closed in between)."""
        self.data = data
        self.save(event)

    def intent(self, action):
        if self.data.get('pending_action'):
            raise Stop('An earlier submission is unresolved; inspect the saved model first')
        if action in self.data.get('actions', {}):
            raise Stop(f'{action} was already submitted; refusing to repeat it')
        actions = dict(self.data.get('actions', {}))
        actions[action] = {'intent_at': now()}
        self.save('submission_intent', pending_action=action, actions=actions)

    def observed(self, stage):
        self.save('server_stage_observed', stage=stage, pending_action=None)


def load_record(out_dir, how):
    """The output's record (the caller holds the output lock), with a pending free preview of a bound model resolved (finding 1)."""
    record = Record(Path(out_dir) / RECORD)
    record.resolve_free_preview(how)
    return record


def record_inputs(record):
    """The ordered inputs a submission uploaded, [{name, sha256, size}] (the exact bytes, in upload order), or None when the
    record predates the field (unknown). `record`: a Record, a record dict, an output dir or a studio_submission.json path.
    ValueError for a malformed field."""
    if isinstance(record, Record):
        data = record.data
    elif isinstance(record, dict):
        data = record
    else:
        path = Path(record)
        data = json.loads((path / RECORD if path.is_dir() else path).read_text(encoding='utf-8-sig'))
    inputs = data.get('inputs')
    if inputs is None:
        return None
    if not isinstance(inputs, list):
        raise ValueError('record inputs is not a list')
    result = []
    for item in inputs:
        if not (isinstance(item, dict) and isinstance(item.get('name'), str) and isinstance(item.get('sha256'), str)
                and re.fullmatch(r'[0-9a-f]{64}', item['sha256']) and isinstance(item.get('size'), int)
                and not isinstance(item.get('size'), bool) and item['size'] >= 0):
            raise ValueError(f'malformed record input {item!r}')
        result.append({'name': item['name'], 'sha256': item['sha256'], 'size': item['size']})
    return result


def snapshot_inputs(plan, out_dir):
    """Copy the views of `plan` into <out_dir>/upload_inputs/ and hash the copies: (dir, [{name, sha256, size}] of the uploaded
    views in upload order). The browser uploads these copies, so the recorded hashes are the bytes uploaded. A views_omitted.json
    marker is copied too (validate_views reads it)."""
    target = Path(out_dir) / UPLOAD_DIR
    target.mkdir(parents=True, exist_ok=True)
    source = Path(plan['views_dir'])
    if (source / 'views_omitted.json').is_file():
        shutil.copyfile(source / 'views_omitted.json', target / 'views_omitted.json')
    else:
        (target / 'views_omitted.json').unlink(missing_ok=True)
    for name in plan['images']:
        shutil.copyfile(source / name, target / name)
    inputs = [{'name': name, 'sha256': sha256_file(target / name), 'size': (target / name).stat().st_size}
              for name in plan['uploaded']]
    return target, inputs


# --------------------------------------------------------------------------- cost guard

def parse_cost(text):
    """Credit cost shown on a control ('2 Credits', '1 Credit'); None when no price is shown."""
    match = re.search(r'(\d+(?:\.\d+)?)\s*credits?\b', text or '', re.I)
    if not match:
        return None
    value = float(match.group(1))
    return int(value) if value.is_integer() else value


def check_cost(kind, observed, expected):
    """Pass only when the displayed cost equals the configured expectation (None expected = unknown = stop)."""
    if expected is None:
        raise Stop(f'{kind}: expected credit cost is not configured (observed {observed!r}); set '
                   f'expected_costs.{kind} in config.json or pass --expect-cost {kind}=N after checking the screenshot')
    if observed is None:
        if expected == 0:
            return
        raise Stop(f'{kind}: no credit price is displayed but {expected} was expected; stopping on UI change')
    if observed != expected:
        raise Stop(f'{kind}: displayed cost {observed} != expected {expected}; stopping on price/UI change')


# --------------------------------------------------------------------------- page-state helpers

def classify_auth(info):
    """signed_in / signed_out / unknown from facts read off the page.

    Signed out (seen 2026-10-22): a visible `Login` link and a NO_CURRENT_USER toast, no login form.
    Signed in: no Login link, the account menu text (`Logout`) is present, wallet balance in the header.
    Google One Tap ("Sign in to Hyper3D with Google") counts as signed out. The plan badge and balance load
    late (first "7-Day Trial"/"Subscribe" and 0, ~10 s later "Business" and the real balance), so callers poll.
    """
    if info.get('login_link') or info.get('no_current_user') or info.get('one_tap') or info.get('password_input'):
        return 'signed_out'
    if info.get('logout'):
        return 'signed_in'
    return 'unknown'


def parse_poly_label(card_text):
    """Polygon slider label (`1M`, `500K`, ...) shown on the Triangle card."""
    match = re.findall(r'(\d+(?:\.\d+)?\s*[KkMm])\b', card_text or '')
    return match[-1].replace(' ', '').upper() if match else None


def derive_stage(base_stage, pack_text, has_download, has_material_action):
    """Refine the old runner's stage: a confirmed model no longer shows De-light (live 2026-10-22: a finished
    model reads 'unknown'), so an unlocked Pack panel with a Download control and no Generate/Confirm = export_ready."""
    if base_stage == 'unknown' and has_download and not has_material_action \
            and 'Unlock after Material confirmed' not in (pack_text or ''):
        return 'export_ready'
    return base_stage


def parse_balance(body_text):
    """Credit balance shown in the header: the number on the line after `MCP&CLI`."""
    lines = [line.strip() for line in (body_text or '').splitlines()]
    for i, line in enumerate(lines):
        if line == 'MCP&CLI':
            for nxt in lines[i + 1:i + 4]:
                if re.fullmatch(r'\d+(?:\.\d+)?', nxt):
                    return float(nxt)
    return None


# --------------------------------------------------------------------------- GLB inspection

def inspect_glb(path):
    """Structural facts convert_job/reduce_model rely on: one mesh, UVs, base-colour image."""
    data = Path(path).read_bytes()
    if data[:4] != b'glTF':
        raise ValueError('not a binary glTF (GLB)')
    length, kind = struct.unpack('<II', data[12:20])
    if kind != 0x4E4F534A:
        raise ValueError('first GLB chunk is not JSON')
    gltf = json.loads(data[20:20 + length])
    mesh_nodes = [n for n in gltf.get('nodes', []) if 'mesh' in n]
    accessors = gltf.get('accessors', [])
    triangles, primitives, uv_all = 0, 0, True
    material_ids = set()
    for mesh in gltf.get('meshes', []):
        for prim in mesh.get('primitives', []):
            primitives += 1
            uv_all &= 'TEXCOORD_0' in prim.get('attributes', {})
            if prim.get('mode', 4) == 4 and 'indices' in prim:
                triangles += accessors[prim['indices']]['count'] // 3
            if 'material' in prim:
                material_ids.add(prim['material'])
    textured = any('baseColorTexture' in gltf['materials'][i].get('pbrMetallicRoughness', {})
                   for i in material_ids)
    report = {'mesh_nodes': len(mesh_nodes), 'primitives': primitives, 'triangles': triangles,
              'has_uv': bool(primitives) and uv_all, 'images': len(gltf.get('images', [])),
              'base_color_texture': textured}
    problems = []
    if len(mesh_nodes) != 1:
        problems.append(f'expected one mesh object, found {len(mesh_nodes)}')
    if not report['has_uv']:
        problems.append('mesh has no UVs')
    if not textured or not report['images']:
        problems.append('no image linked to a base colour texture')
    report['problems'] = problems
    return report


# --------------------------------------------------------------------------- orchestration

def _record_data(plan, config, views_dir, out_dir):
    return {
        'backend': 'hyper3d', 'source': plan['views_dir'], 'images': plan['images'],
        'images_uploaded': plan['uploaded'], 'input_resolution': plan['input_resolution'],
        **({'views_omitted': plan['views_omitted']} if plan.get('views_omitted') else {}),
        **({'input_resolutions': plan['input_resolutions']} if plan.get('input_resolutions') else {}),
        'settings': {'workflow': 'Rodin / Image to 3D' + (' / Multi-view' if config.get('multi_view') else ''),
                     'model': config['model'], 'effort': config['effort'],
                     'directions': {k: config['directions'].get(k) for k in plan['uploaded']},
                     'mesh_mode': config.get('mesh_mode_card') or 'Best Quality', 'mesh_simplify': config.get('mesh_simplify', False),
                     'smart_low_poly': False, 'export_checkboxes': config.get('export_checkboxes'),
                     'de_light': config.get('de_light', True), 'format': config['export_format_label'],
                     'texture': config['texture_resolution']},
        'status': 'preparing', 'started_at': now(), 'model_id': None, 'model_url': None,
        'project_id': None, 'pending_action': None, 'actions': {}, 'stage': 'new',
        'free_retries': bool(config.get('free_retries')),
    }


def submit(views_dir, out_dir, *, config=None, session_factory=None, dry_run=False):
    """Upload the views and start the FREE preview; leaves status 'submitted' with the model URL. Holds the output lock
    for the whole command (a second command on this output stops before the record is created or read)."""
    config = config or load_config()
    out_dir = Path(out_dir)
    plan = validate_views(views_dir, config)
    out_dir.mkdir(parents=True, exist_ok=True)
    with output_lock(out_dir):
        return _submit_locked(views_dir, out_dir, plan, config, session_factory, dry_run)


def _submit_locked(views_dir, out_dir, plan, config, session_factory, dry_run):
    if (out_dir / RECORD).exists():   # checked before the snapshot: an existing record's upload_inputs/ are never overwritten
        raise Stop(f'Submission record already exists; collect or inspect instead: {out_dir / RECORD}')
    upload_dir, inputs = snapshot_inputs(plan, out_dir)
    upload_plan = validate_views(upload_dir, config)   # the copies are what goes up: checked like the originals
    if upload_plan['uploaded'] != plan['uploaded']:
        raise Stop(f'upload snapshot differs from the views: {upload_plan["uploaded"]} != {plan["uploaded"]}')
    record = Record.create(out_dir / RECORD, {**_record_data(plan, config, views_dir, out_dir),
                                              'upload_dir': str(upload_dir.resolve()), 'inputs': inputs})
    session_factory = session_factory or (lambda: SeleniumRodin(config, out_dir))
    with contextlib.ExitStack() as stack:
        try:
            session = stack.enter_context(session_factory())
        except Stop as exc:                     # e.g. the browser lock is busy: nothing was opened, a re-run submits again
            not_submitted(record, str(exc))
        except Exception as exc:
            not_submitted(record, f'{type(exc).__name__} starting the browser: {str(exc).splitlines()[0][:200]}')
        try:
            session.open_workspace()
            paths = [(name, Path(upload_plan['views_dir']) / name) for name in upload_plan['uploaded']]
            prepared = session.prepare_multiview(paths, config) or {}
            record.save('uploaded', upload_info=prepared)
            session.screenshot('before_submit')
            record.save('ready_to_submit', status='ready_to_submit', displayed_cost_credits=0,
                        ready_at=now())
            if dry_run:
                record.save('dry_run', status='dry_run')
                return record.data
            balance = session.wait_balance(BALANCE_WAIT_S)
            record.save('balance_before_submit', balance_before_submit=balance)
        except Stop:
            raise
        except Exception as exc:                # 2026-10-27: a DNS error left a 'preparing' record collect refuses
            not_submitted(record, f'{type(exc).__name__} before the click: {str(exc).splitlines()[0][:200]}')
        if not balance:                         # 2026-10-27: clicked while the header still showed 0 -> refused
            not_submitted(record, f'the wallet still showed {balance!r} credits after {BALANCE_WAIT_S} s')
        record.intent('free_preview')           # written before the click: never repeated
        session.click_free_preview()
        record.save('submission_clicked', status='submission_attempted', submit_clicked_at=now())
        url = session.wait_model_url(600)       # Rodin can sit on GENERATING for minutes when busy
        session.screenshot('after_submit')
        if url and model_id(url):
            record.bind(url, resolve=False)
            # the model is identified: the free intent is done (finding 1), in the same write as the status
            record.resolve_free_preview('submit', status='submitted', after_submit_at=now())
        elif session.insufficient_balance():
            not_submitted(record, f'Hyper3D answered "{INSUFFICIENT}" to Free Preview')
        else:
            record.save('needs_review', status='submission_needs_review', after_submit_at=now())
    return record.data


def _step(record, session, config, kind, expected, allow_paid, accept_preview=False, prepare=None):
    """Guard one credit-capable click: permissions, paid gate, option selection, displayed cost, screenshot, paid gate again,
    intent. Returns a copy of the record from before the intent (_click puts it back when the gate closes before the click)."""
    if not allow_paid:
        raise Stop(f'{kind} may spend credits; pass --allow-paid')
    if kind == 'geometry' and not accept_preview:
        session.screenshot('review_required')
        raise Stop('Preview ready: inspect review_required.png / the model URL, then pass --accept-preview')
    paid_gate.check()          # the stage wait may have lasted minutes: a closed gate stops before anything is selected
    if prepare:
        prepare()
    cost, text = session.read_cost(kind)
    session.screenshot(f'before_{kind}')
    record.save('cost_observed', **{f'observed_cost_{kind}': {'credits': cost, 'text': text}})
    check_cost(kind, cost, expected.get(kind))
    if kind in record.data.get('actions', {}):      # replay guard first: never double-count or repeat
        raise Stop(f'{kind} was already submitted; refusing to repeat it')
    committed = record.data.get('credits_committed', 0) + (cost or 0)
    budget = config.get('budget_credits')
    if budget is not None and committed > budget:
        raise Stop(f'{kind}: committed credits {committed} would exceed budget_credits {budget}')
    paid_gate.check()          # immediately before the intent (DESIGN_PHASE4 I.8): closed = no intent written
    before = json.loads(json.dumps(record.data))
    record.intent(kind)
    record.save('credits_committed', credits_committed=committed)
    return before


def _click(record, session, kind, before):
    """The credit-capable click, with the paid gate checked right before it. A gate that closed after the intent was written
    puts the record back as it was before the intent (nothing was clicked, so nothing may stay pending) and raises GateClosed."""
    try:
        paid_gate.check()
    except paid_gate.GateClosed:
        record.restore(before, f'{kind}_intent_withdrawn')
        raise
    session.click(kind)


def collect(out_dir, *, allow_paid=False, accept_preview=False, expected_costs=None,
            config=None, session_factory=None):
    """Resume by server stage: confirm raw geometry, De-light material, export GLB, record exports.GLB. Holds the output
    lock for the whole command and reads the record only after taking it (two collectors can never both act on one
    record: the second stops with OUTPUT_BUSY before it reads or writes anything)."""
    config = config or load_config()
    out_dir = Path(out_dir)
    with output_lock(out_dir):
        return _collect_locked(out_dir, allow_paid, accept_preview, expected_costs, config, session_factory)


def _collect_locked(out_dir, allow_paid, accept_preview, expected_costs, config, session_factory):
    record = load_record(out_dir, 'collect')
    data = record.data
    if data.get('backend') != 'hyper3d':
        raise Stop(f'{record.path} is not a hyper3d record')
    if data.get('status') not in ('submitted', 'exported'):
        raise Stop(f'record status {data.get("status")!r}; inspect {record.path}')
    exported = data.get('exports', {}).get('GLB')
    if exported and Path(exported['path']).is_file():
        return _finish(record, exported)
    if not data.get('model_url'):
        raise Stop('record has no model URL; find the model under Mine and run bind')
    expected = {**{k: None for k in COST_KINDS}, **config['expected_costs'], **(expected_costs or {})}
    timeout = config.get('stage_timeout_s', 1800)
    session_factory = session_factory or (lambda: SeleniumRodin(config, out_dir))
    with session_factory() as session:
        session.open_model(data['model_url'])
        if 'balance_start' not in data:
            record.save('balance_start', balance_start=session.read_balance())
        exclude = set()
        for _ in range(8):
            stage = session.wait_stage(STAGES_TO_ACT - exclude, timeout)
            record.observed(stage)
            record.save('balance', balances=record.data.get('balances', []) + [
                {'stage': stage, 'credits': session.read_balance(), 'at': now()}])
            exclude = {stage}
            if stage == 'geometry_preview':
                # the raw option is selected before the cost is read: the price belongs to that choice
                before = _step(record, session, config, 'geometry', expected, allow_paid, accept_preview,
                               prepare=lambda: record.save('mesh_mode_selected',
                                                           geometry_options_seen=session.select_mesh_mode(config)))
                record.save('symmetric_selector', symmetric_selector=session.symmetric_value())
                before['symmetric_selector'] = record.data['symmetric_selector']   # an observation, kept on a withdrawal
                _click(record, session, 'geometry', before)
            elif stage == 'material_ready':
                effort = session.material_effort()
                record.save('material_options_seen', material_options=effort)
                want = config.get('material_effort')
                if want and (want not in effort['head'] or 'Extreme' in effort['head']):
                    raise Stop(f'Material effort reads {effort["head"]!r}, expected {want!r}')
                if effort['eight_k_on']:
                    raise Stop('8K is switched on; refusing (not in the configured material options)')
                if config.get('de_light', True):
                    session.set_delight()
                before = _step(record, session, config, 'material_generate', expected, allow_paid)
                _click(record, session, 'material_generate', before)
            elif stage == 'material_preview':
                before = _step(record, session, config, 'material_confirm', expected, allow_paid)
                _click(record, session, 'material_confirm', before)
            elif stage == 'export_ready':
                path, panel = session.export_glb(out_dir, config)
                record.save('balance_end', balance_end=session.read_balance())
                return _record_export(record, path, panel)
        raise Stop('stage machine did not reach export_ready; inspect the model and resume')


def _record_export(record, path, panel_text):
    path = Path(path)
    report = inspect_glb(path)
    exports = dict(record.data.get('exports', {}))
    exports['GLB'] = {'path': str(path), 'bytes': path.stat().st_size, 'sha256': sha256_file(path),
                      'downloaded_at': now(), 'inspection': report, 'export_panel_text': panel_text}
    record.save('exported', exports=exports, studio_result={
        'topology': 'raw', 'faces': report['triangles'], 'inspected_at': now()})
    return _finish(record, exports['GLB'])


def _finish(record, export):
    report = export.get('inspection') or inspect_glb(export['path'])
    if report['problems']:
        record.save('export_needs_review', status='export_needs_review')
        raise Stop('exported GLB failed checks: ' + '; '.join(report['problems']))
    record.save('export_verified', status='exported', pending_action=None)
    return record.data


def preview(out_dir, *, config=None, session_factory=None):
    """Read-only: wait for the free geometry preview, screenshot it, dump the Confirm popover and its displayed
    cost. Picks the configured mesh option (a selection, not a purchase) but NEVER clicks Confirm. Holds the output lock."""
    config = config or load_config()
    out_dir = Path(out_dir)
    with output_lock(out_dir):
        return _preview_locked(out_dir, config, session_factory)


def _preview_locked(out_dir, config, session_factory):
    record = load_record(out_dir, 'preview')
    if record.data.get('backend') != 'hyper3d' or not record.data.get('model_url'):
        raise Stop('record is not a submitted hyper3d model')
    session_factory = session_factory or (lambda: SeleniumRodin(config, out_dir))
    with session_factory() as session:
        session.open_model(record.data['model_url'])
        stage = session.wait_stage({'geometry_preview', 'material_ready', 'material_preview', 'export_ready'},
                                   config.get('stage_timeout_s', 1800))
        record.observed(stage)
        info = {'stage': stage, 'balance': session.read_balance()}
        if stage == 'geometry_preview':
            session.screenshot('preview_default')
            cost, text = session.read_cost('geometry')
            info['default_confirm'] = {'credits': cost, 'text': text}
            info['popover_default'] = session.dump_geometry_panel('geometry_panel_default')
            info['mesh_mode_selected'] = session.select_mesh_mode(config)
            cost, text = session.read_cost('geometry')
            info['configured_confirm'] = {'credits': cost, 'text': text}
            info['popover_configured'] = session.dump_geometry_panel('geometry_panel_configured')
            session.screenshot('preview_configured')
        record.save('preview_inspected', preview=info)
    return record.data


def inspect_only(out_dir, *, config=None, session_factory=None):
    """Read-only: reopen the model, record the server stage and a screenshot. Holds the output lock (an inspection
    never writes back over a newer intent of a collect)."""
    config = config or load_config()
    out_dir = Path(out_dir)
    with output_lock(out_dir):
        return _inspect_locked(out_dir, config, session_factory)


def _inspect_locked(out_dir, config, session_factory):
    record = load_record(out_dir, 'inspect')   # a bound model identifies the free preview: its marker is cleared here
    if not record.data.get('model_url'):
        return record.data
    session_factory = session_factory or (lambda: SeleniumRodin(config, out_dir))
    with session_factory() as session:
        session.open_model(record.data['model_url'])
        stage = session.observe_stage()
        session.screenshot('resume_inspected')
        record.save('resume_inspected', last_observation={'stage': stage})
    return record.data


def bind(out_dir, url):
    """`bind` command: attach a model URL to a submission interrupted before it was captured (under the output lock)."""
    out_dir = Path(out_dir)
    with output_lock(out_dir):
        record = Record(out_dir / RECORD)
        record.bind(url)
        return record.data


# --------------------------------------------------------------------------- browser (UNVERIFIED UI)

class SeleniumRodin:
    """Selenium/native-Firefox session on this folder's profile. Selectors follow the old runner's
    (verified in the items project) except where marked UNVERIFIED; a missing element raises Stop/
    RuntimeError before any paid click.
    """

    def __init__(self, config, out_dir):
        self.config, self.out = config, Path(out_dir)
        self.downloads = self.out / 'downloads'
        self.driver = None

    lock = None

    def __enter__(self):
        try:
            return self._start()
        except BaseException:   # __exit__ is not called when __enter__ raises: quit a started browser, free the lock
            self.__exit__(None, None, None)
            raise

    def _start(self):
        """Open the profile through browser_lock.open_profile: the browser lock (re-entrant within this process) is taken
        before the profile check and the Firefox launch and kept until __exit__. Subclasses (the monster wrappers) inherit
        this; they never take the lock themselves."""
        import workflow as wf
        from native_browser import PROFILE, BINARY, check_profile, firefox_service, firefox_extra_args
        from selenium import webdriver
        from selenium.webdriver.firefox.options import Options
        self.wf = wf
        self.downloads.mkdir(parents=True, exist_ok=True)
        options = Options()
        options.page_load_strategy = 'eager'
        options.binary_location = BINARY
        options.add_argument('-profile')
        options.add_argument(str(PROFILE))
        for extra in firefox_extra_args():
            options.add_argument(extra)
        options.set_preference('browser.download.folderList', 2)
        options.set_preference('browser.download.dir', str(self.downloads.resolve()))
        options.set_preference('browser.download.useDownloadDir', True)
        options.set_preference('browser.helperApps.neverAsk.saveToDisk',
                               'model/gltf-binary,application/octet-stream,application/zip,application/x-zip-compressed')
        # Linux: private X display so no window pops up (PIPELINE_SHOW_WINDOWS=1 shows it); Windows: None (window shown)
        sys.path.insert(0, str(HERE.parent))
        from hidden_display import hidden_display
        display = hidden_display()
        env = {**os.environ, 'DISPLAY': display} if display else None
        try:
            self.driver, self.lock = open_profile(lambda: check_profile(PROFILE),
                                                  lambda: webdriver.Firefox(service=firefox_service(env=env), options=options))
        except ValueError as exc:
            raise Stop(f'Firefox profile is open elsewhere: {exc}')
        self.driver.set_window_size(1500, 1000)
        self.driver.set_page_load_timeout(45)
        return self

    def __exit__(self, *exc):
        driver, self.driver = self.driver, None
        try:
            if driver:
                driver.quit()
        finally:
            if self.lock:
                self.lock.release()

    # -- helpers
    def screenshot(self, label):
        self.driver.save_screenshot(str(self.out / f'{label}.png'))

    AUTH_JS = """
    const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
    const text = document.body.innerText || '';
    const login = [...document.querySelectorAll('a,button,div,span')].some(e => vis(e) && e.children.length === 0 && e.innerText.trim() === 'Login');
    return {login_link: login, no_current_user: text.includes('NO_CURRENT_USER'),
            one_tap: !!document.querySelector('#credential_picker_container, iframe[src*="accounts.google.com/gsi"]') || text.includes('Sign in to Hyper3D with Google'),
            password_input: [...document.querySelectorAll('input[type=password]')].some(vis) || location.pathname.includes('/workspace/login'),
            logout: [...document.querySelectorAll('*')].some(e => e.children.length === 0 && e.innerText && e.innerText.trim() === 'Logout')};
    """

    def _require_signed_in(self, timeout=30, reloads=2):
        """Poll until the page shows a settled signed-in or signed-out state (the header loads late).

        A page that never settles is reloaded `reloads` times first (2026-10-27: after a DNS hiccup the app
        stayed a blank dark page)."""
        for attempt in range(reloads + 1):
            if attempt:
                print(f'Hyper3D page did not settle in {timeout} s; reloading ({attempt}/{reloads})', flush=True)
                self.wf.navigate(self.driver, self.driver.current_url)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                state = classify_auth(self.driver.execute_script(self.AUTH_JS))
                if state == 'signed_in':
                    return
                if state == 'signed_out':
                    self.screenshot('signed_out')
                    raise Stop('Hyper3D profile is signed out (Login link / NO_CURRENT_USER / Google One Tap); '
                               'run native.sh login (Windows: native.cmd login), sign in, close Firefox, resume')
                time.sleep(2)
        self.screenshot('auth_unknown')
        raise Stop('Could not confirm a signed-in Hyper3D session (no Logout entry found)')

    def read_balance(self):
        return parse_balance(self.driver.find_element('tag name', 'body').text)

    def wait_balance(self, timeout):
        """The header balance once it is above 0 (it shows 0 until the account loads), else the last value read."""
        deadline, balance = time.monotonic() + timeout, None
        while True:
            balance = self.read_balance()
            if (balance or 0) > 0 or time.monotonic() >= deadline:
                return balance
            time.sleep(2)

    def insufficient_balance(self):
        return INSUFFICIENT in self.driver.find_element('tag name', 'body').text

    # -- submit
    def open_workspace(self):
        self.wf.navigate(self.driver, self.config['workspace_url'])
        time.sleep(4)
        self._require_signed_in()

    def _pick_effort(self, wanted, levels):
        """Open the effort dropdown (current level next to the timer icon) and choose `wanted`."""
        wf, driver = self.wf, self.driver
        current = next((wf.exact(driver, t) for t in levels if wf.exact(driver, t)), None)
        if current is None:
            raise Stop('Cannot locate effort control')
        if current.text.strip() == wanted:
            return
        current.click()
        time.sleep(1)
        matches = [e for e in driver.find_elements('xpath', f'//*[normalize-space(text())="{wanted}"]') if e.is_displayed()]
        if not matches:
            self.screenshot('effort_missing')
            raise Stop(f'Effort {wanted!r} not offered')
        matches[-1].click()
        time.sleep(1)
        shown = next((e for t in levels for e in [wf.exact(driver, t)] if e is not None), None)
        if shown is None or shown.text.strip() != wanted:
            self.screenshot('effort_failed')
            raise Stop(f'Effort did not change to {wanted!r}')

    def _select_multiview_tab(self):
        """Hover the upload card, click its Multi-view tab (a real mode switch: in Single image mode several
        files become separate jobs) and verify `--active: 1` on .generation-tabs. (live 2026-10-22)"""
        wf, driver = self.wf, self.driver
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.by import By
        label = next((e for e in driver.find_elements(By.XPATH, '//*[normalize-space(text())="Text to Image/3D"]')
                      if e.is_displayed()), None)
        if label is None:
            raise Stop('Upload card not found')
        plus = driver.execute_script(
            'const r=arguments[0].getBoundingClientRect(); return document.elementFromPoint(r.left+r.width/2, r.top-60)', label)
        ActionChains(driver).move_to_element(plus).perform()
        time.sleep(1.5)
        tab = next((e for e in driver.find_elements(By.XPATH, '//*[normalize-space(text())="Multi-view"]')
                    if e.is_displayed()), None)
        if tab is None:
            self.screenshot('multiview_tab_missing')
            raise Stop('Multi-view tab not found')
        tab.click()
        time.sleep(1.5)
        active = driver.execute_script('const t=document.querySelector(".generation-tabs"); return t && t.getAttribute("style")')
        if not active or '--active: 1' not in active:
            self.screenshot('multiview_tab_failed')
            raise Stop(f'Multi-view mode did not activate (tabs style {active!r})')

    def prepare_multiview(self, paths, config):
        """Gen-2.5 + effort, Multi-view mode, then upload all views at once and (best effort) set directions."""
        wf, driver = self.wf, self.driver
        from selenium.webdriver.common.by import By
        if not wf.exact(driver, config['model']):
            raise Stop(f'Select {config["model"]} in the browser before proceeding')
        self._pick_effort(config['effort'], config.get('effort_levels', ['Extreme-Low', 'Low', 'Medium', 'High', 'Extreme-High']))
        wants_multiple = bool(config.get('multi_view'))
        if wants_multiple:
            self._select_multiview_tab()
        inputs = [e for e in driver.find_elements(By.CSS_SELECTOR, 'input[type=file]')
                  if 'png' in (e.get_attribute('accept') or '') and
                  (e.get_attribute('multiple') is not None) == wants_multiple]
        if not inputs:
            raise Stop('No matching image upload input found')
        inputs[0].send_keys('\n'.join(str(p) for _, p in paths))
        time.sleep(6)
        cards = driver.find_elements(By.CSS_SELECTOR, '.rodin-homepage-upload-strip > div > div')
        if len(cards) != len(paths):
            self.screenshot('upload_count_mismatch')
            raise Stop(f'Expected {len(paths)} uploaded thumbnails, found {len(cards)}')
        info = {'thumbnails': len(cards), 'directions_assigned': False}
        if wants_multiple and config.get('directions'):
            info['directions_assigned'] = self._assign_directions(cards, paths, config)
            if not info['directions_assigned'] and config.get('directions_required'):
                raise Stop('Direction picker not found and directions_required is set')
        return info

    def _assign_directions(self, cards, paths, config):
        """Best effort: the #oripopup picker never opened on hover/click of a thumbnail in the 2026-10-22 probe
        (Rodin's multi-view is pose-free; directions are optional and also editable later in the Geometry card).
        Try clicking each card's top-left corner; assign only when the popup really opens. Returns True if all set."""
        wf, driver = self.wf, self.driver
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.by import By
        pops = driver.find_elements(By.CSS_SELECTOR, '#oripopup')
        if not pops:
            return False
        done = 0
        for card, (name, _) in zip(cards, paths):
            ActionChains(driver).move_to_element_with_offset(card, -70, -70).click().perform()
            time.sleep(1)
            if 'opacity-0' in (pops[0].get_attribute('class') or ''):
                return False                  # popup did not open: leave directions unset, never guess
            option = wf.exact(pops[0], config['directions'][name])
            if option is None:
                return False
            option.click()
            time.sleep(1)
            done += 1
        return done == len(paths)

    def click_free_preview(self):
        wf, driver = self.wf, self.driver
        from selenium.webdriver.common.action_chains import ActionChains
        generate = wf.exact(driver, 'GENERATE')
        if generate is None:
            raise Stop('Main Generate control not found')
        ActionChains(driver).move_to_element(generate).move_by_offset(-60, -30).move_to_element(generate).perform()
        time.sleep(1)
        free = wf.exact(driver, 'Free Preview')
        if free is None:
            self.screenshot('free_preview_guard_failed')
            raise Stop('Generate does not explicitly say Free Preview; stopping')
        free.click()

    def wait_model_url(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if model_id(self.driver.current_url):
                return self.driver.current_url
            if self.insufficient_balance():     # refused: no model will appear
                return None
            time.sleep(2)
        return None

    # -- collect
    def open_model(self, url):
        self.wf.navigate(self.driver, url)
        time.sleep(4)
        self._require_signed_in()

    def observe_stage(self):
        wf, driver = self.wf, self.driver
        stage = wf.observe(driver)['stage']
        if stage == 'unknown':
            pack, material = wf.scope(driver, wf.PACK), wf.scope(driver, wf.MATERIAL)
            action = bool(material and (wf.exact(material, 'Confirm') or wf.exact(material, 'Generate')))
            stage = derive_stage(stage, pack.text if pack else '', bool(pack and wf.exact(pack, 'Download')), action)
        return stage

    def wait_stage(self, stages, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._require_signed_in()
            stage = self.observe_stage()
            if stage in stages:
                self.screenshot(stage)
                return stage
            time.sleep(5)
        raise TimeoutError('Stage did not finish; the saved model can be resumed later')

    def _control(self, kind):
        wf, driver = self.wf, self.driver
        from selenium.webdriver.common.by import By
        if kind == 'geometry':
            return driver.find_element(By.CSS_SELECTOR, '[data-geometry-confirm-mode="primary"]')
        material = wf.scope(driver, wf.MATERIAL)
        name = 'Generate' if kind == 'material_generate' else 'Confirm'
        control = wf.exact(material, name) if material else None
        if control is None:
            raise Stop(f'{name} control not found')
        return control        # (live) a text span inside a div, not a <button>; clicks bubble to its handler

    def read_cost(self, kind):
        from selenium.webdriver.common.action_chains import ActionChains
        control = self._control(kind)
        ActionChains(self.driver).move_to_element(control).perform()
        time.sleep(1)
        # the price span sits beside the label (opacity 0 until hover); read the parent's textContent
        text = self.driver.execute_script(
            'const e=arguments[0]; return ((e.matches("button")?e:e.parentElement)||e).textContent', control)
        return parse_cost(text), ' '.join(text.split())

    MESH_CARD = 'Best Quality'   # the raw-mesh card; "Clean Topology" (retopology) must never be confirmed by this backend
    MESH_CARD_JS = """const label=arguments[0];
        const t=[...document.querySelectorAll('*')].find(e=>e.offsetWidth&&e.children.length===0&&e.innerText&&e.innerText.trim()===label);
        if(!t) return null; let n=t; while(n&&!n.hasAttribute('data-polygon-selected')) n=n.parentElement;
        return n ? {selected:n.getAttribute('data-polygon-selected'), text:n.innerText, title:t} : null"""

    def _hover(self, element, *, nudge=False):
        """Move the pointer onto `element`; `nudge` first leaves and re-enters it so the popover opens."""
        from selenium.webdriver.common.action_chains import ActionChains
        chain = ActionChains(self.driver).move_to_element(element)
        if nudge:
            chain = chain.move_by_offset(-30, 0).move_to_element(element)
        chain.perform()

    def _click_element(self, element):
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(self.driver).move_to_element(element).click().perform()

    def select_mesh_mode(self, config):
        """The geometry confirm popover as it is since November 2026 (live-verified 2026-11-21): two cards, "Clean
        Topology" (server-side retopology: Quad / Tri / N-gon, face slider) and "Best Quality" (the raw, unreduced mesh;
        type Tri, face slider up to 1M). The old "Triangle" option (config `mesh_mode_option`, now legacy and unused) no
        longer exists. This picks the Best Quality card (config `mesh_mode_card` may only name it) and stops unless that
        card reports itself selected (`data-polygon-selected="true"`), is titled Best Quality, reads Tri and the configured
        polygon label (`triangle_polycount_label`, default 1M) and offers no Quad / N-gon. Only the title element found
        INSIDE that card is ever clicked (a label elsewhere, e.g. inside the paid Confirm control, is never touched); a
        missing card is retried without clicking. Selecting is not a purchase; the confirm click and its cost check stay
        in `_step`. Returns the geometry card text plus the card's own text for the record."""
        wf, driver = self.wf, self.driver
        control = self._control('geometry')
        card = config.get('mesh_mode_card') or self.MESH_CARD
        if card != self.MESH_CARD:
            raise Stop(f'config mesh_mode_card {card!r} is not allowed: this backend only confirms the raw {self.MESH_CARD!r} mesh')
        want = config.get('triangle_polycount_label') or '1M'

        def read():
            self._hover(control, nudge=True)
            time.sleep(1.5)
            return driver.execute_script(self.MESH_CARD_JS, card)

        state, clicked = None, False
        for _ in range(3):
            state, clicked = read(), False
            if state and state.get('selected') == 'true':
                break
            if state and state.get('title') is not None:   # the card exists but is not selected: click its own title
                self._click_element(state['title'])
                clicked = True
                time.sleep(1)
        if clicked:   # the last attempt ended with a click: judge the state after it, not before
            state = read()
        if not state:
            self.screenshot('mesh_mode_missing')
            raise Stop(f'Geometry popover has no {card!r} card; the Hyper3D page changed again')
        text = state.get('text') or ''
        words = text.split()
        first = text.strip().splitlines()[0].strip() if text.strip() else ''
        if (state.get('selected') != 'true' or first != card or 'Tri' not in words or want not in words
                or 'Quad' in words or 'N-gon' in words):
            self.screenshot('mesh_mode_not_raw')
            raise Stop(f'{card!r} card is not selected as Tri / {want}: selected={state.get("selected")!r}, text={text!r}')
        self.last_slider = want
        self._hover(control)
        time.sleep(1)
        scope = wf.scope(driver, wf.GEOMETRY)
        return (scope.text if scope else '') + f' | {card} card: ' + ' '.join(words)

    def symmetric_value(self):
        e = next((x for x in self.driver.find_elements('xpath', '//*[starts-with(normalize-space(text()),"Symmetric")]')
                  if x.is_displayed()), None)
        return e.text.strip() if e else None

    def material_effort(self):
        """(live) Material header reads 'Material / Native / High'; returns the panel's first lines and the 8K state."""
        wf, driver = self.wf, self.driver
        material = wf.scope(driver, wf.MATERIAL)
        text = material.text if material else ''
        eight = next((b for b in driver.find_elements('css selector', 'button[aria-pressed]') if b.is_displayed()
                      and b.text.strip() == '8K'), None)
        return {'head': ' | '.join(text.splitlines()[:4]), 'eight_k_on': (eight.get_attribute('aria-pressed') == 'true') if eight else None}

    def dump_geometry_panel(self, label):
        """Save the (sanitised) page HTML and the geometry card text; returns the card text. Read-only."""
        wf, driver = self.wf, self.driver
        html = driver.execute_script('return document.documentElement.outerHTML')
        html = re.sub(r'(data:[a-z/+-]+;base64,)[A-Za-z0-9+/=]{100,}', r'\1…', html)
        html = re.sub(r'<svg.*?</svg>', '<svg/>', html, flags=re.S)
        (self.out / f'{label}.html').write_text(html)
        scope = wf.scope(driver, wf.GEOMETRY)
        text = scope.text if scope else ''
        (self.out / f'{label}.txt').write_text(text)
        return text

    def click(self, kind):
        self._control(kind).click()

    def set_delight(self):
        wf = self.wf
        material = wf.scope(self.driver, wf.MATERIAL)
        delight = wf.exact(material, 'De-light') if material else None
        if delight is None:
            raise Stop('De-light toggle missing')
        button = delight.find_element('xpath', './ancestor::button[1]')
        if button.get_attribute('aria-pressed') != 'true':
            button.click()
        if button.get_attribute('aria-pressed') != 'true':
            raise Stop('Could not enable De-light')
        time.sleep(1)

    def _set_checkbox(self, pack, label, wanted):
        # (live) the role=checkbox element is a bare 12px box; its label is the parent's text
        boxes = [e for e in pack.find_elements('css selector', '[role=checkbox]')
                 if e.is_displayed() and self.driver.execute_script(
                     'return arguments[0].parentElement.innerText.trim()', e) == label]
        if len(boxes) != 1:
            raise Stop(f'Export checkbox {label!r}: found {len(boxes)}')
        box = boxes[0]
        if (box.get_attribute('aria-checked') == 'true') != wanted:
            if box.get_attribute('aria-disabled') == 'true':
                raise Stop(f'Export checkbox {label!r} is disabled but must be {wanted}')
            box.click()
            time.sleep(.5)
            if (box.get_attribute('aria-checked') == 'true') != wanted:
                raise Stop(f'Export checkbox {label!r} did not become {wanted}')

    def export_glb(self, out_dir, config):
        wf = self.wf
        pack = wf.scope(self.driver, wf.PACK)
        if pack is None:
            raise Stop('Export panel not found')
        fmt = next((e for e in pack.find_elements('css selector', '[role=radio]')
                    if e.is_displayed() and e.text.strip() == config['export_format_label']), None)
        if fmt is None:
            raise Stop(f'Export format {config["export_format_label"]!r} not offered')
        fmt.click()
        time.sleep(.5)
        if fmt.get_attribute('aria-checked') != 'true':
            raise Stop('GLB did not select')
        for label, wanted in (config.get('export_checkboxes') or {}).items():
            self._set_checkbox(pack, label, wanted)
        res = next((e for e in pack.find_elements('css selector', '[role=radio]')
                    if e.is_displayed() and e.text.strip() == config['texture_resolution']), None)
        if res is None:
            raise Stop(f'Texture resolution {config["texture_resolution"]!r} not offered')
        res.click()
        time.sleep(.5)
        if res.get_attribute('aria-checked') != 'true':
            raise Stop('Texture resolution did not select')
        panel_text = pack.text
        before = {p.name for p in self.downloads.iterdir()}
        started = time.time()
        wf.exact(pack, 'Download').click()
        deadline = time.monotonic() + config.get('download_timeout_s', 900)
        while time.monotonic() < deadline:
            # Firefox leaves an empty <name>.glb placeholder next to <name>.<rand>.glb.part until the download
            # ends (live: 42 MB), so wait until no .part file remains and the file is non-empty and stable.
            # Firefox renames those files while we look, so a file listed a moment ago may be gone (2026-10-27 crash).
            # Only this click's .part counts: one left by an interrupted earlier collect never finishes
            # (2026-10-27: a finished re-download sat beside a stale .part until the timeout).
            pending = [p for p in self.downloads.iterdir() if p.name not in before
                       and p.name.endswith(('.part', '.crdownload'))]
            fresh = [p for p in self.downloads.iterdir() if p.name not in before
                     and not p.name.endswith(('.part', '.crdownload')) and (st := stat_or_none(p))
                     and st.st_size > 0 and st.st_mtime >= started - 1]
            if fresh and not pending:
                size = st.st_size if (st := stat_or_none(fresh[0])) else -1
                time.sleep(3)
                if (st := stat_or_none(fresh[0])) and st.st_size == size:
                    return extract_glb(fresh[0], Path(out_dir)), panel_text
            time.sleep(3)
        raise TimeoutError('Download not found; resume to download again without regenerating')


def extract_glb(downloaded, out_dir):
    """Place the downloaded GLB (or the single GLB inside a ZIP) at <out>/glb_<name>.glb."""
    downloaded = Path(downloaded)
    if zipfile.is_zipfile(downloaded):
        with zipfile.ZipFile(downloaded) as archive:
            if archive.testzip() is not None:
                raise ValueError('ZIP checksum failed')
            members = [n for n in archive.namelist() if n.lower().endswith('.glb')]
            if len(members) > 1:      # Shaded + PBR both ticked exports two GLBs (base_basic_shaded / _pbr)
                members = [n for n in members if 'pbr' in Path(n).name.lower()] or members
            if len(members) != 1:
                raise ValueError(f'expected exactly one GLB in the ZIP, found {members}')
            target = out_dir.resolve() / ('glb_' + Path(members[0]).name)
            if target.exists():
                raise FileExistsError(f'Export destination exists: {target}')
            with archive.open(members[0]) as src, target.open('wb') as dst:
                shutil.copyfileobj(src, dst)
        return target
    if downloaded.suffix.lower() != '.glb':
        raise ValueError(f'downloaded file is neither GLB nor ZIP: {downloaded.name}')
    target = out_dir.resolve() / ('glb_' + downloaded.name)
    if target.exists():
        raise FileExistsError(f'Export destination exists: {target}')
    try:
        os.link(downloaded, target)  # same bytes as the download: a hard link saves ~40 MB per module
    except OSError:
        shutil.copy2(downloaded, target)
    return target


# --------------------------------------------------------------------------- CLI

def parse_expect(items):
    result = {}
    for item in items or []:
        kind, _, value = item.partition('=')
        if kind not in COST_KINDS or not value:
            raise SystemExit(f'--expect-cost wants kind=N with kind in {COST_KINDS}')
        result[kind] = float(value) if '.' in value else int(value)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('submit')
    p.add_argument('views_dir')
    p.add_argument('out_dir')
    p.add_argument('--dry-run', action='store_true', help='fill the form, screenshot, never press Free Preview')
    p = sub.add_parser('collect')
    p.add_argument('out_dir')
    p.add_argument('--allow-paid', action='store_true')
    p.add_argument('--accept-preview', action='store_true')
    p.add_argument('--expect-cost', action='append', metavar='KIND=N')
    p = sub.add_parser('preview', help='read-only: wait for the free preview, screenshot, dump the Confirm options')
    p.add_argument('out_dir')
    p = sub.add_parser('inspect')
    p.add_argument('out_dir')
    p = sub.add_parser('bind')
    p.add_argument('out_dir')
    p.add_argument('url')
    args = parser.parse_args(argv)
    try:
        if args.command == 'submit':
            result = submit(args.views_dir, args.out_dir, dry_run=args.dry_run)
        elif args.command == 'collect':
            result = collect(args.out_dir, allow_paid=args.allow_paid, accept_preview=args.accept_preview,
                             expected_costs=parse_expect(args.expect_cost))
        elif args.command == 'preview':
            result = preview(args.out_dir)
        elif args.command == 'inspect':
            result = inspect_only(args.out_dir)
        else:
            result = bind(args.out_dir, args.url)
    except GateClosed as exc:
        print(f'PAID GATE CLOSED: {exc}', file=sys.stderr)
        raise SystemExit(EXIT_GATE_CLOSED)
    except Stop as exc:
        print(f'STOPPED: {exc}', file=sys.stderr)
        raise SystemExit(3)
    print(json.dumps({k: result.get(k) for k in ('status', 'stage', 'model_id', 'model_url', 'project_id')}
                     | ({'exports': result['exports']} if 'exports' in result else {}), indent=2, default=str))


if __name__ == '__main__':
    main()
