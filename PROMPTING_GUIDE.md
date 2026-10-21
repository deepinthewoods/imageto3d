# Papercraft outfit prompting guide

## Current sequence

Generate **front views only** for a requested set in `concepts/<set>/<race_gender>/<module>/front.png`. For armor, make fronts for both `body_armor` and `right_gauntlet_boot`; for regular clothing, make one `outfit` front. Include `prompts.json` and a short design record with primary/secondary color regions. Do not pause midway through an armor pair.

The user moves a chosen complete set into `approved/`. On a later chat request to process approved outfits, claim each set with `tools/tripo_pipeline/workflow.py`, then generate the other three views. Use the selected front unchanged as the design reference. Place a complete verified four-view set in `multiview_ready/`; the Tripo Studio submit/collect script can convert it manually; the unattended queue watcher is not yet connected. See `tools/tripo_pipeline/README.md` for states and commands. The retired Pixal3D watcher remains disabled.

Use current `base_views/` images, not old experiment copies. The active human male base is the studio-plain semantic marker version: red body, yellow head, magenta anatomical-left hand, cyan anatomical-left foot. Preserve these uncovered colors exactly in every view. Green armor/clothing is `primary`, blue is `secondary`; these stay distinct from the base markers for texture classification and body removal. The old kraft-paper bases are in `archive/base_views_kraft_20261021/`. Other races require matching marker bases before concept generation. Save concept variants under new set/version names rather than overwriting an earlier front.

## Style and color contract

Edit directly on the matching base image, preserving pose, proportions, camera, framing and transparent background. Draw simple matte papercraft with broad planes, crisp edges and folds. Use large, clean **primary** and **secondary** color regions on outfit/armor, with distinct value or hue so they can be classified later. Keep surface color flat enough that the low-poly texture can be averaged or clustered into per-face material regions. Preserve bare base surfaces. Avoid gradients, fine noise, reflective/PBR effects, tiny motifs, realistic cloth texture, text and watermarks unless the user specifically asks for them.

The concept colors are provisional material IDs. Record the marker and primary/secondary swatches in `prompts.json`; actual engine recoloring happens later. For the active human male use base body red `#E61923`, head yellow `#F5DC14`, anatomical-left hand magenta `#E119DC`, anatomical-left foot cyan `#14DCEB`, armor primary green `#19D228`, and armor secondary blue `#1E41EB` as intended source colors. Do not promise automatic semantic classification when a generated texture blends colors or hides a boundary. Flag mixed/uncertain faces for review.

## Front-view prompt pattern

Use case: identity-preserving edit. Image 1 is `base_views/<race_gender>/front.png`. Dress this exact base in [module] from [design]. Preserve anatomy, T-pose, camera, framing, uncovered marker colors and alpha. Give covered parts simple green-primary/blue-secondary matte cardstock regions with large crisp boundaries. Generate a native 1254 × 1254 RGBA image for the current human-male base (or the exact native dimensions of Image 1 for another base); verify the actual saved PNG. Never use resizing, crop, padding or stretching to force a match. If the tool returns a different size, keep it only as a rejected experiment and regenerate. Avoid extra garments, headwear, props, labels and photorealistic detail.

### Armor pair

- `right_gauntlet_boot`: exactly one gauntlet on anatomical RIGHT hand/forearm, and one boot on RIGHT foot/lower leg ending below knee. In front view these are viewer-left. No armor on the opposite side, torso, upper arms or thighs.
- `body_armor`: torso and back, shoulders/upper arms ending above elbows, waist/hips and both upper legs ending above knees. Keep forearms, hands, lower legs, feet, neck and head bare.

Both PNGs show the full base wearing only their own module. They must read as one color/material set. Do not combine them into one image.

### Regular outfit

One `outfit` image shows the whole garment on the full base. Clothing may cover torso, arms and legs and may include right footwear. **Head, both hands and anatomical LEFT foot stay bare.** No gloves or hat; hoods/headwear await a later design decision. Keep these bare anchor areas visibly connected to the unchanged base.

## Generate remaining views after approval

Image 1 is the matching base view and edit target. Image 2 is the user-approved front design. For rear three-quarter, use the established back as an additional design reference. Generate `front_three_quarter.png`, `back.png`, `back_three_quarter.png` with the same parts, colors, folds and coverage. Infer hidden details conservatively. Do not redesign between views; keep anatomical right/left through rotation and natural occlusion.

Do not ask for a second approval between front selection and remaining views. The user chose conversion automatically when all four views pass input checks. Finish all modules and views before staging the set as ready.

## Save and inspect

- Match each corresponding base image's exact dimensions, retain RGBA alpha, and keep the character uncropped. The active human male bases are native 1254 × 1254 renders. Match the active base at generation time and check future bases individually. The older approved armor set is 1024 × 1024 and has a frozen matching base snapshot inside its job folder; do not resize or mix it with new 1254 concepts.
- Do not resize, crop, pad or stretch either the rendered base or an imagegen output. Reject and regenerate a view if its native pixel dimensions differ from its matching base. Check transparency and reject colored alpha-edge halos or stray pixels before staging.
- Confirm pose, viewing direction, silhouette, anatomical side, bare anchor areas and module boundaries. Right limbs are viewer-left from front and viewer-right from back on current human male views; inspect each base rather than assuming this for every character.
- Check primary/secondary color continuity across views and avoid shading that turns one region into many apparent materials.
- Record exact prompts, references, image dimensions/hashes, swatches and remaining differences. Concept views need not be pixel-registered overlays or exact geometric turnarounds.
- To fix a failed view, name the specific failure while repeating pose, camera, side and color invariants. Keep prior versions outside the ready set for recovery.
