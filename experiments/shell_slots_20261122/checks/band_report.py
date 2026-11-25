"""band_report.py race set [set ...]: bare layer sizes and, per set's torso layer, the seams and body strips used."""
import json
import sys
from pathlib import Path

import numpy as np

race = sys.argv[1]
P = Path('F:/godot/blender/characters/imageto3d/experiments/shell_slots_20261122/out/layers') / race
for j in sorted(P.glob('*__bare.json')):
    z = np.load(P / 'scenes' / f'{j.stem}__a0.npz')
    print(f'{j.stem}: {len(z["gF"])} triangles (caps included)')
for sset in sys.argv[2:]:
    for j in sorted(P.glob(f'*__{sset}*.json')):
        nt = json.load(open(j))['notes']
        print(j.stem[:44], 'faces', nt['faces'], '| seams', [(x['ring_edges_joined'], 'strip' if x.get('band') else '-') for x in nt['seams']['rings']],
              '| tries', nt.get('band_tries'), '| strips', {k: (b['faces'], b['inner_joined']) for k, b in (nt.get('band') or {}).items()},
              '| folded', nt['seams'].get('faces_folded'))
