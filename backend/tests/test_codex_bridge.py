"""Protocol/process fixtures are local test doubles. Only codex-live.mjs calls real Codex."""
import asyncio
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from nexus.adapters import ExecutionContext
from nexus.codex_adapter import BridgeError, CodexAdapter, redact, validate_plan
from nexus.file_changes import apply_files, checked_path, context_files, relative_path, sha
from nexus.main import create_app
from nexus.process_tree import ProcessTree


@pytest.mark.parametrize("name", ["../escape.txt", "/outside.txt", "C:/outside.txt", "a\\b.txt", "a//b.txt", "a/../b.txt",
    "hello.txt:stream", "NUL.txt", "COM1.txt", "LPT3.md", "bad. /x.txt", ".git/config.txt", ".env", ".env.local",
    "private.key", "credentials.json", "auth.json", "cookies.txt", "node_modules/test.js", "install.ps1", "start.exe"])
def test_file_path_policy_rejects_unsafe_paths(name):
    with pytest.raises(ValueError):
        relative_path(name)


def test_real_file_writer_new_file_overwrite_context_and_stale_hash(tmp_path):
    artifacts = apply_files(tmp_path, [{"path": "src/hello.txt", "content": "Hello Agent Nexus"}], {})
    assert (tmp_path / "src/hello.txt").read_bytes() == b"Hello Agent Nexus"
    assert artifacts[0]["sha256"] == sha(b"Hello Agent Nexus")
    assert artifacts[0]["written_by"] == "nexus-validated-writer"
    with pytest.raises(ValueError, match="输入"):
        apply_files(tmp_path, [{"path": "src/hello.txt", "content": "overwrite"}], {})
    inputs, hashes = context_files(tmp_path, ["src/hello.txt"])
    assert inputs[0]["content"] == "Hello Agent Nexus"
    apply_files(tmp_path, [{"path": "src/hello.txt", "content": "updated"}], hashes)
    with pytest.raises(ValueError):
        apply_files(tmp_path, [{"path": "src/hello.txt", "content": "stale"}], hashes)


def test_complete_batch_validation_prevents_partial_writes(tmp_path):
    with pytest.raises(ValueError):
        apply_files(tmp_path, [{"path": "ok.txt", "content": "safe"}, {"path": "../bad.txt", "content": "bad"}], {})
    assert not (tmp_path / "ok.txt").exists()
    with pytest.raises(ValueError):
        apply_files(tmp_path, [{"path": "same.txt", "content": "a"}, {"path": "SAME.txt", "content": "b"}], {})
    assert not (tmp_path / "same.txt").exists()


def test_writer_rolls_back_owned_batch_on_failure(tmp_path, monkeypatch):
    real_fsync, count = os.fsync, 0

    def failing_fsync(fd):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("fixture disk failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(OSError):
        apply_files(tmp_path, [{"path": "a.txt", "content": "a"}, {"path": "nested/b.txt", "content": "b"}], {})
    assert not (tmp_path / "a.txt").exists() and not (tmp_path / "nested").exists()


def test_existing_hardlink_is_not_read_or_overwritten(tmp_path):
    outside = tmp_path / "outside"
    target = tmp_path / "target"
    outside.mkdir(); target.mkdir()
    (outside / "private.txt").write_text("not allowed", encoding="utf-8")
    os.link(outside / "private.txt", target / "alias.txt")
    with pytest.raises(ValueError, match="硬链接"):
        checked_path(target, "alias.txt")
    assert (outside / "private.txt").read_text(encoding="utf-8") == "not allowed"


@pytest.mark.parametrize("value", ["not json", "[]", '{"summary":"ok"}', '{"summary":"ok","files":[{"path":"a.txt","content":4}],"approval_requests":[]}'])
def test_invalid_result_never_becomes_completion(value):
    with pytest.raises(BridgeError, match="结构化"):
        validate_plan(value)


def test_no_credential_values_in_logs():
    assert "sk-test" not in redact('Bearer sk-test123 access_token=private-token api_key=private-key')
    assert "private-token" not in redact('access_token=private-token')


def fake_cli(root, plan=None, mode="ok"):
    script = root / ("fixture_cli_" + mode + ".py")
    result = json.dumps(plan or {"summary": "LOCAL TEST FIXTURE, not a real Codex result", "files": [{"path": "hello.txt", "content": "fixture"}], "approval_requests": []})
    lines = ["import json,sys,time", "sys.stdin.read()",
             "print(json.dumps({'type':'thread.started','thread_id':'LOCAL_TEST_FIXTURE'}),flush=True)"]
    if mode == "slow":
        lines.append("time.sleep(30)")
    elif mode == "tool":
        lines.append("print(json.dumps({'type':'item.started','item':{'type':'command_execution'}}),flush=True)")
    else:
        lines += [f"print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':{result!r}}}}}),flush=True)"]
        if mode != "missing_turn":
            lines.append("print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}),flush=True)")
        if mode == "nonzero":
            lines.append("sys.exit(7)")
    script.write_text("\n".join(lines), encoding="utf-8")
    return [sys.executable, str(script)]


def configure_fixture(adapter, root, plan=None, mode="ok"):
    adapter.enabled, adapter.binary, adapter.version = True, sys.executable, "LOCAL TEST FIXTURE (not Codex)"
    adapter.authenticated, adapter.compatible = True, True
    command = fake_cli(root, plan, mode)
    adapter.command = lambda context: command

    async def fixture_detect():
        return adapter.probe()

    adapter.detect = fixture_detect


@pytest.mark.parametrize("mode,kind", [("nonzero", "execution"), ("missing_turn", "execution"), ("tool", "policy_violation"), ("slow", "timeout")])
def test_cli_failure_timeout_and_unexpected_tools_fail_closed(tmp_path, mode, kind):
    adapter = CodexAdapter(tmp_path)
    configure_fixture(adapter, tmp_path, mode=mode)

    async def run():
        context = ExecutionContext("fixture", str(tmp_path), "test", (), 0.4 if mode == "slow" else 10)
        with pytest.raises(BridgeError) as raised:
            async for event in adapter.execute(context):
                assert event.kind != "completion"
        assert raised.value.kind == kind
        assert not adapter.active and adapter.probe()["status"] == "disconnected"
    asyncio.run(run())


def test_cli_command_is_parameterized_and_disables_execution(tmp_path):
    adapter = CodexAdapter(tmp_path)
    command = adapter.command(ExecutionContext("fixture", str(tmp_path), '$(danger) --bypass', ()))
    joined = " ".join(str(x) for x in command)
    assert "$(danger)" not in joined
    for required in ("--ignore-user-config", "--ephemeral", "--json", "--output-schema", "features.shell_tool=false", 'default_permissions="nexus-read"'):
        assert required in joined
    assert "danger-full-access" not in joined and "--dangerously" not in joined
    assert not any("KEY" in key.upper() or "TOKEN" in key.upper() for key in adapter.child_env())


def test_unreviewed_cli_upgrade_cannot_silently_enable_execution(tmp_path, monkeypatch):
    # An explicit path keeps this gate test independent of whether this machine
    # currently has an official Codex CLI installed; production still resolves
    # the real CLI from PATH.
    adapter = CodexAdapter(tmp_path, executable=str(tmp_path / "codex-fixture.exe"))

    def inspected(command, **kwargs):
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, "codex-cli 99.0.0", "")
        if "status" in command:
            return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT", "")
        return subprocess.CompletedProcess(command, 0, "--json --ephemeral --output-schema --ignore-user-config", "")

    monkeypatch.setattr(subprocess, "run", inspected)
    asyncio.run(adapter.detect())
    assert adapter.authenticated and not adapter.compatible
    assert not adapter.ready and adapter.probe()["status"] == "disconnected"


def init_repo(root):
    for args in (["init", "-q"],):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "baseline.txt").write_text("baseline", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "baseline.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Local Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "fixture baseline"], check=True, capture_output=True)


def wait_task(client, task_id):
    for _ in range(150):
        task = next(t for t in client.get("/api/tasks").json() if t["id"] == task_id)
        if task["status"] != "running":
            return task
        time.sleep(0.03)
    pytest.fail("local protocol fixture timed out")


@pytest.mark.parametrize("mode", ["success", "approval", "escape"])
def test_api_dispatch_stream_result_and_safe_artifacts_with_local_fixture(tmp_path, mode):
    init_repo(tmp_path)
    app = create_app(tmp_path, enable_codex=False)
    with TestClient(app) as client:
        client.headers["X-Nexus-Token"] = app.state.token
        plan = {"summary": "LOCAL PROTOCOL FIXTURE", "files": [{"path": "../escape.txt" if mode == "escape" else "hello.txt", "content": "fixture"}], "approval_requests": ["global install requires approval"] if mode == "approval" else []}
        configure_fixture(app.state.adapters["codex"], tmp_path, plan)
        response = client.post("/api/tasks", json={"title": "LOCAL FIXTURE TASK (not real Codex)", "auto_run": True})
        assert response.status_code == 201
        task = wait_task(client, response.json()["id"])
        attempts = client.get(f"/api/tasks/{task['id']}/attempts").json()
        assert attempts[0]["cli_version"].startswith("LOCAL TEST FIXTURE")
        target = Path(task["worktree"])
        if mode == "success":
            assert task["status"] == "done"
            assert attempts[0]["exit_code"] == 0 and attempts[0]["thread_id"] == "LOCAL_TEST_FIXTURE"
            assert (target / "hello.txt").read_bytes() == b"fixture"
            assert client.get(f"/api/tasks/{task['id']}/artifacts/hello.txt").content == b"fixture"
            (target / "hello.txt").write_text("changed", encoding="utf-8")
            assert client.get(f"/api/tasks/{task['id']}/artifacts/hello.txt").status_code == 409
        elif mode == "approval":
            assert task["status"] == "blocked" and attempts[0]["error_kind"] == "approval_required"
            assert not (target / "hello.txt").exists()
        else:
            assert task["status"] == "failed" and attempts[0]["error_kind"] == "permission"
            assert not (target.parent / "escape.txt").exists()


def test_api_cancel_reaps_cli_process_before_returning(tmp_path):
    init_repo(tmp_path)
    app = create_app(tmp_path, enable_codex=False)
    with TestClient(app) as client:
        client.headers["X-Nexus-Token"] = app.state.token
        adapter = app.state.adapters["codex"]
        configure_fixture(adapter, tmp_path, mode="slow")
        task = client.post("/api/tasks", json={"title": "LOCAL CANCELLATION FIXTURE", "auto_run": True}).json()
        process = None
        for _ in range(100):
            process = adapter.active.get(task["id"], (None, None))[0]
            if process:
                break
            time.sleep(0.02)
        assert process is not None
        response = client.post(f"/api/tasks/{task['id']}/cancel")
        assert response.status_code == 200 and response.json()["status"] == "cancelled"
        assert process.returncode is not None and not adapter.active
        attempt = client.get(f"/api/tasks/{task['id']}/attempts").json()[0]
        assert attempt["status"] == "cancelled" and not attempt["artifacts"]
        assert not (Path(task["worktree"]) / "hello.txt").exists()


def test_existing_selected_input_file_can_be_edited(tmp_path):
    init_repo(tmp_path)
    app = create_app(tmp_path, enable_codex=False)
    with TestClient(app) as client:
        client.headers["X-Nexus-Token"] = app.state.token
        configure_fixture(app.state.adapters["codex"], tmp_path,
                          {"summary": "LOCAL EDIT FIXTURE", "files": [{"path": "baseline.txt", "content": "updated fixture"}], "approval_requests": []})
        task = client.post("/api/tasks", json={"title": "LOCAL INPUT EDIT FIXTURE", "input_files": ["baseline.txt"], "auto_run": True}).json()
        done = wait_task(client, task["id"])
        assert done["status"] == "done"
        assert (Path(done["worktree"]) / "baseline.txt").read_text(encoding="utf-8") == "updated fixture"
        assert (tmp_path / "baseline.txt").read_text(encoding="utf-8") == "baseline"


@pytest.mark.parametrize("correct", [True, False])
def test_connection_test_requires_nonce_and_no_files(tmp_path, correct):
    init_repo(tmp_path)
    app = create_app(tmp_path, enable_codex=False)
    with TestClient(app) as client:
        client.headers["X-Nexus-Token"] = app.state.token
        adapter = app.state.adapters["codex"]
        configure_fixture(adapter, tmp_path)
        script = tmp_path / "fixture_connection.py"
        script.write_text("import json,re,sys\ns=sys.stdin.read()\nn=re.search(r'NEXUS_[a-f0-9]{24}',s).group(0)\n" +
                          ("n='wrong nonce'\n" if not correct else "") +
                          "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps({'summary':n,'files':[],'approval_requests':[]})}}),flush=True)\n"
                          "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n", encoding="utf-8")
        adapter.command = lambda context: [sys.executable, str(script)]

        async def fixture_detect():
            return adapter.probe()

        adapter.detect = fixture_detect
        response = client.post("/api/agents/codex/test")
        assert response.status_code == 202
        task = wait_task(client, response.json()["task"]["id"])
        assert task["status"] == ("done" if correct else "failed")
        assert adapter.probe()["status"] == ("connected" if correct else "disconnected")


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object enforcement")
def test_windows_job_object_reaps_grandchild(tmp_path):
    code = "import sys,time,subprocess;sys.stdin.readline();p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);print(p.pid,flush=True);time.sleep(30)"
    process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    tree = ProcessTree(process.pid)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    child = None
    try:
        process.stdin.write("start\n"); process.stdin.flush()
        child_pid = int(process.stdout.readline().strip())
        child = kernel.OpenProcess(0x00100000, False, child_pid)
        assert child
        tree.close()
        process.wait(timeout=5)
        assert kernel.WaitForSingleObject(child, 5000) == 0
    finally:
        tree.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if child:
            kernel.CloseHandle(child)
