"""共享公共热点原始采集快照。

这里只保存来源返回的原始字段，不保存账号组画像匹配分、排序、推荐或分析结论。
各账号组将快照导入自己的 hot_topics 表，再独立执行去重/匹配/分析。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import PROJECT_ROOT

RAW_FIELDS = (
    "id", "source", "raw_url", "discovered_at", "last_seen_at", "evidence_status",
    "title", "heat_score", "heat_unit", "time_sensitive", "platform",
)


def _connect() -> sqlite3.Connection:
    root = PROJECT_ROOT / "shared"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "raw_hot_topics.db"
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS raw_hot_topics (
            id TEXT PRIMARY KEY,
            source TEXT,
            raw_url TEXT,
            discovered_at TEXT,
            last_seen_at TEXT,
            evidence_status TEXT NOT NULL,
            title TEXT,
            heat_score REAL,
            heat_unit TEXT,
            time_sensitive INTEGER,
            platform TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_raw_hot_topics_seen
            ON raw_hot_topics(last_seen_at);
    """)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return conn


def publish_from_tenant(conn) -> int:
    """将当前账号组的原始采集字段同步到共享快照，不导出加工结论。"""
    rows = conn.execute(
        "SELECT " + ", ".join(RAW_FIELDS) + " FROM hot_topics "
        "WHERE evidence_status IN ('collected','failed')"
    ).fetchall()
    if not rows:
        return 0
    shared = _connect()
    try:
        placeholders = ", ".join("?" for _ in RAW_FIELDS)
        updates = ", ".join(
            f"{field}=excluded.{field}" for field in RAW_FIELDS if field != "id"
        )
        shared.executemany(
            f"INSERT INTO raw_hot_topics ({', '.join(RAW_FIELDS)}) "
            f"VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {updates} "
            "WHERE COALESCE(excluded.last_seen_at, excluded.discovered_at, '') >= "
            "COALESCE(raw_hot_topics.last_seen_at, raw_hot_topics.discovered_at, '')",
            [tuple(row[field] for field in RAW_FIELDS) for row in rows],
        )
        # 同一来源恢复成功后，清除旧失败占位，避免把旧失败状态带到其他账号组。
        shared.execute(
            "DELETE FROM raw_hot_topics WHERE evidence_status = 'failed' AND EXISTS ("
            "SELECT 1 FROM raw_hot_topics AS fresh "
            "WHERE fresh.source = raw_hot_topics.source "
            "AND fresh.evidence_status = 'collected' "
            "AND COALESCE(fresh.last_seen_at, fresh.discovered_at, '') >= "
            "COALESCE(raw_hot_topics.last_seen_at, raw_hot_topics.discovered_at, ''))"
        )
        shared.commit()
        return len(rows)
    finally:
        shared.close()


def import_to_tenant(conn) -> dict[str, int]:
    """导入共享原始数据；只有共享数据更新时才清除本组旧匹配结果。"""
    shared = _connect()
    inserted = updated = unchanged = 0
    try:
        rows = shared.execute(
            "SELECT " + ", ".join(RAW_FIELDS) + " FROM raw_hot_topics"
        ).fetchall()
    finally:
        shared.close()

    for row in rows:
        current = conn.execute(
            "SELECT last_seen_at, evidence_status FROM hot_topics WHERE id = ?",
            (row["id"],),
        ).fetchone()
        if current is None:
            conn.execute(
                "INSERT INTO hot_topics (" + ", ".join(RAW_FIELDS) + ") "
                "VALUES (" + ", ".join("?" for _ in RAW_FIELDS) + ")",
                tuple(row[field] for field in RAW_FIELDS),
            )
            inserted += 1
            continue
        incoming_seen = row["last_seen_at"] or row["discovered_at"] or ""
        current_seen = current["last_seen_at"] or ""
        if incoming_seen <= current_seen and current["evidence_status"] == row["evidence_status"]:
            unchanged += 1
            continue
        assignments = ", ".join(f"{field} = ?" for field in RAW_FIELDS if field != "id")
        values = [row[field] for field in RAW_FIELDS if field != "id"]
        # 只重置本账号组加工字段；不复制任何账号组的匹配结果。
        conn.execute(
            f"UPDATE hot_topics SET {assignments}, "
            "dimension = NULL, title_norm = NULL, seen_count = 1, duplicate_of = NULL, "
            "match_score = NULL, match_dimension = NULL, "
            "match_angle = NULL, match_reason = NULL, match_method = NULL, "
            "match_version = NULL, matched_at = NULL, forbidden_hit = NULL "
            "WHERE id = ?",
            (*values, row["id"]),
        )
        updated += 1
    conn.commit()
    return {"inserted": inserted, "updated": updated, "unchanged": unchanged}


def shared_snapshot_fresh(max_age_minutes: int) -> bool:
    from datetime import datetime, timedelta

    shared = _connect()
    try:
        row = shared.execute(
            "SELECT MAX(COALESCE(last_seen_at, discovered_at)) FROM raw_hot_topics "
            "WHERE evidence_status = 'collected'"
        ).fetchone()
    finally:
        shared.close()
    value = row[0] if row else None
    if not value:
        return False
    try:
        captured = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return datetime.now() - captured < timedelta(minutes=max(1, max_age_minutes))
