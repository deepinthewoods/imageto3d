"""Contact sheet before | after of the cut wearables a feature label pass changed (apply_feature_labels.py; 2026-10-25).

    characters/.venv/bin/python tools/tripo_pipeline/applied_sheet.py <out.png> <module_dir> [<module_dir> ...] [--level 0p2]
        [--backup-root DIR] [--tile 380]

Per module one row of four Blender Workbench tiles (flat role colours, ortho): front before | front after | close-up before | close-up
after. `before` = `<backup-root>/<set>/<job>/<race>/<module>/reduced/lod_error_<lv>__wearable.glb`, `after` = the module's own file. The
close-up is centred on the faces of the role the pass is about (studs: tertiary; rings: primary tubes) seen from the front. Renders and their
inputs go to <out>.work/ beside the sheet; nothing under models/ is written.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import wearcut_rules as R  # noqa: E402

BLENDER = '/usr/local/bin/blender'
ROLE_RGB = {'base': (230, 25, 35), 'primary': (25, 210, 40), 'secondary': (30, 65, 235), 'tertiary': (245, 220, 20), 'accent': (225, 25, 220),
            'joint': (20, 220, 235)}


def font(n):
    try:
        return ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', n)
    except OSError:
        return ImageFont.load_default()


def surface(path, manifest):
    gv, gf, names = R.read_glb(path)
    v = R.from_glb(gv, manifest)
    rgb = np.array([ROLE_RGB.get(str(n), (128, 128, 128)) for n in names], float) / 255.
    return v.astype(np.float32), gf.astype(np.int32), rgb.astype(np.float32), np.array([str(n) for n in names])


def process(mods, a, work):
    renders, rows = [], []
    for k, m in enumerate(mods):
        m = Path(m)
        set_name, job, race, module = m.parts[-4:]
        bdir = Path(a.backup_root).joinpath(set_name, job, race, module)
        manifest = json.loads((m / 'reduced' / 'manifest.json').read_text(encoding='utf-8-sig'))
        spec = ((manifest.get('wearable') or {}).get('feature_labels') or {}).get('spec') or ''
        focus = 'tertiary' if spec == 'studs' else 'primary'
        files = {}
        centre = None
        for tag, base in (('before', bdir), ('after', m)):
            p = base / 'reduced' / f'lod_error_{a.level}__wearable.glb'
            v, f, rgb, names = surface(p, manifest)
            npz = work / f'{k:02d}_{tag}.npz'
            np.savez_compressed(npz, v=v, f=f, rgb=rgb)
            files[tag] = str(npz)
            if tag == 'after':
                cent = v[f].mean(1)
                sel = (names == focus) & (cent[:, 1] < 0)
                sel = sel if sel.sum() > 20 else (names == focus)
                if module == 'right_gauntlet_boot':          # the gauntlet (viewer-left arm), not the middle of the pair
                    arm = (names == focus) & (cent[:, 2] > .8) & (cent[:, 0] < 0)
                    sel = arm if arm.sum() > 10 else sel
                centre = np.median(cent[sel], axis=0) if sel.any() else cent.mean(0)
                lo, hi = v.min(0), v.max(0)
        scale = max(hi[2] - lo[2], hi[0] - lo[0]) * 1.1
        close = [float(centre[0]), -1.0, float(centre[2])]
        tiles = []
        for tag in ('before', 'after'):
            for view, tgt, sc in (('front', [float((lo[0] + hi[0]) / 2), 0., float((lo[2] + hi[2]) / 2)], scale), ('close', close, .45 if module == 'body_armor' else .32)):
                out = work / f'{k:02d}_{tag}_{view}.png'
                renders.append({'out': str(out), 'bg': [.80, .80, .82], 'light': 'flat', 'cavity': True, 'size': [a.tile, a.tile],
                                'cam': {'target': tgt, 'dir': [0, -1, 0], 'scale': float(sc)}, 'meshes': [{'npz': files[tag], 'dx': 0.}]})
                tiles.append(out)
        rows.append((m, spec, tiles))
    job = work / 'job.json'
    job.write_text(json.dumps({'renders': renders}))
    run = subprocess.run([BLENDER, '-b', '--factory-startup', '-t', '4', '--python', str(HERE / 'ring_render.py'), '--', str(job)], capture_output=True, text=True)
    if run.returncode != 0:
        raise SystemExit(run.stdout[-1500:] + run.stderr[-1500:])
    return rows


def sheet(rows, out, tile):
    f16, f22 = font(16), font(22)
    head = 34
    W = tile * 4 + 5 * 6
    H = len(rows) * (tile + head + 6) + 6
    img = Image.new('RGB', (W, H), (24, 24, 28))
    d = ImageDraw.Draw(img)
    y = 6
    for m, spec, tiles in rows:
        d.text((8, y + 6), f'{"/".join(m.parts[-4:])}   [{spec}]   columns: front before | front after | close-up before | close-up after', font=f16, fill=(235, 235, 235))
        y += head
        order = [tiles[0], tiles[2], tiles[1], tiles[3]]       # front before, front after, close before, close after
        for i, t in enumerate(order):
            img.paste(Image.open(t).convert('RGB'), (6 + i * (tile + 6), y))
        y += tile + 6
    img.save(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('out')
    ap.add_argument('modules', nargs='+')
    ap.add_argument('--level', default='0p2')
    ap.add_argument('--backup-root', default=str(ROOT / 'experiments/feature_labels_20261025/backups'))
    ap.add_argument('--tile', type=int, default=380)
    a = ap.parse_args(argv)
    out = Path(a.out)
    work = out.parent / (out.stem + '.work')
    work.mkdir(parents=True, exist_ok=True)
    rows = process([Path(m) for m in a.modules], a, work)
    sheet(rows, out, a.tile)
    print('SHEET', out)


if __name__ == '__main__':
    main()
