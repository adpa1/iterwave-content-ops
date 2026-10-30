"""SQLite repository for tenant-scoped runtime configuration.

Secrets are deliberately excluded. Each tenant has its own database, so the
repository never accepts a tenant identifier from callers.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
LEGACY_MIGRATION_KEY = "config_overrides_json_v1"
_SECRET_KEY = re.compile(r"(api[_-]?key|secret|password|token|webhook|credential)", re.I)


def _safe_value(key: str, value: Any) -> bool:
    if _SECRET_KEY.search(key) and not key.endswith(".webhook_url_env"):
        return False
    if isinstance(value, str):
        # Do not persist URL userinfo, even if a future whitelist accidentally allows it.
        from urllib.parse import urlsplit
        if value.startswith(("http://", "https://")):
            try:
                parts = urlsplit(value)
                if parts.username is not None or parts.password is not None:
                    return False
            except ValueError:
                return False
    if isinstance(value, dict):
        return all(_safe_value(f"{key}.{k}", v) for k, v in value.items())
    if isinstance(value, list):
        return all(_safe_value(key, v) for v in value)
    return True


def _history_safe(value: Any) -> Any:
    """Redact secrets in immutable history while retaining operational values in current settings."""
    from src.core.redact import redact
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(k): _history_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_history_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_history_safe(v) for v in value]
    return value


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def legacy_import_completed(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM tenant_setting_migrations WHERE migration_key=?",
        (LEGACY_MIGRATION_KEY,),
    ).fetchone()
    return row is not None


def read_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT key, value_json FROM tenant_settings ORDER BY key").fetchall()
    result: dict[str, Any] = {}
    for row in rows:
        try:
            result[str(row["key"])] = json.loads(row["value_json"])
        except (TypeError, ValueError):
            logger.error("运行时配置记录损坏，已忽略 key=%s", row["key"])
    return result


def import_legacy_overrides(conn: sqlite3.Connection, legacy_path: Path) -> dict[str, int]:
    """Import legacy JSON exactly once; keep the source file untouched for rollback."""
    try:
        raw = json.loads(legacy_path.read_text(encoding="utf-8")) if legacy_path.exists() else {}
    except (OSError, ValueError) as exc:
        logger.error("旧配置文件读取失败，保留文件并跳过回填：%s", type(exc).__name__)
        return {"imported": 0, "rejected": 0}
    if not isinstance(raw, dict):
        logger.error("旧配置文件结构无效，保留文件并跳过回填")
        return {"imported": 0, "rejected": 0}

    accepted: dict[str, Any] = {}
    rejected = 0
    from src.core.config_store import _validate
    for key, value in raw.items():
        if not isinstance(key, str) or not key or not _safe_value(key, value):
            rejected += 1
            continue
        try:
            accepted[key] = _validate(key, value)
        except (ValueError, TypeError):
            rejected += 1

    now = _now()
    lock = getattr(conn, "_lock", None)
    if lock:
        lock.acquire()
    try:
        # Recheck both the migration marker and existing DB values while holding
        # SQLite's write reservation. Two workers may have read the same legacy
        # file before either one acquired this transaction.
        conn.execute("BEGIN IMMEDIATE")
        marker = conn.execute(
            "SELECT 1 FROM tenant_setting_migrations WHERE migration_key=?",
            (LEGACY_MIGRATION_KEY,),
        ).fetchone()
        if marker:
            conn.commit()
            return {"imported": 0, "rejected": 0}

        existing = conn.execute("SELECT COUNT(*) FROM tenant_settings").fetchone()[0]
        if existing:
            conn.execute(
                "INSERT INTO tenant_setting_migrations(migration_key,completed_at,imported_count,rejected_count) VALUES(?,?,0,0)",
                (LEGACY_MIGRATION_KEY, now),
            )
            conn.commit()
            return {"imported": 0, "rejected": 0}

        for key, value in accepted.items():
            conn.execute(
                "INSERT INTO tenant_settings(key,value_json,value_type,version,updated_at,updated_by) VALUES(?,?,?,?,?,?)",
                (key, json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                 type(value).__name__, 1, now, "legacy_import"),
            )
        if accepted:
            snapshot = json.dumps(_history_safe(accepted), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            conn.execute(
                "INSERT INTO tenant_setting_versions(version,snapshot_json,changed_at,changed_by,change_reason) VALUES(1,?,?,?,?)",
                (snapshot, now, "legacy_import", "config_overrides.json migration"),
            )
        conn.execute(
            "INSERT INTO tenant_setting_migrations(migration_key,completed_at,imported_count,rejected_count) VALUES(?,?,?,?)",
            (LEGACY_MIGRATION_KEY, now, len(accepted), rejected),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if lock:
            lock.release()
    if legacy_path.exists():
        try:
            legacy_path.chmod(0o400)
        except OSError:
            logger.warning("无法将旧配置文件收紧为只读：%s", legacy_path.name)
    if rejected:
        logger.warning("旧配置回填拒绝 %d 项（未记录配置值）", rejected)
    return {"imported": len(accepted), "rejected": rejected}


def save_settings(conn: sqlite3.Connection, updates: dict[str, Any], *, audit_rows: list[tuple[str, str | None, str]] | None = None, remove_keys: list[str] | None = None, changed_by: str = "settings_ui") -> int:
    """Atomically upsert changed keys and persist a complete version snapshot."""
    if not updates:
        row = conn.execute("SELECT COALESCE(MAX(version),0) FROM tenant_setting_versions").fetchone()
        return int(row[0])
    unsafe = [key for key, value in updates.items() if not _safe_value(key, value)]
    if unsafe:
        raise ValueError(f"配置包含凭据或带认证信息的 URL，拒绝持久化：{', '.join(unsafe)}")
    now = _now()
    lock = getattr(conn, "_lock", None)
    if lock:
        lock.acquire()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = read_settings(conn)
        for key in (remove_keys or []):
            current.pop(key, None)
        current.update(updates)
        row = conn.execute("SELECT COALESCE(MAX(version),0) FROM tenant_setting_versions").fetchone()
        version = int(row[0]) + 1
        for key in (remove_keys or []):
            conn.execute("DELETE FROM tenant_settings WHERE key=?", (key,))
        for key, value in updates.items():
            conn.execute(
                """INSERT INTO tenant_settings(key,value_json,value_type,version,updated_at,updated_by)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                     value_type=excluded.value_type, version=excluded.version,
                     updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
                (key, json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                 type(value).__name__, version, now, changed_by),
            )
        snapshot = json.dumps(_history_safe(current), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        conn.execute(
            "INSERT INTO tenant_setting_versions(version,snapshot_json,changed_at,changed_by) VALUES(?,?,?,?)",
            (version, snapshot, now, changed_by),
        )
        for key, old_value, new_value in (audit_rows or []):
            conn.execute(
                "INSERT INTO config_changes(key,old_value,new_value,changed_at) VALUES(?,?,?,?)",
                (key, _history_safe(old_value), _history_safe(new_value), now),
            )
        conn.commit()
        return version
    except Exception:
        conn.rollback()
        raise
    finally:
        if lock:
            lock.release()
