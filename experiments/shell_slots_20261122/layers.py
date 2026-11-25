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
FACE_PUSH_ITERS = 12        # Amount 100: garment FACES are pushed clear of the base body (low-poly garments: a body point - the sharp knee -
                            # can pierce the middle of a big flat face whose corners are all outside); repeated until nothing pierces
FACE_PUSH_MIN_COS = 0.5     # (b) only: a face more than 60 deg off facing the body has no meaningful footprint along the body normal
FACE_PUSH_POKE_MM = 30.0    # (b) only: a body point counts as poking through a face when it is at most this far in front of it (a knee tip
                            # through a cuff); a tilted knee plate 'in front' of a knee point 7 cm away was shoved 7 cm
FACE_PUSH_SEAM_MM = 80.0    # the face push only acts where the base body can show next to the layer: garment faces within this of a seam, or
                            # past a seam in the neighbour's section (a boot top over the thigh). Inside its own section the base body is never
                            # drawn, and pushing there (crotch, toes) along a sideways body normal made flaps and spikes
FACE_PUSH_SPREAD = 4        # each round's pushes spread this many vertex rings over the layer (a vertex takes its neighbours' mean push when
                            # that is larger): patches move out together as a bulge; single vertices dragged out on a thin plate made spikes
FACE_PUSH_REACH_MM = 80.0   # a body point up to this far in front of a garment face counts as piercing it
LAYER_GUARD = False         # pieces.fill_layer_guard (a move stops short of another garment surface): off, user 2026-11-24 ('we don't need that guard')
INFLATE_LAYERS = True       # Amount 100 starts with geodiv.inflate_to_body for a layer generated far too slim (the deepest 5 % of its garment
                            # more than INFLATE_TRIGGER_MM inside the body: the gnome): a smooth widening from the body axis. Without it the
                            # vertex push moved gnome torsos up to 15 cm point by point and tore them into spikes. (The slider is gone, not the step.)
LINE_SNAP_MM = 0.0          # OFF 2026-11-25 (made overlapping faces on seam edges: up to 3 points put on one ring edge; the body strip now gives
                            # the seam its shape). Was 25: after the seam vertices are on the ring vertices: any other vertex of the layer this close to a joined seam's LINE
                            # is moved onto that line (vertex to line: onto the nearest ring edge, or merged into a ring vertex when it lands
                            # within LINE_MERGE_MM of one). The row 1-2 cm above the knee seam made a thin shelf all round the join;
                            # triangles left with all three corners on one ring edge (zero area) are KEPT (the open edge stays the ring edge)
LINE_MERGE_MM = 8.0
FOLD_COLLAPSE = True        # seam folds the relax cannot undo: the folded face's free corner is merged into its neighbouring ring vertex
BODY_BAND = True            # at each seam the layer's own skin reaches all round: the strip of game-body faces from
                            # the seam to the body's next row of vertices goes into the layer as it is, and the generated skin is cut and
                            # snapped at that next row instead. Both sides of the seam then have the body's exact shape (a big generated
                            # face cut across the knee bulge right above the seam: a notch, even with all seam points shared)
SEAM_FULL_MM = 80.0         # Amount 0: exposed skin this close to a seam (along the layer) goes all the way onto the body, fading out by
SEAM_BLEND_MM = 140.0        # Amount 0 too: exposed skin of the layer within this (along the layer) of a seam is moved onto the game body,
                            # fully at the seam, fading to its generated place at this distance (the generator's thigh sat up to 30 mm
                            # inside the body 1-2 cm above the knee seam, folding the surface in right at the join). Moves points only.
SKIN_TO_BODY = True          # Amount 100: a layer's exposed skin lies on the game body (both directions), garments are pushed outside it
RING_TAPER_MM = 50.0        # Amount: the push fades to nothing at the rings over this distance (both layers of a joint do the same, so they
                            # still meet at any Amount; without it a collar pushed out opened a gap above the fixed neck ring)
END_CAPS = True             # close every open edge loop that is not a ring seam (where skin was cut away under a garment that runs past
                            # the ring: a tall boot / long glove / skirt) with a fan in the colour of the face at each edge
NECK_CAP = True             # close the neck ring with a skin cap on BOTH sides (body layers and the head), so a moving head never shows a hole
RING_CAPS = True            # every seam ring is capped like the neck: bare layers cap all their rings, a layer caps the rings its own skin
                            # reaches. A seam a neighbour's garment covers (a bare forearm's top inside a short sleeve) is then closed, not a
                            # hole into the limb; where both sides are skin the two caps sit inside the limb, unseen
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


def select_layer(m, slot, rings, body, gd, field_body=None):
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
    sb, rb = ring_field(field_body if field_body is not None else body, slot, rings)
    fbody = field_body if field_body is not None else body
    _, tri, w = fbody.closest(m.V)
    sv = (w * sb[fbody.F[tri]]).sum(1)
    rv = rb[fbody.F[tri][np.arange(nV), np.argmax(w, axis=1)]]
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
    # a kept GARMENT face sharing an edge that a skin face was split on gets the same split (else it keeps the whole edge while the skin has
    # a vertex in its middle: a T-junction that opens into a crack when the seam moves that vertex)
    gsplit = 0
    for key, X in cross.items():
        for i in range(len(F)):
            if not garm[src[i]] or key[0] not in F[i] or key[1] not in F[i]:
                continue
            t = list(F[i])
            for _ in range(3):
                if {t[0], t[1]} == set(key):
                    break
                t = t[1:] + t[:1]
            if {t[0], t[1]} != set(key):
                continue
            F[i] = (t[0], X, t[2])
            F.append((X, t[1], t[2]))
            src.append(src[i])
            gsplit += 1
    V = np.array(V)
    F = np.array(F, np.int64).reshape(-1, 3)
    src = np.array(src, np.int64)
    on_a, ring_a = np.array(on_l), np.array(ring_l)
    # seam halfedges: open edges of kept skin faces with both ends on the seam, near the same ring
    d = np.stack([F, np.roll(F, -1, axis=1)], axis=2).reshape(-1, 2)
    hf = np.repeat(np.arange(len(F)), 3)
    nVV = len(V)
    und = np.sort(d, 1)
    _, inv, cnt = np.unique(und[:, 0] * nVV + und[:, 1], return_inverse=True, return_counts=True)
    inc = cnt[inv]                                                    # faces on each halfedge's (undirected) edge: open = exactly one
    seam, far, mixed = [], 0, 0
    for h in np.flatnonzero(~garm[src[hf]] & on_a[d[:, 0]] & on_a[d[:, 1]] & (inc == 1)):
        a_, b_ = int(d[h, 0]), int(d[h, 1])
        ka, kb = int(ring_a[a_]), int(ring_a[b_])
        if ka >= 0 and kb >= 0 and ka != kb:                          # the ends lie by different rings: not one seam
            mixed += 1
            continue
        k = ka if ka >= 0 else kb
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
    return lay, seam, {'seam_edges': len(seam), 'seam_edges_too_far': far, 'seam_edges_two_rings': mixed, 'faces_split': split,
                       'vertices_added': len(cross), 'garment_faces_split': gsplit}


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


def snap_to_rings(lay, seam, rings, gd, body=None):
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
    E = np.unique(np.sort(np.concatenate([lay.F[:, [0, 1]], lay.F[:, [1, 2]], lay.F[:, [2, 0]]]), axis=1), axis=0)
    done = target >= 0
    # next rows: existing vertices follow part of their moved neighbours' displacement
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
    F = remap[lay.F]                                                  # the LIVE faces; parent[i] = the layer face live face i comes from
    parent = np.arange(len(lay.F))
    ring_sets = [set(map(int, ids)) for ids in ring_ids]
    n0 = np.cross(V0[lay.F[:, 1]] - V0[lay.F[:, 0]], V0[lay.F[:, 2]] - V0[lay.F[:, 0]])   # each parent's original normal

    def distinct(Fx):
        return (Fx[:, 0] != Fx[:, 1]) & (Fx[:, 1] != Fx[:, 2]) & (Fx[:, 0] != Fx[:, 2])

    def folded(Fx, Vx, par):                                          # live faces turned over against their parent's original normal
        nn = np.cross(Vx[Fx[:, 1]] - Vx[Fx[:, 0]], Vx[Fx[:, 2]] - Vx[Fx[:, 0]])
        return distinct(Fx) & ((n0[par] * nn).sum(1) < 0)

    def defects(Fx):                                                  # (duplicate triangles, edges with more than two faces)
        Fk = Fx[distinct(Fx)]
        if not len(Fk):
            return 0, 0
        st = np.sort(Fk, 1)
        dup = len(st) - len(np.unique(st, axis=0))
        e = np.sort(np.concatenate([Fk[:, [0, 1]], Fk[:, [1, 2]], Fk[:, [2, 0]]]), axis=1)
        _, c = np.unique(e, axis=0, return_counts=True)
        return int(dup), int((c > 2).sum())

    # a seam edge whose ends landed on non-neighbouring ring vertices of ITS ring: its CURRENT triangle is fanned through the skipped ring
    # vertices. The live list is edited in place, so a triangle with two such edges is split once per edge, in turn, never twice over.
    inserted = 0
    for a, b, k, f in seam:
        ra, rb_ = int(remap[a]), int(remap[b])
        if ra == rb_ or ra not in ring_sets[k] or rb_ not in ring_sets[k]:
            continue
        K = len(rings[k]['P'])
        i, j = ra - int(ring_ids[k][0]), rb_ - int(ring_ids[k][0])
        fw, bw = (j - i) % K, (i - j) % K
        if min(fw, bw) <= 1:
            continue
        step = 1 if fw <= bw else -1
        path = [int(ring_ids[k][(i + step * t) % K]) for t in range(min(fw, bw) + 1)]
        g, t3 = None, None
        for h in np.flatnonzero((F == ra).any(1) & (F == rb_).any(1)):
            tt = [int(x) for x in F[h]]
            r0 = tt.index(ra)
            tt = tt[r0:] + tt[:r0]
            if tt[1] == rb_:
                g, t3 = h, tt
                break
        if g is None:
            continue
        new = np.array([(path[t], path[t + 1], t3[2]) for t in range(len(path) - 1)], np.int64)
        F[g] = new[0]
        F = np.vstack([F, new[1:]])
        parent = np.r_[parent, [parent[g]] * (len(new) - 1)]
        inserted += len(new) - 1
    # vertex to line (LINE_SNAP_MM): only vertices of the layer's SKIN alone (no garment face on them), within 2 edges of this ring's seam,
    # and only onto ring edges the layer actually joins (an edge it has); garment and uncovered stretches of a ring are left alone
    line_snapped = line_merged = 0
    fixed_line = set()
    if LINE_SNAP_MM > 0:
        for k, R in enumerate(rings):
            ok_ = distinct(F)
            Fo, ro = F[ok_], lay.role[parent[ok_]]
            skin_only = np.ones(len(V), bool)
            skin_only[Fo[ro != gd.SKIN].ravel()] = False
            used = np.zeros(len(V), bool)
            used[Fo.ravel()] = True
            Eu = {tuple(sorted(e)) for e in np.concatenate([Fo[:, [0, 1]], Fo[:, [1, 2]], Fo[:, [2, 0]]]).tolist()}
            ids = [int(x) for x in ring_ids[k]]
            K = len(ids)
            segs = [i for i in range(K) if tuple(sorted((ids[i], ids[(i + 1) % K]))) in Eu]
            if not segs:
                continue
            nb = {}
            for x, y in Eu:
                nb.setdefault(x, set()).add(y)
                nb.setdefault(y, set()).add(x)
            near = {x for x in ids if used[x]}
            for _ in range(2):
                near |= {y for x in list(near) for y in nb.get(x, ())}
            cand = np.array(sorted(v for v in near if v < nV and used[v] and skin_only[v] and v not in fixed_line), np.int64)
            if not len(cand):
                continue
            A = R['P'][segs]
            B = R['P'][[(i + 1) % K for i in segs]]
            AB = B - A
            X = V[cand]
            t_ = np.clip(((X[:, None] - A[None]) * AB[None]).sum(2) / np.maximum((AB ** 2).sum(1), 1e-18)[None], 0, 1)
            Q = A[None] + t_[..., None] * AB[None]
            dd = np.linalg.norm(Q - X[:, None], axis=2)
            sj = dd.argmin(1)
            dmin = dd[np.arange(len(cand)), sj]
            for c_ in np.flatnonzero(dmin < LINE_SNAP_MM / 1000):
                v = int(cand[c_])
                q = Q[c_, sj[c_]]
                i = segs[sj[c_]]
                ends = (ids[i], ids[(i + 1) % K])
                de = [np.linalg.norm(V[e] - q) for e in ends]
                if min(de) < LINE_MERGE_MM / 1000:
                    F = np.where(F == v, ends[int(np.argmin(de))], F)
                    line_merged += 1
                else:
                    V[v] = q
                    fixed_line.add(v)
                    line_snapped += 1
        # a triangle left with its three corners on one ring edge has no area and draws nothing; it is KEPT, so the layer's open edge stays the
        # ring edge itself, exactly the neighbour's (dropping it put the vertex in the middle of the open edge: a T-junction, pinhole cracks)
    fixed = np.zeros(len(V), bool)
    fixed[nV:] = True                                                 # ring vertices never move
    fixed[list(fixed_line)] = True                                    # nor do vertices put on a ring line
    # folds: LIVE faces turned over against their parent; relax their free vertices towards their neighbours, keeping the best state
    folded_first = None
    best = (None, V.copy())
    for it in range(FOLD_RELAX_ITERS + 1):
        bad = folded(F, V, parent)
        folded_first = int(bad.sum()) if folded_first is None else folded_first
        if best[0] is None or bad.sum() < best[0]:                    # a relax step can make it worse
            best = (int(bad.sum()), V.copy())
        if not bad.any() or it == FOLD_RELAX_ITERS:
            break
        free = np.unique(F[bad])
        free = free[~fixed[free]]
        if not len(free):
            break
        Fk = F[distinct(F)]
        Ea = np.unique(np.sort(np.concatenate([Fk[:, [0, 1]], Fk[:, [1, 2]], Fk[:, [2, 0]]]), axis=1), axis=0)
        acc = np.zeros_like(V)
        cnt = np.zeros(len(V))
        for x, y in ((Ea[:, 0], Ea[:, 1]), (Ea[:, 1], Ea[:, 0])):
            np.add.at(acc, x, V[y])
            np.add.at(cnt, x, 1)
        V[free] = acc[free] / np.maximum(cnt[free], 1)[:, None]
    V = best[1]
    # what the relax leaves folded: a free corner of a folded face next to a ring vertex is merged into the nearest such ring vertex (a point
    # moved onto the seam, no new geometry). Accepted only if no face that was fine turns over, the folds drop, and it adds no duplicate
    # triangle and no edge with more than two faces
    collapsed = 0
    if FOLD_COLLAPSE:
        bad = folded(F, V, parent)
        for f in np.flatnonzero(bad):
            if not folded(F[f:f + 1], V, parent[f:f + 1])[0]:
                continue
            Fk = F[distinct(F)]
            nbr = {}
            for x, y in np.concatenate([Fk[:, [0, 1]], Fk[:, [1, 2]], Fk[:, [2, 0]]]).tolist():
                nbr.setdefault(x, set()).add(y)
                nbr.setdefault(y, set()).add(x)
            for v in F[f]:
                v = int(v)
                rn = [r for r in nbr.get(v, ()) if fixed[r]]
                if fixed[v] or not rn:
                    continue
                r = min(rn, key=lambda r_: np.linalg.norm(V[r_] - V[v]))
                F2 = np.where(F == v, r, F)
                b1, b2 = folded(F, V, parent), folded(F2, V, parent)
                d1, d2 = defects(F), defects(F2)
                if b2.sum() < b1.sum() and not (b2 & ~b1).any() and d2[0] <= d1[0] and d2[1] <= d1[1]:
                    F = F2
                    collapsed += 1
                    break
    folded_n = int(folded(F, V, parent).sum())
    # exact duplicate triangles (same three vertices): keep the first
    ok = distinct(F)
    st = np.sort(F, 1)
    _, first = np.unique(st, axis=0, return_index=True)
    dup = np.ones(len(F), bool)
    dup[first] = False
    dup &= ok
    keep = ok & ~dup
    d_left = defects(F[keep])
    m2 = Mesh(V, F[keep], lay.role[parent[keep]], lay.part[parent[keep]], lay.prov[parent[keep]])
    summary = {'rings': notes, 'vertices_moved': int(len(moved)), 'merged_away': int(len(moved) - len(np.unique(target[moved]))),
               'faces_collapsed': int((~ok).sum()), 'triangles_inserted': inserted, 'faces_folded': folded_n, 'faces_folded_before_relax': folded_first,
               'fold_corners_merged_onto_ring': collapsed, 'line_snapped': line_snapped, 'line_merged': line_merged,
               'duplicate_faces_dropped': int(dup.sum()), 'non_manifold_edges': d_left[1],
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


def add_end_caps(lay, U1, seam_keys, gd):
    """Close every opening of the layer that is not a ring seam: where skin was cut away under a garment that runs past its ring (a tall boot,
    a long glove, a skirt, a collar) the garment would otherwise be hollow. No loop tracing (these boundaries are often not clean loops: runs
    that touch at a vertex, inconsistent winding): every OPEN edge is taken as it is (with the odd-count edges that continue a rim, below),
    rim edges that share vertices form one opening, and each rim edge of an opening is fanned to the opening's centre (length-weighted mean of
    its edges). Seam openings still open here are closed whole too (the closed caps only take seams joined all round; a seam left nearly joined
    would otherwise stay a hole). Each fan triangle takes the role
    (colour) and part of the face owning its edge and is wound opposite to that face's edge, so it matches it; whatever is worn next to the
    layer passes through the cap (a leg into a boot). The centre moves with the Amount morph. prov 'endcap'."""
    from shell_slots import Mesh
    n = len(lay.V)
    # edges are counted by POSITION (one vertex per position), and the rim of an opening is every edge with an ODD number of faces, not only
    # the open ones (1 face): a rim that runs on through an edge the model itself uses three times (a flap or a second sheet meeting the rim)
    # would otherwise stop there, and the fan spoke at that vertex had one triangle only (a slit). At any vertex the edges with an odd face
    # count come in pairs, so with them every spoke of the fan gets exactly its partner triangle. The fan triangle of an edge is wound against
    # the edge's unpaired direction (the one more of its faces run along)
    kq = np.round(lay.V * 1e7).astype(np.int64)
    _, first, canon = np.unique(kq, axis=0, return_index=True, return_inverse=True)
    rep = first[canon.ravel()]
    Fr = rep[lay.F]
    d = np.stack([Fr, np.roll(Fr, -1, axis=1)], axis=2).reshape(-1, 2)              # halfedge h: face h // 3
    hl = np.flatnonzero(d[:, 0] != d[:, 1])
    fwd = d[hl, 0] < d[hl, 1]
    _, inv, cnt = np.unique(np.sort(d[hl], axis=1), axis=0, return_inverse=True, return_counts=True)
    inv = inv.ravel()
    net = np.bincount(inv, weights=np.where(fwd, 1., -1.), minlength=len(cnt))
    m_ = (cnt[inv] % 2 == 1) & (fwd == (net[inv] > 0))
    _, fi = np.unique(inv[m_], return_index=True)
    hs = hl[m_][fi]                                                                 # one halfedge per odd edge, in its unpaired direction
    hcnt = cnt[inv[m_][fi]]
    notes = {'openings': 0, 'seams': 0, 'capped': 0, 'cap_edges': 0, 'largest_mm': 0., 'through_3way': 0}
    if not (hcnt == 1).any():
        return lay, U1, notes
    pos = [tuple(x) for x in kq]
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
    seam = np.array([frozenset((pos[a_], pos[b_])) in seam_keys for a_, b_ in d[hs]], bool)   # a real seam edge (consecutive ring vertices)
    V, U = [*lay.V], ([*U1] if U1 is not None else None)
    F, role, part = [], [], []
    for cid in np.unique(comp):
        sel = hs[comp == cid]
        if not (hcnt[comp == cid] == 1).any():                        # only three-way edges: no hole there
            continue
        notes['openings'] += 1
        own = ~seam[comp == cid]
        verts = np.unique(d[sel])
        if len(verts) < 3:
            continue
        notes['through_3way'] += int((hcnt[comp == cid] > 1).sum())
        notes['seams'] += int(not own.any())                          # a seam opening the closed caps did not take (not joined all round)
        own[:] = True                                                 # every opening still open is closed WHOLE, its seam edges too
        notes['mixed'] = notes.get('mixed', 0) + int((~own).any() or seam[comp == cid].any())   # (fanning only part left open spokes)
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


def _part_groups(body, gd):
    """Per body part: its group (geodiv.INFLATE_GROUPS: torso, arm.l/r, leg.l/r; any other part is its own group)."""
    g = {}
    for name, parts in gd.INFLATE_GROUPS.items():
        for pn in parts:
            g[pn] = name
    return np.array([g.get(pn, pn) for pn in body.part_names])


_FACE_SAMPLES = np.array([[a, b, 1 - a - b] for a in np.linspace(0, 1, 5) for b in np.linspace(0, 1, 5) if a + b <= 1 + 1e-9])


def face_push(lay, U, body, gd, margin, fixed, slot=None, rings=()):
    """Push garment FACES (not just vertices) clear of the base body, in place on U. A body point (vertex or face centre of the game body) of the
    same body group as the face (the leg / arm / torso it covers) that lies in front of the face, or less than `margin` behind it, along the body's
    outward normal at the face, with its projection along that normal inside the triangle, pierces it: the face's three corners move out by
    enough to put that point `margin` behind it. Corners take the largest push of their faces, along the mean outward direction. Repeated
    (FACE_PUSH_ITERS). `fixed` vertices (the seams) never move."""
    from scipy.spatial import cKDTree
    grp = _part_groups(body, gd)
    vpart = np.zeros(len(body.V), np.int64)
    for k in range(3):
        vpart[body.F[:, k]] = body.part
    P = np.r_[body.V, body.V[body.F].mean(1)]
    Pg = np.r_[grp[vpart], grp[body.part]]
    tree = cKDTree(P)
    G = np.flatnonzero(lay.role != gd.SKIN)
    if slot is not None and len(G):                                  # only where the base body can show (see FACE_PUSH_SEAM_MM)
        T0 = U[lay.F[G]]
        pts = np.concatenate([T0.mean(1)[:, None], T0], 1).reshape(-1, 3)   # centre and corners: a big face crossing a seam counts
        _, tri0, _ = body.closest(pts)
        away = (~np.isin(body.part[tri0], slot)).reshape(len(G), 4).any(1)
        near = np.zeros(len(pts), bool)
        for R in rings:
            A, B = R['P'], np.roll(R['P'], -1, 0)
            AB = B - A
            t_ = np.clip(((pts[:, None] - A[None]) * AB[None]).sum(2) / np.maximum((AB ** 2).sum(1), 1e-18)[None], 0, 1)
            dd = np.linalg.norm(A[None] + t_[..., None] * AB[None] - pts[:, None], axis=2).min(1)
            near |= dd < FACE_PUSH_SEAM_MM / 1000
        G = G[away | near.reshape(len(G), 4).any(1)]
    U_in = U.copy()
    reach = FACE_PUSH_REACH_MM / 1000
    E = np.unique(np.sort(np.concatenate([lay.F[:, [0, 1]], lay.F[:, [1, 2]], lay.F[:, [2, 0]]]), axis=1), axis=0)
    notes = {'pierced': [], 'max_push_mm': 0.}
    if not len(G):
        return notes
    for it in range(FACE_PUSH_ITERS):
        T = U[lay.F[G]]
        c = T.mean(1)
        _, tri, _ = body.closest(c)
        o = body.tri_n[tri]
        fg = grp[body.part[tri]]
        nf = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
        nf /= np.maximum(np.linalg.norm(nf, axis=1), 1e-18)[:, None]
        rad = np.linalg.norm(T - c[:, None], axis=2).max(1)
        lists = tree.query_ball_point(c, rad + reach)
        need = np.zeros(len(U))
        dirs = np.zeros_like(U)
        hit = 0
        # (a) the face's own points INSIDE the body: the face moves out along the body normal by the deepest one's depth + margin (all points of
        # the face move by the same vector, so no tilt correction). A face merely closer than margin is left alone: squeezed between two body
        # surfaces (beside a belly) it can never satisfy both, and crept out 10 mm a round
        # Each pierce is shared over the corners by its barycentric weights w (corner i moves w_i * need / sum(w^2): the least movement that
        # moves that point by `need`), so a pierce at one corner moves that corner only; corners already clear are not dragged out with it
        dsa = body.measure(np.einsum('kj,fjd->fkd', _FACE_SAMPLES, T).reshape(-1, 3))['d'].reshape(len(G), -1)
        nd = np.where((dsa < 0) & (dsa > -reach), margin - dsa, 0.)                 # need per face sample
        cw = _FACE_SAMPLES / (_FACE_SAMPLES ** 2).sum(1, keepdims=True)              # w / sum(w^2) per sample
        corner = (nd[:, :, None] * cw[None]).max(1)                                  # per face corner: its largest share
        for gi in np.flatnonzero(corner.max(1) > 0):
            hit += 1
            for k, v in enumerate(lay.F[G[gi]]):
                if corner[gi, k] > need[v]:
                    need[v] = corner[gi, k]
                    dirs[v] = o[gi]
        # (b) body points (the sharp knee) inside the face's footprint, in front of it or closer than margin
        for gi, idx in enumerate(lists):
            if not idx:
                continue
            idx = np.asarray(idx)
            idx = idx[Pg[idx] == fg[gi]]
            if not len(idx):
                continue
            on = float(o[gi] @ nf[gi])
            if abs(on) < FACE_PUSH_MIN_COS:                           # face far from facing along the body normal: no meaningful footprint
                continue
            A, B, C = T[gi]
            p = P[idx]
            t = ((p - A) @ nf[gi]) / on                               # distance from the face to the point along o (> 0: point in front)
            q = p - t[:, None] * o[gi]                                # the point's footprint on the face plane
            v0, v1, v2 = B - A, C - A, q - A
            d00, d01, d11 = v0 @ v0, v0 @ v1, v1 @ v1
            d20, d21 = v2 @ v0, v2 @ v1
            den = d00 * d11 - d01 * d01
            if abs(den) < 1e-18:
                continue
            bb = (d11 * d20 - d01 * d21) / den
            cc = (d00 * d21 - d01 * d20) / den
            inside = (bb >= -.02) & (cc >= -.02) & (bb + cc <= 1.02)
            sel = inside & (t > 0) & (t < FACE_PUSH_POKE_MM / 1000)    # the point really pokes through the face
            if not sel.any():
                continue
            w = np.clip(np.c_[1 - bb[sel] - cc[sel], bb[sel], cc[sel]], 0, 1)
            w /= w.sum(1, keepdims=True)
            share = ((t[sel] + margin)[:, None] * w / (w ** 2).sum(1, keepdims=True)).max(0)   # t is measured along o
            hit += 1
            for k, v in enumerate(lay.F[G[gi]]):
                if share[k] > need[v]:                                # each corner takes its single largest share (averaging directions
                    need[v] = share[k]                                # left faces short of clear)
                    dirs[v] = o[gi]
        notes['pierced'].append(hit)
        if not hit:
            break
        D = dirs * need[:, None]
        for _ in range(FACE_PUSH_SPREAD):                             # spread: a vertex takes its neighbours' mean push when that is larger
            acc = np.zeros_like(D)
            cnt = np.zeros(len(D))
            for x, y in ((E[:, 0], E[:, 1]), (E[:, 1], E[:, 0])):
                np.add.at(acc, x, D[y])
                np.add.at(cnt, x, 1)
            avg = acc / np.maximum(cnt, 1)[:, None]
            take = np.linalg.norm(avg, axis=1) > np.linalg.norm(D, axis=1)
            D[take] = avg[take]
            D[fixed] = 0.
        mv = (np.linalg.norm(D, axis=1) > 0) & ~fixed
        U[mv] += D[mv]
        need = np.linalg.norm(D, axis=1)
        notes['max_push_mm'] = max(notes['max_push_mm'], round(float(need[mv].max()) * 1000, 1) if mv.any() else 0.)
    return notes


def seam_blend(lay, body, ring_v, gd):
    """Move the layer's exposed skin vertices (not on a garment, not on a ring) within SEAM_BLEND_MM of a seam, measured along the layer, onto
    the game body (SKIN_OUT_MM out), by 1 - smoothstep(distance / SEAM_BLEND_MM). Faces the move would turn over have their moves halved
    (fold_guard). In place on lay.V; returns notes."""
    from scipy import sparse
    from scipy.sparse import csgraph
    if SEAM_BLEND_MM <= 0 or not len(ring_v):
        return {}
    E = np.unique(np.sort(np.concatenate([lay.F[:, [0, 1]], lay.F[:, [1, 2]], lay.F[:, [2, 0]]]), axis=1), axis=0)
    wt = np.linalg.norm(lay.V[E[:, 0]] - lay.V[E[:, 1]], axis=1) + 1e-12
    nV = len(lay.V)
    G = sparse.coo_matrix((np.r_[wt, wt], (np.r_[E[:, 0], E[:, 1]], np.r_[E[:, 1], E[:, 0]])), shape=(nV, nV)).tocsr()
    d = csgraph.dijkstra(G, directed=False, indices=ring_v, min_only=True, limit=SEAM_BLEND_MM / 1000 * 1.01)
    d = np.where(np.isfinite(d), d, 1e3)
    x = np.clip((d - SEAM_FULL_MM / 1000) / ((SEAM_BLEND_MM - SEAM_FULL_MM) / 1000), 0, 1)
    wb = 1 - x * x * (3 - 2 * x)                                      # 1 up to SEAM_FULL_MM, 0 from SEAM_BLEND_MM
    skin_v = np.unique(lay.F[lay.role == gd.SKIN])
    gar_v = np.unique(lay.F[lay.role != gd.SKIN])
    sv = np.setdiff1d(skin_v, np.r_[gar_v, ring_v])
    sv = sv[wb[sv] > 0]
    if not len(sv):
        return {'vertices': 0}
    _, tri, w = body.closest(lay.V[sv])
    Q = w[:, :1] * body.A[tri] + w[:, 1:2] * body.B[tri] + w[:, 2:] * body.C[tri] + body.tri_n[tri] * SKIN_OUT_MM / 1000
    Vb = lay.V.copy()
    Vb[sv] += (Q - lay.V[sv]) * wb[sv, None]
    moved = np.linalg.norm(Vb[sv] - lay.V[sv], axis=1)
    fg = fold_guard(lay, Vb)
    lay.V[:] = Vb
    return {'vertices': int(len(sv)), 'max_move_mm': round(float(moved.max()) * 1000, 1), 'fold_guard': fg}


def amount_morph(lay, body, ring_v, gd, rings=(), slot=None):
    """Amount 100: generated skin inside the game body goes out onto it, then the garment is pushed out of the body (geodiv.fill_grouped) with the
    pieces.py layer / fold guards. Ring vertices never move."""
    import pieces as pc
    from shell_slots import Mesh
    V = lay.V.copy()
    skin_v = np.unique(lay.F[lay.role == gd.SKIN])
    gar_v = np.unique(lay.F[lay.role != gd.SKIN])
    sv = np.setdiff1d(skin_v, np.r_[gar_v, ring_v])
    notes = {}
    if INFLATE_LAYERS and len(gar_v):
        deep = float(np.percentile(body.measure(V[gar_v])['d'], 5)) * 1000
        notes['inflate'] = {'garment_p5_depth_mm': round(deep, 1)}
        if deep < -gd.INFLATE_TRIGGER_MM:
            im = Mesh(V, lay.F, lay.role, lay.part, lay.prov)
            notes['inflate']['passes'] = [gd.inflate_to_body(im, body) for _ in range(gd.INFLATE_PASSES)]
            V = im.V.copy()
            V[ring_v] = lay.V[ring_v]
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
    failed = [g for g, v in notes['fill'].items() if isinstance(v, str)]
    if failed:
        notes['warnings'] = [f'fill failed for group {g}: {notes["fill"][g]}' for g in failed]
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
        U1 = V + (U1 - V) * w[:, None]                               # from V: exposed skin stays ON the body up to the seam (fading from
        notes['ring_taper_vertices'] = int((w < 1).sum())            # lay.V kept the generator's shape within 5 cm of it: a crease)
    notes['fold_guard'] = fold_guard(lay, U1)                         # before the face push: it would halve the pushes again
    if FACE_PUSH_ITERS:                                               # after the seam fade: a face the body still pierces is pushed in full
        fixed = np.zeros(len(U1), bool)
        fixed[ring_v] = True
        U_pre = U1.copy()
        notes['face_push'] = face_push(lay, U1, body, gd, margin, fixed, slot, rings)
        notes['face_push']['fold_guard'] = fold_guard(type('M', (), {'V': U_pre, 'F': lay.F})(), U1)   # scales back only the push on faces it flips
        n0 = np.cross(lay.V[lay.F[:, 1]] - lay.V[lay.F[:, 0]], lay.V[lay.F[:, 2]] - lay.V[lay.F[:, 0]])
        n1 = np.cross(U1[lay.F[:, 1]] - U1[lay.F[:, 0]], U1[lay.F[:, 2]] - U1[lay.F[:, 0]])
        notes['face_push']['faces_flipped_after'] = int(((n0 * n1).sum(1) < 0).sum())
    if LAYER_GUARD:
        notes['fill_layer_guard'] = pc.fill_layer_guard(lay, U1, gd)
    U1[ring_v] = lay.V[ring_v]
    return U1, notes


def slider_fold_guard(m, U1, rounds=12):
    """No face may turn over ANYWHERE along the Amount slider: per face, its area along its ORIGINAL normal is a quadratic in the slider t
    (n0 . (e1 + t d1) x (e2 + t d2)); its minimum over [0, 1] (both ends and an interior minimum) must stay above 0.1 % of the start. Offending
    faces get their vertices' moves halved, every face re-checked each round; after `rounds` halvings the moves are removed (the face is then
    exactly as at Amount 0, which is fine). Faces with no area to start with (on a ring edge) are skipped. In place on U1."""
    V, F = m.V, m.F
    e1, e2 = V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]
    n0 = np.cross(e1, e2)
    a0 = (n0 * n0).sum(1)
    live = a0 > 1e-16
    D = U1 - V
    first, it, bad = None, 0, np.zeros(len(F), bool)
    for it in range(rounds + 60):
        d1, d2 = D[F[:, 1]] - D[F[:, 0]], D[F[:, 2]] - D[F[:, 0]]
        b_ = (n0 * (np.cross(e1, d2) + np.cross(d1, e2))).sum(1)
        c_ = (n0 * np.cross(d1, d2)).sum(1)
        tm = np.clip(np.where(c_ > 0, -b_ / np.where(c_ > 0, 2 * c_, 1.), 1.), 0, 1)
        fmin = np.minimum(a0 + b_ + c_, a0 + b_ * tm + c_ * tm * tm)
        bad = live & (fmin <= 1e-3 * a0)
        first = int(bad.sum()) if first is None else first
        if not bad.any():
            break
        D[np.unique(F[bad])] *= 0. if it >= rounds - 1 else .5
    U1[:] = V + D
    return {'faces_turning_over': first, 'rounds': it, 'left': int(bad.sum())}


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


def _halfedges(lay):
    """{(a, b): number of faces with the directed edge a -> b}. Triangles with no area (three corners on one ring edge, kept so the open edge
    stays the ring edge) are not counted: they draw nothing and join nothing; two real faces on a seam edge still count as two."""
    h = {}
    V, F = lay.V, lay.F
    area2 = np.linalg.norm(np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]), axis=1)
    for f in F[area2 > 1e-10].tolist():
        for e in ((f[0], f[1]), (f[1], f[2]), (f[2], f[0])):
            h[e] = h.get(e, 0) + 1
    return h


def _ring_edge_use(lay, R, h=None):
    """Per edge of ring R: (real faces a -> b, real faces b -> a) in the layer. A ring edge may be split by vertices put ON it (LINE_SNAP: the
    real faces then run a - x - b along the line, held together by a zero-area triangle on a - b); every piece must then agree, else (-1, -1).
    None where a ring vertex is missing."""
    h = _halfedges(lay) if h is None else h
    q = {tuple(x): i for i, x in enumerate(np.round(lay.V * 1e7).astype(np.int64))}
    ids = [q.get(tuple(x)) for x in np.round(R['P'] * 1e7).astype(np.int64)]
    n = len(ids)
    out = []
    for i in range(n):
        a, b = ids[i], ids[(i + 1) % n]
        if a is None or b is None:
            out.append(None)
            continue
        A, AB = lay.V[a], lay.V[b] - lay.V[a]
        t = ((lay.V - A) @ AB) / max(float(AB @ AB), 1e-18)
        dist = np.linalg.norm(A + t[:, None] * AB - lay.V, axis=1)
        on = np.flatnonzero((dist < 1e-7) & (t > 1e-9) & (t < 1 - 1e-9))
        chain = [a] + [int(x) for x in on[np.argsort(t[on])]] + [b]
        uses = {(h.get((u, v), 0), h.get((v, u), 0)) for u, v in zip(chain, chain[1:])}
        out.append(uses.pop() if len(uses) == 1 else (-1, -1))
    return out


def _joined(lay, R, h=None):
    """'a/n': how many of ring R's edges are OPEN seam edges of the layer (exactly one face has the edge: an edge two of the layer's own faces
    already share is internal, not a place for the neighbour to attach)."""
    use = _ring_edge_use(lay, R, h)
    return f"{sum(1 for u in use if u is not None and sum(u) == 1)}/{len(use)}"


def _full(j):
    a, b = j.split('/')
    return a == b and b != '0'


def _strip(body, slot, R):
    """The game body's faces of the slot that touch ring R: the strip from the seam to the body's next row of vertices."""
    return np.isin(body.part, slot) & np.isin(body.F, R['ids']).any(1)


def _garment_over(m, slot, rings0, body, gd):
    """Per ring: does the module's kept garment come within 40 mm of the body strip next to it?"""
    keepG, _ = gd.kept_garment(m, slot, None, body)
    G = np.flatnonzero(keepG & (m.role != gd.SKIN))
    if not len(G):
        return [False] * len(rings0)
    gv = np.unique(m.F[G])
    d, tri, _ = body.closest(m.V[gv])
    close = np.abs(d) < .04
    return [bool((_strip(body, slot, R)[tri] & close).any()) for R in rings0]


def build_layer(m, slot, body, gd):
    """The layer, with BODY_BAND: at every seam its own skin reaches all round, with no garment over the body strip next to it, the layer's
    skin is cut at the body's NEXT row of vertices instead of the seam and the strip between is the game body's own faces (prov 'seamband'),
    so both sides of the seam have the body's exact shape. A seam whose generated skin does not join the next row all round falls back."""
    import copy
    ob = out_body(body, gd)
    rings0 = rings_of(ob, slot, gd)
    lay, U1, rec = _build_layer(m, slot, body, gd, rings0, [])
    if not BODY_BAND or lay is None or not rings0:
        return lay, U1, rec
    use = [k for k, nk in enumerate(rec['seams']['rings']) if _full(nk.get('ring_edges_joined', '0/0'))]   # no garment rule (user 2026-11-24)
    tried = []
    for _ in range(3):
        if not use:
            break
        tried.append(list(use))
        lay2, U2, rec2 = _build_layer(m, slot, body, gd, rings0, use)
        if lay2 is None:
            break
        bad = [k for k in use if not rec2['band'][k]['inner_complete']]
        if not bad:
            rec2['band_tries'] = tried
            return lay2, U2, rec2
        use = [k for k in use if k not in bad]
    rec['band_tries'] = tried
    return lay, U1, rec


def _build_layer(m, slot, body, gd, rings0, use):
    import copy
    from shell_slots import Mesh
    ob = out_body(body, gd)
    if use:
        band = np.zeros(len(ob.F), bool)
        for k in use:
            band |= _strip(ob, slot, rings0[k])
        fb = copy.copy(ob)
        fb.part = np.where(band, -2, ob.part)                         # the strips are out of the slot for the cut
        rings = rings_of(fb, slot, gd)
    else:
        band, fb, rings = None, ob, rings0
    lay, seam, selnotes = select_layer(m, slot, rings, body, gd, field_body=fb)
    if not len(lay.F) or not (lay.role != gd.SKIN).any():
        return None, None, {'skipped': 'no garment in this slot'}
    lay, snotes = snap_to_rings(lay, seam, rings, gd, body)
    lay = gd.compact(lay)
    allr = list(rings) + ([R for R in rings0] if use else [])
    rq = {tuple(x) for R in allr for x in np.round(R['P'] * 1e7).astype(np.int64)}
    on_ring = np.array([tuple(x) in rq for x in np.round(lay.V * 1e7).astype(np.int64)], bool)
    for R in rings:                                                   # and vertices put ON a ring edge (LINE_SNAP_MM)
        A, B = R['P'], np.roll(R['P'], -1, 0)
        AB = B - A
        t_ = np.clip(((lay.V[:, None] - A[None]) * AB[None]).sum(2) / np.maximum((AB ** 2).sum(1), 1e-18)[None], 0, 1)
        on_ring |= np.linalg.norm(A[None] + t_[..., None] * AB[None] - lay.V[:, None], axis=2).min(1) < 1e-7
    ring_v = np.flatnonzero(on_ring)
    for R, nk in zip(rings, snotes['rings']):
        nk['ring_edges_joined'] = _joined(lay, R)
    blend_notes = seam_blend(lay, body, ring_v, gd)
    U1, anotes = amount_morph(lay, body, ring_v, gd, rings, slot)
    anotes['seam_blend'] = blend_notes
    band_notes = {}
    if use:
        for k in use:                                                 # the rings the strip of ring k leaves on its slot side
            sv = set(ob.F[_strip(ob, slot, rings0[k])].ravel().tolist())
            own = set(np.asarray(rings0[k]['ids']).tolist())
            inner = [j for j, R in enumerate(rings) if set(np.asarray(R['ids']).tolist()) <= sv and not set(np.asarray(R['ids']).tolist()) <= own]
            band_notes[k] = {'faces': int(_strip(ob, slot, rings0[k]).sum()), 'inner_rings': inner,
                             'inner_joined': [snotes['rings'][j]['ring_edges_joined'] for j in inner],
                             'inner_complete': bool(inner) and all(_full(snotes['rings'][j]['ring_edges_joined']) for j in inner)}
        q = {tuple(x): i for i, x in enumerate(np.round(lay.V * 1e7).astype(np.int64))}
        Vn, Un, Fb = [*lay.V], [*U1], []
        for f in ob.F[band]:
            ids = []
            for v in f:
                key = tuple(np.round(ob.V[v] * 1e7).astype(np.int64))
                if key not in q:
                    q[key] = len(Vn)
                    Vn.append(ob.V[v].copy())
                    Un.append(ob.V[v].copy())
                ids.append(q[key])
            Fb.append(ids)
        lay = Mesh(np.array(Vn), np.r_[lay.F, np.array(Fb, np.int64)], np.r_[lay.role, [gd.SKIN] * len(Fb)], np.r_[lay.part, ob.part[band]],
                   np.r_[lay.prov, ['seamband'] * len(Fb)])
        U1 = np.array(Un)
        h = _halfedges(lay)                                           # after welding: every inner ring edge has exactly two faces, one each way,
        for k in use:                                                 # and every outer (seam) edge exactly one, the strip's
            inner_ok = all(u is not None and u == (1, 1) for j in band_notes[k]['inner_rings'] for u in _ring_edge_use(lay, rings[j], h))
            outer_ok = all(u is not None and sum(u) == 1 for u in _ring_edge_use(lay, rings0[k], h))
            band_notes[k]['welded'] = bool(inner_ok and outer_ok)
            band_notes[k]['inner_complete'] = band_notes[k]['inner_complete'] and inner_ok and outer_ok
    h = _halfedges(lay)
    outer = [{'ring': k, 'ring_edges_joined': _joined(lay, R, h), 'band': k in use} for k, R in enumerate(rings0)]
    snotes['inner_rings'] = snotes['rings'] if use else []
    snotes['rings'] = outer
    cnotes, enotes = [], {}
    full = [_full(nk['ring_edges_joined']) for nk in outer]
    necks = neck_rings(ob, rings0, gd) if NECK_CAP else []            # only seams joined all round get a closed cap (a partly covered one is
    caps = [R for R, f_ in zip(rings0, full) if f_ and any(R is C for C in necks)]   # closed whole by the end caps)
    if RING_CAPS:
        caps += [R for R, f_ in zip(rings0, full) if f_ and not any(R is C for C in caps)]
    if caps:
        lay, U1, cnotes = add_caps(lay, U1, caps, gd)
    if END_CAPS:
        seam_keys = set()                                             # the real seam edges: consecutive ring vertices, by position
        for R in rings0:
            Pk = [tuple(x) for x in np.round(R['P'] * 1e7).astype(np.int64)]
            seam_keys |= {frozenset((Pk[i], Pk[(i + 1) % len(Pk)])) for i in range(len(Pk))}
        lay, U1, enotes = add_end_caps(lay, U1, seam_keys, gd)
    gnotes = slider_fold_guard(lay, U1)                               # last: nothing may turn over anywhere along the slider
    return lay, U1, {'select': selnotes, 'seams': snotes, 'amount': anotes, 'neck_caps': cnotes, 'end_caps': enotes, 'faces': int(len(lay.F)),
                     'band': band_notes, 'slider_fold_guard': gnotes, 'ring_vertices': int(len(ring_v)),
                     'ring_vertices_expected': int(sum(len(R['P']) for R in rings0))}


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


_LAYER_BODY = {}


def get_body(race, gd):
    """The game body of a race for the layers: a COPY with its own part labels (tidy_parts / replane_limbs relabel it; the cached canonical body
    that alignment and load_shell use stays untouched), tagged with its race (replane_limbs reads the race's rig)."""
    import copy
    if race not in _LAYER_BODY:
        body = copy.copy(gd.game_body(race))
        body.part = body.part.copy()
        body._race = race
        _LAYER_BODY[race] = body
    return _LAYER_BODY[race]


def out_body(body, gd):
    """The game mesh layers are MADE from: the real game body (body.orig when the measuring body was subdivided: same surface, ~3.4x fewer
    triangles), each face labelled with the part of the measuring body at its centre (so tidy_parts / replane_limbs carry over). Seams, bare
    layers and body strips come from this; distances are still measured on `body`."""
    import copy
    o = getattr(body, 'orig', None)
    if o is None:
        return body
    stamp = hash(body.part.tobytes())
    if getattr(body, '_out_stamp', None) != stamp:
        g = copy.copy(o)
        _, tri, _ = body.closest(o.V[o.F].mean(1))
        g.part = body.part[tri].copy()
        g._race = getattr(body, '_race', None)
        body._out, body._out_stamp = g, stamp
    return body._out


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
        ob = out_body(body, gd)
        g = gd.game_faces(ob, ids)
        cn = []
        if RING_CAPS:
            g, _, cn = add_caps(g, None, rings_of(ob, ids, gd), gd)
        elif NECK_CAP and k in ('head', 'torso'):
            g, _, cn = add_caps(g, None, neck_rings(ob, rings_of(ob, ids, gd), gd, head_slot=k == 'head'), gd)
        write_layer(race, k, 'bare', g, None, {'kind': 'bare', 'parts': [body.part_names[i] for i in ids], 'neck_caps': cn}, gd)
    return f'{race}: bare layers'


def _remove_layer(race, slot_key, set_name):
    """This run's own build products for one layer (glb, json, scene arrays), so a skipped or failed layer leaves no older copy behind."""
    d = OUTL / race
    stem = f'{slot_key}__{set_name}'
    for q in (d / f'{stem}.glb', d / f'{stem}.json', d / 'scenes' / f'{stem}__a0.npz', d / 'scenes' / f'{stem}__a100.npz'):
        if q.is_file():
            q.unlink()


def _module_slots(module):
    return {'outfit': ['outfit'], 'body_armor': ['torso'],
            'right_gauntlet_boot': ['lower_arm.r', 'lower_arm.l', 'lower_leg.r', 'lower_leg.l']}.get(module, [])


def task_module(mod):
    import geodiv as gd
    mod = Path(mod).resolve()
    s, j, race, module = gd.module_identity(mod)
    written = []
    try:
        msg = _task_module(mod, written)
    except Exception:
        for k in _module_slots(module):                               # failed: none of this module's layers may look current
            if k not in written:
                _remove_layer(race, k, s)
        raise
    for k in _module_slots(module):                                   # skipped this time: an older copy must not stay in the index
        if k not in written:
            _remove_layer(race, k, s)
    return msg


def _task_module(mod, written):
    import geodiv as gd
    t0 = time.monotonic()
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
                                                 'warnings': notes['amount'].get('warnings', []),
                                                 'seconds': round(time.monotonic() - t0, 1)}, gd)
        written.append(slot_key)
        sn = notes['seams']
        st = ' '.join(r.get('ring_edges_joined', '-') for r in sn['rings'])
        made.append(f'{slot_key}: {notes["faces"]} faces, seams joined {st}, moved {sn["vertices_moved"]} (max {sn["max_move_mm"]} mm), '
                    f'inserted {sn["triangles_inserted"]}, folded {sn["faces_folded"]}, slider flips fixed '
                    f'{notes["slider_fold_guard"]["faces_turning_over"]}' + (f', WARN {notes["amount"]["warnings"]}' if notes['amount'].get('warnings') else ''))

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
    failed = 0
    with mp.Pool(a.workers, initializer=_setup) as pool:
        for msg in pool.imap_unordered(_run, tasks):
            failed += msg.startswith('FAILED')
            print(time.strftime('%H:%M:%S'), msg, flush=True)
    print(time.strftime('%H:%M:%S'), f'{failed} of {len(tasks)} tasks FAILED', flush=True)
    print(time.strftime('%H:%M:%S'), 'index:', write_index(), 'layers', flush=True)


if __name__ == '__main__':
    main()
