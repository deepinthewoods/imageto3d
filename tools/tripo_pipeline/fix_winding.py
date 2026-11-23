#!/usr/bin/env python3
"""Find and fix flipped faces (inconsistent triangle winding) in a module's GLBs.

A low-poly mesh from the generator / reduction sometimes has a face whose winding disagrees with its neighbours: it renders inside-out
(invisible from outside with back-face culling, e.g. in Godot; red with the review page's inside colour). This tool:

1. welds each GLB's vertices by position (across primitives / materials),
2. walks every edge-connected piece over its manifold edges (edges with exactly two faces) and gives each face the winding of its
   neighbours (a piece that cannot be oriented consistently, e.g. a Moebius-like strip, is reported and left as it is),
3. keeps, per piece, the winding of the larger part by area (so one flipped face in a good piece is turned round, never the piece);
   a piece whose minority covers more than REVIEW_FRACTION of its area is reported (pieces_needing_review) and left alone,
4. turns a CLOSED piece that is inside-out as a whole (negative enclosed volume) the right way round, unless it lies inside another
   part of the mesh (a cavity, which faces inward by design).

Only the order of each flipped triangle's corners changes (and the stored vertex normals of corners used by flipped faces only are
negated): face order, vertex order, colours / UVs and every sidecar array (`_colors.npz`, clearance json, labels) stay valid.

  python tools/tripo_pipeline/fix_winding.py <module dir | glb> ...          # check only (report)
  python tools/tripo_pipeline/fix_winding.py <module dir> --apply            # fix the module's GLBs in place (backup *.glb.prewinding)
  python tools/tripo_pipeline/fix_winding.py --all [--race R] [--apply]      # every module under models/
A module dir means models/<set>/<id>/<race>/<module>: its reduced/*.glb and segment_first/pieces/**/*.glb (lod_error_*.glb, wearables,
aligned / fitted copies, rigid pieces). Exit code 0; the report (json with --summary) lists per GLB the faces flipped.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import defaultdict, deque
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from body_fit import Glb  # noqa: E402


def module_busy(mod):
    """A conversion worker other than our own parent (MODULE_LOCK_OWNER) holds the module's lock."""
    try:
        from rewear import module_busy as busy
        return busy(Path(mod).resolve())
    except Exception:  # noqa: BLE001 - no lock information: not busy
        return False

WELD_REL = 1e-6        # positions closer than this fraction of the mesh size are one vertex
REVIEW_FRACTION = 0.2  # a piece whose minority winding covers more than this share of its area is reported, not fixed
SKIP_SUFFIXES = ('__tripo_body.glb', '__tripo_body_aligned.glb')   # red-body preview overlays: not garment


def weld(points):
    """(n,3) -> (index of the welded vertex per point, welded positions)."""
    if not len(points):
        return np.zeros(0, np.int64), points
    size = float(np.ptp(points, axis=0).max()) or 1.0
    q = np.round(points / (size * WELD_REL)).astype(np.int64)
    _, first, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    return inv.ravel(), points[first]


def orient(W, F):
    """Faces to flip so that every piece is consistently wound. W (n,3) welded positions, F (m,3) welded indices.
    Returns (flip (m,) bool, info dict)."""
    m = len(F)
    flip = np.zeros(m, bool)
    info = {'faces': int(m), 'pieces': 0, 'flipped_in_piece': 0, 'closed_pieces_turned': 0, 'non_orientable_pieces': 0}
    if not m:
        return flip, info
    A, B, C = W[F[:, 0]], W[F[:, 1]], W[F[:, 2]]
    cr = np.cross(B - A, C - A)
    area = 0.5 * np.linalg.norm(cr, axis=1)
    edges = defaultdict(list)                              # undirected edge -> [(face, +1 if a<b in the face's order else -1)]
    for f, (a, b, c) in enumerate(F):
        for x, y in ((a, b), (b, c), (c, a)):
            if x == y:
                continue
            edges[(x, y) if x < y else (y, x)].append((f, 1 if x < y else -1))
    nb = [[] for _ in range(m)]
    open_edge = np.zeros(m, bool)
    for e, lst in edges.items():
        if len(lst) == 2:
            (f1, d1), (f2, d2) = lst
            if f1 != f2:
                nb[f1].append((f2, d1 == d2))              # same direction on a shared edge = opposite winding
                nb[f2].append((f1, d1 == d2))
        elif len(lst) == 1:
            open_edge[lst[0][0]] = True
        else:
            open_edge[[f for f, _ in lst]] = True          # non-manifold: treated as an open border for the closed test
    seen = np.zeros(m, bool)
    rel = np.zeros(m, bool)                                # winding relative to the piece's seed
    turn = []
    for s in range(m):
        if seen[s]:
            continue
        info['pieces'] += 1
        comp, conflict = [s], False
        seen[s] = True
        dq = deque([s])
        while dq:
            f = dq.popleft()
            for g, opp in nb[f]:
                want = rel[f] ^ opp
                if not seen[g]:
                    seen[g], rel[g] = True, want
                    comp.append(g)
                    dq.append(g)
                elif rel[g] != want:
                    conflict = True
        comp = np.array(comp)
        if conflict:
            info['non_orientable_pieces'] += 1
            continue
        # keep the winding of the larger part (by area)
        a1 = area[comp][rel[comp]].sum()
        a0 = area[comp][~rel[comp]].sum()
        fl = rel[comp] if a1 <= a0 else ~rel[comp]
        if area[comp][fl].sum() > REVIEW_FRACTION * area[comp].sum():
            info['pieces_needing_review'] = info.get('pieces_needing_review', 0) + 1     # too much of the piece disagrees: not guessed
            continue
        if not open_edge[comp].any():                      # closed piece: the enclosed volume must be positive after the fix
            sgn = np.where(fl, -1., 1.)
            vol = (sgn * np.einsum('ij,ij->i', A[comp], np.cross(B[comp], C[comp]))).sum() / 6.
            if vol < 0:
                turn.append((comp, fl))                    # decided below: a piece inside another piece is a cavity and faces inward
        flip[comp] = fl
        if not fl.all():
            info['flipped_in_piece'] += int(fl.sum())
    for comp, fl in turn:
        if not enclosed(W, F, comp, A, B, C):
            flip[comp] = ~fl
            info['closed_pieces_turned'] += 1
            info.setdefault('turned_piece_cm2', []).append(round(float(area[comp].sum() * 1e4), 2))
        else:
            info['cavities_kept'] = info.get('cavities_kept', 0) + 1
    info['flipped'] = int(flip.sum())
    info['flipped_area_pct'] = round(float(100 * area[flip].sum() / max(area.sum(), 1e-12)), 3)
    return flip, info


def enclosed(W, F, comp, A, B, C):
    """Is the closed piece `comp` inside another part of the mesh? Rays from a point just inside it (its area centroid nudged along -normal of
    its largest face) in 3 directions; inside when the majority cross the rest of the mesh an odd number of times."""
    others = np.setdiff1d(np.arange(len(F)), comp)
    if not len(others):
        return False
    k = comp[np.argmax(np.linalg.norm(np.cross(B[comp] - A[comp], C[comp] - A[comp]), axis=1))]
    p0 = (A[k] + B[k] + C[k]) / 3
    TA, TB, TC = A[others], B[others], C[others]
    e1, e2 = TB - TA, TC - TA
    votes = 0
    for d in (np.array([0.577, 0.577, 0.577]), np.array([-0.6, 0.48, 0.64]), np.array([0.36, -0.8, 0.48])):
        pv = np.cross(d[None], e2)
        det = (e1 * pv).sum(1)
        ok = np.abs(det) > 1e-14
        inv = np.where(ok, 1. / np.where(ok, det, 1.), 0.)
        tv = p0[None] - TA
        u = (tv * pv).sum(1) * inv
        qv = np.cross(tv, e1)
        v = (qv * d[None]).sum(1) * inv
        t = (e2 * qv).sum(1) * inv
        hits = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-9)
        votes += int(hits.sum()) % 2
    return votes >= 2


def check_glb(path):
    """(flip masks per primitive, info) for one GLB."""
    g = Glb(path)
    P, T, offs = [], [], [0]
    for k in range(len(g.prims)):
        mtx = g.prims[k]['matrix']
        p = g.positions(k) @ mtx[:3, :3].T + mtx[:3, 3]
        P.append(p)
        T.append(g.triangles(k) + offs[-1])
        offs.append(offs[-1] + len(p))
    if not P:
        return g, [], {'faces': 0, 'flipped': 0}
    pts = np.concatenate(P)
    inv, W = weld(pts)
    F = inv[np.concatenate(T)]
    keep = (F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 2] != F[:, 0])
    flip_all = np.zeros(len(F), bool)
    fl, info = orient(W, F[keep])                      # degenerate (welded-away) faces take no part
    flip_all[np.flatnonzero(keep)] = fl
    info['flipped'] = int(flip_all.sum())
    masks, start = [], 0
    for t in T:
        masks.append(flip_all[start:start + len(t)])
        start += len(t)
    return g, masks, info


def apply_glb(path, g, masks):
    """Rewrite the GLB with the flipped triangles' corner order reversed (b <-> c) and the normals of corners used only by flipped
    faces negated. The original is kept once as <name>.glb.prewinding."""
    js = json.loads(json.dumps(g.js))
    blob = bytearray(g.blob)
    from body_fit import _DTYPE, _NCOMP   # noqa: E402
    for k, mask in enumerate(masks):
        if not mask.any():
            continue
        prim = g.prims[k]['def']
        if 'indices' not in prim:
            raise ValueError(f'{path}: primitive {k} has no index buffer (not supported)')
        acc = js['accessors'][prim['indices']]
        view = js['bufferViews'][acc['bufferView']]
        dtype = np.dtype(_DTYPE[acc['componentType']])
        offset = view.get('byteOffset', 0) + acc.get('byteOffset', 0)
        idx = np.ndarray((acc['count'],), dtype, blob, offset, (view.get('byteStride') or dtype.itemsize,))
        tri = idx[:len(idx) // 3 * 3].reshape(-1, 3)
        rows = np.flatnonzero(mask)
        tri[rows, 1], tri[rows, 2] = tri[rows, 2].copy(), tri[rows, 1].copy()
        nacc = prim['attributes'].get('NORMAL')
        if nacc is not None:
            used_ok = np.zeros(js['accessors'][nacc]['count'], bool)
            used_ok[np.unique(tri[~mask])] = True
            only_flipped = np.setdiff1d(np.unique(tri[rows]), np.flatnonzero(used_ok))
            if len(only_flipped):
                na = js['accessors'][nacc]
                nview = js['bufferViews'][na['bufferView']]
                noff = nview.get('byteOffset', 0) + na.get('byteOffset', 0)
                nst = nview.get('byteStride') or 12
                nrm = np.ndarray((na['count'], 3), np.float32, blob, noff, (nst, 4))
                nrm[only_flipped] *= -1
    import struct
    text = json.dumps(js, separators=(',', ':') if g.compact else None).encode()
    text += b' ' * (-len(text) % 4)
    chunks = [[kind, bytes(blob) if kind == 0x004E4942 else (text if kind == 0x4E4F534A else raw)] for kind, raw in g.chunks]
    body = b''.join(struct.pack('<II', len(raw), kind) + raw for kind, raw in chunks)
    path = Path(path)
    backup = path.with_name(path.name + '.prewinding')
    if not backup.exists():
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_bytes(struct.pack('<4sII', b'glTF', 2, 12 + len(body)) + body)
    tmp.replace(path)


def module_glbs(mod):
    mod = Path(mod)
    out = sorted((mod / 'reduced').glob('*.glb'))
    out += sorted((mod / 'segment_first' / 'pieces').rglob('*.glb'))
    out += sorted((mod / 'pieces').rglob('*.glb')) if (mod / 'pieces').is_dir() else []
    return [p for p in out if not p.name.endswith(SKIP_SUFFIXES) and p.is_file()]


def fix_module(mod, apply=False):
    """Check (and with apply fix) every GLB of a module. Returns {glb name: info}."""
    rep = {}
    for p in module_glbs(mod):
        try:
            g, masks, info = check_glb(p)
            if apply and info.get('flipped'):
                apply_glb(p, g, masks)
                info['fixed'] = True
        except Exception as e:  # noqa: BLE001 - report and go on
            info = {'error': f'{type(e).__name__}: {e}'}
        rep[str(p.relative_to(Path(mod)))] = info
    if apply:
        (Path(mod) / 'reduced').mkdir(exist_ok=True)
        (Path(mod) / 'reduced' / 'winding.json').write_text(json.dumps(rep, indent=1), encoding='utf-8')
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('targets', nargs='*', help='module dirs or GLB files')
    ap.add_argument('--all', action='store_true', help='every module under models/')
    ap.add_argument('--race', help='with --all: only this race')
    ap.add_argument('--apply', action='store_true', help='fix in place (default: report only)')
    ap.add_argument('--summary', help='write the full report here (json)')
    a = ap.parse_args(argv)
    targets = [Path(t) for t in a.targets]
    if a.all:
        targets += sorted({p.parent.parent for p in (ROOT / 'models').glob(f'*/*/{a.race or "*"}/*/reduced/manifest.json')})
    report, n_glb, n_bad, n_faces = {}, 0, 0, 0
    for t in targets:
        if t.suffix == '.glb':
            g, masks, info = check_glb(t)
            if a.apply and info.get('flipped'):
                apply_glb(t, g, masks)
                info['fixed'] = True
            rep = {t.name: info}
        elif a.apply and module_busy(t):
            print(f'{t}: skipped, a conversion worker holds its lock')
            continue
        else:
            rep = fix_module(t, a.apply)
        report[str(t)] = rep
        for name, info in rep.items():
            n_glb += 1
            if info.get('flipped'):
                n_bad += 1
                n_faces += info['flipped']
                print(f'{t} {name}: {info["flipped"]} flipped faces ({info.get("flipped_area_pct")} % of area)'
                      f'{", fixed" if info.get("fixed") else ""}')
            if info.get('error'):
                print(f'{t} {name}: ERROR {info["error"]}')
    print(f'{len(targets)} targets, {n_glb} GLBs checked, {n_bad} with flipped faces, {n_faces} faces'
          f'{" fixed" if a.apply else " (report only; --apply fixes)"}')
    if a.summary:
        Path(a.summary).write_text(json.dumps(report, indent=1), encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
