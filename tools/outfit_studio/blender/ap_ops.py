"""Attachment-point placement v2: operators and rig I/O (SPEC section 5).

The maths lives in `core/ap` (pure numpy); this module only reads the base mesh and rig into
arrays, previews the result as coloured empties in `OS_AP_Preview` and writes AP bones that
follow the existing contract (name, +Y outward normal, +Z up/growth tangent, parent = the
bone of the face, non-deform, `cc` tag, bone collection). Operators (base mode):
`ostudio.ap_generate`, `ap_commit`, `ap_lock`, `ap_clear_preview`, `ap_report`.
"""
import contextlib
import json
import re
from pathlib import Path

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty
from mathutils import Matrix, Vector

from ..core import ap as apcore

PREFIX = 'AP_'
TAG = 'cc'
COLLECTION = 'Attachment Points'
LEGACY_COLLECTION = 'Attachments'          # what the project rigs actually use
PREVIEW = 'OS_AP_Preview'
META_KEY = 'outfit_studio'                 # scene JSON shared with the rest of the add-on
LOCK = 'os_locked'
DEFAULT_LENGTH = 0.02

HEAD_STRATEGIES = (('landmark_geodesic', 'Landmarks + geodesic', 'Hairline from head landmarks and surface distance'),
                   ('template', 'Template', 'Masks transferred from a saved reference head'),
                   ('texture', 'Texture', 'Hair-coloured texture clusters (needs an image)'),
                   ('legacy', 'Legacy plane', 'The old plane through temples and crown'))
BODY_STRATEGIES = (('cylinder', 'Cylinder', 'Iso-angle surface lines about the bone axis'),
                   ('keep', 'Keep existing', 'Leave the rig\'s current points of this family untouched'))
GROUP_RE = re.compile(r'^(?P<group>.*?)(?:_(?P<i>\d+))?$')


# ---------------------------------------------------------------- settings (scene JSON)
def _meta(scene):
    try:
        return json.loads(scene.get(META_KEY, '{}'))
    except (TypeError, ValueError):
        return {}


def _put_meta(scene, **kv):
    m = _meta(scene)
    m.update(kv)
    scene[META_KEY] = json.dumps(m)


def get_settings(scene):
    """AP settings: the defaults overlaid with `scene["outfit_studio"]["ap"]`."""
    s = dict(apcore.DEFAULTS)
    s.update({k: v for k, v in _meta(scene).get('ap', {}).items() if k in s})
    return s


def set_settings(scene, **kv):
    """Merge `kv` into the stored AP settings (other keys of the scene JSON are kept)."""
    ap = _meta(scene).get('ap', {})
    ap.update({k: v for k, v in kv.items() if k in apcore.DEFAULTS})
    _put_meta(scene, ap=ap)


_syncing = False


def _store(self, context):
    if _syncing:
        return
    set_settings(context.scene, **{k: getattr(self, k) for k in apcore.DEFAULTS})


def sync_props(scene):
    """Copy the stored settings into the UI property group (after a file load)."""
    global _syncing
    p = getattr(scene, 'ostudio_ap', None)
    if p is None:
        return
    _syncing = True
    try:
        for k, v in get_settings(scene).items():
            try:
                setattr(p, k, v)
            except (TypeError, AttributeError):
                pass
    finally:
        _syncing = False


class OSAPSettings(bpy.types.PropertyGroup):
    strategy_hair: EnumProperty(name='Hair region', items=HEAD_STRATEGIES, default='landmark_geodesic', update=_store)
    strategy_scatter: EnumProperty(name='Scatter region', items=HEAD_STRATEGIES, default='landmark_geodesic',
                                   update=_store)
    strategy_limbs: EnumProperty(name='Limbs', items=BODY_STRATEGIES, default='cylinder', update=_store)
    strategy_torso: EnumProperty(name='Torso', items=BODY_STRATEGIES, default='cylinder', update=_store)
    gen_hair: BoolProperty(name='Hair', default=True, update=_store)
    gen_scatter: BoolProperty(name='Scatter', default=True, update=_store)
    gen_limbs: BoolProperty(name='Limbs', default=True, update=_store)
    gen_torso: BoolProperty(name='Torso', default=True, update=_store)
    row_mode: EnumProperty(name='Counts', items=(('keep', 'Keep rig counts', ''), ('spacing', 'By spacing', '')),
                           default='keep', update=_store)
    spacing: FloatProperty(name='Spacing', unit='LENGTH', default=0.04, min=0.0, max=0.5, update=_store)
    density: FloatProperty(name='Density', default=1.0, min=0.1, max=4.0, update=_store)
    exclude_joints: BoolProperty(name='Exclude joints', default=True, update=_store)
    joint_purity: FloatProperty(name='Joint purity', default=0.8, min=0.5, max=1.0, update=_store)
    joint_floor: FloatProperty(name='Relax floor', default=0.55, min=0.3, max=1.0, update=_store)
    hairline: FloatProperty(name='Hairline', default=0.20, min=0.05, max=0.5, update=_store)
    normal_radius: FloatProperty(name='Normal radius', default=0.015, min=0.0, max=0.1, update=_store)
    symmetry: BoolProperty(name='Mirror left half', default=True, update=_store)
    template: StringProperty(name='Template', default='default', update=_store)
    texture_image: StringProperty(name='Texture', default='', update=_store)
    limb_tangent: EnumProperty(name='Limb +Z', items=(('proximal', 'Toward body', ''), ('distal', 'Away from body', '')),
                               default='proximal', update=_store)


# ---------------------------------------------------------------- rig + mesh -> arrays
def find_objects(scene):
    """(rig, base mesh) of the Character in the scene."""
    rig = scene.objects.get('rig')
    if rig is None or rig.type != 'ARMATURE':
        rig = next((o for o in scene.objects if o.type == 'ARMATURE' and 'root.x' in o.data.bones), None)
    base = scene.objects.get('base')
    if base is None or base.type != 'MESH':
        base = next((o for o in scene.objects if o.type == 'MESH' and rig and any(
            m.type == 'ARMATURE' and m.object == rig for m in o.modifiers)), None)
    return rig, base


@contextlib.contextmanager
def rest_pose(rig):
    prev = rig.data.pose_position
    if prev != 'REST':
        rig.data.pose_position = 'REST'
        bpy.context.view_layer.update()
    try:
        yield
    finally:
        if prev != 'REST':
            rig.data.pose_position = prev
            bpy.context.view_layer.update()


def part_of_material(name):
    """`bone_arm_stretch.l.016` -> `arm_stretch.l` (a trailing .NNN is a Blender duplicate suffix)."""
    n = name[5:] if name.startswith('bone_') else name
    return re.sub(r'\.\d{3}$', '', n)


def body_data(rig, obj):
    """The base mesh in REST pose, armature-local: triangles, part per triangle, deform weights,
    bone head/tail positions. This is the input of `core.ap`."""
    inv = rig.matrix_world.inverted() @ obj.matrix_world
    with rest_pose(rig):
        ev = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        me = ev.to_mesh()
        try:
            n = len(me.vertices)
            co = np.empty(n * 3)
            me.vertices.foreach_get('co', co)
            polys = [np.array(p.vertices) for p in me.polygons]
            mat = np.empty(len(me.polygons), np.int64)
            me.polygons.foreach_get('material_index', mat)
        finally:
            ev.to_mesh_clear()
    if n != len(obj.data.vertices):
        raise RuntimeError(f'{obj.name}: modifiers change the vertex count; AP placement needs the plain base mesh')
    M = np.array(inv)
    V = co.reshape(-1, 3) @ M[:3, :3].T + M[:3, 3]
    tri, tpoly = apcore.mesh.triangulate(V, polys)
    names = [part_of_material(m.name) if m else '' for m in obj.data.materials]
    part = mat[tpoly]
    deform = [g.name for g in obj.vertex_groups if g.name in rig.data.bones and rig.data.bones[g.name].use_deform]
    col = {g.index: deform.index(g.name) for g in obj.vertex_groups if g.name in deform}
    W = np.zeros((n, len(deform)))
    for v in obj.data.vertices:
        for ge in v.groups:
            if ge.group in col:
                W[v.index, col[ge.group]] = ge.weight
    bones = {b.name: (np.array(b.head_local), np.array(b.tail_local)) for b in rig.data.bones
             if not b.name.startswith(PREFIX)}
    return {'V': V, 'F': tri, 'poly': tpoly, 'part': part, 'part_names': names, 'W': W,
            'groups': deform, 'bones': bones, 'x0': 0.0}


# ---------------------------------------------------------------- existing AP bones
def split_name(name):
    """`AP_armL_up_03` -> ('armL_up', 3); `AP_crown` -> ('crown', None)."""
    core = name[len(PREFIX):] if name.startswith(PREFIX) else name
    m = GROUP_RE.match(core)
    return (m.group('group'), int(m.group('i'))) if m.group('i') is not None else (core, None)


def existing_points(rig):
    """{bone name: {'head','y','z','parent','locked','group','index'}} of the rig's AP bones (armature-local)."""
    out = {}
    for b in rig.data.bones:
        if not b.name.startswith(PREFIX):
            continue
        m = np.array(b.matrix_local)
        g, i = split_name(b.name)
        out[b.name] = {'head': m[:3, 3].copy(), 'y': m[:3, 1].copy(), 'z': m[:3, 2].copy(),
                       'parent': b.parent.name if b.parent else '', 'locked': bool(b.get(LOCK, 0)),
                       'group': g, 'index': i}
    return out


def group_counts(points):
    c = {}
    for p in points.values():
        c[p['group']] = c.get(p['group'], 0) + 1
    return c


# ---------------------------------------------------------------- optional inputs of the head regions
TEMPLATE_DIR = Path(__file__).resolve().parents[1] / 'data' / 'ap_templates'


def texture_colors(obj, image):
    """Texture colour per vertex of `obj` (average of its loops' UV samples), or None."""
    me = obj.data
    if image is None or not me.uv_layers.active or not image.size[0]:
        return None
    w, h = image.size
    px = np.empty(w * h * image.channels, np.float32)
    image.pixels.foreach_get(px)
    px = px.reshape(h, w, image.channels)[..., :3]
    uv = np.empty(len(me.loops) * 2)
    me.uv_layers.active.data.foreach_get('uv', uv)
    uv = uv.reshape(-1, 2) % 1.0
    vi = np.empty(len(me.loops), np.int64)
    me.loops.foreach_get('vertex_index', vi)
    col = px[np.clip((uv[:, 1] * (h - 1)).round().astype(int), 0, h - 1),
             np.clip((uv[:, 0] * (w - 1)).round().astype(int), 0, w - 1)]
    cnt = np.maximum(np.bincount(vi, minlength=len(me.vertices)), 1)
    return np.stack([np.bincount(vi, col[:, k], len(me.vertices)) / cnt for k in range(3)], 1)


def paint_weights(obj, name):
    """Weights of the vertex group `name` (manual region override), or None when absent/empty."""
    vg = obj.vertex_groups.get(name)
    if vg is None:
        return None
    w = np.zeros(len(obj.data.vertices))
    for v in obj.data.vertices:
        for ge in v.groups:
            if ge.group == vg.index:
                w[v.index] = ge.weight
    return w if (w > 0).any() else None


def ear_weights(obj):
    """1 on vertices of any `ear_*` / `horn_*` vertex group (hair and face points avoid them), else None."""
    ids = {g.index for g in obj.vertex_groups if g.name.startswith(('ear_', 'horn_'))}
    if not ids:
        return None
    w = np.zeros(len(obj.data.vertices))
    for v in obj.data.vertices:
        if any(ge.group in ids and ge.weight > .5 for ge in v.groups):
            w[v.index] = 1.0
    return w if w.any() else None


def load_template(name):
    f = TEMPLATE_DIR / f'{name}.npz'
    if not f.exists():
        return None
    z = np.load(f)
    return {'V': z['V'], 'scalp': z['scalp'], 'face': z['face']}


def head_matrices(verts, faces, uvs=None, texture=None, counts=None, settings=None, template=None,
                  exclude=None, paint=None):
    """The head-library entry point: AP sockets for any head mesh. `verts` (n,3), `faces` (m,3),
    optional per-vertex `uvs` (n,2) with an RGB `texture` array (h,w,3) for the texture strategy.
    Returns {'AP_<name>': 4x4 numpy matrix} in the mesh's own frame (+Z up, -Y forward, +X left)."""
    rgb = None
    if texture is not None and uvs is not None:
        t = np.asarray(texture, float)
        u = np.asarray(uvs, float) % 1.0
        rgb = t[np.clip((u[:, 1] * (t.shape[0] - 1)).round().astype(int), 0, t.shape[0] - 1),
                np.clip((u[:, 0] * (t.shape[1] - 1)).round().astype(int), 0, t.shape[1] - 1)][:, :3]
    st = dict(settings or {})
    if rgb is not None:
        st.setdefault('strategy_hair', 'texture')
        st.setdefault('strategy_scatter', 'texture')
    return apcore.place_on_head(verts, faces, counts, st, texture_rgb=rgb, template=template, exclude=exclude,
                                paint=paint)


# ---------------------------------------------------------------- generation
_LAST = {}          # rig name -> {'ctx', 'res', 'settings', 'counts', 'report'}


def known_counts(rig):
    """The rig's current groups the generator knows, with their point counts."""
    c = group_counts(existing_points(rig))
    return {g: n for g, n in c.items() if apcore.family_of(g)}


def run_generate(scene, rig, base, settings=None):
    """Generate points for the rig's current groups; returns the cache entry (nothing is changed)."""
    s = dict(settings) if settings is not None else get_settings(scene)
    body = body_data(rig, base)
    template = load_template(s['template']) if 'template' in (s['strategy_hair'], s['strategy_scatter']) else None
    if 'texture' in (s['strategy_hair'], s['strategy_scatter']) and s['texture_image']:
        body['rgb'] = texture_colors(base, bpy.data.images.get(s['texture_image']))
    body['paint_scalp'] = paint_weights(base, 'os_region_scalp')
    body['exclude'] = ear_weights(base)
    ctx = apcore.prepare(body, s)
    counts = known_counts(rig) or apcore.groups.default_groups()
    res = apcore.generate(ctx, counts, template=template)
    rep = apcore.quality.report(ctx, res['points'])
    entry = {'ctx': ctx, 'res': res, 'settings': s, 'counts': counts, 'report': rep, 'body': body}
    _LAST[rig.name] = entry
    write_region_attributes(base, body, ctx)
    _put_meta(scene, ap_report=rep)
    return entry


def write_region_attributes(base, body, ctx):
    """Store the scalp/face masks as face attributes `os_region_<family>` on the base mesh."""
    masks = apcore.api.region_masks(ctx)
    if not masks:
        return
    me = base.data
    poly = np.asarray(body['poly'])
    cnt = np.maximum(np.bincount(poly, minlength=len(me.polygons)), 1)
    for k, tri_vals in masks.items():
        vals = np.bincount(poly, tri_vals, len(me.polygons)) / cnt
        name = f'os_region_{k}'
        attr = me.attributes.get(name) or me.attributes.new(name, 'FLOAT', 'FACE')
        attr.data.foreach_set('value', vals.astype(np.float32))
    me.update()


FAMILY_COLOUR = {'hair': (1.0, .75, .1), 'scatter': (.2, .9, .9), 'limbs': (.3, .5, 1.0), 'torso': (.4, 1.0, .4)}


def named_frames(res):
    """{AP bone name: 4x4} and {AP bone name: parent bone} of a generation result."""
    M = apcore.named_matrices(res['points'])
    par = {}
    for g, p in res['points'].items():
        for i, pb in enumerate(p['parent']):
            par[apcore.point_name(g, i)] = pb
    return M, par


def clear_preview():
    col = bpy.data.collections.get(PREVIEW)
    if col is None:
        return 0
    n = len(col.objects)
    for o in list(col.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    bpy.data.collections.remove(col)
    return n


def make_preview(scene, rig, frames, colour=None, name=PREVIEW, size=0.008):
    """Coloured empties (one per point, named like the bone) in the `name` collection."""
    col = bpy.data.collections.get(name) or bpy.data.collections.new(name)
    if col.name not in scene.collection.children:
        scene.collection.children.link(col)
    for nm, M in frames.items():
        e = bpy.data.objects.new(nm, None)
        e.empty_display_type = 'SPHERE'
        e.empty_display_size = size
        e.matrix_world = rig.matrix_world @ Matrix(M.tolist())
        fam = apcore.family_of(split_name(nm)[0])
        e.color = (*(colour or FAMILY_COLOUR.get(fam, (1, 1, 1))), 1.0)
        e.hide_select = False
        col.objects.link(e)
    return col


# ---------------------------------------------------------------- bones
def ap_collection(rig):
    """The bone collection the rig's AP bones already live in (the most common one), else
    'Attachments' / 'Attachment Points' (created)."""
    cnt = {}
    for b in rig.data.bones:
        if b.name.startswith(PREFIX):
            for c in b.collections:
                cnt[c.name] = cnt.get(c.name, 0) + 1
    if cnt:
        return rig.data.collections_all[max(cnt, key=cnt.get)]
    for nm in (LEGACY_COLLECTION, COLLECTION):
        c = rig.data.collections_all.get(nm)
        if c is not None:
            return c
    return rig.data.collections.new(COLLECTION)


def bone_length(rig):
    ls = [b.length for b in rig.data.bones if b.name.startswith(PREFIX) and b.length > 1e-5]
    return float(np.median(ls)) if ls else DEFAULT_LENGTH


def write_bones(context, rig, res):
    """Write the generated points as AP bones. Locked bones (`os_locked`) are never moved or
    deleted, groups the run skipped are left as they are. Returns {'written','removed','locked'}."""
    frames, parents = named_frames(res)
    existing = existing_points(rig)
    locked = {n for n, p in existing.items() if p['locked']}
    length = bone_length(rig)
    coll = ap_collection(rig)
    groups = set(res['points'])
    stale = [n for n, p in existing.items() if p['group'] in groups and n not in frames and n not in locked]
    if rig.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')
    rig.select_set(True)
    context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    eb = rig.data.edit_bones
    for n in stale:
        if eb.get(n) is not None:
            eb.remove(eb.get(n))
    written = []
    for nm, M in frames.items():
        if nm in locked:
            continue
        b = eb.get(nm) or eb.new(nm)
        head = Vector(M[:3, 3].tolist())
        b.head = head
        b.tail = head + Vector(M[:3, 1].tolist()) * length
        pb = parents.get(nm) or 'head.x'
        b.parent = eb.get(pb) or eb.get('head.x')
        b.use_connect = False
        b.use_deform = False
        b.align_roll(Vector(M[:3, 2].tolist()))
        written.append(nm)
    bpy.ops.object.mode_set(mode='OBJECT')
    for nm in written:
        db = rig.data.bones[nm]
        db[TAG] = 1
        db.use_deform = False
        coll.assign(db)
        if rig.pose and rig.pose.bones.get(nm) is not None:
            rig.pose.bones[nm][TAG] = 1
    thr = dict(_meta(context.scene).get('ap_thr', {}))
    thr.update({g: p.get('thr', 0.0) for g, p in res['points'].items()})
    _put_meta(context.scene, ap_thr=thr)
    return {'written': len(written), 'removed': len(stale), 'locked': len(locked & set(frames))}


# ---------------------------------------------------------------- operators
def _poll(context):
    rig, base = find_objects(context.scene)
    return rig is not None and base is not None and context.mode == 'OBJECT'


def report_text(rep):
    return apcore.quality.format_report(rep)


class OSTUDIO_OT_ap_generate(bpy.types.Operator):
    """Preview new attachment points as coloured empties (the rig is not changed)"""
    bl_idname = 'ostudio.ap_generate'
    bl_label = 'Generate preview'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _poll(context)

    def execute(self, context):
        rig, base = find_objects(context.scene)
        try:
            entry = run_generate(context.scene, rig, base)
        except Exception as e:                       # report instead of a traceback in the UI
            self.report({'ERROR'}, f'AP generation failed: {e}')
            return {'CANCELLED'}
        clear_preview()
        frames, _ = named_frames(entry['res'])
        make_preview(context.scene, rig, frames)
        n = len(frames)
        sk = entry['res']['skipped']
        self.report({'INFO'}, f'{n} points previewed in {PREVIEW}'
                              + (f'; {len(sk)} groups kept as they are ({", ".join(sk[:4])}...)' if sk else ''))
        for line in report_text(entry['report']).splitlines():
            print('[ap]', line)
        return {'FINISHED'}


class OSTUDIO_OT_ap_commit(bpy.types.Operator):
    """Write the previewed points as AP_ bones (locked bones stay untouched)"""
    bl_idname = 'ostudio.ap_commit'
    bl_label = 'Commit to rig'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _poll(context)

    def execute(self, context):
        rig, base = find_objects(context.scene)
        entry = _LAST.get(rig.name)
        if entry is None or bpy.data.collections.get(PREVIEW) is None:
            try:
                entry = run_generate(context.scene, rig, base)
            except Exception as e:
                self.report({'ERROR'}, f'AP generation failed: {e}')
                return {'CANCELLED'}
        out = write_bones(context, rig, entry['res'])
        clear_preview()
        self.report({'INFO'}, f"AP bones: {out['written']} written, {out['removed']} removed, "
                              f"{out['locked']} locked kept")
        return {'FINISHED'}


class OSTUDIO_OT_ap_lock(bpy.types.Operator):
    """Lock (or unlock) the selected AP bones: locked bones are never moved or deleted"""
    bl_idname = 'ostudio.ap_lock'
    bl_label = 'Lock selected'
    bl_options = {'REGISTER', 'UNDO'}

    lock: BoolProperty(name='Lock', default=True)

    @classmethod
    def poll(cls, context):
        rig, _ = find_objects(context.scene)
        return rig is not None

    def execute(self, context):
        rig, _ = find_objects(context.scene)
        if rig.mode == 'EDIT':
            sel = [b.name for b in rig.data.edit_bones if b.name.startswith(PREFIX) and (b.select or b.select_head)]
        else:
            sel = [b.name for b in rig.data.bones if b.name.startswith(PREFIX) and b.select]
            # selected preview empties also count
            sel += [o.name for o in context.selected_objects if o.name.startswith(PREFIX) and o.name in rig.data.bones]
        n = 0
        for nm in dict.fromkeys(sel):
            db = rig.data.bones.get(nm)
            if db is None:
                continue
            if self.lock:
                db[LOCK] = 1
            elif LOCK in db:
                del db[LOCK]
            n += 1
        self.report({'INFO'}, f"{'Locked' if self.lock else 'Unlocked'} {n} AP bones")
        return {'FINISHED'}


class OSTUDIO_OT_ap_clear_preview(bpy.types.Operator):
    """Remove the AP preview empties"""
    bl_idname = 'ostudio.ap_clear_preview'
    bl_label = 'Clear preview'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        self.report({'INFO'}, f'Removed {clear_preview()} preview empties')
        return {'FINISHED'}


class OSTUDIO_OT_ap_report(bpy.types.Operator):
    """Quality report of the rig's current AP bones (mirror error, spacing, scalp/face, joints)"""
    bl_idname = 'ostudio.ap_report'
    bl_label = 'Report'

    @classmethod
    def poll(cls, context):
        return _poll(context)

    def execute(self, context):
        rig, base = find_objects(context.scene)
        s = get_settings(context.scene)
        ctx = apcore.prepare(body_data(rig, base), s)
        pts = apcore.quality.from_rig({n: p for n, p in existing_points(rig).items() if apcore.family_of(p['group'])},
                                      _meta(context.scene).get('ap_thr'))
        rep = apcore.quality.report(ctx, pts)
        _put_meta(context.scene, ap_report_rig=rep)
        text = report_text(rep)
        for line in text.splitlines():
            self.report({'INFO'}, line)
            print('[ap]', line)
        return {'FINISHED'}


class OSTUDIO_OT_ap_save_template(bpy.types.Operator):
    """Save this head's scalp and face regions as the reference for the Template strategy"""
    bl_idname = 'ostudio.ap_save_template'
    bl_label = 'Save region template'

    name: StringProperty(name='Name', default='default')

    @classmethod
    def poll(cls, context):
        return _poll(context)

    def execute(self, context):
        rig, base = find_objects(context.scene)
        entry = _LAST.get(rig.name) or run_generate(context.scene, rig, base)
        ctx = entry['ctx']
        hr = apcore.api.head_context(ctx, ctx['head'], 'landmark_geodesic')
        TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
        np.savez(TEMPLATE_DIR / f'{self.name}.npz', V=ctx['head']['V'], scalp=hr['scalp'] > 0, face=hr['face'] > 0)
        self.report({'INFO'}, f'Saved template {self.name}')
        return {'FINISHED'}


CLASSES = (OSAPSettings, OSTUDIO_OT_ap_generate, OSTUDIO_OT_ap_commit, OSTUDIO_OT_ap_lock,
           OSTUDIO_OT_ap_clear_preview, OSTUDIO_OT_ap_report, OSTUDIO_OT_ap_save_template)


# ---------------------------------------------------------------- UI
def draw(layout, ctx):
    """Base-mode AP placement box (called from the Outfit Studio panel)."""
    sc = ctx.scene
    p = getattr(sc, 'ostudio_ap', None)
    box = layout.box()
    box.label(text='Attachment points', icon='EMPTY_AXIS')
    if p is None:
        box.label(text='AP settings not registered')
        return
    col = box.column(align=True)
    for key, label in (('hair', 'Hair + crown/temples'), ('scatter', 'Scatter'), ('limbs', 'Limb rows'),
                       ('torso', 'Torso lines')):
        row = col.row(align=True)
        row.prop(p, f'gen_{key}', text='')
        sub = row.row(align=True)
        sub.enabled = getattr(p, f'gen_{key}')
        sub.prop(p, f'strategy_{key}', text=label)
    col = box.column(align=True)
    col.prop(p, 'row_mode', text='Counts')
    row = col.row(align=True)
    row.prop(p, 'density')
    row.prop(p, 'spacing')
    row = col.row(align=True)
    row.prop(p, 'exclude_joints', text='Skip joints')
    sub = row.row(align=True)
    sub.enabled = p.exclude_joints
    sub.prop(p, 'joint_purity', text='Purity')
    col.prop(p, 'symmetry')
    col.prop(p, 'hairline')
    if 'template' in (p.strategy_hair, p.strategy_scatter):
        row = col.row(align=True)
        row.prop(p, 'template')
        row.operator('ostudio.ap_save_template', text='', icon='FILE_TICK')
    if 'texture' in (p.strategy_hair, p.strategy_scatter):
        col.prop_search(p, 'texture_image', bpy.data, 'images')
    row = box.row(align=True)
    row.operator('ostudio.ap_generate', icon='PLAY')
    row.operator('ostudio.ap_commit', icon='ARMATURE_DATA')
    row = box.row(align=True)
    row.operator('ostudio.ap_lock', icon='LOCKED').lock = True
    row.operator('ostudio.ap_lock', text='Unlock', icon='UNLOCKED').lock = False
    row = box.row(align=True)
    row.operator('ostudio.ap_clear_preview', icon='X')
    row.operator('ostudio.ap_report', icon='INFO')
    rep = _meta(sc).get('ap_report')
    if rep:
        c = box.column(align=True)
        for line in report_text(rep).splitlines():
            c.label(text=line)
        if rep.get('relaxed'):
            c.label(text=f"{len(rep['relaxed'])} groups relaxed the joint purity (blended weights)")


# ---------------------------------------------------------------- registration
@bpy.app.handlers.persistent
def _on_load(_=None):
    for sc in bpy.data.scenes:
        sync_props(sc)


def register():
    bpy.types.Scene.ostudio_ap = bpy.props.PointerProperty(type=OSAPSettings)
    if _on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load)


def unregister():
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    del bpy.types.Scene.ostudio_ap
