<#
.SYNOPSIS
  Registers (or removes) the Windows scheduled task "Imageto3D Preview": it starts the v2 preview server at logon.

.DESCRIPTION
  Install (default): a per-user task, trigger "at log on of <you>", that runs, with the repository as working directory and no visible
  window:
      python -X utf8 -u tools/preview/v2/server.py --port 8765 --manage-services
  stdout / stderr go to .codex_jobs\_batch\preview_v2_win.out.log / .err.log (the previous run's files are kept as *.prev.log).
  Every run first checks the port: when something already listens on it (normally the v2 server of an earlier trigger, which outlives the
  short-lived launcher this task tracks), it only appends a line to .codex_jobs\_batch\preview_v2_logon.log: no log rotation, no second server.
  The task never stops the pipeline services: the server only ADOPTS the supervisor / view runner / convert worker it finds, and a server
  restart (or this task's removal) leaves them running.

  -Uninstall removes the task. A server that is already running keeps running.
  -WhatIf prints what would happen and changes nothing. This script is never run by the cut-over steps; the user decides.

.PARAMETER Uninstall   Remove the task instead of registering it.
.PARAMETER Port        Port of the server (default 8765, the one Tailscale serve forwards to).
.PARAMETER Python      Full path of python.exe (default: the first `python` on PATH).

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\preview\v2\install_logon_task.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\preview\v2\install_logon_task.ps1 -Uninstall
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Uninstall,
    [int]$Port = 8765,
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'
$TaskName = 'Imageto3D Preview'
$Repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..\..')).Path     # tools\preview\v2 -> the repository root
$Server = Join-Path $Repo 'tools\preview\v2\server.py'

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($Uninstall) {
    if (-not $existing) {
        Write-Host "Scheduled task '$TaskName' is not registered; nothing to remove."
        return
    }
    Write-Host "Removing scheduled task '$TaskName'."
    if ($PSCmdlet.ShouldProcess($TaskName, 'Unregister-ScheduledTask')) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host 'Removed. A preview server that is running now keeps running; the pipeline services are not touched.'
    }
    return
}

if (-not (Test-Path -LiteralPath $Server)) { throw "Server script not found: $Server" }
if (-not $Python) {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if (-not $found) { throw 'python was not found on PATH; pass -Python <full path to python.exe>.' }
    $Python = $found.Source
}
if (-not (Test-Path -LiteralPath $Python)) { throw "python.exe not found: $Python" }
if ($Port -lt 1 -or $Port -gt 65535) { throw "Invalid port $Port" }

function Quote([string]$text) { return "'" + $text.Replace("'", "''") + "'" }      # PowerShell single-quoted literal

$logs = Join-Path $Repo '.codex_jobs\_batch'
$out = Join-Path $logs 'preview_v2_win.out.log'
$err = Join-Path $logs 'preview_v2_win.err.log'
$note = Join-Path $logs 'preview_v2_logon.log'
$serverArgs = "'-X','utf8','-u','tools/preview/v2/server.py','--port','$Port','--manage-services'"
# The task's own process is a short-lived hidden PowerShell that rotates the logs and starts python hidden and detached from the task.
# It FIRST checks the port (review F17): when anything already listens on it (normally the v2 server of an earlier trigger, which outlives
# this launcher), it only notes that in preview_v2_logon.log and exits: no log rotation, no second server.
$launch = @(
    "Set-Location -LiteralPath $(Quote $Repo)",
    "New-Item -ItemType Directory -Force -Path $(Quote $logs) | Out-Null",
    "`$busy = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)",
    "if (`$busy.Count -gt 0) { Add-Content -LiteralPath $(Quote $note) -Value ((Get-Date -Format s) + ' port $Port already has a listener (pid ' + `$busy[0].OwningProcess + '); nothing rotated or started'); exit 0 }",
    "foreach (`$f in @($(Quote $out), $(Quote $err))) { if (Test-Path -LiteralPath `$f) { Move-Item -LiteralPath `$f -Destination (`$f -replace '\.log$', '.prev.log') -Force } }",
    "Start-Process -FilePath $(Quote $Python) -ArgumentList $serverArgs -WorkingDirectory $(Quote $Repo) -WindowStyle Hidden -RedirectStandardOutput $(Quote $out) -RedirectStandardError $(Quote $err)"
) -join '; '
$argument = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -Command "' + $launch.Replace('"', '\"') + '"'

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argument -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Write-Host "Task name   : $TaskName$(if ($existing) { ' (already registered: it will be replaced)' })"
Write-Host "Runs as     : $user, at logon, no window, working directory $Repo"
Write-Host "Command     : $Python -X utf8 -u tools/preview/v2/server.py --port $Port --manage-services"
Write-Host "Logs        : $out / $err (previous run kept as *.prev.log)"
Write-Host 'Services    : untouched (the server adopts the supervisor, view runner and convert worker it finds)'
if ($PSCmdlet.ShouldProcess($TaskName, 'Register-ScheduledTask')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
        -Description 'Starts the Imageto3D preview app v2 (tools/preview/v2/server.py) at logon.' -Force | Out-Null
    Write-Host "Registered. Start it now with: Start-ScheduledTask -TaskName '$TaskName'   (remove it with -Uninstall)"
}
