"""全局任务队列消费者。

消费者独立于前台活动账号组。每个任务按自身 tenant_id 构造运行时，并在异步上下文中
绑定对应登录态目录。生产轮次与账号组定时作业统一由全局单消费者执行。
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import uuid
from datetime import datetime
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
        shutdown_grace_seconds: float | None = None,
    ):
        self.queue = queue
        self.build_tenant_context = build_tenant_context
        self.poll_interval_seconds = poll_interval_seconds
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        if shutdown_grace_seconds is None:
            try:
                shutdown_grace_seconds = float(os.getenv("ITERWAVE_TASK_SHUTDOWN_GRACE_SECONDS", "30"))
            except (TypeError, ValueError):
                shutdown_grace_seconds = 30.0
        self.shutdown_grace_seconds = max(0.0, min(float(shutdown_grace_seconds), 300.0))
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
                await self._execute(task, stop_event=stop_event)
        finally:
            self.queue.release_worker(self.owner)
            logger.info("全局任务消费者退出 worker=%s", self.owner[:8])

    async def _wait(self, stop_event: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _heartbeat_loop(
        self, task: QueuedTask, stop_event: threading.Event,
        loop: asyncio.AbstractEventLoop, lease_lost: asyncio.Event,
    ) -> None:
        """在线程中续租，避免同步 I/O 阻塞事件循环时心跳也停止。"""
        while not stop_event.wait(self.heartbeat_seconds):
            try:
                renewed = self.queue.renew(
                    task.task_id, self.owner, lease_seconds=self.lease_seconds,
                )
            except Exception:
                logger.exception("任务租约续期异常 task_id=%s", task.task_id)
                loop.call_soon_threadsafe(lease_lost.set)
                return
            if not renewed:
                logger.error("任务租约续期失败，停止执行 task_id=%s", task.task_id)
                loop.call_soon_threadsafe(lease_lost.set)
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
        from src.core.notify import bind_context_notifier

        data_dir = Path(context["cfg"].resolve_path(context["cfg"].paths.data_dir))
        try:
            # 生产任务只允许在入队时配置与母计划版本仍一致时执行，防止排队期间配置热更新改变任务含义。
            snapshot = task.payload.get("execution_snapshot")
            if "execution_snapshot" in task.payload:
                if not isinstance(snapshot, dict):
                    from src.core.task_snapshot import StaleTaskSnapshot
                    raise StaleTaskSnapshot("任务执行快照格式无效，已拒绝执行")
                from src.core.task_snapshot import validate_execution_snapshot
                validate_execution_snapshot(snapshot, context["cfg"], context["conn"])
            # 后台通知必须与任务 tenant_id 绑定，不能读取前台切换时更新的模块级通知器。
            with login_manager.tenant_login_dir(data_dir), bind_context_notifier(context.get("notifier")):
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
                        try:
                            report = await context["executor"].run(
                                round_id,
                                topic_count=task.payload.get("topic_count"),
                                platform_values=task.payload.get("platforms"),
                                save_drafts=task.payload.get("save_drafts"),
                                feedback=feedback,
                            )
                        except asyncio.CancelledError:
                            self._mark_content_plan_failed(context["conn"], round_id)
                            raise
                        except Exception:
                            self._mark_content_plan_failed(context["conn"], round_id)
                            raise
                        self._complete_content_plan(context["conn"], round_id, report)
                        return report
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

    @staticmethod
    def _mark_content_plan_failed(conn, round_id: int) -> None:
        """生产异常时将关联计划标记失败，允许用户修复后重试。"""
        if conn is None or not callable(getattr(conn, "execute", None)):
            return
        relation = conn.execute(
            "SELECT plan_id FROM content_plan_rounds WHERE round_id=?", (round_id,),
        ).fetchone()
        if relation is None:
            return
        conn.execute(
            "UPDATE content_plans SET status='failed',version=version+1,updated_at=? "
            "WHERE id=? AND status='generating'",
            (datetime.now().astimezone().isoformat(timespec="seconds"), relation["plan_id"]),
        )
        conn.commit()

    @staticmethod
    def _complete_content_plan(conn, round_id: int, report: Any) -> None:
        """生产完成后把轮次产出的草稿关联回计划，并推进计划状态。"""
        if conn is None or not callable(getattr(conn, "execute", None)):
            return
        relation = conn.execute(
            "SELECT plan_id FROM content_plan_rounds WHERE round_id=?", (round_id,),
        ).fetchone()
        if relation is None:
            return
        plan_id = relation["plan_id"]
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        drafts = conn.execute(
            "SELECT d.id FROM drafts d JOIN contents c ON c.id=d.content_id WHERE c.round_id=?",
            (round_id,),
        ).fetchall()
        for draft in drafts:
            conn.execute(
                "INSERT OR IGNORE INTO content_plan_drafts(plan_id,draft_id,round_id,created_at) VALUES(?,?,?,?)",
                (plan_id, draft["id"], round_id, now),
            )
        failed = isinstance(report, dict) and report.get("final_status") == "failed"
        status = "failed" if failed else "generated"
        conn.execute(
            "UPDATE content_plans SET status=?,version=version+1,updated_at=? "
            "WHERE id=? AND status='generating'",
            (status, now, plan_id),
        )
        conn.commit()

    async def _execute(
        self, task: QueuedTask, *, stop_event: asyncio.Event | None = None,
    ) -> None:
        lease_lost = asyncio.Event()
        heartbeat_stop = threading.Event()
        loop = asyncio.get_running_loop()
        heartbeat = asyncio.create_task(asyncio.to_thread(
            self._heartbeat_loop, task, heartbeat_stop, loop, lease_lost,
        ))
        execution = asyncio.create_task(self._run_task(task))
        stop_waiter = asyncio.create_task(stop_event.wait()) if stop_event is not None else None
        try:
            wait_for = {execution, heartbeat}
            if stop_waiter is not None:
                wait_for.add(stop_waiter)
            done, _ = await asyncio.wait(wait_for, return_when=asyncio.FIRST_COMPLETED)

            if stop_waiter is not None and stop_waiter in done and not execution.done():
                completed, _ = await asyncio.wait(
                    {execution}, timeout=self.shutdown_grace_seconds,
                )
                if not completed:
                    execution.cancel()
                    try:
                        await execution
                    except asyncio.CancelledError:
                        pass
                    heartbeat_stop.set()
                    await heartbeat
                    if not self.queue.interrupt(task.task_id, self.owner):
                        raise WorkerLeaseLost("停机时任务租约已失效，无法安全写入 interrupted")
                    logger.warning(
                        "服务停止超出宽限期，任务已中断 task_id=%s tenant_id=%s",
                        task.task_id, task.tenant_id,
                    )
                    return

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
            logger.error(
                "任务因租约丢失停止 task_id=%s tenant_id=%s",
                task.task_id, task.tenant_id,
            )
        except asyncio.CancelledError:
            execution.cancel()
            raise
        except Exception as exc:  # noqa: BLE001 — 单任务失败不得终止消费者
            from src.core.redact import redact
            error_code = redact(str(getattr(exc, "code", "round_execution_failed")))[:80]
            # 历史列表需要可诊断的失败原因；先统一脱敏，再隐藏本机绝对路径。
            # 不把原始 traceback、请求头或平台响应对象写入用户可见字段。
            import re
            detail = redact(str(exc)).strip()
            detail = re.sub(r"(?<!:)/(?:[^\s:'\"<>]+/)*[^\s:'\"<>]+", "[路径已隐藏]", detail)
            safe_message = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
            safe_message = safe_message[:500]
            logger.error(
                "全局队列任务执行失败 task_id=%s tenant_id=%s error=%s",
                task.task_id, task.tenant_id, safe_message,
            )
            try:
                self.queue.fail(
                    task.task_id, self.owner,
                    error_code=error_code, error_message=safe_message,
                )
            except Exception:
                logger.exception("写入任务失败状态异常 task_id=%s", task.task_id)
        finally:
            if not execution.done():
                execution.cancel()
                try:
                    await execution
                except asyncio.CancelledError:
                    pass
            heartbeat_stop.set()
            if not heartbeat.done():
                await heartbeat
            if stop_waiter is not None:
                stop_waiter.cancel()
                try:
                    await stop_waiter
                except asyncio.CancelledError:
                    pass
