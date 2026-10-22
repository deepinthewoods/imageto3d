"""Read-only interactive UI explorer: runs a JSON list of steps in ONE Selenium session (signed-in profile).

Steps: ["go", url] ["click_text", text] ["click_css", css] ["click_xy", x, y] ["hover_text", text]
       ["key", "Escape"] ["wait", s] ["snap", label] ["html", css, label] ["scroll", y]
Clicks whose target text matches a spending/destructive verb (generate, free preview, download, confirm, unlock,
redo, pay, delete, export, upgrade, ...) are refused. Output dir gets <label>.png/.txt/.html.
"""
import json, re, sys, time
from pathlib import Path
from probe import FORBIDDEN, DOM_JS
from native_browser import PROFILE, BINARY, GECKODRIVER, check_profile
BAD = re.compile(FORBIDDEN.pattern + r'|export|free preview|subscribe|trial|buy|share|submit|fork|bake', re.I)

def main():
    out = Path(sys.argv[1]); steps = json.loads(Path(sys.argv[2]).read_text()); out.mkdir(parents=True, exist_ok=True)
    check_profile(PROFILE)
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service
    o = Options(); o.page_load_strategy = 'eager'; o.binary_location = BINARY
    o.add_argument('-profile'); o.add_argument(str(PROFILE))
    d = webdriver.Firefox(service=Service(GECKODRIVER), options=o); d.set_window_size(1500, 1000); d.set_page_load_timeout(45)
    def snap(label):
        d.save_screenshot(str(out / f'{label}.png'))
        data = d.execute_script(DOM_JS)
        (out / f'{label}.txt').write_text(data.pop('body_text'))
        (out / f'{label}.json').write_text(json.dumps(data, indent=1))
    def guard(el):
        t = (el.text or '') + ' ' + (el.get_attribute('aria-label') or '') + ' ' + (el.get_attribute('title') or '')
        if BAD.search(t): raise SystemExit(f'REFUSED click on {t.strip()[:60]!r}')
    try:
        for s in steps:
            k = s[0]
            if k == 'go':
                try: d.get(s[1])
                except Exception: d.execute_script('window.stop()')
                time.sleep(6)
            elif k == 'click_text':
                els = [e for e in d.find_elements(By.XPATH, f'//*[normalize-space(text())="{s[1]}"]') if e.is_displayed()]
                guard(els[int(s[2]) if len(s) > 2 else 0]); els[int(s[2]) if len(s) > 2 else 0].click(); time.sleep(2)
            elif k == 'click_css':
                e = [e for e in d.find_elements(By.CSS_SELECTOR, s[1]) if e.is_displayed()][0]; guard(e); e.click(); time.sleep(2)
            elif k == 'click_xy':
                e = d.execute_script('return document.elementFromPoint(arguments[0],arguments[1])', s[1], s[2])
                guard(e); ActionChains(d).move_to_element(e).click().perform(); time.sleep(2)
            elif k == 'hover_text':
                e = [e for e in d.find_elements(By.XPATH, f'//*[normalize-space(text())="{s[1]}"]') if e.is_displayed()][0]
                ActionChains(d).move_to_element(e).perform(); time.sleep(1.5)
            elif k == 'hover_xy':
                e = d.execute_script('return document.elementFromPoint(arguments[0],arguments[1])', s[1], s[2])
                ActionChains(d).move_to_element(e).perform(); time.sleep(1.5)
            elif k == 'html_text':
                h = d.execute_script('''const x=document.evaluate('//*[normalize-space(text())="'+arguments[0]+'"]',document,null,9,null).singleNodeValue;
                  if(!x) return 'NOT FOUND'; let n=x; for(let i=0;i<arguments[1];i++) if(n.parentElement) n=n.parentElement; return n.outerHTML.slice(0,9000)''', s[1], s[2] if len(s) > 2 else 3)
                (out / f'{s[-1]}.html').write_text(h)
            elif k == 'key':
                ActionChains(d).send_keys(getattr(Keys, s[1].upper())).perform(); time.sleep(1)
            elif k == 'wait': time.sleep(s[1])
            elif k == 'scroll': d.execute_script('window.scrollTo(0,arguments[0])', s[1]); time.sleep(1.5)
            elif k == 'snap': snap(s[1])
            elif k == 'html':
                els = d.find_elements(By.CSS_SELECTOR, s[1])
                (out / f'{s[2]}.html').write_text('\n\n'.join(e.get_attribute('outerHTML')[:200000] for e in els[:200]))
            else: raise SystemExit(f'unknown step {k}')
    finally:
        d.quit()
main()
