"""热点：异步采集（GET 只读库）+ 标签匹配。"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.core import hot_topic_service
from src.core.hot_topic_matcher import HotTopicMatcher, MATCH_THRESHOLD, dimension_match_of
from src.core.hot_topics import HotTopicCollector


def _insert(conn, tid, title, *, batch="2026-10-28T10:00:00", source="60s.viki.moe/weibo", heat=999999):
    conn.execute(
        "INSERT INTO hot_topics (id, source, raw_url, discovered_at, evidence_status, title, "
        "heat_score, heat_unit, time_sensitive) VALUES (?,?,?,?, 'collected', ?, ?, '热度', NULL)",
        (tid, source, "https://x", batch, title, heat),
    )
    conn.commit()


@pytest.fixture()
def client(cfg, conn):
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({"cfg": cfg, "conn": conn})
    hot_topic_service.reset_for_tests()
    try:
        yield TestClient(api_main._build_app()), conn
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)


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
                        lambda cfg, trigger="manual": started.append(trigger) or {"started": True})
    r = tc.post("/api/hot_topics/refresh")
    assert r.status_code == 202 and started == ["manual"]


def test_keyword_mode_never_scores_unmatched_as_zero(cfg, conn):
    _insert(conn, "a", "AI教育 新工具上线")
    _insert(conn, "b", "某地天气晴")
    m = HotTopicMatcher(cfg, conn, use_default_llm=False)
    rep = m.run()
    assert rep["keyword"] == 2
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM hot_topics").fetchall()}
    assert dimension_match_of(rows["a"], m.version) is True
    assert rows["b"]["match_score"] is None
    assert dimension_match_of(rows["b"], m.version) is None   # 未命中 ≠ 无关
    assert m.pending() == []                                   # 已检查过，不重复处理


def test_llm_mode_scores_and_flags_forbidden(cfg, conn):
    _insert(conn, "a", "学生用AI写作业引发讨论")
    _insert(conn, "b", "明星八卦")
    _insert(conn, "c", "国际新闻")

    def fake_llm(system, user):
        assert "账号定位关键词" in user and "学生用AI写作业" in user
        return json.dumps({"items": [
            {"id": "a", "score": 86, "dimension": "", "angle": "AI 辅助学习边界", "reason": "教育+AI", "forbidden": False},
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
    _insert(conn, "old", "AI教育 旧闻", batch="2026-10-27T10:00:00")
    _insert(conn, "new", "AI教育 新闻", batch="2026-10-28T10:00:00")
    HotTopicMatcher(cfg, conn, use_default_llm=False).run()
    data = HotTopicCollector(cfg, conn).overview()
    ids = [i["id"] for i in data["items"]]
    assert ids == ["new"]                                  # 旧批次不再展示
    item = data["items"][0]
    assert item["dimension_match"] is True and item["match_score"] == pytest.approx(MATCH_THRESHOLD)
    assert data["match"]["matched"] == 1


def test_run_once_is_single_flight(cfg, conn, monkeypatch):
    hot_topic_service.reset_for_tests()
    monkeypatch.setattr(HotTopicCollector, "refresh", lambda self: {"weibo": "collected"})
    res = hot_topic_service.run_once(cfg, conn, trigger="test")
    assert res["running"] is False and res["report"] == {"weibo": "collected"}
    hot_topic_service._STATE["running"] = True
    try:
        assert hot_topic_service.run_once(cfg, conn)["skipped"] is True
    finally:
        hot_topic_service.reset_for_tests()
