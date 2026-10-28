"""
节奏控制器 — 平台独立配额 + 时段窗口 + 随机间隔（§6）。

设计要点：
- 每平台每日配额硬上限（超限跳过）
- 时段窗口（ops 8-23h, draft_save 9-22h，本地时区 R5）
- 随机间隔（基准 ± jitter 范围内随机，避免被反检测）
"""

from __future__ import annotations

import logging
import random
from datetime import datetime
from typing import Optional

from ...adapters.base import Platform

logger = logging.getLogger(__name__)


class RhythmController:
    """节奏控制器。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.intervals = cfg.rhythm.intervals

    # ------------------------------------------------------------------
    # 是否在允许时段
    # ------------------------------------------------------------------

    def is_within_window(self, platform: Platform, action: str) -> bool:
        """检查当前时间是否在时段窗口内。

        action: "ops" 或 "draft_save"
        """
        now = datetime.now()
        window = self.cfg.rhythm.windows.get(action)
        if not window:
            return True  # 没配置窗口则不限制
        if window.start_hour <= now.hour < window.end_hour:
            return True
        logger.debug(
            "%s %s 不在时段窗口内（当前 %d:00, 窗口 %d-%d）",
            platform.value, action, now.hour, window.start_hour, window.end_hour,
        )
        return False

    # ------------------------------------------------------------------
    # 今日配额剩余
    # ------------------------------------------------------------------

    def daily_quota_remaining(
        self, platform: Platform, action_type: str, today_used: int,
    ) -> int:
        """返回今日该平台该动作的剩余配额。"""
        quotas = self.cfg.rhythm.daily_quotas.get(platform.value)
        if not quotas:
            return 0
        mapping = {
            "passive_reply": quotas.passive_replies,
            "active_comment": quotas.active_comments,
            "follow": quotas.follows,
        }
        limit = mapping.get(action_type, 0)
        return max(0, limit - today_used)

    # ------------------------------------------------------------------
    # 随机间隔（返回 sleep 秒数）
    # ------------------------------------------------------------------

    def wait_seconds(self, platform: Platform, action_type: str) -> int:
        """根据动作类型返回应该等待的秒数（± jitter）。"""
        iv = self.intervals
        if action_type == "passive_reply":
            base, jitter = iv.passive_reply_base_seconds, iv.passive_reply_jitter_seconds
        elif action_type == "active_comment":
            base, jitter = iv.active_comment_base_seconds, iv.active_comment_jitter_seconds
        elif action_type == "follow":
            base, jitter = iv.follow_base_seconds, iv.follow_jitter_seconds
        else:
            return 0
        return max(1, random.randint(base - jitter, base + jitter))
