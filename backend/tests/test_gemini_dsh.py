"""Independent model-free fixtures; no Gemini or DSH model is called."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time

import httpx
import pytest

from nexus.adapters import ExecutionContext
from nexus.bridge_protocol import BridgeError
from nexus.dsh_adapter import DSHAdapter
from nexus.gemini_adapter import GeminiAdapter


PLAN = {"summary": "LOCAL TEST FIXTURE; not a real Gemini response", "files": [{"path": "hello.txt", "content": "fixture"}], "approval_requests": []}


def context(root, timeout=2):
    return ExecutionContext("fixture-task", str(root), "LOCAL TEST FIXTURE ONLY", (), timeout_seconds=timeout)


def frame(text=None, finish=None, **extra):
    candidate = {"content": {"parts": [{"text": text}] if text is not None else []}}
    if finish:
        candidate["finishReason"] = finish
    return "data: " + json.dumps({"candidates": [candidate], **extra}) + "\n\n"


def configured(root, handler):
    adapter = GeminiAdapter(root, http_transport=httpx.MockTransport(handler))
    adapter.configure("LOCAL_TEST_FIXTURE_KEY", "gemini-fixture", True)
    return adapter


def test_gemini_never_treats_install_or_environment_as_authorization(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "must-not-be-used")
    monkeypatch.setenv("GOOGLE_API_KEY", "must-not-be-used")
    monkeypatch.setattr("nexus.gemini_adapter.shutil.which", lambda _: "fixture-gemini-cli")
    adapter = GeminiAdapter(tmp_path)
    assert asyncio.run(adapter.detect())["installed"]
    assert adapter.probe()["status"] == "disconnected" and not adapter.ready
    assert not adapter._api_key and adapter.probe()["capabilities"] == []
    async def run():
        with pytest.raises(BridgeError) as caught:
            [event async for event in adapter.execute(context(tmp_path))]
        assert caught.value.kind == "authorization_required"
    asyncio.run(run())


@pytest.mark.parametrize("key,model,approved", [("fixture", "gemini-fixture", False), ("", "gemini-fixture", True), ("a\nb", "gemini-fixture", True), ("fixture", "https://evil.example/", True), ("fixture", "../escape", True), ("gemini-secret", "gemini-secret", True)])
def test_gemini_configuration_rejects_unapproved_or_unsafe_inputs(tmp_path, key, model, approved):
    adapter = GeminiAdapter(tmp_path)
    with pytest.raises(ValueError):
        adapter.configure(key, model, approved)
    assert not adapter.ready


def test_gemini_disabled_instance_cannot_configure(tmp_path):
    with pytest.raises(ValueError):
        GeminiAdapter(tmp_path, enabled=False).configure("fixture", "gemini-fixture", True)


def test_gemini_stream_validates_real_protocol_and_does_not_write(tmp_path):
    requests = []
    payload = json.dumps(PLAN)
    def handler(request):
        requests.append(request)
        assert str(request.url) == "https://generativelanguage.googleapis.com/v1beta/models/gemini-fixture:streamGenerateContent?alt=sse"
        assert request.headers["x-goog-api-key"] == "LOCAL_TEST_FIXTURE_KEY"
        body = json.loads(request.content)
        assert body["tools"] == [] and body["generationConfig"]["responseMimeType"] == "application/json"
        assert body["generationConfig"]["responseJsonSchema"]["additionalProperties"] is False
        assert "api_key" not in request.url.query.decode()
        content = frame(payload[:20], responseId="LOCAL_FIXTURE_RESPONSE") + frame(payload[20:], "STOP", usageMetadata={"promptTokenCount": 7, "candidatesTokenCount": 4, "totalTokenCount": 11})
        return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})
    adapter = configured(tmp_path, handler)
    assert adapter.probe()["status"] == "disconnected"  # Configured is not verified.
    async def run():
        return [event async for event in adapter.execute(context(tmp_path))]
    events = asyncio.run(run())
    assert len(requests) == 1
    assert events[-1].kind == "completion" and events[-1].metadata["plan"] == PLAN
    assert events[-1].metadata["usage"] == {"input_tokens": 7, "output_tokens": 4, "total_tokens": 11}
    assert any(event.kind == "thread" for event in events)
    assert len([event for event in events if event.kind == "log"]) == 2
    assert adapter.probe()["status"] == "connected" and not adapter.active
    assert not (tmp_path / "hello.txt").exists()  # Only Nexus's validated writer may write.
    assert "LOCAL_TEST_FIXTURE_KEY" not in json.dumps(adapter.probe())
    assert not list(tmp_path.iterdir())  # Credentials and response state stay in memory.
    adapter.clear_configuration()
    assert not adapter.ready and adapter.probe()["status"] == "disconnected"


@pytest.mark.parametrize("status,kind", [(401, "authentication"), (403, "authentication"), (429, "rate_limit"), (402, "billing"), (503, "provider_unavailable"), (400, "execution"), (302, "execution")])
def test_gemini_http_errors_are_typed_and_never_expose_remote_secrets(tmp_path, status, kind):
    def handler(request):
        return httpx.Response(status, json={"error": {"message": "LOCAL_TEST_FIXTURE_KEY private-error"}}, headers={"location": "https://evil.example/"})
    adapter = configured(tmp_path, handler)
    async def run():
        with pytest.raises(BridgeError) as caught:
            [event async for event in adapter.execute(context(tmp_path))]
        assert caught.value.kind == kind
        assert "private-error" not in str(caught.value) and "LOCAL_TEST_FIXTURE_KEY" not in str(caught.value)
    asyncio.run(run())
    assert adapter.probe()["status"] == "disconnected" and not adapter.active


@pytest.mark.parametrize("data,kind", [
    ("data: invalid-json\n\n", "protocol"),
    (frame("[]", "STOP"), "protocol"),
    (frame(json.dumps(PLAN), "MAX_TOKENS"), "incomplete"),
    ("data: " + json.dumps({"promptFeedback": {"blockReason": "SAFETY"}}) + "\n\n", "content_blocked"),
    ("data: " + json.dumps({"candidates": [{"content": {"parts": [{"functionCall": {"name": "shell"}}]}, "finishReason": "STOP"}]}) + "\n\n", "policy_violation"),
])
def test_gemini_invalid_or_disallowed_responses_never_complete(tmp_path, data, kind):
    adapter = configured(tmp_path, lambda request: httpx.Response(200, text=data, headers={"content-type": "text/event-stream"}))
    async def run():
        with pytest.raises(BridgeError) as caught:
            [event async for event in adapter.execute(context(tmp_path))]
        assert caught.value.kind == kind
    asyncio.run(run())
    assert not adapter.verified_at and not adapter.active


def test_gemini_cancel_closes_owned_http_stream_and_does_not_guarantee_billing_stop(tmp_path):
    class WaitingStream(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            yield frame("{").encode()
            await asyncio.sleep(30)
        async def aclose(self):
            self.closed = True
    stream = WaitingStream()
    adapter = configured(tmp_path, lambda request: httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"}))
    async def run():
        async def collect():
            return [event async for event in adapter.execute(context(tmp_path))]
        task = asyncio.create_task(collect())
        while not adapter.active:
            await asyncio.sleep(0)
        with pytest.raises(ValueError):
            adapter.configure("replacement", "gemini-fixture", True)
        await adapter.cancel("fixture-task")
        assert task.cancelled()
    asyncio.run(run())
    assert stream.closed and not adapter.active and not adapter.verified_at
    assert "计费" in adapter.last_error


def test_gemini_timeout_cleans_connection(tmp_path):
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(30)
            yield b""
    adapter = configured(tmp_path, lambda request: httpx.Response(200, stream=SlowStream(), headers={"content-type": "text/event-stream"}))
    async def run():
        with pytest.raises(BridgeError) as caught:
            [event async for event in adapter.execute(context(tmp_path, timeout=0.01))]
        assert caught.value.kind == "timeout"
    asyncio.run(run())
    assert not adapter.active and not adapter.verified_at


@pytest.mark.parametrize("event", [
    [], {"promptFeedback": None}, {"responseId": "bad\nresponse"}, {"responseId": False},
    {"candidates": None}, {"candidates": [None]}, {"candidates": [{"content": None}]},
    {"candidates": [{"content": {"parts": "not-a-list"}}]},
    {"candidates": [{"finishReason": True}]},
    {"candidates": [{"content": {"parts": [{"text": None}]}}]},
    {"candidates": [{"content": {"parts": [{"text": "\ud800"}]}}]},
    {"candidates": [{"content": {"parts": [{"thought": "false", "text": "not-visible"}]}}]},
])
def test_gemini_malformed_types_revoke_previous_verification(tmp_path, event):
    body = "data: " + json.dumps(event) + "\n\n"
    adapter = configured(tmp_path, lambda request: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
    adapter.verified_at = datetime.now(timezone.utc).isoformat()
    assert adapter.probe()["status"] == "connected"
    async def run():
        with pytest.raises(BridgeError) as caught:
            [value async for value in adapter.execute(context(tmp_path))]
        assert caught.value.kind == "protocol"
    asyncio.run(run())
    assert adapter.probe()["status"] == "disconnected" and not adapter.active


@pytest.mark.parametrize("body", [
    b"data: " + b"x" * 1_200_001,
    b"data: \xff\n\n",
    ("data: " + "[" * 2000 + "0" + "]" * 2000 + "\n\n").encode(),
    frame(json.dumps({**PLAN, "summary": "\ud800"}), "STOP").encode(),
    (frame("{", responseId="FIRST_LOCAL_FIXTURE") + frame("}", "STOP", responseId="SECOND_LOCAL_FIXTURE")).encode(),
], ids=["oversized-line", "invalid-utf8", "deep-json", "plan-surrogate", "changed-response-id"])
def test_gemini_bounded_raw_utf8_and_deep_json_fail_safely(tmp_path, body):
    adapter = configured(tmp_path, lambda request: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"}))
    adapter.verified_at = datetime.now(timezone.utc).isoformat()
    async def run():
        with pytest.raises(BridgeError) as caught:
            [value async for value in adapter.execute(context(tmp_path))]
        assert caught.value.kind == "protocol"
    asyncio.run(run())
    assert not adapter.verified_at and not adapter.active
    assert "LOCAL_TEST_FIXTURE_KEY" not in adapter.last_error


@pytest.mark.parametrize("content_type", [None, "text/html", "application/json"])
def test_gemini_requires_actual_sse_content_type_before_started(tmp_path, content_type):
    headers = {"content-type": content_type} if content_type else {}
    adapter = configured(tmp_path, lambda request: httpx.Response(200, content=frame(json.dumps(PLAN), "STOP").encode(), headers=headers))
    seen = []
    async def run():
        with pytest.raises(BridgeError) as caught:
            async for event in adapter.execute(context(tmp_path)):
                seen.append(event)
        assert caught.value.kind == "protocol"
    asyncio.run(run())
    assert not seen and not adapter.verified_at


def test_gemini_drops_thought_text_and_keeps_unknown_usage_unknown(tmp_path):
    hidden = "PRIVATE HIDDEN REASONING LOCAL_TEST_FIXTURE_KEY"
    body = "data: " + json.dumps({"responseId": "LOCAL_FIXTURE_RESPONSE", "candidates": [{
        "content": {"parts": [{"thought": True, "text": hidden}, {"text": json.dumps(PLAN)}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": True, "candidatesTokenCount": False, "totalTokenCount": "unknown"}}) + "\n\n"
    adapter = configured(tmp_path, lambda request: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
    async def run():
        return [event async for event in adapter.execute(context(tmp_path))]
    events = asyncio.run(run())
    assert events[-1].kind == "completion" and events[-1].metadata["plan"] == PLAN
    assert events[-1].metadata["usage"] == {}
    assert len([event for event in events if event.kind == "log"]) == 1
    assert hidden not in repr(events) and "LOCAL_TEST_FIXTURE_KEY" not in repr(events)
    assert events[0].metadata["remote_confirmed"] is True


@pytest.mark.parametrize("error_type,kind", [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network"), (RuntimeError, "execution")])
def test_gemini_transport_failures_are_typed_sanitized_and_isolated(tmp_path, error_type, kind):
    def handler(request):
        raise error_type("LOCAL_TEST_FIXTURE_KEY must never appear in exported errors")
    adapter = configured(tmp_path, handler)
    adapter.verified_at = datetime.now(timezone.utc).isoformat()
    async def run():
        with pytest.raises(BridgeError) as caught:
            [event async for event in adapter.execute(context(tmp_path))]
        assert caught.value.kind == kind
        assert "LOCAL_TEST_FIXTURE_KEY" not in str(caught.value)
        assert caught.value.__suppress_context__ is True
    asyncio.run(run())
    assert not adapter.verified_at and not adapter.active


def test_dsh_discovery_only_uses_public_flags_and_scrubbed_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-be-inherited")
    monkeypatch.setenv("DSH_HOME", "private-client-home-must-not-be-used")
    monkeypatch.setattr("nexus.dsh_adapter.shutil.which", lambda name: "fixture-dsh")
    invocations = []
    def inspect(args, **kwargs):
        invocations.append(args)
        assert args[1] in ("--version", "--help") and len(args) == 2
        assert "DEEPSEEK_API_KEY" not in kwargs["env"]
        assert Path(kwargs["env"]["DSH_HOME"]).is_relative_to(tmp_path)
        text = "0.1.1-rc.2\n" if args[1] == "--version" else "DeepSeek Harness --profile"
        return subprocess.CompletedProcess(args, 0, stdout=text, stderr="")
    monkeypatch.setattr("nexus.dsh_adapter.subprocess.run", inspect)
    adapter = DSHAdapter(tmp_path)
    state = asyncio.run(adapter.detect())
    assert len(invocations) == 2 and state["installed"] and state["cli_version"] == "0.1.1-rc.2"
    assert state["status"] == "disconnected" and not state["ready"] and not state["verified_at"]
    # Discovery prepares the Nexus-owned restricted runtime, but a run still
    # needs an operator-provided credential that Nexus never reads or stores.
    assert state["restricted_runtime"]["ready"] is True
    assert state["restricted_runtime"]["home_scope"] == "project-local"
    assert any(item["code"] == "credential_required" for item in state["blockers"])
    async def run():
        with pytest.raises(BridgeError) as error:
            [event async for event in adapter.execute(context(tmp_path))]
        return error.value
    failure = asyncio.run(run())
    assert failure.kind == "not_connected" and not adapter.active
    asyncio.run(adapter.cancel("fixture-task"))


def test_dsh_detection_runs_both_probes_even_when_one_fails(tmp_path, monkeypatch):
    """A failing probe must not cancel its sibling mid-flight.

    `asyncio.gather` without ``return_exceptions`` resolves as soon as one task
    raises and cancels the rest, so `--help` could be skipped while the adapter
    still reported a detection result. Both public probes are now always run, and
    the failure is still surfaced as "not detected" rather than as a connection.
    """
    monkeypatch.setattr("nexus.dsh_adapter.shutil.which", lambda name: "fixture-dsh")
    started, finished = [], []
    preparations = []
    original_env = DSHAdapter.child_env

    def prepare_once(adapter):
        preparations.append(1)
        assert len(preparations) == 1, "shared runtime must be prepared before parallel probes"
        return original_env(adapter)

    monkeypatch.setattr(DSHAdapter, "child_env", prepare_once)

    def inspect(args, **kwargs):
        started.append(args[1])
        if args[1] == "--version":
            raise subprocess.TimeoutExpired(args, 15)
        time.sleep(0.05)  # a real CLI start; the sibling must not be cancelled here
        finished.append(args[1])
        return subprocess.CompletedProcess(args, 0, stdout="DeepSeek Harness --profile", stderr="")

    monkeypatch.setattr("nexus.dsh_adapter.subprocess.run", inspect)
    state = asyncio.run(DSHAdapter(tmp_path).detect())
    assert preparations == [1]
    assert sorted(started) == ["--help", "--version"]
    assert finished == ["--help"]
    assert state["status"] == "disconnected" and not state["ready"]
    assert state["cli_version"] is None


def test_dsh_restricted_patch_disables_tool_rows_and_stays_project_local(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("nexus.dsh_adapter.shutil.which", lambda name: "fixture-dsh")
    adapter = DSHAdapter(tmp_path)
    runtime = adapter.prepare_restricted_runtime()
    patch = Path(runtime["patch"])
    assert patch.is_relative_to(tmp_path) and patch.exists()
    text = patch.read_text(encoding="utf-8")
    for row in ("tool-pwsh", "tool-bash", "tool-web", "tool-workflow", "tool-subagent", "skill"):
        assert f"- id: {row}\n  disabled: true" in text
    assert "never edits a user profile" in text
    assert Path(runtime["home"]).is_relative_to(tmp_path) and Path(runtime["temp"]).is_relative_to(tmp_path)
    # The one-shot command carries the Nexus-owned patch instead of the user profile.
    command = adapter.command("fixture task")
    assert command[1] == "--patch" and command[2] == runtime["patch"]
    assert "--profile" in command and command[-1] == "fixture task"
    env = adapter.child_env()
    assert env["DSH_HOME"] == runtime["home"]
    assert "DEEPSEEK_API_KEY" not in env


def test_dsh_execution_requires_an_operator_credential_and_never_invents_one(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DSH_CREDENTIAL_SOURCE", raising=False)
    monkeypatch.setattr("nexus.dsh_adapter.shutil.which", lambda name: "fixture-dsh")
    adapter = DSHAdapter(tmp_path)
    adapter.launcher_detected, adapter.version = True, "0.1.1-rc.2"

    async def run():
        with pytest.raises(BridgeError) as error:
            [event async for event in adapter.execute(context(tmp_path))]
        return error.value
    error = asyncio.run(run())
    assert not adapter.ready and error.kind == "not_connected"
    assert "DEEPSEEK_API_KEY" in str(error)


def test_dsh_one_shot_output_parsing_accepts_only_a_validated_plan():
    plan = {"summary": "ok", "files": [{"path": "a.txt", "content": "x"}], "approval_requests": []}
    assert DSHAdapter._extract_plan(json.dumps(plan)) == plan
    assert DSHAdapter._extract_plan("```json\n" + json.dumps(plan) + "\n```") == plan
    assert DSHAdapter._extract_plan("prose before\n" + json.dumps(plan) + "\nprose after") == plan
    assert DSHAdapter._extract_plan("") is None
    assert DSHAdapter._extract_plan("no json here") is None
    assert DSHAdapter._extract_plan('{"summary": "x"}') is None
    assert DSHAdapter._extract_plan('{"summary":"x","files":[],"approval_requests":[],"extra":1}') is None


def test_dsh_probe_failures_are_local_and_do_not_mark_connected(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.dsh_adapter.shutil.which", lambda name: "fixture-dsh")
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired("fixture-dsh", 8)
    monkeypatch.setattr("nexus.dsh_adapter.subprocess.run", fail)
    adapter = DSHAdapter(tmp_path)
    state = asyncio.run(adapter.detect())
    assert state["status"] == "disconnected" and state["last_error"]
    assert not adapter.ready


def test_dsh_missing_and_disabled_do_not_launch_process(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.dsh_adapter.shutil.which", lambda name: None)
    def forbidden(*args, **kwargs):
        raise AssertionError("A missing or disabled adapter must not spawn anything")
    monkeypatch.setattr("nexus.dsh_adapter.subprocess.run", forbidden)
    assert not asyncio.run(DSHAdapter(tmp_path).detect())["installed"]
    monkeypatch.setattr("nexus.dsh_adapter.shutil.which", lambda name: "fixture-dsh")
    assert not asyncio.run(DSHAdapter(tmp_path, enabled=False).detect())["ready"]
