"""notes_stats.py <races...>: from the layer JSONs: wall crossings before/after (Amount 0 / 100), body strips (full / narrow) per slot, seams
joined all round, leg centring triggers, hidden garment kept, centring faces turned."""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

OUT = Path('F:/godot/blender/characters/imageto3d/experiments/shell_slots_20261122/out/layers')
races = sys.argv[1:]
w0 = np.zeros(2)
w1 = np.zeros(2)
wl = np.zeros(2)
seam = defaultdict(lambda: Counter())
legs, turned, hidden, nlay, warns = [], 0, 0, 0, 0
for race in races:
    for js in sorted((OUT / race).glob('*.json')):
        if '__bare' in js.name:
            continue
        r = json.load(open(js, encoding='utf-8'))
        n = r.get('notes', {})
        if 'amount' not in n:
            continue
        nlay += 1
        warns += bool(r.get('warnings'))
        slot = r['slot'].split('.')[0]
        w = n['amount'].get('walls', {})
        if w:
            w0 += [w['a0']['crossing_pairs'] or 0, w['a0']['left']]
            w1 += [w['a100']['crossing_pairs'] or 0, w['a100']['left']]
        left = n.get('slider_fold_guard', {}).get('wall_crossings_left')
        if left:
            wl += left
        hidden += n['amount'].get('hidden_kept', 0) or 0
        rings = n['seams']['rings']
        band = n.get('band', {})
        for k, R in enumerate(rings):
            a, b = R['ring_edges_joined'].split('/')
            seam[slot]['rings'] += 1
            seam[slot]['joined'] += a == b and b != '0'
            bk = band.get(str(k)) or band.get(k)
            if bk and bk.get('inner_complete'):
                seam[slot]['strip_full' if bk.get('width', 1) >= 1 else 'strip_narrow'] += 1
        lc = r.get('load', {}).get('legs_centred')
        if lc:
            for s in ('r', 'l'):
                v = lc.get(s)
                if isinstance(v, dict) and v.get('max_move_mm'):
                    legs.append((race, r['set'], s, v['shin_skin_offset_mm'], v['max_move_mm']))
            turned += lc.get('faces_turning_over') or 0
print(f'{nlay} layers, {warns} with warnings')
print(f'inner/outer wall crossings: Amount 0 {int(w0[0])} -> {int(w0[1])}, Amount 100 {int(w1[0])} -> {int(w1[1])} '
      f'(after the final slider guard: {int(wl[0])} / {int(wl[1])})')
print(f'garment vertices kept buried at Amount 100: {hidden}')
for slot, c in sorted(seam.items()):
    t = c['rings']
    print(f'{slot:10s} seams {t:4d}: joined all round {100 * c["joined"] / t:4.0f} %, body strip full {100 * c["strip_full"] / t:4.0f} %, '
          f'narrow {100 * c["strip_narrow"] / t:4.0f} %')
print(f'legs centred: {len(legs)} (faces turned by it: {turned})')
for x in legs:
    print('  ', x)
