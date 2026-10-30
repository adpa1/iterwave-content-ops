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


def test_inactive_tenant_worker_persists_delayed_data_fetch_job(cfg, tmp_path):
    import asyncio
    import sqlite3
    from datetime import datetime, timedelta, timezone

    from src.core.scheduler import SchedulerEngine

    db_path = tmp_path / "tenant-a" / "easel.db"
    db_path.parent.mkdir(parents=True)
    engine = SchedulerEngine(cfg, db_path)
    run_at = datetime.now(timezone.utc) + timedelta(hours=1)

    async def schedule():
        engine.schedule_data_fetch("draft-42", run_at, attempt=2)
        await asyncio.sleep(0.05)

    asyncio.run(schedule())

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM apscheduler_jobs WHERE id=?",
            ("data_fetch_draft-42_a2",),
        ).fetchone()
    assert row is not None
