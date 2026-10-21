"""Error-limited reduction study. No requested triangle counts at any stage.

Blender -- build CASE | render CASE; all derivatives stay in tripo_adaptive_01.
The simplifier's QEM threshold proposes meshes; independent checks select them.
"""
from pathlib import Path
import sys,os,json,time,ctypes as C,math,hashlib
import bpy,bmesh,numpy as np
from mathutils.bvhtree import BVHTree
ROOT=Path(__file__).resolve().parent;sys.path.insert(0,str(ROOT))
import lowpoly_lab as lab
import tripo_reduce as tripo
from adaptive_lod04 import LIB,FN,compact,measure
from adaptive_cleanup04 import cancel_pairs
OUT=ROOT/'results/tripo_adaptive_01'
CASES={'previous':'tripo_01','plain_paper':'tripo_02','h3_dim':'tripo_03','h31_dim':'tripo_04'}
ERRORS=[.00025,.0005,.001,.002,.004,.008,.016]

def dump(path,data):path.write_text(json.dumps(data,indent=2)+'\n')

def reference(case):
    if case in CASES:
        folder=ROOT/'results'/CASES[case];d=np.load(folder/'meshes/source.npz');info=json.loads((folder/'source.json').read_text())
        return d['v'],d['f'],info
    name='authored_base' if case=='authored_control' else 'body_armor_asymmetric_spike_stress'
    path=ROOT/'results/armor_04/adaptive_lod'/name/'reference.npz';d=np.load(path)
    v,f=d['v'],d['f'];lo,hi=v.min(0),v.max(0);origin=(lo+hi)/2;origin[2]=lo[2];scale=2/(hi[2]-lo[2]);v=(v-origin)*scale
    obj=lab.make_obj('control',v,f);stats=lab.stats(obj);lab.drop(obj)
    return v,f,{'source':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'normalization_scale':float(scale),'normalization_origin_rotated':origin.tolist(),'rotation_to_review':np.eye(3).tolist(),'stats':stats,'audit':tripo.audit(v,f),'note':'Existing authored/synthetic detail control, not a new Tripo generation.'}

def normals(v,f):
    cross=np.cross(v[f[:,1]]-v[f[:,0]],v[f[:,2]]-v[f[:,0]])
    n=np.zeros(v.shape)
    for i in range(3):np.add.at(n,f[:,i],cross)
    n/=np.maximum(np.linalg.norm(n,axis=1)[:,None],1e-20)
    return np.ascontiguousarray(n,np.float32)

def reduce_error(v,f,fraction,weight=0):
    v=np.ascontiguousarray(v,np.float32);f=np.ascontiguousarray(f,np.uint32)
    dest=np.empty(f.size,np.uint32);result=C.c_float()
    n=normals(v,f) if weight else None;w=np.full(3,weight,np.float32) if weight else None
    count=FN(dest.ctypes.data,f.ctypes.data,f.size,v.ctypes.data,len(v),12,
        n.ctypes.data if n is not None else None,12 if weight else 0,
        w.ctypes.data if w is not None else None,3 if weight else 0,None,
        0,2*fraction,4,C.byref(result))
    if count==0:raise ValueError('No triangles survived error limit')
    return (*compact(v,dest[:count].reshape(-1,3)),float(result.value))

def local_repair(v,f):
    """Bounded repair proposal: only tiny defective patches/holes, then recheck."""
    obj=lab.make_obj('local_repair',v,f);bm=bmesh.new();bm.from_mesh(obj.data)
    bmesh.ops.recalc_face_normals(bm,faces=list(bm.faces));removed=filled=0
    for iteration in range(2):
        bad=[e for e in bm.edges if len(e.link_faces)>2]
        patches=[]
        for e in bad:
            faces=set(e.link_faces);points=np.array([tuple(v.co) for face in faces for v in face.verts])
            if np.linalg.norm(points.max(0)-points.min(0))<=.008:patches.append(faces)
        doomed=set().union(*patches) if patches else set()
        if doomed:
            removed+=len(doomed);bmesh.ops.delete(bm,geom=list(doomed),context='FACES_ONLY')
        # Discard only loose wire edges/vertices left by deleting the patch.
        wires=[e for e in bm.edges if not e.link_faces]
        if wires:bmesh.ops.delete(bm,geom=wires,context='EDGES')
        boundary=set(e for e in bm.edges if e.is_boundary)
        while boundary:
            first=boundary.pop();group={first};todo=[first]
            while todo:
                edge=todo.pop()
                for vert in edge.verts:
                    for neighbor in vert.link_edges:
                        if neighbor in boundary:boundary.remove(neighbor);group.add(neighbor);todo.append(neighbor)
            points=np.array([tuple(v.co) for e in group for v in e.verts])
            if len(group)<=32 and np.linalg.norm(points.max(0)-points.min(0))<=.008:
                result=bmesh.ops.holes_fill(bm,edges=list(group),sides=0);filled+=len(result['faces'])
        bmesh.ops.recalc_face_normals(bm,faces=list(bm.faces))
    bm.to_mesh(obj.data);bm.free();obj.data.update();v,f=lab.arrays(obj);lab.drop(obj)
    return (*compact(v,f),{'deleted_defect_faces':removed,'filled_small_holes_faces':filled,'maximum_patch_diagonal_height_fraction':.004})

def build(case,local_only=False):
    folder=OUT/case
    for sub in ['meshes','records','exports','renders']:(folder/sub).mkdir(parents=True,exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    rv,rf,info=reference(case)
    if (folder/'source.json').exists():
        previous=json.loads((folder/'source.json').read_text())
        assert all(previous[k]==info[k] for k in ['sha256','normalization_scale','normalization_origin_rotated','rotation_to_review']), 'Use a new case/output for a changed source or normalization'
    dump(folder/'source.json',info)
    np.savez_compressed(folder/'meshes/source.npz',v=rv,f=rf)
    tree=BVHTree.FromPolygons(rv.tolist(),rf.tolist(),all_triangles=True)
    points,_=lab.sample_surface(rv,rf,10000,seed=940)
    tripo.OUT=folder
    def record(name,v,f,meta):
        if (folder/'records'/(name+'.json')).exists():return
        started=time.monotonic();before=len(f);keep=cancel_pairs(v,f);v,f=compact(v,f[keep])
        obj=lab.make_obj(name,v,f);s=lab.stats(obj);a=tripo.audit(v,f)
        score={**meta,**s,**measure(v,f,tree,points),'audit':a,'opposite_duplicate_faces_removed':before-len(f)}
        score['topology_pass']=bool(a['finite_and_valid_indices'] and not any(a[k] for k in ['boundary_edges','nonmanifold_edges','inconsistent_winding_edges','degenerate_triangles','duplicate_triangles']))
        score['components_preserved']=s['face_components']==info['stats']['face_components']
        score['structural_pass']=score['topology_pass'] and score['components_preserved']
        np.savez_compressed(folder/'meshes'/(name+'.npz'),v=v,f=f)
        score['export_triangles']=tripo.export_native(obj,info)
        score['structural_pass'] &= score['export_triangles']==len(f)
        score['measure_export_seconds']=time.monotonic()-started
        dump(folder/'records'/(name+'.json'),score);lab.drop(obj)
        print('ADAPTIVE',case,name,len(f),score['structural_pass'],flush=True)
    # Untouched geometric input, and a separately cached fine-voxel proposal.
    for base in (['local'] if local_only else ['raw','voxel2']):
        cache=folder/'meshes'/(base+'_input.npz')
        if base=='raw':v,f=rv,rf
        elif cache.exists():d=np.load(cache);v,f=d['v'],d['f']
        elif base=='local':
            pruned=np.load(folder/'meshes/pruned_input.npz');v,f,repair=local_repair(pruned['v'],pruned['f'])
            np.savez_compressed(cache,v=v,f=f)
            dump(folder/'local_repair.json',{**repair,'audit':tripo.audit(v,f),'source_components_policy':json.loads((folder/'component_policy.json').read_text())})
            print('LOCAL_REPAIR',case,len(f),repair,flush=True)
        else:
            obj=lab.make_obj('voxel_input',rv,rf);lab.active(obj)
            obj.data.remesh_voxel_size=.002;obj.data.use_remesh_preserve_volume=True
            bpy.ops.object.voxel_remesh();v,f=lab.arrays(obj);lab.drop(obj)
            np.savez_compressed(cache,v=v,f=f)
            print('PREPARED',case,base,len(f),flush=True)
        for weight in ([0] if base=='raw' else [0,.05] if base=='local' else [0,.5]):
            method=base+('_normal' if weight else '_qem')
            for error in ERRORS:
                name=f'{method}_e{error:g}'
                if (folder/'records'/(name+'.json')).exists():continue
                started=time.monotonic();cv,cf,reported=reduce_error(v,f,error,weight)
                record(name,cv,cf,{'method':method,'error_fraction_of_height':error,'normal_weight':weight,'internal_error_combines_normal_and_position':bool(weight),'reported_quadric_error':reported,'target_triangle_count':None,'generator_seconds':time.monotonic()-started})
        # Angle-based dissolve on an error-limited seed: no polygon count target.
        if base=='voxel2':
            seed=np.load(folder/'meshes/voxel2_qem_e0.00025.npz')
            for angle in [1,5,12]:
                name=f'planar_{angle}deg'
                if (folder/'records'/(name+'.json')).exists():continue
                obj=lab.make_obj(name,seed['v'],seed['f']);lab.active(obj)
                mod=obj.modifiers.new('Angle dissolve','DECIMATE');mod.decimate_type='DISSOLVE';mod.angle_limit=math.radians(angle)
                bpy.ops.object.modifier_apply(modifier=mod.name);cv,cf=lab.arrays(obj);lab.drop(obj)
                record(name,cv,cf,{'method':'angle_dissolve','angle_degrees':angle,'seed_error_fraction_of_height':.00025,'target_triangle_count':None})
    assert hashlib.sha256(Path(info['source']).read_bytes()).hexdigest()==info['sha256']
    dump(folder/'build_complete.json',{'case':case,'source_unchanged':True,'candidates':len(list((folder/'records').glob('*.json'))),'no_fixed_triangle_targets':True})

def render(case):
    folder=OUT/case;data=json.loads((folder/'selection.json').read_text())
    names=['source']+data['visual_candidates'];lab.RUN=folder;lab.VIEWS['top']=(0,0,7)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    for name in dict.fromkeys(names):
        if all((folder/'renders'/f'{name}_{view}.png').exists() for view in lab.VIEWS):continue
        d=np.load(folder/'meshes'/(name+'.npz'));obj=lab.make_obj(name,d['v'],d['f']);lab.render(obj);lab.drop(obj)
    print('RENDERED',case,len(set(names)),flush=True)

if __name__=='__main__':
    args=sys.argv[sys.argv.index('--')+1:]
    if args[0]=='local':build(args[1],local_only=True)
    elif args[0]=='build':build(args[1])
    else:render(args[1])
