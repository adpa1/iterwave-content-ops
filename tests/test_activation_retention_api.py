from __future__ import annotations

from contextlib import asynccontextmanager
import time

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


def test_activation_sample_success_stays_outside_formal_production(activation_client, conn, monkeypatch):
    cfg = main._g_ctx["cfg"]
    cfg.llm.enabled = True
    monkeypatch.setenv(cfg.llm.api_key_env, "test-only-key")
    from src.core import content_pipeline

    body = (
        "个人成长 实用经验 产品经理 AI工具 需求分析 用户研究 项目管理 工作效率 数据分析 "
        "业务流程 产品设计 用户反馈 方案验证 目标拆解 方法总结 实践案例 场景分析 效率提升 团队协作 持续迭代 经验复盘"
    )
    monkeypatch.setattr(
        content_pipeline, "generate_copy_content",
        lambda *args, **kwargs: ("产品经理的实用方法", body, ["产品经理", "实用经验"]),
    )
    response = activation_client.post("/api/activation/samples", json={
        "account_description": "分享产品经理的 AI 工具实践和效率方法",
        "platform": "xhs",
        "idempotency_key": "request-success",
    })
    assert response.status_code == 202
    run_id = response.json()["sample_run_id"]
    result = None
    for _ in range(100):
        result = activation_client.get(f"/api/activation/samples/{run_id}").json()
        if result["run"]["status"] in ("ready", "failed"):
            break
        time.sleep(0.02)
    assert result["run"]["status"] == "ready", result["run"]
    assert len(result["contents"]) == 3
    assert all(item["quality_status"] == "pass" for item in result["contents"])
    assert conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0


def test_daily_quota_is_configured_and_idempotency_precedes_quota(activation_client, conn):
    main._g_ctx["cfg"].activation.max_runs_per_day = 1
    payload = {
        "account_description": "分享产品经理的 AI 工具实践和效率方法",
        "platform": "xhs",
        "idempotency_key": "daily-quota-1",
    }
    first = activation_client.post("/api/activation/samples", json=payload)
    assert first.status_code == 202
    repeated = activation_client.post("/api/activation/samples", json=payload)
    assert repeated.status_code == 202
    assert repeated.json()["deduplicated"] is True
    second = activation_client.post("/api/activation/samples", json={
        **payload, "idempotency_key": "daily-quota-2",
    })
    assert second.status_code == 429
    assert conn.execute("SELECT COUNT(*) FROM sample_runs").fetchone()[0] == 1


def test_platform_metric_fields_are_enforced(activation_client, seed):
    round_id = seed.round()
    draft_id = seed.draft(round_id, platform="douyin", draft_status="published", fetch_status=None)
    unsupported = activation_client.post(
        f"/api/drafts/{draft_id}/metrics/manual",
        json={"views": 20, "collects": 3},
    )
    assert unsupported.status_code == 422
    accepted = activation_client.post(
        f"/api/drafts/{draft_id}/metrics/manual",
        json={"views": 20, "likes": 2, "comments": 1},
    )
    assert accepted.status_code == 200


def test_sample_delete_cascades_sample_events_and_contents(activation_client, conn):
    from datetime import datetime
    now = datetime.now().isoformat(sep=" ")
    conn.execute(
        "INSERT INTO sample_runs (id, tenant_id, platform, account_description, status, idempotency_key, created_at) "
        "VALUES ('delete-run', 'default', 'xhs', '分享产品经理实践方法', 'ready', 'delete-key', ?)",
        (now,),
    )
    conn.execute(
        "INSERT INTO sample_contents "
        "(id, sample_run_id, tenant_id, platform, angle, title, body, hashtags_json, quality_status, "
        "quality_findings_json, created_at, updated_at) "
        "VALUES ('delete-sample', 'delete-run', 'default', 'xhs', '实用方法', '标题', '正文', '[]', 'pass', '[]', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO content_events "
        "(id, tenant_id, sample_run_id, event_type, event_at, source, metadata_json) "
        "VALUES ('delete-start', 'default', 'delete-run', 'sample_generation_started', ?, 'web', '{}')",
        (now,),
    )
    conn.execute(
        "INSERT INTO content_events "
        "(id, tenant_id, sample_content_id, event_type, event_at, source, metadata_json) "
        "VALUES ('delete-save', 'default', 'delete-sample', 'content_saved', ?, 'web', '{}')",
        (now,),
    )
    conn.commit()
    response = activation_client.delete("/api/activation/samples/delete-run")
    assert response.status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM sample_runs WHERE id='delete-run'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sample_contents WHERE id='delete-sample'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM content_events WHERE id IN ('delete-start','delete-save')").fetchone()[0] == 0


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
    draft_id = seed.draft(round_id, platform="xhs", draft_status="failed", fetch_status=None)
    from datetime import datetime
    conn.execute(
        "UPDATE drafts SET user_confirmed_at=? WHERE id=?",
        (datetime.now().isoformat(sep=" "), draft_id),
    )
    conn.commit()

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


def test_weekly_report_uses_latest_corrected_snapshot_and_within_platform_evidence(activation_client, conn, seed):
    now = __import__("datetime").datetime.now().isoformat(sep=" ")
    round_id = seed.round()
    drafts = []
    for i, views in enumerate((100, 200, 300), start=1):
        draft_id = seed.draft(round_id, platform="xhs", draft_status="published", fetch_status=None, topic_id=f"weekly-{i}")
        content_id = conn.execute("SELECT content_id FROM drafts WHERE id=?", (draft_id,)).fetchone()[0]
        conn.execute("UPDATE contents SET title=?, body=? WHERE id=?",
                     (f"第{i}篇实测标题", f"用于复盘的正文样本 {i}", content_id))
        conn.commit()
        submitted = activation_client.post(
            f"/api/drafts/{draft_id}/metrics/manual",
            json={"views": views, "measured_at": now},
        )
        assert submitted.status_code == 200, submitted.text
        drafts.append((draft_id, content_id, submitted.json()["snapshot_id"]))

    correction = activation_client.post(
        f"/api/drafts/{drafts[2][0]}/metrics/manual",
        json={"views": 50, "measured_at": now, "correction_of": drafts[2][2]},
    )
    assert correction.status_code == 200, correction.text

    report = activation_client.get("/api/reports/weekly").json()
    views_winner = next(item for item in report["best_content"] if item["metric"] == "views")
    assert views_winner["content_id"] == drafts[1][1]
    assert views_winner["value"] == 200
    assert report["sample_size"] == 3
    assert report["effective_review_count"] == 3
    assert report["why_it_may_work"][0]["sample_size"] == 3
    assert report["next_actions"][0]["evidence_content_ids"]


def test_activation_metrics_calculates_completed_cohort_rates_and_ttv(activation_client, conn, seed):
    from datetime import datetime, timedelta
    started = datetime.now() - timedelta(days=8)
    completed = started + timedelta(seconds=120)
    now = datetime.now()
    conn.execute(
        "INSERT INTO activation_cohort (id, tenant_id, first_seen_at) VALUES (1, 'default', ?)",
        (started.isoformat(sep=" "),),
    )
    conn.execute(
        "INSERT INTO sample_runs "
        "(id, tenant_id, platform, account_description, status, idempotency_key, created_at, started_at, completed_at) "
        "VALUES ('cohort-run', 'default', 'xhs', '分享产品经理实践方法', 'ready', 'cohort-key', ?, ?, ?)",
        (started.isoformat(sep=" "), started.isoformat(sep=" "), completed.isoformat(sep=" ")),
    )
    conn.execute(
        "INSERT INTO sample_contents "
        "(id, sample_run_id, tenant_id, platform, angle, title, body, hashtags_json, quality_status, "
        "quality_findings_json, created_at, updated_at) "
        "VALUES ('cohort-sample', 'cohort-run', 'default', 'xhs', '经验洞察', '样稿标题', '样稿正文', '[]', 'pass', '[]', ?, ?)",
        (started.isoformat(sep=" "), started.isoformat(sep=" ")),
    )
    _round = seed.round()
    draft_id = seed.draft(_round, platform="xhs", draft_status="published", fetch_status=None)
    content_id = conn.execute("SELECT content_id FROM drafts WHERE id=?", (draft_id,)).fetchone()[0]
    conn.execute("UPDATE drafts SET user_confirmed_at=? WHERE id=?",
                 ((started + timedelta(hours=2)).isoformat(sep=" "), draft_id))
    conn.execute(
        "INSERT INTO content_events (id, tenant_id, sample_content_id, event_type, event_at, source, metadata_json) "
        "VALUES ('cohort-save', 'default', 'cohort-sample', 'content_saved', ?, 'web', '{}')",
        ((started + timedelta(hours=1)).isoformat(sep=" "),),
    )
    conn.execute(
        "INSERT INTO content_events (id, tenant_id, content_id, event_type, event_at, source, metadata_json) "
        "VALUES ('cohort-publish', 'default', ?, 'publish_confirmed', ?, 'web', '{}')",
        (content_id, (started + timedelta(hours=2)).isoformat(sep=" ")),
    )
    conn.execute(
        "INSERT INTO content_events (id, tenant_id, content_id, event_type, event_at, source, metadata_json) "
        "VALUES ('cohort-adopt', 'default', ?, 'content_adopted', ?, 'web', '{}')",
        (content_id, (started + timedelta(hours=2)).isoformat(sep=" ")),
    )
    now_text = now.isoformat(sep=" ")
    measured_text = (started + timedelta(hours=3)).isoformat(sep=" ")
    recorded_text = (started + timedelta(hours=4)).isoformat(sep=" ")
    conn.execute(
        "INSERT INTO metric_snapshots "
        "(id, tenant_id, draft_id, content_id, platform, source, views, measured_at, recorded_at, created_at) "
        "VALUES ('cohort-metric', 'default', ?, ?, 'xhs', 'manual', 10, ?, ?, ?)",
        (draft_id, content_id, measured_text, recorded_text, now_text),
    )
    conn.commit()

    result = activation_client.get("/api/activation/metrics")
    assert result.status_code == 200
    payload = result.json()
    assert payload["cohort_size"] == 1
    assert payload["observation_window_complete"] is True
    assert payload["ttv_seconds"] == pytest.approx(120, abs=1)
    assert payload["seven_day_activation_rate"] == 1.0
    assert payload["first_publish_rate"] == 1.0
    assert payload["data_return_rate"] == 1.0


def test_activation_metrics_marks_unfinished_seven_day_window_as_observing(activation_client):
    response = activation_client.get("/api/activation/metrics")
    assert response.status_code == 200
    payload = response.json()
    assert payload["cohort_size"] == 0
    assert payload["seven_day_activation_rate"] is None
