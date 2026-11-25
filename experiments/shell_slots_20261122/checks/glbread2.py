"""Minimal GLB reader for the prototype's own files: positions (optionally + a morph target at weight 1), faces, skin flag per face."""
import json
import struct

import numpy as np

CT = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
NC = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4}


MATS = True


def read(path, morph=None):
    b = open(path, 'rb').read()
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

    Vs, Fs, skin, off_v = [], [], [], 0
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
            mat = js['materials'][prim['material']]['name'] if 'material' in prim else ''
            Vs.append(V)
            Fs.append(F + off_v)
            skin.append(np.full(len(F), mat, dtype=object) if MATS else np.full(len(F), mat.split(':')[0] == 'skin'))
            off_v += len(V)
    V = np.concatenate(Vs)
    V = np.c_[V[:, 0], -V[:, 2], V[:, 1]]                     # glTF Y-up -> the prototype's Z-up
    return V, np.concatenate(Fs), np.concatenate(skin)
