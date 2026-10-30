from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from src.api import main


@pytest.fixture()
def activation_client(cfg, conn, test_auth, monkeypatch):
    cfg.llm.enabled = False

    @asynccontextmanager
    async def no_lifespan(_app):
        yield

    monkeypatch.setattr(main.app.router, "lifespan_context", no_lifespan)
    monkeypatch.setattr(main, "_g_ctx", {
        "conn": conn,
        "cfg": cfg,
        "runtime_tenant_id": "default",
        "notifier": None,
    })
    with TestClient(main.app) as client:
        test_auth(client)
        yield client


def test_activation_status_creates_one_cohort_event(activation_client, conn):
    first = activation_client.get("/api/activation/status")
    second = activation_client.get("/api/activation/status")
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["model_mode"] == "byok"
    assert first.json()["model_ready"] is False
    assert conn.execute("SELECT COUNT(*) FROM activation_cohort").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM content_events WHERE event_type='activation_started'"
    ).fetchone()[0] == 1


def test_activation_samples_without_model_fail_without_content(activation_client, conn):
    response = activation_client.post("/api/activation/samples", json={
        "account_description": "分享产品经理的 AI 工具实践和效率方法",
        "platform": "xhs",
        "idempotency_key": "request-no-model",
    })
    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["error_code"] == "model_not_configured"
    assert conn.execute("SELECT COUNT(*) FROM sample_contents").fetchone()[0] == 0


def test_activation_sample_read_is_tenant_scoped(activation_client, conn):
    from datetime import datetime
    now = datetime.now().isoformat(sep=" ")
    conn.execute(
        "INSERT INTO sample_runs (id, tenant_id, platform, account_description, status, idempotency_key, created_at) "
        "VALUES ('foreign-run', 'other-tenant', 'xhs', '分享产品经理的 AI 工具实践', 'ready', 'foreign-key', ?)",
        (now,),
    )
    conn.commit()
    response = activation_client.get("/api/activation/samples/foreign-run")
    assert response.status_code == 404


def test_manual_metrics_preserve_null_and_note_url_is_platform_validated(activation_client, conn, seed):
    round_id = seed.round()
    draft_id = seed.draft(round_id, platform="xhs", draft_status="published", fetch_status=None)

    bad_link = activation_client.post(
        f"/api/drafts/{draft_id}/note_url",
        json={"note_url": "https://www.douyin.com/video/123"},
    )
    assert bad_link.status_code == 422

    good_link = activation_client.post(
        f"/api/drafts/{draft_id}/note_url",
        json={"note_url": "https://www.xiaohongshu.com/explore/abc"},
    )
    assert good_link.status_code == 200

    metrics = activation_client.post(
        f"/api/drafts/{draft_id}/metrics/manual",
        json={"views": 128, "likes": 12, "collects": None, "measured_at": "2026-10-30T10:00:00"},
    )
    assert metrics.status_code == 200
    row = conn.execute(
        "SELECT source, views, likes, collects, measured_at, recorded_at "
        "FROM metric_snapshots WHERE draft_id=?",
        (draft_id,),
    ).fetchone()
    assert row["source"] == "manual"
    assert row["views"] == 128
    assert row["likes"] == 12
    assert row["collects"] is None
    assert row["measured_at"] != row["recorded_at"] or row["recorded_at"] is not None


def test_weekly_report_does_not_invent_reasons_for_small_sample(activation_client):
    response = activation_client.get("/api/reports/weekly")
    assert response.status_code == 200
    payload = response.json()
    assert payload["best_content"] == []
    assert payload["next_actions"] == []
    assert "样本不足" in payload["why_it_may_work"][0]["statement"]


def test_activation_metrics_marks_unfinished_seven_day_window_as_observing(activation_client):
    response = activation_client.get("/api/activation/metrics")
    assert response.status_code == 200
    payload = response.json()
    assert payload["cohort_size"] == 0
    assert payload["seven_day_activation_rate"] is None
