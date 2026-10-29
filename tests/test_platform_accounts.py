from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from src.api import main as api_main


def test_platform_account_registry_enforces_platform_scoped_identity_uniqueness():
    migration = Path(__file__).resolve().parents[1] / "migrations" / "028_platform_accounts.sql"
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(migration.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO platform_accounts (platform, account_handle, normalized_handle) VALUES (?, ?, ?)", ("xhs", "CreatorA", "creatora"))
        conn.execute("INSERT INTO platform_accounts (platform, account_handle, normalized_handle) VALUES (?, ?, ?)", ("douyin", "CreatorA", "creatora"))
        try:
            conn.execute("INSERT INTO platform_accounts (platform, account_handle, normalized_handle) VALUES (?, ?, ?)", ("xhs", "creatora", "creatora"))
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("same platform identity must be unique after case normalization")
        assert conn.execute("SELECT COUNT(*) FROM platform_accounts").fetchone()[0] == 2
    finally:
        conn.close()

def test_platform_account_api_creates_and_rejects_duplicate_identity(test_auth):
    migration = Path(__file__).resolve().parents[1] / "migrations" / "028_platform_accounts.sql"
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(migration.read_text(encoding="utf-8"))
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({"conn": conn, "runtime_tenant_id": "default"})
    try:
        client = test_auth(TestClient(api_main._build_app(), base_url="http://127.0.0.1"))
        payload = {"platform": "xhs", "account_handle": "Creator-A", "display_name": "创作者 A", "status": "active", "notes": ""}
        created = client.post("/api/platform-accounts", json=payload)
        assert created.status_code == 201
        assert created.json()["account_handle"] == "Creator-A"
        duplicate = client.post("/api/platform-accounts", json={**payload, "account_handle": "creator-a"})
        assert duplicate.status_code == 409
        listing = client.get("/api/platform-accounts")
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        assert listing.json()["active_total"] == 1
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)
        conn.close()
