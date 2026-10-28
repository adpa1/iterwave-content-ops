"""
选题评估引擎 — 三模式自动切换 + 动态权重。

§15 选题评分三模式（权重和必须 = 100%，config.yaml Pydantic 已校验）。

R6.5 复用检查：Easel 只有 SKILL.md 指令（skill-topic-evaluator），无 Python 实现。
→ 独立实现。

核心能力：
1. 三模式判定（冷启动 / 样本积累 / 稳定运行）
2. 评分计算（各分量 × 权重，0-100）
3. 收缩估计（样本积累模式 history_performance 用）
4. 时效衰减（news half-life 7d / evergreen 365d）
5. 可复现 score_breakdown
6. ε-greedy 15% 探索（稳定运行模式）
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# =====================================================================
# 数据模型
# =====================================================================

@dataclass
class TopicItem:
    """选题库中的单条选题。"""
    id: str
    title: str
    dimension: str              # 分类维度（不写死，来自选题库）
    date: Optional[str] = None
    summary: str = ""
    link: str = ""
    source: str = ""
    time_sensitive: Optional[bool] = None  # LLM 打标，时事类 half-life 7d

    def age_days(self, now: Optional[datetime] = None) -> Optional[float]:
        """选题距今的天数（用于时效衰减）。"""
        if not self.date:
            return None
        try:
            now = now or datetime.now()
            # 支持多种日期格式
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.strptime(self.date, fmt)
                    return (now - dt).total_seconds() / 86400
                except ValueError:
                    continue
        except Exception:
            pass
        return None


@dataclass
class ScoredTopic:
    """评分后的选题，带完整可复现明细。"""
    topic: TopicItem
    total_score: float
    mode: str                     # cold_start / sample_accumulating / stable_operation
    score_breakdown: dict          # {"position_match": (raw, weight, score), ...}
    is_exploration: bool = False  # ε-greedy 抽中的探索选题

    def __lt__(self, other: "ScoredTopic") -> bool:
        return self.total_score < other.total_score

    def __repr__(self) -> str:
        return f"<ScoredTopic score={self.total_score:.1f} mode={self.mode} dim={self.topic.dimension}>"


# =====================================================================
# ScoringEngine
# =====================================================================

class ScoringEngine:
    """选题评分引擎。"""

    def __init__(self, cfg, db_conn):
        self.cfg = cfg
        self.db = db_conn
        self.scoring_cfg = cfg.scoring

    # ------------------------------------------------------------------
    # 入口：对一批 TopicItem 打分并排序
    # ------------------------------------------------------------------

    def score_topics(self, topics: list[TopicItem], feedback=None) -> list[ScoredTopic]:
        """对选题列表打分、排序，含 ε-greedy 探索。

        feedback：上一轮复盘结论（autogrow.StrategyFeedback）。按维度在总分上加/减分
        （已在构建时限幅），并写进 score_breakdown.strategy_feedback 便于追溯。
        """
        mode = self._determine_mode()
        weights = self._get_weights(mode)
        logger.info("评分模式 = %s（权重和 = %d）", mode, sum(weights.values()))

        scored: list[ScoredTopic] = []
        for topic in topics:
            s = self._score_one(topic, weights, mode)
            if feedback is not None:
                bias = feedback.bias_for(topic.dimension)
                if bias:
                    s.total_score = round(s.total_score + bias, 2)
                    s.score_breakdown["strategy_feedback"] = {
                        "raw": bias, "weight": 1, "score": round(bias, 2),
                        "reasons": list(feedback.bias_reasons.get(topic.dimension, []))[:3],
                    }
            scored.append(s)

        # 排序
        scored.sort(key=lambda s: s.total_score, reverse=True)

        # 稳定运行模式：ε-greedy 15% 探索
        if mode == "stable_operation" and scored:
            top_count = max(1, len(scored) * 85 // 100)
            exploration_count = len(scored) - top_count
            if exploration_count > 0:
                # 从非 Top-5 的选题里随机抽 exploration_count 个，标记探索
                non_top = scored[top_count:]
                random.shuffle(non_top)
                for s in non_top[:exploration_count]:
                    s.is_exploration = True
                logger.info("ε-greedy 探索: %d / %d 个选题标记为探索", exploration_count, len(scored))

        return scored

    # ------------------------------------------------------------------
    # 三模式判定
    # ------------------------------------------------------------------

    def _determine_mode(self) -> str:
        """§15.1：按历史数据量自动切换。"""
        # 查全库已发布样本数（draft.status=published）
        row = self.db.execute(
            "SELECT COUNT(*) FROM drafts WHERE status='published'"
        ).fetchone()
        total_published = row[0] if row else 0

        if total_published < 10:
            return "cold_start"

        # 检查活跃维度（90 天内至少 2 次使用）
        window = self.scoring_cfg.active_dimension_window_days
        min_uses = self.scoring_cfg.active_dimension_min_uses
        row = self.db.execute(
            "SELECT COUNT(DISTINCT t.dimension) FROM drafts d "
            "JOIN contents c ON d.content_id = c.id "
            "JOIN topic_index t ON c.topic_id = t.id "
            "WHERE d.status='published' "
            "AND d.user_confirmed_at >= datetime('now', ?)",
            (f"-{window} days",),
        ).fetchone()
        active_dims_count = row[0] if row else 0

        # 活跃维度中，有多少维度样本 < 5
        weak_dim_row = self.db.execute(
            "SELECT COUNT(*) FROM ( "
            "  SELECT t.dimension, COUNT(*) as cnt "
            "  FROM drafts d JOIN contents c ON d.content_id = c.id "
            "  JOIN topic_index t ON c.topic_id = t.id "
            "  WHERE d.status='published' "
            "  AND d.user_confirmed_at >= datetime('now', ?) "
            "  GROUP BY t.dimension HAVING cnt < 5 "
            ") w",
            (f"-{window} days",),
        ).fetchone()
        weak_dim_count = weak_dim_row[0] if weak_dim_row else 0

        if weak_dim_count > 0:
            return "sample_accumulating"
        return "stable_operation"

    def _get_weights(self, mode: str) -> dict[str, int]:
        """返回模式权重 dict（已 ×1 变成整数值）。"""
        mode_cfg = {
            "cold_start": self.scoring_cfg.cold_start,
            "sample_accumulating": self.scoring_cfg.sample_accumulating,
            "stable_operation": self.scoring_cfg.stable_operation,
        }[mode]
        return {
            "position_match": mode_cfg.position_match,
            "history_performance": mode_cfg.history_performance,
            "timeliness": mode_cfg.timeliness,
            "gap_weight": mode_cfg.gap_weight,
            "freshness": mode_cfg.freshness,
        }

    # ------------------------------------------------------------------
    # 单条打分
    # ------------------------------------------------------------------

    def _score_one(
        self, topic: TopicItem, weights: dict[str, int], mode: str,
    ) -> ScoredTopic:
        """单条选题打分，返回 ScoredTopic（含完整 breakdown）。"""
        breakdown: dict[str, tuple[float, int, float]] = {}

        # 1. 定位匹配 0-100
        pos_raw = self._score_position_match(topic)
        breakdown["position_match"] = (pos_raw, weights["position_match"], pos_raw * weights["position_match"] / 100)

        # 2. 历史表现（收缩估计）
        hist_raw = self._score_history_performance(topic, mode)
        breakdown["history_performance"] = (hist_raw, weights["history_performance"], hist_raw * weights["history_performance"] / 100)

        # 3. 时效性（指数衰减）
        time_raw = self._score_timeliness(topic, mode)
        breakdown["timeliness"] = (time_raw, weights["timeliness"], time_raw * weights["timeliness"] / 100)

        # 4. 缺口加权 0-100（gap_analyzer 输出 × 权重）
        gap_raw = self._score_gap_weight(topic)
        breakdown["gap_weight"] = (gap_raw, weights["gap_weight"], gap_raw * weights["gap_weight"] / 100)

        # 5. 新鲜度（近 30 天是否用过）
        fresh_raw = self._score_freshness(topic)
        breakdown["freshness"] = (fresh_raw, weights["freshness"], fresh_raw * weights["freshness"] / 100)

        # 总和
        total = sum(s for _, _, s in breakdown.values())

        return ScoredTopic(
            topic=topic,
            total_score=round(total, 2),
            mode=mode,
            score_breakdown={
                k: {"raw": r, "weight": w, "score": round(s, 2)}
                for k, (r, w, s) in breakdown.items()
            },
        )

    # ------------------------------------------------------------------
    # 各分量打分函数
    # ------------------------------------------------------------------

    def _score_position_match(self, topic: TopicItem) -> float:
        """账号定位匹配：persona_keywords 与 topic.title/summary 的重合度。"""
        # 多平台画像：按每个启用平台的生效画像分别算重合度，取最高（选题只要适合任一平台账号即可）
        from ..persona import effective_persona
        try:
            plats = self.cfg.platforms.enabled_keys()
        except Exception:  # noqa: BLE001
            plats = []
        personas = [effective_persona(self.cfg, p)["persona_keywords"] for p in plats] or [
            list(self.cfg.ultimate_goal.persona_keywords)
        ]
        personas = [p for p in personas if p]
        if not personas:
            return 60.0  # 无画像默认给 60
        haystack = (topic.title + " " + topic.summary + " " + topic.dimension).lower()
        best = 0.0
        for persona in personas:
            hits = sum(1 for kw in persona if kw.lower() in haystack)
            best = max(best, 100 * hits / len(persona))
        # 零命中 = 0 分（完全偏离账号定位的选题必须被显著压低，
        # 否则「新鲜但无关」的选题会靠时效+新鲜度挤掉「用过但高度相关」的选题）
        return best

    def _score_history_performance(self, topic: TopicItem, mode: str) -> float:
        """历史表现：同维度历史平均互动率（收缩估计）。"""
        if mode == "cold_start":
            return 0.0  # 冷启动无历史

        # 查同维度已发布内容的平均 interaction_rate
        row = self.db.execute(
            "SELECT AVG(m.interaction_rate), COUNT(*) "
            "FROM metrics m JOIN contents c ON m.content_id = c.id "
            "JOIN topic_index t ON c.topic_id = t.id "
            "WHERE t.dimension = ? AND m.fetch_status IN ('success','partial') "
            "AND m.interaction_rate IS NOT NULL",
            (topic.dimension,),
        ).fetchone()
        dim_mean, dim_count = (row[0] or 0.0, row[1] or 0)

        # 全局均值
        g_row = self.db.execute(
            "SELECT AVG(interaction_rate) FROM metrics "
            "WHERE fetch_status IN ('success','partial') AND interaction_rate IS NOT NULL"
        ).fetchone()
        global_mean = g_row[0] or 0.0

        if mode == "sample_accumulating":
            # 收缩估计：score = (n×dim_mean + m×global_mean)/(n+m)，m=5
            m = self.scoring_cfg.shrinkage_m
            score = (dim_count * dim_mean + m * global_mean) / (dim_count + m)
        else:  # stable_operation
            score = dim_mean if dim_count >= 5 else global_mean

        # 归一化到 0-100（假设全局平均约 0.05=5%，放大 2000 倍）
        return min(100.0, score * 2000)

    def _score_timeliness(self, topic: TopicItem, mode: str) -> float:
        """时效性：按 half-life 指数衰减（news 7d / evergreen 365d）。"""
        age = topic.age_days()
        if age is None:
            return 50.0  # 无日期给中间分

        if topic.time_sensitive is True:
            half_life = self.scoring_cfg.timeliness_half_life_days.get("news", 7)
        else:
            half_life = self.scoring_cfg.timeliness_half_life_days.get("evergreen", 365)

        # 指数衰减：age 天的分量 = 100 × 2^(-age / half_life)
        return 100.0 * (2 ** (-age / half_life))

    def _score_gap_weight(self, topic: TopicItem) -> float:
        """缺口加权（占位实现，未接真实缺口数据）。

        ⚠️ 项目内目前没有可调用的 gap_analyzer 持久化模块（仅有 api 层的 gap_analysis
        端点与 spiral 的概念提及），无法取得真实的"缺口填补强度"。此处仍按维度选题数
        与全局中位数比较做粗略估算（覆盖缺口→高加权 90，否则 40）。
        如需真实计分，需先实现 gap_analyzer 并把其持久化输出接到这里。
        """
        # 查该维度当前选题数
        row = self.db.execute(
            "SELECT COUNT(*) FROM topic_index WHERE dimension = ?",
            (topic.dimension,),
        ).fetchone()
        dim_count = row[0] if row else 0

        # 全维度的中位数（SQLite 不支持 PERCENTILE_CONT，用 Python 算）
        med_row = self.db.execute(
            "SELECT COUNT(*) as cnt FROM topic_index GROUP BY dimension"
        ).fetchall()
        all_counts = sorted(r[0] for r in med_row)
        n = len(all_counts)
        if n == 0:
            median_count = 0
        elif n % 2:
            median_count = all_counts[n // 2]
        else:
            median_count = (all_counts[n // 2 - 1] + all_counts[n // 2]) / 2

        if median_count > 0 and dim_count < median_count * 0.5:
            # 覆盖缺口 — 给高加权
            return 90.0
        return 40.0

    def _score_freshness(self, topic: TopicItem) -> float:
        """新鲜度：近 30 天是否使用过。"""
        row = self.db.execute(
            "SELECT COUNT(*) FROM topic_index WHERE id = ? AND first_seen_at >= datetime('now','-30 days')",
            (topic.id,),
        ).fetchone()
        if row and row[0] > 0:
            # 用过，打低分
            use_row = self.db.execute(
                "SELECT total_uses FROM topic_index WHERE id = ?", (topic.id,)
            ).fetchone()
            uses = use_row[0] if use_row else 1
            return max(10.0, 100.0 - uses * 20.0)
        return 100.0  # 30 天没用过，满分
