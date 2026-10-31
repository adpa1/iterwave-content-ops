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
    d, _ = queue.enqueue(task_type="round_run", tenant_id="d", aggregate_id="4", payload={"round_id": 4})

    first = queue.claim_next("worker-1")
    assert first and first.task_id == a.task_id and first.tenant_id == "a"
    assert queue.finish(first.task_id, "worker-1", result={"ok": True})
    second = queue.claim_next("worker-1")
    assert second and second.task_id == b.task_id and second.tenant_id == "b"
    assert queue.finish(second.task_id, "worker-1", result={"ok": True})
    third = queue.claim_next("worker-1")
    assert third and third.task_id == c.task_id and third.tenant_id == "c"
    assert queue.finish(third.task_id, "worker-1", result={"ok": True})
    fourth = queue.claim_next("worker-1")
    assert fourth and fourth.task_id == d.task_id and fourth.tenant_id == "d"


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


def test_worker_cannot_release_lease_while_task_is_running(tmp_path):
    queue = make_queue(tmp_path)
    task, _ = queue.enqueue(task_type="round_run", tenant_id="a", payload={"round_id": 1})
    claimed = queue.claim_next("worker-1", lease_seconds=120)
    assert claimed and claimed.task_id == task.task_id
    assert queue.release_worker("worker-1") is False
    assert queue.claim_next("worker-2", lease_seconds=120) is None


def test_delayed_task_does_not_block_ready_fifo_tasks(tmp_path):
    from datetime import datetime, timedelta, timezone

    queue = make_queue(tmp_path)
    delayed, _ = queue.enqueue(
        task_type="scheduler_job", tenant_id="tenant-a",
        aggregate_id="_job_data_fetch:draft-1:1",
        payload={"method": "_job_data_fetch", "args": ["draft-1", 1]},
        available_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    ready, _ = queue.enqueue(
        task_type="round_run", tenant_id="tenant-b",
        aggregate_id="2", payload={"round_id": 2},
    )

    claimed = queue.claim_next("worker-1")
    assert claimed is not None and claimed.task_id == ready.task_id
    assert queue.finish(claimed.task_id, "worker-1", result={"ok": True})

    conn = queue._connect()
    try:
        conn.execute(
            "UPDATE global_task_queue SET available_at='2000-01-01T00:00:00+00:00' WHERE task_id=?",
            (delayed.task_id,),
        )
    finally:
        conn.close()

    due = queue.claim_next("worker-1")
    assert due is not None and due.task_id == delayed.task_id


def test_idle_worker_releases_global_lease_for_failover(tmp_path):
    queue = make_queue(tmp_path)
    assert queue.claim_next("idle-worker") is None

    task, _ = queue.enqueue(
        task_type="round_run", tenant_id="tenant-next",
        aggregate_id="9", payload={"round_id": 9},
    )
    claimed = queue.claim_next("replacement-worker")
    assert claimed is not None
    assert claimed.task_id == task.task_id


def test_concurrent_initialize_migrates_legacy_queue_schema_once(tmp_path):
    import sqlite3

    from src.core.task_queue import GlobalTaskQueue

    db_path = tmp_path / "shared" / "system.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE global_task_queue ("
            "task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, tenant_id TEXT NOT NULL, "
            "aggregate_id TEXT, payload_json TEXT NOT NULL, status TEXT NOT NULL, "
            "created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, "
            "attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 1, "
            "lease_owner TEXT, lease_expires_at TEXT, result_json TEXT, "
            "error_code TEXT, error_message TEXT)"
        )
        conn.execute(
            "INSERT INTO global_task_queue(task_id,task_type,tenant_id,payload_json,status,created_at) "
            "VALUES('legacy','round_run','tenant-a','{}','queued','2026-01-01T00:00:00+00:00')"
        )

    queue = GlobalTaskQueue(db_path)
    errors = []

    def initialize():
        try:
            queue.initialize()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=initialize) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    migrated = queue.get("legacy")
    assert migrated is not None
    assert migrated.queue_order == 1
    assert migrated.available_at == migrated.created_at




def test_queue_listing_keeps_active_tasks_ahead_of_recent_history(tmp_path):
    queue = make_queue(tmp_path)
    active, _ = queue.enqueue(
        task_type="round_run", tenant_id="tenant-a", aggregate_id="active",
        payload={"round_id": 1},
    )
    historical_ids = []
    for index in range(3):
        task, _ = queue.enqueue(
            task_type="round_run", tenant_id="tenant-b",
            aggregate_id=f"done-{index}", payload={"round_id": index + 2},
        )
        historical_ids.append(task.task_id)

    conn = queue._connect()
    try:
        conn.executemany(
            "UPDATE global_task_queue SET status='succeeded' WHERE task_id=?",
            [(task_id,) for task_id in historical_ids],
        )
    finally:
        conn.close()

    listed = queue.list(limit=2)
    assert listed[0].task_id == active.task_id
    assert listed[0].status == "queued"


def test_startup_recovery_interrupts_previous_process_tasks_immediately(tmp_path):
    queue = make_queue(tmp_path)
    running, _ = queue.enqueue(
        task_type="round_run", tenant_id="tenant-a", aggregate_id="running",
        payload={"round_id": 1},
    )
    queued, _ = queue.enqueue(
        task_type="round_run", tenant_id="tenant-b", aggregate_id="queued",
        payload={"round_id": 2},
    )
    claimed = queue.claim_next("previous-process", lease_seconds=120)
    assert claimed is not None and claimed.task_id == running.task_id
    result = queue.recover_orphaned_running()
    assert result == {"interrupted": 1}
    assert queue.get(running.task_id).status == "interrupted"
    assert queue.get(queued.task_id).status == "queued"
    next_task = queue.claim_next("new-process")
    assert next_task is not None and next_task.task_id == queued.task_id


def test_queue_list_skips_corrupt_payload_and_logs_it(tmp_path, caplog):
    queue = make_queue(tmp_path)
    good, _ = queue.enqueue(
        task_type="round_run", tenant_id="tenant-a", aggregate_id="good",
        payload={"round_id": 1},
    )
    conn = queue._connect()
    try:
        conn.execute(
            "INSERT INTO global_task_queue(queue_order,task_id,task_type,tenant_id,aggregate_id,"
            "payload_json,available_at,status,created_at) "
            "VALUES(999,'corrupt','round_run','tenant-a','bad','not-json',"
            "'2026-01-01T00:00:00+00:00','failed','2026-01-01T00:00:00+00:00')"
        )
    finally:
        conn.close()
    listed = queue.list(limit=10)
    assert [task.task_id for task in listed] == [good.task_id]
    assert "跳过无法解码的队列记录 task_id=corrupt" in caplog.text


def test_queue_rejects_unsupported_retry_count(tmp_path):
    import pytest
    queue = make_queue(tmp_path)
    with pytest.raises(ValueError, match="不自动重试"):
        queue.enqueue(
            task_type="round_run", tenant_id="tenant-a", payload={}, max_attempts=2,
        )

def test_queue_history_pagination_is_complete_and_disjoint(tmp_path):
    queue = make_queue(tmp_path)
    task_ids = []
    for index in range(47):
        task, _ = queue.enqueue(
            task_type="round_run", tenant_id=f"tenant-{index % 3}",
            aggregate_id=f"history-{index}", payload={"round_id": index},
        )
        task_ids.append(task.task_id)
    conn = queue._connect()
    try:
        conn.executemany(
            "UPDATE global_task_queue SET status='succeeded' WHERE task_id=?",
            [(task_id,) for task_id in task_ids],
        )
    finally:
        conn.close()

    first, total = queue.list_page(scope="history", page=1, page_size=20)
    second, second_total = queue.list_page(scope="history", page=2, page_size=20)
    third, third_total = queue.list_page(scope="history", page=3, page_size=20)
    assert total == second_total == third_total == 47
    assert [len(first), len(second), len(third)] == [20, 20, 7]
    collected = [task.task_id for task in first + second + third]
    assert len(collected) == len(set(collected)) == 47
    assert set(collected) == set(task_ids)


def test_queue_active_page_keeps_fifo_order(tmp_path):
    queue = make_queue(tmp_path)
    tasks = [
        queue.enqueue(task_type="round_run", tenant_id="a", payload={"n": i})[0]
        for i in range(4)
    ]
    page, total = queue.list_page(scope="active", page=1, page_size=10)
    assert total == 4
    assert [task.task_id for task in page] == [task.task_id for task in tasks]
\n