"""Runtime policy settings remain bounded and are honored by execution paths."""
from __future__ import annotations

import io

import pytest
from PIL import Image

from src.core.config_store import ConfigPatchError, _validate
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
