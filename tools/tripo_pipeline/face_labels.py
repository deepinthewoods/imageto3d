"""Material-role labelling shared by reduce_model.py and make_highpoly_blend.py.

Roles: 0 base (body + all base markers), 1 primary, 2 secondary, 3 no usable colour.

Evidence per texture sample
  * hue: nearest swatch hue within HUE_TOLERANCE degrees. Saturation is NOT compared: Tripo's body
    red is more saturated than the swatch, and the old chromaticity-distance test rejected it.
  * weight: ramps with saturation and brightness, so murky pixels count little and grey/black
    pixels count nothing.
  * visibility: texture only counts where one of the concept cameras saw the surface. Unseen areas
    (inner legs, crotch, armpits) are Tripo's invention and count UNSEEN_WEIGHT.

Low-poly faces combine evidence with a soft module prior from the base body, the mirror face
(symmetric modules) and crease-aware neighbours; every face records which of those decided it.
The prior's body zones come from the base's per-face body parts (base_views/<race>/parts.npz,
transferred onto the mesh by BaseParts/fit_base/transfer_parts below), which works for any pose;
without a parts file they fall back to the T-pose landmarks of base_views/<race>/regions.json.
"""
import json
import math
from pathlib import Path

import numpy as np

HUE_TOLERANCE = 20.0      # degrees; closest swatch hues (cyan/blue) are ~45 deg apart
SAT_RAMP = (.2, .45)      # sample weight 0 at/below first value, 1 at/above second
VAL_RAMP = (.08, .25)
UNSEEN_WEIGHT = .05       # texture on surfaces no concept view saw
GRAZING_COS = .25         # view cosine at which a surface counts as fully seen
PRIOR_WEIGHT = .6         # strength of the module prior (score units; full clear evidence = 1)
NEIGHBOUR_WEIGHT = .25    # per smooth edge
MIRROR_WEIGHT = .5
SYMMETRIC_MODULES = {'body_armor'}
SOURCES = ('texture', 'symmetry', 'prior', 'neighbours', 'off_body', 'riser')
OFF_BODY_PENALTY = 3.      # base score taken away from a face fully outside the base body (clear texture = 1)
OFF_BODY_MARGIN = .02      # added to the bare-mesh p99.9 clearance of a part (normalized units, height 2)
OFF_BODY_FLOOR = .03       # threshold never below this
OFF_BODY_RAMP = .02        # the penalty grows over this distance beyond the threshold
RISER_MIN_ANGLE = 60.      # degrees between a hem riser's normal and the nearest base surface normal
RISER_RINGS = 2            # a riser may only flip next to wear faces, at most this many rings deep
OFF_BODY_MIN_PRIOR = .4    # only where the module prior allows wear (body parts that must stay bare are exempt)


def swatch_rgb(value):
    if not isinstance(value, str) or len(value) != 7 or not value.startswith('#'):
        raise ValueError('Swatches must be #RRGGBB strings')
    return np.array([int(value[i:i+2], 16) for i in (1, 3, 5)], float) / 255


def hsv(rgb):
    rgb = np.asarray(rgb, float)
    mx, mn = rgb.max(1), rgb.min(1)
    d = mx - mn
    r, g, b = rgb.T
    h = np.zeros(len(rgb))
    nz = d > 1e-9
    i = nz & (mx == r)
    h[i] = ((g - b)[i] / d[i]) % 6
    j = nz & (mx == g) & ~i
    h[j] = (b - r)[j] / d[j] + 2
    k = nz & ~i & ~j
    h[k] = (r - g)[k] / d[k] + 4
    return h * 60, np.where(mx > 0, d / np.maximum(mx, 1e-9), 0), mx


def ramp(x, lo, hi):
    return np.clip((x - lo) / (hi - lo), 0, 1)


def classify_samples(colors, swatches):
    """Return (role 0..3, weight 0..1) per colour sample."""
    names = [swatches['base'], *swatches.get('base_markers', {}).values(),
             swatches['primary'], swatches['secondary']]
    centre_h, _, _ = hsv(np.array([swatch_rgb(s) for s in names]))
    h, s, v = hsv(colors)
    dist = np.abs((h[:, None] - centre_h[None] + 180) % 360 - 180)
    nearest = dist.argmin(1)
    markers = len(names) - 2
    role = np.where(nearest < markers, 0, nearest - markers + 1)
    weight = ramp(s, *SAT_RAMP) * ramp(v, *VAL_RAMP) * (dist.min(1) <= HUE_TOLERANCE)
    return np.where(weight > 0, role, 3), weight


# ---------------------------------------------------------------- cameras and visibility

def load_cameras(transforms_path, regions):
    """Concept cameras mapped into the normalized frame (base bbox centre XY, feet Z=0, height 2)."""
    t = json.loads(Path(transforms_path).read_text())
    low = np.array(regions['base_scene_bbox']['min'])
    high = np.array(regions['base_scene_bbox']['max'])
    origin = (low + high) / 2
    origin[2] = low[2]
    scale = 2 / (high[2] - low[2])
    cams = []
    for frame in t['frames']:
        m = np.array(frame['transform_matrix'])
        cams.append({'name': frame['name'], 'position': (m[:3, 3] - origin) * scale,
                     'rotation': m[:3, :3], 'tan_half': math.tan(t['camera_angle_x'] / 2)})
    return cams


def visibility(points, normals, tree, cameras, eps=.004):
    """Per point: best view cosine over unoccluded concept cameras (0 = never seen)."""
    from mathutils import Vector
    best = np.zeros(len(points))
    for cam in cameras:
        to_cam = cam['position'][None] - points
        dist = np.linalg.norm(to_cam, axis=1)
        unit = to_cam / np.maximum(dist[:, None], 1e-12)
        cos = (normals * unit).sum(1)
        local = (points - cam['position'][None]) @ cam['rotation']   # camera looks down -Z
        depth = -local[:, 2]
        inside = (depth > 0) & (np.abs(local[:, 0]) < depth * cam['tan_half']) & \
                 (np.abs(local[:, 1]) < depth * cam['tan_half'])
        todo = np.flatnonzero(inside & (cos > 0) & (cos > best))
        start = points[todo] + normals[todo] * eps
        for i, origin, direction, d in zip(todo, start, unit[todo], dist[todo]):
            hit = tree.ray_cast(Vector(origin), Vector(direction), float(d))
            if hit[0] is None:
                best[i] = cos[i]
    return best


def visibility_weight(best_cos, have_cameras):
    if not have_cameras:
        return np.ones_like(best_cos)
    return np.maximum(ramp(best_cos, 0, GRAZING_COS), UNSEEN_WEIGHT)


# ---------------------------------------------------------------- enclosed (interior) surface

INTERIOR_RAYS = 14         # outward-hemisphere rays per face
INTERIOR_MIN_ELEVATION = 20.  # degrees above the face plane; flatter rays graze their own neighbourhood
INTERIOR_EPS = .004        # ray origin offset along the normal (normalized units, height 2)
# Area share of enclosed faces that flags a source and (mode auto) triggers stripping. Calibrated on 30
# Tripo sources (2026-10-22): bare bases <= 0.005, plate-mail modules <= 0.044, a double-shell source 0.415.
INTERIOR_THRESHOLD = .15


def hemisphere_rays(normals, count=INTERIOR_RAYS):
    """(n, count, 3) unit directions over each face's outward hemisphere (Fibonacci spiral, first = normal,
    nothing flatter than INTERIOR_MIN_ELEVATION)."""
    k = np.arange(count) + .5
    sin_min = math.sin(math.radians(INTERIOR_MIN_ELEVATION))
    z = 1 - (1 - sin_min) * k / count                      # cos(polar) from 1 down to sin_min
    z[0] = 1.
    phi = k * math.pi * (3 - math.sqrt(5))
    r = np.sqrt(np.maximum(1 - z * z, 0))
    local = np.stack([r * np.cos(phi), r * np.sin(phi), z], axis=1)
    helper = np.where(np.abs(normals[:, 2:3]) < .9, np.array([[0., 0., 1.]]), np.array([[1., 0., 0.]]))
    t = np.cross(normals, helper)
    t /= np.linalg.norm(t, axis=1, keepdims=True)
    b = np.cross(normals, t)
    return local[None, :, 0:1] * t[:, None] + local[None, :, 1:2] * b[:, None] + local[None, :, 2:3] * normals[:, None]


def escaping_rays(v, f, tree, ids, normals=None, count=INTERIOR_RAYS, stop_at_first=False):
    """Per face in `ids`: how many of `count` rays over the outward hemisphere (from the centroid,
    nudged along the normal) leave the mesh without hitting any triangle. 0 = enclosed: the face
    looks only at other surface, e.g. an inner shell or skin under armour. With `stop_at_first`,
    faces count 1 as soon as one ray escapes (cheaper; only 0 vs >0 is meaningful)."""
    from mathutils import Vector
    if normals is None:
        normals = face_normals(v, f)[0]
    n = normals[ids]
    origin = v[f[ids]].mean(1) + n * INTERIOR_EPS
    rays = hemisphere_rays(n, count)
    out = np.zeros(len(ids), np.int64)
    for i in range(len(ids)):
        o = Vector(origin[i])
        for d in rays[i]:
            if tree.ray_cast(o, Vector(d))[0] is None:
                out[i] += 1
                if stop_at_first:
                    break
    return out


def enclosed_faces(v, f, tree, count=INTERIOR_RAYS):
    """Per face: True when no outward ray escapes (inner shell, duplicated body). Isolated faces and
    small islands are merged into their surroundings (clean_parts) so the result is contiguous."""
    normals, area = face_normals(v, f)
    esc = escaping_rays(v, f, tree, np.arange(len(f)), normals, count, stop_at_first=True)
    return clean_parts(v, f, (esc == 0).astype(np.int64), area).astype(bool)


def interior_metrics(v, f, tree=None, samples=5000, seed=11, count=INTERIOR_RAYS):
    """Interior-surface audit of a mesh: area share of faces whose outward hemisphere is fully
    blocked (`interior_area_share`, no ray escapes), mostly blocked (<= 1 escapes) and blocked along
    the normal alone (`normal_blocked_share`, the simple test), from an area-weighted face sample.
    A closed concavity (inner thigh, armpit) still lets some rays out; an inner shell or a
    duplicated body does not."""
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree
    if tree is None:
        tree = BVHTree.FromPolygons(v.tolist(), f.tolist(), all_triangles=True)
    normals, area = face_normals(v, f)
    ids = np.random.default_rng(seed).choice(len(f), samples, p=area / area.sum())
    esc = escaping_rays(v, f, tree, ids, normals, count)
    origin = v[f[ids]].mean(1) + normals[ids] * INTERIOR_EPS
    gap = []
    for i, o in zip(ids[esc == 0], origin[esc == 0]):
        hit = tree.ray_cast(Vector(o), Vector(normals[i]))
        if hit[0] is not None:
            gap.append(hit[3])
    return {'interior_area_share': float((esc == 0).mean()),
            'interior_area_share_loose': float((esc <= 1).mean()),
            'normal_blocked_share': float(np.mean([tree.ray_cast(Vector(o), Vector(normals[i]))[0] is not None
                                                   for i, o in zip(ids, origin)])),
            'interior_gap_median': float(np.median(gap)) if gap else None,
            'samples': samples, 'rays': count, 'total_area': float(area.sum())}


# ---------------------------------------------------------------- base body parts

PART_RADIUS = .07          # soft part membership kernel radius (normalized units, height 2)
NORMAL_PENALTY = .04       # hard transfer: extra distance a flipped normal is worth
BASE_SAMPLES = 20000       # dense area-uniform surface samples of the base
MULTI_SAMPLE_FACES = 20000  # meshes up to this size vote with 4 points per face
ISLAND_TINY, ISLAND_SMALL = .0005, .003   # area share of a part island that is always / usually merged
SCALE_RANGE = (.6, 1.6)


def _sample_surface(v, f, count, seed):
    normals, area = face_normals(v, f)
    rng = np.random.default_rng(seed)
    ids = rng.choice(len(f), count, p=area / area.sum())
    r, s = np.sqrt(rng.random(count)), rng.random(count)
    w = np.stack((1 - r, r * (1 - s), r * s), axis=1)
    return np.einsum('ij,ijk->ik', w, v[f[ids]]), ids, normals[ids]


class BaseParts:
    """Per-triangle body parts of a marker base (base_parts.py npz): nearest-surface lookup and
    soft membership. `names[part]` are Armourforge bone names (head.x, hand.l, forearm_stretch.r...).
    """

    def __init__(self, v, f, part, names, path=None, sha256=None, samples=None, clearance=None):
        self.v, self.f, self.part, self.names = v, f, part, list(names)
        self.path, self.sha256, self._tree = path, sha256, None
        self.clearance = clearance   # base_clearance.py record: p99.9 outside distance per part
        if samples is None:
            pts, ids, normals = _sample_surface(v, f, BASE_SAMPLES, 7)
            samples = (pts, part[ids], normals)
        self.samples = samples     # points, part, normal

    @classmethod
    def load(cls, path, clearance_path=None):
        """`clearance_path` defaults to clearance.json beside the parts file (absent = no off-body rule)."""
        z = np.load(path, allow_pickle=False)
        beside = Path(clearance_path) if clearance_path else Path(path).with_name('clearance.json')
        return cls(z['v'].astype(float), z['f'].astype(np.int64), z['part'].astype(np.int64),
                   [str(n) for n in z['part_names']], str(path), str(z['source_sha256']),
                   clearance=json.loads(beside.read_text()) if beside.is_file() else None)

    @property
    def tree(self):
        if self._tree is None:
            from mathutils.bvhtree import BVHTree
            self._tree = BVHTree.FromPolygons(self.v.tolist(), self.f.tolist(), all_triangles=True)
        return self._tree

    def moved(self, scale, translation):
        """The base after `x -> scale * x + translation` (fit_base result)."""
        pts, part, normals = self.samples
        return BaseParts(self.v * scale + translation, self.f, self.part, self.names, self.path,
                         self.sha256, (pts * scale + translation, part, normals), self.clearance)

    def nearest_part(self, points, normals=None):
        """Part of the nearest base surface point. With face `normals`, a base triangle whose normal
        agrees wins over a slightly nearer one facing away (NORMAL_PENALTY per unit of disagreement)."""
        from mathutils import Vector
        tree, out = self.tree, np.empty(len(points), np.int64)
        for i, p in enumerate(points):
            co = Vector(p)
            hit = tree.find_nearest(co)
            best = hit[2]
            if normals is not None:
                near = tree.find_nearest_range(co, hit[3] + NORMAL_PENALTY)
                if len(near) > 1:
                    n = Vector(normals[i])
                    best = min(near, key=lambda h: h[3] + NORMAL_PENALTY * (1 - max(h[1].dot(n), 0)))[2]
            out[i] = self.part[best]
        return out

    def signed_distance(self, points):
        """Distance to the nearest base surface point, positive outside the body (base normal side)."""
        from mathutils import Vector
        tree, out = self.tree, np.empty(len(points))
        for i, p in enumerate(points):
            co = Vector(p)
            loc, normal, _, dist = tree.find_nearest(co)
            out[i] = dist if (co - loc).dot(normal) >= 0 else -dist
        return out

    def nearest_normal(self, points):
        """Outward normal of the base triangle nearest to each point."""
        from mathutils import Vector
        return np.array([tuple(self.tree.find_nearest(Vector(p))[1]) for p in points])

    def membership(self, points, normals=None, radius=PART_RADIUS):
        """(n, parts) distance-weighted vote of base surface samples within `radius` of each point
        (triangular kernel, mild normal weighting); rows sum to 1. Points with no sample in range
        take their nearest part outright."""
        pts, label, sample_normals = self.samples
        count = len(self.names)
        pack = lambda a: (a[..., 0] << 42) | (a[..., 1] << 21) | a[..., 2]
        cells = np.floor(pts / radius).astype(np.int64) + 1024
        order = np.argsort(pack(cells), kind='stable')
        sorted_key = pack(cells)[order]
        qcells = np.floor(points / radius).astype(np.int64) + 1024
        qkey = pack(qcells)
        qorder = np.argsort(qkey, kind='stable')
        uniq, starts = np.unique(qkey[qorder], return_index=True)
        offsets = np.array([(i, j, k) for i in (-1, 0, 1) for j in (-1, 0, 1) for k in (-1, 0, 1)])
        out = np.zeros((len(points), count))
        onehot = np.eye(count)
        for members in np.split(qorder, starts[1:]):
            cand = []
            for off in offsets:
                key = pack(qcells[members[0]] + off)
                lo, hi = np.searchsorted(sorted_key, key, 'left'), np.searchsorted(sorted_key, key, 'right')
                if hi > lo:
                    cand.append(order[lo:hi])
            if not cand:
                continue
            cand = np.concatenate(cand)
            d = np.linalg.norm(points[members][:, None] - pts[cand][None], axis=2)
            w = np.clip(1 - d / radius, 0, 1)
            if normals is not None:
                w = w * (.3 + .7 * np.clip(normals[members] @ sample_normals[cand].T, 0, 1))
            out[members] = w @ onehot[label[cand]]
        empty = out.sum(1) < 1e-9
        if empty.any():
            out[empty, self.nearest_part(points[empty])] = 1
        return out / out.sum(1, keepdims=True)


def fit_base(base, v, f, tree=None, iterations=15, keep=.7, count=4000, seed=3):
    """Trimmed ICP of the base body onto mesh (v, f): uniform scale + translation only, no rotation.

    Each iteration pairs base samples with their nearest mesh point and mesh samples with their
    nearest base point, keeps the closest `keep` fraction of each direction (armour bulk and Tripo
    inventions fall outside), and solves the closed-form scale/translation. The best iterate wins;
    identity is used when no iterate beats the starting residual. Returns (scale, translation, info);
    residuals are mean trimmed distances in normalized units (height 2).
    """
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree
    if tree is None:
        tree = BVHTree.FromPolygons(v.tolist(), f.tolist(), all_triangles=True)
    mesh_pts = _sample_surface(v, f, count, seed)[0]
    base_pts = base.samples[0][np.random.default_rng(seed).choice(len(base.samples[0]), count, replace=False)]

    def pairs(s, t):
        p = base_pts * s + t
        q = np.array([tuple(tree.find_nearest(Vector(x))[0]) for x in p])
        d1 = np.linalg.norm(p - q, axis=1)
        inv = (mesh_pts - t) / s
        c = np.array([tuple(base.tree.find_nearest(Vector(x))[0]) for x in inv])
        d2 = np.linalg.norm(inv - c, axis=1) * s
        k1, k2 = d1 <= np.quantile(d1, keep), d2 <= np.quantile(d2, keep)
        return (np.concatenate([base_pts[k1], c[k2]]), np.concatenate([q[k1], mesh_pts[k2]]),
                float(np.concatenate([d1[k1], d2[k2]]).mean()))

    s, t = 1., np.zeros(3)
    P, Q, start = pairs(s, t)
    best = (start, s, t)
    used = 0
    for _ in range(iterations):
        pm, qm = P.mean(0), Q.mean(0)
        dp = P - pm
        s_new = min(max(float((dp * (Q - qm)).sum() / (dp * dp).sum()), SCALE_RANGE[0]), SCALE_RANGE[1])
        t_new = qm - s_new * pm
        settled = abs(s_new - s) < 1e-5 and np.abs(t_new - t).max() < 1e-5
        s, t = s_new, t_new
        P, Q, res = pairs(s, t)
        used += 1
        if np.isfinite(res) and res < best[0]:
            best = (res, s, t)
        if settled:
            break
    res, s, t = best
    icp = res < start - 1e-6
    if not icp:
        res, s, t = start, 1., np.zeros(3)
    return s, t, {'method': 'trimmed_icp_scale_translation', 'used': 'icp' if icp else 'identity',
                  'scale': float(s), 'translation': [float(x) for x in t], 'iterations': used,
                  'keep_fraction': keep, 'residual_identity': start, 'residual_fitted': res,
                  'residual_identity_pct_height': start / 2 * 100, 'residual_fitted_pct_height': res / 2 * 100}


def _components(n, a, b):
    """Connected-component root (smallest face index) per face, given edges a-b."""
    lab = np.arange(n)
    while True:
        m = np.minimum(lab[a], lab[b])
        new = lab.copy()
        np.minimum.at(new, a, m)
        np.minimum.at(new, b, m)
        new = new[new]
        if np.array_equal(new, lab):
            return lab
        lab = new


def clean_parts(v, f, part, area):
    """Merge isolated faces and small islands of a part into the neighbouring part they share
    the most edges with, so parts form contiguous regions. A part's only island is kept unless
    it is a single face or negligible."""
    part = part.copy()
    count = int(part.max()) + 1
    _, weld = np.unique(np.round(v / 1e-6).astype(np.int64), axis=0, return_inverse=True)
    a, b = face_adjacency(weld.reshape(-1)[f])
    total = area.sum()
    for _ in range(4):
        same = part[a] == part[b]
        comp = np.unique(_components(len(f), a[same], b[same]), return_inverse=True)[1].reshape(-1)
        n = comp.max() + 1
        c_area, c_faces = np.bincount(comp, area, n), np.bincount(comp, minlength=n)
        c_part = np.zeros(n, np.int64)
        c_part[comp] = part
        biggest = np.zeros(count)
        np.maximum.at(biggest, c_part, c_area)
        small = (c_faces <= 1) | (c_area < ISLAND_TINY * total) | \
                ((c_area < ISLAND_SMALL * total) & (c_area < biggest[c_part]))
        if not small.any():
            break
        edge = ~same
        owner = np.concatenate([comp[a[edge]], comp[b[edge]]])
        other = np.concatenate([part[b[edge]], part[a[edge]]])
        pick = small[owner]
        keys, votes = np.unique(owner[pick] * count + other[pick], return_counts=True)
        if not len(keys):
            break
        order = np.lexsort((-votes, keys // count))
        keys = keys[order]
        first = np.r_[True, keys[1:] // count != keys[:-1] // count]
        target = np.full(n, -1)
        target[keys[first] // count] = keys[first] % count
        move = target[comp] >= 0
        if not move.any():
            break
        part[move] = target[comp][move]
    return part


def transfer_parts(v, f, base, clean=True):
    """Body part per face of mesh (v, f): the part of the nearest `base` surface, then cleaned.

    `base` must already be fitted to the mesh (BaseParts.moved). Small meshes vote with the centroid
    and three points towards the corners (centroid breaks ties), so a large low-poly face is not
    decided by one point.
    """
    normals, area = face_normals(v, f)
    cent = v[f].mean(1)
    if len(f) > MULTI_SAMPLE_FACES:
        part = base.nearest_part(cent, normals)
    else:
        votes = np.zeros((len(f), len(base.names)))
        for k, p in enumerate([cent] + [cent + .5 * (v[f[:, i]] - cent) for i in range(3)]):
            votes[np.arange(len(f)), base.nearest_part(p, normals)] += 1.5 if k == 0 else 1.
        part = votes.argmax(1)
    return clean_parts(v, f, part, area) if clean else part


def part_area_shares(part, area, names):
    share = np.bincount(part, area, len(names)) / max(area.sum(), 1e-20)
    return {n: round(float(s), 5) for n, s in zip(names, share) if s > 0}


def off_body_distance(v, f, base):
    """Per face: how far it reaches outside the fitted base body = the largest signed distance over its
    three corners and its centroid. A thin armour-edge sliver has corners on the hem even when its
    centroid sits near the skin."""
    corners = [base.signed_distance(v[f[:, k]]) for k in range(3)]
    return np.maximum.reduce([base.signed_distance(v[f].mean(1)), *corners])


def off_body_threshold(parts, base):
    """Per face: distance outside the base beyond which bare skin is implausible. The p99.9 of the same
    body part on the bare Tripo mesh of that race (base_clearance.py) plus OFF_BODY_MARGIN; parts
    without a record use the race-wide value. None when the base has no clearance record."""
    if base.clearance is None:
        return None
    default = base.clearance['global_outside']
    table = np.array([base.clearance['by_part_outside'].get(n, default) for n in base.names])
    return np.maximum(table[parts] + OFF_BODY_MARGIN, OFF_BODY_FLOOR)


def off_body_excess(v, f, parts, base, prior):
    """0..1 per face, how clearly it lies outside the base body (ramp over OFF_BODY_RAMP beyond the
    threshold); 0 where the module prior says the body part must stay bare. Returns (excess,
    distance, threshold) or None without a clearance record."""
    threshold = off_body_threshold(parts, base)
    if threshold is None:
        return None
    dist = off_body_distance(v, f, base)
    excess = np.clip((dist - threshold) / OFF_BODY_RAMP, 0, 1) * (prior >= OFF_BODY_MIN_PRIOR)
    return excess, dist, threshold


def hem_risers(v, f, labels, source, base, prior, min_angle=RISER_MIN_ANGLE, rings=RISER_RINGS):
    """Flip base-labelled hem slivers to the garment next to them.

    Tripo bakes the body's red onto the thin faces where an armour edge meets the limb: they lie flush
    with the skin (so off_body_excess cannot see them) but face along the limb (the edge's underside or
    riser), not out of it. A base face is flipped when it shares an edge with a wear face, the module prior
    allows wear (OFF_BODY_MIN_PRIOR), and its normal differs from the nearest fitted-base normal by more
    than `min_angle`. It takes the wear role most of its wear neighbours have. At most `rings` rounds, each
    only next to wear faces, so it cannot creep up a limb. Returns (labels, source, flipped mask).
    """
    labels, source = labels.copy(), source.copy()
    normals = face_normals(v, f)[0]
    agree = (normals * base.nearest_normal(v[f].mean(1))).sum(1)
    candidate = (prior >= OFF_BODY_MIN_PRIOR) & (agree < math.cos(math.radians(min_angle)))
    a, b = face_adjacency(f)
    flipped = np.zeros(len(f), bool)
    for _ in range(rings):
        wear = np.zeros((len(f), 3))
        for x, y in ((a, b), (b, a)):
            ok = labels[y] != 0
            np.add.at(wear, (x[ok], labels[y][ok]), 1)
        move = (labels == 0) & candidate & (wear.sum(1) > 0)
        if not move.any():
            break
        labels[move] = wear[move].argmax(1)
        source[move] = SOURCES.index('riser')
        flipped |= move
    return labels, source, flipped


# ---------------------------------------------------------------- module prior

def _sig(x, width=.05):
    return 1 / (1 + np.exp(-np.clip(x / width, -60, 60)))


def landmark_zones(points, regions):
    """Body zones (0..1 per point) from T-pose landmarks; wrong for A-posed or legless bases."""
    L = regions['landmarks']
    x, z = points[:, 0], points[:, 2]
    arm = _sig(np.abs(x) - L['shoulder_x']) * _sig(z - (L['arm_band_z'][0] - .15))
    right = _sig(-x, .03)   # anatomical right is -X
    return {'forearm': arm * _sig(np.abs(x) - L['elbow_x']),
            'hand': arm * _sig(np.abs(x) - L['wrist_x']),
            'head': _sig(z - L['neck_z']) * (1 - arm),
            'lower_leg': _sig(L['knee_z'] - z) * (1 - arm),
            'thigh': _sig(z - L['knee_z']) * _sig(L['crotch_z'] + .1 - z) * (1 - arm),
            'right': right,
            'left_foot': (1 - right) * _sig(L['ankle_z'] + .06 - z)}


def part_zones(member, names):
    """The same zones from soft part membership (BaseParts.membership), valid for any pose.
    forearm = forearm_stretch + hand, lower_leg = leg_stretch + foot; `right` is the share of
    '.r' parts among the sided ones nearby."""
    def total(*prefixes, suffix=''):
        cols = [i for i, n in enumerate(names) if n.startswith(prefixes or '') and n.endswith(suffix)]
        return member[:, cols].sum(1)
    right, left = total(suffix='.r'), total(suffix='.l')
    return {'forearm': total('forearm_stretch.', 'hand.'), 'hand': total('hand.'), 'head': total('head.x'),
            'lower_leg': total('leg_stretch.', 'foot.'), 'thigh': total('thigh_stretch.'),
            'right': np.where(right + left > 1e-9, right / np.maximum(right + left, 1e-9), .5),
            'left_foot': total('foot.l')}


def wear_prior(points, module, regions, zones=None):
    """Probability that a surface point belongs to the garment/armour, from body zones.

    0.5 is neutral (evidence decides). Low values only where the module definition (AGENTS.md)
    says the base must be bare; generous elsewhere so visible armour can extend past the nominal
    boundary (e.g. a tall boot). `zones` (part_zones) wins over the landmark fallback `regions`.
    """
    if module is None or (zones is None and regions is None):
        return np.full(len(points), .5)
    z = zones if zones is not None else landmark_zones(points, regions)
    if module == 'body_armor':
        bare = np.maximum.reduce([z['forearm'], z['lower_leg'], z['head']])
        return .5 - .45 * bare
    if module == 'right_gauntlet_boot':
        likely = np.maximum(z['forearm'] * z['right'], z['lower_leg'] * z['right'])   # gauntlet / boot zones
        possible = z['thigh'] * z['right']                                             # a boot may run up the leg
        p = .05 + .55 * likely
        return np.maximum(p, .3 * possible)
    if module == 'outfit':
        bare = np.maximum.reduce([z['head'], z['hand'], z['left_foot']])
        return .5 - .47 * bare
    return np.full(len(points), .5)


# ---------------------------------------------------------------- low-poly resolution

def face_adjacency(f):
    e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    e.sort(axis=1)
    key = e[:, 0].astype(np.int64) * (int(f.max()) + 1) + e[:, 1]
    owner = np.tile(np.arange(len(f)), 3)
    order = np.argsort(key, kind='stable')
    key, owner = key[order], owner[order]
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    counts = np.diff(np.r_[starts, len(key)])
    two = starts[counts == 2]
    return owner[two], owner[two + 1]


def face_normals(v, f):
    n = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
    area = np.linalg.norm(n, axis=1) / 2
    return n / np.maximum(2 * area[:, None], 1e-20), area


def resolve_faces(v, f, sample_owner, sample_role, sample_weight, prior, symmetric, off_body=None):
    """Combine weighted texture votes, module prior, mirror face and crease-aware neighbours.

    `off_body` (0..1 per face, off_body_excess) takes score away from the base role: a face clearly outside
    the base body cannot be bare skin, whatever baked texture says. Returns labels (0..2), source index
    into SOURCES, and per-face evidence arrays.
    """
    n_faces = len(f)
    votes = np.zeros((n_faces, 3))
    ok = sample_role < 3
    np.add.at(votes, (sample_owner[ok], sample_role[ok]), sample_weight[ok])
    n_samples = np.bincount(sample_owner, minlength=n_faces)
    total = votes.sum(1)
    conf = np.clip(total / np.maximum(.5 * n_samples, 1e-9), 0, 1)
    share = votes / np.maximum(total[:, None], 1e-12)
    data = conf[:, None] * share
    prior_score = PRIOR_WEIGHT * np.stack([.5 - prior, prior - .5, prior - .5], axis=1)
    normals, _ = face_normals(v, f)
    a, b = face_adjacency(f)
    w = np.maximum(np.clip((normals[a] * normals[b]).sum(1), 0, 1), .02) ** 8
    mirror, mirror_ok = None, np.zeros(n_faces, bool)
    if symmetric:
        from mathutils import Vector
        from mathutils.bvhtree import BVHTree
        tree = BVHTree.FromPolygons(v.tolist(), f.tolist(), all_triangles=True)
        cent = v[f].mean(1) * np.array([-1, 1, 1])
        hits = [tree.find_nearest(Vector(p)) for p in cent]
        mirror = np.array([h[2] for h in hits], int)
        dist = np.array([h[3] for h in hits])
        hit_n = np.array([tuple(h[1]) for h in hits])
        mirror_ok = (dist < .02) & ((hit_n * normals * np.array([-1, 1, 1])).sum(1) > .7)
    def solve(ban):
        labels = (data + prior_score + ban).argmax(1)
        for _ in range(10):
            score = data + prior_score + ban
            nb = np.zeros((n_faces, 3))
            np.add.at(nb, (a, labels[b]), w)
            np.add.at(nb, (b, labels[a]), w)
            score += NEIGHBOUR_WEIGHT * nb
            if symmetric:
                idx = np.flatnonzero(mirror_ok)
                score[idx, labels[mirror[idx]]] += MIRROR_WEIGHT * (1 - conf[idx]) * conf[mirror[idx]]
            new = score.argmax(1)
            if np.array_equal(new, labels):
                break
            labels = new
        return labels
    ban = np.zeros((n_faces, 3))
    labels = free = solve(ban)
    if off_body is not None and off_body.any():
        ban[:, 0] = -OFF_BODY_PENALTY * off_body
        labels = solve(ban)
    texture_choice = share.argmax(1)
    source = np.full(n_faces, 3)                                   # neighbours
    strong_prior = np.abs(prior - .5) > .3
    is_prior = strong_prior & (conf < .5) & ((labels == 0) == (prior < .5))  # prior speaks base vs wear only
    source[is_prior] = 2
    if symmetric:
        is_mirror = mirror_ok & (conf < .5) & (labels == labels[np.where(mirror_ok, mirror, 0)]) & \
                    (conf[np.where(mirror_ok, mirror, 0)] >= .5)
        source[is_mirror] = 1
    source[(conf >= .5) & (labels == texture_choice)] = 0
    source[(free == 0) & (labels != 0)] = SOURCES.index('off_body')
    return labels, source, {'confidence': conf, 'vote_share': share, 'wear_prior': prior}
