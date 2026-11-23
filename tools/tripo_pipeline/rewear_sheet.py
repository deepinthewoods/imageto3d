"""Contact sheet of a backfill: old (faces) wearable vs new (cut) wearable of rewear.py-processed modules.

    characters/.venv/bin/python tools/tripo_pipeline/rewear_sheet.py <out_dir> <module_dir> [<module_dir> ...] [--level 0p4] [--no-render]

Per module: a metrics tile (from <module>/wearcut/rewear_metrics.json), old above new (front | back, flat garment colours) and close-ups of the
opening regions with the open boundary drawn red (Blender Workbench via symmetry_cut_render.py, which needs the run folder layout of the
prototype: current_lod_<lv>.npz = old, proto_lod_<lv>.npz = new, render_plan.*; built under <module>/wearcut/report/). Writes
<out_dir>/sheet_<n>_<set>_<race>_<module>.jpg and overview.jpg (one tile per module). Pillow only besides numpy/scipy; helpers come from
experiments/symmetry_cut_20261024/make_sheets.py.
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / 'experiments/symmetry_cut_20261024'))
import make_sheets as ms  # noqa: E402  (font, text_tile, vstack, hstack, open_rgb, sheet)
import rewear  # noqa: E402
import symmetry as sym  # noqa: E402
import symmetry_cut as sc  # noqa: E402  (plan_renders: which opening regions to photograph)
import wearcut as wc  # noqa: E402

BLENDER = '/usr/local/bin/blender'


def f2(x, n=2):
    return '-' if x is None else f'{x:.{n}f}'


def prepare_run_dir(module_dir, lv):
    module_dir = Path(module_dir)
    rd = module_dir / 'wearcut' / 'report'
    rd.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((module_dir / 'reduced' / 'manifest.json').read_text(encoding='utf-8-sig'))
    pal = wc.palette_from_dict(__import__('wearcut_rules').palette_dict(manifest))
    plane_d = json.loads((module_dir / 'wearcut' / 'plane.json').read_text(encoding='utf-8-sig'))
    plane = sym.Plane(plane_d['normal'], plane_d['offset'])
    out = module_dir / 'reduced'
    for name, glb in (('current', f'lod_error_{lv}__wearable_faces.glb'), ('proto', f'lod_error_{lv}__wearable.glb')):
        v, f, role = rewear.level_surface(out / glb, manifest, pal)
        np.savez_compressed(rd / f'{name}_lod_{lv}.npz', v=v.astype(np.float32), f=f.astype(np.int32), role=role.astype(np.int8))
    sc.plan_renders(rd, manifest['module'], pal, plane, lv)
    return rd, manifest


def render(rd):
    log = rd / 'render.log'
    with open(log, 'w') as h:
        subprocess.run(['nice', '-n', '5', BLENDER, '-b', '--factory-startup', '-t', '3', '--python', str(HERE / 'symmetry_cut_render.py'), '--', str(rd)],
                       stdout=h, stderr=subprocess.STDOUT, cwd=ROOT, check=True)


def metrics_lines(module_dir, manifest, lv):
    m = json.loads((Path(module_dir) / 'wearcut' / 'rewear_metrics.json').read_text(encoding='utf-8-sig'))
    w = manifest.get('wearable') or {}
    cur, new = m[lv]['old'], m[lv]['new']
    cb, nb = cur['boundary'], new['boundary']
    cm, nm = cur['mirror_mismatch'] or {}, new['mirror_mismatch'] or {}
    lc = m[lv].get('label_change') or {}
    lines = [f"# method {w.get('method')}, {w.get('seconds')} s;  pooling mode {(w.get('pooling') or {}).get('pool_mode')}, pooled area {f2((w.get('pooling') or {}).get('pool_weight_mean_area'))}",
             f"# {lv} level (old faces wearable -> new cut wearable)",
             f"faces {cur['faces']} -> {new['faces']}   (" + ', '.join(f"{k}: {m[k]['old']['faces']} -> {m[k]['new']['faces']}" for k in m if k != lv) + ')',
             f"garment area x{m[lv]['area_ratio_new_over_old']:.3f}   boundary loops {cb['loops']} -> {nb['loops']}, length {cb['boundary_length_pct_height']:.0f}% -> {nb['boundary_length_pct_height']:.0f}% of height",
             f"boundary roughness (rms off a 1.5%-smoothed loop) {f2(cb['rough_rms_pct'], 3)} -> {f2(nb['rough_rms_pct'], 3)} % height",
             f"sawtooth excess {f2(cb['jag_excess'], 3)} -> {f2(nb['jag_excess'], 3)}   sharp turns per 100% of boundary {f2(cb['sharp_turns_per_100pct'], 1)} -> {f2(nb['sharp_turns_per_100pct'], 1)}",
             f"left/right opening mismatch mean {f2(cm.get('mean'), 3)} -> {f2(nm.get('mean'), 4)}, p95 {f2(cm.get('p95'), 2)} -> {f2(nm.get('p95'), 4)}, max {f2(cm.get('max'), 2)} -> {f2(nm.get('max'), 4)} % height",
             f"deviation from the high-poly p95 {f2(cur['surface_deviation']['p95_pct'], 2)} -> {f2(new['surface_deviation']['p95_pct'], 2)} % height",
             f"label change vs the labelled level: base<->garment {100 * (lc.get('base_garment_flip_area_share') or 0):.1f}% of area, role only {100 * (lc.get('role_only_area_share') or 0):.1f}%"]
    for key in ('off_body', 'riser'):
        r = w.get(key)
        if r and r.get('enabled'):
            lines.append(f"{key}: " + json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items() if k != 'enabled'})[:140])
    if w.get('pair'):
        lines.append('# pair: ' + json.dumps({k: v for k, v in w['pair'].items() if k not in ('rule', 'ankle')})[:150])
    if w.get('footwear'):
        lines.append('# footwear: ' + w['footwear'])
    return lines


def main(argv):
    lv = '0p4'
    render_it = True
    args = []
    i = 0
    while i < len(argv):
        if argv[i] == '--level':
            lv = argv[i + 1]
            i += 1
        elif argv[i] == '--no-render':
            render_it = False
        else:
            args.append(argv[i])
        i += 1
    out_dir, mods = Path(args[0]), [Path(m) if Path(m).is_absolute() else ROOT / m for m in args[1:]]
    out_dir.mkdir(parents=True, exist_ok=True)
    tiles = []
    tmp = out_dir / '_tiles'
    tmp.mkdir(exist_ok=True)
    for n, md in enumerate(mods, 1):
        tag = '_'.join(md.parts[-4:-3] + md.parts[-2:])
        title = f'{n} {md.parts[-4]} {md.parts[-2]} {md.parts[-1]}'
        rd, manifest = prepare_run_dir(md, lv)
        if render_it:
            render(rd)
        lines = metrics_lines(md, manifest, lv)
        ms.text_tile(tmp / f'{n}_metrics.png', title, lines)
        cur_fb, pro_fb = ms.open_rgb(rd / 'r_current_fb.png'), ms.open_rgb(rd / 'r_proto_fb.png')
        ms.vstack([cur_fb, pro_fb], tmp / f'{n}_whole.png')
        images = [tmp / f'{n}_metrics.png', tmp / f'{n}_whole.png']
        plan = json.loads((rd / 'render_plan.json').read_text(encoding='utf-8-sig'))
        for k in range(len(plan['regions'])):
            for view in ('front', 'obl'):
                ms.hstack(ms.open_rgb(rd / f'r_close{k}_{view}_cur.png'), ms.open_rgb(rd / f'r_close{k}_{view}_pro.png'), tmp / f'{n}_region{k}_{view}.png')
                images.append(tmp / f'{n}_region{k}_{view}.png')
        ms.sheet(out_dir / f'sheet_{n:02d}_{tag}.jpg', title, images, 2, 900)
        # overview tile: old | new whole body, then the first opening region front (old | new) and oblique (old | new)
        W = 900
        cell = Image.new('RGB', (2 * W, W + 450 + 150), (236, 236, 238))
        cell.paste(cur_fb.crop((0, 0, 900, 900)), (0, 150))
        cell.paste(pro_fb.crop((0, 0, 900, 900)), (W, 150))
        if plan['regions']:
            for j, (v_, c_) in enumerate((('front', 'cur'), ('front', 'pro'), ('obl', 'cur'), ('obl', 'pro'))):
                cell.paste(ms.open_rgb(rd / f'r_close0_{v_}_{c_}.png').resize((450, 450)), (j * 450, 150 + W))
        d = ImageDraw.Draw(cell)
        d.text((20, 8), title, fill=(20, 20, 24), font=ms.font(40, True))
        d.text((20, 62), lines[3] if len(lines) > 3 else '', fill=(10, 60, 140), font=ms.font(26))
        d.text((20, 100), lines[6][:150], fill=(10, 60, 140), font=ms.font(26))
        p = tmp / f'{n}_overview.png'
        cell.save(p)
        tiles.append(p)
        print('SHEET', title, flush=True)
    ms.sheet(out_dir / 'overview.jpg', f'wearable: old (faces) top-left, new (cut) top-right; opening close-ups bottom (old | new, front, oblique), {lv}', tiles, 2, 900)
    print('SHEETS_DONE', out_dir / 'overview.jpg')


if __name__ == '__main__':
    main(sys.argv[1:])
