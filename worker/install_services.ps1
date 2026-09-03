# install_services.ps1 — Registers the workers as Windows services.
#
# The previous deployment docs assumed Linux systemd and journalctl.
# On Windows, NSSM is the closest equivalent: it supervises a plain
# console process, restarts it on exit, starts it at boot before login,
# and rotates stdout/stderr to files.
#
# WHY SERVICES AND NOT TASK SCHEDULER: these are long-lived loops, not
# scheduled jobs. A service restarts within seconds of a crash and runs
# without anyone being logged in — Task Scheduler's restart semantics
# are coarser and its "run whether user is logged on or not" mode
# interacts badly with a Chrome session that needs a real desktop.
#
# NOTE ON CHROME: the extension needs an interactive desktop session to
# run, so Chrome itself stays outside this script. Keep it running in a
# logged-in session (or an always-on RDP session) as you do now. These
# three services only read the folder Chrome writes into, so they are
# unaffected by Chrome restarting.
#
# Usage (elevated PowerShell):
#   choco install nssm         # or download from https://nssm.cc
#   .\install_services.ps1 -PythonExe "C:\Python312\python.exe" -WorkerDir "C:\iv\worker"

param(
    [Parameter(Mandatory=$true)][string]$PythonExe,
    [Parameter(Mandatory=$true)][string]$WorkerDir,
    [string]$LogDir = "C:\iv\logs"
)

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ingest also runs detection and alerting inline, immediately after each
# file lands. Only two services are needed, not three.
$services = @(
    @{ Name = "iv-ingest";   Script = "ingest.py"   },
    @{ Name = "iv-watchdog"; Script = "watchdog.py" }
)

foreach ($s in $services) {
    $name = $s.Name

    if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
        Write-Host "Removing existing service $name..."
        nssm stop $name confirm | Out-Null
        nssm remove $name confirm | Out-Null
        Start-Sleep -Seconds 2
    }

    Write-Host "Installing $name..."
    nssm install $name $PythonExe (Join-Path $WorkerDir $s.Script)
    nssm set $name AppDirectory $WorkerDir

    # -u forces unbuffered stdout, otherwise log lines sit in Python's
    # buffer and the log file looks frozen during an incident — exactly
    # when you need to read it.
    nssm set $name AppParameters "-u $(Join-Path $WorkerDir $s.Script)"

    nssm set $name AppStdout (Join-Path $LogDir "$name.log")
    nssm set $name AppStderr (Join-Path $LogDir "$name.err.log")
    nssm set $name AppRotateFiles 1
    nssm set $name AppRotateBytes 10485760      # 10 MB

    # Restart on any exit, backing off so a misconfiguration (bad key,
    # unreachable Supabase) doesn't spin in a tight crash loop.
    nssm set $name AppExit Default Restart
    nssm set $name AppRestartDelay 15000
    nssm set $name Start SERVICE_AUTO_START

    nssm start $name
    Write-Host "$name installed and started."
}

Write-Host ""
Write-Host "Done. Useful commands:"
Write-Host "  nssm status iv-ingest"
Write-Host "  Get-Content $LogDir\iv-ingest.log -Tail 50 -Wait"
Write-Host "  nssm restart iv-ingest"
