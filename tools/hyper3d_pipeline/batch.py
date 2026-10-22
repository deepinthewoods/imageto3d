"""Cooperative ten-tab scheduler; preview decisions supplied by supervising reviewer."""
import argparse
import fcntl
import json
from pathlib import Path
import shutil
import time
import zipfile

import workflow as w
from job_state import Job, verify_zip


class Batch:
    def __init__(self, driver, images, slots=10):
        self.driver = driver
        if not 1 <= slots <= 10:
            raise ValueError("slots must be between 1 and 10")
        self.slots = slots
        self.locks = {}
        self.queue = [Job.create(w.ROOT/'jobs', p) for p in images]
        self.active = {}
        self.finished = []
        self.failed = []

    def fill(self):
        if self.driver.window_handles:
            self.driver.switch_to.window(self.driver.window_handles[0])
        while self.queue and len(self.active) < self.slots:
            if self.driver.window_handles:
                self.driver.switch_to.window(self.driver.window_handles[0])
            job = self.queue.pop(0)
            if job.data.get('outcome') == 'failed_preview':
                self.failed.append(job)
                continue
            if job.data['stage'] == 'complete':
                verify_zip(job.data['output']['path'])
                self.finished.append(job)
                continue
            lock = job.path.with_name('job.lock').open('w')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.locks[str(job.path)] = lock
            if self.active or len(self.driver.window_handles) != 1:
                self.driver.switch_to.new_window('tab')
            handle = self.driver.current_window_handle
            self.active[handle] = job
            try:
                job.save('batch_slot_started',slot_started=time.time())
                if job.data['model_url']:
                    w.navigate(self.driver, job.data['model_url'])
                else:
                    w.submit_preview(self.driver, job, wait=False)
                print('Started',Path(job.data['input']['path']).name,flush=True)
            except Exception as exc:
                self.fail(handle, job, exc)

    def fail(self, handle, job, exc):
        job.save('batch_error', last_error=str(exc).splitlines()[0])
        self.failed.append(job)
        self.active.pop(handle, None)
        lock=self.locks.pop(str(job.path),None)
        if lock: lock.close()
        if len(self.driver.window_handles)>1:
            self.driver.close()
        print('Stopped',Path(job.data['input']['path']).name,str(exc).splitlines()[0],flush=True)

    def matching_job(self, sheet):
        matches = [(h, j) for h, j in self.active.items()
                   if str(Path(j.data['input']['path'])) == str(sheet)
                   or Path(j.data['input']['path']).stem == str(sheet)]
        if len(matches) != 1:
            raise ValueError('Use the full input path when filenames are ambiguous')
        return matches[0]

    def approve(self, sheet, note):
        _, job = self.matching_job(sheet)
        job.save('preview_reviewed', preview_review={'accepted': True, 'note': note})

    def redo(self, sheet, note):
        handle,job=self.matching_job(sheet)
        self.driver.switch_to.window(handle)
        if w.observe(self.driver)['stage']!='geometry_preview' or 'geometry_confirm' in job.data['actions']:
            raise RuntimeError('Free redo is only available before paid geometry confirmation')
        count=job.data.get('free_redos',0)
        if count>=job.data.get('free_redo_limit',3):
            self.fail(handle,job,RuntimeError('Three free redos exhausted: '+note))
            return
        job.observed('geometry_preview')
        job.intent('free_redo_'+str(count+1))
        job.save('preview_rejected',free_redos=count+1,preview_review={'accepted':False,'note':note},stage='geometry_processing')
        w.exact(w.scope(self.driver,w.GEOMETRY),'Redo').click()

    def downloads(self, job):
        for path in sorted((Path.home()/'Downloads').glob(job.data['model_id']+'*.zip'), key=lambda p:p.stat().st_mtime, reverse=True):
            if path.stat().st_mtime < job.data.get('download_started',0)-1:
                continue
            try:
                verify_zip(path)
                out=w.ROOT/'exports'/Path(job.data['input']['path']).stem/job.data['input']['sha256'][:12]
                out.mkdir(parents=True,exist_ok=True)
                dest=Path(job.data['input']['path']).with_suffix('.zip')
                shutil.copy2(path,dest)
                result=verify_zip(dest)
            except (OSError,ValueError,zipfile.BadZipFile):
                continue
            job.save('export_complete',stage='complete',output=result,pending_action=None)
            (out/'metadata.json').write_text(json.dumps(job.data,indent=2)+'\n')
            return True
        return False

    def tick(self):
        report=[]
        for handle,job in list(self.active.items()):
            try:
                self.driver.switch_to.window(handle)
                job.data = json.loads(job.path.read_text())
                review_file=job.path.with_name('review.json')
                if review_file.exists():
                    decision=json.loads(review_file.read_text())
                    if decision.get('model_id')==job.data['model_id'] and decision.get('at')!=job.data.get('review_applied_at'):
                        job.save('preview_reviewed',preview_review=decision,review_applied_at=decision.get('at'))
                if time.time()-job.data.get('slot_started',time.time()) > 3600:
                    raise TimeoutError('Job has not finished within one hour; saved for resume')
                state=w.observe(self.driver)
                stage=state['stage']
                if state['model_id'] and not job.data['model_id']:
                    job.bind(self.driver.current_url)
                if stage=='signed_out':
                    raise RuntimeError('Session signed out')
                if state != job.data.get('last_observation'):
                    job.save('poll',last_observation=state,last_state_change=time.time())
                elif stage in ('geometry_processing','material_processing','unknown') and job.data.get('model_url') and time.time()-max(job.data.get('last_state_change',job.data.get('slot_started',time.time())),job.data.get('last_reopen',0))>180:
                    job.save('reopen_stale_tab',last_reopen=time.time())
                    w.navigate(self.driver,job.data['model_url'])
                    continue
                if job.data['stage']=='exporting':
                    if self.downloads(job):
                        self.finished.append(job)
                        del self.active[handle]
                        lock=self.locks.pop(str(job.path),None)
                        if lock: lock.close()
                        if len(self.driver.window_handles)>1:
                            self.driver.close()
                        stage='complete'
                elif stage=='geometry_preview' and 'geometry_confirm' not in job.data['actions']:
                    if job.data['stage'] != 'geometry_preview':
                        job.observed(stage)
                        w.screenshot(self.driver,job,'review_required')
                    if job.data.get('preview_review',{}).get('accepted'):
                        w.confirm_geometry(self.driver,job,True,True,wait=False)
                    else:
                        stage='awaiting_visual_review'
                elif stage=='material_ready' and 'material_generate' not in job.data['actions']:
                    job.observed(stage)
                    w.generate_material(self.driver,job,wait=False)
                elif stage=='material_preview' and 'material_confirm' not in job.data['actions']:
                    job.observed(stage)
                    w.screenshot(self.driver,job,'material_preview')
                    w.confirm_material(self.driver,job,wait=False)
                elif stage=='export_ready':
                    job.observed(stage)
                    w.export_zip(self.driver,job,wait=False)
                report.append({'sheet':str(Path(job.data['input']['path']).relative_to(w.ROOT.parent)),'stage':stage,'progress':state['progress']})
            except Exception as exc:
                self.fail(handle,job,exc)
        print(json.dumps({'active':report,'complete':len(self.finished),'failed':len(self.failed),'queued':len(self.queue)},indent=2),flush=True)
        return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('images',nargs='+')
    parser.add_argument('--slots',type=int,default=10,choices=range(1,11))
    parser.add_argument('--allow-paid',action='store_true')
    args=parser.parse_args()
    if not args.allow_paid:
        parser.error('--allow-paid is required for the requested 2-credit-per-sheet workflow')
    w.check_profile(w.PROFILE)
    options=w.Options()
    options.page_load_strategy="eager"
    options.binary_location=w.BINARY
    options.add_argument('-profile')
    options.add_argument(str(w.PROFILE))
    options.set_preference('browser.download.folderList',2)
    options.set_preference('browser.download.dir',str(Path.home()/'Downloads'))
    options.set_preference('browser.download.useDownloadDir',True)
    options.set_preference('browser.helperApps.neverAsk.saveToDisk','application/zip,application/octet-stream,application/x-zip-compressed')
    driver=w.webdriver.Firefox(service=w.Service(w.GECKODRIVER),options=options)
    driver.set_window_size(1500,1000)
    driver.set_page_load_timeout(45)
    batch=Batch(driver,args.images,args.slots)
    try:
        while batch.queue or batch.active:
            batch.fill()
            batch.tick()
            time.sleep(10)
        print('Finished:',len(batch.finished),'Failed:',len(batch.failed),flush=True)
    finally:
        driver.quit()
        for lock in batch.locks.values(): lock.close()


if __name__=='__main__':
    main()
