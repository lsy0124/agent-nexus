# Restart only this project's listener, refusing active tasks or uncertain ownership.
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Url = 'http://127.0.0.1:8765'
$listeners = @(Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
    $tasks = Invoke-RestMethod "$Url/api/tasks" -TimeoutSec 15
    if (@($tasks | Where-Object status -eq 'running').Count -gt 0) {
        throw 'A task is running. Refusing to interrupt it.'
    }
    foreach ($collaborationParent in @($tasks | Where-Object { $_.collaboration_parent -eq $_.id })) {
        $plan = Invoke-RestMethod "$Url/api/tasks/$($collaborationParent.id)/collaboration" -TimeoutSec 15
        if ($plan.state -in @('running', 'waiting_review')) {
            throw 'A collaboration queue is armed. Pause it before restarting Nexus.'
        }
    }
    foreach ($ownerId in @($listeners.OwningProcess | Select-Object -Unique)) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerId"
        $line = [string]$process.CommandLine
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ParentProcessId)" -ErrorAction SilentlyContinue
        $parentLine = if ($parent) { [string]$parent.CommandLine } else { '' }
        $projectLauncher = $parentLine -like "*$PythonPath*" -and $parentLine -like '*nexus.main*'
        if ($line -notlike '*nexus.main*' -or ($line -notlike "*$ProjectRoot*" -and -not $projectLauncher)) {
            throw 'Listener ownership is uncertain. No process was stopped.'
        }
        Stop-Process -Id $ownerId -ErrorAction Stop
    }
}
$stamp = Get-Date -Format 'yyyyMMddTHHmmssfff'
$server = Start-Process -FilePath $PythonPath `
    -ArgumentList @('-m','uvicorn','nexus.main:create_app','--factory','--app-dir','backend',
                    '--host','127.0.0.1','--port','8765','--no-access-log') `
    -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $ProjectRoot "work\server-$stamp.log") `
    -RedirectStandardError (Join-Path $ProjectRoot "work\server-$stamp-error.log")
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    Start-Sleep -Milliseconds 500
    try {
        $health = Invoke-RestMethod "$Url/api/health" -TimeoutSec 2
        if ($health.revision -eq 'reliability-audit-1') {
            $listener = Get-NetTCPConnection -LocalPort 8765 -State Listen | Select-Object -First 1
            $listener.OwningProcess | Set-Content -LiteralPath (Join-Path $ProjectRoot 'work/server.pid') -Encoding ascii
            Write-Output "Nexus restarted; PID=$($listener.OwningProcess); revision=$($health.revision)"
            exit 0
        }
    } catch {}
}
throw "Server did not start. Check work/server-$stamp-error.log"
