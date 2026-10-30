"""Runtime policy settings remain bounded and are honored by execution paths."""
from __future__ import annotations

import io

import pytest
from PIL import Image

from src.core.config_store import ConfigNotAllowedError, ConfigPatchError, SCALAR_RULES, _validate, config_metadata_view, current_config_view, patch_config
from src.core.media_generation import ImageGenerationError, validate_image_bytes
from src.core.media_quality import score_image
from src.core.tenant_backup import MAX_BACKUP_BYTES, _backup_limits


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_runtime_policy_defaults_preserve_previous_behavior(cfg):
    assert cfg.vision_ranking.timeout_seconds == 25
    assert cfg.media_generation.timeout_seconds == 150
    assert cfg.media_generation.connect_timeout_seconds == 15
    assert cfg.media_generation.max_pixels == 40_000_000
    assert cfg.tenant_switch.round_drain_timeout_seconds == 120
    assert cfg.tenant_backup.retention_count == 10
    assert cfg.hot_topic_matcher.batch_size == 20
    assert cfg.scoring.history_normalization_scale == 2000
    assert cfg.scoring.freshness_window_days == 30


def test_runtime_policy_patch_cannot_relax_hard_limits():
    with pytest.raises(ConfigPatchError):
        _validate("media_generation.max_pixels", 40_000_001)
    with pytest.raises(ConfigPatchError):
        _validate("media_generation.max_dimension", 20_001)
    with pytest.raises(ConfigPatchError):
        _validate("tenant_backup.max_bytes", MAX_BACKUP_BYTES + 1)
    with pytest.raises(ConfigPatchError):
        _validate("tenant_switch.request_drain_timeout_seconds", 0)
    with pytest.raises(ConfigPatchError):
        _validate("tenant_backup.retention_count", 4.5)
    with pytest.raises(ConfigPatchError):
        _validate("scoring.stable_exploration_percent", 51)


def test_image_generation_obeys_lower_configured_pixel_limit(cfg):
    raw = _png(1100, 1100)
    policy = cfg.model_copy(update={
        "media_generation": cfg.media_generation.model_copy(update={"max_pixels": 1_000_000})
    })
    with pytest.raises(ImageGenerationError, match="尺寸超出"):
        validate_image_bytes(raw, cfg=policy)


def test_media_quality_uses_configured_component_weight(cfg):
    raw = _png(1000, 1500)
    policy = cfg.model_copy(update={
        "media_quality": cfg.media_quality.model_copy(update={"resolution_weight": 10.0})
    })
    result = score_image(raw, cfg=policy)
    assert result["components"]["resolution"] <= 10.0


def test_backup_policy_can_tighten_but_not_raise_hard_cap(cfg):
    policy = cfg.model_copy(update={
        "tenant_backup": cfg.tenant_backup.model_copy(update={
            "max_bytes": 1_048_576, "retention_count": 4,
        })
    })
    assert _backup_limits(policy) == (1_048_576, 4)
    assert _backup_limits(None) == (MAX_BACKUP_BYTES, 10)


def test_metadata_does_not_include_provider_endpoint_or_account_identity(cfg):
    from src.core.config_store import config_metadata_view
    keys = {item["key"] for item in config_metadata_view(cfg)}
    assert "vision_ranking.base_url" not in keys
    assert "media_generation.base_url" not in keys
    assert not any(key.endswith(".account_key") for key in keys)



def test_new_runtime_policy_fields_are_whitelisted_and_visible(cfg):
    keys = {item["key"] for item in config_metadata_view(cfg)}
    expected = {
        "hot_topics.max_items_per_source",
        "hot_topics.fetch_timeout_seconds",
        "hot_topics.max_concurrent_fetches",
        "hot_topics.trend_burst_multiplier",
        "hot_topics.trend_cooled_ratio",
        "hot_topics.trend_sustained_ratio",
        "hot_topics.trend_previous_drop_ratio",
        "hot_topics.trend_min_seen_count",
        "hot_topics.dedup.cross_platform_similarity",
        "hot_topics.dedup.length_prefilter_ratio",
        "hot_topic_matcher.llm_temperature",
        "media_quality.composition_near_square_multiplier",
        "media_quality.composition_wide_multiplier",
        "media_quality.composition_extreme_multiplier",
        "media_quality.sharpness_fallback_contrast_reference",
        "media_quality.sampling_size",
        "media_quality.color_quantization_colors",
        "autogrow.recency_decay",
        "autogrow.full_confidence_samples",
        "autogrow.coverage_gap_bias_ratio",
        "autogrow.validation_gap_bias_ratio",
        "autogrow.min_dimension_bias",
        "autogrow.max_reference_titles_per_platform",
        "autogrow.max_copy_hints_per_platform",
        "autogrow.max_copy_hint_chars",
        "copy_generation.max_guard_retries",
        "copy_generation.title_max_chars",
        "scheduler.misfire_grace_minutes",
        "timeouts.metrics_retry_attempts",
        "timeouts.metrics_retry_interval_minutes",
        "timeouts.login_timeout_seconds",
        "timeouts.login_export_timeout_seconds",
        "timeouts.recovery_retry_delay_minutes",
        "topic_kb.remote_timeout_seconds",
        "scoring.active_dimension_min_uses",
        "scoring.active_dimension_window_days",
        "scoring.shrinkage_m",
        "scoring.stable_exploration_percent",
        "scoring.normalization_window_days",
        "scoring.timeliness_half_life_days.news",
        "scoring.timeliness_half_life_days.evergreen",
        "rhythm.intervals.passive_reply_base_seconds",
        "insights.window_days",
        "plan_suggestion.default_topic_count",
        "spiral.min_dimension_samples",
        "spiral.validation_gap_topic_min",
        "spiral.validation_gap_topic_max",
        "spiral.coverage_gap_ratio",
        "spiral.low_interaction_rate_threshold",
        "platforms.xhs.enabled",
        "topic_kb.field_mapping",
        "ultimate_goal.goal_mode",
        "notifications.events.draft_saved.enabled",
    }
    assert expected <= keys


def test_hot_topic_and_copy_policy_cross_field_validation(cfg, conn):
    with pytest.raises(ConfigPatchError, match="热点趋势阈值"):
        patch_config(cfg, conn, {
            "hot_topics.trend_cooled_ratio": 0.95,
            "hot_topics.trend_sustained_ratio": 0.9,
        })
    with pytest.raises(ConfigPatchError, match="正文长度下限"):
        patch_config(cfg, conn, {
            "copy_generation.body_min_chars": 900,
            "copy_generation.body_max_chars": 800,
        })
    with pytest.raises(ConfigPatchError, match="标签数量下限"):
        patch_config(cfg, conn, {
            "copy_generation.tag_min_count": 6,
            "copy_generation.tag_max_count": 5,
        })


def test_hot_topic_trend_policy_is_configurable():
    from src.core.config import HotTopicsConfig
    from src.core.hot_topics import _trend_of

    item = {"heat": 50.0, "heat_prev": 100.0, "heat_peak": 100.0, "seen_count": 3}
    policy = HotTopicsConfig(
        trend_cooled_ratio=0.4,
        trend_sustained_ratio=0.8,
        trend_previous_drop_ratio=0.4,
        trend_min_seen_count=3,
    )
    assert _trend_of(item, 100.0, policy)[0] == "平稳"


def test_copy_hint_block_uses_configured_limits():
    from src.core.config import CopyGenerationConfig
    from src.core.content_pipeline import build_hint_block

    policy = CopyGenerationConfig(strategy_hint_max_count=1, strategy_hint_max_chars=20)
    block = build_hint_block(["abcdefghijklmnopqrstuvwx", "second hint"], policy)
    assert "abcdefghijklmnopqrst" in block
    assert "second hint" not in block


def test_metrics_retry_settings_are_typed_and_bounded():
    assert _validate("timeouts.metrics_retry_attempts", 4) == 4
    assert _validate("timeouts.metrics_retry_interval_minutes", 45) == 45
    with pytest.raises(ConfigPatchError):
        _validate("timeouts.metrics_retry_attempts", 0)



def test_internal_platform_identity_and_endpoints_are_not_returned_or_patchable(cfg, conn):
    view = current_config_view(cfg, conn)
    assert set(view["readonly"]["platforms"]["xhs"]) == {"enabled"}
    assert "webhook_url_env" not in view["readonly"]["notification_channels"]["feishu"]
    with pytest.raises(ConfigNotAllowedError):
        patch_config(cfg, conn, {"platforms.xhs.account_key": "xhs_other"})
    with pytest.raises(ConfigNotAllowedError):
        patch_config(cfg, conn, {"platforms.xhs.base_url": "https://internal.example"})
    with pytest.raises(ConfigNotAllowedError):
        patch_config(cfg, conn, {"media_generation.base_url": "https://internal.example"})
    with pytest.raises(ConfigNotAllowedError):
        patch_config(cfg, conn, {"notifications.channels.feishu.webhook_url_env": "OTHER_SECRET_ENV"})



def test_notification_event_switch_is_a_tenant_runtime_setting(cfg, conn):
    patch_config(cfg, conn, {"notifications.events.draft_saved.enabled": False})
    event = next(item for item in cfg.notifications.events if item.name == "draft_saved")
    assert event.enabled is False
    assert _validate("notifications.events.round_timeout.enabled", True) is True
    with pytest.raises(ConfigNotAllowedError):
        _validate("notifications.events.unknown_event.enabled", False)



def test_spiral_ops_suggestions_use_configured_thresholds(cfg, conn):
    from src.core.spiral import SpiralEngine

    cfg.spiral.min_metrics_for_ops_adjustment = 2
    cfg.spiral.min_rounds_for_systematic_adjustment = 9
    engine = SpiralEngine(cfg, conn)
    assert "≥9 轮" in engine._suggest_ops([{}, {}])[0]



def test_every_scalar_whitelist_field_has_config_metadata(cfg):
    metadata_keys = {item["key"] for item in config_metadata_view(cfg)}
    assert set(SCALAR_RULES) <= metadata_keys



def test_topic_kb_path_is_not_runtime_editable_or_exposed(cfg, conn):
    from src.core.config_store import apply_overrides

    original_path = cfg.topic_kb.path
    assert "topic_kb.path" not in {item["key"] for item in config_metadata_view(cfg)}
    assert current_config_view(cfg, conn)["readonly"]["kb_path"] == "由内容资产中的选题库来源管理"
    assert "llm_base_url" not in current_config_view(cfg, conn)["readonly"]
    with pytest.raises(ConfigNotAllowedError):
        patch_config(cfg, conn, {"topic_kb.path": "/tmp/evil_topics.csv"})
    assert apply_overrides(cfg, {"topic_kb.path": "/tmp/evil_topics.csv"}) == []
    assert cfg.topic_kb.path == original_path



def test_feedback_bias_metadata_is_numeric_float(cfg):
    item = next(item for item in config_metadata_view(cfg) if item["key"] == "autogrow.feedback_max_bias")
    assert item["type"] == "number"
    assert item["value"] == 15.0
    assert _validate("autogrow.feedback_max_bias", 15.5) == 15.5


def test_request_validation_messages_are_localized():
    from src.api.main import _friendly_validation_messages

    messages = _friendly_validation_messages([
        {"loc": ("body", "platform"), "type": "string_pattern_mismatch"},
    ])
    assert messages == ["平台标识：格式不符合要求"]
    assert "String should match pattern" not in messages[0]



def test_config_api_hides_knowledge_base_path_from_history(cfg, conn):
    from src.core.config_store import _public_overrides

    assert "topic_kb.path" not in _public_overrides({"topic_kb.path": "/workspace/tenants/ta/kb/topics.csv"})
    conn.execute(
        "INSERT INTO config_changes (key, old_value, new_value, changed_at) VALUES (?, ?, ?, ?)",
        ("topic_kb.path", '"/workspace/tenants/ta/kb/topics.csv"', '"/tmp/evil_topics.csv"', "2026-10-30T10:00:00"),
    )
    conn.commit()
    audit = current_config_view(cfg, conn)["audit"][0]
    assert audit["old_value"] == "已隐藏"
    assert audit["new_value"] == "已隐藏"


def test_api_validation_response_is_chinese(test_auth):
    from fastapi.testclient import TestClient
    from src.api import main as api_main

    client = test_auth(TestClient(api_main._build_app(), base_url="http://127.0.0.1"))
    response = client.post("/api/platform-accounts", json={
        "platform": "not-a-platform", "account_handle": "test-account",
    })
    assert response.status_code == 422
    assert "平台标识" in response.json()["detail"][0]
