"""Head export for the game standard v2: the race head libraries re-targeted to the new base, with a check sheet.

The head libraries (`items/heads/<race>/<race>_heads.blend`, built / seated with `scripts/heads`) store every head's seat as its object
transform relative to a `slot:` empty, in the Head-bone frame of the race's OLD game base (`REF_default_head_<race>` is that base's
default head in the same frame). The head GLBs bake the seat; the game puts the GLB root on the body's Head bone.

For the new base (`bases/<race>/base.json`, written by export_base.py) this module
1. measures the new default head (Head-bone frame) against the old `REF_default_head`: equal within 1 mm = the seats carry over as they are
   (transform = identity), else (human_male) the similarity M = T . Rx(pitch) . S(scale) that maps the OLD default head onto the NEW one
   (`head_fit.fit`: silhouette overlap over scale, pitch and translation) is applied to every seat, so each head keeps the placement relative
   to the default head that was tuned by hand. The plain Head-bone offset (`--shift bone`) and a translation-only silhouette match
   (`--shift mesh`) stay available and are reported next to it;
2. exports every head GLB + json with `scripts/heads/build_race_heads.export_race` (the same exporter, output redirected into the batch), after
   applying M to the seats; the json gets a `retarget` record;
3. writes `heads/bases.json` (head_frames / attach_gltf / head_target_height of the bases of this batch) and `heads/<race>/retarget.json`;
4. for `human_male` also the dragon heads (race-agnostic, authored in the human_male frame) and `heads/dragon_seats.json` with the same transform (seat' = M . seat . M^-1);
5. renders `heads/<race>/check_sheet.png` (body + several heads, front / side; human_male also `heads/dragon_check_sheet.png`) and `bases/<race>/sections_sheet.png` (the body sections).

Nothing is saved to the user's head blends and nothing is written outside the batch folder.
"""
from pathlib import Path
import json
import math
import sys
import time

import bpy
import numpy as np
from mathutils import Matrix, Vector

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]                                      # .../imageto3d
BLENDER = ROOT.parents[1]                                   # .../godot/blender
ITEMS_HEADS = BLENDER / 'items' / 'heads'
SCRIPTS_HEADS = BLENDER / 'scripts' / 'heads'
DRAGONS = ('dragon_type_1', 'dragon_type_2')
HEAD_RACES = ('dwarf_female', 'dwarf_male', 'elf_female', 'elf_male', 'halfling_female', 'halfling_male', 'human_female', 'human_male',
              'orc_female', 'orc_male')                     # races with a head library (gnome_female has none)
IDENTICAL_MM = 1.0                                          # default heads closer than this (every vertex, both ways) are the same head
C4 = Matrix(((1, 0, 0, 0), (0, 0, 1, 0), (0, -1, 0, 0), (0, 0, 0, 1)))        # Blender -> glTF


def _sibling(name):
    try:
        return __import__(__package__ + '.' + name, fromlist=['x']) if __package__ else __import__(name)
    except ImportError:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        return __import__(name)


def old_scripts():
    """(head_fit, build_race_heads, export_heads) of scripts/heads, imported as libraries."""
    for p in (str(SCRIPTS_HEADS), str(SCRIPTS_HEADS.parent)):
        if p not in sys.path:
            sys.path.insert(0, p)
    import head_fit
    import build_race_heads
    import export_heads
    return head_fit, build_race_heads, export_heads


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def dump_json(path, data):
    return _sibling('export_base').dump_json(path, data)


def to_blender(v):
    """glTF (x, y up, z fwd) -> Blender (x, -z, y)."""
    return np.array([v[0], -v[2], v[1]])


# --- measuring the new default head against the old one ---------------------------------------------------------

def default_head_arrays(head_block):
    """(vertices (n,3) Blender Head-bone frame, triangles) of base.json `head.default_head`."""
    d = head_block['default_head']
    return np.array([to_blender(v) for v in d['vertices']]), np.array(d['triangles'], dtype=np.int64)


def ref_default_head(race):
    """(vertices, triangles) of the old base's default head in the old Head-bone frame (`REF_default_head_<race>` of the open heads blend)."""
    obj = next(o for o in bpy.data.objects if o.type == 'MESH' and o.name.startswith('REF_default_head_') and not o.name.endswith('_wire'))
    me = obj.data
    me.calc_loop_triangles()
    v = np.array([list(obj.matrix_world @ x.co) for x in me.vertices])
    t = np.array([list(lt.vertices) for lt in me.loop_triangles], dtype=np.int64)
    return v, t


def chamfer_mm(a, b):
    """(max, median) over both directions of the nearest-vertex distance between two vertex sets, millimetres."""
    from mathutils.kdtree import KDTree

    def tree(p):
        kd = KDTree(len(p))
        for i, x in enumerate(p):
            kd.insert(Vector(x), i)
        kd.balance()
        return kd
    ta, tb = tree(a), tree(b)
    d = [tb.find(Vector(x))[2] for x in a] + [ta.find(Vector(x))[2] for x in b]
    return float(max(d)) * 1000, float(np.median(d)) * 1000


def silhouette_shift(old_v, old_t, new_v, new_t, px=0.001, n=512):
    """Translation T = (0, ty, tz) (Blender Head-bone frame) that moves the NEW default head onto the OLD one: the best overlap of the side
    silhouettes (FFT cross-correlation, `head_fit.best_shift`), x stays centred. Returns (T, iou_side, iou_front)."""
    hf = old_scripts()[0]
    saved = hf.PX, hf.N
    hf.PX, hf.N = px, n
    try:
        c_side = np.array([(old_v[:, 1].min() + old_v[:, 1].max()) / 2, (old_v[:, 2].min() + old_v[:, 2].max()) / 2])
        c_front = np.array([0.0, c_side[1]])
        ref_side = hf.raster(old_v[:, [1, 2]], old_t, c_side)
        ref_front = hf.raster(old_v[:, [0, 2]], old_t, c_front)
        hs = hf.raster(new_v[:, [1, 2]], new_t, np.zeros(2))
        (ki, kj), inter = hf.best_shift(np.fft.fft2(ref_side.astype(float)), hs.astype(float))
        ki, kj = hf.wrap(ki), hf.wrap(kj)
        ty, tz = c_side[0] + ki * px, c_side[1] + kj * px
        iou_side = float(inter / (ref_side.sum() + hs.sum() - inter))
        hfr = hf.raster(new_v[:, [0, 2]], new_t, np.array([0.0, c_front[1] - tz]))
        both = (hfr & ref_front).sum()
        iou_front = float(both / (ref_front.sum() + hfr.sum() - both))
    finally:
        hf.PX, hf.N = saved
    return np.array([0.0, ty, tz]), iou_side, iou_front


def measure(race, head_block, mode='auto'):
    """The retarget record of a race (see module docstring) and the shift (Blender Head-bone frame, metres) to apply to every seat."""
    old_v, old_t = ref_default_head(race)
    new_v, new_t = default_head_arrays(head_block)
    hf, br, eh = old_scripts()
    old_bone = Vector(br.BASE_FRAMES[race]['head_bone_gltf'])
    new_bone = Vector(head_block['head_frame']['head_bone_gltf'])
    # frames differ by the bone offset: a point keeps its model position when its head-frame coordinates move by (old_bone - new_bone)
    bone_shift = to_blender(old_bone - new_bone)
    cham_max, cham_med = chamfer_mm(old_v, new_v)
    T, iou_s, iou_f = silhouette_shift(old_v, old_t, new_v, new_t)
    mesh_shift = -T
    identical = cham_max <= IDENTICAL_MM
    if mode == 'auto':
        used = 'none' if identical else 'fit'
    else:
        used = mode
    fitted = None
    if used == 'fit':
        # similarity (uniform scale, pitch about X, translation) of the OLD default head onto the NEW one: M = T . Rx . S, `head_fit.fit`
        fitted = hf.fit(old_v, old_t, dict(verts=new_v, tris=new_t), pitches=np.arange(-12.0, 12.1, 3.0))
    if used == 'fit':
        M = hf.fit_matrix(fitted)
    else:
        M = Matrix.Translation(Vector({'none': np.zeros(3), 'mesh': mesh_shift, 'bone': bone_shift}[used]))
    shift = np.array(M.translation)
    moved = np.array([list(M @ Vector(v)) for v in old_v])
    cham_after = chamfer_mm(moved, new_v)
    old_size = float(br.BASE_FRAMES[race]['head_size'])
    rec = dict(race=race, mode_requested=mode, mode_used=used, identical_default_head=bool(identical),
               default_head_chamfer_mm=dict(max=round(cham_max, 3), median=round(cham_med, 3)),
               head_bone_old_gltf=[round(float(x), 5) for x in old_bone], head_bone_new_gltf=[round(float(x), 5) for x in new_bone],
               head_bone_offset_mm=[round(float(x) * 1000, 2) for x in (new_bone - old_bone)],
               head_size_old=old_size, head_size_new=head_block['head_frame']['head_size'],
               silhouette=dict(iou_side=round(iou_s, 4), iou_front=round(iou_f, 4), pixel_mm=1.0),
               fit=None if fitted is None else dict(pitch_deg=fitted['pitch'], scale=round(fitted['scale'], 4), iou_side=round(fitted['iou_side'], 4),
                                                    iou_front=round(fitted['iou_front'], 4)),
               matrix_blender=[[round(float(x), 6) for x in row] for row in M],
               scale=round(float(M.to_3x3().median_scale), 4), pitch_deg=round(math.degrees(M.to_euler('XYZ').x), 2),
               default_head_chamfer_after_mm=dict(max=round(cham_after[0], 3), median=round(cham_after[1], 3)),
               shift_blender_mm=[round(float(x) * 1000, 2) for x in shift],
               shift_gltf_mm=[round(float(x) * 1000, 2) for x in (shift[0], shift[2], -shift[1])],
               mesh_shift_gltf_mm=[round(float(x) * 1000, 2) for x in (mesh_shift[0], mesh_shift[2], -mesh_shift[1])],
               bone_shift_gltf_mm=[round(float(x) * 1000, 2) for x in (bone_shift[0], bone_shift[2], -bone_shift[1])],
               note='shift = translation added to every head seat (Head-bone frame); mesh = keep the head where it sat relative to the default '
                    'head (silhouette match of old and new default head), bone = keep its model-space position (old - new Head bone)')
    return rec, M


# --- exporting the heads ----------------------------------------------------------------------------------------------

def shift_seats(heads, m):
    """Apply `m` (Blender, Head-bone frame) to every head's seat (object transform relative to its slot at the origin)."""
    if (m - Matrix.Identity(4)).to_3x3().median_scale < 1e-9 and m.translation.length < 1e-9:
        return
    for o in heads:
        o.matrix_basis = m @ o.matrix_basis


def tag_json(race, out_dir, record, ids):
    """Add the `retarget` record to the exported head jsons."""
    for hid in ids:
        p = Path(out_dir) / race / f'{hid}.json'
        if p.is_file():
            meta = read_json(p)
            meta['retarget'] = dict(standard='imageto3d_v2', mode=record['mode_used'], scale=record['scale'], pitch_deg=record['pitch_deg'],
                                    shift_gltf_mm=record['shift_gltf_mm'])
            p.write_text(json.dumps(meta, indent=1), encoding='utf-8')


def export_library(race, blend, out_dir, record, shift, reference, limit=None):
    """Open a head library blend, shift its seats and export it into out_dir/<race>/ (never saves the blend); returns the head count."""
    bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False)
    hf, br, eh = old_scripts()
    heads = eh.head_objects()
    if limit:
        heads = heads[:limit]
    shift_seats(heads, shift)
    pairs = [(o, eh.info_from_blend(o)) for o in heads]
    br.EXPORT = Path(out_dir)
    _, ref_h = br.reference_for(race)
    br.export_race(race, pairs, reference, ref_h)
    tag_json(race, out_dir, record, [o.name for o in heads])
    return len(pairs)


def export_dragon_seats(out_dir, m):
    """heads/dragon_seats.json for human_male: the human_male seat matrices with the Head-bone shift applied (seat and the dragon GLB's own
    fit both move by M, so  seat' = M . seat . M^-1  in the re-targeted frame)."""
    blend = ITEMS_HEADS / 'human_male' / 'human_male_dragon_seating.blend'
    bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False)
    hf, br, eh = old_scripts()
    br.EXPORT = Path(out_dir)
    t = m
    for o in bpy.data.objects:
        if 'seat_head' in o:
            fit = Matrix([list(o['glb_fit'][i:i + 4]) for i in range(0, 16, 4)])
            o['glb_fit'] = [x for r in (t @ fit) for x in r]
            o.matrix_basis = t @ o.matrix_basis
    return eh.export_dragon_seats('human_male')


def bases_json(out_dir, batch_dir):
    """heads/bases.json (version 3) from the base.json of every base in the batch: Head-bone frame, attach data, target heights.

    head_size is the measured size of the new default head when the race's heads were scaled to it (retarget.json scale != 1, human_male), else the
    legacy value (so heads authored against the old default head keep their size); the measured size is always `head_size_measured`."""
    hf, br, eh = old_scripts()
    frames, attach, target, notes = {}, {}, {}, {}
    for p in sorted((Path(batch_dir) / 'bases').glob('*/base.json')):
        d = read_json(p)
        head = d.get('head')
        if not head:
            continue
        race = d['base']
        frame = dict(head['head_frame'])
        frame['head_size_measured'] = frame['head_size']
        rp = Path(out_dir) / race / 'retarget.json'
        scaled = rp.is_file() and abs(read_json(rp).get('scale', 1.0) - 1.0) > 5e-3          # heads were scaled to the new default head: head_size follows
        if not scaled and race in br.BASE_FRAMES and abs(frame['head_size'] - br.BASE_FRAMES[race]['head_size']) > 5e-4:
            frame['head_size'] = br.BASE_FRAMES[race]['head_size']
            notes[race] = 'head_size kept at the legacy value so heads authored against the old default head keep their size'
        frames[race] = frame
        if head.get('attach'):
            attach[race] = dict(anchor=head['attach']['anchor'], falloff=head['attach']['falloff'], mode=head['attach']['mode'])
            target[race] = head['head_target_height']
    path = Path(out_dir) / 'bases.json'
    dump_json(path, dict(
        version=3, standard='imageto3d_v2', head_frames=frames, head_target_height=target, attach_gltf=attach, head_size_notes=notes,
        placement=('head GLBs are in their reference_base Head-bone frame. Put the GLB root at the body Head bone, uniform scale '
                   'head_frames[body].head_size / head_frames[head.reference_base].head_size, then join the neck (cap by default).'),
        note=('measured on the exported bases (bases/<race>/base.json head block); head_bone_gltf is the Head bone of the NEW skeleton. '
              'Heads exported by export-heads are already shifted into that frame (heads/<race>/retarget.json).')))
    return path


def export_heads(race, batch_dir, mode='auto', dragons=True, sheet=True, limit=None, log=print):
    """Export the head library of one race for the base in `batch_dir/bases/<race>/`; human_male also the dragon heads + seats."""
    batch_dir = Path(batch_dir)
    base_json = batch_dir / 'bases' / race / 'base.json'
    if not base_json.is_file():
        raise FileNotFoundError(f'{base_json} does not exist (run export-base for {race} first)')
    block = read_json(base_json)['head']
    blend = ITEMS_HEADS / race / f'{race}_heads.blend'
    if not blend.is_file():
        raise FileNotFoundError(f'{blend}: {race} has no head library')
    out = batch_dir / 'heads'
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    # measure first (needs the library's REF_default_head), then export with the shifted seats
    bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False)
    record, shift = measure(race, block, mode)
    log(f'[{race}] default head chamfer {record["default_head_chamfer_mm"]["max"]} mm, shift {record["shift_gltf_mm"]} mm scale {record["scale"]} pitch {record["pitch_deg"]} ({record["mode_used"]}); '
        f'bone offset {record["head_bone_offset_mm"]} mm')
    count = export_library(race, blend, out, record, shift, race, limit)
    record['heads'] = count
    result = dict(race=race, heads=count, retarget=record, dragons={})
    if race == 'human_male' and dragons:
        for dragon in DRAGONS:
            result['dragons'][dragon] = export_library(dragon, ITEMS_HEADS / dragon / f'{dragon}_heads.blend', out, record, shift, 'human_male', limit)
        seats = export_dragon_seats(out, shift)
        result['dragon_seats'] = seats
    dump_json(out / race / 'retarget.json', record)
    bases_json(out, batch_dir)
    if sheet:
        try:
            result['sheets'] = render_sheets(race, batch_dir, log=log)
        except Exception as e:                                          # a failed render must not lose the export
            import traceback
            traceback.print_exc()
            result['sheet_error'] = f'{type(e).__name__}: {e}'
    result['seconds'] = round(time.time() - t0, 1)
    return result


# --- check sheets -------------------------------------------------------------------------------------------------------------

BODY_COLOUR = (0.62, 0.68, 0.74, 1.0)
HEAD_COLOUR = (0.93, 0.78, 0.60, 1.0)
WIRE_COLOUR = (0.9, 0.1, 0.1, 1.0)
PX_PER_M = 640


def _import_glb(path):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(path))
    return [o for o in bpy.data.objects if o not in before]


def _detach(obj):
    """A render copy of a (skinned) mesh object: no parent / modifiers, keeps its rest world matrix."""
    mw = obj.matrix_world.copy()
    c = obj.copy()
    c.data = obj.data
    bpy.context.scene.collection.objects.link(c)
    c.parent = None
    for m in list(c.modifiers):
        c.modifiers.remove(m)
    c.matrix_world = mw
    c.hide_render = False
    return c


def _setup_render(width, height, ortho):
    sc = bpy.context.scene
    sc.render.engine = 'BLENDER_WORKBENCH'
    sc.display.shading.light = 'STUDIO'
    sc.display.shading.color_type = 'OBJECT'
    sc.display.shading.show_object_outline = True
    sc.display.shading.show_cavity = False
    sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = width, height, 100
    sc.render.film_transparent = False
    sc.world = sc.world or bpy.data.worlds.new('w')
    sc.world.color = (0.93, 0.93, 0.93)
    sc.render.image_settings.file_format = 'PNG'
    sc.view_settings.view_transform = 'Standard'
    cam = bpy.data.objects.new('cam', bpy.data.cameras.new('cam'))
    cam.data.type = 'ORTHO'
    cam.data.ortho_scale = ortho
    cam.data.clip_start, cam.data.clip_end = 0.01, 50
    sc.collection.objects.link(cam)
    sc.camera = cam
    return cam


def _render(path):
    bpy.context.scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)


def _stack(paths, out):
    """Stack rendered PNGs vertically into one image (numpy via bpy images)."""
    rows = []
    for p in paths:
        img = bpy.data.images.load(str(p))
        w, h = img.size
        arr = np.empty(w * h * 4, np.float32)
        img.pixels.foreach_get(arr)
        rows.append(arr.reshape(h, w, 4))
        bpy.data.images.remove(img)
    sheet = np.concatenate(rows[::-1], axis=0)                      # image rows run bottom -> top: the first path ends up on top
    img = bpy.data.images.new('sheet', sheet.shape[1], sheet.shape[0], alpha=False)
    img.pixels.foreach_set(sheet.ravel())
    img.filepath_raw = str(out)
    img.file_format = 'PNG'
    img.save()
    bpy.data.images.remove(img)


def _label(text, location, size=0.045):
    curve = bpy.data.curves.new('t', 'FONT')
    curve.body = text
    curve.size = size
    curve.align_x = 'CENTER'
    obj = bpy.data.objects.new('t', curve)
    obj.location = location
    obj.rotation_euler = (math.radians(90), 0, 0)
    obj.color = (0.1, 0.1, 0.1, 1.0)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _tile(index, spacing, origin_xy, members):
    """An empty at (index * spacing, 0, 0) holding copies of `members` moved so that origin_xy (the Head bone) is its origin; returns the empty."""
    tile = bpy.data.objects.new(f'tile{index}', None)
    tile.location = (index * spacing, 0, 0)
    bpy.context.scene.collection.objects.link(tile)
    shift = Matrix.Translation((-origin_xy[0], -origin_xy[1], 0))
    for m in members:
        m.parent = tile
        m.matrix_parent_inverse.identity()
        m.matrix_basis = shift @ m.matrix_world
    return tile


def _neck_cap(ring_gltf, apex_up):
    """A fan over the body's open neck seam (rises by `apex_up`), standing in for the game's neck cap."""
    ring = [Vector(to_blender(p)) for p in ring_gltf]
    c = sum(ring, Vector()) / len(ring)
    apex = c + Vector((0, 0, apex_up))
    me = bpy.data.meshes.new('cap')
    me.from_pydata([*ring, apex], [], [(i, (i + 1) % len(ring), len(ring)) for i in range(len(ring))])
    me.update()
    return me


def render_tiles(columns, hb, view_rows, out, title, label_height, originals=()):
    """`columns` = [(label, [mesh objects in world space (Head bone frame placed on hb)], [wire objects])]; renders one PNG per view row
    (front, side) of all tiles side by side and stacks them into `out`."""
    sc = bpy.context.scene
    spacing, th, tw = 0.62, 0.78, 0.62
    zc = hb.z + 0.12
    tiles = []
    for i, (label, members, wires) in enumerate(columns):
        copies = [_detach(m) for m in members]
        for c in copies:
            c.color = BODY_COLOUR if c.get('os_body') else HEAD_COLOUR
        for w in wires:                                              # the default head as a thin red wireframe (Workbench ignores display_type WIRE)
            w.color = WIRE_COLOUR
            mod = w.modifiers.new('wire', 'WIREFRAME')
            mod.thickness = 0.0025
            copies.append(w)
        tiles.append(_tile(i, spacing, (hb.x, hb.y), copies))
        _label(label, (i * spacing, -0.8, zc - th / 2 + 0.05), 0.04)
    for o in originals:                                              # the imported objects stay out of the render; their copies are in the tiles
        o.hide_render = True
    n = len(columns)
    width, height = int(n * tw * PX_PER_M), int(th * PX_PER_M)
    cam = _setup_render(width, height, n * spacing)
    cam.location = ((n - 1) * spacing / 2, -4.0, zc)
    cam.rotation_euler = (math.radians(90), 0, 0)
    parts = []
    for name, rot in view_rows:
        for t in tiles:
            t.rotation_euler = (0, 0, math.radians(rot))
        p = Path(out).with_name(Path(out).stem + f'_{name}.png')
        _render(p)
        parts.append(p)
    _stack(parts, out)
    for p in parts:
        p.unlink(missing_ok=True)


def render_sheets(race, batch_dir, count=8, log=print):
    """check sheets of a race from the exported files: body + heads (heads/<race>/check_sheet.png) and the body sections."""
    batch_dir = Path(batch_dir)
    base_dir = batch_dir / 'bases' / race
    heads_dir = batch_dir / 'heads' / race
    base = read_json(base_dir / 'base.json')
    catalog = read_json(heads_dir / 'catalog.json')
    ids = [h for h, e in catalog['heads'].items() if e.get('usable')]
    pick = [ids[round(i * (len(ids) - 1) / max(count - 2, 1))] for i in range(count - 1)] if len(ids) >= count - 1 else ids
    pick = list(dict.fromkeys(pick))
    out = {}
    bpy.ops.wm.read_factory_settings(use_empty=True)
    new = _import_glb(base_dir / 'base.glb')
    arm = next(o for o in new if o.type == 'ARMATURE')
    hb = arm.matrix_world @ arm.data.bones['Head'].head_local
    sections = [o for o in new if o.type == 'MESH' and o.name.startswith('sec_')]
    default_head = next(o for o in new if o.type == 'MESH' and o.name == 'head')
    for o in sections:
        o['os_body'] = True
    seam = base['head'].get('neck_seam')
    cap = None
    if seam:
        cap = bpy.data.objects.new('cap', _neck_cap(seam['ring'], 0.6 * seam['radius']))
        bpy.context.scene.collection.objects.link(cap)
        cap['os_body'] = True
        sections_with_cap = sections + [cap]
    else:
        sections_with_cap = sections
    columns = []
    # column 0: the body with its own default head; then the library heads on the same body
    columns.append(('default head', sections_with_cap + [default_head], []))
    wire_copies = []
    for hid in pick:
        objs = _import_glb(heads_dir / f'{hid}.glb')
        meshes = [o for o in objs if o.type == 'MESH']
        for m in meshes:
            m.matrix_world = Matrix.Translation(hb) @ m.matrix_world
        wire_copies.append(_detach(default_head))
        columns.append((hid.replace(f'h_{race}_', ''), sections_with_cap + meshes, [wire_copies[-1]]))
    originals = [o for o in bpy.data.objects if o not in wire_copies]
    render_tiles(columns, hb, [('front', 0), ('side', 90)], heads_dir / 'check_sheet.png', race, 0, originals)
    out['check_sheet'] = str(heads_dir / 'check_sheet.png')
    out['sheet_heads'] = pick
    seats_file = batch_dir / 'heads' / 'dragon_seats.json'
    if race == 'human_male' and seats_file.is_file():
        out.update(dragon_sheet(batch_dir, hb, default_head, sections_with_cap))
    render_sections(race, base_dir, base)
    out['sections_sheet'] = str(base_dir / 'sections_sheet.png')
    return out


def seat_matrix(row_major):
    """4x4 row-major seat (glTF axes, GLB model space -> body Head-bone frame) -> Blender matrix."""
    m = Matrix([row_major[i:i + 4] for i in range(0, 16, 4)])
    return C4.inverted() @ m @ C4


def dragon_sheet(batch_dir, hb, default_head, body, count=4):
    """heads/dragon_check_sheet.png: a few dragon heads on the human_male body, placed with their `dragon_seats.json` matrices."""
    seats = read_json(Path(batch_dir) / 'heads' / 'dragon_seats.json')['seats']
    picks = []
    for dragon in DRAGONS:
        ids = sorted(h for h in seats if h.startswith(f'h_{dragon}_'))
        picks += [ids[0], ids[len(ids) // 2], ids[-1]][:count // 2 + 1]
    picks = picks[:count + 2]
    keep = list(bpy.data.objects)
    columns, wires = [('default head', body + [default_head], [])], []
    for hid in picks:
        dragon = hid.rsplit('_', 2)[0][2:]
        meshes = [o for o in _import_glb(Path(batch_dir) / 'heads' / dragon / f'{hid}.glb') if o.type == 'MESH']
        for m in meshes:
            m.matrix_world = Matrix.Translation(hb) @ seat_matrix(seats[hid]['human_male']) @ m.matrix_world
        wires.append(_detach(default_head))
        columns.append((hid.replace('h_dragon_type_', 'd'), body + meshes, [wires[-1]]))
    for o in keep:
        o.hide_render = True                       # the first sheet's tiles and copies
    originals = [o for o in bpy.data.objects if o not in wires]
    out = Path(batch_dir) / 'heads' / 'dragon_check_sheet.png'
    render_tiles(columns, hb, [('front', 0), ('side', 90)], out, 'dragons', 0, originals)
    return dict(dragon_sheet=str(out), dragon_sheet_heads=picks)


def render_sections(race, base_dir, base):
    """Full-body renders (front, side, back) of the exported base with every section in its own colour (and the default head)."""
    import colorsys
    bpy.ops.wm.read_factory_settings(use_empty=True)
    new = _import_glb(Path(base_dir) / 'base.glb')
    meshes = [o for o in new if o.type == 'MESH' and (o.name == 'head' or o.name.startswith('sec_'))]    # not the importer's bone shapes
    sc = bpy.context.scene
    copies = []
    secs = sorted(o.name for o in meshes if o.name.startswith('sec_'))
    for o in meshes:
        c = _detach(o)
        if o.name in secs:
            k = secs.index(o.name)
            c.color = (*colorsys.hsv_to_rgb((k * 0.381966) % 1.0, 0.55, 0.92), 1.0)
        else:
            c.color = HEAD_COLOUR
        copies.append(c)
    zs = [v.co.z for c in copies for v in c.data.vertices]
    xs = [abs(v.co.x) for c in copies for v in c.data.vertices]
    height = max(zs) - min(zs) + 0.2
    width = max(max(xs) * 2 + 0.2, height * 0.5)
    spacing = max(width, 0.9)
    tiles = []
    for i in range(3):
        tile = bpy.data.objects.new(f'v{i}', None)
        tile.location = (i * spacing, 0, 0)
        sc.collection.objects.link(tile)
        tiles.append(tile)
    # one copy of the body per view (shared mesh data), turned about Z: front, side, back
    for i, tile in enumerate(tiles):
        for c in copies:
            d = c.copy()
            d.data = c.data
            d.color = c.color
            d.display_type = 'TEXTURED'
            sc.collection.objects.link(d)
            d.parent = tile
        tile.rotation_euler = (0, 0, math.radians((0, 90, 180)[i]))
    for c in copies:
        sc.collection.objects.unlink(c)
    for o in new:
        o.hide_render = True
    px = 380
    cam = _setup_render(int(3 * spacing * px), int(height * px), 3 * spacing)
    cam.location = (spacing, -6.0, (max(zs) + min(zs)) / 2)
    cam.rotation_euler = (math.radians(90), 0, 0)
    out = Path(base_dir) / 'sections_sheet.png'
    _render(out)
    return out


# --- entry for running on its own ---------------------------------------------------------------------------------------

def main(argv=None):
    import argparse
    args = sys.argv[sys.argv.index('--') + 1:] if argv is None and '--' in sys.argv else (argv or [])
    p = argparse.ArgumentParser()
    p.add_argument('race')
    p.add_argument('--batch', required=True)
    p.add_argument('--shift', default='auto', choices=('auto', 'fit', 'mesh', 'bone', 'none'))
    p.add_argument('--no-dragons', action='store_true')
    p.add_argument('--no-sheet', action='store_true')
    p.add_argument('--limit', type=int, default=None)
    a = p.parse_args(args)
    r = export_heads(a.race, a.batch, a.shift, not a.no_dragons, not a.no_sheet, a.limit)
    print('EXPORT_HEADS', json.dumps(r, default=str))
    return r


if __name__ == '__main__':
    main()
