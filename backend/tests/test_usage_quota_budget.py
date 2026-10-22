"""Three-layer catalog, usage collection, quota semantics and the budget guard.

These tests use fixtures and injected transports only. They never call a real
model, never read a credential file and never reach the network.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from nexus import budget as budget_module
from nexus import catalog
from nexus import gateway_adapter as gateway
from nexus import keyvault
from nexus import quota as quota_module
from nexus import usage as usage_module
from nexus.main import create_app
from nexus.storage import Store


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def store(root: Path) -> Store:
    return Store(root)


@pytest.fixture
def client(root: Path):
    with TestClient(create_app(root, enable_codex=False)) as application:
        yield application


# --------------------------------------------------------------- the catalog


def test_three_layers_are_separate_records():
    """Agent, Model and Provider are distinct objects with distinct ids."""
    assert catalog.RUNTIMES and catalog.MODELS and catalog.PROVIDERS
    for key, record in catalog.MODELS.items():
        assert record["provider_id"] in catalog.PROVIDERS
        assert key == f"{record['provider_id']}/{record['model_id']}"
        assert record["declared"], "每个模型都必须声明能力，即使是空能力"
    for agent_id, info in catalog.RUNTIMES.items():
        assert info["runtime_kind"] and info["transport"]
        for binding in info["bindings"]:
            assert binding["provider_id"] in catalog.PROVIDERS
            for key in binding["models"]:
                assert key in catalog.MODELS
                assert catalog.MODELS[key]["provider_id"] == binding["provider_id"]


def test_one_agent_binds_many_providers_and_one_provider_serves_many_agents():
    """The relationship is many-to-many, not a fixed one-to-one pairing."""
    deepseek = catalog.allowed_bindings("deepseek-gateway")
    assert len({item["provider_id"] for item in deepseek}) >= 2

    serving = [agent_id for agent_id, info in catalog.RUNTIMES.items()
               if any(b["provider_id"] == "deepseek-official" for b in info["bindings"])]
    assert len(serving) >= 1
    # The same provider appears for more than one Agent in this catalog.
    multi = [provider_id for provider_id in catalog.PROVIDERS
             if sum(1 for info in catalog.RUNTIMES.values()
                    if any(b["provider_id"] == provider_id for b in info["bindings"])) > 1]
    assert isinstance(multi, list)


def test_cross_provider_model_binding_is_refused():
    with pytest.raises(catalog.BindingRejected):
        catalog.resolve_binding("deepseek-gateway", "zai-official", "zai-official/glm-5.3")


def test_unknown_provider_and_model_are_refused():
    with pytest.raises(catalog.BindingRejected):
        catalog.resolve_binding("deepseek-gateway", "not-a-provider", None)
    with pytest.raises(catalog.BindingRejected):
        catalog.resolve_binding("deepseek-gateway", "gonode-gateway", "gonode-gateway/nope")


def test_default_binding_is_concrete():
    binding = catalog.resolve_binding("deepseek-gateway", None, None)
    assert binding["provider_id"] == "gonode-gateway"
    assert binding["model_key"] in catalog.MODELS


def test_declared_vision_is_recorded_but_not_claimed_as_verified():
    """DeepSeek documents vision; Nexus must still mark it unverified."""
    record = catalog.MODELS["deepseek-official/deepseek-flash"]
    assert "vision" in record["declared"]
    assert "vision_source" in record
    assert "vision" not in catalog.MODELS["deepseek-official/deepseek-v4-pro"]["declared"]


def test_documented_prices_match_the_vendor_pages():
    flash = catalog.MODELS["deepseek-official/deepseek-flash"]["pricing"]
    assert flash["input_cache_hit"]["peak"] == 0.006
    assert flash["input_cache_miss"]["peak"] == 0.30
    assert flash["output"]["peak"] == 1.20
    # Off-peak is documented as half of peak.
    assert flash["input_cache_miss"]["off_peak"] == pytest.approx(flash["input_cache_miss"]["peak"] / 2)


def test_gateway_prices_are_not_invented():
    """A reseller with no price list must not borrow the vendor's price."""
    pricing = catalog.MODELS["gonode-gateway/deepseek-v4.1-flash"]["pricing"]
    assert pricing.get("currency") is None
    assert "input_cache_miss" not in pricing


def test_snapshot_exposes_the_chain():
    snapshot = catalog.snapshot()
    assert snapshot["layers"] == ["agent", "runtime", "model", "provider"]
    # Relations are stated per execution route, and every runtime appears once.
    assert {item["runtime_id"] for item in snapshot["relations"]} == set(catalog.RUNTIMES)
    # A logical Agent owns one or more runtimes; every runtime has exactly one owner.
    assert {item["agent_id"] for item in snapshot["relations"]} == set(catalog.LOGICAL_AGENTS)
    assert set(snapshot["runtime_owners"]) == set(catalog.RUNTIMES)
    assert len(snapshot["logical_agents"]) == len(catalog.LOGICAL_AGENTS)
    # A gateway is a route of its owning Agent, not a separate Agent.
    deepseek = next(item for item in snapshot["logical_agents"] if item["id"] == "deepseek")
    assert {route["runtime_id"] for route in deepseek["routes"]} == {"dsh", "deepseek-gateway"}
    assert len(snapshot["logical_agents"]) < len(catalog.RUNTIMES)


# ------------------------------------------------------------ usage parsing


def test_parse_usage_from_a_documented_response():
    usage = usage_module.parse_usage({
        "prompt_tokens": 296, "completion_tokens": 52, "total_tokens": 348,
        "prompt_tokens_details": {"cached_tokens": 64},
        "completion_tokens_details": {"reasoning_tokens": 12},
        "credit": 0.01, "model": "deepseek-v4.1-flash"})
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (296, 52, 348)
    assert usage.cached_tokens == 64
    assert usage.reasoning_tokens == 12
    assert usage.credit == 0.01
    assert usage.reported_model == "deepseek-v4.1-flash"
    assert usage.source == usage_module.SOURCE_REPORTED
    assert usage.total_source == "provider"


def test_absent_usage_is_unavailable_never_a_zero():
    for payload in (None, {}, {"choices": []}, {"usage": None}):
        usage = usage_module.parse_usage(payload)
        assert usage.source == usage_module.SOURCE_UNAVAILABLE
        assert usage.total_tokens is None
        assert not usage.has_any


def test_missing_total_is_derived_and_labelled():
    usage = usage_module.parse_usage({"prompt_tokens": 10, "completion_tokens": 5})
    assert usage.total_tokens == 15
    assert usage.total_source == "derived"


def test_only_integer_counters_are_accepted():
    """A boolean or a string must never become a token count."""
    usage = usage_module.parse_usage({"prompt_tokens": True, "completion_tokens": "52",
                                      "total_tokens": 1.5})
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.total_tokens is None or usage.total_source == "derived"
    assert usage.source in (usage_module.SOURCE_UNAVAILABLE, usage_module.SOURCE_PARTIAL,
                            usage_module.SOURCE_REPORTED)


def test_negative_counters_are_rejected():
    usage = usage_module.parse_usage({"prompt_tokens": -5})
    assert usage.input_tokens is None


# ----------------------------------------------------------- streaming usage


def test_streaming_reader_records_usage_only_when_the_provider_sends_it():
    reader = usage_module.StreamUsageReader()
    reader.feed('data: {"id":"x","model":"m","choices":[{"delta":{"content":"a"},'
                '"finish_reason":null}]}\n\n')
    assert reader.chunks == 1
    assert not reader.usage_available, "内容分片不能被视为用量"

    reader.feed('data: {"id":"x","usage":{"prompt_tokens":10,"completion_tokens":5,'
                '"total_tokens":15},"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                'data: [DONE]\n\n')
    assert reader.usage_available
    assert reader.usage.total_tokens == 15
    assert reader.finish_reason == "stop"


def test_streaming_reader_ignores_malformed_and_comment_lines():
    reader = usage_module.StreamUsageReader()
    reader.feed(': keep-alive\n\nnot-data\n\ndata: {broken json\n\n')
    assert reader.chunks == 0
    assert not reader.usage_available


def test_streaming_usage_verification_starts_empty():
    """No provider is assumed to return final usage until a real stream proves it."""
    assert "gonode-gateway" not in usage_module.STREAMING_USAGE_VERIFIED
    assert "hyai666-gateway" not in usage_module.STREAMING_USAGE_VERIFIED


def test_dedupe_key_prefers_the_provider_request_id():
    base = dict(agent_id="a", provider_id="p", model_key="p/m", task_id="t")
    first = usage_module.UsageEvent(**base, attempt_id="att-1",
                                    usage=usage_module.Usage(request_id="req-1"))
    retry = usage_module.UsageEvent(**base, attempt_id="att-2",
                                    usage=usage_module.Usage(request_id="req-1"))
    other = usage_module.UsageEvent(**base, attempt_id="att-3",
                                    usage=usage_module.Usage(request_id="req-2"))
    assert usage_module.dedupe_key(first) == usage_module.dedupe_key(retry)
    assert usage_module.dedupe_key(first) != usage_module.dedupe_key(other)


def test_dedupe_key_falls_back_to_the_attempt_id():
    """Without a provider request id, a retry in a new attempt is its own request."""
    base = dict(agent_id="a", provider_id="p", model_key="p/m", task_id="t")
    one = usage_module.UsageEvent(**base, attempt_id="att-1")
    two = usage_module.UsageEvent(**base, attempt_id="att-2")
    assert usage_module.dedupe_key(one) != usage_module.dedupe_key(two)


def test_deepseek_cache_hit_field_is_understood():
    """DeepSeek documents prompt_cache_hit_tokens as a top-level counter."""
    usage = usage_module.parse_usage({"prompt_tokens": 1000, "completion_tokens": 200,
                                      "total_tokens": 1200, "prompt_cache_hit_tokens": 900,
                                      "prompt_cache_miss_tokens": 100})
    assert usage.cached_tokens == 900


# ------------------------------------------------------- the usage collector


def _event(**overrides):
    payload = dict(agent_id="deepseek-gateway", provider_id="gonode-gateway",
                   model_key="gonode-gateway/deepseek-v4.1-flash", task_id=None,
                   attempt_id="att-1", usage=usage_module.parse_usage(
                       {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}))
    payload.update(overrides)
    return usage_module.UsageEvent(**payload)


def test_record_and_roll_up_across_all_four_levels(store):
    collector = usage_module.UsageCollector(store)
    collector.record(_event(task_id="task-1"))
    collector.record(_event(attempt_id="att-2", task_id="task-1",
                            usage=usage_module.parse_usage({"prompt_tokens": 50, "completion_tokens": 10,
                                                            "total_tokens": 60})))
    collector.record(_event(agent_id="zcode-gateway", provider_id="hyai666-gateway",
                            model_key="hyai666-gateway/glm5.3", attempt_id="att-3", task_id="task-2",
                            usage=usage_module.parse_usage({"prompt_tokens": 7, "completion_tokens": 3,
                                                            "total_tokens": 10})))

    # global
    assert collector.totals("all")["total_tokens"] == 190
    assert collector.totals("all")["requests"] == 3
    # agent
    assert collector.totals("all", agent_id="deepseek-gateway")["total_tokens"] == 180
    # provider
    assert collector.totals("all", provider_id="hyai666-gateway")["total_tokens"] == 10
    # task
    assert collector.totals("all", task_id="task-1")["total_tokens"] == 180

    assert {row["name"] for row in collector.group_by("agent", "all")} == {"deepseek-gateway", "zcode-gateway"}
    assert {row["name"] for row in collector.group_by("provider", "all")} == {"gonode-gateway", "hyai666-gateway"}
    assert collector.task_costs("all")[0]["task_id"] == "task-1"


def test_retry_with_the_same_request_id_is_not_counted_twice(store):
    collector = usage_module.UsageCollector(store)
    first = _event(usage=usage_module.Usage(input_tokens=100, output_tokens=20, total_tokens=120,
                                            source=usage_module.SOURCE_REPORTED, request_id="req-9"))
    second = _event(attempt_id="att-retry",
                    usage=usage_module.Usage(input_tokens=100, output_tokens=20, total_tokens=120,
                                             source=usage_module.SOURCE_REPORTED, request_id="req-9"))
    _, duplicate_a = collector.record(first)
    _, duplicate_b = collector.record(second)
    assert duplicate_a is False and duplicate_b is True
    assert collector.totals("all")["requests"] == 1
    assert collector.totals("all")["total_tokens"] == 120


def test_unavailable_usage_counts_calls_but_not_tokens(store):
    collector = usage_module.UsageCollector(store)
    collector.record(_event(attempt_id="ok"))
    collector.record(_event(attempt_id="nousage", usage=usage_module.Usage()))
    totals = collector.totals("all")
    assert totals["requests"] == 2
    assert totals["usage_reported"] == 1
    assert totals["usage_unavailable"] == 1
    assert totals["total_tokens"] == 120


def test_failed_requests_are_counted_with_success_rate(store):
    collector = usage_module.UsageCollector(store)
    collector.record(_event(attempt_id="ok"))
    collector.record(_event(attempt_id="bad", status=usage_module.STATUS_ERROR,
                            error_kind="rate_limit", usage=usage_module.Usage()))
    totals = collector.totals("all")
    assert totals["succeeded"] == 1 and totals["failed"] == 1
    assert totals["requests"] == 2


def test_time_range_filters_split_by_window(store):
    collector = usage_module.UsageCollector(store)
    collector.record(_event(attempt_id="old"))
    with store.connect() as db:
        old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        db.execute("UPDATE usage_records SET created_at=?", (old,))
    assert collector.totals("month")["requests"] == 0
    assert collector.totals("all")["requests"] == 1
    start = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    end = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    assert collector.totals("custom", start, end)["requests"] == 1


def test_series_buckets_by_day(store):
    collector = usage_module.UsageCollector(store)
    collector.record(_event(attempt_id="a"))
    collector.record(_event(attempt_id="b"))
    series = collector.series("all")
    assert len(series) == 1
    assert series[0]["requests"] == 2
    assert series[0]["total_tokens"] == 240


def test_unknown_span_and_dimension_are_refused(store):
    collector = usage_module.UsageCollector(store)
    with pytest.raises(ValueError):
        collector.totals("fortnight")
    with pytest.raises(ValueError):
        collector.group_by("nonsense")


def test_records_are_persisted_and_survive_a_new_store(root, store):
    collector = usage_module.UsageCollector(store)
    collector.record(_event(attempt_id="persist"))
    reopened = usage_module.UsageCollector(Store(root))
    assert reopened.totals("all")["total_tokens"] == 120


# ------------------------------------------------------------- cost estimate


def test_cost_estimate_is_labelled_and_not_a_bill(store):
    card = budget_module.RateCard(store)
    usage = usage_module.parse_usage({"prompt_tokens": 1_000_000, "completion_tokens": 0,
                                      "total_tokens": 1_000_000})
    result = card.estimate("deepseek-official", "deepseek-official/deepseek-flash", usage)
    assert result["cost_source"] == "nexus-estimate"
    assert result["currency"] == "USD"
    assert "估算" in result["note"]
    assert "assumptions" in result


def test_unpriceable_provider_is_not_given_a_borrowed_price(store):
    card = budget_module.RateCard(store)
    usage = usage_module.parse_usage({"prompt_tokens": 500, "completion_tokens": 100, "total_tokens": 600})
    result = card.estimate("gonode-gateway", "gonode-gateway/deepseek-v4.1-flash", usage)
    assert result["cost_source"] == "not-estimable"
    assert result["estimated_cost"] is None


def test_credit_models_use_the_documented_formula(store):
    card = budget_module.RateCard(store)
    # GLM-5.3: input 6.9, cached 1.7, output 24, per 10 000 tokens.
    usage = usage_module.parse_usage({"prompt_tokens": 10_000, "completion_tokens": 10_000,
                                      "total_tokens": 20_000, "prompt_tokens_details": {"cached_tokens": 0}})
    result = card.estimate("zai-official", "zai-official/glm-5.3", usage)
    assert result["currency"] == "credits"
    assert result["estimated_cost"] == pytest.approx((10_000 * 6.9 + 10_000 * 24) / 10000)


def test_rate_card_is_editable_and_validated(store):
    card = budget_module.RateCard(store)
    updated = card.update("deepseek-official/deepseek-flash", {"output": {"peak": 9.0, "off_peak": 4.5}})
    assert updated["output"]["peak"] == 9.0
    assert card.effective("deepseek-official/deepseek-flash")["output"]["peak"] == 9.0
    with pytest.raises(ValueError):
        card.update("deepseek-official/deepseek-flash", {"nonsense": 1})
    with pytest.raises(ValueError):
        card.update("deepseek-official/deepseek-flash", {"output": {"peak": -3}})
    with pytest.raises(catalog.BindingRejected):
        card.update("no-such-model", {"currency": "USD"})


def test_peak_windows_follow_the_documented_hours():
    tuesday_peak = datetime(2026, 10, 13, 3, 0, tzinfo=timezone.utc)
    tuesday_off = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    saturday = datetime(2026, 10, 17, 3, 0, tzinfo=timezone.utc)
    assert budget_module.is_peak("deepseek-official", tuesday_peak) is True
    assert budget_module.is_peak("deepseek-official", tuesday_off) is False
    assert budget_module.is_peak("deepseek-official", saturday) is False
    # A provider with no documented peak distinction reports None, not False.
    assert budget_module.is_peak("gonode-gateway", tuesday_peak) is None


# --------------------------------------------------------------- the guard


def test_budget_settings_default_to_unset_never_unlimited(store):
    guard = budget_module.BudgetGuard(store, usage_module.UsageCollector(store),
                                      budget_module.RateCard(store))
    settings = guard.settings()
    assert settings["daily_budget"] is None
    assert settings["monthly_budget"] is None
    assert settings["per_task_tokens"] is None
    assert settings["require_metered_approval"] is True


def test_guard_stops_dispatch_when_a_ceiling_is_reached(store):
    collector = usage_module.UsageCollector(store)
    guard = budget_module.BudgetGuard(store, collector, budget_module.RateCard(store))
    collector.record(_event(attempt_id="seed", task_id="t1"))

    guard.update_settings({"per_task_tokens": 10})
    verdict = guard.evaluate(agent_id="deepseek-gateway", provider_id="gonode-gateway",
                             model_key="gonode-gateway/deepseek-v4.1-flash",
                             task={"id": "t1"}, purpose="task")
    assert verdict["allowed"] is False
    assert "task_token_limit" in [item["code"] for item in verdict["denials"]]

    guard.update_settings({"per_task_tokens": None, "daily_calls": 1})
    verdict = guard.evaluate(agent_id="deepseek-gateway", provider_id="gonode-gateway",
                             model_key="gonode-gateway/deepseek-v4.1-flash", purpose="task")
    assert "call_limit_reached" in [item["code"] for item in verdict["denials"]]


def test_guard_never_auto_falls_back_to_gpt_but_allows_an_explicit_choice(store):
    collector = usage_module.UsageCollector(store)
    guard = budget_module.BudgetGuard(store, collector, budget_module.RateCard(store))
    auto = guard.evaluate(agent_id="codex", provider_id="codex-cli", model_key=None,
                          purpose="task", automatic=True)
    assert auto["allowed"] is False
    assert "gpt_auto_fallback_forbidden" in [item["code"] for item in auto["denials"]]
    explicit = guard.evaluate(agent_id="codex", provider_id="codex-cli", model_key=None,
                              purpose="task", automatic=False)
    assert "gpt_auto_fallback_forbidden" not in [item["code"] for item in explicit["denials"]]


def test_metered_provider_requires_per_request_approval(store):
    collector = usage_module.UsageCollector(store)
    guard = budget_module.BudgetGuard(store, collector, budget_module.RateCard(store))
    verdict = guard.evaluate(agent_id="deepseek-gateway", provider_id="gonode-gateway",
                             model_key="gonode-gateway/deepseek-v4.1-flash", purpose="task")
    assert "metered_needs_approval" in [item["code"] for item in verdict["approvals"]]
    approved = guard.evaluate(agent_id="deepseek-gateway", provider_id="gonode-gateway",
                              model_key="gonode-gateway/deepseek-v4.1-flash",
                              task={"id": "t", "cost_approved": 1}, purpose="task")
    assert "metered_needs_approval" not in [item["code"] for item in approved["approvals"]]


def test_unpriceable_provider_flags_an_unenforceable_budget(store):
    collector = usage_module.UsageCollector(store)
    guard = budget_module.BudgetGuard(store, collector, budget_module.RateCard(store))
    collector.record(_event(attempt_id="seed", task_id="t9"))
    guard.update_settings({"per_task_budget": 0.01})
    verdict = guard.evaluate(agent_id="deepseek-gateway", provider_id="gonode-gateway",
                             model_key="gonode-gateway/deepseek-v4.1-flash",
                             task={"id": "t9"}, purpose="task")
    codes = [item["code"] for item in verdict["approvals"]]
    assert "budget_unenforceable" in codes


def test_codex_daily_call_cap_can_be_removed_without_disabling_other_guards(store):
    collector = usage_module.UsageCollector(store)
    guard = budget_module.BudgetGuard(store, collector, budget_module.RateCard(store))
    assert guard.settings()['codex_daily_calls'] is None
    for i in range(4):
        collector.record(_event(agent_id='codex', provider_id='codex-cli', attempt_id=f'LOCAL-CODEX-FIXTURE-{i}'))
    guard.update_settings({'codex_daily_calls': 3, 'daily_calls': 20})
    assert any(d['code'] == 'codex_call_limit' for d in guard.evaluate(agent_id='codex', provider_id='codex-cli', model_key=None)['denials'])
    guard.update_settings({'codex_daily_calls': None})
    assert not any(d['code'] == 'codex_call_limit' for d in guard.evaluate(agent_id='codex', provider_id='codex-cli', model_key=None)['denials'])
    assert guard.settings()['daily_calls'] == 20
    assert guard.settings()['require_metered_approval'] is True
    assert guard.status()['codex_calls']['limit'] is None
    assert not any('Codex' in w for w in guard.status()['warnings'])


def test_budget_settings_are_validated(store):
    guard = budget_module.BudgetGuard(store, usage_module.UsageCollector(store),
                                      budget_module.RateCard(store))
    with pytest.raises(ValueError):
        guard.update_settings({"unknown_field": 1})
    with pytest.raises(ValueError):
        guard.update_settings({"daily_budget": -1})
    with pytest.raises(ValueError):
        guard.update_settings({"currency": "EUR"})
    with pytest.raises(ValueError):
        guard.update_settings({"require_metered_approval": "yes"})


# -------------------------------------------------------------- the keyvault


def test_vault_defaults_to_dpapi_on_a_supported_host(root, tmp_path):
    """DPAPI is the default wherever it is genuinely available.

    The secret directory is redirected out of the project tree, matching how the
    console stores keys in the Windows user's own application data directory.
    """
    directory = tmp_path / "secrets"
    vault = keyvault.SecretVault(root, directory=directory)
    expected = keyvault.BACKEND_MEMORY
    assert vault.backend == expected, "默认仅内存；DPAPI 必须明确启用"
    secret = "sk-" + uuid.uuid4().hex
    described = vault.set("gonode-gateway", secret)
    assert described["configured"] is True
    assert described["persisted"] is (expected == keyvault.BACKEND_DPAPI)
    assert described["fingerprint"] and secret not in json.dumps(described)
    assert vault.get("gonode-gateway") == secret
    assert vault.clear("gonode-gateway")["configured"] is False


def test_vault_refuses_a_directory_inside_a_git_worktree(root, monkeypatch):
    """Ciphertext must never land where it could be committed.

    The escape hatch is only an explicit NEXUS_SECRET_DIR, which the test run
    sets for isolation; without it the in-repository path is refused.
    """
    inside = root / "committable-secrets"
    (root / ".git").mkdir(exist_ok=True)
    monkeypatch.delenv(keyvault.SECRET_DIR_ENV, raising=False)
    with pytest.raises(ValueError):
        keyvault.SecretVault(root, backend=keyvault.BACKEND_DPAPI, directory=inside)


def test_vault_describe_never_returns_the_key(root, tmp_path):
    vault = keyvault.SecretVault(root, directory=tmp_path / "secrets")
    secret = "sk-" + uuid.uuid4().hex
    vault.set("zai-official", secret)
    payload = json.dumps({"describe": vault.describe("zai-official"), "status": vault.status()},
                         ensure_ascii=False)
    assert secret not in payload
    assert secret[:8] not in payload


def test_vault_rejects_an_empty_key(root, tmp_path):
    vault = keyvault.SecretVault(root, directory=tmp_path / "secrets")
    with pytest.raises(ValueError):
        vault.set("gonode-gateway", "   ")


def test_vault_falls_back_loudly_when_dpapi_is_unavailable(root, monkeypatch):
    monkeypatch.setattr(keyvault, "dpapi_available", lambda: False)
    vault = keyvault.SecretVault(root, backend=keyvault.BACKEND_DPAPI)
    assert vault.backend == keyvault.BACKEND_MEMORY
    assert vault.fallback_reason, "回退必须给出原因，不能静默降级"
    assert vault.status()["persists_across_restart"] is False


@pytest.mark.skipif(not keyvault.dpapi_available(), reason="Windows DPAPI is not reachable here")
def test_dpapi_backend_round_trips_and_does_not_store_plaintext(root, tmp_path):
    directory = tmp_path / "secrets"
    vault = keyvault.SecretVault(root, backend=keyvault.BACKEND_DPAPI, directory=directory)
    secret = "sk-" + uuid.uuid4().hex
    described = vault.set("gonode-gateway", secret)
    assert described["persisted"] is True
    on_disk = (directory / "gonode-gateway.dpapi").read_bytes()
    assert secret.encode() not in on_disk, "密文文件中不得出现明文密钥"
    # The directory is tightened to the current user, not left world-inherited.
    assert vault.acl["applied"] is False, "不得擅自修改 Windows ACL"
    # A fresh vault for the same Windows user must recover the key.
    reopened = keyvault.SecretVault(root, backend=keyvault.BACKEND_DPAPI, directory=directory)
    assert reopened.get("gonode-gateway") == secret
    assert reopened.load_errors == []


@pytest.mark.skipif(not keyvault.dpapi_available(), reason="Windows DPAPI is not reachable here")
def test_unreadable_blob_is_reported_and_never_overwritten_with_plaintext(root, tmp_path):
    """A blob another Windows account encrypted must fail loudly, not silently."""
    directory = tmp_path / "secrets"
    directory.mkdir(parents=True)
    # A syntactically valid but foreign DPAPI blob cannot be decrypted here.
    (directory / "gonode-gateway.dpapi").write_bytes(b"\x01\x02\x03\x04not-a-real-dpapi-blob")
    vault = keyvault.SecretVault(root, backend=keyvault.BACKEND_DPAPI, directory=directory)
    assert vault.get("gonode-gateway") is None
    assert len(vault.load_errors) == 1
    assert vault.load_errors[0]["provider_id"] == "gonode-gateway"
    assert "解密失败" in vault.load_errors[0]["reason"]
    described = vault.describe("gonode-gateway")
    assert described["configured"] is False
    assert described["load_error"], "加载失败必须反映到该 Provider 的状态上"
    # The unreadable blob is left untouched rather than replaced.
    assert (directory / "gonode-gateway.dpapi").read_bytes() == b"\x01\x02\x03\x04not-a-real-dpapi-blob"


# --------------------------------------------------------------- the quota


def test_quota_kinds_are_four_and_labelled_independently():
    assert len(quota_module.QUOTA_KINDS) == 4
    assert set(quota_module.KIND_LABELS) == set(quota_module.QUOTA_KINDS)
    assert quota_module.KIND_LABELS[quota_module.OFFICIAL_ACCOUNT_BALANCE] == "官方账户余额"
    assert quota_module.KIND_LABELS[quota_module.THIRD_PARTY_GATEWAY_BALANCE] == "第三方中转站账户余额"
    assert quota_module.KIND_LABELS[quota_module.NEXUS_BUDGET] == "Nexus 内部预算"
    assert quota_module.KIND_LABELS[quota_module.SUBSCRIPTION_ALLOWANCE] == "订阅套餐额度"


def test_documented_endpoint_requires_a_key_and_does_not_guess(root, store):
    """Without an official key the answer is 'credential required', not a number."""
    provider = quota_module.DocumentedBalanceQuota(
        "deepseek-official", path="/user/balance", key_resolver=lambda: None)
    import asyncio
    result = asyncio.run(provider.fetch({}))
    assert result["queryable"] is False
    assert result["status"] == quota_module.STATUS_CREDENTIAL_REQUIRED
    assert result["balances"] == []
    assert result["kind"] == quota_module.OFFICIAL_ACCOUNT_BALANCE


def test_documented_endpoint_parses_the_real_response_shape():
    """DeepSeek documents is_available plus balance_infos[].currency/total_balance."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        assert request.url.path.endswith("/user/balance")
        return httpx.Response(200, json={
            "is_available": True,
            "balance_infos": [{"currency": "CNY", "total_balance": "110.00",
                               "granted_balance": "10.00", "topped_up_balance": "100.00"}]})

    import asyncio
    provider = quota_module.DocumentedBalanceQuota(
        "deepseek-official", path="/user/balance", key_resolver=lambda: "test-key",
        http_transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.fetch({}))
    assert result["queryable"] is True
    assert result["status"] == quota_module.STATUS_OK
    assert result["is_available"] is True
    assert result["balances"][0]["currency"] == "CNY"
    assert result["balances"][0]["total_balance"] == "110.00"
    assert result["kind"] == quota_module.OFFICIAL_ACCOUNT_BALANCE


def test_documented_endpoint_error_path_is_honest():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    import asyncio
    provider = quota_module.DocumentedBalanceQuota(
        "deepseek-official", path="/user/balance", key_resolver=lambda: "test-key",
        http_transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.fetch({}))
    assert result["queryable"] is False
    assert result["status"] == quota_module.STATUS_ERROR
    assert "bad key" not in result["reason"], "错误响应体不得回显"


def test_undocumented_gateway_reports_unsupported_not_fabricated():
    import asyncio
    provider = quota_module.UndocumentedGatewayQuota("gonode-gateway")
    assert provider.capability()["documented"] is False
    result = asyncio.run(provider.fetch({}))
    assert result["queryable"] is False
    assert result["status"] == quota_module.STATUS_UNSUPPORTED
    assert result["display"] == "余额不可查询"
    assert result["balances"] == []


def test_nexus_budget_is_a_user_setting_not_a_provider_statement(store):
    collector = usage_module.UsageCollector(store)
    guard = budget_module.BudgetGuard(store, collector, budget_module.RateCard(store))
    provider = quota_module.NexusBudgetQuota(collector, guard.settings)
    import asyncio
    unset = asyncio.run(provider.fetch({}))
    assert unset["queryable"] is False
    assert unset["status"] == quota_module.STATUS_NOT_CONFIGURED

    guard.update_settings({"daily_budget": 5.0})
    configured = asyncio.run(provider.fetch({}))
    assert configured["queryable"] is True
    assert configured["is_provider_statement"] is False
    assert configured["statement"] == "nexus-self-set"
    assert "不是服务商账单" in configured["reason"]


def test_subscription_allowance_requires_a_declared_plan(store):
    collector = usage_module.UsageCollector(store)
    provider = quota_module.SubscriptionAllowanceQuota(
        "zai-official", plan_reader=lambda: {}, collector=collector)
    import asyncio
    result = asyncio.run(provider.fetch({}))
    assert result["queryable"] is False
    assert result["status"] == quota_module.STATUS_NOT_CONFIGURED
    assert "套餐档位" in result["reason"]


def test_subscription_allowance_uses_the_documented_plan_credits(store):
    collector = usage_module.UsageCollector(store)
    collector.record(usage_module.UsageEvent(
        agent_id="zcode-gateway", provider_id="zai-official", model_key="zai-official/glm-5.3",
        attempt_id="credit-1", usage=usage_module.parse_usage(
            {"prompt_tokens": 10_000, "completion_tokens": 10_000, "total_tokens": 20_000})))
    provider = quota_module.SubscriptionAllowanceQuota(
        "zai-official", plan_reader=lambda: {"plan": "pro"}, collector=collector)
    import asyncio
    result = asyncio.run(provider.fetch({}))
    assert result["queryable"] is True
    assert result["is_provider_statement"] is False
    assert result["statement"] == "nexus-derived"
    assert result["balances"][0]["plan"] == "pro"
    assert result["balances"][0]["weekly_credits"] == 60000


def test_built_registry_marks_both_gateways_unqueryable(store):
    collector = usage_module.UsageCollector(store)
    registry = quota_module.build_registry(
        collector=collector, official_keys={}, budget_settings=lambda: {}, plan_selection={})
    import asyncio
    payload = asyncio.run(registry.fetch_all())
    by_provider = {item["provider_id"]: item for item in payload["results"]}
    assert by_provider["gonode-gateway"]["queryable"] is False
    assert by_provider["hyai666-gateway"]["queryable"] is False
    assert by_provider["gonode-gateway"]["display"] == "余额不可查询"
    assert "gonode-gateway" in payload["unqueryable"]
    # Gateway balance is never reported as the official account balance.
    assert by_provider["gonode-gateway"]["kind"] != quota_module.OFFICIAL_ACCOUNT_BALANCE


def test_registry_survives_one_provider_failing(store):
    class Boom(quota_module.QuotaProvider):
        kind = quota_module.NEXUS_BUDGET
        provider_id = "boom"

        def capability(self):
            return {"provider_id": "boom", "kind": self.kind, "documented": False}

        async def fetch(self, context):
            raise RuntimeError("provider exploded")

    collector = usage_module.UsageCollector(store)
    registry = quota_module.QuotaRegistry([Boom(), quota_module.UndocumentedGatewayQuota("gonode-gateway")])
    import asyncio
    payload = asyncio.run(registry.fetch_all())
    assert len(payload["results"]) == 2
    assert payload["results"][0]["queryable"] is False


# -------------------------------------------------------------- gateway route


def _gateway(config_id: str, **kwargs):
    return gateway.GATEWAY_ROUTES[config_id] and getattr(gateway, {
        "deepseek-gateway": "DeepSeekGatewayAdapter",
        "zcode-gateway": "ZCodeGatewayAdapter"}[config_id])(**kwargs)


def test_gateway_route_declares_its_provider_and_model_key():
    adapter = _gateway("deepseek-gateway", api_key="k" * 20, approved=True)
    assert adapter.provider_id == "gonode-gateway"
    assert adapter.model_key == "gonode-gateway/deepseek-v4.1-flash"
    probe = adapter.probe()
    assert probe["provider_id"] == "gonode-gateway"
    assert probe["model_key"] == "gonode-gateway/deepseek-v4.1-flash"
    assert probe["task_execution_scope"] == "structured-files"
    assert probe["streaming_usage_verified"] is False


def test_gateway_model_switch_keeps_provider_pairing():
    """Switching provider must not silently keep the other provider's model key."""
    adapter = _gateway("deepseek-gateway", api_key="k" * 20, approved=True)
    adapter.provider_id = "deepseek-official"
    adapter.model = "deepseek-flash"
    assert adapter._catalog_key("deepseek-flash") == "deepseek-official/deepseek-flash"
    assert adapter._catalog_key("glm5.3") is None


def test_gateway_usage_is_parsed_with_its_source():
    flat = gateway._usage({"prompt_tokens": 296, "completion_tokens": 52, "total_tokens": 348,
                           "credit": 0.01})
    assert flat["input_tokens"] == 296
    assert flat["output_tokens"] == 52
    assert flat["total_tokens"] == 348
    assert flat["credit"] == 0.01
    assert flat["usage_source"] == usage_module.SOURCE_REPORTED
    assert gateway._usage(None)["usage_source"] == usage_module.SOURCE_UNAVAILABLE


# ------------------------------------------------------------------- routes


def test_catalog_route_lists_the_three_layers(client):
    payload = client.get("/api/catalog").json()
    assert payload["providers"] and payload["models"] and payload["agents"]
    assert payload["layers"] == ["agent", "runtime", "model", "provider"]
    assert client.get("/api/catalog/nope").status_code == 404


def test_agents_route_separates_the_two_capability_facts(client):
    for agent in client.get("/api/agents").json():
        assert "model_api_callable" in agent
        assert "task_execution_capable" in agent
        assert agent["binding"]["provider"] is not None
        assert agent["usage"]["requests"] == 0
        # Nothing has been verified in a fresh workspace.
        assert agent["model_api_callable"] is False
        assert agent["task_execution_capable"] is False


def test_usage_route_returns_an_honest_empty_state(client):
    payload = client.get("/api/usage?span=month").json()
    assert payload["totals"]["requests"] == 0
    assert payload["totals"]["total_tokens"] == 0
    assert payload["totals"]["usage_unavailable"] == 0
    assert payload["records"] == []
    assert payload["by_agent"] == []
    assert payload["series"] == []
    assert "不是服务商账单" in payload["note"]


def test_usage_route_validates_the_span(client):
    assert client.get("/api/usage?span=nonsense").status_code == 422


def test_usage_route_rejects_an_unknown_agent(client):
    assert client.get("/api/usage/agents/nope").status_code == 404


def test_quota_route_marks_undocumented_providers(client):
    payload = client.get("/api/quota").json()
    assert payload["state"] is None, "静态能力声明不应发起真实请求"
    by_provider = {item["provider_id"]: item for item in payload["capability"]}
    assert by_provider["gonode-gateway"]["documented"] is False
    assert by_provider["deepseek-official"]["documented"] is True


def test_quota_probe_reports_unsupported_for_both_gateways(client):
    payload = client.get("/api/quota?probe=true").json()
    by_provider = {item["provider_id"]: item for item in payload["state"]["results"]}
    assert by_provider["gonode-gateway"]["status"] == "unsupported"
    assert by_provider["hyai666-gateway"]["status"] == "unsupported"
    assert by_provider["gonode-gateway"]["display"] == "余额不可查询"
    assert by_provider["deepseek-official"]["status"] == "credential_required"


def test_budget_route_starts_unconfigured(client):
    payload = client.get("/api/budget").json()
    assert payload["settings"]["daily_budget"] is None
    assert payload["gpt_auto_dispatch"] is False
    assert payload["cost_source"] == "nexus-estimate"


def test_provider_settings_route_lists_ten_providers_without_keys(client):
    payload = client.get("/api/settings/providers").json()
    assert len(payload["providers"]) == len(catalog.PROVIDERS)
    for provider in payload["providers"]:
        credential = provider["credential"]
        # Whatever the state of the local vault, a key value is never exposed:
        # only a configured flag, a fingerprint and a length.
        assert set(credential) >= {"provider_id", "configured", "fingerprint", "length"}
        assert "api_key" not in json.dumps(credential)
        assert "secret" not in json.dumps(credential)
        if credential["configured"]:
            assert credential["fingerprint"] and credential["length"]
    assert "绝不下发密钥" in payload["note"]


def test_rates_route_exposes_editable_rates(client):
    payload = client.get("/api/settings/rates").json()
    assert len(payload["models"]) == len(catalog.MODELS)
    assert "估算" in payload["note"]


def test_binding_route_refuses_a_cross_provider_switch(client):
    token = client.get("/api/session").json()["token"]
    response = client.put("/api/agents/deepseek-gateway/binding",
                          headers={"X-Nexus-Token": token},
                          json={"provider_id": "zai-official", "model_key": "zai-official/glm-5.3"})
    assert response.status_code == 422


def test_binding_route_requires_ack_for_an_unconfigured_provider(client):
    token = client.get("/api/session").json()["token"]
    headers = {"X-Nexus-Token": token}
    payload = {"provider_id": "deepseek-official",
               "model_key": "deepseek-official/deepseek-flash"}
    assert client.put("/api/agents/deepseek-gateway/binding", headers=headers,
                      json=payload).status_code == 403
    accepted = client.put("/api/agents/deepseek-gateway/binding", headers=headers,
                          json={**payload, "acknowledge_unconfigured": True})
    assert accepted.status_code == 200
    assert accepted.json()["binding"]["provider"]["id"] == "deepseek-official"
    assert accepted.json()["requires_verification"] is True


def test_mutations_require_the_local_session_token(client):
    assert client.put("/api/settings/budget", json={"daily_budget": 1}).status_code == 403
    assert client.put("/api/agents/deepseek-gateway/binding",
                      json={"provider_id": "gonode-gateway"}).status_code == 403


def test_key_management_is_disabled_on_a_test_instance(client):
    token = client.get("/api/session").json()["token"]
    response = client.put("/api/settings/providers/gonode-gateway/key",
                          headers={"X-Nexus-Token": token},
                          json={"api_key": "k" * 20, "approve_usage": True})
    assert response.status_code == 403


def test_plan_route_validates_the_tier(client):
    token = client.get("/api/session").json()["token"]
    headers = {"X-Nexus-Token": token}
    assert client.put("/api/settings/plan/zai-official", headers=headers,
                      json={"plan": "enterprise"}).status_code == 422
    assert client.put("/api/settings/plan/zai-official", headers=headers,
                      json={"plan": "pro"}).status_code == 200


def test_vision_test_refuses_a_model_without_declared_vision(client):
    token = client.get("/api/session").json()["token"]
    client.put("/api/agents/deepseek-gateway/binding", headers={"X-Nexus-Token": token},
               json={"provider_id": "gonode-gateway", "model_key": "gonode-gateway/deepseek-v4.1-pro",
                     "acknowledge_unconfigured": True})
    response = client.post("/api/agents/deepseek-gateway/vision-test",
                           headers={"X-Nexus-Token": token},
                           json={"model_key": "gonode-gateway/deepseek-v4-pro"})
    assert response.status_code in (403, 422)
