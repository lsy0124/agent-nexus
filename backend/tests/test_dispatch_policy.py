"""Dispatch policy is a statement about approval, billing and order.

These tests use no model, no network and no credential. They assert that the
policy cannot silently enable a metered Agent, cannot drop GPT/Codex into
automatic dispatch, and never overrides an explicit user choice.
"""
import pytest

from nexus.policy import DEFAULT_DISABLED, DEFAULT_PRIORITY, DispatchPolicy
from nexus.registry import AdapterRegistry
from nexus.adapters import DisconnectedAdapter
from nexus.storage import now


class Ready(DisconnectedAdapter):
    def __init__(self, agent_id, capabilities=("file.generate",)):
        super().__init__()
        self.id = self.name = agent_id
        self.capabilities = capabilities
        self.verified_at = now()

    def probe(self):
        return {**super().probe(), "ready": True, "status": "connected",
                "verified_at": self.verified_at, "capabilities": list(self.capabilities)}


def test_default_policy_disables_gpt_automatic_dispatch_and_keeps_explicit_use():
    policy = DispatchPolicy()
    assert "codex" in policy.disabled and policy.disabled == frozenset(DEFAULT_DISABLED)
    assert policy.may_auto_dispatch("codex") is False
    assert "codex" in policy.priority and policy.rank("codex") > policy.rank("dsh")
    summary = policy.summary()
    assert summary["gpt_auto_dispatch"] is True
    assert summary["paid_api_auto_switch"] is False


def test_metered_api_agent_is_never_an_automatic_candidate():
    policy = DispatchPolicy()
    assert policy.billing("mimo") == "metered-api"
    blockers = policy.auto_blockers("mimo")
    assert [item["code"] for item in blockers] == ["billing_not_auto_approved"]
    assert blockers[0]["requires_approval"] is True
    assert policy.may_auto_dispatch("mimo") is False


def test_auto_selection_follows_priority_and_skips_disabled_agent():
    policy = DispatchPolicy()
    adapters = {"codex": Ready("codex"), "dsh": Ready("dsh"), "zcode": Ready("zcode")}
    registry = AdapterRegistry(adapters)
    assert registry.select("auto", {"file.generate"}, (), policy) is adapters["zcode"]
    # A partial match is not enough: the whole capability set must be present.
    assert registry.select("auto", {"file.generate", "file.edit"}, (), policy) is None
    adapters["zcode"].capabilities = ("file.generate", "file.edit")
    assert registry.select("auto", {"file.generate", "file.edit"}, {"dsh"}, policy) is adapters["zcode"]
    # Codex is connected and idle, and is still not selected automatically.
    assert registry.select("auto", {"file.generate", "file.edit"}, {"dsh", "zcode"}, policy) is None


def test_explicit_selection_is_not_redirected_by_policy():
    policy = DispatchPolicy()
    adapters = {"codex": Ready("codex"), "dsh": Ready("dsh")}
    registry = AdapterRegistry(adapters)
    assert registry.select("codex", {"file.generate"}, (), policy) is adapters["codex"]
    assert registry.select("codex", {"file.generate", "browser.test"}, (), policy) is None


def test_hold_reasons_name_every_non_candidate_without_inventing_one():
    policy = DispatchPolicy()
    registry = AdapterRegistry({"codex": Ready("codex"), "dsh": Ready("dsh"), "mimo": Ready("mimo")})
    registry.adapters["mimo"].capabilities = ()
    reasons = registry.auto_hold_reasons({"file.generate"}, policy, {"dsh"})
    joined = " | ".join(reasons)
    assert "codex：调度策略停用自动派发（auto_dispatch_disabled）" in joined
    assert "mimo：调度策略停用自动派发（billing_not_auto_approved）" in joined
    assert "dsh：调度策略停用自动派发（billing_not_auto_approved）" in joined


def test_unknown_agent_is_not_treated_as_subscription():
    policy = DispatchPolicy(known=["fixture"])
    assert policy.billing("fixture") == "unknown"
    assert policy.auto_blockers("fixture")[0]["code"] == "billing_not_auto_approved"
    # Historical behaviour for callers without a policy object is unchanged.
    registry = AdapterRegistry({"fixture": Ready("fixture")})
    assert registry.select("auto", {"file.generate"}) is registry.adapters["fixture"]


def test_manual_only_agent_has_no_automatic_path():
    policy = DispatchPolicy()
    assert policy.billing("workbuddy") == "subscription"
    codes = [item["code"] for item in policy.auto_blockers("workbuddy")]
    assert codes == ["manual_only"]
    assert policy.may_auto_dispatch("workbuddy") is False
    assert policy.snapshot([{"id": "workbuddy", "status": "manual_handoff"}])["agents"][0]["explicit_allowed"] is True


def test_priority_and_disabled_lists_are_environment_configurable(monkeypatch):
    monkeypatch.setenv("NEXUS_PRIORITY", "zcode,dsh")
    monkeypatch.setenv("NEXUS_DISABLED_AGENTS", "zcode,dsh,codex")
    monkeypatch.setenv("NEXUS_MANUAL_ONLY", "")
    policy = DispatchPolicy.from_environment()
    assert policy.priority == ("zcode", "dsh")
    assert policy.disabled == {"zcode", "dsh", "codex"}
    assert policy.manual_only == frozenset()
    assert policy.may_auto_dispatch("dsh") is False
    assert policy.rank("gemini") > policy.rank("dsh")


def test_snapshot_reports_policy_without_claiming_connection():
    policy = DispatchPolicy()
    snapshot = policy.snapshot([{"id": "dsh", "status": "disconnected"}])
    row = next(item for item in snapshot["agents"] if item["id"] == "dsh")
    assert row["auto_dispatch"] is False and row["model_status"] == "disconnected"
    assert snapshot["note"].startswith("策略只决定自动调度候选")
    assert DEFAULT_PRIORITY[1] == "dsh" and DEFAULT_PRIORITY[2] == "zcode"
    assert DEFAULT_PRIORITY[-1] == "codex"


def test_invalid_environment_list_falls_back_to_empty_not_silent_default(monkeypatch):
    monkeypatch.setenv("NEXUS_DISABLED_AGENTS", " , ")
    policy = DispatchPolicy.from_environment()
    assert policy.disabled == frozenset()
    assert policy.may_auto_dispatch("codex") is True
    assert policy.summary()["gpt_auto_dispatch"] is False


@pytest.mark.parametrize("agent_id", ["zcode", "gemini"])
def test_subscription_agents_are_auto_candidates_but_still_need_verification(agent_id):
    policy = DispatchPolicy()
    assert policy.may_auto_dispatch(agent_id) is True
    registry = AdapterRegistry({agent_id: DisconnectedAdapter()})
    registry.adapters[agent_id].id = agent_id
    assert registry.select("auto", set(), (), policy) is None
