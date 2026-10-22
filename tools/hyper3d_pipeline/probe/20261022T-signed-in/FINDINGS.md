# Hyper3D Rodin live probe, signed in (2026-10-22, read-only)

Tools: `probe.py` (page dump), `explore.py` (JSON-scripted read-only clicks/hovers) and the web client's public
JavaScript bundle (`/static/js/main.*.js`, fetched with curl and searched for UI strings and price literals).
Nothing was uploaded; Generate / Free Preview / Confirm / Download / Unlock / Fork were never clicked.
Account: Business plan, balance 1310.5 credits (unchanged at the end). Screenshots and per-step dumps are in the
gitignored `x*/` folders and `*.png` here.

## Session
- Signed in = no `Login` link, no `NO_CURRENT_USER` toast, no Google One Tap, an account menu with `Logout`, header
  `Business` + wallet number. The badge and balance load ~10 s late (first `7-Day Trial`/`Subscribe` and `0`), so the
  backend polls up to 30 s. (A stale `lock` symlink in the profile after a Selenium quit is harmless: `check_profile`
  only refuses when that PID is a running Firefox on this profile.)

## Multi-view input (live)
- There is no separate mode switch: the hover panel's "Single image / Multi-view" tabs are an illustration of
  multi-image input ("Upload photos from any angle"; tips "Rodin's Multiview Image to 3D is pose-free", "RGBA image?
  Click to use alpha channel!", "Specify image directions via the direction bar").
- File inputs: `.jpg,.jpeg,.png,.webp` with `multiple` (the multi-image one) and a single-image one. Multi-image
  fuse needs a subscription (Business OK). The maximum image count is not shown anywhere in the page or bundle
  strings; four views were assumed.
- Direction picker (`#oripopup`, "Select the direction of this image"): Front Left, Front, Front Right, Left, UP,
  Right, Back Left, Back, Back Right, Down, Unknown. Not exercised (needs an upload).

## Generation settings (live)
- Model arrows: Gen-2.5 (Agent-Ready, default) and Gen-1.5 (V1.2 / RLHF). Gen-1.5 has no effort control.
- Effort (Gen-2.5): Extreme-Low ~4s, Low ~9s, Medium ~20s, High ~40s, Extreme-High ~80s.
- Creation Mode: Build Myself (Manual) / AI Assist / Full AI; Presets dialog (Geometry: Micro, Creative, Symmetric,
  Detailed, Soft; prompt caption override). Agentic Mode toggle "1 Credit" (not used).
- Mesh mode / face count are **not** before generation: they are at the geometry **Confirm** step (see code facts).

## Credit costs
Generate button hover, read on an empty workspace for every Gen-2.5 effort (live):

| Effort | Generate shows |
| --- | --- |
| Extreme-Low, Low, Medium | Free Preview |
| High | 0.5 Credits |
| Extreme-High | 1 Credits |
| Gen-1.5 | Free Preview |

From the client code (string/logic literals, not seen on screen):

| Step | Cost |
| --- | --- |
| Geometry Confirm, Quad or Triangle(Raw) mode, any polycount | 0.5 credits (hover label) |
| Geometry Confirm, Smart Low-poly | 2 credits |
| Material Generate / Confirm at effort High or lower | no price shown (free) |
| Material Generate / Redo at Extreme High (12K) | 2 credits |
| Fork (revert to branch) | 0.5 credits |
| Download on Business | free (Gen-2.5 download needs a subscription; 4K / LOD / High-poly show "Unlock with Business Subscription") |

Charged at: Generate (High, Extreme-High only), Geometry Confirm, material only at Extreme High, never at export.
Nothing below 0.5 credits gives a downloadable model: Pack is locked until geometry and material are confirmed
("Unlock after Geometry/Material confirmed"). The cheapest path is **Gen-2.5 at Extreme-Low/Low/Medium (free preview) +
Triangle confirm = 0.5 credits total**. Medium is the best quality at that price. High would cost 0.5 at generate + 0.5 confirm.
Redos are free ("Redo x15"), but confirming costs.

## Geometry confirm (code; popover not seen: no model at that stage existed)
UI modes: Quad Mesh (Smooth / Baked Normal / Forced asymmetry, polygon cards 4k/8k/18k/50k), **Triangle**
("Detailed sharp triangular mesh", Simplify option) and Smart Low-poly (Type, Density). The client sends
`mesh_export_type: "Raw"` for Triangle, with `quality_override` (polygon count) used only when Simplify is on, so Triangle
without Simplify is the unreduced high-poly mesh. A "Low-Poly Model?" warning appears when the face count is very low.
Confirm may ask whether the object is symmetrical (our right-only gauntlet/boot sets are not).

## Material / export (live, on existing finished models)
- Material panel: mode Native, effort High default, toggles `+ 8K`, `+ De-light` (both `button[aria-pressed]`,
  off by default), PBR Temperature slider, Redo (x15) and Confirm (no price on Confirm).
- A confirmed model reads stage `unknown` to the old runner (no De-light text); the backend now derives `export_ready`.
- Pack panel: Geometry checkboxes Base Model (on), LOD, High-poly (disabled, "Unlock with Business Subscription");
  File format radios `.obj .fbx .glb .usdz .stl`; Material checkboxes Shaded and PBR (both on by default);
  Material resolution radios 2K (default) / 4K; Download is a `div[role=button]`. The old manual export folder
  has `base_basic_shaded.glb` and `base_basic_pbr.glb`, i.e. both ticked boxes export two GLBs; the backend ticks only PBR.

## Still unverified
Geometry-confirm popover itself (selectors for Triangle, Simplify; whether the 0.5 label appears), symmetry prompt,
thumbnail/direction mechanics and the right direction for our 45-degree cameras, whether a multi-view generate costs
differently from the empty-workspace hover, that the downloaded GLB faces -Y, and the Gen-1.5 confirm price.

## API / MCP route (note only)
Business plans include API access; the session also has the Blender MCP `generate_hyper3d_model_via_images` tool. Not used.

## Update: first live free run (robe test, model 2c6f6d35-ed8c-45f9-a36c-5e47c3fb3dc5)
- **Multi-view is a real mode switch.** Hover the upload card, click its `Multi-view` tab (`.generation-tabs` style
  `--active: 1`), then upload all four PNGs through the multi-file input: four thumbnails in view order, one model
  (balance unchanged at 1310.5 after Free Preview). The backend now clicks the tab and verifies it before uploading.
- **Directions:** the `#oripopup` picker never opened on hover/click of a homepage thumbnail, so no direction was set at
  upload (recorded as `upload_info.directions_assigned: false`; Rodin multi-view is pose-free). After the preview the
  Geometry card shows a `Direction` bar with the four images (swipe/drag per image, plus Redo to apply).
- **Preview:** reached `geometry_preview` in about two minutes at Medium; the viewer is draggable (screenshots
  `preview_angle_*.png` in the experiment folder).
- **Confirm button:** hover text `0.5 Credits` (default and after choosing Triangle). Redo shows `x50`; `Bang to Parts` is an extra button.
- **Confirm popover (opens on hovering Confirm), three cards:** *Smart Low-poly* (BETA, 5~10 min; Type Quad | Tri; Density Low | High),
  *Triangle* ("Detailed sharp triangular mesh", polygon slider with 5 ticks, thumb at the maximum `1M`), *Quad Mesh*
  (Baked Normal checkbox; 4000 / 8000 / 18000 (default) / 50000). No separate "Simplify" control is visible.
- **Geometry card row:** `T/A Pose`, `< Creative >`, `< Symmetric ≡ >` (a selector, symmetry is a setting, not a prompt),
  prompt box auto-captioned ("Low-poly faceless fantasy wizard in colorful robe.").

## Update: paid collect of the robe test (model 2c6f6d35-...), total 0.5 credits
- Balance 1310.5 -> 1310.0 after Triangle Confirm (0.5, label matched); material Generate and Confirm showed no price
  and cost nothing; export free. Triangle slider read `1M` (maximum) before confirming; Symmetric selector left at `Symmetric`;
  no Redo; material panel read `Material | Native | High | 8K` with 8K off; De-light was switched on.
- Export: `.glb`, Base Model only, PBR only (Shaded unticked), 2K -> `base_basic_pbr.glb`, 42,141,952 bytes: one mesh, one
  primitive, UVs, 1,000,000 triangles (the Triangle cap), 3 images (diffuse 2048x2048, normal, metal/rough), base colour linked.
  Raw height ~1.9 units (Tripo's was 1.0; `reduce_model.py` normalizes). It faces the same way as Tripo's: front = -Y in Blender.
- Gotchas fixed: the `role=checkbox` elements are bare boxes whose label is the parent's text; Firefox writes an empty
  `<name>.glb` placeholder beside `<name>.<rand>.glb.part`, so the downloader now waits for no `.part` and a non-empty stable file.
