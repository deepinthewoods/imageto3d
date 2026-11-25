"""legs_notes.py <set stem> <race> ...: the leg-centring and hidden-garment notes of saved torso layers."""
import json
import sys

stem, races = sys.argv[1], sys.argv[2:]
for race in races:
    r = json.load(open(f'F:/godot/blender/characters/imageto3d/experiments/shell_slots_20261122/out/layers/{race}/torso__{stem}.json'))
    lc = r['load'].get('legs_centred', {})
    sides = {s: (v.get('shin_skin_offset_mm'), v.get('skin_sections'), v.get('max_move_mm')) if isinstance(v, dict) else v
             for s, v in lc.items() if s in ('r', 'l')}
    print(race, 'legs (skin offset mm, skin sections, max move mm):', sides, '| faces turning over', lc.get('faces_turning_over'),
          '| hidden kept', r['notes']['amount'].get('hidden_kept'))
