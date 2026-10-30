from __future__ import annotations

import pytest

from src.adapters.base import Platform
from src.core import content_pipeline


def test_generate_copy_content_without_model_fails_closed(cfg, monkeypatch):
    cfg.llm.enabled = False
    monkeypatch.delenv(cfg.llm.api_key_env, raising=False)

    def forbidden_template(*args, **kwargs):
        raise AssertionError("正式生成不得调用模板降级")

    monkeypatch.setattr(content_pipeline, "_generate_via_template", forbidden_template)

    with pytest.raises(content_pipeline.ContentGenerationError) as caught:
        content_pipeline.generate_copy_content(
            "测试选题", "测试摘要", "个人成长", Platform.XHS, cfg=cfg,
        )

    assert caught.value.code == "model_not_configured"
    assert caught.value.retryable is False


def test_generate_copy_content_model_exception_is_reported_not_template(cfg, monkeypatch):
    cfg.llm.enabled = True
    monkeypatch.setenv(cfg.llm.api_key_env, "test-only-key")
    monkeypatch.setattr(content_pipeline, "_llm_available", lambda _cfg: True)

    def failed_model(*args, **kwargs):
        raise RuntimeError("provider internal detail")

    def forbidden_template(*args, **kwargs):
        raise AssertionError("模型异常不得调用模板降级")

    monkeypatch.setattr(content_pipeline, "_generate_via_llm", failed_model)
    monkeypatch.setattr(content_pipeline, "_generate_via_template", forbidden_template)

    with pytest.raises(content_pipeline.ContentGenerationError) as caught:
        content_pipeline.generate_copy_content(
            "测试选题", "测试摘要", "个人成长", Platform.XHS, cfg=cfg,
        )

    assert caught.value.code == "model_unavailable"
    assert caught.value.retryable is True
    assert "provider internal detail" not in str(caught.value)
