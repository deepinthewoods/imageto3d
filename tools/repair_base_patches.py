"""Rebuild the faulty patches of a game base body (projects/<race>.blend, mesh `base`) (2026-11-24, user: "try to fix all these issues
yourself ... try multiple things if necessary"). Follows tools/fix_base_holes.py, which closed the plain holes and removed loose pieces; what is
left are overlapping / doubled faces (a V-neck collar flap, centre-line pockets) and the gnome's open bottom.

    blender -b projects/<race>.blend --factory-startup -P tools/repair_base_patches.py -- [--apply] [--rings N] [--fill beauty|fan] [--dump out.npz]
        [--report out.json] [--region x,y,z,r_mm]

A problem vertex lies on an open edge or on an edge shared by 3+ faces. Every face touching a problem vertex (grown by --rings more rings) is
deleted, together with the vertices left without faces; each hole that leaves is filled again from its own boundary vertices:
  beauty  triangle fill (Blender's triangle_fill with beauty), no new vertex;
  fan     one face over the hole, poked: one new vertex at the centre, its shape keys / weights interpolated from the boundary (bmesh poke).
New faces take the material (body part) of the nearest deleted face and the UVs of their vertices; their winding is matched to the neighbours.
With --apply the .blend is first copied to projects/<race>.backup_20261124_before_patch_repair.blend (only if no such backup exists)."""
import json
import shutil
import sys
from pathlib import Path

import bmesh
import bpy
import numpy as np
from mathutils import Vector

argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []


def opt(name, default=None):
    return argv[argv.index(name) + 1] if name in argv else default


APPLY = '--apply' in argv
RINGS = int(opt('--rings', 0))
FILL = opt('--fill', 'beauty')
KEEP_MAX_MM = 40.0
DUMP = opt('--dump')
REPORT = opt('--report')
REGION = [float(x) for x in opt('--region').split(',')] if opt('--region') else None
KEEP_BBOX = '--keep-bbox' in argv
blend = Path(bpy.data.filepath)
ob = bpy.data.objects['base']
if ob.mode != 'OBJECT':                        # a .blend saved in Edit Mode: leave it first (to_mesh refuses an edit-mode mesh)
    bpy.context.view_layer.objects.active = ob
    bpy.ops.object.mode_set(mode='OBJECT')
me = ob.data
bm = bmesh.new()
bm.from_mesh(me)
uv = bm.loops.layers.uv.active


def stats():
    return {'verts': len(bm.verts), 'faces': len(bm.faces), 'open_edges': sum(1 for e in bm.edges if len(e.link_faces) == 1),
            'nonmanifold_edges': sum(1 for e in bm.edges if len(e.link_faces) > 2)}


rep = {'race': blend.stem, 'rings': RINGS, 'fill': FILL, 'before': stats(), 'patches': []}


def bbox():
    return [[round(min(v.co[i] for v in bm.verts), 5) for i in range(3)], [round(max(v.co[i] for v in bm.verts), 5) for i in range(3)]]


rep['bbox_before'] = bbox()
BB = rep['bbox_before']
bad_e = [e for e in bm.edges if len(e.link_faces) != 2]
pv = {v for e in bad_e for v in e.verts}
if REGION:
    c = Vector(REGION[:3])
    pv = {v for v in pv if (v.co - c).length * 1000 <= REGION[3]}
region = {f for v in pv for f in v.link_faces}
for _ in range(RINGS):
    region |= {f2 for f in region for v in f.verts for f2 in v.link_faces}
# patches = connected groups of region faces
region = list(region)
seen, patches = set(), []
for f in region:
    if f in seen:
        continue
    comp, stack = [], [f]
    while stack:
        x = stack.pop()
        if x in seen:
            continue
        seen.add(x)
        comp.append(x)
        for v in x.verts:
            stack += [y for y in v.link_faces if y in set(region) and y not in seen]
    patches.append(comp)
for comp in patches:
    cen = [f.calc_center_median().copy() for f in comp]
    mats = [f.material_index for f in comp]
    nrm = sum((f.normal * f.calc_area() for f in comp), Vector())
    vs = {v for f in comp for v in f.verts}
    uvs = {}
    if uv is not None:
        for v in vs:
            for l in v.link_loops:
                if l.face not in comp:
                    uvs[v] = l[uv].uv.copy()
                    break
    bmesh.ops.delete(bm, geom=comp, context='FACES_ONLY')
    loose = [v for v in vs if v.is_valid and not v.link_faces]
    # --keep-bbox: a bounding-box extreme (export_bases.py takes the o/H frame from the box) that only loose vertices still reach is kept: the
    # loose vertex nearest the symmetry plane is stitched into the new fill below (the fill is poked and its centre welded onto it)
    keep = []
    if KEEP_BBOX:
        others = [v for v in bm.verts if v.link_faces]
        for ax in range(3):
            for side, ext in ((0, min), (1, max)):
                val = BB[side][ax]
                if any(abs(v.co[ax] - val) < 1e-5 for v in others):
                    continue
                cand = [v for v in loose if abs(v.co[ax] - val) < 1e-5]
                if cand:
                    keep.append(min(cand, key=lambda v: abs(v.co.x)))
        keep = list(dict.fromkeys(keep))
        loose = [v for v in loose if v not in keep]
    nloose = len(loose)
    if loose:
        bmesh.ops.delete(bm, geom=loose, context='VERTS')
    # the holes this left: open edges among the patch's remaining vertices
    rem = {v for v in vs if v.is_valid}
    hole_e = [e for e in bm.edges if len(e.link_faces) == 1 and e.verts[0] in rem and e.verts[1] in rem]
    # walk loops
    loops, used = [], set()
    for e0 in hole_e:
        if e0 in used:
            continue
        f = e0.link_faces[0]
        lp = next(l for l in f.loops if l.edge == e0)
        start, v, chain, e = lp.vert, lp.link_loop_next.vert, [lp.vert], e0
        ok = True
        while True:
            used.add(e)
            if v == start:
                break
            chain.append(v)
            nx = [x for x in v.link_edges if x in hole_e and x not in used]
            if len(nx) != 1:
                ok = False
                break
            e = nx[0]
            v = e.other_vert(v)
        if ok and len(chain) >= 3:
            loops.append(chain)
    info = {'faces_deleted': len(comp), 'vertices_deleted': nloose, 'centre': [round(x, 4) for x in sum(cen, Vector()) / len(cen)], 'holes': []}
    for lp in loops:
        new = []
        try:
            if FILL == 'fan' and len(lp) > 4:
                f0 = bm.faces.new(list(reversed(lp)))
                new = bmesh.ops.poke(bm, faces=[f0])['faces']
            elif len(lp) <= 4:
                new = [bm.faces.new(list(reversed(lp)))]
                if len(lp) == 4:
                    new = bmesh.ops.triangulate(bm, faces=new, quad_method='BEAUTY')['faces']
            else:
                edges = [bm.edges.get((lp[i], lp[(i + 1) % len(lp)])) for i in range(len(lp))]
                res = bmesh.ops.triangle_fill(bm, use_beauty=True, use_dissolve=False, edges=edges, normal=nrm.normalized() if nrm.length else Vector((0, 0, 1)))
                new = [g for g in res['geom'] if isinstance(g, bmesh.types.BMFace)]
        except ValueError as error:
            info['holes'].append({'sides': len(lp), 'error': str(error)})
            continue
        newset = set(new)
        agree = disagree = 0
        for f in new:
            for l in f.loops:
                for l2 in l.edge.link_loops:
                    if l2.face not in newset:
                        if l2.vert == l.vert:
                            disagree += 1
                        else:
                            agree += 1
        if disagree > agree:
            for f in new:
                f.normal_flip()
        for f in new:
            c = f.calc_center_median()
            f.material_index = mats[int(np.argmin([(c - c0).length for c0 in cen]))]
            if uv is not None:
                for l in f.loops:
                    if l.vert in uvs:
                        l[uv].uv = uvs[l.vert]
        info['holes'].append({'sides': len(lp), 'faces_added': len(new), 'winding_flipped': disagree > agree})
        for kv in list(keep):
            live = [f for f in new if f.is_valid]
            if not live:
                break
            from mathutils.geometry import closest_point_on_tri
            def dist(f):
                vs3 = [v.co for v in f.verts]
                return min((closest_point_on_tri(kv.co, vs3[0], vs3[k], vs3[k + 1]) - kv.co).length for k in range(1, len(vs3) - 1))
            f = min(live, key=dist)
            d0 = dist(f)
            if d0 * 1000 > KEEP_MAX_MM:
                continue
            res = bmesh.ops.poke(bm, faces=[f])
            c = res['verts'][0]
            new = [x for x in new if x.is_valid] + list(res['faces'])
            bmesh.ops.weld_verts(bm, targetmap={c: kv})
            new = [x for x in new if x.is_valid]
            keep.remove(kv)
            info.setdefault('kept_for_bbox', []).append({'at': [round(x, 4) for x in kv.co], 'was_mm_from_fill': round(d0 * 1000, 1)})
    if keep:
        info['bbox_vertices_not_placed'] = [[round(x, 4) for x in v.co] for v in keep]
    rep['patches'].append(info)
bm.normal_update()
rep['after'] = stats()
rep['bbox_after'] = bbox()
rep['open_after_at'] = [[round(c, 4) for c in (e.verts[0].co + e.verts[1].co) / 2] for e in bm.edges if len(e.link_faces) == 1][:20]
rep['nonmanifold_after_at'] = [[round(c, 4) for c in (e.verts[0].co + e.verts[1].co) / 2] for e in bm.edges if len(e.link_faces) > 2][:20]
if DUMP:
    tmp = bm.copy()
    bmesh.ops.triangulate(tmp, faces=tmp.faces[:])
    M = ob.matrix_world
    np.savez(DUMP, V=np.array([list(M @ v.co) for v in tmp.verts]), F=np.array([[v.index for v in f.verts] for f in tmp.faces]),
             P=np.array([f.material_index for f in tmp.faces]))
if APPLY and rep['bbox_after'] != rep['bbox_before']:
    raise SystemExit(f"bounding box changed {rep['bbox_before']} -> {rep['bbox_after']}: not saved (o/H would move)")
if APPLY:
    backup = blend.with_name(f'{blend.stem}.backup_20261124_before_patch_repair.blend')
    if not backup.exists():
        shutil.copy2(blend, backup)
    rep['backup'] = str(backup)
    nsk = len(me.shape_keys.key_blocks) if me.shape_keys else 0
    bm.to_mesh(me)
    me.update()
    assert (len(me.shape_keys.key_blocks) if me.shape_keys else 0) == nsk
    bpy.ops.wm.save_mainfile()
rep['applied'] = APPLY
print('PATCHREPORT', json.dumps(rep))
if REPORT:
    Path(REPORT).write_text(json.dumps(rep, indent=1))
