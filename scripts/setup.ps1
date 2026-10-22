$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $ProjectRoot
try {
    foreach ($Tool in @('git', 'node', 'npm', 'python')) {
        if (-not (Get-Command $Tool -ErrorAction SilentlyContinue)) { throw "Missing required tool: $Tool" }
    }
    New-Item -ItemType Directory -Force -Path work | Out-Null
    if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed' }
    }
    & ./.venv/Scripts/python.exe -m pip install --cache-dir work/pip-cache -r backend/requirements-lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed' }
    npm ci --prefix frontend --cache work/npm-cache --registry https://registry.npmjs.org --no-fund --no-audit
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed' }
    npm --prefix frontend run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
    Write-Host 'Agent Nexus is ready. Run .\scripts\start.ps1'
} finally { Pop-Location }
