"""Optional adapter modules can be enabled independently without installing clients."""
from nexus.adapters import AGENT_MODULES, make_adapters


def test_deployment_allowlist_does_not_enable_other_adapters(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_AGENTS", "codex,gemini")
    adapters = make_adapters(tmp_path)
    assert adapters["codex"].enabled and adapters["gemini"].enabled
    assert not any(adapters[key].enabled for key in ("mimo", "dsh", "zcode", "workbuddy",
                                                     "zcode-gateway", "deepseek-gateway"))
    assert len(adapters) == len(AGENT_MODULES) == 8


def test_gateway_routes_are_separate_units_from_the_first_party_adapters():
    # The gateway routes must never be confused with the official CLI/desktop
    # adapters: separate modules, separate ids, separate transports.
    assert AGENT_MODULES["zcode-gateway"][0] == "gateway_adapter"
    assert AGENT_MODULES["deepseek-gateway"][0] == "gateway_adapter"
    assert AGENT_MODULES["zcode"][0] == "zcode_adapter"
    assert AGENT_MODULES["dsh"][0] == "dsh_adapter"
