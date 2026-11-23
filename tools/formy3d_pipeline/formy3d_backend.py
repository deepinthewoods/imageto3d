"""formy3d.com (Tripo3D H3.1, multi-view) as a third image->3D backend for convert_job.py.

  formy3d_backend.py submit <views_dir> <out_dir> [--dry-run] [--allow-paid] [--expect-cost N]
  formy3d_backend.py collect <out_dir>
  formy3d_backend.py preview <out_dir> [--views <views_dir>]   read-only: sign-in, balance, form, displayed cost
  formy3d_backend.py inspect <out_dir>                        read-only: look the generation up in My 3D Models
  formy3d_backend.py bind <out_dir> <generation-uuid>          recover a submit interrupted before the id was captured

Contract with convert_job.py: <out_dir>/studio_submission.json is the record Tripo's studio.mjs writes
(`status` submitted -> exported, `project_id`, `exports.GLB.path`) plus `backend: "formy3d"`.

Cost safety (fail closed):
- Generate is the only credit-spending click. It needs --allow-paid, the displayed price (`Generate 3D Model - N`)
  must equal config expected_costs.generate (or --expect-cost N) and stay within budget_credits, and the intent is
  flushed to the record BEFORE the click. A record that already exists is never resubmitted.
- The POST /api/demo/gen-3d the click sends is inspected first (request_guard): a body whose pbr / quad /
  geometry_quality / model disagrees with config.json is aborted before it leaves the browser.
- Download is free (no ledger row) and collect never clicks anything that spends credits.

Browser: Playwright Firefox, persistent profile separate from the user's Firefox, cookies for host formy3d.com copied
from the ordinary profile (formy3d_cookies.py, values never printed). It runs headful on a private X display
(tools/hidden_display.py) so no window appears on the desktop; PIPELINE_SHOW_WINDOWS=1 shows it. Never headless,
never minimised. The live selectors were verified read-only on 2026-10-23; the Generate click and everything after
it are UNVERIFIED until a supervised paid run (see README.md). Offline tests fake the session.
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HYPER3D = ROOT / 'tools/hyper3d_pipeline'
for folder in (str(HYPER3D), str(HERE)):
    if folder not in sys.path:
        sys.path.insert(0, folder)
import rodin_backend as rb  # noqa: E402  (validate_views, inspect_glb, Record, Stop, ...)
from job_state import atomic_json, now  # noqa: E402

Stop = rb.Stop
CONFIG = HERE / 'config.json'
RECORD = rb.RECORD
UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')


# --------------------------------------------------------------------------- config / inputs

def load_config(path=CONFIG):
    config = json.loads(Path(path).read_text())
    for key in ('view_order', 'view_slots', 'expected_costs', 'generator_url', 'texture', 'geometry_quality'):
        if key not in config:
            raise ValueError(f'config.json is missing {key!r}')
    return config


def validate_views(views_dir, config):
    """rodin_backend.validate_views (PNG, 8-bit RGBA, identical size, never resized); `uploaded` maps to slots.
    A three-view module (`views_omitted.json`) simply has no entry for the omitted view's slot (Left/Right/Back stays empty)."""
    plan = rb.validate_views(views_dir, config)
    plan['slots'] = {name: config['view_slots'][name] for name in plan['uploaded']}
    if 'Front View' not in plan['slots'].values():
        raise ValueError('the Front View slot is required but front.png is not among the uploaded views')
    return plan


def parse_generate_cost(text):
    """Credits from the Generate label (`Generate 3D Model - 167`); None while no price is shown."""
    match = re.search(r'(\d+(?:\.\d+)?)\s*$', ' '.join((text or '').split()))
    if not match:
        return None
    value = float(match.group(1))
    return int(value) if value.is_integer() else value


def label_of(text):
    """First non-empty line of an element's text: the control label ('PBR', 'Quad Mesh', 'Texture')."""
    for line in (text or '').splitlines():
        if line.strip():
            return line.strip()
    return ''


def pick_glb(urls, config):
    """The GLB to download from a generation's `result_urls` (a list or its JSON string)."""
    if isinstance(urls, str):
        try:
            urls = json.loads(urls)
        except ValueError:
            urls = []
    glbs = [u for u in (urls or []) if isinstance(u, str) and u.split('?')[0].lower().endswith('.glb')]
    if not glbs:
        raise Stop('the finished generation lists no GLB in result_urls')
    if len(glbs) > 1:
        hint = 'pbr' if config.get('pbr') else config.get('glb_pick', 'model')
        glbs = [u for u in glbs if hint in u.rsplit('/', 1)[-1].lower()] or glbs
    return glbs[0]


def check_request(payload, config):
    """Compare the body the Generate click is about to POST with config.json; returns a list of problems.
    Only fields that are present are compared (names from the client bundle, unverified live)."""
    problems = []
    if not isinstance(payload, dict):
        return ['request body is not a JSON object']
    model = str(payload.get('model', '')).lower()
    if 'tripo' not in model or 'h3.1' not in model:
        problems.append(f'model {payload.get("model")!r} is not Tripo H3.1')
    if config.get('multi_view') and model and 'multiview' not in model.replace('-', '').replace('_', '') \
            and not payload.get('back_image') and not payload.get('left_image') and not payload.get('right_image'):
        problems.append('multi-view was configured but the request carries only one image')
    for key in ('pbr', 'quad'):
        if key in payload and bool(payload[key]) != bool(config.get(key)):
            problems.append(f'{key}={payload[key]!r} but config says {config.get(key)!r}')
    if 'geometry_quality' in payload and str(payload['geometry_quality']).lower() != str(config['geometry_quality']).lower():
        problems.append(f'geometry_quality={payload["geometry_quality"]!r} but config says {config["geometry_quality"]!r}')
    if 'texture_quality' in payload and config['texture'] != 'No textures' \
            and str(payload['texture_quality']).lower() not in (str(config['texture']).lower(), 'standard' if config['texture'] == 'Standard' else 'hd'):
        problems.append(f'texture_quality={payload["texture_quality"]!r} but config says {config["texture"]!r}')
    if config['texture'] != 'No textures' and payload.get('texture') is False:
        problems.append('texture=false but config wants a textured model')
    return problems


def summarize_request(payload):
    """Request body without image data: keys, scalar values, and the size/prefix of every data URL."""
    summary = {}
    for key, value in (payload or {}).items():
        if isinstance(value, str) and value.startswith('data:'):
            summary[key] = {'data_url_prefix': value[:30], 'chars': len(value)}
        elif isinstance(value, (str, int, float, bool)) or value is None:
            summary[key] = value
        else:
            summary[key] = f'<{type(value).__name__}>'
    return summary


# --------------------------------------------------------------------------- record

class FormyRecord(rb.Record):
    """studio_submission.json with write-before-click intents (rodin_backend.Record semantics, formy3d ids)."""

    def bind_generation(self, generation_uuid, request_id=None):
        match = UUID.fullmatch(generation_uuid or '')
        if not match:
            raise ValueError('Expected a formy3d generation uuid (as listed by My 3D Models)')
        ident = generation_uuid.lower()
        if self.data.get('generation_uuid') not in (None, ident):
            raise ValueError('This record is already bound to a different generation')
        self.save('generation_linked', generation_uuid=ident, project_id=ident,
                  **({'request_id': request_id} if request_id else {}))


def _record_data(plan, config):
    return {
        'backend': 'formy3d', 'source': plan['views_dir'], 'images': plan['images'],
        'images_uploaded': plan['uploaded'], 'input_resolution': plan['input_resolution'],
        **({'views_omitted': plan['views_omitted']} if plan.get('views_omitted') else {}),
        'settings': {'workflow': 'formy3d / Tripo3D H3.1' + (' / Multi Images' if config.get('multi_view') else ' / Single Image'),
                     'slots': plan['slots'], 'texture': config['texture'], 'geometry_quality': config['geometry_quality'],
                     'pbr': bool(config.get('pbr')), 'quad': bool(config.get('quad')), 'format': 'GLB',
                     'remove_lighting': None},
        'status': 'preparing', 'started_at': now(), 'generation_uuid': None, 'request_id': None,
        'project_id': None, 'pending_action': None, 'actions': {}, 'stage': 'new',
        'free_retries': bool(config.get('free_retries')),
    }


# --------------------------------------------------------------------------- orchestration

def submit(views_dir, out_dir, *, config=None, session_factory=None, dry_run=False, allow_paid=False,
           expect_cost=None):
    """Fill the form and (only with allow_paid and a matching displayed price) press Generate.

    The record is created at the last moment, just before the intent and the click, so a failure while filling
    the form never leaves a stale record behind. Returns the record data (status `submitted`)."""
    config = config or load_config()
    out_dir = Path(out_dir)
    plan = validate_views(views_dir, config)
    if (out_dir / RECORD).exists():
        raise Stop(f'Submission record already exists; collect or inspect instead: {out_dir / RECORD}')
    out_dir.mkdir(parents=True, exist_ok=True)
    expected = expect_cost if expect_cost is not None else config['expected_costs'].get('generate')
    session_factory = session_factory or (lambda: PlaywrightFormy3d(config, out_dir))
    with session_factory() as session:
        session.require_signed_in()
        balance = session.read_balance()
        session.open_generator()
        paths = [(name, Path(plan['views_dir']) / name) for name in plan['uploaded']]
        upload = session.prepare_form(paths, plan['slots'], config) or {}
        cost = session.read_cost()
        session.screenshot('before_submit')
        data = _record_data(plan, config)
        data.update(upload_info=upload, displayed_cost_credits=cost, balance_start=balance, ready_at=now(),
                    status='ready_to_submit')
        if dry_run:
            data['status'] = 'dry_run'
            atomic_json(out_dir / RECORD, data)
            return data
        if not allow_paid:
            raise Stop(f'Generate spends {cost} credits; nothing was clicked. Pass --allow-paid '
                       f'(displayed cost {cost}, expected {expected})')
        rb.check_cost('generate', cost, expected)
        if balance is not None and cost is not None and balance < cost:
            raise Stop(f'balance {balance} is below the displayed cost {cost}')
        budget = config.get('budget_credits')
        if budget is not None and (cost or 0) > budget:
            raise Stop(f'generate: cost {cost} exceeds budget_credits {budget}')
        record = FormyRecord.create(out_dir / RECORD, data)
        record.intent('generate')                    # flushed before the click: never repeated
        record.save('credits_committed', credits_committed=cost)
        outcome = session.click_generate(config)
        record.save('submission_clicked', status='submission_attempted', submit_clicked_at=now(),
                    request_summary=outcome.get('request_summary'))
        session.screenshot('after_submit')
        ident = outcome.get('generation_uuid')
        if outcome.get('code') == 0 and ident:
            record.bind_generation(ident, outcome.get('request_id'))
            record.save('submitted', status='submitted', after_submit_at=now(), pending_action=None,
                        queue_position=outcome.get('queue_position'))
        else:
            record.save('needs_review', status='submission_needs_review', after_submit_at=now(),
                        response_summary={k: outcome.get(k) for k in ('code', 'message', 'error')})
            raise Stop('Generate was clicked but no generation id came back '
                       f'({outcome.get("message") or outcome.get("error") or "no response"}); check My 3D Models '
                       f'and run: formy3d_backend.py bind {out_dir} <generation-uuid>')
    return record.data


def collect(out_dir, *, config=None, session_factory=None, timeout_s=None):
    """Poll My 3D Models for the generation until it completes, download the GLB, check it, record exports.GLB."""
    config = config or load_config()
    out_dir = Path(out_dir)
    record = FormyRecord(out_dir / RECORD)
    data = record.data
    if data.get('backend') != 'formy3d':
        raise Stop(f'{record.path} is not a formy3d record')
    if data.get('status') not in ('submitted', 'exported'):
        raise Stop(f'record status {data.get("status")!r}; inspect {record.path}')
    exported = data.get('exports', {}).get('GLB')
    if exported and Path(exported['path']).is_file():
        return rb._finish(record, exported)
    if not data.get('generation_uuid'):
        raise Stop('record has no generation id; find the model in My 3D Models and run bind')
    if timeout_s is None:
        env = os.environ.get('FORMY3D_COLLECT_WAIT_MIN')
        timeout_s = float(env) * 60 if env else config.get('stage_timeout_s', 1800)
    session_factory = session_factory or (lambda: PlaywrightFormy3d(config, out_dir))
    with session_factory() as session:
        session.require_signed_in()
        generation = session.wait_generation(data['generation_uuid'], timeout_s, config.get('poll_interval_s', 15))
        record.save('generation_observed', stage='completed', pending_action=None, generation={
            k: generation.get(k) for k in ('model', 'status', 'credits_consumed', 'created_at', 'completed_at')})
        consumed = generation.get('credits_consumed')
        expected = data.get('displayed_cost_credits')
        if consumed is not None and expected is not None and float(consumed) != float(expected):
            record.save('cost_mismatch', cost_mismatch={'displayed': expected, 'consumed': consumed})
        url = pick_glb(generation.get('result_urls'), config)
        path = session.download_glb(url, generation['uuid'])
        record.save('balance', balance_end=session.read_balance())
        return _record_export(record, path)


def _record_export(record, path):
    path = Path(path)
    report = rb.inspect_glb(path)
    exports = dict(record.data.get('exports', {}))
    exports['GLB'] = {'path': str(path), 'bytes': path.stat().st_size, 'sha256': rb.sha256_file(path),
                      'downloaded_at': now(), 'inspection': report}
    record.save('exported', exports=exports, studio_result={
        'topology': 'triangle', 'faces': report['triangles'], 'inspected_at': now()})
    return rb._finish(record, exports['GLB'])


def preview(out_dir, *, views_dir=None, config=None, session_factory=None):
    """Read-only: sign-in, balance, the form (with the views when given) and the displayed Generate cost.
    Never creates a submission record and never clicks Generate. Writes preview.json + screenshots."""
    config = config or load_config()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    session_factory = session_factory or (lambda: PlaywrightFormy3d(config, out_dir))
    info = {'at': now()}
    with session_factory() as session:
        session.require_signed_in()
        info['balance'] = session.read_balance()
        session.open_generator()
        info['model_chip'] = session.model_chip_text()
        if views_dir:
            plan = validate_views(views_dir, config)
            paths = [(name, Path(plan['views_dir']) / name) for name in plan['uploaded']]
            info['form'] = session.prepare_form(paths, plan['slots'], config)
            info['displayed_cost_credits'] = session.read_cost()
        session.screenshot('preview')
    atomic_json(out_dir / 'preview.json', info)
    return info


def inspect_only(out_dir, *, config=None, session_factory=None):
    """Read-only: look the recorded generation up in My 3D Models and note its server state."""
    config = config or load_config()
    out_dir = Path(out_dir)
    record = FormyRecord(out_dir / RECORD)
    if not record.data.get('generation_uuid'):
        return record.data
    session_factory = session_factory or (lambda: PlaywrightFormy3d(config, out_dir))
    with session_factory() as session:
        session.require_signed_in()
        generation = session.find_generation(record.data['generation_uuid'])
        record.save('inspected', last_observation={
            'found': generation is not None, 'status': (generation or {}).get('status'),
            'credits_consumed': (generation or {}).get('credits_consumed')})
    return record.data


# --------------------------------------------------------------------------- browser (live selectors, paid path unverified)

class PlaywrightFormy3d:
    """Playwright Firefox session on a private X display. Read-only steps and form filling were verified live
    (2026-10-23); click_generate and the status handling are UNVERIFIED and fail closed."""

    def __init__(self, config, out_dir):
        self.config, self.out = config, Path(out_dir)
        self.downloads = self.out / 'downloads'
        self.playwright = self.ctx = self.page = None
        self.last_request = None

    # -- lifecycle
    def __enter__(self):
        from playwright.sync_api import sync_playwright
        import formy3d_cookies
        sys.path.insert(0, str(ROOT / 'tools'))
        from hidden_display import hidden_display
        self.out.mkdir(parents=True, exist_ok=True)
        self.downloads.mkdir(parents=True, exist_ok=True)
        display = hidden_display()        # private Xvfb/Xephyr display; None = ordinary DISPLAY (PIPELINE_SHOW_WINDOWS=1)
        env = {**os.environ, 'DISPLAY': display} if display else None
        profile = Path(os.path.expanduser(self.config.get('profile_dir', '~/.local/share/formy3d-playwright-firefox')))
        self.playwright = sync_playwright().start()
        try:
            self.ctx = self.playwright.firefox.launch_persistent_context(
                str(profile), headless=False, viewport={'width': 1440, 'height': 1000}, accept_downloads=True,
                **({'env': env} if env else {}))
            try:
                self.ctx.add_cookies(formy3d_cookies.firefox_cookies())
            except FileNotFoundError as exc:
                raise Stop(str(exc))
            self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self.playwright:
                self.playwright.stop()
            self.ctx = self.playwright = None

    # -- helpers
    def screenshot(self, label):
        self.page.screenshot(path=str(self.out / f'{label}.png'))

    def _post_json(self, path):
        response = self.ctx.request.post(self.config['site'] + path, headers={'Content-Type': 'application/json'})
        if response.status != 200:
            raise Stop(f'POST {path} answered HTTP {response.status}')
        return response.json()

    def require_signed_in(self):
        """GET /api/auth/session has a `user` only for a valid session (verified live)."""
        self.page.goto(self.config['site'] + '/my-models', wait_until='domcontentloaded', timeout=60000)
        time.sleep(3)
        session = self.ctx.request.get(self.config['site'] + '/api/auth/session').json()
        if not (isinstance(session, dict) and session.get('user')):
            self.screenshot('signed_out')
            raise Stop('formy3d profile is signed out; sign in to formy3d.com in ordinary Firefox '
                       '(formy3d_cookies.py copies its cookies) and run again')

    def read_balance(self):
        """left_credits from POST /api/get-user-credits (verified live); None when it cannot be read."""
        try:
            return self._post_json('/api/get-user-credits')['data']['left_credits']
        except (Stop, KeyError, TypeError, ValueError):
            return None

    # -- form
    def open_generator(self):
        page = self.page
        page.goto(self.config['generator_url'], wait_until='domcontentloaded', timeout=60000)
        page.locator(f'[data-model-family={self.config["model_family"]}]').first.wait_for(timeout=30000)
        time.sleep(2)
        chip = self.model_chip_text()
        if self.config['model_version_label'].lower() not in chip.lower():
            self.screenshot('model_not_h31')
            raise Stop(f'Tripo chip reads {chip!r}, expected {self.config["model_version_label"]}; refusing to continue')

    def model_chip_text(self):
        return ' '.join(self.page.locator(f'[data-model-family={self.config["model_family"]}]').first.inner_text().split())

    def _slot_locator(self, label):
        return self.page.locator('div.space-y-2').filter(
            has=self.page.locator('label', has_text=label)).locator('input[type=file]').first

    def _slot_has_image(self, label):
        return self.page.locator('div.space-y-2').filter(has=self.page.locator('label', has_text=label)) \
            .locator('img').count() > 0

    def _controls(self):
        """Labels of the select triggers and switches inside #controls, in DOM order."""
        return self.page.evaluate("""() => {
            const label = e => (e.parentElement ? e.parentElement.innerText : '');
            const root = document.querySelector('#controls');
            const pick = sel => [...root.querySelectorAll(sel)].map(e => label(e));
            return {selects: pick('button[role=combobox]'), switches: pick('button[role=switch]')};
        }""")

    def _ensure_settings_open(self):
        page = self.page
        if page.locator('#controls button[role=combobox]').count() == 0:
            page.locator('#controls').get_by_text('Settings', exact=True).first.click()
            time.sleep(1.5)
        if page.locator('#controls button[role=combobox]').count() == 0:
            self.screenshot('settings_missing')
            raise Stop('Settings panel did not open')

    def _choose(self, index, option):
        page = self.page
        trigger = page.locator('#controls button[role=combobox]').nth(index)
        if trigger.inner_text().strip().lower() == option.lower():
            return
        trigger.click()
        time.sleep(.6)
        page.get_by_role('option', name=option, exact=True).click()
        time.sleep(1.2)
        if trigger.inner_text().strip().lower() != option.lower():
            self.screenshot('select_failed')
            raise Stop(f'Select did not change to {option!r} (reads {trigger.inner_text().strip()!r})')

    def _switch(self, index, wanted):
        switch = self.page.locator('#controls button[role=switch]').nth(index)
        if (switch.get_attribute('aria-checked') == 'true') != wanted:
            switch.click()
            time.sleep(1.2)
        if (switch.get_attribute('aria-checked') == 'true') != wanted:
            self.screenshot('switch_failed')
            raise Stop(f'Switch {index} did not become {wanted}')

    def prepare_form(self, paths, slots, config):
        """Tripo H3.1 -> Multi Images (or Single), upload the views by slot label, set Texture / Geometry / PBR / Quad,
        verify every value by reading it back. Nothing here spends credits."""
        page = self.page
        info = {'multi_view': bool(config.get('multi_view')), 'slots': {}}
        page.get_by_text('Multi Images' if config.get('multi_view') else 'Single Image', exact=True).first.click()
        time.sleep(1.5)
        if config.get('multi_view'):
            for name, path in paths:
                label = slots[name]
                self._slot_locator(label).set_input_files(str(path))
                time.sleep(3)
                if not self._slot_has_image(label):
                    self.screenshot('upload_failed')
                    raise Stop(f'{name} did not appear in the {label} slot')
                info['slots'][label] = name
        else:
            page.locator('input[type=file]').first.set_input_files(str(paths[0][1]))
            time.sleep(4)
            info['slots']['single'] = paths[0][0]
        self._ensure_settings_open()
        controls = self._controls()
        want_selects = ['Texture', 'Geometry Quality']
        got = [label_of(t) for t in controls['selects']]
        if got[:2] != want_selects:
            self.screenshot('controls_unexpected')
            raise Stop(f'Select controls read {got}, expected {want_selects}; the page layout changed')
        switches = [label_of(t) for t in controls['switches']]
        if switches[:2] != ['PBR', 'Quad Mesh']:
            self.screenshot('controls_unexpected')
            raise Stop(f'Switches read {switches}, expected [PBR, Quad Mesh]; the page layout changed')
        self._choose(0, config['texture'])
        self._choose(1, config['geometry_quality'])
        self._switch(0, bool(config.get('pbr')))
        self._switch(1, bool(config.get('quad')))
        info['read_back'] = {'texture': self.page.locator('#controls button[role=combobox]').nth(0).inner_text().strip(),
                             'geometry_quality': self.page.locator('#controls button[role=combobox]').nth(1).inner_text().strip(),
                             'pbr': self.page.locator('#controls button[role=switch]').nth(0).get_attribute('aria-checked'),
                             'quad': self.page.locator('#controls button[role=switch]').nth(1).get_attribute('aria-checked')}
        return info

    def _generate_button(self):
        return self.page.get_by_role('button', name=re.compile('Generate 3D Model')).first

    def read_cost(self):
        """The credit price on the Generate button once it has settled (two equal reads)."""
        last, deadline = None, time.monotonic() + 20
        while time.monotonic() < deadline:
            cost = parse_generate_cost(self._generate_button().inner_text())
            if cost is not None and cost == last:
                return cost
            last = cost
            time.sleep(1.5)
        return last

    # -- the paid click (UNVERIFIED)
    def click_generate(self, config):
        """Click Generate once; the POST /api/demo/gen-3d it sends is checked first and its response parsed."""
        page = self.page
        pattern = re.compile(r'/api/demo/gen-3d(\?.*)?$')
        problems = []

        def guard(route):
            request = route.request
            try:
                payload = json.loads(request.post_data or '{}')
            except ValueError:
                payload = None
            self.last_request = summarize_request(payload if isinstance(payload, dict) else {})
            found = check_request(payload, config) if config.get('request_guard', True) else []
            if found:
                problems.extend(found)
                route.abort()
            else:
                route.continue_()

        page.route(pattern, guard)
        try:
            with page.expect_response(lambda r: pattern.search(r.url) is not None and r.request.method == 'POST',
                                      timeout=120000) as waiting:
                self._generate_button().click()
            body = waiting.value.json()
        except Exception as exc:  # noqa: BLE001  a timeout/abort: the intent is already recorded, never click again
            if problems:
                raise Stop('request guard aborted Generate (nothing was sent): ' + '; '.join(problems))
            return {'code': None, 'error': str(exc)[:300], 'request_summary': self.last_request}
        finally:
            page.unroute(pattern, guard)
        data = body.get('data') or {}
        return {'code': body.get('code'), 'message': body.get('message'), 'request_id': data.get('requestId'),
                'generation_uuid': data.get('generation_uuid'), 'queue_position': data.get('queuePosition'),
                'request_summary': self.last_request}

    # -- collect
    def _generations(self):
        return self._post_json('/api/get-user-generations')['data']['generations']

    def find_generation(self, generation_uuid):
        for generation in self._generations():
            if str(generation.get('uuid')).lower() == generation_uuid.lower():
                return generation
        return None

    def wait_generation(self, generation_uuid, timeout_s, interval_s):
        deadline = time.monotonic() + timeout_s
        failed = {'failed', 'error', 'cancelled', 'canceled'}
        while time.monotonic() < deadline:
            generation = self.find_generation(generation_uuid)
            status = str((generation or {}).get('status', '')).lower()
            if generation and status == 'completed':
                return generation
            if generation and status in failed:
                raise Stop(f'generation {generation_uuid} ended as {status!r}: {generation.get("error_message")}')
            time.sleep(interval_s)
        raise TimeoutError('Generation did not finish; the saved record can be collected again later')

    def download_glb(self, url, generation_uuid):
        target = self.downloads.parent.resolve() / f'glb_{generation_uuid}.glb'
        if target.exists():
            raise FileExistsError(f'Export destination exists: {target}')
        timeout = self.config.get('download_timeout_s', 600) * 1000
        response = self.ctx.request.get(url, timeout=timeout)
        if response.status != 200:             # fall back to the site's own asset proxy
            from urllib.parse import quote
            response = self.ctx.request.get(f'{self.config["site"]}/api/proxy/glb-remote?url={quote(url, safe="")}',
                                            timeout=timeout)
        if response.status != 200:
            raise Stop(f'GLB download answered HTTP {response.status}')
        body = response.body()
        if body[:4] != b'glTF':
            raise Stop('downloaded file is not a binary glTF')
        target.write_bytes(body)
        return target


# --------------------------------------------------------------------------- CLI

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('submit')
    p.add_argument('views_dir')
    p.add_argument('out_dir')
    p.add_argument('--dry-run', action='store_true', help='fill the form, screenshot, never press Generate')
    p.add_argument('--allow-paid', action='store_true')
    p.add_argument('--expect-cost', type=float, metavar='N')
    p = sub.add_parser('collect')
    p.add_argument('out_dir')
    p = sub.add_parser('preview', help='read-only: sign-in, balance, form and displayed cost')
    p.add_argument('out_dir')
    p.add_argument('--views', metavar='DIR')
    p = sub.add_parser('inspect')
    p.add_argument('out_dir')
    p = sub.add_parser('bind')
    p.add_argument('out_dir')
    p.add_argument('generation_uuid')
    args = parser.parse_args(argv)
    try:
        if args.command == 'submit':
            cost = None if args.expect_cost is None else (int(args.expect_cost) if args.expect_cost.is_integer() else args.expect_cost)
            result = submit(args.views_dir, args.out_dir, dry_run=args.dry_run, allow_paid=args.allow_paid,
                            expect_cost=cost)
        elif args.command == 'collect':
            result = collect(args.out_dir)
        elif args.command == 'preview':
            result = preview(args.out_dir, views_dir=args.views)
        elif args.command == 'inspect':
            result = inspect_only(args.out_dir)
        else:
            record = FormyRecord(Path(args.out_dir) / RECORD)
            record.bind_generation(args.generation_uuid)
            result = record.data
    except Stop as exc:
        print(f'STOPPED: {exc}', file=sys.stderr)
        raise SystemExit(3)
    keys = ('status', 'stage', 'generation_uuid', 'project_id', 'displayed_cost_credits', 'balance', 'model_chip')
    print(json.dumps({k: result.get(k) for k in keys if k in result}
                     | ({'exports': result['exports']} if 'exports' in result else {}), indent=2, default=str))


if __name__ == '__main__':
    main()
