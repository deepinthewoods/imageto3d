# Preview app v2: cut-over and rollback (this PC)

DESIGN_PHASE4.md section G, as commands. The old site (`python -u tools/preview/serve.py`, port 8765, Tailscale serve already forwards to 8765) is
replaced by v2 (`python -X utf8 -u tools/preview/v2/server.py --port 8765 --manage-services`). Service management is OFF unless the server is
started with `--manage-services`: only the cut-over server gets the flag (a dev / test server on 8766 never adopts, starts, stops or signals a service).
The pipeline services (supervisor, view runner, convert worker) are never stopped by the cut-over: v2 adopts them, and rolling back leaves them running too. Run everything from the repo root:

```powershell
cd F:\godot\blender\characters\imageto3d
$repo = (Get-Location).Path
```

The user must agree before step 2. Nothing below runs by itself.

## 0. Before

1. Everything is committed (`git status` clean; the generated outputs stay ignored, see the blender-repo-generated-outputs note).
2. v2 is read-only safe: a second v2 server on the same root would not write (it holds no `server.lock`). Check nobody started one:
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
     Where-Object { $_.CommandLine -match 'tools[\\/]preview[\\/]v2[\\/]server\.py' } |
     Select-Object ProcessId, CreationDate, CommandLine
   ```
   (the prototype on :8790 is one of these when it runs; it is not the cut-over server and may keep running.)
3. Tests of the pieces the cut-over relies on pass:
   ```powershell
   python -X utf8 -m unittest tools/preview/v2/test_phase4.py tools/preview/v2/test_v2.py tools/preview/v2/test_phase3.py
   python -X utf8 -m pytest tools/test_pipeline_supervisor.py tools/tripo_pipeline/test_workflow.py -q
   ```
4. Record what runs now (to compare after step 4):
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
     Where-Object { $_.CommandLine -match 'pipeline_supervisor\.py|view_batch\.py|convert_queue\.py|preview[\\/]serve\.py' } |
     Select-Object ProcessId, CreationDate, CommandLine | Format-List
   ```

## 1. Stop the old site (only it)

Find the process that runs `tools/preview/serve.py` and stop it by PID after reading its command line. Never `Stop-Process -Name python`.

```powershell
$old = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'tools[\\/]preview[\\/]serve\.py' }
$old | Select-Object ProcessId, CreationDate, CommandLine            # exactly one row, the old site
Stop-Process -Id $old.ProcessId                                       # by that PID only
```

The old site has no children that matter (the services were started separately). Check the port is free:
`Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue` prints nothing.

## 2. Start v2 on 8765, detached

```powershell
$logs = "$repo\.codex_jobs\_batch"
foreach ($f in "$logs\preview_v2_win.out.log", "$logs\preview_v2_win.err.log") {
  if (Test-Path $f) { Move-Item $f ($f -replace '\.log$', '.prev.log') -Force }
}
Start-Process -FilePath python -WorkingDirectory $repo -WindowStyle Hidden `
  -ArgumentList '-X','utf8','-u','tools/preview/v2/server.py','--port','8765','--manage-services' `
  -RedirectStandardOutput "$logs\preview_v2_win.out.log" -RedirectStandardError "$logs\preview_v2_win.err.log"
```

Check (give it a few seconds): `Invoke-WebRequest http://127.0.0.1:8765/ -UseBasicParsing | Select-Object StatusCode` is 200,
`$logs\preview_v2_win.err.log` has no traceback, and through Tailscale the same page loads (an unknown host name answers 421: add it to
`allowed_hosts` in `.codex_jobs/_app/settings.json`, which the Queue tab edits).

## 3. v2 adopts the services

Within about 30 seconds (the first tick of the services thread, which exists only with `--manage-services`; it scans processes and can take several
seconds the first time) the Queue tab shows supervisor,
views and convert as **running, adopted**, with the PIDs recorded in step 0.4. Also visible in `.codex_jobs/_app/services/<name>.json`.
Nothing is started or stopped by this. If a service shows `duplicate`, someone started two of it: fix that by hand (v2 never does).

Settings that matter on this PC today (`.codex_jobs/_app/settings.json`, written by the Queue tab; defaults are fine): `tripo: "off"`
(the convert worker keeps `--no-tripo`), `view_parallel: 8`, all three services enabled. The mode stays `pause-conversions` (the user's manual
pause); v2 never changes it by itself.

## 4. Pick up the claim-lock fix: restart the supervisor

`tools/pipeline_supervisor.py` now holds the set's claim lock from before its first folder move until `workflow.py claim` returns (DESIGN_PHASE4 D).
The running supervisor still has the old code. From the Queue tab press **Restart** on the supervisor (it runs
`python tools/pipeline_supervisor.py stop`, waits for the exit, which resumes any frozen processes and removes PAUSE, and starts it again), or by hand:

```powershell
python -X utf8 tools/pipeline_supervisor.py stop
# wait until `python -X utf8 tools/pipeline_supervisor.py status` says "not running", then:
Start-Process -FilePath python -WorkingDirectory $repo -WindowStyle Hidden `
  -ArgumentList '-X','utf8','-u','tools/pipeline_supervisor.py','run' `
  -RedirectStandardOutput "$logs\supervisor_win.out.log" -RedirectStandardError "$logs\supervisor_win.err.log"
```

The supervisor deletes the PAUSE file when it exits, and the convert worker (which naps 30 s while paused) would start a conversion if it looked in
that gap. The Queue tab's **Restart** writes the old PAUSE file straight back after the exit, before starting the new supervisor. A by-hand
restart must do the same: save `Get-Content "$logs\PAUSE" -Raw` before `stop`, and write it back as soon as `status` says "not running"
(`[IO.File]::WriteAllText("$logs\PAUSE", $saved)`), then start. The mode file survives; the new supervisor rewrites PAUSE from it on its first tick.
The view runner and the convert worker are not restarted by this: the view runner has nothing to pick up; the convert worker gets its stop-file
code only when it is restarted (Queue tab **Restart**, which waits for the conversion in flight; leave it until a quiet moment).

## 5. Rollback (any time; the services keep running)

```powershell
$v2 = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'tools[\\/]preview[\\/]v2[\\/]server\.py' -and $_.CommandLine -match '--port 8765' }
$v2 | Select-Object ProcessId, CreationDate, CommandLine              # exactly one row
Stop-Process -Id $v2.ProcessId
Start-Process -FilePath python -WorkingDirectory $repo -WindowStyle Hidden `
  -ArgumentList '-u','tools/preview/serve.py' `
  -RedirectStandardOutput "$logs\preview_win.out.log" -RedirectStandardError "$logs\preview_win.err.log"
```

The old site reads the same folders. v2 decision files (`pipeline_v2.json`, `.codex_jobs/_app/`) are ignored by it. Convert-list lines v2 appended to
`.codex_jobs/_batch/convert_sets.txt` stay (they only mean "convert this set"); remove them by hand if unwanted. Stopping v2 never stops a service.
If the Queue tab had **stopped** a service on purpose, start it again by hand with the commands in `DESIGN_PHASE4.md` section A.

## 6. Optional: start v2 at logon

`tools/preview/v2/install_logon_task.ps1` registers the scheduled task "Imageto3D Preview" (per user, at logon, hidden, repo as working directory,
same command as step 2). It is not run by this procedure; the user decides:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\preview\v2\install_logon_task.ps1 -WhatIf     # show only
powershell -NoProfile -ExecutionPolicy Bypass -File tools\preview\v2\install_logon_task.ps1             # register
powershell -NoProfile -ExecutionPolicy Bypass -File tools\preview\v2\install_logon_task.ps1 -Uninstall  # remove
```
