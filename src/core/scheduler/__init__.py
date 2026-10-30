"""
APScheduler 调度器 — 8 类定时任务（§7）。

设计要点：
- SQLiteJobStore 持久化（重启恢复）
- AsyncIOScheduler 配合 async Playwright
- 每平台独立 asyncio.Lock（同平台串行，跨平台并行）
- 所有外部操作走幂等包装（R16）

8 类任务（设计方案 §13.1 补充了 data_retention_cleanup；R8 补充轮次阶段超时扫描）：
0. publish_polling        — 用户发布确认双通道（每 30min 只读轮询）
1. data_fetch             — 发布后 metrics_initial_delay_hours（默认 24h）触发
2. daily_ops              — 每日运营计划启动（每天 8:00）
3. comment_polling        — 被动运营评论轮询（每 30min）
4. cb_recovery            — 熔断 cooldown 到期自动触发 3 步恢复（每 5min 扫一次）
5. kb_scan                — 选题库增量扫描（每天 6:00）
6. data_retention_cleanup — 数据保留清理（R23，每天 6:00）
7. round_timeout_sweeper  — 轮次阶段超时判定（R8，每小时）
8. round_review           — 复盘收口（每 10min）
9. hot_topics_refresh     — 热点采集 + 标签匹配（默认每 120min）
10. autogrow_next_round   — 自动闭环：条件满足时开下一轮（默认每 60min 检查）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import SchedulerNotRunningError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger
from zoneinfo import ZoneInfo
from ..timezone import business_now, business_timestamp

logger = logging.getLogger(__name__)

# SQLAlchemyJobStore 需要模块级可 pickle 的包装函数。以数据库绝对路径作为稳定引擎键，
# 防止不同账号组的持久化任务通过可变的“当前活动引擎”指针串租。
_ENGINES: dict[str, "SchedulerEngine"] = {}


class SchedulerEngine:
    """APScheduler 调度器 — 单例，模块级创建。"""

    def __init__(self, cfg, db_path: Path):
        self.cfg = cfg
        self.db_path = Path(db_path).resolve()
        self.engine_key = str(self.db_path)
        # SQLiteJobStore 需要 SQLAlchemy URL
        self.scheduler = AsyncIOScheduler(
            jobstores={
                "default": SQLAlchemyJobStore(url=f"sqlite:///{db_path}"),
            },
            timezone=ZoneInfo(self.cfg.business_timezone),
            # 应用关闭期间错过的任务：按配置的宽限期决定重启后是否补跑。
            job_defaults={"misfire_grace_time": self.cfg.scheduler.misfire_grace_minutes * 60, "coalesce": True},
        )
        # 每平台一把 asyncio.Lock（R13.3）
        from ...adapters.base import Platform
        self.platform_locks: dict[Platform, asyncio.Lock] = {
            p: asyncio.Lock() for p in Platform
        }

        # 由 lifespan 注入的运行时依赖（wire()）
        self.conn = None
        self.adapters: dict = {}
        self.sm_draft = None
        self.sm_round = None
        self.cb = None
        self.notifier = None
        # 由 lifespan 注入，可能为 None
        self.ops = None
        self.kb_reader = None
        self.spiral = None
        self.scoring = None
        self.goal_progress = None
        # G-03：主动评论内容必须过终极目标检查（R2），缺省为 None → 拒绝发送
        self.guard = None
        # 自动闭环控制器（lifespan 注入；None → autogrow_next_round 跳过）
        self.autogrow = None
        # 运行中的 job 数，用于账号组切换时先排空任务再释放连接。
        self._active_job_count = 0

    def wire(self, *, conn, adapters, sm_draft=None, sm_round=None,
             circuit_breaker=None, notifier=None, ops=None,
             kb_reader=None, spiral=None, scoring=None, goal_progress=None,
             guard=None) -> None:
        """注入运行时依赖。必须在 start() 之前调用。"""
        self.conn = conn
        self.adapters = adapters or {}
        self.sm_draft = sm_draft
        self.sm_round = sm_round
        self.cb = circuit_breaker
        self.notifier = notifier
        self.ops = ops
        self.kb_reader = kb_reader
        self.spiral = spiral
        self.scoring = scoring
        self.goal_progress = goal_progress
        self.guard = guard
        logger.info("🔌 Scheduler 依赖注入完成（%d 个平台适配器）", len(self.adapters))

    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动调度器并绑定持久化任务；先暂停，完成旧任务迁移后再执行。"""
        current = _ENGINES.get(self.engine_key)
        if current is not None and current is not self:
            raise RuntimeError(f"该数据库已有活动调度器：{self.engine_key}")
        _ENGINES[self.engine_key] = self
        try:
            self.scheduler.start(paused=True)
            self._register_all_jobs()
            self._bind_persisted_data_fetch_jobs()
            self.scheduler.resume()
        except Exception:
            if _ENGINES.get(self.engine_key) is self:
                _ENGINES.pop(self.engine_key, None)
            try:
                self.scheduler.shutdown(wait=False)
            except SchedulerNotRunningError:
                pass
            raise
        logger.info("✅ APScheduler 启动，本地时区（R5），已注册 %d 类任务",
                    len(self.scheduler.get_jobs()))

    def _bind_persisted_data_fetch_jobs(self) -> None:
        """给旧版本留下的一次性采集任务补上稳定引擎键，避免落入其他账号组。"""
        for job in self.scheduler.get_jobs():
            if not job.id.startswith("data_fetch_"):
                continue
            kwargs = dict(job.kwargs or {})
            if kwargs.get("engine_key") == self.engine_key:
                continue
            kwargs["engine_key"] = self.engine_key
            self.scheduler.modify_job(job.id, kwargs=kwargs)

    def shutdown(self) -> None:
        """停止调度器并解除本引擎注册；未启动时也可安全调用。"""
        try:
            self.scheduler.shutdown(wait=False)
            logger.info("⏹️ APScheduler 已关闭")
        except SchedulerNotRunningError:
            logger.debug("APScheduler 未启动，无需关闭")
        finally:
            if _ENGINES.get(self.engine_key) is self:
                _ENGINES.pop(self.engine_key, None)

    async def _run_tracked(self, method_name: str, *args) -> None:
        """跟踪后台任务生命周期；热切换等待这些任务退出后才释放连接。"""
        self._active_job_count += 1
        try:
            await getattr(self, method_name)(*args)
        finally:
            self._active_job_count -= 1

    async def quiesce(self, timeout_seconds: float = 30.0) -> bool:
        """暂停新调度并等待已启动任务结束；超时则恢复调度并返回 False。"""
        if self.scheduler.running:
            self.scheduler.pause()
        # pause() 在 AsyncIOScheduler 中投递到事件循环，先让暂停动作和已提交 job 生效。
        await asyncio.sleep(0.05)
        deadline = time.monotonic() + timeout_seconds
        while self._active_job_count > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        if self._active_job_count > 0:
            if self.scheduler.running:
                self.scheduler.resume()
            return False
        self.shutdown()
        await asyncio.sleep(0)
        return True

    # ------------------------------------------------------------------
    def _register_all_jobs(self) -> None:
        """按 config.scheduler.jobs 注册定时任务（开关 / 间隔 / cron 全部来自配置）。"""
        for job_id in JOB_RUNNERS:
            self.apply_job_config(job_id)

    def _trigger_for(self, job_id: str):
        sched = self.cfg.scheduler.job(job_id)
        if sched.cron:
            return CronTrigger.from_crontab(sched.cron, timezone=ZoneInfo(self.cfg.business_timezone))
        return IntervalTrigger(minutes=int(sched.interval_minutes))

    def apply_job_config(self, job_id: str) -> dict:
        """按当前配置（重新）排定单个任务：关闭则移除，开启则 replace_existing 重排。

        运行时修改配置（PATCH /api/config scheduler.jobs.*）后调用，立即生效，无需重启。
        """
        if job_id not in JOB_RUNNERS:
            raise KeyError(f"未知定时任务：{job_id}")
        sched = self.cfg.scheduler.job(job_id)
        enabled = sched.enabled
        if job_id == "hot_topics_refresh" and getattr(self.cfg.hot_topics, "enabled", True) is False:
            enabled = False
        if not enabled:
            if self.scheduler.get_job(job_id) is not None:
                self.scheduler.remove_job(job_id)
            logger.info("⏸️ [APS] 定时任务 %s 已关闭", job_id)
            return {"job_id": job_id, "enabled": False}
        if self.scheduler.get_job(job_id) is not None:
            self.scheduler.remove_job(job_id)
        self.scheduler.add_job(
            JOB_RUNNERS[job_id],
            self._trigger_for(job_id),
            id=job_id,
            args=[self.engine_key],
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info("⏰ [APS] 定时任务 %s：%s", job_id, sched.describe())
        return {"job_id": job_id, "enabled": True, "schedule": sched.describe()}

    # ------------------------------------------------------------------
    # 外部接口：动态注册 data_fetch
    # ------------------------------------------------------------------

    def on_user_published(
        self, draft_id: str, via: str, note_url: Optional[str] = None,
        confirmed_at: Optional[datetime] = None,
    ) -> bool:
        """用户已在平台发布（手动确认 / 只读轮询识别）的统一入口：

        1. 草稿 draft_saved → user_published（幂等）
        2. 回写平台笔记链接（被动评论轮询靠它定位笔记）
        3. 注册发布后 metrics_initial_delay_hours 的一次性数据采集
        4. 首篇确认 → 轮次 awaiting_publish → collecting_metrics
        手动确认和轮询识别都只调它，保证两条路径行为一致。
        """
        from datetime import timedelta

        if self.conn is None or self.sm_draft is None:
            return False
        now = confirmed_at or business_now(self.cfg)
        ok = self.sm_draft.transition(
            draft_id, "user_published", reason=via,
            extra_updates={"user_confirmed_at": now.isoformat()},
        )
        if not ok:
            return False
        if note_url:
            self.conn.execute(
                "UPDATE drafts SET platform_note_url = COALESCE(platform_note_url, ?) WHERE id = ?",
                (note_url, draft_id),
            )
            self.conn.commit()
        delay = self.cfg.timeouts.metrics_initial_delay_hours
        self.schedule_data_fetch(draft_id, now + timedelta(hours=delay))
        row = self.conn.execute(
            "SELECT c.round_id FROM drafts d JOIN contents c ON d.content_id = c.id WHERE d.id = ?",
            (draft_id,),
        ).fetchone()
        if self.sm_round is not None and row and row["round_id"] is not None:
            try:
                self.sm_round.on_first_publish_confirmed(row["round_id"])
            except Exception:  # noqa: BLE001 — 推进失败不影响发布确认本身
                logger.exception("[APS] 推进轮次失败 draft=%s round=%s", draft_id, row["round_id"])
        return True

    def backfill_data_fetch(self) -> int:
        """补登记丢失的数据采集：已确认发布、到了采集时间、却既没有采集任务也没有采集结果的草稿。

        覆盖：应用离线错过任务、旧版本手动确认没注册任务等情况。返回补登记篇数。
        """
        from datetime import timedelta

        if self.conn is None:
            return 0
        delay = self.cfg.timeouts.metrics_initial_delay_hours
        cutoff = (business_now(self.cfg) - timedelta(hours=delay)).isoformat()
        rows = self.conn.execute(
            "SELECT d.id FROM drafts d WHERE d.status IN ('user_published', 'data_fetching') "
            "AND d.user_confirmed_at IS NOT NULL AND d.user_confirmed_at <= ? "
            "AND NOT EXISTS (SELECT 1 FROM metrics m WHERE m.draft_id = d.id "
            "AND m.fetch_status IN ('success', 'partial'))",
            (cutoff,),
        ).fetchall()
        n = 0
        for r in rows:
            pending = [j for j in self.scheduler.get_jobs() if j.id.startswith(f"data_fetch_{r['id']}_")]
            if pending:
                continue
            self.schedule_data_fetch(r["id"], business_now(self.cfg) + timedelta(seconds=5))
            n += 1
        if n:
            logger.warning("[APS] 补登记数据采集 %d 篇（任务丢失或旧版本未注册）", n)
        return n

    def schedule_data_fetch(
        self, draft_id: str, run_at: datetime, attempt: int = 1,
    ) -> None:
        """为已发布的 draft 注册一次性数据采集任务（attempt 用于失败重试计数）。"""
        self.scheduler.add_job(
            _run_data_fetch,
            DateTrigger(run_date=run_at),
            id=f"data_fetch_{draft_id}_a{attempt}",
            kwargs={"engine_key": self.engine_key, "draft_id": draft_id, "attempt": attempt},
            replace_existing=True,
        )
        logger.info("📅 注册 data_fetch: draft=%s attempt=%d run_at=%s",
                    draft_id, attempt, run_at.isoformat())

    def _metrics_retry_params(self) -> tuple[int, int]:
        """返回配置化的数据采集重试次数与间隔（分钟）。"""
        return (
            self.cfg.timeouts.metrics_retry_attempts,
            self.cfg.timeouts.metrics_retry_interval_minutes,
        )

    # ------------------------------------------------------------------
    # 7 类任务实现（全部 stub，Phase 5-6 补真实逻辑）
    # ------------------------------------------------------------------

    async def _job_publish_polling(self) -> None:
        """每 30min 只读检查平台侧是否已发布（R1：只读，绝不碰表单）。

        命中平台作品列表 → 视为用户已手动发布 → draft_saved → user_published，
        并按 metrics_initial_delay_hours 注册一次性 data_fetch。
        """
        from datetime import timedelta

        from ...adapters.base import Platform
        from ...core.login_manager import has_login_state

        if self.conn is None:
            logger.warning("[APS] publish_polling 跳过：依赖未注入")
            return

        rows = self.conn.execute(
            "SELECT d.id, d.platform, d.draft_id, d.saved_at, d.saved_content_hash, "
            "c.round_id AS round_id, COALESCE(d.saved_platform_title, c.title) AS poll_title, "
            "c.title AS local_title, c.body, c.tags, c.image_paths, "
            "c.review_rejected AS review_rejected, "
            "c.review_rejection_reason AS review_rejection_reason "
            "FROM drafts d JOIN contents c ON d.content_id = c.id "
            "WHERE d.status = 'draft_saved'"
        ).fetchall()
        # 每次轮询顺带补登记丢失的数据采集任务
        try:
            self.backfill_data_fetch()
        except Exception:  # noqa: BLE001
            logger.exception("[APS] 补登记数据采集失败")
        if not rows:
            return

        delay_hours = self.cfg.timeouts.metrics_initial_delay_hours
        confirmed = 0

        for row in rows:
            try:
                platform = Platform(row["platform"])
            except ValueError:
                continue
            adapter = self.adapters.get(platform)
            if adapter is None or not has_login_state(platform):
                continue

            async with self.platform_locks[platform]:
                # 标题匹配要使用成功存稿时的快照，而非可能已经被用户本地修改的标题。
                snap = await adapter.fetch_metrics(row["draft_id"], row["poll_title"] or "")

            if snap.fetch_status.value not in ("success", "partial"):
                continue  # 平台侧没看到 → 用户还没发

            if row["platform"] == "wechat_channels":
                # 视频号读回拿不到作品标题，按标题匹配会误判；只接受手动确认
                continue
            work_url = getattr(snap, "work_url", None) or None
            if self.on_user_published(
                row["id"], via="30min 只读轮询确认平台侧已发布", note_url=work_url,
            ):
                confirmed += 1
                if bool(row["review_rejected"]):
                    # 审核驳回不能抹掉平台已经发生的真实发布；记录为观察到的例外，
                    # 继续采集数据，同时让后续复盘能追溯到驳回原因。
                    try:
                        self.conn.execute(
                            "INSERT INTO user_override (round_id, override_field, original_value, "
                            "override_value, reason, overridden_by, overridden_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                row["round_id"], "publish_review_rejection_observed",
                                json.dumps({
                                    "review_rejected": True,
                                    "review_rejection_reason": row["review_rejection_reason"],
                                }, ensure_ascii=False)[:500],
                                json.dumps({"status": "user_published", "via": "platform_polling"}, ensure_ascii=False),
                                "系统在平台侧检测到已发布作品，为保留真实运营数据继续采集",
                                "publish-polling",
                                business_now(self.cfg).strftime("%Y-%m-%d %H:%M:%S"),
                            ),
                        )
                        self.conn.commit()
                    except Exception:  # noqa: BLE001 — 实际发布已发生，审计失败不能中断采集
                        logger.exception("[APS] 发布驳回例外审计写入失败 draft=%s", row["id"])
                # 即使自动识别到了真实发布，也记录平台实际版本与当前本地版本是否一致。
                # 这不阻断已发生的发布事实或采集，但让复盘知道指标对应哪一版内容。
                try:
                    from ...core.content_version import content_version_hash, parse_json_list
                    images = parse_json_list(row["image_paths"])
                    payload_images = [str(images[0])] if images and images[0] else []
                    current_hash = content_version_hash(
                        row["local_title"], row["body"], parse_json_list(row["tags"]), payload_images,
                    )
                    saved_hash = row["saved_content_hash"]
                    sync_state = (
                        "unknown" if not saved_hash
                        else "matched" if saved_hash == current_hash
                        else "stale"
                    )
                    if sync_state != "matched":
                        self.conn.execute(
                            "INSERT INTO user_override (round_id, override_field, original_value, "
                            "override_value, reason, overridden_by, overridden_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (
                                row["round_id"], "platform_content_sync_observed",
                                json.dumps({
                                    "state": sync_state,
                                    "saved_content_hash": saved_hash,
                                    "current_content_hash": current_hash,
                                    "platform_title": row["poll_title"],
                                    "local_title": row["local_title"],
                                }, ensure_ascii=False)[:500],
                                json.dumps({
                                    "status": "user_published",
                                    "via": "platform_polling",
                                }, ensure_ascii=False),
                                f"系统已在平台侧确认作品发布；平台内容版本状态为 {sync_state}，保留真实发布并继续采集",
                                "publish-polling",
                                business_now(self.cfg).strftime("%Y-%m-%d %H:%M:%S"),
                            ),
                        )
                        self.conn.commit()
                except Exception:  # noqa: BLE001 — 已确认发布不应因审计问题中断采集
                    logger.exception("[APS] 平台版本观察审计写入失败 draft=%s", row["id"])

                if getattr(self, "notifier", None):
                    self.notifier.emit(
                        "publish_confirmed",
                        f"{platform.display_name} 已确认发布：{(row['poll_title'] or '')[:30]}",
                        platform=platform.value,
                    )

        logger.info("[APS] publish_polling 完成：扫描 %d 篇，确认发布 %d 篇", len(rows), confirmed)

    async def _job_data_fetch(self, draft_id: str, attempt: int = 1) -> None:
        """发布后 metrics_initial_delay_hours（默认 24h）拉取平台数据。

        失败按 timeouts.metrics_retry_attempts / metrics_retry_interval_minutes 配置重试；
        耗尽后保留 fetch_status=failed，由 round_timeout_sweeper 按 R8 收口。
        """
        import json  # noqa: F401 — 保留供后续 raw 扩展

        from ...adapters.base import FetchStatus, MetricsSnapshot, Platform

        if self.conn is None:
            logger.warning("[APS] data_fetch 跳过：依赖未注入")
            return

        row = self.conn.execute(
            "SELECT d.id, d.platform, d.draft_id, d.status, c.id AS content_id, c.title "
            "FROM drafts d JOIN contents c ON d.content_id = c.id WHERE d.id = ?",
            (draft_id,),
        ).fetchone()
        if not row:
            logger.warning("[APS] data_fetch 草稿不存在: %s", draft_id)
            return

        platform = Platform(row["platform"])
        adapter = self.adapters.get(platform)
        if adapter is None:
            # 禁用/未构造适配器是可恢复的采集失败，不能静默返回并丢掉一次性任务。
            # 走统一失败落库与重试分支；如果之后适配器恢复，下一次尝试仍可成功。
            reason = f"{platform.display_name} 数据采集适配器未启用或不可用"
            logger.error("[APS] data_fetch 适配器缺失 draft=%s platform=%s", draft_id, platform.value)
            snap = MetricsSnapshot(
                platform=platform,
                fetch_status=FetchStatus.FAILED,
                error_msg=reason,
            )
        else:
            if self.sm_draft:
                self.sm_draft.transition(draft_id, "data_fetching", reason="定时采集开始")
            async with self.platform_locks[platform]:
                snap = await adapter.fetch_metrics(row["draft_id"], row["title"] or "")

        # 落库 metrics（R3：解析不到的字段保持 NULL，绝不补 0）
        # R16 幂等：draft_id + platform + 采集日期当天只覆盖一次（当天已有记录 → UPDATE 而非 INSERT）
        today = business_now(self.cfg).strftime("%Y-%m-%d")
        prev = self.conn.execute(
            "SELECT id, attempt FROM metrics WHERE draft_id = ? AND platform = ? "
            "AND substr(COALESCE(fetched_at, ''), 1, 10) = ?",
            (draft_id, platform.value, today),
        ).fetchone()
        if prev:
            # 注意：这里的 metrics_attempt 是「当天第几次采集」，与作业重试 attempt 无关
            metrics_attempt = int(prev["attempt"] or 0) + 1
            self.conn.execute(
                "UPDATE metrics SET fetch_status = ?, fetched_at = ?, attempt = ?, "
                "raw_response = ?, error_msg = ?, likes = ?, collects = ?, comments = ?, shares = ?, "
                "views = ?, new_followers = ?, like_rate = ?, collect_rate = ?, "
                "interaction_rate = ?, follower_efficiency = ? WHERE id = ?",
                (
                    snap.fetch_status.value, business_now(self.cfg).isoformat(), metrics_attempt,
                    snap.raw_response, snap.error_msg, snap.likes, snap.collects, snap.comments,
                    snap.shares, snap.views, snap.new_followers,
                    snap.like_rate, snap.collect_rate, snap.interaction_rate,
                    snap.follower_efficiency, prev["id"],
                ),
            )
            logger.info("[APS] data_fetch 当天重复采集 → 覆盖 metrics id=%s（第 %d 次）",
                        prev["id"], metrics_attempt)
        else:
            self.conn.execute(
                "INSERT INTO metrics (draft_id, content_id, platform, fetch_status, fetched_at, "
                "attempt, raw_response, error_msg, likes, collects, comments, shares, views, new_followers, "
                "like_rate, collect_rate, interaction_rate, follower_efficiency) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    draft_id, row["content_id"], platform.value, snap.fetch_status.value,
                    business_now(self.cfg).isoformat(), snap.raw_response, snap.error_msg,
                    snap.likes, snap.collects, snap.comments, snap.shares, snap.views,
                    snap.new_followers,
                    snap.like_rate, snap.collect_rate, snap.interaction_rate,
                    snap.follower_efficiency,
                ),
            )
        self.conn.commit()

        # R21：采集成功后刷新关联目标进度（任一指标 NULL 时保持 NULL）
        if self.goal_progress is not None:
            try:
                self.goal_progress.update_all()
            except Exception:  # noqa: BLE001 — 目标刷新失败不能影响采集主流程
                logger.exception("[APS] 目标进度刷新失败")

        if snap.fetch_status.value in ("success", "partial"):
            if self.sm_draft:
                self.sm_draft.transition(
                    draft_id, "published", reason="数据采集完成",
                    extra_updates={"error_msg": None},
                )
        else:
            logger.warning("[APS] data_fetch 采集未成功 draft=%s status=%s err=%s (attempt=%d)",
                           draft_id, snap.fetch_status.value, snap.error_msg, attempt)
            self.conn.execute(
                "UPDATE drafts SET error_msg = ? WHERE id = ?",
                (snap.error_msg, draft_id),
            )
            self.conn.commit()

            retry_times, interval_min = self._metrics_retry_params()
            if attempt < retry_times:
                next_at = business_now(self.cfg) + timedelta(minutes=interval_min)
                self.schedule_data_fetch(draft_id, next_at, attempt=attempt + 1)
                logger.warning(
                    "[APS] data_fetch 将重试：draft=%s 第 %d/%d 次，预计 %s",
                    draft_id, attempt + 1, retry_times, next_at.isoformat(timespec="seconds"),
                )
            else:
                logger.error("[APS] data_fetch 重试耗尽（%d 次）：draft=%s", attempt, draft_id)
                # 不再停在「采集中」拖满 72h：转失败，轮次可以按其余草稿正常收口
                if self.sm_draft:
                    self.sm_draft.transition(
                        draft_id, "failed", reason=f"数据采集重试 {attempt} 次仍未成功",
                        extra_updates={"error_msg": (snap.error_msg or "数据采集失败")[:500]},
                    )
                if getattr(self, "notifier", None):
                    self.notifier.emit(
                        "data_fetch_failed",
                        f"{platform.display_name} 数据拉取未成功（{snap.fetch_status.value}）："
                        f"{(row['title'] or '')[:30]} — {snap.error_msg or ''}",
                        platform=platform.value,
                    )

        logger.info("[APS] data_fetch draft=%s → %s", draft_id, snap.fetch_status.value)

    async def _job_daily_ops(self) -> None:
        """每天 8:00 派发主动运营任务（G-03）。

        G-02 之后适配器已是真实 Playwright 实现，这里不再"只统计 + 建议"：
        按 daily_quotas 派发 task_runs，并调用 ops.active_comment() / ops.follow()。

        守则：
        - 熔断平台 → 零动作（不建 task_run、不调 ops）
        - 不在 ops 时段窗口 → 跳过（节奏控制器）
        - 剩余配额 ≤0 → 跳过 + quota_exhausted 通知（配额为 0 = 禁用，只记日志）
        - 目标清单为空 → 不派发（不做自动搜索发现，避免无意义互动）
        """
        from ...adapters.base import Platform

        if self.conn is None or self.ops is None:
            return

        from ...core.ops.pause import is_ops_paused
        if is_ops_paused(self.conn):
            logger.info("[APS] daily_ops 跳过：运营已全局暂停")
            return

        # 今日已执行的运营动作（ops_logs）
        # R5：SQLite date('now') 是 UTC，必须用本地时区当日 00:00
        today_start = business_now(self.cfg).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).isoformat()
        rows = self.conn.execute(
            "SELECT platform, action_type, status, COUNT(*) AS n "
            "FROM ops_logs WHERE executed_at >= ? "
            "GROUP BY platform, action_type, status",
            (today_start,),
        ).fetchall()
        usage: dict[str, dict[str, int]] = {}
        for r in rows:
            usage.setdefault(f"{r['platform']}:{r['action_type']}", {})[r["status"]] = int(r["n"])

        # 今日活跃草稿（已存草稿但未 user_published）
        draft_count = self.conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE status IN ('draft_saved','user_published') "
            "AND substr(created_at, 1, 10) = ?",
            (today_start[:10],),
        ).fetchone()[0]

        targets_map = getattr(self.cfg, "ops_targets", None) or {}
        quotas_map = getattr(self.cfg.rhythm, "daily_quotas", None) or {}
        dispatched = 0

        for plat_name, quotas in quotas_map.items():
            platform = next((p for p in Platform if p.value == plat_name), None)
            if platform is None or platform not in self.adapters:
                continue

            # ① 熔断 → 零动作
            if self.cb and self.cb.is_circuit_broken(platform):
                logger.info("🔴 [APS] daily_ops %s 熔断中 → 零动作", plat_name)
                continue

            # ② 节奏控制器：时段窗口（ops 8-23 本地时区）
            if not self.ops.rhythm.is_within_window(platform, "ops"):
                logger.info("⏰ [APS] daily_ops %s 不在运营时段窗口 → 跳过", plat_name)
                continue

            targets = targets_map.get(plat_name)

            # ③ 主动评论
            if quotas.active_comments <= 0:
                logger.info("📴 [APS] daily_ops %s 主动评论配额为 0（禁用）", plat_name)
            elif self._remaining_quota(platform, "active_comment", quotas.active_comments) <= 0:
                self._emit_quota_exhausted(
                    platform, "active_comment", quotas.active_comments,
                )
            elif targets is None or not targets.active_comments:
                logger.info("📝 [APS] daily_ops %s 未配置主动评论目标 → 不派发", plat_name)
            else:
                for t in targets.active_comments[:self._remaining_quota(
                    platform, "active_comment", quotas.active_comments,
                )]:
                    await self._dispatch_action(platform, "active_comment", t.url, content=t.content)
                    dispatched += 1

            # ④ 关注
            if quotas.follows <= 0:
                logger.info("📴 [APS] daily_ops %s 关注配额为 0（禁用）", plat_name)
            elif self._remaining_quota(platform, "follow", quotas.follows) <= 0:
                self._emit_quota_exhausted(platform, "follow", quotas.follows)
            elif targets is None or not targets.follows:
                logger.info("📝 [APS] daily_ops %s 未配置关注目标 → 不派发", plat_name)
            else:
                for user_id in targets.follows[:self._remaining_quota(
                    platform, "follow", quotas.follows,
                )]:
                    await self._dispatch_action(platform, "follow", user_id)
                    dispatched += 1

        logger.info(
            "📋 [APS] daily_ops 完成 — 今日活跃草稿 %d / 今日已执行 %d 条 ops_logs / 本次派发 %d 条",
            draft_count, sum(sum(v.values()) for v in usage.values()), dispatched,
        )

    # ------------------------------------------------------------------
    # G-03 · 主动运营派发辅助
    # ------------------------------------------------------------------

    def _remaining_quota(self, platform, action_type: str, limit: int) -> int:
        """剩余配额 = 上限 - 今日已成功/待核验（R17 灰色地带也计入）。"""
        return max(0, limit - self._today_action_count(platform, action_type))

    def _today_action_count(self, platform, action_type: str) -> int:
        today_start = business_now(self.cfg).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) FROM ops_logs WHERE platform = ? AND action_type = ? "
            "AND executed_at >= ? AND status IN ('success','pending_verify')",
            (platform.value, action_type, today_start),
        ).fetchone()
        return int(row[0]) if row else 0

    def _emit_quota_exhausted(
        self, platform, action_type: str, limit: int,
    ) -> None:
        label = {"active_comment": "主动评论", "follow": "关注"}.get(action_type, action_type)
        used = self._today_action_count(platform, action_type)
        logger.info("📉 [APS] daily_ops %s %s 今日配额已用完 %d/%d → 跳过派发",
                    platform.value, label, used, limit)
        if self.notifier:
            self.notifier.emit(
                "quota_exhausted",
                f"{platform.display_name} 今日{label}配额已用完（{used}/{limit}），跳过派发",
                platform=platform.value,
            )

    async def _dispatch_action(
        self, platform, action_type: str, target: str, content: Optional[str] = None,
    ) -> Optional[int]:
        """派发一条主动运营动作：task_runs running → 终态。"""
        # R2 / §12.1：主动评论内容必须先过终极目标检查；检查器缺失 = 不放行
        if action_type == "active_comment":
            if self.guard is None:
                return self._record_task_run(
                    platform, target, "skipped", "终极目标检查器未注入，拒绝发送评论",
                )
            outcome = self.guard.check_comment(
                content or "", platform=getattr(platform, "value", platform),
            )
            if not outcome.passes:
                return self._record_task_run(
                    platform, target, "skipped",
                    f"评论未通过终极目标检查：{outcome.reason}"[:500],
                )

        task_run_id = self._record_task_run(platform, target, "running", None)
        if task_run_id is None:
            return None

        try:
            if action_type == "active_comment":
                await self.ops.active_comment(platform, target, content or "")
            else:
                await self.ops.follow(platform, target)
        except Exception as exc:  # noqa: BLE001 — 单条失败不能中断整轮 daily_ops
            logger.warning("❌ [APS] daily_ops %s %s 异常: %s",
                           platform.value, action_type, exc)
            self._finish_task_run(task_run_id, "failed", str(exc)[:500])
            return task_run_id

        status, error_msg = self._last_op_outcome(platform, action_type, target)
        self._finish_task_run(task_run_id, status, error_msg)
        return task_run_id

    def _record_task_run(
        self, platform, target: str, status: str, error_msg: Optional[str],
    ) -> Optional[int]:
        """写 task_runs（biz_id=目标，R16 断点恢复留痕）。"""
        if self.conn is None:
            return None
        now = business_now(self.cfg).isoformat()
        cur = self.conn.execute(
            "INSERT INTO task_runs (task_type, biz_id, platform, status, attempt, "
            "started_at, finished_at, error_msg, created_at) "
            "VALUES ('daily_ops', ?, ?, ?, 1, ?, ?, ?, ?)",
            (
                target, platform.value, status,
                now if status == "running" else None,
                now if status != "running" else None,
                error_msg, now,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def _finish_task_run(self, task_run_id: int, status: str, error_msg: Optional[str]) -> None:
        self.conn.execute(
            "UPDATE task_runs SET status = ?, finished_at = ?, error_msg = ? WHERE id = ?",
            (status, business_now(self.cfg).isoformat(), error_msg, task_run_id),
        )
        self.conn.commit()

    def _last_op_outcome(self, platform, action_type: str, target: str) -> tuple[str, Optional[str]]:
        """从 ops_logs 读回本次动作终态 → 映射 task_runs.status（R17 保留 pending_verify）。"""
        column = "target_url" if action_type == "active_comment" else "target_author"
        row = self.conn.execute(
            f"SELECT status, error_msg FROM ops_logs WHERE platform = ? AND action_type = ? "
            f"AND {column} = ? ORDER BY id DESC LIMIT 1",
            (platform.value, action_type, target),
        ).fetchone()
        if not row:
            return "failed", "未在 ops_logs 找到执行记录（适配器未落日志）"
        mapping = {
            "success": "success",
            "pending_verify": "pending_verify",
            "failed": "failed",
            "skipped": "skipped",
            "circuit_broken": "skipped",
        }
        return mapping.get(row["status"], "failed"), row["error_msg"]

    async def _job_comment_polling(self) -> None:
        """被动运营：每 30min 拉已 user_published / published 的草稿，尝试回复新评论。"""
        from datetime import datetime
        from ...adapters.base import Platform
        from ...core.login_manager import has_login_state

        if self.conn is None or self.ops is None:
            return

        from ...core.ops.pause import is_ops_paused
        if is_ops_paused(self.conn):
            logger.info("[APS] comment_polling 跳过：运营已全局暂停")
            return

        # platform_note_url：发布确认时用户补填的笔记链接。没有它就无法定位评论区
        # （内部 draft id 拼 https://.../explore/draft-xxx 只会 404），直接跳过。
        drafts = self.conn.execute(
            "SELECT d.id, d.platform, d.user_confirmed_at, d.saved_at, "
            "       d.platform_note_url, c.title "
            "FROM drafts d JOIN contents c ON d.content_id = c.id "
            "WHERE d.status IN ('user_published','published') "
            "  AND (d.user_confirmed_at IS NOT NULL OR d.saved_at IS NOT NULL) "
            "ORDER BY COALESCE(d.user_confirmed_at, d.saved_at) DESC LIMIT 20"
        ).fetchall()
        if not drafts:
            return

        total_replied = 0
        for row in drafts:
            try:
                platform = Platform(row["platform"])
            except ValueError:
                continue
            if not has_login_state(platform) or (self.cb and self.cb.is_circuit_broken(platform)):
                continue
            adapter = self.adapters.get(platform)
            if adapter is None:
                continue
            note_url = row["platform_note_url"]
            if not note_url:
                logger.info(
                    "[APS] comment_polling 跳过 draft=%s：未填平台笔记链接（发布确认时可补填）",
                    row["id"],
                )
                continue

            async with self.platform_locks[platform]:
                try:
                    comments = await adapter.fetch_new_comments(note_url)
                except Exception as exc:
                    logger.warning("[APS] comment_polling %s fetch_new_comments 异常: %s",
                                   platform.value, exc)
                    continue

                for comment in comments or []:
                    comment_id = comment.get("comment_id") or comment.get("id")
                    content = comment.get("content") or comment.get("text") or ""
                    if not comment_id:
                        continue
                    # 幂等检查（R16：笔记 + comment_id 维度；失败记录不算已回复）
                    exists = self.conn.execute(
                        "SELECT 1 FROM ops_logs WHERE platform = ? AND action_type = 'passive_reply' "
                        "AND target_url = ? AND target_author = ? "
                        "AND status IN ('success','pending_verify')",
                        (platform.value, note_url, comment_id),
                    ).fetchone()
                    if exists:
                        continue
                    # 调用运营引擎（note_url 同时作为 R16 幂等键的「篇」维度）
                    reply_text = self._reply_template(row["title"] or content[:10])
                    ok = await self.ops.passive_reply(
                        platform, note_url, comment_id, reply_text,
                    )
                    if ok:
                        total_replied += 1

        if total_replied:
            logger.info("💬 [APS] comment_polling 本轮回复 %d 条", total_replied)
        else:
            logger.info("[APS] comment_polling 本轮无新评论")

    def _reply_template(self, hook: str) -> str:
        """被动回复模板（底层 adapter 是 stub 时也至少有可读回复语料）。"""
        return (
            f"感谢你的留言～关于{hook}，"
            f"如果你想更深入了解，可以在评论区继续互动，我会第一时间回复！"
        )

    async def _job_hot_topics_refresh(self) -> None:
        """后台采集热点并做标签匹配；独立连接、在线程里执行，不阻塞事件循环。"""
        from ..hot_topic_service import run_once

        await asyncio.to_thread(run_once, self.cfg, None, trigger="scheduler")

    async def _job_kb_scan(self) -> None:
        """每天 6:00 扫描选题库增量 — 对比 topic_index 插入新选题。"""
        if self.conn is None:
            return

        # 1. 统计当前 topic_index 数量
        before = self.conn.execute("SELECT COUNT(*) FROM topic_index").fetchone()[0]

        # 2. 读取选题库：优先人工注入的 reader（测试 / 定制），否则读全部来源
        if self.kb_reader is not None:
            try:
                items, _ = self.kb_reader.read()
            except Exception as exc:
                logger.warning("[APS] kb_scan 读题库异常: %s", exc)
                return
        else:
            try:
                from ..kb_sources import read_all
                items, _ = read_all(self.cfg)
            except Exception as exc:
                logger.warning("[APS] kb_scan 读题库异常: %s", exc)
                return

        # 3. 插入不存在的新选题
        inserted = 0
        for it in items:
            exists = self.conn.execute(
                "SELECT 1 FROM topic_index WHERE id = ?", (it.id,),
            ).fetchone()
            if exists:
                continue
            # 列集合严格对齐设计方案 §topic_index（无 tags / summary 列）
            self.conn.execute(
                "INSERT INTO topic_index (id, title, dimension, source, date, first_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    it.id, it.title, it.dimension,
                    getattr(it, "source", "kb"),
                    getattr(it, "date", None),
                    business_now(self.cfg).strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            inserted += 1
        self.conn.commit()
        after = self.conn.execute("SELECT COUNT(*) FROM topic_index").fetchone()[0]
        logger.info(
            "📚 [APS] kb_scan — 选题库 %d → %d，新增 %d 条", before, after, inserted,
        )

    async def _job_kb_sync(self) -> None:
        """每天 7:00 同步远程选题库（CSV / JSON / RSS），再补一次增量扫描。"""
        from ..kb_sources import sync_all_remote

        results = await asyncio.to_thread(sync_all_remote, self.cfg)
        failed = [k for k, v in results.items() if v != "ok"]
        logger.info("📥 [APS] kb_sync — 远程来源 %d 个，失败 %d 个", len(results), len(failed))
        await self._job_kb_scan()

    async def _job_data_retention_cleanup(self) -> None:
        """R23 数据保留清理（每天 6:00）。

        期限（config.data_retention）：
        - 截图           → screenshot_days（默认 30）
        - 平台原始响应   → raw_response_days（默认 90）
        - ops_logs       → ops_logs_days（默认 365）
        - ai_calls 明细  → ai_calls_days（默认 180）
        - 热点           → hot_topics.retention_days / max_rows（热点采集关闭时也照常清理）
        """
        if self.conn is not None:
            try:
                from ..hot_topic_dedup import prune
                prune(self.conn, retention_days=self.cfg.hot_topics.retention_days,
                      max_rows=self.cfg.hot_topics.max_rows)
            except Exception:  # noqa: BLE001 — 热点清理失败不影响其它保留清理
                logger.exception("[APS] 热点保留清理失败")
        import shutil
        from pathlib import Path as P
        from datetime import datetime, timedelta
        import json

        if self.conn is None:
            return
        cfg = self.cfg.data_retention

        logger.info("🧹 [APS] data_retention_cleanup 启动")

        # 1. 截图清理（已有）
        screenshots_dir = P(self.cfg.resolve_path(self.cfg.paths.screenshot_dir))
        deleted_files = 0
        if screenshots_dir.exists():
            now = business_timestamp(self.cfg)
            cutoff = cfg.screenshot_days * 86_400
            for f in screenshots_dir.iterdir():
                if f.is_file() and (now - f.stat().st_mtime) > cutoff:
                    f.unlink()
                    deleted_files += 1

        # 2. metrics.raw_response（默认 90 天）
        cutoff_raw = (business_now(self.cfg) - timedelta(days=cfg.raw_response_days)).isoformat(timespec="seconds")
        cur = self.conn.execute(
            "UPDATE metrics SET raw_response = NULL WHERE fetched_at < ? AND raw_response IS NOT NULL",
            (cutoff_raw,),
        )
        cleaned_raw = cur.rowcount

        # 3. ops_logs（默认 365 天）
        cutoff_ops = (business_now(self.cfg) - timedelta(days=cfg.ops_logs_days)).isoformat(timespec="seconds")
        cur = self.conn.execute(
            "DELETE FROM ops_logs WHERE executed_at < ?", (cutoff_ops,)
        )
        cleaned_ops = cur.rowcount

        # 4. ai_calls（默认 180 天）
        cutoff_ai = (business_now(self.cfg) - timedelta(days=cfg.ai_calls_detail_days)).isoformat(timespec="seconds")
        cur = self.conn.execute(
            "DELETE FROM ai_calls WHERE called_at < ?", (cutoff_ai,)
        )
        cleaned_ai = cur.rowcount

        self.conn.commit()
        logger.info(
            "🧹 [APS] data_retention_cleanup 完成 — 截图 %d / raw_response %d / ops_logs %d / ai_calls %d",
            deleted_files, cleaned_raw, cleaned_ops, cleaned_ai,
        )

    # ------------------------------------------------------------------
    # R8 · 轮次阶段超时扫描（每小时）
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_dt(value) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    def _phase_started_at(self, row, phase: str) -> Optional[datetime]:
        """取某阶段起点：优先专用列，回退 transition_log 中最近一次进入该阶段的时间。"""
        column = {
            "producing": "producing_started_at",
            "collecting_metrics": "collecting_metrics_started_at",
        }.get(phase)
        if column:
            dt = self._parse_dt(row[column])
            if dt:
                return dt

        try:
            log = json.loads(row["transition_log"]) if row["transition_log"] else []
        except (json.JSONDecodeError, TypeError):
            log = []
        for entry in reversed(log or []):
            if isinstance(entry, dict) and entry.get("to") == phase:
                dt = self._parse_dt(entry.get("at"))
                if dt:
                    return dt
        return None

    async def _job_round_timeout_sweeper(self) -> None:
        """R8：三个阶段各自独立计时，超时后迁移状态并通知。

        - producing           → round_timeout_hours（默认 48h）→ failed
        - awaiting_publish    → awaiting_publish_timeout_hours（默认 120h）→ failed
        - collecting_metrics  → collecting_metrics_timeout_hours（默认 72h）→ analyzing
          （数据未齐的篇目标记 failed，便于 R10 走 partial_completed 路径）
        """
        if self.conn is None or self.sm_round is None:
            logger.warning("[APS] round_timeout_sweeper 跳过：依赖未注入")
            return

        t = self.cfg.timeouts
        now = business_now(self.cfg)

        # R8/R9：单篇发布确认超时独立计时（timeout + grace），与轮次状态无关
        if self.sm_draft is not None:
            for draft_id in self.sm_draft.check_expired_drafts(
                now=now,
                timeout_hours=t.publish_confirm_timeout_hours,
                grace_hours=t.publish_confirm_grace_hours,
            ):
                logger.warning("⏰ [APS] 草稿 %s 发布确认超时 → expired", draft_id)

        rows = self.conn.execute(
            "SELECT id, status, producing_started_at, collecting_metrics_started_at, "
            "transition_log FROM spiral_rounds "
            "WHERE status IN ('producing','awaiting_publish','collecting_metrics')"
        ).fetchall()
        if not rows:
            return

        limits = {
            "producing": (t.round_timeout_hours, "failed"),
            # 120h 内用户始终未确认发布 → 轮次视为放弃（状态机允许的唯一非收集出口是 cancelled）
            "awaiting_publish": (t.awaiting_publish_timeout_hours, "cancelled"),
            "collecting_metrics": (t.collecting_metrics_timeout_hours, "analyzing"),
        }
        handled = 0

        for row in rows:
            status = row["status"]
            limit_hours, target = limits.get(status, (None, None))
            if limit_hours is None:
                continue
            start = self._phase_started_at(row, status)
            if start is None:
                logger.warning(
                    "[APS] round_timeout_sweeper 轮次 %s 在 %s 缺阶段起点，跳过",
                    row["id"], status,
                )
                continue
            elapsed = now - start
            if elapsed <= timedelta(hours=limit_hours):
                continue

            reason = f"{status} 超时 {limit_hours}h（已 {elapsed.total_seconds() / 3600:.1f}h）"
            if status == "awaiting_publish":
                # 整轮计时从首次进入 awaiting_publish 开始，但草稿可分批保存/恢复。
                # 只要还存在未到单篇 timeout+grace 的 draft_saved，就不能先取消整轮。
                waiting = self.conn.execute(
                    "SELECT COUNT(*) FROM drafts d JOIN contents c ON d.content_id = c.id "
                    "WHERE c.round_id = ? AND d.status = 'draft_saved'",
                    (row["id"],),
                ).fetchone()[0]
                if waiting:
                    logger.info(
                        "[APS] 轮次 %s awaiting_publish 阶段超时暂缓：%d 篇平台草稿尚未到单篇过期时间",
                        row["id"], waiting,
                    )
                    continue
                self.conn.execute(
                    "UPDATE spiral_rounds SET awaiting_publish_ended_at = ? WHERE id = ?",
                    (now.isoformat(timespec="seconds"), row["id"]),
                )
                self.conn.commit()

            if status == "collecting_metrics":
                # 未发布的 draft_saved 仍可由用户在平台发布；在其单篇确认超时前
                # 不得先结束轮次，否则晚发布的内容会成为无法进入复盘的孤儿记录。
                waiting = self.conn.execute(
                    "SELECT COUNT(*) FROM drafts d JOIN contents c ON d.content_id = c.id "
                    "WHERE c.round_id = ? AND d.status = 'draft_saved'",
                    (row["id"],),
                ).fetchone()[0]
                if waiting:
                    logger.info(
                        "[APS] 轮次 %s collecting_metrics 阶段超时暂缓：%d 篇平台草稿仍待用户确认",
                        row["id"], waiting,
                    )
                    continue
                self._fail_uncollected_drafts(row["id"], reason)

            if self.sm_round.transition(row["id"], target, reason=f"R8 阶段超时：{reason}"):
                handled += 1
                logger.warning("⏰ [APS] 轮次 %s %s → %s", row["id"], reason, target)
                if self.notifier:
                    self.notifier.emit(
                        "round_timeout",
                        f"轮次 {row['id']} {reason}，已迁移为 {target}",
                    )

        if handled:
            logger.info("⏰ [APS] round_timeout_sweeper 本轮迁移 %d 个轮次", handled)

    def _fail_uncollected_drafts(self, round_id: int, reason: str) -> None:
        """collecting_metrics 超时：仍无成功采集的篇目 → failed（R10 走 partial_completed）。"""
        if self.sm_draft is None:
            return
        rows = self.conn.execute(
            "SELECT d.id FROM drafts d JOIN contents c ON d.content_id = c.id "
            "WHERE c.round_id = ? AND d.status IN ('user_published','data_fetching') "
            "AND NOT EXISTS (SELECT 1 FROM metrics m WHERE m.draft_id = d.id "
            "                AND m.fetch_status IN ('success','partial'))",
            (round_id,),
        ).fetchall()
        for row in rows:
            self.sm_draft.transition(
                row["id"], "failed", reason=f"数据采集超时：{reason}",
                extra_updates={"error_msg": reason},
            )

    async def _job_round_review(self) -> None:
        """复盘收口（R10）：collecting_metrics → analyzing → 终态，并跑螺旋复盘发通知。

        R9：只处理"已发布篇目"，不冻结、不改动仍在 draft_saved 等用户确认的草稿。
        """
        if self.conn is None or self.sm_round is None:
            logger.warning("[APS] round_review 跳过：依赖未注入")
            return

        # 1) collecting_metrics：所有 user_published / data_fetching 都已落定 → analyzing
        rows = self.conn.execute(
            "SELECT id FROM spiral_rounds WHERE status = 'collecting_metrics'"
        ).fetchall()
        for row in rows:
            round_id = row["id"]
            # 只要还有 draft_saved，用户仍可发布它；必须等待单篇发布确认超时任务
            # 将其转为 expired 后再收口，不能靠一段较短的轮次宽限期提前终态化。
            waiting = self.conn.execute(
                "SELECT COUNT(*) FROM drafts d JOIN contents c ON d.content_id = c.id "
                "WHERE c.round_id = ? AND d.status = 'draft_saved'",
                (round_id,),
            ).fetchone()[0]
            if waiting:
                continue
            unresolved = self.conn.execute(
                "SELECT COUNT(*) FROM drafts d JOIN contents c ON d.content_id = c.id "
                "WHERE c.round_id = ? AND d.status IN ('user_published','data_fetching')",
                (round_id,),
            ).fetchone()[0]
            if unresolved:
                continue
            published = self.conn.execute(
                "SELECT COUNT(*) FROM drafts d JOIN contents c ON d.content_id = c.id "
                "WHERE c.round_id = ? AND d.status = 'published'",
                (round_id,),
            ).fetchone()[0]
            if published == 0:
                # 没有成功发布的篇目：交给 round_timeout_sweeper 判 R8 超时，不在这里提前 failed
                continue
            self.sm_round.transition(
                round_id, "analyzing", reason="已发布篇目数据采集完成（R10 进入复盘）",
            )
            logger.info("📊 [APS] round_review 轮次 %s → analyzing", round_id)

        # 2) analyzing → 三条退出路径之一（R10），并执行螺旋复盘
        rows = self.conn.execute(
            "SELECT id, round_number FROM spiral_rounds WHERE status = 'analyzing'"
        ).fetchall()
        for row in rows:
            round_id, round_number = row["id"], row["round_number"]
            target = self.sm_round.decide_analyzing_exit(round_id)

            gaps: list = []
            suggestions = 0
            if self.spiral is not None:
                try:
                    analysis = self.spiral.analyze_round(round_id)
                    gaps = getattr(analysis, "gaps", []) or []
                    suggestions = len(getattr(analysis, "strategy_adjustments", []) or [])
                except Exception:  # noqa: BLE001 — 复盘失败不能卡住状态流转，但必须留日志
                    logger.exception("[APS] 轮次 %s 螺旋复盘失败", round_id)

            self.sm_round.transition(
                round_id, target, reason=f"R10 复盘结论：{target}",
            )
            logger.info("🧭 [APS] round_review 轮次 %s → %s（缺口 %d）",
                        round_id, target, len(gaps))

            if getattr(self, "notifier", None):
                self.notifier.emit(
                    "round_completed",
                    f"第 {round_number} 轮复盘完成 → {target}；"
                    f"缺口建议 {len(gaps)} 条、策略调整 {suggestions} 条",
                )

    async def _job_autogrow_next_round(self) -> None:
        """自动闭环：满足条件时开下一轮并生成草稿（发布仍需用户确认）。"""
        if self.autogrow is None:
            logger.warning("[APS] autogrow_next_round 跳过：依赖未注入")
            return
        await self.autogrow.tick()

    async def _job_cb_recovery(self) -> None:
        """熔断恢复 3 步验证（冷却到期 + test_login + test_lightweight_action，R6）。"""
        from ...adapters.base import Platform
        from ...core.login_manager import has_login_state

        if self.conn is None or self.cb is None:
            logger.warning("[APS] cb_recovery 跳过：依赖未注入")
            return

        rows = self.conn.execute(
            "SELECT DISTINCT platform FROM circuit_breaker_events WHERE resolved_at IS NULL"
        ).fetchall()
        if not rows:
            return

        for row in rows:
            try:
                platform = Platform(row["platform"])
            except ValueError:
                continue
            if not has_login_state(platform):
                logger.info("[APS] cb_recovery %s 跳过：无登录态", platform.value)
                continue

            async with self.platform_locks[platform]:
                result = await self.cb.try_recover(platform)

            if result.get("passed"):
                logger.info("✅ [APS] %s 熔断恢复成功（3 步验证通过）", platform.value)
                if getattr(self, "notifier", None):
                    self.notifier.emit(
                        "circuit_recovered",
                        f"{platform.display_name} 熔断已恢复，运营继续",
                        platform=platform.value,
                    )
            else:
                logger.info("[APS] %s 恢复未通过：%s", platform.value, result.get("reason"))


# =====================================================================
# 模块级可 pickle 的 job 包装（APScheduler SQLAlchemyJobStore 要求）
# =====================================================================

def _engine_for(engine_key: str | None) -> "SchedulerEngine | None":
    """只按任务持久化的账号组引擎键分派；缺键任务安全跳过，不回退到其他账号组。"""
    if not engine_key:
        logger.error("[APS] 任务缺少 engine_key，拒绝执行以避免跨账号组操作")
        return None
    engine = _ENGINES.get(engine_key)
    if engine is None:
        logger.warning("[APS] 任务对应的引擎未运行，跳过 job engine_key=%s", engine_key)
    return engine


async def _invoke(engine, method_name: str, *args) -> None:
    """兼容旧测试替身，同时对真实引擎的 job 执行做生命周期跟踪。"""
    tracked = getattr(engine, "_run_tracked", None)
    if tracked is not None:
        await tracked(method_name, *args)
    else:
        await getattr(engine, method_name)(*args)


async def _run_publish_polling(engine_key: str | None = None) -> None:
    engine = _engine_for(engine_key)
    if engine is not None:
        await _invoke(engine, "_job_publish_polling")


async def _run_data_fetch(engine_key: str | None = None, draft_id: str = "", attempt: int = 1) -> None:
    engine = _engine_for(engine_key)
    if engine is not None and draft_id:
        await _invoke(engine, "_job_data_fetch", draft_id, attempt)


async def _run_hot_topics_refresh(engine_key: str | None = None) -> None:
    engine = _engine_for(engine_key)
    if engine is not None:
        await _invoke(engine, "_job_hot_topics_refresh")


async def _run_daily_ops(engine_key: str | None = None) -> None:
    engine = _engine_for(engine_key)
    if engine is not None:
        await _invoke(engine, "_job_daily_ops")


async def _run_comment_polling(engine_key: str | None = None) -> None:
    engine = _engine_for(engine_key)
    if engine is not None:
        await _invoke(engine, "_job_comment_polling")


async def _run_cb_recovery(engine_key: str | None = None) -> None:
    engine = _engine_for(engine_key)
    if engine is not None:
        await _invoke(engine, "_job_cb_recovery")


async def _run_kb_scan(engine_key: str | None = None) -> None:
    engine = _engine_for(engine_key)
    if engine is not None:
        await _invoke(engine, "_job_kb_scan")


async def _run_kb_sync(engine_key: str | None = None) -> None:
    engine = _engine_for(engine_key)
    if engine is not None:
        await _invoke(engine, "_job_kb_sync")


async def _run_data_retention_cleanup(engine_key: str | None = None) -> None:
    engine = _engine_for(engine_key)
    if engine is not None:
        await _invoke(engine, "_job_data_retention_cleanup")


async def _run_round_timeout_sweeper(engine_key: str | None = None) -> None:
    engine = _engine_for(engine_key)
    if engine is not None:
        await _invoke(engine, "_job_round_timeout_sweeper")


async def _run_round_review(engine_key: str | None = None) -> None:
    engine = _engine_for(engine_key)
    if engine is not None:
        await _invoke(engine, "_job_round_review")


async def _run_autogrow_next_round(engine_key: str | None = None) -> None:
    engine = _engine_for(engine_key)
    if engine is not None:
        await _invoke(engine, "_job_autogrow_next_round")


# 任务 id → 可 pickle 的模块级执行函数（SQLAlchemyJobStore 要求）
JOB_RUNNERS = {
    "publish_polling": _run_publish_polling,
    "daily_ops": _run_daily_ops,
    "comment_polling": _run_comment_polling,
    "cb_recovery": _run_cb_recovery,
    "kb_scan": _run_kb_scan,
    "kb_sync": _run_kb_sync,
    "data_retention_cleanup": _run_data_retention_cleanup,
    "round_timeout_sweeper": _run_round_timeout_sweeper,
    "round_review": _run_round_review,
    "hot_topics_refresh": _run_hot_topics_refresh,
    "autogrow_next_round": _run_autogrow_next_round,
}
