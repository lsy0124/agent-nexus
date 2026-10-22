"""Gateway route behaviour, verified without any real model or network call.

A local MockTransport stands in for the provider. Passing fixtures prove the
implementation's boundaries only; they are not evidence of a real connection,
account balance or provider availability.
"""
import asyncio
import json
from pathlib import Path
import socket

import httpx
import pytest

from nexus.adapters import ExecutionContext, make_adapters
from nexus.bridge_protocol import BridgeError
from nexus.gateway_adapter import (
    BILLING_CLASS, EndpointRejected, GatewayRouteAdapter, GATEWAY_ROUTES,
    _is_local_only_address, assert_public_endpoint, deepseek_route, normalize_base_url, zcode_route,
)

SECRET = "sk-LOCAL-FIXTURE-NOT-A-REAL-KEY"

@pytest.fixture(autouse=True)
def isolate_streaming_fixture_observations():
    from nexus.usage import STREAMING_USAGE_VERIFIED
    previous = set(STREAMING_USAGE_VERIFIED)
    yield
    STREAMING_USAGE_VERIFIED.clear()
    STREAMING_USAGE_VERIFIED.update(previous)



def route_handler(*, models=("glm5.3",), plan=None, status=200, stream_body=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(status, json={"data": [{"id": name} for name in models]})
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "fixture"}})
        if stream_body is not None:
            return httpx.Response(200, content=stream_body)
        body = plan if plan is not None else {"summary": "LOCAL FIXTURE", "files": [], "approval_requests": []}
        message = {"content": body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)}
        return httpx.Response(200, json={"id": "LOCAL_FIXTURE_REQUEST", "model": "glm5.3",
                                         "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                                         "choices": [{"message": message, "finish_reason": "stop"}]})
    return handler


def adapter(agent="zcode-gateway", *, enabled=True, **kwargs):
    factory = zcode_route if agent == "zcode-gateway" else deepseek_route
    return factory(Path("."), enabled, **kwargs)


def configured(agent="zcode-gateway", *, handler=None, model=None, **kwargs):
    instance = adapter(agent, http_transport=httpx.MockTransport(handler or route_handler()), **kwargs)
    instance.configure(api_key=SECRET, model=model or GATEWAY_ROUTES[agent]["models"][0], approved=True)
    return instance


def drain(instance, objective="擦除测验"):
    async def collect():
        return [event async for event in instance.execute(ExecutionContext("t", ".", objective, ()))]
    return asyncio.run(collect())


# --------------------------------------------------------------- endpoint gate

def test_public_https_v1_endpoint_is_accepted():
    assert normalize_base_url("https://gateway.example.com/v1") == "https://gateway.example.com/v1"
    assert normalize_base_url("https://gateway.example.com/v1/") == "https://gateway.example.com/v1"
    assert normalize_base_url("https://gateway.example.com:8443/v1") == "https://gateway.example.com:8443/v1"
    # A bare origin normalizes to the conventional /v1 root rather than guessing a deeper path.
    assert normalize_base_url("https://gateway.example.com") == "https://gateway.example.com/v1"


@pytest.mark.parametrize("value", [
    "http://gateway.example.com/v1",              # plaintext
    "https://localhost/v1",                       # loopback name
    "https://user:pw@gateway.example.com/v1",     # credentials in URL
    "https://gateway.example.com/v1?key=abc",     # query string
    "https://gateway.example.com/v1#frag",        # fragment
    "not a url",
    "",
])
def test_unsafe_endpoint_syntax_is_rejected(value):
    with pytest.raises(EndpointRejected):
        normalize_base_url(value)


@pytest.mark.parametrize("value", [
    "https://127.0.0.1/v1",                       # loopback literal
    "https://10.0.0.5/v1",                        # private range
    "https://169.254.169.254/v1",                 # link-local metadata
    "https://[::1]/v1",                           # IPv6 loopback
])
def test_non_public_route_is_refused_before_a_key_is_stored(value):
    with pytest.raises(EndpointRejected):
        assert_public_endpoint(normalize_base_url(value), allow_unresolved=True)
    instance = adapter()
    with pytest.raises(EndpointRejected):
        instance.configure(api_key=SECRET, model="glm5.3", approved=True, base_url=value)
    # The refusal happens before the credential is retained.
    assert instance.ready is False and instance.probe()["credential_configured"] is False


def test_unresolved_host_is_fail_closed_unless_the_deployment_allows_it():
    host = "nexus-gateway-does-not-exist.invalid"
    try:
        socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        pass
    else:
        pytest.skip("this resolver answers every name; the offline branch cannot be exercised here")
    unresolved = normalize_base_url(f"https://{host}/v1")
    with pytest.raises(EndpointRejected):
        assert_public_endpoint(unresolved)
    assert assert_public_endpoint(unresolved, allow_unresolved=True) == unresolved


def test_a_resolver_that_maps_every_host_into_the_benchmarking_range_is_not_mistaken_for_private():
    # Fake-IP proxies resolve public names into 198.18.0.0/15. That range is not
    # a local target, so a legitimate gateway must stay usable, while a real
    # loopback/private literal is still refused.
    assert _is_local_only_address("198.18.0.167") is False
    assert _is_local_only_address("127.0.0.1") is True
    assert _is_local_only_address("10.1.2.3") is True
    assert _is_local_only_address("169.254.169.254") is True
    assert _is_local_only_address("192.168.0.1") is True
    assert _is_local_only_address("100.64.3.4") is True
    assert _is_local_only_address("::1") is True
    assert _is_local_only_address("8.8.8.8") is False


def test_private_target_cannot_receive_a_key_even_when_unresolved_routes_are_allowed():
    instance = adapter()
    instance.allow_unresolved_route = True
    with pytest.raises(EndpointRejected):
        instance.configure(api_key=SECRET, model="glm5.3", approved=True, base_url="https://192.168.1.10/v1")
    assert instance.probe()["credential_configured"] is False


# ------------------------------------------------------------------ permission

def test_configure_requires_explicit_cost_approval():
    instance = adapter()
    with pytest.raises(BridgeError) as error:
        instance.configure(api_key=SECRET, model="glm5.3", approved=False)
    assert error.value.kind == "approval_required"
    assert instance.ready is False and instance.probe()["status"] == "disconnected"


def test_disabled_instance_refuses_configuration_and_execution():
    instance = adapter(enabled=False)
    with pytest.raises(BridgeError):
        instance.configure(api_key=SECRET, model="glm5.3", approved=True)
    assert instance.ready is False
    with pytest.raises(BridgeError) as error:
        drain(instance)
    assert error.value.kind == "not_connected"
    assert instance.probe()["blockers"][0]["code"] == "execution_disabled"


def test_billing_class_is_metered_and_never_a_free_subscription():
    instance = configured()
    assert instance.billing == BILLING_CLASS == "metered-api"
    assert instance.probe()["billing"] == "metered-api"
    assert any("按量计费" in item for item in instance.probe()["limitations"])


def test_summary_line_always_agrees_with_the_blocker_list_it_renders():
    fresh = adapter()
    state = fresh.probe()
    assert state["detail"] == "；".join(item["detail"] for item in state["blockers"])
    assert "此实例禁用真实模型请求" not in state["detail"]
    disabled = adapter(enabled=False).probe()
    assert disabled["detail"] == "；".join(item["detail"] for item in disabled["blockers"])
    assert disabled["detail"].startswith("此实例禁用真实模型请求")
    verified = configured()
    drain(verified)
    connected = verified.probe()
    assert connected["status"] == "connected" and connected["detail"].startswith("真实请求已验证")


# -------------------------------------------------------------------- discovery

def test_model_discovery_reports_public_ids_only():
    instance = configured(handler=route_handler(models=("glm5.3", "glm-4.6")))
    assert asyncio.run(instance.discover_models()) == ["glm-4.6", "glm5.3"]
    assert instance.probe()["discovered_models"] == ["glm-4.6", "glm5.3"]


def test_model_discovery_does_not_leak_the_key_on_failure():
    instance = configured(handler=route_handler(status=401))
    with pytest.raises(BridgeError) as error:
        asyncio.run(instance.discover_models())
    assert error.value.kind == "authentication"
    assert SECRET not in json.dumps(instance.probe(), ensure_ascii=False)


# --------------------------------------------------------------------- execute

def test_execute_returns_validated_plan_and_never_claims_remote_tools():
    plan = {"summary": "LOCAL FIXTURE", "files": [{"path": "fixture.txt", "content": "hello"}], "approval_requests": []}
    instance = configured(handler=route_handler(plan=plan))
    events = drain(instance)
    kinds = [event.kind for event in events]
    assert kinds[0] == "started" and events[0].metadata["remote_confirmed"] is False
    assert kinds[-1] == "completion"
    completion = events[-1]
    assert completion.metadata["plan"]["files"] == [{"path": "fixture.txt", "content": "hello"}]
    # Usage carries its own provenance, so a later reader can tell a
    # provider-reported count from an estimate.
    assert completion.metadata["usage"] == {
        "input_tokens": 11, "output_tokens": 7, "total_tokens": 18,
        "usage_source": "provider-reported", "total_source": "provider"}
    assert completion.metadata["observed_usage"].source == "provider-reported"
    # The route states which Provider and catalog model served the request.
    assert completion.metadata["provider_id"] == instance.provider_id
    assert completion.metadata["model_key"] == instance.model_key
    assert isinstance(completion.metadata["latency_ms"], int)
    assert completion.metadata["thread_id"] == "LOCAL_FIXTURE_REQUEST"
    assert completion.metadata["exit_code"] == 0
    assert instance.verified_at is not None


def test_execute_rejects_tool_calls_instead_of_running_them():
    def handler(request):
        return httpx.Response(200, json={"id": "X", "choices": [{"message": {
            "content": "{}", "tool_calls": [{"id": "1", "function": {"name": "shell", "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}]})
    instance = configured(handler=handler)
    with pytest.raises(BridgeError) as error:
        drain(instance)
    assert error.value.kind == "policy_violation"
    assert instance.verified_at is None


def test_execute_rejects_truncated_and_non_json_results():
    truncated = configured(handler=route_handler(plan={"summary": "x", "files": []}))
    truncated.probe()  # smoke: probe stays safe
    def finish_handler(request):
        return httpx.Response(200, json={"id": "X", "choices": [{"message": {"content": "{}"}, "finish_reason": "length"}]})
    with pytest.raises(BridgeError) as error:
        drain(configured(handler=finish_handler))
    assert error.value.kind == "output_limit"

    with pytest.raises(BridgeError) as error:
        drain(configured(handler=route_handler(stream_body=b"not json at all")))
    assert error.value.kind == "protocol"


def test_execute_classifies_http_failures_without_echoing_the_key():
    for status, kind in ((401, "authentication"), (402, "billing"), (429, "rate_limit"), (503, "provider_unavailable")):
        instance = configured(handler=route_handler(status=status))
        with pytest.raises(BridgeError) as error:
            drain(instance)
        assert error.value.kind == kind
        assert SECRET not in str(error.value) and instance.verified_at is None


def test_execute_refuses_while_another_request_is_active():
    instance = configured()
    instance.active["other"] = object()
    with pytest.raises(BridgeError) as error:
        drain(instance)
    assert error.value.kind == "busy"


def test_execute_requires_connection_before_any_request():
    with pytest.raises(BridgeError) as error:
        drain(adapter())
    assert error.value.kind == "not_connected"


@pytest.mark.parametrize('part_type', ['text', 'output_text'])
def test_text_parts_are_validated_as_final_plan(part_type):
    plan = {'summary': 'LOCAL PARTS FIXTURE', 'files': [], 'approval_requests': []}
    encoded = json.dumps(plan)
    def handler(request):
        return httpx.Response(200, json={'choices': [{'message': {'content': [
            {'type': part_type, 'text': encoded[:20]}, {'type': part_type, 'text': encoded[20:]}]}, 'finish_reason': 'stop'}]})
    assert drain(configured(handler=handler))[-1].metadata['plan'] == plan


def test_reasoning_only_output_limit_is_not_empty_success_and_retains_reported_usage():
    def handler(request):
        return httpx.Response(200, json={'model': 'deepseek-fixture', 'usage': {'prompt_tokens': 3, 'completion_tokens': 8192, 'total_tokens': 8195},
            'choices': [{'message': {'content': None, 'reasoning_content': SECRET}, 'finish_reason': 'length'}]})
    instance = configured(handler=handler)
    with pytest.raises(BridgeError) as error:
        drain(instance)
    assert error.value.kind == 'output_limit'
    assert SECRET not in str(error.value)
    assert instance.last_observed_usage.total_tokens == 8195
    assert instance.last_attempt_usage['output_tokens'] == 8192
    assert instance.verified_at is None


def test_missing_final_text_does_not_promote_reasoning_or_refusal_to_a_file_plan():
    def handler(request):
        return httpx.Response(200, json={'choices': [{'message': {'content': None, 'reasoning_content': SECRET}, 'finish_reason': 'stop'}]})
    with pytest.raises(BridgeError) as error:
        drain(configured(handler=handler))
    assert error.value.kind == 'protocol' and 'reasoning_present=True' in str(error.value)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize('error_type', [httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError, httpx.ProxyError])
def test_network_classification_never_logs_exception_text_or_retries(error_type):
    calls = []
    def handler(request):
        calls.append(1)
        raise error_type('Authorization: Bearer ' + SECRET)
    instance = configured(handler=handler)
    with pytest.raises(BridgeError) as error:
        drain(instance)
    assert error.value.kind == 'network'
    assert error_type.__name__ in str(error.value)
    assert SECRET not in str(error.value) and 'Bearer' not in str(error.value)
    assert len(calls) == 1


def test_key_never_appears_in_probe_payload_or_logs():
    instance = configured()
    payload = json.dumps(instance.probe(), ensure_ascii=False)
    assert SECRET not in payload
    events = drain(instance)
    assert SECRET not in json.dumps([event.metadata for event in events], ensure_ascii=False, default=str)


def test_clearing_configuration_removes_the_key_and_verification():
    instance = configured()
    drain(instance)
    assert instance.verified_at is not None
    cleared = instance.clear_configuration()
    assert instance.ready is False and cleared["status"] == "disconnected"
    assert cleared["credential_configured"] is False and instance.verified_at is None


def test_cancel_targets_only_its_own_task():
    instance = configured()

    async def scenario():
        async def long_running():
            async for _ in instance.execute(ExecutionContext("t1", ".", "x", ())):
                await asyncio.sleep(5)
        task = asyncio.create_task(long_running())
        await asyncio.sleep(0)
        instance.active["t1"] = task
        await instance.cancel("t1")
        return task.cancelled() or task.done()
    assert asyncio.run(scenario()) is True
    assert asyncio.run(instance.cancel("unknown-task")) is None


# ------------------------------------------------------------------- registry

def test_both_gateway_routes_load_independently_and_share_one_family():
    adapters = make_adapters(Path("."), True)
    assert "zcode-gateway" in adapters and "deepseek-gateway" in adapters
    assert adapters["zcode-gateway"].id == "zcode-gateway"
    assert adapters["deepseek-gateway"].id == "deepseek-gateway"
    assert adapters["deepseek-gateway"].endpoint == "https://gonode.si/v1"
    assert adapters["zcode-gateway"].endpoint == "https://www.hyai666.top/v1"
    assert isinstance(adapters["zcode-gateway"], GatewayRouteAdapter)
    # Detection never opens a paid endpoint: both stay disconnected until a user
    # supplies a key and passes a real request.
    for agent_id in ("zcode-gateway", "deepseek-gateway"):
        assert adapters[agent_id].probe()["status"] == "disconnected"
        assert asyncio.run(adapters[agent_id].detect())["credential_configured"] is False


# Explicit SSE fixtures: no real Provider or billable call.
def stream_fixture(*, finish='stop', done=True, usage=True, tool=False):
    plan = json.dumps({'summary': 'LOCAL SSE FIXTURE', 'files': [{'path': 'hello.txt', 'content': 'LOCAL FIXTURE'}], 'approval_requests': []})
    events = [{'id': 'LOCAL_SSE', 'choices': [{'delta': {'reasoning_content': 'LOCAL REASONING NOT A PLAN'}, 'finish_reason': None}]}]
    events += [{'id': 'LOCAL_SSE', 'model': 'glm5.3', 'choices': [{'delta': {'tool_calls': [{}]} if tool else {'content': plan}, 'finish_reason': finish}]}]
    if usage:
        events += [{'id': 'LOCAL_SSE', 'choices': [], 'usage': {'prompt_tokens': 5, 'completion_tokens': 9, 'total_tokens': 14}}]
    return ''.join('data: ' + json.dumps(e) + '\n\n' for e in events) + ('data: [DONE]\n\n' if done else '')


def test_stream_request_progress_files_and_final_usage_are_real_fixture_fields():
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload['stream'] is True and 'stream_options' not in payload
        assert payload['max_tokens'] == 16384
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=stream_fixture())
    instance = configured(handler=handler)
    instance.stream_enabled, instance.max_output_tokens = True, 16384
    events = drain(instance)
    assert len(calls) == 1
    assert any('实时响应增量' in e.message for e in events)
    assert events[-1].metadata['plan']['files'][0]['content'] == 'LOCAL FIXTURE'
    assert events[-1].metadata['usage']['total_tokens'] == 14
    assert events[-1].metadata['observed_usage'].source == 'provider-reported'
    assert events[-1].metadata['streaming'] is True
    assert not any('LOCAL REASONING' in e.message for e in events)


@pytest.mark.parametrize('finish,done,tool,kind', [('length', True, False, 'output_limit'), ('stop', False, False, 'protocol'), ('stop', True, True, 'policy_violation')])
def test_stream_failures_never_retry_or_complete(finish, done, tool, kind):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={'content-type':'text/event-stream'}, text=stream_fixture(finish=finish, done=done, tool=tool))
    instance=configured(handler=handler); instance.stream_enabled=True
    with pytest.raises(BridgeError) as error:
        drain(instance)
    assert error.value.kind == kind
    assert len(calls)==1 and instance.verified_at is None
    if not tool:
        assert instance.last_attempt_usage['total_tokens']==14


def test_stream_without_final_usage_stays_unknown_and_json_response_needs_no_second_call():
    instance=configured(handler=lambda r: httpx.Response(200,headers={'content-type':'text/event-stream'},text=stream_fixture(usage=False)))
    instance.stream_enabled=True
    event=drain(instance)[-1]
    assert event.metadata['observed_usage'].source=='unavailable'
    assert event.metadata['usage'].get('input_tokens') is None and event.metadata['usage'].get('output_tokens') is None
    instance=configured(); instance.stream_enabled=True
    event=drain(instance)[-1]
    assert event.metadata['streaming'] is False


def test_gateway_config_limit_is_capped_by_task_budget():
    calls=[]
    def handler(request):
        calls.append(json.loads(request.content))
        return route_handler()(request)
    instance=configured(handler=handler);instance.max_output_tokens=32768
    async def execute():
        return [e async for e in instance.execute(ExecutionContext('bounded', '.', 'LOCAL', (), output_token_limit=500))]
    asyncio.run(execute())
    assert calls[0]['max_tokens']==500


@pytest.mark.parametrize('mode,thinking,effort',[('provider',None,None),('none',{'type':'disabled'},None),('low',{'type':'enabled'},'low')])
def test_explicit_reasoning_mode_uses_documented_fields(mode,thinking,effort):
    calls=[]
    def handler(request):
        calls.append(json.loads(request.content));return route_handler()(request)
    instance=configured(handler=handler);instance.reasoning_mode=mode
    drain(instance)
    assert calls[0].get('thinking')==thinking
    assert calls[0].get('reasoning_effort')==effort
    assert len(calls)==1


@pytest.mark.parametrize('agent',['zcode-gateway','deepseek-gateway'])
def test_large_sse_envelopes_are_not_large_file_plans(agent):
    plan=json.dumps({'summary':'LOCAL LARGE SSE FIXTURE','files':[{'path':'large.txt','content':'x'*9000}],'approval_requests':[]})
    events=[{'id':'LOCAL_LARGE_RESPONSE','model':GATEWAY_ROUTES[agent]['models'][0],'object':'chat.completion.chunk','created':1234567890,'choices':[{'delta':{'content':char},'finish_reason':None}]} for char in plan]
    events += [{'id':'LOCAL_LARGE_RESPONSE','choices':[{'delta':{},'finish_reason':'stop'}]}, {'choices':[],'usage':{'prompt_tokens':20,'completion_tokens':3000,'total_tokens':3020}}]
    body=''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n'
    assert len(body.encode())>1_200_000
    calls=[]
    def handler(request):
        calls.append(request);return httpx.Response(200,headers={'content-type':'text/event-stream'},text=body)
    instance=configured(agent,handler=handler);instance.stream_enabled=True
    completed=drain(instance)[-1]
    assert completed.metadata['plan']['files'][0]['content']=='x'*9000
    assert instance.last_stream_stats['wire_bytes']>1_200_000
    assert instance.last_stream_stats['content_bytes']<1_200_000
    assert completed.metadata['usage']['total_tokens']==3020
    assert len(calls)==1


def test_stream_request_id_survives_later_incomplete_response():
    instance=configured(handler=lambda r:httpx.Response(200,headers={'content-type':'text/event-stream'},text=stream_fixture(done=False)))
    instance.stream_enabled=True
    events=[]
    async def run():
        async for event in instance.execute(ExecutionContext('early-id','.','LOCAL',())):events.append(event)
    with pytest.raises(BridgeError):asyncio.run(run())
    assert any(e.kind=='thread' and e.metadata['thread_id']=='LOCAL_SSE' for e in events)
    assert instance.last_observed_usage.request_id=='LOCAL_SSE'
    assert instance.last_attempt_usage['total_tokens']==14


def test_untrusted_stream_model_cannot_echo_key_into_metadata():
    body=stream_fixture().replace('"model": "glm5.3"','"model": "'+SECRET+'"')
    instance=configured(handler=lambda r:httpx.Response(200,headers={'content-type':'text/event-stream'},text=body));instance.stream_enabled=True
    events=drain(instance)
    assert events[-1].metadata['reported_model'] is None
    assert SECRET not in json.dumps([{'message':e.message,'metadata':e.metadata} for e in events],default=str)


# Resolver changes must be rejected before sending a credential. Local fixtures only.
@pytest.mark.parametrize("addresses", [("8.8.8.8", "127.0.0.1"), ("8.8.8.8", "10.0.0.1"), ("8.8.8.8", "::1")])
def test_mixed_public_private_dns_answers_are_rejected(monkeypatch, addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, (ip, 443)) for ip in addresses])
    with pytest.raises(EndpointRejected):
        assert_public_endpoint("https://fixture.example/v1")


@pytest.mark.parametrize("operation", ["discover", "execute"])
def test_changed_dns_is_rechecked_before_sending_a_key(monkeypatch, operation):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("8.8.8.8", 443))])
    calls = []
    instance = configured(handler=lambda request: calls.append(request) or httpx.Response(200, json={}))
    instance.verified_at = "previous-verification"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("169.254.169.254", 443))])
    with pytest.raises(BridgeError) as error:
        asyncio.run(instance.discover_models()) if operation == "discover" else drain(instance)
    assert error.value.kind == "configuration"
    assert calls == [] and instance.verified_at is None and not instance.active
    assert SECRET not in str(error.value)
