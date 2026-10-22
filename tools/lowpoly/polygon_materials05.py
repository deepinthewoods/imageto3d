"""Area-weighted texture classification of already simplified Blender polygons.

The original UV texture is queried on its source surface. No segmentation or
material boundary affects simplification. Samples cover tessellated n-gons in
proportion to triangle area, so UV stretching and tessellation density do not
change the intended vote.
"""
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

# NOTE: stale 6-colour tricolor palette of the 2026-10 lab. NOT the pipeline palette: the Tripo pipeline only uses
# TextureSurface / polygon_samples from this module; its colours come from tools/tripo_pipeline/palette.py
# (palette.json beside the base views).
PALETTE = np.array([
    [230, 25, 35], [245, 220, 20], [225, 25, 220],
    [20, 220, 235], [25, 210, 40], [30, 65, 235],
], dtype=float) / 255
NAMES = ["BODY_red", "BODY_head_yellow", "BODY_hand_magenta",
         "BODY_foot_cyan", "ARMOR_green", "ARMOR_blue", "MIXED_review"]


def chroma(rgb):
    return rgb / np.maximum(rgb.sum(axis=-1, keepdims=True), 1e-10)


def classify_samples(rgb):
    distances = np.linalg.norm(chroma(rgb)[:, None] - chroma(PALETTE), axis=-1)
    labels = distances.argmin(axis=1)
    saturation = (rgb.max(axis=1) - rgb.min(axis=1)) / np.maximum(rgb.max(axis=1), 1e-10)
    known = (distances.min(axis=1) < .27) & (saturation > .20) & (rgb.max(axis=1) > .025)
    return labels, known


def radical_inverse(index, base):
    index = np.asarray(index, dtype=np.int64).copy()
    result = np.zeros(len(index))
    factor = 1.0 / base
    while index.any():
        result += (index % base) * factor
        index //= base
        factor /= base
    return result


def polygon_samples(mesh, density=160000, minimum=96, maximum=4096):
    """Deterministic stratified area sampling; returns point and polygon IDs."""
    mesh.calc_loop_triangles()
    v = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.vertices.foreach_get("co", v); v = v.reshape(-1, 3)
    f = np.empty(len(mesh.loop_triangles) * 3, np.int32)
    mesh.loop_triangles.foreach_get("vertices", f); f = f.reshape(-1, 3)
    owner = np.empty(len(f), np.int32)
    mesh.loop_triangles.foreach_get("polygon_index", owner)
    area = np.linalg.norm(np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]]), axis=1) / 2
    polygon_area = np.bincount(owner, weights=area, minlength=len(mesh.polygons))
    counts = np.clip(np.ceil(polygon_area / max(area.sum(), 1e-12) * density), minimum, maximum).astype(int)
    points, owners = [], []
    for pid in range(len(mesh.polygons)):
        ids = np.flatnonzero(owner == pid)
        weights = area[ids]
        if not len(ids) or weights.sum() < 1e-14:
            continue
        n = counts[pid]; seq = np.arange(1, n + 1)
        # Even coverage of the area CDF and low-discrepancy positions inside it.
        cdf = np.cumsum(weights) / weights.sum()
        tri = ids[np.minimum(np.searchsorted(cdf, (seq - .5) / n), len(ids) - 1)]
        s = np.sqrt(radical_inverse(seq, 2)); t = radical_inverse(seq, 3)
        bary = np.stack([1-s, s*(1-t), s*t], axis=1)
        points.append(np.einsum("ni,nij->nj", bary, v[f[tri]]))
        owners.append(np.full(n, pid, np.int32))
    return np.concatenate(points), np.concatenate(owners), polygon_area, counts


class TextureSurface:
    def __init__(self, obj):
        mesh = obj.data; mesh.calc_loop_triangles()
        v = np.empty(len(mesh.vertices) * 3, np.float32)
        mesh.vertices.foreach_get("co", v); self.v = v.reshape(-1, 3)
        f = np.empty(len(mesh.loop_triangles) * 3, np.int32)
        mesh.loop_triangles.foreach_get("vertices", f); self.f = f.reshape(-1, 3)
        loops = np.empty(len(mesh.loop_triangles) * 3, np.int32)
        mesh.loop_triangles.foreach_get("loops", loops)
        uv = np.empty(len(mesh.loops) * 2, np.float32)
        mesh.uv_layers.active.data.foreach_get("uv", uv)
        self.uv = uv.reshape(-1, 2)[loops.reshape(-1, 3)]
        image = None
        for material in mesh.materials:
            if not material or not material.use_nodes: continue
            bs = next((n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
            if bs and bs.inputs["Base Color"].is_linked:
                image = getattr(bs.inputs["Base Color"].links[0].from_node, "image", None)
                if image: break
        if image is None: raise RuntimeError("Direct source base-color texture required")
        w, h = image.size
        pixels = np.empty(w*h*4, np.float32); image.pixels.foreach_get(pixels)
        self.pixels = pixels.reshape(h, w, 4)
        self.tree = BVHTree.FromPolygons(self.v.tolist(), self.f.tolist(), all_triangles=True)
        self.image_name = image.name

    def sample(self, points):
        hits = [self.tree.find_nearest(Vector(p)) for p in points]
        hit = np.array([x[0] for x in hits])
        ids = np.array([x[2] for x in hits], int)
        distance = np.array([x[3] for x in hits])
        tri = self.v[self.f[ids]]
        a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
        e0, e1, q = b-a, c-a, hit-a
        dot = lambda x, y: np.einsum("ij,ij->i", x, y)
        d00, d01, d11 = dot(e0, e0), dot(e0, e1), dot(e1, e1)
        d20, d21 = dot(q, e0), dot(q, e1)
        den = np.maximum(d00*d11-d01*d01, 1e-30)
        u = (d11*d20-d01*d21)/den; v = (d00*d21-d01*d20)/den
        bary = np.clip(np.stack([1-u-v, u, v], axis=1), 0, 1)
        bary /= np.maximum(bary.sum(axis=1, keepdims=True), 1e-12)
        uv = np.einsum("ni,nij->nj", bary, self.uv[ids])
        h, w = self.pixels.shape[:2]
        xy = np.clip(uv, 0, 1) * np.array([w, h]) - .5
        xy = np.maximum(xy, 0); low = np.floor(xy).astype(int)
        low[:, 0] = np.minimum(low[:, 0], w-1); low[:, 1] = np.minimum(low[:, 1], h-1)
        high = np.minimum(low+1, [w-1, h-1]); t = xy-low
        p00 = self.pixels[low[:, 1], low[:, 0], :3]
        p10 = self.pixels[low[:, 1], high[:, 0], :3]
        p01 = self.pixels[high[:, 1], low[:, 0], :3]
        p11 = self.pixels[high[:, 1], high[:, 0], :3]
        rgb = ((p00*(1-t[:, :1])+p10*t[:, :1])*(1-t[:, 1:])
               +(p01*(1-t[:, :1])+p11*t[:, :1])*t[:, 1:])
        return rgb, distance


def cluster_colors(rgb, k=12):
    """Deterministic chromatic k-means, then map clusters to the known palette."""
    x = chroma(rgb)
    train = x[::max(1, len(x)//30000)]
    centers = [train[len(train)//2]]
    distances = np.full(len(train), np.inf)
    for _ in range(k-1):
        distances = np.minimum(distances, ((train-centers[-1])**2).sum(axis=1))
        centers.append(train[distances.argmax()])
    centers = np.array(centers)
    for _ in range(30):
        ids = ((train[:, None]-centers)**2).sum(axis=-1).argmin(axis=1)
        new = np.array([train[ids == j].mean(axis=0) if (ids == j).any() else centers[j] for j in range(k)])
        if np.max(abs(new-centers)) < 1e-6: break
        centers = new
    ids = np.concatenate([((a[:, None]-centers)**2).sum(axis=-1).argmin(axis=1)
                          for a in np.array_split(x, max(1, len(x)//50000))])
    palette_ids = ((centers[:, None]-chroma(PALETTE))**2).sum(axis=-1).argmin(axis=1)
    return ids, centers, palette_ids


def polygon_evidence(rgb, owner, polygon_count):
    labels, known = classify_samples(rgb)
    counts = np.bincount(owner, minlength=polygon_count)
    votes = np.stack([np.bincount(owner, weights=(labels == i) & known,
                                 minlength=polygon_count) for i in range(6)], axis=1)
    fractions = votes / np.maximum(counts[:, None], 1)
    unknown = 1-fractions.sum(axis=1)
    armor = fractions[:, 4:].sum(axis=1)
    body = fractions[:, :4].sum(axis=1)
    body_id = votes[:, :4].argmax(axis=1)
    armor_id = 4 + votes[:, 4:].argmax(axis=1)
    majority = np.where(armor >= body, armor_id, body_id)
    majority[unknown > .5] = 6
    means = np.stack([np.bincount(owner, weights=rgb[:, i], minlength=polygon_count)
                      for i in range(3)], axis=1) / np.maximum(counts[:, None], 1)
    mean_id, mean_known = classify_samples(means)
    mean_id[~mean_known] = 6
    cluster_id, centers, palette_id = cluster_colors(rgb)
    cluster_votes = np.stack([np.bincount(owner, weights=cluster_id == j,
                                         minlength=polygon_count) for j in range(len(centers))], axis=1)
    dominant = palette_id[cluster_votes.argmax(axis=1)]
    dominant[unknown > .5] = 6
    conservative = majority.copy()
    # Keep mixed faces visible for review instead of pretending their class is certain.
    conservative[(np.maximum(armor, body) < .80) | (unknown > .15)] = 6
    return {"mean_rgb": mean_id, "area_majority": majority,
            "dominant_cluster": dominant, "confidence_review": conservative}, {
                "fractions": fractions, "unknown": unknown,
                "cluster_centers": centers, "cluster_palette_ids": palette_id,
                "counts": counts, "mean_rgb": means,
            }
