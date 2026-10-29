"""安全的公网 HTTP(S) 请求工具。

用户配置的远程选题源、热点源和 Webhook 都会触发服务器主动出站请求。
这里在解析 DNS 后，把已验证的公网 IP 固定到实际 TCP 连接，避免 HTTP 客户端再次解析域名造成 DNS rebinding。
TLS SNI、证书主机名验证和 HTTP Host 仍使用原始域名。

实现通过 httpx 的 HTTPTransport 底层 httpcore ConnectionPool 注入同步网络后端；
这依赖 httpcore 的 NetworkBackend 接口，应由安全边界回归测试和 CI 保护。
公网部署仍应通过防火墙限制服务端出站访问。
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx
from httpcore._backends.sync import SyncBackend


class SafeFetchError(ValueError):
    """可直接展示给操作者的安全请求错误，不含原始 URL。"""


def _normalise_host(host: str) -> str:
    return host.strip("[]").rstrip(".").encode("idna").decode("ascii").lower()


def _resolve_public_url(url: str) -> tuple[object, str, int, tuple[str, ...]]:
    if not isinstance(url, str) or not url:
        raise SafeFetchError("远程地址不能为空")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise SafeFetchError("远程地址不能包含空白或控制字符")

    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        raw_host = parsed.hostname
        declared_port = parsed.port
        if declared_port == 0:
            raise SafeFetchError("远程地址端口不合法")
        port = declared_port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise SafeFetchError("远程地址格式不合法") from exc

    if scheme not in ("http", "https") or not raw_host:
        raise SafeFetchError("远程地址必须是完整的 HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise SafeFetchError("远程地址不能在 URL 中包含用户名或密码")
    if "%" in raw_host:
        raise SafeFetchError("远程地址不能使用带网络接口标识的 IP")

    try:
        host = _normalise_host(raw_host)
    except (UnicodeError, ValueError) as exc:
        raise SafeFetchError("远程地址主机名格式不合法") from exc

    if host == "localhost" or host.endswith(".localhost"):
        raise SafeFetchError("不允许访问本机或内网地址")

    try:
        literal_ip = ipaddress.ip_address(host)
        candidates = (literal_ip,)
    except ValueError:
        try:
            records = socket.getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except (OSError, ValueError) as exc:
            raise SafeFetchError("远程域名无法解析") from exc
        candidates = tuple(
            {
                ipaddress.ip_address(record[4][0].split("%", 1)[0])
                for record in records
            }
        )

    # 若 DNS 返回的地址中有任一非公网 IP，整组拒绝，避免通过地址选择绕过限制。
    if not candidates or any(not address.is_global for address in candidates):
        raise SafeFetchError("远程地址解析到了本机、内网或保留 IP，已拒绝请求")
    addresses = tuple(dict.fromkeys(address.compressed for address in candidates))
    return parsed, host, port, addresses


def validate_public_url(url: str):
    """验证公网 HTTP(S) URL 并返回解析结果；实际请求应使用下方固定 IP 的接口。"""
    parsed, _, _, _ = _resolve_public_url(url)
    return parsed


class _PinnedNetworkBackend:
    """将 httpcore 的 TCP 连接目标映射到当前请求已验证的 IP 列表。"""

    def __init__(self) -> None:
        self._backend = SyncBackend()
        self._lock = threading.RLock()
        self._pins: dict[tuple[str, int], tuple[str, ...]] = {}

    def set_pins(self, host: str, port: int, addresses: tuple[str, ...]) -> None:
        with self._lock:
            self._pins[(_normalise_host(host), int(port))] = tuple(addresses)

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        key = (_normalise_host(host), int(port))
        with self._lock:
            addresses = self._pins.get(key)
        if not addresses:
            raise httpcore.ConnectError("拒绝连接未完成公网 IP 校验的目标")

        last_error: Exception | None = None
        for address in addresses:
            try:
                # address 是数字 IP；socket 层不会再对原始域名做 DNS 查询。
                return self._backend.connect_tcp(
                    host=address,
                    port=port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # 逐一尝试已验证的公网地址
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("没有可用的已验证公网 IP")

    def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options=None):
        return self._backend.connect_unix_socket(
            path=path, timeout=timeout, socket_options=socket_options
        )

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class _PinnedHTTPTransport(httpx.HTTPTransport):
    def __init__(self) -> None:
        # 禁用环境代理：代理会让实际 TCP 目标变成代理服务器，绕过本地 IP 固定。
        super().__init__(trust_env=False, retries=0)
        self._pinned_backend = _PinnedNetworkBackend()
        # HTTPTransport 默认创建 httpcore.ConnectionPool；必须在首次请求前替换后端。
        self._pool._network_backend = self._pinned_backend

    def pin(self, host: str, port: int, addresses: tuple[str, ...]) -> None:
        self._pinned_backend.set_pins(host, port, addresses)


def _origin(parsed) -> tuple[str, str, int]:
    scheme = parsed.scheme.lower()
    host = _normalise_host(parsed.hostname or "")
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, host, port


def _read_limited(response, max_bytes: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared:
        try:
            declared_size = int(declared)
        except ValueError:
            declared_size = None
        if declared_size is not None and (declared_size < 0 or declared_size > max_bytes):
            raise SafeFetchError("远程响应超过大小上限")

    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > max_bytes:
            raise SafeFetchError("远程响应超过大小上限")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_public_bytes(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 20,
    max_bytes: int = 20 * 1024 * 1024,
    max_redirects: int = 5,
) -> tuple[bytes, str, str]:
    """抓取公网 URL，返回 (body, content_type, final_url)，逐跳校验并固定 TCP 目标 IP。"""
    current_url = url
    redirect_statuses = {301, 302, 303, 307, 308}
    transport = _PinnedHTTPTransport()

    try:
        with httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for _ in range(max_redirects + 1):
                parsed, host, port, addresses = _resolve_public_url(current_url)
                transport.pin(host, port, addresses)
                with client.stream(
                    "GET", current_url, headers=headers or {}, follow_redirects=False
                ) as response:
                    if response.status_code in redirect_statuses:
                        location = response.headers.get("location")
                        if not location:
                            raise SafeFetchError("远程地址返回了不完整的重定向")
                        next_url = urljoin(current_url, location)
                        try:
                            next_parsed = urlsplit(next_url)
                            if _origin(parsed) != _origin(next_parsed):
                                raise SafeFetchError(
                                    "远程地址不允许跨域重定向，请直接填写最终地址"
                                )
                        except ValueError as exc:
                            raise SafeFetchError("远程地址重定向目标格式不合法") from exc
                        current_url = next_url
                        continue

                    if response.status_code in (401, 403):
                        raise SafeFetchError("远程地址拒绝访问，请检查鉴权设置")
                    if response.status_code < 200 or response.status_code >= 300:
                        raise SafeFetchError(
                            f"远程地址返回错误（HTTP {response.status_code}）"
                        )
                    body = _read_limited(response, max_bytes)
                    return body, response.headers.get("content-type", ""), current_url
    except SafeFetchError:
        raise
    except httpx.TimeoutException as exc:
        raise SafeFetchError("远程地址响应超时") from exc
    except httpx.HTTPError as exc:
        raise SafeFetchError(f"无法连接远程地址（{type(exc).__name__}）") from exc
    except OSError as exc:
        raise SafeFetchError("无法连接经过校验的远程地址") from exc

    raise SafeFetchError("远程地址重定向次数过多")


def post_public_json(
    url: str,
    payload: dict,
    *,
    timeout: float = 5,
    max_bytes: int = 1024 * 1024,
) -> tuple[int, bytes, str]:
    """向经过校验的公网 URL 发送 JSON；不跟随重定向，返回状态码、响应体与 content-type。"""
    transport = _PinnedHTTPTransport()
    try:
        _, host, port, addresses = _resolve_public_url(url)
        transport.pin(host, port, addresses)
        with httpx.Client(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream("POST", url, json=payload, follow_redirects=False) as response:
                body = _read_limited(response, max_bytes)
                return response.status_code, body, response.headers.get("content-type", "")
    except SafeFetchError:
        raise
    except httpx.TimeoutException as exc:
        raise SafeFetchError("远程通知地址响应超时") from exc
    except httpx.HTTPError as exc:
        raise SafeFetchError(f"无法连接远程通知地址（{type(exc).__name__}）") from exc
    except OSError as exc:
        raise SafeFetchError("无法连接经过校验的远程通知地址") from exc
