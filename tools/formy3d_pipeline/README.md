# formy3d.com backend (Tripo3D H3.1 multi-view)

Third image->3D backend for `convert_job.py` (Tripo Studio stays the default, Hyper3D Rodin is the second):

```bash
python3 tools/tripo_pipeline/convert_job.py <set>--<job-id> --backend formy3d [--dry-run] [--allow-paid]
```

`convert_job.py` calls `formy3d_backend.py submit|collect` with this folder's venv (`config.json: venv_python`, the Playwright venv
shared with Hyper3D; `$FORMY3D_VENV` overrides) in place of `studio.mjs`. The backend writes the same `studio_submission.json`
(`backend: "formy3d"`, `project_id` = formy3d generation uuid, `status` submitted -> exported, `exports.GLB.path`), so
`collect_and_reduce` copies the GLB to `source.glb` and runs `reduce_model.py` unchanged. No free-retry loop (formy3d has none):
a doubled-shell mesh is only flagged `converted_with_errors`. `--accept-preview` is not needed (no preview stage).

## What it does

- Browser: Playwright Firefox, persistent profile `~/.local/share/formy3d-playwright-firefox`, cookies for host `formy3d.com` copied
  from the ordinary Firefox profile each run (`formy3d_cookies.py`, same approach as `tripo_pipeline/import_firefox_cookies.py`;
  private temp copy of the sqlite + wal/shm, values never printed, logged or stored). No Google sign-in flow: sign in once in
  ordinary Firefox. `FORMY3D_FIREFOX_PROFILE` overrides that profile path.
- No pop-ups: headful Firefox on a private Xvfb display from `tools/hidden_display.py` (`PIPELINE_SHOW_WINDOWS=1` shows it). Never headless,
  never minimised.
- `submit <views_dir> <out_dir>`: validates the four views (PNG, 8-bit RGBA, identical size, never resized), checks the session
  (`/api/auth/session`) and balance (`/api/get-user-credits`), opens `/tripo?generationMode=image&modelFamily=tripo`, requires the chip to read
  H3.1, selects **Multi Images**, uploads each view into its slot (`front` -> Front View, `front_three_quarter` -> Left View,
  `back_three_quarter` -> Right View, `back` -> Back View: the `studio.mjs` Front/Left/Right/Back order), sets Texture **HD**, Geometry
  Quality **Detailed**, PBR **off**, Quad Mesh **off** and reads every value back, then reads the displayed price.
  `--dry-run` stops there (screenshot `before_submit.png`, record status `dry_run`). A real submit needs `--allow-paid`, a displayed price equal to
  `expected_costs.generate` (167) or `--expect-cost N`, `budget_credits` (200) and a sufficient balance; the record and the `generate` intent are
  flushed before the click, an existing record refuses (no duplicate charge), and the POST `/api/demo/gen-3d` the click sends is inspected first
  (`request_guard`: a body with the wrong model / pbr / quad / geometry_quality is aborted before it leaves the browser). The generation uuid
  and request id are recorded before it returns.
- `collect <out_dir>`: polls `POST /api/get-user-generations` (My 3D Models) for the uuid until `completed` (`FORMY3D_COLLECT_WAIT_MIN`, default 30),
  downloads the `_model.glb` from `result_urls` (download is free; fallback `/api/proxy/glb-remote`), checks it (one mesh, UVs, base-colour image;
  `rodin_backend.inspect_glb`), saves `glb_<uuid>.glb`, records `exports.GLB` and `status: exported`; compares `credits_consumed` with the displayed price.
- `preview <out_dir> [--views DIR]`: read-only (sign-in, balance, chip, form, displayed cost; never a record, never Generate).
  `inspect <out_dir>`: read-only lookup of the recorded generation. `bind <out_dir> <uuid>`: recover a submit that clicked but lost the id.
- Exit code 3 = intentional stop (convert_job prints it and exits 1; re-run resumes).

Settings chosen (all verified live read-only on 2026-10-23, see below): Tripo3D H3.1, Multi Images with all four views, Texture HD, Geometry
Detailed (the highest detail the site offers), PBR off, Quad off (triangle mesh), GLB. The site has no polycount, topology selector, symmetry or
remove-lighting control, so those Tripo Studio settings have no formy3d equivalent; `reduce_model.py` does the reduction as before.

## Live probe findings (2026-10-23, signed in, read-only; screenshots/costs in `probe/20261023T-signed-in/`, PNG/GLB gitignored)

- Sign-in works with the copied cookies; balance 16688 (generator page `span.font-medium.text-green-600`, `/my-credits`, API `left_credits`).
- Model chips: Free, Hunyuan 3D, Meshy, Seed3D, **Tripo3D H3.1** (select with `Tripo3D H3.1` default and `Tripo3D v2.5`).
- Multi Images slots: Front View (required), Back View, Left View, Right View (optional); 128-5000 px per side, any `image/*`.
- Settings: Texture No textures / Standard / HD; Geometry Quality Standard / Detailed; Seed, Texture Seed; PBR (default on); Quad Mesh (off, FBX only); Reset.
- Displayed Generate price (four views, quad off): Standard+Standard 67 (PBR either); Standard+Detailed 134; HD+Standard 67 (PBR on) / 100 (PBR off);
  HD+Detailed 134 (PBR on) / **167 (PBR off, configured)**; No textures + Standard + PBR off 33. Quad adds 17+. Single image default 100.
- Results: `POST /api/get-user-generations` lists `uuid`, `model`, `status`, `result_urls` (`_model.glb`, `_preview.png`), `credits_consumed`;
  a past Tripo H3.1 GLB passes the GLB check (one mesh, 1.4M triangles, UVs, base-colour texture). Download is free (no ledger row); formats GLB/OBJ.
  Assets expire after 90 days. No free retry exists.

## Part separation on formy3d (read-only, probed 2026-10-23; not used by this backend)

`/3d-to-3d` ("3D to 3D AI Generator") has three tools, chips `PART`, `SMART TOPOLOGY`, `RIG CHARACTER` (all badged New):

| Tool | Engine (from the client bundle) | Input | Price | Notes |
| --- | --- | --- | --- | --- |
| **PART** (default tool; "Hunyuan 3D Part") | `fal-ai/hunyuan-3d/v3.1/part` | **FBX only** (`.fbx`, max 100 MB in the client), source = Upload / Current Result / My Models | **150 credits** flat | no tool-specific settings; the Settings accordion shows only the generic Private badge |
| Smart Topology | `fal-ai/hunyuan-3d/v3.1/smart-topology` | GLB or OBJ | 250 | Polygon Type Triangle/Quad, Face Level (Medium default) |
| Rig Character | `fal-ai/meshy/rigging` (+ multi-animation) | GLB humanoid only | 100 | not opened further |

The row menu of a finished model also offers `Send to Smart Topology` and `Rig Character`. The account has one earlier Part job (150 credits, prompt label
`3D Part | b.fbx`) whose assets have expired, so the output format and part structure were not observable: unverified (the label suggests an FBX with the
parts as separate objects). Part needs an FBX input, so a pipeline GLB would have to be exported to FBX from Blender first; none of this was run.
There is no segmentation tool beyond PART on the site. The generator pages "Hunyuan 3D" / "Meshy" / "Seed3D" are plain image/text-to-3D.

## Credit costs and guards

Only Generate spends credits (167 per module at the configured settings, 334 for an armour job's two modules). Cost kinds, the budget and
`--expect-cost` follow the Hyper3D backend (`expected_costs`, `budget_credits` in `config.json`). A price that differs from `expected_costs.generate`
(for example after a site price change) stops with a message before any click.

## Not verified until a supervised paid run

The Generate click (a confirmation dialog, if any), the real `/api/demo/gen-3d` request field names and values (the guard compares `pbr`, `quad`,
`geometry_quality`, `texture_quality`, `model`; the first run records a sanitized `request_summary` in the record), whether client-side compression
drops the PNG alpha, queue/run time, that `credits_consumed` equals the displayed price, that the GLB faces -Y like Tripo Studio's, and what PBR off does
to the base-colour texture (`face_labels` expects the studio-plain look). Offline tests: `python -m unittest discover -s tools/formy3d_pipeline -p 'test_*.py'`
(view slots, cost guard, intent-before-click, no-resubmit, bind recovery, collect/GLB check, cookie filter, convert_job dispatch).

First supervised run (the leather_14 job is staged in `multiview_ready/`):

```bash
python3 tools/tripo_pipeline/convert_job.py leather_14_caravan_outrider_leather--9ba3b6ee4095 --backend formy3d --dry-run   # form only
python3 tools/tripo_pipeline/convert_job.py leather_14_caravan_outrider_leather--9ba3b6ee4095 --backend formy3d --allow-paid # 167 credits per module
```
