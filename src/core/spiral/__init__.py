"""
螺旋引擎 — 复盘 → 三通道输出（§1）。

§1 三通道：
1. 策略调整建议 — 下一轮评分权重调整、选题维度转向
2. 缺口分析     — coverage_gap（给用户补选题）/ validation_gap（Agent 自己试水）
3. 运营调整建议 — 节奏调整、评论/关注配额调整

R19 gap_analyzer 两类缺口 audience 不能混展示：
- coverage_gap  → audience='user'，正常展示给用户
- validation_gap → audience='agent'，灰色小字标注或默认折叠
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


# =====================================================================
# 数据模型
# =====================================================================

@dataclass
class GapSuggestion:
    """缺口建议 — 两类 gap 合并输出。"""
    dimension: str
    gap_type: str               # coverage_gap / validation_gap
    audience: str               # user / agent
    suggestion_text: str
    current_count: int
    target_count: Optional[int] = None
    sample_count: int = 0
    avg_interaction_rate: Optional[float] = None
    confidence: str = "medium"  # high / medium / low


@dataclass
class StrategyAdjustment:
    """策略调整建议。"""
    type: str                   # weight_shift / dimension_pivot / content_format
    description: str
    reason_data: dict = field(default_factory=dict)


@dataclass
class SpiralDecision:
    """复盘输出 — 三通道。"""
    round_id: int
    created_at: datetime
    gaps: list[GapSuggestion] = field(default_factory=list)
    strategy_adjustments: list[StrategyAdjustment] = field(default_factory=list)
    ops_adjustments: list[str] = field(default_factory=list)
    platform_insights: list[dict] = field(default_factory=list)  # 第四通道：单平台纵深（G-04）
    evidence: dict = field(default_factory=dict)  # 证据：触发数据 + 样本量 + 判断理由

    def to_json(self) -> str:
        return json.dumps({
            "round_id": self.round_id,
            "created_at": self.created_at.isoformat(),
            "gaps": [g.__dict__ for g in self.gaps],
            "strategy_adjustments": [s.__dict__ for s in self.strategy_adjustments],
            "ops_adjustments": self.ops_adjustments,
            "platform_insights": self.platform_insights,
            "evidence": self.evidence,
        }, ensure_ascii=False)


# =====================================================================
# SpiralEngine
# =====================================================================

class SpiralEngine:
    """螺旋引擎 — 复盘分析。"""

    def __init__(self, cfg, db_conn):
        self.cfg = cfg
        self.db = db_conn

    def analyze_round(self, round_id: int) -> SpiralDecision:
        """分析某轮的已发布表现，输出三通道建议。"""
        logger.info("🌀 螺旋引擎分析 round_id=%d", round_id)
        decision = SpiralDecision(round_id=round_id, created_at=datetime.now())

        # 1. 取本轮已发布数据
        published_metrics = self._fetch_published_metrics(round_id)
        decision.evidence = self._build_evidence(published_metrics)

        # 2. 缺口分析（两类 gap）
        decision.gaps = self._analyze_gaps()

        # 3. 策略调整（根据各平台表现）
        decision.strategy_adjustments = self._suggest_strategy(published_metrics)

        # 4. 运营调整（简单建议）
        decision.ops_adjustments = self._suggest_ops(published_metrics)

        # 5. 单平台纵深（第四通道，G-04）：写入 platform_insights
        decision.platform_insights = self._analyze_platforms(round_id)

        # 持久化
        self.db.execute(
            "UPDATE spiral_rounds SET decision = ? WHERE id = ?",
            (decision.to_json(), round_id),
        )
        self.db.commit()
        return decision

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _analyze_platforms(self, round_id: int) -> list[dict]:
        """第四通道：单平台纵深。失败只记日志，不阻断复盘主流程。"""
        try:
            from ..insights import PlatformInsightsEngine

            engine = PlatformInsightsEngine(self.cfg, self.db)
            return [i.to_dict() for i in engine.analyze(round_id=round_id)]
        except Exception:
            logger.exception("单平台纵深分析失败（不阻断复盘）round_id=%s", round_id)
            return []

    def _fetch_published_metrics(self, round_id: int) -> list[dict]:
        """取本轮所有已发布且有有效 metrics 的记录。"""
        rows = self.db.execute(
            "SELECT m.platform, m.interaction_rate, m.likes, m.views, "
            "       m.fetch_status, t.dimension "
            "FROM metrics m "
            "JOIN contents c ON m.content_id = c.id "
            "JOIN topic_index t ON c.topic_id = t.id "
            "WHERE c.round_id = ? AND m.fetch_status IN ('success','partial') "
            "ORDER BY m.platform",
            (round_id,),
        ).fetchall()
        return [
            {
                "platform": r[0],
                "interaction_rate": r[1],
                "likes": r[2],
                "views": r[3],
                "fetch_status": r[4],
                "dimension": r[5],
            }
            for r in rows
        ]

    def _build_evidence(self, metrics: list[dict]) -> dict:
        """构造 evidence 块（§16.3 契约）。"""
        if not metrics:
            return {
                "trigger": "样本不足",
                "sample_count": 0,
                "note": "本轮无已发布有效数据",
            }

        platforms = sorted(set(m["platform"] for m in metrics))
        # R3：interaction_rate 为 NULL 的样本不参与均值（不按 0 计入）
        valid = [m for m in metrics if m["interaction_rate"] is not None]
        avg_ir = (
            round(sum(m["interaction_rate"] for m in valid) / len(valid), 4)
            if valid else None
        )

        return {
            "trigger": "复盘触发",
            "sample_count": len(metrics),
            "platform_count": len(platforms),
            "platforms": platforms,
            "avg_interaction_rate": avg_ir,
        }

    def _analyze_gaps(self) -> list[GapSuggestion]:
        """§15.3 两类缺口分析。"""
        # 各维度库存
        rows = self.db.execute(
            "SELECT dimension, COUNT(*) as cnt FROM topic_index GROUP BY dimension"
        ).fetchall()
        dim_counts = {r[0]: r[1] for r in rows}

        if not dim_counts:
            return []

        # 全局中位数
        counts = sorted(dim_counts.values())
        n = len(counts)
        median = counts[n // 2] if n % 2 else (counts[n // 2 - 1] + counts[n // 2]) / 2

        # 全局平均互动率（已发布样本）
        g_row = self.db.execute(
            "SELECT AVG(interaction_rate) FROM metrics "
            "WHERE fetch_status IN ('success','partial') AND interaction_rate IS NOT NULL"
        ).fetchone()
        # R3：没有任何有效样本 → NULL（不按 0 参与比较，避免误判覆盖缺口）
        global_avg_ir = g_row[0] if g_row and g_row[0] is not None else None

        gaps: list[GapSuggestion] = []

        for dim, count in dim_counts.items():
            # 查该维度已发布样本数
            p_row = self.db.execute(
                "SELECT COUNT(*), AVG(m.interaction_rate) "
                "FROM metrics m JOIN contents c ON m.content_id = c.id "
                "JOIN topic_index t ON c.topic_id = t.id "
                "WHERE t.dimension = ? AND m.fetch_status IN ('success','partial') "
                "AND m.interaction_rate IS NOT NULL",
                (dim,),
            ).fetchone()
            sample_count = p_row[0] if p_row else 0
            avg_ir = p_row[1] if p_row else None

            # validation_gap — 样本不足（Agent 自己试水）
            policy = self.cfg.spiral
            if sample_count < policy.min_dimension_samples:
                gaps.append(GapSuggestion(
                    dimension=dim,
                    gap_type="validation_gap",
                    audience="agent",
                    suggestion_text=f"下一轮给【{dim}】分配 {policy.validation_gap_topic_min}-{policy.validation_gap_topic_max} 个选题试水（验证缺口）",
                    current_count=count,
                    sample_count=sample_count,
                    avg_interaction_rate=avg_ir,
                    confidence="low",
                ))
                continue

            # coverage_gap — 覆盖缺口（库内少且表现好；全局均值为 NULL 时无法比较）
            if (
                global_avg_ir is not None and count < median * policy.coverage_gap_ratio
                and avg_ir and avg_ir > global_avg_ir * policy.coverage_performance_multiplier
            ):
                target_count = int(median)
                suggested_add = max(policy.min_suggested_add, target_count - count)
                gaps.append(GapSuggestion(
                    dimension=dim,
                    gap_type="coverage_gap",
                    audience="user",
                    suggestion_text=f"建议往选题库补充【{dim}】约 {suggested_add} 条（覆盖缺口）",
                    current_count=count,
                    target_count=target_count,
                    sample_count=sample_count,
                    avg_interaction_rate=avg_ir,
                    confidence="high",
                ))

        return gaps

    def _suggest_strategy(self, metrics: list[dict]) -> list[StrategyAdjustment]:
        """根据各平台表现建议权重调整。"""
        adjustments: list[StrategyAdjustment] = []
        if not metrics:
            return adjustments

        # 按平台分组
        by_platform: dict[str, list[dict]] = {}
        for m in metrics:
            by_platform.setdefault(m["platform"], []).append(m)

        for platform, pm in by_platform.items():
            # R3：只对有效互动率样本求均值；无有效样本的平台不产出建议
            valid = [m for m in pm if m["interaction_rate"] is not None]
            policy = self.cfg.spiral
            if len(valid) < policy.min_platform_samples:
                continue
            avg_ir = sum(m["interaction_rate"] for m in valid) / len(valid)
            if avg_ir < policy.low_interaction_rate_threshold:
                adjustments.append(StrategyAdjustment(
                    type="dimension_pivot",
                    description=f"平台 {platform} 互动率偏低（{avg_ir:.2%}），建议增加缺口分析维度的选题占比",
                    reason_data={"avg_interaction_rate": avg_ir, "sample_count": len(valid)},
                ))

        return adjustments

    def _suggest_ops(self, metrics: list[dict]) -> list[str]:
        """运营调整建议。"""
        suggestions: list[str] = []
        policy = self.cfg.spiral
        if len(metrics) < policy.min_metrics_for_ops_adjustment:
            suggestions.append(f"本轮样本 < {policy.min_metrics_for_ops_adjustment}，建议下一轮增加选题数量后再做运营调整")
        else:
            suggestions.append(f"维持当前运营配额节奏，等 ≥{policy.min_rounds_for_systematic_adjustment} 轮数据后做系统性调整")
        return suggestions
