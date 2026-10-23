"""Outfit-mode operators: item navigation, status, views, head swap, concept overlay, poke check, pose."""
import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty

from .. import paths
from . import props, views

_STATUS = tuple((s, s.replace('_', ' ').title(), '') for s in props.STATUSES)
_ERRORS = (views.ViewError, FileNotFoundError, KeyError, RuntimeError, ValueError)


def _fail(op, e):
    op.report({'ERROR'}, str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e))
    return {'CANCELLED'}


class _Base:
    @classmethod
    def poll(cls, context):
        return hasattr(context.scene, 'ostudio')


class _Step(_Base):
    def _go(self, context, d):
        fl = props.filtered_items(context.scene)
        if not fl:
            self.report({'WARNING'}, 'no items match the filter')
            return {'CANCELLED'}
        o, pos = context.scene.ostudio, [i for i, _ in fl]
        k = (pos.index(o.item_index) + d) % len(pos) if o.item_index in pos else (0 if d > 0 else -1)
        o.item_index = pos[k]
        try:
            views.refresh(context)
        except _ERRORS as e:
            views.reset(context)
            self.report({'WARNING'}, f'view reset: {e}')
        return {'FINISHED'}


class OSTUDIO_OT_item_prev(_Step, bpy.types.Operator):
    bl_idname = 'ostudio.item_prev'
    bl_label = 'Previous item'

    def execute(self, context):
        return self._go(context, -1)


class OSTUDIO_OT_item_next(_Step, bpy.types.Operator):
    bl_idname = 'ostudio.item_next'
    bl_label = 'Next item'

    def execute(self, context):
        return self._go(context, 1)


class OSTUDIO_OT_status_set(_Base, bpy.types.Operator):
    bl_idname = 'ostudio.status_set'
    bl_label = 'Set item status'
    bl_description = 'Set the review status of the current item'
    status: EnumProperty(name='Status', items=_STATUS, default='approved')

    def execute(self, context):
        api = props.items_api()
        c = api.current_item(context)
        if c is None:
            self.report({'WARNING'}, 'no current item')
            return {'CANCELLED'}
        d = api.item_data(c)
        d['status'] = self.status
        api.set_item_data(c, d)
        return {'FINISHED'}


class OSTUDIO_OT_view(_Base, bpy.types.Operator):
    bl_idname = 'ostudio.view'
    bl_label = 'Show view'
    bl_options = {'REGISTER', 'UNDO'}
    name: EnumProperty(name='View', items=props.VIEWS, default='GAME')

    def execute(self, context):
        try:
            views.apply_view(context, self.name)
        except _ERRORS as e:
            return _fail(self, e)
        i = views.info(context.scene)
        if i:
            self.report({'INFO'}, i)
        return {'FINISHED'}


class OSTUDIO_OT_view_reset(_Base, bpy.types.Operator):
    bl_idname = 'ostudio.view_reset'
    bl_label = 'Reset view'
    bl_description = 'Restore visibility, colours, shading, camera and pose changed by the views'

    def execute(self, context):
        views.reset(context)
        return {'FINISHED'}


class OSTUDIO_OT_palette_reroll(_Base, bpy.types.Operator):
    bl_idname = 'ostudio.palette_reroll'
    bl_label = 'Reroll palette'

    def execute(self, context):
        o = context.scene.ostudio
        o.palette_seed += 1
        if o.view == 'PALETTE':
            views.refresh(context)
        return {'FINISHED'}


class OSTUDIO_OT_head_swap(_Base, bpy.types.Operator):
    bl_idname = 'ostudio.head_swap'
    bl_label = 'Seat head'
    bl_description = 'Seat the chosen library head on the head bone and hide the base head (Base head restores it)'
    bl_options = {'REGISTER', 'UNDO'}
    choice: StringProperty(name='Head', default='', description='Head file stem; empty uses the panel selection')

    def execute(self, context):
        try:
            views.head_swap(context, self.choice or context.scene.ostudio.head_choice)
        except _ERRORS as e:
            return _fail(self, e)
        return {'FINISHED'}


class OSTUDIO_OT_concept_overlay(_Base, bpy.types.Operator):
    bl_idname = 'ostudio.concept_overlay'
    bl_label = 'Concept overlay'
    bl_description = 'Camera of the concept view with the concept image as its background'
    view: EnumProperty(name='View', default='front', items=tuple(
        (v, v.replace('_', ' ').title(), '') for v in ('front', 'front_three_quarter', 'back', 'back_three_quarter')))

    def execute(self, context):
        context.scene.ostudio.concept_view = self.view
        try:
            views.apply_view(context, 'CONCEPT')
        except _ERRORS as e:
            return _fail(self, e)
        return {'FINISHED'}


class OSTUDIO_OT_poke_check(_Base, bpy.types.Operator):
    bl_idname = 'ostudio.poke_check'
    bl_label = 'Poke-through check'
    bl_description = 'Mark base vertices that stick out through the current item (current frame or whole action)'
    all_frames: BoolProperty(name='Whole action', default=False)
    reach: FloatProperty(name='Reach', default=0.05, min=0.001, max=0.5, unit='LENGTH')

    def execute(self, context):
        o = context.scene.ostudio
        o.poke_all_frames, o.poke_max = self.all_frames, self.reach
        try:
            views.apply_view(context, 'POKE')
        except _ERRORS as e:
            return _fail(self, e)
        self.report({'INFO'}, views.info(context.scene))
        return {'FINISHED'}


class OSTUDIO_OT_pose_play(_Base, bpy.types.Operator):
    bl_idname = 'ostudio.pose_play'
    bl_label = 'Play / stop'

    def execute(self, context):
        try:
            bpy.ops.screen.animation_play()
        except RuntimeError as e:
            return _fail(self, e)
        return {'FINISHED'}


class OSTUDIO_OT_pose_load(_Base, bpy.types.Operator):
    bl_idname = 'ostudio.pose_load'
    bl_label = 'Load clip'
    bl_description = 'Load an external animation clip from the anims folder (needs the animation module)'
    filepath: StringProperty(subtype='FILE_PATH', default=str(paths.ANIMS) + '/')

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        anims = props.lazy('anims')
        fn = getattr(anims, 'load_clip', None)
        if fn is None:
            self.report({'WARNING'}, 'clip loading is not available yet (blender/anims.py has no load_clip)')
            return {'CANCELLED'}
        try:
            fn(context, self.filepath)
        except _ERRORS as e:
            return _fail(self, e)
        return {'FINISHED'}


# ---------------------------------------------------------------- panel drawers
def draw_nav(layout, context):
    o, api = context.scene.ostudio, props.items_api()
    row = layout.row(align=True)
    row.operator('ostudio.item_prev', text='', icon='TRIA_LEFT')
    row.prop(o, 'item_pick', text='')
    row.operator('ostudio.item_next', text='', icon='TRIA_RIGHT')
    row = layout.row(align=True)
    row.prop(o, 'filter_type', text='')
    row.prop(o, 'filter_status', text='')
    layout.prop(o, 'filter_text', text='', icon='VIEWZOOM')
    c = api.current_item(context)
    if c is not None:
        d = api.item_data(c)
        row = layout.row()
        row.label(text=d.get('status', 'imported').replace('_', ' ').upper(), icon='CHECKMARK' if d.get('status') == 'approved' else 'DOT')
        row.operator('ostudio.status_set', text='Approve').status = 'approved'
        row.operator('ostudio.status_set', text='Needs review').status = 'needs_review'


def draw_mix(layout, context):
    o = context.scene.ostudio
    col = layout.column(align=True)
    for p in ('mix_outfit', 'mix_body_armor', 'mix_gauntlets', 'mix_boots'):
        col.prop(o, p)


def draw_views(layout, context):
    o = context.scene.ostudio
    grid = layout.grid_flow(row_major=True, columns=2, align=True)
    for vid in props.VIEW_BUTTONS:
        name = next(v[1] for v in props.VIEWS if v[0] == vid)
        grid.operator('ostudio.view', text=name, depress=(o.view == vid)).name = vid
    layout.operator('ostudio.view_reset', icon='LOOP_BACK')
    i = views.info(context.scene)
    if i:
        layout.label(text=i, icon='INFO')
    if len(o.legend):
        box = layout.box().column(align=True)
        for it in o.legend:
            box.prop(it, 'color', text=it.name)
    box = layout.box()
    v = o.view
    if v == 'PALETTE':
        box.operator('ostudio.palette_reroll', icon='FILE_REFRESH')
    elif v == 'POKE':
        box.prop(o, 'poke_all_frames')
        box.prop(o, 'poke_max')
    elif v == 'POSE':
        box.prop(o, 'pose_action', text='')
        row = box.row(align=True)
        row.operator('ostudio.pose_play', icon='PLAY')
        row.operator('ostudio.pose_load', icon='FILE_FOLDER')
    elif v == 'HEAD':
        box.prop(o, 'head_choice', text='')
        box.operator('ostudio.head_swap', icon='USER')
    elif v == 'THICKNESS':
        box.prop(o, 'thickness', slider=True)
    elif v == 'CONCEPT':
        box.prop(o, 'concept_view', text='')
    else:
        box.label(text='Pick a view for its options')
    row = layout.row(align=True)
    row.prop(o, 'lod', text='')
    if props.has_op('ostudio.reimport'):
        row.operator('ostudio.reimport', text='Re-import at LOD', icon='IMPORT')


CLASSES = (OSTUDIO_OT_item_prev, OSTUDIO_OT_item_next, OSTUDIO_OT_status_set, OSTUDIO_OT_view, OSTUDIO_OT_view_reset,
           OSTUDIO_OT_palette_reroll, OSTUDIO_OT_head_swap, OSTUDIO_OT_concept_overlay, OSTUDIO_OT_poke_check,
           OSTUDIO_OT_pose_play, OSTUDIO_OT_pose_load)
