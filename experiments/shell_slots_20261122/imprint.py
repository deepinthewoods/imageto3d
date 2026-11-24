"""Watertight joins between garments and the game body by imprinting (2026-11-24).

User: "scan them for watertightness ... where the different meshes are stitched together, like the thighs, the knees, where the outfits join with
the skin ... it's almost every mesh". The scan (watertight.py) found no result closed. Cause: the joins were layered, not connected. A garment
edge got a lip that dips 3 mm under the skin while the game skin ran on under the garment (and was dropped where a big game triangle stuck out past
the garment edge), so lip and skin ended side by side, 3-15 mm apart, and nothing joined them.

Here the joins are built connected:
  1. every garment edge loop of every module that lies near the body (90 % of it within LIP_ATTACH_MM) gets a FOOT CURVE: the closest points of its
     vertices on the game body (within the module's slot);
  2. the foot curves are imprinted into ONE copy of the game body: the body's triangles are split exactly along them (a walk across the body from
     foot to foot in the plane through the two feet and the surface normal; edge splits / face splits only, so the surface does not change);
  3. the imprinted body falls apart into regions bounded by the curves; a region that the garments cover (REMOVE_FRAC of its area has garment along
     the normal) and that holds no game-owned piece (head, bare hands / feet) is removed;
  4. each attached garment loop is joined to its foot curve by a strip of triangles (the lip) using the curve's own vertices.
Garment loops standing well off the body (a skirt hem) stay free edges over the body, which is then kept under them (closed). The modules of an
armour set share the one body copy, so a gauntlet, a boot and the body armour meet on the same imprinted body (no separate slot pieces).
"""
from collections import defaultdict

import numpy as np

from shell_slots import SKIN, Mesh, bf, boundary_loops, splice_loops

LIP_ATTACH_MM = 200.0     # a garment loop is attached when 90 % of its vertices are within this of the body
FOOT_JUMP_K = 4.0         # ... and no two consecutive feet are further apart than this x their garment edge + FOOT_JUMP_MM
FOOT_JUMP_MM = 25.0
FOOT_REVERSE_COS = -0.5    # a foot where the foot curve turns back by more than 120 degrees is skipped (garment edge folded into the body)
INSERT_TOL_MM = 0.4       # a point this close to an existing vertex / edge is put on it
MESHLIKE_PATCHES_PER_M2 = 1e9   # (set after measuring) a region with more uncovered patches per m2 than this is under a mesh-like garment: kept
OWNED_MAX_M2 = 0.02        # ... and at most this much of it (a chin under a collar, never a hand: 0.03-0.1 m2)
OWNED_MAX_FRAC = 0.2       # ... and less than this fraction of its area is game-owned (head, bare hands / feet)
REMOVE_MAX_UNCOVERED_M2 = 0.05   # ... and no connected uncovered patch in it is larger than this (a hand is 0.04-0.1 m2)
PERFACE_FALLBACK = False   # off: tried 2026-11-24 on 33 results, coverage-only face removal cut real holes (red 88k -> 332k, open 19 -> 238 m)
RUNON_MM = 60.0           # (regions that cannot go whole) covered faces this far from uncovered skin / owned pieces are removed
SOURCE_PATCH_M2 = 0.01    # ... uncovered patches smaller than this (armpits, where the normal misses the garment) do not count
REMOVE_FRAC = 0.85        # a region is removed when this fraction of its area is covered by garment
COVER_IN_MM = 40.0        # covered: garment on the line along the face normal from this far inside ...
COVER_OUT_MM = 150.0      # ... to this far outside
TRACE_MAX_STEPS = 4000
OVERHANG_FRAC = 0.25      # (pieces) a garment loop with more than this share of its vertices past the slot's ring is left free (overlap)


class LiveMesh:
    """A triangle mesh that can be split in place (edge splits, face splits): vertex positions grow, faces are killed and replaced; every face keeps
    the index of the original face it came from."""

    def __init__(self, V, F, part):
        self.V = np.array(V, np.float64)
        self.nv = len(self.V)
        self.F = [tuple(int(x) for x in f) for f in F]
        self.alive = [True] * len(self.F)
        self.part = [int(p) for p in part]
        self.orig = list(range(len(self.F)))
        self.ef = defaultdict(set)
        self.vf = defaultdict(set)
        self.desc = defaultdict(set)
        self.protect = set()           # edges never split (pieces: the slot's ring, shared with the next piece); a point on one goes to an end
        for f in range(len(self.F)):
            self._index(f)
            self.desc[f].add(f)

    @staticmethod
    def key(a, b):
        return (a, b) if a < b else (b, a)

    def _index(self, f):
        a, b, c = self.F[f]
        for x, y in ((a, b), (b, c), (c, a)):
            self.ef[self.key(x, y)].add(f)
        for v in (a, b, c):
            self.vf[v].add(f)

    def _unindex(self, f):
        a, b, c = self.F[f]
        for x, y in ((a, b), (b, c), (c, a)):
            self.ef[self.key(x, y)].discard(f)
        for v in (a, b, c):
            self.vf[v].discard(f)

    def add_v(self, P):
        if self.nv == len(self.V):
            self.V = np.concatenate([self.V, np.zeros_like(self.V)])
        self.V[self.nv] = P
        self.nv += 1
        return self.nv - 1

    def add_f(self, tri, like):
        f = len(self.F)
        self.F.append(tuple(int(x) for x in tri))
        self.alive.append(True)
        self.part.append(self.part[like])
        self.orig.append(self.orig[like])
        self._index(f)
        self.desc[self.orig[like]].add(f)
        return f

    def kill(self, f):
        self.alive[f] = False
        self._unindex(f)
        self.desc[self.orig[f]].discard(f)

    def split_edge(self, a, b, P):
        if self.key(a, b) in self.protect:
            self.protected_hits = getattr(self, 'protected_hits', 0) + 1
            return a if np.linalg.norm(self.V[a] - P) <= np.linalg.norm(self.V[b] - P) else b
        m = self.add_v(P)
        for f in list(self.ef.get(self.key(a, b), ())):
            t = self.F[f]
            i = next(i for i in range(3) if {t[i], t[(i + 1) % 3]} == {a, b})
            x, y, z = t[i], t[(i + 1) % 3], t[(i + 2) % 3]
            self.kill(f)
            self.add_f((x, m, z), f)
            self.add_f((m, y, z), f)
        return m

    def split_face(self, f, P):
        a, b, c = self.F[f]
        m = self.add_v(P)
        self.kill(f)
        for tri in ((a, b, m), (b, c, m), (c, a, m)):
            self.add_f(tri, f)
        return m

    def closest_in_face(self, f, P):
        """(distance, barycentric (3,), closest point) of P to live face f."""
        A, B, C = self.V[list(self.F[f])]
        w = bf.tri_closest(P[None], A[None], B[None], C[None])[0]
        Q = w[0] * A + w[1] * B + w[2] * C
        return float(np.linalg.norm(P - Q)), w, Q

    def insert(self, P, faces, tol):
        """Put point P (on the surface) into the mesh: onto a vertex within tol, an edge within tol, or into the face. faces: live candidates."""
        best = None
        for f in faces:
            if not self.alive[f]:
                continue
            d, w, Q = self.closest_in_face(f, P)
            if best is None or d < best[0]:
                best = (d, f, w, Q)
        if best is None:
            return None
        d, f, w, Q = best
        tri = self.F[f]
        for i in range(3):
            if np.linalg.norm(self.V[tri[i]] - Q) <= tol:
                return tri[i]
        for i in range(3):
            a, b = tri[i], tri[(i + 1) % 3]
            ab = self.V[b] - self.V[a]
            t = float(np.clip((Q - self.V[a]) @ ab / max(ab @ ab, 1e-30), 0., 1.))
            E = self.V[a] + t * ab
            if np.linalg.norm(E - Q) <= tol:
                return self.split_edge(a, b, E)
        return self.split_face(f, Q)

    def face_normal(self, f):
        A, B, C = self.V[list(self.F[f])]
        n = np.cross(B - A, C - A)
        return n / max(np.linalg.norm(n), 1e-30)

    def trace(self, cur, T, nT, tol):
        """Walk from vertex cur to the surface point T across the live mesh, splitting the edges it crosses; returns the vertex ids of the path
        (cur first, T's vertex last) or None. The walk follows the plane through the current point and T that holds the surface normal."""
        path = [cur]
        seen = {cur}
        for _ in range(TRACE_MAX_STEPS):
            p = self.V[cur]
            if np.linalg.norm(p - T) <= tol:
                return path
            faces = [f for f in self.vf[cur] if self.alive[f]]
            if not faces:
                self.fail = ('no faces', cur, len(path))
                return None
            # T inside a face around cur: insert it there (cur - T is then an edge)
            hit = None
            for f in faces:
                d, w, Q = self.closest_in_face(f, T)
                if d <= 1e-6:
                    hit = f
                    break
            if hit is not None:
                v = self.insert(T, [hit], tol)
                if v is None:
                    return None
                if v != cur:
                    path.append(v)
                return path
            nrm = sum(self.face_normal(f) for f in faces) + nT
            n_pl = np.cross(T - p, nrm)
            ln = np.linalg.norm(n_pl)
            if ln < 1e-12:
                self.fail = ('degenerate plane', cur, len(path))
                return None
            n_pl /= ln
            dirT = (T - p) / max(np.linalg.norm(T - p), 1e-30)
            best = None
            for f in faces:
                t3 = self.F[f]
                i = t3.index(cur)
                x, y = t3[(i + 1) % 3], t3[(i + 2) % 3]
                sx, sy = n_pl @ (self.V[x] - p), n_pl @ (self.V[y] - p)
                if sx * sy > 0 or (abs(sx) < 1e-9 and abs(sy) < 1e-9):
                    continue
                t = sx / (sx - sy)
                q = self.V[x] + t * (self.V[y] - self.V[x])
                # where the crossing lands: on x / y (within tol) or between them (split); never back onto a vertex of this path
                snap = x if np.linalg.norm(q - self.V[x]) <= tol else (y if np.linalg.norm(q - self.V[y]) <= tol else None)
                if snap is not None and snap in seen:
                    other = y if snap == x else x
                    if other in seen or min(np.linalg.norm(q - self.V[x]), np.linalg.norm(q - self.V[y])) < 1e-6:
                        continue
                    snap = -1                                     # split close to the visited vertex instead of landing on it
                step = q - p
                ls = np.linalg.norm(step)
                if ls < 1e-9:
                    continue
                fwd = (step @ dirT) / ls
                if fwd <= 0.05:
                    continue
                if best is None or fwd > best[0]:
                    best = (fwd, x, y, q, snap)
            if best is None:                                  # (a crease the plane does not cross forwards) the direction to T in each face's own plane
                for f in faces:
                    t3 = self.F[f]
                    i = t3.index(cur)
                    x, y = t3[(i + 1) % 3], t3[(i + 2) % 3]
                    nf = self.face_normal(f)
                    dl = dirT - (dirT @ nf) * nf
                    if np.linalg.norm(dl) < 1e-9:
                        continue
                    # p + s dl = x + t (y - x): solve in the face plane (least squares)
                    M = np.stack([dl, -(self.V[y] - self.V[x])], 1)
                    sol, *_ = np.linalg.lstsq(M, self.V[x] - p, rcond=None)
                    s_, t_ = sol
                    if s_ <= 1e-9 or t_ < -1e-6 or t_ > 1 + 1e-6:
                        continue
                    t_ = float(np.clip(t_, 0., 1.))
                    q = self.V[x] + t_ * (self.V[y] - self.V[x])
                    snap = x if np.linalg.norm(q - self.V[x]) <= tol else (y if np.linalg.norm(q - self.V[y]) <= tol else None)
                    if snap is not None and snap in seen:
                        continue
                    fwd = ((q - p) @ dirT) / max(np.linalg.norm(q - p), 1e-30)
                    if best is None or fwd > best[0]:
                        best = (fwd, x, y, q, snap)
            if best is None:
                self.fail = ('no forward crossing', cur, len(path))
                return None
            _, x, y, q, snap = best
            nxt = snap if snap is not None and snap >= 0 else self.split_edge(x, y, q)
            if nxt in seen:
                self.fail = ('revisit', cur, len(path))
                return None
            seen.add(nxt)
            path.append(nxt)
            cur = nxt
        self.fail = ('too many steps', cur, len(path))
        return None

    def edge_path(self, a, b):
        """Shortest path from vertex a to vertex b along live edges (vertex ids, a first), or None."""
        if a == b:
            return [a]
        from scipy import sparse
        from scipy.sparse import csgraph
        E = np.array([e for e, fs in self.ef.items() if any(self.alive[f] for f in fs)], np.int64)
        w = np.linalg.norm(self.V[E[:, 0]] - self.V[E[:, 1]], axis=1) + 1e-12
        G = sparse.coo_matrix((np.r_[w, w], (np.r_[E[:, 0], E[:, 1]], np.r_[E[:, 1], E[:, 0]])), shape=(self.nv, self.nv)).tocsr()
        d, pred = csgraph.dijkstra(G, directed=False, indices=a, return_predecessors=True)
        if not np.isfinite(d[b]):
            return None
        path = [b]
        while path[-1] != a:
            path.append(int(pred[path[-1]]))
        return path[::-1]

    def mesh(self):
        idx = [f for f in range(len(self.F)) if self.alive[f]]
        return np.array([self.F[f] for f in idx], np.int64).reshape(-1, 3), np.array([self.part[f] for f in idx], np.int64), np.array(idx)


def _strip(A, pa, B, pb):
    tris, i, j = [], 0, 0
    while i < len(A) - 1 or j < len(B) - 1:
        if j >= len(B) - 1 or (i < len(A) - 1 and pa[i + 1] <= pb[j + 1]):
            tris.append((A[i + 1], A[i], B[j], i))
            i += 1
        else:
            tris.append((B[j], B[j + 1], A[i], max(i - 1, 0) if i == len(A) - 1 else i))
            j += 1
    return tris


def covered_faces(V, F, garments, cover_in=COVER_IN_MM, cover_out=COVER_OUT_MM):
    """Per face: fraction (0..1) of 4 samples with garment on the line along the face normal."""
    from geodiv import covered_count
    cnt = np.zeros(len(F))
    sub = Mesh(V, F, SKIN, 0, 'game')
    for GV, GF in garments:
        if len(GF):
            cnt = np.maximum(cnt, covered_count(sub, GV, GF, cover_in=cover_in, cover_out=cover_out))
    return cnt / 4.


def build(body, modules, game_only, log=print, loop_outside=None, keep_ring_band=False):
    """modules: list of (garment Mesh (only its kept garment faces matter), slot part indices[, see_through]). Returns (out Mesh, notes).
    A see_through module (chain mail) is joined like any other but does not count as covering the body (the body under it is kept).
    The output holds: the kept game body (prov 'band' on module slots, 'game' on game-owned parts), every module's garment (its own prov), and the
    lips (prov 'lip'), all in one vertex array (body vertices first).
    loop_outside (pieces.py, swappable pieces): P -> True where a garment loop vertex lies past the slot's ring (its closest point on the WHOLE
    game body is outside the slot); a loop with more than OVERHANG_FRAC of its vertices there is left free: it overlaps the next piece.
    keep_ring_band (pieces): `body` is one slot of the game body, open at its rings; the faces touching a ring are kept even in a removed region,
    so the ring is always closed by both pieces that meet there (the band's inner edge stays under the garment)."""
    lm = LiveMesh(body.V, body.F, body.part)
    if keep_ring_band:                 # the slot's open border is the ring shared with the next piece: never split it (no T-junctions there)
        Eb = np.sort(np.concatenate([body.F[:, [0, 1]], body.F[:, [1, 2]], body.F[:, [2, 0]]]), axis=1)
        ub, cb = np.unique(Eb, axis=0, return_counts=True)
        lm.protect = {LiveMesh.key(int(a), int(b)) for a, b in ub[cb == 1]}
    tol = INSERT_TOL_MM / 1000
    notes = {'loops': 0, 'attached': 0, 'free': 0, 'trace_failed': 0, 'jump_free': 0}
    if loop_outside is not None:
        notes['overhang'] = 0
    curves = []                      # (module index, garment loop ids, foot chain vertex ids, chain params)
    for mi, entry in enumerate(modules):
        g, slot = entry[0], entry[1]
        if not len(g.F):
            continue
        slot_faces = np.flatnonzero(np.isin(body.part, slot))
        sub = bf.Body(body.V, body.F[slot_faces], slot_faces, [])
        loops = [np.array(h) for h in splice_loops(boundary_loops(g))]
        for h in loops:
            if len(h) < 3:
                continue
            notes['loops'] += 1
            P = g.V[h]
            d, tri, w = sub.closest(P)
            foot = (w[:, :1] * sub.A[tri] + w[:, 1:2] * sub.B[tri] + w[:, 2:] * sub.C[tri])
            otri = slot_faces[tri]
            from collections import Counter
            info_ = {'module': mi, 'vertices': int(len(h)), 'd_q90_mm': round(float(np.quantile(d, 0.9)) * 1000, 1),
                     'parts': Counter(np.array(body.part_names)[body.part[otri]].tolist()).most_common(2)}
            notes.setdefault('loop_list', []).append(info_)
            if loop_outside is not None:
                fo = float(np.mean(loop_outside(P)))
                if fo > OVERHANG_FRAC:
                    notes['overhang'] += 1
                    info_['status'] = f'past the ring ({fo:.0%}): overlaps the next piece'
                    continue
            if np.quantile(d, 0.9) > LIP_ATTACH_MM / 1000:
                notes['free'] += 1
                info_['status'] = 'free'
                continue
            ge = np.linalg.norm(np.roll(P, -1, 0) - P, axis=1)
            fe = np.linalg.norm(np.roll(foot, -1, 0) - foot, axis=1)
            if (fe > FOOT_JUMP_K * ge + FOOT_JUMP_MM / 1000).any():
                notes['jump_free'] += 1
                info_['status'] = 'feet jump'
                continue
            n = len(h)
            keepk = list(range(n))                                 # feet that reverse the curve (a collar point tucked into the neck) are skipped
            for _ in range(n):
                drop = None
                for i in range(len(keepk)):
                    a_, b_, c_ = foot[keepk[i - 1]], foot[keepk[i]], foot[keepk[(i + 1) % len(keepk)]]
                    u_, w_ = b_ - a_, c_ - b_
                    nu, nw = np.linalg.norm(u_), np.linalg.norm(w_)
                    if nu > 1e-6 and nw > 1e-6 and (u_ @ w_) / (nu * nw) < FOOT_REVERSE_COS:
                        drop = i
                        break
                if drop is None or len(keepk) <= 4:
                    break
                keepk.pop(drop)
            notes['feet_skipped'] = notes.get('feet_skipped', 0) + n - len(keepk)
            v0 = lm.insert(foot[keepk[0]], list(lm.desc[otri[keepk[0]]]), tol)
            if v0 is None:
                notes['trace_failed'] += 1
                continue
            chain, params, cur, ok = [v0], [float(keepk[0])], v0, True
            seq = keepk[1:] + [keepk[0] + n]                       # foot indices to visit (the last is the first again, one turn on)
            i = 0
            prevk = keepk[0]
            while i < len(seq):
                k = seq[i]
                kk = k % n
                path = lm.trace(cur, foot[kk], body.tri_n[otri[kk]], tol)
                if path is None:                                   # the straight walk is stuck: the shortest way along the body's own edges
                    vt = lm.insert(foot[kk], [f for f in lm.desc[otri[kk]] if lm.alive[f]] or list(lm.vf[cur]), tol)
                    path = lm.edge_path(cur, vt) if vt is not None else None
                    notes['edge_path_fallbacks'] = notes.get('edge_path_fallbacks', 0) + 1
                if path is None:
                    ok = False
                    break
                seg = lm.V[path]
                if len(path) > 1:
                    s = np.r_[0., np.cumsum(np.linalg.norm(np.diff(seg, axis=0), axis=1))]
                    s = s / s[-1] if s[-1] > 0 else np.linspace(0, 1, len(path))
                    for v, t in zip(path[1:], s[1:]):
                        chain.append(v)
                        params.append(prevk + (k - prevk) * t)
                else:                                              # the next foot is the same vertex
                    chain.append(cur)
                    params.append(float(k))
                cur = chain[-1]
                prevk = k
                i += 1
            if not ok or chain[-1] != v0:
                notes['trace_failed'] += 1
                info_['status'] = 'trace failed'
                continue
            info_['status'] = 'attached'
            info_['chain'] = len(chain)
            params = np.array(params) - keepk[0]                   # A (the garment loop) starts at foot keepk[0]
            h = np.roll(h, -keepk[0])
            notes['attached'] += 1
            curves.append((mi, h, chain, np.array(params)))
    # regions of the imprinted body, separated by the curves
    F, part, idx = lm.mesh()
    V = lm.V[:lm.nv]
    cedge = set()
    for _, _, chain, _ in curves:
        for a, b in zip(chain[:-1], chain[1:]):
            if a != b:
                cedge.add(LiveMesh.key(a, b))
    E = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    fi = np.tile(np.arange(len(F)), 3)
    k_ = np.sort(E, 1)
    order = np.lexsort((k_[:, 1], k_[:, 0]))
    ks, fs = k_[order], fi[order]
    same = np.r_[False, (ks[1:] == ks[:-1]).all(1)]
    pa, pb = fs[np.flatnonzero(same) - 1], fs[same]
    keep_pair = np.array([LiveMesh.key(int(x), int(y)) not in cedge for x, y in ks[same]], bool)
    from scipy import sparse
    from scipy.sparse import csgraph
    G = sparse.coo_matrix((np.ones(int(keep_pair.sum())), (pa[keep_pair], pb[keep_pair])), shape=(len(F), len(F)))
    nreg, lab = csgraph.connected_components(G, directed=False)
    # see-through garments (chain mail: rings with holes) do not cover the body: it stays under them, so the holes show skin, not the inside
    garments = [(e[0].V, e[0].F) for e in modules if len(e[0].F) and not (len(e) > 2 and e[2])]
    cov = covered_faces(V, F, garments)
    area = np.linalg.norm(np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]), axis=1) / 2
    owned = np.isin(part, game_only)
    rcov = np.bincount(lab, area * cov, nreg) / np.maximum(np.bincount(lab, area, nreg), 1e-30)
    rown = np.bincount(lab, area * owned, nreg) / np.maximum(np.bincount(lab, area, nreg), 1e-30)
    # uncovered patches (connected faces with garment on fewer than half their samples): a region holding a big one (a hand, a forearm left
    # outside a sleeve whose curve failed) is never removed; scattered small ones (armpits, where the normal misses the garment) do not count
    unc = cov < 0.5
    pk = keep_pair & unc[pa] & unc[pb]
    Gu = sparse.coo_matrix((np.ones(int(pk.sum())), (pa[pk], pb[pk])), shape=(len(F), len(F)))
    nu, ulab = csgraph.connected_components(Gu, directed=False)
    uarea = np.bincount(ulab, area * unc, nu)
    big = np.zeros(nreg)
    np.maximum.at(big, lab[unc], uarea[ulab[unc]])
    # uncovered patches per square metre: a garment with holes all over (chain mail rings) shows the inside through them when the body under it
    # is removed, so such a region is kept
    first = np.unique(ulab[unc], return_index=True)[1]
    pf = np.flatnonzero(unc)[first]                                # one face per uncovered patch
    npatch = np.bincount(lab[pf], minlength=nreg).astype(float)
    pdens = npatch / np.maximum(np.bincount(lab, area, nreg), 1e-30)
    # a few game-owned faces on the covered side of a curve (the chin under a collar) go too
    rown_m2 = np.bincount(lab, area * owned, nreg)
    remove = (rcov >= REMOVE_FRAC) & (rown < OWNED_MAX_FRAC) & (rown_m2 <= OWNED_MAX_M2) & (big <= REMOVE_MAX_UNCOVERED_M2) & \
        (pdens <= MESHLIKE_PATCHES_PER_M2)
    notes.pop('largest_uncovered_patch_m2', None)
    rarea = np.bincount(lab, area, nreg)
    notes['region_summary'] = [{'area_m2': round(float(rarea[r]), 3), 'covered': round(float(rcov[r]), 3), 'owned': round(float(rown[r]), 3),
                                'largest_uncovered_patch_m2': round(float(big[r]), 4), 'uncovered_patches_per_m2': round(float(pdens[r]), 1),
                                'removed': bool(remove[r])}
                               for r in np.argsort(-rarea) if rarea[r] > 0.03]
    keepf = ~remove[lab]
    if keep_ring_band:
        Eo = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1)
        ue_, uc_ = np.unique(Eo, axis=0, return_counts=True)
        on_ring = np.zeros(len(V), bool)
        on_ring[ue_[uc_ == 1].ravel()] = True
        band = on_ring[F].any(1) & ~keepf
        keepf |= band
        notes['ring_band_faces_kept'] = int(band.sum())
        notes['ring_splits_refused'] = int(getattr(lm, 'protected_hits', 0))
    # regions that cannot go whole (a game-owned piece or a real uncovered patch in them: ragged garments, edges that did not separate): their
    # covered faces further than RUNON_MM from the uncovered patches / owned pieces go (walking only within the region, never across a curve);
    # the skin left runs on under the garment by that much, as the layered joins did
    part_keep = ~remove[lab]
    if PERFACE_FALLBACK and part_keep.any():
        srcs = (owned | (unc & (uarea[ulab] >= SOURCE_PATCH_M2))) & part_keep
        if srcs.any():
            cen = V[F].mean(1)
            w = np.linalg.norm(cen[pa[keep_pair]] - cen[pb[keep_pair]], axis=1) + 1e-9
            Gf = sparse.coo_matrix((np.r_[w, w], (np.r_[pa[keep_pair], pb[keep_pair]], np.r_[pb[keep_pair], pa[keep_pair]])), shape=(len(F), len(F))).tocsr()
            dist = csgraph.dijkstra(Gf, directed=False, indices=np.flatnonzero(srcs), min_only=True, limit=RUNON_MM / 1000 * 1.5)
            drop = part_keep & (cov >= 0.75) & ~owned & (dist >= RUNON_MM / 1000)
            keepf &= ~drop
            notes['faces_removed_by_coverage'] = int(drop.sum())
    notes['regions'] = int(nreg)
    notes['regions_removed'] = int(remove.sum())
    # assemble: body vertices, then each module's garment vertices
    nb = len(V)
    Vs, Fs, roles, parts, provs = [V], [F[keepf]], [np.full(int(keepf.sum()), SKIN, '<U10')], [part[keepf]], \
        [np.where(owned[keepf], 'game', 'band').astype('<U8')]
    offs, off = [], nb
    for entry in modules:
        g = entry[0]
        offs.append(off)
        Vs.append(g.V)
        Fs.append(g.F + off)
        roles.append(g.role)
        parts.append(g.part)
        provs.append(g.prov)
        off += len(g.V)
    # lips: garment loop -> foot chain; orientation checked against the kept body faces along the curve
    lipF, lipR, flips = [], [], 0
    kept_he = set()
    for t in F[keepf]:
        for x, y in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            kept_he.add((int(x), int(y)))
    own_role = []
    for mi, h, chain, params in curves:
        g = modules[mi][0]
        er = {}
        for f, t in enumerate(g.F):
            for x, y in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
                er[(int(x), int(y))] = str(g.role[f])
        A = [int(x) + offs[mi] for x in h] + [int(h[0]) + offs[mi]]
        pa_ = np.arange(len(h) + 1, dtype=float)
        B = list(chain)
        pb_ = np.maximum.accumulate(params)
        # drop repeated consecutive chain vertices
        Bk, pk = [B[0]], [pb_[0]]
        for v, p in zip(B[1:], pb_[1:]):
            if v != Bk[-1]:
                Bk.append(v)
                pk.append(p)
        tris = _strip(A, pa_, Bk, np.array(pk))
        # orientation: the lip's chain edges run B[j] -> B[j+1]; the kept body next to them should hold B[j+1] -> B[j]
        agree = sum((b, a) in kept_he for a, b in zip(Bk[:-1], Bk[1:]))
        clash = sum((a, b) in kept_he for a, b in zip(Bk[:-1], Bk[1:]))
        flip = clash > agree
        flips += int(flip)
        for a, b, c, i in tris:
            if len({a, b, c}) < 3:
                continue
            lipF.append((a, c, b) if flip else (a, b, c))
            lipR.append(er.get((int(h[i % len(h)]), int(h[(i + 1) % len(h)])), 'primary'))
    notes['lips_flipped_to_match_body'] = flips
    notes.pop('_skips', None)
    notes['lip_faces'] = len(lipF)
    Vall = np.concatenate(Vs)
    Fall = np.concatenate(Fs + [np.array(lipF, np.int64).reshape(-1, 3)])
    out = Mesh(Vall, Fall, np.concatenate(roles + [np.array(lipR, '<U10')]), np.concatenate(parts + [np.full(len(lipF), -1)]),
               np.concatenate(provs + [np.full(len(lipF), 'lip', '<U8')]))
    if (out.part < 0).any():
        out.part[out.part < 0] = body.measure(out.V[out.F[out.part < 0]].mean(1))['part']
    return out, notes
