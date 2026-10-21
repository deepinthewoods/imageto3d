"""Reapply the existing reduction methods to a new, read-only Tripo source.

Run in background Blender with -- prepare | candidate ID | gallery.
Recipes and results are confined to results/tripo_01. Original units/orientation
are restored for GLB exports; normalized geometry is retained for comparison.
"""
from pathlib import Path
import sys, json, math, hashlib, struct, time, subprocess, os
import bpy, bmesh, numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import lowpoly_lab as lab
from adaptive_lod04 import simplify, measure, compact

OUT = Path(os.environ.get('TRIPO_STUDY_RUN', str(ROOT / 'results/tripo_01'))).resolve()
SOURCE = Path(os.environ.get('TRIPO_STUDY_SOURCE', str(ROOT.parents[1] / "tripo/low-poly+human+3d+model.glb"))).resolve()
lab.RUN = OUT
lab.VIEWS['top'] = (0, 0, 7)


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n')


def glb_count(path):
    data = path.read_bytes()
    size, kind = struct.unpack_from('<II', data, 12)
    assert kind == 0x4e4f534a
    doc = json.loads(data[20:20+size])
    return sum(doc['accessors'][p['indices']]['count'] // 3
               for m in doc['meshes'] for p in m['primitives']
               if p.get('mode', 4) == 4)


def audit(v, f):
    edges = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    unique, inv, counts = np.unique(np.sort(edges, axis=1), axis=0,
                                    return_inverse=True, return_counts=True)
    winding = np.bincount(inv, weights=np.where(edges[:, 0] < edges[:, 1], 1, -1))
    area2 = np.linalg.norm(np.cross(v[f[:, 1]]-v[f[:, 0]], v[f[:, 2]]-v[f[:, 0]]), axis=1)
    return {'finite_and_valid_indices': bool(np.isfinite(v).all() and f.min() >= 0 and f.max() < len(v)),
            'boundary_edges': int((counts == 1).sum()),
            'nonmanifold_edges': int((counts > 2).sum()),
            'inconsistent_winding_edges': int(((counts == 2) & (winding != 0)).sum()),
            'degenerate_triangles': int((area2 < 1e-12).sum()),
            'duplicate_triangles': int(len(f)-len(np.unique(np.sort(f, axis=1), axis=0)))}


def save_mesh(obj):
    v, f = lab.arrays(obj)
    sizes = np.array([len(p.vertices) for p in obj.data.polygons], dtype=np.int32)
    indices = np.array([i for p in obj.data.polygons for i in p.vertices], dtype=np.int32)
    np.savez_compressed(OUT / 'meshes' / (obj.name + '.npz'), v=v, f=f,
                        polygon_sizes=sizes, polygon_indices=indices)


def load_mesh(name):
    d = np.load(OUT / 'meshes' / (name + '.npz'))
    sizes = d['polygon_sizes'] if 'polygon_sizes' in d else None
    faces = np.split(d['polygon_indices'], np.cumsum(sizes)[:-1]) if sizes is not None else d['f']
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(d['v'].tolist(), [], [face.tolist() for face in faces]); mesh.update()
    obj = bpy.data.objects.new(name, mesh); bpy.context.collection.objects.link(obj)
    return obj


def recipes():
    result = [{'id': f'collapse_{n}', 'method': 'collapse', 'target': n} for n in [300, 600, 1132, 2400, 5000]]
    result += [
        {'id': 'smooth_1132', 'method': 'smooth_collapse', 'target': 1132, 'factor': .5, 'iterations': 3},
        {'id': 'preplanar_5deg', 'method': 'pre_planar', 'angle': 5},
        {'id': 'preplanar_15deg_1132', 'method': 'pre_planar', 'angle': 15, 'target': 1132},
        {'id': 'patch_12deg_1132', 'method': 'patch', 'target': 1132, 'pre_target': 6000, 'angle': 12, 'tolerance': .002},
        {'id': 'patch_22deg_600', 'method': 'patch', 'target': 600, 'pre_target': 6000, 'angle': 22, 'tolerance': .004},
        {'id': 'quadriflow_550q', 'method': 'quadriflow', 'quads': 550},
        {'id': 'voxel_quad_550q', 'method': 'voxel_quad', 'quads': 550, 'voxel_size': .008},
    ]
    for vox in [.004, .008]:
        for target in [600, 1132]:
            result.append({'id': f'voxel{int(vox*1000)}_{target}', 'method': 'voxel_collapse', 'target': target, 'voxel_size': vox})
    for target in [600, 1132]:
        result.append({'id': f'voxel_patch_{target}', 'method': 'voxel_patch', 'target': target,
                       'voxel_size': .008, 'angle': 18, 'tolerance': .003})
    for fraction in [.0005, .001, .002, .004, .008, .016]:
        result.append({'id': f'meshopt_error{fraction:g}', 'method': 'meshopt_error', 'height_error_fraction': fraction})
    for count in [100, 200, 350]:
        result.append({'id': f'cgal_vsa_{count}', 'method': 'cgal_vsa', 'proxies': count, 'pre_target': 20000})
    for target in [600, 1132, 2400]:
        result.append({'id': f'voxel2_{target}', 'method': 'voxel_collapse', 'target': target, 'voxel_size': .002})
    for fraction in [.001, .002, .004, .008]:
        result.append({'id': f'voxel4_qem_error{fraction:g}', 'method': 'voxel_meshopt',
                       'voxel_size': .004, 'height_error_fraction': fraction, 'cancel_opposite_duplicate_pairs': True})
    for count in [100, 200, 350]:
        result.append({'id': f'voxel4_cgal_vsa_{count}', 'method': 'cgal_vsa', 'voxel_size': .004,
                       'proxies': count, 'pre_target': 20000})
    for target in [600, 1132, 2400]:
        result.append({'id': f'voxel2_{target}_cancel_pairs', 'method': 'cancel_pairs', 'parent': f'voxel2_{target}'})
    return result


def prepare(replace_analysis=False):
    if (OUT / 'source.json').exists():
        assert replace_analysis and not list((OUT / 'records').glob('*.json')), 'Completed run preserved'
        assert json.loads((OUT/'source.json').read_text())['sha256']==hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    for sub in ['meshes', 'renders', 'exports', 'records', 'logs']:
        (OUT / sub).mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    source_hash = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    bpy.ops.import_scene.gltf(filepath=str(SOURCE), merge_vertices=False)
    objects = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    assert len(objects) == 1
    obj = objects[0]
    # Recoverable exact import, including original materials/UVs, before edits.
    bpy.ops.file.pack_all()
    if not (OUT/'source_import.blend').exists():
        bpy.ops.wm.save_as_mainfile(filepath=str(OUT / 'source_import.blend'), compress=True)
    lab.active(obj)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    v, f = lab.arrays(obj)
    yaw = float(os.environ.get('TRIPO_STUDY_YAW_DEG', '-90'))
    rotation = np.array(Matrix.Rotation(math.radians(yaw), 3, 'Z'))
    turned = v @ rotation.T
    lo, hi = turned.min(axis=0), turned.max(axis=0)
    origin = (lo + hi)/2
    origin[2] = lo[2]
    scale = 2 / (hi[2]-lo[2])
    obj.data.vertices.foreach_set('co', ((turned-origin)*scale).ravel())
    obj.data.update()
    imported_vertices, imported_triangles = len(v), len(f)
    bm = bmesh.new(); bm.from_mesh(obj.data)
    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=2e-7)
    bm.to_mesh(obj.data); bm.free(); obj.data.update()
    obj.name = 'source'
    save_mesh(obj)
    v, f = lab.arrays(obj)
    result = {'source': str(SOURCE), 'sha256': source_hash, 'blender_version': bpy.app.version_string,
              'serialized_triangles': glb_count(SOURCE), 'imported_triangles': imported_triangles,
              'imported_vertices': imported_vertices, 'normalization_origin_rotated': origin.tolist(),
              'normalization_scale': float(scale), 'rotation_to_review': rotation.tolist(), 'review_yaw_degrees': yaw,
              'height': 2, 'weld_distance': 2e-7, 'stats': lab.stats(obj), 'audit': audit(v, f),
              'scope': 'Geometry fidelity to Tripo source, not authored-shape accuracy. No texture rebake or rigging.'}
    write_json(OUT / 'source.json', result)
    write_json(OUT / 'recipes.json', recipes())
    lab.render(obj)
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == source_hash
    print('TRIPO_SOURCE', json.dumps(result), flush=True)


def export_native(obj, info):
    copy = obj.copy(); copy.data = obj.data.copy()
    bpy.context.scene.collection.objects.link(copy)
    copy.name = obj.name + '_native'
    v, _ = lab.arrays(copy)
    v = (v / info['normalization_scale'] + np.array(info['normalization_origin_rotated'])) @ np.array(info['rotation_to_review'])
    copy.data.vertices.foreach_set('co', v.ravel()); copy.data.update()
    copy.data.materials.clear()
    lab.active(copy)
    path = OUT / 'exports' / (obj.name + '.glb')
    bpy.ops.export_scene.gltf(filepath=str(path), use_selection=True, export_materials='NONE')
    count = glb_count(path)
    lab.drop(copy)
    return count


def candidate(name):
    path = OUT / 'records' / (name + '.json')
    if path.exists():
        print('RESUME_SKIP', name, flush=True); return
    recipe = next(r for r in json.loads((OUT / 'recipes.json').read_text()) if r['id'] == name)
    info = json.loads((OUT / 'source.json').read_text())
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == info['sha256']
    bpy.ops.wm.read_factory_settings(use_empty=True)
    d = np.load(OUT / 'meshes/source.npz'); rv, rf = d['v'], d['f']
    obj = lab.make_obj(name, rv, rf)
    started = time.perf_counter(); extra = {}
    if recipe['method'] == 'cancel_pairs':
        from adaptive_cleanup04 import cancel_pairs
        parent = np.load(OUT / 'meshes' / (recipe['parent'] + '.npz'))
        v, f = parent['v'], parent['f']; keep = cancel_pairs(v, f)
        extra['opposite_duplicate_pairs_cancelled'] = int((~keep).sum()//2)
        v, f = compact(v, f[keep]); lab.drop(obj); obj = lab.make_obj(name, v, f)
    elif recipe['method'] in {'meshopt_error', 'voxel_meshopt'}:
        v, f = rv, rf
        if recipe['method'] == 'voxel_meshopt':
            lab.active(obj); obj.data.remesh_voxel_size = recipe['voxel_size']
            obj.data.use_remesh_preserve_volume = True
            bpy.ops.object.voxel_remesh(); v, f = lab.arrays(obj)
        v, f, error = simplify(v, f, 2*recipe['height_error_fraction'], [])
        if recipe.get('cancel_opposite_duplicate_pairs'):
            from adaptive_cleanup04 import cancel_pairs
            keep = cancel_pairs(v, f)
            extra['opposite_duplicate_pairs_cancelled'] = int((~keep).sum()//2)
            v, f = compact(v, f[keep])
        lab.drop(obj); obj = lab.make_obj(name, v, f)
        extra['reported_quadric_error'] = error
    elif recipe['method'] == 'cgal_vsa':
        from external_simplify import off_write, off_read
        if recipe.get('voxel_size'):
            lab.apply_recipe(obj, {'method': 'voxel_collapse', 'voxel_size': recipe['voxel_size'], 'target': recipe['pre_target']})
        else:
            lab.decimate(obj, recipe['pre_target'])
        v, f = lab.arrays(obj)
        inp = OUT / 'meshes' / (name + '_input.off'); off_write(inp, v, f)
        output = OUT / 'meshes' / (name + '.off')
        r = subprocess.run([str(ROOT / '.deps/cgal/vsa'), str(inp), str(output), str(recipe['proxies'])],
                           capture_output=True, text=True, timeout=90)
        (OUT / 'logs' / (name + '_cgal.log')).write_text(r.stdout + r.stderr)
        extra['cgal_returncode'] = r.returncode
        v, f = off_read(output); v, f = compact(v, f)
        lab.drop(obj); obj = lab.make_obj(name, v, f)
    else:
        lab.apply_recipe(obj, recipe)
    elapsed = time.perf_counter()-started
    v, f = lab.arrays(obj)
    tree = BVHTree.FromPolygons(rv.tolist(), rf.tolist(), all_triangles=True)
    refpoints, _ = lab.sample_surface(rv, rf, 10000, seed=940)
    result = {'recipe': recipe, 'build_seconds': elapsed, **extra, **lab.stats(obj),
              **measure(v, f, tree, refpoints), 'audit': audit(v, f)}
    a = result['audit']
    result['structural_pass'] = bool(a['finite_and_valid_indices'] and not any(a[k] for k in
        ['boundary_edges', 'nonmanifold_edges', 'inconsistent_winding_edges', 'degenerate_triangles', 'duplicate_triangles'])
        and result['face_components'] == info['stats']['face_components'] and extra.get('cgal_returncode', 0) == 0)
    save_mesh(obj)
    result['export_triangles'] = export_native(obj, info)
    result['export_count_matches'] = result['triangles'] == result['export_triangles']
    result['structural_pass'] &= result['export_count_matches']
    lab.render(obj)
    write_json(path, result)
    print('TRIPO_CANDIDATE', name, result['triangles'], result['structural_pass'], elapsed, flush=True)


def gallery():
    selection = json.loads((OUT / 'selection.json').read_text())
    names = ['source'] + selection['gallery_candidates']
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    for i, name in enumerate(names):
        obj = load_mesh(name)
        obj.location = ((i % 4)*2.9, 0, -(i // 4)*2.8)
        obj['recipe'] = name
        obj['review_display_only'] = 'Normalized height 2; native exports in exports/'
        count = len(lab.arrays(obj)[1])
        font = bpy.data.curves.new(name + '_label', 'FONT')
        state = 'Source has topology defects' if name == 'source' else ('Structure PASS' if selection['candidates'][name]['structural_pass'] else 'Structure FAIL')
        font.body = name + '\n' + f'{count:,} triangles' + '\n' + state
        font.size = .085; font.align_x = 'CENTER'
        label = bpy.data.objects.new(name + '_label', font); scene.collection.objects.link(label)
        label.rotation_euler = (math.pi/2, 0, 0)
        label.location = (obj.location.x, 0, obj.location.z-.3)
    lab.setup_render()
    scene.display.shading.color_type = 'SINGLE'
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            space = area.spaces.active; space.shading.type = 'SOLID'; space.shading.color_type = 'SINGLE'
            space.overlay.show_floor = False
            space.region_3d.view_rotation = (Vector((0, 0, 0))-Vector((0, -10, 0))).to_track_quat('-Z', 'Y')
            space.region_3d.view_location = (4.35, 0, -.4)
            space.region_3d.view_distance = 14
            space.region_3d.view_perspective = 'ORTHO'
    scene['instructions'] = 'Select mesh, Numpad / to isolate, Numpad . to frame. Geometry-only review; original preserved in source_import.blend. Native GLBs in exports/.'
    bpy.ops.object.select_all(action='DESELECT')
    bpy.context.view_layer.objects.active = None
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(OUT / 'comparison.blend'), compress=True)


if __name__ == '__main__':
    args = sys.argv[sys.argv.index('--')+1:]
    if args[0] == 'prepare': prepare()
    elif args[0] == 'reprepare': prepare(replace_analysis=True)
    elif args[0] == 'candidate': candidate(args[1])
    elif args[0] == 'gallery': gallery()
