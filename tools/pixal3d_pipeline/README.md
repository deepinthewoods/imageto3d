# Pixal3D: free local image->3D backend

`convert_job.py <job> --backend pixal3d` converts a staged four-view job on this PC's GPU (RX 9070, gfx1201) with
Pixal3D through the C++ port `pixal3d.cpp` (`trellis-cli --views`). Pixal3D is TRELLIS.2 with
camera-aware multi-view conditioning: it projects each view's image features into the 3D grid through that view's real
camera, so it takes our four views with the cameras of the job's frozen `.base_views/<race>/transforms.json` (the dwarf
20-degree front three-quarter and the gnome back three-quarter camera (back-left until 2026-11-22, back-right since) need nothing special). Added 2026-11-18 at the user's request
after a side-by-side with Tripo and TRELLIS.2 (ComfyUI); Tripo stays the default backend.

Cost: none (local). Time: ~4.5 min per module (generation) plus the usual reduction/split/align.

## How it runs

- `pixal3d_backend.py submit <views_dir> <out_dir> [--seed-offset N]`: stages the view PNGs (hard links) and the matching
  cameras (only the frames whose PNG exists, front first) in `<out_dir>/pixal3d_input/` and writes
  `studio_submission.json` (`backend: pixal3d`, `status: submitted`, seed). Nothing is generated yet.
- `pixal3d_backend.py collect <out_dir>`: runs the generator, takes the GLB, turns it 180 degrees about the up axis
  (Pixal3D faces glTF -Z, Tripo +Z; done in the vertex data), saves `glb_pixal3d.glb` and records it as `exports.GLB`
  (`status: exported`). A failure leaves `status: submitted`, so re-running `convert_job.py` resumes at collect.
- On Windows the generator runs in WSL (`wsl.exe -d Ubuntu-24.04 --exec bash -c ...`; `--exec` matters: with `--` WSL
  re-parses the script through the default shell). In a private work dir under `work_dir`; only the GLB is copied back.
- One generation at a time per machine (`lock_file`, a blocking file lock): the model needs most of the 16 GB GPU.
- Broken mesh: if the reduction flags the mesh (`regenerate_reason`, e.g. an interior shell), `convert_job.py` moves the
  module to `<module>_attemptNN_<reason>/` and regenerates with the next seed, up to `retries` times (free).
- `convert_queue.py --backend pixal3d` runs a worker that converts with Pixal3D only; `--fallback pixal3d` uses it instead
  of Hyper3D once the Tripo credits run out.

## From the app

The preview app's Queue tab has a **Backend** setting (`tripo` in `.codex_jobs/_app/settings.json`): `pixal3d` starts the convert worker with
`--backend pixal3d` (every module is generated here, free, about 4.5 min each, the Tripo balance is never read) and `auto_pixal3d` with
`--fallback pixal3d` (Tripo first, Pixal3D for the modules left once the credits run out). Saving a changed value restarts a running convert
worker gracefully (after its running job). The GPU generation runs in `pixal3d_backend.py collect`, so `convert_job.py` waits at the supervisor's
pause gate before it (conversions idle while the user is at the PC); the Run everything now estimate counts Pixal3D modules at 0 credits.

## config.json

| key | meaning |
|---|---|
| `cli`, `lib_dir` | the patched build (below); `lib_dir` goes on `LD_LIBRARY_PATH` |
| `models` | the GGUF weights (`pixal3d-q8_0-v1`: dinov3, NAF, `*_mv` flows, decoders) |
| `args` | `--pixal3d-weights mv --res 1024 --gss 10.0 --atlas 2048 --webp off --require-gpu` |
| `seed` | base seed (42); a regeneration adds 1, 2, ... |
| `remesh` | `true`: the reference narrow-band UDF remesh (closed, one component). `false`: keep the raw dual-grid mesh (patched build, `PIXAL3D_NO_REMESH=1`). See "Mesh properties" |
| `rotate_y_deg` | 180 (turn to Tripo's facing) |
| `retries`, `timeout_s`, `min_faces` | regeneration count, generation timeout, smallest acceptable mesh |

Settings considered and left alone: `--res` (1024 is the multi-view minimum), the 1M-face decimation (GPU, 2 s; our
reduction wants a Tripo-sized input), `--box-uv` (35 s faster than xatlas but opened holes and gives less even texel
coverage, and the labels come from the texture), `PIXAL3D_STEPS` (fewer flow steps: ~20 s for a quality risk).

## The patched build

Source: `F:\_dev\sd\assetfurnace\vendor\pixal3d-cpp` (another project; never edited), copied to `~/pixal3d/src` in WSL and
built there (`cmake -DGGML_HIP=ON -DAMDGPU_TARGETS=gfx1201 -DCMAKE_HIP_ARCHITECTURES=gfx1201 -DTRELLIS_WEBP=ON`, target
`trellis-cli`; the original binaries copied earlier stay in `~/pixal3d/build`). `patches/trellis_cli_local.patch` holds
both local changes to `src/trellis_cli.cpp`:

1. **GPU-resident SLAT conditioning.** The CLI used the host path `pixal3d_cond_slat`, which reads each view's NAF feature
   map back from the GPU (1 GiB at T=512, 4 GiB at T=1024, channel-major) and samples it on the CPU with 4 MB strides:
   30-80 s per view under WSL, ~70% of the run. The library's own device-resident `pixal3d_cond_slat_gpu` (written for the
   WebGPU build; chunks the NAF output above 1 GiB) keeps it on the GPU and gathers only the active voxels. 667 s -> 294 s
   (-> ~250 s with the 2048 atlas). `PIXAL3D_COND_HOST=1` restores the host path for A/B.
2. **`PIXAL3D_NO_REMESH=1`** skips the UDF remesh in the multi-view post-process (config `remesh: false`).

WSL/ROCm prerequisites (librocdxg, `HSA_ENABLE_DXG_DETECTION=1`, ...) are in the local-3d-gen notes; the generator
prints `ROCm0` when it found the GPU.

## Mesh properties and the pipeline (trial 2026-11-18, dwarf_male magic_leather_08 body_armor)

Run through the real chain (`convert_job.collect_and_reduce` with `--adaptive-hue on`, segment-first split, align + fit)
into `experiments/pixal3d_trial/` (remesh on) and `experiments/pixal3d_trial_noremesh/` (remesh off), against the Tripo
conversion of the same module:

| | Tripo | Pixal3D, remesh (default) | Pixal3D, no remesh |
|---|---|---|---|
| source | 1.33M faces, 8 hole / 5 non-manifold edges | 952k faces, closed, 1 component, ~6k non-manifold edges | 969k faces |
| interior audit (enclosed area) | 7% | 53% -> 2.6% after the strip | 67% -> 0.06% after the strip |
| level 0p2 triangles / components | 4,690 / 4 | 11,280 / 170 | 41,134 / 11,015 |
| level 0p2 hole / non-manifold edges | 0 / 1 | 4,486 / 184 | 42,633 / 5,528 |
| wearable cut, segment-first split, align | ok (residual 14.7 -> 7.2 mm) | ok (residual 11.3 -> 9.5 mm) | stopped (not worth finishing) |

- The non-manifold edges are tolerated: every step ran without error.
- Both Pixal3D variants are double-walled (the remesh wraps thin sheets as two-sided skins; the raw decode is two-sided
  too). reduce_model's interior strip removes the inner layer; with the remesh the result is usable, without it the mesh
  shatters. Keep `remesh: true`.
- Because of that built-in double skin `convert_job.py` judges a pixal3d mesh by the share left AFTER the strip
  (`pixal3d_regenerate_reason`); the Tripo rule would regenerate every pixal3d module.
- Labels: the labelled wearable is clean (scale outlines, pauldrons, collar pleats, the magenta gem), but levels come out
  ~2.5x denser than Tripo's at the same geometric error and are flagged `material_review_required` (25% of the area decided
  without seen texture, mostly neighbours, against Tripo's ~20%).
