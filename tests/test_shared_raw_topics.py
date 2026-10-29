from __future__ import annotations

import sqlite3
from pathlib import Path

from src.core import shared_raw_topics


def _tenant_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE hot_topics (
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
            platform TEXT,
            dimension TEXT,
            title_norm TEXT,
            seen_count INTEGER NOT NULL DEFAULT 1,
            duplicate_of TEXT,
            match_score REAL,
            match_dimension TEXT,
            match_angle TEXT,
            match_reason TEXT,
            match_method TEXT,
            match_version TEXT,
            matched_at TEXT,
            forbidden_hit INTEGER
        );
    """)
    return conn


def test_shared_snapshot_only_contains_raw_fields_and_tenant_recomputes_processing(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(shared_raw_topics, "PROJECT_ROOT", tmp_path)
    source = _tenant_connection()
    source.execute(
        "INSERT INTO hot_topics (id, source, raw_url, discovered_at, last_seen_at, "
        "evidence_status, title, heat_score, heat_unit, platform, dimension, title_norm, "
        "seen_count, duplicate_of, match_score, match_reason) "
        "VALUES ('hot-1','feed/news','https://example.com','2026-10-29T10:00:00',"
        "'2026-10-29T10:00:00','collected','A title',123,'heat','news','成长',"
        "'a title',7,'hot-other',98,'tenant-specific conclusion')"
    )
    source.commit()

    assert shared_raw_topics.publish_from_tenant(source) == 1

    shared = sqlite3.connect(tmp_path / "shared" / "raw_hot_topics.db")
    columns = {row[1] for row in shared.execute("PRAGMA table_info(raw_hot_topics)")}
    assert "match_score" not in columns
    assert "match_reason" not in columns
    assert "dimension" not in columns
    shared.close()

    target = _tenant_connection()
    result = shared_raw_topics.import_to_tenant(target)
    assert result["inserted"] == 1
    row = target.execute("SELECT * FROM hot_topics WHERE id='hot-1'").fetchone()
    assert row["title"] == "A title"
    assert row["heat_score"] == 123
    assert row["match_score"] is None
    assert row["match_reason"] is None
    assert row["dimension"] is None
    assert row["title_norm"] is None
    assert row["seen_count"] == 1
    assert row["duplicate_of"] is None
