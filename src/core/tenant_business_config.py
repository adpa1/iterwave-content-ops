"""Initialize tenant-owned business configuration after the tenant DB is ready."""
from __future__ import annotations



def reset_new_tenant_business_defaults(cfg) -> None:
    """Prevent deployment-level account persona values leaking into a new tenant.

    Tenant-specific overrides are applied after this reset. Global safety redlines and
    infrastructure settings intentionally remain inherited from the deployment config.
    """
    goal = getattr(cfg, "ultimate_goal", None)
    if goal is None:
        return
    goal.persona_keywords = []
    goal.positioning = ""
    goal.forbidden_directions = []
    goal.goal_mode = "unified"
    goal.platform_personas = {}
    goal.platform_links = []


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
