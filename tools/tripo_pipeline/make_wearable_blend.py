"""Build `wearable.blend` from the `lod_error_*__wearable.glb` files of a reduce_model.py run.

Usage: blender -b --python tools/tripo_pipeline/make_wearable_blend.py -- <reduced_dir>

One collection per reduction level (0p4, 0p2, 0p1), laid out side by side along X, with the
flat base / garment-role (primary, secondary, ...) / uncertain face materials from the GLBs. reduce_model.py calls
build() itself at the end of a run; use this script to backfill older outputs. When the level's
`lod_error_<level>_colors.npz` has body parts, each wearable mesh gets the FACE attribute `body_part`
and the property `body_part_names` (the GLB carries neither; the npz holds them in GLB face order). Wearables with mirrored copies (face_labels.mirror_wear) also get the FACE attributes `mirrored` (1 = copy) and `label_source` ('mirrored' = index 6 of `label_source_names`).
"""
from pathlib import Path
import json
import sys

import bpy
import numpy as np
from mathutils import Vector

GAP = 1.6  # spacing between levels, in multiples of the widest level


def build(out):
    out = Path(out)
    files = sorted(out.glob('lod_error_*__wearable.glb'), reverse=True)  # 0p4, 0p2, 0p1
    if not files:
        print('WEARABLE_BLEND no wearable GLBs in', out)
        return None
    bpy.ops.wm.read_factory_settings(use_empty=True)
    offset = 0.0
    for path in files:
        before = set(bpy.data.objects)
        bpy.ops.import_scene.gltf(filepath=str(path))
        new = [o for o in bpy.data.objects if o not in before]
        level = path.stem.replace('lod_error_', '').replace('__wearable', '')
        coll = bpy.data.collections.new('wearable_' + level)
        bpy.context.scene.collection.children.link(coll)
        meshes = [o for o in new if o.type == 'MESH']
        for o in new:
            for c in list(o.users_collection):
                c.objects.unlink(o)
            coll.objects.link(o)
        roots = [o for o in new if o.parent is None]
        spans = []
        for o in meshes:
            xs = [(o.matrix_world @ Vector(c)).x for c in o.bound_box]
            spans.append(max(xs) - min(xs))
        width = max(spans, default=1.0)
        for o in roots:
            o.location.x += offset
        offset += width * GAP
        for o in meshes:
            o.name = f'{level}__wearable' if len(meshes) == 1 else f'{level}__{o.name}'
        evidence = out / f'lod_error_{level}_colors.npz'
        if len(meshes) == 1 and evidence.is_file():
            z = np.load(evidence)
            mesh = meshes[0].data
            if 'wearable_body_part_glb_order' in z and len(z['wearable_body_part_glb_order']) == len(mesh.polygons):
                mesh.attributes.new('body_part', 'INT', 'FACE').data.foreach_set(
                    'value', z['wearable_body_part_glb_order'].astype(np.int32))
                mesh['body_part_names'] = json.dumps([str(n) for n in z['body_part_names']])
            if 'wearable_mirrored_glb_order' in z and len(z['wearable_mirrored_glb_order']) == len(mesh.polygons):
                # copies mirrored onto the bare anchor side (face_labels.mirror_wear): marked per face
                mesh.attributes.new('mirrored', 'INT', 'FACE').data.foreach_set(
                    'value', z['wearable_mirrored_glb_order'].astype(np.int32))
                mesh.attributes.new('label_source', 'INT', 'FACE').data.foreach_set(
                    'value', z['wearable_label_source_glb_order'].astype(np.int32))
                mesh['label_source_names'] = json.dumps([str(n) for n in z['label_source_names']])
    for area in bpy.context.screen.areas if bpy.context.screen else []:
        if area.type == 'VIEW_3D':
            area.spaces.active.shading.type = 'SOLID'
            area.spaces.active.shading.color_type = 'MATERIAL'
    dest = out / 'wearable.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(dest), compress=True)
    print('WEARABLE_BLEND', dest, flush=True)
    return dest


if __name__ == '__main__':
    args = sys.argv[sys.argv.index('--') + 1:]
    if len(args) != 1:
        raise SystemExit('Usage: blender -b --python make_wearable_blend.py -- <reduced_dir>')
    build(args[0])
