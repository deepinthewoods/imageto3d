"""Vertex-weight helpers: rigid segments, joint exclusion and parent bones."""
import re

import numpy as np


def segment_of(bone):
    """Fold a deform bone into the rigid segment it belongs to: stretch/twist/bend helpers of
    a limb and the spine/pelvis bends are one segment, so a joint is where *segments* mix."""
    s = re.sub(r'^c_', '', bone)
    s = re.sub(r'_(stretch|twist|bend)(?=\.|$)', '', s)
    if re.match(r'(root|spine_\d+)\.x$', s):
        return 'torso.x'
    if s.startswith('toes_') or s.startswith('surface_foot'):
        return 'foot.' + s.rsplit('.', 1)[-1]
    return s


def segment_weights(W, groups):
    """(n, segments) weights summed per rigid segment, plus the segment names."""
    segs = sorted({segment_of(g) for g in groups})
    S = np.zeros((len(W), len(segs)))
    for j, g in enumerate(groups):
        S[:, segs.index(segment_of(g))] += W[:, j]
    return S, segs


def purity(S):
    """Largest single-segment share of the weight per vertex (1 = rigidly one segment)."""
    return S.max(1) / np.maximum(S.sum(1), 1e-9)


def swap_side(name):
    """`c_arm_stretch.l` <-> `c_arm_stretch.r` (centre bones unchanged)."""
    if name.endswith('.l'):
        return name[:-2] + '.r'
    if name.endswith('.r'):
        return name[:-2] + '.l'
    return name


def axial_share(V, S, axis, sigma):
    """Share of the region's own rigid segment along an axis, averaged over each cross-section.

    A vertex-wise weight is noisy on a low-poly mesh (a quad's corners differ by 0.4 although the
    whole band is one joint), so the share of the segment that owns most of the region is smoothed
    along `axis` with a Gaussian of width `sigma`; joints show up as bands where it dips."""
    own = int(np.asarray(S).sum(0).argmax())
    share = S[:, own] / np.maximum(S.sum(1), 1e-9)
    a = V @ axis
    out = np.empty(len(V))
    for s in range(0, len(V), 512):
        k = np.exp(-.5 * ((a[s:s + 512, None] - a[None]) / max(sigma, 1e-9)) ** 2)
        out[s:s + 512] = (k * share).sum(1) / k.sum(1)
    return out
