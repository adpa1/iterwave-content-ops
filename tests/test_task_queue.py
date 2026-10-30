from __future__ import annotations

import threading
from pathlib import Path

from src.core.task_queue import GlobalTaskQueue


def make_queue(tmp_path: Path) -> GlobalTaskQueue:
    queue = GlobalTaskQueue(tmp_path / "shared" / "system.db")
    queue.initialize()
    return queue


def test_fifo_is_global_across_tenants(tmp_path):
    queue = make_queue(tmp_path)
    a, _ = queue.enqueue(task_type="round_run", tenant_id="a", aggregate_id="1", payload={"round_id": 1})
    b, _ = queue.enqueue(task_type="round_run", tenant_id="b", aggregate_id="2", payload={"round_id": 2})
    c, _ = queue.enqueue(task_type="round_run", tenant_id="c", aggregate_id="3", payload={"round_id": 3})

    first = queue.claim_next("worker-1")
    assert first and first.task_id == a.task_id and first.tenant_id == "a"
    assert queue.finish(first.task_id, "worker-1", result={"ok": True})
    second = queue.claim_next("worker-1")
    assert second and second.task_id == b.task_id and second.tenant_id == "b"
    assert queue.finish(second.task_id, "worker-1", result={"ok": True})
    third = queue.claim_next("worker-1")
    assert third and third.task_id == c.task_id and third.tenant_id == "c"


def test_active_aggregate_is_idempotent(tmp_path):
    queue = make_queue(tmp_path)
    first, created = queue.enqueue(
        task_type="round_run", tenant_id="a", aggregate_id="42", payload={"round_id": 42},
    )
    again, created_again = queue.enqueue(
        task_type="round_run", tenant_id="a", aggregate_id="42", payload={"round_id": 42},
    )
    assert created is True
    assert created_again is False
    assert first.task_id == again.task_id


def test_worker_lease_prevents_parallel_consumers(tmp_path):
    queue = make_queue(tmp_path)
    queue.enqueue(task_type="round_run", tenant_id="a", payload={})
    claimed = queue.claim_next("worker-1", lease_seconds=120)
    assert claimed is not None
    assert queue.claim_next("worker-2", lease_seconds=120) is None


def test_failure_releases_queue_for_next_task(tmp_path):
    queue = make_queue(tmp_path)
    first, _ = queue.enqueue(task_type="round_run", tenant_id="a", payload={"n": 1})
    second, _ = queue.enqueue(task_type="round_run", tenant_id="b", payload={"n": 2})
    claimed = queue.claim_next("worker-1")
    assert claimed and claimed.task_id == first.task_id
    assert queue.fail(first.task_id, "worker-1", error_code="failed", error_message="safe")
    next_task = queue.claim_next("worker-1")
    assert next_task and next_task.task_id == second.task_id


def test_only_queued_tasks_can_be_cancelled(tmp_path):
    queue = make_queue(tmp_path)
    task, _ = queue.enqueue(task_type="round_run", tenant_id="a", payload={})
    assert queue.cancel(task.task_id)
    assert not queue.cancel(task.task_id)
    assert queue.get(task.task_id).status == "cancelled"


def test_running_task_is_not_replayed_after_worker_loss(tmp_path):
    queue = make_queue(tmp_path)
    task, _ = queue.enqueue(task_type="round_run", tenant_id="a", payload={})
    claimed = queue.claim_next("worker-1", lease_seconds=15)
    assert claimed and claimed.task_id == task.task_id
    # Force lease expiry deterministically to simulate a process crash.
    conn = queue._connect()
    try:
        conn.execute(
            "UPDATE global_task_queue SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE task_id=?",
            (task.task_id,),
        )
        conn.execute(
            "UPDATE global_task_worker_lease SET expires_at='2000-01-01T00:00:00+00:00' WHERE singleton_id=1"
        )
    finally:
        conn.close()
    assert queue.recover_expired() == {"interrupted": 1}
    assert queue.get(task.task_id).status == "interrupted"
    assert queue.claim_next("worker-2") is None


def test_concurrent_duplicate_enqueue_creates_one_active_task(tmp_path):
    queue = make_queue(tmp_path)
    results = []

    def submit():
        results.append(queue.enqueue(
            task_type="round_run", tenant_id="a", aggregate_id="99", payload={"round_id": 99},
        ))

    threads = [threading.Thread(target=submit) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(created for _, created in results) == 1
    assert len({task.task_id for task, _ in results}) == 1


def test_worker_lease_is_held_until_consumer_releases_it(tmp_path):
    queue = make_queue(tmp_path)
    first, _ = queue.enqueue(task_type="round_run", tenant_id="a", payload={"round_id": 1})
    second, _ = queue.enqueue(task_type="round_run", tenant_id="b", payload={"round_id": 2})
    claimed = queue.claim_next("worker-1", lease_seconds=120)
    assert claimed and claimed.task_id == first.task_id
    assert queue.finish(first.task_id, "worker-1", result={"ok": True})
    # 任务状态已完成，但 worker 尚未释放全局租约，其他进程不能并行领取下一项。
    assert queue.claim_next("worker-2", lease_seconds=120) is None
    next_task = queue.claim_next("worker-1", lease_seconds=120)
    assert next_task and next_task.task_id == second.task_id
