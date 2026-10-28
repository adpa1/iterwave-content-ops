"""运营总览 / 目标页适配：达成率口径、总体进度汇总、action_items、platform_progress。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src.api import main as api_main
from src.core.goal_progress import GoalProgressUpdater
from src.core.notify import Notifier
from src.core.plan_version import PlanVersionManager
from src.core.state_machine import DraftStateMachine


@pytest.fixture()
def client(cfg, conn, seed):
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({
        "cfg": cfg,
        "conn": conn,
        "sm_draft": DraftStateMachine(conn),
        "plan_version_manager": PlanVersionManager(cfg, conn),
        "notifier": Notifier(cfg, conn),
    })
    app = api_main._build_app()
    try:
        yield TestClient(app), conn
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)


def _insert_goal(conn, gid, *, metric="followers_gained", target=100.0, scope="delta",
                 baseline=0.0, current=None, platform="xhs", start="2026-10-01",
                 end="2026-12-31", status="active"):
    conn.execute(
        "INSERT INTO goals (id, goal_text, target_metric, target_value, target_platform, "
        "metric_scope, data_source, baseline_value, baseline_at, metric_period_start, "
        "metric_period_end, current_value, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (gid, f"目标 {gid}", metric, target, platform, scope, "account_stats",
         baseline, start, start, end, current, status),
    )
    conn.commit()


# ---------------------------------------------------------------- 达成率口径
def test_rate_delta_subtracts_baseline():
    assert GoalProgressUpdater.achievement_rate(150, 100, 200, "delta") == 25.0


def test_rate_absolute_ignores_baseline():
    # 互动率目标 4.0（总量口径），当前 3.8 → 95%，基线不参与
    assert GoalProgressUpdater.achievement_rate(3.8, 3.0, 4.0, "total") == pytest.approx(95.0)


def test_rate_null_components():
    assert GoalProgressUpdater.achievement_rate(None, 0, 100) is None
    assert GoalProgressUpdater.achievement_rate(10, None, 100, "delta") is None
    assert GoalProgressUpdater.achievement_rate(10, None, 100, "total") == 10.0
    assert GoalProgressUpdater.achievement_rate(10, 0, 0) is None


# ---------------------------------------------------------------- 总体进度
def test_summary_no_goals_is_null(client):
    tc, _ = client
    body = tc.get("/api/goals/summary").json()
    assert body["percent"] is None
    assert body["active_goals"] == 0


def test_summary_equal_weight_mean_clamped(client):
    tc, conn = client
    _insert_goal(conn, "g1", target=2000, current=862)                       # 43.1%
    _insert_goal(conn, "g2", target=80, current=200, platform="douyin")      # 250% → 截断 100
    _insert_goal(conn, "g3", target=100, current=None, platform="kuaishou")  # 待采集，不计入
    _insert_goal(conn, "g4", target=100, current=100, status="completed")    # 非 active，不计入
    body = tc.get("/api/goals/summary").json()
    assert body["active_goals"] == 3
    assert body["counted_goals"] == 2
    assert body["pending_goals"] == 1
    assert body["percent"] == pytest.approx((43.1 + 100) / 2, abs=0.1)
    assert body["method"] == "equal_weight_mean"
    assert body["period_end"] == "2026-12-31"


def test_goals_list_uses_scope(client):
    tc, conn = client
    _insert_goal(conn, "abs", metric="interaction_rate", target=4.0, scope="total",
                 baseline=3.0, current=3.8)
    g = next(x for x in tc.get("/api/goals").json() if x["id"] == "abs")
    assert g["achievement_rate"] == pytest.approx(95.0)


# ---------------------------------------------------------------- action_items
def test_action_items_goal_not_overdue_on_end_day(client):
    tc, conn = client
    today = datetime.now().date().isoformat()
    _insert_goal(conn, "today", end=today, current=10)
    ids = [i["id"] for i in tc.get("/api/action_items").json()["items"]]
    assert "goal:today" not in ids


def test_action_items_overdue_uses_same_rate_and_display_name(client):
    tc, conn = client
    past = (datetime.now() - timedelta(days=2)).date().isoformat()
    # delta：基线 900、当前 1000、目标 200 → 50%（旧逻辑 cur/target 会算成 500% 并漏报）
    _insert_goal(conn, "late", target=200, baseline=900, current=1000, end=past)
    _insert_goal(conn, "done", target=100, baseline=0, current=150, end=past)
    items = {i["id"]: i for i in tc.get("/api/action_items").json()["items"]}
    assert "goal:done" not in items
    late = items["goal:late"]
    assert "50%" in late["title"]
    assert late["detail"].startswith("小红书")
    assert late["platform"] == "xhs"


# ---------------------------------------------------------------- platform_progress
def test_platform_progress_returns_all_platforms_with_enabled(client):
    tc, _ = client
    body = tc.get("/api/platform_progress").json()
    assert set(body) == {"xhs", "douyin", "kuaishou", "wechat_channels"}
    enabled = set(api_main._g_ctx["cfg"].platforms.enabled_keys())
    for p, v in body.items():
        assert v["enabled"] is (p in enabled)


# ---------------------------------------------------------------- 比率类指标单位
def test_rate_metric_current_value_in_percent(client, conn):
    _, _ = client
    _insert_goal(conn, "ir", metric="interaction_rate", target=4.0, scope="total",
                 baseline=0.0, platform="xhs", start="2000-01-01", end="2999-12-31")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(metrics)").fetchall()}
    if not {"interaction_rate", "fetch_status", "fetched_at", "platform"} <= cols:
        pytest.skip("metrics 表结构不同")
    conn.execute("PRAGMA foreign_keys=OFF")
    required = [r for r in conn.execute("PRAGMA table_info(metrics)").fetchall() if r[3] and r[4] is None and not r[5]]
    vals = {"platform": "xhs", "interaction_rate": 0.038, "fetch_status": "success",
            "fetched_at": "2026-10-15T00:00:00"}
    for r in required:
        vals.setdefault(r[1], "x")
    conn.execute(f"INSERT INTO metrics ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})", list(vals.values()))
    conn.commit()
    row = conn.execute("SELECT * FROM goals WHERE id='ir'").fetchone()
    value = GoalProgressUpdater(api_main._g_ctx["cfg"], conn).update_goal(row)
    assert value == pytest.approx(3.8)
    assert GoalProgressUpdater.rate_for(conn.execute("SELECT * FROM goals WHERE id='ir'").fetchone()) == pytest.approx(95.0)
