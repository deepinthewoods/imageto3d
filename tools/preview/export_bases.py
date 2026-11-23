"""Export the game base bodies (projects/<race>.blend, mesh "base" in REST pose, world space) for the preview's
"on body" variant: tools/preview/cache/bases/<race>.glb plus <race>.json with the rest frame (o, H) that
tools/outfit_studio/core/transform.py uses to place a wearable on the base, and <race>.npz with the rest mesh as arrays
(V Blender world coordinates, F triangles, part per triangle into part_names; read by tools/tripo_pipeline/body_fit.py)
and <race>_rig.npz with the rest rig and the skin weights of mesh "base" (read by tools/tripo_pipeline/align_body.py): bone_names,
bone_parent (index, -1 root), bone_head / bone_tail (rest, Blender world), bone_matrix (n,4,4 rest bone matrices, world), bone_deform,
bone_group (index of the bone's vertex group, -1 none), group_names, weights (vertices, groups) float32 in the order of V, rig_world.
A race in EXTRA_OBJECTS gets those meshes joined to "base" exactly as its concept marker base was built
(tools/tripo_pipeline/build_marker_base.py --add), so the cached body has the concept base's rest frame (o, H): the wearables
are stored relative to that base and scaled onto the body by H.

    blender -b --factory-startup -P tools/preview/export_bases.py [-- [--force] <race> ...]

Read-only on the base projects (never saved). Rebuilds a race only when its .blend is newer than the cache (or with --force).
"""
import json
from pathlib import Path
import re
import sys

import bmesh
import bpy
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
PROJECTS = ROOT / 'projects'
CACHE = Path(__file__).resolve().parent / 'cache' / 'bases'
SKIN = (0.78, 0.62, 0.52, 1.0)
# object -> body part joined to `base` (rigidly, weight 1 on the part's vertex group). The gnome's `feet`, `head` and `Nose`
# have been separate objects since 2026-10-26 and stay out, as in its marker base (base_views/gnome_female/NOTES.md); the
# body cached before that (feet + tall head, H 1.9) made the gnome wearables 1.45x too big on the body.
EXTRA_OBJECTS = {'gnome_female': {'location head': 'head.x'}}


def part_names(base):
    return [re.sub(r'\.\d{3}$', '', mt.name.removeprefix('bone_')) if mt else '' for mt in base.data.materials]


def rest_mesh(rig, base, extras=()):
    """World-space rest mesh of `base` with the (object, part) `extras` appended, every extra face in that part."""
    if rig is not None:
        rig.data.pose_position = 'REST'
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(base.evaluated_get(dg))
    me.transform(base.matrix_world)
    if extras:
        names = part_names(base)
        bm = bmesh.new()
        bm.from_mesh(me)
        for obj, part in extras:
            extra = bpy.data.meshes.new_from_object(obj.evaluated_get(dg))
            extra.transform(obj.matrix_world)
            first = len(bm.faces)
            bm.from_mesh(extra)
            bm.faces.ensure_lookup_table()
            for i in range(first, len(bm.faces)):
                bm.faces[i].material_index = names.index(part)
            bpy.data.meshes.remove(extra)
        bm.to_mesh(me)
        bm.free()
    return me


def mesh_arrays(me, base):
    """(V (n,3) world, F (m,3) triangles, part per triangle, part_names) of the rest mesh `me` (already in world space);
    parts come from the `bone_<part>` material slots exactly like tools/outfit_studio/blender/importer.py::base_rest_mesh."""
    n = len(me.vertices)
    V = np.empty(n * 3)
    me.vertices.foreach_get('co', V)
    me.calc_loop_triangles()
    m = len(me.loop_triangles)
    F, poly = np.empty(m * 3, np.int64), np.empty(m, np.int64)
    me.loop_triangles.foreach_get('vertices', F)
    me.loop_triangles.foreach_get('polygon_index', poly)
    mat = np.empty(len(me.polygons), np.int64)
    me.polygons.foreach_get('material_index', mat)
    return V.reshape(-1, 3), F.reshape(-1, 3), mat[poly].astype(np.int32), part_names(base)


def rig_arrays(rig, base, extras=()):
    """Rest rig of the armature and the skin weights of mesh `base` (vertex groups, in vertex order; the vertices of the
    (object, part) `extras` follow, weight 1 on the part's group) as arrays, Blender world space."""
    mw = np.array(rig.matrix_world, np.float64)
    bones = list(rig.data.bones)
    index = {b.name: i for i, b in enumerate(bones)}
    groups = [g.name for g in base.vertex_groups]
    W = np.zeros((len(base.data.vertices), len(groups)), np.float32)
    for v in base.data.vertices:
        for g in v.groups:
            W[v.index, g.group] = g.weight
    for obj, part in extras:
        rows = np.zeros((len(obj.data.vertices), len(groups)), np.float32)
        rows[:, groups.index(part)] = 1.
        W = np.vstack([W, rows])
    pos = lambda m: np.array(m, np.float64)
    head = np.array([(mw @ np.append(pos(b.head_local), 1.))[:3] for b in bones])
    tail = np.array([(mw @ np.append(pos(b.tail_local), 1.))[:3] for b in bones])
    return dict(bone_names=np.array([b.name for b in bones]), bone_parent=np.array([index[b.parent.name] if b.parent else -1 for b in bones], np.int32),
                bone_head=head, bone_tail=tail, bone_matrix=np.array([mw @ pos(b.matrix_local) for b in bones]),
                bone_deform=np.array([b.use_deform for b in bones]), bone_group=np.array([groups.index(b.name) if b.name in groups else -1 for b in bones], np.int32),
                group_names=np.array(groups), weights=W, rig_world=mw)


def export(race, force=False):
    blend = PROJECTS / f'{race}.blend'
    glb, record, arrays, rig_file = CACHE / f'{race}.glb', CACHE / f'{race}.json', CACHE / f'{race}.npz', CACHE / f'{race}_rig.npz'
    stamp = blend.stat().st_mtime
    need_base = force or not (glb.is_file() and record.is_file() and arrays.is_file() and glb.stat().st_mtime >= stamp)
    need_rig = force or not (rig_file.is_file() and rig_file.stat().st_mtime >= stamp)
    if not need_base and not need_rig:
        print('BASE_CACHED', race)
        return
    bpy.ops.wm.open_mainfile(filepath=str(blend))
    pick = lambda name, kind: next((o for o in bpy.data.objects if o.type == kind and o.name.split('.')[0] == name), None)
    rig, base = pick('rig', 'ARMATURE'), pick('base', 'MESH')
    if base is None:
        raise RuntimeError(f'{blend.name}: no mesh "base"')
    extras = [(pick(name, 'MESH'), part) for name, part in EXTRA_OBJECTS.get(race, {}).items()]
    missing = [name for (obj, _), name in zip(extras, EXTRA_OBJECTS.get(race, {})) if obj is None]
    if missing:
        raise RuntimeError(f'{blend.name}: no mesh {missing}')
    if need_rig and rig is not None:                          # a rig-only refresh leaves the body cache (and its mtime, which body_fit.py watches) alone
        CACHE.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(rig_file, **rig_arrays(rig, base, extras))
        print('RIG_EXPORTED', race, len(base.data.vertices))
    if not need_base:
        return
    me = rest_mesh(rig, base, extras)
    xs = [v.co for v in me.vertices]
    lo = [min(c[i] for c in xs) for i in range(3)]
    hi = [max(c[i] for c in xs) for i in range(3)]
    V, F, part, part_names = mesh_arrays(me, base)
    CACHE.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(arrays, V=V, F=F, part=part, part_names=np.array(part_names))
    me.materials.clear()
    mat = bpy.data.materials.new('base_skin')
    mat.diffuse_color = SKIN
    mat.use_nodes = True
    bsdf = next(n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    bsdf.inputs['Base Color'].default_value = SKIN
    bsdf.inputs['Roughness'].default_value = 0.8
    me.materials.append(mat)
    scene = bpy.data.scenes.new('preview_base_export')
    obj = bpy.data.objects.new(f'{race}_base', me)
    scene.collection.objects.link(obj)
    bpy.context.window.scene = scene
    CACHE.mkdir(parents=True, exist_ok=True)
    bpy.ops.export_scene.gltf(filepath=str(glb), export_format='GLB', use_active_scene=True, export_yup=True,
                              export_apply=False, export_materials='EXPORT', export_animations=False,
                              export_skins=False, export_morph=False)
    record.write_text(json.dumps({'race': race, 'source': blend.relative_to(ROOT).as_posix(),
                                  'o': [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]], 'H': hi[2] - lo[2],
                                  'frame': 'Blender world (Z-up); the GLB is the same mesh in glTF Y-up'}, indent=1))
    print('BASE_EXPORTED', race, len(me.vertices))


def main():
    argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
    force = '--force' in argv
    races = [a for a in argv if a != '--force'] or sorted(json.loads((PROJECTS / 'manifest.json').read_text()))
    for race in races:
        export(race, force)


main()
