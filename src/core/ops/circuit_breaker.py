"""
熔断器 — 5 信号检测 + 3 步恢复验证（R6/R6.5）。

5 种触发信号（config.yaml circuit_breaker.signals）：
- captcha       出现验证码
- rate_limit    限流
- login_expired 登录态过期
- action_blocked 操作被拦截
- device_verify 设备验证

3 步恢复验证（冷却到期 ≠ 自动恢复！R6 红线）：
1. 冷却期是否已到期
2. adapter.test_login() → 返回 True
3. adapter.test_lightweight_action() → 读取创作者中心首页成功
任何一步没过 → 延长 cooldown 1h，再次检查。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from ...adapters.base import Platform, PlatformAdapter, CircuitBreakerReason

logger = logging.getLogger(__name__)


class CircuitBreakerEngine:
    """熔断器 — 每平台独立状态。"""

    def __init__(self, cfg, db_conn, adapters: dict[Platform, PlatformAdapter]):
        self.cfg = cfg
        self.db = db_conn
        self.adapters = adapters
        self.cooldown_minutes = cfg.circuit_breaker.cooldown_minutes

    # ------------------------------------------------------------------
    # 触发熔断
    # ------------------------------------------------------------------

    def trigger(self, platform: Platform, reason: CircuitBreakerReason) -> None:
        """触发某平台熔断 — 记录 circuit_breaker_events 表。"""
        now = datetime.now()
        cooldown_until = now + timedelta(minutes=self.cooldown_minutes)
        self.db.execute(
            "INSERT INTO circuit_breaker_events (platform, reason, detected_at, cooldown_until) "
            "VALUES (?, ?, ?, ?)",
            (platform.value, reason.value, now.isoformat(), cooldown_until.isoformat()),
        )
        self.db.commit()
        logger.warning(
            "🔴 %s 熔断触发 reason=%s cooldown_until=%s",
            platform.value, reason.value, cooldown_until.isoformat(),
        )
        # 熔断必须通知用户（critical）
        from ..notify import get_active as _get_notifier
        notifier = _get_notifier()
        if notifier is not None:
            notifier.emit(
                "circuit_broken",
                f"{platform.display_name} 触发熔断：{reason.value}，"
                f"冷却至 {cooldown_until.strftime('%H:%M')}",
                platform=platform.value,
            )

    # ------------------------------------------------------------------
    # 查询熔断状态
    # ------------------------------------------------------------------

    def is_circuit_broken(self, platform: Platform) -> bool:
        """检查某平台当前是否熔断（有未 resolved 且 cooldown_until > now 的事件）。

        注意：cooldown_until 存的是本地时间（R5），不能用 SQLite 的
        datetime('now')（UTC）比较，必须在 Python 侧用 datetime.now()。
        """
        rows = self.db.execute(
            "SELECT cooldown_until FROM circuit_breaker_events "
            "WHERE platform = ? AND resolved_at IS NULL",
            (platform.value,),
        ).fetchall()
        now = datetime.now()
        for row in rows:
            raw = row["cooldown_until"]
            if not raw:
                continue
            try:
                until = datetime.fromisoformat(str(raw))
            except ValueError:
                continue
            if until > now:
                return True
        return False

    def active_events(self, platform: Optional[Platform] = None) -> list[dict]:
        """返回未 resolved 的熔断事件列表。"""
        sql = "SELECT * FROM circuit_breaker_events WHERE resolved_at IS NULL"
        params: list = []
        if platform:
            sql += " AND platform = ?"
            params.append(platform.value)
        sql += " ORDER BY detected_at DESC"
        rows = self.db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # R6 3 步恢复验证（冷却到期 ≠ 自动恢复）
    # ------------------------------------------------------------------

    async def try_recover(self, platform: Platform) -> dict:
        """R6 红线 — 冷却到期后跑 3 步验证。

        返回 {passed: bool, step_results: {step: ok, reason: ...}}
        """
        results: dict = {"passed": False, "steps": {}, "reason": ""}

        # 0. 找到该平台最早到期的熔断事件
        row = self.db.execute(
            "SELECT id, cooldown_until FROM circuit_breaker_events "
            "WHERE platform = ? AND resolved_at IS NULL "
            "ORDER BY cooldown_until ASC LIMIT 1",
            (platform.value,),
        ).fetchone()
        if not row:
            results["reason"] = "该平台无未 resolved 的熔断事件"
            return results

        event_id, cooldown_str = row

        # Step 1: 冷却是否到期
        try:
            cooldown_until = datetime.fromisoformat(cooldown_str)
        except (TypeError, ValueError):
            results["steps"]["cooldown_expired"] = False
            results["reason"] = "冷却时间格式错误"
            return results

        if datetime.now() < cooldown_until:
            results["steps"]["cooldown_expired"] = False
            results["reason"] = f"冷却尚未到期（还剩 {(cooldown_until - datetime.now()).total_seconds() / 60:.0f} 分钟）"
            return results
        results["steps"]["cooldown_expired"] = True

        # Step 2: test_login
        adapter = self.adapters.get(platform)
        if not adapter:
            results["steps"]["cooldown_expired"] = True
            results["steps"]["test_login"] = False
            results["reason"] = "适配器未注册"
            return results

        try:
            login_ok = await adapter.test_login()
        except Exception as exc:
            login_ok = False
            logger.warning("test_login 异常: %s", exc)
        results["steps"]["test_login"] = login_ok

        if not login_ok:
            results["reason"] = "登录态无效，延长 cooldown 1h"
            self._extend_cooldown(event_id, 60)
            return results

        # Step 3: test_lightweight_action
        try:
            light_ok = await adapter.test_lightweight_action()
        except Exception as exc:
            light_ok = False
            logger.warning("test_lightweight_action 异常: %s", exc)
        results["steps"]["test_lightweight_action"] = light_ok

        if not light_ok:
            results["reason"] = "轻量动作失败，延长 cooldown 1h"
            self._extend_cooldown(event_id, 60)
            return results

        # 三步全部通过 → resolved
        results["passed"] = True
        results["reason"] = "三步验证全部通过，恢复运营"
        self.db.execute(
            "UPDATE circuit_breaker_events SET resolved_at = ?, resolved_by = 'auto' WHERE id = ?",
            (datetime.now().isoformat(), event_id),
        )
        self.db.commit()
        logger.info("✅ %s 熔断恢复成功（3 步全部通过）", platform.value)
        return results

    def manual_reset(self, platform: Platform) -> int:
        """手动重置（UI 按钮）。返回被 resolved 的事件数。"""
        now = datetime.now().isoformat()
        cur = self.db.execute(
            "UPDATE circuit_breaker_events SET resolved_at = ?, resolved_by = 'user' "
            "WHERE platform = ? AND resolved_at IS NULL",
            (now, platform.value),
        )
        self.db.commit()
        count = cur.rowcount
        logger.info("🔧 手动重置 %s 熔断事件 %d 条", platform.value, count)
        return count

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _extend_cooldown(self, event_id: int, minutes: int) -> None:
        """延长 cooldown N 分钟（R6 任何一步没过）。"""
        self.db.execute(
            "UPDATE circuit_breaker_events SET cooldown_until = datetime(cooldown_until, ?) WHERE id = ?",
            (f"+{minutes} minutes", event_id),
        )
        self.db.commit()
