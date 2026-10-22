"""Onboarding integration fixtures; no real WorkBuddy account requests."""
import time
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from nexus.main import create_app
from nexus.adapters import make_adapters
from nexus.workbuddy_oauth import OAuthSession


def test_oauth_local_onboarding_callback_never_claims_agent_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_WORKBUDDY_OFFICIAL_API", "1")
    adapters = make_adapters(tmp_path, False)
    adapters["workbuddy"].enabled = True
    async def fixture_detect():
        return adapters["workbuddy"].probe()
    adapters["workbuddy"].detect = fixture_detect
    monkeypatch.setattr("nexus.main.make_adapters", lambda *a: adapters)
    async def fixture_exchange(self, **args):
        assert args["browser_session"] and args["client_secret"] == "LOCAL_FIXTURE_SECRET"
        return OAuthSession("LOCAL_ACCESS_FIXTURE", "LOCAL_REFRESH_FIXTURE", ("user.localassistant.readable",), time.time()+300)
    monkeypatch.setattr("nexus.workbuddy_oauth.WorkBuddyOAuth.exchange", fixture_exchange)
    app = create_app(tmp_path)
    with TestClient(app) as client:
        client.headers["X-Nexus-Token"] = app.state.token
        body = {"client_id": "LOCAL_APP_FIXTURE", "client_secret": "LOCAL_FIXTURE_SECRET", "application_enabled": True, "approve_read_scope": True}
        assert client.post("/api/agents/workbuddy/oauth/begin", json={**body, "approve_read_scope": False}).status_code == 403
        response = client.post("/api/agents/workbuddy/oauth/begin", json=body)
        assert response.status_code == 200 and "httponly" in response.headers["set-cookie"].lower()
        url = response.json()["authorization_url"]
        assert url.startswith("https://www.workbuddy.cn/openapi/v2/authorize?")
        assert "LOCAL_FIXTURE_SECRET" not in response.text
        state = parse_qs(urlsplit(url).query)["state"][0]
        response = client.get("/api/agents/workbuddy/oauth/callback", params={"state": state, "code": "LOCAL_CODE_FIXTURE"}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/"
        status = client.get("/api/agents/workbuddy/oauth/status").json()
        assert status["oauth_authorized"] and not status["execution_enabled"]
        state_text = client.get("/api/state").text
        assert all(secret not in state_text for secret in ("LOCAL_ACCESS_FIXTURE", "LOCAL_REFRESH_FIXTURE", "LOCAL_FIXTURE_SECRET", "LOCAL_CODE_FIXTURE"))
        buddy = next(a for a in client.get("/api/agents").json() if a["id"] == "workbuddy")
        assert buddy["status"] == "manual_handoff" and buddy["oauth"]["oauth_authorized"]
        assert client.post("/api/agents/workbuddy/oauth/clear").json()["oauth_authorized"] is False


def test_test_instance_never_starts_oauth_or_exchanges_invalid_callback(tmp_path):
    app = create_app(tmp_path, enable_codex=False)
    with TestClient(app) as client:
        client.headers["X-Nexus-Token"] = app.state.token
        assert client.post("/api/agents/workbuddy/oauth/begin", json={"client_id": "fixture", "client_secret": "secret", "application_enabled": True, "approve_read_scope": True}).status_code == 403
        response = client.get("/api/agents/workbuddy/oauth/callback?code=NEVER_LOG_THIS", follow_redirects=False)
        assert response.status_code == 303
        assert "NEVER_LOG_THIS" not in client.get("/api/state").text
