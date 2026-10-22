$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ToolRoot = Join-Path $ProjectRoot 'work/agent-tools/gemini'
$CacheRoot = Join-Path $ProjectRoot 'work/npm-cache'
Push-Location -LiteralPath $ProjectRoot
try {
    New-Item -ItemType Directory -Force -Path $ToolRoot | Out-Null
    Copy-Item -LiteralPath 'integrations/gemini-cli/package.json','integrations/gemini-cli/package-lock.json' -Destination $ToolRoot
    $UserConfig = Join-Path $ProjectRoot 'work/npm-gemini-user.npmrc'
    $GlobalConfig = Join-Path $ProjectRoot 'work/npm-gemini-global.npmrc'
    'registry=https://registry.npmjs.org/' | Set-Content -LiteralPath $UserConfig -Encoding ascii
    'registry=https://registry.npmjs.org/' | Set-Content -LiteralPath $GlobalConfig -Encoding ascii
    npm ci --prefix $ToolRoot --cache $CacheRoot --userconfig $UserConfig --globalconfig $GlobalConfig --ignore-scripts --no-audit --no-fund --fetch-retries=0
    if ($LASTEXITCODE -ne 0) { throw 'Project-local Gemini CLI dependency installation failed' }
} finally { Pop-Location }
