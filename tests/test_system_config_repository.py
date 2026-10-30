from __future__ import annotations

import json

import pytest

from src.core.config_repository import read_settings, save_settings
from src.core.config_store import apply_overrides, config_metadata_view, patch_config
from src.core.system_config_repository import (
    migrate_tenant_llm_settings,
    read_system_llm_settings,
)


def test_llm_config_metadata_is_system_scoped(cfg):
    items = {item["key"]: item for item in config_metadata_view(cfg)}
    assert items["llm.model"]["tenant_scoped"] is False
    assert items["llm.enabled"]["tenant_scoped"] is False
    assert items["timeouts.round_timeout_hours"]["tenant_scoped"] is True


def test_llm_patch_persists_to_system_db_not_tenant_db(cfg, conn, tmp_path):
    system_db = tmp_path / "shared" / "system.db"
    result = patch_config(
        cfg, conn, {"llm.model": "shared-model-v2"},
        system_db_file=system_db,
    )

    assert result["changed"][0]["key"] == "llm.model"
    assert cfg.llm.model == "shared-model-v2"
    assert "llm.model" not in read_settings(conn)
    assert read_system_llm_settings(system_db)["llm.model"] == "shared-model-v2"


def test_llm_patch_rejects_mixed_system_and_tenant_updates(cfg, conn, tmp_path):
    with pytest.raises(ValueError, match="必须分开保存"):
        patch_config(
            cfg, conn,
            {"llm.model": "shared-model", "timeouts.round_timeout_hours": 36},
            system_db_file=tmp_path / "shared" / "system.db",
        )
    assert read_settings(conn) == {}
    assert read_system_llm_settings(tmp_path / "shared" / "system.db") == {}


def test_first_active_tenant_llm_config_becomes_shared_and_is_purged(cfg, conn, tmp_path):
    system_db = tmp_path / "shared" / "system.db"
    base_llm = cfg.llm.model_dump()
    save_settings(conn, {
        "llm.provider": "ark",
        "llm.model": "tenant-a-model",
        "llm.fallback_model": "tenant-a-fallback",
    })
    apply_overrides(cfg, read_settings(conn))

    result = migrate_tenant_llm_settings(
        cfg, conn, system_db, tenant_id="tenant-a", base_llm=base_llm,
    )

    shared = read_system_llm_settings(system_db)
    assert shared["llm.model"] == "tenant-a-model"
    assert shared["llm.fallback_model"] == "tenant-a-fallback"
    assert cfg.llm.model == "tenant-a-model"
    assert not any(key.startswith("llm.") for key in read_settings(conn))
    assert result["purged"] == 3
    assert result["conflicts"] == 0


def test_other_tenant_legacy_llm_conflict_is_recorded_and_shared_value_wins(cfg, conn, tmp_path):
    system_db = tmp_path / "shared" / "system.db"
    save_settings(conn, {"llm.model": "tenant-a-model"})
    apply_overrides(cfg, read_settings(conn))
    migrate_tenant_llm_settings(
        cfg, conn, system_db, tenant_id="tenant-a", base_llm=cfg.llm.model_dump(),
    )

    save_settings(conn, {"llm.model": "tenant-b-model"})
    tenant_b_cfg = cfg.model_copy(deep=True)
    tenant_b_cfg.llm.model = "yaml-default"
    result = migrate_tenant_llm_settings(
        tenant_b_cfg, conn, system_db, tenant_id="tenant-b",
        base_llm=tenant_b_cfg.llm.model_dump(),
    )

    assert tenant_b_cfg.llm.model == "tenant-a-model"
    assert result["conflicts"] == 1
    assert read_settings(conn) == {}
    audit = __import__("sqlite3").connect(system_db)
    try:
        row = audit.execute(
            "SELECT legacy_value_json,selected_value_json FROM system_config_migration_conflicts "
            "WHERE tenant_id='tenant-b' AND key='llm.model'"
        ).fetchone()
        assert json.loads(row[0]) == "tenant-b-model"
        assert json.loads(row[1]) == "tenant-a-model"
    finally:
        audit.close()
