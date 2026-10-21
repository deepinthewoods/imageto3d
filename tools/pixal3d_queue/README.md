# Approved armor → Pixal3D service

**Historical workflow (retired 21 October 2026).** The user chose Tripo Studio and approved front-only selection. `pixal3d-armor-queue.service` is disabled and stopped. Do not reactivate it against the new `approved/` folder. Current instructions are in [the Tripo pipeline guide](../tripo_pipeline/README.md). The details below document earlier jobs and their recovery only.

`pixal3d-armor-queue.service` watches this project's `approved/` folder every 10 seconds. It runs without AssetFurnace's browser interface. The user service is enabled at startup; this machine already has user lingering enabled, so it can run while logged out. The PC must be awake and the Samsung drive mounted.

## Use

Move an entire set from `pending/` into `approved/`. For every character in that set, provide both modules, each with all four images:

```
approved/<set>/<race_gender>/
  body_armor/
    front.png
    front_three_quarter.png
    back.png
    back_three_quarter.png
  right_gauntlet_boot/
    front.png
    front_three_quarter.png
    back.png
    back_three_quarter.png
```

The `<race_gender>` folder must match a folder under `base_views/`. Other files such as `prompts.json` are retained. Incomplete or invalid sets remain in `approved/`; `.queue/status.json` gives the reason. New sets are generated with all eight images in pending before approval. An older incomplete set stays waiting until completed; preferably move it back to pending while generating missing views. Once all inputs have remained unchanged for 20 seconds, the set is eligible. Ready sets run in name order, serially. A large generation will delay scanning for new jobs until it finishes.

The service copies **the base character's `transforms.json` byte for byte**, alongside the **four generated armor images**, as explicitly requested. It preserves filenames, camera order, matrices, FOV and mesh scale. It does not crop, resize, pad, reframe, remove backgrounds, infer cameras, or rotate the final model. It verifies dimensions against each corresponding base and requires nonempty transparent RGBA images. This checks the input format, not geometric correspondence between generated images and base cameras.

## Where things go

- `approved/`: waiting for valid complete inputs, or queued.
- `processing/`: claimed set currently generating, or waiting to resume after restart.
- `completed/<set>--<job-id>/`: original set plus `.pixal3d-job.json`, moved only after **both modules for every character** succeed.
- `failed/<set>--<job-id>/`: original set and error details if a generation fails. Successful models from other modules remain available.
- `models/<set>/<job-id>/<race_gender>/<module>/inputs/`: immutable input snapshot, including original base transforms and hashes.
- `models/<set>/<job-id>/<race_gender>/<module>/attempt_01/model.glb`: generated model. Each interrupted retry gets another attempt directory, preserving logs and partial output.
- Next to each GLB: `model.provenance.json` and `generation.log`. Pixal3D may also emit auxiliary files.
- `models/<set>/<job-id>/results.json`: paths to all successful outputs.
- `.queue/status.json`: current queue summary and active log path.

Restarting resumes `processing/` first and skips completed outputs whose stored hashes still match. A failed/interrupted model is never treated as completed merely because a partial `.glb` exists. Up to three interrupted attempts are allowed. Backend errors go to `failed/` immediately rather than looping forever.

To retry a failed set with the **same frozen inputs**, move its folder back from `failed/` to `approved/` and retain `.pixal3d-job.json`. Already successful modules are skipped. If you need to change inputs, change settings, or exceed the interruption limit, first move the folder to `pending/`, give the set a new name, remove only its `.pixal3d-job.json` metadata, make changes, and move it back to `approved/`. Previous models/logs remain untouched. Do not edit files inside `processing/` or `models/.../inputs/`.

## Service controls

```bash
systemctl --user status pixal3d-armor-queue
journalctl --user -u pixal3d-armor-queue -n 50 --no-pager
systemctl --user stop pixal3d-armor-queue
systemctl --user start pixal3d-armor-queue
systemctl --user disable --now pixal3d-armor-queue
```

Stop interrupts the current conversion; starting resumes it as a new attempt. The service has a single-worker lock. Its serialization applies to this queue, not independently launched AssetFurnace or Blender jobs; those share the GPU.

`config.json` holds paths and settings: RX 9070 (`HIP_VISIBLE_DEVICES=1`), multiview weights, 1024 geometry resolution, 12 sampling steps, seed 42, structure guidance 10, four-hour timeout. The existing AssetFurnace Pixal3D helpers provide the command, ROCm environment and license provenance. This user's explicit Pixal3D selection is recorded with conditional-model use enabled; no licensing checks are removed. The service never downloads weights. No AssetFurnace source files were changed.

Restart after editing config. Inspect without generating:

```bash
"/media/niz/Samsung SSD/_dev/sd/assetfurnace/.venv/bin/python" tools/pixal3d_queue/queue_service.py --check
"/media/niz/Samsung SSD/_dev/sd/assetfurnace/.venv/bin/python" tools/pixal3d_queue/queue_service.py --status
```

## Validation

Tests cover byte-identical transforms, four-image validation, module completeness, safe frame paths, source resolution, failure handling, restart recovery, no duplicate processing, and the actual AssetFurnace command/provenance adapter with the expensive subprocess replaced by a test process. Native preflight verifies installed multiview weights, shared libraries and RX 9070 availability. A real four-view generation has not yet run: the first approved set currently contains only front views.

```bash
"/media/niz/Samsung SSD/_dev/sd/assetfurnace/.venv/bin/python" -m unittest discover -s tools/pixal3d_queue/tests -v
```
