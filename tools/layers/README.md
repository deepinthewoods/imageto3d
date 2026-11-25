# tools/layers - the layer builder (pipeline v2, phase 2)

Builds the **swappable layers** of a generated module: per slot one GLB with the garment and its own skin cut on the game body's ring loops, with
the morph target `fill` (the Amount slider). Moved here from `experiments/shell_slots_20261122/` (commit `a626d23` / tag `pre-pipeline-v2` has the
original) with the geometry code unchanged; see `PIPELINE_V2_PLAN.md` section 6 and `tools/preview/v2/DESIGN.md`.

| Module | Slots it builds (all REQUIRED) |
| --- | --- |
| `outfit` | `outfit` (everything below the neck) |
| `body_armor` | `torso` |
| `right_gauntlet_boot` | `lower_arm.l`, `lower_arm.r`, `lower_leg.l`, `lower_leg.r` (the `.l` ones mirrored from the right, JSON `mirrored_from_right: true`); a race whose game body has no leg slots (gnome, decided by `layers.slots()`, not by name) needs only `lower_arm.l/.r` |
| bare (per race) | `head`, `torso`, `lower_arm.l/.r`, `lower_leg.l/.r` (legs only where the body has them): the game body's own faces (no `fill` morph) |

**A required slot with no garment fails the build** (`no garment found for lower_leg.r (boot)`): a one-glove or one-boot publish, or a module whose boots
were labelled as skin, never reaches Aligned. The contract is checked twice: on the layer code's result (`build._check_result`) and again, from
`build.json`, whenever a build is reused (`build.validate_build`).

## API (`build.py`)

```python
import sys; sys.path.insert(0, 'tools/layers')
import build
r = build.build_module('models/<set>/<job>/<race>/<module>', '0p2')            # out_root = <module>/layers, gpu=True
r = build.build_module(module_dir, '0p2', out_root='D:/x/layers', gpu=False, force=True)
b = build.build_bare('human_male')                                             # out_root = tools/preview/cache/layers/human_male
```

Each returns the build record (the content of the LOD's pointer file) with `status`:

- `ok`: built now and published; `current`: a published build with the same fingerprint exists **and validates**, nothing was built (or an identical
  winner of a concurrent worker / an earlier interrupted publish was adopted); `failed`: `error`, `traceback`, `previous_build` (the LOD's published
  build, also on missing-input failures), `retryable: true` for `inputs changed during build`; `last_failure.json` written (see below), pointers and earlier
  builds untouched.
- `force=True` (`--force`): skip the `current` short-circuit and the adoption; the build is made again under a fresh `-r<n>` id and the pointer moved to it
  (CPU / GPU comparisons: the GLBs are byte-identical, the record's `gpu` says which path ran).
- `ValueError` for a `lod` not in `0p4 / 0p2 / 0p1` or a module dir that does not exist (the CLI turns it into a JSON failure). A missing
  `reduced/lod_error_<lod>.glb`, `manifest.json`, `fit/align.json`, an unknown module name, a race that is not a single identifier or has no game body
  cache is a `failed` result. `build_bare` with an invalid race (anything but `[a-z][a-z0-9_]*` with `tools/preview/cache/bases/<race>.npz` and
  `<race>_rig.npz`) returns `failed` **before any filesystem write** (`out_root: null`, nothing created, so a caller-supplied `../x` cannot select a folder).
- Production runs **one build per process** (the layer lane starts a fresh process per task). Repeated in-process calls are safe too: before each build the
  game-body caches of the race (`body_fit._BODIES`, `geodiv._GAME_BODY`, `layers._LAYER_BODY`, `fit_lab._PLANES`) are dropped when the body cache files'
  (size, mtime) changed since the last build of that race in the process. `OMP_NUM_THREADS` etc. default to 1. `gpu=True` sets `BODY_FIT_GPU=1` before
  `body_fit` is imported; the record's `gpu` says whether the GPU path was really active (a GPU error switches `body_fit` to the CPU for the rest of the
  process and prints that to stderr, so `gpu` is false then).

## CLI

```
python tools/layers/build.py module <module_dir> --lod 0p2 [--out DIR] [--cpu] [--force]
python tools/layers/build.py bare <race> [--out DIR] [--cpu] [--force]
python tools/layers/build.py parity <ref_dir> <ref_set> <new_dir> [--lod 0p2] [--json FILE]
```

Works from any cwd. Progress lines go to stdout; **the last stdout line is the result JSON on every path**: build results, failed builds, argument
errors (`argument error: ...`), invalid lod / race, an output path that is a file, unreadable inputs, unexpected exceptions. Exit 0 for `ok` / `current`,
1 for `failed` (never 2 with an empty stdout). `--help` is the only path that prints text and exits 0.

`parity` (strict): `<new_dir>` is a build folder or an out_root with pointers (`--lod` picks `current_<lod>.json`, else it must be the only pointer); it
compares each slot GLB with the prototype's `<ref_dir>/<slot>__<ref_set>.glb` and fails (exit 1) on a missing folder / build / reference, a required slot
without reference, a slot in only one of the two, a face-count or position / **winding** mismatch (faces are matched by their corners in cyclic order),
face materials (role + provenance) or **colour values** (`baseColorFactor`) that differ, vertex positions more than 0.05 mm apart at morph weight 0 or 1,
differing notes or `mirrored_from_right`. The one accepted difference is the old hash-seed role tie-break: a face whose role differs only between two
garment roles (same provenance) is counted separately as `tie_break_faces` / `tie_break{slot: n}` and tolerated up to 12 per slot.

## Outputs

```
<out_root>/
  current_<lod>.json      the published build of that LOD (current_0p4 / current_0p2 / current_0p1; bare: current_bare.json); replaced atomically
  last_failure.json       the last failed build (carries lod + module); cleared by the next success of the same LOD + module
  .publish.lock           the exclusive publish lock (msvcrt byte lock / fcntl.flock); held only while publishing
  <build_id>/             '<lod>-<fp12>' (module) or 'bare-<fp12>', or '<...>-r<n>' (recovery / force); never deleted or rewritten by the builder
    <slot>.glb            flat-shaded, one primitive per (role, provenance), material names 'role:prov', morph target 'fill' (module layers)
    <slot>.json           the prototype's notes (kind, module, load.source_lod, notes{...}, warnings, seconds, mirrored_from_right)
    scenes/<slot>__a0.npz, __a100.npz   gV positions, gF faces, gC role colours, gRole / gProv role and provenance NAME per face (the check tools read these)
    build.json            = the pointer's content
  .attempt-<tag>-<fp12>-<pid>/   the build while it runs (removed after a failure; abandoned ones are cleaned, see below)
```

There is **no single `current.json`**: one pointer per LOD, so a 0p2 and a 0p4 build of one module never replace each other.
Pointer / `build.json`: `build_id, fingerprint, lod, status:'ok', time, seconds, slots{slot:{glb, json, faces}} (paths relative to out_root), required[]
(the slots the contract demanded), warnings[], metrics{wall_crossings_left (max over slots and both Amounts), wall_crossings_left_by_slot, boundary_edges,
ring_vertices_merged, ring_vertices_inserted, skipped, mirrored_from_right}, gpu, module (repo-relative), race, set, kind, peak_rss_mb`.
`ring_vertices_merged{slot: n}` is the number of the seam rings' own vertices that are no longer a vertex of the layer; `ring_vertices_inserted{slot: n}`
the other layer vertices lying on a ring line / inner ring (inserted by the snap): two separate, never negative counts.

## Publishing (validation, locking, recovery)

1. **Reuse** (`current`) only when the LOD's pointer has this fingerprint **and** `validate_build` of its folder is empty: `build.json` present, ok, same
   fingerprint, a required-slot list that satisfies the kind's contract, and for every required slot the JSON, and the GLB parsed whole (header, JSON
   chunk, BIN chunk length equals the file, buffer views and accessors inside the buffer, every primitive's POSITION and morph accessors, materials,
   morph target `fill` for module layers, face count equal to `build.json`). The check is cheap (reads the files, no geometry).
2. Otherwise the layer code runs into `.attempt-*`, the result is checked (every required slot built), and the **commit** happens under the exclusive
   `<out_root>/.publish.lock`:
   - abandoned `.attempt-*` folders / stray `*.tmp-<pid>` files are removed (older than 1 h with no live owner pid, or older than 24 h);
   - the input fingerprint is **recomputed**: if it differs from the one the build started with, the attempt is discarded and the result is `failed`,
     `inputs changed during build (retry)`, `retryable: true`; nothing else is touched (no pointer, no `last_failure.json`);
   - a valid build with the same fingerprint (`<id>` or a recovery `<id>-r<n>` already on disk) is **adopted**: the attempt is discarded, the pointer is
     (re)written from that folder's own `build.json` if it does not already name it, result `current`;
   - otherwise the attempt is renamed to `<out_root>/<build_id>/` and `current_<lod>.json` replaced atomically. If `<build_id>` exists but is damaged
     (it failed validation) it is **left exactly as it is** and the new build gets `<build_id>-r<n>`: an approved build is immutable.
3. A failed attempt removes itself; under the lock, `last_failure.json` is written only if the fingerprint is still current (a superseded worker never
   writes it) and no valid build with that fingerprint has appeared meanwhile (then that build is adopted).
So concurrent workers on one output root (different LODs: independent pointers; same LOD, same inputs: one builds, the other adopts; same LOD, inputs
changed in between: the older one fails retryably) can never replace a newer selection or leave a build without its pointer or a pointer without its
build. A hard crash between the rename and the pointer write leaves a valid folder that the next call adopts.

## Fingerprint

sha256 over (key=value lines): the schema number, the module identity `<set>/<job>/<race>/<module>` and kind (the same bytes under another pinned job or
module are another build), every repo-local file the build imports (`build.CODE_DEPS`: `tools/layers/{layers,geodiv,shell_slots,pieces,build}.py`,
`tools/tripo_pipeline/{body_fit,align_body,fit_lab,gpu_geometry,rewear,portable}.py`, `tools/outfit_studio/core/transform.py`,
`experiments/body_mask_20261104/bm_core.py`), `tools/preview/cache/bases/<race>.npz` and `<race>_rig.npz`, `<module>/fit/align.json`,
`reduced/lod_error_<lod>.glb`, its `_colors.npz` (or `absent`), `reduced/manifest.json`, and the lod string. Source files are hashed with LF line endings.
Bare: schema, kind, race, code, the two body caches. `build_id` = `<lod>-<fingerprint[:12]>` / `bare-<fingerprint[:12]>`. CPU and GPU share a key
(the results are identical); `--force` exists for comparisons. `test_build.py::test_dependency_closure` imports the build path in a subprocess and fails
if a repo-local file reaches `sys.modules` or `spec_from_file_location` that is not in `CODE_DEPS`: **a new import in the build path must be added there.**

## Inputs are not trusted blindly

Colour sidecars (`reduced/lod_error_<lod>_colors.npz`) and the rig cache are loaded with `allow_pickle=False` through `shell_slots.load_npz`, which also
rejects object / wrong-typed arrays with a clear `ValueError`. The align stage's own loaders in `tools/tripo_pipeline/align_body.py` (not part of the layer
build) still use `allow_pickle=True`; they are outside this tool.

## What was pruned

Only code provably unreachable from the layer path (name-based call graph from `module_layers` / `bare_layers` / `write_layer`, checked by grep; the
dynamic uses were `globals()` in the removed variant switch and `getattr` copying body attributes): the prototype's combined-result method of
`geodiv.py` (`process_module`, `run_outfit`, `run_armour`, symmetry / spike / sliver code, variants), the plane-cut / zipper / stitch code of
`shell_slots.py`, the pieces driver of `pieces.py` (only `fill_layer_guard`, switched off by `LAYER_GUARD = False`, remains), the batch drivers
(`main`, pools, `write_index`, `LOD_OVERRIDE`) and `process_all.py` (only `jobs()` discovery left, used by `checks/`). `imprint.py`, `seal.py`,
`watertight.py`, `zipper.py`, `design_symmetry.py`, `render.py`, `serve.py`, `build_variants.py` stayed in the experiment folder.
Constants and thresholds of the live code are untouched. One change in behaviour: `geodiv.relabel_off_body_skin` broke a tie between garment roles by
the set order of role strings, which depends on `PYTHONHASHSEED` (1-4 faces of a layer changed role between runs; the geometry never did). A tie now
goes (`geodiv.pick_role`) to the earlier role of `primary, secondary, tertiary, accent, joint`, then, for custom roles with no canonical place, to the
lexically smaller name, so a build is reproducible (tested under six hash seeds, standard and custom roles).

## Parity with the prototype

Method (`tools/layers/build.py parity`, strict, see CLI): build the module with the LOD the reference JSON records (`load.source_lod`), then per slot
compare the new GLB with `experiments/shell_slots_20261122/out/layers/<race>/<slot>__<set>.glb`. Results of the 2026-11-25 phase-2 review fixes (GPU,
scratch output; eight modules + two bare races): every compared slot has the same faces in the same winding, identical colours, identical notes and
0.0 mm vertex difference at both morph weights; the only role differences are the tie-break (dwarf_female Terrapin plank: 2 faces).

## Tests and checks

- `python tools/layers/test_build.py` (about 20 s, no GPU / game body / real model): fingerprint (identity, kind, code dependencies, dependency closure
  against the real import path), required slots (one-glove human fails, gnome two gloves publish), damaged-build recovery under a fresh id (missing JSON /
  GLB / `build.json`, empty slot map, truncated GLB), concurrent publishers in separate processes (different LODs, same LOD, older worker superseded),
  inputs changed during a build, invalid bare races writing nothing, the CLI's JSON on every failure path, in-process cache reset, parity (reversed
  winding, changed colours, missing slots / inputs, tie-break), role tie-break under `PYTHONHASHSEED` 0-5, pickle-free loads.
- `checks/` (see its README): audit / render / rebuild tools. Run them on scratch builds (`build.py ... --out <scratch>`); never write into a module's own
  `layers/` folder or into the prototype's `out/`. `rebuild_match.py` writes only to a scratch folder (default `<system temp>/layers_rebuild_match`);
  **production rebuilds go through the app** (its layer lane builds the pinned job at the approved LOD). `checks/rebuild_open.py` was removed (it rewrote the
  prototype index; the pointer records `boundary_edges` per slot instead).
