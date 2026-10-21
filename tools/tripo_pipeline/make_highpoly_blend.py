"""Build `highpoly.blend` beside a Tripo source GLB: the full-resolution mesh twice, side by side.

Usage: blender -b --python tools/tripo_pipeline/make_highpoly_blend.py -- <source.glb> <swatches.json>
           [<cameras transforms.json> <regions.json>]

- `highpoly_detected`: every triangle gets the material detected from the texture at its UV centre
  (base / primary / secondary by hue, or orange `uncertain` when the pixel is too grey/dark or between
  hues; face_labels.classify_samples).
- `highpoly_seen` (only with cameras): white where a concept view saw the surface, dark grey where
  none did. Texture there is Tripo's invention and the pipeline barely trusts it.
- `highpoly_textured`: the untouched import with Tripo's own texture (packed into the file).
Native GLB coordinates; the detected copy is shifted along +X. Never overwrites an existing file.
reduce_model.py calls build() at the end of each run that has swatches.
"""
from pathlib import Path
import json
import sys

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reduce_model as rm  # noqa: E402
import face_labels  # noqa: E402
from mathutils.bvhtree import BVHTree  # noqa: E402

ROLE_COLORS = {'base': (0.9, 0.1, 0.14), 'primary': (0.1, 0.82, 0.16),
               'secondary': (0.12, 0.25, 0.92), 'uncertain': (1.0, 0.4, 0.03)}


def uv_centre_colors(obj):
    """Texture colour at each loop-triangle's UV centroid (bilinear), plus the polygon each belongs to."""
    surface = rm.texture_tools.TextureSurface(obj)
    uv = surface.uv.mean(axis=1)
    h, w = surface.pixels.shape[:2]
    xy = np.clip(uv, 0, 1) * np.array([w, h]) - .5
    xy = np.maximum(xy, 0)
    low = np.floor(xy).astype(int)
    low[:, 0] = np.minimum(low[:, 0], w - 1)
    low[:, 1] = np.minimum(low[:, 1], h - 1)
    high = np.minimum(low + 1, [w - 1, h - 1])
    t = xy - low
    p = surface.pixels
    rgb = ((p[low[:, 1], low[:, 0], :3] * (1 - t[:, :1]) + p[low[:, 1], high[:, 0], :3] * t[:, :1]) * (1 - t[:, 1:])
           + (p[high[:, 1], low[:, 0], :3] * (1 - t[:, :1]) + p[high[:, 1], high[:, 0], :3] * t[:, :1]) * t[:, 1:])
    mesh = obj.data
    poly = np.empty(len(mesh.loop_triangles), np.int32)
    mesh.loop_triangles.foreach_get('polygon_index', poly)
    return rgb, poly


def build(source, swatches, dest=None, cameras_path=None, regions_path=None):
    source = Path(source).resolve()
    dest = Path(dest) if dest else source.parent / 'highpoly.blend'
    if dest.exists():
        print('HIGHPOLY_BLEND exists, kept:', dest)
        return dest
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(source), merge_vertices=False)
    meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    if len(meshes) != 1:
        raise ValueError(f'Expected one Tripo mesh; found {len(meshes)}')
    textured = meshes[0]
    textured.name = 'highpoly_textured'
    rgb, poly = uv_centre_colors(textured)
    role = np.full(len(textured.data.polygons), 3, np.int32)
    role[poly] = face_labels.classify_samples(rgb, swatches)[0]  # Tripo meshes: one loop-tri per polygon
    detected = textured.copy()
    detected.data = textured.data.copy()
    detected.name = 'highpoly_detected'
    for c in textured.users_collection:
        c.objects.link(detected)
    detected.data.materials.clear()
    for name, color in ROLE_COLORS.items():
        mat = bpy.data.materials.new(name)
        mat.diffuse_color = (*color, 1)
        mat.use_nodes = True
        mat.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (*color, 1)
        mat.node_tree.nodes['Principled BSDF'].inputs['Roughness'].default_value = .9
        detected.data.materials.append(mat)
    detected.data.polygons.foreach_set('material_index', role)
    width = max(v[0] for v in textured.bound_box) - min(v[0] for v in textured.bound_box)
    detected.location.x += width * 1.3
    if cameras_path and regions_path:
        mw = np.array(textured.matrix_world)
        v = np.array([tuple(x.co) for x in textured.data.vertices]) @ mw[:3, :3].T + mw[:3, 3]
        low, high = v.min(0), v.max(0)
        origin = (low + high) / 2
        origin[2] = low[2]
        v = (v - origin) * (2 / (high[2] - low[2]))          # reduce_model's normalized frame
        f = np.array([tuple(p.vertices) for p in textured.data.polygons])
        normals, _ = face_labels.face_normals(v, f)
        tree = BVHTree.FromPolygons(v.tolist(), f.tolist(), all_triangles=True)
        cams = face_labels.load_cameras(cameras_path, json.loads(Path(regions_path).read_text()))
        seen = face_labels.visibility(v[f].mean(1), normals, tree, cams) > 0
        shown = textured.copy()
        shown.data = textured.data.copy()
        shown.name = 'highpoly_seen'
        for c in textured.users_collection:
            c.objects.link(shown)
        shown.data.materials.clear()
        for name, color in (('seen', (.92, .92, .92)), ('unseen', (.12, .12, .12))):
            mat = bpy.data.materials.new(name)
            mat.diffuse_color = (*color, 1)
            shown.data.materials.append(mat)
        shown.data.polygons.foreach_set('material_index', (~seen).astype(np.int32))
        shown.location.x += 2 * (max(c[0] for c in textured.bound_box) - min(c[0] for c in textured.bound_box)) * 1.3
        shown['unseen_faces'] = int((~seen).sum())
    counts = np.bincount(role, minlength=4)
    detected['detected_face_counts'] = {k: int(n) for k, n in zip(ROLE_COLORS, counts)}
    detected['source_sha256'] = rm.digest(source)
    bpy.ops.file.pack_all()
    bpy.ops.wm.save_as_mainfile(filepath=str(dest), compress=True)
    print('HIGHPOLY_BLEND', dest, json.dumps(detected['detected_face_counts'].to_dict()), flush=True)
    return dest


if __name__ == '__main__':
    args = sys.argv[sys.argv.index('--') + 1:]
    if len(args) not in (2, 4):
        raise SystemExit(__doc__)
    build(args[0], json.loads(Path(args[1]).read_text()), cameras_path=args[2] if len(args) == 4 else None,
          regions_path=args[3] if len(args) == 4 else None)
