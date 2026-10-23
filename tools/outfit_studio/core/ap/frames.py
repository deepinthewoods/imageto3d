"""Attachment-point frames: +Y = outward normal, +Z = tangent (up / growth), +X = Y x Z."""
import numpy as np

from . import mesh


def project_tangent(n, hint, fallback):
    """`hint` projected into the plane perpendicular to the unit `n`; where it degenerates the
    `fallback` direction is used (and, failing that, any perpendicular)."""
    n = np.asarray(n, float)
    out = np.empty_like(n)
    for k in range(len(n)):
        for h in (hint[k], fallback[k] if fallback is not None else None):
            if h is None:
                continue
            t = h - n[k] * np.dot(h, n[k])
            if np.linalg.norm(t) > 1e-6:
                out[k] = t / np.linalg.norm(t)
                break
        else:
            a = np.array([1.0, 0, 0]) if abs(n[k, 0]) < .9 else np.array([0, 1.0, 0])
            t = a - n[k] * np.dot(a, n[k])
            out[k] = t / np.linalg.norm(t)
    return out


def matrices(pos, normal, tangent):
    """4x4 transforms (k,4,4) with columns X, Y, Z, position; right handed and orthonormal."""
    y = mesh.unit(normal)
    z = mesh.unit(tangent - y * (tangent * y).sum(1, keepdims=True))
    x = mesh.unit(np.cross(y, z))
    z = np.cross(x, y)
    M = np.zeros((len(pos), 4, 4))
    M[:, :3, 0], M[:, :3, 1], M[:, :3, 2], M[:, :3, 3], M[:, 3, 3] = x, y, z, pos, 1
    return M


def gradient_field(surf, d):
    """Per-vertex unit direction of increasing `d` (area-weighted average of the face gradients),
    zero where the field is flat."""
    V, F = surf['V'], surf['F']
    n = surf['fnormal']
    area = np.maximum(surf['area'], 1e-15)[:, None]
    g = np.zeros((len(F), 3))
    for k in range(3):
        e = V[F[:, (k + 2) % 3]] - V[F[:, (k + 1) % 3]]       # edge opposite corner k
        g += d[F[:, k]][:, None] * np.cross(n, e)
    g = np.where(np.isfinite(g), g, 0) / (2 * area)
    out = np.zeros_like(V)
    for k in range(3):
        for c in range(3):
            out[:, c] += np.bincount(F[:, k], (g[:, c] * area[:, 0]), len(V))
    return out
