"""Local protocol and process fixtures; ordinary tests never call Gemini."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from nexus.adapters import ExecutionContext
from nexus.bridge_protocol import BridgeError
from nexus.gemini_cli_adapter import GeminiCLIAdapter, literal_prompt


PLAN = {"summary": "LOCAL CLI FIXTURE, not real Gemini", "files": [{"path": "hello.txt", "content": "fixture"}], "approval_requests": []}


def context(root, text="LOCAL FIXTURE", timeout=5):
    return ExecutionContext("fixture-task", str(root), text, (), timeout_seconds=timeout)


def setup(root):
    adapter = GeminiCLIAdapter(root)
    adapter.cli.parent.mkdir(parents=True)
    adapter.cli.write_text("// LOCAL METADATA FIXTURE ONLY", encoding="utf-8")
    adapter.compatible, adapter.version = True, "0.64.0"
    adapter.configure_subscription(True)
    return adapter


def fixture_command(root, adapter, mode="ok"):
    script = root / f"fixture-gemini-{mode}.py"
    lines = ["import json,sys,time", "sys.stdin.read()", "print(json.dumps({'type':'init','session_id':'LOCAL_FIXTURE_SESSION'}),flush=True)"]
    if mode == "slow":
        lines.append("time.sleep(30)")
    elif mode == "tool":
        lines.append("print(json.dumps({'type':'tool_use','tool_name':'run_shell_command'}),flush=True)")
    elif mode == "auth":
        lines.extend(["print('authentication needed https://example.invalid/?token=NEVER_LOG_THIS',file=sys.stderr,flush=True)",
                      "print('    at LOCAL_TEST_FIXTURE_STACK_LINE',file=sys.stderr,flush=True)", "sys.exit(1)"])
    elif mode == "malformed":
        lines.append("print('invalid-json',flush=True)")
    else:
        text = json.dumps(PLAN) if mode != "invalid" else "[]"
        lines.append(f"print(json.dumps({{'type':'message','role':'assistant','content':{text!r},'delta':True}}),flush=True)")
        lines.append("print(json.dumps({'type':'result','status':'success','stats':{'input_tokens':5,'output_tokens':4,'total_tokens':9}}),flush=True)")
        if mode == "exit":
            lines.append("sys.exit(4)")
    script.write_text("\n".join(lines), encoding="utf-8")
    def command(ctx):
        cwd, env = adapter.prepare_runtime()
        return [sys.executable, str(script)], cwd, env
    adapter.command = command


def test_gemini_cli_uses_literal_json_without_any_at_file_or_slash_expansion(tmp_path):
    objective = '/custom !{Get-Content C:/secret} @C:/private.txt create name@example.com'
    ctx = ExecutionContext("fixture", str(tmp_path), objective, (), input_files=({"path": "safe.txt", "content": "@C:/another.txt"},))
    prompt = literal_prompt(ctx).decode()
    assert "@" not in prompt and not prompt.startswith("/")
    envelope = json.loads(prompt.split("Task data (JSON Unicode escapes are literal task text):\n", 1)[1])
    assert envelope["objective"] == objective
    assert envelope["input_files"][0]["content"] == "@C:/another.txt"


def test_gemini_cli_never_inherits_other_client_accounts_or_config(tmp_path, monkeypatch):
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "NODE_OPTIONS", "GEMINI_SYSTEM_MD", "HTTPS_PROXY", "GEMINI_CLI_HOME", "GEMINI_CLI_EXP_AGENT"):
        monkeypatch.setenv(name, "MUST_NOT_INHERIT")
    adapter = GeminiCLIAdapter(tmp_path)
    cwd, env = adapter.prepare_runtime()
    assert cwd.is_relative_to(tmp_path)
    assert "MUST_NOT_INHERIT" not in json.dumps(env)
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "NODE_OPTIONS", "GEMINI_SYSTEM_MD", "HTTPS_PROXY", "GEMINI_CLI_EXP_AGENT"):
        assert name not in env
    assert Path(env["GEMINI_CLI_HOME"]).is_relative_to(tmp_path)
    assert not Path(env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]).exists()
    assert not Path(env["GEMINI_CLI_SYSTEM_DEFAULTS_PATH"]).exists()
    settings = json.loads((Path(env["GEMINI_CLI_HOME"]) / ".gemini" / "settings.json").read_text(encoding="utf-8"))
    assert settings["tools"]["core"] == [] and not settings["hooksConfig"]["enabled"]
    assert settings["context"]["fileName"] == [] and settings["context"]["memoryBoundaryMarkers"] == []
    assert settings["advanced"]["ignoreLocalEnv"] and not settings["ide"]["enabled"]
    assert not settings["admin"]["mcp"]["enabled"] and not settings["admin"]["extensions"]["enabled"]
    assert not settings["skills"]["enabled"] and not settings["experimental"]["enableAgents"]
    assert settings["security"]["auth"]["enforcedType"] == "oauth-personal"
    assert settings["billing"]["overageStrategy"] == "never" and env["GEMINI_FORCE_FILE_STORAGE"] == "true"
    assert not settings["general"]["enableAutoUpdate"]


def test_gemini_cli_unexpected_system_override_file_blocks_launch(tmp_path):
    adapter = GeminiCLIAdapter(tmp_path)
    adapter.runtime.mkdir(parents=True)
    (adapter.runtime / "nexus-absent-system-settings.json").write_text('{"tools":{"core":["ShellTool"]}}', encoding="utf-8")
    with pytest.raises(BridgeError, match="意外存在"):
        adapter.prepare_runtime()


def test_gemini_cli_rejects_hardlinked_policy_file(tmp_path):
    adapter = GeminiCLIAdapter(tmp_path)
    adapter.prepare_runtime()
    policy = adapter.runtime / "home" / ".gemini" / "settings.json"
    original = policy.read_bytes()
    os.link(policy, tmp_path / "fixture-linked-policy.json")
    with pytest.raises(BridgeError, match="硬链接"):
        adapter.prepare_runtime()
    assert policy.read_bytes() == original


@pytest.mark.parametrize("approved,model", [(False, "auto"), (True, "https://evil.invalid"), (True, "../escape"), (True, None)])
def test_gemini_cli_configuration_needs_approval_and_valid_model(tmp_path, approved, model):
    adapter = GeminiCLIAdapter(tmp_path)
    with pytest.raises(ValueError):
        adapter.configure_subscription(approved, model)
    assert not adapter.ready
    assert adapter.probe()["status"] == "disconnected"


def test_gemini_cli_disabled_instance_cannot_configure_or_login(tmp_path):
    adapter = GeminiCLIAdapter(tmp_path, enabled=False)
    with pytest.raises(ValueError):
        adapter.configure_subscription(True)
    with pytest.raises(ValueError):
        adapter.login_spec()


def test_gemini_cli_detect_checks_only_public_flags_and_stays_disconnected(tmp_path, monkeypatch):
    adapter = setup(tmp_path)
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        assert args[-1] in ("--version", "--help")
        assert kwargs["cwd"].is_relative_to(tmp_path)
        assert Path(kwargs["env"]["GEMINI_CLI_HOME"]).is_relative_to(tmp_path)
        text = "0.64.0\n" if args[-1] == "--version" else "--output-format --prompt --approval-mode"
        return subprocess.CompletedProcess(args, 0, text, "")
    monkeypatch.setattr("nexus.gemini_cli_adapter.subprocess.run", run)
    state = asyncio.run(adapter.detect())
    assert len(calls) == 2 and state["cli_version"] == "0.64.0"
    assert state["ready"] and state["status"] == "disconnected" and not state["authenticated"]
    login = adapter.login_spec()
    assert login["argv"] == [adapter.node, str(adapter.cli)] and "--prompt" not in login["argv"]


def test_gemini_cli_wrong_version_and_discovery_failure_stay_unavailable(tmp_path, monkeypatch):
    adapter = setup(tmp_path)
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "0.65.0\n" if args[-1] == "--version" else "--output-format --prompt --approval-mode", "")
    monkeypatch.setattr("nexus.gemini_cli_adapter.subprocess.run", run)
    assert not asyncio.run(adapter.detect())["ready"]
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("local fixture", 15)
    monkeypatch.setattr("nexus.gemini_cli_adapter.subprocess.run", fail)
    assert asyncio.run(adapter.detect())["last_error"]


def test_gemini_cli_streams_protocol_and_leaves_writer_to_nexus(tmp_path):
    adapter = setup(tmp_path)
    fixture_command(tmp_path, adapter)
    async def run():
        return [event async for event in adapter.execute(context(tmp_path))]
    events = asyncio.run(run())
    assert events[0].kind == "started" and events[0].metadata["pid"] > 0
    assert events[-1].kind == "completion" and events[-1].metadata["plan"] == PLAN
    assert events[-1].metadata["usage"] == {"input_tokens": 5, "output_tokens": 4, "total_tokens": 9}
    assert events[-1].metadata["thread_id"] == "LOCAL_FIXTURE_SESSION"
    assert adapter.probe()["status"] == "connected" and not adapter.active
    assert not (tmp_path / "hello.txt").exists()
    adapter.clear_configuration()
    assert not adapter.ready and adapter.probe()["status"] == "disconnected"


@pytest.mark.parametrize("mode,kind", [("tool", "policy_violation"), ("auth", "authentication"), ("malformed", "protocol"), ("invalid", "protocol"), ("exit", "execution")])
def test_gemini_cli_failures_never_complete_or_expose_login_diagnostics(tmp_path, mode, kind):
    adapter = setup(tmp_path)
    fixture_command(tmp_path, adapter, mode)
    events = []
    async def run():
        with pytest.raises(BridgeError) as caught:
            async for event in adapter.execute(context(tmp_path)):
                events.append(event)
        assert caught.value.kind == kind
    asyncio.run(run())
    assert not adapter.active and not adapter.verified_at
    assert not any(event.kind == "completion" for event in events)
    assert "NEVER_LOG_THIS" not in json.dumps([event.__dict__ for event in events])
    assert "NEVER_LOG_THIS" not in json.dumps(adapter.probe())


def test_gemini_cli_timeout_and_cancel_terminate_owned_process(tmp_path):
    adapter = setup(tmp_path)
    fixture_command(tmp_path, adapter, "slow")
    async def timeout():
        with pytest.raises(BridgeError) as caught:
            [event async for event in adapter.execute(context(tmp_path, timeout=0.15))]
        assert caught.value.kind == "timeout"
    asyncio.run(timeout())
    assert not adapter.active
    async def cancel():
        async def collect():
            return [event async for event in adapter.execute(context(tmp_path))]
        task = asyncio.create_task(collect())
        while not adapter.active.get("fixture-task", (None, None))[0]:
            await asyncio.sleep(0.01)
        with pytest.raises(ValueError):
            adapter.clear_configuration()
        await adapter.cancel("fixture-task")
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], BridgeError)
    asyncio.run(cancel())
    assert not adapter.active and not adapter.verified_at


def test_gemini_cli_missing_or_not_approved_never_executes(tmp_path):
    adapter = GeminiCLIAdapter(tmp_path)
    async def run():
        with pytest.raises(BridgeError):
            [event async for event in adapter.execute(context(tmp_path))]
    asyncio.run(run())
    assert not adapter.active
