"""One-image Rodin workflow with durable links and resumable UI stages.

No API keys. Uses the dedicated signed-in native Firefox profile.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import time
import zipfile

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

from job_state import Job, BASE, model_id, verify_zip, atomic_json, now
from native_browser import PROFILE, BINARY, GECKODRIVER, check_profile

ROOT = Path(__file__).resolve().parent
GEOMETRY = '[class*="rodin-card-geometry-"][class*="-layout"]'
MATERIAL = '.rodin-card-material-layout'
PACK = '.rodin-pack-panel'


def navigate(driver, url):
    try:
        driver.get(url)
    except TimeoutException:
        # Late third-party resources can keep load pending after the app is usable.
        driver.execute_script('window.stop()')
    time.sleep(3)


def visible(root, css):
    return [e for e in root.find_elements(By.CSS_SELECTOR, css) if e.is_displayed()]


def exact(root, text):
    # These constant UI labels contain no quotes.
    matches = root.find_elements(By.XPATH, f'.//*[normalize-space(text())="{text}"]')
    return next((e for e in matches if e.is_displayed()), None)


def scope(driver, css):
    items = visible(driver, css)
    return items[0] if items else None


def observe(driver):
    geo, mat, pack = (scope(driver, s) for s in (GEOMETRY, MATERIAL, PACK))
    g, m, p = (e.text if e else '' for e in (geo, mat, pack))
    if '/workspace/login' in driver.current_url or visible(driver, 'input[type=password]'):
        stage = 'signed_out'
    elif geo and 'Unlock: Geometry' in m:
        stage = 'geometry_preview' if exact(geo, 'Redo') else 'geometry_processing'
    elif mat and 'Unlock after Material confirmed' in p:
        if exact(mat, 'Confirm'):
            stage = 'material_preview'
        elif exact(mat, 'Generate'):
            stage = 'material_ready'
        else:
            stage = 'material_processing'
    elif mat and pack and 'Options' in m and 'De-light' in m:
        stage = 'export_ready'
    else:
        stage = 'unknown'
    return {'stage': stage, 'model_id': model_id(driver.current_url),
            'progress': [line for line in (g+'\n'+m+'\n'+p).splitlines() if line.endswith('%')]}


def screenshot(driver, job, label):
    driver.save_screenshot(str(job.path.with_name(label + '.png')))


def wait_for(driver, job, stages, timeout=1800):
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        state = observe(driver)
        if state['model_id'] and not job.data['model_id']:
            job.bind(driver.current_url)
        if state != previous:
            job.save('poll', last_observation=state)
            print(json.dumps(state), flush=True)
            previous = state
        if state['stage'] == 'signed_out':
            raise RuntimeError('Sign in using native.sh login, then resume this job')
        if state['stage'] in stages:
            job.observed(state['stage'])
            screenshot(driver, job, state['stage'])
            return state['stage']
        time.sleep(5)
    raise TimeoutError('Stage did not finish; saved model can be resumed later')


def submit_preview(driver, job, wait=True):
    job.verify_input()
    if job.data.get('pending_action') or job.data['actions']:
        raise RuntimeError('Previous submission has no saved model ID. Find it under Mine and use bind; do not resubmit')
    navigate(driver, BASE)
    time.sleep(4)
    if not exact(driver, 'Gen-2.5'):
        raise RuntimeError('Select Gen-2.5 in the browser before proceeding')
    if not exact(driver, 'Medium'):
        effort = next((exact(driver, t) for t in ('Extreme-High', 'High', 'Low', 'Extreme-Low') if exact(driver, t)), None)
        if effort is None:
            raise RuntimeError('Cannot locate effort control')
        effort.click()
        exact(driver, 'Medium').click()
    driver.find_elements(By.CSS_SELECTOR, 'input[type=file]')[0].send_keys(job.data['input']['path'])
    time.sleep(4)
    generate = exact(driver, 'GENERATE')
    if generate is None:
        raise RuntimeError('Main Generate control not found')
    ActionChains(driver).move_to_element(generate).move_by_offset(-60, -30).move_to_element(generate).perform()
    time.sleep(1)
    free = exact(driver, 'Free Preview')
    if free is None:
        screenshot(driver, job, 'free_preview_guard_failed')
        raise RuntimeError('Generate does not explicitly say Free Preview; stopping')
    screenshot(driver, job, 'before_free_preview')
    job.intent('free_preview')
    free.click()
    job.save('submission_clicked', stage='free_preview_processing')
    if wait:
        wait_for(driver, job, {'geometry_preview'})


def confirm_geometry(driver, job, allow_paid, accept_preview, wait=True):
    if not accept_preview:
        screenshot(driver, job, 'review_required')
        raise RuntimeError('Preview ready: inspect review_required.png or the model URL, then resume with --accept-preview')
    if not allow_paid:
        raise RuntimeError('Geometry confirmation costs 2 credits; pass --allow-paid for this job')
    control = driver.find_element(By.CSS_SELECTOR, '[data-geometry-confirm-mode="primary"]')
    ActionChains(driver).move_to_element(control).move_by_offset(-30, 0).move_to_element(control).perform()
    time.sleep(1)
    smart = exact(driver, 'Smart Low-poly')
    if smart is None:
        raise RuntimeError('Mesh settings did not open')
    ActionChains(driver).move_to_element(smart).perform()
    smart.click()
    # Only the mesh popover should expose High at this stage.
    # The selected pill has a decorative overlay; pointer clicks reach its toggle.
    ActionChains(driver).move_to_element(exact(driver, 'Quad')).click().perform()
    ActionChains(driver).move_to_element(exact(driver, 'High')).click().perform()
    ActionChains(driver).move_to_element(control).perform()
    time.sleep(1)
    if '2 Credits' not in control.text:
        raise RuntimeError('Expected 2-credit confirmation; stopping on price/UI change')
    screenshot(driver, job, 'before_paid_confirmation')
    job.intent('geometry_confirm')
    control.click()
    job.save('submission_clicked', stage='geometry_processing')
    if wait:
        wait_for(driver, job, {'material_ready', 'material_preview', 'export_ready'})


def generate_material(driver, job, wait=True):
    material = scope(driver, MATERIAL)
    delight = exact(material, 'De-light')
    if delight is None:
        raise RuntimeError('De-light toggle missing')
    button = delight.find_element(By.XPATH, './ancestor::button[1]')
    if button.get_attribute('aria-pressed') != 'true':
        button.click()
    if button.get_attribute('aria-pressed') != 'true':
        raise RuntimeError('Could not enable De-light')
    # Let the De-light state update settle before submitting its React form.
    time.sleep(1)
    generate = exact(scope(driver, MATERIAL), 'Generate')
    job.intent('material_generate')
    generate.click()
    job.save('submission_clicked', stage='material_processing')
    if wait:
        wait_for(driver, job, {'material_preview', 'export_ready'})


def confirm_material(driver, job, wait=True):
    confirm = exact(scope(driver, MATERIAL), 'Confirm')
    job.intent('material_confirm')
    confirm.click()
    job.save('submission_clicked', stage='material_confirming')
    if wait:
        wait_for(driver, job, {'export_ready'})


def export_zip(driver, job, wait=True):
    pack = scope(driver, PACK)
    fbx = exact(pack, '.fbx')
    fbx.click()
    time.sleep(.5)
    if fbx.get_attribute('aria-checked') != 'true':
        raise RuntimeError('FBX did not select')
    # Match the successful trial: default base mesh, shaded/PBR and 2K textures.
    exact(pack, '2K').click()
    download_dir = Path.home() / 'Downloads'
    started = time.time()
    job.save('download_requested', stage='exporting', download_started=started)
    exact(pack, 'Download').click()
    if not wait:
        return
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        candidates = sorted(download_dir.glob(job.data['model_id']+'*.zip'), key=lambda p:p.stat().st_mtime, reverse=True)
        for candidate in candidates:
            if candidate.stat().st_mtime < started - 1:
                continue
            try:
                details = verify_zip(candidate)
            except (OSError, ValueError, zipfile.BadZipFile):
                continue
            target = Path(job.data['input']['path']).with_suffix('.zip')
            shutil.copy2(candidate, target)
            details = verify_zip(target)
            job.save('export_complete', stage='complete', output=details, pending_action=None)
            screenshot(driver, job, 'complete')
            print(str(target), flush=True)
            return
        time.sleep(3)
    raise TimeoutError('Download not found or incomplete; resume to download again without regenerating')


def advance(driver, job, allow_paid=False, accept_preview=False, inspect_only=False):
    if job.data['model_url']:
        navigate(driver, job.data['model_url'])
        time.sleep(4)
    elif inspect_only:
        print(json.dumps(job.data, indent=2))
        return
    else:
        submit_preview(driver, job)
    if inspect_only:
        state = observe(driver)
        job.save('resume_inspected', last_observation=state)
        screenshot(driver, job, 'resume_inspected')
        print(json.dumps(state, indent=2))
        return
    stage = wait_for(driver, job, {'geometry_preview','material_ready','material_preview','export_ready'})
    # intent() prevents replay even if server appears to return to an earlier stage.
    if stage == 'geometry_preview':
        confirm_geometry(driver, job, allow_paid, accept_preview)
        stage = observe(driver)['stage']
    if stage == 'material_ready':
        generate_material(driver, job)
        stage = observe(driver)['stage']
    if stage == 'material_preview':
        confirm_material(driver, job)
        stage = 'export_ready'
    if stage == 'export_ready':
        export_zip(driver, job)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    new = sub.add_parser('run', help='Create/reuse job by input SHA256')
    new.add_argument('image')
    new.add_argument('--jobs-root', default=str(ROOT/'jobs'))
    resume = sub.add_parser('resume', help='Reopen saved model and continue from server state')
    resume.add_argument('job')
    bind = sub.add_parser('bind', help='Recover the URL of a submission interrupted before ID capture')
    bind.add_argument('job')
    bind.add_argument('url')
    review = sub.add_parser('review', help='Record a visual review for an active batch job')
    review.add_argument('job')
    review.add_argument('--accept', action='store_true', required=True)
    review.add_argument('--note', required=True)
    for command in (new, resume):
        command.add_argument('--inspect', action='store_true', help='Read-only; no submissions or download')
        command.add_argument('--allow-paid', action='store_true', help='Permit the 2-credit geometry confirmation')
        command.add_argument('--accept-preview', action='store_true', help='Record that the geometry preview has been reviewed')
    args = parser.parse_args()
    job = Job.create(args.jobs_root, args.image) if args.command == 'run' else Job(args.job)
    print('Job:', job.path, flush=True)
    if args.command == 'review':
        # The scheduler reloads the atomic checkpoint every tick.
        if job.data['stage'] != 'geometry_preview':
            raise ValueError('Only a waiting geometry preview can be reviewed')
        atomic_json(job.path.with_name('review.json'), {'model_id':job.data['model_id'], 'accepted':True, 'note':args.note, 'at':now()})
        return
    with job.path.with_name('job.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'bind':
            job.bind(args.url)
            return
        if job.data['stage'] == 'complete' and not args.inspect:
            verified = verify_zip(job.data['output']['path'])
            if verified['sha256'] != job.data['output']['sha256']:
                raise ValueError('Saved export changed; inspect before continuing')
            print('Already complete:', job.data['output']['path'])
            return
        check_profile(PROFILE)
        options = Options()
        options.page_load_strategy = "eager"
        options.binary_location = BINARY
        options.add_argument('-profile')
        options.add_argument(str(PROFILE))
        options.set_preference('browser.download.folderList', 2)
        options.set_preference('browser.download.dir', str(Path.home()/'Downloads'))
        options.set_preference('browser.download.useDownloadDir', True)
        options.set_preference('browser.helperApps.neverAsk.saveToDisk', 'application/zip,application/octet-stream,application/x-zip-compressed')
        driver = webdriver.Firefox(service=Service(GECKODRIVER), options=options)
        driver.set_window_size(1500, 1000)
        driver.set_page_load_timeout(45)
        try:
            advance(driver, job, args.allow_paid, args.accept_preview, args.inspect)
        except (Exception, KeyboardInterrupt) as exc:
            job.save('interrupted', last_error=type(exc).__name__ + ': ' + str(exc).splitlines()[0])
            screenshot(driver, job, 'interrupted')
            raise
        finally:
            driver.quit()


if __name__ == '__main__':
    main()
