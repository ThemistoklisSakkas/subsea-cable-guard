# Registers the AIS collector as a Windows scheduled task for the current user.
# Run from the repo root in PowerShell:  .\scripts\install_collector_task.ps1
# Remove with:  Unregister-ScheduledTask -TaskName "CableGuard AIS Collector" -Confirm:$false

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path "$PSScriptRoot\..").Path
$pythonw = Join-Path $repo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Missing $pythonw - create the venv first (see README)." }
if (-not (Test-Path (Join-Path $repo ".env"))) { throw "Missing .env with AISSTREAM_API_KEY." }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m cableguard collect" `
    -WorkingDirectory $repo

# Start at logon, and re-check every 15 minutes: if the collector has died, the
# task starts it again; if it is running, the new start is ignored (and the
# collector's own single-instance lock refuses a duplicate anyway).
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$every15 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 15)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "CableGuard AIS Collector" -Action $action `
    -Trigger @($atLogon, $every15) -Settings $settings `
    -Description "Subsea Cable Guard: real-time AIS collector (aisstream.io)" -Force | Out-Null

Start-ScheduledTask -TaskName "CableGuard AIS Collector"
Write-Host "Registered and started. Check with: .venv\Scripts\python.exe -m cableguard status"
