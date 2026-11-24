"""Swappable LAYERS cut from the generated models (2026-11-24, the user's design).

User: "all we'd be rendering in the game is gloves layer, boots layer, armor layer, and then the head ... each of those layers would contain the
skin colored parts in such a way that they link up seamlessly when we combine different boots and gloves and armors" ... "We need to use the skin
color sections of the outfit mesh and then still have like a stitching stage which joins them together seamlessly with the sliced point".
"I still want the amount slider to change the silhouette to be more in keeping with the base body ... still need to do all the alignment".

A layer is a slice of ONE generated module (aligned on its white anchors as before, shell_slots.load_shell): its garment and its OWN skin. No game
body faces are in a garment layer, so nothing of the base body can poke through it. The seams are the game body's own existing edge loops at
the neck, elbows and knees (the part borders; the same vertices for every layer of a race, and the bare layers end on them already). A layer
keeps WHOLE faces (no slicing) and its edge vertices at a seam are MOVED onto the seam's vertices (merging where several land on one; a triangle
is split only where a seam vertex would be skipped), with the next rows of existing vertices following part of the move: no stitch ribbon
(user 2026-11-24: "move the existing vertices to make them seamless rather than creating a little ribbon of geometry"). Where the generated
model has garment across the seam (a cuff over the elbow) there is no skin to join: that garment overlaps the next layer and is end-capped.

  armour scheme   head (game) + torso (body armour) + lower_arm.r/l (gloves: forearm + hand) + lower_leg.r/l (boots: shin + foot)
  outfit scheme   head (game) + outfit (everything below the neck)
  bare layers     the game body's own faces of a slot (no gloves / no boots / no armour)

The Amount morph ('fill'): generated skin inside the game body goes out onto it, then the garment is pushed out of the body (geodiv.fill_grouped)
with garment layers kept in order and folds undone (pieces.py guards). Ring vertices never move.

  python experiments/shell_slots_20261122/layers.py [--races ...] [--only <set substring>] [--workers 11] [--gpu]
Writes out/layers/<race>/<slot>__<set>.glb (+ .json, scenes/*.npz), out/layers/index.json; out/layers/mix.html shows them together.
"""
import argparse
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
OUTL = HERE / 'out' / 'layers'
SEAM_SNAP_MM = 25.0         # an existing vertex this close to a seam (along the body) is MOVED onto it; an edge crossing the seam with no
                            # such vertex at either end is split where it crosses (sparse generated meshes: a 75 cm skin edge must not be dragged)
SEAM_BAND = 0.8             # a removed/kept skin edge counts as the seam edge of a ring if it lies within this x the ring radius (+ 30 mm)
FOLD_RELAX_ITERS = 30       # after the seam snap: a free vertex of a turned-over triangle goes to the mean of its neighbours, repeated
LIMB_REPLANE_MM = 60.0      # a knee / elbow border straying more than this from flat is re-drawn by the joint plane from the rig
                            # (elf_female: her shin label ran up the outside of the thigh to the hip)
ROW_FRACS = (0.5, 0.2)      # after the seam vertices are moved onto the ring, the next rows of EXISTING vertices follow this much of their
                            # neighbours' move (no new geometry; keeps the row next to the seam from folding)
SKIN_OUT_MM = 1.0
SKIN_TO_BODY = True          # Amount 100: a layer's exposed skin lies on the game body (both directions), garments are pushed outside it
RING_TAPER_MM = 50.0        # Amount: the push fades to nothing at the rings over this distance (both layers of a joint do the same, so they
                            # still meet at any Amount; without it a collar pushed out opened a gap above the fixed neck ring)
END_CAPS = True             # close every open edge loop that is not a ring seam (where skin was cut away under a garment that runs past
                            # the ring: a tall boot / long glove / skirt) with a fan in the colour of the face at each edge
NECK_CAP = True             # close the neck ring with a skin cap on BOTH sides (body layers and the head), so a moving head never shows a hole
NECK_CAP_DOME = 0.15        # the cap bulges this x the ring radius away from its own side (a rounded stump, not a flat disc)


def _setup():
    sys.path.insert(0, str(HERE))
    import geodiv  # noqa: F401


def rings_of(body, slot, gd):
    """The slot's rings on the game body: ordered vertex loops with centre, unit axis pointing INTO the slot, radius and the parts on both sides."""
    from shell_slots import Mesh, boundary_loops
    fm = np.isin(body.part, slot)
    sub = Mesh(body.V, body.F[fm], gd.SKIN, body.part[fm], 'game')
    out = []
    for lp in boundary_loops(sub):
        lp = np.asarray(lp, np.int64)
        if len(lp) < 3:
            continue
        P = body.V[lp]
        c = P.mean(0)
        a = np.linalg.eigh(np.cov((P - c).T))[1][:, 0]
        touch = np.isin(body.F, lp).any(1)
        inside = body.V[body.F[touch & fm]].mean((0, 1))
        if (inside - c) @ a < 0:
            a = -a
        x = P - c
        r = float(np.linalg.norm(x - np.outer(x @ a, a), axis=1).mean())
        parts = sorted(set(body.part[touch].tolist()))
        out.append({'ids': lp, 'P': P, 'c': c, 'a': a, 'r': r, 'parts': parts})
    return out


def _basis(a):
    e1 = np.cross(a, [0., 0., 1.]) if abs(a[2]) < .9 else np.cross(a, [1., 0., 0.])
    e1 /= np.linalg.norm(e1)
    return e1, np.cross(a, e1)


def ring_field(body, slot, rings):
    """Per game-body vertex: signed distance along the body surface to the nearest ring of the slot (+ inside the slot) and that ring's index."""
    from scipy import sparse
    from scipy.sparse import csgraph
    E = np.unique(np.sort(np.concatenate([body.F[:, [0, 1]], body.F[:, [1, 2]], body.F[:, [2, 0]]]), axis=1), axis=0)
    w = np.linalg.norm(body.V[E[:, 0]] - body.V[E[:, 1]], axis=1) + 1e-12
    n = len(body.V)
    G = sparse.coo_matrix((np.r_[w, w], (np.r_[E[:, 0], E[:, 1]], np.r_[E[:, 1], E[:, 0]])), shape=(n, n)).tocsr()
    src = np.concatenate([R['ids'] for R in rings])
    lab = np.concatenate([[k] * len(R['ids']) for k, R in enumerate(rings)])
    dist, _, sources = csgraph.dijkstra(G, directed=False, indices=src, min_only=True, return_predecessors=True)
    ring_of = np.full(n, -1)
    okk = sources >= 0
    pos = {int(v): i for i, v in enumerate(src)}
    ring_of[okk] = lab[[pos[int(v)] for v in sources[okk]]]
    fm = np.isin(body.part, slot)
    out_v = np.zeros(n, bool)
    out_v[body.F[~fm].ravel()] = True
    in_v = np.zeros(n, bool)
    in_v[body.F[fm].ravel()] = True
    sign = np.where(in_v & ~out_v, 1., np.where(out_v & ~in_v, -1., 0.))
    return np.where(np.isfinite(dist), dist, 1e3) * sign, ring_of


def select_layer(m, slot, rings, body, gd):
    """The module's faces of the slot. Garment: whole pieces mostly in the slot (geodiv.kept_garment). Skin: by the signed distance along the game
    body from the slot's rings (ring_field, read at each vertex's closest game-body point). Snap-or-cut at each seam: a vertex within
    SEAM_SNAP_MM of the seam counts as ON it (it will be moved onto the ring, no new geometry); a skin face with vertices clearly on both sides
    is split where its edges cross the seam (a new vertex, only on edges with no on-seam vertex: sparse meshes). Returns (Mesh, seam halfedges
    [(a, b, ring, face)]: open edges of kept skin faces with both ends on the seam, near ring `ring`, notes)."""
    from shell_slots import Mesh
    nF, nV = len(m.F), len(m.V)
    garm = m.role != gd.SKIN
    keepG, _ = gd.kept_garment(m, slot, None, body)
    if not rings:                                                     # a slot with no border (the gnome's outfit: her head is a separate piece)
        member = np.where(garm, keepG, np.isin(m.part, slot))
        keep = np.flatnonzero(member)
        used = np.unique(m.F[keep])
        idx = np.full(nV, -1)
        idx[used] = np.arange(len(used))
        return Mesh(m.V[used], idx[m.F[keep]], m.role[keep], m.part[keep], m.prov[keep]), [], {'seam_edges': 0}
    sb, rb = ring_field(body, slot, rings)
    _, tri, w = body.closest(m.V)
    sv = (w * sb[body.F[tri]]).sum(1)
    rv = rb[body.F[tri][np.arange(nV), np.argmax(w, axis=1)]]
    on = np.abs(sv) < SEAM_SNAP_MM / 1000
    s2 = np.where(on, 0., sv)
    V = [*m.V]
    on_l, ring_l = [*on], [*rv]
    cross = {}
    F, src = [], []
    split = 0

    def xv(a_, b_):
        key = (min(a_, b_), max(a_, b_))
        if key not in cross:
            t = s2[a_] / (s2[a_] - s2[b_])
            V.append(m.V[a_] + t * (m.V[b_] - m.V[a_]))
            on_l.append(True)
            ring_l.append(rv[a_] if abs(t) < .5 else rv[b_])
            cross[key] = len(V) - 1
        return cross[key]

    for f in range(nF):
        t3 = m.F[f]
        if garm[f]:
            if keepG[f]:
                F.append(tuple(t3))
                src.append(f)
            continue
        s = s2[t3]
        if (s >= 0).all():
            if (s > 0).any() or sv[t3].mean() > 0:
                F.append(tuple(t3))
                src.append(f)
            continue
        if (s <= 0).all():
            continue
        poly = []
        for i in range(3):
            j = (i + 1) % 3
            if s[i] >= 0:
                poly.append(int(t3[i]))
            if (s[i] > 0 > s[j]) or (s[i] < 0 < s[j]):
                poly.append(xv(int(t3[i]), int(t3[j])))
        split += 1
        for i in range(1, len(poly) - 1):
            F.append((poly[0], poly[i], poly[i + 1]))
            src.append(f)
    V = np.array(V)
    F = np.array(F, np.int64).reshape(-1, 3)
    src = np.array(src, np.int64)
    on_a, ring_a = np.array(on_l), np.array(ring_l)
    # seam halfedges: open edges of kept skin faces with both ends on the seam, near the same ring
    d = np.stack([F, np.roll(F, -1, axis=1)], axis=2).reshape(-1, 2)
    hf = np.repeat(np.arange(len(F)), 3)
    nVV = len(V)
    keys = set((d[:, 0] * nVV + d[:, 1]).tolist())
    seam, far = [], 0
    for h in np.flatnonzero(~garm[src[hf]] & on_a[d[:, 0]] & on_a[d[:, 1]]):
        a_, b_ = int(d[h, 0]), int(d[h, 1])
        if b_ * nVV + a_ in keys:
            continue
        k = int(ring_a[a_] if ring_a[a_] >= 0 else ring_a[b_])
        if k < 0:
            continue
        R = rings[k]
        A, B = R['P'], np.roll(R['P'], -1, 0)
        ok = True
        for v in (a_, b_):
            t_ = np.clip(((V[v] - A) * (B - A)).sum(1) / np.maximum(((B - A) ** 2).sum(1), 1e-18), 0, 1)
            if np.linalg.norm(A + t_[:, None] * (B - A) - V[v], axis=1).min() > SEAM_BAND * R['r'] + .03:
                ok = False
        if not ok:
            far += 1
            continue
        seam.append((a_, b_, k, int(hf[h])))
    used = np.unique(F)
    idx = np.full(nVV, -1)
    idx[used] = np.arange(len(used))
    lay = Mesh(V[used], idx[F], m.role[src], m.part[src], m.prov[src])
    seam = [(int(idx[a_]), int(idx[b_]), k, f) for a_, b_, k, f in seam]
    return lay, seam, {'seam_edges': len(seam), 'seam_edges_too_far': far, 'faces_split': split, 'vertices_added': len(cross)}


def _arc(P):
    """Cumulative arc length at each vertex of the closed polyline P, and its total length."""
    L = np.linalg.norm(np.roll(P, -1, 0) - P, axis=1)
    return np.r_[0., np.cumsum(L)[:-1]], float(L.sum())


def _arc_of(X, P):
    """Arc position (closed polyline P) of the closest point to each of X."""
    cum, L = _arc(P)
    A, B = P, np.roll(P, -1, 0)
    AB = B - A
    t = np.clip(((X[:, None] - A[None]) * AB[None]).sum(2) / np.maximum((AB ** 2).sum(1), 1e-18)[None], 0, 1)
    D = np.linalg.norm(A[None] + t[..., None] * AB[None] - X[:, None], axis=2)
    j = D.argmin(1)
    return cum[j] + t[np.arange(len(X)), j] * np.linalg.norm(AB[j], axis=1), cum, L


SEAM_ORDERED = False         # True: a vertex that would step back against its run merges into the previous one (made 23 cm moves where a
                             # skin tongue crosses the seam and its edge runs out and back); False: plain nearest ring vertex


def _ordered(jr, K):
    cyc = lambda x: (x + K // 2) % K - K // 2                       # noqa: E731  signed step round the ring
    sgn = 1 if cyc(np.diff(jr)).sum() >= 0 else -1
    j = [int(jr[0])]
    for t in range(1, len(jr)):
        j.append(int(jr[t]) if cyc(jr[t] - j[-1]) * sgn >= 0 else j[-1])
    return np.array(j)


def snap_to_rings(lay, seam, rings, gd):
    """Make the layer end EXACTLY on the game body's ring vertices by MOVING its existing seam vertices onto them (no slicing, no ribbon):
    along each run of seam edges, every vertex goes to the ring vertex nearest to it along the ring (kept in order along the run); vertices that
    land on the same ring vertex merge (the triangles between them vanish). Only where a seam edge would skip a ring vertex (this model has
    fewer vertices there than the ring) is its triangle split into a fan through the skipped ring vertices: new triangles, no new positions.
    The next rows of existing vertices follow part of the move (ROW_FRACS). Returns (Mesh, notes)."""
    from shell_slots import Mesh
    V0 = lay.V
    nV = len(V0)
    V = [*V0]
    ring_ids = []
    for R in rings:
        ring_ids.append(np.arange(len(V), len(V) + len(R['P'])))
        V.extend(R['P'])
    V = np.array(V)
    target = np.full(nV, -1)
    notes = []
    for k, R in enumerate(rings):
        hs = [(a, b) for a, b, kk, f in seam if kk == k]
        nk = {'ring': k, 'seam_edges': len(hs)}
        notes.append(nk)
        if not hs:
            continue
        K = len(R['P'])
        verts = np.unique(np.array(hs).ravel())
        s_all, cum, L = _arc_of(V0[verts], R['P'])
        s_of = dict(zip(verts.tolist(), s_all.tolist()))
        nxt = {}
        prv = {}
        for a, b in hs:
            nxt.setdefault(a, b)
            prv.setdefault(b, a)
        seen = set()
        chains = []
        for v0 in sorted(verts.tolist(), key=lambda v: v in prv):     # chain starts (no predecessor) first, then cycles
            if v0 in seen:
                continue
            ch = [v0]
            seen.add(v0)
            while ch[-1] in nxt and nxt[ch[-1]] not in seen:
                ch.append(nxt[ch[-1]])
                seen.add(ch[-1])
            chains.append(ch)
        for ch in chains:
            jr = np.round(np.interp(np.array([s_of[v] for v in ch]) % L, np.r_[cum, L], np.r_[np.arange(K), K])).astype(int) % K
            j = jr if not SEAM_ORDERED else _ordered(jr, K)          # each vertex to its OWN nearest ring vertex
            for v, jj in zip(ch, j):
                if target[v] < 0:
                    target[v] = ring_ids[k][jj]
        nk['chains'] = len(chains)
    moved = np.flatnonzero(target >= 0)
    disp = np.zeros_like(V0)
    disp[moved] = V[target[moved]] - V0[moved]
    # next rows: existing vertices follow part of their moved neighbours' displacement
    E = np.unique(np.sort(np.concatenate([lay.F[:, [0, 1]], lay.F[:, [1, 2]], lay.F[:, [2, 0]]]), axis=1), axis=0)
    done = target >= 0
    for frac in ROW_FRACS:
        acc = np.zeros_like(V0)
        cnt = np.zeros(nV)
        for x, y in ((E[:, 0], E[:, 1]), (E[:, 1], E[:, 0])):
            sel = done[x] & ~done[y]
            np.add.at(acc, y[sel], disp[x[sel]])
            np.add.at(cnt, y[sel], 1)
        row = (cnt > 0) & ~done
        disp[row] = frac * acc[row] / cnt[row, None]
        done = done | row
    V[:nV] = V0 + disp
    remap = np.arange(len(V))
    remap[moved] = target[moved]
    F = remap[lay.F]
    role, part, prov = [*lay.role], [*lay.part], [*lay.prov]
    Fl = [tuple(f) for f in F]
    drop = set()
    inserted = 0
    for a, b, k, f in seam:
        ra, rb_ = remap[a], remap[b]
        if ra == rb_ or ra < nV or rb_ < nV:
            continue
        K = len(rings[k]['P'])
        i, j = ra - ring_ids[k][0], rb_ - ring_ids[k][0]
        fw, bw = (j - i) % K, (i - j) % K
        if min(fw, bw) <= 1:
            continue
        step = 1 if fw <= bw else -1
        path = [ring_ids[k][(i + step * t) % K] for t in range(min(fw, bw) + 1)]
        tri = list(Fl[f])
        if ra not in tri or rb_ not in tri:
            continue
        r0 = tri.index(ra)
        tri = tri[r0:] + tri[:r0]
        if tri[1] != rb_:
            continue
        wv = tri[2]
        drop.add(f)
        for t in range(len(path) - 1):
            Fl.append((path[t], path[t + 1], wv))
            role.append(lay.role[f])
            part.append(lay.part[f])
            prov.append(lay.prov[f])
            inserted += 1
    keep = [i for i in range(len(Fl)) if i not in drop]
    F = np.array([Fl[i] for i in keep], np.int64)
    role, part, prov = np.array(role)[keep], np.array(part)[keep], np.array(prov)[keep]
    ok = (F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])
    # folds: surviving original faces whose normal turned over; relax their free vertices (not on a ring) towards their neighbours
    n0 = np.cross(V0[lay.F[:, 1]] - V0[lay.F[:, 0]], V0[lay.F[:, 2]] - V0[lay.F[:, 0]])
    F_orig_new = remap[lay.F]
    alive0 = (F_orig_new[:, 0] != F_orig_new[:, 1]) & (F_orig_new[:, 1] != F_orig_new[:, 2]) & (F_orig_new[:, 0] != F_orig_new[:, 2])
    fixed = np.zeros(len(V), bool)
    fixed[nV:] = True                                                 # ring vertices never move
    Ea = np.unique(np.sort(np.concatenate([F[ok][:, [0, 1]], F[ok][:, [1, 2]], F[ok][:, [2, 0]]]), axis=1), axis=0)
    folded_first = None
    for it in range(FOLD_RELAX_ITERS):
        n1 = np.cross(V[F_orig_new[:, 1]] - V[F_orig_new[:, 0]], V[F_orig_new[:, 2]] - V[F_orig_new[:, 0]])
        bad = alive0 & ((n0 * n1).sum(1) < 0)
        folded_first = int(bad.sum()) if folded_first is None else folded_first
        if not bad.any():
            break
        free = np.unique(F_orig_new[bad])
        free = free[~fixed[free]]
        if not len(free):
            break
        acc = np.zeros_like(V)
        cnt = np.zeros(len(V))
        for x, y in ((Ea[:, 0], Ea[:, 1]), (Ea[:, 1], Ea[:, 0])):
            np.add.at(acc, x, V[y])
            np.add.at(cnt, x, 1)
        V[free] = acc[free] / np.maximum(cnt[free], 1)[:, None]
    n1 = np.cross(V[F_orig_new[:, 1]] - V[F_orig_new[:, 0]], V[F_orig_new[:, 2]] - V[F_orig_new[:, 0]])
    alive = (F_orig_new[:, 0] != F_orig_new[:, 1]) & (F_orig_new[:, 1] != F_orig_new[:, 2]) & (F_orig_new[:, 0] != F_orig_new[:, 2])
    folded = int((alive & ((n0 * n1).sum(1) < 0)).sum())
    m2 = Mesh(V, F[ok], role[ok], part[ok], prov[ok])
    summary = {'rings': notes, 'vertices_moved': int(len(moved)), 'merged_away': int(len(moved) - len(np.unique(target[moved]))),
               'faces_collapsed': int((~ok).sum()), 'triangles_inserted': inserted, 'faces_folded': folded, 'faces_folded_before_relax': folded_first,
               'max_move_mm': round(float(np.linalg.norm(disp[moved], axis=1).max() * 1000) if len(moved) else 0., 1)}
    return m2, summary


def neck_rings(body, rings, gd, head_slot=False):
    """The rings between the head and the body: for the head slot all its rings, otherwise the rings touching a head part."""
    if head_slot:
        return list(rings)
    hp = set(gd.parts(body, 'head.x'))
    return [R for R in rings if hp & set(R['parts'])]


def add_caps(lay, U1, caps, gd):
    """Each cap closes one ring with a fan of skin faces (prov 'cap') from a centre point domed NECK_CAP_DOME x the ring radius away from the
    layer's own side (R['a'] points into the layer's slot). The ring's own vertices are used (added if the layer has garment over that stretch
    of ring), so the cap ends exactly on the ring. Returns (Mesh, U1 or None, notes)."""
    from shell_slots import Mesh
    V = [*lay.V]
    U = [*U1] if U1 is not None else None
    q = {tuple(x): i for i, x in enumerate(np.round(lay.V * 1e7).astype(np.int64))}
    F, notes = [], []
    for R in caps:
        ids = []
        for P in R['P']:
            key = tuple(np.round(P * 1e7).astype(np.int64))
            if key not in q:
                q[key] = len(V)
                V.append(P)
                if U is not None:
                    U.append(P)
            ids.append(q[key])
        out = -R['a']
        c = R['P'].mean(0) + out * NECK_CAP_DOME * R['r']
        ci = len(V)
        V.append(c)
        if U is not None:
            U.append(c)
        T = np.array([(ci, ids[i], ids[(i + 1) % len(ids)]) for i in range(len(ids))], np.int64)
        Vv = np.array(V)
        n = np.cross(Vv[T[:, 1]] - Vv[T[:, 0]], Vv[T[:, 2]] - Vv[T[:, 0]])
        if (n @ out).sum() < 0:
            T = T[:, [0, 2, 1]]
        F.extend(T.tolist())
        notes.append({'ring_vertices': len(ids), 'radius_mm': round(R['r'] * 1000, 1)})
    if not F:
        return lay, U1, notes
    F = np.array(F, np.int64)
    part = int(np.bincount(lay.part[lay.part >= 0]).argmax()) if (lay.part >= 0).any() else 0
    m = Mesh(np.array(V), np.concatenate([lay.F, F]), np.r_[lay.role, [gd.SKIN] * len(F)], np.r_[lay.part, [part] * len(F)],
             np.r_[lay.prov, ['cap'] * len(F)])
    return m, (np.array(U) if U is not None else None), notes


def add_end_caps(lay, U1, ring_v, gd):
    """Close every opening of the layer that is not a ring seam: where skin was cut away under a garment that runs past its ring (a tall boot,
    a long glove, a skirt, a collar) the garment would otherwise be hollow. No loop tracing (these boundaries are often not clean loops: runs
    that touch at a vertex, inconsistent winding): every OPEN edge is taken as it is, open edges that share vertices form one opening, and each
    open edge of an opening is fanned to the opening's centre (length-weighted mean of its edges). A seam edge (both ends ring vertices) is
    not fanned (the neighbouring layer attaches there) and an opening made only of seam edges is skipped. Each fan triangle takes the role
    (colour) and part of the face owning its edge and is wound opposite to that face's edge, so it matches it; whatever is worn next to the
    layer passes through the cap (a leg into a boot). The centre moves with the Amount morph. prov 'endcap'."""
    from shell_slots import Mesh
    n = len(lay.V)
    d = np.stack([lay.F, np.roll(lay.F, -1, axis=1)], axis=2).reshape(-1, 2)          # halfedge h: face h // 3
    key = np.sort(d, axis=1)
    _, inv, cnt = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    hs = np.flatnonzero(cnt[inv.ravel()] == 1)                                       # open halfedges (their edge has one face)
    notes = {'openings': 0, 'seams': 0, 'capped': 0, 'cap_edges': 0, 'largest_mm': 0.}
    if not len(hs):
        return lay, U1, notes
    ring = np.zeros(n, bool)
    ring[ring_v] = True
    parent = np.arange(n)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a_, b_ in d[hs]:
        ra, rb = find(a_), find(b_)
        if ra != rb:
            parent[ra] = rb
    comp = np.array([find(a_) for a_ in d[hs, 0]])
    seam = ring[d[hs, 0]] & ring[d[hs, 1]]
    V, U = [*lay.V], ([*U1] if U1 is not None else None)
    F, role, part = [], [], []
    for cid in np.unique(comp):
        sel = hs[comp == cid]
        notes['openings'] += 1
        own = ~seam[comp == cid]
        verts = np.unique(d[sel])
        if not own.any() or len(verts) < 3:
            notes['seams'] += int(not own.any())
            continue
        A, B = lay.V[d[sel, 0]], lay.V[d[sel, 1]]
        w = np.linalg.norm(B - A, axis=1) + 1e-12
        c = len(V)
        V.append(((A + B) / 2 * w[:, None]).sum(0) / w.sum())
        if U is not None:
            UA, UB = U1[d[sel, 0]], U1[d[sel, 1]]
            U.append(((UA + UB) / 2 * w[:, None]).sum(0) / w.sum())
        for h in sel[own]:
            a_, b_ = d[h]
            F.append((b_, a_, c))
            role.append(lay.role[h // 3])
            part.append(lay.part[h // 3])
        notes['capped'] += 1
        notes['cap_edges'] += int(own.sum())
        notes['largest_mm'] = max(notes['largest_mm'], round(float(np.ptp(lay.V[verts], axis=0).max()) * 1000, 0))
    if not F:
        return lay, U1, notes
    m = Mesh(np.array(V), np.concatenate([lay.F, np.array(F, np.int64)]), np.r_[lay.role, role], np.r_[lay.part, part],
             np.r_[lay.prov, ['endcap'] * len(F)])
    return m, (np.array(U) if U is not None else None), notes


def amount_morph(lay, body, ring_v, gd, rings=()):
    """Amount 100: generated skin inside the game body goes out onto it, then the garment is pushed out of the body (geodiv.fill_grouped) with the
    pieces.py layer / fold guards. Ring vertices never move."""
    import pieces as pc
    from shell_slots import Mesh
    V = lay.V.copy()
    skin_v = np.unique(lay.F[lay.role == gd.SKIN])
    gar_v = np.unique(lay.F[lay.role != gd.SKIN])
    sv = np.setdiff1d(skin_v, np.r_[gar_v, ring_v])
    notes = {}
    if len(sv):
        d, tri, w = body.closest(V[sv])
        Q = w[:, :1] * body.A[tri] + w[:, 1:2] * body.B[tri] + w[:, 2:] * body.C[tri]
        n = body.tri_n[tri]
        if SKIN_TO_BODY:                                              # all exposed skin onto the body: any neighbour's garment (pushed out of
            V[sv] = Q + n * SKIN_OUT_MM / 1000                       # the body) is then outside it (elf: thigh skin over a tall boot's top)
            notes['skin_onto_body'] = int(len(sv))
        else:
            inside = ((V[sv] - Q) * n).sum(1) < SKIN_OUT_MM / 1000
            V[sv[inside]] = Q[inside] + n[inside] * SKIN_OUT_MM / 1000
            notes['skin_moved_out'] = int(inside.sum())
    tmp = Mesh(V, lay.F, lay.role, lay.part, lay.prov)
    margin = gd.fl.DEFAULTS['margin_pct'] / 100 * body.H
    U1, notes['fill'] = gd.fill_grouped(tmp, body, margin)
    if RING_TAPER_MM > 0 and len(rings) and len(ring_v):
        from scipy import sparse
        from scipy.sparse import csgraph
        E = np.unique(np.sort(np.concatenate([lay.F[:, [0, 1]], lay.F[:, [1, 2]], lay.F[:, [2, 0]]]), axis=1), axis=0)
        wt = np.linalg.norm(lay.V[E[:, 0]] - lay.V[E[:, 1]], axis=1) + 1e-12
        nV = len(lay.V)
        G = sparse.coo_matrix((np.r_[wt, wt], (np.r_[E[:, 0], E[:, 1]], np.r_[E[:, 1], E[:, 0]])), shape=(nV, nV)).tocsr()
        d = csgraph.dijkstra(G, directed=False, indices=ring_v, min_only=True, limit=RING_TAPER_MM / 1000 * 1.01)
        d = np.where(np.isfinite(d), d, 1e3)                         # distance ALONG THE LAYER from the seam: a garment that only overlaps the
        x = np.clip(d / (RING_TAPER_MM / 1000), 0, 1)                # seam (a tall boot) is not connected to it and is pushed out in full
        w = x * x * (3 - 2 * x)
        U1 = lay.V + (U1 - lay.V) * w[:, None]
        notes['ring_taper_vertices'] = int((w < 1).sum())
    notes['fill_layer_guard'] = pc.fill_layer_guard(lay, U1, gd)
    notes['fold_guard'] = fold_guard(lay, U1)
    U1[ring_v] = lay.V[ring_v]
    return U1, notes


def fold_guard(m, U1, rounds=10, deg=90.):
    """Any face (skin or garment) the Amount morph turns further than `deg` has its vertices' moves halved until none is (in place)."""
    n0 = np.cross(m.V[m.F[:, 1]] - m.V[m.F[:, 0]], m.V[m.F[:, 2]] - m.V[m.F[:, 0]])
    D = U1 - m.V
    first = None
    bad = np.zeros(len(m.F), bool)
    for it in range(rounds):
        X = m.V + D
        n1 = np.cross(X[m.F[:, 1]] - X[m.F[:, 0]], X[m.F[:, 2]] - X[m.F[:, 0]])
        cs = (n0 * n1).sum(1) / np.maximum(np.linalg.norm(n0, axis=1) * np.linalg.norm(n1, axis=1), 1e-18)
        bad = cs < np.cos(np.radians(deg))
        first = int(bad.sum()) if first is None else first
        if not bad.any():
            break
        D[np.unique(m.F[bad])] *= 0. if it == rounds - 1 else .5
    U1[:] = m.V + D
    return {'faces_folded': first, 'left_folded': int(bad.sum())}


def build_layer(m, slot, body, gd):
    rings = rings_of(body, slot, gd)
    lay, seam, selnotes = select_layer(m, slot, rings, body, gd)
    if not len(lay.F) or not (lay.role != gd.SKIN).any():
        return None, None, {'skipped': 'no garment in this slot'}
    lay, snotes = snap_to_rings(lay, seam, rings, gd)
    lay = gd.compact(lay)
    rq = {tuple(x) for R in rings for x in np.round(R['P'] * 1e7).astype(np.int64)}
    ring_v = np.flatnonzero([tuple(x) in rq for x in np.round(lay.V * 1e7).astype(np.int64)])
    E = {tuple(sorted(e)) for f in lay.F.tolist() for e in ((f[0], f[1]), (f[1], f[2]), (f[2], f[0]))}
    q = {tuple(x): i for i, x in enumerate(np.round(lay.V * 1e7).astype(np.int64))}
    for R, nk in zip(rings, snotes['rings']):
        ids = [q.get(tuple(x)) for x in np.round(R['P'] * 1e7).astype(np.int64)]
        n = len(ids)
        nk['ring_edges_joined'] = f"{sum(1 for i in range(n) if ids[i] is not None and ids[(i + 1) % n] is not None and tuple(sorted((ids[i], ids[(i + 1) % n]))) in E)}/{n}"
    U1, anotes = amount_morph(lay, body, ring_v, gd, rings)
    cnotes, enotes = [], {}
    if NECK_CAP:
        lay, U1, cnotes = add_caps(lay, U1, neck_rings(body, rings, gd), gd)
    if END_CAPS:
        rv2 = np.flatnonzero([tuple(x) in rq for x in np.round(lay.V * 1e7).astype(np.int64)])
        lay, U1, enotes = add_end_caps(lay, U1, rv2, gd)
    return lay, U1, {'select': selnotes, 'seams': snotes, 'amount': anotes, 'neck_caps': cnotes, 'end_caps': enotes, 'faces': int(len(lay.F)),
                     'ring_vertices': int(len(ring_v)), 'ring_vertices_expected': int(sum(len(R['P']) for R in rings))}


def write_layer(race, slot_key, set_name, lay, U1, rec, gd):
    from shell_slots import face_colours, glb_bytes
    d = OUTL / race
    (d / 'scenes').mkdir(parents=True, exist_ok=True)
    stem = f'{slot_key}__{set_name}'
    (d / f'{stem}.glb').write_bytes(glb_bytes(lay, U1))
    for tag, X in (('a0', lay.V), ('a100', U1 if U1 is not None else lay.V)):
        np.savez_compressed(d / 'scenes' / f'{stem}__{tag}.npz', gV=X.astype(np.float32), gF=lay.F.astype(np.int32),
                            gC=face_colours(lay, 'role').astype(np.float32))
    rec.update({'race': race, 'slot': slot_key, 'set': set_name, 'glb': f'{race}/{stem}.glb', 'faces': int(len(lay.F)), 'code_mtime': gd.CODE_MTIME})
    (d / f'{stem}.json').write_text(json.dumps(rec, default=float, indent=1), encoding='utf-8')


ISLAND_CM2 = 300.0           # a stray patch of a section smaller than this (mislabelled faces; elf_female has 213 cm2 shin patches on the thighs that
                             # made a second "knee" ring) joins the section around it; real sections are all > 900 cm2


def tidy_parts(body, gd):
    """Stray islands of a section (human_male: 2 neck faces under the jaw, elf_female: single torso faces, orc_male: shin patches) make extra
    little rings no stitch can close: each island under ISLAND_CM2 takes the part of the section around it. In place on the (cached) game body,
    once per process, the same for every layer and bare piece of the race. Real separate pieces (a halfling hand) are far bigger."""
    if getattr(body, '_tidied', False):
        return getattr(body, '_tidy_notes', {})
    from scipy import sparse
    from scipy.sparse import csgraph
    E = np.concatenate([body.F[:, [0, 1]], body.F[:, [1, 2]], body.F[:, [2, 0]]])
    fi = np.tile(np.arange(len(body.F)), 3)
    k = np.sort(E, 1)
    o = np.lexsort((k[:, 1], k[:, 0]))
    ks, fs = k[o], fi[o]
    same = np.r_[False, (ks[1:] == ks[:-1]).all(1)]
    pa, pb = fs[np.flatnonzero(same) - 1], fs[same]
    area = np.linalg.norm(np.cross(body.V[body.F[:, 1]] - body.V[body.F[:, 0]], body.V[body.F[:, 2]] - body.V[body.F[:, 0]]), axis=1) / 2
    moved = 0
    for _ in range(4):
        S = slots(body, gd, tidy=False)
        sid = np.full(len(body.F), -1)
        for i, key in enumerate(('head', 'torso', 'lower_arm.r', 'lower_arm.l', 'lower_leg.r', 'lower_leg.l')):
            if key in S:
                sid[np.isin(body.part, S[key])] = i
        m = sid[pa] == sid[pb]
        n, lab = csgraph.connected_components(sparse.coo_matrix((np.ones(m.sum()), (pa[m], pb[m])), shape=(len(body.F),) * 2), directed=False)
        ca = np.bincount(lab, area, n)
        small = np.flatnonzero(ca < ISLAND_CM2 / 1e4)
        changed = 0
        for c in small:
            f = np.flatnonzero(lab == c)
            nb = np.r_[pb[np.isin(pa, f)], pa[np.isin(pb, f)]]
            nb = nb[sid[nb] != sid[f[0]]]
            if not len(nb):
                continue                                              # a separate piece of the mesh, not a stray patch
            body.part[f] = np.bincount(body.part[nb]).argmax()
            changed += len(f)
        moved += changed
        if not changed:
            break
    body._tidied = True
    body._tidy_notes = {'faces_relabelled': int(moved), 'limbs_replaned': replane_limbs(body, gd)}
    return body._tidy_notes


def get_body(race, gd):
    """The game body of a race, tagged with its race (replane_limbs reads the race's rig)."""
    body = gd.game_body(race)
    body._race = race
    return body


def replane_limbs(body, gd):
    """A knee / elbow border that strays more than LIMB_REPLANE_MM from flat (a mislabelled base: elf_female's shin label runs up the outside
    of the thigh) is re-drawn: the faces of the two limb parts go to the upper or lower part by the side of the plane through the joint (rig:
    head of the lower bone), normal halfway between the upper and the lower bone. In place, on the cached body."""
    race = getattr(body, '_race', None)
    if race is None:
        return []
    rp = gd.bf.BASE_CACHE / f'{race}_rig.npz'
    if not rp.is_file():
        return []
    z = np.load(rp, allow_pickle=True)
    bn = [str(x) for x in z['bone_names']]
    unit = lambda x: x / max(np.linalg.norm(x), 1e-12)               # noqa: E731
    done = []
    cent = body.V[body.F].mean(1)
    for side in 'lr':
        for up, lo, slot_key in (('thigh_stretch', 'leg_stretch', 'lower_leg'), ('arm_stretch', 'forearm_stretch', 'lower_arm')):
            S = slots(body, gd, tidy=False)
            key = f'{slot_key}.{side}'
            un, ln = f'{up}.{side}', f'{lo}.{side}'
            if key not in S or un not in body.part_names or ln not in body.part_names or un not in bn or ln not in bn:
                continue
            rings = rings_of(body, S[key], gd)
            dev = max((float(np.abs((R['P'] - R['c']) @ R['a']).max()) for R in rings), default=0.)
            if dev < LIMB_REPLANE_MM / 1000:
                continue
            joint = z['bone_head'][bn.index(ln)]
            n = unit(unit(joint - z['bone_head'][bn.index(un)]) + unit(z['bone_tail'][bn.index(ln)] - joint))
            ui, li = body.part_names.index(un), body.part_names.index(ln)
            sel = np.isin(body.part, [ui, li])
            body.part[sel] = np.where((cent[sel] - joint) @ n > 0, li, ui)
            done.append({'limb': key, 'border_deviation_mm': round(dev * 1000), 'faces': int(sel.sum())})
            body._replanes = getattr(body, '_replanes', []) + [(joint, n, ui, li)]
    return done


def slots(body, gd, tidy=True):
    if tidy:
        tidy_parts(body, gd)
    P = lambda *n: gd.parts(body, *n)  # noqa: E731
    S = {'head': P('head.x')}
    for s in 'lr':
        S[f'lower_arm.{s}'] = gd.lower_arm(body, s)
        S[f'lower_leg.{s}'] = gd.lower_leg(body, s)
    low = sum((S[f'lower_arm.{s}'] + S[f'lower_leg.{s}'] for s in 'lr'), [])
    S['torso'] = [p for p in gd.all_parts(body) if p not in S['head'] + low]
    S['outfit'] = [p for p in gd.all_parts(body) if p not in S['head']]
    return {k: v for k, v in S.items() if v}


def prep(mod, body, race, gd):
    """The aligned generated module (white-anchor alignment as before) with its skin labels cleaned; no rim snapping onto the game body."""
    rig = gd.ab.Rig.from_npz(gd.bf.BASE_CACHE / f'{race}_rig.npz')
    m, info = gd.load_shell(mod, gd.bf.get_body(race), rig)
    slots(body, gd)                                                   # makes sure the body's own part fixes are done
    for joint, n, ui, li in getattr(body, '_replanes', []):          # the module carries the base's part labels: same joint-plane fix
        sel = np.isin(m.part, [ui, li])
        m.part[sel] = np.where((m.V[m.F[sel]].mean(1) - joint) @ n > 0, li, ui)
        info.setdefault('limb_parts_replaned', 0)
        info['limb_parts_replaned'] += int(sel.sum())
    info['small_skin_relabelled'] = gd.relabel_small_skin(m)
    info['off_body_skin_relabelled'] = gd.relabel_off_body_skin(m, body)
    return m, info


def task_bare(race):
    import geodiv as gd
    body = get_body(race, gd)
    S = slots(body, gd)
    for k, ids in S.items():
        if k == 'outfit':
            continue
        g = gd.game_faces(body, ids)
        cn = []
        if NECK_CAP and k in ('head', 'torso'):
            g, _, cn = add_caps(g, None, neck_rings(body, rings_of(body, ids, gd), gd, head_slot=k == 'head'), gd)
        write_layer(race, k, 'bare', g, None, {'kind': 'bare', 'parts': [body.part_names[i] for i in ids], 'neck_caps': cn}, gd)
    return f'{race}: bare layers'


def task_module(mod):
    import geodiv as gd
    t0 = time.monotonic()
    mod = Path(mod).resolve()
    s, j, race, module = gd.module_identity(mod)
    body = get_body(race, gd)
    S = slots(body, gd)
    m, info = prep(mod, body, race, gd)
    rel = mod.relative_to(gd.ROOT).as_posix()
    made = []

    def one(slot_key, mm, extra):
        lay, U1, notes = build_layer(mm, S[slot_key], body, gd)
        if lay is None:
            made.append(f'{slot_key}: {notes["skipped"]}')
            return
        write_layer(race, slot_key, s, lay, U1, {'kind': module, 'module': rel, 'notes': notes, 'load': info, **extra,
                                                 'seconds': round(time.monotonic() - t0, 1)}, gd)
        sn = notes['seams']
        st = ' '.join(r.get('ring_edges_joined', '-') for r in sn['rings'])
        made.append(f'{slot_key}: {notes["faces"]} faces, seams joined {st}, moved {sn["vertices_moved"]} (max {sn["max_move_mm"]} mm), '
                    f'inserted {sn["triangles_inserted"]}, folded {sn["faces_folded"]}')

    if module == 'outfit':
        one('outfit', m, {})
    elif module == 'body_armor':
        one('torso', m, {})
    elif module == 'right_gauntlet_boot':
        if gd.CENTRE_LIMBS:
            info['centred_on_game_limbs'] = gd.centre_limb_pieces(m, body, 'r')
        x0 = gd.fl.mirror_plane(race, body)[0]
        ml = gd.mirror_mesh(m, x0, body)
        for side, mm in (('r', m), ('l', ml)):
            for limb in ('lower_arm', 'lower_leg'):
                if f'{limb}.{side}' in S:
                    one(f'{limb}.{side}', mm, {'mirrored_from_right': side == 'l'})
    return f'{race} {s} {module}: ' + '; '.join(made) + f' ({time.monotonic() - t0:.0f} s)'


def _run(task):
    kind, arg = task
    try:
        return task_bare(arg) if kind == 'bare' else task_module(arg)
    except Exception as error:  # noqa: BLE001
        import traceback
        return f'FAILED {arg}: {type(error).__name__}: {error}\n{traceback.format_exc(limit=4)}'


def write_index():
    recs = []
    for p in sorted(OUTL.glob('*/*.json')):
        r = json.loads(p.read_text(encoding='utf-8'))
        recs.append({k: r.get(k) for k in ('race', 'slot', 'set', 'glb', 'kind', 'faces', 'mirrored_from_right')})
    (OUTL / 'index.json').write_text(json.dumps({'pieces': recs}, indent=0), encoding='utf-8')
    return len(recs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--races', default='')
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--gpu', action='store_true')
    ap.add_argument('--only', default='')
    a = ap.parse_args()
    if a.gpu:
        os.environ['BODY_FIT_GPU'] = '1'
    os.environ.setdefault('OMP_NUM_THREADS', '1')
    sys.path.insert(0, str(HERE))
    import process_all as pa
    races = [r for r in a.races.split(',') if r] or None
    mods = []
    for jb in pa.jobs(races):
        for m in jb['modules']:
            if a.only in m and m not in mods:
                mods.append(m)
    rs = sorted({m.split('/')[3] for m in mods} | set(races or []))
    tasks = [('bare', r) for r in rs] + [('module', str(pa.ROOT / m)) for m in mods]
    OUTL.mkdir(parents=True, exist_ok=True)
    print(time.strftime('%H:%M:%S'), f'{len(tasks)} tasks, {a.workers} workers', flush=True)
    with mp.Pool(a.workers, initializer=_setup) as pool:
        for msg in pool.imap_unordered(_run, tasks):
            print(time.strftime('%H:%M:%S'), msg, flush=True)
    print(time.strftime('%H:%M:%S'), 'index:', write_index(), 'layers', flush=True)


if __name__ == '__main__':
    main()
