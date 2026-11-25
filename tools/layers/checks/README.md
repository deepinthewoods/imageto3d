# Layer checks

Check, render and rebuild tools for the layer prototype (`../layers.py`), written while fixing the layers (2026-11-24/25). Run them from
`F:/godot/blender/characters/imageto3d`; most take race names. Outputs they write here (json, logs) are not tracked.

| Script | What it does |
| --- | --- |
| `audit_layers.py [race ...]` | Every layer: faces turning over anywhere along the Amount slider, duplicate triangles, edges with more than two faces, open edges |
| `validate_layers.py <n per race> <seed> [races,]` | Random mixes per race at Amount 100: open seam at the rings, skin poking through, inside of a garment showing |
| `red_check.py <race> <stem part ...>` | One layer alone from 24 directions: back faces seen (red in the viewer) and inner/outer wall crossings, Amount 0 and 100 |
| `prov_view.py <png> <title> <x,y,z> <win mm> label=a.glb[@fill]+b.glb ...` | Close-ups, faces coloured by provenance (end caps magenta, caps cyan, body strips orange), back faces red; `@fill` = Amount 100 |
| `npz_cmp.py <png> <title> <x,y,z> <win mm> label=a.npz+b.npz ...` | Close-ups of scene arrays (`out/layers/<race>/scenes/*__a0/__a100.npz`) |
| `glbread2.py` | Reads the prototype's GLBs (positions, `fill` morph, material per face); used by the others |
| `rebuild_match.py <races,|all> <set part> [module]` | Rebuilds the matching modules' layers (writes `out/layers`) and the index |
| `rebuild_open.py` | Rebuilds the modules whose layers still have open edges |
| `notes_stats.py <race ...>` | From the layer notes: wall crossings, body strips (full / narrow) per slot, seams joined, legs centred |
| `legs_notes.py <set stem> <race ...>` | Leg-centring and buried-garment notes of torso layers |
| `leg_offsets.py <race> <set part> [module]` | Fore-aft / lateral offset of each generated leg from the game leg along thigh and shin |
| `double_layer_scan_layers.py [races,]` | Share of garment that is an inner wall (double layer) per layer |
| `flap_faces.py <glb> <race> <knee x,y,z>` | Garment faces pushed out below a knee at Amount 100, with their Amount 0 depth |
| `gpu_cache_repro.py` | Repro of the shared GPU buffer bug (a shallow copy of a body released its buffer); prints "GPU still on: True" when fixed |
| `band_report.py <race> <set ...>` | Bare layer sizes and, per set's torso layer, the seams and body strips used |
