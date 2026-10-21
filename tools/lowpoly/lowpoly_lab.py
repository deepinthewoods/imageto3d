"""Blender geometry experiment harness. Inputs are read-only; outputs stay in a run.

blender -b -t 4 --python scripts/lowpoly_lab.py -- analyze
blender -b -t 4 --python scripts/lowpoly_lab.py -- build --source pixal_high
"""
import argparse
from collections import Counter
import gc
import json
import math
from pathlib import Path
import sys
import time

import bpy
import bmesh
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from inspect_glb import PATHS

RUN = ROOT / 'results/run_01'
HEIGHT = 2.0
VIEWS = {'front': (0, -6, 1), 'three_quarter': (4, -6, 2.6),
         'back': (0, 6, 1), 'side': (6, 0, 1)}


def emit(**kw):
    print('LAB ' + json.dumps(kw), flush=True)


def active(obj):
    bpy.ops.object.select_all(action='DESELECT')
    obj.hide_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def arrays(obj):
    mesh = obj.data
    mesh.calc_loop_triangles()
    v = np.empty(len(mesh.vertices)*3, dtype=np.float64)
    mesh.vertices.foreach_get('co', v)
    f = np.empty(len(mesh.loop_triangles)*3, dtype=np.int32)
    mesh.loop_triangles.foreach_get('vertices', f)
    return v.reshape(-1, 3), f.reshape(-1, 3)


def make_obj(name, v, f):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(v.tolist(), [], f.tolist())
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


def load_npz(name):
    a = np.load(RUN / 'meshes' / (name + '.npz'))
    return make_obj(name, a['v'], a['f'])


def drop(obj):
    mesh = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def geom(v, f):
    t = v[f]
    cross = np.cross(t[:, 1]-t[:, 0], t[:, 2]-t[:, 0])
    lengths = np.linalg.norm(cross, axis=1)
    return t.mean(axis=1), cross / np.maximum(lengths[:, None], 1e-20), lengths/2


def adjacency(f):
    e = np.sort(np.concatenate((f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]])), axis=1)
    edges, inv, counts = np.unique(e, axis=0, return_inverse=True, return_counts=True)
    order = np.argsort(inv, kind='stable')
    starts = np.r_[0, np.cumsum(counts)[:-1]]
    ids = np.flatnonzero(counts == 2)
    a = order[starts[ids]] % len(f)
    b = order[starts[ids]+1] % len(f)
    return edges, counts, a, b, ids


def stats(obj):
    v, f = arrays(obj)
    c, n, area = geom(v, f)
    edges, counts, a, b, ids = adjacency(f)
    elen = np.linalg.norm(v[edges[:, 0]] - v[edges[:, 1]], axis=1)
    angles = np.degrees(np.arccos(np.clip(np.einsum('ij,ij->i', n[a], n[b]), -1, 1)))
    # Face connected components using edges; touching at a vertex is not connectivity.
    parent = np.arange(len(f))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for x, y in zip(a, b):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry
    roots = np.array([find(i) for i in range(len(f))])
    _, comp = np.unique(roots, return_inverse=True)
    comp_area = np.bincount(comp, weights=area)
    # Scale-free triangle quality 1=equilateral, 0=degenerate.
    t = v[f]
    sum_l2 = ((t[:, 0]-t[:, 1])**2).sum(axis=1) + ((t[:, 1]-t[:, 2])**2).sum(axis=1) + ((t[:, 2]-t[:, 0])**2).sum(axis=1)
    q = 4*math.sqrt(3)*area / np.maximum(sum_l2, 1e-30)
    return {'vertices': len(v), 'faces': len(obj.data.polygons), 'triangles': len(f),
            'polygon_sizes': dict(Counter(len(p.vertices) for p in obj.data.polygons)),
            'bounds_min': v.min(axis=0).tolist(), 'bounds_max': v.max(axis=0).tolist(),
            'area': float(area.sum()), 'triangles_per_height_squared': len(f)/HEIGHT**2,
            'boundary_edges': int((counts==1).sum()), 'nonmanifold_edges_gt2': int((counts>2).sum()),
            'face_components': len(comp_area), 'largest_component_area_fraction': float(comp_area.max()/area.sum()),
            'degenerate_triangles': int((area < 1e-14).sum()),
            'edge_length_height_pct_p10_p50_p90': (np.percentile(elen, [10,50,90])/HEIGHT*100).tolist(),
            'adjacent_normal_angle_deg_p50_p90_p99': np.percentile(angles, [50,90,99]).tolist(),
            'edge_length_fraction_dihedral_below_1deg': float(elen[ids][angles<1].sum()/elen[ids].sum()),
            'triangle_quality_p05_p50': np.percentile(q, [5,50]).tolist()}


def save_mesh(obj):
    v, f = arrays(obj)
    np.savez_compressed(RUN/'meshes'/(obj.name+'.npz'), v=v, f=f)


def setup_render():
    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_WORKBENCH'
    scene.render.resolution_x = 640
    scene.render.resolution_y = 640
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.film_transparent = True
    shade = scene.display.shading
    shade.light = 'STUDIO'
    shade.studiolight_rotate_z = 0.4
    shade.color_type = 'SINGLE'
    shade.single_color = (0.62, 0.69, 0.76)
    shade.show_shadows = True
    shade.show_cavity = True
    shade.cavity_type = 'BOTH'
    shade.curvature_ridge_factor = 1.1
    shade.curvature_valley_factor = 1.0
    shade.show_specular_highlight = False
    scene.view_settings.view_transform = 'Standard'
    if not scene.camera:
        cam = bpy.data.objects.new('LabCamera', bpy.data.cameras.new('LabCamera'))
        scene.collection.objects.link(cam)
        scene.camera = cam
    scene.camera.data.type = 'ORTHO'
    scene.camera.data.ortho_scale = 2.25


def render(obj, views=None, wire=False, suffix=''):
    setup_render()
    for o in bpy.context.scene.objects:
        if o.type == 'MESH':
            o.hide_render = o != obj
    obj.hide_render = False
    # A geometrical flat-shaded review, independent of source texture/normal maps.
    obj.data.polygons.foreach_set('use_smooth', [False]*len(obj.data.polygons))
    mod = None
    if wire:
        mod = obj.modifiers.new('ReviewWire', 'WIREFRAME')
        mod.thickness = .0008
        mod.use_replace = False
    for key in (views or VIEWS):
        cam = bpy.context.scene.camera
        cam.location = VIEWS[key]
        cam.rotation_euler = (Vector((0,0,1))-cam.location).to_track_quat('-Z', 'Y').to_euler()
        bpy.context.scene.render.filepath = str(RUN/'renders'/f'{obj.name}{suffix}_{key}.png')
        bpy.ops.render.render(write_still=True)
    if mod:
        obj.modifiers.remove(mod)


def import_source(name):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(PATHS[name]), merge_vertices=True)
    imported = set(bpy.data.objects)-before
    meshes = [o for o in imported if o.type=='MESH']
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes)>1:
        bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    v, f = arrays(obj)
    lo, hi = v.min(axis=0), v.max(axis=0)
    height = hi[2]-lo[2]
    origin = (lo+hi)/2
    origin[2] = lo[2]
    v = (v-origin) * (HEIGHT/height)
    obj.data.vertices.foreach_set('co', v.ravel())
    obj.name = name
    raw_v = len(v)
    # GLB seam splits are render vertices, not holes. Weld only coincident positions.
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=HEIGHT*1e-7)
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()
    return obj, {'import_vertices': raw_v, 'source_bounds_min':lo.tolist(), 'source_bounds_max':hi.tolist(),
                 'normalized_height': HEIGHT, 'normalization_origin':origin.tolist(), 'normalization_scale':HEIGHT/height,
                 'weld_distance':HEIGHT*1e-7}


def analyze():
    data = {}
    for name in ['hyper_low', 'hyper_high', 'pixal_high']:
        t = time.perf_counter()
        obj, meta = import_source(name)
        data[name] = {**meta, **stats(obj)}
        save_mesh(obj)
        render(obj)
        # Save an independent normalized source copy for recoverability and texture inspection.
        active(obj)
        bpy.ops.wm.save_as_mainfile(filepath=str(RUN/'sources'/f'{name}.blend'), copy=True)
        (RUN/'source_metrics.json').write_text(json.dumps(data, indent=2))
        emit(source=name, seconds=time.perf_counter()-t, metrics=data[name])
        drop(obj)
        bpy.ops.outliner.orphans_purge(do_recursive=True)
        gc.collect()


def decimate(obj, target):
    active(obj)
    _, f = arrays(obj)
    mod = obj.modifiers.new('Budget', 'DECIMATE')
    mod.decimate_type = 'COLLAPSE'
    mod.ratio = min(1.0, target/len(f))
    mod.use_collapse_triangulate = True
    bpy.ops.object.modifier_apply(modifier=mod.name)


def dissolve(obj, degrees):
    active(obj)
    mod = obj.modifiers.new('Planar', 'DECIMATE')
    mod.decimate_type = 'DISSOLVE'
    mod.angle_limit = math.radians(degrees)
    mod.delimit = set()
    bpy.ops.object.modifier_apply(modifier=mod.name)


def sample_surface(v, f, count=16000, seed=17):
    rng = np.random.default_rng(seed)
    _, _, area = geom(v, f)
    ids = rng.choice(len(f), count, p=area/area.sum())
    r = np.sqrt(rng.random(count))
    s = rng.random(count)
    w = np.stack((1-r, r*(1-s), r*s), axis=1)
    return np.einsum('ij,ijk->ik', w, v[f[ids]]), ids


def error_metrics(obj, ref_v, ref_f, ref_bvh, ref_samples):
    v, f = arrays(obj)
    tree = BVHTree.FromPolygons(v.tolist(), f.tolist(), all_triangles=True)
    points, ids = sample_surface(v, f)
    _, normals, _ = geom(v, f)
    def measure(pts, target):
        result = [target.find_nearest(Vector(p)) for p in pts]
        return np.array([r[3] for r in result]), np.array([r[1] for r in result]), np.array([r[2] for r in result])
    forward, hit_n, hit_i = measure(points, ref_bvh)
    reverse, _, _ = measure(ref_samples, tree)
    all_d = np.r_[forward, reverse]
    normal_error = np.degrees(np.arccos(np.clip(np.einsum('ij,ij->i',normals[ids],hit_n),-1,1)))
    return {'surface_sample_count_each_direction':len(points), 'sampling_seed':17,
            'distance_rms_height_pct': float(np.sqrt((all_d**2).mean())/HEIGHT*100),
            'distance_p95_height_pct': float(np.percentile(all_d,95)/HEIGHT*100),
            'distance_sample_max_height_pct':float(all_d.max()/HEIGHT*100),
            'normal_error_deg_p50_p90':np.percentile(normal_error,[50,90]).tolist(),
            'regional_reverse_p95_height_pct':{
                'head':float(np.percentile(reverse[ref_samples[:,2]>1.6],95)/HEIGHT*100),
                'hands':float(np.percentile(reverse[np.abs(ref_samples[:,0])>.75],95)/HEIGHT*100),
                'feet':float(np.percentile(reverse[ref_samples[:,2]<.15],95)/HEIGHT*100)}}


def export_obj(obj):
    active(obj)
    obj.data.materials.clear()
    # Geometry experiments intentionally omit UVs/material transfer.
    while obj.data.uv_layers:
        obj.data.uv_layers.remove(obj.data.uv_layers[0])
    bpy.ops.wm.obj_export(filepath=str(RUN/'exports'/(obj.name+'.obj')), export_selected_objects=True,
                          export_materials=False, export_uv=False, export_normals=True)
    bpy.ops.export_scene.gltf(filepath=str(RUN/'exports'/(obj.name+'.glb')), use_selection=True,
                              export_materials='NONE', export_texcoords=False)


def build(source, recipes):
    inp = np.load(RUN/'meshes'/(source+'.npz'))
    input_v,input_f=inp['v'],inp['f']
    reference_source='pixal_high' if source=='pixal_outer' else source
    ref=np.load(RUN/'meshes'/(reference_source+'.npz'))
    ref_v, ref_f = ref['v'],ref['f']
    ref_bvh = BVHTree.FromPolygons(ref_v.tolist(), ref_f.tolist(), all_triangles=True)
    ref_samples, _ = sample_surface(ref_v, ref_f)
    path = RUN / (source+'_experiments.json')
    results = json.loads(path.read_text()) if path.exists() else {}
    for recipe in recipes:
        name = source.replace('_high','') + '__' + recipe['id']
        if name in results:
            continue
        t = time.perf_counter()
        obj = make_obj(name, input_v, input_f)
        try:
            apply_recipe(obj, recipe)
            build_seconds = time.perf_counter()-t
            result = {'source':source,'reference_source':reference_source,'recipe':recipe,'build_seconds':build_seconds,
                      **stats(obj), **error_metrics(obj,ref_v,ref_f,ref_bvh,ref_samples)}
            save_mesh(obj)
            render(obj)
            export_obj(obj)
            results[name] = result
            emit(candidate=name,triangles=result['triangles'],rms=result['distance_rms_height_pct'],seconds=build_seconds)
        except Exception as e:
            import traceback
            traceback.print_exc()
            results[name] = {'source':source,'recipe':recipe,'error':repr(e)}
        # A run should be scheduled serially per source. Merge the latest completed
        # records as well, so separate method batches do not discard other results.
        latest=json.loads(path.read_text()) if path.exists() else {}
        latest[name]=results[name]
        results=latest
        path.write_text(json.dumps(results,indent=2))
        drop(obj)
        gc.collect()


def apply_recipe(obj, recipe):
    method = recipe['method']
    if method == 'collapse':
        decimate(obj,recipe['target'])
    elif method == 'planar':
        dissolve(obj,recipe['angle'])
        if recipe.get('target'):
            decimate(obj,recipe['target'])
    elif method == 'pre_planar':
        decimate(obj,20000)
        dissolve(obj,recipe['angle'])
        if recipe.get('target'):
            decimate(obj,recipe['target'])
    elif method == 'smooth_collapse':
        decimate(obj,20000)
        active(obj)
        mod = obj.modifiers.new('Denoise','SMOOTH')
        mod.factor = recipe.get('factor',.5)
        mod.iterations = recipe.get('iterations',3)
        bpy.ops.object.modifier_apply(modifier=mod.name)
        decimate(obj,recipe['target'])
    elif method in ('voxel_collapse','voxel_patch','voxel_quad'):
        active(obj)
        obj.data.remesh_voxel_size = recipe['voxel_size']
        obj.data.use_remesh_preserve_volume = True
        bpy.ops.object.voxel_remesh()
        if method == 'voxel_patch':
            from planar_patches import planarize
            decimate(obj,6000)
            v,f=arrays(obj)
            v,meta=planarize(v,f,angle=recipe['angle'],tolerance=recipe['tolerance'])
            obj.data.vertices.foreach_set('co',v.ravel())
            obj.data.update()
            recipe['patch_result']=meta
        if method == 'voxel_quad':
            decimate(obj,20000)
            active(obj)
            bpy.ops.object.quadriflow_remesh(use_mesh_symmetry=False,use_preserve_sharp=True,
                    use_preserve_boundary=True,smooth_normals=False,mode='FACES',target_faces=recipe['quads'],seed=17)
        else:
            decimate(obj,recipe['target'])
    elif method == 'quadriflow':
        decimate(obj,20000)
        active(obj)
        bpy.ops.object.quadriflow_remesh(use_mesh_symmetry=False,use_preserve_sharp=True,
                                        use_preserve_boundary=True, smooth_normals=False,
                                        mode='FACES',target_faces=recipe['quads'],seed=17)
    elif method == 'template_fit':
        ref_v,ref_f=arrays(obj)
        tree=BVHTree.FromPolygons(ref_v.tolist(),ref_f.tolist(),all_triangles=True)
        template=np.load(RUN/'meshes/hyper_low.npz')
        v,f=template['v'].copy(),template['f'].copy()
        # Regularize the displacement field instead of smoothing the character itself.
        neighbors=[set() for _ in v]
        for face in f:
            for i in face:
                neighbors[i].update(int(j) for j in face if j!=i)
        total_move=0.0
        for _ in range(3):
            target=np.array([tree.find_nearest(Vector(p))[0] for p in v])
            delta=target-v
            smooth=np.array([np.mean(delta[sorted(ns)],axis=0) for ns in neighbors])
            step=.8*(.85*delta+.15*smooth)
            v+=step
            total_move+=float(np.max(np.linalg.norm(step,axis=1)))
        old=obj.data
        mesh=bpy.data.meshes.new(obj.name+'_template')
        mesh.from_pydata(v.tolist(),[],f.tolist())
        mesh.update()
        obj.data=mesh
        bpy.data.meshes.remove(old)
        recipe['template']='hyper_low; transferred topology, not independent retopology'
        recipe['max_step_sum']=total_move
    elif method == 'patch':
        from planar_patches import planarize
        decimate(obj,recipe.get('pre_target',6000))
        v, f = arrays(obj)
        new_v, patch_meta = planarize(v,f, angle=recipe['angle'], tolerance=recipe['tolerance'])
        obj.data.vertices.foreach_set('co',new_v.ravel())
        obj.data.update()
        recipe['patch_result'] = patch_meta
        decimate(obj,recipe['target'])
    elif method == 'outer_collapse' or method == 'outer_patch':
        from outer_shells import extract_outer
        v,f=arrays(obj)
        v,f,meta=extract_outer(v,f)
        old=obj.data
        mesh=bpy.data.meshes.new(obj.name+'_outer')
        mesh.from_pydata(v.tolist(),[],f.tolist())
        mesh.update()
        obj.data=mesh
        bpy.data.meshes.remove(old)
        recipe['outer_result']=meta
        if method == 'outer_patch':
            from planar_patches import planarize
            decimate(obj,6000)
            v,f=arrays(obj)
            v,patch_meta=planarize(v,f,angle=recipe['angle'],tolerance=recipe['tolerance'])
            obj.data.vertices.foreach_set('co',v.ravel())
            obj.data.update()
            recipe['patch_result']=patch_meta
        decimate(obj,recipe['target'])
    else:
        raise ValueError(method)


def main():
    global RUN
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=['analyze','build'])
    parser.add_argument('--source',default='pixal_high')
    parser.add_argument('--recipes',default='recipes.json')
    parser.add_argument('--run',default='results/run_01')
    args = parser.parse_args(sys.argv[sys.argv.index('--')+1:])
    RUN = ROOT / args.run
    for sub in ['sources','meshes','renders','exports']:
        (RUN/sub).mkdir(parents=True,exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if args.command == 'analyze':
        analyze()
    else:
        build(args.source,json.loads((ROOT/args.recipes).read_text()))


if __name__=='__main__':
    main()
