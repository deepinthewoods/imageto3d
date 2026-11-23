"""Blender renders for symmetry_cut.py (EXPERIMENT 2026-10-24; nothing imports this).

    blender -b --factory-startup -t 3 --python tools/tripo_pipeline/symmetry_cut_render.py -- <out_dir>

Reads what symmetry_cut.py wrote into <out_dir> (current_lod_<lv>.npz, proto_lod_<lv>.npz, heat_pooling_lod.npz, render_plan.*)
and renders with the Workbench engine, flat colours by garment role:
    r_current_fb.png / r_proto_fb.png   front | back of the whole wearable (1800 x 900)
    r_heat_pool.png                     pooling category on the full labelled LOD, front | back
    r_close<k>_<front|obl>_<cur|pro>.png  close-ups of the k-th opening region (700 x 700) with the cut boundary drawn as a red tube
"""
import json
import math
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import symmetry_scan as ss  # noqa: E402  (Blender-side render helpers)


def lin(c):
    return np.asarray(c, float) ** 2.2


def role_rgb(role, roles_rgb):
    return np.asarray(roles_rgb)[np.asarray(role)][:, :3]


def clear_cams():
    for o in [o for o in bpy.context.scene.objects if o.type == 'CAMERA']:
        bpy.data.objects.remove(o)


def look_camera(target, direction, scale, size):
    d = Vector(direction).normalized()
    cam = bpy.data.objects.new('cam', bpy.data.cameras.new('cam'))
    bpy.context.scene.collection.objects.link(cam)
    cam.data.type = 'ORTHO'
    cam.data.ortho_scale = scale
    cam.data.clip_start, cam.data.clip_end = .01, 100
    cam.location = Vector(target) + d * 10
    cam.rotation_euler = (-d).to_track_quat('-Z', 'Y').to_euler()
    bpy.context.scene.camera = cam
    bpy.context.scene.render.resolution_x, bpy.context.scene.render.resolution_y = size
    bpy.context.scene.render.resolution_percentage = 100


def role_mesh(name, v, f, role, roles_rgb):
    """Mesh with one flat material per garment role (viewport colour = the palette colour)."""
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(np.asarray(v, float).tolist(), [], np.asarray(f).tolist())
    mesh.update()
    for r, rgb in enumerate(roles_rgb):
        mat = bpy.data.materials.new(f'{name}_{r}')
        mat.diffuse_color = (*lin(rgb[:3]), 1.)
        mesh.materials.append(mat)
    mesh.polygons.foreach_set('material_index', np.asarray(role, np.int32))
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def tube(name, poly, radius=.0035, colour=(.9, .05, .05)):
    curve = bpy.data.curves.new(name, 'CURVE')
    curve.dimensions = '3D'
    curve.bevel_depth = radius
    curve.bevel_resolution = 1
    sp = curve.splines.new('POLY')
    sp.points.add(len(poly) - 1)
    for p, c in zip(sp.points, poly):
        p.co = (c[0], c[1], c[2], 1.)
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*lin(colour), 1.)
    curve.materials.append(mat)
    obj = bpy.data.objects.new(name, curve)
    bpy.context.scene.collection.objects.link(obj)
    obj.show_in_front = True
    return obj


def render_close(path, v, f, role, roles_rgb, polys, region, direction):
    scene = ss.new_scene((.80, .80, .82))
    scene.display.shading.color_type = 'MATERIAL'
    scene.display.shading.show_object_outline = True
    scene.display.shading.object_outline_color = (.05, .05, .05)
    role_mesh('m', v, f, role, roles_rgb)
    for i, p in enumerate(polys):
        if len(p) >= 2:
            tube(f'b{i}', p)
    look_camera(region['centre'], direction, region['half'] * 2, (700, 700))
    ss.render_to(path)


def main():
    out = Path(sys.argv[sys.argv.index('--') + 1])
    plan = json.loads((out / 'render_plan.json').read_text(encoding='utf-8-sig'))
    lv = plan['level']
    roles_rgb = plan['roles_rgb']
    z = np.load(out / 'render_plan.npz')
    cur = np.load(out / f'current_lod_{lv}.npz')
    pro = np.load(out / f'proto_lod_{lv}.npz')
    meshes = {'cur': (cur['v'].astype(float), cur['f'].astype(int), cur['role'].astype(int)),
              'pro': (pro['v'].astype(float), pro['f'].astype(int), pro['role'].astype(int))}
    names = {'cur': 'current', 'pro': 'proto'}
    for k, (v, f, role) in meshes.items():
        ss.render_front_back(v, f, role_rgb(role, roles_rgb), out / f'r_{names[k]}_fb.png')
    heat = out / 'heat_pooling_lod.npz'
    if heat.is_file():
        h = np.load(heat)
        ss.render_front_back(h['v'].astype(float), h['f'].astype(int), h['rgb'].astype(float), out / 'r_heat_pool.png')
    polys = {'cur': [z[k] for k in z.files if k.startswith('cur_b')], 'pro': [z[k] for k in z.files if k.startswith('pro_b')]}
    views = {'front': (0., -1., 0.), 'obl': (-.55, -.6, .58)}
    for i, region in enumerate(plan['regions']):
        for vname, direction in views.items():
            for k, (v, f, role) in meshes.items():
                render_close(out / f'r_close{i}_{vname}_{k}.png', v, f, role, roles_rgb, polys[k], region, direction)
    print('RENDER_DONE', out)


main()
