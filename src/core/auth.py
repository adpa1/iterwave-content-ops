"""访问保护 — 访问口令 + 会话 Cookie + 脚本令牌（P0 安全）。

口径：
- 未设置口令：仅允许读取认证状态和设置首次口令；其它业务 API 返回 setup_required
- 本机直连仅在显式设置 ITERWAVE_ALLOW_LOCAL_BOOTSTRAP=1 时可免引导设置；其它首次设置必须提供服务端 ITERWAVE_BOOTSTRAP_TOKEN
- 设置口令后：``/api/*`` 全部校验；页面未登录时只能看到登录页
- 脚本调用：请求头 ``X-Access-Token: <口令>`` 或 ``Authorization: Bearer <口令>``
- 登录和脚本令牌失败共用按 IP 限速（10 分钟内最多 5 次，超出 429）
- 口令以 PBKDF2-HMAC-SHA256 哈希存 config/.env（chmod 600），永不回传明文
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import time
from typing import Optional

from .env_store import read_env, write_env

logger = logging.getLogger(__name__)

PASSWORD_ENV = "ACCESS_PASSWORD_HASH"
COOKIE_NAME = "iterwave_session"
SESSION_TTL_SECONDS = 30 * 24 * 3600
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 10 * 60


def is_trusted_local_setup(request) -> bool:
    """仅限直连本机开发环境的首次设置旁路。

    该开关不能安全地用于反向代理/公网部署：代理后端可能表现为 loopback，
    而 Host 由请求方控制。生产或代理部署应使用 ITERWAVE_BOOTSTRAP_TOKEN。
    """
    if os.getenv("ITERWAVE_ALLOW_LOCAL_BOOTSTRAP", "").strip().lower() not in ("1", "true", "yes"):
        return False
    if any(request.headers.get(name) for name in (
        "forwarded", "x-forwarded-for", "x-real-ip", "x-forwarded-proto", "x-forwarded-host"
    )):
        return False
    peer = request.client.host if request.client else ""
    host = (request.url.hostname or "").strip("[]").lower()
    try:
        peer_local = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        peer_local = peer.lower() == "localhost"
    try:
        host_local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        host_local = host == "localhost"
    return peer_local and host_local

# 内存会话（重启即失效，对本地单进程足够；不落盘避免多一份密钥）
_sessions: dict[str, float] = {}
_failures: dict[str, list[float]] = {}


# ----------------------------------------------------------------------
# 口令存取
# ----------------------------------------------------------------------

def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 300_000)
    return f"pbkdf2$300000${salt}${digest.hex()}"


def _verify_hash(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2":
        return False
    try:
        rounds_n = int(rounds)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), rounds_n)
    return hmac.compare_digest(calc.hex(), digest)


def password_configured() -> bool:
    return bool(read_env(PASSWORD_ENV))


def get_password_hash() -> Optional[str]:
    return read_env(PASSWORD_ENV)


def set_password(password: str) -> None:
    password = (password or "").strip()
    if len(password) < 6:
        raise ValueError("访问口令至少 6 个字符")
    if len(password) > 64:
        raise ValueError("访问口令最长 64 个字符")
    write_env(PASSWORD_ENV, _hash_password(password))
    _sessions.clear()
    logger.info("访问口令已更新，已有会话全部失效")


def clear_password() -> None:
    write_env(PASSWORD_ENV, "")
    _sessions.clear()
    logger.info("访问口令已清除，全站恢复免登录（会显示安全警告）")


def verify_password(password: str) -> bool:
    stored = get_password_hash()
    if not stored:
        return False
    return _verify_hash(password or "", stored)


# ----------------------------------------------------------------------
# 会话
# ----------------------------------------------------------------------

def _new_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL_SECONDS
    return token


def _session_valid(token: Optional[str]) -> bool:
    if not token:
        return False
    expire = _sessions.get(token)
    if expire is None:
        return False
    if expire < time.time():
        _sessions.pop(token, None)
        return False
    return True


def logout_session(token: Optional[str]) -> None:
    if token:
        _sessions.pop(token, None)


# ----------------------------------------------------------------------
# 登录限速
# ----------------------------------------------------------------------

def _recent_failures(ip: str) -> list[float]:
    now = time.time()
    keep = [t for t in _failures.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
    _failures[ip] = keep
    return keep


def is_rate_limited(ip: str) -> bool:
    return len(_recent_failures(ip)) >= LOGIN_MAX_FAILURES


def record_failure(ip: str) -> None:
    _failures.setdefault(ip, []).append(time.time())
    _recent_failures(ip)


def clear_failures(ip: str) -> None:
    _failures.pop(ip, None)


def login(password: str, ip: str) -> str:
    """校验口令并签发会话 token；失败抛 ValueError / 限速抛 PermissionError。"""
    if is_rate_limited(ip):
        raise PermissionError("尝试次数过多，请 10 分钟后再试")
    if not verify_password(password):
        record_failure(ip)
        raise ValueError("访问口令不正确")
    clear_failures(ip)
    return _new_session()


# ----------------------------------------------------------------------
# 请求判定
# ----------------------------------------------------------------------

def request_token(cookie_token: Optional[str], headers) -> Optional[str]:
    """从 Cookie / X-Access-Token / Authorization 头提取候选令牌。"""
    header_token = headers.get("x-access-token") or ""
    if not header_token:
        auth = headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            header_token = auth[7:].strip()
    return cookie_token or header_token or None


def is_authenticated(cookie_token: Optional[str], headers, ip: Optional[str] = None) -> bool:
    if not password_configured():
        # 未完成首次设置时不宣称请求已认证；中间件只开放明确的初始化路由。
        return False
    # Cookie 只接受服务端签发的会话 token；无效 Cookie 不再触发 PBKDF2 口令校验。
    if _session_valid(cookie_token):
        return True

    # 脚本口令只能从明确的认证请求头传入，并且与登录接口共用失败计数/限速。
    candidate = request_token(None, headers)
    if not candidate:
        return False
    client_ip = ip or "unknown"
    if is_rate_limited(client_ip):
        return False
    if verify_password(candidate):
        clear_failures(client_ip)
        return True
    record_failure(client_ip)
    return False


def status(cookie_token: Optional[str], headers, ip: Optional[str] = None) -> dict:
    configured = password_configured()
    authenticated = is_authenticated(cookie_token, headers, ip=ip)
    return {
        "configured": configured,
        "authenticated": authenticated,
        "needs_login": configured and not authenticated,
        "cookie_name": COOKIE_NAME,
    }


def session_cookie_header(token: str, *, secure: bool = False) -> str:
    """生成会话 Cookie；HTTPS 部署附加 Secure，纯 HTTP 本地开发仍可登录。"""
    attrs = f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax"
    if secure:
        attrs += "; Secure"
    return f"{attrs}; Max-Age={SESSION_TTL_SECONDS}"


def clear_cookie_header() -> str:
    return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


def mask_password_hash(stored: Optional[str]) -> str:
    """只暴露前缀，方便排查配置，不泄露哈希本体。"""
    if not stored:
        return ""
    return stored[:10] + "***"


def basic_token_preview(password: str) -> str:
    """脚本用法提示里的占位，不返回真实口令。"""
    return base64.b64encode(password.encode()).decode()[:6] + "***"
