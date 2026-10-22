$ErrorActionPreference = 'Stop'
# An inherited npm_config_* variable makes npm print an "Unknown env config"
# warning on stderr. Under $ErrorActionPreference='Stop' PowerShell treats that
# as a terminating error, so the variable is cleared for this install only.
$env:npm_config_side_effects_cache = $null
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ToolRoot = Join-Path $ProjectRoot 'work/agent-tools/codex'
$CacheRoot = Join-Path $ProjectRoot 'work/npm-cache'
Push-Location -LiteralPath $ProjectRoot
try {
    New-Item -ItemType Directory -Force -Path $ToolRoot | Out-Null
    Copy-Item -LiteralPath 'integrations/codex-cli/package.json' -Destination $ToolRoot -Force
    # Project-scoped npm configuration: the user's global npmrc is not read, and
    # nothing is installed globally. `--ignore-scripts` keeps the package from
    # running install hooks.
    $UserConfig = Join-Path $ProjectRoot 'work/npm-codex-user.npmrc'
    $GlobalConfig = Join-Path $ProjectRoot 'work/npm-codex-global.npmrc'
    'registry=https://registry.npmjs.org/' | Set-Content -LiteralPath $UserConfig -Encoding ascii
    'registry=https://registry.npmjs.org/' | Set-Content -LiteralPath $GlobalConfig -Encoding ascii
    npm install --prefix $ToolRoot --cache $CacheRoot --userconfig $UserConfig --globalconfig $GlobalConfig `
        --ignore-scripts --no-audit --no-fund --save-exact --fetch-retries=1
    if ($LASTEXITCODE -ne 0) { throw 'Project-local Codex CLI dependency installation failed' }
    # Record the resolved lockfile so the version is reproducible.
    Copy-Item -LiteralPath (Join-Path $ToolRoot 'package-lock.json') -Destination 'integrations/codex-cli/package-lock.json' -Force
    Write-Host "Codex CLI installed project-locally at $ToolRoot"
} finally { Pop-Location }
