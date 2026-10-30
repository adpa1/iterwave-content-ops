"""Initialize tenant-owned business configuration after the tenant DB is ready."""
from __future__ import annotations


def initialize_tenant_business_data(conn, cfg) -> None:
    from src.core.ops_targets_repository import load_ops_targets, seed_from_yaml
    from src.core.industry_presets_repository import seed_builtins

    seed_from_yaml(conn, cfg.ops_targets)
    cfg.ops_targets = load_ops_targets(conn)
    seed_builtins(conn)
