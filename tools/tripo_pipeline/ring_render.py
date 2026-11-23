"""Blender Workbench renders for ring_labels.py (EXPERIMENT 2026-10-25; nothing imports this).

    blender -b --factory-startup -t 3 --python tools/tripo_pipeline/ring_render.py -- <job.json>

job.json: {"renders": [{"out": png, "bg": [r,g,b], "light": "flat"|"studio", "cavity": bool, "size": [w, h],
                        "cam": {"target": [x,y,z], "dir": [dx,dy,dz], "scale": ortho_scale},
                        "meshes": [{"npz": path, "dx": float, "rot_z_deg": float}]}]}
Each mesh npz: `v` (n, 3), `f` (m, 3) and `rgb` (m, 3) float 0..1 sRGB per face (removed faces are simply not in f). The model faces -Y, +Z is up
(normalised frame, height 2); a camera direction of (0, -1, 0) is the front view, (0, 1, 0) the back.
"""
import json
import math
import os
import sys

import bpy
import numpy as np
from mathutils import Vector


def lin(c):
    c = np.asarray(c, float)
    return np.where(c <= .04045, c / 12.92, ((c + .055) / 1.055) ** 2.4)


def make_mesh(name, v, f, rgb):
    v = np.asarray(v, np.float32)
    f = np.asarray(f, np.int32)
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(v))
    mesh.vertices.foreach_set('co', v.ravel())
    mesh.loops.add(len(f) * 3)
    mesh.loops.foreach_set('vertex_index', f.ravel())
    mesh.polygons.add(len(f))
    mesh.polygons.foreach_set('loop_start', np.arange(0, len(f) * 3, 3, dtype=np.int32))
    mesh.polygons.foreach_set('loop_total', np.full(len(f), 3, np.int32))
    mesh.update()
    attr = mesh.color_attributes.new('c', 'FLOAT_COLOR', 'CORNER')
    rgba = np.concatenate([lin(np.repeat(np.asarray(rgb, float), 3, axis=0)), np.ones((len(f) * 3, 1))], axis=1)
    attr.data.foreach_set('color', rgba.astype(np.float32).ravel())
    mesh.color_attributes.active_color = attr
    mesh.color_attributes.render_color_index = 0
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def setup(bg, light, cavity, size):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = 'BLENDER_WORKBENCH'
    sc.render.film_transparent = False
    sc.view_settings.view_transform = 'Standard'
    sh = sc.display.shading
    sh.light = 'FLAT' if light == 'flat' else 'STUDIO'
    sh.color_type = 'VERTEX'
    sh.show_specular_highlight = False
    sh.show_object_outline = False
    sh.show_cavity = bool(cavity)
    if cavity:
        sh.cavity_type = 'BOTH'
        sh.cavity_ridge_factor = 1.0
        sh.cavity_valley_factor = 1.4
        sh.curvature_ridge_factor = 1.0
        sh.curvature_valley_factor = 1.0
    sc.world = bpy.data.worlds.new('w')
    sc.world.color = lin(bg)
    sc.render.resolution_x, sc.render.resolution_y = size
    sc.render.resolution_percentage = 100
    sc.render.image_settings.file_format = 'PNG'
    return sc


def camera(target, direction, scale):
    d = Vector(direction).normalized()
    cam = bpy.data.objects.new('cam', bpy.data.cameras.new('cam'))
    bpy.context.scene.collection.objects.link(cam)
    cam.data.type = 'ORTHO'
    cam.data.ortho_scale = scale
    cam.data.clip_start, cam.data.clip_end = .01, 100
    cam.location = Vector(target) + d * 10
    up = Vector((0, 0, 1)) if abs(d.z) < .95 else Vector((0, 1, 0))
    rot = (-d).to_track_quat('-Z', 'Y')
    cam.rotation_euler = rot.to_euler()
    bpy.context.scene.camera = cam


def main():
    job = json.loads(open(sys.argv[sys.argv.index('--') + 1]).read())
    cache = {}
    for r in job['renders']:
        size = r.get('size', [1000, 1000])
        setup(r.get('bg', [.80, .80, .82]), r.get('light', 'flat'), r.get('cavity', False), size)
        for i, m in enumerate(r['meshes']):
            if m['npz'] not in cache:
                z = np.load(m['npz'])
                cache[m['npz']] = (z['v'].astype(np.float32), z['f'].astype(np.int32), z['rgb'].astype(np.float32))
            v, f, rgb = cache[m['npz']]
            obj = make_mesh(f'm{i}', v, f, rgb)
            obj.location.x = m.get('dx', 0.)
            obj.rotation_euler.z = math.radians(m.get('rot_z_deg', 0.))
        cam = r['cam']
        camera(cam['target'], cam['dir'], cam['scale'])
        bpy.context.scene.render.filepath = os.path.abspath(r['out'])
        bpy.ops.render.render(write_still=True)
        print('RENDERED', r['out'], flush=True)


main()
