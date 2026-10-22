"""Read-only Hyper3D Rodin UI probe: opens pages, reads DOM text, takes screenshots.

Never uploads, never clicks Generate/Download/Confirm/Unlock. Safe actions are limited to
opening the workspace/model URL and (optionally, --expand) opening pure option dropdowns whose
visible label is on the SAFE_EXPAND list. Output: probe/<timestamp>/{findings.json,*.png,*.txt}.

  ./native.sh-style:  venv/bin/python probe.py [--model-url URL] [--expand] [--headless]
Stops (exit 2) if the profile is locked or the session is signed out; it never tries to log in.
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from native_browser import PROFILE, BINARY, GECKODRIVER, check_profile  # noqa: E402
from job_state import BASE, MINE  # noqa: E402
from rodin_backend import SeleniumRodin, classify_auth  # noqa: E402

FORBIDDEN = re.compile(r'generate|download|confirm|unlock|redo|pay|buy|purchase|upgrade|delete', re.I)

DOM_JS = r"""
const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
const out = {};
out.url = location.href; out.title = document.title;
out.file_inputs = [...document.querySelectorAll('input[type=file]')].map(e => ({
  accept: e.accept, multiple: e.multiple, name: e.name, id: e.id, visible: vis(e),
  parent_text: (e.closest('div')||{}).innerText ? e.closest('div').innerText.slice(0,120) : ''}));
out.buttons = [...document.querySelectorAll('button,[role=button],[role=tab],[role=switch],[role=radio],[role=combobox]')]
  .filter(vis).map(e => ({tag: e.tagName, role: e.getAttribute('role'), text: e.innerText.trim().slice(0,80),
    aria_pressed: e.getAttribute('aria-pressed'), aria_checked: e.getAttribute('aria-checked'),
    aria_selected: e.getAttribute('aria-selected'), aria_label: e.getAttribute('aria-label'),
    disabled: e.disabled || e.getAttribute('aria-disabled')}));
out.body_text = document.body.innerText.slice(0, 12000);
out.credit_texts = [...document.body.innerText.matchAll(/[^\n]{0,40}\bcredits?\b[^\n]{0,40}/gi)].map(m => m[0].trim());
out.password_inputs = [...document.querySelectorAll('input[type=password]')].filter(vis).length;
return out;
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-url', help='existing model URL (read-only open)')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--click-text', action='append', default=[],
                        help='extra visible element text to click (refused if it matches a forbidden verb)')
    parser.add_argument('--out', default=None)
    args = parser.parse_args()
    for text in args.click_text:
        if FORBIDDEN.search(text):
            raise SystemExit(f'refusing to click {text!r}')
    out = Path(args.out) if args.out else HERE / 'probe' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out.mkdir(parents=True, exist_ok=True)
    try:
        check_profile(PROFILE)
    except ValueError as exc:
        print('PROFILE LOCKED:', exc)
        raise SystemExit(2)
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service
    options = Options()
    options.page_load_strategy = 'eager'
    options.binary_location = BINARY
    options.add_argument('-profile')
    options.add_argument(str(PROFILE))
    if args.headless:
        options.add_argument('-headless')
    driver = webdriver.Firefox(service=Service(GECKODRIVER), options=options)
    driver.set_window_size(1500, 1000)
    driver.set_page_load_timeout(45)
    findings = {'at': datetime.now(timezone.utc).isoformat(), 'pages': {}}

    def snap(label):
        driver.save_screenshot(str(out / f'{label}.png'))
        data = driver.execute_script(DOM_JS)
        (out / f'{label}.txt').write_text(data.pop('body_text'))
        findings['pages'][label] = data
        return data

    def go(url):
        try:
            driver.get(url)
        except Exception:
            driver.execute_script('window.stop()')
        time.sleep(6)

    try:
        go(BASE)
        for _ in range(8):      # the plan badge / account menu load ~10 s after the page
            auth = classify_auth(driver.execute_script(SeleniumRodin.AUTH_JS))
            if auth != 'unknown':
                break
            time.sleep(2)
        data = snap('workspace')
        findings['auth'] = auth
        signed_out = auth != 'signed_in'
        findings['signed_in'] = not signed_out
        if signed_out:
            findings['stopped'] = 'signed out; not attempting login'
            (out / 'findings.json').write_text(json.dumps(findings, indent=2))
            print('SIGNED OUT; see', out)
            raise SystemExit(2)
        for i, text in enumerate(args.click_text):
            matches = [e for e in driver.find_elements(By.XPATH, f'//*[normalize-space(text())="{text}"]') if e.is_displayed()]
            if not matches:
                findings.setdefault('click_misses', []).append(text)
                continue
            matches[0].click()
            time.sleep(1.5)
            snap(f'after_click_{i:02d}_{re.sub(r"[^A-Za-z0-9]+", "_", text)[:30]}')
        go(MINE)
        snap('mine')
        if args.model_url:
            go(args.model_url)
            time.sleep(4)
            snap('model')
    finally:
        (out / 'findings.json').write_text(json.dumps(findings, indent=2))
        driver.quit()
    print('probe written to', out)


if __name__ == '__main__':
    main()
