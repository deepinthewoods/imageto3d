"""Before / after renders and contact sheets of ring_labels.py runs (EXPERIMENT 2026-10-25; nothing imports this).

    characters/.venv/bin/python tools/tripo_pipeline/ring_sheet.py <out_dir_root> <run_dir> [<run_dir> ...] [--no-render]

run_dir: a ring_labels.py output folder (ring_labels.json, ring_labels.npz, wearcut/, lods/). Per run it writes render/ (inputs and PNGs of the
Blender Workbench renders, flat role colours: removed faces are absent) and sheet.jpg beside the run; <out_dir_root>/sheet.png collects one
row per run. Pillow + numpy; Blender through ring_render.py.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
BLENDER = '/usr/local/bin/blender'
ROLE_RGB = np.array([[230, 25, 35], [25, 210, 40], [30, 65, 235], [245, 220, 20], [225, 25, 220]]) / 255.


def font(size, bold=False):
    for p in ('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def save_mesh(path, v, f, role):
    keep = role > 0
    np.savez(path, v=np.asarray(v, np.float32), f=np.asarray(f, np.int32)[keep], rgb=ROLE_RGB[np.asarray(role)[keep]].astype(np.float32))


def views_for(module, v, f, role, part, part_names):
    """Camera plan: whole garment front / back and close-ups at places that show the relief."""
    c = v[f[role > 0]].mean(1)
    lo, hi = c.min(0), c.max(0)
    mid = (lo + hi) / 2
    whole = float(max(hi[0] - lo[0], hi[2] - lo[2])) * 1.12
    plan = {'whole': (mid.tolist(), whole)}
    names = list(part_names)

    def centroid(prefixes):
        ids = [i for i, n in enumerate(names) if any(n.startswith(p) for p in prefixes)]
        m = np.isin(part[role > 0], ids)
        return (c[m].mean(0) if m.sum() > 50 else None), m.sum()
    if module == 'body_armor':
        z0, z1 = lo[2], hi[2]
        plan['closeA'] = ([0., 0., z0 + .72 * (z1 - z0)], .42)
        plan['closeB'] = ([0., 0., z0 + .28 * (z1 - z0)], .34)
    else:
        g, _ = centroid(['hand.r', 'forearm_stretch.r'])
        b, _ = centroid(['foot.r', 'leg_stretch.r'])
        plan['closeA'] = ((g if g is not None else mid).tolist(), .5)
        plan['closeB'] = ((b if b is not None else mid).tolist(), .8)
    return plan


def run_dir_inputs(rd):
    rd = Path(rd)
    info = json.loads((rd / 'ring_labels.json').read_text(encoding='utf-8-sig'))
    import ring_labels as rl
    M = rl.load_module(info['module_dir'])
    z = np.load(rd / 'ring_labels.npz')
    return info, M, z


def build(rd, levels=('0p1', '0p4')):
    """Writes render/*.npz and render/job.json; returns (job, tiles) where tiles name the PNG pairs of the sheet."""
    import ring_labels as rl
    rd = Path(rd)
    info, M, z = run_dir_inputs(rd)
    out = rd / 'render'
    out.mkdir(exist_ok=True)
    v, f = M['v'], M['f']
    before, after = z['label_texture'].astype(np.int64), z['label'].astype(np.int64)
    save_mesh(out / 'hp_before.npz', v, f, before)
    save_mesh(out / 'hp_after.npz', v, f, after)
    plan = views_for(M['module'], v, f, after if (after > 0).any() else before, M['part'], M['part_names'])
    meshes = {'hp': ('hp_before.npz', 'hp_after.npz')}
    for lv in levels:
        old = rl.lod_roles(Path(info['module_dir']) / 'reduced' / f'lod_error_{lv}__wearable.glb', M['manifest'], M['palette'])
        save_mesh(out / f'wear_old_{lv}.npz', *old)
        nz = np.load(rd / 'wearcut' / f'wearcut_lod_{lv}.npz')
        save_mesh(out / f'wear_new_{lv}.npz', nz['v'], nz['f'], nz['role'].astype(np.int64))
        meshes[f'wear_{lv}'] = (f'wear_old_{lv}.npz', f'wear_new_{lv}.npz')
        lz = rd / 'lods' / f'relabel_lod_{lv}.npz'
        if lz.is_file():
            L = np.load(lz)
            save_mesh(out / f'lod_old_{lv}.npz', L['v'], L['f'], L['role_old'].astype(np.int64))
            save_mesh(out / f'lod_new_{lv}.npz', L['v'], L['f'], L['role_new'].astype(np.int64))
            meshes[f'lod_{lv}'] = (f'lod_old_{lv}.npz', f'lod_new_{lv}.npz')
    rows = [('hp front', 'hp', 'whole', (0, -1, 0)), ('hp back', 'hp', 'whole', (0, 1, 0)),
            ('hp close A front', 'hp', 'closeA', (0, -1, 0)), ('hp close A oblique', 'hp', 'closeA', (.65, -.7, .25)),
            ('hp close B front', 'hp', 'closeB', (0, -1, 0)), ('hp close B oblique', 'hp', 'closeB', (.65, -.7, .25))]
    for lv in levels:
        rows += [(f'wearable {lv} front', f'wear_{lv}', 'whole', (0, -1, 0)), (f'wearable {lv} close A', f'wear_{lv}', 'closeA', (0, -1, 0)),
                 (f'wearable {lv} close A oblique', f'wear_{lv}', 'closeA', (.65, -.7, .25))]
        if f'lod_{lv}' in meshes:
            rows += [(f'labelled LOD {lv} front', f'lod_{lv}', 'whole', (0, -1, 0)), (f'labelled LOD {lv} close A', f'lod_{lv}', 'closeA', (0, -1, 0))]
    job = {'renders': []}
    tiles = []
    for title, key, view, d in rows:
        target, scale = plan[view]
        pair = []
        for side, npz in zip(('old', 'new'), meshes[key]):
            name = f"{title.replace(' ', '_')}_{side}.png"
            job['renders'].append({'out': str(out / name), 'size': [760, 760], 'light': 'studio', 'cam': {'target': target, 'dir': list(d), 'scale': scale},
                                   'meshes': [{'npz': str(out / npz)}]})
            pair.append(out / name)
        tiles.append((title, pair))
    (out / 'job.json').write_text(json.dumps(job))
    return out / 'job.json', tiles, info


def render(job):
    log = Path(job).with_suffix('.log')
    with open(log, 'w') as h:
        subprocess.run(['nice', '-n', '5', BLENDER, '-b', '--factory-startup', '-t', '3', '--python', str(HERE / 'ring_render.py'), '--', str(job)],
                       stdout=h, stderr=subprocess.STDOUT, cwd=ROOT, check=True)


def stats_lines(info):
    rp = info['ring_pass']
    ch = rp['changed']
    lines = [f"{info['module']}  ring={info['ring']} centre={info['centre']}   sigma {rp['scale']['sigma']:.3f}  protected {100 * rp['protected_area_share']:.0f}% of garment area",
             f"holes found {rp['holes']['count']}, median diameter {100 * rp['holes']['median_equiv_diameter'] / 2:.2f}% of height;  relabelled {100 * ch['area_share_of_garment']:.1f}% of garment area"
             f" ({ch['ring_to_centre']} ring->centre faces, {ch['to_ring']} centre->ring);  texture agreement before {100 * ch['texture_agreement_before_area_share']:.0f}%"]
    for lv, d in (info.get('wearable') or {}).items():
        o, n = d['old'], d['new']
        if 'hp_area_role_agreement' in o:
            lines.append(f"wearable {lv}: faces {o['faces']} -> {n['faces']};  hp ring/centre agreement {100 * o['hp_area_role_agreement']:.0f}% -> {100 * n['hp_area_role_agreement']:.0f}%;"
                         f"  centre patches {o['centre_patches']} -> {n['centre_patches']}")
        else:
            lines.append(f"wearable {lv}: faces {o['faces']} -> {n['faces']};  area over gap faces {100 * o['area_share_over_gap']:.1f}% -> {100 * n['area_share_over_gap']:.1f}%;"
                         f"  boundary loops {o['open_boundary_loops']} -> {n['open_boundary_loops']}")
    for lv, d in (info.get('lods') or {}).items():
        if 'old' in d:
            lines.append(f"labelled LOD {lv}: faces {d['faces']}, changed {100 * d['area_changed_share']:.1f}% of its area; agreement {100 * d['old'].get('hp_area_role_agreement', 0):.0f}% -> {100 * d['new'].get('hp_area_role_agreement', 0):.0f}%")
    return lines


def make_sheet(rd, tiles, info):
    W = 760
    rows = []
    for title, (a, b) in tiles:
        ia, ib = Image.open(a).convert('RGB'), Image.open(b).convert('RGB')
        row = Image.new('RGB', (2 * W + 30, W + 40), (236, 236, 238))
        row.paste(ia, (10, 36))
        row.paste(ib, (W + 20, 36))
        d = ImageDraw.Draw(row)
        d.text((12, 6), title + '   (before | after)', fill=(20, 20, 24), font=font(24, True))
        rows.append(row)
    lines = stats_lines(info)
    head = Image.new('RGB', (2 * W + 30, 40 + 30 * len(lines)), (255, 255, 255))
    d = ImageDraw.Draw(head)
    d.text((12, 6), Path(info['module_dir']).parts[-4] + ' / ' + Path(info['module_dir']).parts[-2] + ' / ' + info['module'], fill=(0, 0, 0), font=font(28, True))
    for i, ln in enumerate(lines):
        d.text((12, 44 + 30 * i), ln, fill=(10, 60, 140), font=font(20))
    cols = 1
    sheet = Image.new('RGB', (2 * W + 30, head.height + sum(r.height for r in rows)), (255, 255, 255))
    y = 0
    for im in [head, *rows]:
        sheet.paste(im, (0, y))
        y += im.height
    sheet.save(Path(rd) / 'sheet.jpg', quality=88)
    return Path(rd) / 'sheet.jpg'


def overview(out_root, entries):
    """One row per run: whole front before | after, close A before | after (hp), wearable close A old | new (0p1) + a stats line."""
    S = 420
    cells = []
    for rd, tiles, info in entries:
        by = dict(tiles)
        first = 'hp close B front' if info['module'] != 'body_armor' else 'hp front'
        picks = [(first, 0), (first, 1), ('hp close A oblique', 0), ('hp close A oblique', 1)]
        lv = '0p1' if 'wearable 0p1 close A oblique' in by else '0p4'
        picks += [(f'wearable {lv} close A oblique', 0), (f'wearable {lv} close A oblique', 1)]
        row = Image.new('RGB', (6 * S, S + 70), (236, 236, 238))
        for i, (t, k) in enumerate(picks):
            row.paste(Image.open(by[t][k]).convert('RGB').resize((S, S)), (i * S, 70))
        d = ImageDraw.Draw(row)
        rp = info['ring_pass']
        d.text((8, 4), f"{Path(info['module_dir']).parts[-4]} / {Path(info['module_dir']).parts[-2]} / {info['module']}   centre={info['centre']}   holes {rp['holes']['count']}   relabelled {100 * rp['changed']['area_share_of_garment']:.0f}% of garment",
               fill=(0, 0, 0), font=font(24, True))
        d.text((8, 38), first + ' before | after      hp close-up (oblique) before | after      wearable ' + lv + ' close-up (oblique) before | after',
               fill=(10, 60, 140), font=font(20))
        cells.append(row)
    sheet = Image.new('RGB', (6 * S, sum(c.height for c in cells)), (255, 255, 255))
    y = 0
    for c in cells:
        sheet.paste(c, (0, y))
        y += c.height
    Path(out_root).mkdir(parents=True, exist_ok=True)
    sheet.save(Path(out_root) / 'sheet.png')
    return Path(out_root) / 'sheet.png'


def main(argv):
    do_render = '--no-render' not in argv
    keep = '--keep-renders' in argv
    args = [a for a in argv if not a.startswith('--')]
    out_root = Path(args[0])
    entries = []
    for rd in args[1:]:
        rd = Path(rd)
        job, tiles, info = build(rd)
        if do_render and not (keep and all(Path(x).is_file() for _, pair in tiles for x in pair)):
            render(job)
        print('SHEET', make_sheet(rd, tiles, info), flush=True)
        entries.append((rd, tiles, info))
    print('OVERVIEW', overview(out_root, entries))


if __name__ == '__main__':
    main(sys.argv[1:])
