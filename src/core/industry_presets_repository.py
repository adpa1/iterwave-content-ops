"""SQLite-backed tenant industry presets; Python definitions are first-install seeds only."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from src.core.industry_presets import PRESETS

SEED_MARKER = "industry_presets_builtin_seed_v1"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _decode(row) -> dict[str, Any]:
    return {
        "key": row["key"],
        "name": row["name"],
        "description": row["description"],
        "positioning": row["positioning"],
        "persona_keywords": json.loads(row["persona_keywords_json"]),
        "forbidden_directions": json.loads(row["forbidden_directions_json"]),
        "extra_redlines": json.loads(row["extra_redlines_json"]),
        "dimensions": json.loads(row["dimensions_json"]),
        "platform_notes": json.loads(row["platform_notes_json"]),
        "is_builtin": bool(row["is_builtin"]),
        "version": int(row["version"]),
    }


def seed_builtins(conn) -> dict[str, int]:
    marker = conn.execute(
        "SELECT 1 FROM tenant_setting_migrations WHERE migration_key=?", (SEED_MARKER,)
    ).fetchone()
    if marker:
        return {"imported": 0}
    lock = getattr(conn, "_lock", None)
    if lock:
        lock.acquire()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = _now()
        for item in PRESETS:
            conn.execute(
                """INSERT OR IGNORE INTO industry_presets(
                    key,name,description,positioning,persona_keywords_json,
                    forbidden_directions_json,extra_redlines_json,dimensions_json,
                    platform_notes_json,is_builtin,version,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,1,1,?,?)""",
                (
                    item["key"], item["name"], item.get("description", ""),
                    item["positioning"],
                    json.dumps(item.get("persona_keywords", []), ensure_ascii=False),
                    json.dumps(item.get("forbidden_directions", []), ensure_ascii=False),
                    json.dumps(item.get("extra_redlines", []), ensure_ascii=False),
                    json.dumps(item.get("dimensions", []), ensure_ascii=False),
                    json.dumps(item.get("platform_notes", {}), ensure_ascii=False),
                    now, now,
                ),
            )
        conn.execute(
            "INSERT INTO tenant_setting_migrations(migration_key,completed_at,imported_count,rejected_count) VALUES(?,?,?,0)",
            (SEED_MARKER, now, len(PRESETS)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if lock:
            lock.release()
    return {"imported": len(PRESETS)}


def list_presets(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM industry_presets ORDER BY is_builtin DESC, name, key"
    ).fetchall()
    return [_decode(row) for row in rows]


def get_preset(conn, key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM industry_presets WHERE key=?", (key,)).fetchone()
    return _decode(row) if row else None


def upsert_custom_preset(conn, item: dict[str, Any]) -> dict[str, Any]:
    """Create/update a tenant-owned preset without allowing built-in key takeover."""
    key = str(item.get("key") or "").strip()
    if not key or len(key) > 64 or not key.replace("_", "").replace("-", "").isalnum():
        raise ValueError("模板 key 只能包含字母、数字、下划线和连字符")
    required = ("name", "positioning")
    if any(not str(item.get(field) or "").strip() for field in required):
        raise ValueError("模板名称和定位不能为空")
    arrays = ("persona_keywords", "forbidden_directions", "extra_redlines", "dimensions")
    normalized = {}
    for field in arrays:
        value = item.get(field, [])
        if not isinstance(value, list) or len(value) > 200 or any(not isinstance(v, str) for v in value):
            raise ValueError(f"{field} 必须是最多 200 项的字符串数组")
        normalized[field] = [v.strip() for v in value if v.strip()]
    notes = item.get("platform_notes", {})
    if not isinstance(notes, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in notes.items()):
        raise ValueError("platform_notes 必须是字符串映射")
    lock = getattr(conn, "_lock", None)
    if lock:
        lock.acquire()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT is_builtin FROM industry_presets WHERE key=?", (key,)).fetchone()
        if existing and existing["is_builtin"]:
            raise ValueError("内置模板不可覆盖")
        now = _now()
        conn.execute(
            """INSERT INTO industry_presets(
                key,name,description,positioning,persona_keywords_json,
                forbidden_directions_json,extra_redlines_json,dimensions_json,
                platform_notes_json,is_builtin,version,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,0,1,?,?)
            ON CONFLICT(key) DO UPDATE SET
                name=excluded.name, description=excluded.description,
                positioning=excluded.positioning, persona_keywords_json=excluded.persona_keywords_json,
                forbidden_directions_json=excluded.forbidden_directions_json,
                extra_redlines_json=excluded.extra_redlines_json,
                dimensions_json=excluded.dimensions_json, platform_notes_json=excluded.platform_notes_json,
                version=industry_presets.version+1, updated_at=excluded.updated_at""",
            (
                key, str(item["name"]).strip(), str(item.get("description") or "").strip(),
                str(item["positioning"]).strip(),
                json.dumps(normalized["persona_keywords"], ensure_ascii=False),
                json.dumps(normalized["forbidden_directions"], ensure_ascii=False),
                json.dumps(normalized["extra_redlines"], ensure_ascii=False),
                json.dumps(normalized["dimensions"], ensure_ascii=False),
                json.dumps(notes, ensure_ascii=False), now, now,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if lock:
            lock.release()
    return get_preset(conn, key)
