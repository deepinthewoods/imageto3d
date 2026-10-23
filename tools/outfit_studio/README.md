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
  - The mesh is moved into base space. The gauntlet and boot module is split into two items, the right side is mirrored to the left, rigid or smooth weights are applied, and `head_*`/`bulk_*` morphs are transferred.
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
