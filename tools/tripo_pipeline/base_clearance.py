"""How far a bare Tripo base mesh strays outside its ICP-fitted marker base, per body part.

Usage: blender -b --python tools/tripo_pipeline/base_clearance.py -- <race> <bare source.glb> <out clearance.json>

Every face of a bare-base Tripo mesh is genuinely base, so the distribution of its signed distance
to the fitted marker base (face_labels.BaseParts, same fit_base as reduce_model.py) says how far
bare skin can sit outside the base through pose and reconstruction differences. Stores the p99.9
per part (parts with >= MIN_SAMPLES samples) and over all faces; face_labels.off_body_threshold
adds a margin. Written beside base_views/<race>/parts.npz and found by BaseParts.load.
"""
from pathlib import Path
import hashlib
import json
import sys

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import face_labels  # noqa: E402

SAMPLES, MIN_SAMPLES, QUANTILE = 120000, 200, .999
ROOT = Path(__file__).resolve().parents[2]


def measure(race, source):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(source), merge_vertices=False)
    obj = [o for o in bpy.context.scene.objects if o.type == 'MESH'][0]
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    mesh = obj.data
    mesh.calc_loop_triangles()
    v = np.empty(len(mesh.vertices) * 3)
    mesh.vertices.foreach_get('co', v)
    v = v.reshape(-1, 3)
    f = np.empty(len(mesh.loop_triangles) * 3, np.int64)
    mesh.loop_triangles.foreach_get('vertices', f)
    f = f.reshape(-1, 3)
    low, high = v.min(0), v.max(0)
    origin = (low + high) / 2
    origin[2] = low[2]
    v = (v - origin) * (2 / (high[2] - low[2]))
    base = face_labels.BaseParts.load(ROOT / 'base_views' / race / 'parts.npz')
    scale, shift, fit = face_labels.fit_base(base, v, f)
    fitted = base.moved(scale, shift) if fit['used'] == 'icp' else base
    ids = np.random.default_rng(5).choice(len(f), min(SAMPLES, len(f)), replace=False)
    d = fitted.signed_distance(v[f[ids]].mean(1))
    part = fitted.nearest_part(v[f[ids]].mean(1))
    return {'race': race, 'source': str(Path(source).resolve()),
            'source_sha256': hashlib.sha256(Path(source).read_bytes()).hexdigest(), 'fit': fit,
            'quantile': QUANTILE, 'samples': len(ids),
            'global_outside': float(np.quantile(d, QUANTILE)),
            'by_part_outside': {fitted.names[k]: float(np.quantile(d[part == k], QUANTILE))
                                for k in np.unique(part) if (part == k).sum() >= MIN_SAMPLES}}


if __name__ == '__main__':
    args = sys.argv[sys.argv.index('--') + 1:]
    if len(args) != 3:
        raise SystemExit(__doc__)
    if Path(args[2]).exists():
        raise SystemExit(f'Output exists; preserve it: {args[2]}')
    record = measure(*args[:2])
    Path(args[2]).write_text(json.dumps(record, indent=2) + '\n')
    print('BASE_CLEARANCE', args[0], round(record['global_outside'], 4), flush=True)
