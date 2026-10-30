from __future__ import annotations

import pytest
from fastapi import HTTPException

import src.api.main as api_main


def _route(path: str):
    return next(
        route for route in api_main.app.routes
        if getattr(route, "path", None) == path
    )


def test_platform_account_api_rejects_second_registration_even_if_first_is_inactive(conn, monkeypatch):
    monkeypatch.setitem(api_main._g_ctx, "conn", conn)
    monkeypatch.setitem(api_main._g_ctx, "runtime_tenant_id", "default")
    create = _route("/api/platform-accounts")
    first = create.endpoint(api_main.PlatformAccountRequest(
        platform="xhs", account_handle="xhs-first", display_name="小红书", status="inactive",
    ))
    assert first["status"] == "inactive"

    with pytest.raises(HTTPException) as exc:
        create.endpoint(api_main.PlatformAccountRequest(
            platform="xhs", account_handle="xhs-second", display_name="另一个小红书",
        ))
    assert exc.value.status_code == 409
    assert "每个平台只能登记一个账号" in str(exc.value.detail)


def test_platform_account_update_cannot_move_account_into_occupied_platform(conn, monkeypatch):
    monkeypatch.setitem(api_main._g_ctx, "conn", conn)
    monkeypatch.setitem(api_main._g_ctx, "runtime_tenant_id", "default")
    create = _route("/api/platform-accounts")
    update = _route("/api/platform-accounts/{account_id}")
    xhs = create.endpoint(api_main.PlatformAccountRequest(
        platform="xhs", account_handle="xhs-account",
    ))
    create.endpoint(api_main.PlatformAccountRequest(
        platform="douyin", account_handle="douyin-account",
    ))

    with pytest.raises(HTTPException) as exc:
        update.endpoint(
            xhs["id"],
            api_main.PlatformAccountRequest(
                platform="douyin", account_handle="xhs-account",
            ),
        )
    assert exc.value.status_code == 409


def test_platform_account_migration_preflight_reports_legacy_duplicates():
    import sqlite3

    from src.core.db import Database

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "CREATE TABLE platform_accounts (id INTEGER PRIMARY KEY, platform TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO platform_accounts(platform) VALUES(?)",
            [("xhs",), ("xhs",), ("douyin",)],
        )
        with pytest.raises(RuntimeError, match="人工确认并删除/合并"):
            Database._preflight_platform_account_singleton(conn)
    finally:
        conn.close()


def test_database_unique_index_rejects_second_account_on_same_platform(conn):
    import sqlite3

    indexes = {
        row["name"] for row in conn.execute("PRAGMA index_list(platform_accounts)").fetchall()
    }
    assert "uq_platform_accounts_one_per_platform" in indexes

    conn.execute(
        "INSERT INTO platform_accounts(platform,account_handle,normalized_handle,display_name,status,notes) "
        "VALUES('xhs','first','first','','active','')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO platform_accounts(platform,account_handle,normalized_handle,display_name,status,notes) "
            "VALUES('xhs','second','second','','inactive','')"
        )
