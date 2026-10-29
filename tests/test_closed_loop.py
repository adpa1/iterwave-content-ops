"""闭环卡点回归：发布识别 → 数据采集 → 复盘 → 目标进度 / 停止条件。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from src.adapters.base import FetchStatus, MetricsSnapshot, Platform
from src.core.goal_progress import GoalProgressUpdater
from src.core.scheduler import SchedulerEngine
from src.core.state_machine import DraftStateMachine, RoundStateMachine


class _SeenAdapter:
    """平台作品列表里能找到 → 视为已发布。"""

    def __init__(self, status=FetchStatus.SUCCESS, platform=Platform.XHS):
        self.status = status
        self.platform = platform

    async def fetch_metrics(self, *_a, **_k):
        return MetricsSnapshot(platform=self.platform, fetch_status=self.status,
                               views=100, likes=5, comments=1, error_msg=None if self.status == FetchStatus.SUCCESS else "x")


def _engine(cfg, conn, adapters):
    eng = SchedulerEngine(cfg, Path(cfg.paths.db_file))
    eng.wire(conn=conn, adapters=adapters, sm_draft=DraftStateMachine(conn), sm_round=RoundStateMachine(conn))
    return eng


def test_publish_polling_sql_and_schedules_fetch(cfg, conn, seed, monkeypatch):
    """轮询 SQL 不再查不存在的 drafts.round_id；识别发布后注册数据采集。"""
    monkeypatch.setattr("src.core.login_manager.has_login_state", lambda _p: True)
    r = seed.round(status="awaiting_publish")
    d = seed.draft(r, draft_status="draft_saved", fetch_status=None)
    eng = _engine(cfg, conn, {Platform.XHS: _SeenAdapter()})
    asyncio.run(eng._job_publish_polling())
    assert conn.execute("SELECT status FROM drafts WHERE id = ?", (d,)).fetchone()[0] == "user_published"
    assert any(j.id == f"data_fetch_{d}_a1" for j in eng.scheduler.get_jobs())


def test_round_review_waits_until_remaining_saved_drafts_expire(cfg, conn, seed):
    now = datetime.now()
    round_id = seed.round(status="collecting_metrics")
    conn.execute(
        "UPDATE spiral_rounds SET collecting_metrics_started_at = ? WHERE id = ?",
        ((now - timedelta(hours=100)).isoformat(), round_id),
    )
    # 至少一篇已成功发布且完成采集，正常情况下轮次具备复盘条件。
    seed.draft(
        round_id,
        platform="xhs",
        draft_status="published",
        saved_at=(now - timedelta(hours=140)).isoformat(),
        fetch_status="success",
        views=100,
        likes=5,
        collects=2,
        comments=1,
        topic_id="published-work",
    )
    # 但另一篇平台草稿尚未发布，单篇 120h 超时仍未到，不能先结束轮次。
    pending_id = seed.draft(
        round_id,
        platform="douyin",
        draft_status="draft_saved",
        saved_at=(now - timedelta(hours=80)).isoformat(),
        fetch_status=None,
        topic_id="still-publishable",
    )

    eng = _engine(cfg, conn, {})
    asyncio.run(eng._job_round_review())
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == "collecting_metrics"

    # 即使轮次的 72h collecting_metrics 阶段超时，也优先等待未到期的单篇发布确认。
    asyncio.run(eng._job_round_timeout_sweeper())
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == "collecting_metrics"
    assert conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (pending_id,)
    ).fetchone()[0] == "draft_saved"

    # 单篇 72h + 48h 超时到期后，系统将其标为 expired，轮次才可进入复盘并部分收口。
    conn.execute(
        "UPDATE drafts SET saved_at = ? WHERE id = ?",
        ((now - timedelta(hours=121)).isoformat(), pending_id),
    )
    conn.commit()
    asyncio.run(eng._job_round_timeout_sweeper())
    assert conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (pending_id,)
    ).fetchone()[0] == "expired"
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == "analyzing"

    asyncio.run(eng._job_round_review())
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id = ?", (round_id,)
    ).fetchone()[0] == "partial_completed"


def test_manual_confirm_path_schedules_fetch(cfg, conn, seed):
    """手动确认与轮询走同一入口：必须注册数据采集（之前手动确认后数据永远不来）。"""
    r = seed.round(status="awaiting_publish")
    d = seed.draft(r, draft_status="draft_saved", fetch_status=None)
    eng = _engine(cfg, conn, {})
    assert eng.on_user_published(d, via="用户确认发布", note_url="https://x.com/n/1")
    assert any(j.id == f"data_fetch_{d}_a1" for j in eng.scheduler.get_jobs())
    url = conn.execute("SELECT platform_note_url FROM drafts WHERE id = ?", (d,)).fetchone()[0]
    assert url == "https://x.com/n/1"


def test_backfill_lost_fetch_job(cfg, conn, seed):
    """应用离线错过的采集任务会被补登记。"""
    r = seed.round(status="collecting_metrics")
    d = seed.draft(r, draft_status="user_published", fetch_status=None)
    old = (datetime.now() - timedelta(hours=48)).isoformat()
    conn.execute("UPDATE drafts SET user_confirmed_at = ? WHERE id = ?", (old, d))
    conn.commit()
    eng = _engine(cfg, conn, {})
    assert eng.backfill_data_fetch() == 1
    assert eng.backfill_data_fetch() == 0  # 已有待执行任务，不重复登记


def test_fetch_retries_exhausted_marks_failed(cfg, conn, seed):
    r = seed.round(status="collecting_metrics")
    d = seed.draft(r, draft_status="user_published", fetch_status=None)
    eng = _engine(cfg, conn, {Platform.XHS: _SeenAdapter(status=FetchStatus.FAILED)})
    asyncio.run(eng._job_data_fetch(d, attempt=3))
    assert conn.execute("SELECT status FROM drafts WHERE id = ?", (d,)).fetchone()[0] == "failed"


def test_douyin_interaction_rate_without_collects():
    snap = MetricsSnapshot(platform=Platform.DOUYIN, fetch_status=FetchStatus.SUCCESS,
                           views=200, likes=10, comments=10)
    assert snap.interaction_rate == 0.1
    xhs = MetricsSnapshot(platform=Platform.XHS, fetch_status=FetchStatus.SUCCESS,
                          views=200, likes=10, comments=10)
    assert xhs.interaction_rate is None  # 小红书仍要求收藏数


def _goal(conn, gid, metric, platform="xhs", source="account_stats", baseline=1000.0, target=100.0):
    conn.execute(
        "INSERT INTO goals (id, goal_text, target_metric, target_value, target_platform, metric_scope,"
        " baseline_value, baseline_at, metric_period_start, metric_period_end, data_source, status)"
        " VALUES (?, ?, ?, ?, ?, 'delta', ?, '2026-10-01', '2026-10-01', '2099-12-31', ?, 'active')",
        (gid, gid, metric, target, platform, baseline, source),
    )
    conn.commit()


def test_followers_goal_from_account_snapshot(cfg, conn):
    _goal(conn, "g-f", "followers_gained")
    conn.execute(
        "INSERT INTO account_snapshots (platform, followers, captured_at, source) VALUES ('xhs', 1120, ?, 'manual')",
        (datetime.now().isoformat(),),
    )
    conn.commit()
    GoalProgressUpdater(cfg, conn).update_all()
    row = conn.execute("SELECT current_value, status FROM goals WHERE id = 'g-f'").fetchone()
    assert row["current_value"] == 1120
    assert row["status"] == "completed"  # (1120-1000)/100 = 120%


def test_manual_goal_not_overwritten(cfg, conn):
    _goal(conn, "g-m", "views", source="manual", baseline=0.0, target=1000.0)
    conn.execute("UPDATE goals SET current_value = 300 WHERE id = 'g-m'")
    conn.commit()
    GoalProgressUpdater(cfg, conn).update_all()
    assert conn.execute("SELECT current_value FROM goals WHERE id = 'g-m'").fetchone()[0] == 300
