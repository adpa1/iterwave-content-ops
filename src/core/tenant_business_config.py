"""Initialize tenant-owned business configuration after the tenant DB is ready."""
from __future__ import annotations


def initialize_tenant_business_data(conn, cfg) -> None:
    # Test doubles / pre-migration connections may not expose the 031+ tables.
    # A real Database.initialize() applies migrations before this helper is called.
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tenant_setting_migrations'"
    ).fetchone()
    if row is None:
        return

    from src.core.ops_targets_repository import load_ops_targets, seed_from_yaml
    from src.core.industry_presets_repository import seed_builtins

    seed_from_yaml(conn, getattr(cfg, "ops_targets", {}) or {})
    if hasattr(cfg, "ops_targets"):
        cfg.ops_targets = load_ops_targets(conn)
    seed_builtins(conn)
