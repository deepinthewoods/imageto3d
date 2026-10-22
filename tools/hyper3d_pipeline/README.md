# Hyper3D Rodin workflow

Copied from the items project (`../../items/hyper3d_workflow`, original untouched) and extended with
`rodin_backend.py`: an optional **image-to-3D backend for the imageto3d pipeline** (high-poly textured GLB).
Everything below the "Items-project runner" heading is the original single-image/FBX documentation; it still
works but is not used by the new backend. Sample inputs mentioned there lived in the items project.

## Hyper3D as an alternative to Tripo (high-poly, no Smart Low-poly)

Tripo stays the default. Select Hyper3D per run:

```bash
python3 tools/tripo_pipeline/convert_job.py <set>--<job-id> --backend hyper3d [--dry-run] [--allow-paid --accept-preview]
```

Seam: `convert_job.py` calls `rodin_backend.py submit|collect` (via this folder's venv, `config.json: venv_python`
or `$HYPER3D_VENV`) in place of `studio.mjs`. The backend writes the same `studio_submission.json` (extra field
`backend: "hyper3d"`, `project_id` = the Rodin model UUID, `exports.GLB.path`, status `submitted` then `exported`), so
`collect_and_reduce` copies the GLB to `source.glb` and runs `reduce_model.py` unchanged. The Tripo free-retry loop is
skipped for Hyper3D (its retries cost credits): a doubled-shell mesh is only flagged `converted_with_errors`.
`module_conversion[...]['backend']` records the backend.

Flow (resumable by the server's stage, intents flushed before every click, never repeated):

1. `submit`: validates the four views like `studio.mjs` (PNG, 8-bit RGBA, identical size; never resizes), selects
   Gen-2.5 / **Medium**, uploads all views at once through the multi-file input and sets each thumbnail's direction
   (config `directions`: front = Front, 3/4 from above = Front Left, 3/4 from below = Back Right, back = Back), and
   clicks only **Free Preview** (the Generate hover must read "Free Preview", true for Extreme-Low/Low/Medium).
   Records the model URL. Free, no flag needed. `--dry-run` fills the form and screenshots only.
2. `collect`: reopens the model URL; geometry preview -> confirm in **Triangle** mode with Simplify off (unreduced
   high-poly mesh; never Smart Low-poly) -> material Generate (De-light on) -> Confirm -> Pack: `.glb`, Base Model only,
   **PBR** only (Shaded off), 2K. The GLB is checked (one mesh object, UVs, image on base colour) and saved as
   `glb_*.glb` next to the record. Credit-capable clicks need `--allow-paid`; geometry also `--accept-preview`
   (look at `review_required.png` first); the displayed cost must equal `expected_costs.<kind>` (geometry 0.5,
   material 0) or `--expect-cost kind=N`, and the running total must stay within `budget_credits` (0.5). The wallet
   balance is recorded at every stage (`balances`). Exit code 3 = intentional stop (convert_job prints it and exits 1;
   re-run resumes).

### Credit costs and the 0.5-credit configuration (2026-10-22)

Gen-2.5 Generate hover, read live per effort: Extreme-Low / Low / Medium **Free Preview**, High **0.5**, Extreme-High **1**.
Gen-1.5: Free Preview (no effort control). From the web client's code: geometry Confirm **0.5** (Quad or Triangle), **2**
(Smart Low-poly); material Generate/Confirm free at High or lower, 2 at Extreme High; Fork 0.5; download free on
Business. Nothing downloadable costs less than 0.5: Pack stays locked until geometry and material are confirmed.

| Choice | Credits |
| --- | --- |
| Free preview at Medium or lower + Triangle confirm + material at High + GLB export | **0.5 total** (the configured default) |
| Same at High (generate 0.5) | 1.0 |
| Same at Extreme-High (generate 1) | 1.5 |
| Smart Low-poly confirm instead | +1.5 |

Recommended config for a high-poly, 4-view papercraft outfit at 0.5 credits (what `config.json` ships): Gen-2.5, effort Medium
(best quality in the free-preview tier), four views with directions above, Triangle mode / Simplify off (raw faceted
mesh, highest face count allowed, sharp planes), material effort High (default) with De-light on and 8K off, export
`.glb` 2K PBR only. If a preview looks poor, redo is free (x15) before paying the 0.5 confirm; only then re-try at High if
the extra 0.5 is acceptable (`--effort` is a config edit).

Other commands: `./rodin.sh inspect <out_dir>` (read-only stage check), `./rodin.sh bind <out_dir> <model-url>`.
Settings and job/run roots are in `config.json` (jobs/runs live inside this folder, gitignored; per-module
screenshots, downloads and the record live in `models/<set>/<job>/<race>/<module>/`).

### Login and probing

```bash
./native.sh login      # normal Firefox, sign in with Google, then close that window
python probe.py        # via the venv: read-only; writes probe/<timestamp>/{findings.json,*.png,*.txt}
```

`explore.py <outdir> <steps.json>` runs a JSON list of read-only steps (go, click_text, click_xy, hover_text, snap, html) in one session and refuses spending verbs. `probe.py` never uploads or presses Generate/Download/Confirm/Unlock and stops (exit 2) when the profile is locked
or signed out. The signed-out state shows a `Login` link and a `NO_CURRENT_USER` toast on `/workspace/rodin`, **not**
a login form, so `native.sh inspect` mis-reports it as `unknown_or_signed_in`.

### Verified vs not (2026-10-22, details in `probe/20261022T-signed-in/FINDINGS.md`)

Verified offline (`python -m unittest discover -s tools/hyper3d_pipeline -p 'test_*.py'`): view validation, record
schema, intent/resume guard (no repeat after a crash), cost guard and budget, auth/balance/stage helpers, dispatch from
`convert_job.py` (Tripo unchanged, free retry skipped), GLB structure check (also on the old manual
`hyper3d/human male highpoly/base_basic_pbr.glb`: 1 mesh, UVs, 1,000,000 triangles, base-colour image).

Verified live, signed in, read-only: session/auth detection and wallet balance; multi-file image input and the
direction label list; Gen-2.5 / Gen-1.5 and effort levels with their Generate prices; stage detection on a
material-preview model (`material_preview`) and on a finished model (`export_ready`, after the `derive_stage` fix);
Material Confirm shows no price; the Pack panel (`.glb` radio, Base Model / LOD / High-poly, Shaded / PBR, 2K / 4K).

Verified live on the first free run (2026-10-22): the Multi-view tab + 4-image upload, Free Preview submit, preview stage, the Confirm popover (Smart Low-poly / Triangle with a 1M slider / Quad Mesh) and the `0.5 Credits` Confirm label; the preview screenshots of the robe test are in `experiments/multicolor_markers_20261022/hyper3d_robe_white/`. `rodin_backend.py preview <out_dir>` re-reads all of that without confirming.

Verified live on the paid robe run (0.5 credits, balance 1310.5 -> 1310.0): Triangle at the 1M slider maximum, material at High with De-light, GLB 2K PBR-only export (1,000,000 triangles, single mesh, UVs, 3 images, front = -Y like Tripo).

From the client's code only: 0.5-credit geometry Confirm, Triangle = raw mesh with Simplify off, Smart Low-poly 2.

**Not verified (needs a supervised run):** the geometry-confirm popover (Triangle / Simplify selectors; the 0.5 label),
the Symmetric setting (a selector in the Geometry card, default shown `Symmetric`), per-image directions (the upload-time picker never opened; the Geometry card has a Direction bar), the right direction for our 45-degree cameras, the
multi-image limit, that a multi-view Generate keeps the same price as the empty-workspace hover, whether the GLB download
is a ZIP, and that the exported GLB faces -Y like Tripo's (`reduce_model.py` applies no rotation, yaw 0).

First supervised live run (a job already in `processing/` or `multiview_ready/`; keep Firefox's Hyper3D window closed):

```bash
python3 tools/tripo_pipeline/convert_job.py <set>--<job-id> --backend hyper3d --dry-run   # form + screenshot, no click
python3 tools/tripo_pipeline/convert_job.py <set>--<job-id> --backend hyper3d             # free preview only; collect then stops
# inspect review_required.png / the model URL; before_geometry.png shows the confirm cost read from the page
./tools/hyper3d_pipeline/rodin.sh collect models/<set>/<job>/<race>/<module> --allow-paid --accept-preview \
    [--expect-cost geometry=0.5]
python3 tools/tripo_pipeline/convert_job.py <set>--<job-id> --backend hyper3d             # reduce the exported GLB
```

The official Rodin API (Business plan, API key) and the Blender MCP `generate_hyper3d_model_via_images` tool are a
possible cleaner route but were not evaluated or called.

## Items-project runner (original documentation)


## Google sign-in: use normal Firefox

Google may reject sign-in in an automation-controlled browser. Use this workflow for an account that signs in with Google:

```bash
./native.sh login
# Sign in manually to Hyper3D using Google, then close that Firefox window.
./native.sh inspect
./native.sh prepare <image-dir> --slots 10
```

`native.sh login` launches the installed Firefox normally, without Playwright, Selenium or remote-control flags. It uses its own profile at `/home/niz/snap/firefox/common/hyper3d-workflow-profile`, separate from both your main Firefox profile and the previous Playwright profile. Once you close it, `inspect` and `prepare` use Selenium/geckodriver with that same profile. These commands only read Hyper3D page state; they do not upload images or submit conversions. Google sign-in and session reuse were confirmed in the supervised sheet-01 trial. No browser-security or automation-detection settings are disabled.

The native monitor was tested with ten tabs in a disposable profile. The launcher also detects the local X0 display if `DISPLAY` is missing. Only one process can use a profile at a time.

Code: `native_browser.py`. Requirements include Selenium and Playwright; the system Firefox and `/snap/bin/geckodriver` are used for this route.

## Original Playwright route

This is a reusable Firefox tab preparer and monitor for [Hyper3D Rodin](https://hyper3d.ai/workspace/rodin). It uses a separate Playwright profile at `/home/niz/.cache/codex-firefox-playwright/profile`. It does not read your main Firefox profile or credentials.

The current script navigates and reads page state. It does not upload an image, press Generate, call a generation API, or spend credits. The site is currently signed out in this separate profile. A later authenticated run can use the same profile after you sign in manually.

## Current commands

```bash
./run.sh inspect
./run.sh login
./run.sh prepare /path/to/individual-images --slots 10
```

`login` opens a visible Firefox window. Sign in there and close it when finished. `prepare` opens ten Rodin tabs, assigns the first ten image paths to slots, and writes a read-only snapshot every 15 seconds. The images are **queued by path only**; they are not uploaded. Press Ctrl+C to stop monitoring. Use `--offset 10` for the next ten images, or `--headless --cycles 1` for a one-pass check. The script accepts PNG, JPG, JPEG, and WebP files or a directory containing them.

If your terminal does not set `DISPLAY`, `run.sh` automatically uses `:0` when the local X0 display socket exists. You can also specify it explicitly: `DISPLAY=:0 ./run.sh login`. The window opens on this machine's desktop.

The packed practical references are in `../practical_item_sheets_packed/`. Their updated `cell_indices.csv` maps all 352 items to 21 full sheets: one 4-column by 8-row ammunition sheet and twenty 4-by-4 sheets. Individual cropped inputs are in `<image-dir>/`; for a read-only ten-tab check use `./run.sh prepare <image-dir> --slots 10`. This does not submit conversions.

Run logs live under ``tools/hyper3d_pipeline/runs/native/runs/<timestamp>/` (native route) or `/home/niz/.cache/codex-firefox-playwright/runs/<timestamp>/` (Playwright route)`. `run.json` contains the current state and assigned image paths; `history.jsonl` records each monitoring cycle. If the gallery gives you a token such as `arrows/02_crafted:R3C2`, that token identifies a source sheet and cell. Inputs can be whole packed sheets or individual crops. The approved trial used the entire first packed sheet (32 arrows and bolts).

## Reinstalling on this machine

```bash
uv venv --python /usr/bin/python3 /home/niz/.cache/codex-firefox-playwright/venv
uv pip install --python /home/niz/.cache/codex-firefox-playwright/venv/bin/python -r requirements.txt
/home/niz/.cache/codex-firefox-playwright/venv/bin/python -m playwright install firefox
```

Playwright uses its own patched Firefox build. The system Firefox and `/snap/bin/geckodriver` are also present, so Selenium is an alternate route. Hyper3D offers an [official asynchronous API](https://docs.hyper3d.ai/en) for future job submission and status polling; that route would need an API key.

## Image to FBX ZIP with recovery metadata

`workflow.sh` is the conversion runner; `native.sh prepare` and `run.sh prepare`
remain read-only monitors. The runner uses **one visible tab** and the dedicated
native Firefox profile. Close the existing workflow Firefox window before starting
another process with that profile.

```bash
# Create a job, upload, generate a free preview, and save it for visual review:
./workflow.sh run <image>.png

# Resume the job printed by the command, after reviewing its preview:
./workflow.sh resume jobs/<input-sha256>/job.json --accept-preview --allow-paid

# Read-only: reopen the saved model and record its current server stage:
./workflow.sh resume jobs/<input-sha256>/job.json --inspect
```

The conversion sequence is Gen-2.5 / Medium free preview → reviewed geometry →
Smart Low-poly / Quad / High (2 credits) → De-light material generation → material
confirmation → base FBX with shaded/PBR 2K textures → ZIP download and verification.
There is no automatic visual quality judgment: the runner saves a preview screenshot
and stops until `--accept-preview` is supplied. Free redo review is supervised, with
a maximum of three retries; automatic redo is not implemented. The geometry cost
must read `2 Credits` or the runner stops. New selectors/stages fail closed if the
site changes. The single-job runner has a ten-tab companion, `batch.sh`, described below.

Each image is identified by SHA256. `jobs/<sha256>/job.json` stores its original
path/hash, stable model UUID and URL, settings, latest observed stage, submission
intents, and output ZIP path/hash/file list. `events.jsonl` records checkpoints;
screenshots record visible stages. Completed inputs reuse their verified output.
No passwords, cookies, or expiring signed asset URLs are stored in this metadata.

The model UUID is also the Mine gallery card's `data-card-id`; its
`data-card-step` identifies the gallery stage (our finished example is
`TextureRefine`). The stable URL reopens the model modal when signed in:

- Gallery: https://hyper3d.ai/workspace/rodin/tab/mine
- Sheet 01: https://hyper3d.ai/workspace/rodin/e1690fdd-98aa-4581-a015-2a2a58a787e4
- Its export metadata: `exports/sheet_01/metadata.json`

Resume reads the **server's current stage**, so it can wait for an existing job,
continue material processing, or download without repeating paid geometry.
Submission intent is flushed to disk before each click; an uncertain or already
attempted submission is never clicked again automatically. If interrupted before
the new model URL was captured, find the matching image under Mine, open its card,
and bind that URL before resuming:

```bash
./workflow.sh bind jobs/<input-sha256>/job.json https://hyper3d.ai/workspace/rodin/<model-uuid>
./workflow.sh resume jobs/<input-sha256>/job.json --inspect
```

Binding refuses to replace a different saved UUID. An unresolved submission that
did not advance needs inspection, not an automatic restart. For a deliberately new
version of the same image, use a different `--jobs-root` to preserve the old job.
Download retries are safe and do not rerun generation. ZIPs must pass CRC checks
and contain FBX and PNG files. Per-job locking and Firefox profile locking prevent
concurrent use of the same job/profile.

Validation: the supervised original trial completed every UI stage and downloaded
an FBX ZIP. The packaged runner's read-only resume was checked against that live
model, and recovery/state/ZIP tests pass. Sheet 02 subsequently passed the full scripted sequence, including visual review,
material confirmation, and ZIP verification, before the remaining sheets entered
the ten-tab batch.

```bash
/home/niz/.cache/codex-firefox-playwright/venv/bin/python -m unittest discover -s . -p 'test_*.py' -v
```

## Ten-tab batches

```bash
./batch.sh <sheet-images>.png --slots 10 --allow-paid
```

The scheduler keeps at most ten conversion tabs active. It submits free previews,
saves model links, and waits for a supervising reviewer to inspect each job's
`review_required.png`. Accepted previews advance independently through geometry,
De-light materials, confirmation, and FBX ZIP export. Finished tabs close to make
room for queued sheets. Each ZIP is saved beside its input image with the matching basename, e.g.
`practical_item_sheets_packed/sheet_02.png` → `sheet_02.zip`. Metadata is also kept
under `exports/sheet_NN/metadata.json` and the durable job folder. Existing completed jobs are reused.

During an interactive supervised run, `Batch.approve(sheet_name, note)` records
review and `Batch.redo(sheet_name, note)` requests a free redo, capped at three.
The active scheduler also reloads `job.json` each pass so recorded review decisions
survive interruption. It never interprets elapsed time as preview acceptance.

The current supervised batch uses these same functions in the existing visible
Firefox session. The `batch.sh` launcher is for later runs, after that window closes.
Recovery information remains in each `jobs/<sha256>/job.json`; there is no dependence
on keeping a particular tab index or browser session alive.

To record a reviewed preview while `batch.sh` runs in another terminal:

```bash
./workflow.sh review jobs/<input-sha256>/job.json --accept --note "All 16 separate objects visible; no obvious broken geometry"
```

Head sheets are discovered recursively under `../heads/`. The supervised launcher
is `DISPLAY=:0 /home/niz/.cache/codex-firefox-playwright/venv/bin/python -i heads_session.py`;
it opens the session without submitting until `runner.fill()` is called. Run
`runner.tick()` to advance reviewed jobs, then `runner.fill()` to occupy freed slots.
Use full input paths for `approve` and `redo` when categories share filenames.
New batch export metadata uses `exports/<stem>/<input-hash-prefix>/metadata.json`
to avoid collisions; ZIPs retain their matching basename beside each input.
`heads/conversion_catalog.csv` records the head batch's model links and outcomes.
