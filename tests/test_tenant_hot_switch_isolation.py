"""HOT-001..HOT-015 style isolation checks using real SQLite tenant files."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import src.api.main as api_main
import src.core.db as db_module
from src.core.tenant_registry import TenantRegistry


class FakePlatformConfig:
    def __init__(self, account_key="xhs"):
        self.account_key = account_key


class FakePlatforms:
    def __init__(self):
        self.xhs = FakePlatformConfig()

    def model_dump(self):
        return {"xhs": {}}


class FakeConfig:
    def __init__(self, root: Path):
        self.paths = SimpleNamespace(
            data_dir=str(root / "data"),
            db_file=str(root / "data" / "easel.db"),
            backup_dir=str(root / "backups"),
            output_dir=str(root / "data" / "outputs"),
            screenshot_dir=str(root / "data" / "screenshots"),
            log_dir=str(root / "data" / "logs"),
        )
        self.topic_kb = SimpleNamespace(path=str(root / "data" / "kb" / "topics.csv"))
        self.platforms = FakePlatforms()
        self.llm = SimpleNamespace(model_dump=lambda: {})

    def resolve_path(self, value):
        path = Path(value)
        return str(path if path.is_absolute() else Path.cwd() / path)


class FakeDatabase:
    def __init__(self, cfg):
        self.path = Path(cfg.resolve_path(cfg.paths.db_file))

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS isolation_probe (value TEXT NOT NULL)")

    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


class FakeScheduler:
    def __init__(self, cfg, db_path):
        self.cfg = cfg
        self.db_path = Path(db_path)
        self.autogrow = None
        self.wired = False

    def wire(self, **kwargs):
        self.wired = True

    def shutdown(self):
        return None


class FakeRecovery:
    def __init__(self, cfg, conn, scheduler):
        self.conn = conn

    def recover(self):
        return {"interrupted_tasks": 0}


class FakePlanVersionManager:
    def __init__(self, cfg, conn):
        self.conn = conn

    def invalidate_changed_plans(self):
        return {"stale_rounds": [], "current_signature": "test"}


class FakeAutogrow:
    def __init__(self, *args, **kwargs):
        pass


class NoopComponent:
    def __init__(self, *args, **kwargs):
        pass


def test_runtime_builder_uses_distinct_real_sqlite_files_per_tenant(monkeypatch, tmp_path):
    registry = TenantRegistry(tmp_path)
    registry.initialize()
    registry.create("brand_a", "品牌 A")
    registry.create("brand_b", "品牌 B")

    monkeypatch.setattr(api_main, "load_config", lambda: FakeConfig(tmp_path))
    monkeypatch.setattr(api_main, "load_overrides", lambda cfg: {})
    monkeypatch.setattr(api_main, "apply_overrides", lambda cfg, overrides: [])
    from src.core import system_config_repository
    monkeypatch.setattr(
        system_config_repository, "migrate_tenant_llm_settings",
        lambda *args, **kwargs: {"purged": 0, "conflicts": 0, "applied": 0},
    )
    monkeypatch.setattr(api_main, "Database", FakeDatabase)
    monkeypatch.setattr(db_module, "LockedConnection", lambda conn: conn)
    monkeypatch.setattr(api_main, "get_adapters", lambda cfg, conn: {})
    monkeypatch.setattr(api_main, "SchedulerEngine", FakeScheduler)
    monkeypatch.setattr(api_main, "RecoveryManager", FakeRecovery)
    monkeypatch.setattr(api_main, "PlanVersionManager", FakePlanVersionManager)
    monkeypatch.setattr(api_main, "CircuitBreakerEngine", NoopComponent)
    monkeypatch.setattr(api_main, "RhythmController", NoopComponent)
    monkeypatch.setattr(api_main, "RoundStateMachine", NoopComponent)
    monkeypatch.setattr(api_main, "DraftStateMachine", NoopComponent)
    monkeypatch.setattr(api_main, "ScoringEngine", NoopComponent)
    monkeypatch.setattr(api_main, "UltimateGoalChecker", NoopComponent)
    monkeypatch.setattr(api_main, "Normalizer", NoopComponent)
    monkeypatch.setattr(api_main, "SpiralEngine", NoopComponent)
    monkeypatch.setattr(api_main, "OperationsEngine", NoopComponent)
    monkeypatch.setattr(api_main, "Notifier", NoopComponent)
    monkeypatch.setattr(api_main, "RoundExecutor", NoopComponent)
    monkeypatch.setattr(api_main, "GoalProgressUpdater", NoopComponent)
    monkeypatch.setattr(api_main, "PlatformInsightsEngine", NoopComponent)
    monkeypatch.setattr("src.core.autogrow.AutogrowController", FakeAutogrow)

    ctx_a = api_main._build_tenant_runtime_context(registry, "brand_a")
    ctx_b = api_main._build_tenant_runtime_context(registry, "brand_b")
    try:
        path_a = Path(ctx_a["cfg"].resolve_path(ctx_a["cfg"].paths.db_file))
        path_b = Path(ctx_b["cfg"].resolve_path(ctx_b["cfg"].paths.db_file))
        assert path_a != path_b
        assert path_a.is_relative_to(tmp_path / "tenants" / "brand_a")
        assert path_b.is_relative_to(tmp_path / "tenants" / "brand_b")
        assert ctx_a["cfg"].platforms.xhs.account_key == "brand_a__xhs"
        assert ctx_b["cfg"].platforms.xhs.account_key == "brand_b__xhs"

        ctx_a["conn"].execute("INSERT INTO isolation_probe(value) VALUES (?)", ("A-only",))
        ctx_a["conn"].commit()
        ctx_b["conn"].execute("INSERT INTO isolation_probe(value) VALUES (?)", ("B-only",))
        ctx_b["conn"].commit()
        values_a = [row["value"] for row in ctx_a["conn"].execute("SELECT value FROM isolation_probe")]
        values_b = [row["value"] for row in ctx_b["conn"].execute("SELECT value FROM isolation_probe")]
        assert values_a == ["A-only"]
        assert values_b == ["B-only"]
        assert ctx_a["scheduler"].db_path == path_a
        assert ctx_b["scheduler"].db_path == path_b
    finally:
        ctx_a["conn"].close()
        ctx_b["conn"].close()
