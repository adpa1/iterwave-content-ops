"""R7 / R9：轮次 9 态 + 单篇 8 态，状态机与 DB CHECK 必须一致。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from src.core.state_machine import (
    DRAFT_STATUSES,
    DRAFT_TRANSITIONS,
    ROUND_STATUSES,
    ROUND_TRANSITIONS,
    DraftStateMachine,
    RoundStateMachine,
)


# ---------------------------------------------------------------------
# 状态集合完整性
# ---------------------------------------------------------------------

def test_round_has_nine_states() -> None:
    assert len(ROUND_STATUSES) == 9
    assert set(ROUND_STATUSES) == set(ROUND_TRANSITIONS)


def test_draft_has_eight_states_including_expired() -> None:
    assert len(DRAFT_STATUSES) == 8
    assert "expired" in DRAFT_STATUSES
    assert set(DRAFT_STATUSES) == set(DRAFT_TRANSITIONS)


def _check_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    assert row is not None and row[0], f"{table} 表不存在"
    return row[0]


def test_db_check_constraint_matches_round_states(conn: sqlite3.Connection) -> None:
    """DB CHECK 必须包含全部 9 个轮次状态（新增状态三处同步）。"""
    ddl = _check_sql(conn, "spiral_rounds")
    missing = [s for s in ROUND_STATUSES if f"'{s}'" not in ddl]
    assert missing == [], f"spiral_rounds CHECK 缺少状态：{missing}"


def test_db_check_constraint_matches_draft_states(conn: sqlite3.Connection) -> None:
    """DB CHECK 必须包含全部 8 个草稿状态（尤其 expired）。"""
    ddl = _check_sql(conn, "drafts")
    missing = [s for s in DRAFT_STATUSES if f"'{s}'" not in ddl]
    assert missing == [], f"drafts CHECK 缺少状态：{missing}"


def test_db_rejects_unknown_status(conn: sqlite3.Connection, seed) -> None:
    """DB 层兜底：写非法状态直接抛 IntegrityError。"""
    r = seed.round()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE spiral_rounds SET status = 'bogus' WHERE id = ?", (r,))


# ---------------------------------------------------------------------
# 迁移合法性
# ---------------------------------------------------------------------

def test_round_transition_happy_path_and_illegal_jump(conn: sqlite3.Connection, seed) -> None:
    r = seed.round()
    sm = RoundStateMachine(conn)
    assert sm.transition(r, "producing") is True
    assert sm.transition(r, "collecting_metrics") is False   # 不能跳过 awaiting_publish
    assert sm.transition(r, "awaiting_publish") is True
    # 同状态幂等
    assert sm.transition(r, "awaiting_publish") is True
    assert conn.execute("SELECT status FROM spiral_rounds WHERE id = ?", (r,)).fetchone()[0] \
        == "awaiting_publish"


def test_round_transition_writes_local_time_log(conn: sqlite3.Connection, seed) -> None:
    """R5：流转日志用本地时间，且 producing 起点独立落库（R8）。"""
    r = seed.round()
    sm = RoundStateMachine(conn)
    sm.transition(r, "producing", "测试")
    row = conn.execute(
        "SELECT transition_log, producing_started_at FROM spiral_rounds WHERE id = ?", (r,)
    ).fetchone()
    log, started = row[0], row[1]
    assert log and "producing" in log
    assert started is not None
    # 本地时间与 now() 的差应远小于 1 天（若是 UTC 会差 8 小时）
    delta = abs((datetime.now() - datetime.fromisoformat(started)).total_seconds())
    assert delta < 300, f"producing_started_at 疑似 UTC：{started}"


def test_draft_transition_rejects_expired_from_pending(conn: sqlite3.Connection, seed) -> None:
    r = seed.round()
    d = seed.draft(r, draft_status="draft_pending", fetch_status=None)
    sm = DraftStateMachine(conn)
    assert sm.transition(d, "expired") is False      # 只有 draft_saved 才能 expired
    assert sm.transition(d, "draft_saved") is True
    assert sm.transition(d, "expired", "超时未确认") is True


def test_draft_transition_is_idempotent(conn: sqlite3.Connection, seed) -> None:
    """R16：同状态重复设置不报错（幂等）。"""
    r = seed.round()
    d = seed.draft(r, draft_status="draft_saved", fetch_status=None)
    sm = DraftStateMachine(conn)
    assert sm.transition(d, "draft_saved") is True
    assert sm.transition(d, "draft_saved") is True


# ---------------------------------------------------------------------
# R8 + R9：单篇超时按各自 saved_at 独立计时
# ---------------------------------------------------------------------

def test_publish_confirm_timeout_is_per_draft(conn: sqlite3.Connection, seed) -> None:
    """A 已超时 → expired；B 刚存 → 保持 draft_saved（互不牵连）。"""
    r = seed.round(status="collecting_metrics")
    old_at = (datetime.now() - timedelta(hours=72 + 48 + 1)).isoformat()
    fresh_at = (datetime.now() - timedelta(hours=1)).isoformat()
    a = seed.draft(r, draft_status="draft_saved", saved_at=old_at, fetch_status=None, topic_id="ta")
    b = seed.draft(r, draft_status="draft_saved", saved_at=fresh_at, fetch_status=None, topic_id="tb")

    expired = DraftStateMachine(conn).check_expired_drafts(
        timeout_hours=72, grace_hours=48,
    )
    assert expired == [a]
    assert conn.execute("SELECT status FROM drafts WHERE id = ?", (b,)).fetchone()[0] == "draft_saved"


def test_legacy_draft_without_saved_at_uses_created_at_for_expiry(conn: sqlite3.Connection, seed) -> None:
    """迁移/旧数据 saved_at 为空时不能永久留在 draft_saved。"""
    r = seed.round(status="collecting_metrics")
    d = seed.draft(r, draft_status="draft_saved", saved_at=None, fetch_status=None)
    old_created = (datetime.now() - timedelta(hours=72 + 48 + 1)).isoformat()
    conn.execute(
        "UPDATE drafts SET saved_at = NULL, created_at = ? WHERE id = ?",
        (old_created, d),
    )
    conn.commit()

    expired = DraftStateMachine(conn).check_expired_drafts(
        timeout_hours=72, grace_hours=48,
    )
    assert d in expired
    assert conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (d,)
    ).fetchone()[0] == "expired"


def test_timeout_window_is_timeout_plus_grace(conn: sqlite3.Connection, seed) -> None:
    """不到 timeout+grace 不 expired；改了 grace 立刻影响判定（参数真正生效）。"""
    r = seed.round(status="collecting_metrics")
    saved = (datetime.now() - timedelta(hours=80)).isoformat()   # > 72 但 < 72+48
    d = seed.draft(r, draft_status="draft_saved", saved_at=saved, fetch_status=None)
    sm = DraftStateMachine(conn)
    assert sm.check_expired_drafts(timeout_hours=72, grace_hours=48) == []
    assert sm.check_expired_drafts(timeout_hours=72, grace_hours=5) == [d]


# ---------------------------------------------------------------------
# R8：六个超时参数各自独立，不能被一个总超时覆盖
# ---------------------------------------------------------------------

def test_timeout_defaults_match_spec(cfg) -> None:
    t = cfg.timeouts
    assert t.round_timeout_hours == 48                  # 仅 producing
    assert t.awaiting_publish_timeout_hours == 120      # 仅 awaiting_publish
    assert t.publish_confirm_timeout_hours == 72        # per-draft
    assert t.publish_confirm_grace_hours == 48          # per-draft
    assert t.collecting_metrics_timeout_hours == 72     # 仅 collecting_metrics
    assert t.metrics_initial_delay_hours == 24          # user_published 后延迟
    # 阶段超时不得互相覆盖：producing 必须短于 awaiting_publish
    assert t.round_timeout_hours < t.awaiting_publish_timeout_hours


def test_timeout_validator_rejects_overlap() -> None:
    """R8 红线：round_timeout 覆盖到 awaiting_publish 会被配置校验拦下。"""
    from src.core.config import TimeoutsConfig
    with pytest.raises(ValueError, match="awaiting_publish"):
        TimeoutsConfig(round_timeout_hours=200, awaiting_publish_timeout_hours=120)


def test_each_timeout_is_used_independently(conn: sqlite3.Connection, seed) -> None:
    """改 publish_confirm 只影响单篇草稿，不影响轮次阶段超时判定。"""
    r = seed.round(status="collecting_metrics")
    started = (datetime.now() - timedelta(hours=100)).isoformat()
    conn.execute(
        "UPDATE spiral_rounds SET collecting_metrics_started_at = ? WHERE id = ?", (started, r)
    )
    conn.commit()
    d = seed.draft(r, draft_status="draft_saved",
                   saved_at=(datetime.now() - timedelta(hours=10)).isoformat(),
                   fetch_status=None)
    sm = DraftStateMachine(conn)
    # 单篇只过期了 10h，远未到 72+48 → 不 expired
    assert sm.check_expired_drafts(timeout_hours=72, grace_hours=48) == []
    # 但 collecting_metrics 阶段已 100h > 72h → 阶段超时成立（互不影响）
    assert sm.collect_metrics_phase_timeout(r) is True
    assert conn.execute("SELECT status FROM drafts WHERE id = ?", (d,)).fetchone()[0] \
        == "draft_saved"
