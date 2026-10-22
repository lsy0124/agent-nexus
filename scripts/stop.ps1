# Stop this project's Agent Nexus server.
#
# The owner is identified by evidence this project controls: the image path,
# this project's own virtual environment, and the loopback port. A stale PID
# file can therefore never make this stop an unrelated process.
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $ProjectRoot 'work/server.pid'
$VenvPattern = Join-Path $ProjectRoot '.venv\Scripts\*'

function Test-NexusOwner {
    # Ownership rests on evidence this project controls. The strongest signal is
    # that the process runs this project's server module; a process environment
    # policy can hide a command line, in which case this project's own virtual
    # environment is the fallback. An unrelated program on the port is refused.
    param([int]$ProcessId)

    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    $commandLine = if ($process) { [string]$process.CommandLine } else { '' }
    $imagePath = if ($process -and $process.ExecutablePath) { [string]$process.ExecutablePath } else { '' }
    $runsThisServer = $commandLine -like '*nexus.main*'
    $fromThisVenv = ($imagePath -like $VenvPattern) -or ($commandLine -like "*$ProjectRoot\.venv\Scripts\*")
    if ($runsThisServer -and -not $fromThisVenv -and $process) {
        # Windows' venv launcher starts a base-Python child; verify its direct
        # parent instead of assuming every nexus.main process belongs to us.
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ParentProcessId)" -ErrorAction SilentlyContinue
        $parentLine = if ($parent) { [string]$parent.CommandLine } else { '' }
        $fromThisVenv = $parentLine -like "*$ProjectRoot\.venv\Scripts\*" -and $parentLine -like '*nexus.main*'
    }
    return [pscustomobject]@{
        ProcessId = $ProcessId; CommandLine = $commandLine; ImagePath = $imagePath
        Verified = [bool]($runsThisServer -and $fromThisVenv)
    }
}

function Stop-NexusProcess([int]$ProcessId, [string]$Reason) {
    if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
        Write-Host "  PID $ProcessId 已不存在（$Reason）" -ForegroundColor DarkGray
        return $false
    }
    $owner = Test-NexusOwner -ProcessId $ProcessId
    if (-not $owner.Verified) {
        Write-Host "  拒绝停止 PID $ProcessId：无法确认它属于本项目（$Reason）" -ForegroundColor Yellow
        Write-Host "    镜像路径：$($owner.ImagePath)" -ForegroundColor DarkGray
        return $false
    }
    Stop-Process -Id $ProcessId -Force
    Write-Host "  已停止 PID $ProcessId（$Reason）"
    return $true
}

$stopped = $false

# The listening socket is the authoritative owner; the PID file can be stale.
foreach ($item in (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)) {
    if (Stop-NexusProcess $item.OwningProcess '监听 127.0.0.1:8765') { $stopped = $true }
}

if (Test-Path -LiteralPath $PidFile) {
    $recorded = 0
    if ([int]::TryParse((Get-Content -LiteralPath $PidFile -Raw).Trim(), [ref]$recorded) -and $recorded -gt 0) {
        if (Stop-NexusProcess $recorded '记录在 server.pid') { $stopped = $true }
    } else {
        Write-Host '  server.pid 内容无效，已忽略' -ForegroundColor DarkGray
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

if (-not $stopped) { Write-Host 'Agent Nexus 当前没有运行。' }
Write-Host ''
Read-Host '按回车关闭'
