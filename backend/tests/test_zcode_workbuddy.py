"""Independent inert-adapter checks; no model, login or network request is made."""
import asyncio
import json
import subprocess

import pytest

from nexus.adapters import ExecutionContext
from nexus import workbuddy_adapter, zcode_adapter


@pytest.mark.parametrize("module,class_name", [(zcode_adapter, "ZCodeAdapter"), (workbuddy_adapter, "WorkBuddyAdapter")])
def test_disabled_adapter_performs_no_discovery_or_execution(module, class_name, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Disabled adapters must not inspect the host")

    monkeypatch.setattr(module.shutil, "which", forbidden)
    monkeypatch.setattr(module, "_desktop_metadata", forbidden)
    adapter = getattr(module, class_name)(tmp_path, enabled=False)
    state = asyncio.run(adapter.detect())
    assert state["status"] == ("manual_handoff" if module is workbuddy_adapter else "disconnected")
    assert state["ready"] is False and state["verified_at"] is None
    assert not state["installed"] and not state["capabilities"]
    assert state["blockers"][0]["code"] == "disabled"


@pytest.mark.parametrize("module,class_name", [(zcode_adapter, "ZCodeAdapter"), (workbuddy_adapter, "WorkBuddyAdapter")])
def test_desktop_installation_is_not_a_verified_connection(module, class_name, tmp_path, monkeypatch):
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    monkeypatch.setattr(module, "_desktop_metadata", lambda: [
        {"name": "PUBLIC INSTALLATION METADATA FIXTURE", "version": "1.2.3", "executable": "C:/fixture/desktop.exe"},
    ])
    adapter = getattr(module, class_name)(tmp_path)
    state = asyncio.run(adapter.detect())
    assert state["installed"] is True
    assert state["executable_detected"] is False
    assert state["status"] == ("manual_handoff" if module is workbuddy_adapter else "disconnected") and state["ready"] is False
    assert state["verified_at"] is None and state["capabilities"] == []
    assert (state.get("manual_capabilities") if module is workbuddy_adapter else state["supported_capabilities"]) and state["sources"]
    assert all(source["url"].startswith("https://") for source in state["sources"])
    assert state["desktop_installations"][0]["version"] == "1.2.3"


@pytest.mark.parametrize("module,class_name", [(zcode_adapter, "ZCodeAdapter"), (workbuddy_adapter, "WorkBuddyAdapter")])
@pytest.mark.parametrize("failure", [OSError("metadata denied"), subprocess.TimeoutExpired("metadata", 8), ValueError("invalid metadata")])
def test_discovery_failure_is_local_and_retryable(module, class_name, failure, tmp_path, monkeypatch):
    monkeypatch.setattr(module.shutil, "which", lambda name: None)

    def fail():
        raise failure

    monkeypatch.setattr(module, "_desktop_metadata", fail)
    adapter = getattr(module, class_name)(tmp_path)
    state = asyncio.run(adapter.detect())
    assert state["status"] == ("manual_handoff" if module is workbuddy_adapter else "disconnected") and state["discovery_error"]
    monkeypatch.setattr(module, "_desktop_metadata", lambda: [])
    assert asyncio.run(adapter.detect())["discovery_error"] is None


@pytest.mark.parametrize("module,class_name", [(zcode_adapter, "ZCodeAdapter"), (workbuddy_adapter, "WorkBuddyAdapter")])
def test_detected_cli_never_claims_verified_execution(module, class_name, tmp_path, monkeypatch):
    monkeypatch.setattr(module.shutil, "which", lambda name: "C:/fixture/cli.exe")
    monkeypatch.setattr(module, "_desktop_metadata", lambda: [])
    adapter = getattr(module, class_name)(tmp_path)
    state = asyncio.run(adapter.detect())
    assert state["executable_detected"] and state["installed"]
    assert not adapter.ready and not state["capabilities"]
    assert state["verified_at"] is None
    assert state.get("manual_available") if module is workbuddy_adapter else any(item["requires_approval"] for item in state["blockers"])

    async def execute():
        context = ExecutionContext("fixture", str(tmp_path), "No model request allowed", ())
        events = []
        with pytest.raises(ConnectionError):
            async for event in adapter.execute(context):
                events.append(event)
        await adapter.cancel("fixture")
        assert events == [] and adapter.active == {}

    asyncio.run(execute())


def test_workbuddy_does_not_claim_remote_cancellation_support(tmp_path, monkeypatch):
    monkeypatch.setattr(workbuddy_adapter.shutil, "which", lambda name: None)
    state = workbuddy_adapter.WorkBuddyAdapter(tmp_path).probe()
    assert "cancel" not in state["supported_capabilities"]
    assert state["automatic_connected"] is False
    assert any(item["code"] == "manual_provenance" for item in state["blockers"])
    assert "openapi" in state["sources"][0]["url"]


def test_zcode_reports_official_cli_separately_from_desktop(tmp_path, monkeypatch):
    monkeypatch.setattr(zcode_adapter.shutil, "which", lambda name: None)
    state = zcode_adapter.ZCodeAdapter(tmp_path).probe()
    assert any(item["code"] == "cli_not_installed" for item in state["blockers"])
    assert "zai-org/ZCode" in json.dumps(state["sources"])
