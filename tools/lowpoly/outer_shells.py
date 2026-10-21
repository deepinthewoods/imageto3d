"""Exploratory filter for this input's reversed internal shell components.

Not a general-purpose solid repair. Signed volume is only decisive for closed,
consistently wound components; the audit and output topology must be reviewed.
"""
import numpy as np


def extract_outer(v,f):
    from lowpoly_lab import geom,adjacency
    _,_,a,b,_=adjacency(f)
    parent=np.arange(len(f))
    def find(x):
        while parent[x]!=x:
            parent[x]=parent[parent[x]]
            x=parent[x]
        return x
    for x,y in zip(a,b):
        x,y=find(x),find(y)
        if x!=y: parent[x]=y
    roots=np.array([find(i) for i in range(len(f))])
    _,labels=np.unique(roots,return_inverse=True)
    vols=np.einsum('ij,ij->i',v[f[:,0]],np.cross(v[f[:,1]],v[f[:,2]]))/6
    comp_vol=np.bincount(labels,weights=vols)
    keep=comp_vol[labels]>1e-7
    ff=f[keep]
    used,remap=np.unique(ff,return_inverse=True)
    return v[used],remap.reshape(-1,3),{
        'input_triangles':len(f),'retained_triangles':len(ff),
        'retained_components':int((comp_vol>1e-7).sum()),
        'dropped_components':int((comp_vol<=1e-7).sum()),
        'caution':'Signed-volume heuristic on manifold-edge components; not a validated general repair.'}
