$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $ProjectRoot
try {
    # npm prints environment-driven warnings on stderr (for example an unknown
    # `npm_config_*` variable). Those are warnings, not failures, so the exit
    # code -- not the presence of stderr output -- decides the result here.
    $env:npm_config_side_effects_cache = $null
    & ./.venv/Scripts/python.exe -m pytest
    if ($LASTEXITCODE -ne 0) { throw 'Backend tests failed' }
    npm --prefix frontend test
    if ($LASTEXITCODE -ne 0) { throw 'Frontend tests failed' }
    npm --prefix frontend run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend type check/build failed' }
} finally { Pop-Location }
