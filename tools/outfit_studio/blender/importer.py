"""Import pipeline wearables as items on the base of the open project (SPEC section 3)."""
import json
import re
import sys
from pathlib import Path

import bpy
import numpy as np

from .. import paths
from ..core import hashing
from ..core import morph_transfer as mt
from ..core import pipeline_source as ps
from ..core import transform as tf
from . import items, morphs, weights

TRIPO = paths.ROOT / 'tools' / 'tripo_pipeline'
POOR_FIT_PCT = 4.           # median distance (% of height) above which an import carries a warning
FIT_FAIL_PCT = 20.          # median distance of the item to the body, % of height, above which an import is refused (bulky
                            # armour legitimately sits 10 % away: orc_male gauntlets)
FIT_RESIDUAL_FAIL_PCT = 3.  # ICP residual of the base fit, % of height, above which an import is refused
COMPARE_OFFSET = 1.         # metres: a compared re-import sits this far to +X


# ------------------------------------------------------------------------------------------------- base access

def base_objects(context=None):
    """(rig, base): the armature `rig` and mesh `base`, preferring the library overrides of an outfit project
    (also works in a plain base project, where they are ordinary local objects)."""
    def pick(name, kind):
        found = [o for o in bpy.data.objects if o.type == kind and o.name.split('.')[0] == name]
        found.sort(key=lambda o: (o.override_library is None, o.library is not None, o.name != name, o.name))
        return found[0] if found else None
    rig, base = pick('rig', 'ARMATURE'), pick('base', 'MESH')
    if rig is None or base is None:
        raise RuntimeError('no base (mesh "base" + armature "rig") in this file: open a base or an outfit project')
    return rig, base


def base_name(context=None):
    """Base (race_gender) of the open project: scene record, else the file name."""
    scene = (context or bpy.context).scene
    try:
        name = json.loads(scene['outfit_studio']).get('base')
    except (KeyError, TypeError, ValueError):
        name = None
    return name or re.sub(r'_outfits$', '', Path(bpy.data.filepath).stem)


def base_rest_mesh(base, rig):
    """(V (n,3) world, F (m,3) triangles, part_per_face (m,) int into part_names, part_names) of `base` evaluated in
    REST pose (the pose position is restored afterwards). Parts come from the `bone_<part>` material slots."""
    arm = rig.data
    old = arm.pose_position
    me = None
    try:
        if old != 'REST':
            arm.pose_position = 'REST'
            bpy.context.view_layer.update()
        me = bpy.data.meshes.new_from_object(base.evaluated_get(bpy.context.evaluated_depsgraph_get()))
    except (AttributeError, RuntimeError, TypeError):      # pose position not editable (linked data)
        me = bpy.data.meshes.new_from_object(base)
    finally:
        if arm.pose_position != old:
            arm.pose_position = old
            bpy.context.view_layer.update()
    try:
        me.transform(base.matrix_world)
        n = len(me.vertices)
        V = np.empty(n * 3)
        me.vertices.foreach_get('co', V)
        me.calc_loop_triangles()
        m = len(me.loop_triangles)
        F, poly = np.empty(m * 3, np.int64), np.empty(m, np.int64)
        me.loop_triangles.foreach_get('vertices', F)
        me.loop_triangles.foreach_get('polygon_index', poly)
        mat = np.empty(len(me.polygons), np.int64)
        me.polygons.foreach_get('material_index', mat)
    finally:
        bpy.data.meshes.remove(me)
    names = [re.sub(r'\.\d{3}$', '', m.name.removeprefix('bone_')) if m else '' for m in base.data.materials]
    return V.reshape(-1, 3), F.reshape(-1, 3), mat[poly].astype(np.int32), names


def rest_context(rig, base):
    """Everything the import needs from the base rest mesh: world mesh, body parts, frame, mirror plane, hips data."""
    V, F, pf, names = base_rest_mesh(base, rig)
    o, H = tf.rest_frame(V)
    part_tri = np.array(names)[np.clip(pf, 0, len(names) - 1)] if names else np.full(len(F), '')
    centre = lambda part: V[F[part_tri == part]].reshape(-1, 3).mean(0) if (part_tri == part).any() else None
    tl, tr = centre('thigh_stretch.l'), centre('thigh_stretch.r')
    plane = tf.mirror_plane(V, o)
    mid = float((tl[0] + tr[0]) / 2) if tl is not None and tr is not None else plane
    gap = float(abs(tl[0] - tr[0]) * weights.table()['smooth']['hips']['between_thighs']) if tl is not None and tr is not None else 0.
    root = V[F[part_tri == 'root.x']].reshape(-1, 3) if (part_tri == 'root.x').any() else None
    return {'V': V, 'F': F, 'pf': pf, 'part_names': names, 'part_tri': part_tri, 'o': o, 'H': H, 'plane_x': plane,
            'n_vertices': len(V), 'mid_x': mid, 'thigh_gap': gap,
            'crotch_z': float(root[:, 2].min()) if root is not None and gap > 0 else None}


def piece_vertices(obj):
    co = np.empty(len(obj.data.vertices) * 3)
    obj.data.vertices.foreach_get('co', co)
    return co.reshape(-1, 3) @ np.array(obj.matrix_world.to_3x3()).T + np.array(obj.matrix_world.translation)


# ------------------------------------------------------------------------------------------------- fit

def resolve_fit(entry, manifest, lod):
    """(fit {scale, translation, ...}, info): `parts.fit` of the manifest, else fit the base to the labelled LOD."""
    fit = (manifest.get('parts') or {}).get('fit')
    if fit and 'scale' in fit and 'translation' in fit:
        return fit, {'source': 'manifest', 'method': fit.get('method'), 'residual_pct': fit.get('residual_fitted_pct_height')}
    parts = paths.base_views(entry['race']) / 'parts.npz'
    labelled = ps.labelled_path(entry, lod)
    if not parts.is_file() or not labelled.is_file():
        raise ValueError(f"manifest has no parts.fit and the fallback fit needs {parts.name} and {labelled.name}")
    if str(TRIPO) not in sys.path:
        sys.path.insert(0, str(TRIPO))
    import face_labels
    glb = ps.read_glb(labelled)
    rev = tf.review_from_native(glb['v'], manifest)
    s, t, info = face_labels.fit_base(face_labels.BaseParts.load(parts), rev, glb['f'])
    if info['used'] != 'icp' or info['residual_fitted_pct_height'] > 3.:
        raise ValueError(f"fallback fit of the base to {labelled.name} failed ({info['used']}, residual "
                         f"{info['residual_fitted_pct_height']:.2f}% of height)")
    return {'scale': float(s), 'translation': [float(x) for x in t], **info}, \
        {'source': 'fit_base', 'method': info['method'], 'residual_pct': info['residual_fitted_pct_height']}


# ------------------------------------------------------------------------------------------------- objects

def make_piece(coll, name, piece, n_channels, part_names, rig=None):
    """Mesh object `name` in `coll` from a piece dict (world V, F, channel, part, src)."""
    me = bpy.data.meshes.new(name)
    me.from_pydata(piece['V'].tolist(), [], piece['F'].tolist())
    me.update()
    me.shade_flat()
    ch = np.asarray(piece['channel'], np.int32)
    me.polygons.foreach_set('material_index', ch)
    part_idx = np.array([part_names.index(p) if p in part_names else -1 for p in piece['part']], np.int32)
    for attr, vals in (('os_channel', ch), ('os_label_source', np.asarray(piece['src'], np.int32)),
                       ('os_body_part', part_idx)):
        a = me.attributes.new(attr, 'INT', 'FACE')
        a.data.foreach_set('value', vals)
    for _ in range(n_channels):
        me.materials.append(None)
    obj = bpy.data.objects.new(name, me)
    coll.objects.link(obj)
    return obj


def remove_pieces(coll):
    for obj in items.item_pieces(coll):
        me = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if me.users == 0:
            bpy.data.meshes.remove(me)


def new_item_collection(scene, name):
    coll = bpy.data.collections.get(name) or bpy.data.collections.new(name)
    root = items.root_collection(scene, create=True)
    if coll.name not in root.children:
        root.children.link(coll)
    return coll


def delete_collection(coll):
    remove_pieces(coll)
    bpy.data.collections.remove(coll)


def tri_count(coll):
    return sum(len(o.data.polygons) for o in items.item_pieces(coll))


def probe_item(spec, rest):
    """Closest base triangle of every piece vertex (before anything is built): stores `probe` in each piece."""
    for piece in spec['pieces'].values():
        tri, bary, dist = mt.closest_points(piece['V'], rest['V'], rest['F'])
        piece['probe'] = {'tri': tri, 'bary': bary}
        piece['dist'] = dist


def build_item(scene, coll, item_id, item_type, spec, rig, base, rest, deltas, suffix=''):
    """Create the piece objects of one item inside `coll`: geometry, channels / materials, weights, morphs."""
    parts = sorted({p for pc in spec['pieces'].values() for p in set(pc['part'].tolist()) if p} | set(rest['part_names']))
    n_ch = len(spec['channels'])
    for name, piece in spec['pieces'].items():
        obj = make_piece(coll, f'OS_{item_id}__{name}{suffix}', piece, n_ch, parts)
        obj['os_piece'] = name
        obj['os_item'] = item_id
        weights.bind_piece(obj, name, item_type, rig, rest, base, piece['probe'], piece['part'])
        morphs.transfer(obj, base, piece['probe'], rest['F'], deltas)
    return parts


def quality(spec, H):
    d = np.concatenate([p['dist'] for p in spec['pieces'].values()])
    return {'fit_median_pct': float(np.median(d) / H * 100), 'fit_p90_pct': float(np.quantile(d, .9) / H * 100)}


def default_label(set_id, item_type):
    return f"{set_id.replace('_', ' ')} {item_type.replace('_', ' ')}"


def load_world(entry, lod, manifest, fit, rest):
    """Wearable of one module as welded world-space arrays plus per-face role / body part / label source."""
    path = ps.wearable_path(entry, lod)
    glb = ps.read_glb(path)
    P, F = ps.weld(glb['v'], glb['f'])
    W = tf.native_to_world(P, manifest, fit, rest['H'], rest['o'])
    role = ps.face_roles(glb)
    fd = ps.wearable_face_data(entry, lod, len(F))
    if fd is None:
        tri, _, _ = mt.closest_points(ps.tri_centroids(W, F), rest['V'], rest['F'])
        fd = {'part': rest['part_tri'][tri], 'src': np.zeros(len(F), np.int64), 'from': 'nearest base face'}
    return W, F, role, fd


def load_pieces_output(entry, lod, manifest, fit, rest):
    """Rigid pieces output (segment-first / split_pieces) as piece dicts in world space, or None."""
    glbs = ps.pieces_output(entry, lod)
    if not glbs:
        return None, None
    given = {}
    for name, path in glbs.items():
        glb = ps.read_glb(path)
        P, F = ps.weld(glb['v'], glb['f'])
        if not len(F):
            continue
        W = tf.native_to_world(P, manifest, fit, rest['H'], rest['o'])
        tri, _, _ = mt.closest_points(ps.tri_centroids(W, F), rest['V'], rest['F'])
        given[name] = {'V': W, 'F': F, 'role': ps.face_roles(glb), 'part': rest['part_tri'][tri],
                       'src': np.zeros(len(F), np.int64)}
    return given, str(next(iter(glbs.values())).parent)


def import_module(context, entry, lod='0p4', only=None, rig=None, base=None):
    """Import one pipeline module on the base of the open project. -> list of report dicts, one per item.

    New items are created; unedited ones are replaced in place (label, notes, status and channel settings kept);
    edited ones receive the new import in a hidden `__reimport` collection and go to `needs_review`."""
    scene = context.scene
    if rig is None:
        rig, base = base_objects(context)
    race = base_name(context)
    if entry['race'] != race:
        raise ValueError(f"{entry['module']} was converted for {entry['race']}, this project is {race}")
    if lod not in entry['levels']:
        raise ValueError(f"LOD {lod} is not available for {entry['set_id']}/{entry['race']}/{entry['module']} ({entry['levels']})")
    rest = rest_context(rig, base)
    manifest = ps.load_manifest(entry)
    fit, fit_info = resolve_fit(entry, manifest, lod)
    W, F, role, fd = load_world(entry, lod, manifest, fit, rest)
    given, pieces_dir = (load_pieces_output(entry, lod, manifest, fit, rest) if entry['module'] == 'body_armor'
                         else (None, None))
    specs = ps.make_items(entry['module'], W, F, role, fd['part'], fd['src'], rest['plane_x'], given)
    deltas = morphs.base_deltas(base)
    rig_fp, base_fp = items.rig_fingerprint(rig), items.base_fingerprint(base, rig, rest)
    wearable = ps.wearable_path(entry, lod)
    reports = []
    for item_type, spec in specs.items():
        if only and item_type not in only:
            continue
        if (fit_info.get('residual_pct') or 0) > FIT_RESIDUAL_FAIL_PCT:
            raise ValueError(f"the base fit of {entry['module']} is poor (residual {fit_info['residual_pct']:.2f}% of height)")
        probe_item(spec, rest)
        spec['quality'] = quality(spec, rest['H'])
        if spec['quality']['fit_median_pct'] > POOR_FIT_PCT:
            spec['warnings'].append(f"poor fit: the item sits {spec['quality']['fit_median_pct']:.1f}% of the body height "
                                    f"from the base on average (manifest fit residual {fit_info.get('residual_pct')})")
        if spec['quality']['fit_median_pct'] > FIT_FAIL_PCT:
            raise ValueError(f"{item_type}: median distance to the body is {spec['quality']['fit_median_pct']:.1f}% of "
                             f"height; the fit looks wrong ({fit_info})")
    for item_type, spec in specs.items():
        if only and item_type not in only:
            continue
        item_id = ps.item_id(entry['set_id'], item_type)
        existing = items.find_item(scene, item_id)
        old = items.item_data(existing) if existing else {}
        edited = bool(existing) and items.is_edited(existing)
        name = f'OS_{item_id}'
        if existing is None:
            coll, suffix, mode = new_item_collection(scene, name), '', 'created'
        elif edited:
            stale = items.reimport_collection(existing)
            if stale is not None:
                delete_collection(stale)
            coll = new_item_collection(scene, name + '__reimport')
            coll['os_reimport'] = item_id
            suffix, mode = '__reimport', 'reimport'
        else:
            coll, suffix, mode = existing, '', 'replaced'
            remove_pieces(coll)
        if mode != 'created':
            spec['channels'] = _merge(spec['channels'], old.get('channels', []))
        parts = build_item(scene, coll, item_id, item_type, spec, rig, base, rest, deltas, suffix)
        q = spec['quality']
        data = {
            'item_id': item_id, 'set_id': entry['set_id'], 'type': item_type,
            'label': old.get('label') or default_label(entry['set_id'], item_type),
            'status': old.get('status', 'imported') if mode == 'replaced' else 'imported', 'notes': old.get('notes', ''),
            'channels': spec['channels'],
            'source': {'job_id': entry['job_id'], 'source_set': entry.get('source_set', entry['set_id']), 'module': entry['module'], 'race': entry['race'], 'lod': lod,
                       'manifest': entry['manifest'], 'wearable_glb': str(wearable),
                       'wearable_sha256': hashing.file_sha256(wearable), 'pieces_source': pieces_dir,
                       'fit': fit_info, 'fit_values': {'scale': fit['scale'], 'translation': list(fit['translation'])},
                       'face_data': fd.get('from', 'colors.npz'), 'label_source_names': list(ps.SOURCES)},
            'import': {'hashes': {}, 'rig_fingerprint': rig_fp, 'base_fingerprint': base_fp, 'date': items.now()},
            'stats': {'tris': tri_count(coll), 'pieces': list(spec['pieces']), **q},
            'body_part_names': parts, 'stale': [], 'warnings': spec['warnings'], 'version': items.VERSION,
        }
        if mode == 'reimport':
            existing_d = {**old, 'status': 'needs_review'}
            data['import']['hashes'] = items.piece_hashes(coll)
            coll['os_item'] = json.dumps(data)
            items.apply_channel_materials(coll, data['channels'])
            coll.hide_viewport = coll.hide_render = True
            items.set_item_data(existing, existing_d)
            target = existing
        else:
            items.set_item_data(coll, data)
            items.apply_channel_materials(coll)
            data['import']['hashes'] = items.piece_hashes(coll)
            items.set_item_data(coll, data)
            target = coll
        reports.append({'item_id': item_id, 'item_type': item_type, 'mode': mode, 'collection': target.name,
                        'pieces': {n: len(p['F']) for n, p in spec['pieces'].items()}, 'tris': data['stats']['tris'],
                        'fit_median_pct': q['fit_median_pct'], 'fit_p90_pct': q['fit_p90_pct'], 'warnings': spec['warnings']})
    return reports


def _merge(new, old):
    from ..core import channels
    return channels.merge_channels(new, old)


def import_source(context, coll):
    """Import the job's high-poly `source.glb` (textured) with the transform of the item's import. -> new objects."""
    d = items.item_data(coll)
    src = d['source']
    entry = next((e for e in ps.discover() if (e['set_id'], e['job_id'], e['race'], e['module'])
                  == (d['set_id'], src['job_id'], src['race'], src['module'])), None)
    if entry is None:
        raise ValueError('the pipeline job of this item is gone')
    manifest = ps.load_manifest(entry)
    glb = Path(manifest.get('source') or '')
    if not glb.is_file():
        glb = Path(entry['dir']) / 'source.glb'
    rig, base = base_objects(context)
    rest = rest_context(rig, base)
    fit = src['fit_values']
    f = lambda p: tf.native_to_world(np.array([p], float), manifest, fit, rest['H'], rest['o'])[0]
    c = f((0, 0, 0))
    M = np.eye(4)
    M[:3, 3] = c
    M[:3, :3] = np.stack([f(e) - c for e in np.eye(3)], axis=1)
    from mathutils import Matrix
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(glb))
    new = [o for o in bpy.data.objects if o not in before]
    for o in new:
        if o.parent is None or o.parent not in new:
            o.matrix_world = Matrix(M.tolist()) @ o.matrix_world
    return new


# ------------------------------------------------------------------------------------------------- reimport / mirror

def resolve_reimport(context, coll, choice):
    """Settle a pending `__reimport`: keep mine, take new, or compare (new one shown +1 m in X)."""
    pending = items.reimport_collection(coll)
    if pending is None:
        return
    d = items.item_data(coll)
    if choice == 'compare':
        if not pending.get('os_compared'):
            for obj in pending.objects:
                obj.matrix_world.translation.x += COMPARE_OFFSET
            pending['os_compared'] = True
        pending.hide_viewport = pending.hide_render = False
        return
    if choice == 'keep':
        delete_collection(pending)
        d['status'] = 'edited'
    else:
        new = json.loads(pending['os_item'])
        if pending.get('os_compared'):
            for obj in pending.objects:
                obj.matrix_world.translation.x -= COMPARE_OFFSET
        remove_pieces(coll)
        for obj in list(pending.objects):
            pending.objects.unlink(obj)
            coll.objects.link(obj)
            obj.name = obj.name.removesuffix('__reimport')
            obj.hide_viewport = False
        bpy.data.collections.remove(pending)
        new['label'], new['notes'] = d.get('label', new['label']), d.get('notes', '')
        new['channels'] = _merge(new['channels'], d.get('channels', []))
        new['status'] = 'imported'
        d = new
        items.set_item_data(coll, d)
        items.apply_channel_materials(coll)
        d['import']['hashes'] = items.piece_hashes(coll)
    items.set_item_data(coll, d)


def mirror_item(context, coll, source_side='r'):
    """Rebuild the pieces of the other side as mirrored copies of `source_side` pieces. -> number of pieces rebuilt."""
    d = items.item_data(coll)
    rig, base = base_objects(context)
    rest = rest_context(rig, base)
    names = d['body_part_names']
    deltas = morphs.base_deltas(base)
    channels = d['channels']
    ids = [c['id'] for c in channels]
    count = 0
    for obj in items.item_pieces(coll):
        piece = obj.get('os_piece', '')
        if not piece.endswith('_' + source_side):
            continue
        twin = ps.swap_side(piece)
        V = piece_vertices(obj)
        F = np.empty(len(obj.data.loops), np.int64)
        obj.data.loops.foreach_get('vertex_index', F)
        F = F.reshape(-1, 3)
        ch = np.zeros(len(F), np.int32)
        obj.data.attributes['os_channel'].data.foreach_get('value', ch)
        bp = np.zeros(len(F), np.int32)
        obj.data.attributes['os_body_part'].data.foreach_get('value', bp)
        remap = {}
        for c in np.unique(ch):
            sid = items.swap_channel_id(channels[c]['id'])
            if sid not in ids:
                channels.append({**channels[c], 'id': sid, 'label': items.swap_channel_id(channels[c].get('label', sid)).replace('_', ' ')})
                ids.append(sid)
            remap[c] = ids.index(sid)
        V2, F2 = tf.mirror_mesh(V, F, rest['plane_x'])
        part = np.array([names[i] if 0 <= i < len(names) else '' for i in bp])
        mirrored = ps.mirror_piece({'V': V, 'F': F, 'part': part}, rest['plane_x'])
        piece_d = {'V': V2, 'F': F2, 'channel': np.array([remap[c] for c in ch], np.int32), 'part': mirrored['part'],
                   'src': mirrored['src']}
        old = bpy.data.objects.get(f"OS_{d['item_id']}__{twin}")
        if old is not None:
            me = old.data
            bpy.data.objects.remove(old, do_unlink=True)
            bpy.data.meshes.remove(me)
        new = make_piece(coll, f"OS_{d['item_id']}__{twin}", piece_d, len(channels), names)
        new['os_piece'], new['os_item'] = twin, d['item_id']
        tri, bary, _ = mt.closest_points(V2, rest['V'], rest['F'])
        probe = {'tri': tri, 'bary': bary}
        weights.bind_piece(new, twin, d['type'], rig, rest, base, probe, piece_d['part'])
        morphs.transfer(new, base, probe, rest['F'], deltas)
        count += 1
    d['channels'] = channels
    d['stats']['pieces'] = [o.get('os_piece') for o in items.item_pieces(coll)]
    d['stats']['tris'] = tri_count(coll)
    items.set_item_data(coll, d)
    items.apply_channel_materials(coll)
    d['import']['hashes'] = items.piece_hashes(coll)
    items.set_item_data(coll, d)
    return count


# ------------------------------------------------------------------------------------------------- operators

_enum_cache = {}


def _entries(context):
    race = base_name(context)
    return [e for e in ps.discover() if e['race'] == race]


def _keep(key, rows):
    _enum_cache[key] = rows or [('', '(none)', '')]
    return _enum_cache[key]


def _set_items(self, context):
    sets = sorted({e['set_id'] for e in _entries(context)})
    return _keep('sets', [(s, s, '') for s in sets])


def _job_items(self, context):
    jobs = [e for e in _entries(context) if e['set_id'] == self.set_id]
    seen, rows = set(), []
    for e in jobs:                                  # newest first
        if e['job_id'] not in seen:
            seen.add(e['job_id'])
            rows.append((e['job_id'], e['job_id'] + (' (newest)' if not rows else ''), ', '.join(sorted(
                x['module'] for x in jobs if x['job_id'] == e['job_id']))))
    return _keep('jobs', rows)


def _default_lod(context):
    lod = getattr(getattr(context.scene, 'ostudio', None), 'lod', None)
    return lod if lod in ps.LODS else ps.LODS[0]


def _lod_items(self, context):
    return [('0p4', '0.4 %', 'geometry error 0.4 % of height (default)'), ('0p2', '0.2 %', ''), ('0p1', '0.1 %', '')]


class OSTUDIO_OT_import_pipeline(bpy.types.Operator):
    bl_idname = 'ostudio.import_pipeline'
    bl_label = 'Import from pipeline'
    bl_description = 'Import converted pipeline modules as items on this base'
    set_id: bpy.props.EnumProperty(name='Set', items=_set_items)
    job_id: bpy.props.EnumProperty(name='Job', items=_job_items)
    module: bpy.props.EnumProperty(name='Module', items=[
        ('all', 'All modules', 'every module of the job'), ('body_armor', 'Body armour', ''),
        ('right_gauntlet_boot', 'Gauntlets + boots', ''), ('outfit', 'Outfit', '')], default='all')
    lod: bpy.props.EnumProperty(name='LOD', items=_lod_items)
    all_new: bpy.props.BoolProperty(name='All new for this base', default=False,
                                    description='Import the newest job of every set / module that has no item yet')

    def invoke(self, context, event):
        self.lod = _default_lod(context)
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, 'all_new')
        sub = col.column()
        sub.active = not self.all_new
        sub.prop(self, 'set_id')
        sub.prop(self, 'job_id')
        sub.prop(self, 'module')
        col.prop(self, 'lod')
        others = [e['job_id'] for e in _entries(context) if e['set_id'] == self.set_id and e['job_id'] != self.job_id]
        if others and not self.all_new:
            col.label(text='Other jobs: ' + ', '.join(sorted(set(others))))

    def targets(self, context):
        entries = _entries(context)
        if self.all_new:
            have = {items.item_data(c).get('item_id') for c in items.list_items(context.scene)}
            return [e for e in ps.latest(entries)
                    if any(ps.item_id(e['set_id'], t) not in have for t in ps.MODULE_ITEMS[e['module']])]
        return [e for e in entries if e['set_id'] == self.set_id and e['job_id'] == self.job_id
                and self.module in ('all', e['module'])]

    def execute(self, context):
        targets = self.targets(context)
        if not targets:
            self.report({'WARNING'}, 'nothing to import')
            return {'CANCELLED'}
        done = 0
        for e in targets:
            try:
                for r in import_module(context, e, self.lod if self.lod in e['levels'] else e['levels'][0]):
                    done += 1
                    self.report({'INFO'}, f"{r['item_id']}: {r['mode']}, {r['tris']} tris, median {r['fit_median_pct']:.1f}% of height")
            except Exception as ex:
                self.report({'ERROR'}, f"{e['set_id']}/{e['module']}: {ex}")
        return {'FINISHED'} if done else {'CANCELLED'}


class OSTUDIO_OT_reimport(bpy.types.Operator):
    bl_idname = 'ostudio.reimport'
    bl_label = 'Re-import current'
    bl_description = 'Import the current item again from the pipeline (hand edits are protected)'
    lod: bpy.props.StringProperty(name='LOD', default='', description='"" = the item\'s LOD')

    def execute(self, context):
        coll = items.current_item(context)
        if coll is None:
            return {'CANCELLED'}
        d = items.item_data(coll)
        src = d['source']
        entries = [e for e in _entries(context) if (e['set_id'], e['module']) == (d['set_id'], src['module'])]
        entry = next((e for e in entries if e['job_id'] == src['job_id']), entries[0] if entries else None)
        if entry is None:
            self.report({'ERROR'}, 'the pipeline output of this item is gone')
            return {'CANCELLED'}
        try:
            r = import_module(context, entry, self.lod or src['lod'], only={d['type']})
        except Exception as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        self.report({'WARNING' if r and r[0]['mode'] == 'reimport' else 'INFO'},
                    ', '.join(f"{x['item_id']}: {x['mode']}" for x in r))
        return {'FINISHED'}


CLASSES = (OSTUDIO_OT_import_pipeline, OSTUDIO_OT_reimport)


def draw_import(layout, ctx):
    box = layout.box()
    box.label(text='Import')
    col = box.column(align=True)
    col.operator('ostudio.import_pipeline', text='Import from pipeline...', icon='IMPORT')
    col.operator('ostudio.import_pipeline', text='Import all new for this base', icon='ADD').all_new = True
    coll = items.current_item(ctx)
    row = box.row()
    row.enabled = coll is not None
    row.operator('ostudio.reimport', icon='FILE_REFRESH')
    if coll is not None and items.reimport_collection(coll) is not None:
        box.label(text='Edited item: a new import is waiting', icon='ERROR')
        row = box.row(align=True)
        for choice, text in (('keep', 'Keep mine'), ('take', 'Take new'), ('compare', 'Compare')):
            row.operator('ostudio.reimport_resolve', text=text).choice = choice
