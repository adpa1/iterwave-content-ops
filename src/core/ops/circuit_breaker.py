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
        """触发某平台熔断 — 幂等插入（同平台同 reason 未 resolved 时跳过）。

        去重逻辑（N1 修复）：
        - 检查是否已有 resolved_at IS NULL 且同 platform + reason 的事件
        - 若有 → 只刷新 cooldown_until（延长冷却），不新增事件
        - 若无 → 新插入事件
        这避免了多点触发同一信号时产生 N 条堆积事件，恢复 job 一次
        就能解除整条平台的熔断态。
        """
        now = datetime.now()
        cooldown_until = now + timedelta(minutes=self.cooldown_minutes)

        # 幂等检查
        existing = self.db.execute(
            "SELECT id FROM circuit_breaker_events "
            "WHERE platform = ? AND reason = ? AND resolved_at IS NULL LIMIT 1",
            (platform.value, reason.value),
        ).fetchone()

        if existing:
            # 已有同 reason 未 resolved 事件 → 刷新 cooldown（延长）即可
            self.db.execute(
                "UPDATE circuit_breaker_events SET cooldown_until = ? WHERE id = ?",
                (cooldown_until.isoformat(), existing[0]),
            )
            self.db.commit()
            logger.info(
                "🔁 %s 熔断信号重复（reason=%s），已延长冷却至 %s",
                platform.value, reason.value, cooldown_until.isoformat(),
            )
            return

        # 新事件
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
        """R6 熔断门控：存在 resolved_at IS NULL 的事件 → 视为熔断中。

        重要语义：冷却到期 ≠ 自动放行。闸门由「3 步恢复验证是否通过」
        决定（即 resolved_at 是否已被写入）。cooldown_until 仅作为
        try_recover 是否允许尝试 3 步的前置条件，对闸门不起决定作用。

        在调度暂停或 5min 扫描空窗期，即便冷却已到期但 try_recover
        从未执行 → 闸门仍关闭 → 不会撞风控。
        """
        row = self.db.execute(
            "SELECT 1 FROM circuit_breaker_events "
            "WHERE platform = ? AND resolved_at IS NULL LIMIT 1",
            (platform.value,),
        ).fetchone()
        return row is not None

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

        N1 修复（批量语义）：
        - 三步通过后，一次性 UPDATE 该平台 **全部** resolved_at IS NULL 的事件
          （不再只解最早到期的那一条）
        - 三步失败（登录失效 / 轻量动作失败）→ 延长该平台全部未 resolved 事件冷却
        返回 {passed: bool, step_results: {step: ok, reason: ...}}
        """
        results: dict = {"passed": False, "steps": {}, "reason": ""}

        # 0. 找最早到期事件（三步通过前需要检查冷却是否到期）
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
            results["reason"] = "登录态无效，延长全部事件 cooldown 1h"
            self._extend_all_cooldowns(platform, 60)
            return results

        # Step 3: test_lightweight_action
        try:
            light_ok = await adapter.test_lightweight_action()
        except Exception as exc:
            light_ok = False
            logger.warning("test_lightweight_action 异常: %s", exc)
        results["steps"]["test_lightweight_action"] = light_ok

        if not light_ok:
            results["reason"] = "轻量动作失败，延长全部事件 cooldown 1h"
            self._extend_all_cooldowns(platform, 60)
            return results

        # 三步全部通过 → 批量 resolved 该平台所有未 resolved 事件
        results["passed"] = True
        results["reason"] = "三步验证全部通过，恢复运营"
        cur = self.db.execute(
            "UPDATE circuit_breaker_events SET resolved_at = ?, resolved_by = 'auto' "
            "WHERE platform = ? AND resolved_at IS NULL",
            (datetime.now().isoformat(), platform.value),
        )
        self.db.commit()
        count = cur.rowcount
        logger.info(
            "✅ %s 熔断恢复成功（3 步全部通过，批量解除 %d 条事件）",
            platform.value, count,
        )
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

    def _extend_all_cooldowns(self, platform: Platform, minutes: int) -> int:
        """批量延长该平台所有未 resolved 事件冷却 N 分钟（N1 修复）。

        原来只延长单条（最早到期的那一条），现在批量延长避免
        剩余事件冷却已到期、又被闸门误判。
        返回被延长的事件数。
        """
        cur = self.db.execute(
            "UPDATE circuit_breaker_events SET cooldown_until = datetime(cooldown_until, ?) "
            "WHERE platform = ? AND resolved_at IS NULL",
            (f"+{minutes} minutes", platform.value),
        )
        self.db.commit()
        count = cur.rowcount
        logger.info(
            "⏳ 延长 %s 全部未 resolved 事件冷却 %d 分钟（%d 条）",
            platform.value, minutes, count,
        )
        return count
