"""The Amount morph's layer guard (2026-11-24; the rest of the prototype's swappable-pieces driver was removed with the move to tools/layers/).

`fill_layer_guard` (off: layers.LAYER_GUARD = False, user 2026-11-24 'we don't need that guard') keeps the Amount expansion from pushing one garment
layer through another. The original pieces driver is in commit a626d23 / tag pre-pipeline-v2 under experiments/shell_slots_20261122/pieces.py.
"""
import numpy as np


FILL_LAYER_GAP_MM = 3.0      # the expansion stops a garment vertex this short of any other garment surface in its way


def fill_layer_guard(out, U1, gd):
    """The expansion must not push one garment layer through another (a double-walled skirt's inner wall lies inside the body, is pushed out
    past the outer wall and shows its back: gamurra dress 10.6 % of what is seen). Each moved garment vertex looks along its move for another
    garment surface (as it was, not touching the vertex); the move ends FILL_LAYER_GAP_MM short of it. In place on U1. Returns notes."""
    from shell_slots import SKIN
    gmask = out.role != SKIN
    D = U1 - out.V
    L = np.linalg.norm(D, axis=1)
    gv = np.unique(out.F[gmask])
    mv = gv[L[gv] > 1e-3]
    if not len(mv):
        return {'clamped': 0}
    Fg = out.F[gmask]
    T = out.V[Fg]
    d = D[mv] / L[mv, None]
    gap = FILL_LAYER_GAP_MM / 1000
    t = np.full(len(mv), np.inf)
    for k in range(0, len(mv), 64):                                   # per vertex: its own faces do not count
        sl = slice(k, k + 64)
        tt = gd.ray_first_hit(out.V[mv[sl]] + d[sl] * 1e-4, d[sl], T[:, 0], T[:, 1], T[:, 2])
        t[sl] = tt
    own = np.zeros(len(mv), bool)
    hit = np.isfinite(t) & (t < L[mv] + gap)
    if hit.any():                                                     # re-check hits against faces not touching the vertex
        for i in np.flatnonzero(hit):
            v = mv[i]
            keep = ~(Fg == v).any(1)
            tt = gd.ray_first_hit(out.V[v][None] + d[i][None] * 1e-4, d[i][None], T[keep, 0], T[keep, 1], T[keep, 2])[0]
            t[i] = tt
            own[i] = not (np.isfinite(tt) and tt < L[v] + gap)
    clamp = np.isfinite(t) & (t < L[mv] + gap) & ~own
    newL = np.maximum(t[clamp] - gap, 0.)
    U1[mv[clamp]] = out.V[mv[clamp]] + d[clamp] * newL[:, None]
    return {'clamped': int(clamp.sum()), 'moved': int(len(mv))}
