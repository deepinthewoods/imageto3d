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
material confirm) calls paid_gate.check() when it starts, before it writes its intent and again right before the click. That
last check and the click itself run under the paid-gate transition lock (paid_gate.checked_dispatch; last-check row 6), which
every writer of CONVERT_HOLD / CONVERT_DRAIN / PAUSE / the mode takes too: a hold is written before the check (no click) or
after the click was dispatched, never in between; a lock that stays busy counts as a closed gate. The lock is an OS byte lock
(post-cut-over review, blocker 2): it is held until session.click() has returned, control look-up included, however long that
takes, it never expires, and the kernel releases it if this process dies. The check also reads the manual pause mode
(supervisor_mode.json) and the supervisor's own pause, so a Queue Pause stops the click at once (blocker 3). A closed
gate raises GateClosed: no intent is left behind (one written a moment earlier is withdrawn: nothing was clicked), the CLI prints
`PAID GATE CLOSED: <reason>` and exits EXIT_GATE_CLOSED (4); the caller waits for its gate and runs collect again. The free
preview submit is not gated. Without $IMAGETO3D_PAID_GATE (a manual run) the explicit --allow-paid is the authority.

Free preview resolution (phase 5 review finding 1): the `free_preview` intent is resolved (pending_action cleared,
actions.free_preview gets done_at / model_id / resolved_by) as soon as its model is positively identified: the URL captured at
submit, a `bind`, and on load by collect / preview / inspect of an existing record that is bound to a model. An unidentified
click (no model URL) stays pending.

Bind (recheck-before-cutover finding 12): binding a model positively identifies the free preview, so `bind` (and the normal
submit, and a later load of a bound record) writes the model link, the resolved free intent AND the status `submitted` in ONE
atomic record write when the record was `submission_needs_review` / `submission_attempted` (or `ready_to_submit` with the free
intent still pending: the click was never recorded). A pending PAID intent, or a record without a free-preview intent, keeps its
status and its protection.

Paid actions (recheck-before-cutover finding 8): a pending geometry / material intent is cleared ONLY on evidence that its paid
stage advanced: the observed server stage lies past the stage the click was made in (geometry_preview -> material_ready ->
material_preview -> export_ready). Seeing the same stage again proves nothing: the intent stays pending, the record gets
`paid_review: {action, stage, at, why}` and collect stops ('needs review'; the click is never repeated automatically; a later run
that finds the stage advanced confirms it). Each paid action records, in `actions.<kind>`: `intent_at`, `observed_cost` (the
credits displayed on the control, checked against the expected cost) and `observed_cost_text`, `stage_before`, `balance_before`;
and once confirmed `confirmed_at`, `confirmed_by: "stage_advanced"`, `stage_after`, `model_id`, `balance_after`, `balance_delta`
(informational: other jobs share the wallet) and `provider_action_id` (only when the session can read one; None otherwise).
An action counts as charged only when it has `confirmed_at`.

Inputs (phase 5 review finding 7; recheck finding 13; last-check row 14): submit copies the views it uploads into a FRESH folder
<out_dir>/upload_inputs/<stamp>-<pid>-<random>/ (exclusive mkdir, never an existing folder), hashes the copies, makes them
READ-ONLY and uploads those copies; it stores `inputs: [{name, sha256, size}]` (upload order, the exact bytes uploaded),
`upload_dir` and the folder's pinned identity `upload_dir_id: {name, dev, ino}` in the record; `record_inputs(record)` reads the
inputs (None for a record written before the field existed = unknown). The pin (upload_pin_problem: `upload_inputs/` and the
folder are plain folders, not links / junctions / reparse points, the folder still has the recorded file id and resolves inside
the output) is re-verified immediately before every temp-file create and every os.replace of the snapshot, before the upload,
before the free click and before every paid step; a mismatch stops there. A record from before the pin keeps the flat
<out_dir>/upload_inputs/ layout. Final-live review, prior item 14: on Windows submit also HOLDS the pinned folder open
(hold_directory: CreateFileW with FILE_FLAG_BACKUP_SEMANTICS and a share mode WITHOUT FILE_SHARE_DELETE) from right after its
exclusive mkdir, through every snapshot write, the upload and the free click, until submit returns: while that handle is open
the folder (and `upload_inputs/` above it) cannot be renamed, moved or deleted, so it cannot be swapped between a pin check and
the os.replace / the browser's own open of the files. The handle is verified to be the entry at that path (same file id), and
a folder that cannot be held stops the submit before anything is written. Residual: the instant between the mkdir and the
open of the handle (a swap there is caught by the pin, which is taken and checked after the handle is open), and other systems
(POSIX has no such share mode: there the pin checks alone apply, as before). Moving an output
folder to another volume changes the file id: its record then needs review before a paid step.
`verify_upload_inputs(record)` checks the pin and re-hashes the folder against the record ([] = identical, None =
unknown): submit runs it immediately before the upload and again before the click (a mismatch sets the record aside: nothing was
generated), collect before every credit-capable intent (a mismatch stops before the paid click). Deleting an output folder on
Windows needs the read-only attribute cleared first (renaming / moving it does not).

Pre-click failures (final-live review, "a rejected upload strands the unpaid variant"): a submit that stops BEFORE the free
click (the upload was rejected: thumbnail count mismatch, signed out, a missing control, ...) sets its record aside as
`studio_submission.not_submitted_<stamp>.json` under the output lock, so a retry submits fresh. That is done only for a record
that proves nothing was clicked (proven_pre_click: status preparing / ready_to_submit, or not_submitted still in place = an
archive interrupted between its status write and its rename, which is then resumed; no action or intent of any kind, no
pending action, no model URL / id, no click time, no export); a record with ANY click intent is never archived this way. A
submit that finds such a stranded record (its process was killed before the click) archives it the same way before it starts;
`set_aside_pre_click(out_dir, why)` does the same for a caller (the v2 monster lane).

Export (final-live review, "interrupted paid export cannot resume"): the downloaded ZIP is extracted into a staging folder
`<out_dir>/.export_staging_<pid>_<random>/` under the output lock and only the complete, CRC-checked GLB is put at
<out_dir>/glb_<name>.glb (hard link / rename: never a half-written file under the final name). When a collect was interrupted
after the GLB was placed and before the export receipt (exports.GLB) was written, the resumed collect downloads again and finds
the destination taken: an existing file with the same size and SHA-256 as the freshly extracted one is ADOPTED into the receipt
(exports.GLB.adopted_existing true); a different file stops with 'needs review' and nothing is overwritten.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
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
# finding 8: the server stage each paid click is made in; the action is confirmed only by a stage past it (STAGE_ORDER)
PAID_STAGE_BEFORE = {'geometry': 'geometry_preview', 'material_generate': 'material_ready', 'material_confirm': 'material_preview'}
STAGE_ORDER = ('geometry_preview', 'material_ready', 'material_preview', 'export_ready')
# finding 12: statuses a positively identified free preview moves on from (to 'submitted')
FREE_CLICKED = ('submission_attempted', 'submission_needs_review')
FREE_INTENT_ONLY = ('ready_to_submit',)   # only while the free intent is still pending (the click-status write was lost)
STAGES_TO_ACT = {'geometry_preview', 'material_ready', 'material_preview', 'export_ready'}


# Stop, the browser lock and the per-output record lock live in browser_lock.py (no selenium import), so native_browser.py
# and workflow.py take the same browser lock when they open the profile.
from browser_lock import (BrowserLock, LOCK_BUSY, LOCK_FILE, LOCK_FILE_ENV, LOCK_POLL_S, LOCK_WAIT_ENV,  # noqa: E402,F401
                          LOCK_WAIT_S, OUTPUT_BUSY, RECORD_LOCK, Stop, lock_wait_seconds, open_profile, output_lock)


ANNOUNCEMENT_WAIT_S = 4   # how long a freshly loaded page is watched for an announcement dialog before the run goes on
BALANCE_WAIT_S = 60   # the header balance loads late (0, then the real one); Generate with 0 shown = "Insufficient Balance"
INSUFFICIENT = 'Insufficient Balance'


def stat_or_none(path):
    try:
        return path.stat()
    except (FileNotFoundError, PermissionError):   # Windows: a file Firefox is renaming/holding
        return None


def _set_aside(record, why):
    """Mark `record` not_submitted and move it to studio_submission.not_submitted_<stamp>[_N].json (a name that is free: an
    earlier one of the same second is never overwritten). The caller holds the output lock. Returns the new path.

    Crash-safe (confirm review, "Pilot, rejected upload"): the status write and the rename are two steps. A record that already
    says `not_submitted` is one whose rename was interrupted: only the rename is done again (its `why` is kept, the resume is
    noted as `archive_resumed`). Callers resume it only for a record that still proves no click (proven_pre_click)."""
    if record.data.get('status') == ARCHIVE_PENDING_STATUS:
        record.save('not_submitted_archive_resumed', archive_resumed={'at': now(), 'why': why})
    else:
        record.save('not_submitted', status=ARCHIVE_PENDING_STATUS, pending_action=None, why=why)
    _archive_fault()
    stem = f'{record.path.stem}.not_submitted_{time.strftime("%Y%m%dT%H%M%S")}'
    aside, serial = record.path.with_name(f'{stem}.json'), 1
    while os.path.lexists(aside):
        serial += 1
        aside = record.path.with_name(f'{stem}_{serial}.json')
    record.path.rename(aside)
    return aside


def not_submitted(record, why):
    """Nothing was generated (no click, or Hyper3D refused it): set the record aside so a re-run submits again."""
    aside = _set_aside(record, why)
    raise Stop(f'{why}; nothing was generated, record moved to {aside.name}, a re-run submits again')


PRE_CLICK_STATUSES = ('preparing', 'ready_to_submit')   # the statuses of a submit that has not reached its click
# _set_aside wrote its status but its rename did not happen (the process was interrupted in between): a pending archive. With
# no click intent it proves "nothing was clicked" like a pre-click status does (not_submitted after a REFUSED click keeps its
# free intent and click time, so it is never taken for one).
ARCHIVE_PENDING_STATUS = 'not_submitted'
UNCLICKED_STATUSES = PRE_CLICK_STATUSES + (ARCHIVE_PENDING_STATUS,)


def _archive_fault():
    """Tests: the point between _set_aside's status write and its rename (patched to raise there)."""


def proven_pre_click(data):
    """True when the record dict `data` PROVES that nothing was clicked for it (final-live review: a rejected upload strands the
    unpaid variant): a hyper3d record still in a pre-click status with no action or intent of any kind (the free intent is
    written before the click, so a click always leaves one), no pending action, no model URL / id, no click time and no export.
    Anything else (any click intent, resolved or not) is never treated as unsubmitted. A `not_submitted` record still IN PLACE
    (confirm review: its archive was interrupted between the status write and the rename) counts under the very same
    conditions: the next submit / set_aside_pre_click resumes the pending rename."""
    return (isinstance(data, dict) and data.get('backend') == 'hyper3d' and data.get('status') in UNCLICKED_STATUSES
            and not data.get('actions') and not data.get('pending_action')
            and not any(data.get(key) for key in ('model_url', 'model_id', 'project_id', 'submit_clicked_at',
                                                  'after_submit_at', 'exports', 'credits_committed')))


def set_aside_pre_click(out_dir, why):
    """Archive the record of `out_dir` aside (studio_submission.not_submitted_<stamp>.json) when it is a proven pre-click
    failure (proven_pre_click), under the output lock, so a retry submits fresh. Returns the archived path, or None when there
    is no record or it is not provably pre-click (a record with any click intent is left exactly as it is). A lock held by
    another process: Stop(OUTPUT_BUSY). A `not_submitted` record left in place by an interrupted archive (no click intent) is
    resumed here: only its pending rename is done."""
    out_dir = Path(out_dir)
    if not (out_dir / RECORD).exists():
        return None
    with output_lock(out_dir):
        try:
            record = Record(out_dir / RECORD)
        except (OSError, ValueError):
            return None
        if not proven_pre_click(record.data):
            return None
        return _set_aside(record, why)


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

    def bind(self, url, resolve=True, how='bind', **changes):
        """Bind the model URL under the output lock (taken here when the caller does not hold it yet; the record is then
        re-read first, so a bind run on its own never writes back a stale copy). A bound model identifies the free preview:
        with `resolve` the model link, the resolved free intent and the status transition to `submitted` (free_preview_changes)
        go into ONE atomic record write together with `changes` (finding 12: no crash can leave a bound record that collect
        refuses). A pending paid intent is never touched."""
        ident = model_id(url)
        if not ident:
            raise ValueError('Expected a Hyper3D model URL ending in a UUID')
        with output_lock(self.path.parent) as outermost:
            if outermost:
                self.reload()
            if self.data.get('model_id') not in (None, ident):
                raise ValueError('This record is already bound to a different model')
            free = self.free_preview_changes(how, ident) if resolve else {}
            self.save('model_linked', model_id=ident, model_url=f'{BASE}/{ident}', project_id=ident, **{**free, **changes})

    def identified_model(self):
        """The model id this record is positively bound to (its model_url parses to a Hyper3D model UUID and model_id does not
        contradict it), else None."""
        ident = model_id(self.data.get('model_url') or '')
        if ident and self.data.get('model_id') not in (None, ident):
            return None
        return ident

    def free_preview_changes(self, how, ident):
        """The record changes that settle the free preview of the positively identified model `ident` (finding 1 + 12), {} when
        there is nothing to settle:
        - a pending `free_preview` intent is resolved (pending_action None, actions.free_preview done_at / model_id / resolved_by);
        - the status becomes `submitted` when the free preview is resolved for this model (just now, or earlier by a writer that
          did not move the status) and the status is FREE_CLICKED (or FREE_INTENT_ONLY with the intent still pending).
        Any other pending action (a paid intent), a missing free-preview entry or one resolved for another model is ambiguous:
        nothing changes, the record stays protected."""
        data = self.data
        if not ident:
            return {}
        pending = data.get('pending_action')
        entry = (data.get('actions') or {}).get('free_preview')
        changes = {}
        if pending == 'free_preview':
            actions = dict(data.get('actions') or {})
            entry = dict(entry) if isinstance(entry, dict) else {}
            entry.update(done_at=now(), model_id=ident, resolved_by=how)
            actions['free_preview'] = entry
            changes.update(pending_action=None, actions=actions)
            movable = FREE_CLICKED + FREE_INTENT_ONLY
        elif pending is None and isinstance(entry, dict) and entry.get('done_at') and entry.get('model_id') == ident:
            movable = FREE_CLICKED          # resolved earlier (the pre-fix bind), the status was left behind
        else:
            return {}
        if data.get('status') in movable:
            changes.update(status='submitted', submitted_by=how)
            if not data.get('after_submit_at'):
                changes['after_submit_at'] = now()
        return changes

    def resolve_free_preview(self, how, **changes):
        """Phase 5 review finding 1 / recheck finding 12: settle the free preview of a positively identified (bound) model in
        ONE save together with `changes` (free_preview_changes: the pending intent is resolved and a status left at
        submission_attempted / submission_needs_review becomes `submitted`). Returns True when it changed the intent or the
        status. An unidentified click (no model URL) and any other pending action stay as they are (`changes`, when given, are
        saved anyway)."""
        free = self.free_preview_changes(how, self.identified_model())
        if not free:
            if changes:
                self.save('updated', **changes)
            return False
        self.save('free_preview_resolved', **{**free, **changes})
        return True

    def restore(self, data, event):
        """Put back a copy of the record taken before an intent that was never clicked (the paid gate closed in between)."""
        self.data = data
        self.save(event)

    def intent(self, action, **details):
        """Write the intent of `action` before its click; `details` (a paid action's observed cost, stage and balance before
        the click) go into actions.<action> in the same write."""
        if self.data.get('pending_action'):
            raise Stop('An earlier submission is unresolved; inspect the saved model first')
        if action in self.data.get('actions', {}):
            raise Stop(f'{action} was already submitted; refusing to repeat it')
        actions = dict(self.data.get('actions', {}))
        actions[action] = {'intent_at': now(), **details}
        self.save('submission_intent', pending_action=action, actions=actions)

    def observed(self, stage, balance=None, provider_action_id=None):
        """Record the observed server stage. A pending PAID intent is cleared only on evidence that its paid stage advanced
        (finding 8): `stage` lies past the stage the click was made in. Then actions.<kind> gets confirmed_at / confirmed_by /
        stage_after / model_id (+ balance_after, balance_delta, provider_action_id when known). The same (or an earlier / unknown)
        stage proves nothing: the intent stays pending and `paid_review` says why. A pending free preview is never resolved by
        a stage (only by its identified model). Returns the action that is still pending (None = nothing unresolved)."""
        pending = self.data.get('pending_action')
        if pending not in PAID_STAGE_BEFORE:
            self.save('server_stage_observed', stage=stage)
            return pending or None
        actions = dict(self.data.get('actions') or {})
        entry = dict(actions.get(pending) or {})
        before = entry.get('stage_before') if entry.get('stage_before') in STAGE_ORDER else PAID_STAGE_BEFORE[pending]
        if stage in STAGE_ORDER and STAGE_ORDER.index(stage) > STAGE_ORDER.index(before):
            entry.update(confirmed_at=now(), confirmed_by='stage_advanced', stage_after=stage, model_id=self.data.get('model_id'),
                         provider_action_id=provider_action_id or entry.get('provider_action_id'))
            if balance is not None:
                entry['balance_after'] = balance
                if isinstance(entry.get('balance_before'), (int, float)):
                    entry['balance_delta'] = round(entry['balance_before'] - balance, 4)
            actions[pending] = entry
            self.save('paid_action_confirmed', stage=stage, pending_action=None, actions=actions, paid_review=None)
            return None
        self.save('server_stage_observed', stage=stage, paid_review={
            'action': pending, 'stage': stage, 'at': now(),
            'why': f'the {pending} intent is pending and the model still shows {stage} (the click was made at {before}): '
                   f'no evidence that the paid stage advanced'})
        return pending


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


def _protect(path):
    """Read-only for everyone (Windows: the read-only attribute): the snapshot cannot be overwritten in place by accident."""
    os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)


def _lstat(path):
    """os.lstat (the entry itself, never what a link points to), None when nothing is there."""
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _is_link(info):
    """A symbolic link, or on Windows any reparse point (junction, symlink, mount point): an entry that leads elsewhere."""
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0)
                                              & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _same_file_path(a, b):
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def _upload_dir(out_dir):
    """<out_dir>/upload_inputs/ (the container of the per-submission snapshot folders) as a real folder of the output dir
    (created when missing); Stop when it is a link / junction or resolves anywhere else (final-verify row 14)."""
    out_dir = Path(out_dir)
    target = out_dir / UPLOAD_DIR
    info = _lstat(target)
    if info is None:
        target.mkdir(parents=True, exist_ok=True)
        info = _lstat(target)
    if info is None or _is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise Stop(f'upload snapshot folder is not a plain folder of the output (a link, junction or file): {target}')
    if not _same_file_path(target, Path(os.path.realpath(str(out_dir))) / UPLOAD_DIR):
        raise Stop(f'upload snapshot folder resolves outside the output: {target} -> {os.path.realpath(str(target))}')
    return target


def _plain_name(name):
    return isinstance(name, str) and name not in ('', '.', '..') and Path(name).name == name


def upload_pin_problem(out_dir, pin):
    """Why <out_dir>/upload_inputs/<pin name> is not (or no longer) the folder a snapshot was written to, else None (last-check
    row 14). `pin` = {name, dev, ino} taken when the folder was created with an exclusive mkdir. Checked: `upload_inputs/` and
    the folder are plain directories (no symbolic link, junction or other reparse point), the folder still has the pinned file
    id (st_dev, st_ino: a folder swapped in under the same name has another one) and resolves to exactly that path inside the
    resolved output dir."""
    if not (isinstance(pin, dict) and _plain_name(pin.get('name')) and all(
            isinstance(pin.get(k), int) and not isinstance(pin.get(k), bool) for k in ('dev', 'ino'))):
        return f'malformed upload folder pin {pin!r}'
    container = Path(out_dir) / UPLOAD_DIR
    target = container / pin['name']
    for entry in (container, target):
        info = _lstat(entry)
        if info is None:
            return f'upload snapshot folder is gone: {entry}'
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            return f'upload snapshot folder is not a plain folder (a link, junction or file): {entry}'
    if (info.st_dev, info.st_ino) != (pin['dev'], pin['ino']):
        return f'upload snapshot folder was replaced (not the folder the snapshot was written to): {target}'
    if not _same_file_path(target, Path(os.path.realpath(str(out_dir))) / UPLOAD_DIR / pin['name']):
        return f'upload snapshot folder resolves outside the output: {target} -> {os.path.realpath(str(target))}'
    return None


def _require_pin(out_dir, pin):
    problem = upload_pin_problem(out_dir, pin)
    if problem:
        raise Stop(f'{problem}; nothing more is written, uploaded or clicked')


@contextlib.contextmanager
def hold_directory(path):
    """Hold the directory `path` open so that it CANNOT be renamed, moved or deleted while the block runs (final-live review,
    prior item 14). Windows: CreateFileW(GENERIC_READ, share READ|WRITE and NOT FILE_SHARE_DELETE, FILE_FLAG_BACKUP_SEMANTICS |
    FILE_FLAG_OPEN_REPARSE_POINT): a rename / delete needs DELETE access, which the share mode of this handle refuses to every
    process (ours too), and a folder above it cannot be renamed while a handle below it is open; files inside can still be
    created, replaced and read (the upload). The handle is opened on the entry itself (never through a junction) and checked
    to BE the entry at `path` (same file id as os.lstat). Yields True while held. Stop when it cannot be opened or is not
    that entry: nothing was written. Other systems have no such share mode: yields False and holds nothing (the pin checks
    alone apply there)."""
    if os.name != 'nt':
        yield False
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel32.CreateFileW
    create.restype = wintypes.HANDLE
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                       wintypes.HANDLE]
    full = os.path.abspath(str(path))
    if not full.startswith('\\\\'):
        full = '\\\\?\\' + full                      # no MAX_PATH limit
    # GENERIC_READ (list the directory): a handle with data access takes part in share-mode checks; share READ | WRITE only
    handle = create(full, 0x80000000, 0x1 | 0x2, None, 3, 0x02000000 | 0x00200000, None)   # OPEN_EXISTING
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise Stop(f'upload snapshot folder could not be held open (WinError {ctypes.get_last_error()}); '
                   f'nothing more is written, uploaded or clicked: {path}')
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        raise Stop(f'upload snapshot folder could not be held open; nothing more is written, uploaded or clicked: {path}') from None
    try:
        held, entry = os.fstat(fd), _lstat(path)
        if (entry is None or _is_link(entry) or not stat.S_ISDIR(entry.st_mode) or not stat.S_ISDIR(held.st_mode)
                or (held.st_dev, held.st_ino) != (entry.st_dev, entry.st_ino)):
            raise Stop(f'upload snapshot folder changed while it was being held (not the folder that was opened); '
                       f'nothing more is written, uploaded or clicked: {path}')
        yield True
    finally:
        os.close(fd)                                  # closes the OS handle too


def _new_upload_dir(out_dir, holds=None):
    """A fresh snapshot folder <out_dir>/upload_inputs/<stamp>-<pid>-<random>/ for ONE submission (last-check row 14): made with
    an exclusive mkdir (an existing entry of that name is never reused: Stop) and pinned at once. With `holds` (a
    contextlib.ExitStack; prior item 14) the folder is held open against renaming (hold_directory) from right after the mkdir
    until the caller closes that stack; the pin is taken and checked while it is held. Returns (path, pin)."""
    container = _upload_dir(out_dir)
    name = f'{time.strftime("%Y%m%dT%H%M%S")}-{os.getpid()}-{os.urandom(6).hex()}'
    target = container / name
    try:
        os.mkdir(target)
    except FileExistsError:
        raise Stop(f'upload snapshot folder already exists and is never reused: {target}') from None
    if holds is not None:
        holds.enter_context(hold_directory(target))
    info = os.lstat(target)
    pin = {'name': name, 'dev': info.st_dev, 'ino': info.st_ino}
    _require_pin(out_dir, pin)
    return target, pin


def _own_destination(target, name):
    """(path, lstat or None) of the final file <target>/<name>, checked (final-verify row 14): `name` is a bare file name, and an
    existing entry is this snapshot's OWN regular file: not a link / junction / reparse point, not a folder, not a hard link
    shared with another name (its attributes and bytes would be another file's too), and it resolves to exactly
    <resolved target>/<name>. Stop otherwise; nothing is changed."""
    if not _plain_name(name):
        raise Stop(f'upload snapshot: not a plain file name: {name!r}')
    path = Path(target) / name
    info = _lstat(path)
    if info is None:
        return path, None
    if _is_link(info) or not stat.S_ISREG(info.st_mode):
        raise Stop(f'upload snapshot destination is a link, junction or not a regular file; not written: {path}')
    if info.st_nlink > 1:
        raise Stop(f'upload snapshot destination is hard-linked to another file ({info.st_nlink} names); not written: {path}')
    if not _same_file_path(path, Path(os.path.realpath(str(target))) / name):
        raise Stop(f'upload snapshot destination resolves outside the snapshot folder; not written: {path}')
    return path, info


def _unprotect_own(path, info):
    """Clear the read-only bit of a destination _own_destination verified (`info`): through a handle opened without following
    links where the OS can, else by path after checking that it is still the same file."""
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    if os.chmod in getattr(os, 'supports_fd', ()):
        fd = os.open(path, flags)
        try:
            now = os.fstat(fd)
            if (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISREG(now.st_mode) or now.st_nlink > 1:
                raise Stop(f'upload snapshot destination changed while it was replaced; not written: {path}')
            os.chmod(fd, stat.S_IREAD | stat.S_IWRITE)
        finally:
            os.close(fd)
        return
    again = _lstat(path)
    if (again is None or _is_link(again) or not stat.S_ISREG(again.st_mode) or again.st_nlink > 1
            or (again.st_dev, again.st_ino) != (info.st_dev, info.st_ino)):
        raise Stop(f'upload snapshot destination changed while it was replaced; not written: {path}')
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


def _replace_own(source, target, name, pinned=None):
    """Put a read-only copy of `source` at <target>/<name> without ever writing THROUGH an existing entry (final-verify row 14):
    the bytes go to a new temp file of the same folder (exclusive create), are hashed there, and the temp file is renamed over
    the destination (os.replace swaps the directory entry; it never follows a link). An existing destination must be the
    snapshot's own file (_own_destination) and only that verified file loses its read-only bit. `pinned` (last-check row 14):
    a callable that raises Stop when `target` is no longer the pinned snapshot folder; it runs immediately before the temp file
    is created and immediately before the os.replace. Returns (sha256, size)."""
    pinned = pinned or (lambda: None)
    path, info = _own_destination(target, name)
    temp = Path(target) / f'.{name}.{os.getpid()}.{os.urandom(4).hex()}.tmp'
    pinned()
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0), 0o600)
    try:
        with os.fdopen(fd, 'wb') as out, open(source, 'rb') as data:
            shutil.copyfileobj(data, out)
        digest, size = sha256_file(temp), os.lstat(temp).st_size
        _protect(temp)
        path, info = _own_destination(target, name)      # looked at again right before the swap
        if info is not None:
            _unprotect_own(path, info)
        pinned()                                         # the folder is still the pinned one: checked right before the swap
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.chmod(temp, stat.S_IREAD | stat.S_IWRITE)
            os.unlink(temp)
        raise
    return digest, size


def snapshot_inputs(plan, out_dir, holds=None):
    """Copy the views of `plan` into a FRESH folder <out_dir>/upload_inputs/<unique>/, hash the copies and make them read-only
    (finding 13): (dir, [{name, sha256, size}] of the uploaded views in upload order, pin). The browser uploads these copies,
    so the recorded hashes are the bytes uploaded; verify_upload_inputs re-hashes them before the upload and before every paid
    step. A views_omitted.json marker is copied too (validate_views reads it). The snapshot of an earlier submission that was
    set aside (not_submitted) stays where it is: every submission gets its own folder.

    Row 14 (final-verify, last-check): the folder is created by an exclusive mkdir and pinned (_new_upload_dir: file id, no
    link / junction / reparse point on the way, resolved inside the output dir); the pin is re-verified immediately before
    every temp-file create and every os.replace (upload_pin_problem), every final destination is checked before anything is
    written (_own_destination) and a file is put in place by renaming a new temp file, never by writing through a name. A
    mismatch stops the submit (Stop) before the record exists: nothing is uploaded or clicked.

    Prior item 14 (final-live review): the folder is held open against renaming (hold_directory; Windows) during all of these
    writes. With `holds` (a contextlib.ExitStack of the caller: submit) the hold lasts until the caller closes that stack
    (through the upload and the click); without it, until this function returns."""
    with contextlib.ExitStack() as own:
        return _snapshot_inputs(plan, out_dir, own if holds is None else holds)


def _snapshot_inputs(plan, out_dir, holds):
    target, pin = _new_upload_dir(out_dir, holds)
    source = Path(plan['views_dir'])
    names = ['views_omitted.json', *plan['images']]
    for name in names:                                   # all destinations first: nothing is written when one is refused
        _own_destination(target, name)

    def pinned():
        _require_pin(out_dir, pin)
    if (source / 'views_omitted.json').is_file():
        _replace_own(source / 'views_omitted.json', target, 'views_omitted.json', pinned)
    written = {name: _replace_own(source / name, target, name, pinned) for name in plan['images']}
    inputs = [{'name': name, 'sha256': written[name][0], 'size': written[name][1]} for name in plan['uploaded']]
    pinned()
    return target, inputs, pin


def _record_upload(record):
    """(record dict, output folder or None, pin or None) of `record` as verify_upload_inputs takes it."""
    if isinstance(record, Record):
        return record.data, record.path.parent, record.data.get('upload_dir_id')
    if isinstance(record, dict):
        return record, None, record.get('upload_dir_id')
    path = Path(record)
    folder = path if path.is_dir() else path.parent
    data = json.loads((folder / RECORD).read_text(encoding='utf-8-sig'))
    return data, folder, data.get('upload_dir_id') if isinstance(data, dict) else None


def verify_upload_inputs(record, upload_dir=None):
    """Re-hash the final upload copies against the record (finding 13): [] when every recorded input is in the record's
    snapshot folder with the recorded size and sha256, else the list of differences; None when the record predates the
    `inputs` field (unknown). The folder is <record folder>/upload_inputs/<pinned name> when the record carries a pin
    (`upload_dir_id`, last-check row 14): the pin is checked first (upload_pin_problem) and a folder that is not the pinned one
    is a difference, nothing is read through it. A record without a pin has the flat <record folder>/upload_inputs/.
    `record`: as record_inputs (a Record, an output dir or a record path; for a bare dict the record's own `upload_dir` is
    read, without a pin check, unless `upload_dir` is passed). ValueError for a malformed inputs field."""
    inputs = record_inputs(record)
    if inputs is None:
        return None
    if upload_dir is None:
        data, folder, pin = _record_upload(record)
        if folder is None:
            upload_dir = data.get('upload_dir')
        elif pin is not None:
            problem = upload_pin_problem(folder, pin)
            if problem:
                return [problem]
            upload_dir = folder / UPLOAD_DIR / pin['name']
        else:
            upload_dir = folder / UPLOAD_DIR
    if not upload_dir:
        return ['the record names no upload_dir']
    upload_dir = Path(upload_dir)
    problems = []
    for item in inputs:
        path = upload_dir / item['name']
        try:
            size = path.stat().st_size
            digest = sha256_file(path) if path.is_file() else None
        except OSError as error:
            problems.append(f'{item["name"]}: unreadable ({type(error).__name__})')
            continue
        if digest is None:
            problems.append(f'{item["name"]}: not a file')
        elif size != item['size'] or digest != item['sha256']:
            problems.append(f'{item["name"]}: {size} bytes sha256 {digest[:12]}, recorded {item["size"]} bytes sha256 {item["sha256"][:12]}')
    return problems


def _require_inputs(record, before):
    """Stop before a paid step when the upload copies no longer match the record (finding 13)."""
    problems = verify_upload_inputs(record)
    if problems:
        record.save('inputs_changed', inputs_problems=problems)
        raise Stop(f'upload inputs differ from the recorded submission before {before} ({"; ".join(problems)}); '
                   f'nothing was clicked, needs review')


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
    """LEGACY header (until the site change seen 2026-11-26): the credit number on the line after `MCP&CLI`. The header now
    shows a percentage of the plan ("35%") there; the credits are read from the wallet panel (parse_wallet)."""
    lines = [line.strip() for line in (body_text or '').splitlines()]
    for i, line in enumerate(lines):
        if line == 'MCP&CLI':
            for nxt in lines[i + 1:i + 4]:
                if re.fullmatch(r'\d+(?:\.\d+)?', nxt):
                    return float(nxt)
    return None


def _credit_number(text):
    """A credit figure as the wallet panel prints it ('861.50', '2,496.00'); None for anything else (e.g. the dash)."""
    text = (text or '').strip()
    if not re.fullmatch(r'\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?', text):
        return None
    return float(text.replace(',', ''))


def _percent(text):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*%', (text or '').strip())
    return float(match.group(1)) if match else None


def parse_wallet(info):
    """Credit balance from the header wallet panel (site change seen 2026-11-26; probe .codex_jobs/hyper3d_probe_20261126/).

    The header pill now reads a percentage of the plan ("35%"); the absolute numbers are in the wallet panel that the pill
    opens on hover and that is in the DOM all the time (`#rechargeSelf section.unified-wallet`): "Plan remaining 35% /
    Available / total 861.50 / 2,496.00 credits" and "Permanent available 10.00 credits" ("Plan credits are used first,
    then permanent credits"). `info` is what SeleniumRodin.WALLET_JS read: header (the pill's text), plan_percent,
    figures [available, total], permanent, meter (aria-valuenow of the plan bar), summary (the panel's text).

    Returns {'credits': plan available + permanent, 'plan_available', 'plan_total', 'permanent', 'plan_percent', 'header'}
    or None when the panel cannot be read WITH CERTAINTY. Nothing is ever derived from the percentage: the two credit
    figures must be printed as numbers, the word `credits` must be there, and every redundant reading must agree with
    them (the plan bar within 0.5 points, the printed percentages within 1 point, the pill equal to the panel's
    percentage, or to the permanent credits when it shows a number). While the account loads the panel reads
    "0.00 / — credits", permanent 0.00 and the pill "0": that is a certain 0 (the caller waits for more).
    """
    if not isinstance(info, dict):
        return None
    figures = info.get('figures')
    if not isinstance(figures, (list, tuple)) or len(figures) != 2:
        return None
    available, permanent = _credit_number(figures[0]), _credit_number(info.get('permanent'))
    if available is None or permanent is None or 'credits' not in (info.get('summary') or '').lower():
        return None
    total_text = (figures[1] or '').strip()
    total = _credit_number(total_text)
    if total is None and total_text not in ('\u2014', '\u2013', '-'):
        return None
    header = (info.get('header') or '').strip()
    plan_percent = _percent(info.get('plan_percent'))
    if total is None:                       # no plan quota shown (seen while the account loads): nothing may be available
        if available != 0 or info.get('meter') not in (None, ''):
            return None
    else:
        if total <= 0 or available > total + 1e-6:
            return None
        share = available / total * 100
        try:
            meter = None if info.get('meter') in (None, '') else float(info['meter'])
        except (TypeError, ValueError):
            return None
        if meter is not None and abs(meter - share) > 0.5:
            return None
        if plan_percent is None or abs(plan_percent - share) > 1:
            return None
    if _percent(header) is not None:        # the pill shows the plan's percentage ...
        if plan_percent is None or _percent(header) != plan_percent:
            return None
    elif _credit_number(header) is None or abs(_credit_number(header) - permanent) > 1e-6:   # ... or the permanent credits
        return None
    return {'credits': round(available + permanent, 4), 'plan_available': available, 'plan_total': total,
            'permanent': permanent, 'plan_percent': plan_percent, 'header': header}


ANNOUNCEMENT_LABEL = re.compile(r"what'?s new|announcement|changelog|release notes|new feature", re.I)
ANNOUNCEMENT_ACK = 'got it'      # the acknowledge button of the site's announcement dialog (seen 2026-11-26)


def announcement_button(dialog):
    """Index of the button that closes an announcement dialog, or None when `dialog` is not one (it is then left alone).

    `dialog` = {'label': aria-label, 'buttons': [{'text', 'aria'}]} of a visible `aria-modal` dialog. An announcement
    (seen 2026-11-26: aria-label "What's New", a video, `Got it`, a `Close` X) is a dialog whose label says so or that has a
    `Got it` button. Only two kinds of button are ever chosen: one whose whole text is `Got it`, else one without text whose
    aria-label is `Close`. Nothing else is clicked, so a button that buys, confirms or generates can never be hit, and a
    dialog of any other kind (a purchase, a confirmation) is never touched."""
    buttons = dialog.get('buttons') or []
    texts = [((b.get('text') or '').strip().lower(), (b.get('aria') or '').strip().lower()) for b in buttons]
    ack = next((i for i, (text, _) in enumerate(texts) if text == ANNOUNCEMENT_ACK), None)
    if ack is not None:
        return ack
    if not ANNOUNCEMENT_LABEL.search(dialog.get('label') or ''):
        return None
    return next((i for i, (text, aria) in enumerate(texts) if not text and aria == 'close'), None)


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
    # prior item 14: the snapshot folder stays held open (not renameable; Windows) through the writes, the upload and the click
    with contextlib.ExitStack() as holds:
        return _submit_held(views_dir, out_dir, plan, config, session_factory, dry_run, holds)


def _submit_held(views_dir, out_dir, plan, config, session_factory, dry_run, holds):
    if (out_dir / RECORD).exists():   # checked before the snapshot: an existing record's upload_inputs/ are never overwritten
        # a record that PROVES nothing was clicked (an earlier submit died before its click) is archived; any other one stops
        if set_aside_pre_click(out_dir, 'an earlier submit stopped before the Free Preview click (no click intent, no model)') is None:
            raise Stop(f'Submission record already exists; collect or inspect instead: {out_dir / RECORD}')
    upload_dir, inputs, pin = snapshot_inputs(plan, out_dir, holds)   # a fresh, pinned, held folder per submission (row 14)
    upload_plan = validate_views(upload_dir, config)   # the copies are what goes up: checked like the originals
    if upload_plan['uploaded'] != plan['uploaded']:
        raise Stop(f'upload snapshot differs from the views: {upload_plan["uploaded"]} != {plan["uploaded"]}')
    _require_pin(out_dir, pin)
    record = Record.create(out_dir / RECORD, {**_record_data(plan, config, views_dir, out_dir),
                                              'upload_dir': str(upload_dir.resolve()), 'upload_dir_id': pin, 'inputs': inputs})
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
            changed = verify_upload_inputs(record)   # finding 13 + row 14: pin checked, re-hashed immediately before the upload
            if changed:
                not_submitted(record, f'the upload copies changed after they were hashed ({"; ".join(changed)})')
            prepared = session.prepare_multiview(paths, config) or {}
            record.save('uploaded', upload_info=prepared)
            session.screenshot('before_submit')
            record.save('ready_to_submit', status='ready_to_submit', displayed_cost_credits=0,
                        ready_at=now())
            if dry_run:
                record.save('dry_run', status='dry_run')
                return record.data
            balance = session.wait_balance(BALANCE_WAIT_S)
            wallet = getattr(session, 'last_wallet', None)   # the panel's figures behind `balance` (None: legacy header / fake)
            record.save('balance_before_submit', balance_before_submit=balance,
                        **({'wallet_before_submit': wallet} if wallet else {}))
        except Stop as exc:
            # final-live review: a Stop here is a failure BEFORE the click (e.g. 'Expected 4 uploaded thumbnails, found 3').
            # The record proves it (no intent of any kind, no model): archived aside, so a retry submits fresh instead of
            # finding a 'preparing' record that collect refuses. A record that is not provably pre-click stays as it is.
            if record.path.exists() and proven_pre_click(record.data):
                not_submitted(record, str(exc))
            raise
        except Exception as exc:                # 2026-10-27: a DNS error left a 'preparing' record collect refuses
            not_submitted(record, f'{type(exc).__name__} before the click: {str(exc).splitlines()[0][:200]}')
        if not balance:                         # 2026-10-27: clicked while the header still showed 0 -> refused
            not_submitted(record, f'the wallet still showed {balance!r} credits after {BALANCE_WAIT_S} s')
        changed = verify_upload_inputs(record)  # and again before the click: what was uploaded is what the record says
        if changed:
            not_submitted(record, f'the upload copies changed during the upload ({"; ".join(changed)})')
        record.intent('free_preview')           # written before the click: never repeated
        session.click_free_preview()
        record.save('submission_clicked', status='submission_attempted', submit_clicked_at=now())
        url = session.wait_model_url(600)       # Rodin can sit on GENERATING for minutes when busy
        session.screenshot('after_submit')
        if url and model_id(url):
            # the model is identified: link, resolved free intent (finding 1) and status in ONE atomic write (finding 12)
            record.bind(url, how='submit', after_submit_at=now())
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
    _require_inputs(record, kind)   # finding 13: the uploaded evidence must still be the recorded one
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
    _require_inputs(record, kind)
    paid_gate.check()          # immediately before the intent (DESIGN_PHASE4 I.8): closed = no intent written
    before = json.loads(json.dumps(record.data))
    balances = record.data.get('balances') or []
    record.intent(kind, observed_cost=cost, observed_cost_text=text, stage_before=record.data.get('stage'),
                  balance_before=balances[-1].get('credits') if balances else None)
    record.save('credits_committed', credits_committed=committed)
    return before


def _click(record, session, kind, before):
    """The credit-capable click, with the paid gate checked right before it (check + click under the gate transition lock:
    paid_gate.checked_dispatch). A gate that closed after the intent was written
    puts the record back as it was before the intent (nothing was clicked, so nothing may stay pending) and raises GateClosed."""
    try:
        # last-check row 6: the final gate check and the click are one critical section under the transition lock that every
        # hold / drain / pause / mode writer takes; a busy lock counts as a closed gate. Post-cut-over blocker 2: session.click
        # looks its control up before it clicks, inside this section; the lock is an OS lock that does not expire meanwhile
        paid_gate.checked_dispatch(lambda: session.click(kind))
    except paid_gate.GateClosed:
        record.restore(before, f'{kind}_intent_withdrawn')
        raise


def _provider_action_id(session, kind):
    """The provider's id of a paid action when the session can read one (`session.provider_action_id(kind)`), else None. The
    live Hyper3D page shows none (2026-11-25); the model id is recorded with every confirmed action instead."""
    reader = getattr(session, 'provider_action_id', None)
    if not kind or reader is None:
        return None
    try:
        return reader(kind) or None
    except Exception:   # noqa: BLE001  an id that cannot be read is simply not recorded
        return None


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
            balance = session.read_balance()
            unresolved = record.observed(stage, balance=balance,
                                         provider_action_id=_provider_action_id(session, record.data.get('pending_action')))
            record.save('balance', balances=record.data.get('balances', []) + [
                {'stage': stage, 'credits': balance, 'at': now()}])
            if unresolved:   # finding 8: the same stage is no evidence; never certified, never clicked again automatically
                raise Stop(f'{unresolved} intent is unresolved and the model shows {stage}: the click may or may not have '
                           f'happened; needs review (not repeated automatically; a later collect confirms it once the stage '
                           f'has advanced)')
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
                return _record_export(record, path, panel, adopted=bool(getattr(session, 'export_adopted', False)))
        raise Stop('stage machine did not reach export_ready; inspect the model and resume')


def _record_export(record, path, panel_text, adopted=False):
    """Write the export receipt (exports.GLB). `adopted`: the GLB was already in place from an interrupted collect and is
    byte-identical (size + SHA-256) to the one downloaded now (place_glb); the receipt says so."""
    path = Path(path)
    report = inspect_glb(path)
    exports = dict(record.data.get('exports', {}))
    exports['GLB'] = {'path': str(path), 'bytes': path.stat().st_size, 'sha256': sha256_file(path),
                      'downloaded_at': now(), 'inspection': report, 'export_panel_text': panel_text,
                      **({'adopted_existing': True} if adopted else {})}
    record.save('exported', exports=exports, studio_result={
        'topology': 'raw', 'faces': report['triangles'], 'inspected_at': now()})
    return _finish(record, exports['GLB'])


def _finish(record, export):
    report = export.get('inspection') or inspect_glb(export['path'])
    if report['problems']:
        record.save('export_needs_review', status='export_needs_review')
        raise Stop('exported GLB failed checks: ' + '; '.join(report['problems']))
    if record.data.get('pending_action') in PAID_STAGE_BEFORE:
        record.observed('export_ready')   # an export exists: every paid stage lies behind it (confirmed, not just cleared)
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
        balance = session.read_balance()
        record.observed(stage, balance=balance)   # never clears a paid intent on the same stage (finding 8)
        info = {'stage': stage, 'balance': balance}
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

    DIALOGS_JS = """
    const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
    return [...document.querySelectorAll('[role=dialog][aria-modal=true]')].filter(vis).map(d => {
        const buttons = [...d.querySelectorAll('button')].filter(vis);
        return {label: d.getAttribute('aria-label') || '', title: ((d.querySelector('h1,h2,h3') || {}).innerText || '').trim().slice(0, 120),
                buttons: buttons.map(b => ({text: (b.innerText || '').trim(), aria: b.getAttribute('aria-label') || ''})),
                elements: buttons};
    });
    """
    ANNOUNCEMENT_ROUNDS = 5

    def open_dialogs(self):
        """The visible modal dialogs ({label, title, buttons, elements}); [] when the page cannot be asked."""
        try:
            return self.driver.execute_script(self.DIALOGS_JS) or []
        except Exception:   # noqa: BLE001  e.g. the page is navigating: the caller's own checks decide
            return []

    def dismiss_announcements(self, wait=0):
        """Close the site's announcement / what's-new dialogs (2026-11-26: a "What's New" dialog covered the workspace, so
        the upload card could not be hovered: 'Multi-view tab not found'). Looks for up to `wait` seconds for one to show,
        then closes each by its `Got it` (else its `Close` X) button, which is all announcement_button() ever returns; any
        other dialog is left alone. An announcement that does not go away stops the run. Returns the titles closed."""
        closed = []
        deadline = time.monotonic() + wait
        for _ in range(self.ANNOUNCEMENT_ROUNDS):
            while True:
                found = [(d, announcement_button(d)) for d in self.open_dialogs()]
                found = [(d, i) for d, i in found if i is not None]
                if found or time.monotonic() >= deadline:
                    break
                time.sleep(0.5)
            if not found:
                return closed
            dialog, index = found[0]
            name = ' - '.join(t for t in (dialog.get('label'), dialog.get('title')) if t) or 'announcement'
            dialog['elements'][index].click()
            closed.append(name)
            print(f'Hyper3D announcement closed: {name} [{dialog["buttons"][index].get("text") or "Close"}]', flush=True)
            time.sleep(1.5)                 # its fade-out; the next round reads the page again
        if any(announcement_button(d) is not None for d in self.open_dialogs()):
            self.screenshot('announcement_not_dismissed')
            raise Stop(f'A Hyper3D announcement dialog did not close ({closed[-1]}); close it in the browser, then run again')
        return closed

    def _covering_dialogs(self):
        """' (a dialog covers the page: ...)' for a Stop message, '' when none is open."""
        names = [d.get('label') or d.get('title') or 'unnamed' for d in self.open_dialogs()]
        return f' (a dialog covers the page: {", ".join(names)})' if names else ''

    WALLET_JS = """
    const pill = document.querySelector('#rechargeSelf');
    if (!pill) return null;
    const text = s => { const e = pill.querySelector(s); return e ? e.textContent.trim() : null; };
    const meter = pill.querySelector('section.unified-wallet [role=meter]');
    return {header: text('span[class*="walletLabel"]'), aria: pill.getAttribute('aria-label'),
            plan_percent: text('section.unified-wallet .wallet-headline strong'),
            figures: [...pill.querySelectorAll('section.unified-wallet .wallet-summary-figures b')].map(e => e.textContent.trim()),
            permanent: text('section.unified-wallet .permanent-head b'),
            meter: meter ? meter.getAttribute('aria-valuenow') : null,
            summary: text('section.unified-wallet')};
    """

    last_wallet = None

    def read_wallet(self):
        """The wallet panel's figures (parse_wallet) or None; kept in `last_wallet` for the record. Read from the DOM:
        the panel is rendered all the time and only shown on hover, so nothing is hovered or clicked near its top-up buttons."""
        try:
            info = self.driver.execute_script(self.WALLET_JS)
        except Exception:   # noqa: BLE001  unreadable = unknown, never a guess
            info = None
        self.last_wallet = parse_wallet(info)
        return self.last_wallet

    def read_balance(self):
        """Credits in the wallet (plan available + permanent, both printed in the wallet panel); None = unreadable.
        A page that still has the header from before 2026-11-26 is read by parse_balance."""
        wallet = self.read_wallet()
        if wallet is not None:
            return wallet['credits']
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
        self.dismiss_announcements(wait=ANNOUNCEMENT_WAIT_S)

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
        tab = None
        for _ in range(2):                  # an announcement that opened late hides the card: close it and hover once more
            self.dismiss_announcements()
            label = next((e for e in driver.find_elements(By.XPATH, '//*[normalize-space(text())="Text to Image/3D"]')
                          if e.is_displayed()), None)
            if label is None:
                raise Stop('Upload card not found' + self._covering_dialogs())
            plus = driver.execute_script(
                'const r=arguments[0].getBoundingClientRect(); return document.elementFromPoint(r.left+r.width/2, r.top-60)', label)
            ActionChains(driver).move_to_element(plus).perform()
            time.sleep(1.5)
            tab = next((e for e in driver.find_elements(By.XPATH, '//*[normalize-space(text())="Multi-view"]')
                        if e.is_displayed()), None)
            if tab is not None:
                break
            time.sleep(2)
        if tab is None:
            self.screenshot('multiview_tab_missing')
            raise Stop('Multi-view tab not found' + self._covering_dialogs())
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
        self.dismiss_announcements()        # nothing may cover the Generate control when its label is read and clicked
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
        self.dismiss_announcements(wait=ANNOUNCEMENT_WAIT_S)

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
        self.dismiss_announcements()        # the price only shows while the control is hovered
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
                    target, self.export_adopted = place_glb(fresh[0], Path(out_dir))
                    return target, panel_text
            time.sleep(3)
        raise TimeoutError('Download not found; resume to download again without regenerating')


EXPORT_STAGING = '.export_staging_'    # <out_dir>/.export_staging_<pid>_<random>/: where a ZIP is extracted before placing


def _settle_existing(candidate, target):
    """`target` (the export destination) already exists: True when it is a plain file with the same size and SHA-256 as the
    complete, verified `candidate` (it is adopted; nothing is written); Stop for review when it is anything else (never
    overwritten, never deleted)."""
    info = _lstat(target)
    if info is None or _is_link(info) or not stat.S_ISREG(info.st_mode):
        raise Stop(f'Export destination exists and is not a plain file; needs review, nothing was overwritten: {target}')
    size = os.stat(candidate).st_size
    if info.st_size == size:
        have, want = sha256_file(target), sha256_file(candidate)
        if have == want:
            return True
        detail = f'sha256 {have[:12]} != downloaded {want[:12]}'
    else:
        detail = f'{info.st_size} bytes != downloaded {size} bytes'
    raise Stop(f'Export destination exists with DIFFERENT content ({detail}); needs review, nothing was overwritten: {target}')


def _place(candidate, target, staging):
    """Put the complete file `candidate` at `target` without ever exposing a half-written file there and without overwriting:
    a hard link (fails when the name is taken), else a rename of a staged copy. An existing `target`: _settle_existing.
    Returns True when the existing file was adopted, False when the file was placed now."""
    if os.path.lexists(target):
        return _settle_existing(candidate, target)
    try:
        os.link(candidate, target)   # same bytes: no second copy on disk (~40 MB per module), and exclusive
        return False
    except FileExistsError:
        return _settle_existing(candidate, target)
    except OSError:
        pass                         # no hard links here (another volume / file system): a staged copy, then a rename
    staged = Path(candidate)
    if staged.parent != staging:
        staging.mkdir(exist_ok=True)
        staged = staging / target.name
        shutil.copy2(candidate, staged)
    if os.path.lexists(target):
        return _settle_existing(staged, target)
    os.rename(staged, target)
    return False


def place_glb(downloaded, out_dir):
    """Place the downloaded GLB (or the single GLB inside a ZIP) at <out>/glb_<name>.glb: (path, adopted).

    Final-live review ("interrupted paid export cannot resume"): runs under the output lock (re-entrant for the collect that
    holds it). A ZIP member is extracted into the staging folder <out>/.export_staging_<pid>_<random>/ first (staging folders
    left by a killed run are removed: only the lock holder makes them), so the final name only ever holds a complete file.
    When the destination already exists (a collect interrupted after the placement and before the export receipt, then
    resumed): a file identical to the freshly downloaded, CRC-checked one (size + SHA-256) is adopted (`adopted` True); a
    different one raises Stop for review and is left untouched."""
    downloaded, out_dir = Path(downloaded), Path(out_dir).resolve()
    with output_lock(out_dir):
        for stale in out_dir.glob(EXPORT_STAGING + '*'):
            shutil.rmtree(stale, ignore_errors=True)
        staging = out_dir / f'{EXPORT_STAGING}{os.getpid()}_{os.urandom(4).hex()}'
        try:
            if zipfile.is_zipfile(downloaded):
                with zipfile.ZipFile(downloaded) as archive:
                    if archive.testzip() is not None:
                        raise ValueError('ZIP checksum failed')
                    members = [n for n in archive.namelist() if n.lower().endswith('.glb')]
                    if len(members) > 1:      # Shaded + PBR both ticked exports two GLBs (base_basic_shaded / _pbr)
                        members = [n for n in members if 'pbr' in Path(n).name.lower()] or members
                    if len(members) != 1:
                        raise ValueError(f'expected exactly one GLB in the ZIP, found {members}')
                    target = out_dir / ('glb_' + Path(members[0]).name)
                    os.mkdir(staging)
                    staged = staging / target.name
                    with archive.open(members[0]) as src, staged.open('wb') as dst:
                        shutil.copyfileobj(src, dst)
                        dst.flush()
                        os.fsync(dst.fileno())
                    if staged.stat().st_size != archive.getinfo(members[0]).file_size:
                        raise ValueError('extracted GLB is incomplete')
                return target, _place(staged, target, staging)
            if downloaded.suffix.lower() != '.glb':
                raise ValueError(f'downloaded file is neither GLB nor ZIP: {downloaded.name}')
            target = out_dir / ('glb_' + downloaded.name)
            return target, _place(downloaded, target, staging)
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def extract_glb(downloaded, out_dir):
    """Place the downloaded GLB (or the single GLB inside a ZIP) at <out>/glb_<name>.glb and return that path (place_glb: staged
    extraction; an identical file already in place is adopted, a different one stops for review)."""
    return place_glb(downloaded, out_dir)[0]


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
