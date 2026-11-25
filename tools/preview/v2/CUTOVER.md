# Preview app v2: cut-over and rollback (this PC)

DESIGN_PHASE4.md sections G and I, as commands. The old site (`python -u tools/preview/serve.py`, port 8765; Tailscale serve already forwards
to 8765) is replaced by v2 (`python -X utf8 -u tools/preview/v2/server.py --port 8765 --manage-services`). Service management is OFF unless
the server is started with `--manage-services`: only the cut-over server gets the flag (a dev / test server on 8766 never adopts, starts,
stops or signals a service). The prototype on :8790 is `experiments/shell_slots_20261122/serve.py`: it is not part of the cut-over; leave it
running.

The pipeline services (supervisor, view runner, convert worker) are never killed by the cut-over. v2 adopts them; the steps below replace
them in a fixed order, each through the paid-safety interlock of DESIGN_PHASE4.md I: the conversion hold `.codex_jobs/_batch/CONVERT_HOLD`
(no paid conversion step of the CURRENT code starts or continues while it exists) stays in place from the moment v2 starts until the new
supervisor has completed a tick. The order matters (phase 5 review row 17): the live worker and any `convert_job.py` it started run code
from BEFORE the hold existed and only honour PAUSE, which the old supervisor deletes on its way out. So the old paid work is drained FIRST
(step 4), the old view runner is replaced only while it launches nothing (step 5), and only then is the old supervisor restarted (step 6).

Run everything from the repo root in PowerShell:

```powershell
cd F:\godot\blender\characters\imageto3d
$repo = (Get-Location).Path
$logs = "$repo\.codex_jobs\_batch"
```

The user must agree before step 1. Nothing below runs by itself. Never `Stop-Process -Name python`; every stop below goes through the
checker script, which names ONE process, verifies it twice (interpreter command line parsed like `services.identify`, script path resolved
against the process's own working directory = this checkout, creation time unchanged) and refuses otherwise.

## 0. Before

1. Everything is committed (`git status` clean; the generated outputs stay ignored, see the blender-repo-generated-outputs note).
2. Tests of the pieces the cut-over relies on pass:
   ```powershell
   python -X utf8 -m unittest tools/preview/v2/test_v2.py tools/preview/v2/test_phase3.py tools/preview/v2/test_phase4.py tools/preview/v2/test_phase4b.py tools/preview/v2/test_phase5.py
   python -X utf8 -m pytest -q tools/test_pipeline_supervisor.py tools/tripo_pipeline/test_workflow.py tools/test_view_batch_launcher.py tools/test_convert_queue.py
   ```
3. Write the checker script and the `Invoke-Check` helper: run the block of the section *Checker script* at the end of this file.
4. Record what runs now (compare after step 3):
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
     Where-Object { $_.CommandLine -match 'pipeline_supervisor\.py|view_batch\.py|convert_queue\.py|convert_job\.py|preview[\\/](v2[\\/])?serve(r)?\.py' } |
     Select-Object ProcessId, CreationDate, CommandLine | Format-List
   ```
   No other v2 server of THIS checkout may be running (it would hold the writer lock `.codex_jobs\_app\server.lock`, and the new server would
   come up read-only): a `tools/preview/v2/server.py` line whose working directory is this repo must not be in the list.

## 1. Stop the old site (exactly the :8765 listener, checked by the script)

```powershell
Invoke-Check stop-listener 8765 tools/preview/serve.py
```

It stops the listener only when there is exactly ONE, it runs `tools/preview/serve.py` of this checkout (resolved against its own working
directory), and the same process is still the listener a second later. Anything else prints `REFUSED: ...` and stops nothing: look at the
listener by hand and stop here.

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
$queue.convert_hold               # present: the manager wrote the conversion hold at start (DESIGN_PHASE4 I.1), before any tick
(Get-NetTCPConnection -State Listen -LocalPort 8765).OwningProcess   # = $state.server.pid
```

`$logs\preview_v2_win.err.log` has no traceback. Through Tailscale the same page loads; an unknown host name answers 421: add it to
`allowed_hosts` in the Queue tab's settings (it is accepted at once; removing one needs a restart).

## 3. v2 adopts the services

Within about 30 seconds (the first tick of the services thread; the first process scan can take several seconds) the Queue tab shows supervisor,
views and convert as **running, adopted**, with the PIDs of step 0.4 (also in `.codex_jobs/_app/services/<name>.json`). Nothing is started or
stopped by this. If a service shows `duplicate` or `waiting (could not be verified)`, fix that by hand first (v2 never does).

Settings that matter on this PC (`.codex_jobs/_app/settings.json`, edited in the Queue tab; defaults are fine): `tripo: "off"` (the convert
worker keeps `--no-tripo`), `view_parallel: 8`, all three services enabled. The mode stays `pause-conversions` (the user's manual pause); v2
never changes it by itself. If settings.json cannot be read, the last valid settings are kept, and with none v2 starts no service at all.

The conversion hold written at the manager's start is removed by the manager only after a supervisor it has not sent a stop to has written a
tick (`.codex_jobs/_batch/supervisor_tick.json`). The live supervisor of before the cut-over does not write that file, so the hold STAYS
until step 6: no paid step of the current code runs meanwhile.

## 4. Drain the old paid work FIRST (legacy convert worker, I.2)

The live convert worker (and any `convert_job.py` it started) predates CONVERT_HOLD: it only honours PAUSE, which the old supervisor removes
when it exits. It must be gone before step 6.

Press **Stop** on convert in the Queue tab (not Restart: the new worker is started in step 7). The manager:
1. writes / keeps the conversion hold FIRST (`drain_hold` in `.codex_jobs/_app/services/convert.json`; no supervisor tick releases the hold
   while the drain is pending), so every `convert_job.py` started from now on (current code) waits before its paid steps;
2. writes `convert_worker1.stop` naming the worker's pid and creation time (an old worker does not read it);
3. on every tick: waits until no `convert_job.py` of this checkout runs AND the worker's last event in `convert_events.log` (since it started)
   is `PAUSED`, `IDLE` or `LOW DISK` after its last `CONVERT`;
4. then SUSPENDS the worker, checks again while it is frozen (no child process, no `convert_job.py`, no job lock naming it, still asleep in
   its log) and only then terminates it; if anything shows it woke up it is resumed untouched and the stop stays pending. It is never killed
   on a timeout. With the current `pause-conversions` mode the worker sleeps in PAUSED, so this normally completes within a tick or two.

A `convert_job.py` that is running when you press Stop is old code in flight: let it finish by itself (never kill it). Then confirm the drain
with the script, repeating until it succeeds:

```powershell
Invoke-Check drained          # hold: present; convert_job.py: []; convert_queue.py: []   (exit 0)
```

A worker the user started by hand with `--worker N` (N > 1) is not managed by v2: `drained` lists it too; stop it through its own stop file
(`convert_worker<N>.stop`) or wait until it is idle and stop it by hand with the same care before going on.

## 5. Replace the view runner (only while it launches nothing)

The live view runner predates the single-instance lock and the launch reservation (DESIGN_PHASE4 I.5): stopping it between a Codex launch
and its status write would lose that job. Press **Restart** on views in the Queue tab. For such an older runner the manager suspends it and
checks, while it is frozen, that every child process is a `codex_imagegen.sh <job>` whose job its status files already record as running
(the new runner adopts those through `.done` and the job folder) and that it has no other child; only then is it terminated and the new
runner started. If it is launching a job right now the stop stays pending (Queue tab: views "stopping") and the next tick tries again;
running Codex jobs always continue. Afterwards:

```powershell
Invoke-Check views            # view runners: [<new pid>]; older ones: []   (exit 0: it holds view_runner.lock)
```

## 6. Restart the supervisor (claim-lock fix, tick file)

Only after steps 4 and 5. From the Queue tab press **Restart** on the supervisor. The manager:
1. renews the conversion hold (CONVERT_HOLD) BEFORE anything is stopped and names the outgoing supervisor in it (`retiring`: pid + creation
   time),
2. runs `python tools/pipeline_supervisor.py stop` and waits for the exit (frozen processes are resumed, PAUSE is removed by the old process;
   no old worker is left to act on that),
3. puts the old PAUSE file back and starts the new supervisor,
4. removes the hold only after the NEW supervisor completed a tick: a tick of the outgoing supervisor (still running while its stop is
   pending) never releases it, and neither does any supervisor while its stop is pending.

Check in the Queue tab: supervisor running (new pid), `convert_hold` gone after its first tick (about a minute). Never restart the supervisor
by hand around the manager; if it must be done by hand, write CONVERT_HOLD first and let the manager remove it.

## 7. Start the new convert worker

Press **Start** on convert. The new worker honours CONVERT_HOLD, PAUSE and its own stop file (it acknowledges with
`convert_worker1.stopping`, finishes its job, then exits), so it may be started before or after the hold is released.

## 8. Rollback (any time; the services keep running)

```powershell
Invoke-Check stop-listener 8765 tools/preview/v2/server.py
Start-Process -FilePath python -WorkingDirectory $repo -WindowStyle Hidden `
  -ArgumentList '-u','tools/preview/serve.py' `
  -RedirectStandardOutput "$logs\preview_win.out.log" -RedirectStandardError "$logs\preview_win.err.log"
```

Stopping v2 never stops a service. A conversion hold v2 left behind (`.codex_jobs/_batch/CONVERT_HOLD`) keeps conversions waiting: once the
supervisor runs and has ticked, delete it by hand (its `owner_pid` is the stopped v2 server). The old site reads the same folders; v2 decision
files (`pipeline_v2.json`, `.codex_jobs/_app/`) are ignored by it. Convert-list lines v2 appended to `.codex_jobs/_batch/convert_sets.txt` are
exact set ids (never wildcards) and stay; remove them by hand if unwanted. If the Queue tab had **stopped** a service on purpose, start it again
by hand with the commands in DESIGN_PHASE4.md section A.

## 9. Optional: start v2 at logon

`tools/preview/v2/install_logon_task.ps1` registers the scheduled task "Imageto3D Preview" (per user, at logon, hidden, repo as working
directory, same command as step 2). Each run first checks the :8765 listener: when anything already listens (normally the v2 server of an
earlier trigger) it only notes that and does nothing (no log rotation, no second launch). It is not run by this procedure; the user decides:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\preview\v2\install_logon_task.ps1 -WhatIf     # show only
powershell -NoProfile -ExecutionPolicy Bypass -File tools\preview\v2\install_logon_task.ps1             # register
powershell -NoProfile -ExecutionPolicy Bypass -File tools\preview\v2\install_logon_task.ps1 -Uninstall  # remove
```

## Checker script

Run from the repo root (step 0.3). It reuses `tools/preview/v2/services.py`, the manager's own process parser; the here-string's
closing `'@` must stay at column 0.

```powershell
$check = Join-Path $env:TEMP 'imageto3d_cutover_check.py'
[IO.File]::WriteAllText($check, @'
import os, sys, time
repo = os.path.realpath(sys.argv[1])
sys.path.insert(0, os.path.join(repo, 'tools', 'preview', 'v2'))
import psutil
import services

def norm(path):
    return os.path.normcase(os.path.realpath(path))

def script_of(proc):
    """(the python script this process runs, resolved against its own cwd) or None."""
    cmd = proc.cmdline()
    if not cmd or not os.path.basename(cmd[0]).lower().startswith('python'):
        return None
    found, _args = services.python_script(cmd)
    if not found:
        return None
    return norm(found if os.path.isabs(found) else os.path.join(proc.cwd(), found))

def running(rel):
    """Processes of THIS checkout running tools/<rel> (any arguments, any worker number)."""
    want, out = norm(os.path.join(repo, rel)), []
    for proc in psutil.process_iter():
        try:
            if script_of(proc) == want:
                out.append(proc)
        except (psutil.Error, OSError):
            continue
    return out

def listeners(port):
    return sorted({c.pid for c in psutil.net_connections('tcp') if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port and c.pid})

what = sys.argv[2]
if what == 'stop-listener':                # stop-listener <port> <script>: exactly one listener, running <script> of this checkout
    port, rel = int(sys.argv[3]), sys.argv[4]
    pids = listeners(port)
    if len(pids) != 1:
        sys.exit(f'REFUSED: {len(pids)} listener(s) on port {port} {pids}; exactly one expected')
    proc = psutil.Process(pids[0])
    first, created = script_of(proc), proc.create_time()
    if first != norm(os.path.join(repo, rel)):
        sys.exit(f'REFUSED: pid {pids[0]} runs {proc.cmdline()} (script {first}), not {rel} of {repo}')
    time.sleep(1.0)
    again = psutil.Process(pids[0])
    if listeners(port) != pids or abs(again.create_time() - created) > 1 or script_of(again) != first:
        sys.exit('REFUSED: the listener changed between the two checks; look again')
    again.terminate()
    again.wait(30)
    if listeners(port):
        sys.exit(f'port {port} still has a listener')
    print(f'stopped pid {pids[0]} ({rel}); port {port} is free')
elif what == 'drained':                    # the old paid work is gone: hold present, no convert_job.py, no convert worker of this checkout
    hold = services.read_hold(repo)
    jobs = running('tools/tripo_pipeline/convert_job.py')
    workers = running('tools/convert_queue.py')
    print(f'hold: {"present" if hold is not None else "MISSING"}; convert_job.py: {[p.pid for p in jobs]}; convert_queue.py: {[p.pid for p in workers]}')
    sys.exit(0 if hold is not None and not jobs and not workers else 1)
elif what == 'views':                      # exactly one view runner, a current one (launch reservation, holds view_runner.lock)
    runners = services.scan(repo, ['views'])['views']
    old = [r['pid'] for r in runners if services.legacy_runner(repo, r)]
    print(f'view runners: {[r["pid"] for r in runners]}; older ones: {old}')
    sys.exit(0 if len(runners) == 1 and not old else 1)
else:
    sys.exit(f'unknown check {what}')
'@)
function Invoke-Check { python -X utf8 $check $repo @args; $LASTEXITCODE -eq 0 }
```
