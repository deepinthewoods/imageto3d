"""Frames between the pipeline's native GLB space and the base's world space (SPEC section 3). Pure numpy.

native P (Blender coordinates of the imported GLB)
  -> review  rev = s_n * (P @ inv(R) - o_n)                    (manifest normalization_*, rotation_to_review)
  -> base    bp  = (rev - fit.translation) / fit.scale          (manifest parts.fit, trimmed ICP)
  -> world   W   = bp * (H / 2) + o_w                           (H, o_w from the base rest mesh)
`export_native` (tools/tripo_pipeline/reduce_model.py) is the forward direction of the first step."""
import numpy as np

SYMMETRY_TOLERANCE = .001      # metres: below this the rest mesh counts as symmetric about x = 0


def review_from_native(P, manifest):
    R = np.asarray(manifest['rotation_to_review'], np.float64)
    return manifest['normalization_scale'] * (np.asarray(P, np.float64) @ np.linalg.inv(R)
                                              - np.asarray(manifest['normalization_origin_rotated'], np.float64))


def native_from_review(rev, manifest):
    """Inverse of review_from_native; the expression of export_native."""
    R = np.asarray(manifest['rotation_to_review'], np.float64)
    return (np.asarray(rev, np.float64) / manifest['normalization_scale']
            + np.asarray(manifest['normalization_origin_rotated'], np.float64)) @ R


def base_from_review(rev, fit):
    return (np.asarray(rev, np.float64) - np.asarray(fit['translation'], np.float64)) / fit['scale']


def review_from_base(bp, fit):
    return np.asarray(bp, np.float64) * fit['scale'] + np.asarray(fit['translation'], np.float64)


def rest_frame(V):
    """(o_w, H): bbox centre in X/Y with the minimum Z, and the Z height, of the base rest mesh."""
    V = np.asarray(V, np.float64)
    lo, hi = V.min(0), V.max(0)
    o = (lo + hi) / 2
    o[2] = lo[2]
    return o, float(hi[2] - lo[2])


def world_from_base(bp, H, o_w):
    return np.asarray(bp, np.float64) * (H / 2) + np.asarray(o_w, np.float64)


def base_from_world(W, H, o_w):
    return (np.asarray(W, np.float64) - np.asarray(o_w, np.float64)) / (H / 2)


def native_to_world(P, manifest, fit, H, o_w):
    """The whole chain for points `P` of a native wearable GLB."""
    return world_from_base(base_from_review(review_from_native(P, manifest), fit), H, o_w)


def world_to_native(W, manifest, fit, H, o_w):
    return native_from_review(review_from_base(base_from_world(W, H, o_w), fit), manifest)


def symmetry_error(V, plane_x=0.):
    """Largest distance from a vertex mirrored about x = plane_x to the nearest vertex (brute force, chunked)."""
    V = np.asarray(V, np.float64)
    M = V.copy()
    M[:, 0] = 2 * plane_x - M[:, 0]
    worst = 0.
    for i in range(0, len(M), 256):
        d = np.linalg.norm(M[i:i + 256, None] - V[None], axis=2).min(1)
        worst = max(worst, float(d.max()))
    return worst


def mirror_plane(V, o_w=None, tolerance=SYMMETRY_TOLERANCE):
    """x of the sagittal plane: 0 when the rest mesh is symmetric about it (within `tolerance`), else the bbox centre."""
    V = np.asarray(V, np.float64)
    if symmetry_error(V, 0.) <= tolerance:
        return 0.
    return float((V[:, 0].min() + V[:, 0].max()) / 2) if o_w is None else float(o_w[0])


def mirror_mesh(V, F, plane_x=0.):
    """Reflect across x = plane_x and flip the winding so the faces keep pointing outwards."""
    V = np.array(V, np.float64)
    V[:, 0] = 2 * plane_x - V[:, 0]
    return V, np.asarray(F)[:, [0, 2, 1]].copy()
