# Real acceptance for the desktop launcher entry points.
#
# It exercises the actual scripts a user double-clicks: syntax, cold start,
# idempotent second start, refusal to stop a process it cannot attribute to this
# project, and stop. It reports what happened instead of asserting silently.
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Console = Join-Path $PSScriptRoot 'console.ps1'
$Stop = Join-Path $PSScriptRoot 'stop.ps1'
$Url = 'http://127.0.0.1:8765'
$results = @()

function Record([string]$Name, [bool]$Passed, [string]$Detail) {
    $script:results += [pscustomobject]@{ Name = $Name; Passed = $Passed; Detail = $Detail }
    $colour = if ($Passed) { 'Green' } else { 'Red' }
    Write-Host ("  [{0}] {1} — {2}" -f $(if ($Passed) { 'PASS' } else { 'FAIL' }), $Name, $Detail) -ForegroundColor $colour
}

function Test-Health {
    try {
        $response = Invoke-WebRequest -Uri "$Url/api/health" -TimeoutSec 3 -UseBasicParsing
        return ($response.StatusCode -eq 200) -and ($response.Content -match '"storage"\s*:\s*"sqlite"')
    } catch { return $false }
}

function Get-Listener {
    Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
}

function Invoke-Script([string]$Path, [int]$TimeoutSeconds = 90) {
    # The script may block while it owns a server, so it is bounded here.
    $process = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Path) `
        -PassThru -WindowStyle Hidden
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        return 124
    }
    return $process.ExitCode
}

Write-Host ''
Write-Host '  Agent Nexus 桌面启动器验收' -ForegroundColor Cyan
Write-Host ''

# 0 — Windows PowerShell 5.1 reads a BOM-less .ps1 with the local code page, which
# corrupts the Chinese text and breaks parsing. Enforce the BOM before testing.
$bom = New-Object System.Text.UTF8Encoding($true)
$noBom = New-Object System.Text.UTF8Encoding($false)
foreach ($file in @('console.ps1', 'stop.ps1')) {
    $path = Join-Path $PSScriptRoot $file
    $bytes = [System.IO.File]::ReadAllBytes($path)
    if (-not ($bytes.Length -ge 3 -and $bytes[0] -eq 239 -and $bytes[1] -eq 187 -and $bytes[2] -eq 191)) {
        [System.IO.File]::WriteAllText($path, [System.IO.File]::ReadAllText($path, $noBom), $bom)
        Write-Host "  已为 $file 补写 UTF-8 BOM（Windows PowerShell 5.1 需要）" -ForegroundColor Yellow
    }
}

# 1 — syntax
foreach ($file in @('console.ps1', 'stop.ps1')) {
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $file), [ref]$null, [ref]$errors)
    Record "语法：$file" (($errors | Measure-Object).Count -eq 0) "$(($errors | Measure-Object).Count) 个错误"
}

# 2 — cold start through the real console.ps1
Get-Listener | ForEach-Object {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)" -ErrorAction SilentlyContinue
    $ownerCommandLine = if ($owner) { [string]$owner.CommandLine } else { '' }
    $ownerImage = if ($owner -and $owner.ExecutablePath) { [string]$owner.ExecutablePath } else { '' }
    $isOurServer = ($ownerCommandLine -like '*nexus.main*') -or ($ownerImage -like (Join-Path $ProjectRoot '.venv\Scripts\*'))
    if ($isOurServer) { Stop-Process -Id $_.OwningProcess -Force }
    else { Write-Host "  保留端口占用者 PID $($_.OwningProcess)（无法确认属于本项目）" -ForegroundColor Yellow }
}
Remove-Item -LiteralPath (Join-Path $ProjectRoot 'work/server.pid') -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
$before = (Get-Listener | Measure-Object).Count
Record '停止旧服务' ($before -eq 0) "剩余监听 $before 个"

$started = Get-Date
$exit = Invoke-Script $Console 90
$seconds = [int]((Get-Date) - $started).TotalSeconds
$healthy = Test-Health
Record '冷启动：服务就绪' $healthy "console.ps1 退出码 $exit，用时 ${seconds}s"
$pidFile = Join-Path $ProjectRoot 'work/server.pid'
$recorded = if (Test-Path $pidFile) { (Get-Content $pidFile -Raw).Trim() } else { '' }
$listener = Get-Listener
Record '冷启动：PID 文件与实际监听一致' `
    (($listener | Measure-Object).Count -gt 0 -and "$($listener[0].OwningProcess)" -eq $recorded) `
    "server.pid=$recorded，监听=$($listener[0].OwningProcess)"

# 3 — the console must also render its landing page
try {
    $page = Invoke-WebRequest -Uri $Url -TimeoutSec 5 -UseBasicParsing
    Record '控制台首页可访问' ($page.StatusCode -eq 200 -and $page.Content -match 'agent') "HTML $($page.RawContentLength) 字节"
} catch { Record '控制台首页可访问' $false $_.Exception.Message }

# 4 — second start reuses the running server instead of starting a second one
$before = (Get-Listener | Measure-Object).Count
$exit = Invoke-Script $Console 60
$after = Get-Listener
Record '重复启动不会起第二个服务' `
    ((($after | Measure-Object).Count -eq $before) -and ("$($after[0].OwningProcess)" -eq $recorded)) `
    "退出码 $exit，监听 PID 仍为 $($after[0].OwningProcess)"

# 5 — stop must refuse a process it cannot attribute to this project
$decoy = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoLogo', '-NoProfile', '-Command', 'Start-Sleep -Seconds 20') -PassThru -WindowStyle Hidden
$ownership = & {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($decoy.Id)"
    $imagePath = [string]$process.ExecutablePath
    $inProject = $imagePath -like "$ProjectRoot*"
    $isNexus = $imagePath -like (Join-Path $ProjectRoot '.venv\Scripts\*')
    [pscustomobject]@{ InProject = $inProject; IsNexus = $isNexus }
}
Record '归属判定拒绝无关进程' ((-not $ownership.InProject) -and (-not $ownership.IsNexus)) `
    "decoy PID $($decoy.Id) 镜像 $((Get-CimInstance Win32_Process -Filter "ProcessId=$($decoy.Id)").ExecutablePath)"
Stop-Process -Id $decoy.Id -Force -ErrorAction SilentlyContinue

# 6 — stop through the real script. It ends with Read-Host, so its exit code is
# checked only after the server is gone; the wait is bounded.
$stopProcess = Start-Process -FilePath 'powershell.exe' `
    -ArgumentList @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Stop) `
    -PassThru -WindowStyle Hidden
$deadline = (Get-Date).AddSeconds(30)
$stopped = $false
while ((Get-Date) -lt $deadline) {
    if (-not (Test-Health)) { $stopped = $true; break }
    Start-Sleep -Milliseconds 400
}
if ($stopProcess -and -not $stopProcess.HasExited) { Stop-Process -Id $stopProcess.Id -Force -ErrorAction SilentlyContinue }
$remaining = (Get-Listener | Measure-Object).Count
Record 'stop.ps1 停止服务' ($stopped -and $remaining -eq 0) "健康检查停止=$stopped，剩余监听=$remaining"

# 7 — the launcher brings it back, leaving the user with a working console
$exit = Invoke-Script $Console 90
$healthy = Test-Health
Record '再次启动恢复正常' $healthy "退出码 $exit"

Write-Host ''
$failed = ($results | Where-Object { -not $_.Passed } | Measure-Object).Count
Write-Host ("  结果：{0} 项通过 / {1} 项失败" -f (($results | Measure-Object).Count - $failed), $failed) -ForegroundColor $(if ($failed -eq 0) { 'Green' } else { 'Red' })
$results | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $ProjectRoot 'outputs\handoff\launcher-acceptance.json') -Encoding utf8
exit $failed
