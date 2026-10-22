"""WorkBuddy official OAuth/Open API discovery, with explicit authorization gates.

Open API availability does not authorize a call, and outbound MCP connectors do
not imply an inbound desktop task-control API. No desktop credentials are read.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import AsyncIterator

from .adapters import AgentEvent, DisconnectedAdapter, ExecutionContext


SOURCES = (
    {"title": "WorkBuddy OAuth 2.1, local assistant, cloud tasks and ACP", "url": "https://open.workbuddy.cn/docs/openapi"},
    {"title": "WorkBuddy connectors: tools called by WorkBuddy", "url": "https://www.workbuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Connector"},
)


def _desktop_metadata() -> list[dict]:
    """Inspect uninstall registry records and executable version resources only."""
    if os.name != "nt":
        return []
    script = r"""
$found = @()
Get-Process -Name WorkBuddy,WorkBuddyAI -ErrorAction SilentlyContinue | ForEach-Object {
  if ($_.Path) { $found += $_.Path }
}
Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -match '^WorkBuddy(?:\s|$)' } | ForEach-Object {
  if ($_.DisplayIcon) { $found += ($_.DisplayIcon -replace ',\d+$','').Trim('"') }
}
$result = @($found | Select-Object -Unique | ForEach-Object {
  $item = Get-Item -LiteralPath $_ -ErrorAction SilentlyContinue
  if ($item -and $item.Extension -eq '.exe') {
    [pscustomobject]@{name=$item.VersionInfo.ProductName; version=$item.VersionInfo.ProductVersion; executable=$item.FullName}
  }
})
ConvertTo-Json -InputObject $result -Compress
"""
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, timeout=8, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise OSError("WorkBuddy public installation metadata query failed")
    value = json.loads(result.stdout.decode("utf-8-sig", errors="replace") or "[]")
    if not isinstance(value, list):
        raise ValueError("Invalid WorkBuddy installation metadata")
    return [{key: str(item.get(key, ""))[:1024] for key in ("name", "version", "executable")}
            for item in value[:8] if isinstance(item, dict)]


class WorkBuddyAdapter(DisconnectedAdapter):
    id, name, executable = "workbuddy", "WorkBuddy", "workbuddy"
    transport = "manual-handoff"
    description = "个人账号人工交接：确认后复制任务，导入文件与测试申报，经人工审核后由 Nexus 校验落盘"
    execution_mode = "manual"
    manual_available = True
    capabilities: tuple[str, ...] = ()
    supported_capabilities = ()
    sources = SOURCES

    def __init__(self, root: Path | None = None, enabled: bool = True):
        self.root = Path(root).resolve() if root is not None else None
        self.enabled = enabled
        self.binary = shutil.which(self.executable) if enabled else None
        self.desktop_installations: list[dict] = []
        self.discovery_error: str | None = None
        self.active: dict = {}

    @property
    def ready(self) -> bool:
        return False

    async def detect(self) -> dict:
        if self.enabled:
            self.binary = shutil.which(self.executable)
            try:
                self.desktop_installations = await asyncio.to_thread(_desktop_metadata)
                self.discovery_error = None
            except (OSError, ValueError, subprocess.SubprocessError):
                self.discovery_error = "公开安装元数据检测不可用；未读取客户端私有数据"
        return self.probe()

    def probe(self) -> dict:
        blockers = [
            {"code": "personal_automatic_interface_unavailable", "detail": "当前未找到可供个人账号直接自动调度现有 WorkBuddy 的官方接口；企业应用流程已停用，不绕过身份认证", "requires_approval": False},
            {"code": "manual_provenance", "detail": "外部结果与测试报告属于人工申报；无法认证 WorkBuddy 执行身份，不代表自动连接成功", "requires_approval": False},
        ]
        if not self.enabled:
            blockers.insert(0, {"code": "disabled", "detail": "当前实例禁用 Agent 环境检测和执行", "requires_approval": False})
        return {
            "id": self.id, "name": self.name, "model": None,
            "transport": self.transport, "installed": bool(self.binary or self.desktop_installations),
            "status": "manual_handoff", "execution_mode": "manual", "manual_available": self.manual_available,
            "automatic_connected": False, "manual_capabilities": ["handoff.prepare", "handoff.confirm", "handoff.import", "handoff.review"],
            "operations": ["detect", "handoff.prepare", "handoff.confirm", "handoff.import", "handoff.review"],
            "executable_detected": bool(self.binary),
            "verified_at": None, "ready": False, "busy": False,
            "capabilities": [], "supported_capabilities": list(self.supported_capabilities),
            "description": self.description,
            "detail": "人工交接：用户确认后手工复制到 WorkBuddy，导入结果待审核；不需要企业应用，不自动发送任务",
            "sources": list(self.sources), "blockers": blockers,
            "desktop_installations": self.desktop_installations,
            "discovery_error": self.discovery_error,
            "bridge_mode": "manual-handoff", "shell_enabled": False,
            "future_official_api": {"available_for_current_user": False, "mode": "disabled-by-default", "retained": True},
        }

    async def execute(self, context: ExecutionContext) -> AsyncIterator[AgentEvent]:
        raise ConnectionError("WorkBuddy 为人工交接模式；请生成任务包并由用户确认，不支持自动 execute")
        yield  # pragma: no cover

    async def cancel(self, task_id: str) -> None:
        # No remote call was submitted. Remote cancellation remains unsupported.
        return None
