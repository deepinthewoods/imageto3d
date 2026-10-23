# Papercraft outfit prompting guide

## Current sequence

Generate **front views only** for a requested batch, one folder per concept: `concepts/<set_id>/<race_gender>/<module>_front.png` with `<module>_prompts.json` and a short design record in the same race folder. For armor, make both `body_armor_front.png` and `right_gauntlet_boot_front.png` side by side; for regular clothing, make `outfit_front.png`. The prompts JSON records the exact prompt, reference, size, sha256, palette, colours, brief, `check_framing.py` result, notes and attempt count. **Do not keep rejected generation attempts**; record only their count. Do not pause midway through an armor pair. The two existing flat batches remain in place; `workflow.py promote concepts/<batch_id> <NN> [--set <set_id>] [--race <race_gender>] [--approve]` remains for those legacy entries only.

The user reviews concepts and approves or rejects them in the local preview site (`python3 tools/preview/serve.py --open`); approval moves the complete set into `approved/`, rejection moves it to restorable `trash/concepts/`. On a later chat request to process approved outfits, claim each set with `tools/tripo_pipeline/workflow.py`, then generate the other three views. Use the selected front unchanged as the design reference. Place a complete verified four-view set in `multiview_ready/`; the Tripo Studio submit/collect script can convert it manually; the unattended queue watcher is not yet connected. See `tools/tripo_pipeline/README.md` for states and commands. The retired Pixal3D watcher remains disabled.

Use current `base_views/` images, not old experiment copies. Every active base (`base_views/<race>/`, `palette.json` = `white_anchor_v2`) is the studio-plain semantic marker version: red body #E61923 and ONE shared white anchor #EDEDED on the head, anatomical-left hand and anatomical-left foot. Preserve these uncovered colors exactly in every view. Garment roles: green `primary`, blue `secondary`, yellow `tertiary`, magenta `accent` (see the colour contract below); they stay distinct from the red body and the white anchors for texture classification and body removal. Legacy note: bases before 2026-10-22 used a yellow head, magenta left hand and cyan left foot with only green/blue garments (`tricolor_v1`); they are archived with their `palette.json` at `archive/base_views_markers_tricolor_20261022/`. A front drawn on one of those must be processed with that archived base (`workflow.py claim` picks it from the concept's anchor colours; `--base-views <dir>` forces one) and never mixed with the white bases. The old kraft-paper bases are in `archive/base_views_kraft_20261021/`. Other races require matching marker bases before concept generation. Save concept variants under new set/version names rather than overwriting an earlier front.

## Style and color contract

Edit directly on the matching base image, preserving pose, proportions, camera, framing and transparent background. Draw simple matte papercraft with broad planes, crisp edges and folds. Use large, clean garment color regions on outfit/armor, one hue per role (below), so they can be classified later. Preserve bare base surfaces. Avoid reflective/PBR effects, fine noise, realistic cloth texture, text and watermarks unless the user specifically asks for them.

### Lighting and colour (tested 2026-10-21, `experiments/colorless_prompt_test_01/`)

- **Generate directly from the base in green/blue; do not design in free colours and recolour.** The free-colour-then-recolour route costs a second generation and gains nothing over a direct prompt.
- **Colours are hue assignments, not flat fills.** Telling Codex "flat green/blue regions" gives pasted-on, unlit garments that do not match the base and will not texture properly in image-to-3D. State instead: garment hues are exactly green (~#19D228) and blue (~#1E41EB), plus yellow (~#F5DC14) and magenta (~#E119DC) when the design uses them; lit with the same light direction, per-facet shading, contact shadows and crease occlusion as the base, so it looks rendered in the same scene; a shadowed green facet is darker green, a lit one lighter green; no outlines, no other garment hues (cream, orange, purple, teal, glow). Classify shaded pixels by hue, not exact RGB.
- **Colour contract (white_anchor_v2).** `primary` green is the dominant garment colour; `secondary` blue the second large colour; `tertiary` yellow for trim, belts and borders; `accent` magenta for small motifs (stars, moons, charms). Use only as many as the design needs (one to four); every colour is a hue assignment with shading. **No white, grey, black or cream on the garment**: the achromatic white is reserved for the bare head, anatomical-left hand and anatomical-left foot (the single shared anchor that the pipeline reads as `base` and removes), so a white patch on a garment would be cut away like skin. Keep the three white anchors bare and unchanged in every view. Gold-yellow trim is `tertiary`, not a separate gold. Rust/orange blends are not allowed (they sit between red body and yellow).
- Leaving colours out entirely gives the richest designs but no usable material split; the base marker colours do survive.
- **Motifs (only when the design has them; plain outfits need none).** Sigils, trim, stars, moons, charms must be real geometry: raised/embossed, layered cut-out cardstock, folded strips or appliqued panels with visible thickness that catch light and cast small shadows. Never painted or drawn decals on a flat surface. Keep them bold enough to read as low-poly shapes; hairline swirls come out as painted lines. Trim bands are raised borders or separate folded strips, not painted stripes.

The concept colors are provisional material IDs. Record the palette (copy of `base_views/<race>/palette.json`) in `prompts.json`; actual engine recoloring happens later. For the active bases use base body red `#E61923`, white anchor `#EDEDED` (head, anatomical-left hand, anatomical-left foot), garment primary green `#19D228`, secondary blue `#1E41EB`, tertiary yellow `#F5DC14`, accent magenta `#E119DC` as intended source colors (legacy `tricolor_v1`: head yellow, left hand magenta, left foot cyan `#14DCEB`, garments green and blue only). Do not promise automatic semantic classification when a generated texture blends colors or hides a boundary. Flag mixed/uncertain faces for review.

## Front-view prompt pattern

**Framing lock first (2026-10-22).** The built-in imagegen tool has no size, fidelity, mask or batch controls; output size is reliably the base size, but first attempts often come back with the figure enlarged 1.08-1.25x (worse for long robes). Begin every view prompt, before the garment description, with the sentence printed by `python3 tools/framing_lock.py <matching base view>` (canvas, figure bounding box/margins and measured head/hand/foot pixel positions; under `white_anchor_v2` the anchors are described by part, e.g. "white head at x=..", "white anatomical-left hand", "white anatomical-left foot", taken from the palette beside the base). Expect some retries anyway; `check_framing.py` stays the gate.

Use case: identity-preserving edit. Image 1 is `base_views/<race_gender>/front.png`. Dress this exact base in [module] from [design]. Preserve anatomy, T-pose, camera, framing, uncovered marker colors and alpha. Give covered parts simple matte cardstock regions in the palette's garment hues (green primary, blue secondary, and where the design needs them yellow tertiary trim and magenta accent motifs; never white/grey) with large crisp boundaries, lit and shaded exactly like the base (hue assignments, not flat fills; see Lighting and colour). Build any motifs as raised/embossed cardstock geometry, never painted decals. Generate a native 1254 × 1254 RGBA image for the current human-male base (or the exact native dimensions of Image 1 for another base); verify the actual saved PNG. Never use resizing, crop, padding or stretching to force a match. If the tool returns a different size, reject it and regenerate; keep only the attempt count. Avoid extra garments, headwear, props, labels and photorealistic detail.

### Armor pair

- `right_gauntlet_boot`: exactly one gauntlet on anatomical RIGHT hand/forearm, and one boot on RIGHT foot/lower leg ending below knee. In front view these are viewer-left. No armor on the opposite side, torso, upper arms or thighs (the reduced wearable mirrors the gauntlet and boot onto the left, so design them mirror-able).
- `body_armor`: torso and back, shoulders/upper arms ending above elbows, waist/hips and both upper legs ending above knees. Keep forearms, hands, lower legs, feet, neck and head bare.

Both PNGs show the full base wearing only their own module. They must read as one color/material set. Do not combine them into one image.

#### Optional: separable pauldrons (only when the user asks for separate parts/pieces)

Default armour concepts do NOT use this. Use it only when the user asks for armour whose shoulder pieces (or other rigid sections) will be split into separate meshes, e.g. for rigid-body weighting of plate mail. Chosen technique (S2 "magenta field + decoration", tested 2026-10-22 in `experiments/pauldron_colour_strategies_20261022/` and `experiments/pauldron_pipeline_20261022/`):

- Each pauldron's main plate surface and its whole outer edge are MAGENTA (`accent`); raised decorative pieces sit on top in yellow/blue/green, inset from the outer edge. Magenta is used nowhere else on the armour (gems/accents elsewhere become yellow). Pauldrons are distinct rigid pieces with a clear boundary (edge, overlap or a narrow blue mail gap) against breastplate, collar and upper arm, mirrored left/right, never joined across the back.
- Rest of the armour: green main plates, blue under-layer/straps/cloth, yellow trim/emblems/gems.
- After reduction, `tools/tripo_pipeline/split_pieces.py <reduced> --style rim` cuts the wearable into `pauldron_l`, `pauldron_r` and `rest` (magenta plus the decoration it encloses).
- Alternative (also tested, clean): the 2+2 split (armour green/blue only, pauldrons magenta + yellow only, `--style roles`).

#### Optional: rigid sections with per-piece base colours (same "separate parts" request; plate mail for rigid weighting)

Adds to the separable-pauldron rule above (decided 2026-10-22 after `experiments/rigid_sections_20261022/` and `experiments/joint_bands_20261022/`: thin cyan joint bands did not survive Tripo as closed rings, whole-piece colours did). Plate armour is rigid shells joined at articulation points; each rigid section gets ONE large main colour, and NEIGHBOURING sections always differ, so every joint lies on a boundary between two large colour regions:

| Section | Main colour | Touches |
|---|---|---|
| CHEST (cuirass: breastplate + backplate) | green | arms, pauldrons, pelvis |
| ARM_L / ARM_R (rerebrace tube around the upper arm, ending above the elbow) | blue | chest, pauldron |
| PAULDRON_L / PAULDRON_R | magenta (S2 field + decoration) | chest, arm |
| PELVIS (fauld lames round the hips + tassets) | blue | chest, thighs |
| THIGH_L / THIGH_R (cuisse plate around the thigh, ending above the knee) | green | pelvis |

- Each section's main colour covers most of it, including its whole outer edge where it meets a neighbour. Decoration (trim, emblems, rivets, gems, straps) uses the other colours, mostly yellow, and is inset from the section's edges (never touching or crossing a joint). Chest and thighs share green and arms and pelvis share blue because those pairs never touch.
- Decoration never uses a NEIGHBOURING section's main colour: no blue (baldrics, straps, patches, stripes) on the green chest or green thighs, no green on the blue arms/pelvis; use yellow (or the section's own colour as raised geometry). Tested 2026-10-22 (`experiments/piece_colours_20261022/`): a blue baldric or blue chest patches split the chest piece, blue stripes on the thighs made a hip cut fall back.
- Proportions: the pauldron ends clearly ABOVE the arm tube; the blue arm section is a real sleeve-length rerebrace (roughly from below the pauldron edge to above the elbow) whose top edge meets the chest/pauldron edge all round, not a short cuff; no long flared spikes on any section.
- Sections meet edge to edge or slightly overlap; no separate mail/under-layer colour at the joints. Cyan is not used (it bleeds into blue). Everything mirrored left/right; no floating spikes or loose bits.
- Split: `convert_job.py` runs segment-first by default for such jobs (`tools/tripo_pipeline/segment_first.py`, see its README section); the per-face fallback is `split_pieces.py <reduced> --rigid regions` (large same-colour regions plus the decoration they enclose, assigned to sections by body part; `snap` fallback per joint). Older tested variants (cyan joint bands `--rigid bands`, cyan rerebraces `--rigid colour`, blue seam `--rigid seam`) remain available but are not recommended.

## Generate remaining views after approval

Image 1 is the matching base view and edit target. Image 2 is the user-approved front design. For rear three-quarter, use the established back as an additional design reference. Generate `front_three_quarter.png`, `back.png`, `back_three_quarter.png` with the same parts, colors, folds and coverage. Infer hidden details conservatively. Do not redesign between views; keep anatomical right/left through rotation and natural occlusion.

**Three-quarter cameras (active since 2026-10-21):** `front_three_quarter` is from the character's front-left and `back_three_quarter` from the character's back-RIGHT, both 45° around. `front_three_quarter` looks DOWN from 45° above (shoulders and plate tops visible); `back_three_quarter` looks UP from 45° below (undersides, inner legs and crotch visible, head small and partly hidden). `front`/`back` are level. In `front_three_quarter` the anatomical-left side is nearest the camera; in `back_three_quarter` the anatomical-right side is nearest. Tell imagegen this explicitly so it matches the camera instead of reverting to a level side view. The low back view exists so Tripo sees the inner legs; keep uncovered inner-leg base surfaces their marker color there.

Do not ask for a second approval between front selection and remaining views. The user chose conversion automatically when all four views pass input checks. Finish all modules and views before staging the set as ready.

## Save and inspect

- Match each corresponding base image's exact dimensions, retain RGBA alpha, and keep the character uncropped. The active human male bases are native 1254 × 1254 renders. Match the active base at generation time and check future bases individually. The older approved armor set is 1024 × 1024 and has a frozen matching base snapshot inside its job folder; do not resize or mix it with new 1254 concepts.
- Do not resize, crop, pad or stretch either the rendered base or an imagegen output. Reject and regenerate a view if its native pixel dimensions differ from its matching base. Check transparency and reject colored alpha-edge halos or stray pixels before staging.
- Confirm pose, viewing direction, silhouette, anatomical side, bare anchor areas and module boundaries. Right limbs are viewer-left from front and viewer-right from back on current human male views (farther from the camera in `front_three_quarter`, nearer in `back_three_quarter`); inspect each base rather than assuming this for every character.
- **Run `tools/check_framing.py <base.png> <image.png...>` on every generated view before looking at it** (see below). Look at an image yourself only for what it cannot judge (design, motifs, module boundaries).
- Check garment color continuity across views (each role keeps its hue and region) and avoid shading that turns one region into many apparent materials.
- Record exact prompts, references, image dimensions/hashes, swatches and remaining differences. Concept views need not be pixel-registered overlays or exact geometric turnarounds.
- To fix a failed view, name the specific failure while repeating pose, camera, side and color invariants. Keep prior versions outside the ready set for recovery.

## Automated framing check

`tools/check_framing.py <base> <image...>` finds the bare marker regions in base and generated image and compares scale and centroid. The palette beside the base (`palette.json`; none = legacy `tricolor_v1`; `--palette` overrides) decides how: under `white_anchor_v2` it finds the achromatic white blobs (saturation < 0.25, value > 0.45, at least ~250 px) in both images, treats the top-most base blob as the head, fits scale and shift on the head and pairs the remaining blobs with their base blobs by position and size (a touching hand+foot blob, as in the A-posed orcs, is paired as a union; white garment highlights and specks match nothing and are ignored), and the red body is found by hue as before. Under `tricolor_v1` it uses the hue windows of the yellow head, magenta left hand, cyan left foot and red body. The head is always bare, so it is the anchor (the top-most white blob under `white_anchor_v2`); hand/foot/body markers are used only while at least half of their base area is still visible, so long robes or spiky armor that cover them are skipped rather than failed. Exit code 1 = flagged. Output labels:

- `FRAMING`: head shifted or global scale off by >4% (figure enlarged/shifted/shrunk). Reject and regenerate. This reliably caught the enlarged figures in the experiment (scale 1.11-1.16).
- `POSE`: a hand/foot marker moved while the head stayed put (e.g. robe views with the left foot ~45 px lower). Inspect; usually regenerate.
- detached alpha islands: >30 px of specks above alpha 0.1. Fringe below alpha 0.1 is ignored.

Limits: it constrains only the bare markers. Drift of a fully covered area, a changed pose under a garment, or a garment silhouette that grew is not detected; a pass is not approval.
