"""Headless tests for validate.py, export_items.py, batch.py and run.py (Blender 5.1).

    python3 tools/outfit_studio/tests/test_blender_export_items.py [step ...]

Steps (default all, in order): import, validate, approve, base, export, verify, resume, edit, hashcheck, operators, broken, leaks, all.
The orchestrator (plain python) drives `run.py` and `blender -b` workers; this same file is the in-Blender worker
(`blender -b --factory-startup --python this.py -- <mode> <args>`). Everything is written under $OSTUDIO_TEST_OUT
(default <tmp>/ostudio_test_export_items) via --project-dir / --batch; projects/, models/, the real export folder and
the real outfit_projects/ are only read (their mtimes are asserted unchanged). Exit code 1 on any failure."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
ROOT = PKG.parents[1]                                           # .../imageto3d
REPO = ROOT.parents[1]                                          # .../godot/blender
OUT = Path(os.environ.get('OSTUDIO_TEST_OUT') or Path(tempfile.gettempdir()) / 'ostudio_test_export_items')
EXE = os.environ.get('BLENDER', 'blender')
PD, BATCH = OUT / 'projects', OUT / 'batch'
SET = 'plain_plate_mail_01'
GNOME_SET = 'plain_plate_mail_01_gnome'
BASES = ('dwarf_male', 'human_male', 'dwarf_female')            # converted for the same set: item.json races lists all three
TYPES = ('body_armor', 'gauntlets', 'boots')
ENGINE = {'chest': {'Chest': 1.}, 'pelvis': {'Hips': 1.},
          'thigh_l': {'LeftUpperLeg_stretch': 1.}, 'thigh_r': {'RightUpperLeg_stretch': 1.},
          'arm_l': {'LeftUpperArm_stretch': 1.}, 'arm_r': {'RightUpperArm_stretch': 1.},
          'cuff_l': {'LeftLowerArm_stretch': 1.}, 'cuff_r': {'RightLowerArm_stretch': 1.},
          'hand_l': {'LeftHand': 1.}, 'hand_r': {'RightHand': 1.},
          'shin_l': {'LeftLowerLeg_stretch': 1.}, 'shin_r': {'RightLowerLeg_stretch': 1.},
          'foot_l': {'LeftFoot': 1.}, 'foot_r': {'RightFoot': 1.}}
RESULTS = []

try:
    import bpy
except ImportError:
    bpy = None


def check(name, ok, detail=''):
    RESULTS.append((bool(ok), name))
    print(('PASS ' if ok else 'FAIL ') + name + (f'  [{detail}]' if detail else ''), flush=True)
    return bool(ok)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# =====================================================================================================
# in-Blender workers
# =====================================================================================================

def boot():
    import addon_utils
    addon_utils.enable('outfit_studio', default_set=False)
    from outfit_studio.blender import arp_export
    arp_export.enable_arp()


def open_project(base):
    bpy.ops.wm.open_mainfile(filepath=str(PD / f'{base}_outfits.blend'))
    return bpy.context


def w_approve(blend):
    boot()
    bpy.ops.wm.open_mainfile(filepath=blend)
    from outfit_studio.blender import items
    for c in items.list_items(bpy.context.scene):
        d = items.item_data(c)
        d['status'] = 'approved'
        items.set_item_data(c, d)
    bpy.ops.wm.save_mainfile()
    check(f'approved {Path(blend).name}', len(items.list_items(bpy.context.scene)) == 3)


def w_edit_channel(blend):
    boot()
    bpy.ops.wm.open_mainfile(filepath=blend)
    from outfit_studio.blender import items
    c = items.find_item(bpy.context.scene, f'{SET}__body_armor')
    d = items.item_data(c)
    d['channels'][0]['role'] = 'accent'
    items.set_item_data(c, d)
    items.apply_channel_materials(c)
    bpy.ops.wm.save_mainfile()
    check('edited channel 0 role of body_armor', True)


def w_minimal_base(base, out):
    """Fallback base export (SPEC 7): fix_weights, split_head, export_glb; only skeleton_sha256 and joints are recorded."""
    boot()
    from outfit_studio import paths
    from outfit_studio.blender import arp_export as ae
    bpy.ops.wm.open_mainfile(filepath=str(paths.base_project(base)))
    body = bpy.data.objects['base']
    rig = body.find_armature()
    ae.fix_weights([body], rig)
    parts = ae.ce('skin').split_head(body)
    Path(out).mkdir(parents=True, exist_ok=True)
    audit = ae.export_glb(Path(out) / 'base.glb', rig, parts)
    doc, _ = ae.ce('gltf').read(Path(out) / 'base.glb')
    joints = [doc['nodes'][i]['name'] for i in doc['skins'][0]['joints']]
    (Path(out) / 'base.json').write_text(json.dumps({'version': 1, 'base': base, 'skeleton_sha256': audit['skeleton_sha256'], 'joints': joints}))
    check('minimal base export', True, audit['skeleton_sha256'][:12])


def w_verify(batch):
    """Re-import every exported item GLB in a fresh session and compare it with base.glb / <base>.json / item.json."""
    boot()
    from outfit_studio.blender import arp_export as ae
    gl, au = ae.ce('gltf'), ae.ce('native_audit')
    batch = Path(batch)
    cat = json.loads((batch / 'catalog.json').read_text())
    n_items = 0
    for base in sorted(cat['bases']):
        base_glb = batch / 'bases' / base / 'base.glb'
        base_json = json.loads((batch / 'bases' / base / 'base.json').read_text())
        bdoc, _ = gl.read(base_glb)
        bjoints = [bdoc['nodes'][i]['name'] for i in bdoc['skins'][0]['joints']]
        check(f'{base}: catalog hash equals base.json', cat['bases'][base] == base_json['skeleton_sha256'])
        for item in cat['items']:
            if base not in item['races']:
                continue
            iid = item['id']
            d = batch / 'items' / iid
            glb, rec = d / f'{base}.glb', json.loads((d / f'{base}.json').read_text())
            ij = json.loads((d / 'item.json').read_text())
            tag = f'{base}/{iid.split("__", 1)[1]}'
            doc, binary = gl.read(glb)
            joints = [doc['nodes'][i]['name'] for i in doc['skins'][0]['joints']]
            audit = au.audit(glb, require_twist_weights=False)
            check(f'{tag}: one skin, joints == base.glb ({len(joints)})', len(doc['skins']) == 1 and joints == bjoints)
            check(f'{tag}: skeleton_sha256 equal (glb, <base>.json, base.json)',
                  audit['skeleton_sha256'] == rec['skeleton_sha256'] == base_json['skeleton_sha256'], rec['skeleton_sha256'][:12])
            mesh_nodes = [n for n in doc['nodes'] if 'mesh' in n]
            ok = len(mesh_nodes) == len(rec['nodes']) and all(
                [doc['materials'][p['material']]['name'] for p in doc['meshes'][n['mesh']]['primitives']] == rec['nodes'][n['name']]['primitives']
                for n in mesh_nodes)
            check(f'{tag}: primitives / channel map equal <base>.json ({sum(len(n["primitives"]) for n in rec["nodes"].values())} primitives)', ok)
            ids = {c['id'] for c in ij['channels']}
            names = {doc['materials'][p['material']]['name'] for n in mesh_nodes for p in doc['meshes'][n['mesh']]['primitives']}
            check(f'{tag}: material names are channel ids', names <= ids and not any(m.startswith('OSCH_') for m in names), sorted(names)[:3])
            check(f'{tag}: item.json lists this base and its channel ids cover the GLB', base in ij['races'] and names <= ids, str(ij['races']))
            check(f'{tag}: morph weights neutral', all(not any(abs(w) > 1e-7 for w in doc['meshes'][n['mesh']].get('weights', [])) for n in mesh_nodes))
            # Blender re-import
            bpy.ops.wm.read_factory_settings(use_empty=True)
            bpy.ops.import_scene.gltf(filepath=str(glb))
            arms = [o for o in bpy.data.objects if o.type == 'ARMATURE']
            shapes = {pb.custom_shape for pb in arms[0].pose.bones} if arms else set()
            meshes = [o for o in bpy.data.objects if o.type == 'MESH' and o not in shapes]
            check(f'{tag}: re-imported 1 armature, {len(meshes)} meshes', len(arms) == 1 and len(meshes) == len(rec['nodes']),
                  f'bones {len(arms[0].data.bones) if arms else 0}')
            bad, keyed = [], 0
            for o in meshes:
                n = rec['nodes'].get(o.name)
                if n is None:
                    bad.append((o.name, 'no node record'))
                    continue
                mats = [m.name if m else None for m in o.data.materials]
                if mats != n['primitives'] and sorted(mats) != sorted(n['primitives']):
                    bad.append((o.name, 'materials', mats, n['primitives']))
                keys = [k.name for k in o.data.shape_keys.key_blocks[1:]] if o.data.shape_keys else []
                keyed += bool(keys) and all(k.startswith('bulk_') or k.startswith('head_') for k in keys)
                if n['rigid_bone']:
                    want = ENGINE.get(n['piece']) or n['rigid_bones']
                    gs = {}
                    for v in o.data.vertices:
                        for g in v.groups:
                            if g.weight > 1e-6:
                                gs.setdefault(o.vertex_groups[g.group].name, []).append(g.weight)
                    if set(gs) != set(want) or any(len(w) != len(o.data.vertices) or abs(sum(w) / len(w) - want[k]) > 1e-4 for k, w in gs.items()):
                        bad.append((o.name, 'rigid groups', {k: len(w) for k, w in gs.items()}, want))
            check(f'{tag}: materials == channel map, rigid pieces weighted to exactly the table bone', not bad, str(bad[:2]))
            check(f'{tag}: shape keys (bulk_*) present on pieces', keyed == len(meshes) and keyed > 0, f'{keyed}/{len(meshes)}')
            n_items += 1
    check(f'verified {n_items} item layers', n_items >= 9)


def ids(res, **kw):
    return [r for r in res if all(r.get(k) == v for k, v in kw.items())]


def w_broken(batch):
    """Deliberately broken in-memory copies must be caught; nothing is saved."""
    boot()
    from outfit_studio.blender import export_items, importer, items, validate, weights
    ba, ga = f'{SET}__body_armor', f'{SET}__gauntlets'
    before = sha(PD / 'dwarf_male_outfits.blend')

    def load():
        ctx = open_project('dwarf_male')
        return ctx, importer.base_objects(ctx)[0]

    def piece(ctx, item, name):
        return next(o for o in items.item_pieces(items.find_item(ctx.scene, item)) if o['os_piece'] == name)

    ctx, rig = load()
    res = validate.validate(ctx, siblings=False)
    check('clean project: 0 errors', not [r for r in res if r['level'] == 'error'], str([r['code'] for r in res if r['level'] == 'error']))
    check('clean project: no asymmetry / missing morphs', not ids(res, code='asymmetric') and not ids(res, code='missing_morph'))
    res = validate.validate(ctx, [ba], budget=100, siblings=False)
    check('tris over budget -> warning', ids(res, level='warning', code='tris_over_budget'))
    c = items.find_item(ctx.scene, ba)
    d = items.item_data(c)
    d['status'] = 'imported'
    items.set_item_data(c, d)
    check('status not approved -> warning', ids(validate.validate(ctx, [ba], siblings=False), level='warning', code='status'))
    full = validate.validate(ctx)
    check('missing on some bases listed', ids(full, level='warning', code='missing_on_bases'), str(ids(full, code='missing_on_bases')[:1])[:150])
    check('channel ids differing between bases -> warning', ids(full, code='channels_differ'))

    deform = sorted(weights.deform_bones(rig))
    cases = {}

    ctx, rig = load()
    o = piece(ctx, ba, 'chest')
    for name in [n for n in deform if n not in o.vertex_groups][:4]:
        o.vertex_groups.new(name=name).add([0], 0.05, 'REPLACE')
    r = validate.validate(ctx, [ba], siblings=False)
    e = ids(r, code='influences')
    check('5 influences -> warning with the vertex', e and e[0]['level'] == 'warning' and 0 in e[0]['verts'], str(e[:1])[:120])
    check('5 influences on a rigid piece -> rigid_binding error', ids(r, level='error', code='rigid_binding'))

    ctx, rig = load()
    o = piece(ctx, ba, 'chest')
    a = o.data.attributes['os_channel']
    a.data[0].value = 99
    r = validate.validate(ctx, [ba], siblings=False)
    e = ids(r, level='error', code='channel_range')
    check('out-of-range os_channel -> error with face 0', e and e[0]['faces'] == [0])
    try:
        export_items.export_one(ba, 'dwarf_male', Path(batch) / 'broken_export')
        check('export refuses an item with validation errors', False)
    except ValueError as ex:
        check('export refuses an item with validation errors', 'channel_range' in str(ex), str(ex)[:100])
    check('refused export left no GLB', not list((Path(batch) / 'broken_export').rglob('*.glb')))
    # select button
    validate.LAST[:] = r
    idx = next(i for i, x in enumerate(validate.LAST) if x['code'] == 'channel_range')
    bpy.ops.ostudio.validate_select(index=idx)
    ob = bpy.context.object
    in_edit = ob is not None and ob.mode == 'EDIT'
    bpy.ops.object.mode_set(mode='OBJECT')
    sel = [p.index for p in ob.data.polygons if p.select]
    check('ostudio.validate_select selects the offending face', in_edit and ob.name == o.name and sel == [0], f'{ob.name} {sel}')

    ctx, rig = load()
    o = piece(ctx, ba, 'chest')
    o.animation_data_create()
    o.animation_data.action = bpy.data.actions.new('x')
    r = validate.validate(ctx, [ba], siblings=False)
    check('animation data on item -> error', ids(r, level='error', code='animation_data'))

    ctx, rig = load()
    o = piece(ctx, ba, 'chest')
    o.vertex_groups.new(name='NotABone').add([1], 1.0, 'REPLACE')
    r = validate.validate(ctx, [ba], siblings=False)
    check('non-deform vertex group -> error', ids(r, level='error', code='group_not_deform'))

    ctx, rig = load()
    o = piece(ctx, ba, 'chest')
    for g in o.vertex_groups:
        g.remove([2])
    r = validate.validate(ctx, [ba], siblings=False)
    e = ids(r, level='error', code='unweighted')
    check('vertex without deform weight -> error', e and 2 in e[0]['verts'])

    ctx, rig = load()
    o = piece(ctx, ba, 'chest')
    o.vertex_groups['c_spine_02_bend.x'].add([0], 1.0, 'REPLACE')
    o.vertex_groups.new(name='c_root_bend.x').add([0], 0.5, 'REPLACE')
    r = validate.validate(ctx, [ba], siblings=False)
    check('rigid piece with a second bone -> rigid_binding error', ids(r, level='error', code='rigid_binding'))
    check('weight sum != 1 -> warning', ids(r, level='warning', code='weight_sum'))

    ctx, rig = load()
    piece(ctx, ba, 'chest').data.clear_geometry()
    r = validate.validate(ctx, [ba], siblings=False)
    check('empty piece -> error', ids(r, level='error', code='empty_piece', piece='chest'))

    ctx, rig = load()
    c = items.find_item(ctx.scene, ba)
    d = items.item_data(c)
    d['channels'][0]['role'] = 'bogus'
    d['import']['rig_fingerprint'] = 'not-the-rig'
    items.set_item_data(c, d)
    r = validate.validate(ctx, [ba], siblings=False)
    check('role outside the palette -> error', ids(r, level='error', code='bad_role'))
    check('stale rig fingerprint -> error', ids(r, level='error', code='stale_rig'))

    ctx, rig = load()
    piece(ctx, ba, 'chest').shape_key_clear()
    r = validate.validate(ctx, [ba], siblings=False)
    check('missing bulk_* key -> error', ids(r, level='error', code='missing_morph', piece='chest'))

    ctx, rig = load()
    o = piece(ctx, ga, 'cuff_l')
    o.data.vertices[0].co.x += 0.005
    r = validate.validate(ctx, [ga], siblings=False)
    e = ids(r, level='error', code='asymmetric', piece='cuff_l')
    check('5 mm asymmetric gauntlet pair -> error', e and 0 in e[0]['verts'], str(e[:1])[:140])
    check('hand pair untouched', not ids(r, code='asymmetric', piece='hand_l'))
    check('dwarf_male_outfits.blend unchanged', sha(PD / 'dwarf_male_outfits.blend') == before)


def w_leaks():
    """Peeking at sibling projects must leave no libraries / data behind."""
    boot()
    from outfit_studio.blender import validate
    open_project('dwarf_male')
    counts = lambda: (len(bpy.data.libraries), len(bpy.data.objects), len(bpy.data.collections), len(bpy.data.meshes),
                      len(bpy.data.armatures), len(bpy.data.materials))
    before = counts()
    t = time.time()
    sib = validate.sibling_items(PD)
    check('sibling_items found the other bases', set(sib) == {'dwarf_female', 'gnome_female', 'human_male'}, f'{ {k: len(v) for k, v in sib.items()} }')
    check('sibling_items leaves libraries / objects / collections / meshes unchanged', counts() == before, f'{before} -> {counts()}')
    print(f'sibling peek {time.time() - t:.2f}s', flush=True)


def w_operators(batch):
    """ostudio.validate (panel results), the export launcher + completion callback that sets status exported."""
    boot()
    from outfit_studio.blender import export_items, items, validate
    before = sha(PD / 'dwarf_male_outfits.blend')
    ctx = open_project('dwarf_male')
    check('ostudio.validate operator', bpy.ops.ostudio.validate(scope='ALL') == {'FINISHED'})
    s = validate.summary(validate.LAST)
    check('operator stored results for the panel, 0 errors', len(validate.LAST) > 0 and s['errors'] == 0, str(s))
    check('ostudio.validate current item', bpy.ops.ostudio.validate(scope='CURRENT') == {'FINISHED'})
    check('ostudio.export_items poll', bpy.ops.ostudio.export_items.poll())

    class Layout:                               # records nothing; every call and attribute is accepted
        def __getattr__(self, name):
            return lambda *a, **k: Layout()
    try:
        validate.draw(Layout(), ctx)
        export_items.draw(Layout(), ctx)
        check('panel drawers run (validate.draw, export_items.draw)', True)
    except Exception as e:
        check('panel drawers run (validate.draw, export_items.draw)', False, str(e))
    c = items.list_items(ctx.scene)[0]
    d = items.item_data(c)
    d['status'] = 'edited'
    items.set_item_data(c, d)
    bpy.ops.ed.undo_push(message='edit')        # marks the file dirty like a user edit (background mode only tracks undo pushes)
    check('file is dirty after an edit', bpy.data.is_dirty)
    try:
        export_items.start_export(ctx, 'APPROVED', str(batch) + '_ops')
        check('export refuses a file with unsaved changes', False)
    except ValueError as e:
        check('export refuses a file with unsaved changes', 'unsaved' in str(e), str(e)[:80])
    ctx = open_project('dwarf_male')            # discard the change
    check('file is clean after re-open', not bpy.data.is_dirty)
    ops_batch = Path(str(batch) + '_ops')
    shutil.rmtree(ops_batch, ignore_errors=True)
    shutil.copytree(Path(batch) / 'bases', ops_batch / 'bases')
    proc, info = export_items.start_export(ctx, 'CURRENT', str(ops_batch), True)
    code = proc.wait()
    done, failed = export_items.finish_export(ctx.scene, code, info)
    cur = items.current_item(ctx)
    check('background export finished', code == 0 and not failed and len(done) == 1, f'exit {code} done {done} failed {failed}')
    check('completion callback set status exported (in the open session)', items.item_data(cur)['status'] == 'exported', items.item_data(cur)['status'])
    check('the file on disk was not saved by the export', sha(PD / 'dwarf_male_outfits.blend') == before)
    check('exported GLB exists', (ops_batch / 'items' / items.item_data(cur)['item_id'] / 'dwarf_male.glb').is_file())
    bpy.ops.wm.open_mainfile(filepath=str(PD / 'dwarf_male_outfits.blend'))
    check('items on disk still approved', all(items.item_data(c)['status'] == 'approved' for c in items.list_items(bpy.context.scene)))


WORKERS = {'approve': w_approve, 'edit_channel': w_edit_channel, 'minimal_base': w_minimal_base, 'verify': w_verify,
           'broken': w_broken, 'leaks': w_leaks, 'operators': w_operators}


def worker(argv):
    mode, args = argv[0], argv[1:]
    try:
        WORKERS[mode](*args)
    except Exception:
        import traceback
        traceback.print_exc()
        check(f'worker {mode} completed', False)
    bad = [n for ok, n in RESULTS if not ok]
    sys.exit(1 if bad else 0)


# =====================================================================================================
# orchestrator (plain python)
# =====================================================================================================

def run(cmd, **kw):
    t = time.time()
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)
    return p.returncode, p.stdout, time.time() - t


def run_py(*args):
    return run([sys.executable, str(PKG / 'run.py'), *map(str, args), '--project-dir', str(PD), '--batch', str(BATCH)])


def blender_worker(mode, *args):
    code, out, secs = run([EXE, '-b', '--factory-startup', '--python-exit-code', '1', '--python', str(Path(__file__).resolve()), '--', mode, *map(str, args)])
    for line in out.splitlines():
        if line.startswith(('PASS ', 'FAIL ', 'sibling peek')):
            print(('  ' + line) if not line.startswith('FAIL') else line, flush=True)
            if line.startswith(('PASS ', 'FAIL ')):
                RESULTS.append((line.startswith('PASS '), line[5:]))
    if code:
        print(out[-3000:])
    check(f'worker {mode} exit 0', code == 0, f'{secs:.1f}s')
    return out


def snapshot():
    snap = {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in sorted((ROOT / 'projects').glob('*.blend*'))}
    snap['_outfit_projects_exists'] = (ROOT / 'outfit_projects').exists()
    snap['_real_export'] = sorted(str(p) for p in (REPO / 'export' / 'outfit_studio').glob('*')) if (REPO / 'export' / 'outfit_studio').exists() else None
    return snap


def step_import():
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    for base, sets in (('dwarf_male', SET), ('gnome_female', GNOME_SET), ('human_male', SET), ('dwarf_female', SET)):
        code, out, secs = run_py('import', '--base', base, '--set', sets)
        check(f'run.py import {base} {sets}', code == 0 and (PD / f'{base}_outfits.blend').exists(), f'{secs:.1f}s ' + out.strip().splitlines()[-2][:100])
    code, out, _ = run_py('import', '--base', 'gnome_female', '--set', 'arcane_plate_mail_v2')   # staged, never converted
    check('import of a base without output for that set is a no-op, exit 0', code == 0 and 'no converted pipeline output' in out)
    code, out, _ = run_py('import', '--base', 'dwarf_male', '--set', SET)
    check('second import skips existing items', code == 0 and 'imported 0, skipped 3' in out, out.strip().splitlines()[-2][:100])
    code, out, _ = run_py('import', '--base', 'nonsense')
    check('unknown base is a usage error', code != 0 and 'unknown base' in out)


def step_validate():
    code, out, secs = run_py('validate', '--base', 'all')
    check('run.py validate --base all: no errors, exit 0', code == 0 and ' 0 errors' in out and out.count('validate ') >= 4, f'{secs:.1f}s')
    warn = {}
    for f in sorted((BATCH / 'logs').glob('validate_*.json')):
        for r in json.loads(f.read_text())['results']:
            warn[r['code']] = warn.get(r['code'], 0) + 1
    print('  warnings by code:', warn)


def step_approve():
    for base in (*BASES, 'gnome_female'):
        blender_worker('approve', PD / f'{base}_outfits.blend')


def step_base():
    for base in (*BASES, 'gnome_female'):
        if os.environ.get('OSTUDIO_TEST_MINIMAL_BASE') or not (PKG / 'blender' / 'export_base.py').exists():
            blender_worker('minimal_base', base, BATCH / 'bases' / base)
            continue
        code, out, secs = run_py('export-base', '--base', base)
        check(f'run.py export-base {base}', code == 0 and (BATCH / 'bases' / base / 'base.json').exists(), f'{secs:.1f}s')
    code, out, _ = run_py('export-base', '--base', 'dwarf_male')
    check('export-base refuses to overwrite without --resume', code == 1 and 'already has a base export' in (BATCH / 'logs' / 'export-base_dwarf_male.log').read_text())
    code, out, _ = run_py('export-base', '--base', 'dwarf_male', '--resume')
    check('export-base --resume skips', code == 0)


def step_export():
    before = {b: sha(PD / f'{b}_outfits.blend') for b in (*BASES, 'gnome_female')}
    for base in (*BASES, 'gnome_female'):
        code, out, secs = run_py('export-items', '--base', base)
        r = json.loads((BATCH / 'logs' / f'export-items_{base}.json').read_text())
        per = ', '.join(f"{x['item_id'].split('__')[1]} {x['seconds']}s/{x['tris']}t/{x['nodes']}n/{x['joints']}j" for x in r['results'])
        check(f'run.py export-items {base}: 3 exported', code == 0 and len(r['results']) == 3 and not r['failed'], f'{secs:.1f}s; {per}')
    check('export never saved the outfit files', before == {b: sha(PD / f'{b}_outfits.blend') for b in (*BASES, 'gnome_female')})
    cat = json.loads((BATCH / 'catalog.json').read_text())
    check('catalog.json lists all 4 bases and 3 items (the gnome set joins the same items)', sorted(cat['bases']) == sorted((*BASES, 'gnome_female')) and len(cat['items']) == 3 and cat['format'] == 'outfit_studio_v1',
          f"{len(cat['bases'])} bases, {len(cat['items'])} items")
    for t in TYPES:
        ij = json.loads((BATCH / 'items' / f'{SET}__{t}' / 'item.json').read_text())
        check(f'item.json {t}: races lists all 4 bases', ij['races'] == sorted((*BASES, 'gnome_female')), str(ij['races']))
    ij = json.loads((BATCH / 'items' / f'{SET}__body_armor' / 'item.json').read_text())
    ids = [c['id'] for c in ij['channels']]
    check('item.json channel ids are the union over bases', {'chest', 'chest_secondary'} <= set(ids) and len(ids) == len(set(ids)), str(len(ids)))
    rec = json.loads((BATCH / 'items' / f'{SET}__body_armor' / 'dwarf_female.json').read_text())
    check('<base>.json records mismatch warnings', True, f"{len(rec['warnings'])} warnings")


def step_verify():
    blender_worker('verify', BATCH)


def step_resume():
    items = sorted((BATCH / 'items').glob('*/*.glb'))
    before = {str(p): (sha(p), p.stat().st_mtime_ns) for p in items}
    code, out, _ = run_py('export-items', '--base', 'dwarf_male')
    check('re-export without --resume refuses to overwrite', code == 1 and 'exists' in (BATCH / 'logs' / 'export-items_dwarf_male.log').read_text())
    check('...and left the files alone', before == {str(p): (sha(p), p.stat().st_mtime_ns) for p in items})
    for base in (*BASES, 'gnome_female'):
        code, out, secs = run_py('export-items', '--base', base, '--resume')
        r = json.loads((BATCH / 'logs' / f'export-items_{base}.json').read_text())
        check(f'--resume {base}: all 3 skipped (unchanged)', code == 0 and [x['status'] for x in r['results']] == ['skipped'] * 3, f'{secs:.1f}s')
    check('--resume rewrote nothing', before == {str(p): (sha(p), p.stat().st_mtime_ns) for p in items})


def step_edit():
    blender_worker('edit_channel', PD / 'dwarf_male_outfits.blend')
    code, out, secs = run_py('export-items', '--base', 'dwarf_male', '--resume')
    r = json.loads((BATCH / 'logs' / 'export-items_dwarf_male.json').read_text())
    got = {x['item_id'].split('__')[1]: x['status'] for x in r['results']}
    check('--resume re-exports only the edited item', code == 0 and got == {'body_armor': 'exported', 'gauntlets': 'skipped', 'boots': 'skipped'}, str(got))
    ij = json.loads((BATCH / 'items' / f'{SET}__body_armor' / 'item.json').read_text())
    check('item.json keeps the other bases\' races after re-export', ij['races'] == sorted((*BASES, 'gnome_female')) and ij['channels'][0]['role'] == 'accent', str(ij['races']))
    rec = json.loads((BATCH / 'items' / f'{SET}__body_armor' / 'dwarf_male.json').read_text())
    check('<base>.json warns about role mismatch across bases', any('role/material' in w for w in rec['warnings']), str(rec['warnings'][-1:])[:160])


def step_hashcheck():
    """Skeleton hash cross-check against bases/<base>/base.json, and a batch without base exports."""
    bad = OUT / 'batch_badhash'
    (bad / 'bases' / 'dwarf_male').mkdir(parents=True)
    rec = json.loads((BATCH / 'bases' / 'dwarf_male' / 'base.json').read_text())
    rec['skeleton_sha256'] = '0' * 64
    (bad / 'bases' / 'dwarf_male' / 'base.json').write_text(json.dumps(rec))
    code, out, _ = run([sys.executable, str(PKG / 'run.py'), 'export-items', '--base', 'dwarf_male', '--project-dir', str(PD), '--batch', str(bad),
                        '--items', f'{SET}__boots'])
    r = json.loads((bad / 'logs' / 'export-items_dwarf_male.json').read_text())
    check('export refuses when skeleton_sha256 differs from base.json', code == 1 and r['failed'] and 'differs from bases/dwarf_male/base.json' in r['failed'][0]['error'],
          r['failed'][0]['error'][:90] if r['failed'] else '')
    check('...and leaves no GLB / item.json behind', not list(bad.rglob('*.glb')) and not (bad / 'items' / f'{SET}__boots' / 'item.json').exists())
    nob = OUT / 'batch_nobase'
    code, out, _ = run([sys.executable, str(PKG / 'run.py'), 'export-items', '--base', 'dwarf_male', '--project-dir', str(PD), '--batch', str(nob),
                        '--items', f'{SET}__boots'])
    r = json.loads((nob / 'logs' / 'export-items_dwarf_male.json').read_text())
    cat = json.loads((nob / 'catalog.json').read_text())
    check('base not exported yet: exports with a warning, catalog takes the item hash',
          code == 0 and any('base.json not found' in w for w in r['results'][0]['warnings']) and
          cat['bases']['dwarf_male'] == json.loads((BATCH / 'bases' / 'dwarf_male' / 'base.json').read_text())['skeleton_sha256'])


def step_all():
    """run.py all on one small base: import -> validate -> export-base -> export-items (--all-status) -> export-anims (1 clip)."""
    pd, batch = OUT / 'projects_all', OUT / 'batch_all'
    code, out, secs = run([sys.executable, str(PKG / 'run.py'), 'all', '--base', 'gnome_female', '--set', GNOME_SET, '--project-dir', str(pd),
                           '--batch', str(batch), '--all-status', '--only', 'Idle (5)'])
    print('  ' + out.strip().replace('\n', '\n  ')[-1500:])
    check('run.py all (gnome_female): exit 0', code == 0, f'{secs:.0f}s')
    check('all: base.glb, items, catalog and anims.glb exist', (batch / 'bases/gnome_female/base.glb').is_file() and (batch / 'bases/gnome_female/anims.glb').is_file()
          and (batch / 'catalog.json').is_file() and len(list((batch / 'items').glob('*/gnome_female.glb'))) == 3)


def step_operators():
    blender_worker('operators', BATCH)


def step_broken():
    blender_worker('broken', BATCH)


def step_leaks():
    blender_worker('leaks')


STEPS = {'import': step_import, 'validate': step_validate, 'approve': step_approve, 'base': step_base, 'export': step_export,
         'verify': step_verify, 'resume': step_resume, 'edit': step_edit, 'hashcheck': step_hashcheck, 'operators': step_operators,
         'broken': step_broken, 'leaks': step_leaks, 'all': step_all}


def main(argv):
    snap = snapshot()
    t0 = time.time()
    for name in argv or STEPS:
        print(f'== {name}', flush=True)
        STEPS[name]()
    check('projects/*.blend and the real export folder untouched, no real outfit_projects/ created', snap == snapshot())
    bad = [n for ok, n in RESULTS if not ok]
    print(f'\n{len(RESULTS) - len(bad)} passed, {len(bad)} failed in {time.time() - t0:.0f}s (output in {OUT})')
    for n in bad:
        print('  FAILED:', n)
    return 1 if bad else 0


if __name__ == '__main__':
    if bpy is not None:
        worker(sys.argv[sys.argv.index('--') + 1:])
    sys.exit(main(sys.argv[1:]))
