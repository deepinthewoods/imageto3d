"""Automatic head regions (scalp, face) as signed margin fields, with switchable strategies.

Every strategy returns per-vertex *margins* in metres on the head surface: positive inside the
region, negative outside, 0 on its boundary. A margin is what the samplers need (clip a row at 0,
keep scatter points `m` away from the edge) and a mask in [0, 1] is derived from it for storage.

  landmark_geodesic  hairline from head landmarks + surface distance (default)
  template           masks carried over from a reference head after a bounding-box fit
  texture            hair-coloured cluster of the head texture
  legacy             the old plane through temples and crown

Manual paint is an override: `override_margin` turns painted vertex weights into a margin.
"""
import numpy as np

from . import geodesic, mesh

PRM = {'eye': 0.55,          # eye height, fraction of head height above the lowest head vertex
       'hairline': 0.20,     # hairline distance from the brow along the surface, fraction of head height
       'ear': 0.60,          # ear-top height at the sides
       'nape': 0.22,         # hair stops this high at the back (fraction of head height)
       'face_floor': 0.12,   # face starts above this height
       'soft': 0.02}         # width (fraction of head height) over which a mask fades 0..1

AXES = {'up': (0., 0., 1.), 'fwd': (0., -1., 0.), 'left': (1., 0., 0.)}


def local_coords(V, axes=None, origin=(0., 0., 0.)):
    """(lateral, forward, up) coordinates of points: lateral > 0 is the character's left."""
    ax = axes or AXES
    R = np.array([ax['left'], ax['fwd'], ax['up']], float)
    return (np.asarray(V, float) - np.asarray(origin)) @ R.T


def smooth01(x):
    x = np.clip(x, 0, 1)
    return x * x * (3 - 2 * x)


def mask_of(margin, soft):
    """Face/vertex mask in [0, 1]: 0.5 on the boundary, fading over `soft` metres each side."""
    return smooth01(.5 + np.asarray(margin) / max(2 * soft, 1e-9))


def mask_to_margin(g, mask):
    """Signed surface distance to the boundary of a boolean vertex mask (positive inside)."""
    mask = np.asarray(mask, bool)
    if mask.all() or not mask.any():
        return np.where(mask, 1e3, -1e3).astype(float)
    src, dst = g['src'], g['idx']
    edge_b = mask[src] != mask[dst]
    bv = np.unique(np.concatenate([src[edge_b], dst[edge_b]]))
    d = geodesic.dijkstra(g, bv)
    d = np.where(np.isfinite(d), d, 1e3) + .5 * float(g['w'].mean())    # the boundary lies half an edge from its vertices
    return np.where(mask, d, -d)


def landmarks(surf, axes=None, x0=0.0, prm=None):
    """Crown, brow, ear/nape heights and sizes of a head surface (all in the surface's frame)."""
    p = dict(PRM, **(prm or {}))
    ax = axes or AXES
    L = local_coords(surf['V'], ax, x0 * np.asarray(ax['left'], float))
    lat, fwd, up = L.T
    zmin, zmax = up.min(), up.max()
    H = zmax - zmin
    mid = (up > zmin + .3 * H) & (up < zmin + .8 * H)
    halfw = float(np.percentile(np.abs(lat[mid]), 90)) if mid.any() else float(np.abs(lat).max())
    front, back = float(fwd.max()), float(fwd.min())
    cy = (front + back) / 2
    top = up >= zmax - 0.01 * H
    crown = int(np.flatnonzero(top)[np.abs(lat[top]).argmin()])
    z_eye = zmin + p['eye'] * H
    cand = (up >= z_eye) & (up <= zmin + .85 * H) & (np.abs(lat) <= .3 * halfw)
    if not cand.any():
        cand = (up >= z_eye) & (np.abs(lat) <= .5 * halfw)
    ids = np.flatnonzero(cand)
    near = ids[fwd[ids] >= fwd[ids].max() - .004 * H]              # the front-most ones: take the most central
    brow = int(near[np.abs(lat[near]).argmin()])
    lat_ext = np.abs(lat) >= np.percentile(np.abs(lat), 97)
    ear_z = float(np.clip(up[lat_ext].mean(), zmin + .45 * H, zmin + .66 * H)) if lat_ext.any() \
        else zmin + p['ear'] * H
    ear_z = max(ear_z, zmin + p['ear'] * H - .08 * H)
    return {'axes': ax, 'x0': x0, 'L': L, 'zmin': float(zmin), 'zmax': float(zmax), 'H': float(H), 'halfw': halfw,
            'front': front, 'back': back, 'cy': cy, 'crown': crown, 'brow': brow,
            'ear_z': float(ear_z), 'nape_z': float(zmin + p['nape'] * H), 'prm': p}


def brow_distance(surf, lm):
    """Surface distance from the brow, taken from the brow and its mirror-image vertex so that the
    field (and so the hairline) is mirror-symmetric even where the mesh has no vertex on the mid-plane."""
    ax = lm['axes']
    left = np.asarray(ax['left'], float)
    P = surf['V'][lm['brow']]
    q = P - 2 * (np.dot(P, left) - lm['x0']) * left
    twin = int(((surf['V'] - q) ** 2).sum(1).argmin())
    src = [lm['brow']] + ([twin] if twin != lm['brow'] else [])
    d = geodesic.dijkstra(surf['g'], src)
    return np.where(np.isfinite(d), d, 0.0)           # a separate shell (a nose ball, a horn) is not scalp


def landmark_margins(surf, lm):
    """The default strategy: (scalp margin, face margin, brow distance field)."""
    p, L, H = lm['prm'], lm['L'], lm['H']
    lat, fwd, up = L.T
    d = brow_distance(surf, lm)
    t = np.clip((lm['cy'] - fwd) / max(lm['cy'] - lm['back'], 1e-9), 0, 1)
    z_low = lm['ear_z'] + t * (lm['nape_z'] - lm['ear_z'])
    scalp = np.minimum(d - p['hairline'] * H, up - z_low)
    facing = (surf['N'] @ np.asarray(lm['axes']['fwd'], float) - .15) * .25 * H     # normal looks forward
    face = np.minimum.reduce([-scalp, fwd - lm['cy'], up - (lm['zmin'] + p['face_floor'] * H),
                              .85 * lm['halfw'] - np.abs(lat), facing])
    return scalp, face, d


def legacy_margins(surf, lm, axes=None):
    """The old region: scalp side of the plane through both temples and the crown, seeded from
    the head bounding box exactly as the old `_auto_seed_head_singles` did."""
    L = lm['L']
    lat, fwd, up = L.T
    ctr = np.array([0., (lm['front'] + lm['back']) / 2, (lm['zmin'] + lm['zmax']) / 2])
    half = np.array([np.abs(lat).max(), (lm['front'] - lm['back']) / 2, lm['H'] / 2])
    base = ctr + np.array([0, .5 * half[1], .45 * half[2]])
    Lp = base + np.array([.85 * half[0], 0, 0])
    Rp = base - np.array([.85 * half[0], 0, 0])
    cp = np.array([0., ctr[1], lm['zmax'] - .04 * half[2]])
    n = np.cross(Rp - Lp, cp - Lp)
    n = n / max(np.linalg.norm(n), 1e-12)
    face_ref = (Lp + Rp) / 2 + np.array([0, .15, -.10]) * (lm['H'] / .22)
    sign = -1.0 if np.dot(face_ref - Lp, n) > 0 else 1.0
    scalp = ((L - Lp) @ n) * sign + .005
    face = np.minimum(-scalp, (fwd - lm['cy']))
    return scalp, face


def template_margins(surf, lm, ref):
    """Carry the scalp/face masks of a reference head over: the reference's head bounding box
    is fitted to this head's box per axis, every vertex takes the nearest reference vertex's mask."""
    rV = np.asarray(ref['V'], float)
    lo, hi = surf['V'].min(0), surf['V'].max(0)
    rlo, rhi = rV.min(0), rV.max(0)
    sc = (hi - lo) / np.maximum(rhi - rlo, 1e-9)
    q = (surf['V'] - lo) / np.maximum(sc, 1e-9) + rlo
    near = np.empty(len(q), np.int64)
    for s0 in range(0, len(q), 256):
        near[s0:s0 + 256] = ((q[s0:s0 + 256, None, :] - rV[None]) ** 2).sum(-1).argmin(1)
    out = [mask_to_margin(surf['g'], (np.asarray(ref[key], float) > .5)[near]) for key in ('scalp', 'face')]
    return out[0], out[1]


# ------------------------------------------------------------------ texture strategy
def rgb_to_lab(rgb):
    rgb = np.clip(np.asarray(rgb, float), 0, 1)
    lin = np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)
    M = np.array([[.4124, .3576, .1805], [.2126, .7152, .0722], [.0193, .1192, .9505]])
    xyz = (lin @ M.T) / np.array([.95047, 1., 1.08883])
    f = np.where(xyz > .008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]), 200 * (f[:, 1] - f[:, 2])], 1)


def kmeans(X, k=4, iters=20):
    """Small deterministic k-means (farthest-point initialisation)."""
    c = [X[0]]
    for _ in range(k - 1):
        d = np.min([((X - ci) ** 2).sum(1) for ci in c], axis=0)
        c.append(X[d.argmax()])
    c = np.array(c)
    for _ in range(iters):
        lab = ((X[:, None, :] - c[None]) ** 2).sum(-1).argmin(1)
        for j in range(k):
            if (lab == j).any():
                c[j] = X[lab == j].mean(0)
    return lab, c


def texture_margins(surf, lm, rgb, min_delta=12.0):
    """Hair = the colour cluster that differs most from the skin colour sampled at the cheeks
    (lateral, forward half, around 40% height). `rgb` is the texture colour per vertex.
    Returns (scalp, face) margins, or None if the texture has no distinct hair cluster."""
    lab = rgb_to_lab(rgb)
    lat, fwd, up = lm['L'].T
    cheeks = (np.abs(lat) > .35 * lm['halfw']) & (np.abs(lat) < .8 * lm['halfw']) \
        & (fwd > lm['cy']) & (np.abs(up - (lm['zmin'] + .40 * lm['H'])) < .08 * lm['H'])
    if not cheeks.any():
        return None
    skin = np.median(lab[cheeks], axis=0)
    labels, cent = kmeans(lab, 4)
    dist = np.linalg.norm(cent - skin, axis=1)
    j = int(dist.argmax())
    if dist[j] < min_delta:
        return None
    hair = labels == j
    hair = mesh.diffuse(surf['g'], hair.astype(float), 2) > .5
    scalp = mask_to_margin(surf['g'], hair & (up > lm['zmin'] + .4 * lm['H']))
    face = np.minimum.reduce([-scalp, lm['L'][:, 1] - lm['cy'], up - (lm['zmin'] + lm['prm']['face_floor'] * lm['H']),
                              .85 * lm['halfw'] - np.abs(lat)])
    return scalp, face


def override_margin(g, weights, thr=.5):
    """Manual paint: vertex weights above `thr` are the region."""
    return mask_to_margin(g, np.asarray(weights) > thr)


def head_regions(surf, strategy='landmark_geodesic', axes=None, x0=0.0, prm=None, template=None,
                 texture_rgb=None, paint=None):
    """Scalp and face margins plus the landmarks. Strategies that cannot run (no template, no
    texture) fall back to `landmark_geodesic`; `info['strategy']` says which one produced the result."""
    lm = landmarks(surf, axes, x0, prm)
    d = brow_distance(surf, lm)
    info = {'strategy': strategy, 'note': ''}
    out = None
    if strategy == 'template' and template is not None:
        out = template_margins(surf, lm, template)
    elif strategy == 'texture' and texture_rgb is not None:
        out = texture_margins(surf, lm, texture_rgb)
        if out is None:
            info['note'] = 'texture has no distinct hair cluster'
    elif strategy == 'legacy':
        out = legacy_margins(surf, lm, axes)
    elif strategy in ('template', 'texture'):
        info['note'] = f'{strategy} needs its input'
    if out is None:
        info['strategy'] = 'landmark_geodesic'
        scalp, face, _ = landmark_margins(surf, lm)
    else:
        scalp, face = out
    if paint is not None:
        scalp = override_margin(surf['g'], paint)
        face = np.minimum(face, -scalp)
        info['strategy'] += '+paint'
    return {'scalp': scalp, 'face': face, 'brow_dist': d, 'lm': lm, 'info': info}
