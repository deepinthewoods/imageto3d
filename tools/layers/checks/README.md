# Layer checks

Check, render and rebuild tools for the layer builder (`../layers.py`, `../build.py`; moved from `experiments/shell_slots_20261122/checks/` with the code), written while fixing the layers (2026-11-24/25). Run them from
`F:/godot/blender/characters/imageto3d`; most take race names. Outputs they write here (json, logs) are not tracked. `validate_layers.py`, `audit_layers.py`, `prov_view.py`, `npz_cmp.py`, `notes_stats.py`, `legs_notes.py`, `flap_faces.py` and `band_report.py` still read the
prototype's flat layout `<dir>/<race>/<slot>__<set>.glb` (+ `scenes/`) under `experiments/shell_slots_20261122/out/layers` (read-only); `red_check.py` and
`double_layer_scan_layers.py` take the build folders of `build.py` (`<build_id>/<slot>.glb`, `scenes/`, `build.json`); `rebuild_match.py`, `leg_offsets.py` and
`gpu_cache_repro.py` use the production code. Run everything against scratch builds, never into a module's own `layers/` folder.

| Script | What it does |
| --- | --- |
| `audit_layers.py [race ...]` | Every layer: faces turning over anywhere along the Amount slider, duplicate triangles, edges with more than two faces, open edges |
| `validate_layers.py <n per race> <seed> [races,]` | Random mixes per race at Amount 100: open seam at the rings (body and rings exactly as layer generation builds them: `layers.get_body` / `out_body` / `rings_of`, with the joint-plane corrections), skin poking through (skin mask from the GLB material names), inside of a garment showing |
| `red_check.py <race> <build dir> [stem part ...]` | One layer alone from 24 directions: back faces seen (red in the viewer) and inner/outer wall crossings, Amount 0 and 100. Garment = role is not `skin` (scene arrays `gRole` / `gProv`; builds before the 2026-11-25 fixes lack them: rebuild) |
| `prov_view.py <png> <title> <x,y,z> <win mm> label=a.glb[@fill]+b.glb ...` | Close-ups, faces coloured by provenance (end caps magenta, caps cyan, body strips orange), back faces red; `@fill` = Amount 100 |
| `npz_cmp.py <png> <title> <x,y,z> <win mm> label=a.npz+b.npz ...` | Close-ups of scene arrays (`out/layers/<race>/scenes/*__a0/__a100.npz`) |
| `glbread2.py` | Reads layer GLBs: positions, `fill` morph, material name per face (`role:prov`), optionally the material colour; `skin_mask` / `split_names`; used by the others and by `build.py parity` |
| `rebuild_match.py <races,all> <set part> [module] [--lod 0p4] [--job ID] [--out DIR] [--force]` | Rebuilds the matching modules' layers with `build.build_module` (several processes) into a SCRATCH folder (default `<temp>/layers_rebuild_match`; `--out` under `models/` is refused). Discovers the requested LOD's file; the job is the named one, else the newest by `reduced/manifest.json` (job id breaks ties); exit 1 if a build failed or nothing matched. Production rebuilds go through the app |
| `notes_stats.py <race ...>` | From the layer notes: wall crossings, body strips (full / narrow) per slot, seams joined, legs centred |
| `legs_notes.py <set stem> <race ...>` | Leg-centring and buried-garment notes of torso layers |
| `leg_offsets.py <race> <set part> [module] [--lod L] [--job ID]` | Fore-aft / lateral offset of each generated leg from the game leg along thigh and shin; LOD default = the module's newest build pointer, else 0p4; races without game legs (gnome) are skipped |
| `double_layer_scan_layers.py <build dir> ...` | Share of garment (role is not `skin`) that is an inner wall (double layer) per layer of the given builds |
| `flap_faces.py <glb> <race> <knee x,y,z>` | Garment faces pushed out below a knee at Amount 100, with their Amount 0 depth |
| `gpu_cache_repro.py` | Repro of the shared GPU buffer bug (a shallow copy of a body released its buffer); prints "GPU still on: True" when fixed |
| `band_report.py <race> <set ...>` | Bare layer sizes and, per set's torso layer, the seams and body strips used |
