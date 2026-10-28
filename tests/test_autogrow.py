"""自动闭环（自生长）：复盘结论 → 评分 / 文案，自动开下一轮与停止条件。"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta

import pytest

from src.core.autogrow import AutogrowController, StrategyFeedback, build_feedback
from src.core.config_store import patch_config
from src.core.content_pipeline import build_hint_block
from src.core.guard.ultimate_goal_checker import UltimateGoalChecker
from src.core.scoring.topic_eval import ScoringEngine, TopicItem


def _content(conn, round_id, topic_id, dimension, platform, title, ir):
    conn.execute(
        "INSERT OR IGNORE INTO topic_index (id, dimension, source, title) VALUES (?, ?, 'test', ?)",
        (topic_id, dimension, topic_id),
    )
    cid, did = uuid.uuid4().hex, uuid.uuid4().hex
    conn.execute(
        "INSERT INTO contents (id, round_id, topic_id, platform, title, body) VALUES (?, ?, ?, ?, ?, 'B')",
        (cid, round_id, topic_id, platform, title),
    )
    conn.execute(
        "INSERT INTO drafts (id, content_id, platform, status) VALUES (?, ?, ?, 'published')",
        (did, cid, platform),
    )
    conn.execute(
        "INSERT INTO metrics (draft_id, content_id, platform, fetch_status, fetched_at, attempt, interaction_rate) "
        "VALUES (?, ?, ?, 'success', ?, 1, ?)",
        (did, cid, platform, datetime.now().isoformat(sep=" "), ir),
    )


def _reviewed_round(conn, seed, decision: dict, status="completed") -> int:
    rid = seed.round(status=status)
    conn.execute("UPDATE spiral_rounds SET decision = ? WHERE id = ?", (json.dumps(decision, ensure_ascii=False), rid))
    conn.commit()
    return rid


DECISION = {
    "gaps": [{"dimension": "职场", "gap_type": "validation_gap"}],
    "strategy_adjustments": [{"description": "平台 xhs 互动率偏低，建议增加缺口维度占比"}],
    "platform_insights": [{
        "platform": "xhs", "sample_count": 6,
        "best_title_style": {"best_bucket": "13-18"},
        "top_dimensions": [{"dimension": "AI教育"}],
        "conclusion": "",
    }],
}


# ---------------- 复盘结论 → 反馈 ----------------

def test_no_review_means_empty_feedback(cfg, conn):
    fb = build_feedback(cfg, conn)
    assert fb.empty and fb.source_round_ids == []


def test_feedback_bias_and_hints(cfg, conn, seed):
    rid = _reviewed_round(conn, seed, DECISION)
    for i in range(5):
        _content(conn, rid, f"a{i}", "AI教育", "xhs", f"好标题{i}", 0.09)
        _content(conn, rid, f"b{i}", "生活", "xhs", f"一般标题{i}", 0.01)
    conn.commit()

    fb = build_feedback(cfg, conn, UltimateGoalChecker(cfg))
    cap = cfg.autogrow.feedback_max_bias
    assert fb.source_round_ids == [rid]
    assert 0 < fb.dimension_bias["AI教育"] <= cap      # 表现好 → 加分，且有上限
    assert -cap <= fb.dimension_bias["生活"] < 0       # 表现差 → 减分
    assert fb.dimension_bias["职场"] > 0               # 验证缺口 → 小幅试水加分
    xhs = fb.hints_for("xhs")
    assert any("13-18" in h for h in xhs)
    assert any("好标题" in h for h in xhs)             # 高互动标题作参考
    assert any("缺口维度" in h for h in fb.hints_for("douyin"))  # 通用要点各平台都有


def test_failed_rounds_not_used_as_feedback(cfg, conn, seed):
    _reviewed_round(conn, seed, DECISION, status="failed")
    assert build_feedback(cfg, conn).empty


def test_redline_hint_dropped_by_guard(cfg, conn, seed):
    redline = cfg.ultimate_goal.redline_keywords[0]
    _reviewed_round(conn, seed, {"strategy_adjustments": [{"description": f"多写{redline}相关内容"}]})
    fb = build_feedback(cfg, conn, UltimateGoalChecker(cfg))
    assert not any(redline in h for h in fb.hints_for("xhs"))
    assert any(redline in h for h in fb.dropped_hints)


def test_scoring_applies_bias(cfg, conn):
    topics = [TopicItem(id="x", title="选题 X", dimension="AI教育"),
              TopicItem(id="y", title="选题 Y", dimension="生活")]
    base = {s.topic.id: s.total_score for s in ScoringEngine(cfg, conn).score_topics(topics)}
    fb = StrategyFeedback(dimension_bias={"AI教育": 10.0, "生活": -5.0})
    out = {s.topic.id: s for s in ScoringEngine(cfg, conn).score_topics(topics, feedback=fb)}
    assert out["x"].total_score == pytest.approx(base["x"] + 10)
    assert out["y"].total_score == pytest.approx(base["y"] - 5)
    assert out["x"].score_breakdown["strategy_feedback"]["score"] == 10


def test_hint_block():
    assert build_hint_block(None) == ""
    block = build_hint_block(["标题 13-18 字", " "])
    assert "上一轮复盘要点" in block and "- 标题 13-18 字" in block and "红线" in block


# ---------------- 自动开轮的判定 ----------------

class FakeExecutor:
    def __init__(self, conn, status="awaiting_publish"):
        self.conn = conn
        self.status = status
        self.calls = []

    def _resolve_platforms(self, _):
        from src.adapters.base import Platform
        return [Platform.XHS]

    def _platform_broken(self, _):
        return False

    async def run(self, round_id, topic_count, platform_values, save_drafts, feedback):
        self.calls.append((round_id, topic_count, platform_values, feedback))
        self.conn.execute("UPDATE spiral_rounds SET status = ? WHERE id = ?", (self.status, round_id))
        self.conn.commit()
        return {"drafts_saved": 3, "final_status": self.status}


class FakePvm:
    def load_plan(self):
        return {}, None, None, "default"

    def build_plan(self, **kw):
        return kw

    def record(self, *a, **kw):
        return "sig"


def _ctl(cfg, conn, executor=None):
    return AutogrowController(cfg, conn, executor or FakeExecutor(conn), pvm=FakePvm(),
                              guard=UltimateGoalChecker(cfg))


def _age_round(conn, rid, hours):
    at = (datetime.now() - timedelta(hours=hours)).isoformat()
    conn.execute("UPDATE spiral_rounds SET transition_log = ?, created_at = ? WHERE id = ?",
                 (json.dumps([{"to": "completed", "at": at}]), at.replace("T", " ")[:19], rid))
    conn.commit()


def test_tick_starts_round_with_feedback(cfg, conn, seed):
    rid = _reviewed_round(conn, seed, DECISION)
    _age_round(conn, rid, 48)
    ex = FakeExecutor(conn)
    result = asyncio.run(_ctl(cfg, conn, ex).tick())
    assert result["action"] == "started"
    new = conn.execute("SELECT trigger_source, applied_feedback FROM spiral_rounds WHERE id = ?",
                       (result["round_id"],)).fetchone()
    assert new[0] == "auto"
    assert json.loads(new[1])["source_round_ids"] == [rid]
    assert ex.calls and ex.calls[0][3].source_round_ids == [rid]
    assert conn.execute("SELECT action FROM autogrow_runs ORDER BY id DESC").fetchone()[0] == "started"


def test_first_round_must_be_manual(cfg, conn):
    v = _ctl(cfg, conn).evaluate(force=True)
    assert not v["can_start"] and "手动发起" in v["reason"]


def test_active_round_blocks_even_force(cfg, conn, seed):
    seed.round(status="awaiting_publish")
    v = _ctl(cfg, conn).evaluate(force=True)
    assert not v["can_start"] and "仍在进行" in v["reason"]


def test_cooldown_and_force(cfg, conn, seed):
    rid = seed.round(status="completed")
    _age_round(conn, rid, 1)
    v = _ctl(cfg, conn).evaluate()
    assert not v["can_start"] and "冷却" in v["reason"]
    assert _ctl(cfg, conn).evaluate(force=True)["can_start"]   # 立即执行跳过冷却


def test_daily_cap(cfg, conn, seed):
    rid = seed.round(status="completed")
    conn.execute("UPDATE spiral_rounds SET trigger_source='auto' WHERE id = ?", (rid,))
    conn.commit()
    _age_round(conn, rid, 30)
    conn.execute("UPDATE spiral_rounds SET created_at = datetime('now','localtime') WHERE id = ?", (rid,))
    conn.commit()
    cfg.autogrow.cooldown_hours = 0
    v = _ctl(cfg, conn).evaluate()
    assert not v["can_start"] and "每日上限" in v["reason"]


def test_consecutive_failures_stop(cfg, conn, seed):
    seed.round(status="completed")
    for _ in range(cfg.autogrow.max_consecutive_failures):
        seed.round(status="failed")
    v = _ctl(cfg, conn).evaluate(force=True)
    assert v["action"] == "stopped" and "连续" in v["reason"]


def test_goals_all_achieved_stop(cfg, conn, seed):
    seed.round(status="completed")
    conn.execute(
        "INSERT INTO goals (id, goal_text, target_metric, target_value, status) "
        "VALUES ('g1', '涨粉', 'new_followers', 100, 'completed')"
    )
    conn.commit()
    v = _ctl(cfg, conn).evaluate(force=True)
    assert v["action"] == "stopped" and "目标已达成" in v["reason"]


def test_disabled(cfg, conn):
    cfg.autogrow.enabled = False
    assert _ctl(cfg, conn).evaluate()["reason"] == "自动闭环已关闭"


def test_autogrow_config_patchable(cfg, conn, tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.config_store.OVERRIDES_FILE", tmp_path / "ov.json")
    patch_config(cfg, conn, {"autogrow.cooldown_hours": 6, "autogrow.enabled": False})
    assert cfg.autogrow.cooldown_hours == 6 and cfg.autogrow.enabled is False
