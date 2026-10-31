from __future__ import annotations

from src.api.main import _reset_tenant_business_defaults
from src.core.task_snapshot import config_fingerprint


def test_startup_and_worker_tenant_defaults_produce_identical_fingerprints(cfg):
    """A non-default tenant must use the same baseline in lifespan and Worker contexts."""
    startup_cfg = cfg.model_copy(deep=True)
    worker_cfg = cfg.model_copy(deep=True)

    _reset_tenant_business_defaults(startup_cfg, "tga")
    _reset_tenant_business_defaults(worker_cfg, "tga")

    assert startup_cfg.ultimate_goal.persona_keywords == []
    assert startup_cfg.ultimate_goal.positioning == ""
    assert startup_cfg.ultimate_goal.forbidden_directions == []
    assert config_fingerprint(startup_cfg) == config_fingerprint(worker_cfg)


def test_default_tenant_keeps_deployment_business_defaults(cfg):
    original = config_fingerprint(cfg)

    _reset_tenant_business_defaults(cfg, "default")

    assert config_fingerprint(cfg) == original
