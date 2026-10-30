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


def test_worker_cancels_execution_when_lease_renewal_fails(tmp_path, monkeypatch):
    queue = GlobalTaskQueue(tmp_path / "shared" / "system.db")
    queue.initialize()
    task, _ = queue.enqueue(
        task_type="round_run", tenant_id="tenant-a", aggregate_id="18",
        payload={"round_id": 18, "operation": "run"},
    )
    claimed = queue.claim_next("worker-a", lease_seconds=30)
    assert claimed is not None
    cancelled = asyncio.Event()

    class FakeExecutor:
        async def run(self, round_id, **kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    monkeypatch.setattr(queue, "renew", lambda *args, **kwargs: False)

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
        queue, build_context, lease_seconds=30, heartbeat_seconds=0.01,
    )
    asyncio.run(worker._execute(claimed))

    assert cancelled.is_set()
    # 不得把已失去租约的任务伪装成成功或失败；恢复流程稍后会标记 interrupted。
    saved = queue.get(task.task_id)
    assert saved is not None and saved.status == "running"


def test_worker_executes_queued_scheduler_job_in_its_tenant_context(tmp_path):
    queue = GlobalTaskQueue(tmp_path / "shared" / "system.db")
    queue.initialize()
    task, _ = queue.enqueue(
        task_type="scheduler_job", tenant_id="tenant-b",
        aggregate_id="_job_daily_ops",
        payload={"method": "_job_daily_ops", "args": []},
    )
    claimed = queue.claim_next("worker-b")
    assert claimed is not None
    observed = {}

    class FakeScheduler:
        async def _job_daily_ops(self):
            observed["login_path"] = login_manager._platform_state_path(Platform.XHS)
            return None

        def shutdown(self):
            return None

    context = {
        "cfg": SimpleNamespace(
            paths=SimpleNamespace(data_dir=str(tmp_path / "tenant-b" / "data")),
            resolve_path=lambda value: value,
        ),
        "scheduler": FakeScheduler(),
        "conn": SimpleNamespace(close=lambda: None),
    }
    worker = GlobalTaskWorker(queue, lambda tenant_id: context)
    result = asyncio.run(worker._run_task(claimed))

    assert result == {"completed": True}
    assert observed["login_path"] == tmp_path / "tenant-b" / "data" / "login_states" / "xhs.json"
    saved = queue.get(task.task_id)
    assert saved is not None and saved.status == "running"


def test_worker_survives_failure_status_write_error(tmp_path, monkeypatch):
    queue = GlobalTaskQueue(tmp_path / "shared" / "system.db")
    queue.initialize()
    queue.enqueue(
        task_type="round_run", tenant_id="tenant-a", aggregate_id="19",
        payload={"round_id": 19, "operation": "run"},
    )
    claimed = queue.claim_next("worker-c")
    assert claimed is not None

    class FakeExecutor:
        async def run(self, round_id, **kwargs):
            raise RuntimeError("simulated task failure")

    def build_context(tenant_id):
        return {
            "cfg": SimpleNamespace(
                paths=SimpleNamespace(data_dir=str(tmp_path / tenant_id / "data")),
                resolve_path=lambda value: value,
            ),
            "executor": FakeExecutor(),
            "scheduler": SimpleNamespace(shutdown=lambda: None),
            "conn": SimpleNamespace(close=lambda: None),
        }

    def fail_write(*args, **kwargs):
        raise OSError("simulated database outage")

    monkeypatch.setattr(queue, "fail", fail_write)
    worker = GlobalTaskWorker(
        queue, build_context, lease_seconds=30, heartbeat_seconds=1,
    )
    # 持久化失败状态的异常不能逸出 _execute 并终止全局消费者。
    asyncio.run(worker._execute(claimed))


def test_worker_redacts_sensitive_failure_details_before_queue_storage(tmp_path):
    queue = GlobalTaskQueue(tmp_path / "shared" / "system.db")
    queue.initialize()
    task, _ = queue.enqueue(
        task_type="round_run", tenant_id="tenant-a", aggregate_id="20",
        payload={"round_id": 20, "operation": "run"},
    )
    claimed = queue.claim_next("worker-d")

    class SafeCodeError(RuntimeError):
        code = "provider_error"

    class FakeExecutor:
        async def run(self, round_id, **kwargs):
            raise SafeCodeError("provider rejected api_key=sk-abcdef123456")

    context = {
        "cfg": SimpleNamespace(
            paths=SimpleNamespace(data_dir=str(tmp_path / "tenant-a" / "data")),
            resolve_path=lambda value: value,
        ),
        "executor": FakeExecutor(),
        "scheduler": SimpleNamespace(shutdown=lambda: None),
        "conn": SimpleNamespace(close=lambda: None),
    }
    worker = GlobalTaskWorker(queue, lambda tenant_id: context)
    worker.owner = "worker-d"
    asyncio.run(worker._execute(claimed))

    saved = queue.get(task.task_id)
    assert saved is not None and saved.status == "failed"
    assert "sk-abcdef123456" not in (saved.error_message or "")
    assert "sk-***" in (saved.error_message or "")
