"""Headless Blender test of the AP placement operators on real bases (in memory only).

    blender -b --factory-startup --python tools/outfit_studio/tests/test_blender_ap.py -- [out_dir] [base ...]

Each base project is opened and never saved: points are generated and committed to the
in-memory rig, the result is checked against the SPEC section 5 thresholds and the old
contract, and a comparison .blend (a copy, written to `out_dir`) plus Workbench renders
(old points grey, new points coloured) are written for visual review.
"""
import json
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector

TOOLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TOOLS))
from outfit_studio import paths                                     # noqa: E402
from outfit_studio.blender import ap_ops                            # noqa: E402
from outfit_studio.core import ap as apcore                         # noqa: E402

argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
OUT = Path(argv[0]) if argv else Path('/tmp/claude-1000/-media-niz-Samsung-SSD-godot-blender-characters-imageto3d/'
                                      '595ecea8-fc5e-4ebc-9709-1884d8604278/scratchpad/impl_c/out')
BASES = argv[1:] or ['human_male', 'gnome_female', 'orc_male']
OUT.mkdir(parents=True, exist_ok=True)
assert paths.ROOT not in OUT.resolve().parents and OUT.resolve() != paths.ROOT, 'output must not be inside the project tree'

FAIL = []


def check(ok, msg):
    print(('  ok   ' if ok else '  FAIL ') + msg)
    if not ok:
        FAIL.append(msg)


def colour_of(group):
    fam = apcore.family_of(group)
    base = np.array(ap_ops.FAMILY_COLOUR.get(fam, (1, 1, 1)))
    h = (sum(map(ord, group)) % 7) / 7.0                         # slight per-group variation
    return tuple(np.clip(base * (.65 + .35 * h), 0, 1))


# ---------------------------------------------------------------- rendering
OCT = np.array([[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]], float)
OCT_F = np.array([[0, 2, 4], [2, 1, 4], [1, 3, 4], [3, 0, 4], [2, 0, 5], [1, 2, 5], [3, 1, 5], [0, 3, 5]])


def render_scene(V, F, points, views, prefix, res=640):
    """Workbench render of mesh (V,F) with octahedron markers [(pos, rgb, radius)]; one PNG per view
    {name: (centre, direction, ortho_scale)}. Returns the file paths."""
    sc = bpy.data.scenes.new('ap_render')
    sc.render.engine = 'BLENDER_WORKBENCH'
    sc.render.resolution_x = sc.render.resolution_y = res
    sc.display.shading.light = 'STUDIO'
    sc.display.shading.color_type = 'VERTEX'
    sc.display.shading.show_cavity = False
    sc.view_settings.view_transform = 'Standard'
    sc.world = bpy.data.worlds.new('ap_w')
    sc.world.color = (1, 1, 1)
    Vs, Fs, Cs, n = [np.asarray(V, float)], [np.asarray(F)], [np.full((len(V), 4), .86)], len(V)
    for pos, rgb, r in points:
        Vs.append(np.asarray(pos) + OCT * r)
        Fs.append(OCT_F + n)
        Cs.append(np.tile([*rgb, 1.0], (6, 1)))
        n += 6
    me = bpy.data.meshes.new('ap_render')
    me.from_pydata(np.vstack(Vs).tolist(), [], np.vstack(Fs).tolist())
    me.update()
    a = me.color_attributes.new('Col', 'FLOAT_COLOR', 'POINT')
    a.data.foreach_set('color', np.vstack(Cs).ravel())
    ob = bpy.data.objects.new('ap_render', me)
    sc.collection.objects.link(ob)
    cam = bpy.data.objects.new('ap_cam', bpy.data.cameras.new('ap_cam'))
    cam.data.type = 'ORTHO'
    cam.data.clip_end = 100
    sc.collection.objects.link(cam)
    sc.camera = cam
    files = {}
    for name, (centre, d, scale) in views.items():
        dv = Vector(d).normalized()
        cam.data.ortho_scale = scale
        cam.location = Vector(centre) + dv * 10
        cam.rotation_euler = (-dv).to_track_quat('-Z', 'Y').to_euler()
        sc.render.filepath = str(OUT / f'{prefix}_{name}.png')
        bpy.ops.render.render(write_still=True, scene=sc.name)
        files[name] = sc.render.filepath
    bpy.data.objects.remove(ob)
    bpy.data.objects.remove(cam)
    bpy.data.scenes.remove(sc)
    return files


def side_by_side(files_a, files_b, out_prefix):
    """Stack the old (left) and new (right) renders of each view into one PNG."""
    out = []
    for name in files_a:
        ims = []
        for f in (files_a[name], files_b[name]):
            im = bpy.data.images.load(f)
            w, h = im.size
            px = np.empty(w * h * 4, np.float32)
            im.pixels.foreach_get(px)
            ims.append(px.reshape(h, w, 4))
            bpy.data.images.remove(im)
        both = np.concatenate(ims, axis=1)
        img = bpy.data.images.new(f'cmp_{name}', both.shape[1], both.shape[0], alpha=True)
        img.pixels.foreach_set(both.ravel())
        path = str(OUT / f'{out_prefix}_{name}.png')
        img.filepath_raw = path
        img.file_format = 'PNG'
        img.save()
        bpy.data.images.remove(img)
        out.append(path)
    return out


def view_set(V, base_h):
    """Camera setups for head and body (front/side/back/top of the head, front/side/back of the body)."""
    head_top = V[:, 2].max()
    hz = head_top - .16 * base_h / 1.8
    hc = (0, float(V[V[:, 2] > hz, 1].mean()), hz)
    hs = .55 * base_h / 1.8 + .0
    body = {'body_front': ((0, 0, base_h / 2), (0, -1, 0), base_h * 1.1),
            'body_side': ((0, 0, base_h / 2), (1, 0, 0), base_h * 1.1),
            'body_back': ((0, 0, base_h / 2), (0, 1, 0), base_h * 1.1),
            'body_torso': ((0, 0, base_h * .66), (0, -1, 0), base_h * .55),
            'body_arm': ((base_h * .2, 0, base_h * .72), (0, -1, 0), base_h * .5)}
    head = {'head_front': (hc, (0, -1, 0), hs), 'head_side': (hc, (1, 0, 0), hs),
            'head_back': (hc, (0, 1, 0), hs), 'head_top': (hc, (0, -.001, 1), hs)}
    return {**head, **body}


# ---------------------------------------------------------------- one base
def run_base(name):
    print(f'== {name}')
    bpy.ops.wm.open_mainfile(filepath=str(paths.base_project(name)))
    scene = bpy.context.scene
    rig, base = ap_ops.find_objects(scene)
    check(rig is not None and base is not None, 'rig and base found')
    old = ap_ops.existing_points(rig)
    old_counts = ap_ops.group_counts(old)
    old_names = set(old)
    old_groups = set(old_counts)
    old_coll = {c.name for b in rig.data.bones if b.name.startswith('AP_') for c in b.collections}
    old_frames = {n: np.array(rig.data.bones[n].matrix_local) for n in old}
    body = ap_ops.body_data(rig, base)
    height = float(np.ptp(body['V'][:, 2]))
    old_pts = {n: p['head'] for n, p in old.items()}

    # settings live in scene["outfit_studio"]["ap"] and never clobber the other keys of that JSON
    scene['outfit_studio'] = json.dumps({'base': name, 'keep_me': 1})
    scene.ostudio_ap.density = 1.0
    scene.ostudio_ap.hairline = .2
    m = json.loads(scene['outfit_studio'])
    check(m.get('keep_me') == 1 and m.get('base') == name and abs(m['ap']['hairline'] - .2) < 1e-6,
          'AP settings merged into scene["outfit_studio"] without clobbering other keys')

    # lock one bone of a regenerated group: it must come out untouched
    lock_name = next(n for n, p in old.items() if p['group'] == 'torso_f' and p['index'] == 2)
    rig.data.bones[lock_name][ap_ops.LOCK] = 1
    lock_before = np.array(rig.data.bones[lock_name].matrix_local)
    lock_parent = rig.data.bones[lock_name].parent.name

    ap_ops.sync_props(scene)
    s = ap_ops.get_settings(scene)
    ctx_q = apcore.prepare(body, s)

    # generate (preview only) -> the rig must not change
    r = bpy.ops.ostudio.ap_generate()
    check(r == {'FINISHED'}, 'ap_generate finished')
    prev = bpy.data.collections.get(ap_ops.PREVIEW)
    check(prev is not None and len(prev.objects) > 0, f'preview collection has {len(prev.objects) if prev else 0} empties')
    check(set(b.name for b in rig.data.bones if b.name.startswith('AP_')) == old_names, 'generate does not change the rig')
    entry = ap_ops._LAST[rig.name]
    check(base.data.attributes.get('os_region_scalp') is not None, 'os_region_scalp face attribute written')

    # commit
    r = bpy.ops.ostudio.ap_commit()
    check(r == {'FINISHED'}, 'ap_commit finished')
    check(bpy.data.collections.get(ap_ops.PREVIEW) is None, 'preview cleared after commit')
    new = ap_ops.existing_points(rig)
    new_counts = ap_ops.group_counts(new)
    check(set(new_counts) == old_groups, f'same groups as before ({len(old_groups)})')
    changed = {g: (old_counts[g], new_counts[g]) for g in old_groups if old_counts[g] != new_counts[g]}
    print('   count changes', changed)
    skipped = entry['res']['skipped']
    print('   groups kept as they were (no usable surface):', skipped)
    # contract of every AP bone
    bad = []
    for nme, p in new.items():
        b = rig.data.bones[nme]
        m = np.array(b.matrix_local)[:3, :3]
        ok = (b.parent is not None and not b.use_deform and b.get('cc') == 1 and abs(np.linalg.det(m) - 1) < 1e-4
              and np.abs(m.T @ m - np.eye(3)).max() < 1e-4)
        if nme in entry['res']['points'] or nme not in old_names:
            ok = ok and {c.name for c in b.collections} <= (old_coll or {ap_ops.COLLECTION})
        if not ok:
            bad.append(nme)
    check(not bad, f'all {len(new)} AP bones: parented, non-deform, cc-tagged, orthonormal, collection {sorted(old_coll)}'
          + (f' (bad: {bad[:3]})' if bad else ''))
    # +Y is the outward normal: bone +Y at the head point points away from the surface
    pts = entry['res']['points']
    frames_new, _ = ap_ops.named_frames(entry['res'])
    off = max(float(np.abs(np.array(rig.data.bones[n].matrix_local)[:3, 1] - M[:3, 1]).max())
              for n, M in frames_new.items() if n != lock_name and n in new)
    check(off < 1e-3, f'bone +Y matches the generated normal (max dev {off:.1e})')
    # locked bone
    check(np.abs(np.array(rig.data.bones[lock_name].matrix_local) - lock_before).max() < 1e-7
          and rig.data.bones[lock_name].parent.name == lock_parent, 'locked bone untouched')
    # the unlocked bones of that group were moved
    moved = [n for n in new if new[n]['group'] == 'torso_f' and n != lock_name
             and (n not in old or np.abs(new[n]['head'] - old[n]['head']).max() > 1e-4)]
    check(len(moved) > 0, f'unlocked torso_f bones regenerated ({len(moved)} moved)')

    # quality, measured from the bones themselves, on the groups this run generated (old and new alike)
    gen = set(pts)
    thr = ap_ops._meta(scene).get('ap_thr')
    rep_old = apcore.quality.report(ctx_q, apcore.quality.from_rig(old, None, gen))
    rep_new = apcore.quality.report(ctx_q, apcore.quality.from_rig(new, thr, gen))
    r = bpy.ops.ostudio.ap_report()
    rep_all = ap_ops._meta(scene)['ap_report_rig']
    print('   whole rig incl. kept groups:', ap_ops.report_text(rep_all).replace('\n', ' | '))
    print('   relaxed joint purity:', rep_new['relaxed'])
    print('   NEW', ap_ops.report_text(rep_new).replace('\n', ' | '))
    print('   OLD', ap_ops.report_text(rep_old).replace('\n', ' | '))
    check(rep_new['mirror_mm']['max'] < 1.0, f"mirror pairing max {rep_new['mirror_mm']['max']:.3f} mm < 1 mm"
          + f" (worst {max(rep_new['mirror_mm']['by_group'], key=rep_new['mirror_mm']['by_group'].get, default='')})")
    check(rep_new['spacing_cv']['scatter_max'] < .25, f"scatter spacing CV max {rep_new['spacing_cv']['scatter_max']:.3f} < 0.25")
    check(rep_new['scalp_in_face']['count'] == 0, f"scalp points in the face region: {rep_new['scalp_in_face']['count']}")
    check(rep_new['joint_points']['count'] == 0, f"points at joints: {rep_new['joint_points']['count']} "
          f"{rep_new['joint_points']['by_group']}")
    # no scalp point in the face region for the committed bones, with an independent measure: the
    # face mask written on the mesh
    scalp_pts = np.array([new[n]['head'] for n in new if new[n]['group'].startswith('scalp_')])
    hs = apcore.api.head_context(entry['ctx'], entry['ctx']['head'], 'landmark_geodesic')
    _, f, b, _ = apcore.mesh.closest_points(entry['ctx']['head']['V'], entry['ctx']['head']['F'], scalp_pts)
    fm = apcore.mesh.bary_eval(entry['ctx']['head'], hs['face'], f, b)
    check((fm > 0).sum() == 0, f'{len(scalp_pts)} scalp bones, none in the face region')

    # comparison blend + renders
    V = body['V']
    F = body['F']
    old_list = [(p, (.45, .45, .45), .0045 * height / 1.8) for p in old_pts.values()]
    new_list = []
    for n, p in new.items():
        g = p['group']
        new_list.append((p['head'], colour_of(g) if apcore.family_of(g) else (.45, .45, .45), .0055 * height / 1.8))
    views = view_set(V, height)
    fa, fb = {}, {}
    for kind, grow in (('head', 1.0), ('body', 2.4)):          # body markers are drawn bigger to stay visible
        vs = {k: v for k, v in views.items() if k.startswith(kind)}
        fa.update(render_scene(V, F, [(p, c, r * grow) for p, c, r in old_list], vs, f'{name}_old'))
        fb.update(render_scene(V, F, [(p, c, r * grow) for p, c, r in new_list], vs, f'{name}_new'))
    cmp_files = side_by_side(fa, fb, f'{name}_compare')
    write_compare_blend(name, rig, base, old_pts, new, height)
    return {'base': name, 'old': rep_old, 'new': rep_new, 'count_changes': changed, 'kept': skipped,
            'renders': cmp_files}


def write_compare_blend(name, rig, base, old_pts, new, height):
    """Old (grey) and new (coloured) points as two sets of empties on the base mesh; saved as a copy."""
    sc = bpy.context.scene
    for cname in ('AP_compare_old', 'AP_compare_new'):
        c = bpy.data.collections.get(cname)
        if c:
            for o in list(c.objects):
                bpy.data.objects.remove(o)
            bpy.data.collections.remove(c)
    cold, cnew = bpy.data.collections.new('AP_compare_old'), bpy.data.collections.new('AP_compare_new')
    sc.collection.children.link(cold)
    sc.collection.children.link(cnew)
    size = .005 * height / 1.8
    for n, pos in old_pts.items():
        e = bpy.data.objects.new('old_' + n, None)
        e.empty_display_type, e.empty_display_size = 'SPHERE', size
        e.location = rig.matrix_world @ Vector(pos.tolist())
        e.color = (.45, .45, .45, 1)
        cold.objects.link(e)
    for n, p in new.items():
        e = bpy.data.objects.new('new_' + n, None)
        e.empty_display_type, e.empty_display_size = 'SPHERE', size * 1.2
        e.location = rig.matrix_world @ Vector(p['head'].tolist())
        e.color = (*(colour_of(p['group']) if apcore.family_of(p['group']) else (.45, .45, .45)), 1)
        cnew.objects.link(e)
    try:
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                area.spaces[0].shading.color_type = 'OBJECT'
    except Exception:
        pass
    f = OUT / f'compare_{name}.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(f), copy=True)       # a copy: the open project is never saved
    return f


def main():
    for k in ap_ops.CLASSES:
        bpy.utils.register_class(k)
    ap_ops.register()
    results = []
    try:
        for b in BASES:
            results.append(run_base(b))
    finally:
        (OUT / 'ap_results.json').write_text(json.dumps(results, indent=1, default=float))
    print('\nSUMMARY (old -> new)')
    for r in results:
        o, n = r['old'], r['new']
        print(f"{r['base']:14s} mirror {o['mirror_mm']['max']:8.1f} -> {n['mirror_mm']['max']:.3f} mm | scatter CV max "
              f"{o['spacing_cv']['scatter_max']:.2f} -> {n['spacing_cv']['scatter_max']:.2f} | scalp in face "
              f"{o['scalp_in_face']['count']} -> {n['scalp_in_face']['count']} | joint points "
              f"{o['joint_points']['count']} -> {n['joint_points']['count']}")
    if FAIL:
        print('\nFAILED:')
        for f in FAIL:
            print(' -', f)
        sys.exit(1)
    print('\nALL OK')


main()
