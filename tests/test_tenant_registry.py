from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.tenant_registry import TenantError, TenantRegistry


def test_initialize_preserves_default_paths_and_creates_shared_raw(tmp_path: Path) -> None:
    registry = TenantRegistry(tmp_path)
    registry.initialize()

    paths = registry.paths("default")
    assert paths.db_file == (tmp_path / "data" / "easel.db")
    assert paths.backup_dir == (tmp_path / "backups")
    assert (tmp_path / "shared" / "raw").is_dir()
    assert registry.active_id() == "default"


def test_tenant_data_paths_are_separate(tmp_path: Path) -> None:
    registry = TenantRegistry(tmp_path)
    registry.initialize()
    a = registry.create("brand_a", "品牌 A")
    b = registry.create("brand_b", "品牌 B")

    assert a.db_file != b.db_file
    assert a.media_dir != b.media_dir
    assert a.profile_dir != b.profile_dir
    assert a.db_file.is_relative_to(tmp_path / "tenants" / "brand_a")
    assert b.db_file.is_relative_to(tmp_path / "tenants" / "brand_b")


def test_active_tenant_switch_and_invalid_selection_fails_closed(tmp_path: Path) -> None:
    registry = TenantRegistry(tmp_path)
    registry.initialize()
    registry.create("brand_a", "品牌 A")

    registry.select_active("brand_a")
    assert registry.active_id() == "brand_a"
    with pytest.raises(TenantError, match="不存在"):
        registry.select_active("missing")
    assert registry.active_id() == "brand_a"


@pytest.mark.parametrize("tenant_id", ["../escape", "UPPER", "bad/name", "", "-bad", "a" * 49])
def test_invalid_tenant_ids_are_rejected(tmp_path: Path, tenant_id: str) -> None:
    registry = TenantRegistry(tmp_path)
    registry.initialize()
    with pytest.raises(TenantError):
        registry.create(tenant_id, "Invalid")


def test_corrupt_legacy_registry_is_ignored_after_successful_db_migration(tmp_path: Path) -> None:
    registry = TenantRegistry(tmp_path)
    registry.initialize()
    registry.registry_file.write_text("{broken", encoding="utf-8")
    assert registry.active_id() == "default"


def test_corrupt_legacy_active_file_is_ignored_after_successful_db_migration(tmp_path: Path) -> None:
    registry = TenantRegistry(tmp_path)
    registry.initialize()
    registry.active_file.write_text(json.dumps({"tenant_id": "missing"}), encoding="utf-8")
    assert registry.active_id() == "default"


def test_system_database_is_private_and_json_is_not_written_after_migration(tmp_path: Path) -> None:
    registry = TenantRegistry(tmp_path)
    registry.initialize()
    registry.create("brand_a", "品牌 A")
    registry.select_active("brand_a")

    assert registry.system_db_file.exists()
    assert registry.system_db_file.stat().st_mode & 0o777 == 0o600
    assert registry.active_id() == "brand_a"
    assert not registry.registry_file.exists()
    assert not registry.active_file.exists()
    assert not list((tmp_path / "shared").glob("*.tmp"))


def test_existing_json_registry_and_active_state_are_imported_once(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    registry_file = shared / "tenant_registry.json"
    active_file = shared / "active_tenant.json"
    registry_file.write_text(json.dumps({
        "version": 1,
        "tenants": [
            {"id": "default", "name": "默认账号组", "created_at": "2026-01-01T00:00:00"},
            {"id": "brand_a", "name": "品牌 A", "created_at": "2026-01-02T00:00:00"},
        ],
    }), encoding="utf-8")
    active_file.write_text(json.dumps({"tenant_id": "brand_a"}), encoding="utf-8")

    registry = TenantRegistry(tmp_path)
    registry.initialize()
    assert registry.active_id() == "brand_a"
    assert registry.get("brand_a")["name"] == "品牌 A"
    assert registry.registry_file.stat().st_mode & 0o777 == 0o400
    assert registry.active_file.stat().st_mode & 0o777 == 0o400

    # Once imported, legacy files are no longer authoritative.
    registry.registry_file.chmod(0o600)
    registry.active_file.chmod(0o600)
    registry.registry_file.write_text("{broken", encoding="utf-8")
    registry.active_file.write_text(json.dumps({"tenant_id": "missing"}), encoding="utf-8")
    registry.initialize()
    assert registry.active_id() == "brand_a"
