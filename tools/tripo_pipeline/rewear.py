"""Backfill: re-run ONLY the wearable step of already converted modules from their saved outputs (2026-10-24).

    python3 tools/tripo_pipeline/rewear.py <module_dir> [<module_dir> ...] [--all] [--jobs N] [--levels 0p4,0p2,0p1] [--force]
        [--dry-run] [--no-metrics] [--no-job-record] [--summary FILE] [--match SUBSTRING] [--feature-labels SPEC]

<module_dir> = models/<set>/<job-id>/<race>/<module>. `--all` takes every converted module under models/ (reduced/manifest.json, a known
module, no *_attempt* folders); `--match` keeps only paths containing the substring. Nothing is generated or submitted: no Tripo, Hyper3D,
formy3d or browser call, no Studio; only source.glb, reduced/manifest.json and the saved level GLBs are read. For each module it runs
`wearcut_blender.py rewear` in Blender (front matter ~10 s, cut ~20 s) and then compares the old and the new wearable.

Kept, never overwritten: the previous wearable GLBs as `reduced/lod_error_<lv>__wearable_faces.glb`, their sidecar arrays as
`lod_error_<lv>_colors_wearable_faces.npz`, the previous `wearable.blend` as `wearable_faces.blend` and the manifest as
`manifest_before_rewear.json`. Updated: the new wearable GLBs / sidecar arrays / wearable.blend, `reduced/manifest.json` (`wearable`,
per level `wearable_*`, `mirror`) and `.tripo-job.json` `module_conversion.<race>/<module>.wearable` (+ `wearable_backfill`). The full labelled
levels (`lod_error_<lv>.glb`, their evidence arrays), review.blend, highpoly.blend and the pieces / segment_first outputs are not touched.
A module that already has a cut wearable is skipped unless `--force`.
`--feature-labels SPEC` (studs | rings:secondary | rings:void[:auto|all|<z>], feature_hooks.py) re-cuts the wearable with an opt-in label pass
on the high-poly labels AND relabels the labelled full LODs (lod_error_<lv>.glb, _colors.npz, manifest, review.blend; feature_relabel.py); it
reuses the cached wearcut/prep, skips a module that already ran the same spec, and writes `wearcut/feature_labels.npz` / `.json`. It does NOT
keep the files it replaces (the `_faces` copies only protect the pre-cut wearable): use tools/tripo_pipeline/apply_feature_labels.py, which
backs the module up first and also rebuilds the rigid pieces and the body fit. Metrics (before -> after, per level) go to
`<module>/wearcut/rewear_metrics.json` and the summary file (default experiments/rewear_<date>/summary.json).
Needs python with numpy + scipy for the metrics (it re-runs itself with characters/.venv when scipy is missing).
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import portable  # noqa: E402
BLENDER = portable.default_blender()
portable.utf8_environment()
MODULES = ('body_armor', 'right_gauntlet_boot', 'outfit')
JOB_STATES = ('processing', 'completed', 'multiview_ready', 'processing_views', 'failed')
LEVELS = ('0p4', '0p2', '0p1')


def ensure_scipy():
    try:
        import scipy  # noqa: F401
        return
    except ImportError:
        pass
    venv = next((Path(c) for c in portable.venv_python_candidates(ROOT) if Path(c).is_file()), None)
    if os.environ.get('REWEAR_REEXEC') or venv is None:
        raise SystemExit('rewear.py needs numpy + scipy (characters/.venv/bin/python)')
    os.execve(str(venv), [str(venv), *sys.argv], dict(os.environ, REWEAR_REEXEC='1'))


def discover():
    out = []
    for man in sorted((ROOT / 'models').glob('*/*/*/*/reduced/manifest.json')):
        parts = man.relative_to(ROOT / 'models').parts
        if parts[3] in MODULES and not any('_attempt' in p for p in parts[:4]):
            out.append(man.parents[1])
    return out


def identity(module_dir):
    p = Path(module_dir).resolve().parts
    return p[-4], p[-3], p[-2], p[-1]       # set, job id, race, module


def job_record_path(module_dir):
    set_name, job, _, _ = identity(module_dir)
    for state in JOB_STATES:
        p = ROOT / state / f'{set_name}--{job}' / '.tripo-job.json'
        if p.is_file():
            return p
    return None


def module_busy(module_dir):
    """A conversion worker holds this module's lock (it is being converted right now). The holder's own subprocesses (convert_job's
    align step) set $MODULE_LOCK_OWNER to its pid: a lock held by exactly that pid is not busy for them; any other live holder is."""
    set_name, job, race, module = identity(module_dir)
    lock = ROOT / 'models' / set_name / job / '.module_locks' / f'{race}__{module}.lock'
    try:
        pid = int(lock.read_text(encoding='utf-8').strip())
    except (OSError, ValueError):
        return False
    owner = os.environ.get('MODULE_LOCK_OWNER', '').strip()
    if owner.isdigit() and int(owner) == pid:
        return False
    return portable.pid_alive(pid)


def update_job_record(module_dir, wearable, rewear_info, feature_labels=None):
    path = job_record_path(module_dir)
    if path is None:
        return None
    sys.path.insert(0, str(HERE))
    import convert_job
    from wearcut_rules import wearable_record
    _, _, race, module = identity(module_dir)
    key = f'{race}/{module}'

    def change(record):
        entry = record.setdefault('module_conversion', {}).get(key)
        if entry is None:
            return
        entry['wearable'] = wearable_record(wearable)
        entry['wearable_backfill'] = rewear_info
        if feature_labels:
            entry['feature_labels'] = feature_labels
    convert_job.update_record(path, change)
    return str(path.relative_to(ROOT))


# ----------------------------------------------------------------------------------------------- metrics (old vs new wearable)

def level_surface(path, manifest, pal):
    """Wearable / level GLB -> welded (v, f, role) in the normalised frame."""
    import numpy as np
    sys.path.insert(0, str(HERE))
    import wearcut as wc
    gv, gf, names = wc.read_glb(path)
    v = wc.from_glb(gv, manifest)
    lut = {n: i for i, n in enumerate(['base', *pal.roles])}
    role = np.array([lut.get(str(n), 0) for n in names], np.int64)
    vv, ff, at = wc.weld_with_attrs(v, gf, 2e-5, role=role)
    return vv, ff, at['role']


def metrics_for(module_dir, levels):
    """Before -> after per level from the saved files (old = __wearable_faces.glb, new = __wearable.glb)."""
    import numpy as np
    from scipy.spatial import cKDTree
    sys.path.insert(0, str(HERE))
    import wearcut as wc
    import symmetry as sym
    out = Path(module_dir) / 'reduced'
    wcdir = Path(module_dir) / 'wearcut'
    manifest = json.loads((out / 'manifest.json').read_text(encoding='utf-8-sig'))
    pal = wc.palette_from_dict(__import__('wearcut_rules').palette_dict(manifest))
    plane_d = json.loads((wcdir / 'plane.json').read_text(encoding='utf-8-sig'))
    plane = sym.Plane(plane_d['normal'], plane_d['offset'])
    prep = np.load(wcdir / 'prep' / 'prep.npz')
    hv, hf = prep['v'].astype(np.float64), prep['f'].astype(np.int64)
    hp_cent = hv[hf].mean(1)
    labels = np.load(wcdir / 'labels.npz')
    lab_new = labels['label'].astype(np.int64)
    area_hp = labels['area'].astype(np.float64)
    part_names = [str(n) for n in labels['part_names']]
    part = labels['part'].astype(np.int64)
    module = manifest['module']
    left = plane.signed(hp_cent) > 0
    footwear = (manifest.get('wearable') or {}).get('footwear')
    if module == 'outfit':
        ids = [i for i, n in enumerate(part_names) if n in (('foot.l',) if footwear == 'ankle_cut' else ('foot.l', 'leg_stretch.l'))]
        left &= np.isin(part, ids)
    pair = module in ('right_gauntlet_boot',) or (module == 'outfit' and (footwear in ('component', 'ankle_cut') or
                                                                            (manifest.get('wearable', {}).get('pair') or {}).get('status') == 'pair'))
    result = {}
    for lv in levels:
        old_p, new_p = out / f'lod_error_{lv}__wearable_faces.glb', out / f'lod_error_{lv}__wearable.glb'
        if not old_p.is_file() or not new_p.is_file():
            continue
        ov, of, orl = level_surface(old_p, manifest, pal)
        nv, nf, nrl = level_surface(new_p, manifest, pal)
        ent = {}
        for name, (v, f) in (('old', (ov, of)), ('new', (nv, nf))):
            bm, dense = wc.boundary_metrics(v, f)
            per_loop = bm['per_loop']
            ent[name] = {'faces': int(len(f)), 'area': float(wc.tri_area(v, f).sum()),
                         'boundary': {k: val for k, val in bm.items() if k not in ('per_loop', 'dense_per_loop')},
                         'mirror_mismatch': wc.mirror_mismatch(dense, plane), 'surface_deviation': wc.surface_error(v, f, hp_cent)}
        ent['area_ratio_new_over_old'] = ent['new']['area'] / max(ent['old']['area'], 1e-20)
        full = out / f'lod_error_{lv}.glb'
        if full.is_file():                       # label change on the high-poly surface: the pipeline's labelled level vs the cut labels
            fv, ffu, fr = level_surface(full, manifest, pal)
            pts, ids = wc.surface_samples(fv, ffu, 150000, seed=4)
            _, near = cKDTree(pts).query(hp_cent)
            lab_old = fr[ids[near]]
            keep = ~left if pair else np.ones(len(hp_cent), bool)
            flip = ((lab_old > 0) != (lab_new > 0)) & keep
            role_only = (lab_old != lab_new) & ~flip & keep
            tot = area_hp[keep].sum()
            ent['label_change'] = {'base_garment_flip_area_share': float(area_hp[flip].sum() / tot),
                                   'role_only_area_share': float(area_hp[role_only].sum() / tot),
                                   'garment_area_share_old_labels': float(area_hp[(lab_old > 0) & keep].sum() / tot),
                                   'garment_area_share_new_labels': float(area_hp[(lab_new > 0) & keep].sum() / tot)}
        result[lv] = ent
    return result


def feature_labels_job_entry(rec, when):
    """The short record of a feature label pass for .tripo-job.json module_conversion.<race>/<module>.feature_labels."""
    levels = rec.get('levels') or {}
    keep = ('mode', 'studs', 'before', 'fixed', 'holes', 'centre', 'changed_area_share_of_garment', 'voided_area', 'skipped')
    return {'spec': rec.get('spec'), 'date': when, **{k: rec[k] for k in keep if k in rec},
            'lod_triangles_changed': {lv: v.get('faces_changed') for lv, v in levels.items()}}


def summary_row(module_dir, metrics, manifest):
    p = Path(module_dir).resolve()
    rel = str(p.relative_to(ROOT / 'models')) if p.is_relative_to(ROOT / 'models') else str(p)
    w = manifest.get('wearable') or {}
    row = {'module': rel, 'method': w.get('method'), 'footwear': w.get('footwear'), 'status': w.get('status'), 'fallback_reason': w.get('fallback_reason'),
           'seconds': w.get('seconds'), 'backend': None}
    sub = Path(module_dir) / 'studio_submission.json'
    try:
        row['backend'] = json.loads(sub.read_text(encoding='utf-8-sig')).get('backend', 'tripo')
    except (OSError, ValueError):
        pass
    for lv, m in (metrics or {}).items():
        o, n = m['old'], m['new']
        ob, nb = o['boundary'], n['boundary']
        om, nm = o['mirror_mismatch'] or {}, n['mirror_mismatch'] or {}
        row[lv] = {'faces': [o['faces'], n['faces']], 'area_ratio': round(m['area_ratio_new_over_old'], 3),
                   'loops': [ob['loops'], nb['loops']], 'boundary_pct_height': [round(ob['boundary_length_pct_height']), round(nb['boundary_length_pct_height'])],
                   'rough_rms_pct': [ob['rough_rms_pct'], nb['rough_rms_pct']], 'sawtooth_excess': [ob['jag_excess'], nb['jag_excess']],
                   'sharp_turns_per_100pct': [ob['sharp_turns_per_100pct'], nb['sharp_turns_per_100pct']],
                   'mirror_mean_pct': [om.get('mean'), nm.get('mean')], 'mirror_p95_pct': [om.get('p95'), nm.get('p95')], 'mirror_max_pct': [om.get('max'), nm.get('max')],
                   'deviation_p95_pct': [o['surface_deviation']['p95_pct'], n['surface_deviation']['p95_pct']],
                   'label_flip_area_share': (m.get('label_change') or {}).get('base_garment_flip_area_share'),
                   'label_role_only_area_share': (m.get('label_change') or {}).get('role_only_area_share')}
    return row


# ----------------------------------------------------------------------------------------------- one module

def rewear(module_dir, levels, force=False, metrics=True, dry_run=False, job_record=True, feature_labels=None):
    module_dir = Path(module_dir).resolve()
    rel = str(module_dir.relative_to(ROOT / 'models')) if module_dir.is_relative_to(ROOT / 'models') else str(module_dir)
    manifest_path = module_dir / 'reduced' / 'manifest.json'
    if not manifest_path.is_file():
        return {'module': rel, 'status': 'skipped', 'why': 'no reduced/manifest.json'}
    info = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    if info.get('module') not in MODULES:
        return {'module': rel, 'status': 'skipped', 'why': f'module {info.get("module")!r}'}
    sys.path.insert(0, str(HERE))
    import feature_hooks
    spec = feature_hooks.normalise_spec(feature_labels) if feature_labels else None
    done_spec = ((info.get('wearable') or {}).get('feature_labels') or {}).get('spec')
    if (info.get('wearable') or {}).get('method') in ('cut', 'mixed') and not force and (spec is None or spec == done_spec):
        return {'module': rel, 'status': 'skipped', 'why': 'already has a cut wearable (--force to redo)' if spec is None else f'already ran {spec}'}
    if module_busy(module_dir):
        return {'module': rel, 'status': 'skipped', 'why': 'a conversion worker holds this module'}
    if dry_run:
        return {'module': rel, 'status': 'dry-run', 'module_type': info.get('module')}
    (module_dir / 'wearcut').mkdir(exist_ok=True)
    log = module_dir / 'wearcut' / 'rewear.log'
    t0 = time.monotonic()
    command = ([] if portable.WINDOWS else ['nice', '-n', '5']) + [BLENDER, '-b', '--factory-startup', '-t', '3', '--python-exit-code', '1', '--python', str(HERE / 'wearcut_blender.py'), '--',
               'rewear', str(module_dir), '--levels', ','.join(levels)] + (['--force'] if force else [])
    command += ['--feature-labels', spec] if spec else []
    with open(log, 'w') as handle:
        run = subprocess.run(command, stdout=handle, stderr=subprocess.STDOUT, cwd=ROOT)
    seconds = round(time.monotonic() - t0, 1)
    if run.returncode != 0:
        tail = ' | '.join(log.read_text(errors='replace').strip().splitlines()[-4:])
        return {'module': rel, 'status': 'failed', 'seconds': seconds, 'error': f'blender exited {run.returncode}: {tail}'}
    info = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    w = info.get('wearable') or {}
    rec = {'module': rel, 'status': 'done' if w.get('method') in ('cut', 'mixed') else 'fallback', 'method': w.get('method'), 'seconds': seconds,
           'footwear': w.get('footwear'), 'fallback_reason': w.get('fallback_reason')}
    when = datetime.datetime.now().isoformat(timespec='seconds')
    fl_rec = w.get('feature_labels')
    rec['job_record'] = job_record and update_job_record(module_dir, info, {'date': when, 'method': w.get('method'), 'seconds': seconds,
                                                              'fallback_reason': w.get('fallback_reason'), 'tool': 'rewear.py'},
                                                         feature_labels=feature_labels_job_entry(fl_rec, when) if fl_rec else None)
    if metrics and w.get('method') in ('cut', 'mixed'):
        try:
            m = metrics_for(module_dir, levels)
            (module_dir / 'wearcut' / 'rewear_metrics.json').write_text(json.dumps(m, indent=1, default=float) + '\n')
            rec['row'] = summary_row(module_dir, m, info)
        except Exception as error:  # noqa: BLE001 - the backfill itself succeeded
            rec['metrics_error'] = f'{type(error).__name__}: {error}'
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument('modules', nargs='*')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--match', help='only module paths containing this substring')
    ap.add_argument('--jobs', type=int, default=1, help='modules processed in parallel (each Blender uses 3 threads, ~2 GB)')
    ap.add_argument('--levels', default=','.join(LEVELS))
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--no-metrics', action='store_true')
    ap.add_argument('--no-job-record', action='store_true', help='do not touch .tripo-job.json (modules outside models/ or tests)')
    ap.add_argument('--feature-labels', metavar='SPEC', help='opt-in label pass: studs | rings:secondary | rings:void[:auto|all|<z>] (see the module docstring)')
    ap.add_argument('--summary', help='summary json (default experiments/rewear_<date>/summary.json)')
    a = ap.parse_args(argv)
    ensure_scipy()
    mods = [Path(m) for m in a.modules]
    if a.all:
        mods += discover()
    mods = [m if m.is_absolute() else ROOT / m for m in mods]
    if a.match:
        mods = [m for m in mods if a.match in str(m)]
    if not mods:
        raise SystemExit('no modules (give module folders or --all)')
    levels = tuple(a.levels.split(','))
    summary = Path(a.summary) if a.summary else ROOT / 'experiments' / f'rewear_{datetime.date.today():%Y%m%d}' / 'summary.json'
    summary.parent.mkdir(parents=True, exist_ok=True)
    results = {}

    def one(m):
        r = rewear(m, levels, a.force, not a.no_metrics, a.dry_run, not a.no_job_record, a.feature_labels)
        print(f'REWEAR {r["status"]:8} {r["module"]} {r.get("seconds", "")} {r.get("footwear") or ""} {r.get("fallback_reason") or r.get("why") or r.get("error") or ""}', flush=True)
        return r
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
        for r in pool.map(one, mods):
            results[r['module']] = r
            summary.write_text(json.dumps(results, indent=1, default=float) + '\n')
    bad = [k for k, r in results.items() if r['status'] == 'failed']
    print(f'REWEAR_SUMMARY {len(results)} modules: ' + json.dumps({s: sum(r["status"] == s for r in results.values()) for s in sorted({r["status"] for r in results.values()})}) + f' -> {summary}')
    if bad:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
