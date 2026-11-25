# Preview app v2: cut-over and rollback (this PC)

DESIGN_PHASE4.md sections G and I, as commands. The old site (`python -u tools/preview/serve.py`, port 8765; Tailscale serve already forwards
to 8765) is replaced by v2 (`python -X utf8 -u tools/preview/v2/server.py --port 8765 --manage-services`). Service management is OFF unless
the server is started with `--manage-services`: only the cut-over server gets the flag (a dev / test server on 8766 never adopts, starts,
stops or signals a service). The prototype on :8790 is `experiments/shell_slots_20261122/serve.py`: it is not part of steps 1-8; leave it
running until step 9, which removes its files.

The pipeline services (supervisor, view runner, convert worker) are never killed by the cut-over. v2 adopts them; the steps below restart the
supervisor and the view runner on purpose and drain the old convert worker, each through the paid-safety interlock of DESIGN_PHASE4.md I:
the conversion hold `.codex_jobs/_batch/CONVERT_HOLD` (no paid conversion step starts or continues while it exists) stays in place from the
moment v2 starts until the new supervisor has completed a tick.

Run everything from the repo root in PowerShell:

```powershell
cd F:\godot\blender\characters\imageto3d
$repo = (Get-Location).Path
$logs = "$repo\.codex_jobs\_batch"
```

The user must agree before step 1. Nothing below runs by itself. Never `Stop-Process -Name python`; every stop names ONE PID that was
checked twice (command line and creation time) right before it is stopped.

## 0. Before

1. Everything is committed (`git status` clean; the generated outputs stay ignored, see the blender-repo-generated-outputs note).
2. Tests of the pieces the cut-over relies on pass:
   ```powershell
   python -X utf8 -m pytest -q tools/preview/v2/test_phase4.py tools/preview/v2/test_phase4b.py tools/preview/v2/test_phase3.py tools/preview/v2/test_v2.py
   python -X utf8 -m pytest -q tools/test_pipeline_supervisor.py tools/tripo_pipeline/test_workflow.py tools/test_view_batch_launcher.py
   ```
3. Record what runs now (compare after step 3):
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
     Where-Object { $_.CommandLine -match 'pipeline_supervisor\.py|view_batch\.py|convert_queue\.py|preview[\\/](v2[\\/])?serve(r)?\.py' } |
     Select-Object ProcessId, CreationDate, CommandLine | Format-List
   ```
   No other v2 server of THIS checkout may be running (it would hold the writer lock `.codex_jobs\_app\server.lock`, and the new server would
   come up read-only): a `tools/preview/v2/server.py` line whose working directory is this repo must not be in the list.

A helper used below: one process by PID, with its command line and creation time.

```powershell
function Get-ProcInfo([int]$Id) { Get-CimInstance Win32_Process -Filter "ProcessId=$Id" | Select-Object ProcessId, CreationDate, CommandLine }
```

## 1. Stop the old site (exactly the :8765 listener)

```powershell
$listen = @(Get-NetTCPConnection -State Listen -LocalPort 8765 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
$listen                                                       # exactly ONE pid
$old = Get-ProcInfo $listen[0]
$old | Format-List                                            # its command line must run tools/preview/serve.py (this repo), nothing else
```

Only if there is exactly one pid and its command line is `... tools/preview/serve.py ...` (not `tools/preview/v2/server.py`, not
`experiments/...`): check it again right before stopping it, then stop that pid.

```powershell
$again = Get-ProcInfo $listen[0]
if ($again.CreationDate -eq $old.CreationDate -and $again.CommandLine -eq $old.CommandLine -and $again.CommandLine -match 'tools[\\/]preview[\\/]serve\.py') {
  Stop-Process -Id $listen[0]
} else { 'the listener changed: stop here and look again' }
Get-NetTCPConnection -State Listen -LocalPort 8765 -ErrorAction SilentlyContinue    # prints nothing: the port is free
```

## 2. Start v2 on 8765, detached, and verify it is the WRITER with managed services

```powershell
foreach ($f in "$logs\preview_v2_win.out.log", "$logs\preview_v2_win.err.log") {
  if (Test-Path $f) { Move-Item $f ($f -replace '\.log$', '.prev.log') -Force }
}
Start-Process -FilePath python -WorkingDirectory $repo -WindowStyle Hidden `
  -ArgumentList '-X','utf8','-u','tools/preview/v2/server.py','--port','8765','--manage-services' `
  -RedirectStandardOutput "$logs\preview_v2_win.out.log" -RedirectStandardError "$logs\preview_v2_win.err.log"
```

Give it a few seconds, then check the JSON, not just HTTP 200 (a second v2 process answers 200 too, read-only):

```powershell
$state = Invoke-RestMethod http://127.0.0.1:8765/api/v2/state
$queue = Invoke-RestMethod http://127.0.0.1:8765/api/v2/queue
$state.server.read_only           # False  (True = another process holds the writer lock: stop the new one by its pid and find the other)
$state.server.manage_services     # True
$queue.services.managed           # True
$queue.convert_hold               # present: the manager wrote the conversion hold at start (DESIGN_PHASE4 I.1)
(Get-NetTCPConnection -State Listen -LocalPort 8765).OwningProcess   # = $state.server.pid
```

`$logs\preview_v2_win.err.log` has no traceback. Through Tailscale the same page loads; an unknown host name answers 421: add it to
`allowed_hosts` in the Queue tab's settings (it is accepted at once; removing one needs a restart).

## 3. v2 adopts the services

Within about 30 seconds (the first tick of the services thread; the first process scan can take several seconds) the Queue tab shows supervisor,
views and convert as **running, adopted**, with the PIDs of step 0.3 (also in `.codex_jobs/_app/services/<name>.json`). Nothing is started or
stopped by this. If a service shows `duplicate` or `waiting (could not be verified)`, fix that by hand first (v2 never does).

Settings that matter on this PC (`.codex_jobs/_app/settings.json`, edited in the Queue tab; defaults are fine): `tripo: "off"` (the convert
worker keeps `--no-tripo`), `view_parallel: 8`, all three services enabled. The mode stays `pause-conversions` (the user's manual pause); v2
never changes it by itself. If settings.json cannot be read, the last valid settings are kept, and with none v2 starts no service at all.

The conversion hold written at the manager's start is removed by the manager as soon as the adopted supervisor has written a tick
(`.codex_jobs/_batch/supervisor_tick.json`). The live supervisor of before the cut-over does not write that file, so the hold STAYS until
step 4: no paid conversion step runs meanwhile.

## 4. Restart the supervisor (claim-lock fix, tick file)

From the Queue tab press **Restart** on the supervisor. The manager:
1. renews the conversion hold (CONVERT_HOLD) BEFORE anything is stopped,
2. runs `python tools/pipeline_supervisor.py stop` and waits for the exit (frozen processes are resumed, PAUSE is removed by the old process),
3. puts the old PAUSE file back and starts the new supervisor,
4. removes the hold only after the NEW supervisor completed a tick (its pid is in supervisor_tick.json and the tick is newer than the hold).

There is no gap in which the convert worker or a running convert_job.py could start a paid step: they honour CONVERT_HOLD exactly like PAUSE.
Check in the Queue tab: supervisor running (new pid), `convert_hold` gone after its first tick (about a minute). Never restart the supervisor
by hand around the manager; if it must be done by hand, write CONVERT_HOLD first and let the manager remove it.

## 5. Restart the view runner (to load the monster launcher)

The live view runner predates `"launcher": "monsters"` tasks and the single-instance lock / launch reservation (DESIGN_PHASE4 I.5). Press
**Restart** on views in the Queue tab: the runner is stopped through its verified PID; running Codex jobs continue and the new runner adopts
them through their `.done` markers (and launching reservations). Do it when no monster task is waiting to launch, or accept that it waits for the
new runner. Afterwards `.codex_jobs/_batch/view_runner.lock` is held by the new runner.

## 6. Drain the old convert worker (legacy rule, I.2)

The live convert worker predates the per-worker stop file. Press **Stop** (or **Restart**) on convert in the Queue tab. The manager writes
`convert_worker1.stop` naming the worker's pid and creation time, but an old worker does not read it, so the manager applies the legacy rule
on every tick: it stops the worker by its verified PID ONLY when no `convert_job.py` of this checkout runs AND the worker's last event in
`convert_events.log` (since it started) is `PAUSED`, `IDLE` or `LOW DISK` after its last `CONVERT` (it is asleep). Until then the stop just
stays pending ("stopping" in the Queue tab); it is never killed on a timeout. With the current `pause-conversions` mode the worker sleeps in
PAUSED, so this normally completes at the next tick. On Restart the manager starts the new worker after the old one is gone; the new worker
honours CONVERT_HOLD, PAUSE and its own stop file (it acknowledges with `convert_worker1.stopping`, finishes its job, then exits).

## 7. Rollback (any time before step 9; the services keep running)

```powershell
$listen = @(Get-NetTCPConnection -State Listen -LocalPort 8765 | Select-Object -ExpandProperty OwningProcess -Unique)
$v2 = Get-ProcInfo $listen[0]
$v2 | Format-List                                             # exactly one pid; command line tools/preview/v2/server.py --port 8765
$again = Get-ProcInfo $listen[0]
if ($listen.Count -eq 1 -and $again.CreationDate -eq $v2.CreationDate -and $again.CommandLine -match 'tools[\\/]preview[\\/]v2[\\/]server\.py') {
  Stop-Process -Id $listen[0]
}
Start-Process -FilePath python -WorkingDirectory $repo -WindowStyle Hidden `
  -ArgumentList '-u','tools/preview/serve.py' `
  -RedirectStandardOutput "$logs\preview_win.out.log" -RedirectStandardError "$logs\preview_win.err.log"
```

Stopping v2 never stops a service. A conversion hold v2 left behind (`.codex_jobs/_batch/CONVERT_HOLD`) keeps conversions waiting: once the
supervisor runs and has ticked, delete it by hand (its `owner_pid` is the stopped v2 server). The old site reads the same folders; v2 decision
files (`pipeline_v2.json`, `.codex_jobs/_app/`) are ignored by it. Convert-list lines v2 appended to `.codex_jobs/_batch/convert_sets.txt` are
exact set ids (never wildcards) and stay; remove them by hand if unwanted. If the Queue tab had **stopped** a service on purpose, start it again
by hand with the commands in DESIGN_PHASE4.md section A.

## 8. Optional: start v2 at logon

`tools/preview/v2/install_logon_task.ps1` registers the scheduled task "Imageto3D Preview" (per user, at logon, hidden, repo as working
directory, same command as step 2). Each run first checks the :8765 listener: when a v2 server of this repo already listens it does nothing (no
log rotation, no second launch); when anything else listens it reports that and does nothing. It is not run by this procedure; the user
decides:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\preview\v2\install_logon_task.ps1 -WhatIf     # show only
powershell -NoProfile -ExecutionPolicy Bypass -File tools\preview\v2\install_logon_task.ps1             # register
powershell -NoProfile -ExecutionPolicy Bypass -File tools\preview\v2\install_logon_task.ps1 -Uninstall  # remove
```

## 9. Merge the phase 6 branch (last step, after the new site is verified)

Only once steps 1-6 are done and the user has used v2 on :8765 and agrees. Branch `pipeline-v2-phase6` (based on `82e6de1`) removes the old
site's UI (`tools/preview/app.js`, `index.html`, `style.css`, `fitsym.js`, `monsters.js`, `test_fitsym.mjs`, `preview.sh` and the HTTP handler of
`serve.py`, which becomes a library whose `main()` prints how to start v2 and exits 2), the prototype's code and pages in
`experiments/shell_slots_20261122/` (its `out/` data and README stay), and updates AGENTS.md / CLAUDE.md / the READMEs for v2.

1. The old site is no longer running (step 1). The prototype on :8790 loses its server file and pages with the merge: if the user agrees, stop
   exactly its listener first, the same way as step 1 (one pid, command line `experiments/shell_slots_20261122/serve.py`, checked twice).
   v2 itself does not need a restart: it imports `serve.py` only at start, and the functions it uses are unchanged.
2. From the repo root, with `git status` clean:
   ```powershell
   git merge --no-ff pipeline-v2-phase6
   python -X utf8 -m unittest tools/preview/test_serve.py tools/preview/test_monsters.py
   python -X utf8 tools/preview/serve.py        # prints "retired ... tools/preview/v2/server.py --manage-services" and exits 2
   ```
3. v2 keeps running: the branch changes only docstrings in `v2/actions.py`, `v2/decisions.py` and `v2/server.py`. The services are not touched.

**Rollback after this step** means restoring the old files and starting the old site:
```powershell
git checkout 82e6de1 -- tools/preview experiments/shell_slots_20261122
```
then the start command of step 7. Careful: that path list also resets `tools/preview/v2/` and `tools/preview/web/` to `82e6de1`; if v2 fixes
were committed after `82e6de1`, restore only the removed files instead:
```powershell
git checkout 82e6de1 -- tools/preview/serve.py tools/preview/test_serve.py tools/preview/app.js tools/preview/index.html tools/preview/style.css tools/preview/fitsym.js tools/preview/monsters.js tools/preview/test_fitsym.mjs tools/preview/preview.sh experiments/shell_slots_20261122
```
