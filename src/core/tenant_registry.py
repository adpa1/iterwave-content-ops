"""账号组目录与隔离路径。

账号组元数据和活动账号组状态存储在 shared/system.db；各账号组业务数据库仍独立。
旧 tenant_registry.json / active_tenant.json 只用于一次性迁移，迁移后只读保留。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any

TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
DEFAULT_TENANT_ID = "default"
REGISTRY_MIGRATION = "tenant_registry_json_to_system_db_v1"


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


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """仅供兼容工具使用的原子 JSON 写入；运行时不再写注册表 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="",
            prefix=f".{path.name}.", suffix=".tmp",
            dir=path.parent, delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            try:
                os.fchmod(stream.fileno(), 0o600)
            except (AttributeError, OSError):
                temp_path.chmod(0o600)
            stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


class TenantRegistry:
    """持久化账号组目录。注册表是共享基础设施，不承载任何业务结果。"""

    def __init__(self, project_root: Path | str):
        self.project_root = Path(project_root).resolve()
        self.shared_dir = self.project_root / "shared"
        self.raw_dir = self.shared_dir / "raw"
        self.registry_file = self.shared_dir / "tenant_registry.json"
        self.active_file = self.shared_dir / "active_tenant.json"
        self.system_db_file = self.shared_dir / "system.db"
        self._lock = RLock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.system_db_file, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def initialize(self) -> None:
        self.shared_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.raw_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.shared_dir.chmod(0o700)
            self.raw_dir.chmod(0o700)
        except OSError:
            pass
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS tenants (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS system_settings (
                        key TEXT PRIMARY KEY,
                        value_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS system_migrations (
                        migration_key TEXT PRIMARY KEY,
                        completed_at TEXT NOT NULL,
                        imported_count INTEGER NOT NULL DEFAULT 0
                    );
                    """
                )
                marker = conn.execute(
                    "SELECT 1 FROM system_migrations WHERE migration_key=?",
                    (REGISTRY_MIGRATION,),
                ).fetchone()
                if not marker:
                    legacy_tenants = self._legacy_tenants()
                    legacy_active = self._legacy_active_id(legacy_tenants)
                    conn.execute("BEGIN IMMEDIATE")
                    for item in legacy_tenants:
                        conn.execute(
                            "INSERT OR IGNORE INTO tenants(id,name,created_at) VALUES(?,?,?)",
                            (item["id"], item.get("name") or item["id"],
                             item.get("created_at") or datetime.now().isoformat(timespec="seconds")),
                        )
                    now = datetime.now().isoformat(timespec="seconds")
                    conn.execute(
                        "INSERT INTO system_settings(key,value_json,updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO NOTHING",
                        ("active_tenant_id", json.dumps(legacy_active), now),
                    )
                    conn.execute(
                        "INSERT INTO system_migrations(migration_key,completed_at,imported_count) VALUES(?,?,?)",
                        (REGISTRY_MIGRATION, now, len(legacy_tenants)),
                    )
                    conn.commit()
                else:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "INSERT OR IGNORE INTO tenants(id,name,created_at) VALUES(?,?,?)",
                        (DEFAULT_TENANT_ID, "默认账号组", datetime.now().isoformat(timespec="seconds")),
                    )
                    row = conn.execute(
                        "SELECT 1 FROM system_settings WHERE key='active_tenant_id'"
                    ).fetchone()
                    if row is None:
                        conn.execute(
                            "INSERT INTO system_settings(key,value_json,updated_at) VALUES(?,?,?)",
                            ("active_tenant_id", json.dumps(DEFAULT_TENANT_ID),
                             datetime.now().isoformat(timespec="seconds")),
                        )
                    conn.commit()
                rows = conn.execute("SELECT id,name FROM tenants ORDER BY created_at,id").fetchall()
                if not rows:
                    raise TenantError("系统数据库中没有账号组记录")
                for row in rows:
                    self._validate_id(row["id"])
                    self.paths(row["id"], name=row["name"], create=True)
                self.active_id()  # fail-closed validation after import
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            try:
                self.system_db_file.chmod(0o600)
            except OSError:
                pass
            for legacy_file in (self.registry_file, self.active_file):
                if legacy_file.exists():
                    try:
                        legacy_file.chmod(0o400)
                    except OSError:
                        pass

    def _legacy_tenants(self) -> list[dict[str, Any]]:
        if not self.registry_file.exists():
            return [{
                "id": DEFAULT_TENANT_ID,
                "name": "默认账号组",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }]
        registry = self._read_registry()
        tenants = registry.get("tenants")
        if not isinstance(tenants, list):
            raise TenantError("账号组注册表格式错误：tenants 必须是数组")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in tenants:
            if not isinstance(item, dict):
                raise TenantError("账号组注册表包含无效条目")
            tenant_id = self._validate_id(item.get("id"))
            if tenant_id in seen:
                raise TenantError(f"账号组注册表存在重复标识：{tenant_id}")
            seen.add(tenant_id)
            normalized.append({
                "id": tenant_id,
                "name": str(item.get("name") or tenant_id),
                "created_at": item.get("created_at") or datetime.now().isoformat(timespec="seconds"),
            })
        if DEFAULT_TENANT_ID not in seen:
            normalized.insert(0, {
                "id": DEFAULT_TENANT_ID,
                "name": "默认账号组",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            })
        return normalized

    def _legacy_active_id(self, tenants: list[dict[str, Any]]) -> str:
        if not self.active_file.exists():
            return DEFAULT_TENANT_ID
        try:
            data = json.loads(self.active_file.read_text(encoding="utf-8"))
            tenant_id = self._validate_id(data.get("tenant_id"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise TenantError("旧活动账号组状态无效；为防止串用业务数据，已拒绝自动回退") from exc
        if not any(item["id"] == tenant_id for item in tenants):
            raise TenantError("旧活动账号组不存在于注册表；为防止串用业务数据，已拒绝自动回退")
        return tenant_id

    def list(self) -> list[dict[str, Any]]:
        active_id = self.active_id()
        conn = self._connect()
        try:
            rows = conn.execute("SELECT id,name,created_at FROM tenants ORDER BY created_at,id").fetchall()
            return [{
                "id": row["id"], "name": row["name"], "created_at": row["created_at"],
                "is_active": row["id"] == active_id,
            } for row in rows]
        finally:
            conn.close()

    def get(self, tenant_id: str) -> dict[str, Any]:
        tenant_id = self._validate_id(tenant_id)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id,name,created_at FROM tenants WHERE id=?", (tenant_id,)
            ).fetchone()
            if row is None:
                raise TenantError(f"账号组不存在：{tenant_id}")
            return {"id": row["id"], "name": row["name"], "created_at": row["created_at"]}
        finally:
            conn.close()

    def create(self, tenant_id: str, name: str) -> TenantPaths:
        tenant_id = self._validate_id(tenant_id)
        name = name.strip()
        if not name or len(name) > 80:
            raise TenantError("账号组名称必须为 1–80 个字符")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("SELECT 1 FROM tenants WHERE id=?", (tenant_id,)).fetchone():
                    raise TenantError(f"账号组标识已存在：{tenant_id}")
                paths = self.paths(tenant_id, name=name, create=True)
                conn.execute(
                    "INSERT INTO tenants(id,name,created_at) VALUES(?,?,?)",
                    (tenant_id, name, datetime.now().isoformat(timespec="seconds")),
                )
                conn.commit()
                return paths
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def select_active(self, tenant_id: str) -> dict[str, Any]:
        tenant_id = self._validate_id(tenant_id)
        item = self.get(tenant_id)  # 不存在时 fail-closed，不回退 default
        self.paths(tenant_id, name=str(item.get("name") or tenant_id), create=True)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO system_settings(key,value_json,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                    ("active_tenant_id", json.dumps(tenant_id),
                     datetime.now().isoformat(timespec="seconds")),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return {"id": tenant_id, "name": item.get("name") or tenant_id}

    def active_id(self) -> str:
        try:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT value_json FROM system_settings WHERE key='active_tenant_id'"
                ).fetchone()
                if row is None:
                    raise TenantError("系统数据库缺少活动账号组状态")
                tenant_id = self._validate_id(json.loads(row["value_json"]))
                exists = conn.execute("SELECT 1 FROM tenants WHERE id=?", (tenant_id,)).fetchone()
                if exists is None:
                    raise TenantError("活动账号组不存在于系统注册表")
                return tenant_id
            finally:
                conn.close()
        except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
            if isinstance(exc, TenantError):
                raise
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
            profile_dir = data_dir / "_profiles"
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

        allowed_root = (self.project_root / "data") if tenant_id == DEFAULT_TENANT_ID else (self.project_root / "tenants")
        resolved_root = root.resolve()
        if tenant_id != DEFAULT_TENANT_ID and not resolved_root.is_relative_to(allowed_root.resolve()):
            raise TenantError("账号组目录超出允许的隔离根目录")
        if tenant_id != DEFAULT_TENANT_ID:
            root = resolved_root
            data_dir = root / "data"
            db_file = data_dir / "easel.db"
            backup_dir = root / "backups"
            output_dir = data_dir / "outputs"
            screenshot_dir = data_dir / "screenshots"
            log_dir = data_dir / "logs"
            profile_dir = root / "browser_profiles"
            media_dir = root / "media"
            kb_dir = root / "kb"
        result = TenantPaths(
            tenant_id=tenant_id, name=name or tenant_id, root=resolved_root,
            data_dir=data_dir, db_file=db_file, backup_dir=backup_dir,
            output_dir=output_dir, screenshot_dir=screenshot_dir, log_dir=log_dir,
            profile_dir=profile_dir, media_dir=media_dir, kb_dir=kb_dir,
        )
        if create:
            for directory in (
                result.root, result.data_dir, result.backup_dir, result.output_dir,
                result.screenshot_dir, result.log_dir, result.profile_dir,
                result.media_dir, result.kb_dir,
            ):
                if tenant_id != DEFAULT_TENANT_ID and not directory.resolve().is_relative_to(result.root):
                    raise TenantError("账号组子目录超出隔离根目录")
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

