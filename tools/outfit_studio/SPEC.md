# Outfit Studio — implementation spec

The decisions behind this spec are recorded in `../../ADDON_PLANNING.md`, and the
engine-side contract is in `../../ENGINE_CHANGES.md`.

Outfit Studio is a Blender 5.1 add-on, a package in this folder, symlinked as
`~/.config/blender/5.1/scripts/addons/outfit_studio`. Auto-Rig Pro is
available as `bl_ext.user_default.auto_rig_pro`.

Ground rules:
- **Never modify** `projects/<base>.blend`, unless the user runs a base-mode
  operator in that file. Never modify `models/`, the pipeline stage folders, or four-ways.
- Code should be importable without bpy where possible (pure numpy in `core/`)
  so it can be unit-tested with plain python3. bpy-facing code goes in `blender/`.
- Keep the code idiomatic and compact, and match the style of `tools/tripo_pipeline/*.py`:
  module docstring, small functions, numpy, no classes where a function will do.
- Every long operation must also be runnable headless:
  `blender -b <file> --python-expr "import outfit_studio.batch as b; b.main()" -- <args>`,
  or via `tools/outfit_studio/run.py`.

## 1. Paths (`paths.py`)

| Name | Path |
| --- | --- |
| `ROOT` | imageto3d root, found from `__file__` (symlinks resolved) |
| `PROJECTS` | `ROOT/projects` (base projects `<base>.blend`, `manifest.json`) |
| `OUTFIT_PROJECTS` | `ROOT/outfit_projects` (`<base>_outfits.blend`, created by the add-on) |
| `MODELS` | `ROOT/models` (`<set_id>/<job_id>/<race>/<module>/reduced/…`) |
| `BASE_VIEWS` | `ROOT/base_views/<base>` (`transforms.json`, `palette.json`, `parts.npz`) |
| `ANIMS` | `ROOT/../../anims` (Mixamo `*.fbx`; RPG GLBs in subfolders / `*.glb`) |
| `HEADS` | `ROOT/../../export/heads` (`<race>/h_*.glb|json`, `bases.json`) |
| `EXPORT` | `ROOT/../../export/outfit_studio/<batch>` |

BASES = the 11 names in `projects/manifest.json`.

## 2. Data model

### Base project (`projects/<base>.blend`, existing)
- Collection `Character` holds the mesh `base` (body and head in one mesh; the
  `bone_<part>` materials mark the body parts; shape keys `head_*` and `bulk_*`;
  29 ARP deform vertex groups) and the armature `rig` (ARP, about 916 bones, 626
  `AP_*` bones, 10 socket bones).
- Base-mode settings are stored as a JSON string in `scene["outfit_studio"]`:
  arm/leg spacing, AP placement settings, and the region strategy per family.

### Outfit project (`outfit_projects/<base>_outfits.blend`)
- The `Character` collection from `projects/<base>.blend` is **linked**, with a library
  override hierarchy (`bpy.types.ID.override_hierarchy_create`) so that the rig can be
  posed and modifiers can be added to `base`. Nothing from the base is copied.
- `scene["outfit_studio"]` holds JSON: `{base, rig_fingerprint, base_fingerprint,
  created, version}`.
  - `rig_fingerprint` is the sha256 of the sorted (bone name, parent, rounded rest
    head/tail/roll) entries for the deform bones and sockets.
  - `base_fingerprint` is the sha256 of the rounded rest vertex positions.
- Each **item layer** is a collection `OS_<item_id>` under `OS_Items` holding:
  - collection props (`coll["os_item"]` JSON): `item_id, set_id, type
    (outfit|body_armor|gauntlets|boots), label, status
    (imported|edited|needs_review|approved|exported), notes, channels[{id, label,
    role, material}], source{job_id, module, race, lod, manifest, wearable_glb,
    wearable_sha256, pieces_source}, import{hashes{piece: hash}, rig_fingerprint,
    base_fingerprint, date}, stats{tris}`.
  - one mesh object per piece, `OS_<item_id>__<piece>`, parented to the override rig
    with an Armature modifier. The face attribute `os_channel` (INT) is an index into
    `channels`. Material slots are one per channel, using material `OSC_<role>`,
    whose preview colour is the role hex from `base_views/<base>/palette.json`.
    The face attribute `os_label_source` (INT, from the pipeline `_colors.npz`) is
    kept for the confidence view.
- `item_id = <set_id>__<type>`. It is the same across bases, and an item exists for a base
  when that base's outfits file has its layer.
- Reimport protection: `hash(piece)` is the sha256 of the vertex positions (rounded to
  1e-5), the face indices, the `os_channel` values, and the vertex-group weights (rounded to
  1e-3). An item is "edited" when any current hash differs from `import.hashes`.

## 3. Import from the pipeline (`core/pipeline_source.py`, `blender/importer.py`)

### Discovery
Discovery looks for `MODELS/<set_id>/<job_id>/<race>/<module>/reduced/manifest.json`
with `module` in `body_armor`, `right_gauntlet_boot` and `outfit`. It skips
`*_attempt*` folders and non-`reduced/` generations. When several jobs exist for the same
(set, race, module), the newest manifest wins, and the import dialog lists the others.

### Transform (verified 2026-10-23: plate armour lands at a median of about 2 cm, p90 under 5 cm, off the body)
1. Import `reduced/lod_error_<lv>__wearable.glb`. Its Blender coordinates are `P`.
2. `rev = s_n * (P @ inv(R) - o_n)`, where `s_n = normalization_scale`,
   `R = rotation_to_review` and `o_n = normalization_origin_rotated`.
3. `bp = (rev - fit.translation) / fit.scale`, using `manifest.parts.fit`.
4. `W = bp * (H / 2) + o_w`. Here `o_w` is the bbox centre in X/Y with the min Z, and
   `H` is the Z height, both taken from the **base mesh evaluated in REST pose in world
   space**.

If a manifest has no `parts.fit` (older jobs), fit the base's rest mesh to the full
labelled LOD (`lod_error_<lv>.glb`, which includes the red base faces) using
`tools/tripo_pipeline/face_labels.fit_base` (import with `sys.path`, bpy is available).
If that fails, refuse the import and say why.

### Per-face data
- Role: the wearable material slot names (`primary`, `secondary`, `tertiary`,
  `accent`, `joint`, `uncertain`). `uncertain` is mapped to the most common
  neighbouring role.
- Body part: `reduced/lod_error_<lv>_colors.npz` arrays `wearable_body_part_glb_order`
  plus `body_part_names`, and `face_label_source` / `label_source_names` (mapped
  through `wearable_face_order`). If the face counts don't match the imported mesh,
  recompute body part by nearest base face, using the base `bone_<part>` materials.

### Module → items
- `outfit` → one item, `outfit`. It is smooth-skinned and has one piece, `outfit`.
- `body_armor` → one item, `body_armor`. It is rigid-skinned with pieces:
  - Use segment-first / split-pieces output when it exists for that LOD
    (`<module dir>/segment_first/pieces/lod_<lv>/*.glb` or `<module dir>/pieces/lod_<lv>/*.glb`
    plus `pieces.json`). These are in native frame, so apply the same transform.
    Piece names come from their file stems (chest, pelvis, thigh_l/r, arm_l/r, pauldron_l/r).
  - Otherwise use the built-in zone fallback:
    - body_part → piece: spine_01/neck/shoulder → chest, root → pelvis,
      thigh_stretch.* → thigh_*, arm_stretch.* and forearm → arm_*.
    - faces with role `accent` whose part is shoulder/arm_stretch → pauldron_*.
    - Then smooth: each connected same-role region takes its majority piece.
- `right_gauntlet_boot` → two items, `gauntlets` and `boots`. Keep only faces whose
  body part is on the anatomical **right** (`*.r`), or whose centroid has x < 0 where the
  part is unsided.
  - Gauntlets take hand.r and forearm_stretch.r, plus arm_stretch.r faces adjacent to them.
  - Boots take foot.r and leg_stretch.r, plus thigh_stretch.r faces adjacent to them.
  - Pieces: gauntlets → `cuff_r` (forearm) and `hand_r`; boots → `shin_r` and `foot_r`.
    The split is by body part, at the wrist and ankle.
  - Then **mirror** every right piece across the base's sagittal plane (x → -x in base
    world; check the rest mesh is symmetric to within 1 mm, else mirror about `o_w.x`).
    Flip the winding to make `_l` pieces.
- Pieces are separate mesh objects, split along piece boundaries (duplicated
  boundary vertices), so rigid pieces never share a vertex.

### Channels
- Decoration roles per type: body_armor, gauntlets and boots use `tertiary`. An outfit has none.
- Outfit: channel per role present, `id = role`, `role = role`.
- Armour: channel `decoration` (if any decoration faces exist), plus channel `<piece>`
  for each piece. If a piece has more than one non-decoration role covering at least 15% of
  its area, the extra roles become `<piece>_<role>`.
  - Each channel's `role` alias is the majority role of its faces.
  - Left and right pieces are separate channels with the same alias.
- `material` defaults to `""`, meaning the engine default. It is editable.

### Weights (`blender/weights.py`; bone table `data/rigid_bones.json`)
- **Rigid**: every vertex of a piece gets weight 1.0 on that piece's bone(s).
  Default table, in ARP deform names:
  - chest `c_spine_02_bend.x`, pelvis `c_root_bend.x`
  - thigh_l `c_thigh_stretch.l`, arm_l `c_arm_stretch.l`
  - pauldron_l `{shoulder.l: 0.5, c_arm_stretch.l: 0.5}` (the "half" convention)
  - cuff_l `c_forearm_stretch.l`, hand_l `hand.l`
  - shin_l `c_leg_stretch.l`, foot_l `foot.l`
  - The same for `_r`.
  Verify each name exists among the rig's deform bones, and fail loudly if one doesn't.
- **Smooth (outfit)**: data-transfer the vertex-group weights from the base rest mesh
  (nearest face, interpolated). Then limit to 4 influences, normalise, and run 2 smoothing
  passes (`vertex_group_smooth`, factor 0.5). Faces with body part `skirt`, or whose
  centroid is below the crotch and between the thighs, get a hips-dominant blend:
  `c_root_bend.x` 0.6, and 0.4 shared between the thighs by x-distance.
- Record `rig_fingerprint` with the item. On file open, or when "Check rig" is run, items
  with a different fingerprint are marked **stale**. "Re-bind stale" re-runs the weights
  only on unedited items; edited items need explicit confirmation.

### Morph targets (`core/morph_transfer.py`)
For every base shape key except `Basis` (`head_*`, `bulk_*`), find each item vertex's
closest point on the base rest mesh (triangle and barycentric coordinates). The vertex's
delta is the barycentric-weighted sum of the base key's vertex deltas. Create the shape
key only when its max |delta| exceeds 1e-5.

### Reimport
- If the item doesn't exist, create it.
- If it exists and is unedited, replace its pieces in place, keeping the label, notes,
  status and channel materials/aliases when the channel ids still match.
- If it exists and is edited, import into `OS_<item_id>__reimport` (hidden) and set the
  status to needs_review. The UI then offers **Keep mine**, **Take new** and **Compare**
  (show both, offsetting the reimport by +X 1 m).

## 4. UI (`blender/ui.py`, `blender/ops_*.py`), View3D N-panel tab "Outfit Studio"

The mode is detected from the open file: `projects/<base>.blend` gives **Base mode**, a
file in `outfit_projects/` gives **Outfit mode**, and anything else shows a "Create /
open outfit project" box.

### Outfit mode
- Header: the base name, `◀ | item dropdown | ▶`, a filter (type, status, text), and a
  status badge.
- Item box: label, type, source (job/LOD), triangle count, status buttons (approve /
  needs review), notes, and the channel list. Each channel row shows id, a role
  dropdown and a material string.
- Import box: "Import from pipeline…" (choose set / job / module / LOD; "all new"),
  "Re-import current", and the reimport resolution buttons.
- Mix and match: four dropdowns (outfit / body armour / gauntlets / boots), used by
  Game view.
- **Views** (one button each; each view is a function `apply_view(context, name)`
  plus a reset):
  - **Game view**: equipped layers shown. The outfit is hidden when any armour is equipped.
  - **Item only**: the current item without the body.
  - **Colour roles**: faces in their role colours.
  - **Engine palette preview**: random tints per role; the "Reroll" button picks new ones.
  - **Channels/pieces**: a distinct colour per channel.
  - **Label confidence**: colour by `os_label_source`.
  - **Weights by bone**: face colour = dominant bone.
  - **Poke-through**: highlights base vertices outside the item surface, at the current
    frame or across the active action, via a BVH test along the vertex normal.
  - **Pose test**: an action dropdown (rig actions plus "load clip…" from `ANIMS`) with
    play/stop.
  - **Sockets + props**: simple proxy sword, shield, back and hip props bone-parented to
    the socket bones.
  - **AP points**: an empty per `AP_*` bone, coloured by family. The buried ones
    (inside any equipped item) are red.
  - **Head swap**: list `HEADS/<race>/h_*.glb`, seat the chosen head on the head bone
    following `bases.json`, and hide the base head with a Mask modifier on the `head.x`
    vertex group.
  - **Thickness**: one slider driving every `bulk_*` value on the base and items.
  - **Concept overlay**: a camera from `base_views/<base>/transforms.json` (converted to
    base world) with the item's concept view PNG as the camera background. The PNG is
    found via the job's views folder.
  - **Source high-poly**: import `source.glb` with the same transform, textured.
  - **LOD**: re-import at another LOD, with edit protection.
  - **Wireframe / stats**.
- Validate and Export buttons (section 6).

### Base mode
- Rig check: fingerprint, deform names, sockets present.
- AP placement box: per-family strategy dropdowns, density/spacing, Generate (preview
  as empties) / Commit (write bones) / Lock selected / Clear.
- Spacing: arm and leg angle (degrees), with a preview on the active action.
- Export base, export animations.

## 5. Attachment-point placement v2 (`core/ap/`, `blender/ap_ops.py`)

### Contract (unchanged for the game)
- Bones are named `AP_<group>` or `AP_<group>_<NN>`. Each bone's +Y (head→tail) is the
  outward normal and its +Z (roll) is the up/growth tangent. They are non-deform, tagged `cc`,
  live in the bone collection `Attachment Points`, and are parented to the bone of the
  face they sit on.
- Group names are as today: `crown`, `templeL/R`, `scalp_{c,l1,r1,l2,r2}` (rows),
  `scalp_rand`, `face_rand`, the limb rows `<armL|armR|forearmL|…|shoulderR>_<dir>`, `torso_<f|fl|l|bl|b|br|r|fr>`,
  `<region>_rand` and `torso_rand`.
  - Read the current group list and counts off the base rig before regenerating, and
    produce the same groups.
  - Counts may change.
  - Bones with `os_locked = True` are kept untouched.

### Regions
Regions are switchable per family, are automatic, and produce a per-face float mask
in [0, 1]. They are stored as face attributes `os_region_<family>`, and manual paint
is an override layer:
- `landmark_geodesic` (default):
  - Landmarks are found from the head part geometry: crown (top), brow (front-most
    point above eye height), ears (lateral extremes), nape (back of the neck junction).
  - Scalp = head faces whose geodesic distance from the brow is beyond the hairline
    offset, are above the ear-top plane at the sides, and lie above the nape at the back.
  - Face = head faces in front of the hairline, below the brow.
- `template`: transfer the masks from a reference base/head by nearest point after a
  scale+translation fit.
- `texture` (library heads with a source texture): hair-coloured clusters of the
  texture (k-means in Lab, choosing the cluster that differs from the skin cluster at
  the cheeks).
- `legacy`: the current plane through the temples and crown, with normal thresholds.

### Sampling
- Surface (geodesic) distances run on a 2× subdivided copy of the region, using the heat
  method or Dijkstra on edges with lengths. No 3D-only distances.
- Scatter groups (`*_rand`) use farthest-point sampling inside the mask, with min spacing
  `spacing`. Generate the left half (x ≥ 0, including the midline) and mirror it.
- Row groups (scalp rows, limb rows, torso lines):
  - Paths are traced on the surface: scalp rows run from the hairline to the nape at
    fixed lateral offsets; limb rows are iso-angle lines of a bone-aligned cylinder
    parametrisation; torso lines are the same about the spine axis.
  - Resample at equal arc length.
  - Exclude the ends where the weights are mixed. A point is a joint point if its max
    single-bone weight is < 0.8 (default).
- Frames:
  - normal = area-weighted average of face normals within radius `r` (default 1.5% of
    body height);
  - hair tangent = projected gradient of the geodesic distance from the crown whorl;
  - limb tangent = projected bone axis;
  - face/torso tangent = projected world up.
- Deformation check (optional): reject points whose displacement relative to their
  parent bone exceeds a threshold over the rig's actions.

### Quality report
Printed in the UI and as JSON:
- mirror pairing error (max, < 1 mm);
- the nearest-neighbour geodesic spacing CV per group (< 0.25 for scatter);
- points in the face region for scalp groups (0);
- points at joints (0 when exclusion is on).

The same `core/ap` API must work on a library head mesh, returning sockets as matrices
(for `scripts/heads` to use later).

## 6. Shared export helper (`blender/arp_export.py`, main thread, done)
- `ce(name)` imports a `blender/scripts/character_export` submodule as a library.
- `enable_arp()`.
- `fix_weights(meshes, rig)` does a top-4 cut and normalises, in place. Use it only on an
  in-memory session that is never saved.
- `export_glb(path, rig, meshes, animations=False, twist=True) -> audit dict`
  (`skeleton_sha256`, `bones` count, `animations`, …).

It wraps ARP `arp_export_gltf_panel` (Godot, Humanoid rename), `native_contract.finalize`
(AP/socket zero-weight leaves) and `native_audit.audit`. Verified on human_male: 6 base
vertices have 5 influences and need `fix_weights` before export.

Each export mutates the session, so run **one export per Blender process** in batch mode.
Interactive operators either export from a throwaway subprocess (`blender -b <saved file>
--python-expr …`) or require the file to be saved, and say so.

Useful character_export functions:
- `skin.Surface`, `skin.bind`, `skin.validate_weights`
- `native_morphs.body_bulk_keys`, `native_morphs.attachment_offsets`
- `native_attachment_restore.bindings(body, rig)` (AP surface bindings for `base.json`
  `attachments`)
- `skin.split_head(body)` gives the body/head meshes with `TEXCOORD_1`
- `native_glb.animations(model, [(name, clip_glb)], out)` merges clip GLBs
- `batch.clips(repository)`: the clip list from `anims/` (FBX only, exclusions, loop
  heuristic, casting flag)
- `casting.manifest`
- `standard_retarget.retarget_fbx(rig, path, profile, name)` with profile `mixamo` or
  `rpg`, where `rpg` applies to paths under `RPG Animations GLB-0.1.0/`

## 7. Export layout and validation

### Layout (matches `ENGINE_CHANGES.md` §1)
```
EXPORT/<batch>/catalog.json
EXPORT/<batch>/bases/<base>/base.glb, base.json, anims.glb, clips.json, cast.json, work/ (per-clip checkpoints)
EXPORT/<batch>/items/<item_id>/item.json, <base>.glb, <base>.json
EXPORT/<batch>/logs/
```
- `<batch>` defaults to `YYYYMMDD-vN`. Never overwrite an existing batch's files unless
  `--resume` is given, and then only skip or complete the missing ones.

### Base export (D)
1. In memory: `fix_weights`.
2. Ensure the head morphs.
3. Compute `attachment_bindings` and offsets.
4. `split_head`.
5. `export_glb(base.glb, rig, [body, head])`.
6. Write `base.json`: `{version 1, base, skeleton_sha256, bones, sockets[], attachments
   (same record shape as Armor Forge base.json: triangle, vertices, barycentric, weights,
   bone, morph_offsets), morphs[], base_body "body", default_head "head", legless,
   spacing{arm_deg, leg_cm}, source{project, sha256, rig_fingerprint}}`.

### Animation export (D)
- The clip list is `batch.clips(REPOSITORY)`: 1257 FBX (Mixamo plus RPG).
- Per clip: retarget, apply spacing, export a clip GLB to `work/<clip>/clip.glb` plus
  `clip.json` (source sha, settings hash, frames, fps, loop, skeleton sha).
  - Resumable: skip a clip whose `clip.json` matches the source sha, settings hash and
    skeleton sha.
  - Each Blender process handles a chunk of clips (default 40). The process reopens the
    base file every N clips, to bound memory.
- Then `native_glb.animations` merges them into `anims.glb`, and `clips.json`
  (`{name: {start, end, fps, loop, source, set (mixamo|rpg)}}`) is written. Casting clips
  need a reviewed `cast_annotations.json` entry; if one is missing, skip that clip with a
  warning rather than failing the whole base.

### Spacing (D, `blender/spacing.py`)
- **Arms**: after retarget, a per-frame additive rotation of `c_arm_fk.l/.r` about the
  bone's local X axis (abduction; check the sign per side so positive = away from the body).
  The amount is `arm_deg * fade`, where `fade = clamp((90° - current abduction) / 60°, 0, 1)`
  so raised arms are untouched. Keys are dense.
- **Legs** use IK (`c_foot_ik.l/.r`): a constant world-X offset of `±leg_cm` on the foot IK
  controls over the whole clip, plus the same offset on `c_leg_pole.l/.r`.
- Settings live per base in `scene["outfit_studio"]["spacing"]` (`arm_deg`, `leg_cm`,
  `overrides{clip: {arm_deg, leg_cm}}`).
- `ostudio.spacing_preview` applies the offset to a copy of the active action for viewing.

### Item export (E)
For each approved item (or `--all`) in an outfit project:
1. Run in a fresh process on the saved outfit file. In memory only: make the library
   override local (`bpy.ops.object.make_local(type='ALL')`) if ARP refuses the override rig.
2. Rename each channel's material to the channel id (`OSCH_<channel id>`), so that
   glTF primitives map to channels by material name.
3. `fix_weights`.
4. `export_glb(<base>.glb, rig, pieces, twist=False)`.
5. Read the GLB JSON and write `<base>.json`: `{version 1, item_id, base, skeleton_sha256,
   nodes{node_name: {piece, primitives: [channel_id per primitive], rigid_bone|null}},
   morphs[], tris, source{…}}`.
6. Merge into `item.json`: `{version 1, id, label, type, channels[{id, label, role,
   material}], races[]}`.

Also:
- `skeleton_sha256` must equal `bases/<base>/base.json`; fail if not.
- Finally, write `catalog.json`: `{version 1, format "outfit_studio_v1", bases{base:
  skeleton_sha256}, items[{id, label, type, races}]}`.

### Validation (E, `blender/validate.py`; also run before export)
Errors:
- a deform group that is not a deform bone
- a vertex with more than 4 influences or a weight sum ≠ 1, unless fix_weights would repair it (then a warning)
- a rigid piece not bound to its table bone(s)
- a face whose `os_channel` is out of range
- a role not in the palette roles
- a missing `bulk_*` key that the base has
- animation data on item objects
- a stale rig fingerprint
- a gauntlets/boots pair asymmetric by more than 1 mm
- an empty piece

Warnings:
- tris over budget (default 2000 per item, configurable)
- an item missing on some bases (listed)
- channel ids differing between bases for the same item
- status not approved

`ostudio.validate` shows the results in the panel, with "select offending faces/verts" per entry.

## 8. Batch (`batch.py`, `run.py`)
`run.py` is a plain python3 CLI that spawns Blender headless per base. Commands:
- `import --base <b>|all --set <s>|all [--lod 0p4]` creates or opens the outfit project
  and imports new items, skipping edited ones.
- `export-base`, `export-anims`, `export-items`, `validate`.
- Logs go to `EXPORT/<batch>/logs/`.
- Exit codes: 0 = ok, 1 = failures (listed), 2 = usage.

## 9. Tests (`tests/`)
- `test_core.py` (plain python3): transform maths, channel building, morph barycentric
  delta, AP sampling on a synthetic sphere/cylinder (spacing CV, symmetry), hash stability.
- `test_blender.py` (`blender -b --factory-startup --python`):
  - create an outfit project for `dwarf_male` in a temp dir (linking projects/dwarf_male.blend);
  - import `plain_plate_mail_01` body_armor and right_gauntlet_boot;
  - assert the pieces, channels, weights (rigid = one bone), mirrored pair symmetry,
    morph keys and median distance to the body;
  - simulate a hand edit, then re-import → `__reimport`;
  - run validation;
  - export into a temp dir and re-import the GLB, checking the skin joint names.
- Tests must never write inside `projects/`, `models/` or four-ways.

## 10. Module ownership and interfaces

Each module is owned by one implementer. Other owners call its functions but never edit it.

| Owner | Files |
| --- | --- |
| A (shell, UI) | `__init__.py`, `paths.py`, `blender/props.py`, `blender/project.py`, `blender/ui.py`, `blender/views.py`, `blender/ops_outfit.py`, `blender/proxies.py`, `tests/test_blender_ui.py` |
| B (items, import) | `blender/items.py`, `core/pipeline_source.py`, `core/transform.py`, `core/channels.py`, `core/morph_transfer.py`, `core/hashing.py`, `blender/importer.py`, `blender/weights.py`, `blender/morphs.py`, `data/rigid_bones.json`, `tests/test_core.py`, `tests/test_blender_import.py` |
| C (AP placement) | `core/ap/*`, `blender/ap_ops.py`, `tests/test_ap.py`, `tests/test_blender_ap.py` |
| D (base and animation export) | `blender/export_base.py`, `blender/anims.py`, `blender/spacing.py`, `tests/test_blender_export_base.py` |
| main thread | `blender/arp_export.py` (read-only for others) |
| E (item export, validation, batch) | `blender/export_items.py`, `blender/validate.py`, `batch.py`, `run.py`, `tests/test_blender_export_items.py` |

Every `blender/*.py` module that has operators exposes `CLASSES` (a tuple) and an
optional `draw(layout, context)`. `__init__.py` registers the `CLASSES` of every module
listed in `MODULES` and skips any module that doesn't exist yet.
`ui.py` calls these drawers:
- `importer.draw_import(layout, ctx)`
- `items.draw_item(layout, ctx)`
- `ap_ops.draw(layout, ctx)` (base mode)
- `export_base.draw(layout, ctx)` (base mode)
- `anims.draw(layout, ctx)` (base mode)
- `validate.draw(layout, ctx)`
- `export_items.draw(layout, ctx)` (outfit mode)

The `operator` prefix is `ostudio.`. Fixed idnames:
- A:
  - `ostudio.project_create`
  - `ostudio.project_open`
  - `ostudio.item_prev`, `ostudio.item_next`
  - `ostudio.view` (prop `name`), `ostudio.view_reset`
  - `ostudio.status_set`
  - `ostudio.palette_reroll`
  - `ostudio.head_swap`
  - `ostudio.concept_overlay`
  - `ostudio.poke_check`
- B:
  - `ostudio.import_pipeline`
  - `ostudio.reimport`
  - `ostudio.reimport_resolve` (prop `choice` keep|take|compare)
  - `ostudio.rebind_stale`
  - `ostudio.check_rig`
  - `ostudio.mirror_pieces`
- C:
  - `ostudio.ap_generate`
  - `ostudio.ap_commit`
  - `ostudio.ap_lock`
  - `ostudio.ap_clear_preview`
  - `ostudio.ap_report`
- D:
  - `ostudio.export_base`
  - `ostudio.export_anims`
  - `ostudio.spacing_preview`
- E:
  - `ostudio.validate`
  - `ostudio.export_items`

Shared APIs:
- B, `items.py`:
  - `list_items(scene) -> [coll]`
  - `item_data(coll) -> dict`
  - `set_item_data(coll, dict)`
  - `item_pieces(coll) -> [obj]`
  - `current_item(context) -> coll|None`
  - `piece_hash(obj) -> str`
  - `is_edited(coll) -> bool`
  - `ROLE_COLOURS` (loaded from `palette.json`)
- B, `importer.py`:
  - `base_objects(context) -> (rig_override, base_override)`
  - `base_rest_mesh(base, rig) -> (V, F, part_per_face, part_names)` (world space, REST pose)
- A, `props.py`:
  - `scene.ostudio` (PropertyGroup) with
    - `item_index`
    - `filter_text`, `filter_type`, `filter_status`
    - `lod`
    - `view`
    - `mix_outfit`, `mix_body_armor`, `mix_gauntlets`, `mix_boots` (item ids)
    - `thickness`
    - `pose_action`
