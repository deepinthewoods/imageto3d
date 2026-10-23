"""Arm/leg spacing for exported animations (SPEC §7 "Spacing").

Papercraft armour is wide at the shoulders and thighs, so retargeted clips push the limbs
apart a little: the arms abduct (additive rotation of `c_arm_fk.l/.r`, faded out as the arm
rises towards horizontal) and the feet spread (constant lateral offset of `c_foot_ik.l/.r`
and `c_leg_pole.l/.r`, which are the IK targets at ik_fk_switch 0).
`apply_spacing()` edits the keys of a retargeted action in place; settings live in
`scene["outfit_studio"]["spacing"] = {arm_deg, leg_cm, overrides{clip: {arm_deg, leg_cm}}}`.
"""
import json
import math

import bpy
from mathutils import Matrix, Vector

DEFAULTS = dict(arm_deg=8.0, leg_cm=3.0)
FADE_START = 90.0       # arm angle from straight down at which the push is gone
FADE_SPAN = 60.0        # ... and full strength below 90 - 60 degrees
ALGO = 2                # bump when the maths change: part of the per-clip settings hash
SIDES = ('l', 'r')


# --- settings -----------------------------------------------------------------------------------

def _root(scene):
    raw = scene.get('outfit_studio')
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return raw.to_dict() if hasattr(raw, 'to_dict') else dict(raw)


def read_settings(scene, clip=None):
    """{arm_deg, leg_cm, overrides} for the scene; with `clip` (source name/stem) the clip override is merged in."""
    data = dict(DEFAULTS, overrides={})
    data.update({k: v for k, v in _root(scene).get('spacing', {}).items() if v is not None})
    if clip:
        names = {clip, clip.removesuffix('_remap')}
        for key, over in data.get('overrides', {}).items():
            if key in names or key.removesuffix('_remap') in names:
                data.update({k: over[k] for k in ('arm_deg', 'leg_cm') if k in over})
    data['arm_deg'], data['leg_cm'] = float(data['arm_deg']), float(data['leg_cm'])
    return data


def write_settings(scene, arm_deg=None, leg_cm=None, overrides=None):
    """Merge spacing values into scene["outfit_studio"] (a JSON string), leaving every other key alone."""
    root = _root(scene)
    spacing = dict(DEFAULTS, **root.get('spacing', {}))
    for key, value in (('arm_deg', arm_deg), ('leg_cm', leg_cm), ('overrides', overrides)):
        if value is not None:
            spacing[key] = value
    root['spacing'] = spacing
    scene['outfit_studio'] = json.dumps(root)
    return spacing


def settings_identity(arm_deg, leg_cm):
    return dict(arm_deg=round(float(arm_deg), 4), leg_cm=round(float(leg_cm), 4), algo=ALGO)


# --- helpers ------------------------------------------------------------------------------------

def _fcurves(action):
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                yield from bag.fcurves


def _assign(rig, action):
    ad = rig.animation_data or rig.animation_data_create()
    if ad.action != action:
        ad.action = action
    if getattr(ad, 'action_slot', None) is None and getattr(action, 'slots', None):
        ad.action_slot = action.slots[0]


def _evaluated(rig):
    return rig.evaluated_get(bpy.context.evaluated_depsgraph_get())


def _location_map(rig, name):
    """3x3 map from a pose bone's `location` delta to its world translation (Child-Of targets are static in clips)."""
    ad = rig.animation_data
    action, slot = ad.action, getattr(ad, 'action_slot', None)
    ad.action = None
    pb = rig.pose.bones[name]
    saved = pb.location.copy()
    pb.location = (0, 0, 0)
    bpy.context.view_layer.update()
    base = _evaluated(rig).pose.bones[name].head.copy()
    columns = []
    for i in range(3):
        pb.location = tuple(0.01 if j == i else 0.0 for j in range(3))
        bpy.context.view_layer.update()
        columns.append((_evaluated(rig).pose.bones[name].head - base) / 0.01)
    pb.location = saved
    ad.action = action
    if slot is not None:
        ad.action_slot = slot
    m = Matrix((columns[0], columns[1], columns[2])).transposed()          # columns = images of e_x, e_y, e_z
    return rig.matrix_world.to_3x3() @ m                                    # armature space -> world


def _lateral(rig, previous=None):
    """Horizontal unit vector towards the character's left (hips X axis), world space."""
    x = rig.matrix_world.to_3x3() @ _evaluated(rig).pose.bones['c_root_master.x'].matrix.to_3x3().col[0]
    v = Vector((x.x, x.y, 0.0))
    if v.length < 0.2:
        return previous or Vector((1.0, 0.0, 0.0))
    return v.normalized()


def abduction_deg(rig, bone_name):
    """Angle in degrees between the upper arm and straight down (world -Z)."""
    pb = _evaluated(rig).pose.bones[bone_name]
    d = rig.matrix_world.to_3x3() @ (pb.tail - pb.head)
    return math.degrees(d.angle(Vector((0, 0, -1)), 0.0))


def _write(action, path, index, frames, values):
    curve = next((c for c in _fcurves(action) if c.data_path == path and c.array_index == index), None)
    if curve is None:
        raise ValueError(f'Action {action.name} has no curve {path}[{index}]')
    for frame, value in zip(frames, values):
        curve.keyframe_points.insert(frame, value, options={'REPLACE', 'FAST'}).interpolation = 'LINEAR'
    curve.update()


# --- main ---------------------------------------------------------------------------------------

def apply_spacing(rig, action, arm_deg=8.0, leg_cm=3.0):
    """Add arm abduction and foot spread to the (dense) keys of `action` on `rig`; returns a stats dict.

    The action is assigned to the rig, left at its first frame, and edited in place; call it
    on a copy when the original must survive (see `ostudio.spacing_preview`).
    """
    stats = dict(arm_deg=arm_deg, leg_cm=leg_cm, frames=0, arms=False, legs=False)
    if not arm_deg and not leg_cm:
        return stats
    scene = bpy.context.scene
    _assign(rig, action)
    first, last = int(action.frame_range[0]), int(action.frame_range[1])
    frames = list(range(first, last + 1))
    arms = [s for s in SIDES if arm_deg and f'c_arm_fk.{s}' in rig.pose.bones]
    legs = [s for s in SIDES if leg_cm and f'c_foot_ik.{s}' in rig.pose.bones and f'c_leg_pole.{s}' in rig.pose.bones]
    for s in arms:
        if rig.pose.bones[f'c_arm_fk.{s}'].rotation_mode != 'XYZ':
            raise ValueError('Spacing expects Euler XYZ arm controls')
    saved_frame = scene.frame_current
    scene.frame_set(first)
    maps = {n: _location_map(rig, n) for s in legs for n in (f'c_foot_ik.{s}', f'c_leg_pole.{s}')}
    maps = {n: m.inverted() for n, m in maps.items()}
    scene.frame_set(first)
    euler = {s: [] for s in arms}
    loc = {n: [] for n in maps}
    lateral = None
    for frame in frames:
        scene.frame_set(frame)
        lateral = _lateral(rig, lateral)
        ev = _evaluated(rig).pose.bones
        for s in arms:
            name = f'c_arm_fk.{s}'
            fade = min(1.0, max(0.0, (FADE_START - abduction_deg(rig, name)) / FADE_SPAN))
            pose = ev[name]
            cur = pose.rotation_euler.copy()
            basis = cur.to_matrix()
            frame_rot = pose.matrix.to_3x3().normalized() @ basis.inverted()     # parent-rest frame in armature space
            d = pose.tail - pose.head
            out = rig.matrix_world.inverted().to_3x3() @ (lateral * (1.0 if s == 'l' else -1.0))
            axis = d.cross(out)                                                  # swings the tip towards `out`
            if axis.length > 1e-6 and fade > 0.0:
                local = frame_rot.inverted() @ axis.normalized()
                basis = Matrix.Rotation(math.radians(arm_deg) * fade, 3, local) @ basis
            euler[s].append(basis.to_euler('XYZ', cur))
        for n, minv in maps.items():
            side = 1.0 if n.endswith('.l') else -1.0
            delta = minv @ (lateral * (side * leg_cm / 100.0))
            loc[n].append(ev[n].location + delta)
    for s in arms:
        for i in range(3):
            _write(action, f'pose.bones["c_arm_fk.{s}"].rotation_euler', i, frames, [e[i] for e in euler[s]])
    for n, rows in loc.items():
        for i in range(3):
            _write(action, f'pose.bones["{n}"].location', i, frames, [v[i] for v in rows])
    if legs and rig.get('ce_foot_surface_profile'):         # legless: re-bake the surface feet from the moved IK feet
        from_ce = _ce('legless_feet')
        action.pop('ce_foot_surface_bake', None)
        from_ce.bake(rig, action)
    scene.frame_set(first)
    stats.update(frames=len(frames), arms=bool(arms), legs=bool(legs), legless_rebaked=bool(legs and rig.get('ce_foot_surface_profile')))
    scene.frame_current = saved_frame
    return stats


def _ce(name):
    try:
        from . import arp_export
    except ImportError:
        import arp_export
    return arp_export.ce(name)


# --- operator -----------------------------------------------------------------------------------

class OSTUDIO_OT_spacing_preview(bpy.types.Operator):
    bl_idname = 'ostudio.spacing_preview'
    bl_label = 'Spacing preview'
    bl_description = 'Apply arm/leg spacing to a copy of the rig\'s active action (the original action is kept)'
    bl_options = {'REGISTER', 'UNDO'}
    arm_deg: bpy.props.FloatProperty(name='Arm angle (deg)', default=8.0, min=-45.0, max=45.0)
    leg_cm: bpy.props.FloatProperty(name='Leg offset (cm)', default=3.0, min=-20.0, max=20.0)
    save: bpy.props.BoolProperty(name='Save as base setting', default=False)

    @classmethod
    def poll(cls, context):
        rig = _find_rig(context)
        return rig is not None and rig.animation_data is not None and rig.animation_data.action is not None

    def invoke(self, context, event):
        s = read_settings(context.scene)
        self.arm_deg, self.leg_cm = s['arm_deg'], s['leg_cm']
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        rig = _find_rig(context)
        original = rig.animation_data.action
        if original.name.endswith('__spaced'):
            source = bpy.data.actions.get(original.name[:-len('__spaced')]) or original
        else:
            source = original
        copy = source.copy()
        copy.name = source.name + '__spaced'
        copy.use_fake_user = False
        stats = apply_spacing(rig, copy, self.arm_deg, self.leg_cm)
        if self.save:
            write_settings(context.scene, self.arm_deg, self.leg_cm)
        self.report({'INFO'}, f"Spacing preview on {copy.name}: arms {stats['arms']}, legs {stats['legs']}")
        return {'FINISHED'}


def _find_rig(context):
    obj = context.object
    if obj is not None and obj.type == 'ARMATURE':
        return obj
    base = bpy.data.objects.get('base')
    rig = base.find_armature() if base else None
    return rig or bpy.data.objects.get('rig')


CLASSES = (OSTUDIO_OT_spacing_preview,)


def draw(layout, context):
    s = read_settings(context.scene)
    box = layout.box()
    box.label(text='Spacing')
    box.label(text=f"Arms {s['arm_deg']:g} deg, legs {s['leg_cm']:g} cm")
    box.operator('ostudio.spacing_preview', icon='ARMATURE_DATA')
