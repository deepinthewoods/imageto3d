"""Test cancelling exact opposite-oriented duplicate face pairs after QEM.

This can remove a folded zero-thickness fin, so it is a NEW candidate that must
pass the full geometry/silhouette/boundary gate. Never edits the source mesh.
"""
from pathlib import Path
import sys,json
import numpy as np
from mathutils.bvhtree import BVHTree
ROOT=Path(__file__).resolve().parent;sys.path.insert(0,str(ROOT))
from adaptive_lod04 import measure,compact
from build_fit_review02 import samples
OUT=ROOT/"results/armor_04/adaptive_lod"

def cancel_pairs(v,f):
    _,inv,count=np.unique(np.sort(f,axis=1),axis=0,return_inverse=True,return_counts=True)
    keep=np.ones(len(f),bool)
    for group in np.flatnonzero(count==2):
        ids=np.flatnonzero(inv==group);a,b=f[ids]
        na=np.cross(v[a[1]]-v[a[0]],v[a[2]]-v[a[0]])
        nb=np.cross(v[b[1]]-v[b[0]],v[b[2]]-v[b[0]])
        if na@nb<0:keep[ids]=False
    return keep

def main():
    report=json.loads((OUT/"candidates.json").read_text())
    total=0
    for case,item in report["cases"].items():
        folder=OUT/case;r=np.load(folder/"reference.npz")
        ref=BVHTree.FromPolygons(r["v"].tolist(),r["f"].tolist(),all_triangles=True)
        refpts=samples(r["v"],r["f"],5000)
        for name,old in list(item["candidates"].items()):
            if "_cancel_pairs" in name or "vsa_" in name or old.get("external_reference_only"):continue
            if old.get("structural_pass",True):continue
            d=np.load(folder/(name+".npz"));v,f=d["v"],d["f"]
            keep=cancel_pairs(v,f)
            if keep.all() or not keep.any():continue
            cv,cf=compact(v,f[keep]);new=name+"_cancel_pairs"
            np.savez_compressed(folder/(new+".npz"),v=cv,f=cf)
            score=measure(cv,cf,ref,refpts)
            score.update(triangles=len(cf),parent_candidate=name,
                         opposite_duplicate_pairs_cancelled=int((~keep).sum()//2),
                         note="Exact opposite-winding coincident face pairs removed. Revalidated as a separate candidate; source untouched.")
            item["candidates"][new]=score;total+=1
        (OUT/"candidates.json").write_text(json.dumps(report,indent=2))
        print("CANCELLED_PAIR_CANDIDATES",case,total,flush=True)
if __name__=="__main__":main()
