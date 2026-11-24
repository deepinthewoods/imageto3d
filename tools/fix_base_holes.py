"""Close the holes in a game base body (projects/<race>.blend, mesh `base`) (2026-11-24, user: "for those base bodies with a hole in them ...
if the hole is only on one side you could just mirror the geometry from the other side. Otherwise just close the holes and back up the old
blend file and re-export them as necessary").

    blender -b projects/<race>.blend --factory-startup -P tools/fix_base_holes.py -- [--apply] [--report out.json] [--dump out.npz]
        [--no-fins] [--no-hull] [--delete-faces verdict.json] [--move-vertex x,y,z:dx,dy,dz ...] [--retriangulate-degenerate]
        [--merge-collapsed]

Find what to fix with tools/check_base_mesh.py first. Steps (dry run unless --apply; with --apply the .blend is first copied to
projects/<race>.backup_20261124_before_hole_fix.blend if that does not exist yet; nothing is saved if the bounding box - the o/H frame of
tools/preview/export_bases.py - would change). Used on 2026-11-24 per race with --no-fins --no-hull plus the options named in the reports
experiments/shell_slots_20261122/out/base_*fix*_<race>.json:
  0b. --move-vertex: slide a vertex (basis and every shape key by the same offset), e.g. to untwist a bow-tie quad in a flat area;
  0a. --delete-faces: delete the polygons a verdict json lists ({"delete": [...]}, indices of this .blend): hidden layers / interior lids found by
      rendering the body from many directions, or the faces of a flap / fold; the openings are filled in step 3;
  0.  loose pieces of fewer than 4 faces;
  1.  centre-plane fins (all vertices on x = 0, touching a 3+-face edge) - off with --no-fins (it made human_female worse);
  2.  duplicate faces; 2a. coincident vertices on problem edges merged; 2c. zero-height slivers on problem edges; 2d. dangling flaps (a face with
      2+ open edges hanging off a 3+-face edge); 2e. winding-number "outer skin" test - off with --no-hull (unreliable next to open edges);
      2b. T-junctions welded;
  3.  every closed hole of up to MAX_HOLE_SIDES vertices (a figure-8 loop is split into its simple loops) is filled on its existing vertices (no
      new vertex, so shape keys and weights are untouched): a hole whose mirror twin is closed copies the twin's triangles (symmetric result),
      otherwise beauty triangulation; winding matched face by face to the neighbours; material and UVs from the faces around;
  4.  faces wound against all their neighbours are turned over;
  6.  --merge-collapsed: polygons shrunk to a point in the basis and every shape key are merged away;
  5.  --retriangulate-degenerate: polygons whose automatic triangulation has a zero-area triangle are triangulated with the beauty method.
Reports open / non-manifold edges before and after."""
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

import bmesh
import bpy
from mathutils import Vector

FIN_X_MM = 0.1
MAX_HOLE_SIDES = 30
TJ_TOL_MM = 0.5
WELD_MM = 0.5
COLLAPSE_MM = 0.1
SLIVER_MM = 0.3          # a face lower than this (2 x area / longest edge) is a sliver
HULL_EPS_MM = 0.3
HULL_IN = 0.6
HULL_OUT = 0.4
MIRROR_TOL_MM = 2.0

argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else []
APPLY = '--apply' in argv
REPORT = argv[argv.index('--report') + 1] if '--report' in argv else None
blend = Path(bpy.data.filepath)
ob = bpy.data.objects.get('base')
if ob.mode != 'OBJECT':                        # a .blend saved in Edit Mode: leave it first (to_mesh refuses an edit-mode mesh)
    bpy.context.view_layer.objects.active = ob
    bpy.ops.object.mode_set(mode='OBJECT')
me = ob.data
bm = bmesh.new()
bm.from_mesh(me)


def stats(bm):
    return {'verts': len(bm.verts), 'faces': len(bm.faces), 'open_edges': sum(1 for e in bm.edges if len(e.link_faces) == 1),
            'nonmanifold_edges': sum(1 for e in bm.edges if len(e.link_faces) > 2)}


rep = {'race': blend.stem, 'before': stats(bm), 'actions': []}
rep['bbox_before'] = [[round(min(v.co[i] for v in bm.verts), 5) for i in range(3)], [round(max(v.co[i] for v in bm.verts), 5) for i in range(3)]]
# 0b. --move-vertex x,y,z:dx,dy,dz (repeatable): the vertex at x,y,z (within 0.5 mm) moves by dx,dy,dz in the basis AND in every shape key, so
#     the keys keep their offsets (untwists human_male's bow-tie quad on the flat top of each shoulder by sliding one corner 1.5 mm in its plane)
mv = [argv[i + 1] for i, a in enumerate(argv) if a == '--move-vertex']
if mv:
    bm.verts.ensure_lookup_table()
    shape_layers = list(bm.verts.layers.shape.values())
    for spec in mv:
        at, off = spec.split(':')
        at, off = Vector([float(x) for x in at.split(',')]), Vector([float(x) for x in off.split(',')])
        v = min(bm.verts, key=lambda v: (v.co - at).length)
        if (v.co - at).length * 1000 > 0.5:
            raise SystemExit(f'--move-vertex: no vertex within 0.5 mm of {tuple(at)}')
        v.co += off
        for L in shape_layers:
            v[L] = v[L] + off
        rep['actions'].append({'vertex_moved': [round(c, 4) for c in at], 'by_mm': [round(c * 1000, 2) for c in off], 'shape_keys': len(shape_layers)})
# 0a. faces named by a visibility verdict (--delete-faces verdict.json: 'delete' = polygon indices of this .blend, interior lids / fins / inside-out
#     flaps never seen as outer skin from outside; made by rendering the body from many directions around each problem area)
if '--delete-faces' in argv:
    vd = json.loads(Path(argv[argv.index('--delete-faces') + 1]).read_text())
    bm.faces.ensure_lookup_table()
    dl = [bm.faces[i] for i in vd['delete']]
    if dl:
        rep['actions'].append({'not_outer_skin_deleted_by_visibility': len(dl), 'at': [[round(c, 4) for c in f.calc_center_median()] for f in dl[:10]]})
        bmesh.ops.delete(bm, geom=dl, context='FACES')
        bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context='VERTS')
# 0. loose debris: pieces of fewer than 4 faces (a single triangle floating inside the orc hands, one sticking out of the dwarf head on one side
#    only: the other side has nothing there, so removing it IS mirroring that side)
bm.faces.ensure_lookup_table()
seen_f, debris = set(), []
for f in bm.faces:
    if f in seen_f:
        continue
    comp, stack = [], [f]
    while stack:
        x = stack.pop()
        if x in seen_f:
            continue
        seen_f.add(x)
        comp.append(x)
        for e in x.edges:
            stack += [y for y in e.link_faces if y not in seen_f]
    if len(comp) < 4:
        debris.append(comp)
if debris:
    vs = {v for comp in debris for f in comp for v in f.verts}
    rep['actions'].append({'loose_pieces_deleted': len(debris), 'faces': sum(len(c) for c in debris),
                           'at': [[round(c, 4) for c in comp[0].calc_center_median()] for comp in debris]})
    bmesh.ops.delete(bm, geom=list(vs), context='VERTS')
# 1. centre-plane fins
tol = FIN_X_MM / 1000
fins = [] if '--no-fins' in argv else \
    [f for f in bm.faces if all(abs(v.co.x) < tol for v in f.verts) and any(len(e.link_faces) > 2 for e in f.edges)]
if fins:
    rep['actions'].append({'centre_plane_fins_deleted': len(fins), 'at': [[round(c, 4) for c in f.calc_center_median()] for f in fins[:10]]})
    bmesh.ops.delete(bm, geom=fins, context='FACES_ONLY')
    loose = [v for v in bm.verts if not v.link_faces]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context='VERTS')
        rep['actions'].append({'loose_vertices_deleted': len(loose)})
# 2. duplicate faces
seen, dupf = {}, []
for f in bm.faces:
    k = frozenset(v.index for v in f.verts)
    if k in seen:
        dupf.append(f)
    else:
        seen[k] = f
if dupf:
    bmesh.ops.delete(bm, geom=dupf, context='FACES_ONLY')
    rep['actions'].append({'duplicate_faces_deleted': len(dupf)})
bm.verts.ensure_lookup_table()
bm.faces.ensure_lookup_table()
# 2a. coincident vertices on problem edges (open or shared by 3+ faces) that were never merged (the gnome's bottom slit, a crack on human_female's
#     lower back): merged (positions unchanged, so the bounding box - the pipeline's o/H frame - is unchanged)
for _ in range(3):
    bad_v = list({v for e in bm.edges if len(e.link_faces) != 2 for v in e.verts})
    n0 = len(bm.verts)
    bmesh.ops.remove_doubles(bm, verts=bad_v, dist=WELD_MM / 1000)
    if len(bm.verts) == n0:
        break
    rep['actions'].append({'coincident_vertices_merged': n0 - len(bm.verts)})
# 2c. zero-area sliver faces on problem edges (halfling_male's neck): deleted with any edge / vertex left loose
def height(f):
    return 2 * f.calc_area() / max(max(e.calc_length() for e in f.edges), 1e-12)


slivers = [f for f in bm.faces if height(f) * 1000 < SLIVER_MM and any(len(e.link_faces) != 2 for e in f.edges)]
if slivers:
    rep['actions'].append({'sliver_faces_deleted': len(slivers), 'at': [[round(c, 4) for c in f.calc_center_median()] for f in slivers[:10]]})
    bmesh.ops.delete(bm, geom=slivers, context='FACES')
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context='VERTS')
# 2d. dangling flaps: a face with 2+ open edges hanging off an edge that 3+ faces share (halfling_female's shoulders, human_female's back)
for _ in range(10):
    flaps = [f for f in bm.faces if sum(len(e.link_faces) == 1 for e in f.edges) >= 2 and any(len(e.link_faces) > 2 for e in f.edges)]
    if not flaps:
        break
    rep['actions'].append({'dangling_flaps_deleted': len(flaps), 'at': [[round(c, 4) for c in f.calc_center_median()] for f in flaps[:10]]})
    bmesh.ops.delete(bm, geom=flaps, context='FACES')
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context='VERTS')
# 2e. outer skin: of the faces on a problem edge, those whose front is inside the body (generalised winding number just in front of the face
#     > HULL_IN) or that are a zero-thickness fin in the air (winding just in front AND just behind < HULL_OUT) are not part of the outer skin
#     and are deleted (doubled collar flap, centre-line fins, folded pockets); the openings this leaves are filled below
if '--no-hull' not in argv:
    import numpy as np
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    Vn = np.array([v.co[:] for v in bm.verts])
    T = np.array([(f.verts[0].index, f.verts[k].index, f.verts[k + 1].index) for f in bm.faces for k in range(1, len(f.verts) - 1)])

    def winding(P):
        out = []
        for s0 in range(0, len(P), 64):
            p = P[s0:s0 + 64][:, None]
            A, B, C = Vn[T[:, 0]][None] - p, Vn[T[:, 1]][None] - p, Vn[T[:, 2]][None] - p
            la, lb, lc = (np.linalg.norm(X, axis=-1) for X in (A, B, C))
            num = np.einsum('ijk,ijk->ij', A, np.cross(B, C))
            den = la * lb * lc + (A * B).sum(-1) * lc + (B * C).sum(-1) * la + (C * A).sum(-1) * lb
            out.append((2 * np.arctan2(num, den)).sum(1) / (4 * np.pi))
        return np.concatenate(out) if out else np.zeros(0)

    eps = HULL_EPS_MM / 1000
    good = [f for f in bm.faces if all(len(e.link_faces) == 2 for e in f.edges) and f.calc_area() > 1e-6][::7][:150]
    sign = float(np.sign(np.median(winding(np.array([(f.calc_center_median() - f.normal * eps)[:] for f in good]))))) or 1.0
    rep['winding_sign'] = sign
    cand = [f for f in bm.faces if any(len(e.link_faces) != 2 for e in f.edges) and height(f) * 1000 >= SLIVER_MM]
    if cand:
        C0 = np.array([f.calc_center_median()[:] for f in cand])
        Nn = np.array([f.normal[:] for f in cand])
        w_out, w_in = sign * winding(C0 + Nn * eps), sign * winding(C0 - Nn * eps)
        drop = [f for f, wo, wi in zip(cand, w_out, w_in) if wo > HULL_IN or (wo < HULL_OUT and wi < HULL_OUT)]
        rep['hull_test'] = [{'at': [round(c, 4) for c in f.calc_center_median()], 'w_front': round(float(wo), 2), 'w_back': round(float(wi), 2),
                             'dropped': f in drop} for f, wo, wi in zip(cand, w_out, w_in)]
        if drop:
            rep['actions'].append({'not_outer_skin_deleted': len(drop)})
            bmesh.ops.delete(bm, geom=drop, context='FACES')
            bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context='VERTS')
bm.verts.ensure_lookup_table()
bm.faces.ensure_lookup_table()
# 2b. T-junctions: an open-edge vertex lying on another open edge (the gnome's bottom ring against the bottom cap: they touch but share no
#     vertex) -> that edge is split at the vertex (shape keys / weights interpolated by the split) and the new vertex welded onto it
tj = 0
for _ in range(2000):
    changed = False
    open_e = [e for e in bm.edges if e.is_valid and len(e.link_faces) == 1]
    ov = {v for e in open_e for v in e.verts}
    for v in ov:
        if not v.is_valid:
            continue
        for e in open_e:
            if not e.is_valid or v in e.verts:
                continue
            a, b = e.verts[0].co, e.verts[1].co
            ab = b - a
            L2 = ab.length_squared
            if L2 < 1e-12:
                continue
            t = (v.co - a).dot(ab) / L2
            if t <= 0.02 or t >= 0.98:
                continue
            if (a + ab * t - v.co).length * 1000 > TJ_TOL_MM:
                continue
            ne, nv = bmesh.utils.edge_split(e, e.verts[0], t)
            bmesh.ops.weld_verts(bm, targetmap={nv: v})
            tj += 1
            changed = True
            break
        if changed:
            break
    if not changed:
        break
if tj:
    rep['actions'].append({'t_junctions_welded': tj})
bm.verts.ensure_lookup_table()
bm.faces.ensure_lookup_table()
# 3. holes
uv = bm.loops.layers.uv.active


def next_boundary_loop(l):
    """the boundary loop that follows boundary loop l (edge a->b of its face) around the hole: turn about b through the faces (half-edge walk),
    so two holes touching at one vertex are walked separately"""
    cur = l.link_loop_next
    for _ in range(64):
        if len(cur.edge.link_faces) == 1:
            return cur
        opp = [x for x in cur.edge.link_loops if x.face is not cur.face and x.vert is not cur.vert] or \
              [x for x in cur.edge.link_loops if x.face is not cur.face]
        cur = opp[0].link_loop_next
        if cur.vert is not l.link_loop_next.vert:
            cur = next((x for x in cur.face.loops if x.vert is l.link_loop_next.vert), cur)
    return None


def split_at_repeats(chain):
    """a hole loop that passes a vertex twice (two holes touching at one vertex: a figure 8) -> its simple loops"""
    out, stack = [], [chain]
    while stack:
        c = stack.pop()
        seen = {}
        for i, v in enumerate(c):
            if v in seen:
                j = seen[v]
                stack += [c[j:i], c[:j] + c[i:]]
                break
            seen[v] = i
        else:
            if len(c) >= 3:
                out.append(c)
    return out


def boundary_loops(bm):
    loops, used = [], set()
    for e0 in [e for e in bm.edges if len(e.link_faces) == 1]:
        l0 = e0.link_loops[0]
        if l0 in used:
            continue
        chain, l, ok = [], l0, True
        while True:
            used.add(l)
            chain.append(l.vert)
            l = next_boundary_loop(l)
            if l is None or len(chain) > 400:
                ok = False
                why = 'no next boundary edge' if l is None else 'too long'
                break
            if l is l0:
                break
            if l in used:
                ok = False
                why = 'ran into another walk'
                break
        if ok and len(chain) >= 3:
            loops.extend(split_at_repeats(chain))
        elif not ok:
            rep.setdefault('loop_walk_failures', []).append({'why': why, 'walked': len(chain), 'from': [round(c, 4) for c in chain[0].co]})
    return loops


kd = None
from mathutils import kdtree  # noqa: E402


def mirror_faces(loop):
    """Faces of the mirror-image twin of a hole: the twin vertices (mirrored positions), and the twin-side faces that use only them."""
    global kd
    if kd is None:
        bm.verts.index_update()                                  # indices are stale after the deletions above
        bm.verts.ensure_lookup_table()
        kd = kdtree.KDTree(len(bm.verts))
        for v in bm.verts:
            kd.insert(v.co, v.index)
        kd.balance()
    twin = {}
    for v in loop:
        co, i, d = kd.find(Vector((-v.co.x, v.co.y, v.co.z)))
        if d * 1000 > MIRROR_TOL_MM:
            rep.setdefault('mirror_notes', []).append({'hole_at': [round(c, 4) for c in v.co], 'no_twin_within_mm': MIRROR_TOL_MM,
                                                       'nearest_mm': round(d * 1000, 2)})
            return None
        twin[i] = v
    tv = set(twin)
    faces = {f for i in tv for f in bm.verts[i].link_faces if all(x.index in tv for x in f.verts)}
    if not faces:
        rep.setdefault('mirror_notes', []).append({'hole_at': [round(c, 4) for c in loop[0].co], 'twin_side_has_no_faces': True})
        return None
    return [[twin[x.index] for x in f.verts][::-1] for f in faces]     # mirrored, so the winding reverses


def orient_from_neighbours(new):
    """make every new face run each shared edge opposite to the face across it: start from new faces next to old faces, then spread"""
    todo, done = set(new), set()
    while todo:
        start = next((f for f in todo for l in f.loops for l2 in l.edge.link_loops if l2.face not in todo and l2.face is not f
                      and len(l.edge.link_faces) == 2), None) or next(iter(todo))
        stack = [start]
        while stack:
            f = stack.pop()
            if f in done:
                continue
            ref = [(l, l2) for l in f.loops for l2 in l.edge.link_loops if l2.face is not f and (l2.face in done or l2.face not in todo)
                   and len(l.edge.link_faces) == 2]
            if ref and sum(l2.vert == l.vert for l, l2 in ref) * 2 > len(ref):
                f.normal_flip()
            done.add(f)
            todo.discard(f)
            stack += [l2.face for l in f.loops for l2 in l.edge.link_loops if l2.face in todo]


holes = boundary_loops(bm)
filled = []
for lp in holes:
    if len(lp) > MAX_HOLE_SIDES:
        rep['actions'].append({'hole_left_open': len(lp), 'centre': [round(c, 4) for c in sum((v.co for v in lp), Vector()) / len(lp)]})
        continue
    around = Counter(f.material_index for v in lp for f in v.link_faces)
    mat = around.most_common(1)[0][0] if around else 0
    uvs = {}
    if uv is not None:
        for v in lp:
            for l in v.link_loops:
                uvs[v] = l[uv].uv.copy()
                break
    centre = sum((v.co for v in lp), Vector()) / len(lp)
    how = 'triangulated'
    tw = mirror_faces(lp) if abs(centre.x) > 0.01 else None
    new = []
    if tw:
        for vs in tw:
            try:
                new.append(bm.faces.new(vs))
            except ValueError:
                pass
        how = 'mirrored from the twin side'
    if not new:
        try:                                                     # one face over the hole (its winding fixed below), then triangulated
            f0 = bm.faces.new(list(reversed(lp)))
        except ValueError as error:
            rep['actions'].append({'hole_not_filled': len(lp), 'why': str(error), 'centre': [round(c, 4) for c in centre]})
            continue
        new = [f0]
        if len(lp) > 3:
            new = bmesh.ops.triangulate(bm, faces=new, quad_method='BEAUTY', ngon_method='BEAUTY')['faces']
    # winding: a new face must run each edge it shares with an old face the other way round (flip the whole patch if most disagree)
    newset = set(new)
    agree = disagree = 0
    for f in new:
        for l in f.loops:
            for l2 in l.edge.link_loops:
                if l2.face not in newset and l2.face is not f:
                    if l2.vert == l.vert:
                        disagree += 1
                    else:
                        agree += 1
    if disagree > agree:
        for f in new:
            f.normal_flip()
    orient_from_neighbours(new)
    nz = sum((f.normal * f.calc_area() for f in new), Vector())
    rep.setdefault('fill_debug', []).append({'sides': len(lp), 'agree': agree, 'disagree': disagree, 'flipped': disagree > agree,
                                             'area_normal': [round(c, 4) for c in nz]})
    for f in new:
        f.material_index = mat
        if uv is not None:
            for l in f.loops:
                if l.vert in uvs:
                    l[uv].uv = uvs[l.vert]
    bm.normal_update()
    filled.append({'sides': len(lp), 'centre': [round(c, 4) for c in centre], 'faces_added': len(new), 'how': how,
                   'material': me.materials[mat].name if mat < len(me.materials) and me.materials[mat] else mat})
if filled:
    rep['actions'].append({'holes_filled': filled})
# 4. faces wound against ALL their neighbours (every manifold edge runs the same way in both faces: the face is turned over, shows its inside)
flipped = []
for f in bm.faces:
    shared = same = 0
    for l in f.loops:
        others = [l2 for l2 in l.edge.link_loops if l2.face is not f]
        if len(others) != 1:
            continue
        shared += 1
        same += others[0].vert == l.vert
    if shared >= 2 and same == shared:
        flipped.append(f)
for f in flipped:
    f.normal_flip()
if flipped:
    rep['actions'].append({'inside_out_faces_flipped': len(flipped), 'at': [[round(c, 4) for c in f.calc_center_median()] for f in flipped[:10]]})
# 6. --merge-collapsed: a polygon shrunk to a point (all its vertices within COLLAPSE_MM of each other, in the basis and in every shape key -
#    checked) is removed by merging its vertices into one (elf_male's two collapsed triangles at the shoulders)
if '--merge-collapsed' in argv:
    shape_layers = list(bm.verts.layers.shape.values())
    bm.faces.ensure_lookup_table()
    merged = []
    for f in list(bm.faces):
        if not f.is_valid:
            continue
        vs = list(f.verts)
        spread = max((a.co - b.co).length for a in vs for b in vs)
        for L in shape_layers:
            spread = max(spread, max((a[L] - b[L]).length for a in vs for b in vs))
        if spread * 1000 < COLLAPSE_MM:
            at = [round(c, 4) for c in f.calc_center_median()]
            bmesh.ops.pointmerge(bm, verts=vs, merge_co=vs[0].co.copy())
            merged.append(at)
    if merged:
        rep['actions'].append({'collapsed_polygons_merged': len(merged), 'at': merged})
    bmesh.ops.delete(bm, geom=[f for f in bm.faces if len(set(f.verts)) < 3], context='FACES_ONLY')
# 5. --retriangulate-degenerate: a polygon whose automatic triangulation (what the exporters use) contains a zero-area triangle - a vertex lying
#    on the straight line between two others, plugging a T-junction - is triangulated here with the beauty method, which never makes that
#    triangle (the exporter drops it and leaves a zero-width crack: human_female's thigh)
if '--retriangulate-degenerate' in argv:
    me.calc_loop_triangles()
    bad_polys = sorted({lt.polygon_index for lt in me.loop_triangles if lt.area < 1e-8})
    bm.faces.ensure_lookup_table()
    if bad_polys and len(bm.faces) == len(me.polygons):
        tri = bmesh.ops.triangulate(bm, faces=[bm.faces[i] for i in bad_polys], quad_method='BEAUTY', ngon_method='BEAUTY')['faces']
        rep['actions'].append({'degenerate_triangulation_fixed': len(bad_polys), 'triangles': len(tri),
                               'smallest_triangle_mm2': round(min(f.calc_area() for f in tri) * 1e6, 2),
                               'at': [[round(c, 4) for c in me.polygons[i].center] for i in bad_polys]})
    elif bad_polys:
        rep['actions'].append({'degenerate_triangulation_skipped': 'run it on its own (faces were added or removed above)'})
rep['after'] = stats(bm)
bb = lambda: ([min(v.co[i] for v in bm.verts) for i in range(3)], [max(v.co[i] for v in bm.verts) for i in range(3)])
rep['bbox_after'] = [[round(c, 5) for c in x] for x in bb()]
if '--dump' in argv:                                   # triangulated copy for checks / renders: V (world), F, P (material index)
    import numpy as np
    tmp = bm.copy()
    bmesh.ops.triangulate(tmp, faces=tmp.faces[:])
    np.savez(argv[argv.index('--dump') + 1], V=np.array([list(ob.matrix_world @ v.co) for v in tmp.verts]),
             F=np.array([[v.index for v in f.verts] for f in tmp.faces]), P=np.array([f.material_index for f in tmp.faces]))
rep['open_after_at'] = [[round(c, 4) for c in (e.verts[0].co + e.verts[1].co) / 2] for e in bm.edges if len(e.link_faces) == 1][:20]
rep['nonmanifold_after_at'] = [[round(c, 4) for c in (e.verts[0].co + e.verts[1].co) / 2] for e in bm.edges if len(e.link_faces) > 2][:20]
rep['applied'] = APPLY
if APPLY and rep['bbox_after'] != rep['bbox_before']:
    raise SystemExit(f'bounding box changed {rep["bbox_before"]} -> {rep["bbox_after"]}: not saved (o/H would move)')
if APPLY:
    backup = blend.with_name(f'{blend.stem}.backup_20261124_before_hole_fix.blend')
    if not backup.exists():
        shutil.copy2(blend, backup)
    rep['backup'] = str(backup)
    nsk = len(me.shape_keys.key_blocks) if me.shape_keys else 0
    bm.to_mesh(me)
    me.update()
    rep['shape_keys_after'] = len(me.shape_keys.key_blocks) if me.shape_keys else 0
    assert rep['shape_keys_after'] == nsk
    bpy.ops.wm.save_mainfile()
print('FIXREPORT', json.dumps(rep))
if REPORT:
    Path(REPORT).write_text(json.dumps(rep, indent=1))
