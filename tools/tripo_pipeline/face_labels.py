"""Material-role labelling shared by reduce_model.py and make_highpoly_blend.py.

Roles: 0 base (body + all base markers), 1 primary, 2 secondary, 3 no usable colour.

Evidence per texture sample
  * hue: nearest swatch hue within HUE_TOLERANCE degrees. Saturation is NOT compared: Tripo's body
    red is more saturated than the swatch, and the old chromaticity-distance test rejected it.
  * weight: ramps with saturation and brightness, so murky pixels count little and grey/black
    pixels count nothing.
  * visibility: texture only counts where one of the concept cameras saw the surface. Unseen areas
    (inner legs, crotch, armpits) are Tripo's invention and count UNSEEN_WEIGHT.

Low-poly faces combine evidence with a soft module prior from the base body landmarks
(base_views/<race>/regions.json), the mirror face (symmetric modules) and crease-aware
neighbours; every face records which of those decided it.
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
SOURCES = ('texture', 'symmetry', 'prior', 'neighbours')


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


# ---------------------------------------------------------------- module prior

def _sig(x, width=.05):
    return 1 / (1 + np.exp(-np.clip(x / width, -60, 60)))


def wear_prior(points, module, regions):
    """Probability that a surface point belongs to the garment/armour, from base landmarks.

    0.5 is neutral (evidence decides). Low values only where the module definition (AGENTS.md)
    says the base must be bare; generous elsewhere so visible armour can extend past the nominal
    boundary (e.g. a tall boot).
    """
    if regions is None or module is None:
        return np.full(len(points), .5)
    L = regions['landmarks']
    x, z = points[:, 0], points[:, 2]
    arm = _sig(np.abs(x) - L['shoulder_x']) * _sig(z - (L['arm_band_z'][0] - .15))
    forearm = arm * _sig(np.abs(x) - L['elbow_x'])
    hand = arm * _sig(np.abs(x) - L['wrist_x'])
    head = _sig(z - L['neck_z']) * (1 - arm)
    lower_leg = _sig(L['knee_z'] - z) * (1 - arm)
    thigh = _sig(z - L['knee_z']) * _sig(L['crotch_z'] + .1 - z) * (1 - arm)
    right = _sig(-x, .03)   # anatomical right is -X
    left_foot = (1 - right) * _sig(L['ankle_z'] + .06 - z)
    if module == 'body_armor':
        bare = np.maximum.reduce([forearm, lower_leg, head])
        return .5 - .45 * bare
    if module == 'right_gauntlet_boot':
        likely = np.maximum(forearm * right, lower_leg * right)       # gauntlet / boot zones
        possible = thigh * right                                       # a boot may run up the leg
        p = .05 + .55 * likely
        return np.maximum(p, .3 * possible)
    if module == 'outfit':
        bare = np.maximum.reduce([head, hand, left_foot])
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


def resolve_faces(v, f, sample_owner, sample_role, sample_weight, prior, symmetric):
    """Combine weighted texture votes, module prior, mirror face and crease-aware neighbours.

    Returns labels (0..2), source index into SOURCES, and per-face evidence arrays.
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
    labels = (data + prior_score).argmax(1)
    for _ in range(10):
        score = data + prior_score
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
    return labels, source, {'confidence': conf, 'vote_share': share, 'wear_prior': prior}
