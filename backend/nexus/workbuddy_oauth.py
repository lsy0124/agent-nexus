"""Explicit, read-only WorkBuddy OAuth onboarding primitives.

This module is not a task execution bridge and is intentionally not wired into
startup. It never discovers credentials, reads environment secrets or persists
authorization codes/tokens. Only a user-approved server-side call may exchange a
code. The documented provider flow requires a confidential application's secret;
PKCE/public native clients are not claimed to be supported.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlencode, urlsplit

import httpx


AUTHORIZE_URL = "https://www.workbuddy.cn/openapi/v2/authorize"
TOKEN_URL = "https://www.workbuddy.cn/openapi/v2/token"
DEVELOPER_URL = "https://open.workbuddy.cn/"
DOCUMENTATION_URL = "https://open.workbuddy.cn/docs/openapi"
READ_ONLY_SCOPES = frozenset({"user.profile.readable", "user.localassistant.readable"})
DEFAULT_CALLBACK = "http://127.0.0.1:8765/api/agents/workbuddy/oauth/callback"


class WorkBuddyOAuthError(RuntimeError):
    """Safe diagnostic text; never include a provider response or secret value."""


@dataclass(frozen=True)
class WorkBuddyOAuthConfig:
    client_id: str
    redirect_uri: str
    scopes: tuple[str, ...] = ("user.localassistant.readable",)

    def __post_init__(self):
        if not isinstance(self.client_id, str) or not 1 <= len(self.client_id) <= 256:
            raise ValueError("WorkBuddy client_id is required")
        if any(ord(char) < 33 or ord(char) > 126 for char in self.client_id):
            raise ValueError("WorkBuddy client_id must contain visible ASCII characters")
        if (not isinstance(self.redirect_uri, str) or len(self.redirect_uri) > 2048
                or any(ord(char) < 33 for char in self.redirect_uri)):
            raise ValueError("A registered loopback callback is required")
        parsed = urlsplit(self.redirect_uri)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("Invalid loopback callback port") from None
        if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "::1"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not parsed.path.startswith("/") or not port):
            raise ValueError("OAuth callback must be a registered explicit-port loopback URL")
        if (not isinstance(self.scopes, tuple) or not self.scopes
                or any(not isinstance(scope, str) for scope in self.scopes)
                or len(self.scopes) != len(set(self.scopes))):
            raise ValueError("Explicit unique OAuth scopes are required")
        if any(scope not in READ_ONLY_SCOPES for scope in self.scopes):
            raise ValueError("Onboarding permits only the documented read-only scopes; task execution needs a separate design")


@dataclass(frozen=True)
class AuthorizationRequest:
    # This URL includes a CSRF nonce, so avoid logging or persisting it.
    url: str = field(repr=False)
    expires_in: int = 300


@dataclass(frozen=True)
class OAuthSession:
    """Keep on the backend in memory. Do not serialize or send to a browser."""
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    scopes: tuple[str, ...]
    expires_at: float

    def public_status(self) -> dict:
        return {"oauth_authorized": self.expires_at > time.time(), "scopes": list(self.scopes),
                "expires_at": self.expires_at, "execution_enabled": False}


class WorkBuddyOAuth:
    def __init__(self, config: WorkBuddyOAuthConfig, *, state_ttl: int = 300):
        if not 1 <= state_ttl <= 600:
            raise ValueError("OAuth state lifetime must be between 1 and 600 seconds")
        self.config = config
        self.state_ttl = state_ttl
        self._pending: dict[str, tuple[str, float]] = {}

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def begin(self, browser_session: str) -> AuthorizationRequest:
        """Build a URL after explicit user approval. No browser/network call is made."""
        if not isinstance(browser_session, str) or not browser_session:
            raise WorkBuddyOAuthError("A bound browser session is required")
        now = time.monotonic()
        self._pending = {key: entry for key, entry in self._pending.items() if entry[1] > now}
        if len(self._pending) >= 32:
            raise WorkBuddyOAuthError("Too many pending authorization requests")
        state = secrets.token_urlsafe(32)
        self._pending[self._digest(state)] = (self._digest(browser_session), now + self.state_ttl)
        query = urlencode({"response_type": "code", "client_id": self.config.client_id,
                           "redirect_uri": self.config.redirect_uri,
                           "scope": " ".join(self.config.scopes), "state": state})
        return AuthorizationRequest(AUTHORIZE_URL + "?" + query, self.state_ttl)

    async def exchange(self, *, state: str, code: str, browser_session: str,
                       client_secret: str, transport: httpx.AsyncBaseTransport | None = None) -> OAuthSession:
        """One exchange, no retries. The caller must already have user authorization.

        ``transport`` is dependency injection for independent no-network tests.
        No provider URL is configurable and redirects are never followed.
        """
        if not isinstance(client_secret, str) or not client_secret or len(client_secret) > 8192:
            raise WorkBuddyOAuthError("The registered application's server-side secret is required; do not paste it into chat")
        if (not isinstance(state, str) or len(state) > 256 or not isinstance(browser_session, str)
                or not browser_session or not isinstance(code, str) or not code or len(code) > 8192):
            raise WorkBuddyOAuthError("Invalid authorization callback")
        key = self._digest(state)
        pending = self._pending.get(key)
        if not pending or pending[1] <= time.monotonic():
            self._pending.pop(key, None)
            raise WorkBuddyOAuthError("Authorization state is missing, expired or already used")
        if not hmac.compare_digest(pending[0], self._digest(browser_session)):
            raise WorkBuddyOAuthError("Authorization callback does not match the initiating browser session")
        # Consume before sending: codes are single-use, and network errors must
        # not cause automatic replay or retry of an ambiguous exchange.
        self._pending.pop(key)
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False,
                                         transport=transport) as client:
                response = await client.post(TOKEN_URL, data={
                    "grant_type": "authorization_code", "code": code,
                    "client_id": self.config.client_id, "client_secret": client_secret,
                    "redirect_uri": self.config.redirect_uri,
                }, headers={"Accept": "application/json"})
        except httpx.HTTPError:
            raise WorkBuddyOAuthError("WorkBuddy authorization exchange could not reach the official endpoint; start a new authorization") from None
        if response.status_code != 200:
            # Provider error payloads can echo the request; never forward them.
            raise WorkBuddyOAuthError(f"WorkBuddy rejected the authorization exchange (HTTP {response.status_code}); start a new authorization")
        if len(response.content) > 65536:
            raise WorkBuddyOAuthError("WorkBuddy authorization response exceeds the allowed size")
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError
            access_token = payload.get("access_token")
            refresh_token = payload.get("refresh_token")
            expires_in = payload.get("expires_in")
            granted = payload.get("scope")
            if (payload.get("token_type", "").lower() != "bearer"
                    or not isinstance(access_token, str) or not access_token or len(access_token) > 16384
                    or any(ord(char) < 33 or ord(char) > 126 for char in access_token)
                    or (refresh_token is not None and (not isinstance(refresh_token, str) or not refresh_token
                        or len(refresh_token) > 16384 or any(ord(char) < 33 or ord(char) > 126 for char in refresh_token)))
                    or isinstance(expires_in, bool) or not isinstance(expires_in, (int, float))
                    or not 0 < expires_in <= 604800 or not isinstance(granted, str)):
                raise ValueError
            scopes = tuple(granted.split())
            if len(scopes) != len(set(scopes)) or set(scopes) != set(self.config.scopes):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise WorkBuddyOAuthError("WorkBuddy returned an invalid or unexpected-scope authorization response") from None
        return OAuthSession(access_token, refresh_token, scopes, time.time() + expires_in)
