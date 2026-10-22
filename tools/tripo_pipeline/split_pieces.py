"""Split a reduced armour wearable into rigid pieces by face colour label: LEFT pauldron, RIGHT pauldron, rest.

Usage (plain python3 with numpy + Pillow; the .blend step starts Blender itself, or runs in-process when this file
is run by Blender):

    python3 tools/tripo_pipeline/split_pieces.py <reduced_dir> [--style roles|rim] [--output-dir DIR] [--levels 0p4,0p2]
        [--roles accent,tertiary] [--region-parts shoulder,arm_stretch] [--seed-in-region] [--min-island N]
        [--close-rings N] [--max-field-share F] [--plane-x X] [--module body_armor] [--no-blend] [--no-render]

`<reduced_dir>` is a reduce_model.py output (`manifest.json`, `lod_error_<lv>.glb`, `lod_error_<lv>__wearable.glb`,
`lod_error_<lv>_colors.npz`). Per level the WEARABLE GLB is split (its materials are the garment roles; body parts come
from the npz via the full GLB, matched by face centroid because the wearable GLB can have a different face count than the
npz arrays). Output (default `<reduced_dir>/../pieces/`, never into an existing level folder):

    lod_<lv>/{pauldron_l,pauldron_r,rest}.glb   faces and materials of each piece, vertices NOT shared between pieces
    lod_<lv>/pieces_<lv>.blend                  the three objects, named and coloured (object colour) by piece
    lod_<lv>/pieces_render.png                  front, back and exploded views in flat piece colours
    lod_<lv>/faces.npz                          per wearable face: piece, side, role, body part, flags
    pieces.json                                 per level and piece: faces, area, components, bbox, L/R mirror stats,
                                                absorbed islands, ambiguous faces, warnings

Styles (config.json `piece_split[<module>]`, `--style` overrides):

* roles: the pauldron faces are the garment roles of the piece (`tertiary`, `accent`); the rest of the armour only carries
  the other roles (primary, secondary).
* rim: the pauldron is the `accent` rim PLUS the wear faces it encloses: connected components (shared edges, welded
  vertices) of the non-accent wear faces that are not connected to the main body of the armour (largest component).
  A rim with a gap leaks, so a second pass erodes the non-accent faces `close_rings` rings from the rim, finds the cores
  that fall off the main body, and floods the eroded faces back to the nearest core.

Both: a seed component whose area is mostly outside the shoulder/upper-arm body parts (`region_parts`) stays in `rest`
(a stray speck elsewhere is not a pauldron); sides come from the body part suffix (`.l` anatomical left = +X, `.r` = -X in
the GLB frame; `.x` and unlabelled faces from the sagittal plane); islands under `min_island_faces` are absorbed by the
neighbouring piece; a second detached patch of a pauldron under `extra_component_share` of its main patch goes to rest.
Pieces are rigid: nothing is closed, remeshed or moved.

GLB frame: X lateral (anatomical right is -X), Y up, front is +Z (the glTF exporter's Y-up conversion of the Blender frame).
"""
import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
from collections import deque
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
CONFIG = HERE / 'config.json'
class _Colors(dict):
    """Piece colours; every `soft_<joint>` piece shares the soft colour."""

    def __missing__(self, key):
        if str(key).startswith('soft'):
            return self['soft']
        raise KeyError(key)


PIECE_COLORS = _Colors({'pauldron_l': (235, 105, 40), 'pauldron_r': (40, 150, 235), 'rest': (170, 172, 176), 'torso': (170, 172, 176),
                        'arm_l': (150, 90, 200), 'arm_r': (60, 170, 110), 'soft': (225, 190, 120), 'chest': (170, 172, 176),
                        'pelvis': (115, 135, 165), 'thigh_l': (205, 80, 150), 'thigh_r': (60, 150, 140)})
LABEL_NAMES = ['rest', 'L', 'R']          # label 0 = rest, 1 = left (+X), 2 = right (-X)
WEAK_SOURCES = ('prior', 'neighbours')
MATCH_TOL = 1e-4                           # centroid match tolerance, fraction of body height
WELD = 1e-5                                # vertex weld grid, fraction of body height
EXPLODE = .15                              # pauldron offset in the exploded view, fraction of body height

# ----------------------------------------------------------------------------------------------- GLB io

_DTYPE = {5120: 'i1', 5121: 'u1', 5122: '<i2', 5123: '<u2', 5125: '<u4', 5126: '<f4'}
_NCOMP = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4}


def _accessor(js, blob, index):
    acc = js['accessors'][index]
    view = js['bufferViews'][acc['bufferView']]
    dtype, ncomp = np.dtype(_DTYPE[acc['componentType']]), _NCOMP[acc['type']]
    offset = view.get('byteOffset', 0) + acc.get('byteOffset', 0)
    stride = view.get('byteStride') or dtype.itemsize * ncomp
    arr = np.ndarray((acc['count'], ncomp), dtype, blob, offset, (stride, dtype.itemsize))
    return arr.copy()


def read_glb(path):
    """Triangle soup of a GLB: {'v' (n,3) float, 'f' (F,3) int, 'face_material' (F,) int, 'materials': [{name, color}]}.
    Primitives are concatenated in file order (face order = the GLB's face order). Node transforms are not supported."""
    data = Path(path).read_bytes()
    magic, version, length = struct.unpack_from('<4sII', data, 0)
    if magic != b'glTF':
        raise ValueError(f'{path}: not a GLB')
    offset, js, blob = 12, None, b''
    while offset < len(data):
        size, kind = struct.unpack_from('<II', data, offset)
        chunk = data[offset + 8:offset + 8 + size]
        offset += 8 + size
        if kind == 0x4E4F534A:
            js = json.loads(chunk)
        elif kind == 0x004E4942:
            blob = chunk
    for node in js.get('nodes', []):
        if any(k in node for k in ('matrix', 'translation', 'rotation', 'scale')):
            m = node.get('matrix')
            identity = m is not None and np.allclose(m, np.eye(4).ravel())
            if not identity:
                raise ValueError(f'{path}: node transforms are not supported (apply them before export)')
    materials = [{'name': m.get('name', f'material{i}'),
                  'color': list(m.get('pbrMetallicRoughness', {}).get('baseColorFactor', [.8, .8, .8, 1.]))}
                 for i, m in enumerate(js.get('materials', []))]
    vs, fs, ms, base = [], [], [], 0
    for mesh in js['meshes']:
        for prim in mesh['primitives']:
            if prim.get('mode', 4) != 4:
                raise ValueError(f'{path}: only triangle primitives are supported')
            pos = _accessor(js, blob, prim['attributes']['POSITION']).astype(np.float64)
            if 'indices' in prim:
                idx = _accessor(js, blob, prim['indices']).astype(np.int64).ravel()
            else:
                idx = np.arange(len(pos))
            tri = idx.reshape(-1, 3) + base
            vs.append(pos)
            fs.append(tri)
            ms.append(np.full(len(tri), prim.get('material', 0), np.int64))
            base += len(pos)
    if not materials:
        materials = [{'name': 'default', 'color': [.8, .8, .8, 1.]}]
    return {'v': np.concatenate(vs) if vs else np.zeros((0, 3)), 'f': np.concatenate(fs) if fs else np.zeros((0, 3), np.int64),
            'face_material': np.concatenate(ms) if ms else np.zeros(0, np.int64), 'materials': materials}


def write_glb(path, name, v, f, face_material, materials):
    """Write faces `f` of vertex array `v` as a GLB: one mesh/node `name`, one primitive per used material, flat
    per-face normals, unwelded vertices (the same layout the Blender exporter produced for the wearable)."""
    v = np.asarray(v, np.float32)
    f = np.asarray(f, np.int64)
    face_material = np.asarray(face_material, np.int64)
    used = [m for m in np.unique(face_material)] if len(f) else []
    chunks, views, accessors, prims, gl_materials = [], [], [], [], []
    offset = 0

    def add(blob, target):
        nonlocal offset
        views.append({'buffer': 0, 'byteOffset': offset, 'byteLength': len(blob), 'target': target})
        chunks.append(blob + b'\0' * (-len(blob) % 4))
        offset += len(chunks[-1])
        return len(views) - 1

    for mi, mat in enumerate(used):
        sel = f[face_material == mat]
        tri = v[sel]                                        # (n,3,3)
        pos = tri.reshape(-1, 3)
        normal = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        normal = normal / np.maximum(np.linalg.norm(normal, axis=1, keepdims=True), 1e-20)
        normals = np.repeat(normal, 3, axis=0).astype(np.float32)
        idx = np.arange(len(pos), dtype=np.uint32)
        a_pos = len(accessors)
        accessors.append({'bufferView': add(pos.astype('<f4').tobytes(), 34962), 'componentType': 5126, 'count': len(pos),
                          'type': 'VEC3', 'max': pos.max(0).astype(float).tolist(), 'min': pos.min(0).astype(float).tolist()})
        accessors.append({'bufferView': add(normals.astype('<f4').tobytes(), 34962), 'componentType': 5126,
                          'count': len(pos), 'type': 'VEC3'})
        accessors.append({'bufferView': add(idx.astype('<u4').tobytes(), 34963), 'componentType': 5125,
                          'count': len(idx), 'type': 'SCALAR'})
        prims.append({'attributes': {'POSITION': a_pos, 'NORMAL': a_pos + 1}, 'indices': a_pos + 2, 'material': mi})
        src = materials[int(mat)]
        gl_materials.append({'name': src['name'], 'doubleSided': True,
                             'pbrMetallicRoughness': {'baseColorFactor': list(src['color']), 'metallicFactor': 0.,
                                                      'roughnessFactor': .9}})
    js = {'asset': {'version': '2.0', 'generator': 'split_pieces.py'}, 'scene': 0, 'scenes': [{'nodes': [0]}],
          'nodes': [{'mesh': 0, 'name': name}], 'meshes': [{'name': name, 'primitives': prims}],
          'materials': gl_materials, 'accessors': accessors, 'bufferViews': views,
          'buffers': [{'byteLength': offset}]}
    jb = json.dumps(js, separators=(',', ':')).encode()
    jb += b' ' * (-len(jb) % 4)
    binary = b''.join(chunks)
    total = 12 + 8 + len(jb) + 8 + len(binary)
    out = struct.pack('<4sII', b'glTF', 2, total) + struct.pack('<II', len(jb), 0x4E4F534A) + jb \
        + struct.pack('<II', len(binary), 0x004E4942) + binary
    Path(path).write_bytes(out)


# ----------------------------------------------------------------------------------------------- mesh graph

def weld(v, tol):
    """Welded vertex ids by position on a grid of `tol`: (inverse index per input vertex, welded positions)."""
    key = np.round(np.asarray(v, float) / tol).astype(np.int64)
    _, first, inverse = np.unique(key, axis=0, return_index=True, return_inverse=True)
    return inverse.ravel(), np.asarray(v, float)[first]


def face_pairs(f):
    """(E,2) face index pairs that share an edge, each pair once, with (E,) shared edge counts."""
    n = len(f)
    if not n:
        return np.zeros((0, 2), np.int64), np.zeros(0, np.int64)
    a = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
    b = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
    face = np.tile(np.arange(n), 3)
    lo, hi = np.minimum(a, b), np.maximum(a, b)
    key = lo * (int(f.max()) + 1) + hi
    order = np.argsort(key, kind='stable')
    key, face = key[order], face[order]
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    ends = np.r_[starts[1:], len(key)]
    pairs = []
    for s, e in zip(starts, ends):
        if e - s == 2:
            pairs.append((face[s], face[s + 1]))
        elif e - s > 2:
            for i in range(s, e):
                for j in range(i + 1, e):
                    pairs.append((face[i], face[j]))
    if not pairs:
        return np.zeros((0, 2), np.int64), np.zeros(0, np.int64)
    pairs = np.sort(np.array(pairs, np.int64), axis=1)
    pairs = pairs[pairs[:, 0] != pairs[:, 1]]
    uniq, counts = np.unique(pairs, axis=0, return_counts=True)
    return uniq, counts


class Graph:
    """Face adjacency (shared edges) as pairs and CSR neighbour lists."""

    def __init__(self, f):
        self.n = len(f)
        self.pairs, self.weight = face_pairs(f)
        both = np.concatenate([self.pairs, self.pairs[:, ::-1]]) if len(self.pairs) else np.zeros((0, 2), np.int64)
        w = np.concatenate([self.weight, self.weight])
        order = np.argsort(both[:, 0], kind='stable') if len(both) else np.zeros(0, np.int64)
        self.nbr = both[order, 1] if len(both) else np.zeros(0, np.int64)
        self.nbr_w = w[order] if len(both) else np.zeros(0, np.int64)
        self.indptr = np.r_[0, np.cumsum(np.bincount(both[:, 0], minlength=self.n))] if len(both) else np.zeros(self.n + 1, np.int64)

    def neighbours(self, i):
        s, e = self.indptr[i], self.indptr[i + 1]
        return self.nbr[s:e], self.nbr_w[s:e]

    def components(self, mask=None, same=None):
        """Component ids (-1 outside `mask`). `same` (n,) restricts links to faces with equal value (labels)."""
        parent = list(range(self.n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        p = self.pairs
        if len(p):
            keep = np.ones(len(p), bool)
            if mask is not None:
                keep &= mask[p[:, 0]] & mask[p[:, 1]]
            if same is not None:
                keep &= same[p[:, 0]] == same[p[:, 1]]
            for a, b in p[keep]:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
        roots = np.array([find(i) for i in range(self.n)], np.int64)
        comp = np.full(self.n, -1, np.int64)
        valid = np.ones(self.n, bool) if mask is None else mask
        if valid.any():
            _, inv = np.unique(roots[valid], return_inverse=True)
            comp[valid] = inv
        return comp

    def depth(self, sources, allowed, max_depth):
        """Ring distance (shared edges) from `sources` through faces in `allowed`; -1 = not reached within max_depth."""
        depth = np.full(self.n, -1, np.int64)
        frontier = np.flatnonzero(sources)
        depth[frontier] = 0
        for d in range(1, max_depth + 1):
            nxt = []
            for i in frontier:
                for j in self.neighbours(i)[0]:
                    if depth[j] < 0 and allowed[j]:
                        depth[j] = d
                        nxt.append(j)
            frontier = np.array(nxt, np.int64)
            if not len(frontier):
                break
        return depth


def triangle_geometry(v, f):
    tri = v[f]
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    norm = np.linalg.norm(cross, axis=1)
    return tri.mean(1), norm / 2, cross / np.maximum(norm[:, None], 1e-20)


# ----------------------------------------------------------------------------------------------- config

def load_spec(module, style=None, config=CONFIG, overrides=None):
    """Resolved piece settings: {'module', 'style', 'piece', ...} from config.json `piece_split[module]`."""
    cfg = json.loads(Path(config).read_text()).get('piece_split', {})
    if module not in cfg:
        raise SystemExit(f'config has no piece_split for module {module!r} (have {[k for k in cfg if k != "note"]})')
    mod = cfg[module]
    pieces = mod['pieces']
    if len(pieces) != 1:
        raise SystemExit('exactly one sided piece per module is supported')
    name, spec = next(iter(pieces.items()))
    style_arg = style
    style = style or mod['style']
    if style not in spec['styles']:
        raise SystemExit(f'style {style!r} not in {sorted(spec["styles"])}')
    out = {'module': module, 'style': style, 'piece': name, 'sided': spec.get('sided', True),
           'region_parts': list(spec.get('region_parts', [])), 'min_region_share': spec.get('min_region_share', .5),
           'min_island_faces': spec.get('min_island_faces', 6), 'extra_component_share': spec.get('extra_component_share', .25),
           'roles': list(spec['styles'][style]['roles']), 'close_rings': spec['styles'][style].get('close_rings', 0),
           'max_field_share': spec['styles'][style].get('max_field_share', .2),
           'min_enclosure': spec['styles'][style].get('min_enclosure', .6), 'seed_in_region': False,
           'style_given': style_arg is not None, 'all_styles': json.loads(json.dumps(spec['styles'])), 'expect': None}
    out['rigid'] = json.loads(json.dumps(mod.get('rigid', {})))       # deep copy of the section settings
    out.update(rigid_method=None, overlap=False, pose_test=False, joint_l=None, joint_r=None, soft_mode='piece', joints='arms',
               joint_hip_l=None, joint_hip_r=None, joint_waist=None, cross_joint=None, flaps=None)
    nested = {'snap_band': ('snap', 'band'), 'snap_prior': ('snap', 'prior'), 'overlap_length': ('overlap', 'length'),
              'overlap_offset': ('overlap', 'offset'), 'arm_parts': (None, 'arm_parts'), 'overlap_mode': ('overlap', 'mode'),
              'pose_convention': ('pose', 'convention')}
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if key in nested:
            group, leaf = nested[key]
            (out['rigid'].setdefault(group, {}) if group else out['rigid'])[leaf] = value
        else:
            out[key] = value
    if out['rigid_method'] == 'none':
        out['rigid_method'] = False                                  # explicitly off (also for separable reductions)
    if out['rigid_method'] not in (None, False, 'zones', 'snap', 'seam', 'colour', 'bands', 'regions', 'given', 'auto'):
        raise SystemExit(f'rigid method {out["rigid_method"]!r} unknown (zones, snap, seam, colour, bands, regions, given, auto, none)')
    if out['joints'] not in ('arms', 'all'):
        raise SystemExit("joints must be 'arms' or 'all'")
    if out['soft_mode'] not in ('piece', 'nearest'):
        raise SystemExit("soft mode must be 'piece' or 'nearest'")
    if out['rigid_method'] and not out['rigid']:
        raise SystemExit(f'config piece_split[{module}] has no rigid settings')
    return out


# ----------------------------------------------------------------------------------------------- level data

class Level:
    """One reduction level of the wearable: welded face graph + per-face roles, body parts and label evidence."""

    def __init__(self, name, raw_v, raw_f, face_material, materials, height, plane=None):
        self.name = name
        self.raw_v, self.raw_f, self.face_material, self.materials = raw_v, raw_f, face_material, materials
        self.height = float(height)
        wid, self.v = weld(raw_v, self.height * WELD)
        self.f = wid[raw_f]
        self.n = len(raw_f)
        self.centroid, self.area, self.normal = triangle_geometry(raw_v, raw_f)
        self.graph = Graph(self.f)
        self.role_names = [m['name'] for m in materials]
        self.face_role = face_material.copy()
        self.part = None            # (n,) index into part_names, -1 unknown
        self.part_names = []
        self.source = None          # (n,) label source index (-1 unknown)
        self.source_names = []
        self.confidence = None
        self.parts_info = {'method': 'none'}
        self.plane = plane or {'x': None, 'source': 'none'}
        self.joints = None

    def role_mask(self, roles):
        ids = [i for i, n in enumerate(self.role_names) if n in roles]
        return np.isin(self.face_role, ids)

    def part_base(self):
        """Per face part name without the .l/.r/.x suffix ('' when unknown)."""
        if self.part is None:
            return np.full(self.n, '', object)
        names = np.array([p.rsplit('.', 1)[0] for p in self.part_names] + [''], object)
        return names[np.where(self.part < 0, len(self.part_names), self.part)]

    def part_side(self):
        """+1 anatomical left (.l), -1 right (.r), 0 unknown/central."""
        side = np.zeros(self.n, np.int64)
        if self.part is None:
            return side
        by = np.array([1 if p.endswith('.l') else -1 if p.endswith('.r') else 0 for p in self.part_names] + [0])
        return by[np.where(self.part < 0, len(self.part_names), self.part)]


def nearest_centroid(query, ref, chunk=512):
    """(index, distance) of the nearest `ref` row for every `query` row (brute force in chunks; no scipy)."""
    idx = np.zeros(len(query), np.int64)
    dist = np.zeros(len(query))
    for s in range(0, len(query), chunk):
        d = np.linalg.norm(query[s:s + chunk, None, :] - ref[None, :, :], axis=2)
        idx[s:s + chunk] = d.argmin(1)
        dist[s:s + chunk] = d.min(1)
    return idx, dist


def align_by_area(ref, cur, rtol=1e-3, look=10):
    """Index into `ref` (face areas in the npz GLB order) for every face of `cur` (areas of the exported GLB), -1 when none:
    a two-pointer walk with a short look-ahead that skips the few faces one side has and the other lacks."""
    out = -np.ones(len(cur), np.int64)
    same = lambda a, b: abs(a - b) <= rtol * max(a, b, 1e-12)
    i = j = 0
    while i < len(ref) and j < len(cur):
        if same(ref[i], cur[j]):
            out[j] = i
            i += 1
            j += 1
            continue
        skip_ref = next((k for k in range(1, look + 1) if i + k < len(ref) and same(ref[i + k], cur[j])), None)
        skip_cur = next((k for k in range(1, look + 1) if j + k < len(cur) and same(ref[i], cur[j + k])), None)
        if skip_ref is not None and (skip_cur is None or skip_ref <= skip_cur):
            i += skip_ref
        elif skip_cur is not None:
            j += skip_cur
        else:
            i += 1
            j += 1
    return out


def attach_parts(level, full_glb, npz):
    """Give every wearable face its body part (and label source/confidence) by centroid match against the full GLB
    (`body_part_glb_order` etc. are in that GLB's face order); robust against wearable GLBs whose face count differs
    from the npz `wearable_*` arrays. Falls back to the wearable arrays when the counts agree and there is no full GLB."""
    z = np.load(npz, allow_pickle=True) if npz is not None and Path(npz).is_file() else None
    if z is None or 'body_part_names' not in z.files:
        level.parts_info = {'method': 'none', 'reason': 'no body parts in the npz'}
        return
    names = [str(n) for n in z['body_part_names']]
    info = {'method': 'none', 'wearable_faces': level.n}
    wear_arr = z['wearable_body_part_glb_order'] if 'wearable_body_part_glb_order' in z.files else None
    if full_glb is not None and Path(full_glb).is_file() and 'body_part_glb_order' in z.files:
        full = read_glb(full_glb)
        fcent = triangle_geometry(full['v'], full['f'])[0]
        parts_full = z['body_part_glb_order']
        have_src = 'face_label_source' in z.files and 'glb_face_order' in z.files
        src_full = z['face_label_source'][z['glb_face_order']] if have_src else None
        conf_full = z['face_confidence'][z['glb_face_order']] if have_src else None
        info['full_glb_faces'] = int(len(full['f']))
        if len(full['f']) != len(parts_full) and 'face_area' in z.files and 'glb_face_order' in z.files:
            # the exporter dropped (or added) a few invalid faces: align the GLB face sequence with the npz one by face area
            ref = z['face_area'][z['glb_face_order']]
            cur = triangle_geometry(full['v'], full['f'])[1]
            cur = cur * (ref.sum() / max(cur.sum(), 1e-20))      # the npz areas are in the normalized frame
            match = align_by_area(ref, cur)
            if (match >= 0).mean() > .95:
                aligned = np.where(match >= 0, parts_full[np.maximum(match, 0)], -1)
                for k in np.flatnonzero(aligned < 0):         # unmatched: the part of the nearest matched face
                    ok = np.flatnonzero(aligned >= 0)
                    aligned[k] = aligned[ok[np.linalg.norm(fcent[ok] - fcent[k], axis=1).argmin()]]
                info['aligned_by_area'] = {'npz_faces': int(len(parts_full)), 'glb_faces': int(len(cur)), 'unmatched': int((match < 0).sum())}
                parts_full = aligned
                if have_src:
                    src_full = np.where(match >= 0, src_full[np.maximum(match, 0)], 0)
                    conf_full = np.where(match >= 0, conf_full[np.maximum(match, 0)], 0.)
        if len(full['f']) == len(parts_full):
            idx, dist = nearest_centroid(level.centroid, fcent)
            ok = dist <= MATCH_TOL * level.height
            level.part = np.where(ok, parts_full[idx], -1).astype(np.int64)
            far = ~ok
            if far.any():                                  # no exact twin (mirrored copies): take the nearest
                level.part[far] = parts_full[idx[far]]
            if have_src:
                level.source = src_full[idx].astype(np.int64)
                level.source_names = [str(n) for n in z['label_source_names']]
                level.confidence = conf_full[idx].astype(float)
            info.update(method='full_glb_centroid', matched=int(ok.sum()), unmatched=int(far.sum()),
                        max_match_distance=float(dist[ok].max()) if ok.any() else None)
            if wear_arr is not None and len(wear_arr) == level.n:
                info['wearable_array_agreement'] = float((wear_arr == level.part).mean())
            elif wear_arr is not None:
                info['wearable_array_length_mismatch'] = [int(len(wear_arr)), int(level.n)]
    if level.part is None and wear_arr is not None and len(wear_arr) == level.n:
        level.part = wear_arr.astype(np.int64)
        info['method'] = 'wearable_array'
    level.part_names = names
    level.parts_info = info


# ----------------------------------------------------------------------------------------------- sagittal plane

def parts_plane_x(parts_npz):
    """Sagittal plane x of a base parts.npz in its own frame: mean of the area-weighted '.r' and '.l' surface x
    (numpy twin of face_labels.symmetry_plane, which needs mathutils). None without .l/.r parts."""
    z = np.load(parts_npz, allow_pickle=False)
    v, f, part = z['v'].astype(float), z['f'].astype(np.int64), z['part'].astype(np.int64)
    names = [str(n) for n in z['part_names']]
    cent, area, _ = triangle_geometry(v, f)
    out = {}
    for suffix in ('.l', '.r'):
        sel = np.isin(part, [i for i, n in enumerate(names) if n.endswith(suffix)])
        if not sel.any():
            return None
        out[suffix] = float((cent[sel, 0] * area[sel]).sum() / area[sel].sum())
    return (out['.l'] + out['.r']) / 2


def find_plane(manifest, reduced_dir, level, cli_x=None):
    """Sagittal plane x in the GLB frame: --plane-x, else the manifest mirror plane, else the base parts.npz mapped
    through the manifest ICP fit, else the mid of the wearable's own .l and .r faces. All candidates are recorded."""
    cands = {}
    scale = (manifest or {}).get('normalization_scale')
    origin = (manifest or {}).get('normalization_origin_rotated') or [0, 0, 0]
    rot = np.array((manifest or {}).get('rotation_to_review') or np.eye(3))
    x_safe = scale is not None and np.allclose(rot[:, 0], [1, 0, 0], atol=1e-3)
    if cli_x is not None:
        cands['cli'] = float(cli_x)
    mir = (manifest or {}).get('mirror') if isinstance((manifest or {}).get('mirror'), dict) else None
    if mir and mir.get('enabled') and mir.get('plane_x_mesh') is not None and x_safe:
        cands['manifest_mirror'] = float(mir['plane_x_mesh']) / scale + origin[0]
    if x_safe:
        pf = (manifest or {}).get('parts') or {}
        race = Path(reduced_dir).resolve().parent.parent.name
        for path in (pf.get('file'), HERE.parents[1] / 'base_views' / race / 'parts.npz'):
            if path and Path(path).is_file():
                px = parts_plane_x(path)
                if px is not None:
                    fit = pf.get('fit') or {}
                    if fit.get('used') == 'icp':
                        px = fit['scale'] * px + fit['translation'][0]
                        label = 'parts_npz_icp'
                    else:
                        label = 'parts_npz_identity_fit'
                    cands['parts_npz'] = float(px / scale + origin[0])
                    cands['parts_npz_note'] = label
                break
    side = level.part_side()
    if (side > 0).any() and (side < 0).any():
        xl = (level.centroid[side > 0, 0] * level.area[side > 0]).sum() / level.area[side > 0].sum()
        xr = (level.centroid[side < 0, 0] * level.area[side < 0]).sum() / level.area[side < 0].sum()
        cands['wearable_lr_midpoint'] = float((xl + xr) / 2)
        cands['wearable_left_mean_x'] = float(xl)
        cands['wearable_right_mean_x'] = float(xr)
    else:
        cands['wearable_bbox_centre'] = float((level.v[:, 0].min() + level.v[:, 0].max()) / 2)
    for key in ('cli', 'manifest_mirror', 'parts_npz', 'wearable_lr_midpoint', 'wearable_bbox_centre'):
        if key in cands:
            return {'x': cands[key], 'source': key, 'candidates': cands}
    return {'x': 0., 'source': 'zero', 'candidates': cands}


# ----------------------------------------------------------------------------------------------- classification

def _comp_stats(level, comp, k):
    sel = comp == k
    return {'faces': int(sel.sum()), 'area': float(level.area[sel].sum())}


def _centroid_of(level, mask):
    a = level.area[mask]
    return (level.centroid[mask] * a[:, None]).sum(0) / a.sum() if mask.any() else np.zeros(3)


def _enclosure(graph, member, target):
    """Share of the shared-edge perimeter of `member` (bool) that borders `target` (bool); open mesh boundary is not
    a neighbour so it never counts against enclosure."""
    p, w = graph.pairs, graph.weight
    if not len(p):
        return 0.
    a, b = member[p[:, 0]], member[p[:, 1]]
    cross = a ^ b
    other = np.where(a, p[:, 1], p[:, 0])
    total = w[cross].sum()
    return float(w[cross & target[other]].sum() / total) if total else 0.


def find_fields(level, seeds, spec, exclude=None):
    """Rim style: wear faces enclosed by the seed (accent) faces; `exclude` faces are not available (already another
    piece). Returns (field bool mask, details)."""
    g = level.graph
    n = level.n
    fields = np.zeros(n, bool)
    wear_area = level.area.sum()
    base = level.part_base()
    in_region = np.isin(base, spec['region_parts']) if spec['region_parts'] and level.part is not None else np.ones(n, bool)
    max_area = spec['max_field_share'] * wear_area
    details = []

    def consider(cand_mask, how):
        """Accept/reject one candidate component; records the decision."""
        area = float(level.area[cand_mask].sum())
        region = float(level.area[cand_mask & in_region].sum() / area) if area else 0.
        enc = _enclosure(g, cand_mask, seeds | fields)
        touches = bool(enc > 0)
        reason = None
        if region < spec['min_region_share'] and level.part is not None:
            reason = f'region share {region:.2f} < {spec["min_region_share"]}'
        elif area > max_area:
            reason = f'area share {area / wear_area:.2f} > {spec["max_field_share"]}'
        elif not touches:
            reason = 'does not touch the rim'
        elif enc < spec['min_enclosure']:
            reason = f'enclosure {enc:.2f} < {spec["min_enclosure"]}'
        rec = {'how': how, 'faces': int(cand_mask.sum()), 'area': area, 'region_share': region, 'enclosure': enc,
               'centroid': _centroid_of(level, cand_mask).tolist(), 'accepted': reason is None}
        if reason:
            rec['rejected_because'] = reason
        details.append(rec)
        return reason is None

    excl = np.zeros(n, bool) if exclude is None else exclude
    for rings in sorted({0, int(spec['close_rings'])}):
        free = ~seeds & ~fields & ~excl
        if rings > 0:
            ring_depth = g.depth(seeds | fields, free, rings)
            core = free & (ring_depth < 0)
            eroded = free & (ring_depth >= 0)
        else:
            core, eroded = free, np.zeros(n, bool)
        comp = g.components(core)
        if comp.max() < 0:
            continue
        areas = np.bincount(comp[comp >= 0], weights=level.area[comp >= 0])
        main = int(areas.argmax())
        owner = comp.copy()
        if rings > 0:                                   # flood the eroded faces back to the nearest core
            queue = deque(np.flatnonzero(core))
            while queue:
                i = queue.popleft()
                for j in g.neighbours(i)[0]:
                    if eroded[j] and owner[j] < 0:
                        owner[j] = owner[i]
                        queue.append(j)
        for k in range(len(areas)):
            if k == main:
                continue
            cand = owner == k
            if consider(cand, f'rings={rings}'):
                fields |= cand
    return fields, details


def vertex_votes(level, label, members, nl):
    """Shared-VERTEX votes for a patch that has no shared-edge neighbour: per label, the number of vertices of the faces
    `members` (indices) that another label's faces also use (a patch hanging off a piece by one corner)."""
    vs = np.unique(level.f[members])
    other = np.ones(level.n, bool)
    other[members] = False
    touch = np.isin(level.f, vs)
    votes = np.zeros(nl)
    for j in np.flatnonzero(other & touch.any(1)):
        if label[j] >= 0:
            votes[label[j]] += touch[j].sum()
    return votes


def absorb_islands(level, label, min_faces, why, absorbed, names, by_vertex=False):
    """Relabel (in place) every same-label component under `min_faces` faces to the neighbouring label with the most
    shared edges (components without any other-label neighbour stay, or with `by_vertex` go to the label sharing most vertices).
    Appends one record per move to `absorbed`."""
    g = level.graph
    nl = int(label.max()) + 1
    changed = False
    for _ in range(20):
        comp = g.components(None, same=label)
        sizes = np.bincount(comp)
        moved = False
        for k in np.flatnonzero(sizes < min_faces):
            members = np.flatnonzero(comp == k)
            own = label[members[0]]
            votes = np.zeros(nl)
            for i in members:
                nb, w = g.neighbours(i)
                for j, wt in zip(nb, w):
                    if label[j] != own:
                        votes[label[j]] += wt
            vv = False
            if not votes.any() and by_vertex:
                votes, vv = vertex_votes(level, label, members, nl), True
                votes[own] = 0
            if not votes.any():
                continue
            new = int(votes.argmax())
            absorbed.append({'from': names[own], 'to': names[new], 'faces': int(len(members)),
                             'area': float(level.area[members].sum()), 'why': why + (' (attached by vertices only)' if vv else ''),
                             'centroid': _centroid_of(level, np.isin(np.arange(level.n), members)).tolist()})
            label[members] = new
            moved = changed = True
        if not moved:
            break
    return changed


def minor_components(level, label, labels, share, absorbed, names, to, exclude=(), by_vertex=False):
    """Detached patches of the labels in `labels` smaller than `share` x their main patch (area) go to label `to`, or
    (`to` None) to the neighbouring label with the most shared edges (patches without a neighbour stay; with `by_vertex` a patch
    hanging on by corners only goes to the label sharing most vertices, pauldrons included)."""
    g = level.graph
    nl = int(label.max()) + 1
    for lab in labels:
        comp = g.components(label == lab)
        if comp.max() < 1:
            continue
        areas = np.bincount(comp[comp >= 0], weights=level.area[comp >= 0])
        main = areas.argmax()
        for k in range(len(areas)):
            if k != main and areas[k] < share * areas[main]:
                m = comp == k
                dest, vv = to, False
                if dest is None:
                    votes = np.zeros(nl)
                    for i in np.flatnonzero(m):
                        nb, w = g.neighbours(i)
                        for j, wt in zip(nb, w):
                            if label[j] != lab and label[j] not in exclude:
                                votes[label[j]] += wt
                    if not votes.any() and by_vertex:
                        votes, vv = vertex_votes(level, label, np.flatnonzero(m), nl), True
                        votes[lab] = 0
                    if not votes.any():
                        continue
                    dest = int(votes.argmax())
                absorbed.append({'from': names[lab], 'to': names[dest], 'faces': int(m.sum()), 'area': float(areas[k]),
                                 'why': f'detached patch < {share} of the main patch' + (' (attached by vertices only)' if vv else ''),
                                 'centroid': _centroid_of(level, m).tolist()})
                label[m] = dest


def classify(level, spec):
    """Piece label per wearable face (0 rest, 1 left piece, 2 right piece) + the report dict."""
    g = level.graph
    n = level.n
    plane = level.plane['x'] if level.plane['x'] is not None else float(level.centroid[:, 0].mean())
    tol = level.height * .01
    report = {'warnings': []}
    base = level.part_base()
    has_parts = level.part is not None
    in_region = np.isin(base, spec['region_parts']) if (has_parts and spec['region_parts']) else np.ones(n, bool)
    if not has_parts:
        report['warnings'].append('no body parts: region test skipped, sides from the sagittal plane only')

    seeds = level.role_mask(spec['roles'])
    missing = [r for r in spec['roles'] if r not in level.role_names]
    if missing:
        report['warnings'].append(f'roles not present in this wearable: {missing}')
    report['seed_faces'] = int(seeds.sum())
    if spec['seed_in_region'] and has_parts:
        report['seed_outside_region_dropped'] = int((seeds & ~in_region).sum())
        seeds &= in_region

    # seed components that are mostly outside the shoulder / upper-arm region are strays, not pauldrons
    comp = g.components(seeds)
    strays = []
    for k in range(comp.max() + 1):
        m = comp == k
        area = float(level.area[m].sum())
        region = float(level.area[m & in_region].sum() / area) if area else 0.
        if has_parts and spec['region_parts'] and region < spec['min_region_share']:
            seeds[m] = False
            hist = {}
            for p in level.part[m]:
                name = level.part_names[p] if p >= 0 else 'unknown'
                hist[name] = hist.get(name, 0) + 1
            strays.append({'faces': int(m.sum()), 'area': area, 'region_share': region, 'parts': hist,
                           'centroid': _centroid_of(level, m).tolist()})
    report['stray_seed_components'] = strays

    pauldron = seeds.copy()
    report['fields'] = []
    if spec['style'] == 'rim':
        excl = None
        if spec.get('field_exclude_roles'):        # regions mode: a large section-colour region (arm, ...) is no pauldron field
            ids = [i for i, nm in enumerate(level.role_names) if nm in spec['field_exclude_roles']]
            comp_all = g.components(None, same=level.face_role)
            excl = np.zeros(n, bool)
            for k in range(comp_all.max() + 1):
                mk = comp_all == k
                if level.face_role[mk][0] in ids and level.area[mk].sum() >= spec['field_exclude_share'] * level.area.sum():
                    excl |= mk
            report['field_excluded_faces'] = int(excl.sum())
        fields, details = find_fields(level, seeds, spec, exclude=excl)
        pauldron |= fields
        report['fields'] = details
        report['field_faces'] = int(fields.sum())

    # sides
    part_side = level.part_side()
    plane_side = np.where(np.abs(level.centroid[:, 0] - plane) > tol, np.sign(level.centroid[:, 0] - plane), 0).astype(np.int64)
    side = np.where(part_side != 0, part_side, plane_side)
    centerline = (side == 0)
    side = np.where(side == 0, np.where(level.centroid[:, 0] >= plane, 1, -1), side)
    conflict = (part_side != 0) & (plane_side != 0) & (part_side != plane_side)
    left_faces = part_side > 0
    if left_faces.any() and (level.centroid[left_faces, 0] < plane).mean() > .5:
        report['warnings'].append('body-part .l faces are on the -X side of the plane: the left/right convention looks flipped')

    label = np.zeros(n, np.int64)
    label[pauldron & (side > 0)] = 1
    label[pauldron & (side < 0)] = 2

    # clean-up: islands, then detached minor patches of a pauldron
    absorbed = []
    mi = int(spec['min_island_faces'])
    names = dict(enumerate(LABEL_NAMES))
    absorb_islands(level, label, mi, f'island < {mi} faces', absorbed, names)
    minor_components(level, label, (1, 2), spec['extra_component_share'], absorbed, names, 0)
    absorb_islands(level, label, mi, f'island < {mi} faces (after detached patches)', absorbed, names)
    report['absorbed_islands'] = absorbed

    final = label > 0
    weak = np.zeros(n, bool)
    if level.source is not None and level.source_names:
        weak_ids = [i for i, s in enumerate(level.source_names) if s in WEAK_SOURCES]
        weak = np.isin(level.source, weak_ids)
    changed_by_cleanup = (label > 0) != pauldron
    report['ambiguous_faces'] = {
        'side_conflict': int((conflict & (final | pauldron)).sum()),
        'centerline': int((centerline & (final | pauldron)).sum()),
        'weak_label_in_pauldron': int((weak & final).sum()),
        'changed_by_cleanup': int(changed_by_cleanup.sum()),
        'total': int(((conflict | centerline | weak) & (final | pauldron)).sum() + changed_by_cleanup.sum()),
    }
    flags = np.zeros(n, np.int64)
    flags |= conflict.astype(np.int64) << 0
    flags |= centerline.astype(np.int64) << 1
    flags |= weak.astype(np.int64) << 2
    flags |= changed_by_cleanup.astype(np.int64) << 3
    flags |= (pauldron & ~seeds).astype(np.int64) << 4          # rim: absorbed field / gap face (not a seed colour)
    report['flag_bits'] = ['side_conflict', 'centerline', 'weak_label', 'changed_by_cleanup', 'enclosed_by_rim']
    return label, side, flags, report


# ----------------------------------------------------------------------------------------------- statistics

def one_piece_stats(level, label, lab):
    m = label == lab
    total_area = float(level.area.sum())
    comp = level.graph.components(m)
    comps = [_comp_stats(level, comp, k) for k in range(comp.max() + 1)]
    comps.sort(key=lambda c: -c['area'])
    pts = level.v[np.unique(level.f[m])] if m.any() else np.zeros((0, 3))
    return {
        'faces': int(m.sum()), 'area': float(level.area[m].sum()),
        'area_share': float(level.area[m].sum() / total_area) if total_area else 0.,
        'components': len(comps), 'component_list': comps,
        'bbox': {'min': pts.min(0).tolist(), 'max': pts.max(0).tolist()} if len(pts) else None,
        'centroid': _centroid_of(level, m).tolist() if m.any() else None,
    }


def mirror_stats(level, label, left, right, L, R, plane_x):
    """L/R symmetry of two pieces (label ids `left`, `right`; stats dicts L, R) across the sagittal plane."""
    sym = {'plane_x': plane_x, 'plane_source': level.plane.get('source'),
           'area_ratio_l_over_r': (L['area'] / R['area']) if R['area'] else None}
    if L['faces'] and R['faces']:
        cl, cr = np.array(L['centroid']), np.array(R['centroid'])
        refl = cl.copy()
        refl[0] = 2 * plane_x - refl[0]
        err = float(np.linalg.norm(refl - cr))
        sym.update(centroid_offset_from_plane_l=float(cl[0] - plane_x), centroid_offset_from_plane_r=float(cr[0] - plane_x),
                   centroid_mirror_error=err, centroid_mirror_error_pct_height=err / level.height * 100)
        pl, pr = level.centroid[label == left], level.centroid[label == right].copy()
        pr[:, 0] = 2 * plane_x - pr[:, 0]
        near = .025 * level.height
        sym['mirror_face_match_l_to_r'] = float((nearest_centroid(pl, pr)[1] <= near).mean())
        sym['mirror_face_match_r_to_l'] = float((nearest_centroid(pr, pl)[1] <= near).mean())
        sym['mirror_face_match_tolerance_pct_height'] = 2.5
    return sym


def piece_stats(level, label, plane_x):
    out = {key: one_piece_stats(level, label, lab) for lab, key in ((0, 'rest'), (1, 'L'), (2, 'R'))}
    return out, mirror_stats(level, label, 1, 2, out['L'], out['R'], plane_x)


# ----------------------------------------------------------------------------------------------- rigid sections

RIGID_IDS = {'torso': 0, 'pauldron_l': 1, 'pauldron_r': 2, 'arm_l': 3, 'arm_r': 4}
RIGID_COLORS = PIECE_COLORS
STRIP_COLOR = (250, 205, 0)
EDGE_CREASE_DEG = 35.


JOINT_ROLES = ('joint', 'quinary')       # the cyan joint-band role; 'quinary' is the old name used by the M4 data
JOINTS_ARMS = {'arm_l': (0, 3), 'arm_r': (0, 4)}                  # joint -> (label a, label b) of the two pieces it joins
JOINTS_ALL = {'arm_l': (0, 3), 'arm_r': (0, 4), 'waist': (0, 5), 'hip_l': (5, 6), 'hip_r': (5, 7)}
SOFT_ID = {'arm_l': 8, 'arm_r': 9, 'waist': 10, 'hip_l': 11, 'hip_r': 12}   # label of the soft band piece of a joint


def cover_rank(spec, names):
    """label -> rank (0 = outermost) from rigid.overlap.cover_order matched to the piece name prefix (torso/arm/...)."""
    order = list(spec['rigid']['overlap']['cover_order'])
    alias = {'rest': 'torso', 'chest': 'torso', 'pelvis': 'torso', 'thigh': 'arm'}
    return {lab: (order.index(alias.get(n.split('_')[0], n.split('_')[0]))
                  if alias.get(n.split('_')[0], n.split('_')[0]) in order else len(order)) for lab, n in names.items()}


def piece_names(spec, rigid, soft=False, joints='arms'):
    """label -> piece name. joints 'arms': torso, pauldrons, arms (+ 'soft' = id 5 for the M3 seam piece); joints 'all':
    chest, pauldrons, arms, pelvis, thighs (+ soft_<joint> pieces, ids SOFT_ID, when `soft`)."""
    p = spec['piece']
    if not rigid:
        return {0: 'rest', 1: f'{p}_l', 2: f'{p}_r'}
    if joints == 'all':
        out = {0: 'chest', 1: f'{p}_l', 2: f'{p}_r', 3: 'arm_l', 4: 'arm_r', 5: 'pelvis', 6: 'thigh_l', 7: 'thigh_r'}
        if soft:
            out.update({i: f'soft_{j}' for j, i in SOFT_ID.items()})
        return out
    out = {0: 'torso', 1: f'{p}_l', 2: f'{p}_r', 3: 'arm_l', 4: 'arm_r'}
    if soft:
        out[5] = 'soft'
    return out


def is_all(names):
    return 'pelvis' in names.values()


def piece_body(name, convention, all_mode):
    """Rigid body a piece belongs to under the pauldron convention (None = skinned soft piece)."""
    base, side = name.split('_')[0], name.split('_')[-1]
    if base in ('torso', 'rest', 'pelvis'):
        return 'root'
    if base == 'chest':
        return 'C'
    if base == 'thigh':
        return f'T_{side}'
    if base == 'arm':
        return f'A_{side}'
    if base == 'pauldron':
        return {'arm': f'A_{side}', 'torso': 'C' if all_mode else 'root', 'half': f'S_{side}'}[convention]
    return None


def piece_depth(name, convention, all_mode):
    """Depth in the motion hierarchy (pelvis/torso 0 < chest, thigh < arm; the pauldron one level above the arm when it
    rides the shoulder 'half', with the arm when it follows it, with the chest when it follows the torso)."""
    base = name.split('_')[0]
    cb = 1 if all_mode else 0
    if base in ('torso', 'rest', 'pelvis'):
        return 0
    if base in ('chest', 'thigh'):
        return 1
    if base == 'arm':
        return cb + 2
    if base == 'pauldron':
        return {'arm': cb + 2, 'torso': cb, 'half': cb + 1}[convention]
    return 99


def outward_sign(level):
    """+1 when the wearable's winding gives outward normals (majority test around the body axis), else -1."""
    if not hasattr(level, '_outward'):
        c, n = level.centroid, level.normal
        mid = np.array([level.plane['x'] or 0., 0., float(level.v[:, 2].mean())])
        near = np.abs(c[:, 0] - mid[0]) < .15 * level.height
        ref = c[near] - np.c_[np.full(near.sum(), mid[0]), c[near, 1], np.full(near.sum(), mid[2])]
        level._outward = 1. if ((n[near] * ref).sum(1) > 0).mean() >= .5 else -1.
    return level._outward


def edge_geometry(level):
    """Per shared edge (rows of level.graph.pairs): welded vertex ids, length, dihedral angle between the face
    normals (deg, 0 = flat) and whether the two faces have different garment roles."""
    if hasattr(level, '_edge'):
        return level._edge
    p = level.graph.pairs
    if not len(p):
        level._edge = {'verts': np.zeros((0, 2), np.int64), 'length': np.zeros(0), 'dihedral': np.zeros(0),
                       'role_diff': np.zeros(0, bool)}
        return level._edge
    A, B = level.f[p[:, 0]], level.f[p[:, 1]]
    in_b = (A[:, :, None] == B[:, None, :]).any(2)
    idx = np.argsort(~in_b, axis=1, kind='stable')[:, :2]
    verts = np.take_along_axis(A, idx, 1)
    length = np.linalg.norm(level.v[verts[:, 0]] - level.v[verts[:, 1]], axis=1)
    cosang = np.clip((level.normal[p[:, 0]] * level.normal[p[:, 1]]).sum(1), -1, 1)
    level._edge = {'verts': verts, 'length': length, 'dihedral': np.degrees(np.arccos(cosang)),
                   'role_diff': level.face_role[p[:, 0]] != level.face_role[p[:, 1]]}
    return level._edge


# ---- joints (shoulder pivots) in the GLB frame

def base_joints(parts_npz):
    """Per side {'shoulder', 'elbow'} in the base frame from a parts.npz: the arm_stretch ring shared with a non-arm
    part is the shoulder joint, the ring shared with forearm_stretch the elbow."""
    z = np.load(parts_npz, allow_pickle=False)
    v, f, part = z['v'].astype(float), z['f'].astype(np.int64), z['part'].astype(np.int64)
    names = [str(n) for n in z['part_names']]
    out = {}
    for side in ('l', 'r'):
        arm = f'arm_stretch.{side}'
        if arm not in names:
            continue
        mine = {names.index(n) for n in (arm, f'forearm_stretch.{side}', f'hand.{side}') if n in names}
        arm_v = set(f[part == names.index(arm)].ravel().tolist())
        other_v = set(f[~np.isin(part, list(mine))].ravel().tolist())
        elbow_part = names.index(f'forearm_stretch.{side}') if f'forearm_stretch.{side}' in names else None
        elbow_v = set(f[part == elbow_part].ravel().tolist()) & arm_v if elbow_part is not None else set()
        start, elbow = sorted(arm_v & other_v), sorted(elbow_v)
        if start and elbow:
            out[side] = {'shoulder': v[start].mean(0), 'elbow': v[elbow].mean(0), 'ring_vertices': len(start)}
        thigh = f'thigh_stretch.{side}'
        if thigh in names:                                   # hip = thigh ring shared with the pelvis, knee = ring to the shin
            mine = {names.index(n) for n in (thigh, f'leg_stretch.{side}', f'foot.{side}') if n in names}
            th_v = set(f[part == names.index(thigh)].ravel().tolist())
            hip = sorted(th_v & set(f[~np.isin(part, list(mine))].ravel().tolist()))
            leg = names.index(f'leg_stretch.{side}') if f'leg_stretch.{side}' in names else None
            knee = sorted(th_v & set(f[part == leg].ravel().tolist())) if leg is not None else []
            if hip:
                out.setdefault(side, {})['hip'] = v[hip].mean(0)
                if knee:
                    out[side]['knee'] = v[knee].mean(0)
    if 'root.x' in names:                                    # waist = ring between the pelvis (root) and the spine
        root_v = set(f[part == names.index('root.x')].ravel().tolist())
        spine = [i for i, n in enumerate(names) if n.startswith('spine')]
        sp_v = set(f[np.isin(part, spine)].ravel().tolist())
        waist = sorted(root_v & sp_v)
        if waist:
            out['waist'] = v[waist].mean(0)
    return out


def base_to_glb(points, manifest):
    """Base normalized frame -> GLB frame: ICP fit (scale + translation), de-normalization and rotation exactly like
    reduce_model.export_native, then the glTF exporter's Z-up -> Y-up (x, z, -y)."""
    pf = (manifest or {}).get('parts') or {}
    fit = pf.get('fit') or {}
    p = np.asarray(points, float)
    if fit.get('used') == 'icp':
        p = fit['scale'] * p + np.array(fit['translation'])
    ns = manifest['normalization_scale']
    p = (p / ns + np.array(manifest.get('normalization_origin_rotated') or [0, 0, 0])) @ np.array(
        manifest.get('rotation_to_review') or np.eye(3))
    return np.stack([p[..., 0], p[..., 2], -p[..., 1]], -1)


def find_joints(manifest, reduced_dir):
    """{'l': {'shoulder', 'elbow'}, 'r': ...} in the GLB frame (+ 'source'), or None without a parts file."""
    if not manifest or manifest.get('normalization_scale') is None:
        return None
    pf = manifest.get('parts') or {}
    race = Path(reduced_dir).resolve().parent.parent.name
    for path in (pf.get('file'), HERE.parents[1] / 'base_views' / race / 'parts.npz'):
        if path and Path(path).is_file():
            j = base_joints(path)
            if j:
                out = {s: ({k: base_to_glb(v, manifest) for k, v in d.items() if k != 'ring_vertices'} if isinstance(d, dict)
                           else base_to_glb(d, manifest)) for s, d in j.items()}
                out['source'] = f'parts_npz ({"icp fit" if (pf.get("fit") or {}).get("used") == "icp" else "identity fit"})'
                return out
    return None


def band_continuity(level, reach=.12, min_radius=.01):
    """How continuous the joint-role (cyan) band is around each joint's ring, from the labelled wearable alone.

    Every joint-role face within `reach` x height of a joint is attributed to its nearest joint (arm_l / arm_r at the
    shoulder, waist, hip_l / hip_r); the ring axis is shoulder->elbow, hip->knee, up for the waist. For each such face
    the azimuth arc spanned by its vertices around the axis is taken (the shortest arc covering them; vertices closer to
    the axis than `min_radius` x height are ignored). Returns per joint: `coverage` = fraction of the 360 degree
    circumference covered by the union of those arcs, `largest_gap_deg`, `faces`, `axial_width_pct_height` (median axial
    extent of the covered azimuth columns) and `axial_range_pct_height`. A joint without a joint point or without
    joint-role faces nearby has coverage 0."""
    out = {}
    j = level.joints or {}
    spots = {}
    for key, side in (('arm_l', 'l'), ('arm_r', 'r')):
        if side in j and 'shoulder' in j[side] and 'elbow' in j[side]:
            spots[key] = (np.asarray(j[side]['shoulder'], float), np.asarray(j[side]['elbow'], float) - np.asarray(j[side]['shoulder'], float))
    for key, side in (('hip_l', 'l'), ('hip_r', 'r')):
        if side in j and 'hip' in j[side] and 'knee' in j[side]:
            spots[key] = (np.asarray(j[side]['hip'], float), np.asarray(j[side]['knee'], float) - np.asarray(j[side]['hip'], float))
    if 'waist' in j:
        spots['waist'] = (np.asarray(j['waist'], float), np.array([0., 1., 0.]))
    if not spots:
        return {'available': False, 'reason': 'no joint positions (parts file missing)'}
    cyan = np.flatnonzero(level.role_mask(JOINT_ROLES))
    names = list(spots)
    pts = np.stack([spots[k][0] for k in names])
    if len(cyan):
        d = np.linalg.norm(level.centroid[cyan][:, None, :] - pts[None], axis=2)
        nearest = d.argmin(1)
        near_d = d.min(1)
    for n_, key in enumerate(names):
        c, axis = spots[key]
        axis = axis / max(np.linalg.norm(axis), 1e-9)
        info = {'faces': 0, 'coverage': 0., 'largest_gap_deg': 360., 'axial_width_pct_height': None, 'axial_range_pct_height': None}
        faces = cyan[(nearest == n_) & (near_d <= reach * level.height)] if len(cyan) else []
        info['faces'] = int(len(faces))
        if len(faces):
            mk = np.zeros(level.n, bool)
            mk[faces] = True
            comp = level.graph.components(mk)
            info['components'] = int(len(np.unique(comp[mk])))
            u = np.cross(axis, [0., 0., 1.] if abs(axis[2]) < .9 else [1., 0., 0.])
            u /= np.linalg.norm(u)
            w = np.cross(axis, u)
            covered = np.zeros(360, bool)
            col_lo = np.full(360, np.inf)
            col_hi = np.full(360, -np.inf)
            axials = []
            for fi in faces:
                rel = level.v[level.f[fi]] - c
                ax = rel @ axis
                perp = rel - ax[:, None] * axis
                ok = np.linalg.norm(perp, axis=1) > min_radius * level.height
                if ok.sum() < 2:
                    continue
                ang = np.degrees(np.arctan2(perp[ok] @ w, perp[ok] @ u)) % 360
                ang.sort()
                gaps = np.diff(np.concatenate([ang, [ang[0] + 360]]))
                k = int(gaps.argmax())
                start = ang[(k + 1) % len(ang)]
                span = 360 - gaps[k]
                idx = (np.floor(start + np.arange(int(np.ceil(span)) + 1)) % 360).astype(int)
                idx = idx[:max(int(np.ceil(span)), 1)]
                covered[idx] = True
                col_lo[idx] = np.minimum(col_lo[idx], ax[ok].min())
                col_hi[idx] = np.maximum(col_hi[idx], ax[ok].max())
                axials.extend(ax[ok].tolist())
            if covered.any():
                info['coverage'] = float(covered.mean())
                ring = np.concatenate([covered, covered])
                gap, best = 0, 0
                for b in ring:
                    gap = 0 if b else gap + 1
                    best = max(best, gap)
                info['largest_gap_deg'] = float(min(best, 360))
                widths = (col_hi - col_lo)[covered]
                info['axial_width_pct_height'] = float(np.median(widths) / level.height * 100)
                info['axial_range_pct_height'] = [float(min(axials) / level.height * 100), float(max(axials) / level.height * 100)]
        out[key] = info
    out['available'] = True
    return out


# ---- closest points on triangles (numpy, no scipy)

def closest_points(P, tri, chunk=None):
    """Closest point on a triangle soup `tri` (m,3,3) for every row of P (n,3): (points, triangle index, distance)."""
    P = np.asarray(P, float)
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    ab, ac, bc = b - a, c - a, c - b
    chunk = chunk or max(1, 3_000_000 // max(len(tri), 1))
    pts = np.zeros((len(P), 3))
    idx = np.zeros(len(P), np.int64)
    dist = np.zeros(len(P))
    with np.errstate(divide='ignore', invalid='ignore'):
        for s in range(0, len(P), chunk):
            p = P[s:s + chunk, None, :]
            ap, bp, cp = p - a[None], p - b[None], p - c[None]
            d1, d2 = (ab[None] * ap).sum(-1), (ac[None] * ap).sum(-1)
            d3, d4 = (ab[None] * bp).sum(-1), (ac[None] * bp).sum(-1)
            d5, d6 = (ab[None] * cp).sum(-1), (ac[None] * cp).sum(-1)
            vc, vb, va = d1 * d4 - d3 * d2, d5 * d2 - d1 * d6, d3 * d6 - d5 * d4
            denom = va + vb + vc
            res = a[None] + ab[None] * (vb / denom)[..., None] + ac[None] * (vc / denom)[..., None]
            res = np.where(((va <= 0) & (d4 - d3 >= 0) & (d5 - d6 >= 0))[..., None],
                           b[None] + bc[None] * ((d4 - d3) / ((d4 - d3) + (d5 - d6)))[..., None], res)
            res = np.where(((vb <= 0) & (d2 >= 0) & (d6 <= 0))[..., None], a[None] + ac[None] * (d2 / (d2 - d6))[..., None], res)
            res = np.where(((d6 >= 0) & (d5 <= d6))[..., None], c[None] + 0 * res, res)
            res = np.where(((vc <= 0) & (d1 >= 0) & (d3 <= 0))[..., None], a[None] + ab[None] * (d1 / (d1 - d3))[..., None], res)
            res = np.where(((d3 >= 0) & (d4 <= d3))[..., None], b[None] + 0 * res, res)
            res = np.where(((d1 <= 0) & (d2 <= 0))[..., None], a[None] + 0 * res, res)
            res = np.where(np.isfinite(res), res, a[None])
            d = np.linalg.norm(p - res, axis=2)
            k = d.argmin(1)
            rows = np.arange(len(k))
            pts[s:s + chunk], idx[s:s + chunk], dist[s:s + chunk] = res[rows, k], k, d[rows, k]
    return pts, idx, dist


# ---- max-flow / min-cut (Dinic) for the snap method

def min_cut(n, edges, s, t):
    """Source side (bool per node) of a minimum s-t cut. `edges`: (u, v, cap_uv, cap_vu) tuples."""
    to, cap, adj = [], [], [[] for _ in range(n)]
    for u, v, c1, c2 in edges:
        adj[u].append(len(to)); to.append(v); cap.append(float(c1))
        adj[v].append(len(to)); to.append(u); cap.append(float(c2))
    while True:
        level = [-1] * n
        level[s] = 0
        queue = deque([s])
        while queue:
            u = queue.popleft()
            for e in adj[u]:
                if cap[e] > 1e-12 and level[to[e]] < 0:
                    level[to[e]] = level[u] + 1
                    queue.append(to[e])
        if level[t] < 0:
            break
        it = [0] * n
        while True:                                    # blocking flow, iterative DFS
            path, u = [], s
            while u != t:
                advanced = False
                while it[u] < len(adj[u]):
                    e = adj[u][it[u]]
                    if cap[e] > 1e-12 and level[to[e]] == level[u] + 1:
                        path.append(e)
                        u = to[e]
                        advanced = True
                        break
                    it[u] += 1
                if not advanced:
                    if not path:
                        break
                    level[u] = -1
                    e = path.pop()
                    u = to[e ^ 1]
            if u != t:
                break
            push = min(cap[e] for e in path)
            for e in path:
                cap[e] -= push
                cap[e ^ 1] += push
    seen = [False] * n
    seen[s] = True
    queue = deque([s])
    while queue:
        u = queue.popleft()
        for e in adj[u]:
            if cap[e] > 1e-12 and not seen[to[e]]:
                seen[to[e]] = True
                queue.append(to[e])
    return np.array(seen)


# ---- M1 zones / M2 snap

def zone_group(level, spec):
    """Per face zone group from the transferred body part: 'arm', 'thigh', 'pelvis' (rigid.part_groups) else 'chest'."""
    r = spec['rigid']
    groups = r.get('part_groups') or {'arm': r['arm_parts'], 'thigh': ['thigh_stretch', 'leg_stretch', 'foot'], 'pelvis': ['root']}
    base = level.part_base()
    out = np.full(level.n, 'chest', object)
    for grp in ('pelvis', 'thigh', 'arm'):
        out[np.isin(base, groups.get(grp, []))] = grp
    return out


def sections_zones(level, label, side, spec, report, joints='arms'):
    """M1: after the pauldrons, faces on the arm body parts become arm_l / arm_r (ids 3 / 4), the rest stays torso.
    joints 'all': also pelvis (5, `root` part) and thigh_l / thigh_r (6 / 7), the rest is the chest (0)."""
    r = spec['rigid']
    out = label.copy()
    cand = label == 0
    if level.part is not None:
        grp = zone_group(level, spec)
        zone = cand & (grp == 'arm')
        report['zone_source'] = 'body_part ' + ','.join(r['arm_parts'])
    else:
        j = level.joints
        if joints == 'all' or not j:
            raise SystemExit('rigid sections need body parts' + ('' if joints == 'all' else ' or joints') +
                             ' (no parts in the npz' + ('' if joints == 'all' else ' and no parts.npz found') + ')')
        plane = level.plane['x']
        zone = np.zeros(level.n, bool)
        for s_, sgn in (('l', 1), ('r', -1)):
            jx = abs(j[s_]['shoulder'][0] - plane)
            zone |= cand & (side == sgn) & (np.abs(level.centroid[:, 0] - plane) > jx)
        report['zone_source'] = 'lateral distance beyond the shoulder joint (no body parts)'
        grp = None
    out[zone & (side > 0)] = 3
    out[zone & (side < 0)] = 4
    if joints == 'all':
        out[cand & (grp == 'pelvis')] = 5
        out[cand & (grp == 'thigh') & (side > 0)] = 6
        out[cand & (grp == 'thigh') & (side < 0)] = 7
    return out


def snap_weights(level, spec):
    """Cost per shared edge for the snap cut: length x (role_factor on a colour/label boundary) x (crease factor)."""
    r = spec['rigid']['snap']
    e = edge_geometry(level)
    crease = 1. / (1. + (e['dihedral'] / r['crease_deg']) ** 2)
    w = np.where(e['role_diff'], r['role_factor'], 1.) * crease
    return e['length'] * np.maximum(w, r['min_factor'])


def snap_cut(level, label, spec, a, b, info):
    """Re-cut the boundary between pieces `a` and `b` (labels) inside a band around the current cut by a minimum cut:
    terminals are the a and b faces outside the band, edge capacity = snap_weights (cheap on role boundaries and
    creases), plus a weak pull of every free face towards its current label that grows with its distance from the cut.
    Returns the new label array; `info` collects the stats."""
    r = spec['rigid']['snap']
    g = level.graph
    e = edge_geometry(level)
    cost = snap_weights(level, spec)
    pairs = g.pairs
    H = level.height
    out = label.copy()
    la, lb = label[pairs[:, 0]], label[pairs[:, 1]]
    cut = ((la == a) & (lb == b)) | ((la == b) & (lb == a))
    info.update(guess_cut_edges=int(cut.sum()), guess_cut_length=float(e['length'][cut].sum()))
    if not cut.any():
        info['skipped'] = 'no cut between the pieces'
        return out
    mids = level.v[e['verts'][cut]].mean(1)
    cand = np.flatnonzero((label == a) | (label == b))
    d = nearest_centroid(level.centroid[cand], mids)[1]
    dist = np.full(level.n, np.inf)
    dist[cand] = d
    side_b = label == b
    band = r['band'] * H
    b_cut = min(band, .5 * dist[side_b].max())      # a short piece still gets a terminal
    sink = side_b & (dist >= b_cut)
    source = (label == a) & (dist > band)
    free = np.isin(np.arange(level.n), cand) & ~sink & ~source
    info.update(band=band, free_faces=int(free.sum()), arm_terminal_faces=int(sink.sum()))
    if not free.any() or not sink.any() or not source.any():
        info['skipped'] = 'nothing free or no terminal'
        return out
    node = -np.ones(level.n, np.int64)
    node[free] = np.arange(free.sum())
    m = int(free.sum())
    S, T = m, m + 1
    edges = []
    for k in range(len(pairs)):
        i, j = pairs[k]
        c = cost[k]
        if free[i] and free[j]:
            edges.append((node[i], node[j], c, c))
        elif free[i] or free[j]:
            fi, o = (i, j) if free[i] else (j, i)
            if source[o]:
                edges.append((S, node[fi], c, c))
            elif sink[o]:
                edges.append((node[fi], T, c, c))
    mean_len = float(e['length'][cut].mean())
    for i in np.flatnonzero(free):
        u = r['prior'] * min(dist[i] / band, 1.) * level.area[i] / max(mean_len, 1e-9)
        if label[i] == a:
            edges.append((S, node[i], u, 0.))
        else:
            edges.append((node[i], T, u, 0.))
    src_side = min_cut(m + 2, edges, S, T)
    newlab = np.where(src_side[:m], a, b)
    free_idx = np.flatnonzero(free)
    moved = free_idx[newlab != label[free_idx]]
    out[free_idx] = newlab
    info.update(faces_moved=int(len(moved)), mean_shift_of_moved=float(dist[moved].mean() / H) if len(moved) else 0.)
    return out


def sections_snap(level, label, spec, report, joints='arms'):
    """M2: per joint (arms; with joints 'all' also waist and hips) re-cut the zone guess by snap_cut."""
    out = label.copy()
    report['snap'] = {}
    for name, (a, b) in (JOINTS_ALL if joints == 'all' else JOINTS_ARMS).items():
        info = report['snap'].setdefault(name, {})
        out = snap_cut(level, out, spec, a, b, info)
    return out


def cut_report(level, label, names, spec=None):
    """Per piece pair with shared edges: edge count, cut length, share of the length on a colour/label boundary, share
    on a crease (dihedral >= 35 deg) and the mean dihedral angle."""
    e = edge_geometry(level)
    cost = snap_weights(level, spec) if spec is not None and spec['rigid'].get('snap') else None
    p = level.graph.pairs
    la, lb = label[p[:, 0]], label[p[:, 1]]
    out = []
    seen = set()
    for a, b in zip(la[la != lb], lb[la != lb]):
        key = (min(a, b), max(a, b))
        if key in seen:
            continue
        seen.add(key)
        sel = ((la == key[0]) & (lb == key[1])) | ((la == key[1]) & (lb == key[0]))
        ln = e['length'][sel]
        total = float(ln.sum())
        out.append({'pair': [names[key[0]], names[key[1]]], 'edges': int(sel.sum()), 'length': total,
                    'length_pct_height': total / level.height * 100,
                    'role_boundary_share': float(ln[e['role_diff'][sel]].sum() / total) if total else 0.,
                    'crease_share': float(ln[e['dihedral'][sel] >= EDGE_CREASE_DEG].sum() / total) if total else 0.,
                    'mean_dihedral_deg': float((e['dihedral'][sel] * ln).sum() / total) if total else 0.,
                    **({'snap_cost': float(cost[sel].sum()), 'snap_cost_pct_height': float(cost[sel].sum() / level.height * 100)}
                       if cost is not None else {})})
    return sorted(out, key=lambda c: c['pair'])


def flood_nearest(level, label, free, sources):
    """Label every `free` face with the label of the nearest (shared-edge rings) face in `sources` (bool): multi-source BFS."""
    out = label.copy()
    g = level.graph
    queue = deque(np.flatnonzero(sources))
    done = sources.copy()
    while queue:
        i = queue.popleft()
        for j in g.neighbours(i)[0]:
            if free[j] and not done[j]:
                out[j] = out[i]
                done[j] = True
                queue.append(j)
    return out


def sections_seam(level, label, side, spec, report):
    """M3: the soft under-layer (garment roles `seam.soft_roles`, default secondary = blue) separates rigid plates.
    Rigid plates = connected components of the other wear faces; each goes wholly to the arm or torso by area-weighted
    body-part majority (a component with more than `mixed_share` on the minority side is split by body-part zone
    instead). Soft faces: `soft_mode` 'piece' = separate piece 'soft' (to be skinned smoothly), 'nearest' = each face joins
    the nearest rigid piece (rings over the soft band)."""
    r = spec['rigid']
    cfg = r.get('seam', {})
    g = level.graph
    out = label.copy()
    cand = label == 0
    soft = level.role_mask(cfg.get('soft_roles', ['secondary'])) & cand
    if cfg.get('soft_region_parts'):
        near_arm = np.isin(level.part_base(), cfg['soft_region_parts'])
        soft &= near_arm
    arm_zone = np.isin(level.part_base(), r['arm_parts']) if level.part is not None else np.zeros(level.n, bool)
    # a soft component only counts as the shoulder/arm band when enough of it lies on the arm parts (hip cloth etc. stays rigid)
    dropped = []
    sc = g.components(soft)
    for k in range(sc.max() + 1):
        m = sc == k
        share = float(level.area[m & arm_zone].sum() / level.area[m].sum())
        if share < cfg.get('soft_min_arm_share', .25):
            soft[m] = False
            dropped.append({'faces': int(m.sum()), 'arm_share': share, 'centroid': _centroid_of(level, m).tolist()})
    report['seam_not_soft'] = dropped
    rigid = cand & ~soft
    comp = g.components(rigid)
    comps = []
    for k in range(comp.max() + 1):
        m = comp == k
        area = level.area[m]
        a_l = float(area[arm_zone[m] & (side[m] > 0)].sum())
        a_r = float(area[arm_zone[m] & (side[m] < 0)].sum())
        tot = float(area.sum())
        torso = tot - a_l - a_r
        best = int(np.argmax([torso, a_l, a_r]))
        lab = (0, 3, 4)[best]
        win = (torso, a_l, a_r)[best]
        minority = 1 - win / tot if tot else 0.
        rec = {'faces': int(m.sum()), 'area': tot, 'assigned': ('torso', 'arm_l', 'arm_r')[best], 'minority_share': minority}
        if minority > cfg.get('mixed_share', .25) and (a_l + a_r) > 0 and torso > 0:
            out[m & arm_zone & (side > 0)] = 3
            out[m & arm_zone & (side < 0)] = 4
            rec['split_by_zone'] = True
        else:
            out[m] = lab
        comps.append(rec)
    report['seam_components'] = sorted(comps, key=lambda c: -c['area'])[:12]
    report['seam_soft'] = {'faces': int(soft.sum()), 'area_share': float(level.area[soft].sum() / level.area.sum()),
                           'mode': spec['soft_mode'], 'rigid_components': len(comps), 'mixed_split': sum(1 for c in comps if c.get('split_by_zone'))}
    if spec['soft_mode'] == 'piece':
        out[soft] = 5
    else:
        sources = cand & ~soft | (label > 0)
        out = flood_nearest(level, out, soft, sources)
    return out


def sections_colour(level, label, side, spec, report):
    """M4: the arm plates carry their own garment role(s) (`colour.roles`, default quinary = cyan): arm = those faces plus
    the wear faces they enclose (the same rim logic as the pauldron), restricted to the arm body parts; sides from the
    body part. Roles missing from the wearable raise an error (the reduction needs the 5-colour palette)."""
    r = spec['rigid']
    cfg = r.get('colour', {})
    roles = cfg.get('roles') or [x for x in level.role_names if x in JOINT_ROLES][:1] or ['quinary']
    missing = [x for x in roles if x not in level.role_names]
    if missing:
        raise SystemExit(f'rigid colour method needs garment role(s) {missing}; this wearable has {level.role_names} '
                         '(reduce with the 5-colour palette)')
    spec2 = dict(spec, region_parts=r['arm_parts'], close_rings=cfg.get('close_rings', 1),
                 max_field_share=cfg.get('max_field_share', .2), min_enclosure=cfg.get('min_enclosure', .6))
    seeds = level.role_mask(roles) & (label == 0)
    g = level.graph
    in_region = np.isin(level.part_base(), r['arm_parts']) if level.part is not None else np.ones(level.n, bool)
    comp = g.components(seeds)
    strays = []
    for k in range(comp.max() + 1):
        m = comp == k
        area = float(level.area[m].sum())
        reg = float(level.area[m & in_region].sum() / area) if area else 0.
        if level.part is not None and reg < spec['min_region_share']:
            seeds[m] = False
            strays.append({'faces': int(m.sum()), 'area': area, 'region_share': reg,
                           'centroid': _centroid_of(level, m).tolist()})
    arm = seeds.copy()
    fields = np.zeros(level.n, bool)
    details = []
    if cfg.get('fill_enclosed', True):
        fields, details = find_fields(level, seeds, spec2, exclude=label > 0)
        arm |= fields
    out = label.copy()
    out[arm & (side > 0)] = 3
    out[arm & (side < 0)] = 4
    report['colour'] = {'roles': roles, 'seed_faces': int(seeds.sum()), 'field_faces': int(fields.sum()),
                        'stray_components': strays, 'fields': details}
    return out


def sections_bands(level, label, side, spec, report):
    """Joint bands: the joint-role (cyan) faces are the bands, the other wear faces split into rigid plates (connected
    components), each wholly assigned to chest / pelvis / thigh_l / thigh_r / arm_l / arm_r by area-weighted body-part zone
    majority (a plate with at least `mixed_min_faces` faces and more than `mixed_share` of its area in another zone spans
    a joint: its band is broken, the plate is split by zone). A band component
    belongs to the joint whose two pieces it touches (arm_l/arm_r: chest|arm, waist: chest|pelvis, hip_l/hip_r: pelvis|thigh).
    A joint is `band` when such a component touches both pieces with at least `min_contact` edges each and no plate spans
    the joint; otherwise it `fell back`: its cut is the zone guess re-cut by snap_cut (M2). Band faces become a soft_<joint>
    piece per joint (`soft_mode` piece) or join the nearest rigid piece (`nearest`)."""
    r = spec['rigid']
    cfg = r.get('bands', {})
    g = level.graph
    n = level.n
    guess = sections_zones(level, label, side, spec, report, 'all')
    cand = label == 0
    band_all = level.role_mask(JOINT_ROLES) & cand
    bc = g.components(band_all)
    band = np.zeros(n, bool)
    dropped = []
    for k in range(bc.max() + 1):
        m = bc == k
        if m.sum() >= cfg.get('min_band_faces', 6):
            band |= m
        else:
            dropped.append({'faces': int(m.sum()), 'centroid': _centroid_of(level, m).tolist()})
    rigid = cand & ~band
    out = guess.copy()
    rc = g.components(rigid)
    mixed = []
    for k in range(rc.max() + 1):
        m = rc == k
        labs = np.unique(guess[m])
        per = {int(l): float(level.area[m & (guess == l)].sum()) for l in labs}
        tot = sum(per.values())
        main = max(per, key=per.get)
        minority_faces = int((m & (guess != main)).sum())
        if 1 - per[main] / tot > cfg.get('mixed_share', .03) and minority_faces >= cfg.get('mixed_min_faces', 8):
            involved = sorted(l for l, a_ in per.items() if a_ / tot > .1)
            mixed.append({'faces': int(m.sum()), 'labels': involved, 'area': tot})
        else:
            out[m] = main
    # which joint does each band component belong to
    pairs = g.pairs
    joint_of = {frozenset(v): k for k, v in JOINTS_ALL.items()}
    comps = []
    owner = {}
    for k in range(bc.max() + 1):
        m = (bc == k) & band
        if not m.any():
            continue
        contact = {}
        for i in np.flatnonzero(m):
            nb, w = g.neighbours(i)
            for j, wt in zip(nb, w):
                if rigid[j]:
                    contact[int(out[j])] = contact.get(int(out[j]), 0) + int(wt)
        best, bj = 0, None
        for key, jn in joint_of.items():
            a_, b_ = tuple(key)
            c_ = min(contact.get(a_, 0), contact.get(b_, 0))
            if c_ >= cfg.get('min_contact', 3) and c_ > best:
                best, bj = c_, jn
        comps.append({'faces': int(m.sum()), 'joint': bj, 'contact_edges': contact, 'area': float(level.area[m].sum())})
        if bj:
            owner.setdefault(bj, []).append(m)
    joints = {}
    for jn, (a_, b_) in JOINTS_ALL.items():
        have_a, have_b = (out == a_).any(), (out == b_).any()
        broken = [mx for mx in mixed if a_ in mx['labels'] and b_ in mx['labels']]
        info = {'faces_a': int((out == a_).sum()), 'faces_b': int((out == b_).sum())}
        if not (have_a and have_b):
            info.update(status='absent', reason='no faces on one side of the joint')
        elif jn not in owner:
            info.update(status='fallback', reason='no band between the two pieces')
        elif broken:
            info.update(status='fallback', reason=f'{len(broken)} rigid plate(s) span the joint (band broken)')
        else:
            info.update(status='band', band_faces=int(sum(m.sum() for m in owner[jn])), band_components=len(owner[jn]))
        joints[jn] = info
    soft_ids = {}
    for jn, info in joints.items():
        if info['status'] == 'band' and spec['soft_mode'] == 'piece':
            for m in owner[jn]:
                out[m] = SOFT_ID[jn]
            soft_ids[jn] = SOFT_ID[jn]
    # every other band face (nearest mode, joints that fell back, stray bands) joins the nearest rigid piece
    todo = band & ~np.isin(out, list(SOFT_ID.values()))
    sources = rigid & ~np.isin(out, (1, 2))
    out = flood_nearest(level, out, todo, sources)
    report['bands'] = {'joints': joints, 'fell_back': [k for k, v in joints.items() if v['status'] == 'fallback'],
                       'absent': [k for k, v in joints.items() if v['status'] == 'absent'],
                       'band_faces': int(band.sum()), 'band_components': comps, 'small_band_dropped': dropped,
                       'mixed_plates': mixed, 'soft_mode': spec['soft_mode']}
    # fallback joints: cut by M2 snap
    report['snap'] = {}
    for jn in report['bands']['fell_back']:
        a_, b_ = JOINTS_ALL[jn]
        out = snap_cut(level, out, spec, a_, b_, report['snap'].setdefault(jn, {}))
    return out


def pauldron_spec(spec, cfg):
    """Spec for the pauldron step of the regions method: unless a style was asked for, the rim style (the expected pauldron
    colour + the wear faces it encloses = field + decoration) instead of the default `roles` style."""
    if spec.get('style_given') or 'rim' not in spec['all_styles']:
        return spec
    rim = spec['all_styles']['rim']
    role = cfg['expected'].get('pauldron')
    allowed = sorted({cfg['expected'][k] for k in ('chest', 'arm', 'pelvis', 'thigh') if k in cfg['expected']})
    return dict(spec, style='rim', roles=[role] if role else list(rim['roles']), close_rings=rim.get('close_rings', 0),
                max_field_share=rim.get('max_field_share', .2), min_enclosure=rim.get('min_enclosure', .6),
                field_exclude_roles=allowed, field_exclude_share=cfg['min_region_share'])


SECTION_KEY = {0: 'chest', 3: 'arm', 4: 'arm', 5: 'pelvis', 6: 'thigh', 7: 'thigh'}   # label -> key of regions.expected


def region_settings(spec):
    """rigid.regions merged with defaults; `expected` = section key -> garment role (CLI --expect overrides)."""
    cfg = dict({'expected': {'chest': 'primary', 'arm': 'secondary', 'pauldron': 'accent', 'pelvis': 'secondary', 'thigh': 'primary'},
                'min_region_share': .025, 'min_minor_share': .004, 'min_enclosure': .6, 'min_span_share': .04, 'span_depth': .05, 'role_resolve_share': .25,
                'min_boundary_share': .6, 'auto_min_regions': 3, 'auto_min_share': .5},
               **(spec['rigid'].get('regions') or {}))
    cfg['expected'] = {**cfg['expected'], **(spec.get('expect') or {})}
    return cfg


def region_components(level, cand, cfg):
    """Large same-colour connected regions of the candidate faces whose role is one of the expected section roles:
    (comp ids over all faces (-1 outside cand), list of region comp ids, roles allowed)."""
    exp = cfg['expected']
    allowed = {exp[k] for k in ('chest', 'arm', 'pelvis', 'thigh') if k in exp}
    ids = [i for i, n in enumerate(level.role_names) if n in allowed]
    comp = level.graph.components(cand, same=np.where(cand, level.face_role, -1))
    cand_area = float(level.area[cand].sum()) or 1.
    regions = [k for k in range(comp.max() + 1)
               if level.face_role[comp == k][0] in ids and level.area[comp == k].sum() >= cfg['min_region_share'] * cand_area]
    return comp, regions, allowed


def sections_regions(level, label, side, spec, report):
    """Per-piece base colours: every rigid section has one large main colour (chest/thigh primary, arm/pelvis secondary by
    default, `piece_split.body_armor.rigid.regions.expected`), neighbours differ, so each joint is a colour boundary.
    1. Large connected same-role regions of the non-pauldron wear faces (>= min_region_share of their area, expected roles
       only) are section candidates. Each is assigned wholly to the section holding most of it by body part (the zone
       guess of M1), or, when that section's expected colour is another one, to a section with >= role_resolve_share of
       the region whose expected colour is the region's. A second section that holds >= min_span_share of the region
       with its 90th-percentile face farther than span_depth x height from the main zone makes it a region spanning
       joints (e.g. chest and thigh green joined because the pelvis colour is missing): it is split by the zone guess and
       each such cut is re-cut by snap_cut. Spill within span_depth of the zone boundary is only zone-guess error, and a
       section that already has a region of its own expected colour never takes a part of a region of another colour
       (blue tassets over the thigh zone stay pelvis, a green plate edge over the pelvis zone stays chest).
    2. All other same-colour components, largest first: enclosed by one assigned section (>= min_enclosure of the perimeter) =
       decoration, absorbed into it; a same-role component of at least min_minor_share that is not enclosed is assigned
       like a region (e.g. a detached tasset); anything else goes face by face to the nearest section by shared-edge
       rings (decoration touching two sections is reported with the shares per section).
    3. Per joint (arm_l/r chest|arm, waist chest|pelvis, hip_l/r pelvis|thigh): `region` when the cut lies on a colour
       boundary (>= min_boundary_share of its length), `fallback` (cut by snap_cut around the zone guess) when it does
       not, is inside a region, there is no contact, or `absent` when a piece has no faces. Pauldrons come first (rim
       style, accent field + decoration) like the other methods."""
    cfg = region_settings(spec)
    exp = cfg['expected']
    g = level.graph
    n = level.n
    cand = label == 0
    guess = sections_zones(level, label, side, spec, report, 'all')
    comp, regions, allowed = region_components(level, cand, cfg)
    out = guess.copy()
    assigned = np.zeros(n, bool)
    cand_area = float(level.area[cand].sum()) or 1.
    labs = [0, 3, 4, 5, 6, 7]
    lname = {0: 'chest', 3: 'arm_l', 4: 'arm_r', 5: 'pelvis', 6: 'thigh_l', 7: 'thigh_r'}
    inside_pairs = set()
    recs = []

    def place(m, how_note, kind):
        """Assign component mask m like a region (zone shares + expected role); returns the record."""
        area = float(level.area[m].sum())
        role = level.role_names[level.face_role[m][0]] if (level.face_role[m] == level.face_role[m][0]).all() else 'mixed'
        shares = {l: float(level.area[m & (guess == l)].sum() / area) for l in labs}
        main = max(shares, key=shares.get)
        how = 'whole'
        if exp.get(SECTION_KEY[main]) != role:                   # the colour says it belongs to another section it overlaps
            alts = [l for l in labs if shares[l] >= cfg['role_resolve_share'] and exp.get(SECTION_KEY[l]) == role]
            if alts:
                main, how = max(alts, key=shares.get), 'whole (role decides)'
        # sections that hold a real part of the region far from the main zone: it spans joints; spill near the zone
        # boundary (the zone guess is only approximate) is not
        spans = []
        mz = guess == main
        for l in labs:
            if l == main or shares[l] < cfg['min_span_share']:
                continue
            if any(r_ != getattr(place, 'current', None) for r_ in getattr(place, 'own', {}).get(l, ())):
                continue                                   # that section has its own region of its colour
            fl = m & (guess == l)
            depth = float(np.percentile(nearest_centroid(level.centroid[fl], level.centroid[mz])[1], 90)) if mz.any() else np.inf
            if depth >= cfg['span_depth'] * level.height:
                spans.append(l)
        cands = [main] + spans
        rec = {'kind': kind, 'role': role, 'faces': int(m.sum()), 'area_share': area / cand_area,
               'zone_shares': {lname[l]: round(v, 3) for l, v in shares.items() if v > .005}, 'centroid': _centroid_of(level, m).tolist()}
        if len(cands) == 1:
            out[m] = main
            rec.update(how=how, assigned=[lname[main]])
        else:
            out[m] = guess[m]
            fixed = m & np.isin(guess, cands)
            odd = m & ~fixed                              # zone of no candidate section: nearest candidate section
            if odd.any() and fixed.any():
                tmp = out.copy()
                tmp[odd] = -1
                tmp = flood_nearest(level, tmp, odd, fixed)
                out[odd] = np.where(tmp[odd] >= 0, tmp[odd], guess[odd])
            for i_, a_ in enumerate(cands):
                for b_ in cands[i_ + 1:]:
                    inside_pairs.add((min(a_, b_), max(a_, b_)))
            rec.update(how='spanning (split by body part, snap re-cut)', assigned=[lname[l] for l in cands])
        asg = [l for l in labs if lname[l] in rec['assigned']]
        rec['role_matches_expected'] = [exp.get(SECTION_KEY[l]) == role for l in asg]
        if not all(rec['role_matches_expected']):
            report['warnings'].append(f'{kind} {role} ({rec["faces"]} faces) assigned to {rec["assigned"]}, expected role there: '
                                      f'{[exp.get(SECTION_KEY[l]) for l in asg]}')
        assigned[m] = True
        recs.append(rec)
        return rec

    def lname_list(cands, keep):
        return [lname[l] for l in cands if l != keep]

    # sections that have a region of their own colour (the main section of some region whose colour they expect): a region of
    # another colour reaching into their zone (tassets over the thighs, a plate edge over the pelvis) does not span to them
    def main_of(m):
        area_ = float(level.area[m].sum())
        sh = {l: float(level.area[m & (guess == l)].sum() / area_) for l in labs}
        mn = max(sh, key=sh.get)
        role_ = level.role_names[level.face_role[m][0]]
        if exp.get(SECTION_KEY[mn]) != role_:
            alts = [l for l in labs if sh[l] >= cfg['role_resolve_share'] and exp.get(SECTION_KEY[l]) == role_]
            if alts:
                mn = max(alts, key=sh.get)
        return mn, role_
    own = {}
    for k in regions:
        mn, role_ = main_of(comp == k)
        if exp.get(SECTION_KEY[mn]) == role_:
            own.setdefault(mn, set()).add(k)
    place.own, place.regions_comp = own, comp
    for k in sorted(regions, key=lambda k: -level.area[comp == k].sum()):
        place.current = k
        place(comp == k, '', 'region')
    place.current = None
    # second pass: everything else, largest first
    rest_comp = g.components(cand & ~assigned, same=np.where(cand, level.face_role, -1))     # same-colour components
    order = sorted(range(rest_comp.max() + 1), key=lambda k: -level.area[rest_comp == k].sum())
    deco, touching_two = [], []
    nearest_pending = np.zeros(n, bool)
    p, w = g.pairs, g.weight
    for k in order:
        m = rest_comp == k
        area = float(level.area[m].sum())
        a_, b_ = m[p[:, 0]], m[p[:, 1]]
        cross = a_ ^ b_
        other = np.where(a_, p[:, 1], p[:, 0])
        total = float(w[cross].sum())
        votes = {l: float(w[cross & assigned[other] & (out[other] == l) & cand[other]].sum()) for l in labs}
        best = max(votes, key=votes.get)
        enc = votes[best] / total if total else 0.
        roles_in = sorted({level.role_names[r] for r in np.unique(level.face_role[m])})
        rec = {'faces': int(m.sum()), 'area_share': area / cand_area, 'roles': roles_in, 'enclosure': enc,
               'centroid': _centroid_of(level, m).tolist()}
        single_role = len(roles_in) == 1
        if enc >= cfg['min_enclosure'] and votes[best] > 0:
            out[m] = best
            assigned[m] = True
            rec.update(how='enclosed', section=lname[best])
        elif single_role and roles_in[0] in allowed and area >= cfg['min_minor_share'] * cand_area:
            r2 = place(m, '', 'minor region')
            rec.update(how='minor region', section=r2['assigned'])
        else:
            nearest_pending |= m
            rec.update(how='nearest', votes={lname[l]: round(v / total, 3) for l, v in votes.items() if v and total})
        deco.append(rec)
    if nearest_pending.any():
        src = assigned & cand
        tmp = out.copy()
        tmp[nearest_pending] = -1
        tmp = flood_nearest(level, tmp, nearest_pending, src)
        reached = nearest_pending & (tmp >= 0)
        out[reached] = tmp[reached]
        c2 = g.components(nearest_pending)
        for k in range(c2.max() + 1):
            m = c2 == k
            got = sorted({lname[int(l)] for l in out[m]})
            if len(got) > 1:
                touching_two.append({'faces': int(m.sum()), 'area_share': float(level.area[m].sum() / cand_area),
                                     'sections': got, 'centroid': _centroid_of(level, m).tolist()})
        assigned |= nearest_pending
    # per-joint status, snap fallback
    e = edge_geometry(level)

    def pauldron_cut(jn, arm_label):
        """(colour-boundary share, edge count) of the cut between the arm piece and its pauldron (labels 1 / 2)."""
        pl = 1 if jn == 'arm_l' else 2
        la2, lb2 = label[p[:, 0]], label[p[:, 1]]
        sel = ((la2 == pl) & (out[p[:, 1]] == arm_label)) | ((lb2 == pl) & (out[p[:, 0]] == arm_label))
        ln_ = e['length'][sel]
        return (float(ln_[e['role_diff'][sel]].sum() / ln_.sum()) if ln_.sum() else 0.), int(sel.sum())

    joints = {}
    for jn, (a_, b_) in JOINTS_ALL.items():
        la_, lb_ = out[p[:, 0]], out[p[:, 1]]
        cutm = cand[p[:, 0]] & cand[p[:, 1]] & (((la_ == a_) & (lb_ == b_)) | ((la_ == b_) & (lb_ == a_)))
        ln = e['length'][cutm]
        share = float(ln[e['role_diff'][cutm]].sum() / ln.sum()) if ln.sum() else 0.
        pc = pauldron_cut(jn, b_) if jn in ('arm_l', 'arm_r') else (0., 0)
        info = {'faces_a': int((out[cand] == a_).sum()), 'faces_b': int((out[cand] == b_).sum()), 'cut_edges': int(cutm.sum()),
                'cut_length_pct_height': float(ln.sum() / level.height * 100), 'colour_boundary_share': share}
        if not info['faces_a'] or not info['faces_b']:
            info.update(status='absent', reason='no faces on one side of the joint')
        elif not cutm.any() and jn in ('arm_l', 'arm_r') and pc[1] > 0 and pc[0] >= cfg['min_boundary_share']:
            info.update(status='region', reason='arm and chest meet only through the pauldron; the arm|pauldron cut is on a colour boundary',
                        via_pauldron=True)
        elif not cutm.any():
            info.update(status='fallback', reason='the two sections do not touch')
        elif (min(a_, b_), max(a_, b_)) in inside_pairs:
            info.update(status='fallback', reason='cut inside a region spanning both sections (same colour)')
        elif share < cfg['min_boundary_share']:
            info.update(status='fallback', reason=f'cut is on a colour boundary for only {share:.0%} of its length (< {cfg["min_boundary_share"]:.0%})')
        else:
            info.update(status='region')
        joints[jn] = info
    report['regions'] = {'expected': exp, 'region_roles': sorted(allowed), 'regions': recs, 'other_components': deco,
                         'decoration_touching_two_sections': touching_two, 'joints': joints,
                         'fell_back': [k for k, v in joints.items() if v['status'] == 'fallback'],
                         'absent': [k for k, v in joints.items() if v['status'] == 'absent'],
                         'settings': {k: v for k, v in cfg.items() if k != 'expected'}}
    report['snap'] = {}
    for jn in report['regions']['fell_back']:
        a_, b_ = JOINTS_ALL[jn]
        out = snap_cut(level, out, spec, a_, b_, report['snap'].setdefault(jn, {}))
    return out


# ---- cross-joint reassignment ("a rigid section stays on its own side of its joint")

CROSS_JOINT_DEFAULTS = {'enabled': True, 'mode': 'shelf', 'margin': {'waist': .025, 'hip': .04, 'armpit': .04, 'shoulder': .05}, 'beyond_share': .85,
                        'patch_deg': 30., 'max_share': .3, 'min_share': .005,
                        'min_faces': 4, 'min_contact': .1, 'complete_to_cut': True, 'joints': ['waist', 'hip_l', 'hip_r', 'armpit_l', 'armpit_r'],
                        'directions': 'both',
                        'shelf': {'joints': ['waist'], 'band': .03, 'axis_deg': 35., 'crease_deg': 45., 'max_share': .10,
                                  'min_contact': .25, 'min_faces': 1, 'min_fold_deg': 30., 'max_listed': 12}}


def cross_joint_settings(spec):
    """rigid.cross_joint merged with the defaults (the nested `shelf` block too); spec['cross_joint'] (True/False,
    --no-cross-joint) overrides `enabled`. `mode`: 'shelf' (default), 'plane' (the old signed-distance rule) or 'both'."""
    user = spec['rigid'].get('cross_joint') or {}
    cfg = dict(CROSS_JOINT_DEFAULTS, **user)
    cfg['shelf'] = dict(CROSS_JOINT_DEFAULTS['shelf'], **(user.get('shelf') or {}))
    if cfg['mode'] not in ('shelf', 'plane', 'both'):
        raise SystemExit(f"cross_joint mode {cfg['mode']!r} unknown (shelf, plane, both)")
    if spec.get('cross_joint') is not None:
        cfg['enabled'] = bool(spec['cross_joint'])
    return cfg


def cross_joint_table(joints):
    """[(name, label from, label to, pivot xyz, unit axis pointing from `from` into `to`)] for every joint between two rigid
    sections whose pivot exists: waist chest|pelvis (axis down), hip_l/r pelvis|thigh (hip -> knee), armpit_l/r chest|arm and
    shoulder_l/r pauldron|arm (shoulder -> elbow). The caller runs each joint in both directions (reversed axis)."""
    out = []

    def unit(v):
        v = np.asarray(v, float)
        return v / max(np.linalg.norm(v), 1e-12)

    down = np.array([0., -1., 0.])
    if joints and 'waist' in joints:
        out.append(('waist', 0, 5, np.asarray(joints['waist'], float), down))
    for side, thigh, arm, paul in (('l', 6, 3, 1), ('r', 7, 4, 2)):
        j = (joints or {}).get(side) or {}
        if 'hip' in j:
            ax = unit(np.asarray(j['knee'], float) - np.asarray(j['hip'], float)) if 'knee' in j else down
            out.append((f'hip_{side}', 5, thigh, np.asarray(j['hip'], float), ax))
        if 'shoulder' in j and 'elbow' in j:
            ax = unit(np.asarray(j['elbow'], float) - np.asarray(j['shoulder'], float))
            out.append((f'armpit_{side}', 0, arm, np.asarray(j['shoulder'], float), ax))
            out.append((f'shoulder_{side}', paul, arm, np.asarray(j['shoulder'], float), ax))
    return out


def edge_components(n, pairs, keep, mask):
    """Connected components over the face pairs `pairs[keep]` among the faces in `mask` (ids -1 outside `mask`); scipy when
    available (high-poly meshes), a plain union-find otherwise."""
    comp = np.full(n, -1, np.int64)
    if not mask.any():
        return comp
    sel = keep & mask[pairs[:, 0]] & mask[pairs[:, 1]] if len(pairs) else np.zeros(0, bool)
    q = pairs[sel]
    try:
        from scipy import sparse
        from scipy.sparse import csgraph
        g = sparse.coo_matrix((np.ones(len(q), np.int8), (q[:, 0], q[:, 1])), shape=(n, n))
        lab = csgraph.connected_components(g, directed=False)[1]
    except ImportError:
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for x, y in q:
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[max(rx, ry)] = min(rx, ry)
        lab = np.array([find(i) for i in range(n)], np.int64)
    _, inv = np.unique(lab[mask], return_inverse=True)
    comp[mask] = inv
    return comp


def planar_patches(level, mask, angle_deg):
    """Planar patches of the faces in `mask` (ids -1 outside): region growing over the shared-edge graph, flattest faces
    first, a face joins a patch while its normal is within `angle_deg` of the patch's SEED normal. Unlike a threshold on the
    dihedral angle of single edges this also separates plates joined by a bevelled (smoothly rounded over several faces)
    fold, which is what the Tripo high-poly meshes have; the bevel faces end up in small patches of their own."""
    g = level.graph
    n = level.n
    e = edge_geometry(level)
    flat = np.zeros(n)
    if len(g.pairs):
        np.maximum.at(flat, g.pairs[:, 0], e['dihedral'])
        np.maximum.at(flat, g.pairs[:, 1], e['dihedral'])
    ids = np.flatnonzero(mask)
    order = ids[np.argsort(flat[ids], kind='stable')].tolist()
    nrm = level.normal.tolist()
    indptr, nbr = g.indptr.tolist(), g.nbr.tolist()
    inmask = mask.tolist()
    cos_t = float(np.cos(np.radians(angle_deg)))
    comp = [-1] * n
    k = 0
    for seed in order:
        if comp[seed] >= 0:
            continue
        sx, sy, sz = nrm[seed]
        comp[seed] = k
        stack = [seed]
        while stack:
            i = stack.pop()
            for j in nbr[indptr[i]:indptr[i + 1]]:
                if comp[j] < 0 and inmask[j]:
                    x, y, z = nrm[j]
                    if sx * x + sy * y + sz * z >= cos_t:
                        comp[j] = k
                        stack.append(j)
        k += 1
    return np.array(comp, np.int64)


def reassign_cross_joint(level, label, spec, names, joints=None):
    """Move fringe faces back to the section they belong to. For every joint between two rigid sections (waist chest|pelvis,
    hips pelvis|thigh, armpits chest|arm, optionally shoulders pauldron|arm; both directions) `rigid.cross_joint.mode` picks:
      * 'shelf' (default, `_shelf_pass`): ledges / caps. A face of section A whose normal points along the joint axis into
        A's side (the up-facing ledge at the bottom of the chest), close to the A|B boundary, grown into a connected group
        that touches B and ends at a sharp fold, belongs to B (the up-facing ledge becomes the top of the pelvis). No
        plane-distance test, so vertical bands (abdomen, sash) never move.
      * 'plane' (the previous rule, below) and 'both' (shelf first, then plane).
    The plane rule: for every joint the face centroids get a signed distance `s` (fraction of height) along the joint axis from the pivot, positive = into the
    other section's side. The section is cut into planar patches (`planar_patches`, normals within patch_deg of the seed);
    a patch moves to the other section when
      * it lies beyond the joint plane: >= beyond_share of its area has s > 0 and its area-weighted mean s >= margin
        (overhang that only reaches just over the plane, e.g. a breastplate edge over the fauld, tassets over the thighs, stays),
      * it is distinct from the main body: it is its own planar patch (a fold or flange separates it; a plate that continues
        across the plane is one patch, partly on this side, so it stays), and not the main (largest) patch,
      * it is a fringe, not a body: >= min_share and min_faces, and all adjacent beyond-plane patches together are at most
        max_share of the section area,
      * it touches the other section (>= min_contact of its boundary length) so the cut stays one closed seam.
    Adjacent qualifying patches move together; the small bevel patches between a moved group and the rest follow the
    neighbour holding more of their shared border. The new cut therefore runs along the fold. Returns (labels, report)."""
    cfg = cross_joint_settings(spec)
    rep = {'enabled': bool(cfg['enabled']), 'settings': {k: v for k, v in cfg.items() if k != 'enabled'}, 'reassigned': [],
           'rejected': [], 'faces_moved': 0, 'area_moved_pct_height2': 0.}
    if not cfg['enabled']:
        return label, rep
    table = [t for t in cross_joint_table(joints) if t[0] in cfg['joints']]
    if not table:
        rep['skipped'] = 'no joint pivots'
        return label, rep
    out = label.copy()
    if not len(level.graph.pairs):
        return out, rep
    if cfg['mode'] in ('shelf', 'both'):
        _shelf_pass(level, out, cfg, table, names, rep)
    if cfg['mode'] in ('plane', 'both'):
        _plane_pass(level, out, cfg, table, names, rep)
    return out, rep


def _plane_pass(level, out, cfg, table, names, rep):
    """The signed-distance rule (mode plane) described in reassign_cross_joint; changes `out` and `rep` in place."""
    e = edge_geometry(level)
    p = level.graph.pairs
    H = level.height
    n = level.n
    cache = {}
    for jn, a, b, piv, axis in table:
        for frm, to, sgn in ((a, b, 1.), (b, a, -1.)):
            if cfg['directions'] == 'forward' and sgn < 0:
                continue
            ma = out == frm
            if not ma.any() or not (out == to).any():
                continue
            s = (level.centroid - piv) @ (sgn * axis) / H
            if frm not in cache or not np.array_equal(cache[frm][0], ma):
                cache[frm] = (ma, planar_patches(level, ma, cfg['patch_deg']))
            comp = cache[frm][1]
            idx = comp >= 0
            k = int(comp.max()) + 1
            if k < 2:
                continue
            area = np.bincount(comp[idx], weights=level.area[idx], minlength=k)
            faces = np.bincount(comp[idx], minlength=k)
            a_total = float(area.sum()) or 1.
            beyond = np.bincount(comp[idx], weights=(level.area * (s > 0))[idx], minlength=k) / np.maximum(area, 1e-20)
            mean_s = np.bincount(comp[idx], weights=(level.area * s)[idx], minlength=k) / np.maximum(area, 1e-20)
            main = int(area.argmax())
            big = (area >= cfg['min_share'] * a_total) & (faces >= cfg['min_faces'])
            margin = cfg['margin'][jn.rsplit('_', 1)[0]] if isinstance(cfg['margin'], dict) else float(cfg['margin'])
            qual = big & (beyond >= cfg['beyond_share']) & (mean_s >= margin)
            qual[main] = False
            seeds = np.flatnonzero(qual)
            if not len(seeds):
                for c in np.flatnonzero(big & (mean_s > 0) & (beyond > .5)):          # near misses, for the report
                    if c != main:
                        why = (f'only {beyond[c]:.0%} of the area is beyond the plane (< {cfg["beyond_share"]:.0%})' if beyond[c] < cfg['beyond_share']
                               else f'mean distance past the plane {mean_s[c] * 100:.1f}% H < {margin * 100:.1f}% H')
                        rep['rejected'].append(_cj_record(level, e, p, out, comp == c, jn, names, frm, to, sgn * axis, s, area[c], faces[c], a_total,
                                                          beyond[c], mean_s[c], 1, 0., why))
                continue
            # qualifying patches joined through the small bevel patches beyond the plane form one fringe group
            tiny = ma & (comp >= 0) & ~big[np.maximum(comp, 0)]
            m_q = np.isin(comp, seeds)
            grp = edge_components(n, p, np.ones(len(p), bool), m_q | (tiny & (s > 0)))
            keep_groups = [gi for gi in np.unique(grp[m_q])]
            for gi in keep_groups:
                m = grp == gi
                g_area = float(level.area[m].sum())
                a_in, b_in = m[p[:, 0]], m[p[:, 1]]
                cross = a_in ^ b_in
                other = np.where(a_in, p[:, 1], p[:, 0])
                tot = float(e['length'][cross].sum())
                contact = float(e['length'][cross & (out[other] == to)].sum()) / tot if tot else 0.
                why = None
                if g_area > cfg['max_share'] * a_total:
                    why = f'area {g_area / a_total:.0%} of the section > {cfg["max_share"]:.0%}'
                elif contact < cfg['min_contact']:
                    why = f'touches {names[to]} along only {contact:.0%} of its boundary (< {cfg["min_contact"]:.0%})'
                n_patch = int(len(np.unique(comp[m])))
                rec = _cj_record(level, e, p, out, m, jn, names, frm, to, sgn * axis, s, g_area, int(m.sum()), a_total,
                                 float(level.area[m & (s > 0)].sum() / g_area), float(level.area[m] @ s[m] / g_area), n_patch, contact, why)
                if why:
                    rep['rejected'].append(rec)
                    continue
                # bevel patches between the group and the rest of the section: the side holding more of their border
                small = tiny & ~m
                move = m.copy()
                if small.any():
                    sc = edge_components(n, p, np.ones(len(p), bool), small)
                    for ci in range(int(sc.max()) + 1):
                        sm = sc == ci
                        a2, b2 = sm[p[:, 0]], sm[p[:, 1]]
                        cr2 = a2 ^ b2
                        oth = np.where(a2, p[:, 1], p[:, 0])
                        w = e['length'][cr2]
                        to_g = float(w[m[oth[cr2]]].sum())
                        stay = float(w[(ma & ~m)[oth[cr2]]].sum())
                        if to_g > stay and level.area[sm].sum() <= 3 * cfg['min_share'] * a_total:      # a bevel strip, not a body of small plates
                            move |= sm
                rec['bevel_faces_followed'] = int(move.sum() - m.sum())
                if cfg['complete_to_cut']:
                    # the cut level is the group's own upper edge: the rest of the section beyond that level that hangs together
                    # with the group (side walls, bevels, other plates of the same fringe) moves too, so the seam is one clean line
                    level_s = float(np.percentile(s[m], 5))
                    cand = (out == frm) & (s >= level_s) | move
                    reach = edge_components(n, p, np.ones(len(p), bool), cand)
                    extra = (out == frm) & ~move & np.isin(reach, np.unique(reach[move]))
                    if extra.any() and level.area[move | extra].sum() <= cfg['max_share'] * a_total:
                        move |= extra
                        rec['completed_faces'] = int(extra.sum())
                        rec['cut_level_past_plane_pct_height'] = level_s * 100
                rec['faces'] = int(move.sum())
                rec['area'] = float(level.area[move].sum())
                rec['share_of_section'] = float(rec['area'] / a_total)
                rec['area_pct_height2'] = float(rec['area'] / H ** 2 * 100)
                out[move] = to
                rep['reassigned'].append(rec)
                rep['faces_moved'] += int(move.sum())
                rep['area_moved_pct_height2'] += rec['area_pct_height2']


def _boundary_samples(level, e, cross, step):
    """Points along the shared edges `cross` (rows of level.graph.pairs) at most `step` apart (both ends included)."""
    vv = e['verts'][cross]
    a, b = level.v[vv[:, 0]], level.v[vv[:, 1]]
    k = np.clip(np.ceil(np.linalg.norm(b - a, axis=1) / max(step, 1e-12)).astype(np.int64), 1, 64)
    rows = np.repeat(np.arange(len(vv)), k + 1)
    t = (np.arange(len(rows)) - np.repeat(np.cumsum(k + 1) - (k + 1), k + 1)) / np.repeat(k, k + 1)
    return a[rows] + (b[rows] - a[rows]) * t[:, None]


def _near_boundary(level, e, cross, faces, dist):
    """Mask of `faces` having a vertex within `dist` of the polyline made of the shared edges `cross`."""
    out = np.zeros(level.n, bool)
    ids = np.flatnonzero(faces)
    if not len(ids):
        return out
    pts = _boundary_samples(level, e, cross, dist / 4.)
    vid = np.unique(level.f[ids])
    d = np.full(len(level.v), np.inf)
    d[vid] = nearest_centroid(level.v[vid], pts)[1]
    out[ids] = d[level.f[ids]].min(1) <= dist
    return out


def _shelf_pass(level, out, cfg, table, names, rep):
    """The shelf / cap rule (mode shelf), see reassign_cross_joint; changes `out` and `rep` in place.
    For a joint axis `d` pointing from section `frm` into `to`: the faces of `frm` whose outward normal points along -d
    (within shelf.axis_deg) and lie within shelf.band (fraction of height) of the frm|to boundary are grown into connected
    groups, not across edges sharper than shelf.crease_deg. A group that touches `to` is a shelf and moves to `to` when it
    is small (shelf.max_share of `frm`, shelf.min_faces), borders `to` along shelf.min_contact of its outline and is
    separated from the rest of `frm` by a fold (mean dihedral of its neck >= shelf.min_fold_deg)."""
    sc = cfg['shelf']
    e = edge_geometry(level)
    p = level.graph.pairs
    H, n = level.height, level.n
    nrm = level.normal * outward_sign(level)
    cos_axis = float(np.cos(np.radians(sc['axis_deg'])))
    calm = e['dihedral'] <= sc['crease_deg']
    length, dih = e['length'], e['dihedral']
    for jn, a, b, piv, axis in table:
        if jn not in sc['joints']:
            continue
        for frm, to, sgn in ((a, b, 1.), (b, a, -1.)):
            if cfg['directions'] == 'forward' and sgn < 0:
                continue
            mf, mt = out == frm, out == to
            if not mf.any() or not mt.any():
                continue
            d = sgn * axis
            cross = (mf[p[:, 0]] & mt[p[:, 1]]) | (mt[p[:, 0]] & mf[p[:, 1]])
            if not cross.any():
                continue
            a_total = float(level.area[mf].sum()) or 1.
            cand = mf & (nrm @ -d >= cos_axis)
            cb = cand & _near_boundary(level, e, cross, cand, sc['band'] * H)
            if not cb.any():
                continue
            comp = edge_components(n, p, calm, cb)
            k = int(comp.max()) + 1
            ci = comp >= 0
            area = np.bincount(comp[ci], weights=level.area[ci], minlength=k)
            faces = np.bincount(comp[ci], minlength=k)
            cosn = np.bincount(comp[ci], weights=(level.area * (nrm @ -d))[ci], minlength=k) / np.maximum(area, 1e-20)
            tot, touch, neck, neck_dih = (np.zeros(k) for _ in range(4))
            for cs, oi in ((comp[p[:, 0]], p[:, 1]), (comp[p[:, 1]], p[:, 0])):
                sel = (cs >= 0) & (comp[oi] != cs)
                tot += np.bincount(cs[sel], weights=length[sel], minlength=k)
                ts = sel & mt[oi]
                touch += np.bincount(cs[ts], weights=length[ts], minlength=k)
                ns = sel & mf[oi]
                neck += np.bincount(cs[ns], weights=length[ns], minlength=k)
                neck_dih += np.bincount(cs[ns], weights=(length * dih)[ns], minlength=k)
            contact = touch / np.maximum(tot, 1e-20)
            fold = neck_dih / np.maximum(neck, 1e-20)
            move = np.zeros(k, bool)
            listed = 0
            for c in np.argsort(-area, kind='stable'):
                if faces[c] < sc['min_faces'] and not touch[c]:
                    continue
                why = None
                if not touch[c]:
                    why = f'does not touch {names[to]}'
                elif faces[c] < sc['min_faces']:
                    why = f'{faces[c]} face(s) < {sc["min_faces"]}'
                elif area[c] > sc['max_share'] * a_total:
                    why = f'area {area[c] / a_total:.0%} of the section > {sc["max_share"]:.0%}'
                elif contact[c] < sc['min_contact']:
                    why = f'touches {names[to]} along only {contact[c]:.0%} of its outline (< {sc["min_contact"]:.0%})'
                elif neck[c] > 0 and fold[c] < sc['min_fold_deg']:
                    why = f'neck fold {fold[c]:.0f} deg < {sc["min_fold_deg"]:.0f} (no sharp edge to the rest of {names[frm]})'
                if why and listed >= sc['max_listed']:
                    continue
                m = comp == c
                rec = {'rule': 'shelf', 'joint': jn, 'from': names[frm], 'to': names[to], 'faces': int(faces[c]), 'area': float(area[c]),
                       'area_pct_height2': float(area[c] / H ** 2 * 100), 'share_of_section': float(area[c] / a_total),
                       'normal_to_axis_deg': float(np.degrees(np.arccos(np.clip(cosn[c], -1, 1)))),
                       'contact_share_with_target': float(contact[c]), 'neck_length_pct_height': float(neck[c] / H * 100),
                       'neck_mean_dihedral_deg': float(fold[c]) if neck[c] > 0 else None,
                       'axis_offset_from_pivot_pct_height': float((level.centroid[m] - piv).mean(0) @ d / H * 100),
                       'centroid': _centroid_of(level, m).tolist()}
                if why:
                    rec['rejected_because'] = why
                    rep['rejected'].append(rec)
                    listed += 1
                    continue
                move[c] = True
                rep['reassigned'].append(rec)
                rep['faces_moved'] += int(faces[c])
                rep['area_moved_pct_height2'] += rec['area_pct_height2']
            if move.any():
                out[ci & move[np.maximum(comp, 0)]] = to


def _cj_record(level, e, p, out, m, jn, names, frm, to, axis, s, area, faces, a_total, beyond, mean_s, n_patch, contact, why):
    H = level.height
    a_in, b_in = m[p[:, 0]], m[p[:, 1]]
    other = np.where(a_in, p[:, 1], p[:, 0])
    neck = (a_in ^ b_in) & (out[other] == frm)
    nl = e['length'][neck]
    rec = {'rule': 'plane', 'joint': jn, 'from': names[frm], 'to': names[to], 'faces': int(faces), 'area': float(area),
           'area_pct_height2': float(area / H ** 2 * 100), 'share_of_section': float(area / a_total), 'planar_patches': int(n_patch),
           'mean_distance_past_plane_pct_height': float(mean_s * 100), 'max_distance_past_plane_pct_height': float(s[m].max() * 100),
           'beyond_share': float(beyond), 'neck_length_pct_height': float(nl.sum() / H * 100),
           'neck_mean_dihedral_deg': float((e['dihedral'][neck] * nl).sum() / nl.sum()) if nl.sum() else None,
           'normal_to_axis_deg': float(np.degrees(np.arccos(np.clip(
               np.abs((level.normal[m] * axis).sum(1)) @ level.area[m] / max(level.area[m].sum(), 1e-20), 0, 1)))),
           'contact_share_with_target': float(contact), 'centroid': _centroid_of(level, m).tolist()}
    if why:
        rec['rejected_because'] = why
    return rec


FLAP_DEFAULTS = {'enabled': True, 'min_height': .06, 'max_passes': 4}


def prune_flaps(level, label, spec, names):
    """Spike / flap faces: a triangle hanging off the rest of the wearable by ONE edge (or by none) whose free tip stands
    more than `min_height` (fraction of body height) off that edge is a flap of the reduction (a cuff triangle stretched
    over the bare forearm, a skirt tip), not armour that can be skinned to a rigid piece. Removing a leaf face opens no hole,
    so such faces are dropped from the exported pieces (chains are peeled leaf by leaf, up to `max_passes`). Returns
    (dropped mask, records); the labels are not changed (a flap has no second piece to go to). rigid.flaps / spec['flaps']."""
    cfg = dict(FLAP_DEFAULTS, **(spec['rigid'].get('flaps') or {}))
    if spec.get('flaps') is not None:
        cfg['enabled'] = bool(spec['flaps'])
    dropped = np.zeros(level.n, bool)
    recs = []
    p = level.graph.pairs
    if not cfg['enabled'] or not len(p):
        return dropped, recs
    e = edge_geometry(level)
    H = level.height
    for _ in range(int(cfg['max_passes'])):
        alive = ~dropped
        sel = alive[p[:, 0]] & alive[p[:, 1]]
        deg = np.bincount(p[sel, 0], minlength=level.n) + np.bincount(p[sel, 1], minlength=level.n)
        leaves = np.flatnonzero(alive & (deg <= 1))
        if not len(leaves):
            break
        row_of = np.full(level.n, -1, np.int64)
        for col in (0, 1):
            r = np.flatnonzero(sel)
            row_of[p[r, col]] = r
        found = []
        for i in leaves:
            tri = level.v[level.f[i]]
            if deg[i] == 0:
                height = max(np.linalg.norm(tri[a] - tri[b]) for a, b in ((0, 1), (1, 2), (2, 0))) / H
            else:
                edge = e['verts'][row_of[i]]
                free = [v for v in level.f[i] if v not in edge]
                if len(free) != 1:
                    continue
                a, b = level.v[edge[0]], level.v[edge[1]]
                ab = b - a
                foot = a + ab * (((level.v[free[0]] - a) @ ab) / max(ab @ ab, 1e-20))
                height = np.linalg.norm(level.v[free[0]] - foot) / H
            if height > cfg['min_height']:
                found.append((i, height))
        if not found:
            break
        for i, height in found:
            dropped[i] = True
            recs.append({'face': int(i), 'piece': names.get(int(label[i]), '?'), 'tip_height_pct_height': float(height * 100),
                         'area_pct_height2': float(level.area[i] / H ** 2 * 100), 'attached_edges': int(deg[i]),
                         'body_part': level.part_names[level.part[i]] if level.part is not None and level.part[i] >= 0 else None,
                         'centroid': level.centroid[i].tolist()})
    return dropped, recs


def build_sections(level, spec, method='snap', overlap=False):
    """Pauldrons by roles/rim (classify) then the other pieces by `method`: 'zones' = M1, 'snap' = M2, 'seam' = M3,
    'colour' = M4 (arms), 'bands' = cyan joint bands (chest, pelvis, thighs, arms; per-joint snap fallback), 'auto' =
    bands when the wearable has joint-role faces, else snap on all joints. spec['joints'] 'all' adds pelvis / thighs to
    zones / snap."""
    if method == 'given':
        # piece labels computed elsewhere (segment_first.py: sections cut on the high-poly mesh, reduced one by one)
        label, side, flags, report = classify(level, dict(spec, roles=[], style='roles'))
        names = piece_names(spec, True, False, 'all')
        final = np.asarray(spec['given_label'], np.int64).copy()
        report['rigid_method'] = 'given'
        report['cuts_zone_guess'] = []
        report['cuts'] = cut_report(level, final, names, spec)
        report['arm_faces_zone_guess'] = {names[l]: int((final == l).sum()) for l in (3, 4)}
        sec = Sections(level, spec, final, side, flags, names, report)
        sec.guess = final
        sec.joints = resolve_joints(level, spec)
        sec.method = 'given'
        if overlap:
            build_overlap(sec)
        return sec
    label, side, flags, report = classify(level, spec)
    if method == 'auto':
        # explicit cyan designs keep their methods: joint role faces -> bands; the old 'quinary' cyan role (M4 data: cyan
        # rerebrace plates) -> colour; else per-piece base colours (>= auto_min_regions large same-colour regions of the
        # expected section roles covering >= auto_min_share of the wearable area) -> regions; else snap on all joints
        n_joint = int((level.role_mask(('joint',)) & (label == 0)).sum())
        n_quin = int((level.role_mask(('quinary',)) & (label == 0)).sum())
        minf = spec['rigid'].get('bands', {}).get('auto_min_faces', 10)
        rcfg = region_settings(spec)
        pl_spec = pauldron_spec(spec, rcfg)
        label_r = classify(level, pl_spec)[0]
        cand_r = label_r == 0
        comp_r, regs, _ = region_components(level, cand_r, rcfg)
        reg_share = float(sum(level.area[comp_r == k].sum() for k in regs) / max(level.area[cand_r].sum(), 1e-12))
        report['auto'] = {'joint_role_faces': n_joint, 'quinary_role_faces': n_quin, 'min_faces': minf,
                          'large_regions': len(regs), 'region_area_share': reg_share, 'region_roles_present': sorted(
                              {level.role_names[level.face_role[comp_r == k][0]] for k in regs}),
                          'auto_min_regions': rcfg['auto_min_regions'], 'auto_min_share': rcfg['auto_min_share']}
        if n_joint >= minf:
            method = 'bands'
        elif n_quin >= minf:
            method = 'colour'
        elif len(regs) >= rcfg['auto_min_regions'] and reg_share >= rcfg['auto_min_share']:
            method = 'regions'
        else:
            method = 'snap'
        report['auto']['chosen'] = method
        spec = dict(spec, joints='arms' if method == 'colour' else 'all')
    elif method == 'bands':
        spec = dict(spec, joints='all')
    if method == 'regions':
        spec = dict(spec, joints='all')
        pl_spec = pauldron_spec(spec, region_settings(spec))
        if pl_spec is not spec or pl_spec['style'] != spec['style']:
            auto_report = report.get('auto')
            label, side, flags, report = classify(level, pl_spec)
            if auto_report:
                report['auto'] = auto_report
            report['pauldron_spec'] = {'style': pl_spec['style'], 'roles': pl_spec['roles']}
            spec = pl_spec
    joints = spec.get('joints', 'arms')
    soft = (method == 'seam' and spec['soft_mode'] == 'piece') or (method == 'bands' and spec['soft_mode'] == 'piece')
    names = piece_names(spec, True, soft, 'all' if method in ('bands', 'regions') else joints)
    report['rigid_method'] = method
    if method == 'seam':
        guess = sections_seam(level, label, side, spec, report)
        final = guess.copy()
    elif method == 'colour':
        guess = sections_colour(level, label, side, spec, report)
        final = guess.copy()
    elif method == 'bands':
        final = sections_bands(level, label, side, spec, report)
        guess = sections_zones(level, label, side, spec, {}, 'all')
    elif method == 'regions':
        final = sections_regions(level, label, side, spec, report)
        guess = sections_zones(level, label, side, spec, {}, 'all')
    else:
        guess = sections_zones(level, label, side, spec, report, joints)
        final = sections_snap(level, guess, spec, report, joints) if method == 'snap' else guess.copy()
    absorbed = report['absorbed_islands']
    mi = int(spec['min_island_faces'])
    nm = {i: n for i, n in names.items()}
    if is_all(names) and cross_joint_settings(spec)['enabled']:
        final, report['cross_joint'] = reassign_cross_joint(level, final, spec, nm, resolve_joints(level, spec))
    absorb_islands(level, final, mi, f'island < {mi} faces (rigid sections)', absorbed, nm, True)
    limbs = (3, 4, 6, 7) if is_all(names) else (3, 4)
    minor_components(level, final, limbs, spec['extra_component_share'], absorbed, nm, None, (1, 2), True)
    minor_components(level, final, (0, 5) if is_all(names) else (0,), np.inf, absorbed, nm, None, (1, 2), True)   # one torso/chest/pelvis
    absorb_islands(level, final, mi, f'island < {mi} faces (rigid sections, after detached patches)', absorbed, nm, True)
    for lab in [l for l in list(names) if l >= 8 or (l == 5 and names[l] == 'soft')]:      # soft labels without faces
        if not (final == lab).any():
            del names[lab]
    report['cuts_zone_guess'] = cut_report(level, guess, {**names, **{l: f'?{l}' for l in range(20) if l not in names}}, spec)
    report['cuts'] = cut_report(level, final, names, spec)
    report['arm_faces_zone_guess'] = {names[l]: int((guess == l).sum()) for l in (3, 4)}
    sec = Sections(level, spec, final, side, flags, names, report)
    sec.dropped, report['flaps'] = prune_flaps(level, final, spec, names)
    for rec in report['flaps']:
        report['warnings'].append(f'{rec["piece"]}: dropped a flap face ({rec["tip_height_pct_height"]:.1f}% of the height off its '
                                  f'{"edge" if rec["attached_edges"] else "position"}, {rec["body_part"]})')
    sec.guess = guess
    sec.joints = resolve_joints(level, spec)
    sec.method = method
    if overlap:
        build_overlap(sec)
    return sec


def resolve_joints(level, spec):
    """Joint pivots in the GLB frame: {'l': {'shoulder', 'hip', ...}, 'r': ..., 'waist': xyz, 'source'}. spec joint_l /
    joint_r (shoulder), joint_hip_l / joint_hip_r, joint_waist (x,y,z) override the base parts file; None when there is
    neither a parts file nor shoulder overrides."""
    j = {k: (dict(v) if isinstance(v, dict) else v) for k, v in (level.joints or {}).items() if k in ('l', 'r', 'waist')}
    src = (level.joints or {}).get('source', 'none')
    for side in ('l', 'r'):
        if spec.get(f'joint_{side}') is not None:
            j.setdefault(side, {})['shoulder'] = np.asarray(spec[f'joint_{side}'], float)
            src = 'cli/spec'
        if spec.get(f'joint_hip_{side}') is not None:
            j.setdefault(side, {})['hip'] = np.asarray(spec[f'joint_hip_{side}'], float)
            src = 'cli/spec'
    if spec.get('joint_waist') is not None:
        j['waist'] = np.asarray(spec['joint_waist'], float)
        src = 'cli/spec'
    j['source'] = src
    return j if 'l' in j and 'r' in j else None


class Sections:
    """The final piece labels of a level plus per-piece overlap strips (`ext`, filled by build_overlap)."""

    def __init__(self, level, spec, label, side, flags, names, report):
        self.level, self.spec, self.label, self.side, self.flags, self.names, self.report = level, spec, label, side, flags, names, report
        self.ext = {}
        self.strip_cache = {}
        self.guess = None
        self.overlap_report = None
        self.dropped = np.zeros(level.n, bool)       # flap faces (prune_flaps): in no exported piece

    def tris(self, lab, strips=True, ext_all=None):
        """(n,3,3) triangles of piece `lab`, then its overlap strips (`ext_all` or the attached ones); with a (n,)
        bool 'is strip'."""
        lv = self.level
        t = lv.raw_v[lv.raw_f[self.label == lab]]
        flag = np.zeros(len(t), bool)
        ext = (self.ext if ext_all is None else ext_all).get(lab)
        if strips and ext is not None and len(ext['f']):
            allv = np.concatenate([lv.raw_v, ext['v']])
            t = np.concatenate([t, allv[ext['f']]])
            flag = np.r_[flag, np.ones(len(ext['f']), bool)]
        return t, flag


# ---- overlap strips

def cut_pairs(sec, mode, convention):
    """[(owner, under)] per cut between two rigid pieces of different motion bodies (piece_body). mode 'parent': the piece
    that is higher in the hierarchy (piece_depth: pelvis < chest/thigh < arm) owns the strip and tucks it under the
    lower one (the strip stays where the mover was and fills the opening); 'child': the mover owns it and tucks it under
    the static one; 'both': both pieces own one (a gap sample counts as closed when either bridges it); 'cover': the inner
    piece by cover_order owns it (independent of the motion). Cuts to a soft piece never get strips."""
    lv, label, names = sec.level, sec.label, sec.names
    p = lv.graph.pairs
    la, lb = label[p[:, 0]], label[p[:, 1]]
    am = is_all(names)
    body = {l: piece_body(n, convention, am) for l, n in names.items()}
    depth = {l: piece_depth(n, convention, am) for l, n in names.items()}
    rank = cover_rank(sec.spec, names)
    out = []
    for a, b in sorted({(min(x, y), max(x, y)) for x, y in zip(la[la != lb], lb[la != lb])}):
        if body[a] is None or body[b] is None or body[a] == body[b]:
            continue                                     # soft piece, or one rigid body in this convention
        if mode == 'both':
            out += [(a, b), (b, a)]
            continue
        if mode == 'parent':
            owner, under = (a, b) if depth[a] <= depth[b] else (b, a)
        elif mode == 'child':
            owner, under = (a, b) if depth[a] > depth[b] else (b, a)
        else:
            if rank[a] == rank[b]:
                continue
            owner, under = (a, b) if rank[a] > rank[b] else (b, a)
        out.append((owner, under))
    return out


def compute_strips(sec, mode, convention):
    """Overlap strips for (mode, convention): ({owner label: {'v','f','mat','cut'}}, per-cut report list). The owner's
    cut loop is extruded `length` along the other piece's surface (tangent step, projected back onto that surface) and
    sunk `offset` below it along its outward normals; one quad (2 triangles) per cut edge attached to the owner's own
    cut vertices, material of the adjacent owner face."""
    lv, label, names = sec.level, sec.label, sec.names
    ov = sec.spec['rigid']['overlap']
    H = lv.height
    d, off = ov['length'] * H, ov['offset'] * H
    e = edge_geometry(lv)
    p = lv.graph.pairs
    la, lb = label[p[:, 0]], label[p[:, 1]]
    sgn = outward_sign(lv)
    base_n = len(lv.raw_v)
    ext_all, reports = {}, []
    for inner, outer in cut_pairs(sec, mode, convention):
        sel = np.flatnonzero(((la == inner) & (lb == outer)) | ((la == outer) & (lb == inner)))
        fi = np.where(la[sel] == inner, p[sel, 0], p[sel, 1])
        fo = np.where(la[sel] == inner, p[sel, 1], p[sel, 0])
        va, vb = e['verts'][sel, 0], e['verts'][sel, 1]
        pa, pb = lv.v[va], lv.v[vb]
        n_out = lv.normal[fo] * sgn
        tang = np.cross(n_out, pb - pa)
        tang /= np.maximum(np.linalg.norm(tang, axis=1, keepdims=True), 1e-20)
        away = ((lv.centroid[fo] - (pa + pb) / 2) * tang).sum(1) < 0
        tang[away] *= -1
        verts = np.unique(np.r_[va, vb])
        vdir = {v: np.zeros(3) for v in verts}
        for k in range(len(sel)):
            vdir[va[k]] += tang[k]
            vdir[vb[k]] += tang[k]
        q = np.array([lv.v[v] + vdir[v] / max(np.linalg.norm(vdir[v]), 1e-20) * d for v in verts])
        outer_tri = lv.raw_v[lv.raw_f[label == outer]]
        outer_n = np.cross(outer_tri[:, 1] - outer_tri[:, 0], outer_tri[:, 2] - outer_tri[:, 0])
        outer_n = sgn * outer_n / np.maximum(np.linalg.norm(outer_n, axis=1, keepdims=True), 1e-20)
        proj, ti, _ = closest_points(q, outer_tri)
        inner_pts = proj - outer_n[ti] * off
        ext = ext_all.setdefault(inner, {'v': np.zeros((0, 3)), 'f': np.zeros((0, 3), np.int64),
                                         'mat': np.zeros(0, np.int64), 'cut': np.zeros(0, np.int64)})
        start = base_n + len(ext['v'])
        newv = np.concatenate([lv.v[verts], inner_pts])          # cut vertices (copies on the owner), then sunk ones
        bidx = {v: start + i for i, v in enumerate(verts)}
        sidx = {v: start + len(verts) + i for i, v in enumerate(verts)}
        tris, mats = [], []
        for k in range(len(sel)):
            A, B = bidx[va[k]], bidx[vb[k]]
            A2, B2 = sidx[va[k]], sidx[vb[k]]
            pos = lambda i: newv[i - start]
            if np.linalg.norm(pos(A2) - pos(A)) < .2 * off and np.linalg.norm(pos(B2) - pos(B)) < .2 * off:
                continue                                         # nothing to extrude (the other surface ends here)
            t1, t2 = (A, B, B2), (A, B2, A2)
            nrm = np.cross(pos(B) - pos(A), pos(B2) - pos(A))
            if (nrm * n_out[k]).sum() < 0:
                t1, t2 = (A, B2, B), (A, A2, B2)
            tris += [t1, t2]
            mats += [lv.face_material[fi[k]]] * 2
        if not tris:
            continue
        cut_id = len(reports)
        ext['v'] = np.concatenate([ext['v'], newv])
        ext['f'] = np.concatenate([ext['f'], np.array(tris, np.int64)])
        ext['mat'] = np.concatenate([ext['mat'], np.array(mats, np.int64)])
        ext['cut'] = np.concatenate([ext['cut'], np.full(len(tris), cut_id, np.int64)])
        allv = np.concatenate([lv.raw_v, ext['v']])
        st = allv[np.array(tris)]
        area = float(np.linalg.norm(np.cross(st[:, 1] - st[:, 0], st[:, 2] - st[:, 0]), axis=1).sum() / 2)
        depth = np.linalg.norm(inner_pts - lv.v[verts], axis=1)
        reports.append({'owner': names[inner], 'under': names[outer], 'cut_edges': int(len(sel)), 'strip_faces': len(tris),
                        'strip_area': area, 'cut_length': float(e['length'][sel].sum()),
                        'mean_extrusion_pct_height': float(depth.mean() / H * 100),
                        'max_extrusion_pct_height': float(depth.max() / H * 100),
                        'offset_pct_height': ov['offset'] * 100, 'length_pct_height': ov['length'] * 100})
    return ext_all, reports


def get_strips(sec, mode, convention):
    key = (mode, convention)
    if key not in sec.strip_cache:
        sec.strip_cache[key] = compute_strips(sec, mode, convention)
    return sec.strip_cache[key]


def build_overlap(sec):
    """Attach the strips of the configured mode/convention (rigid.overlap.mode, rigid.pose.convention) to the sections:
    they are exported and drawn as part of their owner piece, flagged 'overlap'."""
    ov, pose = sec.spec['rigid']['overlap'], sec.spec['rigid']['pose']
    sec.overlap_mode, sec.overlap_convention = ov.get('mode', 'parent'), pose.get('convention', 'arm')
    sec.ext, sec.overlap_report = get_strips(sec, sec.overlap_mode, sec.overlap_convention)


# ---- rigid pose test

def _rot(axis, ang):
    c, s_ = np.cos(ang), np.sin(ang)
    return {'x': np.array([[1, 0, 0], [0, c, -s_], [0, s_, c]]), 'z': np.array([[c, -s_, 0], [s_, c, 0], [0, 0, 1.]])}[axis]


SCENARIOS = ('arms', 'thigh_flex', 'thigh_abd', 'spine')


def pose_transforms(sec, angle, convention, scenario='arms'):
    """label -> (R, pivot) of the moving pieces (GLB frame: X lateral, Y up, +Z front). Scenarios:
    'arms': arms (and pauldrons by convention: arm 1, torso 0, half 0.5 of the angle) turn about the shoulder joint around Z,
    angle > 0 lowers them; 'thigh_flex': thighs swing forward about the hip joint around X; 'thigh_abd': thighs swing out
    around Z; 'spine': chest, arms and pauldrons (the whole upper body) pitch forward about the waist joint around X."""
    names = sec.names
    T = {}
    J = sec.joints
    for lab, n in names.items():
        base, side = n.split('_')[0], n.split('_')[-1]
        sgn = 1 if side == 'l' else -1
        if scenario == 'arms' and base in ('arm', 'pauldron'):
            frac = 1. if base == 'arm' else {'arm': 1., 'torso': 0., 'half': .5}[convention]
            if frac > 0:
                T[lab] = (_rot('z', np.radians(-sgn * angle * frac)), np.asarray(J[side]['shoulder'], float))
        elif scenario == 'thigh_flex' and base == 'thigh':
            T[lab] = (_rot('x', np.radians(-angle)), np.asarray(J[side]['hip'], float))
        elif scenario == 'thigh_abd' and base == 'thigh':
            T[lab] = (_rot('z', np.radians(sgn * angle)), np.asarray(J[side]['hip'], float))
        elif scenario == 'spine' and base in ('chest', 'arm', 'pauldron'):
            T[lab] = (_rot('x', np.radians(angle)), np.asarray(J['waist'], float))
    return T


def scenario_ready(sec, scenario):
    """Whether the sections/joints support the scenario (pelvis + hip/waist joints for the leg and spine ones)."""
    J = sec.joints
    if not J:
        return False
    if scenario == 'arms':
        return 'shoulder' in J.get('l', {}) and 'shoulder' in J.get('r', {})
    if scenario in ('thigh_flex', 'thigh_abd'):
        return is_all(sec.names) and 'hip' in J.get('l', {}) and 'hip' in J.get('r', {})
    return is_all(sec.names) and 'waist' in J


def pose_soft(sec, tris, T):
    """Linear-blend skinning of a soft piece: every vertex blends the transforms of the rigid pieces (identity for the
    static ones) with weights 1 / (distance to that piece + eps)^2 (rest pose); returns the posed tris and the largest
    edge stretch (posed / rest edge length)."""
    lv, label = sec.level, sec.label
    pts = tris.reshape(-1, 3)
    uniq, first, inv = np.unique(np.round(pts / (lv.height * WELD)).astype(np.int64), axis=0, return_index=True, return_inverse=True)
    inv = inv.ravel()
    pos = pts[first]
    eps = 1e-3 * lv.height
    num, den = np.zeros_like(pos), np.zeros(len(pos))
    for lab, n in sec.names.items():
        if n.startswith('soft'):
            continue
        rp = lv.raw_v[lv.raw_f[label == lab]].reshape(-1, 3)
        if not len(rp):
            continue
        w = 1. / (nearest_centroid(pos, rp)[1] + eps) ** 2
        num += w[:, None] * apply_T(pos, T.get(lab))
        den += w
    new = num / den[:, None]
    posed = new[inv].reshape(tris.shape)
    e0 = np.linalg.norm(tris[:, [0, 1, 2]] - tris[:, [1, 2, 0]], axis=2).ravel()
    e1 = np.linalg.norm(posed[:, [0, 1, 2]] - posed[:, [1, 2, 0]], axis=2).ravel()
    return posed, float((e1 / np.maximum(e0, 1e-9)).max()) if len(e0) else 1.


def apply_T(tris, T):
    if T is None:
        return tris
    R, piv = T
    return (tris - piv) @ R.T + piv


def pose_metrics(sec, angle_down, convention, overlap, mode=None, scenario='arms'):
    """Posed tris per label + opening metrics per cut with relative motion. The strip owner's posed surface (including
    its strips when `overlap`) is measured from the boundary points (cut vertices and edge midpoints, in their own
    pose) of the piece the strips tuck under: 0 = closed. Without overlap the same pairs are measured against the bare
    surface. `separation` is the plain motion of the cut (owner vs other boundary vertex), independent of overlap."""
    lv, label, names = sec.level, sec.label, sec.names
    mode = mode or sec.spec['rigid']['overlap'].get('mode', 'parent')
    T = pose_transforms(sec, angle_down, convention, scenario)
    H = lv.height
    ext_all = get_strips(sec, mode, convention)[0] if overlap else {}
    posed, flags, soft_stretch = {}, {}, {}
    for lab in names:
        t, fl = sec.tris(lab, overlap, ext_all)
        if names[lab].startswith('soft'):
            posed[lab], soft_stretch[names[lab]] = pose_soft(sec, t, T) if len(t) else (t, 1.)
            flags[lab] = fl
        else:
            posed[lab], flags[lab] = apply_T(t, T.get(lab)), fl
    e = edge_geometry(lv)
    p = lv.graph.pairs
    la, lb = label[p[:, 0]], label[p[:, 1]]
    cuts = []
    pairs = cut_pairs(sec, mode, convention)
    done = set()
    for owner, under in pairs:
        key = frozenset((owner, under))
        if key in done:
            continue
        done.add(key)
        ta, tb = T.get(owner), T.get(under)
        if (ta is None and tb is None) or (ta is not None and tb is not None and np.allclose(ta[0], tb[0]) and np.allclose(ta[1], tb[1])):
            continue                                      # no relative motion of this pair in this scenario
        sel = ((la == owner) & (lb == under)) | ((la == under) & (lb == owner))
        v0, v1 = lv.v[e['verts'][sel, 0]], lv.v[e['verts'][sel, 1]]
        pts = np.concatenate([v0, v1, (v0 + v1) / 2])
        under_pts = apply_T(pts, T.get(under))
        owner_pts = apply_T(pts, T.get(owner))
        sep = np.linalg.norm(under_pts - owner_pts, axis=1)
        gap = closest_points(under_pts, posed[owner])[2]
        if (under, owner) in pairs:                       # both pieces own a strip: a sample is closed if either bridges it
            gap = np.minimum(gap, closest_points(owner_pts, posed[under])[2])
        cuts.append({'owner': names[owner], 'under': names[under], 'samples': int(len(pts)),
                     'separation_max_pct_height': float(sep.max() / H * 100), 'separation_mean_pct_height': float(sep.mean() / H * 100),
                     'gap_max_pct_height': float(gap.max() / H * 100), 'gap_mean_pct_height': float(gap.mean() / H * 100)})
    for sname, st in soft_stretch.items():
        cuts.append({'owner': sname, 'under': 'skinned', 'samples': 0, 'separation_max_pct_height': 0., 'separation_mean_pct_height': 0.,
                     'gap_max_pct_height': 0., 'gap_mean_pct_height': 0., 'soft_stretch_max': st})
    return posed, flags, cuts


def pose_test(sec):
    """All pose variants. Arms: angles (down, up) x pauldron convention (arm, torso, half) x strip mode (parent, child,
    both, cover) x overlap (no, yes); thigh flexion / abduction and the spine bend (when the sections have a pelvis and the
    joints exist): the configured convention x modes (parent, both) x overlap. Keys `<conv>/<mode>/<scenario>/<overlap>`;
    each variant lists the cuts with relative motion (gap = opening, soft pieces: `soft_stretch_max`)."""
    pose = sec.spec['rigid']['pose']
    out = {'angles_deg': {'arms_down': pose['down_deg'], 'arms_up': -pose['up_deg'], 'thigh_flex': pose.get('thigh_flex_deg', 45),
                          'thigh_abd': pose.get('thigh_abd_deg', 20), 'spine': pose.get('spine_deg', 20)},
           'pivot_source': sec.joints['source'], 'pivots': {s: {k: np.asarray(v).tolist() for k, v in sec.joints[s].items()} for s in ('l', 'r')},
           'primary': {'mode': getattr(sec, 'overlap_mode', None), 'convention': getattr(sec, 'overlap_convention', None)},
           'variants': {}}
    if 'waist' in sec.joints:
        out['pivots']['waist'] = np.asarray(sec.joints['waist']).tolist()
    plan = []
    if scenario_ready(sec, 'arms'):
        for conv in ('arm', 'torso', 'half'):
            for mode in ('parent', 'child', 'both', 'cover'):
                plan += [(conv, mode, 'arms_down', 'arms', pose['down_deg']), (conv, mode, 'arms_up', 'arms', -pose['up_deg'])]
    conv0 = pose.get('convention', 'arm')
    for mode in ('parent', 'both'):
        for sc, key, ang in (('thigh_flex', 'thigh_flex', pose.get('thigh_flex_deg', 45)), ('thigh_abd', 'thigh_abd', pose.get('thigh_abd_deg', 20)),
                             ('spine', 'spine', pose.get('spine_deg', 20))):
            if scenario_ready(sec, sc):
                plan.append((conv0, mode, key, sc, ang))
    for conv, mode, key, sc, ang in plan:
        for ov in ((False, True) if sec.ext else (False,)):       # strip variants only when the strips were built (--overlap)
            posed, flags, cuts = pose_metrics(sec, ang, conv, ov, mode, sc)
            rigid = [c for c in cuts if 'soft_stretch_max' not in c]
            out['variants'][f'{conv}/{mode}/{key}/{"overlap" if ov else "no_overlap"}'] = {
                'cuts': cuts, 'gap_max_pct_height': max((c['gap_max_pct_height'] for c in rigid), default=0.),
                'gap_mean_pct_height': float(np.mean([c['gap_mean_pct_height'] for c in rigid])) if rigid else 0.,
                **({'soft_stretch_max': max(c['soft_stretch_max'] for c in cuts if 'soft_stretch_max' in c)}
                   if any('soft_stretch_max' in c for c in cuts) else {})}
    return out


# ----------------------------------------------------------------------------------------------- rendering

def _fonts():
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=18), ImageFont.load_default(size=14)
    except TypeError:
        f = ImageFont.load_default()
        return f, f


def tri_normals(tri):
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    return n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-20)


def draw_view(items, sign, bounds, pw, ph, head=12, ss=2, bg=(246, 246, 244)):
    """Orthographic flat-shaded painter's render. `items`: list of (tris (n,3,3), colours (n,3) uint8). View along
    -Z from +Z (`sign` 1 = front) or from -Z (-1 = back; the image is mirrored so the left of the picture is -X...
    viewer-right is anatomical left in front, anatomical right in back). `bounds` = (xmin, xmax, ymin, ymax)."""
    from PIL import Image, ImageDraw
    xmin, xmax, ymin, ymax = bounds
    scale = (pw - 24) / (xmax - xmin)
    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
    img = Image.new('RGB', (pw * ss, ph * ss), bg)
    d = ImageDraw.Draw(img)
    tri = np.concatenate([t for t, _ in items]) if items else np.zeros((0, 3, 3))
    col = np.concatenate([c for _, c in items]) if items else np.zeros((0, 3))
    nrm = tri_normals(tri)
    order = np.argsort(sign * tri[:, :, 2].mean(1), kind='stable')
    for i in order:
        pts = [((sign * (p[0] - cx)) * scale + pw / 2, (ph + head) / 2 - (p[1] - cy) * scale) for p in tri[i]]
        pts = [(x * ss, y * ss) for x, y in pts]
        shade = .55 + .45 * abs(float(sign * nrm[i, 2]))
        d.polygon(pts, fill=tuple(int(c * shade) for c in col[i]), outline=tuple(int(c * .55) for c in col[i]))
    return img.resize((pw, ph), Image.LANCZOS)


def panel_height(bounds, pw, head=12, margin=14):
    xmin, xmax, ymin, ymax = bounds
    return int(np.ceil((ymax - ymin) * (pw - 24) / (xmax - xmin) + head + 2 * margin))


def explode_shift(name, H, rigid):
    """(dx, dy) of a piece in the exploded view: pauldrons +-15% of height sideways by default; with arms the pauldron
    moves 12% out and 6% up, the arm 24% out."""
    base = name.split('_')[0]
    sgn = 1 if name.endswith('_l') else -1 if name.endswith('_r') else 0
    if base == 'pauldron':
        return (sgn * (.12 if rigid else EXPLODE) * H, (.06 if rigid else 0.) * H)
    if base == 'arm':
        return (sgn * .24 * H, 0.)
    if base == 'thigh':
        return (sgn * .10 * H, -.06 * H)
    if base == 'chest':
        return (0., .04 * H)
    if base == 'pelvis':
        return (0., -.02 * H)
    return (0., 0.)


def render_pieces(level, label, plane_x, path, title, names=None, colors=None, sec=None):
    """front / back / exploded-front sheet of the pieces in flat colours (needs Pillow). With `sec` (rigid sections)
    the overlap strips are drawn in yellow."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return False
    rigid = sec is not None
    names = names or {0: 'rest', 1: 'pauldron_l', 2: 'pauldron_r'}
    colors = colors or PIECE_COLORS
    H = level.height
    groups = []
    for lab, name in names.items():
        t = level.raw_v[level.raw_f[label == lab]]
        groups.append((name, t, np.tile(colors[name], (len(t), 1))))
        if rigid and lab in sec.ext and len(sec.ext[lab]['f']):
            st = sec.tris(lab)[0][int((sec.label == lab).sum()):]
            groups.append((name, st, np.tile(STRIP_COLOR, (len(st), 1))))
    pw, margin, head = 560, 28, 12
    shifts = {n: explode_shift(n, H, rigid) for n in names.values()}
    allp = np.concatenate([g[1].reshape(-1, 3) + np.array([*shifts[g[0]], 0.]) for g in groups]
                          + [g[1].reshape(-1, 3) for g in groups])
    xmin, xmax = allp[:, 0].min(), allp[:, 0].max()
    ymin, ymax = allp[:, 1].min(), allp[:, 1].max()
    bounds = (xmin, xmax, ymin, ymax)
    ph = panel_height(bounds, pw)
    font, small = _fonts()
    sheet = Image.new('RGB', (pw * 3, ph + 40 + 34), (246, 246, 244))
    d0 = ImageDraw.Draw(sheet)
    for panel, (sign, explode, text) in enumerate(((1, False, 'front (viewer-right = anatomical left)'), (-1, False, 'back'),
                                                    (1, True, 'exploded, front'))):
        items = []
        for name, t, c in groups:
            if explode:
                t = t + np.array([*shifts[name], 0.])
            items.append((t, c))
        sheet.paste(draw_view(items, sign, bounds, pw, ph), (panel * pw, 40))
        d0.text((panel * pw + 14, 10), text, fill=(30, 30, 30), font=font)
    x = 14
    for lab, name in names.items():
        d0.rectangle([x, ph + 48, x + 16, ph + 64], fill=colors[name])
        d0.text((x + 22, ph + 48), f'{name} {int((label == lab).sum())}f', fill=(30, 30, 30), font=small)
        x += 150 if rigid else 190
    if rigid and sec.ext:
        d0.rectangle([x, ph + 48, x + 16, ph + 64], fill=STRIP_COLOR)
        d0.text((x + 22, ph + 48), 'overlap strip', fill=(30, 30, 30), font=small)
        x += 140
    d0.text((x + 10, ph + 48), title, fill=(90, 90, 90), font=small)
    sheet.save(path)
    return True


def pose_bounds(sec, variants):
    pts = []
    for posed in variants:
        for t in posed.values():
            if len(t):
                pts.append(t.reshape(-1, 3))
    p = np.concatenate(pts)
    return (p[:, 0].min(), p[:, 0].max(), p[:, 1].min(), p[:, 1].max())


def draw_posed(sec, posed, flags, bounds, pw, ph):
    """Front view of a posed variant: pieces in their colours, overlap strips yellow."""
    items = []
    for lab, name in sec.names.items():
        t, fl = posed[lab], flags[lab]
        c = np.tile(RIGID_COLORS[name], (len(t), 1))
        c[fl] = STRIP_COLOR
        items.append((t, c))
    return draw_view(items, 1, bounds, pw, ph)


def render_pose(sec, path, pw=420):
    """Grid of posed front views: rows = pauldron convention (arm, torso), columns = arms down / up x without / with
    overlap; the metric (max gap, % of height) is printed under every panel."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    pose = sec.spec['rigid']['pose']
    with_ov = bool(sec.ext)                       # strips only when they were asked for (--overlap)
    cols = [('arms_down', pose['down_deg'], False)] + ([('arms_down', pose['down_deg'], True)] if with_ov else []) \
        + [('arms_up', -pose['up_deg'], False)] + ([('arms_up', -pose['up_deg'], True)] if with_ov else [])
    cache = {}
    mode = sec.spec['rigid']['overlap'].get('mode', 'parent')
    for conv in ('arm', 'torso', 'half'):
        for aname, ang, ov in cols:
            cache[(conv, aname, ov)] = pose_metrics(sec, ang, conv, ov, mode)
    bounds = pose_bounds(sec, [v[0] for v in cache.values()])
    ph = panel_height(bounds, pw)
    font, small = _fonts()
    sheet = Image.new('RGB', (pw * len(cols), (ph + 44) * 3 + 9), (246, 246, 244))
    d0 = ImageDraw.Draw(sheet)
    metrics = {}
    for r, conv in enumerate(('arm', 'torso', 'half')):
        for c, (aname, ang, ov) in enumerate(cols):
            posed, flags, cuts = cache[(conv, aname, ov)]
            y0 = r * (ph + 44 + 3)
            sheet.paste(draw_posed(sec, posed, flags, bounds, pw, ph), (c * pw, y0 + 22))
            gmax = max((x['gap_max_pct_height'] for x in cuts), default=0.)
            d0.text((c * pw + 8, y0 + 2), f'pauldron follows {conv}: {aname.replace("_", " ")} {abs(ang):g} deg, {"overlap " + mode if ov else "no overlap"}',
                    fill=(30, 30, 30), font=small)
            d0.text((c * pw + 8, y0 + 22 + ph - 18), f'max gap {gmax:.2f}% H', fill=(150, 30, 30), font=small)
    sheet.save(path)
    return True


# ----------------------------------------------------------------------------------------------- blend

def find_blender():
    return os.environ.get('BLENDER') or shutil.which('blender') or '/usr/local/bin/blender'


def blend_worker(lod_dir, level_name, names):
    """Runs inside Blender: import the piece GLBs, name and colour the objects, save pieces_<lv>.blend."""
    import bpy
    lod_dir = Path(lod_dir)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    coll = bpy.data.collections.new('pieces_' + level_name)
    bpy.context.scene.collection.children.link(coll)
    shared = {}
    import numpy as np
    faces = np.load(lod_dir / 'faces.npz', allow_pickle=True) if (lod_dir / 'faces.npz').is_file() else None
    for name in names:
        before = set(bpy.data.objects)
        bpy.ops.import_scene.gltf(filepath=str(lod_dir / f'{name}.glb'))
        new = [o for o in bpy.data.objects if o not in before and o.type == 'MESH']
        for o in new:
            for c in list(o.users_collection):
                c.objects.unlink(o)
            coll.objects.link(o)
            o.name = name
            o.data.name = name
            o.color = (*[c / 255 for c in PIECE_COLORS[name]], 1.)
            o['piece'] = name
            if faces is not None and f'{name}__overlap' in faces.files and len(faces[f'{name}__overlap']) == len(o.data.polygons):
                o.data.attributes.new('overlap', 'INT', 'FACE').data.foreach_set('value', faces[f'{name}__overlap'].astype(np.int32))
        for o in [o for o in bpy.data.objects if o not in before and o.type != 'MESH']:
            bpy.data.objects.remove(o)
        for o in new:                                   # one shared material per role (the importer adds .001 copies)
            for slot in o.material_slots:
                role = slot.material.name.split('.')[0]
                slot.material = shared.setdefault(role, slot.material)
    for mat in [m for m in bpy.data.materials if m.users == 0]:
        bpy.data.materials.remove(mat)
    dest = lod_dir / f'pieces_{level_name}.blend'
    bpy.ops.wm.save_as_mainfile(filepath=str(dest), compress=True)
    print('PIECES_BLEND', dest, flush=True)


def build_blend(lod_dir, level_name, names):
    try:
        import bpy  # noqa: F401
        blend_worker(lod_dir, level_name, names)
        return str(Path(lod_dir) / f'pieces_{level_name}.blend')
    except ImportError:
        pass
    cmd = [find_blender(), '-b', '--python', str(Path(__file__).resolve()), '--', '--blend-worker', str(lod_dir),
           level_name, ','.join(names)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    dest = Path(lod_dir) / f'pieces_{level_name}.blend'
    if res.returncode != 0 or not dest.is_file():
        print('BLEND_FAILED', res.stdout[-600:], res.stderr[-600:], file=sys.stderr)
        return None
    return str(dest)


# ----------------------------------------------------------------------------------------------- driver

def levels_in(reduced):
    return [p.name[len('lod_error_'):-len('__wearable.glb')]
            for p in sorted(Path(reduced).glob('lod_error_*__wearable.glb'), reverse=True)]


def load_level(reduced, name, manifest, plane_x=None):
    reduced = Path(reduced)
    glb = read_glb(reduced / f'lod_error_{name}__wearable.glb')
    scale = (manifest or {}).get('normalization_scale')
    height = 2. / scale if scale else float(glb['v'][:, 1].max() - glb['v'][:, 1].min())
    level = Level(name, glb['v'], glb['f'], glb['face_material'], glb['materials'], height)
    level.height_source = 'manifest normalization_scale (normalized height 2)' if scale else 'wearable bbox height'
    attach_parts(level, reduced / f'lod_error_{name}.glb', reduced / f'lod_error_{name}_colors.npz')
    level.plane = find_plane(manifest, reduced, level, plane_x)
    level.joints = find_joints(manifest, reduced)
    return level


def split_level(level, spec, lod_dir, render=True, blend=True):
    rigid = spec['rigid_method']
    sec = None
    if rigid:
        sec = build_sections(level, spec, rigid, spec['overlap'])
        label, side, flags, report, names = np.where(sec.dropped, -1, sec.label), sec.side, sec.flags, sec.report, sec.names
        stats, sym = piece_stats(level, label, level.plane['x'])
        spec = sec.spec if sec.method == 'regions' else spec          # regions: the pauldron step ran with the rim style
    else:
        label, side, flags, report = classify(level, spec)
        names = piece_names(spec, False)
        stats, sym = piece_stats(level, label, level.plane['x'])
    plane_x = level.plane['x']
    lod_dir = Path(lod_dir)
    lod_dir.mkdir(parents=True, exist_ok=False)
    written, overlap_flags = [], {}
    for lab, name in names.items():
        sel = np.flatnonzero(label == lab)
        v, f, mat = level.raw_v, level.raw_f[sel], level.face_material[sel]
        ovf = np.zeros(len(sel), bool)
        ext = sec.ext.get(lab) if sec is not None else None
        if ext is not None and len(ext['f']):
            v = np.concatenate([v, ext['v']])
            f = np.concatenate([f, ext['f']])
            mat = np.concatenate([mat, ext['mat']])
            ovf = np.r_[ovf, np.ones(len(ext['f']), bool)]
        if len(f):
            write_glb(lod_dir / f'{name}.glb', name, v, f, mat, level.materials)
            written.append(name)
            overlap_flags[f'{name}__overlap'] = ovf[np.argsort(mat, kind='stable')].astype(np.int8)
    np.savez_compressed(lod_dir / 'faces.npz', piece=label.astype(np.int8), piece_names=np.array([names[i] for i in sorted(names)]), piece_ids=np.array(sorted(names)),
                        side=side.astype(np.int8), role=level.face_role, role_names=np.array(level.role_names),
                        body_part=level.part if level.part is not None else np.full(level.n, -1),
                        body_part_names=np.array(level.part_names), flags=flags, flag_bits=np.array(report['flag_bits']),
                        centroid=level.centroid, area=level.area, **overlap_flags)
    title = f'{spec["style"]} style, level {level.name}' + (f', rigid {sec.method}' if rigid else '')
    rendered = False
    if render:
        rendered = render_pieces(level, label, plane_x, lod_dir / 'pieces_render.png', title,
                                 names if rigid else None, RIGID_COLORS if rigid else None, sec)
    blend_path = build_blend(lod_dir, level.name, written) if blend else None
    pieces = {names[0]: stats['rest'], names[1]: stats['L'], names[2]: stats['R']}
    if rigid:
        for lab in [l for l in sorted(names) if l > 2]:
            pieces[names[lab]] = one_piece_stats(level, label, lab)
        sym_arm = mirror_stats(level, label, 3, 4, pieces['arm_l'], pieces['arm_r'], plane_x)
        if 'thigh_l' in pieces and 'thigh_r' in pieces:
            sym_arm['thigh'] = mirror_stats(level, label, 6, 7, pieces['thigh_l'], pieces['thigh_r'], plane_x)
    for key in [names[l] for l in sorted(names) if l > 0 and not names[l].startswith('soft')]:  # soft may have several patches
        if pieces[key]['components'] > 1:
            report['warnings'].append(f'{key} has more than one connected patch (see components)')
        if not pieces[key]['faces']:
            report['warnings'].append(f'{key} is empty')
    if pieces[names[0]]['components'] > 1 and rigid:
        report['warnings'].append(f'{names[0]} has {pieces[names[0]]["components"]} connected patches')
    if spec['style'] == 'rim':
        for lab, key in ((1, 'L'), (2, 'R')):
            on_side = (label == lab)
            if on_side.any() and not (on_side & (flags >> 4 & 1).astype(bool)).any():
                report['warnings'].append(f'{names[lab]}: no enclosed field faces found (rim only: closed ring missing or field leaks to the body)')
    info = {
        'level': level.name, 'style': spec['style'], 'spec': {k: spec[k] for k in (
            'piece', 'roles', 'region_parts', 'min_region_share', 'min_island_faces', 'extra_component_share', 'close_rings',
            'max_field_share', 'min_enclosure', 'seed_in_region')},
        'wearable_faces': level.n, 'wearable_area': float(level.area.sum()), 'body_height': level.height,
        'body_height_source': level.height_source, 'roles': level.role_names,
        'body_parts': level.parts_info, 'plane': level.plane, 'pieces': pieces, 'symmetry': sym,
        'seed_faces': report['seed_faces'], 'stray_seed_components': report['stray_seed_components'],
        'rim_fields': report['fields'], 'absorbed_islands': report['absorbed_islands'],
        'ambiguous_faces': report['ambiguous_faces'], 'flag_bits': report['flag_bits'], 'warnings': report['warnings'],
        'outputs': {'glb': [f'{n}.glb' for n in written], 'faces': 'faces.npz',
                    'render': 'pieces_render.png' if rendered else None,
                    'blend': Path(blend_path).name if blend_path else None},
    }
    if rigid:
        arm_cfg = {k: spec['rigid'].get(k) for k in ('arm_parts', 'snap', 'overlap', 'pose')}
        info['rigid'] = {
            'method': rigid, 'method_used': sec.method, 'auto': report.get('auto'), 'config': arm_cfg, 'zone_source': report.get('zone_source'),
            'joints': ({s: {k: np.asarray(v).tolist() for k, v in sec.joints[s].items()} for s in ('l', 'r')}
                       | ({'waist': np.asarray(sec.joints['waist']).tolist()} if 'waist' in sec.joints else {})
                       | {'source': sec.joints['source']} if sec.joints else None),
            'bands': report.get('bands'), 'regions': report.get('regions'), 'pauldron_spec': report.get('pauldron_spec'),
            'arm_symmetry': sym_arm, 'arm_faces_zone_guess': report['arm_faces_zone_guess'],
            'colour': report.get('colour'), 'seam': {k: report.get(k) for k in ('seam_soft', 'seam_components', 'seam_not_soft')} if 'seam_soft' in report else None,
            'snap': report.get('snap'), 'cross_joint': report.get('cross_joint'), 'flaps': report.get('flaps'), 'cuts_zone_guess': report['cuts_zone_guess'], 'cuts': report['cuts'],
            'overlap': getattr(sec, 'overlap_report', None)}
        if spec['pose_test']:
            if not sec.joints:
                report['warnings'].append('pose test skipped: no shoulder joints (no parts.npz and no --joint-l/--joint-r)')
            else:
                info['rigid']['pose'] = pose_test(sec)
                if render and render_pose(sec, lod_dir / 'pose_render.png'):
                    info['outputs']['pose_render'] = 'pose_render.png'
    return info


def run(reduced, style=None, module=None, levels=None, output_dir=None, overrides=None, plane_x=None, render=True,
        blend=True, config=CONFIG):
    reduced = Path(reduced)
    manifest = json.loads((reduced / 'manifest.json').read_text()) if (reduced / 'manifest.json').is_file() else {}
    module = module or manifest.get('module') or reduced.resolve().parent.name
    spec = load_spec(module, style, config, overrides)
    roles = [g['role'] for g in ((manifest.get('palette') or {}).get('garments') or [])]
    joint_palette = bool(set(roles) & set(JOINT_ROLES))
    adaptive = bool((manifest.get('hue_fit') or {}).get('active'))
    if spec['rigid_method'] is None and module == 'body_armor' and spec['rigid'] and (joint_palette or adaptive):
        # a separable reduction (joint-role garment in its palette, or the adaptive hue fit that separable/regions
        # reductions run with): default = auto (regions / bands / colour / snap, see build_sections), no overlap strips unless asked
        spec['rigid_method'] = spec['rigid'].get('default_for_separable', 'auto')
        ov_cfg = spec['rigid'].get('overlap') or {}
        spec['overlap'] = spec['overlap'] or bool(ov_cfg.get('enabled', False) and not (overrides or {}).get('no_overlap'))
        spec['default_rigid_reason'] = 'separable palette (joint role)' if joint_palette else 'separable reduction (adaptive hue fit on)'
    out = Path(output_dir) if output_dir else reduced.resolve().parent / 'pieces'
    if (out / 'pieces.json').exists():
        raise SystemExit(f'{out}/pieces.json exists; choose a new --output-dir (earlier attempts are never overwritten)')
    wanted = levels or levels_in(reduced)
    if not wanted:
        raise SystemExit(f'no lod_error_*__wearable.glb in {reduced}')
    out.mkdir(parents=True, exist_ok=True)
    result = {'source': str(reduced), 'module': module, 'style': spec['style'], 'piece': spec['piece'],
              'rigid_method': spec['rigid_method'], 'overlap': spec['overlap'], 'default_rigid_reason': spec.get('default_rigid_reason'),
              'levels': {}}
    for name in wanted:
        level = load_level(reduced, name, manifest, plane_x)
        info = split_level(level, spec, out / f'lod_{name}', render, blend)
        result['levels'][name] = info
        pc = info['pieces']
        print(f'PIECES {name}: ' + ', '.join(f'{k} {v["faces"]}f/{v["components"]}c' for k, v in pc.items()) +
              f'; islands absorbed {len(info["absorbed_islands"])}; warnings {len(info["warnings"])}', flush=True)
        for c in (info.get('rigid') or {}).get('cuts', []):
            print(f'  CUT {c["pair"][0]}|{c["pair"][1]}: {c["edges"]} edges, {c["length_pct_height"]:.1f}% H, colour boundary {c["role_boundary_share"]:.0%}, crease {c["crease_share"]:.0%}', flush=True)
        for w in info['warnings']:
            print('  WARNING', w, flush=True)
    (out / 'pieces.json').write_text(json.dumps(result, indent=2))
    print('PIECES_JSON', out / 'pieces.json', flush=True)
    return result


def main(argv=None):
    if argv is None:
        argv = sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else sys.argv[1:]
    if argv and argv[0] == '--blend-worker':
        blend_worker(argv[1], argv[2], [n for n in argv[3].split(',') if n])
        return
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('reduced_dir')
    p.add_argument('--style', choices=['roles', 'rim'], help='default: config.json piece_split[module].style')
    p.add_argument('--module', help='default: manifest module / folder name')
    p.add_argument('--levels', help='comma list, e.g. 0p4,0p2 (default: all wearable GLBs)')
    p.add_argument('--output-dir', help='default: <reduced_dir>/../pieces')
    p.add_argument('--roles', help='comma list of garment roles that form the pauldron (overrides the style default)')
    p.add_argument('--region-parts', help='comma list of body part names without .l/.r (default: shoulder,arm_stretch)')
    p.add_argument('--seed-in-region', action='store_true', help='only seed faces whose own body part is in the region')
    p.add_argument('--min-island', type=int)
    p.add_argument('--close-rings', type=int)
    p.add_argument('--max-field-share', type=float)
    p.add_argument('--plane-x', type=float, help='sagittal plane x in the GLB frame (default: manifest/base/wearable)')
    p.add_argument('--rigid', choices=['zones', 'snap', 'seam', 'colour', 'bands', 'regions', 'auto', 'none'], help='regions = per-piece base colours (large same-colour regions + the decoration they enclose, assigned to chest/arms/pelvis/thighs by body part and expected role; per-joint snap fallback), bands = cyan joint bands (chest/pelvis/thighs/arms, per-joint snap fallback), auto = bands when the wearable has joint-role faces, colour for the cyan quinary role, regions when it has >= 3 large same-colour regions of the expected section roles, else snap on all joints (the default for separable reductions), none = no rigid split; also cut torso / arm_l / arm_r: zones = body-part zones (M1), snap = zone guess re-cut on colour/crease edges (M2), seam = rigid plates between soft secondary bands (M3), colour = arm role faces (M4)')
    p.add_argument('--expect', help='regions: expected main colour per section, e.g. arm:quinary,chest:primary (keys chest, arm, pauldron, pelvis, thigh; default config rigid.regions.expected)')
    p.add_argument('--soft', choices=['piece', 'nearest'], help='seam/bands: soft faces become separate skinned pieces or join the nearest rigid piece')
    p.add_argument('--joints', choices=['arms', 'all'], help='zones/snap: cut only the arms (default) or also waist and hips (pelvis + thighs)')
    p.add_argument('--no-overlap', action='store_true', help='skip the overlap strips (the default since 2026-10-22; kept for old command lines)')
    p.add_argument('--no-cross-joint', action='store_true', help='rigid: keep fringe patches that lie beyond their joint on the section the colours gave them (default: move them to the section on that side, config rigid.cross_joint)')
    p.add_argument('--keep-flaps', action='store_true', help='rigid: keep single-edge flap faces that stick out of a piece (default: drop them from the exported pieces, config rigid.flaps)')
    p.add_argument('--pauldron-convention', choices=['arm', 'torso', 'half'], help='pose / exported strips: pauldron follows the arm, the torso or half the arm rotation')
    p.add_argument('--overlap-mode', choices=['parent', 'child', 'both', 'cover'])
    p.add_argument('--overlap', action='store_true', help='rigid: add hidden underlap strips at every cut (off by default)')
    p.add_argument('--overlap-length', type=float, help='strip length, fraction of body height (default config 0.03)')
    p.add_argument('--overlap-offset', type=float, help='strip sink depth, fraction of body height (default config 0.004)')
    p.add_argument('--snap-band', type=float, help='snap: half-width of the re-cut band, fraction of body height (default 0.08)')
    p.add_argument('--pose-test', action='store_true', help='rigid: rotate arms about the shoulder joints, report gaps, render pose_render.png')
    p.add_argument('--joint-l', help='shoulder pivot x,y,z (GLB frame) instead of the base parts file')
    p.add_argument('--joint-r', help='same, anatomical right')
    p.add_argument('--no-blend', action='store_true')
    p.add_argument('--no-render', action='store_true')
    a = p.parse_args(argv)
    ov = {'roles': a.roles.split(',') if a.roles else None,
          'region_parts': a.region_parts.split(',') if a.region_parts else None,
          'seed_in_region': True if a.seed_in_region else None, 'min_island_faces': a.min_island,
          'close_rings': a.close_rings, 'max_field_share': a.max_field_share, 'rigid_method': a.rigid,
          'overlap': True if a.overlap else None, 'overlap_length': a.overlap_length, 'overlap_offset': a.overlap_offset,
          'snap_band': a.snap_band, 'soft_mode': a.soft, 'joints': a.joints, 'no_overlap': True if a.no_overlap else None, 'cross_joint': False if a.no_cross_joint else None, 'flaps': False if a.keep_flaps else None, 'pose_convention': a.pauldron_convention, 'overlap_mode': a.overlap_mode, 'pose_test': True if a.pose_test else None,
          'expect': dict(x.split(':') for x in a.expect.split(',')) if a.expect else None,
          'joint_l': [float(x) for x in a.joint_l.split(',')] if a.joint_l else None,
          'joint_r': [float(x) for x in a.joint_r.split(',')] if a.joint_r else None}
    run(a.reduced_dir, a.style, a.module, a.levels.split(',') if a.levels else None, a.output_dir, ov, a.plane_x,
        not a.no_render, not a.no_blend)


if __name__ == '__main__':
    main()
