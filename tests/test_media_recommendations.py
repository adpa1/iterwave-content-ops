from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from src.api import main as api_main


def test_media_recommendations_match_keywords_and_exclude_unapproved(test_auth):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE contents (id TEXT PRIMARY KEY, title TEXT, body TEXT, platform TEXT);
        CREATE TABLE media_assets (
            id INTEGER PRIMARY KEY, title TEXT, original_filename TEXT, alt_text TEXT, tags TEXT,
            quality_score INTEGER, width INTEGER, height INTEGER, status TEXT, review_status TEXT, created_at TEXT
        );
    """)
    conn.execute("INSERT INTO contents VALUES (?, ?, ?, ?)", (
        "content-1", "青岛海鲜烧烤店", "新鲜海鲜、门头招牌和店内环境介绍", "xhs"
    ))
    conn.execute("INSERT INTO media_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        1, "青岛海鲜门头", "store-front.jpg", "海鲜烧烤招牌",
        '["青岛", "海鲜", "门头", "封面"]', 90, 1200, 900, "active", "approved", "2026-10-01"
    ))
    conn.execute("INSERT INTO media_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        2, "青岛海鲜候选图", "candidate.jpg", "海鲜店内",
        '["青岛", "海鲜"]', 95, 1200, 900, "active", "pending_review", "2026-10-02"
    ))
    conn.execute("INSERT INTO media_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        3, "无关风景图", "mountain.jpg", "山景",
        '["山景", "旅行"]', 99, 1200, 900, "active", "approved", "2026-10-03"
    ))
    conn.commit()
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({"conn": conn, "runtime_tenant_id": "default"})
    try:
        client = test_auth(TestClient(api_main._build_app(), base_url="http://127.0.0.1"))
        response = client.get("/api/media/recommendations?content_id=content-1&usage_role=cover")
        assert response.status_code == 200
        body = response.json()
        assert body["method"] == "keyword_match"
        assert body["automatic_assignment"] is False
        assert body["items"]
        assert body["items"][0]["asset_id"] == 1
        assert "海鲜" in body["items"][0]["matched_keywords"]
        assert all(item["asset_id"] != 2 for item in body["items"])
        assert all(item["asset_id"] != 3 for item in body["items"])
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)
        conn.close()
