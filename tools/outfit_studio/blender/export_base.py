"""Base export: skeleton + sectioned body + default head GLB and base.json for one base project (SPEC §7, game standard v2).

`export_base()` runs inside a Blender process that has the base project open and works on the
in-memory session only (weights repaired, head split, sections cut, ARP export mutate it), so it never saves.
The `ostudio.export_base` operator therefore runs it in a background `blender -b` subprocess on
the SAVED base file, and the same entry point works headless:
`blender -b projects/<base>.blend --python export_base.py -- --out <dir> [--base <name>] [--layout fine|coarse]`.

base.glb holds ONE skin (ARP Humanoid/Godot skeleton, sockets and AP_* leaf joints) and these mesh nodes, all on one material `skin`:
- `head`: the default head (the `bone_head.x` faces), the body's seam toward it stays OPEN (the head library caps it game-side);
- `sec_<part>_<band>`: the body (without the head) cut into the fixed sections of experiments/body_mask_20261104 (layout `fine`),
  so the game can hide sections per outfit. Every node carries the `bulk_*` / `head_*` morph targets that move it.
base.json (version 2) lists the sections, the head frame / attach data (bases.json head_frames / attach_gltf / head_target_height
equivalents, measured on this base) and the default head silhouette used by the head export (`heads.py`).
"""
from pathlib import Path
import hashlib
import json
import math
import os
import subprocess
import sys
import time

import bpy
import numpy as np
from mathutils import Vector

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]                                      # .../imageto3d
PROJECTS = ROOT / 'projects'
EXPORT = ROOT.parents[1] / 'export' / 'outfit_studio'
SPACING_DEFAULT = dict(arm_deg=8.0, leg_cm=3.0)


def _arp():
    """The arp_export helper, whether this file runs as a package module or as a --python script."""
    try:
        from . import arp_export
    except ImportError:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import arp_export
    return arp_export


def _sibling(name):
    try:
        return __import__(__package__ + '.' + name, fromlist=['x']) if __package__ else __import__(name)
    except ImportError:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        return __import__(name)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def dump_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, default=lambda v: float(v) if hasattr(v, '__float__') else list(v)) + '\n')
    os.replace(tmp, path)


def reset_pose(rig):
    """Rest-pose, action-free session, as native_worker.load_source leaves it."""
    scene = bpy.context.scene
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    if rig.animation_data:
        rig.animation_data.action = None
        for track in list(rig.animation_data.nla_tracks):
            rig.animation_data.nla_tracks.remove(track)
    for pb in rig.pose.bones:
        pb.matrix_basis.identity()
    rig.data.pose_position = 'POSE'
    scene.frame_set(1)
    bpy.context.view_layer.update()


def open_base(path=None):
    """Enable ARP, (re)open a base project in this process and return (body, rig) at rest."""
    _arp().enable_arp()
    if path:
        bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False)
    body = bpy.data.objects['base']
    rig = body.find_armature()
    if rig is None or rig.get('ce_rig_workflow') != 'standard_arp_v1':
        raise ValueError('Expected a native ARP base project (rig ce_rig_workflow=standard_arp_v1)')
    reset_pose(rig)
    return body, rig


def rig_fingerprint(rig):
    """sha256 of sorted (bone, parent, rounded head/tail/roll-axis) for deform bones and sockets."""
    try:
        return _sibling('project').rig_fingerprint(rig)
    except Exception:
        pass
    sockets = _arp().ce('native_contract').SOCKETS
    rows = []
    for b in rig.data.bones:
        if b.use_deform or b.name in sockets:
            rows.append([b.name, b.parent.name if b.parent else None,
                         [round(v, 5) for v in b.head_local], [round(v, 5) for v in b.tail_local],
                         [round(v, 5) for v in b.matrix_local.to_3x3().col[2]]])
    return hashlib.sha256(json.dumps(sorted(rows), separators=(',', ':')).encode()).hexdigest()


def read_spacing(scene):
    try:
        return _sibling('spacing').read_settings(scene)
    except Exception:
        return dict(SPACING_DEFAULT)


def ensure_morphs(body, rig, ce):
    """Head (and, if the project has none, bulk) shape keys; returns the non-Basis key names."""
    keys = lambda: [k.name for k in body.data.shape_keys.key_blocks[1:]] if body.data.shape_keys else []
    if not any(k.startswith('head_') for k in keys()):
        ce('head_morphs').install(body)
    if not any(k.startswith('bulk_') for k in keys()):
        ce('native_morphs').body_bulk_keys(body, rig)
    return keys()


def attachment_points(body, rig, ce):
    """AP surface bindings (+ morph offsets); re-fits the authored contract if the weight repair moved a dominant bone."""
    restore = ce('native_attachment_restore')
    refit = None
    try:
        points = restore.bindings(body, rig)
    except ValueError as e:
        refit = str(e)
        restore.refit_authored(body, rig)
        points = restore.bindings(body, rig)
    ce('native_morphs').attachment_offsets(body, points)
    return points, refit


SEAT = 'Seat'
SEAT_RULE = ('x = 0. Candidate vertices (weights as exported, top 4): those dominated (weight >= 0.5) by Hips within the hip-joint half width of the '
             'midline, plus those dominated by Hips or an UpperLeg bone within 2 cm of the midline. y = lowest candidate; z = centre of the z extent '
             'of the candidates within 3 cm above that height. Child of Hips, identity local rotation, constant local offset, no skin weights '
             '(scripts/character_export/ride_rig.py Rig.seat)')


def seat_rule(body, rig, race=None, band=0.03):
    """Rider Seat rest position in glTF model space: the exact rule of ride_rig.Rig.seat on the mesh that is exported, i.e. the base cut into
    the canonical `fine` sections (the cut adds vertices along the section planes, which can be the lowest candidates) with the weights
    limited to the top 4. Returns (position, info)."""
    arp = _arp()
    bs = _core('body_sections')
    engine = arp.ce('native_contract').engine_name
    Vw, Ft, part_names, W, groups, keys = body_arrays(body)
    names = sorted(set(part_names))
    mesh = bs.Mesh(Vw, Ft, np.array([names.index(p) for p in part_names]), names)
    race = race or Path(bpy.data.filepath).stem.removesuffix('_outfits')
    cut = bs.cut_base(mesh, rest_pivots(rig), W, 'fine', race, bs.channel_weights(W, groups))
    Wc = cut['A']
    top = np.argsort(-Wc, axis=1)[:, :4]
    keep = np.zeros_like(Wc)
    np.put_along_axis(keep, top, np.take_along_axis(Wc, top, 1), 1)
    Wc = keep / np.maximum(keep.sum(1, keepdims=True), 1e-12)
    P = cut['V']
    P = np.stack([P[:, 0], P[:, 2], -P[:, 1]], axis=1)               # Blender -> glTF (x, z, -y)
    eng = [engine(g) for g in groups]
    hips = Wc[:, [i for i, n in enumerate(eng) if n.startswith('Hips')]].sum(1)
    pelvis = Wc[:, [i for i, n in enumerate(eng) if n.startswith(('Hips', 'LeftUpperLeg', 'RightUpperLeg'))]].sum(1)
    head = rig.matrix_world @ rig.data.bones['thigh_stretch.l'].head_local          # the bone exported as LeftUpperLeg
    hx = float(head.x)
    cand = ((hips >= 0.5) & (np.abs(P[:, 0]) <= hx)) | ((pelvis >= 0.5) & (np.abs(P[:, 0]) <= 0.02))
    y0 = float(P[cand, 1].min())
    sel = cand & (P[:, 1] <= y0 + band)
    zc = 0.5 * float(P[sel, 2].min() + P[sel, 2].max())
    return np.array([0.0, y0, zc]), dict(hip_half_width=round(hx, 6), candidates=int(cand.sum()), in_band=int(sel.sum()), mesh='fine sections')


def ensure_seat(body, rig, race=None):
    """Add (or move) the rider `Seat` bone: a non-deform custom bone ('cc', like the sockets) parented to `root.x` (exported as Hips; the Hips
    joint is root.x's TAIL and the export turns the bone round, so Seat takes root.x's orientation turned 180 degrees about X = identity local
    rotation in the GLB), no skin weights. Returns the seat record (also stored in rig['ostudio_seat'])."""
    from mathutils import Matrix, Vector
    pos, info = seat_rule(body, rig, race)
    world = Vector((pos[0], -pos[2], pos[1]))                          # glTF -> Blender
    local = rig.matrix_world.inverted() @ world
    ce = _arp().ce
    ce('standard_arp').activate(rig, 'EDIT')
    eb = rig.data.edit_bones
    root = eb['root.x']
    seat = eb.get(SEAT) or eb.new(SEAT)
    m = (root.matrix.to_3x3() @ Matrix.Diagonal((1, -1, -1))).to_4x4()
    m.translation = local
    seat.use_connect = False
    seat.use_deform = False
    seat.parent = root
    seat.matrix = m
    seat.length = 0.02
    bpy.ops.object.mode_set(mode='OBJECT')
    bone = rig.data.bones[SEAT]
    bone['cc'] = 1
    rig.pose.bones[SEAT]['cc'] = 1
    attachments = rig.data.collections.get('Attachments')
    if attachments and SEAT not in attachments.bones:
        attachments.assign(bone)
    hips = rig.matrix_world @ rig.data.bones['root.x'].tail_local            # the Hips joint
    rec = dict(name=SEAT, parent='Hips', rest_gltf=[round(float(x), 6) for x in pos],
               local_offset_gltf=[round(float(pos[0] - hips.x), 6), round(float(pos[1] - hips.z), 6), round(float(pos[2] + hips.y), 6)],
               rule=SEAT_RULE, **info)
    rig['ostudio_seat'] = json.dumps(rec)
    return rec


def prepare_session(body, rig, morphs=True, race=None):
    """In-memory export prep shared by base and clip exports: weight repair, morphs, AP bindings, the rider Seat bone.
    Returns (repaired {name: [vertex ids]}, morph names, attachment bindings, refit message|None)."""
    arp = _arp()
    fixed = arp.fix_weights([body], rig)                           # top-4 + normalise, in memory only
    names = ensure_morphs(body, rig, arp.ce) if morphs else []
    points, refit = attachment_points(body, rig, arp.ce)
    ensure_seat(body, rig, race)
    return fixed, names, points, refit


def _core(name):
    """A pure-numpy module of ../core, importable from a package import and from a --python script run alike."""
    import importlib
    folder = str(HERE.parent / 'core')
    if folder not in sys.path:
        sys.path.insert(0, folder)
    return importlib.import_module(name)


def env_versions():
    """Blender / Auto-Rig Pro versions of this export (recorded in base.json)."""
    arp = None
    try:
        arp_mod = sys.modules.get(_arp().ARP_MODULE)
        info = getattr(arp_mod, 'bl_info', None)
        arp = '.'.join(map(str, info['version'])) if info and 'version' in info else None
        if arp is None:
            manifest = Path(arp_mod.__file__).parent / 'blender_manifest.toml'
            for line in manifest.read_text(encoding='utf-8').splitlines():
                if line.startswith('version'):
                    arp = line.split('=', 1)[1].strip().strip('"')
                    break
    except Exception:
        pass
    return dict(blender=bpy.app.version_string, auto_rig_pro=arp)


# --- skin material, body arrays, sections -------------------------------------------------------------------

SKIN_COLOUR = (0.86, 0.64, 0.52, 1.0)


def skin_material():
    """The one material of the base meshes. The engine ignores GLB colours (it tints `skin` itself)."""
    mat = bpy.data.materials.get('skin') or bpy.data.materials.new('skin')
    mat.diffuse_color = SKIN_COLOUR
    try:
        mat.use_nodes = True
    except Exception:
        pass
    node = mat.node_tree.nodes.get('Principled BSDF') if mat.node_tree else None
    if node:
        node.inputs['Base Color'].default_value = SKIN_COLOUR
        node.inputs['Roughness'].default_value = 0.9
    return mat


def assign_skin(obj, mat):
    """Every face of the mesh on the single `skin` material."""
    me = obj.data
    me.materials.clear()
    me.materials.append(mat)
    me.polygons.foreach_set('material_index', [0] * len(me.polygons))
    me.update()


def part_of_material(name):
    """`bone_thigh_stretch.l.001` -> `thigh_stretch.l`."""
    name = name.rsplit('.', 1)[0] if name.rsplit('.', 1)[-1].isdigit() and name.count('.') >= 2 else name
    return name[5:] if name.startswith('bone_') else name


def body_arrays(obj):
    """Triangulated rest data of a body object: world positions V, triangles F, part name per triangle, skin weights W (n, groups),
    group names, and the shape-key deltas {name: {delta (n, 3) local, slider_min, slider_max}}."""
    me = obj.data
    me.calc_loop_triangles()
    n = len(me.vertices)
    V = np.empty(n * 3)
    me.vertices.foreach_get('co', V)
    V = V.reshape(n, 3)
    M = np.array(obj.matrix_world)
    Vw = V @ M[:3, :3].T + M[:3, 3]
    tris = me.loop_triangles
    F = np.empty(len(tris) * 3, np.int64)
    tris.foreach_get('vertices', F)
    poly = np.empty(len(tris), np.int64)
    tris.foreach_get('polygon_index', poly)
    mat_index = np.empty(len(me.polygons), np.int64)
    me.polygons.foreach_get('material_index', mat_index)
    mats = [part_of_material(m.name) if m else '' for m in me.materials]
    part = [mats[mat_index[p]] for p in poly]
    groups = [g.name for g in obj.vertex_groups]
    W = np.zeros((n, len(groups)))
    for v in me.vertices:
        for g in v.groups:
            W[v.index, g.group] = g.weight
    keys = {}
    if me.shape_keys:
        ref = me.shape_keys.reference_key
        basis = np.empty(n * 3)
        ref.data.foreach_get('co', basis)
        for kb in me.shape_keys.key_blocks:
            if kb == ref:
                continue
            co = np.empty(n * 3)
            kb.data.foreach_get('co', co)
            keys[kb.name] = dict(delta=(co - basis).reshape(n, 3), slider_min=kb.slider_min, slider_max=kb.slider_max)
    return Vw, F.reshape(-1, 3), part, W, groups, keys


def rest_pivots(rig):
    """{'arm.l': world xyz, ...}: the rest joints the section planes follow (core.body_sections.PIVOT_BONE)."""
    bs = _core('body_sections')
    mw = rig.matrix_world
    return {f'{limb}.{side}': np.array(mw @ rig.data.bones[f'{bs.PIVOT_BONE[limb]}.{side}'].head_local)
            for limb in bs.LIMBS for side in 'lr'}


def make_sections(body, rig, race, mat, layout='fine'):
    """Cut `body` (the split-off body object, skinned + morphs) into the fixed sections; returns (objects, section records, report).

    One object per section named `sec_<part>_<band>`, copies of `body` (same parent / armature modifier) on the `skin` material with its own
    vertex groups and the morph keys that move it. The cuts are exact plane cuts; weights and morph deltas of the cut vertices are
    interpolated along the cut edges (the weight repair afterwards limits them to 4 influences)."""
    bs = _core('body_sections')
    Vw, Ft, part_names, W, groups, keys = body_arrays(body)
    names = sorted(set(part_names))
    part = np.array([names.index(p) for p in part_names])
    mesh = bs.Mesh(Vw, Ft, part, names)
    key_names = list(keys)
    attrs = np.concatenate([W] + [keys[k]['delta'] for k in key_names], axis=1)
    cut = bs.cut_base(mesh, rest_pivots(rig), attrs, layout, race, bs.channel_weights(W, groups))
    G = len(groups)
    Wc = cut['A'][:, :G]
    Dc = {k: cut['A'][:, G + 3 * i:G + 3 * i + 3] for i, k in enumerate(key_names)}
    inv = np.linalg.inv(np.array(body.matrix_world))
    Vl = cut['V'] @ inv[:3, :3].T + inv[:3, 3]
    engine = _arp().ce('native_contract').engine_name
    objects, records = [], []
    for s, sec in enumerate(cut['sec_names']):
        tri = cut['F'][cut['sec'] == s]
        used, inv_idx = np.unique(tri, return_inverse=True)
        node = bs.node_name(sec)
        me = bpy.data.meshes.new(node)
        me.from_pydata(Vl[used].tolist(), [], inv_idx.reshape(-1, 3).tolist())
        me.update()
        me.shade_flat()
        me.materials.append(mat)
        obj = body.copy()
        obj.data = me
        obj.name = node
        obj['ce_role'] = 'section'
        obj['os_section'] = sec
        for c in body.users_collection:
            c.objects.link(obj)
        w = Wc[used]
        for gi, gname in enumerate(groups):
            vg = obj.vertex_groups.get(gname) or obj.vertex_groups.new(name=gname)
            for vi in np.flatnonzero(w[:, gi] > 1e-6):
                vg.add([int(vi)], float(w[vi, gi]), 'REPLACE')
        moving = [k for k in key_names if np.abs(Dc[k][used]).max() > 1e-6]      # float noise of the source keys is not a morph
        if moving:
            obj.shape_key_add(name='Basis', from_mix=False)
            for k in moving:
                key = obj.shape_key_add(name=k, from_mix=False)
                key.data.foreach_set('co', (Vl[used] + Dc[k][used]).ravel())
                key.slider_min, key.slider_max, key.value = keys[k]['slider_min'], keys[k]['slider_max'], 0.0
        objects.append(obj)
        mass = w.sum(0)
        dominant = {engine(groups[i]): round(float(mass[i] / max(mass.sum(), 1e-12)), 3) for i in np.argsort(-mass)[:3] if mass[i] > 1e-9}
        P = cut['V'][used]
        a, b, c = (cut['V'][tri[:, i]] for i in range(3))
        part_name, band = sec.split('#')
        dist = np.linalg.norm(P - np.median(P, axis=0), axis=1)
        records.append(dict(spread_m=dict(max=round(float(dist.max()), 3), p90=round(float(np.percentile(dist, 90)), 3)), node=node, name=sec, part=part_name, band=int(band), tris=int(len(tri)), vertices=int(len(used)),
                            area_m2=round(float(np.linalg.norm(np.cross(b - a, c - a), axis=1).sum() / 2), 6),
                            follows=next(iter(dominant), None), bones=dominant, morphs=moving,
                            bbox_gltf=[[round(float(P[:, 0].min()), 5), round(float(P[:, 2].min()), 5), round(-float(P[:, 1].max()), 5)],
                                       [round(float(P[:, 0].max()), 5), round(float(P[:, 2].max()), 5), round(-float(P[:, 1].min()), 5)]]))
    return objects, records, cut['report']


# --- head frame / attach data (bases.json head_frames, attach_gltf, head_target_height equivalents) -------------------

SOCKET_BASES = ('dwarf_female', 'dwarf_male', 'orc_male', 'gnome_female')       # no neck: the head sits on the rim of the opening
SIZE_MULTIPLIER = {'orc_male': 1.30, 'orc_female': 1.12}                       # scripts/heads/build_race_heads.py (visual corrections)


def gltf_vec(v):
    """Blender (x, y, z) -> glTF (x, z, -y), rounded."""
    return [round(float(v[0]), 5), round(float(v[2]), 5), round(-float(v[1]), 5)]


def seam_of(base_obj):
    """World positions of the open loop between the `bone_head.x` faces and the rest of the body, ordered round the loop; None without one."""
    me = base_obj.data
    mats = [part_of_material(m.name) if m else '' for m in me.materials]
    edge_head = {}
    for p in me.polygons:
        for key in p.edge_keys:
            edge_head.setdefault(key, set()).add(mats[p.material_index] == 'head.x')
    ids = {v for key, flags in edge_head.items() if flags == {True, False} for v in key}
    if not ids:
        return None
    mw = base_obj.matrix_world
    pts = [mw @ me.vertices[i].co for i in sorted(ids)]
    c = sum(pts, Vector()) / len(pts)
    pts.sort(key=lambda p: math.atan2(p.x - c.x, -(p.y - c.y)))
    return pts


def head_block(base_name, rig, base_obj, head_obj):
    """Head frame and attach data measured on this base, in the units of heads/bases.json (glTF model space, rest pose), plus the
    default head mesh in the Head-bone frame (what the head export aligns the library heads to)."""
    hb = rig.matrix_world @ rig.data.bones['head.x'].head_local
    me = head_obj.data
    me.calc_loop_triangles()
    verts = np.array([list(head_obj.matrix_world @ v.co) for v in me.vertices]) - np.array(hb)
    tris = np.array([list(t.vertices) for t in me.loop_triangles], dtype=np.int64)
    ext = verts.max(0) - verts.min(0)
    head_size = float((ext[1] * ext[2]) ** 0.5)               # geometric mean of depth and height (scripts/heads/head_fit.head_size)
    crown = float(verts[:, 2].max() + hb.z)
    block = dict(head_frame=dict(head_bone_gltf=gltf_vec(hb), head_size=round(head_size, 5)),
                 default_head=dict(frame='Head-bone frame, glTF axes (x, y up, z forward), metres',
                                   crown=round(float(verts[:, 2].max()), 5), chin=round(float(verts[:, 2].min()), 5),
                                   front=round(float(verts[:, 1].min()), 5), back=round(float(verts[:, 1].max()), 5),
                                   width=round(float(np.ptp(verts[:, 0])), 5), tris=int(len(tris)),
                                   vertices=[gltf_vec(v) for v in verts], triangles=tris.tolist()))
    seam = seam_of(base_obj)
    if seam is None:
        block.update(attach=None, neck_seam=None, head_target_height=None)
        return block
    c = sum(seam, Vector()) / len(seam)
    r = sum(math.hypot(p.x - c.x, p.y - c.y) for p in seam) / len(seam)
    zs = sorted(p.z for p in seam)
    if base_name in SOCKET_BASES:
        mode = 'socket'
        upper = [p for p in seam if p.z >= zs[len(zs) // 2]]
        anchor = c.lerp(sum(upper, Vector()) / len(upper), 0.5)
        anchor.x = 0.0
        falloff = min(0.16, 0.35 * (zs[-1] - zs[0]) + 0.05)
    else:
        mode = 'neck'
        anchor = Vector((0.0, c.y, c.z))
        falloff = max(0.03, 0.9 * r)
    target = crown - anchor.z
    block.update(attach=dict(mode=mode, anchor=gltf_vec(anchor), falloff=round(falloff, 4), seam_radius=round(r, 4), target_height=round(target, 4)),
                 neck_seam=dict(centre=gltf_vec(c), radius=round(r, 4), vertices=len(seam), ring=[gltf_vec(p) for p in seam]),
                 head_target_height=round(round(target, 3) * SIZE_MULTIPLIER.get(base_name, 1.0) * 0.93, 4))
    return block


def ap_inventory(joints):
    """{group: count} of the `AP_<group>_<NN>` surface points among the skin joints."""
    groups = {}
    for j in joints:
        if j.startswith('AP_'):
            stem = j[3:]
            head, _, tail = stem.rpartition('_')
            group = head if tail.isdigit() and head else stem
            groups[group] = groups.get(group, 0) + 1
    return dict(sorted(groups.items()))


def section_warnings(rows):
    """Sections with stray vertices: one vertex further than 0.25 m from the section's median AND more than 3x the 90th percentile distance
    (a foot triangle labelled as arm made `sec_arm_stretch_*_1` of elf_female 1.3 m long)."""
    out = []
    for r in rows:
        lo, hi = np.array(r['bbox_gltf'][0]), np.array(r['bbox_gltf'][1])
        r['extent_m'] = round(float(np.linalg.norm(hi - lo)), 3)
        m, p90 = r['spread_m']['max'], r['spread_m']['p90']
        if m > 0.25 and m > 3 * max(p90, 1e-3):
            out.append(f"{r['node']}: a vertex {m:.2f} m from the section median (90% within {p90:.2f} m): stray vertices?")
    return out


def glb_meshes(doc):
    """[{node, skinned, primitives, materials, targets, tris}] of every mesh node of a GLB json."""
    rows = []
    for node in doc['nodes']:
        if 'mesh' not in node:
            continue
        mesh = doc['meshes'][node['mesh']]
        prims = mesh['primitives']
        rows.append(dict(node=node['name'], skinned='skin' in node, primitives=len(prims),
                         materials=sorted({doc['materials'][p['material']]['name'] for p in prims if 'material' in p}),
                         targets=list(mesh.get('extras', {}).get('targetNames', [])),
                         tris=sum(doc['accessors'][p['indices']]['count'] // 3 for p in prims if 'indices' in p)))
    return rows


def export_base(base_name, out_dir, project=None, layout='fine'):
    """Write out_dir/base.glb + base.json from the open session (never saves the .blend); returns a summary dict."""
    arp = _arp()
    ce = arp.ce
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    body = bpy.data.objects['base']
    rig = body.find_armature()
    reset_pose(rig)
    spacing = read_spacing(bpy.context.scene)
    if not any(m and part_of_material(m.name) == 'head.x' for m in body.data.materials):
        raise ValueError(f'{base_name}: the base has no bone_head.x faces (its head is a separate object); '
                         'the sectioned base export does not support this base yet')
    fixed, morphs, points, refit = prepare_session(body, rig, race=base_name)
    # the prepared session (weights repaired, morphs, AP leaves re-fitted) IS the skeleton of base.glb: keep a copy so the animation exporter
    # can open exactly this rig (same AP_* placement, same skeleton_sha256)
    prepared = out / 'prepared.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(prepared), copy=True, compress=True)
    whole, head = ce('skin').split_head(body)                         # hidden copies of `base`: [body without the head, head]
    mat = skin_material()
    assign_skin(head, mat)
    head_data = head_block(base_name, rig, body, head)
    sections, section_rows, cut_report = make_sections(whole, rig, base_name, mat, layout)
    repaired = {o.name: len(v) for o in sections for v in [arp.fix_weights([o], rig)[o.name]] if v}
    bpy.data.objects.remove(whole, do_unlink=True)
    glb = out / 'base.glb'
    audit = arp.export_glb(glb, rig, [*sections, head])
    morph_audit = ce('native_morphs').audit_glb(glb, morphs)
    doc, _ = ce('gltf').read(glb)
    joints = [doc['nodes'][i]['name'] for i in doc['skins'][0]['joints']]
    socket_map = ce('native_contract').SOCKETS
    missing = [s for s in socket_map if s not in joints]
    if missing:
        raise ValueError('Exported skeleton lacks sockets: ' + str(missing))
    if SEAT not in joints:
        raise ValueError('Exported skeleton lacks the rider Seat bone')
    seat = json.loads(rig['ostudio_seat'])
    inventory = glb_meshes(doc)
    names = {r['node'] for r in inventory}
    expect = {r['node'] for r in section_rows} | {'head'}
    if names != expect:
        raise ValueError(f'Exported mesh nodes differ from the planned sections / head: {sorted(names ^ expect)}')
    bad = [r['node'] for r in inventory if r['materials'] != ['skin'] or not r['skinned']]
    if bad:
        raise ValueError('Mesh nodes not skinned on the single `skin` material: ' + str(bad))
    by_node = {r['node']: r for r in inventory}
    warnings = section_warnings(section_rows)
    for r in section_rows:
        r['tris_glb'] = by_node[r['node']]['tris']
        r['morph_targets_glb'] = by_node[r['node']]['targets']
    project = Path(project or bpy.data.filepath)
    record = dict(version=2, base=base_name, skeleton_sha256=audit['skeleton_sha256'], bones=audit['bones'],
                  joints=joints, sockets=[dict(name=n, parent=p) for n, p in socket_map.items()] + [dict(name=SEAT, parent='Hips', kind='rider_seat')],
                  seat=seat,
                  attachments=points, ap_groups=ap_inventory(joints), morphs=morphs,
                  base_body=None, body_mode='sections', default_head='head', skin_material='skin',
                  sections=section_rows, section_layout=layout, section_report=cut_report, warnings=warnings,
                  skeleton_inputs=dict(
                      prepared_blend='prepared.blend', skeleton_json='skeleton.json',
                      function='outfit_studio.blender.export_base.prepare_session(body, rig)',
                      steps=['open_base(project): reset_pose', 'skin.normalize_export_weights on `base` (top 4 + normalise; %d vertices repaired)' % len(fixed.get('base', [])),
                             'ensure_morphs: head_* / bulk_* shape keys', 'native_attachment_restore.bindings; on a changed dominant bone refit_authored '
                             '(AP_* leaves re-projected onto the repaired body and re-parented) then bindings again'],
                      attachments_refit=bool(refit)),
                  head=head_data, mesh_nodes=inventory,
                  legless=bool(audit['legless']), spacing=dict(arm_deg=spacing['arm_deg'], leg_cm=spacing['leg_cm'], overrides=spacing.get('overrides', {})),
                  source=dict(project=str(project.relative_to(ROOT)) if ROOT in project.parents else str(project),
                              sha256=sha256(project) if project.is_file() else None,
                              rig_fingerprint=rig_fingerprint(rig)),
                  glb_sha256=sha256(glb), export_env=env_versions(),
                  weights_repaired=dict(base=len(fixed.get('base', [])), sections=repaired), attachments_refit=refit,
                  morph_max_displacement={m['mesh']: m['max_displacement'] for m in morph_audit['meshes']})
    dump_json(out / 'base.json', record)
    dump_json(out / 'skeleton.json', dict(base=base_name, skeleton_sha256=audit['skeleton_sha256'], bones=audit['bones'], prepared_blend='prepared.blend',
                                          rig_fingerprint=record['source']['rig_fingerprint'], project_sha256=record['source']['sha256'],
                                          joints=audit['bones_rest']))
    return dict(base=base_name, glb=str(glb), json=str(out / 'base.json'), skeleton_sha256=record['skeleton_sha256'],
                bones=record['bones'], aps=len(points), sockets=len(record['sockets']), morphs=len(morphs),
                sections=len(section_rows), section_tris=sum(r['tris'] for r in section_rows), head_tris=by_node['head']['tris'],
                plane_fallbacks=cut_report['plane_fallbacks'], stray_labels_fixed=cut_report['stray_labels_fixed'], warnings=warnings, legless=record['legless'],
                repaired_vertices=len(fixed.get('base', [])), refit=bool(refit))


def spawn(blend, script, argv, log, extra=()):
    """Start `blender -b blend --python script -- argv` detached from this session; returns the Popen."""
    exe = bpy.app.binary_path if bpy.app.binary_path else 'blender'
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    stream = open(log, 'ab')
    return subprocess.Popen([exe, '-b', '-noaudio', str(blend), *extra, '--python-exit-code', '3', '--python', str(script), '--', *map(str, argv)],
                            stdout=stream, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)


def default_out(base_name, batch=None):
    return EXPORT / (batch or time.strftime('%Y%m%d') + '-v1') / 'bases' / base_name


def main(argv=None):
    """Headless entry: `-- --out <dir> [--base <name>]` on the already opened base file."""
    import argparse
    args = sys.argv[sys.argv.index('--') + 1:] if argv is None and '--' in sys.argv else (argv or [])
    p = argparse.ArgumentParser()
    p.add_argument('--base')
    p.add_argument('--out', required=True)
    p.add_argument('--layout', default='fine', choices=('fine', 'coarse'))
    a = p.parse_args(args)
    base = a.base or Path(bpy.data.filepath).stem
    open_base()
    result = export_base(base, a.out, layout=a.layout)
    dump_json(Path(a.out) / '.export_base.done', result)
    print('EXPORT_BASE', json.dumps(result), flush=True)
    return result


# --- operator -----------------------------------------------------------------------------------

class OSTUDIO_OT_export_base(bpy.types.Operator):
    bl_idname = 'ostudio.export_base'
    bl_label = 'Export base'
    bl_description = ('Export base.glb + base.json from the saved base project in a background Blender '
                      '(this session is not modified; unsaved changes are not included)')
    batch: bpy.props.StringProperty(name='Batch', description='Export batch folder (empty = today, v1)')
    overwrite: bpy.props.BoolProperty(name='Overwrite', default=False)
    _proc = None
    _timer = None
    _out = None

    @classmethod
    def poll(cls, context):
        return bool(bpy.data.filepath) and 'base' in bpy.data.objects

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        base = Path(bpy.data.filepath).stem
        out = default_out(base, self.batch or None)
        if (out / 'base.json').exists() and not self.overwrite:
            self.report({'WARNING'}, f'{out} already has a base export (tick Overwrite)')
            return {'CANCELLED'}
        if bpy.data.is_dirty:
            self.report({'WARNING'}, 'File has unsaved changes; exporting the saved version')
        (out / '.export_base.done').unlink(missing_ok=True)
        self._out = out
        self._proc = spawn(bpy.data.filepath, Path(__file__).resolve(), ['--base', base, '--out', out], out / 'export_base.log')
        self._timer = context.window_manager.event_timer_add(1.0, window=context.window)
        context.window_manager.modal_handler_add(self)
        self.report({'INFO'}, f'Exporting {base} to {out} ...')
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        code = self._proc.poll()
        if code is None:
            return {'PASS_THROUGH'}
        context.window_manager.event_timer_remove(self._timer)
        done = self._out / '.export_base.done'
        if code == 0 and done.exists():
            r = json.loads(done.read_text())
            self.report({'INFO'}, f"Base exported: {r['bones']} bones, {r['aps']} AP, sha {r['skeleton_sha256'][:10]}")
            return {'FINISHED'}
        self.report({'ERROR'}, f'Base export failed (exit {code}); see {self._out / "export_base.log"}')
        return {'CANCELLED'}


CLASSES = (OSTUDIO_OT_export_base,)


def draw(layout, context):
    box = layout.box()
    box.label(text='Export base')
    row = box.row()
    row.enabled = bool(bpy.data.filepath)
    row.operator('ostudio.export_base', icon='EXPORT')
    if not bpy.data.filepath or bpy.data.is_dirty:
        box.label(text='Save the file first: the saved copy is exported.', icon='INFO')


if __name__ == '__main__':
    main()
