"""Project modes and outfit-project creation.

`projects/<base>.blend` is base mode, `outfit_projects/*.blend` outfit mode. An outfit project
*links* the `Character` collection of its base with a library-override hierarchy (rig posable,
modifiers addable on `base`); nothing is copied. Rig/base fingerprints (SPEC section 2) are
computed here and exported for the importer."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import bpy
import numpy as np
from bpy.props import EnumProperty

from .. import paths

VERSION = 1
SOCKETS = ('HipSheath', 'RightHandAttachment', 'CastEmissionRight', 'LeftHandAttachment', 'CastEmissionLeft',
           'BackSheath', 'BackShield', 'LeftKneePole', 'RightKneePole', 'ShieldAttachment')
META_KEY = 'outfit_studio'


# ---------------------------------------------------------------- mode / metadata
def mode(filepath=None, scene=None):
    """'base' | 'outfit' | 'none' from the open file (outfit projects saved elsewhere are found by their metadata)."""
    fp = filepath if filepath is not None else bpy.data.filepath
    if fp:
        p = Path(fp).resolve()
        if p.suffix == '.blend' and p.parent == paths.PROJECTS.resolve():
            return 'base'
        if p.suffix == '.blend' and p.parent == paths.OUTFIT_PROJECTS.resolve():
            return 'outfit'
    sc = scene or bpy.context.scene
    return 'outfit' if meta(sc).get('created') else 'none'


def meta(scene):
    try:
        return json.loads(scene.get(META_KEY, '{}'))
    except (TypeError, ValueError):
        return {}


def set_meta(scene, d):
    scene[META_KEY] = json.dumps(d)


def base_name(scene=None):
    """Base name of the open project (`dwarf_male`), from the file name or the stored metadata."""
    sc = scene or bpy.context.scene
    b = meta(sc).get('base')
    if b:
        return b
    stem = Path(bpy.data.filepath).stem if bpy.data.filepath else ''
    stem = stem.removesuffix('_outfits')
    return stem if stem in paths.bases() else ''


def find_base(scene=None):
    """(rig, base) objects of the Character in this scene (the overrides in an outfit project)."""
    sc = scene or bpy.context.scene
    rig = next((o for o in sc.objects if o.type == 'ARMATURE' and 'c_root_bend.x' in o.data.bones), None)
    if rig is None:
        rig = next((o for o in sc.objects if o.type == 'ARMATURE' and 'root.x' in o.data.bones), None)
    base = next((o for o in sc.objects if o.type == 'MESH' and rig and any(
        m.type == 'ARMATURE' and m.object == rig for m in o.modifiers) and o.data.shape_keys
        and 'head.x' in o.vertex_groups), None)
    return rig, base


def base_objects(context):
    return find_base(context.scene)


# ---------------------------------------------------------------- fingerprints
def _f(x):
    return f'{float(x) + 0.0:.5f}'


def rig_fingerprint(rig):
    """sha256 of the sorted (bone, parent, rest head/tail/roll) entries of the deform bones and sockets."""
    rows = []
    for b in rig.data.bones:
        if not (b.use_deform or b.name in SOCKETS):
            continue
        roll = bpy.types.Bone.AxisRollFromMatrix(b.matrix_local.to_3x3())[1]
        rows.append([b.name, b.parent.name if b.parent else '', *map(_f, b.head_local), *map(_f, b.tail_local), _f(roll)])
    return hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest()


def base_fingerprint(base, rig):
    """sha256 of the rounded rest vertex positions of `base` in armature space."""
    me = base.data
    v = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get('co', v)
    m = np.array(rig.matrix_world.inverted() @ base.matrix_world)
    p = v.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3]
    p = np.round(p.astype(np.float64), 5) + 0.0
    return hashlib.sha256(p.tobytes()).hexdigest()


def status(scene=None):
    """Compact health report of the open project: fingerprints vs stored, deform and socket names."""
    rig, base = find_base(scene)
    if not rig or not base:
        return {'ok': False, 'problems': ['no Character rig/base in this scene']}
    m, bad = meta(scene or bpy.context.scene), []
    rf, bf = rig_fingerprint(rig), base_fingerprint(base, rig)
    if m.get('rig_fingerprint') and m['rig_fingerprint'] != rf:
        bad.append('rig fingerprint differs from the one stored at creation')
    if m.get('base_fingerprint') and m['base_fingerprint'] != bf:
        bad.append('base mesh fingerprint differs from the one stored at creation')
    bad += [f'socket bone missing: {s}' for s in SOCKETS if s not in rig.data.bones]
    deform = sum(b.use_deform for b in rig.data.bones)
    return {'ok': not bad, 'problems': bad, 'rig_fingerprint': rf, 'base_fingerprint': bf, 'deform': deform,
            'bones': len(rig.data.bones), 'ap': sum(b.name.startswith('AP_') for b in rig.data.bones)}


# ---------------------------------------------------------------- creation
def create_outfit_project(base, path=None):
    """New empty file linking projects/<base>.blend's Character as a library override; saved, never overwriting.

    Replaces the current session (like File > New). Returns the saved path."""
    src = paths.base_project(base)
    if not src.exists():
        raise FileNotFoundError(f'no base project {src}')
    path = Path(path) if path else paths.outfit_project(base)
    if path.exists():
        raise FileExistsError(f'{path} exists; open it instead of creating it again')
    path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_homefile(use_empty=True)
    scene, vl = bpy.context.scene, bpy.context.view_layer
    with bpy.data.libraries.load(str(src), link=True, relative=True) as (a, b):
        if 'Character' not in a.collections:
            raise ValueError(f'{src} has no Character collection')
        b.collections = ['Character']
    linked = b.collections[0]
    inst = bpy.data.objects.new('Character', None)
    inst.instance_type, inst.instance_collection = 'COLLECTION', linked
    scene.collection.objects.link(inst)
    ov = linked.override_hierarchy_create(scene, vl, reference=inst, do_fully_editable=True)
    if ov is None or not ov.override_library:
        raise RuntimeError('library override of the Character collection failed')
    if inst.name in bpy.data.objects:
        bpy.data.objects.remove(inst)
    rig, bobj = find_base(scene)
    if not rig or not bobj:
        raise RuntimeError('overridden Character has no rig/base')
    rig.data.pose_position = 'POSE'
    items =bpy.data.collections.new('OS_Items')
    scene.collection.children.link(items)
    scene.name = f'{base} outfits'
    set_meta(scene, {'base': base, 'rig_fingerprint': rig_fingerprint(rig), 'base_fingerprint': base_fingerprint(bobj, rig),
                     'created': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'version': VERSION})
    bpy.ops.wm.save_as_mainfile(filepath=str(path), relative_remap=True, compress=False)
    return path


# ---------------------------------------------------------------- operators / panel
_cache = {}


def _base_items(self, context):
    items = [(b, b.replace('_', ' ').title(), '') for b in paths.bases()]
    _cache['bases'] = items
    return items


def _outfit_file_items(self, context):
    items = [(p.stem.removesuffix('_outfits'), p.stem.removesuffix('_outfits').replace('_', ' ').title(), str(p))
             for p in sorted(paths.OUTFIT_PROJECTS.glob('*_outfits.blend'))] or [('NONE', '(none yet)', '')]
    _cache['files'] = items
    return items


class OSTUDIO_OT_project_create(bpy.types.Operator):
    bl_idname = 'ostudio.project_create'
    bl_label = 'Create outfit project'
    bl_description = 'Create outfit_projects/<base>_outfits.blend linked to the base project (replaces the current scene)'
    base: EnumProperty(name='Base', items=_base_items)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        self.layout.prop(self, 'base')
        self.layout.label(text='Replaces the current scene; an existing project is never overwritten.')

    def execute(self, context):
        try:
            p = create_outfit_project(self.base)
        except (FileExistsError, FileNotFoundError, RuntimeError, ValueError) as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, f'Created {p}')
        return {'FINISHED'}


class OSTUDIO_OT_project_open(bpy.types.Operator):
    bl_idname = 'ostudio.project_open'
    bl_label = 'Open outfit project'
    bl_description = 'Open an existing outfit project'
    base: EnumProperty(name='Base', items=_outfit_file_items)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        p = paths.outfit_project(self.base)
        if not p.exists():
            self.report({'ERROR'}, f'{p} does not exist; create it first')
            return {'CANCELLED'}
        return bpy.ops.wm.open_mainfile(filepath=str(p))


def draw(layout, context):
    """Shown when the open file is neither a base nor an outfit project."""
    box = layout.box()
    box.label(text='Not an Outfit Studio project', icon='INFO')
    box.operator('ostudio.project_create', icon='ADD')
    box.operator('ostudio.project_open', icon='FILE_FOLDER')


CLASSES = (OSTUDIO_OT_project_create, OSTUDIO_OT_project_open)
