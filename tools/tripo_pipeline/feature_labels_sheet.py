"""Before / after renders and contact sheet for feature_labels.py studs runs (PROTOTYPE 2026-10-25).

    characters/.venv/bin/python tools/tripo_pipeline/feature_labels_sheet.py <studs_root> <module_dir>:<out_dir> ... [--level 0p4] [--sheet sheet.png]

Per module (out_dir = what `feature_labels.py studs <module_dir> --out <out_dir> --recut` wrote): Blender Workbench renders, flat role colours,
before | after, written next to studs.json as `view_*.png`:
  view_hp_fb         the whole high-poly mesh front | back (before top, after bottom)
  view_hp_close_*    close-ups of the densest studded area at the front and back (before | after)
  view_wearable_*    the wearable cut at --level: old (the module's reduced/lod_error_<lv>__wearable.glb) | re-cut (recut/wearcut_lod_<lv>.npz)
  view_lod_*         the labelled full LOD at --level: old labels | relabelled
`--sheet` stacks every module's tiles into one contact sheet. Scratch inputs go to a temp dir, nothing under models/ is written.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import feature_labels as fl  # noqa: E402
import wearcut as wc  # noqa: E402

BLENDER = '/usr/local/bin/blender'


def font(n):
    try:
        return ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', n)
    except OSError:
        return ImageFont.load_default()


def dense_centres(rows, side):
    """Centre of the densest group of studs on the front (side -1) or back (+1) of the body."""
    pts = np.array([r['centre'] for r in rows])
    pts = pts[np.sign(pts[:, 1]) == side] if len(pts) else pts
    if len(pts) == 0:
        return None
    d = np.linalg.norm(pts[:, None, :] - pts[None], axis=2)
    k = (d < .2).sum(1)
    return pts[int(k.argmax())]


def hstack(ims, gap=8):
    w = sum(i.width for i in ims) + gap * (len(ims) - 1)
    out = Image.new('RGB', (w, max(i.height for i in ims)), (20, 20, 22))
    x = 0
    for i in ims:
        out.paste(i, (x, 0))
        x += i.width + gap
    return out


def vstack(ims, gap=8):
    h = sum(i.height for i in ims) + gap * (len(ims) - 1)
    out = Image.new('RGB', (max(i.width for i in ims), h), (20, 20, 22))
    y = 0
    for i in ims:
        out.paste(i, (0, y))
        y += i.height + gap
    return out


def label(im, text):
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, im.width, 34), fill=(0, 0, 0))
    d.text((8, 4), text, fill=(255, 255, 255), font=font(22))
    return im


def build(module_dir, out_dir, level):
    module_dir, out_dir = Path(module_dir), Path(out_dir)
    info = json.loads((out_dir / 'studs.json').read_text(encoding='utf-8-sig'))
    M = fl.load_module(module_dir)
    s = np.load(out_dir / 'studs_labels.npz')
    tmp = Path(tempfile.mkdtemp(prefix='fl_sheet_'))
    np.savez(tmp / 'hp_old.npz', v=M['v'].astype(np.float32), f=M['f'].astype(np.int32), role=s['label_old'])
    np.savez(tmp / 'hp_new.npz', v=M['v'].astype(np.float32), f=M['f'].astype(np.int32), role=s['label_new'])
    from rewear import level_surface
    wear_glb = module_dir / 'reduced' / f'lod_error_{level}__wearable.glb'
    vo, fo, ro = level_surface(wear_glb, M['manifest'], M['pal'])
    np.savez(tmp / 'wear_old.npz', v=vo.astype(np.float32), f=fo.astype(np.int32), role=ro.astype(np.int8))
    jobs, names = [], []

    def add(tag, which, npz, key, view, centre, half, size):
        out = out_dir / f'_{tag}_{which}.png'
        jobs.append({'out': str(out), 'npz': str(npz), 'key': key, 'view': view, 'centre': list(map(float, centre)), 'half': half, 'size': list(size)})
        return out

    full_c = (0., 0., 1.)
    shots = {}
    for which, npz in (('old', tmp / 'hp_old.npz'), ('new', tmp / 'hp_new.npz')):
        shots[('hp_front', which)] = add('hpf', which, npz, 'role', 'front', full_c, 1.15, (800, 800))
        shots[('hp_back', which)] = add('hpb', which, npz, 'role', 'back', full_c, 1.15, (800, 800))
    rows = info['rows']
    closes = []
    for side, view, nm in ((-1, 'front', 'front'), (1, 'back', 'back')):
        c = dense_centres(rows, side)
        if c is not None:
            closes.append((nm, view, c))
            for which, npz in (('old', tmp / 'hp_old.npz'), ('new', tmp / 'hp_new.npz')):
                shots[(f'hp_close_{nm}', which)] = add(f'hpc_{nm}', which, npz, 'role', view, c, .22, (700, 700))
    rc = out_dir / 'recut'
    new_wear = rc / f'wearcut_lod_{level}.npz'
    if new_wear.is_file():
        for which, npz in (('old', tmp / 'wear_old.npz'), ('new', new_wear)):
            shots[('wear_front', which)] = add('wf', which, npz, 'role', 'front', full_c, 1.15, (800, 800))
            for nm, view, c in closes:
                shots[(f'wear_close_{nm}', which)] = add(f'wc_{nm}', which, npz, 'role', view, c, .22, (700, 700))
    lab_npz = rc / f'lod_{level}_labelled.npz'
    if lab_npz.is_file():
        for which, key in (('old', 'role_old'), ('new', 'role_new')):
            for nm, view, c in closes[:1]:
                shots[(f'lod_close_{nm}', which)] = add(f'lc_{nm}', which, lab_npz, key, view, c, .22, (700, 700))
    (tmp / 'jobs.json').write_text(json.dumps(jobs))
    log = tmp / 'render.log'
    with open(log, 'w') as h:
        subprocess.run([BLENDER, '-b', '--factory-startup', '-t', '3', '--python', str(HERE / 'feature_labels_render.py'), '--', str(tmp / 'jobs.json')],
                       stdout=h, stderr=subprocess.STDOUT, cwd=ROOT, check=True)
    names = sorted({k[0] for k in shots})
    tiles = {}
    for nm in names:
        a, b = Image.open(shots[(nm, 'old')]).convert('RGB'), Image.open(shots[(nm, 'new')]).convert('RGB')
        label(a, f'{nm}: before')
        label(b, f'{nm}: after')
        tile = hstack([a, b])
        tile.save(out_dir / f'view_{nm}.png')
        tiles[nm] = tile
    for p in out_dir.glob('_*.png'):
        p.unlink()
    shutil.rmtree(tmp, ignore_errors=True)
    return tiles, info


def stats_tile(title, info, width):
    sp = info['spill']
    lines = [title,
             f"studs found {info['studs']} (geometry only {info['studs_geometry_only']}, rejected candidates {len(info['rejected'])}); radius {info['radius']:.4f}",
             f"before: whole {info['before']['full']}, partial {info['before']['partial']}, none {info['before']['none']}  -> fixed {info['fixed']}",
             f"to tertiary: {info['faces_to_tertiary']} faces, area {info['area_to_tertiary']:.4f}; spill removed: {sp.get('spill_components', 0)} blobs, area {sp.get('spill_area', 0):.4f} ({sp.get('spill_faces', 0)} faces)"]
    im = Image.new('RGB', (width, 150), (20, 20, 22))
    d = ImageDraw.Draw(im)
    for i, t in enumerate(lines):
        d.text((10, 8 + i * 34), t, fill=(255, 255, 255) if i == 0 else (200, 220, 255), font=font(28 if i == 0 else 22))
    return im


def main(argv):
    level, sheet = '0p4', None
    args, i = [], 0
    while i < len(argv):
        if argv[i] == '--level':
            level = argv[i + 1]
            i += 1
        elif argv[i] == '--sheet':
            sheet = argv[i + 1]
            i += 1
        else:
            args.append(argv[i])
        i += 1
    mods = [a.split(':') for a in args]
    overview = []
    for md, od in mods:
        md = Path(md) if Path(md).is_absolute() else ROOT / md
        od = Path(od) if Path(od).is_absolute() else ROOT / od
        tiles, info = build(md, od, level)
        title = f'{md.parts[-4]} {md.parts[-2]} {md.parts[-1]}'
        order = [k for k in ('hp_front', 'hp_back', 'hp_close_front', 'hp_close_back', 'wear_front', 'wear_close_front', 'wear_close_back', 'lod_close_front')
                 if k in tiles]
        w = max(tiles[k].width for k in order)
        col = vstack([stats_tile(title, info, w)] + [tiles[k] for k in order])
        col.save(od / 'view_all.png')
        small = [stats_tile(title, info, tiles['hp_front'].width)] + [tiles[k] for k in order[:1] + [k for k in order if k.startswith('hp_close')][:1] + [k for k in order if k == 'wear_close_front']]
        overview.append(vstack(small))
        print('SHEET', od / 'view_all.png', flush=True)
    if sheet and overview:
        h = max(o.height for o in overview)
        W = hstack([o for o in overview], 12)
        Wd = W.width
        W.save(sheet)
        print('SHEET_DONE', sheet, W.size)


if __name__ == '__main__':
    main(sys.argv[1:])
