"""Item export (SPEC section 7): one GLB per item and base, skinned to that base's exported skeleton.

`export_items(project_path, batch_dir)` runs in a Blender process (the batch, or the operator's background
subprocess) and works on the in-memory session only: it opens the SAVED outfit file once per item, so every export starts
from a pristine session, and it never saves it. Per item: validate, give every channel its own material
(`OSCH_<channel id>`), repair weights (`fix_weights`), zero the shape keys, export through ARP (Godot, Humanoid) and
rewrite the GLB so that material names are the plain channel ids. Then `<base>.json` (primitive -> channel map read back
from the GLB), `item.json` (merged across bases) and `catalog.json` are written under `<batch>/items/<item_id>/`.
The skeleton hash must equal `<batch>/bases/<base>/base.json` when that exists. Layout of the batch: see SPEC section 7.
Files that already exist are never overwritten unless `resume` is set (then unchanged items are skipped)."""
import hashlib
import json
import os
import time
from pathlib import Path

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, StringProperty

from . import arp_export as ae
from . import importer, items as it, project, validate, weights

VERSION = 1
PREFIX = 'OSCH_'
DEFAULT_STATUSES = ('approved', 'exported')


# ------------------------------------------------------------------------------------------------- files

def read_json(path, default=None):
    p = Path(path)
    return json.loads(p.read_text()) if p.is_file() else default


def dump_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, default=lambda v: float(v) if hasattr(v, '__float__') else list(v)) + '\n')
    os.replace(tmp, path)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


# ------------------------------------------------------------------------------------------------- merging (no bpy state)

def merge_item_json(old, rec, base):
    """Merge this base's item record into the existing `item.json`. -> (new item.json, warnings).
    Races of other bases are kept; channel ids are the union (this base's role/material win); mismatches warn."""
    warnings = []
    mine = {c['id']: c for c in rec['channels']}
    channels, races = list(rec['channels']), {base}
    if old:
        races |= set(old.get('races', []))
        theirs = old.get('channels', [])
        ids = [c['id'] for c in theirs]
        if set(ids) != set(mine):
            warnings.append(f"channel ids differ from the other bases: only on others {sorted(set(ids) - set(mine))}, "
                            f"only on {base} {sorted(set(mine) - set(ids))}")
        for c in theirs:
            m = mine.get(c['id'])
            if m and (m['role'], m.get('material', '')) != (c['role'], c.get('material', '')):
                warnings.append(f"channel {c['id']}: role/material {c['role']}/{c.get('material', '')!r} on other bases, "
                                f"{m['role']}/{m.get('material', '')!r} on {base} (kept {base}'s)")
        channels = [mine.get(c['id'], c) for c in theirs] + [c for c in rec['channels'] if c['id'] not in ids]
        if old.get('type') not in (None, rec['type']):
            warnings.append(f"type changed from {old['type']} to {rec['type']}")
    out = {**(old or {}), 'version': 1, 'id': rec['id'], 'label': rec['label'], 'type': rec['type'],
           'channels': channels, 'races': sorted(races)}
    return out, warnings


def merge_catalog(cat, base, sha, item):
    """Add `base` (skeleton hash) and the item summary to catalog.json without touching other bases' entries."""
    cat = cat or {'version': 1, 'format': 'outfit_studio_v1', 'bases': {}, 'items': []}
    if cat['bases'].get(base, sha) != sha:
        raise ValueError(f"{base}: skeleton hash {sha[:12]} differs from {cat['bases'][base][:12]} of earlier exports in this batch")
    cat['bases'][base] = sha
    cat['items'] = sorted([i for i in cat['items'] if i['id'] != item['id']] + [item], key=lambda i: i['id'])
    cat['bases'] = dict(sorted(cat['bases'].items()))
    return cat


# ------------------------------------------------------------------------------------------------- GLB

def inspect_glb(doc, binary):
    """{node name: {piece, primitives [material names], bones {joint: mean weight}}}, triangle count, morph names, joints."""
    acc = ae.ce('native_audit').accessor
    nodes = doc['nodes']
    joints = [nodes[i]['name'] for i in doc['skins'][0]['joints']]
    out, tris, morphs = {}, 0, set()
    for n in nodes:
        if 'mesh' not in n:
            continue
        mesh = doc['meshes'][n['mesh']]
        morphs |= set(mesh.get('extras', {}).get('targetNames', []))
        prims, mass, count = [], {}, 0
        for p in mesh['primitives']:
            prims.append(doc['materials'][p['material']]['name'])
            tris += doc['accessors'][p['indices']]['count'] // 3
            ji, w = acc(doc, binary, p['attributes']['JOINTS_0']), acc(doc, binary, p['attributes']['WEIGHTS_0'])
            for j in np.unique(ji):
                mass[joints[j]] = mass.get(joints[j], 0.) + float(w[(ji == j)].sum())
            count += len(w)
        piece = n.get('extras', {}).get('os_piece') or n['name'].rsplit('__', 1)[-1]
        out[n['name']] = {'piece': piece, 'primitives': prims, 'bones': {k: round(v / count, 6) for k, v in mass.items() if v > 1e-9}}
    return out, tris, sorted(morphs), joints


def finish_glb(path, item_id, base):
    """Material names -> plain channel ids, scene extras scrubbed, outfit_studio metadata added. -> (doc, binary)."""
    ng = ae.ce('native_glb')
    doc, binary = ae.ce('gltf').read(path)
    for m in doc.get('materials', []):
        m['name'] = m['name'].removeprefix(PREFIX)
    for s in doc.get('scenes', []):
        s.pop('extras', None)
    doc['asset'].setdefault('extras', {})['outfit_studio'] = {'item_id': item_id, 'base': base, 'version': VERSION}
    ng.write(path, doc, binary)
    return doc, binary


# ------------------------------------------------------------------------------------------------- session prep

def channel_materials(coll, channels):
    """One material `OSCH_<id>` per channel (preview colour of its role), put into slot <channel index> of every piece."""
    mats = {}
    for c in channels:
        name = PREFIX + c['id']
        mats[c['id']] = bpy.data.materials.get(name)
        if mats[c['id']] is None:
            mats[c['id']] = it.role_material(c['role']).copy()
            mats[c['id']].name = name
    for obj in it.item_pieces(coll):
        me = obj.data
        while len(me.materials) < len(channels):
            me.materials.append(None)
        for i, c in enumerate(channels):
            me.materials[i] = mats[c['id']]
    return mats


def show(coll, pieces):
    """Make sure the pieces can be selected for the export (collections excluded / hidden by a view)."""
    def walk(lc):
        for child in lc.children:
            if _has(child.collection, coll):
                child.exclude = False
                child.hide_viewport = False
                walk(child)
    walk(bpy.context.view_layer.layer_collection)
    coll.hide_viewport = False
    for o in pieces:
        o.hide_viewport = False
        o.hide_set(False)


def _has(parent, target):
    return parent == target or any(_has(c, target) for c in parent.children)


def make_local(objs):
    """Make the library-override objects (and their data) local in this never-saved session."""
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    bpy.ops.object.make_local(type='ALL')


def run_export(path, rig, base_obj, pieces):
    """ARP export of the pieces; if ARP refuses the library-override rig, make the session local (in memory) and retry."""
    try:
        return ae.export_glb(path, rig, pieces, twist=False)
    except Exception as first:
        print(f'[export_items] ARP export failed on the override rig ({first}); retrying with local data')
        make_local([rig, base_obj, *pieces])
        try:
            return ae.export_glb(path, rig, pieces, twist=False)
        except Exception as second:
            raise RuntimeError(f'ARP export failed: {first}; after make_local: {second}') from second


def match_base_skeleton(rig, base_obj, pieces):
    """Bring the rig of this session to what the base export exports: D's `prepare_session` repairs the body weights and
    re-fits the attachment-point contract when needed (human_male), which moves AP bones and so changes skeleton_sha256.
    The re-fit edits bones, which a library-override rig does not allow: the session is made local first when needed.
    Returns a warning text or None."""
    try:
        from . import export_base
    except ImportError:
        return 'blender.export_base not available: AP re-fit of the base export not replicated, skeleton hash may differ'
    try:
        refit = export_base.prepare_session(base_obj, rig)[3]
    except Exception as e:
        if 'linked' not in str(e) and 'editable' not in str(e):
            raise
        make_local([rig, base_obj, *pieces])
        refit = export_base.prepare_session(base_obj, rig)[3]
    return f'attachment contract re-fitted like the base export: {refit[:100]}' if refit else None


def export_key(coll, d, rig_fp, base_sha):
    """Hash of everything that decides the exported bytes: pieces (geometry, weights, channels), morph keys, channels."""
    h = hashlib.sha256()
    h.update(json.dumps([VERSION, d.get('item_id'), d.get('type'), d.get('label'), d.get('channels'), rig_fp, base_sha],
                        sort_keys=True).encode())
    h.update(json.dumps(it.piece_hashes(coll), sort_keys=True).encode())
    for obj in it.item_pieces(coll):
        for kb in (obj.data.shape_keys.key_blocks if obj.data.shape_keys else []):
            co = np.empty(len(kb.data) * 3, np.float32)
            kb.data.foreach_get('co', co)
            h.update(kb.name.encode() + np.round(co, 5).tobytes())
    return h.hexdigest()


# ------------------------------------------------------------------------------------------------- one item

def export_one(item_id, base, batch, resume=False, log=print):
    """Export one item of the open session; returns a result dict (`status`: exported | skipped)."""
    t0 = time.time()
    ctx = bpy.context
    rig, base_obj = importer.base_objects(ctx)
    coll = it.find_item(ctx.scene, item_id)
    if coll is None:
        raise KeyError(f'no item {item_id}')
    d = it.item_data(coll)
    out_dir = Path(batch) / 'items' / item_id
    glb, js = out_dir / f'{base}.glb', out_dir / f'{base}.json'
    base_json = read_json(Path(batch) / 'bases' / base / 'base.json')
    base_sha = base_json['skeleton_sha256'] if base_json else None
    warnings = [] if base_json else [f'bases/{base}/base.json not found: skeleton hash not cross-checked (export the base first)']
    rig_fp = project.rig_fingerprint(rig)
    key = export_key(coll, d, rig_fp, base_sha)
    if glb.exists() or js.exists():
        if not resume:
            raise FileExistsError(f'{glb} exists (use resume to skip unchanged items)')
        old = read_json(js, {})
        if glb.exists() and old.get('export_key') == key:
            return {'item_id': item_id, 'base': base, 'status': 'skipped', 'reason': 'unchanged', 'glb': str(glb)}
    found = validate.validate(ctx, [item_id], siblings=False)
    errors = [r for r in found if r['level'] == 'error']
    if errors:
        raise ValueError(f'{item_id}: {len(errors)} validation errors: ' + '; '.join(
            f"{r['piece']}: {r['code']} ({r['message'][:80]})" for r in errors[:5]))
    warnings += [f"{r['piece']}: {r['code']}: {r['message']}" for r in found if r['level'] == 'warning' and r['code'] != 'status']
    pieces, channels = it.item_pieces(coll), d['channels']
    expected = {}
    for o in pieces:
        v = np.empty(len(o.data.polygons), np.int32)
        o.data.attributes['os_channel'].data.foreach_get('value', v)
        expected[o.get('os_piece', o.name)] = sorted({channels[i]['id'] for i in np.unique(v)})
    # in-memory preparation (the session is never saved)
    show(coll, pieces)
    channel_materials(coll, channels)
    note = match_base_skeleton(rig, base_obj, pieces)
    warnings += [note] if note else []
    repaired = ae.fix_weights(pieces, rig)
    for o in pieces:
        for kb in (o.data.shape_keys.key_blocks if o.data.shape_keys else []):
            kb.value = 0.
    ctx.scene.pop(project.META_KEY, None)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f'{base}.partial.glb'
    try:
        audit = run_export(tmp, rig, base_obj, pieces)
        doc, binary = finish_glb(tmp, item_id, base)
        nodes, tris, morphs, joints = inspect_glb(doc, binary)
        if base_sha and audit['skeleton_sha256'] != base_sha:
            raise ValueError(f"skeleton_sha256 {audit['skeleton_sha256'][:12]} differs from bases/{base}/base.json {base_sha[:12]}")
        if base_json and base_json.get('joints') and base_json['joints'] != joints:
            raise ValueError('skin joint names differ from bases/%s/base.json' % base)
        ids = {c['id'] for c in channels}
        for name, n in nodes.items():
            if not set(n['primitives']) <= ids:
                raise ValueError(f"{name}: primitives use unknown channels {sorted(set(n['primitives']) - ids)}")
            if sorted(n['primitives']) != expected.get(n['piece'], []):
                raise ValueError(f"{name}: primitives {n['primitives']} differ from the piece's channels {expected.get(n['piece'])}")
        if tris != sum(len(o.data.polygons) for o in pieces):
            raise ValueError(f'GLB has {tris} triangles, the pieces have {sum(len(o.data.polygons) for o in pieces)}')
        os.replace(tmp, glb)
    finally:
        tmp.unlink(missing_ok=True)
    node_rec = {}
    for name, n in nodes.items():
        bones = n['bones'] if d['type'] != 'outfit' and weights.rigid_bones(n['piece']) else None
        node_rec[name] = {'piece': n['piece'], 'primitives': n['primitives'],
                          'rigid_bone': max(bones, key=bones.get) if bones else None, 'rigid_bones': bones}
    src = d.get('source', {})
    rec = {'version': 1, 'item_id': item_id, 'base': base, 'skeleton_sha256': audit['skeleton_sha256'], 'nodes': node_rec,
           'pieces': [n['piece'] for n in nodes.values()], 'morphs': morphs, 'tris': tris, 'export_key': key,
           'rig_fingerprint': rig_fp, 'glb_sha256': sha256(glb), 'weights_repaired': {k: len(v) for k, v in repaired.items() if v},
           'source': {k: src.get(k) for k in ('job_id', 'module', 'race', 'lod', 'wearable_sha256', 'pieces_source')},
           'warnings': warnings}
    item_rec = {'id': item_id, 'label': d.get('label', item_id), 'type': d['type'],
                'channels': [{'id': c['id'], 'label': c.get('label', c['id']), 'role': c['role'], 'material': c.get('material', '')}
                             for c in channels]}
    item_json, more = merge_item_json(read_json(out_dir / 'item.json'), item_rec, base)
    rec['warnings'] = warnings + more
    dump_json(js, rec)
    dump_json(out_dir / 'item.json', item_json)
    cat = merge_catalog(read_json(Path(batch) / 'catalog.json'), base, audit['skeleton_sha256'],
                        {'id': item_id, 'label': item_json['label'], 'type': item_json['type'], 'races': item_json['races']})
    dump_json(Path(batch) / 'catalog.json', cat)
    log(f'[export_items] {base} {item_id}: {tris} tris, {len(nodes)} nodes, {len(morphs)} morphs, {time.time() - t0:.1f}s')
    return {'item_id': item_id, 'base': base, 'status': 'exported', 'glb': str(glb), 'tris': tris, 'nodes': len(nodes),
            'joints': len(joints), 'morphs': len(morphs), 'skeleton_sha256': audit['skeleton_sha256'],
            'warnings': warnings + more, 'seconds': round(time.time() - t0, 2)}


# ------------------------------------------------------------------------------------------------- entry point

def select_ids(scene, items=None, all_status=False):
    colls = it.list_items(scene)
    if items is not None:
        want = set(items)
        colls = [c for c in colls if it.item_data(c).get('item_id') in want or c.name in want]
    elif not all_status:
        colls = [c for c in colls if it.item_data(c).get('status') in DEFAULT_STATUSES]
    return [it.item_data(c)['item_id'] for c in colls]


def export_items(project_path, batch_dir, items=None, all_status=False, resume=False, log=print):
    """Export the approved items (or `items` ids, or everything with `all_status`) of an outfit project.
    -> {'base', 'results': [per item dict], 'failed': [..]}. The outfit file is only read."""
    project_path, batch_dir = Path(project_path), Path(batch_dir)

    def reopen():
        bpy.ops.wm.open_mainfile(filepath=str(project_path))
        ae.enable_arp()

    reopen()
    base = project.base_name(bpy.context.scene)
    ids = select_ids(bpy.context.scene, items, all_status)
    results, failed = [], []
    for n, item_id in enumerate(ids):
        if n:
            reopen()                              # the previous export mutated the session
        try:
            results.append(export_one(item_id, base, batch_dir, resume, log))
        except Exception as e:
            import traceback
            traceback.print_exc()
            failed.append({'item_id': item_id, 'base': base, 'status': 'failed', 'error': str(e)})
    if not ids:
        log(f'[export_items] {base}: no items to export (status approved/exported; use all_status or name items)')
    return {'base': base, 'results': results, 'failed': failed}


# ------------------------------------------------------------------------------------------------- operator

def start_export(context, scope='APPROVED', batch_name=None, resume=True):
    """Launch the background export of the saved file. -> (Popen, info). Raises ValueError when it cannot start."""
    from .. import batch as bt
    if not bpy.data.filepath:
        raise ValueError('Save the outfit project first')
    if bpy.data.is_dirty:
        raise ValueError('The file has unsaved changes: save it first (the saved copy is exported)')
    base = project.base_name(context.scene)
    ids = None
    if scope == 'CURRENT':
        coll = it.current_item(context)
        if coll is None:
            raise ValueError('no current item')
        ids = [it.item_data(coll)['item_id']]
    out = bt.resolve_batch(batch_name or None, resume=resume)
    info = {'base': base, 'out': out, 'result': out / 'logs' / f'export-items_{base}.json', 'log': out / 'logs' / f'export-items_{base}.log'}
    info['result'].unlink(missing_ok=True)
    argv = ['--base', base, '--batch', out, '--project-dir', Path(bpy.data.filepath).parent, '--result', info['result']]
    argv += (['--items', ','.join(ids)] if ids else []) + (['--all-status'] if scope == 'ALL' else []) + (['--resume'] if resume else [])
    return bt.spawn('export-items', argv, info['log']), info


def finish_export(scene, code, info):
    """Read the background run's result and set status `exported` on the items it exported. -> (done ids, failed list)."""
    r = read_json(info['result'])
    if r is None:
        return [], [{'error': f"export failed (exit {code}); see {info['log']}"}]
    done = mark_exported(scene, [x['item_id'] for x in r.get('results', []) if x['status'] in ('exported', 'skipped')])
    return done, r.get('failed', [])


class OSTUDIO_OT_export_items(bpy.types.Operator):
    bl_idname = 'ostudio.export_items'
    bl_label = 'Export items'
    bl_description = ('Export the current / all approved items in a background Blender on the saved file '
                      '(this session is not modified; status becomes "exported" when it succeeds)')
    scope: EnumProperty(name='Scope', items=[('CURRENT', 'Current item', ''), ('APPROVED', 'All approved items', ''),
                                             ('ALL', 'Every item (any status)', '')], default='APPROVED')
    batch: StringProperty(name='Batch', description='Export batch folder name (empty = today, v1)')
    resume: BoolProperty(name='Resume', default=True, description='Skip items whose export is unchanged; without it existing files are an error')
    _proc = None
    _timer = None
    _info = None

    @classmethod
    def poll(cls, context):
        return project.mode() == 'outfit' and bool(bpy.data.filepath)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        try:
            self._proc, self._info = start_export(context, self.scope, self.batch, self.resume)
        except ValueError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self._timer = context.window_manager.event_timer_add(1.0, window=context.window)
        context.window_manager.modal_handler_add(self)
        self.report({'INFO'}, f"Exporting {self._info['base']} items to {self._info['out']} ...")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        code = self._proc.poll()
        if code is None:
            return {'PASS_THROUGH'}
        context.window_manager.event_timer_remove(self._timer)
        done, failed = finish_export(context.scene, code, self._info)
        self.report({'ERROR' if failed or code else 'INFO'},
                    f"{len(done)} exported, {len(failed)} failed" + (f": {failed[0]['error'][:120]}" if failed else ''))
        return {'FINISHED'} if not failed else {'CANCELLED'}


def mark_exported(scene, item_ids):
    """Set status `exported` on the open file's items (called when the background export reports success)."""
    done = []
    for item_id in item_ids:
        coll = it.find_item(scene, item_id)
        if coll is None:
            continue
        d = it.item_data(coll)
        if d.get('status') in ('approved', 'exported'):
            d['status'] = 'exported'
            it.set_item_data(coll, d)
            done.append(item_id)
    return done


CLASSES = (OSTUDIO_OT_export_items,)


def draw(layout, ctx):
    box = layout.box()
    box.label(text='Export items')
    col = box.column(align=True)
    col.operator('ostudio.export_items', text='Export current', icon='EXPORT').scope = 'CURRENT'
    col.operator('ostudio.export_items', text='Export all approved', icon='EXPORT').scope = 'APPROVED'
    if not bpy.data.filepath or bpy.data.is_dirty:
        box.label(text='Save the file first: the saved copy is exported.', icon='INFO')
