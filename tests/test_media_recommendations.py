from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from src.api import main as api_main


def test_media_recommendations_match_keywords_and_exclude_unapproved(test_auth):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE contents (id TEXT PRIMARY KEY, title TEXT, body TEXT, platform TEXT);
        CREATE TABLE media_assets (
            id INTEGER PRIMARY KEY, title TEXT, original_filename TEXT, stored_filename TEXT, alt_text TEXT, tags TEXT,
            quality_score INTEGER, width INTEGER, height INTEGER, status TEXT, review_status TEXT, created_at TEXT
        );
    """)
    conn.execute("INSERT INTO contents VALUES (?, ?, ?, ?)", (
        "content-1", "青岛海鲜烧烤店", "新鲜海鲜、门头招牌和店内环境介绍", "xhs"
    ))
    conn.execute("INSERT INTO media_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        1, "青岛海鲜门头", "store-front.jpg", "store-front.jpg", "海鲜烧烤招牌",
        '["青岛", "海鲜", "门头", "封面"]', 90, 1200, 900, "active", "approved", "2026-10-01"
    ))
    conn.execute("INSERT INTO media_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        2, "青岛海鲜候选图", "candidate.jpg", "candidate.jpg", "海鲜店内",
        '["青岛", "海鲜"]', 95, 1200, 900, "active", "pending_review", "2026-10-02"
    ))
    conn.execute("INSERT INTO media_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        3, "无关风景图", "mountain.jpg", "mountain.jpg", "山景",
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
        assert "门头" not in body["items"][0]["purpose_matches"]
        assert all(item["asset_id"] != 2 for item in body["items"])
        assert all(item["asset_id"] != 3 for item in body["items"])
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)
        conn.close()



def test_media_recommendations_use_vision_ranker_when_available(test_auth, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from src.core import media_semantic

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE contents (id TEXT PRIMARY KEY, title TEXT, body TEXT, platform TEXT);
        CREATE TABLE media_assets (
            id INTEGER PRIMARY KEY, title TEXT, original_filename TEXT, stored_filename TEXT,
            alt_text TEXT, tags TEXT, quality_score INTEGER, width INTEGER, height INTEGER,
            status TEXT, review_status TEXT, created_at TEXT
        );
    """)
    conn.execute("INSERT INTO contents VALUES (?, ?, ?, ?)", ("c1", "户外帐篷", "山地露营场景", "xhs"))
    conn.execute("INSERT INTO media_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        7, "帐篷营地图", "camp.jpg", "asset-7.jpg", "山地露营帐篷",
        '["露营"]', 88, 1200, 1600, "active", "approved", "2026-10-01",
    ))
    conn.commit()
    seen = {}

    def fake_ranker(**kwargs):
        seen.update(kwargs)
        return [{"asset_id": 7, "reason": "画面包含帐篷和露营环境"}]

    monkeypatch.setattr(media_semantic, "rank_media_with_vision", fake_ranker)
    registry = SimpleNamespace(paths=lambda tenant_id: SimpleNamespace(media_dir=tmp_path))
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({
        "conn": conn, "runtime_tenant_id": "ta", "tenant_registry": registry,
    })
    try:
        client = test_auth(TestClient(api_main._build_app(), base_url="http://127.0.0.1"))
        response = client.get("/api/media/recommendations?content_id=c1&usage_role=cover")
        assert response.status_code == 200
        body = response.json()
        assert body["method"] == "vision_model"
        assert body["items"][0]["asset_id"] == 7
        assert seen["candidates"][0]["stored_filename"] == "asset-7.jpg"
        assert seen["media_dir"] == tmp_path
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)
        conn.close()
