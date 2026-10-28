"""目标进度维护（R21 口径）—— 采集成功后聚合 metrics 写 current_value。

R21 硬约束：
- 5 个口径字段（target_metric / target_value / metric_scope / baseline / metric_period / data_source）
  必须齐备才落库（校验在 API 层）
- 达成率 = (current - baseline) / target_value，**任一分量为 NULL → 结果 NULL**
- 未采集到数据 → current_value = NULL，绝不当 0（R3）
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# target_metric → (metrics 列名, 聚合方式)
METRIC_COLUMNS: dict[str, tuple[str, str]] = {
    "followers_gained": ("new_followers", "sum"),
    "new_followers": ("new_followers", "sum"),
    "likes": ("likes", "sum"),
    "collects": ("collects", "sum"),
    "comments": ("comments", "sum"),
    "shares": ("shares", "sum"),
    "views": ("views", "sum"),
    "interaction_rate": ("interaction_rate", "avg"),
    "like_rate": ("like_rate", "avg"),
    "collect_rate": ("collect_rate", "avg"),
}


class GoalProgressUpdater:
    """按 target_metric 聚合 metrics → 回写 goals.current_value。"""

    def __init__(self, cfg, conn):
        self.cfg = cfg
        self.conn = conn

    # ------------------------------------------------------------------

    def update_all(self) -> dict[str, Optional[float]]:
        """刷新所有 active 目标的 current_value，返回 {goal_id: 新值或 None}。"""
        rows = self.conn.execute(
            "SELECT * FROM goals WHERE status = 'active'"
        ).fetchall()
        result: dict[str, Optional[float]] = {}
        for row in rows:
            result[row["id"]] = self.update_goal(row)
        return result

    def update_goal(self, row) -> Optional[float]:
        """刷新单个目标。返回新的 current_value（NULL 表示本期无数据）。"""
        column_spec = METRIC_COLUMNS.get(row["target_metric"])
        if column_spec is None:
            logger.info("目标 %s 指标 %s 暂不支持聚合", row["id"], row["target_metric"])
            return None

        column, agg = column_spec
        where = [f"{column} IS NOT NULL", "fetch_status IN ('success','partial')"]
        params: list = []
        if row["target_platform"]:
            where.append("platform = ?")
            params.append(row["target_platform"])
        if row["metric_period_start"]:
            where.append("fetched_at >= ?")
            params.append(row["metric_period_start"])
        if row["metric_period_end"]:
            where.append("fetched_at <= ?")
            params.append(row["metric_period_end"])

        sql = f"SELECT {agg.upper()}({column}) AS v, COUNT({column}) AS n FROM metrics WHERE " + " AND ".join(where)
        agg_row = self.conn.execute(sql, params).fetchone()
        sample_count = int(agg_row["n"] or 0) if agg_row else 0
        # R3：没有样本 → NULL（不是 0）
        value: Optional[float] = float(agg_row["v"]) if agg_row and agg_row["v"] is not None else None

        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE goals SET current_value = ?, progress_updated_at = ? WHERE id = ?",
            (value, now if value is not None else None, row["id"]),
        )
        self.conn.commit()
        logger.info(
            "🎯 目标 %s 进度刷新 — %s.%s = %s（样本 %d）",
            row["id"], row["target_metric"], agg, value, sample_count,
        )

        # 达成 → 结束目标 + 通知（G-15：goal_completed 场景）
        rate = self.achievement_rate(
            current=value, baseline=row["baseline_value"], target=row["target_value"],
        )
        if rate is not None and rate >= 100 and row["status"] == "active":
            self.conn.execute(
                "UPDATE goals SET status = 'completed', completed_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            self.conn.commit()
            from .notify import get_active  # 延迟导入避免循环依赖

            notifier = get_active()
            if notifier is not None:
                notifier.emit(
                    "goal_completed",
                    f"目标达成：{row['goal_text']}（{row['target_metric']} {value:.0f} / {row['target_value']:.0f}）",
                    platform=row["target_platform"],
                )
        return value

    # ------------------------------------------------------------------
    @staticmethod
    def achievement_rate(
        current: Optional[float], baseline: Optional[float], target: Optional[float],
    ) -> Optional[float]:
        """R21：(current - baseline) / target_value × 100。任一分量 NULL → NULL。"""
        if current is None or baseline is None or target is None or target == 0:
            return None
        return (current - baseline) / target * 100.0
