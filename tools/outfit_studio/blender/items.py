"""Item layers of an outfit project (SPEC section 2): data model, edit detection, stale-rig handling, item drawer.

An item is a collection `OS_<item_id>` under `OS_Items`; `coll["os_item"]` holds its JSON record, the pieces are the
mesh objects in it. `scene.ostudio.item_index` indexes `list_items(scene)` (all items, sorted by name)."""
import json
import re
from datetime import datetime, timezone

import bpy
import numpy as np

from .. import paths
from ..core import hashing
from . import weights

ROOT_NAME = 'OS_Items'
STATUSES = ('imported', 'edited', 'needs_review', 'approved', 'exported')
ITEM_TYPES = ('outfit', 'body_armor', 'gauntlets', 'boots')
VERSION = 1
_SOCKET = re.compile(r'Attachment|Sheath|Shield|CastEmission|KneePole')
_SYNC = False

_FALLBACK_COLOURS = {'primary': '#19D228', 'secondary': '#1E41EB', 'tertiary': '#F5DC14', 'accent': '#E119DC',
                     'joint': '#14DCEB', 'base': '#E61923', 'uncertain': '#808080'}


def _load_colours():
    out = dict(_FALLBACK_COLOURS)
    try:
        path = next(p for p in sorted(paths.BASE_VIEWS.glob('*/palette.json')))
        pal = json.loads(path.read_text())
        out['base'] = pal.get('base', {}).get('hex', out['base'])
        for g in pal.get('garments', []) + pal.get('separable_garments', []):
            out[g['role']] = g['hex']
    except (StopIteration, OSError, ValueError, KeyError):
        pass
    return out


ROLE_COLOURS = _load_colours()      # role -> '#RRGGBB' (sRGB), from base_views/<base>/palette.json


def hex_to_linear(hexcode, alpha=1.):
    c = [int(hexcode.lstrip('#')[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return (*[x / 12.92 if x <= .04045 else ((x + .055) / 1.055) ** 2.4 for x in c], alpha)


def role_material(role):
    """Shared preview material `OSC_<role>` (diffuse colour = the role's palette colour)."""
    name = f'OSC_{role}'
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
        rgba = hex_to_linear(ROLE_COLOURS.get(role, ROLE_COLOURS['uncertain']))
        mat.diffuse_color = rgba
        mat.use_nodes = True
        shader = mat.node_tree.nodes.get('Principled BSDF')
        if shader:
            shader.inputs['Base Color'].default_value = rgba
            shader.inputs['Roughness'].default_value = .9
    return mat


def swap_channel_id(cid):
    """cuff_r -> cuff_l, cuff_r_secondary -> cuff_l_secondary (and back)."""
    return re.sub(r'(?<=_)([lr])(?=$|_)', lambda m: 'l' if m.group(1) == 'r' else 'r', cid, count=1)


# ------------------------------------------------------------------------------------------------- collections

def root_collection(scene, create=False):
    root = bpy.data.collections.get(ROOT_NAME)
    if root is None and create:
        root = bpy.data.collections.new(ROOT_NAME)
    if root is not None and create and root.name not in scene.collection.children:
        scene.collection.children.link(root)
    return root


def list_items(scene):
    root = bpy.data.collections.get(ROOT_NAME)
    if root is None:
        return []
    return sorted((c for c in root.children if 'os_item' in c and 'os_reimport' not in c), key=lambda c: c.name)


def find_item(scene, item_id):
    return next((c for c in list_items(scene) if item_data(c).get('item_id') == item_id), None)


def reimport_collection(coll):
    """The hidden `OS_<item_id>__reimport` collection of an item, or None."""
    return bpy.data.collections.get(coll.name + '__reimport')


def item_data(coll):
    return json.loads(coll['os_item']) if 'os_item' in coll else {}


def set_item_data(coll, data):
    """Store the item record (JSON on the collection) and refresh the UI mirrors (channels, label, notes)."""
    coll['os_item'] = json.dumps(data)
    sync_props(coll, data)


def item_pieces(coll):
    """Piece mesh objects of an item, in name order."""
    return sorted((o for o in coll.objects if o.type == 'MESH'), key=lambda o: o.name)


def current_item(context):
    items = list_items(context.scene)
    ost = getattr(context.scene, 'ostudio', None)
    if not items:
        return None
    return items[min(max(getattr(ost, 'item_index', 0), 0), len(items) - 1)]


# ------------------------------------------------------------------------------------------------- hashes / edits

def piece_hash(obj):
    """Content hash of a piece (positions, faces, `os_channel`, vertex weights): see core.hashing."""
    me = obj.data
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get('co', co)
    corner = np.empty(len(me.loops), np.int64)
    me.loops.foreach_get('vertex_index', corner)
    total = np.diff(np.append([p.loop_start for p in me.polygons], len(me.loops))) if len(me.polygons) else np.zeros(0)
    attr = me.attributes.get('os_channel')
    ch = np.zeros(len(me.polygons), np.int32)
    if attr is not None:
        attr.data.foreach_get('value', ch)
    names, W = weights.vertex_weights(obj)
    return hashing.piece_hash(co.reshape(-1, 3), np.concatenate([total, corner]), ch,
                              {n: W[:, j] for j, n in enumerate(names)})


def piece_hashes(coll):
    return {o.get('os_piece', o.name): piece_hash(o) for o in item_pieces(coll)}


def is_edited(coll):
    """True when any piece differs from the hash recorded at import (or pieces were added / removed)."""
    recorded = item_data(coll).get('import', {}).get('hashes', {})
    return piece_hashes(coll) != recorded


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


# ------------------------------------------------------------------------------------------------- fingerprints

def rig_fingerprint(rig):
    try:
        from . import project
        return project.rig_fingerprint(rig)
    except (ImportError, AttributeError):
        pass
    entries = []
    for b in rig.data.bones:
        if b.use_deform or _SOCKET.search(b.name):
            roll = b.AxisRollFromMatrix(b.matrix.to_3x3())[1]
            entries.append((b.name, b.parent.name if b.parent else '', tuple(b.head_local), tuple(b.tail_local), roll))
    return hashing.rig_fingerprint(entries)


def base_fingerprint(base, rig, rest=None):
    try:
        from . import project
        return project.base_fingerprint(base, rig)
    except (ImportError, AttributeError):
        pass
    if rest is None:
        from . import importer
        rest = importer.rest_context(rig, base)
    return hashing.base_fingerprint(rest['V'])


def check_item(coll, rig_fp, base_fp):
    """Reasons ('rig', 'base') an item no longer matches the current rig / base mesh."""
    imp = item_data(coll).get('import', {})
    return [k for k, cur in (('rig', rig_fp), ('base', base_fp)) if imp.get(f'{k}_fingerprint') not in (None, cur)]


def refresh_status(coll):
    """`imported` / `approved` items whose pieces changed become `edited` (the stored status is never lowered)."""
    d = item_data(coll)
    edited = is_edited(coll)
    if edited and d.get('status') in ('imported', 'approved'):
        d['status'] = 'edited'
        set_item_data(coll, d)
    return edited


def check_all(context):
    """Mark stale items and refresh edit statuses. -> {'stale': [item ids], 'edited': [item ids], 'rig_fp': str}"""
    from . import importer
    rig, base = importer.base_objects(context)
    rest = importer.rest_context(rig, base)
    rig_fp, base_fp = rig_fingerprint(rig), base_fingerprint(base, rig, rest)
    out = {'stale': [], 'edited': [], 'rig_fp': rig_fp, 'base_fp': base_fp, 'missing_bones': weights.missing_table_bones(rig)}
    for coll in list_items(context.scene):
        d = item_data(coll)
        d['stale'] = check_item(coll, rig_fp, base_fp)
        set_item_data(coll, d)
        if d['stale']:
            out['stale'].append(d['item_id'])
        if refresh_status(coll):
            out['edited'].append(d['item_id'])
    return out


def rebind_item(context, coll, rig=None, base=None):
    """Re-run weights and morph transfer on every piece of an item against the current rig and base."""
    from . import importer, morphs
    from ..core import morph_transfer as mt
    if rig is None:
        rig, base = importer.base_objects(context)
    rest = importer.rest_context(rig, base)
    d = item_data(coll)
    names = d.get('body_part_names', [])
    deltas = morphs.base_deltas(base)
    for obj in item_pieces(coll):
        V = importer.piece_vertices(obj)
        tri, bary, dist = mt.closest_points(V, rest['V'], rest['F'])
        probe = {'tri': tri, 'bary': bary}
        attr = obj.data.attributes.get('os_body_part')
        part = None
        if attr is not None and names:
            idx = np.zeros(len(obj.data.polygons), np.int32)
            attr.data.foreach_get('value', idx)
            part = np.array(names)[np.clip(idx, 0, len(names) - 1)]
        weights.bind_piece(obj, obj.get('os_piece', obj.name), d['type'], rig, rest, base, probe, part)
        morphs.transfer(obj, base, probe, rest['F'], deltas)
    imp = d.setdefault('import', {})
    imp.update(rig_fingerprint=rig_fingerprint(rig), base_fingerprint=base_fingerprint(base, rig, rest),
               hashes=piece_hashes(coll), date=now())
    d['stale'] = []
    set_item_data(coll, d)


# ------------------------------------------------------------------------------------------------- UI mirrors

def role_items(self, context):
    return [(r, r, '') for r in ROLE_COLOURS if r not in ('base', 'uncertain')]


def apply_channel_materials(coll, channels=None):
    """One material slot per channel on every piece, holding the material of the channel's role alias."""
    channels = item_data(coll).get('channels', []) if channels is None else channels
    for obj in item_pieces(coll):
        me = obj.data
        while len(me.materials) < len(channels):
            me.materials.append(None)
        for i, c in enumerate(channels):
            me.materials[i] = role_material(c['role'])


def _channel_update(self, context):
    if _SYNC:
        return
    coll = self.id_data
    d = item_data(coll)
    d['channels'] = [{'id': c.id, 'label': c.label, 'role': c.role, 'material': c.material} for c in coll.os_channels]
    coll['os_item'] = json.dumps(d)
    apply_channel_materials(coll)


def _text_update(key):
    def update(self, context):
        if _SYNC:
            return
        d = item_data(self)
        d[key] = getattr(self, 'os_' + key)
        self['os_item'] = json.dumps(d)
    return update


class OSChannel(bpy.types.PropertyGroup):
    id: bpy.props.StringProperty()
    label: bpy.props.StringProperty()
    role: bpy.props.EnumProperty(name='Role', items=role_items, update=_channel_update)
    material: bpy.props.StringProperty(name='Material', description='Default game material id ("" = engine default)',
                                       update=_channel_update)


def sync_props(coll, data=None):
    """Copy the JSON record into the registered UI properties (no-op while the add-on is not registered)."""
    global _SYNC
    if not hasattr(coll, 'os_channels'):
        return
    data = data or item_data(coll)
    _SYNC = True
    try:
        coll.os_channels.clear()
        for c in data.get('channels', []):
            row = coll.os_channels.add()
            row.id, row.label, row.material = c['id'], c.get('label', c['id']), c.get('material', '')
            try:
                row.role = c['role']
            except TypeError:
                pass
        coll.os_label, coll.os_notes = data.get('label', ''), data.get('notes', '')
    finally:
        _SYNC = False


# ------------------------------------------------------------------------------------------------- operators

class OSTUDIO_OT_check_rig(bpy.types.Operator):
    bl_idname = 'ostudio.check_rig'
    bl_label = 'Check rig and edits'
    bl_description = 'Compare every item with the current rig and base mesh (stale items) and look for hand edits'

    def execute(self, context):
        try:
            r = check_all(context)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'WARNING' if r['stale'] else 'INFO'},
                    f"{len(r['stale'])} stale, {len(r['edited'])} edited of {len(list_items(context.scene))} items"
                    + (f", rig lacks bones {r['missing_bones']}" if r['missing_bones'] else ''))
        return {'FINISHED'}


class OSTUDIO_OT_rebind_stale(bpy.types.Operator):
    bl_idname = 'ostudio.rebind_stale'
    bl_label = 'Re-bind stale items'
    bl_description = 'Re-run weights and morph transfer on stale items; edited items only with confirmation'
    include_edited: bpy.props.BoolProperty(name='Also re-bind edited items', default=False,
                                           description='Overwrites hand-edited weights of edited stale items')

    def invoke(self, context, event):
        check_all(context)
        if any(item_data(c).get('stale') and is_edited(c) for c in list_items(context.scene)):
            return context.window_manager.invoke_props_dialog(self)
        return self.execute(context)

    def execute(self, context):
        done, skipped = 0, []
        for coll in list_items(context.scene):
            d = item_data(coll)
            if not d.get('stale'):
                continue
            if is_edited(coll) and not self.include_edited:
                skipped.append(d['item_id'])
                continue
            try:
                rebind_item(context, coll)
                done += 1
            except Exception as e:
                self.report({'ERROR'}, f"{d['item_id']}: {e}")
        self.report({'WARNING' if skipped else 'INFO'}, f'{done} re-bound' + (f', edited skipped: {skipped}' if skipped else ''))
        return {'FINISHED'}


class OSTUDIO_OT_reimport_resolve(bpy.types.Operator):
    bl_idname = 'ostudio.reimport_resolve'
    bl_label = 'Resolve re-import'
    bl_description = 'Keep your edited pieces, take the re-imported ones, or show both side by side'
    choice: bpy.props.EnumProperty(items=[('keep', 'Keep mine', ''), ('take', 'Take new', ''), ('compare', 'Compare', '')],
                                   default='keep')

    def execute(self, context):
        from . import importer
        coll = current_item(context)
        if coll is None or reimport_collection(coll) is None:
            self.report({'WARNING'}, 'no pending re-import for the current item')
            return {'CANCELLED'}
        importer.resolve_reimport(context, coll, self.choice)
        return {'FINISHED'}


class OSTUDIO_OT_mirror_pieces(bpy.types.Operator):
    bl_idname = 'ostudio.mirror_pieces'
    bl_label = 'Mirror pieces'
    bl_description = 'Rebuild the pieces of one side as mirrored copies of the other (gauntlets / boots: right to left)'
    source_side: bpy.props.EnumProperty(items=[('r', 'Right to left', ''), ('l', 'Left to right', '')], default='r')

    def execute(self, context):
        from . import importer
        coll = current_item(context)
        if coll is None:
            return {'CANCELLED'}
        try:
            n = importer.mirror_item(context, coll, self.source_side)
        except Exception as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, f'{n} pieces mirrored')
        return {'FINISHED'}


CLASSES = (OSChannel, OSTUDIO_OT_check_rig, OSTUDIO_OT_rebind_stale, OSTUDIO_OT_reimport_resolve, OSTUDIO_OT_mirror_pieces)


def draw_item(layout, ctx):
    coll = current_item(ctx)
    if coll is None:
        layout.label(text='No items yet. Import from the pipeline.', icon='INFO')
        return
    d = item_data(coll)
    box = layout.box()
    if hasattr(coll, 'os_label'):
        box.prop(coll, 'os_label', text='')
    else:
        box.label(text=d.get('label', coll.name))
    src = d.get('source', {})
    box.label(text=f"{d.get('type', '?')}  |  {src.get('race', '')}  {src.get('job_id', '')}  {src.get('lod', '')}")
    row = box.row()
    row.label(text=f"{d.get('stats', {}).get('tris', 0)} tris, {len(item_pieces(coll))} pieces")
    row.label(text=d.get('status', ''), icon='CHECKMARK' if d.get('status') == 'approved' else 'DOT')
    if d.get('stale'):
        box.label(text='Stale: ' + ', '.join(d['stale']) + ' changed', icon='ERROR')
    if reimport_collection(coll) is not None:
        box.label(text='Re-import waiting (see Import)', icon='FILE_REFRESH')
    if hasattr(coll, 'os_notes'):
        box.prop(coll, 'os_notes', text='Notes')
    col = layout.column(align=True)
    col.label(text='Channels')
    if hasattr(coll, 'os_channels'):
        for ch in coll.os_channels:
            row = col.row(align=True)
            row.label(text=ch.id)
            row.prop(ch, 'role', text='')
            row.prop(ch, 'material', text='')
    else:
        for c in d.get('channels', []):
            col.label(text=f"{c['id']}: {c['role']}")
    row = layout.row(align=True)
    row.operator('ostudio.check_rig', icon='ARMATURE_DATA')
    row.operator('ostudio.rebind_stale', icon='FILE_REFRESH')
    if d.get('type') in ('gauntlets', 'boots'):
        layout.operator('ostudio.mirror_pieces', icon='MOD_MIRROR')


# ------------------------------------------------------------------------------------------------- registration

def _on_load(*_):
    try:
        if bpy.data.collections.get(ROOT_NAME) is not None:
            check_all(bpy.context)
    except Exception as e:                  # a file without rig / base must still open
        print('[outfit_studio] load check skipped:', e)


def register():
    bpy.types.Collection.os_channels = bpy.props.CollectionProperty(type=OSChannel)
    bpy.types.Collection.os_label = bpy.props.StringProperty(name='Label', update=_text_update('label'))
    bpy.types.Collection.os_notes = bpy.props.StringProperty(name='Notes', update=_text_update('notes'))
    if _on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load)


def unregister():
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    for name in ('os_channels', 'os_label', 'os_notes'):
        if hasattr(bpy.types.Collection, name):
            delattr(bpy.types.Collection, name)
