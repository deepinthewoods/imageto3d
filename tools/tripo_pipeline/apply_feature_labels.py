"""Apply an opt-in feature label pass (studs / chain-mail rings) to modules that are already converted, with backups (2026-10-25).

    characters/.venv/bin/python tools/tripo_pipeline/apply_feature_labels.py [<module_dir> ...] [--sets S,S] [--spec SPEC] [--jobs 2]
        [--phase rebuild|fit|all] [--fit-jobs 3] [--backup-root DIR] [--report DIR] [--no-pieces] [--retry-locked N] [--dry-run]
    characters/.venv/bin/python tools/tripo_pipeline/apply_feature_labels.py table --report DIR     # markdown table from the per-module records

<module_dir> = models/<set>/<job-id>/<race>/<module>; without any, every body_armor / right_gauntlet_boot module of the sets named by
`--sets` (default: the keys of config.json `feature_labels.sets`; `*_attempt*` folders are skipped). The spec of a module is `--spec`, else
what `feature_hooks.resolve_spec` finds (job record, config.json). Nothing is generated or submitted: no Tripo / Hyper3D / formy3d call.

Per module, `rebuild` (the stages that make new files):
  1. a module whose conversion worker lock is alive (`.module_locks/<race>__<module>.lock`) is skipped and retried at the end (`--retry-locked`);
  2. BACKUP of everything that is about to be replaced into `<backup-root>/<set>/<job>/<race>/<module>/` (default
     experiments/feature_labels_20261025/backups): `reduced/` (without source_import.blend / source_geometry.npz), `wearcut/` and
     `segment_first/` (both without `prep/`, which is only read), `fit/`, `split_step.json` and the module's `.tripo-job.json` entry; marker
     `.backup_complete`; an existing complete backup is never overwritten;
  3. `rewear.py --feature-labels SPEC` logic (Blender): the wearable is re-cut with the pass (all three levels, sidecar arrays,
     wearable.blend) and the labelled levels (GLB, colours npz, manifest, review.blend) get the corrected roles;
  4. when the module has `segment_first/` pieces: they are rebuilt with the pass exactly as convert_job.py does (segment_first.py
     `--feature-labels`, same prep); a failure here restores only the old pieces and is reported (`pieces_failed`);
  5. checks (cut wearable, spec recorded, labelled triangle counts, flat shading of every rebuilt GLB) and the per-module record
     `<report>/modules/<set>/<job>/<race>/<module>.json` (numbers before / after read from the backup and the new files).
  Any failure before step 4 restores the module from its backup (and its job record entry); the module is then reported `failed`.
`fit` re-runs body_fit.py (`--method full` and `--method vertex`, `--force`) on the rebuilt modules, because their wearables / pieces
changed; its verdict goes into the module record (`fit.<method>.clear`: no wearable or piece vertex inside the body and, for `full`, at most
2 cm2 of body poking through the armour; `needs_review`: body_fit flagged a piece or warned). `--phase fit` on already rebuilt modules
re-derives the verdicts (it re-runs body_fit; `--phase verdict` only re-reads fit/*.json).
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import convert_job as cj  # noqa: E402
import feature_hooks  # noqa: E402
import rewear  # noqa: E402
import wearcut_rules as R  # noqa: E402

MODULES = ('body_armor', 'right_gauntlet_boot')
LEVELS = ('0p4', '0p2', '0p1')
DEFAULT_BACKUP = ROOT / 'experiments' / 'feature_labels_20261025' / 'backups'
DEFAULT_REPORT = ROOT / 'experiments' / 'feature_labels_20261025' / 'applied'
ITEMS = {'reduced': ('source_import.blend', 'source_geometry.npz'), 'wearcut': ('prep',), 'segment_first': ('prep',), 'fit': ()}
PIECE_OUTPUTS = ('pieces', 'levels', 'highpoly_labels.npz', 'segment_first.json', 'segment_first.log', 'feature_labels.npz')


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def identity(module_dir):
    p = Path(module_dir).resolve().parts
    return p[-4], p[-3], p[-2], p[-1]


def discover(sets):
    out = []
    for s in sets:
        for module in MODULES:
            for m in sorted((ROOT / 'models' / s).glob(f'*/*/{module}')):
                if (m / 'reduced' / 'manifest.json').is_file() and not any('_attempt' in part for part in m.parts[-4:]):
                    out.append(m)
    return out


# ----------------------------------------------------------------------------------------------- backup / restore

def backup_path(root, module_dir):
    return Path(root).joinpath(*identity(module_dir))


def make_backup(module_dir, backup):
    marker = backup / '.backup_complete'
    if marker.is_file():
        return 'kept'
    if backup.exists():
        shutil.rmtree(backup)                    # an unfinished copy of an untouched module
    backup.mkdir(parents=True)
    for name, excl in ITEMS.items():
        src = module_dir / name
        if not src.is_dir():
            continue
        shutil.copytree(src, backup / name, ignore=lambda d, names, src=src, excl=excl: [n for n in names if Path(d) == src and n in excl])
    if (module_dir / 'split_step.json').is_file():
        shutil.copy2(module_dir / 'split_step.json', backup / 'split_step.json')
    entry = job_entry(module_dir)
    (backup / 'job_record_entry.json').write_text(json.dumps(entry, indent=1) + '\n')
    marker.write_text(datetime.datetime.now().isoformat(timespec='seconds') + '\n')
    return 'created'


def restore_dir(module_dir, backup, name):
    dst, src = module_dir / name, backup / name
    if not src.is_dir():
        return
    excl = ITEMS.get(name, ())
    if dst.is_dir():
        for child in list(dst.iterdir()):
            if child.name in excl:
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    shutil.copytree(src, dst, dirs_exist_ok=True)


def restore(module_dir, backup, names=('reduced', 'wearcut', 'segment_first', 'fit'), files=('split_step.json',), record=True):
    for name in names:
        restore_dir(module_dir, backup, name)
    for name in files:
        if (backup / name).is_file():
            shutil.copy2(backup / name, module_dir / name)
    if record:
        restore_job_entry(module_dir, backup)


def job_record_path(module_dir):
    return rewear.job_record_path(module_dir)


def job_entry(module_dir):
    path = job_record_path(module_dir)
    if path is None:
        return None
    _, _, race, module = identity(module_dir)
    return (cj.read_record(path).get('module_conversion') or {}).get(f'{race}/{module}')


def restore_job_entry(module_dir, backup):
    path = job_record_path(module_dir)
    f = backup / 'job_record_entry.json'
    if path is None or not f.is_file():
        return
    entry = json.loads(f.read_text(encoding='utf-8-sig'))
    _, _, race, module = identity(module_dir)

    def change(r):
        mc = r.setdefault('module_conversion', {})
        if entry is None:
            mc.pop(f'{race}/{module}', None)
        else:
            mc[f'{race}/{module}'] = entry
    cj.update_record(path, change)


# ----------------------------------------------------------------------------------------------- measuring

def glb_area_by_role(path, manifest, roles):
    gv, gf, names = R.read_glb(path)
    v = R.from_glb(gv, manifest)
    a = np.linalg.norm(np.cross(v[gf[:, 1]] - v[gf[:, 0]], v[gf[:, 2]] - v[gf[:, 0]]), axis=1) / 2
    return {r: float(a[names == r].sum()) for r in roles if (names == r).any()}, int(len(gf))


def flat_share(path):
    """Share of triangles whose three corner normals equal the face normal (1.0 = flat shaded, like every pipeline GLB)."""
    import struct
    data = Path(path).read_bytes()
    n = struct.unpack_from('<I', data, 12)[0]
    g = json.loads(data[20:20 + n])
    bo = 20 + n
    binb = data[bo + 8:bo + 8 + struct.unpack_from('<I', data, bo)[0]]

    def acc(i):
        a = g['accessors'][i]
        bv = g['bufferViews'][a['bufferView']]
        dt = {5126: np.float32, 5125: np.uint32, 5123: np.uint16}[a['componentType']]
        nc = {'SCALAR': 1, 'VEC3': 3}[a['type']]
        arr = np.frombuffer(binb, dt, a['count'] * nc, bv.get('byteOffset', 0) + a.get('byteOffset', 0))
        return arr.reshape(-1, nc) if nc > 1 else arr
    flat = tot = 0
    for m in g['meshes']:
        for pr in m['primitives']:
            if 'NORMAL' not in pr['attributes']:
                tot += len(acc(pr['indices'])) // 3
                continue
            pos, nrm, idx = acc(pr['attributes']['POSITION']).astype(float), acc(pr['attributes']['NORMAL']).astype(float), acc(pr['indices']).reshape(-1, 3).astype(int)
            t = pos[idx]
            fn = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
            fn /= np.maximum(np.linalg.norm(fn, axis=1, keepdims=True), 1e-20)
            flat += int(((nrm[idx] * fn[:, None, :]).sum(2).min(1) > .999).sum())
            tot += len(idx)
    return flat / max(tot, 1)


def piece_counts(sf_json):
    try:
        d = json.loads(Path(sf_json).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    return {lv: v.get('wearable_triangles') for lv, v in (d.get('levels') or {}).items()}


def module_stats(module_dir, backup):
    """Numbers before (backup) / after (module) for the record."""
    module_dir = Path(module_dir)
    new = json.loads((module_dir / 'reduced' / 'manifest.json').read_text(encoding='utf-8-sig'))
    old = json.loads((backup / 'reduced' / 'manifest.json').read_text(encoding='utf-8-sig'))
    roles = ['base'] + [g['role'] for g in new['palette']['garments']]
    fl = (new.get('wearable') or {}).get('feature_labels') or {}
    out = {'spec': fl.get('spec'), 'pass': {k: v for k, v in fl.items() if k not in ('levels', 'spec')}, 'levels': {}}
    for lv in LEVELS:
        entry = {}
        n_lv, o_lv = (new.get('levels') or {}).get(lv) or {}, (old.get('levels') or {}).get(lv) or {}
        fr = (fl.get('levels') or {}).get(lv) or {}
        entry['labelled'] = {'triangles_before': o_lv.get('triangles'), 'triangles_after': n_lv.get('triangles'),
                             'triangles_changed_role': fr.get('faces_changed'), 'error': fr.get('error'),
                             'role_area_before': fr.get('role_area_before'), 'role_area_after': fr.get('role_area_after')}
        try:
            a_old, t_old = glb_area_by_role(backup / 'reduced' / f'lod_error_{lv}__wearable.glb', old, roles)
            a_new, t_new = glb_area_by_role(module_dir / 'reduced' / f'lod_error_{lv}__wearable.glb', new, roles)
            entry['wearable'] = {'triangles_before': t_old, 'triangles_after': t_new, 'role_area_before': a_old, 'role_area_after': a_new,
                                 'flat_share': flat_share(module_dir / 'reduced' / f'lod_error_{lv}__wearable.glb')}
        except Exception as error:  # noqa: BLE001
            entry['wearable'] = {'error': f'{type(error).__name__}: {error}'}
        try:
            entry['labelled']['flat_share'] = flat_share(module_dir / 'reduced' / f'lod_error_{lv}.glb')
        except Exception as error:  # noqa: BLE001
            entry['labelled']['flat_error'] = str(error)
        out['levels'][lv] = entry
    p_new, p_old = piece_counts(module_dir / 'segment_first' / 'segment_first.json'), piece_counts(backup / 'segment_first' / 'segment_first.json')
    if p_new or p_old:
        out['pieces'] = {'wearable_triangles_before': p_old, 'wearable_triangles_after': p_new}
        try:
            sfj = json.loads((module_dir / 'segment_first' / 'segment_first.json').read_text(encoding='utf-8-sig'))
            out['pieces']['feature_labels'] = {k: v for k, v in (sfj.get('feature_labels') or {}).items()}
        except (OSError, ValueError):
            pass
    return out


# ----------------------------------------------------------------------------------------------- one module

def rebuild_pieces(module_dir, backup, spec):
    """Rebuild segment_first/pieces with the pass (convert_job.segment_first_split on the existing prep)."""
    sf = module_dir / 'segment_first'
    for name in PIECE_OUTPUTS:
        p = sf / name
        if p.exists():
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    _, _, race, module = identity(module_dir)
    cj.segment_first_split(module_dir, module, spec)
    step_path = module_dir / 'split_step.json'
    step = json.loads(step_path.read_text(encoding='utf-8-sig')) if step_path.is_file() else {'requested': 'segment-first', 'method': 'segment-first'}
    step.update(feature_labels=spec, feature_labels_rebuilt=datetime.date.today().isoformat())
    cj.write_record(step_path, step)
    jp = job_record_path(module_dir)
    if jp is not None:
        def change(r):
            e = r.setdefault('module_conversion', {}).get(f'{race}/{module}')
            if e is not None:
                e.setdefault('split', {}).update(feature_labels=spec)
        cj.update_record(jp, change)


def process(module_dir, a):
    module_dir = Path(module_dir).resolve()
    set_name, job_id, race, module = identity(module_dir)
    name = '/'.join(identity(module_dir))
    rec = {'module': name, 'status': None}
    jp = job_record_path(module_dir)
    record = cj.read_record(jp) if jp else None
    spec, source = (feature_hooks.normalise_spec(a.spec), '--spec') if a.spec else feature_hooks.resolve_spec(set_name, module, record)
    rec.update(spec=spec, spec_source=source)
    if spec is None:
        rec.update(status='skipped', why='no feature label spec for this set/module')
        return rec
    manifest_path = module_dir / 'reduced' / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    if rewear.module_busy(module_dir):
        rec.update(status='locked', why='a conversion worker holds this module')
        return rec
    done = ((manifest.get('wearable') or {}).get('feature_labels') or {}).get('spec') == spec
    sf_done = False
    sfj = module_dir / 'segment_first' / 'segment_first.json'
    has_pieces = (module_dir / 'segment_first' / 'pieces' / 'pieces.json').is_file() and (module_dir / 'segment_first' / 'prep' / 'prep.npz').is_file()
    if sfj.is_file():
        try:
            sf_done = (json.loads(sfj.read_text(encoding='utf-8-sig')).get('feature_labels') or {}).get('spec') == spec
        except ValueError:
            pass
    if a.dry_run:
        rec.update(status='dry-run', wearable_done=done, pieces=('done' if sf_done else 'todo') if has_pieces else 'none')
        return rec
    backup = backup_path(a.backup_root, module_dir)
    t0 = time.monotonic()
    if not done or (has_pieces and not sf_done and not a.no_pieces):
        rec['backup'] = make_backup(module_dir, backup)
    if not done:
        r = rewear.rewear(module_dir, LEVELS, force=False, metrics=False, dry_run=False, job_record=True, feature_labels=spec)
        rec['rewear'] = {k: r.get(k) for k in ('status', 'method', 'seconds', 'fallback_reason', 'why', 'error')}
        manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
        spec_now = ((manifest.get('wearable') or {}).get('feature_labels') or {}).get('spec')
        ok = r['status'] == 'done' and (manifest.get('wearable') or {}).get('method') == 'cut' and spec_now == spec
        if not ok:
            restore(module_dir, backup, names=('reduced', 'wearcut'))
            rec.update(status='failed', why=f'rewear: {r}', restored=True)
            return rec
        lvl_err = {lv: v['error'] for lv, v in (((manifest['wearable'].get('feature_labels') or {}).get('levels')) or {}).items() if v.get('error')}
        if lvl_err:
            rec['labelled_levels_not_relabelled'] = lvl_err
    if has_pieces and not sf_done and not a.no_pieces:
        t1 = time.monotonic()
        try:
            rebuild_pieces(module_dir, backup, spec)
            rec['pieces'] = {'status': 'done', 'seconds': round(time.monotonic() - t1, 1)}
        except Exception as error:  # noqa: BLE001 - the wearable stays rebuilt, the old pieces come back
            restore(module_dir, backup, names=('segment_first',), files=('split_step.json',), record=False)
            rec['pieces'] = {'status': 'failed', 'error': f'{type(error).__name__}: {error}', 'restored_old_pieces': True}
    elif has_pieces:
        rec['pieces'] = {'status': 'already' if sf_done else 'skipped (--no-pieces)'}
    try:
        rec['stats'] = module_stats(module_dir, backup)
    except Exception as error:  # noqa: BLE001
        rec['stats_error'] = f'{type(error).__name__}: {error}'
    flats = [v['wearable'].get('flat_share') for v in (rec.get('stats') or {}).get('levels', {}).values() if v.get('wearable', {}).get('flat_share') is not None]
    rec['flat_shaded_all'] = bool(flats) and min(flats) > .999
    rec['status'] = 'pieces_failed' if (rec.get('pieces') or {}).get('status') == 'failed' else 'done'
    rec['seconds'] = round(time.monotonic() - t0, 1)
    return rec


def write_module_record(report, rec):
    p = Path(report) / 'modules' / (rec['module'] + '.json')
    p.parent.mkdir(parents=True, exist_ok=True)
    old = {}
    try:
        old = json.loads(p.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        pass
    if old.get('stats') and not rec.get('stats') and rec.get('status') in ('done',):
        rec['stats'] = old['stats']          # a re-run that found everything done keeps the earlier numbers
    if old.get('fit') and 'fit' not in rec:
        rec['fit'] = old['fit']
    p.write_text(json.dumps(rec, indent=1, default=float) + '\n')


def rebuild_phase(mods, a):
    results = {}

    def one(m):
        try:
            r = process(m, a)
        except Exception as error:  # noqa: BLE001
            r = {'module': '/'.join(identity(m)), 'status': 'failed', 'why': f'{type(error).__name__}: {error}'}
        log(f'REBUILD {r["status"]:13} {r["module"]} {r.get("seconds", "")} {r.get("why") or ""}')
        if r['status'] not in ('locked', 'dry-run'):
            write_module_record(a.report, r)
        return r
    todo = list(mods)
    for attempt in range(a.retry_locked + 1):
        with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
            for m, r in zip(todo, pool.map(one, todo)):
                results[str(m)] = r
        todo = [m for m in todo if results[str(m)]['status'] == 'locked']
        if not todo:
            break
        log(f'{len(todo)} module(s) locked by conversion workers; retry {attempt + 1}/{a.retry_locked} in {a.retry_wait}s')
        time.sleep(a.retry_wait)
    return results


# ----------------------------------------------------------------------------------------------- body fit

def fit_verdict(module_dir, method):
    """From fit/body_fit[_vertex].json: `clear` = after the fit no wearable / piece vertex is inside the body (penetrating area <= 0.05%)
    and, for method `full` (the only one that removes it), the body does not poke through the armour (<= 2 cm2); `needs_review` = body_fit
    itself flagged a piece (rigid scale / push fallback) or warned; the numbers per level / part are kept."""
    sys.path.insert(0, str(HERE))
    import body_fit
    f = Path(module_dir) / 'fit' / ('body_fit.json' if method == 'full' else f'body_fit_{method}.json')
    rec = json.loads(f.read_text(encoding='utf-8-sig'))
    rows = body_fit.module_summary(rec)
    bad = [r for r in rows if r.get('after_pen_pct') is None or r['after_pen_pct'] > .05 or (method == 'full' and (r.get('after_poke_cm2') or 0) > 2.)]
    review = [f'{r["level"]}/{r["part"]}' for r in rows if 'needs_review' in r['flags']]
    return {'method': method, 'clear': not bad, 'needs_review': bool(review or rec.get('warnings')), 'review_parts': review,
            'levels': {f'{r["level"]}/{r["part"]}': {'after_pen_pct': r.get('after_pen_pct'), 'after_max_mm': r.get('after_max_mm'),
                                                    'after_poke_cm2': r.get('after_poke_cm2'), 'flags': r['flags']} for r in rows},
            'warnings': rec.get('warnings'), 'worst_after_pen_pct': max([r.get('after_pen_pct') or 0 for r in rows] or [0]),
            'worst_after_poke_cm2': max([r.get('after_poke_cm2') or 0 for r in rows] or [0])}


def fit_phase(mods, a):
    mods = [str(m) for m in mods]
    for method in ('full', 'vertex'):
        cmd = [sys.executable, str(HERE / 'body_fit.py'), *mods, '--method', method, '--force', '--jobs', str(a.fit_jobs),
               '--summary', str(Path(a.report) / f'body_fit_{method}_summary.json')]
        log('FIT', method, len(mods), 'modules')
        with open(Path(a.report) / f'body_fit_{method}.log', 'w') as h:
            run = subprocess.run(cmd, stdout=h, stderr=subprocess.STDOUT, cwd=ROOT)
        log('FIT', method, 'exit', run.returncode)
    out = {}
    for m in mods:
        name = '/'.join(identity(m))
        verdict = {}
        for method in ('full', 'vertex'):
            try:
                verdict[method] = fit_verdict(m, method)
            except Exception as error:  # noqa: BLE001
                verdict[method] = {'clear': False, 'error': f'{type(error).__name__}: {error}'}
        out[name] = verdict
        p = Path(a.report) / 'modules' / (name + '.json')
        if p.is_file():
            rec = json.loads(p.read_text(encoding='utf-8-sig'))
            rec['fit'] = verdict
            p.write_text(json.dumps(rec, indent=1, default=float) + '\n')
    return out


# ----------------------------------------------------------------------------------------------- table

def table(report):
    rows = []
    for p in sorted((Path(report) / 'modules').rglob('*.json')):
        rows.append(json.loads(p.read_text(encoding='utf-8-sig')))
    hdr = ['module', 'spec', 'status', 'found / fixed (studs) or holes (rings)', 'tertiary|centre area 0p2 labelled before > after',
           'wearable 0p2 area before > after', 'tris 0p2 labelled', 'tris 0p2 wearable before > after', 'tris 0p2 pieces before > after', 'flat', 'fit clear full/vertex (* = body_fit needs_review)', 'notes']
    md = ['| ' + ' | '.join(hdr) + ' |', '|' + '---|' * len(hdr)]
    f = lambda x, n=4: '-' if x is None else (f'{x:.{n}f}' if isinstance(x, float) else str(x))
    for r in rows:
        st, p = r.get('stats') or {}, (r.get('stats') or {}).get('pass') or {}
        spec = r.get('spec') or ''
        role = 'tertiary' if spec == 'studs' else ('secondary' if spec == 'rings:secondary' else None)
        lv = (st.get('levels') or {}).get('0p2') or {}
        lab, wear = lv.get('labelled') or {}, lv.get('wearable') or {}
        found = (f'{p.get("studs")} / {p.get("fixed")}' if spec == 'studs' else f'{p.get("holes")} holes, voided {f(p.get("voided_area"), 3)}') if p else '-'
        if role:
            la = f'{f((lab.get("role_area_before") or {}).get(role))} > {f((lab.get("role_area_after") or {}).get(role))}'
            wa = f'{f((wear.get("role_area_before") or {}).get(role))} > {f((wear.get("role_area_after") or {}).get(role))}'
        else:    # void: the secondary (centre) role is gone from the cut, report the wearable area instead
            la = f'primary {f((lab.get("role_area_before") or {}).get("primary"))} > {f((lab.get("role_area_after") or {}).get("primary"))}'
            wa = f'sum {f(sum((wear.get("role_area_before") or {}).values()))} > {f(sum((wear.get("role_area_after") or {}).values()))}'
        pc = ((st.get('pieces') or {}))
        pb, pa = (pc.get('wearable_triangles_before') or {}).get('0p2'), (pc.get('wearable_triangles_after') or {}).get('0p2')
        fit = r.get('fit') or {}
        fc = '/'.join(('yes' if (fit.get(m) or {}).get('clear') else ('no' if fit.get(m) else '-')) + ('*' if (fit.get(m) or {}).get('needs_review') else '')
                      for m in ('full', 'vertex'))
        notes = []
        if r.get('labelled_levels_not_relabelled'):
            notes.append('labelled not relabelled: ' + ','.join(r['labelled_levels_not_relabelled']))
        if (r.get('pieces') or {}).get('status') == 'failed':
            notes.append('pieces failed (old kept)')
        if r.get('why'):
            notes.append(str(r['why'])[:80])
        rows_md = [r['module'], spec, r['status'], found, la, wa, f(lab.get('triangles_after')), f'{f(wear.get("triangles_before"))} > {f(wear.get("triangles_after"))}',
                   f'{f(pb)} > {f(pa)}' if pc else '-', 'yes' if r.get('flat_shaded_all') else 'NO', fc, '; '.join(notes)]
        md.append('| ' + ' | '.join(rows_md) + ' |')
    text = '\n'.join(md) + '\n'
    (Path(report) / 'table.md').write_text(text)
    return text


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == 'table':
        ap = argparse.ArgumentParser()
        ap.add_argument('cmd')
        ap.add_argument('--report', default=str(DEFAULT_REPORT))
        print(table(ap.parse_args(argv).report))
        return 0
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('modules', nargs='*')
    ap.add_argument('--sets', help='comma list of set ids (default: config.json feature_labels.sets)')
    ap.add_argument('--spec', help='force this spec for every module (default: resolved per set)')
    ap.add_argument('--jobs', type=int, default=2, help='modules rebuilt in parallel (a Blender of ~2 GB each)')
    ap.add_argument('--fit-jobs', type=int, default=3)
    ap.add_argument('--phase', choices=['rebuild', 'fit', 'verdict', 'all'], default='all')
    ap.add_argument('--backup-root', default=str(DEFAULT_BACKUP))
    ap.add_argument('--report', default=str(DEFAULT_REPORT))
    ap.add_argument('--no-pieces', action='store_true')
    ap.add_argument('--retry-locked', type=int, default=3)
    ap.add_argument('--retry-wait', type=int, default=300)
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args(argv)
    if a.spec:
        feature_hooks.normalise_spec(a.spec)
    Path(a.report).mkdir(parents=True, exist_ok=True)
    mods = [Path(m) if Path(m).is_absolute() else ROOT / m for m in a.modules]
    if not mods:
        sets = a.sets.split(',') if a.sets else list((feature_hooks.config_block().get('sets') or {}))
        mods = discover(sets)
    if not mods:
        raise SystemExit('no modules')
    log(f'{len(mods)} modules; phase {a.phase}')
    results = {}
    if a.phase in ('rebuild', 'all'):
        results = rebuild_phase(mods, a)
        (Path(a.report) / 'rebuild_summary.json').write_text(json.dumps(
            {k: {kk: vv for kk, vv in v.items() if kk != 'stats'} for k, v in results.items()}, indent=1, default=float) + '\n')
    if a.phase == 'verdict':
        for m in mods:
            name = '/'.join(identity(m))
            p = Path(a.report) / 'modules' / (name + '.json')
            if p.is_file():
                rec = json.loads(p.read_text(encoding='utf-8-sig'))
                rec['fit'] = {meth: fit_verdict(m, meth) for meth in ('full', 'vertex')}
                p.write_text(json.dumps(rec, indent=1, default=float) + '\n')
    if a.phase in ('fit', 'all') and not a.dry_run:
        fit_mods = [m for m in mods if results.get(str(m), {}).get('status', 'done') in ('done', 'pieces_failed', 'skipped', 'already')
                    and (a.phase == 'fit' or results.get(str(m), {}).get('status') in ('done', 'pieces_failed'))]
        fit_phase(fit_mods, a)
    table(a.report)
    counts = {}
    for r in results.values():
        counts[r['status']] = counts.get(r['status'], 0) + 1
    log('APPLY_FEATURE_LABELS_DONE', json.dumps(counts))
    return 1 if any(r['status'] in ('failed', 'locked') for r in results.values()) else 0


if __name__ == '__main__':
    sys.exit(main())
