# Pipeline v2: the preview app runs the whole pipeline (plan, 2026-11-25)

Status: **implemented in phases 0-6 (2026-11-25); the app is the site once CUTOVER.md is done. The plan below is kept as written** (the user answered
the questions on 2026-11-25, section 13; the as-built contracts are `tools/preview/v2/DESIGN*.md`). Merged from Claude's plan and Codex's independent plan
(`.codex_jobs/plan_pipeline_v2_20261125/plan.md`); section 11 lists what was taken from Codex.

## 1. Goal

One web app (today's preview site) takes every design from approval to "ready for export" with no chat commands:

| Tab | What you do | What the app then does by itself |
|---|---|---|
| **Concepts** | hover: **Provisional** / **Approve** / **Reject** | Approve requests processing at once |
| **Provisional** | **Approve** / **Unapprove** | as above |
| **Approved** | watch progress; **Unapprove** until work starts; fix holds | claim, other-race fronts, 4 views + checks (characters) or 3 extra views (monsters), 3D conversion (Tripo while there are credits, else Hyper3D), LOD reduction |
| **3D** | pick the LOD per race (race + LOD selectors, **All**, < >), **Approve** | characters: build the layers (alignment, seams, Amount morph) at the picked LOD; monsters: go straight to Completed |
| **Aligned** (characters) | race selector + < > through sets; **Amount** slider; armour: **gloves** and **boots** dropdowns with < > to try other sets' pieces; **Approve** | - |
| **Completed** | look; **Unapprove** sends it back | writes the export manifest (export itself stays manual, later) |

Second group: **Mix** (free try-on of any built layers, the prototype's mix page), **Queue** (running / waiting / failed work),
**Review** (lists agents put there), **Archive**, **Trash**. Removed: Models, Approved models, Pipeline, Held, Monsters (monsters
join the main tabs with a **Monster** tag; header checkboxes **Characters** / **Monsters**, plus race, kind and search filters).

## 2. What exists today (from the code maps)

- **Preview app** (`tools/preview/serve.py`, 1405 lines; `app.js`, 223 very long lines): one stdlib HTTP server on :8765 that
  rescans folders per request. It starts no processing work. Moving a set into `approved/` is what requests processing, and the
  separately started supervisor does the claim. The site is shared on the tailnet with no login and no check on where a
  request comes from.
- **Pipeline**: four hand-started Windows processes coordinated through folders and `.codex_jobs/_batch/` files:
  - `pipeline_supervisor.py` (claims, gates, idle pause)
  - `view_batch.py run` (8 Codex jobs)
  - `convert_queue.py` (1 conversion worker)
  - `serve.py` (the preview site itself)

  A set becomes about 3 jobs: the design race, the other races, and the gnome. `finish_sets.py` releases a set only when all
  11 races are converted, so one failed race keeps the set out of the Models tab.
- **Credits**: `studio.mjs balance` gives `(balance - reserve) // 25` Tripo submits, then Hyper3D takes over. An unreadable balance
  is treated as 0. The outfit Hyper3D path still looks for the removed "Triangle" option. The monster wrapper's "Best Quality"
  step is the fix.
- **Layers prototype** (`experiments/shell_slots_20261122/layers.py`):
  - `task_module(module, lod)` takes a median 22 s for gloves/boots, 46 s for an outfit and 54 s for body armour, and up to 6 min.
    `task_bare(race)` builds the bare layers once per race.
  - Inputs are the full labelled `reduced/lod_error_<lv>.glb` + `_colors.npz`, `fit/align.json`, and the game body caches
    `tools/preview/cache/bases/<race>.npz` and `<race>_rig.npz`.
  - It writes `out/layers/<race>/<slot>__<set>.glb`. The names carry no job and no LOD, and the writes are not atomic.
  - The LOD is hard-coded (0p4 plus one override). **Your 133 existing LOD picks (`chosen_lod.json`, 124 at 0.2) are ignored.**
  - About half of `shell_slots.py` / `geodiv.py`, and several whole files, are the dead "shell" method.
- **Monsters** (`characters/monsters/aaa`): driven by hand.
  1. A concept is picked and copied to `pipeline/<Monster>/views/<n>/`.
  2. Hyper3D converts it from one image (front) or two (front + back).
  3. `reduce_monster.py` makes 5 LODs (0.8–0.1) with the monster palette.
  4. The LOD and approval are saved in `chosen_lod.json` from the Monsters tab.
  5. Rigging and `build_monster_blends.py sync` are run by hand.

  The tab only ever shows `front.png`. Tripo is not used for monsters. 56 variants are converted (34 War Horse) and 17 approved.

## 3. What a card is

- **Character card = a set.** The required races are fixed when the set is approved: every race with a `palette.json`,
  currently 11. Modules: an outfit set has `outfit`; an armour set has `body_armor` + `right_gauntlet_boot` (gloves only for gnomes).
- **Monster card = one variant** (each War Horse armour type is its own card, never overlaid).
- A set moves to **3D** when every required race/module is reduced, or explicitly dropped (see Redo below). One approval covers the
  whole set. Inside the modal you still look at and choose per race.
- **The source job is pinned** for each race/module at 3D approval. A later or duplicate conversion never silently replaces
  what you approved. The prototype picked "the newest align.json".

## 4. Where state lives

- **Decisions = small JSON files** written only by the server (atomic, under a lock), readable by tools and agents:
  - `models/<set>/<job>/<race>/<module>/chosen_lod.json`: kept as is, since the outfit exporter and the monster library already read
    it.
  - `models/<set>/pipeline_v2.json`: required units, pinned jobs, the 3D approval snapshot, Amount per race (outfit: one value;
    armour: body armour and gloves/boots separately), the Aligned approval snapshot (build ids), and history.
  - Monsters: `chosen_lod.json` (existing), plus `views.json` (`[{name, file, enabled}]`) and a stage record per variant.
- **Work queue = one SQLite file** (`.codex_jobs/_app/queue.sqlite3`): tasks, attempts, leases, child PIDs, receipts, events. It is
  operational only: if it is lost, the decisions survive and the queue is rebuilt from the files.
- **Folders the existing tools need stay**: `concepts/`, `provisional/`, `approved/`, `processing_views/`, `multiview_ready/`,
  `processing/`, and `completed/`. `completed/` keeps meaning "conversion finished"; the **Completed tab reads approvals, not that
  folder**. Monster files never move.
- **Completed** = the Aligned approval exists and its build ids are still the published ones. An approved build is never
  overwritten. A rebuild makes a new build id and sends the set back to Aligned with its Amount kept. The Completed tab writes
  `models/<set>/ready.json` (LOD, pinned job, build id, slot files and hashes, `amount_pct`) for the future export.

## 5. The server runs the work

- `python tools/preview/serve.py` starts the website and the **orchestrator** inside the same process. A single-instance lock
  keeps a second copy from also dispatching work.
- Every job runs as a **separate, detached process** with a PID file:
  - Codex image jobs
  - Tripo or Hyper3D browser runs
  - Blender reduction
  - layer builds

  Restarting or closing the site never kills work, and a restarted server takes its running jobs back, as `view_batch` does today.
- **Lanes and limits**:

  | Lane | Limit |
  |---|---|
  | Codex images | 8 shared slots |
  | Tripo browser (balance, submit, collect) | 1 |
  | Hyper3D browser | 1 |
  | Heavy local geometry (Blender reduction + layer builds, GPU) | starts at 1, raised after measuring memory |

  You can change the limits in the Queue tab.
- **Idle rules** are carried over:
  - Image generation runs even while you are at the PC.
  - New conversions and heavy local work wait until you are away (5 min without input). Work already running is paused and
    resumed, as the supervisor does today.
  - Queue tab buttons: **Pause all**, **Pause conversions**, **Run now even though I'm here**.
  - Layer builds also run while the user is at the PC, one at a time, and in parallel when away (decision 4).
- **Credits**: before each new submission it reads the Tripo balance and uses Tripo while `balance - reserve >= 25`, else Hyper3D
  (after fixing its mesh step). It always collects with the backend that submitted. An unreadable balance or an expired login is
  shown as a named problem with a fix button; it is no longer treated as 0. The header shows the credits. Pixal3D and formy3d stay
  out of the automatic route.
- **Paid work is never repeated by accident**: the submission receipt is written before the click (as `studio_submission.json`
  already is), and a crashed or expired job only ever resumes with "collect".
- **Queue tab**: per job the step, elapsed time, the reason it is waiting (idle, credits, hold), the last error with its log tail,
  and **Retry** for just that unit. It replaces the Pipeline and Held tabs.
- **Handover from the old services** happens in phase 4. While the old supervisor, runner and worker still run, they own their
  folders and the new server only builds layers. At cut-over they are drained and stopped; the two are never active on the same
  folders.

## 6. The layer builder becomes a tool

- Move the code that is still used into `tools/layers/`:
  - `layers.py`
  - the needed parts of `geodiv.py` and `shell_slots.py`
  - `pieces.fill_layer_guard`
  - the check tools

  Use `git mv` so history follows. The experiment keeps a small import shim, so the 8790 prototype keeps working for comparison
  until cut-over. The dead shell code is deleted after the parity check.
- API: `build_bare(race, out)` and `build_module(module, lod, out)`. Each returns a structured result: slots written, timings,
  warnings, wall crossings left, open edges.
  - The LOD is an explicit argument, taken from your 3D choice. `LOD_OVERRIDE` and "0p4 required" go.
  - Each build has a fingerprint: hash of the code + the game body cache + `align.json` + the LOD GLB / npz + the LOD. Nothing is
    rebuilt unless one of those changed, and a stale build is flagged instead of silently rebuilt.
- Output goes to `models/<set>/<job>/<race>/<module>/layers/<build_id>/`. It is written to a temporary folder, checked (every
  required slot present, morph `fill` present), then swapped in at once. A failed build keeps the last good one. A missing glove or
  boot is never reported as success.
- Bare layers are built once per race and body version and shared (`tools/preview/cache/layers/<race>/<body_hash>/`).
- Alignment stays at conversion time. The `align.json` field is LOD-independent, so the layers use it with whichever LOD you
  picked.
- Parity test before switching:
  - rebuild about 10 modules (outfit, body armour, gloves/boots, a gnome, a long hem, the 0.2 plate example) with the new tool;
  - compare them with the prototype's output: faces, colours, morph end positions, ring vertices;
  - compare CPU against GPU;
  - you also compare them side by side in Mix.
- Prototype layers already built are **not imported**: 321 of them don't record their LOD, and most were built at 0.4. They are
  rebuilt when you approve in 3D; the whole catalogue takes about 1.3 h at 11 workers.

## 7. The screens

- **Concepts**: hover and modal offer **Provisional** / **Approve** / **Reject**, plus **Archive** in the modal. Keys: Enter
  approves, P sends to Provisional, Esc rejects.
- **Approved**: per-race progress chips (fronts, views 3/4, converting, reducing, failed). Holds are shown with their reason and
  **Regenerate view** / **Accept anyway** / **Retry**. The modal shows every view image full size.
- **3D modal**:
  - The viewer shows the full labelled model at the chosen LOD.
  - Bottom bar: race, module, LOD (characters 0.4 / 0.2 / 0.1; monsters 0.8 to 0.1), **All** ("this LOD for every race"),
    < > between sets, **Approve**.
  - A small race × module grid shows the LOD picked for each, so an odd one out stands out.
  - Wireframe toggle and face count.
  - **Redo** menu per race: reconvert (another backend), regenerate views, or drop the race from this set.
- **Aligned modal** (built from the prototype's `mix.html`):
  - Top bar: race + < > between sets.
  - The viewer shows the bare head with the outfit, or torso + gloves + boots + bare head for armour.
  - **Amount** slider, saved when released. Armour sets have two: body armour, and gloves/boots.
  - Armour: **Gloves** and **Boots** dropdowns with < > (this set / any other built set of the same race / bare), plus
    **Own pieces** to go back.
  - Hold **Space** to compare with Amount 0.
  - Red-backs and wireframe toggles.
  - The build's warnings: open edges, poke-through %, wall crossings left.
  - Buttons: **Rebuild**, **Back to 3D**, **Approve**.
- **Completed**: the same viewer, read-only; **Unapprove** sends the set back to Aligned.
- **Mix**: the prototype's mix page over everything built. Pick any race, outfit/armour, gloves and boots.
- **Frontend**: `app.js` is split into small ES modules with no build step. three.js is stored locally instead of loaded from the
  CDN. Every button that writes sends the version it saw, so two open tabs (PC and phone) can't overwrite each other. Writes need a
  per-session token, because the site is on the tailnet and will now start paid work.

## 8. Monsters

- **Intake**:
  - New monster concepts appear in Concepts with the Monster tag. They are read from `monsters/aaa/concepts/<Monster>/`, or from a
    folder you choose (decision 5).
  - The many old unpicked monster attempts go to **Archive** in the migration (restorable), so they don't flood Concepts.
  - Approve picks the concept. It gets the next variant number and its files are copied into `pipeline/<Monster>/views/<n>/`, as
    done by hand today.
- **Views**:
  - Codex makes 3 more views from the front: camera turned about 90° left, 180° (back), and 90° right, at the same height.
  - There are no checks or retries beyond one retry for a missing file.
  - The Approved modal shows the 4 images with an **Enabled** tick on each.
  - By default the card then says **Choose views**, and **Use enabled views** starts the conversion. Spending only starts once you
    have looked. A "convert straight away" setting skips this (decision 2).
- **Conversion**:
  - Hyper3D: one picture uses its single-image route; 2 to 4 pictures use its multi-image route (`hyper3d_twoview` extended to N
    pictures).
  - Tripo is used when there are credits and when its four fixed slots can hold the enabled pictures. Otherwise Hyper3D is used,
    and the card says why.
  - The pictures are never resized or duplicated. Tripo also needs all pictures at the same pixel size.
  - A pilot on 2 monsters with your OK comes before this is switched on.
- **Then**: `reduce_monster.py` (5 LODs), then the 3D tab, then **Approve**, then **Completed**. Completed writes
  `chosen_lod.json` `approved`, so `build_monster_blends.py sync` keeps working. Rigging and library sync stay manual for now; see
  the suggestions.

## 9. Moving existing work over

A dry-run report comes first: what goes where, conflicts, and files that live on the D: drive. Nothing is moved or deleted; the
importer is run twice and must give the same result.

| Today | In v2 |
|---|---|
| concepts / provisional / archive / trash / held | same tabs, unchanged |
| processing_views (10), multiview_ready (47), processing (36) | Approved, with progress; finished by the old services or handed over at a checkpoint |
| completed/ (220 jobs, 73 sets) | 3D, with your existing LOD picks preselected (not approved) |
| `model_approval.json` (1 set: magic plate mail 15, 22 picks at 0.2) | 3D, picks preselected (you said it may be regenerated) |
| prototype `out/layers` | not imported; rebuilt on 3D approval; kept for comparison until cut-over |
| monsters: reduced, not approved | 3D |
| monsters: approved `chosen_lod.json` (17) | Completed |
| fit lab, body sections, `fit_choice.json`, face marks | UI removed; files kept |

## 10. Phases (each ends with something you can try)

| # | Work | You can try |
|---|---|---|
| 0 | git tag; fix the outfit Hyper3D mesh step; measure layer-build memory | - |
| 1 | split serve.py / app.js; new tab bar + filters; 3D / Aligned / Completed views computed read-only from today's files; new concept buttons; dry-run migration report. Old services keep running | the new layout over all existing data; Provisional / Approve / Reject |
| 2 | `tools/layers/` move + API + parity test; Mix tab | prototype vs new builds side by side |
| 3 | 3D modal + Approve; layer lane in the server; Aligned modal; Completed tab + `ready.json` | approve a converted set at 0.2, watch it build, set the Amount, approve |
| 4 | the orchestrator takes over claims, views, conversions (Tripo then Hyper3D), idle/pause, retries; Queue tab; old supervisor / runner / worker drained and retired | approve a concept and see it reach 3D with no chat command; restart the server mid-run |
| 5 | monsters as a kind (intake, extra views, view ticks, 1–4 picture conversion, `reduce_monster`, 3D, Completed) + migration | a monster from concept to Completed (paid pilot with your OK) |
| 6 | clean-up: old tabs and dead prototype code removed; AGENTS.md / CLAUDE.md / READMEs (both projects) updated; optional start-at-logon task | - |

How it would be built:
- implementer agents per phase, each phase checked by me;
- a Codex (Astra, extra high) code review at the end of every phase (user, 2026-11-25), its findings fixed before the next phase;
- tests extended in `tools/preview/test_serve.py` and `test_monsters.py`, with fake workers for the queue: restart mid-claim,
  after a paid click, during reduction.

## 11. Taken from Codex's plan

- Pin the source job per race/module; never let a newer conversion silently replace an approved one.
- Monster views: "review before spending" by default, with an unattended option. Hyper3D as the fallback when Tripo's slots can't
  hold the chosen pictures.
- Unreadable balance ≠ zero credits. The receipt is written before the paid click. An expired lease never justifies a paid retry.
- Single-instance lock and a clean handover from the old services; never two dispatchers on the same folders.
- Builds published atomically into their own folder, with a completeness check (no missing glove/boot hidden as success).
- Inventory-only migration first, idempotent, with a reconciliation report.
- The race × module LOD grid, hold-to-compare with Amount 0, a "restore own gloves/boots" button, and version checks against
  stale tabs.
- Not taken:
  - One Amount per set and race for armour. You chose separate body-armour and gloves/boots sliders.
  - SQLite for the decisions too. Decisions stay JSON beside the models, so tools, agents and the exporter can read them.
  - Importing prototype layers by provenance. Rebuilding is simpler and they are mostly 0.4.
  - Moving alignment to after 3D approval. The field doesn't depend on the LOD, and alignment is already validated where it is.

## 12. Suggestions (not asked for)

1. **Colour fixes in the 3D modal.** You said you'll correct many colour assignments. The viewer already has a face brush
   (right-drag, saved as marks). It could paint a colour role instead, saved per module and applied before the layer build, so
   there's no round trip through Blender.
2. **Bulk approve in 3D**: tick several cards and approve them at their preselected LODs.
3. **Start at logon**: a Windows logon task for the one server, since nothing restarts the services after a reboot today.
4. **Disk guard** in the header (the disk has filled twice). New work pauses below a threshold.
5. **Phone notification** when a set reaches 3D or Aligned, or something fails (for example via ntfy, if you want it).
6. **Monster extras in Completed**: **Rig** and **Sync to library** buttons that run the existing tools.
7. **Stale-build badge**: after a layer code change, Aligned and Completed show which builds are older, with **Rebuild all stale**.

## 13. Decisions (user, 2026-11-25)

1. **Approval unit: the whole set.** A set enters 3D when every required race/module is reduced or dropped. One Approve covers all
   races at their chosen LODs, and Aligned works the same way.
2. **Monster views: hold for "Choose views".** Conversion starts on **Use enabled views**. A per-monster "convert straight away"
   option is still offered at approval.
3. **Amount: separate for armour.** Armour sets get a body-armour slider and a gloves/boots slider, per race. Outfits get one slider
   per race.
4. **Layer builds while the user is at the PC: yes, one at a time.** Full parallelism when away.
5. **Monster concept intake (default, not asked).** `monsters/aaa/concepts/<Monster>/` as today. Old unpicked attempts go to
   Archive in the migration.
