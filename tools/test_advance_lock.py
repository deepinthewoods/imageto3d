"""advance.json lock: the supervisor's writers and the v2 drift Accept use the same byte lock. Temp roots only."""
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(r'F:/godot/blender/characters/imageto3d')
sys.path.insert(0, str(REPO / 'tools'))
sys.path.insert(0, str(REPO / 'tools' / 'tripo_pipeline'))
import pipeline_supervisor as ps  # noqa: E402

HOLDER = r'''
import sys, time
sys.path.insert(0, sys.argv[1])
from claim_lock import try_lock
h = open(sys.argv[2], 'a+b')
assert try_lock(h)
print('held', flush=True)
time.sleep(float(sys.argv[3]))
'''


class AdvanceLockTest(unittest.TestCase):
    def test_accept_drift_waits_for_the_lock_and_keeps_other_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            adv = batch / 'advance.json'
            adv.write_text(json.dumps({'accepted_drift': [{'sha256': 'x' * 64, 'image': 'other.png'}]}))
            img = batch / 'a.png'
            img.write_bytes(b'png-bytes')
            holder = subprocess.Popen([sys.executable, '-c', HOLDER, str(REPO / 'tools' / 'tripo_pipeline'),
                                       str(batch / 'advance.lock'), '1.5'], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), 'held')
                t0 = time.monotonic()
                self.assertEqual(ps.accept_drift(adv, [img], 'test'), 0)
                self.assertGreater(time.monotonic() - t0, 0.8)   # it waited for the other holder
            finally:
                holder.wait(10)
            data = json.loads(adv.read_text())
            self.assertEqual(len(data['accepted_drift']), 2)

    def test_lock_times_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            holder = subprocess.Popen([sys.executable, '-c', HOLDER, str(REPO / 'tools' / 'tripo_pipeline'),
                                       str(batch / 'advance.lock'), '3'], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), 'held')
                with self.assertRaises(TimeoutError):
                    with ps.advance_lock(batch / 'advance.json', wait=0.3):
                        pass
            finally:
                holder.wait(10)


if __name__ == '__main__':
    unittest.main()
