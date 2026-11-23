"""Blender Workbench renders of role-coloured meshes for feature_labels.py (PROTOTYPE 2026-10-25).

    blender -b --factory-startup -t 3 --python tools/tripo_pipeline/feature_labels_render.py -- <jobs.json>

jobs.json: [{"out": png, "npz": file with v (n,3), f (m,3) and the role array named by "key", "key": "role", "view": "front|back|obl",
"centre": [x, y, z], "half": half width of the view in normalised units, "size": [w, h]}, ...] (normalised frame, z up, the model faces -Y).
Flat role colours (base #E61923, primary #19D228, secondary #1E41EB, tertiary #F5DC14, accent #E119DC), studio light so domes read.
"""
import json
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector

ROLE_RGB = np.array([(230, 25, 35), (25, 210, 40), (30, 65, 235), (245, 220, 20), (225, 25, 220), (255, 140, 0)], float) / 255
VIEWS = {'front': (0., -1., 0.), 'back': (0., 1., 0.), 'obl': (-.55, -.6, .58)}


def lin(c):
    return np.asarray(c, float) ** 2.2


def scene_setup():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = 'BLENDER_WORKBENCH'
    sc.render.film_transparent = False
    sc.view_settings.view_transform = 'Standard'
    sh = sc.display.shading
    sh.light = 'STUDIO'
    sh.color_type = 'MATERIAL'
    sh.show_object_outline = False
    sh.show_cavity = False
    sh.show_specular_highlight = False
    sc.world = bpy.data.worlds.new('w')
    sc.world.color = lin((.36, .36, .38))
    return sc


def add_mesh(v, f, role):
    mesh = bpy.data.meshes.new('m')
    mesh.from_pydata(np.asarray(v, float).tolist(), [], np.asarray(f).tolist())
    mesh.update()
    for r, rgb in enumerate(ROLE_RGB):
        mat = bpy.data.materials.new(f'r{r}')
        mat.diffuse_color = (*lin(rgb), 1.)
        mesh.materials.append(mat)
    mesh.polygons.foreach_set('material_index', np.clip(np.asarray(role, np.int32), 0, len(ROLE_RGB) - 1))
    obj = bpy.data.objects.new('m', mesh)
    bpy.context.scene.collection.objects.link(obj)


def camera(centre, direction, half, size):
    d = Vector(direction).normalized()
    cam = bpy.data.objects.new('cam', bpy.data.cameras.new('cam'))
    bpy.context.scene.collection.objects.link(cam)
    cam.data.type = 'ORTHO'
    cam.data.ortho_scale = half * 2 * max(1., size[0] / size[1])
    cam.data.clip_start, cam.data.clip_end = .01, 100
    cam.location = Vector(centre) + d * 10
    cam.rotation_euler = (-d).to_track_quat('-Z', 'Y').to_euler()
    bpy.context.scene.camera = cam
    bpy.context.scene.render.resolution_x, bpy.context.scene.render.resolution_y = size
    bpy.context.scene.render.resolution_percentage = 100


def main():
    jobs = json.loads(Path(sys.argv[sys.argv.index('--') + 1]).read_text(encoding='utf-8-sig'))
    cache = {}
    for j in jobs:
        z = cache.get(j['npz'])
        if z is None:
            zz = np.load(j['npz'])
            z = cache[j['npz']] = {k: zz[k] for k in zz.files}
        scene_setup()
        add_mesh(z['v'].astype(float), z['f'].astype(int), z[j.get('key', 'role')])
        camera(j['centre'], VIEWS[j['view']], j['half'], tuple(j.get('size', (900, 900))))
        bpy.context.scene.render.filepath = j['out']
        bpy.context.scene.render.image_settings.file_format = 'PNG'
        bpy.ops.render.render(write_still=True)
        print('RENDERED', j['out'], flush=True)


main()
