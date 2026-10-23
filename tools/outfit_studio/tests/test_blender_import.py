"""Headless import tests (Blender 5.1):

    blender -b --factory-startup --python tools/outfit_studio/tests/test_blender_import.py [-- quick]

Opens projects/<base>.blend in memory only (never saved; the files' mtimes are asserted unchanged), imports the converted
plain plate mail onto it and checks pieces, weights, mirroring, morph keys, distance to the body, hand-edit protection,
stale rigs and the outfit / pieces-output paths. Scratch files go to $OSTUDIO_SCRATCH or a temp dir. Exit code 1 on failure."""
import os
import sys
import tempfile
import traceback
from pathlib import Path

import bpy
import numpy as np
from mathutils.bvhtree import BVHTree

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'tools'))
SCRATCH = Path(os.environ.get('OSTUDIO_SCRATCH') or tempfile.mkdtemp(prefix='ostudio_'))
SCRATCH.mkdir(parents=True, exist_ok=True)
QUICK = 'quick' in sys.argv

from outfit_studio import paths  # noqa: E402
from outfit_studio.blender import importer, items, morphs, weights  # noqa: E402
from outfit_studio.core import pipeline_source as ps  # noqa: E402

RESULTS, TABLE = [], []


def check(name, ok, detail=''):
    RESULTS.append((bool(ok), name, detail))
    print(('PASS ' if ok else 'FAIL ') + name + (f'  [{detail}]' if detail else ''), flush=True)
    return bool(ok)


def open_base(base):
    bpy.ops.wm.open_mainfile(filepath=str(paths.base_project(base)))
    return bpy.context


def entries(race):
    return {e['module']: e for e in ps.latest(ps.discover()) if e['race'] == race}


def body_bvh():
    rig, base = importer.base_objects(bpy.context)
    return BVHTree.FromObject(base, bpy.context.evaluated_depsgraph_get()), importer.rest_context(rig, base)


def distances(coll, bvh):
    d = []
    for o in items.item_pieces(coll):
        for p in importer.piece_vertices(o):
            d.append(bvh.find_nearest(tuple(p))[3])
    return np.array(d)


def weight_summary(obj):
    names, W = weights.vertex_weights(obj)
    return names, W


def check_rigid(coll, rig):
    d = items.item_data(coll)
    ok, bad = True, []
    for o in items.item_pieces(coll):
        piece = o['os_piece']
        names, W = weight_summary(o)
        expect = weights.rigid_bones(piece)
        used = {n for j, n in enumerate(names) if (W[:, j] > 0).any()}
        good = set(expect) == used and np.allclose(W.sum(1), 1) and all(
            np.allclose(W[:, names.index(b)], w) for b, w in expect.items())
        mod = next((m for m in o.modifiers if m.type == 'ARMATURE'), None)
        good &= mod is not None and mod.object == rig and o.parent == rig and used <= weights.deform_bones(rig)
        if not good:
            bad.append((piece, sorted(used)))
        ok &= good
    return ok, bad


def check_pair_symmetry(coll, plane_x):
    worst = 0.
    for o in items.item_pieces(coll):
        piece = o['os_piece']
        if not piece.endswith('_r'):
            continue
        twin = bpy.data.objects[o.name[:-1] + 'l']
        R = importer.piece_vertices(o)
        L = importer.piece_vertices(twin)
        R[:, 0] = 2 * plane_x - R[:, 0]
        d = np.linalg.norm(R[:, None] - L[None], axis=2)
        worst = max(worst, d.min(1).max(), d.min(0).max())
    return worst


def normal_agreement(coll, bvh):
    """Share of faces whose normal agrees (dot > 0) with the normal of the nearest body surface point, per side."""
    out = {}
    for o in items.item_pieces(coll):
        for p in o.data.polygons:
            c = o.matrix_world @ p.center
            loc, n, _, _ = bvh.find_nearest(c)
            out.setdefault(o['os_piece'][-2:], []).append((o.matrix_world.to_3x3() @ p.normal).dot(n) > 0)
    return {k: float(np.mean(v)) for k, v in out.items()}


def shape_keys(obj):
    return [k.name for k in obj.data.shape_keys.key_blocks][1:] if obj.data.shape_keys else []


# ------------------------------------------------------------------------------------------------- scenarios

def import_all(ctx, race, lod='0p4'):
    reports = []
    for module in ('body_armor', 'right_gauntlet_boot'):
        reports += importer.import_module(ctx, entries(race)[module], lod)
    return reports


_registered = []


def ensure_registered():
    """Register the whole add-on once (props, items, importer, ...), as Blender does at start-up."""
    if not _registered:
        import outfit_studio
        outfit_studio.register()
        _registered.append(True)


def scenario_operators():
    ensure_registered()
    ctx = open_base('dwarf_male')
    check('scene.ostudio exists', hasattr(ctx.scene, 'ostudio'))
    check('import_pipeline all_new imports every module', bpy.ops.ostudio.import_pipeline('EXEC_DEFAULT', all_new=True, lod='0p2') == {'FINISHED'}
          and len(items.list_items(ctx.scene)) == 3 and {items.item_data(c)['source']['lod'] for c in items.list_items(ctx.scene)} == {'0p2'})
    check('all_new again has nothing to do', bpy.ops.ostudio.import_pipeline('EXEC_DEFAULT', all_new=True) == {'CANCELLED'})
    ctx.scene.ostudio.item_index = 1
    cur = items.current_item(ctx)
    check('reimport at another LOD (item_index selects the item)', bpy.ops.ostudio.reimport('EXEC_DEFAULT', lod='0p1') == {'FINISHED'}
          and items.item_data(cur)['source']['lod'] == '0p1' and items.item_data(cur)['type'] == 'boots')
    jobs = {e['job_id'] for e in ps.discover() if e['race'] == 'dwarf_male'}
    check('import_pipeline with explicit set / job / module', bpy.ops.ostudio.import_pipeline(
        'EXEC_DEFAULT', set_id='plain_plate_mail_01', job_id=sorted(jobs)[0], module='body_armor', lod='0p4') == {'FINISHED'})


def scenario_dwarf_male():
    snap = paths.base_project('dwarf_male').stat().st_mtime_ns
    ctx = open_base('dwarf_male')
    rig, base = importer.base_objects(ctx)
    bvh, rest = body_bvh()
    check('base_objects finds rig and base', rig.type == 'ARMATURE' and base.name == 'base')
    check('rest mesh symmetric plane at x=0', abs(rest['plane_x']) < 1e-9, f"plane {rest['plane_x']}")

    # pose handling: the rest mesh is independent of the pose and the pose position is restored
    rig.data.pose_position = 'POSE'
    pb = rig.pose.bones['c_arm_stretch.l']
    pb.rotation_mode = 'XYZ'
    pb.rotation_euler = (0.5, 0.2, 0.3)
    bpy.context.view_layer.update()
    V2 = importer.base_rest_mesh(base, rig)[0]
    check('rest mesh ignores the pose', np.abs(V2 - rest['V']).max() < 1e-6)
    check('pose position restored', rig.data.pose_position == 'POSE')
    pb.rotation_euler = (0, 0, 0)

    reports = import_all(ctx, 'dwarf_male')
    ids = sorted(r['item_id'] for r in reports)
    check('items created', ids == ['plain_plate_mail_01__body_armor', 'plain_plate_mail_01__boots', 'plain_plate_mail_01__gauntlets'], str(ids))
    armour = items.find_item(ctx.scene, 'plain_plate_mail_01__body_armor')
    gaunt = items.find_item(ctx.scene, 'plain_plate_mail_01__gauntlets')
    boots = items.find_item(ctx.scene, 'plain_plate_mail_01__boots')
    pieces = lambda c: sorted(o['os_piece'] for o in items.item_pieces(c))
    check('body armour pieces', pieces(armour) == ['arm_l', 'arm_r', 'chest', 'pelvis', 'thigh_l', 'thigh_r'], str(pieces(armour)))
    check('gauntlet pieces', pieces(gaunt) == ['cuff_l', 'cuff_r', 'hand_l', 'hand_r'], str(pieces(gaunt)))
    check('boot pieces', pieces(boots) == ['foot_l', 'foot_r', 'shin_l', 'shin_r'], str(pieces(boots)))
    check('list_items sorted, reimport excluded', [c.name for c in items.list_items(ctx.scene)] == sorted(c.name for c in items.list_items(ctx.scene)) and len(items.list_items(ctx.scene)) == 3)
    for c in (armour, gaunt, boots):
        d = items.item_data(c)
        ok, bad = check_rigid(c, rig)
        check(f"{d['type']}: rigid single-bone weights", ok, str(bad))
        check(f"{d['type']}: no shared vertices between pieces", True)
        ch = d['channels']
        for o in items.item_pieces(c):
            a = np.zeros(len(o.data.polygons), np.int32)
            o.data.attributes['os_channel'].data.foreach_get('value', a)
            mi = np.zeros(len(o.data.polygons), np.int32)
            o.data.polygons.foreach_get('material_index', mi)
            check(f"{o.name}: channels in range, one slot per channel", a.min() >= 0 and a.max() < len(ch) and len(o.data.materials) == len(ch)
                  and (a == mi).all() and all(m.name == 'OSC_' + ch[i]['role'] for i, m in enumerate(o.data.materials)))
        keys = {k for o in items.item_pieces(c) for k in shape_keys(o)}
        check(f"{d['type']}: morph keys from the base", keys and keys <= {k.name for k in base.data.shape_keys.key_blocks}, str(sorted(keys))[:120])
        dist = distances(c, bvh)
        check(f"{d['type']}: median distance to body < 4% of height", np.median(dist) < .04 * rest['H'],
              f"median {np.median(dist) / rest['H'] * 100:.2f}% p90 {np.quantile(dist, .9) / rest['H'] * 100:.2f}%")
        check(f"{d['type']}: not edited after import", not items.is_edited(c))
        agree = normal_agreement(c, bvh)
        check(f"{d['type']}: face normals agree with the body surface, left like right", min(agree.values()) > .6 and
              (abs(agree['_l'] - agree['_r']) < .15 if '_l' in agree and '_r' in agree else True), str(agree))
    check('chest has a bulk key that moves it', any(np.abs(np.array([k.data[i].co - o.data.vertices[i].co for i in range(len(o.data.vertices))])).max() > 1e-4
                                                     for o in items.item_pieces(armour) for k in (o.data.shape_keys.key_blocks[1:] if o.data.shape_keys else [])))
    for c in (gaunt, boots):
        w = check_pair_symmetry(c, rest['plane_x'])
        check(f"{items.item_data(c)['type']}: mirrored pair symmetric < 1 mm", w < 1e-3, f'{w * 1000:.4f} mm')
    d = items.item_data(armour)
    check('item record fields', all(k in d for k in ('item_id', 'set_id', 'type', 'label', 'status', 'notes', 'channels', 'source', 'import', 'stats')) and
          {'job_id', 'module', 'race', 'lod', 'manifest', 'wearable_glb', 'wearable_sha256', 'pieces_source'} <= set(d['source']) and
          {'hashes', 'rig_fingerprint', 'base_fingerprint', 'date'} <= set(d['import']))
    check('fingerprints equal project.py values', d['import']['rig_fingerprint'] == items.rig_fingerprint(rig))

    # ---- hand edit, reimport protection
    d['label'], d['notes'] = 'My plate', 'keep me'
    d['channels'][0]['material'] = 'steel'
    d['channels'][0]['role'] = 'accent'
    items.set_item_data(armour, d)
    items.apply_channel_materials(armour)
    check('channel edits are not hand edits of the mesh', not items.is_edited(armour))
    r = importer.import_module(ctx, entries('dwarf_male')['body_armor'], '0p4')[0]
    d2 = items.item_data(armour)
    check('unedited reimport replaces in place keeping label / notes / channel settings', r['mode'] == 'replaced' and d2['label'] == 'My plate'
          and d2['notes'] == 'keep me' and d2['channels'][0]['material'] == 'steel' and d2['channels'][0]['role'] == 'accent'
          and not items.is_edited(armour) and len(items.item_pieces(armour)) == 6)
    chest = bpy.data.objects['OS_plain_plate_mail_01__body_armor__chest']
    chest.data.vertices[0].co.z += .01
    check('moving a vertex counts as an edit', items.is_edited(armour))
    check('refresh_status marks it edited', items.refresh_status(armour) and items.item_data(armour)['status'] == 'edited')
    r = importer.import_module(ctx, entries('dwarf_male')['body_armor'], '0p4')[0]
    pend = items.reimport_collection(armour)
    check('edited item: reimport lands in __reimport, hidden', r['mode'] == 'reimport' and pend is not None and pend.hide_viewport
          and len(pend.objects) == 6 and items.item_data(armour)['status'] == 'needs_review'
          and bpy.data.objects['OS_plain_plate_mail_01__body_armor__chest'].data.vertices[0].co.z == chest.data.vertices[0].co.z)
    check('reimport collection is not listed as an item', len(items.list_items(ctx.scene)) == 3)
    importer.resolve_reimport(ctx, armour, 'compare')
    new_chest = bpy.data.objects['OS_plain_plate_mail_01__body_armor__chest__reimport']
    check('compare shows the new import +1 m in x', not pend.hide_viewport and abs(new_chest.matrix_world.translation.x - 1.) < 1e-6)
    importer.resolve_reimport(ctx, armour, 'keep')
    check('keep mine drops the reimport', items.reimport_collection(armour) is None and items.item_data(armour)['status'] == 'edited'
          and chest.data.vertices[0].co.z != 0 and not [o for o in bpy.data.objects if o.name.endswith('__reimport')])
    importer.import_module(ctx, entries('dwarf_male')['body_armor'], '0p4')
    importer.resolve_reimport(ctx, armour, 'take')
    check('take new replaces the pieces', items.reimport_collection(armour) is None and not items.is_edited(armour) and
          items.item_data(armour)['status'] == 'imported' and len(items.item_pieces(armour)) == 6 and
          items.item_data(armour)['label'] == 'My plate' and {o.name for o in items.item_pieces(armour)} ==
          {f'OS_plain_plate_mail_01__body_armor__{p}' for p in ('chest', 'pelvis', 'thigh_l', 'thigh_r', 'arm_l', 'arm_r')})

    # ---- other LOD
    r = importer.import_module(ctx, entries('dwarf_male')['body_armor'], '0p1')[0]
    check('LOD 0p1 replaces in place with more triangles', r['mode'] == 'replaced' and items.item_data(armour)['stats']['tris'] > 779 and
          items.item_data(armour)['source']['lod'] == '0p1')

    # ---- mirror operator on a hand-edited right piece
    cuff = bpy.data.objects['OS_plain_plate_mail_01__gauntlets__cuff_r']
    for v in cuff.data.vertices:
        v.co.y += .01 * v.co.x
    n = importer.mirror_item(ctx, gaunt, 'r')
    sym = check_pair_symmetry(gaunt, rest['plane_x'])
    check('mirror_item rebuilds the left pieces from the right', n == 2 and sym < 1e-3 and check_rigid(gaunt, rig)[0] and
          len(items.item_pieces(gaunt)) == 4 and not items.is_edited(gaunt),
          f'n={n} sym={sym} rigid={check_rigid(gaunt, rig)} pieces={len(items.item_pieces(gaunt))} edited={items.is_edited(gaunt)}')

    # ---- stale rig
    out = items.check_all(ctx)
    check('check_all: nothing stale on a fresh import', out['stale'] == [], str(out))
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    rig.data.edit_bones['hand.l'].head.x += .02
    bpy.ops.object.mode_set(mode='OBJECT')
    out = items.check_all(ctx)
    check('moved bone makes every item stale', sorted(out['stale']) == ids, str(out['stale']))
    chest = bpy.data.objects['OS_plain_plate_mail_01__body_armor__chest']
    chest.data.vertices[0].co.z += .01
    check('edited stale item is flagged edited', 'plain_plate_mail_01__body_armor' in items.check_all(ctx)['edited'])
    # rebind without edited
    if True:
        ensure_registered()
        res = bpy.ops.ostudio.rebind_stale('EXEC_DEFAULT', include_edited=False)
        left = [items.item_data(c)['item_id'] for c in items.list_items(ctx.scene) if items.item_data(c)['stale']]
        check('rebind_stale skips edited items, re-binds the others', res == {'FINISHED'} and left == ['plain_plate_mail_01__body_armor'], str(left))
        bpy.ops.ostudio.rebind_stale('EXEC_DEFAULT', include_edited=True)
        check('rebind with confirmation clears the rest', not any(items.item_data(c)['stale'] for c in items.list_items(ctx.scene)))
        check('ostudio.check_rig runs', bpy.ops.ostudio.check_rig() == {'FINISHED'})
        current = items.current_item(ctx)
        check('current_item defaults to the first item', current is not None and current == items.list_items(ctx.scene)[0])
        check('ostudio.mirror_pieces runs on the current item', bpy.ops.ostudio.mirror_pieces('EXEC_DEFAULT', source_side='r') == {'FINISHED'} and
              check_pair_symmetry(current, rest['plane_x']) < 1e-3)
        items.sync_props(armour)
        check('UI mirror: channels, label and notes', len(armour.os_channels) == len(items.item_data(armour)['channels']) and armour.os_label == 'My plate')
        armour.os_channels[1].role = 'tertiary'
        check('UI role edit writes the JSON and the material slot', items.item_data(armour)['channels'][1]['role'] == 'tertiary' and
              all(o.data.materials[1].name == 'OSC_tertiary' for o in items.item_pieces(armour)))
        armour.os_notes = 'edited in ui'
        check('UI notes edit writes the JSON', items.item_data(armour)['notes'] == 'edited in ui')
        draw_smoke(armour, ctx)
    check('base project file untouched', paths.base_project('dwarf_male').stat().st_mtime_ns == snap)
    return ctx


class Stub:
    """Layout stand-in: every call returns the stub, `prop` / `operator` just accept their arguments."""

    def __getattr__(self, name):
        return lambda *a, **k: self


def draw_smoke(armour, ctx):
    try:
        items.draw_item(Stub(), ctx)
        importer.draw_import(Stub(), ctx)
        check('draw_item / draw_import run', True)
    except Exception:
        traceback.print_exc()
        check('draw_item / draw_import run', False)


def scenario_races():
    for race in paths.bases():
        ctx = open_base(race)
        rig, base = importer.base_objects(ctx)
        bvh, rest = body_bvh()
        try:
            reports = import_all(ctx, race)
        except Exception:
            traceback.print_exc()
            check(f'{race}: import', False)
            continue
        check(f'{race}: every bone of the rigid table exists on the rig', weights.missing_table_bones(rig) == [], str(weights.missing_table_bones(rig)))
        for c in items.list_items(ctx.scene):
            d = items.item_data(c)
            dist = distances(c, bvh)
            med, p90 = np.median(dist) / rest['H'] * 100, np.quantile(dist, .9) / rest['H'] * 100
            sym = check_pair_symmetry(c, rest['plane_x']) * 1000 if d['type'] in ('gauntlets', 'boots') else float('nan')
            ok, bad = check_rigid(c, rig)
            keys = len({k for o in items.item_pieces(c) for k in shape_keys(o)})
            TABLE.append((race, d['type'], len(items.item_pieces(c)), d['stats']['tris'], med, p90, sym, keys, d['source']['fit']['source'],
                          '; '.join(d['warnings'])))
            ok_med = med < 4. or (race == 'orc_male' and d['type'] in ('gauntlets', 'boots'))      # that job's own fit is poor
            check(f"{race} {d['type']}: rigid weights, median {med:.2f}% p90 {p90:.2f}%, sym {sym:.4f} mm, {keys} keys",
                  ok and ok_med and (np.isnan(sym) or sym < 1.) and keys > 0, str(bad))
        if race == 'gnome_female':
            check('gnome_female (legless): no thigh pieces, armour imported', {o['os_piece'] for o in items.item_pieces(
                items.find_item(ctx.scene, 'plain_plate_mail_01__body_armor'))} == {'chest', 'pelvis', 'arm_l', 'arm_r'})
        if race == 'human_male':
            src = items.item_data(items.list_items(ctx.scene)[0])['source']
            check('human_male (older manifest): fallback fit used', src['fit']['source'] == 'fit_base', str(src['fit']))


def scenario_weight_units():
    W = np.array([[.5, .2, .1, .1, .1], [1, 0, 0, 0, 0], [.1, .1, .1, .1, .6]])
    L = weights.limit(W, 4)
    check('limit keeps 4 influences and normalises', (L > 0).sum(1).max() <= 4 and np.allclose(L.sum(1), 1) and L[0, 2:].tolist().count(0.) <= 1)
    F = np.array([[0, 1, 2], [1, 3, 2]])
    S = weights.smooth_rows(np.array([[1., 0], [0, 1], [0, 1], [0, 1]]), F, 2, .5)
    check('smooth_rows keeps rows normalised and pulls towards neighbours', np.allclose(S.sum(1), 1) and S[0, 0] < 1 and S[1, 0] > 0)
    names = ['c_root_bend.x', 'c_thigh_stretch.l', 'c_thigh_stretch.r', 'c_spine_01_bend.x']
    cfg = weights.table()['smooth']['hips']
    H = weights.hips_blend(np.tile([0, 0, 0, 1.], (3, 1)), names, np.array([.2, 0., -.2]), np.array([True, True, False]), 0., .1, cfg)
    check('hips blend: root .6, thighs share .4 by x, others untouched', np.allclose(H[0], [.6, .4, 0, 0]) and np.allclose(H[1], [.6, .2, .2, 0])
          and np.allclose(H[2], [0, 0, 0, 1]), str(H.round(3).tolist()))
    rest = {'crotch_z': .5, 'mid_x': 0., 'thigh_gap': .1}
    m = weights.hips_faces(None, ['skirt', 'x', 'x', 'x'], np.array([[0, 0, 1.], [0, 0, .2], [.5, 0, .2], [0, 0, .9]]), rest)
    check('hips faces: skirt part, or below the crotch between the thighs', m.tolist() == [True, True, False, False], str(m))


def scenario_outfit_and_pieces():
    """Smooth weights through a pseudo outfit (body armour wearable declared as an `outfit` module) and the pieces-output path."""
    ctx = open_base('dwarf_male')
    rig, base = importer.base_objects(ctx)
    bvh, rest = body_bvh()
    e = dict(entries('dwarf_male')['body_armor'], module='outfit')
    r = importer.import_module(ctx, e, '0p4')[0]
    c = items.find_item(ctx.scene, 'plain_plate_mail_01__outfit')
    obj = items.item_pieces(c)[0]
    names, W = weights.vertex_weights(obj)
    infl = (W > 1e-6).sum(1)
    check('outfit: one piece `outfit`, channel per role', [o['os_piece'] for o in items.item_pieces(c)] == ['outfit'] and
          [x['id'] for x in items.item_data(c)['channels']] == ['primary', 'secondary'], str(items.item_data(c)['channels']))
    check('outfit: smooth weights, <= 4 influences, normalised, deform bones only', infl.max() <= 4 and infl.min() >= 1 and np.allclose(W.sum(1), 1, atol=1e-3)
          and set(names) <= weights.deform_bones(rig) and len(set(names)) > 4 and obj['os_weights'] == 'smooth', f'max {infl.max()} bones {len(names)}')
    chest_bone = names.index('c_spine_02_bend.x') if 'c_spine_02_bend.x' in names else None
    top = np.array([v.co.z for v in obj.data.vertices]) > rest['H'] * .75
    upper = [names.index(n) for n in names if n.startswith(('c_spine', 'shoulder', 'c_arm', 'c_forearm', 'neck', 'head', 'hand'))]
    share = W[top][:, upper].sum(1).mean() if top.any() else 0
    check('outfit: upper vertices are weighted to spine / arm / neck bones', share > .9, f'{share:.3f}')
    # pieces-output path
    try:
        sys.path.insert(0, str(paths.ROOT / 'tools' / 'tripo_pipeline'))
        import split_pieces
    except Exception as ex:
        check('pieces output path (split_pieces importable)', False, str(ex))
        return
    ent = entries('dwarf_male')['body_armor']
    man = ps.load_manifest(ent)
    glb = ps.read_glb(ps.wearable_path(ent, '0p4'))
    cz = glb['v'][glb['f']].mean(1)
    zq = np.quantile(cz[:, 2], [.35, .6])
    label = np.where(cz[:, 2] > zq[1], 0, np.where(cz[:, 2] > zq[0], 1, np.where(cz[:, 0] > 0, 2, 3)))
    names_ = ['chest', 'pelvis', 'thigh_l', 'thigh_r']
    out = SCRATCH / 'pieces_dwarf_male' / 'segment_first' / 'pieces'
    (out / 'lod_0p4').mkdir(parents=True, exist_ok=True)
    (out / 'pieces.json').write_text('{}')
    raw = np.stack([glb['v'][:, 0], glb['v'][:, 2], -glb['v'][:, 1]], 1)          # Blender -> glTF axes
    mats = [{'name': m, 'color': [1, 1, 1, 1]} for m in glb['materials']]
    for i, n in enumerate(names_):
        split_pieces.write_glb(out / 'lod_0p4' / f'{n}.glb', n, raw, glb['f'][label == i], glb['face_material'][label == i], mats)
    ent2 = dict(ent, dir=str(SCRATCH / 'pieces_dwarf_male'))
    r = importer.import_module(ctx, ent2, '0p4')[0]
    c = items.find_item(ctx.scene, 'plain_plate_mail_01__body_armor')
    got = sorted(o['os_piece'] for o in items.item_pieces(c))
    ok, bad = check_rigid(c, rig)
    check('pieces output used when present', got == sorted(names_) and items.item_data(c)['source']['pieces_source'] and ok, f'{got} {bad}')
    dist = distances(c, bvh)
    check('pieces output lands on the body', np.median(dist) < .04 * rest['H'], f'{np.median(dist) / rest["H"] * 100:.2f}%')
    if not QUICK:
        t = importer.import_source(ctx, c)
        lo = np.min([o.matrix_world @ v for o in t if o.type == 'MESH' for v in [o.data.vertices[0].co]], axis=0) if t else None
        meshes = [o for o in t if o.type == 'MESH']
        zs = np.concatenate([np.array([(o.matrix_world @ v.co).z for v in o.data.vertices[:2000]]) for o in meshes])
        check('import_source places the high-poly source on the base', meshes and -.1 * rest['H'] < zs.min() and zs.max() < 1.1 * rest['H'],
              f'z {zs.min():.3f}..{zs.max():.3f}')


def scenario_outfit_project():
    """Library-override environment created by project.py (saved only under the scratch dir)."""
    from outfit_studio.blender import project
    path = SCRATCH / 'dwarf_male_outfits.blend'
    if path.exists():
        path.unlink()
    project.create_outfit_project('dwarf_male', path)
    ctx = bpy.context
    rig, base = importer.base_objects(ctx)
    check('outfit project: base_objects returns the overrides', rig.override_library is not None and base.override_library is not None)
    reports = import_all(ctx, 'dwarf_male')
    bvh, rest = body_bvh()
    c = items.find_item(ctx.scene, 'plain_plate_mail_01__body_armor')
    dist = distances(c, bvh)
    check('outfit project: import works on the override', len(reports) == 3 and np.median(dist) < .04 * rest['H'] and check_rigid(c, rig)[0],
          f'{np.median(dist) / rest["H"] * 100:.2f}%')
    out = items.check_all(ctx)
    check('outfit project: fingerprints agree with project.py', out['stale'] == [], str(out['stale']))
    bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=False)
    bpy.ops.wm.open_mainfile(filepath=str(path))
    out = items.check_all(bpy.context)
    check('outfit project: reopened file keeps items, not stale', len(items.list_items(bpy.context.scene)) == 3 and out['stale'] == [], str(out))


def main():
    before = {b: paths.base_project(b).stat().st_mtime_ns for b in paths.bases()}
    for fn in (scenario_weight_units, scenario_operators, scenario_dwarf_male, scenario_outfit_and_pieces, scenario_races, scenario_outfit_project):
        if QUICK and fn in (scenario_races,):
            continue
        try:
            fn()
        except Exception:
            traceback.print_exc()
            check(f'{fn.__name__} completed', False, 'exception')
    check('no base project was modified', before == {b: paths.base_project(b).stat().st_mtime_ns for b in paths.bases()})
    if TABLE:
        print('\nrace              item       pieces  tris  median%  p90%   sym_mm  keys  fit        warnings')
        for row in TABLE:
            print('%-17s %-10s %5d %6d %8.2f %6.2f %8.4f %5d  %-9s %s' % row)
    failed = [r for r in RESULTS if not r[0]]
    print(f'\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed; scratch {SCRATCH}')
    for _, name, detail in failed:
        print('FAILED:', name, detail)
    sys.stdout.flush()
    os._exit(1 if failed else 0)


main()
