# Preview app v2 - phase 5 contract (monsters as a kind)

Inputs: PIPELINE_V2_PLAN.md §8 and §13; DESIGN_ORCHESTRATOR.md "Monster lanes". Unless stated otherwise, paths are relative to the
monster root `F:/godot/blender/characters/monsters/aaa`. That root is in the **blender** repo, not in imageto3d. Its own rules are in
`AGENTS.md`, `CLAUDE.md`, `PROMPTING_GUIDE.md` and `tools/`.

## A. Concept intake
- **What counts as a concept:**
  - `concepts/<Folder>/codexN-front.png` with its `codexN-front.prompt.json` (and `.colours.json` when present). Shown.
  - `codexN-front_attemptK.png` files, numbers with JSON but no PNG, and loose legacy PNGs without JSON. Hidden.
  - There are 28 folders and about 1,739 final images.
- **State file:** `concepts/<Folder>/.v2_state.json`, which v2 writes atomically under a per-folder lock:
  `{"schema":1,"concepts":{"codexN":{"stage":"concepts|provisional|approved|archive|trash","time":"...","monster":"War Horse",
  "variant":"2"}}}`.
  - A concept the file doesn't list is in stage `concepts`.
  - Monster files never move; Reject and Archive are state changes only.
  - Trash is restorable from the Trash tab.
- **Migration** (`migrate.py --monsters --apply`, run once by Claude with a dry-run report first):
  - Every final concept that exists at cut-over goes to `archive`, unless it is already picked.
  - Already picked means either of:
    - `pipeline/<M>/variant_names.json` `variants[k].concept == "codexN-front.png"`;
    - a sha256-identical `pipeline/<M>/<k>-front.png`.
  - Picked concepts become `approved`, with their monster and variant recorded.
  - Goblin Shaman's codex4 is picked; it is known only through `was`, so handle it with an explicit override list in the migration.
- **Folder to monster:** match `concepts/<Folder>` against `pipeline/<M>` by lowercase with spaces removed (`Warhorse` ↔ `War Horse`).
  With no match, the monster name is the folder name, used for a new pipeline folder.
- **Cards:** id `mconcept:<Folder>/<codexN>`, type monster, kind monster.
  - The title is `<Monster> · codexN`.
  - The thumbnail is the PNG.
  - Details come from the prompt json (design, notes), plus the per-N intent from the folder's set files (`adjectives.json` etc.)
    when present.
  - Actions: concepts get provisional, approve and reject; provisional gets approve and unapprove; archive gets approve and
    unarchive; trash gets restore. These go in the action allowlist.

## B. Approve = pick
These steps run under the monster lock.
1. Assign the variant:
   - a brand-new monster (no `pipeline/<M>`) gets `main`;
   - a monster that already has `main` keeps it and gets the next number from 2 (main counts as 1; never rename `main`);
   - a numbered monster gets max numeric + 1, counting retired numbers in `pipeline/<M>/variant_names.json` and the folders, and
     never reusing one;
   - War Horse `<n>-<armour>` ids are out of scope (armour variants stay manual).
2. Copy `codexN-front.png` to `pipeline/<M>/views/<v>/front.png`, and the `.colours.json` and `.prompt.json` to `colours.json` and
   `prompts.json`. Do not write the flat `<k>-front.*` files; nothing reads them.
3. Add `{"was": "codexN", "concept": "codexN-front.png", "picked": "<time>", "by": "v2"}` to
   `pipeline/<M>/variant_names.json` `variants[<v>]`.
4. Write `pipeline/<M>/views/<v>/views.json`:
   `{"schema":1,"views":[{"name":"front","file":"front.png","enabled":true}],"unattended":<from the approve dialog>,
   "frozen":null}`.
5. Queue the three view tasks (section C).
6. If `<M>` is not in `tools/build_monster_blends.py` `PIPELINE_LIBRARY`, the card gets a badge "no library folder mapping (add to
   PIPELINE_LIBRARY)".

Undo of an approve: allowed only while no view task has started. It removes the copied views folder (to the Recycle Bin via the
trash helper or a `.v2_withdrawn` rename) and the variant_names entry.

## C. Extra views (Codex, no checks)
- Three outputs per variant: `left.png`, `back.png` and `right.png` in `views/<v>/`, each with a `<name>.prompts.json` record that
  follows the Hobgoblin `2-back.prompts.json` field convention.
- Generation goes through the imageto3d view runner with a new **task field `"launcher": "monsters"`** (`tools/view_batch.py`):
  - Launch `F:/godot/blender/characters/monsters/aaa/tools/codex_imagegen.sh` with cwd = the monster root.
  - The job dir is `<monster root>/.codex_jobs/<job>` (`.done`, `exit_code`, `log.txt`).
  - This changes the script path, cwd, `jobdir`, `job_alive` and the name-collision loop. Default behaviour is unchanged.
  - The runner picks this up only after a restart, which the Queue tab can do (phase 4).
- Task: `checks: []`, `max_attempts: 2` (one retry for a missing or invalid file), priority 3. The output must be 8-bit RGBA with
  transparency; the runner only checks that the file exists, and the conversion validates the format.
- Prompt templates live in `v2/monster_prompts.py`.
  - Back: the established wording ("Image 1 shows a papercraft creature from the front. Draw the SAME creature from BEHIND: a back
    three-quarter view from slightly above. The camera has walked round to the opposite side ... turned 180 degrees ... same
    height and distance. The creature has not moved ...").
  - Left and right: the same, with "a quarter of the way round to the creature's LEFT (RIGHT) side (about 90 degrees) ... we now
    see its LEFT (RIGHT) side". Use anatomical sides, stated explicitly.
  - All three share: the same design and the same marker colours (from colours.json roles), front-only ornaments not copied, the
    whole creature in frame, transparent background, the "ONE hue" paragraph from PROMPTING_GUIDE.
  - Image 1 = `views/<v>/front.png`.
- No framing, pose or colour checks, and no aesthetic retries (user, 2026-11-25).

## D. Choose views → convert
- When the view tasks are done or failed, the card says **Choose views**.
- The Approved modal for a monster variant shows the 4 images large with an **Enabled** tick each (front included). A failed view
  shows **Retry**.
- **Use enabled views** (verb `convert`, `args.enabled`) writes `views.json` `frozen = {time, files:[...], sha256:{...}}` and queues
  the conversion task. At least one image must be enabled.
- With `unattended: true`, the freeze happens automatically with every available view once the view tasks finish.
- Changing ticks after freezing makes a new draft. Converting again needs an explicit **Reconvert** (paid) button, which is only
  available in the 3D tab.
- Legacy variants (no `views.json`) are already converted and need no choice.

## E. Conversion lane (orchestrator lane `monster_convert`, limit 1)
- **Gating:** the same as conversions. PAUSE scope `conversions` or supervisor mode `pause`/`pause-conversions` means wait; in
  mode `views-run`/`auto`, it runs only when the user is away. A per-task **Run now** (Queue tab) overrides the idle gate but not
  a manual pause, unless the user also presses it while paused (it then asks for confirmation).
- **Browser lock:** `.codex_jobs/_batch/browser_hyper3d.lock` (imageto3d root) is a cross-process msvcrt/fcntl lock.
  - Acquire it in `tools/hyper3d_pipeline/rodin_backend.py` `SeleniumRodin.__enter__` and release it on exit, waiting up to 10 min;
    if still busy, `Stop('Hyper3D profile busy')`. The lane treats that as "requeue after 5 min", not as a failure.
  - The outfit `convert_job.py` starts fresh python processes, so it picks this up without a restart.
- **Steps** (each resumable from the record; never pay twice):
  1. If `models/<v>/hyper3d/studio_submission.json` exists, go to collect or inspect according to its `status`, `actions` and
     `pending_action`. An existing paid action or `credits_committed > 0` means never submit again.
  2. Submit (free):
     - one enabled view: `hyper3d_single.py submit` with `view_order = [that file]` (generalise `single_image_config`);
     - 2-4 views: a new `tools/hyper3d_multiview.py` (generalising `hyper3d_twoview.py`):
       - `view_order` = the frozen list, in that order;
       - `directions = {}`, `multi_view = True`;
       - `prepare_multiview` compares against `config['view_order']`;
       - `validate_views` without the equal-size rule, keeping the RGBA/PNG checks.
     - Run it with `%USERPROFILE%\.cache\hyper3d-venv\Scripts\python.exe -X utf8` from the monster root.
  3. `collect --allow-paid --accept-preview` (PAID, 0.5 credits). Append to the imageto3d `.codex_jobs/_app/paid_log.jsonl`:
     `{time, kind:"hyper3d", monster, variant, views, credits: 0.5, record}`.
  4. `python tools/reduce_monster.py "<M>/<v>"` (Blender, about 1.5 min). It writes `models/<v>/reduced/` with 5 LODs.
- **Failure:** show the record status, the wrapper's Stop message and screenshot paths. Retry resumes from the record.

## F. 3D → Completed
- The 3D modal (monster3d.js) gets **Approve**: it writes `chosen_lod.json` `{lod, approved: true}` through the locked v2 monster
  writer.
- **Unapprove** in Completed clears `approved`.
- The approved LOD stays locked; this already exists.
- **Reconvert** in the 3D modal (paid; confirmation states the cost) starts a new conversion:
  - it moves `models/<v>/hyper3d` and `reduced` to `*_prev_<time>`;
  - it requires the frozen view list (the current ticks).
- Completed monster cards show the library hint (B.6).

## G. Pilot (Claude, after implementation; at most 4 Hyper3D conversions in total, user 2026-11-25)
1. Never pick concepts on the user's behalf. The pilot uses variants the user ALREADY picked but that were never converted:
   - Gibbering Mouther, Hobgoblin, Lizardfolk, Merfolk and Sahuagin have flat `pipeline/<M>/<k>-front.*` picks and
     `variant_names.json`, but no views/ or models/.
   - Migration maps those picks to `approved`. The app's "import pick" path creates `views/<k>/` from the flat files with the
     same rules as B.2–B.4, without renumbering. That queues the extra views.
   - Use 2 such variants: one with `unattended` off, one on.
2. Let the views generate (Codex), choose views on the first: enable front + 2 others.
3. Run the conversions with Run now. That is 2 paid conversions, logged in paid_log.jsonl.
4. Verify that the mixed-size multi-view upload works. If it doesn't, the fallback is resizing on our side? **NO**: never resize.
   Report and fall back to the largest same-size subset or a single view, and ask the user.
5. Approve one LOD in 3D, check Completed, then unapprove.
6. Report the credits used.

## H. As built (phase 5 integration, 2026-11-25)
This section replaces the two working notes of the phase (`PHASE5_INTEGRATION.md`, the backend hook list, and
`PHASE5_FRONTEND_CONTRACT.md`, the UI's expectations); where they disagreed, the shapes below are the ones both sides now use.

### H.1 Modules and wiring
- `monsters_v2.py`: concept intake, `.v2_state.json`, folder to monster, numbering, pick / import / withdraw, view tasks, `views.json`
  ticks / freeze / convert / reconvert, 3D approve / unapprove, cards and items, auto-freeze, the migration.
  `monster_prompts.py`: the view prompts. `monster_lane.py`: the `monster_convert` lane and its driver.
- `taskqueue.LANES = ('bare', 'layers', 'monster_convert')`. `TaskQueue.mark_run_now(id, override_pause)` is the public write of Run now
  (args `run_now` / `override_pause`, backoff cleared, event `run_now`; only a queued row). `count(..., lanes=)` filters by lane.
- `catalog`: `_build_state` takes every monster card from `monsters_v2.state_cards` ({tab: [card]}); `item()` hands `monster:` and
  `mconcept:` ids to `monsters_v2.item` (unknown = `catalog.NotFound`, 404; a malformed id = 400). The old `monster_stage` /
  `monster_card` are gone (`monsters_v2.monster_stage` is the same rule).
- `actions.perform`: `mconcept:` / `monster:` ids go to `monsters_v2.perform` BEFORE the `NAME` check (their names have spaces and a
  slash); its allowlist is `monsters_v2.ALLOWED`. `task_approved` of a monster task = `monster_lane.task_wanted`. `queue_op` has `run_now`.
- `orchestrator`: every tick (with reconcile on) runs `monster_lane.tick` (auto-freeze + a task for every frozen list without one);
  `_start_queued` is the layer pool only (`LAYER_LANES`, counting only their active rows); `_start_monsters` runs the lane's plan
  (`plan_starts`: cancel unwanted, start at most 1) on its own, so it also starts while the layer pool limit is 0. `default_argv` /
  `_spawn` env / `_finish` / `_approved` dispatch on the lane; `_approved_in` is True for the lane, so reconcile never cancels a monster
  task (the lane's plan cancels a task whose frozen list was replaced). `queue_state` adds `monsters` and `workers.running` counts the
  layer pool only.
- `progress.views_section` already reads a monster view task's log under the monster root (phase 4 F19); `mviews` shows in `by_kind`.
- `migrate.py --monsters [--apply]` (H.6). `server.py` is unchanged: 409s carry `guard.Conflict.code`, `/mfiles/` serves the PNGs.

### H.2 Concept cards and items (`mconcept:<Folder>/<codexN>`)
- Card: `{id, type: "monster", kind: "monster", name: "<Monster> codexN", title: "<Monster> · codexN", stage, thumbnail (/mfiles/ PNG),
  actions, badges, monster, folder, concept, next_variant, library_hint}`.
  - `actions`: concepts `provisional, approve, reject`; provisional `approve, unapprove`; archive `approve, unarchive`; trash `restore`.
    The dialog adds Archive in concepts and provisional (the allowlist accepts `archive` from both stages).
  - `next_variant` = the variant an approve would assign now (`main`, `2`, ...); `library_hint` = the B.6 text or null. Badges: `new monster`
    (next variant is `main`), `no prompt record`, `state unreadable` (and no actions), `from <stage>` in the trash. The library hint is
    NOT a concept card badge (it would sit on every concept of the monster); the dialog shows it.
- Item = the card plus `{card, concept, image, prompt, intent, roles, variant, variant_id, state, state_error, library, pipeline_exists}`:
  `prompt` = the prompt json's `design, notes, colour_variant, job, date, attempts, size, mode, prompt` (present keys only); `intent` =
  `{source: text}` from the folder's set files (`adjectives.json` ...) plus the prompt json's `adjective, direction, local_description,
  user_guidance, set`, or null; `roles` from `.colours.json`; `variant` / `variant_id` once approved.
- Verbs: `provisional`, `approve` (`args.unattended`; response `{ok, monster, variant, id, from, tasks, library, undo, warning?}`; the
  UI's toast names the response's variant), `reject`, `archive`, `unarchive`, `unapprove` (`args.to`), `restore`. Undo descriptors as
  before; the undo of an approve (withdraw) is refused (409) once a view task has started.

### H.3 Variant cards and items (`monster:<Monster>/<variant>`)
- Card: the phase 4 monster card (stage `approved` | `3d` | `completed` by `monster_stage`) plus `actions` (approved: `[]`, or `["import"]`
  for a flat pick; 3d: `["approve"]` once a LOD is saved; completed: `["unapprove"]`), `library_hint`, and badges: the status, `views a/b`,
  `view failed n`, `Choose views`, `convert straight away`, the conversion state (`conversion queued` / `converting` / `conversion failed` /
  `converted` / `conversion cancelled` / `conversion not queued`), `picked, not imported`, `sections out of date` (H.7), `approved`, the
  library hint (warn). A hover Approve (3D) and a hover Import (Approved) only open the modal; a hover Unapprove (Completed) runs.
- Item = the raw card plus:
  - `views`: always front, left, back, right: `{name, file, exists, url, enabled, planned, task, retry}`. `url` = the `/mfiles/` URL or
    null (a flat pick's front shows its flat file). `task` = null (no task file) or `{name, state: queued | running | done | failed,
    attempts, max_attempts, error, job, started}` (the runner's `pending` = queued, `launching` = running; `error` of a failed task =
    `no image written (last job ..., exit ...)`). `retry` = an extra view without an image whose task failed or was never written.
  - `phase`: `legacy` (no views.json) | `generating` | `choose` | `frozen` | `import` (a flat pick); `legacy`, `importable` (booleans).
  - `unattended`, `frozen` (`{time, files, sha256, names, auto?, reconvert?, moved?}` or null), `draft_differs`.
  - `conversion`: null before anything was frozen or recorded, else `{task_id, state: queued | waiting | running | done | failed, step:
    submit | preview | collect | reduce, waiting_reason, paid, credits, error, attempts, run_now, record, screenshots, reduced, started,
    task}`. `step`: no record = submit, a record with nothing paid = preview, paid but no GLB = collect, an exported GLB = reduce (a failed
    task: the verdict's step). `waiting_reason` (state `waiting`): `held` (CONVERT_HOLD), `paused`, `user_present`, `browser_busy` (a busy
    requeue), or a text (`another monster conversion is running`, `the conversion lane queues it at its next pass`). `record` = `{status,
    stage, pending_action, credits_committed, why, model_url, images_uploaded, actions: [names]}`; `screenshots` = `/mfiles/` URLs of the
    record folder's PNGs, newest first. A record without a task (a hand conversion that stopped) is state `failed` with that as error.
  - `library` (`{known, mapped, folder}`), `library_hint`, `pick` (the variant_names entry).
- Verbs (superseded in detail by H.10): `views` (`args.enabled`, a partial map of views with a valid image; 400 for a view without one),
  `convert` (`args.files` + `args.views_rev`; `args.enabled` is 400; 409 `busy` while a conversion of the variant is active; 409 `changed`
  / `missing`; 409 `frozen` once a record / GLB / reduced/ exists: converting again is Reconvert), `reconvert` (`args.files`,
  `args.views_rev`, `args.confirm: true`; 409 `approved` while approved; 400 when there is nothing to redo; moves `hyper3d` and `reduced`
  to `*_prev_<time>` as one journaled transaction), `view_retry` (`args.view`), `import` (`args.unattended`), `approve` (`args.lod` +
  `args.glb_sha256`; 409 `approved` / `changed` / `unverified` / `busy`), `unapprove` (completed -> 3d; on a v2 pick whose views have not
  started it undoes the pick).
- `convert` is allowed while a view is still being drawn (the UI asks "use the views that exist now?"): a stuck view runner must not
  block a conversion with the front alone. It is also allowed again on a frozen list whose conversion never started (no record, GLB or
  reduced/: nothing was paid), which replaces the frozen list; the old task is then cancelled by the lane.

### H.4 Queue
- `POST /api/v2/queue {op: "run_now", id, key, generation, confirm?, pause_id?}` (H.11 F4 / F5): the task is the one the client
  DISPLAYED (`key` + frozen `generation`; `id` is a hint only; 400 without them, **409 `changed`** when that identity is gone). A queued
  monster task (a failed / cancelled one is queued again first, while still wanted) gets `run_now`; it overrides the idle gate. While
  conversions are paused by hand (mode `pause` / `pause-conversions`, or a manual PAUSE with scope conversions) the answer is **409
  `code: "confirm"`** with the question as `error` and the pause in force as `pause_id`; the client asks and sends `confirm: true` AND
  that `pause_id`, which becomes `args.override_pause` (an echoed id that is not the pause in force is answered 409 `confirm` again). The
  conversion hold is never overridden: the answer is 200 with a `warning`, and the task starts when the hold lifts. Logged as queue
  event `op`.
- `retry` / `cancel` of a monster row need `key` + `generation` too (same rule; `task_approved` = the lane's check); for a row of
  another lane a `key`, when sent, must be the row's (409 `changed`). `view_retry {name}` accepts `mviews--...` names.
- `GET /api/v2/queue` `monsters` = `monster_lane.lane_status`: `{lane, limit: 1, gate: {open, reason, code, mode, idle, away,
  needs_confirm, paused, pause_id, pause_reason}, totals: {state: n}, paid: {count, credits, recent}, hold}`. Rows of the lane: `set` = `monster:<M>/<v>`, `unit` =
  `convert`; a queued row has **Run now** in the Queue tab, and the tab shows one "Monster conversions" line (gate, counts, paid).

### H.5 Lane gate and outcomes (E, as built)
- Gate order: CONVERT_HOLD (`held`, never overridden) > mode `pause` / `pause-conversions` or a manual PAUSE with scope conversions
  (`paused`, overridden only by a confirmed Run now) > in mode `auto` / `views-run` the supervisor's "user active" PAUSE or idle < 300 s
  (`user_present`, overridden by Run now). Mode `run` skips the idle gate. The driver re-checks the gate before every step with the
  real presence (a gate that closed mid-run = verdict `waiting` = requeue after 60 s).
- `finish_outcome`: `done` needs exit 0, verdict `ok` and the lane's verified reduction of the task's generation (`v2_reduce_ok.json`,
  H.10 F12); `busy` (Hyper3D profile busy, or another driver / a live child of the variant) requeues after 300 s; `waiting` (the gate,
  also a wrapper child's click-time exit 4) after 60 s; anything else fails (its disposition persisted first, H.10 F4), and a failed task
  is never retried automatically (max_attempts 1; Retry resumes from the record).
- A confirmed Run now overrides only the pause it was confirmed for (H.10 F4); the gate is also re-checked by every wrapper child right
  before each credit-capable click (H.10 F2).

### H.6 Migration
- `python tools/preview/v2/migrate.py --monsters` writes the report `.codex_jobs/_app/monster_migration_report.{json,md}` only;
  `--monsters --apply` then writes `concepts/<Folder>/.v2_state.json` (idempotent: a listed concept is kept). `--apply` without
  `--monsters` is an argument error. The monster root is `$IMAGETO3D_MONSTERS` or `characters/monsters/aaa`; the CLI prints it first.

### H.7 Decisions (user, 2026-11-25)
- **Import of a flat pick may reuse an existing flat `<k>-back.png`** (with its `.prompts.json` / `.colours.json`) as `views/<k>/back.png`;
  only left and right are then queued (`views.json` `planned`), and `imported` records which flat files were copied.
- **`paid_budget_hyper3d` (settings.json) is NOT a gate.** The lane never reads it; it stays an unused setting. What the lane spends is in
  `paid_log.jsonl` and in the Queue tab's "Monster conversions" line.
- **Reconvert leaves `models/<v>/sections/` in place.** The card (and the 3D modal) shows `sections out of date` while the newest file of
  `sections/` is older than `reduced/manifest.json`; rerunning the monsters' `split_monster_sections.py` clears it.
- Never resize, crop or pad a view (C, G.4) holds unchanged: the lane uploads the frozen files as they are.

### H.8 Contract reconciliation (what changed against the two drafts)
| Topic | Draft UI | Draft backend | As built |
| --- | --- | --- | --- |
| concept item | flat fields | `detail.{...}` | flat fields (`detail` dropped; nothing read it) |
| concept card | `next_variant`, `library_hint` | none | both on the card |
| view image | `file_url` | `url` | `url` |
| view task | `{state queued/running/..., max_attempts, error}` | the runner's raw status | the UI's words (H.3) |
| view Retry | queue op `view_retry {name}` | verb `view_retry {view}` | the verb (it also writes a missing task file) |
| frozen list | `views_frozen {names}` | `frozen` | `frozen` with `names` |
| conversion | `{step, state, waiting_reason, task_id, paid, credits, error}` | `{task, record, screenshots, ...}` | the UI fields plus `record` and `screenshots` (E's failure display) |
| Run now while paused | `override_pause: true`, 409 `paused` | `confirm: true`, 409 `confirm` | `confirm` / 409 `confirm` |
| Reconvert | `args.enabled` | `args.confirm` required | the UI sends `confirm: true` |
| reconvert while approved | 409 `approved` | 409 `conflict` | 409 `approved` |
| convert after conversion | 409 `frozen` | 409 `conflict` | 409 `frozen` |
| convert while views are drawn | 400 | allowed | allowed; the UI asks first |
| Import (flat pick) | not drawn | verb `import` | Import button + unattended tick in the Approved modal |
| conversion failure | error only | record + screenshots | error, record status, screenshots, Retry |

### H.9 Before the first real pick
- Restart the view runner (Queue tab, Restart views): it reads `"launcher": "monsters"` only after a restart.
- The Hyper3D browser lock (`rodin_backend.SeleniumRodin.__enter__`) is in place; nothing else is needed.

### H.10 Phase 5 review fixes, monster backend (review_v2_phase5_20261125, findings 1-18; as built)
- **F1 free preview.** The lane resumes a `submitted` record whose `pending_action` is `free_preview` when its model is identified
  (`model_id` set and part of `model_url`: `monster_lane.free_preview_resolved`); the backend now resolves such intents itself
  (`Record.resolve_free_preview`). Any other pending click (a paid one, or a free one without a bound model) stays refused. A record left
  `submission_needs_review` is a real needs-review state (collect refuses it; the lane shows the error). Regression: `RealRecordTest`
  drives the REAL `rodin_backend` submit / collect (fake browser session) through the lane.
- **F2 click-time gate.** Every wrapper child of the driver gets `paid_gate.env_for(root, override_pause = run_now or a bound override,
  cmd = [python, -X, utf8, v2/monster_lane.py, gate, --root, R, --task-args, {run_now, override_pause}, (--idle N)])`;
  `monster_lane.py gate` exits 0 when open, else 1 with the reason on stdout. A child exit 4 (`paid_gate.EXIT_GATE_CLOSED`, stderr
  `PAID GATE CLOSED: ...`) is verdict `waiting` (requeue after 60 s), never a failure. With `--manage-services` the server builds the
  ServiceManager first and passes its `hold_asserted` Event to `Orchestrator(monster_ready=...)`: `_start_monsters` starts nothing until
  the manager asserted the startup hold. Without managed services the CONVERT_HOLD / PAUSE files decide.
- **F4 dispositions.** views.json `conversion[<generation>] = {state: cancelled | failed, reason, key, time}` (generation = the 12-hex
  frozen digest = the task key suffix), written BEFORE the queue row changes: Cancel (`monster_lane.cancel_task`, Queue op `cancel`) and a
  failed / lost attempt (`monster_lane.record_failure`, orchestrator `_finish` / `_lost`). An ordinary Retry (`monster_lane.retry_task`, also
  Run now of a failed / cancelled row) clears it first. `tick` looks each frozen generation up by its EXACT key (`TaskQueue.by_key`;
  `TaskQueue.enqueue_once` never revives a cancelled / failed row); no row + a disposition = the row is restored in that terminal state
  (`TaskQueue.restore_terminal`), never queued; a queued row of a generation with a disposition is cancelled by the plan. Run now's
  override is the `pause_id` of the confirmed pause (superseded key scheme: see H.11 F5), kept as `args.override_pause`; a different
  pause needs a new confirmation; `TaskQueue.retry` clears `run_now` and the override. The run_now answer carries `pause_id`.
  Superseded in part by H.11 F3 (idempotent transitions, the disposition sequence, outstanding writes) and F4 (task identity).
- **F5 paid accounting.** `paid_log.jsonl` entries are per provider action: `{time, kind: hyper3d, id: "hyper3d:<model id>:<action>",
  action, status: confirmed | uncertain, credits (the OBSERVED cost, null when not observed; never assumed), monster, variant, views,
  record, model_id}`; actions observed at cost 0 are not logged. confirmed = the backend observed the next stage (its intent was cleared);
  uncertain = the intent is still pending. `reconcile_paid` runs on EVERY driver run (also exported / reduced records) and after collect;
  the last entry per id counts (older id-less entries count as confirmed). A write error goes to `.codex_jobs/_app/paid_log_errors.json`,
  shows in `lane_status.paid.errors`, the verdict's `accounting_error` and the item's `conversion.accounting_error`, and is retried by
  every tick until it succeeds. `paid_summary` = `{count, credits (confirmed), unknown_amounts, uncertain: {count, credits}, recent,
  errors}`; item `conversion.paid` = a confirmed paid action, `paid_uncertain` = the uncertain ones, `credits` = the confirmed observed
  amount or null.
- **F6 Reconvert.** Under the monster lock AND the LOD lock of 3D approve (`models/<v>/.pipeline_v2.lock`; approve takes both, same
  order): an unreadable chosen_lod.json is 409 `unreadable`; an active conversion (a queued / running row, a driver holding
  `.codex_jobs/_app/locks/mconvert/<variant key>.lock`, or a live recorded child) is 409 `busy`; an unreadable queue is 409 `unreadable`
  (`monster_lane.active_reason`). The new freeze is validated and hashed first; then `models/<v>/.v2_reconvert.json` {moves, generation} is
  written, hyper3d/ and reduced/ are renamed and views.json gets the freeze; an error rolls the moves back; a crash leaves the journal,
  which recovery (any later mutation of the variant, every tick: `recover_reconverts`) completes when views.json carries its generation and
  rolls back otherwise.
- **F7 inputs.** Before submit the driver copies the frozen files to `models/<v>/hyper3d/inputs_<generation>/` (read-only, each sha256
  verified; an existing folder is re-verified or rebuilt) and passes THAT folder to the wrapper (the backend copies it again to
  `upload_inputs/` and records `inputs: [{name, sha256, size}]`). On every resume the record's `inputs` must equal the frozen files and
  digests in order, else verdict failed `check` before anything is spent; a record without `inputs` is accepted only when it uploaded the
  frozen names from this generation's verified snapshot (`source`).
- **F8 explicit files.** `convert` args `{files: [ordered view files], views_rev}`; Reconvert `{files, views_rev, confirm: true}`. `files`
  is authoritative (nothing is merged in); `args.enabled` is 400 for both. `views_rev` (item field, an int below 2**53) covers the frozen
  generation, the planned views, the pick generation and every view PNG / provenance file (size + mtime), NOT the draft ticks; a mismatch
  is 409 `changed`; a listed file that is absent or invalid is 409 `missing` with `file` (the first) and `files` in the body. Order of the
  checks: active conversion (409 `busy`), views_rev, files. The `views` verb answers `views_rev` too.
- **F9 unattended freeze** only when EVERY planned view task is terminal (`done` / `failed` / `cancelled`; a never-written task is not),
  re-checked with the outputs under the monster lock; it freezes the valid views. The item phase `generating` uses the same rule (a PNG
  that appears while its job runs does not end it).
- **F10 guard and publication.** views.json `pick_gen` (uuid) is the pick generation; every view task carries `guard: {file: <abs
  views.json>, key: pick_gen, value}` (the runner spawns only while it matches; withdrawn = `cancelled`). Pick / import write their tasks
  while holding the monster lock. Withdraw renames the views folder (the guard file) FIRST, re-checks the task states (a launch reserved
  meanwhile restores everything and is 409) and only then renames the task files.
- **F11 ledgers and journal.** `variant_names.json` is validated (an object; `variants` / `retired` objects of objects); every mutation reads
  it strictly (409 `unreadable`; the file is never replaced); a damaged ledger makes the migration skip the folder. Pick / import are
  journaled in `.codex_jobs/_app/monster_journal/<monster key>.json` (written once the temp folder is complete): recovery
  (`recover_journals`, every tick; inline for the same monster / folder, else 409 `recovering`) completes the SAME variant (names entry,
  views folder, concept state, tasks); when neither the temp nor the views folder exists, the published number is retired instead.
- **F12 verified reduction.** After reduce_monster.py the lane validates the outputs (5 LOD GLBs, a manifest object, a materials.json
  object) and writes `models/<v>/reduced/v2_reduce_ok.json` `{schema, generation, frozen_time, files: {name: {sha256, size}}, materials:
  true, time}`. Completed = approved AND a verified reduction (`reduce_status`: the record of the CURRENT generation, sizes checked, sha256
  at approve; a legacy variant without views.json by the explicit rule manifest + 5 GLBs + materials.json, item `legacy: true`, card badge
  `legacy reduction`). An approved but unverified variant stays in 3D with an error badge and Unapprove. 3D approve takes `args.lod` +
  `args.glb_sha256` (item `lods[<level>].sha256`; `lods` stays a dict keyed by level): 409 `changed` when the file differs, 409 `unverified`
  without a verified reduction, 409 `busy` while a conversion is active. chosen_lod.json gains `glb_sha256`, `generation`, `legacy`; the
  undo of Unapprove carries `{lod, glb_sha256}`.
- **F13 identity.** Names refuse trailing dots / spaces and Windows device names (CON, NUL, COM1, ...); a monster / variant must match its
  on-disk folder exactly (a case alias is 400); persisted ids of `.v2_state.json` are validated (else the state is unreadable); every
  mutation target is checked against the monster root at mutation time (`contained`: lexical and resolved containment, no symlink or
  junction below the root). Locks are keyed by the casefolded name. View task names are
  `mviews--<monster slug>--<variant slug>--<canonical digest 8>--<view>`; a task file that names another monster / variant is `foreign`
  (ignored for state, 409 on publication).
- **F14 process tree.** The driver starts each child with Popen and records it in `.codex_jobs/_app/monster_lane/<variant key>.children.json`
  (pid, create time, argv) until it exits. While a recorded child lives, the attempt keeps its slot (orchestrator `_monster_tree_hold`,
  checked before the receipt; wrapper dead = orphan), no new driver of the variant starts (verdict `busy`) and the plan counts it as active.
  Kill & retry of a monster attempt collects the whole tree first (psutil children, recursive, of the builder, the wrapper and every
  recorded child), stops builder and wrapper, kills the tree and waits; any survivor = `kill_failed` (slot kept). The generic repair /
  early-receipt adoption is agent C's orchestrator change.
- **F15 migration mappings.** Import also takes a variant only the migration knows (a sha256 / override mapping in `.v2_state.json`; its
  variant_names entry is made from the concept, `how: migration ...`) and War Horse base fronts in `pipeline/War Horse/first round/`.
  `<n>-<armour>` variants are never importable (manual). An approved concept whose variant has no views / models folder and nothing to
  import is a read-only manual-only card (badge with its source; item `phase: manual`). The migration report lists the mapped importable
  picks (with `how`).
- **F16 damaged state.** views.json is schema-validated (`views_problem`); a damaged file raises `guard.Unreadable` (409 for a mutation)
  and is never replaced. The catalog builds every variant on its own: a damaged one is a read-only card (badge `damaged: ...`, no actions,
  `damaged: {error}`) and its item carries `damaged: {error}` and `state: "damaged"` (healthy items `damaged: null`). The tick, the
  auto-freeze and the frozen scan isolate per-variant errors (shown in the orchestrator errors); a lane task whose variant cannot be read
  waits (never started, never cancelled).
- **F17 view validation.** View tasks carry `validate: "rgba_png"` and `provenance: ["<view>.prompts.json"]`. v2 treats a view as valid only
  when its PNG decodes as 8-bit RGBA with a transparent pixel and (extra views) its provenance record is a JSON object (`view_problem`);
  only valid views can be ticked or frozen. Retry (`view_retry`) is offered for a failed / cancelled task and for a finished one with an
  invalid output: the invalid file and its record are renamed `<file>.invalid-<time>` and the task goes back to pending. The Codex job
  prompt no longer asks for a framing inspection or redraw (a technical format check only).
- **F18 Run now first.** The plan considers every queued row of the lane, Run now rows first (then priority, id).

### H.11 Recheck before cut-over, monster app (review_v2_recheck_cutover_20261125: findings 3, 4, 5, 8, 9, 10, 11, 14, 15, phase 5 row 10; as built)
- **F3 dispositions.** views.json `conversion[<generation>] = {state: cancelled | failed, reason, key, time, seq}` plus the variant's
  monotonic counter `conversion_seq`; every entry is validated (`views_problem` / `disposition_problem`: a 12-hex generation, a state of
  the two; anything else makes views.json unreadable = a damaged read-only card, nothing queued). `mv.set_disposition(root, monster,
  variant, generation, value | None, expect_seq=None) -> {previous, seq, written, same?, stale?, missing?}` is one read-compare-write
  under the monster lock: writing the state a generation already has changes nothing (idempotent); `expect_seq` makes an undo
  conditional on nothing newer having been written. **Cancel** (`monster_lane.cancel_task(root, task)`): an already cancelled row is
  success (and the durable `cancelled` is written when missing); when the SQLite cancel finds the row no longer queued, a row that is
  `cancelled` by now (the dispatcher saw the disposition first) is success too, otherwise only the Cancel's OWN write is taken back
  (`expect_seq`), never an older disposition put back. **Retry** restores nothing unless the row turned out to be another task
  (`taskqueue.Expired`). **Failures**: `monster_lane.record_failure` RAISES when the record cannot be written; the durable intent is
  written first to `.codex_jobs/_app/monster_lane/<variant key>.disposition.json` `{pending: {<generation>: {state, value, time,
  error}}}` (`persist_disposition`) and removed only once views.json carries it. While it is pending: the orchestrator does NOT finish
  the row (`_monster_disposition` -> False; the row stays active with the reason as its error, `TaskQueue.note_active`; the pass is
  repeated every tick), `tick` writes it again (`repair_dispositions`), never queues or restores that generation (an error line per
  tick), the plan starts nothing for it, and the item shows `conversion.disposition_pending` + an error, the card a badge.
- **F4 task identity.** A queue row id is a hint. Run now / Retry / Cancel of a monster task carry `key` (`monster_convert:<M>/<v>:<generation>`)
  and `generation`; `monster_lane.resolve_task` finds the row by key (409 `changed` when it is gone or its `args.frozen_sha` differs;
  400 without them) and every state change compares the identity again INSIDE its transaction (`TaskQueue.cancel / retry /
  mark_run_now(..., expect={key, generation})`, `taskqueue.check_identity`, `taskqueue.Expired` -> 409 `changed`). Exposed as `key` +
  `generation` on the queue rows (`generation` null for other lanes) and on `item.conversion`; queue.js and monster_views.js send them.
- **F5 pause identity.** Every pause transition has a unique `pause_id`: `modes.write_mode` stores a fresh uuid4 in
  `supervisor_mode.json` (`{mode, since, pause_id}`) on every write; a mode file or PAUSE file written by somebody else (the supervisor,
  its `mode` command) carries none and gets `file:<digest of inode, creation / modification time in ns, size, bytes>` = one id per
  write (also for two pauses within one second). `modes.manual_pause(root) -> {pause_id, kind: mode | pause, reason} | None` names the
  manual pause of conversions in force. Exposed in `GET /api/v2/state` `status.pause_id`, the queue's `mode.pause_id` (and
  `mode.mode.pause_id`, `mode.pause.pause_id`), `monsters.gate.pause_id` and `item.conversion.pause_id`. A confirmed Run now must echo
  it (H.4); `args.override_pause` = that id opens the gate for that pause only.
- **F8 paid accounting (lane side).** An action is `confirmed` ONLY when the backend recorded `actions[<kind>].confirmed_at`
  (`monster_lane.advance_evidence`); a cleared intent, an observed stage or an exported record prove nothing (`uncertain`). Per action the
  lane reads `observed_cost` (else `observed_cost_<kind>.credits`), `provider_action_id`, `model_id`; `paid_log.jsonl` entries gain
  `provider_id` and `evidence`. An unconfirmed click keeps `pending_action` (the lane refuses to resume it) and the item shows
  `conversion.record.paid_review`. A paid-log write error stores the actions it could not log in `paid_log_errors.json`
  (`actions: [{id, provider_id, action, status, credits, model_id, evidence}]`, `model_id`), so `repair_accounting` logs them even after
  Reconvert moved the record, and never mixes in another model's record found at that path; an error whose record is gone and that
  carries no stored action stays listed. Before a paid collect the lane also re-hashes the final upload copies
  (`rodin_backend.verify_upload_inputs`; a difference = verdict failed `check`).
- **F9 process tree.** The driver process (`monster_lane.py run`, `run_conversion(own_process=True)`) creates a named Windows Job
  Object (`Local\imageto3d-mlane-<variant key>-<uuid>`, default limits: KILL_ON_JOB_CLOSE off) and assigns ITSELF before starting any
  child, so every descendant is in the job. The children file (`<variant key>.children.json`, schema 2) records `driver`, `job`,
  `children` (written BEFORE each spawn: `reserve`; a spawn that cannot be recorded is not made), `descendants` (every descendant as it
  appears, a 0.5 s watcher) and `incomplete` (why the record may not be the whole tree). The orchestrator opens the job by name the
  first time it looks at the tree (every tick, `_monster_tree_hold` / the plan) and keeps the handle, so it owns the tree through the
  driver's exit: `tree_state -> {job, live, incomplete}` (the job's pids when it holds the handle, else the recorded processes). The slot
  is held while anything of the tree lives OR the evidence is incomplete after the driver is gone (`tree_hold`; visible as the waiting
  reason / a note; cleared by hand after checking). Kill & retry (`Orchestrator._kill_monster`): 1. ownership of wrapper and builder
  (`_ownership`: gone | verified by pid + recorded create time + command line | unverified) BEFORE anything is enumerated or signalled;
  unverified = no signal at all, `kill_failed`, slot kept; 2. `tree_plan`: the job, else only processes proven by pid + create time
  (a recorded live process without a create time blocks every signal); 3. builder, wrapper, then `drain_tree` (TerminateJobObject or
  kill + wait); a survivor or incomplete evidence = `kill_failed`.
- **F10 approve what is on screen.** monster3d.js keeps `applied = {level, url, sha}` of the last load the viewer APPLIED; Approve
  (button and key A) is enabled only when that level is the selected one and has a digest; while loading and after a failed load (the
  stale mesh is cleared) it is disabled. Server: an approved variant is Completed only while `approval_problem` is None: the chosen
  LOD's file still has `chosen_lod.json` `glb_sha256` and its `generation` is the current one (an approval without a digest counts only
  for a legacy reduction); otherwise it is shown in 3D with `approved, but not verified (...)` and Unapprove.
- **F11 strict reads.** `mv.read_strict` (None only for a missing file). The publication journal (`read_journal`), the reconvert journal
  (`read_reconvert_journal`, shape-checked) and chosen_lod.json (`read_choice`; also in `decisions.write_monster_lod`) are read strictly
  on every mutation path; `_variant_paths` runs `damage_check` first. A malformed file is 409 `unreadable`, is never overwritten or
  removed (`_write_journal` refuses an existing file), and makes the variant a read-only damaged card (a monster with a damaged pick
  journal: its concept cards lose their actions and carry a `damaged: ...` badge).
- **F14 nested containment.** `mv.contained` is called again immediately before use for: the driver's `hyper3d/`, its record, the input
  snapshot `inputs_<generation>/` (and its temp folder and files), the backend's `upload_inputs/`, `reduced/` + `v2_reduce_ok.json`
  (`_drive.unsafe`, before submit, collect, reduce and the verification); Reconvert's lock, journal and every move source / destination;
  the recovery of a reconvert journal (before its lock file is opened, and per move); 3D approve's lock, choice file and LOD file;
  Retry / Repair renames. A link or junction on the way = refused (verdict failed `check` / 400).
- **F15 one validator, repair, one terminal rule.** `mv.png_problem` = `png_check.png_problem`, provenance =
  `png_check.provenance_problem` (freeze, convert, Retry, the ticks). View items gain `repair: "provenance" | null`: a valid image whose
  record is missing / not a JSON object is repaired by verb `view_repair {view}` (the record is written again from the task
  descriptor's `prompt`, else the last job's `prompt.md`, else v2's prompt for the view; `repaired: {...}` says so; an unusable record is
  renamed `.invalid-<time>`; the image is not touched) and `view_retry` refuses it. The runner then marks its
  `failed` / `reason: provenance` task done itself. `view_waiting` is the one terminal-state rule of the item phase AND the unattended
  freeze: a task of the variant is terminal, or there is no descriptor and a valid view exists.
- **Row 10 old descriptors.** `variant_descriptors` finds every descriptor of a variant (current names, the older undigested
  `mviews--<monster slug>--<variant>--<view>` names, and any descriptor whose `monster` / `variant` fields name it); withdraw checks
  and renames them all. `reconcile_descriptors` (every tick, before the lane dispatches anything) renames a monster descriptor that has
  no guard or an old name to `<name>.json.unguarded-<time>` unless it is launching / running (reported), and writes the current guarded
  descriptor in its place when the view is still planned and missing. This is the APP-side refusal; the runner itself
  (tools/view_batch.py `launch_withdrawn`) still launches a descriptor without a `guard` field, so between two app ticks an unguarded
  descriptor written by hand could start: the app must tick once before the runner is enabled (cut-over order).
