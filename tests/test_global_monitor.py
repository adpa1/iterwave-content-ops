from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from src.api import main as api_main
from src.core.tenant_registry import TenantRegistry


def _seed_tenant_db(db_path: Path, title: str, platform: str = "xhs") -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE contents (id TEXT PRIMARY KEY, platform TEXT, created_at TEXT);
        CREATE TABLE drafts (id TEXT PRIMARY KEY, platform TEXT, status TEXT);
        CREATE TABLE media_assets (id INTEGER PRIMARY KEY, status TEXT);
        CREATE TABLE media_asset_usages (id INTEGER PRIMARY KEY);
    """)
    conn.execute("INSERT INTO contents VALUES (?, ?, ?)", (title, platform, datetime.now().isoformat(sep=" ")))
    conn.execute("INSERT INTO drafts VALUES (?, ?, 'draft_saved')", ("draft-" + title, platform))
    conn.execute("INSERT INTO media_assets VALUES (1, 'active')")
    conn.execute("INSERT INTO media_asset_usages VALUES (1)")
    conn.commit()
    conn.close()


def test_global_monitor_aggregates_tenant_dbs_read_only(tmp_path, test_auth):
    registry = TenantRegistry(tmp_path / "workspace")
    registry.initialize()
    registry.create("brand_a", "品牌 A")
    for tenant_id in ("default", "brand_a"):
        paths = registry.paths(tenant_id, create=True)
        _seed_tenant_db(paths.db_file, "content-" + tenant_id, "xhs" if tenant_id == "default" else "douyin")

    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({
        "tenant_registry": registry,
        "runtime_tenant_id": "default",
    })
    try:
        client = test_auth(TestClient(api_main._build_app(), base_url="http://127.0.0.1"))
        response = client.get("/api/global-monitor?days=30")
        assert response.status_code == 200
        body = response.json()
        assert body["totals"]["tenants"] == 2
        assert body["totals"]["available_tenants"] == 2
        assert body["totals"]["contents_all"] == 2
        assert body["totals"]["drafts_saved"] == 2
        assert body["totals"]["media_active"] == 2
        assert body["platform_totals"]["xhs"]["contents"] == 1
        assert body["platform_totals"]["douyin"]["contents"] == 1
        assert body["account_count_note"]
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)
