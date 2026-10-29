from __future__ import annotations

import io

import pytest
from PIL import Image

from src.core.media_generation import ImageGenerationError, validate_image_bytes
from src.core.media_quality import score_image


def _png(size=(900, 1200), color=(40, 110, 180)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_quality_score_is_bounded_and_explainable() -> None:
    result = score_image(_png())
    assert 0 <= result["score"] <= 100
    assert result["method"] == "deterministic_image_heuristics_v1"
    assert result["metrics"]["width"] == 900
    assert "reasons" in result
    assert "disclaimer" in result


def test_image_validation_returns_verified_dimensions() -> None:
    mime, suffix, width, height = validate_image_bytes(_png((640, 960)))
    assert (mime, suffix, width, height) == ("image/png", ".png", 640, 960)


def test_image_validation_rejects_non_image_bytes() -> None:
    with pytest.raises(ImageGenerationError):
        validate_image_bytes(b"not an image")


def test_generation_requires_server_side_key(monkeypatch) -> None:
    monkeypatch.setenv("ITERWAVE_IMAGE_API_KEY_ENV", "TEST_IMAGE_KEY")
    monkeypatch.delenv("TEST_IMAGE_KEY", raising=False)
    from src.core.media_generation import generate_image_bytes
    with pytest.raises(ImageGenerationError, match="TEST_IMAGE_KEY"):
        generate_image_bytes("a useful image prompt", model="test-model", size="1024x1024")
