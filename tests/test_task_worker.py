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
    assert "sk-a***" in (saved.error_message or "")



def test_worker_uses_task_tenant_notifier_when_foreground_is_another_tenant(tmp_path, monkeypatch):
    """前台停留在 C 时执行 A 任务，A 的通知不得写入/发送到 C。"""
    from src.core import notify

    queue = GlobalTaskQueue(tmp_path / "shared" / "system.db")
    queue.initialize()
    task, _ = queue.enqueue(
        task_type="round_run", tenant_id="tenant-a", aggregate_id="21",
        payload={"round_id": 21, "operation": "run"},
    )
    claimed = queue.claim_next("worker-notifier")
    assert claimed is not None

    class RecordingNotifier:
        def __init__(self, tenant_id):
            self.tenant_id = tenant_id
            self.events = []

        def emit(self, event, message, **kwargs):
            self.events.append((event, message, kwargs))
            return True

    foreground_c = RecordingNotifier("tenant-c")
    task_a_notifier = RecordingNotifier("tenant-a")
    monkeypatch.setattr(notify, "_active", foreground_c)
    observed = {}

    class FakeExecutor:
        async def run(self, round_id, **kwargs):
            # CircuitBreakerEngine / OperationsEngine / GoalProgressUpdater
            # 的通知路径均通过 get_active() 取通知器；这里验证 worker 绑定边界。
            active = notify.get_active()
            observed["notifier_tenant"] = active.tenant_id
            active.emit("circuit_broken", "小红书触发熔断：login_expired")
            return {"round_id": round_id, "final_status": "completed"}

    context = {
        "cfg": SimpleNamespace(
            paths=SimpleNamespace(data_dir=str(tmp_path / "tenant-a" / "data")),
            resolve_path=lambda value: value,
        ),
        "executor": FakeExecutor(),
        "scheduler": SimpleNamespace(shutdown=lambda: None),
        "conn": SimpleNamespace(close=lambda: None),
        "notifier": task_a_notifier,
    }
    worker = GlobalTaskWorker(queue, lambda tenant_id: context)
    result = asyncio.run(worker._run_task(claimed))

    assert result == {"round_id": 21, "final_status": "completed"}
    assert observed["notifier_tenant"] == "tenant-a"
    assert len(task_a_notifier.events) == 1
    assert task_a_notifier.events[0][0] == "circuit_broken"
    assert foreground_c.events == []
    # ContextVar 必须在任务结束后恢复，不能污染后续任务或前台请求。
    assert notify.get_active() is foreground_c



def test_worker_with_no_task_notifier_does_not_fall_back_to_foreground(tmp_path, monkeypatch):
    """任务上下文明确没有 notifier 时，不得回退到前台账号组通知器。"""
    from src.core import notify

    queue = GlobalTaskQueue(tmp_path / "shared" / "system.db")
    queue.initialize()
    task, _ = queue.enqueue(
        task_type="round_run", tenant_id="tenant-a", aggregate_id="22",
        payload={"round_id": 22, "operation": "run"},
    )
    claimed = queue.claim_next("worker-no-notifier")
    assert claimed is not None

    class RecordingNotifier:
        def __init__(self):
            self.events = []

        def emit(self, event, message, **kwargs):
            self.events.append((event, message, kwargs))
            return True

    foreground_c = RecordingNotifier()
    monkeypatch.setattr(notify, "_active", foreground_c)
    observed = {}

    class FakeExecutor:
        async def run(self, round_id, **kwargs):
            observed["notifier"] = notify.get_active()
            return {"round_id": round_id, "final_status": "completed"}

    context = {
        "cfg": SimpleNamespace(
            paths=SimpleNamespace(data_dir=str(tmp_path / "tenant-a" / "data")),
            resolve_path=lambda value: value,
        ),
        "executor": FakeExecutor(),
        "scheduler": SimpleNamespace(shutdown=lambda: None),
        "conn": SimpleNamespace(close=lambda: None),
        # 故意不提供 notifier，验证明确绑定 None 时不会串到前台账号组。
    }
    worker = GlobalTaskWorker(queue, lambda tenant_id: context)
    result = asyncio.run(worker._run_task(claimed))

    assert result == {"round_id": 22, "final_status": "completed"}
    assert observed["notifier"] is None
    assert foreground_c.events == []
    # 任务结束后必须恢复前台默认通知器。
    assert notify.get_active() is foreground_c


def test_worker_shows_redacted_user_facing_precondition_error(tmp_path):
    from src.core.round_executor import RoundPreconditionError

    queue = GlobalTaskQueue(tmp_path / "shared" / "system.db")
    queue.initialize()
    task, _ = queue.enqueue(
        task_type="round_run", tenant_id="tenant-a", aggregate_id="23",
        payload={"round_id": 23, "operation": "run"},
    )
    claimed = queue.claim_next("worker-safe-error")

    class FakeExecutor:
        async def run(self, round_id, **kwargs):
            raise RoundPreconditionError(
                "运行前检查未通过：选题库没有可用选题。请先补充或恢复选题；api_key=sk-abcdef123456"
            )

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
    worker.owner = "worker-safe-error"
    asyncio.run(worker._execute(claimed))

    saved = queue.get(task.task_id)
    assert saved is not None and saved.status == "failed"
    assert "运行前检查未通过" in (saved.error_message or "")
    assert "请先补充或恢复选题" in (saved.error_message or "")
    assert "sk-abcdef123456" not in (saved.error_message or "")
    assert "sk-a***" in (saved.error_message or "")


def test_worker_keeps_unknown_value_error_generic(tmp_path):
    queue = GlobalTaskQueue(tmp_path / "shared" / "system.db")
    queue.initialize()
    task, _ = queue.enqueue(
        task_type="round_run", tenant_id="tenant-a", aggregate_id="24",
        payload={"round_id": 24, "operation": "run"},
    )
    claimed = queue.claim_next("worker-unknown-error")

    class FakeExecutor:
        async def run(self, round_id, **kwargs):
            raise ValueError("third-party internal failure api_key=sk-abcdef123456")

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
    worker.owner = "worker-unknown-error"
    asyncio.run(worker._execute(claimed))

    saved = queue.get(task.task_id)
    assert saved is not None and saved.status == "failed"
    assert saved.error_message == "本轮执行失败，请检查运行记录"


def test_worker_shutdown_cancels_long_task_after_grace_and_marks_interrupted(tmp_path):
    queue = GlobalTaskQueue(tmp_path / "shared" / "system.db")
    queue.initialize()
    first, _ = queue.enqueue(
        task_type="round_run", tenant_id="tenant-a", aggregate_id="first",
        payload={"round_id": 1, "operation": "run"},
    )
    second, _ = queue.enqueue(
        task_type="round_run", tenant_id="tenant-b", aggregate_id="second",
        payload={"round_id": 2, "operation": "run"},
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class FakeExecutor:
        async def run(self, round_id, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

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

    async def scenario():
        stop = asyncio.Event()
        worker = GlobalTaskWorker(
            queue, build_context, poll_interval_seconds=0.005,
            lease_seconds=30, heartbeat_seconds=0.01,
            shutdown_grace_seconds=0.01,
        )
        worker_task = asyncio.create_task(worker.run(stop))
        await asyncio.wait_for(started.wait(), timeout=1)
        stop.set()
        await asyncio.wait_for(worker_task, timeout=1)
        assert cancelled.is_set()

    asyncio.run(scenario())
    saved_first = queue.get(first.task_id)
    saved_second = queue.get(second.task_id)
    assert saved_first is not None and saved_first.status == "interrupted"
    assert saved_first.error_code == "worker_shutdown"
    assert saved_second is not None and saved_second.status == "queued"
    next_task = queue.claim_next("replacement-worker")
    assert next_task is not None and next_task.task_id == second.task_id


def test_worker_heartbeat_runs_in_background_thread(tmp_path):
    import threading
    import time

    queue = GlobalTaskQueue(tmp_path / "shared" / "system.db")
    queue.initialize()
    task, _ = queue.enqueue(
        task_type="round_run", tenant_id="tenant-a", aggregate_id="heartbeat",
        payload={"round_id": 1},
    )
    claimed = queue.claim_next("worker-heartbeat", lease_seconds=30)
    assert claimed is not None
    worker = GlobalTaskWorker(queue, lambda tenant_id: {}, heartbeat_seconds=0.01)
    worker.owner = "worker-heartbeat"
    loop = asyncio.new_event_loop()
    stopped = threading.Event()
    renewed = []
    original_renew = queue.renew

    def recording_renew(*args, **kwargs):
        renewed.append(time.monotonic())
        return original_renew(*args, **kwargs)

    queue.renew = recording_renew
    thread = threading.Thread(
        target=worker._heartbeat_loop, args=(claimed, stopped, loop, asyncio.Event()),
    )
    thread.start()
    # 在 CI 负载下线程调度可能超过固定 60ms；等待实际观察到两次续租，
    # 同时设置上限，避免测试因调度抖动产生偶发失败。
    deadline = time.monotonic() + 1.0
    while len(renewed) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    stopped.set()
    thread.join(timeout=1)
    loop.close()
    assert not thread.is_alive()
    assert len(renewed) >= 2
