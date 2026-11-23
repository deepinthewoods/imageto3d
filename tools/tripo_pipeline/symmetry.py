"""Mirror-symmetry measurement of an image-to-3D mesh (geometry and texture colour). Read-only analysis.

The marker bases are exactly left/right symmetric (anatomical right = -X), but after image-to-3D neither the mesh nor its
colours are. This module measures how much, so a later prototype can pool evidence between mirror partners where they
agree and detect designs that are asymmetric on purpose (diagonal straps, one-sided gauntlets). Nothing in the labelling
pipeline uses it yet.

Pieces (numpy + mathutils, Blender-python compatible; `mathutils` is only imported when a BVH is built, so the plane
fit and the statistics also run in plain python):

* `Plane` / `fit_plane`: a full mirror plane (normal with small rotation + offset), refined by minimising the trimmed
  (80th percentile) distance between reflected surface samples and the surface. Initialised from the base's sagittal
  plane mapped into mesh space through the existing base fit (`face_labels.fit_base`), the pipeline's bbox-centre
  plane x = 0, and a coarse offset scan.
* `Surface`: triangle mesh with BVH, area-uniform sampling and (when it has UVs and a texture) barycentric texture
  look-up.
* `measure`: for surface points, the mirror partner (reflect across the plane, nearest surface point, its normal and
  face), geometric distance, normal agreement, texture role of both sides (`palette.Palette.classify`: hue classes, the
  white anchors are the achromatic part of class 0 "base") and the best concept-camera view cosine of both sides
  (`face_labels.visibility`), so "both sides well seen and disagree" differs from "one side unseen".
* `summarise`: aggregation per body part family (`arm_stretch.l` + `arm_stretch.r` = `arm_stretch`, nearest fitted
  base part of each sample), per height band and per coarse spatial cell. Regions that are asymmetric by design
  (`EXPECTED_ASYMMETRIC`: the right gauntlet/boot, the outfit's right footwear) are reported separately and are not
  counted in the headline numbers or the list of asymmetric regions.

All distances are in the normalized frame (height 2, feet at z = 0, bbox centre x = 0) and reported as per cent of height.
"""
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import face_labels  # noqa: E402

HEIGHT = 2.0                      # normalized height
PCT = 100.0 / HEIGHT              # normalized distance -> per cent of height

# body-part families (part name without .l/.r/.x) whose left/right are different on purpose in a module's concept
EXPECTED_ASYMMETRIC = {
    'right_gauntlet_boot': ('hand', 'forearm_stretch', 'foot', 'leg_stretch'),
    'outfit': ('foot', 'leg_stretch'),
    'body_armor': (),
}

TRIM = .8                         # plane fit: fraction of closest reflected samples that counts
COLOUR_WEIGHT_MIN = .5            # texture sample is confidently classified at classify() weight >= this
MATCH_MAX_DIST = .03              # mirror partner is a real counterpart: within 1.5% of height ...
MATCH_MIN_NORMAL = .5             # ... and reflected normal agrees (dot >= this)
SEEN_COS = face_labels.GRAZING_COS  # a surface point counts as well seen at this best view cosine (weight 1)
FLAG_COLOUR = .25                 # region flagged: well-seen colour disagreement share >= this ...
FLAG_COLOUR_MIN = 120             # ... over at least this many well-seen compared samples
FLAG_GEO_P95 = 2.0                # region flagged: p95 mirror distance of well-seen samples >= this (% height)
FLAG_GEO_MIN = 120
CELL = .2                         # coarse spatial cells (|x|, z) for hotspots, normalized units


# ---------------------------------------------------------------- planes

class Plane:
    """Plane n . p = d with unit normal n (close to +X for a model facing -Y)."""

    def __init__(self, n, d):
        n = np.asarray(n, float)
        self.n, self.d = n / np.linalg.norm(n), float(d)
        if self.n[0] < 0:                       # the sign of a mirror plane is free: keep +X
            self.n, self.d = -self.n, -self.d

    @classmethod
    def from_angles(cls, yaw, pitch, d):
        """Normal (cos pitch cos yaw, cos pitch sin yaw, sin pitch): yaw turns it about Z, pitch tilts it towards Z."""
        return cls([math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch)], d)

    @classmethod
    def from_x(cls, x):
        return cls([1., 0., 0.], x)

    def signed(self, points):
        return np.asarray(points) @ self.n - self.d

    def reflect(self, points):
        points = np.asarray(points, float)
        return points - 2 * self.signed(points)[..., None] * self.n

    def reflect_vectors(self, vectors):
        vectors = np.asarray(vectors, float)
        return vectors - 2 * (vectors @ self.n)[..., None] * self.n

    @property
    def yaw(self):
        return math.atan2(self.n[1], self.n[0])

    @property
    def pitch(self):
        return math.asin(max(-1., min(1., self.n[2])))

    @property
    def tilt_deg(self):
        """Angle between the normal and the X axis."""
        return math.degrees(math.acos(max(-1., min(1., self.n[0]))))

    def x_at(self, y=0., z=1.):
        """x of the plane at (y, z); the plane x = const of the pipeline has the same x everywhere."""
        return (self.d - self.n[1] * y - self.n[2] * z) / self.n[0]

    def to_dict(self):
        return {'normal': [float(c) for c in self.n], 'offset': float(self.d),
                'yaw_deg': math.degrees(self.yaw), 'pitch_deg': math.degrees(self.pitch), 'tilt_deg': self.tilt_deg,
                'x_at_mid_height': float(self.x_at(0., 1.)), 'x_at_feet': float(self.x_at(0., 0.)),
                'x_at_head': float(self.x_at(0., 2.))}


def trimmed_mean(values, trim=TRIM):
    values = np.asarray(values)
    k = max(1, int(trim * len(values)))
    return float(np.partition(values, k - 1)[:k].mean())


def fit_plane(dist_fn, points, inits=(), trim=TRIM, scan=.12, scan_step=.01, start_steps=(.02, .02, .02),
              min_step=5e-5, max_rounds=60):
    """Mirror plane that minimises the trimmed mean of dist_fn(reflected points).

    `dist_fn(points) -> distances to the surface`, `points` are surface samples. Starts: a coarse scan of x = const
    offsets over +-`scan` plus the `inits` (Planes); each start is refined by coordinate pattern search over
    (yaw, pitch, offset) with halving steps. Returns (best Plane, info dict with the cost of every start)."""
    points = np.asarray(points, float)
    count = [0]

    def cost(p):
        count[0] += 1
        return trimmed_mean(dist_fn(p.reflect(points)), trim)

    starts = [(cost(Plane.from_x(x)), 'scan', Plane.from_x(x)) for x in np.arange(-scan, scan + 1e-9, scan_step)]
    starts.sort(key=lambda s: s[0])
    chosen = [starts[0]]
    for name, p in inits:       # an init that is the best scan plane anyway is not refined twice
        if abs(p.d - starts[0][2].d) > 1e-9 or abs(p.yaw) + abs(p.pitch) > 1e-9:
            chosen.append((cost(p), name, p))
    results = []
    for c0, name, p0 in chosen:
        x = np.array([p0.yaw, p0.pitch, p0.d])
        steps = np.array(start_steps, float)
        best = c0
        for _ in range(max_rounds):
            improved = False
            for i in range(3):
                for sign in (1., -1.):
                    y = x.copy()
                    y[i] += sign * steps[i]
                    c = cost(Plane.from_angles(*y))
                    if c < best - 1e-12:
                        best, x, improved = c, y, True
                        break
            if not improved:
                steps *= .5
                if steps[2] < min_step:
                    break
        results.append((best, name, Plane.from_angles(*x), c0))
    best = min(results, key=lambda r: r[0])
    return best[2], {'cost': best[0], 'start': best[1], 'evaluations': count[0],
                     'starts': {r[1]: {'cost_start': r[3], 'cost_refined': r[0], 'x_mid': float(r[2].x_at(0, 1))}
                                for r in results}}


# ---------------------------------------------------------------- surface

class Surface:
    """Triangle mesh (normalized frame) with a BVH, area-uniform sampling and optional UV/texture look-up.
    `uv`: (m, 3, 2) per triangle corner, `pixels`: (h, w, >=3) float image, row 0 = bottom (Blender)."""

    def __init__(self, v, f, uv=None, pixels=None):
        self.v, self.f = np.asarray(v, np.float64), np.asarray(f, np.int64)
        self.uv, self.pixels = uv, pixels
        self.normals, self.area = face_normals(self.v, self.f)
        self._tree = None

    @property
    def tree(self):
        if self._tree is None:
            from mathutils.bvhtree import BVHTree
            self._tree = BVHTree.FromPolygons(self.v.tolist(), self.f.tolist(), all_triangles=True)
        return self._tree

    def sample(self, count, seed=0):
        """Area-uniform points: (points, face ids, face normals)."""
        rng = np.random.default_rng(seed)
        ids = rng.choice(len(self.f), count, p=self.area / self.area.sum())
        r, s = np.sqrt(rng.random(count)), rng.random(count)
        w = np.stack((1 - r, r * (1 - s), r * s), axis=1)
        return np.einsum('ij,ijk->ik', w, self.v[self.f[ids]]), ids, self.normals[ids]

    def nearest(self, points):
        """(location, face normal, face id, distance) of the nearest surface point of each query."""
        from mathutils import Vector
        find = self.tree.find_nearest
        hits = [find(Vector(p)) for p in np.asarray(points, float)]
        return (np.array([tuple(h[0]) for h in hits]), np.array([tuple(h[1]) for h in hits]),
                np.array([h[2] for h in hits], np.int64), np.array([h[3] for h in hits]))

    def distances(self, points):
        from mathutils import Vector
        find = self.tree.find_nearest
        return np.array([find(Vector(p))[3] for p in np.asarray(points, float)])

    def rgb_at(self, ids, locations):
        """Bilinear texture colour at surface points `locations` lying on triangles `ids` (barycentric UV)."""
        tri = self.v[self.f[ids]]
        a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
        e0, e1, q = b - a, c - a, np.asarray(locations) - a
        dot = lambda x, y: np.einsum('ij,ij->i', x, y)
        d00, d01, d11, d20, d21 = dot(e0, e0), dot(e0, e1), dot(e1, e1), dot(q, e0), dot(q, e1)
        den = np.maximum(d00 * d11 - d01 * d01, 1e-30)
        u, w = (d11 * d20 - d01 * d21) / den, (d00 * d21 - d01 * d20) / den
        bary = np.clip(np.stack([1 - u - w, u, w], axis=1), 0, 1)
        bary /= np.maximum(bary.sum(axis=1, keepdims=True), 1e-12)
        uv = np.einsum('ni,nij->nj', bary, self.uv[ids])
        h, wd = self.pixels.shape[:2]
        xy = np.maximum(np.clip(uv, 0, 1) * np.array([wd, h]) - .5, 0)
        low = np.floor(xy).astype(int)
        low[:, 0], low[:, 1] = np.minimum(low[:, 0], wd - 1), np.minimum(low[:, 1], h - 1)
        high = np.minimum(low + 1, [wd - 1, h - 1])
        t = xy - low
        p = self.pixels
        return (((p[low[:, 1], low[:, 0], :3] * (1 - t[:, :1]) + p[low[:, 1], high[:, 0], :3] * t[:, :1]) * (1 - t[:, 1:]))
                + ((p[high[:, 1], low[:, 0], :3] * (1 - t[:, :1]) + p[high[:, 1], high[:, 0], :3] * t[:, :1]) * t[:, 1:]))


def face_normals(v, f):
    return face_labels.face_normals(v, f)


# ---------------------------------------------------------------- body part families

def part_family(name):
    """`arm_stretch.l` -> `arm_stretch`; central parts (`.x`) and unsided parts (`skirt`) keep their name."""
    return name[:-2] if name[-2:] in ('.l', '.r', '.x') else name


def part_side(name):
    """0 central/unsided, 1 anatomical left (+X), 2 anatomical right (-X)."""
    return 1 if name.endswith('.l') else 2 if name.endswith('.r') else 0


def expected_asymmetric_families(module):
    return tuple(EXPECTED_ASYMMETRIC.get(module, ()))


# ---------------------------------------------------------------- measurement

def measure(surface, plane, points, palette, cameras=(), parts=None):
    """Mirror statistics at surface points. Each query point is first snapped to the nearest surface point; its
    partner is the nearest surface point to the reflected snapped point.

    Returns a dict of arrays (one entry per point): p (snapped point), n (face normal), geo (distance of the reflected
    point to the surface, normalized units), nagree (dot of the reflected normal and the partner's normal),
    role/weight of the texture at p and role_p/weight_p at the partner (palette.Palette.classify: 0 base incl. all
    anchors, 1..K garment roles, K+1 unusable), vis/vis_p (best concept-camera view cosine, 0 = never seen; all 1
    without cameras) and, with `parts` (a fitted face_labels.BaseParts), part/part_p ids."""
    loc, nrm, idx, _ = surface.nearest(points)
    refl = plane.reflect(loc)
    ploc, pnrm, pidx, geo = surface.nearest(refl)
    rec = {'p': loc, 'n': nrm, 'partner': ploc, 'n_p': pnrm, 'geo': geo,
           'nagree': (plane.reflect_vectors(nrm) * pnrm).sum(1)}
    if surface.pixels is not None:
        rec['rgb'], rec['rgb_p'] = surface.rgb_at(idx, loc), surface.rgb_at(pidx, ploc)
        rec['role'], rec['weight'] = palette.classify(rec['rgb'])
        rec['role_p'], rec['weight_p'] = palette.classify(rec['rgb_p'])
    if len(cameras):
        rec['vis'] = face_labels.visibility(loc, nrm, surface.tree, cameras)
        rec['vis_p'] = face_labels.visibility(ploc, pnrm, surface.tree, cameras)
    else:
        rec['vis'] = rec['vis_p'] = np.ones(len(loc))
    if parts is not None:
        rec['part'] = parts.nearest_part(loc, nrm)
        rec['part_p'] = parts.nearest_part(ploc, pnrm)
    return rec


def derived(rec, garments):
    """Boolean masks used by every statistic: matched (the partner is a real counterpart), conf_both (both sides
    confidently classified), seen_both (both well seen), garment/garment_p (a garment role on either side),
    disagree (roles differ)."""
    d = {'matched': (rec['geo'] <= MATCH_MAX_DIST) & (rec['nagree'] >= MATCH_MIN_NORMAL),
         'seen_both': np.minimum(rec['vis'], rec['vis_p']) >= SEEN_COS}
    if 'role' in rec:
        conf = lambda role, w: (role <= garments) & (w >= COLOUR_WEIGHT_MIN)
        d['conf'], d['conf_p'] = conf(rec['role'], rec['weight']), conf(rec['role_p'], rec['weight_p'])
        d['conf_both'] = d['conf'] & d['conf_p']
        d['garment'], d['garment_p'] = d['conf'] & (rec['role'] >= 1), d['conf_p'] & (rec['role_p'] >= 1)
        d['disagree'] = rec['role'] != rec['role_p']
        d['compared'] = d['conf_both'] & d['matched']
    return d


def _quantiles(x, qs=(.5, .9, .95)):
    if not len(x):
        return None
    out = {f'p{int(round(q * 100))}': float(np.quantile(x, q)) for q in qs}
    out['mean'] = float(np.mean(x))
    return out


def _share(num, den):
    return float(num / den) if den else None


def region_stats(rec, d, mask, total):
    """Statistics of the samples in `mask`. Geometry in per cent of height; colour shares among the samples where
    both sides are confidently classified and the partner is a real counterpart (`compared`)."""
    n = int(mask.sum())
    out = {'samples': n, 'area_share': n / max(total, 1)}
    if not n:
        return out
    geo = rec['geo'][mask] * PCT
    out['geo_pct_height'] = _quantiles(geo)
    seen = mask & d['seen_both']
    out['seen_both_share'] = _share(int(seen.sum()), n)
    out['geo_seen_pct_height'] = _quantiles(rec['geo'][seen] * PCT) if seen.sum() >= 20 else None
    out['normal_agree_mean'] = float(rec['nagree'][mask].mean())
    out['matched_share'] = float(d['matched'][mask].mean())
    if 'compared' in d:
        comp = mask & d['compared']
        garment = comp & (d['garment'] | d['garment_p'])
        well, poor = comp & d['seen_both'], comp & ~d['seen_both']
        dis = d['disagree']
        out['colour'] = {
            'compared': int(comp.sum()), 'compared_share': _share(int(comp.sum()), n),
            'agree': _share(int((comp & ~dis).sum()), int(comp.sum())),
            'garment_compared': int(garment.sum()),
            'garment_agree': _share(int((garment & ~dis).sum()), int(garment.sum())),
            'well_seen_compared': int(well.sum()),
            'well_seen_disagree': _share(int((well & dis).sum()), int(well.sum())),
            'poorly_seen_compared': int(poor.sum()),
            'poorly_seen_disagree': _share(int((poor & dis).sum()), int(poor.sum())),
            'well_seen_garment_disagree': _share(int((garment & well & dis).sum()), int((garment & well).sum()))}
        if 'side' in rec:
            conf = mask & d['conf']
            out['colour']['garment_share_by_side'] = {
                name: _share(int((conf & (rec['side'] == s) & d['garment']).sum()), int((conf & (rec['side'] == s)).sum()))
                for name, s in (('left', 1), ('right', 2))}
    return out


def flag_region(stats):
    """Why a region (stats of `region_stats`) counts as well-seen asymmetric: list of 'colour' / 'geometry'."""
    why = []
    c = stats.get('colour') or {}
    if (c.get('well_seen_compared') or 0) >= FLAG_COLOUR_MIN and (c.get('well_seen_disagree') or 0) >= FLAG_COLOUR:
        why.append('colour')
    g = stats.get('geo_seen_pct_height')
    if g and (stats['samples'] * (stats.get('seen_both_share') or 0)) >= FLAG_GEO_MIN and g['p95'] >= FLAG_GEO_P95:
        why.append('geometry')
    return why


def summarise(rec, names, module, garments, cell=CELL):
    """Global, per-family, per-height-band and hot-spot statistics for the samples of `measure`.

    `names`: body part names (None without a parts file; then no families). Samples in the module's
    EXPECTED_ASYMMETRIC families are summarised under `expected_asymmetric` and left out of `global` and of the
    flagged `asymmetric_regions`."""
    total = len(rec['geo'])
    if names is not None:
        fam_names = [part_family(n) for n in names]
        rec['family'] = np.array([fam_names[p] for p in rec['part']])
        rec['side'] = np.array([part_side(names[p]) for p in rec['part']])
    d = derived(rec, garments)
    expected = expected_asymmetric_families(module)
    is_expected = np.isin(rec['family'], expected) if names is not None else np.zeros(total, bool)
    keep = ~is_expected
    out = {'samples': total, 'expected_asymmetric_families': list(expected),
           'global_all': region_stats(rec, d, np.ones(total, bool), total),
           'global': region_stats(rec, d, keep, total)}
    out['expected_asymmetric'] = region_stats(rec, d, is_expected, total) if is_expected.any() else None
    if names is not None:
        out['by_family'] = {}
        for fam in sorted(set(fam_names)):
            m = rec['family'] == fam
            if m.sum() >= 30:
                s = region_stats(rec, d, m, total)
                s['expected_asymmetric'] = fam in expected
                s['counterpart_part_share'] = float(np.mean([part_family(names[a]) == part_family(names[b]) and
                                                             (part_side(names[a]) + part_side(names[b]) in (0, 3))
                                                             for a, b in zip(rec['part'][m], rec['part_p'][m])]))
                s['flags'] = [] if fam in expected else flag_region(s)
                out['by_family'][fam] = s
    z = rec['p'][:, 2]
    out['by_height_band'] = {}
    for i in range(10):
        m = keep & (z >= i * .2) & (z < (i + 1) * .2 + (1e-9 if i == 9 else 0))
        if m.sum() >= 30:
            s = region_stats(rec, d, m, total)
            out['by_height_band'][f'{i * .2:.1f}-{(i + 1) * .2:.1f}'] = {
                'samples': s['samples'], 'geo_pct_height': s['geo_pct_height'],
                'colour': {k: s['colour'][k] for k in ('compared', 'agree', 'well_seen_disagree')} if 'colour' in s else None}
    # coarse (|x|, z) hot spots: where well-seen asymmetry concentrates, away from the intended asymmetric regions
    ax = np.minimum((np.abs(rec['p'][:, 0]) / cell).astype(int), 6)
    az = np.clip((z / cell).astype(int), 0, 9)
    cells = []
    for i in range(7):
        for j in range(10):
            m = keep & (ax == i) & (az == j)
            if m.sum() < 60:
                continue
            s = region_stats(rec, d, m, total)
            s['cell'] = {'abs_x': [i * cell, (i + 1) * cell], 'z': [j * cell, (j + 1) * cell]}
            s['flags'] = flag_region(s)
            cells.append(s)
    cells.sort(key=lambda s: -((s.get('colour') or {}).get('well_seen_disagree') or 0) * (1 + (s['samples'] > 150)))
    out['hotspots'] = [{'cell': s['cell'], 'samples': s['samples'], 'flags': s['flags'],
                        'well_seen_colour_disagree': (s.get('colour') or {}).get('well_seen_disagree'),
                        'well_seen_compared': (s.get('colour') or {}).get('well_seen_compared'),
                        'geo_seen_p95_pct': (s.get('geo_seen_pct_height') or {}).get('p95')} for s in cells[:8]]
    out['asymmetric_regions'] = [{'family': fam, 'flags': s['flags'], 'samples': s['samples'],
                                  'well_seen_colour_disagree': (s.get('colour') or {}).get('well_seen_disagree'),
                                  'geo_seen_p95_pct': (s.get('geo_seen_pct_height') or {}).get('p95')}
                                 for fam, s in out.get('by_family', {}).items() if s['flags']]
    out['asymmetric_cells'] = [{'cell': s['cell'], 'flags': s['flags']} for s in cells if s['flags']]
    return out


def headline(summary):
    """Few numbers per model for batch tables (expected-asymmetric regions excluded)."""
    g, e = summary['global'], summary.get('expected_asymmetric') or {}
    gc, ec = g.get('colour') or {}, e.get('colour') or {}
    return {'geo_p50_pct': (g.get('geo_pct_height') or {}).get('p50'), 'geo_p95_pct': (g.get('geo_pct_height') or {}).get('p95'),
            'geo_seen_p95_pct': (g.get('geo_seen_pct_height') or {}).get('p95'),
            'normal_agree': g.get('normal_agree_mean'), 'matched_share': g.get('matched_share'),
            'seen_both_share': g.get('seen_both_share'),
            'colour_agree': gc.get('agree'), 'colour_garment_agree': gc.get('garment_agree'),
            'colour_garment_compared': gc.get('garment_compared'),
            'colour_well_seen_disagree': gc.get('well_seen_disagree'),
            'colour_well_seen_garment_disagree': gc.get('well_seen_garment_disagree'),
            'expected_asym_share': e.get('area_share'), 'expected_asym_geo_p95_pct': (e.get('geo_pct_height') or {}).get('p95'),
            'expected_asym_colour_agree': ec.get('agree'), 'expected_asym_garment_agree': ec.get('garment_agree'),
            'flagged_families': [r['family'] for r in summary.get('asymmetric_regions', [])]}


GARMENT_SCORE_MIN = 300     # compared garment samples needed before the garment colour term counts (else only geometry)


def asymmetry_score(h):
    """One number for ranking models of the same module: p95 mirror distance (% height) plus 20 x the garment colour
    disagreement (only when at least GARMENT_SCORE_MIN garment samples were compared)."""
    geo = h.get('geo_p95_pct') or 0.
    garment = h.get('colour_garment_agree')
    if garment is None or (h.get('colour_garment_compared') or 0) < GARMENT_SCORE_MIN:
        return geo
    return geo + 20. * (1. - garment)
