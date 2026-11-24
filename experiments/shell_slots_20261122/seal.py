"""Seal pass for a finished result (2026-11-23): close the holes the viewer shows, without moving anything that is already there.

User: "the gnomes still have a bunch of holes and back faces showing ... we could cover those holes and use whatever material is nearby ... better
if we fix them properly." The stitching leaves gaps wherever the game skin was cut back further than the garment covers it (elbow / armpit seams,
garment edges that end inside the body, skin strips lost in big gnome triangles). Through such a gap the viewer looks into the body and sees the
inside of the skin on the far side (red), or the inside of the garment.

underlay(): the GAME BODY's own surface is put back where it closes such a hole. Candidates are the game body's triangles (subdivided where needed);
every candidate is rasterised with the result from many directions (visibility.zbuffer). A candidate fragment that lies in front of what the result
shows at that pixel is
  good  when the result shows a back face there (inside of the body / of a garment) or nothing (the body is always covered by something), and
  bad   when the result shows a front face there (the candidate would poke through the garment or skin).
Candidates with good pixels and no bad pixels are added (prov 'underlay', role skin, their body part). Candidates with both are split into four and
tested again, down to UNDERLAY_MIN_MM; nothing that would cover a visible garment is ever added. Candidates behind what is visible change nothing
and are left out (they would only add triangles).
"""
import numpy as np

import visibility as vis

UNDERLAY_MIN_MM = 12.0     # candidates are split down to edges about this long where a hole meets visible garment
UNDERLAY_EPS_MM = 2.0      # a candidate fragment must be this much in front of the result's surface to count
UNDERLAY_PX_MM = 4.0       # z-buffer pixel size
UNDERLAY_BAD_FRAC = 0.0    # a candidate is accepted with bad pixels up to this fraction of its good pixels
UNDERLAY_GOOD_MIN = 2      # ... and at least this many good pixels over all views
GRAZE_COS = 0.3            # pixels where the candidate or the result face is seen this edge-on (|cos|) are not counted


def _subdivide(V, F):
    """Each triangle into 4 (midpoints); returns (V2, F2, parent). Shared edges split identically (conforming)."""
    E = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1)
    uE, inv = np.unique(E, axis=0, return_inverse=True)
    inv = inv.ravel()
    mid = len(V) + np.arange(len(uE))
    V2 = np.concatenate([V, (V[uE[:, 0]] + V[uE[:, 1]]) / 2])
    m = mid[inv].reshape(3, -1).T          # midpoints of edges (01, 12, 20) per face
    a, b, c = F[:, 0], F[:, 1], F[:, 2]
    m01, m12, m20 = m[:, 0], m[:, 1], m[:, 2]
    F2 = np.concatenate([np.stack([a, m01, m20], 1), np.stack([m01, b, m12], 1), np.stack([m20, m12, c], 1), np.stack([m01, m12, m20], 1)])
    parent = np.tile(np.arange(len(F)), 4)
    return V2, F2, parent


def _classify(V, F, Vc, Fc, dirs, px, eps):
    """good / bad pixel counts per candidate face (Vc, Fc) against the result (V, F)."""
    N = vis.face_normals(V, F)
    good = np.zeros(len(Fc), np.int64)
    bad = np.zeros(len(Fc), np.int64)
    allV = np.concatenate([V, Vc])
    from scipy import ndimage
    for d in dirs:
        dd, r, u = vis._frame(d)
        X, Y = allV @ r, allV @ u
        lo = np.array([X.min(), Y.min()]) - 2 * px
        hi = np.array([X.max(), Y.max()]) + 2 * px
        img, _, _ = vis.zbuffer(V, F, d, px, lo=lo, hi=hi)
        cim, _, _ = vis.zbuffer(Vc, Fc, d, px, lo=lo, hi=hi)
        H, W = img.shape
        fg = img >= 0
        lab, nb = ndimage.label(~fg)
        border = set(np.unique(np.r_[lab[0], lab[-1], lab[:, 0], lab[:, -1]])) - {0}
        enclosed = ~fg & ~np.isin(lab, list(border))
        cs = cim >= 0
        if not cs.any():
            continue
        ys, xs = np.nonzero(cs)
        cf = cim[ys, xs]
        # depths at pixel centres (plane of the face)
        cx = lo[0] + (xs + .5) * px
        cy = lo[1] + (ys + .5) * px

        def depth(Vx, Fx, fid):
            Nx = vis.face_normals(Vx, Fx)[fid]
            P0 = Vx[Fx[fid, 0]]
            den = Nx @ dd
            q = cx[:, None] * r + cy[:, None] * u
            t = np.einsum('ij,ij->i', P0 - q, Nx) / np.where(np.abs(den) < 1e-9, 1e-9, den)
            return t
        zc = depth(Vc, Fc, cf)
        rf = img[ys, xs]
        has = rf >= 0
        zr = np.full(len(ys), np.inf)
        if has.any():                                    # depth of the result's visible face at these pixels
            idx = np.flatnonzero(has)
            Nx = N[rf[idx]]
            P0 = V[F[rf[idx], 0]]
            den = Nx @ dd
            q = cx[idx, None] * r + cy[idx, None] * u
            zr[idx] = np.einsum('ij,ij->i', P0 - q, Nx) / np.where(np.abs(den) < 1e-9, 1e-9, den)
        infront = zc < zr - eps
        backface = np.zeros(len(ys), bool)
        backface[has] = (N[rf[has]] @ dd) > 0
        # perpendicular separation from the result's face (a depth difference along a grazing view is mostly sideways offset)
        cosr = np.ones(len(ys))
        cosr[has] = np.abs(N[rf[has]] @ dd)
        nc = vis.face_normals(Vc, Fc)[cf] @ dd
        cfront = nc < 0                                 # the candidate faces the camera (seen from behind it would show its inside)
        cosc = np.abs(nc)
        graze = (cosc < GRAZE_COS) | (has & (cosr < GRAZE_COS))
        g = infront & cfront & (~has | backface) & ~graze      # the result shows its inside or nothing: the body belongs there
        b = infront & ~graze & ((has & ~backface & ((zr - zc) * cosr > eps)) | ~cfront)   # covers a visible front face, or shows its own inside
        np.add.at(good, cf[g], 1)
        np.add.at(bad, cf[b], 1)
    return good, bad


def underlay(V, F, BV, BF, Bpart, px_mm=UNDERLAY_PX_MM, min_mm=UNDERLAY_MIN_MM, eps_mm=UNDERLAY_EPS_MM, dirs=None, max_level=6):
    """Faces of the game body (BV, BF, per-face part Bpart) that close holes in the result (V, F). Returns (Vu, Fu, part_u, info)."""
    dirs = vis.view_dirs() if dirs is None else dirs
    px, eps = px_mm / 1000, eps_mm / 1000
    Vc, Fc, pc = np.asarray(BV, float), np.asarray(BF, np.int64), np.asarray(Bpart)
    accV, accF, accP = [], [], []
    info = {'levels': []}
    for level in range(max_level + 1):
        if not len(Fc):
            break
        good, bad = _classify(V, F, Vc, Fc, dirs, px, eps)
        ok = (good >= UNDERLAY_GOOD_MIN) & (bad <= UNDERLAY_BAD_FRAC * good)
        L = np.linalg.norm(Vc[Fc] - Vc[np.roll(Fc, 1, axis=1)], axis=2).max(1)
        split = (good > 0) & ~ok & (L > min_mm / 1000)
        info['levels'].append({'candidates': int(len(Fc)), 'accepted': int(ok.sum()), 'split': int(split.sum())})
        if ok.any():
            used, Fk = np.unique(Fc[ok], return_inverse=True)
            accV.append(Vc[used])
            accF.append(Fk.reshape(-1, 3))
            accP.append(pc[ok])
        if not split.any():
            break
        Fs = Fc[split]
        used, Fk = np.unique(Fs, return_inverse=True)
        V2, F2, par = _subdivide(Vc[used], Fk.reshape(-1, 3))
        Vc, Fc, pc = V2, F2, pc[split][par]
    if not accF:
        return np.zeros((0, 3)), np.zeros((0, 3), np.int64), np.zeros(0, np.int64), info
    off = np.cumsum([0] + [len(v) for v in accV[:-1]])
    Vu = np.concatenate(accV)
    Fu = np.concatenate([f + o for f, o in zip(accF, off)])
    Pu = np.concatenate(accP)
    info['faces'] = int(len(Fu))
    return Vu, Fu, Pu, info


def merge_flat(V, F, mask, ang_tol=1e-4, plane_tol=1e-7, max_rounds=20):
    """Lossless decimation of the faces in `mask` (2026-11-23: undoes a flat subdivision where nothing was cut or moved): a vertex whose faces are
    all in `mask`, with no open edge around it, is collapsed onto a neighbour u when u lies in the plane of every face around it (so the surface
    does not change) and no remaining face turns over or loses its area. Returns (F_new, kept face mask over the input F, vertices removed)."""
    F = np.array(F, np.int64)
    alive = np.ones(len(F), bool)
    mask = np.asarray(mask, bool)
    N0 = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    removed = 0
    for _ in range(max_rounds):
        vf = {}
        for fi in np.flatnonzero(alive):
            for x in F[fi]:
                vf.setdefault(int(x), []).append(int(fi))
        # edge use counts (open edges)
        A = F[alive]
        E = np.sort(np.concatenate([A[:, [0, 1]], A[:, [1, 2]], A[:, [2, 0]]]), axis=1)
        ue, cnt = np.unique(E, axis=0, return_counts=True)
        bad_v = set(np.unique(ue[cnt != 2]).tolist())
        touched = set()
        changed = 0
        for v, fs in vf.items():
            if v in bad_v or v in touched or not all(mask[f] for f in fs):
                continue
            fs = np.array(fs)
            n = N0[fs]
            ln = np.linalg.norm(n, axis=1)
            if (ln < 1e-14).any():
                continue
            n = n / ln[:, None]
            nbr = np.unique(F[fs])
            nbr = nbr[nbr != v]
            if any(int(u) in touched for u in nbr):
                continue
            for u in nbr:
                d = V[u] - V[v]
                if np.abs(n @ d).max() > plane_tol:
                    continue
                keep = fs[~np.any(F[fs] == u, axis=1)]          # faces that survive (the two with edge v-u vanish)
                Fn = F[keep].copy()
                Fn[Fn == v] = u
                nn = np.cross(V[Fn[:, 1]] - V[Fn[:, 0]], V[Fn[:, 2]] - V[Fn[:, 0]])
                lnn = np.linalg.norm(nn, axis=1)
                if (lnn < 1e-12).any() or ((nn / lnn[:, None] * n[~np.any(F[fs] == u, axis=1)]).sum(1) < 1 - ang_tol).any():
                    continue
                F[keep] = Fn
                N0[keep] = nn
                alive[fs[np.any(F[fs] == u, axis=1) & np.any(F[fs] == v, axis=1)]] = False
                # faces that contained both u and v were removed above; ensure no alive face still references v
                touched.update([int(v), int(u)] + [int(x) for x in nbr])
                changed += 1
                break
        removed += changed
        if not changed:
            break
    return F, alive, removed


BACK_RATIO = 4.0


def fix_inverted(V, F, body, px_mm=4.0, min_back=3, inward_dot=-0.3, always=None):
    """Faces wound the wrong way round (2026-11-23: whole skirt panels of some generated modules showed their back, red, from outside): a face whose
    back is seen at least BACK_RATIO times as much as its front (and min_back px over all views) and whose normal points INTO the game body (dot with the
    body's outward normal at its closest point < inward_dot) while the face itself lies outside the body, is flipped. Returns the flip mask."""
    v = vis.visibility(V, F, px=px_mm / 1000, see_through=False)
    cand = (v['back'] >= min_back) & (v['back'] >= BACK_RATIO * v['front'])   # seen mostly from behind (the front only from inside a skirt)
    if not cand.any():
        return cand
    C = V[F[cand]].mean(1)
    ms = body.measure(C)
    N = vis.face_normals(V, F[cand])
    bn = body.tri_n[ms['tri']] if 'tri' in ms else ms['n']
    ok = ((N * bn).sum(1) < inward_dot) & (ms['d'] > 0.002)
    if always is not None:
        ok |= np.asarray(always, bool)[cand]
    flip = np.zeros(len(F), bool)
    flip[np.flatnonzero(cand)[ok]] = True
    return flip
