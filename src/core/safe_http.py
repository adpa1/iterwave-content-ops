"""安全的公网 HTTP(S) 抓取工具，用于用户可配置的远程数据源。

限制：
- 拒绝 localhost、内网、链路本地、多播和保留 IP；
- 每一跳重定向重新校验，且只允许同源跳转；
- 流式读取并限制解压后的响应体积；
- 对外错误不暴露带签名参数的完整 URL。

注意：DNS 校验是纵深防御；公网部署仍应在网络/防火墙层限制出站访问，防止 DNS rebinding 与云元数据访问。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import httpx


class SafeFetchError(ValueError):
    """可直接展示给操作者的安全抓取错误，不含原始 URL。"""


def validate_public_url(url: str):
    if not isinstance(url, str) or not url:
        raise SafeFetchError("远程地址不能为空")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise SafeFetchError("远程地址不能包含空白或控制字符")

    try:
        parsed = urlsplit(url)
        scheme, host, port = parsed.scheme.lower(), parsed.hostname, parsed.port
    except ValueError as exc:
        raise SafeFetchError("远程地址格式不合法") from exc

    if scheme not in ("http", "https") or not host:
        raise SafeFetchError("远程地址必须是完整的 HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise SafeFetchError("远程地址不能在 URL 中包含用户名或密码")

    host = host.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise SafeFetchError("不允许访问本机或内网地址")

    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            records = socket.getaddrinfo(
                host,
                port or (443 if scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
            addresses = {
                ipaddress.ip_address(record[4][0].split("%", 1)[0])
                for record in records
            }
        except (OSError, ValueError) as exc:
            raise SafeFetchError("远程域名无法解析") from exc

    if not addresses or any(not address.is_global for address in addresses):
        raise SafeFetchError("远程地址解析到了本机、内网或保留 IP，已拒绝请求")
    return parsed


def _origin(parsed) -> tuple[str, str, int]:
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, host, port


def fetch_public_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 20,
    max_bytes: int = 20 * 1024 * 1024,
    max_redirects: int = 5,
) -> tuple[bytes, str, str]:
    """下载一个公网 URL，返回 (body, content_type, final_url)。"""
    current_url = url
    redirect_statuses = {301, 302, 303, 307, 308}

    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            for _ in range(max_redirects + 1):
                parsed = validate_public_url(current_url)
                with client.stream(
                    "GET",
                    current_url,
                    headers=headers or {},
                    follow_redirects=False,
                ) as response:
                    if response.status_code in redirect_statuses:
                        location = response.headers.get("location")
                        if not location:
                            raise SafeFetchError("远程地址返回了不完整的重定向")
                        next_url = urljoin(current_url, location)
                        next_parsed = validate_public_url(next_url)
                        if _origin(parsed) != _origin(next_parsed):
                            raise SafeFetchError("远程地址不允许跨域重定向，请填写最终地址")
                        current_url = next_url
                        continue

                    if response.status_code in (401, 403):
                        raise SafeFetchError("远程地址拒绝访问，请检查鉴权设置")
                    if response.status_code < 200 or response.status_code >= 300:
                        raise SafeFetchError(f"远程地址返回错误（HTTP {response.status_code}）")

                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            declared_size = int(content_length)
                        except ValueError:
                            declared_size = None
                        if declared_size is not None and declared_size > max_bytes:
                            raise SafeFetchError("远程响应超过大小上限")

                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise SafeFetchError("远程响应超过大小上限")
                        chunks.append(chunk)
                    return b"".join(chunks), response.headers.get("content-type", ""), current_url
    except SafeFetchError:
        raise
    except httpx.TimeoutException as exc:
        raise SafeFetchError("远程地址响应超时") from exc
    except httpx.HTTPError as exc:
        raise SafeFetchError(f"无法连接远程地址（{type(exc).__name__}）") from exc

    raise SafeFetchError("远程地址重定向次数过多")
