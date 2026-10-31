"""热点：异步采集（GET 只读库）+ 标签匹配。"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.core import hot_topic_service
from src.core.hot_topic_dedup import topic_id
from src.core.hot_topic_matcher import HotTopicMatcher, MATCH_THRESHOLD, dimension_match_of
from src.core.hot_topics import HotTopicCollector, _fetch_public_text


def _insert(conn, tid, title, *, batch="2026-10-28T10:00:00", source="60s.viki.moe/weibo", heat=999999):
    conn.execute(
        "INSERT INTO hot_topics (id, source, raw_url, discovered_at, evidence_status, title, "
        "heat_score, heat_unit, time_sensitive) VALUES (?,?,?,?, 'collected', ?, ?, '热度', NULL)",
        (tid, source, "https://x", batch, title, heat),
    )
    conn.commit()


@pytest.fixture()
def client(cfg, conn, test_auth):
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({"cfg": cfg, "conn": conn})
    hot_topic_service.reset_for_tests()
    try:
        yield test_auth(TestClient(api_main._build_app(), base_url="http://127.0.0.1")), conn
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)


def test_hot_topic_publishability_fails_closed_for_sensitive_and_unreviewed_topics():
    from src.core.hot_topics import _publishability_status, _sensitive_topic_hits

    political = "中共中央举行新闻发布会"
    military = "国防部回应美日近期军演"
    taiwan = "国防部称以台制华必付沉重代价"
    for title in (political, military, taiwan):
        assert _sensitive_topic_hits(title)
        assert _publishability_status(title, [], False, True, True) == "需人工确认"

    assert _publishability_status("个人成长复盘方法", [], False, True, False) == "未研判"
    assert _publishability_status("个人成长复盘方法", [], False, None, True) == "未研判"
    assert _publishability_status("个人成长复盘方法", ["敏感红线"], False, True, True) == "不可发布"
    assert _publishability_status("个人成长复盘方法", [], False, True, True) == "可发布"


def test_hot_topic_timestamps_are_emitted_with_explicit_utc_offset():
    from datetime import timezone
    from src.core.hot_topics import _api_utc, _parse_feed_ts

    assert _api_utc("2026-10-30T06:58:43") == "2026-10-30T06:58:43+00:00"
    assert _api_utc("2026-10-30T14:58:43+08:00") == "2026-10-30T14:58:43+08:00"
    parsed = _parse_feed_ts("2026-10-30T14:58:43+08:00")
    assert parsed == datetime(2026, 10, 30, 6, 58, 43)


def test_hot_source_dns_resolution_has_a_hard_timeout(monkeypatch):
    import threading
    from src.core.safe_http import SafeFetchError, _resolve_public_url

    blocked = threading.Event()
    monkeypatch.setattr(
        "src.core.safe_http.socket.getaddrinfo",
        lambda *args, **kwargs: (blocked.wait(30) and []),
    )
    started = __import__("time").monotonic()
    with pytest.raises(SafeFetchError, match="解析超时"):
        _resolve_public_url("https://slow-dns.example/feed")
    assert __import__("time").monotonic() - started < 6


def test_hot_source_fetch_rejects_private_destinations(monkeypatch):
    with pytest.raises(ValueError, match="内网|本机|保留"):
        _fetch_public_text("http://127.0.0.1/internal", headers={}, timeout=1)

    monkeypatch.setattr(
        "src.core.safe_http.socket.getaddrinfo",
        lambda host, port, **kwargs: [
            (__import__("socket").AF_INET, __import__("socket").SOCK_STREAM, 6, "", ("192.168.1.10", port))
        ],
    )
    with pytest.raises(ValueError, match="内网|本机|保留"):
        _fetch_public_text("https://private-feed.example/rss", headers={}, timeout=1)


def test_get_does_not_collect(client, monkeypatch):
    tc, conn = client
    called = []
    monkeypatch.setattr(HotTopicCollector, "refresh", lambda self: called.append(1) or {})
    body = tc.get("/api/hot_topics").json()
    assert called == []                    # 请求里不再同步抓取
    assert body["items"] == [] and "refresh_job" in body


def test_refresh_endpoint_runs_in_background(client, monkeypatch):
    tc, _ = client
    started = []
    monkeypatch.setattr(hot_topic_service, "trigger_background",
                        lambda cfg, trigger="manual", tenant_id=None: started.append((trigger, tenant_id)) or {"started": True, "tenant_id": tenant_id})
    r = tc.post("/api/hot_topics/refresh")
    assert r.status_code == 202 and started == [("manual", "default")]


def test_research_endpoint_rejects_without_llm(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr("src.core.hot_topic_matcher.default_llm_fn", lambda cfg: None)
    r = tc.post("/api/hot_topics/research")
    assert r.status_code == 409
    assert "大模型未启用" in r.json()["detail"]


def test_research_endpoint_starts_background(client, monkeypatch):
    tc, _ = client
    monkeypatch.setattr("src.core.hot_topic_matcher.default_llm_fn", lambda cfg: lambda s, u: ("", {}))
    started = []
    monkeypatch.setattr(hot_topic_service, "trigger_research",
                        lambda cfg, tenant_id=None: started.append(tenant_id) or {"started": True, "running": True, "tenant_id": tenant_id})
    r = tc.post("/api/hot_topics/research")
    assert r.status_code == 202 and started == ["default"]
    body = tc.get("/api/hot_topics").json()
    assert "research_job" in body and "research_ready" in body


def test_upsert_rolls_current_board_only(cfg, conn):
    """采集是「当前榜单快照」：本轮没再上榜的旧热点直接清理，不累积。"""
    c = HotTopicCollector(cfg, conn)
    def batch(title):
        return [{"id": topic_id("weibo", title), "title": title, "raw_url": "https://x",
                 "heat_score": 100, "heat_unit": "热度", "time_sensitive": None}]
    c._upsert("weibo", batch("三天前的旧热点"))
    c._upsert("weibo", batch("今天的新热点"))
    titles = {r["title"] for r in conn.execute(
        "SELECT title FROM hot_topics WHERE evidence_status = 'collected'").fetchall()}
    assert titles == {"今天的新热点"}


def test_keyword_mode_never_scores_unmatched_as_zero(cfg, conn):
    _insert(conn, "a", "个人成长 新方法分享")
    _insert(conn, "b", "某地天气晴")
    m = HotTopicMatcher(cfg, conn, use_default_llm=False)
    rep = m.run()
    assert rep["keyword"] == 2
    assert rep["unjudged"] >= 1
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM hot_topics").fetchall()}
    assert dimension_match_of(rows["a"], m.version) is True
    assert rows["b"]["match_score"] is None
    assert dimension_match_of(rows["b"], m.version) is None   # 未命中 ≠ 无关
    assert m.pending() == []                                   # 已检查过，不重复处理


def test_llm_mode_scores_and_flags_forbidden(cfg, conn):
    _insert(conn, "a", "个人成长 复盘方法分享")
    _insert(conn, "b", "明星八卦")
    _insert(conn, "c", "国际新闻")

    def fake_llm(system, user):
        assert "账号定位关键词" in user and "个人成长 复盘方法分享" in user
        return json.dumps({"items": [
            {"id": "a", "score": 86, "dimension": "", "angle": "复盘方法", "reason": "成长+方法", "forbidden": False},
            {"id": "b", "score": 70, "dimension": "", "angle": "", "reason": "", "forbidden": True},
            {"id": "c", "score": 12, "dimension": "", "angle": "", "reason": "无关", "forbidden": False},
        ]}), {"prompt_tokens": 10, "completion_tokens": 5}

    m = HotTopicMatcher(cfg, conn, llm_fn=fake_llm)
    rep = m.run()
    assert rep["llm"] == 3
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM hot_topics").fetchall()}
    assert dimension_match_of(rows["a"], m.version) is True
    assert dimension_match_of(rows["b"], m.version) is False   # 禁止方向优先于分数
    assert dimension_match_of(rows["c"], m.version) is False
    assert conn.execute("SELECT COUNT(*) FROM ai_calls WHERE step='hot_match'").fetchone()[0] == 1


def test_llm_batch_failure_keeps_pending(cfg, conn):
    _insert(conn, "a", "AI 新闻")

    def boom(system, user):
        raise RuntimeError("timeout")

    m = HotTopicMatcher(cfg, conn, llm_fn=boom)
    assert m.run()["failed_batches"] == 1
    assert conn.execute("SELECT match_version FROM hot_topics WHERE id='a'").fetchone()[0] is None


def test_overview_shows_latest_batch_and_match_fields(cfg, conn, monkeypatch):
    monkeypatch.setattr("src.core.hot_topic_matcher.default_llm_fn", lambda cfg: None)
    _insert(conn, "old", "个人成长 旧闻", batch="2026-10-27T10:00:00")
    _insert(conn, "new", "个人成长 新闻", batch="2026-10-28T10:00:00")
    HotTopicMatcher(cfg, conn, use_default_llm=False).run()
    data = HotTopicCollector(cfg, conn).overview()
    ids = [i["id"] for i in data["items"]]
    assert ids == ["new"]                                  # 旧批次不再展示
    item = data["items"][0]
    assert item["dimension_match"] is True and item["match_score"] == pytest.approx(MATCH_THRESHOLD)
    assert data["match"]["matched"] == 1


def test_trend_labels():
    """趋势辅助判断：新上榜看同源中位数，老条目看上轮/峰值回撤。"""
    from src.core.hot_topics import _trend_of

    # 首次上榜 + 热度是同源中位数 2 倍 → 爆发
    assert _trend_of({"heat": 100, "heat_prev": None, "heat_peak": 100, "seen_count": 1}, 50)[0] == "爆发"
    # 首次上榜但热度一般 → 平稳
    assert _trend_of({"heat": 50, "heat_prev": None, "heat_peak": 50, "seen_count": 1}, 50)[0] == "平稳"
    # 连续在榜仍在峰值 90% 以上 → 持续爆发
    assert _trend_of({"heat": 98, "heat_prev": 100, "heat_peak": 100, "seen_count": 3}, 40)[0] == "持续爆发"
    # 较峰值回撤 40% 以上 → 已降温
    assert _trend_of({"heat": 55, "heat_prev": 80, "heat_peak": 100, "seen_count": 3}, 40)[0] == "已降温"
    # 比上轮降了 10%+ 但未到回撤线 → 降温中
    assert _trend_of({"heat": 80, "heat_prev": 100, "heat_peak": 100, "seen_count": 2}, 40)[0] == "降温中"
    # 热度 NULL 不编趋势（R3）
    label, reason = _trend_of({"heat": None, "heat_prev": None, "heat_peak": None, "seen_count": 1}, None)
    assert label == "平稳" and "无法判断" in reason


def test_upsert_tracks_prev_and_peak(cfg, conn):
    """第二轮采集记录上轮热度与峰值，供趋势判断使用。"""
    c = HotTopicCollector(cfg, conn)
    item = [{"id": "t1", "title": "某热点", "raw_url": "https://x",
             "heat_score": 100, "heat_unit": "热度", "time_sensitive": None}]
    c._upsert("60s.viki.moe/weibo", "weibo", item)
    item[0]["heat_score"] = 70
    c._upsert("60s.viki.moe/weibo", "weibo", item)
    row = conn.execute("SELECT heat_score, heat_prev, heat_peak, seen_count FROM hot_topics WHERE id='t1'").fetchone()
    assert row["heat_score"] == 70 and row["heat_prev"] == 100 and row["heat_peak"] == 100
    assert row["seen_count"] == 2


def test_run_once_is_single_flight(cfg, conn, monkeypatch):
    hot_topic_service.reset_for_tests()
    monkeypatch.setattr(HotTopicCollector, "refresh",
                        lambda self, on_progress=None: {"weibo": "collected"})
    res = hot_topic_service.run_once(cfg, conn, trigger="test")
    assert res["running"] is False and res["report"] == {"weibo": "collected"}
    assert res["progress"]["stage"] == "done" and res["progress"]["percent"] == 100
    hot_topic_service._STATE["running"] = True
    try:
        assert hot_topic_service.run_once(cfg, conn)["skipped"] is True
    finally:
        hot_topic_service.reset_for_tests()


def test_refresh_reports_progress(cfg, conn, monkeypatch):
    """采集过程按数据源上报进度，供前端轮询展示（与页面是否停留无关）。"""
    events = []
    c = HotTopicCollector(cfg, conn)
    monkeypatch.setattr(c, "_fetch_platform",
                        lambda *a, **k: [{"id": f"hot-{a[0]}", "title": f"{a[0]} 热点",
                                          "raw_url": "https://x", "heat_score": 100,
                                          "heat_unit": "热度", "time_sensitive": None}])
    c.aggregate_sources = [{"name": "60s.viki.moe", "platforms": ["weibo", "douyin"]}]
    c.custom_sources = []
    def capture(update):
        events.append(update)
        if update["stage"] == "fetching" and update["message"].startswith("已完成并保存 1/"):
            # 至少一个来源完成时，其结果必须已经落库，不再等待所有来源结束。
            assert conn.execute("SELECT COUNT(*) FROM hot_topics WHERE evidence_status='collected'").fetchone()[0] >= 1

    c.refresh(on_progress=capture)
    stages = [e["stage"] for e in events]
    assert stages[0] == "fetching" and stages[-1] == "dedup"
    fetch_done = [e for e in events if e["stage"] == "fetching"][-1]
    assert fetch_done["percent"] <= 85
    assert [s["status"] for s in fetch_done["sources"]] == ["collected", "collected"]
    assert {s["items"] for s in fetch_done["sources"]} == {1}


def test_hot_topic_background_state_recovers_when_database_open_fails(cfg, monkeypatch):
    from src.core.db import Database

    def fail_connect(self):
        raise OSError("simulated tenant database unavailable")

    monkeypatch.setattr(Database, "connect", fail_connect)
    hot_topic_service.reset_for_tests()

    refresh = hot_topic_service.run_once(cfg, tenant_id="tenant-a")
    assert refresh["running"] is False
    assert "simulated tenant database unavailable" in refresh["error"]

    research = hot_topic_service.run_research(cfg, tenant_id="tenant-a")
    assert research["running"] is False
    assert "simulated tenant database unavailable" in research["error"]
    hot_topic_service.reset_for_tests()

def test_manual_research_reports_failed_batch_items_as_remaining_pending(cfg, conn, monkeypatch):
    """手动研判的 remaining_pending 必须包含整批失败的真实条目数。"""
    for index in range(4):
        _insert(
            conn, f"research-{index}", f"个人成长方法 {index}",
            source=f"research-source-{index}",
        )

    def failed_llm(system, user):
        raise RuntimeError("simulated batch timeout")

    monkeypatch.setattr(
        "src.core.hot_topic_matcher.default_llm_fn",
        lambda config: failed_llm,
    )
    hot_topic_service.reset_for_tests()
    failed = hot_topic_service.run_research(cfg, conn, tenant_id="default")
    assert failed["report"]["failed_batches"] == 1
    assert failed["report"]["remaining_pending"] == 4

    def successful_llm(system, user):
        import re
        ids = re.findall(r'"id":"([^"]+)"', user)
        return json.dumps({"items": [
            {"id": item_id, "score": 85, "dimension": "", "angle": "成长方法",
             "reason": "与账号定位相关", "forbidden": False}
            for item_id in ids
        ]}), {}

    monkeypatch.setattr(
        "src.core.hot_topic_matcher.default_llm_fn",
        lambda config: successful_llm,
    )
    recovered = hot_topic_service.run_research(cfg, conn, tenant_id="default")
    assert recovered["report"]["failed_batches"] == 0
    assert recovered["report"]["remaining_pending"] == 0
    hot_topic_service.reset_for_tests()

