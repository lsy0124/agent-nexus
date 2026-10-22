"""Public ZCode interface discovery; desktop installation is never execution proof.

No private ZCode configuration, authentication store, logs, or packaged application
code is read.  A desktop executable is deliberately not launched as a CLI.
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
    {"title": "ZCode official CLI and Web distribution", "url": "https://github.com/zai-org/ZCode/blob/main/README.en.md"},
    {"title": "ZCode model access and plans", "url": "https://www.zcode.network/en/docs/configuration/"},
    {"title": "ZCode MCP: external tools used by ZCode", "url": "https://www.zcode.network/en/docs/mcp-services/"},
)


def _desktop_metadata() -> list[dict]:
    """Read public installation/process image metadata only, without opening data stores."""
    if os.name != "nt":
        return []
    # An argv list, a fixed script, and NoProfile avoid shell interpolation and
    # user startup scripts. No client command line or client contents are read.
    script = r"""
$found = @()
Get-Process -Name ZCode -ErrorAction SilentlyContinue | ForEach-Object {
  if ($_.Path) { $found += $_.Path }
}
Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -match '^ZCode(?:\s|$)' } | ForEach-Object {
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
        raise OSError("ZCode public installation metadata query failed")
    value = json.loads(result.stdout.decode("utf-8-sig", errors="replace") or "[]")
    if not isinstance(value, list):
        raise ValueError("Invalid ZCode installation metadata")
    return [{key: str(item.get(key, ""))[:1024] for key in ("name", "version", "executable")}
            for item in value[:8] if isinstance(item, dict)]


class ZCodeAdapter(DisconnectedAdapter):
    id, name, executable, model = "zcode", "ZCode", "zcode", "GLM-5.3"
    transport = "official-cli-pending"
    description = "官方 CLI / Web 接口已确认；桌面安装与外部执行连接分别检测"
    capabilities: tuple[str, ...] = ()
    supported_capabilities = ("task.submit", "status", "logs", "cancel")
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
                # Never expose stdout/stderr, which are not needed for a public
                # installation probe, and never break another adapter's probe.
                self.discovery_error = "公开安装元数据检测不可用；未读取客户端私有数据"
        return self.probe()

    def probe(self) -> dict:
        blockers = [
            {"code": "bridge_not_verified", "detail": "尚未验证官方 CLI 的无工具、结构化文件输出与取消协议；桌面 GUI 不作为 CLI 调用", "requires_approval": False},
            {"code": "account_authorization_required", "detail": "需要用户批准官方模型账号/额度授权；不会读取或复用桌面客户端凭证", "requires_approval": True},
        ]
        if not self.binary:
            blockers.insert(0, {"code": "cli_not_installed", "detail": "PATH 未检测到独立官方 zcode CLI；需获取官方源码并在项目内构建，账号额度另行授权；全局安装需批准", "requires_approval": False})
        if not self.enabled:
            blockers.insert(0, {"code": "disabled", "detail": "当前实例禁用 Agent 环境检测和执行", "requires_approval": False})
        return {
            "id": self.id, "name": self.name, "model": self.model,
            "transport": self.transport, "installed": bool(self.binary or self.desktop_installations),
            "status": "disconnected", "executable_detected": bool(self.binary),
            "verified_at": None, "ready": False, "busy": False,
            "capabilities": [], "supported_capabilities": list(self.supported_capabilities),
            "description": self.description,
            "detail": "未连接：官方 CLI 接口已确认，尚未完成授权与受限执行验证",
            "sources": list(self.sources), "blockers": blockers,
            "desktop_installations": self.desktop_installations,
            "discovery_error": self.discovery_error,
            "bridge_mode": "unavailable", "shell_enabled": False,
        }

    async def execute(self, context: ExecutionContext) -> AsyncIterator[AgentEvent]:
        raise ConnectionError("ZCode 未连接：需要官方 CLI、用户授权及结构化文件安全桥接验证")
        yield  # pragma: no cover

    async def cancel(self, task_id: str) -> None:
        # No task was submitted and no process or remote operation is owned.
        return None
