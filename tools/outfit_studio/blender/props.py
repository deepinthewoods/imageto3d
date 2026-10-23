"""scene.ostudio: navigation, filter, view and mix-and-match settings (SPEC section 10).

Also the small shared layer the UI modules use: `items_api()` (B's items.py, or a minimal
stand-in over the SPEC section 2 data model when it is not there yet), role colours,
and the filtered item list. Enum callbacks keep their returned lists in `_cache`
(Blender frees the strings of a returned list otherwise)."""
import importlib
import json
import types

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, IntProperty, PointerProperty, StringProperty)
from bpy.app.handlers import persistent

from .. import paths

TYPES = (('outfit', 'Outfit', ''), ('body_armor', 'Body armour', ''), ('gauntlets', 'Gauntlets', ''),
         ('boots', 'Boots', ''))
STATUSES = ('imported', 'edited', 'needs_review', 'approved', 'exported')
VIEWS = (('NONE', 'Off', 'No view active'),
         ('GAME', 'Game view', 'Equipped layers; outfit hidden when any armour is equipped'),
         ('ITEM', 'Item only', 'Current item without the body'),
         ('ROLES', 'Colour roles', 'Faces in their palette role colours'),
         ('PALETTE', 'Engine palette', 'Random engine tints per role'),
         ('CHANNELS', 'Channels', 'A distinct colour per channel'),
         ('CONFIDENCE', 'Label confidence', 'Colour by label source'),
         ('WEIGHTS', 'Weights', 'Dominant bone per vertex'),
         ('POKE', 'Poke-through', 'Base vertices outside the item surface'),
         ('POSE', 'Pose test', 'Play an action on the rig'),
         ('SOCKETS', 'Sockets + props', 'Proxy props on the socket bones'),
         ('AP', 'AP points', 'Attachment point markers, buried ones red'),
         ('HEAD', 'Head swap', 'Library heads on the head bone'),
         ('THICKNESS', 'Thickness', 'Drive every bulk_* shape key'),
         ('CONCEPT', 'Concept overlay', 'Concept camera with the view image behind'),
         ('SOURCE', 'Source high-poly', 'The textured high-poly source next to the item'),
         ('WIRE', 'Wireframe / stats', 'Wireframe and triangle statistics'))
VIEW_BUTTONS = tuple(v[0] for v in VIEWS[1:])
_cache = {}


# ---------------------------------------------------------------- items api (B or stand-in)
def _list_items(scene):
    top = bpy.data.collections.get('OS_Items')
    kids = [c for c in top.children if not c.name.endswith('__reimport')] if top else []
    return sorted(kids, key=lambda c: c.name)


def _item_data(coll):
    try:
        return json.loads(coll.get('os_item', '{}'))
    except (TypeError, ValueError):
        return {}


def _set_item_data(coll, d):
    coll['os_item'] = json.dumps(d)


def _item_pieces(coll):
    return [o for o in coll.objects if o.type == 'MESH']


def _current_item(context):
    lst = _list_items(context.scene)
    i = context.scene.ostudio.item_index
    return lst[i] if 0 <= i < len(lst) else None


_fallback = types.SimpleNamespace(list_items=_list_items, item_data=_item_data, set_item_data=_set_item_data,
                                  item_pieces=_item_pieces, current_item=_current_item, FALLBACK=True)


def items_api():
    """blender/items.py when present, else the stand-in with the same names."""
    try:
        from . import items
        return items
    except ModuleNotFoundError as e:
        if e.name != f'{__package__}.items':
            raise
        return _fallback


def lazy(name):
    """Sibling module by name, or None when it does not exist (yet)."""
    try:
        return importlib.import_module(f'{__package__}.{name}')
    except ModuleNotFoundError as e:
        if e.name != f'{__package__}.{name}':
            raise
        return None


def has_op(idname):
    mod, _, name = idname.partition('.')
    return hasattr(getattr(bpy.ops, mod, None), name)


def srgb_to_linear(c):
    return tuple(x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c[:3]) + (1.0,)


def hex_to_rgba(h):
    h = h.lstrip('#')
    return srgb_to_linear([int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)])


def base_name(scene=None):
    from . import project
    return project.base_name(scene or bpy.context.scene)


def role_colours(scene=None):
    """role -> linear RGBA from base_views/<base>/palette.json (+ base body and `uncertain`)."""
    out = {'base': hex_to_rgba('#E61923'), 'uncertain': (0.5, 0.5, 0.5, 1.0)}
    p = paths.base_views(base_name(scene) or '') / 'palette.json'
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        d = {'garments': [{'role': 'primary', 'hex': '#19D228'}, {'role': 'secondary', 'hex': '#1E41EB'},
                          {'role': 'tertiary', 'hex': '#F5DC14'}, {'role': 'accent', 'hex': '#E119DC'}],
             'separable_garments': [{'role': 'joint', 'hex': '#14DCEB'}]}
    out['base'] = hex_to_rgba(d.get('base', {}).get('hex', '#E61923'))
    for g in d.get('garments', []) + d.get('separable_garments', []):
        out[g['role']] = hex_to_rgba(g['hex'])
    return out


def filtered_items(scene):
    """[(index in api.list_items, collection)] after the type/status/text filters."""
    api, o = items_api(), scene.ostudio
    out = []
    for i, c in enumerate(api.list_items(scene)):
        d = api.item_data(c)
        if o.filter_type != 'ALL' and d.get('type') != o.filter_type:
            continue
        if o.filter_status != 'ALL' and d.get('status', 'imported') != o.filter_status:
            continue
        t = o.filter_text.lower()
        if t and t not in (d.get('item_id', c.name) + ' ' + d.get('label', '') + ' ' + d.get('set_id', '')).lower():
            continue
        out.append((i, c))
    return out


def item_by_id(scene, item_id):
    api = items_api()
    for c in api.list_items(scene):
        if api.item_data(c).get('item_id', c.name.removeprefix('OS_')) == item_id:
            return c
    return None


def mix_collections(scene):
    """Collections for the four mix-and-match slots (None = empty slot)."""
    o = scene.ostudio
    ids = [o.mix_outfit, o.mix_body_armor, o.mix_gauntlets, o.mix_boots]
    return [None if i in ('', 'NONE') else item_by_id(scene, i) for i in ids]


# ---------------------------------------------------------------- enum callbacks
def _mix_items(typ):
    def cb(self, context):
        out = [('NONE', 'None', '')]
        api = items_api()
        for c in api.list_items(context.scene):
            d = api.item_data(c)
            if d.get('type') == typ:
                iid = d.get('item_id', c.name.removeprefix('OS_'))
                out.append((iid, d.get('label') or iid, ''))
        _cache['mix_' + typ] = out
        return out
    return cb


def _pick_items(self, context):
    out = [(c.name, items_api().item_data(c).get('label') or c.name, '', '', n)
           for n, (_, c) in enumerate(filtered_items(context.scene))]
    _cache['pick'] = out or [('NONE', '(no items)', '', '', 0)]
    return _cache['pick']


def _pick_get(self):
    cur = bpy.context.scene.ostudio.item_index
    for n, (i, _) in enumerate(filtered_items(bpy.context.scene)):
        if i == cur:
            return n
    return 0


def _pick_set(self, value):
    fl = filtered_items(bpy.context.scene)
    if 0 <= value < len(fl):
        self.item_index = fl[value][0]


def _head_items(self, context):
    out = [('NONE', 'Base head', '')]
    d = paths.HEADS / (base_name(context.scene) or '')
    out += [(p.stem, p.stem.replace('h_', ''), '') for p in sorted(d.glob('h_*.glb'))]
    _cache['heads'] = out
    return out


def _action_items(self, context):
    out = [('NONE', 'No action', '')] + [(a.name, a.name, '') for a in bpy.data.actions]
    try:
        man = json.loads((paths.PROJECTS / 'manifest.json').read_text())
        have = {a.name for a in bpy.data.actions}
        out += [('LIB:' + n, n + ' (base project)', '') for n in man.get(base_name(context.scene) or '', {}).get('actions', [])
                if n not in have]
    except (OSError, ValueError):
        pass
    _cache['actions'] = out
    return out


def _thickness_update(self, context):
    from . import views
    views.set_thickness(context.scene, self.thickness)


def _refresh_if(view):
    def update(self, context):
        if self.view == view:
            from . import views
            try:
                views.refresh(context)
            except Exception as e:
                print(f'[outfit_studio] {view} refresh failed: {e}')
    return update


class OSLegendItem(bpy.types.PropertyGroup):
    color: FloatVectorProperty(subtype='COLOR', size=4, min=0.0, max=1.0, default=(1, 1, 1, 1))


class OStudioProps(bpy.types.PropertyGroup):
    item_index: IntProperty(name='Item', min=0, default=0, description='Index into the item list')
    item_pick: EnumProperty(name='Item', items=_pick_items, get=_pick_get, set=_pick_set)
    filter_text: StringProperty(name='Filter', default='', options={'TEXTEDIT_UPDATE'})
    filter_type: EnumProperty(name='Type', items=(('ALL', 'All types', ''),) + TYPES, default='ALL')
    filter_status: EnumProperty(name='Status', items=(('ALL', 'All statuses', ''),) +
                                tuple((s, s.replace('_', ' ').title(), '') for s in STATUSES), default='ALL')
    lod: EnumProperty(name='LOD', items=(('0p4', 'LOD 0.4%', ''), ('0p2', 'LOD 0.2%', ''), ('0p1', 'LOD 0.1%', '')),
                      default='0p4')
    view: EnumProperty(name='View', items=VIEWS, default='NONE')
    mix_outfit: EnumProperty(name='Outfit', items=_mix_items('outfit'))
    mix_body_armor: EnumProperty(name='Body armour', items=_mix_items('body_armor'))
    mix_gauntlets: EnumProperty(name='Gauntlets', items=_mix_items('gauntlets'))
    mix_boots: EnumProperty(name='Boots', items=_mix_items('boots'))
    thickness: FloatProperty(name='Thickness', min=0.0, max=1.0, default=0.0, update=_thickness_update,
                             description='Drives every bulk_* shape key on the base and the items')
    pose_action: EnumProperty(name='Action', items=_action_items, update=_refresh_if('POSE'))
    palette_seed: IntProperty(name='Palette seed', default=1)
    poke_all_frames: BoolProperty(name='Whole action', default=False,
                                  description='Scan the frames of the active action instead of the current frame')
    poke_max: FloatProperty(name='Reach', default=0.05, min=0.001, max=0.5, unit='LENGTH',
                            description='Only base vertices this close to the item are tested')
    concept_view: EnumProperty(name='Concept view', default='front', update=_refresh_if('CONCEPT'),
                               items=tuple((v, v.replace('_', ' ').title(), '') for v in
                                           ('front', 'front_three_quarter', 'back', 'back_three_quarter')))
    head_choice: EnumProperty(name='Head', items=_head_items)
    legend: CollectionProperty(type=OSLegendItem)


CLASSES = (OSLegendItem, OStudioProps)


@persistent
def _on_load(*_):
    """Linked shape-key values are not saved: re-apply the thickness slider after open. A linked
    override rig also comes back in REST position, which hides every pose: put it in POSE."""
    try:
        sc = bpy.context.scene
        from . import project
        if project.mode() == 'outfit':
            rig = project.find_base(sc)[0]
            if rig and rig.data.pose_position != 'POSE':
                rig.data.pose_position = 'POSE'
        if sc.ostudio.thickness:
            from . import views
            views.set_thickness(sc, sc.ostudio.thickness)
    except Exception as e:
        print('[outfit_studio] load handler:', e)


def register():
    bpy.types.Scene.ostudio = PointerProperty(type=OStudioProps)
    bpy.app.handlers.load_post.append(_on_load)


def unregister():
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    del bpy.types.Scene.ostudio
