"""SQLite configuration repository migration and isolation tests."""
from __future__ import annotations

import json

from src.core.config_repository import import_legacy_overrides, read_settings, save_settings


def test_legacy_json_import_is_idempotent(conn, tmp_path):
    legacy = tmp_path / "config_overrides.json"
    legacy.write_text(json.dumps({
        "timeouts.round_timeout_hours": 24,
        "llm.api_key": "must-not-import",
        "llm.base_url": "https://user:password@example.com/v1",
        "notifications.channels.wecom.webhook_url_env": "WECOM_WEBHOOK",
    }), encoding="utf-8")

    first = import_legacy_overrides(conn, legacy)
    second = import_legacy_overrides(conn, legacy)

    assert first == {"imported": 2, "rejected": 2}
    assert second == {"imported": 0, "rejected": 0}
    assert read_settings(conn) == {
        "timeouts.round_timeout_hours": 24,
        "notifications.channels.wecom.webhook_url_env": "WECOM_WEBHOOK",
    }
    assert legacy.exists()  # migration never deletes the rollback source


def test_save_settings_versions_and_audit_are_atomic(conn):
    version = save_settings(
        conn,
        {"timeouts.round_timeout_hours": 36},
        audit_rows=[("timeouts.round_timeout_hours", "24", "36")],
    )
    assert version == 1
    assert read_settings(conn)["timeouts.round_timeout_hours"] == 36
    snapshot = conn.execute(
        "SELECT snapshot_json FROM tenant_setting_versions WHERE version=1"
    ).fetchone()
    assert json.loads(snapshot["snapshot_json"])["timeouts.round_timeout_hours"] == 36
    audit = conn.execute(
        "SELECT old_value,new_value FROM config_changes WHERE key=?",
        ("timeouts.round_timeout_hours",),
    ).fetchone()
    assert tuple(audit) == ("24", "36")


def test_save_settings_rejects_url_userinfo(conn):
    import pytest
    with pytest.raises(ValueError):
        save_settings(conn, {"llm.base_url": "https://user:pass@example.com/v1"})
    assert read_settings(conn) == {}
    assert conn.execute("SELECT COUNT(*) FROM tenant_setting_versions").fetchone()[0] == 0


def test_remove_superseded_scheduler_setting(conn):
    save_settings(conn, {"scheduler.jobs.daily_ops.interval_minutes": 60})
    save_settings(
        conn,
        {"scheduler.jobs.daily_ops.cron": "0 9 * * *"},
        remove_keys=["scheduler.jobs.daily_ops.interval_minutes"],
    )
    settings = read_settings(conn)
    assert settings == {"scheduler.jobs.daily_ops.cron": "0 9 * * *"}


def test_metadata_exposes_hard_locks_as_read_only(cfg):
    from src.core.config_store import config_metadata_view
    items = {item["key"]: item for item in config_metadata_view(cfg)}
    locked = items["hot_topics.filters.dimension_match_required"]
    assert locked["editable"] is False
    assert locked["tenant_scoped"] is True
    assert items["timeouts.round_timeout_hours"]["type"] == "integer"


def test_secret_query_is_redacted_from_history_but_preserved_for_runtime(conn):
    url = "https://feeds.example/rss?access_token=TOPSECRET"
    save_settings(
        conn,
        {"hot_topics.sources": [{"name": "private", "type": "rss", "url": url}]},
        audit_rows=[("hot_topics.sources", None, url)],
    )
    assert read_settings(conn)["hot_topics.sources"][0]["url"] == url
    snapshot = conn.execute("SELECT snapshot_json FROM tenant_setting_versions").fetchone()[0]
    audit = conn.execute("SELECT new_value FROM config_changes").fetchone()[0]
    assert "TOPSECRET" not in snapshot
    assert "TOPSECRET" not in audit


def test_audit_failure_rolls_back_settings_and_version(conn):
    import sqlite3
    import pytest
    conn.execute(
        "CREATE TRIGGER reject_config_audit BEFORE INSERT ON config_changes "
        "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        save_settings(
            conn,
            {"timeouts.round_timeout_hours": 48},
            audit_rows=[("timeouts.round_timeout_hours", "24", "48")],
        )
    assert read_settings(conn) == {}
    assert conn.execute("SELECT COUNT(*) FROM tenant_setting_versions").fetchone()[0] == 0


def test_settings_are_isolated_between_database_files(cfg, conn, tmp_path):
    from src.core.db import Database
    from src.core.config_repository import save_settings
    second_paths = cfg.paths.model_copy(update={
        "data_dir": str(tmp_path / "tenant-b" / "data"),
        "db_file": str(tmp_path / "tenant-b" / "easel.db"),
        "backup_dir": str(tmp_path / "tenant-b" / "backups"),
        "output_dir": str(tmp_path / "tenant-b" / "data" / "outputs"),
        "screenshot_dir": str(tmp_path / "tenant-b" / "data" / "screenshots"),
        "log_dir": str(tmp_path / "tenant-b" / "data" / "logs"),
    })
    second_cfg = cfg.model_copy(update={"paths": second_paths})
    db = Database(second_cfg)
    db.initialize()
    second = db.connect()
    try:
        save_settings(conn, {"timeouts.round_timeout_hours": 24})
        save_settings(second, {"timeouts.round_timeout_hours": 72})
        assert read_settings(conn)["timeouts.round_timeout_hours"] == 24
        assert read_settings(second)["timeouts.round_timeout_hours"] == 72
    finally:
        second.close()
