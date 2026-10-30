"""全局任务队列消费者。

消费者独立于前台活动账号组。每个任务按自身 tenant_id 构造运行时，并在异步上下文中
绑定对应登录态目录。生产轮次与账号组定时作业统一由全局单消费者执行。
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


class WorkerLeaseLost(RuntimeError):
    """消费者无法确认自己仍持有全局任务租约。"""


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

    async def _heartbeat(self, task: QueuedTask, lease_lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            try:
                renewed = self.queue.renew(
                    task.task_id, self.owner, lease_seconds=self.lease_seconds,
                )
            except Exception:
                logger.exception("任务租约续期异常 task_id=%s", task.task_id)
                lease_lost.set()
                return
            if not renewed:
                logger.error("任务租约续期失败，停止执行 task_id=%s", task.task_id)
                lease_lost.set()
                return

    async def _run_task(self, task: QueuedTask) -> dict[str, Any]:
        allowed_scheduler_methods = {
            "_job_publish_polling", "_job_data_fetch", "_job_daily_ops",
            "_job_comment_polling", "_job_cb_recovery", "_job_kb_scan",
            "_job_kb_sync", "_job_data_retention_cleanup",
            "_job_round_timeout_sweeper", "_job_round_review", "_job_autogrow_next_round",
        }
        if task.task_type not in {"round_run", "scheduler_job"}:
            raise ValueError(f"不支持的任务类型：{task.task_type}")
        context = self.build_tenant_context(task.tenant_id)
        from src.core import login_manager

        data_dir = Path(context["cfg"].resolve_path(context["cfg"].paths.data_dir))
        try:
            with login_manager.tenant_login_dir(data_dir):
                if task.task_type == "round_run":
                    round_id = int(task.payload["round_id"])
                    operation = task.payload.get("operation", "run")
                    feedback_data = task.payload.get("feedback")
                    feedback = None
                    if isinstance(feedback_data, dict):
                        from src.core.autogrow import StrategyFeedback
                        feedback = StrategyFeedback(**feedback_data)
                    if operation == "save_pending_drafts":
                        return await context["executor"].save_pending_drafts(round_id)
                    if operation == "run":
                        return await context["executor"].run(
                            round_id,
                            topic_count=task.payload.get("topic_count"),
                            platform_values=task.payload.get("platforms"),
                            save_drafts=task.payload.get("save_drafts"),
                            feedback=feedback,
                        )
                    raise ValueError(f"不支持的轮次任务操作：{operation}")

                method = task.payload.get("method")
                args = task.payload.get("args", [])
                if method not in allowed_scheduler_methods or not isinstance(args, list):
                    raise ValueError("定时任务类型或参数无效")
                result = await getattr(context["scheduler"], method)(*args)
                return result if isinstance(result, dict) else {"completed": True}
        finally:
            scheduler = context.get("scheduler")
            if scheduler is not None:
                try:
                    scheduler.shutdown()
                except Exception:
                    logger.exception("关闭任务账号组调度器失败")
            conn = context.get("conn")
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    logger.exception("关闭任务账号组数据库连接失败")

    async def _execute(self, task: QueuedTask) -> None:
        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(task, lease_lost))
        execution = asyncio.create_task(self._run_task(task))
        try:
            done, _ = await asyncio.wait(
                {execution, heartbeat}, return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done and not execution.done():
                execution.cancel()
                try:
                    await execution
                except asyncio.CancelledError:
                    pass
                raise WorkerLeaseLost("任务租约丢失，已请求停止当前任务")
            report = await execution
            if lease_lost.is_set():
                raise WorkerLeaseLost("任务租约丢失，拒绝提交完成状态")
            if not self.queue.finish(task.task_id, self.owner, result=report):
                raise WorkerLeaseLost("任务完成状态写入失败，租约可能已失效")
        except WorkerLeaseLost:
            # 不尝试覆盖其他 worker 的状态；租约过期后由恢复流程标记 interrupted。
            logger.error(
                "任务因租约丢失停止 task_id=%s tenant_id=%s",
                task.task_id, task.tenant_id,
            )
        except asyncio.CancelledError:
            execution.cancel()
            raise
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
            try:
                self.queue.fail(
                    task.task_id, self.owner,
                    error_code=error_code, error_message=safe_message,
                )
            except Exception:
                # 数据库短暂不可用时不能杀死消费者；租约过期后恢复流程会标记 interrupted。
                logger.exception("写入任务失败状态异常 task_id=%s", task.task_id)
        finally:
            if not execution.done():
                execution.cancel()
                try:
                    await execution
                except asyncio.CancelledError:
                    pass
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
