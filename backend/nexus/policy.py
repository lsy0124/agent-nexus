"""Dispatch policy: which Agent may run automatically, and at what cost.

This module is deliberately separate from any provider implementation. It states
three facts the console must never blur together, and it never claims a
connection:

* **Approval** — the user's current instruction allows the Agent on this project.
* **Billing** — whether a run draws on a subscription the user already holds, a
  free allowance, a metered API key, or an unknown meter.
* **Priority** — the order automatic dispatch prefers.

GPT/Codex is disabled for automatic dispatch. That is a policy state, not a
capability claim: a user may still address Codex explicitly, one task at a time,
exactly as before. Nothing here enables, installs, authenticates or pays for an
Agent.
"""
from __future__ import annotations

import os
from typing import Mapping

# Order matters: earlier entries win automatic dispatch.
DEFAULT_PRIORITY = ("deepseek-gateway", "dsh", "zcode", "zcode-gateway", "gemini", "workbuddy", "mimo", "codex")
DEFAULT_DISABLED = ("codex",)
DEFAULT_MANUAL_ONLY = ("workbuddy",)

BILLING = {
    "dsh": "metered-api",    # headless bridge explicitly requires an API key
    "zcode": "subscription",  # existing ZCode desktop account/Coding Plan
    "gemini": "subscription",  # official Google member CLI, no API fallback
    "workbuddy": "subscription",  # human handoff; no automatic call at all
    "mimo": "metered-api",    # official API key; explicit cost approval required
    "codex": "subscription",  # existing official CLI login (disabled by default)
    # User-authorized third-party gateways: the route meters tokens or credits,
    # so they are metered API usage even when the plan behind them is a
    # subscription. They are never treated as an unmetered allowance.
    "deepseek-gateway": "metered-api",
    "zcode-gateway": "metered-api",
}

BILLING_DETAIL = {
    "subscription": "沿用你当前已登录的订阅额度；不自动改走付费 API",
    "subscription-manual": "沿用你当前已登录的订阅；由人工在第三方界面执行",
    "free": "免费额度；超出即停止，不自动转付费",
    "metered-api": "按量计费 API；需要逐次显式费用批准，绝不自动切换",
    "unknown": "计费方式未核实；默认不自动调度",
}

# Which Agents draw on a meter the user has already approved for automatic use.
AUTO_APPROVED_BILLING = ("subscription", "free", "subscription-manual")


def _ids(variable: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(variable)
    if raw is None:
        return default
    parts = tuple(part.strip() for part in raw.split(",") if part.strip())
    return parts or ()


class DispatchPolicy:
    """Immutable view of the current dispatch policy."""

    def __init__(self, priority=None, disabled=None, manual_only=None, known=()):
        self.priority = tuple(priority if priority is not None else DEFAULT_PRIORITY)
        self.disabled = frozenset(disabled if disabled is not None else DEFAULT_DISABLED)
        self.manual_only = frozenset(manual_only if manual_only is not None else DEFAULT_MANUAL_ONLY)
        self.known = frozenset(known)

    @classmethod
    def from_environment(cls, known=()) -> "DispatchPolicy":
        return cls(_ids("NEXUS_PRIORITY", DEFAULT_PRIORITY),
                   _ids("NEXUS_DISABLED_AGENTS", DEFAULT_DISABLED),
                   _ids("NEXUS_MANUAL_ONLY", DEFAULT_MANUAL_ONLY), known)

    def billing(self, agent_id: str) -> str:
        return BILLING.get(agent_id, "unknown")

    def rank(self, agent_id: str) -> int:
        """Lower sorts first. Unknown Agents never outrank a configured one."""
        return self.priority.index(agent_id) if agent_id in self.priority else len(self.priority) + 1

    def auto_blockers(self, agent_id: str, info: Mapping | None = None) -> list[dict]:
        """Reasons this Agent may not be picked by automatic dispatch.

        An empty list means policy permits it. It never means the Agent is
        connected: the caller still requires a fresh verified connection and a
        complete capability match.
        """
        reasons = []
        if agent_id in self.disabled:
            reasons.append({
                "code": "auto_dispatch_disabled", "agent_id": agent_id, "requires_approval": True,
                "detail": f"{agent_id} 已被调度策略停用自动派发；显式选择该 Agent 的单次任务仍可执行",
            })
        if agent_id in self.manual_only:
            reasons.append({
                "code": "manual_only", "agent_id": agent_id, "requires_approval": False,
                "detail": f"{agent_id} 使用人工交接模式，没有自动执行接口",
            })
        billing = info.get('billing', self.billing(agent_id)) if info is not None else self.billing(agent_id)
        if info is not None and info.get('connection_mode') == 'official-rest-api':
            billing = 'metered-api'
        if billing not in AUTO_APPROVED_BILLING:
            reasons.append({
                "code": "billing_not_auto_approved", "agent_id": agent_id, "requires_approval": True,
                "billing": billing,
                "detail": f"{agent_id} 计费方式为 {billing}，未获自动调度批准",
            })
        if info is not None and info.get("execution_mode") == "manual":
            reasons.append({
                "code": "manual_only", "agent_id": agent_id, "requires_approval": False,
                "detail": f"{agent_id} 当前为人工交接模式",
            })
        return reasons

    def may_auto_dispatch(self, agent_id: str, info: Mapping | None = None) -> bool:
        return not self.auto_blockers(agent_id, info)

    def order(self, agent_ids) -> list[str]:
        return sorted(agent_ids, key=lambda item: (self.rank(item), item))

    def snapshot(self, agents=()) -> dict:
        listing = [item for item in agents if isinstance(item, Mapping)]
        by_id = {item.get("id"): item for item in listing}
        rows = []
        for agent_id in self.order(list(by_id) or list(self.priority)):
            info = by_id.get(agent_id)
            billing = self.billing(agent_id)
            rows.append({
                "id": agent_id, "priority": self.rank(agent_id), "billing": billing,
                "billing_detail": BILLING_DETAIL.get(billing, BILLING_DETAIL["unknown"]),
                "auto_dispatch": self.may_auto_dispatch(agent_id, info),
                # A manual-only Agent is not less available: it has a defined
                # human-handoff path, so an explicit choice is still honoured.
                "explicit_allowed": True,
                "auto_blockers": self.auto_blockers(agent_id, info),
                "model_status": (info or {}).get("status"),
            })
        return {
            "priority": list(self.priority),
            "disabled_agents": sorted(self.disabled),
            "manual_only_agents": sorted(self.manual_only),
            "auto_approved_billing": list(AUTO_APPROVED_BILLING),
            "agents": rows,
            "gpt_auto_dispatch": not self.may_auto_dispatch("codex"),
            "paid_api_auto_switch": False,
            "note": ("策略只决定自动调度候选；连接状态仍以真实验证为准。"
                     "显式选择 Agent 的任务不受自动派发停用影响。"),
        }

    def summary(self) -> dict:
        """Compact form embedded in /api/state."""
        return {
            "priority": list(self.priority),
            "gpt_auto_dispatch": not self.may_auto_dispatch("codex"),
            "disabled_agents": sorted(self.disabled),
            "manual_only_agents": sorted(self.manual_only),
            "paid_api_auto_switch": False,
            "billing": {agent_id: self.billing(agent_id) for agent_id in self.order(list(self.priority))},
        }
