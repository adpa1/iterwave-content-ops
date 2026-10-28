"""
运营引擎 — 被动评论 / 主动评论 / 关注。

§1 R16 所有外部操作必须幂等：
- reply_comment   → 回复前查 ops_logs 是否已有 draft_id+comment_id
- post_comment    → 崩溃无法可靠判断是否发出 → pending_verify
- follow_user     → 关注前查 ops_logs 是否已关注过 user_id
"""

from __future__ import annotations

import logging
from datetime import datetime, date
from typing import Optional

from ..redact import redact as _redact_exc  # R23 异常日志脱敏

from ...adapters.base import (
    PlatformAdapter, Platform, ContentToPublish, CircuitBreakerReason, AdapterError,
    NotLoggedInError, PlatformUnsupportedError,
)
from .circuit_breaker import CircuitBreakerEngine
from .pause import is_ops_paused

logger = logging.getLogger(__name__)


class OperationsEngine:
    """运营引擎。"""

    def __init__(self, cfg, db_conn, adapters: dict[Platform, PlatformAdapter],
                 circuit_breaker: CircuitBreakerEngine, rhythm):
        self.cfg = cfg
        self.db = db_conn
        self.adapters = adapters
        self.cb = circuit_breaker
        self.rhythm = rhythm

    # ------------------------------------------------------------------
    # 被动运营：自动回复评论
    # ------------------------------------------------------------------

    def _notify_login_expired(self, platform: Platform, exc: Exception) -> None:
        """登录态失效 → 通知用户（G-15 场景 3）+ 触发熔断（R6 login_expired 信号）。"""
        from ..notify import get_active
        from ...adapters.base import CircuitBreakerReason

        if self.cb is not None:
            try:
                self.cb.trigger(platform, CircuitBreakerReason.LOGIN_EXPIRED)
            except Exception:  # noqa: BLE001 — 熔断上报失败不影响通知
                logger.exception("熔断上报失败 platform=%s", platform.value)

        notifier = get_active()
        if notifier is None:
            return
        notifier.emit(
            "login_expired",
            f"{platform.display_name} 登录态失效，需重新扫码：{_redact_exc(str(exc))[:120]}",
            platform=platform.value,
        )

    async def passive_reply(
        self, platform: Platform, draft_id: str, comment_id: str, content: str,
    ) -> bool:
        """被动回复评论。幂等检查 + 熔断检查 + 节奏控制。"""
        adapter = self.adapters.get(platform)
        if not adapter:
            return False

        # 0. 全局软暂停（人工开关，与熔断无关）
        if is_ops_paused(self.db):
            logger.info("⏸️ 运营已全局暂停，跳过被动回复")
            self._log_op(platform, "passive_reply", "skipped", error_msg="运营已暂停")
            return False

        # 1. 熔断检查
        if self.cb.is_circuit_broken(platform):
            logger.info("🔴 %s 已熔断，跳过被动回复", platform.value)
            self._log_op(platform, "passive_reply", "circuit_broken", error_msg="熔断中")
            return False

        # 2. 幂等检查（R16：draft_id + comment_id 维度；失败记录不算已执行）
        # R5：不能用 SQLite date('now')（UTC），必须按本地时区当日 00:00 比较
        exists = self.db.execute(
            "SELECT 1 FROM ops_logs WHERE platform = ? AND action_type = 'passive_reply' "
            "AND target_url = ? AND target_author = ? "
            "AND status IN ('success','pending_verify') AND executed_at >= ?",
            (platform.value, draft_id, comment_id, self._today_start()),
        ).fetchone()
        if exists:
            logger.info("幂等命中：%s 已回复过 draft=%s comment=%s",
                        platform.value, draft_id, comment_id)
            return True

        # 3. 时段窗口 + 每日配额（被动回复同样受节奏控制，避免短时间大量回复触发风控）
        if hasattr(self.rhythm, "is_within_window") and not self.rhythm.is_within_window(platform, "ops"):
            self._log_op(platform, "passive_reply", "skipped",
                         target_url=draft_id, target_author=comment_id, error_msg="不在运营时段窗口内")
            return False
        used = self.db.execute(
            "SELECT COUNT(*) FROM ops_logs WHERE platform = ? AND action_type = 'passive_reply' "
            "AND status IN ('success','pending_verify') AND executed_at >= ?",
            (platform.value, self._today_start()),
        ).fetchone()[0]
        quotas = self.cfg.rhythm.daily_quotas.get(platform.value) if hasattr(self, "cfg") else None
        if quotas is not None and hasattr(self.rhythm, "daily_quota_remaining") \
                and self.rhythm.daily_quota_remaining(platform, "passive_reply", used) <= 0:
            self._log_op(platform, "passive_reply", "skipped",
                         target_url=draft_id, target_author=comment_id, error_msg="今日被动回复配额已用完")
            return False

        # 4. 节奏控制：真正等待随机间隔（之前只写日志不等待）
        wait_seconds = self.rhythm.wait_seconds(platform, "passive_reply")
        if wait_seconds > 0:
            logger.info("⏳ 节奏等待 %ds 后回复", wait_seconds)
            import asyncio
            import os
            if not os.environ.get("PYTEST_CURRENT_TEST"):
                await asyncio.sleep(min(wait_seconds, 600))

        # 4. 执行（G-02：适配器异常必须落 ops_logs failed + 原因，不能静默吞掉）
        try:
            success = await adapter.reply_comment(draft_id, comment_id, content)
        except PlatformUnsupportedError as exc:
            # G-02：平台能力不支持（如视频号网页端无评论区）→ skipped，不算失败
            logger.info("⏭ %s 被动回复跳过：%s", platform.value, _redact_exc(str(exc)))
            self._log_op(
                platform, "passive_reply", "skipped",
                target_url=draft_id, target_author=comment_id,
                error_msg=_redact_exc(str(exc))[:500],
            )
            return False
        except AdapterError as exc:
            logger.warning("❌ %s 被动回复失败：%s", platform.value, _redact_exc(str(exc)))
            if isinstance(exc, NotLoggedInError):
                self._notify_login_expired(platform, exc)
            self._log_op(
                platform, "passive_reply", "failed",
                target_url=draft_id, target_author=comment_id, content=content,
                rhythm_wait_seconds=wait_seconds, error_msg=_redact_exc(str(exc))[:500],
            )
            return False
        self._log_op(
            platform, "passive_reply",
            status="success" if success else "failed",
            target_url=draft_id, target_author=comment_id,
            content=content,
            rhythm_wait_seconds=wait_seconds,
            error_msg=None if success else "适配器返回 False（未确认回复成功）",
        )
        return success

    # ------------------------------------------------------------------
    # 主动运营：评论
    # ------------------------------------------------------------------

    async def active_comment(
        self, platform: Platform, target_url: str, content: str,
    ) -> Optional[str]:
        """主动评论。pending_verify 场景：崩溃无法确认是否发出。"""
        adapter = self.adapters.get(platform)
        if not adapter:
            return None

        if is_ops_paused(self.db):
            logger.info("⏸️ 运营已全局暂停，跳过主动评论")
            self._log_op(platform, "active_comment", "skipped", error_msg="运营已暂停")
            return None

        if self.cb.is_circuit_broken(platform):
            logger.info("🔴 %s 已熔断，跳过主动评论", platform.value)
            self._log_op(platform, "active_comment", "circuit_broken")
            return None

        # 幂等检查（R16：同一目标今日已发过 → 不重复发）
        seen = self.db.execute(
            "SELECT 1 FROM ops_logs WHERE platform = ? AND action_type = 'active_comment' "
            "AND target_url = ? AND status IN ('success','pending_verify') AND executed_at >= ?",
            (platform.value, target_url, self._today_start()),
        ).fetchone()
        if seen:
            logger.info("幂等命中：%s 今日已在该目标下评论过 %s", platform.value, target_url)
            return None

        # 每日配额检查
        daily_count = self._today_action_count(platform, "active_comment")
        quotas = self.cfg.rhythm.daily_quotas.get(platform.value)
        if quotas and daily_count >= quotas.active_comments:
            logger.info("📉 %s 主动评论今日配额已用完 (%d)", platform.value, daily_count)
            self._log_op(platform, "active_comment", "skipped", error_msg="配额用完")
            return None

        wait_seconds = self.rhythm.wait_seconds(platform, "active_comment")

        try:
            comment_id = await adapter.post_comment(target_url, content)
        except PlatformUnsupportedError as exc:
            logger.info("⏭ %s 主动评论跳过：%s", platform.value, _redact_exc(str(exc)))
            self._log_op(
                platform, "active_comment", "skipped",
                target_url=target_url, content=content, error_msg=_redact_exc(str(exc))[:500],
            )
            return None
        except AdapterError as exc:
            logger.warning("❌ %s 主动评论失败：%s", platform.value, _redact_exc(str(exc)))
            if isinstance(exc, NotLoggedInError):
                self._notify_login_expired(platform, exc)
            self._log_op(
                platform, "active_comment", "failed",
                target_url=target_url, content=content,
                rhythm_wait_seconds=wait_seconds, error_msg=_redact_exc(str(exc))[:500],
            )
            return None
        # comment_id=None → 可能已发出但无法确认
        status = "success" if comment_id else "pending_verify"
        self._log_op(
            platform, "active_comment", status,
            target_url=target_url, content=content,
            rhythm_wait_seconds=wait_seconds,
        )
        return comment_id

    # ------------------------------------------------------------------
    # 主动运营：关注
    # ------------------------------------------------------------------

    async def follow(self, platform: Platform, user_id: str) -> bool:
        adapter = self.adapters.get(platform)
        if not adapter:
            return False

        if is_ops_paused(self.db):
            logger.info("⏸️ 运营已全局暂停，跳过关注")
            self._log_op(platform, "follow", "skipped", error_msg="运营已暂停")
            return False

        if self.cb.is_circuit_broken(platform):
            self._log_op(platform, "follow", "circuit_broken")
            return False

        # 幂等：今日是否已关注过（status 只认 success/pending_verify，失败不算已执行）
        already = self.db.execute(
            "SELECT 1 FROM ops_logs WHERE platform = ? AND action_type = 'follow' "
            "AND target_author = ? AND status IN ('success','pending_verify') "
            "AND executed_at >= ?",
            (platform.value, user_id, self._today_start()),
        ).fetchone()
        if already:
            logger.info("幂等命中：%s 今日已关注过 %s", platform.value, user_id)
            return True

        quotas = self.cfg.rhythm.daily_quotas.get(platform.value)
        daily_count = self._today_action_count(platform, "follow")
        if quotas and daily_count >= quotas.follows:
            self._log_op(platform, "follow", "skipped", error_msg="配额用完")
            return False

        try:
            success = await adapter.follow_user(user_id)
        except PlatformUnsupportedError as exc:
            logger.info("⏭ %s 关注跳过：%s", platform.value, _redact_exc(str(exc)))
            self._log_op(
                platform, "follow", "skipped",
                target_author=user_id, error_msg=_redact_exc(str(exc))[:500],
            )
            return False
        except AdapterError as exc:
            logger.warning("❌ %s 关注失败：%s", platform.value, _redact_exc(str(exc)))
            if isinstance(exc, NotLoggedInError):
                self._notify_login_expired(platform, exc)
            self._log_op(
                platform, "follow", "failed",
                target_author=user_id, error_msg=_redact_exc(str(exc))[:500],
            )
            return False
        self._log_op(
            platform, "follow",
            status="success" if success else "pending_verify",
            target_author=user_id,
            error_msg=None if success else "点击后未确认已关注",
        )
        return success

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _log_op(
        self, platform: Platform, action_type: str, status: str,
        target_url: Optional[str] = None, target_author: Optional[str] = None,
        content: Optional[str] = None, error_msg: Optional[str] = None,
        rhythm_wait_seconds: int = 0,
    ) -> None:
        self.db.execute(
            "INSERT INTO ops_logs (platform, action_type, target_url, target_author, "
            "content, schedule_time, executed_at, status, error_msg, rhythm_wait_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (platform.value, action_type, target_url, target_author,
             content, datetime.now().isoformat(), datetime.now().isoformat(),
             status, error_msg, rhythm_wait_seconds),
        )
        self.db.commit()

    @staticmethod
    def _today_start() -> str:
        """本地时区当日 00:00（R5：SQLite 的 date('now') 是 UTC，差 8 小时会算错天）。"""
        return datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).isoformat()

    def _today_action_count(self, platform: Platform, action_type: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM ops_logs WHERE platform = ? AND action_type = ? "
            "AND executed_at >= ? AND status IN ('success','pending_verify')",
            (platform.value, action_type, self._today_start()),
        ).fetchone()
        return row[0] if row else 0
