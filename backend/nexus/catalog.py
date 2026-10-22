"""Three-layer capability catalog: Agent -> execution runtime -> Model -> Provider.

The console must never blur four different things together, so they are separate
records here with separate identifiers:

* **Provider** — who serves the model: an official API, a user-authorized
  third-party OpenAI-compatible gateway, an official CLI, a desktop client or a
  local runtime.
* **Model** — the inference model and its *declared* capabilities, context
  window and documented price.
* **Runtime** — how an Agent actually executes: an HTTP adapter, an official CLI
  bridge, a desktop client, a human handoff.
* **Agent** — the executor the user dispatches a task to. One Agent may be bound
  to several models and providers, and one Provider may serve several Agents.

Two capability facts stay deliberately apart, because conflating them is how a
console starts lying:

``model_api_callable``
    A real request to this model through this runtime returned a valid response.
    It says nothing about whether the Agent can finish a development task.

``task_execution_capable``
    The Agent can take a task to a validated deliverable. A gateway route can
    produce a structured file plan and still have no tools, no shell and no
    filesystem authority — that is recorded as ``structured-files`` scope, not
    as full execution.

Everything here is a *declaration* sourced from vendor documentation, or a
*verification* recorded after a real request. Nothing in this module performs a
network call, installs software, authenticates or pays for anything.
"""
from __future__ import annotations

# --------------------------------------------------------------------- facts

# Vendors document these; the URLs are recorded so the console can cite them.
DEEPSEEK_PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing"
DEEPSEEK_BALANCE_URL = "https://api-docs.deepseek.com/api/get-user-balance"
DEEPSEEK_VISION_URL = "https://api-docs.deepseek.com/quick_start/token_usage"
ZAI_CODING_PLAN_URL = "https://docs.z.ai/devpack/overview"
ZAI_API_REFERENCE_URL = "https://docs.z.ai/api-reference/introduction"

PROVIDER_KINDS = (
    "official-api",          # first-party HTTP API operated by the vendor
    "third-party-gateway",   # user-authorized OpenAI-compatible reseller route
    "official-cli",          # vendor CLI run locally
    "desktop-client",        # vendor desktop application
    "local-runtime",         # locally installed agent runtime
    "manual",                # no automatic interface at all
)

# How a provider's remaining allowance can be read. Never guessed.
QUOTA_CAPABILITIES = (
    "documented-endpoint",   # vendor documents a balance/quota/billing HTTP call
    "documented-formula",    # vendor documents how consumption is computed
    "none",                  # no documented interface exists
)

# ``none`` is a finding, not a failure: it is recorded with its reason.
PROVIDERS: dict[str, dict] = {
    "deepseek-official": {
        "name": "DeepSeek 官方 API",
        "kind": "official-api",
        "base_url": "https://api.deepseek.com",
        "billing": "metered-api",
        "credential": "official-api-key",
        "docs": (
            {"title": "DeepSeek Models & Pricing（官方价目表）", "url": DEEPSEEK_PRICING_URL},
            {"title": "DeepSeek Get User Balance（官方余额接口）", "url": DEEPSEEK_BALANCE_URL},
        ),
        "quota": {
            "capability": "documented-endpoint",
            "kind": "official_account_balance",
            "endpoint": "GET /user/balance",
            "returns": ["is_available", "balance_infos[].currency", "balance_infos[].total_balance",
                        "balance_infos[].granted_balance", "balance_infos[].topped_up_balance"],
            "source": DEEPSEEK_BALANCE_URL,
            "requires": "official-api-key",
            "note": "官方文档化的余额接口；只有配置了官方账号密钥后才可查询。Nexus 不读取其他客户端的凭证文件。",
        },
        "note": "官方按量计费 API。与用户当前使用的第三方网关是两条不同路由，余额与账单互不相通。",
    },
    "gonode-gateway": {
        "name": "gonode.si 第三方网关",
        "kind": "third-party-gateway",
        "base_url": "https://gonode.si/v1",
        "billing": "metered-api",
        "credential": "gateway-api-key",
        "docs": (
            {"title": "OpenAI 兼容 /v1/models 与 /v1/chat/completions（已真实验证）", "url": "https://gonode.si/v1"},
        ),
        "quota": {
            "capability": "none",
            "reason": "该第三方中转站未发布任何余额、配额或账单查询接口文档。Nexus 不猜测私有接口、不抓取网页登录态、不绕过服务商权限校验。",
        },
        "note": "用户授权的第三方 OpenAI 兼容网关。本项目已用真实请求验证其 /v1/models 与 nonce 对话。按量计费，不是官方账户余额。",
    },
    "hyai666-gateway": {
        "name": "hyai666.top 第三方网关",
        "kind": "third-party-gateway",
        "base_url": "https://www.hyai666.top/v1",
        "billing": "metered-api",
        "credential": "gateway-api-key",
        "docs": (
            {"title": "OpenAI 兼容 /v1/models 与 /v1/chat/completions（已真实验证）", "url": "https://www.hyai666.top/v1"},
        ),
        "quota": {
            "capability": "none",
            "reason": "该第三方中转站未发布任何余额、配额或账单查询接口文档。Nexus 不猜测私有接口、不抓取网页登录态、不绕过服务商权限校验。",
        },
        "note": "用户授权的第三方 OpenAI 兼容网关（GLM 路由）。真实响应中出现过按次 credit 字段，属于本次调用计费值，不是账户余额。",
    },
    "zai-official": {
        "name": "Z.ai / GLM 官方 API",
        "kind": "official-api",
        "base_url": "https://api.z.ai/api/paas/v4",
        "billing": "metered-api",
        "credential": "official-api-key",
        "docs": (
            {"title": "Z.AI API Reference（无余额/配额查询接口）", "url": ZAI_API_REFERENCE_URL},
            {"title": "GLM Coding Plan 额度与 credit 计算规则", "url": ZAI_CODING_PLAN_URL},
        ),
        "quota": {
            "capability": "documented-formula",
            "kind": "subscription_allowance",
            "reason": "官方 API Reference 未提供余额/配额查询接口；但官方文档化了 Coding Plan 的 credit 计算公式与套餐额度。",
            "formula": "credits = (input_tokens × input_multiplier + cached_input_tokens × cached_input_multiplier + output_tokens × output_multiplier) / 10000",
            "plans": {
                "lite": {"five_hour_credits": 2000, "weekly_credits": 10000},
                "pro": {"five_hour_credits": 12000, "weekly_credits": 60000},
                "max": {"five_hour_credits": 28000, "weekly_credits": 140000},
            },
            "off_peak_multiplier": 0.5,
            "peak_window": "周一至周五 14:00–18:00（新加坡时间 UTC+8）",
            "source": ZAI_CODING_PLAN_URL,
            "note": "套餐额度必须由用户声明套餐档位；credit 消耗由 Nexus 依据官方公式从真实 token 数据推算，属于推算值，不是官方账单。",
        },
        "note": "官方 GLM API 与官方 Coding Plan 订阅是两种不同计费。本项目当前使用的是第三方网关路由，不是该官方接口。",
    },
    "mimo-official": {
        "name": "MiMo 官方 API",
        "kind": "official-api",
        "base_url": "https://api.xiaomimimo.com/v1",
        "billing": "metered-api",
        "credential": "official-api-key",
        "docs": (
            {"title": "Xiaomi MiMo: 首次调用 API", "url": "https://mimo.mi.com/docs/zh-CN/quick-start/summary/first-api-call"},
            {"title": "Xiaomi MiMo: 模型列表", "url": "https://mimo.mi.com/docs/zh-CN/quick-start/summary/model"},
        ),
        "quota": {
            "capability": "none",
            "reason": "官方文档未提供余额、配额或账单查询接口。Nexus 不猜测私有接口、不抓取登录 Cookie；仅记录服务商真实返回的 Token 用量。",
        },
        "note": "小米 MiMo 官方按量计费 API。真实请求已验证 /v1/models 与 /v1/chat/completions 可用。",
    },
    "dsh-desktop": {
        "name": "DeepSeek Harness 本地运行时",
        "kind": "local-runtime",
        "base_url": None,
        "billing": "metered-api",
        "credential": "operator-credential",
        "docs": ({"title": "DeepSeek Harness 官方仓库", "url": "https://github.com/deepseek-ai/deepseek-harness"},),
        "quota": {"capability": "none", "reason": "本地 headless 运行消耗其自身凭证背后的额度；该运行时未提供用量或余额查询接口。"},
        "note": "官方一次性 headless 运行；需要 Operator 显式授权凭证才可用。",
    },
    "zcode-desktop": {
        "name": "ZCode 桌面客户端",
        "kind": "desktop-client",
        "base_url": None,
        "billing": "subscription",
        "credential": "desktop-login",
        "docs": ({"title": "ZCode 官方模型与套餐说明", "url": "https://www.zcode.network/en/docs/configuration/"},),
        "quota": {"capability": "none", "reason": "桌面客户端未提供自动化用量或余额查询接口；Nexus 不读取桌面登录态。"},
        "note": "桌面 GUI 不作为 CLI 调用；官方独立 CLI 尚未在本机可用。",
    },
    "codex-cli": {
        "name": "Codex 官方 CLI",
        "kind": "official-cli",
        "base_url": None,
        "billing": "subscription",
        "credential": "cli-login",
        "docs": ({"title": "OpenAI Codex 官方 CLI", "url": "https://github.com/openai/codex"},),
        "quota": {"capability": "none", "reason": "CLI 未提供用量或余额查询接口；订阅用量只在账号页面可见。"},
        "note": "按用户指令已停用自动派发；显式单次任务仍可执行。",
    },
    "gemini-api": {
        "name": "Google Gemini 官方 API（独立按量计费）", "kind": "official-api",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/models/", "billing": "metered-api",
        "credential": "explicit-api-key", "docs": ({"title": "Google Gemini API", "url": "https://ai.google.dev/gemini-api/docs"},),
        "quota": {"capability": "none", "reason": "当前未接入文档化余额接口；会员订阅不等于 API 额度，不查询客户端凭证。"},
        "note": "与 Google 会员 CLI 分开授权。只有显式配置 API Key 和费用批准后才可调用；未登记费率时费用不可估算。",
    },
    "gemini-cli": {
        "name": "Gemini 官方 CLI",
        "kind": "official-cli",
        "base_url": None,
        "billing": "subscription",
        "credential": "cli-login",
        "docs": (),
        "quota": {"capability": "none", "reason": "未完成专用官方登录，也未核实用量查询接口。"},
        "note": "等待专用官方登录。",
    },
    "workbuddy-app": {
        "name": "WorkBuddy 人工交接",
        "kind": "manual",
        "base_url": None,
        "billing": "subscription",
        "credential": "none",
        "docs": (),
        "quota": {"capability": "none", "reason": "人工交接没有自动接口，因此也没有可自动采集的用量或余额。"},
        "note": "个人账号无法完成企业第三方应用注册；仅人工交接。",
    },
}

# Models are keyed by "<provider_id>/<model_id>" so the same vendor model served
# by two different providers stays two distinct, separately priced records.
_PRICING_NOTE = ("官方价目表（每 1M tokens，USD）。高峰/非高峰为官方文档化区分；"
                 "中国法定节假日无法在本机可靠判定，估算会标注该局限。")

MODELS: dict[str, dict] = {
    "deepseek-official/deepseek-flash": {
        "model_id": "deepseek-flash",
        "provider_id": "deepseek-official",
        "display_name": "DeepSeek-V4.1-Flash",
        "declared": ("text", "vision", "json", "tools", "thinking"),
        "context_window": 1_000_000,
        "max_output": 384_000,
        "pricing": {
            "currency": "USD", "unit": "per_1m_tokens",
            "input_cache_hit": {"off_peak": 0.003, "peak": 0.006},
            "input_cache_miss": {"off_peak": 0.15, "peak": 0.30},
            "output": {"off_peak": 0.60, "peak": 1.20},
            "revision": "2026-10-15",
            "source": DEEPSEEK_PRICING_URL,
            "note": _PRICING_NOTE,
        },
        "notes": ("官方文档声明支持 Vision；旧名称 deepseek-v4-flash 与 deepseek-v4-flash-vision-exp 已退役，"
                  "请求由本模型承接并按本模型计费。视觉能力仍须真实图像输入测试通过后才在 Nexus 中标记为可用。"),
        "vision_source": DEEPSEEK_VISION_URL,
    },
    "deepseek-official/deepseek-v4-pro": {
        "model_id": "deepseek-v4-pro",
        "provider_id": "deepseek-official",
        "display_name": "DeepSeek-V4-Pro",
        "declared": ("text", "json", "tools", "thinking"),
        "context_window": 1_000_000,
        "max_output": 384_000,
        "pricing": {
            "currency": "USD", "unit": "per_1m_tokens",
            "input_cache_hit": {"off_peak": 0.022, "peak": 0.044},
            "input_cache_miss": {"off_peak": 0.66, "peak": 1.32},
            "output": {"off_peak": 1.98, "peak": 3.96},
            "revision": "2026-10-15",
            "source": DEEPSEEK_PRICING_URL,
            "note": _PRICING_NOTE,
        },
        "notes": "官方文档明确标注不支持 Vision。",
    },
    "gonode-gateway/deepseek-v4.1-flash": {
        "model_id": "deepseek-v4.1-flash",
        "provider_id": "gonode-gateway",
        "display_name": "deepseek-v4.1-flash（网关声明）",
        "declared": ("text",),
        "context_window": None,
        "max_output": None,
        "pricing": {"currency": None, "unit": "per_1m_tokens", "revision": None,
                    "note": "该第三方网关未公布价目表；Nexus 不会用官方价格代替网关价格，费用显示为不可估算。"},
        "notes": "网关 /v1/models 声明的模型 ID。已通过真实 nonce 请求验证可调用。",
    },
    "gonode-gateway/deepseek-v4-pro": {
        "model_id": "deepseek-v4-pro",
        "provider_id": "gonode-gateway",
        "display_name": "deepseek-v4-pro（网关声明）",
        "declared": ("text",),
        "context_window": None,
        "max_output": None,
        "pricing": {"currency": None, "unit": "per_1m_tokens", "revision": None,
                    "note": "该第三方网关未公布价目表；费用显示为不可估算。"},
        "notes": "网关 /v1/models 声明的模型 ID。",
    },
    "hyai666-gateway/glm5.3": {
        "model_id": "glm5.3",
        "provider_id": "hyai666-gateway",
        "display_name": "glm5.3（网关声明）",
        "declared": ("text",),
        "context_window": None,
        "max_output": None,
        "pricing": {"currency": None, "unit": "per_1m_tokens", "revision": None,
                    "note": "该第三方网关未公布价目表；真实响应中出现按次 credit 字段，属于该次调用计费值。"},
        "notes": "网关 /v1/models 声明的唯一模型。已通过真实 nonce 请求验证可调用，响应带 credit 字段。",
    },
    "mimo-official/mimo-v2.6-flash": {
        "model_id": "mimo-v2.6-flash",
        "provider_id": "mimo-official",
        "display_name": "MiMo-V2.6-Flash",
        "declared": ("text", "json", "thinking"),
        "context_window": None,
        "max_output": None,
        "pricing": {"currency": None, "unit": "per_1m_tokens", "revision": None,
                    "note": "官方未公布价目表；费用显示为不可估算，不用其他 Provider 的价格代替。"},
        "notes": "官方 /v1/models 声明的模型。真实 nonce 请求已验证可调用并返回 usage。",
    },
    "mimo-official/mimo-v2.6-pro": {
        "model_id": "mimo-v2.6-pro",
        "provider_id": "mimo-official",
        "display_name": "MiMo-V2.6-Pro",
        "declared": ("text", "json", "thinking"),
        "context_window": None,
        "max_output": None,
        "pricing": {"currency": None, "unit": "per_1m_tokens", "revision": None,
                    "note": "官方未公布价目表；费用显示为不可估算。"},
        "notes": "官方 /v1/models 声明的模型。",
    },
    "zai-official/glm-5.3": {
        "model_id": "glm-5.3",
        "provider_id": "zai-official",
        "display_name": "GLM-5.3",
        "declared": ("text", "vision", "json", "tools", "thinking"),
        "context_window": None,
        "max_output": None,
        "pricing": {"currency": "credits", "unit": "per_10k_tokens",
                    "input_multiplier": 6.9, "cached_input_multiplier": 1.7, "output_multiplier": 24,
                    "revision": "2026-10-15", "source": ZAI_CODING_PLAN_URL,
                    "note": "官方 Coding Plan credit 公式参数；off-peak 按 50% 计。credit 不是货币金额。"},
        "notes": "官方文档化的 credit 倍率。当前项目使用第三方网关路由，不是该官方接口。",
    },
    "zai-official/glm-5.3-flash": {
        "model_id": "glm-5.3-flash",
        "provider_id": "zai-official",
        "display_name": "GLM-5.3-Flash",
        "declared": ("text", "vision", "json", "tools"),
        "context_window": None,
        "max_output": None,
        "pricing": {"currency": "credits", "unit": "per_10k_tokens",
                    "input_multiplier": 2.3, "cached_input_multiplier": 0.56, "output_multiplier": 8,
                    "revision": "2026-10-15", "source": ZAI_CODING_PLAN_URL,
                    "note": "官方 Coding Plan credit 公式参数；含视觉理解的 MCP。"},
        "notes": "官方文档化的 credit 倍率。",
    },
}

# ------------------------------------------------------- agent execution layer

# ``task_execution_scope`` states precisely how far an Agent can carry a task.
# ``structured-files`` means: it produces a validated text-file plan, and Nexus
# alone writes it. There is no shell, no dependency installation, no network
# tool use and no filesystem authority for the model.
RUNTIMES: dict[str, dict] = {
    "deepseek-gateway": {
        "name": "DeepSeek 网关",
        "runtime_kind": "http-adapter",
        "transport": "user-authorized-openai-compatible-gateway",
        "task_execution_scope": "structured-files",
        "execution_note": "生成结构化文件方案；Nexus 校验后写入 Worktree。模型无工具、无 Shell、无文件权限。",
        "default_binding": {"provider_id": "gonode-gateway", "model_key": "gonode-gateway/deepseek-v4.1-flash"},
        "bindings": (
            {"provider_id": "gonode-gateway",
             "models": ("gonode-gateway/deepseek-v4.1-flash", "gonode-gateway/deepseek-v4-pro"),
             "requires": "gateway-api-key", "verified": True},
            {"provider_id": "deepseek-official",
             "models": ("deepseek-official/deepseek-flash", "deepseek-official/deepseek-v4-pro"),
             "requires": "official-api-key", "verified": False,
             "note": "官方接口需要单独的官方账号密钥；当前未配置，因此不可直接切换。"},
        ),
    },
    "zcode-gateway": {
        "name": "ZCode 网关",
        "runtime_kind": "http-adapter",
        "transport": "user-authorized-openai-compatible-gateway",
        "task_execution_scope": "structured-files",
        "execution_note": "生成结构化文件方案；Nexus 校验后写入 Worktree。模型无工具、无 Shell、无文件权限。",
        "default_binding": {"provider_id": "hyai666-gateway", "model_key": "hyai666-gateway/glm5.3"},
        "bindings": (
            {"provider_id": "hyai666-gateway", "models": ("hyai666-gateway/glm5.3",),
             "requires": "gateway-api-key", "verified": True},
            {"provider_id": "zai-official",
             "models": ("zai-official/glm-5.3", "zai-official/glm-5.3-flash"),
             "requires": "official-api-key", "verified": False,
             "note": "官方 GLM 接口需要单独的官方密钥；当前未配置，因此不可直接切换。"},
        ),
    },
    "dsh": {
        "name": "DSH",
        "runtime_kind": "official-headless-cli",
        "transport": "official-cli-headless",
        "task_execution_scope": "structured-files",
        "execution_note": "Nexus 使用受限 headless 文件方案；通用工具关闭，需要 Operator 显式授权 API 凭证与费用。",
        "default_binding": {"provider_id": "dsh-desktop", "model_key": None},
        "bindings": ({"provider_id": "dsh-desktop", "models": (), "requires": "operator-credential", "verified": False},),
    },
    "zcode": {
        "name": "ZCode",
        "runtime_kind": "desktop-client",
        "transport": "desktop-app",
        "task_execution_scope": "none",
        "execution_note": "官方独立 CLI 尚未在本机可用；桌面 GUI 不作为 CLI 调用。",
        "default_binding": {"provider_id": "zcode-desktop", "model_key": None},
        "bindings": ({"provider_id": "zcode-desktop", "models": (), "requires": "desktop-login", "verified": False},),
    },
    "codex": {
        "name": "Codex",
        "runtime_kind": "official-cli",
        "transport": "official-cli-jsonl",
        "task_execution_scope": "structured-files",
        "execution_note": "官方 CLI 结构化文件桥接；已按用户指令停用自动派发。",
        "default_binding": {"provider_id": "codex-cli", "model_key": None},
        "bindings": ({"provider_id": "codex-cli", "models": (), "requires": "cli-login", "verified": False},),
    },
    "gemini": {
        "name": "Gemini",
        "runtime_kind": "official-cli",
        "transport": "google-member-cli",
        "task_execution_scope": "structured-files",
        "execution_note": "官方 Google 会员 CLI；等待专用官方登录。",
        "default_binding": {"provider_id": "gemini-cli", "model_key": None},
        "bindings": ({"provider_id": "gemini-cli", "models": (), "requires": "cli-login", "verified": False}, {"provider_id": "gemini-api", "models": (), "requires": "api-key-and-cost-approval", "verified": False}),
    },
    "mimo": {
        "name": "MIMO",
        "runtime_kind": "http-adapter",
        "transport": "official-rest",
        "task_execution_scope": "structured-files",
        "execution_note": "官方 REST 文件方案；Nexus 校验后写入 Worktree。模型无工具、无 Shell、无文件权限。",
        "default_binding": {"provider_id": "mimo-official", "model_key": "mimo-official/mimo-v2.6-flash"},
        "bindings": (
            {"provider_id": "mimo-official",
             "models": ("mimo-official/mimo-v2.6-flash", "mimo-official/mimo-v2.6-pro"),
             "requires": "official-api-key", "verified": True,
             "note": "官方 /v1/models 与 /v1/chat/completions 已用真实请求验证，实测返回 usage。"},
        ),
    },
    "workbuddy": {
        "name": "WorkBuddy",
        "runtime_kind": "human-handoff",
        "transport": "manual-handoff",
        "task_execution_scope": "manual",
        "execution_note": "无自动接口；由人工在第三方界面执行并导入结果。",
        "default_binding": {"provider_id": "workbuddy-app", "model_key": None},
        "bindings": ({"provider_id": "workbuddy-app", "models": (), "requires": "none", "verified": False},),
    },
}


# ---------------------------------------------------------------- operations

class BindingRejected(ValueError):
    """The requested Agent/model/provider combination is not permitted."""


def provider(provider_id: str) -> dict:
    if provider_id not in PROVIDERS:
        raise BindingRejected(f"未知 Provider：{provider_id}")
    return PROVIDERS[provider_id]


def model(model_key: str) -> dict:
    if model_key not in MODELS:
        raise BindingRejected(f"未知模型：{model_key}")
    return MODELS[model_key]


def runtime(agent_id: str) -> dict:
    if agent_id not in RUNTIMES:
        raise BindingRejected(f"未知 Agent：{agent_id}")
    return RUNTIMES[agent_id]


def allowed_bindings(agent_id: str) -> tuple[dict, ...]:
    """Every (provider, models) pair this Agent is permitted to be bound to.

    Permission here means "the runtime has an implementation for this provider",
    not "it is configured". Whether a route is usable is a separate, verifiable
    fact reported by the adapter.
    """
    return tuple(runtime(agent_id)["bindings"])


def resolve_binding(agent_id: str, provider_id: str | None, model_key: str | None) -> dict:
    """Validate a requested Agent/model/provider combination.

    ``None`` for both selects the Agent's default binding, so the console can
    always show a concrete provider and model instead of an empty cell.
    """
    info = runtime(agent_id)
    if provider_id is None and model_key is None:
        return dict(info["default_binding"], verified=any(
            b["provider_id"] == info["default_binding"]["provider_id"] for b in info["bindings"]))
    bindings = {b["provider_id"]: b for b in info["bindings"]}
    if provider_id not in bindings:
        raise BindingRejected(f"{agent_id} 未声明 Provider {provider_id} 的接入实现")
    entry = bindings[provider_id]
    if model_key is None:
        model_key = entry["models"][0] if entry["models"] else None
    if model_key is not None:
        if model_key not in entry["models"]:
            raise BindingRejected(f"Provider {provider_id} 不为 {agent_id} 提供模型 {model_key}")
        if model(model_key)["provider_id"] != provider_id:
            raise BindingRejected("模型与 Provider 记录不一致")
    return {"provider_id": provider_id, "model_key": model_key, "verified": entry.get("verified", False)}


def model_public(model_key: str) -> dict:
    """Model record without internal-only fields; safe for the local console."""
    item = dict(model(model_key))
    pricing = dict(item["pricing"])
    pricing["estimable"] = bool(pricing.get("currency") in ("USD",) or pricing.get("input_multiplier"))
    item["pricing"] = pricing
    item["key"] = model_key
    item["provider_name"] = PROVIDERS[item["provider_id"]]["name"]
    return item


# ------------------------------------------------------- logical Agent grouping

# The console shows one card per *logical Agent*. A third-party API route is an
# execution path of the Agent that already owns that vendor's work, not a
# separate Agent product: showing "DeepSeek 网关" next to "DeepSeek" as two
# peers duplicated the same vendor in the network and inflated the Agent count.
#
# The runtime records above are kept exactly as they are — the gateway adapters,
# their configuration forms, their usage rows and their history all still work.
# What changes is *presentation and accounting*: each runtime declares which
# logical Agent it belongs to.
LOGICAL_AGENTS: dict[str, dict] = {
    "deepseek": {
        "name": "DeepSeek",
        "description": (
            "DeepSeek 主力开发 Agent。本地执行器为 DeepSeek Harness（官方 headless 运行时，"
            "仅生成受限文件方案，需要 Operator 授权凭证）；第三方 API 路线为用户授权的 OpenAI 兼容网关"
            "（deepseek-v4.1-flash），只产出经 Nexus 校验的结构化文件方案。"
        ),
        "primary_runtime": "dsh",
        "runtimes": ("dsh", "deepseek-gateway"),
        "preferred_route": "deepseek-gateway",
        "preferred_route_reason": "用户明确配置的第三方 API 路线优先；本地 headless 路线需要 Operator 凭证。",
    },
    "zcode": {
        "name": "ZCode",
        "description": (
            "第二开发 Agent（ZCode / GLM）。本地执行器为 ZCode 官方桌面客户端（独立 CLI 在本机尚不可用）；"
            "第三方 API 路线为用户授权的 OpenAI 兼容网关（glm5.3），只产出经 Nexus 校验的结构化文件方案。"
        ),
        "primary_runtime": "zcode",
        "runtimes": ("zcode", "zcode-gateway"),
        "preferred_route": "zcode-gateway",
        "preferred_route_reason": "用户明确配置的第三方 API 路线优先；官方桌面客户端不作为 CLI 调用。",
    },
    "codex": {
        "name": "Codex",
        "description": "官方 Codex CLI。按用户指令已停用自动派发；显式单次任务仍可执行。",
        "primary_runtime": "codex",
        "runtimes": ("codex",),
        "preferred_route": "codex",
        "preferred_route_reason": None,
    },
    "gemini": {
        "name": "Gemini",
        "description": "官方 Google 会员 CLI；当前账号收到客户端不受支持提示，等待 Google 官方支持方案。",
        "primary_runtime": "gemini",
        "runtimes": ("gemini",),
        "preferred_route": "gemini",
        "preferred_route_reason": None,
    },
    "mimo": {
        "name": "MIMO",
        "description": "MiMo 官方 REST 文件桥接；连接与用量以真实请求结果为准。",
        "primary_runtime": "mimo",
        "runtimes": ("mimo",),
        "preferred_route": "mimo",
        "preferred_route_reason": None,
    },
    "workbuddy": {
        "name": "WorkBuddy",
        "description": "无自动接口；由人工在第三方界面执行并导入结果。",
        "primary_runtime": "workbuddy",
        "runtimes": ("workbuddy",),
        "preferred_route": "workbuddy",
        "preferred_route_reason": None,
    },
}

# Runtime id -> logical Agent id. Every runtime must appear exactly once, so the
# Agent count can never double-count a vendor.
RUNTIME_OWNER: dict[str, str] = {
    runtime_id: logical_id
    for logical_id, info in LOGICAL_AGENTS.items()
    for runtime_id in info["runtimes"]
}
_ORPHANS = set(RUNTIMES) - set(RUNTIME_OWNER)
if _ORPHANS:
    raise RuntimeError(f"以下执行运行时未归属任何逻辑 Agent：{sorted(_ORPHANS)}")


def logical_agent(runtime_id: str) -> str:
    """Which logical Agent a runtime belongs to (defaults to itself)."""
    return RUNTIME_OWNER.get(runtime_id, runtime_id)


def runtimes_of(logical_id: str) -> tuple[str, ...]:
    return tuple(LOGICAL_AGENTS.get(logical_id, {}).get("runtimes", (logical_id,)))


def logical_agents() -> list[dict]:
    """One entry per logical Agent, with its execution routes attached."""
    result = []
    for logical_id, info in LOGICAL_AGENTS.items():
        routes = []
        for runtime_id in info["runtimes"]:
            runtime = RUNTIMES[runtime_id]
            providers = [
                {
                    "provider_id": binding["provider_id"],
                    "provider_name": PROVIDERS[binding["provider_id"]]["name"],
                    "provider_kind": PROVIDERS[binding["provider_id"]]["kind"],
                    "credential": PROVIDERS[binding["provider_id"]]["credential"],
                    "requires": binding["requires"],
                    "verified": binding.get("verified", False),
                    "note": binding.get("note"),
                    "models": [model_public(key) for key in binding["models"]],
                }
                for binding in runtime["bindings"]]
            routes.append({
                "runtime_id": runtime_id,
                "name": runtime["name"],
                "runtime_kind": runtime["runtime_kind"],
                "transport": runtime["transport"],
                "task_execution_scope": runtime["task_execution_scope"],
                "execution_note": runtime["execution_note"],
                "is_third_party_api": runtime["runtime_kind"] == "http-adapter",
                "providers": providers,
            })
        result.append({
            "id": logical_id,
            "name": info["name"],
            "description": info["description"],
            "primary_runtime": info["primary_runtime"],
            "preferred_route": info["preferred_route"],
            "preferred_route_reason": info["preferred_route_reason"],
            "routes": routes,
            "route_count": len(routes),
        })
    return result


def provider_public(provider_id: str) -> dict:
    item = dict(provider(provider_id))
    item["id"] = provider_id
    return item


def agents_public() -> list[dict]:
    return [dict(info, id=agent_id) for agent_id, info in RUNTIMES.items()]


def snapshot() -> dict:
    """Full catalog for the console: providers, models and Agent bindings."""
    return {
        "providers": [provider_public(pid) for pid in PROVIDERS],
        "models": [model_public(key) for key in MODELS],
        "agents": agents_public(),
        # One card per logical Agent; each carries its execution routes.
        "logical_agents": logical_agents(),
        "runtime_owners": dict(RUNTIME_OWNER),
        "relations": [
            {"agent_id": logical_id,
             "runtime_id": runtime_id,
             "provider_id": RUNTIMES[runtime_id]["default_binding"]["provider_id"],
             "model_key": RUNTIMES[runtime_id]["default_binding"]["model_key"]}
            for logical_id, info in LOGICAL_AGENTS.items()
            for runtime_id in info["runtimes"]
        ],
        "layers": ["agent", "runtime", "model", "provider"],
        "note": ("Agent → 执行运行时 → Model → Provider。一个 Agent 可以有多条执行路径"
                 "（本地 CLI、第三方 API 等），但界面只显示一张主卡片；Provider 与网关不占用"
                 "额外的 Agent 名额。「模型 API 可调用」与「Agent 具备完整任务执行能力」是两个"
                 "独立事实，界面分别标注。"),
    }
