"""Surface (geodesic) distances on an edge graph (pure numpy + heapq).

Blender 5.1 ships numpy but no scipy, so the heat method (sparse Poisson solves) is not
available; Dijkstra on the subdivided mesh with diagonal edges (mesh.graph) is used
instead. It overestimates the true surface distance by a few percent at most and never
goes through space, which is what the placement needs.
"""
import heapq

import numpy as np


def _lists(g):
    if '_py' not in g:
        g['_py'] = (g['indptr'].tolist(), g['idx'].tolist(), g['w'].tolist())
    return g['_py']


def dijkstra(g, sources, init=None, cutoff=np.inf, allowed=None):
    """Multi-source shortest paths. `init` gives each source's starting distance (default 0).
    `allowed` (bool per vertex) confines the walk. Unreached vertices are inf."""
    ip, ix, wt = _lists(g)
    dist = [np.inf] * g['n']
    ok = None if allowed is None else np.asarray(allowed).tolist()
    h = []
    for k, s in enumerate(np.atleast_1d(sources).tolist()):
        d0 = 0.0 if init is None else float(np.atleast_1d(init)[k])
        if d0 < dist[s] and (ok is None or ok[s]):
            dist[s] = d0
            h.append((d0, s))
    heapq.heapify(h)
    while h:
        d, u = heapq.heappop(h)
        if d > dist[u]:
            continue
        for e in range(ip[u], ip[u + 1]):
            v = ix[e]
            nd = d + wt[e]
            if nd < dist[v] and nd <= cutoff and (ok is None or ok[v]):
                dist[v] = nd
                heapq.heappush(h, (nd, v))
    return np.array(dist)


class Field:
    """A distance field that sources can be added to one by one (farthest-point sampling).
    Only the part of the surface that gets closer to the new source is touched."""

    def __init__(self, g, allowed=None):
        self.g = g
        self.ip, self.ix, self.wt = _lists(g)
        self.d = [np.inf] * g['n']
        self.np = np.full(g['n'], np.inf)
        self.ok = None if allowed is None else np.asarray(allowed).tolist()

    def add(self, s, d0=0.0, cutoff=np.inf):
        d, ip, ix, wt, ok = self.d, self.ip, self.ix, self.wt, self.ok
        if d0 >= d[s]:
            return
        d[s] = d0
        touched = {s: d0}
        h = [(d0, s)]
        while h:
            du, u = heapq.heappop(h)
            if du > d[u]:
                continue
            for e in range(ip[u], ip[u + 1]):
                v = ix[e]
                nd = du + wt[e]
                if nd < d[v] and nd <= cutoff and (ok is None or ok[v]):
                    d[v] = nd
                    touched[v] = nd
                    heapq.heappush(h, (nd, v))
        idx = np.fromiter(touched.keys(), np.int64, len(touched))
        self.np[idx] = np.fromiter(touched.values(), float, len(touched))


def farthest_points(g, cand, count, spacing=0.0, first=None, mirror=None, rng=None,
                    allowed=None, symmetric_ok=None, gap=None):
    """Farthest-point sampling over candidate vertices (bool mask `cand`).

    Starts at `first` (default: a random candidate from `rng`, else the first). Stops at `count`
    points or when the largest gap falls below `spacing` (0 = ignore). `gap` (per vertex) caps
    a candidate's score by its distance to its own mirror image. `mirror` (vertex -> its mirror
    vertex or -1) makes the sampling mirror-symmetric: a pick off the mid-plane also inserts its
    mirror as a source and counts for two; `symmetric_ok` (bool per vertex) marks the on-plane
    vertices. Returns (vertices picked, paired flags): a paired pick stands for itself and its
    mirror image."""
    cand = np.asarray(cand, bool)
    if not cand.any():
        return [], []
    fld = Field(g, allowed)
    picks, paired = [], []
    total = 0

    def take(v):
        nonlocal total
        has = mirror is not None and mirror[v] >= 0 and mirror[v] != v
        picks.append(int(v))
        paired.append(bool(has))
        fld.add(int(v))
        total += 1
        if has:
            fld.add(int(mirror[v]))
            total += 1

    ids = np.flatnonzero(cand)
    sym = mirror is not None and symmetric_ok is not None
    if sym:
        plane = np.asarray(symmetric_ok, bool)
        if count % 2 == 1 and (cand & plane).any():            # an odd count needs exactly one on-plane pick
            ids = np.flatnonzero(cand & plane)
            first = ids[0] if rng is None else ids[rng.integers(len(ids))]
        cand = cand & ~plane
        if first is None:
            ids = np.flatnonzero(cand)
    if first is None:
        if gap is not None and len(ids):                        # start where a pick is farthest from its own mirror image
            ids = ids[gap[ids] >= gap[ids].max() - 1e-12]
        first = ids[0] if rng is None else ids[rng.integers(len(ids))]
    take(first)
    while total < count:
        score = np.where(cand, fld.np if gap is None else np.minimum(fld.np, gap), -1.0)
        score[~np.isfinite(score)] = -1.0
        v = int(score.argmax())
        if score[v] <= 0 or (spacing > 0 and score[v] < spacing):
            break
        take(v)
    return picks, paired


def nn_distances(g, verts, start=0.02, factor=2.5):
    """Surface distance from each of `verts` to its nearest other one (search radius grows from `start`)."""
    verts = np.asarray(verts, np.int64)
    out = np.full(len(verts), np.nan)
    if len(verts) < 2:
        return out
    for i, v in enumerate(verts):
        cut = start
        for _ in range(12):
            d = geodesic_cut(g, v, cut)
            dd = d[verts].copy()
            dd[i] = np.inf
            if np.isfinite(dd.min()):
                out[i] = dd.min()
                break
            cut *= factor
    return out


def geodesic_cut(g, v, cutoff):
    return dijkstra(g, [v], cutoff=cutoff)
