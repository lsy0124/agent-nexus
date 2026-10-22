"""Human-handoff fixtures, never real WorkBuddy execution or real user approval."""
import json
import os
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient
import pytest

from nexus.main import create_app


@pytest.fixture
def client(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "baseline.txt").write_text("LOCAL BASELINE FIXTURE", "utf-8")
    subprocess.run(["git", "add", "baseline.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=Local Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "LOCAL TEST FIXTURE"], cwd=tmp_path, check=True)
    app = create_app(tmp_path, enable_codex=False)
    with TestClient(app) as client:
        client.headers["X-Nexus-Token"] = app.state.token
        yield client


def task(client, **kwargs):
    response = client.post("/api/tasks", json={"title": "LOCAL MANUAL HANDOFF FIXTURE", "description": "Not a real WorkBuddy task execution", "agent_id": "workbuddy", **kwargs})
    assert response.status_code == 201, response.text
    return response.json()


def prepare(client, t):
    response = client.post(f"/api/tasks/{t['id']}/handoff/prepare")
    assert response.status_code == 200, response.text
    return response.json()["handoff"]


def confirm(client, h):
    response = client.post(f"/api/handoffs/{h['id']}/confirm")
    assert response.status_code == 200, response.text
    return response.json()["handoff"]


def submission(h, **kwargs):
    return {"handoff_id": h["id"], "task_id": h["task_id"], "packet_sha256": h["packet_sha256"],
            "summary": "LOCAL IMPORT FIXTURE; provenance not verified", "files": [{"path": "hello.txt", "content": "Hello Agent Nexus"}],
            "test_reports": [{"name": "LOCAL CLAIMED TEST FIXTURE", "status": "passed", "command": "not executed by Nexus", "output": "EXTERNAL DECLARATION ONLY"}], **kwargs}


def import_result(client, h, **kwargs):
    return client.post(f"/api/handoffs/{h['id']}/import", json={"sender": "LOCAL TEST FIXTURE IMPORTER", "submission": submission(h, **kwargs)})


def review(client, h, **kwargs):
    return client.post(f"/api/handoffs/{h['id']}/review", json={"decision": "approve", "reviewer": "LOCAL TEST FIXTURE REVIEWER", "note": "TEST DECISION ONLY, NOT USER APPROVAL", "approve_files": True, **kwargs})


def test_manual_status_never_routes_as_automatic_agent_and_oauth_is_default_disabled(client):
    buddy = next(a for a in client.get("/api/agents").json() if a["id"] == "workbuddy")
    assert buddy["status"] == "manual_handoff" and not buddy["ready"] and not buddy["automatic_connected"]
    assert buddy["capabilities"] == [] and buddy["verified_at"] is None
    response = client.post("/api/agents/workbuddy/oauth/begin", json={"client_id": "fixture", "client_secret": "fixture", "application_enabled": True, "approve_read_scope": True})
    assert response.status_code == 403
    auto = task(client, agent_id="auto", auto_run=True)
    assert auto["status"] == "blocked" and auto["last_attempt"] is None


def test_end_to_end_manual_import_review_real_file_and_persistence(client):
    t = task(client, auto_run=True)
    assert t["status"] == "blocked" and t["last_attempt"] is None
    h = t["latest_handoff"]
    assert h["phase"] == "prepared" and h["packet"] is None
    assert client.get(f"/api/handoffs/{h['id']}/packet").status_code == 409
    assert prepare(client, t)["id"] == h["id"]  # no duplicate preparation
    h = confirm(client, h)
    packet = client.get(f"/api/handoffs/{h['id']}/packet")
    assert packet.status_code == 200 and h["packet_sha256"] in packet.text
    assert "不要直接写入" in packet.text and "not_run" in packet.text
    assert import_result(client, h).status_code == 200
    root = Path(t["worktree"])
    assert not (root / "hello.txt").exists()
    assert client.get(f"/api/tasks/{t['id']}/artifacts/hello.txt").status_code == 404
    result = review(client, h)
    assert result.status_code == 200, result.text
    approved = result.json()["handoff"]
    assert approved["phase"] == "approved" and approved["external_execution_verified"] is False
    assert approved["test_reports"][0]["status"] == "passed"  # claimed only, not re-executed
    assert approved["artifacts"][0]["generated_by"] == "human-import:workbuddy-unverified"
    assert (root / "hello.txt").read_bytes() == b"Hello Agent Nexus"
    assert client.get(f"/api/tasks/{t['id']}/artifacts/hello.txt").content == b"Hello Agent Nexus"
    updated = next(v for v in client.get("/api/tasks").json() if v["id"] == t["id"])
    assert updated["status"] == "done" and updated["last_attempt"] is None
    assert client.get(f"/api/tasks/{t['id']}/attempts").json() == []
    assert client.get(f"/api/handoffs/{h['id']}").json()["handoff"]["review"]["reviewer"] == "LOCAL TEST FIXTURE REVIEWER"
    assert review(client, h).status_code == 409
    assert not (root.parents[2] / "hello.txt").exists()


def test_reject_does_not_write_and_new_round_invalidates_old_import(client):
    t = task(client)
    h = confirm(client, prepare(client, t))
    assert import_result(client, h).status_code == 200
    rejected = review(client, h, decision="reject", approve_files=False)
    assert rejected.status_code == 200 and rejected.json()["handoff"]["phase"] == "rejected"
    assert not (Path(t["worktree"] or h["task_snapshot"]["task"]["worktree"]) / "hello.txt").exists()
    h2 = prepare(client, t)
    assert h2["id"] != h["id"]
    assert import_result(client, h).status_code == 409


@pytest.mark.parametrize("field,value", [("handoff_id", "f"*12), ("task_id", "a"*12), ("packet_sha256", "0"*64)])
def test_binding_rejects_foreign_task_results(client, field, value):
    h = confirm(client, prepare(client, task(client)))
    assert import_result(client, h, **{field: value}).status_code == 409
    assert client.get(f"/api/handoffs/{h['id']}").json()["handoff"]["phase"] == "confirmed"


@pytest.mark.parametrize("path", ["../escape.txt", "C:/escape.txt", ".env", "auth.json", "node_modules/a.js", "test.ps1", "NUL.txt", "a.txt:stream"])
def test_unsafe_file_import_fails_before_review(client, path):
    h = confirm(client, prepare(client, task(client)))
    assert import_result(client, h, files=[{"path": path, "content": "LOCAL FIXTURE"}]).status_code == 422


def test_duplicate_case_size_and_empty_fields_are_rejected(client):
    h = confirm(client, prepare(client, task(client)))
    for files in ([{"path": "a.txt", "content": "a"}, {"path": "A.txt", "content": "b"}], [{"path": "big.txt", "content": "汉"*100000}]):
        assert import_result(client, h, files=files).status_code == 422
    assert import_result(client, h, summary=" ").status_code == 422
    assert import_result(client, h, test_reports=[{"name": "x", "status": "invented"}]).status_code == 422


def test_approval_requires_explicit_file_permission(client):
    h = confirm(client, prepare(client, task(client)))
    assert import_result(client, h).status_code == 200
    assert review(client, h, approve_files=False).status_code == 403
    assert review(client, h, reviewer=" ").status_code == 422
    assert client.get(f"/api/handoffs/{h['id']}").json()["handoff"]["phase"] == "awaiting_review"


def test_import_requires_confirmation_and_edit_invalidates_packet(client):
    t = task(client)
    h = prepare(client, t)
    assert import_result(client, h).status_code == 409
    h = confirm(client, h)
    response = client.put(f"/api/tasks/{t['id']}", json={"title": "EDITED LOCAL FIXTURE", "agent_id": "workbuddy"})
    assert response.status_code == 200
    assert response.json()["latest_handoff"]["phase"] == "cancelled"
    assert import_result(client, h).status_code == 409
    assert client.get(f"/api/handoffs/{h['id']}/packet").status_code == 409


def test_selected_file_staleness_prevents_overwrite(client):
    t = task(client, input_files=["baseline.txt"])
    h = confirm(client, prepare(client, t))
    assert import_result(client, h, files=[{"path": "baseline.txt", "content": "LOCAL EDIT"}]).status_code == 200
    target = Path(h["task_snapshot"]["task"]["worktree"]) / "baseline.txt"
    target.write_text("USER CHANGED AFTER IMPORT", "utf-8")
    assert review(client, h).status_code == 409
    assert target.read_text("utf-8") == "USER CHANGED AFTER IMPORT"


def test_summary_only_review_is_human_acceptance_not_automatic_execution(client):
    h = confirm(client, prepare(client, task(client, requires_skills=["analysis.requested"])) )
    assert import_result(client, h, files=[], test_reports=[]).status_code == 200
    response = review(client, h, approve_files=False)
    assert response.status_code == 200
    assert response.json()["handoff"]["artifacts"] == []
    assert response.json()["handoff"]["external_execution_verified"] is False


def test_hardlinked_destination_rejected_and_dependency_gate(client, tmp_path):
    h = confirm(client, prepare(client, task(client)))
    target = Path(h["task_snapshot"]["task"]["worktree"])
    source = target.parent / "local-outside-fixture.txt"
    source.write_text("NOT ALLOWED TO OVERWRITE", "utf-8")
    os.link(source, target / "link.txt")
    assert import_result(client, h, files=[{"path": "link.txt", "content": "bad"}]).status_code == 422
    assert source.read_text("utf-8") == "NOT ALLOWED TO OVERWRITE"
    parent = task(client)
    child = task(client, dependencies=[parent["id"]])
    assert client.post(f"/api/tasks/{child['id']}/handoff/prepare").status_code == 409
