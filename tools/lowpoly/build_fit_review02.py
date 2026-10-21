"""Build editable fitting comparisons and measure clearance in background Blender.

Uses saved task backup; never changes the live/source armor_parts.blend.
"""
from pathlib import Path
import sys,json,math
import bpy,numpy as np
from mathutils import Matrix,Vector
from mathutils.bvhtree import BVHTree
ROOT=Path(__file__).resolve().parent;OUT=ROOT/'results/armor_02/fitting'
sys.path.insert(0,str(ROOT))
import lowpoly_lab as lab
SOURCE=ROOT/'results/armor_02/live_before_experiments.blend'

def signed(tree,v):
    hits=[tree.find_nearest(Vector(p)) for p in v]
    near=np.array([h[0] for h in hits]);normal=np.array([h[1] for h in hits])
    return np.einsum('ij,ij->i',v-near,normal),normal

def clearance(v,f,tree,gap=.003,steps=18):
    v=v.copy()
    # Resolve vertices and face interiors. Face-center displacement is distributed
    # to its three vertices, retaining topology and UVs. No target shrinkwrap.
    for _ in range(steps):
        d,n=signed(tree,v);v+=n*np.clip(gap-d,0,.012)[:,None]*.8
        p=v[f].mean(axis=1);d,n=signed(tree,p);delta=n*np.clip(gap-d,0,.012)[:,None]
        sums=np.zeros_like(v);counts=np.zeros(len(v))
        for i in range(3):np.add.at(sums,f[:,i],delta);np.add.at(counts,f[:,i],(d<gap).astype(float))
        v+=sums/np.maximum(counts[:,None],1)*.8
    return v

def samples(v,f,n=4000):
    rng=np.random.default_rng(183);tri=v[f];area=np.linalg.norm(np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]),axis=1)
    ids=rng.choice(len(f),n,p=area/area.sum());t=tri[ids];u=rng.random((n,2));u[u.sum(1)>1]=1-u[u.sum(1)>1]
    return t[:,0]+u[:,0,None]*(t[:,1]-t[:,0])+u[:,1,None]*(t[:,2]-t[:,0])

def main():
    bpy.ops.wm.open_mainfile(filepath=str(SOURCE))
    names=list(json.loads((OUT/'source_parts.json').read_text()));originals={n:bpy.data.objects[n] for n in names}
    base=bpy.data.objects['Base_Human_Male'];base.hide_set(False);base.hide_render=False
    bx=np.load(ROOT/'results/armor_01/cache/base_clean.npz');tree=BVHTree.FromPolygons(bx['v'].tolist(),bx['f'].tolist(),all_triangles=True)
    height=float(np.ptp(bx['v'][:,2]))
    methods=['unfitted','uniform_height','axis_bbox','icp_similarity','icp_affine','piecewise_rigid','piecewise_axial','rbf_landmarks','piecewise_clearance','rbf_clearance']
    if (OUT/'affine_clearance/cage_parameters.json').exists():methods.append('affine_clearance')
    if (OUT/'arap_clearance/parameters.json').exists():methods.append('arap_clearance')
    for method,parent in [('piecewise_clearance','piecewise_axial'),('rbf_clearance','rbf_landmarks')]:
        (OUT/method).mkdir(exist_ok=True)
        for name in names:
            d=np.load(OUT/parent/(name+'.npz'));v=clearance(d['v'],d['f'],tree)
            np.savez_compressed(OUT/method/(name+'.npz'),v=v,f=d['f'])
    collections={};metrics={}
    for method in methods:
        col=bpy.data.collections.new('Fit__'+method);bpy.context.scene.collection.children.link(col);collections[method]=col
        metrics[method]={'parts':{}}
        whole=[]
        for name in names:
            data=np.load(OUT/method/(name+'.npz'));v,f=data['v'],data['f']
            obj=originals[name].copy();obj.data=originals[name].data.copy();col.objects.link(obj);obj.name=method+'__'+name
            obj.matrix_world=Matrix.Identity(4);obj.data.vertices.foreach_set('co',v.astype(np.float32).ravel());obj.data.update()
            obj['fit_method']=method;obj['source_part']=name
            source=np.load(OUT/(name+'_source.npz'))['v'];edges=np.sort(np.concatenate([f[:,[0,1]],f[:,[1,2]],f[:,[2,0]]]),axis=1);edges=np.unique(edges,axis=0)
            ratio=np.linalg.norm(v[edges[:,1]]-v[edges[:,0]],axis=1)/np.maximum(np.linalg.norm(source[edges[:,1]]-source[edges[:,0]],axis=1),1e-10)
            pts=samples(v,f,2500);d,_=signed(tree,pts);whole.append(d)
            tri=v[f];area=np.linalg.norm(np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]),axis=1)
            metrics[method]['parts'][name]={'triangles':len(f),'penetration_fraction_below_minus_001':float((d<-.001).mean()),
                'signed_gap_p05_p50_p95':np.percentile(d,[5,50,95]).tolist(),'edge_length_ratio_p05_p50_p95':np.percentile(ratio,[5,50,95]).tolist(),
                'degenerate_triangles':int((area<1e-12).sum()),'max_vertex_movement':float(np.linalg.norm(v-source,axis=1).max())}
        d=np.concatenate(whole)
        metrics[method]['aggregate_equal_samples_per_part']={'penetration_fraction':float((d<-.001).mean()),
            'farther_than_2pct_body_height_fraction':float((d>.02*height).mean()),'signed_gap_p05_p50_p95':np.percentile(d,[5,50,95]).tolist()}
        bpy.ops.object.select_all(action='DESELECT')
        for o in col.objects:o.select_set(True)
        bpy.context.view_layer.objects.active=next(iter(col.objects))
        bpy.ops.export_scene.gltf(filepath=str(OUT/method/'armor.glb'),use_selection=True)
        col.hide_render=True;col.hide_viewport=True
    # The originals are preserved in SOURCE; remove only their copies in this review.
    for o in originals.values():bpy.data.objects.remove(o,do_unlink=True)
    (OUT/'fit_metrics.json').write_text(json.dumps(metrics,indent=2))
    lab.setup_render();scene=bpy.context.scene;scene.render.resolution_x=640;scene.render.resolution_y=640
    scene.display.shading.color_type='TEXTURE';scene.camera.data.ortho_scale=1.05
    views={'front':(0,-3,0),'three_quarter':(2,-3,.9),'back':(0,3,0),'side':(3,0,0),'top':(0,0,3)}
    for method,col in collections.items():
        col.hide_render=False;col.hide_viewport=False
        for view,pos in views.items():
            if (OUT/method/(view+'.png')).exists():continue
            scene.camera.location=pos
            scene.camera.rotation_euler=(Vector((0,0,0))-scene.camera.location).to_track_quat('-Z','Y').to_euler()
            if view=='top':scene.camera.rotation_euler=(0,0,0)
            scene.render.filepath=str(OUT/method/(view+'.png'));bpy.ops.render.render(write_still=True)
        col.hide_render=True;col.hide_viewport=True
    preferred='arap_clearance' if 'arap_clearance' in collections else 'rbf_clearance'
    collections[preferred].hide_render=False;collections[preferred].hide_viewport=False
    # Exact top guide of armor alone, using the base library's top camera/FOV.
    collections[preferred].hide_render=True
    collections['rbf_clearance'].hide_render=False
    base.hide_render=True
    doc=json.loads((ROOT.parent/'top_views_20261020/human_male/transforms.json').read_text())
    scene.camera.data.type='PERSP';scene.camera.data.sensor_fit='HORIZONTAL';scene.camera.data.angle_x=doc['camera_angle_x']
    scene.camera.matrix_world=Matrix(doc['frames'][0]['transform_matrix']);scene.render.resolution_x=scene.render.resolution_y=1024
    for module in ['body_armor','right_gauntlet_boot']:
        for o in collections['rbf_clearance'].objects:o.hide_render=(('gauntlet' in o['source_part'] or 'boot' in o['source_part'])!=(module=='right_gauntlet_boot'))
        scene.render.filepath=str(OUT/(module+'_top_guide.png'));bpy.ops.render.render(write_still=True)
    for o in collections['rbf_clearance'].objects:o.hide_render=False
    collections['rbf_clearance'].hide_render=(preferred!='rbf_clearance')
    collections[preferred].hide_render=False
    base.hide_render=False
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type=='VIEW_3D':
                s=area.spaces.active;s.shading.type='SOLID';s.shading.color_type='TEXTURE';s.region_3d.view_perspective='ORTHO'
                s.region_3d.view_location=(0,0,0);s.region_3d.view_distance=1.3;s.region_3d.view_rotation=Vector((.7,-1.6,.55)).to_track_quat('Z','Y')
    bpy.ops.object.select_all(action='DESELECT');bpy.context.view_layer.objects.active=None
    bpy.ops.file.pack_all();bpy.ops.wm.save_as_mainfile(filepath=str(OUT/'fitting_experiments.blend'))
    print('FIT_REVIEW_DONE',flush=True)

if __name__=='__main__':main()
