from __future__ import annotations

import json
import uuid
from datetime import datetime

import pytest


def test_activation_retention_migration_creates_tenant_scoped_tables(conn):
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {
        "activation_cohort", "sample_runs", "sample_contents",
        "content_events", "metric_snapshots",
    } <= tables

    sample_columns = {row[1] for row in conn.execute("PRAGMA table_info(sample_contents)")}
    assert {"sample_run_id", "tenant_id", "quality_status", "adopted_at", "rejected_at"} <= sample_columns
    metric_columns = {row[1] for row in conn.execute("PRAGMA table_info(metric_snapshots)")}
    assert {"source", "measured_at", "recorded_at", "correction_of"} <= metric_columns


def test_sample_run_idempotency_is_scoped_to_tenant(conn):
    now = datetime.now().isoformat(sep=" ")
    values = ("run-a", "tenant-a", "xhs", "分享产品经理实践方法", "created", "key-1", now)
    conn.execute(
        "INSERT INTO sample_runs (id, tenant_id, platform, account_description, status, idempotency_key, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", values,
    )
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO sample_runs (id, tenant_id, platform, account_description, status, idempotency_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("run-b", "tenant-a", "xhs", "分享产品经理实践方法", "created", "key-1", now),
        )
    conn.execute(
        "INSERT INTO sample_runs (id, tenant_id, platform, account_description, status, idempotency_key, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("run-c", "tenant-b", "xhs", "分享产品经理实践方法", "created", "key-1", now),
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM sample_runs").fetchone()[0] == 2


def test_sample_content_events_are_idempotent_and_tenant_scoped(conn):
    now = datetime.now().isoformat(sep=" ")
    conn.execute(
        "INSERT INTO sample_runs (id, tenant_id, platform, account_description, status, idempotency_key, created_at) "
        "VALUES ('run-1', 'tenant-a', 'xhs', '分享产品经理实践方法', 'ready', 'request-1', ?)", (now,),
    )
    conn.execute(
        "INSERT INTO sample_contents "
        "(id, sample_run_id, tenant_id, platform, angle, title, body, hashtags_json, quality_status, "
        "quality_findings_json, created_at, updated_at) "
        "VALUES ('sample-1', 'run-1', 'tenant-a', 'xhs', '经验洞察', '标题', '正文内容', '[]', 'pass', '[]', ?, ?)",
        (now, now),
    )
    event = (
        "event-1", "tenant-a", "sample-1", "content_saved", now,
        json.dumps({"source": "web"}), "content-saved:sample-1",
    )
    sql = (
        "INSERT OR IGNORE INTO content_events "
        "(id, tenant_id, sample_content_id, event_type, event_at, source, metadata_json, idempotency_key) "
        "VALUES (?, ?, ?, ?, ?, 'web', ?, ?)"
    )
    conn.execute(sql, event)
    conn.execute(sql, ("event-2", *event[1:]))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM content_events WHERE event_type='content_saved'").fetchone()[0] == 1


def test_metric_snapshots_preserve_nulls_and_history(conn, seed):
    round_id = seed.round()
    draft_id = seed.draft(round_id, draft_status="published", fetch_status=None)
    content_id = conn.execute("SELECT content_id FROM drafts WHERE id=?", (draft_id,)).fetchone()[0]
    now = datetime.now().isoformat(sep=" ")
    for snapshot_id, source, views, likes in (
        ("snapshot-manual-1", "manual", 100, None),
        ("snapshot-manual-2", "manual", 150, 12),
    ):
        conn.execute(
            "INSERT INTO metric_snapshots "
            "(id, tenant_id, draft_id, content_id, platform, source, views, likes, measured_at, recorded_at, created_at) "
            "VALUES (?, 'default', ?, ?, 'xhs', ?, ?, ?, ?, ?, ?)",
            (snapshot_id, draft_id, content_id, source, views, likes, now, now, now),
        )
    conn.commit()
    rows = conn.execute(
        "SELECT source, views, likes FROM metric_snapshots WHERE draft_id=? ORDER BY id",
        (draft_id,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["views"] == 100
    assert rows[0]["likes"] is None
    assert all(row["source"] == "manual" for row in rows)


def test_metric_snapshot_rejects_negative_values(conn, seed):
    round_id = seed.round()
    draft_id = seed.draft(round_id, draft_status="published", fetch_status=None)
    content_id = conn.execute("SELECT content_id FROM drafts WHERE id=?", (draft_id,)).fetchone()[0]
    now = datetime.now().isoformat(sep=" ")
    with pytest.raises(Exception):
        conn.execute(
            "INSERT INTO metric_snapshots "
            "(id, tenant_id, draft_id, content_id, platform, source, views, measured_at, recorded_at, created_at) "
            "VALUES (?, 'default', ?, ?, 'xhs', 'manual', -1, ?, ?, ?)",
            (uuid.uuid4().hex, draft_id, content_id, now, now, now),
        )
