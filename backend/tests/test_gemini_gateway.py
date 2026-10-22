"""Mode selection is explicit; a subscription is never API billing authorization."""
import pytest

from nexus.gemini_gateway import GeminiGateway


def test_gateway_defaults_to_subscription_and_requires_explicit_api_authorization(tmp_path):
    adapter = GeminiGateway(tmp_path)
    assert adapter.probe()["connection_mode"] == "google-subscription-cli"
    assert adapter.probe()["status"] == "disconnected"
    with pytest.raises(ValueError):
        adapter.configure(api_key="LOCAL_FIXTURE_KEY", model="gemini-fixture", approved=False)
    assert adapter.mode == "cli"
    adapter.configure(api_key="LOCAL_FIXTURE_KEY", model="gemini-fixture", approved=True)
    assert adapter.probe()["connection_mode"] == "official-rest-api"
    assert adapter.probe()["status"] == "disconnected"
    adapter.configure_subscription(approved=True)
    assert adapter.mode == "cli" and not adapter.api.ready
    adapter.clear_configuration()
    assert not adapter.ready


def test_gateway_disabled_instance_never_authorizes_either_mode(tmp_path):
    adapter = GeminiGateway(tmp_path, enabled=False)
    with pytest.raises(ValueError):
        adapter.configure_subscription(approved=True)
    with pytest.raises(ValueError):
        adapter.configure(api_key="LOCAL_FIXTURE_KEY", model="gemini-fixture", approved=True)
