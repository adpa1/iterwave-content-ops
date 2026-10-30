"""持久化全局任务队列。

队列位于 shared/system.db，与当前活动账号组无关。所有业务任务必须显式携带
tenant_id；消费者只根据任务快照构造对应账号组运行时，绝不读取活动账号组。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class QueuedTask:
    task_id: str
    task_type: str
    tenant_id: str
    aggregate_id: str | None
    payload: dict[str, Any]
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    attempts: int
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None


class TaskQueueError(RuntimeError):
    """任务队列操作失败。"""


class GlobalTaskQueue:
    """基于 SQLite 的单消费者 FIFO 队列，跨账号组共享且可在重启后恢复。

    claim_next 使用 BEGIN IMMEDIATE + 全局 worker lease，确保多个 API worker
    不会同时消费不同任务。任务租约需要由消费者定期续期。
    """

    def __init__(self, system_db_file: Path | str):
        self.db_file = Path(system_db_file).resolve()
        self.db_file.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_file), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def initialize(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS global_task_queue (
                    task_id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    aggregate_id TEXT,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('queued','running','succeeded','failed','cancelled','interrupted')),
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 1,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    result_json TEXT,
                    error_code TEXT,
                    error_message TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_global_task_queue_fifo
                    ON global_task_queue(status, created_at, task_id);
                CREATE INDEX IF NOT EXISTS idx_global_task_queue_tenant
                    ON global_task_queue(tenant_id, created_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_global_task_active_aggregate
                    ON global_task_queue(tenant_id, task_type, aggregate_id)
                    WHERE aggregate_id IS NOT NULL AND status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS global_task_worker_lease (
                    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
                    owner TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                """
            )
        finally:
            conn.close()
        try:
            self.db_file.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> QueuedTask | None:
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
            result = json.loads(row["result_json"]) if row["result_json"] else None
        except (TypeError, ValueError) as exc:
            raise TaskQueueError("队列任务数据损坏，已拒绝执行") from exc
        if not isinstance(payload, dict) or (result is not None and not isinstance(result, dict)):
            raise TaskQueueError("队列任务数据结构无效，已拒绝执行")
        return QueuedTask(
            task_id=row["task_id"], task_type=row["task_type"],
            tenant_id=row["tenant_id"], aggregate_id=row["aggregate_id"],
            payload=payload, status=row["status"], created_at=row["created_at"],
            started_at=row["started_at"], finished_at=row["finished_at"],
            attempts=int(row["attempts"]), result=result,
            error_code=row["error_code"], error_message=row["error_message"],
        )

    def enqueue(
        self, *, task_type: str, tenant_id: str, payload: dict[str, Any],
        aggregate_id: str | None = None, max_attempts: int = 1,
    ) -> tuple[QueuedTask, bool]:
        if not task_type or len(task_type) > 80:
            raise ValueError("task_type 无效")
        if not tenant_id or len(tenant_id) > 48:
            raise ValueError("tenant_id 无效")
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是对象")
        if not 1 <= int(max_attempts) <= 5:
            raise ValueError("max_attempts 必须在 1–5 之间")
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        now = _stamp()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if aggregate_id is not None:
                existing = conn.execute(
                    "SELECT * FROM global_task_queue WHERE tenant_id=? AND task_type=? "
                    "AND aggregate_id=? AND status IN ('queued','running') "
                    "ORDER BY created_at LIMIT 1",
                    (tenant_id, task_type, str(aggregate_id)),
                ).fetchone()
                if existing:
                    conn.commit()
                    task = self._decode(existing)
                    assert task is not None
                    return task, False
            task_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO global_task_queue(task_id,task_type,tenant_id,aggregate_id,"
                "payload_json,status,created_at,max_attempts) VALUES(?,?,?,?,?,'queued',?,?)",
                (task_id, task_type, tenant_id, str(aggregate_id) if aggregate_id is not None else None,
                 payload_json, now, int(max_attempts)),
            )
            row = conn.execute("SELECT * FROM global_task_queue WHERE task_id=?", (task_id,)).fetchone()
            conn.commit()
            task = self._decode(row)
            assert task is not None
            return task, True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim_next(self, owner: str, *, lease_seconds: int = 60) -> QueuedTask | None:
        if not owner:
            raise ValueError("worker owner 不能为空")
        now = _now()
        expires = _stamp(now + timedelta(seconds=max(15, lease_seconds)))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            lease = conn.execute(
                "SELECT owner,expires_at FROM global_task_worker_lease WHERE singleton_id=1"
            ).fetchone()
            if lease and lease["owner"] != owner and lease["expires_at"] > _stamp(now):
                conn.commit()
                return None
            conn.execute(
                "INSERT INTO global_task_worker_lease(singleton_id,owner,expires_at) VALUES(1,?,?) "
                "ON CONFLICT(singleton_id) DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at",
                (owner, expires),
            )
            row = conn.execute(
                "SELECT * FROM global_task_queue WHERE status='queued' "
                "ORDER BY created_at ASC, task_id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            started = _stamp(now)
            conn.execute(
                "UPDATE global_task_queue SET status='running',started_at=?,attempts=attempts+1,"
                "lease_owner=?,lease_expires_at=? WHERE task_id=? AND status='queued'",
                (started, owner, expires, row["task_id"]),
            )
            claimed = conn.execute(
                "SELECT * FROM global_task_queue WHERE task_id=?", (row["task_id"],)
            ).fetchone()
            conn.commit()
            return self._decode(claimed)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def renew(self, task_id: str, owner: str, *, lease_seconds: int = 60) -> bool:
        expires = _stamp(_now() + timedelta(seconds=max(15, lease_seconds)))
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            lease = conn.execute(
                "SELECT owner FROM global_task_worker_lease WHERE singleton_id=1"
            ).fetchone()
            if not lease or lease["owner"] != owner:
                conn.rollback()
                return False
            cursor = conn.execute(
                "UPDATE global_task_queue SET lease_expires_at=? "
                "WHERE task_id=? AND status='running' AND lease_owner=?",
                (expires, task_id, owner),
            )
            if cursor.rowcount:
                conn.execute(
                    "UPDATE global_task_worker_lease SET expires_at=? WHERE singleton_id=1 AND owner=?",
                    (expires, owner),
                )
            conn.commit()
            return cursor.rowcount == 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def finish(
        self, task_id: str, owner: str, *, result: dict[str, Any] | None = None,
    ) -> bool:
        result_json = json.dumps(result or {}, ensure_ascii=False, sort_keys=True)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE global_task_queue SET status='succeeded',finished_at=?,result_json=?,"
                "lease_owner=NULL,lease_expires_at=NULL,error_code=NULL,error_message=NULL "
                "WHERE task_id=? AND status='running' AND lease_owner=?",
                (_stamp(), result_json, task_id, owner),
            )
            if cursor.rowcount:
                conn.execute(
                    "DELETE FROM global_task_worker_lease WHERE singleton_id=1 AND owner=?", (owner,)
                )
            conn.commit()
            return cursor.rowcount == 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def fail(
        self, task_id: str, owner: str, *, error_code: str,
        error_message: str,
    ) -> bool:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE global_task_queue SET status='failed',finished_at=?,error_code=?,"
                "error_message=?,lease_owner=NULL,lease_expires_at=NULL "
                "WHERE task_id=? AND status='running' AND lease_owner=?",
                (_stamp(), error_code[:80], error_message[:500], task_id, owner),
            )
            if cursor.rowcount:
                conn.execute(
                    "DELETE FROM global_task_worker_lease WHERE singleton_id=1 AND owner=?", (owner,)
                )
            conn.commit()
            return cursor.rowcount == 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def release_worker(self, owner: str) -> bool:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "DELETE FROM global_task_worker_lease WHERE singleton_id=1 AND owner=?",
                (owner,),
            )
            return cursor.rowcount == 1
        finally:
            conn.close()

    def cancel(self, task_id: str) -> bool:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE global_task_queue SET status='cancelled',finished_at=? "
                "WHERE task_id=? AND status='queued'",
                (_stamp(), task_id),
            )
            return cursor.rowcount == 1
        finally:
            conn.close()

    def recover_expired(self) -> dict[str, int]:
        """只回收租约过期的 running 任务；转为 interrupted，避免不安全地重复执行副作用。"""
        now = _stamp()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE global_task_queue SET status='interrupted',finished_at=?,"
                "error_code='worker_lost',error_message='任务执行期间服务中断，需人工确认后恢复',"
                "lease_owner=NULL,lease_expires_at=NULL "
                "WHERE status='running' AND (lease_expires_at IS NULL OR lease_expires_at<=?)",
                (now, now),
            )
            conn.execute(
                "DELETE FROM global_task_worker_lease WHERE singleton_id=1 AND expires_at<=?", (now,)
            )
            conn.commit()
            return {"interrupted": cursor.rowcount}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, task_id: str) -> QueuedTask | None:
        conn = self._connect()
        try:
            return self._decode(conn.execute(
                "SELECT * FROM global_task_queue WHERE task_id=?", (task_id,)
            ).fetchone())
        finally:
            conn.close()

    def find_latest(
        self, *, tenant_id: str, task_type: str, aggregate_id: str,
    ) -> QueuedTask | None:
        conn = self._connect()
        try:
            return self._decode(conn.execute(
                "SELECT * FROM global_task_queue WHERE tenant_id=? AND task_type=? "
                "AND aggregate_id=? ORDER BY created_at DESC,task_id DESC LIMIT 1",
                (tenant_id, task_type, str(aggregate_id)),
            ).fetchone())
        finally:
            conn.close()

    def list(self, *, limit: int = 100, tenant_id: str | None = None) -> list[QueuedTask]:
        limit = max(1, min(int(limit), 500))
        conn = self._connect()
        try:
            if tenant_id is None:
                rows = conn.execute(
                    "SELECT * FROM global_task_queue ORDER BY created_at DESC,task_id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM global_task_queue WHERE tenant_id=? "
                    "ORDER BY created_at DESC,task_id DESC LIMIT ?", (tenant_id, limit),
                ).fetchall()
            return [task for row in rows if (task := self._decode(row)) is not None]
        finally:
            conn.close()
