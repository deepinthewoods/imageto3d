"""Blender side of the wearable-by-cut step (wearcut.py does the geometry in plain python with scipy).

Used by reduce_model.py (`--wearable-method cut`, the default) after its three reduction levels, and by rewear.py (backfill of
already converted modules, run as a Blender script). In order:

 1. `build_prep`: the cleaned high-poly mesh of the reduction with three texture colours per face (read through the nearest source
    triangle), its body part (face_labels.transfer_parts), the concept-camera visibility cosine and, where the module lets garment be,
    the off-body distance and base-normal agreement (the rules of face_labels.off_body_excess / hem_risers at high-poly level);
 2. `fit_mirror_plane`: the mirror plane (symmetry.fit_plane on the 0.1% level, started from the base's sagittal plane);
 3. `wearcut.py` in the venv python (numpy + scipy) with the reduction's own palette (no second hue fit);
 4. `install`: per level the wearable GLB (export_native, same material slots as the old wearable), the sidecar arrays in the new GLB's
    face order, the manifest records. The previous wearable is kept as `<name>__wearable_faces.glb`, its sidecar arrays as
    `<level>_colors_wearable_faces.npz`.
Any failure leaves the old (`faces`) wearable of that level in place and is recorded in the manifest (`wearable.fallback_reason`).
The full labelled levels and their evidence are never touched.

Rewear (Blender): blender -b --factory-startup -t 3 --python wearcut_blender.py -- rewear <module_dir> [--levels 0p4,0p2,0p1] [--force]
"""
from pathlib import Path
import json
import math
import os
import shutil
import subprocess
import sys
import time
import traceback

import bpy
import numpy as np
from mathutils import Matrix, kdtree
from mathutils.bvhtree import BVHTree

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import portable  # noqa: E402
import face_labels as fl  # noqa: E402
import symmetry as sym  # noqa: E402
import wearcut_rules as R  # noqa: E402
from palette import Palette  # noqa: E402

ALL_LEVELS = ('0p4', '0p2', '0p1')
OLD_SUFFIX = '__wearable_faces'
LOW_EVIDENCE_REVIEW = .2          # reduce_model.LOW_EVIDENCE_REVIEW


def digest_str(path):
    import hashlib
    h = hashlib.sha256()
    with Path(path).open('rb') as s:
        for chunk in iter(lambda: s.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def venv_python():
    """Interpreter with numpy + scipy: $SEGMENT_FIRST_PYTHON, config.json segment_first.venv_python, else <imageto3d>/../.venv."""
    try:
        cfg = (json.loads((HERE / 'config.json').read_text(encoding='utf-8-sig')).get('segment_first') or {}).get('venv_python')
    except (OSError, ValueError):
        cfg = None
    candidates = portable.venv_python_candidates(ROOT, cfg)
    for c in candidates:
        if Path(c).is_file():
            return c
    raise FileNotFoundError(f'no python with numpy+scipy for wearcut.py (tried {[c for c in candidates if c]})')


def palette_from_dict(d):
    """The palette a reduction used (manifest `palette`, `hue_shift` included) exactly as it classified there."""
    d = dict(d)
    pal = Palette(d, d.get('source', 'manifest'))
    pal.separable = bool(d.get('separable'))
    shifts = d.get('hue_shift') or {}
    return pal.with_hue_shifts(shifts) if shifts else pal


def wearcut_dir(out):
    """Cache folder of a reduction output folder: <module>/wearcut for <module>/reduced, else <out>/wearcut."""
    out = Path(out)
    return out.parent / 'wearcut' if out.name == 'reduced' else out / 'wearcut'


# ----------------------------------------------------------------------------------------------- 1. prep

def sample_with_ids(surface, points, ids):
    """TextureSurface.sample for known source triangles `ids` (same maths as segment_first_prep.sample_with_ids)."""
    tri = surface.v[surface.f[ids]]
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    e0, e1, q = b - a, c - a, points - a
    dot = lambda x, y: np.einsum('ij,ij->i', x, y)
    d00, d01, d11 = dot(e0, e0), dot(e0, e1), dot(e1, e1)
    d20, d21 = dot(q, e0), dot(q, e1)
    den = np.maximum(d00 * d11 - d01 * d01, 1e-30)
    u = (d11 * d20 - d01 * d21) / den
    v = (d00 * d21 - d01 * d20) / den
    bary = np.clip(np.stack([1 - u - v, u, v], axis=1), 0, 1)
    bary /= np.maximum(bary.sum(axis=1, keepdims=True), 1e-12)
    uv = np.einsum('ni,nij->nj', bary, surface.uv[ids])
    h, w = surface.pixels.shape[:2]
    xy = np.maximum(np.clip(uv, 0, 1) * np.array([w, h]) - .5, 0)
    low = np.floor(xy).astype(int)
    low[:, 0] = np.minimum(low[:, 0], w - 1)
    low[:, 1] = np.minimum(low[:, 1], h - 1)
    high = np.minimum(low + 1, [w - 1, h - 1])
    t = xy - low
    p = surface.pixels
    p00, p10 = p[low[:, 1], low[:, 0], :3], p[low[:, 1], high[:, 0], :3]
    p01, p11 = p[high[:, 1], low[:, 0], :3], p[high[:, 1], high[:, 0], :3]
    return ((p00 * (1 - t[:, :1]) + p10 * t[:, :1]) * (1 - t[:, 1:]) + (p01 * (1 - t[:, :1]) + p11 * t[:, :1]) * t[:, 1:])


def visibility_two_sided(points, normals, tree, cameras, eps=.004):
    """face_labels.visibility without the winding: a face counts as seen when either of its sides faces an unoccluded concept camera
    (the cosine is |n . to_camera|, the occlusion ray starts on the camera-facing side), so a face whose winding is wrong (see
    R.orient_consistent) is not mistaken for an unseen one. Same arguments and result as face_labels.visibility."""
    from mathutils import Vector
    best = np.zeros(len(points))
    for cam in cameras:
        to_cam = cam['position'][None] - points
        dist = np.linalg.norm(to_cam, axis=1)
        unit = to_cam / np.maximum(dist[:, None], 1e-12)
        raw = (normals * unit).sum(1)
        sign = np.where(raw >= 0, 1., -1.)
        cos = np.abs(raw)
        local = (points - cam['position'][None]) @ cam['rotation']
        depth = -local[:, 2]
        inside = (depth > 0) & (np.abs(local[:, 0]) < depth * cam['tan_half']) & (np.abs(local[:, 1]) < depth * cam['tan_half'])
        todo = np.flatnonzero(inside & (cos > 0) & (cos > best))
        start = points[todo] + normals[todo] * sign[todo][:, None] * eps
        for i, origin, direction, d in zip(todo, start, unit[todo], dist[todo]):
            if tree.ray_cast(Vector(origin), Vector(direction), float(d))[0] is None:
                best[i] = cos[i]
    return best


def build_prep(dest, surface, cleaned_v, cleaned_f, part_fit, cameras, pal, module, meta, part=None):
    """Write dest/prep.npz + prep.json. surface: texture_tools.TextureSurface of the (interior-stripped) source; cleaned_v/f: the
    reduction's cleaned high-poly mesh; part_fit: face_labels.BaseParts fitted to it (with its clearance record, or none: then the
    off-body / riser arrays are left out and those rules are off); cos: concept-camera cosine per face; pal: the final palette (used
    only to skip faces that are certainly garment when measuring the off-body distance). `part`: precomputed face parts."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    timing = {}
    t1 = time.monotonic()
    # adaptive.local_repair leaves patches (or a whole shell) with inconsistent / inward winding, differently from run to run: orient the
    # mesh before anything uses normals (visibility, part transfer, pooling, field smoothing). Faces keep their order and vertex ids.
    cleaned_f, orient = R.orient_consistent(cleaned_v, cleaned_f)
    timing['orient_s'] = time.monotonic() - t1
    t1 = time.monotonic()
    if cameras:      # concept-camera visibility of the (oriented) faces
        tree = BVHTree.FromPolygons(cleaned_v.tolist(), cleaned_f.tolist(), all_triangles=True)
        cos = visibility_two_sided(cleaned_v[cleaned_f].mean(1), fl.face_normals(cleaned_v, cleaned_f)[0], tree, cameras)
    else:
        cos = np.ones(len(cleaned_f))
    timing['visibility_s'] = time.monotonic() - t1
    t1 = time.monotonic()
    if part is None:
        part = face_labels_transfer(cleaned_v, cleaned_f, part_fit)
    timing['parts_s'] = time.monotonic() - t1
    t1 = time.monotonic()
    tris = surface.v[surface.f]
    kd = kdtree.KDTree(len(surface.f))
    for i, c in enumerate(tris.mean(1)):
        kd.insert(c.tolist(), i)
    kd.balance()
    cent = cleaned_v[cleaned_f].mean(1)
    ids = np.array([kd.find(c.tolist())[1] for c in cent], np.int64)
    tri = cleaned_v[cleaned_f].astype(np.float32)
    colors = np.zeros((len(cleaned_f), 3, 3), np.float32)
    for k, w in enumerate(((2 / 3, 1 / 6, 1 / 6), (1 / 6, 2 / 3, 1 / 6), (1 / 6, 1 / 6, 2 / 3))):
        colors[:, k] = sample_with_ids(surface, (tri * np.array(w, np.float32)[None, :, None]).sum(1), ids)
    timing['colours_s'] = time.monotonic() - t1
    extra, off_info = {}, {'enabled': False}
    t1 = time.monotonic()
    thr = fl.off_body_threshold(part, part_fit) if part_fit.clearance is not None else None
    if thr is not None:
        allowed = R.part_tables(part_fit.names, module)['allowed'][part]
        role, w = pal.classify(colors.reshape(-1, 3).astype(np.float64))
        sure_garment = ((role >= 1) & (role <= pal.n_garments) & (w > .3)).reshape(-1, 3).all(1)
        mask = allowed & ~sure_garment
        idx = np.flatnonzero(mask)
        dist = np.full(len(cleaned_f), -np.inf)
        agree = np.ones(len(cleaned_f))
        if len(idx):
            dist[idx] = fl.off_body_distance(cleaned_v, cleaned_f[idx], part_fit)
            normals = fl.face_normals(cleaned_v, cleaned_f[idx])[0]
            agree[idx] = (normals * part_fit.nearest_normal(cent[idx])).sum(1)
        extra = {'off_dist': dist.astype(np.float32), 'off_thr': thr.astype(np.float32), 'base_agree': agree.astype(np.float32)}
        off_info = {'enabled': True, 'faces_measured': int(len(idx)), 'faces': int(len(cleaned_f))}
    timing['off_body_s'] = time.monotonic() - t1
    np.savez_compressed(dest / 'prep.npz', v=cleaned_v.astype(np.float32), f=cleaned_f.astype(np.int32), colors=colors,
                        part=np.asarray(part, np.int16), part_names=np.array(part_fit.names), cos=np.asarray(cos).astype(np.float32),
                        source_tri=ids.astype(np.int32), **extra)
    meta = dict(meta, module=module, faces=int(len(cleaned_f)), timing=timing, off_body=off_info, orientation=orient, schema='wearcut_prep_v1')
    (dest / 'prep.json').write_text(json.dumps(meta, indent=2, default=float) + '\n')
    print('WEARCUT_PREP', json.dumps(timing), flush=True)
    return part


def face_labels_transfer(v, f, part_fit):
    return fl.transfer_parts(v, f, part_fit)


# ----------------------------------------------------------------------------------------------- 2. plane

def fit_mirror_plane(base_parts, fit_info, lod_v, lod_f):
    """symmetry.fit_plane on the (0.1%) level, as symmetry_scan.scan_module does: started from the coarse x = const scan and the
    base's sagittal plane mapped through the ICP fit. Returns (Plane, info dict)."""
    surface = sym.Surface(lod_v, lod_f)
    pts = surface.sample(2500, seed=1)[0]
    inits = []
    if base_parts is not None:
        base_sym = fl.symmetry_plane(base_parts)
        if base_sym.get('x') is not None:
            s, t = ((fit_info['scale'], np.asarray(fit_info['translation'])) if fit_info and fit_info.get('used') == 'icp'
                    else (1., np.zeros(3)))
            inits = [('base_mapped', sym.Plane.from_x(s * base_sym['x'] + t[0]))]
    plane, info = sym.fit_plane(surface.distances, pts, inits, scan=.12, scan_step=.04)
    return plane, {**plane.to_dict(), 'fit': info}


# ----------------------------------------------------------------------------------------------- 3 + 4. run and install

def run_wearcut(wc, module, palette_dict, plane_dict, levels, log, feature_labels=None):
    (wc / 'palette.json').write_text(json.dumps(palette_dict, indent=1, default=float) + '\n')
    (wc / 'plane.json').write_text(json.dumps(plane_dict, indent=1, default=float) + '\n')
    command = [venv_python(), str(HERE / 'wearcut.py'), str(wc / 'prep'), str(wc), '--module', module,
               '--palette', str(wc / 'palette.json'), '--plane', str(wc / 'plane.json'), '--levels', ','.join(levels)]
    if feature_labels:
        command += ['--feature-labels', feature_labels]
    with open(log, 'w') as handle:
        run = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT)
    if run.returncode != 0:
        tail = ' | '.join(Path(log).read_text(errors='replace').strip().splitlines()[-5:])
        raise RuntimeError(f'wearcut.py exited {run.returncode} (log {log}): {tail}')
    return json.loads((wc / 'wearcut.json').read_text(encoding='utf-8-sig'))


def run_feature_plan(out, wc, spec, levels, log):
    """venv python: the role transfer of the feature label pass to the labelled full LODs (feature_relabel.py plan)."""
    command = [venv_python(), str(HERE / 'feature_relabel.py'), 'plan', str(Path(out).parent), '--reduced', str(out), '--wearcut', str(wc),
               '--spec', spec, '--levels', ','.join(levels)]
    with open(log, 'w') as handle:
        run = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT)
    if run.returncode != 0:
        tail = ' | '.join(Path(log).read_text(errors='replace').strip().splitlines()[-5:])
        raise RuntimeError(f'feature_relabel.py plan exited {run.returncode} (log {log}): {tail}')


def export_level(rm, info, name, v, f, role, pal, cache, dest):
    """Wearable mesh -> GLB with the reduction's material slots (one per palette class, so the exporter groups faces by role)."""
    obj = rm.lab.make_obj(name, v.astype(np.float64), f.astype(np.int64))
    try:
        rm.assign_materials(obj, role.astype(np.int32), pal.material_srgb(), pal.material_names(), cache)
        count = rm.export_native(obj, info, Path(dest))
    finally:
        rm.lab.drop(obj)
    return count


def glb_order_arrays(role, **per_face):
    """The exporter writes one primitive per material slot, faces of a slot in mesh order: GLB face i = mesh face order[i]."""
    order = np.argsort(role, kind='stable')
    return order, {k: a[order] for k, a in per_face.items()}


def prepare_sidecar(npz_path, new_arrays, backup_path):
    """Write the level's _colors.npz with the wearable_* / mirror_* arrays replaced to `<name>.new.npz` (the old ones go to backup_path,
    kept if it already exists); returns (temporary path, names of the replaced arrays). The caller swaps it in with os.replace."""
    z = dict(np.load(npz_path, allow_pickle=False))
    old = {k: v for k, v in z.items() if k.startswith('wearable_') or k.startswith('mirror_')}
    if old and not Path(backup_path).exists():
        np.savez_compressed(backup_path, **old)
    for k in old:
        del z[k]
    z.update(new_arrays)
    tmp = Path(str(npz_path) + '.new.npz')
    np.savez_compressed(tmp, **z)
    return tmp, sorted(old)


def glb_material_names(path):
    data = Path(path).read_bytes()
    n = int.from_bytes(data[12:16], 'little')
    return [m.get('name') for m in json.loads(data[20:20 + n]).get('materials', [])]


def free_material_names(pal, keep):
    """Materials of the open Blender session that carry a palette material name (and are not the ones we reuse) are renamed, so the
    new materials get the exact names `primary`, `secondary`... (a clash would export `primary.001`, which the consumers do not know)."""
    import re
    names = set(pal.material_names())
    keep_ids = {id(m) for m in keep.values()}
    for mat in list(bpy.data.materials):
        if re.sub(r'\.\d+$', '', mat.name) in names and id(mat) not in keep_ids:
            mat.name = mat.name + '__stale'


def install(rm, out, info, module, pal, plane, result, levels, part_names, wc, log, material_cache=None):
    """Swap the cut wearables in. Per level: export to a temporary GLB, then keep the old wearable as __wearable_faces.glb, move the
    new one in place, rewrite the sidecar arrays and the manifest record. A level that fails keeps its old wearable."""
    out = Path(out)
    cache = material_cache if material_cache is not None else {}
    free_material_names(pal, cache)
    wearable_info = {}
    for lv in levels:
        name = f'lod_error_{lv}'
        entry = info['levels'][lv]
        try:
            z = np.load(wc / f'wearcut_lod_{lv}.npz', allow_pickle=False)
            v, f, role = z['v'].astype(np.float64), z['f'].astype(np.int64), z['role'].astype(np.int64)
            part, src, mirrored = z['part'].astype(np.int64), z['src'].astype(np.int64), z['mirrored'].astype(np.int64)
            if not len(f):
                raise ValueError('the cut wearable has no faces')
            tmp = out / f'{name}__wearable.cut.glb'
            count = export_level(rm, info, f'{name}__wearable', v, f, role, pal, cache, tmp)
            if count != len(f):
                raise ValueError(f'GLB has {count} triangles, expected {len(f)}')
            bad = [n for n in glb_material_names(tmp) if n not in set(pal.material_names())]
            if bad:
                raise ValueError(f'GLB material names {bad} are not the palette roles')
            order, glb = glb_order_arrays(role, part=part, src=src, mirrored=mirrored)
            area = np.linalg.norm(np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]]), axis=1) / 2
            glb_path, old_path = out / f'{name}__wearable.glb', out / f'{name}{OLD_SUFFIX}.glb'
            new_arrays = {'wearable_face_order': np.full(len(f), -1, np.int64),
                          'wearable_body_part_glb_order': glb['part'].astype(np.int64),
                          'wearable_label_source_glb_order': glb['src'].astype(np.int64),
                          'wearable_method': np.array('cut')}
            lr = result['levels'][lv]
            if mirrored.any():
                new_arrays.update(wearable_mirrored_glb_order=glb['mirrored'].astype(np.int64),
                                  mirror_plane_x=np.array(float(plane.x_at(0., 1.))),
                                  mirror_plane_normal=np.asarray(plane.n, float), mirror_plane_offset=np.array(float(plane.d)))
            previous_triangles = ((entry.get('wearable') or {}).get('previous_triangles') if entry.get('wearable_method') == 'cut'
                                  else entry.get('wearable_triangles'))
            colors_path = out / f'{name}_colors.npz'
            sidecar, old_keys = (prepare_sidecar(colors_path, new_arrays, out / f'{name}_colors_wearable_faces.npz')
                                 if colors_path.exists() else (None, []))
            if glb_path.exists() and not old_path.exists():
                os.replace(glb_path, old_path)
            os.replace(tmp, glb_path)
            if sidecar is not None:
                os.replace(sidecar, colors_path)
            wear_info = {'method': 'cut', 'triangles': int(len(f)), 'area': float(area.sum()), 'mirrored_faces': int(mirrored.sum()),
                         **({'footwear': result['footwear']} if result.get('footwear') else {}),
                         'previous_glb': old_path.name, 'previous_triangles': previous_triangles,
                         'previous_sidecar_arrays': old_keys, 'low_evidence_area_share': lr['low_evidence_area_share'],
                         'review_required': bool(lr['low_evidence_area_share'] > LOW_EVIDENCE_REVIEW),
                         **({'feature_labels': result['feature_labels'].get('spec')} if result.get('feature_labels') else {}),
                         'garment_area_share_by_source': {k: float(area[src == i].sum() / max(area.sum(), 1e-20))
                                                          for i, k in enumerate(['texture', 'symmetry', 'prior', 'neighbours', 'off_body', 'riser', 'mirrored'])
                                                          if (src == i).any()}}
            entry.update(wearable_glb=str(glb_path), wearable_triangles=int(len(f)), wearable_method='cut', wearable_is_open_surface=True,
                         wearable=wear_info,
                         mirror=({'method': 'exact_reflection_of_cut_right_piece', 'faces': int(mirrored.sum()),
                                  'area_share_of_wearable': float(area[mirrored > 0].sum() / max(area.sum(), 1e-20)),
                                  'pair_check': lr.get('pair_check') or lr.get('right', {}).get('pair_check')}
                                 if mirrored.any() else None))
            if entry.get('body_parts') is not None:
                entry['body_parts']['wearable_area_share'] = fl.part_area_shares(part, area, part_names)
            wearable_info[lv] = 'cut'
        except Exception as error:  # noqa: BLE001 - the old wearable of this level stays
            traceback.print_exc()
            tmp = out / f'{name}__wearable.cut.glb'
            tmp.unlink(missing_ok=True)
            Path(str(out / f'{name}_colors.npz') + '.new.npz').unlink(missing_ok=True)
            entry['wearable_method'] = 'faces'
            entry['wearable'] = {'method': 'faces', 'requested': 'cut', 'fallback_reason': f'{type(error).__name__}: {error}'}
            wearable_info[lv] = f'faces ({error})'
            print('WEARCUT_LEVEL_FALLBACK', lv, error, flush=True)
    return wearable_info


def cut_wearables(rm, out, info, module, pal, make_prep, plane_surface, base_parts, fit_info, levels=ALL_LEVELS, material_cache=None,
                  palette_dict=None, feature_labels=None, live_objects=False):
    """Whole step; never raises. make_prep(dest): writes prep.npz/prep.json (reduction in-process data or rebuilt for a backfill);
    plane_surface(): (v, f) of the 0.1% level in the normalised frame. Sets info['wearable'] (and each level's record) and returns it.
    feature_labels: opt-in label pass spec (feature_hooks.py: studs | rings:secondary | rings:void...): wearcut.py applies it to the
    high-poly labels before the cut, then feature_relabel.py carries the corrected roles to the labelled full LODs (GLB, colours npz,
    manifest, review.blend; `live_objects` = the level objects are in the open Blender session, as in reduce_model.py). Recorded in
    info['wearable']['feature_labels']."""
    t0 = time.monotonic()
    out = Path(out)
    wc = wearcut_dir(out)
    record = {'requested': 'cut', 'method': 'faces', 'status': 'fallback', 'dir': str(wc), 'fallback_reason': None}
    try:
        if module is None:
            raise ValueError('no module (the garment rules need one)')
        if base_parts is None or fit_info is None:
            raise ValueError('no body-parts file for this base (the cut needs body parts)')
        levels = [lv for lv in levels if lv in info['levels']]
        wc.mkdir(parents=True, exist_ok=True)
        t1 = time.monotonic()
        if not (wc / 'prep' / 'prep.npz').exists():
            make_prep(wc / 'prep')
        record['prep_seconds'] = round(time.monotonic() - t1, 1)
        t1 = time.monotonic()
        lod_v, lod_f = plane_surface()
        plane, plane_info = fit_mirror_plane(base_parts, fit_info, lod_v, lod_f)
        record['plane_seconds'] = round(time.monotonic() - t1, 1)
        t1 = time.monotonic()
        result = run_wearcut(wc, module, palette_dict or info['palette'], {'normal': plane.n.tolist(), 'offset': plane.d, **{k: v for k, v in plane_info.items() if k not in ('normal', 'offset')}},
                             levels, wc / 'wearcut.log', feature_labels)
        record['wearcut_seconds'] = round(time.monotonic() - t1, 1)
        if result.get('fallback'):
            raise ValueError(result['fallback'])
        part_names = [str(n) for n in np.load(wc / 'prep' / 'prep.npz')['part_names']]
        per_level = install(rm, out, info, module, pal, plane, result, levels, part_names, wc, wc / 'install.log', material_cache)
        ok = [lv for lv, m in per_level.items() if m == 'cut']
        record.update(method=('cut' if len(ok) == len(per_level) else 'mixed') if ok else 'faces', status='done' if ok else 'fallback',
                      levels=per_level, plane=plane_info, pair=result.get('pair'), **({'footwear': result['footwear']} if result.get('footwear') else {}),
                      off_body=result.get('off_body'), riser=result.get('riser'),
                      pooling={k: result['pooling'].get(k) for k in ('pool_mode', 'matched_share', 'pool_weight_mean_area', 'global_garment_agreement_role')},
                      hue=result.get('hue'), flaps={lv: (result['levels'][lv].get('right') or result['levels'][lv].get('garment') or {}).get('flaps')
                                                    for lv in per_level},
                      wearcut_json=str((wc / 'wearcut.json')))
        if not ok:
            record['fallback_reason'] = 'every level failed to install: ' + '; '.join(per_level.values())
        if feature_labels:
            record['feature_labels'] = relabel_full_levels(rm, out, info, pal, wc, result, levels, feature_labels, material_cache, live_objects)
    except Exception as error:  # noqa: BLE001
        traceback.print_exc()
        record['fallback_reason'] = f'{type(error).__name__}: {error}'
        for lv in levels:
            entry = info['levels'].get(lv)
            if entry is not None:
                entry['wearable_method'] = 'faces'
                entry['wearable'] = {'method': 'faces', 'requested': 'cut', 'fallback_reason': record['fallback_reason']}
        print('WEARCUT_FALLBACK', record['fallback_reason'], flush=True)
    record['seconds'] = round(time.monotonic() - t0, 1)
    info['wearable'] = record
    print('WEARCUT_STEP', record['method'], record['status'], f'{record["seconds"]}s', flush=True)
    return record


def relabel_full_levels(rm, out, info, pal, wc, result, levels, spec, material_cache, live_objects):
    """The labelled full LODs after a feature label pass: plan (venv python), then rebuild (Blender). Never raises; returns the record."""
    import feature_hooks
    import feature_relabel
    spec = feature_hooks.normalise_spec(spec)
    rec = {'spec': spec, **{k: v for k, v in (result.get('feature_labels') or {}).items() if k != 'spec'}}
    if rec.get('skipped'):
        return rec
    try:
        run_feature_plan(out, wc, spec, levels, wc / 'feature_plan.log')
        rec['levels'] = feature_relabel.apply(rm, out, info, pal, spec, levels, material_cache, objects=live_objects)
    except Exception as error:  # noqa: BLE001
        traceback.print_exc()
        rec['levels_error'] = f'{type(error).__name__}: {error}'
    return rec


# ----------------------------------------------------------------------------------------------- backfill (Blender script)

def resolve_module_inputs(rm, module_dir, manifest):
    """Cameras / regions / parts exactly as the reduction used them (manifest paths while they exist, else the same resolution
    reduce_model.py does, which also finds jobs that have since moved between pipeline folders)."""
    module_dir = Path(module_dir)
    found = {}
    try:
        _, cam, reg, parts, _, _, _ = rm.resolve_inputs(module_dir / 'source.glb', None, None, None, manifest.get('module'), None, True, None, False)
        found = {'cameras': cam, 'regions': reg, 'parts': parts}
    except Exception as exc:  # noqa: BLE001
        found = {'error': repr(exc)}
    out = {}
    for key, recorded in (('cameras', manifest.get('cameras')), ('regions', manifest.get('regions')),
                          ('parts', (manifest.get('parts') or {}).get('file'))):
        for candidate in (portable.local_path(recorded), found.get(key)):
            if candidate and Path(candidate).is_file():
                out[key] = Path(candidate)
                break
        else:
            out[key] = None
    if out['parts'] is None and manifest.get('parts'):
        fallback = ROOT / 'base_views' / module_dir.parent.name / 'parts.npz'
        if fallback.is_file():
            out['parts'] = fallback
    return out


def load_level_surface(manifest, path):
    """Level GLB -> (v, f) in the normalised frame (symmetry_scan.load_lod)."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(path), merge_vertices=False)
    obj = [o for o in bpy.context.scene.objects if o.type == 'MESH'][0]
    mw = np.array(obj.matrix_world)
    mesh = obj.data
    mesh.calc_loop_triangles()
    v = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.vertices.foreach_get('co', v)
    v = v.reshape(-1, 3).astype(np.float64) @ mw[:3, :3].T + mw[:3, 3]
    f = np.empty(len(mesh.loop_triangles) * 3, np.int64)
    mesh.loop_triangles.foreach_get('vertices', f)
    v = (v @ np.asarray(manifest['rotation_to_review']).T - np.asarray(manifest['normalization_origin_rotated'])) * manifest['normalization_scale']
    return v, f.reshape(-1, 3)


def rewear_module(rm, module_dir, levels=ALL_LEVELS, force=False, feature_labels=None):
    """Re-run only the wearable step of a converted module from its saved outputs (source.glb, reduced/manifest.json, the level GLBs).
    The previous wearable GLBs / sidecar arrays / wearable.blend are kept under their `_faces` names. Returns the manifest record.
    feature_labels: an opt-in label pass spec (feature_hooks.py); the cached wearcut/prep is reused and, besides the wearable, the
    labelled full LODs (GLB, colours npz, manifest, review.blend) get the corrected roles. A module that already ran the same spec is
    skipped (no --force needed); the earlier files are NOT kept by this function (rewear.py / apply_feature_labels.py back them up)."""
    import feature_hooks
    module_dir = Path(module_dir).resolve()
    out = module_dir / 'reduced'
    manifest_path = out / 'manifest.json'
    info = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    module = info.get('module') or module_dir.name
    spec = feature_hooks.normalise_spec(feature_labels) if feature_labels else None
    if feature_labels and spec is None:
        raise ValueError('rewear --feature-labels none: a plain wearable comes back from the backup, not from a re-run')
    done_spec = ((info.get('wearable') or {}).get('feature_labels') or {}).get('spec')
    if (info.get('wearable') or {}).get('method') in ('cut', 'mixed') and not force and (spec is None or spec == done_spec):
        print('REWEAR already cut, skipped', module_dir, flush=True)
        return info['wearable']
    if spec is None and done_spec and not force:
        raise ValueError(f'this module ran feature labels {done_spec!r}: pass --feature-labels {done_spec} (or restore the backup) instead of --force without it')
    inputs = resolve_module_inputs(rm, module_dir, info)
    palette_dict = R.palette_dict(info)    # modules converted before palette.json existed: their legacy tricolor_v1 swatches
    pal = palette_from_dict(palette_dict)
    source = module_dir / 'source.glb'
    state = {}

    def make_prep(dest):
        t0 = time.monotonic()
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.import_scene.gltf(filepath=str(source), merge_vertices=False)
        obj = [o for o in bpy.context.scene.objects if o.type == 'MESH'][0]
        rm.lab.active(obj)
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
        native, _ = rm.lab.arrays(obj)
        rotation = np.asarray(Matrix.Rotation(math.radians(0.0), 3, 'Z'))
        turned = native @ rotation.T
        low, high = turned.min(axis=0), turned.max(axis=0)
        origin = (low + high) / 2
        origin[2] = low[2]
        scale = 2 / (high[2] - low[2])
        if abs(scale - info['normalization_scale']) > 1e-6 * scale or np.abs(origin - np.asarray(info['normalization_origin_rotated'])).max() > 1e-6:
            raise ValueError('source.glb no longer normalises like the reduction did (scale/origin differ from the manifest)')
        obj.data.vertices.foreach_set('co', ((turned - origin) * scale).ravel())
        obj.data.update()
        surface = rm.texture_tools.TextureSurface(obj)
        sv, sf_ = surface.v.astype(np.float64), surface.f.astype(np.int64)
        import bmesh
        enclosed = None
        if (info.get('interior') or {}).get('stripped'):
            enclosed = fl.enclosed_faces(sv, sf_, surface.tree)
            surface.f, surface.uv = surface.f[~enclosed], surface.uv[~enclosed]
            surface.tree = BVHTree.FromPolygons(surface.v.tolist(), surface.f.tolist(), all_triangles=True)
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        if enclosed is not None:
            bm.faces.ensure_lookup_table()
            bmesh.ops.delete(bm, geom=[bm.faces[i] for i in np.flatnonzero(enclosed)], context='FACES')
            bmesh.ops.delete(bm, geom=[x for x in bm.verts if not x.link_faces], context='VERTS')
        bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=2e-7)
        bm.to_mesh(obj.data)
        bm.free()
        obj.data.update()
        rv, rf = rm.lab.arrays(obj)
        cv, cf, _ = rm.adaptive.local_repair(rv, rf)
        cv, cf = rm.canonical_order(cv, cf)
        base_parts = state['base_parts']
        part_fit = state['part_fit']
        regions = json.loads(Path(inputs['regions']).read_text(encoding='utf-8-sig')) if inputs['regions'] else None
        cams = fl.load_cameras(inputs['cameras'], regions, info.get('omit_views') or ()) if inputs['cameras'] and regions else []
        print('REWEAR front matter', len(cf), 'faces', f'{time.monotonic() - t0:.0f}s', flush=True)
        meta = {k: info[k] for k in ('normalization_scale', 'normalization_origin_rotated', 'rotation_to_review')}
        meta.update(source=str(source), cameras=str(inputs['cameras']) if inputs['cameras'] else None, omit_views=info.get('omit_views') or [],
                    regions=str(inputs['regions']) if inputs['regions'] else None, palette=palette_dict)
        build_prep(dest, surface, cv, cf, part_fit, cams, pal, module, meta)

    base_parts = part_fit = fit_info = None
    if inputs['parts'] and info.get('parts'):
        clearance_path = None
        for c in (Path(inputs['parts']).with_name('clearance.json'), ROOT / 'base_views' / module_dir.parent.name / 'clearance.json'):
            if c.is_file():
                clearance_path = c
                break
        # a knee-cop module (manifest knee_cops) is re-cut with the same relabelled base parts the reduction used
        cops = info.get('knee_cops') or {}
        base_parts = fl.BaseParts.load(inputs['parts'], clearance_path, cops if cops.get('enabled') else None)
        fit_info = info['parts']['fit']
        part_fit = base_parts.moved(fit_info['scale'], np.asarray(fit_info['translation'])) if fit_info['used'] == 'icp' else base_parts
    state.update(base_parts=base_parts, part_fit=part_fit)

    def plane_surface():
        for lv in ('0p1', '0p2', '0p4'):
            p = out / f'lod_error_{lv}.glb'
            if p.is_file():
                return load_level_surface(info, p)
        raise FileNotFoundError('no lod_error_*.glb for the plane fit')

    # keep what is replaced: the old wearable blend (rewritten at the end), the manifest as it was
    if (out / 'wearable.blend').exists() and not (out / 'wearable_faces.blend').exists():
        shutil.copy2(out / 'wearable.blend', out / 'wearable_faces.blend')
    if not (out / 'manifest_before_rewear.json').exists():
        shutil.copy2(manifest_path, out / 'manifest_before_rewear.json')
    if force:      # a forced rewear rebuilds the cached prep as well
        shutil.rmtree(wearcut_dir(out) / 'prep', ignore_errors=True)
    record = cut_wearables(rm, out, info, module, pal, make_prep, plane_surface, base_parts, fit_info, levels, palette_dict=palette_dict,
                           feature_labels=spec)
    manifest_path.write_text(json.dumps(info, indent=2, sort_keys=True, default=float) + '\n')
    import make_wearable_blend
    make_wearable_blend.build(out)
    return record


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog='blender -b --python wearcut_blender.py --', description='Re-run only the wearable step of a converted module.')
    ap.add_argument('command', choices=['rewear'])
    ap.add_argument('module_dir')
    ap.add_argument('--levels', default=','.join(ALL_LEVELS))
    ap.add_argument('--force', action='store_true', help='run again even if the module already has a cut wearable (rebuilds the cached prep)')
    ap.add_argument('--feature-labels', metavar='SPEC', help='opt-in label pass (feature_hooks.py: studs | rings:secondary | rings:void): the wearable is '
                    're-cut with it and the labelled full LODs relabelled; reuses the cached wearcut/prep; skipped when the module already ran this spec')
    a = ap.parse_args(argv)
    import reduce_model as rm
    record = rewear_module(rm, a.module_dir, tuple(a.levels.split(',')), a.force, a.feature_labels)
    print('REWEAR_DONE', json.dumps({k: record.get(k) for k in ('method', 'status', 'seconds', 'fallback_reason')}), flush=True)


if __name__ == '__main__':
    main(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else sys.argv[1:])
