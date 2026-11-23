# Outfit Studio

Outfit Studio is a Blender 5.1 add-on. It turns this pipeline's reduced wearables into game items
for four-ways, and it exports the base characters and their animation libraries.
- `SPEC.md` is the implementation contract.
- `../../ADDON_PLANNING.md` records the decisions behind it.
- `../../ENGINE_CHANGES.md` is the format the game must learn. The engine itself is not changed from here.

## Install

The folder is symlinked as `~/.config/blender/5.1/scripts/addons/outfit_studio`. To turn it on, enable
**Outfit Studio** in Preferences > Add-ons, together with Auto-Rig Pro. The panel is the
**Outfit Studio** tab in the 3D view sidebar (N).

## Files

| File | Role |
| --- | --- |
| `projects/<base>.blend` | Base project: the ARP rig `rig` and the body/head mesh `base`. Edit the rig and attachment points here. |
| `outfit_projects/<base>_outfits.blend` | Outfit project. It **links** the base's `Character` collection (as a library override), so rig changes flow through. Every item is an `OS_<item_id>` collection. |
| `blender/export/outfit_studio/<batch>/` | Export: `bases/<base>/{base.glb, base.json, anims.glb, clips.json}`, `items/<item_id>/{item.json, <base>.glb, <base>.json}`, `catalog.json`. |

An item id is `<set_id>__<type>`, where the type is `outfit`, `body_armor`, `gauntlets` or `boots`. A single
id is shared by all races. A race suffix on a pipeline set folder (`plain_plate_mail_01_gnome`) is
dropped from the id.

## Base mode (open `projects/<base>.blend`)

- **Rig check**: bone, deform and AP counts, plus any problems.
- **Attachment points**:
  - Choose an automatic region strategy for each family. Hair can use `landmark_geodesic` (the default), `template`, `texture` or `legacy`. Limbs and torso can use `cylinder` or `keep`.
  - **Generate** previews the points as empties. **Commit** writes the `AP_*` bones.
  - **Lock** protects the selected bones from regeneration. **Report** shows the quality numbers.
  - Painting a vertex group named `os_region_scalp` overrides the scalp mask.
- **Arm / leg spacing**: sets degrees of arm abduction and centimetres of foot spread for this base, with an optional override per clip. **Preview** applies the spacing to a copy of the active action.
- **Export base** and **Animations** run in a background Blender process on the saved file.

## Outfit mode (open or create `outfit_projects/<base>_outfits.blend`)

- **Navigate** with ◀ / ▶ or the dropdown, and narrow the list with the type, status and text filters.
- **Item**: label, notes, status, triangle count, and the channel list. For each channel you can set its role alias and material.
- **Import from pipeline**:
  - Choose the set, job, module and LOD, or **all new for this base**.
  - **Since 2026-11-20 the input mesh is the fit lab's choice** (`fit_choice.json` baked by `fit_symmap.py`, else the lab default, else the aligned / fitted wearable), and every item records the body **section** states (`show` / `hide` / `connector`) and connector meshes of its module (SPEC section 11). The older body-fitted wearable rule below only applies to modules that were never aligned.
  - The importer takes the body-fitted wearable (`lod_error_<lv>__wearable_fitted.glb`, and `lod_<lv>_fitted/` pieces) from `tools/tripo_pipeline/body_fit.py` when it exists and is newer than the unfitted one, else the plain wearable; the item's `source` records which (`wearable_fitted`, `pieces_fitted`). Opt out with `pipeline_source.USE_FITTED = False`.
  - The mesh is moved into base space. The gauntlet and boot module is split into two items (both sides of the wearable are kept; `--mirror-left` mirrors the right side instead), rigid or smooth weights are applied, and `head_*`/`bulk_*` morphs are transferred.
  - **Hand edits are protected.** When you re-import an item you have edited, the new version goes to `OS_<id>__reimport`, and you choose **Keep mine**, **Take new** or **Compare**.
- **Check rig** / **Re-bind stale**: after the base rig changes, re-applies weights to unedited items. Edited items are re-bound only when you confirm.
- **Mix and match**: chooses the outfit, body armour, gauntlets and boots that Game view shows.
- **Views**:
  - Game view (the outfit is hidden while any armour is worn)
  - Item only, Colour roles, Engine palette (with Reroll), Channels, Label confidence
  - Weights by bone, Poke-through, Pose test (actions, or load an FBX clip)
  - Sockets with props, AP points (buried points shown red), Head swap
  - Thickness, Concept overlay, Source high-poly, LOD, Wireframe/stats
  - Reset undoes any view.
- **Validate** before **Export**. Export only includes approved (or already exported) items, and needs a saved file.

## Batch (plain python3)

```bash
cd tools/outfit_studio
python3 run.py import --base all --set all            # create projects if missing, import new items (never overwrites edits)
python3 run.py validate --base all
python3 run.py export-base --base all --batch 20261023-v1
python3 run.py export-heads --base all --batch 20261023-v1                  # needs export-base first; human_male also exports the dragon heads + seats
python3 run.py export-anims --base human_male --batch 20261023-v1 --resume   # ~8 s per clip, ~3 h per base
python3 run.py export-items --base all --batch 20261023-v1 --resume
python3 run.py all --base gnome_female --batch test --limit 1                # smoke run
```

## Tests

| Test | How to run |
| --- | --- |
| `tests/test_core.py`, `tests/test_ap.py` | plain python3 |
| `tests/test_blender_*.py` | headless Blender; see each file's docstring |

Tests write only to `$OSTUDIO_SCRATCH` / `$OSTUDIO_TEST_OUT`, or a temp folder. They never touch `projects/`, `models/`
or four-ways.

## Known gaps

- Real outfits (`outfit` module) and segment-first pieces have not been converted by the pipeline yet. Smooth weights and the pieces path are only tested on stand-ins.
- Pauldrons:
  - The zone fallback finds pauldron pieces only where the concept painted them `accent`.
  - The current plate jobs were labelled with the legacy two-colour palette, so they have no pauldron pieces.
  - The pauldron "half" binding has never been exercised.
- Head swap places a library head by translation and scale at the head bone. It does not do the game's neck tuck.
- Attachment-point placement has no ear or horn auto-detection yet; you can exclude them with `ear_*`/`horn_*` vertex groups. The gnome's head part includes its hat cone.
- The full animation library has not been run (~1255 clips per base, 3–5 GB of checkpoints per base).

## Base + heads export (game standard v2)

Needs Blender 5.1 (`BLENDER=C:/Users/niall/Tools/blender-5.1.2-windows-x64/blender.exe`, it is not on PATH) with Auto-Rig Pro 3.78.x installed as an extension
(`bl_ext.user_default.auto_rig_pro`) and the `character_export` add-on folder linked (`scripts/character_export`).

- `export-base` writes `bases/<base>/base.glb` + `base.json` (version 2): one skin (ARP Humanoid/Godot skeleton, 10 sockets, `AP_*` leaves), a `head` node
  (default head, open neck seam) and `sec_<part>_<band>` body section nodes (layout `fine` of `experiments/body_mask_20261104`, cut in `core/body_sections.py`),
  all on the single material `skin`, each with the `bulk_*`/`head_*` morph targets that move it. `base.json` also has `sections[]`, `head{head_frame, attach, neck_seam,
  head_target_height, default_head}` (bases.json equivalents measured on this base) and the source hashes. `gnome_female` is not supported yet (its head and feet are separate objects).
- The skeleton also has the rider bone `Seat` (child of `Hips`, identity local rotation, constant offset, no weights; rule of `scripts/character_export/ride_rig.py` `Rig.seat`, computed on the `fine` sections mesh in `export_base.ensure_seat`, part of `prepare_session`). `base.json` has `seat{rest_gltf, local_offset_gltf, rule}` and lists it in `sockets`; `bases/<race>/prepared.blend` + `skeleton.json` are the prepared rig / hash inputs for the animation exporter.
- `export-heads` re-targets `items/heads/<race>/<race>_heads.blend` to the exported base (`heads/<race>/h_*.glb|json`, `catalog.json`, `retarget.json`, `check_sheet.png`,
  `heads/bases.json`, and `bases/<race>/sections_sheet.png`). `--shift auto|fit|mesh|bone|none` picks the seat transform (`auto`: identity when the default head is unchanged, else `fit` = scale + pitch +
  translation mapping the old default head onto the new one; `bone` = Head-bone offset only; `mesh` = translation-only silhouette match). `human_male` also exports `dragon_type_*` and `heads/dragon_seats.json` (`--no-dragons` skips them). See `blender/heads.py`.
