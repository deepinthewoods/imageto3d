"""Interactive supervised heads batch; no conversions before explicit runner.fill()."""
from pathlib import Path
import json,time
import workflow as w
import batch
from selenium.webdriver.common.by import By
w.check_profile(w.PROFILE)
options=w.Options();options.page_load_strategy='eager';options.binary_location=w.BINARY
options.add_argument('-profile');options.add_argument(str(w.PROFILE))
options.set_preference('browser.download.folderList',2)
options.set_preference('browser.download.dir',str(Path.home()/'Downloads'))
options.set_preference('browser.download.useDownloadDir',True)
options.set_preference('browser.helperApps.neverAsk.saveToDisk','application/zip,application/octet-stream,application/x-zip-compressed')
driver=w.webdriver.Firefox(service=w.Service(w.GECKODRIVER),options=options)
driver.set_window_size(1500,1000);driver.set_page_load_timeout(15);driver.set_script_timeout(20)
images=sorted((w.ROOT.parent/'heads').rglob('*.png'))
runner=batch.Batch(driver,images,slots=10)
w.navigate(driver,w.BASE)
print('Ready',len(images),'inputs',w.observe(driver),flush=True)
