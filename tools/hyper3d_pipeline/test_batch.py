import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import batch


class Driver:
    def __init__(self):
        self.window_handles=['first']
        self.current_window_handle='first'
        self.switch_to=self
    def new_window(self, kind):
        name=str(len(self.window_handles))
        self.window_handles.append(name)
        self.current_window_handle=name
    def window(self, handle): self.current_window_handle=handle
    def get(self, url): pass
    def close(self): self.window_handles.remove(self.current_window_handle)


class SchedulerTests(unittest.TestCase):
    def test_duplicate_basenames_require_full_path_for_review(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            images=[]
            for i,category in enumerate(('elf','dwarf')):
                p=root/category/'same.png';p.parent.mkdir();p.write_bytes(bytes([i]));images.append(p)
            with patch('workflow.ROOT',root):
                run=batch.Batch(Driver(),images)
                run.active={str(i):j for i,j in enumerate(run.queue)}
                with self.assertRaises(ValueError):run.approve('same','ambiguous')
                run.approve(str(images[1]),'reviewed dwarf')
                self.assertNotIn('preview_review',run.active['0'].data)
                self.assertTrue(run.active['1'].data['preview_review']['accepted'])

    def test_ten_slot_limit_and_paid_step_not_repeated(self):
        with tempfile.TemporaryDirectory() as td, contextlib.redirect_stdout(io.StringIO()):
            root=Path(td)
            images=[]
            for i in range(12):
                p=root/f'sheet_{i:02}.png';p.write_bytes(bytes([i]));images.append(p)
            with patch('workflow.ROOT',root), patch('workflow.submit_preview'):
                run=batch.Batch(Driver(),images)
                try:
                    run.fill()
                    self.assertEqual(len(run.active),10)
                    self.assertEqual(len(run.driver.window_handles),10)
                    self.assertEqual(len(run.queue),2)
                    job=next(iter(run.active.values()))
                    job.save('review',stage='geometry_preview',preview_review={'accepted':True})
                    def confirm(driver, target, *args, **kwargs): target.intent('geometry_confirm')
                    with patch('workflow.observe',return_value={'stage':'geometry_preview','model_id':None,'progress':[]}),patch('workflow.screenshot'),patch('workflow.confirm_geometry',side_effect=confirm) as submit:
                        run.tick();run.tick()
                        self.assertEqual(submit.call_count,1)
                finally:
                    for lock in run.locks.values():lock.close()

if __name__=='__main__':unittest.main()
