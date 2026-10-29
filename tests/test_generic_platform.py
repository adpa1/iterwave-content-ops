"""通用平台改造：选题库三来源 / 访问口令 / 通知 Webhook / 自定义热点源 / 行业模板。"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.core.kb_reader import KbReader
from src.core.kb_sources import display_path, parse_feed, scrub_paths
from src.core.notify import Notifier
from src.core.state_machine import DraftStateMachine

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
<item><title>First news</title><link>https://e.com/1</link><category>Macro</category>
<pubDate>Wed, 28 Oct 2026 10:00:00 GMT</pubDate><description>&lt;p&gt;hello&lt;/p&gt;</description></item>
<item><title>Second</title><link>https://e.com/2</link></item>
</channel></rss>"""


@pytest.fixture()
def env(cfg, conn, tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.config_store.OVERRIDES_FILE", tmp_path / "ov.json")
    envf = tmp_path / ".env"
    monkeypatch.setattr("src.core.secrets_env.DEFAULT_ENV_PATH", envf)
    monkeypatch.setenv("ITERWAVE_ACCESS_TOKEN", "test-access")
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({"cfg": cfg, "conn": conn, "sm_draft": DraftStateMachine(conn),
                            "notifier": Notifier(cfg, conn)})
    try:
        client = TestClient(
            api_main._build_app(),
            base_url="http://127.0.0.1",
            headers={"Authorization": "Bearer test-access"},
            client=("127.0.0.1", 54321),
        )
        yield client, cfg, envf
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)
        os.environ.pop("WECOM_WEBHOOK_URL", None)


@pytest.fixture()
def unconfigured_env(cfg, conn, tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.config_store.OVERRIDES_FILE", tmp_path / "ov.json")
    envf = tmp_path / ".env"
    monkeypatch.setattr("src.core.secrets_env.DEFAULT_ENV_PATH", envf)
    monkeypatch.delenv("ITERWAVE_ACCESS_TOKEN", raising=False)
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({"cfg": cfg, "conn": conn, "sm_draft": DraftStateMachine(conn),
                            "notifier": Notifier(cfg, conn)})
    try:
        client = TestClient(
            api_main._build_app(),
            base_url="http://127.0.0.1",
            client=("127.0.0.1", 54321),
        )
        yield client, cfg, envf
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)
        os.environ.pop("ITERWAVE_ACCESS_TOKEN", None)
        os.environ.pop("WECOM_WEBHOOK_URL", None)


def test_kb_not_configured_is_not_an_error(env):
    client, cfg, _ = env
    cfg.topic_kb.path = ""
    cfg.topic_kb.source_type = "local"
    items, rep = KbReader(cfg).read()
    assert items == [] and rep["configured"] is False
    r = client.get("/api/kb/inspect").json()
    assert r["configured"] is False and r["ok"] is False


def test_kb_upload_csv_with_chinese_headers_and_default_dimension(env):
    client, cfg, _ = env
    csv_bytes = "标题,摘要\n第一条,内容A\n第二条,内容B\n".encode("utf-8")
    r = client.post("/api/kb/upload", files={"file": ("我的选题.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200, r.text
    assert cfg.topic_kb.source_type == "upload"
    assert not os.path.isabs(cfg.topic_kb.path) or "kb/uploads" in cfg.topic_kb.path
    ins = r.json()["inspect"]
    assert ins["ok"] is True and ins["field_mapping"]["title"] == "标题"
    items, _ = KbReader(cfg).read()
    assert [i.title for i in items] == ["第一条", "第二条"]
    assert all(i.dimension == "综合" for i in items)


def test_kb_upload_rejects_bad_type(env):
    client, _, _ = env
    r = client.post("/api/kb/upload", files={"file": ("x.exe", b"abc", "application/octet-stream")})
    assert r.status_code == 400


def test_kb_missing_file_message_hides_absolute_path(env):
    client, cfg, _ = env
    cfg.topic_kb.source_type = "local"
    cfg.topic_kb.path = "/Users/someone/private/kb.db"
    r = client.get("/api/kb/inspect").json()
    assert r["ok"] is False
    assert "/Users/someone" not in str(r)


def test_remote_rss_sync(env, monkeypatch):
    client, cfg, _ = env
    import httpx

    from urllib.parse import urlsplit
    from src.core import safe_http

    class Resp:
        status_code = 200
        content = RSS
        headers = {"content-type": "application/rss+xml"}
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def iter_bytes(self): yield self.content

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def stream(self, method, url, headers=None, follow_redirects=False): return Resp()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr(safe_http, "validate_public_url", lambda url: urlsplit(url))
    r = client.patch("/api/config", json={"updates": {
        "topic_kb.source_type": "remote", "topic_kb.remote_url": "https://e.com/feed"}})
    assert r.status_code == 200, r.text
    s = client.post("/api/kb/sync")
    assert s.status_code == 200, s.text
    assert s.json()["rows"] == 2 and s.json()["format"] == "rss"
    items, _ = KbReader(cfg).read()
    assert {i.dimension for i in items} == {"Macro", "综合"}


def test_parse_feed_and_scrub():
    rows = parse_feed(RSS)
    assert rows[0]["summary"] == "hello"
    assert "/home/ubuntu" not in scrub_paths("找不到 /home/ubuntu/data/kb.db")
    assert display_path("/srv/elsewhere/kb.db").endswith("kb.db")


def test_public_first_setup_requires_bootstrap_token(unconfigured_env, monkeypatch):
    client, _, _ = unconfigured_env
    monkeypatch.setenv("ITERWAVE_BOOTSTRAP_TOKEN", "bootstrap-secret")
    public = TestClient(
        client.app,
        base_url="https://eduplay.cloud",
        client=("198.51.100.10", 54000),
    )
    assert public.post("/api/auth/setup", json={"token": "secret-12345"}).status_code == 403
    assert public.post(
        "/api/auth/setup",
        json={"token": "secret-12345", "bootstrap_token": "wrong"},
    ).status_code == 403
    r = public.post(
        "/api/auth/setup",
        json={"token": "secret-12345", "bootstrap_token": "bootstrap-secret"},
    )
    assert r.status_code == 200, r.text


def test_bearer_auth_failures_are_rate_limited(unconfigured_env, monkeypatch):
    client, _, _ = unconfigured_env
    monkeypatch.setenv("ITERWAVE_ACCESS_TOKEN", "secret-12345")
    for _ in range(10):
        assert client.get(
            "/api/verticals", headers={"Authorization": "Bearer wrong"}
        ).status_code == 401
    blocked = client.get("/api/verticals", headers={"Authorization": "Bearer wrong"})
    assert blocked.status_code == 429


def test_access_token_flow(unconfigured_env):
    client, _, envf = unconfigured_env
    assert client.get("/api/auth/status").json() == {"required": False, "authed": True}
    assert client.get("/api/verticals").status_code == 403
    assert client.get("/api/verticals").json()["code"] == "setup_required"
    r = client.post("/api/auth/setup", json={"token": "short"})
    assert r.status_code == 400
    r = client.post("/api/auth/setup", json={"token": "secret-12345"})
    assert r.status_code == 200
    assert "ITERWAVE_ACCESS_TOKEN=secret-12345" in envf.read_text()
    anon = TestClient(client.app)
    assert anon.get("/api/verticals").status_code == 401
    assert anon.post("/api/auth/login", json={"token": "nope"}).status_code == 401
    assert anon.get("/api/verticals", headers={"Authorization": "Bearer secret-12345"}).status_code == 200
    assert anon.post("/api/auth/login", json={"token": "secret-12345"}).status_code == 200
    assert anon.get("/api/verticals").status_code == 200
    # 已有口令时，没登录也不能改
    other = TestClient(client.app)
    assert other.post("/api/auth/setup", json={"token": "another-123456"}).status_code == 401


def test_webhook_saved_to_env_and_validated(env):
    client, cfg, envf = env
    bad = client.post("/api/notify/webhook", json={"channel": "wecom", "url": "https://evil.com/x"})
    assert bad.status_code in (400, 422)
    ok = client.post("/api/notify/webhook", json={
        "channel": "wecom", "url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["wecom"]["configured"] is True
    assert cfg.notifications.channels["wecom"].enabled is True
    assert "qyapi" in envf.read_text()
    assert "qyapi" not in client.get("/api/notify/channels").text


def test_config_redacts_custom_feed_urls_and_preserves_them_on_other_updates(env):
    client, cfg, _ = env
    secret_url = "https://feeds.example.com/private/token?access_token=TOPSECRET"
    cfg.hot_topics.custom_feeds = [{"name": "私有行业源", "url": secret_url}]

    response = client.get("/api/config")
    assert response.status_code == 200
    feeds = response.json()["config"]["editable"]["hot_topics"]["custom_feeds"]
    assert feeds == [{"name": "私有行业源", "url": "https://feeds.example.com/…"}]
    assert "TOPSECRET" not in response.text
    assert "/private/token" not in response.text

    # 模拟设置页回传脱敏地址，同时修改另一个配置项；后端应恢复未变更的原 URL。
    updated = client.patch("/api/config", json={"updates": {
        "hot_topics.custom_feeds": feeds,
        "hot_topics.general_lists_enabled": False,
    }})
    assert updated.status_code == 200, updated.text
    assert cfg.hot_topics.custom_feeds[0]["url"] == secret_url


def test_custom_feeds_config_validation(env):
    client, cfg, _ = env
    r = client.patch("/api/config", json={"updates": {
        "hot_topics.custom_feeds": [{"name": "行业快讯", "url": "https://e.com/rss"}],
        "hot_topics.general_lists_enabled": False}})
    assert r.status_code == 200, r.text
    from src.core.hot_topics import HotTopicCollector
    c = HotTopicCollector(cfg, None)
    assert c.platforms == [] and list(c.feeds) == ["feed:行业快讯"]
    bad = client.patch("/api/config", json={"updates": {
        "hot_topics.custom_feeds": [{"name": "x", "url": "ftp://e.com"}]}})
    assert bad.status_code in (400, 422)


def test_schedule_describe_is_human():
    from src.core.config import JobScheduleConfig
    assert JobScheduleConfig(cron="0 8 * * *").describe() == "每天 8:00"
    assert JobScheduleConfig(interval_minutes=120).describe() == "每 2 小时"
