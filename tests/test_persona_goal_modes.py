"""多平台画像 + 目标模式（unified / per_platform / hybrid）。"""

from __future__ import annotations

import pytest

from src.core.config_store import ConfigPatchError, patch_config
from src.core.goal_progress import GoalProgressUpdater
from src.core.guard.ultimate_goal_checker import CheckResult, UltimateGoalChecker
from src.core.persona import effective_persona, matching_platforms, personas_view


@pytest.fixture(autouse=True)
def _isolated_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.config_store.OVERRIDES_FILE", tmp_path / "ov.json")


def _setup(cfg, conn, mode="per_platform"):
    patch_config(cfg, conn, {
        "ultimate_goal.persona_keywords": ["AI教育"],
        "ultimate_goal.forbidden_directions": ["炒股"],
        "ultimate_goal.goal_mode": mode,
        "ultimate_goal.platform_personas.wechat_channels.inherit_global": False,
        "ultimate_goal.platform_personas.wechat_channels.persona_keywords": ["家庭理财"],
        "ultimate_goal.platform_personas.wechat_channels.positioning": "宝妈理财账号",
        "ultimate_goal.platform_personas.wechat_channels.extra_redlines": ["稳赚"],
        "ultimate_goal.platform_personas.xhs.persona_keywords": ["效率工具"],
        "ultimate_goal.platform_personas.xhs.role": "traffic",
    })


def test_unified_mode_ignores_platform_persona(cfg, conn):
    _setup(cfg, conn, mode="unified")
    ep = effective_persona(cfg, "wechat_channels")
    assert ep["source"] == "global"
    assert ep["persona_keywords"] == ["AI教育"]
    # 红线始终叠加平台额外红线
    assert "稳赚" in ep["redline_keywords"]


def test_per_platform_replace_and_merge(cfg, conn):
    _setup(cfg, conn)
    wc = effective_persona(cfg, "wechat_channels")
    assert wc["source"] == "platform"
    assert wc["persona_keywords"] == ["家庭理财"]
    assert wc["forbidden_directions"] == []
    assert wc["positioning"] == "宝妈理财账号"
    xhs = effective_persona(cfg, "xhs")
    assert xhs["source"] == "merged"
    assert xhs["persona_keywords"] == ["AI教育", "效率工具"]
    assert xhs["forbidden_directions"] == ["炒股"]
    assert xhs["role"] == "traffic"


def test_checker_uses_platform_persona(cfg, conn):
    _setup(cfg, conn)
    guard = UltimateGoalChecker(cfg)
    title, body = "家庭理财入门", "三步做好家庭理财预算"
    assert guard.check_content(title, body, [], platform="wechat_channels").result == CheckResult.PASS
    assert guard.check_content(title, body, [], platform="xhs").result == CheckResult.WARN
    # 平台额外红线只作用于该平台，但全局红线所有平台都有
    bad = guard.check_content("家庭理财稳赚", "稳赚方法", [], platform="wechat_channels")
    assert bad.result == CheckResult.FAIL


def test_matching_platforms(cfg, conn):
    _setup(cfg, conn)
    plats = matching_platforms(cfg, "家庭理财的三个误区", ["xhs", "wechat_channels"])
    assert plats == ["wechat_channels"]


def test_patch_validation(cfg, conn):
    with pytest.raises(ConfigPatchError):
        patch_config(cfg, conn, {"ultimate_goal.goal_mode": "whatever"})
    with pytest.raises(ConfigPatchError):
        patch_config(cfg, conn, {"ultimate_goal.platform_personas.xhs.role": "boss"})
    with pytest.raises(ConfigPatchError):
        patch_config(cfg, conn, {"ultimate_goal.platform_links": [{"source": "xhs", "target": "xhs"}]})
    res = patch_config(cfg, conn, {"ultimate_goal.platform_links": [
        {"source": "xhs", "target": "wechat_channels", "relation": "traffic", "note": "种草导流到视频号"},
    ]})
    assert res["changed"]
    view = personas_view(cfg)
    assert view["links"][0]["target"] == "wechat_channels"


def _goal(gid, level="platform", platform=None, parent=None, cur=None, target=100.0, weight=None):
    return {
        "id": gid, "goal_text": gid, "status": "active", "goal_level": level,
        "target_platform": platform, "parent_goal_id": parent, "weight": weight,
        "target_value": target, "baseline_value": 0.0, "current_value": cur, "metric_scope": "delta",
        "metric_period_start": "2026-10-01", "metric_period_end": "2026-12-31",
    }


def test_summary_modes():
    rows = [
        _goal("ns", level="north_star"),
        _goal("x", platform="xhs", parent="ns", cur=50.0, weight=3),
        _goal("w", platform="wechat_channels", parent="ns", cur=10.0, weight=1),
    ]
    hy = GoalProgressUpdater.summarize(rows, mode="hybrid")
    assert hy["method"] == "weighted_children"
    assert hy["percent"] == 40.0  # (50*3 + 10*1) / 4
    assert hy["by_platform"]["xhs"]["percent"] == 50.0
    rows[0]["current_value"] = 70.0
    hy2 = GoalProgressUpdater.summarize(rows, mode="hybrid")
    assert hy2["method"] == "north_star_own" and hy2["percent"] == 70.0
    pp = GoalProgressUpdater.summarize(rows, mode="per_platform")
    assert pp["percent"] == 30.0
    un = GoalProgressUpdater.summarize(rows, mode="unified")
    assert un["percent"] == pytest.approx(43.3, abs=0.1)
