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
