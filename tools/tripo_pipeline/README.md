# Front concepts → four views → Tripo Studio → low-poly materials

This is the current parent-project workflow. The user chose the **studio plain paper** source-image look and local cleanup plus geometry reduction at **0.4% of body height** as the default. Also retain **0.2% and 0.1%** variants so a reviewer can switch when a garment or armor panel needs more detail. None of these settings asks for a polygon count. On the reference character the results were 436, 626 and 1,352 triangles, with about 0.45–0.49 seconds of simplification and about 0.17–0.19 seconds of measurement/export per variant after shared preparation. New assets will have different counts and times.

The selected reference and decision record are at `pipeline_decisions.json`; the eight-scene comparison and metrics are under `../../experiments/tripo_method_gallery_01/`. Studio's original reference model version is unknown. New submissions use confirmed H3.0, 2K texture, PBR off, Remove Lighting on, and **Triangle by default**; `config.json` records those choices.

## Folder states

```text
concepts/<set>/<race_gender>/{body_armor,right_gauntlet_boot}_front.png   (flat, side by side)
concepts/<set>/<race_gender>/outfit_front.png
              │ user moves a complete chosen set
              ▼
approved/<set>/... same flat <module>_front.png layout
              │ chat request: process approved outfits
              ▼
processing_views/<set>--<job-id>/<race_gender>/<module>/ front.png + generated other views
              │ (claim unflattens the fronts; notes/rejects go to <race_gender>/.concept_notes/)
              │ all four views verified per module
              ▼
multiview_ready/<set>--<job-id>/... four views + frozen hashes
              │ Firefox Playwright submit/collect; queue watcher pending
              ▼
processing/<set>--<job-id>/... website conversion in progress
              │ Tripo GLB + 0.4/0.2/0.1% low-poly GLBs + flat material review
              ├── models/<set>/<job-id>/<race_gender>/<module>/...
              └── completed/<set>--<job-id>/... images, prompts, job record
```

An armor set has **both** `body_armor` and `right_gauntlet_boot` fronts for each character. A regular outfit has one `outfit` front. They are separate jobs/set types; do not combine them into a single image. The outfit leaves head, both hands and anatomical left foot bare. Headwear and hoods await a later decision. Use large, matte primary and secondary color regions plus the unchanged exposed base so the generated texture can later be assigned to engine materials.

Concept generation produces fronts in `concepts/`; it does not move anything to `approved/`. The user approves by moving the whole set. The assistant processes approved sets when asked in chat. It preserves each selected front, generates the three other views from corresponding base views, checks all images, and stages the completed set with `workflow.py ready`. The active human-male base views are native 1254 × 1254 studio-plain semantic-marker renders: red body, yellow head, magenta anatomical-left hand, cyan anatomical-left foot. The prior 1024 set is archived at `archive/base_views_studio_plain_markers_1024_20261021/human_male/`. The old brown views and other races are archived at `archive/base_views_kraft_20261021/`; activate another race only after producing matching marker views. The user chose conversion to run automatically after the four views exist, without a second review stop. `studio.mjs` now provides reusable Firefox Playwright submission and collection for a four-view module. It is not yet connected to a background queue watcher; `multiview_ready/` jobs remain staged until that connection and its recovery rules are tested. The approved arcane plate mail job has not yet been uploaded.

Past Pixal3D results remain in `models/` and `completed/`. The Pixal3D user service was disabled and stopped before this contract was installed. Do not reactivate it while `approved/` has front-only Tripo selections.

## Validator and staging commands

From the parent project directory:

```bash
python3 tools/tripo_pipeline/workflow.py status
python3 tools/tripo_pipeline/workflow.py check approved/<set>
python3 tools/tripo_pipeline/workflow.py claim <set>
# Generate the three other views for each module in processing_views/<set>--<job-id>/.
python3 tools/tripo_pipeline/workflow.py ready <set>--<job-id>
# Convert: moves the job to processing/, then per module Tripo submit -> collect -> reduce.
python3 tools/tripo_pipeline/convert_job.py <set>--<job-id>            # --dry-run: fill Studio's form only
```

`convert_job.py` runs modules strictly one at a time (Studio greys out Generate while another model of the account is generating; `studio.mjs` waits up to `TRIPO_QUEUE_WAIT_MIN`, default 30). Collection polls until Studio shows the finished model (`TRIPO_COLLECT_WAIT_MIN`, default 30) and retries a blocked export click. It is resumable: a module with a submission record is only collected, a reduced module is skipped. It leaves the job in `processing/` with state `converted` and lists the levels needing review; move it to `completed/` after that review.

The validator requires matching native base and generated-view dimensions, nonempty RGBA with transparency, and either both armor modules or one outfit module. Newly approved sets use active `base_views/<race_gender>/`; claiming freezes all four base views in the job’s hidden `.base_views/<race_gender>/` snapshot. Ready/staged jobs validate against that snapshot, so a later active-base resolution change cannot invalidate an older set. It rejects symlinks and extra/missing PNG views. Claiming records SHA-256 hashes of user-approved fronts. Staging verifies those hashes again and writes hashes for all four views before the folder move. The tool never generates images, invokes Tripo or marks a job completed. Preserve prompt/color records alongside the images. Do not edit an active/frozen job behind the worker's back.

## Conversion and material contract

The planned queue worker will use one Tripo multi-image conversion for each `race_gender/module`, with the tested H3.0, no-PBR, 2K texture and Triangle settings. Export the original high-poly GLB unchanged. Apply the same local-cleanup + geometry-error reduction at 0.4%, 0.2% and 0.1% of normalized body height and keep all three. Audit boundaries, non-manifold edges, winding, duplicate/degenerate faces, significant components, and geometric/silhouette changes. Flag any failed option for manual review rather than silently selecting it. Do not force an alternative algorithm merely because an asset has more detail; first inspect the finer 0.2%/0.1% outputs. Normal-aware reduction remains an experiment because it introduced odd arm geometry on the reference.

Then label each reduced face from the original texture (`face_labels.py`, 2026-10-21):

- **Hue, not chromaticity distance.** Each texture sample takes the nearest swatch hue within 20°. The earlier r:g:b-ratio distance rejected Tripo's body red for being *more* saturated than the swatch (0.131 vs a 0.13 cutoff), which produced the orange patches on bare torsos and most "uncertain" counts. Averaging a face was dropped too: green+blue straddles average to teal and read as cyan.
- **Sample weight** ramps with saturation (0.2→0.45) and brightness (0.08→0.25), so murky pixels count little and grey/black ones nothing.
- **Visibility.** With `--cameras` (the `transforms.json` the concept views were made with) and `--regions`, every high-poly face gets its best unoccluded view angle; texture on unseen surfaces counts 5% (Tripo invented it). Note that the diagonal views do see the spread inner legs at a steep angle, so visibility alone does not fix inner-leg bleed.
- **Module prior** (`--module`): soft base/wear bias from base landmarks. `body_armor`: forearms, lower legs and head lean base. `right_gauntlet_boot`: right forearm and right lower leg lean wear, right thigh stays possible (tall boots), everything else leans base. `outfit`: head, hands and left foot lean base. Clear visible texture can override it.
- **Resolution.** Weighted votes + prior + mirror face (body armour only) + crease-aware neighbours, iterated; each face records its deciding source (`label_source` face attribute and `_colors.npz`). A level is flagged when more than 20% of its area was decided by prior/neighbours.

With the standard `models/<set>/<job-id>/<race>/<module>/source.glb` layout no flags are needed: the module comes from the folder name, swatches from `swatches.json` beside the source or else `config.json`'s marker palette, regions from the job snapshot or `base_views/<race>/regions.json`, and cameras from the job's frozen `.base_views/<race>/transforms.json` (`workflow.py claim` freezes it). For jobs claimed before that, the cameras come from whichever active/archived base set has the same three-quarter view hashes as the job's frozen images. The manifest records each choice under `input_sources`. The repaired source mesh is put into a canonical order before reduction, so runs are reproducible. `base_regions.py` measures `regions.json` for a marker base (`base_views/human_male/regions.json` from `assets/studio_plain_input.blend`). Use semantic roles `base`, `primary`, `secondary` only where the source colors make that assignment reliable; keep `uncertain` visible for review. For the active human-male base, red body, yellow head, magenta left hand and cyan left foot all classify as `base` for removal. Preserve the original texture, per-face evidence, source GLB, each low-poly option and materials in `models/`. The reducer reuses area-weighted texture sampling from `../lowpoly/polygon_materials05.py` and reads the concept's marker and armor swatches.

The reusable three-level Blender processor is now `reduce_model.py`. It was run on the selected studio plain paper GLB and reproduced the 436 / 626 / 1,352 triangle results with mesh checks passing. It preserves an exact imported backup, exports three native-coordinate full-model GLBs with flat face materials, stores sampled face-color evidence, and saves an editable `review.blend`. With recorded swatches, it also removes all `base` marker faces **after** classification and exports a `__wearable.glb` at each level; these intentionally open garment surfaces need fitting review. Its no-swatch fallback uses neutral `COLOR_00/01/02` labels and cannot safely remove the body. For production, pass the `swatches` object from the concept's `prompts.json`, e.g. `{"base":"#E61923","base_markers":{"head":"#F5DC14","left_hand":"#E119DC","left_foot":"#14DCEB"},"primary":"#19D228","secondary":"#1E41EB"}`. The original three-color swatch form still works for older inputs. Example:

```bash
/usr/local/bin/blender -b -t 6 --python tools/tripo_pipeline/reduce_model.py -- \
  models/<set>/<job-id>/<race_gender>/<module>/source.glb \
  models/<set>/<job-id>/<race_gender>/<module>/reduced \
  [swatches.json] [--cameras transforms.json --regions regions.json --module <module>]
```

Each run also writes `reduced/wearable.blend` (the three `__wearable` levels side by side, base markers removed, with the primary/secondary/uncertain face materials). `make_wearable_blend.py -- <reduced_dir>` rebuilds it from existing wearable GLBs. With swatches it also writes `highpoly.blend` beside the source GLB: the full Tripo mesh twice, `highpoly_detected` (each triangle's material detected from the texture at its UV centre, orange = uncertain) and `highpoly_textured` (original texture, packed); `make_highpoly_blend.py -- <source.glb> <swatches.json>` backfills it and never overwrites. The output directory must be empty to protect earlier attempts. The processor currently expects one textured mesh with direct base-color UV texture; it stops with a clear error for an unfamiliar Tripo export. It records material review status when swatches are absent, more than 10% of faces are uncertain, texture projection is too far from the original surface, or no confidently labeled primary/secondary faces remain. A bare-base sample with the four-marker palette correctly produced no confident wearable faces and was flagged for review. Color-role thresholds, body removal and fit on genuinely new outfit textures still need visual validation before automatic completion. Automatic queue invocation of the browser script and processor is the remaining integration step.

Once conversion and all derived outputs succeed for every module, move the four-view image set and its prompt/job metadata to `completed/`. Keep failed/retryable jobs and partial model attempts distinct; do not overwrite older exports. No automatic queue watcher is currently enabled, so `multiview_ready/` is a safe holding state until it is implemented and verified.

## Studio browser script and quad base test

Install once with `npm ci` in this directory and `npx playwright install firefox`. Sign in through ordinary Firefox, then use `studio.mjs`; the helper copies only Tripo-domain cookies into a separate Playwright Firefox profile. Set `TRIPO_FIREFOX_PROFILE` if the ordinary Firefox profile differs from the default Snap path. Do not commit cookies or the private browser profile.

```bash
# Run from the parent imageto3d directory. Four images are read in Front/Left/Right/Back order.
node tools/tripo_pipeline/studio.mjs submit multiview_ready/<job>/human_male/body_armor models/<set>/<job>/human_male/body_armor
# Once Studio has finished, use the submission output directory recorded by the first command.
node tools/tripo_pipeline/studio.mjs collect models/<set>/<job>/human_male/body_armor
```

Omitting the final topology argument selects **Triangle**. Pass `Quad` explicitly only for an editing experiment. Submission records a task/project ID before returning, refuses an existing record to prevent duplicate charges, verifies the displayed 25-credit Triangle or 30-credit Quad price, and saves a pre-submit screenshot. Collection reopens that project ID, records Studio's face count and topology, and downloads both OBJ and GLB. Repeating collection skips already downloaded formats. Browser layout and controls can change; check the screenshot and record if a step fails. The optional quad ceiling is 50,000 faces and Studio may produce fewer.
The script checks that all four uploaded PNGs have the same native width and height and are 8-bit RGBA; it never resizes inputs. The script sets the Triangle ceiling to 2,000,000 faces for a high-detail source; Studio retains the last ceiling when changing topology, so leaving the 50,000-face quad setting would unintentionally cap later triangle jobs. Use `TRIPO_DRY_RUN=1` with `submit` to check the four previews and controls without pressing Generate (use a fresh temporary output directory for each dry run).

The bare human-male Quad trial is at `models/base_quad_comparison_20261021/`. Studio generated 44,388 faces at H3.0/2K/no-PBR for 30 credits. Blender imported the OBJ with 44,311 quads, 46 triangles and 31 pentagons; the GLB triangulated it to 88,761 triangles. The previous plain-paper triangle GLB has 660,574 triangles. The side-by-side `quad_vs_triangle.blend` and front/three-quarter renders show a similar silhouette; the quad surface is rounder across chest and legs and retains joint gaps. The source images differ in color treatment, so this is a practical comparison, not a controlled topology-only A/B test. Both source meshes have roughly 19,000 boundary edges after import, so neither is a watertight replacement for joint-aware fitting. The quad GLB processed through the adaptive reducer into 468/730/2,010 triangles at 0.4/0.2/0.1%; the derived GLBs are triangles, and the bare-base wearable extraction is intentionally marked for material review.

## Resolution transition

The active human-male base is now natively rendered at **1254 × 1254**, in `base_views/human_male/`, with 45° three-quarter cameras since 2026-10-21 (front-left looking down 45°; back-right looking up from 45° below, so the inner legs are seen); `experiments/cam45_elev45_20261021/` holds the render script, the plate-mail trial that motivated the switch, and its comparison against the previous level cameras (archived at `archive/base_views_studio_plain_markers_1254_level_20261021/human_male/`). `tools/tripo_pipeline/render_base_1254.py` rendered the earlier level set. The separate 2048 experiment remains inactive. The earlier `arcane_plate_mail_v2--c7f70712717e` views remain 1024 × 1024 and validate against their frozen 1024 `.base_views/` snapshot. For all new concepts and views, request the base’s exact native dimensions and reject a different output size or colored alpha fringe. Never resize, crop, pad or stretch either generated images or rendered bases to force a match.
