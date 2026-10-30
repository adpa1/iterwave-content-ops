from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from src.adapters.base import Platform
from src.core import login_manager
from src.core.task_queue import GlobalTaskQueue
from src.core.task_worker import GlobalTaskWorker


def test_worker_uses_task_tenant_context_not_active_login_directory(tmp_path):
    queue = GlobalTaskQueue(tmp_path / "shared" / "system.db")
    queue.initialize()
    task, _ = queue.enqueue(
        task_type="round_run", tenant_id="tenant-a", aggregate_id="17",
        payload={"round_id": 17, "operation": "run"},
    )
    stop = asyncio.Event()
    observed = {}

    class FakeExecutor:
        async def run(self, round_id, **kwargs):
            observed["round_id"] = round_id
            observed["login_path"] = login_manager._platform_state_path(Platform.XHS)
            stop.set()
            return {"round_id": round_id, "final_status": "completed"}

    def build_context(tenant_id):
        assert tenant_id == "tenant-a"
        return {
            "cfg": SimpleNamespace(
                paths=SimpleNamespace(data_dir=str(tmp_path / "tenant-a" / "data")),
                resolve_path=lambda value: value,
            ),
            "executor": FakeExecutor(),
            "scheduler": SimpleNamespace(shutdown=lambda: None),
            "conn": SimpleNamespace(close=lambda: None),
        }

    worker = GlobalTaskWorker(
        queue, build_context, poll_interval_seconds=0.01,
        lease_seconds=30, heartbeat_seconds=1,
    )
    asyncio.run(worker.run(stop))

    assert observed["round_id"] == 17
    assert observed["login_path"] == tmp_path / "tenant-a" / "data" / "login_states" / "xhs.json"
    saved = queue.get(task.task_id)
    assert saved is not None and saved.status == "succeeded"
    assert saved.result == {"round_id": 17, "final_status": "completed"}
