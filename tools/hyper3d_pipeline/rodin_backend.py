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
- No Smart Low-poly: the geometry step selects config `mesh_mode_option` (raw / high-poly).

The browser part (SeleniumRodin) drives the native Firefox profile of this folder (see README.md) and
is UNVERIFIED against a signed-in session; everything else is offline-tested with a fake session.
"""
import argparse
import hashlib
import json
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
RECORD = 'studio_submission.json'
COST_KINDS = ('geometry', 'material_generate', 'material_confirm')
STAGES_TO_ACT = {'geometry_preview', 'material_ready', 'material_preview', 'export_ready'}


class Stop(Exception):
    """A fail-closed stop: nothing wrong happened, a guard or a missing permission halted the run."""


# --------------------------------------------------------------------------- config / inputs

def load_config(path=CONFIG):
    config = json.loads(Path(path).read_text())
    for key in ('view_order', 'directions', 'mesh_mode_option', 'expected_costs', 'workspace_url'):
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

    With multi_view false only the first view (front) is used. Never resizes anything.
    """
    views_dir = Path(views_dir)
    names = list(config['view_order'])
    for name in names:
        if not (views_dir / name).is_file():
            raise ValueError(f'Missing {name}')
    dims = [png_info(views_dir / name) for name in names]
    if any(d != dims[0] for d in dims):
        raise ValueError(f'Multi-view inputs have different native dimensions: {dims}')
    used = names if config.get('multi_view') else names[:1]
    if config.get('max_images') and len(used) > config['max_images']:
        raise ValueError(f'{len(used)} views exceed the max_images={config["max_images"]} of this config')
    return {'views_dir': str(views_dir.resolve()), 'images': names, 'uploaded': used,
            'input_resolution': list(dims[0])}


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------- record (intent guard)

class Record:
    """studio_submission.json with write-before-click intents; mirrors job_state.Job semantics."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text())

    @classmethod
    def create(cls, path, data):
        path = Path(path)
        if path.exists():
            raise Stop(f'Submission record already exists; collect or inspect instead: {path}')
        atomic_json(path, data)
        return cls(path)

    def save(self, event, **changes):
        self.data.update(changes)
        self.data['updated_at'] = now()
        atomic_json(self.path, self.data)
        with self.path.with_name('events.jsonl').open('a') as stream:
            stream.write(json.dumps({'at': now(), 'event': event, **changes}, default=str) + '\n')

    def bind(self, url):
        ident = model_id(url)
        if not ident:
            raise ValueError('Expected a Hyper3D model URL ending in a UUID')
        if self.data.get('model_id') not in (None, ident):
            raise ValueError('This record is already bound to a different model')
        self.save('model_linked', model_id=ident, model_url=f'{BASE}/{ident}', project_id=ident)

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
        'settings': {'workflow': 'Rodin / Image to 3D' + (' / Multi-view' if config.get('multi_view') else ''),
                     'model': config['model'], 'effort': config['effort'],
                     'directions': {k: config['directions'].get(k) for k in plan['uploaded']},
                     'mesh_mode': config['mesh_mode_option'], 'mesh_simplify': config.get('mesh_simplify', False),
                     'smart_low_poly': False, 'export_checkboxes': config.get('export_checkboxes'),
                     'de_light': config.get('de_light', True), 'format': config['export_format_label'],
                     'texture': config['texture_resolution']},
        'status': 'preparing', 'started_at': now(), 'model_id': None, 'model_url': None,
        'project_id': None, 'pending_action': None, 'actions': {}, 'stage': 'new',
        'free_retries': bool(config.get('free_retries')),
    }


def submit(views_dir, out_dir, *, config=None, session_factory=None, dry_run=False):
    """Upload the views and start the FREE preview; leaves status 'submitted' with the model URL."""
    config = config or load_config()
    out_dir = Path(out_dir)
    plan = validate_views(views_dir, config)
    out_dir.mkdir(parents=True, exist_ok=True)
    record = Record.create(out_dir / RECORD, _record_data(plan, config, views_dir, out_dir))
    session_factory = session_factory or (lambda: SeleniumRodin(config, out_dir))
    with session_factory() as session:
        session.open_workspace()
        paths = [(name, Path(plan['views_dir']) / name) for name in plan['uploaded']]
        prepared = session.prepare_multiview(paths, config) or {}
        record.save('uploaded', upload_info=prepared)
        session.screenshot('before_submit')
        record.save('ready_to_submit', status='ready_to_submit', displayed_cost_credits=0,
                    ready_at=now())
        if dry_run:
            record.save('dry_run', status='dry_run')
            return record.data
        record.intent('free_preview')           # written before the click: never repeated
        session.click_free_preview()
        record.save('submission_clicked', status='submission_attempted', submit_clicked_at=now())
        url = session.wait_model_url(90)
        session.screenshot('after_submit')
        if url and model_id(url):
            record.bind(url)
            record.save('submitted', status='submitted', after_submit_at=now())
        else:
            record.save('needs_review', status='submission_needs_review', after_submit_at=now())
    return record.data


def _step(record, session, config, kind, expected, allow_paid, accept_preview=False, prepare=None):
    """Guard one credit-capable click: permissions, option selection, displayed cost, screenshot, intent."""
    if not allow_paid:
        raise Stop(f'{kind} may spend credits; pass --allow-paid')
    if kind == 'geometry' and not accept_preview:
        session.screenshot('review_required')
        raise Stop('Preview ready: inspect review_required.png / the model URL, then pass --accept-preview')
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
    record.intent(kind)
    record.save('credits_committed', credits_committed=committed)


def collect(out_dir, *, allow_paid=False, accept_preview=False, expected_costs=None,
            config=None, session_factory=None):
    """Resume by server stage: confirm raw geometry, De-light material, export GLB, record exports.GLB."""
    config = config or load_config()
    out_dir = Path(out_dir)
    record = Record(out_dir / RECORD)
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
                _step(record, session, config, 'geometry', expected, allow_paid, accept_preview,
                      prepare=lambda: record.save('mesh_mode_selected',
                                                  geometry_options_seen=session.select_mesh_mode(config)))
                record.save('symmetric_selector', symmetric_selector=session.symmetric_value())
                session.click('geometry')
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
                _step(record, session, config, 'material_generate', expected, allow_paid)
                session.click('material_generate')
            elif stage == 'material_preview':
                _step(record, session, config, 'material_confirm', expected, allow_paid)
                session.click('material_confirm')
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
    cost. Picks the configured mesh option (a selection, not a purchase) but NEVER clicks Confirm."""
    config = config or load_config()
    out_dir = Path(out_dir)
    record = Record(out_dir / RECORD)
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
    """Read-only: reopen the model, record the server stage and a screenshot."""
    config = config or load_config()
    out_dir = Path(out_dir)
    record = Record(out_dir / RECORD)
    if not record.data.get('model_url'):
        return record.data
    session_factory = session_factory or (lambda: SeleniumRodin(config, out_dir))
    with session_factory() as session:
        session.open_model(record.data['model_url'])
        stage = session.observe_stage()
        session.screenshot('resume_inspected')
        record.save('resume_inspected', last_observation={'stage': stage})
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

    def __enter__(self):
        import workflow as wf
        from native_browser import PROFILE, BINARY, GECKODRIVER, check_profile
        from selenium import webdriver
        from selenium.webdriver.firefox.options import Options
        from selenium.webdriver.firefox.service import Service
        self.wf = wf
        try:
            check_profile(PROFILE)
        except ValueError as exc:
            raise Stop(f'Firefox profile is open elsewhere: {exc}')
        self.downloads.mkdir(parents=True, exist_ok=True)
        options = Options()
        options.page_load_strategy = 'eager'
        options.binary_location = BINARY
        options.add_argument('-profile')
        options.add_argument(str(PROFILE))
        options.set_preference('browser.download.folderList', 2)
        options.set_preference('browser.download.dir', str(self.downloads.resolve()))
        options.set_preference('browser.download.useDownloadDir', True)
        options.set_preference('browser.helperApps.neverAsk.saveToDisk',
                               'model/gltf-binary,application/octet-stream,application/zip,application/x-zip-compressed')
        self.driver = webdriver.Firefox(service=Service(GECKODRIVER), options=options)
        self.driver.set_window_size(1500, 1000)
        self.driver.set_page_load_timeout(45)
        return self

    def __exit__(self, *exc):
        if self.driver:
            self.driver.quit()

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

    def _require_signed_in(self, timeout=30):
        """Poll until the page shows a settled signed-in or signed-out state (the header loads late)."""
        deadline = time.monotonic() + timeout
        state = 'unknown'
        while time.monotonic() < deadline:
            state = classify_auth(self.driver.execute_script(self.AUTH_JS))
            if state == 'signed_in':
                return
            if state == 'signed_out':
                self.screenshot('signed_out')
                raise Stop('Hyper3D profile is signed out (Login link / NO_CURRENT_USER / Google One Tap); '
                           'run ./native.sh login, sign in, close Firefox, resume')
            time.sleep(2)
        self.screenshot('auth_unknown')
        raise Stop('Could not confirm a signed-in Hyper3D session (no Logout entry found)')

    def read_balance(self):
        return parse_balance(self.driver.find_element('tag name', 'body').text)

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

    def select_mesh_mode(self, config):
        """Open the confirm popover and pick the Triangle (raw) mode with Simplify off. UNVERIFIED popover."""
        wf, driver = self.wf, self.driver
        from selenium.webdriver.common.action_chains import ActionChains
        control = self._control('geometry')
        ActionChains(driver).move_to_element(control).move_by_offset(-30, 0).move_to_element(control).perform()
        time.sleep(1)
        option = wf.exact(driver, config['mesh_mode_option'])
        if option is None:
            self.screenshot('mesh_mode_missing')
            raise Stop(f'Mesh option {config["mesh_mode_option"]!r} not offered')
        ActionChains(driver).move_to_element(option).click().perform()
        time.sleep(1)
        card = driver.execute_script("""const x=[...document.querySelectorAll('*')].find(e=>e.children.length===0&&e.innerText&&e.innerText.includes('Detailed sharp triangular mesh')&&e.offsetWidth);
            if(!x) return ''; let n=x; for(let i=0;i<3&&n.parentElement;i++) n=n.parentElement; return n.innerText""")
        self.last_slider = parse_poly_label(card)
        want = config.get('triangle_polycount_label')
        if want and self.last_slider != want:
            self.screenshot('triangle_slider_not_max')
            raise Stop(f'Triangle polygon slider reads {self.last_slider!r}, expected {want!r}; not confirming')
        simplify = wf.exact(driver, 'Simplify')
        if simplify is not None and not config.get('mesh_simplify'):
            node, state = simplify, None
            for _ in range(4):                                    # the state sits on the label or an ancestor
                state = node.get_attribute('aria-checked') or node.get_attribute('aria-pressed')
                if state is not None:
                    break
                node = node.find_element('xpath', '..')
            if state is None:
                self.screenshot('simplify_state_unknown')
                raise Stop('Cannot read the Simplify state; refusing to risk a reduced mesh')
            if state == 'true':
                node.click()
                time.sleep(.5)
                if node.get_attribute('aria-checked') == 'true' or node.get_attribute('aria-pressed') == 'true':
                    raise Stop('Could not switch Simplify off')
        ActionChains(driver).move_to_element(control).perform()
        time.sleep(1)
        scope = wf.scope(driver, wf.GEOMETRY)
        return ((scope.text if scope else '') + f'\n[triangle slider: {self.last_slider}]')

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
            pending = [p for p in self.downloads.iterdir() if p.name.endswith(('.part', '.crdownload'))]
            fresh = [p for p in self.downloads.iterdir() if p.name not in before
                     and not p.name.endswith(('.part', '.crdownload')) and p.stat().st_size > 0
                     and p.stat().st_mtime >= started - 1]
            if fresh and not pending:
                size = fresh[0].stat().st_size
                time.sleep(3)
                if fresh[0].stat().st_size == size:
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
            record = Record(Path(args.out_dir) / RECORD)
            record.bind(args.url)
            result = record.data
    except Stop as exc:
        print(f'STOPPED: {exc}', file=sys.stderr)
        raise SystemExit(3)
    print(json.dumps({k: result.get(k) for k in ('status', 'stage', 'model_id', 'model_url', 'project_id')}
                     | ({'exports': result['exports']} if 'exports' in result else {}), indent=2, default=str))


if __name__ == '__main__':
    main()
