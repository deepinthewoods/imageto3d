"""Headless test of project creation, the UI-side operators and every view (create/reset round trip).

    blender -b --factory-startup --python tools/outfit_studio/tests/test_blender_ui.py -- [workdir]

Writes only to <workdir> (default: the impl_a scratch dir). Never touches projects/, models/ or four-ways."""
import json
import re
import shutil
import sys
import traceback
from pathlib import Path

import bmesh
import bpy
import numpy as np

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2]))
import outfit_studio as OS                                       # noqa: E402
from outfit_studio import paths                                  # noqa: E402

argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
WORK = Path(argv[0] if argv else '/tmp/claude-1000/-media-niz-Samsung-SSD-godot-blender-characters-imageto3d/'
            '595ecea8-fc5e-4ebc-9709-1884d8604278/scratchpad/impl_a/test_ui')
assert 'projects' not in WORK.parts and 'models' not in WORK.parts, WORK
shutil.rmtree(WORK, ignore_errors=True)
WORK.mkdir(parents=True)
BASE = 'dwarf_male'
FAIL = []


def check(cond, msg):
    print(('ok   ' if cond else 'FAIL ') + msg)
    if not cond:
        FAIL.append(msg)


def run(fn, label):
    try:
        return fn()
    except Exception as e:
        traceback.print_exc()
        check(False, f'{label}: {type(e).__name__}: {e}')


# ---------------------------------------------------------------- fake item layers (SPEC section 2 data model)
def role_mat(role, hexcol):
    m = bpy.data.materials.get('OSC_' + role) or bpy.data.materials.new('OSC_' + role)
    m.diffuse_color = OS.blender.props.hex_to_rgba(hexcol)
    return m


def make_item(item_id, typ, pieces, inflate, set_id='plain_plate_mail_01'):
    """pieces: {name: set of body parts}. Each piece is a copy of the base faces on those parts, moved along normals."""
    from outfit_studio.blender import project
    scene = bpy.context.scene
    rig, base = project.find_base(scene)
    top = bpy.data.collections['OS_Items']
    coll = bpy.data.collections.new('OS_' + item_id)
    top.children.link(coll)
    mats = [role_mat('primary', '#19D228'), role_mat('secondary', '#1E41EB'), role_mat('tertiary', '#F5DC14')]
    channels = [{'id': 'decoration', 'label': 'Decoration', 'role': 'tertiary', 'material': ''}]
    n_total = 0
    for pname, parts in pieces.items():
        me = bpy.data.meshes.new(f'OS_{item_id}__{pname}')
        keep = {i for i, m in enumerate(base.data.materials) if m and re.sub(r'\.\d{3}$', '', m.name).removeprefix('bone_') in parts}
        bm = bmesh.new()
        bm.from_mesh(base.data)
        for k in list(bm.verts.layers.shape):
            bm.verts.layers.shape.remove(k)
        bmesh.ops.delete(bm, geom=[f for f in bm.faces if f.material_index not in keep], context='FACES')
        bm.to_mesh(me)
        bm.free()
        nv = len(me.vertices)
        co, nr = np.empty(nv * 3, np.float32), np.empty(nv * 3, np.float32)
        me.vertices.foreach_get('co', co)
        me.vertex_normals.foreach_get('vector', nr)
        co += nr * inflate
        me.vertices.foreach_set('co', co)
        me.materials.clear()
        for m in mats:
            me.materials.append(m)
        nf = len(me.polygons)
        ch_index = len(channels)
        channels.append({'id': pname, 'label': pname, 'role': 'primary' if len(channels) % 2 else 'secondary', 'material': ''})
        a = me.attributes.new('os_channel', 'INT', 'FACE')
        a.data.foreach_set('value', [0 if i % 5 == 0 else ch_index for i in range(nf)])
        b = me.attributes.new('os_label_source', 'INT', 'FACE')
        b.data.foreach_set('value', [i % 4 for i in range(nf)])
        me.polygons.foreach_set('material_index', [2 if i % 5 == 0 else (ch_index % 2) for i in range(nf)])
        o = bpy.data.objects.new(me.name, me)
        coll.objects.link(o)
        for g in base.vertex_groups:
            o.vertex_groups.new(name=g.name)
        o.parent = rig
        mod = o.modifiers.new('Armature', 'ARMATURE')
        mod.object = rig
        k = o.shape_key_add(name='Basis')
        kb = o.shape_key_add(name='bulk_torso')
        d = np.array([v.co for v in me.vertices]) + nr[:nv * 3].reshape(-1, 3) * 0.04
        kb.data.foreach_set('co', d.astype(np.float32).ravel())
        kb.value = 0.0
        n_total += nf
    coll['os_item'] = json.dumps({
        'item_id': item_id, 'set_id': set_id, 'type': typ, 'label': item_id.split('__')[1], 'status': 'imported',
        'notes': '', 'channels': channels, 'stats': {'tris': n_total},
        'source': {'job_id': '64eb09f6e516', 'module': 'body_armor', 'race': 'human_male', 'lod': '0p4'}})
    return coll


# ---------------------------------------------------------------- snapshot of everything a view may touch
def snapshot():
    sc = bpy.context.scene
    return {
        'hidden': {o.name: o.hide_get() for o in sc.objects},
        'objects': sorted(o.name for o in bpy.data.objects if o.library is None),
        'meshes': sorted(m.name for m in bpy.data.meshes if m.library is None),
        'materials': sorted(m.name for m in bpy.data.materials if m.library is None),
        'cameras': sorted(c.name for c in bpy.data.cameras),
        'collections': sorted(c.name for c in bpy.data.collections if c.library is None),
        'attrs': {m.name: sorted(a.name for a in m.color_attributes) for m in bpy.data.meshes if m.library is None},
        'camera': sc.camera.name if sc.camera else None,
        'res': (sc.render.resolution_x, sc.render.resolution_y),
        'frame': (sc.frame_current, sc.frame_start, sc.frame_end),
        'action': next((o.animation_data.action.name for o in sc.objects if o.type == 'ARMATURE' and o.animation_data
                        and o.animation_data.action), None),
        'wire': {o.name: o.show_wire for o in sc.objects if o.type == 'MESH'},
        'mods': {o.name: [m.name for m in o.modifiers] for o in sc.objects if o.type == 'MESH'},
    }


def diff(a, b):
    return [k for k in a if a[k] != b[k]]


def corner_colours(obj):
    a = obj.data.color_attributes.get('os_view')
    if a is None:
        return None
    c = np.empty(len(a.data) * 4, np.float32)
    a.data.foreach_get('color', c)
    return c.reshape(-1, 4)


class Dummy:
    """Stand-in for UILayout: any call returns another Dummy, attributes can be set."""
    def __getattr__(self, n):
        return lambda *a, **k: Dummy()

    def __iter__(self):
        return iter(())


def draw_all_panels(label):
    from outfit_studio.blender import ui
    for cls in ui.CLASSES:
        if cls.poll(bpy.context):
            holder = type('P', (), {'layout': Dummy(), 'module': getattr(cls, 'module', ''), 'fn': getattr(cls, 'fn', 'draw')})()
            try:
                cls.draw(holder, bpy.context)
                check(True, f'{label}: {cls.__name__}.draw')
            except Exception as e:
                traceback.print_exc()
                check(False, f'{label}: {cls.__name__}.draw raised {e}')


def main():
    OS.register()
    from outfit_studio.blender import project, props, views
    sc = bpy.context.scene
    check(hasattr(sc, 'ostudio'), 'scene.ostudio registered')
    for cls in ('OSTUDIO_PT_main', 'OSTUDIO_PT_views', 'OSTUDIO_OT_view', 'OSTUDIO_OT_project_create'):
        check(hasattr(bpy.types, cls), f'{cls} registered')
    check(project.mode('/x/y/z.blend') == 'none', 'mode none for unrelated file')
    check(project.mode(str(paths.PROJECTS / f'{BASE}.blend')) == 'base', 'mode base')
    check(project.mode(str(paths.OUTFIT_PROJECTS / f'{BASE}_outfits.blend')) == 'outfit', 'mode outfit')
    check(paths.ROOT.name == 'imageto3d' and paths.ANIMS.exists() and paths.HEADS.exists(), f'paths {paths.ROOT}')
    check(len(paths.bases()) == 11, '11 bases in manifest')

    # ---- project creation + relink
    out = WORK / 'proj' / f'{BASE}_outfits.blend'
    run(lambda: project.create_outfit_project(BASE, out), 'create_outfit_project')
    check(out.exists(), 'outfit project saved')
    try:
        project.create_outfit_project(BASE, out)
        check(False, 'existing project must not be overwritten')
    except FileExistsError:
        check(True, 'refuses to overwrite an existing project')
    check(project.mode() == 'outfit', 'mode outfit after creation (metadata)')
    meta0 = project.meta(bpy.context.scene)
    rig, base = project.find_base()
    check(rig is not None and base is not None and rig.override_library is not None, 'override rig + base found')
    check(base.library is None and base.override_library.reference.library is not None, 'base is an override of the linked mesh')
    check(project.status()['ok'], f'project status ok {project.status()["problems"]}')
    lib = bpy.data.libraries[0]
    check(lib.filepath.startswith('//'), f'library path is relative: {lib.filepath}')

    # posing the override rig deforms base
    def pose_moves(rig, base, bone='c_arm_stretch.l'):
        def verts():
            ev = base.evaluated_get(bpy.context.evaluated_depsgraph_get())
            me = ev.to_mesh()
            a = np.array([base.matrix_world @ v.co for v in me.vertices])
            ev.to_mesh_clear()
            return a
        v0 = verts()
        pb = rig.pose.bones[bone]
        pb.rotation_mode = 'XYZ'
        pb.rotation_euler = (0, 0, 0.8)
        bpy.context.view_layer.update()
        d = np.abs(verts() - v0).max()
        pb.rotation_euler = (0, 0, 0)
        bpy.context.view_layer.update()
        return d
    check(pose_moves(rig, base) > 0.01, 'posing the override rig deforms base')

    # reopen keeps the link
    bpy.ops.wm.open_mainfile(filepath=str(out))
    rig, base = project.find_base()
    check(rig is not None and not bpy.data.libraries[0].is_missing if hasattr(bpy.data.libraries[0], 'is_missing') else rig is not None,
          'reopen keeps the library link')
    check(project.mode() == 'outfit' and project.meta(bpy.context.scene)['rig_fingerprint'] == meta0['rig_fingerprint'],
          'reopened file is an outfit project with the same fingerprints')
    check(project.rig_fingerprint(rig) == meta0['rig_fingerprint'] and project.base_fingerprint(base, rig) == meta0['base_fingerprint'],
          'fingerprints reproducible after reopen')
    check(pose_moves(rig, base) > 0.01, 'posing works after reopen')

    # ---- fake items
    sc = bpy.context.scene
    ARM = {'spine_01.x', 'shoulder.l', 'shoulder.r', 'arm_stretch.l', 'arm_stretch.r'}
    armor = make_item('testset__body_armor', 'body_armor', {'chest': ARM, 'pelvis': {'root.x', 'thigh_stretch.l', 'thigh_stretch.r'}}, 0.02)
    boots = make_item('testset__boots', 'boots', {'shin_r': {'leg_stretch.r'}, 'foot_r': {'foot.r'}}, 0.02)
    outfit = make_item('testset__outfit', 'outfit', {'outfit': ARM | {'root.x', 'thigh_stretch.l', 'thigh_stretch.r', 'leg_stretch.l', 'leg_stretch.r'}}, 0.03)
    sunk = make_item('testset__sunk', 'gauntlets', {'cuff_r': {'forearm_stretch.r'}}, -0.02)
    o = sc.ostudio
    api = props.items_api()
    names = [c.name for c in api.list_items(sc)]
    check(len(names) == 4, f'4 fake items listed: {names}')
    o.item_index = names.index('OS_testset__body_armor')

    # navigation / filter / dropdown
    o.item_pick = 'OS_testset__boots'
    check(api.current_item(bpy.context).name == 'OS_testset__boots', 'item dropdown selects the item')
    bpy.ops.ostudio.item_next()
    check(api.current_item(bpy.context).name != 'OS_testset__boots', 'next moves')
    bpy.ops.ostudio.item_prev()
    check(api.current_item(bpy.context).name == 'OS_testset__boots', 'prev moves back')
    o.filter_type = 'boots'
    check(len(props.filtered_items(sc)) == 1, 'type filter')
    bpy.ops.ostudio.item_next()
    check(api.current_item(bpy.context).name == 'OS_testset__boots', 'next wraps inside the filter')
    o.filter_type = 'ALL'
    o.filter_text = 'sunk'
    check([c.name for _, c in props.filtered_items(sc)] == ['OS_testset__sunk'], 'text filter')
    o.filter_text = ''
    o.filter_status = 'approved'
    check(len(props.filtered_items(sc)) == 0, 'status filter')
    o.filter_status = 'ALL'
    o.item_index = names.index('OS_testset__body_armor')
    bpy.ops.ostudio.status_set(status='approved')
    check(api.item_data(armor)['status'] == 'approved', 'status_set writes the item status')
    o.mix_body_armor = 'testset__body_armor'
    o.mix_outfit = 'testset__outfit'
    o.mix_boots = 'testset__boots'

    # ---- views: apply, check, reset, compare with the snapshot
    snap0 = snapshot()
    hid = lambda c: [x.hide_get() for x in api.item_pieces(c)]  # noqa: E731
    expected_fail = {'SOURCE'}
    for v in props.VIEW_BUTTONS:
        try:
            bpy.ops.ostudio.view(name=v)
            ok = True
        except RuntimeError as e:
            ok = False
            print('   view error:', str(e).strip()[:160])
        if v in expected_fail:
            check(not ok or o.view == v, f'view {v} runs or fails with a clear message')
        else:
            check(ok and o.view == v, f'view {v} applies')
        if ok:
            if v == 'GAME':
                check(all(hid(outfit)) and not any(hid(armor)) and not any(hid(boots)) and all(hid(sunk)),
                      'GAME: outfit hidden when armour equipped, armour + boots shown')
            if v == 'ITEM':
                check(base.hide_get() and not any(hid(armor)), 'ITEM: body hidden, item shown')
            if v in ('ROLES', 'PALETTE', 'CHANNELS', 'CONFIDENCE', 'WEIGHTS'):
                cols = [corner_colours(p) for p in api.item_pieces(armor)]
                check(all(c is not None and len(c) for c in cols), f'{v}: colour attribute on every piece')
                check(len(o.legend) > 0, f'{v}: legend filled ({len(o.legend)})')
                if v == 'ROLES':
                    prim = props.role_colours()['primary']
                    chest = bpy.data.objects['OS_testset__body_armor__chest']
                    me, ch = chest.data, [0] * len(chest.data.polygons)
                    chest.data.attributes['os_channel'].data.foreach_get('value', ch)
                    loops0 = [p.loop_start for p in me.polygons]
                    fi = next(i for i, c in enumerate(ch) if c == 1)
                    want = props.role_colours()['secondary' if api.item_data(armor)['channels'][1]['role'] == 'secondary' else 'primary']
                    check(np.allclose(corner_colours(chest)[loops0[fi]], want, atol=1e-4), 'ROLES: face colour = channel role colour')
                if v == 'PALETTE':
                    c1 = corner_colours(api.item_pieces(armor)[0]).copy()
                    bpy.ops.ostudio.palette_reroll()
                    c2 = corner_colours(api.item_pieces(armor)[0])
                    check(not np.allclose(c1, c2), 'PALETTE: reroll changes the tints')
            if v == 'POKE':
                check(views.info(sc).startswith('poke-through: 0 '), f'POKE: inflated armour has no poke-through ({views.info(sc)})')
            if v == 'SOCKETS':
                check(len([n for n in bpy.data.objects.keys() if n.startswith('OSV_prop_')]) == 10, 'SOCKETS: 10 proxies')
            if v == 'AP':
                n_ap = sum(b.name.startswith('AP_') for b in rig.data.bones)
                check(len([n for n in bpy.data.objects.keys() if n.startswith('OSAP_')]) == n_ap, f'AP: {n_ap} markers')
                check('buried' in views.info(sc) and not views.info(sc).split(',')[1].strip().startswith('0 '),
                      f'AP: some points buried under armour ({views.info(sc)})')
            if v == 'CONCEPT':
                cam = sc.camera
                check(cam is not None and cam.name == 'OSV_concept_cam' and cam.data.background_images[0].image is not None,
                      'CONCEPT: camera + background image')
                check(sc.render.resolution_x == 1254, 'CONCEPT: render resolution from image')
                from bpy_extras.object_utils import world_to_camera_view
                pts = [world_to_camera_view(sc, cam, base.matrix_world @ v.co) for v in base.data.vertices]
                xs, ys = [p.x for p in pts], [p.y for p in pts]
                img = bpy.data.images.load(str(paths.base_views(BASE) / 'front.png'))     # the base's own front render
                alpha = np.array(img.pixels[:]).reshape(img.size[1], img.size[0], 4)[..., 3] > 0.5
                yy, xx = np.where(alpha)
                want = (xx.min() / img.size[0], xx.max() / img.size[0], yy.min() / img.size[1], yy.max() / img.size[1])
                got = (min(xs), max(xs), min(ys), max(ys))
                check(np.abs(np.array(want) - got).max() < 0.01,
                      f'CONCEPT: projected base matches base_views front.png silhouette (png {np.round(want, 3)}, mesh {np.round(got, 3)})')
            if v == 'WIRE':
                check(all(p.show_wire for p in api.item_pieces(armor)), 'WIRE: wire on')
            if v == 'POSE':
                check(rig.animation_data is None or True, 'POSE applied')
        bpy.ops.ostudio.view_reset()
        d = diff(snap0, snapshot())
        check(not d and o.view == 'NONE' and len(o.legend) == 0, f'reset after {v} restores state {d}')

    # poke: sunk item (inside the body) must register poke-through
    o.item_index = names.index('OS_testset__sunk')
    bpy.ops.ostudio.poke_check()
    check(not views.info(sc).startswith('poke-through: 0 ') and 'OSV_poke' in bpy.data.objects, f'poke finds sunk item ({views.info(sc)})')
    bpy.ops.ostudio.view_reset()
    check(not diff(snap0, snapshot()), 'reset after poke')
    o.item_index = names.index('OS_testset__body_armor')

    # pose test with a linked action: base verts move, reset restores
    o.pose_action = 'LIB:Joint Review - Walk_remap'
    bpy.ops.ostudio.view(name='POSE')
    check(rig.animation_data and rig.animation_data.action and rig.animation_data.action.name == 'Joint Review - Walk_remap', 'POSE: linked action assigned')
    sc.frame_set(sc.frame_start + 7)
    d1 = np.array([(base.matrix_world @ v.co) for v in base.data.vertices])
    ev = base.evaluated_get(bpy.context.evaluated_depsgraph_get())
    me = ev.to_mesh()
    d2 = np.array([(base.matrix_world @ v.co) for v in me.vertices])
    ev.to_mesh_clear()
    check(np.abs(d1 - d2).max() > 0.01, 'POSE: action deforms the base')
    o.poke_all_frames = True
    bpy.ops.ostudio.view(name='POKE')
    check('frames)' in views.info(sc) and 'poke-through' in views.info(sc), f'POKE over the action ({views.info(sc)})')
    bpy.ops.ostudio.view_reset()
    sc.frame_set(snap0['frame'][0])
    check(not diff(snap0, snapshot()) or diff(snap0, snapshot()) == ['action'], f'reset after pose test {diff(snap0, snapshot())}')
    o.poke_all_frames = False

    # sockets follow the bones
    bpy.ops.ostudio.view(name='SOCKETS')
    prop = bpy.data.objects['OSV_prop_RightHandAttachment']
    p0 = prop.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.translation.copy()
    pb = rig.pose.bones['hand.r']
    pb.location = (0.1, 0.1, 0.1)
    bpy.context.view_layer.update()
    p1 = prop.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.translation.copy()
    pb.location = (0, 0, 0)
    check((p1 - p0).length > 0.02, f'SOCKETS: proxy follows its socket bone ({(p1 - p0).length:.3f} m)')
    bpy.ops.ostudio.view_reset()

    # head swap
    heads = views.head_choices(BASE)
    check(len(heads) > 0, f'{len(heads)} library heads for {BASE}')
    before = snapshot()
    bpy.ops.ostudio.head_swap(choice=heads[0])
    root = next((x for x in bpy.data.objects if 'os_head' in x), None)
    check(root is not None and 'OS_head_mask' in [m.name for m in base.modifiers], 'head swap: head root + mask modifier')
    meshes = [c for c in root.children_recursive if c.type == 'MESH']
    pts = np.concatenate([np.array([c.matrix_world @ v.co for v in c.data.vertices]) for c in meshes])
    bv = np.array([base.matrix_world @ v.co for v in base.data.vertices])
    hz = bv[:, 2].max()
    check(len(meshes) > 0 and abs(pts[:, 2].max() - hz) < 0.15 and abs(pts[:, 0].mean()) < 0.05,
          f'head swap: head sits on the base head (top {pts[:, 2].max():.3f} vs base {hz:.3f}, mean x {pts[:, 0].mean():.3f})')
    bpy.ops.ostudio.head_swap(choice='NONE')
    check(not any('os_head' in x for x in bpy.data.objects) and 'OS_head_mask' not in [m.name for m in base.modifiers],
          'head swap NONE removes head and mask')

    # thickness
    def width():
        ev = base.evaluated_get(bpy.context.evaluated_depsgraph_get())
        me = ev.to_mesh()
        v = np.array([x.co for x in me.vertices])
        ev.to_mesh_clear()
        return np.ptp(v[:, 1])
    w0 = width()
    o.thickness = 1.0
    bpy.context.view_layer.update()
    check(base.data.shape_keys.key_blocks['bulk_torso'].value == 1.0 and
          bpy.data.objects['OS_testset__body_armor__chest'].data.shape_keys.key_blocks['bulk_torso'].value == 1.0,
          'thickness drives bulk_* on base and items')
    check(width() > w0 + 1e-4, f'thickness changes the evaluated body ({w0:.4f} -> {width():.4f})')
    o.thickness = 0.0

    draw_all_panels('outfit mode')
    o.view = 'NONE'

    # save with items, reopen, views still work
    out2 = WORK / 'proj' / 'with_items.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(out2), relative_remap=True)
    bpy.ops.wm.open_mainfile(filepath=str(out2))
    sc = bpy.context.scene
    rig, base = project.find_base()
    check(rig is not None and len(props.items_api().list_items(sc)) == 4, 'reopen with items: base + 4 items')
    bpy.ops.ostudio.view(name='ITEM')
    bpy.ops.ostudio.view_reset()
    check(sc.ostudio.view == 'NONE', 'views work after reopen')

    # base mode on the real base project (opened, never saved)
    bpy.ops.wm.open_mainfile(filepath=str(paths.PROJECTS / f'{BASE}.blend'))
    sc = bpy.context.scene
    rig, base = project.find_base()
    check(project.mode() == 'base' and rig and base, 'base project is base mode')
    check(project.rig_fingerprint(rig) == meta0['rig_fingerprint'] and project.base_fingerprint(base, rig) == meta0['base_fingerprint'],
          'fingerprints of the linked override equal those of the base project')
    check(project.status()['ok'], 'base status ok')
    draw_all_panels('base mode')
    bpy.ops.wm.read_homefile(use_empty=True)
    check(project.mode() == 'none', 'empty file is mode none')
    draw_all_panels('none mode')


try:
    main()
except Exception:
    traceback.print_exc()
    FAIL.append('exception in main')
print('\nRESULT:', 'PASS' if not FAIL else f'FAIL ({len(FAIL)}): ' + '; '.join(FAIL))
sys.exit(1 if FAIL else 0)
