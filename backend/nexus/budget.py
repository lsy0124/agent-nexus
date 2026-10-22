"""Cost estimation and the budget guard that stops dispatch.

Two separations this module will not break:

**Estimate is not a bill.** Every cost figure Nexus produces is derived from a
vendor price list multiplied by tokens the provider reported. It is stored with
``cost_source`` and rendered as 「估算」. Only ``credit`` values that a provider
itself returned are marked as provider-reported.

**Unpriceable is not free.** The user's third-party gateway routes publish no
price list, so their cost is ``not-estimable`` — the console shows that instead
of substituting the official DeepSeek or Z.AI price for a reseller route.

The guard enforces per-task token and budget ceilings, daily and monthly
budgets, and call counts, and it refuses dispatch once a ceiling is reached.
It also re-asserts that automatic dispatch never falls back to GPT/Codex: that
is a policy fact, and a budget component must not be able to undo it.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

from . import catalog

# Singapore time, used for the Z.AI documented peak window.
_SGT = timezone(timedelta(hours=8))

DEFAULT_SETTINGS: dict = {
    # Ceilings are ``None`` until the user sets them: an unset ceiling means
    # "not configured", never "unlimited spending approved".
    "per_task_tokens": None,
    "per_task_budget": None,
    "daily_budget": None,
    "monthly_budget": None,
    "daily_calls": None,
    "monthly_calls": None,
    'codex_daily_calls': None,
    "currency": "USD",
    # A metered route always needs a per-task human approval.
    "require_metered_approval": True,
    "stop_dispatch_when_exhausted": True,
    "note": "由用户自行设置的 Nexus 预算上限；不是服务商账户余额，也不是账单。",
}

# DeepSeek documents peak hours as 01:00–04:00 and 06:00–10:00 UTC, Monday to
# Friday, excluding Chinese public holidays (holidays cannot be resolved on this
# machine, and the estimate says so).
DEEPSEEK_PEAK_UTC = ((1, 4), (6, 10))
# Z.AI documents peak as Monday–Friday 14:00–18:00 Singapore time (UTC+8).
ZAI_PEAK_SGT = ((14, 18),)


def is_peak(provider_id: str, moment: datetime) -> bool | None:
    """Whether a timestamp falls in the provider's documented peak window.

    ``None`` means the provider documents no peak/off-peak distinction.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if provider_id == "deepseek-official":
        utc = moment.astimezone(timezone.utc)
        if utc.weekday() >= 5:
            return False
        return any(start <= utc.hour < end for start, end in DEEPSEEK_PEAK_UTC)
    if provider_id == "zai-official":
        local = moment.astimezone(timezone.utc).astimezone(_SGT)
        if local.weekday() >= 5:
            return False
        return any(start <= local.hour < end for start, end in ZAI_PEAK_SGT)
    return None


def _rate(pricing: dict, field: str, peak: bool | None):
    value = pricing.get(field)
    if isinstance(value, dict):
        if peak is None:
            return value.get("off_peak")
        return value.get("peak") if peak else value.get("off_peak")
    return value


class RateCard:
    """Editable per-model rates. Different providers may price differently."""

    def __init__(self, store):
        self.store = store

    def defaults(self) -> dict:
        return {key: dict(record["pricing"]) for key, record in catalog.MODELS.items()}

    def overrides(self) -> dict:
        with self.store.connect() as db:
            return {row["model_key"]: json.loads(row["payload"]) for row in
                    db.execute("SELECT model_key, payload FROM rate_card")}

    def effective(self, model_key: str) -> dict:
        base = dict(catalog.model(model_key)["pricing"])
        override = self.overrides().get(model_key)
        if isinstance(override, dict):
            base.update(override)
            base["override"] = True
        return base

    def update(self, model_key: str, patch: dict) -> dict:
        catalog.model(model_key)  # rejects an unknown model before writing
        allowed = {"currency", "unit", "input_cache_hit", "input_cache_miss", "output",
                   "input_multiplier", "cached_input_multiplier", "output_multiplier",
                   "revision", "source", "note"}
        unknown = set(patch) - allowed
        if unknown:
            raise ValueError("费率字段不被支持：" + "、".join(sorted(unknown)))
        for key, value in patch.items():
            if key in ("input_cache_hit", "input_cache_miss", "output"):
                if not isinstance(value, dict) or not set(value) <= {"peak", "off_peak"}:
                    raise ValueError(f"{key} 必须是包含 peak / off_peak 的对象")
                for rate in value.values():
                    if rate is not None and (not isinstance(rate, (int, float)) or rate < 0 or rate > 100_000):
                        raise ValueError(f"{key} 费率超出合理范围")
            elif key.endswith("multiplier"):
                if value is not None and (not isinstance(value, (int, float)) or value < 0 or value > 10_000):
                    raise ValueError(f"{key} 倍率超出合理范围")
            elif key in ("currency", "unit", "revision", "source", "note"):
                if value is not None and (not isinstance(value, str) or len(value) > 500):
                    raise ValueError(f"{key} 必须是长度不超过 500 的文本")
        merged = {**self.effective(model_key), **patch}
        merged.pop("override", None)
        with self.store.connect() as db:
            db.execute("INSERT INTO rate_card(model_key,payload,updated_at) VALUES(?,?,?)"
                       " ON CONFLICT(model_key) DO UPDATE SET payload=excluded.payload,"
                       " updated_at=excluded.updated_at",
                       (model_key, json.dumps(merged, ensure_ascii=False), datetime.now(timezone.utc).isoformat()))
        return self.effective(model_key)

    def estimate(self, provider_id: str, model_key: str | None, usage, at: str | None = None) -> dict:
        """Label the cost of one request. Never returns a bare number."""
        if not usage.has_any:
            return {'cost_source': 'not-estimable', 'estimated_cost': None, 'currency': None,
                    'reason': '服务商未返回 Token 用量，不能将未知费用计为零。'}
        if not model_key or model_key not in catalog.MODELS:
            return {"cost_source": "not-estimable", "estimated_cost": None, "currency": None,
                    "price_revision": None,
                    "reason": "该模型没有可用的费率记录；费用显示为不可估算，未用其他 Provider 的价格代替。"}
        pricing = self.effective(model_key)
        revision = pricing.get("revision")
        moment = datetime.fromisoformat(at) if at else datetime.now(timezone.utc)
        if pricing.get("input_multiplier") is not None:
            return self._estimate_credits(provider_id, usage, pricing, revision, moment)
        if pricing.get("currency") != "USD":
            return {"cost_source": "not-estimable", "estimated_cost": None,
                    "currency": pricing.get("currency"), "price_revision": revision,
                    "reason": pricing.get("note") or "该 Provider 未公布可换算的价目表。"}
        peak = is_peak(provider_id, moment)
        miss_rate = _rate(pricing, "input_cache_miss", peak)
        hit_rate = _rate(pricing, "input_cache_hit", peak)
        out_rate = _rate(pricing, "output", peak)
        if None in (miss_rate, out_rate):
            return {"cost_source": "not-estimable", "estimated_cost": None, "currency": "USD",
                    "price_revision": revision, "reason": "价目表缺少必要费率。"}
        input_tokens = usage.input_tokens or 0
        cached = usage.cached_tokens or 0
        assumptions = []
        if cached > input_tokens:
            # The two counters are additive in that response; bill both parts.
            billable_miss, billable_hit = input_tokens, cached
            assumptions.append("cached_tokens 大于 input_tokens，按两部分相加计价")
        else:
            billable_miss, billable_hit = max(0, input_tokens - cached), cached
            assumptions.append("cached_tokens 视为 input_tokens 的已缓存部分")
        cost = (billable_miss * miss_rate + billable_hit * (hit_rate if hit_rate is not None else miss_rate)
                + (usage.output_tokens or 0) * out_rate) / 1_000_000
        return {
            "cost_source": "nexus-estimate",
            "estimated_cost": round(cost, 8),
            "currency": "USD",
            "price_revision": revision,
            "peak": peak,
            "breakdown": {"cache_miss_tokens": billable_miss, "cache_hit_tokens": billable_hit,
                          "output_tokens": usage.output_tokens or 0,
                          "cache_miss_rate": miss_rate, "cache_hit_rate": hit_rate, "output_rate": out_rate},
            "assumptions": assumptions + (["未按中国法定节假日调整高峰判定；官方文档以此为高峰例外"]
                                          if peak is not None and provider_id == "deepseek-official" else []),
            "note": "Nexus 依据服务商公开价目表推算的估算值，不是服务商账单。",
        }

    def _estimate_credits(self, provider_id, usage, pricing, revision, moment) -> dict:
        credits = ((usage.input_tokens or 0) * pricing["input_multiplier"]
                   + (usage.cached_tokens or 0) * pricing["cached_input_multiplier"]
                   + (usage.output_tokens or 0) * pricing["output_multiplier"]) / 10000
        return {
            "cost_source": "nexus-estimate-credits",
            "estimated_cost": round(credits, 6),
            "currency": "credits",
            "price_revision": revision,
            "breakdown": {"input_tokens": usage.input_tokens or 0, "cached_tokens": usage.cached_tokens or 0,
                          "output_tokens": usage.output_tokens or 0},
            "assumptions": ["按官方 credit 公式计算；未计入非高峰 50% 折扣时段权重"],
            "note": "credit 为官方文档化的套餐计量单位，不是货币金额；该值为估算。",
        }


class BudgetGuard:
    """Refuses dispatch when a ceiling is reached, with a stated reason."""

    def __init__(self, store, collector, rate_card: RateCard, policy=None):
        self.store, self.collector, self.rate_card, self.policy = store, collector, rate_card, policy

    # ------------------------------------------------------------- settings

    def settings(self) -> dict:
        with self.store.connect() as db:
            stored = {row["key"]: json.loads(row["value"]) for row in
                      db.execute("SELECT key, value FROM settings WHERE scope='budget'")}
        return {**DEFAULT_SETTINGS, **stored}

    def update_settings(self, patch: dict) -> dict:
        allowed = set(DEFAULT_SETTINGS)
        unknown = set(patch) - allowed
        if unknown:
            raise ValueError("预算字段不被支持：" + "、".join(sorted(unknown)))
        clean: dict = {}
        for key, value in patch.items():
            if key == "note":
                continue
            if key == "currency":
                if value not in ("USD", "CNY"):
                    raise ValueError("币种只支持 USD 或 CNY")
            elif key in ("require_metered_approval", "stop_dispatch_when_exhausted"):
                if not isinstance(value, bool):
                    raise ValueError(f"{key} 必须是布尔值")
            elif value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or value > 1_000_000_000:
                    raise ValueError(f"{key} 必须是非负数字或留空")
                value = float(value) if "budget" in key else int(value)
            clean[key] = value
        with self.store.connect() as db:
            for key, value in clean.items():
                db.execute("INSERT INTO settings(scope,key,value,updated_at) VALUES('budget',?,?,?)"
                           " ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value,"
                           " updated_at=excluded.updated_at",
                           (key, json.dumps(value), datetime.now(timezone.utc).isoformat()))
        return self.settings()

    # ---------------------------------------------------------------- guard

    def evaluate(self, *, agent_id: str, provider_id: str | None, model_key: str | None,
                 task: dict | None = None, purpose: str = "task", automatic: bool = False) -> dict:
        """May this dispatch proceed? Every denial carries a reason the UI shows."""
        settings = self.settings()
        billing = catalog.PROVIDERS.get(provider_id or "", {}).get("billing")
        denials: list[dict] = []
        approvals: list[dict] = []
        with self.store.connect() as db:
            active = db.execute("SELECT COUNT(*) FROM attempts WHERE status='running'").fetchone()[0]
            codex_active = db.execute("SELECT COUNT(*) FROM attempts WHERE status='running' AND agent_id='codex'").fetchone()[0]
        if agent_id == 'codex' and settings['codex_daily_calls'] is not None:
            used = self.collector.totals('today', agent_id='codex')['requests'] + codex_active
            if used >= settings['codex_daily_calls']:
                denials.append({'code': 'codex_call_limit', 'requires_approval': True,
                                'detail': f"Codex 今日 Nexus 调用已达上限 {used}/{settings['codex_daily_calls']}；无后台重试或自动切换。"})
        financial_limits = any(settings[key] is not None for key in ('per_task_budget', 'daily_budget', 'monthly_budget'))
        if billing == 'metered-api' and financial_limits:
            pricing = self.rate_card.effective(model_key) if model_key in catalog.MODELS else {}
            peak = is_peak(provider_id, datetime.now(timezone.utc))
            if (pricing.get('currency') != 'USD' or pricing.get('currency') != settings['currency']
                    or _rate(pricing, 'input_cache_miss', peak) is None or _rate(pricing, 'output', peak) is None):
                denials.append({'code': 'budget_unenforceable', 'requires_approval': True,
                                'detail': '该路由没有可用的同币种费率，无法强制金额预算；已停止派发，请使用次数/Token 限额并明确批准。'})

        if automatic and agent_id == "codex":
            denials.append({
                "code": "gpt_auto_fallback_forbidden", "requires_approval": False,
                "detail": "策略禁止自动回退到 Codex/GPT；自动调度永远不会选择它。显式单次任务不受影响。",
            })

        if settings["stop_dispatch_when_exhausted"]:
            for label, span, limit_key in (("每日", "today", "daily_budget"),
                                           ("每月", "month", "monthly_budget")):
                limit = settings[limit_key]
                if limit is None:
                    continue
                window_totals = self.collector.totals(span)
                spent = window_totals["estimated_cost"]
                if window_totals.get('unpriced_requests'):
                    denials.append({'code': 'unknown_prior_cost', 'requires_approval': True,
                                    'detail': f'{label}历史中含不可估算费用；不能声称剩余额度可用，已停止金额预算派发。'})
                if spent >= limit:
                    denials.append({
                        "code": "budget_exhausted", "requires_approval": True,
                        "detail": (f"{label}预算已用尽（估算 {spent:.4f} / 上限 {limit} {settings['currency']}）；"
                                   "已停止继续派发。提高预算后才会恢复。"),
                        "spent": round(spent, 6), "limit": limit, "window": span,
                    })
            for label, span, limit_key in (("每日", "today", "daily_calls"),
                                           ("每月", "month", "monthly_calls")):
                limit = settings[limit_key]
                if limit is None:
                    continue
                used = self.collector.totals(span)["requests"] + active
                if used >= limit:
                    denials.append({
                        "code": "call_limit_reached", "requires_approval": True,
                        "detail": f"{label}调用次数已达上限（{used} / {limit}）；已停止继续派发。",
                        "used": used, "limit": limit, "window": span,
                    })

        if task is not None:
            task_totals = self.collector.totals("all", task_id=task["id"])
            token_limit = settings["per_task_tokens"]
            if token_limit is not None and task_totals["total_tokens"] >= token_limit:
                denials.append({
                    "code": "task_token_limit", "requires_approval": True,
                    "detail": (f"单任务 Token 上限已达 {task_totals['total_tokens']} / {token_limit}；"
                               "该任务不再继续派发。"),
                    "used": task_totals["total_tokens"], "limit": token_limit,
                })
            budget_limit = settings["per_task_budget"]
            if budget_limit is not None:
                unpriceable = (task_totals["requests"] > 0 and task_totals["estimated_cost"] == 0
                               and provider_id is not None
                               and (catalog.PROVIDERS.get(provider_id, {}).get("quota", {})
                                    .get("capability") == "none"))
                if task_totals["estimated_cost"] >= budget_limit:
                    denials.append({
                        "code": "task_budget_limit", "requires_approval": True,
                        "detail": (f"单任务预算上限已达（估算 {task_totals['estimated_cost']:.4f} / {budget_limit}）；"
                                   "该任务不再继续派发。"),
                        "used": round(task_totals["estimated_cost"], 6), "limit": budget_limit,
                    })
                elif unpriceable:
                    # Refusing to invent a price is correct, but silently letting
                    # an unpriceable route ignore a budget ceiling is not: say so.
                    approvals.append({
                        "code": "budget_unenforceable", "requires_approval": False,
                        "detail": (f"该 Provider 未公布价目表，Nexus 无法估算费用，因此单任务预算上限 "
                                   f"{budget_limit} 无法对其强制执行；请改用单任务 Token 上限或调用次数上限。"),
                    })

        if billing == "metered-api" and settings["require_metered_approval"] and purpose != "connection":
            approved = bool((task or {}).get("cost_approved")) if task is not None else False
            if not approved and automatic:
                denials.append({
                    "code": "metered_needs_approval", "requires_approval": True,
                    "detail": ("按量计费 Provider 需要逐次人工批准；自动派发不会代为批准，也不会改走其他 Provider。"),
                })
            elif not approved:
                approvals.append({
                    "code": "metered_needs_approval", "requires_approval": True,
                    "detail": "本次调用按量计费，需要用户显式批准费用后才会提交。",
                })

        return {
            "allowed": not denials,
            "denials": denials,
            "approvals": approvals,
            "billing": billing,
            "checks": {
                "per_task_tokens": settings["per_task_tokens"],
                "per_task_budget": settings["per_task_budget"],
                "daily_budget": settings["daily_budget"],
                "monthly_budget": settings["monthly_budget"],
                "daily_calls": settings["daily_calls"],
                "monthly_calls": settings["monthly_calls"],
                "currency": settings["currency"],
                "stop_dispatch_when_exhausted": settings["stop_dispatch_when_exhausted"],
            },
            "gpt_auto_dispatch": False,
            "note": ("金额均为 Nexus 估算，不是服务商账单。预算耗尽的判定基于已采集到的真实调用数据。"),
        }

    def status(self) -> dict:
        """Current budget position for the monitoring page."""
        settings = self.settings()
        usage_today = self.collector.totals("today")
        usage_month = self.collector.totals("month")
        codex_calls = self.collector.totals('today', agent_id='codex')['requests']
        rows = []
        for label, span, limit_key, call_key, snapshot in (
                ("今日", "today", "daily_budget", "daily_calls", usage_today),
                ("本月", "month", "monthly_budget", "monthly_calls", usage_month)):
            rows.append({
                "window": label, "span": span,
                "budget_limit": settings[limit_key],
                "spent_estimate": round(snapshot["estimated_cost"], 6),
                "remaining_estimate": (0 if settings[limit_key] == 0 else round(max(0.0, settings[limit_key] - snapshot["estimated_cost"]), 6)
                                       if settings[limit_key] is not None and not snapshot.get('unpriced_requests') else None),
                "call_limit": settings[call_key], "calls": snapshot["requests"],
                "tokens": snapshot["total_tokens"],
                "exhausted": bool((settings[limit_key] is not None and snapshot["estimated_cost"] >= settings[limit_key])
                                  or (settings[call_key] is not None and snapshot['requests'] >= settings[call_key])),
            })
        warnings = [f"{row['window']}预算或调用次数已达上限" for row in rows if row['exhausted']]
        if settings['codex_daily_calls'] is not None and codex_calls >= settings['codex_daily_calls']:
            warnings.append('Codex 今日 Nexus 调用次数已达上限')
        return {
            "settings": settings,
            'warnings': warnings,
            'codex_calls': {'used': codex_calls, 'limit': settings['codex_daily_calls'], 'source': 'nexus-call-count', 'note': '不是官方会员余额或剩余额度'},
            "windows": rows,
            "token_limits": {"per_task_tokens": settings["per_task_tokens"],
                             "per_task_budget": settings["per_task_budget"]},
            "gpt_auto_dispatch": False,
            "cost_source": "nexus-estimate",
            "unenforceable": {
                "budget": settings["per_task_budget"] is not None,
                "providers": [provider_id for provider_id, record in catalog.PROVIDERS.items()
                              if record["quota"]["capability"] == "none"],
                "detail": ("未公布价目表的 Provider 无法被金额上限约束；"
                           "对这些 Provider 请使用单任务 Token 上限或调用次数上限。"),
            },
            "note": ("预算金额为 Nexus 估算，Token 只计真实上报；周期按 UTC。输出上限只约束请求参数；超限上报后停止落盘与后续派发，不能撤回已产生的远端费用。"),
        }
