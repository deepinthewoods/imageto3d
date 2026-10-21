# Papercraft outfit concepts and Tripo Studio workflow

## Current pipeline

- Use Tripo Studio with the **studio plain paper** source-image look. The chosen reference export and reduction experiments are recorded in `tools/tripo_pipeline/pipeline_decisions.json` and `experiments/tripo_method_gallery_01/`.
- The old `pixal3d-armor-queue` service was disabled on 21 October 2026. Do not restart it or use its old `approved/` contract for new work. Its code and past outputs remain under `tools/pixal3d_queue/` and `models/` for recovery.
- See `tools/tripo_pipeline/README.md` for the folder contract, processing commands and current Tripo website automation. Firefox Playwright submit/collect works manually; the unattended queue watcher is not yet connected. Never claim a conversion ran or move a job to `completed/` without its Tripo output and low-poly/material artifacts.
- New front concepts belong under `concepts/<set_id>/<race_gender>/<module>/front.png`. Generate fronts for the entire requested set in one pass. A set is either both `body_armor` and `right_gauntlet_boot`, or one `outfit`, per character.
- The user selects a complete front set by moving it to `approved/`. Do not move a set into `approved/` without explicit approval. Process `approved/` only when the user asks in chat.
- For each approved set, preserve the chosen `front.png`, generate `front_three_quarter.png`, `back.png`, and `back_three_quarter.png` from the same design and matching base views, then place the complete four-view set in `multiview_ready/`. The user chose automatic Tripo conversion once views are complete; do not add a second image-review gate. Until the queue watcher is connected, `multiview_ready/` is a ready queue and conversion remains pending.
- The planned queue worker will call `studio.mjs` with **Triangle** topology by default, H3.0, 2K texture, PBR off, Remove Lighting on, and a 2,000,000-face Studio *ceiling*. It will export the high-poly model under `models/` and run three automatic low-poly options (geometry error 0.4%, 0.2%, 0.1% of normalized height). The 0.4% result is the default candidate; retain the other two as fallbacks. Each output must pass mesh/part checks or be marked for review. Low-poly polygon counts are outcomes, never fixed targets.
- After reduction, sample/average the source texture across the low-poly faces and group colors into simple material regions for later engine recoloring. Keep base, primary, secondary and uncertain regions distinguishable; do not force uncertain faces into a semantic material. Retain source texture and color evidence. Material assignment follows whole-geometry reduction. When swatches identify the bare base, remove those faces **after** assignment to export a separate wearable mesh; keep the colored full model as evidence. Review cut boundaries and mixed faces.
- Move all view images with prompts and provenance records to `completed/` only after successful conversion, low-poly variants and material output. On failure, keep a job recoverable in its current stage or `failed/`; never overwrite prior attempts or frozen input snapshots.

## Concepts and views

- For new raster references use the local imagegen skill and built-in image generation tool. No CLI/API fallback without user authorization. Read `PROMPTING_GUIDE.md` before generation.
- Use `base_views/<race>_<gender>/<view>.png` as the edit target for each view. The active human-male base is a native **1254 × 1254** render; the former 1024 set is archived at `archive/base_views_studio_plain_markers_1024_20261021/human_male/`. The existing staged armor job remains 1024 and validates against its frozen `.base_views/` snapshot. A 2048 experiment remains separate. Never overwrite base images. Preserve proportions, faceted anatomy, T-pose, camera angle, framing, alpha and uncovered base material/color. The active human male views are studio-plain **semantic marker** views: red body, yellow head, magenta anatomical-left hand, cyan anatomical-left foot. Keep those uncovered colors intact. The old kraft-paper base views are archived at `archive/base_views_kraft_20261021/`; other races need marker-colored base views before new concepts.
- For the active marker base, use broad green `primary` and blue `secondary` garment/armor regions, distinct from every base marker. The red base is not a final character color; it identifies body surfaces for later removal.
- Style: simple papercraft, broad polygon planes, crisp folds, matte cardstock, and large readable primary/secondary color regions. Avoid smooth anatomy, photoreal metal, fine noise and excessive ornament. Concept colors encode later material regions, not final engine colors.
- No weapons, props, hats, capes, labels or unrelated garments unless requested. Headwear/hood handling is deferred; keep the head bare for now.

### Armor set

1. `right_gauntlet_boot`: exactly one gauntlet on anatomical RIGHT hand/forearm and one boot on RIGHT foot/lower leg. Leave torso, upper arms, thighs and the opposite side as the base.
2. `body_armor`: torso including back, shoulders/upper arms and both upper legs. Stop above elbows and knees. Leave forearms, hands, lower legs, feet, neck and head as the base.

Right means anatomical right, never a fixed picture side: viewer-left from front, viewer-right from back. Follow that limb through three-quarter views. Both outputs show the full base wearing only their own module, not floating parts or the combined outfit.

### Regular outfit

- One `outfit` module covers the clothing as a whole, including the right foot if the design has footwear. Keep the **head, both hands and anatomical LEFT foot bare** for alignment and future separation. Do not add gloves or headwear. The bare left foot is viewer-right in front and viewer-left in back.
- Keep the covered/uncovered boundary consistent across views. Do not split a regular outfit into armor modules.

## Output and review

- Delivered images must match the pixel width and height of their corresponding base and retain alpha. The current human male bases are 1254 × 1254; inspect each future base rather than assuming that size. **Do not resize, crop, pad or stretch generated images or rendered bases to make dimensions match.** Ask imagegen for the native base dimensions, inspect the actual saved pixels, and reject/retry any mismatch.
- Use `front.png`, `front_three_quarter.png`, `back.png`, `back_three_quarter.png`. Do not crop/pad views or infer camera matrices as an approval condition.
- Record exact prompts, reference paths, design/color details and review notes; retain earlier versions when revising a concept. Do not edit frozen models or active `processing/` inputs.
- Review all views for module boundaries, anatomical side, pose/camera drift, silhouette, dimensions, alpha and panel/color continuity. These are concept references, not guaranteed pixel-registered overlays.
- Use `experiments/` only for explicitly requested experiments. Past `pending/`, `completed/` and model records are historical and must not be cleaned up for the new workflow.
