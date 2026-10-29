"""访问保护 + 选题库三来源 + Webhook 落盘 + 自定义热点源配置。

覆盖审查文档「未完成」里没有专门测试的新功能：
口令登录/限速、上传（CSV/XLSX）、服务器路径、远程（CSV/JSON/RSS）、路径脱敏、Webhook 写 .env。
"""

from __future__ import annotations

import base64
import csv
import io
import json
import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.core import auth, config_store, env_store, kb_sources


@pytest.fixture(autouse=True)
def clean_auth_state(monkeypatch, tmp_path):
    """每个用例独立 .env + 清空内存会话/失败计数，避免用例互相污染。"""
    monkeypatch.setenv("ITERWAVE_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv(auth.PASSWORD_ENV, raising=False)
    auth._sessions.clear()
    auth._failures.clear()
    yield
    auth._sessions.clear()
    auth._failures.clear()
    os.environ.pop(auth.PASSWORD_ENV, None)


@pytest.fixture()
def api_client(cfg, conn):
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({"cfg": cfg, "conn": conn})
    try:
        yield TestClient(api_main._build_app())
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)


# =====================================================================
# 访问保护
# =====================================================================

def test_password_lifecycle(tmp_path):
    assert auth.password_configured() is False
    with pytest.raises(ValueError):
        auth.set_password("short")

    auth.set_password("secret123")
    assert auth.password_configured() is True
    assert auth.verify_password("secret123") is True
    assert auth.verify_password("wrong") is False
    # 落盘位置就是我们指定的临时 .env，且权限 600（R23）
    env_path = env_store.env_file()
    assert env_path == tmp_path / ".env"
    assert oct(env_path.stat().st_mode)[-3:] == "600"

    auth.clear_password()
    assert auth.password_configured() is False


def test_session_cookie_marks_https_as_secure():
    http_cookie = auth.session_cookie_header("session-token")
    https_cookie = auth.session_cookie_header("session-token", secure=True)
    assert "HttpOnly" in http_cookie and "SameSite=Lax" in http_cookie
    assert "Secure" not in http_cookie
    assert "; Secure;" in https_cookie


def test_login_rate_limit():
    auth.set_password("secret123")
    for _ in range(auth.LOGIN_MAX_FAILURES):
        with pytest.raises(ValueError):
            auth.login("wrong", "1.2.3.4")
    with pytest.raises(PermissionError):
        auth.login("secret123", "1.2.3.4")
    # 其它 IP 不受影响
    assert auth.login("secret123", "5.6.7.8")


def test_middleware_requires_login_only_after_password_set(api_client):
    assert api_client.get("/api/topics").status_code == 200

    assert api_client.post("/api/auth/password", json={"password": "secret123"}).status_code == 200
    assert api_client.get("/api/auth/status").json()["needs_login"] is True
    assert api_client.get("/api/topics").status_code == 401
    assert api_client.get("/api/topics").json()["code"] == "auth_required"

    assert api_client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    r = api_client.post("/api/auth/login", json={"password": "secret123"})
    assert r.status_code == 200 and r.json()["authenticated"] is True
    # TestClient 自动保存 Set-Cookie，会话生效
    assert api_client.get("/api/topics").status_code == 200

    # 脚本口令（X-Access-Token）也能过
    api_client.cookies.clear()
    assert api_client.get("/api/topics", headers={"X-Access-Token": "secret123"}).status_code == 200
    assert api_client.get("/api/topics", headers={"Authorization": "Bearer secret123"}).status_code == 200

    # 登出后会话失效
    api_client.post("/api/auth/logout")
    assert api_client.get("/api/topics").status_code == 401


def test_login_rate_limit_over_http(api_client):
    api_client.post("/api/auth/password", json={"password": "secret123"})
    for _ in range(auth.LOGIN_MAX_FAILURES):
        assert api_client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert api_client.post("/api/auth/login", json={"password": "wrong"}).status_code == 429


# =====================================================================
# 选题库三来源
# =====================================================================

CSV_HEADER = "source,title,summary,link,date,dimension"


def _csv_bytes(rows: int = 3) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_HEADER.split(","))
    w.writeheader()
    for i in range(rows):
        w.writerow({"source": "upload", "title": f"上传选题 {i}", "summary": "s",
                    "link": f"https://e.com/{i}", "date": "2026-10-20", "dimension": "维度A"})
    return buf.getvalue().encode("utf-8-sig")


def _xlsx_bytes() -> bytes:
    """最小可用的 xlsx（inlineStr，无 sharedStrings）。"""
    sheet = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>标题</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>维度</t></is></c></row>'
        '<row r="2"><c r="A2" t="inlineStr"><is><t>Excel 选题一</t></is></c>'
        '<c r="B2" t="inlineStr"><is><t>维度B</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def test_upload_csv_and_read(cfg):
    b64 = "data:text/csv;base64," + base64.b64encode(_csv_bytes()).decode()
    source = kb_sources.add_upload(cfg, filename="topics.csv", content_b64=b64)
    assert source["format"] == "csv" and source["rows"] == 3

    items, report = kb_sources.read_all(cfg)
    assert report["kb_count"] >= 3
    titles = {i.title for i in items}
    assert {"上传选题 0", "上传选题 1", "上传选题 2"} <= titles
    # 对外视图不暴露绝对路径（只给脱敏后的 display_path）
    assert "path" not in source
    assert not source["display_path"].startswith("/")


def test_upload_xlsx(cfg):
    b64 = base64.b64encode(_xlsx_bytes()).decode()
    source = kb_sources.add_upload(cfg, filename="topics.xlsx", content_b64=b64)
    assert source["format"] == "xlsx"
    items, report = kb_sources.read_all(cfg)
    assert any(i.title == "Excel 选题一" for i in items)
    assert report["errors"] == 0


def test_upload_rejects_bad_file(cfg):
    with pytest.raises(ValueError):
        kb_sources.add_upload(cfg, filename="x.txt", content_b64="ZGF0YQ==")
    with pytest.raises(ValueError):
        kb_sources.add_upload(cfg, filename="x.xlsx", content_b64="ZGF0YQ==")


def test_server_source_and_path_redaction(cfg, tmp_path):
    path = tmp_path / "server.csv"
    path.write_bytes(_csv_bytes(2))
    source = kb_sources.add_server(cfg, path=str(path), name="服务器库")
    assert source["kind"] == "server"
    # 项目外的绝对路径被脱敏成 ~/…/x 或 …/x，且不回传原始 path
    assert "path" not in source
    assert source["display_path"].startswith(("~/", "…/"))

    items, report = kb_sources.read_all(cfg)
    assert any(i.title == "上传选题 0" for i in items)
    with pytest.raises(FileNotFoundError):
        kb_sources.add_server(cfg, path=str(tmp_path / "missing.csv"))


def test_remote_source_rejects_private_destinations(monkeypatch):
    # 字面 IP 直接拦截；域名解析到内网地址也必须拦截，不能发出 HTTP 请求。
    with pytest.raises(ValueError, match="内网|本机|保留"):
        kb_sources.fetch_remote({"url": "http://127.0.0.1/internal.csv"})

    monkeypatch.setattr(
        kb_sources.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (kb_sources.socket.AF_INET, kb_sources.socket.SOCK_STREAM, 6, "", ("10.0.0.8", 80))
        ],
    )
    with pytest.raises(ValueError, match="内网|本机|保留"):
        kb_sources.fetch_remote({"url": "http://topics.example/internal.csv"})

    with pytest.raises(ValueError, match="用户名或密码"):
        kb_sources.fetch_remote({"url": "http://user:secret@public.example/topics.csv"})


def test_remote_csv_json_rss(cfg, monkeypatch):
    monkeypatch.setattr(kb_sources, "fetch_remote", lambda source, timeout=20: (
        ["source", "title", "summary", "link", "date", "dimension"],
        [{"source": "r", "title": "远程选题", "summary": "s",
          "link": "https://e.com/r", "date": "2026-10-20", "dimension": "维度C"}],
    ))
    csv_src = kb_sources.add_remote(cfg, url="https://example.com/topics.csv", name="远程CSV")
    assert csv_src["rows"] == 1

    monkeypatch.setattr(kb_sources, "fetch_remote", lambda source, timeout=20: (
        ["title", "dimension"],
        [{"title": "JSON 选题", "dimension": "维度D"}],
    ))
    json_src = kb_sources.add_remote(cfg, url="https://example.com/topics.json", name="远程JSON")
    assert json_src["format"] == "json"

    monkeypatch.setattr(kb_sources, "fetch_remote", lambda source, timeout=20: (
        ["title", "link", "summary", "date"],
        [{"title": "RSS 选题", "link": "https://e.com/rss", "summary": "s", "date": "2026-10-20"}],
    ))
    rss_src = kb_sources.add_remote(cfg, url="https://example.com/feed.xml", name="远程RSS")
    assert rss_src["format"] == "rss"

    items, report = kb_sources.read_all(cfg)
    titles = {i.title for i in items}
    assert {"远程选题", "JSON 选题", "RSS 选题"} <= titles
    assert report["errors"] == 0

    # 单源失败不影响其它来源，只记 last_error
    monkeypatch.setattr(kb_sources, "fetch_remote",
                        lambda source, timeout=20: (_ for _ in ()).throw(OSError("boom")))
    out = kb_sources.sync_all_remote(cfg)
    assert out[json_src["id"]] == "failed"
    sources = {s["id"]: s for s in kb_sources.load_registry(cfg)}
    assert "boom" in (sources[json_src["id"]]["last_error"] or "")


def test_preview_and_delete(cfg):
    b64 = base64.b64encode(_csv_bytes(2)).decode()
    source = kb_sources.add_upload(cfg, filename="p.csv", content_b64=b64)
    preview = kb_sources.preview_source(cfg, source["id"], limit=5)
    assert preview["columns"] and len(preview["sample"]) >= 2
    assert preview["rows"] == 2
    assert "title" not in preview["missing"]

    kb_sources.delete_source(cfg, source["id"])
    assert [s["id"] for s in kb_sources.load_registry(cfg)] != [source["id"]]


# =====================================================================
# Webhook 写 .env
# =====================================================================

def test_webhook_written_to_env(api_client, tmp_path):
    r = api_client.post("/api/notify/webhook",
                        json={"channel": "wecom", "url": "https://qy.example/hook", "enabled": True})
    assert r.status_code == 200
    assert env_store.read_env("WECOM_WEBHOOK_URL") == "https://qy.example/hook"
    assert "WECOM_WEBHOOK_URL" in (tmp_path / ".env").read_text(encoding="utf-8")
    # 非 http(s) 地址被拒
    assert api_client.post("/api/notify/webhook",
                           json={"channel": "feishu", "url": "ftp://x"}).status_code == 422
    assert api_client.post("/api/notify/webhook", json={"channel": "x", "url": ""}).status_code == 400


# =====================================================================
# 自定义热点源配置
# =====================================================================

def test_hot_sources_config_roundtrip(api_client):
    assert api_client.get("/api/hot_topics/sources").status_code == 200
    items = [
        {"name": "综合热榜", "type": "aggregate", "url": "", "platforms": ["weibo"]},
        {"name": "行业 RSS", "type": "rss", "url": "https://example.com/feed.xml", "platforms": []},
    ]
    r = api_client.put("/api/hot_topics/sources", json={"items": items})
    assert r.status_code == 200
    saved = api_client.get("/api/hot_topics/sources").json()["items"]
    assert [s["name"] for s in saved] == ["综合热榜", "行业 RSS"]
    assert saved[1]["type"] == "rss"

    # 前端表单只填平台名（不带 platforms 数组）→ 综合热榜自动补全平台，不报错
    r = api_client.put("/api/hot_topics/sources", json={"items": [
        {"name": "综合热榜", "type": "aggregate", "url": "", "platform": "", "enabled": True},
    ]})
    assert r.status_code == 200
    row = r.json()["items"][0]
    assert row["platforms"] == list(config_store.AGGREGATE_PLATFORMS)

    # 超过 20 条被拒
    assert api_client.put(
        "/api/hot_topics/sources", json={"items": [dict(items[1]) for _ in range(21)]}
    ).status_code == 422
