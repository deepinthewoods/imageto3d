"""Validation of outfit items before export (SPEC section 7, Validation).

`validate(context, items=None)` returns a list of `{level, item_id, piece, code, message, verts?, faces?}`
(`level` is `error` or `warning`; `verts` / `faces` are indices into the piece mesh for the panel's select button).
Errors block an export; warnings are things the export repairs or that need a look. It only reads the session.
Siblings (the other bases' outfit projects next to the open file) are peeked at by linking their `OS_*` item
collections for a moment, to find items missing on some bases and channel ids that differ between bases."""
from pathlib import Path

import bpy
import numpy as np
from bpy.props import EnumProperty, IntProperty

from .. import paths
from ..core import morph_transfer as mt
from ..core import pipeline_source as ps
from . import importer, items as it, morphs, project, weights

TRI_BUDGET = 2000           # triangles per item (warning above)
SYMMETRY_TOL = 1e-3         # m: gauntlet / boot pairs must mirror within 1 mm
LIMIT = 20000               # indices kept per entry
EPS = 1e-8
LAST = []                   # results of the last `ostudio.validate`, drawn by the panel
PAIRED = ('gauntlets', 'boots')


def entry(level, item_id, piece, code, message, verts=None, faces=None):
    d = {'level': level, 'item_id': item_id, 'piece': piece, 'code': code, 'message': message}
    if verts is not None and len(verts):
        d['verts'] = [int(i) for i in verts][:LIMIT]
    if faces is not None and len(faces):
        d['faces'] = [int(i) for i in faces][:LIMIT]
    return d


def summary(results):
    return {'errors': sum(r['level'] == 'error' for r in results), 'warnings': sum(r['level'] == 'warning' for r in results)}


# ------------------------------------------------------------------------------------------------- siblings

def sibling_items(directory=None):
    """{base: {item_id: [channel ids]}} of every other `*_outfits.blend` in `directory` (default: next to the open file).
    Item collections are linked for a moment and every library added by that is removed again."""
    here = Path(bpy.data.filepath).resolve() if bpy.data.filepath else None
    directory = Path(directory) if directory else (here.parent if here else paths.OUTFIT_PROJECTS)
    out = {}
    for p in sorted(directory.glob('*_outfits.blend')):
        if here is not None and p.resolve() == here:
            continue
        before = set(bpy.data.libraries)
        found = {}
        try:
            with bpy.data.libraries.load(str(p), link=True) as (src, dst):
                dst.collections = [n for n in src.collections if n.startswith('OS_') and n != it.ROOT_NAME and 'os_reimport' not in n
                                   and not n.endswith('__reimport')]
            for c in dst.collections:
                if c is not None and 'os_item' in c:
                    d = it.item_data(c)
                    found[d.get('item_id', c.name)] = [ch['id'] for ch in d.get('channels', [])]
        except (OSError, RuntimeError, ValueError) as e:
            print(f'[validate] cannot read {p.name}: {e}')
        finally:
            for lib in [x for x in bpy.data.libraries if x not in before]:
                bpy.data.libraries.remove(lib)
        out[p.stem.removesuffix('_outfits')] = found
    return out


# ------------------------------------------------------------------------------------------------- piece checks

def _hausdorff(A, B, chunk=512):
    """Per-point distance of A's rows to the nearest row of B (brute force; pieces have a few hundred vertices)."""
    out = np.empty(len(A))
    for i in range(0, len(A), chunk):
        out[i:i + chunk] = np.sqrt(((A[i:i + chunk, None] - B[None]) ** 2).sum(-1).min(1)) if len(B) else np.inf
    return out


def _channel_values(obj):
    attr = obj.data.attributes.get('os_channel')
    if attr is None:
        return None
    v = np.empty(len(obj.data.polygons), np.int32)
    attr.data.foreach_get('value', v)
    return v


def _animated(obj):
    """Names of the datablocks of a piece that carry animation data (action, drivers or NLA tracks)."""
    found = []
    for label, idb in (('object', obj), ('mesh', obj.data), ('shape keys', obj.data.shape_keys)):
        ad = getattr(idb, 'animation_data', None) if idb is not None else None
        if ad is not None and (ad.action or ad.drivers or len(ad.nla_tracks)):
            found.append(label)
    return found


def check_piece(out, item_id, item_type, obj, channels, ctx):
    me, piece = obj.data, obj.get('os_piece', obj.name)
    add = lambda level, code, msg, verts=None, faces=None: out.append(entry(level, item_id, piece, code, msg, verts, faces))
    nv, nf = len(me.vertices), len(me.polygons)
    if nf == 0 or nv == 0:
        add('error', 'empty_piece', 'piece has no faces')
        return
    if not any(m.type == 'ARMATURE' and m.object == ctx['rig'] for m in obj.modifiers):
        add('error', 'no_armature', 'no Armature modifier bound to the rig')
    anim = _animated(obj)
    if anim:
        add('error', 'animation_data', 'animation data on ' + ', '.join(anim))
    # channels
    ch = _channel_values(obj)
    if ch is None:
        add('error', 'channel_range', 'face attribute os_channel is missing')
    else:
        bad = np.flatnonzero((ch < 0) | (ch >= len(channels)))
        if len(bad):
            add('error', 'channel_range', f'{len(bad)} faces have an os_channel outside 0..{len(channels) - 1}', faces=bad)
    # weights
    names, W = weights.vertex_weights(obj)
    isdef = np.array([n in ctx['deform'] for n in names], bool)
    for j in np.flatnonzero(~isdef):
        used = np.flatnonzero(W[:, j] > 0)
        add('error', 'group_not_deform', f'vertex group {names[j]!r} is not a deform bone of the rig', verts=used)
    Wd = W[:, isdef] * (W[:, isdef] > EPS)
    total, count = Wd.sum(1), (Wd > 0).sum(1)
    none = np.flatnonzero(total <= EPS)
    if len(none):
        add('error', 'unweighted', f'{len(none)} vertices have no deform weight (not repairable)', verts=none)
    over = np.flatnonzero(count > 4)
    if len(over):
        add('warning', 'influences', f'{len(over)} vertices have more than 4 influences (max {int(count.max())}); export keeps the top 4',
            verts=over)
    off = np.flatnonzero((total > EPS) & (np.abs(total - 1) > 1e-5))
    if len(off):
        add('warning', 'weight_sum', f'{len(off)} vertices have a weight sum other than 1 (e.g. {total[off[0]]:.4f}); export normalises', verts=off)
    # rigid binding
    table = weights.rigid_bones(piece) if item_type != 'outfit' else None
    if table:
        dnames = [n for n, d in zip(names, isdef) if d]
        want = np.zeros((nv, len(dnames)))
        for b, w in table.items():
            if b in dnames:
                want[:, dnames.index(b)] = w
        wrong = np.flatnonzero((np.abs(Wd - want) > 1e-3).any(1)) if dnames else np.arange(nv)
        miss = [b for b in table if b not in dnames]
        if miss or len(wrong):
            add('error', 'rigid_binding', f'rigid piece not bound to {table}' + (f' (no group {miss})' if miss else '')
                + (f'; {len(wrong)} vertices differ' if len(wrong) else ''), verts=wrong)
    # shape keys: neutral values, every base key that moves this piece present
    keys = me.shape_keys.key_blocks if me.shape_keys else []
    have = {k.name for k in keys[1:]} if keys else set()
    loud = [k.name for k in keys[1:] if abs(k.value) > 1e-6]
    if loud:
        add('warning', 'morph_not_neutral', f'shape key values are not 0 ({loud[:3]}...); export zeroes them')
    if ctx['deltas'] is not None:
        tri, bary, _ = mt.closest_points(importer.piece_vertices(obj), ctx['rest']['V'], ctx['rest']['F'])
        expected = set(mt.transfer_deltas(tri, bary, ctx['rest']['F'], ctx['deltas']))
        gone = sorted(expected - have)
        if gone:
            add('error', 'missing_morph', f'base shape keys missing on the piece: {gone}')


def check_pairs(out, item_id, pieces, plane):
    """Gauntlet / boot pairs: the left piece must be the mirror image of the right one about the sagittal plane."""
    by = {o.get('os_piece', o.name): o for o in pieces}
    for name, right in by.items():
        if not name.endswith('_r'):
            continue
        left = by.get(ps.swap_side(name))
        if left is None:
            out.append(entry('error', item_id, name, 'asymmetric', f'no mirrored counterpart {ps.swap_side(name)}'))
            continue
        R, L = importer.piece_vertices(right), importer.piece_vertices(left)
        R2 = R * np.array([-1., 1., 1.]) + np.array([2 * plane, 0., 0.])
        dl, dr = _hausdorff(L, R2), _hausdorff(R2, L)
        for obj, d, tag in ((left, dl, 'left'), (right, dr, 'right')):
            bad = np.flatnonzero(d > SYMMETRY_TOL)
            if len(bad):
                out.append(entry('error', item_id, obj.get('os_piece', obj.name), 'asymmetric',
                                 f'{tag} piece differs from the mirrored {name if tag == "left" else ps.swap_side(name)} '
                                 f'by up to {d.max() * 1000:.1f} mm ({len(bad)} vertices over {SYMMETRY_TOL * 1000:.0f} mm)', verts=bad))


# ------------------------------------------------------------------------------------------------- items

def context_data(context):
    """Everything shared by the checks: rig, deform bones, fingerprints, rest mesh, base morph deltas."""
    rig, base = importer.base_objects(context)
    rest = importer.rest_context(rig, base)
    deltas = morphs.base_deltas(base)
    return {'rig': rig, 'base': base, 'rest': rest, 'deform': weights.deform_bones(rig), 'deltas': deltas or None,
            'rig_fp': project.rig_fingerprint(rig), 'base_fp': project.base_fingerprint(base, rig),
            'bulk': sorted(k.name for k in base.data.shape_keys.key_blocks[1:] if k.name.startswith('bulk_'))
            if base.data.shape_keys else []}


def validate(context, items=None, budget=None, siblings=True, sibling_dir=None):
    """Check `items` (item ids, collections, or None = all) of the open outfit project. -> list of entries."""
    scene = context.scene
    budget = budget or project.meta(scene).get('tri_budget') or TRI_BUDGET
    colls = it.list_items(scene)
    if items is not None:
        want = {getattr(i, 'name', i) for i in items} | {it.item_data(i).get('item_id') for i in items if hasattr(i, 'name')}
        colls = [c for c in colls if c.name in want or it.item_data(c).get('item_id') in want]
    out = []
    if not colls:
        return out
    ctx = context_data(context)
    base_name = project.base_name(scene)
    sib = sibling_items(sibling_dir) if siblings else {}
    roles = {r for r in it.ROLE_COLOURS if r not in ('base', 'uncertain')}
    for coll in colls:
        d = it.item_data(coll)
        item_id, item_type, channels = d.get('item_id', coll.name), d.get('type', ''), d.get('channels', [])
        add = lambda level, code, msg, piece='': out.append(entry(level, item_id, piece, code, msg))
        pieces = it.item_pieces(coll)
        if not pieces:
            add('error', 'empty_item', 'item has no pieces')
        ids = [c['id'] for c in channels]
        if len(set(ids)) != len(ids):
            add('error', 'duplicate_channel', 'channel ids are not unique')
        for c in channels:
            if c.get('role') not in roles:
                add('error', 'bad_role', f"channel {c['id']!r} has role {c.get('role')!r}, not one of {sorted(roles)}")
        stale = it.check_item(coll, ctx['rig_fp'], ctx['base_fp'])
        if stale:
            add('error', 'stale_rig', 'item was built on a different ' + ' and '.join(stale) + ' (re-bind it)')
        for obj in pieces:
            check_piece(out, item_id, item_type, obj, channels, ctx)
        if item_type in PAIRED and pieces:
            check_pairs(out, item_id, pieces, ctx['rest']['plane_x'])
        tris = sum(len(o.data.polygons) for o in pieces)
        if tris > budget:
            add('warning', 'tris_over_budget', f'{tris} triangles, budget {budget}')
        if d.get('status') not in ('approved', 'exported'):
            add('warning', 'status', f"status is {d.get('status')!r}, not approved")
        if not siblings:
            continue
        have = [b for b, found in sib.items() if item_id in found]
        for b, found in sib.items():
            if item_id in found and sorted(found[item_id]) != sorted(ids):
                add('warning', 'channels_differ', f'channel ids differ on {b}: {sorted(set(found[item_id]) ^ set(ids))}')
        want_on = {e['race'] for e in ps.discover() if e['set_id'] == d.get('set_id') and item_type in ps.MODULE_ITEMS.get(e['module'], ())}
        missing = sorted((want_on | set(have)) - set(have) - {base_name})
        if missing:
            add('warning', 'missing_on_bases', 'item has no layer on: ' + ', '.join(missing))
    return out


# ------------------------------------------------------------------------------------------------- select / operators

def select_offending(obj, verts=None, faces=None):
    """Make `obj` the active, selected object and select the given vertices / faces in its edit mode."""
    if bpy.context.object is not None and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    obj.hide_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    me = obj.data
    nv, nf = len(me.vertices), len(me.polygons)
    sv, sf = np.zeros(nv, bool), np.zeros(nf, bool)
    if faces:
        sf[np.asarray(faces, np.int64)] = True
        for i in np.flatnonzero(sf):
            sv[list(me.polygons[int(i)].vertices)] = True
    if verts:
        sv[np.asarray(verts, np.int64)] = True
    me.vertices.foreach_set('select', sv)
    me.edges.foreach_set('select', np.zeros(len(me.edges), bool))
    me.polygons.foreach_set('select', sf)
    me.update()
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_mode(type='FACE' if faces and not verts else 'VERT')


class OSTUDIO_OT_validate(bpy.types.Operator):
    bl_idname = 'ostudio.validate'
    bl_label = 'Validate'
    bl_description = 'Check the items against the export rules (weights, channels, rigid binding, morphs, symmetry, budget)'
    scope: EnumProperty(name='Scope', items=[('ALL', 'All items', ''), ('CURRENT', 'Current item', '')], default='ALL')

    @classmethod
    def poll(cls, context):
        return project.mode() == 'outfit'

    def execute(self, context):
        coll = it.current_item(context) if self.scope == 'CURRENT' else None
        if self.scope == 'CURRENT' and coll is None:
            self.report({'WARNING'}, 'no current item')
            return {'CANCELLED'}
        try:
            LAST[:] = validate(context, [coll] if coll else None)
        except Exception as e:
            self.report({'ERROR'}, f'validation failed: {e}')
            return {'CANCELLED'}
        s = summary(LAST)
        self.report({'ERROR' if s['errors'] else 'WARNING' if s['warnings'] else 'INFO'},
                    f"{s['errors']} errors, {s['warnings']} warnings")
        return {'FINISHED'}


class OSTUDIO_OT_validate_select(bpy.types.Operator):
    bl_idname = 'ostudio.validate_select'
    bl_label = 'Select'
    bl_description = 'Select the offending faces / vertices of this validation entry'
    index: IntProperty()

    def execute(self, context):
        if not 0 <= self.index < len(LAST):
            return {'CANCELLED'}
        r = LAST[self.index]
        coll = it.find_item(context.scene, r['item_id'])
        obj = next((o for o in it.item_pieces(coll) if o.get('os_piece', o.name) == r['piece']), None) if coll else None
        if obj is None:
            self.report({'WARNING'}, 'piece not found')
            return {'CANCELLED'}
        select_offending(obj, r.get('verts'), r.get('faces'))
        return {'FINISHED'}


CLASSES = (OSTUDIO_OT_validate, OSTUDIO_OT_validate_select)


def draw(layout, ctx):
    box = layout.box()
    box.label(text='Validate')
    row = box.row(align=True)
    row.operator('ostudio.validate', text='All').scope = 'ALL'
    row.operator('ostudio.validate', text='Current').scope = 'CURRENT'
    if not LAST:
        return
    s = summary(LAST)
    box.label(text=f"{s['errors']} errors, {s['warnings']} warnings", icon='ERROR' if s['errors'] else 'INFO')
    for i, r in enumerate(LAST[:40]):
        row = box.row(align=True)
        row.label(text=f"{r['item_id'].split('__')[-1]}/{r['piece']}: {r['message']}"[:90] if r['piece'] else r['message'][:90],
                  icon='CANCEL' if r['level'] == 'error' else 'ERROR')
        if r.get('verts') or r.get('faces'):
            row.operator('ostudio.validate_select', text='Select').index = i
    if len(LAST) > 40:
        box.label(text=f'... and {len(LAST) - 40} more')
