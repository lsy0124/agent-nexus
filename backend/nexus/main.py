from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid
from typing import Literal

import httpx

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .adapters import ExecutionContext, make_adapters
from .bridge_protocol import BridgeError, redact, validate_plan
from .policy import DispatchPolicy
from .registry import AdapterRegistry
from . import budget as budget_module
from . import catalog
from . import file_changes, safety, worktrees
from . import keyvault, quota as quota_module, usage as usage_module
from .storage import Store, now
from . import orchestration
from .reliability import Checkpoints, RecoveryDecision

ROOT = Path(__file__).resolve().parents[2]
ORIGINS = {f"http://{host}:{port}" for host in ("localhost", "127.0.0.1") for port in (5173, 5174, 8765)}
AGENT_IDS = Literal["codex", "zcode", "workbuddy", "gemini", "dsh", "mimo",
                    "zcode-gateway", "deepseek-gateway", "local-check", "auto"]


class TaskInput(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=12000)
    agent_id: AGENT_IDS = "codex"
    kind: Literal["agent", "diagnostic"] = "agent"
    priority: Literal["high", "normal", "low"] = "normal"
    dependencies: list[str] = Field(default_factory=list, max_length=50)
    requires_skills: list[str] = Field(default_factory=list, max_length=30)
    input_files: list[str] = Field(default_factory=list, max_length=20)
    auto_run: bool = False
    cost_approved: bool = False

    @model_validator(mode="after")
    def normalize(self):
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("任务标题不能为空")
        if (self.kind == "diagnostic") != (self.agent_id == "local-check"):
            raise ValueError("本地环境检查必须使用 local-check；AI 任务必须使用 Agent Adapter")
        if any(not s.strip() or len(s) > 100 for s in self.requires_skills):
            raise ValueError("能力名称必须为 1–100 个字符")
        self.dependencies = list(dict.fromkeys(self.dependencies))
        self.requires_skills = list(dict.fromkeys(s.strip() for s in self.requires_skills))
        for name in self.input_files:
            file_changes.relative_path(name)
        if self.auto_run and self.kind != "agent":
            raise ValueError("提交后执行只支持 AI 文件任务")
        return self


class SkillInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    source_url: str = Field(max_length=2000)
    content: str = Field(min_length=1, max_length=64000)
    permissions: list[str] = Field(default_factory=lambda: ["workspace.read"], max_length=20)

    @model_validator(mode="after")
    def validate_source(self):
        if not safety.valid_source(self.source_url):
            raise ValueError("来源必须是无内嵌凭证的 HTTPS URL")
        if not self.name.strip() or not self.content.strip():
            raise ValueError("名称和审查内容不能为空")
        return self


class ApprovalInput(BaseModel):
    decision: Literal["approve", "reject"]
    reviewer: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=2000)


class ConnectionInput(BaseModel):
    api_key: SecretStr = Field(min_length=8, max_length=4096)
    model: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:@-]+$")
    approve_usage: bool = False
    # Only used by the OpenAI-compatible gateway routes. Omitted means the
    # Adapter's configured default route; Nexus still validates scheme and host.
    base_url: str | None = Field(default=None, max_length=300)


class SubscriptionInput(BaseModel):
    approve_subscription: bool = False
    model: str = Field(default="auto", max_length=100, pattern=r"^[A-Za-z0-9._-]+$")


class BudgetInput(BaseModel):
    """Nexus budget ceilings. ``None`` means "not configured", never "unlimited"."""

    per_task_tokens: int | None = Field(default=None, ge=0, le=100_000_000)
    per_task_budget: float | None = Field(default=None, ge=0, le=1_000_000)
    daily_budget: float | None = Field(default=None, ge=0, le=1_000_000)
    monthly_budget: float | None = Field(default=None, ge=0, le=10_000_000)
    daily_calls: int | None = Field(default=None, ge=0, le=1_000_000)
    monthly_calls: int | None = Field(default=None, ge=0, le=10_000_000)
    codex_daily_calls: int | None = Field(default=None, ge=0, le=1_000_000)
    currency: Literal["USD", "CNY"] | None = None
    require_metered_approval: bool | None = None
    stop_dispatch_when_exhausted: bool | None = None
    approve_cost: bool = False


class ProviderKeyInput(BaseModel):
    api_key: SecretStr = Field(min_length=8, max_length=4096)
    approve_usage: bool = False
    # Which model this key should be used with; omitted keeps the current choice.
    model: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9._:@-]+$")


class PlanInput(BaseModel):
    plan: Literal["lite", "pro", "max"] | None = None


class RateInput(BaseModel):
    currency: str | None = Field(default=None, max_length=16)
    unit: str | None = Field(default=None, max_length=32)
    input_cache_hit: dict | None = None
    input_cache_miss: dict | None = None
    output: dict | None = None
    input_multiplier: float | None = Field(default=None, ge=0, le=10_000)
    cached_input_multiplier: float | None = Field(default=None, ge=0, le=10_000)
    output_multiplier: float | None = Field(default=None, ge=0, le=10_000)
    revision: str | None = Field(default=None, max_length=64)
    source: str | None = Field(default=None, max_length=500)
    note: str | None = Field(default=None, max_length=500)


class BindingInput(BaseModel):
    provider_id: str | None = Field(default=None, max_length=64)
    model_key: str | None = Field(default=None, max_length=160)
    acknowledge_unconfigured: bool = False


class VisionTestInput(BaseModel):
    model_key: str | None = Field(default=None, max_length=160)
    approve_usage: bool = False


class ReverifyInput(BaseModel):
    runtime_id: str | None = Field(default=None, max_length=64)


class BatchVerificationInput(BaseModel):
    revision: str = Field(max_length=64)
    runtime_ids: list[str] = Field(min_length=1, max_length=8)
    approve_usage: bool = False


class VaultInput(BaseModel):
    backend: Literal['memory', 'dpapi']
    approve_persistence: bool = False


class RunInput(BaseModel):
    cost_approved: bool = False
    agent_id: str | None = None
    execution_key: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{16,80}$')


def validate_dependencies(db, task_id: str, dependencies: list[str]):
    known = {r[0] for r in db.execute("SELECT id FROM tasks")}
    if task_id in dependencies:
        raise HTTPException(422, "任务不能依赖自身")
    if set(dependencies) - known:
        raise HTTPException(422, "依赖任务不存在")
    graph: dict[str, list[str]] = {key: [] for key in known}
    for row in db.execute("SELECT task_id, depends_on FROM dependencies"):
        graph[row[0]].append(row[1])
    graph[task_id] = dependencies
    visited, active = set(), set()

    def visit(node):
        if node in active:
            raise HTTPException(422, "依赖关系存在循环")
        if node in visited:
            return
        active.add(node)
        for child in graph.get(node, []):
            visit(child)
        active.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)


def create_app(root: Path = ROOT, enable_codex: bool = True) -> FastAPI:
    root = root.resolve()
    store = Store(root)
    checkpoints = Checkpoints(root, store)
    token = secrets.token_urlsafe(32)
    running: dict[str, asyncio.Task] = {}
    finalizing: set[str] = set()
    pause_requests: set[str] = set()
    mutation = asyncio.Lock()
    adapters = make_adapters(root, enable_codex)
    codex = adapters["codex"]
    registry = AdapterRegistry(adapters)
    policy = DispatchPolicy.from_environment(known=list(adapters))

    # ---- unified usage / quota / budget plane -------------------------------
    vault = keyvault.SecretVault(root, backend=os.environ.get('NEXUS_KEY_VAULT') or store.setting('vault', 'backend', 'memory'))
    rate_card = budget_module.RateCard(store)
    collector = usage_module.UsageCollector(store, estimator=rate_card.estimate)
    guard = budget_module.BudgetGuard(store, collector, rate_card, policy)

    # Which provider each API-configurable runtime serves, so a persisted key
    # can be restored into the right adapter without any further user action.
    RUNTIME_PROVIDERS = {runtime_id: runtime["default_binding"]["provider_id"]
                         for runtime_id, runtime in catalog.RUNTIMES.items()}

    def save_binding(runtime_id, adapter):
        binding = {'provider_id': getattr(adapter, 'provider_id', None) or RUNTIME_PROVIDERS[runtime_id],
                   'model': getattr(adapter, 'model', None), 'base_url': getattr(adapter, 'endpoint', None),
                   'enabled': bool(getattr(adapter, '_approved', getattr(adapter, 'ready', False)))}
        if hasattr(adapter, 'model_key'):
            candidate = f"{binding['provider_id']}/{binding['model']}"
            adapter.model_key = candidate if candidate in catalog.MODELS else None
        with store.connect() as db:
            db.execute("INSERT INTO settings(scope,key,value,updated_at) VALUES('binding',?,?,?) "
                       "ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                       (runtime_id, json.dumps(binding), now()))

    def restore_persisted_credentials() -> dict:
        """Reload DPAPI-protected keys into their adapters at startup.

        A key is only handed to an adapter whose Provider matches, and the
        adapter still has to pass a real request before it counts as connected.
        Restoring configuration is never presented as a verified connection.
        """
        restored, failed = [], []
        for runtime_id, provider_id in RUNTIME_PROVIDERS.items():
            adapter = adapters.get(runtime_id)
            if adapter is None or not hasattr(adapter, "configure"):
                continue
            configured = store.setting("binding", runtime_id) or {}
            if configured.get('enabled') is False:
                continue
            provider_id = configured.get('provider_id') or provider_id
            secret = vault.get(provider_id)
            if not secret:
                continue
            try:
                # Preserve the model the user last selected, if it is still valid.
                model = configured.get("model") or getattr(adapter, "model", None)
                if runtime_id in ('zcode-gateway', 'deepseek-gateway'):
                    adapter.provider_id = provider_id
                    adapter.configure(api_key=secret, model=model, approved=True,
                                      base_url=configured.get('base_url') or catalog.PROVIDERS[provider_id]['base_url'])
                else:
                    adapter.configure(api_key=secret, model=model, approved=True)
                restored.append(runtime_id)
            except Exception:
                failed.append(runtime_id)
        return {"restored": restored, "failed": failed,
                "load_errors": list(vault.load_errors),
                "note": ("已从 DPAPI 恢复密钥配置；恢复配置不等于已连接，"
                         "仍需真实请求验证后才会显示为已连接。")}

    def official_keys() -> dict:
        """Official-account keys supplied by the user, resolved by Provider id."""
        return {provider_id: vault.get(provider_id) for provider_id in catalog.PROVIDERS
                if vault.get(provider_id)}

    def plan_selection() -> dict:
        with store.connect() as db:
            return {row["key"]: json.loads(row["value"]) for row in
                    db.execute("SELECT key, value FROM settings WHERE scope='plan'")}

    quota_registry = quota_module.build_registry(
        collector=collector, official_keys=official_keys(),
        budget_settings=guard.settings, plan_selection=plan_selection())

    def agent_binding(agent_id: str) -> dict:
        """Agent → runtime → model → provider, with the two capability facts apart."""
        info = catalog.runtime(agent_id)
        adapter = adapters.get(agent_id)
        provider_id = getattr(adapter, "provider_id", None) or info["default_binding"]["provider_id"]
        model_id = getattr(adapter, "model", None)
        model_key = getattr(adapter, "model_key", None) or catalog.resolve_binding(agent_id, provider_id, None)["model_key"]
        record = catalog.MODELS.get(model_key) if model_key else None
        return {
            "runtime_kind": info["runtime_kind"],
            "task_execution_scope": info["task_execution_scope"],
            "execution_note": info["execution_note"],
            "provider": catalog.provider_public(provider_id) if provider_id in catalog.PROVIDERS else None,
            "model": catalog.model_public(model_key) if record else None,
            "model_id": model_id or (record["model_id"] if record else None),
              "execution": ({'stream': adapter.stream_enabled, 'max_output_tokens': adapter.max_output_tokens, 'reasoning': getattr(adapter, 'reasoning_mode', 'provider')}
                            if hasattr(adapter, 'stream_enabled') else None),
            "bindings": [
                {"provider_id": binding["provider_id"],
                 "provider_name": catalog.PROVIDERS[binding["provider_id"]]["name"],
                 "requires": binding["requires"],
                 "verified": binding.get("verified", False),
                 "note": binding.get("note"),
                 "models": [catalog.model_public(key) for key in binding["models"]]}
                for binding in info["bindings"]],
        }

    def usage_summary_for(agent_id: str, *, window: str = "month") -> dict:
        """Usage for one *runtime*, with its provider and quota class.

        Rows stay per-runtime so switching a model or a route never mixes one
        model's tokens into another's totals.
        """
        totals = collector.totals(window, agent_id=agent_id)
        provider_id = (getattr(adapters.get(agent_id), "provider_id", None)
                       or catalog.RUNTIMES[agent_id]["default_binding"]["provider_id"])
        quota_view = next((item for item in quota_registry.providers
                           if item.provider_id == provider_id), None)
        return {
            "range": window,
            "requests": totals["requests"],
            "succeeded": totals["succeeded"],
            "failed": totals["failed"],
            "success_rate": (round(totals["succeeded"] / totals["requests"], 4) if totals["requests"] else None),
            "total_tokens": totals["total_tokens"],
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "cached_tokens": totals["cached_tokens"],
            "reasoning_tokens": totals["reasoning_tokens"],
            "usage_reported": totals["usage_reported"],
            "usage_unavailable": totals["usage_unavailable"],
            "estimated_cost": round(totals["estimated_cost"], 6),
            "currency": totals["currency"],
            "cost_source": "nexus-estimate",
            "provider_id": provider_id,
            "quota_kind": quota_view.kind if quota_view else None,
            "quota_label": (quota_module.KIND_LABELS[quota_view.kind] if quota_view else None),
        }

    def agents():
        """One entry per *runtime* (used internally), enriched with binding facts.

        The console groups these into logical Agent cards; see ``logical_agents()``.
        """
        result = registry.snapshots()
        task_rows = store.tasks()
        executing = {((t.get('last_attempt') or {}).get('agent_id') or t['agent_id']): t
                     for t in task_rows if t['status'] == 'running'}
        latest = {}
        for task in task_rows:
            attempt = task.get('last_attempt')
            if attempt:
                runtime = attempt.get('agent_id') or task['agent_id']
                if runtime not in latest or attempt['created_at'] > latest[runtime][1]['created_at']:
                    latest[runtime] = task, attempt
        for item in result:
            item["binding"] = agent_binding(item["id"])
            item['billing'] = item['binding']['provider']['billing']
            item["logical_agent_id"] = catalog.logical_agent(item["id"])
            current = executing.get(item['id'])
            item['current_task'] = {'id': current['id'], 'title': current['title']} if current else None
            last = latest.get(item['id'])
            item['last_task_error'] = ({'task_id': last[0]['id'], 'kind': last[1]['error_kind'],
                'message': last[1]['error_message'],
                'state': 'BLOCKED_PERMISSION' if last[1]['error_kind'] == 'permission' else 'FAILED'}
                if last and last[1]['status'] == 'failed' else None)
            # "Model API callable" and "Agent can execute a whole task" are
            # distinct claims and are reported separately, never merged.
            item["model_api_callable"] = bool(item.get("status") == "connected")
            proof = store.latest_verification(item['id'], 'task_execution_capable')
            item["task_execution_capable"] = bool(
                item.get("status") == "connected" and proof and proof['outcome'] == 'verified'
                and proof['evidence'].get('artifacts')
                and proof.get('provider_id') == item['binding']['provider']['id']
                and proof['evidence'].get('model') == getattr(adapters[item['id']], 'model', None))
            item["execution_capability_detail"] = item["binding"]["execution_note"]
            item["usage"] = usage_summary_for(item["id"])
            # A live connection and a historical verification are different
            # facts: the past result is shown, but it never implies "online now".
            item["verification"] = store.latest_verification(item["id"], "model_api_callable")
            item["execution_verification"] = store.latest_verification(item["id"], "task_execution_capable")
        if hasattr(app.state, "workbuddy_oauth_status"):
            for item in result:
                if item["id"] == "workbuddy":
                    item["oauth"] = app.state.workbuddy_oauth_status()
                    if item["oauth"].get("oauth_authorized"):
                        item["detail"] = "OAuth 只读授权完成；任务执行边界仍未验证，Agent 未连接"
        return result

    def logical_agents(by_runtime: dict | None = None) -> list[dict]:
        """The Agent network: one card per logical Agent, routes attached.

        Statistics are defined here so the overview, the Agent network and the
        scheduler cannot disagree:
          * ``agent_total`` counts logical Agents only — a Provider or gateway
            never takes an Agent slot;
          * ``online`` counts Agents with a route that is connected *now*;
          * ``model_api_callable`` and ``task_execution_capable`` are counted
            separately, because a callable model API is not a development Agent;
          * ``manual_handoff`` and ``verified_history`` are reported on their own
            and are never folded into ``online``.
        """
        by_runtime = by_runtime if by_runtime is not None else {item["id"]: item for item in agents()}
        cards = []
        for logical in catalog.logical_agents():
            routes = []
            for route in logical["routes"]:
                runtime = by_runtime.get(route["runtime_id"])
                if runtime is None:
                    continue
                routes.append({**route, "state": runtime})
            online_routes = [r for r in routes if r["state"]["status"] == "connected"]
            manual = any(r["state"]["status"] == "manual_handoff" for r in routes)
            # Prefer the route the user explicitly configured, then any online one.
            preferred = next((r for r in routes if r["runtime_id"] == logical["preferred_route"]), None)
            selected = next((r for r in online_routes if r["runtime_id"] == logical["preferred_route"]),
                            online_routes[0] if online_routes else preferred or (routes[0] if routes else None))
            history = [r["state"].get("verification") for r in routes if r["state"].get("verification")]
            cards.append({
                "id": logical["id"],
                "name": logical["name"],
                "description": logical["description"],
                "preferred_route": logical["preferred_route"],
                "preferred_route_reason": logical["preferred_route_reason"],
                "routes": routes,
                "route_count": len(routes),
                "selected_runtime_id": selected["runtime_id"] if selected else None,
                "selected_model_id": (selected["state"]["binding"]["model_id"] if selected else None),
                "selected_provider": (selected["state"]["binding"]["provider"] if selected else None),
                # Live state, from this process only.
                "online": bool(online_routes),
                "online_runtime_ids": [r["runtime_id"] for r in online_routes],
                'status_label': ('人工交接' if manual and not online_routes else selected['state'].get('status_label') if selected else '未连接'),
                "status": ("connected" if online_routes else "manual_handoff" if manual else "disconnected"),
                "model_api_callable": any(r["state"]["model_api_callable"] for r in routes),
                "task_execution_capable": any(r["state"]["task_execution_capable"] for r in routes),
                "manual_handoff": manual,
                # Historical verification, kept visibly separate from "online".
                "verified_history": bool(history),
                "last_verified_at": max((h["created_at"] for h in history), default=None),
                "verification": max(history, key=lambda h: h["created_at"]) if history else None,
                "usage": logical_usage(logical),
            })
        return cards

    def logical_usage(logical: dict) -> dict:
        """Merged usage across every route of one logical Agent.

        Rows stay separated by runtime/provider/model underneath, so switching a
        model never mixes one model's tokens into another's totals.
        """
        merged = None
        routes = []
        for route in logical["routes"]:
            summary = usage_summary_for(route["runtime_id"])
            routes.append({"runtime_id": route["runtime_id"], "provider_id": summary["provider_id"],
                           "quota_label": summary["quota_label"], **summary})
            if merged is None:
                merged = dict(summary)
            else:
                for key in ("requests", "succeeded", "failed", "total_tokens", "input_tokens",
                            "output_tokens", "cached_tokens", "reasoning_tokens",
                            "usage_reported", "usage_unavailable", "estimated_cost"):
                    merged[key] = merged.get(key, 0) + summary.get(key, 0)
        if merged is None:
            merged = usage_summary_for(logical["primary_runtime"])
        merged["success_rate"] = (round(merged["succeeded"] / merged["requests"], 4)
                                 if merged["requests"] else None)
        merged["estimated_cost"] = round(merged["estimated_cost"], 6)
        merged["by_route"] = routes
        return merged

    def agent_stats(cards: list[dict] | None = None) -> dict:
        """The single source of truth for Agent counts across every page."""
        cards = cards if cards is not None else logical_agents()
        return {
            "agent_total": len(cards),
            "agent_total_detail": "按逻辑 Agent 统计；Provider 与网关不占用 Agent 名额",
            "online": sum(1 for card in cards if card["online"]),
            "manual_handoff": sum(1 for card in cards if card["manual_handoff"] and not card["online"]),
            "model_api_callable": sum(1 for card in cards if card["model_api_callable"]),
            "task_execution_capable": sum(1 for card in cards if card["task_execution_capable"]),
            "verified_history_only": sum(
                1 for card in cards if card["verified_history"] and not card["online"]),
            "runtime_count": len(catalog.RUNTIMES),
            "provider_count": len(catalog.PROVIDERS),
            "note": ("「在线」只表示本进程内存在通过真实请求验证且未过期的执行路径；"
                     "历史验证记录与人工交接单独统计，不与在线混为一谈。"
                     "「模型 API 可调用」与「Agent 具备完整任务执行能力」分别统计。"),
        }
        return {
            "range": "month",
            "requests": totals["requests"],
            "succeeded": totals["succeeded"],
            "failed": totals["failed"],
            "success_rate": (round(totals["succeeded"] / totals["requests"], 4) if totals["requests"] else None),
            "total_tokens": totals["total_tokens"],
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "cached_tokens": totals["cached_tokens"],
            "reasoning_tokens": totals["reasoning_tokens"],
            "usage_reported": totals["usage_reported"],
            "usage_unavailable": totals["usage_unavailable"],
            "estimated_cost": round(totals["estimated_cost"], 6),
            "currency": totals["currency"],
            "cost_source": "nexus-estimate",
            "provider_id": provider_id,
            "quota_kind": quota_view.kind if quota_view else None,
            "quota_label": (quota_module.KIND_LABELS[quota_view.kind] if quota_view else None),
        }

    @asynccontextmanager
    async def lifespan(app):
        # Restore DPAPI-protected credentials before the first probe, so a
        # restart brings the configured routes back with no user action.
        restore = restore_persisted_credentials()
        app.state.credential_restore = restore
        await registry.detect_all()
        with store.connect() as db:
            for row in db.execute("SELECT id FROM tasks WHERE status='running'").fetchall():
                checkpoints.interrupt(db, row['id'])
                db.execute("UPDATE tasks SET status='failed',result=?,updated_at=? WHERE id=?",
                           ("后端重启：上次执行已中断，请检查日志后重新运行", now(), row[0]))
                store.log(db, row[0], "上次执行被后端重启中断", "error")
                store.event(db, "task.interrupted", {"task_id": row[0]})
            db.execute("UPDATE attempts SET status='failed',error_kind='interrupted',error_message=?,finished_at=? WHERE status='running'",
                       ("后端重启，上次执行已中断", now()))
            previous = db.execute("SELECT a.* FROM attempts a WHERE a.agent_id='codex' ORDER BY a.created_at DESC LIMIT 1").fetchone()
            if codex.ready and previous and previous["status"] == "done" and previous["exit_code"] == 0 and previous["cli_version"] == codex.version:
                codex.verified_at = previous["finished_at"]
            if restore["restored"]:
                store.log(db, None,
                          "已从 Windows DPAPI 恢复密钥配置：" + "、".join(restore["restored"])
                          + "；恢复配置不等于已连接，需真实请求验证", source="keyvault")
                store.event(db, "credentials.restored", {"runtimes": restore["restored"]})
            for item in restore["load_errors"]:
                store.log(db, None,
                          f"密钥加载失败（{item['provider_id']}）：{item['reason']}",
                          "error", "keyvault")
            store.event(db, "startup.restore", restore)
        checkpoints.reconcile_usage(collector)
        workflow_worker = asyncio.create_task(collaborator.worker())
        yield
        workflow_worker.cancel()
        await asyncio.gather(workflow_worker, return_exceptions=True)
        pause_requests.update(running)
        for job in list(running.values()):
            job.cancel()
        if running:
            await asyncio.gather(*list(running.values()), return_exceptions=True)

    app = FastAPI(title="Agent Nexus", version="0.4.0", lifespan=lifespan)
    app.state.store, app.state.token = store, token
    app.state.checkpoints = checkpoints
    app.state.adapters = adapters
    app.state.registry = registry
    app.state.policy = policy
    handoffs = None
    try:
        from .manual_handoff import ManualHandoffs, install_routes as install_handoff_routes
        handoffs = ManualHandoffs(root, store, mutation)
        install_handoff_routes(app, handoffs)
    except Exception:
        handoffs = None
        adapters["workbuddy"].manual_available = False
        registry.faults["workbuddy"] = "manual_handoff_module_unavailable"
    try:
        from .workbuddy_routes import install_routes
        official_api_enabled = os.environ.get("NEXUS_WORKBUDDY_OFFICIAL_API") == "1"
        install_routes(app, enabled=official_api_enabled and enable_codex and getattr(adapters["workbuddy"], "enabled", False), store=store)
    except Exception:
        registry.faults["workbuddy"] = "oauth_module_unavailable"
    reviews = None
    try:
        from .review import WorktreeReviews, install_routes as install_review_routes
        reviews = WorktreeReviews(root, store, mutation)
        install_review_routes(app, reviews)
    except Exception:
        reviews = None
    app.state.reviews = reviews

    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(request, exc):
        # Pydantic errors may echo input; credentials must never appear in responses.
        return JSONResponse(status_code=422, content={"detail": [
            {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
            for error in exc.errors()]})
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])
    app.add_middleware(CORSMiddleware, allow_origins=sorted(ORIGINS), allow_methods=["GET", "POST", "PUT"],
                       allow_headers=["Content-Type", "X-Nexus-Token"])

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        from fastapi.responses import JSONResponse
        origin = request.headers.get("origin")
        if origin and origin not in ORIGINS:
            return JSONResponse({"detail": "拒绝非本地控制台来源"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            supplied = request.headers.get("x-nexus-token", "")
            if not secrets.compare_digest(supplied, token):
                return JSONResponse({"detail": "缺少有效的本地会话令牌，请刷新页面"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def task_or_404(task_id):
        task = next((t for t in store.tasks() if t["id"] == task_id), None)
        if not task:
            raise HTTPException(404, "任务不存在")
        return task

    def skill_or_404(skill_id):
        skill = next((s for s in store.skills() if s["id"] == skill_id), None)
        if not skill:
            raise HTTPException(404, "Skill 不存在")
        return skill

    def auto_gaps(task, runtime_agents):
        required = set(task["requires_skills"])
        candidates = [a for a in runtime_agents if a["status"] == "connected" and a.get("ready")]
        if any(required.issubset(a["capabilities"]) for a in candidates):
            return []
        return sorted(required)

    def gaps(task, runtime_agents=None):
        if task["agent_id"] == "local-check":
            available = {"environment.check"}
        elif task["agent_id"] == "auto":
            return auto_gaps(task, runtime_agents if runtime_agents is not None else agents())
        else:
            available = set(registry.snapshot(task["agent_id"])["capabilities"])
        return sorted(set(task["requires_skills"]) - available)

    def task_maps():
        """Review/planning/collaboration facts for every task in two queries.

        Scanning ``events`` with ``json_extract`` once per task made the console
        list response scale as tasks x events. One pass here keeps ``/api/tasks``
        and ``/api/state`` flat as the history grows.
        """
        decisions, planning = {}, {}
        with store.connect() as db:
            for row in db.execute(
                    "SELECT json_extract(payload,'$.task_id') AS task_id, type FROM events "
                    "WHERE type IN ('worktree.merged','worktree.rejected') ORDER BY id"):
                if row['task_id']:
                    decisions[row['task_id']] = row['type']
            for row in db.execute('SELECT task_id, parent_id FROM planning_runs'):
                planning[row['task_id']] = row['parent_id']
        return decisions, planning, collaborator.membership_map()

    def add_gaps_many(tasks, runtime_agents=None, maps=None):
        decisions, planning, members = maps or task_maps()
        states = {'backlog': 'PENDING', 'running': 'RUNNING', 'blocked': 'PAUSED',
                  'done': 'COMPLETED', 'failed': 'FAILED', 'cancelled': 'PAUSED'}
        # ``gaps`` probes the adapter it asks about. A task list contains many
        # rows per Agent, so probe each distinct Agent at most once instead of
        # once per task.
        provided = {item["id"]: set(item.get("capabilities") or ())
                    for item in runtime_agents or ()}
        availability, auto_rows = {}, runtime_agents
        result = []
        for task in tasks:
            needs_review = bool(task['status'] in ('done', 'failed', 'cancelled') and ((task.get('last_attempt') or {}).get('artifacts') or (task.get('latest_handoff') or {}).get('artifacts')))
            decision = decisions.get(task['id'])
            review_status = ('merged' if decision == 'worktree.merged' else 'rejected') if decision else 'pending' if needs_review else 'not_ready'
            member = members.get(task['id'])
            execution_state = ('PAUSED' if task.get('recovery_hold') else 'BLOCKED_PERMISSION' if task['status'] == 'failed' and (task.get('last_attempt') or {}).get('error_kind') == 'permission'
                               else states[task['status']])
            agent_id = task['agent_id']
            if agent_id == 'local-check':
                capability_gaps = gaps(task)
            elif agent_id == 'auto':
                # One runtime snapshot answers every auto task in this response.
                if auto_rows is None:
                    auto_rows = agents()
                capability_gaps = auto_gaps(task, auto_rows)
            else:
                if agent_id not in availability:
                    if agent_id in provided:
                        availability[agent_id] = provided[agent_id]
                    else:
                        availability[agent_id] = set(registry.snapshot(agent_id)['capabilities'])
                capability_gaps = sorted(set(task['requires_skills']) - availability[agent_id])
            result.append({**task, 'execution_state': execution_state, "capability_gaps": capability_gaps,
                           'review_status': review_status,
                           'planning_parent': planning.get(task['id']),
                           'collaboration_parent': member[0] if member else None,
                           'collaboration_node': member[1] if member else None})
        return result

    def add_gaps(task):
        return add_gaps_many([task])[0]

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "version": "0.4.0", "storage": "sqlite", "time": now(),
                'stability_revision': 'phase-b-1', 'reliability_revision': 'phase-c-1',
                'revision': 'reliability-audit-1', 'workspace_id': file_changes.sha(str(root).replace('\\', '/').casefold().encode('utf-8'))[:16]}

    @app.get("/api/session")
    async def session():
        return {"token": token}

    repo_memo = {"at": 0.0, "value": None}

    def repository_state():
        """Repository facts for the console, cached very briefly.

        Three ``git`` subprocesses cost roughly 300 ms on Windows and the console
        re-reads them after every live event. The two-second window only hides a
        change made by this same process moments ago; any Git command that needs
        the truth (worktree creation, review) still calls ``worktrees`` directly.
        """
        current = time.monotonic()
        if repo_memo["value"] is None or current - repo_memo["at"] > 2:
            repo_memo["value"] = worktrees.repository_state(root)
            repo_memo["at"] = current
        return repo_memo["value"]

    @app.get("/api/state")
    async def state():
        with store.connect() as db:
            cursor = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        # The console polls this after every live event, so the runtime snapshot
        # and the logical cards derived from it are computed exactly once.
        runtime_agents = agents()
        cards = logical_agents({item["id"]: item for item in runtime_agents})
        return {"tasks": add_gaps_many(store.tasks(), runtime_agents=runtime_agents),
                "agents": runtime_agents,
                # One card per logical Agent; the overview and the Agent network
                # both read these, so their counts cannot drift apart.
                "logical_agents": cards, "agent_stats": agent_stats(cards),
                "credential_restore": getattr(app.state, "credential_restore", None),
                'budget': guard.status(),
                "skills": store.skills(), "logs": store.logs(), "event_cursor": cursor,
                "repository": repository_state(),
                "workspace": root.name, "server_time": now(),
                "policy": {"local_only": True, "agent_execution": enable_codex, "skill_installation": False,
                           "codex_bridge": "structured-files", "codex_shell": False, "adapter_protocol": "1.0", "auto_requires_verified": True,
                           "skill_trial": "manifest-only", "remote_push": False,
                           "dispatch": policy.summary()}}

    @app.get("/api/agents")
    async def list_agents():
        return agents()

    @app.get("/api/logical-agents")
    async def list_logical_agents():
        """The Agent network as the console renders it: one card per logical Agent."""
        cards = logical_agents()
        return {"agents": cards, "stats": agent_stats(cards)}

    @app.get("/api/logical-agents/{agent_id}")
    async def get_logical_agent(agent_id: str):
        card = next((item for item in logical_agents() if item["id"] == agent_id), None)
        if card is None:
            raise HTTPException(404, "Agent 不存在")
        return card

    @app.get("/api/policy")
    async def get_policy():
        """Dispatch order, billing class and automatic-dispatch eligibility.

        This is a policy statement, never a connection claim: an Agent listed as
        eligible still has to pass a real connection test before automatic
        dispatch can use it.
        """
        return policy.snapshot(agents())

    @app.post("/api/agents/refresh")
    async def refresh_agents():
        await registry.detect_all()
        result = agents()
        with store.connect() as db:
            store.log(db, None, "已重新探测 CLI 可用性；CLI 存在不代表 Agent 已连接", source="discovery")
            store.event(db, "agents.probed", {"agents": result})
        return result

    @app.get("/api/tasks")
    async def list_tasks():
        return add_gaps_many(store.tasks())

    @app.get("/api/catalog")
    async def get_catalog():
        """Agent → runtime → model → provider, plus every declared binding.

        A declaration is not a connection: ``model_api_callable`` and
        ``task_execution_capable`` are real-request facts that appear on
        ``/api/agents``, not here.
        """
        return catalog.snapshot()

    # ------------------------------------------------------- usage & quota

    @app.get("/api/usage")
    async def get_usage(span: str = Query(default="month", pattern="^(today|week|month|all|custom)$"),
                        start: str | None = None, end: str | None = None,
                        agent_id: str | None = None, provider_id: str | None = None,
                        model_key: str | None = None, task_id: str | None = None,
                        limit: int = Query(default=200, ge=1, le=2000)):
        """Unified usage statistics. Unavailable usage is reported, never zero-filled."""
        try:
            overview = collector.overview(span, start, end)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        overview["records"] = collector.rows(span, start, end, agent_id, provider_id,
                                             model_key, task_id, limit)
        overview["filters"] = {"agent_id": agent_id, "provider_id": provider_id,
                               "model_key": model_key, "task_id": task_id}
        return overview

    @app.get("/api/usage/agents/{agent_id}")
    async def agent_usage(agent_id: str, span: str = Query(default="month"),
                          start: str | None = None, end: str | None = None):
        """One Agent's detailed history — the card drill-down."""
        if agent_id not in adapters and agent_id not in catalog.LOGICAL_AGENTS:
            raise HTTPException(404, "Agent 不存在")
        try:
            runtime_ids = catalog.runtimes_of(agent_id)
            logical = next((a for a in catalog.logical_agents() if a['id'] == agent_id), None)
            summary = logical_usage(logical) if logical else usage_summary_for(agent_id)
            return {
                "agent_id": agent_id,
                "summary": summary,
                "totals": collector.totals(span, start, end, agent_ids=runtime_ids),
                "by_model": collector.group_by("model", span, start, end, agent_ids=runtime_ids),
                "series": collector.series(span, start, end, agent_ids=runtime_ids),
                "records": collector.rows(span, start, end, agent_ids=runtime_ids, limit=200),
                "verifications": sorted([v for runtime_id in runtime_ids for v in store.verifications(runtime_id, limit=20)], key=lambda v:v['id'], reverse=True)[:20],
                "binding": agent_binding(runtime_ids[0]),
            }
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/quota")
    async def get_quota(probe: bool = Query(default=False)):
        """Four separated quota kinds. We never mix them into one number.

        ``probe=true`` performs the real documented balance request when a key
        for that provider is configured. Without a key the result states that a
        credential is required — it does not guess.
        """
        current_official = official_keys()
        for item in quota_registry.providers:
            if isinstance(item, quota_module.DocumentedBalanceQuota):
                item.key_resolver = (lambda pid=item.provider_id: current_official.get(pid))
            elif isinstance(item, quota_module.SubscriptionAllowanceQuota):
                item.plan_reader = (lambda pid=item.provider_id: plan_selection().get(pid))
        payload = quota_registry.capability()
        payload["capability"] = payload.pop("providers")
        if probe:
            payload["state"] = await quota_registry.fetch_all()
        else:
            payload["state"] = None
            payload["note"] = ("capability 为静态能力声明；带 probe=true 才发起真实的余额查询请求。"
                               "没有文档化接口的 Provider 标记为 unsupported。")
        return payload

    @app.get("/api/budget")
    async def get_budget():
        return guard.status()

    # ---------------------------------------------------------- settings/policy

    @app.put("/api/settings/budget")
    async def update_budget(data: BudgetInput):
        async with mutation:
            try:
                updated = guard.update_settings(
                    data.model_dump(exclude_unset=True, exclude={"approve_cost"}))
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            with store.connect() as db:
                store.log(db, None, "Nexus 预算与额度保护设置已更新；金额均为 Nexus 估算，不是服务商账单",
                          source="budget")
                store.event(db, "budget.updated", {"settings": updated})
            return guard.status()

    @app.get("/api/settings/providers")
    async def list_provider_settings():
        """Provider configuration and key management, without ever showing a key."""
        return {
            "vault": vault.status(),
            "providers": [
                {
                    **catalog.provider_public(provider_id),
                    "credential": vault.describe(provider_id),
                    "binding_agents": [agent_id for agent_id, info in catalog.RUNTIMES.items()
                                       if any(b["provider_id"] == provider_id for b in info["bindings"])],
                }
                for provider_id in catalog.PROVIDERS],
            "note": ("密钥只保存在后端。接口只返回指纹与长度，绝不下发密钥本身或可读片段。"
                     "第三方 Provider 只接收任务显式指定的输入文件，不上传整个项目、环境变量或用户文件。"),
        }

    @app.put('/api/settings/vault')
    async def update_vault(data: VaultInput):
        nonlocal vault
        if not enable_codex:
            raise HTTPException(403, '测试实例禁用持久化配置')
        if data.backend == 'dpapi' and not data.approve_persistence:
            raise HTTPException(403, '需要明确批准当前用户 DPAPI 加密保存')
        async with mutation:
            candidate = keyvault.SecretVault(root, backend=data.backend, directory=root / 'work' / 'secrets')
            if candidate.backend != data.backend:
                raise HTTPException(409, candidate.fallback_reason)
            try:
                for provider_id in catalog.PROVIDERS:
                    secret = vault.get(provider_id)
                    if secret:
                        candidate.set(provider_id, secret)
            except (keyvault.VaultUnavailable, OSError, ValueError):
                raise HTTPException(409, '持久化失败；原内存配置仍保留，未回退明文') from None
            vault = candidate
            with store.connect() as db:
                db.execute("INSERT INTO settings(scope,key,value,updated_at) VALUES('vault','backend',?,?) "
                           "ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                           (json.dumps(data.backend), now()))
                store.event(db, 'vault.configured', {'backend': data.backend})
            return vault.status()

    @app.put("/api/settings/providers/{provider_id}/key")
    async def set_provider_key(provider_id: str, data: ProviderKeyInput):
        """Store a Provider key in the vault and apply it to its owning runtime(s).

        Saving a key is configuration, not verification: the runtime is left
        unverified until a real request succeeds.
        """
        if provider_id not in catalog.PROVIDERS:
            raise HTTPException(404, "Provider 不存在")
        async with mutation:
            if not enable_codex:
                raise HTTPException(403, "测试实例禁用所有真实连接配置")
            if not data.approve_usage:
                raise HTTPException(403, "需要明确批准该 Provider 的账号调用与可能的费用")
            try:
                vault.set(provider_id, data.api_key.get_secret_value())
            except (ValueError, keyvault.VaultUnavailable) as exc:
                raise HTTPException(422, str(exc)) from None
            record = vault.describe(provider_id)
            applied, refused = [], []
            for runtime_id, provider in RUNTIME_PROVIDERS.items():
                if (getattr(adapters.get(runtime_id), 'provider_id', None) or provider) != provider_id:
                    continue
                adapter = adapters.get(runtime_id)
                if adapter is None or not hasattr(adapter, "configure"):
                    continue
                try:
                    model = (data.model or (store.setting("binding", runtime_id) or {}).get("model")
                             or getattr(adapter, "model", None))
                    adapter.configure(api_key=data.api_key.get_secret_value(), model=model, approved=True)
                    adapter.verified_at = None  # a new key invalidates the old proof
                    if data.model:
                        with store.connect() as db:
                            db.execute("INSERT INTO settings(scope,key,value,updated_at)"
                                       " VALUES('binding',?,?,?) ON CONFLICT(scope,key) DO UPDATE SET"
                                       " value=excluded.value, updated_at=excluded.updated_at",
                                       (runtime_id, json.dumps({"model": data.model}), now()))
                    applied.append(runtime_id)
                    save_binding(runtime_id, adapter)
                except Exception:
                    refused.append(runtime_id)
            with store.connect() as db:
                store.log(db, None,
                          f"{catalog.PROVIDERS[provider_id]['name']} 密钥已保存"
                          f"（后端 {record['backend']}，指纹 {record['fingerprint']}）"
                          + (f"；已应用到 {'、'.join(applied)}" if applied else "")
                          + (f"；未能应用：{'、'.join(refused)}" if refused else "")
                          + "；日志不记录密钥内容",
                          source="permission")
                store.event(db, "provider.key_set", {"provider_id": provider_id,
                                                     "backend": record["backend"],
                                                     "fingerprint": record["fingerprint"],
                                                     "applied": applied, "refused": refused})
            return {"provider": catalog.provider_public(provider_id), "credential": record,
                    "applied_runtimes": applied, "refused_runtimes": refused,
                    "vault": vault.status(),
                    "detail": ("密钥已保存并应用；仍需执行真实连接测试后才会显示为已连接。"
                               if applied else "密钥已保存；没有可应用该 Provider 的执行运行时。")}

    @app.delete("/api/settings/providers/{provider_id}/key")
    async def clear_provider_key(provider_id: str):
        if provider_id not in catalog.PROVIDERS:
            raise HTTPException(404, "Provider 不存在")
        async with mutation:
            record = vault.clear(provider_id)
            cleared = []
            for runtime_id, provider in RUNTIME_PROVIDERS.items():
                if (getattr(adapters.get(runtime_id), 'provider_id', None) or provider) != provider_id:
                    continue
                adapter = adapters.get(runtime_id)
                if adapter is not None and hasattr(adapter, "clear_configuration"):
                    try:
                        adapter.clear_configuration()
                        cleared.append(runtime_id)
                    except Exception:
                        pass
            with store.connect() as db:
                store.log(db, None,
                          f"{catalog.PROVIDERS[provider_id]['name']} 密钥已清除"
                          + (f"；已断开 {'、'.join(cleared)}" if cleared else ""),
                          source="permission")
                store.event(db, "provider.key_cleared", {"provider_id": provider_id, "cleared": cleared})
            return {"provider": catalog.provider_public(provider_id), "credential": record,
                    "cleared_runtimes": cleared, "vault": vault.status()}

    @app.put("/api/settings/plan/{provider_id}")
    async def set_subscription_plan(provider_id: str, data: PlanInput):
        """Declare a subscription tier so the documented credit formula can apply."""
        if provider_id not in catalog.PROVIDERS:
            raise HTTPException(404, "Provider 不存在")
        plans = catalog.PROVIDERS[provider_id]["quota"].get("plans") or {}
        if data.plan is not None and data.plan not in plans:
            raise HTTPException(422, "未知套餐档位；可选：" + "、".join(sorted(plans)))
        async with mutation:
            with store.connect() as db:
                db.execute("INSERT INTO settings(scope,key,value,updated_at) VALUES('plan',?,?,?)"
                           " ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value,"
                           " updated_at=excluded.updated_at",
                           (provider_id, json.dumps({"plan": data.plan}), now()))
                store.log(db, None, f"{catalog.PROVIDERS[provider_id]['name']} 套餐档位声明为 {data.plan}；"
                                    "额度消耗由 Nexus 依据官方公式推算", source="settings")
                store.event(db, "plan.updated", {"provider_id": provider_id, "plan": data.plan})
            return {"provider_id": provider_id, "plan": data.plan, "plans": plans,
                    "statement": "nexus-derived"}

    @app.get("/api/settings/rates")
    async def list_rates():
        return {
            "models": [{"model_key": key, "provider_id": record["provider_id"],
                        "model_id": record["model_id"], "display_name": record["display_name"],
                        "pricing": rate_card.effective(key),
                        "documented": catalog.model_public(key)["pricing"]}
                       for key, record in catalog.MODELS.items()],
            "note": ("费率为独立可编辑配置，不同 Provider 可不同。修改只影响 Nexus 的估算，"
                     "不会改变服务商实际计费。费用一律标注为「估算」。"),
        }

    @app.put("/api/settings/rates/{model_key:path}")
    async def update_rate(model_key: str, data: RateInput):
        async with mutation:
            try:
                updated = rate_card.update(model_key, data.model_dump(exclude_unset=True))
            except (ValueError, catalog.BindingRejected) as exc:
                raise HTTPException(422, str(exc)) from None
            with store.connect() as db:
                store.log(db, None, f"费率已更新：{model_key}；仅影响 Nexus 估算", source="settings")
                store.event(db, "rate.updated", {"model_key": model_key})
            return {"model_key": model_key, "pricing": updated}

    @app.put("/api/agents/{agent_id}/binding")
    async def set_agent_binding(agent_id: str, data: BindingInput):
        """Switch the model and Provider an Agent uses, inside the Agent card."""
        if agent_id not in adapters:
            raise HTTPException(404, "Agent 不存在")
        if agent_id == 'gemini':
            raise HTTPException(409, 'Gemini 模式必须通过会员或 API 授权入口切换，不能只改变 Provider 标签。')
        adapter = adapters[agent_id]
        async with mutation:
            if registry.snapshot(agent_id).get("busy"):
                raise HTTPException(409, "请先取消当前任务，再切换模型或 Provider")
            try:
                binding = catalog.resolve_binding(agent_id, data.provider_id, data.model_key)
            except catalog.BindingRejected as exc:
                raise HTTPException(422, str(exc)) from None
            record = catalog.MODELS.get(binding["model_key"]) if binding["model_key"] else None
            if not binding["verified"] and data.acknowledge_unconfigured is not True:
                raise HTTPException(
                    403, "该 Provider 的接入实现已声明但尚未验证可用；需要显式确认后才会切换。"
                         "Nexus 不会把未验证的组合显示为已连接。")
            if agent_id in API_CONFIGURABLE:
                if hasattr(adapter, 'clear_configuration'):
                    adapter.clear_configuration()
                adapter.provider_id = binding["provider_id"]
                adapter.model_key = binding["model_key"]
                if record is not None:
                    adapter.model = record["model_id"]
                # A binding change invalidates any previous verification: the new
                # combination has not been proven by a real request yet.
                adapter.verified_at = None
                adapter.last_error = None
                if hasattr(adapter, 'endpoint'):
                    adapter.endpoint = catalog.PROVIDERS[binding['provider_id']]['base_url']
                secret = vault.get(binding['provider_id'])
                if secret:
                    if agent_id in ('deepseek-gateway', 'zcode-gateway'):
                        adapter.configure(api_key=secret, model=adapter.model, approved=True,
                                          base_url=catalog.PROVIDERS[binding['provider_id']]['base_url'])
                    else:
                        adapter.configure(api_key=secret, model=adapter.model, approved=True)
            save_binding(agent_id, adapter)
            with store.connect() as db:
                store.log(db, None,
                          f"{adapter.name} 绑定已切换为 {binding['provider_id']} / "
                          f"{binding['model_key'] or '默认'}；需要重新执行真实连接测试",
                          source="settings")
                store.event(db, "agent.binding_changed", {"agent_id": agent_id, **binding})
            return {"agent_id": agent_id, "binding": agent_binding(agent_id),
                    "requires_verification": True,
                    "detail": "绑定已切换；在真实连接测试通过前，该组合不会显示为已连接。"}

    @app.post("/api/agents/{agent_id}/vision-test")
    async def vision_test(agent_id: str, data: VisionTestInput):
        """Declare a vision capability usable only after a real image input succeeds.

        A declared vision capability is not a usable one. This records the real
        outcome and the console keeps showing 「未验证」 until it passes.
        """
        if not enable_codex:
            raise HTTPException(403, "测试实例禁用真实模型请求")
        if agent_id not in adapters:
            raise HTTPException(404, "Agent 不存在")
        adapter = adapters[agent_id]
        model_key = data.model_key or getattr(adapter, "model_key", None)
        if not model_key or model_key not in catalog.MODELS:
            raise HTTPException(422, "该 Agent 当前没有可测试的模型记录")
        declared = "vision" in catalog.MODELS[model_key]["declared"]
        async with mutation:
            if not declared:
                store.record_verification(agent_id, "vision_input", "unsupported",
                                          f"{model_key} 未声明视觉能力；未发起图像请求。",
                                          provider_id=getattr(adapter, "provider_id", None))
                raise HTTPException(422, "该模型未声明视觉能力，未发起图像输入请求")
            if not data.approve_usage:
                raise HTTPException(403, "图像输入测试会真实调用模型并可能产生费用，需要显式批准")
            gate = guard.evaluate(agent_id=agent_id, provider_id=getattr(adapter, "provider_id", None),
                                  model_key=model_key, purpose="vision-test")
            if not gate["allowed"]:
                raise HTTPException(409, "；".join(item["detail"] for item in gate["denials"]))
            result = await run_vision_probe(adapter, model_key)
            store.record_verification(
                agent_id, "vision_input", "verified" if result["passed"] else "failed",
                result["detail"], provider_id=getattr(adapter, "provider_id", None),
                evidence=result.get("evidence", {}))
            with store.connect() as db:
                store.log(db, None, f"{adapter.name} 视觉能力真实测试：" + result["detail"],
                          "info" if result["passed"] else "warning", "capability-test")
                store.event(db, "capability.tested", {"agent_id": agent_id, "kind": "vision_input",
                                                      "passed": result["passed"]})
            return {"agent_id": agent_id, "model_key": model_key, "declared": True, **result}

    @app.get("/api/verifications")
    async def list_verifications(agent_id: str | None = None,
                                 kind: str | None = Query(default=None,
                                                          pattern="^(model_api_callable|task_execution_capable|vision_input|tool_use)$"),
                                 limit: int = Query(default=100, ge=1, le=500)):
        """Historical verification records, kept apart from live connection state.

        A record here proves a real request once succeeded. It is **not** a claim
        that the Provider is reachable right now: expiry and the current process
        decide that separately, and the console shows the two distinctly.
        """
        records = store.verifications(agent_id, kind, limit)
        live = {item["id"]: item for item in agents()}
        return {
            "records": records,
            "live_state": [
                {"runtime_id": runtime_id, "status": item["status"],
                 "model_api_callable": item["model_api_callable"],
                 "task_execution_capable": item["task_execution_capable"],
                 "verified_at": item.get("verified_at"),
                 "logical_agent_id": item["logical_agent_id"]}
                for runtime_id, item in live.items()],
            "note": ("历史验证记录与当前实时连接状态是两件事："
                     "历史成功证明曾经通过真实请求，不代表此刻在线。"
                     "只记录真实请求产生的结果；没有记录即未验证。"),
        }

    def verification_preview():
        rows = []
        occupied = {(t.get('last_attempt') or {}).get('agent_id') or t['agent_id']
                    for t in store.tasks() if t['status'] == 'running'}
        for card in logical_agents():
            routes = card['routes']
            ready = [r for r in routes if r['state'].get('ready')]
            route = next((r for r in ready if r['runtime_id'] == card['preferred_route']), None) or (ready[0] if ready else routes[0])
            runtime_id, info = route['runtime_id'], route['state']
            reason = ('人工交接，没有自动验证接口' if card['manual_handoff'] else
                      '正在执行任务，请稍后验证' if info.get('busy') or runtime_id in occupied else
                      '尚未配置就绪或缺少官方授权' if not info.get('ready') else None)
            rows.append({'agent_id': card['id'], 'name': card['name'], 'runtime_id': runtime_id,
                         'model': info.get('model'), 'provider_id': agent_binding(runtime_id)['provider']['id'],
                         'billing': info.get('billing'), 'eligible': reason is None, 'reason': reason,
                         'binding': orchestration.digest(agent_binding(runtime_id))})
        return {'routes': rows, 'revision': orchestration.digest(rows),
                'note': '每个逻辑 Agent 只验证一条当前就绪路径；没有自动重试、模型切换或文件开发操作。'}

    @app.get('/api/agents/verification-batch')
    async def preview_verification_batch():
        return verification_preview()

    @app.post('/api/agents/verification-batch', status_code=202)
    async def verify_batch(data: BatchVerificationInput):
        if not data.approve_usage:
            raise HTTPException(403, '请批准本次最小请求消耗订阅或 API 额度')
        if not enable_codex:
            raise HTTPException(403, '测试实例禁用真实模型请求')
        preview = verification_preview()
        if data.revision != preview['revision']:
            raise HTTPException(409, '路径配置或运行状态已变化，请重新打开验证确认')
        by_id = {r['runtime_id']: r for r in preview['routes']}
        selected = list(dict.fromkeys(data.runtime_ids))
        if any(k not in by_id or not by_id[k]['eligible'] for k in selected):
            raise HTTPException(409, '所选路径未就绪、忙碌或不在本次预览中')
        results = []
        for runtime_id in selected:
            row = by_id[runtime_id]
            try:
                created = await create_task(TaskInput(title=f"{row['name']} 一键连接验证", agent_id=runtime_id,
                    description='用户批准本次最小真实请求；只验证 nonce，不生成开发文件。'))
                result = await dispatch(created['id'], 'connection', 'NEXUS_' + secrets.token_hex(12),
                                        expected_binding=row['binding'])
                results.append({'runtime_id': runtime_id, 'task_id': created['id'],
                                'accepted': result['accepted'], 'reason': result.get('reason')})
            except HTTPException as exc:
                results.append({'runtime_id': runtime_id, 'accepted': False, 'reason': str(exc.detail)})
            except Exception:
                results.append({'runtime_id': runtime_id, 'accepted': False, 'reason': '该路径验证启动失败，其他路径继续；请查看日志'})
        return {'results': results, 'note': '已提交不等于已连接；最终状态以真实任务结果为准。'}

    @app.post("/api/logical-agents/{agent_id}/reverify", status_code=202)
    async def reverify_logical_agent(agent_id: str, data: ReverifyInput):
        """Re-verify every configured route of one logical Agent.

        Runs against the Provider configuration currently in effect, so the
        result describes this machine's real state rather than a past run.
        """
        card = next((item for item in logical_agents() if item["id"] == agent_id), None)
        if card is None:
            raise HTTPException(404, "Agent 不存在")
        if not enable_codex:
            raise HTTPException(403, "测试实例禁用真实模型请求")
        runtimes = [route["runtime_id"] for route in card["routes"]
                    if route["runtime_id"] == data.runtime_id] if data.runtime_id else \
                   [route["runtime_id"] for route in card["routes"]]
        if not runtimes:
            raise HTTPException(422, "该 Agent 没有可验证的执行路径")
        if data.runtime_id and runtimes == [data.runtime_id] \
                and data.runtime_id not in {route["runtime_id"] for route in card["routes"]}:
            raise HTTPException(422, "该执行路径不属于此 Agent")

        results = []
        for runtime_id in runtimes:
            adapter = adapters.get(runtime_id)
            if adapter is None:
                results.append({"runtime_id": runtime_id, "accepted": False,
                                "reason": "Adapter 不可用"})
                continue
            if getattr(adapter, "execution_mode", None) == "manual":
                results.append({"runtime_id": runtime_id, "accepted": False,
                                "reason": "人工交接模式没有自动验证接口"})
                continue
            if not getattr(adapter, "ready", False):
                reason = "尚未配置可用的 Provider 凭据或授权，无法执行真实请求"
                store.record_verification(
                    runtime_id, "model_api_callable", "skipped", reason,
                    provider_id=RUNTIME_PROVIDERS.get(runtime_id),
                    evidence={"reason": "not_configured"})
                results.append({"runtime_id": runtime_id, "accepted": False, "reason": reason})
                continue
            created = await create_task(TaskInput(
                title=f"{adapter.name} 重新验证连接", agent_id=runtime_id,
                description="重新验证当前 Provider 配置的真实请求；不生成或写入开发文件。"))
            dispatched = await dispatch(created["id"], "connection",
                                        "NEXUS_" + secrets.token_hex(12))
            results.append({"runtime_id": runtime_id, "accepted": dispatched["accepted"],
                            "reason": dispatched.get("reason"), "task_id": created["id"]})
        with store.connect() as db:
            store.log(db, None,
                      f"{agent_id} 重新验证连接：{len(results)} 条执行路径",
                      source="verification")
            store.event(db, "agent.reverified", {"agent_id": agent_id, "results": results})
        return {"agent_id": agent_id, "results": results,
                "note": "验证针对当前真实 Provider 配置执行；结果会写入历史记录。"}

    # ---------------------------------------------------------- vision probe

    # A tiny but real PNG: a 2x2 image whose four pixels are distinct, known
    # colours. The model must read the pixels; a text-only model cannot answer.
    VISION_PROBE_PNG = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000002000000020802000000fdd49a73"
        "00000016504c5445ff000000ff000000ffffffff000000000000000002fea6b4b0"
        "0000000e49444154789c63606060606400000006000230b1e0e20000000049454e44ae426082")
    VISION_EXPECTED = "左上红、右上绿、左下蓝、右下白"

    async def run_vision_probe(adapter, model_key: str) -> dict:
        """Send a real image and require the model to describe the actual pixels.

        Nothing here marks vision as available on a declaration alone: the pixel
        colours must come back correctly in the response.
        """
        if not getattr(adapter, "ready", False):
            return {"passed": False, "detail": "Agent 尚未通过真实连接验证；未发起图像请求。",
                    "evidence": {}}
        try:
            from .gateway_adapter import GatewayRouteAdapter
        except Exception:
            GatewayRouteAdapter = None
        if GatewayRouteAdapter is None or not isinstance(adapter, GatewayRouteAdapter):
            store.record_verification(adapter.id, "vision_input", "unsupported",
                                      "该 Agent 的运行时不支持图像输入探针；未声称视觉可用。",
                                      provider_id=getattr(adapter, "provider_id", None))
            return {"passed": False, "detail": "该 Agent 的运行时不支持图像输入探针；保持未验证。",
                    "evidence": {}}
        import base64
        payload = {
            "model": adapter.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": (
                        "Look at this 2x2 image. Report the colour of each pixel in the order "
                        "top-left, top-right, bottom-left, bottom-right, in Chinese. "
                        "Answer in one short line.")},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/png;base64," + base64.b64encode(VISION_PROBE_PNG).decode("ascii")}},
                ]}],
            "max_tokens": 200,
            "temperature": 0,
            "stream": False,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        observed = usage_module.Usage()
        started = time.monotonic()
        try:
            async with asyncio.timeout(120):
                async with httpx.AsyncClient(transport=adapter._transport, timeout=120,
                                             follow_redirects=False, trust_env=False) as client:
                    response = await client.post(adapter.endpoint + "/chat/completions",
                                                 headers=adapter._headers(), content=body)
                    raw = response.content
            if response.status_code != 200:
                return {"passed": False,
                        "detail": f"图像输入请求返回 HTTP {response.status_code}；未标记视觉可用。",
                        "evidence": {"http_status": response.status_code}}
            document = json.loads(raw.decode("utf-8"))
            observed = usage_module.parse_usage(document.get("usage") if isinstance(document, dict) else None)
            if isinstance(document.get("id"), str):
                observed.request_id = document["id"]
            content = ""
            choices = document.get("choices") if isinstance(document, dict) else None
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message") or {}
                if isinstance(message.get("content"), str):
                    content = message["content"]
            passed = all(colour in content for colour in ("红", "绿", "蓝", "白"))
        except (TimeoutError, httpx.TimeoutException):
            return {"passed": False, "detail": "图像输入请求超时；未标记视觉可用。", "evidence": {}}
        except (httpx.HTTPError, ValueError, UnicodeError):
            return {"passed": False, "detail": "图像输入请求失败或响应不可解析；未标记视觉可用。",
                    "evidence": {}}
        finally:
            latency = int((time.monotonic() - started) * 1000)
        event = usage_module.UsageEvent(
            agent_id=adapter.id, provider_id=getattr(adapter, "provider_id", None) or "unknown",
            model_key=model_key, purpose="vision-test", usage=observed, status=usage_module.STATUS_OK,
            latency_ms=latency, idempotency_key=f"vision-{uuid.uuid4().hex[:8]}")
        collector.record(event)
        return {
            "passed": passed,
            "detail": (f"图像输入真实测试通过：模型正确描述了 2x2 图像像素（{VISION_EXPECTED}）。" if passed else
                       f"图像输入真实测试失败：模型未能正确描述图像像素。响应摘要：{content[:200]}"),
            "evidence": {"usage_source": observed.source, "total_tokens": observed.total_tokens,
                         "latency_ms": latency, "response_excerpt": content[:300]},
            "usage": observed.payload(),
        }

    API_CONFIGURABLE = ("mimo", "gemini", "zcode-gateway", "deepseek-gateway")

    @app.post("/api/agents/{agent_id}/configure")
    async def configure_agent(agent_id: str, data: ConnectionInput):
        if agent_id not in API_CONFIGURABLE:
            raise HTTPException(404, "该 Adapter 没有 API 会话配置入口")
        adapter = adapters[agent_id]
        async with mutation:
            if not enable_codex or not getattr(adapter, "enabled", False):
                raise HTTPException(403, "测试实例禁用所有真实连接配置")
            if not data.approve_usage:
                raise HTTPException(403, "需要用户明确批准账号调用、可能的费用和所选文件发送")
            if registry.snapshot(agent_id).get("busy"):
                raise HTTPException(409, "请先取消当前任务，再更换连接配置")
            try:
                if agent_id in ("zcode-gateway", "deepseek-gateway"):
                    adapter.configure(api_key=data.api_key.get_secret_value(), model=data.model,
                                      approved=True, base_url=data.base_url)
                else:
                    adapter.configure(api_key=data.api_key.get_secret_value(), model=data.model, approved=True)
            except (ValueError, TypeError, AttributeError, BridgeError):
                raise HTTPException(422, "配置不可用，请核对模型名称、接口地址和 API Key；没有调用模型") from None
            provider_id = getattr(adapter, 'provider_id', None) or RUNTIME_PROVIDERS[agent_id]
            try:
                vault.set(provider_id, data.api_key.get_secret_value())
            except (keyvault.VaultUnavailable, OSError, ValueError):
                raise HTTPException(409, '密钥持久化失败；未声称重启可恢复，请检查密钥库状态') from None
            save_binding(agent_id, adapter)
            with store.connect() as db:
                store.log(db, None, f"{adapter.name} 配置保存至 {vault.backend}；尚未验证连接", source="permission")
                store.event(db, "agent.configured", {"agent_id": agent_id})
            return registry.snapshot(agent_id)

    @app.post("/api/agents/gemini/subscription")
    async def configure_subscription(data: SubscriptionInput):
        if not enable_codex:
            raise HTTPException(403, "测试实例禁用账号授权")
        async with mutation:
            if not data.approve_subscription:
                raise HTTPException(403, "需要明确批准官方 Google 登录和订阅额度使用")
            try:
                adapters["gemini"].configure_subscription(approved=True, model=data.model)
            except (ValueError, AttributeError, BridgeError):
                raise HTTPException(409, "会员 CLI 暂不可配置；请查看安装检测和当前任务状态") from None
            with store.connect() as db:
                store.event(db, "agent.configured", {"agent_id": "gemini", "mode": "google-subscription-cli"})
            return registry.snapshot("gemini")

    @app.post("/api/agents/{agent_id}/disconnect")
    async def disconnect_agent(agent_id: str):
        if agent_id not in API_CONFIGURABLE:
            raise HTTPException(404, "该 Adapter 没有 API 会话配置入口")
        async with mutation:
            if registry.snapshot(agent_id).get("busy"):
                raise HTTPException(409, "请先取消当前任务")
            try:
                adapters[agent_id].clear_configuration()
            except (AttributeError, ValueError, BridgeError):
                raise HTTPException(503, "Adapter 会话清理不可用；其他 Agent 不受影响") from None
            save_binding(agent_id, adapters[agent_id])
            with store.connect() as db:
                store.event(db, "agent.disconnected", {"agent_id": agent_id})
            return registry.snapshot(agent_id)

    @app.post("/api/tasks", status_code=201)
    async def create_task(data: TaskInput):
        async with mutation:
            task_id, stamp = uuid.uuid4().hex[:12], now()
            with store.connect() as db:
                validate_dependencies(db, task_id, data.dependencies)
                db.execute("INSERT INTO tasks(id,title,description,agent_id,kind,status,priority,requires_skills,result,worktree,created_at,updated_at,input_files) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    task_id, data.title, data.description, data.agent_id, data.kind, "backlog",
                    data.priority, json.dumps(data.requires_skills), None, None, stamp, stamp, json.dumps(data.input_files)))
                db.execute('UPDATE tasks SET cost_approved=? WHERE id=?', (int(data.cost_approved), task_id))
                for dep in data.dependencies:
                    db.execute("INSERT INTO dependencies VALUES(?,?)", (task_id, dep))
                store.log(db, task_id, f"任务已创建：{data.title}")
                store.event(db, "task.created", {"task_id": task_id})
        if data.auto_run:
            await run_task(task_id)
        return add_gaps(task_or_404(task_id))

    @app.put("/api/tasks/{task_id}")
    async def update_task(task_id: str, data: TaskInput):
        async with mutation:
            task = task_or_404(task_id)
            if collaborator.membership(task_id):
                raise HTTPException(409, '协作任务不能在普通编辑器中变更；请在协作方案中操作')
            if task["status"] in ("running", "done"):
                raise HTTPException(409, "运行中或已完成任务不能修改")
            if task.get('recovery_hold'):
                raise HTTPException(409, '任务需要先完成恢复审核，不能改写执行证据')
            with store.connect() as db:
                validate_dependencies(db, task_id, data.dependencies)
                db.execute("UPDATE handoffs SET phase='cancelled',updated_at=? WHERE task_id=? AND phase IN ('prepared','confirmed','awaiting_review')", (now(), task_id))
                db.execute("UPDATE tasks SET title=?,description=?,agent_id=?,kind=?,priority=?,requires_skills=?,"
                           "status='backlog',result=NULL,input_files=?,updated_at=? WHERE id=?", (
                               data.title, data.description, data.agent_id, data.kind, data.priority,
                               json.dumps(data.requires_skills), json.dumps(data.input_files), now(), task_id))
                db.execute("DELETE FROM dependencies WHERE task_id=?", (task_id,))
                db.execute('UPDATE tasks SET cost_approved=? WHERE id=?', (int(data.cost_approved), task_id))
                for dep in data.dependencies:
                    db.execute("INSERT INTO dependencies VALUES(?,?)", (task_id, dep))
                store.log(db, task_id, "任务与依赖关系已更新")
                store.event(db, "task.updated", {"task_id": task_id})
            return add_gaps(task_or_404(task_id))

    async def log(task_id, message, level="info", source="local-check"):
        with store.connect() as db:
            store.log(db, task_id, message, level, source)

    async def diagnostic(task_id):
        process = None
        try:
            await log(task_id, "开始真实本地环境检查；这不是 AI Agent 执行。")
            commands = [("Python", [sys.executable, "--version"]), ("Git", ["git", "--version"]),
                        ("Node.js", ["node", "--version"])]
            for label, args in commands:
                await log(task_id, f"$ {label} version · cwd=项目工作区", "debug")
                process = await asyncio.create_subprocess_exec(
                    *args, cwd=root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                output, _ = await asyncio.wait_for(process.communicate(), timeout=15)
                if process.returncode:
                    raise RuntimeError(f"{label} 检查失败，exit={process.returncode}")
                await log(task_id, output.decode("utf-8", "replace").strip())
                process = None
            with store.connect() as db:
                result = "Python、Git、Node.js 的真实版本命令全部通过。未调用任何 AI Agent。"
                db.execute("UPDATE tasks SET status='done',result=?,updated_at=? WHERE id=?", (result, now(), task_id))
                store.log(db, task_id, result, source="local-check")
                store.event(db, "task.completed", {"task_id": task_id})
        except asyncio.CancelledError:
            with store.connect() as db:
                db.execute("UPDATE tasks SET status='cancelled',result=?,updated_at=? WHERE id=?",
                           ("本地检查已取消", now(), task_id))
                store.log(db, task_id, "本地检查已取消", "warning")
                store.event(db, "task.cancelled", {"task_id": task_id})
            raise
        except Exception as exc:
            with store.connect() as db:
                db.execute("UPDATE tasks SET status='failed',result=?,updated_at=? WHERE id=?", (str(exc), now(), task_id))
                store.log(db, task_id, str(exc), "error")
                store.event(db, "task.failed", {"task_id": task_id})
        finally:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            running.pop(task_id, None)

    def attempt_update(db, attempt_id, **fields):
        allowed = {"status", "pid", "thread_id", "result", "usage", "artifacts", "error_kind", "error_message", "finished_at", "exit_code"}
        if not fields.keys() <= allowed:
            raise ValueError("Invalid internal attempt update")
        for key in ("usage", "artifacts"):
            if key in fields:
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        db.execute("UPDATE attempts SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?", (*fields.values(), attempt_id))

    def record_usage(adapter, task_id, attempt_id, purpose, observed, *, status, error_kind=None,
                     latency_ms=None, reported_model=None, streaming=False):
        """Persist one request's usage. Never invents counters the provider omitted."""
        checkpoint = checkpoints.get(attempt_id)
        if checkpoint and checkpoint['stage'] in ('RESERVED', 'INPUTS_VALIDATED'):
            return  # No outbound request crossed the durable boundary.
        saved = checkpoint['payload'] if checkpoint else {}
        provider_id = getattr(adapter, "provider_id", None) or catalog.RUNTIMES[adapter.id]["default_binding"]["provider_id"]
        provider_id = saved.get('provider_id') or provider_id
        # Prefer the runtime's own catalog key. Fall back to the model it was
        # actually configured with, so a runtime that does not emit a model_key
        # (for example MiMo) is still attributed to the right model instead of
        # showing up as "未标注" in the per-model statistics.
        model_key = saved.get('model_key') or getattr(adapter, "model_key", None)
        if not model_key:
            configured = getattr(adapter, "model", None)
            if isinstance(configured, str) and provider_id:
                model_key = f"{provider_id}/{configured}"
            # An unrecognised model id is recorded as-is rather than dropped;
            # it must not be silently merged into a different model's totals.
        if reported_model and provider_id:
            candidate = f"{provider_id}/{reported_model}"
            model_key = candidate
        event = usage_module.UsageEvent(
            agent_id=adapter.id, provider_id=provider_id, model_key=model_key, task_id=task_id,
            requested_model=saved.get('requested_model') or getattr(adapter, 'model', None),
            attempt_id=attempt_id, purpose=purpose, usage=observed, streaming=streaming,
            status=status, error_kind=error_kind, latency_ms=latency_ms)
        row_id, duplicate = collector.record(event)
        with store.connect() as db:
            store.log(db, task_id,
                      ("重复事件已合并，未重复计费：记录 #%d" % row_id) if duplicate else
                      ("用量已记录：%s · 输入 %s / 输出 %s / 合计 %s tokens（%s）" % (
                          provider_id, observed.input_tokens, observed.output_tokens,
                          observed.total_tokens, observed.source)),
                      "info", "usage-collector")
            store.event(db, "usage.recorded", {
                "usage_id": row_id, "agent_id": adapter.id, "provider_id": provider_id,
                "model_key": model_key, "task_id": task_id, "usage_source": observed.source,
                "total_tokens": observed.total_tokens, "duplicate": duplicate, "status": status})
        return row_id

    async def agent_task(adapter, task_id, attempt_id, purpose, nonce):
        plan, usage = None, {}
        request_started, request_validated = False, False
        completion_at = None
        observed, latency_ms, reported_model = usage_module.Usage(), None, None
        streaming = 'sse' in adapter.transport
        if hasattr(adapter, 'last_context_task_id'):
            adapter.last_context_task_id = None
        try:
            task = task_or_404(task_id)
            target = worktrees.validate(root, task_id, task["worktree"])
            inputs, hashes = file_changes.context_files(target, task["input_files"])
            inputs.extend(collaborator.context(task_id))
            with store.connect() as db:
                checkpoints.advance(db, attempt_id, 'INPUTS_VALIDATED', input_hashes=hashes)
            objective = (f"Connection test: return summary exactly {nonce}, files=[], approval_requests=[]; do not use tools."
                         if purpose == "connection" else task["title"] + "\n" + task["description"])
            ceiling = guard.settings()['per_task_tokens']
            remaining = max(1, ceiling - collector.totals('all', task_id=task_id)['total_tokens']) if ceiling is not None else None
            planning_only = planning_desk.is_planning(task_id) and purpose != 'connection'
            context = ExecutionContext(task_id, str(target), objective, (), 90 if purpose == "connection" else 300, tuple(inputs), remaining, planning_only)
            await log(task_id, f"真实 {adapter.name} 文件桥接开始；模型生成文件计划，Nexus 校验后写入 Worktree。", source=adapter.id)
            request_started = True
            with store.connect() as db:
                checkpoints.advance(db, attempt_id, 'REQUEST_STARTED', known_result='REMOTE_RESULT_UNKNOWN')
            async for event in adapter.execute(context):
                metadata = event.metadata or {}
                if 'streaming' in metadata:
                    streaming = bool(metadata['streaming'])
                with store.connect() as db:
                    store.log(db, task_id, redact(event.message), metadata.get("level", "info"), adapter.id)
                    if event.kind == "started" and metadata.get("pid"):
                        attempt_update(db, attempt_id, pid=metadata["pid"])
                    elif event.kind == "thread":
                        attempt_update(db, attempt_id, thread_id=metadata["thread_id"])
                    elif event.kind == "completion":
                        if plan is not None:
                            raise BridgeError("protocol", "重复 completion；拒绝落盘")
                        plan = validate_plan(json.dumps(metadata["plan"], ensure_ascii=False))
                        usage = metadata.get("usage", {})
                        observed = metadata.get("observed_usage") or usage_module.parse_usage(usage)
                        observed.request_id = metadata.get("thread_id") or observed.request_id
                        latency_ms = metadata.get("latency_ms")
                        reported_model = metadata.get("reported_model")
                        if metadata.get("exit_code") != 0:
                            raise BridgeError("protocol", "Adapter completion 未确认成功")
                        completion_at = now()
                        checkpoints.advance(db, attempt_id, 'RESPONSE_RECEIVED', known_result='RESPONSE_RECEIVED',
                            metrics=observed.payload(), streaming=streaming,
                            reported_model_key=f"{checkpoints.get(attempt_id, db)['payload']['provider_id']}/{reported_model}" if reported_model else None)
                        token_limit = guard.settings()['per_task_tokens']
                        if token_limit is not None:
                            previous = collector.totals('all', task_id=task_id)['total_tokens']
                            if observed.total_tokens is None or previous + observed.total_tokens > token_limit:
                                raise BridgeError('task_token_limit', '真实用量超过单任务 Token 上限或服务商未报告用量；已停止，不写入文件。远端已产生的用量无法撤回。')
                        attempt_update(db, attempt_id, result=json.dumps(plan, ensure_ascii=False), usage=usage, exit_code=0)
                    elif event.kind == "error":
                        raise BridgeError("execution", event.message)
                    store.event(db, adapter.id + "." + event.kind, {"task_id": task_id, "attempt_id": attempt_id})
            if plan is None:
                raise BridgeError("protocol", "Adapter 没有返回可验证的最终结果")
            if purpose == 'connection' and (plan['summary'] != nonce or plan['files'] or plan['approval_requests']):
                raise BridgeError('protocol', '真实连接测试未返回预期随机文本；未记录连接成功')
            # A valid API/CLI completion is independent of local permission,
            # file-writing and snapshot outcomes. Never retry those side effects.
            request_validated = True
            adapter.verified_at, adapter.last_error = completion_at, None
            adapter.last_error_kind = None
            if purpose == "connection":
                artifacts, result = [], f"{adapter.name} 真实请求验证成功；随机文本匹配、完成事件、exit=0；没有写入文件。"
            else:
                if plan["approval_requests"]:
                    reason = "需要额外授权，未执行也未写入文件：" + "；".join(plan["approval_requests"])
                    with store.connect() as db:
                        attempt_update(db, attempt_id, status="blocked", error_kind="approval_required", error_message=reason, finished_at=now())
                        db.execute("UPDATE tasks SET status='blocked',result=?,updated_at=? WHERE id=?", (reason, now(), task_id))
                        store.log(db, task_id, reason, "warning", "permission")
                        store.event(db, "task.blocked", {"task_id": task_id})
                        checkpoints.halt(db, attempt_id, reason, metrics=observed.payload())
                    record_usage(adapter, task_id, attempt_id, purpose, observed, status=usage_module.STATUS_ERROR,
                                 error_kind='approval_required', latency_ms=latency_ms, reported_model=reported_model, streaming=streaming)
                    return
                if not plan["files"]:
                    raise BridgeError("no_changes", f"{adapter.name} 没有返回文件变更；当前桥接仅支持文件任务。" + plan["summary"])
                target = worktrees.validate(root, task_id, str(target))
                planning_desk.validate_output(task_id, plan['files'])
                collaborator.validate_output(task_id, plan['files'])
                # Record the write boundary only after the shared permission and
                # input-hash preflight. The writer still rechecks before writing.
                file_changes.validate_files(target, plan['files'], hashes)
                with store.connect() as db:
                    checkpoints.advance(db, attempt_id, 'OUTPUT_VALIDATED',
                        proposed_artifacts=[{'path': f['path'], 'size': len(f['content'].encode('utf-8')),
                                             'sha256': file_changes.sha(f['content'].encode('utf-8'))} for f in plan['files']])
                finalizing.add(task_id)
                with store.connect() as db:
                    checkpoints.advance(db, attempt_id, 'WRITING_FILES', known_result='LOCAL_RESULT_UNKNOWN')
                artifacts = file_changes.apply_files(target, plan["files"], hashes)
                for artifact in artifacts:
                    artifact["generated_by"] = adapter.id + ("-cli" if "cli" in adapter.transport else "-api")
                with store.connect() as db:
                    attempt_update(db, attempt_id, artifacts=artifacts)
                    checkpoints.advance(db, attempt_id, 'FILES_WRITTEN', artifacts=artifacts, known_result='FILES_VERIFIED')
                    checkpoints.advance(db, attempt_id, 'SNAPSHOTTING')
                checkpoint = asyncio.create_task(asyncio.to_thread(worktrees.snapshot, root, task_id, artifacts))
                try:
                    commit = await asyncio.shield(checkpoint)
                except asyncio.CancelledError:
                    await checkpoint
                    raise
                for artifact in artifacts:
                    artifact['review_commit'] = commit
                result = plan["summary"] + "\n\n已由 Nexus 校验并写入：" + "、".join(a["path"] for a in artifacts)
            adapter.verified_at = now()
            with store.connect() as db:
                attempt_update(db, attempt_id, status="done", artifacts=artifacts, usage=usage, finished_at=now())
                db.execute("UPDATE tasks SET status='done',result=?,updated_at=? WHERE id=?", (result, now(), task_id))
                for artifact in artifacts:
                    store.log(db, task_id, f"真实文件已落盘：{artifact['path']} · {artifact['size']} bytes · SHA-256 {artifact['sha256']}", source="file-writer")
                store.log(db, task_id, result, source=adapter.id)
                store.event(db, "task.completed", {"task_id": task_id, "attempt_id": attempt_id, "artifacts": artifacts})
                store.event(db, "agents.verified", {"agent_id": adapter.id, "verified_at": adapter.verified_at})
                checkpoints.advance(db, attempt_id, 'COMPLETED', artifacts=artifacts, known_result='COMPLETED')
            record_usage(adapter, task_id, attempt_id, purpose, observed, status=usage_module.STATUS_OK,
                         latency_ms=latency_ms, reported_model=reported_model, streaming=streaming)
            if purpose != 'connection' and artifacts and not planning_only:
                store.record_verification(adapter.id, 'task_execution_capable', 'verified',
                    '真实文件任务已完成；Nexus 验证并写入生成的 Worktree。',
                    provider_id=getattr(adapter, 'provider_id', None) or RUNTIME_PROVIDERS[adapter.id],
                    evidence={'task_id': task_id, 'artifacts': artifacts, 'model': getattr(adapter, 'model', None)})
            if purpose == "connection":
                store.record_verification(
                    adapter.id, "model_api_callable", "verified",
                    f"{adapter.name} 通过真实最小请求：nonce 文本匹配，完成事件与 exit=0 均确认。",
                    provider_id=getattr(adapter, "provider_id", None),
                    evidence={"usage_source": observed.source, "total_tokens": observed.total_tokens,
                              "request_id": observed.request_id})
        except asyncio.CancelledError:
            if getattr(adapter, 'last_context_task_id', None) == task_id:
                observed, usage = adapter.last_observed_usage, dict(adapter.last_attempt_usage)
                latency_ms, reported_model = adapter.last_latency_ms, observed.reported_model
            cleanup_error = None
            try:
                await asyncio.wait_for(adapter.cancel(task_id), timeout=5)
            except Exception:
                cleanup_error = "取消清理接口异常或超时；该 Adapter 已隔离，未写入未完成结果"
                registry.faults[adapter.id] = "cancel_cleanup_error"
            with store.connect() as db:
                attempt_update(db, attempt_id, status="cancelled", usage=usage, error_kind="cancel_cleanup_error" if cleanup_error else "cancelled", error_message=cleanup_error or "本地调用已停止；远端是否停止计费取决于官方接口", finished_at=now())
                detail = '调用已停止；已落盘文件保留待审，未合并。' if task_id in finalizing else '本地执行已取消；未将未完成结果落盘。'
                paused = task_id in pause_requests
                checkpoints.halt(db, attempt_id, cleanup_error or '本地等待已停止；远端结果可能未知', pause=paused, cancelled=True, metrics=observed.payload())
                db.execute("UPDATE tasks SET status=?,result=?,updated_at=? WHERE id=?", ('blocked' if paused else 'cancelled', f"{adapter.name} " + ('已安全暂停本地执行，需恢复审核；不自动重放。' if paused else detail), now(), task_id))
                store.log(db, task_id, detail, "warning", adapter.id)
                store.event(db, "task.cancelled", {"task_id": task_id})
            # A cancelled request may still have been billed upstream, so the
            # attempt is counted; absent reported usage its tokens stay NULL.
            record_usage(adapter, task_id, attempt_id, purpose, observed,
                         status=usage_module.STATUS_ERROR,
                         error_kind="cancelled", latency_ms=latency_ms, reported_model=reported_model, streaming=streaming)
            raise
        except Exception as exc:
            saved_checkpoint = checkpoints.get(attempt_id)
            if saved_checkpoint and saved_checkpoint['state'] == 'COMPLETED':
                # Local result and commit were already durably recorded. A later
                # bookkeeping error must not turn them into a replayable failure.
                with store.connect() as db:
                    store.log(db, task_id, '文件与完成记录已保存；后续记账异常待本地核对，不会重放任务', 'error', 'recovery')
                return
            # A gateway can receive billed usage before its file-plan parser
            # fails. Keep only metadata belonging to this exact request.
            if getattr(adapter, 'last_context_task_id', None) == task_id:
                observed = adapter.last_observed_usage
                usage = dict(adapter.last_attempt_usage)
                latency_ms = adapter.last_latency_ms
                reported_model = observed.reported_model
            kind = getattr(exc, "kind", "permission" if isinstance(exc, ValueError) else "execution")
            message = redact(str(exc))[:8000]
            if kind == 'task_token_limit' and completion_at and purpose != 'connection':
                adapter.verified_at, adapter.last_error = completion_at, None
                adapter.last_error_kind = None
            elif request_started and not request_validated:
                adapter.verified_at = None
                adapter.last_error, adapter.last_error_kind = message, kind
            with store.connect() as db:
                checkpoints.halt(db, attempt_id, message, metrics=observed.payload(), failure_kind=kind)
                attempt_update(db, attempt_id, status="failed", error_kind=kind, error_message=message, usage=usage, exit_code=getattr(exc, "exit_code", None), finished_at=now())
                db.execute("UPDATE tasks SET status='failed',result=?,updated_at=? WHERE id=?", (f"[{kind}] {message}", now(), task_id))
                store.log(db, task_id, f"[{kind}] {message}", "error", adapter.id)
                store.event(db, "task.failed", {"task_id": task_id, "error_kind": kind})
            record_usage(adapter, task_id, attempt_id, purpose, observed,
                         status=usage_module.STATUS_ERROR, error_kind=kind,
                         latency_ms=latency_ms, reported_model=reported_model, streaming=streaming)
        finally:
            finalizing.discard(task_id)
            pause_requests.discard(task_id)
            running.pop(task_id, None)

    async def dispatch(task_id: str, purpose="task", nonce=None, cost_approved=False, selected_agent=None, workflow=False, expected_binding=None, expected_task_revision=None, execution_key=None):
        original = task_or_404(task_id)
        fingerprint = orchestration.digest({'task_id': task_id, 'purpose': purpose, 'cost_approved': cost_approved, 'agent_id': selected_agent})
        def replay():
            if not execution_key:
                return None
            with store.connect() as db:
                receipt = db.execute('SELECT * FROM execution_requests WHERE request_key=?', (execution_key,)).fetchone()
            if receipt:
                if receipt['task_id'] != task_id or receipt['fingerprint'] != fingerprint:
                    raise HTTPException(409, '执行键已用于不同请求，拒绝复用')
                return {'accepted': True, 'replayed': True, 'attempt_id': receipt['attempt_id'],
                        'reason': '原请求已记录；返回原执行回执，没有再次执行', 'task': add_gaps(task_or_404(task_id))}
            return None
        replayed = replay()
        if replayed:
            return replayed
        if original.get('recovery_hold'):
            raise HTTPException(409, '执行结果需要恢复审核；不会重放模型、文件写入或 Git 提交')
        member = collaborator.membership(task_id)
        if member and (not workflow or member[1] is None):
            raise HTTPException(409, '该任务由协作方案管理，请在协作面板批准、启动或暂停')
        requested = original["agent_id"]
        if selected_agent is not None:
            if selected_agent not in adapters or selected_agent == 'workbuddy':
                raise HTTPException(422, "请选择一个真实自动执行路径；人工交接请编辑任务")
            if requested != 'auto' or original['kind'] != 'agent' or purpose != 'task':
                raise HTTPException(409, "执行确认仅适用于自动选择的 Agent 任务")
            requested = selected_agent
        if requested == "workbuddy":
            if handoffs is None:
                raise HTTPException(503, "人工交接模块不可用，其他 Agent 可继续运行")
            if purpose != "task":
                raise HTTPException(409, "WorkBuddy 为人工交接，不提供自动连接测试")
            prepared = await handoffs.prepare(task_id)
            return {"accepted": True, "execution_mode": "manual", "reason": "人工交接已准备；尚未发送或自动执行", "task": add_gaps(task_or_404(task_id)), **prepared}
        if requested in adapters:
            await registry.detect(requested)
        async with mutation:
            replayed = replay()
            if replayed:
                return replayed
            task = task_or_404(task_id)
            if task.get('recovery_hold'):
                raise HTTPException(409, '执行结果需要恢复审核，请先核对检查点')
            if expected_task_revision is not None and expected_task_revision != task_revision(task):
                raise HTTPException(409, '任务目标、输入或依赖已变化，请重新预览后批准')
            member = collaborator.membership(task_id)
            if member and (not workflow or member[1] is None):
                raise HTTPException(409, '任务已纳入协作方案，请刷新')
            if expected_binding is not None and expected_binding != orchestration.digest(agent_binding(requested)):
                raise HTTPException(409, '执行路径的模型或 Provider 已变化，拒绝使用旧批准')
            if task["status"] in ("running", "done"):
                raise HTTPException(409, "任务正在运行或已经完成")
            if selected_agent is not None:
                if task['agent_id'] != 'auto':
                    raise HTTPException(409, "任务执行端已变化，请刷新后重新确认")
                with store.connect() as db:
                    db.execute('UPDATE tasks SET agent_id=?,cost_approved=?,updated_at=? WHERE id=?',
                               (requested, int(cost_approved), now(), task_id))
                    store.event(db, 'task.agent_selected', {'task_id': task_id, 'agent_id': requested, 'previous_agent_id': 'auto'})
                task['agent_id'], task['cost_approved'] = requested, bool(cost_approved)
            if cost_approved and requested != 'auto':
                with store.connect() as db:
                    db.execute('UPDATE tasks SET cost_approved=1 WHERE id=?', (task_id,))
                    store.event(db, 'task.cost_approved', {'task_id': task_id, 'agent_id': requested})
                task['cost_approved'] = True
            all_tasks = {t["id"]: t for t in store.tasks()}
            unfinished = [all_tasks[d]["title"] for d in task["dependencies"] if all_tasks[d]["status"] != "done"]
            busy_ids = {((t.get("last_attempt") or {}).get("agent_id") or t["agent_id"]) for t in all_tasks.values() if t["status"] == "running"}
            reason, adapter = None, None
            if unfinished:
                reason = "依赖尚未完成：" + "、".join(unfinished)
            elif task["requires_skills"] and gaps(task) and not (requested != 'auto' and requested in registry.adapters and registry.snapshot(requested).get('ready') and set(task['requires_skills']).issubset(registry.snapshot(requested).get('supported_capabilities', []))):
                reason = "缺少可执行能力：" + "、".join(gaps(task)) + "；批准 Skill 清单不会安装或激活执行能力。"
            elif task["kind"] == "agent":
                requirements = set(task["requires_skills"]) | {"file.edit" if task["input_files"] else "file.generate"}
                if purpose == "connection":
                    requirements = set()
                adapter = registry.select(requested, requirements, busy_ids, policy)
                if adapter is None:
                    if requested == "auto":
                        holds = registry.auto_hold_reasons(requirements, policy, busy_ids)
                        reason = ("没有可自动派发的 Agent。请点击“选择执行端并运行”，明确选择已配置的 API 路径，"
                                  "批准本次费用后执行。单次费用批准不开放自动选择；"
                                  "Codex 自动派发仍禁用，显式执行仍受预算限制。"
                                  "自动调度只使用真实验证通过、能力完整、空闲且计费方式已获批准的执行器。"
                                  + ("；".join(holds) if holds else "当前没有已注册的 Adapter。"))
                    else:
                        info = registry.snapshot(requested)
                        reason = (f"{info['name']} 正在执行其他任务；请等待或取消后重试。" if requested in busy_ids or info.get("busy") else
                                  "缺少可执行能力：" + "、".join(sorted(requirements - set(info["capabilities"]))) if info.get("ready") else
                                  f"{info['name']} Adapter 尚未连接：{info['detail']}")
                else:
                    # Budget and approval gate. A metered request is never
                    # auto-approved and a reached ceiling stops dispatch.
                    gate = guard.evaluate(
                        agent_id=adapter.id, provider_id=getattr(adapter, "provider_id", None) or RUNTIME_PROVIDERS[adapter.id],
                        model_key=getattr(adapter, "model_key", None), task=task,
                        purpose=purpose, automatic=(requested == "auto"))
                    required_approvals = [a for a in gate['approvals'] if a.get('requires_approval')]
                    if not gate["allowed"] or required_approvals:
                        adapter = None
                        reason = "；".join(item["detail"] for item in [*gate["denials"], *required_approvals])
                        with store.connect() as db:
                            store.log(db, task_id, reason, "warning", "budget-guard")
                            store.event(db, "dispatch.denied", {
                                "task_id": task_id, "codes": [item["code"] for item in [*gate["denials"], *required_approvals]],
                                "plan": gate})
                    else:
                        try:
                            if not task["worktree"]:
                                path = await asyncio.to_thread(worktrees.create, root, task_id)
                                with store.connect() as db:
                                    db.execute("UPDATE tasks SET worktree=?,updated_at=? WHERE id=?", (path, now(), task_id))
                                    store.log(db, task_id, f"已自动创建真实隔离 Worktree：{path}", source="git")
                                    store.event(db, "worktree.created", {"task_id": task_id, "path": path})
                                task["worktree"] = path
                            worktrees.validate(root, task_id, task["worktree"])
                        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                            reason = f"Worktree 白名单检查失败：{exc}"
            elif gaps(task):
                reason = "缺少可执行能力：" + "、".join(gaps(task))
            attempt_id = uuid.uuid4().hex[:12]
            with store.connect() as db:
                db.execute("UPDATE tasks SET status=?,result=?,updated_at=? WHERE id=?", ("blocked" if reason else "running", reason, now(), task_id))
                store.log(db, task_id, reason or (f"调度已接受：真实 {adapter.name} 文件桥接" if adapter else "调度已接受：本地环境检查"), "warning" if reason else "info")
                store.event(db, "task.blocked" if reason else "task.started", {"task_id": task_id, "agent_id": adapter.id if adapter else None})
                if not reason and adapter:
                    db.execute('UPDATE tasks SET cost_approved=0 WHERE id=?', (task_id,))
                    db.execute("INSERT INTO attempts(id,task_id,purpose,status,cli_version,created_at,agent_id,transport,request_profile) VALUES(?,?,?,?,?,?,?,?,?)",
                               (attempt_id, task_id, purpose, "running", getattr(adapter, "version", None), now(), adapter.id, adapter.transport, json.dumps(planning_desk.request_profile(task_id, adapter.id, agent_binding(adapter.id)['execution']))))
                    member = collaborator.membership(task_id)
                    node = next((n for n in collaborator.load(member[0])['nodes'] if n['key'] == member[1]), None) if member and member[1] else None
                    checkpoints.reserve(db, attempt_id, task, adapter.id, agent_binding(adapter.id), node['directory'] if node else None)
                if not reason and execution_key:
                    db.execute('INSERT INTO execution_requests VALUES(?,?,?,?,?)', (execution_key, task_id, attempt_id if adapter else None, fingerprint, now()))
            if not reason:
                running[task_id] = asyncio.create_task(agent_task(adapter, task_id, attempt_id, purpose, nonce) if adapter else diagnostic(task_id))
            return {"accepted": not bool(reason), "reason": reason, "task": add_gaps(task_or_404(task_id))}

    @app.post("/api/tasks/{task_id}/run")
    async def run_task(task_id: str, data: RunInput | None = None):
        return await dispatch(task_id, cost_approved=bool(data and data.cost_approved),
                              selected_agent=data.agent_id if data else None, execution_key=data.execution_key if data else None)

    @app.post("/api/agents/{agent_id}/test", status_code=202)
    async def test_agent(agent_id: str):
        if agent_id not in adapters:
            raise HTTPException(404, "Agent 不存在")
        adapter = adapters[agent_id]
        task = await create_task(TaskInput(title=f"{adapter.name} 真实连接测试", agent_id=agent_id, description="官方接口随机文本验证；不生成或写入开发文件。"))
        return await dispatch(task["id"], "connection", "NEXUS_" + secrets.token_hex(12))

    @app.get("/api/tasks/{task_id}/attempts")
    async def task_attempts(task_id: str):
        task_or_404(task_id)
        return store.attempts(task_id)

    @app.get("/api/tasks/{task_id}/artifacts/{name:path}")
    async def artifact(task_id: str, name: str):
        task = task_or_404(task_id)
        attempts = store.attempts(task_id)
        recorded = next((a for attempt in attempts if attempt["status"] == "done" for a in attempt["artifacts"] if a["path"] == name), None)
        latest_handoff = task.get("latest_handoff")
        if not recorded and latest_handoff and latest_handoff["phase"] == "approved":
            recorded = next((a for a in latest_handoff["artifacts"] if a["path"] == name), None)
        if not recorded:
            raise HTTPException(404, "该文件不是已记录的任务产物")
        try:
            target = worktrees.validate(root, task_id, task["worktree"])
            path = file_changes.checked_path(target, name)
            if path.stat().st_size != recorded["size"] or file_changes.sha(path.read_bytes()) != recorded["sha256"]:
                raise ValueError("产物内容已发生变更")
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return FileResponse(path, media_type="application/octet-stream", filename=path.name)

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str):
        if task_id in finalizing:
            raise HTTPException(409, '任务已进入文件落盘与审核快照阶段；请等待完成后审核，不能声称撤回已落盘文件。')
        async with mutation:
            task_or_404(task_id)
            job = running.get(task_id)
            if not job:
                raise HTTPException(409, "任务没有正在运行的进程")
            if not job.cancelling():
                job.cancel()
        # Cancellation must not hold the scheduler's mutation lock.
        try:
            await asyncio.wait_for(asyncio.shield(job), timeout=10)
        except asyncio.CancelledError:
            if not job.done():
                raise
        except TimeoutError:
            raise HTTPException(504, "取消尚未完成；请检查该 Adapter，其他 Agent 可继续运行") from None
        return task_or_404(task_id)

    @app.get('/api/tasks/{task_id}/recovery')
    async def recovery_preview(task_id: str):
        return await asyncio.to_thread(checkpoints.preview, task_or_404(task_id))

    @app.post('/api/tasks/{task_id}/recovery/review')
    async def recovery_review(task_id: str, data: RecoveryDecision):
        async with mutation:
            return await asyncio.to_thread(checkpoints.release, task_or_404(task_id), data)

    @app.post('/api/tasks/{task_id}/pause')
    async def pause_task(task_id: str):
        task = task_or_404(task_id)
        if task['kind'] != 'agent' or task['status'] != 'running' or not task.get('checkpoint'):
            raise HTTPException(409, '只有本次正在运行且已记录检查点的 Agent 任务可以暂停')
        if task_id in finalizing:
            raise HTTPException(409, '文件落盘或 Git 快照已开始，不能撤回；完成后请审核文件')
        async with collaborator.lock:
            member = collaborator.membership(task_id)
            if member:
                plan = collaborator.load(member[0])
                plan['state'] = 'paused'
                collaborator.save(plan, '子任务请求暂停，停止继续派发；已在运行的其他节点可独立结束')
        pause_requests.add(task_id)
        try:
            await cancel_task(task_id)
        finally:
            pause_requests.discard(task_id)
        return add_gaps(task_or_404(task_id))

    @app.post("/api/tasks/{task_id}/worktree")
    async def create_worktree(task_id: str):
        async with mutation:
            task = task_or_404(task_id)
            if task["worktree"]:
                raise HTTPException(409, "该任务已有 Worktree")
            if task["status"] == "running":
                raise HTTPException(409, "运行中不能更改隔离目录")
            try:
                path = await asyncio.to_thread(worktrees.create, root, task_id)
            except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                raise HTTPException(409, str(exc)) from exc
            with store.connect() as db:
                db.execute("UPDATE tasks SET worktree=?,updated_at=? WHERE id=?", (path, now(), task_id))
                store.log(db, task_id, f"已创建真实 Git Worktree：{path}", source="git")
                store.event(db, "worktree.created", {"task_id": task_id, "path": path})
            return task_or_404(task_id)

    @app.get("/api/logs")
    async def list_logs(task_id: str | None = None, after: int = Query(default=0, ge=0),
                        limit: int = Query(default=300, ge=1, le=2000)):
        if task_id:
            task_or_404(task_id)
        return store.logs(task_id, after, limit)

    @app.get("/api/skills/search")
    async def search_skills(q: str = Query(min_length=1, max_length=200)):
        return {"mode": "external-search-links", "query": q, "links": safety.search_links(q),
                "detail": "仅生成在线检索入口；不会自动抓取、下载、安装或执行搜索结果"}

    @app.post("/api/skills/candidates", status_code=201)
    async def add_skill(data: SkillInput):
        skill_id = uuid.uuid4().hex[:12]
        with store.connect() as db:
            db.execute("INSERT INTO skills VALUES(?,?,?,?,?,?,?,?,?,?)", (
                skill_id, data.name.strip(), data.source_url, data.content, safety.digest(data.content),
                json.dumps(sorted(set(data.permissions))), "candidate", None, None, now()))
            store.log(db, None, f"已提交 Skill 候选清单：{data.name}；未安装", source="skills")
            store.event(db, "skill.submitted", {"skill_id": skill_id})
        return skill_or_404(skill_id)

    @app.post("/api/skills/{skill_id}/review")
    async def review_skill(skill_id: str):
        async with mutation:
            skill = skill_or_404(skill_id)
            if skill["status"] not in ("candidate", "review_blocked"):
                raise HTTPException(409, "此清单已经完成审查")
            result = safety.review(skill["content"], skill["permissions"])
            with store.connect() as db:
                db.execute("UPDATE skills SET review=?,status=? WHERE id=?", (
                    json.dumps(result), "reviewed" if result["passed"] else "review_blocked", skill_id))
                store.log(db, None, f"Skill 静态审查：{skill['name']} · " + ("规则通过" if result["passed"] else "发现风险，已阻止"), source="skills")
                store.event(db, "skill.reviewed", {"skill_id": skill_id})
            return skill_or_404(skill_id)

    @app.post("/api/skills/{skill_id}/trial")
    async def trial_skill(skill_id: str):
        async with mutation:
            skill = skill_or_404(skill_id)
            if skill["status"] != "reviewed":
                raise HTTPException(409, "只有静态审查通过的候选清单可以试运行")
            matches = safety.digest(skill["content"]) == skill["sha256"] == skill["review"]["sha256"]
            permissions_ok = set(skill["permissions"]).issubset(safety.SAFE_PERMISSIONS)
            if not matches or not permissions_ok or not skill["review"]["passed"]:
                raise HTTPException(409, "内容哈希或权限与审查记录不匹配；拒绝试运行")
            result = {"passed": matches and permissions_ok, "mode": "manifest-only", "executed": False,
                      "sha256": skill["sha256"], "time": now(),
                      "checks": ["内容哈希匹配", "权限限定 workspace.read / workspace.write", "第三方代码未执行"],
                      "limitation": "仅清单验证；隔离执行沙箱尚未实现"}
            with store.connect() as db:
                db.execute("UPDATE skills SET trial=?,status='awaiting_approval' WHERE id=?", (json.dumps(result), skill_id))
                store.log(db, None, f"Skill 清单试运行：{skill['name']}；未执行第三方代码", source="skills")
                store.event(db, "skill.trialed", {"skill_id": skill_id})
            return skill_or_404(skill_id)

    @app.post("/api/skills/{skill_id}/decision")
    async def decide_skill(skill_id: str, data: ApprovalInput):
        async with mutation:
            skill = skill_or_404(skill_id)
            if data.decision == "approve" and skill["status"] != "awaiting_approval":
                raise HTTPException(409, "必须完成安全审查及清单试运行后才能批准")
            if skill["status"] in ("approved", "rejected"):
                raise HTTPException(409, "已经决策；如需变更，请重新提交候选清单")
            if not data.reviewer.strip() or not data.note.strip():
                raise HTTPException(422, "需要审批人与审批说明")
            with store.connect() as db:
                db.execute("INSERT INTO approvals(skill_id,decision,reviewer,note,sha256,created_at) VALUES(?,?,?,?,?,?)",
                           (skill_id, data.decision, data.reviewer.strip(), data.note.strip(), skill["sha256"], now()))
                db.execute("UPDATE skills SET status=? WHERE id=?", ("approved" if data.decision == "approve" else "rejected", skill_id))
                store.log(db, None, f"Skill 人工决策：{skill['name']} · {data.decision}；安装功能关闭", source="approval")
                store.event(db, "skill.decided", {"skill_id": skill_id})
            return skill_or_404(skill_id)

    @app.websocket("/ws")
    async def websocket(ws: WebSocket, after: int = Query(default=0, ge=0)):
        protocols = [p.strip() for p in ws.headers.get("sec-websocket-protocol", "").split(",")]
        if ws.headers.get("origin") not in ORIGINS or token not in protocols:
            await ws.close(code=1008)
            return
        await ws.accept(subprotocol="nexus")
        cursor = after
        await ws.send_json({"type": "hello", "cursor": cursor, "time": now()})
        try:
            while True:
                for event in store.events(cursor):
                    await ws.send_json(event)
                    cursor = event["id"]
                try:
                    text = await asyncio.wait_for(ws.receive_text(), timeout=0.35)
                    if text == "ping":
                        await ws.send_json({"type": "pong", "time": now()})
                except asyncio.TimeoutError:
                    pass
        except (WebSocketDisconnect, RuntimeError):
            pass

    collaborator = orchestration.Collaborator(root, store, registry, agent_binding,
                                             lambda tid: add_gaps(task_or_404(tid)), dispatch, cancel_task, mutation)
    app.state.collaborator = collaborator
    orchestration.mount(app, collaborator)
    from .planning import PlanningDesk
    planning_desk = PlanningDesk(app, root, store, registry, collaborator,
                                 lambda tid: add_gaps(task_or_404(tid)), mutation)
    app.state.planning_desk = planning_desk
    from .execution_desk import ExecutionDesk, task_revision
    from . import execution_options
    execution_options.mount(app, store, registry, mutation)
    ExecutionDesk(app, store, registry, collaborator, agent_binding,
                  lambda tid: add_gaps(task_or_404(tid)), dispatch, guard)

    dist = root / "frontend" / "dist"
    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/")
    async def index():
        if not (dist / "index.html").exists():
            raise HTTPException(503, "前端尚未构建：请先运行 npm --prefix frontend run build")
        return FileResponse(dist / "index.html")

    @app.get("/index.html")
    async def index_explicit():
        """Typing the file name explicitly should not 404 on a single-page console."""
        return await index()

    return app
