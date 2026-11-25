# Preview app v2 - phase 3 contract (3D approval, layer builds, Aligned, Completed, Mix, Queue v0)

Extends `DESIGN.md` §4 "Phase 3+". The orchestrator principles are in `DESIGN_ORCHESTRATOR.md` §2. The user's decisions are in
`PIPELINE_V2_PLAN.md` §13:
- one Approve covers the whole set;
- Amount: armour has body + limbs per race, outfits have one per race;
- layer builds run one at a time while the user is at the PC, in parallel when away.

## A. Backend (tools/preview/v2/)

### taskqueue.py
SQLite at `.codex_jobs/_app/queue.sqlite3`, using the schema in DESIGN_ORCHESTRATOR §2. Do not name it `queue.py`: that shadows the
stdlib module.
- API:
  - `enqueue(lane, key, set_name, unit, args, priority=5, max_attempts=3) -> id`. Idempotent per key: a queued, running or done
    task with that key is returned unchanged; a failed one is returned unchanged unless `retry=True`.
  - `next_queued(lane, n)`, `mark_running(id, pid, create_time, log)`, `finish(id, ok, result, error)`, `retry(id)`,
    `cancel(id)`, `tasks(filter...)`, `events(after_id)`.
- Thread-safety: a single module-level lock. The orchestrator thread is the only one that starts or finishes tasks; HTTP threads
  only enqueue, retry or cancel.

### run_task.py
`python tools/preview/v2/run_task.py <task json path>`:
1. Read `{id, argv, env, log, result}`.
2. Run `argv` with output appended to `log`.
3. Write `result` atomically as `{exit, last_json, finished}`.

`last_json` is the last stdout line that parses as JSON. The orchestrator starts this wrapper detached
(`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`).

### orchestrator.py
A thread started by `server.py` (unless `--no-orchestrator`). It takes a single-instance lock at `.codex_jobs/_app/orchestrator.lock`
(msvcrt byte lock, as `pipeline_supervisor.lock_try` does). If another server already holds it, this server runs read-only and the
UI shows a banner.

**Startup.** Adopt running tasks: the PID is alive and its create_time matches. Anything else in state `running` whose result
file exists is finished; one without a result file is failed with "lost".

**Loop (every 3 s):**
- **Finish.** Read result files of running tasks and finish them. On failure with attempts below max and an error that isn't
  `invalid input`, re-queue it with a 60 s backoff.
- **Start.** Start queued tasks while `running < limit(lane)`.
  - `limit('bare')` and `limit('layers')` share one pool: `settings.layer_workers_present` (default 1) while the user is present,
    `settings.layer_workers_away` (default 4) when away.
  - The limit is 0 when the supervisor mode is `pause`.
  - Presence: `C:\Users\niall\.claude\presence.json` when `updated_unix` is under 30 s old, using `user_idle_seconds >= 300` for
    away; else `pipeline_supervisor.win_idle_seconds()`.
- **Dependencies.** A `layers` task waits until the `bare` task of its race is done, or until a current bare build exists.
- **Task argv:**
  - layers: `[sys.executable, 'tools/layers/build.py', 'module', <module_dir>, '--lod', <lod>]`
  - bare: `[sys.executable, 'tools/layers/build.py', 'bare', <race>]`
  - env: `BODY_FIT_GPU=1`, `PYTHONUTF8=1`, `OMP_NUM_THREADS=1`
- **Stale scan.** Every 10 min, and at start, run the stale scan described below.

### Decisions and verbs (`set:S`)
**`approve` in 3d**, with `rev`:
1. Every required unit is reduced or dropped, otherwise 400 listing the missing units.
2. For each unit without a saved `chosen_lod.json`, write the LOD the client shows: `args.lods` = `{"race/module": lod}` from the
   client, and the server checks it is one of the unit's levels.
3. Snapshot `lod_approval` = `{time, units: {"race/module": {job, lod, glb_sha256}}}`.
4. Enqueue bare tasks for the races of the set, and layer tasks per unit.

Task keys:
- `layers:S:race/module:lod:job:<glb sha12>`
- `bare:race:<body npz sha12>`

Response: `{ok, undo: {verb: 'unapprove', id, args: {to: '3d'}}}`.

**`unapprove`:**
- In 3d while building, or in aligned (`args.to: '3d'`): clear `lod_approval` and cancel queued layer tasks of the set. Running
  tasks finish and their builds are kept.
- In completed (`args.to: 'aligned'`): clear `aligned_approval` and delete `ready.json` (rename it to `ready.json.revoked-<time>`).

**`approve` in aligned**, with `rev` and `args.amount` = the full amount map the client shows:
1. Every approved unit has a published build.
2. Write `aligned_approval` = `{time, units: {"race/module": {job, lod, build_id}}, amount}` and the `amount` decision.
3. Write `models/S/ready.json`. Its schema is in DESIGN.md §3. Slots carry relative paths and sha256 of the GLBs in the build dir.
   `bare` maps each race to the relative dir of the bare build used.

**`rebuild`** (`args.race?`, `args.module?`): re-enqueue with `retry=True` for failed tasks. Stale units get a new key, because the
fingerprint changed. A current build stays current; `build.py` returns `current` quickly.

**`POST /api/v2/amount`** `{id, race, group, value, rev}`:
- The group must match the kind: outfit sets use `outfit`, armour sets use `body` and `limbs`.
- `value` is an integer from 0 to 100.
- It bumps the rev (a value equal to the saved one writes nothing and keeps the rev). **Backend as built:** it does NOT move the set to
  another stage; while `aligned_approval` exists, a changed amount keeps the set in Completed with the card badge "amount changed since
  approval" (`item.card.badges`), and the user approves again from there (`approve` is accepted in aligned AND completed). It needs the 3D approval
  (400 otherwise).

**Build status per unit**, added as `item.builds["race/module"]`:
```json
{"status": "published|building|queued|failed|stale|none", "build_id": "0p2-abc...", "lod": "0p2",
 "slots": {"torso": "/files/models/.../layers/<build_id>/torso.glb", "...": "..."}, "warnings": [], "metrics": {},
 "error": "last_failure.json text tail or task error", "task": {"id": 12, "state": "running", "elapsed": 31.0}}
```
- **published**: `layers/current_<approved lod>.json` (one pointer per LOD, written by tools/layers/build.py under its publish lock;
  phase 2 review fix) names a valid ok build. There is no single `current.json` any more.
- **stale**: published, but the stale scan found the fingerprint is no longer current.
- `item.bare[race]` = `{slot: url}` from the bare pointer in `tools/preview/cache/layers/<race>/` (see tools/layers/README.md).
- **Stage rule 3** (DESIGN.md §2): the set stays in **3d** with a "building n/m" badge (tone info) or "build failed n" (tone error)
  until every approved unit is published or stale.
- **Completed** requires that `aligned_approval` build ids equal the current published ids.

**Stale scan.** Uses `build.fingerprint(module_dir, lod)`; expose it in build.py if it doesn't exist. Cache it in memory keyed by
the mtimes of the inputs. Only published units of sets in 3d, aligned or completed are scanned.

### GET /api/v2/layers?race=R
```json
{"race": "human_male", "bare": {"head": "url", "torso": "url", "lower_arm.l": "url", "lower_arm.r": "url",
                                "lower_leg.l": "url", "lower_leg.r": "url"},
 "layers": [{"set": "S", "title": "...", "kind": "armour", "module": "right_gauntlet_boot", "slot": "lower_arm.r",
             "url": "...", "stage": "aligned|completed|3d", "build_id": "...", "amount": 100}]}
```
- Lists every published build of race R in sets in 3d (building), aligned or completed.
- `amount` is the set's saved amount for the group of that slot: torso → body, lower_* → limbs, outfit → outfit; default 100.
- Cached per race by mtimes.

### GET /api/v2/queue
`{"tasks": [{id, lane, set, unit, state, attempts, started, finished, elapsed, error, log_tail}], "workers": {"limit": n,
"running": n, "present": bool}}`. It lists running and queued tasks, plus failed and done tasks from the last 24 h, newest first.

`POST /api/v2/queue {op: "retry"|"cancel", id}`. Phase 4 extends both.

## B. Frontend (tools/preview/web/)

### 3D modal
- **Approve** (key A) opens a confirmation listing the race × module LODs, then sends `approve` with `args.lods` for the unsaved
  cells.
- After approval the modal shows a progress bar "Building layers n/m". The set card stays in 3D with its badge. Failed units show
  their error tail and a **Retry** button (`rebuild`).

### Aligned modal
`aligned.js`, ported from `experiments/shell_slots_20261122/out/layers/mix.html` and using the shared `viewer.js`.
- **Top bar:** race select, ◀ ▶ through the filtered Aligned list, and the set title.
- **Composition:** always the bare head, then either the outfit layer, or torso + lower_arm.l/r + lower_leg.l/r. Missing slots
  fall back to the race's bare layers. Gnomes have no leg slots.
- **Amount sliders**, 0 to 100, set from `item.decisions.amount[race]` (default 100), and they drive the morph `fill` weight live:
  - outfit sets: one slider for the outfit layer;
  - armour sets: two sliders, **Body** (torso layer) and **Gloves & boots** (lower_* layers of THIS set).
  - Values save on release with POST amount.
- **Gloves** and **Boots** dropdowns, each with ◀ ▶:
  - options: "Own" (this set, the default), every other set's published `lower_arm.r` / `lower_leg.r` layers for this race from
    `/api/v2/layers`, and "Bare";
  - choosing a set loads both sides (`.l` and `.r`) of that slot;
  - **Own pieces** resets both dropdowns;
  - donor layers use the donor's own saved limbs amount and do not follow this set's slider.
- **Hold Space** to show every layer at Amount 0. Releasing restores the slider values.
- **Toggles:** red backs (default on), wireframe, colour by layer.
- **Notes panel:** per loaded layer, its slot, set and faces, plus build warnings and metrics (wall crossings left, boundary edges).
- **Buttons:** **Rebuild** (`rebuild`), **Back to 3D** (`unapprove` `to: 3d`), and **Approve**. Approve sends `approve` with the
  amount map of every race and moves on to the next set.
- **Camera:** carries over between sets and races.

### Completed modal
The same viewer, with no sliders except a read-only display of the approved amount. It has **Unapprove** (to aligned). If the
saved amount differs from the approved one it shows "amount changed".

### Mix tab
A free composition over `/api/v2/layers`, like mix.html:
- race select, then outfit / armour body select over all published layers;
- gloves, boots, random, and same set;
- one global Amount slider, preview only, never saved;
- red backs and wireframe toggles.

### Queue tab, v0
A table of `/api/v2/queue` tasks with state, elapsed time, error tail and Retry / Cancel. It refreshes every 3 s while visible.
Phase 4 adds services, modes and credits.

## C. Compatibility with the phase 1 review fixes (DESIGN.md section 6)
- **Approve in 3d** snapshots `lod_approval` = `{time, units, required_races}`: `required_races` = the set's `required_races` at that moment
  (DESIGN.md section 2); the catalog uses that frozen list from then on. It runs under `decisions.set_lock` and writes through
  `decisions.mutate` (rev check, history, cross-process lock); enqueueing tasks happens after the write.
- A LOD write is refused (409 `locked`) once `lod_approval` exists, so the "unsaved cells" the client sends in `args.lods` are the only LOD data the
  approve may add; it must write those `chosen_lod.json` files and `lod_approval` in ONE `guard.apply_transaction`.
- `unapprove` of a set already exists in phase 1 (`decisions.send_back`): phase 3 only adds the queued-task cancel and the `ready.json` revocation to
  `actions.set_action`. `pin_missing` units block both approve verbs (400).
- The orchestrator thread starts only in a server that holds `.codex_jobs/_app/server.lock` (`server.ctx['read_only']` false); a read-only server
  also skips `orchestrator.lock`.
- Layer builds read the model of a unit from `unit.model_set` (the physical dir), never from the card's set name.

## D. Backend as built (phase 3, `tools/preview/v2/`)
**Superseded in parts by sections F and G** (phase 3 review and re-check fixes): the approval request shapes (D.2, D.5), task states and attempts (D.3), the
Completed rule and build statuses (D.4), the queue endpoint and ops (D.7). Where D and F differ, F is the contract.
Where the backend settled a detail the contract left open (the frontend can rely on all of it):
1. **Files.** `taskqueue.py` (SQLite WAL queue; `for_root(root)`), `run_task.py` (detached wrapper), `orchestrator.py` (thread, adoption, pool,
   stale scan, `queue_state`), `builds.py` (pointers, URLs, fingerprint cache, task keys, per-unit status), plus `decisions.approve_3d /
   approve_aligned / set_amount`, `actions.approve_3d / approve_aligned / rebuild_set / set_amount / queue_op`, `catalog.layers_for_race`.
   The `tasks` table has one extra column, `not_before REAL` (the retry backoff). `create_server(..., run_orchestrator=False)`: only
   `server.py main()` turns it on (`--no-orchestrator` turns it off); the state's `server.orchestrator` says whether this server runs it.
   A writer server that cannot get `orchestrator.lock` becomes read-only with a banner message.
2. **`approve` in 3d** (`rev` required): `args.lods` = `{"race/module": lod}` for the units with no saved pick (400 when one is missing; an
   unknown unit or an invalid LOD is a 400; a lod that contradicts a SAVED pick is **409 `code: lod`**), optional `args.jobs` =
   `{"race/module": job}` (the conversions the client displayed; mismatch = 409 `code: job`). A set that is already approved is **409
   `code: approved`**. Units that are not reduced / dropped, or a set still in `approved`, are a 400 that lists them (`race/module (status)`).
   Response `{ok, rev, stage, queued: <units>, undo: {verb: "unapprove", id, args: {to: "3d"}, rev}}`. The snapshot is written by
   `decisions.approve_3d` in ONE `guard.apply_transaction` (the unsaved `chosen_lod.json` files + `pipeline_v2.json`); tasks are queued
   afterwards, still under the set lock; if queueing fails the approval is cleared again (400 "rolled back").
3. **Task keys / queue rules.** Layers `layers:<S>:<race>/<module>:<lod>:<job>:<glb sha12>`, bare `bare:<race>:<game body npz sha12>`; a
   `rebuild` of a stale / unpublished unit appends `:<fingerprint[:12]>` (a done task whose build is no longer current gets that new key).
   Approve and Rebuild retry failed tasks of the same key (fresh attempts) and re-queue cancelled ones. **Bare dependency:** a layers task
   waits while a bare task of its race is queued or running and no CURRENT bare build exists; a FAILED bare task does not block the layers
   (they build independently; Aligned approval then reports the missing bare layers). Bare tasks have `set_name` null, so `unapprove` never
   cancels them. Automatic retries: 2 (60 s backoff) for a lost task, `inputs changed during build`, `retryable` results, crashes without a
   verdict and error texts matching `PermissionError|timed out|MemoryError|out of memory|device lost|GPU|CUDA|Errno|busy|lost`; none for
   `missing input|no garment found|required slot|invalid race|unknown module|argument error|must be one of|module dir not found|...`
   and for any other build error text (deterministic).
4. **`item.builds["race/module"]`** exists for every approved, reduced, non-dropped unit: `{status, build_id, lod, slots: {slot: url},
   warnings, metrics, error, task: {id, state, elapsed}|null}`. `status`: published and stale win over a running / queued rebuild (the
   old build stays visible, `task` shows the new one); without a published build: `building` (task running), `queued`, `failed` (newest
   task failed; `error` = `layers/last_failure.json` text when it is about this unit, else the task error), `none` (no task, e.g. cancelled).
   `item.bare` = `{race: {slot: url}}` for the live races (only when approved); `server.orchestrator` in `/api/v2/state`.
   Card badges: `building n/m` (info), `build failed n` (error), `not built n` (warn), `stale n` (warn), `amount changed since approval` (warn).
   A unit whose approved conversion is `pin_missing` has no build and does not count for rule 3.
5. **`approve` in aligned / completed**: the set must be in `aligned` (or `completed`, to approve again after an amount change); 400 when a
   unit has no published build, a pin is missing, or a race has no bare build. `args.amount` = `{race: {group: 0..100}}` (missing values
   keep the saved amount, default 100); the merged map is stored in `aligned_approval.amount` AND `amount`. Response `{ok, rev, stage:
   "completed", undo: {verb: "unapprove", id, args: {to: "aligned"}, rev}}`. `ready.json` paths (`races.<race>.units.<module>.slots.<slot>.glb`,
   `bare.<race>`) are **relative to the project root** (posix); the bare dir is `tools/preview/cache/layers/<race>/<bare build id>`.
   `unapprove` clears `aligned_approval` in every case and renames an existing `ready.json` to `ready.json.revoked-<time>`; response adds
   `{cancelled: <queued tasks cancelled>, revoked: <file name>|null}`.
6. **`rebuild`** response `{ok, queued: n, actions: {"race/module" | "bare:<race>": "queued|retried|running|current|failed"}}`; 400 when the
   set is not approved in 3D or no unit matches the filters. It writes no decision (the `rev` must be present but is not compared).
7. **`/api/v2/queue`** adds `key` and `max_attempts` per task and `workers: {limit, running, present, mode, idle, orchestrator}`; `POST`
   `retry` needs a failed / cancelled task, `cancel` a queued one (a running build cannot be stopped from here yet); errors are 400, an unknown
   id 404.
8. ~~`/files/tools/preview/cache/layers/...` served from the real repo for a fixture root~~: removed by the re-check (section G.1, R13).
9. **Stale scan** (`orchestrator.scan_stale(root)`, every 10 min and at start, in its own thread) writes `.codex_jobs/_app/stale.json`
   (`{time, units: {"S/race/module": {build_id, lod, fingerprint, current}}, bare: {race: ...}}`); the catalog reads it (also in a read-only
   server). Only published units of locked sets are scanned; fingerprints are cached by the mtimes of their inputs.

## E. Phase 3 review fixes: what the frontend sends and expects (findings 1, 12-15)
The frontend was changed first; the backend has to accept and CHECK these shapes. Fixture server for the browser tests:
`scratchpad/v2_fixture_p3/fixture_server_p3.py` implements them and answers 409 when they differ; the flows are in `flows3.mjs`.

### E1. Approve in 3D (`POST /api/v2/action {id: "set:S", verb: "approve", rev, args}`)
```json
{"rev": 4, "args": {
  "units": {"human_male/body_armor": {"job": "9ba3b6ee4095", "lod": "0p2"}, "...": "ONE ENTRY FOR EVERY UNIT THE CONFIRMATION SHOWS"},
  "lods":  {"human_male/body_armor": "0p2"}}}
```
- `units` = the conversion (`job`) and LOD the reviewer SAW in the confirmation, for every live (non-dropped) unit, saved or not. It is the contract
  for what gets approved: compare it, under the set lock, with the job the catalog resolves now and the LOD that would be frozen (the saved pick, else
  the unsaved default). The key set must equal the live unit set exactly.
- `lods` (unchanged from section D.2) = only the cells whose LOD was never saved: the units whose LOD the server must write with the approval. For
  those cells it always equals `units[key].lod`; nothing else about the units is sent.
- **409** `{ok: false, error, code: "changed", changes: ["race/module: job a -> b, lod 0p2 -> 0p1", ...], item}` when any entry differs (newer
  conversion, another saved LOD, a unit added or removed). The client also treats `code` `rev`, `job`, `lod` (and no code) as "reload and look again".
  On such a 409 the client KEEPS the dialog open, reloads the item (`GET /api/v2/item`), redraws the table with the changed cells highlighted and the
  differences listed (computed by the client from its snapshot; the `changes` strings are shown only when the client finds no difference itself) and
  needs a fresh confirm. Any other 409 code (`approved`, `locked`, `unreadable` ...) closes the dialog and reloads. Nothing is written on a 409.

### E2. Approve in Aligned (`verb: "approve"`, set in `aligned`)
```json
{"rev": 9, "args": {
  "builds": {"human_male/body_armor": "0p2-869db65548b6", "...": "EVERY unit of EVERY race of the set"},
  "bare":   {"human_male": "bare-697f1bde9abc", "...": "every race of the set"},
  "amount": {"human_male": {"body": 100, "limbs": 100}, "...": "every race"}}}
```
- `builds` = the published build id shown for each unit (the id the reviewer's composition was made of); `bare` = the id of the bare build shown per
  race (the directory name under `tools/preview/cache/layers/<race>/`).
- Compare both with the CURRENT published ids under the lock and refuse stale builds / stale bare builds: **409** `{code: "changed" | "stale",
  changes: [...], item}` (`stale`: a unit or bare build is published but the stale scan found it out of date; `changed`: an id differs or the key
  sets differ). The client then reloads, lists what changed (build and bare ids, old -> new) in the layer notes and STAYS on the set (it advances only
  after a 200). Nothing is written on a 409. A missing `builds` or `bare` is a 400.
- The Approve button is disabled until every unit of every race has a published build, the bare layers exist, AND the composition on screen is
  complete (the bare head and the set's own slots all loaded, no load failure, own pieces selected - not a donor's gloves).

### E3. Item additions the frontend reads (requested)
- `item.bare_builds = {race: {status: "published|stale|building|queued|failed|none", build_id, error?, task?: {id, state, elapsed, error?}}}`. When it
  is missing, the bare build id is parsed from the bare layer URL (`/layers/<race>/<build id>/<slot>.glb`) and no bare status is shown. A stale or
  failed bare build is shown as a warning in the Aligned notes.
- `build.task.state` is one of `queued | running | stalled | failed`; `build.task.error` for a failed one. The Aligned viewer polls the item every
  2.5 s while ANY build or bare build of the set has a task in `queued | running | stalled` or a status of `queued | building`, shows
  `rebuild failed - <error>` for `task.state == "failed"` next to a still-published older build, and re-reads the layer files when a published
  `build_id` (and so the file URLs) changes.
- A queue task `state` may be `stalled` (a running task past its progress deadline).

### E4. Queue operations the frontend sends (`POST /api/v2/queue`)
| body | meaning | response |
| --- | --- | --- |
| `{op: "kill_retry", id}` | for a `stalled` task: kill the wrapper AND the builder, then queue a fresh attempt (400 when the task is not stalled) | `{ok: true}` |
| `{op: "rebuild_stale"}` | **chosen instead of a per-set action**: re-queue the rebuild of every stale published layer build (and stale bare build) of all sets in 3d / aligned / completed, i.e. what `rebuild` with `args: {stale: true}` would do for each set; no `id`, no `rev` | `{ok: true, queued: <n builds>, sets: [<set names>]}` |

Queue tab: **Rebuild stale** is enabled while a card of the 3D / Aligned / Completed tabs carries a `stale n` badge; **Kill & retry** is shown on
rows in state `stalled`.

### E5. State addition
`GET /api/v2/state` may carry `queue_health`: `null` / `true` / `{ok: true}` when healthy, otherwise a string, a list of strings, or
`{ok: false, errors: [...]}` (`error` / `message` are also accepted). The errors are shown in a banner on every tab and at the top of the Queue tab.

### E6. Keyboard and sliders (client-only rules)
- No mutation shortcut (A, Enter, P, Esc-reject) acts on a repeated key event, or on a key that already fired one before it was released
  (`modal.js` `mutationKey`); opening a confirmation or advancing to another card spends the key. Aligned approve advances only after the 200.
- Amount sliders are bound to the set and race they were drawn for and are disabled synchronously when the race changes; a release on a stale slider
  never POSTs. `POST /api/v2/amount` bodies keep the shape of section A.
- A layer that fails to load removes the mesh it was to replace; the Aligned notes name slot, URL and error; a bare fallback is labelled
  "bare (fallback)".

## F. Phase 3 review fixes: backend as built (findings 1-11, 16, 17; supersedes section D where they differ)
Section E is the frontend's side of the same contract; this section is what the backend checks. Findings 12-15 are frontend-only.

1. **3D approval binding (F1).** `args.units` = `{"race/module": {job, lod}}` for EVERY live unit (400 when missing / malformed / a bad LOD) and
   `args.lods` = the never-saved cells (each must equal `units[key].lod`, else 400). Under the set lock: the resolved job and the saved-or-default LOD
   of every live unit must equal `units`, with exactly the same key set, and `lods` must name exactly the units that have no saved pick; anything else
   is **409 `changed`** with `changes: [text, ...]` (and the current `item`). Nothing is written on a 409. `args.jobs` is gone. The server writes the
   unsaved picks (the default LOD) in the approval's transaction. The tasks are queued after the transaction, best effort: an enqueue failure no longer
   rolls the approval back; the response then carries `queue_error` and the orchestrator's reconcile step queues the work (F7).
2. **Aligned approval binding + recheck (F1, F2, F4).** `args.builds` = `{"race/module": build_id|null}` for every live unit, `args.bare` =
   `{race: bare build_id|null}` for every live race (400 when either is missing), `args.amount` = the FULL map (every live race, exactly the kind's
   groups; another race set is 409 `changed`, a missing / unknown group or a bad value 400). Order of checks under the set lock: rev; stage; a
   `pin_missing` unit (400); the displayed ids vs the published ids (409 `changed` + `changes`); the amount; a unit whose pinned source changed (409
   `stale`); unpublished units / missing bare builds (400); then, synchronously (~1 s per set), every module fingerprint and every bare fingerprint is
   recomputed (uncached) and every approved source GLB rehashed: a stale build or a digest mismatch is **409 `stale`** + `changes`. ONE transaction then
   writes `aligned_approval = {time, approval_id (uuid hex), units: {key: {job, lod, build_id}}, amount, bare: {race: build_id}}`, the merged `amount`,
   and `models/<S>/ready.json` (with the same `approval_id`; an older ready.json is kept as a revoked copy). Every ready.json path is RESOLVED, checked to
   lie inside its build folder (slots) / the bare root (bare dirs), and written relative to the project root (`builds.rel_path` raises for anything
   outside the project; never an absolute path). Response adds `approval_id`.
3. **Completed (F3).** A set is `completed` only when (catalog.completed_problems is empty): `aligned_approval.units` key set == the live unit set (of
   the frozen required races) == `lod_approval.units` key set; every live unit's build is published (or stale) with exactly the approved build id; for
   every live race `aligned_approval.bare[race]` is the published bare build id; and `models/<S>/ready.json` exists with the approval's `approval_id`.
   The item lists the reasons in `not_completed`. `drop_race` / `undrop_race` answer **409 `approved`** while `lod_approval` exists (Back to 3D first).
   The orchestrator's reconcile step (never a GET) revokes a ready.json whose set is not Completed: renamed to `ready.json.revoked-<time>` under the set
   lock (a guard transaction), queue event `revoke`, `tools/preview/actions.jsonl` entry `revoke_ready`.
4. **Pointer validation (F4).** A module pointer counts as published only when `builds.pointer_problem` is None: status ok; `kind`, `race`, `lod` and
   `module` equal the module's own (module = `build._rel(module_dir)`: the repo-relative path, or the absolute posix path outside the repo; exact
   equality, no suffix match); `required` and the slot map are exactly `build.required_slots(kind, race)`; the build folder is a real folder directly in
   the output root; the pointer equals that folder's `build.json`; every slot path is `<build_id>/<slot>.glb`, resolves inside the build folder and
   exists. Bare pointers the same (`kind: bare`, `module: bare:<race>`, `lod: null`). A damaged pointer shows `none`; the reconcile step queues a build,
   and the builder repairs the pointer from build.json under its publish lock (`current`).
5. **Source digest (F2).** Layer tasks carry `args.glb_sha256`; the builder runs with `--expect-source-sha256` (checked before building and under the
   publish lock: `source changed: ...`, never retried); the dispatcher compares the digest before starting (mismatch: the queued task is failed with that
   error). `item.builds[key].status = "source_changed"` when the approved LOD file's sha256 is no longer the pinned one (old layers still listed in
   `slots` for viewing; `error` says to send the set back to 3D); the unit gets `source_changed: true`, the card a `source changed n` (error) badge, the
   set stays in 3D (not built), `rebuild` answers `source_changed` for it and never queues it. A 3D-approved unit always shows the FROZEN LOD of
   `lod_approval` (`frozen: true`), never the mutable chosen_lod.json (phase 1 review #12).
6. **Attempts (F5, F6).** Task states: `queued -> starting -> running -> done | failed`, `queued -> cancelled`. Starting an attempt: (a) the
   approval is checked again (withdrawn: the queued task is cancelled; also on every automatic retry / lost requeue: a withdrawn task is failed, not
   requeued), (b) `claim` = `UPDATE ... WHERE state='queued'` (rowcount 1, else nothing starts) with a new uuid `token`, attempts + 1 and the attempt's
   files `logs/<id>-a<n>.log / .err.log / .result.json / .claim.json / .task.json` (n stays unique also after a manual reset), (c) the detached wrapper
   `run_task.py <task json> --token <token>` writes the claim file {token, attempt, task: {key, lane, set, unit, args, max_attempts}, wrapper_pid,
   wrapper_create} first and adds builder_pid / builder_create after starting the builder, (d) the row becomes running with the wrapper's pid. Adoption:
   a `starting` row with a live claim (or, without one, a live process whose command line has the token) becomes running; otherwise it is lost. Results
   and `finish` / `requeue` apply only to the row that still carries the attempt's token (a build finishes once). New columns: `token`, `builder_pid`,
   `builder_create`, `kill_requested` (added by ALTER TABLE to an older database).
7. **Verdict (F10).** The wrapper keeps stderr in `<id>-a<n>.err.log` and reads the verdict from STDOUT only. `done` needs exit 0, a verdict
   `ok` / `current` AND that verdict's build_id being the valid publication now (module pointer / bare pointer); exit 0 without a verdict is
   `failed` ("the builder exited 0 without a verdict on stdout", not retried); a verdict without its publication is failed (retried while attempts last).
8. **Stalled builds (F10).** Heartbeat = the mtime of the attempt's two logs. A starting / running attempt whose logs have not grown for
   `settings.json build_stall_minutes` (integer >= 1, default 30; the phase 4 settings module should accept the key) is shown as state `stalled`
   (queue rows: `state: "stalled"`, `stored_state`, `stalled: true`, `last_output`; `item.builds[*].task.state` / `item.bare_builds[*].task.state`).
   `POST /api/v2/queue {op: "kill_retry", id}` (400 unless the task is stalled) sets `kill_requested`; the orchestrator then kills the builder and then
   the wrapper, each by PID only after its create time and command line (builder: the claim's argv; wrapper: the token) were checked, and queues a fresh
   attempt (attempts reset; error "killed by the user (Kill & retry): ..."); with the approval withdrawn it fails the task instead.
9. **Queue = derived cache (F7, F9).** Reconcile (orchestrator start + every loop; ~0.1 s on the real 102-set catalog): (a) a damaged database
   (`file is not a database`, `malformed`, ...) is renamed `queue.sqlite3[-wal|-shm].corrupt-<time>`, a fresh one created, and every surviving
   wrapper (claim file, live wrapper, no result) re-inserted as a running row with its token; (b) interrupted file transactions are recovered (item
   10); (c) for every approved set: a bare task for each live race without a bare pointer, and a layers task for each approved unit whose build status
   is `none` (no valid publication, no live / queued task; failed units are NOT retried automatically, stale ones are NOT rebuilt automatically);
   (d) queued tasks without a matching approval (job, LOD and digest of `lod_approval`) and bare tasks no approved set needs are cancelled; (e) the
   ready.json revocation of item 3. Readers never raise on a damaged database: they return empty results and record `queue_health`;
   `GET /api/v2/state` has `queue_health` (null when fine, `{ok: false, errors: [..], corrupt, since}`, or `{ok: true, repaired: <kept file>}` after a
   repair), `GET /api/v2/queue` has `queue_health` (the raw health record). Unapprove is ONE transaction (pipeline_v2.json + the ready.json
   revocation, the rev checked before anything is renamed); its queued-task cancel is best effort (reconcile cancels the rest).
10. **Recovery (F8).** Every `files_tx` intent names its `set` and keeps, per file, `before_sha256` and `after_sha256`. Recovery (server start and every
   orchestrator loop) runs under the set lock and restores the backups only when every file holds either what the transaction wrote or its old content;
   otherwise nothing is touched and the intent is marked `conflict` (`conflict_files`): its Recovery card offers `complete` (keep the files as they are
   now) and `rollback` (restore the backups anyway). While a set has an unresolved intent every write of it (LOD, amount, approvals, unapprove, races) is
   **409 `recovering`**; the item has `recovering: [{id, label, conflict}]` and the card a `recovering: read-only` badge.
11. **Repair (F11).** `_ensure` revalidates the publication of every done key; a done fingerprint key whose publication is still missing / invalid is
   reset to queued (attempts kept, event `repair`), for module and bare builds alike. An already-queued key answers `waiting` (new action value in
   `rebuild` / reconcile output; `queued` now means "queued by this call").
12. **Queue totals (F16).** `GET /api/v2/queue` adds `totals` ({state: count} over the whole table), `workers.running` = SQL count of starting + running,
   `page` = `{open_limit, open_total, open_shown, recent_limit, recent_total, recent_shown, recent_since, truncated}`, `stalled` and `stall_minutes`.
   `POST {op: "rebuild_stale"}` (no id): for every approved set in 3d / aligned / completed, rebuild every published unit the stale scan marked stale
   and the stale bare builds of its races; response `{ok, queued, sets}`.
13. **Item additions.** `item.bare_builds = {race: {status: published|stale|building|queued|failed|none, build_id, error?, task?: {id, state, elapsed,
   error?}}}` for the live races of an approved set; `item.builds[*].task` gains `error` for a failed task and shows `stalled`; units gain `frozen` and
   `source_changed`; the item gains `not_completed` and `recovering`.
14. **Promotion id (phase 1 review #3, #4).** A legacy approve writes `approved/<set>/.preview-promotion.json` `{promotion_id}` (it travels with the folder
   into its job when claimed) and records the same `promotion_id` in the origin sidecar and the flat-move intent. Undo (`restore trashlegacy`) withdraws
   only the folder with that promotion id (a folder without a mark, or with another one, is refused: "a later promotion"). Recovery `complete` of an
   interrupted legacy approve also succeeds when the promotion was already claimed (a job folder `<set>--<id>` carrying that promotion id).
15. **Decision files (phase 1 review #10).** `pipeline_v2.json` validation covers nested records (dropped entries, amount values 0..100, approval units
   with job / lod / build_id, required_races, bare map, approval_id); invalid UTF-8 is an unreadable file too.

## G. Phase 3 re-check fixes: backend as built (R2-R13, server parts of R14-R16; supersedes D and F where they differ)
Review: `.codex_jobs/review_v2_phase3_recheck_20261125/result.md`. Frontend parts of R1 / R14 / R16 are done in `tools/preview/web/`.

1. **Bare layers belong to the project root (R13).** Output AND lookup root: `<root>/tools/preview/cache/layers/<race>/` (`builds.bare_root(root)`; on the
   real root this is the builder's default). The dispatcher always passes `--out` (bare: that folder; module: `<module_dir>/layers`). No fallback reads
   another root: `/files/` no longer serves the real repository's cache for a fixture root (D.8 is gone) and `builds.file_url` / `rel_path` raise outside
   the root (ready.json `bare.<race>` = `tools/preview/cache/layers/<race>/<build id>`, relative to the project root).
2. **Slot digests and publication = the builder's validator (R3).** The builder writes `size` + `sha256` of every slot GLB into build.json (= the pointer)
   at publication; `build.validate_build` requires and checks them (an older build without digests is invalid and is rebuilt as `-r<n>`). A pointer counts
   as published only when `builds.pointer_problem` passes, which now ends with `build.validate_build(folder, expect_id, expect)` (cached by the
   (mtime, size, inode) of build.json and every slot file; `fresh=True` uncached: the dispatcher's verdict check and the Aligned approval). A pointer that
   exists but fails is status **`damaged`** (`item.builds[*]` and `item.bare_builds[*]`, with `error`; card badge `damaged build n`, tone error); it counts
   as not built (stage 3d), reconcile queues its rebuild (the builder never reuses it). Certification: the Aligned approval re-reads every pointer uncached
   (bytes rehashed) under the publish locks (item 3) and writes the verified `sha256` into ready.json; Completed derivation uses the cached validation.
3. **One pointer snapshot; approval under the builders' publish locks (R2).** `builds.snapshot()` reads a pointer ONCE and returns (pointer | None,
   problem); `item.builds[*]` (status, id, slot URLs) and the item's bare URLs + `bare_builds` ids each come from one read per derivation
   (`info.bare_snaps`). The Aligned approval, after its checks and still under the set lock, takes `tools/layers/build.py`'s `.publish.lock` of EVERY
   output root it certifies (module layers folders + bare race folders, sorted path order, waiting up to 30 s, else 409 `busy`; the builder never takes a
   set lock), re-reads every pointer uncached, compares with the displayed `args.builds` / `args.bare` (any difference, damaged bytes, or a bare build
   republished since the fingerprint check: **409 `changed`** with `changes`), writes the decision + ready.json, then releases.
4. **ready.json bound to the approval (R4).** `aligned_approval.ready_sha256` = sha256 of the exact ready.json bytes written in the same transaction
   (`decisions.ready_bytes` / `ready_digest`). Completed also requires ready.json to exist, parse (an object with `approval_id`), carry the approval id and
   have exactly that digest (`not_completed`: `ready.json is not the manifest this approval wrote ...`). Reconcile, under the set lock, **quarantines**
   (renames to `ready.json.quarantine-<time>`, bytes kept, queue event `quarantine`, actions.jsonl `quarantine_ready`) an active ready.json whose
   decisions are unreadable, or whose aligned approval exists but whose bytes / approval id are not its own; any other no-longer-Completed set is
   revoked as before (`ready.json.revoked-<time>`). Reconcile output gains `quarantined`.
5. **Lost queue adopts first (R5).** Whenever the database is missing or empty (once per database identity) or was repaired, `Orchestrator.adopt_claims()`
   runs BEFORE reconcile queues anything: every attempt task file (`<id>-a<n>.task.json`) without a result whose claim names a live wrapper (identity
   checked), or, with no usable claim, whose token is on a live process's command line (`--token`), is re-inserted as a running row (key, token, args,
   `approval_gen`); a claim whose wrapper died while its builder lives is adopted as `orphan`. The attempt's task file `task` now carries `approval_gen`.
   Reconcile output gains `adopted`.
6. **Worker slots and Kill & retry (R6).** New task state **`orphan`** (`taskqueue.ACTIVE` = starting | running | orphan; OPEN adds queued): a dead
   wrapper whose builder (claim / row pid + create time) is still alive is not lost: the row becomes `orphan`, keeps its slot, shows `state: "orphan"`,
   `orphan: true` in the queue, and is lost / requeued only once the builder is gone too. `POST /api/v2/queue {op: "kill_retry", id, token}`: `token`
   (the attempt the client saw; queue rows carry `token` for active attempts) is REQUIRED (400 without, 409 `changed` when the row runs another attempt);
   allowed for a stalled or an orphan task. The orchestrator stops the builder, then the wrapper: a process is `gone` (not alive, or its pid now has
   another create time), `killed` (identity + command line verified, killed, confirmed gone), `unverified` (alive but not verifiable: not touched) or
   `survived`. Only when both are gone / killed is a fresh attempt queued; otherwise the row keeps its state and slot, `kill_requested` is cleared and
   `error` says why ("Kill & retry could not stop the attempt: ...", event `kill_failed`).
7. **Legacy promotion identity (R7).** `workflow.py promote --promotion-id <32 hex>` writes `.preview-promotion.json` into its STAGING folder before the
   atomic rename into approved/ and returns `promotion_id`; `actions.legacy_approve` passes it and refuses a promote result without it (it never writes
   into approved/ afterwards). Withdrawal (Undo `restore`, Recovery roll-back of an interrupted approve) needs POSITIVE evidence: the folder must carry
   exactly that promotion id; an entry without a promotion id (written before the fix) withdraws nothing and fails with "manual check needed ...".
8. **Persistent Cancel (R8).** A Queue Cancel stores `cancel_gen` on the row (layers: the set's current 3D approval generation; bare: `manual`). Reconcile
   never re-queues a row cancelled for the current generation; the unit's build status is **`cancelled`** (counts as not built; no automatic work).
   Retry (queue), Rebuild (set) and a new 3D approval clear it. Automatic cancels (unapprove, reconcile, dispatcher) leave `cancel_gen` empty, as before.
9. **Builder adoption rechecks (R9).** See tools/layers/README.md "Publishing": every adoption path rechecks the source digest and fingerprint under the
   publish lock; pointer repair tries the generation the damaged pointer names first, then the newest valid `-r<n>`.
10. **Digests on pins (R10).** `decisions.approve_3d` refuses a unit without a 64-hex `glb_sha256`; validation rejects a malformed one (unreadable card)
   but accepts an absent one (an older pin): such a unit's build status is **`needs_review`** (`unit.needs_review`, card badge `needs 3D review n`, error
   tone; `rebuild` answers `needs_review`), it is never queued or built, and the set needs Back to 3D. Reconcile handles each set on its own: an exception
   is logged (`out.errors`, orchestrator `errors`) and the pass continues.
11. **Full unit identity (R11).** `lod_approval.approval_id` (uuid hex, new in every 3D approval; older approvals use their `time`) is the approval
   GENERATION (`decisions.approval_gen`). Task rows carry `approval_gen` (set when `_ensure` queues / re-queues / retries the row; a waiting queued row is
   re-tagged). `item.builds[*].task` / status and Retry match the FULL identity: set, unit, job, LOD, source digest AND generation (`builds.task_matches`);
   an older generation's attempt never shows on a newer approval, and its failed row is retried for the new approval by reconcile. The dispatcher's
   approval check also refuses a row tagged with another generation. A running attempt of an older generation with the SAME key (same inputs) is not
   re-tagged: its success publishes the identical build, its failure is not shown and the row is retried for the new approval.
12. **Pre-fix intents (R12).** `guard.intent_set(rec)`: the intent's `set`, else the set of the decision file it recorded (`models/<set>/pipeline_v2.json`
   or `models/<set>/ready.json*`). Used by `set_intents` (409 `recovering`), recovery (set lock, the same conflict guard: an old intent without
   `after_sha256` whose file changed is a conflict), the item's `recovering` and the Recovery card.
13. **Stale bare builds (R14, server).** `build_counts.stale_bare`; the card badge `stale n` counts stale units + stale bare races; `GET /api/v2/state`
   adds `stale = {units, bare: [race], sets: [set]}` (what Rebuild stale would rebuild); the stale scan covers the bare build of every live race of every
   approved set in 3d / aligned / completed; `rebuild_stale` rebuilds a stale bare race even when no unit is stale.
14. **Approval payloads (R15, server).** 3D: `args.lods` keys must be EXACTLY the never-saved live units (an extra or unknown key: 409 `changed`).
   Routing is by payload: `args.builds` / `args.bare` -> the Aligned approval, `args.units` -> the 3D approval, whatever the stage became; an Aligned
   payload for a set no longer in aligned / completed is **409 `stale`** (a pinned source changed / needs review) or **409 `changed`** (`stage: aligned
   displayed, <stage> now`), never 400.
15. **Queue listing (R16, server).** Rows: EVERY active attempt (starting / running / stalled / orphan; never truncated) first, then queued (newest first,
   at most `open_limit`), then the recent terminal rows. `page` adds `active_total`, `active_shown`, `queued_total`, `queued_shown` (`open_shown` =
   active + queued shown); totals unchanged. Rows add `orphan` and `token` (active attempts only).
