"""Quota and balance monitoring behind one extensible interface.

Four quantities are routinely conflated and must never be added together, so
each result carries its own ``kind`` and the console renders them in separate
rows:

``official_account_balance``
    Real money or granted credit held on a **vendor's own** account, read from a
    documented vendor endpoint.
``third_party_gateway_balance``
    Balance held at a **reseller** route the user authorized. It is not the
    vendor's balance and never substitutes for it.
``nexus_budget``
    A ceiling **the user set in Nexus**. It is Nexus's own bookkeeping derived
    from observed usage, not a provider statement.
``subscription_allowance``
    A plan allowance (for example GLM Coding Plan credits). It is not money and
    not a topped-up balance.

Capability resolution follows the required order, and stops honestly:

A. A documented provider endpoint is used when one exists **and** a credential
   for it is configured. DeepSeek publishes ``GET /user/balance``.
B. Otherwise, the user's own Nexus budget is computed from real observed usage.
C. Otherwise the result is ``queryable: false`` with the code ``unsupported``
   and the console shows 「余额不可查询」 — while still recording whatever token
   usage the provider does report.

No private interface is guessed, no login cookie is scraped, and no provider
authorization check is bypassed. Where documentation is absent, that absence is
recorded as a finding with its reason.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import httpx

from . import catalog
from .storage import now

OFFICIAL_ACCOUNT_BALANCE = "official_account_balance"
THIRD_PARTY_GATEWAY_BALANCE = "third_party_gateway_balance"
NEXUS_BUDGET = "nexus_budget"
SUBSCRIPTION_ALLOWANCE = "subscription_allowance"

QUOTA_KINDS = (OFFICIAL_ACCOUNT_BALANCE, THIRD_PARTY_GATEWAY_BALANCE, NEXUS_BUDGET, SUBSCRIPTION_ALLOWANCE)

KIND_LABELS = {
    OFFICIAL_ACCOUNT_BALANCE: "官方账户余额",
    THIRD_PARTY_GATEWAY_BALANCE: "第三方中转站账户余额",
    NEXUS_BUDGET: "Nexus 内部预算",
    SUBSCRIPTION_ALLOWANCE: "订阅套餐额度",
}

STATUS_OK = "ok"
STATUS_UNSUPPORTED = "unsupported"
STATUS_CREDENTIAL_REQUIRED = "credential_required"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_ERROR = "error"

BALANCE_TIMEOUT_SECONDS = 15


def _result(kind: str, provider_id: str, *, queryable: bool, status: str, reason: str,
            balances=(), source=None, extra=None) -> dict:
    return {
        "kind": kind,
        "kind_label": KIND_LABELS[kind],
        "provider_id": provider_id,
        "queryable": bool(queryable),
        "status": status,
        "reason": reason,
        "balances": list(balances),
        "source": source,
        "fetched_at": now() if queryable else None,
        "is_provider_statement": kind in (OFFICIAL_ACCOUNT_BALANCE, THIRD_PARTY_GATEWAY_BALANCE),
        **(extra or {}),
    }


class QuotaProvider(ABC):
    """One answer to "how much is left", for exactly one provider and one kind."""

    kind: str
    provider_id: str

    @abstractmethod
    def capability(self) -> dict:
        """Static declaration: can this be queried, and on what basis."""

    @abstractmethod
    async def fetch(self, context: dict) -> dict:
        """Current state. Must return ``queryable: False`` rather than guess."""


class DocumentedBalanceQuota(QuotaProvider):
    """Kind A: a vendor-documented balance endpoint.

    DeepSeek documents ``GET /user/balance`` returning ``is_available`` and
    ``balance_infos[]`` (``currency``, ``total_balance``, ``granted_balance``,
    ``topped_up_balance``). The call is made only when an official-account key
    for *this* provider has been supplied by the user in Nexus; Nexus never
    reads another client's credential file.
    """

    kind = OFFICIAL_ACCOUNT_BALANCE

    def __init__(self, provider_id: str, *, path: str, key_resolver, http_transport=None):
        self.provider_id = provider_id
        self.path = path
        self.key_resolver = key_resolver
        self._transport = http_transport

    def capability(self) -> dict:
        provider = catalog.provider(self.provider_id)
        quota = provider["quota"]
        return {
            "provider_id": self.provider_id,
            "kind": self.kind,
            "documented": True,
            "endpoint": quota.get("endpoint"),
            "source": quota.get("source"),
            "requires": quota.get("requires"),
            "note": quota.get("note"),
        }

    async def fetch(self, context: dict) -> dict:
        provider = catalog.provider(self.provider_id)
        quota = provider["quota"]
        key = self.key_resolver()
        if not key:
            return _result(
                self.kind, self.provider_id, queryable=False, status=STATUS_CREDENTIAL_REQUIRED,
                reason=(f"官方文档化接口 {quota.get('endpoint')} 存在，但本机未配置该 Provider 的官方账号密钥；"
                        "Nexus 不读取其他客户端的凭证文件，也不猜测私有接口。"),
                source=quota.get("source"))
        base = (provider.get("base_url") or "").rstrip("/")
        if not base.startswith("https://"):
            return _result(self.kind, self.provider_id, queryable=False, status=STATUS_ERROR,
                           reason="官方接口地址必须为 https；未发起请求。", source=quota.get("source"))
        try:
            async with asyncio.timeout(BALANCE_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(transport=self._transport, timeout=BALANCE_TIMEOUT_SECONDS,
                                             follow_redirects=False, trust_env=False) as client:
                    response = await client.get(
                        base + self.path,
                        headers={"Authorization": "Bearer " + str(key), "Accept": "application/json",
                                 "User-Agent": "AgentNexus/0.4 (local balance query)"})
        except (TimeoutError, httpx.TimeoutException):
            return _result(self.kind, self.provider_id, queryable=False, status=STATUS_ERROR,
                           reason="余额接口请求超时；未记录响应内容。", source=quota.get("source"))
        except httpx.HTTPError:
            return _result(self.kind, self.provider_id, queryable=False, status=STATUS_ERROR,
                           reason="余额接口网络请求失败；底层错误中的敏感信息未记录。", source=quota.get("source"))
        if response.status_code != 200:
            return _result(self.kind, self.provider_id, queryable=False, status=STATUS_ERROR,
                           reason=f"余额接口返回 HTTP {response.status_code}；未记录响应内容。",
                           source=quota.get("source"))
        try:
            document = response.json()
        except ValueError:
            return _result(self.kind, self.provider_id, queryable=False, status=STATUS_ERROR,
                           reason="余额接口返回的不是 JSON。", source=quota.get("source"))
        balances = []
        entries = document.get("balance_infos") if isinstance(document, dict) else None
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                currency = entry.get("currency")
                if not isinstance(currency, str) or len(currency) > 8:
                    continue
                row = {"currency": currency}
                for field in ("total_balance", "granted_balance", "topped_up_balance"):
                    value = entry.get(field)
                    row[field] = value if isinstance(value, str) and len(value) <= 32 else None
                balances.append(row)
        if not balances:
            return _result(self.kind, self.provider_id, queryable=False, status=STATUS_ERROR,
                           reason="余额接口响应缺少 balance_infos 字段；不显示任何推算值。",
                           source=quota.get("source"))
        return _result(
            self.kind, self.provider_id, queryable=True, status=STATUS_OK,
            reason="官方账户余额，来自服务商文档化接口的真实响应。",
            balances=balances, source=quota.get("source"),
            extra={"is_available": bool(document.get("is_available")), "statement": "provider"})

    async def probe(self, transport) -> dict:
        """Test hook: performs the real request through an injected transport."""
        original, self._transport = self._transport, transport
        try:
            return await self.fetch({})
        finally:
            self._transport = original


class UndocumentedGatewayQuota(QuotaProvider):
    """Kind C fallback for a reseller route with no documented balance API.

    Recording "no documented interface" is the whole point: the console then
    shows 「余额不可查询」 instead of an invented number.
    """

    kind = THIRD_PARTY_GATEWAY_BALANCE

    def __init__(self, provider_id: str):
        self.provider_id = provider_id

    def capability(self) -> dict:
        quota = catalog.provider(self.provider_id)["quota"]
        return {"provider_id": self.provider_id, "kind": self.kind, "documented": False,
                "endpoint": None, "reason": quota.get("reason")}

    async def fetch(self, context: dict) -> dict:
        quota = catalog.provider(self.provider_id)["quota"]
        return _result(self.kind, self.provider_id, queryable=False, status=STATUS_UNSUPPORTED,
                       reason=quota.get("reason") or "该服务商未提供文档化的余额查询接口。",
                       extra={"display": "余额不可查询", "statement": "none"})


class NexusBudgetQuota(QuotaProvider):
    """Kind B: a ceiling the user set in Nexus, measured against real usage."""

    kind = NEXUS_BUDGET
    provider_id = "nexus-budget"

    def __init__(self, collector, settings_reader):
        self.collector = collector
        self.settings_reader = settings_reader

    def capability(self) -> dict:
        return {"provider_id": self.provider_id, "kind": self.kind, "documented": True,
                "endpoint": "Nexus 本地预算设置", "reason": None}

    async def fetch(self, context: dict) -> dict:
        settings = self.settings_reader()
        windows = []
        for label, span, limit_key in (("今日", "today", "daily_budget"), ("本月", "month", "monthly_budget")):
            limit = settings.get(limit_key)
            totals = self.collector.totals(span) if limit is not None else {}
            spent = totals.get('estimated_cost', 0.0)
            unknown = bool(totals.get('unpriced_requests'))
            windows.append({
                "window": label, "span": span,
                "limit": limit,
                "spent_estimate": round(spent, 6) if limit is not None and not unknown else None,
                "remaining_estimate": 0 if limit == 0 else round(max(0.0, limit - spent), 6) if limit is not None and not unknown else None,
                'unknown_cost': unknown,
                "exhausted": bool(limit is not None and spent >= limit) if limit is not None else False,
                "currency": settings.get("currency", "USD"),
            })
        configured = any(item["limit"] is not None for item in windows)
        token_limits = {
            "per_task_tokens": settings.get("per_task_tokens"),
            "per_task_budget": settings.get("per_task_budget"),
        }
        return _result(
            self.kind, self.provider_id,
            queryable=configured, status=STATUS_OK if configured else STATUS_NOT_CONFIGURED,
            reason=("用户自设的 Nexus 预算，基于 Nexus 采集到的实际调用数据计算；"
                    "这不是服务商账单，也不是任何账户余额。" if configured else
                    "尚未设置 Nexus 内部预算上限；因此没有内部剩余额度可显示。"),
            balances=windows, source="Nexus 本地预算设置",
            extra={"statement": "nexus-self-set", "token_limits": token_limits})


class SubscriptionAllowanceQuota(QuotaProvider):
    """Kind D: a plan allowance derived with the vendor's documented formula.

    Z.AI documents the GLM Coding Plan credit calculation and the per-plan
    credits (Lite 2 000/5h & 10 000/week, Pro 12 000/60 000, Max 28 000/140 000).
    The plan tier itself can only come from the user, so the derived figure is
    labelled as a Nexus derivation and never as the vendor's own statement.
    """

    kind = SUBSCRIPTION_ALLOWANCE

    def __init__(self, provider_id: str, *, plan_reader, collector):
        self.provider_id = provider_id
        self.plan_reader = plan_reader
        self.collector = collector

    def capability(self) -> dict:
        quota = catalog.provider(self.provider_id)["quota"]
        return {"provider_id": self.provider_id, "kind": self.kind, "documented": True,
                "endpoint": None, "formula": quota.get("formula"), "plans": quota.get("plans"),
                "source": quota.get("source"), "reason": quota.get("reason")}

    async def fetch(self, context: dict) -> dict:
        quota = catalog.provider(self.provider_id)["quota"]
        selection = self.plan_reader() or {}
        plan_id = selection.get("plan")
        if plan_id not in (quota.get("plans") or {}):
            return _result(
                self.kind, self.provider_id, queryable=False, status=STATUS_NOT_CONFIGURED,
                reason=("官方未提供套餐额度查询接口；需要用户声明套餐档位（Lite / Pro / Max）后，"
                        "Nexus 才能依据官方公式推算额度消耗。"),
                source=quota.get("source"))
        plan = quota["plans"][plan_id]
        rows = self.collector.group_by("model", "week")
        credits = 0.0
        for row in rows:
            key = row["name"]
            record = catalog.MODELS.get(key)
            if not record or record["provider_id"] != self.provider_id:
                continue
            price = record["pricing"]
            credits += (
                row["input_tokens"] * price["input_multiplier"]
                + row["cached_tokens"] * price["cached_input_multiplier"]
                + row["output_tokens"] * price["output_multiplier"]) / 10000
        weekly = plan["weekly_credits"]
        return _result(
            self.kind, self.provider_id, queryable=True, status=STATUS_OK,
            reason=("依据官方文档化 credit 公式，用 Nexus 采集到的真实 token 数据推算的套餐额度消耗；"
                    "套餐档位由用户声明，结果不是服务商账单。"),
            balances=[{"window": "本周", "plan": plan_id, "weekly_credits": weekly,
                       "five_hour_credits": plan["five_hour_credits"],
                       "credits_used_estimate": round(credits, 4),
                       "credits_remaining_estimate": round(max(0.0, weekly - credits), 4),
                       "currency": "credits", "exhausted": credits >= weekly}],
            source=quota.get("source"),
            extra={"statement": "nexus-derived", "formula": quota.get("formula"),
                   "off_peak_multiplier": quota.get("off_peak_multiplier"),
                   "peak_window": quota.get("peak_window"),
                   "note": "推算未区分高峰/非高峰时段权重；官方非高峰按 50% 计，实际消耗可能低于本推算值。"})


class QuotaRegistry:
    """Every quota answer Nexus can give, resolved in documented order."""

    def __init__(self, providers: list[QuotaProvider]):
        self.providers = providers

    def capability(self) -> dict:
        return {
            "kinds": [{"id": kind, "label": KIND_LABELS[kind]} for kind in QUOTA_KINDS],
            "providers": [item.capability() for item in self.providers],
            "note": ("四类额度互不混用：官方账户余额、第三方中转站余额、Nexus 内部预算、订阅套餐额度。"
                     "没有文档化接口时结果为 unsupported，界面显示「余额不可查询」。"),
        }

    async def fetch_all(self) -> dict:
        results = await asyncio.gather(*(item.fetch({}) for item in self.providers), return_exceptions=True)
        payload = []
        for item, result in zip(self.providers, results):
            if isinstance(result, BaseException):
                payload.append(_result(item.kind, item.provider_id, queryable=False, status=STATUS_ERROR,
                                       reason="额度查询失败；其他 Provider 不受影响。"))
            else:
                payload.append(result)
        by_kind: dict[str, list] = {kind: [] for kind in QUOTA_KINDS}
        for item in payload:
            by_kind[item["kind"]].append(item)
        return {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "kinds": by_kind,
            "results": payload,
            "unqueryable": [item["provider_id"] for item in payload if not item["queryable"]],
            "note": "查询失败的 Provider 只影响自身；不会用估算值冒充余额。",
        }


def build_registry(*, collector, official_keys, budget_settings, plan_selection) -> QuotaRegistry:
    """Assemble the registry from documented capabilities, not from guesses."""
    providers: list[QuotaProvider] = []
    for provider_id, record in catalog.PROVIDERS.items():
        quota = record["quota"]
        capability = quota.get("capability")
        if capability == "documented-endpoint":
            path = (quota.get("endpoint") or "").split(" ", 1)[-1].strip() or "/user/balance"
            providers.append(DocumentedBalanceQuota(
                provider_id, path=path,
                key_resolver=lambda pid=provider_id: official_keys.get(pid)))
        elif provider_id in ("gonode-gateway", "hyai666-gateway"):
            providers.append(UndocumentedGatewayQuota(provider_id))
        elif capability == "documented-formula":
            providers.append(SubscriptionAllowanceQuota(
                provider_id, plan_reader=lambda pid=provider_id: plan_selection.get(pid), collector=collector))
    providers.append(NexusBudgetQuota(collector, budget_settings))
    return QuotaRegistry(providers)
