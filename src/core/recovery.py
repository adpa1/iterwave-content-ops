"""
断点恢复 —— 进程重启后接续未完成的工作（R16/R17）。

启动时执行一次（lifespan 中 scheduler.start() 之后）：
1. 把遗留的 running 任务标为 interrupted（崩溃现场留痕）
2. 为「已确认发布但没有采集任务」的草稿重新注册 data_fetch
   （APScheduler JobStore 会持久化大部分任务，这一步兜底异常丢失）
3. 列出仍卡在 producing 的轮次，交给用户决定是否重新执行
4. 统计 pending_verify（R17：不自动重试，必须用户核实）

注意：本模块只做「读 + 补登记」，绝不重复执行可能已经发生的写操作。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


class RecoveryManager:
    """启动期断点恢复。"""

    def __init__(self, cfg, conn, scheduler=None):
        self.cfg = cfg
        self.conn = conn
        self.scheduler = scheduler

    def recover(self) -> dict:
        report = {
            "interrupted_tasks": 0,
            "requeued_data_fetch": [],
            "stale_rounds": [],
            "pending_verify": 0,
        }
        report["interrupted_tasks"] = self._mark_interrupted_running()
        report["requeued_data_fetch"] = self._requeue_missing_data_fetch()
        report["stale_rounds"] = self._stale_producing_rounds()
        report["pending_verify"] = self._count_pending_verify()

        if (report["interrupted_tasks"] or report["requeued_data_fetch"]
                or report["stale_rounds"] or report["pending_verify"]):
            logger.warning(
                "♻️ 断点恢复: 中断任务 %d / 补登记采集 %d / 卡住轮次 %s / 待核实 %d",
                report["interrupted_tasks"], len(report["requeued_data_fetch"]),
                report["stale_rounds"] or "-", report["pending_verify"],
            )
        else:
            logger.info("♻️ 断点恢复检查完成：没有需要接续的任务")
        return report

    # ------------------------------------------------------------------

    def _mark_interrupted_running(self) -> int:
        """上次进程被强杀留下的 running 行 → interrupted。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = self.conn.execute(
            "SELECT id FROM task_runs WHERE status = 'running'"
        ).fetchall()
        if not rows:
            return 0
        self.conn.execute(
            "UPDATE task_runs SET status = 'interrupted', finished_at = ?, "
            "error_msg = COALESCE(error_msg, '进程重启，任务被中断') WHERE status = 'running'",
            (now,),
        )
        self.conn.commit()
        return len(rows)

    def _requeue_missing_data_fetch(self) -> list[str]:
        """已发布草稿若没有对应的 data_fetch 任务，补登记一次。"""
        rows = self.conn.execute(
            "SELECT d.id, d.platform FROM drafts d "
            "WHERE d.status IN ('user_published','data_fetching') "
            "AND NOT EXISTS (SELECT 1 FROM metrics m WHERE m.draft_id = d.id "
            "                AND m.fetch_status IN ('success','partial'))"
        ).fetchall()
        requeued: list[str] = []
        job_ids: set[str] = set()
        if self.scheduler is not None:
            try:
                job_ids = {j.id for j in self.scheduler.scheduler.get_jobs()}
            except Exception:  # noqa: BLE001 — JobStore 未就绪时继续补登记
                logger.debug("查询 JobStore 失败，直接补登记 data_fetch 任务")
        for row in rows:
            draft_id = row["id"]
            # 任务 id 形如 data_fetch_{draft_id}_a{n}（attempt 区分重试），按前缀判重
            prefix = f"data_fetch_{draft_id}"
            if any(jid.startswith(prefix) for jid in job_ids):
                continue
            if self.scheduler is not None:
                run_at = datetime.now() + timedelta(minutes=1)
                self.scheduler.schedule_data_fetch(draft_id, run_at)
                requeued.append(draft_id)

            self.conn.execute(
                "INSERT INTO task_runs (task_type, biz_id, platform, status, attempt, "
                "next_run_at) VALUES ('data_fetch', ?, ?, 'pending', 1, ?)",
                (
                    draft_id, row["platform"],
                    (datetime.now() + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
        if requeued:
            self.conn.commit()
        return requeued

    def _stale_producing_rounds(self) -> list[int]:
        """卡在 producing 且无 running 任务的轮次 → 实际迁移（G-07）。

        - 已有存草稿/已发布篇目 → 生产其实已部分完成 → awaiting_publish
        - 一篇都没有 → failed
        """
        rows = self.conn.execute(
            "SELECT id FROM spiral_rounds WHERE status = 'producing' "
            "AND NOT EXISTS (SELECT 1 FROM task_runs t WHERE t.task_type = 'round_pipeline' "
            "                AND t.biz_id = CAST(spiral_rounds.id AS TEXT) "
            "                AND t.status = 'running')"
        ).fetchall()
        if not rows:
            return []

        from .state_machine import RoundStateMachine

        sm = RoundStateMachine(self.conn)
        migrated: list[int] = []
        for row in rows:
            round_id = row["id"]
            saved = self.conn.execute(
                "SELECT COUNT(*) FROM drafts d JOIN contents c ON d.content_id = c.id "
                "WHERE c.round_id = ? AND d.status IN ('draft_saved','user_published','published')",
                (round_id,),
            ).fetchone()[0]
            if saved:
                target = "awaiting_publish"
                reason = f"进程重启，生产中断；已有 {saved} 篇草稿落定，转待发布"
            else:
                target = "failed"
                reason = "进程重启，生产中断且无草稿落定"
            if sm.transition(round_id, target, reason=reason):
                migrated.append(round_id)
                logger.warning("♻️ 轮次 %s 卡在 producing → 已迁移为 %s", round_id, target)
        return migrated

    def _count_pending_verify(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM task_runs WHERE status = 'pending_verify'"
        ).fetchone()
        return int(row["n"]) if row else 0
