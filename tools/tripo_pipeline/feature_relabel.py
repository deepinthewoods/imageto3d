"""Blender side of the opt-in feature label passes: the corrected high-poly roles go to the labelled full LODs (2026-10-25).

`wearcut.py --feature-labels SPEC` (feature_hooks.py) corrects the high-poly labels before the wearable is cut and writes
`<module>/wearcut/feature_labels.npz` (label_old / label_new per high-poly face of `wearcut/prep/prep.npz`, plus the pass's own arrays).
This module carries those labels to the labelled levels, whose roles came from the texture of the reduced triangles:

  per level: lod_error_<lv>.glb is rebuilt (the level's mesh in its saved order, new material per triangle, Blender's own glTF exporter via
  reduce_model.export_native: the same flat normals, material slots and primitives as the original export), lod_error_<lv>_colors.npz gets
  the new `material_id` / `glb_face_order` / `body_part_glb_order` (the old `material_id` stays as `material_id_before_feature_labels`),
  the manifest level gets `material_face_counts` and `feature_labels` (rule, triangles changed, area per role before / after), and
  review.blend gets the new material per polygon of the `lod_error_<lv>` objects. Triangle count, vertices and winding never change; only
  which role a triangle has (studs: the tertiary stud triangles; rings: ring / centre / void). Nothing else of the level is touched.

Two steps because Blender's python has no scipy:
  * `plan` (venv python, `python feature_relabel.py plan <module_dir> --spec S [--levels ...]`): the role transfer
    (feature_hooks.transfer_roles) per level -> `wearcut/feature_lod_<lv>.npz` (role_old / role_new per triangle in the GLB's face order)
    and `wearcut/feature_lod.json`;
  * `apply` (Blender, numpy only) reads those plans and rebuilds files: in reduce_model.py right after the wearable cut (objects = the live
    level objects, review.blend is saved afterwards by reduce_model), and from `wearcut_blender.py rewear --feature-labels`
    (objects=False: review.blend is opened, edited, saved).
"""
import json
import os
import sys
import traceback
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import feature_hooks  # noqa: E402  (no numpy / scipy at import)
import wearcut_rules as R  # noqa: E402  (numpy only: read_glb / from_glb)


def tri_area(v, f):
    return np.linalg.norm(np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]]), axis=1) / 2


def role_lookup(pal):
    return {n: i for i, n in enumerate(['base', *pal.roles])}


def area_by_role(v, f, role, names):
    a = tri_area(v, f)
    return {nm: float(a[role == i].sum()) for i, nm in enumerate(names) if (role == i).any()}


def plan(reduced_dir, wearcut_dir, spec, levels, log=print):
    """venv python: the role transfer for every level of a module (reduced_dir = <module>/reduced, wearcut_dir = <module>/wearcut) ->
    wearcut/feature_lod_<lv>.npz (+ feature_lod.json). Returns the dict {lv: record} that is also written."""
    out, wdir = Path(reduced_dir), Path(wearcut_dir)
    spec = feature_hooks.normalise_spec(spec)
    info = json.loads((out / 'manifest.json').read_text(encoding='utf-8-sig'))
    pal = wearcut_palette(info)
    arrays = dict(np.load(wdir / 'feature_labels.npz', allow_pickle=False))
    with np.load(wdir / 'prep' / 'prep.npz') as prep:   # closed again (an open npz handle blocks deleting the file on Windows)
        hv, hf = prep['v'].astype(np.float64), prep['f'].astype(np.int64)
    tri = hv[hf]
    hp_cent = tri.mean(1)
    hp_area = tri_area(hv, hf)
    K = len(pal['roles']) + 1
    names = ['base', *pal['roles']]
    lut = {n: i for i, n in enumerate(names)}
    result = {}
    for lv in levels:
        glb_path = out / f'lod_error_{lv}.glb'
        if not glb_path.is_file():
            result[lv] = {'error': f'no {glb_path.name}'}
            continue
        gv, gf, glb_names = R.read_glb(glb_path)
        role_old = np.array([lut.get(str(nm), 0) for nm in glb_names], np.int64)
        vn = R.from_glb(gv, info)
        role_new, tinfo = feature_hooks.transfer_roles(spec, vn, gf, role_old, hp_cent, hp_area, arrays, K)
        la = tri_area(vn, gf)
        result[lv] = {'spec': spec, **tinfo, 'triangles': int(len(gf)), 'area_changed_share': float(la[role_new != role_old].sum() / la.sum()),
                      'role_area_before': area_by_role(vn, gf, role_old, names), 'role_area_after': area_by_role(vn, gf, role_new, names)}
        np.savez_compressed(wdir / f'feature_lod_{lv}.npz', role_old=role_old.astype(np.int8), role_new=role_new.astype(np.int8))
        log(f'FEATURE_PLAN {lv}: {tinfo["faces_changed"]} of {len(gf)} triangles change role')
    (wdir / 'feature_lod.json').write_text(json.dumps(result, indent=1) + '\n')
    return result


def wearcut_palette(info):
    """roles of the palette the reduction used (numpy-only: the manifest's `palette` block, else the legacy tricolor_v1)."""
    pal = info.get('palette') or {}
    roles = [g['role'] for g in pal.get('garments', [])]
    if not roles:
        import wearcut_rules
        roles = [g['role'] for g in wearcut_rules.palette_dict(info).get('garments', [])]
    return {'roles': roles}


def apply(rm, out, info, pal, spec, levels, material_cache=None, objects=False, review_blend=True, log=print):
    """Blender: rebuild the full LODs of the module in `out` (the reduced/ folder) from the plans `plan` wrote. `info` = manifest dict
    (updated in place). Never raises: returns {lv: record}; a level that fails keeps its files and gets {'error': ...}."""
    import bpy
    out = Path(out)
    spec = feature_hooks.normalise_spec(spec)
    wdir = out.parent / 'wearcut'
    records = {}
    if spec is None:
        return records
    try:
        plans = json.loads((wdir / 'feature_lod.json').read_text(encoding='utf-8-sig'))
    except Exception as error:  # noqa: BLE001
        traceback.print_exc()
        return {lv: {'spec': spec, 'error': f'no feature_lod.json: {error}'} for lv in levels}
    names = ['base', *pal.roles]
    lut = {n: i for i, n in enumerate(names)}
    cache = material_cache if material_cache is not None else {}
    import wearcut_blender
    wearcut_blender.free_material_names(pal, cache)
    new_ids = {}
    for lv in levels:
        name = f'lod_error_{lv}'
        glb_path, npz_path = out / f'{name}.glb', out / f'{name}_colors.npz'
        tmp_glb, tmp_npz = out / f'{name}.feature.glb', Path(str(npz_path) + '.feature.npz')
        try:
            rec = plans.get(lv)
            if rec is None or 'error' in rec:
                raise ValueError((rec or {}).get('error', 'no plan for this level'))
            if lv not in info['levels'] or not glb_path.is_file() or not npz_path.is_file():
                raise FileNotFoundError(f'{name}: no level GLB / colours file')
            with np.load(wdir / f'feature_lod_{lv}.npz') as z_plan:
                role_old, role_new = z_plan['role_old'].astype(np.int64), z_plan['role_new'].astype(np.int64)
            gv, gf, glb_names = R.read_glb(glb_path)
            n = len(gf)
            if len(role_old) != n or not (np.array([lut.get(str(nm), 0) for nm in glb_names]) == role_old).all():
                raise ValueError(f'{name}: the plan does not match the level GLB (rebuilt since?)')
            z = dict(np.load(npz_path, allow_pickle=False))
            order_old = z['glb_face_order'].astype(np.int64) if 'glb_face_order' in z else np.argsort(z['material_id'], kind='stable')
            if len(order_old) != n:
                raise ValueError(f'{name}: the GLB has {n} triangles but the review mesh {len(order_old)} (the exporter dropped degenerate '
                                 f'triangles), so the triangles cannot be mapped back; this level keeps its roles')
            if not (z['material_id'][order_old] == role_old).all():
                raise ValueError(f'{name}: the sidecar material_id does not match the level GLB (not a plain reduction output)')
            # the level's mesh in its saved (mesh) order: face order_old[i] is GLB triangle i
            uniq, inv = np.unique(gv, axis=0, return_inverse=True)       # exact duplicates only: the exporter split them per primitive
            vw, fw = R.from_glb(uniq, info), inv.reshape(-1)[gf]
            f_mesh = np.empty_like(fw)
            f_mesh[order_old] = fw
            mat_new = np.empty(n, np.int64)
            mat_new[order_old] = role_new
            order_new = np.argsort(mat_new, kind='stable')
            if (role_new != role_old).any():
                obj = rm.lab.make_obj(f'{name}__feature', vw.astype(np.float64), f_mesh.astype(np.int64))
                try:
                    rm.assign_materials(obj, mat_new.astype(np.int32), pal.material_srgb(), pal.material_names(), cache)
                    count = rm.export_native(obj, info, tmp_glb)
                finally:
                    rm.lab.drop(obj)
                if count != n:
                    raise ValueError(f'rebuilt GLB has {count} triangles, expected {n}')
                gv2, gf2, nm2 = R.read_glb(tmp_glb)
                if [str(x) for x in nm2] != [names[r] for r in mat_new[order_new]]:
                    raise ValueError('rebuilt GLB: material order differs from the expected one')
                c_exp = vw[f_mesh[order_new]].mean(1)
                c_got = R.from_glb(gv2, info)[gf2].mean(1)
                if np.abs(c_exp - c_got).max() > 1e-4:
                    raise ValueError(f'rebuilt GLB: triangle order differs from the expected one (max {np.abs(c_exp - c_got).max():.2e})')
                bad = [m for m in wearcut_blender.glb_material_names(tmp_glb) if m not in set(pal.material_names())]
                if bad:
                    raise ValueError(f'rebuilt GLB: material names {bad} are not the palette roles')
                z_new = dict(z, material_id_before_feature_labels=z['material_id'], material_id=mat_new, glb_face_order=order_new,
                             feature_labels_spec=np.array(spec))
                if 'body_part' in z:
                    z_new['body_part_glb_order'] = z['body_part'][order_new]
                np.savez_compressed(tmp_npz, **z_new)
                os.replace(tmp_glb, glb_path)
                os.replace(tmp_npz, npz_path)
            entry = info['levels'][lv]
            entry['material_face_counts'] = np.bincount(mat_new, minlength=len(entry.get('material_face_counts') or names)).tolist()
            entry['feature_labels'] = rec
            records[lv] = rec
            new_ids[lv] = mat_new
            log(f'FEATURE_RELABEL {lv}: {rec["faces_changed"]} of {n} triangles changed')
        except Exception as error:  # noqa: BLE001 - this level keeps its files
            traceback.print_exc()
            tmp_glb.unlink(missing_ok=True)
            tmp_npz.unlink(missing_ok=True)
            records[lv] = {'spec': spec, 'error': f'{type(error).__name__}: {error}'}
            if lv in info['levels']:
                info['levels'][lv]['feature_labels'] = records[lv]
            log(f'FEATURE_RELABEL_FAILED {lv}: {error}')
    changed = {lv: m for lv, m in new_ids.items() if records[lv].get('faces_changed')}
    if changed and objects:                         # live session of reduce_model: the level objects are in the scene
        for lv, mat in changed.items():
            obj = bpy.data.objects.get(f'lod_error_{lv}')
            if obj is not None and len(obj.data.polygons) == len(mat):
                obj.data.polygons.foreach_set('material_index', mat.astype(np.int32))
                obj.data.update()
    elif changed and review_blend and (out / 'review.blend').is_file():
        try:
            update_review_blend(out / 'review.blend', changed)
        except Exception as error:  # noqa: BLE001
            traceback.print_exc()
            for lv in changed:
                records[lv]['review_blend_error'] = f'{type(error).__name__}: {error}'
            log(f'FEATURE_RELABEL review.blend not updated: {error}')
    return records


def update_review_blend(path, mats):
    """Open review.blend, set the material index of the polygons of lod_error_<lv> (mats: lv -> new mesh-order material ids), save it
    again (compressed, no .blend1). Resets the Blender session: call it last."""
    import bpy
    bpy.ops.wm.open_mainfile(filepath=str(path))
    for lv, mat in mats.items():
        obj = bpy.data.objects.get(f'lod_error_{lv}')
        if obj is None or len(obj.data.polygons) != len(mat):
            raise ValueError(f'review.blend: object lod_error_{lv} missing or with {None if obj is None else len(obj.data.polygons)} polygons, expected {len(mat)}')
        obj.data.polygons.foreach_set('material_index', mat.astype(np.int32))
        obj.data.update()
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=str(path), compress=True)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='venv python: plan the labelled-LOD role transfer of a module (feature label pass)')
    ap.add_argument('command', choices=['plan'])
    ap.add_argument('module_dir')
    ap.add_argument('--reduced', help='default <module_dir>/reduced')
    ap.add_argument('--wearcut', help='default <module_dir>/wearcut')
    ap.add_argument('--spec', required=True)
    ap.add_argument('--levels', default='0p4,0p2,0p1')
    a = ap.parse_args(argv)
    r = plan(a.reduced or Path(a.module_dir) / 'reduced', a.wearcut or Path(a.module_dir) / 'wearcut', a.spec, tuple(a.levels.split(',')))
    print('FEATURE_PLAN_DONE', json.dumps({lv: v.get('faces_changed', v.get('error')) for lv, v in r.items()}))


if __name__ == '__main__':
    main()
