"""Rebuild the bottom of a game base body as the convex hull of its own vertices (2026-11-24, user: "try to fix all these issues yourself ...
try multiple things if necessary"). Made for gnome_female, whose legless bottom is open (two leg notches at the front-bottom corners) and built
from overlapping, inconsistently wound pieces that can not be stitched edge by edge (see tools/fix_base_holes.py for the other bodies).

    blender -b projects/<race>.blend --factory-startup -P tools/rebuild_base_bottom.py -- --zcut 0.105 [--apply] [--dump out.npz] [--report out.json]

Every face with a vertex below --zcut is deleted; the vertices that leave plus the ring of vertices the cut leaves open are wrapped in their convex
hull (bmesh convex_hull on the existing vertices: no new vertex, so shape keys and weights are untouched, and the lowest vertex - the o/H frame of
tools/preview/export_bases.py - stays because a hull keeps its extreme points); the hull's lid over the ring is dropped, and the vertices the hull
does not use are deleted. The hull fills concavities below the cut (the leg notches) and keeps the convex shape as it was. New faces take the
material and UVs of the nearest deleted face. With --apply the .blend is first copied to projects/<race>.backup_20261124_before_bottom_rebuild.blend
and nothing is saved if the bounding box or the shape-key count would change, or if the result has open or non-manifold edges near the cut."""
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


ZCUT = float(opt('--zcut', 0.105))
LID_NZ = 0.5
APPLY = '--apply' in argv
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


def bbox():
    return [[round(min(v.co[i] for v in bm.verts), 5) for i in range(3)], [round(max(v.co[i] for v in bm.verts), 5) for i in range(3)]]


rep = {'race': blend.stem, 'zcut': ZCUT, 'before': stats(), 'bbox_before': bbox()}
region = [f for f in bm.faces if any(v.co.z < ZCUT for v in f.verts)]
rverts = {v for f in region for v in f.verts}
old = [(f.calc_center_median().copy(), f.material_index, f.normal.copy(), f.calc_area()) for f in region]
uvs = {}
if uv is not None:
    for v in rverts:
        for l in v.link_loops:
            uvs[v] = l[uv].uv.copy()
            break
bmesh.ops.delete(bm, geom=region, context='FACES_ONLY')
ring = {v for v in rverts if v.is_valid and v.link_faces}
rep['deleted_faces'] = len(region)
rep['ring_vertices'] = len(ring)
rep['ring_z'] = [round(min(v.co.z for v in ring), 4), round(max(v.co.z for v in ring), 4)] if ring else None
pts = [v for v in rverts if v.is_valid]
hull = bmesh.ops.convex_hull(bm, input=pts, use_existing_faces=False)
hf = [g for g in hull['geom'] if isinstance(g, bmesh.types.BMFace)]
bm.normal_update()
lid = [f for f in hf if all(v in ring for v in f.verts) and f.normal.z > LID_NZ]
bmesh.ops.delete(bm, geom=lid, context='FACES_ONLY')
hf = [f for f in hf if f.is_valid]
# hull faces that repeat an existing face (same vertices) are dropped
keys = {}
dup = []
for f in bm.faces:
    k = frozenset(f.verts)
    if k in keys:
        dup.append(f if f in hf else keys[k])
    else:
        keys[k] = f
if dup:
    bmesh.ops.delete(bm, geom=list(set(dup)), context='FACES_ONLY')
hf = [f for f in hf if f.is_valid]
loose = [v for v in bm.verts if not v.link_faces]
rep['hull_faces'] = len(hf)
rep['lid_faces_dropped'] = len(lid)
rep['vertices_not_on_hull_deleted'] = len(loose)
rep['ring_vertices_not_on_hull'] = [[round(c, 4) for c in v.co] for v in loose if v in ring]
if loose:
    bmesh.ops.delete(bm, geom=loose, context='VERTS')
# the hull's faces wind outward of the hull; make sure they agree with the faces above the ring
agree = disagree = 0
hs = set(hf)
for f in hf:
    for l in f.loops:
        for l2 in l.edge.link_loops:
            if l2.face not in hs:
                if l2.vert == l.vert:
                    disagree += 1
                else:
                    agree += 1
if disagree > agree:
    for f in hf:
        f.normal_flip()
rep['winding_against_ring'] = {'agree': agree, 'disagree': disagree, 'flipped': disagree > agree}
for f in hf:
    c = f.calc_center_median()
    # material (= body part): of the deleted faces facing the same way (outer skin, not the hidden inner faces of the notches), the nearest
    same = [o for o in old if o[2].dot(f.normal) > 0.3] or old
    f.material_index = same[int(np.argmin([(c - o[0]).length for o in same]))][1]
    if uv is not None:
        for l in f.loops:
            if l.vert in uvs:
                l[uv].uv = uvs[l.vert]
bm.normal_update()
from collections import Counter
mats = [m.name if m else '' for m in me.materials]
rep['new_face_materials'] = dict(Counter(mats[f.material_index] for f in hf))
rep['deleted_face_materials'] = dict(Counter(mats[o[1]] for o in old))
rep['after'] = stats()
rep['bbox_after'] = bbox()
rep['open_after_at'] = [[round(c, 4) for c in (e.verts[0].co + e.verts[1].co) / 2] for e in bm.edges if len(e.link_faces) == 1][:20]
rep['nonmanifold_after_at'] = [[round(c, 4) for c in (e.verts[0].co + e.verts[1].co) / 2] for e in bm.edges if len(e.link_faces) > 2][:20]
if opt('--dump'):
    tmp = bm.copy()
    bmesh.ops.triangulate(tmp, faces=tmp.faces[:])
    np.savez(opt('--dump'), V=np.array([list(ob.matrix_world @ v.co) for v in tmp.verts]), F=np.array([[v.index for v in f.verts] for f in tmp.faces]),
             P=np.array([f.material_index for f in tmp.faces]))
bad_near_cut = [p for p in rep['open_after_at'] + rep['nonmanifold_after_at'] if p[2] < ZCUT + 0.05]
rep['problems_near_cut'] = bad_near_cut
if APPLY:
    if rep['bbox_after'] != rep['bbox_before']:
        raise SystemExit(f"bounding box changed {rep['bbox_before']} -> {rep['bbox_after']}: not saved (o/H would move)")
    if bad_near_cut:
        raise SystemExit(f'open / non-manifold edges left near the cut {bad_near_cut}: not saved')
    backup = blend.with_name(f'{blend.stem}.backup_20261124_before_bottom_rebuild.blend')
    if not backup.exists():
        shutil.copy2(blend, backup)
    rep['backup'] = str(backup)
    nsk = len(me.shape_keys.key_blocks) if me.shape_keys else 0
    bm.to_mesh(me)
    me.update()
    assert (len(me.shape_keys.key_blocks) if me.shape_keys else 0) == nsk
    bpy.ops.wm.save_mainfile()
rep['applied'] = APPLY
print('BOTTOMREPORT', json.dumps(rep))
if opt('--report'):
    Path(opt('--report')).write_text(json.dumps(rep, indent=1))
