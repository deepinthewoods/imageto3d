"""Align a converted wearable to the game base body in two stages (2026-10-25, prototype; only ADDS files).

    python3 tools/tripo_pipeline/align_body.py <module_dir> [<module_dir> ...] [--all] [--match S] [--levels 0p4,0p2,0p1]
        [--jobs N] [--force] [--measure-only] [--bodies-only] [--summary FILE]

<module_dir> = models/<set>/<job-id>/<race>/<module>. Reads reduced/manifest.json, the level wearables (`reduced/lod_error_<lv>__wearable.glb`),
the rigid pieces (`segment_first/pieces/lod_<lv>/*.glb` or `pieces/lod_<lv>/*.glb`), the bare-body evidence (high-poly
`wearcut/prep/prep.npz` + `wearcut/labels.npz`, else the labelled LOD `reduced/lod_error_<lv>.glb` + `_colors.npz`) and the game base
(`tools/preview/cache/bases/<race>.npz` + `<race>_rig.npz`; the rig comes from `blender -b -P tools/preview/export_bases.py`).
Nothing existing is changed (`parts.fit`, labels, labelled LODs, fitted outputs).

Why: the pipeline places a Tripo body on the game base with a uniform-scale + translation ICP on the WHOLE mesh including the armour.
Measured on 44 modules (experiments/alignment_20261025): the bare Tripo body is 5-9 % too deep in Y, ~1 % tall, its feet 6-16 mm high and
outward, and its arms/legs are posed differently per module (shoulder ~4.5 deg, elbow ~7 deg, up to 18 deg).

Stage 1, global placement: per-axis scale (0.80-1.15) + rotation (<= 5 deg) + translation, a trimmed robust point-to-plane ICP of the
BARE-body faces (role base incl. the white anchors; faces within ~8 mm / 1 ring of the garment boundary and faces whose label source is
off_body / riser / prior are left out; samples balanced per body part with extra weight on the white anchors head, hand.l, foot.l)
onto the game base surface. The residual is also measured on held-out spatial cells.
Stage 2, skeletal un-posing: per-bone rotations of the limb chains (upper arm, forearm, hand, thigh, shin, foot, left and right; rotations
about the rest joints, hierarchical) are fitted so that the linear-blend-skinned base matches the bare Tripo skin (robust, regularised toward
rest, joint limits, a strong penalty on twist about the bone axis). A bone without bare evidence is carried by its children and, if it has
none either, by the garment's own surface as a weak proxy (shell offset kept) or stays at rest; every bone gets a confidence. The wearable is
un-posed with skin weights taken from the nearest POSED base surface (kernel-smoothed), applying the inverse blended LBS transform.
The WEARABLE and the rigid pieces get a reduced stage 1 (default variant v2, `WEAR_SCALE`): rotation + translation, no scale (the Tripo frame of the garment is already at base
scale from the pipeline's whole-mesh ICP; a geometric-mean uniform scale was measured worse), not the per-axis scale. That scale is fitted on bare limbs / head and, applied to armour, squashed the garment depth onto
the base torso: the unfitted poke area grew on body_armor (dwarf_female 1742 -> 4821 cm2). The bare-body evidence keeps the per-axis fit for
measuring and for stage 2; the split is recorded in align.json stage1.wearable_scale_mode / wearable_scale. `--variant v1` applies the per-axis scale
to the wearable too; `--variant core` is the experimental core-first alternative (README).
The whole field D(x) = unpose(T1(x)) is a function of the world position only, so pieces of one module that share boundary vertices stay
together. D is baked back into the NATIVE frame with the original manifest / parts.fit (transform.world_to_native), so
native_to_world stays the single frame conversion. right_gauntlet_boot: the left piece (faces flagged mirrored) is re-created as the exact
reflection of the aligned right piece across the game base's sagittal plane.

Outputs (beside the originals, flat shading / materials / attributes preserved exactly like body_fit.py's fitted files):
  reduced/lod_error_<lv>__wearable_aligned.glb, <pieces dir>/lod_<lv>_aligned/<piece>.glb, <module>/fit/align.json
  (then `body_fit.py --source aligned` writes ..._aligned_fitted.* and fit/body_fit_aligned.json).
Red-body overlays for the preview (a run also writes them; `--bodies-only` writes just them from an existing fit/align.json, no refit):
  reduced/lod_error_<lv>__tripo_body.glb          the Tripo model's own body (base-role faces of the labelled LOD, white anchors white) as converted
  reduced/lod_error_<lv>__tripo_body_aligned.glb  the same faces deformed with the field the WEARABLE got (stage 1 without the per-axis scale + un-posing)
  fit/tripo_body.json                             what was written / skipped per level
Needs numpy + scipy (re-runs itself with characters/.venv when scipy is missing).
"""
import os

for _k in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(_k, '1')
import argparse
import copy
import datetime
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

try:
    import numpy as np
    import scipy  # noqa: F401
except ImportError:
    _venv = ROOT.parent / '.venv/bin/python'
    if __name__ == '__main__' and not os.environ.get('ALIGN_BODY_REEXEC') and _venv.is_file():
        os.execve(str(_venv), [str(_venv), *sys.argv], dict(os.environ, ALIGN_BODY_REEXEC='1'))
    raise SystemExit('align_body.py needs numpy + scipy (characters/.venv/bin/python)')
from scipy import sparse
from scipy.optimize import least_squares
from scipy.spatial import cKDTree
from scipy.spatial.transform import Rotation as Rot

import body_fit as bf  # noqa: E402
from rewear import MODULES, discover, identity, module_busy   # noqa: E402

tf = bf.tf
CODE_VERSIONS = {'v2': 'align-20261025-v2', 'v1': 'align-20261025', 'core': 'align-20261025-core'}
VARIANTS = ('v2', 'v1', 'core')  # v2 (default): global fit of all bare skin, bone rotations, and the WEARABLE / pieces get stage 1 without the per-axis scale (see WEAR_SCALE);
#                                  v1: the first prototype (the wearable gets the per-axis scale too); core: core-first stage 1 + bone lengths / root offsets (experimental)
WEAR_SCALE = 'none'              # the stage-1 scale the wearable gets in v2: 'none', 'uniform' (geometric mean of the per-axis scales; measured WORSE than none, the armour is already at base scale) or 'axes' (v1). The per-axis scale is fitted on bare
#                                  skin (limbs, head); applied to armour it squashes the garment depth onto the base torso and made the unfitted poke grow (measured)
LEVELS = bf.LEVELS
SUFFIX = '_aligned'
BODY_SUFFIX = '__tripo_body'      # red-body overlay of the preview: lod_error_<lv>__tripo_body.glb and ..._aligned.glb
BODY_RGB, ANCHOR_RGB = (230 / 255, 25 / 255, 35 / 255), (237 / 255, 237 / 255, 237 / 255)    # #E61923 base red, #EDEDED white anchor
EVIDENCE_LOD = ('0p2', '0p4', '0p1')

# ---- stage 1
SCALE_BOUNDS = (.80, 1.15)       # per axis (measured Y scale of Tripo bodies down to 0.82)
ROT_MAX_DEG = 5.
TRANS_MAX = .10                  # m
FIT_SAMPLES = 16000
EVAL_SAMPLES = 9000
GROUP_MIN_AREA = 1e-3            # m^2: a (part, anchor) group with less bare surface is ignored
ANCHOR_BOOST = 2.
EXCLUDE_MM = 8.                  # garment boundary margin of the evidence (rings = ceil(EXCLUDE_MM / mean edge), at least 1)
BAD_SOURCES = ('prior', 'off_body', 'riser', 'mirrored')
TANGENT_WEIGHT = .3
TRIM = .85
HOLD_CELL = .06                  # m: held-out evidence = 20 % of the spatial cells of this size
HOLD_SHARE = .2
SCALE_PRIOR = .003 / .12         # ridge on log scale: a 12 % scale deviation costs as much as 3 mm of residual on average

# ---- stage 2
LIMBS = ('arm', 'forearm', 'hand', 'thigh', 'leg', 'foot')
PARENT = {'arm': None, 'forearm': 'arm', 'hand': 'forearm', 'thigh': None, 'leg': 'thigh', 'foot': 'leg'}
LIMIT_DEG = {'arm': 45., 'forearm': 50., 'hand': 40., 'thigh': 40., 'leg': 45., 'foot': 30.}
LAMBDA_SWING = 2e-4              # prior cost per rad^2 of rotation off the bone axis (data cost is a mean square error in m^2)
LAMBDA_TWIST = 2e-2              # ... and about it (twist is weakly observable)
TAIL_HEAD = {'arm': 'c_arm_twist', 'forearm': 'c_forearm_stretch', 'hand': 'hand', 'thigh': 'c_thigh_twist', 'leg': 'c_leg_stretch', 'foot': 'foot'}
OUTER = 12
CORE_PARTS = ('head.x', 'neck.x', 'spine_01.x', 'root.x')      # stage 1 of the core-first variant sees only these (bare) body parts
TORSO_PARTS = ('spine_01.x', 'root.x')
CORE_STRONG_AREA = .08           # m^2 of bare torso + pelvis (usable) needed for a per-axis scale / rotation; less = weak core: translation only, scale / rotation pinned
WEAK_SCALE_BOUNDS = (.985, 1.015)
WEAK_ROT_MAX_DEG = 1.
CORE_SCALE_BOUNDS = (.90, 1.10)
CORE_ROT_MAX_DEG = 3.
CORE_PRIOR_SCALE = .003 / .05    # core evidence: a 5 % scale deviation costs as much as 3 mm of residual
CORE_PRIOR_ROT = .003 / np.radians(1.5)   # ... and 1.5 degrees of rotation
LEN_MAX = .06                    # per-bone length scale along the bone axis, +-6 %
OFF_MAX = .015                   # chain-root offset (shoulder, hip), +-15 mm
LAMBDA_LEN = 2e-3                # prior cost per unit^2 of (length scale - 1)
LAMBDA_OFF = 5e-2                # prior cost per m^2 of root offset
ROOTS = (0, 3, 6, 9)             # arm.l, thigh.l, arm.r, thigh.r in Rig.names
PROXY_MASS = .04                 # share of the data mass a bone without bare evidence gets from the garment shell
PROXY_MAX_D = .15
MIN_OWN = 400                    # bare samples whose dominant limb weight is this bone: confidence high
KERNEL_SIGMA_PCT = 4.            # skin-weight kernel width, % of body height (~72 mm): wider = smoother blend zones = less edge strain (2 % gave p99 strain 17 % on an A-posed orc)
KERNEL_K = 768                   # nearest posed-surface samples used (about 3 sigma around the foot point with FIELD_SAMPLES samples)
FIELD_SAMPLES = 8000
STRAIN_WARN_P99 = 8.             # stage-2-only edge strain (%) above which a module is flagged
STRAIN_WARN_MAX = 30.
SIMPLE = {'head.x': 'head', 'neck.x': 'neck', 'spine_01.x': 'torso', 'root.x': 'pelvis', 'skirt': 'skirt', 'shoulder.l': 'shoulder.l', 'shoulder.r': 'shoulder.r',
          'arm_stretch.l': 'upperarm.l', 'arm_stretch.r': 'upperarm.r', 'forearm_stretch.l': 'forearm.l', 'forearm_stretch.r': 'forearm.r',
          'hand.l': 'hand.l', 'hand.r': 'hand.r', 'thigh_stretch.l': 'thigh.l', 'thigh_stretch.r': 'thigh.r', 'leg_stretch.l': 'shin.l',
          'leg_stretch.r': 'shin.r', 'foot.l': 'foot.l', 'foot.r': 'foot.r'}
ANCHORS = (('head', 'head.x'), ('hand.l', 'hand.l'), ('foot.l', 'foot.l'))


def rel(path):
    return bf.rel(path)


def mm(x):
    return float(x) * 1000.


# ----------------------------------------------------------------------------------------------- rig

def channel_of_group(name):
    """(limb, side) a base vertex group (Auto-Rig Pro deform bone) belongs to, or None (static: spine, shoulders, neck, head, root)."""
    side = 'l' if name.endswith('.l') else ('r' if name.endswith('.r') else None)
    if side is None and not name.startswith('c_surface_foot'):
        return None
    if name.startswith('c_surface_foot'):
        side = 'l' if name.endswith('.l') else 'r'
    for prefix, limb in (('c_forearm_', 'forearm'), ('c_arm_', 'arm'), ('hand.', 'hand'), ('c_thigh_', 'thigh'), ('c_leg_', 'leg'),
                         ('foot.', 'foot'), ('toes_', 'foot'), ('c_surface_foot', 'foot')):
        if name.startswith(prefix):
            return limb, side
    return None


class Rig:
    """The limb chains of a base: 12 channels (arm, forearm, hand, thigh, leg, foot; left then right) + channel 0 = static.
    `W` (vertices, 13) skin weights merged per channel, `pivot` (12,3) rest joint positions, `axis` (12,3) unit bone directions.
    A channel's pose is a rotation about its rest joint in its parent's frame: P_c = P_parent T(p + o) R S T(-p); a rest point x goes to P_c x.
    S = I + (s - 1) a a^T is an optional length scale along the bone axis a, o an optional offset of a chain root (arm, thigh)."""

    names = [f'{limb}.{side}' for side in 'lr' for limb in LIMBS]

    def __init__(self, W, pivot, axis):
        self.W, self.pivot, self.axis = np.asarray(W, np.float64), np.asarray(pivot, np.float64), np.asarray(axis, np.float64)
        self.parent = [(self.names.index(f'{PARENT[n.split(".")[0]]}.{n.split(".")[1]}') if PARENT[n.split('.')[0]] else -1) for n in self.names]
        self.limit = np.radians([LIMIT_DEG[n.split('.')[0]] for n in self.names])
        self.children = {i: [j for j, p in enumerate(self.parent) if p == i] for i in range(len(self.names))}

    @classmethod
    def from_npz(cls, path):
        z = np.load(path, allow_pickle=False)
        groups = [str(g) for g in z['group_names']]
        bones = [str(b) for b in z['bone_names']]
        n = len(z['weights'])
        W = np.zeros((n, 13))
        for gi, g in enumerate(groups):
            ch = channel_of_group(g)
            k = 0 if ch is None else 1 + cls.names.index(f'{ch[0]}.{ch[1]}')
            W[:, k] += z['weights'][:, gi]
        head, tail = z['bone_head'], z['bone_tail']
        piv = np.zeros((12, 3))
        axis = np.zeros((12, 3))
        for i, nm in enumerate(cls.names):
            limb, side = nm.split('.')
            piv[i] = head[bones.index(f'{TAIL_HEAD[limb]}.{side}')]
        for i, nm in enumerate(cls.names):
            limb, side = nm.split('.')
            nxt = {'arm': 'forearm', 'forearm': 'hand', 'thigh': 'leg', 'leg': 'foot'}.get(limb)
            if nxt:
                a = piv[cls.names.index(f'{nxt}.{side}')] - piv[i]
            else:
                b = bones.index(f'{TAIL_HEAD[limb]}.{side}')
                a = tail[b] - head[b]
            axis[i] = a / np.linalg.norm(a)
        return cls(W, piv, axis)

    def matrices(self, rv, ls=None, off=None):
        """(13,4,4) pose matrices (rest world -> posed world) for per-channel rotation vectors rv (12,3), optional length scales ls (12,)
        (1 = none) and chain-root offsets off (12,3)."""
        P = np.tile(np.eye(4), (13, 1, 1))
        for i in range(12):                                   # parents come before children in `names`
            R = Rot.from_rotvec(rv[i]).as_matrix()
            if ls is not None:
                R = R @ (np.eye(3) + (ls[i] - 1.) * np.outer(self.axis[i], self.axis[i]))
            T = np.eye(4)
            T[:3, :3] = R
            T[:3, 3] = self.pivot[i] - R @ self.pivot[i] + (off[i] if off is not None else 0.)
            p = self.parent[i]
            P[1 + i] = (P[1 + p] if p >= 0 else np.eye(4)) @ T
        return P

    def pose(self, V, W, M):
        """Linear blend skinning of rest points V (n,3) with weights W (n,13) and matrices M (13,4,4)."""
        out = np.zeros_like(V, dtype=np.float64)
        for c in range(13):
            w = W[:, c:c + 1]
            if np.any(w):
                out += w * (V @ M[c, :3, :3].T + M[c, :3, 3])
        return out


# ----------------------------------------------------------------------------------------------- evidence

def sample_triangles(A, B, C, area, idx, n, rng):
    """n area-weighted points on the triangles `idx`; returns (points, triangle index)."""
    w = area[idx] / area[idx].sum()
    f = rng.choice(idx, size=n, p=w)
    u, v = rng.random(n), rng.random(n)
    flip = u + v > 1
    u[flip], v[flip] = 1 - u[flip], 1 - v[flip]
    return A[f] + u[:, None] * (B[f] - A[f]) + v[:, None] * (C[f] - A[f]), f


def dilate_faces(F, nV, mask, rings):
    """Faces within `rings` vertex-adjacency hops of the faces in `mask` (inclusive)."""
    inc = sparse.csr_matrix((np.ones(F.size, np.float32), (np.repeat(np.arange(len(F)), 3), F.ravel())), shape=(len(F), nV))
    cur = mask.copy()
    for _ in range(rings):
        verts = (inc.T @ cur.astype(np.float32)) > 0
        cur = cur | ((inc @ verts.astype(np.float32)) > 0)
    return cur


def is_white(rgb):
    r = np.asarray(rgb, np.float64)
    if r.size and r.max() > 1.5:
        r = r / 255.
    mx, mn = r.max(-1), r.min(-1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-9), 0.)
    return (sat < .25) & (mx > .45)


def face_geometry(V, F):
    A, B, C = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    cr = np.cross(B - A, C - A)
    return A, B, C, np.linalg.norm(cr, axis=1) / 2


def hold_mask(centroids, seed=7):
    """Spatial held-out cells (20 %): True for faces/points in a held-out cell."""
    cell = np.floor(centroids / HOLD_CELL).astype(np.int64)
    h = (cell[:, 0] * 73856093) ^ (cell[:, 1] * 19349663) ^ (cell[:, 2] * 83492791) ^ seed
    return (np.abs(h) % 100) < HOLD_SHARE * 100


def build_evidence(V, F, bare, part, white, part_names, rings, garment, rng, source, extra=None):
    """Evidence sets from a triangle soup (world V, triangles F): `fit` (balanced per body part, anchors boosted), `eval` (area
    proportional, for reporting) and `garment` samples. bare / garment: face masks. Faces within `rings` hops of garment are dropped."""
    A, B, C, area = face_geometry(V, F)
    near = dilate_faces(F, len(V), garment, rings) if garment.any() else np.zeros(len(F), bool)
    use = bare & ~near & (area > 1e-12)
    cent = (A + B + C) / 3
    hold_f = hold_mask(cent)
    out = {'source': source, 'part_names': list(part_names), 'faces': len(F), 'bare_faces': int(bare.sum()), 'usable_faces': int(use.sum()),
           'bare_area_m2': float(area[bare].sum()), 'usable_area_m2': float(area[use].sum()), 'rings': int(rings),
           'garment_faces': int(garment.sum()), 'garment_area_m2': float(area[garment].sum())}
    # fit set: groups (part, white)
    pts, fidx, wts = [], [], []
    groups = {}
    keys = part.astype(np.int64) * 2 + white.astype(np.int64)
    budget = {}
    for k in np.unique(keys[use]):
        sel = use & (keys == k)
        a = float(area[sel].sum())
        if a < GROUP_MIN_AREA:
            continue
        budget[int(k)] = np.sqrt(a) * (ANCHOR_BOOST if k % 2 else 1.)
    tot = sum(budget.values())
    for k, b in budget.items():
        sel = np.flatnonzero(use & (keys == k))
        n = int(np.clip(round(FIT_SAMPLES * b / tot), 40, 4000))
        p, f = sample_triangles(A, B, C, area, sel, n, rng)
        pts.append(p), fidx.append(f), wts.append(np.full(n, b / n))
        groups[f'{part_names[k // 2]}{"+anchor" if k % 2 else ""}'] = n
    P, F_ = np.concatenate(pts), np.concatenate(fidx)
    W = np.concatenate(wts)
    out['fit'] = {'pts': P, 'part': part[F_], 'white': white[F_], 'w': W / W.sum(), 'hold': hold_f[F_]}
    out['groups'] = groups
    idx = np.flatnonzero(use)
    Pe, Fe = sample_triangles(A, B, C, area, idx, EVAL_SAMPLES, rng)
    out['eval'] = {'pts': Pe, 'part': part[Fe], 'white': white[Fe], 'hold': hold_f[Fe]}
    if garment.any():
        Pg, Fg = sample_triangles(A, B, C, area, np.flatnonzero(garment & (area > 1e-12)), 6000, rng)
        out['garment'] = {'pts': Pg, 'part': part[Fg]}
    else:
        out['garment'] = {'pts': np.zeros((0, 3)), 'part': np.zeros(0, np.int64)}
    return out


def mean_edge(V, F):
    e = np.linalg.norm(V[F[:, 0]] - V[F[:, 1]], axis=1)
    return float(e.mean())


def load_evidence(module_dir, manifest, body, rng, lods=EVIDENCE_LOD):
    """Bare-body evidence in the CURRENT world frame (manifest chain), from the high-poly labels when present, else the labelled LOD."""
    module_dir = Path(module_dir)
    fit = manifest['parts']['fit']
    prep, lab = module_dir / 'wearcut' / 'prep' / 'prep.npz', module_dir / 'wearcut' / 'labels.npz'
    if prep.is_file() and lab.is_file():
        z, L = np.load(prep), np.load(lab)
        if len(z['f']) == len(L['label']):
            v = tf.world_from_base(tf.base_from_review(z['v'].astype(np.float64), fit), body.H, body.o)
            F = z['f'].astype(np.int64)
            label = L['label'].astype(np.int64)
            snames = [str(s) for s in L['source_names']]
            bad = np.isin(L['source'].astype(np.int64), [snames.index(s) for s in BAD_SOURCES if s in snames])
            rings = int(np.clip(np.ceil(EXCLUDE_MM / 1000 / mean_edge(v, F)), 1, 12))
            col = z['colors'].astype(np.float64).mean(1)
            return build_evidence(v, F, (label == 0) & ~bad, z['part'].astype(np.int64), is_white(col), [str(x) for x in z['part_names']], rings,
                                  label > 0, rng, 'highpoly')
    for lv in lods:
        path, npz = module_dir / 'reduced' / f'lod_error_{lv}.glb', module_dir / 'reduced' / f'lod_error_{lv}_colors.npz'
        if not (path.is_file() and npz.is_file()):
            continue
        surf = bf.Surface(bf.Glb(path), manifest, body)
        z = np.load(npz, allow_pickle=True)
        order = z['glb_face_order']
        if len(order) != len(surf.T):
            continue
        mat = z['material_id'][order]
        names = [str(x) for x in z['label_source_names']]
        src = z['face_label_source'][order]
        bad = np.isin(src, [names.index(s) for s in BAD_SOURCES if s in names])
        part = z['body_part_glb_order'].astype(np.int64)
        return build_evidence(surf.Wc, surf.T, (mat == 0) & ~bad, part, is_white(z['face_mean_source_rgb'][order]) & (mat == 0),
                              [str(x) for x in z['body_part_names']], 1, mat > 0, rng, f'lod_{lv}')
    raise RuntimeError('no bare-body evidence (no wearcut prep/labels and no consistent labelled LOD)')


# ----------------------------------------------------------------------------------------------- stage 1

def t1_apply(p, X, c):
    """Y = c + t + R S (X - c) for p = [log sx, log sy, log sz, rotvec(3), t(3)]."""
    R = Rot.from_rotvec(p[3:6]).as_matrix()
    return c + p[6:9] + ((np.asarray(X, np.float64) - c) * np.exp(p[:3])) @ R.T


def t1_matrix(p, c):
    """The 4x4 affine of t1_apply."""
    R = Rot.from_rotvec(p[3:6]).as_matrix()
    A = R @ np.diag(np.exp(p[:3]))
    M = np.eye(4)
    M[:3, :3] = A
    M[:3, 3] = c + p[6:9] - A @ c
    return M


def closest_points(body, P):
    d, tri, bary = body.closest(P)
    c = bary[:, :1] * body.A[tri] + bary[:, 1:2] * body.B[tri] + bary[:, 2:] * body.C[tri]
    return d, tri, c


def plane_residual(d, n, tw=TANGENT_WEIGHT):
    dn = (d * n).sum(1, keepdims=True) * n
    return dn + tw * (d - dn)


def fit_stage1(body, ev, iters=25, mask=None, scale_bounds=SCALE_BOUNDS, rot_max=ROT_MAX_DEG, prior_scale=SCALE_PRIOR, prior_rot=0.):
    """Trimmed robust point-to-plane ICP of the fit evidence (optionally only the samples in `mask`) onto the game base: per-axis scale,
    small rotation, translation, with ridge priors toward scale 1 / rotation 0."""
    use = ~ev['hold'] if mask is None else (~ev['hold'] & mask)
    pts, w = ev['pts'][use], ev['w'][use]
    w = w / w.sum()
    c = np.array([body.o[0], body.o[1], body.o[2] + .55 * body.H])
    lo = np.r_[np.log(scale_bounds[0]) * np.ones(3), -np.radians(rot_max) * np.ones(3), -TRANS_MAX * np.ones(3)]
    hi = np.r_[np.log(scale_bounds[1]) * np.ones(3), np.radians(rot_max) * np.ones(3), TRANS_MAX * np.ones(3)]
    p = np.zeros(9)
    hist = []
    for it in range(iters):
        Y = t1_apply(p, pts, c)
        d, tri, cp = closest_points(body, Y)
        keep = (d <= np.quantile(d, TRIM)) & (d < .12)
        X, cc, n, ww = pts[keep], cp[keep], body.tri_n[tri[keep]], np.sqrt(w[keep] / w[keep].sum())

        def fun(q):
            r = plane_residual(t1_apply(q, X, c) - cc, n) * ww[:, None]
            return np.r_[r.ravel(), prior_scale * q[:3], prior_rot * q[3:6]]
        sol = least_squares(fun, p, bounds=(lo, hi), loss='soft_l1', f_scale=.02, max_nfev=40)
        delta = np.abs(sol.x - p).max()
        p = sol.x
        hist.append(float(np.sqrt(np.average(d[keep] ** 2))))
        if delta < 1e-6:
            break
    return {'p': p, 'c': c, 'iterations': it + 1, 'rms_m': hist}


def core_stage1(body, ev):
    """Stage 1 of the core-first variant: only the bare head, neck, torso and pelvis (white head anchor included) are fitted, never limb skin,
    so an arm or leg posed differently cannot turn or shift the whole body. With little bare torso (body armour hides it: `weak` core) the scale
    and rotation are pinned near 1 / 0 and the fit is essentially the translation of head + neck (+ whatever pelvis is bare); the limbs' length and pose
    then come from stage 2. Returns the stage-1 dict of fit_stage1 plus `mode`, `core_samples`, `torso_area_m2`."""
    names = ev['part_names']
    core = [i for i, n in enumerate(names) if n in CORE_PARTS]
    torso = [i for i, n in enumerate(names) if n in TORSO_PARTS]
    e = ev['eval']
    share = float(np.isin(e['part'], torso).mean()) if len(e['part']) else 0.
    torso_area = share * ev['usable_area_m2']
    mask = np.isin(ev['fit']['part'], core)
    strong = torso_area >= CORE_STRONG_AREA
    if mask.sum() < 100:
        mode, kw = 'none', {'scale_bounds': (1., 1. + 1e-9), 'rot_max': 1e-6, 'prior_scale': 0., 'prior_rot': 0.}
    elif strong:
        mode, kw = 'strong', {'scale_bounds': CORE_SCALE_BOUNDS, 'rot_max': CORE_ROT_MAX_DEG, 'prior_scale': CORE_PRIOR_SCALE, 'prior_rot': CORE_PRIOR_ROT}
    else:
        mode, kw = 'weak', {'scale_bounds': WEAK_SCALE_BOUNDS, 'rot_max': WEAK_ROT_MAX_DEG, 'prior_scale': CORE_PRIOR_SCALE * 10, 'prior_rot': CORE_PRIOR_ROT * 10}
    s1 = fit_stage1(body, ev['fit'], mask=mask, **kw) if mode != 'none' else {'p': np.zeros(9), 'c': np.array([body.o[0], body.o[1], body.o[2] + .55 * body.H]), 'iterations': 0, 'rms_m': []}
    if mode == 'none':                                   # no core evidence at all: nothing to place the body with
        s1['p'] = np.zeros(9)
    s1.update({'mode': mode, 'core_samples': int(mask.sum()), 'torso_area_m2': float(torso_area)})
    return s1


# ----------------------------------------------------------------------------------------------- stage 2

def fit_stage2(body, rig, Yb, wb, Yg, outer=OUTER, lengths=False):
    """Per-bone rotations rv (12,3) such that the LBS-posed base matches the bare skin points Yb (weights wb, normalised); Yg = garment
    surface points for bones without bare evidence (weak proxy). With `lengths` also a length scale along every bone axis (+-LEN_MAX, prior toward 1)
    and an offset of each chain root (arm, thigh; +-OFF_MAX, prior toward 0). Returns (rv, info); info['length_scale'] (12,) and info['root_offset']
    (12,3) are always present (ones / zeros without `lengths`)."""
    F = body.F
    rv = np.zeros((12, 3))
    ls = np.ones(12)
    off = np.zeros((12, 3))
    lim = np.repeat(rig.limit[:, None], 3, axis=1)
    nq = 36 + (12 + 12 if lengths else 0)
    unpack = lambda q: (q[:36].reshape(12, 3), (1. + q[36:48]) if lengths else None, _root_offsets(q[48:60]) if lengths else None)
    lo_q = np.r_[-lim.ravel(), -LEN_MAX * np.ones(12), -OFF_MAX * np.ones(12)][:nq]
    hi_q = -lo_q
    wb = wb / wb.sum()
    proxy_delta = {}
    info = {'iterations': 0, 'rms_m': []}
    for it in range(outer):
        M = rig.matrices(rv, ls if lengths else None, off if lengths else None)
        posed = bf.Body(rig.pose(body.V, rig.W, M), F, body.part, body.part_names)
        d, tri, bary = posed.closest(Yb)
        keep = (d <= np.quantile(d, TRIM)) & (d < .08)
        Wc = np.einsum('ki,kij->kj', bary[keep], rig.W[F[tri[keep]]])
        x = np.einsum('ki,kij->kj', bary[keep], body.V[F[tri[keep]]])
        n = posed.tri_n[tri[keep]]
        limb = Wc[:, 1:].sum(1) >= .05
        Y, w = Yb[keep][limb], wb[keep][limb]
        x, Wc, n = x[limb], Wc[limb], n[limb]
        dom = Wc[:, 1:].argmax(1)
        own = np.bincount(dom[Wc[np.arange(len(dom)), 1 + dom] > .5], minlength=12)
        w = w / w.sum() if len(w) else w
        if it == 0:
            info['own'] = own.tolist()
            info['desc'] = [int(own[i] + sum(own[j] for j in _descendants(rig, i))) for i in range(12)]
        low = [i for i in range(12) if info['desc'][i] < MIN_OWN]
        Yp, xp, np_, wp, delta = np.zeros((0, 3)), np.zeros((0, 3)), np.zeros((0, 3)), np.zeros(0), np.zeros(0)
        Wp = np.zeros((0, 13))
        if len(Yg) and low:
            dg, tg, bg = posed.closest(Yg)
            Wg = np.einsum('ki,kij->kj', bg, rig.W[F[tg]])
            domg = Wg[:, 1:].argmax(1)
            sel = (dg < PROXY_MAX_D) & np.isin(domg, low) & (Wg[np.arange(len(domg)), 1 + domg] > .5)
            Yp, Wp, domp, np_ = Yg[sel], Wg[sel], domg[sel], posed.tri_n[tg[sel]]
            xp = np.einsum('ki,kij->kj', bg[sel], body.V[F[tg[sel]]])
            cp = np.einsum('ki,kij->kj', bg[sel], posed.V[F[tg[sel]]])
            off_n = ((Yp - cp) * np_).sum(1)
            wp, delta = np.zeros(len(Yp)), np.zeros(len(Yp))
            for i in low:
                m = domp == i
                if m.sum() < 30:
                    continue
                if i not in proxy_delta:
                    proxy_delta[i] = float(np.median(off_n[m]))
                wp[m] = PROXY_MASS / m.sum()
                delta[m] = proxy_delta[i]
            has = wp > 0
            Yp, xp, Wp, np_, wp, delta = Yp[has], xp[has], Wp[has], np_[has], wp[has], delta[has]
        info['n_proxy'] = {rig.names[i]: int((np.argmax(Wp[:, 1:], 1) == i).sum()) if len(Wp) else 0 for i in range(12)}
        sw = np.sqrt(np.r_[w, wp])
        XA, WA, NA = np.concatenate([x, xp]), np.concatenate([Wc, Wp]), np.concatenate([n, np_])
        YA, shell_off = np.concatenate([Y, Yp]), np.concatenate([np.zeros(len(Y)), delta])
        sw = sw / np.sqrt((sw ** 2).sum()) if len(sw) else sw

        shell = shell_off                                # normal offset of the garment proxy samples
        def fun(q):
            rq, lq, oq = unpack(q)
            Mq = rig.matrices(rq, lq, oq)
            tgt = rig.pose(XA, WA, Mq) + shell[:, None] * NA
            r = plane_residual(YA - tgt, NA) * sw[:, None]
            axis = rig.axis
            twist = (rq * axis).sum(1, keepdims=True)
            swing = rq - twist * axis
            parts = [r.ravel(), np.sqrt(LAMBDA_SWING) * swing.ravel(), np.sqrt(LAMBDA_TWIST) * twist.ravel()]
            if lengths:
                parts += [np.sqrt(LAMBDA_LEN) * (lq - 1.), np.sqrt(LAMBDA_OFF) * q[48:60]]
            return np.concatenate(parts)
        if len(YA) < 50:
            break
        q0 = np.r_[rv.ravel(), ls - 1., _root_offsets_inv(off)][:nq]
        sol = least_squares(fun, q0, bounds=(lo_q, hi_q), loss='soft_l1', f_scale=.02, max_nfev=25)
        change = np.degrees(np.abs(sol.x[:36] - rv.ravel()).max())
        rv, ls_new, off_new = unpack(sol.x)
        if lengths:
            change = max(change, float(np.abs(ls_new - ls).max()) * 100)
            ls, off = ls_new, off_new
        info['iterations'] = it + 1
        info['rms_m'].append(float(np.sqrt(np.average(d[keep][limb] ** 2, weights=w))) if len(w) else 0.)
        if change < .05:
            break
    info['length_scale'], info['root_offset'] = ls, off
    return rv, info


def _root_offsets(q12):
    """(12,3) per-channel offsets from the 12 free numbers (3 per chain root, ROOTS order)."""
    out = np.zeros((12, 3))
    for k, i in enumerate(ROOTS):
        out[i] = q12[3 * k:3 * k + 3]
    return out


def _root_offsets_inv(off):
    return np.concatenate([off[i] for i in ROOTS])


def _descendants(rig, i):
    out = []
    for j in rig.children[i]:
        out += [j] + _descendants(rig, j)
    return out


def bone_report(rig, rv, info):
    """Per bone: angle, swing / twist split, evidence counts and confidence (high: >= MIN_OWN bare samples of its own, medium: carried by
    its children's evidence, low: only the garment shell proxy or nothing, i.e. at rest unless the proxy moved it)."""
    out = {}
    for i, nm in enumerate(rig.names):
        r = rv[i]
        tw = float(np.dot(r, rig.axis[i]))
        sw = float(np.linalg.norm(r - tw * rig.axis[i]))
        own, desc = info['own'][i], info['desc'][i]
        conf = 'high' if own >= MIN_OWN else ('medium' if desc >= MIN_OWN else 'low')
        out[nm] = {'rotvec_deg': [float(x) for x in np.degrees(r)], 'angle_deg': float(np.degrees(np.linalg.norm(r))), 'swing_deg': float(np.degrees(sw)),
                   'twist_deg': float(np.degrees(tw)), 'bare_samples_own': int(own), 'bare_samples_with_children': int(desc),
                   'proxy_samples': info.get('n_proxy', {}).get(nm, 0), 'confidence': conf,
                   'at_limit': bool(np.any(np.abs(r) >= .98 * rig.limit[i]))}
        if info.get('length_scale') is not None:
            out[nm]['length_scale_pct'] = float((info['length_scale'][i] - 1.) * 100)
            out[nm]['root_offset_mm'] = [float(x) for x in info['root_offset'][i] * 1000]
    return out


# ----------------------------------------------------------------------------------------------- the field

class Field:
    """D(x) = unpose(T1(x)): world point -> aligned world point. A function of the position only (so pieces of one module that share boundary
    vertices stay together). `unpose` takes the skin weights of a point from the POSED base surface: every posed surface sample p_s counts with
    exp(-(|x - p_s|^2 - d0^2) / (2 sigma^2)) (d0 = distance to the nearest sample), i.e. a Gaussian of width `sigma` around the foot point,
    smooth in x even across the medial axis between two limbs, so blend zones are wide and nothing jumps; then the inverse of the blended LBS
    transform is applied."""

    def __init__(self, body, rig, t1p, c, rv, n_samples=FIELD_SAMPLES, seed=0, ls=None, off=None):
        self.body, self.rig, self.t1p, self.c, self.rv = body, rig, np.asarray(t1p, np.float64), np.asarray(c, np.float64), np.asarray(rv, np.float64)
        self.ls = None if ls is None else np.asarray(ls, np.float64)
        self.off = None if off is None else np.asarray(off, np.float64)
        self.identity = not np.any(self.rv) and (self.ls is None or not np.any(self.ls != 1.)) and (self.off is None or not np.any(self.off))
        self.sigma = KERNEL_SIGMA_PCT / 100 * body.H
        if not self.identity:
            self.M = rig.matrices(self.rv, self.ls, self.off)
            rng = np.random.default_rng(seed)
            A, B, C = body.A, body.B, body.C
            w = body.area / body.area.sum()
            f = rng.choice(len(body.F), size=n_samples, p=w)
            u, v = rng.random(n_samples), rng.random(n_samples)
            flip = u + v > 1
            u[flip], v[flip] = 1 - u[flip], 1 - v[flip]
            bary = np.stack([1 - u - v, u, v], 1)
            x = np.einsum('ki,kij->kj', bary, np.stack([A[f], B[f], C[f]], 1))
            Ws = np.einsum('ki,kij->kj', bary, rig.W[body.F[f]])
            self.Ws = Ws
            self.tree = cKDTree(rig.pose(x, Ws, self.M))

    def t1(self, X):
        return t1_apply(self.t1p, X, self.c)

    def wearable(self, mode):
        """The field for the garment: the same stage 2 (bone rotations, lengths, un-posing) but stage 1 with the scale reduced to `mode`:
        'axes' = unchanged, 'uniform' = the geometric mean of the three axis scales on every axis, 'none' = no scale; rotation and translation stay."""
        if mode == 'axes':
            return self
        out = copy.copy(self)
        out.t1p = self.t1p.copy()
        out.t1p[:3] = self.t1p[:3].mean() if mode == 'uniform' else 0.
        return out

    def weights(self, Y, chunk=1500):
        out = np.empty((len(Y), 13))
        for s in range(0, len(Y), chunk):
            d, idx = self.tree.query(Y[s:s + chunk], k=KERNEL_K)
            k = np.exp(-((d ** 2 - d[:, :1] ** 2) / (2 * self.sigma ** 2)))
            out[s:s + chunk] = np.einsum('nk,nkc->nc', k, self.Ws[idx]) / k.sum(1, keepdims=True)
        return out

    def unpose(self, Y):
        Y = np.asarray(Y, np.float64)
        if self.identity or not len(Y):
            return Y.copy()
        w = self.weights(Y)
        A = np.einsum('nc,cij->nij', w, self.M[:, :3, :3])
        t = np.einsum('nc,ci->ni', w, self.M[:, :3, 3])
        return np.linalg.solve(A, (Y - t)[..., None])[..., 0]

    def __call__(self, X):
        return self.unpose(self.t1(X))

    @classmethod
    def from_record(cls, rec, body, rig, wearable=False):
        """The field of a fit/align.json record (stage 1 numbers + per-bone rotations): the body-evidence field, or with `wearable` the one the
        wearable / pieces were aligned with (record stage1.wearable_scale_mode; records without it are v1: per-axis)."""
        s1 = rec['stage1']
        p = np.r_[np.log(s1['scale']), np.radians(s1['rotvec_deg']), np.asarray(s1['translation_mm']) / 1000.]
        bones = rec['stage2']['bones']
        rv = np.radians([bones[n]['rotvec_deg'] for n in rig.names])
        ls = off = None
        if all('length_scale_pct' in bones[n] for n in rig.names):
            ls = 1. + np.array([bones[n]['length_scale_pct'] for n in rig.names]) / 100.
            off = np.array([bones[n]['root_offset_mm'] for n in rig.names]) / 1000.
        f = cls(body, rig, p, np.asarray(s1['center']), rv, ls=ls, off=off)
        return f.wearable(s1.get('wearable_scale_mode', 'axes')) if wearable else f

    def to_json(self):
        return {'t1_params': [float(x) for x in self.t1p], 't1_center': [float(x) for x in self.c], 'rotvec_rad': self.rv.tolist(), 'length_scale': None if self.ls is None else self.ls.tolist(),
                'root_offset_m': None if self.off is None else self.off.tolist(), 'bone_order': list(self.rig.names)}


def reflect(X, plane):
    """Reflection across plane = (unit normal n, offset d): n.x = d. A float `plane` means x = plane (the base's sagittal plane)."""
    X = np.asarray(X, np.float64)
    if np.isscalar(plane):
        Y = X.copy()
        Y[:, 0] = 2 * plane - Y[:, 0]
        return Y
    n, d = plane
    return X - 2 * (X @ n - d)[:, None] * n


def mirror_pair_error(U, left, plane):
    """Distances (m) of the reflected left vertices from the nearest vertex of the right part."""
    return cKDTree(U[~left]).query(reflect(U[left], plane))[0]


def refine_plane(U, left, plane, iters=8):
    """Least-squares reflection plane taking the left vertices onto their (nearest) right counterparts, started at `plane`."""
    n, d = (np.array([1., 0., 0.]), float(plane)) if np.isscalar(plane) else (np.asarray(plane[0], np.float64), float(plane[1]))
    tree = cKDTree(U[~left])
    L, R = U[left], U[~left]
    for _ in range(iters):
        _, j = tree.query(reflect(L, (n, d)))
        diff = R[j] - L
        u, sv, vt = np.linalg.svd(diff - diff.mean(0) * 0, full_matrices=False)
        nn = vt[0] if np.dot(vt[0], n) >= 0 else -vt[0]
        dd = float(np.mean((((L + R[j]) / 2) @ nn)))
        if np.linalg.norm(nn - n) < 1e-12 and abs(dd - d) < 1e-12:
            break
        n, d = nn / np.linalg.norm(nn), dd
    return n, d


def current_mirror_plane(surf, left, colors, fit, body):
    """The plane the existing wearable's left copy was reflected across: the cut wearable's fitted mesh plane (colors npz, mapped review -> world) or
    the base's sagittal plane (faces method); candidates are refined on the vertex pairs and the best kept. Returns (plane, max error m)."""
    cands = [tf.mirror_plane(body.V, body.o)]
    if colors is not None and 'mirror_plane_normal' in colors.files:
        n = np.asarray(colors['mirror_plane_normal'], np.float64)
        t = np.asarray(fit['translation'], np.float64)
        cands.append((n, float((float(colors['mirror_plane_offset']) - n @ t) * (body.H / 2) / fit['scale'] + n @ body.o)))
    best = None
    for c in cands:
        for plane in (c, refine_plane(surf.U, left, c)):
            err = mirror_pair_error(surf.U, left, plane)
            score = (float(err.max()), plane)
            if best is None or score[0] < best[0]:
                best = score
    return best[1], best[0]


def align_vertices(field, U, left=None, plane_cur=0., plane_base=0.):
    """Aligned positions of welded vertices U. `left` (bool mask) = the vertices of a mirrored copy: re-created as the exact reflection of the
    aligned right counterpart across the base's plane (the right counterpart sits at U reflected across the plane the copy was made with)."""
    out = field(U)
    if left is not None and left.any():
        out[left] = reflect(field(reflect(U[left], plane_cur)), plane_base)
    return out


# ----------------------------------------------------------------------------------------------- metrics

def stage_points(P, field, stage):
    if stage == 'current':
        return P
    if stage == 'stage1':
        return field.t1(P)
    return field(P)


def residual_table(body, ev, field, part_names):
    """Mean / p95 of |distance to the game base| of the area-proportional bare samples, overall, held-out, per simplified part."""
    out = {}
    for stage in ('current', 'stage1', 'stage12'):
        P = stage_points(ev['eval']['pts'], field, stage)
        d = body.closest(P)[0] * 1000
        hold, part = ev['eval']['hold'], ev['eval']['part']
        parts = {}
        for pi in np.unique(part):
            m = part == pi
            if m.sum() >= 40:
                parts[SIMPLE.get(part_names[pi], part_names[pi])] = {'n': int(m.sum()), 'mean_mm': float(d[m].mean()), 'p95_mm': float(np.percentile(d[m], 95))}
        simple = np.array([SIMPLE.get(part_names[i], part_names[i]) for i in part])
        core, limb = np.isin(simple, ('head', 'neck', 'torso', 'pelvis')), np.isin(simple, [f'{a}{s}' for a in ('upperarm', 'forearm', 'hand', 'thigh', 'shin', 'foot') for s in ('.l', '.r')])
        out[stage] = {'core_mean_mm': float(d[core].mean()) if core.any() else None, 'limb_mean_mm': float(d[limb].mean()) if limb.any() else None, 'n_core': int(core.sum()), 'n_limb': int(limb.sum()),
                      'mean_mm': float(d.mean()), 'p95_mm': float(np.percentile(d, 95)), 'rms_mm': float(np.sqrt((d ** 2).mean())),
                      'heldout_mean_mm': float(d[hold].mean()) if hold.any() else None, 'heldout_p95_mm': float(np.percentile(d[hold], 95)) if hold.any() else None,
                      'fit_mean_mm': float(d[~hold].mean()) if (~hold).any() else None, 'n': int(len(d)), 'n_heldout': int(hold.sum()), 'parts': parts}
    return out


def anchor_table(body, ev, field, part_names):
    """White-anchor landmark errors per stage: offset of the anchor samples' centroid from the centroid of their closest game-base points
    (lat_out = outward positive, fwd, up; mm) and the mean distance."""
    out = {}
    pts = np.concatenate([ev['eval']['pts'], ev['fit']['pts']])
    part = np.concatenate([ev['eval']['part'], ev['fit']['part']])
    white = np.concatenate([ev['eval']['white'], ev['fit']['white']])
    for name, pn in ANCHORS:
        if pn not in part_names:
            continue
        m = white & (part == part_names.index(pn))
        if m.sum() < 30:
            out[name] = None
            continue
        side = -1. if name.endswith('.r') else 1.
        rec = {'samples': int(m.sum())}
        for stage in ('current', 'stage1', 'stage12'):
            P = stage_points(pts[m], field, stage)
            d, _, cp = closest_points(body, P)
            off = P.mean(0) - cp.mean(0)
            rec[stage] = {'lat_out_mm': mm(off[0] * side), 'fwd_mm': mm(-off[1]), 'up_mm': mm(off[2]), 'norm_mm': mm(np.linalg.norm(off)), 'mean_abs_d_mm': mm(d.mean())}
        out[name] = rec
    return out


def edge_strain(surf, U0, U1):
    e = np.unique(surf.edges(), axis=0)
    l0 = np.linalg.norm(U0[e[:, 0]] - U0[e[:, 1]], axis=1)
    l1 = np.linalg.norm(U1[e[:, 0]] - U1[e[:, 1]], axis=1)
    ok = l0 > 1e-6
    s = np.abs(l1[ok] / l0[ok] - 1) * 100
    return {'edges': int(ok.sum()), 'p50_pct': float(np.percentile(s, 50)), 'p99_pct': float(np.percentile(s, 99)), 'max_pct': float(s.max())}


# ----------------------------------------------------------------------------------------------- module

def aligned_surface(surf, field, left, plane_cur, plane_base, out_path, info):
    """Aligned copy of one wearable / piece surface. Returns (U_new, U_stage1, info)."""
    U1 = field.t1(surf.U)
    U_new = align_vertices(field, surf.U, left, plane_cur, plane_base)
    info['strain'] = edge_strain(surf, surf.U, U_new)
    info['strain_stage2_only'] = edge_strain(surf, U1, U_new)
    bf.fit_write(surf, U_new, info, out_path)
    return U_new, U1, info


def strain_flag(info):
    s = info.get('strain_stage2_only') or {}
    if s.get('p99_pct', 0) > STRAIN_WARN_P99 or s.get('max_pct', 0) > STRAIN_WARN_MAX:
        return f'edge strain of the un-posing p99 {s["p99_pct"]:.1f} % max {s["max_pct"]:.1f} %'
    return None


def left_vertices(surf, flags):
    """Welded vertices that only belong to mirrored (left) faces."""
    if flags is None or len(flags) != len(surf.T) or not np.any(flags):
        return None
    m = np.asarray(flags).astype(bool)
    on_left = np.zeros(surf.K, bool)
    on_right = np.zeros(surf.K, bool)
    on_left[surf.Tw[m].ravel()] = True
    on_right[surf.Tw[~m].ravel()] = True
    return on_left & ~on_right


def mirror_error(surf, left, plane):
    """Max / mean distance (mm) of the reflected left vertices from the nearest right vertex of the same surface (0 for an exact pair)."""
    if left is None or not left.any() or left.all():
        return None
    d = mirror_pair_error(surf.U, left, plane)
    return {'max_mm': mm(d.max()), 'mean_mm': mm(d.mean()), 'left_vertices': int(left.sum())}


def solve_field(body, rig, ev, variant='v2'):
    """The deformation field of a module from its evidence: (field, stage-1 dict, stage-2 info, rotation vectors)."""
    tr = ~ev['fit']['hold']
    if variant == 'core':
        s1 = core_stage1(body, ev)
    else:
        s1 = fit_stage1(body, ev['fit'])
    p = s1['p']
    ptmp = Field(body, rig, p, s1['c'], np.zeros((12, 3)))
    Yb, Yg = ptmp.t1(ev['fit']['pts']), ptmp.t1(ev['garment']['pts'])
    rv, s2 = fit_stage2(body, rig, Yb[tr], ev['fit']['w'][tr], Yg, lengths=(variant == 'core'))
    if variant == 'core':
        field = Field(body, rig, p, s1['c'], rv, ls=s2['length_scale'], off=s2['root_offset'])
    else:
        field = Field(body, rig, p, s1['c'], rv)
    return field, s1, s2, rv


def process_module(module_dir, levels=LEVELS, force=False, measure_only=False, variant='v2'):
    module_dir = Path(module_dir).resolve()
    name = module_dir.relative_to(ROOT / 'models').as_posix() if module_dir.is_relative_to(ROOT / 'models') else str(module_dir)
    res = {'module': name, 'status': 'done'}
    mpath = module_dir / 'reduced' / 'manifest.json'
    if not mpath.is_file():
        return {**res, 'status': 'skipped', 'why': 'no reduced/manifest.json'}
    if module_busy(module_dir):
        return {**res, 'status': 'skipped', 'why': 'a conversion worker holds this module'}
    manifest = json.loads(mpath.read_text(encoding='utf-8-sig'))
    if manifest.get('module') not in MODULES or not ((manifest.get('parts') or {}).get('fit') or {}).get('scale'):
        return {**res, 'status': 'skipped', 'why': 'not a converted module with a base fit'}
    _, _, race, module = identity(module_dir)
    rigf = bf.BASE_CACHE / f'{race}_rig.npz'
    if not (bf.BASE_CACHE / f'{race}.npz').is_file() or not rigf.is_file():
        return {**res, 'status': 'skipped', 'why': f'no cached base / rig for {race} (run export_bases.py)'}
    lvs = bf.levels_present(module_dir, levels)
    if not lvs:
        return {**res, 'status': 'skipped', 'why': 'no wearable GLB'}
    out_json = module_dir / 'fit' / 'align.json'
    if not force and not measure_only and bf.fresh(out_json, mpath, rigf, bf.BASE_CACHE / f'{race}.npz', *[module_dir / 'reduced' / f'lod_error_{lv}__wearable.glb' for lv in lvs]):
        try:
            if json.loads(out_json.read_text(encoding='utf-8-sig')).get('code_version') == CODE_VERSIONS[variant]:
                return {**res, 'status': 'fresh', 'record': json.loads(out_json.read_text(encoding='utf-8-sig'))}
        except (OSError, ValueError):
            pass
    t0 = time.monotonic()
    body = bf.get_body(race)
    rig = Rig.from_npz(rigf)
    rng = np.random.default_rng(1)
    rec = {'module': name, 'race': race, 'code_version': CODE_VERSIONS[variant], 'variant': variant, 'warnings': []}
    ev = load_evidence(module_dir, manifest, body, rng)
    rec['evidence'] = {k: ev[k] for k in ('source', 'faces', 'bare_faces', 'usable_faces', 'bare_area_m2', 'usable_area_m2', 'rings', 'garment_faces', 'garment_area_m2', 'groups')}
    field, s1, s2, rv = solve_field(body, rig, ev, variant)
    p = s1['p']
    at_bound = [a for a, v in zip(('sx', 'sy', 'sz'), np.exp(p[:3])) if v <= (CORE_SCALE_BOUNDS if variant == 'core' else SCALE_BOUNDS)[0] + 1e-3 or v >= (CORE_SCALE_BOUNDS if variant == 'core' else SCALE_BOUNDS)[1] - 1e-3]
    rec['variant'] = variant
    wear_mode = WEAR_SCALE if variant == 'v2' else 'axes'
    rec['stage1'] = {'center': s1['c'].tolist(), 'scale': np.exp(p[:3]).tolist(), 'rotvec_deg': np.degrees(p[3:6]).tolist(), 'angle_deg': float(np.degrees(np.linalg.norm(p[3:6]))),
                     'translation_mm': (p[6:9] * 1000).tolist(), 'at_bound': at_bound, 'iterations': s1['iterations'], 'rms_m': s1['rms_m'],
                     'wearable_scale_mode': wear_mode, 'wearable_scale': np.exp(field.wearable(wear_mode).t1p[:3]).tolist()}
    for k in ('mode', 'core_samples', 'torso_area_m2'):
        if k in s1:
            rec['stage1'][k] = s1[k]
    if variant == 'core' and s1['mode'] != 'strong':
        rec['warnings'].append(f'weak core evidence ({s1["mode"]}): stage 1 is a translation, scale / rotation pinned (torso bare area {s1["torso_area_m2"]:.3f} m2)')
    if at_bound and variant != 'core':
        rec['warnings'].append(f'stage 1 scale at its bound: {at_bound}')
    rec['stage2'] = {'bones': bone_report(rig, rv, s2), 'iterations': s2['iterations'], 'rms_m': s2['rms_m'], 'prior': {'swing_per_rad2': LAMBDA_SWING, 'twist_per_rad2': LAMBDA_TWIST,
                     **({'length_per_unit2': LAMBDA_LEN, 'root_offset_per_m2': LAMBDA_OFF, 'length_max': LEN_MAX, 'root_offset_max_m': OFF_MAX} if variant == 'core' else {})},
                     'kernel_sigma_mm': mm(field.sigma)}
    rec['residuals'] = residual_table(body, ev, field, ev['part_names'])
    rec['anchors'] = anchor_table(body, ev, field, ev['part_names'])
    low = [n for n, b in rec['stage2']['bones'].items() if b['confidence'] == 'low']
    rec['low_confidence_bones'] = low
    plane_x = tf.mirror_plane(body.V, body.o)
    rec['mirror_plane_x'] = plane_x
    rec['levels'] = {}
    if not measure_only:
        for lv in lvs:
            rec['levels'][lv] = process_level(module_dir, manifest, body, field.wearable(wear_mode), lv, plane_x)
            for flag in [rec['levels'][lv].get('strain_flag')] + (rec['levels'][lv].get('pieces') or {}).get('strain_flags', []):
                if flag and f'{lv}: {flag}' not in rec['warnings']:
                    rec['warnings'].append(f'{lv}: {flag}')
    rec['seconds'] = round(time.monotonic() - t0, 1)
    if not measure_only:
        (module_dir / 'fit').mkdir(exist_ok=True)
        bf.write_json(out_json, rec)
        bodies = write_bodies(module_dir, manifest, body, field.wearable(wear_mode), levels)        # after the json: the preview hides an overlay older than what it was made from
        bf.write_json(module_dir / 'fit' / 'tripo_body.json', {'module': name, 'code_version': 'tripo-body-20261025', 'aligned': True, 'levels': bodies})
    res.update({'record': rec, 'seconds': rec['seconds']})
    return res


def process_level(module_dir, manifest, body, field, lv, plane_x):
    reduced = module_dir / 'reduced'
    src = reduced / f'lod_error_{lv}__wearable.glb'
    out = reduced / f'lod_error_{lv}__wearable{SUFFIX}.glb'
    rec = {}
    surf = bf.Surface(bf.Glb(src), manifest, body)
    flags = None
    cz = reduced / f'lod_error_{lv}_colors.npz'
    if cz.is_file():
        z = np.load(cz, allow_pickle=True)
        if 'wearable_mirrored_glb_order' in z.files:
            flags = z['wearable_mirrored_glb_order']
    left = left_vertices(surf, flags)
    plane_cur = plane_x
    info = {}
    if left is not None:
        plane_cur, err = current_mirror_plane(surf, left, np.load(cz, allow_pickle=True), manifest['parts']['fit'], body)
        info['mirror_plane_current'] = {'normal': [float(x) for x in plane_cur[0]], 'offset': float(plane_cur[1]), 'max_pair_error_mm': mm(err)} if not np.isscalar(plane_cur) \
            else {'x': float(plane_cur), 'max_pair_error_mm': mm(err)}
    aligned_surface(surf, field, left, plane_cur, plane_x, out, info)
    asurf = bf.Surface(bf.Glb(out), manifest, body)
    info['file'] = rel(out)
    info['mirrored_vertices'] = int(left.sum()) if left is not None else 0
    me = mirror_error(asurf, left, plane_x)
    if me:
        info['mirror_error_aligned'] = me
        info['mirror_error_current'] = mirror_error(surf, left, plane_cur)
    rec['wearable'] = info
    rec['strain_flag'] = strain_flag(info)
    pdir = bf.pieces_dir(module_dir, lv)
    if pdir is not None:
        rec['pieces'] = process_pieces(manifest, body, field, pdir, lv, plane_x)
    return rec


def process_pieces(manifest, body, field, pdir, lv, plane_x):
    src_dir, out_dir = pdir / f'lod_{lv}', pdir / f'lod_{lv}{SUFFIX}'
    paths = {p.stem: p for p in sorted(src_dir.glob('*.glb'))}
    surfs = {n: bf.Surface(bf.Glb(p), manifest, body) for n, p in paths.items()}
    table, outs = {}, {}
    for n, s in surfs.items():
        outs[n] = out_dir / paths[n].name
        info = {'source': rel(paths[n]), 'file': rel(outs[n])}
        aligned_surface(s, field, None, plane_x, plane_x, outs[n], info)
        table[n] = info
    fits = {n: bf.Surface(bf.Glb(outs[n]), manifest, body) for n in outs}
    gaps = bf.gap_metrics(surfs, fits)
    flags = [f'{n}: {x}' for n, i in table.items() for x in [strain_flag(i)] if x]
    return {'dir': rel(src_dir), 'aligned_dir': rel(out_dir), 'table': table, 'gaps': gaps, 'strain_flags': flags,
            'gap_note': 'max distance (mm) between vertices of different pieces that coincided before the alignment (0 = seams stay closed)'}


# ----------------------------------------------------------------------------------------------- red-body overlay

def base_soup(glb, colors=None):
    """The base-role faces of a labelled LOD GLB: (triangles (n,3,3) in glTF scene space, anchor mask (n,), note) or None without any.
    The labelled GLB has one primitive per role and the base role is the material named `base` (white anchors are base too), so the faces
    come from the GLB itself and survive a GLB / colours-npz face-count mismatch; the anchor flags (face_mean_source_rgb white) are only
    taken when the npz order matches the GLB exactly (same face count, every base face has material_id 0 there), else all faces stay red."""
    names = [m.get('name') for m in glb.js.get('materials', [])]
    tris, ids, offset = [], [], 0
    for k, prim in enumerate(glb.prims):
        t = glb.triangles(k)
        mat = prim['def'].get('material')
        if mat is not None and mat < len(names) and names[mat] == 'base' and len(t):
            m = prim['matrix']
            tris.append((glb.positions(k) @ m[:3, :3].T + m[:3, 3])[t])
            ids.append(np.arange(offset, offset + len(t)))
        offset += len(t)
    if not tris:
        return None
    P, ids = np.concatenate(tris), np.concatenate(ids)
    anchor, note = np.zeros(len(P), bool), 'all red'
    if colors is not None and all(k in colors.files for k in ('glb_face_order', 'material_id', 'face_mean_source_rgb')):
        order = colors['glb_face_order']
        if len(order) == offset == len(colors['material_id']) == len(colors['face_mean_source_rgb']) and (colors['material_id'][order][ids] == 0).all():
            anchor, note = is_white(colors['face_mean_source_rgb'][order][ids]), 'white anchors from the colours npz'
        else:
            note = 'all red (colours npz does not match the GLB face order)'
    return P, anchor, note


def soup_glb_bytes(groups):
    """A GLB of flat-shaded triangle soups: groups = [(name, rgb, triangles (n,3,3))]; one primitive + material per non-empty group."""
    import struct
    blob, views, accessors, prims, materials = bytearray(), [], [], [], []

    def add(arr, minmax=False):
        off = len(blob)
        blob.extend(arr.tobytes())
        blob.extend(b'\0' * (-len(blob) % 4))
        views.append({'buffer': 0, 'byteOffset': off, 'byteLength': arr.nbytes, 'target': 34962})
        acc = {'bufferView': len(views) - 1, 'componentType': 5126, 'count': len(arr), 'type': 'VEC3'}
        if minmax:
            acc['min'], acc['max'] = [float(x) for x in arr.min(0)], [float(x) for x in arr.max(0)]
        accessors.append(acc)
        return len(accessors) - 1
    for name, rgb, T in groups:
        if not len(T):
            continue
        cr = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
        ln = np.linalg.norm(cr, axis=1, keepdims=True)
        nrm = np.where(ln > 1e-30, cr / np.maximum(ln, 1e-30), 0.)
        prims.append({'attributes': {'POSITION': add(T.reshape(-1, 3).astype(np.float32), True), 'NORMAL': add(np.repeat(nrm, 3, axis=0).astype(np.float32))},
                      'material': len(materials)})
        materials.append({'name': name, 'doubleSided': True, 'pbrMetallicRoughness': {'baseColorFactor': [*[float(c) for c in rgb], 1.], 'metallicFactor': 0., 'roughnessFactor': .9}})
    js = {'asset': {'version': '2.0', 'generator': 'align_body.py'}, 'scene': 0, 'scenes': [{'nodes': [0]}], 'nodes': [{'mesh': 0, 'name': 'tripo_body'}],
          'meshes': [{'name': 'tripo_body', 'primitives': prims}], 'materials': materials, 'accessors': accessors, 'bufferViews': views, 'buffers': [{'byteLength': len(blob)}]}
    text = json.dumps(js, separators=(',', ':')).encode()
    text += b' ' * (-len(text) % 4)
    body = struct.pack('<II', len(text), 0x4E4F534A) + text + struct.pack('<II', len(blob), 0x004E4942) + bytes(blob)
    return struct.pack('<4sII', b'glTF', 2, 12 + len(body)) + body


def write_body_glb(path, T, anchor):
    bf.write_atomic(path, soup_glb_bytes([('tripo_body', BODY_RGB, T[~anchor]), ('tripo_body_anchor', ANCHOR_RGB, T[anchor])]))


def body_to_aligned(T, field, manifest, body):
    """Triangles (n,3,3) in the native glTF frame -> the same triangles with `field` (a world-frame map) applied, back in the native glTF frame
    (the chain of Surface.to_world / to_local with the ORIGINAL manifest and parts.fit, so native_to_world places them like the wearable)."""
    fit = manifest['parts']['fit']
    uniq, inv = np.unique(T.reshape(-1, 3), axis=0, return_inverse=True)
    world = tf.native_to_world(bf.gl_to_bl(uniq), manifest, fit, body.H, body.o)
    new = bf.bl_to_gl(tf.world_to_native(field(world), manifest, fit, body.H, body.o))
    return new[inv.ravel()].reshape(T.shape)


def write_bodies(module_dir, manifest, body, field, levels=LEVELS):
    """Write the red-body overlays of every level that has a labelled GLB: `lod_error_<lv>__tripo_body.glb` and, with `field` (the WEARABLE field
    of the module, `Field.wearable(mode)`), `..._aligned.glb`. Returns the per-level record (also written to fit/tripo_body.json by the callers)."""
    reduced = Path(module_dir) / 'reduced'
    out = {}
    for lv in levels:
        src, cz = reduced / f'lod_error_{lv}.glb', reduced / f'lod_error_{lv}_colors.npz'
        if not src.is_file():
            continue
        try:
            try:
                colors = np.load(cz, allow_pickle=True) if cz.is_file() else None
            except Exception:  # noqa: BLE001 - unreadable colours: no anchors, every face red
                colors = None
            soup = base_soup(bf.Glb(src), colors)
            if soup is None:
                out[lv] = {'status': 'skipped', 'why': 'no base-role primitive in the labelled GLB'}
                continue
            T, anchor, note = soup
            rec = {'status': 'done', 'faces': int(len(T)), 'anchor_faces': int(anchor.sum()), 'colours': note}
            write_body_glb(reduced / f'lod_error_{lv}{BODY_SUFFIX}.glb', T, anchor)
            rec['file'] = rel(reduced / f'lod_error_{lv}{BODY_SUFFIX}.glb')
            if field is not None:
                Ta = body_to_aligned(T, field, manifest, body)
                write_body_glb(reduced / f'lod_error_{lv}{BODY_SUFFIX}{SUFFIX}.glb', Ta, anchor)
                rec['aligned_file'] = rel(reduced / f'lod_error_{lv}{BODY_SUFFIX}{SUFFIX}.glb')
                rec['moved_mm'] = {'mean': mm(np.linalg.norm(Ta - T, axis=2).mean()), 'max': mm(np.linalg.norm(Ta - T, axis=2).max())}
            out[lv] = rec
        except Exception as error:  # noqa: BLE001 - a broken level must not stop the others
            out[lv] = {'status': 'failed', 'why': f'{type(error).__name__}: {error}'}
    return out


def process_bodies(module_dir, levels=LEVELS, force=False):
    """`--bodies-only`: the red-body overlays of a module from its existing fit/align.json (no refit). The un-aligned overlay needs only the
    labelled LOD; the aligned one the record (fit/align.json) and the cached base + rig."""
    module_dir = Path(module_dir).resolve()
    name = module_dir.relative_to(ROOT / 'models').as_posix() if module_dir.is_relative_to(ROOT / 'models') else str(module_dir)
    res = {'module': name, 'status': 'done'}
    mpath, apath = module_dir / 'reduced' / 'manifest.json', module_dir / 'fit' / 'align.json'
    if not mpath.is_file():
        return {**res, 'status': 'skipped', 'why': 'no reduced/manifest.json'}
    if module_busy(module_dir):
        return {**res, 'status': 'skipped', 'why': 'a conversion worker holds this module'}
    manifest = json.loads(mpath.read_text(encoding='utf-8-sig'))
    if manifest.get('module') not in MODULES or not ((manifest.get('parts') or {}).get('fit') or {}).get('scale'):
        return {**res, 'status': 'skipped', 'why': 'not a converted module with a base fit'}
    _, _, race, module = identity(module_dir)
    rigf = bf.BASE_CACHE / f'{race}_rig.npz'
    if not (bf.BASE_CACHE / f'{race}.npz').is_file():
        return {**res, 'status': 'skipped', 'why': f'no cached base for {race} (run export_bases.py)'}
    lvs = [lv for lv in levels if (module_dir / 'reduced' / f'lod_error_{lv}.glb').is_file()]
    if not lvs:
        return {**res, 'status': 'skipped', 'why': 'no labelled LOD GLB'}
    outs = [module_dir / 'reduced' / f'lod_error_{lv}{BODY_SUFFIX}{x}.glb' for lv in lvs for x in ('', SUFFIX if apath.is_file() else '')]
    inputs = [mpath, *[module_dir / 'reduced' / f'lod_error_{lv}.glb' for lv in lvs], *([apath, rigf] if apath.is_file() else [])]
    if not force and all(bf.fresh(o, *inputs) for o in outs):
        return {**res, 'status': 'fresh'}
    t0 = time.monotonic()
    body = bf.get_body(race)
    field = None
    if apath.is_file() and rigf.is_file():
        field = Field.from_record(json.loads(apath.read_text(encoding='utf-8-sig')), body, Rig.from_npz(rigf), wearable=True)
    rec = write_bodies(module_dir, manifest, body, field, lvs)
    doc = {'module': name, 'code_version': 'tripo-body-20261025', 'aligned': field is not None, 'levels': rec,
           'transform': 'aligned overlay = Field.from_record(fit/align.json, wearable=True): stage 1 without the per-axis scale + stage-2 un-posing, as the wearable got' if field else None}
    (module_dir / 'fit').mkdir(exist_ok=True)
    bf.write_json(module_dir / 'fit' / 'tripo_body.json', doc)
    res.update({'record': {'levels': rec}, 'seconds': round(time.monotonic() - t0, 1)})
    if any(r.get('status') == 'failed' for r in rec.values()):
        res['why'] = '; '.join(f'{lv}: {r["why"]}' for lv, r in rec.items() if r.get('status') == 'failed')
    return res


# ----------------------------------------------------------------------------------------------- CLI

def _one(work):
    m, levels, force, measure_only, variant, bodies_only = work
    try:
        r = process_bodies(m, levels, force) if bodies_only else process_module(m, levels, force, measure_only, variant)
    except Exception as error:  # noqa: BLE001
        import traceback
        r = {'module': str(m), 'status': 'failed', 'why': f'{type(error).__name__}: {error}', 'trace': traceback.format_exc()}
    rec = r.get('record') or {}
    res = rec.get('residuals') or {}
    line = ''
    if res:
        line = f"current {res['current']['mean_mm']:.1f} -> stage1 {res['stage1']['mean_mm']:.1f} -> stage1+2 {res['stage12']['mean_mm']:.1f} mm (held-out {res['stage12']['heldout_mean_mm']:.1f})"
    if bodies_only:
        lv = (rec.get('levels') or {})
        line = ' '.join(f'{k}:{v.get("faces", v.get("status"))}' for k, v in lv.items())
    print(f'{"ALIGN_BODIES" if bodies_only else "ALIGN_BODY"} {r["status"]:8} {r["module"]} {r.get("seconds", "")} {line} {r.get("why") or ""}', flush=True)
    if r['status'] == 'failed':
        print(r.get('trace', ''), flush=True)
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('modules', nargs='*')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--match', action='append', default=[], help='keep modules whose path contains this (repeatable: any of)')
    ap.add_argument('--levels', default=','.join(LEVELS))
    ap.add_argument('--jobs', type=int, default=1)
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--variant', choices=VARIANTS, default='v2', help='v2: global fit of all bare skin, the wearable without per-axis scale (default); v1: wearable with the per-axis scale too; core: core-first stage 1 + bone lengths / root offsets (experimental)')
    ap.add_argument('--measure-only', action='store_true', help='fit and report, write no GLB / align.json')
    ap.add_argument('--bodies-only', action='store_true', help="write only the preview's red-body overlays (reduced/lod_error_<lv>__tripo_body[_aligned].glb, fit/tripo_body.json) from the existing fit/align.json, no refit")
    ap.add_argument('--summary')
    a = ap.parse_args(argv)
    mods = [Path(m) for m in a.modules]
    if a.all:
        mods += discover()
    mods = [m if m.is_absolute() else ROOT / m for m in mods]
    if a.match:
        mods = [m for m in mods if any(s in str(m) for s in a.match)]
    if not mods:
        raise SystemExit('no modules (give module folders or --all)')
    levels = tuple(a.levels.split(','))
    work = [(m, levels, a.force, a.measure_only, a.variant, a.bodies_only) for m in mods]
    results = {}
    if a.jobs > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=a.jobs) as pool:
            for r in pool.map(_one, work):
                results[r['module']] = r
    else:
        for w in work:
            r = _one(w)
            results[r['module']] = r
    counts = {s: sum(r['status'] == s for r in results.values()) for s in sorted({r['status'] for r in results.values()})}
    if a.summary:
        bf.write_json(a.summary, {'date': datetime.datetime.now().isoformat(timespec='seconds'), 'modules': {k: {'status': r['status'], 'why': r.get('why'), 'seconds': r.get('seconds')} for k, r in results.items()}})
    print(f'ALIGN_BODY_SUMMARY {len(results)} modules: {json.dumps(counts)}')
    if counts.get('failed'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
