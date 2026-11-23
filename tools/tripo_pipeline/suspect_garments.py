"""Suspect source garments (2026-11-20): modules whose aligned wearable cannot be repaired by fitting because the source garment itself is broken (the Tripo / Pixal3D model
came out as a band around the belly, a torso shell with the chest missing, ...). They are reported so the user can re-convert them; the fit lab does not try to fit them in batch runs.

    python tools/tripo_pipeline/suspect_garments.py [--all | <module_dir> ...] [--match S] [--jobs N] [--no-mark] [--out-dir experiments/fit_lab_20261118]

Detector (`analyse`, per module, from the aligned 0p4 wearable and the cached game base only, no fit): 30 000 area-weighted points of the garment surface in a KD-tree; for the
region the module type must cover, 6000 area-weighted points of the base, and `cover` = the share of those that have a garment point within `COVER_DIST` x body height.
  - `outfit` / `body_armor`: expected region = the torso (`spine_01`, `root`); suspect when the torso cover is below `MIN_TORSO_COVER` (0.6). Healthy modules of every race cover 0.8-1.0
    (the broken gnome ones 0.25-0.41); `area_ratio` (garment area / base area of torso + shoulders + upper arms + thighs) is reported as supporting evidence.
  - `right_gauntlet_boot`: expected region = right forearm + hand (+ right shin + foot for races with feet); suspect when its cover is below `MIN_LIMB_COVER`.
The result goes to the module's fit-lab `index.json` as `suspect: true|false` (+ `suspect_reasons`, `suspect_metrics`); the viewer may flag them later. The batch writes
`<out-dir>/suspect_garments.json` and `.md`.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import numpy as np   # noqa: E402
from scipy.spatial import cKDTree   # noqa: E402

COVER_DIST = 0.08          # x body height
MIN_TORSO_COVER = 0.6
MIN_LIMB_COVER = 0.35
TORSO = ('spine_01', 'root')
WIDE = ('spine_01', 'root', 'arm_stretch', 'thigh_stretch', 'shoulder')
LIMB_R = ('forearm_stretch', 'hand', 'leg_stretch', 'foot')
N_GARMENT, N_BODY = 30000, 6000


def _points(A, B, C, n, rng):
    area = 0.5 * np.linalg.norm(np.cross(B - A, C - A), axis=1)
    ti = rng.choice(len(A), n, p=area / area.sum())
    r1, r2 = rng.random(n), rng.random(n)
    f = r1 + r2 > 1
    r1[f], r2[f] = 1 - r1[f], 1 - r2[f]
    return A[ti] + r1[:, None] * (B[ti] - A[ti]) + r2[:, None] * (C[ti] - A[ti]), ti, area


def analyse(surf, body, module):
    """Metrics + verdict of one garment (bf.Surface of the aligned wearable) against its race's game base. Returns {'suspect', 'reasons', 'metrics'}."""
    rng = np.random.default_rng(0)
    T, U = surf.Tw, surf.U
    G, _, gar_area = _points(U[T[:, 0]], U[T[:, 1]], U[T[:, 2]], N_GARMENT, rng)
    tree = cKDTree(G)
    P, bi, _ = _points(body.A, body.B, body.C, N_BODY, rng)
    names = np.array([body.part_names[i] for i in body.part[bi]])
    base = np.array([n.split('.')[0] for n in names])
    dist, _ = tree.query(P)
    H = body.H
    wide = np.isin(base, WIDE)
    m = {'garment_area_cm2': float(gar_area.sum() * 1e4), 'cover_dist_m': COVER_DIST * H}
    m['area_ratio'] = m['garment_area_cm2'] / max(float(body.area.sum() * wide.mean() * 1e4), 1.)
    reasons = []
    suspect = False
    if module in ('outfit', 'body_armor'):
        sel = np.isin(base, TORSO)
        m['torso_cover'] = float((dist[sel] < COVER_DIST * H).mean()) if sel.any() else None
        if m['torso_cover'] is not None and m['torso_cover'] < MIN_TORSO_COVER:
            suspect = True
            reasons.append(f'the garment covers only {100 * m["torso_cover"]:.0f}% of the torso (within {COVER_DIST * H * 100:.0f} cm; healthy modules: 80-100%); '
                           f'garment area is {m["area_ratio"]:.2f} x the area of torso, shoulders, upper arms and thighs')
    elif module == 'right_gauntlet_boot':
        sel = np.isin(base, LIMB_R) & np.array([n.endswith('.r') for n in names])
        m['limb_cover'] = float((dist[sel] < COVER_DIST * H).mean()) if sel.any() else None
        if m['limb_cover'] is not None and m['limb_cover'] < MIN_LIMB_COVER:
            suspect = True
            reasons.append(f'the garment covers only {100 * m["limb_cover"]:.0f}% of the right forearm / hand / shin / foot')
    return {'suspect': suspect, 'reasons': reasons, 'metrics': m}


def analyse_module(module_dir):
    """Load the module's aligned wearable and analyse it. Returns the `analyse` dict plus identity fields."""
    import body_fit as bf
    from rewear import identity
    m = Path(module_dir).resolve()
    set_name, job, race, module = identity(m)
    body = bf.get_body(race)
    man = json.loads((m / 'reduced' / 'manifest.json').read_text(encoding='utf-8-sig'))
    surf = bf.Surface(bf.Glb(m / 'reduced' / 'lod_error_0p4__wearable_aligned.glb'), man, body)
    return {'set': set_name, 'job': job, 'race': race, 'module': module, **analyse(surf, body, module)}


def mark_index(module_dir, res):
    """Write `suspect` into the module's fit-lab index.json (kept by later fits: update_index only patches what it computes). A module that is not suspect and has no index is left alone."""
    import fit_lab as fl
    P = fl.module_paths(module_dir)
    if not res['suspect'] and not (P['dir'] / 'index.json').is_file():
        return
    fl.update_index(P, {'suspect': bool(res['suspect']), 'suspect_reasons': res['reasons'], 'suspect_metrics': res['metrics']})


def _one(args):
    out = []
    for m in args.modules:
        try:
            r = analyse_module(m)
            r['module_dir'] = str(Path(m).resolve().relative_to(ROOT)).replace('\\', '/')
            if not args.no_mark:
                mark_index(m, r)
        except Exception as error:   # noqa: BLE001
            r = {'module_dir': str(m), 'error': f'{type(error).__name__}: {error}'}
        out.append(r)
    Path(args.result).write_text(json.dumps(out), encoding='utf-8')


def write_reports(results, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = [r for r in results if 'error' not in r]
    sus = sorted((r for r in ok if r['suspect']), key=lambda r: (r['race'], r['set'], r['job']))
    by_race = {}
    for r in sus:
        by_race[r['race']] = by_race.get(r['race'], 0) + 1
    doc = {'analysed': len(ok), 'errors': [r for r in results if 'error' in r], 'suspect_count': len(sus), 'suspect_by_race': by_race,
           'rules': {'cover_dist_x_height': COVER_DIST, 'min_torso_cover': MIN_TORSO_COVER, 'min_limb_cover': MIN_LIMB_COVER}, 'suspect': sus}
    (out_dir / 'suspect_garments.json').write_text(json.dumps(doc, indent=1), encoding='utf-8')
    lines = ['# Suspect source garments', '', f'{len(sus)} of {len(ok)} modules (aligned 0p4 wearable) look broken at the source, so fitting cannot help; re-convert them '
             '(the fit lab skips them in batch runs and sets `suspect: true` in their fitlab index).', '',
             'By race: ' + ', '.join(f'{k} {v}' for k, v in sorted(by_race.items())), '',
             f'Rule: the share of the torso (spine / root part of the base) with a garment point within {COVER_DIST * 100:.0f}% of the body height must be at least {MIN_TORSO_COVER:.0%} '
             f'(outfit, body_armor); a gauntlet / boot must cover at least {MIN_LIMB_COVER:.0%} of the right forearm / hand / shin / foot.', '',
             '| set | job | race | module | metric | garment area cm2 | area ratio | reason |', '|---|---|---|---|---|---|---|---|']
    for r in sus:
        m = r['metrics']
        met = f'torso cover {m["torso_cover"]:.2f}' if 'torso_cover' in m else f'limb cover {m["limb_cover"]:.2f}'
        lines.append(f'| {r["set"]} | {r["job"]} | {r["race"]} | {r["module"]} | {met} | {m["garment_area_cm2"]:.0f} | {m["area_ratio"]:.2f} | {"; ".join(r["reasons"])} |')
    (out_dir / 'suspect_garments.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('modules', nargs='*')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--match')
    ap.add_argument('--jobs', type=int, default=8)
    ap.add_argument('--no-mark', action='store_true', help='do not write suspect into the fit-lab indexes')
    ap.add_argument('--out-dir', default=str(ROOT / 'experiments' / 'fit_lab_20261118'))
    ap.add_argument('--result', help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.result:        # worker
        _one(a)
        return
    from rewear import discover
    mods = [Path(m) if Path(m).is_absolute() else ROOT / m for m in a.modules]
    if a.all:
        mods += [m for m in discover() if (m / 'reduced' / 'lod_error_0p4__wearable_aligned.glb').is_file()]
    if a.match:
        mods = [m for m in mods if a.match in str(m)]
    if not mods:
        raise SystemExit('no modules')
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix='suspect_'))
    chunks = [mods[i::a.jobs] for i in range(min(a.jobs, len(mods)))]
    procs = []
    for i, ch in enumerate(chunks):
        res = tmp / f'r{i}.json'
        cmd = [sys.executable, str(Path(__file__).resolve()), '--result', str(res)] + (['--no-mark'] if a.no_mark else []) + [str(m) for m in ch]
        procs.append((res, subprocess.Popen(cmd, cwd=str(ROOT))))
    results = []
    for res, p in procs:
        p.wait()
        if res.is_file():
            results += json.loads(res.read_text(encoding='utf-8'))
    doc = write_reports(results, a.out_dir)
    print(f'SUSPECT analysed {doc["analysed"]} modules, {doc["suspect_count"]} suspect, by race {doc["suspect_by_race"]}, {len(doc["errors"])} errors')


if __name__ == '__main__':
    main()
