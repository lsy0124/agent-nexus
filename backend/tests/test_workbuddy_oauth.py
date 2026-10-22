"""Read-only OAuth protocol tests using local HTTP fixtures, never real accounts."""
import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from nexus import workbuddy_oauth as oauth


def config(**kwargs):
    return oauth.WorkBuddyOAuthConfig("app_nonsecret_fixture", oauth.DEFAULT_CALLBACK, **kwargs)


def begin(service, browser="browser fixture"):
    request = service.begin(browser)
    return request, parse_qs(urlsplit(request.url).query)["state"][0]


def exchange(service, state, *, browser="browser fixture", handler=None):
    if handler is None:
        handler = lambda request: httpx.Response(200, json={
            "access_token": "LOCAL_ACCESS_FIXTURE", "refresh_token": "LOCAL_REFRESH_FIXTURE",
            "token_type": "Bearer", "expires_in": 3600, "scope": "user.localassistant.readable",
        })
    return asyncio.run(service.exchange(state=state, code="LOCAL_CODE_FIXTURE", browser_session=browser,
                                       client_secret="LOCAL_SECRET_FIXTURE", transport=httpx.MockTransport(handler)))


def test_begin_is_explicit_minimal_bound_state_with_no_network(monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: pytest.fail("No network allowed while preparing authorization"))
    service = oauth.WorkBuddyOAuth(config())
    request, state = begin(service)
    parsed = urlsplit(request.url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https" and parsed.netloc == "www.workbuddy.cn"
    assert query["scope"] == ["user.localassistant.readable"]
    assert query["redirect_uri"] == [oauth.DEFAULT_CALLBACK]
    assert query["response_type"] == ["code"] and len(state) >= 40
    assert "client_secret" not in query and "code_challenge" not in query
    assert state not in repr(request) and state not in repr(service._pending)
    assert "browser fixture" not in repr(service._pending)


@pytest.mark.parametrize("scope", [(), ("user.task.invokable",), ("user.localassistant.invokable",),
                                 ("user.task.readable",), ("user.contact.readable",),
                                 ("user.localassistant.readable", "user.localassistant.readable")])
def test_read_only_onboarding_cannot_enable_execution_or_expand_authority(scope):
    with pytest.raises(ValueError):
        config(scopes=scope)


@pytest.mark.parametrize("callback", ["https://external.example/callback", "http://localhost:8765/callback",
    "http://127.0.0.1/callback", "http://user@127.0.0.1:8765/callback", "http://127.0.0.1:8765/callback?code=x",
    "http://127.0.0.1:8765/callback#fragment", "file:///callback", "http://127.0.0.1:bad/callback",
    "http://127.0.0.1:8765/call\nback"])
def test_callback_stays_explicit_loopback_with_no_embedded_credentials(callback):
    with pytest.raises(ValueError):
        oauth.WorkBuddyOAuthConfig("app_fixture", callback)


def test_exchange_uses_only_fixed_official_endpoint_and_returns_no_public_secret():
    service = oauth.WorkBuddyOAuth(config())
    _, state = begin(service)
    seen = []

    def handler(request):
        seen.append(request)
        assert str(request.url) == oauth.TOKEN_URL
        assert request.method == "POST"
        fields = parse_qs(request.content.decode())
        assert fields["client_secret"] == ["LOCAL_SECRET_FIXTURE"]
        assert fields["redirect_uri"] == [oauth.DEFAULT_CALLBACK]
        assert fields["grant_type"] == ["authorization_code"]
        return httpx.Response(200, json={"access_token": "LOCAL_ACCESS_FIXTURE", "token_type": "Bearer",
                                        "expires_in": 3600, "scope": "user.localassistant.readable"})

    session = exchange(service, state, handler=handler)
    assert len(seen) == 1 and session.access_token == "LOCAL_ACCESS_FIXTURE"
    assert session.public_status()["execution_enabled"] is False
    assert "LOCAL_ACCESS_FIXTURE" not in json.dumps(session.public_status())
    assert "LOCAL_ACCESS_FIXTURE" not in repr(session)
    assert "LOCAL_SECRET_FIXTURE" not in repr(session)
    with pytest.raises(oauth.WorkBuddyOAuthError, match="already used"):
        exchange(service, state, handler=lambda request: pytest.fail("Code/state replay attempted network"))


def test_wrong_browser_cannot_redeem_a_state_or_consume_legitimate_callback():
    service = oauth.WorkBuddyOAuth(config())
    _, state = begin(service)
    with pytest.raises(oauth.WorkBuddyOAuthError, match="browser session"):
        exchange(service, state, browser="different browser", handler=lambda request: pytest.fail("Wrong browser made network call"))
    assert exchange(service, state).public_status()["oauth_authorized"]


def test_expired_state_rejected_before_exchange(monkeypatch):
    now = [1.0]
    monkeypatch.setattr(oauth.time, "monotonic", lambda: now[0])
    service = oauth.WorkBuddyOAuth(config(), state_ttl=5)
    _, state = begin(service)
    now[0] = 10
    with pytest.raises(oauth.WorkBuddyOAuthError, match="expired"):
        exchange(service, state, handler=lambda request: pytest.fail("Expired state made network call"))
    assert not service._pending


@pytest.mark.parametrize("status", [302, 400, 401, 429, 500])
def test_provider_error_or_redirect_never_exposes_secret_and_is_not_retried(status):
    service = oauth.WorkBuddyOAuth(config())
    _, state = begin(service)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://attacker.invalid/"},
                              text="LOCAL_SECRET_FIXTURE LOCAL_ACCESS_FIXTURE LOCAL_CODE_FIXTURE")

    with pytest.raises(oauth.WorkBuddyOAuthError) as error:
        exchange(service, state, handler=handler)
    assert len(calls) == 1
    assert "LOCAL_" not in str(error.value) and "attacker" not in str(error.value)
    assert not service._pending


@pytest.mark.parametrize("payload", [[], {}, {"token_type": "Bearer", "access_token": "fixture", "expires_in": -1, "scope": "user.localassistant.readable"},
    {"token_type": "Bearer", "access_token": "fixture", "expires_in": 3600, "scope": "user.task.invokable"},
    {"token_type": "Bearer", "access_token": "fixture", "expires_in": 3600, "scope": "user.localassistant.readable user.task.invokable"},
    {"token_type": "Bearer", "access_token": "fixture\nunsafe", "expires_in": 3600, "scope": "user.localassistant.readable"}])
def test_malformed_or_surplus_scope_response_rejected(payload):
    service = oauth.WorkBuddyOAuth(config())
    _, state = begin(service)
    with pytest.raises(oauth.WorkBuddyOAuthError, match="invalid or unexpected-scope"):
        exchange(service, state, handler=lambda request: httpx.Response(200, json=payload))


def test_network_failure_is_sanitized_and_not_retried():
    service = oauth.WorkBuddyOAuth(config())
    _, state = begin(service)
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("LOCAL_SECRET_FIXTURE", request=request)

    with pytest.raises(oauth.WorkBuddyOAuthError, match="official endpoint") as error:
        exchange(service, state, handler=handler)
    assert "LOCAL_" not in str(error.value) and len(calls) == 1
    assert not service._pending
