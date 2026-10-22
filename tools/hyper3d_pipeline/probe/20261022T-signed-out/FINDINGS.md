# Hyper3D Rodin live probe, 2026-10-22 (read-only)

Tool: `tools/hyper3d_pipeline/probe.py` (Selenium, native profile `~/snap/firefox/common/hyper3d-workflow-profile`).
It opened https://hyper3d.ai/workspace/rodin and the Mine tab, read the DOM and took screenshots. No upload,
no Generate, no clicks at all.

## Result: the profile is SIGNED OUT

- Header shows **Login** (not an account menu), a red toast `NO_CURRENT_USER` / "Oops, something went wrong", and
  the page stays on `/workspace/rodin` (no redirect to `/workspace/login`, no password field). The old
  `observe()` / `native.sh inspect` would report this as `unknown_or_signed_in`; `rodin_backend.py` checks for the
  `Login` link and `NO_CURRENT_USER` instead.
- Per the task the probe stopped there: no login attempt, no model opened, the Mine tab shows no models.
- Needed from the user: `./native.sh login`, sign in with Google, close that Firefox window, re-run the probe
  with `--model-url <an existing model>` (and `--click-text` for pure option tabs) to complete the items below.

## Confirmed from the signed-out DOM (static page structure)

| Item | Finding |
| --- | --- |
| Model selector | Gen-1.5 V1.2, **Gen-2.5** (Agent-Ready; current default) |
| Effort | Extreme-Low ~4s, Low ~9s, **Medium ~20s** (default), High ~40s, Extreme-High ~80s |
| Creation mode | Build Myself (Manual: Create Manually / Generate Objects), AI Assist, Full AI; Default / Speedy |
| Input modes | **Single image** and **Multi-view** ("Upload photos from any angle") |
| Image inputs | file input `accept=.jpg,.jpeg,.png,.webp`, **`multiple`** (the multi-view one), max count NOT shown |
| Direction labels | "Select the direction of this image": Front Left, Front, Front Right, Left, UP, Right, Back Left, Back, Back Right, Down, Unknown ("Images that have been set with Direction will help improve the accuracy") |
| Free step | Generate button hover says **Free Preview**; tips: "Redos are free, but confirmation costs credits", "Regret confirming? Fork and redo!", "More details on low-poly? Click Bake Normal at confirm!" |
| Plans | 7-Day Trial / Creator 24 per month (30 credits renew) / Business 96 per month (208) / Education / Enterprise; "Business subscriptions include API access" |
| Other | Add On (DCC plugins incl. Blender/Godot), API, MCP&CLI links in the header |

## NOT verifiable while signed out (still unverified)

- The confirm-geometry popover: label of the raw / high-poly option (the old runner only knew Smart Low-poly +
  Quad + High), any face-count / polycount choices, Quad vs Triangle, displayed credit costs per step.
- Material step options (De-light seen as a toggle in the old runner), export dialog: whether GLB exists
  (a past manual high-poly GLB export exists, so likely), texture resolutions, shaded vs PBR choice.
- Max number of multi-view images and how the direction picker is operated per thumbnail.

## API / MCP route (note only, not used)

The header offers Add On, API and MCP&CLI; the page says Business plans include API access (Creator/trial plans may not). The Blender MCP server of this session also has
`generate_hyper3d_model_via_images` (official Rodin API, needs an API key / free-trial key; not called here). From
memory of the public Rodin API docs (not verified in this session) the API exposes `mesh_mode` (Raw/Quad),
`quality_override` (face count), `material` (PBR/Shaded), `geometry_file_format` (glb), multi-image input and a
`condition_mode`; that would be a cleaner backend than browser automation if an API key with enough quota
exists, and would fit the same `studio_submission.json` seam.
