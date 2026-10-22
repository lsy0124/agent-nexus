$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $ProjectRoot
try {
    if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) { throw 'Run .\scripts\setup.ps1 first' }
    if (-not (Test-Path -LiteralPath 'frontend/dist/index.html')) {
        npm --prefix frontend run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
    }
    Write-Host 'Agent Nexus: http://127.0.0.1:8765  (Ctrl+C to stop)'
    & ./.venv/Scripts/python.exe -m uvicorn nexus.main:create_app --factory --app-dir backend --host 127.0.0.1 --port 8765 --no-access-log
    if ($LASTEXITCODE -ne 0) { throw 'Backend exited with an error (check whether port 8765 is occupied)' }
} finally { Pop-Location }
