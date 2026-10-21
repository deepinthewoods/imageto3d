"""Measure body landmarks of a marker base mesh, in the normalized frame reduce_model.py uses.

Usage: blender -b --python tools/tripo_pipeline/base_regions.py -- <base_scene.blend> <out regions.json>

Normalized frame: bbox centre in X/Y, feet at Z=0, height 2 (same as reduce_model.run). The base
scene bbox is stored too, so base camera matrices (transforms.json) can be mapped into that frame.
Landmarks assume the active T-pose marker base: magenta = anatomical-left hand, cyan = anatomical-left
foot, yellow = head; anatomical left is +X. The right side mirrors the left.
"""
from pathlib import Path
import json
import sys

import bpy
import numpy as np


def measure(blend):
    bpy.ops.wm.open_mainfile(filepath=str(blend))
    meshes = [o for o in bpy.data.objects if o.type == 'MESH']
    if len(meshes) != 1:
        raise ValueError(f'Expected one base mesh; found {len(meshes)}')
    obj = meshes[0]
    mw = np.array(obj.matrix_world)
    v = np.array([tuple(x.co) for x in obj.data.vertices])
    v = v @ mw[:3, :3].T + mw[:3, 3]
    low, high = v.min(0), v.max(0)
    origin = (low + high) / 2
    origin[2] = low[2]
    scale = 2 / (high[2] - low[2])
    n = (v - origin) * scale
    names = [m.name for m in obj.data.materials]

    def marker(word):
        k = next(i for i, name in enumerate(names) if word in name)
        ids = sorted({i for p in obj.data.polygons if p.material_index == k for i in p.vertices})
        return n[ids]

    hand, foot, head = marker('hand'), marker('foot'), marker('head')
    arm_z = (float(hand[:, 2].min()), float(hand[:, 2].max()))
    wrist_x = float(hand[:, 0].min())
    # torso half-width just below the armpit (arms are horizontal in the T-pose)
    band = (n[:, 2] > arm_z[0] - .25) & (n[:, 2] < arm_z[0] - .1)
    shoulder_x = float(np.abs(n[band, 0]).max())
    ankle_z = float(foot[:, 2].max())
    # crotch: lowest face centroid on the midline above the feet
    cent = np.array([n[list(p.vertices)].mean(0) for p in obj.data.polygons])
    mid = (np.abs(cent[:, 0]) < .03) & (cent[:, 2] > ankle_z + .1)
    crotch_z = float(cent[mid, 2].min())
    return {
        'source_scene': str(Path(blend).resolve()),
        'base_scene_bbox': {'min': low.tolist(), 'max': high.tolist()},
        'frame': 'normalized: bbox centre X/Y, feet Z=0, height 2; anatomical left = +X, front faces -Y',
        'landmarks': {
            'arm_band_z': arm_z,
            'wrist_x': wrist_x,
            'elbow_x': (shoulder_x + wrist_x) / 2,
            'shoulder_x': shoulder_x,
            'neck_z': float(head[:, 2].min()),
            'crotch_z': crotch_z,
            'knee_z': (ankle_z + crotch_z) / 2,
            'ankle_z': ankle_z,
        },
    }


if __name__ == '__main__':
    args = sys.argv[sys.argv.index('--') + 1:]
    if len(args) != 2:
        raise SystemExit(__doc__)
    record = measure(args[0])
    Path(args[1]).write_text(json.dumps(record, indent=2) + '\n')
    print('REGIONS', json.dumps(record['landmarks']), flush=True)
