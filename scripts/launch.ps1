$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonPath = Join-Path $ProjectRoot '.venv/Scripts/python.exe'
$PidFile = Join-Path $ProjectRoot 'work/server.pid'
if (-not (Test-Path -LiteralPath $PythonPath)) { throw 'Run .\scripts\setup.ps1 first' }
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'frontend/dist/index.html'))) { throw 'Build the frontend first' }
if (Test-Path -LiteralPath $PidFile) {
    $RecordedProcessId = [int](Get-Content -LiteralPath $PidFile)
    $ExistingProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$RecordedProcessId" -ErrorAction SilentlyContinue
    if ($ExistingProcess -and $ExistingProcess.CommandLine -like "*$ProjectRoot*" -and $ExistingProcess.CommandLine -like '*nexus.main:*') {
        Write-Host 'This project already owns a server process. Open http://127.0.0.1:8765'
        return
    }
}
New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot 'work') | Out-Null
$ServerProcess = Start-Process -FilePath $PythonPath -ArgumentList @('-m', 'uvicorn', 'nexus.main:create_app', '--factory', '--app-dir', 'backend', '--host', '127.0.0.1', '--port', '8765', '--no-access-log') -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $ProjectRoot 'work/server.log') -RedirectStandardError (Join-Path $ProjectRoot 'work/server-error.log')
$ServerProcess.Id | Set-Content -LiteralPath $PidFile -Encoding ascii
Start-Sleep -Seconds 1
if ($ServerProcess.HasExited) { throw 'Server startup failed. See work/server-error.log; port 8765 may be occupied.' }
Write-Host 'Agent Nexus started: http://127.0.0.1:8765'
Write-Host 'Stop this project server with .\scripts\stop.ps1'
