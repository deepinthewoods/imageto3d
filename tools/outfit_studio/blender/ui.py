"""N-panel tab "Outfit Studio" (SPEC section 4). Mode comes from the open file: base / outfit / none.

Sections owned by other modules are drawn through their `draw(layout, context)` when importable;
a drawer that fails shows its error instead of breaking the panel."""
import traceback

import bpy

from . import ops_outfit, project, props


def _call(layout, context, module, fn='draw'):
    mod = props.lazy(module)
    f = getattr(mod, fn, None)
    if f is None:
        layout.label(text=f'{module} not available', icon='ERROR')
        return
    try:
        f(layout, context)
    except Exception as e:
        traceback.print_exc()
        layout.label(text=f'{module}: {e}', icon='ERROR')


def _fallback_item(layout, context):
    api = props.items_api()
    c = api.current_item(context)
    if c is None:
        layout.label(text='No items yet')
        return
    d = api.item_data(c)
    layout.label(text=f'{d.get("label") or c.name}  ({d.get("type", "?")})')
    src = d.get('source', {})
    layout.label(text=f'{src.get("job_id", "?")} {src.get("module", "")} {src.get("lod", "")}'.strip())
    layout.label(text=f'{d.get("stats", {}).get("tris", "?")} triangles, {len(api.item_pieces(c))} pieces')
    for ch in d.get('channels', []):
        layout.label(text=f'{ch.get("id")}: {ch.get("role")}  {ch.get("material", "")}'.strip())


class _Panel:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Outfit Studio'
    mode = None

    @classmethod
    def poll(cls, context):
        return hasattr(context.scene, 'ostudio') and (cls.mode is None or project.mode() == cls.mode)


class OSTUDIO_PT_main(_Panel, bpy.types.Panel):
    bl_idname = 'OSTUDIO_PT_main'
    bl_label = 'Outfit Studio'

    def draw(self, context):
        m, layout = project.mode(), self.layout
        if m == 'none':
            project.draw(layout, context)
            return
        layout.label(text=f'{m.title()} mode: {project.base_name() or "?"}', icon='ARMATURE_DATA')
        if m == 'outfit':
            ops_outfit.draw_nav(layout, context)
            st = project.status()
            for p in st['problems']:
                layout.label(text=p, icon='ERROR')
        else:
            st = project.status()
            layout.label(text=f'{st.get("bones", 0)} bones, {st.get("deform", 0)} deform, {st.get("ap", 0)} AP',
                         icon='BONE_DATA')
            for p in st['problems']:
                layout.label(text=p, icon='ERROR')


class _Sub(_Panel):
    bl_parent_id = 'OSTUDIO_PT_main'
    bl_options = {'DEFAULT_CLOSED'}
    module = ''
    fn = 'draw'

    def draw(self, context):
        _call(self.layout, context, self.module, self.fn)


class OSTUDIO_PT_item(_Sub, bpy.types.Panel):
    bl_idname, bl_label, mode = 'OSTUDIO_PT_item', 'Item', 'outfit'
    bl_options = set()

    def draw(self, context):
        if props.lazy('items') is None:
            _fallback_item(self.layout, context)
        else:
            _call(self.layout, context, 'items', 'draw_item')


class OSTUDIO_PT_import(_Sub, bpy.types.Panel):
    bl_idname, bl_label, mode = 'OSTUDIO_PT_import', 'Import from pipeline', 'outfit'
    bl_options = set()

    def draw(self, context):
        _call(self.layout, context, 'importer', 'draw_import')
        row = self.layout.row(align=True)
        for op, text in (('ostudio.check_rig', 'Check rig'), ('ostudio.rebind_stale', 'Re-bind stale')):
            if props.has_op(op):
                row.operator(op, text=text)


class OSTUDIO_PT_mix(_Sub, bpy.types.Panel):
    bl_idname, bl_label, mode = 'OSTUDIO_PT_mix', 'Mix and match', 'outfit'

    def draw(self, context):
        ops_outfit.draw_mix(self.layout, context)


class OSTUDIO_PT_views(_Sub, bpy.types.Panel):
    bl_idname, bl_label, mode = 'OSTUDIO_PT_views', 'Views', 'outfit'
    bl_options = set()

    def draw(self, context):
        ops_outfit.draw_views(self.layout, context)


class OSTUDIO_PT_validate(_Sub, bpy.types.Panel):
    bl_idname, bl_label, mode, module = 'OSTUDIO_PT_validate', 'Validate', 'outfit', 'validate'


class OSTUDIO_PT_export(_Sub, bpy.types.Panel):
    bl_idname, bl_label, mode, module = 'OSTUDIO_PT_export', 'Export', 'outfit', 'export_items'


class OSTUDIO_PT_ap(_Sub, bpy.types.Panel):
    bl_idname, bl_label, mode, module = 'OSTUDIO_PT_ap', 'Attachment points', 'base', 'ap_ops'


class OSTUDIO_PT_spacing(_Sub, bpy.types.Panel):
    bl_idname, bl_label, mode, module = 'OSTUDIO_PT_spacing', 'Arm / leg spacing', 'base', 'spacing'


class OSTUDIO_PT_export_base(_Sub, bpy.types.Panel):
    bl_idname, bl_label, mode, module = 'OSTUDIO_PT_export_base', 'Export base', 'base', 'export_base'


class OSTUDIO_PT_anims(_Sub, bpy.types.Panel):
    bl_idname, bl_label, mode, module = 'OSTUDIO_PT_anims', 'Animations', 'base', 'anims'


CLASSES = (OSTUDIO_PT_main, OSTUDIO_PT_item, OSTUDIO_PT_import, OSTUDIO_PT_mix, OSTUDIO_PT_views, OSTUDIO_PT_validate,
           OSTUDIO_PT_export, OSTUDIO_PT_ap, OSTUDIO_PT_spacing, OSTUDIO_PT_export_base, OSTUDIO_PT_anims)
