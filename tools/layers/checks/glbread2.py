"""Minimal GLB reader for the layer builder's own files (shell_slots.glb_bytes): positions (optionally + a morph target at weight 1), faces and the
material name ('role:prov') of every face; with colours=True also the material's baseColorFactor per face.

    V, F, mats = read(path, morph='fill')                       mats: object array of material names
    V, F, mats, rgba = read(path, colours=True)
    skin = skin_mask(mats); roles, provs = split_names(mats)

Vertices are returned in the builder's Z-up space (glTF Y-up converted back); a layer GLB is not indexed (three vertices per face, in face order)."""
import json
import struct

import numpy as np

CT = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
NC = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4}


def read(path, morph=None, colours=False):
    with open(path, 'rb') as fh:
        b = fh.read()
    n = struct.unpack_from('<I', b, 12)[0]
    js = json.loads(b[20:20 + n])
    off = 20 + n
    blen = struct.unpack_from('<I', b, off)[0]
    bin_ = b[off + 8:off + 8 + blen]

    def acc(i):
        a = js['accessors'][i]
        bv = js['bufferViews'][a['bufferView']]
        o = bv.get('byteOffset', 0) + a.get('byteOffset', 0)
        dt = CT[a['componentType']]
        k = NC[a['type']]
        return np.frombuffer(bin_, dt, a['count'] * k, o).reshape(a['count'], k) if k > 1 else np.frombuffer(bin_, dt, a['count'], o)

    Vs, Fs, mats, cols, off_v = [], [], [], [], 0
    for node in js.get('nodes', []):
        if 'mesh' not in node:
            continue
        mesh = js['meshes'][node['mesh']]
        names = (mesh.get('extras') or {}).get('targetNames', [])
        for prim in mesh['primitives']:
            V = acc(prim['attributes']['POSITION']).astype(np.float64)
            if morph and morph in names and 'targets' in prim:
                V = V + acc(prim['targets'][names.index(morph)]['POSITION'])
            F = acc(prim['indices']).astype(np.int64).reshape(-1, 3) if 'indices' in prim else np.arange(len(V)).reshape(-1, 3)
            m = js['materials'][prim['material']] if 'material' in prim else {}
            Vs.append(V)
            Fs.append(F + off_v)
            mats.append(np.full(len(F), m.get('name', ''), dtype=object))
            rgba = (m.get('pbrMetallicRoughness') or {}).get('baseColorFactor', [1., 1., 1., 1.])
            cols.append(np.tile(np.array(rgba, np.float64), (len(F), 1)))
            off_v += len(V)
    V = np.concatenate(Vs)
    V = np.c_[V[:, 0], -V[:, 2], V[:, 1]]                     # glTF Y-up -> the builder's Z-up
    out = (V, np.concatenate(Fs), np.concatenate(mats))
    return out + (np.concatenate(cols),) if colours else out


def split_names(mats):
    """(role, prov) arrays of the 'role:prov' material names."""
    parts = [str(m).split(':', 1) + [''] for m in mats]
    return np.array([p[0] for p in parts]), np.array([p[1] for p in parts])


def skin_mask(mats):
    """True where the face's role is 'skin' (the role part of the 'role:prov' material name)."""
    return split_names(mats)[0] == 'skin'
