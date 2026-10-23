"""Morph targets of item pieces: base shape keys (head_*, bulk_*) transferred by barycentric deltas (SPEC section 3)."""
import numpy as np

from ..core import morph_transfer as mt


def base_deltas(base):
    """{key name: (n_vertices, 3) delta in world axes} for every base shape key except the reference."""
    keys = base.data.shape_keys
    if keys is None:
        return {}
    n = len(base.data.vertices)
    rot = np.array(base.matrix_world.to_3x3())
    out = {}
    for kb in keys.key_blocks:
        if kb == keys.reference_key:
            continue
        a, b = np.empty(n * 3), np.empty(n * 3)
        kb.data.foreach_get('co', a)
        kb.relative_key.data.foreach_get('co', b)
        out[kb.name] = (a - b).reshape(-1, 3) @ rot.T
    return out


def transfer(obj, base, probe, rest_F, deltas=None):
    """Replace the shape keys of `obj` by the transferred base keys (only those that move this piece)."""
    deltas = base_deltas(base) if deltas is None else deltas
    moved = mt.transfer_deltas(probe['tri'], probe['bary'], rest_F, deltas)
    if obj.data.shape_keys:
        obj.shape_key_clear()
    if not moved:
        return []
    n = len(obj.data.vertices)
    co = np.empty(n * 3)
    obj.data.vertices.foreach_get('co', co)
    co = co.reshape(-1, 3)
    obj.shape_key_add(name='Basis', from_mix=False)
    src = base.data.shape_keys.key_blocks
    for name, d in moved.items():
        kb = obj.shape_key_add(name=name, from_mix=False)
        kb.data.foreach_set('co', (co + d).astype(np.float32).ravel())
        kb.value = 0.0
        if name in src:
            kb.slider_min, kb.slider_max = src[name].slider_min, src[name].slider_max
    return list(moved)
