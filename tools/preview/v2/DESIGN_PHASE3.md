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
- It bumps the rev and sends the set back to aligned. While `aligned_approval` exists, a changed amount makes Completed show an
  "amount changed since approval" badge; the user approves again.

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
