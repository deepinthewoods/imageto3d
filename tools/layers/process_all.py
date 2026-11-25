"""Module discovery for the layer builder's check tools (what is left of the prototype's process_all.py, 2026-11-23; its worker pool / batch driver was
removed with the move to tools/layers/: use build.py).

`jobs(races=None, lod='0p4', job=None)` lists, per race, every models/<set>/<job>/<race>/{outfit, body_armor (+ its gauntlet/boot module)} that has a fit (fit/align.json)
and a reduction (reduced/manifest.json + lod_error_<lod>.glb). Where a set has several jobs for one race the job named by `job` (exact job id) is
used, else the one whose reduced/manifest.json is newest, the job id breaking a tie: the choice never depends on the order the filesystem lists folders.
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SKIP_SETS = ('chain_shirt',)    # sets not used (user 2026-11-24: chain shirt dropped, chain mail is used instead)


def ready(p, lod='0p4'):
    return (p / 'fit' / 'align.json').is_file() and (p / 'reduced' / 'manifest.json').is_file() and (p / 'reduced' / f'lod_error_{lod}.glb').is_file()


def jobs(races=None, lod='0p4', job=None):
    cache = ROOT / 'tools' / 'preview' / 'cache' / 'bases'
    all_races = sorted(p.stem for p in cache.glob('*.npz') if not p.stem.endswith('_rig') and (cache / f'{p.stem}_rig.npz').is_file())
    races = [r for r in all_races if not races or r in races]
    out = []
    for race in races:
        best = {}
        for p in sorted(ROOT.glob(f'models/*/*/{race}/*')):
            if p.name not in ('outfit', 'body_armor', 'right_gauntlet_boot') or not ready(p, lod):
                continue
            if any(k in p.parts[-4] for k in SKIP_SETS) or (job is not None and p.parts[-3] != job):
                continue
            key = (p.parts[-4], p.name)
            t = ((p / 'reduced' / 'manifest.json').stat().st_mtime, p.parts[-3])
            if key not in best or t > best[key][0]:
                best[key] = (t, p)
        for (s, kind), (_, p) in sorted(best.items()):
            if kind == 'outfit':
                out.append({'kind': 'outfit', 'race': race, 'set': s, 'modules': [p.relative_to(ROOT).as_posix()]})
            elif kind == 'body_armor':
                g = p.parent / 'right_gauntlet_boot'
                if not ready(g, lod):
                    g = best.get((s, 'right_gauntlet_boot'), (0, None))[1]
                out.append({'kind': 'armour', 'race': race, 'set': s, 'modules': [p.relative_to(ROOT).as_posix()] + ([g.relative_to(ROOT).as_posix()] if g else [])})
    # names: geodiv's own default names (so curated results are the same entries), the full set name where two jobs would share one
    for j in out:
        s = j['set']
        if j['kind'] == 'outfit':
            j['name'] = f'outfit__{j["race"]}__{s.split("_outfit_")[-1][:28]}'
        else:
            pre = f'{s.split("_")[0]}_{s.split("_")[1]}'
            j['name'] = f'armour__{j["race"]}__{pre}' + (f'__arms_{pre}' if len(j['modules']) > 1 else '__base_arms')
    from collections import Counter
    c = Counter(j['name'] for j in out)
    for j in out:
        if c[j['name']] > 1:
            j['name'] = f'{j["kind"]}__{j["race"]}__{j["set"]}'
    return out
