param(
    [switch]$Browser,
    [switch]$NewWindow,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ($NewWindow) {
    if ($Check) { throw '-Check does not need a new window.' }
    # The user explicitly requests a visible interactive authentication window.
    # Native stdio is inherited; neither PowerShell nor Nexus captures OAuth input.
    $ShellFile = if (Test-Path -LiteralPath (Join-Path $PSHOME 'pwsh.exe')) {
        Join-Path $PSHOME 'pwsh.exe'
    } else {
        Join-Path $PSHOME 'powershell.exe'
    }
    $LoginScript = Join-Path $PSScriptRoot 'gemini-login.ps1'
    $LaunchArguments = @('-NoLogo', '-NoProfile', '-NoExit', '-File', ('"' + $LoginScript + '"'))
    if ($Browser) { $LaunchArguments += '-Browser' }
    Start-Process -FilePath $ShellFile -WorkingDirectory $ProjectRoot -WindowStyle Normal -ArgumentList $LaunchArguments
    return
}

Push-Location -LiteralPath $ProjectRoot
try {
    $LoginMode = if ($Browser) { 'browser' } else { 'manual' }
    $PythonArguments = @('-X', 'utf8', '-u', 'scripts/gemini_login.py', '--mode', $LoginMode)
    if ($Check) { $PythonArguments += '--check' }
    & ./.venv/Scripts/python.exe @PythonArguments
    if ($LASTEXITCODE -eq 2) {
        throw 'Use Windows Terminal / PowerShell and run scripts/gemini-login.ps1 -NewWindow; no login or model request was started.'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Official Gemini login did not finish normally; account login and model connectivity remain unverified.' }
} finally { Pop-Location }
