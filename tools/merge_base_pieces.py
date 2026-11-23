"""Join a game base body that was modelled as separate pieces (2026-11-22, human_male): merge the doubled seam vertices and bridge the closed
end caps of stacked pieces (thigh -> shin, shin -> foot) into one connected skin. Shape keys, vertex groups (skin weights), materials (body
parts) and UVs are kept; new bridge faces copy their neighbours' UVs.

    blender -b projects/human_male.blend -P tools/merge_base_pieces.py -- [--save] [--report FILE]

Without --save nothing is written (dry run: the report only). Steps on mesh `base`:
  1. coincident vertices (< 0.01 mm): their shape-key positions and skin weights are averaged, then they are merged;
  2. per pair of pieces that meet end to end (a smaller piece's end cap facing a cap of the piece above it within CAP_MM, normals opposite):
     both caps are deleted; rings lying on top of each other (>= 80 % of the vertices pair up within WELD_MM) are welded (shape keys and weights
     averaged, the merged vertex at the midpoint) and the leftover gap filled, other rings are bridged; loose vertices are removed;
  3. faces with identical vertex sets (internal walls left by step 1) are removed in pairs.
The report lists pieces, merged vertices, caps, bridges and the open / non-manifold edges before and after.
"""
import json
import sys

import bmesh
import bpy
from mathutils.bvhtree import BVHTree

argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
SAVE = '--save' in argv
REPORT = argv[argv.index('--report') + 1] if '--report' in argv else None
MERGE_DIST = 1e-5
CAP_MM = 20.0
WELD_MM = 8.0        # end rings whose vertices pair up within this are welded instead of bridged

ob = bpy.data.objects['base']
me = ob.data
mats = [m.name if m else '' for m in me.materials]
rep = {'file': bpy.data.filepath, 'save': SAVE}
bm = bmesh.new()
bm.from_mesh(me)
shape = [bm.verts.layers.shape[k] for k in bm.verts.layers.shape.keys()]
deform = bm.verts.layers.deform.active
uv = bm.loops.layers.uv.active


def edge_stats():
    return {'open_edges': sum(1 for e in bm.edges if len(e.link_faces) == 1), 'nonmanifold_edges': sum(1 for e in bm.edges if len(e.link_faces) > 2),
            'wire_edges': sum(1 for e in bm.edges if not e.link_faces), 'loose_verts': sum(1 for v in bm.verts if not v.link_faces)}


def pieces():
    seen, out = set(), []
    for f in bm.faces:
        if f in seen:
            continue
        comp, st = [], [f]
        seen.add(f)
        while st:
            x = st.pop()
            comp.append(x)
            for e in x.edges:
                for y in e.link_faces:
                    if y not in seen:
                        seen.add(y)
                        st.append(y)
        out.append(comp)
    return sorted(out, key=len, reverse=True)


rep['before'] = {'verts': len(bm.verts), 'faces': len(bm.faces), **edge_stats()}

# 1. coincident vertices: average shape keys + weights, then merge
groups = {}
for v in bm.verts:
    groups.setdefault(tuple(round(c / MERGE_DIST) for c in v.co), []).append(v)
groups = [g for g in groups.values() if len(g) > 1]
averaged = 0
for g in groups:
    for sl in shape:
        mean = sum((v[sl] for v in g), g[0][sl] * 0) / len(g)
        if any((v[sl] - mean).length > 1e-6 for v in g):
            averaged += 1
        for v in g:
            v[sl] = mean.copy()
    if deform is not None:
        keys = set()
        for v in g:
            keys |= set(v[deform].keys())
        w = {k: sum(v[deform].get(k, 0.) for v in g) / len(g) for k in keys}
        for v in g:
            dv = v[deform]
            dv.clear()
            for k, x in w.items():
                if x > 0:
                    dv[k] = x
nv = len(bm.verts)
bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=MERGE_DIST)
rep['merge'] = {'groups': len(groups), 'vertices_removed': nv - len(bm.verts), 'shape_key_values_averaged': averaged}
bm.normal_update()
P = pieces()
rep['pieces_after_merge'] = [{'faces': len(c), 'parts': sorted({mats[f.material_index] for f in c})} for c in P]


def bvh(faces):
    vs = sorted({v for f in faces for v in f.verts}, key=lambda v: v.index)
    ix = {v: i for i, v in enumerate(vs)}
    return BVHTree.FromPolygons([v.co.copy() for v in vs], [[ix[v] for v in f.verts] for f in faces])


def caps(A, B):
    t = bvh(B)
    out = []
    for f in A:
        hit = t.find_nearest(f.calc_center_median())
        if hit[0] is None:
            continue
        vd = max(t.find_nearest(v.co)[3] for v in f.verts)
        if vd <= CAP_MM / 1000 and f.normal.dot(hit[1]) < -.5:
            out.append(f)
    return out


def average_into(g):
    """Give every vertex of g the mean shape-key positions and skin weights of the group."""
    for sl in shape:
        mean = sum((v[sl] for v in g), g[0][sl] * 0) / len(g)
        for v in g:
            v[sl] = mean.copy()
    if deform is not None:
        keys = set()
        for v in g:
            keys |= set(v[deform].keys())
        w = {k: sum(v[deform].get(k, 0.) for v in g) / len(g) for k in keys}
        for v in g:
            dv = v[deform]
            dv.clear()
            for k, x in w.items():
                if x > 0:
                    dv[k] = x


def coincident_pairs(ring_a, ring_b, tol=WELD_MM / 1000):
    """One-to-one (a, b) pairs of ring vertices closer than tol, nearest first."""
    cand = sorted(((a.co - b.co).length, i, j) for i, a in enumerate(ring_a) for j, b in enumerate(ring_b))
    ua, ub, out = set(), set(), []
    for i, a in enumerate(ring_a):                    # a vertex both rings already share (merged in step 1) is never welded to anything
        for j, b in enumerate(ring_b):
            if a is b:
                ua.add(i)
                ub.add(j)
    for d, i, j in cand:
        if d > tol:
            break
        if i in ua or j in ub:
            continue
        ua.add(i)
        ub.add(j)
        out.append((ring_a[i], ring_b[j]))
    return out


def boundary_edges(fs):
    s = set(fs)
    return list({e for f in fs for e in f.edges if sum(1 for g in e.link_faces if g in s) == 1})


# 2. bridge stacked pieces, smallest first onto the piece that its caps face
bridges = []
for _ in range(8):
    bm.normal_update()
    P = pieces()
    if len(P) == 1:
        break
    done = False
    for small in P[:0:-1]:
        for big in P:
            if big is small:
                continue
            ca, cb = caps(small, big), caps(big, small)
            if not ca or not cb:
                continue
            la, lb = boundary_edges(ca), boundary_edges(cb)
            parts = sorted({mats[f.material_index] for f in ca + cb})
            # remember UVs per vertex to give the bridge faces their neighbours' UVs
            uvs = {}
            if uv is not None:
                for f in ca + cb:
                    for l in f.loops:
                        uvs.setdefault(l.vert, []).append(l[uv].uv.copy())
            ring_a = {v for e in la for v in e.verts}
            ring_b = {v for e in lb for v in e.verts}
            bmesh.ops.delete(bm, geom=ca + cb, context='FACES')        # faces + their now unused inner edges / vertices (ring edges keep their wall faces)
            la = [e for e in la if e.is_valid]
            lb = [e for e in lb if e.is_valid]
            ring_a = [v for v in ring_a if v.is_valid and v.link_faces]
            ring_b = [v for v in ring_b if v.is_valid and v.link_faces]
            pairs = coincident_pairs(ring_a, ring_b)
            if len(pairs) >= .8 * min(len(ring_a), len(ring_b)):
                mode = 'weld'                     # the two rings lie on top of each other (knees): weld the matching vertices, fill what is left
                for va, vb in pairs:
                    average_into([va, vb])
                    mid = (va.co + vb.co) / 2
                    va.co, vb.co = mid.copy(), mid.copy()
                bmesh.ops.weld_verts(bm, targetmap={va: vb for va, vb in pairs})
                ring = {vb for _, vb in pairs} | {v for v in ring_b if v.is_valid} | {v for v in ring_a if v.is_valid}
                hole = [e for e in bm.edges if len(e.link_faces) == 1 and all(v in ring for v in e.verts)]
                res = bmesh.ops.holes_fill(bm, edges=hole, sides=0) if hole else {'faces': []}
                new = res['faces']
                for f in new:
                    nb = [g for e in f.edges for g in e.link_faces if g is not f]
                    if nb:
                        f.material_index = nb[0].material_index
            else:
                mode = 'bridge'                   # rings a few mm to cm apart (ankles): bridge them
                res = bmesh.ops.bridge_loops(bm, edges=la + lb)
                new = res['faces']
            if uv is not None:
                for f in new:
                    for l in f.loops:
                        other = [x[uv].uv for x in l.vert.link_loops if x.face not in new]
                        if other:
                            l[uv].uv = sum(other, other[0] * 0) / len(other)
                        elif l.vert in uvs:
                            l[uv].uv = uvs[l.vert][0]
            loose = [v for v in bm.verts if not v.link_faces]
            bmesh.ops.delete(bm, geom=loose, context='VERTS')
            mat_of_new = sorted({mats[f.material_index] for f in new})
            bridges.append({'mode': mode, 'welded_pairs': len(pairs), 'bridge_face_parts': mat_of_new, 'small_piece_faces': len(small), 'into_piece_faces': len(big), 'caps_deleted': [len(ca), len(cb)], 'ring_edges': [len(la), len(lb)],
                            'ring_verts': [len(ring_a), len(ring_b)], 'bridge_faces': len(new), 'loose_removed': len(loose), 'parts': parts})
            done = True
            break
        if done:
            break
    if not done:
        break
rep['bridges'] = bridges

# 3. internal walls: faces with identical vertex sets
sig = {}
for f in bm.faces:
    sig.setdefault(frozenset(v.index for v in f.verts), []).append(f)
dups = [fs for fs in sig.values() if len(fs) > 1]
rep['duplicate_face_sets'] = len(dups)
bmesh.ops.delete(bm, geom=[f for fs in dups for f in fs], context='FACES_ONLY') if dups else None
bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context='VERTS')


# 4. leftovers of the pieces that were already partly merged in the source (internal fins / doubled faces at the collar, facing wrist caps):
#    remove, one at a time, the face on a non-manifold edge whose removal lowers (non-manifold + open) edges the most; then fill small holes.
def defect_count():
    return sum(1 for e in bm.edges if len(e.link_faces) > 2) + sum(1 for e in bm.edges if len(e.link_faces) == 1)


def removal_gain(f):
    d = 0
    for e in f.edges:
        n = len(e.link_faces)
        before = (n > 2) + (n == 1)
        after = ((n - 1) > 2) + ((n - 1) == 1)
        d += after - before
    return d


def removal_gain_set(fs):
    cnt = {}
    for f in fs:
        for e in f.edges:
            cnt[e] = cnt.get(e, 0) + 1
    d = 0
    for e, k in cnt.items():
        n = len(e.link_faces)
        d += (((n - k) > 2) + ((n - k) == 1)) - ((n > 2) + (n == 1))
    return d


removed_faces = []
for _ in range(40):
    nm_edges = [e for e in bm.edges if len(e.link_faces) > 2]
    if not nm_edges:
        break
    cands = [(f,) for f in {f for e in nm_edges for f in e.link_faces}]
    for e in nm_edges:                               # facing pairs on one edge (two end caps against each other)
        fs = list(e.link_faces)
        cands += [(f, g) for i, f in enumerate(fs) for g in fs[i + 1:] if f.normal.dot(g.normal) < -.7]
    best = min(cands, key=lambda fs: (removal_gain_set(fs), sum(f.calc_area() for f in fs)))
    if removal_gain_set(best) >= 0:
        break
    for f in best:
        removed_faces.append({'part': mats[f.material_index], 'area_cm2': round(f.calc_area() * 1e4, 2), 'gain': removal_gain_set(best), 'with': len(best),
                              'centre': [round(c, 3) for c in f.calc_center_median()]})
    bmesh.ops.delete(bm, geom=list(best), context='FACES')
holes = [e for e in bm.edges if len(e.link_faces) == 1]
filled = bmesh.ops.holes_fill(bm, edges=holes, sides=6)['faces'] if holes else []
for f in filled:
    nb = [g for e in f.edges for g in e.link_faces if g is not f]
    if nb:
        f.material_index = nb[0].material_index
    if uv is not None:
        for l in f.loops:
            other = [x[uv].uv for x in l.vert.link_loops if x.face is not f]
            if other:
                l[uv].uv = sum(other, other[0] * 0) / len(other)
rep['cleanup'] = {'removed_faces': removed_faces, 'holes_filled': len(filled), 'defects_left': defect_count()}
bm.normal_update()
P = pieces()
nm = [e for e in bm.edges if len(e.link_faces) > 2]
op = [e for e in bm.edges if len(e.link_faces) == 1]
rep['after'] = {'verts': len(bm.verts), 'faces': len(bm.faces), 'pieces': len(P), **edge_stats(),
                'open_edge_parts': sorted({mats[f.material_index] for e in op for f in e.link_faces}),
                'nonmanifold_edge_parts': sorted({mats[f.material_index] for e in nm for f in e.link_faces}),
                'nonmanifold_at': [[round(c, 3) for c in (e.verts[0].co + e.verts[1].co) / 2] for e in nm]}
def edge_groups(edges):
    left, out = set(edges), []
    while left:
        e = left.pop()
        grp, st = [e], [e]
        while st:
            x = st.pop()
            for v in x.verts:
                for y in v.link_edges:
                    if y in left:
                        left.discard(y)
                        grp.append(y)
                        st.append(y)
        out.append(grp)
    return out


rep['after']['open_loops'] = [{'verts': [[round(c, 3) for c in v.co] for v in {v for e in g for v in e.verts}], 'edges': len(g), 'parts': sorted({mats[f.material_index] for e in g for f in e.link_faces}),
                               'centre': [round(sum(((e.verts[0].co + e.verts[1].co) / 2)[i] for e in g) / len(g), 3) for i in range(3)]} for g in edge_groups(op)]
wire = [e for e in bm.edges if not e.link_faces]
rep['after']['wire_groups'] = [{'edges': len(g), 'centre': [round(sum(((e.verts[0].co + e.verts[1].co) / 2)[i] for e in g) / len(g), 3) for i in range(3)]} for g in edge_groups(wire)]
DUMP = argv[argv.index('--dump') + 1] if '--dump' in argv else None
if DUMP:                                             # the result as arrays (triangulated copy) for review renders
    import numpy as np
    b2 = bm.copy()
    bmesh.ops.triangulate(b2, faces=b2.faces)
    b2.verts.index_update()
    np.savez_compressed(DUMP, V=np.array([v.co[:] for v in b2.verts]), F=np.array([[v.index for v in f.verts] for f in b2.faces]),
                        part=np.array([f.material_index for f in b2.faces]), nm=np.array([[v.index for v in e.verts] for e in b2.edges if len(e.link_faces) > 2]).reshape(-1, 2),
                        op=np.array([[v.index for v in e.verts] for e in b2.edges if len(e.link_faces) == 1]).reshape(-1, 2))
    b2.free()
if SAVE:
    bm.to_mesh(me)
    me.update()
    bpy.ops.wm.save_mainfile()
    rep['saved'] = bpy.data.filepath
bm.free()
text = json.dumps(rep, indent=1, default=str)
print('REPORT' + json.dumps(rep, default=str))
if REPORT:
    open(REPORT, 'w').write(text)
