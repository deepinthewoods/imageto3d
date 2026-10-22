"""Export the per-face body parts of a marker base mesh, in the normalized frame reduce_model.py uses.

Usage: blender -b --python tools/tripo_pipeline/base_parts.py -- <marker_scene.blend> <out parts.npz>

Reads the single mesh of a marker base scene (assets/marker_bases/<race>.blend, built by
build_marker_base.py): its FACE int attribute `body_part` and the mesh property `body_part_names`
(JSON list; the attribute value indexes it, e.g. head.x, hand.l, forearm_stretch.r, skirt).
Vertices are put into the same frame as base_regions.py: bbox centre in X/Y, feet at Z=0,
height 2; anatomical left = +X, front faces -Y. The npz holds `v` (float32), `f` (int32 triangles),
`part` (int16 per triangle), `part_names`, `frame`, `source_scene` and `source_sha256`;
face_labels.BaseParts reads it to transfer parts onto Tripo meshes.
"""
from pathlib import Path
import hashlib
import json
import sys

import bpy
import numpy as np


def extract(blend):
    bpy.ops.wm.open_mainfile(filepath=str(blend))
    meshes = [o for o in bpy.data.objects if o.type == 'MESH']
    if len(meshes) != 1:
        raise ValueError(f'Expected one base mesh; found {len(meshes)}')
    obj = meshes[0]
    mesh = obj.data
    if 'body_part' not in mesh.attributes or 'body_part_names' not in mesh:
        raise ValueError(f'{blend}: mesh has no body_part attribute / body_part_names property')
    names = json.loads(mesh['body_part_names'])
    mw = np.array(obj.matrix_world)
    v = np.array([tuple(x.co) for x in mesh.vertices]) @ mw[:3, :3].T + mw[:3, 3]
    low, high = v.min(0), v.max(0)
    origin = (low + high) / 2
    origin[2] = low[2]
    v = (v - origin) * (2 / (high[2] - low[2]))
    mesh.calc_loop_triangles()
    f = np.array([tuple(t.vertices) for t in mesh.loop_triangles], np.int32)
    polygon_part = np.zeros(len(mesh.polygons), np.int32)
    mesh.attributes['body_part'].data.foreach_get('value', polygon_part)
    part = polygon_part[[t.polygon_index for t in mesh.loop_triangles]].astype(np.int16)
    if part.max() >= len(names):
        raise ValueError('body_part value outside body_part_names')
    sha = hashlib.sha256(Path(blend).read_bytes()).hexdigest()
    return dict(v=v.astype(np.float32), f=f, part=part, part_names=np.array(names),
                frame='normalized: bbox centre X/Y, feet Z=0, height 2; anatomical left = +X, front faces -Y',
                source_scene=str(Path(blend).resolve()), source_sha256=sha)


if __name__ == '__main__':
    args = sys.argv[sys.argv.index('--') + 1:]
    if len(args) != 2:
        raise SystemExit(__doc__)
    out = Path(args[1])
    if out.exists():
        raise SystemExit(f'Output exists; preserve it: {out}')
    record = extract(args[0])
    np.savez_compressed(out, **record)
    share = np.bincount(record['part'], minlength=len(record['part_names']))
    print('BASE_PARTS', out, len(record['f']), 'faces',
          json.dumps({n: int(c) for n, c in zip(record['part_names'], share) if c}), flush=True)
