"""Stage 1 of the segment-first prototype (Blender): everything reduce_model.py does before it reduces, plus per-face
texture colours of the HIGH-POLY mesh.

Usage: blender -b --python tools/tripo_pipeline/segment_first_prep.py -- source.glb out_dir
           [--palette dir|json] [--cameras transforms.json] [--regions regions.json] [--module body_armor]

Same front matter as reduce_model.run (import, height normalisation, interior audit/strip, weld, local repair, canonical
order, ICP fit of the base parts) so both pipelines reduce exactly the same cleaned mesh. Then, for every cleaned face
(typically 0.7-0.9 M): texture colours at three interior points (read straight from the face's UVs through the nearest
source triangle, no projection search), the body part of the fitted base (face_labels.transfer_parts) and the best
unoccluded concept-camera cosine. Writes out_dir/prep.npz and out_dir/prep.json; segment_first.py (plain Python with
numpy/scipy) does the rest. Nothing of reduce_model.py is changed or called with different behaviour.
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, kdtree
from mathutils.bvhtree import BVHTree

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import reduce_model as rm  # noqa: E402  (imports bpy and the lowpoly lab; running it as a module does nothing)

lab, texture_tools, adaptive, tripo, face_labels = rm.lab, rm.texture_tools, rm.adaptive, rm.tripo, rm.face_labels


def sample_with_ids(surface, points, ids):
    """TextureSurface.sample for known source triangles `ids` (same maths, no nearest-point search)."""
    tri = surface.v[surface.f[ids]]
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    e0, e1, q = b - a, c - a, points - a
    dot = lambda x, y: np.einsum('ij,ij->i', x, y)
    d00, d01, d11 = dot(e0, e0), dot(e0, e1), dot(e1, e1)
    d20, d21 = dot(q, e0), dot(q, e1)
    den = np.maximum(d00 * d11 - d01 * d01, 1e-30)
    u = (d11 * d20 - d01 * d21) / den
    v = (d00 * d21 - d01 * d20) / den
    bary = np.clip(np.stack([1 - u - v, u, v], axis=1), 0, 1)
    bary /= np.maximum(bary.sum(axis=1, keepdims=True), 1e-12)
    uv = np.einsum('ni,nij->nj', bary, surface.uv[ids])
    h, w = surface.pixels.shape[:2]
    xy = np.maximum(np.clip(uv, 0, 1) * np.array([w, h]) - .5, 0)
    low = np.floor(xy).astype(int)
    low[:, 0] = np.minimum(low[:, 0], w - 1)
    low[:, 1] = np.minimum(low[:, 1], h - 1)
    high = np.minimum(low + 1, [w - 1, h - 1])
    t = xy - low
    p = surface.pixels
    p00, p10 = p[low[:, 1], low[:, 0], :3], p[low[:, 1], high[:, 0], :3]
    p01, p11 = p[high[:, 1], low[:, 0], :3], p[high[:, 1], high[:, 0], :3]
    return ((p00 * (1 - t[:, :1]) + p10 * t[:, :1]) * (1 - t[:, 1:]) + (p01 * (1 - t[:, :1]) + p11 * t[:, :1]) * t[:, 1:])


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument('source')
    ap.add_argument('out')
    ap.add_argument('--palette')
    ap.add_argument('--cameras')
    ap.add_argument('--regions')
    ap.add_argument('--module')
    a = ap.parse_args(argv)
    t0 = time.monotonic()
    timing = {}
    source, out = Path(a.source).resolve(), Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    swatch_path, cameras_path, regions_path, parts_path, module, notes, pal = rm.resolve_inputs(
        source, None, a.cameras, a.regions, a.module, None, True, a.palette, False)
    if cameras_path and not regions_path:
        cameras_path = None
    regions = json.loads(Path(regions_path).read_text()) if regions_path else None
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(source), merge_vertices=False)
    obj = [o for o in bpy.context.scene.objects if o.type == 'MESH'][0]
    bpy.ops.file.pack_all()
    lab.active(obj)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    native, _ = lab.arrays(obj)
    rotation = np.asarray(Matrix.Rotation(0.0, 3, 'Z'))
    turned = native @ rotation.T
    low, high = turned.min(axis=0), turned.max(axis=0)
    origin = (low + high) / 2
    origin[2] = low[2]
    scale = 2 / (high[2] - low[2])
    obj.data.vertices.foreach_set('co', ((turned - origin) * scale).ravel())
    obj.data.update()
    surface = texture_tools.TextureSurface(obj)
    sv, sf = surface.v.astype(np.float64), surface.f.astype(np.int64)
    interior = face_labels.interior_metrics(sv, sf, surface.tree)
    flagged = interior['interior_area_share'] >= face_labels.INTERIOR_THRESHOLD
    enclosed = None
    if flagged:
        enclosed = face_labels.enclosed_faces(sv, sf, surface.tree)
        surface.f, surface.uv = surface.f[~enclosed], surface.uv[~enclosed]
        surface.tree = BVHTree.FromPolygons(surface.v.tolist(), surface.f.tolist(), all_triangles=True)
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    if enclosed is not None:
        bm.faces.ensure_lookup_table()
        bmesh.ops.delete(bm, geom=[bm.faces[i] for i in np.flatnonzero(enclosed)], context='FACES')
        bmesh.ops.delete(bm, geom=[x for x in bm.verts if not x.link_faces], context='VERTS')
    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=2e-7)
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()
    rv, rf = lab.arrays(obj)
    cleaned_v, cleaned_f, cleanup = adaptive.local_repair(rv, rf)
    cleaned_v, cleaned_f = rm.canonical_order(cleaned_v, cleaned_f)
    timing['front_matter_s'] = time.monotonic() - t0
    print('PREP front matter', len(cleaned_f), 'faces', f'{timing["front_matter_s"]:.0f}s', flush=True)

    # base parts (ICP fit to the raw mesh, like reduce_model)
    t1 = time.monotonic()
    tree = BVHTree.FromPolygons(rv.tolist(), rf.tolist(), all_triangles=True)
    clearance_path = None
    base_parts = face_labels.BaseParts.load(parts_path, clearance_path)
    scale_fit, shift_fit, fit_info = face_labels.fit_base(base_parts, rv, rf, tree=tree)
    part_fit = base_parts.moved(scale_fit, shift_fit) if fit_info['used'] == 'icp' else base_parts
    part = face_labels.transfer_parts(cleaned_v, cleaned_f, part_fit)
    timing['parts_s'] = time.monotonic() - t1
    print('PREP parts', json.dumps(fit_info), f'{timing["parts_s"]:.0f}s', flush=True)

    # visibility of the high-poly faces from the concept cameras
    t1 = time.monotonic()
    hi_normals, hi_area = face_labels.face_normals(cleaned_v, cleaned_f)
    cams = face_labels.load_cameras(cameras_path, regions) if cameras_path else []
    cent = cleaned_v[cleaned_f].mean(1)
    if cams:
        hi_tree = BVHTree.FromPolygons(cleaned_v.tolist(), cleaned_f.tolist(), all_triangles=True)
        hi_cos = face_labels.visibility(cent, hi_normals, hi_tree, cams)
    else:
        hi_cos = np.ones(len(cleaned_f))
    timing['visibility_s'] = time.monotonic() - t1
    print('PREP visibility', f'{timing["visibility_s"]:.0f}s', flush=True)

    # texture colours: three interior points per cleaned face, through the nearest source triangle
    t1 = time.monotonic()
    tris = surface.v[surface.f]
    kd = kdtree.KDTree(len(surface.f))
    for i, c in enumerate(tris.mean(1)):
        kd.insert(c.tolist(), i)
    kd.balance()
    ids = np.array([kd.find(c.tolist())[1] for c in cent], np.int64)
    tri = cleaned_v[cleaned_f].astype(np.float32)
    colors = np.zeros((len(cleaned_f), 3, 3), np.float32)
    for k, w in enumerate(((2 / 3, 1 / 6, 1 / 6), (1 / 6, 2 / 3, 1 / 6), (1 / 6, 1 / 6, 2 / 3))):
        pts = (tri * np.array(w, np.float32)[None, :, None]).sum(1)
        colors[:, k] = sample_with_ids(surface, pts, ids)
    timing['colours_s'] = time.monotonic() - t1
    print('PREP colours', f'{timing["colours_s"]:.0f}s', flush=True)

    np.savez_compressed(out / 'prep.npz', v=cleaned_v.astype(np.float32), f=cleaned_f.astype(np.int32), colors=colors,
                        part=np.asarray(part, np.int16), part_names=np.array(part_fit.names), cos=hi_cos.astype(np.float32),
                        source_tri=ids.astype(np.int32))
    meta = {'source': str(source), 'module': module, 'normalization_scale': float(scale),
            'normalization_origin_rotated': origin.tolist(), 'rotation_to_review': rotation.tolist(),
            'cameras': str(cameras_path) if cameras_path else None, 'regions': str(regions_path) if regions_path else None,
            'palette_source': str(a.palette) if a.palette else notes.get('palette'), 'palette': pal.describe(),
            'parts': {'file': str(Path(parts_path).resolve()), 'fit': fit_info, 'names': part_fit.names},
            'cleanup': cleanup, 'interior': interior, 'input_sources': notes, 'timing': timing,
            'faces': int(len(cleaned_f)), 'source_triangles': int(len(sf)),
            'unseen_area_share': float(hi_area[hi_cos <= 0].sum() / hi_area.sum()) if cams else None,
            'total_s': time.monotonic() - t0}
    (out / 'prep.json').write_text(json.dumps(meta, indent=2) + '\n')
    print('PREP_DONE', json.dumps(timing), flush=True)


if __name__ == '__main__':
    main(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else sys.argv[1:])
