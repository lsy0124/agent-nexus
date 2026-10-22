$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $ProjectRoot
$BackendProcess = $null
try {
    if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) { throw 'Run .\scripts\setup.ps1 first' }
    $PythonPath = Join-Path $ProjectRoot '.venv/Scripts/python.exe'
    $BackendProcess = Start-Process -FilePath $PythonPath -ArgumentList @('-m', 'uvicorn', 'nexus.main:create_app', '--factory', '--app-dir', 'backend', '--host', '127.0.0.1', '--port', '8765', '--no-access-log') -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $ProjectRoot 'work/dev-backend.log') -RedirectStandardError (Join-Path $ProjectRoot 'work/dev-backend-error.log')
    Start-Sleep -Seconds 1
    if ($BackendProcess.HasExited) { throw 'Backend startup failed. See work/dev-backend-error.log; port 8765 may be occupied.' }
    Write-Host 'Development console: http://127.0.0.1:5173 (Ctrl+C to stop both servers)'
    npm --prefix frontend run dev
} finally {
    if ($BackendProcess -and -not $BackendProcess.HasExited) { Stop-Process -Id $BackendProcess.Id -ErrorAction SilentlyContinue }
    Pop-Location
}
