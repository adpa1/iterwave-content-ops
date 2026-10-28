"""R11 / R12：三模式权重必须和 = 100，模式按样本量自动切换。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from src.core.config import ScoringModeConfig
from src.core.scoring.topic_eval import ScoringEngine, TopicItem


def _weights(cfg, mode: str) -> dict[str, int]:
    e = ScoringEngine(cfg, db_conn=None)  # type: ignore[arg-type]
    return e._get_weights(mode)


def test_cold_start_weights(cfg) -> None:
    """冷启动：定位 40 / 时效 30 / 新鲜度 30，历史 0 / 缺口 0。"""
    w = _weights(cfg, "cold_start")
    assert w == {
        "position_match": 40,
        "history_performance": 0,
        "timeliness": 30,
        "gap_weight": 0,
        "freshness": 30,
    }
    assert sum(w.values()) == 100


def test_sample_accumulating_weights(cfg) -> None:
    """样本积累：35 / 10 / 25 / 10 / 20。"""
    w = _weights(cfg, "sample_accumulating")
    assert w == {
        "position_match": 35,
        "history_performance": 10,
        "timeliness": 25,
        "gap_weight": 10,
        "freshness": 20,
    }
    assert sum(w.values()) == 100


def test_stable_operation_weights(cfg) -> None:
    """稳定运行：30 / 25 / 20 / 15 / 10。"""
    w = _weights(cfg, "stable_operation")
    assert w == {
        "position_match": 30,
        "history_performance": 25,
        "timeliness": 20,
        "gap_weight": 15,
        "freshness": 10,
    }
    assert sum(w.values()) == 100


def test_all_modes_sum_to_100(cfg) -> None:
    for mode in ("cold_start", "sample_accumulating", "stable_operation"):
        assert sum(_weights(cfg, mode).values()) == 100


def test_weight_sum_validator_rejects_bad_config() -> None:
    """权重和 ≠ 100 的配置必须直接校验失败（不能静默接受）。"""
    with pytest.raises(ValueError, match="100"):
        from src.core.config import ScoringConfig
        ScoringConfig(
            cold_start=ScoringModeConfig(position_match=40, timeliness=30, freshness=20),
            sample_accumulating=ScoringModeConfig(
                position_match=35, history_performance=10, timeliness=25,
                gap_weight=10, freshness=20,
            ),
            stable_operation=ScoringModeConfig(
                position_match=30, history_performance=25, timeliness=20,
                gap_weight=15, freshness=10,
            ),
        )


# ---------------------------------------------------------------------
# 模式自动切换：< 10 篇已发布 → 冷启动
# ---------------------------------------------------------------------

def _pub(seed, r, platform="xhs", topic_id="t1", days_ago=0):
    confirmed = (datetime.now() - timedelta(days=days_ago)).isoformat()
    d = seed.draft(r, platform=platform, draft_status="published",
                   fetch_status="success", topic_id=topic_id)
    seed.conn.execute("UPDATE drafts SET user_confirmed_at = ? WHERE id = ?", (confirmed, d))
    seed.conn.commit()
    return d


def test_mode_is_cold_start_below_ten_samples(cfg, seed) -> None:
    r = seed.round()
    for i in range(9):
        _pub(seed, r, topic_id=f"c{i}")
    assert ScoringEngine(cfg, seed.conn)._determine_mode() == "cold_start"


def test_mode_is_stable_when_all_active_dimensions_have_five(cfg, seed) -> None:
    """≥10 已发布 且 所有活跃维度 ≥5 → 稳定运行。"""
    r = seed.round()
    for i in range(10):
        _pub(seed, r, topic_id=f"c{i}", days_ago=1)
    assert ScoringEngine(cfg, seed.conn)._determine_mode() == "stable_operation"


def test_mode_is_sample_accumulating_when_a_dimension_is_weak(cfg, seed) -> None:
    """≥10 已发布 但有维度样本 < 5 → 样本积累。"""
    r = seed.round()
    for i in range(8):
        _pub(seed, r, topic_id=f"c{i}", days_ago=1)
    # 另一个维度只出现 1 次（< 5）
    for i in range(2):
        d = _pub(seed, r, topic_id=f"w{i}", days_ago=1)
        seed.conn.execute(
            "UPDATE topic_index SET dimension = '效率工具' WHERE id = ?", (f"w{i}",)
        )
        seed.conn.commit()
    assert ScoringEngine(cfg, seed.conn)._determine_mode() == "sample_accumulating"


def test_scored_topics_sorted_desc(cfg, seed) -> None:
    """打分结果按 total_score 降序（UI 直接消费顺序）。"""
    seed.round()
    today = datetime.now().strftime("%Y-%m-%d")
    old = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    topics = [
        TopicItem(id="a", title="AI 教育政策解读", dimension="AI教育", date=today),
        TopicItem(id="b", title="无关内容", dimension="其它", date=old),
    ]
    out = ScoringEngine(cfg, seed.conn).score_topics(topics)
    scores = [s.total_score for s in out]
    assert scores == sorted(scores, reverse=True)
