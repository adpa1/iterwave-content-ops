from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from src.api import main as api_main
from src.core import media_generation
from src.core.tenant_registry import TenantRegistry


def _client(test_auth):
    return test_auth(TestClient(api_main._build_app(), base_url="http://127.0.0.1"))


def test_failed_image_generation_does_not_consume_monthly_quota(
    conn, cfg, test_auth, monkeypatch, tmp_path
):
    registry = TenantRegistry(tmp_path / "workspace")
    registry.initialize()
    conn.execute(
        "INSERT INTO media_generation_settings (id, enabled, monthly_quota, prompt_prefix, model, size) "
        "VALUES (1, 1, 1, '', 'test-model', '1024x1024') "
        "ON CONFLICT(id) DO UPDATE SET enabled=1, monthly_quota=1, model='test-model', size='1024x1024'"
    )
    conn.commit()

    def fail_provider(*args, **kwargs):
        raise media_generation.ImageGenerationError("provider error key=sk-masked-secret-fragment")

    monkeypatch.setattr(media_generation, "generate_image_bytes", fail_provider)
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({
        "conn": conn,
        "cfg": cfg,
        "tenant_registry": registry,
        "runtime_tenant_id": "default",
    })
    try:
        client = _client(test_auth)
        for _ in range(2):
            response = client.post("/api/media/generate", json={"prompt": "a detailed test image prompt"})
            assert response.status_code == 502
            assert "sk-masked-secret-fragment" not in response.text
            assert "provider error" not in response.text
        settings = client.get("/api/media/generation-settings")
        assert settings.status_code == 200
        assert settings.json()["used_this_month"] == 0
        jobs = conn.execute("SELECT status FROM media_generation_jobs").fetchall()
        assert len(jobs) == 2
        assert all(row["status"] == "failed" for row in jobs)
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)


def test_concurrent_image_generation_is_rejected_without_creating_a_job(
    conn, cfg, test_auth, tmp_path
):
    registry = TenantRegistry(tmp_path / "workspace")
    registry.initialize()
    conn.execute(
        "INSERT INTO media_generation_settings (id, enabled, monthly_quota, prompt_prefix, model, size) "
        "VALUES (1, 1, 5, '', 'test-model', '1024x1024') "
        "ON CONFLICT(id) DO UPDATE SET enabled=1, monthly_quota=5, model='test-model', size='1024x1024'"
    )
    conn.commit()
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({
        "conn": conn,
        "cfg": cfg,
        "tenant_registry": registry,
        "runtime_tenant_id": "default",
    })
    api_main._media_generation_lock.acquire()
    try:
        response = _client(test_auth).post(
            "/api/media/generate", json={"prompt": "a detailed test image prompt"}
        )
        assert response.status_code == 409
        assert "正在执行" in response.json()["detail"]
        assert conn.execute("SELECT COUNT(*) FROM media_generation_jobs").fetchone()[0] == 0
    finally:
        api_main._media_generation_lock.release()
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)


def test_media_generation_settings_do_not_expose_provider_url_or_key_variable(
    conn, test_auth, monkeypatch
):
    monkeypatch.setenv("ITERWAVE_IMAGE_API_BASE_URL", "https://internal-provider.example/v1")
    monkeypatch.setenv("ITERWAVE_IMAGE_API_KEY_ENV", "OPENAI_API_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({"conn": conn, "runtime_tenant_id": "default"})
    try:
        response = _client(test_auth).get("/api/media/generation-settings")
        assert response.status_code == 200
        body = response.json()
        assert body["provider_configured"] is True
        assert "api_base_url" not in body
        assert "api_key_env" not in body
        assert "internal-provider.example" not in response.text
        assert "OPENAI_API_KEY" not in response.text
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)


def test_tenant_create_response_does_not_return_server_paths(tmp_path, test_auth):
    registry = TenantRegistry(tmp_path / "workspace")
    registry.initialize()
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({"tenant_registry": registry, "runtime_tenant_id": "default"})
    try:
        response = _client(test_auth).post(
            "/api/tenants", json={"tenant_id": "test_brand", "name": "测试账号组"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["tenant"] == {"id": "test_brand", "name": "测试账号组"}
        assert body["storage_ready"] is True
        assert "paths" not in body
        assert str(tmp_path) not in response.text
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)


def test_empty_body_cannot_trigger_content_image_generation(conn, seed, cfg, test_auth, tmp_path):
    registry = TenantRegistry(tmp_path / "workspace")
    registry.initialize()
    round_id = seed.round()
    topic_id = seed.topic("empty-body-topic")
    conn.execute(
        "INSERT INTO contents (id, round_id, topic_id, platform, title, body) "
        "VALUES ('empty-body-content', ?, ?, 'xhs', '有标题', '')",
        (round_id, topic_id),
    )
    conn.commit()
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({
        "conn": conn,
        "cfg": cfg,
        "tenant_registry": registry,
        "runtime_tenant_id": "default",
    })
    try:
        response = _client(test_auth).post(
            "/api/media/generate-for-content/empty-body-content"
        )
        assert response.status_code == 422
        assert "补充正文" in response.json()["detail"]
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)


def test_recommendation_limit_can_return_more_than_eight_items(conn, seed, test_auth):
    round_id = seed.round()
    topic_id = seed.topic("many-media-topic")
    conn.execute(
        "INSERT INTO contents (id, round_id, topic_id, platform, title, body) "
        "VALUES ('many-media-content', ?, ?, 'xhs', '产品设计介绍', '产品设计细节和使用方式')",
        (round_id, topic_id),
    )
    for asset_id in range(1, 13):
        conn.execute(
            "INSERT INTO media_assets "
            "(id, title, original_filename, stored_filename, alt_text, tags, quality_score, width, height, status, review_status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 80, 1200, 900, 'active', 'approved', '2026-10-01')",
            (asset_id, f"产品设计素材 {asset_id}", f"product-{asset_id}.jpg", f"product-{asset_id}.jpg",
             "产品设计细节", '["产品", "设计", "细节"]'),
        )
    conn.commit()
    saved = dict(api_main._g_ctx)
    api_main._g_ctx.clear()
    api_main._g_ctx.update({"conn": conn, "runtime_tenant_id": "default"})
    try:
        response = _client(test_auth).get(
            "/api/media/recommendations?content_id=many-media-content&usage_role=cover&limit=20"
        )
        assert response.status_code == 200
        assert len(response.json()["items"]) > 8
        assert len(response.json()["items"]) <= 20
    finally:
        api_main._g_ctx.clear()
        api_main._g_ctx.update(saved)
