"""Stable content hashes (SPEC section 2): piece hash for edit detection, rig and base fingerprints. Pure numpy."""
import hashlib

import numpy as np


def _quant(a, step):
    """Round to a grid and return int64 (so -0.0 / float noise below the step do not change bytes)."""
    q = np.round(np.asarray(a, np.float64) / step).astype(np.int64)
    return np.ascontiguousarray(q)


def piece_hash(co, faces, channel, weights=None):
    """sha256 of vertex positions (1e-5), face indices, per-face channel and vertex-group weights (1e-3).

    `weights`: {group name: (n_vertices,) array}; all-zero groups are ignored, group order does not matter."""
    h = hashlib.sha256()
    h.update(_quant(co, 1e-5).tobytes())
    h.update(np.ascontiguousarray(faces, np.int32).tobytes())
    h.update(np.ascontiguousarray(channel, np.int32).tobytes())
    for name in sorted(weights or {}):
        w = _quant(weights[name], 1e-3)
        if w.any():
            h.update(name.encode())
            h.update(w.tobytes())
    return h.hexdigest()


def rig_fingerprint(entries):
    """sha256 over the sorted (name, parent, head, tail, roll) entries (positions rounded to 1e-5) of the
    deform bones and sockets. `entries`: iterable of (name, parent or '', head(3), tail(3), roll)."""
    h = hashlib.sha256()
    for name, parent, head, tail, roll in sorted(entries, key=lambda e: e[0]):
        h.update(f'{name}|{parent or ""}|'.encode())
        h.update(_quant(np.r_[head, tail, roll], 1e-5).tobytes())
    return h.hexdigest()


def base_fingerprint(co):
    """sha256 of the rounded rest vertex positions."""
    return hashlib.sha256(_quant(co, 1e-5).tobytes()).hexdigest()


def file_sha256(path, block=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(block), b''):
            h.update(chunk)
    return h.hexdigest()
