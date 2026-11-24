"""Visibility of a finished mesh from outside, without Blender (2026-11-23): a small numpy z-buffer.

The prototype viewer paints back faces red; a back face seen from outside is a hole, an open garment end or a wrongly wound face. This module renders
the mesh orthographically from many directions and reports, per face, how many pixels show its front and its back, plus see-through pixels
(background enclosed by the silhouette). Used by geodiv.seal (repairs) and as a quick score of a result.
"""
import importlib.util
import os
import sys
from pathlib import Path

import numpy as np
from scipy import ndimage


def view_dirs(rings=(-60, -30, 0, 30, 60), per_ring=8, poles=True):
    """Unit view directions (camera looks along them): rings of elevations (deg) x azimuths, plus straight up / down."""
    D = []
    for el in rings:
        n = per_ring if abs(el) < 75 else max(per_ring // 2, 3)
        for k in range(n):
            az = 2 * np.pi * (k + (0.5 if (rings.index(el) % 2) else 0.)) / n
            e = np.radians(el)
            D.append(-np.array([np.sin(az) * np.cos(e), -np.cos(az) * np.cos(e), np.sin(e)]))   # camera at that place, looking at the centre
    if poles:
        D += [np.array([0., 0., -1.]), np.array([0., 0., 1.])]
    return np.array(D)


_GPU_ENV = os.environ.get('BODY_FIT_GPU', '').strip().lower()      # see tools/tripo_pipeline/body_fit.py: '1' / 'all' or a list incl. raster
_GPU_ON = _GPU_ENV in ('1', 'all', 'true', 'yes') or 'raster' in _GPU_ENV.split(',')
_gpu_mod = None


def _gpu():
    """tools/tripo_pipeline/gpu_geometry.py, the same module object (and GPU context) body_fit uses."""
    global _gpu_mod
    if _gpu_mod is None:
        _gpu_mod = sys.modules.get('gpu_geometry')
        if _gpu_mod is None:
            path = Path(__file__).resolve().parents[2] / 'tools' / 'tripo_pipeline' / 'gpu_geometry.py'
            spec = importlib.util.spec_from_file_location('gpu_geometry', path)
            _gpu_mod = importlib.util.module_from_spec(spec)
            sys.modules['gpu_geometry'] = _gpu_mod
            spec.loader.exec_module(_gpu_mod)
    return _gpu_mod


def _gpu_failed(e):
    global _GPU_ON
    _GPU_ON = False
    print(f'visibility: GPU z-buffer failed ({type(e).__name__}: {e}); CPU from now on in this process', file=sys.stderr, flush=True)


def _frame(d):
    d = d / np.linalg.norm(d)
    up = np.array([0., 0., 1.]) if abs(d[2]) < 0.95 else np.array([0., 1., 0.])
    r = np.cross(d, up)
    r /= np.linalg.norm(r)
    u = np.cross(r, d)
    return d, r, u


def zbuffer(V, F, d, px, lo=None, hi=None, max_px=4_000_000, zmin=None):
    """Orthographic z-buffer along direction d (pixel size px, metres). Returns (face id per pixel, -1 = background; image origin; (r, u))."""
    d, r, u = _frame(d)
    X, Y, Z = V @ r, V @ u, V @ d
    if lo is None:
        lo = np.array([X.min(), Y.min()]) - 2 * px
        hi = np.array([X.max(), Y.max()]) + 2 * px
    W = int(np.ceil((hi[0] - lo[0]) / px))
    H = int(np.ceil((hi[1] - lo[1]) / px))
    x, y = (X - lo[0]) / px, (Y - lo[1]) / px
    if _GPU_ON and len(F) and W > 0 and H > 0 and W <= 16384 and H <= 16384:        # BODY_FIT_GPU=1: rasterised on the GPU
        try:
            return _gpu().raster_ids(x, y, Z, F, W, H, znear=zmin), lo, (d, r, u)
        except Exception as e:  # noqa: BLE001
            _gpu_failed(e)
    tx, ty, tz = x[F], y[F], Z[F]
    x0 = np.clip(np.ceil(tx.min(1) - .5).astype(np.int64), 0, W - 1)
    x1 = np.clip(np.floor(tx.max(1) - .5).astype(np.int64), 0, W - 1)
    y0 = np.clip(np.ceil(ty.min(1) - .5).astype(np.int64), 0, H - 1)
    y1 = np.clip(np.floor(ty.max(1) - .5).astype(np.int64), 0, H - 1)
    nx, ny = np.maximum(x1 - x0 + 1, 0), np.maximum(y1 - y0 + 1, 0)
    cnt = nx * ny
    best = np.full(W * H, np.inf)
    fid = np.full(W * H, -1, np.int64)
    # chunks of faces so the expanded pixel list stays bounded
    order = np.arange(len(F))
    start = 0
    cum = np.cumsum(cnt)
    while start < len(F):
        base = cum[start - 1] if start else 0
        end = int(np.searchsorted(cum, base + max_px, side='right'))
        end = max(end, start + 1)
        fs = order[start:end]
        c = cnt[fs]
        tot = int(c.sum())
        if tot:
            fi = np.repeat(fs, c)
            k = np.arange(tot) - np.repeat(np.cumsum(c) - c, c)
            px_ = x0[fi] + k % nx[fi]
            py_ = y0[fi] + k // nx[fi]
            cx, cy = px_ + .5, py_ + .5
            ax, ay = tx[fi, 0], ty[fi, 0]
            bx, by = tx[fi, 1] - ax, ty[fi, 1] - ay
            qx, qy = tx[fi, 2] - ax, ty[fi, 2] - ay
            den = bx * qy - by * qx
            ok = np.abs(den) > 1e-12
            den = np.where(ok, den, 1.)
            wx, wy = cx - ax, cy - ay
            b1 = (wx * qy - wy * qx) / den
            b2 = (bx * wy - by * wx) / den
            b0 = 1 - b1 - b2
            ins = ok & (b0 >= -1e-6) & (b1 >= -1e-6) & (b2 >= -1e-6)
            if zmin is not None:                         # near clipping plane (close-ups): fragments nearer than zmin are skipped
                zz = b0 * tz[fi, 0] + b1 * tz[fi, 1] + b2 * tz[fi, 2]
                ins &= zz >= zmin
            if ins.any():
                fi, px_, py_ = fi[ins], px_[ins], py_[ins]
                z = b0[ins] * tz[fi, 0] + b1[ins] * tz[fi, 1] + b2[ins] * tz[fi, 2]
                pix = py_ * W + px_
                o = np.lexsort((z, pix))
                pix, z, fi = pix[o], z[o], fi[o]
                first = np.r_[True, pix[1:] != pix[:-1]]
                pix, z, fi = pix[first], z[first], fi[first]
                better = z < best[pix]
                best[pix[better]] = z[better]
                fid[pix[better]] = fi[better]
        start = end
    return fid.reshape(H, W), lo, (d, r, u)


def face_normals(V, F):
    N = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    return N / np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-18)


def visibility(V, F, px=0.004, dirs=None, see_through=True, min_hole_px=3):
    """Per face: pixels showing its front / its back over all views; see-through pixel total and per-view masks' centroids (3D points
    approximated from the ring of faces around each enclosed background region). Returns dict."""
    V = np.asarray(V, np.float64)
    F = np.asarray(F, np.int64)
    dirs = view_dirs() if dirs is None else dirs
    N = face_normals(V, F)
    front = np.zeros(len(F), np.int64)
    back = np.zeros(len(F), np.int64)
    holes = []
    for vi, d in enumerate(dirs):
        img, lo, (dd, r, u) = zbuffer(V, F, d, px)
        fg = img >= 0
        f = img[fg]
        facing = N[f] @ dd                      # > 0: the face's back is towards the camera
        np.add.at(front, f[facing <= 0], 1)
        np.add.at(back, f[facing > 0], 1)
        if see_through:
            lab, nb = ndimage.label(~fg)
            if nb:
                border = set(np.unique(np.r_[lab[0], lab[-1], lab[:, 0], lab[:, -1]])) - {0}
                for k in range(1, nb + 1):
                    if k in border:
                        continue
                    m = lab == k
                    if m.sum() < min_hole_px:
                        continue
                    ring = ndimage.binary_dilation(m, iterations=2) & fg
                    rf = np.unique(img[ring])
                    holes.append({'view': vi, 'px': int(m.sum()), 'faces': rf})
    return {'front': front, 'back': back, 'holes': holes, 'dirs': dirs, 'px': px}


def score(V, F, px=0.004, dirs=None):
    """(back-seen pixels, see-through pixels) over all views: a quick quality number for a result (lower is better)."""
    v = visibility(V, F, px, dirs)
    return int(v['back'].sum()), int(sum(h['px'] for h in v['holes']))
