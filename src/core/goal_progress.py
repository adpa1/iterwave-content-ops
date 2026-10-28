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


# 比率类指标：metrics 表里存的是 0–1 小数（adapters.base.interaction_rate 等），
# 但目标值由人按「百分数」填写（互动率 ≥ 4 表示 4%）。current_value 回写时统一换算为百分数，
# 让 target / baseline / current 三者同一单位，达成率才有意义。
PERCENT_METRICS = {"interaction_rate", "like_rate", "collect_rate"}


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
        get = row.get if isinstance(row, dict) else (lambda k: row[k] if k in row.keys() else None)
        # 人工录入的目标：当前值由人填写，自动刷新不能把它覆盖成 NULL
        if (get("data_source") or "") == "manual":
            cur = get("current_value")
            self._maybe_complete(row, cur, datetime.now().isoformat(timespec="seconds"))
            return cur
        # 涨粉类目标：优先用账号级快照（作品级读回没有新增粉丝字段）
        if row["target_metric"] in ("followers_gained", "new_followers"):
            snap_value = self._followers_from_snapshots(row)
            if snap_value is not None:
                now = datetime.now().isoformat(timespec="seconds")
                self.conn.execute(
                    "UPDATE goals SET current_value = ?, progress_updated_at = ? WHERE id = ?",
                    (snap_value, now, row["id"]),
                )
                self.conn.commit()
                self._maybe_complete(row, snap_value, now)
                return snap_value

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
        if value is not None and row["target_metric"] in PERCENT_METRICS:
            value = round(value * 100.0, 4)  # 0.038 → 3.8（%）

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

        self._maybe_complete(row, value, now)
        return value

    def _followers_from_snapshots(self, row) -> Optional[float]:
        """账号快照里的粉丝总数（目标平台；全平台 = 各平台最新值之和）。

        delta 口径：achievement_rate 用 (current - baseline) / target，current 取周期内最新粉丝总数，
        baseline 是创建目标时填的起始粉丝数；total 口径直接比较总数。没有快照 → None（回落作品级聚合）。
        """
        try:
            plats = [row["target_platform"]] if row["target_platform"] else [
                r[0] for r in self.conn.execute("SELECT DISTINCT platform FROM account_snapshots").fetchall()
            ]
        except Exception:  # noqa: BLE001 — 旧库没有快照表
            return None
        total, found = 0.0, False
        for p in plats:
            params: list = [p]
            sql = "SELECT followers FROM account_snapshots WHERE platform = ? AND followers IS NOT NULL"
            if row["metric_period_end"]:
                sql += " AND captured_at <= ?"
                params.append(str(row["metric_period_end"])[:10] + "T23:59:59")
            sql += " ORDER BY captured_at DESC LIMIT 1"
            r = self.conn.execute(sql, params).fetchone()
            if r is not None:
                total += float(r[0])
                found = True
        return total if found else None

    def _maybe_complete(self, row, value: Optional[float], now: str) -> None:
        # 达成 → 结束目标 + 通知（G-15：goal_completed 场景）
        rate = self.achievement_rate(
            current=value, baseline=row["baseline_value"], target=row["target_value"],
            scope=row["metric_scope"] or "delta",
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

    # ------------------------------------------------------------------
    @staticmethod
    def achievement_rate(
        current: Optional[float], baseline: Optional[float], target: Optional[float],
        scope: Optional[str] = "delta",
    ) -> Optional[float]:
        """R21 达成率（百分比，不截断，可 >100 或 <0）。任一必要分量 NULL → NULL。

        - delta（增量口径，默认）：(current - baseline) / target × 100
        - total（总量口径，DB CHECK 值）：current / target × 100 —— 目标值本身就是要达到的绝对水平
          （如互动率 ≥ 4%），此时减基线会把进度算错。
        """
        if current is None or target is None or target == 0:
            return None
        if scope in ("total", "absolute"):
            return current / target * 100.0
        if baseline is None:
            return None
        return (current - baseline) / target * 100.0

    @classmethod
    def rate_for(cls, row) -> Optional[float]:
        """从 goals 行（dict / sqlite3.Row）直接算达成率，统一各调用点口径。"""
        get = row.get if isinstance(row, dict) else (lambda k: row[k] if k in row.keys() else None)
        return cls.achievement_rate(
            current=get("current_value"), baseline=get("baseline_value"),
            target=get("target_value"), scope=get("metric_scope") or "delta",
        )

    @classmethod
    def summarize(cls, rows: list, now: Optional[datetime] = None, mode: str = "unified") -> dict:
        """总体目标进度汇总（Dashboard / Goals 的 goal-hero 用，前端不再自行计算）。

        mode（config.ultimate_goal.goal_mode）：
        - unified：所有活跃目标达成率等权平均（每个先截断到 [0,100]，单个超额不能掩盖其它落后）。
        - per_platform：各平台分别汇总（by_platform），总进度 = 各平台进度的等权平均，仅作参考。
        - hybrid：有总目标（goal_level=north_star）且总目标自身有数据 → 用总目标达成率；
          否则用挂在总目标下的平台子目标按 weight 加权平均；平台各自进度仍在 by_platform。
        没有任何数据 → percent = None（R21：未知 ≠ 0）。
        """
        now = now or datetime.now()
        active = [dict(r) for r in rows if (dict(r).get("status") or "active") == "active"]

        def _rate(g: dict) -> Optional[float]:
            r = cls.rate_for(g)
            return None if r is None else max(0.0, min(100.0, r))

        def _mean(goals: list[dict], weighted: bool = False) -> tuple[Optional[float], int]:
            num, den, n = 0.0, 0.0, 0
            for g in goals:
                r = _rate(g)
                if r is None:
                    continue
                w = float(g.get("weight") or 1.0) if weighted else 1.0
                num += r * w
                den += w
                n += 1
            return (round(num / den, 1) if den else None), n

        north = [g for g in active if (g.get("goal_level") or "platform") == "north_star"]
        platform_goals = [g for g in active if (g.get("goal_level") or "platform") != "north_star"]

        by_platform: dict[str, dict] = {}
        for g in platform_goals:
            key = g.get("target_platform") or "all"
            by_platform.setdefault(key, []).append(g)  # type: ignore[arg-type]
        by_platform_out = {}
        for key, goals in by_platform.items():
            pct, n = _mean(goals)  # type: ignore[arg-type]
            by_platform_out[key] = {"percent": pct, "goals": len(goals), "counted": n}  # type: ignore[arg-type]

        method = "equal_weight_mean"
        counted_goals = 0
        if mode == "hybrid" and north:
            ns = north[0]
            own = _rate(ns)
            children = [g for g in platform_goals if g.get("parent_goal_id") == ns["id"]] or platform_goals
            if own is not None:
                percent, counted_goals, method = round(own, 1), 1, "north_star_own"
            else:
                percent, counted_goals = _mean(children, weighted=True)
                method = "weighted_children"
            scope_goals = [ns] + children
        elif mode == "per_platform":
            vals = [v["percent"] for v in by_platform_out.values() if v["percent"] is not None]
            percent = round(sum(vals) / len(vals), 1) if vals else None
            counted_goals = sum(v["counted"] for v in by_platform_out.values())
            method = "per_platform_mean"
            scope_goals = platform_goals
        else:
            percent, counted_goals = _mean(active)
            scope_goals = active

        starts = sorted(g["metric_period_start"] for g in scope_goals if g.get("metric_period_start"))
        ends = sorted(g["metric_period_end"] for g in scope_goals if g.get("metric_period_end"))
        period_end = ends[-1] if ends else None
        days_left = None
        if period_end:
            try:
                end_dt = datetime.fromisoformat(period_end[:10])
                days_left = (end_dt.date() - now.date()).days
            except ValueError:
                days_left = None
        return {
            "percent": percent,
            "method": method,
            "mode": mode,
            "active_goals": len(scope_goals),
            "counted_goals": counted_goals,
            "pending_goals": max(0, len(scope_goals) - counted_goals) if method != "north_star_own" else 0,
            "period_start": starts[0] if starts else None,
            "period_end": period_end,
            "days_left": days_left,
            "north_star": (
                {"id": north[0]["id"], "goal_text": north[0]["goal_text"], "rate": _rate(north[0])}
                if north else None
            ),
            "by_platform": by_platform_out,
        }
