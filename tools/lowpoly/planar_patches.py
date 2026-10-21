"""Experimental connected plane clustering and bounded vertex projection.

This is a small prototype, not an implementation of CGAL VSA. A seed's normal
limits angular drift; an area-weighted proxy limits distance. No global normal
binning: spatially disconnected regions cannot become one patch.
"""
import math
import numpy as np


def planarize(v, f, angle=15, tolerance=.002):
    from lowpoly_lab import geom, adjacency
    centers, normals, areas = geom(v, f)
    _, _, a, b, _ = adjacency(f)
    neighbors = [[] for _ in f]
    for x, y in zip(a, b):
        neighbors[x].append(y)
        neighbors[y].append(x)
    labels = np.full(len(f),-1,dtype=np.int32)
    patches = []
    cos_max = math.cos(math.radians(angle))
    for seed in np.argsort(-areas,kind='stable'):
        if labels[seed] >= 0:
            continue
        region = len(patches)
        labels[seed] = region
        members = [seed]
        total = areas[seed]
        nc = normals[seed]*total
        cc = centers[seed]*total
        queue = list(neighbors[seed])
        rejected = set()
        while queue:
            idx = queue.pop()
            if labels[idx] >= 0 or idx in rejected:
                continue
            normal = nc/np.linalg.norm(nc)
            center = cc/total
            # Test all three vertices, not just the centroid of a possibly long face.
            if (normals[idx]@normals[seed] < cos_max or
                    np.max(np.abs((v[f[idx]]-center)@normal)) > tolerance):
                rejected.add(idx)
                continue
            labels[idx] = region
            members.append(idx)
            total += areas[idx]
            nc += normals[idx]*areas[idx]
            cc += centers[idx]*areas[idx]
            queue.extend(neighbors[idx])
        # Least-squares plane fit with area-weighted face corner samples.
        points = v[f[members]].reshape(-1,3)
        weights = np.repeat(areas[members],3)
        center = np.average(points,axis=0,weights=weights)
        delta = points-center
        cov = (delta*weights[:,None]).T@delta/weights.sum()
        _, vectors = np.linalg.eigh(cov)
        normal = vectors[:,0]
        if normal@nc < 0:
            normal = -normal
        patches.append((normal, float(normal@center), len(members)))
    incident = [set() for _ in v]
    for face,label in zip(f,labels):
        for vi in face:
            incident[vi].add(label)
    out = v.copy()
    max_move = tolerance*2
    for vi, groups in enumerate(incident):
        if not groups:
            continue
        groups = sorted(groups)
        ns = np.array([patches[g][0] for g in groups])
        ds = np.array([patches[g][1] for g in groups])
        # Projection toward intersections, with a tiny tether in unconstrained directions.
        step = np.linalg.solve(ns.T@ns + np.eye(3)*.02, ns.T@(ds-ns@v[vi]))
        length = np.linalg.norm(step)
        if length > max_move:
            step *= max_move/length
        out[vi] += step
    # Avoid face flips/collapses by backing off the whole displacement if necessary.
    factor = 1.0
    original_cross = np.cross(v[f[:,1]]-v[f[:,0]],v[f[:,2]]-v[f[:,0]])
    for _ in range(10):
        trial = v + (out-v)*factor
        cross = np.cross(trial[f[:,1]]-trial[f[:,0]],trial[f[:,2]]-trial[f[:,0]])
        if np.all(np.einsum('ij,ij->i',cross,original_cross) > 0):
            out = trial
            break
        factor *= .5
    else:
        out = v.copy()
        factor = 0.0
    return out, {'patch_count':len(patches),'patch_faces_p50_p90':np.percentile([p[2] for p in patches],[50,90]).tolist(),
                 'max_displacement':float(np.linalg.norm(out-v,axis=1).max()),'flip_backoff_factor':factor}
