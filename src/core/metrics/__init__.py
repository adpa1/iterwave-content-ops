"""
数据归一化 — z-score 平台内部基准。

§14 / R13：
- 各平台独立计算自己的 mean/std，本平台内容的 z = (value - platform_mean) / platform_std
- 不同平台 +1σ 不代表相同业务表现，只代表"高于本平台自身平均水平 1σ"
- 可跨平台 z-score 对比的指标：互动率、收藏率、涨粉效率
- 原始曝光/点赞数不做跨平台 z-score 对比
- views 为 NULL 或 0 时所有比率为 NULL，**不做除法**（R3）
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import statistics

from ...adapters.base import Platform, MetricsSnapshot, FetchStatus

logger = logging.getLogger(__name__)


@dataclass
class PlatformStats:
    """某平台某指标的 mean / std。"""
    platform: Platform
    metric: str             # interaction_rate / like_rate / views ...
    mean: float
    std: float
    sample_count: int


class Normalizer:
    """跨平台数据归一化。"""

    def __init__(self, db_conn, cfg=None):
        self.db = db_conn
        self.cfg = cfg

    # ------------------------------------------------------------------
    # 计算平台内部 mean / std
    # ------------------------------------------------------------------

    def compute_platform_stats(
        self, platform: Platform, metric: str, window_days: Optional[int] = None,
    ) -> Optional[PlatformStats]:
        """计算某平台某指标的历史均值和标准差。

        window_days: 时效窗口；缺省读取评分配置。
        """
        if window_days is None:
            cfg = self.cfg
            if cfg is None:
                from ..config import get_config
                cfg = get_config()
            window_days = cfg.scoring.normalization_window_days
        # 可直接做 z-score 的比率指标
        rate_metrics = {
            "interaction_rate", "like_rate", "collect_rate", "follower_efficiency",
        }
        # 原始计数指标（只平台内部 z，不跨平台比）
        raw_metrics = {"views", "likes", "collects", "comments", "shares", "new_followers"}

        if metric not in rate_metrics and metric not in raw_metrics:
            logger.warning("未知指标: %s", metric)
            return None

        # 从 metrics 表取数据
        col_name = {
            "interaction_rate": "interaction_rate",
            "like_rate": "like_rate",
            "collect_rate": "collect_rate",
            "follower_efficiency": "follower_efficiency",
            "views": "views",
            "likes": "likes",
            "collects": "collects",
            "comments": "comments",
            "shares": "shares",
            "new_followers": "new_followers",
        }[metric]

        cutoff = (datetime.now() - timedelta(days=window_days)).isoformat()
        rows = self.db.execute(
            f"SELECT {col_name} FROM metrics "
            f"WHERE platform = ? AND fetch_status IN ('success','partial') "
            f"AND {col_name} IS NOT NULL "
            f"AND fetched_at >= ?",
            (platform.value, cutoff),
        ).fetchall()

        values = [r[0] for r in rows if r[0] is not None]
        if len(values) < 2:
            return None  # 样本不够

        mean = statistics.mean(values)
        std = statistics.pstdev(values)
        if std == 0:
            return None  # 无波动，z-score 无意义

        return PlatformStats(
            platform=platform, metric=metric,
            mean=mean, std=std, sample_count=len(values),
        )

    # ------------------------------------------------------------------
    # 计算 z-score（平台内部）
    # ------------------------------------------------------------------

    def z_score(
        self, platform: Platform, metric: str, value: Optional[float],
    ) -> Optional[float]:
        """计算平台内部 z-score。

        value 为 None → 返回 None（R3 不计算）。
        """
        if value is None:
            return None
        stats = self.compute_platform_stats(platform, metric)
        if stats is None:
            return None
        return round((value - stats.mean) / stats.std, 3)

    # ------------------------------------------------------------------
    # Winsorization（极端值截断，§14）
    # ------------------------------------------------------------------

    @staticmethod
    def winsorize(value: float, p01: float, p99: float) -> float:
        """按 1% / 99% 分位数截断极端值。"""
        return max(p01, min(p99, value))

    # ------------------------------------------------------------------
    # 归一化比率计算（views=NULL/0 时全部返回 None）
    # ------------------------------------------------------------------

    @staticmethod
    def safe_ratio(numerator: Optional[int], denominator: Optional[int]) -> Optional[float]:
        """安全比率计算（R3）。

        任一入参为 None → 返回 None（NULL 不等于 0，不允许把缺失当零算）；
        denominator ≤ 0 → 返回 None，不做除法。
        """
        if numerator is None or denominator is None or denominator <= 0:
            return None
        return numerator / denominator
