# Preview app v2: cut-over and rollback (this PC)

DESIGN_PHASE4.md sections G and I, as commands. The old site (`python -u tools/preview/serve.py`, port 8765; Tailscale serve already forwards
to 8765) is replaced by v2 (`python -X utf8 -u tools/preview/v2/server.py --port 8765 --manage-services`). Service management is OFF unless
the server is started with `--manage-services`: only the cut-over server gets the flag (a dev / test server on 8766 never adopts, starts,
stops or signals a service). The prototype on :8790 is `experiments/shell_slots_20261122/serve.py`: it is not part of steps 1-8; leave it
running until step 9, which removes its files.

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
worker keeps `--no-tripo`; since 2026-11-26 the Queue tab also offers `auto`, `pixal3d` = `--backend pixal3d` and `auto_pixal3d` = `--fallback pixal3d`, and saving a changed value restarts a running convert worker after its job), `view_parallel: 8`, all three services enabled. The mode stays `pause-conversions` (the user's manual pause); v2
never changes it by itself. If settings.json cannot be read, the last valid settings are kept, and with none v2 starts no service at all.

The conversion hold written at the manager's start is removed by the manager only after a supervisor it has not sent a stop to has written a
tick (`.codex_jobs/_batch/supervisor_tick.json`). The live supervisor of before the cut-over does not write that file, so the hold STAYS
until step 6: no paid step of the current code runs meanwhile.

## 4. Drain the old paid work FIRST (legacy convert worker, I.2)

The live convert worker (and any `convert_job.py` it started) predates CONVERT_HOLD: it only honours PAUSE, which the old supervisor removes
when it exits. It must be gone before step 6.

Stopping intake and draining a job are two separate things (DESIGN_PHASE4 I.10). The old worker launches whatever `convert_job.py` is on
disk, so a child it started after the code update is CURRENT code: it waits at its paid gate for the very hold the drain keeps. Such a child
is asked to leave; it is never waited for (that was a deadlock) and never killed.

Press **Stop** on convert in the Queue tab (not Restart: the new worker is started in step 7). The manager:
1. writes / keeps the conversion hold FIRST (`drain_hold` in `.codex_jobs/_app/services/convert.json`; no supervisor tick releases the hold
   while the drain is pending): intake is stopped, every `convert_job.py` of the current code waits before its paid steps;
2. THEN writes `.codex_jobs/_batch/CONVERT_DRAIN`: every `convert_job.py` that is blocked at its paid gate (it is listed in
   `.codex_jobs/_batch/gate_wait/<pid>.json`; nothing paid in flight) releases its module lock, marks the module `deferred` in the job record
   and exits 76 within about 15 s, leaving a note in `.codex_jobs/_batch/drain_exits/`. Nothing is counted as a failure by current code;
3. writes `convert_worker1.stop` naming the worker's pid and creation time (an old worker does not read it);
4. on every tick: waits until no `convert_job.py` of this checkout runs AND the worker's last event in `convert_events.log` (since it started)
   is `PAUSED`, `IDLE` or `LOW DISK` after its last `CONVERT`; then SUSPENDS the worker (the suspension is recorded first in
   `.codex_jobs/_app/services/suspensions.json`, so a manager restart resumes it), checks again while it is frozen (no child process, no
   `convert_job.py`, no job lock naming it, still asleep in its log) and only then terminates it; if anything shows it woke up it is resumed
   untouched and the stop stays pending. It is never killed on a timeout;
5. an old worker that KEEPS TAKING JOBS (it does not sleep on the hold, or it counts the exit 76 of step 2 as a failure and tries the next
   job) cannot do paid work: each child it starts only reaches the gate and leaves. It is suspended and terminated while it has NO child
   process and its last event is one after which it only picks the next job (`CONVERT`, `FAILED`, `PAUSED`, `IDLE`, ...; never while it
   finishes or offloads a set). A child doing real work (a collect / reduction in flight) is always waited for;
6. when the worker is gone: restores the `.codex_jobs/_batch/convert_locks/<job>.fails` counts the old worker wrote for the exits of
   step 2 (listed in the Queue event log, `.codex_jobs/_batch/convert_drain.log` and `drain_restored` of the convert record), removes the
   job locks naming the stopped worker and deletes `CONVERT_DRAIN`. `PAUSE` (your pause mode) is never touched in any step.

With the current `pause-conversions` mode the worker sleeps in PAUSED and this normally completes within a tick or two. A `convert_job.py`
that is past its gate when you press Stop (a conversion in flight) finishes by itself: never kill it. The same goes for a `convert_job.py`
that was started BEFORE the code was updated (it writes no `gate_wait` file and cannot leave for the drain): check before step 2 that none
runs (`Invoke-Check drained` lists them); if one does, let it finish before the cut-over. Then confirm the drain with the script,
repeating until it succeeds:

```powershell
Invoke-Check drained          # hold: present; drain: none; convert_job.py: []; convert_queue.py: []; suspended: []   (exit 0)
```

`drain: pending` means the manager is still at steps 4 / 5 (see the convert service's message in the Queue tab); `gate waiters` in its
output are leaving by themselves. A job whose module shows `deferred` in its `.tripo-job.json` simply continues when the new worker (step 7)
takes it; no credit was spent for it and its failure count is what it was before.

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

Stopping v2 never stops a service. A conversion hold v2 left behind (`.codex_jobs/_batch/CONVERT_HOLD`) keeps conversions waiting. Do NOT
delete it on sight. First ask the script whether it is safe (it changes nothing):

```powershell
Invoke-Check rollback         # exit 0 (SAFE) only when the hold may be removed by hand; otherwise REFUSED and what is outstanding
```

It refuses while any of these holds, and names it:
- a drain is outstanding: `CONVERT_DRAIN` exists, the convert record still has a pending legacy stop (`drain_hold`), a recorded suspended
  process (`suspensions.json`) still exists, or a `convert_job.py` / an old-code `convert_queue.py` of this checkout still runs (EVERY
  convert worker counts, whatever its `--worker N`: the same enumeration as `drained`, each worker's code age tested on its own; a
  python process that may be one but cannot be resolved, because its command line or its working directory cannot be read, counts as
  running too: the check never answers SAFE on a guess. Run the check as the user who owns the workers, or stop that process). Finish
  step 4 with v2 running (start v2 again as in step 2: at start it resumes anything a stopped manager left suspended and finishes or
  continues the drain), or wait for those processes to exit by themselves. If v2 will not be started again and NO convert worker runs,
  `CONVERT_DRAIN` may be deleted by hand together with the hold; with an old worker still running it must stay;
- the running supervisor is not the intended generation: no supervisor runs, more than one runs, the one that runs is named in the hold's
  `retiring` list or has a stop pending in `.codex_jobs/_app/services/supervisor.json` (a stop was sent to it: wait until it exits and start
  the supervisor again), or it has not completed a tick since the hold was written (`supervisor_tick.json` names another pid or an earlier
  time; the pre-cut-over supervisor code writes no tick file at all: restart it on the current code first).

Only when `rollback` exits 0 delete `.codex_jobs/_batch/CONVERT_HOLD` (its `owner_pid` is the stopped v2 server). The old site reads the same folders; v2 decision
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

## 10. Merge the phase 6 branch (done 2026-11-26, after the cut-over)

Branch `pipeline-v2-phase6` retires the old site's UI and the prototype:
- the old site's files go: `tools/preview/app.js`, `index.html`, `style.css`, `fitsym.js`, `monsters.js`, `test_fitsym.mjs` and
  `preview.sh`;
- `serve.py` loses its HTTP handler and becomes the library v2 imports (`main()` prints how to start v2 and exits 2);
- the prototype's code and pages in `experiments/shell_slots_20261122/` go (its `out/` data and README stay);
- AGENTS.md, CLAUDE.md and the READMEs are updated for v2.

Order:
1. Stop exactly the prototype's listener first (`Invoke-Check stop-listener 8790 experiments/shell_slots_20261122/serve.py`).
2. `git tag pre-phase6-merge`.
3. `git merge --no-ff pipeline-v2-phase6`.
4. Run `python -X utf8 -m unittest tools/preview/test_serve.py tools/preview/test_monsters.py`.

v2 keeps running: it loaded `serve.py` at start, and the functions it uses are unchanged. The services are not touched.

**Rollback of this step only** restores just the retired files, then starts the old site with section 8's command:
```powershell
git checkout pre-phase6-merge -- tools/preview/serve.py tools/preview/test_serve.py tools/preview/app.js tools/preview/index.html tools/preview/style.css tools/preview/fitsym.js tools/preview/monsters.js tools/preview/test_fitsym.mjs tools/preview/preview.sh experiments/shell_slots_20261122
```
Never restore all of `tools/preview` from an older commit: that would undo the v2 and paid-safety fixes made since.

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

UNRESOLVED = {}   # rel -> ['pid N ...']: candidates of running(rel) that could not be resolved; a check that relies on rel refuses

def running(rel):
    """Processes of THIS checkout running tools/<rel> (any arguments, any worker number). A python process that may be one but
    cannot be resolved (its command line is unreadable, or it names a script of that file name by a relative path and its working
    directory cannot be read) is NOT skipped: it is listed in UNRESOLVED[rel], and the caller refuses (never SAFE on a guess)."""
    want, out, unknown = norm(os.path.join(repo, rel)), [], []
    base = os.path.basename(rel).lower()
    for proc in psutil.process_iter():
        try:
            name = (proc.name() or '').lower()
            if not name.startswith('python'):
                continue
            try:
                cmd = proc.cmdline()
            except psutil.NoSuchProcess:
                raise
            except (psutil.Error, OSError) as error:
                unknown.append(f'pid {proc.pid} ({name}): its command line cannot be read ({type(error).__name__})')
                continue
            found, _args = services.python_script(cmd) if cmd else (None, None)
            if not found:
                continue
            if os.path.isabs(found):
                path = norm(found)
            elif os.path.basename(found).lower() != base:
                continue                   # another script, wherever it runs
            else:
                try:
                    path = norm(os.path.join(proc.cwd(), found))
                except psutil.NoSuchProcess:
                    raise
                except (psutil.Error, OSError) as error:
                    unknown.append(f'pid {proc.pid} runs {found} from a working directory that cannot be read ({type(error).__name__})')
                    continue
            if path == want:
                out.append(proc)
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            continue                       # not even its name can be read: not a python process of this user
    UNRESOLVED[rel] = unknown
    return out

def unresolved(*rels):
    return [text for rel in rels for text in UNRESOLVED.get(rel, [])]

def workers():
    """EVERY convert worker of this checkout (`--worker N` for any N, or none): the one enumeration both `drained` and `rollback` use."""
    return running('tools/convert_queue.py')

def old_workers():
    """pids of the convert workers (any worker number) that run code older than the current tools/convert_queue.py
    (services.legacy_worker: the per-worker code-age test); a worker whose start time cannot be read counts as old."""
    out = []
    for proc in workers():
        try:
            inst = {'pid': proc.pid, 'create_time': proc.create_time()}
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            out.append(proc.pid)
            continue
        if services.legacy_worker(repo, inst):
            out.append(proc.pid)
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
    try:
        first, created = script_of(proc), proc.create_time()
    except (psutil.Error, OSError) as error:
        sys.exit(f'REFUSED: the listener pid {pids[0]} cannot be resolved ({type(error).__name__}: {error}); nothing was stopped')
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
elif what == 'drained':                    # the old paid work is gone: hold present, drain finished, no convert_job.py, no convert worker
    hold = services.read_hold(repo)
    jobs = running('tools/tripo_pipeline/convert_job.py')
    queue = workers()
    conv = services.read_record(repo, 'convert')
    pending = services.read_drain(repo) is not None or bool(conv.get('stop_requested') and conv.get('drain_hold'))
    frozen = [i['pid'] for i in services.read_suspensions(repo) if psutil.pid_exists(i['pid'])]
    waiters = [w['pid'] for w in services.gate_waiters(repo, clean=False)]
    unknown = unresolved('tools/tripo_pipeline/convert_job.py', 'tools/convert_queue.py')
    print(f'hold: {"present" if hold is not None else "MISSING"}; drain: {"pending" if pending else "none"}; gate waiters: {waiters}; '
          f'convert_job.py: {[p.pid for p in jobs]}; convert_queue.py: {[p.pid for p in queue]}; suspended: {frozen}')
    if unknown:
        print('REFUSED: not drained while a possible convert process cannot be resolved:\n  - ' + '\n  - '.join(unknown))
    sys.exit(0 if hold is not None and not pending and not jobs and not queue and not frozen and not unknown else 1)
elif what == 'rollback':                   # read-only: may a conversion hold left by a stopped v2 be removed by hand?
    hold = services.read_hold(repo)
    if hold is None:
        print('no CONVERT_HOLD: nothing to remove')
        sys.exit(0)
    why = []
    if services._process_alive(hold.get('owner_pid'), hold.get('owner_create_time')):
        why.append(f'its owner (pid {hold.get("owner_pid")}) still runs: that server manages the hold')
    conv = services.read_record(repo, 'convert')
    if services.read_drain(repo) is not None:
        why.append('a drain is outstanding: CONVERT_DRAIN exists')
    if conv.get('stop_requested') and conv.get('drain_hold'):
        why.append('a drain is outstanding: the convert record has a pending legacy stop')
    frozen = [i['pid'] for i in services.read_suspensions(repo) if psutil.pid_exists(i['pid'])]
    if frozen:
        why.append(f'a recorded suspended process still exists {frozen}: start v2 again, it resumes it')
    jobs = [p.pid for p in running('tools/tripo_pipeline/convert_job.py')]
    if jobs:
        why.append(f'convert_job.py still runs {jobs}')
    old = old_workers()                    # every worker number, not only the managed worker 1
    if old:
        why.append(f'a convert worker of older code still runs {old} (any --worker number counts)')
    for text in unresolved('tools/tripo_pipeline/convert_job.py', 'tools/convert_queue.py'):
        why.append(f'a possible convert process cannot be resolved, so it counts as running: {text}')
    sups = services.scan(repo, ['supervisor'])['supervisor']
    if len(sups) != 1:
        why.append(f'{len(sups)} supervisors run; exactly one, of the intended generation, is needed')
    else:
        sup = sups[0]
        if any(services._same_process(r, sup) for r in services._retiring(hold)):
            why.append(f'supervisor pid {sup["pid"]} is an outgoing generation (a stop was sent to it)')
        rec = services.read_record(repo, 'supervisor')
        if rec.get('stop_requested') and services._same_process(rec, sup):
            why.append(f'a stop of supervisor pid {sup["pid"]} is still pending')
        if not services.supervisor_ticked(repo, sup, services._hold_time(hold)):
            why.append(f'supervisor pid {sup["pid"]} has not completed a tick since the hold was written')
    print('SAFE: the supervisor is the intended generation and ticked after the hold; no drain is outstanding. CONVERT_HOLD may be deleted.'
          if not why else 'REFUSED: keep the hold:\n  - ' + '\n  - '.join(why))
    sys.exit(1 if why else 0)
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
