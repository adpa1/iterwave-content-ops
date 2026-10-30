"""System-scoped configuration repository.

Only shared runtime infrastructure belongs here. The first migrated namespace is
llm.*: one model configuration and one deployment key shared by all customers.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)
_SYSTEM_WAL_LOCK = threading.Lock()

SYSTEM_LLM_KEYS = frozenset({
    "llm.enabled",
    "llm.provider",
    "llm.base_url",
    "llm.model",
    "llm.fallback_model",
    "llm.temperature",
    "llm.timeout_seconds",
})
_TENANT_LLM_KEYS = SYSTEM_LLM_KEYS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _history_value(value: Any) -> Any:
    from src.core.redact import redact
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(key): _history_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_history_value(item) for item in value]
    return value


def connect_system(system_db_file: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(Path(system_db_file).resolve()), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize_system_config(system_db_file: Path | str) -> None:
    db_path = Path(system_db_file).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = connect_system(db_path)
    try:
        with _SYSTEM_WAL_LOCK:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS system_setting_versions (
                version INTEGER PRIMARY KEY,
                snapshot_json TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                changed_by TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS system_config_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                changed_at TEXT NOT NULL,
                changed_by TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS system_config_migration_conflicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                key TEXT NOT NULL,
                legacy_value_json TEXT NOT NULL,
                selected_value_json TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                UNIQUE(tenant_id, key, legacy_value_json, selected_value_json)
            );
            """
        )
        # 多个 API worker 可能同时启动；串行化列迁移，避免重复 ALTER。
        conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(system_settings)").fetchall()
            }
            if "updated_by" not in columns:
                conn.execute(
                    "ALTER TABLE system_settings ADD COLUMN updated_by TEXT NOT NULL DEFAULT 'system'"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    try:
        db_path.chmod(0o600)
    except OSError:
        logger.warning("无法收紧系统级配置数据库权限：%s", db_path.name)


def read_system_llm_settings(system_db_file: Path | str) -> dict[str, Any]:
    initialize_system_config(system_db_file)
    conn = connect_system(system_db_file)
    try:
        rows = conn.execute(
            "SELECT key,value_json FROM system_settings WHERE key LIKE 'llm.%' ORDER BY key"
        ).fetchall()
        values: dict[str, Any] = {}
        for row in rows:
            key = str(row["key"])
            if key not in SYSTEM_LLM_KEYS:
                continue
            try:
                values[key] = json.loads(row["value_json"])
            except (TypeError, ValueError):
                logger.error("系统级模型配置损坏，拒绝套用 key=%s", key)
                raise ValueError(f"系统级模型配置损坏：{key}")
        return values
    finally:
        conn.close()


def _validate_system_value(key: str, value: Any) -> Any:
    from src.core.config_store import _validate

    normalized = _validate(key, value)
    if key == "llm.base_url":
        parts = urlsplit(normalized)
        if parts.username is not None or parts.password is not None:
            raise ValueError("模型服务地址不能包含用户名或密码")
    return normalized


def save_system_llm_settings(
    system_db_file: Path | str,
    updates: dict[str, Any],
    *,
    changed_by: str = "settings_ui",
    audit_rows: list[tuple[str, str | None, str]] | None = None,
) -> int:
    if not updates:
        return 0
    normalized: dict[str, Any] = {}
    for key, value in updates.items():
        if key not in SYSTEM_LLM_KEYS:
            raise ValueError(f"不允许保存系统级配置：{key}")
        normalized[key] = _validate_system_value(key, value)

    initialize_system_config(system_db_file)
    conn = connect_system(system_db_file)
    now = _now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current_rows = conn.execute(
            "SELECT key,value_json FROM system_settings WHERE key LIKE 'llm.%'"
        ).fetchall()
        current = {str(row["key"]): json.loads(row["value_json"]) for row in current_rows}
        current.update(normalized)
        version_row = conn.execute(
            "SELECT COALESCE(MAX(version),0) FROM system_setting_versions"
        ).fetchone()
        version = int(version_row[0]) + 1
        for key, value in normalized.items():
            conn.execute(
                "INSERT INTO system_settings(key,value_json,updated_at,updated_by) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,"
                "updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                (key, json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                 now, changed_by),
            )
        snapshot = json.dumps(_history_value(current), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        conn.execute(
            "INSERT INTO system_setting_versions(version,snapshot_json,changed_at,changed_by) "
            "VALUES(?,?,?,?)",
            (version, snapshot, now, changed_by),
        )
        for key, old_value, new_value in (audit_rows or []):
            conn.execute(
                "INSERT INTO system_config_changes(key,old_value,new_value,changed_at,changed_by) "
                "VALUES(?,?,?,?,?)",
                (key, _history_value(old_value), _history_value(new_value), now, changed_by),
            )
        conn.commit()
        return version
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_system_llm_settings(cfg, system_db_file: Path | str) -> list[str]:
    from src.core.config_store import _set_value

    values = read_system_llm_settings(system_db_file)
    applied: list[str] = []
    for key, value in values.items():
        normalized = _validate_system_value(key, value)
        _set_value(cfg, key, normalized)
        applied.append(key)
    return applied


def migrate_tenant_llm_settings(
    cfg,
    tenant_conn: sqlite3.Connection,
    system_db_file: Path | str,
    *,
    tenant_id: str,
    base_llm: dict[str, Any] | None = None,
) -> dict[str, int]:
    """首次以启动时活动账号组配置为准迁移；后续账号组冲突留痕并清除租户副本。"""
    from src.core.config_repository import purge_disallowed_settings, read_settings

    initialize_system_config(system_db_file)
    legacy = {
        key: value for key, value in read_settings(tenant_conn).items()
        if key in _TENANT_LLM_KEYS
    }
    system_values = read_system_llm_settings(system_db_file)
    if not system_values:
        # 第一次迁移以启动时活动账号组的有效值为准，避免直接丢弃已有自定义模型配置。
        current_llm = cfg.llm.model_dump()
        initial = {
            key: current_llm.get(key.removeprefix("llm."))
            for key in SYSTEM_LLM_KEYS
        }
        save_system_llm_settings(
            system_db_file, initial, changed_by=f"tenant_migration:{tenant_id}",
        )
        system_values = read_system_llm_settings(system_db_file)
    else:
        defaults = base_llm or cfg.llm.model_dump()
        missing = {
            key: defaults.get(key.removeprefix("llm."))
            for key in SYSTEM_LLM_KEYS if key not in system_values
        }
        if missing:
            save_system_llm_settings(
                system_db_file, missing, changed_by="system_config_defaults",
            )
            system_values = read_system_llm_settings(system_db_file)

    conflicts = 0
    if legacy:
        conn = connect_system(system_db_file)
        try:
            now = _now()
            for key, legacy_value in legacy.items():
                selected = system_values.get(key)
                if selected == legacy_value:
                    continue
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO system_config_migration_conflicts"
                    "(tenant_id,key,legacy_value_json,selected_value_json,detected_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        tenant_id, key,
                        json.dumps(_history_value(legacy_value), ensure_ascii=False, sort_keys=True),
                        json.dumps(_history_value(selected), ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                conflicts += cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        purged = purge_disallowed_settings(tenant_conn, set(legacy))
    else:
        purged = 0

    applied = apply_system_llm_settings(cfg, system_db_file)
    if conflicts:
        logger.warning(
            "账号组模型配置已统一为系统级值 tenant=%s conflicts=%d",
            tenant_id, conflicts,
        )
    return {"purged": purged, "conflicts": conflicts, "applied": len(applied)}
