"""Measure and fix armour / garment wearables that intersect the game base body (2026-10-24).

    python3 tools/tripo_pipeline/body_fit.py <module_dir> [<module_dir> ...] [--all] [--match S] [--levels 0p4,0p2,0p1]
        [--method full|vertex] [--source original|aligned] [--margin-pct 0.4] [--measure-only] [--jobs N] [--force] [--summary FILE] [--race-check] [--inside-test pseudonormal|winding]

<module_dir> = models/<set>/<job-id>/<race>/<module>. `--all` takes every converted module under models/ (same discovery and
module-lock rule as rewear.py: a live conversion worker's lock skips the module). Nothing is generated or submitted; only
reduced/manifest.json, the level wearables (`reduced/lod_error_<lv>__wearable.glb`), the rigid piece GLBs
(`segment_first/pieces/lod_<lv>/*.glb` or `pieces/lod_<lv>/*.glb`) and the game base body
`tools/preview/cache/bases/<race>.npz` (+ .json; made by tools/preview/export_bases.py in Blender) are read.

Each wearable / piece is put into game world space with outfit_studio/core/transform.native_to_world (the chain Outfit Studio and
the preview's "on body" view use), the signed distance of every vertex to the body is measured (exact point-to-triangle distance;
negative = inside), and every triangle gets d_mm = the smallest of its three vertices. Margin = --margin-pct % of the body
height (default 0.4 %). The inside test (`--inside-test`) is by default the angle-weighted pseudonormal sign (Baerentzen & Aanaes
2005) of the closest face / edge / vertex, which costs nothing next to the closest-point search; the generalized winding number
(every point against every body triangle, the reference) decides only where that sign is unreliable: closest feature on a boundary,
non-manifold or pinched edge / vertex, a triangle that crosses another part or is buried in it (the game bases of several races
have overlapping limbs), or a point within 1 micron of the surface. `--inside-test winding` runs the reference everywhere; the
results are the same.

Methods (--method): `full` (default) also removes the game body poking OUT THROUGH armour faces (a reverse test: body surface samples
whose normal line finds no armour outside but armour behind, see "body through armour" in the README; poking triangles are lifted by
depth + margin, rigid scales must clear both tests) and writes the `_fitted` names; `vertex` is the first version (vertices only) and
writes `..._fitted_vertex.glb` / `lod_<lv>_fitted_vertex/` and fit/body_fit_vertex.json. Both write clearance sidecars whose d_mm has
the poking triangles folded in (min(vertex d, -poke depth)) and a `poke` block in metrics.

Fit A, the wearable: a smooth displacement field over the welded vertex graph pushes vertices closer than the margin out along the
body normal (iterated, re-measured, at most 6 rounds); only POSITION (and NORMAL) accessors of the GLB change, so primitives, face
order, materials and node names stay valid for the face-order sidecar arrays. Output `reduced/lod_error_<lv>__wearable_fitted.glb`.
Fit B, rigid pieces: the smallest affine scale s in [1, 1.35] perpendicular to the principal axis of the body parts a piece covers
(about their centroid) that clears the margin; flat panels stay flat; otherwise Fit A on that piece (method `push`, needs_review).
Output `lod_<lv>_fitted/<piece>.glb` beside `lod_<lv>/`.

Outputs: `<stem>.clearance.json` next to every measured GLB (d_mm per triangle in three.js traversal order), `<module>/fit/body_fit.json`
and the summary (default experiments/body_fit_<date>/summary.json + summary.md). `--measure-only` writes only the originals' clearance
sidecars. A fitted GLB is redone when its source GLB, the manifest or the body cache is newer (or with --force).
`--source aligned` fits the ALIGNED wearable / pieces that align_body.py wrote (`..__wearable_aligned.glb`, `lod_<lv>_aligned/`) and writes
separately named outputs (`..__wearable_aligned_fitted.glb`, `lod_<lv>_aligned_fitted/`, `fit/body_fit_aligned.json`); the existing fitted
files are never touched and the record carries a `compare_to_current` block (poke area, displacement vs the current fitted outputs).
`--race-check` compares every race's concept base (base_views/<race>/parts.npz) with the game base.
Needs numpy + scipy (re-runs itself with characters/.venv when scipy is missing).
"""
import os

for _k in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(_k, '1')
import argparse
import datetime
import importlib.util
import json
import struct
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
    if __name__ == '__main__' and not os.environ.get('BODY_FIT_REEXEC') and _venv.is_file():
        os.execve(str(_venv), [str(_venv), *sys.argv], dict(os.environ, BODY_FIT_REEXEC='1'))
    raise SystemExit('body_fit.py needs numpy + scipy (characters/.venv/bin/python)')
from scipy import sparse
from scipy.sparse import csgraph
from scipy.spatial import cKDTree

from rewear import MODULES, discover, identity, module_busy   # noqa: E402

LEVELS = ('0p4', '0p2', '0p1')
BASE_CACHE = ROOT / 'tools' / 'preview' / 'cache' / 'bases'
TOL = 5e-4                    # metres: a fit is done when every vertex is within 0.5 mm of the margin or better
MAX_ROUNDS = 12               # smooth rounds; 6 left a third of the wearables short (vertices between two body parts ping-pong)
CLEANUP_ITERS = 40            # hard per-vertex projections after the smooth rounds, only for vertices still inside the margin
S_MAX = 1.35
KAPPA = .03                   # screened-Laplacian damping of the displacement field (decay per hop ~ 0.8 at .03)
SMOOTH_ITERS = 80
FLAT_DEG = 5.                 # an edge counts as flat when its dihedral angle was below this
PART_SHARE = .15              # a body part counts as covered by a piece when this share of its vertices has it as closest
LARGE_PUSH_MM = 60.
CELL = .03                    # ray grid cell (m)
# RayGrid.hits, Body.closest and Body.winding on the GPU (gpu_geometry.py, OpenGL compute via moderngl) when BODY_FIT_GPU=1; off by default (the CPU path is unchanged). Work
# below GPU_MIN_TESTS rays x triangles stays on the CPU (the upload costs more); a GPU error switches the process back to the CPU.
GPU_MIN_TESTS = 2e5
GPU_WINDING = False           # the GPU winding number (float32) flips inside/outside for ~0.3 % of points lying on the skin even with
                              # the float64 fallback for points it flags, and saves only ~1.3 s per fitting job: kept on the CPU
_GPU_ENV = os.environ.get('BODY_FIT_GPU', '').strip().lower()      # '1' / 'all', or a comma list of: rays, closest, raster, winding
_GPU_PARTS = set() if _GPU_ENV in ('', '0') else ({'rays', 'closest', 'raster'} if _GPU_ENV in ('1', 'all', 'true', 'yes') else set(_GPU_ENV.split(',')))
_GPU_ON = bool(_GPU_PARTS)
_gpu_mod = None


def _gpu():
    global _gpu_mod
    if _gpu_mod is None:                                     # one shared copy per process (visibility.py uses the same GPU context)
        _gpu_mod = sys.modules.get('gpu_geometry')
        if _gpu_mod is None:
            spec = importlib.util.spec_from_file_location('gpu_geometry', HERE / 'gpu_geometry.py')
            _gpu_mod = importlib.util.module_from_spec(spec)
            sys.modules['gpu_geometry'] = _gpu_mod
            spec.loader.exec_module(_gpu_mod)
    return _gpu_mod


def _gpu_failed(e):
    global _GPU_ON
    _GPU_ON = False
    print(f'body_fit: GPU ray test failed ({type(e).__name__}: {e}); CPU from now on in this process', file=sys.stderr, flush=True)


POKE_OUT = .25               # body sample counts as covered when armour is hit this far along its outward normal
POKE_NEAR_OUT = .04           # first stage of the outward (cover) search; the rest of POKE_OUT only for samples still uncovered
POKE_IN = .12                 # ... and as poking when armour is hit this far behind its surface (and the hit point is inside the body)
POKE_SPACING_PCT = .5         # sample spacing on the body surface, % of the body height
POKE_DEPTH_TOL = 1e-3         # a surface is clear when no poking sample is deeper than this ...
POKE_AREA_TOL = 5e-5          # ... or the poking area is below this (0.5 cm^2)
POKE_LIFT = 'vertex'           # how a poke lifts its triangle's vertices: 'tri' (minimum-norm lift along the triangle normal) or 'vertex' (each corner by depth + margin along the body normal)
POKE_NEAR = .6               # a poke needs the hit point's distance to the body >= this share of its depth along the sample's normal
POKE_MIN_COS = .05            # smallest n.t used by the 'tri' lift
INSIDE_TEST = 'pseudonormal'   # sign of the signed distance: 'pseudonormal' (angle-weighted pseudonormal of the closest feature, winding only where that is unreliable) or 'winding' (reference: winding number against every triangle)
PN_BARY_TOL = 1e-7            # barycentric coordinate below this = the closest point is on the edge / vertex opposite
PN_AMBIG_DIST = 1e-6          # closest point this near the surface (m): the pseudonormal sign is ambiguous, winding decides
PN_REACH = .05                # triangles this close (m) to a boundary / non-manifold edge take the winding number
PN_WELD = 1e-7                # body vertices closer than this (m) are one vertex of the pseudonormal topology
CODE_VERSION = 'two-sided-20261025'   # bump when a change makes earlier fits stale (reuse of fresh outputs is skipped across versions)
SUFFIX = {'full': '_fitted', 'vertex': '_fitted_vertex'}   # output name suffix per fit method (`full` = the names the preview / Outfit Studio read)
SOURCE_SUFFIX = {'original': '', 'aligned': '_aligned'}    # which wearable is fitted: the converted one or the one align_body.py wrote
POKE_CAP = POKE_IN + .03       # poke-driven lift of a vertex along the body normal never exceeds this (+ margin): what does not clear by then is reported
POKE_CLEANUP = 8              # hard poke projections after the smooth rounds (the vertex test keeps CLEANUP_ITERS)
POKE_PASSES = 3               # extra level passes for rigid pieces (pokes that remain after the per-piece fits)


def _load_transform():
    spec = importlib.util.spec_from_file_location('outfit_transform', ROOT / 'tools' / 'outfit_studio' / 'core' / 'transform.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tf = _load_transform()


def rel(path):
    p = Path(path).resolve()
    return p.relative_to(ROOT).as_posix() if p.is_relative_to(ROOT) else str(p)


def write_atomic(path, data):
    path = Path(path)
    tmp = path.with_name(f'.{path.name}.tmp{os.getpid()}')
    if isinstance(data, str):
        data = data.encode()
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_json(path, obj, indent=1):
    write_atomic(path, json.dumps(obj, indent=indent, default=float) + '\n')


# ----------------------------------------------------------------------------------------------- body

def _dot(a, b):
    return np.einsum('ij,ij->i', a, b)


def tri_closest(p, a, b, c):
    """Closest point on triangles (a, b, c) to points p, all (k,3) (Ericson, Real-Time Collision Detection 5.1.5).
    Returns barycentric coordinates (k,3) of the closest point."""
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = _dot(ab, ap), _dot(ac, ap)
    bp = p - b
    d3, d4 = _dot(ab, bp), _dot(ac, bp)
    cp = p - c
    d5, d6 = _dot(ab, cp), _dot(ac, cp)
    vc, vb, va = d1 * d4 - d3 * d2, d5 * d2 - d1 * d6, d3 * d6 - d5 * d4
    k = len(p)
    out = np.zeros((k, 3))
    with np.errstate(divide='ignore', invalid='ignore'):
        den = 1. / (va + vb + vc)
        v, w = vb * den, vc * den
        out[:] = np.stack([1 - v - w, v, w], 1)                                   # face interior
        t = (d4 - d3) / ((d4 - d3) + (d5 - d6))                                    # edge BC
        m = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
        out[m] = np.stack([np.zeros(m.sum()), 1 - t[m], t[m]], 1)
        t = d2 / (d2 - d6)                                                          # edge AC
        m = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
        out[m] = np.stack([1 - t[m], np.zeros(m.sum()), t[m]], 1)
        m = (d6 >= 0) & (d5 <= d6)                                                   # vertex C
        out[m] = (0, 0, 1)
        t = d1 / (d1 - d3)                                                          # edge AB
        m = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
        out[m] = np.stack([1 - t[m], t[m], np.zeros(m.sum())], 1)
        m = (d3 >= 0) & (d4 <= d3)                                                   # vertex B
        out[m] = (0, 1, 0)
        m = (d1 <= 0) & (d2 <= 0)                                                    # vertex A
        out[m] = (1, 0, 0)
    return np.nan_to_num(out, nan=1 / 3.)


class Body:
    """The game base mesh (Blender world coordinates, rest pose): exact signed distance, outward normal, part of the closest triangle."""

    def __init__(self, V, F, part, part_names):
        V = np.asarray(V, np.float64)
        F = np.asarray(F, np.int64)
        part = np.asarray(part, np.int64)
        a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
        cross = np.cross(b - a, c - a)
        keep = np.linalg.norm(cross, axis=1) > 1e-12
        F, part = F[keep], part[keep]
        if np.einsum('ij,ij->', V[F[:, 0]], np.cross(V[F[:, 1]], V[F[:, 2]])) < 0:      # signed volume: make the winding outward
            F = F[:, [0, 2, 1]].copy()
        self.V, self.F, self.part = V, F, part
        self.part_names = [str(n) for n in part_names]
        self.A, self.B, self.C = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
        cross = np.cross(self.B - self.A, self.C - self.A)
        self.area = np.linalg.norm(cross, axis=1) / 2
        self.tri_n = cross / (2 * self.area[:, None])
        vn = np.zeros_like(V)                                                          # angle-weighted vertex pseudo-normals
        for i in range(3):
            e1 = V[F[:, (i + 1) % 3]] - V[F[:, i]]
            e2 = V[F[:, (i + 2) % 3]] - V[F[:, i]]
            ang = np.arccos(np.clip(_dot(e1, e2) / (np.linalg.norm(e1, axis=1) * np.linalg.norm(e2, axis=1) + 1e-30), -1, 1))
            np.add.at(vn, F[:, i], self.tri_n * ang[:, None])
        ln = np.linalg.norm(vn, axis=1, keepdims=True)
        self.vn = np.where(ln > 1e-12, vn / np.maximum(ln, 1e-30), 0.)
        self.cent = (self.A + self.B + self.C) / 3
        self.rad = np.max([np.linalg.norm(x - self.cent, axis=1) for x in (self.A, self.B, self.C)], axis=0)
        self.rmax = float(self.rad.max())
        self.tree = cKDTree(self.cent)
        self.lo, self.hi = V.min(0), V.max(0)
        self.o, self.H = tf.rest_frame(V)

    @classmethod
    def load(cls, race):
        z = np.load(BASE_CACHE / f'{race}.npz', allow_pickle=False)
        return cls(z['V'], z['F'], z['part'], z['part_names'])

    def closest(self, P, chunk=1024, _cpu=False):
        """(distance (k,), triangle (k,), barycentric (k,3)) of the exact closest body point to every point."""
        P = np.asarray(P, np.float64)
        K = len(P)
        if _GPU_ON and 'closest' in _GPU_PARTS and not _cpu and K * len(self.F) >= GPU_MIN_TESTS:
            try:                     # the GPU picks the nearest triangle, the point is computed here in float64; ties are settled on the CPU
                ti, tie = _gpu().closest_tri(self, self.A, self.B, self.C, P)
                bb = tri_closest(P, self.A[ti], self.B[ti], self.C[ti])
                dd = np.linalg.norm(P - (bb[:, :1] * self.A[ti] + bb[:, 1:2] * self.B[ti] + bb[:, 2:] * self.C[ti]), axis=1)
                if tie.any():
                    dd[tie], ti[tie], bb[tie] = self.closest(P[tie], chunk, _cpu=True)
                return dd, ti, bb
            except Exception as e:  # noqa: BLE001
                _gpu_failed(e)
        dist, tri, bary = np.empty(K), np.empty(K, np.int64), np.empty((K, 3))
        kn = min(8, len(self.F))
        for s in range(0, K, chunk):
            p = P[s:s + chunk]
            n = len(p)
            _, idx = self.tree.query(p, k=kn)
            idx = idx.reshape(n, -1)
            pp = np.repeat(p, idx.shape[1], axis=0)
            ti = idx.ravel()
            bb = tri_closest(pp, self.A[ti], self.B[ti], self.C[ti])
            dd = np.linalg.norm(pp - (bb[:, :1] * self.A[ti] + bb[:, 1:2] * self.B[ti] + bb[:, 2:] * self.C[ti]), axis=1)
            ub = dd.reshape(n, -1).min(1) * (1 + 1e-9) + 1e-12
            lists = self.tree.query_ball_point(p, ub + self.rmax)
            counts = np.fromiter((len(x) for x in lists), np.int64, n)
            pi = np.repeat(np.arange(n), counts)
            ti = np.concatenate([np.asarray(x, np.int64) for x in lists])
            near = np.linalg.norm(p[pi] - self.cent[ti], axis=1) - self.rad[ti] <= ub[pi]
            pi, ti = pi[near], ti[near]
            pp = p[pi]
            bb = tri_closest(pp, self.A[ti], self.B[ti], self.C[ti])
            dd = np.linalg.norm(pp - (bb[:, :1] * self.A[ti] + bb[:, 1:2] * self.B[ti] + bb[:, 2:] * self.C[ti]), axis=1)
            starts = np.searchsorted(pi, np.arange(n))
            mn = np.minimum.reduceat(dd, starts)
            hit = dd <= mn[pi]
            first = np.unique(pi[hit], return_index=True)[1]
            sel = np.flatnonzero(hit)[first]
            dist[s:s + n], tri[s:s + n], bary[s:s + n] = mn, ti[sel], bb[sel]
        return dist, tri, bary

    def winding(self, P, chunk_pairs=600_000, _cpu=False):
        """Generalized winding number of the body at every point (1 inside, 0 outside, robust to small holes)."""
        P = np.asarray(P, np.float64)
        out = np.zeros(len(P))
        box = np.all((P >= self.lo) & (P <= self.hi), axis=1)
        idx = np.flatnonzero(box)
        if _GPU_ON and (GPU_WINDING or 'winding' in _GPU_PARTS) and not _cpu and len(idx) * len(self.F) >= GPU_MIN_TESTS:
            try:
                w, unsure = _gpu().winding(self, self.A, self.B, self.C, P[idx])
                if unsure.any():                                       # points on / next to the surface: float64 on the CPU
                    w[unsure] = self.winding(P[idx[unsure]], chunk_pairs, _cpu=True)
                out[idx] = w
                return out
            except Exception as e:  # noqa: BLE001
                _gpu_failed(e)
        step = max(1, chunk_pairs // max(1, len(self.F)))
        for s in range(0, len(idx), step):
            ii = idx[s:s + step]
            p = P[ii][:, None, :]
            a, b, c = self.A[None] - p, self.B[None] - p, self.C[None] - p
            la, lb, lc = (np.linalg.norm(x, axis=2) for x in (a, b, c))
            num = np.einsum('kmi,kmi->km', a, np.cross(b, c))
            den = la * lb * lc + np.einsum('kmi,kmi->km', a, b) * lc + np.einsum('kmi,kmi->km', b, c) * la + np.einsum('kmi,kmi->km', c, a) * lb
            out[ii] = np.arctan2(num, den).sum(1) * 2 / (4 * np.pi)
        return out

    def _unclean(self, Fw, defects):
        """Triangles that are not plain boundary of the body: they cross a triangle that shares no vertex with them (self-intersection),
        their centroid is not outside on the normal side and inside on the other (buried in another part, fold-over), or they lie
        within PN_REACH of a topological defect (`defects`: points along the boundary / non-manifold edges; around a hole or a
        sliver the winding number is not 0 / 1 any more and its 0.5 threshold, not the surface side, decides). The sign of the
        closest feature is only trustworthy where the whole feature is clean; the winding number sees the union of the parts."""
        nF = len(self.F)
        pairs = self.tree.query_pairs(2 * self.rmax, output_type='ndarray')
        i, j = pairs[:, 0], pairs[:, 1]
        near = np.linalg.norm(self.cent[i] - self.cent[j], axis=1) <= self.rad[i] + self.rad[j]
        i, j = i[near], j[near]
        share = (Fw[i][:, :, None] == Fw[j][:, None, :]).any((1, 2))
        i, j = i[~share], j[~share]
        hit = np.zeros(len(i), bool)
        T = (self.A, self.B, self.C)
        for a, b in ((i, j), (j, i)):                                                          # edges of a against triangle b
            for k in range(3):
                p0, p1 = T[k][a], T[(k + 1) % 3][a]
                d = p1 - p0
                e1, e2 = self.B[b] - self.A[b], self.C[b] - self.A[b]
                pv = np.cross(d, e2)
                det = _dot(e1, pv)
                with np.errstate(divide='ignore', invalid='ignore'):
                    inv = 1. / det
                    tv = p0 - self.A[b]
                    u = _dot(tv, pv) * inv
                    qv = np.cross(tv, e1)
                    v = _dot(d, qv) * inv
                    t = _dot(e2, qv) * inv
                    hit |= (np.abs(det) > 1e-14) & (u >= -1e-6) & (v >= -1e-6) & (u + v <= 1 + 1e-6) & (t >= -1e-6) & (t <= 1 + 1e-6)
        bad = np.zeros(nF, bool)
        bad[i[hit]] = bad[j[hit]] = True
        eps = 1e-4 * self.H
        w_out = self.winding(self.cent + eps * self.tri_n)
        w_in = self.winding(self.cent - eps * self.tri_n)
        if len(defects):
            bad |= cKDTree(defects).query(self.cent)[0] <= self.rad + PN_REACH + .005
        return bad | ~((w_out < .25) & (w_in > .75))

    def _pseudonormals(self):
        """Angle-weighted pseudonormals (Baerentzen & Aanaes 2005) on the position-welded topology, built once per body on first use:
        `en` (f,3,3) / `ebad` (f,3) per triangle edge k (opposite corner k) = sum of the two adjacent face normals, `vpn` / `vbad`
        per welded vertex = angle-weighted sum, and `Fw` the welded corner ids. A feature is `bad` (the sign test must not trust it)
        when it touches a boundary or non-manifold or inconsistently wound edge, a pinched vertex (its triangle fan is not one cycle),
        a collapsed triangle, or its pseudonormal cancels out. `fbad` per triangle: unclean (see `_unclean`) or touching a bad edge."""
        if getattr(self, '_pn', None) is not None:
            return self._pn
        V, F, nF = self.V, self.F, len(self.F)
        pairs = cKDTree(V).query_pairs(PN_WELD, output_type='ndarray')
        _, w = csgraph.connected_components(sparse.coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(len(V), len(V))), directed=False)
        nW = int(w.max()) + 1
        Fw = w[F]
        k3 = np.arange(3)
        ea, eb = Fw[:, (k3 + 1) % 3].ravel(), Fw[:, (k3 + 2) % 3].ravel()                    # edge k = (corner k+1 -> corner k+2), face-major
        _, first, inv, cnt = np.unique(np.minimum(ea, eb) * nW + np.maximum(ea, eb), return_index=True, return_inverse=True, return_counts=True)
        inv = inv.ravel()
        nE = len(cnt)
        dirs = np.bincount(inv, weights=np.where(ea < eb, 1., -1.), minlength=nE)
        degenerate = (ea == eb).reshape(nF, 3).any(1)                                         # a triangle collapsed by the weld
        bad_e = (cnt != 2) | (dirs != 0)                                                      # boundary / non-manifold / same-direction twins
        bad_e[inv[(ea == eb)]] = True
        tn = np.repeat(self.tri_n, 3, axis=0)
        esum = np.stack([np.bincount(inv, weights=tn[:, i], minlength=nE) for i in range(3)], 1)
        bad_e |= np.linalg.norm(esum, axis=1) < 1e-6                                          # folded back on itself
        fe = first[bad_e]                                                                     # the defect edges, sampled every ~1 cm
        fa, fk = fe // 3, fe % 3
        p0, p1 = V[F[fa, (fk + 1) % 3]], V[F[fa, (fk + 2) % 3]]
        m = np.maximum(2, np.ceil(np.linalg.norm(p1 - p0, axis=1) / .01).astype(np.int64) + 1)
        r = np.repeat(np.arange(len(m)), m)
        t = ((np.arange(len(r)) - np.repeat(np.cumsum(m) - m, m)) / np.repeat(m - 1, m))[:, None]
        unclean = self._unclean(Fw, p0[r] * (1 - t) + p1[r] * t)
        bad_e |= np.bincount(inv, weights=np.repeat(unclean, 3).astype(float), minlength=nE) > 0
        ebad = bad_e[inv].reshape(nF, 3)
        en = esum[inv].reshape(nF, 3, 3)
        vsum = np.zeros((nW, 3))
        for i in range(3):                                                                    # angle at corner i weights the face normal
            e1, e2 = V[F[:, (i + 1) % 3]] - V[F[:, i]], V[F[:, (i + 2) % 3]] - V[F[:, i]]
            ang = np.arccos(np.clip(_dot(e1, e2) / (np.linalg.norm(e1, axis=1) * np.linalg.norm(e2, axis=1) + 1e-30), -1, 1))
            for a in range(3):
                vsum[:, a] += np.bincount(Fw[:, i], weights=self.tri_n[:, a] * ang, minlength=nW)
        ln = np.linalg.norm(vsum, axis=1)
        vbad = ln < 1e-9
        for k in range(3):                                                                    # a vertex with a bad edge or collapsed triangle around it
            vbad |= np.bincount(Fw[:, k], weights=(ebad[:, (k + 1) % 3] | ebad[:, (k + 2) % 3] | degenerate | unclean).astype(float), minlength=nW) > 0
        ent_e = np.concatenate([inv.reshape(nF, 3)[:, (k3 + 1) % 3].ravel(), inv.reshape(nF, 3)[:, (k3 + 2) % 3].ravel()])   # corner c = 3f + k touches edges k+1, k+2
        ent_c = np.tile(np.arange(3 * nF), 2)
        key = ent_e * nW + np.tile(Fw.ravel(), 2)
        o = np.argsort(key, kind='stable')
        key, ent_c = key[o], ent_c[o]
        same = np.flatnonzero(key[1:] == key[:-1])                                            # corners of one vertex joined across an edge: its fan
        link = sparse.coo_matrix((np.ones(len(same)), (ent_c[same], ent_c[same + 1])), shape=(3 * nF, 3 * nF))
        _, comp = csgraph.connected_components(link, directed=False)
        vc = np.unique(np.stack([Fw.ravel(), comp], 1), axis=0)[:, 0]
        vbad |= np.bincount(vc, minlength=nW) != 1                                            # more than one fan = pinched
        self._pn = {'Fw': Fw, 'en': en, 'ebad': ebad, 'vpn': vsum, 'vbad': vbad, 'fbad': unclean | ebad.any(1)}
        return self._pn

    def inside(self, P, dist, tri, bary):
        """Inside flag per point from the closest triangle / barycentrics `closest` returns: sign of (p - q) . pseudonormal of the
        feature (face / edge / vertex) q lies on. Points whose feature is `bad` (see `_pseudonormals`) or that lie within PN_AMBIG_DIST
        of the surface take the winding number instead; outside the bounding box nothing is inside."""
        P = np.asarray(P, np.float64)
        pn = self._pseudonormals()
        k = len(P)
        n = self.tri_n[tri].copy()
        bad = pn['fbad'][tri].copy()
        nz = (bary > PN_BARY_TOL).sum(1)
        e = np.flatnonzero(nz == 2)
        ke = bary[e].argmin(1)
        n[e], bad[e] = pn['en'][tri[e], ke], pn['ebad'][tri[e], ke]
        v = np.flatnonzero(nz == 1)
        vi = pn['Fw'][tri[v], bary[v].argmax(1)]
        n[v], bad[v] = pn['vpn'][vi], pn['vbad'][vi]
        q = bary[:, :1] * self.A[tri] + bary[:, 1:2] * self.B[tri] + bary[:, 2:] * self.C[tri]
        s = _dot(P - q, n)
        bad |= (dist < PN_AMBIG_DIST) | (np.abs(s) <= 1e-9 * dist)
        out = s < 0
        amb = np.flatnonzero(bad)
        if len(amb):
            out[amb] = self.winding(P[amb]) > .5
        return out & np.all((P >= self.lo) & (P <= self.hi), axis=1)

    def measure(self, P, inside_test=None):
        """{'d' signed distance (negative inside), 'n' outward unit normal at the closest point, 'part' (part index of the closest
        triangle), 'tri'} for points P. `inside_test` (default INSIDE_TEST) picks the sign: 'pseudonormal' or 'winding'."""
        dist, tri, bary = self.closest(P)
        inside = self.inside(P, dist, tri, bary) if (inside_test or INSIDE_TEST) == 'pseudonormal' else self.winding(P) > .5
        n = np.einsum('ki,kij->kj', bary, self.vn[self.F[tri]])
        ln = np.linalg.norm(n, axis=1, keepdims=True)
        n = np.where(ln > 1e-9, n / np.maximum(ln, 1e-30), self.tri_n[tri])
        return {'d': np.where(inside, -dist, dist), 'n': n, 'part': self.part[tri], 'tri': tri}


_BODIES = {}


def get_body(race):
    if race not in _BODIES:
        _BODIES[race] = Body.load(race)
    return _BODIES[race]


# ----------------------------------------------------------------------------------------------- GLB

_DTYPE = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_NCOMP = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}


def node_matrix(node):
    if 'matrix' in node:
        return np.array(node['matrix'], np.float64).reshape(4, 4).T
    m = np.eye(4)
    if 'scale' in node:
        m = np.diag([*node['scale'], 1.]) @ m
    if 'rotation' in node:
        x, y, z, w = node['rotation']
        r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                      [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                      [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
        m = np.pad(r, ((0, 1), (0, 1))) @ m
        m[3, 3] = 1
    if 'translation' in node:
        m[:3, 3] += node['translation']
    return m


def gl_to_bl(p):
    p = np.asarray(p, np.float64)
    return np.stack([p[:, 0], -p[:, 2], p[:, 1]], 1)


def bl_to_gl(p):
    p = np.asarray(p, np.float64)
    return np.stack([p[:, 0], p[:, 2], -p[:, 1]], 1)


class Glb:
    """A GLB with triangle primitives: read positions / indices / normals, write a copy with new POSITION / NORMAL data only.
    `prims` follow three.js GLTFLoader traversal (scene nodes depth first, a node's meshes before its children, one three.js
    Mesh per primitive) and are the order of the clearance sidecar's `meshes`."""

    def __init__(self, path):
        self.path = Path(path)
        data = self.path.read_bytes()
        magic, _, _ = struct.unpack_from('<4sII', data, 0)
        if magic != b'glTF':
            raise ValueError(f'{path}: not a GLB')
        self.chunks, off = [], 12
        while off < len(data):
            size, kind = struct.unpack_from('<II', data, off)
            self.chunks.append([kind, bytes(data[off + 8:off + 8 + size])])
            off += 8 + size
        self.js = json.loads(self.chunks[0][1])
        self.compact = b'"asset":' in self.chunks[0][1][:40]
        blob = [c for c in self.chunks if c[0] == 0x004E4942]
        self.blob = bytearray(blob[0][1]) if blob else bytearray()
        self.prims = self._walk()

    def _walk(self):
        js = self.js
        nodes = js.get('nodes', [])
        roots = js['scenes'][js.get('scene', 0)]['nodes'] if js.get('scenes') else list(range(len(nodes)))
        out, used = [], {}

        def visit(i, parent):
            node = nodes[i]
            m = parent @ node_matrix(node)
            if 'mesh' in node:
                mesh = js['meshes'][node['mesh']]
                base = mesh.get('name') or node.get('name') or f'mesh_{node["mesh"]}'
                count = used.get(base, 0)
                for pi, prim in enumerate(mesh['primitives']):
                    if prim.get('mode', 4) != 4:
                        raise ValueError(f'{self.path}: only triangle primitives are supported')
                    name = base if count == 0 else f'{base}_{count}'
                    count += 1
                    out.append({'mesh': node['mesh'], 'prim': pi, 'node': i, 'name': name, 'matrix': m, 'def': prim})
                used[base] = count
            for ch in node.get('children', []):
                visit(ch, m)
        for r in roots:
            visit(r, np.eye(4))
        seen = {}
        for k, p in enumerate(out):
            acc = p['def']['attributes']['POSITION']
            if acc in seen:
                raise ValueError(f'{self.path}: POSITION accessor {acc} is shared by primitives {seen[acc]} and {k}')
            seen[acc] = k
        return out

    def accessor(self, index):
        acc = self.js['accessors'][index]
        view = self.js['bufferViews'][acc['bufferView']]
        dtype, ncomp = np.dtype(_DTYPE[acc['componentType']]), _NCOMP[acc['type']]
        offset = view.get('byteOffset', 0) + acc.get('byteOffset', 0)
        stride = view.get('byteStride') or dtype.itemsize * ncomp
        return np.ndarray((acc['count'], ncomp), dtype, self.blob, offset, (stride, dtype.itemsize))

    def positions(self, k):
        return self.accessor(self.prims[k]['def']['attributes']['POSITION']).astype(np.float64)

    def normals(self, k):
        a = self.prims[k]['def']['attributes'].get('NORMAL')
        return None if a is None else self.accessor(a).astype(np.float64)

    def triangles(self, k):
        prim = self.prims[k]['def']
        if 'indices' in prim:
            idx = self.accessor(prim['indices']).astype(np.int64).ravel()
        else:
            idx = np.arange(self.js['accessors'][prim['attributes']['POSITION']]['count'])
        return idx[:len(idx) // 3 * 3].reshape(-1, 3)

    def patched(self, positions=None, normals=None):
        """GLB bytes with POSITION (`positions[k]` (n,3) local, float32) and NORMAL data replaced; accessor min/max updated;
        everything else (JSON order, extras, materials, other bytes) unchanged."""
        js = json.loads(json.dumps(self.js))
        blob = bytearray(self.blob)
        for kind, table in (('POSITION', positions or {}), ('NORMAL', normals or {})):
            for k, arr in table.items():
                idx = self.prims[k]['def']['attributes'][kind]
                acc, view = js['accessors'][idx], js['bufferViews'][js['accessors'][idx]['bufferView']]
                offset = view.get('byteOffset', 0) + acc.get('byteOffset', 0)
                stride = view.get('byteStride') or 12
                dst = np.ndarray((acc['count'], 3), np.float32, blob, offset, (stride, 4))
                dst[:] = np.asarray(arr, np.float32)
                if 'min' in acc:
                    acc['min'], acc['max'] = [float(x) for x in dst.min(0)], [float(x) for x in dst.max(0)]
        text = json.dumps(js, separators=(',', ':') if self.compact else None).encode()
        text += b' ' * (-len(text) % 4)
        chunks = [[kind, bytes(blob) if kind == 0x004E4942 else (text if kind == 0x4E4F534A else raw)] for kind, raw in self.chunks]
        body = b''.join(struct.pack('<II', len(raw), kind) + raw for kind, raw in chunks)
        return struct.pack('<4sII', b'glTF', 2, 12 + len(body)) + body


# ----------------------------------------------------------------------------------------------- surface in game world space

class Surface:
    """A GLB's triangles in game world space, with identical (welded) vertex positions merged across primitives."""

    def __init__(self, glb, manifest, body, weld=1e-6):
        self.glb, self.manifest, self.body = glb, manifest, body
        self.fit = manifest['parts']['fit']
        Ws, Ts, self.offsets, self.tri_offsets, base = [], [], [], [], 0
        for k in range(len(glb.prims)):
            m = glb.prims[k]['matrix']
            p = glb.positions(k)
            gl = p @ m[:3, :3].T + m[:3, 3]
            Ws.append(self.to_world(gl))
            t = glb.triangles(k)
            Ts.append(t + base)
            self.offsets.append(base)
            self.tri_offsets.append(sum(len(x) for x in Ts[:-1]))
            base += len(p)
        self.Wc = np.concatenate(Ws) if Ws else np.zeros((0, 3))
        self.T = np.concatenate(Ts) if Ts else np.zeros((0, 3), np.int64)
        self.n_tri = [len(glb.triangles(k)) for k in range(len(glb.prims))]
        n = len(self.Wc)
        if n:
            pairs = cKDTree(self.Wc).query_pairs(weld, output_type='ndarray')
            g = sparse.coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
            self.K, self.wid = csgraph.connected_components(g, directed=False)
        else:
            self.K, self.wid = 0, np.zeros(0, np.int64)
        first = np.full(self.K, -1, np.int64)
        first[self.wid[::-1]] = np.arange(n)[::-1]
        self.U = self.Wc[first] if n else np.zeros((0, 3))
        self.Tw = self.wid[self.T]

    def to_world(self, gl):
        return tf.native_to_world(gl_to_bl(gl), self.manifest, self.fit, self.body.H, self.body.o)

    def to_local(self, world, k):
        """World points -> local float64 coordinates of primitive k's accessor."""
        gl = bl_to_gl(tf.world_to_native(world, self.manifest, self.fit, self.body.H, self.body.o))
        m = np.linalg.inv(self.glb.prims[k]['matrix'])
        return gl @ m[:3, :3].T + m[:3, 3]

    def tri_area(self, U=None):
        U = self.U if U is None else U
        a, b, c = U[self.Tw[:, 0]], U[self.Tw[:, 1]], U[self.Tw[:, 2]]
        return np.linalg.norm(np.cross(b - a, c - a), axis=1) / 2

    def face_normals(self, U):
        a, b, c = U[self.Tw[:, 0]], U[self.Tw[:, 1]], U[self.Tw[:, 2]]
        n = np.cross(b - a, c - a)
        ln = np.linalg.norm(n, axis=1, keepdims=True)
        return np.where(ln > 1e-30, n / np.maximum(ln, 1e-30), 0.)

    def edges(self):
        e = np.sort(np.concatenate([self.Tw[:, [0, 1]], self.Tw[:, [1, 2]], self.Tw[:, [2, 0]]]), axis=1)
        return e[e[:, 0] != e[:, 1]]

    def apply(self, disp):
        """Per-primitive new local positions for a world displacement per welded vertex (corners that do not move stay bit-exact)."""
        out = {}
        for k in range(len(self.glb.prims)):
            lo, hi = self.offsets[k], self.offsets[k] + len(self.glb.positions(k))
            d = disp[self.wid[lo:hi]]
            new = self.glb.positions(k)
            move = np.linalg.norm(d, axis=1) > 0
            if move.any():
                new[move] = self.to_local(self.Wc[lo:hi][move] + d[move], k)
            out[k] = new
        return out


def measure_surface(surf, U=None, body=None):
    """Signed distance per welded vertex and per triangle (min of its three vertices)."""
    body = body or surf.body
    U = surf.U if U is None else U
    m = body.measure(U)
    d = m['d'][surf.Tw]
    worst = d.argmin(1)
    rows = np.arange(len(d))
    return {'d': m['d'], 'n': m['n'], 'part': m['part'], 'tri_d': d[rows, worst], 'tri_part': m['part'][surf.Tw][rows, worst],
            'tri_area': surf.tri_area(U)}


def summarize(meas, margin, part_names, centroid=None):
    """Metrics of a measurement: penetrating / within-margin area, depth, by body part (distances in mm, areas in m^2)."""
    d, area, part = meas['tri_d'], meas['tri_area'], meas['tri_part']
    total = float(area.sum())
    pen, near = d < 0, (d >= 0) & (d < margin)
    depth = -d[pen] * 1000

    def block(mask):
        return {'triangles': int(mask.sum()), 'area_m2': float(area[mask].sum()), 'area_pct': float(100 * area[mask].sum() / total) if total else 0.}
    by = {}
    for i in np.unique(part):
        sel = part == i
        by[part_names[i]] = {'area_m2': float(area[sel].sum()), 'penetrating_area_m2': float(area[sel & pen].sum()),
                             'within_margin_area_m2': float(area[sel & near].sum()),
                             'max_depth_mm': float(max(0., -d[sel].min() * 1000)) if sel.any() else 0.}
    out = {'triangles': int(len(d)), 'area_m2': total, 'margin_mm': float(margin * 1000), 'penetrating': block(pen),
           'within_margin': block(near), 'max_depth_mm': float(depth.max()) if len(depth) else 0.,
           'p95_depth_mm': float(np.percentile(depth, 95)) if len(depth) else 0.,
           'min_d_mm': float(d.min() * 1000) if len(d) else 0., 'by_part': by}
    if centroid is not None:
        out['centroid_penetrating_area_pct'] = float(100 * area[centroid < 0].sum() / total) if total else 0.
    return out


def centroid_signed(surf, U=None):
    U = surf.U if U is None else U
    c = (U[surf.Tw[:, 0]] + U[surf.Tw[:, 1]] + U[surf.Tw[:, 2]]) / 3
    return surf.body.measure(c)['d']


def clearance_doc(surf, meas, margin, metrics):
    """Sidecar of the preview's Clearance overlay. `meas['tri_d']` is what is drawn: pass the vertex distances with the
    body-through-armour test folded in (fold_poke) to colour the poked triangles red."""
    meshes = []
    for k, prim in enumerate(surf.glb.prims):
        lo = surf.tri_offsets[k]
        meshes.append({'mesh_index': k, 'name': prim['name'],
                       'd_mm': [round(float(x), 1) for x in meas['tri_d'][lo:lo + surf.n_tri[k]] * 1000]})
    return {'model': rel(surf.glb.path), 'margin_mm': round(margin * 1000, 3), 'metrics': metrics, 'meshes': meshes}


# ----------------------------------------------------------------------------------------------- body through armour (reverse test)

class RayGrid:
    """Uniform grid over a triangle soup for short segment queries. Triangles are bucketed by their (dilated) bounding box, a
    segment is sampled every cell/2, so every triangle the segment crosses is among the candidates of one of its samples."""

    def __init__(self, A, B, C, cell=CELL):
        self.A, self.B, self.C = A, B, C
        self.n, self.cell = len(A), cell
        if not self.n:
            return
        pad = cell / 4 + 1e-6
        lo = np.minimum(np.minimum(A, B), C) - pad
        hi = np.maximum(np.maximum(A, B), C) + pad
        self.o = lo.min(0)
        ilo = np.floor((lo - self.o) / cell).astype(np.int64)
        ihi = np.floor((hi - self.o) / cell).astype(np.int64)
        self.dim = ihi.max(0) + 1
        span = ihi - ilo + 1
        counts = span.prod(1)
        csum = np.cumsum(counts)
        cids, tris, start = [], [], 0
        while start < self.n:                                    # expand the bounding boxes into cells in bounded chunks
            base = int(csum[start - 1]) if start else 0
            end = min(self.n, max(start + 1, int(np.searchsorted(csum, base + 2_000_000, side='right'))))
            cnt = counts[start:end]
            tri = np.repeat(np.arange(start, end), cnt)
            off = np.arange(int(cnt.sum())) - np.repeat(np.cumsum(cnt) - cnt, cnt)
            sx, sy = span[tri, 0], span[tri, 1]
            ix, iy, iz = off % sx, (off // sx) % sy, off // (sx * sy)
            cids.append(((ilo[tri, 0] + ix) * self.dim[1] + (ilo[tri, 1] + iy)) * self.dim[2] + (ilo[tri, 2] + iz))
            tris.append(tri)
            start = end
        cid, tri = np.concatenate(cids), np.concatenate(tris)
        order = np.argsort(cid, kind='stable')
        self.cid, self.tri = cid[order], tri[order]

    def hits(self, P, N, t_lo=-POKE_IN, t_hi=POKE_OUT, eps=1e-5, chunk=3000):
        """Nearest hit on the line P + t N, t in [t_lo, t_hi]: outward (t > eps) and inward (t < -eps), both orientations.
        Returns t_out / t_in (inf / -inf when none), the triangle indices and the barycentric (u, v) of the inward hit."""
        n = len(P)
        out = {'t_out': np.full(n, np.inf), 'tri_out': np.full(n, -1, np.int64), 't_in': np.full(n, -np.inf),
               'tri_in': np.full(n, -1, np.int64), 'uv_in': np.zeros((n, 2))}
        if not self.n or not n:
            return out
        if _GPU_ON and 'rays' in _GPU_PARTS and n * self.n >= GPU_MIN_TESTS:
            try:
                return _gpu().ray_hits(self, P, N, t_lo, t_hi, eps)
            except Exception as e:  # noqa: BLE001
                _gpu_failed(e)
        steps = int(np.ceil((t_hi - t_lo) / (self.cell / 2))) + 1
        ts = np.linspace(t_lo, t_hi, steps)
        for s in range(0, n, chunk):
            p, d = P[s:s + chunk], N[s:s + chunk]
            pts = p[:, None, :] + ts[None, :, None] * d[:, None, :]
            ic = np.floor((pts - self.o) / self.cell).astype(np.int64)
            ok = np.all((ic >= 0) & (ic < self.dim), axis=2)
            cid = np.where(ok, (ic[..., 0] * self.dim[1] + ic[..., 1]) * self.dim[2] + ic[..., 2], -1).ravel()
            a, b = np.searchsorted(self.cid, cid, 'left'), np.searchsorted(self.cid, cid, 'right')
            cnt = b - a
            total = int(cnt.sum())
            if not total:
                continue
            ray = np.repeat(np.arange(len(cid)) // steps, cnt)
            tri = self.tri[np.arange(total) + np.repeat(a - (np.cumsum(cnt) - cnt), cnt)]
            key = np.sort(ray.astype(np.int64) * self.n + tri)    # = np.unique(key), which is ~15x slower here (numpy 2.4)
            key = key[np.r_[True, key[1:] != key[:-1]]]
            ray, tri = key // self.n, key % self.n
            e1, e2 = self.B[tri] - self.A[tri], self.C[tri] - self.A[tri]
            dd = d[ray]
            pv = np.cross(dd, e2)
            det = _dot(e1, pv)
            good = np.abs(det) > 1e-14
            inv = 1. / np.where(good, det, 1.)
            tv = p[ray] - self.A[tri]
            u = _dot(tv, pv) * inv
            q = np.cross(tv, e1)
            v = _dot(dd, q) * inv
            t = _dot(e2, q) * inv
            hit = good & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9) & (t >= t_lo) & (t <= t_hi) & (np.abs(t) > eps)
            ray, tri, u, v, t = ray[hit] + s, tri[hit], u[hit], v[hit], t[hit]
            fwd = t > 0
            np.minimum.at(out['t_out'], ray[fwd], t[fwd])
            sel = fwd & (t == out['t_out'][ray])
            out['tri_out'][ray[sel]] = tri[sel]
            np.maximum.at(out['t_in'], ray[~fwd], t[~fwd])
            sel = ~fwd & (t == out['t_in'][ray])
            out['tri_in'][ray[sel]] = tri[sel]
            out['uv_in'][ray[sel]] = np.stack([u[sel], v[sel]], 1)
        return out


def body_samples(body):
    """Samples of the body surface (cached on the body): sub-triangle centroids with at most POKE_SPACING_PCT % of the height
    between samples, each with its triangle's outward normal, area share, part and the distance `exit` the line along -n
    stays inside the body (inf when the body is open there)."""
    if getattr(body, '_samples', None) is not None:
        return body._samples
    spacing = POKE_SPACING_PCT / 100 * body.H
    edge = np.max([np.linalg.norm(body.B - body.A, axis=1), np.linalg.norm(body.C - body.B, axis=1), np.linalg.norm(body.A - body.C, axis=1)], axis=0)
    m = np.maximum(1, np.ceil(edge / spacing)).astype(np.int64)
    parts = {'P': [], 'tri': [], 'area': []}
    for mv in np.unique(m):
        sel = np.flatnonzero(m == mv)
        uv = [((i + 1 / 3) / mv, (j + 1 / 3) / mv) for i in range(mv) for j in range(mv - i)]
        uv += [((i + 2 / 3) / mv, (j + 2 / 3) / mv) for i in range(mv - 1) for j in range(mv - 1 - i)]
        uv = np.array(uv)
        w = np.stack([1 - uv.sum(1), uv[:, 0], uv[:, 1]], 1)                                    # (s, 3)
        P = (w[:, 0, None, None] * body.A[sel] + w[:, 1, None, None] * body.B[sel] + w[:, 2, None, None] * body.C[sel])
        parts['P'].append(P.reshape(-1, 3))
        parts['tri'].append(np.tile(sel, len(w)))
        parts['area'].append(np.tile(body.area[sel] / mv ** 2, len(w)))
    tri = np.concatenate(parts['tri'])
    smp = {'P': np.concatenate(parts['P']), 'tri': tri, 'area': np.concatenate(parts['area']), 'N': body.tri_n[tri], 'part': body.part[tri],
           'spacing': spacing}
    hit = RayGrid(body.A, body.B, body.C).hits(smp['P'], smp['N'], -POKE_IN, 0., eps=1e-4)
    smp['exit'] = -hit['t_in']
    body._samples = smp
    return smp


class PokeScene:
    """The body samples that can matter for a set of armour surfaces (those within reach of their bounding box) and the
    casts / combination of the reverse test."""

    def __init__(self, body, lo, hi):
        smp = body_samples(body)
        reach = POKE_OUT
        near = np.flatnonzero(np.all((smp['P'] >= lo - reach) & (smp['P'] <= hi + reach), axis=1))
        self.body, self.spacing = body, smp['spacing']
        self.P, self.N, self.area, self.part, self.exit = (smp[k][near] for k in ('P', 'N', 'area', 'part', 'exit'))

    def cast(self, A, B, C, sub=None, cache=False):
        """The armour triangles A, B, C (n,3) as seen from the scene's body samples (`sub` = sample indices to serve)."""
        return Armour(self, A, B, C, sub, cache)

    def poke(self, armours, sub=None):
        """Samples where the body is outside the armour: no armour along +n (within POKE_OUT) in any of the `armours` (one per
        surface), but armour behind the body surface (within POKE_IN) whose hit point is still inside the body. The nearest
        inward hit is attributed to its surface (`src` = index into `armours`). The inward test is the cheap one (most samples have
        no armour behind them), so the outward coverage is only looked up for the samples that have an inward hit."""
        idx_all = np.arange(len(self.P)) if sub is None else sub
        t_in_all = np.stack([a.inward(idx_all)[0] for a in armours])
        src = t_in_all.argmax(0)
        t_in = t_in_all.max(0)
        has = np.flatnonzero(np.isfinite(t_in))
        idx = idx_all[has]
        covered = np.zeros(len(has), bool)
        for a in armours:
            covered |= np.isfinite(a.outward(idx, covered))
        cand = np.flatnonzero(~covered)
        has, idx = has[cand], idx[cand]
        depth = -t_in[has]
        keep = depth < self.exit[idx]                                          # the hit point is inside the body ...
        has, idx, depth = has[keep], idx[keep], depth[keep]
        if len(depth):                                                         # ... and this sample is (about) its nearest body surface:
            X = self.P[idx] - self.N[idx] * depth[:, None]                     # an armour triangle 100 mm behind a chest sample but 15 mm
            keep = self.body.closest(X)[0] >= POKE_NEAR * depth                # from the back of the body belongs to the back's samples
            has, idx, depth = has[keep], idx[keep], depth[keep]
        s = src[has]
        tri = np.zeros(len(has), np.int64)
        uv = np.zeros((len(has), 2))
        for i, a in enumerate(armours):
            m = s == i
            if m.any():
                _, ti, uvi = a.inward(idx_all[has[m]])
                tri[m], uv[m] = ti, uvi
        return {'sample': idx, 'src': s, 'depth': depth, 'area': self.area[idx], 'part': self.part[idx], 'N': self.N[idx], 'tri': tri, 'uv': uv}


class Armour:
    """One armour surface's triangles seen from the scene's body samples: nearest hit behind the body surface (`inward`) and
    whether anything lies outside it (`outward`, staged: near first, far only for the samples still uncovered). With `cache`
    (static surfaces) results are kept per sample, so repeated measurements of the other pieces cost nothing."""

    def __init__(self, scene, A, B, C, sub=None, cache=False):
        self.scene, self.grid, self.cache = scene, RayGrid(A, B, C), cache
        self._in = self._out = None
        n = len(scene.P)
        if cache:
            self._in = (np.full(n, np.nan), np.zeros(n, np.int64), np.zeros((n, 2)))
            self._out = np.full(n, np.nan)

    def inward(self, idx):
        sc = self.scene
        if not self.cache:
            h = self.grid.hits(sc.P[idx], sc.N[idx], -POKE_IN, 0.)
            return h['t_in'], h['tri_in'], h['uv_in']
        todo = idx[np.isnan(self._in[0][idx])]
        if len(todo):
            h = self.grid.hits(sc.P[todo], sc.N[todo], -POKE_IN, 0.)
            self._in[0][todo], self._in[1][todo], self._in[2][todo] = h['t_in'], h['tri_in'], h['uv_in']
        return self._in[0][idx], self._in[1][idx], self._in[2][idx]

    def _out_hits(self, idx):
        sc = self.scene
        t = self.grid.hits(sc.P[idx], sc.N[idx], 0., POKE_NEAR_OUT)['t_out']
        far = np.flatnonzero(~np.isfinite(t))
        if len(far):
            t[far] = self.grid.hits(sc.P[idx[far]], sc.N[idx[far]], 0., POKE_OUT)['t_out']
        return t

    def outward(self, idx, skip=None):
        """Nearest outward hit distance (inf when none) for the samples `idx`; those flagged in `skip` are not looked at (inf)."""
        todo = np.arange(len(idx)) if skip is None else np.flatnonzero(~skip)
        out = np.full(len(idx), np.inf)
        if not len(todo):
            return out
        if not self.cache:
            out[todo] = self._out_hits(idx[todo])
            return out
        need = todo[np.isnan(self._out[idx[todo]])]
        if len(need):
            self._out[idx[need]] = self._out_hits(idx[need])
        out[todo] = self._out[idx[todo]]
        return out


def scene_for(body, points):
    pts = [p for p in points if len(p)]
    return PokeScene(body, np.min([p.min(0) for p in pts], axis=0), np.max([p.max(0) for p in pts], axis=0))


def surf_tris(surf, U):
    return U[surf.Tw[:, 0]], U[surf.Tw[:, 1]], U[surf.Tw[:, 2]]


def poke_select(pk, mask):
    return {k: v[mask] for k, v in pk.items()}


def poke_ok(pk):
    """A surface is clear of the body when no poking sample is deeper than POKE_DEPTH_TOL or they cover less than POKE_AREA_TOL."""
    return not len(pk['depth']) or pk['depth'].max() < POKE_DEPTH_TOL or pk['area'].sum() < POKE_AREA_TOL


def poke_metrics(pk, part_names, spacing):
    depth = pk['depth'] * 1000
    out = {'area_cm2': float(pk['area'].sum() * 1e4), 'samples': int(len(depth)), 'sample_spacing_mm': float(spacing * 1000),
           'depth_mm': {'p50': float(np.percentile(depth, 50)) if len(depth) else 0., 'p90': float(np.percentile(depth, 90)) if len(depth) else 0.,
                        'max': float(depth.max()) if len(depth) else 0.},
           'area_deeper_1mm_cm2': float(pk['area'][pk['depth'] >= POKE_DEPTH_TOL].sum() * 1e4), 'by_part': {}}
    for i in np.unique(pk['part']):
        sel = pk['part'] == i
        out['by_part'][part_names[i]] = {'area_cm2': float(pk['area'][sel].sum() * 1e4), 'max_depth_mm': float(depth[sel].max())}
    out['note'] = ('body through armour: samples of the body surface with no armour outside (0.25 m along the body normal) but armour behind the surface '
                   '(0.12 m) that the body has come through; the armour triangle they hit has d_mm = min(vertex d_mm, -depth) in this sidecar')
    return out


class PokeCtx:
    """The reverse test for one surface (any positions U) against static Armours (cached) of the other surfaces of its level."""

    def __init__(self, scene, static=()):
        self.scene, self.static = scene, list(static)

    def measure(self, surf, U):
        sc = self.scene
        sub = np.flatnonzero(np.all((sc.P >= U.min(0) - POKE_OUT) & (sc.P <= U.max(0) + POKE_OUT), axis=1))
        pk = sc.poke(self.static + [sc.cast(*surf_tris(surf, U))], sub)
        return poke_select(pk, pk['src'] == len(self.static))


def poke_vertex_need(surf, U, pk, margin, lift=None):
    """Per welded vertex: the outward lift (and its direction) the poking samples that hit one of its triangles demand, so that
    the triangle passes `margin` outside the body at the sample (it is behind the body surface by `depth` along the body normal n).
    `lift` 'vertex' (the conservative rule): each of its three vertices moves at least depth + margin along n.
    'tri' (default): the minimum-norm lift along the triangle's own normal t (oriented to n): translating the plane by d along t
    moves its crossing with the ray by d / (n.t), so d = (depth + margin) n.t is enough where the triangle stands steeply
    across the body surface (a fin or wall inside the body is lifted by a few mm instead of its depth along n), split over the
    corners with the barycentric weights w_i of the hit (h_i = d w_i / sum w^2: sum w_i h_i = d), which leaves the far corners
    of a big triangle alone. The per-vertex maximum over all samples satisfies every sample's constraint; the fit re-measures,
    so what the linearisation misses is caught in the next round."""
    lift = lift or POKE_LIFT
    pneed, pdir = np.zeros(surf.K), np.zeros((surf.K, 3))
    if not len(pk['depth']):
        return pneed, pdir
    v = surf.Tw[pk['tri']].ravel()
    need = pk['depth'] + margin
    if lift == 'tri':
        t = surf.face_normals(U)[pk['tri']]
        c = _dot(t, pk['N'])
        t = t * np.where(c < 0, -1., 1.)[:, None]
        c = np.maximum(np.abs(c), POKE_MIN_COS)
        w = np.stack([1 - pk['uv'].sum(1), pk['uv'][:, 0], pk['uv'][:, 1]], 1)
        need = ((need * c) / np.maximum((w ** 2).sum(1), 1e-9))[:, None] * w
        need = need.ravel()
        dirs = np.repeat(t, 3, axis=0)
    else:
        need = np.repeat(need, 3)
        dirs = np.repeat(pk['N'], 3, axis=0)
    order = np.lexsort((need, v))
    last = np.r_[v[order][1:] != v[order][:-1], True]
    sel = order[last]
    pneed[v[sel]], pdir[v[sel]] = need[sel], dirs[sel]
    return pneed, pdir


def fold_poke(tri_d, pk):
    """Clearance per triangle with the body-through-armour test folded in: min(d, -depth) for the triangles that were hit."""
    out = tri_d.copy()
    if len(pk['depth']):
        np.minimum.at(out, pk['tri'], -pk['depth'])
    return out


# ----------------------------------------------------------------------------------------------- Fit A: smooth push

def smoothing_operator(surf, kappa=KAPPA):
    e = surf.edges()
    e = np.unique(e, axis=0)
    w = 1. / np.maximum(np.linalg.norm(surf.U[e[:, 0]] - surf.U[e[:, 1]], axis=1), 1e-9)
    W = sparse.coo_matrix((np.r_[w, w], (np.r_[e[:, 0], e[:, 1]], np.r_[e[:, 1], e[:, 0]])), shape=(surf.K, surf.K)).tocsr()
    rows = np.asarray(W.sum(1)).ravel()
    inv = np.where(rows > 0, 1. / (rows * (1 + kappa)), 0.)
    return sparse.diags(inv) @ W


def fit_push(surf, margin, rounds=MAX_ROUNDS, tol=TOL, kappa=KAPPA, iters=SMOOTH_ITERS, U0=None, poke_fn=None):
    """Smooth displacement field (world, per welded vertex) that lifts every vertex to `margin` from the body and, with
    `poke_fn(U)` (the reverse test: poking samples of the body, see PokeCtx.measure), every armour triangle the body comes
    through by depth + margin along the body normal at the sample (max-merged per vertex over its triangles).
    Returns (disp (K,3), info)."""
    S = smoothing_operator(surf, kappa)
    K = surf.K
    U0 = surf.U if U0 is None else U0
    disp = np.zeros((K, 3))
    act = np.zeros(K, bool)
    nrm, tgt = np.zeros((K, 3)), np.zeros(K)
    pact = np.zeros(K, bool)
    pn, pt = np.zeros((K, 3)), np.zeros(K)
    info = {'rounds': 0, 'converged': False, 'cleanup_iterations': 0}

    def state(d):
        m = measure_surface(surf, U0 + d)
        need = margin - m['d']
        pk = poke_fn(U0 + d) if poke_fn else None
        pneed, pdir = poke_vertex_need(surf, U0 + d, pk, margin) if pk is not None else (np.zeros(K), np.zeros((K, 3)))
        info['residual_mm'] = float(max(0., need.max()) * 1000)
        if pk is not None:
            info['poke_residual'] = {'area_cm2': float(pk['area'].sum() * 1e4), 'max_depth_mm': float(pk['depth'].max() * 1000) if len(pk['depth']) else 0.}
        return m, need, pk, pneed, pdir

    def clear(need, pk):
        return need.max() <= tol and (pk is None or poke_ok(pk))
    for r in range(rounds + 1):
        m, need, pk, pneed, pdir = state(disp)
        info['rounds'] = r
        if clear(need, pk):
            info['converged'] = True
            break
        if r == rounds:
            break
        viol = need > 0
        nrm[viol] = m['n'][viol]
        tgt[viol] = _dot(disp[viol], m['n'][viol]) + need[viol]
        act |= viol
        pv = pneed > 0
        pn[pv] = pdir[pv]
        pt[pv] = np.minimum(_dot(disp[pv], pdir[pv]) + pneed[pv], POKE_CAP + margin)      # a poke that does not go away must not run away
        pact |= pv
        u = disp
        for _ in range(iters):
            u = S @ u
            short = tgt[act] - _dot(u[act], nrm[act])
            raise_ = np.maximum(short, 0.)
            u[act] += raise_[:, None] * nrm[act]
            if pact.any():
                short = pt[pact] - _dot(u[pact], pn[pact])
                u[pact] += np.maximum(short, 0.)[:, None] * pn[pact]
        disp = u
    for it in range(CLEANUP_ITERS):                       # leftovers: alternating hard projections (no smoothing)
        if info['converged']:
            break
        m, need, pk, pneed, pdir = state(disp)
        if clear(need, pk):
            info['converged'] = True
            break
        viol = need > tol * .6
        pv = (pneed > 0) if (pk is not None and not poke_ok(pk) and it < POKE_CLEANUP) else np.zeros(K, bool)
        info['cleanup_iterations'] = it + 1
        if not viol.any() and not pv.any():
            info['converged'] = clear(need, pk)
            break
        disp[viol] += (need[viol] * 1.05)[:, None] * m['n'][viol]
        room = np.maximum(POKE_CAP + margin - _dot(disp[pv], pdir[pv]), 0.)
        disp[pv] += np.minimum(pneed[pv] * 1.05, room)[:, None] * pdir[pv]
    else:
        m, need, pk, pneed, pdir = state(disp)
        info['converged'] = clear(need, pk)
    return disp, info


def displacement_stats(surf, disp, U0=None):
    mag = np.linalg.norm(disp, axis=1) * 1000
    moved = mag > 0.05
    return {'vertices': int(surf.K), 'moved_vertices': int(moved.sum()), 'max_mm': float(mag.max()) if len(mag) else 0.,
            'mean_moved_mm': float(mag[moved].mean()) if moved.any() else 0., 'median_moved_mm': float(np.median(mag[moved])) if moved.any() else 0.,
            'p95_mm': float(np.percentile(mag, 95)) if len(mag) else 0.}


def bend_stats(surf, U1, flat_deg=FLAT_DEG):
    """How much the fit bent the panels: change of face normals, and of the dihedral angle across edges that were flat."""
    n0, n1 = surf.face_normals(surf.U), surf.face_normals(U1)
    ok = (np.linalg.norm(n0, axis=1) > 0) & (np.linalg.norm(n1, axis=1) > 0)
    change = np.degrees(np.arccos(np.clip(_dot(n0, n1), -1, 1)))[ok]
    area = surf.tri_area()[ok]
    out = {'face_normal_change_deg': {'max': float(change.max()) if len(change) else 0., 'p95': float(np.percentile(change, 95)) if len(change) else 0.,
                                      'area_weighted_mean': float((change * area).sum() / max(area.sum(), 1e-30)) if len(change) else 0.}}
    for deg in (20, 45, 90):
        out[f'area_pct_normal_change_over_{deg}'] = float(100 * area[change > deg].sum() / max(area.sum(), 1e-30)) if len(change) else 0.
    e = np.sort(surf.Tw[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2), axis=1)
    face = np.repeat(np.arange(len(surf.Tw)), 3)
    order = np.lexsort((e[:, 1], e[:, 0]))
    e, face = e[order], face[order]
    same = (e[1:] == e[:-1]).all(1) & (e[1:, 0] != e[1:, 1])
    pair = np.flatnonzero(same)
    f0, f1 = face[pair], face[pair + 1]
    good = ok[f0] & ok[f1] & (f0 != f1)
    f0, f1 = f0[good], f1[good]
    if len(f0):
        a0 = np.degrees(np.arccos(np.clip(_dot(n0[f0], n0[f1]), -1, 1)))
        a1 = np.degrees(np.arccos(np.clip(_dot(n1[f0], n1[f1]), -1, 1)))
        flat = a0 < flat_deg
        dd = np.abs(a1 - a0)
        out['flat_edges'] = int(flat.sum())
        out['flat_edge_bend_deg'] = {'max': float(dd[flat].max()) if flat.any() else 0., 'p95': float(np.percentile(dd[flat], 95)) if flat.any() else 0.}
        out['edge_dihedral_change_deg'] = {'max': float(dd.max()), 'p95': float(np.percentile(dd, 95))}
    return out


def recomputed_normals(surf, new_pos):
    """New NORMAL data per primitive: the original normal shifted by the change of the face normals around it (exact face
    normals for flat-shaded primitives, a first-order update for smooth ones)."""
    out = {}
    for k in range(len(surf.glb.prims)):
        n = surf.glb.normals(k)
        if n is None:
            continue
        t = surf.glb.triangles(k)
        if not len(t):
            continue
        p0, p1 = surf.glb.positions(k), new_pos[k]

        def fn(p):
            c = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
            ln = np.linalg.norm(c, axis=1, keepdims=True)
            return np.where(ln > 1e-30, c / np.maximum(ln, 1e-30), 0.)
        delta = fn(p1) - fn(p0)
        acc, cnt = np.zeros_like(n), np.zeros(len(n))
        for i in range(3):
            np.add.at(acc, t[:, i], delta)
            np.add.at(cnt, t[:, i], 1.)
        new = n + acc / np.maximum(cnt, 1)[:, None]
        ln = np.linalg.norm(new, axis=1, keepdims=True)
        out[k] = np.where(ln > 1e-9, new / np.maximum(ln, 1e-30), n)
    return out


# ----------------------------------------------------------------------------------------------- Fit B: rigid pieces

def scaled(U, pivot, axis, s):
    q = U - pivot
    along = (q @ axis)[:, None] * axis
    return pivot + along + s * (q - along)


TRUNK_PARTS = ('root.x', 'spine_01.x', 'neck.x', 'head.x')
DISTAL = {'shoulder': 'arm_stretch', 'arm_stretch': 'forearm_stretch', 'forearm_stretch': 'hand', 'thigh_stretch': 'leg_stretch', 'leg_stretch': 'foot'}


def piece_frame(surf, meas, body):
    """Covered body parts (share of the piece's vertices whose closest triangle has the part), pivot and axis.
    Limb parts win over trunk parts (a sleeve's nearest trunk vertices must not move the limb's pivot off its centreline);
    the axis is the principal axis (PCA) of the covered parts' vertices plus the next part down the limb (a stubby part's own
    PCA is its thickness, not the limb direction; the pivot stays the centroid of the covered parts), except for trunk-only pieces where the PCA is the
    shoulder width in a T-pose: those scale about the up axis."""
    counts = np.bincount(meas['part'], minlength=len(body.part_names)).astype(float)
    share = counts / max(counts.sum(), 1)
    covered = [int(i) for i in np.flatnonzero(share >= PART_SHARE)] or [int(share.argmax())]
    limbs = [i for i in covered if body.part_names[i] not in TRUNK_PARTS]
    used = limbs or covered
    verts = np.unique(body.F[np.isin(body.part, used)])
    V = body.V[verts]
    pivot = V.mean(0)
    if limbs:
        extra = []
        for i in used:
            stem, _, side = body.part_names[i].partition('.')
            nxt = f'{DISTAL[stem]}.{side}' if stem in DISTAL else None
            if nxt in body.part_names:
                extra.append(body.part_names.index(nxt))
        VA = body.V[np.unique(body.F[np.isin(body.part, used + extra)])]
        _, _, vt = np.linalg.svd(VA - VA.mean(0), full_matrices=False)
        axis, source = vt[0] / np.linalg.norm(vt[0]), 'pca'
    else:
        axis, source = np.array([0., 0., 1.]), 'up'
    return covered, used, share, pivot, axis, source


def fit_rigid(surf, margin, tol=TOL, s_max=S_MAX, ctx=None):
    """Smallest s in [1, s_max] whose perpendicular scale about the covered body parts' axis clears the margin (every vertex)
    and, with `ctx` (PokeCtx: the other pieces of the level count as cover), leaves no body poking through this piece.
    Returns (U_new, info); info['method'] is 'none' (already clear), 'scale' or 'push' (needs a Fit A fallback)."""
    body = surf.body
    m0 = measure_surface(surf)
    covered, used, share, pivot, axis, source = piece_frame(surf, m0, body)
    info = {'covered_parts': {body.part_names[i]: round(float(share[i]), 3) for i in covered}, 'frame_parts': [body.part_names[i] for i in used],
            'pivot': pivot.tolist(), 'axis': axis.tolist(), 'axis_source': source, 's': 1.0}
    if (margin - m0['d']).max() <= tol and (ctx is None or poke_ok(ctx.measure(surf, surf.U))):
        info['method'] = 'none'
        return surf.U, info

    def clears(s):
        U = scaled(surf.U, pivot, axis, s)
        return (margin - measure_surface(surf, U)['d']).max() <= tol and (ctx is None or poke_ok(ctx.measure(surf, U)))
    if not clears(s_max):
        info['method'] = 'push'
        info['s'] = None
        info['needs_review'] = f'scale {s_max} does not clear the margin or the body'
        return surf.U, info
    lo, hi = 1., s_max
    for _ in range(10):
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if clears(mid) else (mid, hi)
    info['method'], info['s'] = 'scale', float(hi)
    return scaled(surf.U, pivot, axis, hi), info


def vertex_displacement(surf, U_new):
    return U_new - surf.U


# ----------------------------------------------------------------------------------------------- module

def levels_present(module_dir, levels, source='original'):
    return [lv for lv in levels if (module_dir / 'reduced' / f'lod_error_{lv}__wearable{SOURCE_SUFFIX[source]}.glb').is_file()]


def pieces_dir(module_dir, lv, source='original'):
    """The rigid piece folder of a level (segment-first first); only a split with a chest or pelvis counts. For source `aligned` the folder
    must hold the aligned twin `lod_<lv>_aligned`."""
    for sub in ('segment_first/pieces', 'pieces'):
        d = module_dir / sub
        if (d / 'pieces.json').is_file() and (d / f'lod_{lv}{SOURCE_SUFFIX[source]}').is_dir():
            names = {p.stem for p in (d / f'lod_{lv}{SOURCE_SUFFIX[source]}').glob('*.glb')}
            if 'chest' in names or 'pelvis' in names:
                return d
    return None


def fresh(out, *inputs):
    out = Path(out)
    try:
        return out.is_file() and out.stat().st_mtime >= max(Path(i).stat().st_mtime for i in inputs)
    except OSError:
        return False


def measure_surfaces(surfs, body, margin, paths=None, write=True):
    """Vertex distances and the reverse test (body through armour) for the surfaces of one level (rigid pieces cover each
    other: a body sample is covered when any of them lies outside it). Writes `<stem>.clearance.json` for every surface in
    `paths` (d_mm per triangle = min(vertex distance, -poke depth)). Returns (meas, metrics per name, group poke metrics, pokes)."""
    names = list(surfs)
    meas = {n: measure_surface(surfs[n]) for n in names}
    scene = scene_for(body, [surfs[n].U for n in names])
    pk = scene.poke([scene.cast(*surf_tris(surfs[n], surfs[n].U)) for n in names])
    metrics = {}
    for i, n in enumerate(names):
        mine = poke_select(pk, pk['src'] == i)
        metrics[n] = summarize(meas[n], margin, body.part_names, centroid_signed(surfs[n]))
        metrics[n]['poke'] = poke_metrics(mine, body.part_names, scene.spacing)
        if write and paths:
            folded = {**meas[n], 'tri_d': fold_poke(meas[n]['tri_d'], mine)}
            write_json(Path(paths[n]).with_suffix('.clearance.json'), clearance_doc(surfs[n], folded, margin, metrics[n]), indent=None)
    return meas, metrics, poke_metrics(pk, body.part_names, scene.spacing), pk


def measure_file(glb_path, manifest, body, margin, write=True, force=False, inputs=()):
    """Measure one GLB and (re)write its clearance sidecar. Returns (surface, measurement, metrics)."""
    surf = Surface(Glb(glb_path), manifest, body)
    meas, metrics, _, _ = measure_surfaces({'glb': surf}, body, margin, {'glb': glb_path}, write)
    return surf, meas['glb'], metrics['glb']


def fit_compute(surf, margin, kind, ctx):
    """Fit one wearable / piece by Fit A (`push`) or Fit B (`rigid`). Returns (U_new, info); nothing is written."""
    t0 = time.monotonic()
    info = {}
    U_new = None
    if kind == 'rigid':
        U_new, info = fit_rigid(surf, margin, ctx=ctx)       # ctx None = method `vertex` (vertices only)
        if info['method'] == 'push':
            kind = 'push'
    if kind == 'push':
        disp, push = fit_push(surf, margin, poke_fn=(lambda U: ctx.measure(surf, U)) if ctx is not None else None)
        U_new = surf.U + disp
        info.update({'method': 'push', 'push': push})
        if not (info.get('needs_review') or push['converged']):
            info['needs_review'] = f'push did not converge ({push["residual_mm"]:.2f} mm short, poke {push.get("poke_residual", {}).get("area_cm2", 0):.1f} cm2)'
    info['seconds_fit'] = round(time.monotonic() - t0, 2)
    return U_new, info


def fit_write(surf, U_new, info, out_path):
    """Write the fitted GLB for new welded vertex positions U_new and add displacement / bend statistics to `info`."""
    t0 = time.monotonic()
    disp = vertex_displacement(surf, U_new)
    info['displacement'] = displacement_stats(surf, disp)
    if info['displacement']['max_mm'] > LARGE_PUSH_MM:
        info.setdefault('warnings', []).append(f'large displacement {info["displacement"]["max_mm"]:.0f} mm')
    info['bend'] = bend_stats(surf, U_new)
    new_pos = surf.apply(disp)
    data = surf.glb.patched(new_pos, recomputed_normals(surf, new_pos))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    write_atomic(out_path, data)
    info['seconds'] = round(info.get('seconds_fit', 0.) + time.monotonic() - t0, 2)
    return info


def fit_file(surf, meas, margin, kind, out_path, body, manifest, ctx=None, method='full'):
    """Fit one wearable / piece by Fit A (`push`) or Fit B (`rigid`), write the fitted GLB and return the info.
    `ctx` = PokeCtx for the reverse test (default: the surface alone); method `vertex` fits the vertices only (no reverse test)."""
    if method == 'full':
        ctx = ctx or PokeCtx(scene_for(body, [surf.U]))
    else:
        ctx = None
    U_new, info = fit_compute(surf, margin, kind, ctx)
    return fit_write(surf, U_new, info, out_path)


def gap_metrics(originals, fitted):
    """Largest distance between vertices of different pieces that coincided before the fit (the cut between two pieces)."""
    names = list(originals)
    allp, owner = [], []
    for i, n in enumerate(names):
        allp.append(originals[n].Wc)
        owner.append(np.full(len(originals[n].Wc), i))
    P, own = np.concatenate(allp), np.concatenate(owner)
    F = np.concatenate([fitted[n].Wc for n in names])
    pairs = cKDTree(P).query_pairs(1e-4, output_type='ndarray')
    if not len(pairs):
        return {'coincident_pairs': 0, 'max_gap_mm': 0., 'by_pair': {}}
    pairs = pairs[own[pairs[:, 0]] != own[pairs[:, 1]]]
    if not len(pairs):
        return {'coincident_pairs': 0, 'max_gap_mm': 0., 'by_pair': {}}
    # the fitted surfaces keep their original corner order; fitted world positions come from the written files (see process_level)
    gap = np.linalg.norm(F[pairs[:, 0]] - F[pairs[:, 1]], axis=1) * 1000
    by = {}
    for (a, b), g in zip(pairs, gap):
        key = '/'.join(sorted((names[own[a]], names[own[b]])))
        by[key] = max(by.get(key, 0.), float(g))
    return {'coincident_pairs': int(len(pairs)), 'max_gap_mm': float(gap.max()), 'by_pair': {k: round(v, 3) for k, v in sorted(by.items())}}


def poke_flag(after):
    """needs_review text when the body still comes through after the fit (beyond the tolerance), else None."""
    pk = after.get('poke') or {}
    if pk.get('area_cm2', 0) * 1e-4 >= POKE_AREA_TOL and pk.get('depth_mm', {}).get('max', 0) >= POKE_DEPTH_TOL * 1000:
        return f'body still pokes through: {pk["area_cm2"]:.1f} cm2, max {pk["depth_mm"]["max"]:.1f} mm'
    return None


def process_level(module_dir, manifest, body, lv, margin, measure_only, force, prev, tol_inputs, method='full', source='original'):
    """One level of a module: wearable and pieces. Returns the level record."""
    reduced = module_dir / 'reduced'
    rec = {}
    ssuf = SOURCE_SUFFIX[source]
    src = reduced / f'lod_error_{lv}__wearable{ssuf}.glb'
    out = reduced / f'lod_error_{lv}__wearable{ssuf}{SUFFIX[method]}.glb'
    ins = (src, *tol_inputs)
    t0 = time.monotonic()
    surf, meas, before = measure_file(src, manifest, body, margin)
    rec['wearable'] = {'source': rel(src), 'before': before, 'seconds_measure': round(time.monotonic() - t0, 2)}
    if not measure_only:
        old = ((prev or {}).get('levels') or {}).get(lv, {}).get('wearable')
        if not force and fresh(out, *ins) and fresh(out.with_suffix('.clearance.json'), out) and old and old.get('fitted'):
            rec['wearable'].update({k: old[k] for k in old if k not in rec['wearable']})
            rec['wearable']['reused'] = True
        else:
            info = fit_file(surf, meas, margin, 'push', out, body, manifest, method=method)
            fsurf, fmeas, after = measure_file(out, manifest, body, margin)
            rec['wearable'].update({'fitted': rel(out), 'fit_algorithm': method, 'after': after, **info})
            if after['min_d_mm'] < (margin - 2 * TOL) * 1000:
                rec['wearable'].setdefault('warnings', []).append(f'not cleared: min d {after["min_d_mm"]:.2f} mm')
            flag = poke_flag(after) if method == 'full' else None       # method `vertex` does not try to clear the body
            if flag:
                rec['wearable'].setdefault('warnings', []).append(flag)
    pdir = pieces_dir(module_dir, lv, source)
    if pdir is not None:
        rec['pieces'] = process_pieces(module_dir, pdir, manifest, body, lv, margin, measure_only, force, prev, tol_inputs, method, source)
    return rec


def process_pieces(module_dir, pdir, manifest, body, lv, margin, measure_only, force, prev, tol_inputs, method='full', source='original'):
    ssuf = SOURCE_SUFFIX[source]
    src_dir, out_dir = pdir / f'lod_{lv}{ssuf}', pdir / f'lod_{lv}{ssuf}{SUFFIX[method]}'
    old_rec = (((prev or {}).get('levels') or {}).get(lv, {}).get('pieces') or {})
    old_pieces = old_rec.get('table', {})
    paths = {p.stem: p for p in sorted(src_dir.glob('*.glb'))}
    surfs = {n: Surface(Glb(p), manifest, body) for n, p in paths.items()}
    meas, before, gbefore, _ = measure_surfaces(surfs, body, margin, paths)
    table = {n: {'source': rel(paths[n]), 'before': before[n]} for n in paths}
    rec = {'dir': rel(src_dir), 'fitted_dir': rel(out_dir), 'fit_algorithm': method, 'table': table, 'poke_before': gbefore}
    if measure_only or not paths:
        return rec
    outs = {n: out_dir / paths[n].name for n in paths}
    if not force and all(fresh(outs[n], paths[n], *tol_inputs) and fresh(outs[n].with_suffix('.clearance.json'), outs[n]) and old_pieces.get(n, {}).get('fitted') for n in paths):
        for n in paths:
            table[n].update({k: v for k, v in old_pieces[n].items() if k not in table[n]})
            table[n]['reused'] = True
        rec.update({k: old_rec[k] for k in ('gaps', 'poke_after', 'poke_passes') if k in old_rec})
        return rec
    scene = scene_for(body, [s.U for s in surfs.values()])
    names = list(surfs)
    U_new, infos = {}, {}
    passes = 0
    if method == 'vertex':                                      # vertices only: every piece alone, no reverse test
        for n, s in surfs.items():
            U_new[n], infos[n] = fit_compute(s, margin, 'rigid', None)
    else:
        casts0 = {n: scene.cast(*surf_tris(s, s.U), cache=True) for n, s in surfs.items()}
        for n, s in surfs.items():                              # Fit B per piece; the other pieces (unfitted) are the cover
            ctx = PokeCtx(scene, [casts0[m] for m in surfs if m != n])
            U_new[n], infos[n] = fit_compute(s, margin, 'rigid', ctx)
        cur = {n: scene.cast(*surf_tris(s, U_new[n]), cache=True) for n, s in surfs.items()}
        for passes in range(POKE_PASSES + 1):                   # level pass: pokes left once all pieces moved (cuts between pieces ...)
            pk = scene.poke([cur[n] for n in names])
            todo = [n for i, n in enumerate(names) if not poke_ok(poke_select(pk, pk['src'] == i))]
            if not todo or passes == POKE_PASSES:
                break
            for n in todo:
                ctx = PokeCtx(scene, [cur[m] for m in names if m != n])
                s = surfs[n]
                disp, push = fit_push(s, margin, U0=U_new[n], poke_fn=lambda U, s=s, ctx=ctx: ctx.measure(s, U))
                U_new[n] = U_new[n] + disp
                cur[n] = scene.cast(*surf_tris(s, U_new[n]), cache=True)
                infos[n].setdefault('level_pass', []).append({'pass': passes + 1, 'push': push, 'max_mm': float(np.linalg.norm(disp, axis=1).max() * 1000)})
    rec['poke_passes'] = passes
    for n, s in surfs.items():
        fit_write(s, U_new[n], infos[n], outs[n])
    fits = {n: Surface(Glb(outs[n]), manifest, body) for n in names}
    fmeas, after, gafter, _ = measure_surfaces(fits, body, margin, outs)
    for n in names:
        table[n].update({'fitted': rel(outs[n]), 'fit_algorithm': method, 'after': after[n], **infos[n]})
        if after[n]['min_d_mm'] < (margin - 2 * TOL) * 1000:
            table[n]['needs_review'] = table[n].get('needs_review') or f'not cleared: min d {after[n]["min_d_mm"]:.2f} mm'
    rec['poke_after'] = gafter
    flag = poke_flag({'poke': gafter}) if method == 'full' else None
    if flag:
        rec['warnings'] = [flag]
    rec['gaps'] = gap_metrics(surfs, fits)
    return rec


def module_summary(rec):
    """Rows for the summary table from a module record."""
    rows = []
    for lv, lr in rec.get('levels', {}).items():
        w = lr['wearable']
        b, a = w['before'], w.get('after')
        flags = list(w.get('warnings', []))
        if w.get('needs_review'):
            flags.append('needs_review')
        row = {'module': rec['module'], 'level': lv, 'part': 'wearable', 'before_pen_pct': b['penetrating']['area_pct'], 'before_margin_pct': b['within_margin']['area_pct'],
               'before_max_mm': b['max_depth_mm'], 'method': 'push' if a else None, 'flags': flags,
               'before_poke_cm2': b['poke']['area_cm2'], 'before_poke_max_mm': b['poke']['depth_mm']['max']}
        if a:
            row.update({'after_pen_pct': a['penetrating']['area_pct'], 'after_margin_pct': a['within_margin']['area_pct'], 'after_max_mm': a['max_depth_mm'],
                        'max_displacement_mm': w['displacement']['max_mm'], 'after_poke_cm2': a['poke']['area_cm2'], 'after_poke_max_mm': a['poke']['depth_mm']['max']})
        rows.append(row)
        pt = (lr.get('pieces') or {}).get('table')
        if pt:
            area = sum(p['before']['area_m2'] for p in pt.values())
            bpen = sum(p['before']['penetrating']['area_m2'] for p in pt.values())
            bmar = sum(p['before']['within_margin']['area_m2'] for p in pt.values())
            prow = {'module': rec['module'], 'level': lv, 'part': 'pieces', 'before_pen_pct': 100 * bpen / area, 'before_margin_pct': 100 * bmar / area,
                    'before_max_mm': max(p['before']['max_depth_mm'] for p in pt.values()), 'method': None, 'flags': [],
                    'before_poke_cm2': lr['pieces']['poke_before']['area_cm2'], 'before_poke_max_mm': lr['pieces']['poke_before']['depth_mm']['max']}
            if all('after' in p for p in pt.values()):
                apen = sum(p['after']['penetrating']['area_m2'] for p in pt.values())
                amar = sum(p['after']['within_margin']['area_m2'] for p in pt.values())
                methods = [p.get('method') for p in pt.values()]
                prow.update({'after_pen_pct': 100 * apen / area, 'after_margin_pct': 100 * amar / area,
                             'after_max_mm': max(p['after']['max_depth_mm'] for p in pt.values()),
                             'method': 'scale %d/push %d/none %d' % (methods.count('scale'), methods.count('push'), methods.count('none')),
                             'max_scale': max([p.get('s') or 1. for p in pt.values()]),
                             'max_displacement_mm': max(p['displacement']['max_mm'] for p in pt.values())})
                prow.update({'after_poke_cm2': lr['pieces']['poke_after']['area_cm2'], 'after_poke_max_mm': lr['pieces']['poke_after']['depth_mm']['max']})
                prow['flags'] = ['needs_review'] if any(p.get('needs_review') for p in pt.values()) else []
                prow['flags'] += lr['pieces'].get('warnings', [])
                gaps = (lr['pieces'].get('gaps') or {}).get('max_gap_mm')
                if gaps is not None:
                    prow['max_gap_mm'] = gaps
            rows.append(prow)
    return rows


def compare_to_current(module_dir, rec):
    """For an aligned fit: poke area and displacement of the aligned(+fitted) wearable / pieces next to the current fitted outputs
    (fit/body_fit.json of the same method), per level."""
    try:
        cur = json.loads((module_dir / 'fit' / ('body_fit.json' if rec['fit_algorithm'] == 'full' else f'body_fit_{rec["fit_algorithm"]}.json')).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {'note': 'no current fit record'}
    out = {}
    for lv, lr in rec['levels'].items():
        cl = (cur.get('levels') or {}).get(lv)
        if not cl:
            continue
        row = {}
        w, cw = lr['wearable'], cl['wearable']
        if w.get('after') and cw.get('after'):
            row['wearable'] = {k: {'poke_before_cm2': x['before']['poke']['area_cm2'], 'poke_after_cm2': x['after']['poke']['area_cm2'], 'poke_after_max_mm': x['after']['poke']['depth_mm']['max'],
                                   'penetrating_before_pct': x['before']['penetrating']['area_pct'], 'displacement': x['displacement']}
                               for k, x in (('current', cw), ('aligned', w))}
        pt, cpt = (lr.get('pieces') or {}).get('table'), (cl.get('pieces') or {}).get('table')
        if pt and cpt and all('after' in p for p in pt.values()) and all('after' in p for p in cpt.values()):
            row['pieces'] = {k: {'poke_before_cm2': g['pieces']['poke_before']['area_cm2'], 'poke_after_cm2': g['pieces']['poke_after']['area_cm2'],
                                 'max_displacement_mm': max(p['displacement']['max_mm'] for p in t.values()),
                                 'median_displacement_mm': float(np.median([p['displacement']['median_moved_mm'] for p in t.values() if 'median_moved_mm' in p['displacement']] or [0.])),
                                 'max_gap_mm': (g['pieces'].get('gaps') or {}).get('max_gap_mm')}
                             for k, g, t in (('current', cl, cpt), ('aligned', lr, pt))}
        out[lv] = row
    return out


def process_module(module_dir, levels, margin_pct, measure_only=False, force=False, method='full', source='original'):
    module_dir = Path(module_dir).resolve()
    name = module_dir.relative_to(ROOT / 'models').as_posix() if module_dir.is_relative_to(ROOT / 'models') else str(module_dir)
    res = {'module': name, 'status': 'done'}
    manifest_path = module_dir / 'reduced' / 'manifest.json'
    if not manifest_path.is_file():
        return {**res, 'status': 'skipped', 'why': 'no reduced/manifest.json'}
    if module_busy(module_dir):
        return {**res, 'status': 'skipped', 'why': 'a conversion worker holds this module'}
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    if manifest.get('module') not in MODULES:
        return {**res, 'status': 'skipped', 'why': f'module {manifest.get("module")!r}'}
    if not ((manifest.get('parts') or {}).get('fit') or {}).get('scale'):
        return {**res, 'status': 'skipped', 'why': 'manifest has no base fit (parts.fit)'}
    _, _, race, _ = identity(module_dir)
    if not (BASE_CACHE / f'{race}.npz').is_file():
        return {**res, 'status': 'skipped', 'why': f'no cached base body for {race} (run export_bases.py)'}
    lvs = levels_present(module_dir, levels, source)
    if not lvs:
        return {**res, 'status': 'skipped', 'why': 'no wearable GLB' if source == 'original' else 'no aligned wearable GLB (run align_body.py)'}
    t0 = time.monotonic()
    body = get_body(race)
    margin = margin_pct / 100 * body.H
    stem = 'body_fit' + SOURCE_SUFFIX[source] + ('' if method == 'full' else f'_{method}')
    fit_json = module_dir / 'fit' / f'{stem}.json'
    prev = None
    try:
        prev = json.loads(fit_json.read_text(encoding='utf-8-sig'))
        if prev.get('params', {}).get('margin_pct') != margin_pct or prev.get('code_version') != CODE_VERSION:
            prev = None
    except (OSError, ValueError):
        pass
    inputs = (manifest_path, BASE_CACHE / f'{race}.npz')
    rec = {'module': name, 'race': race, 'fit_algorithm': method, 'source': source, 'code_version': CODE_VERSION, 'body_height_m': body.H, 'params': {'margin_pct': margin_pct, 'margin_mm': margin * 1000, 'tolerance_mm': TOL * 1000,
                                                                                'rounds': MAX_ROUNDS, 's_max': S_MAX, 'kappa': KAPPA, 'smooth_iters': SMOOTH_ITERS,
                                                                                'poke': {'spacing_pct': POKE_SPACING_PCT, 'out_m': POKE_OUT, 'in_m': POKE_IN, 'depth_tol_mm': POKE_DEPTH_TOL * 1000,
                                                                                         'area_tol_cm2': POKE_AREA_TOL * 1e4, 'level_passes': POKE_PASSES}},
           'levels': {}, 'warnings': []}
    for lv in lvs:
        try:
            rec['levels'][lv] = process_level(module_dir, manifest, body, lv, margin, measure_only, force, prev, inputs, method, source)
        except Exception as error:  # noqa: BLE001 - one level must not stop the batch
            rec['warnings'].append(f'{lv}: {type(error).__name__}: {error}')
    rec['seconds'] = round(time.monotonic() - t0, 1)
    rec['inputs'] = {rel(p): datetime.datetime.fromtimestamp(Path(p).stat().st_mtime).isoformat(timespec='seconds') for p in inputs}
    if source == 'aligned' and not measure_only:
        rec['compare_to_current'] = compare_to_current(module_dir, rec)
    if not measure_only:
        (module_dir / 'fit').mkdir(exist_ok=True)
        write_json(fit_json, rec)
    res.update({'record': rec, 'rows': module_summary(rec), 'seconds': rec['seconds']})
    if rec['warnings']:
        res['status'] = 'warnings'
    return res


# ----------------------------------------------------------------------------------------------- race check

def race_check():
    """Concept base (base_views/<race>/parts.npz, normalised frame) vs the game base, per race."""
    rows = []
    for npz in sorted(BASE_CACHE.glob('*.npz')):
        race = npz.stem
        pp = ROOT / 'base_views' / race / 'parts.npz'
        if not pp.is_file():
            rows.append({'race': race, 'note': 'no base_views parts.npz'})
            continue
        body = get_body(race)
        z = np.load(pp, allow_pickle=False)
        cv = z['v'].astype(np.float64) * body.H / 2 + body.o
        row = {'race': race, 'game_vertices': int(len(body.V)), 'concept_vertices': int(len(cv)), 'H_game': body.H}
        if len(cv) == len(body.V):
            dv = np.linalg.norm(cv - body.V, axis=1)
            row['vertex_match_max_mm'] = float(dv.max() * 1000)
            row['same_topology'] = bool(len(z['f']) == len(body.F) and (np.asarray(z['f']) == np.asarray(body.F)).all()) if len(z['f']) == len(body.F) else False
        dist = body.closest(cv)[0]
        row['concept_to_game_surface_mm'] = {'max': float(dist.max() * 1000), 'mean': float(dist.mean() * 1000), 'p95': float(np.percentile(dist, 95) * 1000)}
        cb = Body(cv, z['f'], z['part'], z['part_names'])
        back = cb.closest(body.V)[0]
        row['game_to_concept_surface_mm'] = {'max': float(back.max() * 1000), 'mean': float(back.mean() * 1000), 'p95': float(np.percentile(back, 95) * 1000)}
        row['concept_bbox_h_m'] = float(cv[:, 2].max() - cv[:, 2].min())
        row['game_bbox_h_m'] = float(body.hi[2] - body.lo[2])
        parts = {}
        pn = [str(n) for n in z['part_names']]
        for i, n in enumerate(pn):
            sel = np.unique(np.asarray(z['f'])[np.asarray(z['part']) == i])
            if len(sel):
                parts[n] = float(body.closest(cv[sel])[0].max() * 1000)
        row['worst_parts_mm'] = dict(sorted(parts.items(), key=lambda kv: -kv[1])[:4])
        rows.append(row)
    return rows


# ----------------------------------------------------------------------------------------------- CLI

def write_summary(path, results, args):
    rows = [r for res in results.values() for r in res.get('rows', [])]
    rows.sort(key=lambda r: (-r['before_pen_pct'], -r['before_max_mm']))
    summary = {'date': datetime.datetime.now().isoformat(timespec='seconds'), 'margin_pct': args.margin_pct, 'measure_only': args.measure_only, 'fit_algorithm': args.method,
               'modules': {k: {'status': r['status'], 'why': r.get('why'), 'seconds': r.get('seconds')} for k, r in results.items()}, 'rows': rows}
    write_json(path, summary)
    md = ['| module | level | part | before pen % | before max mm | before <margin % | after pen % | after max mm | after <margin % | poke cm2 before | poke cm2 after | poke max mm after | method | flags |',
          '|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|']
    f = lambda x, n=2: '' if x is None else f'{x:.{n}f}'
    for r in rows:
        md.append(f'| {r["module"]} | {r["level"]} | {r["part"]} | {f(r["before_pen_pct"])} | {f(r["before_max_mm"], 1)} | {f(r["before_margin_pct"])} | '
                  f'{f(r.get("after_pen_pct"))} | {f(r.get("after_max_mm"), 1)} | {f(r.get("after_margin_pct"))} | {f(r.get("before_poke_cm2"), 1)} | '
                  f'{f(r.get("after_poke_cm2"), 1)} | {f(r.get("after_poke_max_mm"), 1)} | {r.get("method") or ""} | {", ".join(r["flags"])} |')
    write_atomic(Path(path).with_suffix('.md'), '\n'.join(md) + '\n')


def _one(work):
    global INSIDE_TEST
    m, levels, margin_pct, measure_only, force, method, source, INSIDE_TEST = work
    try:
        r = process_module(m, levels, margin_pct, measure_only, force, method, source)
    except Exception as error:  # noqa: BLE001
        r = {'module': str(m), 'status': 'failed', 'why': f'{type(error).__name__}: {error}'}
    worst = max((x['before_max_mm'] for x in r.get('rows', [])), default=0)
    print(f'BODY_FIT {r["status"]:8} {r["module"]} {r.get("seconds", "")} max_before={worst:.1f}mm {r.get("why") or ""}', flush=True)
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('modules', nargs='*')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--match')
    ap.add_argument('--levels', default=','.join(LEVELS))
    ap.add_argument('--margin-pct', type=float, default=0.4)
    ap.add_argument('--measure-only', action='store_true')
    ap.add_argument('--jobs', type=int, default=1)
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--method', choices=sorted(SUFFIX), default='full')
    ap.add_argument('--source', choices=sorted(SOURCE_SUFFIX), default='original', help='fit the converted wearable (default) or the one align_body.py wrote')
    ap.add_argument('--inside-test', choices=('pseudonormal', 'winding'), default=INSIDE_TEST, help='sign of the body distance: pseudonormal of the closest feature (default, fast) or the generalized winding number (reference)')
    ap.add_argument('--summary')
    ap.add_argument('--race-check', action='store_true')
    a = ap.parse_args(argv)
    if a.race_check:
        print(json.dumps(race_check(), indent=1))
        return
    mods = [Path(m) for m in a.modules]
    if a.all:
        mods += discover()
    mods = [m if m.is_absolute() else ROOT / m for m in mods]
    if a.match:
        mods = [m for m in mods if a.match in str(m)]
    if not mods:
        raise SystemExit('no modules (give module folders or --all)')
    levels = tuple(a.levels.split(','))
    summary = Path(a.summary) if a.summary else ROOT / 'experiments' / f'body_fit_{datetime.date.today():%Y%m%d}' / ('summary.json' if a.source == 'original' else f'summary_{a.source}.json')
    summary.parent.mkdir(parents=True, exist_ok=True)
    results = {}

    work = [(m, levels, a.margin_pct, a.measure_only, a.force, a.method, a.source, a.inside_test) for m in mods]
    if a.jobs > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=a.jobs) as pool:
            for r in pool.map(_one, work):
                results[r['module']] = r
    else:
        for w in work:
            r = _one(w)
            results[r['module']] = r
    write_summary(summary, results, a)
    counts = {s: sum(r['status'] == s for r in results.values()) for s in sorted({r['status'] for r in results.values()})}
    print(f'BODY_FIT_SUMMARY {len(results)} modules: {json.dumps(counts)} -> {summary}')
    if counts.get('failed'):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
