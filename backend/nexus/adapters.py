"""Versioned adapter contract; discovery never proves a real connection."""
from __future__ import annotations

import importlib
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

PROTOCOL_VERSION = "1.0"
AGENT_MODULES = {
    "codex": ("codex_adapter", "CodexAdapter"),
    "zcode": ("zcode_adapter", "ZCodeAdapter"),
    "workbuddy": ("workbuddy_adapter", "WorkBuddyAdapter"),
    "gemini": ("gemini_gateway", "GeminiGateway"),
    "dsh": ("dsh_adapter", "DSHAdapter"),
    "mimo": ("mimo_adapter", "MIMOAdapter"),
    # User-authorized third-party OpenAI-compatible gateway routes. These are
    # metered API routes, kept separate from the first-party adapters above.
    "zcode-gateway": ("gateway_adapter", "ZCodeGatewayAdapter"),
    "deepseek-gateway": ("gateway_adapter", "DeepSeekGatewayAdapter"),
}


@dataclass(frozen=True)
class AgentEvent:
    kind: str  # started, thread, log, completion; failures raise, never complete
    message: str
    metadata: dict | None = None


@dataclass(frozen=True)
class ExecutionContext:
    task_id: str
    worktree: str
    objective: str
    approved_skills: tuple[str, ...]
    timeout_seconds: int = 300
    input_files: tuple[dict, ...] = ()
    output_token_limit: int | None = None
    planning_only: bool = False


class AgentAdapter(ABC):
    id: str
    name: str
    model: str | None = None
    transport = "cli"
    executable: str | None = None
    planned = False
    description = ""
    capabilities: tuple[str, ...] = ()
    supported_capabilities: tuple[str, ...] = ()
    sources: tuple = ()
    blockers: tuple = ()
    ready = False
    verified_at = None
    version = None
    protocol_version = PROTOCOL_VERSION

    def __init__(self, root: Path | None = None, enabled=True):
        self.root = root.resolve() if root else None
        self.enabled = enabled
        self.active = {}

    def probe(self) -> dict:
        binary = shutil.which(self.executable) if self.executable else None
        return {
            "id": self.id, "name": self.name, "model": self.model,
            "transport": self.transport, "installed": bool(binary),
            "status": "disconnected", "ready": False, "busy": bool(getattr(self, "active", {})),
            "executable_detected": bool(binary), "verified_at": None,
            "capabilities": [], "supported_capabilities": list(self.supported_capabilities),
            "description": self.description, "sources": list(self.sources),
            "blockers": list(self.blockers), "protocol_version": self.protocol_version,
            "detail": "已发现 CLI，尚未授权或验证真实请求" if binary else "未发现受支持 CLI；未连接",
        }

    async def detect(self):
        """Local, bounded discovery only. Never log in or invoke a paid model."""
        return self.probe()

    @abstractmethod
    async def execute(self, context: ExecutionContext) -> AsyncIterator[AgentEvent]:
        yield AgentEvent("error", "未实现")

    async def cancel(self, task_id: str) -> None:
        """Terminate owned processes or stop local HTTP consumption; see per-adapter limits."""


class DisconnectedAdapter(AgentAdapter):
    async def execute(self, context: ExecutionContext) -> AsyncIterator[AgentEvent]:
        raise ConnectionError(f"{self.name} 未连接；不提供模拟执行。")
        yield  # pragma: no cover


class UnavailableAdapter(DisconnectedAdapter):
    def __init__(self, agent_id, reason):
        super().__init__()
        self.id, self.name = agent_id, agent_id
        self.blockers = (reason,)
        self.description = "Adapter 加载失败，其余 Agent 可独立工作"


def make_adapters(root, codex_enabled=True):
    """Each module is optional: a failed import or constructor affects one agent only."""
    result = {}
    # Deployment controls are non-secret and project/service scoped. No key discovery.
    configured = os.environ.get("NEXUS_AGENTS")
    enabled_ids = set(AGENT_MODULES) if configured is None else {part.strip() for part in configured.split(",")}
    for agent_id, (module, cls) in AGENT_MODULES.items():
        try:
            adapter_type = getattr(importlib.import_module("." + module, __package__), cls)
            result[agent_id] = adapter_type(root, enabled=codex_enabled and agent_id in enabled_ids)
        except Exception as exc:
            result[agent_id] = UnavailableAdapter(agent_id, "adapter_load_error:" + type(exc).__name__)
    return result
