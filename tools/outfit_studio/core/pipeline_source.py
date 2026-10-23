"""Pipeline outputs -> item geometry (SPEC section 3, no bpy): discovery, GLB reading, per-face data, module -> items.

Face-level inputs are numpy arrays in the base world frame (see transform.py); every piece comes out as its own
mesh {V, F, role, part, src, channel} with compacted vertices, so rigid pieces never share a vertex."""
import json
import re
import struct
from pathlib import Path

import numpy as np

from . import channels as chan

MODULES = ('outfit', 'body_armor', 'right_gauntlet_boot')
LODS = ('0p4', '0p2', '0p1')                 # geometry error 0.4 / 0.2 / 0.1 % of height, default first
MODULE_ITEMS = {'outfit': ('outfit',), 'body_armor': ('body_armor',), 'right_gauntlet_boot': ('gauntlets', 'boots')}
SOURCES = ('texture', 'symmetry', 'prior', 'neighbours', 'off_body', 'riser', 'mirrored')   # face_labels.SOURCES
MIRRORED = SOURCES.index('mirrored')
PIECE_ORDER = {
    'body_armor': ('chest', 'pelvis', 'thigh_l', 'thigh_r', 'arm_l', 'arm_r', 'pauldron_l', 'pauldron_r'),
    'gauntlets': ('cuff_r', 'hand_r', 'cuff_l', 'hand_l'),
    'boots': ('shin_r', 'foot_r', 'shin_l', 'foot_l'),
    'outfit': ('outfit',),
}
RIGHT_PIECES = {'gauntlets': ('cuff_r', 'hand_r'), 'boots': ('shin_r', 'foot_r')}
SMALL_REGION = .08            # same-role regions below this share of the wearable area follow their majority piece
ISLAND_FACES = 6              # piece islands below this size are absorbed by the surrounding piece


def item_id(set_id, item_type):
    return f'{set_id}__{item_type}'


RACE_WORDS = ('dwarf', 'elf', 'gnome', 'halfling', 'human', 'orc')


def canonical_set(folder):
    """Game set id of a models/ set folder: a trailing race (`plain_plate_mail_01_gnome`,
    `..._dwarf_female`) only marks a per-race job, the item is the same across races."""
    return re.sub(r'_(%s)(_(male|female))?$' % '|'.join(RACE_WORDS), '', folder)


# ------------------------------------------------------------------------------------------------- discovery

def discover(models=None):
    """Every `<set>/<job>/<race>/<module>/reduced/manifest.json` with a known module (attempt folders and other
    generations are skipped). Entries are dicts, newest first."""
    if models is None:
        from .. import paths
        models = paths.MODELS
    root, out = Path(models), []
    for man in root.glob('*/*/*/*/reduced/manifest.json'):
        set_id, job_id, race, module = man.relative_to(root).parts[:4]
        if module not in MODULES or any('_attempt' in p for p in (set_id, job_id, race, module)):
            continue
        reduced = man.parent
        levels = [lv for lv in LODS if (reduced / f'lod_error_{lv}__wearable.glb').is_file()]
        out.append({'set_id': canonical_set(set_id), 'source_set': set_id, 'job_id': job_id, 'race': race, 'module': module, 'dir': str(man.parents[1]),
                    'reduced': str(reduced), 'manifest': str(man), 'mtime': man.stat().st_mtime, 'levels': levels})
    return sorted(out, key=lambda e: (-e['mtime'], e['set_id'], e['race'], e['module']))


def latest(entries):
    """The newest entry per (set, race, module); `others` lists the older jobs."""
    best = {}
    for e in sorted(entries, key=lambda e: -e['mtime']):
        key = (e['set_id'], e['race'], e['module'])
        if key in best:
            best[key]['others'].append(e['job_id'])
        else:
            best[key] = {**e, 'others': []}
    return sorted(best.values(), key=lambda e: (e['set_id'], e['race'], e['module']))


def load_manifest(entry):
    return json.loads(Path(entry['manifest']).read_text())


def wearable_path(entry, lod):
    return Path(entry['reduced']) / f'lod_error_{lod}__wearable.glb'


def labelled_path(entry, lod):
    return Path(entry['reduced']) / f'lod_error_{lod}.glb'


def pieces_output(entry, lod):
    """{piece: glb path} of the segment-first / split_pieces rigid output for this LOD, else None. Only a split with
    chest or pelvis counts (a pauldron-only split does not describe the whole armour)."""
    base = Path(entry['dir'])
    for sub in ('segment_first/pieces', 'pieces'):
        d = base / sub
        if (d / 'pieces.json').is_file() and (d / f'lod_{lod}').is_dir():
            glbs = {p.stem: p for p in sorted((d / f'lod_{lod}').glob('*.glb'))}
            if 'chest' in glbs or 'pelvis' in glbs:
                return glbs
    return None


# ------------------------------------------------------------------------------------------------- GLB

_DTYPE = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_NCOMP = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}


def _accessor(js, blob, index):
    acc = js['accessors'][index]
    view = js['bufferViews'][acc['bufferView']]
    dtype, ncomp = np.dtype(_DTYPE[acc['componentType']]), _NCOMP[acc['type']]
    offset = view.get('byteOffset', 0) + acc.get('byteOffset', 0)
    stride = view.get('byteStride') or dtype.itemsize * ncomp
    return np.ndarray((acc['count'], ncomp), dtype, blob, offset, (stride, dtype.itemsize)).copy()


def _node_matrix(node):
    if 'matrix' in node:
        return np.array(node['matrix'], np.float64).reshape(4, 4).T
    m = np.eye(4)
    if 'scale' in node:
        m = np.diag([*node['scale'], 1.]) @ m
    if 'rotation' in node:
        x, y, z, w = node['rotation']
        r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                      [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                      [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
        m = np.pad(r, ((0, 1), (0, 1)), constant_values=0) @ m
        m[3, 3] = 1
    if 'translation' in node:
        m[:3, 3] += node['translation']
    return m


def role_name(name):
    """Material name -> role (`primary.001` -> `primary`)."""
    return re.sub(r'\.\d+$', '', name).lower()


def read_glb(path):
    """Triangles of a GLB in Blender coordinates (what the glTF importer gives: (x, y, z) -> (x, -z, y)).
    {'v' (n,3), 'f' (m,3), 'face_material' (m,), 'materials' [role names]}; primitives in file order, which is the
    face order the pipeline's `*_glb_order` arrays refer to."""
    data = Path(path).read_bytes()
    magic, _, _ = struct.unpack_from('<4sII', data, 0)
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
    node_of = {n['mesh']: _node_matrix(n) for n in js.get('nodes', []) if 'mesh' in n}
    materials = [role_name(m.get('name', f'material{i}')) for i, m in enumerate(js.get('materials', []))] or ['primary']
    vs, fs, ms, base = [], [], [], 0
    for mi, mesh in enumerate(js['meshes']):
        mat4 = node_of.get(mi, np.eye(4))
        for prim in mesh['primitives']:
            if prim.get('mode', 4) != 4:
                raise ValueError(f'{path}: only triangle primitives are supported')
            pos = _accessor(js, blob, prim['attributes']['POSITION']).astype(np.float64)
            pos = pos @ mat4[:3, :3].T + mat4[:3, 3]
            idx = (_accessor(js, blob, prim['indices']).astype(np.int64).ravel() if 'indices' in prim
                   else np.arange(len(pos)))
            vs.append(pos)
            fs.append(idx.reshape(-1, 3) + base)
            ms.append(np.full(len(idx) // 3, prim.get('material', 0), np.int64))
            base += len(pos)
    v = np.concatenate(vs) if vs else np.zeros((0, 3))
    return {'v': np.stack([v[:, 0], -v[:, 2], v[:, 1]], 1), 'f': np.concatenate(fs) if fs else np.zeros((0, 3), np.int64),
            'face_material': np.concatenate(ms) if ms else np.zeros(0, np.int64), 'materials': materials}


def face_roles(glb):
    """Per-face role strings of a read_glb result."""
    return np.array([glb['materials'][i] for i in glb['face_material']], dtype='<U12')


def wearable_face_data(entry, lod, n_faces):
    """Per wearable face (GLB order) body part names and label source from `lod_error_<lv>_colors.npz`, or
    None when the arrays are missing or their length differs from the imported mesh."""
    path = Path(entry['reduced']) / f'lod_error_{lod}_colors.npz'
    if not path.is_file():
        return None
    z = np.load(path, allow_pickle=False)
    if 'wearable_body_part_glb_order' not in z.files or len(z['wearable_body_part_glb_order']) != n_faces:
        return None
    names = np.array([str(n) for n in z['body_part_names']])
    part = names[z['wearable_body_part_glb_order']]
    if 'wearable_label_source_glb_order' in z.files:
        src = z['wearable_label_source_glb_order']
    elif 'face_label_source' in z.files and 'wearable_face_order' in z.files:
        order = z['wearable_face_order']
        src = np.where(order >= 0, z['face_label_source'][np.maximum(order, 0)], MIRRORED)
    else:
        src = np.zeros(n_faces, np.int64)
    return {'part': part, 'src': np.asarray(src, np.int64), 'sha256_source': None}


# ------------------------------------------------------------------------------------------------- mesh helpers

def weld(v, f, tol=1e-7):
    """Merge coincident vertices (unwelded flat-shaded GLB). Face order and count are kept."""
    key = np.round(np.asarray(v, np.float64) / tol).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    inv = inv.reshape(-1)
    return np.asarray(v, np.float64)[first], inv[np.asarray(f, np.int64)]


def tri_areas(V, F):
    V = np.asarray(V, np.float64)
    return .5 * np.linalg.norm(np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]]), axis=1)


def tri_centroids(V, F):
    return np.asarray(V, np.float64)[F].mean(1)


def face_pairs(F):
    """(k, 2) pairs of faces sharing an edge."""
    F = np.asarray(F, np.int64)
    edges = np.sort(np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1)
    face = np.tile(np.arange(len(F)), 3)
    order = np.lexsort((edges[:, 1], edges[:, 0]))
    e, fa = edges[order], face[order]
    same = np.flatnonzero((e[1:] == e[:-1]).all(1))
    return np.stack([fa[same], fa[same + 1]], 1)


def components(n, pairs):
    """Component id (smallest face index in it) of every face, given the pairs that connect them."""
    lab = np.arange(n)
    if len(pairs) == 0:
        return lab
    a, b = pairs[:, 0], pairs[:, 1]
    while True:
        m = np.minimum(lab[a], lab[b])
        new = lab.copy()
        np.minimum.at(new, a, m)
        np.minimum.at(new, b, m)
        new = new[new]
        if (new == lab).all():
            return lab
        lab = new


def resolve_uncertain(role, pairs, known=chan.ROLES):
    """Faces with a role outside `known` (`uncertain`) take the most common role among their neighbours."""
    role = role.copy()
    bad = ~np.isin(role, known)
    for _ in range(8):
        if not bad.any():
            break
        votes = [[] for _ in range(len(role))]
        for a, b in pairs:
            if bad[a] and not bad[b]:
                votes[a].append(role[b])
            if bad[b] and not bad[a]:
                votes[b].append(role[a])
        fixed = False
        for i in np.flatnonzero(bad):
            if votes[i]:
                vals, cnt = np.unique(votes[i], return_counts=True)
                role[i] = vals[cnt.argmax()]
                fixed = True
        bad = ~np.isin(role, known)
        if not fixed:
            break
    role[bad] = known[0]
    return role


# ------------------------------------------------------------------------------------------------- module -> items

ZONE = {'spine_01.x': 'chest', 'neck.x': 'chest', 'head.x': 'chest', 'shoulder.l': 'chest', 'shoulder.r': 'chest',
        'root.x': 'pelvis', 'skirt': 'pelvis'}
for _s in 'lr':
    ZONE.update({f'thigh_stretch.{_s}': f'thigh_{_s}', f'leg_stretch.{_s}': f'thigh_{_s}', f'foot.{_s}': f'thigh_{_s}',
                 f'arm_stretch.{_s}': f'arm_{_s}', f'forearm_stretch.{_s}': f'arm_{_s}', f'hand.{_s}': f'arm_{_s}'})


def smooth_pieces(piece, role, area, pairs, small=SMALL_REGION, island=ISLAND_FACES):
    """Tidy a zone-based piece map: small connected same-role regions follow their majority piece, and tiny islands
    of a piece (not its largest patch) are absorbed by the piece around them."""
    piece = piece.copy()
    total = float(area.sum()) or 1.
    lab = components(len(piece), pairs[role[pairs[:, 0]] == role[pairs[:, 1]]])
    for c in np.unique(lab):
        m = lab == c
        sides = {n[-2:] for n in piece[m] if n.endswith(('_l', '_r'))}
        if area[m].sum() < small * total and len(sides) < 2:       # never merge across the body's midline
            names, inv = np.unique(piece[m], return_inverse=True)
            piece[m] = names[np.bincount(inv.reshape(-1), weights=area[m]).argmax()]
    lab = components(len(piece), pairs[piece[pairs[:, 0]] == piece[pairs[:, 1]]])
    ids, inv, size = np.unique(lab, return_inverse=True, return_counts=True)
    inv = inv.reshape(-1)
    for k in np.flatnonzero(size < island):
        m = inv == k
        owner = piece[m][0]
        if size[(inv[piece == owner])].max() == size[k]:
            continue                                         # largest patch of its piece: keep
        a, b = pairs[:, 0], pairs[:, 1]
        other = np.r_[piece[b][m[a] & ~m[b]], piece[a][m[b] & ~m[a]]]
        if len(other):
            names, cnt = np.unique(other, return_counts=True)
            piece[m] = names[cnt.argmax()]
    return piece


def zone_pieces(part, role, area, pairs):
    """Per-face rigid piece of body armour from body parts (the built-in fallback): see SPEC section 3."""
    piece = np.array([ZONE.get(p, 'chest') for p in part], dtype='<U12')
    base = np.array([p.rsplit('.', 1)[0] for p in part])
    side = np.array([p.rsplit('.', 1)[-1] for p in part])
    paul = (role == 'accent') & np.isin(base, ('shoulder', 'arm_stretch')) & np.isin(side, ('l', 'r'))
    piece[paul] = np.char.add('pauldron_', side[paul])
    return smooth_pieces(piece, role, area, pairs)


def grow(core, allowed, pairs):
    """`core` plus the `allowed` faces connected to it through `allowed` faces."""
    sel = core.copy()
    a, b = pairs[:, 0], pairs[:, 1]
    while True:
        near = np.zeros(len(sel), bool)
        near[b[sel[a]]] = True
        near[a[sel[b]]] = True
        add = near & allowed & ~sel
        if not add.any():
            return sel
        sel |= add


def limb_pieces(part, pairs):
    """Right-side gauntlet and boot piece names per face ('' = not part of that item): hand / forearm (+ the arm
    faces touching them) and foot / lower leg (+ the thigh faces touching them), split at wrist and ankle."""
    n = len(part)
    gaunt, boot = np.full(n, '', '<U8'), np.full(n, '', '<U8')
    hand, fore = part == 'hand.r', part == 'forearm_stretch.r'
    foot, shin = part == 'foot.r', part == 'leg_stretch.r'
    fore = grow(fore | hand, part == 'arm_stretch.r', pairs) & ~hand
    shin = grow(shin | foot, part == 'thigh_stretch.r', pairs) & ~foot
    gaunt[fore], gaunt[hand] = 'cuff_r', 'hand_r'
    boot[shin], boot[foot] = 'shin_r', 'foot_r'
    return gaunt, boot


def cut_piece(V, F, mask, **face_arrays):
    """The faces `mask` of mesh (V, F) as a standalone piece with compacted vertices plus the per-face arrays."""
    F = np.asarray(F)[mask]
    used, inv = np.unique(F, return_inverse=True)
    return {'V': np.asarray(V, np.float64)[used], 'F': inv.reshape(F.shape).astype(np.int64),
            **{k: np.asarray(a)[mask] for k, a in face_arrays.items()}}


def _flip(part):
    """Anatomical side swap of body part names (.r -> .l)."""
    return np.array([p[:-2] + '.l' if p.endswith('.r') else p for p in part], dtype=part.dtype)


def mirror_piece(piece, plane_x=0.):
    """The left twin of a right piece: reflected about x = plane_x, winding flipped, parts renamed, source 'mirrored'."""
    V = piece['V'].copy()
    V[:, 0] = 2 * plane_x - V[:, 0]
    out = {**piece, 'V': V, 'F': piece['F'][:, [0, 2, 1]].copy(), 'part': _flip(piece['part']),
           'src': np.full(len(piece['F']), MIRRORED, np.int64)}
    out.pop('channel', None)
    return out


def swap_side(name):
    return name[:-2] + ('_l' if name.endswith('_r') else '_r') if name.endswith(('_l', '_r')) else name


def assemble(item_type, pieces):
    """Order pieces canonically, build the channels and set each piece's per-face `channel`. -> (pieces, channels)."""
    order = [n for n in PIECE_ORDER[item_type] if n in pieces] + sorted(set(pieces) - set(PIECE_ORDER[item_type]))
    pieces = {n: pieces[n] for n in order}
    names = np.concatenate([np.full(len(p['F']), n, '<U16') for n, p in pieces.items()])
    role = np.concatenate([p['role'] for p in pieces.values()])
    area = np.concatenate([tri_areas(p['V'], p['F']) for p in pieces.values()])
    channels, ch = chan.build_channels(item_type, names, role, area, order)
    start = 0
    for p in pieces.values():
        p['channel'] = ch[start:start + len(p['F'])]
        start += len(p['F'])
    return pieces, channels


def make_items(module, V, F, role, part, src, plane_x=0., given=None):
    """Split a world-space wearable (welded V, F; per-face role / part names / label source) into items.

    -> {item_type: {'pieces': {name: piece}, 'channels': [...], 'warnings': [...]}}.
    `given`: optional {piece name: piece dict (V, F, role, part, src)} from a rigid pieces output (body armour only);
    V, F, ... are then ignored."""
    warnings = []
    if given:
        if module != 'body_armor':
            raise ValueError('rigid pieces outputs exist for body_armor only')
        out = {'body_armor': {n: {**p, 'role': resolve_uncertain(p['role'], face_pairs(p['F']))} for n, p in given.items()}}
    else:
        pairs = face_pairs(F)
        role = resolve_uncertain(role, pairs)
        area = tri_areas(V, F)
        if module == 'outfit':
            out = {'outfit': {'outfit': cut_piece(V, F, np.ones(len(F), bool), role=role, part=part, src=src)}}
        elif module == 'body_armor':
            piece = zone_pieces(part, role, area, pairs)
            out = {'body_armor': {n: cut_piece(V, F, piece == n, role=role, part=part, src=src) for n in np.unique(piece)}}
        elif module == 'right_gauntlet_boot':
            out = {}
            gaunt, boot = limb_pieces(part, pairs)
            for item_type, names in zip(('gauntlets', 'boots'), (gaunt, boot)):
                pieces = {n: cut_piece(V, F, names == n, role=role, part=part, src=src) for n in RIGHT_PIECES[item_type]
                          if (names == n).any()}
                if not pieces:
                    warnings.append(f'{item_type}: no faces on the anatomical right')
                    continue
                for n in list(pieces):
                    pieces[swap_side(n)] = mirror_piece(pieces[n], plane_x)
                out[item_type] = pieces
            dropped = int(((gaunt == '') & (boot == '')).sum())
            if dropped:
                warnings.append(f'{dropped} faces outside the gauntlet / boot body parts were dropped')
        else:
            raise ValueError(f'unknown module {module}')
    result = {}
    for item_type, pieces in out.items():
        pieces = {n: p for n, p in pieces.items() if len(p['F'])}
        pieces, channels = assemble(item_type, pieces)
        result[item_type] = {'pieces': pieces, 'channels': channels, 'warnings': list(warnings)}
    return result
