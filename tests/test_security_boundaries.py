"""Focused regression tests for security boundaries added during source review."""

from __future__ import annotations

import socket
import zipfile
from urllib.parse import urlsplit

import pytest

from src.core import kb_sources, safe_http
from src.core.db import Database


def _public_dns(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def test_remote_url_rejects_loopback_private_and_url_credentials():
    for url in (
        "http://127.0.0.1/internal",
        "http://10.1.2.3/private",
        "http://169.254.169.254/latest/meta-data",
        "http://user:password@example.com/feed",
        "http://example.com/feed\nAuthorization: Bearer stolen",
    ):
        with pytest.raises(safe_http.SafeFetchError):
            safe_http.validate_public_url(url)


def test_remote_domain_that_resolves_to_private_ip_is_rejected(monkeypatch):
    monkeypatch.setattr(
        safe_http.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.2.7", port))
        ],
    )
    with pytest.raises(safe_http.SafeFetchError, match="内网|保留 IP"):
        safe_http.validate_public_url("https://feed.example/rss")


def test_remote_redirect_to_private_host_is_blocked(monkeypatch):
    monkeypatch.setattr(safe_http.socket, "getaddrinfo", _public_dns)
    requested = []

    class FakeResponse:
        status_code = 302
        headers = {"location": "http://127.0.0.1/admin"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self):
            return iter(())

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, **kwargs):
            requested.append(url)
            return FakeResponse()

    monkeypatch.setattr(safe_http.httpx, "Client", FakeClient)
    with pytest.raises(safe_http.SafeFetchError, match="本机|内网"):
        safe_http.fetch_public_bytes("https://feed.example/start")
    assert requested == ["https://feed.example/start"]


def test_remote_body_size_is_enforced_before_accumulating(monkeypatch):
    monkeypatch.setattr(safe_http, "validate_public_url", lambda url: urlsplit(url))

    class FakeResponse:
        status_code = 200
        headers = {"content-length": "6", "content-type": "text/plain"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_bytes(self):
            yield b"123456"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def stream(self, method, url, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(safe_http.httpx, "Client", FakeClient)
    with pytest.raises(safe_http.SafeFetchError, match="大小上限"):
        safe_http.fetch_public_bytes("https://feed.example/data", max_bytes=5)


def test_remote_url_display_hides_path_and_query_credentials():
    shown = kb_sources._mask_url(
        "https://feeds.example.com/private/token?access_token=TOPSECRET&x=1"
    )
    assert shown == "https://feeds.example.com/…"
    assert "TOPSECRET" not in shown and "private" not in shown


def test_xlsx_expanded_size_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(kb_sources, "MAX_XLSX_EXPANDED_BYTES", 128)
    path = tmp_path / "oversized.xlsx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
        archive.writestr("payload.txt", "x" * 2048)
    with pytest.raises(kb_sources.KbSourceError, match="展开后"):
        kb_sources.read_xlsx_rows(path)


def test_uploaded_source_and_database_permissions_are_private(cfg, conn):
    path = kb_sources.save_upload(cfg, "private.csv", b"source,title\nmanual,example\n")
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert oct(kb_sources.kb_dir(cfg).stat().st_mode)[-3:] == "700"
    assert oct((kb_sources.kb_dir(cfg) / "uploads").stat().st_mode)[-3:] == "700"

    db = Database(cfg)
    assert oct(db.db_path.stat().st_mode)[-3:] == "600"
