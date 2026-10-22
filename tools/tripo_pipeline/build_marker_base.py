"""Build a studio-plain semantic-marker scene for one race from its Armourforge V2 project.

Usage: blender -b --python tools/tripo_pipeline/build_marker_base.py -- <race> <out.blend>

Opens the frozen human-male scene (assets/studio_plain_input.blend: lights, world, camera,
render settings, the six marker/armor materials) and replaces its single `studio_plain` mesh
with projects/<race>.blend `base` in REST pose. Same normalisation as the archived kraft views:
bbox centred on the origin, largest dimension 0.8, no yaw (figure faces -Y, anatomical left +X);
then triangulated and flat shaded like the human mesh. Neither source .blend is modified.

Markers come from the project's per-face body-part materials: bone_head.x -> head_yellow,
bone_hand.l -> left_hand_magenta, bone_foot.l -> left_foot_cyan, everything else body_red. Each
triangle also keeps its part in the face attribute `body_part` (names in the mesh property
`body_part_names`), for pose-independent region priors. The frozen human-male scene was marked
differently (4 neck-edge triangles of bone_head.x red, 4 wrist triangles of bone_forearm_stretch.l
magenta); this script does not reproduce those 8 triangles, so human_male keeps its frozen scene.
Bone-weight rules were tried first and failed on the other races (heads mostly red).
"""
from pathlib import Path
import collections, json, re, sys

import bpy
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / 'assets/studio_plain_input.blend'
FIT = 0.8
MARKER = {'head.x': 1, 'hand.l': 2, 'foot.l': 3}
MATS = ('body_red', 'head_yellow', 'left_hand_magenta', 'left_foot_cyan', 'armor_green', 'armor_blue')


def main(race, out):
    out = Path(out)
    if out.exists():
        raise SystemExit(f'Output exists; preserve it: {out}')
    project = ROOT / 'projects' / f'{race}.blend'
    bpy.ops.wm.open_mainfile(filepath=str(TEMPLATE))
    scene = bpy.context.scene
    old = bpy.data.objects['studio_plain']
    mats = [bpy.data.materials[n] for n in MATS]
    assert [m.name for m in old.data.materials] == list(MATS)
    old_mesh = old.data
    bpy.data.objects.remove(old)
    bpy.data.meshes.remove(old_mesh)

    with bpy.data.libraries.load(str(project)) as (src, dst):
        dst.objects = ['base']
    base = dst.objects[0]
    rig = base.parent
    scene.collection.objects.link(base)
    scene.collection.objects.link(rig)
    rig.data.pose_position = 'REST'
    dg = bpy.context.evaluated_depsgraph_get()
    ev = base.evaluated_get(dg)
    me = bpy.data.meshes.new_from_object(ev)
    if len(me.vertices) != len(base.data.vertices):
        raise SystemExit('Evaluated mesh changed vertex count')
    me.transform(base.matrix_world)
    v = np.array([x.co[:] for x in me.vertices])
    low, high = v.min(0), v.max(0)
    centre = (low + high) / 2
    scale = FIT / float((high - low).max())
    n = (v - centre) * scale
    for x, co in zip(me.vertices, n):
        x.co = co

    # body-part materials of the project mesh (bone_head.x, bone_hand.l, ...) are pose independent
    # (a Blender duplicate suffix such as .016 is dropped)
    parts = [re.sub(r'\.\d{3}$', '', m.name).removeprefix('bone_') if m else '' for m in me.materials]
    # loop triangles: the glTF-style split the human scene has, face for face
    me.calc_loop_triangles()
    tris = [tuple(t.vertices) for t in me.loop_triangles]
    part = [me.polygons[t.polygon_index].material_index for t in me.loop_triangles]
    plain = bpy.data.meshes.new('studio_plain')
    plain.from_pydata(n.tolist(), [], tris)
    for m in mats:
        plain.materials.append(m)
    attr = plain.attributes.new('body_part', 'INT', 'FACE')
    for p, k in zip(plain.polygons, part):
        p.material_index = MARKER.get(parts[k], 0)
        p.use_smooth = False
        attr.data[p.index].value = k
    plain['body_part_names'] = json.dumps(parts)
    bpy.data.meshes.remove(me)
    me = plain
    obj = bpy.data.objects.new('studio_plain', me)
    scene.collection.objects.link(obj)

    bpy.data.objects.remove(base)
    arm_data = rig.data
    bpy.data.objects.remove(rig)
    bpy.data.armatures.remove(arm_data)
    bpy.data.orphans_purge(do_recursive=True)
    bpy.data.libraries.remove(bpy.data.libraries[0]) if bpy.data.libraries else None
    assert len([o for o in scene.objects if o.type == 'MESH']) == 1
    counts = collections.Counter(p.material_index for p in me.polygons)
    record = {'race': race, 'project': str(project), 'template': str(TEMPLATE), 'fit': FIT,
              'scale': scale, 'centre': centre.tolist(),
              'vertices': len(me.vertices), 'faces': len(me.polygons),
              'faces_per_material': {MATS[i]: counts[i] for i in range(len(MATS))},
              'normalised_bbox': {'min': n.min(0).tolist(), 'max': n.max(0).tolist()}}
    out.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(out), compress=False)
    print('MARKER_BASE', json.dumps(record), flush=True)


if __name__ == '__main__':
    args = sys.argv[sys.argv.index('--') + 1:]
    if len(args) != 2:
        raise SystemExit(__doc__)
    main(*args)
