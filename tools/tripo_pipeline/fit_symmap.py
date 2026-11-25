"""Symmetry map for the fit lab (2026-11-19): symmetry is applied at RUNTIME by mirroring both endpoints of the viewer's Amount slider (the unfitted aligned mesh and
the symmetry-off fitted mesh of the chosen strategy) with a small precomputed per-module map, so only the three symmetry-off fits per module have to be cached.

    python tools/tripo_pipeline/fit_symmap.py <module_dir> [<module_dir> ...] | --all [--match S] [--jobs N] [--force]
    python tools/tripo_pipeline/fit_symmap.py <module_dir> --bake [--out FILE] [--choice strategy,amount,sym,scope]

<module_dir> = models/<set>/<job-id>/<race>/<module>. Input: `reduced/lod_error_0p4__wearable_aligned.glb` (the same welded vertices and GLB vertex order the fit lab GLBs
use). Output: `<module>/fitlab/lod_0p4/symmap.bin` + `symmap.json` (json = layout of the binary arrays, mirror plane, race, input stamps, version, the section states;
rebuilt when the version or an input changed).

The symmetry semantics are fit_lab.symmetrize's (same mirror plane per race, +x = anatomical left, same limb definition and `sym_ramp_pct` ramps; fit_lab is only
imported, never changed): per welded vertex a side (0 mid, 1 left, 2 right), and per direction `l2r` / `r2l` for every target-side vertex the weight for scope `limbs`
(limb membership x ramp from the limb boundary along the garment surface) and for scope `whole` (ramp from the mid-plane band), plus the mirror correspondence = a source
triangle (3 welded vertex ids) and the barycentric coordinates of the closest point on the REFLECTED source surface (source triangles = centroid on the source side), found
on the ALIGNED (unfitted) mesh. At runtime, for any mesh with this topology (original or fitted), a target vertex moves to

    p' = lerp(p, R(sum b_i * q_src_i), w)           (p, q in the viewer's scene space = node matrix applied to the GLB position, R = reflection in that space)

and a vertex with weight 0 keeps its exact GLB position. The same math is `SymMap.mirror` here (used by `--bake`) and was `tools/preview/fitsym.js` `mirror` (the old site's viewer, removed 2026-11-25 with the fit lab UI).
Welded: the map is per welded vertex plus `wid` (GLB vertex -> welded vertex), so duplicated seam vertices move together.

Sections (`symmap.json` `sections.<l2r|r2l>.<limbs|whole>`): the body sections' states with the source side's `.l`/`.r` states copied onto the other side (limbs: only the
limb parts; whole: every sided part): `states` (base node -> state), `hide` (base nodes to leave out), `connectors` (connector nodes to show as they are) and `mirrored`
(connector nodes of the source side to show reflected across the same plane in the viewer, for the target side's connector sections).

`--bake` applies the module's fit_choice.json (strategy, amount, sym, scope) with exactly the viewer's math and writes `reduced/lod_error_0p4__wearable_fitlab.glb`
(topology preserved; positions blended between the two endpoints by amount, normals: file normals blended for symmetry off, the first-order update of body_fit's
recomputed_normals for a symmetric result).
"""
import os

for _k in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(_k, '1')
import argparse
import datetime
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import numpy as np   # noqa: E402
from scipy.spatial import cKDTree   # noqa: E402

import body_fit as bf   # noqa: E402
import fit_lab as fl   # noqa: E402
from rewear import discover, module_busy   # noqa: E402

VERSION = 'symmap-20261119a'
LV = fl.LV
EPS = 1e-3                       # the mid-plane band (m), as fit_lab.symmetrize
DIRS = ('l2r', 'r2l')
SCOPES = ('limbs', 'whole')
SEC_RE = re.compile(r'^(.+)\.([lr])#(\d+)$')
MAP_NAME = 'symmap'
CHOICE_FILE = 'fit_choice.json'
BAKE_NAME = f'lod_error_{LV}__wearable_fitlab.glb'


# ----------------------------------------------------------------------------------------------- paths / freshness

def map_paths(module_dir):
    P = fl.module_paths(module_dir)
    P['map_json'] = P['dir'] / f'{MAP_NAME}.json'
    P['map_bin'] = P['dir'] / f'{MAP_NAME}.bin'
    return P


def params():
    return {'sym_ramp_pct': fl.DEFAULTS['sym_ramp_pct'], 'eps_m': EPS, 'limb_parts': list(fl.LIMB_PARTS)}


def input_stamps(P):
    return {'aligned': fl.stamp(P['aligned']), 'manifest': fl.stamp(P['manifest']), 'base': fl.stamp(bf.BASE_CACHE / f'{P["race"]}.npz'), 'sections': fl.stamp(P['sec_json'])}


def read_doc(P):
    try:
        return json.loads(P['map_json'].read_text())
    except (OSError, ValueError):
        return None


def is_fresh(module_dir):
    """The module's symmap exists and matches the version, params and input stamps."""
    P = map_paths(module_dir)
    d = read_doc(P)
    if not d or not P['map_bin'].is_file() or d.get('version') != VERSION or d.get('params') != params():
        return False
    if P['map_bin'].stat().st_size != d.get('bin_bytes'):
        return False
    return d.get('inputs') == json.loads(json.dumps(input_stamps(P)))


# ----------------------------------------------------------------------------------------------- map building

def closest_on_tris(P, A, B, C, budget=1_500_000):
    """Index of the closest triangle (A, B, C) and the barycentric coordinates of the closest point, for every point of P. Exact: the nearest few centroids give an upper
    bound of the distance, every triangle whose centroid lies within that bound + the largest triangle radius is then tested."""
    n = len(P)
    cen = (A + B + C) / 3
    rad = np.max([np.linalg.norm(X - cen, axis=1) for X in (A, B, C)], axis=0)
    rmax = float(rad.max())
    tree = cKDTree(cen)
    kq = min(6, len(A))
    _, nn = tree.query(P, k=kq)
    nn = nn.reshape(n, -1)
    ub = np.full(n, np.inf)
    for j in range(nn.shape[1]):
        t = nn[:, j]
        bb = bf.tri_closest(P, A[t], B[t], C[t])
        q = bb[:, :1] * A[t] + bb[:, 1:2] * B[t] + bb[:, 2:] * C[t]
        ub = np.minimum(ub, np.linalg.norm(P - q, axis=1))
    cand = tree.query_ball_point(P, ub + rmax + 1e-9)
    best_t, best_b, best_d = np.zeros(n, np.int64), np.zeros((n, 3)), np.full(n, np.inf)
    start = 0
    while start < n:
        stop, tot = start, 0
        while stop < n and (tot + len(cand[stop]) <= budget or stop == start):
            tot += len(cand[stop])
            stop += 1
        pi = np.repeat(np.arange(start, stop), [len(cand[i]) for i in range(start, stop)])
        ti = np.concatenate([np.asarray(cand[i], np.int64) for i in range(start, stop)])
        bb = bf.tri_closest(P[pi], A[ti], B[ti], C[ti])
        q = bb[:, :1] * A[ti] + bb[:, 1:2] * B[ti] + bb[:, 2:] * C[ti]
        d = np.linalg.norm(P[pi] - q, axis=1)
        order = np.lexsort((d, pi))                       # per point, the smallest distance first
        first = np.r_[True, pi[order][1:] != pi[order][:-1]]
        sel = order[first]
        best_t[pi[sel]], best_b[pi[sel]], best_d[pi[sel]] = ti[sel], bb[sel], d[sel]
        start = stop
    return best_t, best_b, best_d


def reflection_gl(surf, x0, n=24):
    """Affine map (3x4, row-major: p' = M[:, :3] @ p + M[:, 3]) of the viewer's scene space (node matrix applied to the GLB position) that is the reflection across the
    game world plane x = x0. The chain native -> world is affine (scale + translation per module), so a least squares fit of sample points is exact; the residual is checked."""
    rng = np.random.default_rng(7)
    lo, hi = surf.Wc.min(0), surf.Wc.max(0)
    W = lo + (hi - lo) * rng.random((32, 3))
    gl = bf.bl_to_gl(bf.tf.world_to_native(W, surf.manifest, surf.fit, surf.body.H, surf.body.o))
    Wr = W.copy()
    Wr[:, 0] = 2 * x0 - Wr[:, 0]
    glr = bf.bl_to_gl(bf.tf.world_to_native(Wr, surf.manifest, surf.fit, surf.body.H, surf.body.o))
    X = np.c_[gl, np.ones(len(gl))]
    M, *_ = np.linalg.lstsq(X, glr, rcond=None)           # (4, 3)
    err = float(np.abs(X @ M - glr).max())
    if err > 1e-6 * max(1., float(np.abs(glr).max())):
        raise ValueError(f'the native -> world chain is not affine here (residual {err:g})')
    return M.T.copy(), err


def build_sections(P):
    """Body section states with the source side copied onto the target side, per direction and scope. {} when the module has no usable record."""
    try:
        rec = json.loads(P['sec_json'].read_text())
    except (OSError, ValueError):
        return {}
    if rec.get('states_only') or f'/{P["job"]}/' not in f'/{rec.get("module", "")}/':
        return {}
    secs = {k: v for k, v in (rec.get('sections') or {}).items() if isinstance(v, dict) and v.get('base_node')}
    out = {}
    for sym in DIRS:
        src_side, tgt_side = ('l', 'r') if sym == 'l2r' else ('r', 'l')
        out[sym] = {}
        for scope in SCOPES:
            state = {k: v['state'] for k, v in secs.items()}
            keep_conn = {k: v.get('connector_node') for k, v in secs.items()}
            mirrored = []
            for key in secs:
                m = SEC_RE.match(key)
                if not m or m.group(2) != tgt_side or (scope == 'limbs' and m.group(1) not in fl.LIMB_PARTS):
                    continue
                s = secs.get(f'{m.group(1)}.{src_side}#{m.group(3)}')
                if s is None:
                    continue
                state[key], keep_conn[key] = s['state'], None
                if s['state'] == 'connector' and s.get('connector_node'):
                    mirrored.append(s['connector_node'])
            out[sym][scope] = {'states': {secs[k]['base_node']: state[k] for k in secs},
                               'hide': sorted(secs[k]['base_node'] for k in secs if state[k] in ('hide', 'connector')),
                               'connectors': sorted(c for k, c in keep_conn.items() if c and state[k] == 'connector'),
                               'mirrored': sorted(mirrored)}
    return out


def build(module_dir, log=print):
    """(doc, blob) of the module's symmetry map."""
    t0 = time.monotonic()
    P = map_paths(module_dir)
    if not P['aligned'].is_file():
        raise FileNotFoundError(f'no aligned 0p4 wearable: {P["aligned"]}')
    manifest = json.loads(P['manifest'].read_text(encoding='utf-8-sig'))
    body = bf.get_body(P['race'])
    glb = bf.Glb(P['aligned'])
    surf = bf.Surface(glb, manifest, body)
    U, K = surf.U, surf.K
    x0, left, err = fl.mirror_plane(P['race'], body)
    ramp = fl.DEFAULTS['sym_ramp_pct'] / 100 * body.H
    sd = (U[:, 0] - x0) * left
    side = np.where(sd > EPS, 1, np.where(sd < -EPS, 2, 0)).astype(np.int8)
    part = body.measure(U)['part']
    limb = np.array([n.split('.')[0] in fl.LIMB_PARTS for n in body.part_names])[part]
    arrays = {'wid': surf.wid.astype(np.int32), 'side': side}
    R = U.copy()
    R[:, 0] = 2 * x0 - R[:, 0]
    tri_c = U[surf.Tw].mean(1)
    stats = {}
    for sym in DIRS:
        src = left if sym == 'l2r' else -left
        target = (U[:, 0] - x0) * src < -EPS
        in_limbs = target & limb
        w_limbs = np.where(in_limbs, np.clip(fl.graph_distance(surf, np.flatnonzero(~in_limbs)) / ramp, 0., 1.), 0.)
        w_whole = np.where(target, np.clip(np.abs(U[:, 0] - x0) / ramp, 0., 1.), 0.)
        src_tris = (tri_c[:, 0] - x0) * src > 0
        T = surf.Tw[src_tris]
        idx = np.flatnonzero(target & ((w_limbs > 0) | (w_whole > 0)))
        if len(idx) and len(T):
            tj, bary, dist = closest_on_tris(U[idx], R[T[:, 0]], R[T[:, 1]], R[T[:, 2]])
            tri = T[tj]
        else:
            idx, tri, bary, dist = idx[:0], np.zeros((0, 3), np.int64), np.zeros((0, 3)), np.zeros(0)
        arrays[f'{sym}_idx'] = idx.astype(np.int32)
        arrays[f'{sym}_tri'] = tri.astype(np.int32)
        arrays[f'{sym}_bary'] = bary.astype(np.float32)
        arrays[f'{sym}_w_limbs'] = w_limbs[idx].astype(np.float32)
        arrays[f'{sym}_w_whole'] = w_whole[idx].astype(np.float32)
        stats[sym] = {'targets': int(len(idx)), 'limb_targets': int((w_limbs[idx] > 0).sum()), 'mean_match_mm': float(dist.mean() * 1000) if len(dist) else 0.,
                      'max_match_mm': float(dist.max() * 1000) if len(dist) else 0., 'source_triangles': int(src_tris.sum())}
    refl, rerr = reflection_gl(surf, x0)
    prims = []
    for k, p in enumerate(glb.prims):
        M = p['matrix']
        prims.append({'name': p['name'], 'offset': int(surf.offsets[k]), 'count': int(len(glb.positions(k))), 'to_scene': M[:3].tolist(), 'from_scene': np.linalg.inv(M)[:3].tolist()})
    layout, chunks, off = {}, [], 0
    for name, a in arrays.items():
        a = np.ascontiguousarray(a)
        raw = a.tobytes()
        raw += b'\0' * (-len(raw) % 4)
        layout[name] = {'offset': off, 'dtype': {'int32': 'i32', 'float32': 'f32', 'int8': 'i8'}[a.dtype.name], 'count': int(a.size), 'shape': list(a.shape)}
        chunks.append(raw)
        off += len(raw)
    blob = b''.join(chunks)
    doc = {'version': VERSION, 'module': P['rel'], 'race': P['race'], 'lod': LV, 'params': params(), 'inputs': json.loads(json.dumps(input_stamps(P))),
           'plane': {'x0_m': float(x0), 'left_sign': float(left), 'asymmetry_mean_mm': float(err * 1000), 'axis': 'x', 'space': 'game world (glTF x = Blender x)'},
           'reflection': refl.tolist(), 'reflection_residual': rerr, 'welded_vertices': int(K), 'glb_vertices': int(len(surf.wid)), 'prims': prims,
           'arrays': layout, 'bin_bytes': len(blob), 'sections': build_sections(P), 'stats': stats, 'seconds': round(time.monotonic() - t0, 2),
           'written': datetime.datetime.now().isoformat(timespec='seconds')}
    return doc, blob


def write_map(module_dir, doc, blob):
    P = map_paths(module_dir)
    P['dir'].mkdir(parents=True, exist_ok=True)
    bf.write_atomic(P['map_bin'], blob)             # json last: its presence means a complete pair
    bf.write_json(P['map_json'], doc)


def process_module(module_dir, force=False, log=print):
    P = map_paths(module_dir)
    if not P['aligned'].is_file():
        return {'module': P['rel'], 'status': 'skipped', 'why': 'no aligned 0p4 wearable'}
    if not (bf.BASE_CACHE / f'{P["race"]}.npz').is_file():
        return {'module': P['rel'], 'status': 'skipped', 'why': f'no cached base body for {P["race"]}'}
    if not force and is_fresh(module_dir):
        return {'module': P['rel'], 'status': 'cached'}
    doc, blob = build(module_dir, log)
    write_map(module_dir, doc, blob)
    return {'module': P['rel'], 'status': 'done', 'seconds': doc['seconds'], 'bytes': len(blob) + P['map_json'].stat().st_size, 'sections': bool(doc['sections'])}


# ----------------------------------------------------------------------------------------------- runtime math (was mirrored by tools/preview/fitsym.js, removed 2026-11-25)

class SymMap:
    def __init__(self, doc, blob):
        self.doc = doc
        self.a = {}
        for name, d in doc['arrays'].items():
            dt = {'i32': '<i4', 'f32': '<f4', 'i8': 'i1'}[d['dtype']]
            self.a[name] = np.frombuffer(blob, dtype=dt, count=d['count'], offset=d['offset']).reshape(d['shape'])
        self.wid = self.a['wid'].astype(np.int64)
        self.K = int(doc['welded_vertices'])
        self.n = len(self.wid)
        self.first = np.full(self.K, -1, np.int64)
        self.first[self.wid[::-1]] = np.arange(self.n)[::-1]
        self.prims = doc['prims']
        self.R = np.asarray(doc['reflection'], np.float64)
        self.prim_of = np.zeros(self.n, np.int64)
        for k, p in enumerate(self.prims):
            self.prim_of[p['offset']:p['offset'] + p['count']] = k
        self.to_scene = [np.asarray(p['to_scene'], np.float64) for p in self.prims]
        self.from_scene = [np.asarray(p['from_scene'], np.float64) for p in self.prims]

    @classmethod
    def load(cls, module_dir):
        P = map_paths(module_dir)
        return cls(json.loads(P['map_json'].read_text()), P['map_bin'].read_bytes())

    def scene(self, per_prim):
        """(n, 3) scene-space positions of the concatenated GLB vertices."""
        out = np.zeros((self.n, 3))
        for k, p in enumerate(self.prims):
            M = self.to_scene[k]
            out[p['offset']:p['offset'] + p['count']] = np.asarray(per_prim[k], np.float64) @ M[:, :3].T + M[:, 3]
        return out

    def mirror(self, per_prim, sym, scope):
        """Mirrored copy (float32 per primitive) of positions `per_prim` (list of (n_k, 3) local GLB positions): the target side's in-scope vertices move to
        lerp(p, R(sum b_i q_i), w) (q = the unmodified positions); everything else keeps its exact value."""
        out = [np.asarray(p, np.float32).copy() for p in per_prim]
        if sym == 'off':
            return out
        idx, tri, bary = self.a[f'{sym}_idx'].astype(np.int64), self.a[f'{sym}_tri'].astype(np.int64), self.a[f'{sym}_bary'].astype(np.float64)
        w = self.a[f'{sym}_w_{scope}'].astype(np.float64)
        keep = w > 0
        idx, tri, bary, w = idx[keep], tri[keep], bary[keep], w[keep]
        if not len(idx):
            return out
        S = self.scene(per_prim)
        W = S[self.first]                                                           # welded scene positions
        q = np.einsum('jk,jkc->jc', bary, W[tri])
        q = q @ self.R[:, :3].T + self.R[:, 3]
        Wn = W.copy()
        Wn[idx] = W[idx] + w[:, None] * (q - W[idx])
        moved = np.zeros(self.K, bool)
        moved[idx] = True
        v = np.flatnonzero(moved[self.wid])
        for k, p in enumerate(self.prims):
            vk = v[(v >= p['offset']) & (v < p['offset'] + p['count'])]
            if not len(vk):
                continue
            M = self.from_scene[k]
            out[k][vk - p['offset']] = (Wn[self.wid[vk]] @ M[:, :3].T + M[:, 3]).astype(np.float32)
        return out


def recomputed_normals(glb, new_pos):
    """body_fit.recomputed_normals for a bare GLB (only `.glb` of its surface argument is used)."""
    return bf.recomputed_normals(SimpleNamespace(glb=glb), new_pos)


def blend(glb, fitted_pos, sym_map, sym, scope, amount, fitted_nor=None):
    """(positions, normals) per primitive of the viewer's result for a choice. `fitted_pos` None = strategy `original`."""
    n = len(glb.prims)
    base_pos = [glb.positions(k).astype(np.float32) for k in range(n)]
    base_nor = [glb.normals(k) for k in range(n)]
    t = 0. if fitted_pos is None else amount / 100.
    if sym == 'off':
        pos, nor = [], {}
        for k in range(n):
            a = base_pos[k]
            b = a if fitted_pos is None else np.asarray(fitted_pos[k], np.float32)
            p = a.astype(np.float64) + (b.astype(np.float64) - a) * t if t else a
            pos.append(np.asarray(p, np.float32))
            na, nb = base_nor[k], (fitted_nor[k] if fitted_nor else None)
            if na is not None:
                x = na.astype(np.float32).astype(np.float64)
                if t and nb is not None:
                    x = x + (nb.astype(np.float32) - x) * t
                ln = np.linalg.norm(x, axis=1, keepdims=True)
                nor[k] = x / np.where(ln > 0, ln, 1.)
        return pos, nor
    A = sym_map.mirror(base_pos, sym, scope)
    B = A if fitted_pos is None else sym_map.mirror([np.asarray(p, np.float32) for p in fitted_pos], sym, scope)
    pos = [np.asarray(A[k].astype(np.float64) + (B[k].astype(np.float64) - A[k]) * t if t else A[k], np.float32) for k in range(n)]
    return pos, recomputed_normals(glb, [p.astype(np.float64) for p in pos])


# ----------------------------------------------------------------------------------------------- bake

def read_choice(module_dir):
    try:
        return json.loads((Path(module_dir) / CHOICE_FILE).read_text())
    except (OSError, ValueError):
        return None


def bake(module_dir, out=None, choice=None, log=print):
    """Apply the module's saved choice (or `choice`) and write reduced/lod_error_0p4__wearable_fitlab.glb (or `out`). Returns the output path."""
    P = map_paths(module_dir)
    choice = choice or read_choice(module_dir)
    if not choice:
        raise ValueError(f'no {CHOICE_FILE} in {P["module"]} (and no --choice)')
    strategy, amount, sym, scope = choice['strategy'], float(choice['amount']), choice.get('sym', 'off'), choice.get('scope', 'none')
    fl.parse_combo(f'{strategy}__{sym}__{scope}')
    glb = bf.Glb(P['aligned'])
    fitted_pos = fitted_nor = None
    if strategy != 'original' and amount > 0:
        f = fl.combo_file(P, f'{strategy}__off__none')
        if not f.is_file():
            raise FileNotFoundError(f'the symmetry-off fit {f.name} is not cached; run fit_lab.py --combos {strategy}__off__none')
        g = bf.Glb(f)
        if len(g.prims) != len(glb.prims) or any(len(g.positions(k)) != len(glb.positions(k)) for k in range(len(glb.prims))):
            raise ValueError(f'{f.name}: topology differs from the aligned GLB')
        fitted_pos = [g.positions(k) for k in range(len(g.prims))]
        fitted_nor = [g.normals(k) for k in range(len(g.prims))]
    sym_map = None
    if sym != 'off':
        if not is_fresh(module_dir):
            log('SYMMAP map missing or stale; building')
            process_module(module_dir, force=True, log=log)
        sym_map = SymMap.load(module_dir)
    pos, nor = blend(glb, fitted_pos, sym_map, sym, scope, amount, fitted_nor)
    out = Path(out) if out else P['module'] / 'reduced' / BAKE_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    bf.write_atomic(out, glb.patched({k: p for k, p in enumerate(pos)}, nor))
    return out


# ----------------------------------------------------------------------------------------------- CLI

def _one(work):
    m, force = work
    try:
        return process_module(m, force)
    except Exception as error:   # noqa: BLE001
        import traceback
        traceback.print_exc()
        return {'module': str(m), 'status': 'failed', 'why': f'{type(error).__name__}: {error}'}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('modules', nargs='*')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--match')
    ap.add_argument('--jobs', type=int, default=1)
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--bake', action='store_true', help='apply fit_choice.json and write reduced/lod_error_0p4__wearable_fitlab.glb (one or a few modules; not for --all)')
    ap.add_argument('--out', help='--bake: output file (default reduced/lod_error_0p4__wearable_fitlab.glb)')
    ap.add_argument('--choice', help='--bake: strategy,amount,sym,scope instead of fit_choice.json')
    a = ap.parse_args(argv)
    mods = [Path(m) for m in a.modules]
    if a.all:
        mods += [m for m in discover() if (m / 'reduced' / f'lod_error_{LV}__wearable_aligned.glb').is_file()]
    mods = [m if m.is_absolute() else ROOT / m for m in mods]
    if a.match:
        mods = [m for m in mods if a.match in str(m)]
    if a.all:
        mods = [m for m in mods if not module_busy(m)]
    if not mods:
        raise SystemExit('no modules (give module folders or --all)')
    if a.bake:
        if a.all or (a.out and len(mods) != 1):
            raise SystemExit('--bake: give module folders (not --all); --out needs exactly one')
        choice = None
        if a.choice:
            s, amt, sym, scope = a.choice.split(',')
            choice = {'strategy': s, 'amount': float(amt), 'sym': sym, 'scope': scope}
        for m in mods:
            print('SYMMAP baked', bake(m, a.out, choice), flush=True)
        return
    t0, bad = time.time(), 0
    work = [(m, a.force) for m in mods]
    if a.jobs > 1 and len(work) > 1:
        import concurrent.futures as cf
        with cf.ProcessPoolExecutor(a.jobs) as ex:
            results = ex.map(_one, work)
            results = list(results)
    else:
        results = [_one(w) for w in work]
    for r in results:
        bad += r['status'] == 'failed'
        print(f'SYMMAP {r["status"]:8} {r["module"]} {r.get("seconds", "")} {("%d KB" % (r["bytes"] / 1024)) if "bytes" in r else ""} {r.get("why", "")}'.rstrip(), flush=True)
    print(f'SYMMAP finished {len(results)} modules in {time.time() - t0:.0f} s, {bad} failed', flush=True)
    if bad:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
