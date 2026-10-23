"""Skin weights of item pieces (SPEC section 3, Weights): rigid single-bone table and smooth transfer from the base."""
import json
from functools import lru_cache
from pathlib import Path

import numpy as np


DATA = Path(__file__).resolve().parents[1] / 'data' / 'rigid_bones.json'


@lru_cache(maxsize=1)
def table():
    return json.loads(DATA.read_text())


def rigid_bones(piece):
    """{ARP deform bone: weight} of a rigid piece, or None when the piece is skinned smoothly."""
    return table()['pieces'].get(piece)


def deform_bones(rig):
    return {b.name for b in rig.data.bones if b.use_deform}


def check_bones(rig, names):
    missing = sorted(set(names) - deform_bones(rig))
    if missing:
        raise ValueError(f'rig {rig.name!r} has no deform bone(s) {missing}')


def missing_table_bones(rig):
    """Deform bones named by the rigid table or the hips blend that this rig does not have."""
    need = {b for bones in table()['pieces'].values() for b in bones}
    hips = table()['smooth']['hips']
    need |= {hips['root'], *hips['thighs']}
    return sorted(need - deform_bones(rig))


def ensure_armature(obj, rig):
    """Parent `obj` to the rig (keeping its world position) and give it an Armature modifier."""
    obj.parent = rig
    obj.matrix_parent_inverse = rig.matrix_world.inverted()
    mod = next((m for m in obj.modifiers if m.type == 'ARMATURE'), None) or obj.modifiers.new('Armature', 'ARMATURE')
    mod.object = rig
    mod.use_vertex_groups = True


def vertex_weights(obj):
    """(group names, W (n, groups)) of a mesh object."""
    names = [g.name for g in obj.vertex_groups]
    W = np.zeros((len(obj.data.vertices), len(names)))
    for i, v in enumerate(obj.data.vertices):
        for g in v.groups:
            W[i, g.group] = g.weight
    return names, W


def set_vertex_weights(obj, names, W):
    """Replace all vertex groups of `obj` by the columns of W (n, len(names)) that carry any weight."""
    obj.vertex_groups.clear()
    for j, name in enumerate(names):
        col = W[:, j]
        used = np.flatnonzero(col > 1e-6)
        if not len(used):
            continue
        group = obj.vertex_groups.new(name=name)
        for i in used:
            group.add([int(i)], float(col[i]), 'REPLACE')


def rigid_matrix(piece, n):
    bones = rigid_bones(piece)
    names = list(bones)
    return names, np.tile([bones[b] for b in names], (n, 1)).astype(np.float64)


def limit(W, k=4):
    """Keep the k largest influences per row and normalise."""
    W = W.copy()
    if W.shape[1] > k:
        drop = np.argsort(-W, axis=1)[:, k:]
        np.put_along_axis(W, drop, 0., axis=1)
    s = W.sum(1, keepdims=True)
    return np.where(s > 1e-9, W / np.maximum(s, 1e-9), W)


def vertex_adjacency(F):
    e = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1)
    return np.unique(e, axis=0)


def smooth_rows(W, F, passes=2, factor=.5):
    """Blend every row with the mean of its mesh neighbours (the `vertex_group_smooth` operation, vectorised)."""
    edges = vertex_adjacency(F)
    if not len(edges):
        return W
    a, b = edges[:, 0], edges[:, 1]
    deg = np.bincount(np.r_[a, b], minlength=len(W)).astype(np.float64)
    for _ in range(passes):
        acc = np.zeros_like(W)
        np.add.at(acc, a, W[b])
        np.add.at(acc, b, W[a])
        mean = acc / np.maximum(deg, 1)[:, None]
        W = np.where(deg[:, None] > 0, (1 - factor) * W + factor * mean, W)
    return W


def hips_blend(W, names, x, mask, mid, gap, cfg):
    """Rows in `mask` become hips dominant: root `root_weight`, the rest shared between the thighs by x distance
    (left = +X)."""
    if not mask.any() or cfg['root'] not in names:
        return W
    W = W.copy()
    W[mask] = 0.
    thighs = [t for t in cfg['thighs'] if t in names]
    if len(thighs) < 2:
        W[mask, names.index(cfg['root'])] = 1.
        return W
    left = np.clip(.5 + (x[mask] - mid) / (2 * gap), 0, 1)
    W[mask, names.index(cfg['root'])] = cfg['root_weight']
    W[mask, names.index(cfg['thighs'][0])] = cfg['thigh_weight'] * left
    W[mask, names.index(cfg['thighs'][1])] = cfg['thigh_weight'] * (1 - left)
    return W


def base_groups(base, rig):
    """(names, Wb (n_base_vertices, groups)): the base's vertex groups that name deform bones of the rig."""
    deform = deform_bones(rig)
    names = [g.name for g in base.vertex_groups if g.name in deform]
    index = {g.index: names.index(g.name) for g in base.vertex_groups if g.name in names}
    Wb = np.zeros((len(base.data.vertices), len(names)))
    for i, v in enumerate(base.data.vertices):
        for g in v.groups:
            if g.group in index:
                Wb[i, index[g.group]] = g.weight
    return names, Wb


def smooth_weights(V, F, probe, rest, base, rig, hips_vertices=None):
    """Transfer the base weights to the vertices `V` (nearest face, interpolated), limit to 4, normalise and smooth."""
    cfg = table()['smooth']
    names, Wb = base_groups(base, rig)
    if len(Wb) != rest['n_vertices']:
        raise ValueError('base vertex count differs from its rest mesh (modifiers that change topology?)')
    W = np.einsum('nj,njg->ng', probe['bary'], Wb[rest['F'][probe['tri']]])
    W = limit(W, cfg['influences'])
    W = smooth_rows(W, F, cfg['smooth_passes'], cfg['smooth_factor'])
    W = limit(W, cfg['influences'])
    if hips_vertices is not None and hips_vertices.any():
        h = cfg['hips']
        W = hips_blend(W, names, V[:, 0], hips_vertices, rest['mid_x'], max(rest['thigh_gap'], 1e-3), h)
    return names, W


def hips_faces(F, face_part, centroids, rest, margin=0.):
    """Face mask for the hips-dominant blend: body part `skirt`, or below the crotch and between the thighs."""
    mask = np.asarray(face_part) == 'skirt'
    if rest.get('crotch_z') is not None:
        c = np.asarray(centroids)
        mask |= (c[:, 2] < rest['crotch_z']) & (np.abs(c[:, 0] - rest['mid_x']) < rest['thigh_gap'])
    return mask


def bind_piece(obj, piece, item_type, rig, rest, base, probe, face_part=None):
    """Write the vertex groups of `obj`: rigid table weights for armour pieces, smooth transfer otherwise."""
    V = np.empty(len(obj.data.vertices) * 3)
    obj.data.vertices.foreach_get('co', V)
    V = V.reshape(-1, 3)
    corner = np.empty(len(obj.data.loops), np.int64)
    obj.data.loops.foreach_get('vertex_index', corner)
    if item_type != 'outfit' and rigid_bones(piece):
        names, W = rigid_matrix(piece, len(V))
        check_bones(rig, names)
        mode = 'rigid'
    else:
        F = corner.reshape(-1, 3)
        hv = None
        if face_part is not None:
            fm = hips_faces(F, face_part, V[F].mean(1), rest)
            hv = np.zeros(len(V), bool)
            hv[F[fm].ravel()] = True
        names, W = smooth_weights(V, F, probe, rest, base, rig, hv)
        mode = 'smooth'
    set_vertex_weights(obj, names, W)
    ensure_armature(obj, rig)
    obj['os_weights'] = mode
    return mode


def dominant_bone(obj):
    """Name of the highest weighted group per vertex ('' for none)."""
    names, W = vertex_weights(obj)
    if not names:
        return [''] * len(obj.data.vertices)
    return [names[j] if W[i, j] > 0 else '' for i, j in enumerate(W.argmax(1))]
