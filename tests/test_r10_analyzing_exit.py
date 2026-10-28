"""R10：analyzing 必须有三条退出路径，不能只走 completed 一条。"""

from __future__ import annotations

import sqlite3

from src.core.state_machine import RoundStateMachine


def _exit(conn: sqlite3.Connection, round_id: int) -> str:
    return RoundStateMachine(conn).decide_analyzing_exit(round_id)


def test_path1_all_success_is_completed(conn: sqlite3.Connection, seed) -> None:
    r = seed.round(status="analyzing")
    seed.draft(r, draft_status="published", fetch_status="success")
    seed.draft(r, draft_status="published", fetch_status="partial", topic_id="t2")
    assert _exit(conn, r) == "completed"


def test_path2_partial_when_some_failed(conn: sqlite3.Connection, seed) -> None:
    """有 ≥1 published 成功 + 另有 failed/expired/cancelled → partial_completed。"""
    r = seed.round(status="analyzing")
    seed.draft(r, draft_status="published", fetch_status="success")
    seed.draft(r, draft_status="published", fetch_status="failed", topic_id="t2")
    assert _exit(conn, r) == "partial_completed"


def test_path2_partial_when_fetch_pending(conn: sqlite3.Connection, seed) -> None:
    """未到 success/partial 也不是全 failed → partial（不能当 completed）。"""
    r = seed.round(status="analyzing")
    seed.draft(r, draft_status="published", fetch_status="success")
    seed.draft(r, draft_status="published", fetch_status="pending", topic_id="t2")
    assert _exit(conn, r) == "partial_completed"


def test_path2_partial_when_draft_cancelled(conn: sqlite3.Connection, seed) -> None:
    """全部 published 采集成功，但另有 cancelled/expired 掉队篇目 → 仍为 partial_completed。

    这是 R10 第二路的关键场景：不能只看 published 的 fetch_status。
    """
    r = seed.round(status="analyzing")
    seed.draft(r, draft_status="published", fetch_status="success")
    seed.draft(r, draft_status="cancelled", fetch_status=None, topic_id="t2")
    seed.draft(r, draft_status="expired", fetch_status=None, topic_id="t3")
    assert _exit(conn, r) == "partial_completed"


def test_path2_partial_when_draft_failed_before_publish(conn: sqlite3.Connection, seed) -> None:
    """published 全部 success，但有一篇存草稿阶段就 failed → partial_completed。"""
    r = seed.round(status="analyzing")
    seed.draft(r, draft_status="published", fetch_status="success")
    seed.draft(r, draft_status="failed", fetch_status=None, topic_id="t2")
    assert _exit(conn, r) == "partial_completed"


def test_path3_all_failed_is_failed(conn: sqlite3.Connection, seed) -> None:
    r = seed.round(status="analyzing")
    seed.draft(r, draft_status="published", fetch_status="failed")
    seed.draft(r, draft_status="published", fetch_status="failed", topic_id="t2")
    assert _exit(conn, r) == "failed"


def test_no_published_draft_is_failed(conn: sqlite3.Connection, seed) -> None:
    r = seed.round(status="analyzing")
    seed.draft(r, draft_status="expired", fetch_status=None)
    assert _exit(conn, r) == "failed"


def test_exit_paths_are_all_reachable_from_analyzing(conn: sqlite3.Connection, seed) -> None:
    """三条目标状态都必须是 analyzing 的合法出边，且状态机真能落地。"""
    for expected in ("completed", "partial_completed", "failed"):
        r = seed.round(status="analyzing")
        if expected == "completed":
            seed.draft(r, draft_status="published", fetch_status="success")
        elif expected == "partial_completed":
            seed.draft(r, draft_status="published", fetch_status="success")
            seed.draft(r, draft_status="published", fetch_status="failed", topic_id="t2")
        else:
            seed.draft(r, draft_status="published", fetch_status="failed")
        sm = RoundStateMachine(conn)
        assert sm.decide_analyzing_exit(r) == expected
        assert sm.transition(r, expected, "test") is True
        assert conn.execute(
            "SELECT status FROM spiral_rounds WHERE id = ?", (r,)
        ).fetchone()[0] == expected
