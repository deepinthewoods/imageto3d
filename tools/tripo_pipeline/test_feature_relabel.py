"""Tests of feature_relabel.py's venv-side `plan` and of its Blender-side import contract (numpy only): venv python.

    characters/.venv/bin/python -m unittest tools/tripo_pipeline/test_feature_relabel.py
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import feature_relabel as fr  # noqa: E402
import wearcut as wc  # noqa: E402

META = {'normalization_scale': 1., 'normalization_origin_rotated': [0, 0, 0], 'rotation_to_review': np.eye(3).tolist(),
        'palette': {'garments': [{'role': 'primary'}, {'role': 'secondary'}, {'role': 'tertiary'}, {'role': 'accent'}]}}


class Plan(unittest.TestCase):
    def test_plan_writes_the_role_transfer_per_level(self):
        # high-poly: 4 faces over two LOD triangles; the first two turn tertiary
        hv = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [2, 0, 0], [3, 0, 0], [2, 1, 0]], float)
        hf = np.array([[0, 1, 2], [0, 1, 2], [3, 4, 5], [3, 4, 5]])
        # LOD in the glTF frame: from_glb maps (x, y, z)_glb -> (x, -z, y); a z=0 triangle in the normalised frame is y_glb = 0... use identity-ish
        g = np.stack([hv[:, 0], hv[:, 2], -hv[:, 1]], 1)
        lf = np.array([[0, 1, 2], [3, 4, 5]])
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / 'reduced').mkdir()
            (tmp / 'wearcut' / 'prep').mkdir(parents=True)
            (tmp / 'reduced' / 'manifest.json').write_text(json.dumps(META))
            names = ['base', 'primary', 'secondary', 'tertiary', 'accent']
            rgb = [(.9, .1, .1)] * 5
            wc.write_glb(tmp / 'reduced' / 'lod_error_0p2.glb', g, lf, np.array([1, 1]), names, rgb)
            np.savez_compressed(tmp / 'wearcut' / 'prep' / 'prep.npz', v=hv, f=hf)
            np.savez_compressed(tmp / 'wearcut' / 'feature_labels.npz', label_old=np.array([1, 1, 1, 1]), label_new=np.array([3, 3, 1, 1]))
            out = fr.plan(tmp / 'reduced', tmp / 'wearcut', 'studs', ('0p2', '0p1'), log=lambda *a: None)
            self.assertEqual(out['0p2']['faces_changed'], 1)
            self.assertIn('error', out['0p1'])                                   # no such level
            with np.load(tmp / 'wearcut' / 'feature_lod_0p2.npz') as z:
                self.assertEqual(z['role_old'].tolist(), [1, 1])
                self.assertEqual(z['role_new'].tolist(), [3, 1])
            self.assertEqual(json.loads((tmp / 'wearcut' / 'feature_lod.json').read_text())['0p2']['spec'], 'studs')

    def test_blender_side_imports_without_scipy(self):
        code = ('import sys; sys.path.insert(0, %r); import feature_relabel, feature_hooks, wearcut_rules; '
                'assert "scipy" not in sys.modules, "scipy imported"; print("ok")' % str(HERE))
        run = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(run.stdout.strip(), 'ok', run.stderr[-400:])


if __name__ == '__main__':
    unittest.main()
