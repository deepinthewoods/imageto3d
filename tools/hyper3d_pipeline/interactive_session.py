"""Visible one-tab session for supervised Rodin workflow exploration.

Run with python -i. Leaves Firefox open for interactive inspection.
No uploads or generation are triggered automatically.
"""
import json
from pathlib import Path
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from native_browser import PROFILE, BINARY, GECKODRIVER, check_profile, WORKSPACE

check_profile(PROFILE)
options=Options()
options.binary_location=BINARY
options.add_argument("-profile")
options.add_argument(str(PROFILE))
driver=webdriver.Firefox(service=Service(GECKODRIVER),options=options)
driver.set_window_size(1500,1000)
driver.set_page_load_timeout(45)
driver.get(WORKSPACE)
time.sleep(4)
evidence=Path(__file__).parent/"runs/free_preview_trial"
evidence.mkdir(parents=True,exist_ok=True)
def shot(name):
    path=evidence/(name+".png")
    driver.save_screenshot(str(path))
    print(str(path.resolve()))
def body():
    print(driver.find_element(By.TAG_NAME,"body").text[:16000])
def buttons():
    print(json.dumps([{"text":e.text,"aria":e.get_attribute("aria-label"),"title":e.get_attribute("title")} for e in driver.find_elements(By.CSS_SELECTOR,"button") if e.is_displayed()],indent=2))
print("VISIBLE SESSION READY; one tab; no generation submitted.",flush=True)
shot("01_workspace")
body()
