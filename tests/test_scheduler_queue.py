from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.core.scheduler import _invoke
from src.core.task_queue import GlobalTaskQueue


def test_tenant_scheduler_platform_jobs_are_enqueued_globally(tmp_path):
    queue = GlobalTaskQueue(tmp_path / "shared" / "system.db")
    queue.initialize()
    engine = SimpleNamespace(task_queue=queue, tenant_id="tenant-b")

    asyncio.run(_invoke(engine, "_job_daily_ops"))

    task = queue.claim_next("worker-test")
    assert task is not None
    assert task.task_type == "scheduler_job"
    assert task.tenant_id == "tenant-b"
    assert task.aggregate_id == "_job_daily_ops"
    assert task.payload == {"method": "_job_daily_ops", "args": []}


def test_one_time_data_fetch_jobs_keep_draft_and_attempt_identity(tmp_path):
    queue = GlobalTaskQueue(tmp_path / "shared" / "system.db")
    queue.initialize()
    engine = SimpleNamespace(task_queue=queue, tenant_id="tenant-c")

    asyncio.run(_invoke(engine, "_job_data_fetch", "draft-123", 2))

    task = queue.claim_next("worker-test")
    assert task is not None
    assert task.task_type == "scheduler_job"
    assert task.tenant_id == "tenant-c"
    assert task.aggregate_id == "_job_data_fetch:draft-123:2"
    assert task.payload == {
        "method": "_job_data_fetch",
        "args": ["draft-123", 2],
    }


def test_delayed_metrics_fetch_is_persisted_in_global_queue(tmp_path):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from src.core.scheduler import SchedulerEngine

    queue = GlobalTaskQueue(tmp_path / "shared" / "system.db")
    queue.initialize()
    engine = SimpleNamespace(task_queue=queue, tenant_id="tenant-z")
    run_at = datetime.now(timezone.utc) + timedelta(hours=2)

    SchedulerEngine.schedule_data_fetch(engine, "draft-xyz", run_at, attempt=2)

    task = queue.find_latest(
        tenant_id="tenant-z",
        task_type="scheduler_job",
        aggregate_id="_job_data_fetch:draft-xyz:2",
    )
    assert task is not None
    assert task.status == "queued"
    assert task.payload == {
        "method": "_job_data_fetch",
        "args": ["draft-xyz", 2],
    }
    conn = queue._connect()
    try:
        row = conn.execute(
            "SELECT available_at FROM global_task_queue WHERE task_id=?",
            (task.task_id,),
        ).fetchone()
    finally:
        conn.close()
    assert datetime.fromisoformat(row["available_at"]) == run_at.astimezone(timezone.utc)
