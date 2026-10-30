"""调度器缺陷回归：kb_scan 列对齐 / data_fetch 重试 / 熔断真实触发。"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path

from src.adapters.base import CircuitBreakerReason, DraftResult, Platform
from src.core.scheduler import SchedulerEngine
from src.core.timezone import business_now
from src.core.scoring.topic_eval import TopicItem


class _FakeKb:
    def read(self):
        return (
            [
                TopicItem(id="kt1", title="标题一", dimension="AI教育", source="kb"),
                TopicItem(id="kt2", title="标题二", dimension="AI教育", source="kb"),
            ],
            {"total": 2},
        )


def _engine(cfg, conn) -> SchedulerEngine:
    eng = SchedulerEngine(cfg, Path(cfg.paths.db_file))
    eng.wire(conn=conn, adapters={}, kb_reader=_FakeKb())
    return eng


def test_kb_scan_inserts_without_missing_columns(conn: sqlite3.Connection, cfg) -> None:
    """topic_index 无 tags/summary 列，kb_scan 必须能成功插入（曾因列不存在必崩）。"""
    eng = _engine(cfg, conn)
    asyncio.run(eng._job_kb_scan())

    rows = conn.execute(
        "SELECT id, title, dimension, source FROM topic_index ORDER BY id"
    ).fetchall()
    assert [r["id"] for r in rows] == ["kt1", "kt2"]
    assert rows[0]["title"] == "标题一"


def test_metrics_retry_params_from_typed_config(cfg) -> None:
    """数据采集重试次数与间隔直接来自类型化配置。"""
    eng = SchedulerEngine(cfg, Path(cfg.paths.db_file))
    assert eng._metrics_retry_params() == (3, 30)
    cfg.timeouts.metrics_retry_attempts = 5
    cfg.timeouts.metrics_retry_interval_minutes = 45
    assert eng._metrics_retry_params() == (5, 45)


def test_retry_job_id_includes_attempt(cfg) -> None:
    """重试注册必须区分 attempt，否则 replace_existing 会覆盖掉首次任务。"""
    eng = SchedulerEngine(cfg, Path(cfg.paths.db_file))
    eng.schedule_data_fetch("d1", datetime.now(), attempt=1)
    eng.schedule_data_fetch("d1", datetime.now(), attempt=2)
    ids = {j.id for j in eng.scheduler.get_jobs()}
    assert {"data_fetch_d1_a1", "data_fetch_d1_a2"} <= ids


def test_missing_metrics_adapter_records_failure_and_retries(cfg, conn, seed) -> None:
    from datetime import timedelta
    from src.adapters.base import FetchStatus
    from src.core.state_machine import DraftStateMachine

    round_id = seed.round(status="collecting_metrics")
    draft_id = seed.draft(round_id, draft_status="user_published", fetch_status=None)
    conn.execute(
        "UPDATE drafts SET user_confirmed_at = ? WHERE id = ?",
        ((datetime.now() - timedelta(hours=48)).isoformat(), draft_id),
    )
    conn.commit()
    eng = SchedulerEngine(cfg, Path(cfg.paths.db_file))
    eng.wire(conn=conn, adapters={}, sm_draft=DraftStateMachine(conn))

    # attempt 1：缺少适配器也要形成 failed metrics，且注册 attempt 2。
    asyncio.run(eng._job_data_fetch(draft_id, attempt=1))
    metrics = conn.execute(
        "SELECT fetch_status, raw_response, error_msg FROM metrics WHERE draft_id = ?", (draft_id,)
    ).fetchone()
    assert metrics["fetch_status"] == FetchStatus.FAILED.value
    assert "适配器未启用" in metrics["error_msg"]
    assert conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0] == "user_published"
    assert f"data_fetch_{draft_id}_a2" in {j.id for j in eng.scheduler.get_jobs()}

    # attempt 2 继续重试；attempt 3 耗尽后明确结束，不再静默挂在 collecting。
    asyncio.run(eng._job_data_fetch(draft_id, attempt=2))
    assert f"data_fetch_{draft_id}_a3" in {j.id for j in eng.scheduler.get_jobs()}
    asyncio.run(eng._job_data_fetch(draft_id, attempt=3))
    assert conn.execute(
        "SELECT status FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0] == "failed"
    error = conn.execute(
        "SELECT error_msg FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()[0]
    assert "适配器未启用" in error


def test_backfill_requeues_when_only_failed_metrics_exist(cfg, conn, seed) -> None:
    """只存在 failed 采集记录不代表闭环完成；任务丢失后必须允许补登记。"""
    from datetime import timedelta

    round_id = seed.round(status="collecting_metrics")
    draft_id = seed.draft(round_id, draft_status="data_fetching", fetch_status=None)
    old = (datetime.now() - timedelta(hours=48)).isoformat()
    conn.execute("UPDATE drafts SET user_confirmed_at = ? WHERE id = ?", (old, draft_id))
    conn.execute(
        "INSERT INTO metrics (draft_id, content_id, platform, fetch_status, fetched_at, attempt) "
        "SELECT ?, id, 'xhs', 'failed', ?, 1 FROM contents WHERE round_id = ? LIMIT 1",
        (draft_id, datetime.now().isoformat(), round_id),
    )
    conn.commit()

    eng = SchedulerEngine(cfg, Path(cfg.paths.db_file))
    eng.wire(conn=conn, adapters={})
    assert eng.backfill_data_fetch() == 1
    assert f"data_fetch_{draft_id}_a1" in {j.id for j in eng.scheduler.get_jobs()}
    assert eng.backfill_data_fetch() == 0


def test_data_fetch_failure_schedules_retry(cfg, conn, seed) -> None:
    """采集失败 → 注册下一次 attempt（此前单次失败直接放弃，重试策略形同虚设）。"""
    from src.adapters.base import FetchStatus, MetricsSnapshot
    from src.core.state_machine import DraftStateMachine

    r = seed.round()
    draft_id = seed.draft(r, draft_status="user_published", fetch_status=None)

    # 当天已采集过 6 次（metrics.attempt=6）——不得被误当成作业重试次数
    conn.execute(
        "INSERT INTO metrics (draft_id, platform, fetch_status, fetched_at, attempt) "
        "VALUES (?, 'xhs', 'failed', ?, 6)",
        (draft_id, business_now(cfg).strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()

    class _Ad:
        async def fetch_metrics(self, *_a, **_k):
            return MetricsSnapshot(
                platform=Platform.XHS, fetch_status=FetchStatus.FAILED, error_msg="平台超时",
            )

    eng = SchedulerEngine(cfg, Path(cfg.paths.db_file))
    eng.wire(
        conn=conn, adapters={Platform.XHS: _Ad()},
        sm_draft=DraftStateMachine(conn),
    )
    asyncio.run(eng._job_data_fetch(draft_id, attempt=1))

    persisted_error = conn.execute(
        "SELECT error_msg FROM metrics WHERE draft_id = ?", (draft_id,)
    ).fetchone()[0]
    assert persisted_error == "平台超时"

    ids = {j.id for j in eng.scheduler.get_jobs()}
    assert f"data_fetch_{draft_id}_a2" in ids
    assert f"data_fetch_{draft_id}_a1" not in ids


def test_round_executor_reports_signal(cfg, conn) -> None:
    """round_executor._report_circuit_signal 真的调 trigger（R6 熔断不再形同虚设）。"""
    from src.core.round_executor import RoundExecutor

    class _Cb:
        def __init__(self) -> None:
            self.calls = []

        def trigger(self, platform, reason) -> None:
            self.calls.append((platform, reason))

    cb = _Cb()
    ex = RoundExecutor(cfg, conn, None, None, {}, None, None, circuit_breaker=cb)
    ex._report_circuit_signal(
        Platform.DOUYIN, DraftResult(success=False, cb_signal=CircuitBreakerReason.RATE_LIMIT)
    )
    ex._report_circuit_signal(Platform.DOUYIN, DraftResult(success=False, login_required=True))
    # 无信号的失败不应熔断
    ex._report_circuit_signal(Platform.DOUYIN, DraftResult(success=False))
    assert cb.calls == [
        (Platform.DOUYIN, CircuitBreakerReason.RATE_LIMIT),
        (Platform.DOUYIN, CircuitBreakerReason.LOGIN_EXPIRED),
    ]


def test_daily_ops_skips_when_globally_paused(cfg, conn) -> None:
    """全局软暂停 → daily_ops 直接返回，不建 task_run、不调 ops（R-6）。"""
    from src.core.ops.pause import set_ops_paused

    class _Ops:
        def __init__(self) -> None:
            self.calls = 0

        async def active_comment(self, *_a, **_k):
            self.calls += 1

        async def follow(self, *_a, **_k):
            self.calls += 1

    ops = _Ops()
    eng = SchedulerEngine(cfg, Path(cfg.paths.db_file))
    eng.wire(conn=conn, adapters={}, ops=ops)
    set_ops_paused(conn, True)

    asyncio.run(eng._job_daily_ops())

    assert ops.calls == 0
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
