"""gpu_cache_repro.py: does building a band body (copy.copy of the output body + new triangles) release the output body's GPU buffer?"""
import os
import numpy as np
import sys

os.environ['BODY_FIT_GPU'] = '1'
sys.path.insert(0, 'F:/godot/blender/characters/imageto3d/tools/layers')
import geodiv as gd  # noqa: E402
import layers as L  # noqa: E402

body = L.get_body('human_male', gd)
ob = L.out_body(body, gd)
S = L.slots(body, gd)
rings0 = L.rings_of(ob, S['torso'], gd)
ob.closest(np.repeat(ob.V, 4, 0))
print('output body has a GPU buffer:', hasattr(ob, '_gpu_tris'))
fb = L._band_body(ob, S['torso'], rings0, {0: 0.4})
print('band body shares it:', getattr(fb, '_gpu_tris', None) is getattr(ob, '_gpu_tris', None))
fb.closest(np.repeat(fb.V, 4, 0))
ob.closest(np.repeat(ob.V, 4, 0))
print('GPU still on:', gd.bf._GPU_ON)
