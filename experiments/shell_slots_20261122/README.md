# Shell / slot / layer prototype (2026-11-22 to 2026-11-25) - retired

**Superseded by `tools/layers/` and the preview app v2.** Nothing here runs any more:

- The layer builder moved to **`tools/layers/`** (pipeline v2, phase 2): `layers.py`, `geodiv.py`, `shell_slots.py`, `pieces.py`, `visibility.py`,
  `process_all.py` and the check tools (`checks/`) were `git mv`ed there, the dead "shell" method code was pruned, and `tools/layers/build.py` is the
  per-module entry point (`python tools/layers/build.py module <module_dir> --lod 0p2`). See `tools/layers/README.md`. Production builds go through the
  app: approving a set in its **3D** tab builds its layers at the chosen LOD.
- The try-on pages became tabs of the app (`tools/preview/v2/server.py`, port 8765): the prototype's `out/layers/mix.html` is the **Aligned** modal
  (Amount sliders, gloves / boots dropdowns) and the **Mix** tab (any built layers of a race).
- Phase 6 (2026-11-25) removed what was left of the prototype: its Mix page server `serve.py` (:8790), the older shell-method scripts (`render.py`,
  `build_variants.py`, `imprint.py`, `seal.py`, `watertight.py`, `zipper.py`, `design_symmetry.py`) and the viewer pages `out/index.html`, `out/view.html`,
  `out/layers/mix.html`, `out/pieces/mix.html`. They are in commit `82e6de1` (`git show 82e6de1:experiments/shell_slots_20261122/serve.py`); the code exactly as
  it was while the reference layers were built is in commit `a626d23` (tag `pre-pipeline-v2`).

## Where the data is

`out/` (not tracked; local to this PC) is kept as the user's reference. **Do not write into it.**

- `out/layers/<race>/<slot>__<set>.glb` (+ `.json` notes, `scenes/*.npz`) and `out/layers/index.json`: the prototype's layers. They are the parity
  reference of `tools/layers/build.py parity` and are read (read-only) by `tools/layers/checks/` and by `tools/preview/v2/migrate.py` (inventory only;
  they are not imported: v2 rebuilds layers on 3D approval).
- `out/pieces/`, `out/variants/`, `out/scenes/`, `out/renders/` and the `out/*.glb` combined results: older shell-method outputs.
- `out/base_*fix*_<race>.json`, `out/base_flaps_*.json`, `out/base_bottom_rebuild_*.json`: reports of `tools/fix_base_holes.py` /
  `tools/rebuild_base_bottom.py` (the game body repairs of 2026-11-24).

To look at the old pages again: `git checkout 82e6de1 -- experiments/shell_slots_20261122` and `python experiments/shell_slots_20261122/serve.py --port 8790`
(their imports need `tools/layers` on `sys.path`; the variant builds were already retired before phase 6).
