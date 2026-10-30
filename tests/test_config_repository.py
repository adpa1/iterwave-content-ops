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
