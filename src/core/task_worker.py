"""全局任务队列消费者。

消费者独立于前台活动账号组。每个任务按自身 tenant_id 构造运行时，并在异步上下文中
绑定对应登录态目录。首期只执行 round_run；其它后台入口逐步迁移。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.core.task_queue import GlobalTaskQueue, QueuedTask

logger = logging.getLogger(__name__)


class GlobalTaskWorker:
    def __init__(
        self,
        queue: GlobalTaskQueue,
        build_tenant_context: Callable[[str], dict[str, Any]],
        *,
        poll_interval_seconds: float = 0.5,
        lease_seconds: int = 90,
        heartbeat_seconds: float = 20,
    ):
        self.queue = queue
        self.build_tenant_context = build_tenant_context
        self.poll_interval_seconds = poll_interval_seconds
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.owner = uuid.uuid4().hex

    async def run(self, stop_event: asyncio.Event) -> None:
        logger.info("全局任务消费者启动 worker=%s", self.owner[:8])
        try:
            while not stop_event.is_set():
                try:
                    self.queue.recover_expired()
                    task = self.queue.claim_next(self.owner, lease_seconds=self.lease_seconds)
                except Exception:
                    logger.exception("全局任务队列读取失败")
                    await self._wait(stop_event, self.poll_interval_seconds * 4)
                    continue
                if task is None:
                    await self._wait(stop_event, self.poll_interval_seconds)
                    continue
                await self._execute(task)
        finally:
            self.queue.release_worker(self.owner)
            logger.info("全局任务消费者退出 worker=%s", self.owner[:8])

    async def _wait(self, stop_event: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _heartbeat(self, task: QueuedTask) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            if not self.queue.renew(
                task.task_id, self.owner, lease_seconds=self.lease_seconds,
            ):
                logger.error("任务租约续期失败 task_id=%s", task.task_id)
                return

    async def _execute(self, task: QueuedTask) -> None:
        context: dict[str, Any] | None = None
        heartbeat = asyncio.create_task(self._heartbeat(task))
        try:
            if task.task_type != "round_run":
                raise ValueError(f"不支持的任务类型：{task.task_type}")
            round_id = int(task.payload["round_id"])
            context = self.build_tenant_context(task.tenant_id)
            from src.core import login_manager

            data_dir = Path(context["cfg"].resolve_path(context["cfg"].paths.data_dir))
            with login_manager.tenant_login_dir(data_dir):
                report = await context["executor"].run(
                    round_id,
                    topic_count=task.payload.get("topic_count"),
                    platform_values=task.payload.get("platforms"),
                    save_drafts=task.payload.get("save_drafts"),
                )
            if not self.queue.finish(task.task_id, self.owner, result=report):
                logger.error("任务完成状态写入失败，租约可能已失效 task_id=%s", task.task_id)
        except Exception as exc:  # noqa: BLE001 — 单任务失败不得终止消费者
            logger.exception(
                "全局队列任务执行失败 task_id=%s tenant_id=%s",
                task.task_id, task.tenant_id,
            )
            error_code = getattr(exc, "code", "round_execution_failed")
            safe_message = (
                str(exc) if getattr(exc, "code", None)
                else "本轮执行失败，请检查运行记录"
            )
            self.queue.fail(
                task.task_id, self.owner,
                error_code=error_code, error_message=safe_message,
            )
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            if context is not None:
                scheduler = context.get("scheduler")
                if scheduler is not None:
                    try:
                        scheduler.shutdown()
                    except Exception:
                        pass
                conn = context.get("conn")
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        logger.exception("关闭任务账号组数据库连接失败")
