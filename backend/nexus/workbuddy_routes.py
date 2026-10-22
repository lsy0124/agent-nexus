"""Optional read-only OAuth onboarding; no desktop or cloud task execution."""
import secrets
import time

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, SecretStr

from .workbuddy_oauth import (DEFAULT_CALLBACK, WorkBuddyOAuth, WorkBuddyOAuthConfig,
                             WorkBuddyOAuthError)


class OAuthInput(BaseModel):
    client_id: str = Field(min_length=1, max_length=256)
    client_secret: SecretStr = Field(min_length=1, max_length=8192)
    application_enabled: bool = False
    approve_read_scope: bool = False


def install_routes(app, *, enabled, store):
    context = {}  # Process memory only; never part of a serialized state snapshot.
    cookie = "nexus_workbuddy_oauth"

    def status():
        session = context.get("session")
        return session.public_status() if session else {
            "oauth_authorized": False, "execution_enabled": False,
            "configured": "manager" in context, "callback": DEFAULT_CALLBACK,
            "required_scope": "user.localassistant.readable",
        }

    app.state.workbuddy_oauth_status = status

    @app.get("/api/agents/workbuddy/oauth/status")
    async def oauth_status():
        return status()

    @app.post("/api/agents/workbuddy/oauth/begin")
    async def oauth_begin(data: OAuthInput):
        if not enabled:
            raise HTTPException(403, "测试实例禁用真实账号授权")
        if not data.application_enabled or not data.approve_read_scope:
            raise HTTPException(403, "需要已启用的官方应用和明确只读范围授权；该 scope 也允许历史读取，本项目不调用历史接口")
        try:
            config = WorkBuddyOAuthConfig(data.client_id, DEFAULT_CALLBACK)
            manager = WorkBuddyOAuth(config)
            browser_session = secrets.token_urlsafe(32)
            authorization = manager.begin(browser_session)
        except (ValueError, WorkBuddyOAuthError):
            raise HTTPException(422, "官方应用配置无效；未发起网络请求") from None
        context.clear()
        context.update(manager=manager, secret=data.client_secret.get_secret_value(), expires_at=time.monotonic() + 300)
        from fastapi.responses import JSONResponse
        response = JSONResponse({"authorization_url": authorization.url, "expires_in": authorization.expires_in,
                                 "execution_enabled": False})
        response.set_cookie(cookie, browser_session, max_age=300, httponly=True, samesite="lax",
                            path="/api/agents/workbuddy/oauth")
        return response

    @app.get("/api/agents/workbuddy/oauth/callback")
    async def oauth_callback(request: Request):
        manager = context.get("manager")
        secret = context.pop("secret", None)
        # No query text is logged or returned. Always leave the sensitive URL.
        try:
            if (not enabled or manager is None or secret is None or request.query_params.get("error")
                    or time.monotonic() > context.get("expires_at", 0)):
                raise WorkBuddyOAuthError("Authorization not available")
            session = await manager.exchange(state=request.query_params.get("state", ""),
                code=request.query_params.get("code", ""), browser_session=request.cookies.get(cookie, ""),
                client_secret=secret)
            context.clear()
            context["session"] = session
            message = "WorkBuddy 官方 OAuth 只读授权完成；执行桥接仍未连接，未启动任何任务"
        except WorkBuddyOAuthError:
            context.clear()
            message = "WorkBuddy OAuth 未完成；请核对应用回调、scope 和启用状态后重新授权"
        with store.connect() as db:
            store.log(db, None, message, source="permission")
            store.event(db, "workbuddy.oauth.updated", status())
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(cookie, path="/api/agents/workbuddy/oauth")
        return response

    @app.post("/api/agents/workbuddy/oauth/clear")
    async def oauth_clear():
        context.clear()
        with store.connect() as db:
            store.event(db, "workbuddy.oauth.cleared", status())
        return status()
