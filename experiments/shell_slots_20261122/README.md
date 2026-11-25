# Shell / slot / layer prototype (2026-11-22 to 2026-11-25)

The layer builder moved to **`tools/layers/`** (pipeline v2, phase 2): `layers.py`, `geodiv.py`, `shell_slots.py`, `pieces.py`, `visibility.py`,
`process_all.py` and the check tools (`checks/`) were `git mv`ed there, the dead "shell" method code was pruned, and `tools/layers/build.py` is the
per-module entry point (`python tools/layers/build.py module <module_dir> --lod 0p2`). See `tools/layers/README.md`.

The original prototype code, exactly as it was while the reference layers below were built, is in commit `a626d23` (tag `pre-pipeline-v2`):
`git show pre-pipeline-v2:experiments/shell_slots_20261122/layers.py`.

What stays in this folder:

- `out/` - the prototype's outputs, the user's reference for comparison. **Do not write into it.** `out/layers/<race>/<slot>__<set>.glb` are the layers
  (+ `.json` notes, `scenes/*.npz`), `out/layers/mix.html` the try-on page served on :8790 by `serve.py`.
- `serve.py` (Mix page server on :8790): **variants are retired** (2026-11-25). `GET /api/variant` answers `410 {"status": "error", "error": "variants retired; the layer
  builder moved to tools/layers ..."}` without touching any file, `GET /api/queue` is always empty, and no build worker is started; the static pages in `out/`
  are served as before. A :8790 process started before this change keeps the old code until it is restarted (its `/api/variant` cannot build: the old command
  and geometry paths are gone).
- `render.py`, `build_variants.py`, `imprint.py`, `seal.py`, `watertight.py`, `zipper.py`, `design_symmetry.py` - the older shell-method
  scripts. They import the moved modules (`geodiv`, `shell_slots`, `visibility`) and no longer run from here (put `tools/layers` on `sys.path`
  and the old `geodiv.py` batch / variant commands are gone anyway - they were pruned); the static viewer pages in `out/` still work.
