"""Tenant business configuration persistence tests."""
from __future__ import annotations

from src.core.industry_presets_repository import (
    get_preset, list_presets, seed_builtins, upsert_custom_preset,
)
from src.core.ops_targets_repository import (
    load_ops_targets, read_targets, replace_targets, seed_from_yaml,
)


def test_ops_targets_yaml_seed_is_idempotent(conn):
    configured = {
        "xhs": {
            "active_comments": [{"url": "https://example.com/user/1", "content": "有依据的评论"}],
            "follows": ["user_123", "https://example.com/user/2"],
        },
        "douyin": {"active_comments": [], "follows": []},
    }
    first = seed_from_yaml(conn, configured)
    second = seed_from_yaml(conn, configured)
    assert first == {"imported": 3}
    assert second == {"imported": 0}
    assert read_targets(conn)["xhs"] == {
        "active_comments": [{"url": "https://example.com/user/1", "content": "有依据的评论"}],
        "follows": ["user_123", "https://example.com/user/2"],
    }
    assert len(load_ops_targets(conn)["xhs"].active_comments) == 1


def test_ops_targets_replace_is_atomic_and_rejects_unsafe_url(conn):
    import pytest
    replace_targets(conn, {"xhs": {"follows": ["user_1"]}})
    with pytest.raises(ValueError):
        replace_targets(conn, {
            "xhs": {"active_comments": [{"url": "https://user:pass@example.com", "content": "comment"}]}
        })
    assert read_targets(conn)["xhs"]["follows"] == ["user_1"]


def test_ops_targets_empty_replace_does_not_reseed_yaml(conn):
    seed_from_yaml(conn, {"xhs": {"follows": ["legacy_user"]}})
    replace_targets(conn, {})
    assert read_targets(conn)["xhs"]["follows"] == []


def test_industry_presets_seed_once_and_read_from_sqlite(conn):
    first = seed_builtins(conn)
    second = seed_builtins(conn)
    assert first["imported"] > 0
    assert second == {"imported": 0}
    items = list_presets(conn)
    assert len(items) == first["imported"]
    assert get_preset(conn, "general")["is_builtin"] is True


def test_custom_industry_preset_can_be_versioned(conn):
    item = {
        "key": "custom_ai", "name": "AI工具", "description": "自定义模板",
        "positioning": "分享可验证的AI工具经验",
        "persona_keywords": ["AI工具"], "forbidden_directions": ["夸大效果"],
        "extra_redlines": [], "dimensions": ["工具测评"],
        "platform_notes": {"xhs": "说明真实体验"},
    }
    first = upsert_custom_preset(conn, item)
    second = upsert_custom_preset(conn, {**item, "name": "AI工具与效率"})
    assert first["version"] == 1
    assert second["version"] == 2
    assert second["name"] == "AI工具与效率"
