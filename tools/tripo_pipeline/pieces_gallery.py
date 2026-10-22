"""Review gallery of rigid armour pieces as ONE saved .blend (+ an overview PNG), one row per concept.

Usage: blender -b --python tools/tripo_pipeline/pieces_gallery.py -- <out.blend> [--png <overview.png> | --no-png] [--levels 0p4]

Rows (config in ROWS below), left to right: the textured Tripo high-poly source, OLD per-face regions pieces (pieces_nofringe),
NEW per-face regions (pieces_shelf: shelf rule + spike drop), OLD segment-first (segment_first_fringe/before), NEW segment-first
(segment_first_seam_shelf: seam fix + shelf rule), that one exploded, then three spine-bend demos seen from the side (group rotated
90 degrees about Z, "front ->" marks the front; 20 degrees forward about the waist pivot, chest + arms + pauldrons rotate):
OLD segment-first, NEW segment-first, NEW per-face regions. Red sphere = waist pivot. Pieces are flat-coloured per piece, no overlap strips. Never overwrites: a versioned name
is used when the file exists. The file is packed (textures included) so it opens standalone; the scene camera is a front
orthographic camera and the 3D viewport is set to Material Preview where a screen exists.
"""
import json
import math
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Euler, Vector

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import split_pieces as sp  # noqa: E402

ROOT = HERE.parents[1]
EXP = ROOT / 'experiments'
PIECE_NAMES = ['chest', 'pelvis', 'thigh_l', 'thigh_r', 'arm_l', 'arm_r', 'pauldron_l', 'pauldron_r']
COLORS = {'chest': (.62, .63, .66), 'pelvis': (.80, .64, .38), 'thigh_l': (.52, .33, .82), 'thigh_r': (.30, .38, .88),
          'arm_l': (.20, .66, .36), 'arm_r': (.50, .74, .20), 'pauldron_l': (.93, .42, .12), 'pauldron_r': (.92, .30, .62)}
SPINE_MOVERS = ('chest', 'arm_l', 'arm_r', 'pauldron_l', 'pauldron_r')
ROWS = [('P_11', 'P_11 Knight errant', 'P_11_knight_errant'), ('P_29', 'P_29 Hawk champion', 'P_29_hawk_helmless_champion'),
        ('P_40', 'P_40 Celestial orrery', 'P_40_celestial_orrery')]
ROW_PITCH = 1.55
COL_X = [0.0, 1.2, 2.4, 3.6, 4.8, 6.25, 7.65, 8.35, 9.05]      # source, old/new regions, old/new seg-first, new exploded, 3 side views


def unique(path):
    path = Path(path)
    if not path.exists():
        return path
    k = 2
    while True:
        cand = path.with_name(f'{path.stem}_v{k}{path.suffix}')
        if not cand.exists():
            return cand
        k += 1


def to_bl(v):
    """GLB frame (x lateral, y up, +z front) -> the Blender frame the glTF importer produces (x, -z, y)."""
    v = np.asarray(v, float)
    return np.stack([v[..., 0], -v[..., 2], v[..., 1]], -1)


def load_pieces(lod_dir):
    out = {}
    for n in PIECE_NAMES:
        p = Path(lod_dir) / f'{n}.glb'
        if p.is_file():
            g = sp.read_glb(p)
            out[n] = (g['v'], g['f'])
    return out


def piece_info(lod_dir):
    """(waist pivot in the GLB frame, body height) from the level's pieces.json entry."""
    d = json.loads((Path(lod_dir).parent / 'pieces.json').read_text())
    lv = Path(lod_dir).name[len('lod_'):]
    rg = d['levels'][lv]['rigid']
    return np.asarray(rg['joints']['waist'], float), float(d['levels'][lv]['body_height'])


_MATS = {}


def material(name, rgb, emit=False):
    if name in _MATS:
        return _MATS[name]
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = m.node_tree.nodes.get('Principled BSDF')
    bsdf.inputs['Base Color'].default_value = (*rgb, 1.)
    bsdf.inputs['Roughness'].default_value = .85
    if emit and 'Emission Color' in bsdf.inputs:
        bsdf.inputs['Emission Color'].default_value = (*rgb, 1.)
        bsdf.inputs['Emission Strength'].default_value = 1.
    m.diffuse_color = (*rgb, 1.)
    _MATS[name] = m
    return m


def add_mesh(name, v, f, mat, parent):
    me = bpy.data.meshes.new(name)
    v = np.asarray(v, np.float32)
    f = np.asarray(f, np.int32)
    me.vertices.add(len(v))
    me.vertices.foreach_set('co', v.ravel())
    me.loops.add(len(f) * 3)
    me.loops.foreach_set('vertex_index', f.ravel())
    me.polygons.add(len(f))
    me.polygons.foreach_set('loop_start', np.arange(0, len(f) * 3, 3, dtype=np.int32))
    me.polygons.foreach_set('loop_total', np.full(len(f), 3, np.int32))
    me.update(calc_edges=True)
    me.materials.append(mat)
    ob = bpy.data.objects.new(name, me)
    ob.parent = parent
    bpy.context.scene.collection.objects.link(ob)
    return ob


def add_empty(name, loc, rot_z=0.):
    e = bpy.data.objects.new(name, None)
    e.location = loc
    e.rotation_euler = Euler((0, 0, math.radians(rot_z)))
    bpy.context.scene.collection.objects.link(e)
    return e


def add_text(body, loc, size=.045, align='CENTER'):
    c = bpy.data.curves.new('txt', 'FONT')
    c.body = body
    c.size = size
    c.align_x = align
    ob = bpy.data.objects.new('label', c)
    ob.location = loc
    ob.rotation_euler = Euler((math.pi / 2, 0, 0))
    ob.data.materials.append(material('label_black', (.05, .05, .05)))
    bpy.context.scene.collection.objects.link(ob)
    return ob


def add_pivot(parent, pivot_glb, H):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=.012 * H, location=tuple(to_bl(pivot_glb)), segments=16, ring_count=8)
    ob = bpy.context.active_object
    ob.name = 'waist_pivot'
    ob.data.materials.append(material('pivot_red', (.95, .05, .05), emit=True))
    ob.parent = parent
    ob.matrix_parent_inverse.identity()
    return ob


def build_pieces_item(tag, loc, pieces, pivot, H, rot_z=0., bend=0., explode=False):
    """One group of pieces: `bend` degrees forward about the waist pivot for the spine movers, `explode` = split_pieces offsets."""
    root = add_empty(f'{tag}', loc, rot_z)
    R = sp._rot('x', math.radians(bend)) if bend else None
    for n, (v, f) in pieces.items():
        v = np.asarray(v, float)
        if R is not None and n in SPINE_MOVERS:
            v = (v - pivot) @ R.T + pivot
        if explode:
            dx, dy = sp.explode_shift(n, H, True)
            v = v + np.array([dx, dy, 0.])
        add_mesh(f'{tag}_{n}', to_bl(v), f, material(f'piece_{n}', COLORS[n]), root)
    add_pivot(root, pivot, H)
    return root


def import_source(path, loc):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(path))
    new = [o for o in bpy.data.objects if o not in before]
    root = add_empty('source_item', loc)
    for o in new:
        if o.parent is None:
            o.parent = root
            o.matrix_parent_inverse.identity()
    return root, new


def build(out_blend, png, level='0p4'):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.unit_settings.system = 'METRIC'
    notes = {}
    for ri, (key, title, concept) in enumerate(ROWS):
        z = -ri * ROW_PITCH
        pc = EXP / 'piece_colours_20261022' / concept
        sf = EXP / 'segment_first_fringe_20261022' / concept
        sn = EXP / 'segment_first_seam_shelf_20261022' / concept
        reg = load_pieces(pc / 'pieces_nofringe' / f'lod_{level}')
        shelf_dir = pc / 'pieces_shelf' / f'lod_{level}'
        before_dir, after_dir = sf / 'before' / 'pieces' / f'lod_{level}', sn / 'pieces' / f'lod_{level}'
        shelf, before, after = load_pieces(shelf_dir), load_pieces(before_dir), load_pieces(after_dir)
        pivot_reg, H = piece_info(pc / 'pieces_nofringe' / f'lod_{level}')
        pivot_s, _ = piece_info(shelf_dir)
        pivot_b, _ = piece_info(before_dir)
        pivot_a, _ = piece_info(after_dir)
        cj = json.loads((after_dir.parent / 'pieces.json').read_text()).get('cross_joint') or {}
        agg = {}
        for r in cj.get('reassigned', []):
            agg[(r['from'], r['to'])] = agg.get((r['from'], r['to']), 0) + r['faces']
        moved = 'shelf: ' + ', '.join(f'{a}->{b} {n} hp faces' for (a, b), n in agg.items()) if agg else 'shelf: nothing moved'
        add_text(title, (-.55, 0, z + 1.14), .08, 'LEFT')
        root, objs = import_source(pc / 'tripo' / 'source.glb', (COL_X[0], 0, z))
        add_text('Tripo high-poly\n(textured source)', (COL_X[0], 0, z - .1))
        build_pieces_item(f'{key}_regions', (COL_X[1], 0, z), reg, pivot_reg, H)
        add_text(f'OLD per-face regions\n{level} LOD', (COL_X[1], 0, z - .1))
        build_pieces_item(f'{key}_regions_shelf', (COL_X[2], 0, z), shelf, pivot_s, H)
        add_text('NEW per-face regions\nshelf rule + spike drop', (COL_X[2], 0, z - .1))
        build_pieces_item(f'{key}_segfirst', (COL_X[3], 0, z), before, pivot_b, H)
        add_text(f'OLD segment-first\n{level} LOD', (COL_X[3], 0, z - .1))
        build_pieces_item(f'{key}_segfirst_new', (COL_X[4], 0, z), after, pivot_a, H)
        add_text(f'NEW segment-first: seam fix\n+ {moved}', (COL_X[4], 0, z - .1))
        build_pieces_item(f'{key}_segfirst_new_exploded', (COL_X[5], 0, z), after, pivot_a, H, explode=True)
        add_text('NEW segment-first\nexploded', (COL_X[5], 0, z - .1))
        for ci, (tag, pcs, pv, label) in enumerate((('segfirst_old', before, pivot_b, 'spine +20 deg\nOLD segment-first'),
                                                    ('segfirst_new', after, pivot_a, 'spine +20 deg\nNEW segment-first'),
                                                    ('regions_new', shelf, pivot_s, 'spine +20 deg\nNEW per-face regions'))):
            x = COL_X[6 + ci]
            build_pieces_item(f'{key}_spine20_{tag}', (x, 0, z), pcs, pv, H, rot_z=90., bend=20)
            add_text(label + '\n(side view)', (x, 0, z - .1))
            add_text('front ->', (x, 0, z + 1.06), .05)
        notes[key] = {'concept': concept, 'level': level, 'height': H, 'waist_pivot_glb': pivot_a.tolist(), 'fringe': cj.get('reassigned')}
    # camera + viewport
    width = COL_X[-1] + .6 + .6
    height = (len(ROWS) - 1) * ROW_PITCH + 1.4
    cx, cz = (COL_X[0] - .6 + COL_X[-1] + .6) / 2, -(len(ROWS) - 1) * ROW_PITCH / 2 + .5
    cam = bpy.data.cameras.new('gallery_front_ortho')
    cam.type = 'ORTHO'
    aspect = width / height
    cam.ortho_scale = max(width, height * aspect) * 1.02
    cam.clip_end = 100
    co = bpy.data.objects.new('gallery_front_ortho', cam)
    co.location = (cx, -30, cz)
    co.rotation_euler = Euler((math.pi / 2, 0, 0))
    sc.collection.objects.link(co)
    sc.camera = co
    sc.render.resolution_x = 4200
    sc.render.resolution_y = int(4200 / aspect)
    world = bpy.data.worlds.new('gallery_world')
    world.color = (.86, .86, .87)                      # Workbench reads world.color, the node tree is for Material Preview/EEVEE
    world.use_nodes = True
    bg = world.node_tree.nodes.get('Background')
    bg.inputs['Color'].default_value = (.86, .86, .87, 1)
    sc.world = world
    sc.render.engine = 'BLENDER_EEVEE'                 # Material Preview needs a non-Workbench engine while the viewport is set up
    sh = sc.display.shading
    sh.light = 'STUDIO'
    sh.color_type = 'TEXTURE'
    sh.show_cavity = False
    sc.display.render_aa = '8'
    sc.render.film_transparent = False
    for scr in bpy.data.screens:
        for area in scr.areas:
            if area.type == 'VIEW_3D':
                sp3 = area.spaces[0]
                try:
                    sp3.shading.type = 'MATERIAL'
                except TypeError:
                    pass
                r3 = sp3.region_3d
                r3.view_perspective = 'ORTHO'
                r3.view_rotation = Euler((math.pi / 2, 0, 0)).to_quaternion()
                r3.view_location = Vector((cx, 0, cz))
                r3.view_distance = max(width, height) * 1.1
                sp3.clip_end = 200
    sc.render.engine = 'BLENDER_WORKBENCH'              # the overview PNG is a Workbench render (fast, textured)
    png_path = unique(png) if png else None
    if png_path:
        sc.render.filepath = str(png_path)
        sc.render.image_settings.file_format = 'PNG'
        bpy.ops.render.render(write_still=True)
    bpy.ops.file.pack_all()
    blend_path = unique(out_blend)
    bpy.ops.wm.save_as_mainfile(filepath=str(blend_path), compress=True)
    (blend_path.with_suffix('.json')).write_text(json.dumps({'blend': str(blend_path), 'overview_png': str(png_path) if png_path else None, 'rows': notes}, indent=2, default=float))
    unpacked = [i.name for i in bpy.data.images if i.source == 'FILE' and not i.packed_file]
    print('GALLERY_SAVED', blend_path, 'png', png_path, 'unpacked images', unpacked, flush=True)


def main(argv):
    out = argv[0]
    png = argv[argv.index('--png') + 1] if '--png' in argv else str(Path(out).with_suffix('.png'))
    if '--no-png' in argv:
        png = None
    level = argv[argv.index('--levels') + 1] if '--levels' in argv else '0p4'
    build(out, png, level)


if __name__ == '__main__':
    main(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else sys.argv[1:])
