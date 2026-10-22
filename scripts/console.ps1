# Agent Nexus console launcher: start if needed, then open the browser.
#
# Double-click friendly. It is safe to run repeatedly: an already healthy
# server is reused instead of starting a second one, and a failure is reported
# with a readable reason instead of a silently vanishing window.
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Url = 'http://127.0.0.1:8765'
$HealthUrl = "$Url/api/health"

function Open-Console {
    # A broken browser association must not take the server down with it.
    try { Start-Process $Url } catch { Write-Host "  无法自动打开浏览器，请手动访问 $Url" -ForegroundColor Yellow }
}

function Test-NexusHealth {
    # HttpWebRequest with an explicit null proxy: Invoke-WebRequest goes through
    # the system proxy stack, which can turn a one-millisecond loopback check
    # into a multi-second timeout (and fail outright behind some proxies).
    try {
        $request = [System.Net.HttpWebRequest]::Create($HealthUrl)
        $request.Proxy = $null
        $request.Timeout = 2500
        $request.ReadWriteTimeout = 2500
        $response = $request.GetResponse()
        try {
            if ([int]$response.StatusCode -ne 200) { return $false }
            $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
            $body = $reader.ReadToEnd()
        } finally { $response.Close() }
        # Confirm it is this project's server, not an unrelated service on the port.
        return ($body -match '"storage"\s*:\s*"sqlite"')
    } catch { return $false }
}

function Get-NexusListener {
    Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
}

function Wait-Nexus([int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-NexusHealth) { return $true }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

Write-Host ''
Write-Host '  agent nexus  ·  本地多 Agent 协作控制中心' -ForegroundColor Green
Write-Host "  项目目录：$ProjectRoot"
Write-Host ''

if (-not (Test-Path -LiteralPath $PythonPath)) {
    Write-Host '  [无法启动] 缺少 Python 虚拟环境。请先运行 scripts\setup.ps1。' -ForegroundColor Red
    Write-Host ''; Read-Host '按回车关闭'; exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'frontend\dist\index.html'))) {
    Write-Host '  [无法启动] 前端尚未构建。请在项目内运行：npm --prefix frontend run build' -ForegroundColor Red
    Write-Host ''; Read-Host '按回车关闭'; exit 1
}
$distIndex = Get-Item -LiteralPath (Join-Path $ProjectRoot 'frontend\dist\index.html')
$newestSource = Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'frontend\src') -Recurse -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
if ($newestSource -and $distIndex.LastWriteTimeUtc -lt $newestSource.LastWriteTimeUtc) {
    Write-Host '  提示：前端构建早于源码，界面可能不是最新。可运行：npm --prefix frontend run build' -ForegroundColor Yellow
}

if (Test-NexusHealth) {
    Write-Host '  服务已在运行，直接打开控制台。' -ForegroundColor DarkGray
    Open-Console
    Start-Sleep -Seconds 1
    exit 0
}

$listener = Get-NexusListener
if ($listener) {
    # A second double-click can arrive while the first launcher is still starting
    # its server. Wait for it instead of reporting the port as foreign.
    if (Wait-Nexus 8) {
        Write-Host '  服务已在运行，直接打开控制台。' -ForegroundColor DarkGray
        Open-Console
        Start-Sleep -Seconds 1
        exit 0
    }
    Write-Host '  [无法启动] 端口 8765 已被其他程序占用，且它不是 Agent Nexus。' -ForegroundColor Red
    foreach ($item in $listener) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($item.OwningProcess)" -ErrorAction SilentlyContinue
        $detail = if ($process -and $process.CommandLine) { $process.CommandLine } else { '(命令行不可读)' }
        Write-Host ("    占用进程 PID {0}：{1}" -f $item.OwningProcess, $detail) -ForegroundColor DarkGray
    }
    Write-Host '    请先释放该端口，或运行 scripts\stop.ps1。' -ForegroundColor DarkGray
    Write-Host ''; Read-Host '按回车关闭'; exit 1
}

Write-Host '  正在启动本地服务…' -ForegroundColor DarkGray
# The redirect targets live in work\; a fresh clone may not have it yet.
$null = New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot 'work')
$server = Start-Process -FilePath $PythonPath `
    -ArgumentList @('-m', 'uvicorn', 'nexus.main:create_app', '--factory', '--app-dir', 'backend',
                    '--host', '127.0.0.1', '--port', '8765', '--no-access-log') `
    -WorkingDirectory $ProjectRoot -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $ProjectRoot 'work\server.log') `
    -RedirectStandardError (Join-Path $ProjectRoot 'work\server-error.log')

if (-not (Wait-Nexus 45)) {
    # Losing a start race to another launcher is success for the user.
    if (Test-NexusHealth) {
        Write-Host '  另一个启动器已完成启动，直接打开控制台。' -ForegroundColor DarkGray
        Open-Console
        Start-Sleep -Seconds 1
        exit 0
    }
    Write-Host '  [启动失败] 服务在 45 秒内没有就绪。' -ForegroundColor Red
    $errorLog = Join-Path $ProjectRoot 'work\server-error.log'
    if (Test-Path -LiteralPath $errorLog) {
        Write-Host '  server-error.log 末尾：' -ForegroundColor DarkGray
        Get-Content -LiteralPath $errorLog -Tail 15 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    }
    Write-Host '  最后 5 行：' -ForegroundColor DarkGray
    Get-Content -LiteralPath (Join-Path $ProjectRoot 'work\server.log') -Tail 5 -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    Write-Host ''; Read-Host '按回车关闭'; exit 1
}

# The port owner is recorded rather than the launcher: uvicorn serves from a
# child process, and a mismatched PID would make scripts\stop.ps1 inaccurate.
$ownerId = $server.Id
$listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) { $ownerId = $listener.OwningProcess }
$ownerId | Set-Content -LiteralPath (Join-Path $ProjectRoot 'work\server.pid') -Encoding ascii

Write-Host ''
Write-Host '  控制台已就绪：' -NoNewline -ForegroundColor Green
Write-Host $Url -ForegroundColor Green
Write-Host ''
Write-Host '  浏览器已打开。' -ForegroundColor DarkGray
# Closing this window does not kill the detached server; the next double-click
# reuses it. Say that instead of implying the window owns the service lifetime.
Write-Host '  再次双击快捷方式会直接复用本服务；要停止请运行 scripts\stop.ps1。' -ForegroundColor DarkGray
Write-Host ''
Open-Console

# Hold the window open so this process owns the server's lifetime.
try { Wait-Process -Id $server.Id } catch { }
Write-Host '  服务已停止。' -ForegroundColor DarkGray
