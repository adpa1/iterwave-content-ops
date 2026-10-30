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
