"""Review views (SPEC section 4): one `apply_view(context, name)` per view plus `reset`.

Views never change item data. They hide/show objects, add a temporary colour attribute `os_view`
to item meshes (Solid shading, colour = Vertex), and create temp objects in the collection OS_View.
Everything needed to undo that is kept as JSON in `scene["ostudio_view"]`, so `reset` also works
after the file was saved and reopened. Head swap and thickness are persistent settings, not views."""
import colorsys
import json
import random

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

from .. import paths
from . import project, props, proxies

STATE_KEY = 'ostudio_view'
HEAD_MASK = 'OS_head_mask'
LABEL_SOURCES = ('texture', 'symmetry', 'prior', 'neighbours', 'off_body', 'riser', 'mirrored')
LABEL_COLOURS = {'texture': (0.2, 0.8, 0.25), 'symmetry': (0.2, 0.75, 0.9), 'mirrored': (0.2, 0.4, 0.95),
                 'prior': (0.95, 0.6, 0.15), 'neighbours': (0.95, 0.9, 0.2), 'off_body': (0.9, 0.15, 0.15),
                 'riser': (0.85, 0.2, 0.8)}
MODULE_OF = {'outfit': 'outfit', 'body_armor': 'body_armor', 'gauntlets': 'right_gauntlet_boot',
             'boots': 'right_gauntlet_boot'}


class ViewError(RuntimeError):
    pass


# ---------------------------------------------------------------- state
def _fresh():
    return {'view': 'NONE', 'hidden': {}, 'attrs': {}, 'wire': {}, 'shading': {}, 'scene': {}, 'info': '',
            'reg': proxies.new_reg()}


def load_state(scene):
    try:
        st = json.loads(scene.get(STATE_KEY, ''))
    except (TypeError, ValueError):
        return _fresh()
    return {**_fresh(), **st}


def _save(scene, st):
    scene[STATE_KEY] = json.dumps(st)


def info(scene):
    return load_state(scene).get('info', '')


def _is_hidden(o):
    try:
        return o.hide_get()
    except RuntimeError:
        return False


def _hide(st, o, hide):
    st['hidden'].setdefault(o.name, _is_hidden(o))
    try:
        o.hide_set(hide)
    except RuntimeError:
        pass


def _api():
    return props.items_api()


def _pieces(coll):
    return _api().item_pieces(coll)


def _all_item_objects(scene):
    top = bpy.data.collections.get('OS_Items')
    return list(top.all_objects) if top else []


def _vis(scene, st, show_items=(), body=True, rig=False):
    """Show exactly the pieces of `show_items` (+ body/rig); hide the rest of the item layers and the base."""
    r, b = project.find_base(scene)
    show = {o for c in show_items for o in _pieces(c)}
    if body and b:
        show.add(b)
    if rig and r:
        show.add(r)
    for o in _all_item_objects(scene) + [x for x in (r, b) if x]:
        _hide(st, o, o not in show)


def equipped(scene):
    """Collections shown by Game view: the mix-and-match selection (current item when nothing is picked)."""
    mix = [c for c in props.mix_collections(scene) if c]
    if not mix:
        cur = _api().current_item(bpy.context)
        return [cur] if cur else []
    types = {_api().item_data(c).get('type') for c in mix}
    if types & {'body_armor', 'gauntlets', 'boots'}:
        mix = [c for c in mix if _api().item_data(c).get('type') != 'outfit']
    return mix


def _current(context):
    c = _api().current_item(context)
    if c is None:
        raise ViewError('no current item; import or select one first')
    return c


# ---------------------------------------------------------------- viewport shading
def _spaces(context):
    scr = context.screen
    return [(i, s) for i, a in enumerate(scr.areas) if a.type == 'VIEW_3D' for s in a.spaces if s.type == 'VIEW_3D'] \
        if scr else []


def _shading(context, st, colour=None, **extra):
    """Switch the 3D views to Solid shading with `colour` as colour type (remembering the originals)."""
    for i, s in _spaces(context):
        sh = s.shading
        st['shading'].setdefault(str(i), {'type': sh.type, 'color_type': sh.color_type,
                                          'stats': s.overlay.show_stats, 'persp': s.region_3d.view_perspective})
        sh.type = 'SOLID'
        if colour:
            sh.color_type = colour
        if 'stats' in extra:
            s.overlay.show_stats = extra['stats']
        if 'camera' in extra:
            s.region_3d.view_perspective = 'CAMERA'


def _unshade(context, st):
    for i, s in _spaces(context):
        o = st['shading'].get(str(i))
        if o:
            s.shading.type, s.shading.color_type = o['type'], o['color_type']
            s.overlay.show_stats, s.region_3d.view_perspective = o['stats'], o['persp']
    st['shading'] = {}


# ---------------------------------------------------------------- colour attributes
def _face_ints(me, name):
    out = np.zeros(len(me.polygons), np.int64)
    a = me.attributes.get(name)
    if a is not None and a.domain == 'FACE':
        a.data.foreach_get('value', out)
    return out


def _paint(st, obj, rgba, per_corner=False):
    """Colour attribute `os_view` on obj's mesh: rgba is (faces, 4), or (corners, 4) with per_corner."""
    me = obj.data
    ca = me.color_attributes
    if me.name not in st['attrs']:
        st['attrs'][me.name] = ca.active_color_name or ''
    if 'os_view' in ca:
        ca.remove(ca['os_view'])
    a = ca.new('os_view', 'FLOAT_COLOR', 'CORNER')
    rgba = np.asarray(rgba, np.float32)
    if not per_corner:
        tot = np.empty(len(me.polygons), np.int32)
        me.polygons.foreach_get('loop_total', tot)
        rgba = np.repeat(rgba, tot, axis=0)
    a.data.foreach_set('color', np.ascontiguousarray(rgba).ravel())
    ca.active_color = a
    ca.default_color_name = 'os_view'


def _unpaint(st):
    for name, prev in st['attrs'].items():
        me = bpy.data.meshes.get(name)
        if me is None:
            continue
        ca = me.color_attributes
        if 'os_view' in ca:
            ca.remove(ca['os_view'])
        if prev and prev in ca:
            ca.active_color_name = ca.default_color_name = prev
    st['attrs'] = {}


def _set_legend(scene, entries):
    lg = scene.ostudio.legend
    lg.clear()
    for name, col in entries:
        it = lg.add()
        it.name, it.color = name, tuple(col) + (1.0,) * (4 - len(col))


def _hue(i, s=0.65, v=0.95):
    return props.srgb_to_linear(colorsys.hsv_to_rgb((i * 0.61803398875) % 1.0, s, v))


# ---------------------------------------------------------------- colour views
def _channel_roles(coll, me):
    """Per face: (role names list, channel index array)."""
    ch = _api().item_data(coll).get('channels', [])
    idx = _face_ints(me, 'os_channel')
    roles = np.array([c.get('role', 'uncertain') for c in ch] + ['uncertain'], dtype=object)
    return ch, idx, roles[np.where((idx >= 0) & (idx < len(ch)), idx, len(ch))]


def _colour_item(context, st, coll, fn):
    entries = {}
    for o in _pieces(coll):
        rgba, ent = fn(coll, o.data)
        _paint(st, o, rgba)
        entries.update(ent)
    _set_legend(context.scene, list(entries.items()))


def _roles(coll, me):
    cols = props.role_colours()
    _, _, role = _channel_roles(coll, me)
    return np.array([cols.get(r, cols['uncertain']) for r in role], np.float32).reshape(-1, 4), \
        {r: cols.get(r, cols['uncertain']) for r in set(role)}


def palette_tints(seed, roles=('primary', 'secondary', 'tertiary', 'accent', 'joint')):
    rng = random.Random(seed)
    return {r: props.srgb_to_linear(colorsys.hsv_to_rgb(rng.random(), rng.uniform(0.35, 0.9), rng.uniform(0.45, 0.95)))
            for r in roles}


def _palette(seed):
    tints = palette_tints(seed)
    cols = props.role_colours()

    def f(coll, me):
        _, _, role = _channel_roles(coll, me)
        rgba = np.array([tints.get(r, cols['uncertain']) for r in role], np.float32).reshape(-1, 4)
        return rgba, {f'{r} (seed {seed})': tints[r] for r in set(role) if r in tints}
    return f


def _channels(coll, me):
    ch, idx, _ = _channel_roles(coll, me)
    cols = np.array([_hue(i) for i in range(len(ch))] + [(0.5, 0.5, 0.5, 1.0)], np.float32)
    ent = {(c.get('label') or c.get('id', str(i))): _hue(i) for i, c in enumerate(ch) if i in set(idx.tolist())}
    return cols[np.where((idx >= 0) & (idx < len(ch)), idx, len(ch))], ent


def _confidence(coll, me):
    src = _face_ints(me, 'os_label_source')
    names = list(_api().item_data(coll).get('source', {}).get('label_source_names') or LABEL_SOURCES)
    cols = [props.srgb_to_linear(LABEL_COLOURS[n]) if n in LABEL_COLOURS else _hue(i) for i, n in enumerate(names)]
    cols = np.array(cols + [(0.5, 0.5, 0.5, 1.0)], np.float32)
    k = np.where((src >= 0) & (src < len(names)), src, len(names))
    return cols[k], {(names[i] if i < len(names) else 'unknown'): tuple(cols[i]) for i in sorted(set(k.tolist()))}


def _weights(context, st, coll):
    entries = {}
    for o in _pieces(coll):
        me = o.data
        names = [g.name for g in o.vertex_groups]
        dom = np.full(len(me.vertices), -1)
        for v in me.vertices:
            if v.groups:
                g = max(v.groups, key=lambda x: x.weight)
                dom[v.index] = g.group
        loops = np.empty(len(me.loops), np.int32)
        me.loops.foreach_get('vertex_index', loops)
        k = dom[loops]
        cols = np.array([_hue(i, 0.7, 0.9) for i in range(len(names))] + [(0.4, 0.4, 0.4, 1.0)], np.float32)
        _paint(st, o, cols[np.where(k >= 0, k, len(names))], per_corner=True)
        entries.update({names[i]: tuple(cols[i]) for i in sorted(set(k[k >= 0].tolist())) if i < len(names)})
    _set_legend(context.scene, list(entries.items())[:24])


# ---------------------------------------------------------------- measurements
def _world_verts(obj):
    n = len(obj.data.vertices)
    co = np.empty(n * 3, np.float32)
    obj.data.vertices.foreach_get('co', co)
    m = np.array(obj.matrix_world)
    return co.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3]


def body_frame(base):
    """(centre, scale, height): REST bbox centre of the base in world space, 0.8 / largest dimension (marker frame)."""
    v = _world_verts(base)
    lo, hi = v.min(0), v.max(0)
    return (lo + hi) / 2, 0.8 / float((hi - lo).max()), float(hi[2] - lo[2])


def _eval_world(obj, dg):
    """(world verts, triangles, world vertex normals) of the evaluated mesh of obj."""
    ev = obj.evaluated_get(dg)
    me = ev.to_mesh()
    n = len(me.vertices)
    co, nr = np.empty(n * 3, np.float32), np.empty(n * 3, np.float32)
    me.vertices.foreach_get('co', co)
    me.vertex_normals.foreach_get('vector', nr)
    me.calc_loop_triangles()
    t = np.empty(len(me.loop_triangles) * 3, np.int32)
    me.loop_triangles.foreach_get('vertices', t)
    ev.to_mesh_clear()
    m = np.array(obj.matrix_world)
    nw = nr.reshape(-1, 3) @ m[:3, :3].T
    return co.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3], t.reshape(-1, 3), nw / np.maximum(np.linalg.norm(nw, axis=1), 1e-9)[:, None]


def _bvh(objs, dg):
    V, T, off = [], [], 0
    for o in objs:
        v, t, _ = _eval_world(o, dg)
        V.append(v)
        T.append(t + off)
        off += len(v)
    if not V:
        return None
    return BVHTree.FromPolygons([tuple(x) for x in np.concatenate(V)], [tuple(x) for x in np.concatenate(T)], all_triangles=True)


def _poke_hits(base, bvh, dg, reach):
    """Boolean mask of evaluated base vertices that stick out through the item surface.

    Along the vertex normal: a vertex is under the garment when the outward ray leaves through a surface seen
    from inside; it pokes when no such surface is above it but one lies within `reach` below it, facing out."""
    v, _, nrm = _eval_world(base, dg)
    out = np.zeros(len(v), bool)
    for i, (p, n) in enumerate(zip(v, nrm)):
        p, n = Vector(p), Vector(n)
        up, un, _, _ = bvh.ray_cast(p, n, reach)
        if up is not None and un.dot(n) > 0:
            continue                               # the normal leaves through the garment from inside
        dn, dnn, _, _ = bvh.ray_cast(p, -n, reach)
        out[i] = dn is not None and dnn.dot(n) > 0  # an outward-facing garment surface lies below the vertex
    return out


def _markers(reg, scene, name, points, radius, mat):
    bm = bmesh.new()
    for p in points:
        bmesh.ops.create_icosphere(bm, subdivisions=1, radius=radius, matrix=Matrix.Translation(Vector(p)))
    return proxies.new_obj(reg, scene, name, proxies.new_mesh(reg, name, bm, mat))


def _v_poke(context, st):
    scene, o = context.scene, context.scene.ostudio
    cur = _current(context)
    rig, base = project.find_base(scene)
    _vis(scene, st, [cur])
    pieces = _pieces(cur)
    if not pieces:
        raise ViewError('current item has no pieces')
    frames = [scene.frame_current]
    if o.poke_all_frames and not (rig.animation_data and rig.animation_data.action) and o.pose_action != 'NONE':
        apply_action(context, st, o.pose_action)       # scan the action chosen in the pose-test dropdown
    ad = rig.animation_data
    if o.poke_all_frames and ad and ad.action:
        a, b = (int(x) for x in ad.action.frame_range)
        frames = list(range(a, b + 1, max(1, (b - a) // 24)))
    f0, hits = scene.frame_current, None
    for f in frames:
        if len(frames) > 1:
            scene.frame_set(f)
        dg = context.evaluated_depsgraph_get()
        h = _poke_hits(base, _bvh(pieces, dg), dg, o.poke_max)
        hits = h if hits is None or len(h) != len(hits) else hits | h
    scene.frame_set(f0)
    dg = context.evaluated_depsgraph_get()
    v = _eval_world(base, dg)[0]
    n = int(hits.sum())
    if n:
        mat = proxies.new_mat(st['reg'], 'OSV_poke', (1, 0.05, 0.05, 1))
        _markers(st['reg'], scene, 'OSV_poke', v[hits], 0.008 * body_frame(base)[2] / 1.75, mat)
    st['info'] = f'poke-through: {n} base vertices outside {_api().item_data(cur).get("item_id", cur.name)}' \
                 f' ({len(frames)} frame{"s" * (len(frames) > 1)})'
    _set_legend(scene, [(st['info'], (1, 0.05, 0.05, 1))])


# ---------------------------------------------------------------- other views
def apply_action(context, st, key):
    """Assign an action (name, or LIB:<name> from the base project) to the rig, remembering the original."""
    scene = context.scene
    rig, _ = project.find_base(scene)
    ad = rig.animation_data or rig.animation_data_create()
    sc = st['scene']
    sc.setdefault('action', ad.action.name if ad.action else '')
    sc.setdefault('frame', scene.frame_current)
    sc.setdefault('range', [scene.frame_start, scene.frame_end])
    if key in ('', 'NONE'):
        return
    if key.startswith('LIB:'):
        nm = key[4:]
        if nm not in bpy.data.actions:
            with bpy.data.libraries.load(str(paths.base_project(project.base_name(scene))), link=True, relative=True) as (a, b):
                b.actions = [nm] if nm in a.actions else []
        key = nm
    act = bpy.data.actions.get(key)
    if act is None:
        raise ViewError(f'action {key!r} not found')
    ad.action = act
    if getattr(ad, 'action_slot', 1) is None:
        suit = list(ad.action_suitable_slots)
        if suit:
            ad.action_slot = suit[0]
    scene.frame_start, scene.frame_end = (int(x) for x in act.frame_range)
    scene.frame_set(scene.frame_start)


def _restore_scene(context, st):
    scene, sc = context.scene, st['scene']
    rig, _ = project.find_base(scene)
    if 'action' in sc and rig and rig.animation_data:
        rig.animation_data.action = bpy.data.actions.get(sc['action']) if sc['action'] else None
    if 'range' in sc:
        scene.frame_start, scene.frame_end = sc['range']
    if 'frame' in sc:
        scene.frame_set(sc['frame'])
    if 'camera' in sc:
        scene.camera = bpy.data.objects.get(sc['camera'])
    if 'res' in sc:
        scene.render.resolution_x, scene.render.resolution_y = sc['res']
    st['scene'] = {}


def _v_ap(context, st):
    scene = context.scene
    rig, base = project.find_base(scene)
    eq = equipped(scene)
    _vis(scene, st, eq)
    dg = context.evaluated_depsgraph_get()
    pieces = [o for c in eq for o in _pieces(c)]
    bvh = _bvh(pieces, dg)
    ev = rig.evaluated_get(dg)
    unit = body_frame(base)[2] / 1.75
    fams = sorted({b.name.split('_')[1].rstrip('LR') or b.name for b in rig.data.bones if b.name.startswith('AP_')})
    mats = {f: proxies.new_mat(st['reg'], f'OSV_ap_{f}', _hue(i, 0.6, 0.9)) for i, f in enumerate(fams)}
    red = proxies.new_mat(st['reg'], 'OSV_ap_buried', (1, 0.0, 0.0, 1))
    meshes = {}
    for f, m in list(mats.items()) + [('buried', red)]:
        bm = bmesh.new()
        bmesh.ops.create_icosphere(bm, subdivisions=1, radius=0.007 * unit)
        meshes[f] = proxies.new_mesh(st['reg'], f'OSV_ap_{f}', bm, m)
    buried = n = 0
    for b in rig.data.bones:
        if not b.name.startswith('AP_'):
            continue
        pm = ev.matrix_world @ ev.pose.bones[b.name].matrix
        head, d = pm.translation, (pm.to_3x3() @ Vector((0, 1, 0))).normalized()
        hit = bvh is not None and _ray_parity(bvh, head, d, 0.3 * unit) % 2 == 1
        buried, n = buried + hit, n + 1
        fam = b.name.split('_')[1].rstrip('LR') or b.name
        o = proxies.new_obj(st['reg'], scene, 'OSAP_' + b.name, meshes['buried' if hit else fam])
        proxies.parent_to_bone(o, rig, b.name, Matrix.Translation(head), pose_matrix=pm)
    st['info'] = f'AP points: {n} markers, {buried} buried under {len(eq)} equipped item(s)'
    _set_legend(scene, [(st['info'], (1, 0, 0, 1))] + [(f, tuple(_hue(i, 0.6, 0.9))) for i, f in enumerate(fams)])


def _ray_parity(bvh, origin, d, dist):
    """Number of surface crossings from `origin` along `d` within `dist`."""
    n, o = 0, Vector(origin) + d * 1e-4
    while n < 8:
        loc, _, _, _ = bvh.ray_cast(o, d, dist)
        if loc is None:
            break
        n, dist, o = n + 1, dist - (loc - o).length - 1e-5, loc + d * 1e-5
        if dist <= 0:
            break
    return n


def _v_sockets(context, st):
    scene = context.scene
    rig, base = project.find_base(scene)
    _vis(scene, st, equipped(scene), rig=True)
    made = proxies.build(context, st['reg'], unit=body_frame(base)[2] / 1.75)
    st['info'] = f'{len(made)} socket proxies'


def concept_image(context, coll, view):
    d = _api().item_data(coll)
    src = d.get('source', {})
    race = src.get('race') or project.base_name(context.scene)
    module = src.get('module') or MODULE_OF.get(d.get('type'), 'outfit')
    png = paths.view_image(src.get('source_set') or d.get('set_id', ''), src.get('job_id', ''), race, module, view)
    if png is None:
        raise ViewError(f'no {view}.png for {d.get("set_id")}--{src.get("job_id")}/{race}/{module} in '
                        + ', '.join(paths.VIEW_STAGES))
    return png


def _v_concept(context, st):
    scene = context.scene
    cur = _current(context)
    rig, base = project.find_base(scene)
    view = scene.ostudio.concept_view
    png = concept_image(context, cur, view)
    tf = json.loads((paths.base_views(project.base_name(scene)) / 'transforms.json').read_text())
    fr = next((f for f in tf['frames'] if f['name'] == view), None)
    if fr is None:
        raise ViewError(f'transforms.json has no frame {view}')
    centre, scale, _ = body_frame(base)                  # marker frame -> base world: p_w = centre + p_m / scale
    T = Matrix(fr['transform_matrix'])
    cam = bpy.data.cameras.new('OSV_concept')
    st['reg']['data'].append(('cameras', cam.name))
    cam.sensor_fit, cam.angle, cam.clip_start, cam.clip_end = 'HORIZONTAL', tf['camera_angle_x'], 0.05, 200.0
    img = bpy.data.images.load(str(png), check_existing=True)
    cam.show_background_images = True
    bg = cam.background_images.new()
    bg.image, bg.alpha, bg.display_depth = img, 0.6, 'FRONT'
    pos = Vector(centre) + T.translation / scale
    obj = proxies.new_obj(st['reg'], scene, 'OSV_concept_cam', cam, Matrix.Translation(pos) @ T.to_3x3().to_4x4())
    st['scene'].setdefault('camera', scene.camera.name if scene.camera else '')
    st['scene'].setdefault('res', [scene.render.resolution_x, scene.render.resolution_y])
    scene.camera = obj
    scene.render.resolution_x, scene.render.resolution_y = img.size[0] or 1254, img.size[1] or 1254
    _vis(scene, st, [cur])
    _shading(context, st, camera=True)
    st['info'] = f'concept {view}: {png.relative_to(paths.ROOT)}'


def _v_source(context, st):
    scene = context.scene
    cur = _current(context)
    _vis(scene, st, [cur])
    imp = props.lazy('importer')
    fn = next((getattr(imp, n) for n in ('import_source', 'import_source_highpoly') if imp and hasattr(imp, n)), None)
    if fn is None:
        raise ViewError('importer.import_source is not available (importer module missing or without a source helper)')
    before = set(bpy.data.objects.keys())
    fn(context, cur)
    new = [n for n in bpy.data.objects.keys() if n not in before]
    st['reg']['objs'] += new
    st['info'] = f'source high-poly: {len(new)} object(s)'


def _v_wire(context, st):
    scene = context.scene
    cur = _current(context)
    _vis(scene, st, [cur])
    lines = []
    for o in _pieces(cur):
        st['wire'].setdefault(o.name, o.show_wire)
        o.show_wire = True
        me = o.data
        me.calc_loop_triangles()
        lines.append((f'{o.name.split("__")[-1]}: {len(me.loop_triangles)} tris, {len(me.vertices)} verts', (1, 1, 1, 1)))
    _shading(context, st, stats=True)
    st['info'] = f'{sum(int(x[0].split(": ")[1].split(" ")[0]) for x in lines)} triangles in {len(lines)} piece(s)'
    _set_legend(scene, lines)


def _v_pose(context, st):
    scene = context.scene
    _vis(scene, st, equipped(scene), rig=True)
    apply_action(context, st, scene.ostudio.pose_action)
    st['info'] = f'pose test: {scene.ostudio.pose_action}'


_COLOURED = {'ROLES': _roles, 'CHANNELS': _channels, 'CONFIDENCE': _confidence}


def _dispatch(context, st, name):
    scene = context.scene
    if name == 'GAME':
        _vis(scene, st, equipped(scene))
        _shading(context, st, 'MATERIAL')
    elif name == 'ITEM':
        _vis(scene, st, [_current(context)], body=False)
    elif name in _COLOURED or name == 'PALETTE':
        cur = _current(context)
        _vis(scene, st, [cur])
        _shading(context, st, 'VERTEX')
        _colour_item(context, st, cur, _palette(scene.ostudio.palette_seed) if name == 'PALETTE' else _COLOURED[name])
    elif name == 'WEIGHTS':
        cur = _current(context)
        _vis(scene, st, [cur])
        _shading(context, st, 'VERTEX')
        _weights(context, st, cur)
    elif name == 'POKE':
        _shading(context, st, 'MATERIAL')
        _v_poke(context, st)
    elif name == 'POSE':
        _v_pose(context, st)
    elif name == 'SOCKETS':
        _shading(context, st, 'MATERIAL')
        _v_sockets(context, st)
    elif name == 'AP':
        _shading(context, st, 'MATERIAL')
        _v_ap(context, st)
    elif name in ('HEAD', 'THICKNESS'):
        _vis(scene, st, equipped(scene))
        _shading(context, st, 'MATERIAL')
    elif name == 'CONCEPT':
        _v_concept(context, st)
    elif name == 'SOURCE':
        _v_source(context, st)
    elif name == 'WIRE':
        _v_wire(context, st)
    else:
        raise ViewError(f'unknown view {name!r}')


def apply_view(context, name):
    """Switch to view `name` (previous view is undone first). Raises ViewError when it cannot be shown."""
    reset(context)
    if name == 'NONE':
        return
    scene = context.scene
    st = _fresh()
    try:
        _dispatch(context, st, name)
    except Exception:
        _save(scene, st)
        reset(context)
        raise
    st['view'] = scene.ostudio.view = name
    _save(scene, st)


def refresh(context):
    v = context.scene.ostudio.view
    if v != 'NONE':
        apply_view(context, v)


def reset(context):
    """Undo the active view: visibility, colour attributes, shading, temp objects/data, action and camera."""
    scene = context.scene
    st = load_state(scene)
    for n, was in st['hidden'].items():
        o = bpy.data.objects.get(n)
        if o:
            try:
                o.hide_set(was)
            except RuntimeError:
                pass
    for n, was in st['wire'].items():
        o = bpy.data.objects.get(n)
        if o:
            o.show_wire = was
    _unpaint(st)
    _unshade(context, st)
    _restore_scene(context, st)
    for n in st['reg']['objs']:
        o = bpy.data.objects.get(n)
        if o:
            bpy.data.objects.remove(o)
    for kind, n in st['reg']['data']:
        d = getattr(bpy.data, kind).get(n)
        if d:
            getattr(bpy.data, kind).remove(d)
    c = bpy.data.collections.get(proxies.VIEW_COLLECTION)
    if c and not c.objects:
        bpy.data.collections.remove(c)
    scene.ostudio.legend.clear()
    scene.ostudio.view = 'NONE'
    scene[STATE_KEY] = json.dumps(_fresh())


# ---------------------------------------------------------------- thickness / head swap (persistent settings)
def set_thickness(scene, t):
    """Set every bulk_* shape key of the base and of all item pieces to t. Returns the number of keys set."""
    n = 0
    _, base = project.find_base(scene)
    for o in ([base] if base else []) + [x for x in _all_item_objects(scene) if x.type == 'MESH']:
        keys = o.data.shape_keys
        for kb in keys.key_blocks if keys else ():
            if kb.name.startswith('bulk_'):
                kb.value, n = t, n + 1
    return n


def head_choices(base):
    d = paths.HEADS / base
    return sorted(p.stem for p in d.glob('h_*.glb'))


def _remove_head(scene):
    rig, base = project.find_base(scene)
    for o in [o for o in bpy.data.objects if 'os_head' in o]:
        for c in list(o.children_recursive):
            bpy.data.objects.remove(c)
        bpy.data.objects.remove(o)
    m = base.modifiers.get(HEAD_MASK) if base else None
    if m:
        base.modifiers.remove(m)


def head_swap(context, choice):
    """Seat library head `choice` (file stem, or NONE) on the head bone; hide the base head with a Mask modifier."""
    scene = context.scene
    rig, base = project.find_base(scene)
    bname = project.base_name(scene)
    _remove_head(scene)
    if choice in ('', 'NONE'):
        return None
    glb = paths.HEADS / bname / f'{choice}.glb'
    if not glb.exists():
        raise ViewError(f'{glb} not found')
    bases = json.loads((paths.HEADS / 'bases.json').read_text())['head_frames']
    ref = json.loads(glb.with_suffix('.json').read_text()).get('reference_base', bname)
    s = bases[bname]['head_size'] / bases[ref]['head_size']
    before = set(bpy.data.objects.keys())
    bpy.ops.import_scene.gltf(filepath=str(glb))
    new = [bpy.data.objects[n] for n in bpy.data.objects.keys() if n not in before]
    root = bpy.data.objects.new('OSHEAD_' + choice, None)
    root['os_head'] = choice
    scene.collection.objects.link(root)
    for o in new:
        if o.parent is None:
            o.parent = root
    hb = rig.data.bones['head.x']
    proxies.parent_to_bone(root, rig, 'head.x', Matrix.Translation(rig.matrix_world @ hb.head_local) @ Matrix.Scale(s, 4))
    m = base.modifiers.new(HEAD_MASK, 'MASK')
    m.mode, m.vertex_group, m.invert_vertex_group = 'VERTEX_GROUP', 'head.x', True
    for o in new:
        if o.type == 'ARMATURE':
            o.hide_set(True)
    return root


CLASSES = ()
