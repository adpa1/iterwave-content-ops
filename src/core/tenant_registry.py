"""账号组注册表与隔离路径。

账号组只用于单运营者切换不同业务空间，不引入多用户登录。
default 继续使用历史 data/ 与 backups/ 路径；新账号组使用 tenants/<id>/。
公共原始采集数据统一位于 shared/raw/，业务加工结果不得写入此目录。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any

TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
DEFAULT_TENANT_ID = "default"


class TenantError(ValueError):
    """账号组标识、注册表或路径不合法。"""


@dataclass(frozen=True)
class TenantPaths:
    tenant_id: str
    name: str
    root: Path
    data_dir: Path
    db_file: Path
    backup_dir: Path
    output_dir: Path
    screenshot_dir: Path
    log_dir: Path
    profile_dir: Path
    media_dir: Path
    kb_dir: Path

    def as_dict(self) -> dict[str, str]:
        return {
            key: str(value) if isinstance(value, Path) else value
            for key, value in asdict(self).items()
        }


class TenantRegistry:
    """持久化账号组目录。注册表是共享基础设施，不承载任何业务结果。"""

    def __init__(self, project_root: Path | str):
        self.project_root = Path(project_root).resolve()
        self.shared_dir = self.project_root / "shared"
        self.raw_dir = self.shared_dir / "raw"
        self.registry_file = self.shared_dir / "tenant_registry.json"
        self.active_file = self.shared_dir / "active_tenant.json"
        self._lock = RLock()

    def initialize(self) -> None:
        self.shared_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.raw_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.shared_dir.chmod(0o700)
            self.raw_dir.chmod(0o700)
        except OSError:
            pass
        with self._lock:
            if not self.registry_file.exists():
                self._write_registry({
                    "version": 1,
                    "tenants": [{
                        "id": DEFAULT_TENANT_ID,
                        "name": "默认账号组",
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                    }],
                })
            registry = self._read_registry()
            tenants = registry.get("tenants")
            if not isinstance(tenants, list):
                raise TenantError("账号组注册表格式错误：tenants 必须是数组")
            if not any(item.get("id") == DEFAULT_TENANT_ID for item in tenants if isinstance(item, dict)):
                tenants.insert(0, {
                    "id": DEFAULT_TENANT_ID,
                    "name": "默认账号组",
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                })
                self._write_registry(registry)
            for item in tenants:
                if not isinstance(item, dict):
                    raise TenantError("账号组注册表包含无效条目")
                self._validate_id(item.get("id"))
                self.paths(item["id"], name=str(item.get("name") or item["id"]), create=True)
            if not self.active_file.exists():
                self.select_active(DEFAULT_TENANT_ID)

    def list(self) -> list[dict[str, Any]]:
        registry = self._read_registry()
        active_id = self.active_id()
        result = []
        for item in registry["tenants"]:
            if not isinstance(item, dict):
                continue
            result.append({
                "id": item["id"],
                "name": item.get("name") or item["id"],
                "created_at": item.get("created_at"),
                "is_active": item["id"] == active_id,
            })
        return result

    def get(self, tenant_id: str) -> dict[str, Any]:
        tenant_id = self._validate_id(tenant_id)
        for item in self._read_registry()["tenants"]:
            if isinstance(item, dict) and item.get("id") == tenant_id:
                return item
        raise TenantError(f"账号组不存在：{tenant_id}")

    def create(self, tenant_id: str, name: str) -> TenantPaths:
        tenant_id = self._validate_id(tenant_id)
        name = name.strip()
        if not name or len(name) > 80:
            raise TenantError("账号组名称必须为 1–80 个字符")
        with self._lock:
            registry = self._read_registry()
            if any(isinstance(item, dict) and item.get("id") == tenant_id
                   for item in registry["tenants"]):
                raise TenantError(f"账号组标识已存在：{tenant_id}")
            # 先验证并创建隔离目录，再登记账号组；路径逃逸失败时不能留下损坏注册项。
            paths = self.paths(tenant_id, name=name, create=True)
            registry["tenants"].append({
                "id": tenant_id,
                "name": name,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            })
            self._write_registry(registry)
            return paths

    def select_active(self, tenant_id: str) -> dict[str, Any]:
        tenant_id = self._validate_id(tenant_id)
        item = self.get(tenant_id)  # 不存在时 fail-closed，不回退 default
        self.paths(tenant_id, name=str(item.get("name") or tenant_id), create=True)
        temp = self.active_file.with_suffix(".tmp")
        temp.write_text(
            json.dumps({"tenant_id": tenant_id}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            temp.chmod(0o600)
        except OSError:
            pass
        os.replace(temp, self.active_file)
        return {"id": tenant_id, "name": item.get("name") or tenant_id}

    def active_id(self) -> str:
        if not self.active_file.exists():
            return DEFAULT_TENANT_ID
        try:
            data = json.loads(self.active_file.read_text(encoding="utf-8"))
            tenant_id = self._validate_id(data.get("tenant_id"))
            self.get(tenant_id)
            return tenant_id
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise TenantError("当前账号组状态无效；为防止串用业务数据，已拒绝自动回退") from exc

    def paths(self, tenant_id: str, name: str | None = None, create: bool = False) -> TenantPaths:
        tenant_id = self._validate_id(tenant_id)
        if tenant_id == DEFAULT_TENANT_ID:
            # 保留历史路径，避免首次升级搬迁现有数据库和内容文件。
            root = self.project_root / "data"
            data_dir = root
            db_file = root / "easel.db"
            backup_dir = self.project_root / "backups"
            output_dir = root / "outputs"
            screenshot_dir = root / "screenshots"
            log_dir = root / "logs"
            profile_dir = root / "browser_profiles"
            media_dir = root / "media"
            kb_dir = root / "kb"
        else:
            root = self.project_root / "tenants" / tenant_id
            data_dir = root / "data"
            db_file = data_dir / "easel.db"
            backup_dir = root / "backups"
            output_dir = data_dir / "outputs"
            screenshot_dir = data_dir / "screenshots"
            log_dir = data_dir / "logs"
            profile_dir = root / "browser_profiles"
            media_dir = root / "media"
            kb_dir = root / "kb"

        # resolve() + containment check prevents traversal and symlink escapes.
        allowed_root = (self.project_root / "data") if tenant_id == DEFAULT_TENANT_ID else (self.project_root / "tenants")
        resolved_root = root.resolve()
        if tenant_id != DEFAULT_TENANT_ID and not resolved_root.is_relative_to(allowed_root.resolve()):
            raise TenantError("账号组目录超出允许的隔离根目录")
        result = TenantPaths(
            tenant_id=tenant_id,
            name=name or tenant_id,
            root=resolved_root,
            data_dir=data_dir,
            db_file=db_file,
            backup_dir=backup_dir,
            output_dir=output_dir,
            screenshot_dir=screenshot_dir,
            log_dir=log_dir,
            profile_dir=profile_dir,
            media_dir=media_dir,
            kb_dir=kb_dir,
        )
        if create:
            for directory in (
                result.root, result.data_dir, result.backup_dir, result.output_dir,
                result.screenshot_dir, result.log_dir, result.profile_dir,
                result.media_dir, result.kb_dir,
            ):
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    directory.chmod(0o700)
                except OSError:
                    pass
        return result

    @staticmethod
    def _validate_id(value: Any) -> str:
        if not isinstance(value, str) or not TENANT_ID_RE.fullmatch(value):
            raise TenantError("账号组标识只允许小写字母、数字、下划线和连字符，且必须以字母或数字开头")
        return value

    def _read_registry(self) -> dict[str, Any]:
        try:
            data = json.loads(self.registry_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise TenantError("账号组注册表尚未初始化") from None
        except (OSError, json.JSONDecodeError) as exc:
            raise TenantError("账号组注册表无法读取，已拒绝回退默认账号组") from exc
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("tenants"), list):
            raise TenantError("账号组注册表结构无效")
        return data

    def _write_registry(self, data: dict[str, Any]) -> None:
        temp = self.registry_file.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            temp.chmod(0o600)
        except OSError:
            pass
        os.replace(temp, self.registry_file)
        try:
            self.registry_file.chmod(0o600)
        except OSError:
            pass
