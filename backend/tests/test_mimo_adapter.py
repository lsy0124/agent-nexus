"""All MiMo responses here are LOCAL FIXTURES. No model or network is called."""
import asyncio
from datetime import datetime, timezone
import json
from dataclasses import replace

import httpx
import pytest

from nexus.adapters import ExecutionContext
from nexus.mimo_adapter import ENDPOINT, MIMOAdapter, MIMOBridgeError


KEY = "sk-LOCAL-TEST-FIXTURE-NOT-A-REAL-KEY"
PLAN = {"summary": "LOCAL TEST FIXTURE: proposed a file; no real model",
        "files": [{"path": "hello.txt", "content": "Hello Agent Nexus"}], "approval_requests": []}


def context(timeout=2):
    return ExecutionContext("mimo-local-fixture", "unused-no-filesystem-access", "create hello.txt", (), timeout)


def sse(plan=PLAN, *, finish="stop", done=True, usage=True):
    text = json.dumps(plan)
    midpoint = len(text) // 2
    events = [
        {"id": "LOCAL-TEST-FIXTURE-response", "choices": [{"delta": {"content": text[:midpoint]}, "finish_reason": None}]},
        {"id": "LOCAL-TEST-FIXTURE-response", "choices": [{"delta": {"content": text[midpoint:]}, "finish_reason": finish}]},
    ]
    if usage:
        events.append({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}})
    data = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    return data + ("data: [DONE]\n\n" if done else "")


def adapter(handler=None, **kwargs):
    handler = handler or (lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse()))
    return MIMOAdapter(api_key=KEY, approved=True, transport=httpx.MockTransport(handler), **kwargs)


async def collect(instance, execution_context=None):
    return [event async for event in instance.execute(execution_context or context())]


def test_discovery_never_reads_environment_keys_or_calls_network(monkeypatch):
    monkeypatch.setenv("MIMO_API_KEY", KEY)
    instance = MIMOAdapter(transport=httpx.MockTransport(lambda request: pytest.fail("Discovery must not call a model")))
    state = asyncio.run(instance.detect())
    assert state["status"] == "disconnected"
    assert state["ready"] is False
    assert state["verified_at"] is None
    assert state["capabilities"] == []
    assert state["installed"] is False and state["executable_detected"] is False
    assert state["adapter_available"] is True and state["requires_software_installation"] is False
    assert {blocker["code"] for blocker in state["blockers"]} == {"account_and_cost_approval", "explicit_api_key"}
    assert KEY not in json.dumps(state)
    with pytest.raises(MIMOBridgeError) as error:
        asyncio.run(collect(instance))
    assert error.value.kind == "not_connected"


def test_intermediate_sse_usage_is_not_final_reported_usage():
    intermediate = {'choices': [{'delta': {}, 'finish_reason': None}],
                    'usage': {'prompt_tokens': 999, 'completion_tokens': 999, 'total_tokens': 1998}}
    body = 'data: ' + json.dumps(intermediate) + '\n\n' + sse(usage=False)
    instance = adapter(lambda request: httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=body))
    completion = asyncio.run(collect(instance))[-1]
    assert completion.metadata['usage'] == {}
    assert completion.metadata['observed_usage'].source == 'unavailable'


@pytest.mark.parametrize('planning', [True, False])
def test_planning_requests_disable_documented_default_thinking_without_budget_increase(planning):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=sse())
    events = asyncio.run(collect(adapter(handler), replace(context(), planning_only=planning, output_token_limit=1024)))
    assert len(requests) == 1
    assert requests[0]['max_completion_tokens'] == 1024
    if planning:
        assert requests[0]['thinking'] == {'type': 'disabled'}
        assert any((event.metadata or {}).get('requested_reasoning') == 'none' for event in events)
    else:
        assert 'thinking' not in requests[0]
    assert events[-1].kind == 'completion'


@pytest.mark.parametrize("enabled,approved,key,model,kind", [
    (False, True, KEY, "mimo-v2.6-flash", "permission"),
    (True, False, KEY, "mimo-v2.6-flash", "approval_required"),
    (True, True, "bad\nkey", "mimo-v2.6-flash", "configuration"),
    (True, True, "", "mimo-v2.6-flash", "configuration"),
    (True, True, KEY, "https://arbitrary-endpoint.example/", "configuration"),
    (True, True, "mimo-secret-misplaced-key", "mimo-secret-misplaced-key", "configuration"),
])
def test_explicit_configuration_guards(enabled, approved, key, model, kind):
    instance = MIMOAdapter(enabled=enabled)
    with pytest.raises(MIMOBridgeError) as error:
        instance.configure(api_key=key, approved=approved, model=model)
    assert error.value.kind == kind
    assert instance.ready is False
    assert KEY not in str(error.value)


def test_official_request_stream_and_plan_contract(tmp_path, monkeypatch):
    requests = []
    monkeypatch.setenv("HTTP_PROXY", "http://must-not-use-environment-proxy.invalid")
    def handler(request):
        requests.append(request)
        assert str(request.url) == ENDPOINT
        assert request.headers["api-key"] == KEY
        payload = json.loads(request.content)
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["stream"] is True
        assert payload["model"] == "mimo-v2.6-flash"
        assert "tools" not in payload
        assert KEY not in request.content.decode()
        assert json.loads(payload["messages"][1]["content"])["input_files"] == [{"path": "src/a.txt", "content": "user-selected file"}]
        return httpx.Response(200, headers={"content-type": "text/event-stream; charset=utf-8"}, text=sse())
    instance = adapter(handler, root=tmp_path)
    assert instance.ready and instance.probe()["status"] == "disconnected"
    assert instance.probe()["capabilities"] == []
    assert instance.probe()["authenticated"] is False
    ctx = ExecutionContext("fixture", str(tmp_path), "create hello.txt", (), 2,
                           ({"path": "src/a.txt", "content": "user-selected file"},))
    events = asyncio.run(collect(instance, ctx))
    assert len(requests) == 1
    assert events[0].kind == "started"
    assert events[0].metadata["remote_confirmed"] is False
    assert any(event.metadata and event.metadata.get("remote_confirmed") for event in events if event.kind == "log")
    assert any(event.kind == "log" for event in events)
    assert any(event.kind == "thread" for event in events)
    result = events[-1]
    assert result.kind == "completion" and result.metadata["plan"] == PLAN
    assert result.metadata["usage"] == {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}
    assert result.metadata["exit_code"] == 0
    assert list(tmp_path.iterdir()) == []  # The adapter never writes generated files itself.
    assert instance.probe()["status"] == "connected"  # Fixture-only state in this isolated instance.
    assert KEY not in json.dumps(instance.probe())
    assert KEY not in repr(events)
    assert instance.active == {}
    instance.clear_configuration()
    assert instance.probe()["status"] == "disconnected" and not instance.ready
    assert instance.verified_at is None


@pytest.mark.parametrize("status,kind", [(301, "http_error"), (401, "authentication"), (403, "authentication"),
    (402, "billing"), (408, "timeout"), (429, "rate_limit"), (503, "provider_unavailable")])
def test_http_errors_are_classified_without_leaking_body_or_auth(status, kind):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="server error echo " + KEY,
                              headers={"location": "https://untrusted.invalid/key"})
    instance = adapter(handler)
    with pytest.raises(MIMOBridgeError) as error:
        asyncio.run(collect(instance))
    assert error.value.kind == kind
    assert len(calls) == 1  # No retries, follow-redirect or duplicate billable request.
    assert KEY not in str(error.value) and KEY not in json.dumps(instance.probe())
    assert instance.probe()["status"] == "disconnected" and instance.active == {}


@pytest.mark.parametrize("body", [
    sse(done=False), sse(finish="length"), sse({"summary": "wrong schema"}),
    "data: not-json\n\ndata: [DONE]\n\n",
    "data: []\n\ndata: [DONE]\n\n",
    'data: {"error":{"message":"sensitive upstream data"}}\n\ndata: [DONE]\n\n',
    'data: {"choices":[{"delta":{"tool_calls":[{}]}}]}\n\ndata: [DONE]\n\n',
    sse({**PLAN, "summary": KEY}),
    'data: {"choices":[{"delta":{"content":"\\ud800"}}]}\n\ndata: [DONE]\n\n',
    'data: {"choices":[{"delta":null}]}\n\ndata: [DONE]\n\n',
    'data: {"choices":[{"delta":{"content":[]}}]}\n\ndata: [DONE]\n\n',
    sse({**PLAN, "summary": "\ud800"}),
    "data: " + "[" * 2000 + "0" + "]" * 2000 + "\n\n",
])
def test_malformed_incomplete_or_tool_responses_fail_without_completion(body):
    instance = adapter(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body))
    instance.verified_at = datetime.now(timezone.utc).isoformat()  # A new protocol failure must revoke an older verification.
    assert instance.probe()["status"] == "connected"
    with pytest.raises(MIMOBridgeError):
        asyncio.run(collect(instance))
    assert instance.active == {} and instance.verified_at is None
    assert instance.probe()["status"] == "disconnected"
    assert KEY not in (instance.last_error or "")


def test_oversized_unterminated_sse_line_is_bounded_before_decoding():
    instance = adapter(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"},
        content=b"data: " + b"x" * 2_400_001))
    with pytest.raises(MIMOBridgeError) as error:
        asyncio.run(collect(instance))
    assert error.value.kind == "protocol" and instance.verified_at is None


def test_unexpected_transport_failure_is_isolated_and_sanitized():
    def handler(request):
        raise RuntimeError("malformed local transport " + KEY)
    instance = adapter(handler)
    instance.verified_at = datetime.now(timezone.utc).isoformat()
    with pytest.raises(MIMOBridgeError) as error:
        asyncio.run(collect(instance))
    assert error.value.kind == "execution" and instance.verified_at is None
    assert instance.active == {} and KEY not in str(error.value)


def test_missing_usage_remains_unknown():
    instance = adapter(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse(usage=False)))
    events = asyncio.run(collect(instance))
    assert events[-1].metadata["usage"] == {}


def test_timeout_is_real_async_timeout_not_fake_completion():
    async def handler(request):
        await asyncio.sleep(1)
        pytest.fail("Should be cancelled by timeout")
    instance = adapter(handler)
    with pytest.raises(MIMOBridgeError) as error:
        asyncio.run(collect(instance, context(0.01)))
    assert error.value.kind == "timeout"
    assert instance.active == {} and instance.verified_at is None


def test_cancellation_releases_stream_and_provider_slot():
    class OwnedStream(httpx.AsyncByteStream):
        def __init__(self):
            self.started, self.closed = asyncio.Event(), False
        async def __aiter__(self):
            self.started.set()
            await asyncio.sleep(10)
            yield b"data: [DONE]\n\n"
        async def aclose(self):
            self.closed = True
    async def scenario():
        stream = OwnedStream()
        instance = adapter(lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream))
        task = asyncio.create_task(collect(instance))
        await asyncio.wait_for(stream.started.wait(), 1)
        assert instance.probe()["busy"] is True
        with pytest.raises(MIMOBridgeError) as error:
            await collect(instance)
        assert error.value.kind == "busy"
        with pytest.raises(MIMOBridgeError):
            instance.configure(api_key=KEY, approved=True)
        with pytest.raises(MIMOBridgeError):
            instance.clear_configuration()
        await instance.cancel(context().task_id)
        assert task.cancelled() and stream.closed
        assert instance.active == {} and instance.probe()["status"] == "disconnected"
        await instance.cancel("not-an-owned-task")
    asyncio.run(scenario())


def test_network_exception_does_not_export_sensitive_exception_text():
    def handler(request):
        raise httpx.ConnectError("sensitive proxy URL " + KEY, request=request)
    instance = adapter(handler)
    with pytest.raises(MIMOBridgeError) as error:
        asyncio.run(collect(instance))
    assert error.value.kind == "network"
    assert KEY not in str(error.value) and KEY not in json.dumps(instance.probe())


def test_length_failure_preserves_final_usage_without_retry():
    instance = adapter(lambda request: httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=sse(finish='length')))
    with pytest.raises(MIMOBridgeError) as error:
        asyncio.run(collect(instance))
    assert error.value.kind == 'output_limit'
    assert instance.last_context_task_id == context().task_id
    assert instance.last_attempt_usage['input_tokens'] == 12
    assert instance.last_observed_usage.source == 'provider-reported'
    assert instance.last_latency_ms >= 0
