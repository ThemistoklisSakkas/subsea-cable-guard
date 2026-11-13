# Copies the collector's finished daily files and its heartbeat log from the VM to
# this laptop (into private\eastmed, git-ignored). Only files not already here are
# copied; today's unfinished parts are skipped. Uses the OpenSSH built into Windows.
#
#   .\scripts\pull_from_vm.ps1 -VmHost ubuntu@<public-ip> -Key $HOME\.ssh\oracle_vm.key
param(
    [Parameter(Mandatory = $true)][string]$VmHost,
    [Parameter(Mandatory = $true)][string]$Key,
    [string]$RemoteRepo = "subsea-cable-guard"
)
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path "$PSScriptRoot\..").Path
$ssh = @("-i", $Key, "-o", "BatchMode=yes")

$list = ssh @ssh $VmHost "cd $RemoteRepo && find private/eastmed/ais -maxdepth 2 -name '*.parquet' -not -path '*/parts/*'"
if ($LASTEXITCODE -ne 0) { throw "ssh failed - check host, key and that the VM is running." }

$copied = 0
foreach ($rel in ($list -split "`n" | Where-Object { $_.Trim() })) {
    $rel = $rel.Trim()
    $local = Join-Path $repo ($rel -replace "/", "\")
    if (Test-Path $local) { continue }
    New-Item -ItemType Directory -Force -Path (Split-Path $local) | Out-Null
    scp @ssh "${VmHost}:$RemoteRepo/$rel" $local
    if ($LASTEXITCODE -ne 0) { throw "scp failed for $rel" }
    $copied++
}

# The heartbeat log is small and grows: always refresh it (kept under a separate name
# so it never mixes with a local collector's log).
$logDir = Join-Path $repo "private\eastmed\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
scp @ssh "${VmHost}:$RemoteRepo/private/eastmed/logs/stats.jsonl" (Join-Path $logDir "stats_vm.jsonl")

Write-Host "Copied $copied new daily file(s). Heartbeat log -> private\eastmed\logs\stats_vm.jsonl"
