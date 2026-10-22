"""Model-free integration: HTTP fixtures -> scheduler -> real isolated Git files.

Fixture responses are never evidence of a real provider connection.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
import subprocess
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from nexus.adapters import AgentEvent, DisconnectedAdapter, make_adapters
from nexus.gemini_adapter import GeminiAdapter
from nexus.main import create_app
from nexus.registry import AdapterRegistry
from nexus.storage import now


class FixtureAdapter(DisconnectedAdapter):
    id, name = "fixture", "LOCAL FIXTURE"

    def __init__(self, *, fail_probe=False, fail_detect=False, verified=True, capabilities=("file.generate",)):
        super().__init__()
        self.fail_probe, self.fail_detect = fail_probe, fail_detect
        self.verified_at = now() if verified else None
        self.capabilities = capabilities

    async def detect(self):
        if self.fail_detect:
            raise RuntimeError("LOCAL DETECTION FIXTURE FAILURE")
        return self.probe()

    def probe(self):
        if self.fail_probe:
            raise RuntimeError("LOCAL PROBE FIXTURE FAILURE")
        return {**super().probe(), "ready": True, "status": "connected", "verified_at": self.verified_at,
                "capabilities": list(self.capabilities)}


def test_auto_requires_fresh_verified_connection_and_whole_capability_set():
    a, b = FixtureAdapter(verified=False), FixtureAdapter(capabilities=("file.generate", "file.edit"))
    registry = AdapterRegistry({"a": a, "b": b})
    assert registry.select("auto", {"file.generate"}) is b
    assert registry.select("auto", {"file.generate", "browser.test"}) is None
    assert registry.select("auto", {"file.generate"}, {"b"}) is None
    b.verified_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert registry.select("auto", {"file.generate"}) is None
    b.verified_at = "invalid timestamp"
    assert registry.snapshot("b")["ready"] is False


def test_batch_verification_requires_approval_and_isolates_route_failure(tmp_path, monkeypatch):
    app, adapter = setup_app(tmp_path, monkeypatch)
    class Broken(FixtureAdapter):
        id, ready, transport = 'zcode', True, 'local-batch-fixture'
        async def execute(self, context):
            if False:
                yield AgentEvent('log', 'LOCAL FIXTURE')
            raise RuntimeError('LOCAL BATCH FAILURE FIXTURE')
    with TestClient(app) as client:
        client.headers['X-Nexus-Token'] = app.state.token
        adapter.configure('LOCAL_FIXTURE_SECRET', 'gemini-local-fixture', True)
        app.state.adapters['zcode'] = Broken()
        preview = client.get('/api/agents/verification-batch').json()
        assert len(preview['routes']) == 6
        assert not next(r for r in preview['routes'] if r['agent_id'] == 'workbuddy')['eligible']
        before = len(client.get('/api/tasks').json())
        body = {'revision': preview['revision'], 'runtime_ids': ['zcode', 'gemini', 'gemini']}
        assert client.post('/api/agents/verification-batch', json=body).status_code == 403
        assert len(client.get('/api/tasks').json()) == before
        assert client.post('/api/agents/verification-batch', json={**body, 'revision': 'stale', 'approve_usage': True}).status_code == 409
        response = client.post('/api/agents/verification-batch', json={**body, 'approve_usage': True})
        assert response.status_code == 202, response.text
        results = response.json()['results']
        assert len(results) == 2
        tasks = {r['runtime_id']: wait(client, r['task_id']) for r in results}
        assert tasks['zcode']['status'] == 'failed'
        assert tasks['gemini']['status'] == 'done'
        assert tasks['gemini']['last_attempt']['artifacts'] == []


def test_one_probe_or_detection_failure_does_not_disable_other_adapter():
    good, bad = FixtureAdapter(), FixtureAdapter(fail_probe=True, fail_detect=True)
    registry = AdapterRegistry({"bad": bad, "good": good})
    results = asyncio.run(registry.detect_all())
    assert len(results) == 2 and results[0]["status"] == "disconnected"
    assert registry.select("auto", {"file.generate"}) is good
    bad.fail_detect = bad.fail_probe = False
    assert asyncio.run(registry.detect("bad"))["status"] == "connected"


def test_module_import_failure_is_local(tmp_path, monkeypatch):
    original = importlib.import_module
    def load(name, package=None):
        if name == ".zcode_adapter":
            raise ImportError("LOCAL TEST ONLY")
        return original(name, package)
    monkeypatch.setattr("nexus.adapters.importlib.import_module", load)
    adapters = make_adapters(tmp_path, False)
    assert len(adapters) == 8
    assert adapters["zcode"].probe()["blockers"] == ["adapter_load_error:ImportError"]
    assert adapters["codex"].id == "codex" and adapters["mimo"].id == "mimo"


def repo(root):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "baseline.txt").write_text("baseline", "utf-8")
    subprocess.run(["git", "add", "baseline.txt"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=Local Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "LOCAL TEST FIXTURE"], cwd=root, check=True)


def wait(client, task_id):
    for _ in range(150):
        task = next(t for t in client.get("/api/tasks").json() if t["id"] == task_id)
        if task["status"] != "running":
            return task
        time.sleep(.02)
    pytest.fail("Local fixture task timed out")


def setup_app(root, monkeypatch, *, wrong_nonce=False):
    repo(root)
    def handler(request):
        body = json.loads(request.content)
        context = json.loads(body["contents"][0]["parts"][0]["text"])
        objective = context["objective"]
        if "Connection test:" in objective:
            nonce = objective.split("exactly ")[1].split(",")[0]
            plan = {"summary": "WRONG LOCAL NONCE" if wrong_nonce else nonce, "files": [], "approval_requests": []}
        else:
            plan = {"summary": "LOCAL HTTP FIXTURE; not Gemini execution evidence", "files": [{"path": "fixture.txt", "content": "LOCAL FIXTURE CONTENT"}], "approval_requests": []}
        event = {"responseId": "LOCAL_FIXTURE_ID", "candidates": [{"content": {"parts": [{"text": json.dumps(plan)}]}, "finishReason": "STOP"}]}
        return httpx.Response(200, text="data: " + json.dumps(event) + "\n\n", headers={"content-type": "text/event-stream"})
    adapter = GeminiAdapter(root, http_transport=httpx.MockTransport(handler))
    adapters = make_adapters(root, False)
    adapters["gemini"] = adapter
    monkeypatch.setattr("nexus.main.make_adapters", lambda *a: adapters)
    return create_app(root), adapter


def test_full_http_adapter_configuration_nonce_scheduler_artifact_and_fault_isolation(tmp_path, monkeypatch):
    app, adapter = setup_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.headers["X-Nexus-Token"] = app.state.token
        body = {"api_key": "LOCAL_FIXTURE_SECRET", "model": "gemini-local-fixture", "approve_usage": True}
        assert client.post("/api/agents/gemini/configure", json={**body, "approve_usage": False}).status_code == 403
        configured = client.post("/api/agents/gemini/configure", json=body)
        assert configured.status_code == 200 and configured.json()["status"] == "disconnected"
        # Auto cannot use merely configured accounts.
        first = client.post("/api/tasks", json={"title": "LOCAL FIXTURE AUTO", "agent_id": "auto", "auto_run": True}).json()
        assert first["status"] == "blocked"
        assert "明确选择已配置的 API 路径" in first["result"]
        assert "单次费用批准不开放自动选择" in first["result"]
        assert first.get("last_attempt") is None
        response = client.post("/api/agents/gemini/test")
        verified = wait(client, response.json()["task"]["id"])
        assert verified["status"] == "done" and not verified["last_attempt"]["artifacts"]
        app.state.adapters["zcode"] = FixtureAdapter(fail_probe=True, fail_detect=True)
        task = client.post("/api/tasks", json={"title": "LOCAL FIXTURE AUTO", "agent_id": "auto", "auto_run": True}).json()
        done = wait(client, task["id"])
        assert done["status"] == "done" and done["agent_id"] == "auto"
        attempt = done["last_attempt"]
        assert attempt["agent_id"] == "gemini" and attempt["transport"] == "official-rest-sse"
        assert attempt["pid"] is None and attempt["thread_id"] == "LOCAL_FIXTURE_ID"
        assert attempt["artifacts"][0]["generated_by"] == "gemini-api"
        assert (Path(done["worktree"]) / "fixture.txt").read_text("utf-8") == "LOCAL FIXTURE CONTENT"
        assert not (tmp_path / "fixture.txt").exists()
        assert client.get(f"/api/tasks/{task['id']}/artifacts/fixture.txt").content == b"LOCAL FIXTURE CONTENT"
        assert len(client.get("/api/agents").json()) == 8
        assert client.post("/api/agents/gemini/disconnect").status_code == 200
        assert not adapter.ready
        assert "LOCAL_FIXTURE_SECRET" not in client.get("/api/state").text
        assert b"LOCAL_FIXTURE_SECRET" not in app.state.store.path.read_bytes()


def test_wrong_connection_nonce_revokes_verification(tmp_path, monkeypatch):
    app, adapter = setup_app(tmp_path, monkeypatch, wrong_nonce=True)
    with TestClient(app) as client:
        client.headers["X-Nexus-Token"] = app.state.token
        adapter.configure("LOCAL_FIXTURE_KEY", "gemini-fixture", True)
        response = client.post("/api/agents/gemini/test").json()
        assert wait(client, response["task"]["id"])["status"] == "failed"
        assert adapter.verified_at is None


def test_configuration_errors_do_not_echo_secret_and_test_instance_cannot_authorize(tmp_path):
    app = create_app(tmp_path, enable_codex=False)
    with TestClient(app) as client:
        client.headers["X-Nexus-Token"] = app.state.token
        secret = "DO_NOT_ECHO_SECRET"
        bad = client.post("/api/agents/mimo/configure", json={"api_key": secret, "model": "https://evil", "approve_usage": True})
        assert bad.status_code == 422 and secret not in bad.text
        assert client.post("/api/agents/mimo/configure", json={"api_key": secret, "model": "mimo-fixture", "approve_usage": True}).status_code == 403


def test_cancel_cleanup_fault_does_not_break_other_adapter_or_task_status(tmp_path, monkeypatch):
    app, gemini = setup_app(tmp_path, monkeypatch)
    class CancelFailure(FixtureAdapter):
        id, name = "zcode", "LOCAL CANCEL FAILURE FIXTURE"
        async def execute(self, context):
            yield AgentEvent("log", "LOCAL TEST FIXTURE WAITING")
            await asyncio.sleep(30)
        async def cancel(self, task_id):
            raise RuntimeError("LOCAL CANCEL ERROR FIXTURE")
    with TestClient(app) as client:
        client.headers["X-Nexus-Token"] = app.state.token
        app.state.adapters["zcode"] = CancelFailure()
        task = client.post("/api/tasks", json={"title": "LOCAL CANCEL FIXTURE", "agent_id": "zcode", "auto_run": True}).json()
        response = client.post(f"/api/tasks/{task['id']}/cancel")
        assert response.status_code == 200 and response.json()["status"] == "cancelled"
        assert response.json()["last_attempt"]["error_kind"] == "cancel_cleanup_error"
        assert app.state.registry.snapshot("zcode")["ready"] is False
        gemini.configure("LOCAL_FIXTURE_KEY", "gemini-fixture", True)
        verify = client.post("/api/agents/gemini/test").json()
        assert wait(client, verify["task"]["id"])["status"] == "done"


def test_phase_b_expired_verification_is_not_a_process_failure():
    adapter = FixtureAdapter()
    adapter.verified_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    snapshot = AdapterRegistry({'fixture': adapter}).snapshot('fixture')
    assert snapshot['ready'] is True and snapshot['status'] == 'disconnected'
    assert snapshot['status_reason'] == 'verification_expired'
    assert snapshot['verification_state'] == 'expired'
    assert snapshot['last_verified_at'] == adapter.verified_at
    assert '过期' in snapshot['status_label']
    assert not snapshot.get('last_error')
