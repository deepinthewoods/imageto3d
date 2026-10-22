"""Build `highpoly.blend` beside a Tripo source GLB: the full-resolution mesh twice, side by side.

Usage: blender -b --python tools/tripo_pipeline/make_highpoly_blend.py -- <source.glb> <swatches.json|palette.json>
           [<cameras transforms.json> <regions.json> [<parts.npz> [<module>]]]

- `highpoly_detected`: every triangle gets the material detected from the texture at its UV centre
  (base / one material per palette garment role - primary, secondary, and with white_anchor_v2 also
  tertiary, accent - or orange `uncertain` when the pixel is too grey/dark or between hues;
  palette.Palette.classify).
- `off_body` (magenta; cyan when the palette has a magenta accent; with parts + module + clearance record): a face the texture calls base that lies
  clearly outside the fitted base body where the module allows garment (face_labels.off_body_excess).
- `highpoly_seen` (only with cameras): white where a concept view saw the surface, dark grey where
  none did. Texture there is Tripo's invention and the pipeline barely trusts it.
- `highpoly_textured`: the untouched import with Tripo's own texture (packed into the file).
- with a base parts file (`[parts.npz]`, or `base_parts`/`part_fit` from reduce_model.py): every
  object carries the FACE attribute `body_part` (names in the mesh property `body_part_names`) and
  `highpoly_parts` shows one flat colour per part, so the transfer can be checked by eye.
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
               'secondary': (0.12, 0.25, 0.92), 'tertiary': (0.96, 0.86, 0.08),
               'accent': (0.88, 0.1, 0.86), 'uncertain': (1.0, 0.4, 0.03),
               'off_body': (0.95, 0.3, 0.75)}
OFF_BODY_ALT = (0.1, 0.85, 0.95)     # when the palette's accent is magenta, off_body would be indistinguishable


def role_colors(pal):
    """Ordered {material name: display colour}: base, garment roles, uncertain, off_body (class index = position)."""
    from palette import swatch_rgb
    out = {'base': ROLE_COLORS['base']}
    for role, hex_ in pal.garments:
        out[role] = ROLE_COLORS.get(role) or tuple(swatch_rgb(hex_).tolist())
    out['uncertain'] = ROLE_COLORS['uncertain']
    out['off_body'] = OFF_BODY_ALT if 'accent' in pal.roles else ROLE_COLORS['off_body']
    return out


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


def normalized_triangles(obj):
    """Vertices in reduce_model's normalized frame, triangles, and the polygon of each triangle."""
    mw = np.array(obj.matrix_world)
    mesh = obj.data
    mesh.calc_loop_triangles()
    v = np.empty(len(mesh.vertices) * 3)
    mesh.vertices.foreach_get('co', v)
    v = v.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]
    low, high = v.min(0), v.max(0)
    origin = (low + high) / 2
    origin[2] = low[2]
    f = np.empty(len(mesh.loop_triangles) * 3, np.int64)
    mesh.loop_triangles.foreach_get('vertices', f)
    poly = np.empty(len(mesh.loop_triangles), np.int64)
    mesh.loop_triangles.foreach_get('polygon_index', poly)
    return (v - origin) * (2 / (high[2] - low[2])), f.reshape(-1, 3), poly


def build(source, palette, dest=None, cameras_path=None, regions_path=None, parts_path=None,
          base_parts=None, part_fit=None, module=None):
    source = Path(source).resolve()
    pal = face_labels.Palette.coerce(palette)
    colors_by_role = role_colors(pal)
    off_body_index = len(colors_by_role) - 1
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
    parts = None
    if parts_path or base_parts is not None:
        if base_parts is None:
            base_parts = face_labels.BaseParts.load(parts_path)
        v, f, tri_poly = normalized_triangles(textured)
        if part_fit is None:
            scale, shift, fit = face_labels.fit_base(base_parts, v, f)
            part_fit = base_parts.moved(scale, shift) if fit['used'] == 'icp' else base_parts
        tri_parts = face_labels.transfer_parts(v, f, part_fit)
        parts = np.zeros(len(textured.data.polygons), np.int64)
        parts[tri_poly] = tri_parts
        rm.set_parts(textured.data, part_fit.names, parts)
    rgb, poly = uv_centre_colors(textured)
    role = np.full(len(textured.data.polygons), pal.unusable, np.int32)
    role[poly] = pal.classify(rgb)[0]  # Tripo meshes: one loop-tri per polygon
    if parts is not None and module is not None and part_fit.clearance is not None:
        regions = json.loads(Path(regions_path).read_text()) if regions_path else None
        tri_base = np.flatnonzero(role[tri_poly] == 0)
        threshold = face_labels.off_body_threshold(tri_parts[tri_base], part_fit)
        far = face_labels.off_body_distance(v, f[tri_base], part_fit) >= threshold + face_labels.OFF_BODY_RAMP / 2
        cand = tri_base[far]
        if len(cand):    # the prior (zones where garment is allowed) only for the far faces
            n_cand = face_labels.face_normals(v, f[cand])[0]
            c_cand = v[f[cand]].mean(1)
            zones = face_labels.part_zones(part_fit.membership(c_cand, n_cand), part_fit.names)
            allowed = face_labels.wear_prior(c_cand, module, regions, zones) >= face_labels.OFF_BODY_MIN_PRIOR
            role[tri_poly[cand[allowed]]] = off_body_index
    detected = textured.copy()
    detected.data = textured.data.copy()
    detected.name = 'highpoly_detected'
    for c in textured.users_collection:
        c.objects.link(detected)
    detected.data.materials.clear()
    for name, color in colors_by_role.items():
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
        v, f, _ = normalized_triangles(textured)             # reduce_model's normalized frame
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
    if parts is not None:
        shown_parts = rm.part_object(textured, part_fit.names, parts, 'highpoly_parts', (0, 0, 0), {})
        shown_parts.location.x += 3 * width * 1.3
        shown_parts['part_face_counts'] = {n: int(c) for n, c in zip(part_fit.names, np.bincount(
            parts, minlength=len(part_fit.names)))}
    counts = np.bincount(role, minlength=len(colors_by_role))
    detected['detected_face_counts'] = {k: int(n) for k, n in zip(colors_by_role, counts)}
    detected['palette_scheme'] = pal.scheme
    detected['source_sha256'] = rm.digest(source)
    bpy.ops.file.pack_all()
    bpy.ops.wm.save_as_mainfile(filepath=str(dest), compress=True)
    print('HIGHPOLY_BLEND', dest, json.dumps(detected['detected_face_counts'].to_dict()), flush=True)
    return dest


if __name__ == '__main__':
    args = sys.argv[sys.argv.index('--') + 1:]
    if len(args) not in (2, 4, 5, 6):
        raise SystemExit(__doc__)
    module = args[5] if len(args) == 6 else None
    build(args[0], Path(args[1]), cameras_path=args[2] if len(args) >= 4 else None,
          regions_path=args[3] if len(args) >= 4 else None, parts_path=args[4] if len(args) >= 5 else None,
          module=module)
