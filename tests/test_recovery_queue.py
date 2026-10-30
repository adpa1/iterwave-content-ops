from __future__ import annotations

import pytest

from src.core.recovery import RecoveryManager
from src.core.task_queue import GlobalTaskQueue


@pytest.mark.parametrize("operation", ["run", "save_pending_drafts"])
def test_startup_recovery_preserves_rounds_with_active_global_queue_tasks(
    cfg, conn, seed, tmp_path, operation,
):
    round_id = seed.round(status="producing")
    queue = GlobalTaskQueue(tmp_path / "shared" / "system.db")
    queue.initialize()
    aggregate_id = (
        str(round_id) if operation == "run"
        else f"{round_id}:save_pending_drafts"
    )
    queue.enqueue(
        task_type="round_run",
        tenant_id="tenant-a",
        aggregate_id=aggregate_id,
        payload={"round_id": round_id, "operation": operation},
    )

    report = RecoveryManager(
        cfg, conn, task_queue=queue, tenant_id="tenant-a",
    ).recover()

    assert round_id not in report["stale_rounds"]
    assert conn.execute(
        "SELECT status FROM spiral_rounds WHERE id=?", (round_id,),
    ).fetchone()[0] == "producing"


def test_startup_recovery_does_not_duplicate_delayed_metrics_retry(
    cfg, conn, seed, tmp_path,
):
    round_id = seed.round(status="awaiting_publish")
    draft_id = seed.draft(
        round_id, draft_status="user_published", fetch_status=None,
    )
    conn.execute(
        "UPDATE drafts SET user_confirmed_at='2026-10-01T00:00:00+00:00' WHERE id=?",
        (draft_id,),
    )
    conn.commit()

    queue = GlobalTaskQueue(tmp_path / "shared" / "system.db")
    queue.initialize()
    retry_attempt = int(cfg.timeouts.metrics_retry_attempts)
    queue.enqueue(
        task_type="scheduler_job",
        tenant_id="tenant-a",
        aggregate_id=f"_job_data_fetch:{draft_id}:{retry_attempt}",
        payload={"method": "_job_data_fetch", "args": [draft_id, retry_attempt]},
        available_at="2026-11-01T00:00:00+00:00",
    )
    scheduled = []

    class FakeScheduler:
        scheduler = type("JobStore", (), {"get_jobs": lambda self: []})()

        def schedule_data_fetch(self, draft_id, run_at, attempt=1):
            scheduled.append((draft_id, attempt))

    report = RecoveryManager(
        cfg, conn, FakeScheduler(), task_queue=queue, tenant_id="tenant-a",
    ).recover()

    assert draft_id not in report["requeued_data_fetch"]
    assert scheduled == []
