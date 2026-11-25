# Preview app v2 - phase 4 contract (the server runs the pipeline services; Queue tab v1; cut-over)

Extends DESIGN_ORCHESTRATOR.md §1. The principle: **adopt and manage** the three existing daemons; do not rewrite them.

## A. Service manager (`v2/services.py`, driven by the orchestrator thread on the writer server)
- **Services** (name → command, matcher), all started with the repo root as cwd:
  - `supervisor`: `python -X utf8 -u tools/pipeline_supervisor.py run`. Matched by a cmdline containing `pipeline_supervisor.py` and
    `run`.
  - `views`: `python -u tools/view_batch.py run --parallel <settings.view_parallel, default 8>`. Matched by `view_batch.py` and `run`.
  - `convert`: `python -X utf8 -u tools/convert_queue.py --sets-file .codex_jobs/_batch/convert_sets.txt --worker 1` plus `--no-tripo`
    when `settings.tripo == "off"`. Tripo is off in November 2026: there are no credits. Matched by `convert_queue.py`.
- **Adopt before start.** Every 30 s, scan processes with psutil for python processes whose cmdline matches. A match is recorded in
  `.codex_jobs/_app/services/<name>.json` as `{pid, create_time, cmdline, adopted: true|false, since}`.
  - A service is started only when `settings.services.<name>` is true and no instance runs.
  - Two instances of the same service is an error shown in the Queue tab. It is never "fixed" automatically.
- **Start.** Detached (`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`), stdout/stderr appended to
  `.codex_jobs/_batch/<name>_win.out.log` / `.err.log`, env `PYTHONUTF8=1`. Restarting the web server never stops a service.
- **Stop** (Queue tab button, with a confirmation):
  - `supervisor`: `python tools/pipeline_supervisor.py stop` (its own stop file). It resumes frozen processes on exit.
  - `views`: by PID, after re-checking the PID's cmdline and create_time. Running Codex jobs continue and are re-adopted by the next
    runner through their `.done` markers.
  - `convert`: **graceful only.**
    1. Add a stop-file check to `convert_queue.py`'s loop: `.codex_jobs/_batch/convert_worker.stop` makes it exit before taking the
       next job, and it deletes the file.
    2. The button writes that file. "Stop now" is not offered while a `convert_job.py` child runs.
    3. The adopted old worker without this code can only be stopped by PID when no `convert_job.py` runs.
- **Crash restart.** An enabled service that is not running restarts after 60 s, then 5 min, then holds with an error ("restart
  failed 3 times") until the user presses Start. A clean exit counts too, unless a stop was requested. The convert worker exits by
  itself when idle with no view runner; that is a clean idle exit, and it is restarted only when work appears: staged jobs exist.
- **Settings** in `.codex_jobs/_app/settings.json` (missing keys take defaults):
  `{"services": {"supervisor": true, "views": true, "convert": true}, "view_parallel": 8, "tripo": "off", "layer_workers_present": 1,
  "layer_workers_away": 4, "allowed_hosts": [], "paid_budget_hyper3d": 0}`. The Queue tab edits them.

## B. Convert eligibility (approval = processing request)
- The worker already re-reads `.codex_jobs/_batch/convert_sets.txt` every loop. v2 appends a set id line under a lock (atomic
  rewrite) when the supervisor claims a set the user approved in v2, i.e. when `approve` reaches `approved/`. The set's
  `_gnome`-suffixed ids follow the base pattern: the worker matches on `job.name.split('--')[0]`, so a `<set>_gnome_female` job needs
  its own line or a `<set>*` pattern. ~~Write `<set>` and `<set>_*`.~~ Superseded by I.6: exact ids only, never a wildcard.
- Jobs in `multiview_ready/` or `processing/` whose set matches no pattern show in Approved and Queue as **held: not in the convert
  list**, with **Release**, which appends the lines. Existing `!` excludes are respected and shown.
- Paid work stays under the mode/PAUSE authority. Today the mode is `pause-conversions` (the user's manual pause); v2 never changes
  the mode by itself.

## C. Mode and pause controls (Queue tab header, also a compact indicator in the main header)
- Buttons write `.codex_jobs/_batch/supervisor_mode.json` `{mode, since}`; the supervisor reads it every tick:
  - **Auto** = `views-run`: images always run; conversions only when the user is away.
  - **Pause conversions** = `pause-conversions`.
  - **Pause all** = `pause`.
  - **Run everything now** = `run`.
- Shows the `PAUSE` file (reason, scope, since), the user idle seconds, the layer pool limit and why. If the supervisor is not
  running, the mode has no effect on conversions; the indicator says "supervisor not running".

## D. Supervisor fix (claim lock gap from the phase 1 fix)
`pipeline_supervisor.py` moves race folders into `approved/<set>` before it runs `workflow.py claim` (gate 1, around lines
635-722). It must hold `tripo_pipeline/claim_lock.py`'s lock for the set from before the first move until claim returns. v2
unapprove already takes that lock. The running supervisor picks this up only after a restart; the Queue tab offers **Restart**,
which runs stop and then start.

## E. Queue tab v1 (extends v0)
1. **Services**: name, state (running, adopted or started, stopped, crashed, held), PID, uptime, last 20 log lines (expandable),
   Start / Stop / Restart.
2. **Mode / pause** (section C), **disk free**, **credits**:
   - Tripo: "off (no credits this month)" plus a **Check balance** button. It runs `node tools/tripo_pipeline/studio.mjs balance` only
     when no `convert_job.py` runs, because it uses the same browser profile; the result is cached in `.codex_jobs/_app/credits.json`.
   - Hyper3D: the last balance recorded in any `studio_submission.json` (`balances`), with its time.
3. **Views** (`.codex_jobs/_batch/tasks` and `status`): counts by state; running tasks with elapsed time; failed tasks with the last
   job's log tail and **Retry**. Retry sets the status to `pending` with attempts 0; it is the runner's own resume path.
4. **Conversions**: the current job and module (from `convert_locks/*.lock` holders and the module locks), the
   `convert_events.log` tail, jobs with `.fails` > 0 with **Reset** (deletes the `.fails` file), and held jobs with **Release**.
5. **Holds** (supervisor_state.json `holds`): reason and set. **Accept** uses the supervisor's existing accept path where one exists
   (`accept_drift` for colour drift); others link to the Approved modal. Read the supervisor code to see what each hold kind needs.
6. **Layer builds** (phase 3 table).

## F. Approved tab progress
Per race/module, the unit status: views x/4 from the task status files, `held` with the reason, `not in convert list`,
`converting <step>`, `failed` with the error. Badges on cards: `views 3/11`, `converting 2/11`, `held`, `failed`.

## G. Cut-over (needs the user's OK; Claude runs it)
1. Commit everything. Write `tools/preview/v2/CUTOVER.md` with these steps and a rollback.
2. Stop the old site: the python process running `tools/preview/serve.py`, by PID after checking its cmdline.
3. Start v2 on port 8765, detached: `python -X utf8 -u tools/preview/v2/server.py --port 8765 --manage-services`. Tailscale serve already
   forwards to 8765. (Service management is off without `--manage-services`; see H.)
4. v2 adopts supervisor, views and convert. Check the Queue tab.
5. Restart the supervisor from the Queue tab, so it picks up the claim-lock fix in section D.
6. **Rollback:** stop v2 by PID and start `python -u tools/preview/serve.py`. The services keep running either way.
- Optional: `tools/preview/v2/install_logon_task.ps1` registers a Windows scheduled task, "Imageto3D Preview", that starts v2 at
  logon. The user decides whether to run it.

## H. Integration contract (phase 4B; uses the part-A modules settings, services, credits, convert_list, modes)
### Orchestrator
- **Services thread.** A second daemon thread on the writer server runs `services.tick(root, settings.load(root))` every 30 s. The
  first psutil scan takes about 6 s, which is why this is not part of the 3 s loop.
  - It keeps the last snapshot in memory for the API.
  - HTTP start/stop/restart requests run on this thread, never on the HTTP thread, because `stop(wait=...)` blocks.
- **Convert list.** v2 `approve` into `approved/` (direct, and provisional -> approved) calls `convert_list.add_sets(root, [set])`
  after the move, writing the exact family ids of I.6 (no wildcard). If that fails the response carries a warning and the move stands.
- **Settings.** `settings.load` becomes the only settings reader: replace the orchestrator's direct reads of settings.json for
  `layer_workers_*` and `build_stall_minutes`.

### GET /api/v2/queue (extends phase 3)
Adds these keys:
- `services`: `services.status`.
- `mode`: `modes.snapshot` (mode, pause, idle_seconds, supervisor_running).
- `credits`: `credits.snapshot`.
- `disk`: free bytes on the root drive.
- `views`: `{counts: {state: n}, running: [{name, job, elapsed}], failed: [{name, attempts, log_tail}]}`, from
  `.codex_jobs/_batch/tasks` and `status`.
- `conversions`: `{current: [{job, module, holder_pid}], events_tail: [...], fails: [{job, count}], held: convert_list.held_jobs}`.
- `holds`: from supervisor_state.json, as `[{key, reasons, set}]`.
- `settings`: `settings.load`.

### POST /api/v2/queue ops (extends phase 3)
| op | args | effect |
| --- | --- | --- |
| `service` | `{name, action: start\|stop\|restart}` | restart keeps PAUSE (part A) |
| `mode` | `{mode: auto\|pause-conversions\|pause\|run}` | |
| `settings` | `{patch}` | 400 with the `SettingsError` problems |
| `check_tripo` | | |
| `release` | `{set}` | `convert_list.add_sets` |
| `view_retry` | `{name}` | status -> pending, attempts 0, jobs kept |
| `convert_reset` | `{job}` | deletes `convert_locks/<job>.fails` |

Every op uses the existing token/host rules and is logged as an event.

### Approved tab progress (catalog)
- Each unit gets `progress`:
  - `views_done` / `views_total`, from the view task status;
  - `held_reason`: a supervisor hold, `not in convert list` or `excluded`;
  - `converting_step`, from the module lock holder and `module_conversion`;
  - `error`.
- Card badges: `views a/b`, `converting n/m`, `held`, `not in convert list`, `failed`.

### Frontend
- **Queue tab v1.** These sections sit above the phase 3 task table:
  1. Services, with Start / Stop / Restart and log tails.
  2. Mode buttons with plain labels: Auto (images always, conversions when away), Pause conversions, Pause all, Run everything
     now. A pause chip shows the reason and since when.
  3. Credits with Check balance, and disk free.
  4. Views: counts, running, failed with Retry.
  5. Conversions: current, events tail, fails with Reset, held jobs with Release.
  6. Holds.
  7. Settings: an editable form for `view_parallel`, `layer_workers_present`, `layer_workers_away`, `build_stall_minutes`,
     `tripo` (off/auto) and the services toggles.
- **Header.** A compact status chip (`mode · paused? · services ok/n problems`) that links to Queue.
- **Approved modal.** Shows `progress` per unit, plus Release when a set is held as not in the convert list.

### H.1 Backend as built (phase 4B; additive: nothing above changes meaning, these are the exact shapes)
- **`--manage-services`** (server flag, default OFF; `create_server(manage_services=...)`). Only a writer server with the flag runs the services thread
  (`orchestrator.ServiceManager`, tick every 30 s, the first one at start). Without it nothing adopts, starts, stops or signals a service, and the
  `service` op answers **409 `code: "services_off"`**, error `service management is off (start the server with --manage-services)`. CUTOVER.md and
  `install_logon_task.ps1` pass the flag.
- **`GET /api/v2/state`** adds `status = {mode: {mode, label, since}, paused: false | {reason, since, scope}, services: null | {ok, problems: [text]},
  queue_health, pause_reason, services_problems}` (the shape the phase 4 client reads; `pause_reason` / `services_problems` are flat copies).
  `services` is `null` without `--manage-services`; `problems` come from the last tick: states `duplicate` / `held` / `crashed`, and an enabled
  service that is `stopped` without a stop request (`<name>: not running`) or `waiting` for anything but the supervisor's first tick
  (`<name>: not started (...)`); `ok` = no problems. With the flag `services` also carries `managed: true` and `scanned` (false until the first
  services tick: the header can say "services scanning..."). `state.server` adds `manage_services` and `pid` (CUTOVER.md checks them).
- **`GET /api/v2/queue`** keys:
  - `services`: without the flag `{managed: false, message, services: {}, convert_jobs: null, errors: [], problems: null}`; with it
    `services.status(...)` = `{services: {name: {name, enabled, state, message, pid, uptime, adopted, since, error, fails, restart_in, stop_pending,
    duplicates, command, running_command, command_differs, convert_jobs, logs: {out_file, err_file, out, err, events}}}, convert_jobs: null, errors,
    hold, settings}` plus `{managed: true, scanned,
    last_tick, interval, pending: [{id, name, action, time, running?}], events: [{time, kind: "tick"|"request", text, request?, name?, action?, ok?,
    pending?}], manager_errors, problems}`. A service request's outcome is the `events` entry with its `request` id.
  - `mode`: `modes.snapshot` (`{mode: {mode, since, label}, pause, idle_seconds, idle_source, away, supervisor_running, supervisor_pid, warning}`).
  - `credits`: `credits.snapshot` (`{tripo: {mode, text, credits, checked_at, ok, error}, hyper3d: {credits, at, stage, source}}`, reused 20 s) plus
    `check: {running, started, finished, last}` (`last` = `{status: ok|busy|error, credits?, checked_at?, message?}`).
  - `disk`: `{free, total, path}` (bytes).
  - `views`: `{counts: {state: n}, by_kind: {views|fronts|concepts: {state: n}}, running: [{name, job, started, elapsed}], failed: [{name, attempts,
    job, exit, log_tail}], total}` (log tail = `.codex_jobs/<job>/log.txt`).
  - `conversions`: `{current: [{job, module: "race/module"|null, holder_pid, step}], events_tail: [lines], fails: [{job, count, stuck}],
    held: convert_list.held_jobs, excludes: ["!..."], max_tries: 3}` (`stuck` = count >= the worker's MAX_TRIES: it no longer takes the job).
  - `holds`: `[{key, reasons, set, job, gnome, drift, images}]` (`job` = the key when it is a job folder name, else null; `drift` = a colour DRIFT
    reason; `images` = `[{path, sha256, race}]` of the drifted images inside the hold's scope with their CURRENT digests, `[]` without drift: exactly
    the pairs `accept_drift` accepts).
  - `settings`: `settings.load`; `settings_health`: `settings.health` (`{error, problems, source: file|defaults|last_valid|none, hold_starts}`).
  - `convert_hold`: `services.hold_state` (`{since, reason, owner_pid, owner: "this server"|"another live process"|"a process that exited",
    message}`) or null (I.1).
- **`POST /api/v2/queue` ops** (all logged as queue events, kind `op`):
  - `service {name, action}` -> `{ok, pending: true, request, name, action}`; 400 for an unknown name / action; 409 `busy` while a request for that
    service is waiting or running; 409 `services_off` without the flag. Stop / Restart of the supervisor write the conversion hold first (I.1;
    the old PAUSE is also put straight back after the exit). Stop of the convert worker follows I.2 (pending until the worker exits; never killed
    on a timeout).
  - `mode {mode: auto|pause-conversions|pause|run}` -> `{ok, mode: modes.read_mode}` (auto writes `views-run`).
  - `settings {patch}` -> `{ok, settings}`; a bad patch is **400 with `problems: [...]`** and writes nothing. New `allowed_hosts` are accepted at once
    (removing one needs a restart); with the flag the services thread ticks at once.
  - `check_tripo` -> HTTP 200 in the `credits.check_tripo` shape: `{ok: true, status: "running", pending: true}` when the check started on its
    worker thread, `{ok: false, status: "busy", message}` while a check runs, the Tripo browser lock (I.3) is held or a `convert_job.py` /
    `studio.mjs` of this checkout runs. Only exit 0 with a numeric `credits` counts; anything else is `error` and keeps the last good balance. The
    result (up to ~300 s
    later): `queue.credits.check.last` (`{status: ok|busy|error, credits | message}`) and, when ok, `queue.credits.tripo.credits`.
  - `release {set}` -> `{ok, set, added, present, excluded, warning?}`; 400 for a name outside `serve.NAME`; 404 unless the set EXACTLY is the set
    of a staged job (`catalog.job_set` of its folder name) or a catalog set (a prefix such as `plate` of `plate_01` is not); writes the family ids
    of I.6 plus the exact prefix of each staged job of the set; `warning` when a `!` exclude still matches.
  - `accept_drift {key, images: [{path, sha256}], note?}` -> `{ok, key, accepted: [{path, sha256}], already: [...], advance}`: the Accept of a
    colour-drift hold (E.5). Each image must be one of the hold's `images` (400 otherwise; 404 for an unknown hold) and its current bytes must
    hash to the given sha256 (409 `changed`). The digests are appended to `.codex_jobs/_batch/advance.json` `accepted_drift` in the supervisor's
    own format (`{sha256, image, note, date}`, as `pipeline_supervisor.py accept-drift`); the supervisor re-opens the hold at its next pass.
  - `view_retry {name}` -> `{ok, name, state: "pending", attempts_before, jobs}`; only a `failed` task (400 otherwise), 404 for an unknown task.
  - `convert_reset {job}` -> `{ok, job, count_before}`; 404 when the job has no `.fails` file.
- **Approve -> convert list.** Direct approve, provisional -> approved, the legacy promote and a Recovery Complete of an interrupted legacy
  approve add the exact family ids of I.6; the response carries
  `convert_list: {added, present, excluded} | null` and, on a failure or a matching exclude, a `warning` (the move stands).
- **Unit progress** (`item.units[*].progress`; `null` only on a fresh single-set derivation): `{views_done, views_total, views_state, views_task,
  views_attempts, fronts_state, fronts_task, held_reason, held_kind, held_detail, converting_step, converting_pid, convert_fails, error}`.
  - views: in `processing_views` from the newest `views--<job prefix>--<race>--<module>[-suffix]` task (total = its outputs + the views already there,
    4 without a task; done = the views present, all when the task is `done`); before claim 0/4 plus `fronts_state` of `fronts--<set>--<race>*`;
    past views done = total.
  - `held_kind` / `held_reason`: `supervisor` (the hold reasons that name this unit, or the job-wide ones; gate-1 set holds for unclaimed races) >
    `held_conversions` (`held for review (held_conversions/)`) > `excluded` (`excluded`, `held_detail` names the pattern) > `convert_list`
    (`not in convert list`).
  - `converting_step` (only while the job lock and the module lock have a live holder): `submitting`, `<backend>: <stage|status>` from
    studio_submission.json, `reducing`, `post-processing (split / align / winding)`.
  - `error`: the unit's conversion error, else `views task failed after n attempt(s): <task>`, `fronts task failed: <task>`, or (at >= 3 `.fails`)
    `conversion failed n times; the convert worker no longer takes this job (Reset in the Queue tab)`. 1-2 fails only set `convert_fails`.
- **Approved card badges** (with progress): `reduced a/b` (as before), `views a/b` (units whose views are complete; only while some are not),
  `converting n/m` (units in processing not yet converted), `held n` (supervisor + held_conversions), `not in convert list`, `excluded from convert
  list`, `failed n` (status failed or a progress error), plus the plain `ready n`, `missing n`, `pin missing n`.
- **`card.progress`** (Approved tab) = `{race: {module: {status, views_done, views_total, held_reason, converting_step, error}}}` (the unit progress
  record above, trimmed); a bare status text only when a derivation carried no progress (the client accepts both).
- Settings: `settings.load` is the only reader (orchestrator pool limits and `build_stall_minutes`, builds' stall check, the server's `allowed_hosts`).

## I. Paid-safety mechanisms (phase 4 review fixes, review_v2_phase4_20261125; shared by the daemons and v2)
These names and semantics are fixed. Both the daemon-side fixes and the v2-side fixes implement them.

1. **Conversion hold: `.codex_jobs/_batch/CONVERT_HOLD`.** JSON `{since, reason, owner_pid, owner_create_time}`.
   - **Who writes it.** v2's service manager writes it (atomically):
     - BEFORE it stops or restarts the supervisor;
     - at manager start, before it starts or adopts a convert worker.
   - **Who removes it.** The manager removes it only when the supervisor is running AND has completed a tick after the last
     (re)start. The supervisor writes `.codex_jobs/_batch/supervisor_tick.json` (`{pid, create_time, time, mode, paused}`) at the end of
     every tick, so "completed a tick" means the file's pid is the current supervisor and its time is after the hold's `since`.
   - **Without a supervisor.** If the supervisor is disabled or held, the hold stays. Conversions then need the supervisor running;
     this is documented.
   - **Stale holds.** A hold whose owner pid/create_time is dead and older than 10 min may be cleared by the next manager start, which
     then writes its own.
   - **What readers do:**
     - `convert_queue.py` starts no job while CONVERT_HOLD or PAUSE (scope conversions) exists, the same as PAUSE today.
     - `convert_job.py` waits, logging once, while CONVERT_HOLD or PAUSE (scope conversions) exists before EVERY paid step: Tripo
       submit, Hyper3D submit, and Hyper3D collect with --allow-paid. Today it only waits before a new submission. A collect of an
       existing submission must wait too.
     - Heavy local steps are still governed by the supervisor's freeze as today.
2. **Per-worker graceful stop.**
   - **The request.** `.codex_jobs/_batch/convert_worker<N>.stop` with `{pid, create_time, request, time}` naming the worker process
     to stop.
   - **Which worker obeys.** Worker N honours only a file naming its own pid and create_time. A file for a dead process is deleted by
     the manager.
   - **The worker's response:**
     1. On honouring it, the worker writes `convert_worker<N>.stopping` (`{pid, time}`).
     2. It finishes its current job, including finish() and offload.
     3. It exits, then removes both files.
   - **The manager.** It waits for the process to exit and never kills on timeout; a stop just stays pending, visible in the Queue.
   - **Legacy workers** (no stop support, i.e. the live worker at cut-over) are stopped only when no `convert_job.py` of this checkout
     runs AND the worker's last event in `convert_events.log` is `PAUSED`, `IDLE` or `LOW DISK` after its last `CONVERT`. That means it
     is sleeping. Otherwise the stop stays pending.
3. **Tripo browser lock: `.codex_jobs/_batch/browser_tripo.lock`.** A cross-process byte lock in the style of `claim_lock`, taken by
   every python caller of `tools/tripo_pipeline/studio.mjs` around each call:
   - convert_job's studio calls;
   - `convert_queue.tripo_balance`;
   - `v2.credits.check_tripo`, which returns `busy` immediately on contention.
4. **Hyper3D browser lock** (`.codex_jobs/_batch/browser_hyper3d.lock`):
   - It moves down into the function(s) that actually open the Firefox profile (`native_browser` / `workflow.py` profile launch), so
     every entry point is covered.
   - It is re-entrant within one process (module-level depth counter), and the process that opens the profile holds it.
   - Lanes and parents never hold it while waiting on a child.
   - **Paid-record lock:** each output dir gets `.record.lock`, held for the whole submit / collect / inspect command. A second command
     on the same output stops with `another process is handling this output`. The record is re-read after acquiring the lock, and
     record creation is exclusive.
5. **View runner single instance.**
   - **Instance lock:** `tools/view_batch.py run` takes `.codex_jobs/_batch/view_runner.lock` (byte lock); a second instance exits.
   - **Launch reservation:** before Popen it writes the task status `{state: "launching", job, started}`. On (re)start, a `launching`
     task whose `.codex_jobs/<job>` (or monster root job dir) exists is adopted as running; otherwise it goes back to `pending`.
6. **Convert list without wildcards.** Approve and Release write exact ids only:
   - `<set>`, plus `<set>_<race>` for every race in `base_views/*/palette.json`. These are the only family job names the supervisor
     creates.
   - Release accepts only a set id that exactly equals a staged job's set (via `catalog.set_of`) or a catalog set. One name grammar
     (`serve.NAME`) is used for approve and release.
   - Existing user patterns (e.g. `plate_basic_05*`) are left as they are.

### I.7 As built on the v2 side (review_v2_phase4_20261125)
- **Conversion hold (I.1)**, `services.py`: `begin_management` (the services thread's first step) writes ours, replacing a dead owner's hold and
  never touching a live other owner's; `stop` / `restart` of the supervisor renew it BEFORE `pipeline_supervisor.py stop`; every tick re-asserts
  it while no supervisor (exactly one verified instance) runs, so a crashed or disabled supervisor also holds conversions. The tick removes it
  only when it is ours (or a dead owner's older than 10 min) and `supervisor_tick.json` names the running supervisor (pid, and create_time
  when present) with a `time` (epoch, or ISO) after the hold's `since_unix`. The file is `{since (ISO, ms), since_unix, reason, owner_pid,
  owner_create_time}`. Start order: supervisor, views, then the convert worker only once the running supervisor ticked after its own start
  (a user Restart of the worker is not delayed; the hold covers it).
- **Process identity (F5)**: `services.identify` parses the interpreter options (`-c` / `-m` / stdin = no script), the exact script path of this
  checkout, the sub-command (`run`; `run --dry-run` is not the daemon), `--root` (supervisor) and `--worker` (`N`, `=N`, argparse prefixes, last
  wins, default 1). Unresolvable relative scripts are `unverified`: never adopted or signalled, and they block a start, as do a held
  `supervisor.lock` / `view_runner.lock`. Signals go to one re-verified `psutil.Process` handle. The server keeps `server.lock` while the services
  thread is still running at shutdown (`ServiceManager.stop` returns False), so no second manager can start.
- **Convert stop (I.2)**: `convert_worker1.stop` = `{pid, create_time, request, time}`; the shared `convert_worker.stop` is never written (and
  removed when no stop is pending). Legacy = the worker started before the current `convert_queue.py` was written, or that file lacks
  `.stopping`. Every tick re-evaluates a pending stop; nothing is killed on a timeout. Stop/stopping files naming a dead process are deleted.
- **Tripo browser lock (I.3)**: `credits.check_tripo` holds `browser_tripo.lock` (claim_lock-style byte at 1 << 20) around `studio.mjs balance`
  (busy at once on contention); `TripoCheck.request` probes it first. credits.json writes are per-key read-merge-writes under
  `.codex_jobs/_app/credits.lock`.
- **View runner (I.5)**: the manager probes `view_runner.lock` before starting a runner; `launching` tasks count as running in the Queue.
- **Convert list (I.6)**: the family ids are `<set>`, `<set>_<race>` for every `base_views/*/palette.json` race and `<set>_<gnome family>`
  (`<set>_gnome`, the historical gnome-only job name that catalog.job_set and finish_sets.set_of map back to the set). `convert_list.NAME` is
  `serve.NAME` (a test compares them). `has_work` (waking an idle worker) uses `convert_list.eligible_jobs`: listed, fewer than MAX_TRIES fails,
  at least one module that is unconverted and not held by a live module lock.
- **Settings (F16)**: a missing file = defaults; an unreadable / invalid existing file keeps the last valid policy of the process; with none,
  `settings.health().hold_starts` makes the manager start nothing. An invalid `services.<name>` flag counts as the last valid flag or off.
- **Aligned approve (F11)**: under the publish locks, right before ready.json, the pinned GLB is rehashed and the module / bare fingerprints are
  recomputed against the pointers just read: a change is 409 `stale`.
- **Queue recovery (F12)**: every new database identity merges the surviving attempts by attempt identity (`taskqueue.adopt_attempt`:
  inserted / known / merged into a queued-failed-cancelled-done row / conflict), whatever rows it holds. run_task writes its result only after
  the builder exited (`builder_pid`, `builder_create`, `builder_exited`; a log write failure drains the pipe and waits); a result whose builder
  still runs keeps the slot (orphan).
- **Legacy recovery (F13)**: the promotion is located by its mark in `approved/<set>` and in every claimed job folder; a claimed promotion
  cannot be rolled back (409), several places are ambiguous (409), an intent without a promotion id is never completed by folder name (409,
  manual check). Complete applies the convert-list update.
- **Damaged sets (F14)**: record fields of the wrong type are ignored and listed as unit warnings; a job / model folder whose scan raises, or a
  set whose derivation raises, becomes a damaged set (`derive_error`, read-only, badge `damaged: ...`) that reconcile skips (no queueing,
  cancelling or revoking because of it).
- **Progress (F19)**: a unit's views task must write into the unit's own job folder (its outputs' `<job>/<race>/<module>/<view>.png`); a failed
  task's log is read under the launcher's project (`"launcher": "monsters"` = `$IMAGETO3D_MONSTERS` or `characters/monsters/aaa`).
