"""Render review sheets for shell_slots results (Blender workbench via experiments/body_mask_20261104/render_cells.py).

    python experiments/shell_slots_20261122/render.py [name ...]      (default: every result in out/index.json)

Sheet rows: the aligned generator mesh as it came (tripo), the result at amount 0 (game colours), its provenance (game parts blue, stitches red,
generator skin pink, mirrored skin yellow), the result at amount 100 (fill). Columns: front, front-left 3/4, left side, back.
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
OUT = HERE / 'out'
SC = OUT / 'scenes'
REN = OUT / 'renders'
CELLS = HERE.parents[0] / 'body_mask_20261104' / 'render_cells.py'
BLENDER = 'C:/Program Files/Blender Foundation/Blender 4.4/blender.exe'
VIEWS = [('front', 0, 0), ('3/4', 40, 10), ('left side', 90, 0), ('back', 180, 0)]
CW = 300


def rows_for(rec):
    n = rec['name']
    rows = []
    for g in rec['ghosts']:
        if g.startswith('tripo'):
            rows.append((f'generator mesh, aligned ({g})', SC / f'{n}__{g}.npz'))
    rows += [('result, amount 0', SC / f'{n}__a0.npz'), ('provenance: light blue game part, blue game skin band, red stitch, pink generator skin, yellow mirrored', SC / f'{n}__prov.npz'),
             ('result, amount 100 (fill)', SC / f'{n}__a100.npz')]
    return rows


def main(names):
    idx = json.loads((OUT / 'index.json').read_text())
    recs = [r for r in idx['results'] if not names or r['name'] in names]
    REN.mkdir(parents=True, exist_ok=True)
    jobs, layout = [], {}
    W, H = 300, 420
    for rec in recs:
        rows = rows_for(rec)
        z = np.load(rows[1][1])
        V = z['gV']
        lo, hi = V.min(0), V.max(0)
        center = ((lo + hi) / 2).tolist()
        scale = float(max(hi[2] - lo[2], (hi[0] - lo[0]) * H / W) * 1.05)
        cells = []
        for ri, (label, npz) in enumerate(rows):
            for vi, (vname, az, el) in enumerate(VIEWS):
                out = REN / 'cells' / f'{rec["name"]}__{ri}_{vi}.png'
                out.parent.mkdir(parents=True, exist_ok=True)
                jobs.append({'npz': str(npz), 'out': str(out), 'az': az, 'el': el, 'res': [W, H], 'center': center, 'scale': scale})
                cells.append((ri, vi, out))
        layout[rec['name']] = (rows, cells)
        # close-ups of the seams: neck front / back, right wrist, both feet (centres from the game base parts), result + provenance
        sys.path.insert(0, str(HERE.parents[1] / 'tools' / 'tripo_pipeline'))
        import body_fit as bf
        body = bf.get_body(rec['race'])

        def centre(part):
            if part not in body.part_names:
                return None
            return body.V[np.unique(body.F[body.part == body.part_names.index(part)])].mean(0).tolist()
        spots = [('neck front', centre('neck.x'), 0, 10, .42), ('neck back', centre('neck.x'), 180, 10, .42), ('right wrist', centre('hand.r'), 20, 15, .36),
                 ('left wrist', centre('hand.l'), -20, 15, .36), ('feet', centre('foot.l') and [(centre('foot.l')[0] + centre('foot.r')[0]) / 2, centre('foot.l')[1], centre('foot.l')[2] + .08], 25, 20, .55)]
        if rec['kind'] == 'armour':
            def mid(a, b):
                ca, cb = centre(a), centre(b)
                return None if ca is None or cb is None else [(x + y) / 2 for x, y in zip(ca, cb)]
            spots = [('neck front', centre('neck.x') or mid('spine_01.x', 'head.x'), 0, 10, .42), ('right elbow', mid('arm_stretch.r', 'forearm_stretch.r'), 20, 25, .4),
                     ('left elbow', mid('arm_stretch.l', 'forearm_stretch.l'), -20, 25, .4), ('right knee', mid('thigh_stretch.r', 'leg_stretch.r'), 15, 10, .45),
                     ('left knee', mid('thigh_stretch.l', 'leg_stretch.l'), -15, 10, .45)]
        spots = [x for x in spots if x[1] is not None]
        crows = [('close-up, amount 0', SC / f'{rec["name"]}__a0.npz'), ('close-up, provenance', SC / f'{rec["name"]}__prov.npz')]
        ccells = []
        for ri, (label, npz) in enumerate(crows):
            for vi, (vname, cen, az, el, sc_) in enumerate(spots):
                out = REN / 'cells' / f'{rec["name"]}__c{ri}_{vi}.png'
                jobs.append({'npz': str(npz), 'out': str(out), 'az': az, 'el': el, 'res': [CW, CW], 'center': cen, 'scale': sc_ * body.H / 1.8})
                ccells.append((ri, vi, out))
        layout[rec['name'] + '__close'] = (crows, ccells, [x[0] for x in spots])
    jf = REN / 'jobs.json'
    jf.write_text(json.dumps(jobs))
    r = subprocess.run([BLENDER, '-b', '--factory-startup', '-P', str(CELLS), '--', str(jf)], capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-2000:])
        raise SystemExit('render failed')
    for name, lay in layout.items():
        rows, cells = lay[0], lay[1]
        cw, ch = (CW, CW) if name.endswith('__close') else (W, H)
        ncol = len(lay[2]) if name.endswith('__close') else len(VIEWS)
        lab = 22
        sheet = Image.new('RGB', (cw * ncol, (ch + lab) * len(rows) + 30), 'white')
        d = ImageDraw.Draw(sheet)
        d.text((6, 8), name + ('   ' + ' | '.join(lay[2]) if name.endswith('__close') else ''), fill=(0, 0, 0))
        for ri, vi, p in cells:
            y = 30 + ri * (ch + lab)
            if vi == 0:
                d.text((6, y + 4), rows[ri][0], fill=(60, 60, 60))
            sheet.paste(Image.open(p).convert('RGB'), (vi * cw, y + lab))
        sheet.save(REN / f'{name}.png')
        print('sheet', REN / f'{name}.png')


if __name__ == '__main__':
    main(sys.argv[1:])
