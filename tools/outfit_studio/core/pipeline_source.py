"""Pipeline outputs -> item geometry (SPEC section 3, no bpy): discovery, GLB reading, per-face data, module -> items.

Face-level inputs are numpy arrays in the base world frame (see transform.py); every piece comes out as its own
mesh {V, F, role, part, src, channel} with compacted vertices, so rigid pieces never share a vertex."""
import json
import os
import re
import shutil
import struct
import subprocess
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


USE_FITTED = True     # global opt-out for the body-fitted wearables / pieces made by tools/tripo_pipeline/body_fit.py


def _fresh(fitted_path, source_path):
    """True when `fitted_path` exists and is not older than the unfitted file it was made from."""
    try:
        return fitted_path.is_file() and fitted_path.stat().st_mtime >= source_path.stat().st_mtime
    except OSError:
        return False


def fitted_wearable_path(entry, lod):
    """`lod_error_<lv>__wearable_fitted.glb` (same native space and face order as the unfitted wearable, only the
    vertex positions differ) when it exists and is at least as new as the unfitted wearable, else None."""
    reduced = Path(entry['reduced'])
    fitted = reduced / f'lod_error_{lod}__wearable_fitted.glb'
    return fitted if _fresh(fitted, reduced / f'lod_error_{lod}__wearable.glb') else None


def wearable_path(entry, lod, fitted=True):
    """The wearable GLB to import: the fresh fitted one when present (see fitted_wearable_path), else the unfitted
    one. `fitted=False` (or USE_FITTED = False) always gives the unfitted file."""
    return (fitted and USE_FITTED and fitted_wearable_path(entry, lod)) or Path(entry['reduced']) / f'lod_error_{lod}__wearable.glb'


def is_fitted(path):
    """Whether a path from wearable_path, a pieces_output value or a pieces folder is body-fitted."""
    path = Path(path)
    return path.stem.endswith('__wearable_fitted') or path.name.endswith('_fitted') or path.parent.name.endswith('_fitted')


def labelled_path(entry, lod):
    return Path(entry['reduced']) / f'lod_error_{lod}.glb'


def pieces_output(entry, lod, fitted=True):
    """{piece: glb path} of the segment-first / split_pieces rigid output for this LOD, else None. Only a split with
    chest or pelvis counts (a pauldron-only split does not describe the whole armour). The sibling folder
    `lod_<lv>_fitted/` (body_fit.py) replaces it when every unfitted piece has a fitted twin that is at least as new;
    `fitted=False` (or USE_FITTED = False) always gives the unfitted pieces."""
    base = Path(entry['dir'])
    for sub in ('segment_first/pieces', 'pieces'):
        d = base / sub
        if (d / 'pieces.json').is_file() and (d / f'lod_{lod}').is_dir():
            glbs = {p.stem: p for p in sorted((d / f'lod_{lod}').glob('*.glb'))}
            if 'chest' in glbs or 'pelvis' in glbs:
                if fitted and USE_FITTED:
                    twins = {n: d / f'lod_{lod}_fitted' / p.name for n, p in glbs.items()}
                    if all(_fresh(t, glbs[n]) for n, t in twins.items()):
                        return twins
                return glbs
    return None


# ------------------------------------------------------------------------------------------------- input mesh (fit lab)
# New game standard (2026-11-20): the mesh that is imported is the fit lab's choice for the module (preview viewer ->
# `<module>/fit_choice.json`), else the lab's default combo, else the aligned (fitted) wearable. All of them share the
# topology, face order and native frame of `lod_error_<lv>__wearable_aligned.glb`, so the per-face data and
# transform.native_to_world apply unchanged. The body sections (`experiments/body_mask_20261104/out/...`) say which
# parts of the base body each item hides.

FITLAB_LOD = '0p4'                                   # the fit lab only exists for this level
FITLAB_DIR = 'fitlab/lod_0p4'
FITLAB_DEFAULT = {'strategy': 'sections_fill', 'amount': 100, 'sym': 'off', 'scope': 'none'}     # fit_lab.DEFAULT_CHOICE
CHOICE_FILE = 'fit_choice.json'
LOD_CHOICE_FILE = 'chosen_lod.json'
BAKED_NAME = f'lod_error_{FITLAB_LOD}__wearable_fitlab.glb'
SKIN_MATERIAL = 'skin'
ITEM_SECTION_PARTS = {'gauntlets': ('arm_stretch', 'forearm_stretch', 'hand'),     # the sections an item of a
                      'boots': ('thigh_stretch', 'leg_stretch', 'foot')}           # right_gauntlet_boot module can affect
_SECTION_KEY = re.compile(r'^([^#]+)#(\d+)$')
_LR_NODE = re.compile(r'_([lr])_(\d+)$')


class SuspectModule(ValueError):
    """The fit lab flagged the module's source garment as broken (`suspect: true`): it is skipped, not exported."""


def _paths():
    from .. import paths
    return paths


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return default


def stamp(path):
    """[mtime_ns, size] of a file (the fit lab's input stamp), None when it is missing."""
    try:
        st = Path(path).stat()
        return [st.st_mtime_ns, st.st_size]
    except OSError:
        return None


def _mtime(path):
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def _at_least_as_new(path, *others):
    """`path` exists and is not older than any of the existing `others`."""
    m = _mtime(path)
    return m is not None and all(m >= o for o in (_mtime(x) for x in others) if o is not None)


def model_approved(entry, models=None):
    """The set was approved in the preview's Models tab (`models/<set>/model_approval.json` {"approved": true})."""
    root = Path(models) if models else _paths().MODELS
    return (read_json(root / entry.get('source_set', entry['set_id']) / 'model_approval.json') or {}).get('approved') is True


def chosen_lod(entry):
    """The LOD picked in the preview viewer (`<module>/chosen_lod.json`), if it is one of LODS."""
    lod = (read_json(Path(entry['dir']) / LOD_CHOICE_FILE) or {}).get('lod')
    return lod if lod in LODS else None


def resolve_lod(entry, lod='auto'):
    """`lod` as given, or ('auto' / empty) the preview's choice, else the default level; must be a converted level."""
    lod = lod if lod not in (None, '', 'auto') else (chosen_lod(entry) or LODS[0])
    if lod not in entry['levels']:
        raise ValueError(f"LOD {lod} is not converted for {entry['set_id']}/{entry['race']}/{entry['module']} (has {entry['levels']})")
    return lod


def fitlab_index(entry):
    return read_json(Path(entry['dir']) / FITLAB_DIR / 'index.json', {})


def suspect_reasons(entry):
    """List of the fit lab's reasons when the module is flagged suspect, else []."""
    idx = fitlab_index(entry)
    return list(idx.get('suspect_reasons') or ['suspect']) if idx.get('suspect') is True else []


def _fitlab_combo_fresh(entry, combo, aligned):
    """The cached fit lab combo `<combo>.glb` exists, was made from this aligned wearable (index input stamp) and is not
    older than the aligned wearable or the body sections record. (`code_version` is not compared: that needs fit_lab.)"""
    mod = Path(entry['dir'])
    f = mod / FITLAB_DIR / f'{combo}.glb'
    rec = (fitlab_index(entry).get('combos') or {}).get(combo) or {}
    if not f.is_file() or (rec.get('inputs') or {}).get('aligned') != stamp(aligned):
        return False
    return _at_least_as_new(f, aligned, sections_paths(entry)['json'])


def source_choice(entry, lod):
    """Which mesh to import for (entry, lod): {'kind', 'path', 'choice', 'needs_bake', 'note'}.

    kinds, best first: `fitlab_choice` (`fit_choice.json` baked by fit_symmap.py --bake, level 0p4 only), `fitlab_default`
    (the lab's `sections_fill__off__none` combo, 0p4, when fresh), `aligned_fitted` (`lod_error_<lv>__wearable_aligned_fitted.glb`,
    any level), `aligned`, `legacy` (the old unaligned / body_fitted wearable; the alignment was never run)."""
    mod = Path(entry['dir'])
    red = Path(entry['reduced'])
    aligned = red / f'lod_error_{lod}__wearable_aligned.glb'
    if lod == FITLAB_LOD and aligned.is_file():
        choice = read_json(mod / CHOICE_FILE)
        if choice:
            baked = red / BAKED_NAME
            strategy, sym = choice.get('strategy', 'original'), choice.get('sym', 'off')
            deps = [mod / CHOICE_FILE, aligned]
            if strategy != 'original' and float(choice.get('amount', 100)) > 0:
                deps.append(mod / FITLAB_DIR / f'{strategy}__off__none.glb')
            if sym != 'off':
                deps.append(mod / FITLAB_DIR / 'symmap.json')
            return {'kind': 'fitlab_choice', 'path': baked, 'needs_bake': not _at_least_as_new(baked, *deps),
                    'choice': {k: choice[k] for k in ('strategy', 'amount', 'sym', 'scope') if k in choice}, 'note': ''}
        combo = '{strategy}__{sym}__{scope}'.format(**FITLAB_DEFAULT)
        if _fitlab_combo_fresh(entry, combo, aligned):
            return {'kind': 'fitlab_default', 'path': mod / FITLAB_DIR / f'{combo}.glb', 'needs_bake': False,
                    'choice': dict(FITLAB_DEFAULT), 'note': ''}
    note = '' if lod == FITLAB_LOD else f'LOD {lod}: the fit lab only covers {FITLAB_LOD}'
    fitted = red / f'lod_error_{lod}__wearable_aligned_fitted.glb'
    if _fresh(fitted, aligned):
        return {'kind': 'aligned_fitted', 'path': fitted, 'needs_bake': False, 'choice': None, 'note': note}
    if aligned.is_file():
        return {'kind': 'aligned', 'path': aligned, 'needs_bake': False, 'choice': None,
                'note': (note + '; ' if note else '') + 'no fresh aligned_fitted wearable'}
    return {'kind': 'legacy', 'path': wearable_path(entry, lod), 'needs_bake': False, 'choice': None,
            'note': 'no aligned wearable (align_body.py was not run): using the unaligned wearable'}


def tools_python():
    """The numpy + scipy interpreter of the pipeline tools (tripo_pipeline README "numpy + scipy interpreter")."""
    root = _paths().ROOT
    cfg = read_json(root / 'tools' / 'tripo_pipeline' / 'config.json', {}) or {}
    local = os.environ.get('LOCALAPPDATA', '')
    cands = [os.environ.get('OUTFIT_STUDIO_PYTHON'), os.environ.get('SEGMENT_FIRST_PYTHON'),
             (cfg.get('segment_first') or {}).get('venv_python'),
             root.parent / '.venv' / ('Scripts' if os.name == 'nt' else 'bin') / ('python.exe' if os.name == 'nt' else 'python'),
             Path(local) / 'Programs' / 'Python' / 'Python310' / 'python.exe' if local else None, shutil.which('python')]
    for c in cands:
        if c and Path(c).is_file():
            return str(c)
    raise FileNotFoundError('no numpy + scipy python for fit_symmap.py: set $OUTFIT_STUDIO_PYTHON (or $SEGMENT_FIRST_PYTHON)')


def bake_fitlab(entry, python=None, log=print):
    """Run `fit_symmap.py <module> --bake` (applies `fit_choice.json`) -> path of `reduced/lod_error_0p4__wearable_fitlab.glb`."""
    exe = python or tools_python()
    cmd = [exe, str(_paths().ROOT / 'tools' / 'tripo_pipeline' / 'fit_symmap.py'), str(entry['dir']), '--bake']
    log('[pipeline_source] ' + ' '.join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    out = Path(entry['reduced']) / BAKED_NAME
    if r.returncode or not out.is_file():
        raise RuntimeError(f"fit_symmap --bake failed for {entry['dir']} (exit {r.returncode}): {(r.stderr or r.stdout).strip()[-600:]}")
    return out


def resolve_source(entry, lod, bake=True, log=print):
    """source_choice with the fit-lab bake done when needed (`bake=False`: not run, the choice says `needs_bake`).
    Raises when the chosen file does not exist."""
    src = source_choice(entry, lod)
    if src['needs_bake'] and bake:
        src['path'] = bake_fitlab(entry, log=log)
        src['needs_bake'] = False
    if not Path(src['path']).is_file():
        raise FileNotFoundError(f"{src['path']} does not exist" + (' (run fit_symmap.py --bake)' if src['needs_bake'] else ''))
    return src


# ------------------------------------------------------------------------------------------------- body sections

def sections_paths(entry):
    """Folder / record / connectors file of the module's body sections (fit_lab.sections_dir: the job keyed folder, or
    the old job-less folder when its record belongs to this job)."""
    out = _paths().ROOT / 'experiments' / 'body_mask_20261104' / 'out'
    s, j, r, m = entry.get('source_set', entry['set_id']), entry['job_id'], entry['race'], entry['module']
    new = out / f'{s}__{j}__{r}__{m}'
    d = new
    if not (new / 'body_sections.json').is_file():
        old = out / f'{s}__{r}__{m}'
        rec = read_json(old / 'body_sections.json') or {}
        if f'/{j}/' in f'/{rec.get("module", "")}/':
            d = old
    return {'dir': d, 'json': d / 'body_sections.json', 'connectors': d / 'connectors.glb'}


def parse_section_key(key):
    """`thigh_stretch.l#1` -> ('thigh_stretch', 'l', 1); `neck.x#0` -> ('neck', 'x', 0)."""
    m = _SECTION_KEY.match(key)
    if not m:
        raise ValueError(f'bad section key {key!r}')
    name, side = (m.group(1).rsplit('.', 1) + ['x'])[:2]
    return name, side, int(m.group(2))


def swap_node_side(name):
    """`conn_thigh_stretch_l_1` -> `conn_thigh_stretch_r_1` (and back); other names are unchanged."""
    return _LR_NODE.sub(lambda m: f"_{'r' if m.group(1) == 'l' else 'l'}_{m.group(2)}", name)


def sections_record(entry, aligned=None):
    """The body sections record of the module, checked like fit_lab.sections_status: -> (record dict | None, warnings).
    Sections of the record: {key: {state show|hide|connector, base_node, connector_node, part, side, index}}."""
    P = sections_paths(entry)
    rec = read_json(P['json'])
    if rec is None:
        return None, [f"no body sections record ({P['json']})"]
    if rec.get('states_only'):
        return None, ['body sections record is states-only']
    if f'/{entry["job_id"]}/' not in f'/{rec.get("module", "")}/':
        return None, [f"body sections record belongs to another job ({rec.get('module')})"]
    warnings, secs = [], {}
    for key, s in (rec.get('sections') or {}).items():
        if not isinstance(s, dict) or not s.get('base_node'):
            continue
        part, side, index = parse_section_key(key)
        secs[key] = {'state': s['state'], 'base_node': s['base_node'], 'connector_node': s.get('connector_node'),
                     'part': part, 'side': side, 'index': index}
    if not secs:
        return None, ['body sections record has no sections']
    need = [s for s in secs.values() if s['state'] == 'connector']
    conn = P['connectors']
    if need and not conn.is_file():
        return None, [f'{len(need)} connector sections but {conn} is missing']
    a, r = _mtime(aligned) if aligned else None, _mtime(P['json'])
    if a and r and a > r:
        warnings.append('the aligned wearable is newer than the body sections record (re-run the fit lab sections)')
    return {'dir': str(P['dir']), 'json': str(P['json']), 'connectors_glb': str(conn) if conn.is_file() else None,
            'layout': rec.get('layout'), 'sections': secs}, warnings


def item_sections(rec, item_type, symmap=None, sym='off', scope='none'):
    """(states {base node: show|hide|connector}, connectors {connector node: {section, mirror_of}}) of one item.

    Every section of the base is listed. An item of a right_gauntlet_boot module only affects the sections of its limb
    parts (ITEM_SECTION_PARTS); the rest is `show`. With a left/right symmetry choice the target side's states come from the
    symmetry map (`symmap.json` `sections.<sym>.<scope>`): connectors of the source side are then also placed mirrored
    (`mirror_of` = the connector node whose geometry is reflected)."""
    limb = ITEM_SECTION_PARTS.get(item_type)
    secs = rec['sections']
    mapped = ((symmap or {}).get('sections') or {}).get(sym, {}).get(scope) if sym != 'off' else None
    states = {}
    for s in secs.values():
        state = mapped['states'].get(s['base_node'], s['state']) if mapped else s['state']
        states[s['base_node']] = state if limb is None or s['part'] in limb else 'show'
    conns = {}
    if mapped:
        for node, src in [(n, None) for n in mapped.get('connectors', [])] + [(swap_node_side(n), n) for n in mapped.get('mirrored', [])]:
            sec = 'sec_' + node.removeprefix('conn_')
            if states.get(sec) == 'connector':
                conns[node] = {'section': sec, 'mirror_of': src}
    else:
        conns = {s['connector_node']: {'section': s['base_node'], 'mirror_of': None} for s in secs.values()
                 if states[s['base_node']] == 'connector' and s['connector_node']}
    return states, conns


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


def _load_glb(path):
    """(JSON document, binary chunk) of a GLB file."""
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
    return js, blob


def _prim_triangles(js, blob, prim, mat4, path):
    """(positions in glTF space after the node matrix, triangle indices) of one primitive."""
    if prim.get('mode', 4) != 4:
        raise ValueError(f'{path}: only triangle primitives are supported')
    pos = _accessor(js, blob, prim['attributes']['POSITION']).astype(np.float64)
    pos = pos @ mat4[:3, :3].T + mat4[:3, 3]
    idx = (_accessor(js, blob, prim['indices']).astype(np.int64).ravel() if 'indices' in prim else np.arange(len(pos)))
    return pos, idx.reshape(-1, 3)


def _to_blender(v):
    return np.stack([v[:, 0], -v[:, 2], v[:, 1]], 1)


def read_glb(path):
    """Triangles of a GLB in Blender coordinates (what the glTF importer gives: (x, y, z) -> (x, -z, y)).
    {'v' (n,3), 'f' (m,3), 'face_material' (m,), 'materials' [role names]}; primitives in file order, which is the
    face order the pipeline's `*_glb_order` arrays refer to."""
    js, blob = _load_glb(path)
    node_of = {n['mesh']: _node_matrix(n) for n in js.get('nodes', []) if 'mesh' in n}
    materials = [role_name(m.get('name', f'material{i}')) for i, m in enumerate(js.get('materials', []))] or ['primary']
    vs, fs, ms, base = [], [], [], 0
    for mi, mesh in enumerate(js['meshes']):
        mat4 = node_of.get(mi, np.eye(4))
        for prim in mesh['primitives']:
            pos, tri = _prim_triangles(js, blob, prim, mat4, path)
            vs.append(pos)
            fs.append(tri + base)
            ms.append(np.full(len(tri), prim.get('material', 0), np.int64))
            base += len(pos)
    v = np.concatenate(vs) if vs else np.zeros((0, 3))
    return {'v': _to_blender(v), 'f': np.concatenate(fs) if fs else np.zeros((0, 3), np.int64),
            'face_material': np.concatenate(ms) if ms else np.zeros(0, np.int64), 'materials': materials}


def read_glb_nodes(path):
    """{node name: {'v' (n,3) Blender coordinates, 'f' (m,3)}} of every mesh node of a GLB (the fit lab's `connectors.glb`)."""
    js, blob = _load_glb(path)
    out = {}
    for n in js.get('nodes', []):
        if 'mesh' not in n:
            continue
        vs, fs, base = [], [], 0
        for prim in js['meshes'][n['mesh']]['primitives']:
            pos, tri = _prim_triangles(js, blob, prim, _node_matrix(n), path)
            vs.append(pos)
            fs.append(tri + base)
            base += len(pos)
        if vs:
            out[n.get('name', f"mesh{n['mesh']}")] = {'v': _to_blender(np.concatenate(vs)), 'f': np.concatenate(fs)}
    return out


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


def limb_pieces(part, pairs, side='r'):
    """Gauntlet and boot piece names per face of one side ('' = not part of that item): hand / forearm (+ the arm
    faces touching them) and foot / lower leg (+ the thigh faces touching them), split at wrist and ankle.
    Names are `cuff_<side>`, `hand_<side>`, `shin_<side>`, `foot_<side>`."""
    n = len(part)
    gaunt, boot = np.full(n, '', '<U8'), np.full(n, '', '<U8')
    hand, fore = part == f'hand.{side}', part == f'forearm_stretch.{side}'
    foot, shin = part == f'foot.{side}', part == f'leg_stretch.{side}'
    fore = grow(fore | hand, part == f'arm_stretch.{side}', pairs) & ~hand
    shin = grow(shin | foot, part == f'thigh_stretch.{side}', pairs) & ~foot
    gaunt[fore], gaunt[hand] = f'cuff_{side}', f'hand_{side}'
    boot[shin], boot[foot] = f'shin_{side}', f'foot_{side}'
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


def make_items(module, V, F, role, part, src, plane_x=0., given=None, mirror=False):
    """Split a world-space wearable (welded V, F; per-face role / part names / label source) into items.

    -> {item_type: {'pieces': {name: piece}, 'channels': [...], 'warnings': [...], 'mirrored': [piece names]}}.
    `given`: optional {piece name: piece dict (V, F, role, part, src)} from a rigid pieces output (body armour only);
    V, F, ... are then ignored.
    `mirror`: right_gauntlet_boot only. False (the default since the fit lab, 2026-11-20): the wearable already has both sides
    (the pipeline mirrors the right piece onto the left, and the fit lab then fits each side), so both are taken as they
    are; a right piece without a left twin is still mirrored. True: the left pieces are always the mirrored right ones."""
    warnings, mirrored = [], {}
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
            sides = 'r' if mirror else 'rl'
            by_side = {s: limb_pieces(part, pairs, s) for s in sides}
            for k, item_type in enumerate(('gauntlets', 'boots')):
                pieces = {}
                for n in RIGHT_PIECES[item_type]:
                    for side in sides:
                        name = n[:-1] + side
                        if (by_side[side][k] == name).any():
                            pieces[name] = cut_piece(V, F, by_side[side][k] == name, role=role, part=part, src=src)
                for n in RIGHT_PIECES[item_type]:
                    if n in pieces and swap_side(n) not in pieces:        # no left twin in the wearable (or `mirror`): reflect
                        pieces[swap_side(n)] = mirror_piece(pieces[n], plane_x)
                        mirrored.setdefault(item_type, []).append(swap_side(n))
                if not pieces:
                    warnings.append(f'{item_type}: no faces on the anatomical right')
                    continue
                out[item_type] = pieces
            used = np.zeros(len(F), bool)
            for gaunt, boot in by_side.values():
                used |= (gaunt != '') | (boot != '')
            dropped = int((~used).sum())
            if dropped:
                warnings.append(f'{dropped} faces outside the gauntlet / boot body parts were dropped')
        else:
            raise ValueError(f'unknown module {module}')
    result = {}
    for item_type, pieces in out.items():
        pieces = {n: p for n, p in pieces.items() if len(p['F'])}
        pieces, channels = assemble(item_type, pieces)
        result[item_type] = {'pieces': pieces, 'channels': channels, 'warnings': list(warnings),
                             'mirrored': mirrored.get(item_type, [])}
    return result
