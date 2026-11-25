# Preview app v2 - orchestrator design (phases 3-5)

Companion to `DESIGN.md`. Decisions, rationale and contracts for the work the server runs.

## 1. Principle: adopt the existing daemons, add only what is new
The pipeline already runs as three robust, restart-safe daemons:
- **supervisor** (`tools/pipeline_supervisor.py run`). It claims `approved/` sets, runs gate 1 (other-race fronts) and gate 2
  (views ready -> `multiview_ready/`), and handles the idle/PAUSE authority.
  - Modes live in `.codex_jobs/_batch/supervisor_mode.json` (`auto`, `views-run`, `pause-conversions`, `pause`, `run`).
  - The `PAUSE` file has a scope. While paused it freezes the heavy descendants of a conversion.
  - It holds a single-instance lock.
- **view runner** (`tools/view_batch.py run --parallel 8`). It runs Codex jobs from `.codex_jobs/_batch/tasks/*.json` and
  `status/*.json`, and adopts running jobs through their `.done` markers.
- **convert worker** (`tools/convert_queue.py --sets-file ... --worker 1 [--no-tripo]`). It does the Tripo balance check, falls back
  to Hyper3D, runs `convert_job.py`, then `finish_sets`, then offload.

v2 does **not** rewrite them. The server becomes their **process manager**, gives them a UI, and adds the new lanes:
**layer builds** (phase 3) and **monster views and conversion** (phase 5).

### Service manager (`v2/services.py`)
- Service table (name, command line, matcher):
  - supervisor: `python -X utf8 -u tools/pipeline_supervisor.py run`
  - views: `python -u tools/view_batch.py run --parallel 8`
  - convert: `python -X utf8 -u tools/convert_queue.py --sets-file .codex_jobs/_app/convert_sets.txt --worker 1 <backend flags>`
  - The backend flags come from settings: `--no-tripo` while Tripo has no credits (the current state), otherwise Tripo first.
- **Adopt before start.** At startup and every 30 s, find running instances by scanning processes (psutil): python processes whose
  command line runs that script. A matching instance (e.g. one the user started by hand) is adopted and recorded in
  `.codex_jobs/_app/services/<name>.json` with `{pid, create_time, cmdline}`. A service is started only when none is running and
  its setting says enabled.
- Start detached: `creationflags = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW`, stdout/stderr appended to
  `.codex_jobs/_batch/<name>_win.out.log` / `.err.log` (the files the services already use). Restarting or stopping the web server
  never stops a service.
- Stop: the supervisor via its own stop file (`pipeline_supervisor.py stop`); the others by their recorded PID only, after checking
  the PID's command line. Never a broad kill.
- Crash restart: a service that is enabled and not running is restarted, with backoff (1 min, 5 min, then hold and show an error).
- Settings: `.codex_jobs/_app/settings.json`, e.g.
  `{"services": {"supervisor": true, "views": true, "convert": true}, "tripo": "off|auto", "layer_workers_present": 1,
  "layer_workers_away": 4}`.

### Convert eligibility
- The worker converts only sets matching its sets-file patterns. v2 writes its own file, `.codex_jobs/_app/convert_sets.txt`:
  - the old `.codex_jobs/_batch/convert_sets.txt` lines (legacy allowlist and `!` excludes), copied once;
  - plus every set that was approved in v2 (the action log), so an approval is the processing request.
- Old jobs in `multiview_ready/` / `processing/` that match no pattern show as **held: not in the convert list**, with a
  **Release** button that appends the set.
- Paid work still obeys the PAUSE / mode authority. Conversions are manually paused today; v2 shows that and leaves it until the
  user changes the mode in the Queue tab.

### Pause / mode controls (Queue tab)
- The mode buttons write `supervisor_mode.json`, which the supervisor reads every tick:
  - **Auto** (`views-run`): images always run; conversions only when the user is away.
  - **Pause conversions** (`pause-conversions`).
  - **Pause all** (`pause`).
  - **Run everything now** (`run`).
- Show the `PAUSE` file (reason, scope, since) and the idle seconds. Idle comes from `C:\Users\niall\.claude\presence.json` when it
  is fresh (< 30 s), else `GetLastInputInfo` (same rule as the supervisor: away = idle >= 300 s).

## 2. v2 lanes (new work owned by the server)
### Queue store `.codex_jobs/_app/queue.sqlite3` (`v2/queue.py`, stdlib sqlite3, WAL)
```sql
tasks(id INTEGER PRIMARY KEY, lane TEXT, key TEXT UNIQUE, set_name TEXT, unit TEXT, args TEXT /*json*/,
      state TEXT /*queued|running|done|failed|cancelled*/, priority INTEGER, attempts INTEGER, max_attempts INTEGER,
      pid INTEGER, pid_create REAL, started REAL, finished REAL, log TEXT, result TEXT /*json*/, error TEXT,
      created REAL, updated REAL)
events(id INTEGER PRIMARY KEY, time REAL, task_id INTEGER, kind TEXT, detail TEXT)
```
- `key` = `<lane>:<set>:<race>/<module>:<lod>:<job>`. Enqueueing an existing key that is done or running is a no-op. A failed one
  can be re-queued by Retry.
- Only the orchestrator thread writes task state. HTTP handlers call `queue.enqueue` / `queue.request(op)`, which take the same
  lock.

### Running a task
- Each task runs `python tools/preview/v2/run_task.py <task-json-file>` detached, so it survives a server restart.
- `run_task.py` runs the real command (e.g. `python tools/layers/build.py module <dir> --lod 0p2`), tees output to
  `.codex_jobs/_app/logs/<task id>.log`, and on exit writes `<task id>.result.json` (`{exit, last_json, finished}`).
- The orchestrator loop (every 3 s) handles:
  - **Running tasks.** A result file means the task finished: read it, set the task to done or failed, and record the result. A
    dead PID with no result file means `failed` ("lost"), with up to 2 automatic retries for local tasks.
  - **Queued tasks.** Start them while the lane has free slots and its gate is open.
  - **On startup.** Adopt running PIDs (pid + create_time) before starting anything.

### Layer lane (phase 3)
- **Approve in 3D** (`set:S` verb `approve`):
  - validate that every required unit is reduced with a `chosen_lod.json`, or dropped;
  - write `lod_approval` with job, lod and `glb_sha256` per unit;
  - enqueue `bare` tasks for races whose bare build is missing;
  - enqueue `layers` tasks per unit, after the bare task of their race.
- Concurrency: `layer_workers_present` (1) while the user is at the PC, `layer_workers_away` (start at 4; raise after the phase 2
  memory measurements) when away; 0 when mode is `pause`. GPU: `BODY_FIT_GPU=1` in the task env.
- **Build status per unit**:
  - **published** when `layers/current_<lod>.json` is `ok` and its `lod`/`module` match the approval;
  - **building** while a task is queued or running;
  - **failed** when the newest task failed (error + Retry).
  - The set stays in 3D with the badge "building n/m" until every approved unit is published, then shows in Aligned.
- **Stale builds.** The orchestrator recomputes fingerprints (cached by file mtimes) at start, after code changes, and every
  10 min. A unit whose published fingerprint is no longer current is **stale** (badge), and **Rebuild stale** (Queue tab, and per
  set) enqueues rebuilds. Approved builds are never overwritten; a rebuild makes a new build id.
- **Approve in Aligned**: write `aligned_approval` (units -> build_id + lod + job, and the amounts), then write `ready.json`.
  Completed is shown when the approval's build ids are still the ones in each `current_<lod>.json`. A rebuild after approval sends the
  set back to Aligned, with its amounts kept.

### Monster lanes (phase 5)
- **Intake.** Monster concepts are `monsters/aaa/concepts/<Monster>/codex<N>-front.png` with `.prompt.json` / `.colours.json`.
  - Their v2 state lives in `monsters/aaa/concepts/<Monster>/.v2_state.json`, mapping each concept file to
    `concepts|provisional|approved|archive|trash` plus the assigned variant number. No monster file ever moves.
  - The migration marks every concept that existed before the cut-over as `archive`, unless it is already picked (matched
    through `pipeline/<Monster>/variant_names.json` / the views' `prompts.json` source).
- **Approve (pick).**
  - Assign the next variant number, the way `number_monster_variants.py` does (flat numbers; `main` for a single-variant monster
    stays as is).
  - Copy the front and its json into `pipeline/<Monster>/views/<n>/front.png`, `colours.json` and `prompts.json`.
  - Record the pick in `variant_names.json`.
- **Views.** Three Codex view-runner tasks, one per output (`left.png`, `back.png`, `right.png`). They are image edits of the front:
  the camera turned about 90° left, 180° and 90° right around the monster, at the same height, same colour coding, no checks.
  - They are written into the runner's `tasks/` folder with `checks: []`, so the existing runner runs them (8 shared slots).
  - The prompt template lives in `v2/monster_prompts.py` and follows `monsters/aaa/PROMPTING_GUIDE.md`.
- **Choose views.** `views/<n>/views.json` holds `{"views": [{"name", "file", "enabled"}], "unattended": false}`. The card says
  "Choose views" until **Use enabled views** (verb `convert`) freezes the list into `views.json` (`frozen_at`, the hashes) and
  queues the conversion. An "unattended" tick at approval skips the wait.
- **Conversion lane** (one at a time; it shares the browser profile with the outfit convert worker):
  - It takes the cross-process lock `.codex_jobs/_batch/browser_hyper3d.lock`. Phase 5 adds the same lock to `rodin_backend`'s
    session start, so the outfit worker and the monster lane never use the profile at once.
  - It is gated like conversions (PAUSE scope `conversions`, idle rule).
  - Backend: Tripo is off this month (settings). Hyper3D runs `hyper3d_single.py` for one enabled view and an N-view generalisation
    of `hyper3d_twoview.py` for 2-4 views (upload order = enabled order; no direction labels).
  - Paid steps carry `--allow-paid --accept-preview` only when the app's paid budget allows. The user allowed up to 4 Hyper3D
    conversions without asking (2026-11-25); the app counts them in `.codex_jobs/_app/paid_log.jsonl`. Beyond that a conversion
    waits as "needs approval" with an **Allow** button.
  - Then `reduce_monster.py <variant>` (Blender) runs, giving 5 LODs.
- **3D -> Completed.** Approve writes `chosen_lod.json` with `approved: true` (`monsters.write_choice`), so
  `build_monster_blends.py sync` keeps working.
