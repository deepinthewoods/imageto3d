"""Generate error-limited LOD candidates: triangle count is always an output.

meshoptimizer's target index count is zero. Its internal quadric error is only
a proposal criterion; independent surface/silhouette gates select final LODs.
Order-of-operations controls compare against exactly the same armor reference.
"""
from pathlib import Path
import sys,json,ctypes as C,time
import bpy,numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree
ROOT=Path(__file__).resolve().parent;sys.path.insert(0,str(ROOT))
import lowpoly_lab as lab
from planar_patches import planarize
from build_fit_review02 import samples
OUT=ROOT/"results/armor_04/adaptive_lod"
LIB=C.CDLL(str(ROOT/"lib/libmeshoptimizer.so"))
FN=LIB.meshopt_simplifyWithAttributes;P=C.c_void_p;S=C.c_size_t
FN.argtypes=[P,P,S,P,S,S,P,S,P,S,P,S,C.c_float,C.c_uint,P];FN.restype=S

def compact(v,f):
    ids,inv=np.unique(f,return_inverse=True);return v[ids],inv.reshape(-1,3)
def lock_vertices(f,labels=None):
    e=np.sort(np.concatenate([f[:,[0,1]],f[:,[1,2]],f[:,[2,0]]]),axis=1)
    unique,inv,count=np.unique(e,axis=0,return_inverse=True,return_counts=True)
    boundary=count==1
    if labels is not None:
        lo=np.full(len(unique),2);hi=np.zeros(len(unique),int)
        np.minimum.at(lo,inv,np.tile(labels.astype(int),3));np.maximum.at(hi,inv,np.tile(labels.astype(int),3))
        boundary|=lo!=hi
    return np.unique(unique[boundary])
def simplify(v,f,error,locked):
    v=np.ascontiguousarray(v,np.float32);f=np.ascontiguousarray(f,np.uint32)
    locks=np.zeros(len(v),np.uint8);locks[locked]=1
    dest=np.empty(f.size,np.uint32);result=C.c_float()
    n=FN(dest.ctypes.data,f.ctypes.data,f.size,v.ctypes.data,len(v),12,
         None,0,None,0,locks.ctypes.data,0,error,4,C.byref(result))
    return (*compact(v,dest[:n].reshape(-1,3)),float(result.value))
def measure(v,f,reference,refpts):
    tree=BVHTree.FromPolygons(v.tolist(),f.tolist(),all_triangles=True)
    pts,ids=lab.sample_surface(v,f,5000,seed=940)
    forward=[reference.find_nearest(Vector(p)) for p in pts]
    reverse=[tree.find_nearest(Vector(p)) for p in refpts]
    dist=np.array([x[3] for x in forward]+[x[3] for x in reverse])
    _,norm,_=lab.geom(v,f);hn=np.array([x[1] for x in forward])
    angle=np.degrees(np.arccos(np.clip((norm[ids]*hn).sum(1),-1,1)))
    return {"sampled_surface_rms":float(np.sqrt((dist*dist).mean())),
            "sampled_surface_p95":float(np.percentile(dist,95)),
            "sampled_surface_max":float(dist.max()),"normal_error_p90_degrees":float(np.percentile(angle,90))}
def main():
    OUT.mkdir(parents=True,exist_ok=True);start=time.time()
    base=np.load(ROOT/"results/armor_01/cache/base_clean.npz");height=float(np.ptp(base["v"][:,2]))
    cases={"pixal_base":(base["v"],base["f"],None,"base")}
    authored=np.load(ROOT/"results/armor_02/authored_base.npz")
    cases["authored_base"]=(authored["v"],authored["f"],None,"base")
    hyper=np.load(ROOT/"results/run_01/meshes/hyper_high.npz")
    hv=hyper["v"]*height/2;hv[:,2]-=height/2
    cases["hyper_high"]=(hv,hyper["f"],None,"base")
    for module in ["body_armor","right_gauntlet_boot"]:
        d=np.load(ROOT/"results/armor_04"/("control_"+module+"_truth.npz"))
        mask=d["labels"]>=4
        cases[module+"_remove_first"]=(*compact(d["v"],d["f"][mask]),None,"armor")
        cases[module+"_reduce_first"]=(d["v"],d["f"],mask,"armor")
    # Explicit synthetic regression asset: long narrow protrusions on ONE
    # shoulder. It is not a proposed outfit and does not change any source model.
    sv,sf,_,_=cases["body_armor_remove_first"]
    centers,normals,areas=lab.geom(sv,sf)
    possible=np.flatnonzero((centers[:,0]>.08)&(centers[:,2]>.18)&(normals[:,2]>.2))
    chosen=[]
    for i in possible[np.argsort(-areas[possible])]:
        if all(np.linalg.norm(centers[i]-centers[j])>.022 for j in chosen):chosen.append(int(i))
        if len(chosen)>=8:break
    extra=[];newv=list(sv)
    for k,i in enumerate(chosen):
        tip=centers[i]+normals[i]*height*(.04+.012*k);idx=len(newv);newv.append(tip)
        a,b,c=sf[i];extra.extend([[a,b,idx],[b,c,idx],[c,a,idx]])
    keep=np.ones(len(sf),bool);keep[chosen]=False
    cases["body_armor_asymmetric_spike_stress"]=(np.asarray(newv),np.concatenate([sf[keep],np.array(extra)]),None,"synthetic_stress")
    report={"body_height":height,"target_triangles":None,"candidate_generator":"meshoptimizer, zero target count, absolute quadric-error limits",
            "important":"Internal QEM error is not a Hausdorff guarantee. Final selection additionally gates independently sampled surface error and six-view silhouette deviation.",
            "scope":"Current examples and controlled semantic masks; no elaborate new outfit ground truth yet. Boundary locks can be expensive; inspect results.",
            "cases":{}}
    if "--case" in sys.argv:
        selected=sys.argv[sys.argv.index("--case")+1:]
        cases={k:v for k,v in cases.items() if k in selected}
        if (OUT/"candidates.json").exists():report=json.loads((OUT/"candidates.json").read_text())
    for case,(v,f,mask,kind) in cases.items():
        folder=OUT/case;folder.mkdir(exist_ok=True)
        rv,rf=compact(v,f[mask]) if mask is not None else (v,f)
        np.savez_compressed(folder/"reference.npz",v=rv,f=rf)
        ref=BVHTree.FromPolygons(rv.tolist(),rf.tolist(),all_triangles=True)
        refpts=samples(rv,rf,5000)
        fulltree=BVHTree.FromPolygons(v.tolist(),f.tolist(),all_triangles=True)
        locked=lock_vertices(f,mask)
        data={"source_triangles":len(f),"reference_armor_triangles":len(rf),"order":"reduce_then_remove" if mask is not None else "already_extracted_or_base","candidates":{}}
        methods=["qem"] if case=="hyper_high" else ["qem","boundary_qem"] if case=="pixal_base" else ["qem","boundary_qem","planar_qem"]
        for fraction in [.0005,.001,.002,.004,.008,.016]:
            error=height*fraction
            for method in methods:
                name=f"{method}_error{fraction:g}";path=folder/(name+".npz")
                x=v;meta={}
                if method=="planar_qem":
                    x,meta=planarize(v,f,angle=15,tolerance=error*.3)
                    x[locked]=v[locked]
                cv,cf,reported=simplify(x,f,error,locked if method!="qem" else [])
                if not len(cf):continue
                before=len(cf)
                if mask is not None:
                    bary=np.array([[1/3]*3,[.7,.15,.15],[.15,.7,.15],[.15,.15,.7]])
                    pts=np.einsum("fvc,sv->fsc",cv[cf],bary)
                    ids=np.array([fulltree.find_nearest(Vector(p))[2] for p in pts.reshape(-1,3)]).reshape(len(cf),-1)
                    keep=mask[ids].mean(1)>=.5
                    cv,cf=compact(cv,cf[keep])
                if not len(cf):continue
                np.savez_compressed(path,v=cv,f=cf)
                score=measure(cv,cf,ref,refpts)
                score.update(triangles=len(cf),triangles_before_body_removal=before,
                             absolute_error_setting=error,reported_quadric_error=reported,
                             locked_vertex_count=len(locked) if method!="qem" else 0,planar_preprocess=meta)
                data["candidates"][name]=score
            print("ADAPTIVE",case,fraction,flush=True)
        if case=="hyper_high":
            supplied=np.load(ROOT/"results/run_01/meshes/hyper_low.npz")
            lv=supplied["v"]*height/2;lv[:,2]-=height/2;lf=supplied["f"]
            np.savez_compressed(folder/"provided_hyper_low.npz",v=lv,f=lf)
            score=measure(lv,lf,ref,refpts)
            score.update(triangles=len(lf),external_reference_only=True,
                note="Supplied Hyper3D low mesh; comparison only, excluded from automatic candidate selection. Pair not proven to be a literal high-to-low decimation.")
            data["candidates"]["provided_hyper_low"]=score
        report["cases"][case]=data
        (OUT/"candidates.json").write_text(json.dumps(report,indent=2))
    report["seconds"]=time.time()-start;(OUT/"candidates.json").write_text(json.dumps(report,indent=2))
if __name__=="__main__":main()
