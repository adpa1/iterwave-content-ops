from __future__ import annotations

import sqlite3
from pathlib import Path


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
