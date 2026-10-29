"""访问口令保护（部署到公网时必须开启）。

- 口令存在 config/.env 的 ITERWAVE_ACCESS_TOKEN（不进配置文件、不进日志）
- 设置口令后，所有 /api/*（除 /api/auth/*）都要求登录：
  浏览器走 HttpOnly Cookie；脚本 / 监控可用 `Authorization: Bearer <口令>`
- 未设置口令时为开放模式：接口照常可用，页面顶部持续警告，并允许在页面上设置口令
  （设置后立即生效，之后只能凭旧口令修改）
- 登录失败按 IP 限速，防止暴力猜测
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import threading
import time
from collections import deque

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

TOKEN_ENV = "ITERWAVE_ACCESS_TOKEN"
COOKIE_NAME = "iw_session"
COOKIE_MAX_AGE = 30 * 24 * 3600
MIN_TOKEN_LEN = 8

_OPEN_PREFIXES = ("/api/auth/",)
_fail_lock = threading.Lock()
_fails: dict[str, deque] = {}
MAX_FAILS_PER_10MIN = 10


def current_token() -> str:
    return (os.environ.get(TOKEN_ENV) or "").strip()


def auth_required() -> bool:
    return bool(current_token())


def _session_value(token: str) -> str:
    return hmac.new(token.encode(), b"iterwave-session-v1", hashlib.sha256).hexdigest()


def is_authed(request: Request) -> bool:
    token = current_token()
    if not token:
        return True
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        if hmac.compare_digest(auth[7:].strip(), token):
            return True
    cookie = request.cookies.get(COOKIE_NAME, "")
    return bool(cookie) and hmac.compare_digest(cookie, _session_value(token))


def _client_ip(request: Request) -> str:
    # 不直接信任客户端可伪造的 X-Forwarded-For；需要代理链时应由受信任代理层配置。
    return request.client.host if request.client else "unknown"


def _is_trusted_local_setup(request: Request) -> bool:
    # localhost 只能以 TCP 对端地址判定，不能信任 Host 头；
    # 反向代理传入转发头时按远程请求处理，即使代理本身监听在 loopback。
    if any(request.headers.get(name) for name in (
        "forwarded", "x-forwarded-for", "x-real-ip", "x-forwarded-proto"
    )):
        return False
    peer = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return False


def _too_many_fails(ip: str) -> bool:
    now = time.time()
    with _fail_lock:
        q = _fails.setdefault(ip, deque())
        while q and now - q[0] > 600:
            q.popleft()
        return len(q) >= MAX_FAILS_PER_10MIN


def _record_fail(ip: str) -> None:
    with _fail_lock:
        _fails.setdefault(ip, deque()).append(time.time())


def _set_cookie(resp: Response, token: str, request: Request) -> None:
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(COOKIE_NAME, _session_value(token), max_age=COOKIE_MAX_AGE,
                    httponly=True, samesite="lax", secure=secure, path="/")


class LoginRequest(BaseModel):
    token: str


class SetupRequest(BaseModel):
    token: str
    old_token: str | None = None
    bootstrap_token: str | None = None


def install_auth(app: FastAPI) -> None:
    @app.middleware("http")
    async def access_guard(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or request.method == "OPTIONS":
            return await call_next(request)

        if not current_token():
            # 首次初始化时关闭业务 API，只开放状态查询与设置口令；不能因为尚未设密而匿名管理平台。
            if path not in ("/api/auth/status", "/api/auth/setup", "/api/auth/login"):
                return JSONResponse(
                    {"detail": "请先完成访问口令设置", "code": "setup_required"},
                    status_code=403,
                )
            return await call_next(request)

        if path.startswith(_OPEN_PREFIXES):
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        has_bearer = auth_header.lower().startswith("bearer ")
        ip = _client_ip(request)
        if has_bearer and _too_many_fails(ip):
            return JSONResponse(
                {"detail": "尝试次数过多，请 10 分钟后再试", "auth_required": True},
                status_code=429,
            )
        if not is_authed(request):
            # 脚本 Bearer 认证也纳入失败计数，避免绕开 /login 暴力猜口令。
            if has_bearer:
                _record_fail(ip)
            return JSONResponse({"detail": "需要登录", "auth_required": True}, status_code=401)
        return await call_next(request)

    @app.get("/api/auth/status")
    def auth_status(request: Request):
        return {"required": auth_required(), "authed": is_authed(request)}

    @app.post("/api/auth/login")
    def auth_login(req: LoginRequest, request: Request, response: Response):
        token = current_token()
        if not token:
            return {"ok": True, "required": False}
        ip = _client_ip(request)
        if _too_many_fails(ip):
            raise HTTPException(429, "尝试次数过多，请 10 分钟后再试")
        if not hmac.compare_digest((req.token or "").strip(), token):
            _record_fail(ip)
            raise HTTPException(401, "口令不正确")
        _set_cookie(response, token, request)
        return {"ok": True}

    @app.post("/api/auth/logout")
    def auth_logout(response: Response):
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @app.post("/api/auth/setup")
    def auth_setup(req: SetupRequest, request: Request, response: Response):
        """设置或修改访问口令。已有口令时必须提供旧口令（或已登录）。"""
        from src.core.secrets_env import set_env_secret

        new = (req.token or "").strip()
        if len(new) < MIN_TOKEN_LEN:
            raise HTTPException(400, f"口令至少 {MIN_TOKEN_LEN} 位")
        if any(c.isspace() for c in new):
            raise HTTPException(400, "口令不能包含空格")
        old = current_token()
        ip = _client_ip(request)
        if old:
            if _too_many_fails(ip):
                raise HTTPException(429, "尝试次数过多，请 10 分钟后再试")
            ok = is_authed(request) or hmac.compare_digest((req.old_token or "").strip(), old)
            if not ok:
                _record_fail(ip)
                raise HTTPException(401, "旧口令不正确")
        elif not _is_trusted_local_setup(request):
            # 公网初始化需要操作者事先通过服务器控制台配置引导令牌，防止陌生访客抢先设密。
            bootstrap = os.environ.get("ITERWAVE_BOOTSTRAP_TOKEN", "")
            provided = (req.bootstrap_token or "").strip()
            if not bootstrap or not provided or not hmac.compare_digest(provided, bootstrap):
                raise HTTPException(
                    403,
                    "首次设置口令需从本机直连，或在服务器配置 ITERWAVE_BOOTSTRAP_TOKEN 后提交引导口令",
                )
        set_env_secret(TOKEN_ENV, new)
        _set_cookie(response, new, request)
        return {"ok": True, "required": True}
