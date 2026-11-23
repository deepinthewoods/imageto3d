"""Item export (SPEC section 7, material schema 2 of 2026-11-20): one GLB per item and base, skinned to that base's
exported skeleton.

`export_items(project_path, batch_dir)` runs in a Blender process (the batch, or the operator's background
subprocess) and works on the in-memory session only: it opens the SAVED outfit file once per item, so every export starts
from a pristine session, and it never saves it. Per item: validate, give the pieces ONE MATERIAL PER COLOUR ROLE (named
exactly the role: `primary`, `secondary`, `tertiary`, `accent`, `joint`; each face's role is its channel's role alias),
add the section connectors (base-body meshes `conn_<bone>_<i>`, material `skin`, skinned like the base body), repair
weights (`fix_weights`), zero the shape keys, export through ARP (Godot, Humanoid) and rewrite the GLB: material names
become the plain role names and every item primitive gets `COLOR_0.r = role_index / 8` (the per-vertex role index).
Then `<base>.json` (node -> materials, kind, bones, sections), `item.json` (merged across bases: roles, role encoding,
section states per base, input choice per base) and `catalog.json` are written under `<batch>/items/<item_id>/`.
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

from .. import paths
from ..core import channels as chan
from ..core import morph_transfer as mt
from ..core import pipeline_source as ps
from ..core import transform as tf
from . import arp_export as ae
from . import importer, items as it, morphs, project, validate, weights

VERSION = 2                      # <base>.json / item.json / catalog.json version (material schema 2)
SCHEMA = 2
PREFIX = 'OSCH_'
PIECE_PREFIX = 'piece_'           # glTF node of a piece: `piece_<piece>` (ARP refuses object names over 56 characters)
SKIN = ps.SKIN_MATERIAL          # material of the connector meshes
ROLES = chan.ROLES               # role index = position in this tuple
ROLE_INDEX = {r: i for i, r in enumerate(ROLES)}
ROLE_SCALE = 8                   # COLOR_0.r = role_index / ROLE_SCALE
ROLE_ENCODING = {'material': 'one glTF material per colour role, named exactly the role',
                 'vertex_attribute': 'COLOR_0', 'component': 'r', 'scale': ROLE_SCALE,
                 'encode': 'COLOR_0.r = role_index / 8 (g = b = 0, a = 1)', 'decode': 'role_index = round(COLOR_0.r * 8)',
                 'role_index': ROLE_INDEX}
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

def ordered_roles(roles):
    roles = set(roles)
    return [r for r in ROLES if r in roles] + sorted(roles - set(ROLES))


def merge_item_json(old, rec, base):
    """Merge this base's item record into the existing `item.json`. -> (new item.json, warnings).
    Races and roles (union) of other bases are kept, and so are their per-base blocks (`sections`, `connectors`,
    `input`, each keyed by base). The channel list of the old schema is dropped."""
    old = old or {}
    warnings = []
    if old.get('type') not in (None, rec['type']):
        warnings.append(f"type changed from {old['type']} to {rec['type']}")
    if old and old.get('material_schema') != SCHEMA:
        warnings.append(f"the existing item.json has material_schema {old.get('material_schema')!r}; its channel data is dropped")
    out = {**{k: v for k, v in old.items() if k != 'channels'}, 'version': VERSION, 'material_schema': SCHEMA, 'id': rec['id'],
           'label': rec['label'], 'type': rec['type'], 'roles': ordered_roles(set(rec['roles']) | set(old.get('roles', []))),
           'role_encoding': ROLE_ENCODING, 'skin_material': SKIN, 'races': sorted({base} | set(old.get('races', [])))}
    for key in ('sections', 'connectors', 'input'):
        out[key] = {**old.get(key, {}), base: rec[key]}
    return out, warnings


def merge_catalog(cat, base, sha, item):
    """Add `base` (skeleton hash) and the item summary to catalog.json without touching other bases' entries."""
    cat = cat or {'version': VERSION, 'format': 'outfit_studio_v2', 'material_schema': SCHEMA, 'bases': {}, 'items': []}
    if cat['bases'].get(base, sha) != sha:
        raise ValueError(f"{base}: skeleton hash {sha[:12]} differs from {cat['bases'][base][:12]} of earlier exports in this batch")
    cat['bases'][base] = sha
    cat['items'] = sorted([i for i in cat['items'] if i['id'] != item['id']] + [item], key=lambda i: i['id'])
    cat['bases'] = dict(sorted(cat['bases'].items()))
    return cat


# ------------------------------------------------------------------------------------------------- GLB

def inspect_glb(doc, binary):
    """({node name: {piece, kind piece|connector, primitives [material names], bones {joint: mean weight}}}, triangle count,
    morph names, joints, {max_influences, max_sum_error}). Checks that every role primitive carries its COLOR_0 role index."""
    acc = ae.ce('native_audit').accessor
    nodes = doc['nodes']
    joints = [nodes[i]['name'] for i in doc['skins'][0]['joints']]
    out, tris, targets, worst = {}, 0, set(), {'max_influences': 0, 'max_sum_error': 0.}
    for n in nodes:
        if 'mesh' not in n:
            continue
        mesh = doc['meshes'][n['mesh']]
        targets |= set(mesh.get('extras', {}).get('targetNames', []))
        prims, mass, count, n_tris = [], {}, 0, 0
        for p in mesh['primitives']:
            name = doc['materials'][p['material']]['name']
            prims.append(name)
            n_tris += doc['accessors'][p['indices']]['count'] // 3
            ji, w = acc(doc, binary, p['attributes']['JOINTS_0']), acc(doc, binary, p['attributes']['WEIGHTS_0'])
            for j in np.unique(ji):
                mass[joints[j]] = mass.get(joints[j], 0.) + float(w[(ji == j)].sum())
            count += len(w)
            worst['max_influences'] = max(worst['max_influences'], int((w > 1e-8).sum(1).max()))
            worst['max_sum_error'] = max(worst['max_sum_error'], float(np.abs(w.sum(1) - 1).max()))
            if name in ROLE_INDEX:
                if 'COLOR_0' not in p['attributes']:
                    raise ValueError(f"{n['name']}: primitive {name!r} has no COLOR_0 role index")
                got = np.round(acc(doc, binary, p['attributes']['COLOR_0'])[:, 0] * ROLE_SCALE)
                if not (got == ROLE_INDEX[name]).all():
                    raise ValueError(f"{n['name']}: COLOR_0.r does not decode to the role index of {name!r}")
        piece = n['name'].removeprefix(PIECE_PREFIX)
        kind = 'connector' if set(prims) == {SKIN} else 'piece'
        tris += n_tris
        out[n['name']] = {'piece': piece, 'kind': kind, 'primitives': prims, 'tris': n_tris,
                          'bones': {k: round(v / count, 6) for k, v in mass.items() if v > 1e-9}}
    return out, tris, sorted(targets), joints, worst


def add_role_colour(doc, binary):
    """Write `COLOR_0` (r = role_index / 8, g = b = 0, a = 1, float) on every primitive whose material is a role. -> binary."""
    binary = bytearray(binary)
    for mesh in doc.get('meshes', []):
        for prim in mesh['primitives']:
            role = doc['materials'][prim['material']]['name']
            if role not in ROLE_INDEX:
                continue
            n = doc['accessors'][prim['attributes']['POSITION']]['count']
            col = np.zeros((n, 4), '<f4')
            col[:, 0], col[:, 3] = ROLE_INDEX[role] / ROLE_SCALE, 1.
            binary.extend(b'\0' * (-len(binary) % 4))
            doc['bufferViews'].append({'buffer': 0, 'byteOffset': len(binary), 'byteLength': col.nbytes, 'target': 34962})
            binary.extend(col.tobytes())
            prim['attributes']['COLOR_0'] = len(doc['accessors'])
            doc['accessors'].append({'bufferView': len(doc['bufferViews']) - 1, 'componentType': 5126, 'count': n, 'type': 'VEC4'})
    return bytes(binary)


def finish_glb(path, item_id, base):
    """Material names -> plain role names (`skin` for connectors), `COLOR_0` role index, scene extras scrubbed,
    outfit_studio metadata added. -> (doc, binary)."""
    ng = ae.ce('native_glb')
    doc, binary = ae.ce('gltf').read(path)
    for m in doc.get('materials', []):
        m['name'] = m['name'].removeprefix(PREFIX)
    binary = add_role_colour(doc, binary)
    for s in doc.get('scenes', []):
        s.pop('extras', None)
    doc['asset'].setdefault('extras', {})['outfit_studio'] = {'item_id': item_id, 'base': base, 'version': VERSION,
                                                              'material_schema': SCHEMA, 'role_scale': ROLE_SCALE}
    ng.write(path, doc, binary)
    return doc, binary


# ------------------------------------------------------------------------------------------------- session prep

def role_material(role):
    """Material `OSCH_<role>` (a copy of the preview material of the role; the prefix is stripped from the GLB)."""
    name = PREFIX + role
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = it.role_material(role).copy()
        mat.name = name
    return mat


def skin_material():
    """Material `OSCH_skin` for the connector meshes (the base body's colour)."""
    name = PREFIX + SKIN
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        mat.diffuse_color = (.8, .72, .66, 1.)
    return mat


def role_slots(pieces, channels):
    """Rebuild the material slots of the pieces as ONE slot per role (face role = its channel's role alias) and remap the
    faces. -> {piece name: [roles used]}."""
    used = {}
    for obj in pieces:
        me = obj.data
        ch = np.zeros(len(me.polygons), np.int32)
        me.attributes['os_channel'].data.foreach_get('value', ch)
        roles = np.array([channels[i]['role'] for i in ch])
        order = ordered_roles(roles.tolist())
        me.materials.clear()                                  # (this resets the face slot indices: set them afterwards)
        for r in order:
            me.materials.append(role_material(r))
        me.polygons.foreach_set('material_index', np.array([order.index(r) for r in roles], np.int32))
        used[obj.get('os_piece', obj.name)] = order
    return used


def build_connectors(d, rig, base_obj, rest, deltas):
    """The section connector meshes of the item (see ps.item_sections) as skinned objects in a throw-away collection of
    this session: geometry from `connectors.glb` (rest pose, in game world = Blender world), mirrored for
    symmetric connectors, weights transferred from the base, `bulk_*` / `head_*` morphs, material `skin`. -> [objects]."""
    sec = d.get('sections') or {}
    conns = sec.get('connectors') or {}
    if not conns:
        return []
    glb = sec.get('connectors_glb')
    if not glb or not Path(glb).is_file():
        raise FileNotFoundError(f'connectors file {glb} is missing (needed for {sorted(conns)})')
    nodes = ps.read_glb_nodes(glb)
    coll = bpy.data.collections.new('OS_connectors_tmp')
    bpy.context.scene.collection.children.link(coll)
    mat, out = skin_material(), []
    for name in sorted(conns):
        info = conns[name]
        src = info.get('mirror_of') or name
        if src not in nodes:
            raise ValueError(f'{Path(glb).name} has no node {src} (connector {name})')
        V, F = ps.weld(nodes[src]['v'], nodes[src]['f'])
        F = F[ps.tri_areas(V, F) > 1e-12]                     # the glTF exporter drops degenerate triangles anyway
        if info.get('mirror_of'):
            V, F = tf.mirror_mesh(V, F, sec.get('plane_x') if sec.get('plane_x') is not None else rest['plane_x'])
        me = bpy.data.meshes.new(name)
        me.from_pydata(V.tolist(), [], F.tolist())
        me.update()
        me.shade_flat()
        me.materials.append(mat)
        obj = bpy.data.objects.new(name, me)
        obj['os_connector'] = info['section']
        coll.objects.link(obj)
        tri, bary, _ = mt.closest_points(V, rest['V'], rest['F'])
        probe = {'tri': tri, 'bary': bary}
        weights.bind_connector(obj, rig, rest, base_obj, probe)
        morphs.transfer(obj, base_obj, probe, rest['F'], deltas)
        out.append(obj)
    return out


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
    """Hash of everything that decides the exported bytes: pieces (geometry, weights, channels), morph keys, channels,
    the section states / connectors and the input choice."""
    h = hashlib.sha256()
    sec = d.get('sections') or {}
    h.update(json.dumps([VERSION, d.get('item_id'), d.get('type'), d.get('label'), d.get('channels'), rig_fp, base_sha,
                         sec.get('states'), sec.get('connectors'), sec.get('connectors_sha256'), sec.get('plane_x'),
                         (d.get('source') or {}).get('input')], sort_keys=True, default=str).encode())
    h.update(json.dumps(it.piece_hashes(coll), sort_keys=True).encode())
    for obj in it.item_pieces(coll):
        for kb in (obj.data.shape_keys.key_blocks if obj.data.shape_keys else []):
            co = np.empty(len(kb.data) * 3, np.float32)
            kb.data.foreach_get('co', co)
            h.update(kb.name.encode() + np.round(co, 5).tobytes())
    return h.hexdigest()


def base_section_names(base_json):
    """Section node names listed by the base export's base.json `sections` (names, or records with node / name), or None."""
    secs = (base_json or {}).get('sections')
    if not secs:
        return None
    names = set()
    for k, v in (secs.items() if isinstance(secs, dict) else enumerate(secs)):
        if isinstance(v, str):
            names.add(v)
        elif isinstance(v, dict):
            names.add(v.get('node') or v.get('name') or (k if isinstance(k, str) else None))
        elif isinstance(k, str):
            names.add(k)
    return {n for n in names if isinstance(n, str)}


# ------------------------------------------------------------------------------------------------- one item

def rel(path):
    """Path relative to the imageto3d root when inside it (portable item records), else as given."""
    try:
        return str(Path(path).resolve().relative_to(paths.ROOT.resolve())).replace('\\', '/')
    except (ValueError, OSError):
        return str(path)


def export_one(item_id, base, batch, resume=False, log=print, allow_no_sections=False):
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
    sec = d.get('sections') or {}
    states = sec.get('states')
    if states is None and not allow_no_sections:
        raise ValueError(f"{item_id}: no body sections ({'; '.join(sec.get('warnings', [])) or 'import it again'}); "
                         'without them the body shows through the item (allow_no_sections to export anyway)')
    if states is None:
        warnings.append('exported WITHOUT body sections (allow_no_sections)')
    names = base_section_names(base_json)
    if states is not None and names is not None:         # the states are listed per section of the exported base
        extra = {k: v for k, v in states.items() if k not in names}
        if any(v != 'show' for v in extra.values()):
            raise ValueError(f'{item_id}: sections {extra} are not sections of bases/{base}/base.json')
        lost = sorted(names - set(states))
        if lost:
            warnings.append(f'base sections without a record entry (state show): {lost[:6]}')
        states = {n: states.get(n, 'show') for n in sorted(names)}
        bad = [c for c, i in (sec.get('connectors') or {}).items() if i['section'] not in names]
        if bad:
            raise ValueError(f'{item_id}: connectors {bad} belong to sections that bases/{base}/base.json does not have')
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
    # in-memory preparation (the session is never saved)
    show(coll, pieces)
    expected = role_slots(pieces, channels)
    for o in pieces:                                          # node names of this session only (it is never saved)
        o.name = PIECE_PREFIX + o.get('os_piece', o.name)
    note = match_base_skeleton(rig, base_obj, pieces)
    warnings += [note] if note else []
    rest = importer.rest_context(rig, base_obj)
    conns = build_connectors(d, rig, base_obj, rest, morphs.base_deltas(base_obj))
    show(coll, conns)
    objs = [*pieces, *conns]
    repaired = ae.fix_weights(objs, rig)
    for o in objs:
        for kb in (o.data.shape_keys.key_blocks if o.data.shape_keys else []):
            kb.value = 0.
    ctx.scene.pop(project.META_KEY, None)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f'{base}.partial.glb'
    try:
        audit = run_export(tmp, rig, base_obj, objs)
        doc, binary = finish_glb(tmp, item_id, base)
        nodes, tris, morph_names, joints, wstats = inspect_glb(doc, binary)
        if base_sha and audit['skeleton_sha256'] != base_sha:
            raise ValueError(f"skeleton_sha256 {audit['skeleton_sha256'][:12]} differs from bases/{base}/base.json {base_sha[:12]}")
        if base_json and base_json.get('joints') and base_json['joints'] != joints:
            raise ValueError('skin joint names differ from bases/%s/base.json' % base)
        if wstats['max_influences'] > 4 or wstats['max_sum_error'] > 1e-5:
            raise ValueError(f'exported weights break the contract: {wstats}')
        got_conns = {k for k, n in nodes.items() if n['kind'] == 'connector'}
        if got_conns != {o.name for o in conns}:
            raise ValueError(f'connector nodes {sorted(got_conns)} differ from the planned {sorted(o.name for o in conns)}')
        for name, n in nodes.items():
            if n['kind'] == 'connector':
                continue
            if sorted(n['primitives']) != sorted(expected.get(n['piece'], [])):
                raise ValueError(f"{name}: primitives {n['primitives']} differ from the piece's roles {expected.get(n['piece'])}")
        short = {o.name: (nodes[o.name]['tris'], len(o.data.polygons)) for o in objs
                 if o.name in nodes and nodes[o.name]['tris'] != len(o.data.polygons)}
        if short or tris != sum(len(o.data.polygons) for o in objs):
            raise ValueError(f'triangles in the GLB differ from the meshes (node: glb, mesh): {short or (tris, len(objs))}')
        os.replace(tmp, glb)
    finally:
        tmp.unlink(missing_ok=True)
    node_rec = {}
    for name, n in nodes.items():
        if n['kind'] == 'connector':
            node_rec[name] = {'kind': 'connector', 'piece': name, 'section': sec['connectors'][name]['section'],
                              'primitives': n['primitives'], 'rigid_bone': None, 'rigid_bones': None}
            continue
        bones = n['bones'] if d['type'] != 'outfit' and weights.rigid_bones(n['piece']) else None
        node_rec[name] = {'kind': 'piece', 'piece': n['piece'], 'primitives': n['primitives'],
                          'rigid_bone': max(bones, key=bones.get) if bones else None, 'rigid_bones': bones}
    src = d.get('source', {})
    inp = {'kind': (src.get('input') or {}).get('kind'), 'choice': (src.get('input') or {}).get('choice'), 'lod': src.get('lod'),
           'file': rel(src.get('wearable_glb', '')) if src.get('wearable_glb') else None, 'sha256': src.get('wearable_sha256'),
           'job_id': src.get('job_id'), 'module': src.get('module'), 'sections_record': rel(sec['record']) if sec.get('record') else None,
           'model_approved': (src.get('input') or {}).get('model_approved')}
    roles = ordered_roles({r for n in nodes.values() if n['kind'] == 'piece' for r in n['primitives']})
    rec = {'version': VERSION, 'material_schema': SCHEMA, 'item_id': item_id, 'base': base, 'skeleton_sha256': audit['skeleton_sha256'],
           'nodes': node_rec, 'pieces': [n['piece'] for n in nodes.values() if n['kind'] == 'piece'],
           'connectors': sorted(got_conns), 'roles': roles, 'sections': states or {}, 'morphs': morph_names, 'tris': tris,
           'weights': wstats, 'export_key': key, 'rig_fingerprint': rig_fp, 'glb_sha256': sha256(glb),
           'weights_repaired': {k: len(v) for k, v in repaired.items() if v}, 'input': inp,
           'source': {k: src.get(k) for k in ('job_id', 'module', 'race', 'lod', 'wearable_sha256', 'pieces_source')},
           'warnings': warnings}
    item_rec = {'id': item_id, 'label': d.get('label', item_id), 'type': d['type'], 'roles': roles,
                'sections': states or {}, 'connectors': sorted(got_conns), 'input': inp}
    item_json, more = merge_item_json(read_json(out_dir / 'item.json'), item_rec, base)
    rec['warnings'] = warnings + more
    dump_json(js, rec)
    dump_json(out_dir / 'item.json', item_json)
    cat = merge_catalog(read_json(Path(batch) / 'catalog.json'), base, audit['skeleton_sha256'],
                        {'id': item_id, 'label': item_json['label'], 'type': item_json['type'], 'races': item_json['races']})
    dump_json(Path(batch) / 'catalog.json', cat)
    log(f'[export_items] {base} {item_id}: {tris} tris, {len(nodes)} nodes ({len(got_conns)} connectors), {len(morph_names)} morphs, {time.time() - t0:.1f}s')
    return {'item_id': item_id, 'base': base, 'status': 'exported', 'glb': str(glb), 'tris': tris, 'nodes': len(nodes),
            'connectors': len(got_conns), 'joints': len(joints), 'morphs': len(morph_names), 'roles': roles,
            'skeleton_sha256': audit['skeleton_sha256'], 'warnings': warnings + more, 'seconds': round(time.time() - t0, 2)}


# ------------------------------------------------------------------------------------------------- entry point

def select_ids(scene, items=None, all_status=False):
    colls = it.list_items(scene)
    if items is not None:
        want = set(items)
        colls = [c for c in colls if it.item_data(c).get('item_id') in want or c.name in want]
    elif not all_status:
        colls = [c for c in colls if it.item_data(c).get('status') in DEFAULT_STATUSES]
    return [it.item_data(c)['item_id'] for c in colls]


def export_items(project_path, batch_dir, items=None, all_status=False, resume=False, log=print, allow_no_sections=False):
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
            results.append(export_one(item_id, base, batch_dir, resume, log, allow_no_sections))
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
