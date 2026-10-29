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


def test_corrupt_registry_does_not_fall_back_to_default(tmp_path: Path) -> None:
    registry = TenantRegistry(tmp_path)
    registry.initialize()
    registry.registry_file.write_text("{broken", encoding="utf-8")
    with pytest.raises(TenantError, match="拒绝自动回退"):
        registry.active_id()


def test_active_file_unknown_tenant_does_not_fall_back(tmp_path: Path) -> None:
    registry = TenantRegistry(tmp_path)
    registry.initialize()
    registry.active_file.write_text(json.dumps({"tenant_id": "missing"}), encoding="utf-8")
    with pytest.raises(TenantError, match="拒绝自动回退"):
        registry.active_id()


def test_registry_atomic_writes_use_unique_temp_files_and_private_permissions(tmp_path: Path) -> None:
    registry = TenantRegistry(tmp_path)
    registry.initialize()
    registry.create("brand_a", "品牌 A")
    registry.select_active("brand_a")

    assert json.loads(registry.registry_file.read_text(encoding="utf-8"))["tenants"]
    assert json.loads(registry.active_file.read_text(encoding="utf-8"))["tenant_id"] == "brand_a"
    assert registry.registry_file.stat().st_mode & 0o777 == 0o600
    assert registry.active_file.stat().st_mode & 0o777 == 0o600
    assert not list((tmp_path / "shared").glob("*.tmp"))
